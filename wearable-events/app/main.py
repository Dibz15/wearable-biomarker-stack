from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
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
    '''
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-store"
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
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")