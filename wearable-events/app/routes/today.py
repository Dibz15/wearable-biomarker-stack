""" /today, /today/series/{field} - the Today tab's own summary + per-field chart data. """

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.queries.activity import get_today_steps
from app.queries.sleep_sessions import find_last_completed_sleep_session, get_sleep_stage_breakdown
from app.queries.vitals import get_today_series, get_today_vitals, local_today_bounds
from app.routes._shared import TODAY_SERIES_FIELDS, _parse_optional_date

router = APIRouter()


# --- today dashboard ---

@router.get("/today")
def get_today(date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Read-only summary for the "Today" tab: vitals (per device, for
    whichever fields reported anything that day), that day's step
    total, and the most recently completed sleep session as of that
    day (duration + stage breakdown) if a qualifying one exists.

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

    # Always bounded at the END of the requested day (midnight going
    # into the next one), rather than only doing this for a past date
    # and leaving today's own case as an open-ended "now" - the two are
    # provably equivalent for today specifically (there's no future
    # sleep data to find either way), so one code path handles both
    # rather than branching on whether a date was given.
    _, before = local_today_bounds(parsed_date)

    sleep = None
    session = find_last_completed_sleep_session(username, before=before)
    if session is not None:
        stages = get_sleep_stage_breakdown(
            username,
            session["start_time"],
            session["start_time"] + timedelta(seconds=session["duration_s"]),
            device=session.get("device"),
        )
        sleep = {
            "sleep_date": session["sleep_date"],
            "start_time": session["start_time"].isoformat(),
            "duration_s": session["duration_s"],
            "stages_min": stages,
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