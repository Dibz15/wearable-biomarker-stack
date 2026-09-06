""" /events/*, /timeline - manually-created calendar events. """

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app import db
from app.auth import get_current_user
from app.queries.calendar_events import (
    delete_event_tag_point,
    find_manual_event_by_id,
    find_manual_events_in_range,
    manual_event_id,
    write_event_points,
)

router = APIRouter()


# --- request/response models ---

class ManualEventIn(BaseModel):
    tags: list[str]
    duration_min: int | None = None

class ManualEventUpdateIn(BaseModel):
    ''' Partial update - only fields actually provided are changed.
    tags, if given, is a full replacement set (not a diff).
    '''
    tags: list[str] | None = None
    timestamp: str | None = None  # ISO 8601
    duration_min: int | None = None

# --- events ---

@router.post("/events")
def post_event(payload: ManualEventIn, current_user: dict = Depends(get_current_user)):
    if not payload.tags:
        raise HTTPException(400, "at least one tag is required")

    event_id = manual_event_id()
    write_event_points(
        user=current_user["username"],
        tags=payload.tags,
        source="manual",
        timestamp=datetime.now(timezone.utc),
        event_id=event_id,
        duration_min=payload.duration_min,
    )
    return {"event_id": event_id, "tags": payload.tags}

@router.patch("/events/{event_id}")
def patch_event(event_id: str, payload: ManualEventUpdateIn, current_user: dict = Depends(get_current_user)):
    ''' Edit a manual event's tags, timestamp, and/or duration. At least
    one field must be provided. Unset fields keep their current value.

    duration_min is how the Tags tab's start/stop timer for
    is_duration-flagged buttons gets its final value: the tap that
    starts the timer POSTs the event with no duration, and the tap that
    stops it PATCHes duration_min in here once elapsed time is known.
    It's also editable directly, as a manual correction/safety net for
    a timer that got orphaned (e.g. the tab was closed mid-timer).
    '''
    if payload.tags is None and payload.timestamp is None and payload.duration_min is None:
        raise HTTPException(400, "provide at least one of: tags, timestamp, duration_min")

    username = current_user["username"]
    existing = find_manual_event_by_id(username, event_id)
    if existing is None:
        raise HTTPException(404, "manual event not found")

    old_tags = set(existing["tags"])
    old_timestamp = existing["timestamp"]

    new_tags = set(payload.tags) if payload.tags is not None else old_tags
    new_duration_min = payload.duration_min if payload.duration_min is not None else existing["duration_min"]

    if payload.timestamp is not None:
        try:
            new_timestamp = datetime.fromisoformat(payload.timestamp)
            if new_timestamp.tzinfo is None:
                new_timestamp = new_timestamp.replace(tzinfo=timezone.utc)
        except ValueError as e:
            raise HTTPException(400, f"invalid timestamp: {e}") from e
    else:
        new_timestamp = old_timestamp

    if not new_tags:
        raise HTTPException(400, "at least one tag is required")

    timestamp_changed = new_timestamp != old_timestamp

    # If the timestamp is moving, every old point (all old tags) needs
    # deleting from the old timestamp - a partial tag diff doesn't make
    # sense once the point in time itself has changed. If the timestamp
    # is unchanged, only delete the tags actually being removed. Note a
    # duration_min-only change (same tags, same timestamp) still needs
    # a full rewrite below since duration_min lives on every point for
    # this event_id - but nothing needs deleting first in that case.
    tags_to_delete = old_tags if timestamp_changed else (old_tags - new_tags)
    for tag in tags_to_delete:
        try:
            delete_event_tag_point(tag=tag, source="manual", timestamp=old_timestamp)
        except Exception as e:
            logger.warning(f"Failed to delete stale point for manual event {event_id} (tag={tag}): {e}")

    write_event_points(
        user=username,
        tags=sorted(new_tags),
        source="manual",
        timestamp=new_timestamp,
        event_id=event_id,
        duration_min=new_duration_min,
    )

    return {
        "event_id": event_id,
        "tags": sorted(new_tags),
        "timestamp": new_timestamp.isoformat(),
        "duration_min": new_duration_min,
    }

@router.delete("/events/{event_id}")
def delete_event(event_id: str, current_user: dict = Depends(get_current_user)):
    ''' Delete a manual event entirely (all its tags). '''
    username = current_user["username"]
    existing = find_manual_event_by_id(username, event_id)
    if existing is None:
        raise HTTPException(404, "manual event not found")

    for tag in existing["tags"]:
        try:
            delete_event_tag_point(tag=tag, source="manual", timestamp=existing["timestamp"])
        except Exception as e:
            logger.warning(f"Failed to delete point for manual event {event_id} (tag={tag}): {e}")

    return {"ok": True}

# --- timeline (read-only merged view) ---

@router.get("/timeline")
def get_timeline(start: str | None = None, end: str | None = None, current_user: dict = Depends(get_current_user)):
    ''' Read-only merged view of calendar-derived events (from the local
    cache, so no ICS re-fetch needed) and manual tag logs (from
    InfluxDB), sorted chronologically. Powers the Timeline tab.

    `start`/`end` are ISO date strings (YYYY-MM-DD). Defaults to the
    last 7 days through tomorrow if omitted.
    '''
    now = datetime.now(timezone.utc)
    try:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else now - timedelta(days=7)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else now + timedelta(days=1)
    except ValueError as e:
        raise HTTPException(400, f"invalid start/end date: {e}") from e

    user_id = current_user["id"]
    calendar_names = {c["id"]: c["name"] for c in db.list_calendars(user_id)}

    entries = []

    # Calendar-derived - read straight from the local cache, no ICS
    # fetch, so this is always fast and doesn't touch external feeds.
    for ev in db.list_cached_events(user_id):
        try:
            ev_start = datetime.fromisoformat(ev["start_iso"])
        except ValueError:
            continue
        if not (start_dt <= ev_start < end_dt):
            continue
        entries.append({
            "kind": "calendar",
            "event_id": ev["event_id"],
            "timestamp": ev["start_iso"],
            "title": ev["title"],
            "calendar": calendar_names.get(ev["calendar_id"], "(deleted calendar)"),
            "tags": json.loads(ev["applied_tags"] or "[]"),
            "manually_tagged": bool(ev["manually_tagged"]),
            "duration_min": ev["duration_min"],
        })

    # Manual taps - from InfluxDB, reconstructed per event_id
    for ev in find_manual_events_in_range(current_user["username"], start_dt, end_dt):
        entries.append({
            "kind": "manual",
            "event_id": ev["event_id"],
            "timestamp": ev["timestamp"],
            "title": None,
            "calendar": None,
            "tags": ev["tags"],
            "duration_min": ev["duration_min"],
        })

    entries.sort(key=lambda e: e["timestamp"])
    return entries