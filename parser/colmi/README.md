# Gadgetbridge to InfluxDB (Colmi fork)

Fetches a [Gadgetbridge](https://www.gadgetbridge.org/) database export from
a WebDAV server (e.g. Nextcloud) and writes biomarker data into
[InfluxDB](https://github.com/influxdata/influxdb) for dashboarding/alerting
in Grafana. Runs as a persistent service on an interval, not one-shot —
see [Running](#running) below.

This is a fork of [bentasker/gadgetbridge_to_influxdb](https://github.com/bentasker/gadgetbridge_to_influxdb)
(originally for Huami/Amazfit devices via `HUAMI_*` tables), adapted by
[Dibz15](https://github.com/Dibz15/colmi_gadgetbridge_to_influxdb) for
**Colmi/Yawell smart rings** (R02/R03/R06/R09/R10/R11/R12 family) via
the `COLMI_*` tables instead. This directory lives in a monorepo
alongside `../activefit/` (an Amazfit parser) and `wearable-events/` —
see the [top-level README](../../README.md) for how they fit together,
and `../common/` for the WebDAV fetch, checkpoint, and InfluxDB
write-path code shared between parsers.

## Gadgetbridge configuration

See [docs/SETUP.md](../../docs/SETUP.md#2-set-up-gadgetbridge) for
pointing Gadgetbridge's auto-export at your WebDAV server.

One detail specific to this parser: each export is a **full overwrite**
of the database file, not an incremental diff — this container re-reads
the whole file every run and relies on InfluxDB's identical-timestamp-
and-tags dedup to avoid duplicating points, so re-processing the same
file repeatedly is harmless.

## Configuration (environment variables)

| Variable | Description | Default |
|---|---|---|
| `WEBDAV_URL` | WebDAV server URL. For Nextcloud: `https://<domain>/remote.php/dav/` | — (required) |
| `WEBDAV_USER` | WebDAV username | — (required) |
| `WEBDAV_PASS` | WebDAV password (use a Nextcloud **app password**, not your login password) | — (required) |
| `WEBDAV_PATH` | Path to the export directory on the WebDAV server, e.g. `files/<nextcloud_user>/GadgetBridge/` | — (required) |
| `EXPORT_FILENAME` | Filename of the export on the WebDAV server | `gadgetbridge` |
| `QUERY_DURATION` | How far back (seconds) to query on the **first run only** - see [Checkpointed sync](#checkpointed-sync) below | `86400` |
| `MAX_CATCHUP_SECONDS` | Safety cap on catch-up distance if the last checkpoint is very old | `2592000` (30 days) |
| `CHECKPOINT_OVERLAP_SECONDS` | Overlap subtracted from the checkpoint before resuming, to avoid missing a boundary sample | `300` (5 min) |
| `INFLUXDB_URL` | InfluxDB server URL | — (required) |
| `INFLUXDB_TOKEN` | InfluxDB API token (or `user:pass` on 1.x) | — (required) |
| `INFLUXDB_ORG` | InfluxDB org name/ID | — (required) |
| `INFLUXDB_BUCKET` | InfluxDB bucket to write into | — (required) |
| `INFLUXDB_MEASUREMENT` | InfluxDB measurement name | `gadgetbridge` |
| `SLEEP_HOURS` | Comma-separated hours (0–23) treated as sleeping hours, for stress-field averaging | `0,1,2,3,4,5,6` |
| `SYNC_INTERVAL_SECONDS` | Seconds between sync runs. Set to `0` to run once and exit (for driving from an external cron instead) | `1800` |
| `GADGETBRIDGE_USER` | Tag identifying which person this data belongs to (see [Multi-user notes](../../docs/SETUP.md#multi-user-notes)) | `primary` |
| `PARSER_SOURCE` | Tag identifying which parser wrote a point, distinct from the physical `device` tag - scopes checkpoint lookups so a second device parser (e.g. `../activefit/`) sharing the same bucket/user doesn't inherit this one's sync history. Only change this if you know what you're doing - see `../common/checkpoint.py` | `colmi` |
| `MAX_FUTURE_TOLERANCE_SECONDS` | Tolerance for a sample/checkpoint being ahead of "now" before it's treated as corrupted data | `300` (5 min) |

> Field/table names above match Gadgetbridge's documented Colmi tables
> (`COLMI_HEART_RATE_SAMPLE`, `COLMI_HRV_VALUE_SAMPLE`,
> `COLMI_HRV_SUMMARY_SAMPLE`, `COLMI_SPO2_SAMPLE`, `COLMI_STRESS_SAMPLE`,
> `COLMI_TEMPERATURE_SAMPLE`, `COLMI_SLEEP_SESSION_SAMPLE`,
> `COLMI_SLEEP_STAGE_SAMPLE`, `COLMI_ACTIVITY_SAMPLE`). If a metric is
> missing from InfluxDB after a sync, compare `app/gadgetbridge_to_influxdb.py`'s
> query logic against your own export's real schema
> (`sqlite3 gadgetbridge.sqlite .schema`) — table/column names can drift
> slightly between Gadgetbridge versions.

## Checkpointed sync

Each run doesn't blindly re-query the last `QUERY_DURATION` seconds —
it checks InfluxDB for the most recent `last_seen` value from its own
`sync_check` points (written every run as a health marker) and resumes
from there:

- **No checkpoint found** (first run ever) — falls back to
  `QUERY_DURATION`.
- **Checkpoint recent** (normal operation) — resumes from just past the
  checkpoint, usually *narrower* than `QUERY_DURATION`, cutting down on
  redundant re-writes.
- **Checkpoint old** (container was down a while) — resumes from the
  checkpoint even if *further back* than `QUERY_DURATION`, so downtime
  gets backfilled instead of silently lost. Clamped to
  `MAX_CATCHUP_SECONDS` so a corrupted checkpoint can't trigger an
  unbounded resync.

## Running

### Via Docker Hub image (recommended)

```bash
docker run -d --name colmi-parser \
  -e WEBDAV_URL=https://nextcloud.example.invalid/remote.php/dav/ \
  -e WEBDAV_USER=youruser \
  -e WEBDAV_PASS=yourapppassword \
  -e WEBDAV_PATH=files/youruser/GadgetBridge/ \
  -e INFLUXDB_URL=http://influxdb:8086 \
  -e INFLUXDB_TOKEN=yourtoken \
  -e INFLUXDB_ORG=home \
  -e INFLUXDB_BUCKET=health \
  -e SYNC_INTERVAL_SECONDS=1800 \
  yourdockerhubuser/colmi2influx:latest
```

Or as part of the full `docker-compose.yml` stack (InfluxDB + Grafana +
ntfy + this parser) — see that file for the complete setup.

### Running once, from an external cron

```bash
docker run --rm \
  -e SYNC_INTERVAL_SECONDS=0 \
  -e WEBDAV_URL=... \
  ... \
  yourdockerhubuser/colmi2influx:latest
```

### Running directly (no container)

```bash
pip install webdavclient3 influxdb-client loguru
# export the env vars above
# run from parser/ (not this directory) so `common` is importable as
# a sibling package - PYTHONPATH=. makes parser/ itself the import root
cd parser && PYTHONPATH=. python3 colmi/app/gadgetbridge_to_influxdb.py
```

## Building and publishing your own image

This directory is one of three images built by the monorepo workflow at
[`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml)
(repo root) — it rebuilds automatically on every push to `main` that
touches `parser/common/` or this directory (and on `v*.*.*` tags).

1. Create a Docker Hub repository, e.g. `colmi2influx`.
2. Generate a Docker Hub access token (Account Settings → Security).
3. In the repo's GitHub Settings → Secrets and variables → Actions, add
   `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.
4. Push to `main` — the workflow builds `linux/amd64` and `linux/arm64`
   images and pushes `:latest`, `:<git-sha>`, and (on tags) `:<semver>`.

To build locally instead — build context is `../` (`parser/`), not
this directory, since the image needs `../common/` too:

```bash
cd parser
docker build -f colmi/Dockerfile -t colmi2influx .
```

## Before trusting your data

- **Timestamp units are confirmed correct as shipped** —
  `COLMI_TIMESTAMPS_ARE_MS` defaults to `Y` (milliseconds), verified
  against real R09 hardware. See the comment above that constant in
  `app/gadgetbridge_to_influxdb.py` if your own device's dates land
  implausibly far in the future in Grafana; flip it to `N` if so.
- **Sleep stage codes are not independently confirmed.**
  `COLMI_SLEEP_STAGE_SAMPLE.STAGE`'s integer-to-stage mapping is a
  best-effort default — cross-check a night you remember clearly
  against what lands in InfluxDB.

## License

Copyright (c) 2023 B Tasker (original), with modifications by Dibz15 (Colmi
table adaptation) and this fork (loop wrapper, CI, multi-device
restructure factoring shared code into `../common/`). Released under the
[BSD 3-Clause License](https://www.bentasker.co.uk/pages/licenses/bsd-3-clause.html).
