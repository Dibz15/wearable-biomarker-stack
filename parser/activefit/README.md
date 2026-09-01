# activefit parser (Amazfit Active 3 Premium)

**Status: skeleton only. `extract_data()` raises `NotImplementedError`
on purpose - do not deploy this yet.**

This is the HUAMI_* counterpart to [`../colmi/`](../colmi/README.md),
sharing the same device-agnostic plumbing from [`../common/`](../common)
(WebDAV fetch, DEVICE table lookup, checkpoint mechanics, the
future-timestamp guard, the InfluxDB write path). Config, imports, and
the `__main__` flow are already wired up and match the Colmi parser's
shape - only the actual table extraction is missing.

## Why extraction isn't written yet

The Colmi parser's own history is the reason: `COLMI_TIMESTAMPS_ARE_MS`
was initially assumed wrong (it's the opposite of `MI_BAND_ACTIVITY_SAMPLE`'s
convention), and `COLMI_SLEEP_STAGE_SAMPLE.DURATION`'s unit was
misidentified twice before being confirmed against real exported data.
Writing HUAMI_* queries from secondhand research and shipping them as
if verified would very likely repeat that pattern - so this parser
stays a stub until it can be checked against a real export.

## What research turned up (unverified starting points)

Sourced from the Gadgetbridge issue tracker and third-party writeups
about *other* Huami/Zepp OS devices - **not** the Active 3 Premium
specifically, and not confirmed against any real export:

| Data | Likely table | Notes |
|---|---|---|
| Resting HR | `HUAMI_HEART_RATE_RESTING_SAMPLE` | `TIMESTAMP, UTC_OFFSET, HEART_RATE` |
| Stress | `HUAMI_STRESS_SAMPLE` | `TIMESTAMP, TYPE_NUM, STRESS` |
| SpO2 | possibly `HUAMI_SPO2_SAMPLE` | column names unconfirmed |
| Activity | `HUAMI_ACTIVITY_SAMPLE` (older) or `HUAMI_EXTENDED_ACTIVITY_SAMPLE` (newer Zepp OS) | Active 3 Premium is a recent Zepp OS device, so EXTENDED is the more likely candidate |
| Sleep | `HUAMI_SLEEP_SESSION_SAMPLE` (Gadgetbridge >=0.85, Zepp OS devices) | per real-world reports, per-night detail lives largely in a BLOB `DATA` column, not queryable per-stage rows the way Colmi's `COLMI_SLEEP_STAGE_SAMPLE` is - decoding that, if needed, is real reverse-engineering, not a SELECT |
| Timestamps | assumed milliseconds, same as COLMI_* | unconfirmed for this device |

## Before writing real queries here

1. Pair the Active 3 Premium with Gadgetbridge and let it sync/export
   at least once.
2. Pull the resulting `Gadgetbridge.db` and inspect it directly:
   `sqlite3 Gadgetbridge.db .schema` for every `HUAMI_*` table
   actually present (the exact set depends on Gadgetbridge version and
   firmware - don't assume the table list above is complete or even
   all correctly named).
3. Confirm the DEVICE table / `DEVICE_ID` foreign-key assumption holds
   the same way it does for Colmi (very likely, since it's the same
   Gadgetbridge app/DB - but check `PRAGMA table_info(DEVICE)` and a
   real join, don't assume).
4. Confirm the timestamp unit the same way `COLMI_TIMESTAMPS_ARE_MS`
   was confirmed - look for "value out of range" InfluxDB write errors
   (implies ms, treated as ns without ×1e6 correction) or implausible
   far-future graphed data.
5. Only then fill in `extract_data()`, following the same pattern as
   `../colmi/app/gadgetbridge_to_influxdb.py` (using `common.devices.run_query`
   so a missing/renamed table degrades gracefully instead of crashing
   the whole sync).

## Checkpoint isolation

This parser already writes/reads its own checkpoint history
independently of Colmi's - every point gets a `source="activefit"` tag
(vs Colmi's `source="colmi"`), and `common.checkpoint.get_last_checkpoint_ns`
filters on it. This is what lets both parsers run simultaneously
against the same InfluxDB bucket/user without one's first-ever sync
inheriting the other's "already caught up" checkpoint. See
[`../common/checkpoint.py`](../common/checkpoint.py)'s module docstring
for the full reasoning.
