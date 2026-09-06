# Setup

Full step-by-step walkthrough for getting the stack running. For the
high-level pitch and a quick-start summary, see the
[top-level README](../README.md).

## Prerequisites

- A Linux host that can run Docker + Docker Compose (NAS, Raspberry
  Pi, home server — anything).
- A Colmi/Yawell smart ring. `R09` is a good starting point. See [`parser/colmi/README.md`](../parser/colmi/README.md)
  for per-model notes. This repo also supports newer Amazfit/Huami watches, such as the Active 3 Premium ([`parser/activefit/README.md`](../parser/activefit/README.md))
- [Gadgetbridge](https://gadgetbridge.org/) on an Android phone (from
  F-Droid, not Google Play).
- A WebDAV server reachable from both your phone and your Docker host
  (built and tested against Nextcloud, but any WebDAV target
  Gadgetbridge can export to should work).
- A Docker Hub account, only if you want to build and publish your own
  images via the included GitHub Actions workflow — optional, see
  [step 1](#1-get-the-container-images).

## Setup steps

### 1. Get the container images

**Use prebuilt images** — point `.env` at wherever this repo's images
are published (see [step 3](#3-configure-the-stack)) and skip to step 2.

**Or build them yourself** — no Docker Hub account needed. Swap the
relevant `image:` line in `docker-compose.yml` for a `build:` block:

```yaml
wearable-events:
  build:
    context: ./wearable-events
```

Then run `docker compose up -d --build` in step 4. `parser-colmi` and
`parser-activefit` need an explicit Dockerfile path and a build context
of `./parser` (not `./parser/colmi`), since both share `parser/common/`:

```yaml
parser-colmi:
  build:
    context: ./parser
    dockerfile: ./parser/colmi/Dockerfile
```

(`parser-activefit` is best-effort against an Actie 3 Premium and
safe to run — see [`parser/activefit/README.md`](../parser/activefit/README.md).)

Maintaining your own fork? [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml)
builds and publishes all three images automatically on push — not
needed for normal use.

### 2. Set up Gadgetbridge

Pair your ring/watch, then **Settings → Data auto-export** and point it at
your WebDAV server (e.g. a Nextcloud folder like `GadgetBridge/`).
Gadgetbridge periodically writes its full database there — that's what
the parser container reads.

### 3. Configure the stack

```bash
git clone https://github.com/<your-username>/wearable-biomarker-stack
cd wearable-biomarker-stack
cp env.stack.example .env
```

> **Generate secrets with `openssl rand -hex N`, not a typed password.**
> Compose applies `${VAR}` interpolation to `.env`'s own values, so a
> raw `$` in a password is silently truncated at that character — hex
> output can't contain `$`, so this can't happen. Full explanation at
> the top of `env.stack.example`.

Edit `.env` and fill in, at minimum:

- `PARSER_COLMI_IMAGE` / `PARSER_ACTIVEFIT_IMAGE` / `WEARABLE_EVENTS_IMAGE` —
  wherever those images live (prebuilt or your own, per step 1)
- `INFLUXDB_TOKEN` and `INFLUXDB_INIT_ADMIN_TOKEN` — the **same**
  generated value for both (`openssl rand -hex 32`); this is a
  full-access credential, not something to type yourself
- `WEBDAV_URL`, `WEBDAV_USER`, `WEBDAV_PASS`, `WEBDAV_PATH` — match
  step 2 (use a dedicated WebDAV app password if your server supports
  one)
- `WEARABLE_EVENTS_ADMIN_USERNAME` / `WEARABLE_EVENTS_ADMIN_PASSWORD` —
  your first login for the wearable-events web UI
- For Fit track/GPS path support, you also need to set Gadgetbridge to export these specifically. Point them to a WebDAV directory, and then set the env variable `EXPORT_TRACKS_PATH` to that path.

Every variable in `env.stack.example` has an inline comment explaining
what it does and a safe default where one exists.

**`PUID`/`PGID`** (default `1000:1000`) set which host uid/gid the
InfluxDB, Grafana, ntfy, and wearable-events containers run as. These
are host bind mounts, not Docker-managed volumes, so a non-root
container can't fix ownership itself — **create the directories and
set ownership before first boot** if you set these:

```bash
sudo mkdir -p ${APPDATA_ROOT:-/DATA/AppData/biomarker-stack}/{influxdb/data,influxdb/config,grafana/data,ntfy/cache,ntfy/lib,wearable-events}
sudo chown -R $(id -u):$(id -g) ${APPDATA_ROOT:-/DATA/AppData/biomarker-stack}
```

(Skip this if the images' own default user is fine — leave `PUID`/`PGID`
unset and Docker creates these directories automatically.)

### 4. Start everything

```bash
docker compose up -d
```

Add `--build` if you're building one or both images locally (step 1).
This starts InfluxDB, Grafana, ntfy, the ring parser, and
wearable-events.

### 5. Set up InfluxDB

InfluxDB initializes itself on first boot from your `.env`'s
`INFLUXDB_ORG`/`INFLUXDB_BUCKET`/`INFLUXDB_INIT_USERNAME`/
`INFLUXDB_INIT_PASSWORD`/`INFLUXDB_TOKEN` — no manual wizard. Open
`http://<your-host>:8086` and log in to confirm the org and bucket
exist.

This only runs once, against an empty volume — changing those `.env`
values later won't retroactively apply. Either create the new
org/bucket/token by hand in this UI, or wipe the volume (losing
collected data) to re-init.

> Every host port in this guide (8086, 3000, 8090, 8081) is just the
> default — override with `INFLUXDB_HOST_PORT`/`GRAFANA_HOST_PORT`/
> `NTFY_HOST_PORT`/`WEARABLE_EVENTS_HOST_PORT` if any collide with
> something else on your host.

### 6. Set up Grafana

Open `http://<your-host>:3000` (default `admin`/`admin`, you'll be
asked to change it). Add InfluxDB as a data source — URL
`http://influxdb:8086`, your `.env` org/bucket/token — then build
dashboards on whatever matters to you. Heart rate, HRV, temperature,
and sleep stages are all written by the parser (full field list in the
[parser README](../parser/colmi/README.md)).

### 7. Set up ntfy (for alerting; optional)

ntfy ships with auth locked down (deny-all, signup disabled), so it
needs two accounts before alerting works:

```bash
# Your own account, to receive notifications
docker exec -it biomarker-ntfy ntfy user add youruser
docker exec -it biomarker-ntfy ntfy access youruser "$NTFY_TOPIC" read

# A dedicated account for Grafana to publish as (matches
# NTFY_GRAFANA_USER / NTFY_GRAFANA_PASSWORD in .env)
docker exec -it biomarker-ntfy ntfy user add grafana
docker exec -it biomarker-ntfy ntfy access grafana "$NTFY_TOPIC" write
```

Then subscribe to `$NTFY_TOPIC` (default `biomarker-alerts`) from the
ntfy app, pointed at `http://<your-host>:8090`.

#### Grafana alerting (provisioned)

The alerting pipeline is already wired up, provisioned from templates
under `grafana/provisioning-templates/` rather than configured by
hand: a datasource, an alert rule ("HRV dropped >20% below your
14-day baseline" — a worked example; the file comments show how to
copy the pattern for resting heart rate or temperature), a contact
point routing to ntfy, and a notification policy tying them together.

A small init container renders these with `envsubst` before Grafana
starts — necessary because Grafana's own alert-rule provisioning
doesn't support `$ENV_VAR` interpolation inside query/settings blocks.

The render step itself is verified (output substitutes correctly,
leaves ntfy's `{{.title}}/{{.message}}` syntax untouched), but the
resulting config hasn't been checked against a live Grafana. After
your stack is up: confirm the rule loaded (**Alerting → Alert rules**)
and test the contact point (**Alerting → Contact points → Test**)
before relying on it. Expect "No data" for about 2 weeks until enough
HRV history has synced — that's expected, not a bug.

### 8. Set up wearable-events (optional)

Open `http://<your-host>:8081` and log in with the account from step 3.
Add calendar feeds (ICS URLs), keyword rules to tag calendar events,
manual one-tap context tags (caffeine, alcohol, etc.), and a nightly
subjective sleep score. Add a second household member from the Manage
tab, using the same username as their `GADGETBRIDGE_USER` (see
[Multi-user notes](#multi-user-notes)) so their data lines up.

Developing or modifying this component? See
[`wearable-events/README.md`](../wearable-events/README.md).

## Running just one device
 
Both parser containers are in `docker-compose.yml` by default so
`docker compose up -d` works out of the box regardless of which device
you actually have - but if you only have a ring or only a watch,
there's no need to also start (or even pull the image for) the other
one.
 
Compose's `profiles` mechanism controls this via the `COMPOSE_PROFILES`
variable in `.env`:
 
```bash
COMPOSE_PROFILES=all       # default - both parsers
COMPOSE_PROFILES=colmi     # ring only
COMPOSE_PROFILES=activefit # watch only
```
 
Or override it for a single run without touching `.env` at all - an
explicit `--profile` flag takes full precedence over whatever
`COMPOSE_PROFILES` says:
 
```bash
docker compose --profile colmi up -d
```
 
Every other service (InfluxDB, Grafana, ntfy, wearable-events) has no
profile of its own, so it always starts regardless of which parser
profile is active - only the two parser containers are affected.
 
**Switching profiles after both are already running:** `docker compose
up -d` with a narrower profile won't stop a parser that's no longer in
the active set - it just leaves it running, untouched. `--remove-orphans`
is supposed to clean this up but has inconsistent behavior across
Compose versions when profiles are involved - the reliable way is to
stop and remove the specific container directly:
 
```bash
docker compose stop parser-activefit && docker compose rm -f parser-activefit
```


## Multi-user notes

Each person gets their own device and their own parser container
instance (`GADGETBRIDGE_USER` set differently per instance — see
[`env.stack.example`](../env.stack.example)). Use the *same* username
for a person's wearable-events account as their `GADGETBRIDGE_USER`,
so their calendar/sleep-score data and sensor data share one `user` tag
in InfluxDB. The Manage tab's "add household member" form offers a
picker of already-synced device identities rather than free text, to
avoid a typo breaking that correlation.
