# activefit parser (Amazfit Active 3 Premium)

**Status:** real synced data confirmed for all 11 tables this parser
queries, including full sleep-stage decoding from
`HUAMI_SLEEP_SESSION_SAMPLE`'s BLOB — ported directly from
Gadgetbridge's own source, not reverse-engineered. Some field
semantics are still unverified — see
[What's still unverified](#whats-still-unverified) below.

This is the HUAMI_*/GENERIC_* counterpart to [`../colmi/`](../colmi/README.md),
sharing the same device-agnostic plumbing from [`../common/`](../common)
(WebDAV fetch, DEVICE table lookup, checkpoint mechanics, the
future-timestamp guard, the InfluxDB write path).

## What "confirmed" means here

- **Schema-confirmed** — table/column exists in a real
  `sqlite3 Gadgetbridge.db .schema` dump (see [`schema.txt`](./schema.txt)).
  Doesn't mean the device actually writes to it.
- **Data-confirmed** — schema-confirmed *and* has real non-zero rows
  from an actual paired Active 3 Premium, via
  [`scripts/check_table_usage.py`](#checking-which-table-family-is-actually-used).
  The stronger claim.

HRV lives in `GENERIC_HRV_VALUE_SAMPLE`, not any `HUAMI_*`-prefixed
table — HRV support was likely added to Gadgetbridge after a refactor
consolidated newer fields into cross-vendor `GENERIC_*` tables, while
older fields (stress, SpO2, activity) stayed on legacy `HUAMI_*`
tables. Same story for body temperature (`GENERIC_TEMPERATURE_SAMPLE`).
`HUAMI_ACTIVITY_SAMPLE` (the older, pre-extended activity table) isn't
in the real schema dump — kept in the parser only as a defensive
fallback for older Huami devices, unverified against this device.

## Why this is safe to run continuously

Gadgetbridge's schema is generated at app-install time for every
supported device class, not dynamically per paired device — every
table this parser queries already exists (with zero rows) before a
watch is even paired.

Every query goes through `common.devices.run_query`, which catches
`sqlite3.OperationalError` (a genuinely missing table, or a mismatched
column) and returns `None` instead of raising:

- Zero rows in a window logs at **DEBUG**, treated as "nothing to sync
  this run" — not fatal.
- A column mismatch logs at **WARNING** and that section is skipped —
  everything else keeps working.

Tables that need a full sleep cycle or full day to populate (PAI, max
HR, sleep respiratory rate) will show zero rows until then — expected,
not a sign of a problem. Re-run
[`check_table_usage.py`](#checking-which-table-family-is-actually-used)
after a full day/night to confirm they fill in.

## What's still unverified

Schema/data existence is confirmed for the tables below, but these
remain unknown until independently checked against real values:

| Question | Notes |
|---|---|
| `GENERIC_HRV_VALUE_SAMPLE.VALUE` — same unit/algorithm as Colmi's HRV? | Both write to the same `hrv` field for direct dashboard comparison, but a ring and a watch may compute HRV differently (sensor placement, algorithm) — don't assume the two lines are apples-to-apples just because the field name matches |
| `RAW_KIND`/`RAW_INTENSITY` code meanings | `activity_kind` codes `64`=outdoor_running, `115`=not_worn, `118`=charging, `120`=sleep are confirmed from Gadgetbridge's own source (`HUAMI_ACTIVITY_KIND_MAP`). Codes `80`, `88`, `96`, `112` also appear in real data but aren't in that source file — stay unmapped (raw numeric tag only) rather than guessed. `RAW_INTENSITY` itself is undecoded for any value |
| Do `SLEEP`/`REM_SLEEP`/`DEEP_SLEEP` actually differ? | A Gadgetbridge bug report ([issue #4715](https://codeberg.org/Freeyourgadget/Gadgetbridge/issues/4715)) observed `REM_SLEEP` and `DEEP_SLEEP` holding identical values on one device — fields are named `sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw` deliberately, so they aren't confused with the real sleep-stage data (see below) |
| `GENERIC_TEMPERATURE_SAMPLE.TEMPERATURE_TYPE`/`TEMPERATURE_LOCATION` codes | Captured as tags, not decoded — same unverified state as Colmi's own temperature codes |
| Unlabeled byte regions in the sleep session BLOB (offsets `0x0d`-`0x14`, `0x17`-`0x53`, two single bytes at `0x08`/`0x09`) | Not extracted — Gadgetbridge's own source doesn't use them either, so their meaning is unknown even to Gadgetbridge's own maintainers |
| Daily distance/calorie totals | Confirmed computed watch-side (seen in Zepp's UI) but no matching column found in any `HUAMI_*`/`GENERIC_*` table after an exhaustive schema search — likely computed client-side in the Zepp app rather than synced. `steps × an estimated stride length` is a reasonable DIY approximation for distance if ever wanted |
| `BASE_ACTIVITY_SUMMARY.activity_kind_summary` meaning | Gadgetbridge decodes workout-type codes via generation-specific classes it hasn't fully mapped even upstream for this device's generation (confirmed via a real maintainer comment on [Gadgetbridge PR #2894](https://codeberg.org/Freeyourgadget/Gadgetbridge/pulls/2894)) — building an internal `(code, name)` map from real logged workouts over time is the practical path, not a source dive |

**Researched and closed, not just unimplemented:** a device-side
Hypopnea/Sleep-Apnea-Risk estimate (computed from raw sensor data, not
decoded from Zepp's own result) isn't feasible with what this parser
can access — three independent findings converge on this: the
mechanism is almost certainly SpO2-dip-based rather than
respiration-rate-based, Gadgetbridge has no decoded field for it on
any Huami device and its own docs cite real published research showing
poor correlation for this class of estimate on comparable hardware,
and this device's own SpO2 data during a real flagged event showed no
notable dip. Decoding whatever Zepp itself already computed, if it
happens to be hiding in the sleep-session BLOB's unlabeled byte
regions above, remains a separate, still-open possibility.

## Sleep-stage decoding (`HUAMI_SLEEP_SESSION_SAMPLE`)

**The real source of accurate sleep-stage data for this device** —
cross-checked against the watch's own display and the Zepp app, both
agreeing, while `HUAMI_EXTENDED_ACTIVITY_SAMPLE`'s
`sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw` columns were
independently shown to stay completely frozen for 9+ hours straight —
physiologically impossible for real stage tracking. Those columns are
kept (harmless) but are not the real signal.

The BLOB's byte layout is ported directly from Gadgetbridge's own
`HuamiSleepSessionSampleProvider.java`, not reverse-engineered.
`decode_sleep_session_blob()` in `app/gadgetbridge_to_influxdb.py` has
the full field-by-field byte offsets in its docstring:

- Stage type codes: `4`=light, `5`=deep, `8`=rem, `7`=awake
- The blob's internal timestamps (`timestampSession`, `timestampMidnight`)
  are epoch **seconds**
- A fixed layout: header fields, up to 100 stage slots (5 bytes each —
  start/end in minutes-since-previous-midnight, plus type), then
  summary totals (total REM/light/deep/wake minutes, average HR, a
  0–100 sleep score) at fixed offsets after the stage array

The table's own outer SQL `TIMESTAMP` column is a *separate*,
independently-confirmed **milliseconds** value — distinct from the
blob's internal seconds-based timestamps above; the same table
legitimately uses two different scales for two different things.

Extracted per session (`sample_type: "sleep_session"`, field names
matching Colmi's own where a direct equivalent exists):

- `sleep_session_start`, `sleep_session_wakeup`, `sleep_session_duration_s`
- `sleep_avg_hr`, `sleep_score` (Gadgetbridge's own computed 0–100 score)
- `rem_sleep_total_duration_s`, `light_sleep_total_duration_s`,
  `deep_sleep_total_duration_s`, `awake_sleep_total_duration_s`

And per stage transition (`sample_type: "sleep_stage"`), using the
same pattern as Colmi's own stage-timeline extraction (start/end
active-window markers plus dense per-minute points), so the existing
Sleep Stage Timeline Grafana panel works for both devices unmodified:
`sleep_stage_duration_s`, `{stage}_sleep_duration_s`,
`sleep_stage_active`, `sleep_stage_now`; tags `sleep_stage`,
`sleep_stage_raw`.

## `activity_kind` decoding

Confirmed directly from `HuamiExtendedSampleProvider.java`: `64`=outdoor_running,
`115`=not_worn, `118`=charging, `120`=sleep. Real data matches cleanly
— e.g. `118` (charging) is the one code where `heart_rate` is
completely absent, consistent with no wrist contact on the charger.
Codes `80`, `88`, `96`, `112` also appear but aren't in that source
file (presumably a parent/shared constants class not yet pulled) —
`activity_kind_label` becomes `"unknown"` for these rather than being
omitted (see [Tag consistency](#tag-consistency) below for why that
matters); the raw numeric `activity_kind` tag is always present either
way.

## Tag consistency

Give every new tag an explicit sentinel value for the "don't
know"/`NULL` case rather than conditionally including the tag.
`Point.tag(key, None)` silently drops the tag from the line protocol
entirely (confirmed against the real `influxdb_client` library) — a
tag present on some points but absent on others is a *different
series* to InfluxDB, not a different value of the same series, which
silently fragments Grafana panels into extra, meaningless-looking
series. Both `stress_type_num`/`spo2_type_num` (`NULL` on the earliest
rows of a real export) and `activity_kind_label` follow this rule now:
`"unknown"` instead of a dropped tag.

`sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw` and the
undifferentiated `activity_kind=120` aren't worth chasing further for
stage detail: Gadgetbridge's own `postProcess()` method confirms the
finer-grained codes (`TYPE_CUSTOM_DEEP_SLEEP`/`REM_SLEEP`/`AWAKE_SLEEP`,
121–123) are assigned purely in-memory at render time by overlaying
the already-decoded BLOB stages back onto activity samples — never
written to `RAW_KIND` in SQLite. A real export only ever contains
`120` here. The same method's own threshold-based fallback for the raw
sleep byte columns only runs when session data is *unavailable* — this
device has working session data, so that fallback path never executes
either.

## What's extracted

| Data | Table | Fields/tags written |
|---|---|---|
| Activity | `HUAMI_EXTENDED_ACTIVITY_SAMPLE` (falls back to `HUAMI_ACTIVITY_SAMPLE`) | `steps`, `heart_rate`, `raw_intensity`, `sleep_extended_raw`, `sleep_rem_raw`, `sleep_deep_raw`; tags `activity_kind` (raw numeric), `activity_kind_label` (decoded for confirmed codes, `"unknown"` otherwise) |
| HRV | `GENERIC_HRV_VALUE_SAMPLE` | `hrv` (same field name as Colmi, for direct comparison) |
| Temperature | `GENERIC_TEMPERATURE_SAMPLE` | `temperature`; tags `temperature_type`, `temperature_location` (same names as Colmi) |
| Resting HR | `HUAMI_HEART_RATE_RESTING_SAMPLE` | `resting_heart_rate` |
| Max HR | `HUAMI_HEART_RATE_MAX_SAMPLE` | `max_heart_rate` |
| Manual HR | `HUAMI_HEART_RATE_MANUAL_SAMPLE` | `manual_heart_rate` |
| Stress | `HUAMI_STRESS_SAMPLE` | `stress`, `stress_exc_sleep`; tag `stress_type_num` (`0`=manual, `1`=automatic, confirmed) |
| SpO2 | `HUAMI_SPO2_SAMPLE` | `spo2`; tag `spo2_type_num` (same 0/1 convention as stress, confirmed) |
| Sleep respiratory rate | `HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE` | `sleep_respiratory_rate` |
| PAI | `HUAMI_PAI_SAMPLE` | `pai_low`, `pai_moderate`, `pai_high`, `pai_time_low_min`, `pai_time_moderate_min`, `pai_time_high_min`, `pai_today`, `pai_total` |
| Sleep sessions + stages | `HUAMI_SLEEP_SESSION_SAMPLE` | see [Sleep-stage decoding](#sleep-stage-decoding-huami_sleep_session_sample) above |
| Workout summaries | `BASE_ACTIVITY_SUMMARY` (device-agnostic) | `duration_s`, `base_altitude_m`; tags `name`, `activity_kind_summary` (raw numeric, unmapped — see [What's still unverified](#whats-still-unverified)), `sample_type: "activity_summary"`. Sparse by nature — only deliberately-started workouts, not ambient movement. The richer Zepp OS `RAW_SUMMARY_DATA` breakdown (HR/HR zones, training load/effect, VO2max, elevation, cadence, swimming, movement evaluation) is decoded via `flatten_workout_summary()`, confirmed against Gadgetbridge's real parsing source rather than guessed — see that function's own docstring for the full field list and every scaling factor |
| Per-workout detail (per-sample GPS/HR/cadence) | Gadgetbridge's separate GPX/FIT track-export files, correlated via `BASE_ACTIVITY_SUMMARY.RAW_DETAILS_PATH` | `sample_type: "workout_detail"`, one point per sample (typically every 1–2s during a workout), tagged `workout_start_time` (links back to the parent summary) and `source_format` (`"fit"`/`"gpx"`). FIT preferred when available (richer — carries HR/cadence/power, works for non-GPS activities too); GPX only as a GPS-only fallback. Requires `EXPORT_TRACKS_PATH` configured — silently does nothing otherwise. Each workout is downloaded and parsed at most once ever, not re-fetched every sync |

## Before trusting your data

1. Re-run [`check_table_usage.py`](#checking-which-table-family-is-actually-used)
   after a first full night and day, to confirm PAI/max HR/sleep
   respiratory rate fill in.
2. Watch this container's logs for the extraction summary line —
   anything at WARNING means a table exists but a column didn't match.
3. Spot-check `sleep_extended_raw`/`sleep_rem_raw`/`sleep_deep_raw`
   against a night you remember clearly, given the identical-values
   caveat above.

## Checking which table family is actually used

`scripts/check_table_usage.py` fetches the current export (same WebDAV
fetch the parser itself uses) and reports row counts across every
`HUAMI_*`, `GENERIC_*`, and `XIAOMI_*` table in a real Gadgetbridge
schema — not just the tables this parser currently queries. Run it
inside the running container so it picks up the same `WEBDAV_*` env
vars:

```bash
docker cp parser/activefit/scripts/check_table_usage.py biomarker-parser-activefit:/tmp/check.py
docker exec -it biomarker-parser-activefit python3 /tmp/check.py
```

Non-zero tables not already extracted (flagged
`<- NOT YET EXTRACTED, has data!` in the output) are the concrete
starting point for extending `extract_data()` — real evidence instead
of another round of schema-reading. This is exactly how HRV and
temperature were found.

## Checkpoint isolation

This parser writes/reads its own checkpoint history independently of
Colmi's — every point gets a `source="activefit"` tag (vs Colmi's
`source="colmi"`), and `common.checkpoint.get_last_checkpoint_ns`
filters on it. This is what lets both parsers run simultaneously
against the same InfluxDB bucket/user without one's first-ever sync
inheriting the other's "already caught up" checkpoint. See
[`../common/checkpoint.py`](../common/checkpoint.py)'s module docstring
for the full reasoning.
