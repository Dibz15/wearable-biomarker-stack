""" /calendars/*, /calendar_events/*, /keyword_rules/* - calendar CRUD, sync, tag rules. """

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app import db
from app.auth import get_current_user
from app.ics_sync import classify_event
from app.queries.calendar_events import delete_event_tag_point, write_event_points
from app.reprocess import compute_reclassification_diff

router = APIRouter()


class CalendarEventTagsIn(BaseModel):
    tags: list[str]  # full replacement set

class CalendarIn(BaseModel):
    name: str
    ics_url: str
    default_tag: str

class CalendarUpdateIn(BaseModel):
    name: str | None = None
    ics_url: str | None = None
    default_tag: str | None = None
    enabled: bool | None = None

class KeywordRuleIn(BaseModel):
    keyword: str
    tag: str
    category: str  # 'context' | 'meta' | 'substance' | 'restful'
    is_regex: bool = False
    match_field: str = "title"  # 'title' | 'description'
    priority: int = 0
    enabled: bool = True
    exclusive: bool = True  # False = "stack" this tag rather than compete for its category's one slot

class KeywordRuleBatchIn(BaseModel):
    ''' Staged batch of rule changes from the Manage tab - nothing hits
    the database until this is posted.
    '''
    added: list[KeywordRuleIn] = []
    deleted_ids: list[int] = []

# --- calendars ---

@router.get("/calendars")
def get_calendars(current_user: dict = Depends(get_current_user)):
    return db.list_calendars(current_user["id"])

@router.post("/calendars")
def post_calendar(payload: CalendarIn, current_user: dict = Depends(get_current_user)):
    try:
        calendar_id = db.add_calendar(current_user["id"], payload.name, payload.ics_url, payload.default_tag)
    except Exception as e:
        raise HTTPException(400, f"could not add calendar: {e}") from e
    return {"id": calendar_id}

@router.patch("/calendars/{calendar_id}")
def patch_calendar(calendar_id: int, payload: CalendarUpdateIn, current_user: dict = Depends(get_current_user)):
    fields = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    db.update_calendar(calendar_id, current_user["id"], **fields)
    return {"ok": True}

@router.delete("/calendars/{calendar_id}")
def delete_calendar(calendar_id: int, current_user: dict = Depends(get_current_user)):
    db.delete_calendar(calendar_id, current_user["id"])
    return {"ok": True}

@router.post("/calendars/{calendar_id}/sync")
def trigger_calendar_sync(calendar_id: int, current_user: dict = Depends(get_current_user)):
    ''' Manual "sync now" for one of the current user's calendars.
    '''
    calendar = db.get_calendar(calendar_id, current_user["id"])
    if calendar is None:
        raise HTTPException(404, "calendar not found")

    from app.ics_sync import sync_calendar
    rules = db.list_keyword_rules(current_user["id"], enabled_only=True)
    sync_calendar(calendar, rules, current_user["username"])
    return {"ok": True}

@router.patch("/calendar_events/{event_id}/tags")
def patch_calendar_event_tags(event_id: str, payload: CalendarEventTagsIn, current_user: dict = Depends(get_current_user)):
    ''' Manually override a calendar-derived event's tags (add/edit/
    remove), independent of keyword-rule classification. Marks the
    event manually_tagged=1, so future scheduled syncs and reprocess
    runs leave it alone from now on - see the schema.sql comment on
    that column for why this is necessary (without it, the very next
    15-minute sync would silently revert the edit back to whatever the
    ruleset says).
    '''
    if not payload.tags:
        raise HTTPException(400, "at least one tag is required")

    user_id = current_user["id"]
    cached = db.get_cached_event(event_id, user_id)
    if cached is None:
        raise HTTPException(404, "calendar event not found")

    calendar = db.get_calendar(cached["calendar_id"], user_id)
    if calendar is None:
        raise HTTPException(409, "this event's calendar no longer exists")

    old_tags = set(json.loads(cached["applied_tags"] or "[]"))
    new_tags = set(payload.tags)
    start_dt = datetime.fromisoformat(cached["start_iso"])

    removed = old_tags - new_tags
    for tag in removed:
        try:
            delete_event_tag_point(tag=tag, source="calendar", timestamp=start_dt, calendar=calendar["name"])
        except Exception as e:
            logger.warning(f"Failed to delete stale point for calendar event {event_id} (tag={tag}): {e}")

    write_event_points(
        user=current_user["username"],
        tags=sorted(new_tags),
        source="calendar",
        timestamp=start_dt,
        event_id=event_id,
        calendar=calendar["name"],
        duration_min=cached["duration_min"],
    )
    db.set_cached_event_tags(event_id, sorted(new_tags), manually_tagged=True)

    return {"event_id": event_id, "tags": sorted(new_tags)}

@router.post("/calendar_events/{event_id}/reset_tags")
def reset_calendar_event_tags(event_id: str, current_user: dict = Depends(get_current_user)):
    ''' Undoes a manual tag override: re-runs keyword classification
    against the *current* ruleset (not whatever was cached before the
    override), writes the result, and clears manually_tagged so future
    syncs/reprocess apply normally to this event again.
    '''
    user_id = current_user["id"]
    cached = db.get_cached_event(event_id, user_id)
    if cached is None:
        raise HTTPException(404, "calendar event not found")
    if not cached["manually_tagged"]:
        raise HTTPException(400, "this event isn't manually tagged - nothing to reset")

    calendar = db.get_calendar(cached["calendar_id"], user_id)
    if calendar is None:
        raise HTTPException(409, "this event's calendar no longer exists")

    rules = db.list_keyword_rules(user_id, enabled_only=True)
    new_tags = classify_event(cached["title"], cached["description"], rules, calendar["default_tag"])
    old_tags = set(json.loads(cached["applied_tags"] or "[]"))
    start_dt = datetime.fromisoformat(cached["start_iso"])

    removed = old_tags - set(new_tags)
    for tag in removed:
        try:
            delete_event_tag_point(tag=tag, source="calendar", timestamp=start_dt, calendar=calendar["name"])
        except Exception as e:
            logger.warning(f"Failed to delete stale point while resetting calendar event {event_id} (tag={tag}): {e}")

    write_event_points(
        user=current_user["username"],
        tags=new_tags,
        source="calendar",
        timestamp=start_dt,
        event_id=event_id,
        calendar=calendar["name"],
        duration_min=cached["duration_min"],
    )
    db.set_cached_event_tags(event_id, new_tags, manually_tagged=False)

    return {"event_id": event_id, "tags": new_tags}

# --- keyword rules ---

@router.get("/keyword_rules")
def get_keyword_rules(current_user: dict = Depends(get_current_user)):
    return db.list_keyword_rules(current_user["id"])

@router.post("/keyword_rules/save_batch")
def save_keyword_rules_batch(payload: KeywordRuleBatchIn, current_user: dict = Depends(get_current_user)):
    ''' Commits a staged batch of add/delete changes to this user's
    ruleset in one go, then returns a precise count (and sample titles)
    of how many already-synced cached events would be reclassified
    differently under the new ruleset.

    This endpoint only commits the rule changes and reports the diff -
    it does NOT reprocess/rewrite any Influx data itself. That's a
    separate, explicit step via POST /reprocess.
    '''
    user_id = current_user["id"]
    old_rules = db.list_keyword_rules(user_id, enabled_only=True)

    for rule_id in payload.deleted_ids:
        db.delete_keyword_rule(rule_id, user_id)

    for rule in payload.added:
        if rule.category not in {"context", "meta", "substance", "restful"}:
            raise HTTPException(400, f"invalid category: {rule.category}")
        db.add_keyword_rule(
            user_id, rule.keyword, rule.tag, rule.category,
            is_regex=rule.is_regex, match_field=rule.match_field,
            priority=rule.priority, enabled=rule.enabled, exclusive=rule.exclusive,
        )

    new_rules = db.list_keyword_rules(user_id, enabled_only=True)
    diff = compute_reclassification_diff(user_id, old_rules, new_rules)

    return {
        "saved": True,
        "affected_events": diff["count"],
        "sample_titles": diff["sample_titles"],
    }