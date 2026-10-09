"""Leaving test mode: the read-only checks decide, old test-mode approvals go back to review first,
and nothing posts live without a fresh approval. Graph API and Bot API are faked (no network)."""
import json

import pytest
import yaml

FLOW = ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]


@pytest.fixture
def env(temp_db, tmp_path, monkeypatch, pinned_config):
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"))
    return temp_db


def run_at(db, stage, topic="inflation", platforms=None, approved_by=None):
    from pipeline.config import run_dir
    rid = db.create_run(topic)
    for st in FLOW + ["approved", "publishing", "published"]:
        db.transition(rid, st, **({"platforms": json.dumps(platforms)} if st == "approved" and platforms else {}))
        if st == stage:
            break
    if approved_by:
        with db.db() as c:
            c.execute("UPDATE runs SET approved_by=? WHERE id=?", (approved_by, rid))
    (run_dir(rid) / "meta.json").write_text(json.dumps({"title": f"About {topic}"}))
    return rid


def test_test_mode_flip_resets_old_approvals(env):
    from pipeline import review
    db = env
    dry = run_at(db, "published", "air_passengers", platforms=["youtube", "instagram"])
    db.upsert_post(dry, "youtube", "dry_run")
    db.upsert_post(dry, "instagram", "dry_run")
    approved = run_at(db, "approved", "landlines")
    failed = run_at(db, "publishing", "nuclear_share")
    db.upsert_post(failed, "instagram", "failed", message="boom")
    db.fail(failed, "publish", "all platforms failed")
    live = run_at(db, "published", "homicide_rate")
    db.upsert_post(live, "youtube", "live", remote_id="yt1")
    rejected = run_at(db, "awaiting_approval", "life_expectancy")
    db.transition(rejected, "rejected")
    waiting = run_at(db, "awaiting_approval", "gdp_growth")

    reset = review.reset_test_approvals("go-live")

    assert [r["id"] for r in reset] == [dry, approved, failed]
    assert {r["was"] for r in reset} == {"published", "approved", "failed"}
    for rid in (dry, approved, failed):
        run = db.get_run(rid)
        assert run["stage"] == "awaiting_approval" and run["platforms"] is None and run["approved_by"] is None
        assert db.get_posts(rid) == []                   # no test-mode record can stand in for a live post
        assert any("needs a fresh approval" in e["message"] for e in db.get_log(rid))
    assert db.get_run(live)["stage"] == "published" and db.get_posts(live)[0]["status"] == "live"
    assert db.get_run(rejected)["stage"] == "rejected"
    assert [r["id"] for r in review.queue()] == [waiting, dry, approved, failed]   # back of the queue, oldest first
    assert review.reset_test_approvals("again") == []


@pytest.mark.parametrize("ig,fb,live", [("ok", "ok", ["instagram", "facebook"]), ("ok", "error", ["instagram"]),
                                        ("error", "ok", ["facebook"]), ("error", "error", [])])
def test_go_live_turns_on_only_what_passed_and_resets_first(env, monkeypatch, ig, fb, live):
    from pipeline import golive, review, settings
    db = env
    old = run_at(db, "published")
    db.upsert_post(old, "instagram", "dry_run")
    checks = {"instagram": {"state": ig, "detail": "ig", "missing": []},
              "facebook": {"state": fb, "detail": "fb: HTTP 400 {'message': 'Invalid OAuth', 'code': 190}", "missing": []}}
    monkeypatch.setattr(golive, "check", lambda: checks)
    order = []
    monkeypatch.setattr(review, "reset_test_approvals",
                        lambda by, real=review.reset_test_approvals: order.append("reset") or real(by))
    monkeypatch.setattr(settings, "go_live", lambda passed: order.append(("config", list(passed))))
    report = golive.run()
    assert report["passed"] == live
    if live:
        assert order == ["reset", ("config", live)]       # approvals are reset BEFORE test mode goes off
        assert [r["id"] for r in report["reset"]] == [old]
        assert db.get_run(old)["stage"] == "awaiting_approval"
    else:
        assert order == [] and not report["changed"]      # both failed: nothing changes
        assert db.get_run(old)["stage"] == "published"
    assert golive.run(apply=False)["changed"] is False


def test_go_live_config_edit(tmp_path, monkeypatch):
    from pipeline import config, settings
    source = ("# owner comment\nyoutube: {enabled: true}\ninstagram: {enabled: true}\nfacebook: {enabled: false}\n"
              "dry_run: {youtube: true, instagram: true, facebook: true}\n")
    (tmp_path / "config.yaml").write_text(source)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    settings.go_live(["instagram", "facebook"])
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["youtube"]["enabled"] is False and cfg["dry_run"]["youtube"] is True   # off, still safe if turned on
    assert cfg["instagram"]["enabled"] and cfg["facebook"]["enabled"]
    assert cfg["dry_run"]["instagram"] is False and cfg["dry_run"]["facebook"] is False
    assert "# owner comment" in (tmp_path / "config.yaml").read_text()
    (tmp_path / "config.yaml").write_text(source)
    settings.go_live(["instagram"])
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["facebook"]["enabled"] is False and cfg["dry_run"]["facebook"] is True


@pytest.mark.parametrize("name,indent", [("config.yaml", "config"), ("topics.yaml", None)])
def test_yaml_writes_keep_the_file_layout(tmp_path, name, indent):
    """A Settings save or go-live changes values only: no re-wrapping, no re-indenting, LF endings."""
    from pipeline import config, settings
    original = (config.ROOT / name).read_bytes().replace(b"\r\n", b"\n")
    (tmp_path / name).write_bytes(original)
    settings.update_yaml(tmp_path / name, lambda document: None, settings.CONFIG_INDENT if indent else None)
    assert (tmp_path / name).read_bytes() == original


def test_an_old_approval_never_posts_live(env, monkeypatch, pinned_config):
    """A run approved by out-of-date code (no approved_by stamp) goes back to review instead of posting."""
    from pipeline import accounts, orchestrator, publish_instagram
    db = env
    pinned_config["dry_run"]["instagram"] = False
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram"])
    monkeypatch.setattr(publish_instagram, "publish", lambda *a: pytest.fail("must not post"))
    rid = run_at(db, "approved")
    assert orchestrator.publish_run(rid) is False
    assert db.get_run(rid)["stage"] == "awaiting_approval"
    assert any("fresh approval" in e["message"] for e in db.get_log(rid))


def test_a_test_mode_record_does_not_block_a_live_upload(env, pinned_config):
    db = env
    rid = run_at(db, "approved")
    db.upsert_post(rid, "instagram", "dry_run")
    assert db.already_posted(rid, "instagram", dry_run=True) == "dry_run"
    assert db.already_posted(rid, "instagram", dry_run=False) is None
    db.upsert_post(rid, "instagram", "live")
    assert db.already_posted(rid, "instagram", dry_run=False) == "live"


def test_youtube_off_skips_upload(env, monkeypatch, pinned_config):
    from pipeline import accounts, actions, orchestrator, publish_instagram, publish_youtube
    db = env
    monkeypatch.setattr(actions, "spawn", lambda *a: 0)
    rid = run_at(db, "awaiting_approval")
    actions.approve(rid, "telegram")
    assert json.loads(db.get_run(rid)["platforms"]) == ["youtube", "instagram"]   # chosen while YouTube was on
    pinned_config["youtube"]["enabled"] = False
    pinned_config["dry_run"]["instagram"] = False
    states = accounts.account_states()
    for s in states.values():
        s["connected"] = True
    monkeypatch.setattr(accounts, "account_states", lambda: states)
    monkeypatch.setattr(publish_youtube, "publish", lambda *a: pytest.fail("YouTube is off: no upload"))
    calls = []
    monkeypatch.setattr(publish_instagram, "publish", lambda *a: calls.append("instagram") or "live")
    assert accounts.enabled_platforms() == ["instagram"]
    assert orchestrator.publish_run(rid) is True and calls == ["instagram"]
    assert db.get_run(rid)["stage"] == "published"


class GraphResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


@pytest.mark.parametrize("scopes,token_type,ok,missing", [
    (["pages_show_list", "pages_read_engagement", "pages_manage_posts", "instagram_basic"], "PAGE", True, []),
    (["pages_show_list", "pages_read_engagement"], "PAGE", False, ["pages_manage_posts"]),
    (["pages_show_list", "pages_read_engagement", "pages_manage_posts"], "USER", False, []),
])
def test_facebook_check_names_a_missing_permission(monkeypatch, scopes, token_type, ok, missing):
    from pipeline import publish_instagram as pi
    monkeypatch.setenv("FB_PAGE_ID", "42")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    posted = []
    monkeypatch.setattr(pi.requests, "post", lambda *a, **k: posted.append(a))

    def get(url, params=None, timeout=None):
        if url.endswith("/debug_token"):
            return GraphResponse({"data": {"is_valid": True, "type": token_type, "profile_id": "42", "scopes": scopes}})
        return GraphResponse({"id": "42", "name": "Graphony"})
    monkeypatch.setattr(pi.requests, "get", get)
    off = pi.check_facebook()                                  # facebook is off in the pinned config
    assert off["state"] == "disabled"
    res = pi.check_facebook(force=True)
    assert (res["state"] == "ok") is ok and res["missing"] == missing and posted == []   # read-only
    if missing:
        assert "missing permissions: pages_manage_posts" in res["detail"]
    if token_type == "USER":
        assert "Page access token" in res["detail"]


def test_dashboard_banner_follows_the_real_test_mode(pinned_config):
    from dashboard.viewmodels import status_line
    from pipeline.accounts import test_mode
    accounts = {p: {"connected": True} for p in ("youtube", "instagram", "facebook")}
    assert status_line(False, {"state": "ok"}, 0, ["youtube", "instagram"], pinned_config["dry_run"], accounts)["text"] \
        == "Test mode — nothing is actually posted"
    assert test_mode()["on"] is True
    pinned_config["youtube"]["enabled"] = False
    pinned_config["facebook"]["enabled"] = True
    pinned_config["dry_run"].update(instagram=False, facebook=False)
    assert test_mode() == {"on": False, "testing": [], "live": ["instagram", "facebook"], "off": ["youtube"]}
    assert status_line(False, {"state": "ok"}, 0, ["instagram", "facebook"], pinned_config["dry_run"], accounts)["text"] \
        == "All good"
    pinned_config["dry_run"]["facebook"] = True
    assert status_line(False, {"state": "ok"}, 0, ["instagram", "facebook"], pinned_config["dry_run"], accounts)["text"] \
        == "Test mode on for Facebook — not actually posted there"
