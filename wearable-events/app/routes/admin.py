""" /reprocess/*, /tag_definitions/*, /health - admin utilities and the unauthenticated healthcheck. """

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db, reprocess
from app.auth import get_current_user

router = APIRouter()


class TagDefinitionIn(BaseModel):
    tag: str
    label: str
    category: str  # 'context' | 'substance' | 'restful' | 'meta'
    is_duration: bool = False
    sort_order: int = 0

@router.post("/reprocess")
def post_reprocess(current_user: dict = Depends(get_current_user)):
    ''' Kicks off a background reprocess of the current user's cached
    calendar events against their current (just-saved) ruleset. Runs in
    a thread rather than blocking this request or the rest of the UI -
    poll GET /reprocess/status for progress.
    '''
    started = reprocess.start_reprocess(current_user["id"], current_user["username"])
    if not started:
        return {"started": False, "reason": "already running", **reprocess.get_status(current_user["id"])}
    return {"started": True, **reprocess.get_status(current_user["id"])}

@router.get("/reprocess/status")
def get_reprocess_status(current_user: dict = Depends(get_current_user)):
    return reprocess.get_status(current_user["id"])

# --- tag definitions ---

@router.get("/tag_definitions")
def get_tag_definitions(current_user: dict = Depends(get_current_user)):
    return db.list_tag_definitions(current_user["id"])

@router.post("/tag_definitions")
def post_tag_definition(payload: TagDefinitionIn, current_user: dict = Depends(get_current_user)):
    try:
        tag_def_id = db.add_tag_definition(
            current_user["id"], payload.tag, payload.label, payload.category,
            is_duration=payload.is_duration, sort_order=payload.sort_order,
        )
    except Exception as e:
        raise HTTPException(400, f"could not add tag: {e}") from e
    return {"id": tag_def_id}

@router.delete("/tag_definitions/{tag_def_id}")
def delete_tag_definition(tag_def_id: int, current_user: dict = Depends(get_current_user)):
    db.delete_tag_definition(tag_def_id, current_user["id"])
    return {"ok": True}

# --- health check (unauthenticated - used by docker healthcheck /
# restart policies) ---

@router.get("/health")
def health():
    return {"status": "ok"}