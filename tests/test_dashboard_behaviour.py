import json
import sqlite3
from datetime import datetime

import pytest
import yaml


@pytest.mark.parametrize("raw,expected,section", [
    ("invalid_grant: token expired", "access token expired", "accounts"),
    ("quotaExceeded", "daily posting limit", "posting"),
    ("ConnectionError: network down", "internet connection", "advanced"),
    ("media processing failed", "could not process", "posting"),
    ("missing permissions", "missing permission", "accounts"),
    ("ffmpeg exited 1", "video maker stopped", "advanced"),
    ("mysterious thing", "Something went wrong while making the video", "advanced"),
])
def test_error_mapping(raw, expected, section):
    from pipeline.errors import explain_error
    value = explain_error(raw, "render", "Instagram")
    assert expected in value["sentence"] and value["section"] == section
    assert value["fix"] and value["details"] == raw


def test_migration_preserves_two_old_runs_as_skip_events(temp_db):
    db = temp_db
    ids = []
    for reason in ("low variety: no falling country (net < -0.5)", "every country set has already been used"):
        rid = db.create_run("life_expectancy")
        db.fail(rid, "pick", reason)
        ids.append(rid)
    real = db.create_run("inflation")
    db.fail(real, "render", "ffmpeg crashed")
    db.init()
    db.init()  # migration must be repeatable
    assert all(db.get_run(rid)["stage"] == "skipped" for rid in ids)
    assert len(db.topic_events("life_expectancy")) == 2
    assert [r["id"] for r in db.list_runs(stage="failed")] == [real]
    assert all(db.get_log(rid) for rid in ids)


@pytest.mark.parametrize("skip_count", [0, 1, 4, 5])
def test_skip_tries_next_topic_up_to_five(temp_db, monkeypatch, tmp_path, skip_count):
    from pipeline import orchestrator as o, selector, topics
    from pipeline.picker import LowVariety
    db = temp_db
    attempts = []
    def choose(exclude):
        return f"topic{len(exclude)}"
    monkeypatch.setattr(selector, "choose_topic", choose)
    monkeypatch.setattr(topics, "get_topic", lambda tid: {"id": tid})
    monkeypatch.setattr(o, "_telegram_on", lambda: False)
    monkeypatch.setattr(o, "run_dir", lambda rid: tmp_path)
    def pick(rid, topic):
        attempts.append(topic["id"])
        if len(attempts) <= skip_count:
            raise LowVariety("low variety: no falling country")
        return "picked", {}
    monkeypatch.setattr(o, "STEP_FN", {s: (pick if s == "pick" else lambda *a: ("done", {})) for s in o.STEPS})
    done = o.produce(1)
    assert len(attempts) == min(skip_count + 1, 5) and len(set(attempts)) == len(attempts)
    assert len(db.topic_events()) == min(skip_count, 5)
    assert not db.list_runs(stage="failed")
    if skip_count < 5:
        assert len(done) == 1 and not db.kv_get("attention:selection")
    else:
        assert done == []
        assert json.loads(db.kv_get("attention:selection"))["sentence"] == o.SELECTION_MESSAGE
        with db.db() as conn:
            assert conn.execute("SELECT count(*) FROM kv WHERE key='attention:selection'").fetchone()[0] == 1


def test_explicit_topic_skip_falls_back_and_real_failure_does_not_skip(temp_db, monkeypatch):
    from pipeline import orchestrator as o, selector
    seen = []
    monkeypatch.setattr(selector, "choose_topic", lambda exclude: "next")
    def run(rid):
        topic = temp_db.get_run(rid)["topic_id"]
        seen.append(topic)
        if topic == "requested":
            temp_db.skip(rid, "low variety")
        else:
            temp_db.fail(rid, "render", "ffmpeg crash")
        return False
    monkeypatch.setattr(o, "run_pipeline", run)
    assert o.produce(1, "requested") == []
    assert seen == ["requested", "next"]
    assert not temp_db.kv_get("attention:selection")


def test_yaml_writes_backup_validate_and_preserve_comments(tmp_path, monkeypatch):
    from pipeline import config, settings, topics
    configfile = tmp_path / "config.yaml"
    source = "# keep this owner comment\nyoutube: {enabled: true}\ninstagram: {enabled: true}\nfacebook: {enabled: false}\ndry_run: {youtube: true, instagram: true, facebook: true}\ncadence: {videos_per_day: 2}\n"
    configfile.write_text(source)
    topicfile = tmp_path / "topics.yaml"
    topicfile.write_text("# a topic comment\n- id: example\n  name: Example\n  enabled: true\n")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(topics, "TOPICS_FILE", topicfile)
    backup = settings.save_posting({"videos_per_day": "3", "posting_times": "18:00, 08:00", "youtube_enabled": "on", "youtube_dry": "on"})
    assert backup.read_text() == source
    updated = yaml.safe_load(configfile.read_text())
    assert updated["cadence"] == {"videos_per_day": 3, "posting_times": ["08:00", "18:00"]}
    assert updated["youtube"]["enabled"] and not updated["instagram"]["enabled"]
    saved = settings.toggle_topic("example", False)
    assert saved.exists() and yaml.safe_load(topicfile.read_text())[0]["enabled"] is False
    assert "# keep this owner comment" in configfile.read_text()
    assert "# a topic comment" in topicfile.read_text()
    before = configfile.read_bytes()
    with pytest.raises(ValueError):
        settings.save_posting({"videos_per_day": "3", "posting_times": "25:99"})
    assert configfile.read_bytes() == before


def test_posting_schedule_is_daily_and_restart_safe(temp_db, monkeypatch):
    from pipeline import actions, schedule
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(schedule, "get_config", lambda: {"cadence": {"videos_per_day": 3, "posting_times": ["08:00", "18:00"]}})
    calls = []
    monkeypatch.setattr(actions, "spawn", lambda *a: calls.append(a))
    now = datetime(2026, 10, 8, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    schedule.tick(now)
    schedule.tick(now)  # restarting at the same minute cannot make duplicates
    schedule.tick(now.replace(hour=18))
    assert calls == [("produce", "--count", "2"), ("produce", "--count", "1")]
    assert schedule.next_time(now).hour == 18


def test_app_py_runs_directly_from_any_folder(tmp_path):
    """`python dashboard/app.py` must import without run.py; checked from an unrelated working directory."""
    import subprocess
    import sys
    from pipeline.config import ROOT
    script = ("import runpy; g = runpy.run_path(r'%s', run_name='direct'); "
              "c = g['app'].test_client(); r = c.get('/ping'); print(r.status_code, r.text)") % (ROOT / "dashboard" / "app.py")
    out = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("200 graphony-dashboard")


def test_how_to_connect_names_every_env_variable_in_plain_steps():
    from dashboard import setup_steps
    from pipeline.config import SECRET_NAMES
    services = setup_steps.SERVICES
    assert set(services) == {"youtube", "instagram", "facebook", "telegram", "gemini"}
    assert sorted(n for s in services.values() for n in s["fills"]) == sorted(SECRET_NAMES)
    for key, service in services.items():
        text = " ".join(setup_steps.steps(key))
        assert all(f"`{name}" in text for name in service["fills"]), key
        assert "{folder}" not in text and "Test" in text
    youtube = " ".join(setup_steps.steps("youtube"))
    for needed in ("In production", "audit", "yt_api_form", "terminal window of its own",
                   "python -m pipeline.publish_youtube --auth", "client_secret.json"):
        assert needed in youtube, needed
    instagram = " ".join(setup_steps.steps("instagram"))
    for needed in ("professional account", "Facebook Page", "Create app", "long-lived Page token", "instagram_business_account"):
        assert needed in instagram, needed
    assert "@BotFather" in " ".join(setup_steps.steps("telegram")) and "getUpdates" in " ".join(setup_steps.steps("telegram"))
    assert "aistudio.google.com/apikey" in " ".join(setup_steps.steps("gemini"))
