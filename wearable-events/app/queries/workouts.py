"""Workout summaries, per-sample detail series, laps, activity intensity."""
from datetime import datetime, time, timedelta, timezone

from loguru import logger

from app.config import INFLUX_BUCKET, SENSOR_MEASUREMENT
from app.queries.client import get_client
from app.queries.vitals import _grouped_series

TRAINING_EFFECT_SCALE = [
    (0.9, "No effect"),
    (1.9, "Minor effect"),
    (2.9, "Maintaining effect"),
    (3.9, "Improving effect"),
    (4.9, "Highly improving effect"),
    (5.0, "Overreaching effect"),
]

HR_ZONE_DISPLAY_NAMES = {
    "na": "N/A",
    "warm_up": "Active Recovery",
    "fat_burn": "Efficient Fat Burning",
    "aerobic": "Aerobic Endurance",
    "anaerobic": "Lactate Threshold",
    "extreme": "Anaerobic Endurance",
}

HR_ZONE_ORDER = ["na", "warm_up", "fat_burn", "aerobic", "anaerobic", "extreme"]

def get_training_effect_label(value: float | None) -> dict | None:
    ''' Firstbeat's own 0-5 Training Effect scale (see
    TRAINING_EFFECT_SCALE's own comment for the citation and the real
    cross-checks against this app's own data) - returns the matching
    label alongside the raw value, or None if value itself is None
    (a workout with no HasField("trainingEffect") at all in its own
    RAW_SUMMARY_DATA, not the same as a genuine 0.0).
    '''
    if value is None:
        return None
    for upper_bound, label in TRAINING_EFFECT_SCALE:
        if value <= upper_bound:
            return {"value": value, "label": label, "source": "Firstbeat Technologies EPOC/Training Effect scale"}
    return {"value": value, "label": TRAINING_EFFECT_SCALE[-1][1], "source": "Firstbeat Technologies EPOC/Training Effect scale"}


# The person's own real watch setting (Zepp: Settings -> interval type
# -> "lactate threshold heart rate zone") - confirmed to change BOTH
# the zone names AND the actual BPM thresholds.
# This naming is a deliberate choice matching what the person actually
# sees on their own watch/app, not Gadgetbridge's own generic internal
# names (Warm-Up/Fat Burn/Aerobic/Anaerobic/Extreme) - see
# UI_DESIGN_NOTES.md's own zone-naming table for the full mapping
# between the two.
HR_ZONE_DISPLAY_NAMES = {
    "na": "N/A",
    "warm_up": "Active Recovery",
    "fat_burn": "Efficient Fat Burning",
    "aerobic": "Aerobic Endurance",
    "anaerobic": "Lactate Threshold",
    "extreme": "Anaerobic Endurance",
}
HR_ZONE_ORDER = ["na", "warm_up", "fat_burn", "aerobic", "anaerobic", "extreme"]

def get_workout_summary_detail(user: str, start_ms: int) -> dict | None:
    ''' The full field set for ONE specific workout's own
    sample_type="activity_summary" point (written by the parser's
    extract_base_activity_summary_rows/flatten_workout_summary), keyed
    by its own start time in epoch milliseconds - the same value
    already returned as `start` (as an ISO string) by
    get_combined_activity_sessions()/get_precomputed_activity_sessions(),
    and the same raw value the parser's own `workout_start_time` tag on
    workout_detail/workout_lap points is derived from (both are the
    exact same BASE_ACTIVITY_SUMMARY.START_TIME value, just represented
    differently - one as this point's own InfluxDB timestamp, one as a
    string tag on the SEPARATE per-sample/per-lap points) - see
    get_workout_detail_series()'s own docstring for how that
    correlation is used to fetch those separately.

    A tight (+/- 1 second) window around the exact millisecond is used
    rather than an exact-instant match, since real-world clock/
    precision differences between how a timestamp was originally
    constructed and how it's now being queried back are a more robust
    assumption than expecting bit-exact equality - workouts are
    naturally spaced apart in real time by more than a second, so this
    window is not expected to ever match more than one point.

    Returns None if no matching point exists at all (a bad/stale
    identifier, or the workout was somehow removed) - the caller should
    treat this as a 404, not silently render an empty page.
    '''
    client = get_client()
    query_api = client.query_api()

    center = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    start_iso = (center - timedelta(seconds=1)).isoformat()
    stop_iso = (center + timedelta(seconds=1)).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.sample_type == "activity_summary")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.error(f"Failed to query workout summary detail for user={user}, start_ms={start_ms}: {e}")
        return None

    for table in tables:
        for record in table.records:
            values = record.values
            name = values.get("name")
            has_real_name = name is not None and name != "Unset"

            zones = []
            for zone_key in HR_ZONE_ORDER:
                seconds = values.get(f"hr_zone_{zone_key}_seconds")
                max_bpm = values.get(f"hr_zone_{zone_key}_max_bpm")
                if seconds is None and max_bpm is None:
                    continue
                zones.append({
                    "key": zone_key,
                    "display_name": HR_ZONE_DISPLAY_NAMES[zone_key],
                    "seconds": seconds,
                    "max_bpm": max_bpm,
                })

            return {
                "start": record.get_time().isoformat(),
                "device": values.get("device"),
                "name": name if has_real_name else None,
                "activity_kind_summary": values.get("activity_kind_summary"),
                "duration_s": values.get("duration_s"),
                "active_seconds": values.get("active_seconds"),
                "calories_kcal": values.get("calories_kcal"),
                "hr_avg": values.get("hr_avg"),
                "hr_max": values.get("hr_max"),
                "hr_min": values.get("hr_min"),
                "training_load": values.get("training_load"),
                "steps": values.get("steps"),
                "distance_m": values.get("distance_m"),
                "avg_speed_mps": values.get("avg_speed_mps"),
                "max_speed_mps": values.get("max_speed_mps"),
                "avg_cadence_per_min": values.get("avg_cadence_per_min"),
                "max_cadence_per_min": values.get("max_cadence_per_min"),
                "altitude_avg_m": values.get("altitude_avg_m"),
                "altitude_min_m": values.get("altitude_min_m"),
                "altitude_max_m": values.get("altitude_max_m"),
                "elevation_gain_m": values.get("elevation_gain_m"),
                "elevation_loss_m": values.get("elevation_loss_m"),
                "ascent_seconds": values.get("ascent_seconds"),
                "descent_seconds": values.get("descent_seconds"),
                "avg_stride_cm": values.get("avg_stride_cm"),
                "aerobic_training_effect": get_training_effect_label(values.get("aerobic_training_effect")),
                "anaerobic_training_effect": get_training_effect_label(values.get("anaerobic_training_effect")),
                "hr_zones": zones,
            }
    return None

WORKOUT_LAP_FIELDS = [
    "lap_number", "avg_hr", "max_hr", "avg_cadence_rpm", "max_cadence_rpm",
    "distance_m", "calories_kcal", "duration_s", "avg_speed_mps", "max_speed_mps",
    "ascent_m", "descent_m",
]

def get_workout_detail_series(user: str, start_ms: int, fields: list[str]) -> list[dict]:
    ''' Per-sample points (sample_type="workout_detail", written by the
    parser's extract_workout_detail_points from a real FIT/GPX export)
    for ONE specific workout,
    correlated by its own `workout_start_time` tag - the SAME raw
    BASE_ACTIVITY_SUMMARY.START_TIME value get_workout_summary_detail()
    is keyed by, just carried as a string tag on these separate points
    rather than as their own timestamp (each workout_detail point's own
    timestamp is instead when THAT SPECIFIC SAMPLE was taken).

    Returns an EMPTY list (not None) when no per-sample export exists
    for this workout - a real, expected, and already-documented case
    (no FIT/GPX automation enabled at all, or the workout predates
    enabling it), not an error - the caller should treat this as "no
    chart to show", falling back to the summary-only fields already
    returned by get_workout_summary_detail().

    Only the given `fields` are requested/returned per point (e.g.
    ["hr"] for a simple HR-over-time chart) - a real sample may not
    carry every requested field (see flatten_fit_records's own
    docstring on this), so a point's own dict here may have some keys
    missing, not zeroed.
    '''
    client = get_client()
    query_api = client.query_api()
    field_filter = " or ".join(f'r._field == "{f}"' for f in fields)

    # Bounded around start_ms itself (generous +24h to comfortably
    # cover even a very long workout) rather than a blanket lookback -
    # the workout's own start time is already known here, no need to
    # scan further than that to find its own samples.
    center = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    start_iso = (center - timedelta(hours=1)).isoformat()
    stop_iso = (center + timedelta(hours=24)).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.sample_type == "workout_detail")
      |> filter(fn: (r) => r.workout_start_time == "{start_ms}")
      |> filter(fn: (r) => {field_filter})
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> sort(columns: ["_time"])
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query workout detail series for user={user}, start_ms={start_ms}: {e}")
        return []

    results = []
    for table in tables:
        for record in table.records:
            point = {"time": record.get_time().isoformat()}
            for f in fields:
                if f in record.values:
                    point[f] = record.values[f]
            results.append(point)
    return results


# Every field flatten_fit_laps() (parser/activefit) can possibly write
# - a fixed allowlist rather than pivoting and returning whatever keys
# happen to appear, so the shape of what this function returns doesn't
# silently change if the parser's own field set ever changes; any
# genuinely new field would need adding here deliberately, matching
# this codebase's own general "known fields, not whatever shows up"
# convention.
WORKOUT_LAP_FIELDS = [
    "lap_number", "avg_hr", "max_hr", "avg_cadence_rpm", "max_cadence_rpm",
    "distance_m", "calories_kcal", "duration_s", "avg_speed_mps", "max_speed_mps",
    "ascent_m", "descent_m",
]

def get_workout_laps(user: str, start_ms: int) -> list[dict]:
    ''' Per-lap summaries (sample_type="workout_lap", written by the
    parser's flatten_fit_laps) for one workout, correlated the
    same way get_workout_detail_series()
    is (the shared workout_start_time tag). FIT's own "lap" message
    type only exists for FIT exports specifically - a GPX-sourced
    workout (see extract_workout_detail_points's own source-priority
    reasoning) has no lap concept at all, so this returns an empty list
    for those, same "empty, not an error" convention as
    get_workout_detail_series().

    Sorted by each lap's own `lap_number` (1-indexed, matching both
    Zepp's and Gadgetbridge's own real "No. 1, 2, ..." lap numbering) -
    NOT by _time, since a lap's own point uses its start_time as its
    timestamp, and while these should always agree in practice, sorting
    by the semantically real ordering field is more robust than relying
    on that coincidence.
    '''
    client = get_client()
    query_api = client.query_api()

    center = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    start_iso = (center - timedelta(hours=1)).isoformat()
    stop_iso = (center + timedelta(hours=24)).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.sample_type == "workout_lap")
      |> filter(fn: (r) => r.workout_start_time == "{start_ms}")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query workout laps for user={user}, start_ms={start_ms}: {e}")
        return []

    laps = []
    for table in tables:
        for record in table.records:
            lap = {}
            for f in WORKOUT_LAP_FIELDS:
                if f in record.values:
                    lap[f] = record.values[f]
            if lap:
                laps.append(lap)
    laps.sort(key=lambda l: l.get("lap_number", 0))
    return laps

def get_workout_raw_intensity(user: str, start_ms: int) -> dict[str, list[dict]]:
    ''' raw_intensity readings during one specific workout's own real
    time window - the same continuous background activity-monitoring
    stream the Activity page's own day-view intensity chart reads
    (_grouped_series, shared with get_today_series()), just scoped to
    a workout's own start/duration instead of a full calendar day.

    Unlike get_workout_detail_series() (which correlates by the
    workout_start_time TAG present on GPX/FIT-derived per-sample
    points), raw_intensity isn't workout-specific data at all - it's
    the watch's own always-on monitoring, with no such tag - so this
    needs the workout's own real start/end TIMES to scope a plain
    range query instead. Looked up via get_workout_summary_detail()
    itself (start + duration_s), not requested as a parameter, so the
    caller doesn't need to already know or separately fetch those.

    Returns {} (not an error) if the workout itself can't be found -
    the caller should treat this the same as "no intensity data for
    this workout", not surface a separate failure mode.
    '''
    summary = get_workout_summary_detail(user, start_ms)
    if summary is None or summary.get("duration_s") is None:
        return {}
    start_dt = datetime.fromisoformat(summary["start"])
    end_dt = start_dt + timedelta(seconds=summary["duration_s"])
    return _grouped_series("raw_intensity", user, start_dt, end_dt)


# Excluded from "sitting" time even though their intensity is
# typically low too (sleep's median intensity was 0, charging's was
# also near-zero, confirmed against real data) - counting them would
# silently fold hours of sleep
# or a charging watch into a "sitting time" figure, which isn't what
# this feature is for. not_worn is excluded for the same reason: time
# the watch wasn't being worn isn't time the person was sitting,
# whatever the intensity reading happens to be during it.
SITTING_EXCLUDED_LABELS = {"sleep", "not_worn", "charging"}
