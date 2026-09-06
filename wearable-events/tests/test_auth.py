"""Auth: login/logout, session cookie handling, and that every
protected route actually rejects an unauthenticated request. This last
part matters more than it might look - a route that forgets its own
`Depends(get_current_user)` is a real, easy-to-miss bug (see
wearable-events/README.md's own "Known limitations" on multi-tenant
isolation depending entirely on this being correct everywhere).
"""


def test_login_with_bootstrapped_admin_succeeds(client):
    resp = client.post(
        "/auth/login",
        json={"username": "testadmin", "password": "testpassword123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "testadmin"
    assert "session" in resp.cookies or len(resp.cookies) > 0


def test_login_with_wrong_password_fails(client):
    resp = client.post(
        "/auth/login",
        json={"username": "testadmin", "password": "wrong-password"},
    )
    assert resp.status_code == 401


def test_login_with_unknown_username_fails(client):
    resp = client.post(
        "/auth/login",
        json={"username": "nobody", "password": "whatever"},
    )
    assert resp.status_code == 401


def test_me_reflects_logged_in_user(auth_client):
    resp = auth_client.get("/auth/me")
    assert resp.status_code == 200
    assert resp.json()["username"] == "testadmin"


def test_me_without_session_is_401(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_logout_invalidates_the_session(auth_client):
    assert auth_client.get("/auth/me").status_code == 200
    resp = auth_client.post("/auth/logout")
    assert resp.status_code == 200
    # The cookie itself is deleted by /auth/logout, but the underlying
    # session row is also gone - either way, a followup request should
    # no longer be treated as authenticated.
    assert auth_client.get("/auth/me").status_code == 401


def test_every_endpoint_except_login_and_health_requires_a_session(client):
    """Not exhaustive over every single route, but covers one GET from
    each feature area - real coverage that the auth dependency is
    actually wired up everywhere it should be, not just on the routes
    this file happens to otherwise exercise.
    """
    protected_get_paths = [
        "/users",
        "/unclaimed_ring_users",
        "/timeline",
        "/sleep",
        "/calendars",
        "/keyword_rules",
        "/tag_definitions",
        "/reprocess/status",
        "/today",
    ]
    for path in protected_get_paths:
        resp = client.get(path)
        assert resp.status_code == 401, f"expected {path} to require auth, got {resp.status_code}"
