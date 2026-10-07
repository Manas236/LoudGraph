"""Telegram approval (section 9): plain Bot API over HTTPS with long polling (getUpdates), so no
public URL or webhook is needed. Buttons call the same functions as the dashboard (actions.py).

`run.py bot` also acts as the publish fallback: approved runs that nobody published yet are
handed to `run.py publish` in the background.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from . import actions, db, health
from .config import country_by_iso3, get_config, run_dir, secret

log = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 50 * 1024 * 1024  # Bot API upload limit


def enabled() -> bool:
    return bool(secret("TELEGRAM_BOT_TOKEN") and secret("TELEGRAM_CHAT_ID"))


def _url(method: str) -> str:
    return f"{get_config()['telegram']['api_base']}/bot{secret('TELEGRAM_BOT_TOKEN')}/{method}"


def call(method: str, http_timeout: float = 30, files=None, **params):
    """Bot API call. `http_timeout` is the HTTP timeout; Bot API params (incl. getUpdates' own
    long-poll `timeout`) go in **params."""
    if files:
        data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
        r = requests.post(_url(method), data=data, files=files, timeout=http_timeout)
    else:
        r = requests.post(_url(method), json=params, timeout=http_timeout)
    try:
        d = r.json()
    except ValueError:
        raise RuntimeError(f"Telegram {method}: HTTP {r.status_code} {r.text[:200]}")
    if not d.get("ok"):
        raise RuntimeError(f"Telegram {method}: {d.get('error_code')} {d.get('description')}")
    return d["result"]


def check() -> tuple[bool, str]:
    try:
        me = call("getMe")
        return True, f"bot @{me.get('username')} ok, approvals from chat {secret('TELEGRAM_CHAT_ID')}"
    except Exception as e:  # noqa: BLE001
        return False, f"getMe failed: {e}"


def send_message(text: str) -> None:
    call("sendMessage", chat_id=secret("TELEGRAM_CHAT_ID"), text=text[:4000], disable_web_page_preview=True)


def keyboard(run_id: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Approve", "callback_data": f"a:{run_id}"}, {"text": "❌ Reject", "callback_data": f"r:{run_id}"}],
        [{"text": "🔁 Regenerate (new countries)", "callback_data": f"g:{run_id}"}],
    ]}


def caption(run_id: str) -> str:
    run = db.get_run(run_id)
    d = run_dir(run_id)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    labels = json.loads((d / "labels.json").read_text(encoding="utf-8"))
    names = country_by_iso3()
    lab = [f"{names[k]['name']} {v['year']}: {v['label']}" for k, v in labels.items() if v.get("label")]
    lines = [
        f"🎬 {meta['title']}",
        f"Topic: {run['topic_id']}  |  score {run['score']}",
        "Countries: " + ", ".join(names[c]["name"] for c in run["countries"]),
        "Labels: " + ("; ".join(lab) if lab else "none"),
        f"{meta['duration_seconds']:.1f}s, {meta['audio'].get('lufs')} LUFS",
        f"Run {run_id}",
    ]
    return "\n".join(lines)[:1000]


def send_for_approval(run_id: str) -> None:
    video = run_dir(run_id) / "video.mp4"
    chat = secret("TELEGRAM_CHAT_ID")
    cap = caption(run_id)
    if video.stat().st_size <= MAX_VIDEO_BYTES:
        with open(video, "rb") as f:
            msg = call("sendVideo", http_timeout=300, files={"video": ("video.mp4", f, "video/mp4")}, chat_id=chat,
                       caption=cap, supports_streaming="true", width=1080, height=1920,
                       reply_markup=keyboard(run_id))
    else:
        msg = call("sendMessage", chat_id=chat, text=cap + "\n(video over 50 MB: watch it on the dashboard)",
                   reply_markup=keyboard(run_id))
    db.log(run_id, "awaiting_approval", f"telegram: sent message {msg['message_id']}")


def _authorized(cq: dict) -> bool:
    allowed = str(secret("TELEGRAM_CHAT_ID"))
    chat = cq.get("message", {}).get("chat", {})
    if str(chat.get("id")) != allowed:
        return False
    if chat.get("type") == "private" and str(cq.get("from", {}).get("id")) != allowed:
        return False
    return True


def handle_callback(cq: dict) -> None:
    if not _authorized(cq):
        log.warning("telegram: ignored callback from %s in chat %s", cq.get("from", {}).get("id"),
                    cq.get("message", {}).get("chat", {}).get("id"))
        call("answerCallbackQuery", callback_query_id=cq["id"], text="Not allowed")
        return
    action, _, run_id = (cq.get("data") or "").partition(":")
    who = f"telegram ({cq.get('from', {}).get('username') or cq.get('from', {}).get('id')})"
    try:
        if action == "a":
            note = actions.approve(run_id, who)
        elif action == "r":
            note = actions.reject(run_id, who)
        elif action == "g":
            note = f"regenerating as {actions.regenerate(run_id, who)}"
        else:
            note = "unknown action"
    except db.TransitionError:
        run = db.get_run(run_id)
        note = f"already {run['stage'] if run else 'gone'}"
    except Exception as e:  # noqa: BLE001
        log.exception("telegram action failed")
        note = f"error: {e}"[:180]
    call("answerCallbackQuery", callback_query_id=cq["id"], text=note[:190])
    msg = cq.get("message", {})
    try:
        base = msg.get("caption") or msg.get("text") or ""
        method = "editMessageCaption" if "caption" in msg else "editMessageText"
        field = "caption" if method == "editMessageCaption" else "text"
        call(method, chat_id=msg["chat"]["id"], message_id=msg["message_id"], **{field: f"{base}\n\n→ {note}"[:1000]})
    except Exception as e:  # noqa: BLE001 - cosmetic only
        log.info("telegram: could not edit message: %s", e)


def handle_message(m: dict) -> None:
    if str(m.get("chat", {}).get("id")) != str(secret("TELEGRAM_CHAT_ID")):
        return
    if (m.get("text") or "").strip().startswith("/status"):
        send_message(status_text())


def status_text() -> str:
    runs = db.list_runs(limit=200)
    counts: dict[str, int] = {}
    for r in runs:
        counts[r["stage"]] = counts.get(r["stage"], 0) + 1
    waiting = [r["id"] for r in runs if r["stage"] == "awaiting_approval"]
    return "Pipeline: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) + \
           (f"\nWaiting for approval: {', '.join(waiting)}" if waiting else "")


def daily_summary_text() -> str:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    runs = [r for r in db.list_runs(limit=500) if db.parse_ts(r["created_at"]) >= since]
    posts = [p for p in db.get_posts() if db.parse_ts(p["created_at"]) >= since]
    stats = sorted((s for s in db.latest_stats() if s.get("views") is not None), key=lambda s: -s["views"])[:3]
    lines = [f"📊 Daily summary ({datetime.now():%Y-%m-%d})",
             f"Runs in the last 24h: {len(runs)} (" +
             ", ".join(f"{st}={sum(1 for r in runs if r['stage'] == st)}" for st in sorted({r['stage'] for r in runs})) + ")",
             f"Posts in the last 24h: {len(posts)} (" + ", ".join(f"{p['platform']}:{p['status']}" for p in posts) + ")"]
    if stats:
        lines.append("Top posts: " + "; ".join(f"{s['topic_id']} {s['platform']} {s['views']} views" for s in stats))
    lines.append(status_text())
    return "\n".join(lines)


def _scan() -> None:
    """Send waiting runs that have not been sent yet; hand stale approved runs to the publisher."""
    if enabled():
        for r in db.list_runs(stage="awaiting_approval"):
            if not db.has_log(r["id"], "telegram: sent"):
                try:
                    send_for_approval(r["id"])
                except Exception as e:  # noqa: BLE001
                    log.warning("telegram send %s failed: %s", r["id"], e)
    now = datetime.now(timezone.utc)
    for r in db.list_runs(stage="approved"):
        idle = (now - db.parse_ts(r["updated_at"])).total_seconds()
        recent_defer = any("deferred" in (e["message"] or "") and (now - db.parse_ts(e["ts"])).total_seconds() < 3 * 3600
                           for e in db.get_log(r["id"])[-5:])
        if idle > 120 and not recent_defer:
            actions.publish(r["id"], "bot (approved run not yet published)")


def poll_once(timeout: int) -> int:
    """One getUpdates round using the offset persisted in the DB, so a restart never re-delivers
    button presses. Returns the number of updates handled."""
    saved = db.kv_get("telegram_offset")
    params = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
    if saved:
        params["offset"] = int(saved)
    n = 0
    for u in call("getUpdates", http_timeout=timeout + 15, **params):
        db.kv_set("telegram_offset", str(u["update_id"] + 1))  # before handling: a crash skips, never repeats
        if "callback_query" in u:
            handle_callback(u["callback_query"])
        elif "message" in u:
            handle_message(u["message"])
        n += 1
    return n


def run_bot() -> int:
    db.init()
    tg = enabled()
    if not tg:
        log.warning("Telegram credentials missing: approval works from the dashboard only; "
                    "bot keeps running as the publish loop")
    else:
        ok, detail = check()
        log.info("telegram: %s", detail)
        if not ok:
            return 1
    last_scan = last_beat = 0.0
    timeout = get_config()["telegram"]["poll_timeout"]
    while True:
        try:
            if time.time() - last_beat >= health.HEARTBEAT_EVERY_S:
                health.beat("bot", telegram=tg)   # the dashboard turns red when this is > 3 min old
                last_beat = time.time()
            if time.time() - last_scan > 30:
                _scan()
                last_scan = time.time()
            if not tg:
                time.sleep(30)
                continue
            poll_once(timeout)
        except KeyboardInterrupt:
            log.info("bot stopped")
            return 0
        except Exception as e:  # noqa: BLE001 - keep the long-running loop alive
            log.warning("bot loop error: %s", e)
            time.sleep(10)
