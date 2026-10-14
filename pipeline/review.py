"""The review queue and the Telegram review card, shared by the bot and the dashboard.

The queue is every run waiting for review (stage awaiting_approval) in `runs.queue_pos` order; a run
joins the back when it reaches review. The bot shows ONE review card: a single message that shows one
reel at a time (the video, its opening question, the 3 caption options and the hashtags) and moves on to
the next ready reel after each decision, by editing itself. Skip for now sends a reel to the back of the
queue and leaves it alone until the card is opened again. Decisions go through actions.approve /
reject, so the dashboard and Telegram act on the same queue and a reel is never approved twice.

The dashboard can send one chosen reel to Telegram (send_to_telegram): the bot puts it on the review
card now. This module holds the state and the wording; approve_telegram.py does the Telegram calls.
No run id ever appears in a card.
"""
from __future__ import annotations

import json
import logging
import re

from . import db
from .config import country_by_iso3, get_config, path

log = logging.getLogger(__name__)

LIVE = {"live", "uploaded", "private_locked"}
CAPTION_LIMIT = 1024           # Telegram's caption limit
STALE = "This button is out of date. Use the menu."
K_CARD = "review:card"         # {"mid", "run", "kind", "skipped": [run ids skipped while the card was open]}
K_REQUESTED = "review:requested"   # the run the dashboard sent to Telegram, until the card shows it
RUN_ID = re.compile(r"r\d{8}-\d{6}-[0-9a-f]{4}")


# ------------------------------------------------------------------ queue order

ORDER = "COALESCE(queue_pos, rowid), rowid"


def queue() -> list[dict]:
    """Runs waiting for review, next first."""
    with db.db() as c:
        rows = c.execute(f"SELECT rowid AS ref, * FROM runs WHERE stage='awaiting_approval' ORDER BY {ORDER}").fetchall()
    return [db._run_dict(r) for r in rows]


def _move(run_id: str, edge: str) -> float:
    agg = "MAX(COALESCE(queue_pos, rowid)) + 1" if edge == "back" else "MIN(COALESCE(queue_pos, rowid)) - 1"
    with db.db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            pos = c.execute(f"SELECT {agg} FROM runs WHERE stage='awaiting_approval'").fetchone()[0]
            pos = 1 if pos is None else pos   # 0 and negatives are fine: the front keeps moving down
            c.execute("UPDATE runs SET queue_pos=? WHERE id=?", (pos, run_id))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return pos


def move_to_back(run_id: str) -> float:
    return _move(run_id, "back")


def move_to_front(run_id: str) -> float:
    return _move(run_id, "front")


def next_for_review(skipped=()) -> dict | None:
    """The next reel to show: the first in the queue that was not skipped while the card is open."""
    return next((r for r in queue() if r["id"] not in skipped), None)


# ------------------------------------------------------------------ the card's state

def card_state() -> dict | None:
    try:
        st = json.loads(db.kv_get(K_CARD) or "null")
    except ValueError:
        return None
    return st if isinstance(st, dict) and st.get("mid") else None


def set_card_state(state: dict | None) -> None:
    db.kv_set(K_CARD, json.dumps(state) if state else "")


def shown_run() -> str | None:
    st = card_state()
    return st["run"] if st else None


# ------------------------------------------------------------------ decisions

def decide(run_id: str, action: str) -> str:
    """Approve / Reject / Skip for now on the review card. Returns the short answer to show."""
    from . import actions
    run = db.get_run(run_id)
    if not run or run["stage"] != "awaiting_approval":
        return "Already decided in the dashboard."
    try:
        if action == "approve":
            actions.approve(run_id, "telegram")
            return approved_answer(run_id)
        if action == "reject":
            actions.reject(run_id, "telegram")
            return "Rejected: it won't be posted."
        if action == "skip":
            move_to_back(run_id)
            db.log(run_id, "awaiting_approval", "skipped for now via telegram: back of the review queue")
            return "Skipped for now: it stays in Ready for review."
    except db.TransitionError:   # decided in the dashboard a moment ago
        return "Already decided in the dashboard."
    except ValueError as e:
        return plain(str(e), 180)
    return STALE


def approved_answer(run_id: str) -> str:
    from .accounts import ready_destinations
    from .posting import when_for
    if not ready_destinations(db.get_run(run_id)):
        return "Approved. It posts once an account is connected (Settings)."
    return f"Approved: it posts {when_for(run_id)}."


# ------------------------------------------------------------------ wording

def short_error(message: str | None) -> str:
    from .errors import explain_error
    sentence = explain_error(message, "publish")["sentence"]
    if sentence.startswith("Something went wrong"):
        return plain(message)
    return sentence.rstrip(".")


def plain(message: str | None, limit: int = 120) -> str:
    """An error in one short line for the owner: no run ids, no exception class, no file paths."""
    text = " ".join((message or "unknown error").split())
    text = RUN_ID.sub("this video", text)
    text = re.sub(r"^(?:[A-Za-z_.]+(?:Error|Exception|Exhausted|Rejected)):\s*", "", text)
    text = re.sub(r"[A-Za-z]:\\\S+|/\S+/\S+", "a file", text)
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _meta(run_id: str) -> dict:
    try:
        return json.loads((path("out") / run_id / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _topic(topic_id: str) -> dict:
    from .topics import get_topic
    try:
        return get_topic(topic_id)
    except (KeyError, OSError, ValueError):
        return {}


def topic_name(topic_id: str) -> str:
    t = _topic(topic_id)
    return t.get("name") or t.get("title") or "a topic that was removed"


def video_title(run: dict) -> str:
    return run.get("title") or _meta(run["id"]).get("title") or _topic(run["topic_id"]).get("title") or "New video"


def _captions(run_id: str) -> dict | None:
    from . import captions
    try:
        return captions.load(run_id)
    except Exception:  # noqa: BLE001 - a card without caption options is still a card
        return None


def card_text(run: dict, skipped=()) -> str:
    """The review card's caption: which reel, its opening question, metric, countries, length, then the
    caption options (or the owner's own caption) and hashtags. No ids, no scores."""
    meta, topic = _meta(run["id"]), _topic(run["topic_id"])
    waiting = [r["id"] for r in queue() if r["id"] not in skipped]
    pos = waiting.index(run["id"]) + 1 if run["id"] in waiting else 1
    names = country_by_iso3()
    countries = [names[c]["name"] for c in run["countries"] if c in names] or \
                [c.get("name", c.get("iso3", "")) for c in meta.get("countries", [])]
    lines = [f"🎬 Reel {pos} of {max(len(waiting), pos)} for review: {topic.get('name') or video_title(run)}"]
    question = (meta.get("opening") or {}).get("question")
    lines.append(f"❓ Hook question: {question}" if question else f"❓ Title: {video_title(run)}")
    if topic.get("subtitle"):
        lines.append(f"📈 {topic['subtitle']}")
    facts = []
    if countries:
        facts.append("🌍 " + ", ".join(countries))
    if meta.get("duration_seconds"):
        facts.append(f"⏱ {round(meta['duration_seconds'])} s")
    if facts:
        lines.append(" · ".join(facts))
    base = "\n".join(lines)
    c = _captions(run["id"])
    tail = caption_lines(c) if c else []
    return fit(base, tail)


def caption_lines(c: dict) -> list[str]:
    from .captions import current, settings
    cur = current(c)
    if cur["edited"]:
        lines = ["✍️ Caption: your own text (Use caption 1-3 below goes back to an option)", cur["text"]]
    else:
        lines = ["✍️ Caption options (the ✔️ one gets posted):"]
        for i, v in enumerate(c["variants"]):
            lines += [f"{'✔️' if i == cur['index'] else '▫️'} {i + 1}. {v['hook']}", f"      {v['question']}"]
    if cur["hashtags"]:
        lines.append("🏷 " + " ".join(cur["hashtags"]))
    lines += [f"⚠️ {w}" for w in cur["warnings"]]
    if not settings()["use_for_posting"]:
        lines.append("ℹ️ Preview only: posts keep the old caption until captions.use_for_posting is on")
    return lines


def card_markup(run: dict) -> dict:
    ref = run["ref"]
    rows = []
    c = _captions(run["id"])
    if c:
        from .captions import current
        cur = current(c)
        rows.append([{"text": f"✔️ Caption {i + 1}" if (i == cur["index"] and not cur["edited"]) else f"Use caption {i + 1}",
                      "callback_data": f"v:cap:{ref}:{i}"} for i in range(len(c["variants"]))])
        rows.append([{"text": "✏️ Edit caption", "callback_data": f"v:edit:{ref}"}])
    rows += [[{"text": APPROVE_LABEL, "callback_data": f"v:ok:{ref}"}],
             [{"text": "❌ Reject: don't post", "callback_data": f"v:no:{ref}"},
              {"text": "⏭ Skip for now", "callback_data": f"v:skip:{ref}"}],
             [{"text": "🏠 Home", "callback_data": "v:home"}]]
    return {"inline_keyboard": rows}


APPROVE_LABEL = "✅ Approve: add to posting queue"


def fit(base: str, tail: list[str], limit: int = CAPTION_LIMIT) -> str:
    """base + tail within Telegram's caption limit; the tail (caption options) gives way first."""
    end = ("\n\n" + "\n".join(tail)) if tail else ""
    room = limit - len(base)
    if len(end) > room:
        end = end[:max(room - 1, 0)] + "…"
    return (base + end)[:limit]


# ------------------------------------------------------------------ going back to review

def back_to_review(run_id: str, why: str) -> None:
    """Move a run (not posted anywhere) back to review: forget its earlier approval, platform
    choice and every not-live post record, so it posts only after a fresh approval."""
    with db.db() as c:
        gone = [r["platform"] for r in c.execute("SELECT platform FROM posts WHERE run_id=? AND status NOT IN "
                                                 "('live','uploaded','private_locked')", (run_id,))]
        c.execute("DELETE FROM posts WHERE run_id=? AND status NOT IN ('live','uploaded','private_locked')", (run_id,))
    note = f"; removed test/failed post records: {', '.join(sorted(gone))}" if gone else ""
    db.transition(run_id, "awaiting_approval", why + note, reset=True, platforms=None)


def _posted(run_id: str) -> bool:
    return any(p["status"] in LIVE for p in db.get_posts(run_id))


def _publisher_alive(run_id: str) -> bool:
    from .lock import held_elsewhere, publish_lock
    return held_elsewhere(publish_lock(run_id))


def reset_test_approvals(by: str) -> list[dict]:
    """Before test mode goes off: every run that was approved or queued for posting while in test
    mode (approved / publishing / published / publish-failed, with no live post) goes back to
    awaiting review, oldest first, so it comes back through the queue and needs a fresh approval."""
    out = []
    for run in sorted(db.list_runs(limit=100000), key=lambda r: (r["created_at"], r["ref"])):
        approved = run["stage"] in ("approved", "publishing", "published") or \
            (run["stage"] == "failed" and run["failed_stage"] == "publish")
        if not approved or _posted(run["id"]) or _publisher_alive(run["id"]):
            continue
        # "telegram: sent" keeps a still-running old bot (it matches that text) from re-sending it
        back_to_review(run["id"], f"back to review via {by}: approved while in test mode, so it needs a fresh "
                                  f"approval (earlier telegram: sent cards are stale)")
        out.append({"id": run["id"], "topic_id": run["topic_id"], "was": run["stage"], "title": video_title(run)})
    return out


# ------------------------------------------------------------------ Send to Telegram (dashboard)

def requested_run() -> dict | None:
    """The reel the dashboard sent to Telegram, while it still waits for review and for the card."""
    run_id = db.kv_get(K_REQUESTED)
    run = db.get_run(run_id) if run_id else None
    if run and run["stage"] == "awaiting_approval":
        return run
    if run_id:
        clear_request(run_id)   # decided, remade or removed in the meantime
    return None


def clear_request(run_id: str) -> None:
    with db.db() as c:
        c.execute("UPDATE kv SET value='', updated_at=? WHERE key=? AND value=?", (db.now(), K_REQUESTED, run_id))


def send_to_telegram(run_id: str, by: str) -> str:
    """Ask the bot to show this reel on its review card now (it moves to the front of the queue).
    Nothing is approved or posted by this."""
    run = db.get_run(run_id)
    if not run or run["stage"] != "awaiting_approval":
        raise ValueError("Only a video waiting for review can be sent to Telegram.")
    if shown_run() == run_id:
        return "This video is already on Telegram."
    move_to_front(run_id)
    db.kv_set(K_REQUESTED, run_id)
    db.log(run_id, "awaiting_approval", f"sent to Telegram for approval via {by}")
    return "Sent to Telegram — approve it there."


def telegram_state(run_id: str) -> str | None:
    """Where a waiting reel stands on Telegram, for the dashboard: "card" (the review card shows it),
    "next" (sent from the dashboard, the card is on its way) or None."""
    if shown_run() == run_id:
        return "card"
    return "next" if db.kv_get(K_REQUESTED) == run_id else None


# ------------------------------------------------------------------ topics

_SCORES: dict[tuple, float] = {}


def topic_score(topic: dict) -> float:
    """Mean scorer score of the topic's best countries, from the local series cache only (never
    the network). 0 when the topic has not been fetched yet."""
    from .fetch import load_cached
    from .score import score_topic
    data = load_cached(topic["id"])
    if not data or not data.get("series"):
        return 0.0
    key = (topic["id"], data.get("fetched_at"), topic.get("start_year"), topic.get("min_meaningful"))
    if key not in _SCORES:
        best = [s["score"] for s in score_topic(data, topic) if s["ok"]][:get_config()["picker"]["max_countries"]]
        _SCORES[key] = round(sum(best) / len(best), 2) if len(best) >= get_config()["picker"]["min_countries"] else 0.0
    return _SCORES[key]


def skip_reason(run_id: str) -> str:
    """Why a run was skipped, in plain words."""
    with db.db() as c:
        r = c.execute("SELECT reason FROM topic_events WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    text = (r["reason"] if r else "").lower()
    if "low variety" in text:
        return "every country moved the same way, so there was no story to hear"
    if "already" in text and "used" in text:
        return "every mix of countries for it has been used already"
    return "not enough countries had interesting data"
