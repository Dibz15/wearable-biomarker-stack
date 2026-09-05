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
# pip install webdavclient3 influxdb-client loguru protobuf

import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

import fitparse
from loguru import logger
from webdav3.client import Client

import huami_pb2

from common.webdav import fetch_database, open_database
from common.devices import run_query, fetch_devices, device_tags_factory
from common.checkpoint import get_last_checkpoint_ns, ObservedTracker
from common.influx import build_client, write_results

### Config section

PARSER_SOURCE = os.getenv("PARSER_SOURCE", "activefit")

WEBDAV_URL = os.getenv("WEBDAV_URL", False)
WEBDAV_PATH = os.getenv("WEBDAV_PATH", "files/service_user/GadgetBridge/")
# Where Gadgetbridge's own "Auto export GPX/FIT tracks" automations
# (Settings -> Automations, a SEPARATE mechanism from the main
# Gadgetbridge.db auto-export - see the workout-detail extraction
# code's own module-level comment) write their per-workout export
# files. No universal default exists - this is wherever the person
# pointed those automations at when setting them up.
#
# NOT automatically relative to WEBDAV_PATH, despite the similar name -
# both are independent, FULL paths from the same WebDAV root (passed
# straight to webdav_client.list()/.download_sync() with no
# combination step - see fetch_database()'s own identical use of
# WEBDAV_PATH for the same pattern). If the export folder is a
# subfolder of WEBDAV_PATH itself (confirmed the common case in
# practice, e.g. a "Tracks/" folder picked from within the same
# Gadgetbridge sync location), WEBDAV_PATH's own prefix must be
# included explicitly too - e.g. WEBDAV_PATH="files/austin/GadgetBridge/"
# and a "Tracks" subfolder within it needs
# EXPORT_TRACKS_PATH="files/austin/GadgetBridge/Tracks/" in full, not
# just "Tracks/".
EXPORT_TRACKS_PATH = os.getenv("EXPORT_TRACKS_PATH", None)
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


def flatten_workout_summary(raw_summary_data: bytes) -> dict:
    ''' Decodes BASE_ACTIVITY_SUMMARY.RAW_SUMMARY_DATA (Zepp OS devices
    only - confirmed via ZeppOsActivitySummaryParser.java, 2026-09) and
    flattens it into a flat {field_name: value} dict ready to merge
    into an InfluxDB point's own "fields". Uses the REAL protobuf
    library (huami_pb2, generated from proto/huami.proto) rather than
    a hand-rolled decoder - see the commit history for why a hand-
    rolled version was built, tested, and then deliberately replaced:
    for a real, well-defined proto3 schema (as opposed to an ad-hoc
    binary format with no schema, like decode_sleep_session_blob()
    below), the official library is less code to maintain here and
    can't have a subtly-wrong wire-format bug of its own.

    Every scaling factor applied below is copied directly from
    ZeppOsActivitySummaryParser.java's own real parsing code (NOT the
    .proto file's own inline comments, which turned out to disagree in
    one case - see the module-level note near the imports on
    baseLatitude/baseLongitude specifically). Only fields that
    Gadgetbridge's own real parser actually reads and uses are
    extracted here - the .proto defines a couple of fields
    (Location.startTimestamp; Altitude.totalClimbing) that exist in
    the wire format but that Gadgetbridge's own code never reads
    (visibly marked "// TODO" in the Java source) - skipped here too,
    since there's no confirmed meaning to attach to them yet.

    Returns {} (not None) for a summary with no recognizable fields at
    all, so the caller can always safely merge this dict in without a
    None-check - an InfluxDB point just ends up with only its base
    fields (duration_s) if nothing else decoded.
    '''
    fields = {}
    try:
        version = raw_summary_data[0] | (raw_summary_data[1] << 8)
        if version != 0x8000:
            logger.warning(f"BASE_ACTIVITY_SUMMARY.RAW_SUMMARY_DATA: unexpected version "
                            f"0x{version:04x} (expected 0x8000) - attempting to parse anyway, "
                            f"matching Gadgetbridge's own tolerant behavior here")
        summary = huami_pb2.WorkoutSummary()
        summary.ParseFromString(raw_summary_data[2:])
    except Exception as e:
        logger.warning(f"BASE_ACTIVITY_SUMMARY.RAW_SUMMARY_DATA: failed to decode "
                        f"({len(raw_summary_data)} byte blob) - {e}")
        return fields

    if summary.HasField("type"):
        fields["activity_type_code"] = summary.type.type
        # summary.type.ai ("0 = normal, 1 = ai/automatic" per the
        # .proto's own comment) not extracted - Gadgetbridge's own
        # parser never reads it either.

    if summary.HasField("time"):
        fields["active_seconds"] = summary.time.workoutDuration
        fields["total_duration_s"] = summary.time.totalDuration
        fields["pause_duration_s"] = summary.time.pauseDuration

    if summary.HasField("heartRate"):
        fields["hr_avg"] = summary.heartRate.avg
        fields["hr_max"] = summary.heartRate.max
        fields["hr_min"] = summary.heartRate.min

    if summary.HasField("steps"):
        fields["steps"] = summary.steps.steps
        fields["avg_stride_cm"] = summary.steps.avgStride
        # *60: steps/sec -> steps/min, exactly as
        # ZeppOsActivitySummaryParser.java's own addCadenceAvg/Max calls do.
        fields["avg_cadence_per_min"] = summary.steps.avgCadence * 60
        fields["max_cadence_per_min"] = summary.steps.maxCadence * 60

    if summary.HasField("distance"):
        fields["distance_m"] = summary.distance.distance

    if summary.HasField("count"):
        fields["total_jumps"] = summary.count.totalJumps

    if summary.HasField("calories"):
        fields["calories_kcal"] = summary.calories.calories

    if summary.HasField("frequency"):
        # Not scaled - used directly as a cadence in the Java source
        # (addCadenceAvg/Max with no multiplier), unlike Steps' own
        # avgCadence/maxCadence above which ARE *60'd there.
        fields["avg_frequency_per_min"] = summary.frequency.avgFrequency
        fields["max_frequency_per_min"] = summary.frequency.maxFrequency

    if summary.HasField("trainingEffect"):
        fields["aerobic_training_effect"] = summary.trainingEffect.aerobicTrainingEffect
        fields["anaerobic_training_effect"] = summary.trainingEffect.anaerobicTrainingEffect
        fields["training_load"] = summary.trainingEffect.currentWorkoutLoad
        fields["vo2max"] = summary.trainingEffect.maximumOxygenUptake

    if summary.HasField("altitude"):
        # /200 and /100 confirmed directly from the Java source's own
        # division, not the .proto's "// cm" comments (elevationGain/
        # Loss) - the CODE divides those by 100 too, consistent with
        # cm->m, so both sources agree there; only totalClimbing is
        # skipped (Java: "// TODO totalClimbing" - never read).
        fields["altitude_max_m"] = summary.altitude.maxAltitude / 200
        fields["altitude_min_m"] = summary.altitude.minAltitude / 200
        fields["altitude_avg_m"] = summary.altitude.avgAltitude / 200
        fields["elevation_gain_m"] = summary.altitude.elevationGain / 100
        fields["elevation_loss_m"] = summary.altitude.elevationLoss / 100

    if summary.HasField("elevation"):
        fields["ascent_seconds"] = summary.elevation.uphillTime
        fields["descent_seconds"] = summary.elevation.downhillTime

    if summary.HasField("temperature"):
        fields["temperature_avg_c"] = summary.temperature.avg
        fields["temperature_max_c"] = summary.temperature.max
        fields["temperature_min_c"] = summary.temperature.min

    if summary.HasField("heartRateZones"):
        # Only trusted with exactly 6 zones (N/A, Warm-up, Fat-burn,
        # Aerobic, Anaerobic, Extreme) - the same guard
        # ZeppOsActivitySummaryParser.java's own code applies before
        # touching zoneTime at all ("Unexpected number of HR zones").
        zone_time = list(summary.heartRateZones.zoneTime)
        zone_max = list(summary.heartRateZones.zoneMax)
        zone_names = ["na", "warm_up", "fat_burn", "aerobic", "anaerobic", "extreme"]
        if len(zone_time) == 6:
            for name, seconds in zip(zone_names, zone_time):
                fields[f"hr_zone_{name}_seconds"] = seconds
        else:
            logger.warning(f"BASE_ACTIVITY_SUMMARY.RAW_SUMMARY_DATA: expected 6 HR zones, "
                            f"got {len(zone_time)} - skipping hr_zone_*_seconds fields for this row")

        # zoneMax is never read by Gadgetbridge's own parser (visibly
        # unused in ZeppOsActivitySummaryParser.java) - but it's a real
        # field in the schema, and decoding it directly against a real
        # workout confirmed it holds each zone's own upper BPM bound,
        # cross-checked exactly against that same workout's real Zepp
        # screenshot (zoneMax [106,132,144,152,162,178] matches the
        # shown 106-131/132-143/144-151/152-161/162-178 ranges exactly,
        # accounting for an inclusive/exclusive boundary convention -
        # zoneMax[i] is zone i's own upper bound; its lower bound is
        # zoneMax[i-1]). Genuinely useful precisely because the zone
        # SYSTEM/naming Zepp uses (max HR / heart rate reserve /
        # lactate threshold - person-configurable, confirmed changes
        # both the zone NAMES and the actual thresholds) isn't reported
        # anywhere else at all - these are the REAL computed thresholds
        # for THIS workout, correct regardless of which zone system was
        # active or what the person's own calibration inputs (e.g.
        # lactate threshold HR) were at the time, with no need to know
        # or reproduce that formula ourselves.
        if len(zone_max) == 6:
            for name, bpm in zip(zone_names, zone_max):
                fields[f"hr_zone_{name}_max_bpm"] = bpm
        else:
            logger.warning(f"BASE_ACTIVITY_SUMMARY.RAW_SUMMARY_DATA: expected 6 HR zone "
                            f"thresholds, got {len(zone_max)} - skipping hr_zone_*_max_bpm "
                            f"fields for this row")

    if summary.HasField("swimmingData"):
        sd = summary.swimmingData
        fields["laps"] = sd.laps
        fields["strokes"] = sd.strokes
        fields["swim_style_code"] = sd.style
        fields["stroke_rate_avg_per_min"] = sd.avgStrokeRate
        fields["stroke_rate_max_per_min"] = sd.maxStrokeRate
        fields["stroke_distance_avg_cm"] = sd.avgDps
        fields["swolf_index"] = sd.swolf
        if sd.laneLengthUnit in (0, 1):
            fields["lane_length"] = sd.laneLength
            fields["lane_length_unit"] = "meter" if sd.laneLengthUnit == 0 else "yard"

    if summary.HasField("pace"):
        # Raw values kept for transparency/debugging - and because the
        # scaling below is now CONFIRMED (2026-09) directly against a
        # real outdoor Walking workout's own known ground truth:
        # Gadgetbridge's own SUMMARY_DATA JSON (averageKMPaceSeconds:
        # 648.9649, maxPace: 0.568) AND Zepp's own displayed 3.45/3.94
        # mph both match exactly once converted (see below) - not a
        # guess anymore.
        fields["pace_avg_raw"] = summary.pace.avg
        fields["pace_best_raw"] = summary.pace.best

        # ZeppOsActivitySummaryParser.java applies a DIFFERENT scaling
        # for swim activities (*100 -> seconds-per-100m) than every
        # other activity kind (*1000 for avg -> seconds-per-km; best
        # used directly, unscaled -> seconds-per-m). Determining that
        # split from activity_type_code alone isn't possible (no
        # confirmed code->kind mapping exists in this codebase) - but
        # `summary.HasField("swimmingData")` is a reliable, DIRECT
        # proxy for the exact same branch: that field is only ever
        # populated for a genuine swim workout in the first place, so
        # checking it sidesteps needing the activity-code mapping at
        # all for this specific purpose.
        is_swim = summary.HasField("swimmingData")
        if is_swim:
            pace_avg_seconds = summary.pace.avg * 100  # seconds per 100m
            pace_best_seconds = summary.pace.best * 100
        else:
            pace_avg_seconds = summary.pace.avg * 1000  # seconds per km
            pace_best_seconds = summary.pace.best  # already seconds per m, unscaled
        # avg_speed_mps/max_speed_mps use the SAME unit (m/s) regardless
        # of activity kind, matching the per-sample workout-detail
        # fields' own speed_mps naming - a consumer doesn't need to
        # know which scaling branch applied to use these directly.
        if pace_avg_seconds:
            fields["avg_speed_mps"] = (100 if is_swim else 1000) / pace_avg_seconds
        if pace_best_seconds:
            fields["max_speed_mps"] = (100 if is_swim else 1) / pace_best_seconds

    if summary.HasField("movementEvaluation"):
        me = summary.movementEvaluation
        fields["movement_consistency"] = me.consistency
        fields["movement_stability"] = me.stability
        fields["movement_continuity"] = me.continuity
        fields["movement_rhythm"] = me.rhythm
        fields["movement_speed_decay"] = me.speedDecay

    return fields


# --- Per-sample workout detail (GPS/HR/cadence/etc.), separate from
# the per-workout SUMMARY numbers above ---
#
# Source priority, per the person's own explicit guidance: use
# whichever available source is richest, in this order:
#   1. FIT (BASE_ACTIVITY_SUMMARY.RAW_DETAILS_PATH's own real per-
#      sample data, exported via Gadgetbridge's "Auto export FIT
#      tracks" automation) - confirmed richest: works for GPS AND non-
#      GPS activities alike, and carries HR/cadence/power/temperature
#      alongside position, not just position.
#   2. GPX (the same automation's "Auto export GPX tracks" sibling) -
#      only exists for GPS-tracked activities at all, and even then
#      Gadgetbridge's own GPX export has not been confirmed to embed
#      anything beyond position/elevation/time (no HR/cadence
#      extension confirmed present - see parse_gpx_track_points()'s
#      own docstring).
#   3. Neither found: already handled by extract_base_activity_summary_rows()
#      itself doing nothing extra - the per-workout SUMMARY point
#      (duration/HR avg/etc, from RAW_SUMMARY_DATA) is written either
#      way, this per-sample detail is purely additive on top of it.
#
# Neither export type is INSIDE Gadgetbridge.db itself (confirmed via
# a real Gadgetbridge maintainer's own PR description, 2026-09) - both
# live as separate files in a person-chosen WebDAV-synced folder,
# correlated back to their own BASE_ACTIVITY_SUMMARY row by a shared
# TIMESTAMP embedded in both filenames (confirmed directly against two
# real examples - see find_workout_detail_export()'s own docstring).

WORKOUT_DETAIL_MEASUREMENT_SAMPLE_TYPE = "workout_detail"
WORKOUT_LAP_MEASUREMENT_SAMPLE_TYPE = "workout_lap"

# FIT field name -> (influx field name, optional transform function).
# Deliberately a known-fields allowlist (skip anything else) rather
# than writing every field FIT happens to carry - some FIT fields seen
# even in an official test fixture (e.g. "resistance", "power",
# "time_from_course") aren't populated by this device family at all
# and aren't worth carrying through untranslated.
FIT_RECORD_FIELD_MAP = {
    "heart_rate": ("hr", None),
    # Cadence's real meaning is device-dependent (some report full
    # steps/min, others report one leg's cycles/min, i.e. roughly half
    # actual steps/min) - stored as-is, under a name that doesn't
    # assert which convention this device uses, since that hasn't been
    # confirmed (see the person's own note that a 45-second test walk
    # isn't a reliable sample to judge this from either way).
    "cadence": ("cadence_rpm", None),
    "distance": ("distance_m", None),
    # enhanced_* are FIT's own newer, higher-precision fields for the
    # same measurement - preferred over the plain versions when a
    # record has both (handled in flatten_fit_records() itself, not
    # here, since it requires seeing both keys on the same record).
    "enhanced_altitude": ("altitude_m", None),
    "altitude": ("altitude_m", None),
    "enhanced_speed": ("speed_mps", None),
    "speed": ("speed_mps", None),
    "temperature": ("temperature_c", None),
    "power": ("power_watts", None),
    "step_length": ("step_length_mm", None),
    "grade": ("grade_percent", None),
    # FIT's own "semicircles" coordinate encoding - a real, documented
    # unit (not a guess): degrees = semicircles * (180 / 2**31).
    "position_lat": ("latitude", lambda v: v * (180 / 2**31)),
    "position_long": ("longitude", lambda v: v * (180 / 2**31)),
}


def datetime_to_nanos(dt: datetime) -> int:
    ''' Converts a Python datetime (from fitparse's own FIT timestamp
    decoding, or this module's own GPX <time> parsing below) to
    nanoseconds-since-epoch - the SAME "timestamp is always an int"
    convention every other point in this entire parser (both
    activefit and colmi) already follows via to_nanos(), and the only
    format write_results() itself actually accepts (it compares
    row['timestamp'] directly against an int future-timestamp bound
    before ever reaching Point.time() - a real crash found and fixed
    here: `datetime.datetime(...) > int` raises TypeError, discovered
    only once this code ran for real against actual synced data, not
    caught by this module's own earlier tests, which never exercised
    the actual write_results() integration boundary at all).

    FIT's own timestamps come back from fitparse as NAIVE datetimes
    (no tzinfo) that represent UTC (FIT's own spec: seconds since a
    2010-01-01T00:00:00 UTC epoch - confirmed directly against a real
    fitparse-decoded example). A naive datetime is therefore assumed
    UTC here, NOT the host's own local timezone - which is Python's
    own default assumption for a naive datetime's .timestamp() call,
    and would otherwise silently shift every FIT-derived sample by
    the host's own UTC offset. A genuinely timezone-AWARE datetime
    (this module's own GPX parsing always produces one, from a real
    "Z"/UTC-suffixed <time> element) is respected as given, not
    reinterpreted.
    '''
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def flatten_fit_records(fit_messages) -> list[dict]:
    ''' Converts a parsed FIT file's own "record" messages (fitparse's
    own message objects, one per FIT sample - typically one every 1-2
    seconds during a workout, confirmed directly against real
    exported .fit files) into a list of {"timestamp": <datetime>,
    "fields": {...}} dicts - NOT the full {"timestamp","fields","tags"}
    shape write_results() wants, since the caller (extract_data())
    still needs to attach the SAME workout-linking tags to every one
    of these before that.

    Only fields in FIT_RECORD_FIELD_MAP are extracted - anything else
    a record happens to carry is ignored (see that map's own comment
    for why). A record with NONE of the known fields present (fitparse
    itself confirms this really happens - the very first record of a
    real file can carry only a timestamp, before any sensor value has
    been sampled yet) is skipped entirely, matching this codebase's
    existing "an InfluxDB point needs at least one field" rule.

    Prefers enhanced_altitude/enhanced_speed over their plain
    altitude/speed counterparts when a record has both (FIT's own
    higher-precision versions of the same measurement) - never writes
    both under the same influx field name for one record.
    '''
    results = []
    for message in fit_messages:
        if message.name != "record":
            continue
        timestamp = message.get_value("timestamp")
        if timestamp is None:
            continue

        raw_values = {}
        for field in message:
            if field.value is not None:
                raw_values[field.name] = field.value

        fields = {}
        # enhanced_* first, so the plain fallback never overwrites it
        # if a record happens to carry both under the same influx name.
        for fit_name in ("enhanced_altitude", "altitude", "enhanced_speed", "speed"):
            if fit_name in raw_values:
                influx_name, transform = FIT_RECORD_FIELD_MAP[fit_name]
                if influx_name not in fields:
                    fields[influx_name] = transform(raw_values[fit_name]) if transform else raw_values[fit_name]
        for fit_name, (influx_name, transform) in FIT_RECORD_FIELD_MAP.items():
            if fit_name in ("enhanced_altitude", "altitude", "enhanced_speed", "speed"):
                continue  # already handled above
            if fit_name in raw_values:
                fields[influx_name] = transform(raw_values[fit_name]) if transform else raw_values[fit_name]

        if not fields:
            continue
        results.append({"timestamp": timestamp, "fields": fields})
    return results


# FIT lap field name -> (influx field name, optional transform). A
# "lap" is a per-SEGMENT summary (one per manual lap press or auto-
# segment, confirmed both Hybrid Training's and Walking's own real Zepp
# screenshots show a small number of these per workout, not a per-
# sample rate) - a genuinely different message type from "record"
# above, confirmed present in real exported .fit files (both the
# person's own real Yoga/Walking exports and fitparse's own official
# test fixture all contain real "lap" messages).
FIT_LAP_FIELD_MAP = {
    "avg_heart_rate": ("avg_hr", None),
    "max_heart_rate": ("max_hr", None),
    "avg_cadence": ("avg_cadence_rpm", None),
    "max_cadence": ("max_cadence_rpm", None),
    "total_distance": ("distance_m", None),
    "total_calories": ("calories_kcal", None),
    # total_timer_time (active/moving time, pauses excluded) preferred
    # over total_elapsed_time (real wall-clock time, pauses included) -
    # matches this app's own existing "active_seconds" convention
    # elsewhere (workout summary's own activeSeconds vs totalDuration).
    "total_timer_time": ("duration_s", None),
    "enhanced_avg_speed": ("avg_speed_mps", None),
    "avg_speed": ("avg_speed_mps", None),
    "enhanced_max_speed": ("max_speed_mps", None),
    "max_speed": ("max_speed_mps", None),
    "total_ascent": ("ascent_m", None),
    "total_descent": ("descent_m", None),
}


def flatten_fit_laps(fit_messages) -> list[dict]:
    ''' Converts a parsed FIT file's own "lap" messages into the same
    [{"timestamp": <datetime>, "fields": {...}}, ...] shape
    flatten_fit_records() produces for per-sample data - a SEPARATE,
    much coarser-grained series (one point per lap, not per sample),
    meant to be written to its own sample_type (see
    WORKOUT_LAP_MEASUREMENT_SAMPLE_TYPE), not merged into
    workout_detail's own per-sample points.

    Same enhanced_*-preferred-over-plain rule as flatten_fit_records()
    for avg/max speed. `lap_number` is 1-indexed (FIT's own
    message_index is 0-indexed) to match both Zepp's and Gadgetbridge's
    own real "No. 1, 2, ..." lap numbering seen directly in their
    screenshots. A lap with none of the known fields present (should
    be rare - a real lap always carries at least a duration - but
    handled the same defensively as an empty record) is skipped.
    '''
    results = []
    for message in fit_messages:
        if message.name != "lap":
            continue
        start_time = message.get_value("start_time")
        if start_time is None:
            continue

        raw_values = {}
        for field in message:
            if field.value is not None:
                raw_values[field.name] = field.value

        fields = {}
        for fit_name in ("enhanced_avg_speed", "avg_speed", "enhanced_max_speed", "max_speed"):
            if fit_name in raw_values:
                influx_name, transform = FIT_LAP_FIELD_MAP[fit_name]
                if influx_name not in fields:
                    fields[influx_name] = transform(raw_values[fit_name]) if transform else raw_values[fit_name]
        for fit_name, (influx_name, transform) in FIT_LAP_FIELD_MAP.items():
            if fit_name in ("enhanced_avg_speed", "avg_speed", "enhanced_max_speed", "max_speed"):
                continue
            if fit_name in raw_values:
                fields[influx_name] = transform(raw_values[fit_name]) if transform else raw_values[fit_name]

        message_index = message.get_value("message_index")
        if message_index is not None:
            fields["lap_number"] = message_index + 1

        if not fields:
            continue
        results.append({"timestamp": start_time, "fields": fields})
    return results


def parse_gpx_track_points(gpx_bytes: bytes) -> list[dict]:
    ''' Parses a GPX (plain XML) file's own <trkpt> elements into the
    same [{"timestamp": <datetime>, "fields": {...}}, ...] shape
    flatten_fit_records() produces - the GPX fallback path, used only
    when no matching .fit export exists for a workout (FIT is
    confirmed richer - see this module's own comment above).

    Uses Python's stdlib xml.etree.ElementTree, not a new dependency -
    GPX's own <trkpt lat="..." lon="..."><ele>...</ele><time>...</time></trkpt>
    structure is simple enough not to need a dedicated GPX library.

    Only lat/lon/elevation/time are extracted - Gadgetbridge's own GPX
    export has NOT been confirmed to embed HR/cadence via the
    <gpxtpx:TrackPointExtension> namespace some other tools use (an
    unconfirmed possibility, not assumed present or absent without a
    real file to check) - if a real Gadgetbridge-exported GPX file
    turns out to carry that extension too, this function would need
    extending to read it, not something to guess into existence now.
    '''
    import xml.etree.ElementTree as ET

    root = ET.fromstring(gpx_bytes)
    # GPX's default namespace makes every tag come back as
    # "{http://www.topografix.com/GPX/1/1}trkpt" etc - stripping to the
    # local tag name (after the last "}") rather than hardcoding one
    # specific namespace URI, which could differ across GPX versions/
    # generators.
    def local_tag(elem):
        return elem.tag.rsplit("}", 1)[-1]

    results = []
    for trkpt in root.iter():
        if local_tag(trkpt) != "trkpt":
            continue
        lat = trkpt.get("lat")
        lon = trkpt.get("lon")
        fields = {}
        if lat is not None and lon is not None:
            fields["latitude"] = float(lat)
            fields["longitude"] = float(lon)
        timestamp = None
        for child in trkpt:
            tag = local_tag(child)
            if tag == "ele" and child.text:
                fields["altitude_m"] = float(child.text)
            elif tag == "time" and child.text:
                timestamp = datetime.fromisoformat(child.text.replace("Z", "+00:00"))
        if timestamp is None or not fields:
            continue
        results.append({"timestamp": timestamp, "fields": fields})
    return results


def find_workout_detail_export(export_entries: list[str], raw_details_path: str) -> tuple[str, str] | None:
    ''' Given a WebDAV directory listing (export_entries - just
    filenames/paths as returned by the webdav client's own .list())
    and a workout's own RAW_DETAILS_PATH, finds the matching export
    file by the shared TIMESTAMP embedded in both filenames - NOT by
    the (confirmed unreliable - see flatten_workout_summary's sibling
    docstring) GPX_TRACK database column.

    Confirmed directly against two real examples: RAW_DETAILS_PATH's
    own basename ("2026-09-05T17_05_33+01_00.bin") matched a real GPX
    export ("2026-09-05T17_05_33+01_00-walking.gpx"), and separately
    ("2026-09-05T17_29_36+01_00.bin") matched a real FIT export
    ("2026-09-05T17_29_36+01_00-yoga.fit") - only an activity-type
    suffix and the extension differ from RAW_DETAILS_PATH's own
    basename either way.

    Returns (matched_filename, "fit"|"gpx") for whichever export
    exists, PREFERRING fit over gpx when both are present for the same
    workout (fit is confirmed richer - see this section's own top-of-
    file comment) - or None if neither is found.
    '''
    from pathlib import Path
    if not raw_details_path:
        return None
    timestamp_prefix = Path(raw_details_path).name.rsplit(".", 1)[0]
    matches = {Path(e).name.rsplit(".", 1)[-1].lower(): Path(e).name
               for e in export_entries if Path(e).name.startswith(timestamp_prefix)}
    if "fit" in matches:
        return matches["fit"], "fit"
    if "gpx" in matches:
        return matches["gpx"], "gpx"
    return None


def get_already_processed_workout_starts(client, bucket, measurement, user, source) -> set[str]:
    ''' Which workouts (by their own BASE_ACTIVITY_SUMMARY START_TIME,
    as a string tag) already have per-sample workout_detail points
    written - queried directly from InfluxDB itself (same "ask the
    destination what it already has" approach as
    get_last_checkpoint_ns, not a separate local tracking file) so a
    workout's own (potentially large) .fit/.gpx export is downloaded
    and parsed AT MOST ONCE ever, rather than being re-fetched every
    sync cycle for as long as it remains within the checkpoint window.

    A workout that has ANY workout_detail points at all counts as
    fully processed - this parser only ever writes a workout's detail
    points in one single pass (see extract_workout_detail_points()),
    so partial-write states aren't a real scenario worth detecting
    here.
    '''
    query_api = client.query_api()
    flux = f'''
    from(bucket: "{bucket}")
      |> range(start: -365d)
      |> filter(fn: (r) => r._measurement == "{measurement}")
      |> filter(fn: (r) => r.sample_type == "{WORKOUT_DETAIL_MEASUREMENT_SAMPLE_TYPE}")
      |> filter(fn: (r) => r.user == "{user}")
      |> filter(fn: (r) => r.source == "{source}")
      |> keep(columns: ["workout_start_time"])
      |> distinct(column: "workout_start_time")
    '''
    try:
        tables = query_api.query(flux)
    except Exception as e:
        logger.warning(f"Could not query already-processed workout details (treating as none processed yet): {e}")
        return set()
    return {record.get_value() for table in tables for record in table.records}


def extract_workout_detail_points(webdav_client, export_tracks_path, base_activity_rows, already_processed_starts, device_tags) -> list[dict]:
    ''' For every BASE_ACTIVITY_SUMMARY row not already covered by
    already_processed_starts, looks for a matching FIT (preferred) or
    GPX export in export_tracks_path (see find_workout_detail_export's
    own docstring for the correlation approach), downloads + parses
    whichever is found, and returns InfluxDB points for BOTH per-
    sample data (sample_type "workout_detail", one point per sample -
    NOT one point per workout, unlike every other section of this
    parser) AND, when the export is FIT specifically (GPX has no
    confirmed lap equivalent), per-lap summaries (sample_type
    "workout_lap", one point per lap - see flatten_fit_laps()). Both
    carry a `workout_start_time` tag (the parent workout's own
    START_TIME, as a string) linking them back to that workout's own
    summary point, so a frontend can query "every sample/lap for this
    specific workout" directly.

    A row with no matching export at all is silently skipped here -
    NOT an error, and not this function's concern to log loudly about,
    since the person may simply not have GPX/FIT export enabled for
    every device, or a given workout may predate enabling it (both
    real, confirmed-observed situations already, not hypothetical) -
    that workout's own SUMMARY point (from RAW_SUMMARY_DATA, written
    by extract_base_activity_summary_rows regardless) is already the
    best available data for it, exactly per the person's own "fill in
    what we can from the Gadgetbridge DB otherwise" guidance.
    '''
    if not export_tracks_path:
        return []

    try:
        export_entries = webdav_client.list(export_tracks_path)
    except Exception as e:
        logger.warning(f"Could not list EXPORT_TRACKS_PATH ({export_tracks_path!r}) - skipping "
                        f"workout detail extraction for this cycle: {e}")
        return []

    results = []
    for row in base_activity_rows:
        start_time, device_id, raw_details_path = row["start_time"], row["device_id"], row["raw_details_path"]
        start_time_str = str(start_time)
        if start_time_str in already_processed_starts:
            continue
        match = find_workout_detail_export(export_entries, raw_details_path)
        if match is None:
            continue
        filename, kind = match
        remote_path = export_tracks_path + filename if not filename.startswith(export_tracks_path) else filename

        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = os.path.join(tmpdir, filename)
            try:
                webdav_client.download_sync(remote_path=remote_path, local_path=local_path)
            except Exception as e:
                logger.warning(f"Failed to download workout detail export {remote_path!r}: {e}")
                continue

            try:
                if kind == "fit":
                    fitfile = fitparse.FitFile(local_path)
                    # Materialized once (fitparse's own get_messages()
                    # is otherwise a single-pass generator) so both
                    # per-sample records AND laps can be extracted from
                    # the same parse, without re-reading the file.
                    fit_messages = list(fitfile.get_messages())
                    samples = flatten_fit_records(fit_messages)
                    laps = flatten_fit_laps(fit_messages)
                else:
                    with open(local_path, "rb") as f:
                        samples = parse_gpx_track_points(f.read())
                    laps = []  # GPX has no standard lap concept, and
                    # Gadgetbridge's own GPX export hasn't been
                    # confirmed to embed one via any extension either -
                    # not assumed present without a real file to check.
            except Exception as e:
                logger.warning(f"Failed to parse workout detail export {filename!r} ({kind}): {e}")
                continue

        if not samples and not laps:
            logger.info(f"Workout detail export {filename!r} ({kind}) parsed but yielded no usable samples or laps")
            continue

        device_specific_tags = device_tags(device_id)
        for sample in samples:
            results.append({
                "timestamp": datetime_to_nanos(sample["timestamp"]),
                "fields": sample["fields"],
                "tags": {
                    **device_specific_tags,
                    "workout_start_time": start_time_str,
                    "sample_type": WORKOUT_DETAIL_MEASUREMENT_SAMPLE_TYPE,
                    "source_format": kind,
                }
            })
        for lap in laps:
            results.append({
                "timestamp": datetime_to_nanos(lap["timestamp"]),
                "fields": lap["fields"],
                "tags": {
                    **device_specific_tags,
                    "workout_start_time": start_time_str,
                    "sample_type": WORKOUT_LAP_MEASUREMENT_SAMPLE_TYPE,
                    "source_format": kind,
                }
            })
        logger.info(f"Workout detail: extracted {len(samples)} sample(s) and {len(laps)} lap(s) "
                    f"from {filename!r} ({kind}) for workout starting {start_time}")

    return results


def extract_base_activity_summary_rows(rows, device_tags) -> list[dict]:
    ''' Turns raw BASE_ACTIVITY_SUMMARY rows (START_TIME, END_TIME,
    DEVICE_ID, NAME, ACTIVITY_KIND, BASE_LONGITUDE, BASE_LATITUDE,
    BASE_ALTITUDE, RAW_SUMMARY_DATA - in that column order, matching
    the SELECT in extract_data()) into the same {"timestamp", "fields",
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

    BASE_LONGITUDE/BASE_LATITUDE are extracted RAW (unscaled) - the
    .proto's own inline comment claims a /6000000 (/-6000000 for
    longitude) conversion to real coordinates, but
    ZeppOsActivitySummaryParser.java's real code stores these
    completely unscaled, with no confirmation anywhere that a
    downstream consumer applies that division either. Storing raw
    rather than guessing that scaling is real. BASE_ALTITUDE, in
    contrast, genuinely IS pre-scaled to meters already by the time it
    reaches this column (Gadgetbridge's own code divides by 2 - the
    SAME division flatten_workout_summary() applies when reading
    altitude straight from the blob - before calling setBaseAltitude()),
    so it's extracted as base_altitude_m directly, no further division
    applied here.

    RAW_SUMMARY_DATA (the richer per-workout breakdown) is decoded via
    flatten_workout_summary() when present and non-empty - see that
    function's own docstring for the full field list and every
    confirmed scaling factor. A row with no blob (SUMMARY_DATA/
    RAW_SUMMARY_DATA are both known to sometimes be entirely absent -
    see FIELD_RESEARCH.md) still gets its basic duration_s field, just
    without the richer breakdown.

    Rows with a missing END_TIME (an in-progress/unfinished workout, or
    a malformed row) are skipped with a warning, not written with a
    missing duration_s - an InfluxDB point needs at least one field,
    and a start/stop-time feature can't meaningfully show an entry with
    no stop time anyway.
    '''
    results = []
    for r in rows:
        start_time, end_time, device_id, name, activity_kind = r[0], r[1], r[2], r[3], r[4]
        base_longitude, base_latitude, base_altitude, raw_summary_data = r[5], r[6], r[7], r[8]
        if end_time is None:
            logger.warning(f"BASE_ACTIVITY_SUMMARY: row with no END_TIME "
                           f"(device_id={device_id}, start_time={start_time}) - skipping")
            continue
        row_ts = to_nanos(start_time, BASE_ACTIVITY_SUMMARY_TIMESTAMPS_ARE_MS)
        unit_divisor = 1000 if BASE_ACTIVITY_SUMMARY_TIMESTAMPS_ARE_MS else 1
        duration_s = (end_time - start_time) / unit_divisor

        fields = {"duration_s": duration_s}
        if base_longitude is not None:
            fields["base_longitude_raw"] = base_longitude
        if base_latitude is not None:
            fields["base_latitude_raw"] = base_latitude
        if base_altitude is not None:
            fields["base_altitude_m"] = base_altitude
        if raw_summary_data:
            fields.update(flatten_workout_summary(bytes(raw_summary_data)))
        else:
            # A genuinely distinct case from "blob present but failed to
            # decode" (which flatten_workout_summary() itself already
            # warns about) - this row simply has no RAW_SUMMARY_DATA at
            # all (NULL or empty). Logged explicitly rather than
            # silently, since from the outside (e.g. checking InfluxDB
            # afterward) the two cases look identical - only duration_s
            # written either way - and telling them apart matters for
            # debugging why a given workout has no rich breakdown.
            logger.info(f"BASE_ACTIVITY_SUMMARY: row has no RAW_SUMMARY_DATA at all "
                        f"(device_id={device_id}, start_time={start_time}, name={name!r}) - "
                        f"writing duration_s only, no richer breakdown available for this row")

        results.append({
            "timestamp": row_ts,
            "fields": fields,
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


def deduplicate_sleep_session_rows(decoded_rows: list[tuple]) -> list[tuple]:
    ''' Keeps only the most recently synced HUAMI_SLEEP_SESSION_SAMPLE
    row per (device, night). Real bug found and fixed here (2026-09),
    confirmed against real data: the watch can sync more than one
    session-summary blob for the SAME night (two real rows one minute
    apart, both describing the same sleep session with nearly-
    identical but not byte-identical durations - presumably the watch
    re-sending a refined summary shortly after the first). Each blob's
    own stage array gets turned into its own set of sleep_stage points,
    timestamped from that blob's OWN internal clock (timestamp_midnight),
    not the outer row's TIMESTAMP - so two near-duplicate blobs for the
    same night produce near-duplicate-but-not-identical stage
    timestamps, which InfluxDB stores as SEPARATE points rather than
    the second overwriting the first. Without this, get_sleep_stage_breakdown
    (which sums every sleep_stage point in a session's time window)
    silently summed both blobs' stage minutes together - confirmed
    against real reported values still showing inflated stage sums
    for a single, consistent device_id even after the earlier cross-
    device summing fix (a different, unrelated bug in the dashboard's
    own query, not this one).

    Identifies "the same night" by (device_id, the blob's own
    timestamp_midnight) - not the outer row TIMESTAMP, since that's
    exactly the field that legitimately differs between the duplicate
    rows. "Most recently synced" is decided by the outer row_ts (when
    Gadgetbridge actually received this particular blob), the same
    "most recent wins" principle find_last_completed_sleep_session
    already applies at query time - just applied here at write time
    instead, so duplicate stage data never reaches InfluxDB in the
    first place, rather than relying on every downstream query to
    correctly filter it back out.

    Takes a list of (row_ts, device_id, decoded) tuples (decoded being
    decode_sleep_session_blob()'s own return value - never None, the
    caller already filters those out before this), returns the same
    shape, deduplicated. Order of the returned list is not significant.
    '''
    best_by_night: dict[tuple, tuple] = {}
    for row_ts, device_id, decoded in decoded_rows:
        key = (device_id, decoded["timestamp_midnight"])
        existing = best_by_night.get(key)
        if existing is None or row_ts > existing[0]:
            best_by_night[key] = (row_ts, device_id, decoded)
    return list(best_by_night.values())


def extract_data(cur, client, webdav_client):
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
    # RAW_SUMMARY_DATA (the richer per-workout breakdown - HR zones,
    # training load, laps, etc.) is now decoded via flatten_workout_summary()
    # - confirmed against Gadgetbridge's own real source (both
    # proto/huami.proto and ZeppOsActivitySummaryParser.java, 2026-09),
    # not a guessed shape. SUMMARY_DATA (the plaintext JSON alternative)
    # is deliberately still NOT extracted - independently confirmed
    # (a Gadgetbridge maintainer, via a third-party blog post) that
    # Gadgetbridge strips this column before writing to the DB to save
    # space, so it's expected to be empty/null in practice; the real
    # content lives in RAW_SUMMARY_DATA only. BASE_LONGITUDE/
    # BASE_LATITUDE/BASE_ALTITUDE (separate, simple top-level columns,
    # no blob parsing needed) are also now extracted - see
    # extract_base_activity_summary_rows()'s own docstring for why only
    # BASE_ALTITUDE is stored pre-scaled to meters while the lat/lon
    # pair is stored raw.
    rows = run_query(cur, "BASE_ACTIVITY_SUMMARY",
        "SELECT START_TIME, END_TIME, DEVICE_ID, NAME, ACTIVITY_KIND, "
        "BASE_LONGITUDE, BASE_LATITUDE, BASE_ALTITUDE, RAW_SUMMARY_DATA, RAW_DETAILS_PATH "
        "FROM BASE_ACTIVITY_SUMMARY "
        f"WHERE START_TIME >= {base_activity_summary_query_start_bound_scaled} ORDER BY START_TIME ASC")
    if rows is not None:
        new_results = extract_base_activity_summary_rows(rows, device_tags)
        results.extend(new_results)
        for r in rows:
            observed.note(r[2], to_nanos(r[0], BASE_ACTIVITY_SUMMARY_TIMESTAMPS_ARE_MS))
        section_counts["base_activity_summary"] = len(rows)

    # --- Per-sample workout detail (GPS/HR/cadence/etc.), a SEPARATE,
    # opt-in enrichment layered on top of the summary points above -
    # see extract_workout_detail_points()'s own docstring for the full
    # source-priority reasoning (FIT preferred, GPX fallback, DB-only
    # summary if neither exists).
    #
    # Deliberately its OWN, UNBOUNDED query - NOT reusing `rows` above,
    # and NOT gated on `rows is not None`. A real bug found and fixed
    # here before it ever shipped correctly: this section originally
    # lived inside the `if rows is not None:` block, sharing the SAME
    # checkpoint-bounded query as the summary extraction above. But
    # that checkpoint is GLOBAL and shared across every section
    # (including the continuous per-minute activity stream, which
    # advances every single cycle) - so by the time a workout's detail
    # export becomes available (the person enabling Auto export well
    # after the workout itself already happened and was already
    # summarized in an earlier run), that workout's own START_TIME is
    # long since behind the checkpoint, `rows` comes back empty on
    # every subsequent cycle, and this whole section silently never
    # ran at all - not even once, with zero log output, exactly
    # matching a real report of "no workout_detail entries anywhere
    # AND no mention in the logs at all" after enabling the feature and
    # resyncing. Backfilling detail for an ALREADY-summarized workout
    # is the whole point of this feature, so it needs to look at every
    # workout regardless of the summary checkpoint's own position -
    # BASE_ACTIVITY_SUMMARY is confirmed genuinely sparse in practice
    # (FIELD_RESEARCH.md: a handful of rows even after real use, not
    # thousands), so querying it in full on every cycle is cheap; the
    # already-processed InfluxDB check inside
    # extract_workout_detail_points() is what actually prevents
    # redundant download/parse work, not this query's own bound.
    if EXPORT_TRACKS_PATH:
        detail_rows = run_query(cur, "BASE_ACTIVITY_SUMMARY",
            "SELECT START_TIME, DEVICE_ID, RAW_DETAILS_PATH FROM BASE_ACTIVITY_SUMMARY "
            "WHERE RAW_DETAILS_PATH IS NOT NULL ORDER BY START_TIME ASC")
        if detail_rows:
            already_processed = get_already_processed_workout_starts(
                client, INFLUXDB_BUCKET, INFLUXDB_MEASUREMENT, GADGETBRIDGE_USER, PARSER_SOURCE
            )
            base_activity_rows_for_details = [
                {"start_time": r[0], "device_id": r[1], "raw_details_path": r[2]}
                for r in detail_rows
            ]
            detail_results = extract_workout_detail_points(
                webdav_client, EXPORT_TRACKS_PATH, base_activity_rows_for_details, already_processed, device_tags
            )
            if detail_results:
                results.extend(detail_results)
                section_counts["workout_detail"] = len(detail_results)
    else:
        # A clear, low-noise signal that this feature is simply not
        # configured - NOT total silence (this section's own earlier,
        # real mistake): a person who just enabled Gadgetbridge's own
        # export automations and resynced, expecting to see this
        # section's own log line, deserves to know definitively
        # whether the parser even attempted anything, rather than
        # being left to guess between "not configured" and "configured
        # but broken" from an empty log.
        logger.debug("EXPORT_TRACKS_PATH not set - skipping workout detail extraction "
                     "(this is expected if Gadgetbridge's own Auto export GPX/FIT tracks "
                     "automations haven't been set up, or haven't been pointed at this "
                     "parser's own EXPORT_TRACKS_PATH env var yet)")

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
        # First pass: decode every row, skipping malformed/unpopulated
        # blobs (same as before). Deliberately NOT writing session/stage
        # points yet - deduplicate_sleep_session_rows() needs every
        # successfully-decoded row available at once to pick the
        # single most-recently-synced one per (device, night), not one
        # at a time as they're decoded.
        decoded_rows = []
        for r in rows:
            row_ts = to_nanos(r[0], HUAMI_SLEEP_SESSION_TIMESTAMPS_ARE_MS)
            device_id = r[1]
            decoded = decode_sleep_session_blob(bytes(r[2]) if r[2] is not None else None)
            if decoded is None:
                logger.warning(f"HUAMI_SLEEP_SESSION_SAMPLE: could not decode blob for a row "
                               f"(device_id={device_id}, timestamp={r[0]}) - too short, malformed, "
                               f"or an unpopulated placeholder session - skipping")
                continue
            decoded_rows.append((row_ts, device_id, decoded))

        deduped_count = len(decoded_rows)
        decoded_rows = deduplicate_sleep_session_rows(decoded_rows)
        if deduped_count != len(decoded_rows):
            logger.info(f"HUAMI_SLEEP_SESSION_SAMPLE: {deduped_count - len(decoded_rows)} "
                        f"duplicate same-night session row(s) discarded (kept the most "
                        f"recently synced blob per device/night)")

        for row_ts, device_id, decoded in decoded_rows:
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
        results = extract_data(cur, influx_client, webdav_client)
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