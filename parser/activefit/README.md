# activefit parser (Amazfit Active 3 Premium)

**Status: schema CONFIRMED against a real Gadgetbridge schema dump,
semantics still not verified against real Active 3 Premium data.**

This is the HUAMI_* counterpart to [`../colmi/`](../colmi/README.md),
sharing the same device-agnostic plumbing from [`../common/`](../common)
(WebDAV fetch, DEVICE table lookup, checkpoint mechanics, the
future-timestamp guard, the InfluxDB write path).

## What "confirmed" means here

Every table and column this parser queries comes directly from a real
`sqlite3 Gadgetbridge.db .schema` dump, grepped for `HUAMI_*` rows -
not secondhand research. The exact `CREATE TABLE` statements are
reproduced below for reference. This is a big step up from guessing,
but confirming a table/column *exists* is not the same as confirming
what its *values mean* - see "What's still unverified" below.

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
```

Note this dump did **not** include `HUAMI_ACTIVITY_SAMPLE` (the older,
pre-extended table) - it's kept in the parser only as a defensive
fallback for older Huami devices, not something verified against this
specific Gadgetbridge install.

Cross-referencing against Gadgetbridge's documented Zepp OS feature
list: PAI, resting heart rate, stress (automatic *and* manual), SpO2,
and sleep respiratory rate are all listed as supported "Activity sync"
features, which lines up with the dedicated tables above. Two
documented features notably have **no** corresponding table in this
dump: **sleep score** and **body temperature** (listed as
device-dependent). Either they're not written to the database at all
(computed on-device/in-app only), stored somewhere outside the
`HUAMI_*` tables, or simply absent because this particular
Gadgetbridge build/device combination doesn't populate them - not
something this parser can resolve without a real paired export to
check against.

## Why this is safe to deploy before pairing the watch

Gadgetbridge's schema is generated at app-install time for every
supported device class - it's not created dynamically per paired
device. That's exactly why the tables above already showed up in a
schema dump before the Active 3 Premium was paired: they exist, just
with zero rows, until a watch actually syncs data into them.

Every table query here also goes through `common.devices.run_query`,
which catches `sqlite3.OperationalError` (e.g. a genuinely missing
table, or a column that doesn't match) and returns `None` instead of
raising. Concretely:

- Before pairing: every `HUAMI_*` query returns zero rows (not an
  error) at **DEBUG** log level. `extract_data()` returns an empty
  list, and `__main__` treats that as "nothing to sync this run" - not
  a fatal error. It will **not** crash-loop the container under
  `restart: unless-stopped`.
- If some future Gadgetbridge version renames or drops a column, that
  one section logs a **WARNING** and is skipped - everything else
  keeps working.

Verified directly: ran `extract_data()` against an in-memory SQLite
database built from these exact `CREATE TABLE` statements, both with
zero rows in every table (pre-pairing case - returns `[]` safely) and
with one row in each (happy-path case - all 8 queryable tables extract
correctly). See conversation history for the test harness.

## What's still unverified

Confirming the schema is a big step, but these remain unknown until
real Active 3 Premium data exists:

| Question | Where |
|---|---|
| Timestamp unit (ms vs s) | `HUAMI_TIMESTAMPS_ARE_MS` - same style of check as `COLMI_TIMESTAMPS_ARE_MS`: watch for InfluxDB "value out of range" write errors, or implausible far-future graphed data |
| `RAW_KIND` / `RAW_INTENSITY` code meanings | Stored raw (as `activity_kind` tag / `raw_intensity` field), same situation Colmi's `activity_kind` tag is in - not decoded, device-specific |
| Do `SLEEP`, `REM_SLEEP`, `DEEP_SLEEP` actually differ? | A real Gadgetbridge bug report ([issue #4715](https://codeberg.org/Freeyourgadget/Gadgetbridge/issues/4715)) observed `REM_SLEEP` and `DEEP_SLEEP` holding **identical** values on one device - fields are named `sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw` deliberately, so they aren't confused with Colmi's independently-verified `sleep_stage_*` fields |
| `TYPE_NUM` meaning on `HUAMI_STRESS_SAMPLE`/`HUAMI_SPO2_SAMPLE` | Gadgetbridge's Zepp OS feature list documents "automatic and manual" stress measurements and SpO2 monitoring, so `TYPE_NUM` is presumed to distinguish those - captured as a tag (`stress_type_num`/`spo2_type_num`) rather than decoded, so it's filterable in Grafana once confirmed without a parser change |
| Sleep sessions (per-night, stage-level like Colmi's) | **Not attempted.** `HUAMI_SLEEP_SESSION_SAMPLE` is confirmed to exist, but its per-night detail lives in a `DATA` BLOB column, not queryable rows - decoding that is real reverse-engineering work this parser doesn't do. The `SLEEP`/`REM_SLEEP`/`DEEP_SLEEP` columns on the activity table and the respiratory-rate table are the only sleep-related data currently extracted |

## What's extracted

| Data | Table | Fields/tags written |
|---|---|---|
| Activity | `HUAMI_EXTENDED_ACTIVITY_SAMPLE` (falls back to `HUAMI_ACTIVITY_SAMPLE`) | `steps`, `heart_rate`, `raw_intensity`, `sleep_extended_raw`, `sleep_rem_raw`, `sleep_deep_raw`; tag `activity_kind` |
| Resting HR | `HUAMI_HEART_RATE_RESTING_SAMPLE` | `resting_heart_rate` |
| Max HR | `HUAMI_HEART_RATE_MAX_SAMPLE` | `max_heart_rate` |
| Manual HR | `HUAMI_HEART_RATE_MANUAL_SAMPLE` | `manual_heart_rate` |
| Stress | `HUAMI_STRESS_SAMPLE` | `stress`, `stress_exc_sleep`; tag `stress_type_num` |
| SpO2 | `HUAMI_SPO2_SAMPLE` | `spo2`; tag `spo2_type_num` |
| Sleep respiratory rate | `HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE` | `sleep_respiratory_rate` |
| PAI | `HUAMI_PAI_SAMPLE` | `pai_low`, `pai_moderate`, `pai_high`, `pai_time_low_min`, `pai_time_moderate_min`, `pai_time_high_min`, `pai_today`, `pai_total` |

## Before trusting any of this data

1. Pair the Active 3 Premium with Gadgetbridge and let it sync/export
   at least once.
2. Watch this container's logs for the extraction summary line - it
   lists row counts per section. Anything at WARNING means a table
   exists but a column didn't match - worth investigating, since it
   means Gadgetbridge's schema differs from the dump this was built
   against.
3. Confirm the timestamp unit (see table above).
4. Spot-check `sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw`
   against a night you remember clearly, given the identical-values
   caveat - these may need dropping or reinterpreting rather than
   trusted as-is.
5. Once verified, update this file's status line and the "still
   unverified" table above - and decide whether the not-yet-attempted
   sleep session BLOB is worth decoding, or whether the activity-table
   sleep columns turn out to be good enough on their own.

## Checkpoint isolation

This parser already writes/reads its own checkpoint history
independently of Colmi's - every point gets a `source="activefit"` tag
(vs Colmi's `source="colmi"`), and `common.checkpoint.get_last_checkpoint_ns`
filters on it. This is what lets both parsers run simultaneously
against the same InfluxDB bucket/user without one's first-ever sync
inheriting the other's "already caught up" checkpoint. See
[`../common/checkpoint.py`](../common/checkpoint.py)'s module docstring
for the full reasoning.