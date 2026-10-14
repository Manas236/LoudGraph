"""The posting queue (pipeline/posting.py): approved reels post one a day at posting.time, a missed slot
posts once when the bot is back, never several at once and never the same reel twice. Time is simulated
(a fixed day a month ahead); publishing is faked (actions.spawn is recorded, never run)."""
import json
from datetime import datetime, timedelta, timezone
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
    pinned_config["posting"] = {"time": "21:00", "timezone": "Asia/Kolkata", "min_gap_hours": 12}
    spawned = []
    monkeypatch.setattr(actions, "spawn", lambda *a: spawned.append(a) or 0)
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram"])
    return {"db": temp_db, "spawned": spawned}


def approved(e, topic, when=None):
    """A reel approved via the review card; `when` backdates the approval (default: day 0, 10:00 IST)."""
    from pipeline import actions
    db = e["db"]
    rid = db.create_run(topic)
    for st in FLOW:
        db.transition(rid, st)
    actions.approve(rid, "telegram")
    stamp = (when or at(0, "10:00")).strftime("%Y-%m-%dT%H:%M:%SZ")
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


# ------------------------------------------------------------------ approving

def test_approving_adds_to_the_queue_and_posts_nothing(env):
    from pipeline import posting
    a, b = approved(env, "inflation"), approved(env, "coal_share")
    assert env["spawned"] == [] and [r["id"] for r in posting.queue()] == [a, b]
    assert env["db"].get_run(a)["approved_at"] and env["db"].get_run(b)["post_pos"] > env["db"].get_run(a)["post_pos"]


# ------------------------------------------------------------------ one per day

def test_one_reel_a_day_at_the_posting_time(env):
    from pipeline import posting
    a, b, c = approved(env, "inflation"), approved(env, "coal_share"), approved(env, "broadband")
    assert posting.tick(at(0, "20:59")) is None and posts(env) == []
    assert posting.tick(at(0, "21:00")) == a
    for t in ("21:00", "21:01", "23:59"):
        assert posting.tick(at(0, t)) is None                    # the slot is used: nothing more today
    goes_live(env, a, at(0, "21:01"))
    assert posting.tick(at(1, "09:00")) is None and posting.tick(at(1, "20:59")) is None
    assert posting.tick(at(1, "21:00")) == b
    goes_live(env, b, at(1, "21:01"))
    assert posting.tick(at(2, "21:00")) == c
    assert posts(env) == [a, b, c]


def test_the_posting_time_comes_from_config(env, pinned_config):
    from pipeline import posting
    pinned_config["posting"]["time"] = "08:30"
    a = approved(env, "inflation", at(-1, "10:00"))
    assert posting.tick(at(0, "08:29")) is None and posting.tick(at(0, "08:30")) == a


def test_a_reel_approved_after_the_slot_waits_for_the_next_one(env):
    from pipeline import posting
    assert posting.tick(at(0, "21:00")) is None                  # nothing to post: the slot passes
    a = approved(env, "inflation", at(0, "21:30"))
    assert posting.tick(at(0, "21:31")) is None and posting.tick(at(1, "09:00")) is None
    assert posting.tick(at(1, "21:00")) == a


# ------------------------------------------------------------------ the missed-slot rule

def test_a_missed_slot_posts_once_when_the_bot_comes_back_never_several(env):
    from pipeline import posting
    a, b, c = approved(env, "inflation"), approved(env, "coal_share"), approved(env, "broadband")
    # the bot was off for three evenings (day 0, 1 and 2 at 21:00) and comes back on day 3 at 09:00
    assert posting.tick(at(3, "09:00")) == a
    for t in ("09:00", "09:01", "12:00"):
        assert posting.tick(at(3, t)) is None                    # one catch-up, not one per missed day
    goes_live(env, a, at(3, "09:01"))
    assert posting.tick(at(3, "21:00")) == b                     # 12 h later: today's own slot
    assert posts(env) == [a, b]


def test_a_late_post_right_before_the_slot_skips_that_slot(env):
    from pipeline import posting
    a, b = approved(env, "inflation"), approved(env, "coal_share")
    assert posting.tick(at(1, "20:30")) == a                     # yesterday's 21:00 was missed
    goes_live(env, a, at(1, "20:31"))
    assert posting.tick(at(1, "21:00")) is None                  # 30 min later: not a second post
    assert posting.tick(at(1, "23:00")) is None
    assert posting.tick(at(2, "21:00")) == b
    assert posts(env) == [a, b]


# ------------------------------------------------------------------ no double posts

def test_no_double_posts(env, monkeypatch, pinned_config):
    from pipeline import orchestrator, posting, publish_instagram
    pinned_config["youtube"]["enabled"] = False                  # Instagram is the only destination
    a, b = approved(env, "inflation"), approved(env, "coal_share")
    assert posting.tick(at(0, "21:00")) == a
    assert posting.tick(at(0, "21:00")) is None                  # a second bot / a restart: the slot is taken
    assert posting._claim(posting.due_slot(at(0, "21:00")), "another bot") is False
    # the same reel can only be posted once, even if two publishers start
    from pipeline.config import run_dir
    (run_dir(a) / "meta.json").write_text(json.dumps({"title": "t"}))
    uploads = []
    monkeypatch.setattr(publish_instagram, "publish", lambda *x: uploads.append(x) or "dry_run")
    assert orchestrator.publish_run(a) is True and orchestrator.publish_run(a) is False and len(uploads) == 1
    with pytest.raises(ValueError):
        posting.post_now(a, "telegram")                          # no longer waiting to post
    # Post now counts as the day's post: the evening slot right after it is skipped
    assert posting.post_now(b, "telegram") and posts(env) == [a, b]
    assert posting.starting() == b and [s["run"]["id"] for s in posting.schedule()] == []   # b is starting
    goes_live(env, b, datetime.now(timezone.utc))
    c = approved(env, "broadband")
    assert posting.tick(at(1, "21:00")) == c                     # (Post now ran at real time, long before)
    assert posts(env) == [a, b, c]


def test_post_now_then_the_slot_within_the_gap_is_skipped(env):
    from pipeline import posting
    a, b = approved(env, "inflation"), approved(env, "coal_share")
    posting.start(a, "post now via telegram", at(0, "15:00"))
    goes_live(env, a, at(0, "15:02"))
    assert posting.tick(at(0, "21:00")) is None                  # 6 h after Post now
    assert posting.tick(at(1, "21:00")) == b


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
    assert posting.tick(at(0, "21:00")) == a


def test_the_schedule_lists_one_date_per_reel(env):
    from pipeline import posting
    a, b, c = approved(env, "inflation"), approved(env, "coal_share"), approved(env, "broadband")
    sched = posting.schedule(at(0, "12:00"))
    assert [s["run"]["id"] for s in sched] == [a, b, c]
    assert [s["when"] for s in sched] == [at(0, "21:00"), at(1, "21:00"), at(2, "21:00")]
    assert posting.when_text(sched[0]["when"]) == f"{at(0, '21:00').astimezone(IST):%a} {DAY.day} " \
                                                  f"{at(0, '21:00').astimezone(IST):%b}, 21:00 IST"
    late = posting.schedule(at(2, "09:00"))                      # the bot was off: the first one posts now
    assert late[0]["when"] is None and late[1]["when"] == at(2, "21:00") and posting.when_text(None) == "now"
    posting.tick(at(2, "09:00"))
    assert [s["when"] for s in posting.schedule(at(2, "09:05"))] == [at(2, "21:00"), at(3, "21:00")]
