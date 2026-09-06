"""Two unauthenticated-by-design routes: the Docker healthcheck
endpoint, and the SPA fallback that lets a page refresh (or a
bookmark/shared link) land on any /app/* page directly instead of
404ing - see app/main.py's own comment on spa_fallback for why this
exists and why /app/* specifically (not a bare path like /sleep,
which would collide with a real API endpoint of the same name).
"""


def test_health_is_unauthenticated(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_spa_fallback_serves_the_shell_for_any_app_path(client):
    resp = client.get("/app/activity/workout/12345")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()


def test_spa_fallback_serves_the_same_shell_regardless_of_path(client):
    a = client.get("/app/today")
    b = client.get("/app/sleep/duration")
    assert a.status_code == b.status_code == 200
    assert a.text == b.text


def test_spa_fallback_does_not_shadow_real_api_routes(client):
    """/sleep is a real API endpoint (GET /sleep, sleep history) -
    confirms the /app/* fallback can't accidentally swallow it.
    Unauthenticated here on purpose: a 401 (not the HTML shell) is
    exactly the evidence that the real API route handled this request,
    not the fallback.
    """
    resp = client.get("/sleep")
    assert resp.status_code == 401
