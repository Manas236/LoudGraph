"""Alerts for the autopilot: silent while the channel is healthy, one Telegram message when something breaks.

An alert is two plain lines: what broke, and what to check. Each has a key (e.g. "post_failed"); the same key goes
out at most once per autopilot.alert_every_hours (12), however often the job that sees it runs. When the condition
is gone, clear(key) sends one short "Resolved" line, once. State lives in the `alerts` table.

Messages go out as direct Bot API sendMessage calls (requests.post): nothing here reads updates, so no long-polling
bot runs in autopilot and nothing competes for getUpdates.

Healthchecks.io: ping() GETs HEALTHCHECK_URL after every successful post and HEALTHCHECK_URL + "/fail" when any job
fails, with a 10 s timeout. A ping that fails is logged and never breaks the job.

Neither sending an alert nor pinging ever raises.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import requests

from . import db
from .config import brand_name, get_config, secret

log = logging.getLogger(__name__)
PING_TIMEOUT_S = 10


def _every() -> timedelta:
    return timedelta(hours=float((get_config().get("autopilot") or {}).get("alert_every_hours", 12)))


def send(text: str) -> bool:
    """One Telegram message to TELEGRAM_CHAT_ID, by sendMessage only. False (logged) when it could not go out."""
    token, chat = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.warning("alert not sent (Telegram is not set up in .env): %s", text.replace("\n", " / "))
        return False
    base = (get_config().get("telegram") or {}).get("api_base", "https://api.telegram.org")
    try:
        r = requests.post(f"{base}/bot{token}/sendMessage", timeout=30,
                          json={"chat_id": chat, "text": text[:4000], "link_preview_options": {"is_disabled": True}})
        if r.status_code == 200 and r.json().get("ok"):
            return True
        log.warning("alert not sent: Telegram answered HTTP %s %s", r.status_code, r.text[:200].replace(token, "***"))
    except Exception as e:  # noqa: BLE001 - an alert must never break the job that raises it
        log.warning("alert not sent: %s", str(e).replace(token, "***")[:300])
    return False


def _row(key: str) -> dict | None:
    with db.db() as c:
        r = c.execute("SELECT * FROM alerts WHERE key=?", (key,)).fetchone()
    return dict(r) if r else None


def fire(key: str, what: str, check: str, now=None) -> bool:
    """Alert `key`: "<what>" and "Check: <check>". Sent unless the same key went out within the last 12 h.
    Returns True when a message was sent."""
    now = now or db.parse_ts(db.now())
    text = f"⚠️ {brand_name()}: {what}\nCheck: {check}"
    try:
        row = _row(key)
        if row and row["active"] and row["sent_at"] and now - db.parse_ts(row["sent_at"]) < _every():
            log.info("alert %s already sent at %s: %s", key, row["sent_at"], what)
            with db.db() as c:
                c.execute("UPDATE alerts SET message=?, updated_at=? WHERE key=?", (text, db.now(), key))
            return False
        sent = send(text)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with db.db() as c:
            c.execute("INSERT INTO alerts(key, message, active, sent_at, cleared_at, updated_at) VALUES (?,?,1,?,NULL,?) "
                      "ON CONFLICT(key) DO UPDATE SET message=excluded.message, active=1, sent_at=excluded.sent_at, "
                      "cleared_at=NULL, updated_at=excluded.updated_at",
                      (key, text, stamp if sent else (row or {}).get("sent_at"), db.now()))
        log.warning("ALERT %s: %s | check: %s", key, what, check)
        return sent
    except Exception as e:  # noqa: BLE001
        log.warning("alert %s could not be recorded: %s", key, e)
        return False


def clear(key: str, what: str | None = None) -> bool:
    """The condition behind `key` is gone: one short line, only when that alert had fired. True when sent."""
    try:
        row = _row(key)
        if not row or not row["active"]:
            return False
        with db.db() as c:
            c.execute("UPDATE alerts SET active=0, cleared_at=?, updated_at=? WHERE key=?", (db.now(), db.now(), key))
        first = row["message"].splitlines()[0].replace("⚠️ ", "", 1)
        log.info("alert %s cleared", key)
        return send(f"✅ Resolved: {what or first}")
    except Exception as e:  # noqa: BLE001
        log.warning("alert %s could not be cleared: %s", key, e)
        return False


def active() -> list[dict]:
    with db.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM alerts WHERE active=1 ORDER BY updated_at DESC")]


def ping(ok: bool = True) -> bool:
    """Healthchecks.io: GET HEALTHCHECK_URL (ok) or HEALTHCHECK_URL/fail. Never raises; False when not sent."""
    url = secret("HEALTHCHECK_URL")
    if not url:
        log.warning("HEALTHCHECK_URL is empty: no healthcheck ping")
        return False
    target = url.rstrip("/") + ("" if ok else "/fail")
    try:
        requests.get(target, timeout=PING_TIMEOUT_S)
        return True
    except Exception as e:  # noqa: BLE001 - a ping must never break a job
        log.warning("healthcheck ping failed: %s", e)
        return False
