import hashlib
import statistics
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
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
    MAX_SLEEP_SESSION_SECONDS,
    MIN_SLEEP_SESSION_SECONDS,
    SENSOR_MEASUREMENT,
    SESSION_GAP_MINUTES,
    SESSION_MIN_DURATION_MINUTES,
    SLEEP_MEASUREMENT,
    STAND_INTENSITY_THRESHOLD,
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


def find_last_completed_sleep_session(user: str, lookback_days: int = 7, before: datetime | None = None) -> dict | None:
    ''' Query the ring parser's sensor measurement for the most recent
    completed sleep session (has a wakeup time, i.e. duration_s field
    present) belonging to `user`, at least MIN_SLEEP_SESSION_SECONDS long.

    `user` must match the GADGETBRIDGE_USER value the ring parser tags
    that person's sensor data with - otherwise this will correctly find
    nothing, since the two are joined only by this shared tag value.

    `before`, if given, shifts the whole lookback window to end there
    instead of now - what the Today tab's date navigation uses to show
    "the sleep session that had most recently completed as of that day"
    rather than always today's actual most-recent session, regardless
    of which day is being viewed.

    Returns {"sleep_date": "YYYY-MM-DD", "start_time": datetime,
    "duration_s": int} or None if nothing qualifying was found - in
    which case the caller should reject the /sleep write per spec §6
    rather than guessing.
    '''
    client = get_client()
    query_api = client.query_api()

    range_args = f"start: -{lookback_days}d" if before is None else (
        f'start: {(before - timedelta(days=lookback_days)).astimezone(timezone.utc).isoformat()}, '
        f'stop: {before.astimezone(timezone.utc).isoformat()}'
    )

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range({range_args})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.sample_type == "sleep_session")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "sleep_session_duration_s")
      |> filter(fn: (r) => r._value >= {MIN_SLEEP_SESSION_SECONDS})
      |> filter(fn: (r) => r._value <= {MAX_SLEEP_SESSION_SECONDS})
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
            device = record.values.get("device")
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
                # Which device this session's sleep_session_duration_s
                # point came from - needed by get_sleep_stage_breakdown
                # to scope its own query to the SAME device, now that
                # more than one device can have sleep-stage data for
                # overlapping nights (the ring's historical data
                # persists in InfluxDB even after being unbound from
                # Gadgetbridge - see parser/activefit/FIELD_RESEARCH.md).
                # Real bug found and fixed here (2026-09): without this,
                # get_sleep_stage_breakdown had no way to avoid summing
                # BOTH devices' stage minutes together for the same
                # night, confirmed against real reported values showing
                # stage sums roughly 2-3x a plausible night's duration.
                "device": device,
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


def local_today_bounds(for_date: date | None = None) -> tuple[datetime, datetime]:
    ''' Start/end (midnight to midnight) of one local calendar day in
    the configured local timezone (TZ_NAME) - not UTC's calendar day,
    for the same reason find_last_completed_sleep_session() resolves
    sleep_date locally: a person in a timezone ahead of UTC would
    otherwise see "today" flip over hours before their own local
    midnight.

    `for_date` defaults to today when omitted (the original single
    purpose this function was written for), but can be any date - this
    is what the detail-view navigation (previous/next day, jump to a
    date) uses to compute bounds for a day other than today, without
    every existing caller needing to change.
    '''
    tz = ZoneInfo(TZ_NAME)
    if for_date is None:
        for_date = datetime.now(tz).date()
    start_local = datetime.combine(for_date, datetime.min.time(), tzinfo=tz)
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


def get_today_vitals(user: str, for_date: date | None = None) -> dict[str, dict]:
    ''' Today's (local calendar day) vitals summary, per device, for
    every field both parsers share a common name for (see the shared-
    field-name design used throughout parser/activefit - this is
    exactly what makes a single query work unmodified across whichever
    devices happen to be reporting, ring or watch or both).

    `for_date` defaults to today when omitted - what the Today tab's
    date navigation uses to view a past day's summary instead.

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
    start, end = local_today_bounds(for_date)

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


def get_today_series(field: str, user: str, for_date: date | None = None) -> dict[str, list[dict]]:
    ''' Raw (unaggregated) points for one field, for one day (today by
    default, or `for_date` for the detail-view's day-navigation), per
    device - the time series a detail-view chart plots, as opposed to
    get_today_vitals()'s reduced last/avg/min/max summary.

    Returns {"<device>": [{"t": <ISO8601>, "v": <value>}, ...], ...},
    each device's list sorted chronologically.
    '''
    start, end = local_today_bounds(for_date)
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


def get_manual_readings(field: str, user: str, start: datetime, end: datetime) -> dict[str, list[dict]]:
    ''' Per-device manually-triggered readings only (as opposed to the
    device's own automatic periodic sampling) for one field, over an
    arbitrary [start, end) window - what Zepp's own Stress page calls
    its "Manual Data" list, and (person-confirmed, not assumed) also
    applicable to SpO2 given the identical TYPE_NUM convention on both
    fields.

    Relies on `{field}_type_num` being a TAG (not a field) with value
    "0" meaning manual - confirmed for both stress and spo2 via a
    deliberate cross-check (see parser/activefit/FIELD_RESEARCH.md),
    not inferred from Gadgetbridge's feature-list wording alone. Only
    ever call this for a field actually confirmed to have this tag -
    MANUAL_TYPE_NUM_FIELDS in main.py is the enforced allowlist: an
    unsupported field would just silently return {} here (the tag
    filter simply never matches anything), which is a much less
    obvious failure than the 400 the allowlist gives instead.

    Returns {"<device>": [{"t": <ISO8601>, "v": <value>}, ...], ...},
    each device's list sorted chronologically - same shape as
    get_today_series(), just pre-filtered to manual readings only.
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
      |> filter(fn: (r) => r.{field}_type_num == "0")
      |> group(columns: ["device"])
      |> sort(columns: ["_time"])
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query manual {field} readings for user={user}: {e}")
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


def _get_pivoted_activity_minutes(user: str, start: datetime, end: datetime) -> dict[str, list[dict]]:
    ''' Raw per-minute activity samples (steps, raw_intensity, heart_rate)
    pivoted together by timestamp - the shape get_activity_sessions()
    needs to walk chronologically per device and detect session
    boundaries, rather than three separate single-field series that
    would need re-joining by hand.

    Grouped by device (unlike get_today_series()'s per-field grouping,
    this groups the WHOLE pivoted row) - session detection needs to
    process one device's minutes in isolation, since interleaving two
    devices' timestamps would garble gap-tolerance logic and produce
    sessions that jump between devices mid-stream. raw_intensity is
    currently only ever written by the watch parser (the ring's own
    activity table has no such column), but steps/heart_rate could in
    principle come from more than one device, so this doesn't assume
    single-device data even though that's the practical reality right
    now.

    Returns {"<device>": [{"t": <datetime>, "steps": .., "raw_intensity": ..,
    "heart_rate": .., "activity_kind": .., "activity_kind_label": ..}, ...]},
    each device's list sorted chronologically. Values are None for
    whichever fields didn't happen to report at that exact timestamp.
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
      |> filter(fn: (r) => r.sample_type == "activity")
      |> filter(fn: (r) => r._field == "steps" or r._field == "raw_intensity" or r._field == "heart_rate")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> sort(columns: ["_time"])
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query activity minutes for user={user}: {e}")
        return {}

    result: dict[str, list[dict]] = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            if device is None:
                continue
            result.setdefault(device, []).append({
                "t": record.get_time(),
                "steps": record.values.get("steps"),
                "raw_intensity": record.values.get("raw_intensity"),
                "heart_rate": record.values.get("heart_rate"),
                "activity_kind": record.values.get("activity_kind"),
                "activity_kind_label": record.values.get("activity_kind_label"),
            })

    # Belt-and-suspenders, same reasoning as _grouped_series's own
    # explicit sort: pivot()/group() don't guarantee row order, and an
    # earlier bug in this exact codebase (see _grouped_series's own
    # docstring) came from trusting that they did.
    for device in result:
        result[device].sort(key=lambda r: r["t"])
    return result


def _finalize_session(session: dict) -> dict | None:
    ''' Turns an in-progress session accumulator into the public
    session dict, or None if it doesn't meet SESSION_MIN_DURATION_MINUTES -
    shared by get_activity_sessions() wherever a session needs closing
    out, so the "does this qualify" and "what does the output look
    like" logic lives in exactly one place.
    '''
    # +1 sample interval (1 minute - the established per-minute
    # sampling cadence throughout this project), not just the raw
    # delta between the first and last sample's own timestamps. Each
    # sample represents a full minute of coverage, so a session made
    # of samples at minutes 0, 1, 2 genuinely spans 3 minutes of real
    # activity, not the 2-minute gap between its first and last
    # timestamp - using the raw delta alone would undercount every
    # session's true duration by exactly one sample interval, which
    # for short sessions is the difference between correctly passing
    # and incorrectly failing the minimum-duration filter below.
    duration_min = (session["end"] - session["start"]).total_seconds() / 60 + 1
    if duration_min < SESSION_MIN_DURATION_MINUTES:
        return None
    kinds = session["kinds"]
    kind, label = Counter(kinds).most_common(1)[0][0] if kinds else (None, "unknown")
    hr_values = [h for h in session["hr_values"] if h is not None]
    return {
        "start": session["start"].isoformat(),
        "end": session["end"].isoformat(),
        "device": session["device"],
        "activity_kind": kind,
        "activity_kind_label": label if label else "unknown",
        "avg_heart_rate": round(statistics.mean(hr_values), 1) if hr_values else None,
        "source": "derived",
    }


def get_activity_sessions(user: str, for_date: date | None = None) -> list[dict]:
    ''' Derived activity sessions for one day - groups consecutive
    "active" minutes (steps > 0 OR raw_intensity >= STAND_INTENSITY_THRESHOLD,
    the same threshold confirmed against the watch's own real hourly
    Stand display) into discrete sessions, each summarized with a
    start/end time, the session's most common activity_kind, and
    average heart rate over its duration.

    This is OUR OWN session-merging heuristic (SESSION_GAP_MINUTES
    tolerance between active minutes before a session is considered
    ended, SESSION_MIN_DURATION_MINUTES floor to filter out single-
    minute noise - both in config.py), not a replication of
    Gadgetbridge's own StepAnalysis algorithm - its exact source
    wasn't available to pull directly, only its wiki's informal
    description. Worth revisiting these two numbers once there's
    enough real data to compare our own session list against
    Gadgetbridge's or the watch's own.

    Processes each device's minutes independently (see
    _get_pivoted_activity_minutes) so a session never silently jumps
    between devices, then merges every device's sessions back into one
    chronologically sorted list - this is meant to be shown as a
    single combined "what happened today" list, not broken out per
    device the way most other views in this app are.

    Returns a list of dicts, sorted by start time:
    {"start": <ISO8601>, "end": <ISO8601>, "device": <str>,
     "activity_kind": <int|None>, "activity_kind_label": <str>,
     "avg_heart_rate": <float|None>, "source": "derived"}
    '''
    start, end = local_today_bounds(for_date)
    minutes_by_device = _get_pivoted_activity_minutes(user, start, end)

    all_sessions: list[dict] = []
    for device, minutes in minutes_by_device.items():
        current: dict | None = None
        for row in minutes:
            steps = row["steps"] or 0
            intensity = row["raw_intensity"] or 0
            is_active = steps > 0 or intensity >= STAND_INTENSITY_THRESHOLD
            if not is_active:
                continue

            if current is not None:
                # Gap since the last ACTIVE minute, not since session
                # start - a session should survive a brief lull, but a
                # long-enough gap starts a genuinely new session rather
                # than stretching this one across it.
                gap_min = (row["t"] - current["end"]).total_seconds() / 60
                if gap_min > SESSION_GAP_MINUTES:
                    finalized = _finalize_session(current)
                    if finalized:
                        all_sessions.append(finalized)
                    current = None

            if current is None:
                current = {"start": row["t"], "end": row["t"], "device": device, "kinds": [], "hr_values": []}
            else:
                current["end"] = row["t"]
            current["kinds"].append((row["activity_kind"], row["activity_kind_label"]))
            current["hr_values"].append(row["heart_rate"])

        if current is not None:
            finalized = _finalize_session(current)
            if finalized:
                all_sessions.append(finalized)

    all_sessions.sort(key=lambda s: s["start"])
    return all_sessions


def get_precomputed_activity_sessions(user: str, for_date: date | None = None) -> list[dict]:
    ''' Pre-computed activity summaries (BASE_ACTIVITY_SUMMARY, written
    by the parser as sample_type="activity_summary") for one day - the
    "deliberately started workout" half of the Activity page's combined
    session list, as opposed to get_activity_sessions()'s derived-from-
    raw-data half. Genuinely sparse in practice - this table is
    populated only for explicitly-started workouts, not ambient daily
    movement (see parser/activefit/FIELD_RESEARCH.md), so most days
    will return an empty list here.

    Average heart rate for each entry is computed by querying the
    already-extracted per-minute heart_rate field over that entry's own
    [start, end) window (reusing _device_stat_by_field, the same
    function every other heart-rate view in this app already uses) -
    not duplicated at parse time, since the parser deliberately only
    extracts BASE_ACTIVITY_SUMMARY's simple NAME/START_TIME/END_TIME/
    ACTIVITY_KIND columns.

    Returns a list of dicts, sorted chronologically:
    {"start": <ISO8601>, "end": <ISO8601>, "device": <str>,
     "name": <str|None>, "activity_kind_summary": <int|str|None>,
     "avg_heart_rate": <float|None>, "source": "precomputed"}
    '''
    start, end = local_today_bounds(for_date)
    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.sample_type == "activity_summary")
      |> filter(fn: (r) => r._field == "duration_s")
      |> sort(columns: ["_time"])
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query precomputed activity summaries for user={user}: {e}")
        return []

    sessions = []
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            duration_s = record.get_value()
            if device is None or duration_s is None:
                continue
            entry_start = record.get_time()
            entry_end = entry_start + timedelta(seconds=duration_s)
            # Only this entry's own device's heart rate over its own
            # window - _device_stat_by_field returns every device that
            # reported in the range, but averaging in a DIFFERENT
            # device's heart rate onto this entry would be wrong even
            # if it happened to also report something during the same
            # window.
            hr_by_device = _device_stat_by_field("heart_rate", user, entry_start, entry_end, "mean")
            avg_hr = hr_by_device.get(device)
            sessions.append({
                "start": entry_start.isoformat(),
                "end": entry_end.isoformat(),
                "device": device,
                "name": record.values.get("name"),
                "activity_kind_summary": record.values.get("activity_kind_summary"),
                "avg_heart_rate": round(avg_hr, 1) if avg_hr is not None else None,
                "source": "precomputed",
            })

    sessions.sort(key=lambda s: s["start"])
    return sessions


def get_combined_activity_sessions(user: str, for_date: date | None = None) -> list[dict]:
    ''' Both halves of the Activity page's session list merged into one
    chronologically sorted, display-ready list - derived sessions
    (get_activity_sessions) and pre-computed workout entries
    (get_precomputed_activity_sessions), unified into a common shape so
    the frontend doesn't need to know which source each entry came
    from to render it.

    No deduplication/overlap-suppression between the two sources - if
    a real workout ever gets both a derived-session entry and a
    precomputed entry for the same time range, both would appear here.
    Not handled, given BASE_ACTIVITY_SUMMARY currently has only 1 real
    row total (making overlaps essentially nonexistent in practice
    right now) - worth revisiting once that becomes a real, observable
    problem rather than a hypothetical one.

    `label` follows the person's own instruction: the activity's name
    if known, "unknown" otherwise, with `raw_code` always present
    separately so the frontend can still show the raw code alongside
    "unknown" ("we can slowly figure out what the codes mean"). For
    precomputed entries specifically, NAME's own "Unset" sentinel (see
    the parser's extract_base_activity_summary_rows) is treated the
    same as no name at all, not displayed literally.

    Returns a list of dicts, sorted chronologically:
    {"start": <ISO8601>, "end": <ISO8601>, "device": <str>,
     "label": <str>, "raw_code": <int|str|None>,
     "avg_heart_rate": <float|None>, "source": "derived"|"precomputed"}
    '''
    derived = get_activity_sessions(user, for_date)
    precomputed = get_precomputed_activity_sessions(user, for_date)

    combined = []
    for s in derived:
        combined.append({
            "start": s["start"],
            "end": s["end"],
            "device": s["device"],
            "label": s["activity_kind_label"],
            "raw_code": s["activity_kind"],
            "avg_heart_rate": s["avg_heart_rate"],
            "source": "derived",
        })
    for s in precomputed:
        name = s["name"]
        has_real_name = name is not None and name != "Unset"
        combined.append({
            "start": s["start"],
            "end": s["end"],
            "device": s["device"],
            "label": name if has_real_name else "unknown",
            "raw_code": s["activity_kind_summary"],
            "avg_heart_rate": s["avg_heart_rate"],
            "source": "precomputed",
        })

    combined.sort(key=lambda s: s["start"])
    return combined


# Excluded from "sitting" time even though their intensity is
# typically low too (see the real per-activity-kind distribution in
# FIELD_RESEARCH.md - sleep's median intensity was 0, charging's was
# also near-zero) - counting them would silently fold hours of sleep
# or a charging watch into a "sitting time" figure, which isn't what
# this feature is for. not_worn is excluded for the same reason: time
# the watch wasn't being worn isn't time the person was sitting,
# whatever the intensity reading happens to be during it.
SITTING_EXCLUDED_LABELS = {"sleep", "not_worn", "charging"}


def get_sitting_minutes(user: str, for_date: date | None = None) -> dict[str, float]:
    ''' Cumulative minutes of "sitting" for one day, per device -
    minutes where raw_intensity is below STAND_INTENSITY_THRESHOLD,
    excluding sleep/not_worn/charging (see SITTING_EXCLUDED_LABELS)
    since those have low intensity too but aren't meaningfully
    "sitting".

    Reuses the exact same per-minute data get_activity_sessions()
    already fetches (via _get_pivoted_activity_minutes) rather than a
    second query for the same underlying samples.

    Counts qualifying samples directly and treats each as one minute
    (the established per-minute sampling cadence throughout this
    project) - doesn't try to account for gaps in the underlying data,
    and doesn't need to special-case "today isn't over yet": a future
    hour simply has no samples at all yet, so it's naturally excluded
    from the count rather than needing explicit handling.

    Returns {"<device>": <minutes>, ...} - a device with zero
    qualifying minutes is simply absent, not present with a 0, matching
    how the rest of this app treats "no data" (see get_today_vitals).
    '''
    start, end = local_today_bounds(for_date)
    minutes_by_device = _get_pivoted_activity_minutes(user, start, end)

    result: dict[str, float] = {}
    for device, minutes in minutes_by_device.items():
        count = 0
        for row in minutes:
            if row["activity_kind_label"] in SITTING_EXCLUDED_LABELS:
                continue
            intensity = row["raw_intensity"] or 0
            if intensity < STAND_INTENSITY_THRESHOLD:
                count += 1
        if count > 0:
            result[device] = count
    return result


def get_stood_hours(user: str, for_date: date | None = None) -> dict[str, int]:
    ''' Count of hours today where raw_intensity crossed
    STAND_INTENSITY_THRESHOLD at some point during that hour, per
    device - confirmed empirically against the watch's own real hourly
    Stand display (see STAND_INTENSITY_THRESHOLD's own docstring in
    config.py and parser/activefit/FIELD_RESEARCH.md).

    Reuses the exact same per-minute data get_activity_sessions()
    already fetches. Buckets by LOCAL hour, not UTC - InfluxDB always
    returns UTC-aware timestamps, and the watch's own Stand display is
    almost certainly hour-of-day in the wearer's own local time, not
    UTC; bucketing on the raw UTC timestamp directly would silently
    misalign against what the watch shows for any timezone offset that
    isn't a whole number of... well, any offset at all, really, since
    UTC hour boundaries and local hour boundaries only coincide for
    UTC+0 itself. Same category of bug this project has hit more than
    once before (see local_today_bounds, find_last_completed_sleep_session).

    Returns {"<device>": <hour_count>, ...} - a device with zero
    qualifying hours is absent, not present with a 0, matching
    get_sitting_minutes and get_today_vitals.
    '''
    start, end = local_today_bounds(for_date)
    minutes_by_device = _get_pivoted_activity_minutes(user, start, end)
    tz = ZoneInfo(TZ_NAME)

    result: dict[str, int] = {}
    for device, minutes in minutes_by_device.items():
        stood_hours = set()
        for row in minutes:
            intensity = row["raw_intensity"] or 0
            if intensity >= STAND_INTENSITY_THRESHOLD:
                local_t = row["t"].astimezone(tz)
                stood_hours.add(local_t.replace(minute=0, second=0, microsecond=0))
        if stood_hours:
            result[device] = len(stood_hours)
    return result


def get_hourly_activity_breakdown(user: str, for_date: date | None = None) -> dict[str, list[dict]]:
    ''' Per-hour breakdown of sitting vs. active minutes for one day,
    per device - the Activity page's day-view "sitting vs standing"
    chart. Each hour reports how many of its minutes were sitting (low
    intensity, not sleep/not_worn/charging - same SITTING_EXCLUDED_LABELS
    criteria as get_sitting_minutes), how many were active (crossed
    STAND_INTENSITY_THRESHOLD - the same threshold get_stood_hours uses
    to credit a whole hour), and how many were excluded (sleep/
    not_worn/charging) - the three categories sum to that hour's total
    sampled minutes, which is what makes this suited to a stacked bar
    rather than needing a separate "total" figure.

    A minute with an excluded label is excluded outright, even if its
    intensity happens to be high (e.g. briefly rolling over in bed) -
    it counts toward neither sitting nor active, matching
    get_sitting_minutes's own reasoning exactly (sleep/not_worn/
    charging aren't meaningfully "sitting" OR "standing").

    Bucketed by LOCAL hour, same reasoning as get_stood_hours - watch
    displays are local-time, InfluxDB timestamps are always UTC.

    Returns {"<device>": [{"t": <local hour start, ISO8601 with its
    own local offset - ready to display directly, no further timezone
    math needed by the caller>, "sitting_minutes": N,
    "active_minutes": M, "excluded_minutes": K}, ...]}, one entry per
    hour that has ANY data (an hour with zero samples - e.g. before
    the first sync of the day - is omitted entirely, not shown as an
    all-zero row), sorted chronologically.
    '''
    start, end = local_today_bounds(for_date)
    minutes_by_device = _get_pivoted_activity_minutes(user, start, end)
    tz = ZoneInfo(TZ_NAME)

    result: dict[str, list[dict]] = {}
    for device, minutes in minutes_by_device.items():
        buckets: dict[datetime, dict] = {}
        for row in minutes:
            local_t = row["t"].astimezone(tz)
            hour_start = local_t.replace(minute=0, second=0, microsecond=0)
            bucket = buckets.setdefault(hour_start, {"sitting_minutes": 0, "active_minutes": 0, "excluded_minutes": 0})
            if row["activity_kind_label"] in SITTING_EXCLUDED_LABELS:
                bucket["excluded_minutes"] += 1
                continue
            intensity = row["raw_intensity"] or 0
            if intensity >= STAND_INTENSITY_THRESHOLD:
                bucket["active_minutes"] += 1
            else:
                bucket["sitting_minutes"] += 1
        if buckets:
            result[device] = [
                {"t": hour.isoformat(), **counts}
                for hour, counts in sorted(buckets.items())
            ]
    return result


def get_activity_time_range_series(user: str, start: datetime, end: datetime, window: str) -> dict[str, list[dict]]:
    ''' Per-bucket (day- or month-sized, matching `window`) sitting/
    active minute sums across an arbitrary range - the Week/Month/Year
    "total activity time" chart (active_minutes alone) and "sitting vs
    standing" stacked bar chart (both fields together) share this same
    underlying data rather than each needing their own query.

    Same sitting/active criteria as get_hourly_activity_breakdown
    (SITTING_EXCLUDED_LABELS, STAND_INTENSITY_THRESHOLD) - a minute
    with an excluded label counts toward neither, and unlike
    get_hourly_activity_breakdown, excluded minutes aren't tracked as
    their own category here (this function only reports the two
    fields the charts above actually need) - which also means a
    bucket where EVERY minute was excluded (e.g. a day the watch
    wasn't worn at all) is correctly skipped entirely, not shown as a
    misleading all-zero bar.

    `window` is "1d" for the Week/Month views or "1mo" for the Year
    view - matching get_period_range_series's own convention, so this
    can be wired into the same call sites the same way. Bucketed in
    LOCAL time throughout (each raw minute converted to local time
    before being assigned to a bucket - same reasoning as
    get_stood_hours/get_hourly_activity_breakdown), and "1mo" buckets
    align to calendar months (the 1st of each month), matching how the
    Year view's other range charts already align.

    Returns {"<device>": [{"t": <bucket start, local-time ISO8601>,
    "sitting_minutes": N, "active_minutes": M}, ...]}, sorted
    chronologically. A bucket with zero qualifying minutes is omitted,
    not shown as an all-zero row.
    '''
    if window not in ("1d", "1mo"):
        raise ValueError(f"unsupported window: {window!r} (must be '1d' or '1mo')")

    minutes_by_device = _get_pivoted_activity_minutes(user, start, end)
    tz = ZoneInfo(TZ_NAME)

    def bucket_key(local_t: datetime) -> datetime:
        if window == "1d":
            return local_t.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    result: dict[str, list[dict]] = {}
    for device, minutes in minutes_by_device.items():
        buckets: dict[datetime, dict] = {}
        for row in minutes:
            if row["activity_kind_label"] in SITTING_EXCLUDED_LABELS:
                continue
            local_t = row["t"].astimezone(tz)
            key = bucket_key(local_t)
            bucket = buckets.setdefault(key, {"sitting_minutes": 0, "active_minutes": 0})
            intensity = row["raw_intensity"] or 0
            if intensity >= STAND_INTENSITY_THRESHOLD:
                bucket["active_minutes"] += 1
            else:
                bucket["sitting_minutes"] += 1
        if buckets:
            result[device] = [
                {"t": key.isoformat(), **counts}
                for key, counts in sorted(buckets.items())
            ]
    return result


def get_period_range_series(field: str, user: str, start: datetime, end: datetime, window: str) -> dict[str, list[dict]]:
    ''' Per-device min/max/median for one field, bucketed into
    `window`-sized periods (a Flux duration string, e.g. "1d" or "1mo")
    across [start, end) - what the W/M/Y "range bar" charts plot (one
    bar per period spanning that period's low-to-high, with the median
    marked inside it), as opposed to get_today_series()'s raw
    per-point series used for the D view.

    Returns {"<device>": [{"t": <period start ISO8601>, "min": v,
    "max": v, "median": v}, ...]}.

    Three separate aggregateWindow() queries (min, max, median), zipped
    together by (device, period start) - matches this file's established
    style of simple single-purpose queries over one cleverer combined
    query (see _device_stat_by_field's own docstring for the same
    reasoning). median needs different Flux syntax from the other two:
    plain `fn: median` doesn't work in aggregateWindow() (median()
    lacks the `column` parameter aggregateWindow tries to pass to it -
    confirmed via InfluxDB's own docs, not assumed), so it needs the
    full anonymous-function form instead. Uses median()'s
    "exact_selector" method specifically, which returns an actual
    observed reading rather than an interpolated/averaged value -
    right for showing "an actual recorded reading from that day", not
    a synthetic number nobody's device ever produced.

    All three queries explicitly set timeSrc: "_start" - confirmed via
    InfluxDB's own docs that aggregateWindow() otherwise defaults to
    _stop (the END of each window) as the timestamp it assigns to the
    aggregated value. Without this, every bucket's data is labeled
    with the FOLLOWING period's boundary - a full month off for the
    Year view's monthly buckets (exactly the reported "August's data
    shows up under September" bug), a subtler one-day shift for
    week/month's daily buckets that likely went unnoticed for the same
    reason a smaller error is easier to miss.
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    by_device_and_time: dict[str, dict[str, dict]] = {}

    def run_and_collect(flux: str, key: str):
        try:
            tables = query_api.query(flux)
        except Exception as e:
            logger.warning(f"Failed to query {key}({field}) range series for user={user}: {e}")
            return
        for table in tables:
            for record in table.records:
                device = record.values.get("device")
                value = record.get_value()
                period_start = record.get_time()
                if device is None or value is None or period_start is None:
                    continue
                period_key = period_start.isoformat()
                by_device_and_time.setdefault(device, {}).setdefault(period_key, {"t": period_key})[key] = value

    for stat in ("min", "max"):
        run_and_collect(f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {start_iso}, stop: {stop_iso})
          |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
          |> filter(fn: (r) => r.user == "{user}")
          |> filter(fn: (r) => r._field == "{field}")
          |> group(columns: ["device"])
          |> aggregateWindow(every: {window}, fn: {stat}, createEmpty: false, timeSrc: "_start")
        ''', stat)

    run_and_collect(f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "{field}")
      |> group(columns: ["device"])
      |> aggregateWindow(
           every: {window},
           fn: (tables=<-, column) => tables |> median(method: "exact_selector"),
           createEmpty: false,
           timeSrc: "_start",
         )
    ''', "median")

    result: dict[str, list[dict]] = {}
    for device, periods in by_device_and_time.items():
        # Only keep periods where min, max, AND median all came back -
        # a period missing one (shouldn't normally happen, since all
        # three queries share the same filter/window) is incomplete
        # data, not a real zero-width range or a period with no median.
        complete = [p for p in periods.values() if "min" in p and "max" in p and "median" in p]
        result[device] = sorted(complete, key=lambda p: p["t"])
    return result


def get_rolling_mean_series(field: str, user: str, start: datetime, end: datetime, window_days: int = 7) -> dict[str, list[dict]]:
    ''' Per-device rolling `window_days`-day mean, one value per
    calendar day in [start, end) - the trend line overlaid on the W/M
    range-bar charts, giving a smoothed view of drift beneath the
    day-to-day noise of individual bars' min/max/median. Only
    meaningful at daily granularity (week/month views) - a "7-day"
    rolling mean doesn't map cleanly onto the Year view's monthly
    buckets, so this isn't used there.

    Returns {"<device>": [{"t": <day ISO8601>, "value": v}, ...]},
    one entry per day actually within [start, end) that has enough
    trailing history to average - a day near the very start of a
    person's data (before `window_days` days of history exist) uses
    however many days ARE available rather than being dropped, the
    same "use what's there" approach a rolling average commonly takes
    (e.g. COVID case-tracking dashboards near the start of a series),
    rather than requiring a full window before showing anything.

    One aggregateWindow(fn: mean, every: 1d) query over an EXTENDED
    range - starting window_days-1 days before `start` - so the very
    first displayed day still has a genuine trailing window to average
    over; the rolling average itself is then computed in Python from
    those daily means.
    '''
    client = get_client()
    query_api = client.query_api()

    extended_start = start - timedelta(days=window_days - 1)
    start_iso = extended_start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "{field}")
      |> group(columns: ["device"])
      |> aggregateWindow(every: 1d, fn: mean, createEmpty: false, timeSrc: "_start")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query rolling mean series for {field}, user={user}: {e}")
        return {}

    # Per-device, chronologically sorted (day, value) pairs across the
    # EXTENDED range (including the window_days-1 lookback-only days).
    daily_by_device: dict[str, list[tuple[str, float]]] = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            value = record.get_value()
            period_start = record.get_time()
            if device is None or value is None or period_start is None:
                continue
            daily_by_device.setdefault(device, []).append((period_start.isoformat(), value))
    for device in daily_by_device:
        daily_by_device[device].sort(key=lambda p: p[0])

    start_iso_cutoff = start.astimezone(timezone.utc).isoformat()
    result: dict[str, list[dict]] = {}
    for device, days in daily_by_device.items():
        rolling: list[dict] = []
        for i, (day_iso, _value) in enumerate(days):
            if day_iso < start_iso_cutoff:
                continue  # a lookback-only day, not one to actually display
            window_slice = days[max(0, i - window_days + 1):i + 1]
            mean_value = sum(v for _, v in window_slice) / len(window_slice)
            rolling.append({"t": day_iso, "value": mean_value})
        if rolling:
            result[device] = rolling
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
      |> aggregateWindow(every: 1d, fn: mean, createEmpty: false, timeSrc: "_start")
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


def _zscore_comparison(today_value: dict[str, float], daily_values: dict[str, list[float]]) -> dict[str, dict]:
    ''' Shared z-score math behind every "today vs. trailing baseline"
    comparison bar - given one day's value and a list of baseline
    daily values per device, compute mean/stddev/z/delta. Extracted
    from get_baseline_comparison() so the identical math (including
    the zero-stddev edge case handling - see the comment below) is
    reused by get_nightly_baseline_comparison() too, rather than two
    near-identical copies of the same logic differing only in how
    "one day's value" gets computed upstream.

    Returns {"<device>": {"today": v, "baseline_mean": m,
    "baseline_stddev": s, "z": (today-mean)/stddev, "delta": today-mean}}.

    A device is omitted entirely if there isn't enough data to compute
    something meaningful - fewer than 2 baseline days (a stddev needs
    at least 2 points) or no value at all for that day. This is a real
    "insufficient data" case, not an error - same situation Zepp's own
    gauge shows early on, and the caller should treat it the same way
    (an empty/insufficient-data state, not a failure).
    '''
    result: dict[str, dict] = {}
    for device, values in daily_values.items():
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
            # a non-zero difference - a direct visual/textual
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


def get_baseline_comparison(field: str, user: str, baseline_days: int = 7, for_date: date | None = None) -> dict[str, dict]:
    ''' For one field, per device: one day's value (today by default,
    or `for_date` for the detail-view's day-navigation) compared
    against a trailing baseline - the mean and (sample) standard
    deviation of daily values over the `baseline_days` days immediately
    BEFORE that day (the day itself excluded, so a value is never
    compared against a baseline that includes itself). Powers the
    Slower/Faster z-scored comparison bar - the same "vs. your own
    baseline" gauge concept Zepp's own Resting Heart Rate/HRV pages
    show (see wearable-events/UI_DESIGN_NOTES.md), just computed here
    instead of left blank the way Zepp's own version was for lack of
    history.

    Uses a calendar-midnight-to-midnight day as "one day's value" -
    the right definition for a field like resting_heart_rate. HRV uses
    a different, night-anchored definition instead - see
    get_nightly_baseline_comparison().
    '''
    today_start, today_end = local_today_bounds(for_date)
    baseline_start = today_start - timedelta(days=baseline_days)

    daily = _daily_values(field, user, baseline_start, today_start)
    today_value = _device_stat_by_field(field, user, today_start, today_end, "last")
    return _zscore_comparison(today_value, daily)


def find_sleep_sessions_in_range(user: str, start: datetime, end: datetime) -> list[dict]:
    ''' Device-recorded sleep sessions (not subjective sleep-journal
    entries - see find_sleep_entries_in_range() for those) whose START
    falls within [start, end), each at least MIN_SLEEP_SESSION_SECONDS
    long. Returns one dict per session: {"device": str, "start_time":
    datetime, "end_time": datetime, "duration_s": int}, all in the
    local timezone.

    Reads the same sample_type=="sleep_session" / sleep_session_duration_s
    data find_last_completed_sleep_session() already relies on -
    generalized here to a date range and every device (that function
    only ever returns the single most recent session across all
    devices combined). end_time is computed as start + duration_s,
    using the point's own reliable InfluxDB timestamp for the start and
    the already-correctly-scaled duration field - deliberately not the
    separate raw sleep_session_wakeup field the parsers also write,
    since that field's raw units aren't something this file has
    established a trustworthy scaling for elsewhere, and getting that
    wrong would silently produce a garbage timestamp rather than an
    error.
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.sample_type == "sleep_session")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "sleep_session_duration_s")
      |> filter(fn: (r) => r._value >= {MIN_SLEEP_SESSION_SECONDS})
      |> filter(fn: (r) => r._value <= {MAX_SLEEP_SESSION_SECONDS})
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.error(f"Failed to query sleep sessions for user={user}: {e}")
        return []

    tz = ZoneInfo(TZ_NAME)
    sessions = []
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            start_time = record.get_time()
            duration_s = record.get_value()
            if device is None or start_time is None or duration_s is None:
                continue
            local_start = start_time.astimezone(tz)
            sessions.append({
                "device": device,
                "start_time": local_start,
                "end_time": local_start + timedelta(seconds=duration_s),
                "duration_s": int(duration_s),
            })
    return sessions


def _sleep_sessions_by_wake_date(user: str, start_date: date, end_date: date) -> dict[date, dict[str, dict]]:
    ''' Per-device longest sleep session for EACH wake date in
    [start_date, end_date) - the bulk version of what
    _sleep_session_for_night() does for one night at a time. Fetches
    the underlying sleep-session data ONCE for the whole range,
    instead of once per night the way calling _sleep_session_for_night()
    in a loop would (each of ITS calls independently re-queries an
    overlapping search window) - that redundancy is fine for the
    handful of nights get_nightly_baseline_comparison() needs, but
    would multiply badly for get_nightly_differential_series(), which
    needs a full trailing baseline for potentially dozens of displayed
    nights (a Month view).

    Resolves by WAKE-UP day (not the bedtime-day sleep_date convention
    used elsewhere in this file - see _sleep_session_for_night()'s own
    docstring for why that distinction matters), and keeps the LONGEST
    session per device per night when more than one ended the same day
    (a nap plus the main sleep) - same rules as the single-night
    version, just applied across the whole range in one pass.
    '''
    tz = ZoneInfo(TZ_NAME)
    range_start = datetime.combine(start_date, datetime.min.time(), tzinfo=tz) - timedelta(hours=36)
    range_end = datetime.combine(end_date, datetime.min.time(), tzinfo=tz) + timedelta(hours=12)

    by_date: dict[date, dict[str, dict]] = {}
    for session in find_sleep_sessions_in_range(user, range_start, range_end):
        wake_date = session["end_time"].date()
        if wake_date < start_date or wake_date >= end_date:
            continue
        best_by_device = by_date.setdefault(wake_date, {})
        device = session["device"]
        if device not in best_by_device or session["duration_s"] > best_by_device[device]["duration_s"]:
            best_by_device[device] = session
    return by_date


def _sleep_session_for_night(user: str, wake_date: date) -> dict[str, dict]:
    ''' Per-device sleep session that ended (woke up) on `wake_date` -
    the actual night HRV's nightly mean should be computed over,
    replacing an earlier fixed-clock-time heuristic window with each
    night's real, per-device recorded boundaries.

    A thin single-night wrapper around _sleep_sessions_by_wake_date()
    (see that function for the shared search-window/nap-vs-main-sleep
    logic) - kept as its own function since most callers only ever
    need one night at a time, and "the single-night case" reads more
    clearly than "the bulk function with a one-day range" at each call
    site.
    '''
    return _sleep_sessions_by_wake_date(user, wake_date, wake_date + timedelta(days=1)).get(wake_date, {})


def _nightly_mean_for_date(field: str, user: str, for_date: date) -> dict[str, float]:
    ''' Per-device mean of `field` readings during the ACTUAL sleep
    session that ended (woke up) on `for_date` - see
    _sleep_session_for_night() - one night's representative value for
    a field like HRV, where "today's HRV" conventionally means last
    night's mean, not a calendar-day average (which would dilute the
    figure with daytime readings for a device - the Colmi ring - that
    also samples HRV while awake). A device with no detected sleep
    session that night is simply absent - no fallback window is used.
    '''
    sessions = _sleep_session_for_night(user, for_date)
    result: dict[str, float] = {}
    for device, session in sessions.items():
        device_means = _device_stat_by_field(field, user, session["start_time"], session["end_time"], "mean")
        if device in device_means:
            result[device] = device_means[device]
    return result


def get_nightly_baseline_comparison(field: str, user: str, baseline_days: int = 7, for_date: date | None = None) -> dict[str, dict]:
    ''' Same z-score-vs-trailing-baseline comparison as
    get_baseline_comparison(), but using each day's NIGHTLY mean (the
    mean over that night's ACTUAL recorded sleep session - see
    _nightly_mean_for_date()) as that day's representative value,
    instead of a calendar-midnight-to-midnight mean. Built for HRV
    specifically.

    One query per night (today's night plus each baseline night, so
    up to baseline_days + 1 total) rather than one aggregateWindow()
    call for the whole range - each night's window is a different,
    data-derived span (that night's own sleep session), not a fixed
    period Flux's aggregateWindow() offset could express in one query.
    baseline_days is always small (7 or 14), so the extra queries cost
    little - consistent with this file's existing preference for
    several simple queries over one cleverer one.
    '''
    tz = ZoneInfo(TZ_NAME)
    if for_date is None:
        for_date = datetime.now(tz).date()

    today_value = _nightly_mean_for_date(field, user, for_date)

    daily: dict[str, list[float]] = {}
    for i in range(1, baseline_days + 1):
        night_values = _nightly_mean_for_date(field, user, for_date - timedelta(days=i))
        for device, v in night_values.items():
            daily.setdefault(device, []).append(v)

    return _zscore_comparison(today_value, daily)


def _primary_device_session(sessions: dict[str, dict]) -> tuple[str, dict]:
    ''' Picks the single primary device+session from a
    {device: session_dict} mapping (as returned by
    _sleep_session_for_night()) - the LONGEST session, same rule
    _sleep_sessions_by_wake_date() already applies to disambiguate a
    nap from the main night's sleep. Shared by every Sleep-tab-family
    function that needs "the one session for this night" rather than
    a per-device breakdown, so this rule lives in exactly one place.
    Caller's responsibility to have already checked `sessions` is
    non-empty.
    '''
    return max(sessions.items(), key=lambda item: item[1]["duration_s"])


def get_sleep_overview_for_night(user: str, wake_date: date) -> dict | None:
    ''' Everything the Sleep tab's main day-view needs for ONE specific
    night, combining several already-existing per-session queries into
    one call rather than making every caller re-derive the session
    window and re-pick a primary device itself.

    Picks ONE primary device's session when more than one exists for
    the same night (see _primary_device_session()) - Zepp's own Sleep
    tab is a single-device view by construction (it only ever shows
    the one device you're using), so this doesn't attempt a genuinely
    multi-device sleep tab, matching find_last_completed_sleep_session()'s
    own existing single-session convention already used for the Today
    tab.

    Returns None if no sleep session is recorded for this night (an
    empty dict from _sleep_session_for_night() - e.g. a night before
    any device was worn, or a genuinely missed sync) - the caller
    should treat this as "no sleep data for this night", not an error.
    '''
    sessions = _sleep_session_for_night(user, wake_date)
    if not sessions:
        return None

    device, session = _primary_device_session(sessions)
    start, end = session["start_time"], session["end_time"]

    stages = get_sleep_stage_breakdown(user, start, end, device=device)
    wake_events = count_wake_events(user, start, end, device=device)

    hr_means = _device_stat_by_field("heart_rate", user, start, end, "mean")
    resp_means = _device_stat_by_field("sleep_respiratory_rate", user, start, end, "mean")

    return {
        "device": device,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "duration_s": session["duration_s"],
        "stages_min": stages,
        "wake_events": wake_events,
        "avg_heart_rate": round(hr_means[device], 1) if device in hr_means else None,
        "avg_respiratory_rate": round(resp_means[device], 1) if device in resp_means else None,
    }


def get_sleep_timing_trend(user: str, start_date: date, end_date: date) -> list[dict]:
    ''' Per-night start_time/end_time/duration_s for each night waking
    in [start_date, end_date), one primary (longest) device's session
    per night - the shared underlying data behind BOTH the Sleep
    Duration detail page's "Last 7 days" bar chart AND the Sleep
    Regularity detail page's bedtime/wake-time scatter charts and
    weekly averages (see UI_DESIGN_NOTES.md for both) - one function
    rather than two, since both need the exact same per-night rows,
    just different slices of the same data.

    Returns a chronologically-sorted list of {"date": "YYYY-MM-DD",
    "start_time": <ISO8601>, "end_time": <ISO8601>, "duration_s": int,
    "device": str} - nights with no recorded session are simply
    omitted (not a zero-duration entry), so callers building an
    average or a bar chart don't need to filter these out themselves.
    '''
    by_date = _sleep_sessions_by_wake_date(user, start_date, end_date)
    result = []
    for wake_date in sorted(by_date):
        sessions = by_date[wake_date]
        if not sessions:
            continue
        device, session = _primary_device_session(sessions)
        result.append({
            "date": wake_date.strftime("%Y-%m-%d"),
            "start_time": session["start_time"].isoformat(),
            "end_time": session["end_time"].isoformat(),
            "duration_s": session["duration_s"],
            "device": device,
        })
    return result


def get_sleep_stage_trend(user: str, start_date: date, end_date: date) -> list[dict]:
    ''' Per-night stage-minute breakdown for each night waking in
    [start_date, end_date) - the Sleep tab's own "vs Last 7 Days"
    stacked-bar weekly view (see UI_DESIGN_NOTES.md). Reuses the same
    primary-device-per-night selection as get_sleep_timing_trend()
    (kept as a separate function rather than merged with it, since
    this needs its own get_sleep_stage_breakdown() call per night - a
    real query per night, not free to compute from the same rows
    get_sleep_timing_trend() already has).

    Returns a chronologically-sorted list of {"date": "YYYY-MM-DD",
    "stages_min": {...}} - nights with no recorded session are simply
    omitted, same convention as get_sleep_timing_trend().
    '''
    by_date = _sleep_sessions_by_wake_date(user, start_date, end_date)
    result = []
    for wake_date in sorted(by_date):
        sessions = by_date[wake_date]
        if not sessions:
            continue
        device, session = _primary_device_session(sessions)
        stages = get_sleep_stage_breakdown(user, session["start_time"], session["end_time"], device=device)
        result.append({
            "date": wake_date.strftime("%Y-%m-%d"),
            "stages_min": stages,
        })
    return result


def get_sleep_vitals_series(field: str, user: str, wake_date: date) -> dict[str, list[dict]]:
    ''' Raw (unaggregated) points for one field (heart_rate or
    sleep_respiratory_rate) within the actual sleep session window for
    one specific night - the full-night chart the Sleep Heart Rate and
    Sleep Respiratory Rate detail pages plot against a stage-hypnogram
    background (see UI_DESIGN_NOTES.md for both - same "physiological
    signal during sleep, plotted against the stage hypnogram" pattern
    confirmed for both pages).

    Resolves the night's actual session window first (same primary-
    device selection as the rest of this Sleep-tab-family - see
    _primary_device_session()), then reuses _grouped_series() - the
    same underlying query get_today_series() and the W/M/Y range
    endpoints already use - over that session's real [start, end)
    window rather than a calendar day, since a sleep session's actual
    boundaries rarely align with midnight.

    Filtered to the PRIMARY device's own series only, not every device
    that happened to report during the window - matches this whole
    Sleep-tab family's single-device convention (see
    get_sleep_overview_for_night()'s docstring), so an unrelated
    device's readings during the same hours can't leak into what's
    meant to be one specific night's chart.

    Returns the same shape as get_today_series():
    {"<device>": [{"t": <ISO8601>, "v": <value>}, ...]} - empty dict if
    no sleep session is recorded for this night.
    '''
    sessions = _sleep_session_for_night(user, wake_date)
    if not sessions:
        return {}
    device, session = _primary_device_session(sessions)
    series = _grouped_series(field, user, session["start_time"], session["end_time"])
    return {device: series[device]} if device in series else {}


def get_nightly_differential_series(field: str, user: str, start_date: date, end_date: date, baseline_days: int = 7) -> dict[str, list[dict]]:
    ''' Per-device, per-night DELTA from a trailing baseline_days-night
    rolling average, for each night waking in [start_date, end_date) -
    the "day by day, how far off your recent normal" TREND Zepp's own
    temperature Week view shows (7 days of differential from the
    moving average) - as distinct from get_nightly_baseline_comparison(),
    which only ever gives ONE such comparison (today vs. baseline), not
    a series of them. A plain delta in the field's own units (e.g. degrees),
    not a z-score - matching what Zepp actually displays for
    temperature, which uses fixed absolute thresholds (person-confirmed:
    roughly +-0.5/1.0/1.5 degrees) rather than a statistical measure.

    Returns {"<device>": [{"t": <wake date ISO8601>, "delta": v,
    "baseline_mean": m}, ...]}. A night is only included once it has at
    least 2 baseline nights behind it (same rule as the single-comparison
    functions - a delta against fewer than 2 baseline points isn't
    meaningful).

    Needs each displayed night's own trailing baseline, so this pulls
    in [start_date - baseline_days, end_date) of nightly values - the
    sleep-SESSION lookup for that whole extended range is fetched ONCE
    up front (_sleep_sessions_by_wake_date()), but each night's actual
    field-value mean still needs its own query (one per night in the
    extended range) - unlike get_rolling_mean_series()'s calendar-day
    version, a real sleep session's boundaries differ night to night
    and can't be expressed as a single batched aggregateWindow() call.
    This means a Month view here costs roughly (30 + baseline_days)
    queries - noticeably more than this file's other functions, and a
    reasonable place to look first if this page ever turns out to load
    slowly in practice; not optimized further here without evidence
    that it actually needs to be.
    '''
    sessions_by_date = _sleep_sessions_by_wake_date(user, start_date - timedelta(days=baseline_days), end_date)

    nightly_by_device: dict[str, dict[str, float]] = {}
    for wake_date, sessions in sessions_by_date.items():
        for device, session in sessions.items():
            device_means = _device_stat_by_field(field, user, session["start_time"], session["end_time"], "mean")
            if device in device_means:
                nightly_by_device.setdefault(device, {})[wake_date.isoformat()] = device_means[device]

    result: dict[str, list[dict]] = {}
    for device, by_date in nightly_by_device.items():
        series: list[dict] = []
        d = start_date
        while d < end_date:
            d_iso = d.isoformat()
            if d_iso in by_date:
                baseline_values = [
                    by_date[(d - timedelta(days=i)).isoformat()]
                    for i in range(1, baseline_days + 1)
                    if (d - timedelta(days=i)).isoformat() in by_date
                ]
                if len(baseline_values) >= 2:
                    baseline_mean = statistics.mean(baseline_values)
                    series.append({
                        "t": d_iso,
                        # 1 decimal - matches _zscore_comparison's own
                        # rounding for the same kind of value elsewhere
                        # in this file, and what the frontend displays
                        # everywhere temperature numbers show up.
                        "delta": round(by_date[d_iso] - baseline_mean, 1),
                        "baseline_mean": round(baseline_mean, 1),
                    })
            d += timedelta(days=1)
        if series:
            result[device] = series
    return result


def get_today_steps(user: str, for_date: date | None = None) -> dict[str, int]:
    ''' Today's (local calendar day) total steps per device - a sum,
    not last/mean/etc., since steps is a per-sample count that needs
    adding up across the day rather than reduced to one representative
    reading.

    `for_date` defaults to today when omitted - same date-navigation
    use as get_today_vitals().
    '''
    start, end = local_today_bounds(for_date)
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


def get_sleep_stage_breakdown(user: str, session_start: datetime, session_end: datetime, device: str | None = None) -> dict[str, int]:
    ''' Minutes spent in each sleep stage (light/deep/rem/awake) for one
    specific sleep session, identified by its own [start, end) window -
    NOT a lookback query, the caller (typically find_last_completed_
    sleep_session's result) already knows exactly which session.

    Sums sleep_stage_duration_s (already extracted per stage segment,
    see parser/activefit and parser/colmi's sleep stage extraction)
    grouped by the sleep_stage tag, converted to whole minutes.

    `device`, if given, filters to that device's own stage data only -
    added (2026-09) after a confirmed real bug: this function used to
    NOT filter by device at all, on the assumption that "a single
    sleep session belongs to one device by construction". That
    assumption breaks once more than one device has sleep-stage data
    for the same or overlapping night (e.g. a ring's historical data
    persisting in InfluxDB after being unbound from Gadgetbridge,
    alongside a watch that's since taken over) - without this filter,
    the query silently SUMMED every device's stage minutes together
    for the same calendar window, confirmed against real reported
    values showing stage-minute sums roughly 2-3x a plausible night's
    total. `device` is optional (not required) so any caller that
    genuinely doesn't have one yet still gets a result, just without
    this protection - every current caller does have one, though.
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = session_start.astimezone(timezone.utc).isoformat()
    stop_iso = session_end.astimezone(timezone.utc).isoformat()

    device_filter = f'|> filter(fn: (r) => r.device == "{device}")\n      ' if device else ""

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.sample_type == "sleep_stage")
      |> filter(fn: (r) => r._field == "sleep_stage_duration_s")
      {device_filter}|> group(columns: ["sleep_stage"])
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


def count_wake_events(user: str, session_start: datetime, session_end: datetime, device: str | None = None) -> int:
    ''' Count of distinct awake-stage SEGMENTS during one sleep session -
    genuinely different information from get_sleep_stage_breakdown()'s
    duration sum: three separate 2-minute wake-ups and one continuous
    6-minute wake-up both sum to 6 minutes of total awake time, but
    are very different sleep-quality signals. Matches Zepp's own
    "Awake ... N wake events" framing on the Sleep Metrics card (see
    UI_DESIGN_NOTES.md).

    Counts the sleep_stage_duration_s field specifically - written
    ONCE per stage segment, at that segment's own start point (see
    parser/activefit's sleep-stage extraction) - not sleep_stage_active
    (written TWICE per segment, a start=1 marker and an end=0 marker
    one second before the next stage begins, which would double the
    count if used here instead).

    Same optional device-filtering as get_sleep_stage_breakdown, same
    reasoning: without it, this would silently sum wake-event counts
    across multiple devices with stage data for the same night rather
    than counting one device's own.
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = session_start.astimezone(timezone.utc).isoformat()
    stop_iso = session_end.astimezone(timezone.utc).isoformat()

    device_filter = f'|> filter(fn: (r) => r.device == "{device}")\n      ' if device else ""

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.sample_type == "sleep_stage")
      |> filter(fn: (r) => r.sleep_stage == "awake")
      |> filter(fn: (r) => r._field == "sleep_stage_duration_s")
      {device_filter}|> count()
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to count wake events for user={user}: {e}")
        return 0

    total = 0
    for table in tables:
        for record in table.records:
            value = record.get_value()
            if value is not None:
                total += value
    return total


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