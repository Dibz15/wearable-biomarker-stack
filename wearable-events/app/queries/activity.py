"""Steps, activity sessions, sitting/standing time."""
import statistics
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from app.config import (
    INFLUX_BUCKET,
    SENSOR_MEASUREMENT,
    SESSION_GAP_MINUTES,
    SESSION_MIN_DURATION_MINUTES,
    STAND_INTENSITY_THRESHOLD,
    TZ_NAME,
)
from app.queries.client import get_client
from app.queries.vitals import _device_stat_by_field, local_today_bounds

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
                "start_ms": int(entry_start.timestamp() * 1000),
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
    {"start": <ISO8601>, "start_ms": <int|None>, "end": <ISO8601>,
     "device": <str>, "label": <str>, "raw_code": <int|str|None>,
     "avg_heart_rate": <float|None>, "source": "derived"|"precomputed"}

    `start_ms` (this entry's own BASE_ACTIVITY_SUMMARY.START_TIME, in
    epoch milliseconds) is only ever present for "precomputed" entries
    - the identifier the Workout Detail page's own
    /activity/workout/{start_ms} endpoint expects, since only those
    entries HAVE a corresponding workout to look up at all. None for
    "derived" entries - the frontend should treat those as not
    tappable through to a detail page, not attempt the same link with
    a missing identifier.
    '''
    derived = get_activity_sessions(user, for_date)
    precomputed = get_precomputed_activity_sessions(user, for_date)

    combined = []
    for s in derived:
        combined.append({
            "start": s["start"],
            "start_ms": None,  # no BASE_ACTIVITY_SUMMARY row to look up - not tappable
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
            "start_ms": s["start_ms"],
            "end": s["end"],
            "device": s["device"],
            "label": name if has_real_name else "unknown",
            "raw_code": s["activity_kind_summary"],
            "avg_heart_rate": s["avg_heart_rate"],
            "source": "precomputed",
        })

    combined.sort(key=lambda s: s["start"])
    return combined


# Firstbeat's own publicly documented Training Effect scale
# (firstbeat.com/en/science-and-physiology/epoc-and-training-effect) -
# confirmed against real data this app already extracts (both 0.9 and
# 0.6 correctly land in "No effect"; 3.2 lands in "Improving effect",
# consistent with Zepp's own simplified "Good" wording over the same
# underlying scale) and independently reinforced by Amazfit's own
# "Running Technology" page describing the same 0-5 range. Applies
# identically to both aerobic and anaerobic values per Firstbeat's own
# documentation. Same "individually cited, not a blended/invented
# score" spirit as get_sleep_duration_recommendation()'s own NSF
# citation - a real published scale, not a guess.
TRAINING_EFFECT_SCALE = [
    (0.9, "No effect"),
    (1.9, "Minor effect"),
    (2.9, "Maintaining effect"),
    (3.9, "Improving effect"),
    (4.9, "Highly improving effect"),
    (5.0, "Overreaching effect"),
]

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