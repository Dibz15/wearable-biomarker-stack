"""Shared pytest fixtures.

Environment variables are set BEFORE any `app.*` import below, since
app/config.py reads them at import time (module-level `os.getenv(...)`
calls) - setting them any later would silently have no effect.

No live InfluxDB is used anywhere in this suite. Tests either exercise
pure functions that don't touch Influx at all (keyword-rule
classification, the sleep entry_id hash), or SQLite-backed endpoints
(auth, calendars, keyword rules, tag definitions) that never need it.
INFLUX_URL is set to something obviously fake specifically so a test
that accidentally exercises a real Influx code path fails fast and
loud (connection refused) instead of hanging or silently reaching a
real server.
"""

import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="wearable-events-tests-")
os.environ.setdefault("SQLITE_PATH", os.path.join(_tmpdir, "test.db"))
os.environ.setdefault("ADMIN_USERNAME", "testadmin")
os.environ.setdefault("ADMIN_PASSWORD", "testpassword123")
os.environ.setdefault("TZ", "UTC")
os.environ.setdefault("INFLUX_URL", "http://do-not-contact.invalid:9999")
os.environ.setdefault("INFLUX_TOKEN", "unused-in-tests")

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.config import ADMIN_PASSWORD, ADMIN_USERNAME
from app.main import app

_TABLES = ["sessions", "users", "calendars", "keyword_rules", "tag_definitions", "calendar_events_cache"]


@pytest.fixture(scope="session")
def _test_client():
    """Session-scoped rather than one-per-test: app/main.py's
    BackgroundScheduler is a module-level singleton, and apscheduler
    doesn't support starting it again after it's been shut down - a
    fresh TestClient(app) per test would start/stop it once per test
    and crash on the second one. One client for the whole run avoids
    that; clean_db() below (function-scoped, autouse) still gives each
    individual test a clean database and a fresh admin bootstrap.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db(_test_client):
    """Wipes every table before each test, then re-bootstraps the
    admin account.

    A fresh SQLite file per test isn't practical here - SQLITE_PATH is
    read once at import time (see the module docstring above), so it's
    fixed for the whole test session's process. Wiping tables instead
    keeps tests isolated from each other's leftover rows while still
    sharing one file. Depends on _test_client (not used directly) so
    the scheduler-owning app has already started before this runs.

    Bootstrap normally only happens once, in the app's own startup
    lifespan - since _test_client is session-scoped, that already
    happened before the FIRST test. Calling it again here (safe: it's
    a no-op once any user exists, see bootstrap_admin_if_configured's
    own early return) is what actually recreates the admin account
    after every wipe.
    """
    db.init_db()
    with db.get_conn() as conn:
        for table in _TABLES:
            conn.execute(f"DELETE FROM {table}")
    auth.bootstrap_admin_if_configured(ADMIN_USERNAME, ADMIN_PASSWORD)
    yield


@pytest.fixture
def client(_test_client):
    """A TestClient with no session cookie - the "logged out" case."""
    _test_client.cookies.clear()
    yield _test_client


@pytest.fixture
def auth_client(client):
    """A TestClient already logged in as the bootstrapped admin
    account (created fresh by clean_db() above)."""
    resp = client.post(
        "/auth/login",
        json={"username": "testadmin", "password": "testpassword123"},
    )
    assert resp.status_code == 200, resp.text
    return client
