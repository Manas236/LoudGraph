"""A post's date is when it really went out: a failed post retried later gets the retry's publish time (Instagram's
own timestamp where it is known), old rows are corrected by `run.py post-dates`, and the cold-open A/B report
compares within the current format_version only, with the posting slot of each post."""
import json

import pytest


@pytest.fixture
def env(temp_db, tmp_path, pinned_config):
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"))
    return temp_db


def _post(db, rid, platform="instagram"):
    return next(p for p in db.get_posts(rid) if p["platform"] == platform)


def _backdate(db, rid, platform, created_at):
    with db.db() as c:
        c.execute("UPDATE posts SET created_at=? WHERE run_id=? AND platform=?", (created_at, rid, platform))


# ------------------------------------------------------------------ the retry

def test_a_retried_post_gets_the_date_it_really_went_out(env):
    db = env
    rid = db.create_run("co2_per_capita")
    db.upsert_post(rid, "instagram", "uploading", message="creating the REELS container")
    db.upsert_post(rid, "instagram", "failed", message="the upload stopped unexpectedly")
    _backdate(db, rid, "instagram", "2026-10-19T07:55:07Z")              # the first attempt, three days earlier
    db.upsert_post(rid, "instagram", "failed", message="still failed")
    assert _post(db, rid)["created_at"] == "2026-10-19T07:55:07Z"        # nothing new went out
    db.upsert_post(rid, "instagram", "uploading", message="retry")       # the retry starts: a new attempt
    assert _post(db, rid)["created_at"] > "2026-10-22"
    db.upsert_post(rid, "instagram", "live", remote_id="m1", published_at="2026-10-22T09:43:52Z")
    assert _post(db, rid)["created_at"] == "2026-10-22T09:43:52Z"        # Instagram's own publish time
    # Facebook has no publish time to ask for: the retry's own start is kept
    db.upsert_post(rid, "facebook", "failed", message="x")
    _backdate(db, rid, "facebook", "2026-10-19T07:57:10Z")
    db.upsert_post(rid, "facebook", "uploading", message="retry")
    db.upsert_post(rid, "facebook", "live", remote_id="v1")
    assert _post(db, rid, "facebook")["created_at"] > "2026-10-22"


def test_a_normal_post_keeps_its_date_and_the_slot_follows_the_attempt(env):
    db = env
    rid = db.create_run("inflation")
    db.set_post_slot(rid, "14:30")
    db.upsert_post(rid, "instagram", "uploading")
    first = _post(db, rid)["created_at"]
    db.upsert_post(rid, "instagram", "live", remote_id="m1")
    assert (_post(db, rid)["created_at"], _post(db, rid)["slot"]) == (first, "14:30")
    other = db.create_run("coal_share")
    db.set_post_slot(other, "16:00")
    db.upsert_post(other, "instagram", "failed")
    db.set_post_slot(other, None)                                        # Retry is not a queue slot
    db.upsert_post(other, "instagram", "uploading")
    assert _post(db, other)["slot"] is None


def test_instagram_times_are_read_as_utc():
    from pipeline.publish_instagram import graph_time
    assert graph_time("2026-10-22T09:43:52+0000") == "2026-10-22T09:43:52Z"
    assert graph_time("2026-10-22T15:13:52+0530") == "2026-10-22T09:43:52Z"
    assert graph_time(None) is None and graph_time("soon") is None


def test_an_earlier_attempt_found_live_keeps_instagrams_time(env, monkeypatch, pinned_config):
    from pipeline import posted
    db = env
    pinned_config["dry_run"]["instagram"] = False
    monkeypatch.setenv("IG_ACCESS_TOKEN", "t")
    monkeypatch.setenv("IG_USER_ID", "u")
    rid = db.create_run("t")
    db.upsert_post(rid, "instagram", "uploading", remote_id="container1")
    _backdate(db, rid, "instagram", "2026-10-19T07:55:07Z")
    caption = "the caption"
    answers = {"container status": {"status_code": "PUBLISHED"},
               "recent media": {"data": [{"id": "m9", "caption": caption, "permalink": "https://ig/p",
                                          "timestamp": "2026-10-19T07:56:30+0000"}]}}
    monkeypatch.setattr(posted, "_get", lambda url, what, **params: answers[what])
    assert posted.earlier_attempt_live(rid, "instagram", {"description_instagram": caption}) == "live"
    assert _post(db, rid)["created_at"] == "2026-10-19T07:56:30Z" and _post(db, rid)["remote_id"] == "m9"


# ------------------------------------------------------------------ correcting the old rows

def test_post_dates_lists_and_corrects_the_rows_that_went_out_later(env, monkeypatch, pinned_config):
    from pipeline import posted
    db = env
    late = db.create_run("co2_per_capita")
    db.upsert_post(late, "instagram", "live", remote_id="m1")            # logged live now
    _backdate(db, late, "instagram", "2026-10-19T07:55:07Z")             # ... but dated three days back
    fine = db.create_run("inflation")
    db.upsert_post(fine, "instagram", "live", remote_id="m2")            # dated and logged at the same moment
    rows = posted.fix_post_dates()
    assert [(r["run_id"], r["recorded"], r["source"]) for r in rows] == \
        [(late, "2026-10-19T07:55:07Z", "when the log saw it go live")]
    assert _post(db, late)["created_at"] == "2026-10-19T07:55:07Z"       # listing writes nothing
    # with Instagram reachable, its own publish time wins
    pinned_config["dry_run"]["instagram"] = False
    monkeypatch.setenv("IG_ACCESS_TOKEN", "t")
    stamps = {"m1": "2026-10-22T09:43:52+0000", "m2": None}
    monkeypatch.setattr(posted, "_get", lambda url, what, **p: {"timestamp": stamps[url.rsplit("/", 1)[1]]})
    rows = posted.fix_post_dates(apply=True)
    assert [(r["run_id"], r["actual"], r["source"]) for r in rows] == \
        [(late, "2026-10-22T09:43:52Z", "Instagram publish time")]
    assert _post(db, late)["created_at"] == "2026-10-22T09:43:52Z" and posted.fix_post_dates() == []


# ------------------------------------------------------------------ the A/B within format_version 2

def test_the_ab_report_compares_within_the_current_format_only(env):
    from pipeline import coldopen
    db = env
    reels = [  # (format_version, cold open, slot, views, skip rate, watch %)
        (1, False, None, 3400, 33.0, 54.0),         # a format 1 breakout: never mixed into the format 2 A/B
        (2, True, "14:30", 150, 30.0, 50.0), (2, True, "16:00", 250, 34.0, 46.0), (2, True, "16:00", 2000, 32.0, 48.0),
        (2, False, "16:00", 120, 40.0, 40.0), (2, False, None, 180, 38.0, 42.0)]
    for k, (fv, on, slot, views, skip, watch) in enumerate(reels):
        rid = db.create_run("t")
        db.set_cold_open(rid, on, "beat" if on else "control side of the A/B")
        db.set_format_version(rid, fv)
        db.set_post_slot(rid, slot)
        db.upsert_post(rid, "instagram", "live", remote_id=f"m{k}")
        _backdate(db, rid, "instagram", "2026-01-01T00:00:00Z")          # old enough to count
        db.add_stats(db.get_posts(rid)[0]["id"], views=views, skip_rate=skip, avg_view_pct=watch, raw={})
    a = coldopen.arms([r for r in coldopen._rows(0) if r["format_version"] == 2], ["14:30", "16:00"])
    assert a["with"] == {"n": 3, "skip_rate": 32.0, "avg_view_pct": 48.0, "median_views": 250,
                         "slots": {"14:30": 1, "16:00": 2, "other": 0}}
    assert a["without"]["n"] == 2 and a["without"]["median_views"] == 150 and \
        a["without"]["slots"] == {"14:30": 0, "16:00": 1, "other": 1}
    text = "\n".join(coldopen.report_lines())
    assert "format_version 2 only" in text and "3,400" not in text and "--v1" in text
    row = next(ln for ln in text.splitlines() if ln.startswith("with cold open"))
    assert row.split() == ["with", "cold", "open", "3", "32.0", "48.0", "250", "1", "2", "0"]
    old = "\n".join(coldopen.report_lines(v1=True))
    assert "format 1 reels" in old and "3,400" in old and "format_version 2 only" not in old
