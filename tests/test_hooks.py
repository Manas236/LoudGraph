"""Hook templates (pipeline/hooks.py), "Which country ends up last?" as the default opening question and its
truth check, re-branding unposted reels (pipeline/rebrand.py), and stats that can fail without stopping the
bot. No network: Meta, Telegram and the renderer are faked."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

TOPIC_ID = "homicide_rate"                  # a real topic (unit "{:.1f}"), so get_topic works
YEARS = list(range(2000, 2011))
SERIES = {
    "IND": list(zip(YEARS, [1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 30])),     # overtakes France in 2007
    "USA": list(zip(YEARS, [20, 20, 20, 19, 19, 19, 19, 18, 18, 18, 18])),
    "FRA": list(zip(YEARS, [10, 13, 16, 19, 22, 25, 20, 15, 10, 7, 5])),    # ends last: 5.0 in 2010
    "JPN": list(zip(YEARS, [15, 15, 14, 14, 13, 13, 13, 12, 12, 12, 12])),
}
FLOW = ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture
def env(temp_db, tmp_path, monkeypatch, pinned_config):
    from pipeline import accounts, actions
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"))
    pinned_config["opening"] = {"hook_template": "which_last", "hook_explore_pct": 0}
    monkeypatch.setattr(actions, "spawn", lambda *a: 0)
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram"])
    return temp_db


def reel(db, question=None, stage="awaiting_approval", series=None):
    """A run with the files a rendered reel has (data, pick, labels, timeline, video, meta)."""
    from pipeline import timeline
    from pipeline.config import run_dir
    series = series or SERIES
    rid = db.create_run(TOPIC_ID)
    for st in FLOW[:FLOW.index(stage) + 1] if stage in FLOW else FLOW:
        db.transition(rid, st)
    d = run_dir(rid)
    isos = list(series)
    (d / "data.json").write_text(json.dumps({"topic_id": TOPIC_ID, "series": {k: [list(r) for r in v] for k, v in series.items()}}))
    (d / "pick.json").write_text(json.dumps({"countries": isos, "x_start": 2000, "x_end": 2010,
                                             "scores": {i: {"score": 50} for i in isos}}))
    (d / "labels.json").write_text("{}")
    tl = timeline.build([(i, [y for y, _ in series[i]], [v for _, v in series[i]]) for i in isos], 2000, 2010)
    tl.save(d / "timeline.json")
    (d / "audio.wav").write_bytes(b"RIFF-old-audio")
    (d / "video.mp4").write_bytes(b"old video")
    (d / "thumb.jpg").write_bytes(b"old cover")
    ids = {"Which country ends up last?": "ends_last", "Who overtakes who?": "overtakes",
           "Which country falls the most?": "falls_most"}
    opening = {"id": ids[question], "question": question, "answer": "a"} if question else None
    (d / "meta.json").write_text(json.dumps({"title": "t", "duration_seconds": 22.0, "opening": opening}))
    return rid


# ------------------------------------------------------------------ the hook template, apart from the topic

def test_hook_backfill_maps_every_reel_by_its_on_screen_question(env):
    from pipeline import hooks
    db = env
    last = reel(db, "Which country ends up last?")
    over = reel(db, "Who overtakes who?")
    falls = reel(db, "Which country falls the most?")
    old = reel(db, None)                                 # the reels from before 13 Oct had no on-screen hook
    db.transition(last, "approved")
    db.transition(last, "publishing")
    db.upsert_post(last, "instagram", "live", remote_id="m1")   # posted before the field existed
    rows = {r["run_id"]: r for r in hooks.backfill()}
    assert {k: v["hook_template"] for k, v in rows.items()} == {
        last: "which_last", over: "who_overtakes", falls: "other", old: "other"}
    assert all(r["changed"] for r in rows.values())
    assert db.get_posts(last)[0]["hook_template"] == "which_last" and db.get_run(over)["hook_template"] == "who_overtakes"
    assert not any(r["changed"] for r in hooks.backfill())          # idempotent
    db.upsert_post(over, "facebook", "live", remote_id="f1")        # a new post copies its run's template
    assert db.get_posts(over)[0]["hook_template"] == "who_overtakes"


def test_the_migration_is_idempotent(env):
    from pipeline import db
    with db.db() as c:
        db._ensure_schema(c)
        db._ensure_schema(c)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(posts)")}
        runs = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
    assert "hook_template" in cols and {"hook_template", "prior_post_pos"} <= runs


def test_hook_weights_use_the_topic_weight_formula_per_template():
    from pipeline.analytics import compute_weights
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    ts = lambda h: (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731
    rows = [{"topic_id": "a", "hook_template": "which_last", "views": 2100, "platform": "instagram", "posted_at": ts(80)},
            {"topic_id": "b", "hook_template": "who_overtakes", "views": 130, "platform": "instagram", "posted_at": ts(80)},
            {"topic_id": "c", "hook_template": "who_overtakes", "views": 150, "platform": "instagram", "posted_at": ts(90)},
            {"topic_id": "d", "hook_template": "which_last", "views": 9999, "platform": "instagram", "posted_at": ts(10)}]
    w = compute_weights(rows, 72, 3, 10 ** 9, now=now, key="hook_template")
    assert w["which_last"]["n_posts"] == 1 and w["who_overtakes"]["n_posts"] == 2       # the 10 h old post waits
    assert w["which_last"]["weight"] > w["who_overtakes"]["weight"]
    assert w["which_last"]["weight"] < w["which_last"]["mean_z"]                          # 1 post: shrunk to the mean


# ------------------------------------------------------------------ "ends up last" must be true

def check(series, country="FRA"):
    from pipeline.captions import check_ends_last
    names = {"IND": "India", "USA": "United States", "FRA": "France", "JPN": "Japan"}
    return check_ends_last({"iso3": country, "country": names[country]}, series, lambda v: f"{v:.1f}")


def test_ends_last_is_checked_against_the_rendered_data():
    assert check(SERIES) is None
    assert check(SERIES, "JPN").startswith("false")                                       # France is lower
    tie = {**SERIES, "JPN": SERIES["JPN"][:-1] + [(2010, 5.0)]}
    assert check(tie).startswith("tie")
    on_screen_tie = {**SERIES, "JPN": SERIES["JPN"][:-1] + [(2010, 5.04)]}                 # 5.0 and 5.0 on screen
    assert check(on_screen_tie).startswith("tie")
    assert check({k: SERIES[k] for k in ("IND", "FRA")}).startswith("only 2 countries")
    early = {**SERIES, "FRA": SERIES["FRA"][:-1]}                                        # stops in 2009
    assert "stops in 2009" in check(early, "FRA")
    other_early = {**SERIES, "USA": SERIES["USA"][:-1]}                                  # USA ends 2009 at 18.0
    assert check(other_early) is None


def test_which_last_is_the_default_opening_and_falls_back_when_it_is_not_true(env, pinned_config):
    from pipeline import render
    from pipeline.render import CountryView
    from pipeline.topics import get_topic
    topic = get_topic(TOPIC_ID)
    names = {"IND": "India", "USA": "United States", "FRA": "France", "JPN": "Japan"}

    def views(series):
        return [CountryView(iso3=i, name=names[i], iso2=None, color="#22d3ee", years=[y for y, _ in s],
                            values=[v for _, v in s]) for i, s in series.items()]

    for seed in range(6):                                   # the old choice alternated with "Who overtakes who?"
        o = render.opening_for(topic, views(SERIES), 2000, 2010, seed=seed)
        assert (o["question"], o["answer"], o["hook_template"]) == \
            ("Which country ends up last?", "France ends up last: 5.0 in 2010", "which_last")
        assert "fallback" not in o
    tie = {**SERIES, "JPN": SERIES["JPN"][:-1] + [(2010, 5.0)]}
    o = render.opening_for(topic, views(tie), 2000, 2010, seed=0)
    assert o["question"] == "Who overtakes who?" and o["fallback"].startswith("tie")
    pinned_config["opening"] = {"hook_template": "which_last", "hook_explore_pct": 100}
    o = render.opening_for(topic, views(SERIES), 2000, 2010, seed=0)
    assert o["hook_template"] != "which_last" and o["fallback"].startswith("exploring")
    pinned_config["opening"] = {"hook_template": "auto", "hook_explore_pct": 0}
    assert {render.opening_for(topic, views(SERIES), 2000, 2010, seed=s)["id"] for s in range(4)} == \
        {"overtakes", "ends_last"}                           # auto: the old choice between the top two


def test_which_last_captions_tease_the_answer_without_giving_it_away(env):
    from pipeline import captions
    db = env
    rid = reel(db, "Which country ends up last?")
    c = captions.generate_for_run(rid, llm=False)
    teasers = {h["id"] for h in captions.for_opener(captions.load_formats(), "ends_last")["hooks"]}
    assert len(c["variants"]) == 3 and {v["template_id"] for v in c["variants"]} <= teasers
    for v in c["variants"]:
        hook, _, question = v["caption"].split("\n")
        assert "last" in (hook + question).lower() and "France" not in hook + question
    assert c["opener"] == "ends_last"
    over = captions.generate_for_run(reel(db, "Who overtakes who?"), llm=False)    # other openers: the general hooks
    assert not {v["template_id"] for v in over["variants"]} & teasers


# ------------------------------------------------------------------ re-branding unposted reels

@pytest.fixture
def fake_render(monkeypatch):
    from pipeline import render
    calls = []

    def render_video(topic, tl, views, wav, out_mp4, thumb=None, cfg=None, on_progress=None, opening=None,
                     audio_from=None):
        calls.append({"opening": opening["question"], "audio_from": audio_from.name, "countries": [v.iso3 for v in views],
                      "frames": tl.n_frames})
        out_mp4.write_bytes(b"new video " + audio_from.read_bytes())
        thumb.write_bytes(b"new cover")
        return {"cover_seconds": 2.35, "render_seconds": 0.1, "opening": {"question": opening["question"]}}

    monkeypatch.setattr(render, "render_video", render_video)
    return calls


def test_rebrand_rerenders_unposted_reels_and_keeps_the_old_files(env, fake_render):
    from pipeline import actions, rebrand
    from pipeline.config import run_dir
    db = env
    first = reel(db, "Who overtakes who?")
    actions.approve(first, "telegram")
    second = reel(db, "Who overtakes who?")
    actions.approve(second, "telegram")
    waiting = reel(db, "Which country ends up last?")
    tie = reel(db, "Who overtakes who?", series={**SERIES, "JPN": SERIES["JPN"][:-1] + [(2010, 5.0)]})
    posted = reel(db, "Who overtakes who?")
    db.transition(posted, "approved")
    db.transition(posted, "publishing")
    db.upsert_post(posted, "instagram", "live", remote_id="m9")
    db.transition(posted, "published")
    places = {r: db.get_run(r)["post_pos"] for r in (first, second)}

    res = {r["run_id"]: r for r in rebrand.rebrand_all(send=False)}
    assert posted not in res                                                    # never touched
    assert (run_dir(posted) / "video.mp4").read_bytes() == b"old video"
    assert res[tie]["action"] == "kept" and res[tie]["why"].startswith("tie")   # its old hook stays
    assert (run_dir(tie) / "video.mp4").read_bytes() == b"old video" and not (run_dir(tie) / "video_oldhook.mp4").exists()
    for rid in (first, second, waiting):
        d = run_dir(rid)
        assert res[rid]["action"] == "rebranded"
        assert (d / "video_oldhook.mp4").read_bytes() == b"old video" and (d / "thumb_oldhook.jpg").read_bytes() == b"old cover"
        assert (d / "video.mp4").read_bytes() == b"new video old video"         # audio stream copied from the old file
        meta = json.loads((d / "meta.json").read_text())
        assert meta["opening"]["question"] == "Which country ends up last?" and meta["hook_template"] == "which_last"
        assert json.loads((d / "meta_oldhook.json").read_text())["opening"]["question"] in (
            "Who overtakes who?", "Which country ends up last?")
        assert db.get_run(rid)["stage"] == "awaiting_approval" and db.get_run(rid)["hook_template"] == "which_last"
    assert {c["audio_from"] for c in fake_render} == {"video_oldhook.mp4"}
    assert all(c["countries"] == ["IND", "USA", "FRA", "JPN"] for c in fake_render)     # same data, same order

    from pipeline import posting, review
    assert [r["id"] for r in review.queue()][:2] == [first, second]   # the ones that were approved come first
    newer = reel(db, "Which country ends up last?")                    # approved before they are approved again
    actions.approve(newer, "telegram")
    actions.approve(second, "telegram")
    actions.approve(first, "telegram")
    assert [r["id"] for r in posting.queue()] == [first, second, newer]   # their original order, ahead of newer
    assert {r: db.get_run(r)["post_pos"] for r in (first, second)} == places
    assert db.get_run(first)["prior_post_pos"] is None

    again = {r["run_id"]: r for r in rebrand.rebrand_all([waiting], send=False)}
    assert (run_dir(waiting) / "video_oldhook.mp4").read_bytes() == b"old video"   # a second run keeps the first originals
    assert again[waiting]["action"] == "rebranded"


def test_rebrand_refuses_a_published_reel(env, fake_render):
    from pipeline import rebrand
    db = env
    rid = reel(db, "Who overtakes who?")
    db.transition(rid, "approved")
    db.transition(rid, "publishing")
    db.transition(rid, "published")
    assert rebrand.rebrand_run(rid)["action"] == "refused" and not fake_render


# ------------------------------------------------------------------ stats: on startup, every pull kept, failures harmless

def live_posts(db):
    from pipeline.config import run_dir
    rid = reel(db, "Which country ends up last?")
    db.transition(rid, "approved")
    db.transition(rid, "publishing")
    db.upsert_post(rid, "instagram", "live", remote_id="ig1")
    db.upsert_post(rid, "facebook", "live", remote_id="fb1")
    db.transition(rid, "published")
    assert (run_dir(rid) / "meta.json").exists()
    return rid


def test_stats_pull_once_at_startup_then_every_6_hours(env, monkeypatch):
    from pipeline import analytics
    pulls = []
    monkeypatch.setattr(analytics, "_collect", lambda: pulls.append(1) or {})
    monkeypatch.setattr(analytics, "send_summary", lambda: None)
    monkeypatch.setattr(analytics, "_pulled_here", False)
    now = datetime.now(timezone.utc)
    env.kv_set(analytics.K_LAST_RUN, env.now())           # the bot before the restart pulled a moment ago
    assert analytics.tick(now, background=False) is True and len(pulls) == 1    # pulls on startup anyway
    assert analytics.tick(now + timedelta(hours=2), background=False) is False
    assert analytics.tick(now + timedelta(hours=6, minutes=1), background=False) is True and len(pulls) == 2


def test_every_pull_is_kept_and_facebook_and_refused_metrics_are_handled(env, monkeypatch):
    from pipeline import analytics, meta_api
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(analytics, "_refused", {"instagram": set(), "facebook": set()})
    rid = live_posts(env)

    def get(url, params=None, timeout=None):
        metrics = params["metric"].split(",")
        if "/video_insights" in url:
            data = {"fb_reels_total_plays": 3, "post_video_avg_time_watched": 4200,
                    "post_video_social_actions": {"SHARE": 1}, "post_video_likes_by_reaction_type": {"LIKE": 2}}
        else:
            if "reels_skip_rate" in metrics and len(metrics) > 1:     # Meta refuses one name: the rest still land
                return Resp(400, {"error": {"code": 100, "message": "(#100) The value must be a valid insights metric"}})
            if metrics == ["reels_skip_rate"]:
                return Resp(400, {"error": {"code": 100, "message": "(#100) The value must be a valid insights metric"}})
            data = {"views": 2123, "reach": 1531, "shares": 0, "saved": 0, "ig_reels_avg_watch_time": 12110}
        return Resp(200, {"data": [{"name": m, "values": [{"value": data[m]}]} for m in metrics if m in data]})

    monkeypatch.setattr(meta_api, "get", get)
    assert analytics.fetch_instagram() == 1 and analytics.fetch_facebook() == 1
    assert "reels_skip_rate" in analytics._refused["instagram"]
    assert analytics.fetch_instagram() == 1                       # a second pull: a second row, nothing overwritten
    rows = {s["platform"]: s for s in env.latest_stats() if s["run_id"] == rid}
    assert (rows["instagram"]["views"], rows["instagram"]["avg_watch_s"], rows["instagram"]["shares"]) == (2123, 12.11, 0)
    assert rows["instagram"]["avg_view_pct"] == 55.0                                 # 12.11 s of a 22 s video
    assert (rows["facebook"]["views"], rows["facebook"]["shares"], rows["facebook"]["likes"]) == (3, 1, 2)
    with env.db() as c:
        assert c.execute("SELECT COUNT(*) FROM stats").fetchone()[0] == 3


def test_a_failing_stats_pull_never_stops_the_bot_approvals_or_posting(env, monkeypatch, caplog):
    """Meta hangs, then fails, while the bot loop goes on: posting.tick keeps running, a reel can be
    approved meanwhile, the error is logged and the next cycle tries again."""
    from pipeline import actions, analytics, approve_telegram as tg, makequeue, meta_api, posting
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    live_posts(env)
    waiting = reel(env, "Which country ends up last?")
    release = threading.Event()

    def get(url, params=None, timeout=None):
        release.wait(10)
        raise requests.ConnectionError("Meta is down")

    monkeypatch.setattr(meta_api, "get", get)
    monkeypatch.setattr(analytics, "_pulled_here", False)
    monkeypatch.setattr(analytics, "_thread", None)
    monkeypatch.setattr(analytics, "send_summary", lambda: None)
    ticks, loops = [], []
    monkeypatch.setattr(posting, "tick", lambda: ticks.append(1))
    monkeypatch.setattr(tg, "_idle", lambda s: None)

    def work():
        loops.append(1)
        if len(loops) == 2:
            assert analytics._thread.is_alive()          # the pull is still hanging on Meta ...
            actions.approve(waiting, "telegram")          # ... and approving works meanwhile
            raise KeyboardInterrupt

    monkeypatch.setattr(makequeue, "work", work)
    assert tg._bot_loop() == 0
    assert len(ticks) == 2 and env.get_run(waiting)["stage"] == "approved"
    release.set()
    analytics._thread.join(10)
    assert not analytics._thread.is_alive()
    with env.db() as c:
        assert c.execute("SELECT COUNT(*) FROM stats").fetchone()[0] == 0
    assert any("insights" in r.getMessage() and "failed" in r.getMessage() for r in caplog.records)
    later = datetime.now(timezone.utc) + timedelta(hours=6, minutes=1)
    monkeypatch.setattr(meta_api, "get", lambda url, params=None, timeout=None: Resp(500, {"error": {"message": "x"}}))
    assert analytics.tick(later, background=False) is True        # the next cycle tries again, still no crash
