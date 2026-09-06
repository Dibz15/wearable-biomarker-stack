"""Objective sleep sessions: overview, hypnogram, stages, regularity, quality."""
import statistics
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from app.config import (
    INFLUX_BUCKET,
    MAX_SLEEP_SESSION_SECONDS,
    MIN_SLEEP_SESSION_SECONDS,
    SENSOR_MEASUREMENT,
    SLEEP_DURATION_RECOMMENDED_HOURS,
    SLEEP_QUALITY_AGE_BRACKET,
    SLEEP_QUALITY_THRESHOLDS,
    TZ_NAME,
)
from app.queries.client import get_client
from app.queries.vitals import _device_stat_by_field, _grouped_series, _zscore_comparison

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
                # Gadgetbridge).
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

    quality = get_sleep_quality_indicators(
        user, start, end, session["duration_s"], stages.get("awake", 0), device=device
    )
    duration_recommendation = get_sleep_duration_recommendation(session["duration_s"])

    return {
        "device": device,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "duration_s": session["duration_s"],
        "stages_min": stages,
        "wake_events": wake_events,
        "avg_heart_rate": round(hr_means[device], 1) if device in hr_means else None,
        "avg_respiratory_rate": round(resp_means[device], 1) if device in resp_means else None,
        "sleep_quality": quality,
        "duration_recommendation": duration_recommendation,
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

def get_sleep_regularity_index(user: str, end_date: date | None = None, num_days: int = 7) -> dict | None:
    ''' Sleep Regularity Index (SRI) - Phillips et al. 2017, "Irregular
    sleep/wake patterns are associated with poorer academic performance
    and delayed circadian and sleep/wake timing," Scientific Reports
    7:3216 (doi:10.1038/s41598-017-03171-4). Since independently
    replicated in much larger cohorts (UK Biobank, >500,000 adults;
    MESA's older-adult sample) and linked to cardiometabolic risk,
    incident depression/anxiety, and all-cause mortality, independent
    of sleep duration itself. This is a real, peer-reviewed,
    widely-used metric we can actually compute and cite - NOT an
    attempt to reverse-engineer Zepp's own unpublished 0-100%
    "regularity" score (see UI_DESIGN_NOTES.md's own note that that
    formula was never published and isn't reproducible).

    Definition: the percentage probability of being in the same sleep/
    wake state (asleep vs. awake) at any two clock times exactly 24
    hours apart, averaged across every pair of consecutive days in the
    window:

        SRI = -100 + (200 / (M*(N-1))) * sum_j sum_i delta(s[i,j], s[i+1,j])

    where N is the number of days, M is the number of epochs per day
    (here, 1-minute epochs, so M=1440), s[i,j] is the sleep/wake state
    (1=asleep, 0=awake) at day i minute j, and delta=1 when two
    CONSECUTIVE days agree at the same minute-of-day, 0 otherwise.
    Ranges from -100 (every consecutive day exactly disagrees) to +100
    (identical sleep/wake timing every day); 0 represents a
    statistically random pattern. This exact formula (and the
    -100..100 scale) is consistent across the original paper and every
    later replication/toolkit (GGIR, sleepreg) found during research.

    Days are defined NOON-TO-NOON, the original paper's own convention
    - and the same one this app's bedtime/wake-time charts already use
    independently - so a normal night's sleep session (PM to AM) falls
    entirely within one day's own window rather than being split
    across two by a midnight boundary.

    A night with no recorded session breaks BOTH day-pairs it would
    have been part of (the pair ending on it and the pair starting the
    night after) - SRI is only computed over pairs where both days have
    a real recorded session, rather than failing the whole window over
    one missing night. Only the binary asleep/awake state matters here
    (not sleep stage) - naps aren't included, since this app's
    underlying data model is one main per-night session, not full nap
    detection.

    Returns {"sri": float, "days_used": int, "pairs_used": int,
    "source": <citation>}, or None if fewer than 2 usable consecutive-
    day pairs exist in the window (matching the "insufficient data"
    state every other implementation of this metric also reports for a
    too-short recording).
    '''
    tz = ZoneInfo(TZ_NAME)
    if end_date is None:
        end_date = datetime.now(tz).date()
    start_date = end_date - timedelta(days=num_days - 1)
    by_date = _sleep_sessions_by_wake_date(user, start_date, end_date + timedelta(days=1))

    MINUTES_PER_DAY = 1440

    def day_vector(wake_date: date) -> list[bool] | None:
        ''' 1440-length asleep/awake array for the noon-to-noon day
        ending at `wake_date` noon (i.e. [wake_date-1 12:00,
        wake_date 12:00) local time) - True at any minute covered by
        that night's own recorded session. None if there's no session
        for that night at all.
        '''
        sessions = by_date.get(wake_date)
        if not sessions:
            return None
        _, session = _primary_device_session(sessions)
        day_start = datetime.combine(wake_date - timedelta(days=1), time(12, 0), tzinfo=tz)
        sess_start = session["start_time"].astimezone(tz)
        sess_end = session["end_time"].astimezone(tz)
        start_min = max(0, int((sess_start - day_start).total_seconds() // 60))
        end_min = min(MINUTES_PER_DAY, int((sess_end - day_start).total_seconds() // 60))
        vec = [False] * MINUTES_PER_DAY
        for m in range(start_min, end_min):
            vec[m] = True
        return vec

    total_matches = 0
    total_epochs = 0
    pairs_used = 0
    days_used: set[date] = set()

    prev_vec, prev_date = None, None
    current = start_date
    while current <= end_date:
        vec = day_vector(current)
        if vec is not None and prev_vec is not None and (current - prev_date).days == 1:
            total_matches += sum(1 for a, b in zip(prev_vec, vec) if a == b)
            total_epochs += MINUTES_PER_DAY
            pairs_used += 1
            days_used.add(prev_date)
            days_used.add(current)
        prev_vec, prev_date = vec, current
        current += timedelta(days=1)

    if pairs_used < 2:
        return None

    sri = -100 + (200 * total_matches / total_epochs)
    return {
        "sri": round(sri, 1),
        "days_used": len(days_used),
        "pairs_used": pairs_used,
        "source": "Phillips et al. 2017, Scientific Reports 7:3216 (doi:10.1038/s41598-017-03171-4)",
    }

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

def get_sleep_vitals_trend(field: str, user: str, start_date: date, end_date: date) -> list[dict]:
    ''' Per-night min/max/median/mean of `field` (heart_rate or
    sleep_respiratory_rate) for each night waking in
    [start_date, end_date) - the "Last 7 days" range-bar chart (with a
    per-night median tick and a flat weekly-mean line, same visual
    language as this app's other range-bar charts) on the Sleep Heart
    Rate / Sleep Respiratory Rate detail pages. Same primary-device-
    per-night selection as get_sleep_timing_trend()/
    get_sleep_stage_trend() (see _primary_device_session()).

    Returns a chronologically-sorted list of {"date": "YYYY-MM-DD",
    "device": <device name>, "min": float, "max": float,
    "median": float, "mean": float} - nights with no recorded session,
    OR no readings of this field during that session, are simply
    omitted (not a null entry), so callers building an average or a
    chart don't need to filter these out themselves.
    '''
    by_date = _sleep_sessions_by_wake_date(user, start_date, end_date)
    result = []
    for wake_date in sorted(by_date):
        sessions = by_date[wake_date]
        if not sessions:
            continue
        device, session = _primary_device_session(sessions)
        mins = _device_stat_by_field(field, user, session["start_time"], session["end_time"], "min")
        if device not in mins:
            continue
        maxs = _device_stat_by_field(field, user, session["start_time"], session["end_time"], "max")
        medians = _device_stat_by_field(field, user, session["start_time"], session["end_time"], "median")
        means = _device_stat_by_field(field, user, session["start_time"], session["end_time"], "mean")
        result.append({
            "date": wake_date.strftime("%Y-%m-%d"),
            "device": device,
            "min": round(mins[device], 1),
            "max": round(maxs[device], 1),
            "median": round(medians[device], 1) if device in medians else None,
            "mean": round(means[device], 1) if device in means else None,
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

def _sleep_stage_timeline(user: str, session_start: datetime, session_end: datetime, device: str | None = None) -> list[dict]:
    ''' Ordered, individual stage SEGMENTS during one sleep session -
    NOT the aggregated per-stage-type minute totals
    get_sleep_stage_breakdown() returns. This is what a true hypnogram
    visualization needs (Zepp's own "horizontal timeline of colored
    blocks across the night" - see UI_DESIGN_NOTES.md's "Sleep tab"
    entry, which confirms this is exactly what this app's own stage
    extraction already produces) - the CHRONOLOGICAL SEQUENCE of which
    stage was active when, not just how many total minutes were spent
    in each stage type overall.

    Reads the same sleep_stage_duration_s points get_sleep_stage_breakdown()
    sums, but keeps each one as its own row instead of grouping+summing
    by stage - same underlying data, different shape for a different
    purpose. Same optional device-filtering as that function, same
    reasoning (see its own docstring) - and doubly important here,
    since segments from two different devices sorted together purely
    by time would interleave into a nonsensical, non-chronological-per-
    device timeline, not just double-count a total the way an
    unfiltered sum would.

    Returns a chronologically-sorted list of {"stage": str,
    "start": <ISO8601>, "duration_min": int}.
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
      {device_filter}|> sort(columns: ["_time"])
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query sleep stage timeline for user={user}: {e}")
        return []

    segments = []
    for table in tables:
        for record in table.records:
            stage = record.values.get("sleep_stage")
            value = record.get_value()
            t = record.get_time()
            if stage is not None and value is not None and t is not None:
                segments.append({
                    "stage": stage,
                    "start": t.isoformat(),
                    "duration_min": round(value / 60),
                })
    segments.sort(key=lambda s: s["start"])
    return segments

def get_sleep_hypnogram_for_night(user: str, wake_date: date) -> list[dict]:
    ''' The Sleep tab's hypnogram data for one specific night - resolves
    that night's actual session window (same primary-device selection
    as the rest of this Sleep-tab family, see _primary_device_session())
    and returns _sleep_stage_timeline()'s ordered segment list for it.

    Returns an empty list (not an error) if no sleep session is
    recorded for this night.
    '''
    sessions = _sleep_session_for_night(user, wake_date)
    if not sessions:
        return []
    device, session = _primary_device_session(sessions)
    return _sleep_stage_timeline(user, session["start_time"], session["end_time"], device=device)

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

def get_sleep_stage_breakdown(user: str, session_start: datetime, session_end: datetime, device: str | None = None) -> dict[str, int]:
    ''' Minutes spent in each sleep stage (light/deep/rem/awake) for one
    specific sleep session, identified by its own [start, end) window -
    NOT a lookback query, the caller (typically find_last_completed_
    sleep_session's result) already knows exactly which session.

    Sums sleep_stage_duration_s (already extracted per stage segment,
    see parser/amazfit and parser/colmi's sleep stage extraction)
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

def count_wake_events(user: str, session_start: datetime, session_end: datetime, device: str | None = None, min_duration_s: int | None = None) -> int:
    ''' Count of distinct awake-stage SEGMENTS during one sleep session -
    genuinely different information from get_sleep_stage_breakdown()'s
    duration sum: three separate 2-minute wake-ups and one continuous
    6-minute wake-up both sum to 6 minutes of total awake time, but
    are very different sleep-quality signals. Matches Zepp's own
    "Awake ... N wake events" framing on the Sleep Metrics card (see
    UI_DESIGN_NOTES.md).

    Counts the sleep_stage_duration_s field specifically - written
    ONCE per stage segment, at that segment's own start point (see
    parser/amazfit's sleep-stage extraction) - not sleep_stage_active
    (written TWICE per segment, a start=1 marker and an end=0 marker
    one second before the next stage begins, which would double the
    count if used here instead).

    Same optional device-filtering as get_sleep_stage_breakdown, same
    reasoning: without it, this would silently sum wake-event counts
    across multiple devices with stage data for the same night rather
    than counting one device's own.

    `min_duration_s`, if given, only counts segments AT LEAST this
    long - added for the literature-backed Sleep Quality panel's
    "awakenings >5 minutes" metric (Ohayon et al. 2017's own
    definition specifically excludes brief stirrings under 5 minutes,
    not any detected awake segment). Default None preserves the
    original "every awake segment, any length" behavior every existing
    caller (the Sleep Metrics card's own wake-event count) still wants.
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = session_start.astimezone(timezone.utc).isoformat()
    stop_iso = session_end.astimezone(timezone.utc).isoformat()

    device_filter = f'|> filter(fn: (r) => r.device == "{device}")\n      ' if device else ""
    duration_filter = f'|> filter(fn: (r) => r._value >= {min_duration_s})\n      ' if min_duration_s else ""

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.sample_type == "sleep_stage")
      |> filter(fn: (r) => r.sleep_stage == "awake")
      |> filter(fn: (r) => r._field == "sleep_stage_duration_s")
      {device_filter}{duration_filter}|> count()
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

def get_sleep_quality_indicators(user: str, session_start: datetime, session_end: datetime, duration_s: int, awake_minutes: int, device: str | None = None) -> dict:
    ''' The literature-backed Sleep Quality panel's 3 computable metrics
    (sleep efficiency, WASO, awakenings >5min), each compared against
    Ohayon et al. 2017's published "appropriate" thresholds for the
    configured age bracket - see SLEEP_QUALITY_THRESHOLDS' own
    docstring in config.py for full sourcing and for why this is 3
    metrics shown individually rather than one blended Zepp-style
    score.

    Takes duration_s and awake_minutes as already-known inputs (from
    get_sleep_overview_for_night's own session/stage data) rather than
    re-querying them - this function's own job is the one new query
    it actually needs (the awakenings>5min count) plus the threshold
    comparison arithmetic, not re-deriving data the caller already has.

    Sleep efficiency here is duration-based ((duration_s - awake
    seconds) / duration_s), using session duration as a "time in bed"
    proxy - NOT the strict clinical definition, which uses true time
    in bed (including any pre-sleep-onset period) as the denominator.
    This app has no separately-tracked "got into bed" timestamp to use
    instead - stated here so this isn't silently conflated with a
    clinical-grade efficiency figure if ever compared against one.

    Returns a dict with each metric's raw value, whether it meets the
    published "appropriate" threshold, and (efficiency only, the one
    metric with an independently-corroborated "poor" bound too - see
    config.py) whether it falls below the "poor" cutoff. Also returns
    the age_bracket used and a source citation string, so the frontend
    can display these thresholds as genuinely attributed, not just
    asserted.
    '''
    thresholds = SLEEP_QUALITY_THRESHOLDS.get(SLEEP_QUALITY_AGE_BRACKET, SLEEP_QUALITY_THRESHOLDS["adult"])

    efficiency_pct = round((duration_s - awake_minutes * 60) / duration_s * 100, 1) if duration_s else None
    awakenings_5min = count_wake_events(user, session_start, session_end, device=device, min_duration_s=300)

    return {
        "efficiency_pct": efficiency_pct,
        "efficiency_meets_threshold": (
            efficiency_pct >= thresholds["efficiency_appropriate_min_pct"] if efficiency_pct is not None else None
        ),
        "efficiency_poor": (
            efficiency_pct < thresholds["efficiency_poor_max_pct"] if efficiency_pct is not None else None
        ),
        "waso_min": awake_minutes,
        "waso_meets_threshold": awake_minutes <= thresholds["waso_appropriate_max_min"],
        "awakenings_5min": awakenings_5min,
        "awakenings_meets_threshold": awakenings_5min <= thresholds["awakenings_appropriate_max"],
        "age_bracket": SLEEP_QUALITY_AGE_BRACKET,
        "source": "Ohayon et al. 2017, National Sleep Foundation Sleep Quality Consensus Panel (Sleep Health 3(1):6-19)",
    }

def get_sleep_duration_recommendation(duration_s: int) -> dict:
    ''' Compares a night's total duration against the age-bracketed NSF-
    recommended range (SLEEP_DURATION_RECOMMENDED_HOURS in config.py) -
    a DIFFERENT NSF publication than get_sleep_quality_indicators()'s
    sleep-continuity metrics (Hirshkowitz et al. 2015, not Ohayon et
    al. 2017 - see config.py's own comment for the full citation), but
    the same "individually cited, not a blended score" spirit: a
    literature-backed range comparison for the Sleep Duration detail
    page's qualitative label, not an attempt to reproduce Zepp's own
    "Good"/"Fair" tiering.

    Deliberately a simple below/within/above comparison, not a multi-
    tier gauge - the source publishes one recommended range per age
    bracket, not finer "may be appropriate" sub-bands, so that's the
    honest amount of precision to claim here.

    Uses the same SLEEP_QUALITY_AGE_BRACKET config as the Sleep
    Quality panel, for one consistent age-bracket setting across the
    whole Sleep section rather than two independently-configured ones.
    '''
    bracket = SLEEP_QUALITY_AGE_BRACKET
    rec = SLEEP_DURATION_RECOMMENDED_HOURS.get(bracket, SLEEP_DURATION_RECOMMENDED_HOURS["adult"])
    hours = round(duration_s / 3600, 1)
    return {
        "hours": hours,
        "meets_recommendation": rec["min"] <= hours <= rec["max"],
        "below": hours < rec["min"],
        "above": hours > rec["max"],
        "range_min_hours": rec["min"],
        "range_max_hours": rec["max"],
        "age_bracket": bracket,
        "source": "Hirshkowitz et al. 2015, National Sleep Foundation (reaffirmed 2026)",
    }
