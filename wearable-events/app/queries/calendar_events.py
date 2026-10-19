"""Manual/calendar event read-writes (events measurement)."""
import hashlib
import threading
import uuid
from datetime import datetime, time, timedelta, timezone

from influxdb_client import Point
from influxdb_client.client.write_api import SYNCHRONOUS
from loguru import logger

from app.config import EVENTS_MEASUREMENT, INFLUX_BUCKET, INFLUX_ORG
from app.queries.client import get_client

# Serializes "read a calendar event's cache row (excluded flag, tags),
# then write/delete its Influx points" across the scheduled sync, the
# reprocess thread and the per-event Timeline endpoints. Without it, a
# sync that read excluded=0 just before someone clicked "Didn't attend"
# could write the points straight back after they'd been deleted.
#
# In-process only: correct because the app runs as ONE uvicorn process
# (Dockerfile CMD, no --workers) with its scheduler thread inside it.
# Running several workers would also run several schedulers and need a
# cross-process guard instead - see docs/REFERENCE.md.
calendar_points_lock = threading.RLock()

def _predicate_str(value: str) -> str:
    ''' Escape a value for a double-quoted string in an InfluxDB delete
    predicate. The predicate parser reads backslash escapes (escaped
    backslash, double quote, newline) and rejects a lone backslash, a bare
    double quote or a raw newline, so calendar names / tags containing any
    of those would otherwise make every delete fail - verified against
    InfluxDB 2.7's delete endpoint.
    '''
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _flux_str(value: str) -> str:
    ''' Escape a value for a double-quoted Flux string literal (InfluxDB
    OSS doesn't support parameterized queries). Flux also treats "${" as
    the start of string interpolation. '''
    return (value.replace("\\", "\\\\").replace('"', '\\"')
                 .replace("\n", "\\n").replace("${", "\\${"))


def calendar_event_id(calendar: str, start_time: str, title: str) -> str:
    ''' Deterministic event_id for calendar-derived events, per spec §6 -
    same event synced twice produces the same id, so re-syncs are
    idempotent-ish (see the known stale-tag caveat noted separately).
    '''
    raw = f"{calendar}|{start_time}|{title}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]

def manual_event_id() -> str:
    return str(uuid.uuid4())

def delete_event_tag_point(*, user: str, tag: str, source: str, timestamp: datetime, calendar: str | None = None):
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
    handling. Always scoped to `user`: household members commonly
    subscribe to the same shared calendar, so without it one person's
    edit/strike-off would also delete the other's identical point.
    (Usernames can't be renamed, so the `user` tag is stable.)

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
    # itself, not just the value, is the documented fix. Quoting "user",
    # "source" and "calendar" defensively too, since there's no published
    # authoritative list of every reserved word in this specific grammar.
    predicate = (
        f'_measurement="{_predicate_str(EVENTS_MEASUREMENT)}" AND "user"="{_predicate_str(user)}" '
        f'AND "tag"="{_predicate_str(tag)}" AND "source"="{_predicate_str(source)}"'
    )
    if calendar is not None:
        predicate += f' AND "calendar"="{_predicate_str(calendar)}"'
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


def find_calendar_event_point_tags(*, user: str, event_id: str, timestamp: datetime) -> set[str]:
    ''' The tags this calendar event ACTUALLY has points for in Influx,
    for this user - looked up by its event_id field over the same 1-second
    window the deletes use. Exclude uses this rather than trusting the
    cache's applied_tags, which can lag behind Influx: a sync that
    retagged an event writes the new tag without deleting the old one,
    and a retag whose delete failed leaves the old point behind.

    Raises on query failure (unlike the other lookups here) - the caller
    must not treat "couldn't check" as "nothing there".
    '''
    start = timestamp.astimezone(timezone.utc)
    stop = start + timedelta(seconds=1)
    flux = f'''
    from(bucket: "{_flux_str(INFLUX_BUCKET)}")
      |> range(start: {start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}, stop: {stop.strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
      |> filter(fn: (r) => r._measurement == "{_flux_str(EVENTS_MEASUREMENT)}")
      |> filter(fn: (r) => r.source == "calendar" and r.user == "{_flux_str(user)}")
      |> filter(fn: (r) => r._field == "event_id" and r._value == "{_flux_str(event_id)}")
      |> keep(columns: ["tag"])
    '''
    tables = get_client().query_api().query(flux)
    return {record.values["tag"] for table in tables for record in table.records if record.values.get("tag")}


def delete_calendar_event_points(*, user: str, event_id: str, tags, timestamp: datetime,
                                 calendar: str, action: str) -> list[str]:
    ''' Best-effort delete of one calendar event's points for each tag.
    Failures are logged, not raised - returns the tags whose delete
    failed so callers that care (exclude/restore) can act on them.
    `action` is only for the log line ("excluding", "retagging", ...).
    '''
    failed = []
    for tag in sorted(set(tags)):
        try:
            delete_event_tag_point(user=user, tag=tag, source="calendar", timestamp=timestamp, calendar=calendar)
        except Exception as e:
            logger.warning(f"Failed to delete point while {action} calendar event {event_id} (tag={tag}): {e}")
            failed.append(tag)
    return failed


def replace_calendar_event_points(*, user: str, event_id: str, old_tags, new_tags: list[str],
                                  timestamp: datetime, calendar: str, duration_min: int | None,
                                  action: str) -> list[str]:
    ''' Move one calendar event's Influx points from old_tags to new_tags:
    deletes points for tags no longer present (best-effort, see
    delete_calendar_event_points), then writes the full new set.
    Rewriting unchanged tags is safe - identical tag-set + timestamp
    overwrites in InfluxDB rather than duplicating. Write failures DO
    raise. Returns the tags whose delete failed.
    '''
    failed = delete_calendar_event_points(
        user=user, event_id=event_id, tags=set(old_tags) - set(new_tags),
        timestamp=timestamp, calendar=calendar, action=action,
    )
    write_event_points(
        user=user,
        tags=new_tags,
        source="calendar",
        timestamp=timestamp,
        event_id=event_id,
        calendar=calendar,
        duration_min=duration_min,
    )
    return failed
