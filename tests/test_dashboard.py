import json
import re
from datetime import datetime, timedelta, timezone

import pytest

FLOW = ["data_ready", "picked", "labelled", "rendered", "awaiting_approval", "approved", "publishing", "published"]


@pytest.fixture
def client(temp_db):
    from dashboard import app as appmod
    appmod.app.config["TESTING"] = True
    return appmod, appmod.app.test_client()


def _to(db, rid, stages):
    for st in stages:
        db.transition(rid, st)
    return rid


def _lane(html, key):
    m = re.search(rf'<section class="lane lane-{key}" data-lane="{key}">(.*?)</section>', html, re.S)
    assert m, f"lane {key} missing"
    return m.group(1)


def _cards(html):
    return dict(re.findall(r'<article class="card" data-run="([^"]+)"[^>]*>(.*?)</article>', html, re.S))


def test_every_stage_has_exactly_one_lane():
    from dashboard.app import LANES, lane_for
    from pipeline.db import STAGES
    assert [name for _, name, _ in LANES] == ["Data secured", "Video made", "Waiting for approval", "Approved",
                                               "Shipped", "Failed", "Rejected"]
    assert sorted(s for _, _, stages in LANES for s in stages) == sorted(STAGES)   # each stage exactly once
    assert {s: lane_for(s) for s in STAGES} == {
        "queued": "data", "data_ready": "data", "picked": "data", "labelled": "data", "rendered": "made",
        "awaiting_approval": "waiting", "approved": "approved", "publishing": "approved", "published": "shipped",
        "failed": "failed", "rejected": "rejected"}


def test_chip_states_cover_every_post_status():
    from dashboard.app import CHIP, chip_state
    from pipeline.db import POST_STATUSES
    assert set(CHIP) == POST_STATUSES
    assert chip_state(None, "awaiting_approval") == "pending"
    assert chip_state(None, "rejected") == "none"
    for status, chip in [("pending", "pending"), ("uploading", "uploading"), ("uploaded", "uploading"),
                         ("live", "live"), ("private_locked", "private-locked"), ("dry_run", "dry-run"),
                         ("failed", "failed")]:
        assert chip_state({"status": status}, "published") == chip


def test_cards_land_in_their_lanes(client):
    appmod, c = client
    from pipeline import db
    runs = {lane: db.create_run("inflation") for lane in ("data", "made", "waiting", "approved", "shipped", "rejected")}
    _to(db, runs["data"], FLOW[:2])
    _to(db, runs["made"], FLOW[:4])
    _to(db, runs["waiting"], FLOW[:5])
    _to(db, runs["approved"], FLOW[:6])
    _to(db, runs["shipped"], FLOW)
    _to(db, runs["rejected"], FLOW[:5] + ["rejected"])
    html = c.get("/fragment/board").data.decode()
    for lane, rid in runs.items():
        assert rid in _cards(_lane(html, lane)), lane
    assert c.get("/api/board").get_json()["waiting"] == [runs["waiting"]]


def test_waiting_card_shows_sent_to_telegram(client):
    appmod, c = client
    from pipeline import db
    sent, unsent = (_to(db, db.create_run("inflation"), FLOW[:5]) for _ in range(2))
    db.log(sent, "awaiting_approval", "telegram: sent message 42")
    cards = _cards(_lane(c.get("/fragment/board").data.decode(), "waiting"))
    assert "sent to Telegram ✓" in cards[sent]
    assert "sent to Telegram ✓" not in cards[unsent]


def test_platform_chips_follow_post_status_and_link_live_posts(client):
    appmod, c = client
    from pipeline import db
    rid = _to(db, db.create_run("inflation"), FLOW)
    db.upsert_post(rid, "youtube", "live", remote_id="v1", url="https://youtube.com/shorts/v1")
    db.upsert_post(rid, "instagram", "failed", message="boom")
    card = _cards(c.get("/fragment/board").data.decode())[rid]
    assert re.search(r'<a class="chip c-live" data-chip="youtube" href="https://youtube.com/shorts/v1"', card)
    assert re.search(r'<span class="chip c-failed" data-chip="instagram"', card)


def test_render_progress_shows_on_the_card(client):
    appmod, c = client
    from pipeline import db
    from pipeline.orchestrator import _progress_writer
    rid = _to(db, db.create_run("inflation"), FLOW[:3])     # labelled: the render step is running
    _progress_writer(rid)(86, 200)
    assert db.get_run(rid)["progress"] == 0.43
    html = c.get("/fragment/board").data.decode()
    assert "making the video 43%" in _cards(_lane(html, "data"))[rid]
    assert 'data-busy="1"' in html                          # the board polls every 2 s while busy


def test_failed_card_shows_error_and_retries_from_its_stage(client, monkeypatch):
    appmod, c = client
    from pipeline import actions, db
    spawned = []
    monkeypatch.setattr(actions, "spawn", lambda *a: spawned.append(a) or 0)
    rid = _to(db, db.create_run("inflation"), FLOW[:3])
    db.fail(rid, "render", "RuntimeError: ffmpeg exited 1")
    card = _cards(_lane(c.get("/fragment/board").data.decode(), "failed"))[rid]
    assert "failed at render" in card and "ffmpeg exited 1" in card and 'name="step" value="render"' in card
    r = c.post(f"/run/{rid}/retry", data={"csrf": appmod.CSRF, "step": "render", "next": "board"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert spawned == [("produce", "--run", rid, "--from", "render")]


def test_retry_publish_reapproves_and_publishes(temp_db, monkeypatch):
    from pipeline import actions
    db = temp_db
    spawned = []
    monkeypatch.setattr(actions, "spawn", lambda *a: spawned.append(a) or 0)
    rid = _to(db, db.create_run("inflation"), FLOW[:7])
    db.fail(rid, "publish", "all platforms failed")
    assert actions.retry(rid, "publish", "test") == "publishing again"
    assert db.get_run(rid)["stage"] == "approved" and spawned == [("publish", "--run", rid)]
    with pytest.raises(ValueError):
        actions.retry(rid, "publish", "test")             # only for a run whose publish failed


def test_make_video_button_runs_produce_count_1(client, monkeypatch):
    appmod, c = client
    from pipeline import actions
    spawned = []
    monkeypatch.setattr(actions, "spawn", lambda *a: spawned.append(a) or 0)
    assert c.post("/make").status_code == 403
    assert c.post("/make", data={"csrf": appmod.CSRF}).status_code == 302
    assert spawned == [("produce", "--count", "1")]
    c.post("/make", data={"csrf": appmod.CSRF})            # a double click within a minute starts nothing
    assert len(spawned) == 1


def test_health_panel_turns_red_when_the_bot_is_silent(client):
    appmod, c = client
    from pipeline import db
    old = (datetime.now(timezone.utc) - timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.kv_set("heartbeat:bot", json.dumps({"ts": old}))
    assert 'data-bot-state="stale"' in c.get("/fragment/board").data.decode()
    db.kv_set("heartbeat:bot", json.dumps({"ts": db.now()}))
    assert 'data-bot-state="ok"' in c.get("/fragment/board").data.decode()


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
