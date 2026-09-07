#!/usr/bin/env python3
"""
InfluxDB client setup and the write path. Writing a list of already-
computed {timestamp, fields, tags} dicts as Points is entirely
device-agnostic - by the time results reach here, all the COLMI_* /
HUAMI_* specific extraction has already happened upstream.
"""

import time
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point
from loguru import logger


# How far either side of a newly-decoded session to look for points a
# PREVIOUS run already wrote for that same night. Sized for the real
# failure mode: the watch re-syncs a refined summary of the same night
# whose stage boundaries have shifted by minutes, not hours. Wide
# enough to still match after that drift, deliberately narrower than
# the gap to an adjacent nap or the previous night's session, since
# anything this window reaches is a deletion candidate.
SESSION_REPLACE_PROBE_PAD_SECONDS = 5400  # 1.5h

# How much two sessions must overlap to be considered re-syncs of the
# same real night rather than two genuinely separate sleeps.
# Normalized by the SHORTER of the two spans on purpose: a refined
# blob often describes a shorter session than the first one did, and
# normalizing by the longer span would stop it matching exactly when
# the replacement matters most.
SESSION_MATCH_MIN_OVERLAP = 0.5


def build_client(url, token, org) -> InfluxDBClient:
    return InfluxDBClient(url=url, token=token, org=org)


def _flux_escape(value) -> str:
    ''' Escapes a value for interpolation into a Flux string literal.
    Same reasoning as _quote_predicate_value, for the read path.
    '''
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _quote_predicate_key(tag: str) -> str:
    ''' Delete predicates are parsed as InfluxQL, so a tag name that
    collides with an InfluxQL keyword has to be quoted or the parser
    rejects the whole expression.

    This is not hypothetical: `user` - a tag EVERY point this parser
    writes carries - is an InfluxQL keyword (CREATE USER / SHOW USERS
    / GRANT ... TO USER). An unquoted `user="..."` term fails the
    whole delete with 400 "bad logical expression", pointing at the
    character where that term starts.

    Every tag key is quoted rather than maintaining a list of the
    ~60 InfluxQL keywords and checking against it: quoting is
    harmless for non-keywords, and a missing entry in a hand-kept
    keyword list would resurface exactly this bug for some future tag.
    '''
    return f'"{tag}"'


def _quote_predicate_value(value) -> str:
    ''' Values are already quoted; this only guards against a value
    that itself contains a quote or backslash (a device name is
    user-editable, so it can contain anything) breaking out of the
    string and corrupting the expression.
    '''
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _identity_tag_filters(user, source, device_tags):
    ''' The tag set that identifies "this parser's copy of this
    device's data", as (tag, value) pairs.

    Scoped by BOTH `device`/`identifier` AND `source`, never just one.
    Two different parsers can legitimately write the same physical
    device under the same `device` name (a Gadgetbridge export is
    shared, so more than one parser can pick up the same watch) - so a
    predicate scoped only by device would have one parser deleting the
    other's data. Conversely, `source` alone would delete every device
    that parser handles. Both, always.

    `alias` is deliberately NOT included even though device_tags
    carries it: it's a user-editable label that can change between
    runs, and a delete predicate that no longer matches simply fails
    to clean up (leaving the duplicates this is meant to remove).
    '''
    filters = [("user", user)]
    if source is not None:
        filters.append(("source", source))
    for tag in ("device", "identifier"):
        if device_tags.get(tag):
            filters.append((tag, device_tags[tag]))
    return filters


def _probe_existing_session(client, bucket, measurement, user, source, device_tags,
                            start_ns, stop_ns):
    ''' Returns (earliest_start_ns, latest_end_ns) covering the sleep
    session/stage points already stored in [start_ns, stop_ns) for
    this device+source, or None if there are none.

    Reads only the two duration-carrying fields, both in seconds, so a
    point's real end is `_time + duration`. That matters: a stage
    segment's own start timestamp says nothing about how far it
    extends, and the extent is what the caller needs in order to
    delete a PREVIOUS version of a session that ran longer than the
    replacement does.
    '''
    query_api = client.query_api()
    # Bracket notation with an escaped value, not r.tag == "value":
    # tag values here are device names, which are user-editable and can
    # contain a quote or backslash. Interpolated raw, one of those
    # would break out of the Flux string and corrupt the whole query -
    # the same hazard the delete predicate guards against, and it has
    # to be handled on both paths, not just the destructive one.
    tag_filter = "".join(
        f'\n      |> filter(fn: (r) => r["{tag}"] == "{_flux_escape(value)}")'
        for tag, value in _identity_tag_filters(user, source, device_tags)
    )
    flux = f'''
    from(bucket: "{_flux_escape(bucket)}")
      |> range(start: {_ns_to_rfc3339(start_ns)}, stop: {_ns_to_rfc3339(stop_ns)})
      |> filter(fn: (r) => r._measurement == "{_flux_escape(measurement)}"){tag_filter}
      |> filter(fn: (r) => r._field == "sleep_stage_duration_s" or r._field == "sleep_session_duration_s")
    '''

    tables = query_api.query(flux)
    earliest = None
    latest = None
    for table in tables:
        for record in table.records:
            t = record.get_time()
            duration_s = record.get_value()
            if t is None or duration_s is None:
                continue
            point_start = int(t.timestamp() * 1_000_000_000)
            point_end = point_start + int(duration_s) * 1_000_000_000
            earliest = point_start if earliest is None else min(earliest, point_start)
            latest = point_end if latest is None else max(latest, point_end)
    if earliest is None:
        return None
    return (earliest, latest)


def _ns_to_rfc3339(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _overlap_fraction(a_start, a_end, b_start, b_end) -> float:
    ''' Length of the intersection over the length of the SHORTER
    span - see SESSION_MATCH_MIN_OVERLAP for why the shorter one.
    '''
    overlap = min(a_end, b_end) - max(a_start, b_start)
    if overlap <= 0:
        return 0.0
    shortest = min(a_end - a_start, b_end - b_start)
    if shortest <= 0:
        return 0.0
    return overlap / shortest


def replace_session_points(client, bucket, org, measurement, user, source, device_tags,
                           session_start_ns, session_end_ns,
                           probe_pad_seconds=SESSION_REPLACE_PROBE_PAD_SECONDS,
                           min_overlap=SESSION_MATCH_MIN_OVERLAP) -> bool:
    ''' Clears any previously-written version of this sleep session so
    the caller's own points replace it rather than piling up beside it.
    Returns True when it's safe to write, False when a needed delete
    failed and the caller must NOT write.

    Why this is needed at all: every other data type this parser
    handles is naturally idempotent - re-syncing the same sample
    rewrites the same (series, timestamp) and InfluxDB overwrites it,
    which is exactly the assumption get_last_checkpoint_ns's own
    docstring records ("re-querying an already-synced device's recent
    data is harmless"). Sleep sessions break it. The watch re-sends a
    REFINED summary blob for a night it already sent, and the stage
    timestamps are derived from that blob's own internal clock - so a
    refined blob describes the same real night at slightly different
    boundaries, writing points BESIDE the originals instead of over
    them. Summing them then double-counts the night.

    deduplicate_sleep_session_rows() already handles this when both
    blobs arrive in the same batch, but it can only see one run's rows;
    when the refined blob lands after the first has passed out of the
    checkpoint overlap window, no amount of within-batch dedup can
    help, because the earlier points are already in InfluxDB. This is
    the cross-run half of the same fix.

    Matching is by time OVERLAP rather than an exact key, since the
    whole problem is that the refined blob's boundaries moved. The
    delete then covers the UNION of the old and new spans - deleting
    only the new span would strand any points from a previous version
    that ran longer, which is precisely the case that leaves an
    inflated total behind.
    '''
    probe_pad_ns = probe_pad_seconds * 1_000_000_000
    try:
        prior = _probe_existing_session(
            client, bucket, measurement, user, source, device_tags,
            session_start_ns - probe_pad_ns, session_end_ns + probe_pad_ns,
        )
    except Exception as e:
        # Probe failure is NOT fatal: with nothing deleted, writing
        # behaves exactly as it did before this function existed
        # (possible duplicates), which is strictly better than skipping
        # a real night's data entirely.
        logger.warning(f"Could not check for an existing sleep session to replace "
                       f"(device={device_tags.get('device')!r}): {e} - writing without replacing")
        return True

    if prior is None:
        return True

    prior_start, prior_end = prior
    overlap = _overlap_fraction(session_start_ns, session_end_ns, prior_start, prior_end)
    if overlap < min_overlap:
        # Close in time but not the same sleep - e.g. a genuinely
        # separate session earlier the same evening. Leave it alone.
        logger.debug(f"Existing sleep points near this session overlap it by only "
                     f"{overlap:.0%} (< {min_overlap:.0%}) - treating as a different "
                     f"session and leaving them in place")
        return True

    delete_start_ns = min(session_start_ns, prior_start)
    # +1s because InfluxDB's delete stop bound is exclusive, and the
    # last stage point can sit exactly on the session end.
    delete_stop_ns = max(session_end_ns, prior_end) + 1_000_000_000

    predicate_base = " AND ".join(
        [f'_measurement={_quote_predicate_value(measurement)}']
        + [
            f'{_quote_predicate_key(tag)}={_quote_predicate_value(value)}'
            for tag, value in _identity_tag_filters(user, source, device_tags)
        ]
    )

    try:
        delete_api = client.delete_api()
        # Two calls, not one with an OR: InfluxDB's delete predicate
        # grammar supports AND but not OR.
        for sample_type in ("sleep_session", "sleep_stage"):
            delete_api.delete(
                _ns_to_rfc3339(delete_start_ns),
                _ns_to_rfc3339(delete_stop_ns),
                f'{predicate_base} AND {_quote_predicate_key("sample_type")}={_quote_predicate_value(sample_type)}',
                bucket=bucket,
                org=org,
            )
    except Exception as e:
        # Deliberately fatal for this session: writing after a failed
        # delete would recreate exactly the duplication this exists to
        # prevent, and would make it permanent. Skipping leaves the
        # night as it already was, and the next run retries.
        logger.error(f"Failed to delete the previous version of this sleep session "
                     f"(device={device_tags.get('device')!r}, "
                     f"{_ns_to_rfc3339(delete_start_ns)} - {_ns_to_rfc3339(delete_stop_ns)}): {e} - "
                     f"NOT writing this session's points, to avoid duplicating them")
        return False

    logger.info(f"Replaced a previously-written version of this sleep session "
                f"(device={device_tags.get('device')!r}, overlap {overlap:.0%})")
    return True


def write_results(client, results, bucket, org, measurement, user, source,
                   max_future_tolerance_seconds):
    ''' Write results to InfluxDB using the given (already-open) client.

    Every point gets a `user` tag (see GADGETBRIDGE_USER-equivalent
    config in each device app for why, even in a single-user setup)
    and, when `source` is given, a `source` tag identifying which
    parser wrote it (e.g. "colmi", "amazfit") - this is what lets
    get_last_checkpoint_ns (see common/checkpoint.py) scope checkpoint
    lookups per-parser instead of blending multiple devices' sync
    history together.
    '''
    logger.debug(f"Writing {len(results)} point(s) tagged user={user!r}"
                 + (f", source={source!r}" if source is not None else ""))
    now_ns = time.time_ns()
    max_future_ns = now_ns + (max_future_tolerance_seconds * 1_000_000_000)
    write_failures = 0
    skipped_future = 0
    with client.write_api() as _write_client:
        for row in results:
            if row['timestamp'] > max_future_ns:
                skipped_future += 1
                logger.warning(
                    f"Skipping point with implausible future timestamp "
                    f"(tags={row['tags']}) - likely corrupted raw data, same class "
                    f"of issue the ObservedTracker guards against for the checkpoint."
                )
                continue

            p = Point(measurement)
            p = p.tag("user", user)
            if source is not None:
                p = p.tag("source", source)

            for tag in row['tags']:
                p = p.tag(tag, row['tags'][tag])

            for field in row['fields']:
                val = row['fields'][field]

                if val == -1:
                    continue

                # Skip any special heart_rate values (upstream noted
                # these show up as sentinel/error values on Huami gear;
                # kept as a safety net here too, generically, since it
                # applies to any device's heart_rate field)
                if field == "heart_rate" and val is not None and val > 253:
                    continue

                if val is None:
                    continue

                p = p.field(field, val)

            p = p.time(row['timestamp'])

            # A single point's write can fail for reasons unrelated to
            # every other point - most notably an InfluxDB field-type
            # conflict (a field's type is locked on first write; a
            # later point sending a different Python type for the same
            # field name, e.g. float vs the field's established int, is
            # rejected outright, not coerced). Without this try/except,
            # one such conflict crashes the whole sync run and no
            # further points get written at all - logging and
            # continuing means the rest of this run's data still lands.
            try:
                _write_client.write(bucket, org, p)
            except Exception as e:
                write_failures += 1
                logger.warning(f"Failed to write point (tags={row['tags']}, timestamp={row['timestamp']}): {e}")

    if write_failures:
        logger.warning(f"{write_failures} of {len(results)} point(s) failed to write this run - see warnings above for details")
    if skipped_future:
        logger.warning(f"{skipped_future} of {len(results)} point(s) skipped for having implausible future timestamps this run")