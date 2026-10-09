"""Telegram control channel: plain Bot API over HTTPS with long polling (getUpdates), so no public
URL or webhook is needed. Only the chat TELEGRAM_CHAT_ID is listened to; every other chat is ignored.

Review cards come ONE at a time (pipeline/review.py holds the queue and the card wording):
  * a card is the video with a short caption (title, metric, countries, length) and Approve /
    Reject / Later buttons; Later moves the video to the back of the queue;
  * after Approve the same message edits itself through the posting stages until Done;
  * the next card goes out only after a decision and, when approved, once posting is final.
Buttons on any other message answer "This card is stale" and do nothing.
Commands (registered with setMyCommands): /new /ready /status /pause /resume /help.
Edits are throttled to one per EDIT_GAP_S per message, and a 429 waits for its retry_after.

`run.py bot` is also the publish fallback (approved runs nobody published yet are handed to
`run.py publish`) and makes the scheduled daily videos (schedule.py).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from . import actions, db, health, review
from .accounts import PLATFORMS, test_mode
from .config import brand_name, get_config, run_dir, secret

log = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 50 * 1024 * 1024  # Bot API upload limit
EDIT_GAP_S = 3.0
STALE = review.STALE
COMMANDS = [("new", "Make a new video: pick one of 3 topics"),
            ("ready", "Finished videos that are not posted yet"),
            ("status", "What's rendering, the open card and the queue"),
            ("pause", "Stop sending review cards"),
            ("resume", "Start sending review cards again"),
            ("help", "What this bot can do")]
HELP = ("🤖 {brand} bot\n"
        "/new — make a new video: pick one of 3 topics, or Random\n"
        "/ready — finished videos not posted yet; tap one to review it next\n"
        "/status — what's rendering, the open card, the queue, test mode\n"
        "/pause — stop sending review cards (the open card stays)\n"
        "/resume — send them again\n"
        "/help — this message\n\n"
        "Review cards come one at a time. ✅ Approve posts the video, ❌ Reject drops it, ⏭ Later moves it to the "
        "back of the queue. The next card comes after you decide and, if you approved, once posting has finished.")


class TelegramError(RuntimeError):
    """A Bot API error. retry_after is set on 429 Too Many Requests."""

    def __init__(self, method: str, code, description: str, retry_after=None):
        super().__init__(f"Telegram {method}: {code} {description}")
        self.code, self.description, self.retry_after = code, description or "", retry_after


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
        raise TelegramError(method, r.status_code, r.text[:200])
    if not d.get("ok"):
        raise TelegramError(method, d.get("error_code"), d.get("description"),
                            (d.get("parameters") or {}).get("retry_after"))
    return d["result"]


def check() -> tuple[bool, str]:
    try:
        me = call("getMe")
        return True, f"bot @{me.get('username')} ok, approvals from chat {secret('TELEGRAM_CHAT_ID')}"
    except Exception as e:  # noqa: BLE001
        return False, f"getMe failed: {e}"


def send_message(text: str, **extra) -> dict:
    return call("sendMessage", chat_id=secret("TELEGRAM_CHAT_ID"), text=text[:4000],
                link_preview_options={"is_disabled": True}, **extra)


def register_commands() -> None:
    call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])


def _allowed(chat: dict, sender: dict) -> bool:
    """Only TELEGRAM_CHAT_ID; in a private chat the sender must be that same account."""
    allowed = str(secret("TELEGRAM_CHAT_ID") or "")
    if not allowed or str(chat.get("id")) != allowed:
        return False
    return chat.get("type") != "private" or str(sender.get("id")) == allowed


def _claim_menu(key: str, message_id) -> bool:
    """A /new or /ready menu works once, and only the latest one: atomically use it up."""
    with db.db() as c:
        cur = c.execute("UPDATE kv SET value='', updated_at=? WHERE key=? AND value=?", (db.now(), key, str(message_id)))
    return cur.rowcount == 1


def _ago(ts: str) -> str:
    s = max(0.0, (datetime.now(timezone.utc) - db.parse_ts(ts)).total_seconds())
    for word, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= size:
            return f"{int(s // size)} {word} ago"
    return "just now"


def _topic_name(topic_id: str) -> str:
    t = review._topic(topic_id)
    return t.get("name") or t.get("title") or topic_id


# ------------------------------------------------------------------ throttled edits

class Editor:
    """Message edits, at most one per `gap` seconds per message (the latest text wins). A 429 holds
    every edit back for its retry_after; "message is not modified" counts as done."""

    def __init__(self, clock=time.monotonic, gap: float = EDIT_GAP_S):
        self.clock, self.gap = clock, gap
        self.last: dict[int, float] = {}
        self.pending: dict[int, tuple] = {}
        self.blocked_until = 0.0

    def touch(self, message_id: int) -> None:
        """The message was just sent: its first edit waits a full gap too."""
        self.last[message_id] = self.clock()

    def edit(self, message_id: int, kind: str, text: str, markup: dict, on_sent=None) -> bool:
        self.pending[message_id] = (kind, text, markup, on_sent)
        return self._send(message_id)

    def flush(self) -> None:
        for mid in list(self.pending):
            self._send(mid)

    def _send(self, mid: int) -> bool:
        now = self.clock()
        if now < self.blocked_until or now - self.last.get(mid, float("-inf")) < self.gap:
            return False
        kind, text, markup, on_sent = self.pending.pop(mid)
        self.last[mid] = now
        params = {"chat_id": secret("TELEGRAM_CHAT_ID"), "message_id": mid, "reply_markup": markup}
        if kind == "video":
            method, params["caption"] = "editMessageCaption", text
        else:
            method, params["text"], params["link_preview_options"] = "editMessageText", text, {"is_disabled": True}
        try:
            call(method, **params)
        except TelegramError as e:
            if e.retry_after:   # 429 Too Many Requests: wait as told, keep the edit
                self.blocked_until = now + float(e.retry_after)
                self.pending.setdefault(mid, (kind, text, markup, on_sent))
                log.info("telegram: rate limited, edits wait %ss", e.retry_after)
                return False
            if "not modified" not in e.description.lower():   # deleted or too old to edit: give up on it
                log.info("telegram: could not edit message %s: %s", mid, e)
        except requests.RequestException as e:
            self.pending.setdefault(mid, (kind, text, markup, on_sent))
            log.info("telegram: edit of message %s failed (%s); trying again", mid, e)
            return False
        if on_sent:
            on_sent()
        return True


# ------------------------------------------------------------------ the bot

class Bot:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.editor = Editor(clock)
        self.send_after = 0.0
        self.job_text: dict[int, str] = {}

    # ---------------------------------------------------------- updates
    def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            self.on_callback(u["callback_query"])
        elif "message" in u:
            self.on_message(u["message"])

    def on_callback(self, cq: dict) -> None:
        msg = cq.get("message") or {}
        if not _allowed(msg.get("chat") or {}, cq.get("from") or {}):
            log.info("telegram: ignored a button from chat %s", (msg.get("chat") or {}).get("id"))
            return
        try:
            answer = self._button(cq.get("data") or "", msg.get("message_id"))
        except Exception as e:  # noqa: BLE001 - always answer the button
            log.exception("telegram button failed")
            answer = f"Something went wrong: {e}"
        try:
            call("answerCallbackQuery", callback_query_id=cq["id"], text=answer[:190])
        except Exception as e:  # noqa: BLE001 - cosmetic only
            log.info("telegram: could not answer a button: %s", e)
        if answer != STALE:
            self.dispatch()   # a rejected / later / moved card may free the slot: send the next card now

    def _button(self, data: str, mid) -> str:
        kind, _, rest = data.partition(":")
        if kind == "c":
            return self._card_button(rest, mid)
        if kind == "n":
            return self._new_button(rest, mid)
        if kind == "f":
            return self._ready_button(rest, mid)
        return STALE   # every button from before the review queue, or anything unknown

    def _card_button(self, rest: str, mid) -> str:
        parts = rest.split(":")
        try:
            card_id = int(parts[1])
        except (IndexError, ValueError):
            return STALE
        if parts[0] == "t" and len(parts) == 3:   # Retry on a finished card, while it is that video's latest card
            card = review.get_card(card_id)
            latest = card and review.latest_card_for_run(card["run_id"])
            if not card or card["message_id"] != mid or card["state"] != "done" or latest["id"] != card_id:
                return STALE
            answer = review.retry(card, parts[2])
        else:
            action = {"a": "approve", "r": "reject", "l": "later"}.get(parts[0])
            card = review.current_card()
            if not action or not card or card["id"] != card_id or card["message_id"] != mid:
                return STALE
            answer = review.decide(card, action)
        self.render(review.get_card(card_id))
        return answer

    def _new_button(self, choice: str, mid) -> str:
        if not _claim_menu("telegram:new_msg", mid):
            return STALE
        random_pick = choice == "*"
        topic_id = review.random_topic() if random_pick else choice
        if not topic_id or not review._topic(topic_id):
            self.editor.edit(mid, "text", "No topic is free right now: every topic has a video or is resting.",
                             {"inline_keyboard": []})
            return "No topic available."
        waits = review.render_busy() or bool(review.jobs("queued"))
        job = review.add_job(topic_id, mid, random_pick)
        self.advance_jobs()
        self.render_job(review.get_job(job["id"]))
        return "Queued: another video is rendering." if waits else "Starting."

    def _ready_button(self, ref: str, mid) -> str:
        if not _claim_menu("telegram:ready_msg", mid):
            return STALE
        run = db.run_by_ref(int(ref)) if ref.isdigit() else None
        if not run:
            answer, text = "That video is gone.", "That video is gone."
        else:
            answer = review.bring_to_front(run["id"], "telegram /ready")
            text = f"⏫ {review.video_title(run)}\n{answer}"
        self.editor.edit(mid, "text", text, {"inline_keyboard": []})
        return answer

    def on_message(self, m: dict) -> None:
        if not _allowed(m.get("chat") or {}, m.get("from") or {}):
            return
        text = (m.get("text") or "").strip()
        cmd = text.split()[0][1:].split("@")[0].lower() if text.startswith("/") else ""
        handler = {"new": self.cmd_new, "ready": self.cmd_ready, "status": self.cmd_status, "pause": self.cmd_pause,
                   "resume": self.cmd_resume, "help": self.cmd_help, "start": self.cmd_help}.get(cmd, self.cmd_help)
        handler()

    # ---------------------------------------------------------- commands
    def cmd_help(self) -> None:
        send_message(HELP.format(brand=brand_name()))

    def cmd_status(self) -> None:
        send_message(status_text())

    def cmd_pause(self) -> None:
        review.set_paused(True)
        send_message("⏸ Paused: no new review cards until /resume. The open card (if any) still works.")

    def cmd_resume(self) -> None:
        review.set_paused(False)
        send_message("▶️ Resumed: review cards come one at a time again.")
        self.dispatch()

    def cmd_new(self) -> None:
        topics = [t for t in review.suggestions(3) if len(t["id"]) <= 60]
        rows = [[{"text": t.get("name") or t["title"], "callback_data": f"n:{t['id']}"}] for t in topics]
        rows.append([{"text": "🎲 Random", "callback_data": "n:*"}])
        lines = ["Pick a topic for a new video:" if topics else
                 "Every topic already has a video or is resting. Random may still find one."]
        if review.render_busy() or review.jobs("queued"):
            lines.append("A video is rendering now, so your pick will wait its turn.")
        msg = send_message("\n".join(lines), reply_markup={"inline_keyboard": rows})
        db.kv_set("telegram:new_msg", str(msg["message_id"]))

    def cmd_ready(self) -> None:
        videos = review.ready_videos(8)
        if not videos:
            send_message("No finished videos are waiting to be posted. Use /new to make one.")
            return
        rows = [[{"text": f"{review.video_title(r)[:48]} · {_ago(r['created_at'])}", "callback_data": f"f:{r['ref']}"}]
                for r in videos]
        msg = send_message("Finished videos not posted yet, newest first. Tap one to review it next:",
                           reply_markup={"inline_keyboard": rows})
        db.kv_set("telegram:ready_msg", str(msg["message_id"]))

    # ---------------------------------------------------------- the review queue
    def tick(self) -> None:
        """Follow the current card (dashboard decisions, posting), the /new jobs, then send the next card."""
        card = review.current_card()
        if card and card["state"] == "sending":   # left over from a crash mid-send: it is offered again
            review.close_card(card["id"], "sending", "closed", "unsent")
            card = None
        if card and card["state"] == "open":
            card = review.sync_open(card)
        if card and card["state"] == "posting":
            review.check_posting(card)
        for c in review.cards_to_render():
            if c["state"] == "done" and c["watch"]:
                review.check_watched(c)
                c = review.get_card(c["id"])
            self.render(c)
        self.advance_jobs()
        self.dispatch()
        self.editor.flush()

    def render(self, card: dict | None) -> None:
        """Edit the card's message to match its state (throttled; unchanged text is not resent)."""
        if not card or not card.get("message_id"):
            return
        text, markup = review.card_view(card)
        if text == card["caption"] and markup == card["buttons"]:
            return
        cid = card["id"]
        self.editor.edit(card["message_id"], card["kind"], text, markup,
                         on_sent=lambda: review.update_card(cid, caption=text, buttons=markup))

    def dispatch(self) -> None:
        """Send the next review card, unless one is already out, the queue is paused or empty."""
        if review.paused() or self.clock() < self.send_after or review.current_card():
            return
        run = review.next_for_review()
        if not run:
            return
        cid = review.create_card(run["id"])
        if cid is None:
            return
        text, markup = review.card_view(review.get_card(cid))
        try:
            msg, kind = self._send_card(run, text, markup)
        except Exception as e:  # noqa: BLE001 - try again later; the queue must not jam
            review.drop_card(cid)
            wait = float(getattr(e, "retry_after", None) or 30)
            self.send_after = self.clock() + wait
            log.warning("telegram: could not send the review card for %s (%s); trying again in %ss", run["id"], e, wait)
            return
        review.card_sent(cid, msg["message_id"], kind, text, markup)
        self.editor.touch(msg["message_id"])
        db.log(run["id"], "awaiting_approval", f"telegram: review card {cid} sent (message {msg['message_id']})")

    def _send_card(self, run: dict, text: str, markup: dict) -> tuple[dict, str]:
        video = run_dir(run["id"]) / "video.mp4"
        chat = secret("TELEGRAM_CHAT_ID")
        if video.exists() and video.stat().st_size <= MAX_VIDEO_BYTES:
            try:
                with open(video, "rb") as f:
                    return call("sendVideo", http_timeout=300, files={"video": ("video.mp4", f, "video/mp4")},
                                chat_id=chat, caption=text, supports_streaming="true", width=1080, height=1920,
                                reply_markup=markup), "video"
            except TelegramError as e:
                if e.code != 400:
                    raise
                log.warning("telegram: sendVideo refused for %s (%s); sending a text card", run["id"], e)
        note = "(video over 50 MB: watch it in the dashboard)" if video.exists() else "(no video file: see the dashboard)"
        return send_message(review.fit(text, [note]), reply_markup=markup), "text"

    # ---------------------------------------------------------- /new jobs
    def advance_jobs(self) -> None:
        """One render at a time: follow the running /new job, then start the next queued one."""
        for job in review.jobs("running"):
            self._follow_job(job)
        if not review.jobs("running") and not review.making_now():
            queued = review.jobs("queued")
            if queued:
                self._start_job(queued[0])
        for job in review.jobs("queued"):
            self.render_job(job)

    def _start_job(self, job: dict) -> None:
        rid = db.create_run(job["topic_id"])
        review.update_job(job["id"], state="running", run_id=rid)
        try:
            actions.spawn("produce", "--run", rid, "--from", "fetch")
        except Exception as e:  # noqa: BLE001
            db.fail(rid, "fetch", f"could not start the video maker: {e}")
            review.update_job(job["id"], state="failed")
        self.render_job(review.get_job(job["id"]))

    def _follow_job(self, job: dict) -> None:
        run = db.get_run(job["run_id"]) if job["run_id"] else None
        if not run:
            review.update_job(job["id"], state="failed")
        elif run["stage"] in review.MAKING:
            if not review.making_alive(run, started_at=job["updated_at"]):
                db.fail(run["id"], run["stage"], "the video maker stopped unexpectedly")
                review.update_job(job["id"], state="failed")
        elif run["stage"] == "skipped":
            review.update_job(job["id"], state="skipped")
        elif run["stage"] == "failed":
            review.update_job(job["id"], state="failed")
        else:
            review.update_job(job["id"], state="ready")
        self.render_job(review.get_job(job["id"]))

    def render_job(self, job: dict) -> None:
        if not job or not job["message_id"]:
            return
        text = job_text(job)
        if self.job_text.get(job["id"]) == text:
            return
        jid = job["id"]
        self.editor.edit(job["message_id"], "text", text, {"inline_keyboard": []},
                         on_sent=lambda: self.job_text.__setitem__(jid, text))

    # ---------------------------------------------------------- loop helpers
    def poll_timeout(self) -> int:
        """Short polls while something on screen is changing, long ones when idle."""
        cfg = get_config()["telegram"]
        card = review.current_card()
        busy = bool(self.editor.pending or (card and card["state"] != "open") or review.jobs("queued", "running")
                    or any(c["watch"] for c in review.cards_to_render()))
        return int(cfg.get("busy_poll_timeout", 2) if busy else cfg.get("poll_timeout", 10))


def job_text(job: dict) -> str:
    from .errors import explain_error
    name = _topic_name(job["topic_id"])
    head = f"🎲 Random pick: {name}" if job["random"] else f"🛠 New video: {name}"
    run = db.get_run(job["run_id"]) if job["run_id"] else None
    state = job["state"]
    if state == "queued":
        line = "⏳ Queued: another video is rendering. This one starts when it's done."
    elif state == "running":
        stage = run["stage"] if run else "queued"
        if stage == "queued":
            line = "📥 Fetching data…"
        else:
            pct = f" {round(run['progress'] * 100)}%" if run and run.get("progress") is not None else "…"
            line = f"🎨 Rendering{pct}"
    elif state == "ready":
        line = "✅ Ready: it's in the review queue."
    elif state == "skipped":
        line = (f"😴 Skipped: {name} turned out too boring this time ({review.skip_reason(run['id'])}). "
                "Nothing broke. Try another topic with /new.")
    else:
        why = explain_error(run["error"], run["failed_stage"] or "render")["sentence"] if run and run.get("error") else \
            "the video maker stopped."
        line = f"⚠️ Couldn't make this video: {why}"
    return f"{head}\n{line}"


_BOT: Bot | None = None


def get_bot() -> Bot:
    global _BOT
    if _BOT is None:
        _BOT = Bot()
    return _BOT


def handle_callback(cq: dict) -> None:
    get_bot().on_callback(cq)


def handle_message(m: dict) -> None:
    get_bot().on_message(m)


# ------------------------------------------------------------------ texts

def status_text() -> str:
    lines = [f"📊 {brand_name()} status"]
    running = review.jobs("running")
    making = review.making_now()
    if running:
        lines.append(f"🛠 Rendering: {_topic_name(running[0]['topic_id'])} · {job_text(running[0]).split(chr(10), 1)[1]}")
    elif making:
        r = making[0]
        pct = f" {round(r['progress'] * 100)}%" if r.get("progress") is not None else ""
        lines.append(f"🛠 Rendering: {_topic_name(r['topic_id'])}{pct}")
    else:
        lines.append("🛠 Nothing rendering")
    waiting_jobs = review.jobs("queued")
    if waiting_jobs:
        lines.append(f"   + {len(waiting_jobs)} /new pick{'s' if len(waiting_jobs) != 1 else ''} waiting to render")
    card = review.current_card()
    if card:
        run = db.get_run(card["run_id"])
        doing = "posting" if card["state"] == "posting" else "waiting for your decision"
        lines.append(f"🃏 Open card: {review.video_title(run) if run else 'a removed video'} ({doing})")
    else:
        lines.append("🃏 No open card")
    q = [r for r in review.queue() if not (card and r["id"] == card["run_id"])]
    lines.append(f"📥 Review queue: {len(q)} waiting")
    if review.paused():
        lines.append("⏸ Review cards are paused: /resume to continue")
    tm = test_mode()
    names = lambda keys: ", ".join(PLATFORMS[k] for k in keys)  # noqa: E731
    if tm["on"]:
        line = "🧪 Test mode: ON, nothing is actually posted"
    elif tm["testing"]:
        line = f"🧪 Test mode: on for {names(tm['testing'])}; live on {names(tm['live'])}"
    elif tm["live"]:
        line = f"🧪 Test mode: off, posting live to {names(tm['live'])}"
    else:
        line = "🧪 No platform is turned on"
    lines.append(line + (f" ({names(tm['off'])} off)" if tm["off"] and (tm["live"] or tm["testing"]) else ""))
    return "\n".join(lines)


def daily_summary_text() -> str:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    runs = [r for r in db.list_runs(limit=500) if db.parse_ts(r["created_at"]) >= since]
    posts = [p for p in db.get_posts() if db.parse_ts(p["created_at"]) >= since]
    stats = sorted((s for s in db.latest_stats() if s.get("views") is not None), key=lambda s: -s["views"])[:3]
    lines = [f"📊 {brand_name()} · daily summary ({datetime.now():%Y-%m-%d})",
             f"Runs in the last 24h: {len(runs)} (" +
             ", ".join(f"{st}={sum(1 for r in runs if r['stage'] == st)}" for st in sorted({r['stage'] for r in runs})) + ")",
             f"Posts in the last 24h: {len(posts)} (" + ", ".join(f"{p['platform']}:{p['status']}" for p in posts) + ")"]
    if stats:
        lines.append("Top posts: " + "; ".join(f"{s['topic_id']} {s['platform']} {s['views']} views" for s in stats))
    lines.append(status_text())
    return "\n".join(lines)


# ------------------------------------------------------------------ loop

def _publish_stragglers() -> None:
    """Hand approved runs that nobody published yet (e.g. accounts connected later) to the publisher."""
    now = datetime.now(timezone.utc)
    for r in db.list_runs(stage="approved"):
        idle = (now - db.parse_ts(r["updated_at"])).total_seconds()
        recent_defer = any("deferred" in (e["message"] or "") and (now - db.parse_ts(e["ts"])).total_seconds() < 3 * 3600
                           for e in db.get_log(r["id"])[-5:])
        if idle > 120 and not recent_defer:
            actions.publish(r["id"], "bot (approved run not yet published)")


def poll_once(timeout: int, bot: Bot | None = None) -> int:
    """One getUpdates round using the offset persisted in the DB, so a restart never re-delivers
    button presses. Returns the number of updates handled."""
    bot = bot or get_bot()
    saved = db.kv_get("telegram_offset")
    params = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
    if saved:
        params["offset"] = int(saved)
    n = 0
    for u in call("getUpdates", http_timeout=timeout + 15, **params):
        db.kv_set("telegram_offset", str(u["update_id"] + 1))  # before handling: a crash skips, never repeats
        bot.handle_update(u)
        n += 1
    return n


def run_bot() -> int:
    from .lock import single_instance
    with single_instance("bot") as mine:
        if not mine:  # e.g. Bot.bat double-clicked twice: two bots would fight over getUpdates
            log.warning("the bot is already running in another window; this one stops")
            print("The bot is already running in another window. You can close this one.")
            return 0
        return _bot_loop()


def _bot_loop() -> int:
    db.init()
    tg = enabled()
    bot = get_bot()
    if not tg:
        log.warning("Telegram credentials missing: approval works from the dashboard only; "
                    "bot keeps running as the publish loop")
    else:
        ok, detail = check()
        log.info("telegram: %s", detail)
        if not ok:
            return 1
        try:
            register_commands()
        except Exception as e:  # noqa: BLE001 - the commands still work when typed
            log.warning("telegram: setMyCommands failed: %s", e)
        card = review.current_card()
        if card:
            log.info("telegram: resuming review card %s (%s) for %s", card["id"], card["state"], card["run_id"])
        try:
            review.suggestions(3)   # scores the cached topics once, so the first /new answers at once
        except Exception as e:  # noqa: BLE001
            log.info("telegram: could not pre-score topics: %s", e)
    last_scan = last_beat = 0.0
    while True:
        try:
            from .schedule import tick
            tick()
            if time.time() - last_beat >= health.HEARTBEAT_EVERY_S:
                health.beat("bot", telegram=tg)   # the dashboard turns red when this is > 3 min old
                last_beat = time.time()
            if time.time() - last_scan > 30:
                _publish_stragglers()
                last_scan = time.time()
            if not tg:
                time.sleep(30)
                continue
            bot.tick()
            poll_once(bot.poll_timeout(), bot)
        except KeyboardInterrupt:
            log.info("bot stopped")
            return 0
        except Exception as e:  # noqa: BLE001 - keep the long-running loop alive
            log.warning("bot loop error: %s", e)
            time.sleep(10)
