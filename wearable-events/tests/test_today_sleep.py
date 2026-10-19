"""The Today tab's sleep card picks the session that woke up on the
viewed day, the same rule as the Sleep tab (#8).

Influx is never contacted: find_sleep_sessions_in_range is replaced
with synthetic sessions, so the real wake-date and longest-session
logic in app.queries.sleep_sessions still runs.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.queries import sleep_sessions
from app.routes import today as today_routes

DAY = "2026-10-10"


def _session(device, start, hours):
    start = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    duration_s = int(hours * 3600)
    return {
        "device": device,
        "start_time": start,
        "end_time": start + timedelta(seconds=duration_s),
        "duration_s": duration_s,
    }


@pytest.fixture
def sessions(monkeypatch):
    """Sessions "in Influx". Returns the list to append to, plus the
    calls get_sleep_stage_breakdown received."""
    stored = []
    stage_calls = []

    def fake_find(user, start, end):
        return [s for s in stored if start <= s["start_time"] < end]

    def fake_stages(user, start, end, device=None):
        stage_calls.append((start, end, device))
        return {"deep": 60, "light": 200, "rem": 90, "awake": 10}

    monkeypatch.setattr(sleep_sessions, "find_sleep_sessions_in_range", fake_find)
    monkeypatch.setattr(today_routes, "get_sleep_stage_breakdown", fake_stages)
    monkeypatch.setattr(today_routes, "get_today_vitals", lambda *a, **k: {})
    monkeypatch.setattr(today_routes, "get_today_steps", lambda *a, **k: {})
    return stored, stage_calls


def _sleep(auth_client, date=DAY):
    resp = auth_client.get(f"/today?date={date}")
    assert resp.status_code == 200, resp.text
    return resp.json()["sleep"]


def test_previous_night_is_not_shown_as_last_night(auth_client, sessions):
    stored, stage_calls = sessions
    # Woke up on the 9th; nothing has synced for the night ending on the 10th.
    stored.append(_session("ring", "2026-10-08T23:00:00", 8))

    assert _sleep(auth_client) is None
    assert stage_calls == []


def test_last_night_is_the_session_that_woke_up_today(auth_client, sessions):
    stored, stage_calls = sessions
    stored.append(_session("ring", "2026-10-08T23:00:00", 8))
    last_night = _session("ring", "2026-10-09T23:30:00", 7.5)
    stored.append(last_night)

    sleep = _sleep(auth_client)

    assert sleep["duration_s"] == last_night["duration_s"]
    # Labelled by wake date, like the Sleep tab, not by bedtime.
    assert sleep["wake_date"] == DAY
    assert sleep["start_time"] == last_night["start_time"].isoformat()
    assert sleep["stages_min"]["deep"] == 60
    assert stage_calls == [(last_night["start_time"], last_night["end_time"], "ring")]


def test_session_starting_after_midnight_still_counts(auth_client, sessions):
    stored, _ = sessions
    late = _session("ring", "2026-10-10T01:15:00", 6)
    stored.append(late)

    sleep = _sleep(auth_client)

    assert sleep["duration_s"] == late["duration_s"]
    assert sleep["wake_date"] == DAY


def test_main_sleep_wins_over_a_nap_ending_the_same_day(auth_client, sessions):
    stored, _ = sessions
    main = _session("ring", "2026-10-09T23:00:00", 7)
    stored.append(main)
    # MIN_SLEEP_SESSION_SECONDS filtering happens in the real Flux query,
    # so a short "nap" here only has to be shorter than the main sleep.
    stored.append(_session("ring", "2026-10-10T14:00:00", 3.5))

    assert _sleep(auth_client)["duration_s"] == main["duration_s"]


def test_longest_device_wins(auth_client, sessions):
    stored, stage_calls = sessions
    stored.append(_session("watch", "2026-10-09T23:10:00", 6))
    ring = _session("ring", "2026-10-09T23:00:00", 7)
    stored.append(ring)

    assert _sleep(auth_client)["duration_s"] == ring["duration_s"]
    assert stage_calls[-1][2] == "ring"


def test_past_date_shows_that_nights_sleep_not_a_newer_one(auth_client, sessions):
    stored, _ = sessions
    older = _session("ring", "2026-10-06T22:00:00", 8)
    stored.append(older)
    stored.append(_session("ring", "2026-10-09T23:00:00", 7))

    assert _sleep(auth_client, "2026-10-07")["duration_s"] == older["duration_s"]
    assert _sleep(auth_client, "2026-10-08") is None


def test_influx_failure_leaves_the_card_empty_not_the_page(auth_client, monkeypatch):
    """If the sleep-session query fails, /today still answers with the
    rest of the dashboard and an empty sleep card. The real
    find_sleep_sessions_in_range runs here; only its Influx client is
    broken."""

    class BrokenQueryApi:
        def query(self, flux):
            raise ConnectionError("influx is down")

    class BrokenClient:
        def query_api(self):
            return BrokenQueryApi()

    monkeypatch.setattr(sleep_sessions, "get_client", lambda: BrokenClient())
    monkeypatch.setattr(today_routes, "get_today_vitals", lambda *a, **k: {"heart_rate": {}})
    monkeypatch.setattr(today_routes, "get_today_steps", lambda *a, **k: {"ring": 1234})

    resp = auth_client.get(f"/today?date={DAY}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sleep"] is None
    assert body["steps"] == {"ring": 1234}
