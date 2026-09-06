#!/usr/bin/env python3
'''
One-off diagnostic: answers the key open question before any per-sample
GPS/elevation track extraction work starts - is the actual per-workout
track data REACHABLE by this parser at all, via ANY mechanism?

Per Gadgetbridge's own real source (confirmed via a PR discussion,
codeberg.org/Freeyourgadget/Gadgetbridge/pulls/3180, a maintainer
describing BaseActivitySummary's own fields directly):
  - RAW_SUMMARY_DATA (already decoded by this parser) is a BLOB
    column, stored directly in Gadgetbridge.db itself.
  - RAW_DETAILS_PATH is a FILE PATH STRING, pointing to a separate raw
    binary file "saved to the external storage" - i.e. NOT inside
    Gadgetbridge.db at all, and not necessarily anywhere this parser's
    existing WebDAV sync (which only fetches Gadgetbridge.db) can see.
  - GPX_TRACK is ANOTHER file path column, supposedly pointing to a
    GPX (plain XML, genuinely easy to parse) file Gadgetbridge's own
    in-app conversion generates from the raw binary - but confirmed
    directly (2026-09, against every real row that exists) to always
    be NULL regardless of whether a real GPX file exists elsewhere.
    This column reflects Gadgetbridge's OWN internal GPX conversion
    pathway specifically, NOT the separate "Auto GPX export"
    automation (Settings -> Automations -> Auto export GPX tracks),
    which writes a real GPX file to a person-chosen folder without
    ever updating this database column - so a null GPX_TRACK here does
    NOT mean no GPX file exists; it means this SPECIFIC column can't
    be trusted to tell you that.

Given GPX_TRACK's own unreliability, this script correlates Auto GPX
export's own output files back to their BASE_ACTIVITY_SUMMARY row a
different way: by matching the shared TIMESTAMP embedded in both
filenames. Confirmed directly against a real example: RAW_DETAILS_PATH's
own basename ("2026-09-05T17_05_33+01_00.bin") and Auto GPX export's
real output filename for that exact same workout
("2026-09-05T17_05_33+01_00-walking.gpx") share an IDENTICAL timestamp
prefix - only an activity-type suffix and the file extension differ.
This works regardless of NAME being null (as it is for a workout that
hasn't been manually tagged in the Zepp app yet) and regardless of
GPX_TRACK's own unreliability.

This script does NOT attempt to parse any file yet - it only checks
reachability and correlation. That answer determines everything
downstream: if nothing is reachable, some Gadgetbridge/Android-side
sync setting needs to change before any of this is buildable at all,
and that's a decision for the person, not something this script or the
parser can fix on its own.

Run via the same pattern as check_table_usage.py:

    docker cp parser/activefit/scripts/inspect_activity_details_paths.py biomarker-parser-activefit:/tmp/inspect_activity_details_paths.py
    docker exec -it biomarker-parser-activefit python3 /tmp/inspect_activity_details_paths.py

Requires the same WEBDAV_* env vars the parser itself uses, plus
optionally GPX_TRACKS_PATH (defaults to guessing WEBDAV_PATH + "Tracks/"
- override with the real path once known, e.g. a "Tracks" subfolder
picked as the Auto GPX export destination).

What to look for in the output:
  - If RAW_DETAILS_PATH is NULL for every row: this device/firmware
    combination might not populate it at all - a real dead end, not a
    sync problem. (Confirmed NOT the case here - every real row has a
    real RAW_DETAILS_PATH.)
  - If GPX_TRACKS_PATH fails to list at all: the guessed/configured
    path is wrong, or genuinely unreachable from this container -
    re-run with the correct GPX_TRACKS_PATH env var once known.
  - If a workout's RAW_DETAILS_PATH timestamp DOES have a matching GPX
    export file: real progress - the file is reachable, and actually
    downloading + parsing it (GPX is plain XML, no new dependency
    needed) is the natural next step. Note this only ever fires for
    genuinely GPS-tracked activities (Auto GPX export has nothing to
    export for an indoor workout) - a "no match" on an indoor workout
    is expected, not a bug.
'''

import os
import sys
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
# Auto GPX export (Settings -> Automations -> Auto export GPX tracks)
# writes to a person-chosen folder, separate from the main
# Gadgetbridge.db auto-export location - not assumed to be WEBDAV_PATH
# itself, since it's very likely a subfolder or a different location
# entirely depending on what was picked. Override via env var once the
# real folder is known, rather than guessing a single hardcoded default.
GPX_TRACKS_PATH = os.getenv("GPX_TRACKS_PATH", WEBDAV_PATH + "Tracks/")


def main():
    if not WEBDAV_URL:
        print("WEBDAV_URL not set in environment", file=sys.stderr)
        sys.exit(1)

    webdav_client = Client({
        "webdav_hostname": WEBDAV_URL,
        "webdav_login": WEBDAV_USER,
        "webdav_password": WEBDAV_PASS,
    })

    # Step 1: what's actually in the same WebDAV folder Gadgetbridge.db
    # itself is fetched from? Listed FIRST, before touching the
    # database, so it's easy to eyeball whether anything .gpx-shaped
    # (or otherwise workout-detail-shaped) is even present.
    print("=" * 90)
    print(f"WebDAV directory listing for {WEBDAV_PATH!r}:")
    try:
        entries = webdav_client.list(WEBDAV_PATH)
        for entry in sorted(entries):
            print(f"  {entry}")
        print(f"({len(entries)} entries total)")
    except Exception as e:
        print(f"  Failed to list directory: {e}")
        entries = []

    # Step 1b: Auto GPX export (Settings -> Automations -> Auto export
    # GPX tracks) writes to its OWN chosen folder, not necessarily
    # WEBDAV_PATH itself - listed separately, tolerating this folder
    # not existing/being reachable at all (a real possible outcome,
    # not an error worth crashing over).
    print(f"\nWebDAV directory listing for GPX_TRACKS_PATH={GPX_TRACKS_PATH!r}:")
    try:
        gpx_entries = webdav_client.list(GPX_TRACKS_PATH)
        for entry in sorted(gpx_entries):
            print(f"  {entry}")
        print(f"({len(gpx_entries)} entries total)")
    except Exception as e:
        print(f"  Failed to list directory ({e}) - GPX_TRACKS_PATH may be wrong, "
              f"or not reachable from this container at all. Override with the "
              f"real path via the GPX_TRACKS_PATH env var and re-run.")
        gpx_entries = []

    # Step 2: what does the database itself say RAW_DETAILS_PATH/
    # GPX_TRACK are, for every workout - not just one, since different
    # workouts (or workout TYPES) may behave differently.
    tempdir = fetch_database(webdav_client, WEBDAV_PATH, EXPORT_FILE)
    conn, cur = open_database(tempdir)

    rows = run_query(cur, "BASE_ACTIVITY_SUMMARY",
        "SELECT START_TIME, NAME, ACTIVITY_KIND, GPX_TRACK, RAW_DETAILS_PATH, "
        "RAW_SUMMARY_DATA FROM BASE_ACTIVITY_SUMMARY ORDER BY START_TIME ASC")

    conn.close()
    import shutil
    shutil.rmtree(tempdir, ignore_errors=True)

    if not rows:
        print("\nNo rows in BASE_ACTIVITY_SUMMARY.")
        return

    entry_basenames = {Path(e).name for e in entries}

    print("\n" + "=" * 90)
    print(f"Found {len(rows)} row(s) in BASE_ACTIVITY_SUMMARY\n")
    for i, (start_time, name, activity_kind, gpx_track, raw_details_path, raw_summary_data) in enumerate(rows):
        print("-" * 90)
        print(f"Row {i}: name={name!r} activity_kind={activity_kind} start_time={start_time}")
        print(f"  RAW_SUMMARY_DATA: {'present, ' + str(len(bytes(raw_summary_data))) + ' bytes' if raw_summary_data else 'NULL/empty'}")
        print(f"  GPX_TRACK column: {gpx_track!r} (NOTE: this column reflects Gadgetbridge's "
              f"OWN in-app GPX conversion, a DIFFERENT mechanism from Auto GPX export's own "
              f"automation - a null/stale value here does NOT mean Auto GPX export didn't "
              f"produce a real file; check the GPX_TRACKS_PATH correlation below instead.)")
        print(f"  RAW_DETAILS_PATH: {raw_details_path!r}")

        if raw_details_path:
            basename = Path(raw_details_path).name
            if basename in entry_basenames:
                print(f"    -> RAW_DETAILS_PATH's basename ({basename!r}) DOES show up in the main WebDAV listing - reachable!")
            else:
                print(f"    -> RAW_DETAILS_PATH's basename ({basename!r}) does NOT appear in the main WebDAV listing")

            # Auto GPX export's own filenames embed the SAME timestamp
            # RAW_DETAILS_PATH's own basename does (confirmed directly
            # against a real example: RAW_DETAILS_PATH basename
            # "2026-09-05T17_05_33+01_00.bin" against the exported GPX
            # "2026-09-05T17_05_33+01_00-walking.gpx" - identical
            # timestamp prefix, an activity-type suffix and different
            # extension appended) - correlating on that shared prefix
            # works regardless of the unreliable GPX_TRACK column, and
            # regardless of NAME being null (as it is for a fresh
            # not-yet-manually-tagged workout, like this one).
            timestamp_prefix = basename.rsplit(".", 1)[0]  # strip ".bin"
            matches = [g for g in gpx_entries if Path(g).name.startswith(timestamp_prefix)]
            if matches:
                print(f"    -> Found {len(matches)} matching GPX export(s) in GPX_TRACKS_PATH by timestamp prefix: {matches}")
            else:
                print(f"    -> No GPX export found in GPX_TRACKS_PATH matching timestamp prefix {timestamp_prefix!r}")

    print("\n" + "=" * 90)
    print("See this script's own docstring for how to interpret the above.")


if __name__ == "__main__":
    main()