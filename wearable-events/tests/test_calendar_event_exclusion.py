"""Striking off a calendar event ("didn't attend") from the Timeline.

Influx is never contacted - every Influx write/delete the code paths
under test would make is monkeypatched with a recorder, so the tests
assert on *what would have been written/deleted* instead.
"""

import json
import sqlite3
from datetime import datetime

import pytest

from app import db, ics_sync, reprocess
from app.queries import calendar_events as ce
from app.routes import calendars as calendars_routes

START_ISO = "2026-10-01T09:00:00+00:00"


@pytest.fixture(autouse=True)
def _no_manual_events(monkeypatch):
    """/timeline also reads manual taps from Influx - stub that out."""
    import app.routes.events as events_routes
    monkeypatch.setattr(events_routes, "find_manual_events_in_range", lambda *a, **k: [])


class Recorder:
    def __init__(self, effect=None):
        self.calls = []
        self.effect = effect

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.effect:
            self.effect(**kwargs)


class FakeInflux:
    """Just enough of the events measurement to answer "which tags does
    this event really have points for": points keyed the way the delete
    predicate addresses them (user, calendar, timestamp, tag)."""

    def __init__(self):
        self.points = {}

    def write(self, **kw):
        for tag in kw["tags"]:
            self.points[(kw["user"], kw["calendar"], kw["timestamp"], tag)] = kw["event_id"]

    def delete(self, **kw):
        self.points.pop((kw["user"], kw["calendar"], kw["timestamp"], kw["tag"]), None)

    def find(self, *, user, event_id, timestamp):
        return {(k[3], k[1]) for k, eid in self.points.items() if k[0] == user and eid == event_id}

    def tags_for(self, event_id, user="testadmin"):
        return {tag for tag, _ in self.find(user=user, event_id=event_id, timestamp=None)}


# The fake for the current test, so _seed_event can put the seeded
# event's points "in Influx" too (set/cleared by the fake_influx fixture).
_current_fake = None


@pytest.fixture
def fake_influx():
    global _current_fake
    _current_fake = FakeInflux()
    yield _current_fake
    _current_fake = None


@pytest.fixture
def influx(monkeypatch, fake_influx):
    # Routes and reprocess go through the shared helpers in
    # app.queries.calendar_events; sync writes directly.
    writes, deletes = Recorder(fake_influx.write), Recorder(fake_influx.delete)
    monkeypatch.setattr(ce, "write_event_points", writes)
    monkeypatch.setattr(ce, "delete_event_tag_point", deletes)
    monkeypatch.setattr(ics_sync, "write_event_points", writes)
    monkeypatch.setattr(calendars_routes, "find_calendar_event_points", fake_influx.find)
    return writes, deletes


def _admin_id():
    return db.get_user_by_username("testadmin")["id"]


def _seed_event(auth_client, *, tags=("work",), event_id="evt1", title="Standup", manually_tagged=False):
    calendar_id = auth_client.post(
        "/calendars",
        json={"name": "Work", "ics_url": "https://example.com/work.ics", "default_tag": "work"},
    ).json()["id"]
    db.upsert_cached_event(
        event_id=event_id, user_id=_admin_id(), calendar_id=calendar_id, title=title,
        description="", start_iso=START_ISO, duration_min=30, applied_tags=list(tags),
    )
    if manually_tagged:
        db.set_cached_event_tags(event_id, _admin_id(), list(tags), manually_tagged=True)
    if _current_fake is not None:
        _current_fake.write(user="testadmin", tags=list(tags), calendar="Work",
                            timestamp=datetime.fromisoformat(START_ISO), event_id=event_id)
    return calendar_id


def _timeline_entry(auth_client, event_id="evt1"):
    entries = auth_client.get("/timeline?start=2026-09-30&end=2026-10-02").json()
    return next(e for e in entries if e.get("event_id") == event_id)


def test_exclude_deletes_points_and_flags_event(auth_client, influx):
    writes, deletes = influx
    _seed_event(auth_client, tags=("work", "meeting"))

    resp = auth_client.post("/calendar_events/evt1/exclude")
    assert resp.status_code == 200

    assert sorted(c["tag"] for c in deletes.calls) == ["meeting", "work"]
    assert all(c["source"] == "calendar" and c["calendar"] == "Work" for c in deletes.calls)
    assert all(c["user"] == "testadmin" for c in deletes.calls)
    assert writes.calls == []
    assert db.get_cached_event("evt1", _admin_id())["excluded"] == 1

    entry = _timeline_entry(auth_client)
    assert entry["excluded"] is True
    assert entry["tags"] == ["work", "meeting"]  # still shown, just struck off


def test_exclude_and_restore_guards(auth_client, influx):
    _seed_event(auth_client)
    assert auth_client.post("/calendar_events/evt1/restore").status_code == 400
    assert auth_client.post("/calendar_events/evt1/exclude").status_code == 200
    # Excluding again is allowed - it's how a failed delete gets retried
    assert auth_client.post("/calendar_events/evt1/exclude").status_code == 200
    assert auth_client.post("/calendar_events/nope/exclude").status_code == 404
    assert auth_client.post("/calendar_events/nope/restore").status_code == 404


def test_cannot_exclude_another_users_event(auth_client, client, influx):
    _seed_event(auth_client)
    auth_client.post("/users", json={"username": "seconduser", "password": "password123"})
    client.post("/auth/login", json={"username": "seconduser", "password": "password123"})

    assert client.post("/calendar_events/evt1/exclude").status_code == 404
    assert db.get_cached_event("evt1", _admin_id())["excluded"] == 0


def test_restore_auto_tagged_event_reclassifies(auth_client, influx, monkeypatch):
    writes, _ = influx
    _seed_event(auth_client, tags=("stale-tag",))
    auth_client.post("/calendar_events/evt1/exclude")
    monkeypatch.setattr(calendars_routes, "classify_event", lambda *a, **k: ["work"])

    resp = auth_client.post("/calendar_events/evt1/restore")
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["work"]

    assert len(writes.calls) == 1
    assert writes.calls[0]["tags"] == ["work"]
    assert writes.calls[0]["event_id"] == "evt1"
    assert writes.calls[0]["duration_min"] == 30
    cached = db.get_cached_event("evt1", _admin_id())
    assert cached["excluded"] == 0
    assert json.loads(cached["applied_tags"]) == ["work"]


def test_restore_manually_tagged_event_keeps_its_tags(auth_client, influx, monkeypatch):
    writes, _ = influx
    _seed_event(auth_client, tags=("custom",), manually_tagged=True)
    auth_client.post("/calendar_events/evt1/exclude")
    monkeypatch.setattr(calendars_routes, "classify_event", lambda *a, **k: ["should-not-be-used"])

    auth_client.post("/calendar_events/evt1/restore")
    assert writes.calls[0]["tags"] == ["custom"]
    cached = db.get_cached_event("evt1", _admin_id())
    assert cached["manually_tagged"] == 1
    assert json.loads(cached["applied_tags"]) == ["custom"]


def test_excluded_flag_survives_resync_upsert(auth_client, influx):
    calendar_id = _seed_event(auth_client)
    auth_client.post("/calendar_events/evt1/exclude")
    db.upsert_cached_event(
        event_id="evt1", user_id=_admin_id(), calendar_id=calendar_id, title="Standup (renamed desc)",
        description="new", start_iso=START_ISO, duration_min=45, applied_tags=["work"],
    )
    cached = db.get_cached_event("evt1", _admin_id())
    assert cached["excluded"] == 1
    assert cached["duration_min"] == 45


def test_tag_edits_on_excluded_event_skip_influx(auth_client, influx):
    writes, deletes = influx
    _seed_event(auth_client, tags=("work",))
    auth_client.post("/calendar_events/evt1/exclude")
    deletes.calls.clear()

    resp = auth_client.patch("/calendar_events/evt1/tags", json={"tags": ["focus"]})
    assert resp.status_code == 200
    assert auth_client.post("/calendar_events/evt1/reset_tags").status_code == 200

    assert writes.calls == []
    assert deletes.calls == []
    assert db.get_cached_event("evt1", _admin_id())["excluded"] == 1


def test_sync_does_not_write_excluded_events(auth_client, influx, monkeypatch):
    writes, _ = influx
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:a\r\nSUMMARY:Standup\r\nDTSTART:20261001T090000Z\r\nDTEND:20261001T093000Z\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b\r\nSUMMARY:Gym\r\nDTSTART:20261001T180000Z\r\nDTEND:20261001T190000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode()

    class FakeResp:
        content = ics

        def raise_for_status(self):
            pass

    monkeypatch.setattr(ics_sync.httpx, "get", lambda *a, **k: FakeResp())

    calendar_id = auth_client.post(
        "/calendars",
        json={"name": "Work", "ics_url": "https://example.com/work.ics", "default_tag": "work"},
    ).json()["id"]
    calendar = db.get_calendar(calendar_id, _admin_id())

    ics_sync.sync_calendar(calendar, [], "testadmin")
    assert len(writes.calls) == 2
    standup_id = next(c["event_id"] for c in writes.calls if c["timestamp"].hour == 9)

    auth_client.post(f"/calendar_events/{standup_id}/exclude")
    writes.calls.clear()

    ics_sync.sync_calendar(calendar, [], "testadmin")
    written_ids = [c["event_id"] for c in writes.calls]
    assert len(written_ids) == 1 and standup_id not in written_ids
    assert db.get_cached_event(standup_id, _admin_id())["excluded"] == 1


def test_reprocess_skips_excluded_events(auth_client, influx, monkeypatch):
    writes, deletes = influx
    _seed_event(auth_client, tags=("stale-tag",))
    auth_client.post("/calendar_events/evt1/exclude")
    deletes.calls.clear()
    monkeypatch.setattr(reprocess, "classify_event", lambda *a, **k: ["work"])

    assert reprocess.compute_reclassification_diff(_admin_id(), [], [])["count"] == 0
    reprocess._run_reprocess(_admin_id(), "testadmin")

    assert writes.calls == []
    assert deletes.calls == []
    assert reprocess.get_status(_admin_id())["changed"] == 0


_OLD_CACHE_DDL = """
    CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE calendars (id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL, ics_url TEXT NOT NULL, default_tag TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, last_synced TEXT, last_error TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (user_id, ics_url));
    CREATE TABLE calendar_events_cache (
        event_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
        title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', start_iso TEXT NOT NULL,
        duration_min INTEGER, applied_tags TEXT NOT NULL DEFAULT '[]',
        manually_tagged INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE INDEX idx_cache_user_calendar ON calendar_events_cache(user_id, calendar_id);
    INSERT INTO users (id, username, password_hash) VALUES (1, 'alice', 'x'), (2, 'bob', 'y');
    INSERT INTO calendars (id, user_id, name, ics_url, default_tag) VALUES
        (1, 1, 'Family', 'https://e/a.ics', 'family'), (2, 2, 'Family', 'https://e/b.ics', 'family');
    INSERT INTO calendar_events_cache VALUES ('a', 1, 1, 'Standup', '', '2026-10-01T09:00:00+00:00', 30, '["work"]', 0, '2026-10-01 10:00:00');
    INSERT INTO calendar_events_cache VALUES ('b', 1, 1, 'Gym', 'legs', '2026-10-01T18:00:00+00:00', 60, '["exercise"]', 1, '2026-10-01 19:00:00');
"""

_CACHE_COLS = ("event_id, user_id, calendar_id, title, description, start_iso, duration_min, "
               "applied_tags, manually_tagged, updated_at")


def _cache_pk(conn):
    return sorted((r[5], r[1]) for r in conn.execute("PRAGMA table_info(calendar_events_cache)") if r[5])


@pytest.mark.parametrize("with_issue7_columns", [False, True])
def test_migration_rebuilds_cache_keyed_per_user_keeping_every_row(tmp_path, monkeypatch, with_issue7_columns):
    """Simulates a live install: either on the pre-#7 schema, or on the
    first #7 build (has excluded/stranded_tags, still keyed by event_id
    alone - what the running test instance has). init_db() must add the
    columns, rebuild the table keyed by (user_id, event_id), keep every
    row and value, and be a no-op the second time (it runs on every
    startup)."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_CACHE_DDL)
    if with_issue7_columns:
        conn.executescript("""
            ALTER TABLE calendar_events_cache ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE calendar_events_cache ADD COLUMN stranded_tags TEXT NOT NULL DEFAULT '[]';
            UPDATE calendar_events_cache SET excluded = 1, stranded_tags = '["meeting"]' WHERE event_id = 'b';
        """)
    conn.commit()
    before = [tuple(r) for r in conn.execute(f"SELECT {_CACHE_COLS} FROM calendar_events_cache ORDER BY event_id")]
    conn.close()

    monkeypatch.setattr(db, "SQLITE_PATH", str(path))
    db.init_db()
    db.init_db()

    conn = sqlite3.connect(path)
    assert _cache_pk(conn) == [(1, "user_id"), (2, "event_id")]
    after = [tuple(r) for r in conn.execute(f"SELECT {_CACHE_COLS} FROM calendar_events_cache ORDER BY event_id")]
    assert after == before  # every pre-existing value untouched
    flags = conn.execute("SELECT excluded, stranded_tags FROM calendar_events_cache ORDER BY event_id").fetchall()
    assert flags == ([(0, "[]"), (1, '["meeting"]')] if with_issue7_columns else [(0, "[]"), (0, "[]")])
    indexes = [r[1] for r in conn.execute("PRAGMA index_list(calendar_events_cache)")]
    assert "idx_cache_user_calendar" in indexes
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'calendar_events_cache_new'").fetchone() is None

    # The point of the new key: another user can now hold the same event_id
    conn.execute("INSERT INTO calendar_events_cache (event_id, user_id, calendar_id, title, start_iso) "
                 "VALUES ('a', 2, 2, 'Standup', '2026-10-01T09:00:00+00:00')")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("fail_after_rebuild", [False, True])
def test_migration_rolls_back_cleanly_on_failure(tmp_path, monkeypatch, fail_after_rebuild):
    """If the rebuild fails - at the start, or after every step including
    the DROP + RENAME has run - the transaction must leave the original
    table exactly as it was, and a later startup can retry."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_CACHE_DDL)
    conn.commit()
    before = [tuple(r) for r in conn.execute(f"SELECT {_CACHE_COLS} FROM calendar_events_cache ORDER BY event_id")]
    conn.close()
    monkeypatch.setattr(db, "SQLITE_PATH", str(path))

    real_rebuild = db._rebuild_cache_table

    def failing_rebuild(conn, ddl):
        if fail_after_rebuild:
            real_rebuild(conn, ddl)
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(db, "_rebuild_cache_table", failing_rebuild)
    with pytest.raises(RuntimeError, match="simulated failure"):
        db.init_db()

    conn = sqlite3.connect(path)
    assert _cache_pk(conn) == [(1, "event_id")]
    assert [tuple(r) for r in conn.execute(f"SELECT {_CACHE_COLS} FROM calendar_events_cache ORDER BY event_id")] == before
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'calendar_events_cache_new'").fetchone() is None
    assert "idx_cache_user_calendar" in [r[1] for r in conn.execute("PRAGMA index_list(calendar_events_cache)")]
    conn.close()

    monkeypatch.setattr(db, "_rebuild_cache_table", real_rebuild)
    db.init_db()  # retry succeeds
    conn = sqlite3.connect(path)
    assert _cache_pk(conn) == [(1, "user_id"), (2, "event_id")]
    assert [tuple(r) for r in conn.execute(f"SELECT {_CACHE_COLS} FROM calendar_events_cache ORDER BY event_id")] == before
    conn.close()


def test_exclude_sets_flag_before_deleting(auth_client, influx, monkeypatch):
    """Flag first, so a concurrent sync that checks it can't write the
    points back in between the delete and the flag being set."""
    _seed_event(auth_client)
    seen = []

    def deleter(**kwargs):
        seen.append(db.get_cached_event("evt1", _admin_id())["excluded"])

    monkeypatch.setattr(ce, "delete_event_tag_point", deleter)
    auth_client.post("/calendar_events/evt1/exclude")
    assert seen == [1]


def test_exclude_delete_failure_returns_502_and_can_be_retried(auth_client, influx, monkeypatch):
    _, deletes = influx
    _seed_event(auth_client, tags=("work", "meeting"))

    def failing(**kwargs):
        if kwargs["tag"] == "meeting":
            raise RuntimeError("influx down")
        deletes(**kwargs)

    monkeypatch.setattr(ce, "delete_event_tag_point", failing)
    resp = auth_client.post("/calendar_events/evt1/exclude")
    assert resp.status_code == 502
    assert "try again" in resp.json()["detail"]
    cached = db.get_cached_event("evt1", _admin_id())
    assert cached["excluded"] == 1
    assert json.loads(cached["stranded_tags"]) == ["meeting"]
    entry = _timeline_entry(auth_client)
    assert entry["excluded"] is True and entry["exclude_incomplete"] is True

    # Retry only needs to delete what's still stranded
    deletes.calls.clear()
    monkeypatch.setattr(ce, "delete_event_tag_point", deletes)
    assert auth_client.post("/calendar_events/evt1/exclude").status_code == 200
    assert [c["tag"] for c in deletes.calls] == ["meeting"]
    assert json.loads(db.get_cached_event("evt1", _admin_id())["stranded_tags"]) == []
    assert _timeline_entry(auth_client)["exclude_incomplete"] is False


def test_restore_write_failure_keeps_event_excluded(auth_client, influx, monkeypatch):
    _seed_event(auth_client)
    auth_client.post("/calendar_events/evt1/exclude")

    def failing(**kwargs):
        raise RuntimeError("influx down")

    monkeypatch.setattr(ce, "write_event_points", failing)
    resp = auth_client.post("/calendar_events/evt1/restore")
    assert resp.status_code == 502
    assert "still excluded" in resp.json()["detail"]
    cached = db.get_cached_event("evt1", _admin_id())
    assert cached["excluded"] == 1
    # The write may have landed part-way, so those tags are tracked as
    # possibly-live and the Timeline flags the event as needing a retry.
    assert json.loads(cached["stranded_tags"]) == ["work"]
    assert _timeline_entry(auth_client)["exclude_incomplete"] is True


def test_reprocess_rechecks_rows_excluded_after_its_snapshot(auth_client, influx, monkeypatch):
    """_run_reprocess lists every event up front; one struck off after
    that snapshot was taken must still not be written back."""
    writes, _ = influx
    _seed_event(auth_client, tags=("stale-tag",))
    stale_snapshot = db.list_cached_events(_admin_id())  # excluded=0 here
    auth_client.post("/calendar_events/evt1/exclude")
    monkeypatch.setattr(reprocess.db, "list_cached_events", lambda user_id: stale_snapshot)
    monkeypatch.setattr(reprocess, "classify_event", lambda *a, **k: ["work"])

    reprocess._run_reprocess(_admin_id(), "testadmin")
    assert writes.calls == []
    assert reprocess.get_status(_admin_id())["processed"] == 1


def test_delete_event_tag_point_is_scoped_to_user(monkeypatch):
    from datetime import datetime, timezone

    predicates = []

    class FakeDeleteApi:
        def delete(self, start, stop, predicate, **kwargs):
            predicates.append(predicate)

    class FakeClient:
        def delete_api(self):
            return FakeDeleteApi()

    monkeypatch.setattr(ce, "get_client", lambda: FakeClient())
    ce.delete_event_tag_point(user="alice", tag="work", source="calendar",
                              timestamp=datetime(2026, 10, 1, 9, tzinfo=timezone.utc), calendar="Family")
    assert '"user"="alice"' in predicates[0]
    assert '"calendar"="Family"' in predicates[0]


def test_editing_tags_after_partial_exclude_keeps_stranded_points(auth_client, influx, fake_influx, monkeypatch):
    """Tag edits on an excluded event are cache-only - they must not
    make a retry forget the point that failed to delete."""
    _, deletes = influx
    _seed_event(auth_client, tags=("work", "meeting"))

    def failing(**kwargs):
        if kwargs["tag"] == "meeting":
            raise RuntimeError("influx down")
        fake_influx.delete(**kwargs)

    monkeypatch.setattr(ce, "delete_event_tag_point", failing)
    assert auth_client.post("/calendar_events/evt1/exclude").status_code == 502

    monkeypatch.setattr(ce, "delete_event_tag_point", deletes)
    assert auth_client.patch("/calendar_events/evt1/tags", json={"tags": ["focus"]}).status_code == 200
    assert deletes.calls == []

    assert auth_client.post("/calendar_events/evt1/exclude").status_code == 200
    assert [c["tag"] for c in deletes.calls] == ["meeting"]


def test_restore_clears_stranded_points_not_in_restored_tags(auth_client, influx, fake_influx, monkeypatch):
    writes, deletes = influx
    _seed_event(auth_client, tags=("work", "meeting"), manually_tagged=True)

    def failing(**kwargs):
        if kwargs["tag"] == "meeting":
            raise RuntimeError("influx down")
        fake_influx.delete(**kwargs)

    monkeypatch.setattr(ce, "delete_event_tag_point", failing)
    auth_client.post("/calendar_events/evt1/exclude")
    monkeypatch.setattr(ce, "delete_event_tag_point", deletes)
    auth_client.patch("/calendar_events/evt1/tags", json={"tags": ["focus"]})

    resp = auth_client.post("/calendar_events/evt1/restore")
    assert resp.status_code == 200
    assert [c["tag"] for c in deletes.calls] == ["meeting"]
    assert writes.calls[-1]["tags"] == ["focus"]
    cached = db.get_cached_event("evt1", _admin_id())
    assert cached["excluded"] == 0 and json.loads(cached["stranded_tags"]) == []


@pytest.mark.parametrize("method,path,body", [
    ("post", "/calendar_events/evt1/exclude", None),
    ("post", "/calendar_events/evt1/restore", None),
    ("patch", "/calendar_events/evt1/tags", {"tags": ["focus"]}),
    ("post", "/calendar_events/evt1/reset_tags", None),
])
def test_per_event_endpoints_read_the_row_under_the_lock(auth_client, influx, monkeypatch, method, path, body):
    """The row must be read while calendar_points_lock is held, so a
    sync/reprocess can't change it between the read and the Influx
    writes. Checked by trying to take the lock from another thread at
    the moment the row is read."""
    import threading
    from app.queries.calendar_events import calendar_points_lock

    _seed_event(auth_client, manually_tagged=True)
    if path.endswith("restore"):
        auth_client.post("/calendar_events/evt1/exclude")

    real_get = db.get_cached_event
    lock_free_at_read = []

    def spying_get(event_id, user_id):
        def probe():
            got = calendar_points_lock.acquire(blocking=False)
            if got:
                calendar_points_lock.release()
            lock_free_at_read.append(got)

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        return real_get(event_id, user_id)

    monkeypatch.setattr(calendars_routes.db, "get_cached_event", spying_get)
    resp = getattr(auth_client, method)(path, **({"json": body} if body else {}))
    assert resp.status_code == 200, resp.text
    assert lock_free_at_read and not any(lock_free_at_read)


def test_reprocess_only_rereads_rows_it_will_rewrite(auth_client, influx, monkeypatch):
    """Unchanged events are checked against the start-of-run snapshot
    alone - only ones whose tags actually change get re-read under the
    lock and rewritten."""
    writes, _ = influx
    calendar_id = _seed_event(auth_client, tags=("work",), event_id="same")
    db.upsert_cached_event(
        event_id="changes", user_id=_admin_id(), calendar_id=calendar_id, title="Gym",
        description="", start_iso="2026-10-01T18:00:00+00:00", duration_min=60, applied_tags=["stale"],
    )
    monkeypatch.setattr(reprocess, "classify_event", lambda *a, **k: ["work"])

    reread = []
    real_get = db.get_cached_event
    monkeypatch.setattr(reprocess.db, "get_cached_event", lambda eid, uid: reread.append(eid) or real_get(eid, uid))

    reprocess._run_reprocess(_admin_id(), "testadmin")
    assert reread == ["changes"]
    assert [c["event_id"] for c in writes.calls] == ["changes"]
    status = reprocess.get_status(_admin_id())
    assert status["processed"] == 2 and status["changed"] == 1


def _second_user(auth_client):
    auth_client.post("/users", json={"username": "seconduser", "password": "password123"})
    uid = db.get_user_by_username("seconduser")["id"]
    calendar_id = db.add_calendar(uid, "Work", "https://example.com/shared.ics", "work")
    return uid, calendar_id


def test_excluded_flag_is_per_user_for_a_shared_calendar(auth_client, influx):
    """Two accounts with a same-named calendar get the same event_id for
    the same event - one person's strike-off must not touch the other's."""
    _seed_event(auth_client)
    b_id, b_cal = _second_user(auth_client)
    auth_client.post("/calendar_events/evt1/exclude")

    # The second user's sync of the same event (same event_id)
    db.upsert_cached_event(
        event_id="evt1", user_id=b_id, calendar_id=b_cal, title="Standup",
        description="", start_iso=START_ISO, duration_min=30, applied_tags=["work"],
    )

    mine = db.get_cached_event("evt1", _admin_id())
    theirs = db.get_cached_event("evt1", b_id)
    assert mine["excluded"] == 1 and mine["user_id"] == _admin_id()
    assert theirs["excluded"] == 0 and theirs["user_id"] == b_id
    assert _timeline_entry(auth_client)["excluded"] is True


def test_shared_calendar_sync_still_writes_other_users_points(auth_client, influx, monkeypatch):
    writes, _ = influx
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:a\r\nSUMMARY:Standup\r\nDTSTART:20261001T090000Z\r\nDTEND:20261001T093000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode()

    class FakeResp:
        content = ics

        def raise_for_status(self):
            pass

    monkeypatch.setattr(ics_sync.httpx, "get", lambda *a, **k: FakeResp())
    my_cal = db.get_calendar(_seed_event(auth_client), _admin_id())
    b_id, b_cal_id = _second_user(auth_client)

    ics_sync.sync_calendar(my_cal, [], "testadmin")
    event_id = writes.calls[0]["event_id"]
    auth_client.post(f"/calendar_events/{event_id}/exclude")
    writes.calls.clear()

    ics_sync.sync_calendar(db.get_calendar(b_cal_id, b_id), [], "seconduser")
    assert [(c["user"], c["event_id"]) for c in writes.calls] == [("seconduser", event_id)]
    assert db.get_cached_event(event_id, _admin_id())["excluded"] == 1


def test_exclude_deletes_points_the_cache_doesnt_know_about(auth_client, influx, fake_influx):
    """A sync that retagged the event (rule change, no reprocess) leaves
    the old tag's point behind while the cache only lists the new tag.
    Exclude must find and delete it via Influx itself."""
    _seed_event(auth_client, tags=("meeting",))
    start = datetime.fromisoformat(START_ISO)
    for tag in ("work", "meeting"):  # what's really in Influx
        fake_influx.write(user="testadmin", tags=[tag], calendar="Work", timestamp=start, event_id="evt1")

    assert auth_client.post("/calendar_events/evt1/exclude").status_code == 200
    assert fake_influx.tags_for("evt1") == set()
    assert _timeline_entry(auth_client)["exclude_incomplete"] is False


def test_exclude_lookup_failure_keeps_event_incomplete_until_retry(auth_client, influx, fake_influx, monkeypatch):
    _seed_event(auth_client, tags=("work",))
    start = datetime.fromisoformat(START_ISO)
    fake_influx.write(user="testadmin", tags=["work", "old"], calendar="Work", timestamp=start, event_id="evt1")

    def failing_find(**kwargs):
        raise RuntimeError("influx down")

    monkeypatch.setattr(calendars_routes, "find_calendar_event_points", failing_find)
    resp = auth_client.post("/calendar_events/evt1/exclude")
    assert resp.status_code == 502 and "InfluxDB" in resp.json()["detail"]
    assert _timeline_entry(auth_client)["exclude_incomplete"] is True

    monkeypatch.setattr(calendars_routes, "find_calendar_event_points", fake_influx.find)
    assert auth_client.post("/calendar_events/evt1/exclude").status_code == 200
    assert fake_influx.tags_for("evt1") == set()
    assert _timeline_entry(auth_client)["exclude_incomplete"] is False


def test_delete_predicate_escapes_quotes_backslashes_and_newlines(monkeypatch):
    from datetime import timezone as tz

    predicates = []

    class FakeDeleteApi:
        def delete(self, start, stop, predicate, **kwargs):
            predicates.append(predicate)

    class FakeClient:
        def delete_api(self):
            return FakeDeleteApi()

    monkeypatch.setattr(ce, "get_client", lambda: FakeClient())
    ce.delete_event_tag_point(user="al\\ice", tag='a"b', source="calendar",
                              timestamp=datetime(2026, 10, 1, 9, tzinfo=tz.utc), calendar="Work\nmain")
    assert '"user"="al\\\\ice"' in predicates[0]
    assert '"tag"="a\\"b"' in predicates[0]
    assert '"calendar"="Work\\nmain"' in predicates[0]


def test_find_calendar_event_points_escapes_and_reads_tag_calendar_pairs(monkeypatch):
    from datetime import timezone as tz

    queries = []

    class Record:
        def __init__(self, tag, calendar):
            self.values = {"tag": tag, "calendar": calendar}

    class Table:
        records = [Record("work", "Work"), Record("meeting", "Work"), Record("work", "Work"), Record("work", "Old name")]

    class FakeQueryApi:
        def query(self, flux):
            queries.append(flux)
            return [Table()]

    class FakeClient:
        def query_api(self):
            return FakeQueryApi()

    monkeypatch.setattr(ce, "get_client", lambda: FakeClient())
    points = ce.find_calendar_event_points(user='bob"${x}', event_id="evt1",
                                           timestamp=datetime(2026, 10, 1, 9, tzinfo=tz.utc))
    assert points == {("work", "Work"), ("meeting", "Work"), ("work", "Old name")}
    assert 'keep(columns: ["tag", "calendar"])' in queries[0]
    assert 'r.user == "bob\\"\\${x}"' in queries[0]
    assert 'r._value == "evt1"' in queries[0]
    assert "range(start: 2026-10-01T09:00:00.000000Z, stop: 2026-10-01T09:00:01.000000Z)" in queries[0]


def test_exclude_after_calendar_rename_deletes_points_under_old_name(auth_client, influx, fake_influx):
    """Renaming a calendar leaves its existing points under the old
    `calendar` tag value - a delete using the new name matches nothing,
    so exclude must delete each point with its own value."""
    calendar_id = _seed_event(auth_client, tags=("work",))
    assert auth_client.patch(f"/calendars/{calendar_id}", json={"name": "Job"}).status_code == 200

    assert auth_client.post("/calendar_events/evt1/exclude").status_code == 200
    assert fake_influx.tags_for("evt1") == set()
    assert ("work", "Work") in {(c["tag"], c["calendar"]) for c in influx[1].calls}


def test_exclude_rechecks_influx_and_strands_points_a_delete_missed(auth_client, influx, fake_influx, monkeypatch):
    """Influx reports success for a delete that matched nothing - only a
    re-check after deleting can tell the strike-off didn't take."""
    _seed_event(auth_client, tags=("work",))
    monkeypatch.setattr(ce, "delete_event_tag_point", lambda **kw: None)  # "succeeds", removes nothing

    resp = auth_client.post("/calendar_events/evt1/exclude")
    assert resp.status_code == 502 and "InfluxDB" in resp.json()["detail"]
    assert json.loads(db.get_cached_event("evt1", _admin_id())["stranded_tags"]) == ["work"]
    assert _timeline_entry(auth_client)["exclude_incomplete"] is True


def test_exclude_records_found_points_as_stranded_before_deleting(auth_client, influx, fake_influx, monkeypatch):
    """A crash mid-delete must not leave points the lookup found looking
    like a completed strike-off."""
    _seed_event(auth_client, tags=())  # cache knows no tags...
    fake_influx.write(user="testadmin", tags=["old"], calendar="Work",
                      timestamp=datetime.fromisoformat(START_ISO), event_id="evt1")  # ...but Influx has one
    stranded_at_delete = []

    def deleter(**kw):
        stranded_at_delete.append(json.loads(db.get_cached_event("evt1", _admin_id())["stranded_tags"]))
        fake_influx.delete(**kw)

    monkeypatch.setattr(ce, "delete_event_tag_point", deleter)
    assert auth_client.post("/calendar_events/evt1/exclude").status_code == 200
    assert stranded_at_delete == [["old"]]
    assert fake_influx.tags_for("evt1") == set()


def test_exclude_recheck_failure_keeps_event_incomplete(auth_client, influx, fake_influx, monkeypatch):
    _seed_event(auth_client, tags=("work",))
    calls = []

    def find_then_fail(**kw):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("influx down")
        return fake_influx.find(**kw)

    monkeypatch.setattr(calendars_routes, "find_calendar_event_points", find_then_fail)
    resp = auth_client.post("/calendar_events/evt1/exclude")
    assert resp.status_code == 502 and "couldn't check InfluxDB" in resp.json()["detail"]
    assert json.loads(db.get_cached_event("evt1", _admin_id())["stranded_tags"]) == ["work"]


def test_restore_after_rename_replaces_old_name_points(auth_client, influx, fake_influx, monkeypatch):
    """A leftover point under the calendar's old name would be counted
    twice next to the restored one - restore removes it first."""
    calendar_id = _seed_event(auth_client, tags=("work",), manually_tagged=True)

    def keep_points(**kw):
        raise RuntimeError("influx down")

    monkeypatch.setattr(ce, "delete_event_tag_point", keep_points)
    assert auth_client.post("/calendar_events/evt1/exclude").status_code == 502  # leaves (work, Work)
    monkeypatch.setattr(ce, "delete_event_tag_point", influx[1])
    auth_client.patch(f"/calendars/{calendar_id}", json={"name": "Job"})

    assert auth_client.post("/calendar_events/evt1/restore").status_code == 200
    assert fake_influx.find(user="testadmin", event_id="evt1", timestamp=None) == {("work", "Job")}


def test_restore_lookup_failure_keeps_event_excluded(auth_client, influx, fake_influx, monkeypatch):
    _seed_event(auth_client, tags=("work",))
    auth_client.post("/calendar_events/evt1/exclude")
    writes = influx[0]
    writes.calls.clear()

    def failing_find(**kw):
        raise RuntimeError("influx down")

    monkeypatch.setattr(calendars_routes, "find_calendar_event_points", failing_find)
    resp = auth_client.post("/calendar_events/evt1/restore")
    assert resp.status_code == 502 and "still excluded" in resp.json()["detail"]
    assert writes.calls == []
    assert db.get_cached_event("evt1", _admin_id())["excluded"] == 1
