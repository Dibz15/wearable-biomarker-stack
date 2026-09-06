"""Heart rate / HRV / stress / SpO2 / temperature series, baselines, rolling means."""
import statistics
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from app.config import INFLUX_BUCKET, SENSOR_MEASUREMENT, TZ_NAME
from app.queries.client import get_client

def local_today_bounds(for_date: date | None = None) -> tuple[datetime, datetime]:
    ''' Start/end (midnight to midnight) of one local calendar day in
    the configured local timezone (TZ_NAME) - not UTC's calendar day,
    for the same reason find_last_completed_sleep_session() resolves
    sleep_date locally: a person in a timezone ahead of UTC would
    otherwise see "today" flip over hours before their own local
    midnight.

    `for_date` defaults to today when omitted (the original single
    purpose this function was written for), but can be any date - this
    is what the detail-view navigation (previous/next day, jump to a
    date) uses to compute bounds for a day other than today, without
    every existing caller needing to change.
    '''
    tz = ZoneInfo(TZ_NAME)
    if for_date is None:
        for_date = datetime.now(tz).date()
    start_local = datetime.combine(for_date, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local, end_local

def _device_stat_by_field(field: str, user: str, start: datetime, end: datetime, stat: str) -> dict[str, float]:
    ''' One reducer (stat: "last"/"mean"/"min"/"max"/"median") for one
    field, grouped by device, within [start, end). Returns
    {device_name: value}.

    Deliberately one simple query per (field, stat) pair rather than a
    cleverer combined Flux query (e.g. multiple yield() calls in one
    script) - this app's existing InfluxDB functions are all written
    this way (see find_last_completed_sleep_session, list_distinct_
    sensor_users above), favoring obviously-correct simple queries over
    fewer-but-trickier round trips. Personal-use traffic volume makes
    that the right tradeoff here too.
    '''
    if stat not in ("last", "mean", "min", "max", "median"):
        raise ValueError(f"unsupported stat: {stat!r}")

    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "{field}")
      |> group(columns: ["device"])
      |> {stat}()
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query {stat}({field}) for user={user}: {e}")
        return {}

    result = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            value = record.get_value()
            if device is not None and value is not None:
                result[device] = value
    return result

def get_today_vitals(user: str, for_date: date | None = None) -> dict[str, dict]:
    ''' Today's (local calendar day) vitals summary, per device, for
    every field both parsers share a common name for (see the shared-
    field-name design used throughout parser/activefit - this is
    exactly what makes a single query work unmodified across whichever
    devices happen to be reporting, ring or watch or both).

    `for_date` defaults to today when omitted - what the Today tab's
    date navigation uses to view a past day's summary instead.

    Returns:
        {
          "heart_rate": {"<device>": {"last": .., "avg": .., "min": .., "max": ..}, ...},
          "hrv": {"<device>": {"last": ..}, ...},           # last only - a single
          "stress": {"<device>": {"last": .., "avg": ..}, ...},  # reading isn't
          "spo2": {"<device>": {"last": .., "min": .., "max": ..}, ...},  # usefully
          "temperature": {"<device>": {"last": ..}, ...},    # averaged/ranged
        }

    A device missing from a field's dict simply hasn't reported that
    field today - not an error, callers should treat absence as "no
    data yet" (e.g. before the first sync of the day) rather than a
    failure.
    '''
    start, end = local_today_bounds(for_date)

    # (field, which stats actually make sense for it)
    field_stats = {
        "heart_rate": ("last", "mean", "min", "max"),
        "hrv": ("last",),
        "stress": ("last", "mean"),
        "spo2": ("last", "min", "max"),
        "temperature": ("last",),
        # Device-computed (not app-aggregated - see gadgetbridge_to_
        # influxdb.py's own extraction of HUAMI_HEART_RATE_RESTING_SAMPLE),
        # typically one reading a day - "last" only, same "a single
        # reading isn't usefully averaged/ranged" reasoning as hrv/
        # temperature above. Not in today.js's own METRIC_FIELDS
        # allowlist, so this doesn't add a new card to the Today tab -
        # added here specifically so the Sleep Heart Rate detail page
        # can fetch a given wake-date's resting HR via this same,
        # already-established endpoint rather than a new one.
        "resting_heart_rate": ("last",),
    }

    result: dict[str, dict] = {}
    for field, stats in field_stats.items():
        by_device: dict[str, dict] = {}
        for stat in stats:
            stat_key = "avg" if stat == "mean" else stat
            for device, value in _device_stat_by_field(field, user, start, end, stat).items():
                by_device.setdefault(device, {})[stat_key] = round(value, 1)
        result[field] = by_device

    return result

def get_today_series(field: str, user: str, for_date: date | None = None) -> dict[str, list[dict]]:
    ''' Raw (unaggregated) points for one field, for one day (today by
    default, or `for_date` for the detail-view's day-navigation), per
    device - the time series a detail-view chart plots, as opposed to
    get_today_vitals()'s reduced last/avg/min/max summary.

    Returns {"<device>": [{"t": <ISO8601>, "v": <value>}, ...], ...},
    each device's list sorted chronologically.
    '''
    start, end = local_today_bounds(for_date)
    return _grouped_series(field, user, start, end)

def _grouped_series(field: str, user: str, start: datetime, end: datetime) -> dict[str, list[dict]]:
    ''' Shared query behind get_today_series() and the W/M/Y range
    endpoints - one field, grouped by device, sorted chronologically,
    over an arbitrary [start, end) window.

    The explicit sort() after group() is required, not optional or
    redundant - confirmed directly from InfluxDB's own docs ("Group
    does not guarantee sort order. To ensure data is sorted correctly,
    use sort() after group()."), not just inferred from the symptom.
    Without it, points from what were originally several disjoint
    underlying series (this data also carries activity_kind, sample_type,
    etc. as tags - see parser/activefit - each combination is its own
    series until an explicit group() call collapses them by device
    alone) get merged in whatever order the query engine happened to
    produce internally, not necessarily chronological - a line chart
    connecting points in that non-chronological array order visually
    looks like the reported "skip lines and a bunch of separate
    points", since the line jumps backward and forward in time rather
    than progressing smoothly left to right. An earlier version of
    this function incorrectly assumed group() preserved time order
    (see git history) - that assumption was never actually verified
    and was wrong.
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
      |> filter(fn: (r) => r._field == "{field}")
      |> group(columns: ["device"])
      |> sort(columns: ["_time"])
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query {field} series for user={user}: {e}")
        return {}

    result: dict[str, list[dict]] = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            value = record.get_value()
            if device is None or value is None:
                continue
            result.setdefault(device, []).append({
                "t": record.get_time().isoformat(),
                "v": value,
            })
    return result

def get_manual_readings(field: str, user: str, start: datetime, end: datetime) -> dict[str, list[dict]]:
    ''' Per-device manually-triggered readings only (as opposed to the
    device's own automatic periodic sampling) for one field, over an
    arbitrary [start, end) window - what Zepp's own Stress page calls
    its "Manual Data" list, and (person-confirmed, not assumed) also
    applicable to SpO2 given the identical TYPE_NUM convention on both
    fields.

    Relies on `{field}_type_num` being a TAG (not a field) with value
    "0" meaning manual - confirmed for both stress and spo2 via a
    deliberate cross-check, not inferred from Gadgetbridge's
    feature-list wording alone. Only
    ever call this for a field actually confirmed to have this tag -
    MANUAL_TYPE_NUM_FIELDS in main.py is the enforced allowlist: an
    unsupported field would just silently return {} here (the tag
    filter simply never matches anything), which is a much less
    obvious failure than the 400 the allowlist gives instead.

    Returns {"<device>": [{"t": <ISO8601>, "v": <value>}, ...], ...},
    each device's list sorted chronologically - same shape as
    get_today_series(), just pre-filtered to manual readings only.
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
      |> filter(fn: (r) => r._field == "{field}")
      |> filter(fn: (r) => r.{field}_type_num == "0")
      |> group(columns: ["device"])
      |> sort(columns: ["_time"])
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query manual {field} readings for user={user}: {e}")
        return {}

    result: dict[str, list[dict]] = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            value = record.get_value()
            if device is None or value is None:
                continue
            result.setdefault(device, []).append({
                "t": record.get_time().isoformat(),
                "v": value,
            })
    return result

def get_period_range_series(field: str, user: str, start: datetime, end: datetime, window: str) -> dict[str, list[dict]]:
    ''' Per-device min/max/median for one field, bucketed into
    `window`-sized periods (a Flux duration string, e.g. "1d" or "1mo")
    across [start, end) - what the W/M/Y "range bar" charts plot (one
    bar per period spanning that period's low-to-high, with the median
    marked inside it), as opposed to get_today_series()'s raw
    per-point series used for the D view.

    Returns {"<device>": [{"t": <period start ISO8601>, "min": v,
    "max": v, "median": v}, ...]}.

    Three separate aggregateWindow() queries (min, max, median), zipped
    together by (device, period start) - matches this file's established
    style of simple single-purpose queries over one cleverer combined
    query (see _device_stat_by_field's own docstring for the same
    reasoning). median needs different Flux syntax from the other two:
    plain `fn: median` doesn't work in aggregateWindow() (median()
    lacks the `column` parameter aggregateWindow tries to pass to it -
    confirmed via InfluxDB's own docs, not assumed), so it needs the
    full anonymous-function form instead. Uses median()'s
    "exact_selector" method specifically, which returns an actual
    observed reading rather than an interpolated/averaged value -
    right for showing "an actual recorded reading from that day", not
    a synthetic number nobody's device ever produced.

    All three queries explicitly set timeSrc: "_start" - confirmed via
    InfluxDB's own docs that aggregateWindow() otherwise defaults to
    _stop (the END of each window) as the timestamp it assigns to the
    aggregated value. Without this, every bucket's data is labeled
    with the FOLLOWING period's boundary - a full month off for the
    Year view's monthly buckets (exactly the reported "August's data
    shows up under September" bug), a subtler one-day shift for
    week/month's daily buckets that likely went unnoticed for the same
    reason a smaller error is easier to miss.
    '''
    client = get_client()
    query_api = client.query_api()

    start_iso = start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    by_device_and_time: dict[str, dict[str, dict]] = {}

    def run_and_collect(flux: str, key: str):
        try:
            tables = query_api.query(flux)
        except Exception as e:
            logger.warning(f"Failed to query {key}({field}) range series for user={user}: {e}")
            return
        for table in tables:
            for record in table.records:
                device = record.values.get("device")
                value = record.get_value()
                period_start = record.get_time()
                if device is None or value is None or period_start is None:
                    continue
                period_key = period_start.isoformat()
                by_device_and_time.setdefault(device, {}).setdefault(period_key, {"t": period_key})[key] = value

    for stat in ("min", "max"):
        run_and_collect(f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {start_iso}, stop: {stop_iso})
          |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
          |> filter(fn: (r) => r.user == "{user}")
          |> filter(fn: (r) => r._field == "{field}")
          |> group(columns: ["device"])
          |> aggregateWindow(every: {window}, fn: {stat}, createEmpty: false, timeSrc: "_start")
        ''', stat)

    run_and_collect(f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "{field}")
      |> group(columns: ["device"])
      |> aggregateWindow(
           every: {window},
           fn: (tables=<-, column) => tables |> median(method: "exact_selector"),
           createEmpty: false,
           timeSrc: "_start",
         )
    ''', "median")

    result: dict[str, list[dict]] = {}
    for device, periods in by_device_and_time.items():
        # Only keep periods where min, max, AND median all came back -
        # a period missing one (shouldn't normally happen, since all
        # three queries share the same filter/window) is incomplete
        # data, not a real zero-width range or a period with no median.
        complete = [p for p in periods.values() if "min" in p and "max" in p and "median" in p]
        result[device] = sorted(complete, key=lambda p: p["t"])
    return result

def get_rolling_mean_series(field: str, user: str, start: datetime, end: datetime, window_days: int = 7) -> dict[str, list[dict]]:
    ''' Per-device rolling `window_days`-day mean, one value per
    calendar day in [start, end) - the trend line overlaid on the W/M
    range-bar charts, giving a smoothed view of drift beneath the
    day-to-day noise of individual bars' min/max/median. Only
    meaningful at daily granularity (week/month views) - a "7-day"
    rolling mean doesn't map cleanly onto the Year view's monthly
    buckets, so this isn't used there.

    Returns {"<device>": [{"t": <day ISO8601>, "value": v}, ...]},
    one entry per day actually within [start, end) that has enough
    trailing history to average - a day near the very start of a
    person's data (before `window_days` days of history exist) uses
    however many days ARE available rather than being dropped, the
    same "use what's there" approach a rolling average commonly takes
    (e.g. COVID case-tracking dashboards near the start of a series),
    rather than requiring a full window before showing anything.

    One aggregateWindow(fn: mean, every: 1d) query over an EXTENDED
    range - starting window_days-1 days before `start` - so the very
    first displayed day still has a genuine trailing window to average
    over; the rolling average itself is then computed in Python from
    those daily means.
    '''
    client = get_client()
    query_api = client.query_api()

    extended_start = start - timedelta(days=window_days - 1)
    start_iso = extended_start.astimezone(timezone.utc).isoformat()
    stop_iso = end.astimezone(timezone.utc).isoformat()

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> range(start: {start_iso}, stop: {stop_iso})
      |> filter(fn: (r) => r._measurement == "{SENSOR_MEASUREMENT}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r._field == "{field}")
      |> group(columns: ["device"])
      |> aggregateWindow(every: 1d, fn: mean, createEmpty: false, timeSrc: "_start")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query rolling mean series for {field}, user={user}: {e}")
        return {}

    # Per-device, chronologically sorted (day, value) pairs across the
    # EXTENDED range (including the window_days-1 lookback-only days).
    daily_by_device: dict[str, list[tuple[str, float]]] = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            value = record.get_value()
            period_start = record.get_time()
            if device is None or value is None or period_start is None:
                continue
            daily_by_device.setdefault(device, []).append((period_start.isoformat(), value))
    for device in daily_by_device:
        daily_by_device[device].sort(key=lambda p: p[0])

    start_iso_cutoff = start.astimezone(timezone.utc).isoformat()
    result: dict[str, list[dict]] = {}
    for device, days in daily_by_device.items():
        rolling: list[dict] = []
        for i, (day_iso, _value) in enumerate(days):
            if day_iso < start_iso_cutoff:
                continue  # a lookback-only day, not one to actually display
            window_slice = days[max(0, i - window_days + 1):i + 1]
            mean_value = sum(v for _, v in window_slice) / len(window_slice)
            rolling.append({"t": day_iso, "value": mean_value})
        if rolling:
            result[device] = rolling
    return result

def _daily_values(field: str, user: str, start: datetime, end: datetime) -> dict[str, list[float]]:
    ''' Per-device list of one mean value per day, over [start, end) -
    the intermediate this file's own get_baseline_comparison() needs
    to compute a mean/stddev ACROSS days (not across raw readings
    within a single window, which _device_stat_by_field() already
    does but isn't the same statistic). Timestamps are dropped - only
    the values themselves matter for the mean/stddev calculation.
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
      |> filter(fn: (r) => r._field == "{field}")
      |> group(columns: ["device"])
      |> aggregateWindow(every: 1d, fn: mean, createEmpty: false, timeSrc: "_start")
    '''

    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Failed to query daily {field} values for user={user}: {e}")
        return {}

    result: dict[str, list[float]] = {}
    for table in tables:
        for record in table.records:
            device = record.values.get("device")
            value = record.get_value()
            if device is None or value is None:
                continue
            result.setdefault(device, []).append(value)
    return result

def _zscore_comparison(today_value: dict[str, float], daily_values: dict[str, list[float]]) -> dict[str, dict]:
    ''' Shared z-score math behind every "today vs. trailing baseline"
    comparison bar - given one day's value and a list of baseline
    daily values per device, compute mean/stddev/z/delta. Extracted
    from get_baseline_comparison() so the identical math (including
    the zero-stddev edge case handling - see the comment below) is
    reused by get_nightly_baseline_comparison() too, rather than two
    near-identical copies of the same logic differing only in how
    "one day's value" gets computed upstream.

    Returns {"<device>": {"today": v, "baseline_mean": m,
    "baseline_stddev": s, "z": (today-mean)/stddev, "delta": today-mean}}.

    A device is omitted entirely if there isn't enough data to compute
    something meaningful - fewer than 2 baseline days (a stddev needs
    at least 2 points) or no value at all for that day. This is a real
    "insufficient data" case, not an error - same situation Zepp's own
    gauge shows early on, and the caller should treat it the same way
    (an empty/insufficient-data state, not a failure).
    '''
    result: dict[str, dict] = {}
    for device, values in daily_values.items():
        if len(values) < 2 or device not in today_value:
            continue
        mean = statistics.mean(values)
        stddev = statistics.stdev(values)
        today_v = today_value[device]
        delta = today_v - mean
        if stddev > 0:
            z = delta / stddev
        elif delta != 0:
            # Zero historical variance (every baseline day identical)
            # but today differs anyway - any deviation from a
            # perfectly flat baseline is maximally noteworthy, not
            # "no different". Defaulting to z=0 here would put the
            # marker dead-center while the delta text correctly shows
            # a non-zero difference - a direct visual/textual
            # contradiction. Pin far beyond any real cap (the frontend
            # clamps display to +-2) so the marker lands at the
            # correct edge instead.
            z = 10.0 if delta > 0 else -10.0
        else:
            z = 0.0
        result[device] = {
            "today": round(today_v, 1),
            "baseline_mean": round(mean, 1),
            "baseline_stddev": round(stddev, 2),
            "z": round(z, 2),
            "delta": round(delta, 1),
        }
    return result

def get_baseline_comparison(field: str, user: str, baseline_days: int = 7, for_date: date | None = None) -> dict[str, dict]:
    ''' For one field, per device: one day's value (today by default,
    or `for_date` for the detail-view's day-navigation) compared
    against a trailing baseline - the mean and (sample) standard
    deviation of daily values over the `baseline_days` days immediately
    BEFORE that day (the day itself excluded, so a value is never
    compared against a baseline that includes itself). Powers the
    Slower/Faster z-scored comparison bar - the same "vs. your own
    baseline" gauge concept Zepp's own Resting Heart Rate/HRV pages
    show (see wearable-events/UI_DESIGN_NOTES.md), just computed here
    instead of left blank the way Zepp's own version was for lack of
    history.

    Uses a calendar-midnight-to-midnight day as "one day's value" -
    the right definition for a field like resting_heart_rate. HRV uses
    a different, night-anchored definition instead - see
    get_nightly_baseline_comparison().
    '''
    today_start, today_end = local_today_bounds(for_date)
    baseline_start = today_start - timedelta(days=baseline_days)

    daily = _daily_values(field, user, baseline_start, today_start)
    today_value = _device_stat_by_field(field, user, today_start, today_end, "last")
    return _zscore_comparison(today_value, daily)
