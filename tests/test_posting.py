"""The posting queue (pipeline/posting.py): approved reels post in two daily slots, 14:30 and 16:00 IST, the reel
with a cold open and the one without swap slots every day, a missed slot posts late only while it is still open
(never caught up later), and the same reel never posts twice. Time is simulated (a fixed day a month ahead);
publishing is faked (actions.spawn is recorded, never run)."""
import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")
FLOW = ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]
DAY = (datetime.now(IST) + timedelta(days=30)).date()     # a day after every real timestamp in the test DB


def at(days: int, hhmm: str) -> datetime:
    """IST wall-clock time `days` after DAY, as UTC."""
    h, m = map(int, hhmm.split(":"))
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=IST).astimezone(timezone.utc) + timedelta(days=days)


@pytest.fixture
def env(temp_db, tmp_path, monkeypatch, pinned_config):
    from pipeline import accounts, actions
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"))
    pinned_config["posting"] = {"time": ["14:30", "16:00"], "timezone": "Asia/Kolkata", "min_gap_hours": 1,
                                "late_until": "18:00", "ab_first_day": DAY.isoformat()}
    pinned_config["cold_open"]["mode"] = "never"             # queue order only; the A/B tests turn it on
    spawned = []
    monkeypatch.setattr(actions, "spawn", lambda *a: spawned.append(a) or 0)
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram"])
    return {"db": temp_db, "spawned": spawned}


def approved(e, topic, when=None, cold=None):
    """A reel approved via the review card; `when` backdates the approval (default: the day before, 10:00 IST);
    cold: True / False gives it a cold open or not."""
    from pipeline import actions
    db = e["db"]
    rid = db.create_run(topic)
    for st in FLOW:
        db.transition(rid, st)
    if cold is not None:
        db.set_cold_open(rid, cold, "a beat" if cold else "control side of the A/B")
    actions.approve(rid, "telegram")
    stamp = (when or at(-1, "10:00")).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.db() as c:
        c.execute("UPDATE runs SET approved_at=? WHERE id=?", (stamp, rid))
    return rid


def posts(e):
    return [a[2] for a in e["spawned"] if a[0] == "publish"]


def goes_live(e, rid, when):
    """The publisher (faked) posts it: what `run.py publish` records."""
    db = e["db"]
    db.transition(rid, "publishing")
    db.upsert_post(rid, "instagram", "live", url="https://instagram.com/reel/x")
    with db.db() as c:
        c.execute("UPDATE posts SET created_at=?, updated_at=? WHERE run_id=?",
                  (when.strftime("%Y-%m-%dT%H:%M:%SZ"),) * 2 + (rid,))
    db.transition(rid, "published")


def slot_of(e, rid):
    return e["db"].get_posts(rid)[0]["slot"]


# ------------------------------------------------------------------ approving

def test_approving_adds_to_the_queue_and_posts_nothing(env):
    from pipeline import posting
    a, b = approved(env, "inflation"), approved(env, "coal_share")
    assert env["spawned"] == [] and [r["id"] for r in posting.queue()] == [a, b]
    assert env["db"].get_run(a)["approved_at"] and env["db"].get_run(b)["post_pos"] > env["db"].get_run(a)["post_pos"]


# ------------------------------------------------------------------ two slots a day

def test_two_reels_a_day_at_14_30_and_16_00_ist(env):
    from pipeline import posting
    a, b, c, d = (approved(env, t) for t in ("inflation", "coal_share", "broadband", "oil_per_capita"))
    assert posting.tick(at(0, "14:29")) is None and posts(env) == []
    assert posting.tick(at(0, "14:30")) == a
    assert posting.tick(at(0, "14:31")) is None                  # the slot is used
    goes_live(env, a, at(0, "14:31"))
    assert posting.tick(at(0, "15:59")) is None
    assert posting.tick(at(0, "16:00")) == b
    goes_live(env, b, at(0, "16:01"))
    for t in ("16:02", "17:59", "21:00", "23:59"):
        assert posting.tick(at(0, t)) is None                    # two a day, no more
    assert posting.tick(at(1, "09:00")) is None
    assert posting.tick(at(1, "14:30")) == c
    goes_live(env, c, at(1, "14:31"))
    assert posting.tick(at(1, "16:00")) == d
    goes_live(env, d, at(1, "16:01"))
    assert posts(env) == [a, b, c, d]
    assert [slot_of(env, r) for r in (a, b, c, d)] == ["14:30", "16:00", "14:30", "16:00"]   # kept on the post


def test_the_slots_are_ist_whatever_the_pc_clock_says(env, pinned_config):
    """The slots are built in posting.timezone and compared in UTC: the PC's own time zone never enters."""
    from pipeline import posting
    slots = posting.slots_on(DAY)
    assert [s.label for s in slots] == ["14:30", "16:00"]
    assert [s.at.astimezone(timezone.utc).strftime("%H:%M") for s in slots] == ["09:00", "10:30"]
    assert slots[0].until == slots[1].at and slots[1].until.astimezone(timezone.utc).strftime("%H:%M") == "12:30"
    assert posting.current_slot(at(0, "14:30")).label == "14:30" and posting.current_slot(at(0, "18:00")) is None
    assert slots[0].key == f"posting:slot:{DAY.isoformat()}T14:30"
    pinned_config["posting"]["time"] = [960, 870]                # PyYAML reads unquoted 14:30 as 870 (base 60)
    assert posting.times() == [time(14, 30), time(16, 0)]
    pinned_config["posting"]["time"] = "08:30"                   # one time still works
    a = approved(env, "inflation")
    assert posting.tick(at(0, "08:29")) is None and posting.tick(at(0, "08:30")) == a
    assert posting.rule_text() == "One reel a day, at 08:30 IST"


def test_a_reel_approved_after_the_slot_waits_for_the_next_one(env):
    from pipeline import posting
    assert posting.tick(at(0, "14:30")) is None                  # nothing to post: the slot passes
    a = approved(env, "inflation", at(0, "14:45"))
    assert posting.tick(at(0, "14:50")) is None and posting.tick(at(0, "15:59")) is None
    assert posting.tick(at(0, "16:00")) == a


# ------------------------------------------------------------------ missed slots

def test_a_missed_slot_posts_late_while_it_is_still_open(env):
    from pipeline import posting
    a, b, c = approved(env, "inflation"), approved(env, "coal_share"), approved(env, "broadband")
    assert posting.tick(at(0, "15:10")) == a                     # the PC slept through 14:30: posted late
    assert any("posted late, still before 16:00" in x["message"] for x in env["db"].get_log(a))
    goes_live(env, a, at(0, "15:11"))
    assert slot_of(env, a) == "14:30"
    assert posting.tick(at(0, "16:00")) is None                  # within the hour after the late post: it waits
    assert posting.tick(at(0, "16:09")) is None
    assert posting.tick(at(0, "16:10")) == b                     # ... then posts, still before 18:00
    assert posts(env) == [a, b]


def test_a_closed_slot_rolls_its_reel_to_the_next_slot_and_is_never_caught_up(env):
    from pipeline import db, posting
    a, b, c = approved(env, "inflation"), approved(env, "coal_share"), approved(env, "broadband")
    assert posting.tick(at(0, "16:30")) == a                     # 14:30 closed at 16:00: only the 16:00 slot posts
    goes_live(env, a, at(0, "16:31"))
    assert db.kv_get(posting.slots_on(DAY)[0].key).startswith("missed")
    for t in ("17:30", "17:59", "18:00", "20:00"):
        assert posting.tick(at(0, t)) is None                    # no catch-up post
    assert posting.tick(at(1, "18:30")) is None                  # off all day 1 until after 18:00: both slots gone
    assert all(db.kv_get(s.key).startswith("missed") for s in posting.slots_on(DAY + timedelta(days=1)))
    assert posting.tick(at(2, "14:30")) == b                     # the reels roll on, one per slot
    goes_live(env, b, at(2, "14:31"))
    assert posting.tick(at(2, "16:00")) == c
    assert posts(env) == [a, b, c]


# ------------------------------------------------------------------ the cold-open A/B

def test_the_cold_open_reel_and_the_one_without_swap_slots_every_day(env, pinned_config):
    from pipeline import posting
    pinned_config["cold_open"]["mode"] = "alternate"
    off1, on1 = approved(env, "inflation", cold=False), approved(env, "coal_share", cold=True)
    off2, on2 = approved(env, "broadband", cold=False), approved(env, "oil_per_capita", cold=True)
    on3, off3 = approved(env, "co2_per_capita", cold=True), approved(env, "net_migration", cold=False)
    planned = posting.schedule(at(0, "12:00"))
    want = [(on1, "14:30"), (off1, "16:00"), (off2, "14:30"), (on2, "16:00"), (on3, "14:30"), (off3, "16:00")]
    assert [(s["run"]["id"], s["slot"]) for s in planned] == want
    assert [s["when"] for s in planned] == [at(d, t) for d in range(3) for t in ("14:30", "16:00")]
    assert [s["cold_open"] for s in planned] == [True, False, False, True, True, False]
    for k, (rid, slot) in enumerate(want):   # the bot does exactly what the schedule says
        when = at(k // 2, slot)
        assert posting.tick(when) == rid
        goes_live(env, rid, when + timedelta(minutes=1))
    assert [slot_of(env, r) for r, _ in want] == [s for _, s in want]


def test_without_one_of_each_the_queue_order_posts_and_it_is_logged(env, pinned_config):
    from pipeline import posting
    pinned_config["cold_open"]["mode"] = "alternate"
    on1, on2 = approved(env, "inflation", cold=True), approved(env, "coal_share", cold=True)
    assert posting.tick(at(1, "14:30")) == on1                   # day 1: 14:30 is for a reel WITHOUT one; there is none
    note = [x["message"] for x in env["db"].get_log(on1) if x["message"].startswith("A/B slot balance")]
    assert note and "no reel without a cold open was waiting for the 14:30 slot" in note[0]
    goes_live(env, on1, at(1, "14:31"))
    assert posting.tick(at(1, "16:00")) == on2                   # 16:00 is the cold-open side: no note
    assert not any(x["message"].startswith("A/B slot balance") for x in env["db"].get_log(on2))


# ------------------------------------------------------------------ no double posts, Post now

def test_no_double_posts(env, monkeypatch, pinned_config):
    from pipeline import orchestrator, posting, publish_instagram
    pinned_config["youtube"]["enabled"] = False                  # Instagram is the only destination
    a, b = approved(env, "inflation"), approved(env, "coal_share")
    assert posting.tick(at(0, "14:30")) == a
    assert posting.tick(at(0, "14:30")) is None                  # a second bot / a restart: the slot is taken
    assert posting._claim(posting.current_slot(at(0, "14:30")), "another bot") is False
    # the same reel can only be posted once, even if two publishers start
    from pipeline.config import run_dir
    (run_dir(a) / "meta.json").write_text(json.dumps({"title": "t"}))
    uploads = []
    monkeypatch.setattr(publish_instagram, "publish", lambda *x: uploads.append(x) or "dry_run")
    assert orchestrator.publish_run(a) is True and orchestrator.publish_run(a) is False and len(uploads) == 1
    with pytest.raises(ValueError):
        posting.post_now(a, "telegram")                          # no longer waiting to post
    assert posting.post_now(b, "telegram") and posts(env) == [a, b]
    assert posting.starting() == b and [s["run"]["id"] for s in posting.schedule()] == []   # b is starting
    goes_live(env, b, datetime.now(timezone.utc))
    assert slot_of(env, b) is None                               # Post now is not a slot
    c = approved(env, "broadband")
    assert posting.tick(at(1, "14:30")) == c                     # (Post now ran at real time, long before)
    assert posts(env) == [a, b, c]


def test_post_now_then_the_next_slot_waits_an_hour(env):
    from pipeline import posting
    a, b = approved(env, "inflation"), approved(env, "coal_share")
    posting.start(a, "post now via telegram", at(0, "14:00"))
    goes_live(env, a, at(0, "14:02"))
    assert posting.tick(at(0, "14:30")) is None and posting.tick(at(0, "14:59")) is None
    assert posting.tick(at(0, "15:00")) == b                     # an hour after Post now, the 14:30 slot is still open
    goes_live(env, b, at(0, "15:01"))
    assert (slot_of(env, a), slot_of(env, b)) == (None, "14:30")


# ------------------------------------------------------------------ order, unapprove, dates

def test_move_up_down_and_unapprove(env):
    from pipeline import posting, review
    db = env["db"]
    a, b, c = approved(env, "inflation"), approved(env, "coal_share"), approved(env, "broadband")
    assert posting.move(c, -1) and [r["id"] for r in posting.queue()] == [a, c, b]
    assert posting.move(a, -1) is False                          # already first
    assert posting.move(a, 1) and [r["id"] for r in posting.queue()] == [c, a, b]
    assert posting.unapprove(c, "telegram") == "Unapproved: it is back in Ready for review."
    run = db.get_run(c)
    assert run["stage"] == "awaiting_approval" and run["approved_at"] is None and run["post_pos"] is None
    assert review.queue()[0]["id"] == c and [r["id"] for r in posting.queue()] == [a, b]
    assert posting.tick(at(0, "14:30")) == a


def test_the_schedule_lists_a_slot_for_each_reel(env):
    from pipeline import posting
    a, b, c, d = (approved(env, t) for t in ("inflation", "coal_share", "broadband", "oil_per_capita"))
    sched = posting.schedule(at(0, "12:00"))
    assert [s["run"]["id"] for s in sched] == [a, b, c, d] and [s["slot"] for s in sched] == ["14:30", "16:00"] * 2
    assert [s["when"] for s in sched] == [at(0, "14:30"), at(0, "16:00"), at(1, "14:30"), at(1, "16:00")]
    assert posting.when_text(sched[0]["when"]) == f"{at(0, '14:30').astimezone(IST):%a} {DAY.day} " \
                                                  f"{at(0, '14:30').astimezone(IST):%b}, 14:30 IST"
    late = posting.schedule(at(0, "15:10"))                      # the bot was off at 14:30: the first one posts now
    assert late[0]["when"] is None and late[1]["when"] == at(0, "16:10") and posting.when_text(None) == "now"
    posting.tick(at(0, "15:10"))
    goes_live(env, a, at(0, "15:11"))
    assert [s["when"] for s in posting.schedule(at(0, "15:15"))] == [at(0, "16:10"), at(1, "14:30"), at(1, "16:00")]
    evening = posting.schedule(at(0, "18:30"))                   # both of today's slots are gone
    assert [s["when"] for s in evening] == [at(1, "14:30"), at(1, "16:00"), at(2, "14:30")]
    assert posting.rule_text() == "Two reels a day, at 14:30 and 16:00 IST"
