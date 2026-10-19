""" /today, /today/series/{field} - the Today tab's own summary + per-field chart data. """

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.config import TZ_NAME
from app.queries.activity import get_today_steps
from app.queries.sleep_sessions import get_sleep_stage_breakdown, primary_sleep_session_for_night
from app.queries.vitals import get_today_series, get_today_vitals
from app.routes._shared import TODAY_SERIES_FIELDS, _parse_optional_date

router = APIRouter()


# --- today dashboard ---

@router.get("/today")
def get_today(date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Read-only summary for the "Today" tab: vitals (per device, for
    whichever fields reported anything that day), that day's step
    total, and the sleep session that ended (woke up) on that day
    (duration + stage breakdown), if one was recorded.

    The sleep session is resolved by wake date, the same rule the Sleep
    tab uses (primary_sleep_session_for_night), and labelled by that
    wake date, as the Sleep tab does. It
    used to be "the most recent session before the end of the day",
    which showed the previous night's sleep as last night's whenever
    last night's hadn't synced yet (#8).

    `date` (YYYY-MM-DD) is optional and defaults to today - the Today
    tab's own date navigation (prev/next/date-picker, mirroring the
    detail views) uses this to show a past day's summary instead.

    Deliberately a single combined endpoint rather than one call per
    card - the frontend renders this as one dashboard, so one round
    trip on tab load is simpler than several racing fetches, and the
    underlying InfluxDB queries are already independent/parallelizable
    work happening server-side regardless of how many HTTP calls the
    client makes.
    '''
    username = current_user["username"]
    parsed_date = _parse_optional_date(date)

    vitals = get_today_vitals(username, for_date=parsed_date)
    steps = get_today_steps(username, for_date=parsed_date)

    wake_date = parsed_date or datetime.now(ZoneInfo(TZ_NAME)).date()

    sleep = None
    primary = primary_sleep_session_for_night(username, wake_date)
    if primary is not None:
        device, session = primary
        start, end = session["start_time"], session["end_time"]
        sleep = {
            "wake_date": wake_date.isoformat(),
            "start_time": start.isoformat(),
            "duration_s": session["duration_s"],
            "stages_min": get_sleep_stage_breakdown(username, start, end, device=device),
        }

    return {
        "vitals": vitals,
        "steps": steps,
        "sleep": sleep,
    }

@router.get("/today/series/{field}")
def get_today_series_endpoint(field: str, date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Raw per-point time series for one field, for one day (today by
    default, or `date` for the detail-view's day-navigation) - what a
    detail view's chart plots, as distinct from /today's reduced
    summary stats. One entry per device that reported anything.
    '''
    if field not in TODAY_SERIES_FIELDS:
        raise HTTPException(400, f"unsupported field: {field!r}")
    return get_today_series(field, current_user["username"], _parse_optional_date(date))