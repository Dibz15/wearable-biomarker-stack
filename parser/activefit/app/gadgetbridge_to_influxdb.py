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

# UNVERIFIED - research suggests HUAMI_* tables use millisecond
# timestamps, same convention as COLMI_*, but this is NOT confirmed
# against a real Active 3 Premium export. See colmi/app's docstring
# for how COLMI_TIMESTAMPS_ARE_MS was actually confirmed (InfluxDB
# "value out of range" write errors, or implausible far-future
# graphed data, are the tell) - do the same check here once real data
# exists before trusting this default.
HUAMI_TIMESTAMPS_ARE_MS = os.getenv("HUAMI_TIMESTAMPS_ARE_MS", "Y") == "Y"

MAX_CATCHUP_SECONDS = int(os.getenv("MAX_CATCHUP_SECONDS", str(30 * 86400)))
CHECKPOINT_OVERLAP_SECONDS = int(os.getenv("CHECKPOINT_OVERLAP_SECONDS", "300"))
MAX_FUTURE_TOLERANCE_SECONDS = int(os.getenv("MAX_FUTURE_TOLERANCE_SECONDS", "300"))

### Config ends


def to_nanos(ts):
    if HUAMI_TIMESTAMPS_ARE_MS:
        return ts * 1000000
    return ts * 1000000000


def from_nanos(ns):
    if HUAMI_TIMESTAMPS_ARE_MS:
        return ns // 1000000
    return ns // 1000000000


def scaled_to_iso(ts_scaled) -> str:
    seconds = ts_scaled / 1000 if HUAMI_TIMESTAMPS_ARE_MS else ts_scaled
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


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

    if checkpoint_ns is not None:
        checkpoint_scaled = from_nanos(checkpoint_ns)
        unit_multiplier = 1000 if HUAMI_TIMESTAMPS_ARE_MS else 1
        overlap_scaled = CHECKPOINT_OVERLAP_SECONDS * unit_multiplier
        resume_bound_scaled = checkpoint_scaled - overlap_scaled

        min_allowed_scaled = (now_seconds - MAX_CATCHUP_SECONDS) * unit_multiplier

        if resume_bound_scaled < min_allowed_scaled:
            logger.warning(
                f"Checkpoint ({scaled_to_iso(checkpoint_scaled)}) is older than "
                f"MAX_CATCHUP_SECONDS ({MAX_CATCHUP_SECONDS}s) - clamping catch-up "
                f"window to {scaled_to_iso(min_allowed_scaled)}."
            )
            query_start_bound_scaled = min_allowed_scaled
        else:
            query_start_bound_scaled = resume_bound_scaled

        logger.info(
            f"Resuming from checkpoint at {scaled_to_iso(checkpoint_scaled)} - "
            f"querying from {scaled_to_iso(query_start_bound_scaled)} after "
            f"subtracting a {CHECKPOINT_OVERLAP_SECONDS}s overlap margin"
        )
    else:
        query_start_bound_scaled = fallback_bound_seconds * 1000 if HUAMI_TIMESTAMPS_ARE_MS else fallback_bound_seconds
        logger.info(
            f"No checkpoint found - using QUERY_DURATION fallback ({QUERY_DURATION}s), "
            f"querying from {scaled_to_iso(query_start_bound_scaled)}"
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
    # table doesn't exist. SLEEP/REM_SLEEP/DEEP_SLEEP columns are
    # confirmed to exist, but a real Gadgetbridge bug report (issue
    # #4715) observed REM_SLEEP and DEEP_SLEEP holding IDENTICAL values
    # on one device - don't trust the REM/deep split without checking
    # your own data.
    extended_query = (
        "SELECT TIMESTAMP, DEVICE_ID, RAW_KIND, STEPS, HEART_RATE, RAW_INTENSITY, "
        "SLEEP, REM_SLEEP, DEEP_SLEEP FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC"
    )
    rows = run_query(cur, "HUAMI_EXTENDED_ACTIVITY_SAMPLE", extended_query)
    activity_table_used = "HUAMI_EXTENDED_ACTIVITY_SAMPLE"

    if rows is None:
        basic_query = (
            "SELECT TIMESTAMP, DEVICE_ID, RAW_KIND, STEPS, HEART_RATE, RAW_INTENSITY "
            "FROM HUAMI_ACTIVITY_SAMPLE "
            f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC"
        )
        rows = run_query(cur, "HUAMI_ACTIVITY_SAMPLE", basic_query)
        activity_table_used = "HUAMI_ACTIVITY_SAMPLE"

    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
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
            results.append({
                "timestamp": row_ts,
                "fields": fields,
                "tags": {
                    **device_tags(r[1]),
                    "activity_kind": r[2],
                    "sample_type": "activity"
                }
            })
            observed.note(r[1], row_ts)
        section_counts[f"activity ({activity_table_used})"] = len(rows)

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
    # as a tag rather than decoded, since its exact encoding isn't
    # confirmed (Gadgetbridge's Zepp OS feature list documents both
    # "automatic and manual" stress measurements, so TYPE_NUM is
    # presumed to distinguish those, but the 0/1-or-other mapping isn't
    # verified). Keeping it raw as a tag means it can still be filtered
    # on in Grafana once its meaning is confirmed, without needing a
    # parser change to retroactively recover it. ---
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
            results.append({
                "timestamp": row_ts,
                "fields": fields,
                "tags": {**device_tags(r[1]), "stress_type_num": r[2]}
            })
            observed.note(r[1], row_ts)
        section_counts["stress"] = len(rows)

    # --- SpO2. CONFIRMED table/columns, including TYPE_NUM (same
    # automatic-vs-manual caveat as HUAMI_STRESS_SAMPLE.TYPE_NUM above). ---
    rows = run_query(cur, "HUAMI_SPO2_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, TYPE_NUM, SPO2 FROM HUAMI_SPO2_SAMPLE "
        f"WHERE TIMESTAMP >= {query_start_bound_scaled} ORDER BY TIMESTAMP ASC")
    if rows is not None:
        for r in rows:
            row_ts = to_nanos(r[0])
            results.append({
                "timestamp": row_ts,
                "fields": {"spo2": r[3]},
                "tags": {**device_tags(r[1]), "spo2_type_num": r[2]}
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

    # --- Sleep sessions: intentionally NOT attempted. HUAMI_SLEEP_SESSION_SAMPLE
    # is confirmed to exist (TIMESTAMP, DEVICE_ID, USER_ID, DATA BLOB) but
    # its per-night detail lives in that BLOB column, not queryable rows
    # the way COLMI_SLEEP_STAGE_SAMPLE is - decoding that blob format is
    # real reverse-engineering work this parser doesn't attempt yet. The
    # SLEEP/REM_SLEEP/DEEP_SLEEP columns pulled from
    # HUAMI_EXTENDED_ACTIVITY_SAMPLE above and the respiratory-rate table
    # are the only sleep-related data this parser currently extracts.

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