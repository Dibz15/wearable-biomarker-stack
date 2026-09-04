# activefit parser (Amazfit Active 3 Premium)

**Status: real synced data confirmed for all 11 tables this parser
queries (via `scripts/check_table_usage.py` against the actual paired
watch), including full sleep-stage decoding from
`HUAMI_SLEEP_SESSION_SAMPLE`'s BLOB - ported directly from
Gadgetbridge's own source, not reverse-engineered. Some field
semantics (a few code meanings, one table's timestamp scale) are still
unverified - see "What's still unverified" below.**

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

## RESOLVED: activity/steps/continuous HR were being silently dropped

Root cause found via a live debugging session (Flux queries comparing
InfluxDB contents against `check_table_usage.py`'s row counts):
`HUAMI_EXTENDED_ACTIVITY_SAMPLE` uses **seconds-scale** timestamps,
not milliseconds like every other table this parser queries. Since
`extract_data()` used one shared, milliseconds-computed
`WHERE TIMESTAMP >= <bound>` clause for every table, a seconds-scale
comparison against that bound was always false - the SQL query itself
returned zero rows for this table specifically, silently, even though
it had 248 real rows in SQLite. Every other table (stress, SpO2,
temperature, HRV, PAI) worked fine in the same run using the same
(correct, for them) bound, which is exactly why this looked like a
Grafana problem at first rather than a table-specific unit mismatch.

Fixed with a separate `HUAMI_ACTIVITY_TIMESTAMPS_ARE_MS` flag
(confirmed `N`/seconds) and a per-table bound computation
(`compute_query_start_bound()`), independent of the general
`HUAMI_TIMESTAMPS_ARE_MS` flag (confirmed `Y`/milliseconds, correct
for every other table here) - see the config section in
`app/gadgetbridge_to_influxdb.py` for the full story.

**If you're deploying this fix**: the per-device checkpoint has
already been advanced to "recent" by every other section that was
working correctly this whole time, so simply restarting the parser
will only capture *new* activity data going forward - it won't
backfill the gap that built up while this was broken. Run
`scripts/clear_checkpoint.py` and temporarily widen `QUERY_DURATION`
to recover it - see that script's docstring for the exact steps.

## What's still unverified

Schema/data existence is confirmed for the tables below, but these
remain unknown until independently checked against real values:

| Question | Where |
|---|---|
| `GENERIC_HRV_VALUE_SAMPLE.VALUE` - same unit/algorithm as Colmi's HRV? | Both are written to the same `hrv` field for direct dashboard comparison (see the `${device}` filter in the main Grafana dashboard), but a ring and a watch may compute HRV differently (sensor placement, algorithm) - don't assume the two lines are apples-to-apples just because they're the same field name and same units aren't confirmed either |
| `RAW_KIND` / `RAW_INTENSITY` code meanings | **PARTIALLY RESOLVED** - confirmed directly from Gadgetbridge's `HuamiExtendedSampleProvider.java` source: `64`=outdoor_running, `115`=not_worn, `118`=charging, `120`=sleep (see `HUAMI_ACTIVITY_KIND_MAP`, and the `activity_kind_label` tag now added alongside the raw `activity_kind` tag for these). Real data has also shown `80`, `88`, `96`, `112` - not defined in this file, presumably from a parent/shared Huami constants class not yet pulled; these stay unmapped (raw numeric tag only) rather than guessed. `RAW_INTENSITY` itself is still undecoded for any value |
| Do `SLEEP`, `REM_SLEEP`, `DEEP_SLEEP` actually differ? | A real Gadgetbridge bug report ([issue #4715](https://codeberg.org/Freeyourgadget/Gadgetbridge/issues/4715)) observed `REM_SLEEP` and `DEEP_SLEEP` holding **identical** values on one device - fields are named `sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw` deliberately, so they aren't confused with Colmi's independently-verified `sleep_stage_*` fields |
| `TYPE_NUM` meaning on `HUAMI_STRESS_SAMPLE`/`HUAMI_SPO2_SAMPLE` | **RESOLVED** (see `FIELD_RESEARCH.md`) - both independently confirmed by matching deliberately-taken manual Zepp readings against their InfluxDB timestamps: `stress_type_num` and `spo2_type_num` each use 0=manual, 1=automatic |
| `GENERIC_TEMPERATURE_SAMPLE.TEMPERATURE_TYPE`/`TEMPERATURE_LOCATION` codes | Captured as tags, same as Colmi's own (also-unverified) temperature type/location codes - not decoded for either device |
| `HUAMI_ACTIVITY_SAMPLE` (fallback table) timestamp scale | Assumed to share `HUAMI_EXTENDED_ACTIVITY_SAMPLE`'s seconds scale (same older lineage), but this device doesn't populate that table, so it's unconfirmed |
| `HUAMI_SLEEP_SESSION_SAMPLE`'s outer SQL `TIMESTAMP` column scale | **RESOLVED** (Sept 2026, via `scripts/check_table_usage.py` against real data): confirmed **milliseconds** - `HUAMI_SLEEP_SESSION_TIMESTAMPS_ARE_MS` now defaults `Y`. Distinct from the blob's own internal timestamps, which are seconds (see below) - same table, two different timestamp representations at different scales, confirmed independently of each other |
| The unlabeled/unknown byte regions in the sleep session BLOB (offsets 0x0d-0x14, 0x17-0x53, the two single "always 1?" bytes at 0x08/0x09) | Not extracted - Gadgetbridge's own source doesn't use them either (per the ported code), so their meaning is unknown even to Gadgetbridge's maintainers, not just to this parser |

## Sleep-stage decoding (HUAMI_SLEEP_SESSION_SAMPLE)

**This is the real source of accurate sleep-stage data for this
device** - confirmed by comparing Gadgetbridge's own sleep graph
against the watch's display and the Zepp app, all agreeing, while
`HUAMI_EXTENDED_ACTIVITY_SAMPLE`'s `sleep_extended_raw`/`sleep_rem_raw`/
`sleep_deep_raw` columns were independently shown (via a live query
spanning a full night) to stay completely frozen for 9+ hours straight
- physiologically impossible for real stage tracking. Those columns
are kept (see the table above) since they're free and harmless, but
they are NOT the real signal.

The BLOB's byte layout is **not reverse-engineered** - it's ported
directly from Gadgetbridge's own `HuamiSleepSessionSampleProvider.java`
(fetched from `master`, September 2026), the same code that produces
the sleep graph confirmed accurate above. `decode_sleep_session_blob()`
in `app/gadgetbridge_to_influxdb.py` has the full field-by-field byte
offsets in its docstring. Confirmed directly from source, not inferred:

- Stage type codes: `4`=light, `5`=deep, `8`=rem, `7`=awake
- The blob's internal timestamps (`timestampSession`, `timestampMidnight`)
  are epoch **seconds** (Gadgetbridge does `new Date(x * 1000L)` on them)
- A fixed layout: header fields, up to 100 stage slots (5 bytes each:
  start/end in minutes-since-previous-midnight, plus type), then
  summary totals (total REM/light/deep/wake minutes, average HR, a
  0-100 sleep score) at fixed offsets after the stage array

**Confirmed, not assumed**: the SQL table's own outer `TIMESTAMP`
column scale (`HUAMI_SLEEP_SESSION_TIMESTAMPS_ARE_MS`) is
**milliseconds** - checked directly via `scripts/check_table_usage.py`'s
Scale column against real data. This is a *different* value from the
blob's own internal timestamps (seconds, confirmed from source above)
- the same table legitimately has two timestamp representations at two
different scales, each independently confirmed rather than assumed to
match the other.

Extracted, per session (`sample_type: "sleep_session"`, field names
matching Colmi's own where a direct equivalent exists):

- `sleep_session_start`, `sleep_session_wakeup`, `sleep_session_duration_s`
- `sleep_avg_hr`, `sleep_score` (Gadgetbridge's own computed 0-100 score)
- `rem_sleep_total_duration_s`, `light_sleep_total_duration_s`,
  `deep_sleep_total_duration_s`, `awake_sleep_total_duration_s`

And per stage transition (`sample_type: "sleep_stage"`), using the
**exact same pattern** as Colmi's own stage timeline extraction (start/
end active-window markers plus dense per-minute points) - so the
existing Sleep Stage Timeline Grafana panel works for both devices
without modification:

- `sleep_stage_duration_s`, `{stage}_sleep_duration_s`,
  `sleep_stage_active`, `sleep_stage_now`; tags `sleep_stage`,
  `sleep_stage_raw`

## activity_kind decoding

Confirmed directly from `HuamiExtendedSampleProvider.java` (the actual
class backing this table): `64`=outdoor_running, `115`=not_worn,
`118`=charging, `120`=sleep. Real observed data matches this cleanly -
e.g. `118` (charging) was the one code where `heart_rate` was
completely absent (not zero - missing) across every sample, which
makes sense with no wrist contact while on the charger. `80`, `88`,
`96`, `112` also show up in real data but aren't defined in this file -
presumably a parent/shared Huami constants class not yet pulled - the
`activity_kind_label` tag becomes `"unknown"` for these rather than
being omitted (see "Tag consistency" below for why that matters), and
the raw numeric `activity_kind` tag is always there regardless either
way.

## Tag consistency: always present, never conditionally omitted

Two real bugs so far came from the same root cause: a tag included on
some points but entirely absent on others (as opposed to present with
a different value everywhere) - to InfluxDB, that's a *different
series*, not a different value of the same series, which silently
fragments Grafana panels into extra, meaningless-looking series. Fixed
both the same way, and it's now the standing rule for every tag this
parser adds:

- `HUAMI_STRESS_SAMPLE`/`HUAMI_SPO2_SAMPLE`'s `TYPE_NUM` is `NULL` for
  some real rows (the earliest couple hours of a real export) -
  `stress_type_num`/`spo2_type_num` now becomes the string `"unknown"`
  instead of omitting the tag.
- `activity_kind_label` was only being added when `activity_kind`
  matched a confirmed code - now always present, `"unknown"` otherwise.

If you add a new tag to this parser, give it an explicit sentinel
value for the "don't know"/`NULL` case rather than conditionally
including the tag - confirmed directly against the real `influxdb_client`
library (not assumed) that `Point.tag(key, None)` silently drops the
tag from the line protocol entirely, which is what causes this.

**Why `sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw` and the
undifferentiated `activity_kind=120` aren't worth chasing further for
stage detail**: the same source file's `postProcess()` method confirms
`TYPE_CUSTOM_DEEP_SLEEP`/`REM_SLEEP`/`AWAKE_SLEEP` (121/122/123) are
assigned by Gadgetbridge itself, purely in-memory at read/display time,
by overlaying `HuamiSleepSessionSampleProvider`'s already-decoded
stages back onto activity samples for rendering - never written back
to the `RAW_KIND` column in SQLite. A real export can only ever
contain `120` here, never 121-123. The same method also shows
Gadgetbridge's own threshold-based fallback for the raw sleep byte
columns (`sample.getRemSleep() > 55`, the one issue #4715 called
"poorly" accurate) only runs when sleep-session data is *unavailable* -
since this device has working `HUAMI_SLEEP_SESSION_SAMPLE` data, that
fallback path never executes here either. Both are now confirmed
vestigial for this device, not just suspected from the frozen-all-
night observation.

## Workout/exercise summaries - not yet inventoried

`HuamiActivitySummaryParser.java` (also pulled from source, though not
wired into the parser) parses a completely separate, richer data
source: structured workout summaries (GPS track, pace, cadence, HR
zones, swim stroke data, etc.) for actual exercise sessions, backed by
`BaseActivitySummary` - not any of the `HUAMI_*`/`GENERIC_*`/`XIAOMI_*`
tables this parser currently scans for. Worth investigating if
structured workout data (as opposed to the continuous background
activity stream already extracted) becomes something worth pulling in.

## What's extracted

| Data | Table | Fields/tags written |
|---|---|---|
| Activity | `HUAMI_EXTENDED_ACTIVITY_SAMPLE` (falls back to `HUAMI_ACTIVITY_SAMPLE`) | `steps`, `heart_rate`, `raw_intensity`, `sleep_extended_raw`, `sleep_rem_raw`, `sleep_deep_raw`; tags `activity_kind` (raw numeric), `activity_kind_label` (decoded for confirmed codes, `"unknown"` otherwise - always present, never omitted, to avoid the same tag-presence series fragmentation described below) |
| HRV | `GENERIC_HRV_VALUE_SAMPLE` | `hrv` (same field name as Colmi, for direct comparison) |
| Temperature | `GENERIC_TEMPERATURE_SAMPLE` | `temperature`; tags `temperature_type`, `temperature_location` (same field/tag names as Colmi) |
| Resting HR | `HUAMI_HEART_RATE_RESTING_SAMPLE` | `resting_heart_rate` |
| Max HR | `HUAMI_HEART_RATE_MAX_SAMPLE` | `max_heart_rate` |
| Manual HR | `HUAMI_HEART_RATE_MANUAL_SAMPLE` | `manual_heart_rate` |
| Stress | `HUAMI_STRESS_SAMPLE` | `stress`, `stress_exc_sleep`; tag `stress_type_num` |
| SpO2 | `HUAMI_SPO2_SAMPLE` | `spo2`; tag `spo2_type_num` |
| Sleep respiratory rate | `HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE` | `sleep_respiratory_rate` |
| PAI | `HUAMI_PAI_SAMPLE` | `pai_low`, `pai_moderate`, `pai_high`, `pai_time_low_min`, `pai_time_moderate_min`, `pai_time_high_min`, `pai_today`, `pai_total` |
| Sleep sessions + stages | `HUAMI_SLEEP_SESSION_SAMPLE` | see "Sleep-stage decoding" above |
| Pre-computed activity summaries | `BASE_ACTIVITY_SUMMARY` (device-agnostic, no HUAMI_ prefix) | `duration_s`; tags `name` (`"Unset"` if null), `activity_kind_summary` (raw numeric, NOT assumed to share `activity_kind`'s code space above - a different table, unconfirmed either way), `sample_type: "activity_summary"`. Genuinely sparse in practice (see FIELD_RESEARCH.md) - populated only for deliberately-started workouts, not ambient daily movement. `SUMMARY_DATA`/`RAW_SUMMARY_DATA` (the richer per-workout breakdown) deliberately not extracted yet - content never inspected against a real row. |

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