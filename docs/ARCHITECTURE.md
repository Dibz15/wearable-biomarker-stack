# Architecture

For the high-level pitch and quick start, see the
[top-level README](../README.md); for the full setup walkthrough, see
[SETUP.md](./SETUP.md).

## How it fits together

```
Colmi ring (BLE)
   │
   ▼
Gadgetbridge (Android app, periodic auto-export)
   │  WebDAV
   ▼
Nextcloud (or any WebDAV server)
   │  WebDAV (pulled on a loop by the parser container)
   ▼
InfluxDB  ──────────────────────────────▶  Grafana (dashboards, alerts)
   ▲                                            │
   │ writes                                     ▼
wearable-events (calendar tags,             ntfy (push notifications)
subjective sleep score - has its
own login, one account per person)
   ▲
   │ ICS feed
Your calendar (Google Calendar, etc.)
```

Two custom images are built from this repo — the ring parser and
wearable-events (see
[Get the container images](./SETUP.md#1-get-the-container-images));
everything else (InfluxDB, Grafana, ntfy) is off-the-shelf.

## Repo structure

```
.
├── .github/workflows/
│   ├── docker-publish.yml          builds + pushes all THREE container images
│   └── tests.yml                   runs wearable-events' test suite on every push/PR
├── docker-compose.yml              the full stack: InfluxDB, Grafana, ntfy, both parsers, wearable-events
├── env.stack.example                copy to .env, fill in your own values
├── parser/                         device parsers - see parser/colmi/README.md and parser/amazfit/README.md
│   ├── common/                     shared, device-agnostic: WebDAV fetch, DEVICE table lookup,
│   │                               checkpoint mechanics, future-timestamp guard, InfluxDB write path
│   ├── colmi/                      Colmi/Yawell ring parser (COLMI_* tables) - functional
│   │   ├── app/gadgetbridge_to_influxdb.py
│   │   ├── Dockerfile
│   │   └── scripts/                 one-off maintenance scripts (checkpoint reset, historical data fixes)
│   └── amazfit/                  Amazfit Active 3 Premium parser (HUAMI_* tables) - best-effort,
│       ├── app/gadgetbridge_to_influxdb.py   runs safely pre-pairing (graceful no-op), but
│       ├── Dockerfile                        table/column guesses are unverified - see its README
│       └── README.md
├── grafana/                         Grafana datasource + alerting config-as-code (see docs/SETUP.md step 7)
│   ├── provisioning-templates/      templates - NOT read directly by Grafana, see render-provisioning.sh
│   └── render-provisioning.sh       renders templates into a Docker volume before Grafana starts
└── wearable-events/                calendar tagging + subjective sleep score, has its own web UI
    ├── app/                        FastAPI backend - routes/ and queries/ subpackages, one module per feature
    ├── static/                     the web UI itself (plain HTML/CSS/JS, static/js/ has one module per page)
    ├── tests/                      pytest suite (auth, keyword classification, calendar CRUD, etc.)
    └── schema.sql
```

## Known limitations

- **Timestamp units and sleep-stage codes are best-effort.** The
  Colmi/Gadgetbridge integration isn't officially documented — two
  defaults (`COLMI_TIMESTAMPS_ARE_MS` and `SLEEP_STAGE_MAP` in the
  parser script) should be verified against a few real nights of your
  own data. See the comments at each definition in
  [`parser/colmi/app/gadgetbridge_to_influxdb.py`](../parser/colmi/app/gadgetbridge_to_influxdb.py).
- **No password reset flow** in wearable-events — a forgotten password
  means editing the SQLite file directly or recreating the account.
- **Reprocessing calendar events** after a keyword-rule change is
  bounded by the local cache — it can't resurrect classification for
  events that rolled off the ICS feed before this stack ever saw them.
- **Grafana alerting provisioning is unverified against a live
  instance** — verify it actually works before relying on it. See
  [SETUP.md's alerting step](./SETUP.md#grafana-alerting-provisioned).
- **The HRV alert rule only watches `ALERT_HRV_SOURCE` (default
  `colmi`)** — the ring, not the watch, even on a stack running both
  parsers. `amazfit` extracts HRV too (`GENERIC_HRV_VALUE_SAMPLE`),
  it's just not alerted on by default; the two devices may compute HRV
  differently, so blending both into one rule isn't obviously more
  correct than picking one. Switch `ALERT_HRV_SOURCE` to `amazfit`,
  or copy the rule block in `rules.yaml` for a second, parallel rule
  scoped to the other source, once you have a preference.
- **The HRV alert rule is single-user**, watching one
  `ALERT_HRV_USER` — a household needs a copy of the rule block in
  `rules.yaml` per person, each with its own uid and user filter.
