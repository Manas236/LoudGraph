"""The Telegram home card: ONE pinned message that the bot keeps editing, plus the screens it turns into
(New reel, Make a batch, Stats, Account). Everything here is wording and state; approve_telegram.py does
the Telegram calls. Every screen has a Home button, every button label says what it does, and no run id
is ever shown.

The home card says, in plain words: what is being made now (step X of 4), how many topics wait to be
made, how many reels wait for review, how many approved reels wait to post, what was posted last, and
the newest problem (a reel that could not be made, a skipped topic, a failed post) with Retry and Drop.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db, makequeue, review
from .accounts import PLATFORMS, test_mode
from .config import brand_name, get_config

K_MSG = "home:msg"            # the home card's message id
K_SCREEN = "home:screen"      # {"name": ..., ...} what the home card shows now
K_AWAIT = "home:await"        # {"kind": "topic" | "caption", "prompt": message id, "run": run id}
PROBLEM_DAYS = 3              # problems older than this are left to the dashboard's Library
STEPS = {"queued": (1, "getting data"), "data_ready": (2, "rendering"), "picked": (2, "rendering"),
         "labelled": (2, "rendering"), "rendered": (3, "writing captions"), "awaiting_approval": (4, "ready")}
BATCH_SIZES = (3, 5, 7)
IDEAS_PER_PAGE = 5
HOME = {"text": "🏠 Home", "callback_data": "h:home"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def tz() -> ZoneInfo:
    return ZoneInfo(get_config()["cadence"].get("timezone", "Asia/Kolkata"))


def when(ts: str | datetime, now: datetime | None = None) -> str:
    """'today 16:48 IST', 'yesterday 21:00 IST', 'Thu 15 Oct, 21:00 IST': absolute, so the card does not
    need an edit every minute."""
    t = (db.parse_ts(ts) if isinstance(ts, str) else ts).astimezone(tz())
    today = (now or _now()).astimezone(tz()).date()
    days = (t.date() - today).days
    day = {0: "today", -1: "yesterday", 1: "tomorrow"}.get(days, f"{t:%a} {t.day} {t:%b},")
    return f"{day} {t:%H:%M} {t.tzname()}"


# ------------------------------------------------------------------ kv state

def _json(key: str):
    try:
        return json.loads(db.kv_get(key) or "null")
    except ValueError:
        return None


def screen() -> dict:
    s = _json(K_SCREEN)
    return s if isinstance(s, dict) and s.get("name") else {"name": "home"}


NOTE_S = 10 * 60               # a one-line note ("Added to the make queue: ...") shows this long


def set_screen(name: str, **data) -> dict:
    s = {"name": name, **data}
    if s.get("note"):
        s["note_at"] = db.now()
    db.kv_set(K_SCREEN, json.dumps(s))
    return s


def note(s: dict, now: datetime | None = None) -> str | None:
    """The screen's note while it is fresh, so a finished action does not linger on the card."""
    if not s.get("note") or not s.get("note_at"):
        return None
    return s["note"] if ((now or _now()) - db.parse_ts(s["note_at"])).total_seconds() < NOTE_S else None


def awaiting() -> dict | None:
    a = _json(K_AWAIT)
    return a if isinstance(a, dict) and a.get("kind") else None


def set_awaiting(a: dict | None) -> None:
    db.kv_set(K_AWAIT, json.dumps(a) if a else "")


def home_msg() -> int | None:
    v = db.kv_get(K_MSG)
    return int(v) if v and v.isdigit() else None


# ------------------------------------------------------------------ status lines

def making_line() -> str:
    run = None
    running = makequeue.jobs("running")
    if running and running[0]["run_id"]:
        run = db.get_run(running[0]["run_id"])
    if run and run["stage"] not in STEPS:   # it just failed or was skipped: the problem line says so
        run = None
    if not run:
        others = makequeue.making_now()
        run = others[0] if others else None
    if not run:
        return "🛠 Making now: nothing"
    step, word = STEPS.get(run["stage"], (2, "rendering"))
    if word == "rendering" and run.get("progress") is not None:
        word += f" {int(run['progress'] * 10) * 10}%"   # 10% steps: fewer edits
    return f"🛠 Making now: {review.topic_name(run['topic_id'])} (step {step} of 4: {word})"


def waiting_line() -> str:
    q = makequeue.jobs("queued")
    if not q:
        return "⏳ Waiting to make: 0"
    names = [review.topic_name(j["topic_id"]) for j in q[:3]]
    return f"⏳ Waiting to make: {len(q)} ({', '.join(names)}{', …' if len(q) > 3 else ''})"


def batch_line() -> str | None:
    b = makequeue.active_batch()
    return f"📦 Batch: {b['ready']} of {b['asked']} ready" if b else None


def approved_line() -> str:
    n = len(db.list_runs(stage="approved"))
    return f"📅 Approved, waiting to post: {n}"


def posting_now_line() -> str | None:
    runs = db.list_runs(stage="publishing")
    if not runs:
        return None
    return f"📤 Posting now: {review.topic_name(runs[0]['topic_id'])}"


def last_posted() -> dict | None:
    posts = [p for p in db.get_posts() if p["status"] in review.LIVE]
    if not posts:
        return None
    p = max(posts, key=lambda p: p["created_at"])
    run = db.get_run(p["run_id"]) or {"topic_id": ""}
    views = [s["views"] for s in db.latest_stats() if s["run_id"] == p["run_id"] and s["views"] is not None
             and s["platform"] == "instagram"]
    return {"run": run, "post": p, "views": views[0] if views else None}


def last_posted_line(now: datetime | None = None) -> str:
    lp = last_posted()
    if not lp:
        return "📣 Last posted: nothing yet"
    views = f", {_n(lp['views'], 'view')}" if lp["views"] is not None else ""
    return f"📣 Last posted: {review.topic_name(lp['run']['topic_id'])}, {when(lp['post']['created_at'], now)}{views}"


def test_mode_line() -> str | None:
    tm = test_mode()
    if tm["on"]:
        return "🧪 Test mode: nothing is really posted"
    if tm["testing"]:
        return "🧪 Test mode on for " + ", ".join(PLATFORMS[p] for p in tm["testing"])
    return None


# ------------------------------------------------------------------ problems

def _dismissed(key: str) -> bool:
    return db.kv_get("dismissed:problem:" + key) == "1"


def dismiss(key: str) -> None:
    db.kv_set("dismissed:problem:" + key, "1")


def problems() -> list[dict]:
    """Recent things that went wrong and were not dropped, newest first: a reel that could not be made,
    a topic you picked that was skipped (batch topics are replaced instead), a failed post."""
    from .errors import explain_error
    out = []
    for run in db.list_runs(limit=300):
        name = review.topic_name(run["topic_id"])
        job = makequeue.job_for_run(run["id"])
        if job and job["dismissed"]:
            continue
        recent = _age(run["updated_at"]) < PROBLEM_DAYS * 86400
        key = f"run:{run['id']}:{run['updated_at']}"
        if recent and run["stage"] == "failed" and run["failed_stage"] != "publish" and not _dismissed(key):
            reason = explain_error(run["error"], run["failed_stage"] or "render")
            why = review.plain(run["error"]) if reason["sentence"].startswith("Something went wrong") \
                else reason["sentence"].rstrip(".")
            out.append({"key": key, "kind": "make", "run": run, "ts": run["updated_at"],
                        "text": f"⚠️ Rendering failed for {name}: {why}."})
        elif recent and run["stage"] == "skipped" and job and not job["batch_id"] and not _dismissed(key):
            out.append({"key": key, "kind": "skip", "run": run, "ts": run["updated_at"],
                        "text": f"😴 {name} was skipped: {review.skip_reason(run['id'])}. Nothing broke."})
        if run["stage"] == "publishing" or review._publisher_alive(run["id"]):
            continue
        for p in db.get_posts(run["id"]):
            pkey = f"post:{p['id']}:{p['updated_at']}"
            if p["status"] == "failed" and _age(p["updated_at"]) < PROBLEM_DAYS * 86400 and not _dismissed(pkey):
                out.append({"key": pkey, "kind": "post", "run": run, "platform": p["platform"], "ts": p["updated_at"],
                            "text": f"⚠️ Posting failed for {name} on {PLATFORMS[p['platform']]}: "
                                    f"{review.short_error(p['message'])}."})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


def _age(ts: str | None) -> float:
    return (_now() - db.parse_ts(ts)).total_seconds() if ts else float("inf")


def problem_rows(p: dict) -> list[list[dict]]:
    ref = p["run"]["ref"]
    name = review.topic_name(p["run"]["topic_id"])
    if p["kind"] == "post":
        retry = {"text": f"🔁 Retry posting to {PLATFORMS[p['platform']]}", "callback_data": f"h:retry:{ref}:{p['platform']}"}
    else:
        retry = {"text": f"🔁 Retry {name}"[:60], "callback_data": f"h:retry:{ref}"}
    return [[retry, {"text": "🗑 Drop it", "callback_data": f"h:drop:{ref}" + (f":{p['platform']}" if p["kind"] == "post" else "")}]]


# ------------------------------------------------------------------ screens

def view(s: dict | None = None, now: datetime | None = None) -> tuple[str, dict]:
    """(text, reply_markup) for the home card showing screen s (the stored one by default)."""
    s = s or screen()
    fn = SCREENS.get(s["name"], home_view)
    return fn(s, now) if fn is home_view else fn(s)


def home_view(s: dict | None = None, now: datetime | None = None) -> tuple[str, dict]:
    s = s or {}
    lines = [f"🏠 {brand_name()}"]
    if note(s, now):
        lines.append(note(s, now))
    lines.append("")
    lines += [making_line(), waiting_line()]
    if batch_line():
        lines.append(batch_line())
    ready = len(review.queue())
    lines += [f"👀 Ready for your review: {ready}", approved_line()]
    if posting_now_line():
        lines.append(posting_now_line())
    lines.append(last_posted_line(now))
    if test_mode_line():
        lines.append(test_mode_line())
    rows = []
    probs = problems()
    if probs:
        more = len(probs) - 1
        lines += ["", probs[0]["text"] + (f"\n({more} more after this one)" if more else "")]
        rows += problem_rows(probs[0])
    rows += [[{"text": "🆕 New reel", "callback_data": "h:new"}, {"text": "📦 Make a batch", "callback_data": "h:batch"}],
             [{"text": f"👀 Review ready reels ({ready})", "callback_data": "h:review"}],
             [{"text": "📊 Stats", "callback_data": "h:stats"}, {"text": "👤 Account", "callback_data": "h:account"}]]
    return "\n".join(lines), {"inline_keyboard": rows}


def new_view(s: dict) -> tuple[str, dict]:
    from .ideas import suggestions
    ideas = suggestions(IDEAS_PER_PAGE, s.get("page", 0))
    lines = ["🆕 New reel", "", "Pick a topic that has no reel yet. It joins the make queue and the home card "
             "shows it being made."]
    if note(s):
        lines += ["", note(s)]
    if not ideas:
        lines += ["", "Every interesting topic already has a reel or is resting. Try Surprise me later, or add "
                  "topics in Settings."]
    rows = [[{"text": f"{t['name']} · {t.get('category', '')}", "callback_data": f"h:pick:{t['id']}"}] for t in ideas]
    rows += [[{"text": "🔄 More ideas", "callback_data": f"h:more:{s.get('page', 0) + 1}"},
              {"text": "🎲 Surprise me", "callback_data": "h:surprise"}],
             [{"text": "⌨️ Type my own topic", "callback_data": "h:type"}],
             [{"text": "🏠 Back to Home", "callback_data": "h:home"}]]
    return "\n".join(lines), {"inline_keyboard": rows}


def type_view(s: dict) -> tuple[str, dict]:
    text = ("⌨️ Type my own topic\n\nReply to my message below with a word or two, for example: coal, babies, "
            "internet, murder. I'll find the dataset that matches.")
    return text, {"inline_keyboard": [[{"text": "⬅️ Back to topic ideas", "callback_data": "h:new"}], [HOME]]}


def typed_view(s: dict) -> tuple[str, dict]:
    """The matches for what the owner typed."""
    from .topics import load_topics
    by_id = {t["id"]: t for t in load_topics()}
    free = [by_id[i] for i in s.get("free", []) if i in by_id]
    made = [by_id[i] for i in s.get("made", []) if i in by_id]
    lines = [f"⌨️ You typed: “{s.get('query', '')}”", ""]
    if free:
        lines.append("Tap the one you meant:")
    elif made:
        lines.append("That topic already has a reel (or is being made): " + ", ".join(t["name"] for t in made) + ".")
    else:
        lines.append("I don't have a dataset for that. Try another word, or pick from the ideas.")
    rows = [[{"text": f"{t['name']} · {t.get('category', '')}", "callback_data": f"h:pick:{t['id']}"}] for t in free]
    rows += [[{"text": "⌨️ Type again", "callback_data": "h:type"},
              {"text": "⬅️ Topic ideas", "callback_data": "h:new"}], [HOME]]
    return "\n".join(lines), {"inline_keyboard": rows}


def batch_view(s: dict) -> tuple[str, dict]:
    text = ("📦 Make a batch\n\nHow many reels? I'll pick distinct topics you haven't made, from different "
            "categories, and make them one at a time.")
    rows = [[{"text": f"{n} reels", "callback_data": f"h:bn:{n}"} for n in BATCH_SIZES], [HOME]]
    return text, {"inline_keyboard": rows}


def batch_preview_view(s: dict) -> tuple[str, dict]:
    from .topics import load_topics
    by_id = {t["id"]: t for t in load_topics()}
    ids = [i for i in makequeue.pending_batch() if i in by_id]
    if not ids:
        return ("📦 Make a batch\n\nI couldn't find enough topics that have no reel yet. Try a smaller batch.",
                {"inline_keyboard": [[{"text": "📦 Choose another size", "callback_data": "h:batch"}], [HOME]]})
    lines = [f"📦 Batch of {len(ids)}, not started yet:", ""]
    lines += [f"{i}. {by_id[t]['name']} ({by_id[t].get('category', '')})" for i, t in enumerate(ids, 1)]
    lines += ["", "They are made one at a time, in this order, and wait for your review."]
    if len(ids) < s.get("n", len(ids)):
        lines.append(f"Only {len(ids)} topics are free right now.")
    rows = [[{"text": f"▶️ Start making these {len(ids)}", "callback_data": "h:bgo"}],
            [{"text": "🔀 Shuffle: pick others", "callback_data": "h:bshuffle"},
             {"text": "✖️ Cancel", "callback_data": "h:bcancel"}], [HOME]]
    return "\n".join(lines), {"inline_keyboard": rows}


def _n(n, word: str) -> str:
    n = n or 0
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def stats_text(limit: int = 6) -> str:
    rows = [s for s in db.latest_stats() if s["status"] in review.LIVE]
    raw = {}
    with db.db() as c:
        for r in c.execute("SELECT post_id, raw FROM stats WHERE id IN (SELECT MAX(id) FROM stats GROUP BY post_id)"):
            try:
                raw[r["post_id"]] = json.loads(r["raw"] or "{}")
            except ValueError:
                pass
    ig = [s for s in rows if s["platform"] == "instagram"][:limit]
    last = db.kv_get("stats:last_run")
    lines = ["📊 Stats" + (f" (Instagram, checked {when(last)})" if last else " (not checked yet)"), ""]
    if not ig:
        lines.append("No Instagram posts with stats yet.")
    for s in ig:
        r = raw.get(s["post_id"], {})
        bits = [_n(s["views"], "view") if s["views"] is not None else "views not in yet"]
        if s.get("avg_view_pct") is not None:
            bits.append(f"{s['avg_view_pct']:.0f}% watched")
        if r.get("reels_skip_rate") is not None:
            bits.append(f"{r['reels_skip_rate']:.0f}% skipped in 3 s")
        bits.append(", ".join(_n(v, w) for v, w in ((s["likes"], "like"), (r.get("shares"), "share"), (r.get("saved"), "save"))))
        lines.append(f"• {review.topic_name(s['topic_id'])} ({when(s['posted_at'])}): " + ", ".join(bits))
    views = [s["views"] for s in rows if s["platform"] == "instagram" and s["views"] is not None]
    if views:
        lines += ["", f"Average: {_n(round(sum(views) / len(views)), 'view')} over {_n(len(views), 'reel')}."]
    return "\n".join(lines)


def stats_view(s: dict) -> tuple[str, dict]:
    text = stats_text() + ("\n\n" + note(s) if note(s) else "")
    return text, {"inline_keyboard": [[{"text": "🔄 Check stats now", "callback_data": "h:statsnow"}], [HOME]]}


def account_view(s: dict) -> tuple[str, dict]:
    text = s.get("text") or "👤 Reading the accounts…"
    return text, {"inline_keyboard": [[{"text": "🔄 Read again", "callback_data": "h:account"}], [HOME]]}


SCREENS = {"home": home_view, "new": new_view, "type": type_view, "typed": typed_view, "batch": batch_view,
           "batch_preview": batch_preview_view, "stats": stats_view, "account": account_view}


# ------------------------------------------------------------------ short messages

def ready_note(topic_id: str) -> tuple[str, dict]:
    return (f"🔔 Reel ready: {review.topic_name(topic_id)}. Tap Review.",
            {"inline_keyboard": [[{"text": "👀 Review it now", "callback_data": "r:open"}]]})


def posted_text(run: dict) -> str | None:
    """The one short message after a reel posts: its links, or why a platform failed. None when nothing
    went live and nothing was a test (a failure shows on the home card instead)."""
    posts = db.get_posts(run["id"])
    if not any(p["status"] in review.LIVE | {"dry_run"} for p in posts):
        return None
    name = review.topic_name(run["topic_id"])
    live = any(p["status"] in review.LIVE for p in posts)
    lines = [f"✅ Posted: {name}" if live else f"🧪 Test mode: {name} was not really posted"]
    icon = {"youtube": "▶️", "instagram": "📸", "facebook": "📘"}
    for p in sorted(posts, key=lambda p: list(PLATFORMS).index(p["platform"])):
        if p["status"] in review.LIVE:
            lines.append(f"{icon[p['platform']]} {PLATFORMS[p['platform']]}: {p['url'] or 'posted'}")
        elif p["status"] == "failed":
            lines.append(f"{icon[p['platform']]} {PLATFORMS[p['platform']]}: failed ({review.short_error(p['message'])})")
    return "\n".join(lines)
