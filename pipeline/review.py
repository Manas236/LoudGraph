"""The one-at-a-time review queue, shared by the Telegram bot and the dashboard.

The queue is every run waiting for review (stage awaiting_approval) in `runs.queue_pos` order; a run
joins the back when it reaches review. The bot shows at most ONE review card at a time: a card holds
`slot = 1` while it is sending, open or posting, and the UNIQUE slot column makes a second one
impossible. An approved card follows the posting until every platform is posted or failed (or a
stage takes longer than STAGE_TIMEOUT_S); only then is the slot freed and the next card sent.

Decisions go through the run's state machine (actions.approve / reject), so the dashboard and
Telegram act on the same queue, and a second approval of the same run is refused: it posts once.

/new production requests are make_jobs; one runs at a time and the rest wait their turn.
This module holds the state and the card wording; approve_telegram.py does the Telegram calls.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import db
from .accounts import PLATFORMS
from .config import country_by_iso3, get_config, path

log = logging.getLogger(__name__)

ACTIVE = ("sending", "open", "posting")
LIVE = {"live", "uploaded", "private_locked"}
FINAL = LIVE | {"dry_run", "failed"}
MAKING = ("queued", "data_ready", "picked", "labelled", "rendered")
STAGE_TIMEOUT_S = 10 * 60      # a posting stage with no progress for this long is marked failed on the card
LATER_REST_S = 15 * 60         # "Later" on the only waiting video does not bring it straight back
WATCH_FOR_S = 2 * 3600         # a finished card keeps following a slow or retried upload this long
RECENT_S = 10 * 60             # closed cards are re-rendered this long (a deferred edit still lands)
START_GRACE_S = 120            # a just-spawned maker gets this long to take its run lock
CAPTION_LIMIT = 1024           # Telegram's caption limit
ICON = {"youtube": "▶️", "instagram": "📸", "facebook": "📘"}
STALE = "This card is stale"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age(ts: str | None, now: datetime | None = None) -> float:
    return ((now or _now()) - db.parse_ts(ts)).total_seconds() if ts else float("inf")


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


def next_for_review() -> dict | None:
    """The next run to show. A run just sent to the back with Later rests a while if it is the
    only one waiting, so it does not come straight back."""
    last = last_closed_card()
    for run in queue():
        if (last and last["outcome"] == "later" and last["run_id"] == run["id"]
                and run["queue_pos"] == last["later_pos"] and _age(last["closed_at"]) < LATER_REST_S):
            continue
        return run
    return None


def paused() -> bool:
    return db.kv_get("review_paused") == "1"


def set_paused(flag: bool) -> None:
    db.kv_set("review_paused", "1" if flag else "0")


# ------------------------------------------------------------------ cards

JSON_FIELDS = ("buttons", "platforms", "overrides")


def _card(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS:
        d[k] = json.loads(d[k]) if d.get(k) else ({} if k == "overrides" else None)
    return d


def current_card() -> dict | None:
    """The one card that is sending, open or posting, if any."""
    with db.db() as c:
        return _card(c.execute("SELECT * FROM review_cards WHERE slot=1").fetchone())


def get_card(card_id: int) -> dict | None:
    with db.db() as c:
        return _card(c.execute("SELECT * FROM review_cards WHERE id=?", (card_id,)).fetchone())


def latest_card_for_run(run_id: str) -> dict | None:
    with db.db() as c:
        return _card(c.execute("SELECT * FROM review_cards WHERE run_id=? ORDER BY id DESC LIMIT 1",
                               (run_id,)).fetchone())


def last_closed_card() -> dict | None:
    with db.db() as c:
        return _card(c.execute("SELECT * FROM review_cards WHERE state IN ('done','closed') AND closed_at IS NOT NULL "
                               "ORDER BY closed_at DESC, id DESC LIMIT 1").fetchone())


def cards_to_render(now: datetime | None = None) -> list[dict]:
    """Cards whose Telegram message may need an edit: the current one, finished cards still
    following an upload, and cards closed in the last few minutes."""
    since = ((now or _now()).timestamp() - RECENT_S)
    since_ts = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.db() as c:
        rows = c.execute("SELECT * FROM review_cards WHERE message_id IS NOT NULL AND (slot=1 OR watch=1 OR updated_at>=?) "
                         "ORDER BY id", (since_ts,)).fetchall()
    return [_card(r) for r in rows]


def create_card(run_id: str) -> int | None:
    """Take the single slot for a new card. None if another card already holds it."""
    ts = db.now()
    try:
        with db.db() as c:
            return c.execute("INSERT INTO review_cards(run_id, slot, state, created_at, updated_at) VALUES (?,1,'sending',?,?)",
                             (run_id, ts, ts)).lastrowid
    except db.sqlite3.IntegrityError:
        return None


def update_card(card_id: int, **fields) -> None:
    for k in JSON_FIELDS:
        if k in fields and fields[k] is not None:
            fields[k] = json.dumps(fields[k])
    fields["updated_at"] = db.now()
    with db.db() as c:
        c.execute(f"UPDATE review_cards SET {', '.join(k + '=?' for k in fields)} WHERE id=?", [*fields.values(), card_id])


def claim_card(card_id: int, from_state: str, **fields) -> bool:
    """Atomically change a card that is still in `from_state` (a repeated button press loses)."""
    for k in JSON_FIELDS:
        if k in fields and fields[k] is not None:
            fields[k] = json.dumps(fields[k])
    fields["updated_at"] = db.now()
    with db.db() as c:
        cur = c.execute(f"UPDATE review_cards SET {', '.join(k + '=?' for k in fields)} WHERE id=? AND state=?",
                        [*fields.values(), card_id, from_state])
    return cur.rowcount == 1


def close_card(card_id: int, from_state: str, state: str, outcome: str, **fields) -> bool:
    """Finish a card (state done or closed) and free the slot so the queue advances."""
    return claim_card(card_id, from_state, state=state, outcome=outcome, slot=None, closed_at=db.now(), **fields)


def drop_card(card_id: int) -> None:
    """The card could not be sent: forget it so the run is offered again."""
    with db.db() as c:
        c.execute("DELETE FROM review_cards WHERE id=? AND message_id IS NULL", (card_id,))


def card_sent(card_id: int, message_id: int, kind: str, caption: str, buttons: dict) -> None:
    update_card(card_id, message_id=message_id, kind=kind, state="open", caption=caption, buttons=buttons)


# ------------------------------------------------------------------ decisions

def _approved_at(run_id: str) -> str:
    with db.db() as c:
        r = c.execute("SELECT ts FROM stage_log WHERE run_id=? AND stage='approved' ORDER BY id DESC LIMIT 1",
                      (run_id,)).fetchone()
    return r["ts"] if r else db.now()


def start_posting(card: dict, via: str) -> bool:
    from .accounts import ready_destinations
    run = db.get_run(card["run_id"])
    return claim_card(card["id"], "open", state="posting", decided_via=via, outcome="approved",
                      platforms=ready_destinations(run), approved_at=_approved_at(run["id"]))


def sync_open(card: dict) -> dict:
    """An open card whose run was decided elsewhere (the dashboard) follows that decision."""
    run = db.get_run(card["run_id"])
    stage = run["stage"] if run else None
    if stage == "awaiting_approval":
        return card
    if stage in ("approved", "publishing", "published") or (stage == "failed" and run["failed_stage"] == "publish"):
        start_posting(card, "dashboard")
    elif stage == "rejected":
        close_card(card["id"], "open", "closed", "remade" if run.get("replaced") else "rejected", decided_via="dashboard")
    else:
        close_card(card["id"], "open", "closed", "gone", decided_via="dashboard")
    return get_card(card["id"])


def decide(card: dict, action: str) -> str:
    """A Telegram Approve / Reject / Later on the current card. Returns the answer to show."""
    from . import actions
    if card["state"] == "posting":
        return "Already approved — it is posting."
    if card["state"] != "open":
        return STALE
    run_id = card["run_id"]
    run = db.get_run(run_id)
    if not run or run["stage"] != "awaiting_approval":
        sync_open(card)
        return "Already handled in the dashboard."
    try:
        if action == "approve":
            actions.approve(run_id, "telegram")
            start_posting(card, "telegram")
            return "Approved — posting now."
        if action == "reject":
            actions.reject(run_id, "telegram")
            close_card(card["id"], "open", "closed", "rejected", decided_via="telegram")
            return "Rejected."
        if action == "later":
            pos = move_to_back(run_id)
            db.log(run_id, "awaiting_approval", "later via telegram: moved to the back of the review queue")
            close_card(card["id"], "open", "closed", "later", decided_via="telegram", later_pos=pos)
            return "Moved to the back of the queue."
    except db.TransitionError:   # decided in the dashboard a moment ago
        sync_open(card)
        return "Already handled in the dashboard."
    except ValueError as e:
        return str(e)[:190]
    return STALE


def retry(card: dict, platform: str) -> str:
    """Retry one failed platform from a finished card."""
    from . import actions
    view = posting_view(card)
    if platform not in view["failed"]:
        return STALE
    try:
        actions.retry_platform(card["run_id"], platform, "telegram")
    except (ValueError, db.TransitionError) as e:
        return str(e)[:190]
    overrides = dict(card["overrides"] or {})
    overrides[platform] = {"text": "retrying…", "at": db.now(), "retrying": True}
    update_card(card["id"], overrides=overrides, watch=1)
    return f"Trying {PLATFORMS[platform]} again."


# ------------------------------------------------------------------ posting progress

def short_error(message: str | None) -> str:
    from .errors import explain_error
    sentence = explain_error(message, "publish")["sentence"]
    if sentence.startswith("Something went wrong"):
        text = " ".join((message or "unknown error").split())
        return text[:150] + ("…" if len(text) > 150 else "")
    return sentence.rstrip(".")


def _publisher_alive(run_id: str) -> bool:
    from .lock import held_elsewhere, publish_lock
    return held_elsewhere(publish_lock(run_id))


def posting_view(card: dict, now: datetime | None = None) -> dict:
    """Per-platform lines of an approved card. final: every platform is posted or failed and no
    publisher is still running; failed: platforms that get a Retry button; stuck: platforms whose
    stage has made no progress for STAGE_TIMEOUT_S."""
    now = now or _now()
    run = db.get_run(card["run_id"]) or {}
    posts = {p["platform"]: p for p in db.get_posts(card["run_id"])}
    overrides = card["overrides"] or {}
    lines, failed, pending, last = [], [], [], card["approved_at"]
    for p in card["platforms"] or []:
        post, ov = posts.get(p), overrides.get(p)
        for ts in (post["updated_at"] if post else None, ov["at"] if ov else None):
            if ts and (not last or ts > last):
                last = ts   # the last sign of progress: a post update or a card note (e.g. a retry)
        status = post["status"] if post else None
        # the card's own note (a timeout or a retry) stands until the DB has newer news: a later update,
        # a live post, or (after a timeout, when the post was still unfinished) any final status
        newer = bool(post) and (post["updated_at"] > ov["at"] or status in LIVE or
                                (not ov.get("retrying") and status in FINAL)) if ov else True
        if not newer:
            text = ov["text"]
            if ov.get("retrying"):
                pending.append(p)
            else:
                failed.append(p)
        elif status in ("live", "uploaded"):
            text = "posted" + (f" — {post['url']}" if post.get("url") else "")
        elif status == "private_locked":
            text = "posted as private (needs YouTube's approval)" + (f" — {post['url']}" if post.get("url") else "")
        elif status == "dry_run":
            text = "test mode, not posted"
        elif status == "failed":
            text = f"failed — {short_error(post.get('message'))}"
            failed.append(p)
        elif status in ("uploading", "pending"):
            text = "uploading…" if status == "uploading" else "waiting (daily limit)"
            pending.append(p)
        else:
            text = "waiting"
            pending.append(p)
        lines.append(f"{ICON[p]} {PLATFORMS[p]}: {text}")
    alive = run.get("stage") == "publishing" and _publisher_alive(card["run_id"])
    stuck = pending if pending and _age(last, now) > STAGE_TIMEOUT_S else []
    return {"lines": lines, "failed": failed, "pending": pending, "stuck": stuck, "alive": alive,
            "final": not pending and not alive, "posts": posts}


def check_posting(card: dict, now: datetime | None = None) -> dict:
    """Finish an approved card once posting is final, or mark stuck platforms failed on the card
    (it keeps following them) after STAGE_TIMEOUT_S without progress. Either way the queue advances."""
    view = posting_view(card, now)
    if view["stuck"]:
        overrides = dict(card["overrides"] or {})
        for p in view["stuck"]:
            started = p in view["posts"]
            overrides[p] = {"text": "failed — " + ("took longer than 10 minutes" if started else
                                                   "didn't start within 10 minutes"), "at": db.now()}
        close_card(card["id"], "posting", "done", "approved", overrides=overrides, watch=1)
        db.log(card["run_id"], "publishing", "telegram card: " + ", ".join(view["stuck"]) + " timed out after 10 minutes")
    elif view["final"]:
        close_card(card["id"], "posting", "done", "approved", watch=0)
    return get_card(card["id"])


def check_watched(card: dict, now: datetime | None = None) -> None:
    """A finished card follows a slow or retried upload until it settles (or WATCH_FOR_S)."""
    view = posting_view(card, now)
    overrides = dict(card["overrides"] or {})
    changed = False
    for p in view["stuck"]:
        if overrides.get(p, {}).get("retrying"):
            overrides[p] = {"text": "failed — the retry took longer than 10 minutes", "at": db.now()}
            changed = True
    still_retrying = any(ov.get("retrying") for p, ov in overrides.items() if p in view["pending"])
    done = (not view["alive"] and not still_retrying) or _age(card["closed_at"], now) > WATCH_FOR_S
    if changed or done:
        update_card(card["id"], overrides=overrides, watch=0 if done and not changed else 1)


# ------------------------------------------------------------------ card wording

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


def video_title(run: dict) -> str:
    return run.get("title") or _meta(run["id"]).get("title") or _topic(run["topic_id"]).get("title") or "New video"


def card_base(run: dict) -> str:
    """Title, metric, countries, length. No scores, no data."""
    meta, topic = _meta(run["id"]), _topic(run["topic_id"])
    names = country_by_iso3()
    countries = [names[c]["name"] for c in run["countries"] if c in names] or \
                [c.get("name", c.get("iso3", "")) for c in meta.get("countries", [])]
    lines = [f"🎬 {video_title(run)}"]
    if topic.get("subtitle"):
        lines.append(f"📈 {topic['subtitle']}")
    if countries:
        lines.append("🌍 " + ", ".join(countries))
    if meta.get("duration_seconds"):
        lines.append(f"⏱ {round(meta['duration_seconds'])} s")
    return "\n".join(lines)


def card_view(card: dict) -> tuple[str, dict]:
    """(caption, reply_markup) the card's Telegram message should show now."""
    run = db.get_run(card["run_id"])
    base = card_base(run) if run else "🎬 This video was removed"
    tail, rows = [], []
    via = " in the dashboard" if card.get("decided_via") == "dashboard" else ""
    cid = card["id"]
    if card["state"] in ("sending", "open"):
        rows = [[{"text": "✅ Approve", "callback_data": f"c:a:{cid}"},
                 {"text": "❌ Reject", "callback_data": f"c:r:{cid}"},
                 {"text": "⏭ Later", "callback_data": f"c:l:{cid}"}]]
    elif card["outcome"] == "approved":
        tail.append(f"✅ Approved{via}")
        if not card["platforms"]:
            tail.append("Not posted: no connected platform is turned on (Settings → Posting).")
        view = posting_view(card)
        tail += view["lines"]
        if card["state"] == "done":
            tail.append("🏁 Done")
            rows = [[{"text": f"🔁 Retry {PLATFORMS[p]}", "callback_data": f"c:t:{cid}:{p}"}] for p in view["failed"]]
    elif card["outcome"] == "rejected":
        tail.append(f"❌ Rejected{via}")
    elif card["outcome"] == "remade":
        tail.append("🔁 Remade in the dashboard — a new version is on its way")
    elif card["outcome"] == "later":
        tail.append("⏭ Later — moved to the back of the queue")
    elif card["outcome"] == "gone":
        tail.append("↩️ Taken back in the dashboard — no longer waiting for review")
    return fit(base, tail), {"inline_keyboard": rows}


def fit(base: str, tail: list[str], limit: int = CAPTION_LIMIT) -> str:
    """base + tail within Telegram's caption limit; the base (countries line) gives way first."""
    end = ("\n\n" + "\n".join(tail)) if tail else ""
    if len(end) > limit:
        end = end[:limit - 1] + "…"
    room = limit - len(end)
    if len(base) > room:
        base = base[:max(room - 1, 0)] + "…"
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


# ------------------------------------------------------------------ /ready

def ready_videos(limit: int = 8) -> list[dict]:
    """Finished, unposted videos, newest first: waiting for review, or approved / publish-failed
    with nothing live. The run on the open card and runs that are posting right now are left out."""
    card = current_card()
    out = []
    for run in db.list_runs(limit=100000):
        if card and run["id"] == card["run_id"]:
            continue
        ok = run["stage"] in ("awaiting_approval", "approved") or \
            (run["stage"] == "failed" and run["failed_stage"] == "publish")
        if not ok or _posted(run["id"]) or not (path("out") / run["id"] / "video.mp4").exists():
            continue
        if run["stage"] != "awaiting_approval" and _publisher_alive(run["id"]):
            continue
        out.append(run)
        if len(out) == limit:
            break
    return out


def bring_to_front(run_id: str, by: str) -> str:
    card = current_card()
    if card and card["run_id"] == run_id:
        return "That video is on the card above now."
    run = db.get_run(run_id)
    if not run or _posted(run_id):
        return "That video is already posted."
    if run["stage"] != "awaiting_approval":
        if not (run["stage"] == "approved" or (run["stage"] == "failed" and run["failed_stage"] == "publish")) \
                or _publisher_alive(run_id):
            return "That video can't be reviewed right now."
        back_to_review(run_id, f"back to review via {by}")
    move_to_front(run_id)
    db.log(run_id, "awaiting_approval", f"moved to the front of the review queue via {by}")
    return "Next in the review queue."


# ------------------------------------------------------------------ /new: topics and make jobs

DONE_JOB = ("ready", "skipped", "failed")


def _job(row) -> dict | None:
    return dict(row) if row else None


def add_job(topic_id: str, message_id: int | None, random_pick: bool = False) -> dict:
    ts = db.now()
    with db.db() as c:
        jid = c.execute("INSERT INTO make_jobs(topic_id, message_id, state, random, created_at, updated_at) "
                        "VALUES (?,?,'queued',?,?,?)", (topic_id, message_id, int(random_pick), ts, ts)).lastrowid
    return get_job(jid)


def get_job(job_id: int) -> dict | None:
    with db.db() as c:
        return _job(c.execute("SELECT * FROM make_jobs WHERE id=?", (job_id,)).fetchone())


def update_job(job_id: int, **fields) -> None:
    fields["updated_at"] = db.now()
    with db.db() as c:
        c.execute(f"UPDATE make_jobs SET {', '.join(k + '=?' for k in fields)} WHERE id=?", [*fields.values(), job_id])


def jobs(*states: str) -> list[dict]:
    with db.db() as c:
        rows = c.execute(f"SELECT * FROM make_jobs WHERE state IN ({','.join('?' * len(states))}) ORDER BY id",
                         states).fetchall()
    return [dict(r) for r in rows]


def job_for_run(run_id: str) -> dict | None:
    with db.db() as c:
        return _job(c.execute("SELECT * FROM make_jobs WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone())


def making_alive(run: dict, started_at: str | None = None) -> bool:
    from .lock import held_elsewhere, run_lock
    return held_elsewhere(run_lock(run["id"])) or _age(started_at or run["updated_at"]) < START_GRACE_S


def making_now() -> list[dict]:
    """Runs being made right now (their maker process holds the run lock)."""
    from .lock import held_elsewhere, run_lock
    return [r for stage in MAKING for r in db.list_runs(stage=stage) if held_elsewhere(run_lock(r["id"]))]


def render_busy() -> bool:
    return bool(jobs("running") or making_now())


def topics_with_videos() -> set[str]:
    """Topics that already have a video (made, waiting, posted or rejected), are being made, or
    are waiting in the /new queue."""
    made = {r["topic_id"] for r in db.list_runs(limit=100000)
            if r["stage"] not in ("skipped", "failed") or (r["stage"] == "failed" and r["failed_stage"] == "publish")}
    return made | {j["topic_id"] for j in jobs("queued", "running")}


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


def suggestions(n: int = 3) -> list[dict]:
    """The highest-scoring topics that have no video yet (enabled, not retired, not resting)."""
    from .selector import eligible_topics
    candidates = eligible_topics(exclude=topics_with_videos())
    scored = [(topic_score(t), t) for t in candidates]
    scored.sort(key=lambda st: -st[0])
    return [t for s, t in scored if s > 0][:n]


def random_topic() -> str | None:
    from .selector import choose_topic
    return choose_topic(exclude=topics_with_videos())


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
