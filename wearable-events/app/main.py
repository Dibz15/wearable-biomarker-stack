import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app import auth, db
from app.config import ADMIN_PASSWORD, ADMIN_USERNAME, PORT, SYNC_INTERVAL_MINUTES
from app.ics_sync import sync_all_calendars
from app.routes import activity, admin, auth as auth_routes, calendars, events, sleep, today, vitals

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    auth.bootstrap_admin_if_configured(ADMIN_USERNAME, ADMIN_PASSWORD)
    scheduler.add_job(
        sync_all_calendars,
        "interval",
        minutes=SYNC_INTERVAL_MINUTES,
        id="calendar_sync",
        next_run_time=datetime.now(),  # run once immediately on startup
    )
    scheduler.start()
    logger.info(f"Calendar sync scheduled every {SYNC_INTERVAL_MINUTES} minutes")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Wearable Events", lifespan=lifespan)


# Compresses both the JSON API responses and the static JS/CSS bundle.
# The W/M endpoints return one entry per day and are small either way,
# but the Day views' raw per-point series and the ~357 KB frontend
# bundle both compress by roughly an order of magnitude - and gzip is
# where almost all of that saving comes from on a mobile connection.
# minimum_size skips the many tiny responses (auth checks, single
# baseline objects) where the compression header overhead would
# outweigh the saving.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def server_timing(request: Request, call_next):
    ''' Reports how long the server itself spent on each request, via
    the standard Server-Timing header - surfaced directly in Chrome
    and Firefox devtools' network Timing panel. This is the one thing
    that makes "is this slow because of the server or the connection?"
    answerable from a phone over remote debugging, rather than
    guessed at. Cheap enough (one perf_counter pair) to leave on
    permanently.
    '''
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
    return response


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    ''' Forces the browser to always revalidate static assets (HTML/JS/
    CSS) rather than heuristically caching them. FastAPI's StaticFiles
    mount sends Last-Modified/ETag but no explicit Cache-Control, and
    browsers can decide to skip revalidation entirely for a while -
    meaning a shipped frontend fix can silently not take effect in an
    already-open browser tab, with no visible sign anything is wrong.
    This app is small and self-hosted, so trading away browser caching
    for "you always get what's actually on disk" is the right default.

    "no-cache", not "no-store" - a deliberate distinction, not a
    synonym. Both guarantee the browser never serves a stale asset
    without asking the server first, which is the whole point above.
    But no-store forbids keeping a copy at all, so every revalidation
    re-downloads the full file; no-cache lets the browser keep the
    copy and revalidate it, and StaticFiles already sends the ETag and
    Last-Modified that turn that into a ~200-byte 304. The frontend is
    ~357 KB across 18 ES modules plus the stylesheet, all of it
    re-fetched on every load under no-store - the single largest
    transfer in the app, and painful on mobile data. This keeps the
    freshness guarantee and drops the bytes.
    '''
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# --- routers - one per feature domain, see each module's own docstring
# in app/routes/ for exactly which endpoints it owns. auth is aliased
# (auth_routes) since the bare name `auth` is already the app.auth
# module used directly above (bootstrap_admin_if_configured). ---
app.include_router(auth_routes.router)
app.include_router(events.router)
app.include_router(today.router)
app.include_router(vitals.router)
app.include_router(activity.router)
app.include_router(sleep.router)
app.include_router(calendars.router)
app.include_router(admin.router)


# --- static UI ---
_static_dir = Path(__file__).parent.parent / "static"


# Client-side routing fallback. Every real frontend "page" the SPA
# router understands lives under /app/* (see static/js/router.js's own
# route table) - a deliberately distinct namespace from the API's own
# paths, since a bare path like /sleep or /activity would otherwise
# collide with a real API endpoint of the same name (GET /sleep,
# GET /activity/sessions, etc.) and a full-page browser navigation
# there would hit the API and get raw JSON instead of the app shell.
# Without this, refreshing (or opening a bookmark/shared link) on any
# in-app URL other than "/" would 404, since no file actually exists
# at e.g. /app/activity/workout/12345 on disk - this always returns
# the same index.html regardless of the specific /app/* path, and the
# router's own client-side code (already loaded once index.html runs)
# takes it from there. Registered before the StaticFiles mount below,
# though order wouldn't actually matter here in practice - nothing
# under static/ is ever placed at an /app/* path.
@app.get("/app/{full_path:path}")
async def spa_fallback(full_path: str):
    return FileResponse(_static_dir / "index.html")


app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")