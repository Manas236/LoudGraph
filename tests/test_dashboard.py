import pytest


@pytest.fixture
def client(temp_db):
    from dashboard import app as appmod
    appmod.app.config["TESTING"] = True
    return appmod, appmod.app.test_client()


def test_pages_render(client):
    appmod, c = client
    from pipeline import db
    rid = db.create_run("inflation")
    for url in ["/", "/fragment/board", f"/run/{rid}", "/analytics", "/topics", "/api/board"]:
        r = c.get(url)
        assert r.status_code == 200, url
    assert rid.encode() in c.get("/fragment/board").data


def test_post_requires_csrf_token(client):
    appmod, c = client
    from pipeline import db
    rid = db.create_run("inflation")
    assert c.post(f"/run/{rid}/reject").status_code == 403


def test_reject_button_uses_shared_action(client):
    appmod, c = client
    from pipeline import db
    rid = db.create_run("inflation")
    for st in ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]:
        db.transition(rid, st)
    r = c.post(f"/run/{rid}/reject", data={"csrf": appmod.CSRF})
    assert r.status_code == 302
    assert db.get_run(rid)["stage"] == "rejected"
    assert any("via dashboard" in e["message"] for e in db.get_log(rid))


def test_media_whitelist(client):
    appmod, c = client
    assert c.get("/media/r20260101-000000-abcd/../../config.yaml").status_code == 404
    assert c.get("/media/r20260101-000000-abcd/data.json").status_code == 404


def test_refuses_public_bind(monkeypatch):
    from dashboard import app as appmod
    cfg = dict(appmod.get_config())
    cfg["dashboard"] = {"host": "0.0.0.0", "port": 5055}
    monkeypatch.setattr(appmod, "get_config", lambda: cfg)
    with pytest.raises(SystemExit, match="loopback only"):
        appmod.main()
