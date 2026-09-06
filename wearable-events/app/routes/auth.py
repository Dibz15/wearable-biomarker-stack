""" /auth/*, /users, /unclaimed_ring_users - login/session/user management. """

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app import auth, db
from app.auth import SESSION_COOKIE_NAME, get_current_user
from app.config import SESSION_COOKIE_SECURE, SESSION_MAX_AGE_DAYS
from app.queries.client import list_distinct_sensor_users

router = APIRouter()


class LoginIn(BaseModel):
    username: str
    password: str

class CreateUserIn(BaseModel):
    username: str
    password: str

# --- auth ---

@router.post("/auth/login")
def login(payload: LoginIn, response: Response):
    user = db.get_user_by_username(payload.username)
    if user is None or not auth.verify_password(payload.password, user["password_hash"]):
        raise HTTPException(401, "invalid username or password")

    token = auth.create_session(user["id"])
    response.set_cookie(
        SESSION_COOKIE_NAME, token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_MAX_AGE_DAYS * 86400,
    )
    return {"id": user["id"], "username": user["username"]}

@router.post("/auth/logout")
def logout(request: Request, response: Response, current_user: dict = Depends(get_current_user)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        auth.delete_session(token)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}

@router.get("/auth/me")
def get_me(current_user: dict = Depends(get_current_user)):
    return {"id": current_user["id"], "username": current_user["username"]}

@router.get("/users")
def get_users(current_user: dict = Depends(get_current_user)):
    ''' Any logged-in household member can see who else has an account
    (usernames only) - no roles/permissions distinction, this is a
    personal/family app, not a multi-tenant SaaS product.
    '''
    return db.list_users()

@router.get("/unclaimed_ring_users")
def get_unclaimed_ring_users(current_user: dict = Depends(get_current_user)):
    ''' Distinct `user` tag values seen in the ring parser's sensor data
    that don't already belong to an account here. Used by the "Add
    household member" form to offer picking an existing ring identity
    instead of free-typing a username that has to be manually kept in
    sync with GADGETBRIDGE_USER.

    An empty list is a normal, expected response (e.g. before any ring
    has synced yet) - the frontend falls back to manual entry, it's not
    treated as an error.
    '''
    sensor_users = set(list_distinct_sensor_users())
    claimed = {u["username"] for u in db.list_users()}
    return sorted(sensor_users - claimed)

@router.post("/users")
def post_user(payload: CreateUserIn, current_user: dict = Depends(get_current_user)):
    ''' Adds another household member. Requires being logged in as
    someone already, since there's no public signup page - this is the
    intended way to add a second/third person after the initial
    ADMIN_USERNAME/ADMIN_PASSWORD bootstrap account exists.

    Returns whether the chosen username matched existing ring sensor
    data at creation time, so the UI can confirm the link worked (or
    warn that it didn't, if someone typed a username manually instead
    of picking from the unclaimed list).
    '''
    if db.get_user_by_username(payload.username) is not None:
        raise HTTPException(400, "username already exists")
    user_id = auth.create_user(payload.username, payload.password)
    linked = payload.username in set(list_distinct_sensor_users())
    return {"id": user_id, "username": payload.username, "linked_to_ring_data": linked}