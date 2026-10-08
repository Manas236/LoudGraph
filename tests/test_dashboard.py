"""Owner workflow regression tests; all state and media are isolated from the real database."""
import copy
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
import yaml

FLOW = ["data_ready", "picked", "labelled", "rendered", "awaiting_approval", "approved", "publishing", "published"]


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    from dashboard import app as web, viewmodels
    from pipeline import accounts, actions, config, health, orchestrator
    cfg = copy.deepcopy(config.get_config())
    for key in ("out", "cache", "tokens"):
        cfg["paths"][key] = str(tmp_path / key)
    for module in (web, viewmodels, accounts, actions, config, health, orchestrator):
        monkeypatch.setattr(module, "get_config", lambda: cfg)
    for name in config.SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web.app, "testing", True)
    return web, web.app.test_client()


def waiting(db, topic="air_passengers", stage="awaiting_approval"):
    rid = db.create_run(topic)
    for st in FLOW[:FLOW.index(stage) + 1]:
        db.transition(rid, st, countries=["IND", "USA"])
    return db.get_run(rid)


def post(c, web, run, operation, **values):
    return c.post(f"/video/{run['ref']}/{operation}", data={"csrf": web.CSRF, **values},
                  headers={"Accept": "application/json"})


def test_status_line_priority():
    from dashboard.viewmodels import status_line
    accounts = {"youtube": {"connected": False}, "instagram": {"connected": False}}
    def line(tg=False, beat="ok", failed=0, dry=True):
        return status_line(tg, {"state": beat}, failed, list(accounts), dict(youtube=dry, instagram=dry), accounts)
    assert line(True, "stale", 2)["text"].startswith("Bot is stopped")
    assert line(True, "never", 2)["tone"] == "red"
    assert line(False, "never", 2)["text"] == "2 videos failed to post — see Library"
    assert line()["text"] == "Test mode — nothing is actually posted"
    assert line(dry=False)["text"] == "2 accounts not connected — Settings"
    accounts["youtube"]["connected"] = accounts["instagram"]["connected"] = True
    assert line(dry=False) == {"tone": "green", "text": "All good", "href": "/settings#accounts"}
    assert status_line(False, {"state": "never"}, 0, [], {}, accounts)["text"] == "All good"


def test_review_shows_video_queue_and_clear_actions_without_ids(client):
    web, c = client
    from pipeline import db
    first = waiting(db)
    second = waiting(db, "homicide_rate")
    html = c.get("/").text
    assert "1 of" not in html or "waiting" in html
    assert "of 2 waiting" in html and "Approve (posts once accounts are connected)" in html
    assert 'data-action="remake"' in html and 'data-action="reject"' in html
    assert "Passengers flown by each country" in html and "United States" in html
    assert "Nothing here" not in html and "heartbeat" not in html
    assert not re.search(r"r\d{8}-\d{6}-[0-9a-f]{4}|air_passengers|homicide_rate", html)
    assert len(re.findall(r'class="status status-', html)) == 1
    chosen = c.get(f"/fragment/review?selected={second['ref']}").text
    assert f'data-current="{second["ref"]}"' in chosen
    assert c.get("/api/review").json["waiting"] == [first["ref"], second["ref"]]


def test_no_accounts_approval_stays_in_library_and_never_calls_publisher(client, monkeypatch):
    web, c = client
    from pipeline import actions, db, orchestrator
    calls = []
    monkeypatch.setattr(actions, "spawn", lambda *args: calls.append(args))
    run = waiting(db)
    response = post(c, web, run, "approve")
    assert response.status_code == 200 and "waiting in Library" in response.json["message"]
    assert db.get_run(run["id"])["stage"] == "approved" and calls == []
    assert orchestrator.publish_run(run["id"]) is False
    html = c.get("/library?tab=approved").text
    assert "Approved, not posted" in html and 'class="library-card"' in html
    assert not re.search(r"r\d{8}-\d{6}-[0-9a-f]{4}|air_passengers", html)


def test_title_edit_persists_exact_payload_consumed_by_publishers(client, monkeypatch):
    web, c = client
    from pipeline import accounts, db, orchestrator, publish_instagram, publish_youtube
    from pipeline.config import run_dir
    run = waiting(db)
    folder = run_dir(run["id"])
    (folder / "meta.json").write_text(json.dumps({"title": "Old title", "description_youtube": "Old title. Data credit stays.",
                                                 "description_instagram": "Old title. Data credit stays.", "tags": []}))
    assert post(c, web, run, "title", title="A much better hook").status_code == 200
    assert db.get_run(run["id"])["title"] == "A much better hook"
    seen = []
    def publish(rid, meta, file):
        seen.append(meta)
        return "dry_run"
    monkeypatch.setattr(accounts, "ready_destinations", lambda r: ["youtube", "instagram"])
    monkeypatch.setattr(publish_youtube, "publish", publish)
    monkeypatch.setattr(publish_instagram, "publish", publish)
    db.transition(run["id"], "approved")
    assert orchestrator.publish_run(run["id"])
    assert len(seen) == 2
    assert all(m["title"] == "A much better hook" for m in seen)
    assert all(m["description_instagram"] == "A much better hook. Data credit stays." for m in seen)
    assert publish_youtube._body(seen[0])["snippet"]["title"] == "A much better hook"
    assert post(c, web, run, "title", title="Too late").status_code == 409


def test_video_range_response_and_whitelist(client):
    web, c = client
    from pipeline import db
    from pipeline.config import run_dir
    run = waiting(db)
    data = bytes(range(256)) * 20
    (run_dir(run["id"]) / "video.mp4").write_bytes(data)
    response = c.get(f"/video/{run['ref']}/media/video.mp4", headers={"Range": "bytes=200-499"})
    assert response.status_code == 206 and response.data == data[200:500]
    assert response.headers["Content-Range"] == f"bytes 200-499/{len(data)}"
    assert c.get(f"/video/{run['ref']}/media/meta.json").status_code == 404
    assert c.get(f"/video/{run['ref']}/media/../../config.yaml").status_code == 404


def test_make_retry_reject_remake_use_shared_actions(client, monkeypatch):
    web, c = client
    from pipeline import actions, db
    calls = []
    monkeypatch.setattr(actions, "spawn", lambda *args: calls.append(args) or 0)
    assert c.post("/make").status_code == 403
    assert c.post("/make", data={"csrf": web.CSRF}).status_code == 302
    c.post("/make", data={"csrf": web.CSRF})
    assert calls == [("produce", "--count", "1")]
    run = waiting(db)
    assert post(c, web, run, "reject").status_code == 200
    assert db.get_run(run["id"])["stage"] == "rejected"
    assert any("via dashboard" in e["message"] for e in db.get_log(run["id"]))
    run = waiting(db)
    assert post(c, web, run, "remake").status_code == 200
    assert db.get_run(run["id"])["replaced"] == 1
    new = next(r for r in db.list_runs() if r["regen_of"] == run["id"])
    assert new["topic_id"] == run["topic_id"]
    assert calls[-1] == ("produce", "--run", new["id"], "--from", "fetch")
    db.fail(new["id"], "render", "ffmpeg crashed")
    assert post(c, web, new, "retry").status_code == 200
    assert calls[-1] == ("produce", "--run", new["id"], "--from", "render")


def test_progress_and_single_attention_with_dismiss(client):
    web, c = client
    from pipeline import db
    run = waiting(db, stage="labelled")
    db.set_progress(run["id"], .62)
    html = c.get("/").text
    assert "Making a video" in html and "Air passengers" in html and "rendering 62%" in html
    db.fail(run["id"], "render", "RuntimeError: ffmpeg crashed")
    other = waiting(db)
    db.fail(other["id"], "notify", "unexpected")
    html = c.get("/").text
    assert html.count('aria-label="Needs attention"') == 1
    from dashboard.viewmodels import attention
    item = attention()
    assert c.post('/attention/dismiss', data={"csrf": web.CSRF, "key": item["key"], "version": item["version"]}).status_code == 302
    assert attention()["key"] != item["key"]


def test_failed_platform_counts_video_once_and_drawer_retry(client, monkeypatch):
    web, c = client
    from pipeline import accounts, actions, db
    calls = []
    monkeypatch.setattr(actions, "spawn", lambda *a: calls.append(a))
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["youtube", "instagram"])
    run = waiting(db, stage="published")
    db.upsert_post(run["id"], "youtube", "live", url="https://youtube.com/shorts/example")
    db.upsert_post(run["id"], "instagram", "failed", message="token expired")
    response = c.get("/library")
    assert "1 video failed to post" in response.text and "YouTube · live" in response.text
    html = c.get(f"/video/{run['ref']}/drawer").text
    assert "Instagram upload failed: the access token expired." in html and "Try again" in html
    assert '<summary>Technical details</summary>' in html
    assert post(c, web, run, "retry", platform="instagram").status_code == 200
    assert calls == [("publish", "--run", run["id"], "--platform", "instagram")]


@pytest.mark.parametrize("url", ["/", "/library", "/library?tab=approved", "/library?tab=rejected", "/settings", "/api/review"])
def test_pages_render(client, url):
    assert client[1].get(url).status_code == 200


@pytest.mark.parametrize("url", ["/make", "/settings/posting", "/settings/topics/inflation", "/settings/accounts/youtube/test", "/attention/dismiss", "/video/1/approve", "/video/1/title"])
def test_every_post_requires_csrf(client, url):
    assert client[1].post(url).status_code == 403


def test_test_button_checks_only_requested_service(client, monkeypatch):
    web, c = client
    from pipeline import publish_youtube, publish_instagram
    calls = []
    monkeypatch.setattr(publish_youtube, "check", lambda: calls.append("youtube") or {"state": "ok", "level": "OK", "detail": "connected"})
    monkeypatch.setattr(publish_instagram, "check", lambda: pytest.fail("unrelated account check"))
    response = c.post("/settings/accounts/youtube/test", data={"csrf": web.CSRF}, headers={"Accept": "application/json"})
    assert calls == ["youtube"] and response.json["message"] == "Connected ✓"


def test_refuses_public_bind(monkeypatch):
    from dashboard import app as web
    cfg = dict(web.get_config(), dashboard={"host": "0.0.0.0", "port": 5055})
    monkeypatch.setattr(web, "get_config", lambda: cfg)
    with pytest.raises(SystemExit, match="loopback only"):
        web.main()


def test_platform_choices_survive_approval_and_only_selected_platform_posts(client, monkeypatch):
    web, c = client
    from pipeline import accounts, actions, db, orchestrator, publish_instagram, publish_youtube
    from pipeline.config import run_dir
    states = accounts.account_states()
    for state in states.values():
        state["connected"] = True
    monkeypatch.setattr(accounts, "account_states", lambda: states)
    spawned = []
    monkeypatch.setattr(actions, "spawn", lambda *a: spawned.append(a))
    run = waiting(db)
    (run_dir(run["id"]) / "meta.json").write_text('{"title":"Test title"}')
    response = post(c, web, run, "approve", platforms_present="1", platform="instagram")
    assert response.json["message"] == "Approved — posting to Instagram"
    assert json.loads(db.get_run(run["id"])["platforms"]) == ["instagram"]
    assert spawned == [("publish", "--run", run["id"])]
    monkeypatch.setattr(publish_youtube, "publish", lambda *a: pytest.fail("Unselected YouTube must not post"))
    calls = []
    monkeypatch.setattr(publish_instagram, "publish", lambda *a: calls.append("instagram") or "live")
    assert orchestrator.publish_run(run["id"]) and calls == ["instagram"]


def test_html_never_contains_ids_or_slugs_for_any_library_tab(client):
    web, c = client
    from pipeline import db
    posted = waiting(db, "homicide_rate", "published")
    db.upsert_post(posted["id"], "youtube", "live")
    waiting(db, "life_expectancy", "approved")
    rejected = waiting(db, "nuclear_share")
    db.transition(rejected["id"], "rejected")
    for url in ("/", "/library", "/library?tab=approved", "/library?tab=rejected"):
        assert not re.search(r"r\d{8}-\d{6}-[0-9a-f]{4}|homicide_rate|life_expectancy|nuclear_share", c.get(url).text)


def test_failures_link_includes_a_partially_posted_video(client):
    web, c = client
    from pipeline import db
    run = waiting(db, stage="published")
    db.upsert_post(run["id"], "youtube", "live")
    db.upsert_post(run["id"], "instagram", "failed", message="network error")
    html = c.get("/library?tab=approved&failed=1").text
    assert f'data-drawer="{run["ref"]}"' in html


def test_approved_metadata_read_failure_is_actionable(client, monkeypatch):
    from pipeline import accounts, db, orchestrator
    run = waiting(db, stage="approved")
    monkeypatch.setattr(accounts, "ready_destinations", lambda r: ["youtube"])
    assert not orchestrator.publish_run(run["id"])
    assert db.get_run(run["id"])["failed_stage"] == "publish"


def test_selection_attention_is_shown_once_and_can_be_dismissed(client):
    web, c = client
    from pipeline import orchestrator
    from dashboard import viewmodels
    orchestrator.selection_attention()
    html = c.get("/").text
    assert html.count('aria-label="Needs attention"') == 1
    assert "interesting topic to make" in html
    item = viewmodels.attention()
    c.post("/attention/dismiss", data={"csrf": web.CSRF, "key": item["key"], "version": item["version"]})
    assert 'aria-label="Needs attention"' not in c.get("/").text


def test_settings_show_where_each_key_goes_and_brand(client):
    web, c = client
    html = c.get("/settings").text
    assert "<title>Settings · Graphony</title>" in html and 'class="brand"' in html and "Graphony" in html
    assert html.count("How to connect") == 5
    for name in ("YT_CLIENT_SECRET_FILE", "IG_USER_ID", "IG_ACCESS_TOKEN", "FB_PAGE_ID", "TELEGRAM_BOT_TOKEN",
                 "TELEGRAM_CHAT_ID", "GEMINI_API_KEY"):
        assert f"<code>{name}</code>" in html, name
    assert "`" not in re.sub(r"<script.*?</script>", "", html, flags=re.S)  # every backtick became <code>
    assert "<code>&lt;TOKEN&gt;</code>" in html  # code spans stay escaped


def test_an_expired_account_is_explained_as_a_connection_not_an_upload(client, monkeypatch):
    web, c = client
    from pipeline import config, db, health
    monkeypatch.setenv("IG_USER_ID", "1")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "fake")
    detail = "IG user lookup: HTTP 400 {'code': 190, 'message': 'Session has expired'}"
    db.kv_set(health.K_CREDS, json.dumps({"checked_at": db.now(), "items": [
        {"name": "Instagram", "level": "FAIL", "state": "expired", "detail": detail}]}))
    html = c.get("/").text
    assert "Instagram is not connected: the access token expired." in html and "upload failed" not in html
    assert html.count('aria-label="Needs attention"') == 1
    assert "Expired — reconnect" in c.get("/settings").text


def test_a_making_card_with_no_progress_for_30_minutes_says_so(client):
    web, c = client
    from pipeline import db
    run = waiting(db, stage="labelled")
    db.set_progress(run["id"], .43)
    assert "rendering 43%" in c.get("/").text and "no progress since" not in c.get("/").text
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.db() as conn:
        conn.execute("UPDATE runs SET updated_at=?, progress_at=? WHERE id=?", (old, old, run["id"]))
    html = c.get("/").text
    assert "rendering 43%" in html and "no progress since 2 hours ago" in html
