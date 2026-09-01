#!/usr/bin/env python3
"""
Checkpoint mechanics and the future-timestamp corruption guard - both
correct by physical necessity regardless of device: "resume from the
last synced sample" and "sensor data can't be recorded before it
happens" are true whether the sample came from a ring or a watch.

Multi-device checkpoint isolation
----------------------------------
Gadgetbridge keeps ONE database for every paired device, so a Colmi
parser instance and an Amazfit parser instance both read the same
export and both write sync_check points under the same InfluxDB
`user` tag. If checkpoint lookups were only scoped by `user`, a
brand-new device's parser (zero sync_check history of its own) would
inherit an *existing* device's checkpoint as if it were its own -
silently skipping that new device's entire backfill, since the lookup
would look like "already caught up" rather than "never synced".

The fix: every point written also carries a `source` tag identifying
which parser wrote it (e.g. "colmi", "activefit") - a stable identifier
known at parser-startup, unlike the physical `device` tag/name which
is only discovered by reading the DEVICE table per-run and may not
even be knowable yet on a device's very first sync. get_last_checkpoint_ns
filters on `source` (when given) so each parser instance only ever
resumes from its own history.
"""

import time

from loguru import logger


def get_last_checkpoint_ns(client, bucket, measurement, user, source=None) -> int | None:
    ''' Queries InfluxDB for the most recent `last_seen` value from our
    own sync_check points, so a run can resume from there instead of
    blindly re-querying "now - QUERY_DURATION" every single time.

    Searches a full year back so a checkpoint is found regardless of
    how long the container's been down - the actual catch-up distance
    is separately clamped by the caller (MAX_CATCHUP_SECONDS-style
    logic) once this returns, so searching widely here doesn't risk an
    unbounded resync on its own.

    Returns None if nothing is found (first run ever for this
    `source`, or the query itself failed) - callers should fall back
    to a QUERY_DURATION-based bound in that case.

    If multiple devices/series share this `source`, returns the
    MINIMUM of their checkpoints - conservative, so we don't skip data
    for whichever device happens to be furthest behind. Re-querying an
    already-synced device's recent data is harmless (InfluxDB
    overwrites identical points rather than duplicating them).

    `source` should be passed by every multi-device-aware caller - see
    module docstring for why omitting it risks a new device silently
    inheriting an unrelated device's checkpoint.
    '''
    query_api = client.query_api()
    source_filter = f'\n      |> filter(fn: (r) => r.source == "{source}")' if source is not None else ""
    flux = f'''
    from(bucket: "{bucket}")
      |> range(start: -365d)
      |> filter(fn: (r) => r._measurement == "{measurement}")
      |> filter(fn: (r) => r.sample_type == "sync_check")
      |> filter(fn: (r) => r.user == "{user}"){source_filter}
      |> filter(fn: (r) => r._field == "last_seen")
      |> max()
    '''
    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Could not query last checkpoint (treating as first run): {e}")
        return None

    values = [int(record.get_value()) for table in tables for record in table.records]
    if not values:
        logger.debug("No prior checkpoint found - this looks like the first run")
        return None

    return min(values)


def is_future_ns(ts_ns, max_future_tolerance_seconds, now_ns=None) -> bool:
    ''' True if ts_ns is far enough ahead of "now" that it can only be
    corrupted raw data, not a genuine future measurement. Shared by
    every device parser since "a sample can't be recorded before it
    happens" doesn't depend on which device produced it.
    '''
    now_ns = now_ns if now_ns is not None else time.time_ns()
    return ts_ns > now_ns + (max_future_tolerance_seconds * 1_000_000_000)


class ObservedTracker:
    ''' Tracks the most recent sample timestamp per device, used to
    compute the sync_check/last_seen checkpoint. Rejects (and warns
    about) any timestamp meaningfully in the future - real sensor data
    can't be recorded before it happens, so a future timestamp means
    the raw sample is corrupted. Without this guard, a single bad row
    would become "the most recent" checkpoint forever (nothing beats a
    future timestamp), and every subsequent sync would resume from a
    point in the future that never matches anything real again.

    This is a class (rather than the original closure-over-a-dict)
    purely so device-specific extraction code can hold one instance
    and call .note(...) per row without needing to also thread a
    mutable dict through every call site by hand.
    '''

    def __init__(self, max_future_tolerance_seconds):
        self.max_future_tolerance_seconds = max_future_tolerance_seconds
        self.observed = {}

    def note(self, device_id, row_ts):
        now_ns = time.time_ns()
        if is_future_ns(row_ts, self.max_future_tolerance_seconds, now_ns):
            hours_ahead = (row_ts - now_ns) / 1e9 / 3600
            logger.warning(
                f"Ignoring implausible future-dated sample for checkpoint purposes "
                f"(device={device_id}, {hours_ahead:.2f}h ahead of now) - likely "
                f"corrupted raw data. This sample's timestamp will not be allowed "
                f"to become the sync checkpoint."
            )
            return
        key = f"dev-{device_id}"
        if key not in self.observed or self.observed[key] < row_ts:
            self.observed[key] = row_ts
