import hashlib
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from loguru import logger

from app.config import (
    EVENTS_MEASUREMENT,
    INFLUX_BUCKET,
    INFLUX_ORG,
    INFLUX_TOKEN,
    INFLUX_URL,
    MIN_SLEEP_SESSION_SECONDS,
    SENSOR_MEASUREMENT,
    SLEEP_MEASUREMENT,
    TZ_NAME,
)

_client = None


def get_client() -> InfluxDBClient:
    global _client
    if _client is None:
        _client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    return _client


def calendar_event_id(calendar: str, start_time: str, title: str) -> str:
    ''' Deterministic event_id for calendar-derived events, per spec §6 -
    same event synced twice produces the same id, so re-syncs are
    idempotent-ish (see the known stale-tag caveat noted separately).
    '''
    raw = f"{calendar}|{start_time}|{title}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def sleep_entry_id(user: str, session_start_iso: str) -> str:
    ''' Deterministic id for a subjective sleep score entry, derived
    from the underlying sleep SESSION's own start time - not from
    sleep_date. Same pattern as calendar_event_id() above.

    This is the fix for a real bug: two genuinely different sessions
    can share the same sleep_date (e.g. one starting just after local
    midnight, another starting just before the NEXT local midnight -
    both correctly resolve to the same calendar day under the
    "which day did this session start on" rule, but they're not the
    same night). Keying entries by sleep_date alone meant the second
    submission silently overwrote the first's score. Keying by the
    session's own start time instead means only a genuine re-
    submission for the SAME session (same start time) produces the
    same id and correctly overwrites - two different sessions, even
    on the same calendar date, get different ids and coexist.
    '''
    raw = f"{user}|{session_start_iso}".encode()
    return hashlib.sha1(raw).hexdigest()[:12]


def manual_event_id() -> str:
    return str(uuid.uuid4())


def delete_event_tag_point(*, tag: str, source: str, timestamp: datetime, calendar: str | None = None):
    ''' Delete the single Influx point for one (event, tag) pairing.

    event_id is stored as a FIELD, not a tag, on events points - and
    InfluxDB's delete API predicates can only match on tags/measurement,
    not field values. So we can't delete "everything with this event_id"
    directly. Instead this relies on tag + source (+ calendar, for
    calendar-derived events) + the event's exact timestamp being unique
    in practice for a personal calendar/tap log - a narrow (1 second)
    time window keeps this safe from touching neighbouring points.
    Known edge case: two events sharing the same tag/source/calendar
    starting at the exact same second would collide here; for personal
    use this is vanishingly unlikely and not worth the complexity of
    handling. Doesn't filter on `user` (the rest of the predicate scopes
    tightly enough in practice, and `user` isn't guaranteed stable if an
    account were ever renamed - a real but narrow edge case).

    Shared by the reprocess job (reclassifying calendar events under new
    keyword rules) and the manual event/calendar-tag-override edit
    endpoints in main.py - same underlying constraint, same fix.
    '''
    client = get_client()
    delete_api = client.delete_api()
    start = timestamp
    stop = timestamp + timedelta(seconds=1)
    # Key names are quoted (not just values) - InfluxDB's delete predicate
    # parser treats certain bare words as reserved (confirmed: "tag" broke
    # parsing with "bad logical expression, at position 26", landing
    # exactly on the "t" of "tag=" - the same class of bug documented for
    # "from=" in InfluxDB's own issue tracker/forums). Quoting the key
    # itself, not just the value, is the documented fix. Quoting "source"
    # and "calendar" defensively too, since there's no published
    # authoritative list of every reserved word in this specific grammar.
    predicate = f'_measurement="{EVENTS_MEASUREMENT}" AND "tag"="{tag}" AND "source"="{source}"'
    if calendar is not None:
        predicate += f' AND "calendar"="{calendar}"'
    delete_api.delete(start, stop, predicate, bucket=INFLUX_BUCKET, org=INFLUX_ORG)


def find_manual_event_by_id(user: str, event_id: str, lookback_days: int = 1825) -> dict | None:
    ''' Look up a single manual event's current timestamp and tag set by
    event_id, for the edit/delete endpoints. Unlike delete, event_id
    CAN be filtered on directly in a normal query (it's the InfluxDB
    delete API specifically that's restricted to tags) - so this reuses
    the same pivot-then-group approach as find_manual_events_in_range,
    just filtered to one event_id instead of a time range.

    lookback_days defaults to ~5 years since a lookup-by-id has no
    natural "recent" bound the way a timeline view does - the person
    could be editing an old manual tap.
    '''
    client = get_client()
    query_api = client.query_api()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{lookback_days}d)
      |> filter(fn: (r) => r._measurement == "{EVENTS_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.source == "manual")
      |> pivot(rowKey: ["_time", "tag"], columnKey: ["_field"], valueColumn: "_value")
      |> filter(fn: (r) => r.event_id == "{event_id}")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.error(f"Failed to look up manual event {event_id} for user={user}: {e}")
        return None

    tags = set()
    timestamp = None
    duration_min = None
    for table in tables:
        for record in table.records:
            tag_value = record.values.get("tag")
            if tag_value:
                tags.add(tag_value)
            timestamp = record.get_time()
            dm = record.values.get("duration_min")
            if dm is not None:
                duration_min = dm

    if timestamp is None:
        return None

    return {"timestamp": timestamp, "tags": sorted(tags), "duration_min": duration_min}


def write_event_points(*, user: str, tags: list[str], source: str, timestamp: datetime,
                        event_id: str, calendar: str | None = None,
                        duration_min: int | None = None):
    ''' Write one Influx point per tag, all sharing event_id, per spec §6.

    `user` is the InfluxDB `user` tag value - the logged-in session's
    username (see app/auth.py), not a fixed env var, so this now
    correctly separates household members' data as long as each account
    was created with the same username as their ring parser's
    GADGETBRIDGE_USER value (see the note in schema.sql).
    '''
    if not tags:
        logger.warning(f"write_event_points called with no tags (event_id={event_id}) - nothing written")
        return

    client = get_client()
    with client.write_api(write_options=SYNCHRONOUS) as write_api:
        for tag in tags:
            p = (
                Point(EVENTS_MEASUREMENT)
                .tag("tag", tag)
                .tag("source", source)
                .tag("user", user)
                .field("value", 1)
                .field("event_id", event_id)
                .time(timestamp)
            )
            if calendar is not None:
                p = p.tag("calendar", calendar)
            if duration_min is not None:
                p = p.field("duration_min", duration_min)
            write_api.write(INFLUX_BUCKET, INFLUX_ORG, p)

    logger.debug(f"Wrote {len(tags)} event point(s) for event_id={event_id} tags={tags} user={user}")


def write_sleep_point(*, user: str, session_start: datetime, sleep_date: str, score: int,
                       qualifiers: dict, submission_ts: datetime):
    ''' One point per SESSION, not per date - anchored at the actual
    session start time, with a deterministic entry_id (see
    sleep_entry_id()) as the stable tag used for addressing edits/
    deletes. Fixed a real bug: the old version anchored on a synthetic
    "midnight of sleep_date" timestamp, so two different sessions
    sharing a calendar date (one starting just after local midnight,
    another just before the next one) silently overwrote each other.
    Anchoring on the real session start + a start-time-derived id means
    a genuine re-submission for the SAME session still overwrites
    correctly (same start time -> same id -> same point), while two
    different sessions never collide regardless of what date they
    land on.

    sleep_date is still written as a tag (not just a display label) -
    kept for date-range querying convenience ("all entries logged
    around Aug 31") even though it's no longer the uniqueness key.
    '''
    entry_id = sleep_entry_id(user, session_start.isoformat())
    client = get_client()
    with client.write_api(write_options=SYNCHRONOUS) as write_api:
        p = (
            Point(SLEEP_MEASUREMENT)
            .tag("sleep_date", sleep_date)
            .tag("user", user)
            .tag("entry_id", entry_id)
            .field("score", score)
            .field("logged_at", submission_ts.isoformat())
        )
        for qualifier, value in qualifiers.items():
            p = p.field(qualifier, bool(value))

        p = p.time(session_start)

        write_api.write(INFLUX_BUCKET, INFLUX_ORG, p)

    logger.info(f"Wrote subjective_sleep point for {sleep_date} (entry_id={entry_id}): score={score} user={user}")
    return entry_id


def find_sleep_entries_in_range(user: str, start: datetime, end: datetime) -> list[dict]:
    ''' Read-only query for subjective sleep entries in the given range.
    Unlike events, each sleep entry is a single point with all fields
    (score, logged_at, qualifiers) together - no per-tag multi-point
    reconstruction needed, just a pivot to combine the fields onto one
    row per point.
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SLEEP_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.error(f"Failed to query sleep entries for user={user}: {e}")
        return []

    # Keys that aren't qualifier fields - everything else on the pivoted
    # row is treated as a qualifier, so new qualifier chips added later
    # (a frontend-only concept) show up here with zero backend changes.
    KNOWN_NON_QUALIFIER_KEYS = {
        "_time", "_start", "_stop", "_measurement", "result", "table",
        "sleep_date", "user", "entry_id", "score", "logged_at",
    }

    results = []
    for table in tables:
        for record in table.records:
            values = record.values
            qualifiers = {
                k: v for k, v in values.items()
                if k not in KNOWN_NON_QUALIFIER_KEYS and isinstance(v, bool)
            }
            results.append({
                "entry_id": values.get("entry_id"),
                "sleep_date": values.get("sleep_date"),
                "start_time": record.get_time().isoformat(),
                "score": values.get("score"),
                "logged_at": values.get("logged_at"),
                "qualifiers": qualifiers,
            })
    return results


def find_sleep_entry_by_id(user: str, entry_id: str) -> dict | None:
    ''' Look up a single sleep entry by its stable entry_id, for the
    edit/delete endpoints. entry_id is a tag, so this can be filtered
    directly (unlike event_id for manual/calendar events, which is a
    field and needs the pivot-then-filter workaround find_manual_event_by_id
    uses).
    '''
    client = get_client()
    query_api = client.query_api()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -1825d)
      |> filter(fn: (r) => r._measurement == "{SLEEP_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.entry_id == "{entry_id}")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.error(f"Failed to look up sleep entry {entry_id} for user={user}: {e}")
        return None

    KNOWN_NON_QUALIFIER_KEYS = {
        "_time", "_start", "_stop", "_measurement", "result", "table",
        "sleep_date", "user", "entry_id", "score", "logged_at",
    }

    for table in tables:
        for record in table.records:
            values = record.values
            qualifiers = {
                k: v for k, v in values.items()
                if k not in KNOWN_NON_QUALIFIER_KEYS and isinstance(v, bool)
            }
            return {
                "entry_id": values.get("entry_id"),
                "sleep_date": values.get("sleep_date"),
                "start_time": record.get_time(),
                "score": values.get("score"),
                "logged_at": values.get("logged_at"),
                "qualifiers": qualifiers,
            }
    return None


def delete_sleep_entry(user: str, entry_id: str):
    ''' entry_id and user are both TAGS on this measurement, so
    InfluxDB's delete API - which only matches on tags/measurement -
    can target this directly. entry_id alone is already globally
    unique (derived from user + session start time), so unlike
    sleep_date previously, no narrow timestamp window is needed here -
    a wide delete range is safe, since the entry_id tag match can only
    ever hit the one point it identifies. Key names still quoted
    defensively per the lesson from delete_event_tag_point (InfluxDB's
    delete predicate parser treats some bare words as reserved).
    '''
    client = get_client()
    delete_api = client.delete_api()
    predicate = f'_measurement="{SLEEP_MEASUREMENT}" AND "entry_id"="{entry_id}" AND "user"="{user}"'
    delete_api.delete("1970-01-01T00:00:00Z", "2100-01-01T00:00:00Z", predicate, bucket=INFLUX_BUCKET, org=INFLUX_ORG)


def find_last_completed_sleep_session(user: str, lookback_days: int = 7) -> dict | None:
    ''' Query the ring parser's sensor measurement for the most recent
    completed sleep session (has a wakeup time, i.e. duration_s field
    present) belonging to `user`, at least MIN_SLEEP_SESSION_SECONDS long.

    `user` must match the GADGETBRIDGE_USER value the ring parser tags
    that person's sensor data with - otherwise this will correctly find
    nothing, since the two are joined only by this shared tag value.

    Returns {"sleep_date": "YYYY-MM-DD", "start_time": datetime,
    "duration_s": int} or None if nothing qualifying was found - in
    which case the caller should reject the /sleep write per spec §6
    rather than guessing.
    '''
    client = get_client()
    query_api = client.query_api()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{lookback_days}d)
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.sample_type == "sleep_session")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "sleep_session_duration_s")
      |> filter(fn: (r) => r._value >= {MIN_SLEEP_SESSION_SECONDS})
      |> sort(columns: ["_time"], desc: true)
      |> limit(n: 1)
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.error(f"InfluxDB query failed while resolving last sleep session for user={user}: {e}")
        return None

    for table in tables:
        for record in table.records:
            start_time = record.get_time()
            duration_s = record.get_value()
            # Resolve the calendar date in the user's own local
            # timezone, not UTC's - a session starting shortly after
            # local midnight (any timezone ahead of UTC) can otherwise
            # resolve to the wrong day, since UTC's date for that same
            # instant is still the previous one.
            local_start_time = start_time.astimezone(ZoneInfo(TZ_NAME))
            return {
                "sleep_date": local_start_time.strftime("%Y-%m-%d"),
                # Deliberately the LOCAL-timezone version, not the raw
                # UTC one - astimezone() doesn't change which real
                # instant this represents, only which timezone's clock
                # face it displays, so this is safe for any absolute-
                # time math a future caller might do. Returning the
                # local version keeps this dict internally consistent
                # with sleep_date above - if this were left as raw UTC
                # instead, a future caller formatting it directly
                # (e.g. to show "sleep started at HH:MM") would get a
                # time that doesn't match the local date sitting right
                # next to it in this same dict. Nothing currently reads
                # this field, but that's exactly why the inconsistency
                # would be easy to introduce unnoticed later.
                "start_time": local_start_time,
                "duration_s": int(duration_s),
            }

    logger.debug(
        f"No qualifying completed sleep session found in the last {lookback_days}d "
        f"(measurement={SENSOR_MEASUREMENT}, user={user}, "
        f"min_duration_s={MIN_SLEEP_SESSION_SECONDS})"
    )
    return None


def find_manual_events_in_range(user: str, start: datetime, end: datetime) -> list[dict]:
    ''' Read-only query for manual tag taps (source="manual") in the
    events measurement, reconstructed into one entry per event_id.

    Each manual tap writes one Influx POINT per tag (all sharing the
    same event_id and timestamp), and each point contributes multiple
    raw rows in Flux's default output (one row per field). Without
    pivoting, `event_id` and `duration_min` would only be visible on
    the specific row for that field, not alongside the `tag` value on
    the same row - so this uses pivot(rowKey: ["_time", "tag"], ...) to
    recombine each point's fields onto one row first, then groups those
    rows by event_id in Python.
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{EVENTS_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.source == "manual")
      |> pivot(rowKey: ["_time", "tag"], columnKey: ["_field"], valueColumn: "_value")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.error(f"Failed to query manual events for user={user}: {e}")
        return []

    grouped: dict[str, dict] = {}
    for table in tables:
        for record in table.records:
            event_id = record.values.get("event_id")
            if not event_id:
                continue
            tag_value = record.values.get("tag")
            duration_min = record.values.get("duration_min")

            entry = grouped.setdefault(event_id, {
                "event_id": event_id,
                "timestamp": record.get_time(),
                "tags": set(),
                "duration_min": None,
            })
            if tag_value:
                entry["tags"].add(tag_value)
            if duration_min is not None:
                entry["duration_min"] = duration_min

    return [
        {
            "event_id": e["event_id"],
            "timestamp": e["timestamp"].isoformat(),
            "tags": sorted(e["tags"]),
            "duration_min": e["duration_min"],
        }
        for e in grouped.values()
    ]


def local_today_bounds() -> tuple[datetime, datetime]:
    ''' Start/end of "today" (midnight to midnight) in the configured
    local timezone (TZ_NAME) - not UTC's calendar day, for the same
    reason find_last_completed_sleep_session() resolves sleep_date
    locally: a person in a timezone ahead of UTC would otherwise see
    "today" flip over hours before their own local midnight.
    '''
    now_local = datetime.now(ZoneInfo(TZ_NAME))
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local, end_local


def _device_stat_by_field(field: str, user: str, start: datetime, end: datetime, stat: str) -> dict[str, float]:
    ''' One reducer (stat: "last"/"mean"/"min"/"max") for one field,
    grouped by device, within [start, end). Returns {device_name: value}.

    Deliberately one simple query per (field, stat) pair rather than a
    cleverer combined Flux query (e.g. multiple yield() calls in one
    script) - this app's existing InfluxDB functions are all written
    this way (see find_last_completed_sleep_session, list_distinct_
    sensor_users above), favoring obviously-correct simple queries over
    fewer-but-trickier round trips. Personal-use traffic volume makes
    that the right tradeoff here too.
    '''
    if stat not in ("last", "mean", "min", "max"):
        raise ValueError(f"unsupported stat: {stat!r}")

    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "{field}")
      |> group(columns: ["device"])
      |> {stat}()
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query {stat}({field}) for user={user}: {e}")
        return {}

    result = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            value = record.get_value()
            if device is not None and value is not None:
                result[device] = value
    return result


def get_today_vitals(user: str) -> dict[str, dict]:
    ''' Today's (local calendar day) vitals summary, per device, for
    every field both parsers share a common name for (see the shared-
    field-name design used throughout parser/activefit - this is
    exactly what makes a single query work unmodified across whichever
    devices happen to be reporting, ring or watch or both).

    Returns:
        {
          "heart_rate": {"<device>": {"last": .., "avg": .., "min": .., "max": ..}, ...},
          "hrv": {"<device>": {"last": ..}, ...},           # last only - a single
          "stress": {"<device>": {"last": .., "avg": ..}, ...},  # reading isn't
          "spo2": {"<device>": {"last": .., "min": .., "max": ..}, ...},  # usefully
          "temperature": {"<device>": {"last": ..}, ...},    # averaged/ranged
        }

    A device missing from a field's dict simply hasn't reported that
    field today - not an error, callers should treat absence as "no
    data yet" (e.g. before the first sync of the day) rather than a
    failure.
    '''
    start, end = local_today_bounds()

    # (field, which stats actually make sense for it)
    field_stats = {
        "heart_rate": ("last", "mean", "min", "max"),
        "hrv": ("last",),
        "stress": ("last", "mean"),
        "spo2": ("last", "min", "max"),
        "temperature": ("last",),
    }

    result: dict[str, dict] = {}
    for field, stats in field_stats.items():
        by_device: dict[str, dict] = {}
        for stat in stats:
            stat_key = "avg" if stat == "mean" else stat
            for device, value in _device_stat_by_field(field, user, start, end, stat).items():
                by_device.setdefault(device, {})[stat_key] = round(value, 1)
        result[field] = by_device

    return result


def get_today_series(field: str, user: str) -> dict[str, list[dict]]:
    ''' Raw (unaggregated) points for one field, today, per device - the
    time series a detail-view chart plots, as opposed to
    get_today_vitals()'s reduced last/avg/min/max summary.

    Returns {"<device>": [{"t": <ISO8601>, "v": <value>}, ...], ...},
    each device's list sorted chronologically.
    '''
    start, end = local_today_bounds()
    return _grouped_series(field, user, start, end)


def _grouped_series(field: str, user: str, start: datetime, end: datetime) -> dict[str, list[dict]]:
    ''' Shared query behind get_today_series() and the W/M/Y range
    endpoints - one field, grouped by device, sorted chronologically,
    over an arbitrary [start, end) window.

    The explicit sort() after group() is required, not optional or
    redundant - confirmed directly from InfluxDB's own docs ("Group
    does not guarantee sort order. To ensure data is sorted correctly,
    use sort() after group()."), not just inferred from the symptom.
    Without it, points from what were originally several disjoint
    underlying series (this data also carries activity_kind, sample_type,
    etc. as tags - see parser/activefit - each combination is its own
    series until an explicit group() call collapses them by device
    alone) get merged in whatever order the query engine happened to
    produce internally, not necessarily chronological - a line chart
    connecting points in that non-chronological array order visually
    looks like the reported "skip lines and a bunch of separate
    points", since the line jumps backward and forward in time rather
    than progressing smoothly left to right. An earlier version of
    this function incorrectly assumed group() preserved time order
    (see git history) - that assumption was never actually verified
    and was wrong.
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "{field}")
      |> group(columns: ["device"])
      |> sort(columns: ["_time"])
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query {field} series for user={user}: {e}")
        return {}

    result: dict[str, list[dict]] = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            value = record.get_value()
            if device is None or value is None:
                continue
            result.setdefault(device, []).append({
                "t": record.get_time().isoformat(),
                "v": value,
            })
    return result


def get_period_range_series(field: str, user: str, start: datetime, end: datetime, window: str) -> dict[str, list[dict]]:
    ''' Per-device min/max for one field, bucketed into `window`-sized
    periods (a Flux duration string, e.g. "1d" or "1mo") across
    [start, end) - what the W/M/Y "range bar" charts plot (one bar per
    period spanning that period's low-to-high), as opposed to
    get_today_series()'s raw per-point series used for the D view.

    Returns {"<device>": [{"t": <period start ISO8601>, "min": v, "max": v}, ...]}.

    Two separate aggregateWindow() queries (min, then max), zipped
    together by (device, period start) - matches this file's established
    style of simple single-purpose queries over one cleverer combined
    query (see _device_stat_by_field's own docstring for the same
    reasoning).
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    by_device_and_time: dict[str, dict[str, dict]] = {}

    for stat in ("min", "max"):
        flux = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {start_iso}, stop: {stop_iso})
          |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
          |> filter(fn: (r) => r.user == "{user}")
          |> filter(fn: (r) => r._field == "{field}")
          |> group(columns: ["device"])
          |> aggregateWindow(every: {window}, fn: {stat}, createEmpty: false)
        '''
        try:
            tables = query_api.query(flux)
        except Exception as e:
            logger.warning(f"Failed to query {stat}({field}) range series for user={user}: {e}")
            continue

        for table in tables:
            for record in table.records:
                device = record.values.get("device")
                value = record.get_value()
                period_start = record.get_time()
                if device is None or value is None or period_start is None:
                    continue
                period_key = period_start.isoformat()
                by_device_and_time.setdefault(device, {}).setdefault(period_key, {"t": period_key})[stat] = value

    result: dict[str, list[dict]] = {}
    for device, periods in by_device_and_time.items():
        # Only keep periods where both min and max actually came back -
        # a period missing one (shouldn't normally happen, since both
        # queries share the same filter/window) is incomplete data,
        # not a real zero-width range.
        complete = [p for p in periods.values() if "min" in p and "max" in p]
        result[device] = sorted(complete, key=lambda p: p["t"])
    return result


def _daily_values(field: str, user: str, start: datetime, end: datetime) -> dict[str, list[float]]:
    ''' Per-device list of one mean value per day, over [start, end) -
    the intermediate this file's own get_baseline_comparison() needs
    to compute a mean/stddev ACROSS days (not across raw readings
    within a single window, which _device_stat_by_field() already
    does but isn't the same statistic). Timestamps are dropped - only
    the values themselves matter for the mean/stddev calculation.
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "{field}")
      |> group(columns: ["device"])
      |> aggregateWindow(every: 1d, fn: mean, createEmpty: false)
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query daily {field} values for user={user}: {e}")
        return {}

    result: dict[str, list[float]] = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            value = record.get_value()
            if device is None or value is None:
                continue
            result.setdefault(device, []).append(value)
    return result


def get_baseline_comparison(field: str, user: str, baseline_days: int = 7) -> dict[str, dict]:
    ''' For one field, per device: today's value compared against a
    trailing baseline - the mean and (sample) standard deviation of
    daily values over the `baseline_days` days immediately BEFORE
    today (today itself excluded, so a value is never compared against
    a baseline that includes itself). Powers the Slower/Faster
    z-scored comparison bar - the same "vs. your own baseline" gauge
    concept Zepp's own Resting Heart Rate/HRV pages show (see
    wearable-events/UI_DESIGN_NOTES.md), just computed here instead of
    left blank the way Zepp's own version was for lack of history.

    Returns {"<device>": {"today": v, "baseline_mean": m,
    "baseline_stddev": s, "z": (today-mean)/stddev, "delta": today-mean}}.

    A device is omitted entirely if there isn't enough data to compute
    something meaningful - fewer than 2 baseline days (a stddev needs
    at least 2 points) or no reading at all today. This is a real
    "insufficient data" case, not an error - same situation Zepp's own
    gauge shows early on, and the caller should treat it the same way
    (an empty/insufficient-data state, not a failure).
    '''
    today_start, today_end = local_today_bounds()
    baseline_start = today_start - timedelta(days=baseline_days)

    daily = _daily_values(field, user, baseline_start, today_start)
    today_value = _device_stat_by_field(field, user, today_start, today_end, "last")

    result: dict[str, dict] = {}
    for device, values in daily.items():
        if len(values) < 2 or device not in today_value:
            continue
        mean = statistics.mean(values)
        stddev = statistics.stdev(values)
        today_v = today_value[device]
        delta = today_v - mean
        if stddev > 0:
            z = delta / stddev
        elif delta != 0:
            # Zero historical variance (every baseline day identical)
            # but today differs anyway - any deviation from a
            # perfectly flat baseline is maximally noteworthy, not
            # "no different". Defaulting to z=0 here would put the
            # marker dead-center while the delta text correctly shows
            # a non-zero bpm difference - a direct visual/textual
            # contradiction. Pin far beyond any real cap (the frontend
            # clamps display to +-2) so the marker lands at the
            # correct edge instead.
            z = 10.0 if delta > 0 else -10.0
        else:
            z = 0.0
        result[device] = {
            "today": round(today_v, 1),
            "baseline_mean": round(mean, 1),
            "baseline_stddev": round(stddev, 2),
            "z": round(z, 2),
            "delta": round(delta, 1),
        }
    return result


def get_today_steps(user: str) -> dict[str, int]:
    ''' Today's (local calendar day) total steps per device - a sum,
    not last/mean/etc., since steps is a per-sample count that needs
    adding up across the day rather than reduced to one representative
    reading.
    '''
    start, end = local_today_bounds()
    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "steps")
      |> group(columns: ["device"])
      |> sum()
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query today's steps for user={user}: {e}")
        return {}

    result = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            value = record.get_value()
            if device is not None and value is not None:
                result[device] = int(value)
    return result


def get_sleep_stage_breakdown(user: str, session_start: datetime, session_end: datetime) -> dict[str, int]:
    ''' Minutes spent in each sleep stage (light/deep/rem/awake) for one
    specific sleep session, identified by its own [start, end) window -
    NOT a lookback query, the caller (typically find_last_completed_
    sleep_session's result) already knows exactly which session.

    Sums sleep_stage_duration_s (already extracted per stage segment,
    see parser/activefit and parser/colmi's sleep stage extraction)
    grouped by the sleep_stage tag, converted to whole minutes.
    Deliberately does NOT filter by device - a single sleep session
    belongs to one device by construction (whichever one was worn that
    night), so grouping by sleep_stage alone is sufficient and avoids
    an empty result if the device tag's exact string ever shifts
    between sync and query (e.g. a device rename in Gadgetbridge).
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = session_start.astimezone(timezone.utc).isoformat()
    stop_iso = session_end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.sample_type == "sleep_stage")
      |> filter(fn: (r) => r._field == "sleep_stage_duration_s")
      |> group(columns: ["sleep_stage"])
      |> sum()
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query sleep stage breakdown for user={user}: {e}")
        return {}

    result = {}
    for table in tables:
        for record in table.records:
            stage = record.values.get("sleep_stage")
            value = record.get_value()
            if stage is not None and value is not None:
                result[stage] = round(value / 60)
    return result


def list_distinct_sensor_users(lookback_days: int = 365) -> list[str]:
    ''' Returns the distinct `user` tag values seen in the ring parser's
    sensor measurement over the lookback window. Used to power the
    registration "claim existing ring data" picker - a long default
    lookback (1 year) so someone whose ring hasn't synced in a while
    doesn't silently disappear from the list, at the cost of possibly
    surfacing a genuinely stale/abandoned identifier. Returns an empty
    list (not an error) if the bucket/measurement has no data yet, or if
    the query fails - callers should treat both the same way: fall back
    to manual entry.
    '''
    client = get_client()
    query_api = client.query_api()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: -{lookback_days}d)
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> keep(columns: ["user"])
      |> distinct(column: "user")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query distinct sensor users (bucket empty/unreachable?): {e}")
        return []

    users = []
    for table in tables:
        for record in table.records:
            value = record.get_value()
            if value:
                users.append(value)
    return sorted(set(users))