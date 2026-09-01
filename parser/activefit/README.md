# activefit parser (Amazfit Active 3 Premium)

**Status: best-effort implementation, running safely, NOT verified
against real Active 3 Premium hardware yet.**

This is the HUAMI_* counterpart to [`../colmi/`](../colmi/README.md),
sharing the same device-agnostic plumbing from [`../common/`](../common)
(WebDAV fetch, DEVICE table lookup, checkpoint mechanics, the
future-timestamp guard, the InfluxDB write path).

## Why this is safe to deploy before pairing the watch

Every table query goes through `common.devices.run_query`, which
catches `sqlite3.OperationalError` (missing table, wrong column name)
and returns `None` instead of raising. Concretely:

- Before the watch is paired, none of the `HUAMI_*` tables below exist
  in the export at all - every query reports "table missing" at
  **DEBUG** level (not a crash, not even a WARNING) and is skipped.
- `extract_data()` then returns an empty list, and `__main__` treats
  an empty list as "nothing to sync this run" - **not** a fatal error.
  It does **not** call `sys.exit(1)`, so it won't crash-loop under
  `restart: unless-stopped` alongside your existing Colmi parser and
  other tools.
- If a guessed column name is wrong (table exists, query doesn't
  match its actual columns), that one section logs a **WARNING** and
  is skipped - everything else still runs normally.

In short: this container can run continuously right now, before the
watch exists in your Gadgetbridge database, and it will do nothing
except log quiet "table not found" debug lines every cycle. This was
verified directly (see the restructure commit / conversation history)
by running `extract_data()` against an in-memory SQLite database with
only a `DEVICE` table and zero `HUAMI_*` tables — it returns `[]`
without raising.

**What this does NOT mean:** it does not mean the guessed table/column
names are *correct* once real data exists. Treat any rows that do
start landing in InfluxDB once the watch is paired as a *starting
point* to verify, not as confirmed-correct data - see the checklist
below.

## What's actually implemented (all UNVERIFIED against real hardware)

| Data | Table tried | Confidence |
|---|---|---|
| Activity (steps/HR/intensity) | `HUAMI_EXTENDED_ACTIVITY_SAMPLE`, falls back to `HUAMI_ACTIVITY_SAMPLE` | Medium - `HUAMI_EXTENDED_ACTIVITY_SAMPLE` and its `TIMESTAMP, DEVICE_ID, RAW_KIND, STEPS, HEART_RATE, RAW_INTENSITY` base columns are confirmed to exist on *other* real Huami/Zepp OS devices per [Gadgetbridge issue #1389](https://codeberg.org/Freeyourgadget/Gadgetbridge/issues/1389) and [#2837](https://codeberg.org/Freeyourgadget/Gadgetbridge/pulls/2837) - not confirmed for the Active 3 Premium specifically |
| Sleep (`sleep_extended_raw`, `sleep_rem_raw`, `sleep_deep_raw`) | `SLEEP`, `REM_SLEEP`, `DEEP_SLEEP` columns on `HUAMI_EXTENDED_ACTIVITY_SAMPLE` | Low - columns confirmed to exist on an Amazfit Bip 6 per [Gadgetbridge issue #4715](https://codeberg.org/Freeyourgadget/Gadgetbridge/issues/4715), **but that same report says `REM_SLEEP` and `DEEP_SLEEP` held identical values** - don't trust the REM/deep split without checking your own data |
| Resting HR | `HUAMI_HEART_RATE_RESTING_SAMPLE` | Low - table name unconfirmed |
| Stress | `HUAMI_STRESS_SAMPLE` | Low - table name unconfirmed; a Gadgetbridge design discussion ([#2797](https://about.codeberg.org/Freeyourgadget/Gadgetbridge/issues/2797)) considered storing this as extra columns on the activity table instead, so this may simply never match for this device |
| SpO2 | `HUAMI_SPO2_SAMPLE` | Low - table name unconfirmed; SpO2 fetch support is itself relatively recent/per-device per [#3131](https://codeberg.org/Freeyourgadget/Gadgetbridge/issues/3131) |
| Sleep sessions (per-night, stage-level like Colmi's) | **Not attempted** | `HUAMI_SLEEP_SESSION_SAMPLE` exists on Gadgetbridge ≥0.85 for Zepp OS devices, but per real-world reports its per-night detail lives largely in a BLOB `DATA` column - decoding that is real reverse-engineering work, not a SELECT, and isn't attempted here |
| Timestamps | assumed milliseconds, same as `COLMI_*` | Unconfirmed for this device |

## Before trusting any of this data

1. Pair the Active 3 Premium with Gadgetbridge and let it sync/export
   at least once.
2. Watch this container's logs for the extraction summary line - it
   lists which sections actually matched a table and how many rows.
   Anything still showing "table does not exist" (DEBUG) after
   pairing means that guess was wrong; anything at WARNING means the
   table exists but the columns don't match.
3. Pull the resulting `Gadgetbridge.db` and inspect it directly:
   `sqlite3 Gadgetbridge.db .schema` for every `HUAMI_*` table
   actually present - don't assume the table list above is complete
   or correctly named just because some sections matched.
4. Confirm the timestamp unit the same way `COLMI_TIMESTAMPS_ARE_MS`
   was confirmed for Colmi - look for "value out of range" InfluxDB
   write errors (implies ms, treated as ns without ×1e6 correction) or
   implausible far-future graphed data.
5. Spot-check the `sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw`
   fields against a night you remember clearly, given the identical-
   values caveat above - these may need dropping or reinterpreting
   entirely rather than trusted as-is.
6. Once verified, update the confidence table above and this file's
   status line - and consider whether the not-yet-attempted sleep
   session BLOB is worth decoding, or whether the activity-table sleep
   columns turn out to be good enough.

## Checkpoint isolation

This parser already writes/reads its own checkpoint history
independently of Colmi's - every point gets a `source="activefit"` tag
(vs Colmi's `source="colmi"`), and `common.checkpoint.get_last_checkpoint_ns`
filters on it. This is what lets both parsers run simultaneously
against the same InfluxDB bucket/user without one's first-ever sync
inheriting the other's "already caught up" checkpoint. See
[`../common/checkpoint.py`](../common/checkpoint.py)'s module docstring
for the full reasoning.