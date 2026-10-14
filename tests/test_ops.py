"""Gemini model fallback, stats inside the bot, /account, and token-free Meta errors. No network."""
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests


class Resp:
    def __init__(self, status, data):
        self.status_code, self.data, self.text = status, data, json.dumps(data)

    def json(self):
        return self.data


def ok(obj):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]})


@pytest.fixture
def gem(monkeypatch, pinned_config):
    from pipeline import gemini
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    pinned_config["gemini"]["timeout"] = 5          # the model order is the owner's config.yaml
    calls, answers = [], {}

    def post(url, json=None, headers=None, timeout=None):
        model = url.split("/models/")[1].split(":")[0]
        calls.append(model)
        a = answers[model]
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(gemini.requests, "post", post)
    return gemini, calls, answers


def test_the_owner_config_tries_2_5_flash_first_then_3_8(gem):
    gemini = gem[0]
    assert gemini.models() == ["gemini-2.5-flash", "gemini-3.8-flash"]


@pytest.mark.parametrize("failure", [Resp(503, {"error": {"status": "UNAVAILABLE", "message": "high demand"}}),
                                     Resp(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "per day"}}),
                                     requests.Timeout("read timed out")])
def test_an_overloaded_model_hands_over_to_the_fallback_at_once(gem, caplog, failure):
    gemini, calls, answers = gem
    answers.update({"gemini-2.5-flash": failure, "gemini-3.8-flash": ok({"label": "x"})})
    caplog.set_level("INFO")
    reply, model = gemini.ask_json("prompt", what="label test")
    assert reply == {"label": "x"} and model == "gemini-3.8-flash"
    assert calls == ["gemini-2.5-flash", "gemini-3.8-flash"]          # one try each: no retries on the busy one
    assert "label test: served by gemini-3.8-flash" in caplog.text


def test_the_primary_serves_when_it_can_and_nothing_answering_raises(gem):
    gemini, calls, answers = gem
    answers.update({"gemini-2.5-flash": ok({"a": 1}), "gemini-3.8-flash": ok({"b": 2})})
    assert gemini.ask_json("p") == ({"a": 1}, "gemini-2.5-flash") and calls == ["gemini-2.5-flash"]
    calls.clear()
    answers.update({"gemini-2.5-flash": Resp(503, {}), "gemini-3.8-flash": Resp(500, {})})
    with pytest.raises(gemini.QuotaExhausted, match="no Gemini model answered"):
        gemini.ask_json("p")
    assert calls == ["gemini-2.5-flash", "gemini-3.8-flash"]
    calls.clear()
    answers["gemini-2.5-flash"] = Resp(400, {"error": {"message": "bad request"}})
    with pytest.raises(RuntimeError, match="HTTP 400"):           # a refused request is not retried elsewhere
        gemini.ask_json("p")
    assert calls == ["gemini-2.5-flash"]


def test_labels_go_through_the_shared_helper_and_record_the_model(gem, tmp_path, pinned_config):
    from pipeline import labels
    gemini, calls, answers = gem
    pinned_config["paths"]["cache"] = str(tmp_path / "cache")
    answers.update({"gemini-2.5-flash": Resp(503, {}),
                    "gemini-3.8-flash": ok({"label": "Oil price crash", "event_year": 2014, "confident": True})})
    years = list(range(2000, 2021))
    vals = [10.0 + 0.1 * k for k in range(14)] + [2.0] + [2.0 + 0.1 * k for k in range(6)]
    out = labels.label_run({"id": "t", "start_year": 2000, "subtitle": "x"}, ["NOR"], {"NOR": list(zip(years, vals))},
                           "x", {"NOR": "Norway"})
    assert out["NOR"]["label"] == "Oil price crash" and out["NOR"]["model"] == "gemini-3.8-flash"


# ------------------------------------------------------------------ stats in the bot

def test_stats_run_in_the_bot_every_6_hours_and_summarise_once_a_day(temp_db, monkeypatch):
    from pipeline import analytics
    runs, summaries = [], []
    monkeypatch.setattr(analytics, "fetch_youtube", lambda: runs.append("yt") or 0)
    monkeypatch.setattr(analytics, "fetch_instagram", lambda: runs.append("ig") or 4)
    monkeypatch.setattr(analytics, "send_summary", lambda: summaries.append(1) or temp_db.kv_set(
        analytics.K_LAST_SUMMARY, temp_db.now()))
    now = datetime.now(timezone.utc)
    assert analytics.tick(now, background=False) is True                # never run before: runs now
    assert runs == ["yt", "ig"] and summaries == [1]
    assert analytics.tick(now + timedelta(hours=1), background=False) is False
    assert analytics.tick(now + timedelta(hours=6, minutes=1), background=False) is True
    assert len(runs) == 4 and summaries == [1]                        # collected again, no second summary today
    assert temp_db.kv_get(analytics.K_LAST_RUN)


def test_instagram_stats_land_with_reels_metrics_and_watch_percent(temp_db, monkeypatch, tmp_path, pinned_config):
    from pipeline import analytics, meta_api
    from pipeline.config import run_dir
    pinned_config["paths"]["out"] = str(tmp_path / "out")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    rid = temp_db.create_run("homicide_rate")
    (run_dir(rid) / "meta.json").write_text(json.dumps({"duration_seconds": 40.0}))
    temp_db.upsert_post(rid, "instagram", "live", remote_id="m1")
    asked = []

    def get(url, params=None, timeout=None):
        asked.append(params["metric"])
        data = [{"name": "views", "values": [{"value": 133}]}, {"name": "reach", "values": [{"value": 117}]},
                {"name": "ig_reels_avg_watch_time", "values": [{"value": 12000}]},
                {"name": "reels_skip_rate", "values": [{"value": 53.4}]}]
        return Resp(200, {"data": data})

    monkeypatch.setattr(meta_api, "get", get)
    assert analytics.fetch_instagram() == 1
    assert "reels_skip_rate" in asked[0]
    row = temp_db.latest_stats()[0]
    assert (row["views"], row["avg_view_pct"]) == (133, 30.0)
    with temp_db.db() as c:
        raw = c.execute("SELECT raw FROM stats").fetchone()["raw"]
    assert json.loads(raw)["reels_skip_rate"] == 53.4


# ------------------------------------------------------------------ /account and token-free errors

def test_account_reads_the_accounts_live_by_id(monkeypatch):
    from pipeline import approve_telegram as tg, meta_api
    monkeypatch.setenv("IG_ACCESS_TOKEN", "SECRET-TOKEN")
    monkeypatch.setenv("IG_USER_ID", "1784")
    monkeypatch.setenv("FB_PAGE_ID", "1294")
    seen = []

    def get(url, params=None, timeout=None):
        seen.append((url.rsplit("/", 1)[-1], params["fields"]))
        if url.endswith("/1784"):
            return Resp(200, {"username": "graphony", "name": "Graphony", "followers_count": 12, "media_count": 33})
        return Resp(200, {"name": "Graphony", "followers_count": 3, "fan_count": 2})

    monkeypatch.setattr(meta_api, "get", get)
    text = tg.account_text()
    assert seen == [("1784", "username,name,followers_count,media_count"), ("1294", "name,followers_count,fan_count")]
    assert "@graphony · Graphony" in text and "12 followers · 33 posts" in text and "Facebook Page: Graphony · 3 followers" in text
    assert "SECRET-TOKEN" not in text and "1784" not in text and "1294" not in text


def test_a_network_error_never_carries_the_token(monkeypatch):
    from pipeline import approve_telegram as tg, meta_api
    monkeypatch.setenv("IG_ACCESS_TOKEN", "SECRET-TOKEN")
    monkeypatch.setenv("IG_USER_ID", "1784")
    monkeypatch.delenv("FB_PAGE_ID", raising=False)

    def boom(url, **kw):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}?fields=x&access_token=SECRET-TOKEN")

    monkeypatch.setattr(meta_api.requests, "get", boom)
    with pytest.raises(requests.ConnectionError) as e:
        meta_api.get("https://graph.facebook.com/v25.0/1784", params={"access_token": "SECRET-TOKEN"})
    assert "SECRET-TOKEN" not in str(e.value) and "access_token=***" in str(e.value)
    text = tg.account_text()
    assert "SECRET-TOKEN" not in text and "could not read it" in text


def test_account_is_a_button_on_the_home_card(temp_db):
    from pipeline import home
    rows = home.home_view()[1]["inline_keyboard"]
    assert {"text": "👤 Account", "callback_data": "h:account"} in [b for row in rows for b in row]
    text, markup = home.account_view({"name": "account", "text": "👤 Connected accounts (live from Meta)"})
    assert text.startswith("👤 Connected accounts") and markup["inline_keyboard"][-1] == [home.HOME]
