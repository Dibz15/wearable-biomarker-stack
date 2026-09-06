""" /vitals/* - HR/HRV/stress/SpO2/temperature range, rolling-mean, baseline, differential, manual readings. """

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.config import TZ_NAME
from app.queries.sleep_sessions import get_nightly_baseline_comparison, get_nightly_differential_series
from app.queries.vitals import get_baseline_comparison, get_manual_readings, get_period_range_series, get_rolling_mean_series
from app.routes._shared import BASELINE_ALLOWED_DAYS, RANGE_PERIODS, TODAY_SERIES_FIELDS, _parse_optional_date, _period_bounds

router = APIRouter()


@router.get("/vitals/range/{field}")
def get_vitals_range(field: str, period: str, end_date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Per-device min/max/median range bars for one field, for the
    W/M/Y tabs on a detail view - one entry per day (week/month) or
    per month (year), as opposed to /today/series's raw per-point
    series that only makes sense zoomed into a single day. The window
    ends on `end_date` (today by default) - the detail-view's
    back/forward navigation shifts this by a whole period at a time.
    '''
    if field not in TODAY_SERIES_FIELDS:
        raise HTTPException(400, f"unsupported field: {field!r}")
    if period not in RANGE_PERIODS:
        raise HTTPException(400, f"unsupported period: {period!r} (must be one of {sorted(RANGE_PERIODS)})")

    spec = RANGE_PERIODS[period]
    start, end = _period_bounds(period, end_date)
    return get_period_range_series(field, current_user["username"], start, end, spec["window"])

@router.get("/vitals/rolling-mean/{field}")
def get_vitals_rolling_mean(field: str, period: str, end_date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' 7-day rolling mean overlay line for the W/M range-bar charts -
    see get_rolling_mean_series() for why this only applies to
    daily-bucketed periods. Year (monthly-bucketed) isn't supported
    here - a "7-day" mean doesn't map onto monthly bars, so the
    frontend simply doesn't request this overlay for that period.
    '''
    if field not in TODAY_SERIES_FIELDS:
        raise HTTPException(400, f"unsupported field: {field!r}")
    if period not in ("week", "month"):
        raise HTTPException(400, f"unsupported period for a rolling mean: {period!r} (must be 'week' or 'month')")

    start, end = _period_bounds(period, end_date)
    return get_rolling_mean_series(field, current_user["username"], start, end, window_days=7)

# Same night-anchored baseline as HRV, for the same reason: "today's
# SpO2"/"today's temperature" conventionally means last night's
# reading, and the overnight value is the one actually worth tracking
# drift on (a daytime SpO2 reading only happens when the wearer is
# already still, so it's sparse and less representative than the
# night's readings anyway; daytime temperature swings with activity,
# meals, and environment enough that only the overnight reading is a
# stable enough baseline to be worth comparing against).
NIGHTLY_BASELINE_FIELDS = {"hrv", "spo2", "temperature"}

@router.get("/vitals/baseline/{field}")
def get_vitals_baseline(field: str, days: int = 7, date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' One day's value (today by default, or `date` for the
    detail-view's day-navigation) vs. a trailing baseline (mean +
    stddev of the `days` days before that day) for one field, per
    device - what powers the comparison bar. Devices without enough
    history yet are simply absent from the response (not an error) -
    the caller should render that as an "insufficient data" state.

    HRV uses a night-anchored baseline (get_nightly_baseline_comparison) -
    "today's HRV" conventionally means last night's mean, not a
    calendar-day average, and that's the field-specific fact that
    decides which comparison function applies, not something the
    caller needs to specify.
    '''
    if field not in TODAY_SERIES_FIELDS:
        raise HTTPException(400, f"unsupported field: {field!r}")
    if days not in BASELINE_ALLOWED_DAYS:
        raise HTTPException(400, f"unsupported days: {days!r} (must be one of {sorted(BASELINE_ALLOWED_DAYS)})")
    parsed_date = _parse_optional_date(date)
    if field in NIGHTLY_BASELINE_FIELDS:
        return get_nightly_baseline_comparison(field, current_user["username"], baseline_days=days, for_date=parsed_date)
    return get_baseline_comparison(field, current_user["username"], baseline_days=days, for_date=parsed_date)

@router.get("/vitals/differential/{field}")
def get_vitals_differential(field: str, period: str, end_date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Per-night delta from a trailing 7-night baseline, across a week
    or month - the TREND chart for a night-anchored field (temperature
    so far), as opposed to /vitals/baseline's single today-vs-baseline
    comparison. Day isn't offered here on purpose - that's exactly the
    single-point comparison /vitals/baseline already gives, not a
    series.

    Only offered for fields that actually have a nightly-baseline
    concept in the first place (NIGHTLY_BASELINE_FIELDS) - a plain
    calendar-day field like resting_heart_rate has no per-night value
    this would even be a delta FROM.
    '''
    if field not in NIGHTLY_BASELINE_FIELDS:
        raise HTTPException(400, f"unsupported field for a nightly differential: {field!r}")
    if period not in ("week", "month"):
        raise HTTPException(400, f"unsupported period: {period!r} (must be 'week' or 'month')")

    spec = RANGE_PERIODS[period]
    anchor = _parse_optional_date(end_date) or datetime.now(ZoneInfo(TZ_NAME)).date()
    end_date_obj = anchor + timedelta(days=1)  # exclusive - include all of the anchor day
    start_date_obj = end_date_obj - timedelta(days=spec["days"])

    return get_nightly_differential_series(field, current_user["username"], start_date_obj, end_date_obj, baseline_days=7)

# Fields confirmed to carry a `{field}_type_num` tag distinguishing
# manual from automatic readings (0=manual, 1=automatic - confirmed
# for BOTH fields independently via a deliberate cross-check, not
# assumed to carry over from one to the other). Enforced here rather than
# trusting the path parameter, same reasoning as every other allowlist
# in this file - an unsupported field would otherwise just silently
# return no rows (the tag filter never matches), a far less obvious
# failure than a 400.
MANUAL_TYPE_NUM_FIELDS = {"stress", "spo2"}

@router.get("/vitals/manual-readings/{field}")
def get_vitals_manual_readings(field: str, period: str, end_date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Manually-triggered readings only, for one field, over a D/W/M/Y
    period - Zepp's own Stress page "Manual Data" list for the Day
    view; used as just a count (not the full list) for Week/Month/Year's
    "Single Stress Measurement: N time(s)" style extra stat.
    '''
    if field not in MANUAL_TYPE_NUM_FIELDS:
        raise HTTPException(400, f"unsupported field for manual-reading filtering: {field!r}")
    if period not in RANGE_PERIODS:
        raise HTTPException(400, f"unsupported period: {period!r} (must be one of {sorted(RANGE_PERIODS)})")

    start, end = _period_bounds(period, end_date)
    return get_manual_readings(field, current_user["username"], start, end)
