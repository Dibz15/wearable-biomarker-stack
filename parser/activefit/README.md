# activefit parser (Amazfit Active 3 Premium)

**Status: real synced data confirmed for 10 of the tables below (via
`scripts/check_table_usage.py` against the actual paired watch).
Semantics (units, code meanings) are still not independently verified
- see "What's still unverified" below.**

This is the HUAMI_*/GENERIC_* counterpart to [`../colmi/`](../colmi/README.md),
sharing the same device-agnostic plumbing from [`../common/`](../common)
(WebDAV fetch, DEVICE table lookup, checkpoint mechanics, the
future-timestamp guard, the InfluxDB write path).

## What "confirmed" means here

Two levels of confirmation, worth distinguishing:

1. **Schema-confirmed** - table/column exists, from a real
   `sqlite3 Gadgetbridge.db .schema` dump (see `CREATE TABLE`
   statements below). Doesn't mean the device actually writes to it.
2. **Data-confirmed** - schema-confirmed AND has real non-zero rows
   from the actual paired Active 3 Premium, found by running
   `scripts/check_table_usage.py` against a live export after the
   first sync. This is the stronger claim.

```sql
CREATE TABLE IF NOT EXISTS "HUAMI_EXTENDED_ACTIVITY_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"RAW_INTENSITY" INTEGER NOT NULL ,"STEPS" INTEGER NOT NULL ,"RAW_KIND" INTEGER NOT NULL ,"HEART_RATE" INTEGER NOT NULL ,"UNKNOWN1" INTEGER,"SLEEP" INTEGER,"DEEP_SLEEP" INTEGER,"REM_SLEEP" INTEGER,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS "HUAMI_STRESS_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"TYPE_NUM" INTEGER NOT NULL ,"STRESS" INTEGER NOT NULL ,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS "HUAMI_SPO2_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"TYPE_NUM" INTEGER NOT NULL ,"SPO2" INTEGER NOT NULL ,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS "HUAMI_HEART_RATE_MANUAL_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"UTC_OFFSET" INTEGER NOT NULL ,"HEART_RATE" INTEGER NOT NULL ,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS "HUAMI_HEART_RATE_MAX_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"UTC_OFFSET" INTEGER NOT NULL ,"HEART_RATE" INTEGER NOT NULL ,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS "HUAMI_HEART_RATE_RESTING_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"UTC_OFFSET" INTEGER NOT NULL ,"HEART_RATE" INTEGER NOT NULL ,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS "HUAMI_PAI_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"UTC_OFFSET" INTEGER NOT NULL ,"PAI_LOW" REAL NOT NULL ,"PAI_MODERATE" REAL NOT NULL ,"PAI_HIGH" REAL NOT NULL ,"TIME_LOW" INTEGER NOT NULL ,"TIME_MODERATE" INTEGER NOT NULL ,"TIME_HIGH" INTEGER NOT NULL ,"PAI_TODAY" REAL NOT NULL ,"PAI_TOTAL" REAL NOT NULL ,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS "HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"UTC_OFFSET" INTEGER NOT NULL ,"RATE" INTEGER NOT NULL ,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS "HUAMI_SLEEP_SESSION_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"DATA"BLOB,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS "GENERIC_HRV_VALUE_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"VALUE" INTEGER NOT NULL ,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS "GENERIC_TEMPERATURE_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,"DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,"TEMPERATURE" REAL NOT NULL ,"TEMPERATURE_TYPE" INTEGER NOT NULL ,"TEMPERATURE_LOCATION" INTEGER NOT NULL ,PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
```

**Real finding, not a guess anymore:** HRV lives in `GENERIC_HRV_VALUE_SAMPLE`,
not any `HUAMI_*`-prefixed table - confirmed against actual watch data.
This matches the hypothesis that HRV support was added to Gadgetbridge
after a refactor consolidated some newer fields into the cross-vendor
`GENERIC_*` tables, while older fields (stress, SpO2, activity) stayed
on their legacy `HUAMI_*` tables. Same story for body temperature
(`GENERIC_TEMPERATURE_SAMPLE`) - both were "documented feature, no
corresponding `HUAMI_*` table" gaps noted earlier, both resolved by
checking `GENERIC_*` instead of assuming the feature wasn't persisted
at all.

Note the schema dump did **not** include `HUAMI_ACTIVITY_SAMPLE` (the
older, pre-extended table) - it's kept in the parser only as a
defensive fallback for older Huami devices, not something verified
against this specific Gadgetbridge install.

## Confirmed with real data (via `scripts/check_table_usage.py`)

- `HUAMI_SLEEP_SESSION_SAMPLE` - **has real rows already**, even from
  only a couple hours of pre-sleep wear. Still a BLOB column, still not
  decoded - see "Not yet extracted" below.
- `GENERIC_HRV_VALUE_SAMPLE` - has real rows. Now extracted (see below).
- `GENERIC_TEMPERATURE_SAMPLE` - has real rows. Now extracted (see below).
- Everything else in `ALREADY_IMPLEMENTED` was still zero rows at
  check time - expected for anything that needs a full sleep cycle
  (sleep-adjacent respiratory rate, resting HR) or a full day (PAI,
  max HR) to populate, not a sign anything's wrong. Worth re-running
  `check_table_usage.py` after a first full night and a full day to
  confirm those fill in rather than staying empty.

## Why this is safe to run continuously

Gadgetbridge's schema is generated at app-install time for every
supported device class - it's not created dynamically per paired
device, which is why every table above already existed (with zero
rows) before the watch was even paired.

Every table query here also goes through `common.devices.run_query`,
which catches `sqlite3.OperationalError` (e.g. a genuinely missing
table, or a column that doesn't match) and returns `None` instead of
raising. Concretely:

- Zero rows in a window (not an error) logs at **DEBUG** and is
  treated as "nothing to sync this run" - not fatal, won't crash-loop
  the container.
- A column mismatch logs at **WARNING** and that section is skipped -
  everything else keeps working.

## What's still unverified

Schema/data existence is confirmed for the tables below, but these
remain unknown until independently checked against real values:

| Question | Where |
|---|---|
| Timestamp unit (ms vs s) | `HUAMI_TIMESTAMPS_ARE_MS` - same style of check as `COLMI_TIMESTAMPS_ARE_MS`: watch for InfluxDB "value out of range" write errors, or implausible far-future graphed data |
| `GENERIC_HRV_VALUE_SAMPLE.VALUE` - same unit/algorithm as Colmi's HRV? | Both are written to the same `hrv` field for direct dashboard comparison (see the `${device}` filter in the main Grafana dashboard), but a ring and a watch may compute HRV differently (sensor placement, algorithm) - don't assume the two lines are apples-to-apples just because they're the same field name and same units aren't confirmed either |
| `RAW_KIND` / `RAW_INTENSITY` code meanings | Stored raw (as `activity_kind` tag / `raw_intensity` field), same situation Colmi's `activity_kind` tag is in - not decoded, device-specific |
| Do `SLEEP`, `REM_SLEEP`, `DEEP_SLEEP` actually differ? | A real Gadgetbridge bug report ([issue #4715](https://codeberg.org/Freeyourgadget/Gadgetbridge/issues/4715)) observed `REM_SLEEP` and `DEEP_SLEEP` holding **identical** values on one device - fields are named `sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw` deliberately, so they aren't confused with Colmi's independently-verified `sleep_stage_*` fields |
| `TYPE_NUM` meaning on `HUAMI_STRESS_SAMPLE`/`HUAMI_SPO2_SAMPLE` | Gadgetbridge's Zepp OS feature list documents "automatic and manual" stress measurements and SpO2 monitoring, so `TYPE_NUM` is presumed to distinguish those - captured as a tag (`stress_type_num`/`spo2_type_num`) rather than decoded, so it's filterable in Grafana once confirmed without a parser change |
| `GENERIC_TEMPERATURE_SAMPLE.TEMPERATURE_TYPE`/`TEMPERATURE_LOCATION` codes | Captured as tags, same as Colmi's own (also-unverified) temperature type/location codes - not decoded for either device |

## Not yet extracted

- **Sleep sessions (per-night, stage-level like Colmi's).**
  `HUAMI_SLEEP_SESSION_SAMPLE` now has **confirmed real data**, but
  it's a `DATA` BLOB column, not queryable rows - decoding that format
  is real reverse-engineering work (inspecting raw bytes, likely
  cross-referencing Gadgetbridge's own Java source for how it's
  written) that hasn't been started. The `SLEEP`/`REM_SLEEP`/`DEEP_SLEEP`
  columns on the activity table and the respiratory-rate table remain
  the only sleep-related data currently extracted. Worth revisiting
  now that there's a real BLOB sample to inspect, if useful enough to
  be worth the effort.

## What's extracted

| Data | Table | Fields/tags written |
|---|---|---|
| Activity | `HUAMI_EXTENDED_ACTIVITY_SAMPLE` (falls back to `HUAMI_ACTIVITY_SAMPLE`) | `steps`, `heart_rate`, `raw_intensity`, `sleep_extended_raw`, `sleep_rem_raw`, `sleep_deep_raw`; tag `activity_kind` |
| HRV | `GENERIC_HRV_VALUE_SAMPLE` | `hrv` (same field name as Colmi, for direct comparison) |
| Temperature | `GENERIC_TEMPERATURE_SAMPLE` | `temperature`; tags `temperature_type`, `temperature_location` (same field/tag names as Colmi) |
| Resting HR | `HUAMI_HEART_RATE_RESTING_SAMPLE` | `resting_heart_rate` |
| Max HR | `HUAMI_HEART_RATE_MAX_SAMPLE` | `max_heart_rate` |
| Manual HR | `HUAMI_HEART_RATE_MANUAL_SAMPLE` | `manual_heart_rate` |
| Stress | `HUAMI_STRESS_SAMPLE` | `stress`, `stress_exc_sleep`; tag `stress_type_num` |
| SpO2 | `HUAMI_SPO2_SAMPLE` | `spo2`; tag `spo2_type_num` |
| Sleep respiratory rate | `HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE` | `sleep_respiratory_rate` |
| PAI | `HUAMI_PAI_SAMPLE` | `pai_low`, `pai_moderate`, `pai_high`, `pai_time_low_min`, `pai_time_moderate_min`, `pai_time_high_min`, `pai_today`, `pai_total` |

## ACTION NEEDED: the HRV alert rule is now under-scoped, not just cautious

`$ALERT_HRV_SOURCE` (see `env.stack.example` and
`grafana/provisioning-templates/alerting/rules.yaml.template`) defaults
to `colmi`, on the reasoning "activefit doesn't extract HRV yet." That
reasoning no longer holds - HRV is now extracted from
`GENERIC_HRV_VALUE_SAMPLE`. The alert currently still only evaluates
the ring's HRV; the watch's HRV is being collected but not alerted on.

This is a real decision, not a mechanical follow-up: given the
ring/watch may compute HRV differently (see the unverified-units row
above), blending both into one `mean()` may not be more meaningful
than picking one. Options, roughly in order of how much they change
current behaviour:

- Leave `ALERT_HRV_SOURCE=colmi` as-is (no change, watch's HRV
  collected but not alerted on)
- Switch it to `activefit` if the watch turns out to be the
  more-worn/more-reliable device
- Add a second, parallel alert rule scoped to the other source, so
  both devices get independent HRV-drop alerts
- Something that explicitly averages/compares both once their units
  and reliability are actually compared against each other

Not resolved here - worth deciding once there's enough HRV data from
both devices to compare them directly.

## Before trusting any of this data

1. Re-run `scripts/check_table_usage.py` after a first full night and
   a full day, to confirm the still-zero tables (PAI, max HR, sleep
   respiratory rate) fill in as expected rather than staying empty.
2. Watch this container's logs for the extraction summary line - it
   lists row counts per section. Anything at WARNING means a table
   exists but a column didn't match.
3. Confirm the timestamp unit (see table above).
4. Spot-check `sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw`
   against a night you remember clearly, given the identical-values
   caveat.
5. Decide the HRV alert scoping question above.
6. Once verified, update this file's status line and the "still
   unverified" table.

## Checking which table family is actually used

`scripts/check_table_usage.py` fetches the current export (same WebDAV
fetch the parser itself uses) and reports row counts across every
`HUAMI_*`, `GENERIC_*`, and `XIAOMI_*` table found in a real
Gadgetbridge schema dump - not just the tables this parser currently
queries. Run it inside the running container so it picks up the same
`WEBDAV_*` env vars:

```bash
docker cp parser/activefit/scripts/check_table_usage.py biomarker-parser-activefit:/tmp/check.py
docker exec -it biomarker-parser-activefit python3 /tmp/check.py
```

Non-zero tables not already in `ALREADY_IMPLEMENTED` (flagged
`<- NOT YET EXTRACTED, has data!` in the output) are the concrete
starting point for extending `extract_data()` - real evidence instead
of another round of schema-reading. This is exactly how HRV and
temperature were found.

## Checkpoint isolation

This parser already writes/reads its own checkpoint history
independently of Colmi's - every point gets a `source="activefit"` tag
(vs Colmi's `source="colmi"`), and `common.checkpoint.get_last_checkpoint_ns`
filters on it. This is what lets both parsers run simultaneously
against the same InfluxDB bucket/user without one's first-ever sync
inheriting the other's "already caught up" checkpoint. See
[`../common/checkpoint.py`](../common/checkpoint.py)'s module docstring
for the full reasoning.