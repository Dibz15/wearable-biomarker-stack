#!/usr/bin/env python3
'''
One-off diagnostic: downloads a real per-workout .fit export (found via
inspect_activity_details_paths.py's own timestamp-correlation approach)
and dumps its full structure using the REAL `fitparse` library - not a
hand-rolled decoder. FIT is a real, standardized, widely-documented
binary format (not something Gadgetbridge invented), so this follows
the same reasoning that led to using the real `protobuf` library for
RAW_SUMMARY_DATA rather than a hand-rolled wire-format decoder: don't
reverse-engineer a format a battle-tested open-source parser already
reads correctly.

REQUIRES fitparse, which is NOT yet a dependency of this parser image -
install it in the running container first:

    docker exec -it biomarker-parser-amazfit pip install fitparse --break-system-packages

Then run via the same pattern as the other diagnostics:

    docker cp parser/amazfit/scripts/inspect_fit_track.py biomarker-parser-amazfit:/tmp/inspect_fit_track.py
    docker exec -it biomarker-parser-amazfit python3 /tmp/inspect_fit_track.py

Requires the same WEBDAV_* env vars the parser itself uses, plus
optionally EXPORT_TRACKS_PATH (see inspect_activity_details_paths.py's
own docstring for what this is and how to find the real value).

What this does, for EVERY BASE_ACTIVITY_SUMMARY row that has a matching
.fit export (found by the same RAW_DETAILS_PATH-basename-vs-export-
filename timestamp correlation already confirmed working):
  1. Downloads that one .fit file.
  2. Parses it with fitparse.
  3. Prints every message TYPE encountered and how many of each -
     FIT files are a stream of typed messages (e.g. "record" for
     per-sample data points, "session"/"lap" for summaries,
     "device_info", etc.) - this alone tells us the file's overall
     shape before looking at any individual field.
  4. For "record" messages specifically (the per-sample stream this
     whole investigation is actually after - GPS position, heart
     rate, cadence, elevation, one message per sample interval) -
     prints the full field list from the FIRST and LAST record, plus
     a small handful from the middle, so the real available fields
     and their real value ranges are visible without dumping
     thousands of lines for a long workout.

What to look for in the output:
  - Real field names among "record" messages - e.g. "heart_rate",
    "position_lat"/"position_long" (FIT's own semicircle-encoded
    coordinate format, not plain degrees - a real, DOCUMENTED unit
    conversion exists for this, not a guess: degrees = semicircles *
    (180 / 2^31)), "altitude"/"enhanced_altitude", "cadence",
    "distance", "speed"/"enhanced_speed", "timestamp". Exactly which
    of these are present will vary by activity type (a Yoga session
    won't have GPS fields at all; an outdoor Walking session should).
  - The "timestamp" field on each record is what turns this from "one
    big blob of numbers" into a genuine per-sample TIME SERIES -
    confirms whether per-sample extraction into InfluxDB is
    realistic (each record becoming its own point) rather than
    needing extra reconstruction work.
'''

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")

from webdav3.client import Client

from common.webdav import fetch_database, open_database
from common.devices import run_query

WEBDAV_URL = os.getenv("WEBDAV_URL", False)
WEBDAV_PATH = os.getenv("WEBDAV_PATH", "files/service_user/GadgetBridge/")
WEBDAV_USER = os.getenv("WEBDAV_USER", False)
WEBDAV_PASS = os.getenv("WEBDAV_PASS", False)
EXPORT_FILE = os.getenv("EXPORT_FILENAME", "Gadgetbridge.db")
EXPORT_TRACKS_PATH = os.getenv("EXPORT_TRACKS_PATH", WEBDAV_PATH + "Tracks/")

RECORD_FIELDS_TO_SHOW = 3  # how many sample "record" messages to print in full


def dump_fit_file(local_path: Path, workout_name: str):
    import fitparse

    try:
        fitfile = fitparse.FitFile(str(local_path))
        # Force a full parse now (fitparse is lazy/streaming by
        # default) so any real parse error surfaces here, clearly
        # attributed to this specific file, rather than partway
        # through the message-counting loop below.
        all_messages = list(fitfile.get_messages())
    except Exception as e:
        print(f"  FAILED to parse {local_path.name} with fitparse: {e}")
        return

    print(f"  Parsed OK - {len(all_messages)} total message(s)")

    message_type_counts = {}
    for m in all_messages:
        message_type_counts[m.name] = message_type_counts.get(m.name, 0) + 1
    print("  Message type counts:")
    for name, count in sorted(message_type_counts.items(), key=lambda kv: -kv[1]):
        print(f"    {name}: {count}")

    records = [m for m in all_messages if m.name == "record"]
    if not records:
        print("  No 'record' (per-sample) messages found in this file.")
        return

    print(f"\n  {len(records)} 'record' (per-sample) message(s) - showing first/middle/last in full:")
    indices_to_show = sorted({0, len(records) // 2, len(records) - 1})
    for idx in indices_to_show:
        record = records[idx]
        print(f"\n  Record[{idx}]:")
        for field in record:
            print(f"    {field.name} = {field.value!r} (units: {field.units!r})")


def main():
    if not WEBDAV_URL:
        print("WEBDAV_URL not set in environment", file=sys.stderr)
        sys.exit(1)

    try:
        import fitparse  # noqa: F401
    except ImportError:
        print("fitparse is not installed in this container. Run:\n"
              "  pip install fitparse --break-system-packages\n"
              "then re-run this script.", file=sys.stderr)
        sys.exit(1)

    webdav_client = Client({
        "webdav_hostname": WEBDAV_URL,
        "webdav_login": WEBDAV_USER,
        "webdav_password": WEBDAV_PASS,
    })

    try:
        export_entries = webdav_client.list(EXPORT_TRACKS_PATH)
    except Exception as e:
        print(f"Failed to list EXPORT_TRACKS_PATH ({EXPORT_TRACKS_PATH!r}): {e}", file=sys.stderr)
        sys.exit(1)

    tempdir = fetch_database(webdav_client, WEBDAV_PATH, EXPORT_FILE)
    conn, cur = open_database(tempdir)
    rows = run_query(cur, "BASE_ACTIVITY_SUMMARY",
        "SELECT START_TIME, NAME, ACTIVITY_KIND, RAW_DETAILS_PATH "
        "FROM BASE_ACTIVITY_SUMMARY ORDER BY START_TIME ASC")
    conn.close()
    shutil.rmtree(tempdir, ignore_errors=True)

    if not rows:
        print("No rows in BASE_ACTIVITY_SUMMARY.")
        return

    download_dir = Path(tempfile.mkdtemp())
    try:
        for start_time, name, activity_kind, raw_details_path in rows:
            if not raw_details_path:
                continue
            timestamp_prefix = Path(raw_details_path).name.rsplit(".", 1)[0]
            fit_matches = [e for e in export_entries if Path(e).name.startswith(timestamp_prefix) and e.endswith(".fit")]
            if not fit_matches:
                continue

            print("=" * 90)
            print(f"name={name!r} activity_kind={activity_kind} start_time={start_time}")
            for match in fit_matches:
                remote_path = EXPORT_TRACKS_PATH + Path(match).name if not match.startswith(EXPORT_TRACKS_PATH) else match
                local_path = download_dir / Path(match).name
                print(f"  Downloading {match!r} ...")
                try:
                    webdav_client.download_sync(remote_path=remote_path, local_path=str(local_path))
                except Exception as e:
                    print(f"  FAILED to download: {e} (tried remote_path={remote_path!r})")
                    continue
                dump_fit_file(local_path, name or "(unnamed)")
    finally:
        shutil.rmtree(download_dir, ignore_errors=True)

    print("\n" + "=" * 90)
    print("See this script's own docstring for what to look for above.")


if __name__ == "__main__":
    main()