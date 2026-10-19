import json
import threading
from datetime import datetime, timezone

from loguru import logger

from app import db
from app.ics_sync import classify_event
from app.queries.calendar_events import calendar_points_lock, replace_calendar_event_points

_lock = threading.Lock()

# Per-user state, since each household member can independently trigger
# and monitor their own reprocess run. Keyed by user_id.
_states: dict[int, dict] = {}


def _default_state() -> dict:
    return {
        "status": "idle",  # idle | running | done | error
        "total": 0,
        "processed": 0,
        "changed": 0,
        "started_at": None,
        "finished_at": None,
        "error": None,
    }


def get_status(user_id: int) -> dict:
    with _lock:
        return dict(_states.get(user_id, _default_state()))


def compute_reclassification_diff(user_id: int, old_rules: list[dict], new_rules: list[dict]) -> dict:
    ''' Dry run only - writes nothing. For every cached (previously
    synced) calendar event belonging to user_id, compares classification
    under old_rules vs new_rules. This is what powers the "N events
    would change" figure shown before a reprocess is actually kicked
    off, so the person is confirming a real number, not a guess.

    Manually-tagged and struck-off ("didn't attend") events are left out
    - reprocessing skips them (see _run_reprocess), so including them in
    this count would overstate how many events will actually change.
    '''
    cached = db.list_cached_events(user_id)
    affected_titles = []
    for ev in cached:
        if ev["manually_tagged"] or ev["excluded"]:
            continue
        calendar = db.get_calendar(ev["calendar_id"])
        if calendar is None:
            # Calendar was deleted since this event was cached - nothing
            # sensible to reclassify against, skip it.
            continue
        old_tags = set(classify_event(ev["title"], ev["description"], old_rules, calendar["default_tag"]))
        new_tags = set(classify_event(ev["title"], ev["description"], new_rules, calendar["default_tag"]))
        if old_tags != new_tags:
            affected_titles.append(ev["title"] or "(untitled event)")

    return {"count": len(affected_titles), "sample_titles": affected_titles[:5]}


def _reprocess_one(snapshot: dict, user_id: int, username: str, rules: list[dict]) -> bool:
    ''' Reclassify one cached event; returns True if its tags changed.

    `snapshot` comes from the list taken at the start of the run, so the
    cheap "would the tags change?" check runs on it WITHOUT the lock -
    most events don't change. Only for one that does is the row re-read
    under calendar_points_lock and re-checked (the person may have struck
    it off or retagged it since the snapshot) right before writing.
    '''
    if snapshot["manually_tagged"] or snapshot["excluded"]:
        # Respect the manual override / strike-off - see the
        # manually_tagged and excluded comments in schema.sql.
        # (Restoring an excluded event reclassifies it then.)
        return False
    calendar = db.get_calendar(snapshot["calendar_id"])
    if calendar is None:
        return False
    new_tags = classify_event(snapshot["title"], snapshot["description"], rules, calendar["default_tag"])
    if set(new_tags) == set(json.loads(snapshot["applied_tags"] or "[]")):
        return False

    with calendar_points_lock:
        ev = db.get_cached_event(snapshot["event_id"], user_id)
        if ev is None or ev["manually_tagged"] or ev["excluded"]:
            return False
        new_tags = classify_event(ev["title"], ev["description"], rules, calendar["default_tag"])
        old_tags = json.loads(ev["applied_tags"] or "[]")
        if set(new_tags) == set(old_tags):
            return False
        replace_calendar_event_points(
            user=username,
            event_id=ev["event_id"],
            old_tags=old_tags,
            new_tags=new_tags,
            timestamp=datetime.fromisoformat(ev["start_iso"]),
            calendar=calendar["name"],
            duration_min=ev["duration_min"],
            action="reprocessing",
        )
        db.set_cached_event_tags(ev["event_id"], user_id, new_tags, manually_tagged=False)
    return True


def _run_reprocess(user_id: int, username: str):
    with _lock:
        _states[user_id] = _default_state()
        _states[user_id]["status"] = "running"
        _states[user_id]["started_at"] = datetime.now(timezone.utc).isoformat()

    try:
        rules = db.list_keyword_rules(user_id, enabled_only=True)
        cached = db.list_cached_events(user_id)
        with _lock:
            _states[user_id]["total"] = len(cached)

        for snapshot in cached:
            if _reprocess_one(snapshot, user_id, username, rules):
                with _lock:
                    _states[user_id]["changed"] += 1
            with _lock:
                _states[user_id]["processed"] += 1

        with _lock:
            _states[user_id]["status"] = "done"
            _states[user_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"Reprocess complete for user={username}: "
            f"{_states[user_id]['changed']}/{_states[user_id]['total']} event(s) reclassified"
        )

    except Exception as e:
        logger.error(f"Reprocess failed for user={username}: {e}")
        with _lock:
            _states[user_id]["status"] = "error"
            _states[user_id]["error"] = str(e)
            _states[user_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


def start_reprocess(user_id: int, username: str) -> bool:
    ''' Kicks off a background reprocess thread for one user. Returns
    False (without starting a new one) if a reprocess is already running
    for that user - the UI should surface the existing run's status
    instead. Different users can run reprocesses concurrently.
    '''
    with _lock:
        existing = _states.get(user_id)
        if existing and existing["status"] == "running":
            return False
    thread = threading.Thread(target=_run_reprocess, args=(user_id, username), daemon=True)
    thread.start()
    return True