"""Nap detection and trend, decoded from the hidden sleep-session sub-structure (see parser/activefit/FIELD_RESEARCH.md)."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from app.config import INFLUX_BUCKET, SENSOR_MEASUREMENT, TZ_NAME
from app.queries.client import get_client

def get_naps_for_date(user: str, local_date: date) -> list[dict]:
    ''' Naps (sample_type="nap", written by the parser's own
    decode_nap_candidates_from_blob() - see
    parser/activefit/FIELD_RESEARCH.md for the full confirmed byte
    layout and reference-timestamp reasoning, verified end to end
    against real ground truth across 3 real naps on 2 separate dates)
    whose own start time falls within `local_date`'s calendar day, in
    the configured TZ_NAME.

    Deliberately NOT a "wake date" resolution the way overnight sleep
    sessions need (_sleep_sessions_by_wake_date()'s own docstring) - a
    nap happens entirely within one day and has no midnight-crossing
    ambiguity to resolve, so a plain calendar-day range is enough.

    Returns a chronologically-sorted list of {"device": str,
    "start_time": datetime, "end_time": datetime, "duration_s": int} -
    every nap that day, not just one. The person's own real Zepp
    screenshots showed two separate naps on the same real day
    (2026-09-05) - this returns all of them, not just the first/last.
    '''
    tz = ZoneInfo(TZ_NAME)
    day_start = datetime.combine(local_date, datetime.min.time(), tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    client = get_client()
    query_api = client.query_api()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {day_start.astimezone(timezone.utc).isoformat()}, stop: {day_end.astimezone(timezone.utc).isoformat()})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.sample_type == "nap")
      |> filter(fn: (r) => r.user == "{user}")
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query naps for user={user}, date={local_date}: {e}")
        return []

    naps = []
    for table in tables:
        for record in table.records:
            values = record.values
            nap_start = values.get("nap_start")
            nap_end = values.get("nap_end")
            if nap_start is None or nap_end is None:
                continue
            naps.append({
                "device": values.get("device"),
                "start_time": datetime.fromtimestamp(nap_start, tz=timezone.utc),
                "end_time": datetime.fromtimestamp(nap_end, tz=timezone.utc),
                "duration_s": values.get("nap_duration_s"),
            })
    naps.sort(key=lambda n: n["start_time"])
    return naps

def get_nap_trend(user: str, start_date: date, end_date: date) -> list[dict]:
    ''' Total nap minutes per calendar day across [start_date, end_date)
    - the Sleep Reports page's own weekly/monthly composition chart,
    showing naps as their own distinct category alongside Deep/Light/
    REM/Awake - matching Zepp's own real treatment (UI_DESIGN_NOTES.md's
    "Time Asleep (advanced)" page notes: naps shown as a separate,
    distinctly-colored bar/legend entry, not folded into the main
    stack).

    One Flux query over the whole range (not one get_naps_for_date()
    call per day in a loop) - same "fetch once, group client-side"
    principle already used elsewhere in this file (e.g.
    find_sleep_entries_in_range). Groups by each nap's own START time
    converted to the configured local timezone, matching
    get_naps_for_date()'s own per-day query bounds, so a day's total
    here always matches what that function would return for the same
    day.

    Returns a chronologically-sorted list of {"date": "YYYY-MM-DD",
    "total_nap_minutes": int} - days with no naps are omitted (not
    zero-filled), same convention as get_sleep_stage_trend()/
    get_sleep_timing_trend(). A day with more than one nap (confirmed
    real - the person's own 2026-09-05 had two) sums to one combined
    total for that day, matching how the stacked composition chart
    itself works (one bar per day, not one segment per individual nap).
    '''
    tz = ZoneInfo(TZ_NAME)
    range_start = datetime.combine(start_date, datetime.min.time(), tzinfo=tz)
    range_end = datetime.combine(end_date, datetime.min.time(), tzinfo=tz)

    client = get_client()
    query_api = client.query_api()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {range_start.astimezone(timezone.utc).isoformat()}, stop: {range_end.astimezone(timezone.utc).isoformat()})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.sample_type == "nap")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "nap_duration_s")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query nap trend for user={user}: {e}")
        return []

    seconds_by_date: dict[str, float] = {}
    for table in tables:
        for record in table.records:
            local_date = record.get_time().astimezone(tz).date().isoformat()
            duration_s = record.get_value()
            if duration_s is None:
                continue
            seconds_by_date[local_date] = seconds_by_date.get(local_date, 0) + duration_s

    return [
        {"date": d, "total_nap_minutes": round(secs / 60)}
        for d, secs in sorted(seconds_by_date.items())
    ]