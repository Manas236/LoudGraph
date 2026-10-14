"""Telegram control channel: plain Bot API over HTTPS with long polling (getUpdates), so no public
URL or webhook is needed. Only the chat TELEGRAM_CHAT_ID is listened to; every other chat is ignored.

Everything is a button; no command has to be remembered. The command list (setMyCommands) is just
/menu, and /menu or /start sends the HOME CARD and pins it: one message the bot keeps editing
(pipeline/home.py). It shows what is being made (step X of 4), what waits to be made, what waits for
review, what waits to post and what was posted last, and turns into the New reel, Make a batch, Stats and
Account screens. A new /menu moves the card to the bottom (the old one is deleted).

  * New reel / Make a batch put topics in the make queue (makequeue.py); reels are made one at a time and
    their progress shows only on the home card.
  * Review ready reels opens the REVIEW CARD: one message that shows one reel at a time (video, hook
    question, caption options, hashtags) and moves to the next reel after each decision. Edit caption
    asks for a reply and updates the same card.
  * When a reel becomes ready the bot sends at most ONE short note, and only when no other note is
    unread and the review card is not open.
  * After a reel posts, the home card updates and one short message carries the post link.
  * Errors show on the home card in plain words with Retry and Drop, never as a stack trace.
Buttons on any older message answer that they are out of date and do nothing. Edits are throttled to one
per EDIT_GAP_S per message (a tap is answered at once), and a 429 waits for its retry_after.

`run.py bot` also works the make queue, settles a publisher that died, publishes approved runs nobody
published yet, collects stats and makes the scheduled daily videos (schedule.py).
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone

import requests

from . import actions, db, health, home, ideas, makequeue, review
from .config import brand_name, get_config, run_dir, secret

log = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 50 * 1024 * 1024  # Bot API upload limit
EDIT_GAP_S = 3.0
STALE = review.STALE
COMMANDS = [("menu", "Open the Graphony menu")]
K_READY_NOTE = "home:ready_note"     # the one unread "Reel ready" note
K_SEEN_READY = "home:seen_ready"     # reels already known to be waiting for review
K_ANNOUNCED = "posting:announced"    # runs whose posting result was already sent
FORCE_REPLY = {"force_reply": True}


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


def delete_message(message_id) -> bool:
    try:
        call("deleteMessage", chat_id=secret("TELEGRAM_CHAT_ID"), message_id=message_id)
        return True
    except Exception as e:  # noqa: BLE001 - too old or already gone: the caller decides what to do
        log.info("telegram: could not delete message %s: %s", message_id, e)
        return False


def send_test_preview(run_id: str, report: dict | None = None) -> dict:
    """A TEST render (orchestrator.make_test_video) to the owner's chat as a plain video message:
    marked TEST, no buttons, not on the review card, so it is outside the review queue and never posted."""
    meta = json.loads((run_dir(run_id) / "meta.json").read_text(encoding="utf-8"))
    opening = meta.get("opening") or {}
    lines = ["🧪 TEST PREVIEW · NOT POSTED · not in the review queue",
             f"🎬 {meta.get('title', '')}",
             f"❓ Opens with: {opening.get('question', '')}",
             f"✅ End card: {opening.get('answer', '')}",
             "🌍 " + ", ".join(c["name"] for c in meta.get("countries", [])),
             f"⏱ {meta.get('duration_seconds', 0):.1f} s"]
    if report:
        f = report.get("frames", [])
        lines.append("🔎 Frames " + ", ".join(f"{x['t']:.1f}s" for x in f) + ": " +
                     ("no dark frame, question + line visible" if report["checks"].get("no_near_black_frame")
                      and report["checks"].get("hook_in_every_frame") else "CHECK FAILED, see the log") +
                     f"; audio from {report['audio']['first_audible_s']} s")
    from .captions import load
    c = load(run_id)
    if c:
        v = c["variants"][c.get("chosen", 0)]
        lines += ["", "✍️ Post caption 1:", v["hook"], v["question"], " ".join(c.get("hashtags", []))]
    text = review.fit("\n".join(lines), [])
    with open(run_dir(run_id) / "video.mp4", "rb") as fh:
        return call("sendVideo", http_timeout=300, files={"video": ("video.mp4", fh, "video/mp4")},
                    chat_id=secret("TELEGRAM_CHAT_ID"), caption=text, supports_streaming="true", width=1080,
                    height=1920)


def register_commands() -> None:
    call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])


def _allowed(chat: dict, sender: dict) -> bool:
    """Only TELEGRAM_CHAT_ID; in a private chat the sender must be that same account."""
    allowed = str(secret("TELEGRAM_CHAT_ID") or "")
    if not allowed or str(chat.get("id")) != allowed:
        return False
    return chat.get("type") != "private" or str(sender.get("id")) == allowed


def _json_kv(key: str):
    try:
        return json.loads(db.kv_get(key) or "null")
    except ValueError:
        return None


# ------------------------------------------------------------------ throttled edits

class Editor:
    """Message edits, at most one per `gap` seconds per message (the latest text wins). A 429 holds
    every edit back for its retry_after; "message is not modified" counts as done. now=True (a tap)
    skips the per-message gap, but never a 429 wait."""

    def __init__(self, clock=time.monotonic, gap: float = EDIT_GAP_S):
        self.clock, self.gap = clock, gap
        self.last: dict[int, float] = {}
        self.pending: dict[int, tuple] = {}
        self.blocked_until = 0.0

    def touch(self, message_id: int) -> None:
        """The message was just sent: its first edit waits a full gap too."""
        self.last[message_id] = self.clock()

    def edit(self, message_id: int, kind: str, text: str, markup: dict, on_sent=None, now: bool = False) -> bool:
        self.pending[message_id] = (kind, text, markup, on_sent)
        return self._send(message_id, now)

    def flush(self) -> None:
        for mid in list(self.pending):
            self._send(mid)

    def forget(self, message_id: int) -> None:
        self.pending.pop(message_id, None)

    def _send(self, mid: int, now: bool = False) -> bool:
        t = self.clock()
        if t < self.blocked_until or (not now and t - self.last.get(mid, float("-inf")) < self.gap):
            return False
        kind, text, markup, on_sent = self.pending.pop(mid)
        self.last[mid] = t
        params = {"chat_id": secret("TELEGRAM_CHAT_ID"), "message_id": mid, "reply_markup": markup}
        if kind == "video":
            method, params["caption"] = "editMessageCaption", text
        else:
            method, params["text"], params["link_preview_options"] = "editMessageText", text, {"is_disabled": True}
        try:
            call(method, **params)
        except TelegramError as e:
            if e.retry_after:   # 429 Too Many Requests: wait as told, keep the edit
                self.blocked_until = t + float(e.retry_after)
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
    def __init__(self, clock=time.monotonic, rng: random.Random | None = None):
        self.clock = clock
        self.rng = rng or random.Random()
        self.editor = Editor(clock)
        self.home_shown: tuple | None = None    # (text, markup) the home card shows (or will after the edit)

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
        except Exception:  # noqa: BLE001 - always answer the button, in plain words
            log.exception("telegram button failed")
            answer = "That didn't work. Please try again."
        try:
            call("answerCallbackQuery", callback_query_id=cq["id"], text=answer[:190])
        except Exception as e:  # noqa: BLE001 - cosmetic only
            log.info("telegram: could not answer a button: %s", e)
        if answer != STALE:
            self.refresh_home(now=True)

    def _button(self, data: str, mid) -> str:
        kind, _, rest = data.partition(":")
        if kind == "h":
            return self._home_button(rest) if mid and mid == home.home_msg() else STALE
        if kind == "v":
            st = review.card_state()
            return self._review_button(rest, st) if st and st["mid"] == mid else STALE
        if kind == "r":
            return self.open_review() if mid and str(mid) == (db.kv_get(K_READY_NOTE) or "") else STALE
        return STALE   # every button from before this menu, or anything unknown

    def on_message(self, m: dict) -> None:
        if not _allowed(m.get("chat") or {}, m.get("from") or {}):
            return
        text = (m.get("text") or "").strip()
        cmd = text.split()[0][1:].split("@")[0].lower() if text.startswith("/") else ""
        if cmd in ("menu", "start"):
            delete_message(m["message_id"])   # keeps the chat to the home card
            self.open_menu()
            return
        waiting = home.awaiting()
        if waiting and text and not cmd:
            if waiting["kind"] == "caption":
                self.caption_reply(waiting, m, text)
                return
            if waiting["kind"] == "topic":
                self.topic_reply(waiting, m, text)
                return
        self.open_menu()   # an old command or a stray message: the menu comes down to the bottom

    # ---------------------------------------------------------- the home card
    def open_menu(self) -> None:
        """Send the home card at the bottom of the chat and pin it; the old one is deleted."""
        old = home.home_msg()
        if old and not delete_message(old):
            try:   # too old to delete: it stops being a menu
                call("editMessageText", chat_id=secret("TELEGRAM_CHAT_ID"), message_id=old,
                     text="⬇️ The menu moved to the newest message.", reply_markup={"inline_keyboard": []})
                call("unpinChatMessage", chat_id=secret("TELEGRAM_CHAT_ID"), message_id=old)
            except Exception as e:  # noqa: BLE001
                log.info("telegram: could not retire the old menu: %s", e)
        if old:
            self.editor.forget(old)
        self._end_prompt(home.awaiting(), keep_caption=True)
        home.set_screen("home")
        text, markup = home.view()
        msg = send_message(text, reply_markup=markup)
        db.kv_set(home.K_MSG, str(msg["message_id"]))
        self.home_shown = (text, markup)
        self.editor.touch(msg["message_id"])
        try:
            call("pinChatMessage", chat_id=secret("TELEGRAM_CHAT_ID"), message_id=msg["message_id"],
                 disable_notification=True)
        except Exception as e:  # noqa: BLE001 - an unpinned menu still works
            log.info("telegram: could not pin the menu: %s", e)

    def refresh_home(self, now: bool = False) -> None:
        """Edit the home card when what it should show has changed (now=True right after a tap)."""
        mid = home.home_msg()
        if not mid:
            return
        view = home.view()
        if view == self.home_shown:
            return
        self.home_shown = view
        self.editor.edit(mid, "text", view[0], view[1], now=now)

    def show(self, name: str, **data) -> None:
        home.set_screen(name, **data)

    def _queue_topic(self, topic_id: str, source: str, how: str) -> str:
        from .topics import get_topic
        try:
            topic = get_topic(topic_id)
        except KeyError:
            return "That topic is no longer in the list."
        if topic_id in ideas.library():
            self.show("home", note=f"ℹ️ {topic['name']} already has a reel or is in the queue.")
            return "That topic already has a reel."
        makequeue.add_job(topic_id, source=source)
        waits = makequeue.busy() or len(makequeue.jobs("queued")) > 1
        self.show("home", note=f"✅ {how}: {topic['name']}. " + ("It waits its turn in the make queue."
                                                                 if waits else "Making it now."))
        return f"Added: {topic['name']}"

    def _home_button(self, rest: str) -> str:
        action, _, arg = rest.partition(":")
        if action != "type":
            self._end_prompt(home.awaiting(), keep_caption=True)
        if action == "home":
            self.show("home")
            return "Home"
        if action == "new":
            self.show("new", page=0)
            return "Topic ideas"
        if action == "more":
            self.show("new", page=int(arg) if arg.isdigit() else 0)
            return "More ideas"
        if action == "pick":
            return self._queue_topic(arg, "pick", "Added to the make queue")
        if action == "surprise":
            t = ideas.surprise(self.rng)
            if not t:
                return "No topic is free right now."
            return self._queue_topic(t["id"], "surprise", "Surprise pick added to the make queue")
        if action == "type":
            return self.ask_topic()
        if action == "batch":
            self.show("batch")
            return "How many?"
        if action == "bn" and arg.isdigit():
            n = int(arg)
            makequeue.set_pending_batch(None)
            makequeue.set_pending_batch([t["id"] for t in ideas.pick_batch(n, self.rng)])
            self.show("batch_preview", n=n)
            return f"Here are {n} topics."
        if action == "bshuffle":
            s = home.screen()
            n = s.get("n") or len(makequeue.pending_batch()) or 7
            before = makequeue.pending_batch()
            makequeue.set_pending_batch(None)
            picks = ideas.pick_batch(n, self.rng, exclude=before)
            if len(picks) < n:   # not enough other topics: shuffle among all of them
                picks = ideas.pick_batch(n, self.rng)
            makequeue.set_pending_batch([t["id"] for t in picks])
            self.show("batch_preview", n=n)
            return "Shuffled."
        if action == "bgo":
            ids = makequeue.pending_batch()
            if not ids:
                self.show("home")
                return "That batch is gone. Make a new one."
            makequeue.start_batch(ids)
            self.show("home", note=f"📦 Batch of {len(ids)} started. Reels are made one at a time.")
            return "Started."
        if action == "bcancel":
            makequeue.set_pending_batch(None)
            self.show("home")
            return "Cancelled."
        if action == "review":
            return self.open_review()
        if action == "stats":
            self.show("stats")
            return "Stats"
        if action == "statsnow":
            from . import analytics
            started = analytics.collect_now()
            self.show("stats", note="🔄 Checking Instagram now; this screen updates in a minute." if started
                      else "🔄 Already checking; this screen updates in a minute.")
            return "Checking stats."
        if action == "account":
            self.show("account", text=account_text())
            return "Account"
        if action in ("retry", "drop"):
            return self._problem(action, arg)
        return STALE

    def _problem(self, action: str, arg: str) -> str:
        ref, _, platform = arg.partition(":")
        run = db.run_by_ref(int(ref)) if ref.isdigit() else None
        match = [p for p in home.problems() if run and p["run"]["id"] == run["id"]
                 and (p.get("platform") or "") == platform]
        if not match:
            return "That problem is already gone."
        p = match[0]
        home.dismiss(p["key"])
        if p["kind"] != "post":
            makequeue.dismiss_run(run["id"])
        if action == "drop":
            self.show("home")
            return "Dropped."
        name = review.topic_name(run["topic_id"])
        if p["kind"] == "post":
            try:
                if run["stage"] == "failed":
                    actions.retry(run["id"], "publish", "telegram")
                else:
                    actions.retry_platform(run["id"], platform, "telegram")
            except (ValueError, db.TransitionError) as e:
                return review.plain(str(e), 180)
            self.show("home", note=f"🔁 Posting {name} again.")
            return "Trying again."
        makequeue.retry(run["topic_id"])
        self.show("home", note=f"🔁 {name} is back in the make queue.")
        return "Trying again."

    # ---------------------------------------------------------- typed replies
    def ask_topic(self) -> str:
        self._end_prompt(home.awaiting(), keep_caption=True)
        prompt = send_message("⌨️ Which topic? Reply with a word or two, for example: coal, babies, internet.",
                              reply_markup={**FORCE_REPLY, "input_field_placeholder": "a topic"})
        home.set_awaiting({"kind": "topic", "prompt": prompt["message_id"]})
        self.show("type")
        return "Reply with your topic."

    def topic_reply(self, waiting: dict, m: dict, text: str) -> None:
        found = ideas.match(text)
        self._end_prompt(waiting)
        delete_message(m["message_id"])
        if found["clear"]:
            t = found["free"][0]
            self._queue_topic(t["id"], "typed", f"Added to the make queue (you typed “{text[:40]}”)")
        else:
            self.show("typed", query=text[:60], free=[t["id"] for t in found["free"]],
                      made=[t["id"] for t in found["made"]])
        self.refresh_home(now=True)

    def ask_caption(self, run: dict) -> str:
        self._end_prompt(home.awaiting())
        name = review.topic_name(run["topic_id"])
        prompt = send_message(f"✏️ Reply to this message with the new caption for {name}.\n"
                              "Hashtags on their own last line replace the current ones; leave them out to keep "
                              "them. The data credit is added when it posts.",
                              reply_markup={**FORCE_REPLY, "input_field_placeholder": "New caption"})
        home.set_awaiting({"kind": "caption", "run": run["id"], "prompt": prompt["message_id"], "cleanup": []})
        return "Reply with the new caption."

    def caption_reply(self, waiting: dict, m: dict, text: str) -> None:
        from .captions import CaptionRejected
        cleanup = list(waiting.get("cleanup") or []) + [m["message_id"]]
        try:
            actions.edit_caption(waiting["run"], text, None, "telegram")
        except ValueError as e:
            reasons = e.errors if isinstance(e, CaptionRejected) else [str(e)]
            delete_message(waiting["prompt"])
            prompt = send_message("❌ Not saved: " + " ".join(reasons) + "\n\nReply to this message with the "
                                  "caption again (your text is just above, to copy).",
                                  reply_markup={**FORCE_REPLY, "input_field_placeholder": "New caption"})
            home.set_awaiting({**waiting, "prompt": prompt["message_id"], "cleanup": cleanup})
            return
        home.set_awaiting(None)
        for mid in [waiting["prompt"], *cleanup]:
            delete_message(mid)
        st = review.card_state()
        if st and st["run"] == waiting["run"]:
            self.render_review(st, now=True)   # the same card now shows the new caption

    def _end_prompt(self, waiting: dict | None, keep_caption: bool = False) -> None:
        """Forget a question the bot asked (its message is deleted). keep_caption: a caption question stays
        open while the owner moves around the home card."""
        if not waiting or (keep_caption and waiting["kind"] == "caption"):
            return
        delete_message(waiting["prompt"])
        home.set_awaiting(None)

    # ---------------------------------------------------------- the review card
    def open_review(self) -> str:
        self.clear_ready_note()
        run = review.requested_run() or review.next_for_review()
        if not run:
            return "Nothing is waiting for review."
        old = review.card_state()
        if old:
            self._retire_card(old)
        self._send_card(run, [])
        n = len(review.queue())
        return f"{n} reel{'s' if n != 1 else ''} to review."

    def _send_card(self, run: dict, skipped: list[str]) -> None:
        text, markup = review.card_text(run, skipped), review.card_markup(run)
        video = run_dir(run["id"]) / "video.mp4"
        msg, kind = None, "text"
        if video.exists() and video.stat().st_size <= MAX_VIDEO_BYTES:
            try:
                with open(video, "rb") as f:
                    msg = call("sendVideo", http_timeout=300, files={"video": ("video.mp4", f, "video/mp4")},
                               chat_id=secret("TELEGRAM_CHAT_ID"), caption=text, supports_streaming="true",
                               width=1080, height=1920, reply_markup=markup)
                kind = "video"
            except TelegramError as e:
                if e.code != 400:
                    raise
                log.warning("telegram: sendVideo refused for %s (%s); sending a text card", run["id"], e)
        if msg is None:
            note = "(video over 50 MB: watch it in the dashboard)" if video.exists() else "(no video file: see the dashboard)"
            msg = send_message(review.fit(text, [note]), reply_markup=markup)
        review.set_card_state({"mid": msg["message_id"], "run": run["id"], "kind": kind, "skipped": skipped,
                               "shown": [text, markup]})
        review.clear_request(run["id"])
        self.editor.touch(msg["message_id"])
        db.log(run["id"], "awaiting_approval", "telegram: on the review card")

    def show_reel(self, st: dict, run: dict, skipped: list[str]) -> None:
        """The same review card moves to another reel (editMessageMedia); a text card, or a card Telegram
        will not edit any more, is replaced by a new one."""
        text, markup = review.card_text(run, skipped), review.card_markup(run)
        video = run_dir(run["id"]) / "video.mp4"
        if st["kind"] == "video" and video.exists() and video.stat().st_size <= MAX_VIDEO_BYTES:
            try:
                with open(video, "rb") as f:
                    call("editMessageMedia", http_timeout=300, files={"video": ("video.mp4", f, "video/mp4")},
                         chat_id=secret("TELEGRAM_CHAT_ID"), message_id=st["mid"], reply_markup=markup,
                         media={"type": "video", "media": "attach://video", "caption": text,
                                "supports_streaming": True, "width": 1080, "height": 1920})
                self.editor.forget(st["mid"])
                self.editor.touch(st["mid"])
                review.set_card_state({**st, "run": run["id"], "skipped": skipped, "shown": [text, markup]})
                review.clear_request(run["id"])
                db.log(run["id"], "awaiting_approval", "telegram: on the review card")
                return
            except TelegramError as e:
                log.info("telegram: could not switch the review card (%s); sending a new one", e)
        self._retire_card(st)
        self._send_card(run, skipped)

    def render_review(self, st: dict, now: bool = False) -> None:
        run = db.get_run(st["run"])
        if not run:
            return
        text, markup = review.card_text(run, st.get("skipped", [])), review.card_markup(run)
        if [text, markup] == st.get("shown"):
            return
        review.set_card_state({**st, "shown": [text, markup]})
        self.editor.edit(st["mid"], st["kind"], text, markup, now=now)

    def next_reel(self, st: dict, skipped: list[str]) -> str:
        run = review.next_for_review(skipped)
        if not run:
            self.close_review()
            left = len(review.queue())
            return (" That was the last one." if not left else
                    f" {left} skipped reel{'s stay' if left != 1 else ' stays'} in Ready for review.")
        self.show_reel(st, run, skipped)
        return ""

    def close_review(self) -> None:
        st = review.card_state()
        if st:
            self._retire_card(st)
        waiting = home.awaiting()
        if waiting and waiting["kind"] == "caption":
            self._end_prompt(waiting)

    def _retire_card(self, st: dict) -> None:
        self.editor.forget(st["mid"])
        if not delete_message(st["mid"]):
            try:
                self.editor.edit(st["mid"], st["kind"], "Review closed. Use the menu to open it again.",
                                 {"inline_keyboard": []}, now=True)
            except Exception as e:  # noqa: BLE001
                log.info("telegram: could not close the old review card: %s", e)
        review.set_card_state(None)

    def _review_button(self, rest: str, st: dict) -> str:
        parts = rest.split(":")
        if parts[0] == "home":
            self.close_review()
            self.show("home")
            return "Back to Home."
        if len(parts) < 2 or not parts[1].isdigit():
            return STALE
        run = db.run_by_ref(int(parts[1]))
        if not run or run["id"] != st["run"]:
            return STALE
        skipped = list(st.get("skipped") or [])
        if parts[0] == "cap" and len(parts) == 3 and parts[2].isdigit():
            try:
                answer = actions.choose_caption(run["id"], int(parts[2]), "telegram")
            except ValueError as e:
                return review.plain(str(e), 180)
            self.render_review(review.card_state(), now=True)
            return answer
        if parts[0] == "edit":
            return self.ask_caption(run)
        action = {"ok": "approve", "no": "reject", "skip": "skip"}.get(parts[0])
        if not action:
            return STALE
        waiting = home.awaiting()
        if waiting and waiting["kind"] == "caption" and waiting["run"] == run["id"]:
            self._end_prompt(waiting)   # decided without replying: the question goes away
        answer = review.decide(run["id"], action)
        if action == "skip":
            skipped.append(run["id"])
        return answer + self.next_reel(review.card_state() or st, skipped)

    def follow_review(self) -> None:
        """The dashboard sent a reel to Telegram, or decided the one on the card: the card follows."""
        wanted = review.requested_run()
        st = review.card_state()
        if wanted:
            self.clear_ready_note()
            if st and st["run"] != wanted["id"]:
                self.show_reel(st, wanted, st.get("skipped", []))
            elif not st:
                self._send_card(wanted, [])
            review.clear_request(wanted["id"])
            return
        if not st:
            return
        run = db.get_run(st["run"])
        if not run or run["stage"] != "awaiting_approval":
            self.next_reel(st, st.get("skipped", []))
        else:
            self.render_review(st)

    # ---------------------------------------------------------- short notes
    def clear_ready_note(self) -> None:
        mid = db.kv_get(K_READY_NOTE)
        if mid:
            delete_message(int(mid))
            db.kv_set(K_READY_NOTE, "")

    def notify_ready(self) -> None:
        """At most ONE "Reel ready" note: only when none is unread and the review card is not open."""
        ids = [r["id"] for r in review.queue()]
        seen = _json_kv(K_SEEN_READY)
        db.kv_set(K_SEEN_READY, json.dumps(ids))
        if not ids and db.kv_get(K_READY_NOTE):
            self.clear_ready_note()   # everything was reviewed elsewhere
        if seen is None:
            return   # first start: what is already waiting is on the home card
        new = [i for i in ids if i not in seen]
        if not new or review.card_state() or db.kv_get(K_READY_NOTE):
            return
        text, markup = home.ready_note(db.get_run(new[-1])["topic_id"])
        msg = send_message(text, reply_markup=markup)
        db.kv_set(K_READY_NOTE, str(msg["message_id"]))

    def announce_posts(self) -> None:
        """One short message per reel that finished posting, with its links."""
        done = db.list_runs(stage="published") + [r for r in db.list_runs(stage="failed") if r["failed_stage"] == "publish"]
        announced = _json_kv(K_ANNOUNCED)
        if announced is None:   # first start: earlier posts are not announced again
            db.kv_set(K_ANNOUNCED, json.dumps([r["id"] for r in done]))
            return
        fresh = [r for r in done if r["id"] not in announced and not review._publisher_alive(r["id"])]
        for run in reversed(fresh):   # oldest first
            text = home.posted_text(run)
            if text:
                send_message(text)
            announced.append(run["id"])
        if fresh:
            db.kv_set(K_ANNOUNCED, json.dumps(announced[-300:]))

    # ---------------------------------------------------------- the loop
    def tick(self) -> None:
        """Make the next reel, follow the review card, send the notes, then update the home card."""
        makequeue.work()
        self.follow_review()
        self.notify_ready()
        self.announce_posts()
        self.refresh_home()
        self.editor.flush()

    def poll_timeout(self) -> int:
        """Short polls while something on screen is changing, long ones when idle."""
        cfg = get_config()["telegram"]
        busy = bool(self.editor.pending or makequeue.jobs("queued", "running") or makequeue.making_now()
                    or db.list_runs(stage="publishing"))
        return int(cfg.get("busy_poll_timeout", 2) if busy else cfg.get("poll_timeout", 10))


_BOT: Bot | None = None


def get_bot() -> Bot:
    global _BOT
    if _BOT is None:
        _BOT = Bot()
    return _BOT


# ------------------------------------------------------------------ texts

def _count(n, word: str) -> str:
    return f"? {word}s" if n is None else f"{n} {word}" + ("" if n == 1 else "s")


def account_text() -> str:
    """The connected accounts, read live from the Graph API by IG_USER_ID and FB_PAGE_ID (never from
    cached names, so a renamed account shows its new name at once). Ids and tokens are not shown."""
    from . import meta_api
    from .publish_instagram import _check_json, base
    tok, ig, page = secret("IG_ACCESS_TOKEN"), secret("IG_USER_ID"), secret("FB_PAGE_ID")
    lines = ["👤 Connected accounts (live from Meta)"]
    if not tok:
        return "\n".join(lines + ["IG_ACCESS_TOKEN is missing: connect the accounts in Settings."])

    def read(url: str, fields: str, what: str) -> dict | str:
        try:
            return _check_json(meta_api.get(url, params={"fields": fields, "access_token": tok}, timeout=30), what)
        except Exception as e:  # noqa: BLE001 - one account failing must not hide the other
            return review.short_error(meta_api.redact(str(e)))

    if ig:
        d = read(f"{base()}/{ig}", "username,name,followers_count,media_count", "IG account")
        if isinstance(d, dict):
            lines += [f"📸 Instagram: @{d.get('username')}" + (f" · {d['name']}" if d.get("name") else ""),
                      f"     {_count(d.get('followers_count'), 'follower')} · {_count(d.get('media_count'), 'post')}"]
        else:
            lines.append(f"📸 Instagram: could not read it ({d})")
    else:
        lines.append("📸 Instagram: IG_USER_ID is missing")
    if page:
        d = read(f"{base('facebook')}/{page}", "name,followers_count,fan_count", "FB page")
        if isinstance(d, dict):
            lines.append(f"📘 Facebook Page: {d.get('name')} · "
                         f"{_count(d.get('followers_count', d.get('fan_count')), 'follower')}")
        else:
            lines.append(f"📘 Facebook Page: could not read it ({d})")
    else:
        lines.append("📘 Facebook Page: FB_PAGE_ID is missing")
    return "\n".join(lines)


def daily_summary_text() -> str:
    """The once-a-day stats message (analytics.send_summary): the Stats screen's numbers."""
    stats = home.stats_text().splitlines()
    return "\n".join([f"📊 {brand_name()} · daily summary ({datetime.now():%Y-%m-%d})", *stats[1:], "",
                      "Open the menu (/menu) for what is being made and what waits for you."])


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


def start(bot: Bot) -> None:
    """Once per bot start: the /menu command list, and the home card (sent and pinned the first time)."""
    try:
        register_commands()
    except Exception as e:  # noqa: BLE001 - /menu still works when typed
        log.warning("telegram: setMyCommands failed: %s", e)
    if not home.home_msg():
        bot.open_menu()
    else:
        bot.refresh_home(now=True)


def _bot_loop() -> int:
    db.init()
    tg = enabled()
    bot = get_bot()
    if not tg:
        log.warning("Telegram credentials missing: approval works from the dashboard only; "
                    "bot keeps running as the make queue and publish loop")
    else:
        ok, detail = check()
        log.info("telegram: %s", detail)
        if not ok:
            return 1
        try:
            ideas.pool()   # scores the cached topics once, so the first New reel answers at once
        except Exception as e:  # noqa: BLE001
            log.info("telegram: could not pre-score topics: %s", e)
        start(bot)
    last_scan = last_beat = 0.0
    while True:
        try:
            from .schedule import tick
            tick()
            if time.time() - last_beat >= health.HEARTBEAT_EVERY_S:
                health.beat("bot", telegram=tg)   # the dashboard turns red when this is > 3 min old
                last_beat = time.time()
            if time.time() - last_scan > 30:
                from .orchestrator import settle_stopped
                settle_stopped()   # a publisher that died mid-post is reported, not left "posting"
                _publish_stragglers()
                from . import analytics
                analytics.tick()   # stats every analytics.every_hours, on a background thread
                last_scan = time.time()
            if not tg:
                makequeue.work()
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
