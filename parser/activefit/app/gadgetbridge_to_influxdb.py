#!/usr/bin/env python3
#
# Fetch a Gadgetbridge database export from a WebDAV URL and extract
# stats for an Amazfit device into InfluxDB - the HUAMI_* counterpart
# to ../../colmi/app/gadgetbridge_to_influxdb.py.
#
# STATUS: SCHEMA CONFIRMED, SEMANTICS NOT YET VERIFIED AGAINST REAL
# ACTIVE 3 PREMIUM DATA.
#
# Table and column names below are taken directly from a real
# Gadgetbridge schema dump (`sqlite3 Gadgetbridge.db .schema`, grepped
# for HUAMI_*) - not secondhand research. Every table/column this file
# queries is confirmed to exist. What's still UNKNOWN, because no real
# Active 3 Premium data has flowed through yet:
#   - Timestamp unit (ms vs s) - see HUAMI_TIMESTAMPS_ARE_MS below
#   - RAW_KIND / RAW_INTENSITY code meanings (device-specific, same
#     situation Colmi's activity_kind tag is in - stored raw, not
#     decoded)
#   - Whether SLEEP/REM_SLEEP/DEEP_SLEEP actually differ in practice -
#     a real Gadgetbridge bug report (issue #4715) observed REM_SLEEP
#     and DEEP_SLEEP holding IDENTICAL values on one device
#   - TYPE_NUM's meaning on HUAMI_STRESS_SAMPLE/HUAMI_SPO2_SAMPLE -
#     Gadgetbridge's Zepp OS feature list documents both "automatic and
#     manual" stress measurements and SpO2 monitoring, so TYPE_NUM is
#     presumed to distinguish those, but the actual 0/1 (or other)
#     encoding isn't confirmed
# See README.md for the full table-by-table status and the schema dump
# this was built against.
#
# This is deliberately written to run safely before the watch is even
# paired: every query goes through common.devices.run_query, which
# catches sqlite3.OperationalError and returns None rather than
# raising - so if a column turns out to differ after all (e.g. a
# Gadgetbridge version difference from the schema dump this was built
# against), that section is silently skipped this run, not a crash.
# Gadgetbridge's schema is fixed at app-install time for every
# supported device class, not created per-paired-device, so these
# tables already exist in a fresh export even before the watch is
# paired - they'll just have zero rows in the query window, which
# run_query and extract_data() both handle as a normal empty result,
# not an error. That's the point: this needs to be deployable NOW,
# running quietly alongside the existing Colmi parser, without either
# breaking anything or crash-looping the container - not deferred
# until the watch is physically paired.
#
# Once real HUAMI_* data starts flowing in, watch the logs for row
# counts per section and treat that as the starting point for the
# semantic verification pass described in README.md - a non-empty
# result confirms the table/column exists (already known from the
# schema dump) but does NOT by itself confirm the values mean what
# their names suggest (see the SLEEP/REM_SLEEP/DEEP_SLEEP caveat
# above).
#
# pip install webdavclient3 influxdb-client loguru

import os
import shutil
import sys
import time
from datetime import datetime, timezone

from loguru import logger
from webdav3.client import Client

from common.webdav import fetch_database, open_database
from common.devices import run_query, fetch_devices, device_tags_factory
from common.checkpoint import get_last_checkpoint_ns, ObservedTracker
from common.influx import build_client, write_results

### Config section

PARSER_SOURCE = os.getenv("PARSER_SOURCE", "activefit")

WEBDAV_URL = os.getenv("WEBDAV_URL", False)
WEBDAV_PATH = os.getenv("WEBDAV_PATH", "files/service_user/GadgetBridge/")
WEBDAV_USER = os.getenv("WEBDAV_USER", False)
WEBDAV_PASS = os.getenv("WEBDAV_PASS", False)
EXPORT_FILE = os.getenv("EXPORT_FILENAME", "Gadgetbridge.db")

QUERY_DURATION = int(os.getenv("QUERY_DURATION", "86400"))

INFLUXDB_URL = os.getenv("INFLUXDB_URL", False)
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN", "")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "")
INFLUXDB_MEASUREMENT = os.getenv("INFLUXDB_MEASUREMENT", "gadgetbridge")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "testing_db")

SLEEP_HOURS = os.getenv("SLEEP_HOURS", "0,1,2,3,4,5,6").split(",")
REMOVE_TEMP_DB = os.getenv("REMOVE_TEMP_DB", "Y")
GADGETBRIDGE_USER = os.getenv("GADGETBRIDGE_USER", "primary")

# CONFIRMED (Sept 2026, real Active 3 Premium data, via a live
# debugging session comparing InfluxDB query results against
# scripts/check_table_usage.py's row counts) - HUAMI_* and GENERIC_*
# tables use MILLISECOND timestamps, EXCEPT HUAMI_EXTENDED_ACTIVITY_SAMPLE
# specifically, which uses SECONDS (see HUAMI_ACTIVITY_TIMESTAMPS_ARE_MS
# below). This single flag now correctly covers every OTHER table this
# file queries (stress, SpO2, temperature, HRV, PAI, resting/max/manual
# HR) - confirmed because all of those successfully wrote real points to
# InfluxDB using this assumption, in the same run where the activity
# table (using the same assumption) silently returned zero rows despite
# having 248 real rows in SQLite.
HUAMI_TIMESTAMPS_ARE_MS = os.getenv("HUAMI_TIMESTAMPS_ARE_MS", "Y") == "Y"

# CONFIRMED SECONDS, not milliseconds - the opposite of every other
# table this file queries. Discovered via scripts/check_table_usage.py's
# timestamp-scale classifier: HUAMI_EXTENDED_ACTIVITY_SAMPLE had 248 real
# rows in SQLite, but zero corresponding points ever reached InfluxDB.
# Root cause: this file computed ONE query_start_bound_scaled (in
# milliseconds, per HUAMI_TIMESTAMPS_ARE_MS above) and reused it for
# every table's WHERE TIMESTAMP >= <bound> clause - but a seconds-scale
# TIMESTAMP compared against a milliseconds-scale bound is always
# false (the bound is ~1000x larger than any real seconds-scale value
# could be), so the SQL query itself returned zero rows for this table
# specifically, silently, with no error anywhere in the pipeline. Every
# other table in the same run used the same bound correctly, which is
# exactly why this stayed hidden - stress/SpO2/temperature/HRV/PAI all
# worked, making it look like a Grafana problem rather than a table-
# specific unit mismatch, until compared directly against SQLite row
# counts. This makes sense in hindsight: HUAMI_EXTENDED_ACTIVITY_SAMPLE
# extends the older MiBandActivitySample lineage (see Gadgetbridge PR
# #2837), which predates the newer dedicated sample tables and likely
# predates their millisecond convention too.
#
# This is deliberately a SEPARATE flag from HUAMI_TIMESTAMPS_ARE_MS,
# not a replacement for it - every other table this file queries is
# independently confirmed to be milliseconds (see above), so a single
# shared flag would have been wrong for one or the other regardless of
# which way it was set. Per-table timestamp unit flags, not a single
# global one, is the correct model for HUAMI_*-family tables - this is
# the same class of lesson COLMI_TIMESTAMPS_ARE_MS/DURATION-unit bugs
# already taught for Colmi, just discovered here via a live comparison
# against real data instead of an InfluxDB write-rejection error (the
# failure mode is different: a wrong-direction ms-as-seconds mistake
# overflows and gets rejected by InfluxDB loudly; this seconds-as-ms
# mistake instead filters everything out silently upstream of any
# write ever being attempted - worth remembering as a second, quieter
# failure shape for the same underlying class of bug).
HUAMI_ACTIVITY_TIMESTAMPS_ARE_MS = os.getenv("HUAMI_ACTIVITY_TIMESTAMPS_ARE_MS", "N") == "Y"

# CONFIRMED MILLISECONDS (Sept 2026, via scripts/check_table_usage.py's
# Scale column against real data: a raw value of 1788430801000 only
# decodes to a sane date - 2026-09-03T10:20:01Z - when treated as
# milliseconds; as seconds it's out of range entirely). This is the
# OUTER SQL TIMESTAMP column (the row's own primary-key timestamp,
# used for the WHERE clause query bound) - a DIFFERENT value from the
# blob's INTERNAL timestamps (timestampSession/timestampMidnight),
# which remain confirmed SECONDS from Gadgetbridge's source itself (see
# decode_sleep_session_blob() below - both get multiplied by 1000L to
# build a Java Date, the standard idiom for seconds-to-milliseconds
# conversion). These two being different units for the same table isn't
# a contradiction: the outer TIMESTAMP column is whatever Gadgetbridge's
# own row-writing code chose (matches the surrounding GENERIC_*/HUAMI_*
# tables' own milliseconds convention), while the blob's internal
# fields are raw values the WATCH itself encoded, independent of how
# Gadgetbridge stores the row - the same kind of split HUAMI_EXTENDED_ACTIVITY_SAMPLE's
# OWN outer TIMESTAMP column turned out to have (seconds) versus every
# other table's outer TIMESTAMP (milliseconds) - per-table/per-context
# verification, not a single assumption, is what actually holds up here.
HUAMI_SLEEP_SESSION_TIMESTAMPS_ARE_MS = os.getenv("HUAMI_SLEEP_SESSION_TIMESTAMPS_ARE_MS", "Y") == "Y"

# BASE_ACTIVITY_SUMMARY is a device-agnostic Gadgetbridge-native table
# (no HUAMI_/XIAOMI_/etc. prefix, unlike every table above), storing
# discrete workout/activity entries rather than a continuous per-minute
# stream. Its START_TIME/END_TIME scale was classified as milliseconds
# via scripts/check_table_usage.py against real data - but based on
# just the ONE row that existed at the time (see
# parser/activefit/FIELD_RESEARCH.md's "Workout/Activity summaries"
# entry), not the same exhaustive confirmation the flags above have.
# Worth re-checking once more real rows accumulate.
BASE_ACTIVITY_SUMMARY_TIMESTAMPS_ARE_MS = os.getenv("BASE_ACTIVITY_SUMMARY_TIMESTAMPS_ARE_MS", "Y") == "Y"

# CONFIRMED directly from Gadgetbridge's own source
# (HuamiSleepSessionSampleProvider.java, SleepStage.getType() docstring
# and asActivityKind()) - not inferred, not guessed. See
# decode_sleep_session_blob() for the full byte layout this came from.
HUAMI_SLEEP_STAGE_MAP = {4: "light", 5: "deep", 8: "rem", 7: "awake"}

# CONFIRMED directly from Gadgetbridge's own source
# (HuamiExtendedSampleProvider.java - the actual class backing
# HUAMI_EXTENDED_ACTIVITY_SAMPLE, fetched from master, Sept 2026).
# PARTIAL: only the codes named as constants in this specific file are
# included here. Real data has also shown 80, 88, 96, 112 as activity_kind
# values - these aren't defined in this file, so they're presumed to
# come from a parent/shared Huami constants class (not yet pulled) and
# are deliberately left unmapped rather than guessed. Unmapped values
# still get an explicit "unknown" activity_kind_label tag (see below)
# rather than omitting the tag - a present-vs-absent tag key would
# fragment InfluxDB series the same way NULL TYPE_NUM did for stress/
# SpO2 (see that fix's comment on HUAMI_STRESS_SAMPLE's extraction) -
# the raw numeric activity_kind tag is still there too either way, so
# no information is lost, just consistently tagged.
#
# TYPE_CUSTOM_DEEP_SLEEP/REM_SLEEP/AWAKE_SLEEP (121/122/123) are
# deliberately NOT included here even though they're defined in the
# same source file - confirmed (from postProcess(), same file) that
# Gadgetbridge assigns those ONLY in-memory at read/display time, by
# overlaying HuamiSleepSessionSampleProvider's already-decoded stages
# back onto activity samples purely for rendering - never writing them
# back to the RAW_KIND column in SQLite. Real exported data can only
# ever contain 120 (undifferentiated sleep) here, never 121-123, which
# is exactly why real stage detail has to come from
# HUAMI_SLEEP_SESSION_SAMPLE's BLOB (already decoded above) rather than
# this field - now confirmed by source, not just inferred from the
# earlier finding that these columns stay frozen all night.
HUAMI_ACTIVITY_KIND_MAP = {
    64: "outdoor_running",
    115: "not_worn",
    118: "charging",
    120: "sleep",
}


MAX_CATCHUP_SECONDS = int(os.getenv("MAX_CATCHUP_SECONDS", str(30 * 86400)))
CHECKPOINT_OVERLAP_SECONDS = int(os.getenv("CHECKPOINT_OVERLAP_SECONDS", "300"))
MAX_FUTURE_TOLERANCE_SECONDS = int(os.getenv("MAX_FUTURE_TOLERANCE_SECONDS", "300"))

### Config ends


def to_nanos(ts, is_ms=None):
    ''' Converts a raw TIMESTAMP value to nanoseconds for InfluxDB.
    `is_ms` defaults to HUAMI_TIMESTAMPS_ARE_MS (the common case) but
    callers dealing with a table on a different unit - currently just
    HUAMI_EXTENDED_ACTIVITY_SAMPLE, see HUAMI_ACTIVITY_TIMESTAMPS_ARE_MS
    above - must pass it explicitly.
    '''
    if is_ms is None:
        is_ms = HUAMI_TIMESTAMPS_ARE_MS
    if is_ms:
        return ts * 1000000
    return ts * 1000000000


def from_nanos(ns, is_ms=None):
    ''' Inverse of to_nanos() - see its docstring for `is_ms`. '''
    if is_ms is None:
        is_ms = HUAMI_TIMESTAMPS_ARE_MS
    if is_ms:
        return ns // 1000000
    return ns // 1000000000


def scaled_to_iso(ts_scaled, is_ms=None) -> str:
    ''' See to_nanos()'s docstring for `is_ms`. '''
    if is_ms is None:
        is_ms = HUAMI_TIMESTAMPS_ARE_MS
    seconds = ts_scaled / 1000 if is_ms else ts_scaled
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def compute_query_start_bound(checkpoint_ns, now_seconds, fallback_bound_seconds, is_ms, label):
    ''' Derives a scaled (raw-table-unit) query-start bound from the
    already-unit-agnostic checkpoint (nanoseconds, from InfluxDB), for
    a table using the given timestamp convention - applying the same
    overlap-subtraction and MAX_CATCHUP_SECONDS clamping logic
    regardless of which unit that table happens to use.

    This exists as its own function (rather than inlined once in
    extract_data() like colmi's single-scale equivalent) specifically
    because activefit now has two different per-table scales in play -
    see HUAMI_ACTIVITY_TIMESTAMPS_ARE_MS above - and both need this
    exact logic, just parameterized by `is_ms`. `label` is only used
    for clearer log lines when there's more than one bound in play in
    the same run.
    '''
    if checkpoint_ns is not None:
        checkpoint_scaled = from_nanos(checkpoint_ns, is_ms)
        unit_multiplier = 1000 if is_ms else 1
        overlap_scaled = CHECKPOINT_OVERLAP_SECONDS * unit_multiplier
        resume_bound_scaled = checkpoint_scaled - overlap_scaled

        min_allowed_scaled = (now_seconds - MAX_CATCHUP_SECONDS) * unit_multiplier

        if resume_bound_scaled < min_allowed_scaled:
            logger.warning(
                f"[{label}] Checkpoint ({scaled_to_iso(checkpoint_scaled, is_ms)}) is older than "
                f"MAX_CATCHUP_SECONDS ({MAX_CATCHUP_SECONDS}s) - clamping catch-up "
                f"window to {scaled_to_iso(min_allowed_scaled, is_ms)}."
            )
            bound_scaled = min_allowed_scaled
        else:
            bound_scaled = resume_bound_scaled

        logger.info(
            f"[{label}] Resuming from checkpoint at {scaled_to_iso(checkpoint_scaled, is_ms)} - "
            f"querying from {scaled_to_iso(bound_scaled, is_ms)} after "
            f"subtracting a {CHECKPOINT_OVERLAP_SECONDS}s overlap margin"
        )
    else:
        bound_scaled = fallback_bound_seconds * 1000 if is_ms else fallback_bound_seconds
        logger.info(
            f"[{label}] No checkpoint found - using QUERY_DURATION fallback ({QUERY_DURATION}s), "
            f"querying from {scaled_to_iso(bound_scaled, is_ms)}"
        )

    return bound_scaled


def extract_base_activity_summary_rows(rows, device_tags) -> list[dict]:
    ''' Turns raw BASE_ACTIVITY_SUMMARY rows (START_TIME, END_TIME,
    DEVICE_ID, NAME, ACTIVITY_KIND - in that column order, matching the
    SELECT in extract_data()) into the same {"timestamp", "fields",
    "tags"} dict shape every other section builds for write_results().

    `device_tags` is the same per-run closure extract_data() builds via
    device_tags_factory() - passed in explicitly rather than assumed
    available, since this function lives outside extract_data()'s own
    scope and has no other way to reach it.

    Pulled out as its own function (rather than inlined like most other
    sections) specifically so it's independently testable against
    hand-built rows, without needing to mock the whole extract_data()
    call chain - matching the same reasoning compute_query_start_bound()
    was already split out for.

    NAME is nullable in the schema - "Unset" for a missing value,
    matching the exact same sentinel device_tags() already uses for
    DEVICE.ALIAS (see parser/common/devices.py), not a new convention
    invented just for this table. ACTIVITY_KIND here is NOT assumed to
    share HUAMI_EXTENDED_ACTIVITY_SAMPLE's RAW_KIND code space (a
    different table, no confirmation either way) - kept as its own
    separate raw tag (activity_kind_summary), not run through
    HUAMI_ACTIVITY_KIND_MAP.

    Rows with a missing END_TIME (an in-progress/unfinished workout, or
    a malformed row) are skipped with a warning, not written with a
    missing duration_s - an InfluxDB point needs at least one field,
    and a start/stop-time feature can't meaningfully show an entry with
    no stop time anyway.
    '''
    results = []
    for r in rows:
        start_time, end_time, device_id, name, activity_kind = r[0], r[1], r[2], r[3], r[4]
        if end_time is None:
            logger.warning(f"BASE_ACTIVITY_SUMMARY: row with no END_TIME "
                           f"(device_id={device_id}, start_time={start_time}) - skipping")
            continue
        row_ts = to_nanos(start_time, BASE_ACTIVITY_SUMMARY_TIMESTAMPS_ARE_MS)
        unit_divisor = 1000 if BASE_ACTIVITY_SUMMARY_TIMESTAMPS_ARE_MS else 1
        duration_s = (end_time - start_time) / unit_divisor
        results.append({
            "timestamp": row_ts,
            "fields": {"duration_s": duration_s},
            "tags": {
                **device_tags(device_id),
                "name": "Unset" if name is None else name,
                "activity_kind_summary": "unknown" if activity_kind is None else activity_kind,
                "sample_type": "activity_summary",
            }
        })
    return results


def decode_sleep_session_blob(data: bytes):
    ''' Decodes HUAMI_SLEEP_SESSION_SAMPLE.DATA - a fixed-layout binary
    blob, not a general-purpose format. This is a direct Python port of
    Gadgetbridge's own HuamiSleepSessionSampleProvider.java (fetched
    2026-09, from master), NOT reverse-engineered from raw bytes - the
    byte offsets, field widths, and stage type codes below are all
    copied straight from that source, which is the same code Gadgetbridge
    itself uses to render the sleep graph that was independently
    confirmed (against the watch's own display and the Zepp app) to
    show real, correct stage-by-stage data. This is why it's trusted
    without the usual "UNVERIFIED" caveat this file gives everything
    else - it isn't a guess.

    Byte layout (offsets in decimal, from the Java source's hex literals):
      0x00 (0):    timestampSession   uint32  epoch SECONDS (confirmed:
                    Gadgetbridge does `new Date(timestampSession * 1000L)`)
      0x04 (4):    timestampMidnight  uint32  epoch seconds, midnight
                    boundary of the day in the user's timezone
      0x08 (8):    unknown, single byte, Gadgetbridge's own code just
                    comments "// 1" without using the value
      0x09 (9):    unknown, single byte, same "// 1" comment
      0x0a (10):   sleepStart         uint16  minutes-since-previous-
                    midnight (Gadgetbridge's own docstring hedges this
                    with a "?" - the CODE's arithmetic is unambiguous
                    even though the comment isn't, so the code is what
                    this follows)
      0x0c (12):   sleepEnd           uint16  same unit as sleepStart
      0x0d-0x14:   unused/unknown gap (7 bytes)
      0x15 (21):   avgHr              uint8
      0x16 (22):   score              uint8   (Gadgetbridge's own
                    computed sleep score, 0-100)
      0x17-0x53:   unused/unknown gap (61 bytes)
      0x54 (84):   numStages          uint8   how many of the fixed 100
                    stage slots below are actually populated
      0x55 (85):   unused/unknown (1 byte)
      0x56 (86):   stage array, exactly 100 slots x 5 bytes each (500
                    bytes total, slots beyond numStages are unused/zero):
                      +0 uint16  stage start (same minutes-since-
                                 previous-midnight unit as sleepStart)
                      +2 uint16  stage end (same unit) - NOT used by
                                 Gadgetbridge's own display logic
                                 (each stage's classification extends
                                 until the NEXT stage's start, not to
                                 its own end), kept here anyway since
                                 it's free and may be a useful sanity
                                 check
                      +4 uint8   stage type: 4=light, 5=deep, 8=rem,
                                 7=awake (any other value -> unknown)
      0x024a (586): totalRemMinutes   uint16
      0x024c (588): totalLightMinutes uint16
      0x024e (590): totalDeepMinutes  uint16
      0x0250 (592): totalWakeMinutes  uint16
      (blob ends at 0x0252 / 594 bytes total)

    Returns a dict, or None if `data` is too short to contain even the
    fixed-size header+stage-count (0x55 bytes) - some other malformed/
    truncated/future-format blob, logged and skipped by the caller via
    the same graceful-degradation pattern as everything else in this
    file, rather than raising and taking down the whole sync run. Also
    None if sleepStart/sleepEnd are left at the 0xFFFF "unset" firmware
    sentinel (see the check right after they're decoded, below) - a
    confirmed real pattern for backfilled placeholder sessions from
    before a device was paired, not a real night's sleep.
    '''
    if data is None or len(data) < 0x55:
        return None

    def u8(offset):
        return data[offset]

    def u16(offset):
        return int.from_bytes(data[offset:offset + 2], "little")

    def u32(offset):
        return int.from_bytes(data[offset:offset + 4], "little")

    timestamp_session = u32(0x00)
    timestamp_midnight = u32(0x04)
    sleep_start_min = u16(0x0a)
    sleep_end_min = u16(0x0c)
    avg_hr = u8(0x15)
    score = u8(0x16)

    # Real bug found and fixed here (2026-09), confirmed against real
    # data, not speculative: a backfilled/placeholder session (from
    # before the watch was actually paired - Gadgetbridge or the
    # watch's own firmware appears to write one when there's no real
    # data for a period, though the exact mechanism isn't confirmed)
    # left sleepStart/sleepEnd at their unpopulated firmware default,
    # 0xFFFF (65535) - the maximum value a uint16 can hold, a classic
    # "unset" sentinel. Naively computing a duration from that (as this
    # function used to) produces (65535 - 0) * 60 = 3,932,100 seconds -
    # confirmed to the exact second against a real reported value (was
    # displaying as "1092hr 15min" on the dashboard, appearing
    # identically on two different dates before the watch was owned).
    # Checked against EITHER field, not just sleep_end_min - defensive
    # against the same sentinel appearing on sleep_start_min instead in
    # some other unpopulated-session variant, not just the one pattern
    # actually observed.
    UINT16_UNSET_SENTINEL = 0xFFFF
    if sleep_start_min == UINT16_UNSET_SENTINEL or sleep_end_min == UINT16_UNSET_SENTINEL:
        logger.warning(f"Sleep session blob has an unpopulated sleepStart/sleepEnd "
                       f"(0xFFFF sentinel) - likely a backfilled placeholder for a period "
                       f"before the device was paired, not a real session. Skipping.")
        return None

    num_stages = u8(0x54)

    # Defensive cap: the blob only has room for 100 stage slots (500
    # bytes) before the summary totals begin - a numStages beyond that
    # would read into (and misinterpret) the totals fields. Not
    # expected from real Gadgetbridge-written data, but a firmware
    # quirk or a genuinely different blob layout on some other device/
    # version shouldn't be allowed to read out of bounds or corrupt
    # the totals.
    if num_stages > 100:
        logger.warning(f"Sleep session blob claims {num_stages} stages (max 100 fit in the "
                       f"fixed layout) - clamping to 100, may indicate a different blob "
                       f"format than what this was decoded against")
        num_stages = 100

    stages = []
    for i in range(num_stages):
        base = 0x56 + 5 * i
        if base + 5 > len(data):
            logger.warning(f"Sleep session blob truncated mid-stage-array (stage {i} of "
                           f"{num_stages}) - stopping stage extraction early for this session")
            break
        stage_start = u16(base)
        stage_end = u16(base + 2)
        stage_type = u8(base + 4)
        stages.append((stage_start, stage_end, stage_type))

    result = {
        "timestamp_session": timestamp_session,
        "timestamp_midnight": timestamp_midnight,
        "sleep_start_min": sleep_start_min,
        "sleep_end_min": sleep_end_min,
        "avg_hr": avg_hr,
        "score": score,
        "stages": stages,
        "total_rem_min": None,
        "total_light_min": None,
        "total_deep_min": None,
        "total_wake_min": None,
    }

    # Summary totals are optional - only present if the blob is the
    # full expected length. A shorter-but-still-valid-so-far blob still
    # yields session info + stages without these.
    if len(data) >= 0x0252:
        result["total_rem_min"] = u16(0x024a)
        result["total_light_min"] = u16(0x024c)
        result["total_deep_min"] = u16(0x024e)
        result["total_wake_min"] = u16(0x0250)

    return result


def extract_data(cur, client):
    ''' Query the database for data - see this file's module docstring
    and README.md for the unverified/best-effort status of every table
    queried here.
    '''
    results = []

    now_seconds = int(time.time())
    fallback_bound_seconds = now_seconds - QUERY_DURATION

    checkpoint_ns = get_last_checkpoint_ns(
        client, INFLUXDB_BUCKET, INFLUXDB_MEASUREMENT, GADGETBRIDGE_USER, source=PARSER_SOURCE
    )

    if checkpoint_ns is not None:
        now_ns_check = time.time_ns()
        if checkpoint_ns > now_ns_check + (MAX_FUTURE_TOLERANCE_SECONDS * 1_000_000_000):
            hours_ahead = (checkpoint_ns - now_ns_check) / 1e9 / 3600
            logger.warning(
                f"Checkpoint is {hours_ahead:.2f}h in the future - ignoring it and "
                f"falling back to QUERY_DURATION instead of resuming from an "
                f"impossible point in time."
            )
            checkpoint_ns = None

    # Two bounds, not one: HUAMI_EXTENDED_ACTIVITY_SAMPLE uses a
    # different timestamp scale (seconds) than every other table this
    # file queries (milliseconds) - see HUAMI_ACTIVITY_TIMESTAMPS_ARE_MS's
    # docstring in the config section above for how that was discovered.
    # Both derive from the same underlying checkpoint_ns (already
    # unit-agnostic, from InfluxDB), just scaled differently.
    query_start_bound_scaled = compute_query_start_bound(
        checkpoint_ns, now_seconds, fallback_bound_seconds, HUAMI_TIMESTAMPS_ARE_MS, "default"
    )
    activity_query_start_bound_scaled = compute_query_start_bound(
        checkpoint_ns, now_seconds, fallback_bound_seconds, HUAMI_ACTIVITY_TIMESTAMPS_ARE_MS, "activity"
    )
    sleep_session_query_start_bound_scaled = compute_query_start_bound(
        checkpoint_ns, now_seconds, fallback_bound_seconds, HUAMI_SLEEP_SESSION_TIMESTAMPS_ARE_MS, "sleep_session"
    )
    base_activity_summary_query_start_bound_scaled = compute_query_start_bound(
        checkpoint_ns, now_seconds, fallback_bound_seconds, BASE_ACTIVITY_SUMMARY_TIMESTAMPS_ARE_MS, "base_activity_summary"
    )

    devices = fetch_devices(cur)
    if devices is None:
        logger.error("Unable to fetch stats - DEVICE table missing or unreadable (empty/corrupt database export?)")
        return False

    device_tags = device_tags_factory(devices)
    observed = ObservedTracker(MAX_FUTURE_TOLERANCE_SECONDS)
    section_counts = {}

    # --- Activity (steps/HR/intensity, + sleep columns on newer Zepp OS
    # devices). CONFIRMED table/columns (real schema dump). Tries the
    # newer HUAMI_EXTENDED_ACTIVITY_SAMPLE first - falls back to the
    # older HUAMI_ACTIVITY_SAMPLE (no sleep columns, NOT present in the
    # schema dump this was verified against, kept only as a defensive
    # fallback for other users' older Huami devices) if the extended
    # table doesn't exist. Both queried with HUAMI_ACTIVITY_TIMESTAMPS_ARE_MS
    # (confirmed seconds, not milliseconds, for the extended table - see
    # its docstring in the config section; the fallback table is assumed
    # to share the same scale as part of the same older lineage, but
    # that assumption itself is unconfirmed since this device doesn't
    # populate it). SLEEP/REM_SLEEP/DEEP_SLEEP columns are confirmed to
    # exist, but a real Gadgetbridge bug report (issue #4715) observed
    # REM_SLEEP and DEEP_SLEEP holding IDENTICAL values on one device -
    # don't trust the REM/deep split without checking your own data.
    extended_query = (
        "SELECT TIMESTAMP, DEVICE_ID, RAW_KIND, STEPS, HEART_RATE, RAW_INTENSITY, "
        "SLEEP, REM_SLEEP, DEEP_SLEEP FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
        f"WHERE TIMESTAMP >= {activity_query_start_bound_scaled} ORDER BY TIMESTAMP ASC"
    )
    rows = run_query(cur, "HUAMI_EXTENDED_ACTIVITY_SAMPLE", extended_query)
    activity_table_used = "HUAMI_EXTENDED_ACTIVITY_SAMPLE"

    if rows is None:
        basic_query = (
            "SELECT TIMESTAMP, DEVICE_ID, RAW_KIND, STEPS, HEART_RATE, RAW_INTENSITY "
            "FROM HUAMI_ACTIVITY_SAMPLE "
            f"WHERE TIMESTAMP >= {activity_query_start_bound_scaled} ORDER BY TIMESTAMP ASC"
        )
        rows = run_query(cur, "HUAMI_ACTIVITY_SAMPLE", basic_query)
        activity_table_used = "HUAMI_ACTIVITY_SAMPLE"

    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0], HUAMI_ACTIVITY_TIMESTAMPS_ARE_MS)
            fields = {
                "steps": r[3],
                "heart_rate": r[4],
                "raw_intensity": r[5],
            }
            if len(r) > 6:
                # Extended table - columns confirmed to exist, semantics
                # unverified (see docstring above). Named "*_raw"
                # deliberately so these aren't confused with Colmi's
                # independently verified sleep_stage_* fields.
                if r[6] is not None:
                    fields["sleep_extended_raw"] = r[6]
                if r[7] is not None:
                    fields["sleep_rem_raw"] = r[7]
                if r[8] is not None:
                    fields["sleep_deep_raw"] = r[8]
            tags = {
                **device_tags(r[1]),
                "activity_kind": r[2],
                "activity_kind_label": HUAMI_ACTIVITY_KIND_MAP.get(r[2], "unknown"),
                "sample_type": "activity"
            }
            results.append({
                "timestamp": row_ts,
                "fields": fields,
                "tags": tags
            })
            observed.note(r[1], row_ts)
        section_counts[f"activity ({activity_table_used})"] = len(rows)

    # --- HRV. CONFIRMED table AND real data - discovered via
    # scripts/check_table_usage.py against actual synced watch data,
    # not schema-reading. Answers the original open question of where
    # HRV lives for this device: not a HUAMI_*-prefixed table at all,
    # but the cross-vendor GENERIC_HRV_VALUE_SAMPLE. Same shape and
    # field name as Colmi's own HRV extraction (TIMESTAMP, DEVICE_ID,
    # VALUE -> field "hrv", no extra tags) - this is exactly the
    # shared-field design point: same field name across devices, so
    # they compare directly once split apart by the ${device}
    # dashboard filter, rather than needing device-specific field names. ---
    rows = run_query(cur, "GENERIC_HRV_VALUE_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, VALUE FROM GENERIC_HRV_VALUE_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"hrv": r[2]},
                "tags": device_tags(r[1])
            })
            observed.note(r[1], row_ts)
        section_counts["hrv (GENERIC_HRV_VALUE_SAMPLE)"] = len(rows)

    # --- Temperature. CONFIRMED table AND real data (same discovery
    # path as HRV above). Same shape as Colmi's COLMI_TEMPERATURE_SAMPLE
    # and the same field/tag names, for the same shared-field reason. ---
    rows = run_query(cur, "GENERIC_TEMPERATURE_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, TEMPERATURE, TEMPERATURE_TYPE, TEMPERATURE_LOCATION "
        "FROM GENERIC_TEMPERATURE_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"temperature": r[2]},
                "tags": {
                    **device_tags(r[1]),
                    "temperature_type": r[3],
                    "temperature_location": r[4]
                }
            })
            observed.note(r[1], row_ts)
        section_counts["temperature (GENERIC_TEMPERATURE_SAMPLE)"] = len(rows)

    # --- Resting heart rate. CONFIRMED table/columns. ---
    rows = run_query(cur, "HUAMI_HEART_RATE_RESTING_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, HEART_RATE FROM HUAMI_HEART_RATE_RESTING_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"resting_heart_rate": r[2]},
                "tags": {**device_tags(r[1]), "sample_type": "resting_heart_rate"}
            })
            observed.note(r[1], row_ts)
        section_counts["resting_heart_rate"] = len(rows)

    # --- Max heart rate. CONFIRMED table/columns (same shape as resting HR). ---
    rows = run_query(cur, "HUAMI_HEART_RATE_MAX_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, HEART_RATE FROM HUAMI_HEART_RATE_MAX_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"max_heart_rate": r[2]},
                "tags": {**device_tags(r[1]), "sample_type": "max_heart_rate"}
            })
            observed.note(r[1], row_ts)
        section_counts["max_heart_rate"] = len(rows)

    # --- Manually-triggered heart rate readings (e.g. from the watch's
    # on-demand HR screen). CONFIRMED table/columns. ---
    rows = run_query(cur, "HUAMI_HEART_RATE_MANUAL_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, HEART_RATE FROM HUAMI_HEART_RATE_MANUAL_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"manual_heart_rate": r[2]},
                "tags": {**device_tags(r[1]), "sample_type": "manual_heart_rate"}
            })
            observed.note(r[1], row_ts)
        section_counts["manual_heart_rate"] = len(rows)

    # --- Stress. CONFIRMED table/columns, including TYPE_NUM - captured
    # as a tag (still raw, not decoded into a friendlier value at parse
    # time) so it stays filterable in Grafana without a parser change.
    # Meaning CONFIRMED via a deliberate cross-check (see
    # FIELD_RESEARCH.md's stress_type_num entry): three manual stress
    # readings taken in Zepp at known timestamps all showed
    # stress_type_num="0" when matched against this data.
    # stress_type_num: 0 = manual, 1 = automatic.
    #
    # TYPE_NUM is NULL for some rows (observed: the earliest couple
    # hours of a real export - likely an initial historical-backfill
    # sync that didn't populate it, unlike regular ongoing syncs which
    # do). A None tag VALUE and an ABSENT tag KEY are not the same
    # thing to InfluxDB - the client silently omits a tag entirely when
    # given None (confirmed directly: Point.tag(key, None) drops it
    # from the line protocol), which makes "has TYPE_NUM" vs "doesn't"
    # a structurally different series, not just a different value of
    # the same series - fragmenting Grafana panels into extra series
    # that don't represent anything meaningful. Same fix already used
    # for a NULL device ALIAS in common/devices.py: normalize to an
    # explicit sentinel string so every point shares the same tag KEY,
    # differing only in value. ---
    rows = run_query(cur, "HUAMI_STRESS_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, TYPE_NUM, STRESS FROM HUAMI_STRESS_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            fields = {"stress": r[3]}
            sample_epoch_s = r[0] / 1000 if HUAMI_TIMESTAMPS_ARE_MS else r[0]
            try:
                sample_hour = time.gmtime(sample_epoch_s).tm_hour
                if str(sample_hour) not in SLEEP_HOURS:
                    fields["stress_exc_sleep"] = r[3]
            except (OverflowError, OSError, ValueError):
                pass
            stress_type_num = "unknown" if r[2] is None else r[2]
            results.append({
                "timestamp": row_ts,
                "fields": fields,
                "tags": {**device_tags(r[1]), "stress_type_num": stress_type_num}
            })
            observed.note(r[1], row_ts)
        section_counts["stress"] = len(rows)

    # --- SpO2. CONFIRMED table/columns, including TYPE_NUM (same
    # NULL-vs-absent-tag normalization as HUAMI_STRESS_SAMPLE.TYPE_NUM
    # above). spo2_type_num meaning CONFIRMED independently (see
    # FIELD_RESEARCH.md), same convention as stress_type_num:
    # 0 = manual, 1 = automatic. ---
    rows = run_query(cur, "HUAMI_SPO2_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, TYPE_NUM, SPO2 FROM HUAMI_SPO2_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            spo2_type_num = "unknown" if r[2] is None else r[2]
            results.append({
                "timestamp": row_ts,
                "fields": {"spo2": r[3]},
                "tags": {**device_tags(r[1]), "spo2_type_num": spo2_type_num}
            })
            observed.note(r[1], row_ts)
        section_counts["spo2"] = len(rows)

    # --- Sleep respiratory rate. CONFIRMED table/columns. Distinct from
    # the SLEEP/REM_SLEEP/DEEP_SLEEP columns on the activity table above -
    # this is breathing rate during sleep, not a sleep-stage classifier. ---
    rows = run_query(cur, "HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, RATE FROM HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"sleep_respiratory_rate": r[2]},
                "tags": {**device_tags(r[1]), "sample_type": "sleep_respiratory_rate"}
            })
            observed.note(r[1], row_ts)
        section_counts["sleep_respiratory_rate"] = len(rows)

    # --- PAI (Personal Activity Intelligence) - a composite score
    # Zepp/Amazfit compute from sustained heart-rate-zone minutes.
    # CONFIRMED table/columns. All fields stored raw/as-is; PAI_TODAY
    # and PAI_TOTAL are presumably the headline numbers shown in the
    # Zepp app, with the LOW/MODERATE/HIGH breakdown as contributing
    # detail, but that split isn't independently confirmed here. ---
    rows = run_query(cur, "HUAMI_PAI_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, PAI_LOW, PAI_MODERATE, PAI_HIGH, "
        "TIME_LOW, TIME_MODERATE, TIME_HIGH, PAI_TODAY, PAI_TOTAL "
        "FROM HUAMI_PAI_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {
                    "pai_low": r[2],
                    "pai_moderate": r[3],
                    "pai_high": r[4],
                    "pai_time_low_min": r[5],
                    "pai_time_moderate_min": r[6],
                    "pai_time_high_min": r[7],
                    "pai_today": r[8],
                    "pai_total": r[9],
                },
                "tags": {**device_tags(r[1]), "sample_type": "pai"}
            })
            observed.note(r[1], row_ts)
        section_counts["pai"] = len(rows)

    # --- Pre-computed activity summaries (deliberately-started workouts,
    # not the continuous per-minute stream above). BASE_ACTIVITY_SUMMARY
    # is a device-agnostic Gadgetbridge-native table (no HUAMI_/XIAOMI_
    # prefix), confirmed present via scripts/check_table_usage.py against
    # real data - genuinely sparse in practice (1 row observed against
    # 2798 rows in the per-minute activity table over the same period),
    # since it's populated only for explicitly-started workout sessions,
    # not ambient daily movement (see parser/activefit/FIELD_RESEARCH.md's
    # "Workout/Activity summaries" entry for the full reasoning behind
    # that conclusion).
    #
    # Deliberately NOT extracting SUMMARY_DATA/RAW_SUMMARY_DATA (the
    # richer per-workout breakdown - HR zones, laps, etc.) here - their
    # actual content has never been inspected against a real row, so
    # writing extraction code against a guessed shape risks silently
    # extracting nothing or the wrong thing. NAME/START_TIME/END_TIME/
    # ACTIVITY_KIND are all simple, directly-typed columns needing no
    # such guessing, and are all this feature (the Activity page's
    # session list) actually needs - average heart rate is computed
    # downstream by wearable-events from the already-extracted per-
    # minute heart_rate field over each entry's [start, end) window,
    # not duplicated here.
    rows = run_query(cur, "BASE_ACTIVITY_SUMMARY",
        "SELECT START_TIME, END_TIME, DEVICE_ID, NAME, ACTIVITY_KIND "
        "FROM BASE_ACTIVITY_SUMMARY "
        f"WHERE START_TIME >= {base_activity_summary_query_start_bound_scaled} ORDER BY START_TIME ASC")
    if rows is not None:
        new_results = extract_base_activity_summary_rows(rows, device_tags)
        results.extend(new_results)
        for r in rows:
            observed.note(r[2], to_nanos(r[0], BASE_ACTIVITY_SUMMARY_TIMESTAMPS_ARE_MS))
        section_counts["base_activity_summary"] = len(rows)

    # --- Sleep sessions, decoded from the BLOB. CONFIRMED byte layout,
    # ported directly from Gadgetbridge's own HuamiSleepSessionSampleProvider.java
    # (see decode_sleep_session_blob()'s docstring for the full field-by-
    # field source). This is the REAL sleep-stage source for this device -
    # confirmed (against the watch's own display and the Zepp app) that
    # Gadgetbridge's sleep graph shows genuine stage transitions overnight,
    # while HUAMI_EXTENDED_ACTIVITY_SAMPLE's sleep_extended_raw/rem/deep
    # columns above were independently shown (via a live query spanning a
    # full night) to stay completely frozen for 9+ hours straight -
    # physiologically impossible for real stage tracking, so those columns
    # are NOT the real source and this table is.
    rows = run_query(cur, "HUAMI_SLEEP_SESSION_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, DATA FROM HUAMI_SLEEP_SESSION_SAMPLE "
        f"WHERE TIMESTAMP >= {sleep_session_query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    session_points = 0
    stage_points = 0
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0], HUAMI_SLEEP_SESSION_TIMESTAMPS_ARE_MS)
            device_id = r[1]
            decoded = decode_sleep_session_blob(bytes(r[2]) if r[2] is not None else None)
            if decoded is None:
                logger.warning(f"HUAMI_SLEEP_SESSION_SAMPLE: could not decode blob for a row "
                               f"(device_id={device_id}, timestamp={r[0]}) - too short or malformed, skipping")
                continue

            tags_base = device_tags(device_id)

            # Session-start reference point, in the blob's own (confirmed
            # seconds) internal clock - independent of whichever scale the
            # outer TIMESTAMP column turns out to use.
            midnight_prev = decoded["timestamp_midnight"] - 86400

            # --- Session summary point. Field names deliberately mirror
            # Colmi's own sleep_session fields (sleep_session_start,
            # sleep_session_wakeup, sleep_session_duration_s) for direct
            # cross-device comparison, same shared-field-name principle
            # used throughout this parser. avg_hr/score/total_*_duration_s
            # have no Colmi equivalent, so they're new, clearly-named fields.
            session_start_epoch_s = midnight_prev + decoded["sleep_start_min"] * 60
            session_end_epoch_s = midnight_prev + decoded["sleep_end_min"] * 60
            session_fields = {
                "sleep_session_start": session_start_epoch_s,
                "sleep_session_wakeup": session_end_epoch_s,
                "sleep_session_duration_s": session_end_epoch_s - session_start_epoch_s,
                "sleep_avg_hr": decoded["avg_hr"],
                "sleep_score": decoded["score"],
            }
            if decoded["total_rem_min"] is not None:
                session_fields["rem_sleep_total_duration_s"] = decoded["total_rem_min"] * 60
                session_fields["light_sleep_total_duration_s"] = decoded["total_light_min"] * 60
                session_fields["deep_sleep_total_duration_s"] = decoded["total_deep_min"] * 60
                session_fields["awake_sleep_total_duration_s"] = decoded["total_wake_min"] * 60
            results.append({
                "timestamp": to_nanos(session_start_epoch_s, is_ms=False),
                "fields": session_fields,
                "tags": {**tags_base, "sample_type": "sleep_session"}
            })
            observed.note(device_id, row_ts)
            session_points += 1

            # --- Per-stage timeline, same start/end-marker + dense-per-
            # minute-point pattern as Colmi's own sleep stage extraction,
            # so the same Grafana Sleep Stage Timeline panel works for
            # both devices unmodified. stage.end is captured but (matching
            # Gadgetbridge's own display logic) not used to bound this
            # stage's active window - each stage is treated as running
            # until the NEXT stage's start, exactly as Gadgetbridge itself
            # does in HuamiSleepSessionSampleProvider.getSleepStages().
            stages = decoded["stages"]
            for i, (stage_start_min, stage_end_min, stage_type) in enumerate(stages):
                stage_label = HUAMI_SLEEP_STAGE_MAP.get(stage_type, f"stage_{stage_type}")
                stage_start_epoch_s = midnight_prev + stage_start_min * 60
                # Next stage's start (or this session's own wakeup time for
                # the last stage) - matches Gadgetbridge's own model of
                # "each stage runs until the next one begins", not this
                # stage's own (unused-by-Gadgetbridge) end field.
                if i + 1 < len(stages):
                    next_start_min = stages[i + 1][0]
                else:
                    next_start_min = decoded["sleep_end_min"]
                stage_active_until_epoch_s = midnight_prev + next_start_min * 60
                duration_s = stage_active_until_epoch_s - stage_start_epoch_s
                if duration_s <= 0:
                    continue

                common_tags = {
                    **tags_base,
                    "sample_type": "sleep_stage",
                    "sleep_stage": stage_label,
                    "sleep_stage_raw": stage_type,
                }

                results.append({
                    "timestamp": to_nanos(stage_start_epoch_s, is_ms=False),
                    "fields": {
                        "sleep_stage_duration_s": duration_s,
                        f"{stage_label}_sleep_duration_s": duration_s,
                        "sleep_stage_active": 1,
                    },
                    "tags": common_tags,
                })
                # End marker 1s early, same reasoning as Colmi's own sleep
                # stage extraction: two points at the identical nanosecond
                # (this stage's end == next stage's start) leaves sort()
                # order undefined in a Grafana query, so end 1s early to
                # guarantee this always sorts before the next stage's start.
                results.append({
                    "timestamp": to_nanos(stage_active_until_epoch_s, is_ms=False) - 1_000_000_000,
                    "fields": {"sleep_stage_active": 0},
                    "tags": common_tags,
                })

                minutes = duration_s // 60
                for minute_offset in range(minutes):
                    point_ts = to_nanos(stage_start_epoch_s, is_ms=False) + (minute_offset * 60 * 1_000_000_000)
                    results.append({
                        "timestamp": point_ts,
                        "fields": {"sleep_stage_now": stage_label},
                        "tags": common_tags,
                    })
                stage_points += 1

    if session_points:
        section_counts["sleep_session (HUAMI_SLEEP_SESSION_SAMPLE)"] = session_points
        section_counts["sleep_stage (HUAMI_SLEEP_SESSION_SAMPLE)"] = stage_points

    now = time.time_ns()
    for device_key, row_ts in observed.observed.items():
        device_id = device_key.replace("dev-", "")
        row_age = now - row_ts
        row_age_hours = row_age / 1_000_000_000 / 3600
        if row_age_hours > 24:
            logger.warning(f"Device {devices.get(device_key, {}).get('name', device_key)}: "
                           f"last sample is {row_age_hours:.1f}h old")
        results.append({
            "timestamp": now,
            "fields": {
                "last_seen": row_ts,
                "last_seen_age": row_age
            },
            "tags": {
                **device_tags(device_id),
                "sample_type": "sync_check"
            }
        })

    if not results:
        logger.info(
            "No HUAMI_* data in this run's time window - expected before the "
            "watch is paired (tables already exist in the Gadgetbridge schema, "
            "just with zero rows), or if a table has been renamed since this "
            "was verified against a real schema dump. See README.md for the "
            "verification checklist."
        )

    logger.info(f"Extraction summary: {section_counts} | total points to write: {len(results)}")

    return results


if __name__ == "__main__":
    if not WEBDAV_URL:
        logger.error("WEBDAV_URL not set in environment")
        sys.exit(1)

    if not INFLUXDB_URL:
        logger.error("INFLUXDB_URL not set in environment")
        sys.exit(1)

    webdav_options = {
        "webdav_hostname": WEBDAV_URL,
        "webdav_login": WEBDAV_USER,
        "webdav_password": WEBDAV_PASS
    }

    webdav_client = Client(webdav_options)
    tempdir = fetch_database(webdav_client, WEBDAV_PATH, EXPORT_FILE)
    conn, cur = open_database(tempdir)

    with build_client(INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG) as influx_client:
        results = extract_data(cur, influx_client)
        # See colmi/app's __main__ for why False (fatal) and an empty
        # list (legitimately nothing to sync - the normal case before
        # pairing, or any quiet cycle after) are handled differently.
        # This distinction matters MORE here than for colmi: before
        # the watch is paired, every single cycle will legitimately
        # find zero HUAMI_* data, and treating that as failure would
        # crash-loop this container indefinitely under
        # `restart: unless-stopped`.
        if results is False:
            logger.error("Data extraction failed")
            sys.exit(1)

        if results:
            write_results(
                influx_client, results, INFLUXDB_BUCKET, INFLUXDB_ORG, INFLUXDB_MEASUREMENT,
                GADGETBRIDGE_USER, PARSER_SOURCE, MAX_FUTURE_TOLERANCE_SECONDS
            )
        else:
            logger.info("No new data points to sync this run")

    conn.close()
    if tempdir not in ["/", ""]:
        if REMOVE_TEMP_DB == "N":
            logger.debug(tempdir)
        else:
            shutil.rmtree(tempdir)