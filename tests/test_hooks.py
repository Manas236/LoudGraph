"""Hook templates (pipeline/hooks.py): the which_last guess-the-loser phrasings, their truth checks computed from
the data, the rotation, the end card answer that answers the question asked, hook_phrase tracking, subtitles on
one line, re-branding unposted reels (pipeline/rebrand.py), and stats that can fail without stopping the bot.
No network: Meta, Telegram and the renderer are faked."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

TOPIC_ID = "landlines"                      # a real topic ranked highest first (unit "{:.1f}"), so get_topic works
YEARS = list(range(2000, 2011))
SERIES = {
    "IND": list(zip(YEARS, [1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 30])),     # overtakes France in 2007
    "USA": list(zip(YEARS, [20, 20, 20, 19, 19, 19, 19, 18, 18, 18, 18])),
    "FRA": list(zip(YEARS, [10, 13, 16, 19, 22, 25, 20, 15, 10, 7, 5])),    # ends last: 5.0 in 2010
    "JPN": list(zip(YEARS, [15, 15, 14, 14, 13, 13, 13, 12, 12, 12, 12])),
}
NAMES = {"IND": "India", "USA": "United States", "FRA": "France", "JPN": "Japan"}
# every check is true here: Norway leads, falls the most and ends last; Finland takes the lead in 2002
RACE_YEARS = [2000, 2001, 2002, 2003, 2004]
RACE = {"NOR": [50, 45, 30, 20, 10], "SWE": [40, 38, 35, 32, 30], "FIN": [30, 40, 42, 44, 45],
        "DNK": [20, 25, 28, 33, 35], "ISL": [10, 15, 20, 22, 25]}
RACE_NAMES = {"NOR": "Norway", "SWE": "Sweden", "FIN": "Finland", "DNK": "Denmark", "ISL": "Iceland"}
RACE_ROWS = {i: list(zip(RACE_YEARS, v)) for i, v in RACE.items()}
# no loser question is true: France and Japan tie for last, nobody falls, the US always leads
NO_LOSER = {"USA": [(y, 40 + 0.1 * k) for k, y in enumerate(YEARS)], "IND": [(y, 1 + 2.9 * k) for k, y in enumerate(YEARS)],
            "FRA": [(y, 10 + 0.2 * k) for k, y in enumerate(YEARS)], "JPN": [(y, 11 + 0.1 * k) for k, y in enumerate(YEARS)]}
FLOW = ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]
FMT = lambda v: f"{v:.1f}"   # noqa: E731 - landlines' unit_format


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture
def env(temp_db, tmp_path, monkeypatch, pinned_config):
    from pipeline import accounts, actions, config, hooks
    # hooks may first have been imported during a dashboard test, which binds that test's get_config for good
    monkeypatch.setattr(hooks, "get_config", config.get_config)
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"))
    pinned_config["opening"] = {**pinned_config["opening"], "hook_template": "which_last", "hook_explore_pct": 0,
                                "recent_block": 3}
    monkeypatch.setattr(actions, "spawn", lambda *a: 0)
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram"])
    return temp_db


def P():
    """The configured phrasings, in order."""
    from pipeline import hooks
    return [p["text"] for p in hooks.phrasings()]


def topic(lower_first=False):
    from pipeline.topics import get_topic
    t = dict(get_topic(TOPIC_ID))
    if lower_first:
        t["rank"] = "lower_first"
    return t


def views(series, names):
    from pipeline.render import CountryView
    return [CountryView(iso3=i, name=names[i], iso2=None, color="#22d3ee", years=[y for y, _ in s],
                        values=[v for _, v in s]) for i, s in series.items()]


def reel(db, question=None, stage="awaiting_approval", series=None):
    """A run with the files a rendered reel has (data, pick, labels, timeline, video, meta)."""
    from pipeline import hooks, timeline
    from pipeline.config import run_dir
    series = series or SERIES
    years = sorted({y for s in series.values() for y, _ in s})
    rid = db.create_run(TOPIC_ID)
    for st in FLOW[:FLOW.index(stage) + 1] if stage in FLOW else FLOW:
        db.transition(rid, st)
    d = run_dir(rid)
    isos = list(series)
    (d / "data.json").write_text(json.dumps({"topic_id": TOPIC_ID, "series": {k: [list(r) for r in v] for k, v in series.items()}}))
    (d / "pick.json").write_text(json.dumps({"countries": isos, "x_start": years[0], "x_end": years[-1],
                                             "scores": {i: {"score": 50} for i in isos}}))
    (d / "labels.json").write_text("{}")
    tl = timeline.build([(i, [y for y, _ in series[i]], [v for _, v in series[i]]) for i in isos], years[0], years[-1])
    tl.save(d / "timeline.json")
    (d / "audio.wav").write_bytes(b"RIFF-old-audio")
    (d / "video.mp4").write_bytes(b"old video")
    (d / "thumb.jpg").write_bytes(b"old cover")
    ids = {"Who overtakes who?": "overtakes"}
    opening = {"id": ids.get(question, "which_last" if hooks.classify(question) == "which_last" else "other"),
               "question": question, "answer": "a"} if question else None
    (d / "meta.json").write_text(json.dumps({"title": "t", "duration_seconds": 22.0, "opening": opening}))
    return rid


def publish(db, rid):
    db.transition(rid, "approved")
    db.transition(rid, "publishing")
    db.upsert_post(rid, "instagram", "live", remote_id="m-" + rid)
    db.transition(rid, "published")


# ------------------------------------------------------------------ the truth checks, from the data

def test_every_check_is_computed_from_the_data():
    from pipeline import hooks
    t = hooks.truths(topic(), RACE_ROWS, RACE_NAMES, FMT)
    assert all(r["ok"] for r in t.values()), {k: r["why"] for k, r in t.items() if not r["ok"]}
    assert t["ends_last"]["fields"] == {"country": "Norway", "value": "10.0", "year": 2004}
    assert t["falls_most"]["fields"] == {"country": "Norway", "from": "50.0", "to": "10.0", "from_year": 2000,
                                         "to_year": 2004}
    assert t["drops_to_last"]["fields"] == {"country": "Norway", "rank_from": "1st", "from_year": 2000, "value": "10.0",
                                            "year": 2004}
    assert t["loses_lead"]["fields"] == {"country": "Norway", "country2": "Finland", "year": 2002}
    assert t["loses_lead"]["highlight"] == ["Norway", "Finland"]
    assert t["overtaken_most"]["fields"] == {"country": "Norway", "n": 4}
    assert t["loses_ground"]["fields"] == {"country": "Norway", "rank_from": "1st", "rank_to": "5th", "from_year": 2000,
                                           "year": 2004}
    assert all(r["fact"] for r in t.values())


def test_a_check_is_false_when_the_data_does_not_say_it():
    from pipeline import hooks
    t = hooks.truths(topic(), SERIES, NAMES, FMT)
    assert t["ends_last"]["ok"] and t["falls_most"]["ok"] and t["drops_to_last"]["ok"]
    assert t["falls_most"]["fields"]["country"] == "France" and t["drops_to_last"]["fields"]["rank_from"] == "3rd"
    assert "changes hands" in t["loses_lead"]["why"]            # the US, then France, then India: no single loser
    assert not t["overtaken_most"]["ok"] and not t["loses_ground"]["ok"]   # nobody is passed by 2 or drops 2 places
    already = {**RACE_ROWS, "NOR": list(zip(RACE_YEARS, [5, 6, 7, 8, 9]))}
    t = hooks.truths(topic(), already, RACE_NAMES, FMT)
    assert t["ends_last"]["ok"] and "already last in 2000" in t["drops_to_last"]["why"]
    on_screen_tie = {**SERIES, "JPN": SERIES["JPN"][:-1] + [(2010, 5.04)]}   # 5.0 and 5.0 on screen
    assert hooks.truths(topic(), on_screen_tie, NAMES, FMT)["ends_last"]["why"].startswith("tie")
    early = {**SERIES, "FRA": SERIES["FRA"][:-1]}                         # France's data stops in 2009
    assert "stops in 2009" in hooks.truths(topic(), early, NAMES, FMT)["ends_last"]["why"]
    t = hooks.truths(topic(), NO_LOSER, NAMES, FMT)
    assert not any(r["ok"] for r in t.values()), {k: r for k, r in t.items() if r["ok"]}
    assert t["falls_most"]["why"].startswith("no country falls")


def test_ends_last_is_checked_against_the_rendered_data():
    from pipeline.captions import check_ends_last

    def check(series, country="FRA"):
        return check_ends_last({"iso3": country, "country": NAMES[country]}, series, FMT)

    assert check(SERIES) is None
    assert check(SERIES, "JPN").startswith("false")                                       # France is lower
    assert check({**SERIES, "JPN": SERIES["JPN"][:-1] + [(2010, 5.0)]}).startswith("tie")
    assert check({k: SERIES[k] for k in ("IND", "FRA")}).startswith("only 2 countries")
    assert check({**SERIES, "USA": SERIES["USA"][:-1]}) is None                          # another country ends early


def test_last_follows_the_metric_direction():
    """rank: lower_first (a lower number is better): last is the HIGHEST final value, the lead is the lowest, the
    up/down phrasings are left out, and the end card ranks the lowest value first."""
    from pipeline import hooks, render
    t = hooks.truths(topic(lower_first=True), RACE_ROWS, RACE_NAMES, FMT)
    assert t["ends_last"]["fields"] == {"country": "Finland", "value": "45.0", "year": 2004}
    assert t["loses_lead"]["fields"] == {"country": "Iceland", "country2": "Norway", "year": 2003}
    o, why_not = hooks.which_last_opening(topic(lower_first=True), RACE_ROWS, RACE_NAMES, FMT, [])
    assert (o["question"], o["answer"]) == ("Which country ends up last?", "Finland ends up last: 45.0 in 2004")
    spatial = ("Which country falls the most?", "Which country sinks to last?")
    assert all("up/down words" in why_not[q] for q in spatial) and not set(spatial) & set(o["eligible"])
    v = views(RACE_ROWS, RACE_NAMES)
    assert [v[i].name for i in render.end_order(topic(), v)] == ["Finland", "Denmark", "Sweden", "Iceland", "Norway"]
    assert [v[i].name for i in render.end_order(topic(lower_first=True), v)] == \
        ["Norway", "Iceland", "Sweden", "Denmark", "Finland"]
    from pipeline.topics import load_topics
    assert {t["id"] for t in load_topics() if t.get("rank") == "lower_first"} == \
        {"child_mortality", "homicide_rate", "unemployment"}


# ------------------------------------------------------------------ rotation

def test_only_which_country_questions_are_used():
    """Every breakout opened with a "Which country...?" question: the configured and shipped lists hold nothing else,
    and a reel that showed a retired "Who...?" wording is still which_last."""
    from pipeline import hooks
    for texts in (P(), [x["text"] for x in hooks.DEFAULT_PHRASINGS]):
        assert texts == ["Which country ends up last?", "Which country falls the most?",
                         "Which country loses the lead?", "Which country finishes last?", "Which country sinks to last?"]
    assert all(hooks.classify(q) == "which_last" for q in hooks.RETIRED_PHRASINGS)
    assert not any(q.startswith("Which country") for q in hooks.RETIRED_PHRASINGS)


def test_rotation_never_reuses_the_last_3_and_picks_the_least_used(env):
    from pipeline import hooks
    p = P()
    assert len(p) == 5 and p[0] == "Which country ends up last?"

    def pick(history):
        return hooks.which_last_opening(topic(), RACE_ROWS, RACE_NAMES, FMT, history)[0]["question"]

    assert pick([]) == p[0]
    assert pick([p[0]] * 3) == p[1]
    assert pick([p[0], p[1], p[2]]) == p[3]
    assert pick([p[1], p[2], p[3], p[1], p[2], p[3], p[0], None, None]) == p[4]   # p[1..3] used twice: the least used
    history = []
    for _ in range(12):
        q = pick(history)
        assert q not in history[-3:]
        history.append(q)
    assert set(history) == set(p)


def test_the_fallback_is_ends_up_last_when_every_true_phrasing_was_just_used(env, pinned_config):
    from pipeline import hooks
    rising = {"NOR": list(zip(RACE_YEARS, [1, 2, 3, 4, 5])), "SWE": list(zip(RACE_YEARS, [10, 11, 12, 13, 14])),
              "FIN": list(zip(RACE_YEARS, [20, 21, 22, 23, 24]))}         # only "last" is true: Norway
    last = [x["text"] for x in hooks.phrasings() if x["check"] == "ends_last"]
    assert last == ["Which country ends up last?", "Which country finishes last?"]
    o, _ = hooks.which_last_opening(topic(), rising, RACE_NAMES, FMT, last[1:])
    assert o["question"] == last[0] and o["choice"].startswith("rotation") and o["eligible"] == last
    pinned_config["opening"]["recent_block"] = 2
    o, _ = hooks.which_last_opening(topic(), rising, RACE_NAMES, FMT, [last[0], last[1]])
    assert o["question"] == "Which country ends up last?" and o["choice"].startswith("fallback")


def test_a_phrasing_with_a_bad_answer_or_check_is_left_out(env, pinned_config):
    from pipeline import hooks
    pinned_config["opening"]["phrasings"] = [
        {"text": "Which country ends up last?", "check": "ends_last", "answer": "{country} ends up last: {value} in {year}"},
        {"text": "Who melts down?", "check": "melts", "answer": "{country}"},
        {"text": "Which country loses the lead?", "check": "loses_lead", "answer": "{country} loses it to {nobody}"}]
    o, why_not = hooks.which_last_opening(topic(), RACE_ROWS, RACE_NAMES, FMT, [])
    assert o["eligible"] == ["Which country ends up last?"]
    assert "unknown check" in why_not["Who melts down?"] and "nobody" in why_not["Which country loses the lead?"]
    assert hooks.classify("Who drops to the bottom?") == "which_last"   # the shipped wordings keep their template


# ------------------------------------------------------------------ the video: question, reveal, layout

def test_the_end_card_answers_the_question_that_was_asked(env):
    """Each phrasing, reached through the rotation, opens the video and its own answer closes it."""
    from pipeline import render
    p = P()
    want = {"Which country ends up last?": "Norway ends up last: 10.0 in 2004",
            "Which country finishes last?": "Norway finishes last: 10.0 in 2004",
            "Which country falls the most?": "Norway falls the most: 50.0 to 10.0",
            "Which country sinks to last?": "Norway sinks from 1st in 2000 to last in 2004",
            "Which country loses the lead?": "Norway loses the lead to Finland in 2002"}
    assert set(want) == set(p)
    for k in range(len(p)):
        o = render.opening_for(topic(), views(RACE_ROWS, RACE_NAMES), 2000, 2004, history=p[:k])
        assert (o["question"], o["answer"]) == (p[k], want[p[k]])
        assert o["hook_template"] == "which_last" and o["hook_phrase"] == p[k] and "fallback" not in o
        assert o["truth"] and "Norway" in o["highlight"]


def test_with_no_true_phrasing_the_video_opens_with_another_question(env, pinned_config):
    from pipeline import render
    o = render.opening_for(topic(), views(NO_LOSER, NAMES), 2000, 2010, seed=0, history=[])
    assert (o["question"], o["hook_template"], o["hook_phrase"]) == ("Who overtakes who?", "who_overtakes", None)
    assert o["fallback"].startswith("no which_last phrasing")
    pinned_config["opening"]["hook_explore_pct"] = 100
    o = render.opening_for(topic(), views(RACE_ROWS, RACE_NAMES), 2000, 2004, seed=0, history=[])
    assert o["hook_template"] != "which_last" and o["fallback"].startswith("exploring")
    pinned_config["opening"].update(hook_template="auto", hook_explore_pct=0)
    assert {render.opening_for(topic(), views(SERIES, NAMES), 2000, 2010, seed=s)["id"] for s in range(4)} == \
        {"overtakes", "ends_last"}                           # auto: the old choice between the top two


def test_every_phrasing_is_drawn_like_the_first_so_the_chart_never_moves():
    from pipeline import hooks, render
    ref = render.hook_layout(P()[0])
    assert ref == (88, 2, 54, 1)
    fields = {"country": "United Kingdom", "country2": "Saudi Arabia", "value": "$12,345", "year": 2025,
              "from": "$12,345", "to": "$1,234", "from_year": 1990, "to_year": 2025, "rank_from": "1st",
              "rank_to": "10th", "n": 9}
    for p in hooks.phrasings() + [{**x, "spatial": False} for x in hooks.DEFAULT_PHRASINGS]:
        assert render.hook_layout(p["text"]) == ref, p["text"]
        assert render.phrasing_fits(p["text"], p["answer"].format(**fields)) is None, p["text"]
    assert "the chart would move" in render.phrasing_fits("Who slips to last place?", "x")   # 1 line at 72 px


def test_every_subtitle_is_one_line_so_the_chart_sits_where_a_one_line_reel_does():
    from pipeline import render
    from pipeline.topics import load_topics
    cv = render._measure_canvas()
    for t in load_topics():
        size, lines = render.header_fit(cv, t["subtitle"])
        assert len(lines) == 1 and size >= 44, (t["id"], size, lines)
    hook = render.hook_layout(P()[0])
    one = render.compute_layout(*hook, 52, 1)
    two = render.compute_layout(*hook, 48, 2)                  # what a wrapped subtitle did
    assert two["chart_t"] - one["chart_t"] >= 50
    for tid in ("co2_per_capita", "oil_per_capita", "net_migration", "gdp_per_capita"):   # shortened, not shrunk
        size, lines = render.header_fit(cv, next(t for t in load_topics() if t["id"] == tid)["subtitle"])
        assert (size, len(lines)) == (52, 1)
        assert render.compute_layout(*hook, size, 1)["chart_t"] == one["chart_t"]


# ------------------------------------------------------------------ the caption follows the screen

def test_caption_line_1_is_the_question_on_screen(env):
    """Line 1 is the question on screen + "Guess before the end" or "Don't scroll before the end"; option 1
    alternates between the two from one reel to the next."""
    from pipeline import captions
    asked, firsts = [], []
    cta = {"wl_guess": "Guess before the end", "wl_dont_scroll": "Don't scroll before the end"}
    for q in ("Which country sinks to last?", "Which country loses the lead?", "Which country ends up last?"):
        rid = reel(env, q)
        c = captions.generate_for_run(rid, ask=lambda prompt: asked.append(prompt))
        assert c["opener"] == "which_last" and {v["template_id"] for v in c["variants"]} == set(cta)
        assert len(c["variants"]) == 2 and not any("variants could be made" in n for n in c["notes"])
        for v in c["variants"]:
            hook, _, question = v["caption"].split("\n")
            assert hook == f"{q} {cta[v['template_id']]}" and question.endswith("?") and "France" not in hook + question
        firsts.append(c["variants"][c["chosen"]]["template_id"])
    assert firsts[0] != firsts[1] != firsts[2] and firsts[0] == firsts[2]
    assert not asked                                            # the wording is fixed: Gemini is not asked
    over = captions.generate_for_run(reel(env, "Who overtakes who?"), llm=False)    # other openers: the general hooks
    assert not any(v["template_id"].startswith("wl_") for v in over["variants"])


# ------------------------------------------------------------------ tracking: hook_template + hook_phrase

def test_hook_backfill_maps_every_reel_by_its_on_screen_question(env):
    from pipeline import hooks
    db = env
    last = reel(db, "Which country ends up last?")
    over = reel(db, "Who overtakes who?")
    falls = reel(db, "Which country falls the most?")
    old = reel(db, None)                                 # the reels from before 13 Oct had no on-screen hook
    db.transition(last, "approved")
    db.transition(last, "publishing")
    db.upsert_post(last, "instagram", "live", remote_id="m1")   # posted before the fields existed
    rows = {r["run_id"]: r for r in hooks.backfill()}
    assert {k: v["hook_template"] for k, v in rows.items()} == {
        last: "which_last", over: "who_overtakes", falls: "which_last", old: "other"}
    assert {k: v["hook_phrase"] for k, v in rows.items()} == {
        last: "Which country ends up last?", over: None, falls: "Which country falls the most?", old: None}
    assert all(r["changed"] for r in rows.values())
    post = db.get_posts(last)[0]
    assert (post["hook_template"], post["hook_phrase"]) == ("which_last", "Which country ends up last?")
    assert db.get_run(over)["hook_template"] == "who_overtakes" and db.get_run(over)["hook_phrase"] is None
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
    assert {"hook_template", "hook_phrase"} <= cols and {"hook_template", "hook_phrase", "prior_post_pos"} <= runs


def test_hook_phrase_is_copied_to_posts_and_shown_next_to_the_template(env):
    from pipeline import home
    db = env
    rid = reel(db, "Who drops to the bottom?")
    db.set_hook_template(rid, "which_last", "Who drops to the bottom?")
    publish(db, rid)
    post = db.get_posts(rid)[0]
    assert (post["hook_template"], post["hook_phrase"]) == ("which_last", "Who drops to the bottom?")
    db.add_stats(post["id"], views=321)
    row = next(s for s in db.latest_stats() if s["run_id"] == rid)
    assert (row["hook_template"], row["hook_phrase"]) == ("which_last", "Who drops to the bottom?")
    text = home.stats_text()
    assert "❓ which_last: “Who drops to the bottom?”" in text and "321 views" in text


def test_the_history_is_posted_then_queued_then_waiting(env):
    from pipeline import actions, hooks
    db = env
    legacy = reel(db, "Which country ends up last?")
    db.set_hook_template(legacy, "which_last")         # posted before hook_phrase existed: read from its video
    publish(db, legacy)
    posted = reel(db, "Which country finishes last?")
    db.set_hook_template(posted, "which_last", "Which country finishes last?")
    publish(db, posted)
    queued = reel(db, "Who drops to the bottom?")
    db.set_hook_template(queued, "which_last", "Who drops to the bottom?")
    actions.approve(queued, "telegram")
    waiting = reel(db, "Which country loses the lead?")
    db.set_hook_template(waiting, "which_last", "Which country loses the lead?")
    other = reel(db, "Who overtakes who?")
    db.set_hook_template(other, "who_overtakes")
    assert hooks.phrase_history() == ["Which country ends up last?", "Which country finishes last?",
                                      "Who drops to the bottom?", "Which country loses the lead?", None]
    assert hooks.phrase_history(exclude=queued) == ["Which country ends up last?", "Which country finishes last?",
                                                    "Which country loses the lead?", None]


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
    kept = reel(db, "Who overtakes who?", series=NO_LOSER)
    posted = reel(db, "Who overtakes who?")
    db.transition(posted, "approved")
    db.transition(posted, "publishing")
    db.upsert_post(posted, "instagram", "live", remote_id="m9")
    db.transition(posted, "published")
    places = {r: db.get_run(r)["post_pos"] for r in (first, second)}

    res = {r["run_id"]: r for r in rebrand.rebrand_all(send=False)}
    assert posted not in res                                                    # never touched
    assert (run_dir(posted) / "video.mp4").read_bytes() == b"old video"
    assert res[kept]["action"] == "kept" and res[kept]["why"].startswith("no which_last phrasing")
    assert (run_dir(kept) / "video.mp4").read_bytes() == b"old video" and not (run_dir(kept) / "video_oldhook.mp4").exists()
    p = P()
    # each rotates against the reels before it: "loses the lead" (p[2]) is not true of SERIES, so the third is p[3]
    for rid, phrase in ((first, p[0]), (second, p[1]), (waiting, p[3])):
        d = run_dir(rid)
        assert res[rid]["action"] == "rebranded"
        assert (d / "video_oldhook.mp4").read_bytes() == b"old video" and (d / "thumb_oldhook.jpg").read_bytes() == b"old cover"
        assert (d / "video.mp4").read_bytes() == b"new video old video"         # audio stream copied from the old file
        meta = json.loads((d / "meta.json").read_text())
        assert meta["opening"]["question"] == phrase and meta["hook_template"] == "which_last"
        assert meta["hook_phrase"] == phrase and res[rid]["truth"]
        assert json.loads((d / "meta_oldhook.json").read_text())["opening"]["question"] in (
            "Who overtakes who?", "Which country ends up last?")
        run = db.get_run(rid)
        assert run["stage"] == "awaiting_approval" and (run["hook_template"], run["hook_phrase"]) == ("which_last", phrase)
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
