"""Subjective sleep journal entries (score, mood, qualifiers)."""
import hashlib
from datetime import date, datetime, time, timezone

from influxdb_client import Point
from influxdb_client.client.write_api import SYNCHRONOUS
from loguru import logger

from app.config import INFLUX_BUCKET, INFLUX_ORG, SLEEP_MEASUREMENT
from app.queries.client import get_client
from app.queries.sleep_sessions import _primary_device_session, _sleep_session_for_night

def sleep_entry_id(user: str, session_start: datetime) -> str:
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

    Takes the datetime directly (not a pre-formatted ISO string) and
    normalizes to UTC before hashing - a SECOND real bug, reported
    directly ("editing doesn't preserve changes") and found here: the
    exact same instant produces a DIFFERENT isoformat() string
    depending on which timezone the datetime object happens to carry.
    session["start_time"] from a fresh sensor-session query comes back
    in this app's configured LOCAL timezone, but InfluxDB's own query
    results - e.g. find_sleep_entry_by_id()'s "start_time", read back
    from the journal entry itself - always come back UTC-parsed
    (confirmed via influxdb_client's own date_utils.py), regardless of
    what timezone the point was originally written with. Without
    normalizing first, a PATCH computed its entry_id from the UTC-
    parsed round-tripped timestamp while the ORIGINAL entry had been
    tagged with the id from the local-timezone one - two different
    strings for the same real instant - so an edit silently wrote a
    brand-new, orphaned point instead of overwriting the original,
    which is exactly why saved edits appeared not to stick.
    '''
    normalized = session_start.astimezone(timezone.utc).isoformat()
    raw = f"{user}|{normalized}".encode()
    return hashlib.sha1(raw).hexdigest()[:12]

def write_sleep_point(*, user: str, session_start: datetime, sleep_date: str, score: int,
                       qualifiers: dict, pre_sleep_factors: dict | None = None, submission_ts: datetime):
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

    pre_sleep_factors are a SEPARATE concept from qualifiers - things
    that happened BEFORE sleep (read, had alcohol, late screen time,
    ...) rather than how the sleep itself felt (groggy, vivid dreams,
    ...). Written with a "factor_" field-name prefix specifically so
    find_sleep_entries_in_range() can tell the two categories apart
    purely from the field name, without needing to hardcode either
    category's own known-key list on the backend (matching how
    qualifiers already work generically today).
    '''
    entry_id = sleep_entry_id(user, session_start)
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
        for factor, value in (pre_sleep_factors or {}).items():
            p = p.field(f"factor_{factor}", bool(value))

        p = p.time(session_start)

        write_api.write(INFLUX_BUCKET, INFLUX_ORG, p)

    logger.info(f"Wrote subjective_sleep point for {sleep_date} (entry_id={entry_id}): score={score} user={user}")
    return entry_id

def _split_sleep_entry_fields(values: dict) -> tuple[dict, dict]:
    ''' Shared by find_sleep_entries_in_range() and find_sleep_entry_by_id()
    - splits a pivoted sleep-entry row's boolean fields into qualifiers
    (how the sleep itself felt) vs. pre_sleep_factors (what happened
    before it), distinguished by the "factor_" field-name prefix (see
    write_sleep_point's own comment on why). Extracted here specifically
    so this splitting logic lives in exactly one place - it was
    duplicated across both functions before pre_sleep_factors existed,
    and duplicating it again would risk the two copies drifting apart
    the next time either changes.
    '''
    KNOWN_NON_QUALIFIER_KEYS = {
        "_time", "_start", "_stop", "_measurement", "result", "table",
        "sleep_date", "user", "entry_id", "score", "logged_at",
    }
    qualifiers = {}
    pre_sleep_factors = {}
    for k, v in values.items():
        if k in KNOWN_NON_QUALIFIER_KEYS or not isinstance(v, bool):
            continue
        if k.startswith("factor_"):
            pre_sleep_factors[k[len("factor_"):]] = v
        else:
            qualifiers[k] = v
    return qualifiers, pre_sleep_factors

def find_sleep_entries_in_range(user: str, start: datetime, end: datetime) -> list[dict]:
    ''' Read-only query for subjective sleep entries in the given range.
    Unlike events, each sleep entry is a single point with all fields
    (score, logged_at, qualifiers, pre-sleep factors) together - no
    per-tag multi-point reconstruction needed, just a pivot to combine
    the fields onto one row per point.
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

    results = []
    for table in tables:
        for record in table.records:
            values = record.values
            qualifiers, pre_sleep_factors = _split_sleep_entry_fields(values)
            results.append({
                "entry_id": values.get("entry_id"),
                "sleep_date": values.get("sleep_date"),
                "start_time": record.get_time().isoformat(),
                "score": values.get("score"),
                "logged_at": values.get("logged_at"),
                "qualifiers": qualifiers,
                "pre_sleep_factors": pre_sleep_factors,
            })
    return results

def get_sleep_journal_rollup(user: str, start_date: date, end_date: date) -> dict:
    ''' Aggregates subjective sleep journal entries across
    [start_date, end_date) into per-tag frequency counts - the "Time
    Asleep (advanced)" page's own weekly Bedtime Journal / Wake-up Mood
    rollup cards (see UI_DESIGN_NOTES.md's M/Y zoom levels entry: "a
    ranked list of tag options, one row per tag actually used in the
    period, showing a percentage of days + a day-count"). Reuses
    find_sleep_entries_in_range() directly - no new Flux query, just
    aggregation over what it already returns.

    pre_sleep_factors -> "Bedtime Journal" (things logged as happening
    BEFORE sleep - read, alcohol, late_screen_time, etc.); score ->
    "Wake-up Mood" (the 1-5 rating, rolled up the same way, treating
    each score value 1-5 as its own "tag"); qualifiers (how the sleep
    itself felt - groggy, vivid_dreams, etc.) included too as a third
    rollup, even though UI_DESIGN_NOTES.md's own two-category naming
    doesn't have a slot for it - it's the same kind of subjective
    per-night data and there's no reason to drop it just because
    Zepp's own page didn't have a third section for it.

    total_nights is the number of CALENDAR NIGHTS in the period, not
    the number of entries - a night with no entry at all still counts
    toward "no record" below, matching Zepp's own real screenshot
    treatment of "No record" as its own pseudo-tag row rather than
    simply excluding nights with nothing logged.

    Multiple entries CAN share the same sleep_date (see
    write_sleep_point's own docstring - e.g. a nap plus the main
    night's sleep) - tag counts are deduplicated by sleep_date per tag
    (a `set`, not a running total) so a night logging the same factor
    twice across two sessions still counts as ONE night for that
    factor, not two.
    '''
    start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc)
    entries = find_sleep_entries_in_range(user, start_dt, end_dt)

    total_nights = (end_date - start_date).days

    nights_with_entry = set()
    factor_dates: dict[str, set] = {}
    qualifier_dates: dict[str, set] = {}
    mood_dates: dict[int, set] = {}

    for entry in entries:
        sleep_date = entry.get("sleep_date")
        if not sleep_date:
            continue
        nights_with_entry.add(sleep_date)
        for factor, is_set in (entry.get("pre_sleep_factors") or {}).items():
            if is_set:
                factor_dates.setdefault(factor, set()).add(sleep_date)
        for qualifier, is_set in (entry.get("qualifiers") or {}).items():
            if is_set:
                qualifier_dates.setdefault(qualifier, set()).add(sleep_date)
        score = entry.get("score")
        if score is not None:
            mood_dates.setdefault(score, set()).add(sleep_date)

    def to_rollup(dates_by_key: dict) -> list[dict]:
        rows = [
            {"key": k, "nights": len(dates), "pct": round(len(dates) / total_nights * 100, 1) if total_nights else 0.0}
            for k, dates in dates_by_key.items()
        ]
        return sorted(rows, key=lambda r: r["nights"], reverse=True)

    no_record_nights = total_nights - len(nights_with_entry)

    return {
        "total_nights": total_nights,
        "pre_sleep_factors": to_rollup(factor_dates),
        "qualifiers": to_rollup(qualifier_dates),
        "mood_scores": to_rollup(mood_dates),
        "no_record_nights": no_record_nights,
        "no_record_pct": round(no_record_nights / total_nights * 100, 1) if total_nights else 0.0,
    }

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
            qualifiers, pre_sleep_factors = _split_sleep_entry_fields(values)
            return {
                "entry_id": values.get("entry_id"),
                "sleep_date": values.get("sleep_date"),
                "start_time": record.get_time(),
                "score": values.get("score"),
                "logged_at": values.get("logged_at"),
                "qualifiers": qualifiers,
                "pre_sleep_factors": pre_sleep_factors,
            }
    return None

def find_sleep_entry_for_wake_date(user: str, wake_date: date) -> dict | None:
    ''' The subjective sleep journal entry (if any) for the night that
    woke up on `wake_date` - what the per-night Sleep Heart Rate/
    Sleep Duration/etc. detail pages' own date-nav is already anchored
    to, replacing the old flat "Recent nights" list (which showed every
    logged entry across many nights at once, from before those per-
    night pages existed).

    Resolves the actual session for that night first (same primary-
    device-session logic every other per-night Sleep function uses -
    see _sleep_session_for_night()), then looks up the entry via the
    SAME deterministic entry_id derivation write_sleep_point()/
    sleep_entry_id() use (the session's own start time, normalized to
    UTC before hashing - see sleep_entry_id()'s own docstring for why
    that normalization matters) - not by filtering on the entry's own
    "sleep_date" tag, which is the BEDTIME's date (typically the day
    BEFORE wake_date) and would otherwise require the caller to get
    that off-by-one-day mapping right itself every time.

    Returns None if there's no recorded session for this night at all,
    OR a session exists but nothing has been logged for it yet - both
    are "nothing to show/edit yet" from the caller's point of view, and
    the caller already knows separately (from /sleep/overview) whether
    a session exists at all.
    '''
    sessions = _sleep_session_for_night(user, wake_date)
    if not sessions:
        return None
    _, session = _primary_device_session(sessions)
    entry_id = sleep_entry_id(user, session["start_time"])
    return find_sleep_entry_by_id(user, entry_id)

def write_sleep_entry_for_wake_date(user: str, wake_date: date, score: int, qualifiers: dict,
                                     pre_sleep_factors: dict, submission_ts: datetime) -> dict | None:
    ''' Writes a subjective sleep journal entry for the night that woke
    up on `wake_date` - the write-side counterpart to
    find_sleep_entry_for_wake_date(), used by the per-night Sleep Heart
    Rate/Sleep Duration/etc. pages' own journal section to submit for
    THIS SPECIFIC night, replacing the old behavior of always writing
    against "whatever the most recently completed session happens to
    be" (find_last_completed_sleep_session) - a real gap once several
    different nights' pages could all be open/edited independently
    rather than only ever the latest one.

    write_sleep_point() itself is keyed by the session's own real start
    time (see its own docstring), so re-submitting for the SAME night
    correctly overwrites rather than duplicating - no separate
    edit-vs-create distinction needed at this layer.

    Returns None if there's no recorded session for this night at all
    (caller's responsibility to turn that into an error response) - a
    subjective entry can't be logged against a night with no session
    to anchor it to.
    '''
    sessions = _sleep_session_for_night(user, wake_date)
    if not sessions:
        return None
    _, session = _primary_device_session(sessions)
    resolved_sleep_date = session["start_time"].strftime("%Y-%m-%d")
    entry_id = write_sleep_point(
        user=user,
        session_start=session["start_time"],
        sleep_date=resolved_sleep_date,
        score=score,
        qualifiers=qualifiers,
        pre_sleep_factors=pre_sleep_factors,
        submission_ts=submission_ts,
    )
    return {
        "entry_id": entry_id,
        "sleep_date": resolved_sleep_date,
        "resolved_session_duration_s": session["duration_s"],
    }

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