#!/usr/bin/env python3
'''
One-off diagnostic: fetches the current Gadgetbridge export and dumps
the raw bytes of every HUAMI_SLEEP_SESSION_SAMPLE.DATA BLOB, plus
structural hints (length, byte-value histogram, divisibility) to help
reverse-engineer the format - this is the last table check_table_usage.py
flags as having real data but not yet extracted, and it's the one that
actually matters for real sleep-stage data: confirmed (via Gadgetbridge's
own sleep graph matching the watch/Zepp app) that the activity table's
sleep_extended_raw/sleep_rem_raw/sleep_deep_raw columns do NOT vary
during the night (frozen for 9+ hours straight), so whatever produces
Gadgetbridge's real stage-by-stage graph has to be reading this BLOB,
not those columns.

Run via the same pattern as check_table_usage.py:

    docker cp parser/activefit/scripts/inspect_sleep_session_blob.py biomarker-parser-activefit:/tmp/inspect_bob.py
    docker exec -it biomarker-parser-activefit python3 /tmp/inspect_blob.py

Requires the same WEBDAV_* env vars the parser itself uses.

What to look for in the output:
  - LENGTH vs known session duration: if a night's session ran roughly
    23:02-08:09 (~547 minutes), a length near 547 suggests 1 byte per
    minute; near 547*2 suggests 2 bytes per minute (e.g. a stage code
    + a confidence/intensity byte per minute); a length that divides
    evenly by small numbers (2,3,4) hints at a fixed-size per-minute
    or per-epoch record rather than one byte per minute.
  - UNIQUE BYTE VALUES: a small alphabet (roughly 3-8 distinct values)
    strongly suggests simple stage codes (awake/light/deep/REM plus
    maybe a "not worn"/padding marker). A large, near-uniform spread
    across most of 0-255 suggests either compressed data or a
    multi-field packed structure, not simple per-minute codes.
  - TIMESTAMP scale is ALSO unverified for this table specifically -
    same situation HUAMI_EXTENDED_ACTIVITY_SAMPLE turned out to have
    its own scale from every other table. Both seconds and
    milliseconds interpretations are printed so this can be checked
    by eye (whichever one lands on a plausible bedtime is very likely
    correct) rather than assumed.
'''

import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from webdav3.client import Client

from common.webdav import fetch_database, open_database
from common.devices import run_query

WEBDAV_URL = os.getenv("WEBDAV_URL", False)
WEBDAV_PATH = os.getenv("WEBDAV_PATH", "files/service_user/GadgetBridge/")
WEBDAV_USER = os.getenv("WEBDAV_USER", False)
WEBDAV_PASS = os.getenv("WEBDAV_PASS", False)
EXPORT_FILE = os.getenv("EXPORT_FILENAME", "Gadgetbridge.db")

HEX_DUMP_BYTES = 96  # how many bytes to show from the start/end of each blob


def try_iso(raw_ts, is_ms):
    try:
        seconds = raw_ts / 1000 if is_ms else raw_ts
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return "(out of range)"


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

    rows = run_query(cur, "HUAMI_SLEEP_SESSION_SAMPLE",
        "SELECT TIMESTAMP, DEVICE_ID, USER_ID, DATA FROM HUAMI_SLEEP_SESSION_SAMPLE "
        "ORDER BY TIMESTAMP ASC")

    conn.close()
    shutil.rmtree(tempdir, ignore_errors=True)

    if not rows:
        print("No rows in HUAMI_SLEEP_SESSION_SAMPLE (or table missing/unreadable).")
        return

    print(f"Found {len(rows)} row(s) in HUAMI_SLEEP_SESSION_SAMPLE\n")

    for i, (raw_ts, device_id, user_id, data) in enumerate(rows):
        data = bytes(data) if data is not None else b""
        print("=" * 90)
        print(f"Row {i}: DEVICE_ID={device_id} USER_ID={user_id}")
        print(f"  Raw TIMESTAMP: {raw_ts}")
        print(f"    as seconds:      {try_iso(raw_ts, is_ms=False)}")
        print(f"    as milliseconds: {try_iso(raw_ts, is_ms=True)}")
        print(f"  DATA length: {len(data)} bytes")

        if len(data) == 0:
            print("  (empty blob)")
            continue

        divisors = [n for n in (2, 3, 4, 5, 6, 8, 10) if len(data) % n == 0]
        print(f"  Divides evenly by: {divisors if divisors else 'none of 2,3,4,5,6,8,10'}")

        counts = Counter(data)
        unique_count = len(counts)
        print(f"  Unique byte values: {unique_count} (out of {len(data)} total bytes)")
        print("  Most common byte values (value: count, % of blob):")
        for value, count in counts.most_common(20):
            pct = 100 * count / len(data)
            print(f"    0x{value:02x} ({value:3}): {count:6}  ({pct:5.1f}%)")

        head = data[:HEX_DUMP_BYTES]
        tail = data[-HEX_DUMP_BYTES:] if len(data) > HEX_DUMP_BYTES else b""
        print(f"  First {len(head)} bytes (hex):")
        print(f"    {hex_dump(head)}")
        if tail:
            print(f"  Last {len(tail)} bytes (hex):")
            print(f"    {hex_dump(tail)}")

    print("\n" + "=" * 90)
    print("Compare DATA length against the known session duration (minutes) to test")
    print("a per-minute (or per-N-minute) encoding hypothesis - see this file's")
    print("docstring for what to look for.")


if __name__ == "__main__":
    main()