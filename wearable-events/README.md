# wearable-events

Calendar tagging + subjective sleep score, as a companion to the
[Colmi ring parser](../parser/colmi/README.md). Adds a "context" layer
on top of raw sensor data — manual one-tap tags (caffeine, alcohol,
social), calendar-derived tags (meeting, deep-work, commute, resolved
from ICS feeds via keyword rules), and a nightly 1–5 subjective sleep
score — all written into the same InfluxDB bucket so they can
eventually be correlated against HRV/HR/temperature trends.

Has its own login (multiple household members, one account each) and a
small web UI served directly from the FastAPI backend — no separate
frontend build step.

## Running it for development

You don't need Docker to iterate on this — it's a plain FastAPI app.

```bash
cd wearable-events
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export SQLITE_PATH=./dev-config.db
export INFLUX_URL=http://localhost:8086      # or wherever your dev InfluxDB is
export INFLUX_TOKEN=devtoken
export INFLUX_ORG=home
export INFLUX_BUCKET=health
export ADMIN_USERNAME=dev
export ADMIN_PASSWORD=devpassword

uvicorn app.main:app --reload --port 8080
```

Open `http://localhost:8080` — you'll get the login screen, log in with
the `ADMIN_USERNAME`/`ADMIN_PASSWORD` you set (bootstrap only fires once,
on an empty `users` table, so delete `dev-config.db` to reset it).

Auto-reload (`--reload`) picks up changes to `app/*.py` immediately. The
static files under `static/` are served fresh on every request (no
caching), so editing HTML/CSS/JS just needs a browser refresh.

You don't strictly need a real ring or InfluxDB data to develop most of
this app — calendars, keyword rules, tag buttons, and manual event
logging all work against an empty InfluxDB. The one thing that needs
real sensor data to test end-to-end is `/sleep`, since it queries for an
actual completed sleep session (see
[Known limitations](./docs/REFERENCE.md#known-limitations)).

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

No live InfluxDB or Docker needed — the suite uses a temp SQLite file
and never contacts a real Influx server (see `tests/conftest.py`).
Runs automatically on every push/PR via
[`.github/workflows/tests.yml`](../.github/workflows/tests.yml).

Coverage is deliberately "basic," not exhaustive: auth/session
handling, the SPA fallback route, keyword-rule classification, the
sleep `entry_id` addressing scheme (protects a real historical bug —
see [docs/REFERENCE.md](./docs/REFERENCE.md#sleep-entry-addressing-entry_id-not-sleep_date)),
and CRUD + multi-tenant isolation for calendars and tag definitions. It
doesn't cover every endpoint — contributions adding tests for other
areas (vitals, activity, sleep detail views) are welcome.

## Documentation

- **[docs/REFERENCE.md](./docs/REFERENCE.md)** — architecture, directory
  structure, environment variables, auth model, data model, full API
  reference, keyword-rule classification, sleep entry addressing,
  manual tag overrides, reprocessing internals, known limitations
- **[UI_DESIGN_NOTES.md](./UI_DESIGN_NOTES.md)** — running design notes
  on page layouts and UX patterns (mostly reverse-engineered from the
  Zepp app), cross-referenced against
  [`parser/activefit/README.md`](../parser/activefit/README.md)
  for which underlying data is confirmed to exist

## License

[BSD 3-Clause](../LICENSE), same as the rest of the repo.
