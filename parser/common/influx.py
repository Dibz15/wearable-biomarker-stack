#!/usr/bin/env python3
"""
InfluxDB client setup and the write path. Writing a list of already-
computed {timestamp, fields, tags} dicts as Points is entirely
device-agnostic - by the time results reach here, all the COLMI_* /
HUAMI_* specific extraction has already happened upstream.
"""

import time

from influxdb_client import InfluxDBClient, Point
from loguru import logger


def build_client(url, token, org) -> InfluxDBClient:
    return InfluxDBClient(url=url, token=token, org=org)


def write_results(client, results, bucket, org, measurement, user, source,
                   max_future_tolerance_seconds):
    ''' Write results to InfluxDB using the given (already-open) client.

    Every point gets a `user` tag (see GADGETBRIDGE_USER-equivalent
    config in each device app for why, even in a single-user setup)
    and, when `source` is given, a `source` tag identifying which
    parser wrote it (e.g. "colmi", "activefit") - this is what lets
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
