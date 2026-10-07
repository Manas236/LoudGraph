"""Health panel logic: bot heartbeat, credential checks, YouTube token refresh detection (no network)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _beat(age_s):
    return json.dumps({"ts": (NOW - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ"), "pid": 1})


def test_heartbeat_state():
    from pipeline.health import heartbeat_state
    assert heartbeat_state(None, NOW)["state"] == "never"
    assert heartbeat_state("not json", NOW)["state"] == "never"
    assert heartbeat_state(_beat(30), NOW)["state"] == "ok"
    assert heartbeat_state(_beat(180), NOW)["state"] == "ok"        # exactly 3 min is still fine
    s = heartbeat_state(_beat(181), NOW)
    assert s["state"] == "stale" and s["age_s"] == 181


def test_bot_loop_writes_a_heartbeat(temp_db, monkeypatch):
    from pipeline import approve_telegram as bot, health
    for n in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(n, raising=False)

    def stop(_seconds):
        raise KeyboardInterrupt   # end the loop at its first sleep
    monkeypatch.setattr(bot.time, "sleep", stop)
    assert bot.run_bot() == 0
    st = health.heartbeat_state(temp_db.kv_get("heartbeat:bot"))
    assert st["state"] == "ok" and st["info"]["telegram"] is False


def test_credential_checks_without_secrets_stay_offline(temp_db, monkeypatch):
    import requests
    from pipeline import config, health
    for n in config.SECRET_NAMES:
        monkeypatch.delenv(n, raising=False)

    def no_network(*a, **k):
        raise AssertionError("network call without credentials")
    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(requests, "post", no_network)
    res = {c["name"]: c for c in health.credential_checks()}
    assert list(res) == ["Gemini", "Telegram", "YouTube", "Instagram", "Facebook"]
    assert all(res[n]["state"] == "missing" and res[n]["level"] == "WARN"
               for n in ("Gemini", "Telegram", "YouTube", "Instagram"))
    assert res["Facebook"]["state"] in ("disabled", "missing")


def test_doctor_flags_a_youtube_token_that_no_longer_refreshes(temp_db, tmp_path, monkeypatch):
    """A refresh token from a consent screen left in 'Testing' dies after 7 days. doctor must say so
    (with the fix), and the publisher must fail loudly instead of stopping silently."""
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from pipeline import publish_youtube as yt
    client_secret = tmp_path / "client_secret.json"
    client_secret.write_text("{}")
    token = tmp_path / "youtube_token.json"
    token.write_text(json.dumps({"token": "old", "refresh_token": "dead", "client_id": "id", "client_secret": "s",
                                 "token_uri": "https://oauth2.googleapis.com/token",
                                 "expiry": "2020-01-01T00:00:00Z"}))
    monkeypatch.setattr(yt, "client_secret_file", lambda: client_secret)
    monkeypatch.setattr(yt, "token_file", lambda: token)

    def dead(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")
    monkeypatch.setattr(Credentials, "refresh", dead)
    res = yt.check()
    assert res["state"] == "expired" and res["level"] in ("WARN", "FAIL")
    assert "invalid_grant" in res["detail"] and "In production" in res["detail"]
    with pytest.raises(yt.NotAuthorized, match="In production"):
        yt.get_credentials()


def test_youtube_without_a_token_asks_the_owner_to_authorise(temp_db, tmp_path, monkeypatch):
    from pipeline import publish_youtube as yt
    client_secret = tmp_path / "client_secret.json"
    client_secret.write_text("{}")
    monkeypatch.setattr(yt, "client_secret_file", lambda: client_secret)
    monkeypatch.setattr(yt, "token_file", lambda: tmp_path / "none.json")
    res = yt.check()
    assert res["state"] == "missing" and "publish_youtube --auth" in res["detail"]
