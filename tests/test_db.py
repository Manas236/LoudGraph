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


def test_progress_is_written_and_cleared_on_transition(temp_db):
    db = temp_db
    rid = db.create_run("t1")
    for st in ["data_ready", "picked", "labelled"]:
        db.transition(rid, st)
    entered = db.get_run(rid)["updated_at"]
    db.set_progress(rid, 0.4321)
    r = db.get_run(rid)
    assert r["progress"] == 0.4321 and r["progress_at"] and r["updated_at"] == entered  # time in stage kept
    db.transition(rid, "rendered")
    r = db.get_run(rid)
    assert r["progress"] is None and r["progress_at"] is None


def test_old_database_gets_the_new_columns(tmp_path, monkeypatch):
    import sqlite3
    from pipeline import db
    f = tmp_path / "old.db"
    c = sqlite3.connect(f)
    c.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, stage TEXT NOT NULL, failed_stage TEXT, "
              "error TEXT, countries TEXT, country_set TEXT, score REAL, regen_of TEXT, created_at TEXT NOT NULL, "
              "updated_at TEXT NOT NULL)")
    c.execute("INSERT INTO runs VALUES ('r1','t1','labelled',NULL,NULL,NULL,NULL,NULL,NULL,"
              "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
    c.commit()
    c.close()
    monkeypatch.setattr(db, "path", lambda key: f)
    db.set_progress("r1", 0.5)
    assert db.get_run("r1")["progress"] == 0.5


def test_youtube_cap_counts_the_pacific_quota_day(temp_db):
    from datetime import datetime, timezone
    db = temp_db
    before, after = db.create_run("t1"), db.create_run("t2")
    db.upsert_post(before, "youtube", "live")
    db.upsert_post(after, "youtube", "live")
    # 15 July is PDT (UTC-7), so the quota day starts at 07:00 UTC
    with db.db() as c:
        c.execute("UPDATE posts SET updated_at=? WHERE run_id=?", ("2026-07-15T06:59:00Z", before))  # 23:59 the day before
        c.execute("UPDATE posts SET updated_at=? WHERE run_id=?", ("2026-07-15T07:01:00Z", after))   # 00:01
    now = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
    assert db.quota_day_start(now) == datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc)
    assert db.uploads_today("youtube", now) == 1
    # January is PST (UTC-8); 03:00 UTC on the 10th is still the 9th in California
    assert db.quota_day_start(datetime(2026, 1, 10, 3, 0, tzinfo=timezone.utc)) == \
        datetime(2026, 1, 9, 8, 0, tzinfo=timezone.utc)


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
