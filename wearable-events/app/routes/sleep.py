""" /sleep/* - subjective journal entries plus every objective sleep-data endpoint. """

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.config import SLEEP_DURATION_GOAL_SECONDS, TZ_NAME
from app.queries.naps import get_nap_trend, get_naps_for_date
from app.queries.sleep_journal import (
    delete_sleep_entry,
    find_sleep_entries_in_range,
    find_sleep_entry_by_id,
    find_sleep_entry_for_wake_date,
    get_sleep_journal_rollup,
    write_sleep_entry_for_wake_date,
    write_sleep_point,
)
from app.queries.sleep_sessions import (
    get_nightly_baseline_comparison,
    get_sleep_hypnogram_for_night,
    get_sleep_overview_for_night,
    get_sleep_regularity_index,
    get_sleep_stage_trend,
    get_sleep_timing_trend,
    get_sleep_vitals_series,
    get_sleep_vitals_trend,
)
from app.routes._shared import BASELINE_ALLOWED_DAYS, _parse_optional_date, _period_bounds

router = APIRouter()


class SleepIn(BaseModel):
    score: int  # 1-5
    qualifiers: dict[str, bool] = {}
    pre_sleep_factors: dict[str, bool] = {}

class SleepUpdateIn(BaseModel):
    score: int  # 1-5
    qualifiers: dict[str, bool] = {}
    pre_sleep_factors: dict[str, bool] = {}

# --- sleep ---

@router.post("/sleep")
def post_sleep(payload: SleepIn, date: str, current_user: dict = Depends(get_current_user)):
    ''' Log a subjective sleep journal entry for the night that woke up
    on `date` - required now that each night has its own dedicated
    page (Sleep Heart Rate/Duration/etc, all date-nav-driven), rather
    than always writing against "whatever the most recently completed
    session happens to be" (the old, pre-per-night-pages behavior,
    which made it impossible to log/edit anything but the latest
    night). See write_sleep_entry_for_wake_date()'s own docstring for
    how the actual session gets resolved from this date.
    '''
    if not (1 <= payload.score <= 5):
        raise HTTPException(400, "score must be between 1 and 5")
    parsed_date = _parse_optional_date(date)
    if parsed_date is None:
        raise HTTPException(400, "date is required (YYYY-MM-DD)")

    result = write_sleep_entry_for_wake_date(
        user=current_user["username"],
        wake_date=parsed_date,
        score=payload.score,
        qualifiers=payload.qualifiers,
        pre_sleep_factors=payload.pre_sleep_factors,
        submission_ts=datetime.now(timezone.utc),
    )
    if result is None:
        raise HTTPException(
            409,
            f"No completed sleep session found for {date} - try again after your ring syncs "
            "(a session needs a recorded wake-up time and be long enough to not look like a nap). "
            "If this persists, check that your account username matches the GADGETBRIDGE_USER "
            "value configured for your ring parser instance."
        )
    return {
        "entry_id": result["entry_id"],
        "sleep_date": result["sleep_date"],
        "score": payload.score,
        "qualifiers": payload.qualifiers,
        "pre_sleep_factors": payload.pre_sleep_factors,
        "resolved_session_duration_s": result["resolved_session_duration_s"],
    }

@router.get("/sleep/entry")
def get_sleep_entry_for_date(date: str, current_user: dict = Depends(get_current_user)):
    ''' The subjective sleep journal entry (if any) for the night that
    woke up on `date` - what each per-night Sleep page's own journal
    section fetches to decide whether to show the submit form or the
    existing entry (read-only, with an Edit option). Returns null
    (not a 404) when nothing has been logged yet for this specific
    night - a normal, expected state, not an error.
    '''
    parsed_date = _parse_optional_date(date)
    if parsed_date is None:
        raise HTTPException(400, "date is required (YYYY-MM-DD)")
    return find_sleep_entry_for_wake_date(current_user["username"], parsed_date)

@router.get("/sleep")
def get_sleep_history(start: str | None = None, end: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Read-only history of subjective sleep entries across a date
    range. No longer powers a "Recent nights" list on the Sleep tab
    (each night's own page now shows just its own entry, via
    /sleep/entry) - kept as a general-purpose range query, e.g. for a
    future review/export view. start/end are ISO date strings;
    defaults to the last 30 days through tomorrow.

    Sorted by start_time (the session's own real timestamp), not
    sleep_date - multiple entries can share a sleep_date (see
    write_sleep_point's docstring for why), so sorting by date alone
    wouldn't give a stable or meaningful order between same-day entries.
    '''
    now = datetime.now(timezone.utc)
    try:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else now - timedelta(days=30)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else now + timedelta(days=1)
    except ValueError as e:
        raise HTTPException(400, f"invalid start/end date: {e}") from e

    entries = find_sleep_entries_in_range(current_user["username"], start_dt, end_dt)
    entries.sort(key=lambda e: e["start_time"] or "", reverse=True)
    return entries

@router.patch("/sleep/{entry_id}")
def patch_sleep(entry_id: str, payload: SleepUpdateIn, current_user: dict = Depends(get_current_user)):
    ''' Edit an existing sleep entry's score/qualifiers/pre_sleep_factors,
    addressed by its stable entry_id (not sleep_date - multiple entries
    can share a date, see write_sleep_point's docstring). Relies on
    write_sleep_point's fixed-per-session timestamp to overwrite
    cleanly - no delete-and-rewrite needed, unlike event tag edits.
    The caller (frontend) is expected to send every known qualifier AND
    pre_sleep_factor explicitly as true/false, not just the ones that
    are true - InfluxDB only overwrites fields actually included in a
    write, so an omitted one that was previously true would otherwise
    silently persist instead of being cleared.
    '''
    if not (1 <= payload.score <= 5):
        raise HTTPException(400, "score must be between 1 and 5")

    username = current_user["username"]
    existing = find_sleep_entry_by_id(username, entry_id)
    if existing is None:
        raise HTTPException(404, "no sleep entry found for this id")

    new_entry_id = write_sleep_point(
        user=username,
        session_start=existing["start_time"],
        sleep_date=existing["sleep_date"],
        score=payload.score,
        qualifiers=payload.qualifiers,
        pre_sleep_factors=payload.pre_sleep_factors,
        submission_ts=datetime.now(timezone.utc),
    )
    return {
        "entry_id": new_entry_id,
        "sleep_date": existing["sleep_date"],
        "score": payload.score,
        "qualifiers": payload.qualifiers,
        "pre_sleep_factors": payload.pre_sleep_factors,
    }

@router.delete("/sleep/{entry_id}")
def delete_sleep(entry_id: str, current_user: dict = Depends(get_current_user)):
    ''' Delete a sleep entry entirely, addressed by its stable entry_id. '''
    username = current_user["username"]
    existing = find_sleep_entry_by_id(username, entry_id)
    if existing is None:
        raise HTTPException(404, "no sleep entry found for this id")

    delete_sleep_entry(username, entry_id)
    return {"ok": True}

SLEEP_TREND_PERIODS = {"week", "month", "year"}

@router.get("/sleep/overview")
def get_sleep_overview(date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Everything the Sleep tab's main day-view needs for one specific
    night - session timing, stage breakdown, wake-event count, and
    sleep-window HR/respiratory averages, all combined server-side by
    get_sleep_overview_for_night() rather than requiring several
    separate frontend fetches.

    `date` names the WAKE date (the night that ended waking up on this
    calendar day) - defaults to today. Returns null (not a 404) when
    no sleep session is recorded for that night, the same "absence is
    normal, not an error" convention /today already uses for its own
    sleep card.
    '''
    wake_date = _parse_optional_date(date) or datetime.now(ZoneInfo(TZ_NAME)).date()
    overview = get_sleep_overview_for_night(current_user["username"], wake_date)
    if overview is not None:
        overview["duration_goal_s"] = SLEEP_DURATION_GOAL_SECONDS
    return overview

@router.get("/sleep/hypnogram")
def get_sleep_hypnogram(date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' The Sleep tab's hypnogram - the ordered, individual sleep-stage
    segments for one specific night (NOT /sleep/overview's aggregated
    stages_min totals), the chronological sequence a real hypnogram
    visualization needs. Same `date` (wake date) convention as
    /sleep/overview. Returns an empty list (not an error) when no
    sleep session is recorded for that night.
    '''
    wake_date = _parse_optional_date(date) or datetime.now(ZoneInfo(TZ_NAME)).date()
    return get_sleep_hypnogram_for_night(current_user["username"], wake_date)

@router.get("/sleep/vitals-series/{field}")
def get_sleep_vitals_series_endpoint(field: str, date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Raw per-point series for heart_rate or sleep_respiratory_rate
    within one night's actual sleep session window - the full-night
    chart the Sleep Heart Rate and Sleep Respiratory Rate detail pages
    plot against a stage-hypnogram background (see UI_DESIGN_NOTES.md).
    Reuses TODAY_SERIES_FIELDS' existing allowlist rather than a new
    one - both fields are already valid there.
    '''
    if field not in ("heart_rate", "sleep_respiratory_rate"):
        raise HTTPException(400, f"unsupported field for sleep vitals: {field!r}")
    wake_date = _parse_optional_date(date) or datetime.now(ZoneInfo(TZ_NAME)).date()
    return get_sleep_vitals_series(field, current_user["username"], wake_date)

@router.get("/sleep/timing-trend")
def get_sleep_timing_trend_endpoint(period: str, end_date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Per-night start_time/end_time/duration_s across a W/M/Y range -
    shared by the Sleep Duration detail page's "Last 7 days" bar chart
    and the Sleep Regularity detail page's bedtime/wake-time scatter
    charts and weekly averages (see get_sleep_timing_trend()'s own
    docstring for why one endpoint covers both). "day" deliberately
    not accepted - a trend across nights doesn't apply to a single
    night, same reasoning as /activity/time-range rejecting it.
    '''
    if period not in SLEEP_TREND_PERIODS:
        raise HTTPException(400, f"unsupported period: {period!r} (must be one of {sorted(SLEEP_TREND_PERIODS)})")
    start, end = _period_bounds(period, end_date)
    return get_sleep_timing_trend(current_user["username"], start.date(), end.date())

@router.get("/sleep/regularity-index")
def get_sleep_regularity_index_endpoint(end_date: str | None = None, days: int = 7, current_user: dict = Depends(get_current_user)):
    ''' Sleep Regularity Index (SRI) - a real, peer-reviewed metric
    (Phillips et al. 2017, Scientific Reports 7:3216) for the Sleep
    Regularity detail page, replacing an attempt to reproduce Zepp's
    own unpublished 0-100% "regularity" score (see UI_DESIGN_NOTES.md's
    own note that formula was never published). See
    get_sleep_regularity_index()'s own docstring for the full
    definition and citation.

    `days` mirrors the same window the page's other charts already use
    (7 by default) - the original paper's own recommendation is 7, or a
    multiple of 7, consecutive days, so this isn't an arbitrary default.
    Returns null (not a 4xx) when there's insufficient data (fewer than
    2 usable consecutive-night pairs) - matching how every other
    "insufficient data" state in this app's sleep/vitals baseline
    endpoints already behaves, not an error condition.
    '''
    parsed_date = _parse_optional_date(end_date)
    return get_sleep_regularity_index(current_user["username"], end_date=parsed_date, num_days=days)

@router.get("/sleep/stage-trend")
def get_sleep_stage_trend_endpoint(period: str, end_date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Per-night stage-minute breakdown across a W/M/Y range - the
    Sleep tab's own "vs Last 7 Days" stacked-bar weekly view (see
    UI_DESIGN_NOTES.md). Same period restriction as /sleep/timing-trend
    and for the same reason.
    '''
    if period not in SLEEP_TREND_PERIODS:
        raise HTTPException(400, f"unsupported period: {period!r} (must be one of {sorted(SLEEP_TREND_PERIODS)})")
    start, end = _period_bounds(period, end_date)
    return get_sleep_stage_trend(current_user["username"], start.date(), end.date())

@router.get("/sleep/journal-rollup")
def get_sleep_journal_rollup_endpoint(period: str, end_date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Subjective sleep journal entries aggregated into per-tag
    frequency counts across a W/M/Y range - the "Time Asleep
    (advanced)" page's own weekly Bedtime Journal / Wake-up Mood
    rollup cards (see get_sleep_journal_rollup's own docstring for the
    full field-by-field mapping). Same period restriction as
    /sleep/timing-trend and /sleep/stage-trend - unlike those two,
    this ISN'T at risk of an unusable "365 raw bars" problem for a
    year (it aggregates into a short list of tags, not one point per
    night), so year is included here even though it's deliberately
    left out of the Sleep Duration page's own period selector for a
    different reason.
    '''
    if period not in SLEEP_TREND_PERIODS:
        raise HTTPException(400, f"unsupported period: {period!r} (must be one of {sorted(SLEEP_TREND_PERIODS)})")
    start, end = _period_bounds(period, end_date)
    return get_sleep_journal_rollup(current_user["username"], start.date(), end.date())

@router.get("/sleep/naps")
def get_naps_endpoint(date: str, current_user: dict = Depends(get_current_user)):
    ''' Every nap for one specific calendar day - see
    get_naps_for_date's own docstring for the full confirmed byte
    layout this is decoded from. Returns an empty list (not a 404)
    when there were none that day - a normal, expected state (most
    days have zero naps), not an error.
    '''
    parsed_date = _parse_optional_date(date)
    if parsed_date is None:
        raise HTTPException(400, "date is required (YYYY-MM-DD)")
    naps = get_naps_for_date(current_user["username"], parsed_date)
    return [
        {
            "device": n["device"],
            "start_time": n["start_time"].isoformat(),
            "end_time": n["end_time"].isoformat(),
            "duration_s": n["duration_s"],
        }
        for n in naps
    ]

@router.get("/sleep/nap-trend")
def get_nap_trend_endpoint(period: str, end_date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Total nap minutes per calendar day across a W/M range - the
    Sleep Reports page's own weekly/monthly composition chart, adding
    naps as their own distinct stacked-bar category alongside Deep/
    Light/REM/Awake (see get_nap_trend's own docstring). Same period
    restriction as /sleep/stage-trend and for the same reason (this
    page's own week/month-only selector) - unlike that endpoint,
    "year" isn't offered here at all rather than allowed-but-unused,
    since nap totals have no standalone use outside this one chart.
    '''
    if period not in {"week", "month"}:
        raise HTTPException(400, f"unsupported period: {period!r} (must be one of ['month', 'week'])")
    start, end = _period_bounds(period, end_date)
    return get_nap_trend(current_user["username"], start.date(), end.date())

@router.get("/sleep/vitals-trend/{field}")
def get_sleep_vitals_trend_endpoint(field: str, period: str, end_date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Per-night average heart_rate or sleep_respiratory_rate across a
    W/M/Y range - the "Last 7 days" trend on the Sleep Heart Rate /
    Sleep Respiratory Rate detail pages. Same field restriction as
    /sleep/vitals-series, same period restriction as the other sleep
    trend endpoints and for the same reason.
    '''
    if field not in ("heart_rate", "sleep_respiratory_rate"):
        raise HTTPException(400, f"unsupported field for sleep vitals: {field!r}")
    if period not in SLEEP_TREND_PERIODS:
        raise HTTPException(400, f"unsupported period: {period!r} (must be one of {sorted(SLEEP_TREND_PERIODS)})")
    start, end = _period_bounds(period, end_date)
    return get_sleep_vitals_trend(field, current_user["username"], start.date(), end.date())

@router.get("/sleep/vitals-baseline/{field}")
def get_sleep_vitals_baseline_endpoint(field: str, days: int = 7, date: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Night-anchored baseline comparison for heart_rate or
    sleep_respiratory_rate - the "Slower/Lower - Baseline - Faster/
    Higher" gauge on the Sleep Heart Rate / Sleep Respiratory Rate
    detail pages.

    Deliberately its OWN endpoint under /sleep/, not routed through
    the existing /vitals/baseline/{field} (which decides nightly-vs-
    calendar-day comparison via the global NIGHTLY_BASELINE_FIELDS
    set) - heart_rate specifically is a field a future GENERAL
    (non-sleep) Heart Rate detail page would plausibly also want to
    show, and that page would want a calendar-day baseline (matching
    resting_heart_rate/hrv's own existing split), not the sleep-
    specific nightly one. Adding heart_rate to NIGHTLY_BASELINE_FIELDS
    globally would have silently forced every future caller into the
    nightly comparison - scoping this here instead avoids that
    conflict entirely rather than needing to resolve it later.

    get_nightly_baseline_comparison() itself needed no changes - it
    was already generic on `field`, this is purely about NOT wiring it
    through the field-to-comparison-type routing that's global.
    '''
    if field not in ("heart_rate", "sleep_respiratory_rate"):
        raise HTTPException(400, f"unsupported field for sleep vitals: {field!r}")
    if days not in BASELINE_ALLOWED_DAYS:
        raise HTTPException(400, f"unsupported days: {days!r} (must be one of {sorted(BASELINE_ALLOWED_DAYS)})")
    parsed_date = _parse_optional_date(date)
    return get_nightly_baseline_comparison(field, current_user["username"], baseline_days=days, for_date=parsed_date)