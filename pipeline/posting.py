"""The posting queue: approved reels post automatically, in the daily SLOTS of posting.time (config.yaml:
14:30 and 16:00, in posting.timezone, Asia/Kolkata; the PC clock's own time zone is never used). The bot runs
it itself (tick(), every loop), not the OS scheduler, so it works the same on a laptop or a server.

The queue is every run in stage "approved", in runs.post_pos order; approving puts a reel at the back,
and Move up / Move down swap neighbours. tick() claims a slot atomically in the kv table
(posting:slot:<date>T<HH:MM>) before it starts a post, so a restart or a second bot never posts twice in
one slot, and the publisher itself refuses a run that is no longer "approved", so a reel is never posted
twice. The slot a post was made in (14:30, 16:00) is kept on its run (post_slot) and copied onto its post
rows (posts.slot); Post now and a Retry leave it empty.

The cold-open A/B (cold_open.mode alternate): the reel with a cold open and the one without SWAP slots every
day. On posting.ab_first_day, and every second day after it, the first slot is for a reel with a cold open
and the second for one without; on the days in between it is the other way round. Within its side a slot
takes the first approved reel in queue order. When the queue has no reel on a slot's side, the slot posts
the first reel in queue order instead, and that is logged on the reel.

Missed slots (the PC asleep, the bot stopped, no connection): a slot stays open until the next slot of the
day starts or, for the last one, until posting.late_until (18:00); while it is open the bot posts it as
soon as it can. A slot whose window closed is gone ("missed"): its reel stays at the front of the queue and
rolls to the next slot on its side. Missed slots are never caught up with extra posts. A post waits until
posting.min_gap_hours have passed since the last one (inside its slot's window). Only a reel approved
before a slot can post in it. Post now posts at once (the owner asked for it) and counts as the last post.
A publish that FAILS keeps its slot: the reel waits on the home card for Retry, so a broken token cannot
use up the queue.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .config import get_config

log = logging.getLogger(__name__)

DEFAULTS = {"time": ["14:30", "16:00"], "timezone": "Asia/Kolkata", "min_gap_hours": 1, "late_until": "18:00",
            "ab_first_day": None}
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

def _hm(value) -> time:
    """"14:30" as a time. PyYAML reads an unquoted 14:30 as the number 870 (base 60), so that is accepted too."""
    if isinstance(value, int):
        h, m = divmod(value, 60)
    else:
        h, m = (int(x) for x in str(value).strip().split(":"))
    return time(h, m)


def times() -> list[time]:
    """posting.time (one time or a list), sorted."""
    raw = settings()["time"]
    out = sorted({_hm(t) for t in ([raw] if isinstance(raw, (str, int)) else raw or [])})
    if not out:
        raise ValueError("posting.time has no posting time")
    return out


@dataclass(frozen=True)
class Slot:
    at: datetime        # the slot's time, in posting.timezone
    until: datetime     # it may still post (late) up to here: the next slot of the day, else posting.late_until
    index: int          # 0 = the day's first slot

    @property
    def label(self) -> str:
        return f"{self.at:%H:%M}"

    @property
    def key(self) -> str:
        return f"posting:slot:{self.at.date().isoformat()}T{self.label}"


def slots_on(day: date) -> list[Slot]:
    """The day's slots. A slot's window ends when the next one starts; the last one's at posting.late_until (at
    midnight when late_until is not after it)."""
    z = tz()
    ats = [datetime.combine(day, t, z) for t in times()]
    late = datetime.combine(day, _hm(settings()["late_until"]), z)
    midnight = datetime.combine(day + timedelta(days=1), time(0, 0), z)
    out = []
    for i, at in enumerate(ats):
        ends = [x for x in (ats[i + 1] if i + 1 < len(ats) else None, late if late > at else None) if x]
        out.append(Slot(at, min(ends) if ends else midnight, i))
    return out


def current_slot(now: datetime | None = None) -> Slot | None:
    """The slot whose window is open now, else None."""
    local = (now or _now()).astimezone(tz())
    return next((s for s in slots_on(local.date()) if s.at <= local < s.until), None)


def upcoming(now: datetime | None = None, days: int = 366):
    """Every slot whose window has not closed yet, from now on, in order."""
    local = (now or _now()).astimezone(tz())
    for k in range(days):
        for s in slots_on(local.date() + timedelta(days=k)):
            if s.until > local:
                yield s


def claimed(slot: Slot) -> bool:
    return bool(db.kv_get(slot.key))


def _claim(slot: Slot, what: str) -> bool:
    """Atomically take the slot; False when it was already taken (a restart, a second bot)."""
    db.init()
    with db.db() as c:
        return c.execute("INSERT OR IGNORE INTO kv(key, value, updated_at) VALUES (?,?,?)",
                         (slot.key, what, db.now())).rowcount == 1


def _close_missed(now: datetime) -> None:
    """Today's slots whose window closed without a post are marked missed (their reels roll to the next slot)."""
    local = now.astimezone(tz())
    for s in slots_on(local.date()):
        if s.until <= local and not claimed(s) and _claim(s, f"missed: nothing posted between {s.label} and "
                                                             f"{s.until:%H:%M}"):
            log.info("posting: the %s slot was missed; its reel waits for the next slot", s.at.isoformat())


# ------------------------------------------------------------------ the cold-open A/B

def ab_running() -> bool:
    from .coldopen import settings as cold_open
    return cold_open()["mode"] == "alternate"


def _ab_anchor() -> date:
    first = settings().get("ab_first_day")
    if isinstance(first, datetime):
        return first.date()
    if isinstance(first, date):   # PyYAML reads an unquoted 2026-10-24 as a date
        return first
    return date.fromisoformat(str(first)) if first else date(2026, 1, 1)


def wants_cold_open(slot: Slot) -> bool:
    """The side of the A/B a slot is for: with a cold open in the first slot on ab_first_day and every second day
    after it, and in the second slot on the days between."""
    on_first = (slot.at.date() - _ab_anchor()).days % 2 == 0
    return (slot.index % 2 == 0) == on_first


def has_cold_open(run: dict) -> bool:
    return bool(run.get("cold_open"))


def pick(slot: Slot, q: list[dict]) -> tuple[dict | None, str]:
    """(the reel this slot posts, a note when it is not on the slot's side). Only reels approved before the slot;
    with the A/B running, the first of them on the slot's side, else the first in queue order (noted)."""
    ready = [r for r in q if approved_time(r) <= slot.at]
    if not ready:
        return None, ""
    if not ab_running():
        return ready[0], ""
    want = wants_cold_open(slot)
    run = next((r for r in ready if has_cold_open(r) == want), None)
    if run:
        return run, ""
    return ready[0], (f"A/B slot balance: no reel {'with' if want else 'without'} a cold open was waiting for the "
                      f"{slot.label} slot of {slot.at:%a %d %b}, so the next one in queue order posts instead")


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


# ------------------------------------------------------------------ posting

def start(run_id: str, by: str, now: datetime | None = None, slot: str | None = None) -> str:
    """Hand a reel to the publisher. slot: the queue slot it posts in ("14:30"), None for Post now."""
    from . import actions
    db.kv_set(K_LAST, (now or _now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    db.kv_set(K_LAST_RUN, run_id)
    db.set_post_slot(run_id, slot)
    return actions.publish(run_id, by)


def tick(now: datetime | None = None) -> str | None:
    """Called on every bot loop: post the reel the open slot is for, at most once per slot.
    Returns the run id it started posting, else None."""
    from .accounts import ready_destinations
    now = now or _now()
    _close_missed(now)
    slot = current_slot(now)
    if slot is None or claimed(slot):
        return None
    run, note = pick(slot, queue())
    if not run:   # nothing was waiting at that time: the slot passes (a later approval waits for the next)
        _claim(slot, "nothing to post")
        return None
    last = last_post()
    if last and now - last < _gap():
        return None   # too soon after the last post: the slot waits, while its window is open
    if not ready_destinations(run):
        return None   # no connected account: the slot stays open and the home card says so
    if not _claim(slot, run["id"]):
        return None
    late = "" if now - slot.at < timedelta(minutes=10) else \
        f" (the bot was off at {slot.label}: posted late, still before {slot.until:%H:%M})"
    if note:
        log.info("posting: %s", note)
        db.log(run["id"], run["stage"], note)
    log.info("posting: %s for the %s slot%s", run["id"], slot.at.isoformat(), late)
    start(run["id"], f"posting queue, {slot.at:%a %d %b %H:%M}{late}", now, slot=slot.label)
    return run["id"]


def post_now(run_id: str, by: str) -> str:
    run = db.get_run(run_id)
    if not run or run["stage"] != "approved":
        raise ValueError("Only an approved reel that is waiting to post can be posted now.")
    return start(run_id, f"post now via {by}")


# ------------------------------------------------------------------ what the owner sees

def schedule(now: datetime | None = None) -> list[dict]:
    """[{"run", "when", "slot", "cold_open", "note"}] for the whole queue, by the same rules as tick(): when is a
    datetime, or None for "now" (the open slot is due and the bot posts it on its next loop)."""
    now = now or _now()
    busy = starting(now)
    q = [r for r in queue() if r["id"] != busy]
    out, last = [], last_post()
    for slot in upcoming(now):
        if not q:
            break
        if claimed(slot):
            continue
        at = max(slot.at, now)
        if last and at < last + _gap():
            at = last + _gap()
        if at >= slot.until:
            continue   # the gap runs past this slot's window: the reel rolls to the next slot
        run, note = pick(slot, q)
        if not run:
            continue
        out.append({"run": run, "when": None if at <= now else at, "slot": slot.label,
                    "cold_open": has_cold_open(run), "note": note})
        q = [r for r in q if r["id"] != run["id"]]
        last = at
    return out


def rule_text() -> str:
    """"Two reels a day, at 14:30 and 16:00 IST"."""
    ts = [t.strftime("%H:%M") for t in times()]
    count = {1: "One reel", 2: "Two reels", 3: "Three reels"}.get(len(ts), f"{len(ts)} reels")
    at = ts[0] if len(ts) == 1 else ", ".join(ts[:-1]) + " and " + ts[-1]
    return f"{count} a day, at {at} {datetime.now(tz()).tzname()}"


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
