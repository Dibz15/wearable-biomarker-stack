""" /activity/* - sessions, workout detail/samples/laps/intensity, sitting/standing, time-range chart. """

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.queries.activity import (
    get_activity_time_range_series,
    get_combined_activity_sessions,
    get_hourly_activity_breakdown,
    get_sitting_minutes,
    get_stood_hours,
)
from app.queries.workouts import (
    get_workout_detail_series,
    get_workout_laps,
    get_workout_raw_intensity,
    get_workout_summary_detail,
)
from app.routes._shared import RANGE_PERIODS, _parse_optional_date, _period_bounds

router = APIRouter()


# --- activity page ---

@router.get("/activity/sessions")
def get_activity_sessions_endpoint(date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Combined activity session list for one day (both automatic
    sessions derived from raw per-minute data, and pre-computed
    workout entries from BASE_ACTIVITY_SUMMARY) - the Activity page's
    session list. `date` defaults to today, same convention as /today.
    '''
    parsed_date = _parse_optional_date(date)
    return get_combined_activity_sessions(current_user["username"], for_date=parsed_date)

@router.get("/activity/workout/{start_ms}")
def get_workout_detail_endpoint(start_ms: int, current_user: dict = Depends(get_current_user)):
    ''' Full detail for ONE specific precomputed workout (a
    BASE_ACTIVITY_SUMMARY entry, i.e. a "source": "precomputed" row
    from /activity/sessions - NOT a "derived" one, which has no
    corresponding summary point to look up at all) - the Workout
    Detail page's own entry point. `start_ms` is that entry's own
    start time in epoch milliseconds - see get_workout_summary_detail's
    own docstring for exactly how that identifies the right point.

    404s if no matching workout exists at all (a stale/bad start_ms),
    rather than returning an empty-but-200 body a frontend might
    render as a blank page without explanation.
    '''
    detail = get_workout_summary_detail(current_user["username"], start_ms)
    if detail is None:
        raise HTTPException(404, f"no workout found for start_ms={start_ms}")
    return detail

@router.get("/activity/workout/{start_ms}/samples")
def get_workout_samples_endpoint(start_ms: int, current_user: dict = Depends(get_current_user)):
    ''' Every per-sample field the Workout Detail page's own charts
    need (HR, cadence, distance, altitude, speed, GPS position, step
    length) for one specific workout, fetched in a SINGLE call rather
    than one request per chart - see get_workout_detail_series's own
    docstring for the full reasoning on what this data is and when
    it's genuinely absent. Returns an empty list (not a 404) when no
    FIT/GPX export exists for this workout - a real, expected, already-
    documented case, not an error; the page should just skip whichever
    charts have nothing to show, not treat this as missing data the
    way a 404 on the summary endpoint above would be.
    '''
    fields = ["hr", "cadence_rpm", "distance_m", "altitude_m", "speed_mps",
              "latitude", "longitude", "step_length_mm"]
    return get_workout_detail_series(current_user["username"], start_ms, fields)

@router.get("/activity/workout/{start_ms}/laps")
def get_workout_laps_endpoint(start_ms: int, current_user: dict = Depends(get_current_user)):
    ''' Per-lap summaries for one specific workout, for the Workout
    Detail page's own Lap Details table - see get_workout_laps's own
    docstring. Returns an empty list (not a 404) when the workout's own
    export has no lap data at all - either a GPX-sourced workout (no
    lap concept in GPX at all) or, less commonly, a FIT export that
    genuinely recorded none.
    '''
    return get_workout_laps(current_user["username"], start_ms)

@router.get("/activity/workout/{start_ms}/raw-intensity")
def get_workout_raw_intensity_endpoint(start_ms: int, current_user: dict = Depends(get_current_user)):
    ''' raw_intensity readings scoped to one specific workout's own
    real start/duration - see get_workout_raw_intensity's own
    docstring for how this differs from the GPX/FIT-tag-correlated
    /samples endpoint above (this is the watch's own always-on
    background stream, not workout-specific data, so it's scoped by
    real time window instead of a shared tag). Returns {} (not a 404)
    when the workout itself can't be found or has no duration -
    treated as "no intensity data", not a separate error case.
    '''
    return get_workout_raw_intensity(current_user["username"], start_ms)

@router.get("/activity/sitting-minutes")
def get_sitting_minutes_endpoint(date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Cumulative "sitting" minutes per device for one day (low-
    intensity minutes, excluding sleep/not-worn/charging - see
    get_sitting_minutes's own docstring for the full reasoning).
    `date` defaults to today, same convention as /today.
    '''
    parsed_date = _parse_optional_date(date)
    return get_sitting_minutes(current_user["username"], for_date=parsed_date)

@router.get("/activity/stood-hours")
def get_stood_hours_endpoint(date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Count of hours today where activity intensity crossed the
    confirmed Stand threshold at some point, per device - see
    get_stood_hours's own docstring for the full reasoning (including
    why this buckets by local hour, not UTC). `date` defaults to
    today, same convention as /today.
    '''
    parsed_date = _parse_optional_date(date)
    return get_stood_hours(current_user["username"], for_date=parsed_date)

@router.get("/activity/hourly-breakdown")
def get_hourly_activity_breakdown_endpoint(date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Per-hour sitting/active/excluded minute breakdown for one day,
    per device - the Activity page's day-view sitting-vs-standing
    chart. See get_hourly_activity_breakdown's own docstring. `date`
    defaults to today, same convention as /today.
    '''
    parsed_date = _parse_optional_date(date)
    return get_hourly_activity_breakdown(current_user["username"], for_date=parsed_date)

@router.get("/activity/time-range")
def get_activity_time_range_endpoint(period: str, end_date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Per-day (week/month) or per-month (year) sitting/active minute
    sums - the Activity page's Week/Month/Year "total activity time"
    chart (active_minutes alone) and "sitting vs standing" stacked bar
    chart (both fields), which share this same endpoint rather than
    each needing their own. Day isn't offered here - that's exactly
    the single-day sitting/active breakdown /activity/hourly-breakdown
    already gives, at hourly rather than daily granularity, not a
    period this endpoint's day/month bucketing would even apply to.
    '''
    if period not in ("week", "month", "year"):
        raise HTTPException(400, f"unsupported period: {period!r} (must be 'week', 'month', or 'year')")

    spec = RANGE_PERIODS[period]
    start, end = _period_bounds(period, end_date)
    return get_activity_time_range_series(current_user["username"], start, end, spec["window"])