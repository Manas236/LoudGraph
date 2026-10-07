import pytest


def test_state_machine_happy_path(temp_db):
    db = temp_db
    rid = db.create_run("t1")
    for st in ["data_ready", "picked", "labelled", "rendered", "awaiting_approval", "approved", "publishing", "published"]:
        db.transition(rid, st)
    assert db.get_run(rid)["stage"] == "published"
    stages = [r["stage"] for r in db.get_log(rid)]
    assert stages[0] == "queued" and stages[-1] == "published"


def test_illegal_transition_rejected(temp_db):
    db = temp_db
    rid = db.create_run("t1")
    with pytest.raises(db.TransitionError):
        db.transition(rid, "rendered")
    assert db.get_run(rid)["stage"] == "queued"


def test_fail_and_reset(temp_db):
    db = temp_db
    rid = db.create_run("t1")
    db.transition(rid, "data_ready")
    db.fail(rid, "pick", "boom")
    r = db.get_run(rid)
    assert r["stage"] == "failed" and r["failed_stage"] == "pick" and r["error"] == "boom"
    db.transition(rid, "data_ready", "retry", reset=True)
    r = db.get_run(rid)
    assert r["stage"] == "data_ready" and r["error"] is None


def test_claim_is_exclusive(temp_db):
    db = temp_db
    rid = db.create_run("t1")
    assert db.claim(rid, "queued", "data_ready")
    assert not db.claim(rid, "queued", "data_ready")


def test_posts_upsert(temp_db):
    db = temp_db
    rid = db.create_run("t1")
    db.upsert_post(rid, "youtube", "pending")
    db.upsert_post(rid, "youtube", "uploaded", remote_id="abc", url="https://youtu.be/abc")
    db.upsert_post(rid, "youtube", "live")
    p = db.get_posts(rid)
    assert len(p) == 1 and p[0]["status"] == "live" and p[0]["remote_id"] == "abc"
    with pytest.raises(ValueError):
        db.upsert_post(rid, "youtube", "bogus")
