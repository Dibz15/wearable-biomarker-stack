import secrets
from datetime import datetime, timezone

import bcrypt
from fastapi import HTTPException, Request
from loguru import logger

from app import db
from app.config import DEFAULT_TAG_DEFINITIONS

SESSION_COOKIE_NAME = "wevents_session"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash - treat as a failed verification, not a crash
        return False


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.create_session(token, user_id)
    return token


def delete_session(token: str):
    db.delete_session(token)


# How stale last_seen_at is allowed to get before it's worth a write.
# last_seen_at exists to answer "is this session still in use", which
# minute-level precision answers just as well as second-level.
SESSION_TOUCH_INTERVAL_SECONDS = 60


def get_user_from_token(token: str) -> dict | None:
    ''' Resolves a session token to its user row, refreshing the
    session's last_seen_at at most once every
    SESSION_TOUCH_INTERVAL_SECONDS.

    This used to write on EVERY authenticated request. Each write is
    its own connection, transaction and commit (so, an fsync), and a
    single page load in this app fires up to six API requests in
    parallel - so six serialized write transactions, on storage that
    is often an SD card. Since the value only needs to be roughly
    right, skipping the write when it's already fresh removes almost
    all of them.
    '''
    row = db.get_session_with_user(token)
    if row is None:
        return None

    # Not part of the user identity - callers get the same dict shape
    # they always did.
    last_seen_at = row.pop("session_last_seen_at", None)
    if _session_touch_is_due(last_seen_at):
        db.touch_session(token)
    return row


def _session_touch_is_due(last_seen_at: str | None) -> bool:
    ''' True when last_seen_at is missing, unparseable, or older than
    the throttle interval. Anything unexpected errs toward writing -
    the write is what the old behavior always did, so falling back to
    it can only cost performance, never correctness.
    '''
    if not last_seen_at:
        return True
    try:
        # Written by SQLite's datetime('now'), which is UTC without a
        # timezone suffix - so it's parsed as naive and compared
        # against a naive UTC now, not the local clock.
        seen = datetime.strptime(last_seen_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return True
    # datetime.now(timezone.utc) rather than the deprecated utcnow(),
    # then dropped back to naive so it compares against the naive value
    # parsed above.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    age = (now - seen).total_seconds()
    # A negative age means the stored value is in the future (clock
    # skew, or a manually edited row) - treat that as due rather than
    # letting it suppress writes indefinitely.
    return age < 0 or age >= SESSION_TOUCH_INTERVAL_SECONDS


def get_current_user(request: Request) -> dict:
    ''' FastAPI dependency - resolves the session cookie to a user row,
    raising 401 if missing/invalid so the frontend knows to show the
    login screen.
    '''
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(401, "not authenticated")
    user = get_user_from_token(token)
    if user is None:
        raise HTTPException(401, "session expired or invalid")
    return user


def create_user(username: str, password: str) -> int:
    ''' Creates a user and seeds their default tag_definitions (the
    starting manual-entry buttons from spec §4), so a freshly created
    household member isn't looking at an empty Tags tab.
    '''
    user_id = db.create_user(username, hash_password(password))
    for tag, label, category, is_duration, sort_order in DEFAULT_TAG_DEFINITIONS:
        db.add_tag_definition(user_id, tag, label, category, is_duration=is_duration, sort_order=sort_order)
    return user_id


def bootstrap_admin_if_configured(admin_username: str | None, admin_password: str | None):
    ''' On startup, if no users exist yet and ADMIN_USERNAME/ADMIN_PASSWORD
    are set, create the first account automatically. There's no public
    signup page by design (this is meant to be reached only over
    Tailscale by household members) - after this first account exists,
    logged-in users can add further accounts from the UI.
    '''
    existing = db.count_users()
    if existing > 0:
        logger.info(f"Skipping bootstrap - {existing} user(s) already exist")
        return
    if not admin_username or not admin_password:
        logger.warning(
            "No users exist and ADMIN_USERNAME/ADMIN_PASSWORD are not both set - "
            "no account was created. Login will fail until one exists; set both "
            "env vars and restart, or create a user directly in the SQLite database."
        )
        return
    create_user(admin_username, admin_password)
    logger.info(f"Bootstrapped initial user: {admin_username!r}")