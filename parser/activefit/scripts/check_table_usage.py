#!/usr/bin/env python3
'''
One-off diagnostic: fetches the current Gadgetbridge export (same
WebDAV fetch the parser itself uses) and reports row counts across
every HUAMI_*, GENERIC_*, and XIAOMI_* table - not just the ones
parser/activefit currently queries. This is the concrete way to answer
"which table family does this device actually write to" once real
data exists, rather than continuing to reason from the schema alone.

Run via the same pattern as the colmi maintenance scripts:

    docker cp parser/activefit/scripts/check_table_usage.py biomarker-parser-activefit:/tmp/check.py
    docker exec -it biomarker-parser-activefit python3 /tmp/check.py

Requires the same WEBDAV_* env vars the parser itself uses (already
set in the running container, so no extra config needed).
'''

import os
import shutil
import sys

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

# Every HUAMI_*/GENERIC_*/XIAOMI_* table found in a real Gadgetbridge
# schema dump (see parser/colmi/schema.txt) - deliberately broader than
# what parser/activefit currently queries, so this also surfaces
# candidates (GENERIC_*, XIAOMI_*) not implemented yet.
CANDIDATE_TABLES = [
    "GENERIC_BLOOD_PRESSURE_SAMPLE",
    "GENERIC_BODY_ENERGY_SAMPLE",
    "GENERIC_HEART_RATE_SAMPLE",
    "GENERIC_HRV_VALUE_SAMPLE",
    "GENERIC_METRIC_SAMPLE",
    "GENERIC_RESPIRATORY_RATE_SAMPLE",
    "GENERIC_SLEEP_SCORE_SAMPLE",
    "GENERIC_SLEEP_STAGE_SAMPLE",
    "GENERIC_SPO2_SAMPLE",
    "GENERIC_STRESS_SAMPLE",
    "GENERIC_TEMPERATURE_SAMPLE",
    "GENERIC_TRAINING_LOAD_ACUTE_SAMPLE",
    "GENERIC_TRAINING_LOAD_CHRONIC_SAMPLE",
    "GENERIC_WEIGHT_SAMPLE",
    "HUAMI_EXTENDED_ACTIVITY_SAMPLE",
    "HUAMI_HEART_RATE_MANUAL_SAMPLE",
    "HUAMI_HEART_RATE_MAX_SAMPLE",
    "HUAMI_HEART_RATE_RESTING_SAMPLE",
    "HUAMI_PAI_SAMPLE",
    "HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE",
    "HUAMI_SLEEP_SESSION_SAMPLE",
    "HUAMI_SPO2_SAMPLE",
    "HUAMI_STRESS_SAMPLE",
    "XIAOMI_ACTIVITY_FILE",
    "XIAOMI_ACTIVITY_SAMPLE",
    "XIAOMI_DAILY_SUMMARY_SAMPLE",
    "XIAOMI_MANUAL_SAMPLE",
    "XIAOMI_SLEEP_STAGE_SAMPLE",
    "XIAOMI_SLEEP_TIME_SAMPLE",
]

# Currently queried by parser/activefit/app/gadgetbridge_to_influxdb.py -
# printed distinctly below so it's obvious at a glance which non-zero
# tables are already wired up vs newly discovered.
ALREADY_IMPLEMENTED = {
    "GENERIC_HRV_VALUE_SAMPLE",
    "GENERIC_TEMPERATURE_SAMPLE",
    "HUAMI_EXTENDED_ACTIVITY_SAMPLE",
    "HUAMI_HEART_RATE_MANUAL_SAMPLE",
    "HUAMI_HEART_RATE_MAX_SAMPLE",
    "HUAMI_HEART_RATE_RESTING_SAMPLE",
    "HUAMI_PAI_SAMPLE",
    "HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE",
    "HUAMI_SPO2_SAMPLE",
    "HUAMI_STRESS_SAMPLE",
}


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

    results = []
    for table in CANDIDATE_TABLES:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
        except Exception as e:
            count = None
            print(f"  (skipped {table}: {e})", file=sys.stderr)
        results.append((table, count))

    conn.close()
    shutil.rmtree(tempdir, ignore_errors=True)

    results.sort(key=lambda r: (r[1] is None, -(r[1] or 0)))

    print(f"\n{'Table':45} {'Rows':>8}  Status")
    print("-" * 70)
    for table, count in results:
        if count is None:
            status = "MISSING/ERROR"
        elif count == 0:
            status = ""
        elif table in ALREADY_IMPLEMENTED:
            status = "<- already extracted"
        else:
            status = "<- NOT YET EXTRACTED, has data!"
        count_str = "-" if count is None else str(count)
        print(f"{table:45} {count_str:>8}  {status}")

    print()
    nonzero_new = [t for t, c in results if c and c > 0 and t not in ALREADY_IMPLEMENTED]
    if nonzero_new:
        print(f"Tables with real data NOT currently extracted: {nonzero_new}")
    else:
        print("No non-zero tables found outside what's already extracted.")


if __name__ == "__main__":
    main()