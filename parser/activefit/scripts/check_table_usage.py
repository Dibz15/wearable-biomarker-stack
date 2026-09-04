#!/usr/bin/env python3
'''
One-off diagnostic: fetches the current Gadgetbridge export (same
WebDAV fetch the parser itself uses) and reports row counts across
EVERY table actually present in the real database - not a maintained
list of "tables we thought might be relevant". Reading table names
directly from sqlite_master, rather than from parser/colmi/schema.txt
or a hand-curated CANDIDATE_TABLES list, means this can't drift out of
date and can't miss a table nobody thought to add - which is exactly
what happened to the previous version of this script: it never
included BASE_ACTIVITY_SUMMARY (a device-agnostic Gadgetbridge-native
table, not prefixed HUAMI_/XIAOMI_/etc. like everything it did check),
found later by reading the schema file by hand instead.

Not every table uses a column literally named TIMESTAMP - a few
(BASE_ACTIVITY_SUMMARY, ACTIVITY_DESCRIPTION, HEALTH_CONNECT_SLEEP_SESSION)
use START_TIME/END_TIME instead, and some tables (DEVICE, USER, TAG)
have no timestamp-like column at all. This checks each table's actual
columns via PRAGMA table_info and picks the best fit, rather than
assuming TIMESTAMP exists everywhere and silently erroring (or being
skipped) on every table that doesn't.

Run via the same pattern as before:

    docker cp parser/activefit/scripts/check_table_usage.py biomarker-parser-activefit:/tmp/check.py
    docker exec -it biomarker-parser-activefit python3 /tmp/check.py

Requires the same WEBDAV_* env vars the parser itself uses (already
set in the running container, so no extra config needed). Works
identically if copied into the colmi container instead - both parsers
read from the same shared Gadgetbridge export, so which container runs
this doesn't matter.
'''

import os
import shutil
import sys
import time

# /app/common holds the shared webdav/db helpers (see Dockerfile: COPY
# common /app/common), but this script itself lives in /scripts (a
# separate COPY destination) - or gets docker cp'd to an arbitrary path
# like /tmp per the usage note above. Either way, /app is a fixed,
# known location in this image, so add it explicitly rather than
# relying on whatever directory the script happens to be invoked from.
sys.path.insert(0, "/app")

from webdav3.client import Client

from common.webdav import fetch_database, open_database

WEBDAV_URL = os.getenv("WEBDAV_URL", False)
WEBDAV_PATH = os.getenv("WEBDAV_PATH", "files/service_user/GadgetBridge/")
WEBDAV_USER = os.getenv("WEBDAV_USER", False)
WEBDAV_PASS = os.getenv("WEBDAV_PASS", False)
EXPORT_FILE = os.getenv("EXPORT_FILENAME", "Gadgetbridge.db")

# Every table currently queried by EITHER parser (parser/colmi and
# parser/activefit both read from this same shared Gadgetbridge
# export) - printed distinctly below so it's obvious at a glance which
# non-zero tables are already wired up vs newly discovered. Kept as an
# explicit set rather than derived by parsing the parser source files
# here too - simpler, and this only needs updating on the rare occasion
# a table gets newly wired up, not something worth its own fragile
# source-parsing logic in a one-off diagnostic script.
ALREADY_IMPLEMENTED = {
    # parser/activefit
    "GENERIC_HRV_VALUE_SAMPLE",
    "GENERIC_TEMPERATURE_SAMPLE",
    "HUAMI_ACTIVITY_SAMPLE",
    "HUAMI_EXTENDED_ACTIVITY_SAMPLE",
    "HUAMI_HEART_RATE_MANUAL_SAMPLE",
    "HUAMI_HEART_RATE_MAX_SAMPLE",
    "HUAMI_HEART_RATE_RESTING_SAMPLE",
    "HUAMI_PAI_SAMPLE",
    "HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE",
    "HUAMI_SLEEP_SESSION_SAMPLE",
    "HUAMI_SPO2_SAMPLE",
    "HUAMI_STRESS_SAMPLE",
    # parser/colmi
    "COLMI_ACTIVITY_SAMPLE",
    "COLMI_HEART_RATE_SAMPLE",
    "COLMI_HRV_SUMMARY_SAMPLE",
    "COLMI_HRV_VALUE_SAMPLE",
    "COLMI_SLEEP_SESSION_SAMPLE",
    "COLMI_SLEEP_STAGE_SAMPLE",
    "COLMI_SPO2_SAMPLE",
    "COLMI_STRESS_SAMPLE",
    "COLMI_TEMPERATURE_SAMPLE",
}

# Tables that exist in every Gadgetbridge export but are pure
# configuration/metadata (device list, user profiles, tag definitions,
# etc.) rather than time-series health data - deliberately excluded so
# the report stays focused on "health data we might be missing", not
# padded with rows that will never matter for this project. Matched by
# exact name, not a prefix guess, so nothing real accidentally gets
# swept in here by a loose pattern.
SKIP_TABLES = {
    "DEVICE", "DEVICE_ATTRIBUTES", "USER", "USER_ATTRIBUTES",
    "TAG", "ACTIVITY_DESC_TAG_LINK", "MISC_DATA",
    "android_metadata", "sqlite_sequence",
}

# Candidate column names for "the timestamp this row is anchored to",
# checked in this priority order per table - most tables use TIMESTAMP,
# a few Gadgetbridge-native ones use START_TIME/END_TIME instead (a
# session/summary shape rather than a single-instant sample). A table
# matching none of these still gets a row count, just no time range.
TIMESTAMP_COLUMN_CANDIDATES = ["TIMESTAMP", "START_TIME", "START_TIMESTAMP"]


def classify_timestamp_scale(value):
    ''' Guesses whether a raw timestamp value is seconds- or
    milliseconds-since-epoch by magnitude, comparing against plausible
    "recent" ranges for each unit (roughly 2020-2033). This exists
    because a per-table timestamp-unit mismatch is exactly the kind of
    bug that silently filters an entire table's rows out of every
    query (a WHERE TIMESTAMP >= <bound computed in the wrong unit>
    clause can end up always-false for that table specifically, while
    every other table in the same run works fine) - the same class of
    bug COLMI_TIMESTAMPS_ARE_MS turned out to be for Colmi, and DURATION
    units were, twice. Catching this here, on the raw values directly,
    is far more direct than inferring it from query results.
    '''
    if value is None:
        return "?"
    now_s = time.time()
    now_ms = now_s * 1000
    # +/- ~7 years around "now" in each unit
    window_s = 7 * 365 * 86400
    if abs(value - now_s) < window_s:
        return "seconds"
    if abs(value - now_ms) < window_s * 1000:
        return "milliseconds"
    return "UNRECOGNIZED"


def discover_tables(cur):
    ''' Every real table in this database, straight from sqlite_master -
    not a maintained list that can drift out of date or miss a table
    nobody thought to add ahead of time.
    '''
    cur.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    return [row[0] for row in cur.fetchall()]


def pick_timestamp_column(cur, table):
    ''' The best available "anchoring timestamp" column for this
    table, checked via its REAL columns (not assumed) - returns None
    if nothing recognizable exists, so the caller can still report a
    row count without a time range rather than erroring or skipping
    the table outright.
    '''
    cur.execute(f'PRAGMA table_info("{table}")')
    columns = {row[1] for row in cur.fetchall()}  # row[1] is the column name
    for candidate in TIMESTAMP_COLUMN_CANDIDATES:
        if candidate in columns:
            return candidate
    return None


def main():
    if not WEBDAV_URL:
        print("WEBDAV_URL not set in environment", file=sys.stderr)
        sys.exit(1)

    webdav_client = Client({
        "webdav_hostname": WEBDAV_URL,
        "webdav_login": WEBDAV_USER,
        "webdav_password": WEBDAV_PASS,
    })
    tempdir = fetch_database(webdav_client, WEBDAV_PATH, EXPORT_FILE)
    conn, cur = open_database(tempdir)

    tables = [t for t in discover_tables(cur) if t not in SKIP_TABLES]
    print(f"Found {len(tables)} tables in the real export (excluding config/metadata tables)", file=sys.stderr)

    results = []
    for table in tables:
        ts_col = pick_timestamp_column(cur, table)
        try:
            if ts_col:
                cur.execute(f'SELECT COUNT(*), MIN("{ts_col}"), MAX("{ts_col}") FROM "{table}"')
                count, min_ts, max_ts = cur.fetchone()
            else:
                cur.execute(f'SELECT COUNT(*) FROM "{table}"')
                count = cur.fetchone()[0]
                min_ts, max_ts = None, None
        except Exception as e:
            count, min_ts, max_ts = None, None, None
            print(f"  (skipped {table}: {e})", file=sys.stderr)
        results.append((table, count, min_ts, max_ts, ts_col))

    conn.close()
    shutil.rmtree(tempdir, ignore_errors=True)

    results.sort(key=lambda r: (r[1] is None, -(r[1] or 0)))

    print(f"\n{'Table':45} {'Rows':>8}  {'TS column':16} {'Scale':12} Status")
    print("-" * 115)
    for table, count, min_ts, max_ts, ts_col in results:
        if count is None:
            status = "MISSING/ERROR"
            scale = ""
        elif count == 0:
            status = ""
            scale = ""
        else:
            scale = classify_timestamp_scale(max_ts) if ts_col else "(no ts column)"
            already = table in ALREADY_IMPLEMENTED
            if already and scale == "seconds":
                status = "<- extracted, but scale looks like SECONDS - check *_TIMESTAMPS_ARE_MS handling for this table specifically"
            elif already:
                status = "<- already extracted"
            else:
                status = "<- NOT YET EXTRACTED, has data!"
        count_str = "-" if count is None else str(count)
        ts_col_str = ts_col or ""
        print(f"{table:45} {count_str:>8}  {ts_col_str:16} {scale:12} {status}")

    print()
    nonzero_new = [t for t, c, _, _, _ in results if c and c > 0 and t not in ALREADY_IMPLEMENTED]
    if nonzero_new:
        print(f"Tables with real data NOT currently extracted: {nonzero_new}")
    else:
        print("No non-zero tables found outside what's already extracted.")

    seconds_scale_implemented = [
        t for t, c, _, mx, ts_col in results
        if c and c > 0 and t in ALREADY_IMPLEMENTED and ts_col and classify_timestamp_scale(mx) == "seconds"
    ]
    if seconds_scale_implemented:
        print(f"\nWARNING: these already-extracted tables have SECONDS-scale timestamps, "
              f"but the parser may assume milliseconds applies uniformly for that table family. "
              f"A table with seconds-scale timestamps will have every row silently excluded by "
              f"a WHERE TIMESTAMP >= <bound-in-ms> clause, even though the table has real "
              f"data (this is very likely why rows show up here but never reach InfluxDB): "
              f"{seconds_scale_implemented}")


if __name__ == "__main__":
    main()