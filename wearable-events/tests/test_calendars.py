"""Calendar CRUD - add/list/update/delete an ICS feed. Deliberately
doesn't touch the sync endpoint (POST /calendars/{id}/sync), since that
actually fetches the ICS URL over the network - out of scope for a
fast, network-free unit test.
"""


def test_add_and_list_calendar(auth_client):
    resp = auth_client.post(
        "/calendars",
        json={"name": "Work", "ics_url": "https://example.com/work.ics", "default_tag": "work"},
    )
    assert resp.status_code == 200
    calendar_id = resp.json()["id"]

    resp = auth_client.get("/calendars")
    assert resp.status_code == 200
    calendars = resp.json()
    assert len(calendars) == 1
    assert calendars[0]["id"] == calendar_id
    assert calendars[0]["name"] == "Work"
    assert calendars[0]["default_tag"] == "work"


def test_new_calendar_defaults_to_enabled(auth_client):
    resp = auth_client.post(
        "/calendars",
        json={"name": "Personal", "ics_url": "https://example.com/personal.ics", "default_tag": "personal"},
    )
    calendar_id = resp.json()["id"]
    calendar = next(c for c in auth_client.get("/calendars").json() if c["id"] == calendar_id)
    assert calendar["enabled"]


def test_patch_updates_only_the_given_fields(auth_client):
    calendar_id = auth_client.post(
        "/calendars",
        json={"name": "Work", "ics_url": "https://example.com/work.ics", "default_tag": "work"},
    ).json()["id"]

    resp = auth_client.patch(f"/calendars/{calendar_id}", json={"enabled": False})
    assert resp.status_code == 200

    calendar = next(c for c in auth_client.get("/calendars").json() if c["id"] == calendar_id)
    assert not calendar["enabled"]  # SQLite gives back 0/1, not a real bool - falsy check either way
    assert calendar["name"] == "Work"  # untouched field preserved


def test_delete_removes_the_calendar(auth_client):
    calendar_id = auth_client.post(
        "/calendars",
        json={"name": "Work", "ics_url": "https://example.com/work.ics", "default_tag": "work"},
    ).json()["id"]

    resp = auth_client.delete(f"/calendars/{calendar_id}")
    assert resp.status_code == 200
    assert auth_client.get("/calendars").json() == []


def test_calendars_are_scoped_per_user(auth_client, client):
    """A second account should never see the first account's
    calendars - the whole basis of this app's multi-tenant model (see
    wearable-events/README.md's "Known limitations" on this).
    """
    auth_client.post(
        "/calendars",
        json={"name": "Work", "ics_url": "https://example.com/work.ics", "default_tag": "work"},
    )

    auth_client.post("/users", json={"username": "seconduser", "password": "password123"})
    second = client
    second.post("/auth/login", json={"username": "seconduser", "password": "password123"})

    assert second.get("/calendars").json() == []
