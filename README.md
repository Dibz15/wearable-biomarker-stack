# Self-Hosted Wearable Biomarker Stack

A self-hosted alternative to subscription-gated wearable health apps.

Wear a cheap smart device and own your own data. This stack pulls biomarker
data (heart rate, HRV, SpO2, temperature, sleep) off a Colmi/Yawell smart
ring via [Gadgetbridge](https://gadgetbridge.org/) (or Amazfit watch), stores it in your own
InfluxDB instance, and visualizes it in Grafana. No vendor cloud need, no
per-user fees, etc. A companion service
([`wearable-events`](./wearable-events/README.md)) adds manual context
tagging (caffeine, alcohol, meetings pulled from your calendar) and a
subjective sleep-quality score, so raw sensor trends can eventually be
correlated against what was actually happening in your day.

An Amazfit parser (`parser/amazfit/`) optionally runs alongside the ring parser
for a second device — best-effort, some field semantics unverified. See
[`parser/amazfit/README.md`](./parser/amazfit/README.md).

Everything runs in Docker, tested against [CasaOS](https://casaos.io/)
but works with plain `docker compose` anywhere.

**Is:** a data-ownership layer. It's your raw sensor data, your database,
your dashboards and alerting rules.
**Isn't:** a polished consumer app. No proprietary "readiness score". The
device itself computes a basic HRV baseline, and this stack otherwise
gives you the raw trends to build on.

## Quick start

1. Pair your device in Gadgetbridge, point its auto-export at a WebDAV server.
2. `cp env.stack.example .env` and fill in your InfluxDB token, WebDAV credentials, and image locations.
3. `docker compose --profile all up -d` (see [setup](./docs/SETUP.md#running-just-one-device) to run just one device type)
4. Open InfluxDB (`:8086`), Grafana (`:3000`), and wearable-events (`:8081`) to finish setup on each.

Full walkthrough with all the details (Gadgetbridge configuration,
`.env` reference, ntfy/alerting setup, multi-user households) is in
**[docs/SETUP.md](./docs/SETUP.md)**.

## Development

Each component has its own dev setup and test instructions:

- [`wearable-events/README.md`](./wearable-events/README.md) — the
  companion FastAPI app + web UI (calendar tagging, sleep journal)
- [`parser/colmi/README.md`](./parser/colmi/README.md) — the Colmi/Yawell
  ring parser
- [`parser/amazfit/README.md`](./parser/amazfit/README.md) — the
  Amazfit parser

## Documentation

- **[docs/SETUP.md](./docs/SETUP.md)** — full setup walkthrough
- **[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)** — how the pieces
  fit together, repo layout, known limitations

## Contributing

Bug reports, real-device data confirming/contradicting the unverified
bits noted throughout these docs, and PRs are all welcome — see
[`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

[BSD 3-Clause](./LICENSE) for the whole repo. The ring parser
specifically is a derivative of
[bentasker/gadgetbridge_to_influxdb](https://github.com/bentasker/gadgetbridge_to_influxdb)
(same license) — see [`parser/colmi/README.md`](./parser/colmi/README.md)
for its full attribution.
