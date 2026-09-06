"""Tag definitions - the manual one-tap context tags (caffeine,
alcohol, etc.) shown as buttons on the Today tab. Pure SQLite CRUD.
"""


def test_new_user_gets_the_default_seeded_tags(auth_client):
    """DEFAULT_TAG_DEFINITIONS (app/config.py) is seeded for every
    newly created account - confirms that seeding actually happens on
    the bootstrapped admin account, not just documented in config.py.
    """
    resp = auth_client.get("/tag_definitions")
    assert resp.status_code == 200
    tags = {t["tag"] for t in resp.json()}
    assert "caffeine" in tags
    assert "alcohol" in tags


def test_add_and_list_tag_definition(auth_client):
    resp = auth_client.post(
        "/tag_definitions",
        json={"tag": "meditation", "label": "Meditation", "category": "restful"},
    )
    assert resp.status_code == 200
    tag_def_id = resp.json()["id"]

    tags = auth_client.get("/tag_definitions").json()
    added = next(t for t in tags if t["id"] == tag_def_id)
    assert added["tag"] == "meditation"
    assert added["label"] == "Meditation"
    assert added["category"] == "restful"


def test_delete_removes_the_tag(auth_client):
    tag_def_id = auth_client.post(
        "/tag_definitions",
        json={"tag": "meditation", "label": "Meditation", "category": "restful"},
    ).json()["id"]

    resp = auth_client.delete(f"/tag_definitions/{tag_def_id}")
    assert resp.status_code == 200

    tags = auth_client.get("/tag_definitions").json()
    assert not any(t["id"] == tag_def_id for t in tags)


def test_duplicate_tag_name_is_rejected(auth_client):
    payload = {"tag": "meditation", "label": "Meditation", "category": "restful"}
    assert auth_client.post("/tag_definitions", json=payload).status_code == 200
    resp = auth_client.post("/tag_definitions", json=payload)
    assert resp.status_code == 400
