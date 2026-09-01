#!/usr/bin/env python3
#
# Fetch a Gadgetbridge database export from a WebDAV URL and extract
# stats for an Amazfit device into InfluxDB - the HUAMI_* counterpart
# to ../../colmi/app/gadgetbridge_to_influxdb.py.
#
# STATUS: SKELETON, NOT YET VERIFIED AGAINST REAL HARDWARE.
#
# Everything device-agnostic (WebDAV fetch, DEVICE table lookup,
# checkpoint mechanics, the future-timestamp guard, the InfluxDB write
# path) is already wired up below via ../../common/ - that part is
# safe to run as-is.
#
# extract_data() itself is deliberately left raising NotImplementedError.
# The Colmi parser's own history (COLMI_TIMESTAMPS_ARE_MS assumed wrong
# on the first pass, DURATION units assumed wrong TWICE) is exactly why:
# guessing HUAMI_* table/column names and unit conventions from
# secondhand research and shipping them as if verified would very
# likely repeat that pattern. See README.md in this directory for what
# research turned up as a starting point, and what to check for real
# once the Active 3 Premium is actually paired and has exported data.
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

# Identifies this parser's points/checkpoints, distinct from the
# physical `device` tag. See common/checkpoint.py's module docstring.
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

# UNVERIFIED - research (Gadgetbridge issue tracker + blog posts
# describing real exports from other Huami/Zepp OS devices) points to
# HUAMI_* tables using MILLISECOND timestamps, same convention as
# COLMI_*. NOT confirmed against an actual Active 3 Premium export.
# Do not trust this default until checked the same way
# COLMI_TIMESTAMPS_ARE_MS was checked (see colmi/app's docstring for
# that verification story) - look for "value out of range" InfluxDB
# write errors, or implausible far-future graphed data, as the tell.
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
    ''' UNVERIFIED SKELETON - raises NotImplementedError on purpose.

    What research (not a real export) suggests as a starting point,
    per device.py::TABLE_NOTES-equivalent - see README.md for sources:

      - Resting HR:  HUAMI_HEART_RATE_RESTING_SAMPLE (TIMESTAMP, UTC_OFFSET, HEART_RATE)
      - Stress:      HUAMI_STRESS_SAMPLE (TIMESTAMP, TYPE_NUM, STRESS)
      - SpO2:        likely HUAMI_SPO2_SAMPLE, unconfirmed column names
      - Activity:    HUAMI_ACTIVITY_SAMPLE (older) vs HUAMI_EXTENDED_ACTIVITY_SAMPLE
                      (newer Zepp OS devices, adds SLEEP/REM_SLEEP/DEEP_SLEEP
                      columns) - Active 3 Premium is a recent Zepp OS device,
                      so EXTENDED is the more likely candidate, unconfirmed
      - Sleep:       HUAMI_SLEEP_SESSION_SAMPLE exists on Gadgetbridge >=0.85
                      for Zepp OS devices, but per real-world reports its
                      per-night data lives largely in a BLOB `DATA` column
                      (not a queryable per-stage row the way COLMI_SLEEP_STAGE_SAMPLE
                      is) - decoding that BLOB, if needed, is real reverse-
                      engineering work, not a SELECT statement.

    None of the above should be trusted as-is. Once the Active 3
    Premium is paired and has produced a real Gadgetbridge export,
    inspect it directly (sqlite3 .schema against every HUAMI_* table
    actually present) before writing real queries here - exactly the
    same empirical-first approach used to verify the Colmi parser.
    '''
    raise NotImplementedError(
        "activefit extraction not yet implemented - schema unverified against "
        "real Amazfit Active 3 Premium hardware. See this file's docstring "
        "and parser/activefit/README.md before writing real queries here."
    )


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
        if not results:
            logger.error("Data extraction failed")
            sys.exit(1)

        write_results(
            influx_client, results, INFLUXDB_BUCKET, INFLUXDB_ORG, INFLUXDB_MEASUREMENT,
            GADGETBRIDGE_USER, PARSER_SOURCE, MAX_FUTURE_TOLERANCE_SECONDS
        )

    conn.close()
    if tempdir not in ["/", ""]:
        if REMOVE_TEMP_DB == "N":
            logger.debug(tempdir)
        else:
            shutil.rmtree(tempdir)
