"""The posting queue: approved reels post automatically, ONE PER DAY, at posting.time (config.yaml,
default 21:00 IST). The bot runs it itself (tick(), every loop), not the OS scheduler, so it works the
same on a laptop or a server.

The queue is every run in stage "approved", in runs.post_pos order; approving puts a reel at the back,
and Move up / Move down swap neighbours. Every day has one slot at posting.time in posting.timezone.
tick() claims the slot atomically in the kv table (posting:slot:<date>) before it starts a post, so a
restart or a second bot never posts twice in one slot, and the publisher itself refuses a run that is
no longer "approved", so a reel is never posted twice.

Missed slots: when the bot was off at posting time, the reel that was due posts ONCE when the bot comes
back. Only the most recent slot counts (three missed days never become three posts in a row), and only a
reel approved before that slot. A slot that comes less than posting.min_gap_hours after the last post is
skipped, so a late post is never followed straight away by the next one.
Post now posts at once (the owner asked for it) and counts as the last post.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .config import get_config

log = logging.getLogger(__name__)

DEFAULTS = {"time": "21:00", "timezone": "Asia/Kolkata", "min_gap_hours": 12}
K_LAST = "posting:last_started"   # when the last post (from the queue or Post now) was started
K_LAST_RUN = "posting:last_run"   # ... and which reel it was
STARTING_S = 15 * 60              # a reel just handed to the publisher is "starting" this long, not queued
LIVE = ("live", "uploaded", "private_locked", "dry_run")


def settings() -> dict:
    return {**DEFAULTS, **(get_config().get("posting") or {})}


def tz() -> ZoneInfo:
    return ZoneInfo(settings()["timezone"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gap() -> timedelta:
    return timedelta(hours=float(settings()["min_gap_hours"]))


# ------------------------------------------------------------------ the queue

def queue() -> list[dict]:
    """Approved reels waiting to post, next first."""
    with db.db() as c:
        rows = c.execute("SELECT rowid AS ref, * FROM runs WHERE stage='approved' "
                         "ORDER BY COALESCE(post_pos, rowid), rowid").fetchall()
    return [db._run_dict(r) for r in rows]


def next_pos() -> float:
    """The back of the posting queue, behind the places kept for reels waiting to be approved again
    (review.back_to_review keep_place), so those go back in front of reels approved after them."""
    with db.db() as c:
        r = c.execute("SELECT MAX(p) FROM (SELECT MAX(COALESCE(post_pos, rowid)) p FROM runs WHERE stage='approved' "
                      "UNION ALL SELECT MAX(prior_post_pos) FROM runs WHERE stage='awaiting_approval')").fetchone()[0]
    return (r or 0) + 1


def move(run_id: str, step: int) -> bool:
    """Swap a queued reel with its neighbour (step -1 = up, +1 = down). False at either end."""
    ids = [r["id"] for r in queue()]
    if run_id not in ids:
        raise ValueError("That reel is no longer waiting to post.")
    i = ids.index(run_id)
    j = i + step
    if not 0 <= j < len(ids):
        return False
    ids[i], ids[j] = ids[j], ids[i]
    with db.db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            for pos, rid in enumerate(ids, 1):
                c.execute("UPDATE runs SET post_pos=? WHERE id=? AND stage='approved'", (pos, rid))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return True


def unapprove(run_id: str, by: str) -> str:
    """Back to Ready for review (at the front), with its approval forgotten."""
    from .review import back_to_review, move_to_front
    run = db.get_run(run_id)
    if not run or run["stage"] != "approved":
        raise ValueError("That reel is no longer waiting to post.")
    back_to_review(run_id, f"unapproved via {by}: back to Ready for review")
    move_to_front(run_id)
    return "Unapproved: it is back in Ready for review."


# ------------------------------------------------------------------ slots

def slot_on(day: date) -> datetime:
    hh, mm = (int(x) for x in str(settings()["time"]).split(":"))
    return datetime.combine(day, time(hh, mm), tz())


def due_slot(now: datetime | None = None) -> datetime:
    """The most recent slot at or before now."""
    local = (now or _now()).astimezone(tz())
    s = slot_on(local.date())
    return s if s <= local else slot_on(local.date() - timedelta(days=1))


def _key(slot: datetime) -> str:
    return f"posting:slot:{slot.date().isoformat()}"


def claimed(slot: datetime) -> bool:
    return bool(db.kv_get(_key(slot)))


def _claim(slot: datetime, what: str) -> bool:
    """Atomically take the slot; False when it was already taken (a restart, a second bot)."""
    db.init()
    with db.db() as c:
        return c.execute("INSERT OR IGNORE INTO kv(key, value, updated_at) VALUES (?,?,?)",
                         (_key(slot), what, db.now())).rowcount == 1


def last_post() -> datetime | None:
    """When the last post started (from the queue or Post now). Before the queue ever posted: the newest
    post the publisher made."""
    if db.kv_get(K_LAST):
        return db.parse_ts(db.kv_get(K_LAST))
    stamps = [p["created_at"] for p in db.get_posts() if p["status"] in LIVE]
    return max(db.parse_ts(s) for s in stamps) if stamps else None


def starting(now: datetime | None = None) -> str | None:
    """The reel just handed to the publisher, while it is still "approved" (the publisher has not taken
    it yet). After STARTING_S it counts as queued again (the publisher never started)."""
    rid, ts = db.kv_get(K_LAST_RUN), db.kv_get(K_LAST)
    if not rid or not ts or ((now or _now()) - db.parse_ts(ts)).total_seconds() > STARTING_S:
        return None
    run = db.get_run(rid)
    return rid if run and run["stage"] == "approved" else None


def approved_time(run: dict) -> datetime:
    if run.get("approved_at"):
        return db.parse_ts(run["approved_at"])
    with db.db() as c:
        r = c.execute("SELECT ts FROM stage_log WHERE run_id=? AND stage='approved' ORDER BY id DESC LIMIT 1",
                      (run["id"],)).fetchone()
    return db.parse_ts(r["ts"] if r else run["updated_at"])


def _due_reel(slot: datetime, q: list[dict]) -> dict | None:
    """The reel that slot posts: the first in the queue that was approved before the slot."""
    return next((r for r in q if approved_time(r) <= slot), None)


# ------------------------------------------------------------------ posting

def start(run_id: str, by: str, now: datetime | None = None) -> str:
    from . import actions
    db.kv_set(K_LAST, (now or _now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    db.kv_set(K_LAST_RUN, run_id)
    return actions.publish(run_id, by)


def tick(now: datetime | None = None) -> str | None:
    """Called on every bot loop: post the reel the current slot is due for, at most once per slot.
    Returns the run id it started posting, else None."""
    from .accounts import ready_destinations
    now = now or _now()
    slot = due_slot(now)
    if claimed(slot):
        return None
    q = queue()
    run = _due_reel(slot, q)
    if not run:   # nothing was waiting at that time: the slot passes (a later approval waits for the next)
        _claim(slot, "nothing to post")
        return None
    last = last_post()
    if last and now - last < _gap():
        _claim(slot, f"skipped: the last post started at {last:%Y-%m-%d %H:%M} UTC, under "
                     f"{settings()['min_gap_hours']} h before")
        log.info("posting: slot %s skipped, the last post was less than %s h ago", slot, settings()["min_gap_hours"])
        return None
    if not ready_destinations(run):
        return None   # no connected account: the slot stays open and the home card says so
    if not _claim(slot, run["id"]):
        return None
    late = "" if now - slot < timedelta(minutes=10) else f" (the bot was off at {slot:%H:%M}: posted late, once)"
    log.info("posting: %s for the %s slot%s", run["id"], slot.isoformat(), late)
    start(run["id"], f"posting queue, {slot:%a %d %b %H:%M}{late}", now)
    return run["id"]


def post_now(run_id: str, by: str) -> str:
    run = db.get_run(run_id)
    if not run or run["stage"] != "approved":
        raise ValueError("Only an approved reel that is waiting to post can be posted now.")
    return start(run_id, f"post now via {by}")


# ------------------------------------------------------------------ what the owner sees

def schedule(now: datetime | None = None) -> list[dict]:
    """[{"run", "when"}] for the whole queue, by the same rules as tick(): when is a datetime, or None
    for "now" (the current slot is due and the bot posts it on its next loop)."""
    now = now or _now()
    busy = starting(now)
    q = [r for r in queue() if r["id"] != busy]
    if not q:
        return []
    out, last = [], last_post()
    slot = due_slot(now)
    head = _due_reel(slot, q) if not claimed(slot) else None
    if head and (not last or now - last >= _gap()):
        out.append({"run": head, "when": None})
        q = [r for r in q if r["id"] != head["id"]]
        last = now
    nxt = slot_on(slot.date() + timedelta(days=1))
    for run in q:
        while last and nxt - last < _gap():
            nxt += timedelta(days=1)
        out.append({"run": run, "when": nxt})
        last, nxt = nxt, nxt + timedelta(days=1)
    return out


def when_text(when: datetime | None) -> str:
    if when is None:
        return "now"
    t = when.astimezone(tz())
    return f"{t:%a} {t.day} {t:%b}, {t:%H:%M} {t.tzname()}"


def next_post_text(now: datetime | None = None) -> str | None:
    s = schedule(now)
    return when_text(s[0]["when"]) if s else None


def when_for(run_id: str, now: datetime | None = None) -> str | None:
    return next((when_text(s["when"]) for s in schedule(now) if s["run"]["id"] == run_id), None)
