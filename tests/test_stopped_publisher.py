"""A publisher that died mid-post: the run is settled as failed with a retry, and a retry never posts
a second copy of a reel that had gone live after all. The Graph API is faked (no network)."""
import json

import pytest

FLOW = ["data_ready", "picked", "labelled", "rendered", "awaiting_approval", "approved", "publishing"]
OLD = "2020-01-01T00:00:00Z"
CAPTION = "The rise and fall of nuclear power"
BLOCKED = {"error": {"message": "Cannot call API for app 1 on behalf of user 2", "type": "OAuthException", "code": 200}}


@pytest.fixture
def env(temp_db, tmp_path, monkeypatch, pinned_config):
    from pipeline import accounts, orchestrator
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"))
    pinned_config["youtube"]["enabled"], pinned_config["facebook"]["enabled"] = False, True
    pinned_config["dry_run"].update(instagram=False, facebook=False)
    monkeypatch.setenv("IG_USER_ID", "7")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram", "facebook"])
    return temp_db, None


def publishing(db):
    from pipeline.config import run_dir
    rid = db.create_run("nuclear_share")
    for st in FLOW:
        chosen = {"platforms": json.dumps(["instagram", "facebook"]), "approved_by": "dashboard"} if st == "approved" else {}
        db.transition(rid, st, **chosen)
    (run_dir(rid) / "meta.json").write_text(json.dumps({"title": CAPTION, "description_instagram": CAPTION + "\n"}))
    return rid


def long_ago(db):
    with db.db() as c:
        c.execute("UPDATE runs SET updated_at=?", (OLD,))
        c.execute("UPDATE posts SET updated_at=?", (OLD,))


def statuses(db, rid):
    return {p["platform"]: p for p in db.get_posts(rid)}


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self.data


@pytest.fixture
def graph(monkeypatch):
    """Answers a GET by the end of its URL; any POST (an upload or a publish) fails the test."""
    from pipeline import meta_api
    answers, asked = {}, []

    def get(url, params=None, timeout=None):
        asked.append(url.rsplit("/", 1)[-1])
        data = next(v for k, v in answers.items() if url.endswith(k))
        return Response(data, 400 if "error" in data else 200)
    monkeypatch.setattr(meta_api.requests, "get", get)
    monkeypatch.setattr(meta_api.requests, "post", lambda *a, **k: pytest.fail("read-only: nothing may be posted"))
    return answers, asked


# ------------------------------------------------------------------ the publisher died

def test_a_dead_publisher_is_reported_with_a_retry(env):
    from pipeline import orchestrator
    from pipeline.errors import explain_error
    from pipeline.lock import publish_lock, single_instance
    db, alerts = env
    rid = publishing(db)
    db.upsert_post(rid, "instagram", "uploading", remote_id="c1", message="file uploaded, Instagram is processing")
    assert orchestrator.settle_stopped() == []                 # it may only just have started
    long_ago(db)
    with single_instance(publish_lock(rid)):
        assert orchestrator.settle_stopped() == []             # slow, but its publisher is alive
    assert orchestrator.settle_stopped() == [rid]
    posts, run = statuses(db, rid), db.get_run(rid)
    assert posts["instagram"]["status"] == posts["facebook"]["status"] == "failed"
    assert "Instagram is processing" in posts["instagram"]["message"] and posts["instagram"]["remote_id"] == "c1"
    assert "before this platform was started" in posts["facebook"]["message"]
    assert run["stage"] == "failed" and run["failed_stage"] == "publish" and run["approved_by"] == "dashboard"
    assert explain_error(posts["instagram"]["message"], "publish", "Instagram")["sentence"] == \
        "Instagram upload failed: the upload stopped before it finished."
    from pipeline import home
    shown = sorted(p["text"] for p in home.problems())          # on the Telegram home card, with Retry
    assert shown == ["⚠️ Posting failed for Nuclear power on Facebook: The upload stopped before it finished.",
                     "⚠️ Posting failed for Nuclear power on Instagram: The upload stopped before it finished."]
    assert orchestrator.settle_stopped() == [] and sorted(p["text"] for p in home.problems()) == shown   # once


def test_a_dead_publisher_keeps_what_went_live(env):
    from pipeline import orchestrator
    db, alerts = env
    rid = publishing(db)
    db.upsert_post(rid, "instagram", "live", remote_id="m1", url="https://instagram.com/reel/x")
    db.upsert_post(rid, "facebook", "uploading", remote_id="v1", message="upload session started, uploading")
    long_ago(db)
    assert orchestrator.settle_stopped() == [rid]
    posts = statuses(db, rid)
    assert posts["instagram"]["status"] == "live" and posts["facebook"]["status"] == "failed"
    assert db.get_run(rid)["stage"] == "published"


# ------------------------------------------------------------------ retrying never posts twice

def retried(db, platform, remote_id):
    """A run whose publisher died on `platform`, settled and sent back for a retry."""
    from pipeline import orchestrator
    rid = publishing(db)
    db.upsert_post(rid, platform, "uploading", remote_id=remote_id, message="uploading")
    long_ago(db)
    orchestrator.settle_stopped()
    db.transition(rid, "approved", "retry", reset=True)
    return rid


def test_a_retry_records_the_reel_that_already_went_live(env, graph, monkeypatch):
    from pipeline import orchestrator, publish_instagram
    db, _ = env
    answers, asked = graph
    rid = retried(db, "instagram", "c1")
    answers["/c1"] = {"id": "c1", "status_code": "PUBLISHED"}
    answers["/7/media"] = {"data": [
        {"id": "m9", "caption": "Another video", "permalink": "https://instagram.com/reel/other", "timestamp": "2099-01-01T00:00:00+0000"},
        {"id": "m2", "caption": CAPTION, "permalink": "https://instagram.com/reel/new", "timestamp": "2099-01-01T00:00:00+0000"},
        {"id": "m1", "caption": CAPTION, "permalink": "https://instagram.com/reel/old", "timestamp": "2000-01-01T00:00:00+0000"}]}
    monkeypatch.setattr(publish_instagram, "publish", lambda *a: pytest.fail("the reel is live: no second upload"))
    monkeypatch.setattr(publish_instagram, "publish_facebook", lambda *a: "live")
    assert orchestrator.publish_run(rid) is True
    post = statuses(db, rid)["instagram"]
    assert (post["status"], post["remote_id"], post["url"]) == ("live", "m2", "https://instagram.com/reel/new")
    assert "nothing was uploaded again" in post["message"] and asked == ["c1", "media"]
    assert db.get_run(rid)["stage"] == "published"


@pytest.mark.parametrize("container,reels,uploads", [
    ({"id": "c1", "status_code": "FINISHED"}, [], True),          # processed but never published
    ({"id": "c1", "status_code": "EXPIRED"}, [], True),
    ({"error": {"code": 100, "message": "Object with ID 'c1' does not exist"}}, [], True),
    ({"error": {"code": 100, "message": "Object with ID 'c1' does not exist"}},
     [{"id": "m2", "caption": CAPTION, "timestamp": "2099-01-01T00:00:00+0000"}], False),   # gone, but the reel is there
    ({"id": "c1", "status_code": "PUBLISHED"}, [], False),         # live, though not among the recent reels
])
def test_a_retry_uploads_again_only_when_nothing_went_live(env, graph, monkeypatch, container, reels, uploads):
    from pipeline import orchestrator, publish_instagram
    db, _ = env
    answers, _ = graph
    rid = retried(db, "instagram", "c1")
    answers["/c1"], answers["/7/media"] = container, {"data": reels}
    calls = []
    monkeypatch.setattr(publish_instagram, "publish", lambda *a: calls.append("instagram") or "live")
    monkeypatch.setattr(publish_instagram, "publish_facebook", lambda *a: "live")
    assert orchestrator.publish_run(rid) is True
    assert (calls == ["instagram"]) is uploads


@pytest.mark.parametrize("status,already", [
    ({"video_status": "ready", "publishing_phase": {"status": "complete", "publish_status": "published"}}, True),
    ({"video_status": "upload_complete", "publishing_phase": {"status": "not_started"}}, False),
])
def test_a_facebook_retry_follows_the_same_rule(env, graph, monkeypatch, status, already):
    from pipeline import orchestrator, publish_instagram
    db, _ = env
    answers, _ = graph
    rid = retried(db, "facebook", "v1")
    answers["/v1"] = {"status": status, "permalink_url": "/reel/v1/"}
    calls = []
    monkeypatch.setattr(publish_instagram, "publish", lambda *a: "live")
    monkeypatch.setattr(publish_instagram, "publish_facebook", lambda *a: calls.append("facebook") or "live")
    assert orchestrator.publish_run(rid) is True
    assert (calls == []) is already
    if already:
        post = statuses(db, rid)["facebook"]
        assert (post["status"], post["remote_id"], post["url"]) == ("live", "v1", "https://www.facebook.com/reel/v1/")


def test_a_retry_that_cannot_ask_fails_without_uploading(env, graph, monkeypatch):
    """Meta refuses the question (the block of 2026-10-10): the platform fails with that error; it is
    not taken as "nothing went live", so nothing is uploaded a second time."""
    from pipeline import orchestrator, publish_instagram
    from pipeline.errors import explain_error
    db, _ = env
    answers, _ = graph
    rid = retried(db, "instagram", "c1")
    answers["/c1"] = BLOCKED
    monkeypatch.setattr(publish_instagram, "publish", lambda *a: pytest.fail("no upload without an answer"))
    monkeypatch.setattr(publish_instagram, "publish_facebook", lambda *a: "live")
    orchestrator.publish_run(rid)
    post = statuses(db, rid)["instagram"]
    assert post["status"] == "failed" and "Cannot call API for app" in post["message"]
    assert explain_error(post["message"], "publish", "Instagram")["sentence"].startswith("Instagram upload failed: Meta is refusing")


def test_test_mode_and_first_uploads_ask_nothing(env, graph, pinned_config):
    from pipeline import posted
    db, _ = env
    _, asked = graph
    rid = publishing(db)
    assert posted.earlier_attempt_live(rid, "instagram", {}) is None          # never tried: nothing to ask about
    db.upsert_post(rid, "instagram", "failed", message="BLOCKED: token missing")   # failed before any container
    assert posted.earlier_attempt_live(rid, "instagram", {}) is None
    db.upsert_post(rid, "instagram", "failed", remote_id="c1")
    pinned_config["dry_run"]["instagram"] = True
    assert posted.earlier_attempt_live(rid, "instagram", {}) is None and asked == []
