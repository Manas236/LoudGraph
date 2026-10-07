"""Telegram bot logic with the Bot API mocked (no network)."""
import pytest


@pytest.fixture
def tg(temp_db, monkeypatch):
    from pipeline import actions, approve_telegram
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    calls = []
    monkeypatch.setattr(approve_telegram, "call", lambda method, **kw: calls.append((method, kw)) or {"message_id": 1})
    monkeypatch.setattr(actions, "spawn", lambda *a: 0)  # don't start background jobs in tests
    return approve_telegram, temp_db, calls


def _waiting_run(db):
    rid = db.create_run("inflation")
    for st in ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]:
        db.transition(rid, st)
    return rid


def _cq(data, chat=555, user=555, chat_type="private"):
    return {"id": "cb1", "data": data, "from": {"id": user, "username": "owner"},
            "message": {"message_id": 9, "chat": {"id": chat, "type": chat_type}, "caption": "x"}}


def test_approve_from_owner(tg):
    bot, db, calls = tg
    rid = _waiting_run(db)
    bot.handle_callback(_cq(f"a:{rid}"))
    assert db.get_run(rid)["stage"] == "approved"
    assert calls[0][0] == "answerCallbackQuery" and calls[0][1]["text"] == "approved"


def test_ignores_other_chats_and_users(tg):
    bot, db, calls = tg
    rid = _waiting_run(db)
    bot.handle_callback(_cq(f"a:{rid}", chat=999, user=999))
    bot.handle_callback(_cq(f"a:{rid}", chat=555, user=999))   # stranger in the owner's private chat id
    assert db.get_run(rid)["stage"] == "awaiting_approval"
    assert all(c[1].get("text") == "Not allowed" for c in calls)


def test_reject_and_regenerate(tg):
    bot, db, calls = tg
    rid = _waiting_run(db)
    bot.handle_callback(_cq(f"g:{rid}"))
    assert db.get_run(rid)["stage"] == "rejected"
    new = [r for r in db.list_runs() if r["regen_of"] == rid]
    assert len(new) == 1 and new[0]["topic_id"] == "inflation"


def test_double_tap_is_harmless(tg):
    bot, db, calls = tg
    rid = _waiting_run(db)
    bot.handle_callback(_cq(f"a:{rid}"))
    bot.handle_callback(_cq(f"r:{rid}"))
    assert db.get_run(rid)["stage"] == "approved"
    assert "already approved" in calls[-2][1]["text"]
