""" Small helpers/constants shared across more than one route module.
Anything used by only one route module lives directly in that module
instead - this is only for genuine cross-module sharing. """

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.config import TZ_NAME
from app.queries.vitals import local_today_bounds

# Only these fields are ever valid to chart - a fixed allowlist rather
# than passing the path parameter straight into the Flux query, since
# `field` reaches the query string directly (see get_today_series())
# and this is a user-influenceable URL segment.
TODAY_SERIES_FIELDS = {
    "heart_rate", "hrv", "stress", "spo2", "temperature", "resting_heart_rate",
    # Added for the Activity page: raw_intensity (day-view raw activity
    # chart only - no week/month/year raw-intensity view exists, so its
    # presence on the shared /vitals/range, /vitals/rolling-mean, and
    # /vitals/baseline endpoints is unused surface area, not a problem -
    # same shared-allowlist tradeoff resting_heart_rate already makes)
    # and steps (used by BOTH /today/series/steps for the day chart AND
    # /vitals/range/steps for the Week/Month/Year chart - this one is
    # actually exercised on more than one of the four endpoints).
    "raw_intensity", "steps",
}


# Same allowlist reasoning as above - `period` also reaches Flux
# (as an aggregateWindow() duration), so it's validated against a fixed
# mapping rather than accepted as an arbitrary string.
RANGE_PERIODS = {
    # "day" buckets a single day hourly, rather than the raw per-point
    # series /today/series returns - for a field like spo2 that's only
    # sampled when the wearer is still (see get_period_range_series's
    # own callers), a continuous line would either draw misleading
    # straight segments across long gaps or just show scattered dots;
    # hourly bars (matching Zepp's own SpO2 day view) leave an hour
    # with no reading as a simple gap in the bars instead. Not every
    # chart uses this for its day view - see DETAIL_VIEWS' per-chart
    # dayViewStyle flag on the frontend.
    "day": {"days": 1, "window": "1h"},
    "week": {"days": 7, "window": "1d"},
    "month": {"days": 30, "window": "1d"},
    "year": {"days": 365, "window": "1mo"},
}

def _parse_optional_date(date_str: str | None) -> date | None:
    ''' Shared YYYY-MM-DD parsing for the three detail-view endpoints'
    optional navigation date - None in means None out (defaults to
    today, same as before navigation existed), a malformed string is a
    400, never silently ignored or guessed at.
    '''
    if date_str is None:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, f"invalid date: {date_str!r} (expected YYYY-MM-DD)")

def _add_months(d: date, months: int) -> date:
    ''' Add (or, for a negative `months`, subtract) whole calendar
    months to a date - only ever called here with day=1 dates, so day-
    clamping for shorter target months never actually matters, but the
    arithmetic is written generally regardless.
    '''
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)

def _period_bounds(period: str, end_date: str | None) -> tuple[datetime, datetime]:
    ''' [start, end) for a W/M/Y period ending on `end_date` (today by
    default) - shared by /vitals/range and /vitals/rolling-mean so the
    window-boundary logic (including the year period's calendar-month
    alignment - see the comment on that branch) exists in exactly one
    place.
    '''
    parsed_end_date = _parse_optional_date(end_date)

    if period == "year":
        # Deliberately NOT a rolling 365-day window here, unlike week/
        # month below - Flux's aggregateWindow(every: 1mo) buckets
        # align to real calendar-month boundaries (confirmed via
        # InfluxDB's own docs), not fixed 30-day chunks. A rolling
        # 365-day range spans 12 months plus a few extra days, so it
        # wraps into a 13th, PARTIAL month-aligned bucket at each end -
        # and since 365 days is close to but not exactly 12 months,
        # those two partial buckets often land in the SAME calendar
        # month (e.g. a few days of "this September" and a few days of
        # "last September"), rendering as an apparently duplicate
        # month with no way to tell them apart. Anchoring to exactly
        # 12 full calendar months instead - from the start of the
        # month 11 months before the anchor month through the start of
        # the month AFTER the anchor month - always produces exactly
        # 12 distinct (month, year) buckets, no wraparound duplicate.
        anchor = parsed_end_date or datetime.now(ZoneInfo(TZ_NAME)).date()
        anchor_month_start = anchor.replace(day=1)
        start, _ = local_today_bounds(_add_months(anchor_month_start, -11))
        end, _ = local_today_bounds(_add_months(anchor_month_start, 1))
    else:
        spec = RANGE_PERIODS[period]
        end, _ = local_today_bounds(parsed_end_date)
        end = end + timedelta(days=1)  # include all of end_date (or today)
        start = end - timedelta(days=spec["days"])
        # Known narrow limitation, not fixed here: "day" period's 1h
        # aggregateWindow buckets align to whole-hour boundaries in
        # absolute (epoch) time, not necessarily to this local
        # timezone's own hour marks. For any TZ_NAME with a whole-hour
        # UTC offset (true for most real timezones, including all of
        # the US and most of Europe/East Asia) this makes no
        # difference; for a fractional-hour offset (e.g. India's
        # UTC+5:30) bucket boundaries would sit ~30-45 minutes off from
        # this local timezone's actual hour marks. Not addressed here
        # since it's unconfirmed to affect this deployment and no
        # smaller than the effort already spent getting the far more
        # consequential timeSrc/month-alignment bugs right - flagged
        # plainly instead of silently ignored.

    return start, end

BASELINE_ALLOWED_DAYS = {7, 14}