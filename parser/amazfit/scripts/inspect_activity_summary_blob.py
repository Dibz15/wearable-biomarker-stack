#!/usr/bin/env python3
'''
One-off diagnostic: fetches the current Gadgetbridge export and dumps
BASE_ACTIVITY_SUMMARY.RAW_SUMMARY_DATA/SUMMARY_DATA for every row,
directly - to answer a specific question that InfluxDB alone can't:
when a workout shows up with only duration_s and none of
flatten_workout_summary()'s richer fields, is that because
RAW_SUMMARY_DATA was genuinely NULL/empty for that row (no richer data
was ever recorded), or because it had real bytes that failed to
decode (a genuine bug worth investigating)? Both cases look IDENTICAL
from InfluxDB's own side - only duration_s written either way - so
this has to be checked against the real database directly.

Run via the same pattern as check_table_usage.py:

    docker cp parser/amazfit/scripts/inspect_activity_summary_blob.py biomarker-parser-amazfit:/tmp/inspect_activity_summary_blob.py
    docker exec -it biomarker-parser-amazfit python3 /tmp/inspect_activity_summary_blob.py

See inspect_sleep_session_blob.py's own docstring for why the
destination filename matters (don't shadow a stdlib module name).

Requires the same WEBDAV_* env vars the parser itself uses.

Dumps the FULL blob as hex (not just a head/tail preview) for any blob
up to FULL_DUMP_MAX_BYTES - a real gap found and fixed here: an
earlier version only ever printed the first/last 64 bytes, which
happened to cover a 118-byte blob completely (the two windows
overlapped) but silently left a real gap in the middle for anything
larger (e.g. a 248-byte blob has a 120-byte hole neither window
touches) - discovered when a person's own real outdoor-workout blob
turned out to be exactly this larger case, and the missing middle
bytes were the ones actually needed to solve a real question (the
Pace field's own scaling formula).

What to look for in the output:
  - RAW_SUMMARY_DATA is NULL or 0 bytes: this row genuinely has no
    richer breakdown to extract - matches the parser's own new
    "no RAW_SUMMARY_DATA at all" log line, not a bug.
  - RAW_SUMMARY_DATA has bytes but the first 2 bytes aren't 0x00 0x80
    (i.e. not the expected 0x8000 version little-endian): either a
    different/older summary format, or a genuinely different device
    generation - the version itself is only a warning in
    flatten_workout_summary(), not a hard stop, so this alone doesn't
    explain a fully-empty decode.
  - RAW_SUMMARY_DATA has bytes, version looks right, but the decode
    still comes back mostly/fully empty: worth pasting the hex dump
    back for a closer look - could be a genuinely sparse workout (this
    device might just not have recorded much for that particular
    session), or a wire-format edge case the schema doesn't cover yet.
  - SUMMARY_DATA (separate, plaintext TEXT column): expected to be
    NULL/empty in practice - independently confirmed Gadgetbridge
    strips this before writing to the DB to save space - a non-empty
    value here would be a pleasant surprise, not something to expect.
'''

import os
import shutil
import sys

sys.path.insert(0, "/app")

from webdav3.client import Client

from common.webdav import fetch_database, open_database
from common.devices import run_query

WEBDAV_URL = os.getenv("WEBDAV_URL", False)
WEBDAV_PATH = os.getenv("WEBDAV_PATH", "files/service_user/GadgetBridge/")
WEBDAV_USER = os.getenv("WEBDAV_USER", False)
WEBDAV_PASS = os.getenv("WEBDAV_PASS", False)
EXPORT_FILE = os.getenv("EXPORT_FILENAME", "Gadgetbridge.db")

HEX_DUMP_BYTES = 64
FULL_DUMP_MAX_BYTES = 2000


def hex_dump(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


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

    rows = run_query(cur, "BASE_ACTIVITY_SUMMARY",
        "SELECT START_TIME, END_TIME, DEVICE_ID, NAME, ACTIVITY_KIND, "
        "SUMMARY_DATA, RAW_SUMMARY_DATA FROM BASE_ACTIVITY_SUMMARY "
        "ORDER BY START_TIME ASC")

    conn.close()
    shutil.rmtree(tempdir, ignore_errors=True)

    if not rows:
        print("No rows in BASE_ACTIVITY_SUMMARY (or table missing/unreadable).")
        return

    print(f"Found {len(rows)} row(s) in BASE_ACTIVITY_SUMMARY\n")

    for i, (start_time, end_time, device_id, name, activity_kind, summary_data, raw_summary_data) in enumerate(rows):
        print("=" * 90)
        print(f"Row {i}: device_id={device_id} name={name!r} activity_kind={activity_kind} "
              f"start_time={start_time} end_time={end_time}")

        print(f"  SUMMARY_DATA (TEXT): {summary_data!r}" if summary_data else "  SUMMARY_DATA (TEXT): NULL/empty")

        if raw_summary_data is None:
            print("  RAW_SUMMARY_DATA (BLOB): NULL")
            continue
        data = bytes(raw_summary_data)
        if len(data) == 0:
            print("  RAW_SUMMARY_DATA (BLOB): present but 0 bytes")
            continue

        print(f"  RAW_SUMMARY_DATA (BLOB): {len(data)} bytes")
        if len(data) >= 2:
            version = data[0] | (data[1] << 8)
            print(f"    version header (first 2 bytes, little-endian): 0x{version:04x} "
                  + ("(matches the expected 0x8000)" if version == 0x8000 else "(UNEXPECTED - expected 0x8000)"))
        # Full dump, not just head/tail - a real gap found and fixed
        # here: these blobs are consistently small (under a couple
        # hundred bytes in every real example seen so far), but an
        # earlier version of this script only dumped the first/last
        # HEX_DUMP_BYTES, which happened to work for a 118-byte blob
        # (the two 64-byte windows overlapped, covering everything)
        # but left a real, silent GAP in the middle for anything
        # larger (e.g. a 248-byte blob leaves a 120-byte hole neither
        # window touches) - reconstructing "first 64 + last 64" for
        # such a blob is missing real data, not just cosmetically
        # incomplete. A hard ceiling (FULL_DUMP_MAX_BYTES) still
        # guards against flooding the terminal if a genuinely huge
        # blob ever shows up, falling back to the old head/tail view
        # only in that unexpected case.
        if len(data) <= FULL_DUMP_MAX_BYTES:
            print(f"  Full blob (hex): {hex_dump(data)}")
        else:
            head = data[:HEX_DUMP_BYTES]
            tail = data[-HEX_DUMP_BYTES:]
            print(f"  Blob exceeds {FULL_DUMP_MAX_BYTES} bytes - showing head/tail only "
                  f"(a real gap in the middle - ask for a fuller dump if this range matters):")
            print(f"  First {len(head)} bytes (hex): {hex_dump(head)}")
            print(f"  Last {len(tail)} bytes (hex): {hex_dump(tail)}")

    print("\n" + "=" * 90)
    print("If a row's RAW_SUMMARY_DATA is NULL/0-bytes, that matches the parser's own "
          "'no RAW_SUMMARY_DATA at all' log line - not a bug, just nothing recorded for "
          "that particular workout. If it has real bytes with the expected 0x8000 version "
          "but the decode still came back empty, paste this output back for a closer look.")


if __name__ == "__main__":
    main()
