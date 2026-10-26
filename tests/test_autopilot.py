"""The autopilot (AUTOPILOT=true): the breakout model (learn.py), the slot picker and post job (autopick.py), the
generation run (generator.py), alerts and healthchecks (alerts.py), the job wrapper and the MIGRATED_TO_SERVER guard
(autopilot.py), the caption answer guard, and the deploy files. Time is simulated (a day a month ahead); publishing,
Telegram and healthchecks.io are faked."""
import json
import random
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

from pipeline import alerts

IST = ZoneInfo("Asia/Kolkata")
DAY = (datetime.now(IST) + timedelta(days=30)).date()
FLOW = ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]
ISO = {"India": "IND", "China": "CHN", "Germany": "DEU", "Japan": "JPN", "Brazil": "BRA", "Nigeria": "NGA",
       "Mexico": "MEX", "Kenya": "KEN"}
REAL_SEND = alerts.send


def at(days: int, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=IST).astimezone(timezone.utc) + timedelta(days=days)


@pytest.fixture
def ap(temp_db, tmp_path, monkeypatch, pinned_config):
    from pipeline import learn
    monkeypatch.setenv("AUTOPILOT", "true")
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"),
                                  migrated_marker=str(tmp_path / "MIGRATED_TO_SERVER"))
    pinned_config["youtube"]["enabled"] = False            # as shipped: Instagram (+ Facebook) only
    pinned_config["autopilot"].update(anchor_day=DAY.isoformat(), scoring_since="2020-01-01", discover_below=0)
    learn.reset_cache()
    sent = []
    monkeypatch.setattr(alerts, "send", lambda text: sent.append(text) or True)
    return {"db": temp_db, "sent": sent, "tmp": tmp_path, "cfg": pinned_config}


def reel(e, topic, answer="India", others=("China", "Germany", "Japan"), end=2023, short=None, cold=False,
         phrase="Which country ends up last?", duration=22.0, score=50.0):
    """A reel in the pool (awaiting_approval) with its files: meta.json (question + answer), pick.json, data.json
    (every country through `end`, except `short`, which stops 2 years early) and a video."""
    from pipeline.config import run_dir
    db = e["db"]
    rid = db.create_run(topic)
    isos = [ISO[answer]] + [ISO[o] for o in others]
    for st in FLOW:
        db.transition(rid, st, **({"countries": isos, "country_set": ",".join(sorted(isos)), "score": score}
                                  if st == "picked" else {}))
    db.set_hook_template(rid, "which_last", phrase)
    db.set_cold_open(rid, cold, "a beat" if cold else "control side")
    write_files(rid, isos, answer, end, short, phrase, duration)
    return rid


def write_files(rid, isos, answer, end=2023, short=None, phrase="Which country ends up last?", duration=22.0):
    from pipeline.config import run_dir
    d = run_dir(rid)
    stop = {c: end - 2 if short and c == ISO[short] else end for c in isos}
    series = {c: [[y, float(10 + i + (y - 1990) * (1 if i % 2 else -0.5))] for y in range(1990, stop[c] + 1)]
              for i, c in enumerate(isos)}
    (d / "meta.json").write_text(json.dumps({"duration_seconds": duration, "opening": {
        "question": phrase, "highlight": [answer], "answer": f"{answer} ends up last"}}), encoding="utf-8")
    (d / "pick.json").write_text(json.dumps({"countries": isos, "x_start": 1990, "x_end": end}), encoding="utf-8")
    (d / "data.json").write_text(json.dumps({"series": series}), encoding="utf-8")
    (d / "video.mp4").write_bytes(b"video")


def outcome(category, views, **feats):
    f = {"category": category, "hook_template": "which_last", "hook_phrase": "Which country ends up last?",
         "cold_open": "no", "slot": "14:30", "length": "mid", **feats}
    return {"views72": float(views), "weight": 1.0, "features": f, "posted_at": "2026-10-20T09:00:00Z",
            "estimated": False}


def history(monkeypatch, rows):
    """The model learns from these outcomes (fresh copies on each build)."""
    from pipeline import learn
    monkeypatch.setattr(learn, "outcomes", lambda now=None: [dict(r, features=dict(r["features"])) for r in rows])


# economy has 5 scored posts (exploited); health, planet, people have fewer (explored)
HISTORY = ([outcome("economy", v) for v in (150, 2200, 160, 1900, 140)] +
           [outcome("health", 2500), outcome("planet", 150), outcome("people", 170)])


def fake_publish(e, fail_first=0):
    """orchestrator.publish_run, faked: fails `fail_first` times, then goes live on Instagram."""
    calls = []

    def publish(rid, platform=None):
        db = e["db"]
        calls.append(rid)
        db.transition(rid, "publishing", reset=True)
        if len(calls) <= fail_first:
            db.upsert_post(rid, "instagram", "failed", message="Meta said no")
            db.fail(rid, "publish", "all platforms failed: instagram=failed")
            return False
        db.upsert_post(rid, "instagram", "live", remote_id=f"ig{len(calls)}", url="https://instagram.com/reel/x")
        db.transition(rid, "published", "published: instagram=live")
        return True
    return publish, calls


# ------------------------------------------------------------------ the breakout model

def test_one_lucky_breakout_does_not_outrank_a_category_with_steady_results(ap):
    from pipeline import learn
    rows = ([outcome("lucky", 2500)] + [outcome("steady", v) for v in (2400, 1900, 160, 2100, 150, 2600)] +
            [outcome("other", v) for v in (150,) * 8 + (2000, 2200)])
    m = learn.build(rows)
    assert m.threshold == 750 and m.base == pytest.approx(7 / 17)        # 5 x the median (150), above the 500 floor
    lucky, steady = m.mean("category", "lucky"), m.mean("category", "steady")
    assert lucky == pytest.approx((4 * m.base + 1) / 5)                  # one reel moves it a fifth of the way, no more
    assert steady > lucky
    base = dict(rows[0]["features"])
    assert m.score({**base, "category": "steady"})[0] > m.score({**base, "category": "lucky"})[0]
    states = learn.refresh_categories(m)
    assert states["lucky"]["explore"] and not states["lucky"]["exploit"]  # 1 post: explored, never exploited
    assert states["steady"]["exploit"]


def test_the_breakout_threshold_follows_the_account_and_never_drops_under_the_floor(ap):
    from pipeline import learn
    assert learn.build([outcome("a", 100) for _ in range(9)]).threshold == 500
    m = learn.build([outcome("a", v) for v in [300] * 20 + [5000] * 3])
    assert m.threshold == 1500 and sum(r["breakout"] for r in m.rows) == 3


def test_snapshots_at_24_and_72_hours_and_the_estimate_for_younger_reels(ap):
    from pipeline import learn
    db = ap["db"]
    t0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)

    def post(topic, ages_views, version=2):
        rid = reel(ap, topic)
        db.set_format_version(rid, version)
        db.upsert_post(rid, "instagram", "live", remote_id="m" + rid[-4:])
        pid = db.get_posts(rid)[0]["id"]
        with db.db() as c:
            c.execute("UPDATE posts SET created_at=? WHERE id=?", (t0.strftime("%Y-%m-%dT%H:%M:%SZ"), pid))
        for age, views in ages_views:
            db.add_stats(pid, views=views)
            with db.db() as c:
                c.execute("UPDATE stats SET fetched_at=? WHERE id=(SELECT MAX(id) FROM stats)",
                          ((t0 + timedelta(hours=age)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        return pid
    a = post("inflation", [(5, 100), (21, 150), (45, 155), (75, 160)])
    b = post("child_mortality", [(22, 2000)])
    c = post("co2_per_capita", [(23, 1000), (70, 1100)], version=1)
    assert learn.take_snapshots(now=t0 + timedelta(hours=100)) == 5
    snaps = learn.snapshots()
    assert (snaps[a][24]["age_hours"], snaps[a][24]["views"]) == (21, 150)   # nearest 24 h, its real age kept
    assert (snaps[a][72]["age_hours"], snaps[a][72]["views"]) == (75, 160)
    assert 72 not in snaps[b]
    rows = {r["post_id"]: r for r in learn.outcomes()}
    assert rows[a]["views72"] == 160 and not rows[a]["estimated"]
    ratio = (160 / 150 + 1100 / 1000) / 2                                      # the account's typical 72 h / 24 h
    assert rows[b]["estimated"] and rows[b]["views72"] == pytest.approx(2000 * ratio, abs=0.1)
    assert rows[c]["weight"] == 0.5 and rows[a]["weight"] == 1.0              # an older format_version counts half
    assert learn.take_snapshots(now=t0 + timedelta(hours=100)) == 0           # each snapshot is taken once


def test_a_category_is_retired_explored_once_after_30_days_and_retired_again(ap):
    from pipeline import learn
    rows = [outcome("economy", v) for v in [2000] * 25 + [150] * 5] + [outcome("health", 150) for _ in range(14)]
    m = learn.build(rows)
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert m.upper("health") < 0.5 * m.base
    st = learn.refresh_categories(m, t0)["health"]
    assert st["retired"] and not st["exploit"] and not st["explore"]
    st = learn.refresh_categories(m, t0 + timedelta(days=31))["health"]
    assert st["retired"] and st["retry_due"] and st["explore"]                # due its one retry
    retry = outcome("health", 150)
    retry["posted_at"] = (t0 + timedelta(days=32)).strftime("%Y-%m-%dT%H:%M:%SZ")
    m2 = learn.build(rows + [retry])
    st = learn.refresh_categories(m2, t0 + timedelta(days=34))["health"]
    assert st["retired"] and not st["retry_due"]                              # the retry flopped: a new 30 days
    st = learn.refresh_categories(m2, t0 + timedelta(days=60))["health"]
    assert not st["retry_due"]


def test_the_cold_open_chance_follows_the_data_but_stays_between_20_and_80_percent(ap):
    from pipeline import learn
    floor = [outcome("a", 150, cold_open="yes") for _ in range(4)] + [outcome("a", 150) for _ in range(10)]
    win = floor + [outcome("a", 2000, cold_open="yes") for _ in range(6)]   # breakouts are the few, as on the account
    lose = floor + [outcome("a", 2000) for _ in range(6)]
    assert learn.cold_open_chance(learn.build(win)) == 0.8
    assert learn.cold_open_chance(learn.build(lose)) == 0.2
    assert 0.4 < learn.cold_open_chance(learn.build([outcome("a", 150), outcome("a", 2000)])) < 0.6


def test_new_reels_get_a_cold_open_by_thompson_sampling(ap, monkeypatch):
    from pipeline import coldopen, learn
    ap["cfg"]["cold_open"]["mode"] = "thompson"
    monkeypatch.setattr(learn, "cold_open_chance", lambda *a, **k: 0.8)
    picks = [coldopen.decide(None, rng=random.Random(i))["want"] for i in range(400)]
    assert 0.7 < sum(picks) / len(picks) < 0.9
    d = coldopen.decide(None, rng=random.Random(3))
    assert d["chance"] == 0.8 and (d["want"] or "Thompson sampling" in d["why"])


# ------------------------------------------------------------------ the picker

def test_exploit_and_explore_swap_slots_every_day(ap, monkeypatch):
    from pipeline import autopick
    days = [DAY + timedelta(days=k) for k in range(10)]
    assert [autopick.role_for(d, 1) for d in days[:3]] == ["exploit", "explore", "exploit"]
    assert all(autopick.role_for(d, 1) != autopick.role_for(d, 2) for d in days)
    assert sum(autopick.role_for(d, 1) == "exploit" for d in days) == 5    # each slot time exploits every other day
    history(monkeypatch, HISTORY)
    reel(ap, "inflation", answer="India", score=80)
    reel(ap, "child_mortality", answer="Brazil", others=("China", "Mexico", "Kenya"))
    first, second = autopick.plan(DAY, rng=random.Random(1))
    assert (first["role"], first["category"]) == ("exploit", "economy")
    assert (second["role"], second["category"]) == ("explore", "health")
    first, second = autopick.plan(DAY + timedelta(days=1), rng=random.Random(1))
    assert (first["role"], first["category"]) == ("explore", "health")
    assert (second["role"], second["category"]) == ("exploit", "economy")


def test_the_days_two_posts_differ_in_category_and_hook_answer_country(ap, monkeypatch):
    from pipeline import autopick
    history(monkeypatch, HISTORY)
    a = reel(ap, "inflation", answer="India", score=90)                       # economy: the best exploit reel
    b = reel(ap, "gdp_growth", answer="Brazil", others=("Mexico", "Kenya", "Nigeria"), score=50)   # economy too
    c = reel(ap, "child_mortality", answer="India", others=("Mexico", "Kenya", "Nigeria"))         # India again
    d = reel(ap, "co2_per_capita", answer="Brazil", others=("China", "Japan", "Germany"))
    first, second = autopick.plan(DAY, rng=random.Random(2))
    assert first["run_id"] == a and first["role"] == "exploit"
    assert second["run_id"] == d
    assert "same category (economy)" in second["excluded"][b]
    assert "answer country India" in second["excluded"][c]


def test_no_explore_reel_posts_the_second_best_exploit_reel_and_says_so(ap, monkeypatch):
    from pipeline import autopick
    history(monkeypatch, HISTORY + [outcome("health", 150) for _ in range(3)])   # health has 4 posts: exploited
    best = reel(ap, "inflation", answer="India", score=90)
    reel(ap, "gdp_growth", answer="Brazil", others=("Mexico", "Kenya", "Nigeria"), score=60)
    other = reel(ap, "child_mortality", answer="Nigeria", others=("Mexico", "Kenya", "Brazil"), score=40)
    d = autopick.plan(DAY + timedelta(days=1), indexes=(1,), rng=random.Random(0))[0]   # slot 1 explores that day
    assert d["role"] == "explore" and d["mode"] == "explore->exploit" and "second-best" in d["note"]
    assert d["run_id"] == other and d["run_id"] != best                       # not the best, nor its category


def test_with_no_category_at_4_posts_both_slots_use_thompson_sampling_over_the_pool(ap, monkeypatch):
    from pipeline import autopick
    history(monkeypatch, [outcome("economy", 150), outcome("health", 2000)])
    reel(ap, "inflation", answer="India")
    reel(ap, "child_mortality", answer="Brazil", others=("Mexico", "Kenya", "Nigeria"))
    first, second = autopick.plan(DAY, rng=random.Random(5))
    assert first["mode"] == second["mode"] == "thompson" and first["run_id"] and second["run_id"]
    assert first["category"] != second["category"]
    assert all("draw_log_odds" in p for p in first["breakdown"]["parts"].values())


def test_a_reel_is_never_posted_with_a_short_series_a_used_set_or_another_hook(ap, monkeypatch):
    from pipeline import autopick
    history(monkeypatch, HISTORY)
    short = reel(ap, "inflation", short="Japan")
    who = reel(ap, "gdp_growth", answer="Brazil", others=("Mexico", "Kenya", "Nigeria"), phrase="Who drops to the bottom?")
    used = reel(ap, "unemployment", answer="Kenya", others=("Mexico", "Brazil", "Nigeria"))
    twin = reel(ap, "unemployment", answer="Kenya", others=("Mexico", "Brazil", "Nigeria"))
    ap["db"].upsert_post(twin, "instagram", "live", remote_id="x")              # the same topic + set already went out
    d = autopick.plan(DAY, indexes=(1,))[0]
    assert "no 2023 data for JPN" in d["excluded"][short]
    assert "not a \"Which country...?\"" in d["excluded"][who]
    assert "posted before" in d["excluded"][used]
    assert d["run_id"] is None


def test_every_country_in_a_reel_has_data_through_its_final_year():
    from pipeline import picker
    rng = random.Random(7)
    names = ["IND", "CHN", "USA", "BRA", "DEU", "NGA", "JPN", "MEX"]
    series = {}
    for i, c in enumerate(names):
        trend = [1.2, -1.5, 0.4, -0.8, 0.9, -1.1, 0.2, 1.4][i]
        last = 2021 if c == "USA" else 2023
        series[c] = [[y, 50 + trend * (y - 1990) + 9 * __import__("math").sin((y - 1990) * (0.35 + i / 20)) +
                      rng.uniform(-1.5, 1.5)] for y in range(1990, last + 1)]
    topic = {"id": "t", "start_year": 1990, "min_meaningful": 1.0}
    from pipeline.score import score_topic
    scored = score_topic({"series": series}, topic)
    assert picker.final_year([s for s in scored if s["ok"]], series, 4, 4) == 2023
    p = picker.pick(scored, series, topic, seed=1)
    assert p["x_end"] == 2023 and "USA" not in p["countries"]
    assert all(any(int(y) == p["x_end"] for y, _ in series[c]) for c in p["countries"])


# ------------------------------------------------------------------ the post job

def test_the_post_job_picks_posts_and_records_its_decision_once_per_slot(ap, monkeypatch):
    from pipeline import autopick, orchestrator
    history(monkeypatch, HISTORY)
    publish, calls = fake_publish(ap)
    monkeypatch.setattr(orchestrator, "publish_run", publish)
    a = reel(ap, "inflation", answer="India", score=90)
    b = reel(ap, "child_mortality", answer="Brazil", others=("Mexico", "Kenya", "Nigeria"))
    assert autopick.post(1, now=at(0, "14:31"), wait_s=0) == 0
    assert calls == [a]
    run = ap["db"].get_run(a)
    assert run["stage"] == "published" and run["approved_by"].startswith("autopilot") and run["post_slot"] == "14:30"
    rows = autopick.decisions_on(DAY)
    assert [(r["slot"], r["role"], r["run_id"]) for r in rows] == [("14:30", "exploit", a)]
    assert rows[0]["breakdown"]["parts"]["category"]["value"] == "economy" and "runners_up" in rows[0]
    assert autopick.post(1, now=at(0, "14:40"), wait_s=0) == 0 and calls == [a]   # the slot is claimed: never twice
    assert autopick.post(2, now=at(0, "16:01"), wait_s=0) == 0 and calls == [a, b]
    assert ap["sent"] == []                                                    # silent while healthy


def test_a_failed_post_is_retried_once_then_alerts(ap, monkeypatch):
    from pipeline import autopick, orchestrator
    history(monkeypatch, HISTORY)
    publish, calls = fake_publish(ap, fail_first=1)
    monkeypatch.setattr(orchestrator, "publish_run", publish)
    a = reel(ap, "inflation")
    assert autopick.post(1, now=at(0, "14:31"), wait_s=0) == 0 and calls == [a, a]
    assert ap["db"].get_run(a)["stage"] == "published" and ap["sent"] == []
    publish, calls = fake_publish(ap, fail_first=5)
    monkeypatch.setattr(orchestrator, "publish_run", publish)
    b = reel(ap, "child_mortality", answer="Brazil", others=("Mexico", "Kenya", "Nigeria"))
    assert autopick.post(2, now=at(0, "16:01"), wait_s=0) == 1 and calls == [b, b]
    assert len(ap["sent"]) == 1 and "failed after 1 retry" in ap["sent"][0] and "Meta said no" in ap["sent"][0]


def test_the_late_slot_guard_refuses_and_alerts_once(ap, monkeypatch):
    from pipeline import autopick, orchestrator, posting
    history(monkeypatch, HISTORY)
    publish, calls = fake_publish(ap)
    monkeypatch.setattr(orchestrator, "publish_run", publish)
    reel(ap, "inflation")
    assert autopick.post(1, now=at(0, "16:01"), wait_s=0) == 0                # 91 min after 14:30
    assert calls == [] and len(ap["sent"]) == 1 and "14:30 IST post was skipped" in ap["sent"][0]
    assert autopick.post(1, now=at(0, "16:10"), wait_s=0) == 0 and len(ap["sent"]) == 1   # the same alert: once in 12 h
    ap["db"].kv_set(posting.K_LAST, at(1, "15:40").strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert "20 min ago" in autopick.late_reason(2, at(1, "16:00"))             # too soon after the last post
    assert autopick.late_reason(2, at(1, "16:45")) is None
    assert autopick.late_reason(1, at(1, "13:00"))                             # before 14:30: yesterday's slot, far too late


def test_alerts_go_out_once_per_12_hours_and_one_line_when_they_clear(ap):
    t0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    assert alerts.fire("x", "the thing broke", "the thing's log", now=t0)
    assert not alerts.fire("x", "the thing broke", "the thing's log", now=t0 + timedelta(hours=11))
    assert alerts.fire("x", "the thing broke", "the thing's log", now=t0 + timedelta(hours=13))
    assert ap["sent"][0].splitlines() == ["⚠️ Graphony: the thing broke", "Check: the thing's log"]
    assert alerts.clear("x") and not alerts.clear("x") and not alerts.clear("never-fired")
    assert ap["sent"][-1] == "✅ Resolved: Graphony: the thing broke" and len(ap["sent"]) == 3


def test_a_failing_healthcheck_ping_never_breaks_a_job(ap, monkeypatch):
    from pipeline import autopick, autopilot, orchestrator
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.example/abc")
    pings = []

    def down(url, timeout):
        pings.append((url, timeout))
        raise requests.ConnectionError("healthchecks.io is down")
    monkeypatch.setattr(alerts.requests, "get", down)
    history(monkeypatch, HISTORY)
    publish, _ = fake_publish(ap)
    monkeypatch.setattr(orchestrator, "publish_run", publish)
    reel(ap, "inflation")
    assert autopilot.run_job("post", autopick.post, 1, now=at(0, "14:31"), wait_s=0) == 0

    def crash():
        raise RuntimeError("boom")
    assert autopilot.run_job("insights", crash) == 1
    assert pings == [("https://hc-ping.example/abc", 10), ("https://hc-ping.example/abc/fail", 10)]
    assert len(ap["sent"]) == 1 and "the insights job crashed (RuntimeError: boom)" in ap["sent"][0]
    monkeypatch.delenv("HEALTHCHECK_URL")
    assert alerts.ping() is False                                              # empty: a warning, no request


# ------------------------------------------------------------------ the pool and generation

def test_pool_refill_math(ap):
    from pipeline import generator
    assert [generator.plan_count(n) for n in (0, 3, 6, 7, 9, 10, 14)] == [5, 5, 4, 3, 1, 0, 0]


def test_generation_tops_up_the_pool_and_keeps_3_explore_reels(ap, monkeypatch):
    from pipeline import generator, orchestrator
    history(monkeypatch, HISTORY)
    for k, t in enumerate(["inflation", "gdp_growth", "unemployment", "remittances", "military_spending",
                           "manufacturing_share", "tourism_arrivals"]):
        reel(ap, t, answer=list(ISO)[k % 8], others=[x for x in ISO if x != list(ISO)[k % 8]][:3])
    cands = [{"id": "gdp_per_capita", "name": "Income", "category": "economy", "score": 90},
             {"id": "child_mortality", "name": "Child mortality", "category": "health", "score": 40},
             {"id": "co2_per_capita", "name": "Carbon", "category": "planet", "score": 40},
             {"id": "net_migration", "name": "Migration", "category": "people", "score": 40},
             {"id": "internet_users", "name": "Internet", "category": "tech", "score": 40}]
    monkeypatch.setattr(generator, "candidate_topics", lambda exclude=(): [t for t in cands if t["id"] not in exclude])

    def make(rid):
        db = ap["db"]
        for st in FLOW:
            db.transition(rid, st, **({"country_set": rid} if st == "picked" else {}))
        db.set_hook_template(rid, "which_last", "Which country ends up last?")
        write_files(rid, ["IND", "CHN", "DEU", "JPN"], "India")
        return True
    monkeypatch.setattr(orchestrator, "run_pipeline", make)
    assert generator.generate(rng=random.Random(4)) == 0
    new = [r for r in ap["db"].list_runs() if r["topic_id"] in {t["id"] for t in cands}]
    assert len(new) == 3                                                       # min(5, 10 - 7)
    assert {next(t["category"] for t in cands if t["id"] == r["topic_id"]) for r in new} <= {"health", "planet",
                                                                                             "people", "tech"}
    assert generator.generate() == 0 and len(ap["db"].list_runs(stage="awaiting_approval")) == 10   # full: none


def test_a_reel_unpicked_on_5_days_is_dropped_with_its_media_and_keeps_its_row(ap):
    from pipeline import autopick
    from pipeline.config import run_dir
    x = reel(ap, "inflation")
    y = reel(ap, "child_mortality", answer="Brazil", others=("Mexico", "Kenya", "Nigeria"))
    with ap["db"].db() as c:
        for k in range(5):
            day = (DAY - timedelta(days=10 - k)).isoformat()
            for slot in ("14:30", "16:00"):                                    # two slots a day: still one loss a day
                picked = y if (k == 2 and slot == "14:30") else "r-someone-else"
                c.execute("INSERT INTO decisions(day, slot, role, run_id, breakdown, runners_up, created_at) "
                          "VALUES (?,?,?,?,?,?,?)", (day, slot, "exploit", picked, json.dumps({"eligible": [x, y]}),
                                                     "[]", "2026-01-01T00:00:00Z"))
    assert autopick.count_losses(DAY) == [x]
    gone = ap["db"].get_run(x)
    assert gone["stage"] == "dropped" and gone["pool_losses"] == 5 and gone["media_deleted_at"]
    assert not (run_dir(x) / "video.mp4").exists() and (run_dir(x) / "meta.json").exists()
    assert ap["db"].get_run(y)["pool_losses"] == 4 and ap["db"].get_run(y)["stage"] == "awaiting_approval"
    assert autopick.count_losses(DAY) == [] and ap["db"].get_run(y)["pool_losses"] == 4     # each day counted once


def test_posted_media_is_deleted_after_14_days(ap):
    from pipeline import autopick
    from pipeline.config import run_dir
    old, new = reel(ap, "inflation"), reel(ap, "child_mortality")
    for rid, days in ((old, 15), (new, 3)):
        ap["db"].upsert_post(rid, "instagram", "live", remote_id="m")
        ap["db"].transition(rid, "published", reset=True)
        with ap["db"].db() as c:
            c.execute("UPDATE posts SET created_at=? WHERE run_id=?",
                      ((datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"), rid))
    assert autopick.cleanup_posted_media() == 1
    assert not (run_dir(old) / "video.mp4").exists() and (run_dir(new) / "video.mp4").exists()
    assert ap["db"].get_run(old)["stage"] == "published"


# ------------------------------------------------------------------ captions never name the answer

def test_captions_never_name_the_hooks_answer_country(ap):
    from pipeline import captions
    from pipeline.config import run_dir
    assert captions.metric_line({"name": "Inflation", "subtitle": "Consumer prices"}, ["India", "Brazil", "Japan"],
                                1990, 2023, hide=True) == "Inflation: consumer prices in 3 countries, 1990 to 2023."
    tags, _ = captions.pick_hashtags({"id": "inflation"}, [{"country": "India"}, {"country": "Japan"}], avoid=["India"])
    assert "#india" not in tags
    rid = reel(ap, "inflation", answer="India")
    c = {"variants": [{"caption": "Which country ends up last? Guess before the end\nInflation: consumer prices in "
                                  "India, China, Germany and Japan, 1990 to 2023.\nWho was your guess?",
                       "template_id": "wl_guess"}],
         "metric_line": "Inflation: consumer prices in India, China, Germany and Japan, 1990 to 2023.",
         "hashtags": ["#dataviz", "#india", "#inflation"], "credit": ["Data: World Bank WDI (CC BY 4.0)."], "chosen": 0}
    captions.save(rid, c)
    meta = json.loads((run_dir(rid) / "meta.json").read_text(encoding="utf-8"))
    posted = captions.apply_to_meta(rid, meta)["description_instagram"]
    assert "india" not in posted.lower() and "in 4 countries, 1990 to 2023." in posted and "#inflation" in posted
    assert captions.names_in("Watch #UnitedStates", ["United States"]) and not captions.names_in("Indiana", ["India"])
    captions.save(rid, {**c, "answer_names": ["India"]})                     # what a new captions.json records
    with pytest.raises(captions.CaptionRejected, match="India"):              # an owner's edit that names it
        captions.save_edit(rid, "India is last, of course", ["#data"])
    ap["cfg"]["captions"]["use_for_posting"] = False                          # the old meta.json caption is guarded too
    old = captions.apply_to_meta(rid, {**meta, "description_instagram": "Inflation\nCountries: India, Japan.\n#data"})
    assert "India" not in old["description_instagram"] and "#data" in old["description_instagram"]


# ------------------------------------------------------------------ no Telegram bot, and the move to the server

def test_autopilot_never_calls_getupdates(ap, monkeypatch):
    from pipeline import approve_telegram, autopilot
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    urls = []
    ok = SimpleNamespace(status_code=200, text="", json=lambda: {"ok": True, "result": []})
    monkeypatch.setattr(requests, "post", lambda url, **kw: urls.append(url) or ok)
    monkeypatch.setattr(requests, "get", lambda url, **kw: urls.append(url) or ok)
    monkeypatch.setattr(approve_telegram, "poll_once", lambda *a, **k: pytest.fail("long polling in autopilot"))
    monkeypatch.setattr(approve_telegram, "_bot_loop", lambda: pytest.fail("the Telegram bot in autopilot"))
    started = []
    monkeypatch.setattr(autopilot.subprocess, "run", lambda argv, **kw: started.append(argv[2:]) or SimpleNamespace(returncode=0))

    def stop(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(autopilot.time, "sleep", stop)
    assert approve_telegram.run_bot() == 0                                    # Bot.bat runs the jobs instead
    assert ["pipeline", "insights"] in started
    monkeypatch.setattr(alerts, "send", REAL_SEND)
    alerts.fire("x", "something broke", "the log")
    assert urls and any(u.endswith("/sendMessage") for u in urls) and not any("getUpdates" in u for u in urls)


def test_migrated_to_server_stops_every_posting_entrypoint(ap, monkeypatch):
    import run
    from pipeline import actions, approve_telegram, autopilot, orchestrator, publish_instagram
    from pipeline.config import ROOT
    rid = reel(ap, "inflation")
    actions.approve(rid, "dashboard")
    autopilot.marker().write_text("moved to the server")
    uploads = []
    monkeypatch.setattr(publish_instagram, "publish", lambda *a: uploads.append(a) or "live")
    assert orchestrator.publish_run(rid) is False and uploads == [] and ap["db"].get_run(rid)["stage"] == "approved"
    for job in ("generate", "post", "insights", "health"):
        assert autopilot.run_job(job, lambda: pytest.fail("a job ran")) == autopilot.REFUSED
    assert approve_telegram.run_bot() == autopilot.REFUSED
    monkeypatch.setenv("AUTOPILOT", "false")
    assert approve_telegram.run_bot() == autopilot.REFUSED                    # the old approval bot too
    assert autopilot.loop(once=True, runner=lambda argv: pytest.fail("a job started")) == autopilot.REFUSED
    assert run.cmd_publish(SimpleNamespace(run=None, platform=None)) == autopilot.REFUSED
    assert ap["sent"] == []
    assert "MIGRATED_TO_SERVER" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_autopilot_is_on_unless_env_says_false(ap, monkeypatch):
    from pipeline import autopilot
    assert autopilot.on()
    for off in ("false", "0", "no", "OFF"):
        monkeypatch.setenv("AUTOPILOT", off)
        assert not autopilot.on()
    monkeypatch.delenv("AUTOPILOT")
    assert autopilot.on()


def test_jobs_never_overlap(ap):
    from filelock import Timeout
    from pipeline import autopilot
    with autopilot.job_lock("generate"):
        with pytest.raises(Timeout):
            with autopilot.job_lock("post", wait_s=0.1):
                pass
        assert autopilot.run_job("insights", lambda: 0, lock_wait_s=0.1) == 1   # it waits, then gives up and alerts
    assert "could not start" in ap["sent"][0]
    assert autopilot.run_job("insights", lambda: 0) == 0


# ------------------------------------------------------------------ deploy files

def test_systemd_units_and_timers(pinned_config):
    from pipeline.config import ROOT
    d = ROOT / "deploy"
    want = {"generate": "*-*-* 21:30:00 UTC", "post-1": "*-*-* 09:00:00 UTC", "post-2": "*-*-* 10:30:00 UTC",
            "insights": "*-*-* 00/6:15:00 UTC", "health": "*-*-* 03:30:00 UTC"}
    ist = {"generate": "03:00 IST", "post-1": "14:30 IST", "post-2": "16:00 IST", "health": "09:00 IST"}
    for name, cal in want.items():
        timer = (d / f"graphony-{name}.timer").read_text(encoding="utf-8")
        assert f"OnCalendar={cal}" in timer and "Persistent=true" in timer
        assert ist.get(name, "IST") in timer
        svc = (d / f"graphony-{name}.service").read_text(encoding="utf-8")
        assert "User=ubuntu" in svc and "WorkingDirectory=/home/ubuntu/graphony" in svc
        assert "-m pipeline " + {"post-1": "post --slot 1", "post-2": "post --slot 2"}.get(name, name) in svc
    for svc in d.glob("graphony-*.service"):
        text = svc.read_text(encoding="utf-8")
        assert "Nice=19" in text and "IOSchedulingClass=idle" in text, svc.name
    gen = (d / "graphony-generate.service").read_text(encoding="utf-8")
    assert "CPUQuota=100%" in gen and "MemoryMax=1500M" in gen
    assert "-m pipeline dashboard" in (d / "graphony-dashboard.service").read_text(encoding="utf-8")
    assert (d / "README_SERVER.md").exists()


def test_the_dashboard_port_comes_from_env_and_it_binds_loopback_only(pinned_config, monkeypatch):
    from dashboard import app
    assert app.port() == 5055
    monkeypatch.setenv("DASHBOARD_PORT", "6060")
    assert app.port() == 6060
    pinned_config["dashboard"]["host"] = "0.0.0.0"
    with pytest.raises(SystemExit, match="loopback only"):
        app.main()


def test_requirements_are_pinned(pinned_config):
    from pipeline.config import ROOT
    lines = [ln.strip() for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert lines and all(re.match(r"^[A-Za-z0-9_.\-]+==[0-9][^;\s]*(;\s*python_version\s*[<>=]+\s*\"3\.\d+\")?$", ln)
                         for ln in lines), lines
    assert any(ln.startswith("filelock==") for ln in lines)


def test_no_windows_paths_in_the_code(pinned_config):
    from pipeline.config import ROOT
    for f in [ROOT / "run.py", *(ROOT / "pipeline").glob("*.py"), *(ROOT / "dashboard").glob("*.py")]:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"[A-Za-z]:\\\\(Users|Windows|Program)", text), f.name


def test_the_dashboard_shows_the_picks_read_only(ap, monkeypatch):
    from dashboard.app import app
    from pipeline import autopick
    from pipeline.config import ROOT
    history(monkeypatch, HISTORY)
    reel(ap, "inflation", score=90)
    reel(ap, "child_mortality", answer="Brazil", others=("Mexico", "Kenya", "Nigeria"))
    for d in autopick.plan(DAY, rng=random.Random(1)):
        autopick.record(d)
    page = app.test_client().get("/autopilot")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page.data.decode("utf-8")))
    assert page.status_code == 200 and "AUTOPILOT ON" in text
    assert "Inflation" in text and "Child mortality" in text and "exploit" in text and "explore" in text
    assert "<form" not in (ROOT / "dashboard" / "templates" / "autopilot.html").read_text(encoding="utf-8")


def test_discovered_topics_take_every_number_from_the_data_and_only_names_from_gemini(ap):
    from pipeline import discover, gemini
    from pipeline.config import countries
    pool = [c["iso3"] for c in countries()][:30]
    series = {c: [[y, 20.0 + i] for y in range(1990 if i < 26 else 2001, 2024)] for i, c in enumerate(pool)}
    assert discover.numbers_from_data(series, "", "Arable land (% of land area)") == \
        {"start_year": 1990, "min_meaningful": 1.7, "unit_format": "{:.1f}%"}           # 5% of the median peak (34.5)
    cand = {"name": "Arable land (% of land area)", "unit": "", "note": "", "code": "AG.LND.ARBL.ZS", "source": "worldbank"}

    def says(**reply):
        return lambda prompt: ({"name": "Arable land", "title": "Who farms the most land?",
                                "subtitle": "Arable land, % of land area", "lower_is_better": False, **reply}, "m")
    got = discover.label(cand, "farming", ask=says())
    assert got == {"name": "Arable land", "title": "Who farms the most land?", "subtitle": "Arable land, % of land area",
                   "lower_first": False}
    assert discover.label(cand, "farming", ask=says(title="The top 10 farming nations")) is None   # a made-up number
    assert discover.label(cand, "farming", ask=says(subtitle="Share of all the land in a country that is arable "
                                                             "farmland, in percent")) is None        # not one line

    def down(prompt):
        raise gemini.QuotaExhausted("no Gemini model answered")
    assert discover.label(cand, "farming", ask=down) is None                  # no name: it waits for a later run
    assert discover.quota_left() == 3


def test_a_slot_with_nothing_to_post_raises_the_pool_alert(ap, monkeypatch):
    from pipeline import autopick
    history(monkeypatch, HISTORY)
    assert autopick.post(1, now=at(0, "14:31"), wait_s=0) == 0
    assert autopick.decisions_on(DAY)[0]["run_id"] is None
    assert len(ap["sent"]) == 1 and "only 0 ready reel(s) in the pool" in ap["sent"][0]
