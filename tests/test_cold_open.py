"""Cold open (pipeline/coldopen.py): the race's most dramatic beat first, then a rewind cut to the start of the
race. One A/B variable: the race itself is untouched (the cold open is added on top), the hook keeps its timing,
the beat never shows the country that finishes last, and the cover is the same moment of the race."""
import json
import math

import numpy as np
import pytest

from pipeline import audio, coldopen
from pipeline import render as R
from pipeline.config import countries, get_config
from pipeline.timeline import build

YEARS = list(range(1990, 2024))
T = np.arange(len(YEARS))
NAMES = ("GBR", "SAU", "ZAF", "USA")   # 4 countries: about 20 s, so the 1.7 s cold open stays under 25 s
TOPIC = {"id": "t", "title": "What your carbon footprint sounds like",
         "subtitle": "CO2 from fossil fuels & industry, tonnes per person", "unit_format": "{:,.0f}",
         "source": "owid", "start_year": 1990}
OPENING = {"id": "which_last", "question": "Which country ends up last?", "answer": "United Kingdom ends up last: 1,030 in 2023",
           "highlight": ["United Kingdom"], "hook_template": "which_last"}
WANT = {"want": True, "why": "", "counts": False}
LEAD = 1.7


def _series(crash_at=12, crash_country=2, lift=2000, names=NAMES):
    """Gentle waves (no year moves 25% of its range); one country drops 1,500 for good at `crash_at`. `lift` raises
    that country first, so by default it does NOT finish last (the United Kingdom does, at 1,030); lift=0 makes the
    crash country the last one."""
    rows = []
    for i, c in enumerate(names):
        vals = [float(v) for v in 1000 + 300 * np.sin(T / (3 + i)) + 10 * T]
        if i == crash_country and crash_at is not None:
            vals = [v + lift - (1500 if k >= crash_at else 0) for k, v in enumerate(vals)]
        rows.append((c, YEARS, vals))
    return rows


def _drop(rows, i, at=10, by=900):
    """rows with country i dropping `by` for good from year index `at` (a second, smaller crash)."""
    rows = list(rows)
    rows[i] = (rows[i][0], YEARS, [v - (by if k >= at else 0) for k, v in enumerate(rows[i][2])])
    return rows


def _views(rows):
    pal = get_config()["render"]["palette"]
    by = {c["iso3"]: c for c in countries()}
    return [R.CountryView(iso3=c, name=by[c]["name"], iso2=by[c]["iso2"], color=pal[i], years=list(ys), values=list(vs))
            for i, (c, ys, vs) in enumerate(rows)]


def _cold(rows, exclude=None):
    s = coldopen.settings()
    tl = build(rows, 1990, 2023, cold_open=s["seconds"], snap=s["snap_seconds"])
    views = _views(rows)
    beat, why = coldopen.pick_beat(TOPIC, views, tl, exclude)
    if beat:
        tl.cold_open_from = beat["window"][0]
    return tl, views, beat, why


def _timeline_for(rows, opening=None, decision=WANT):
    data = {"series": {c: [[y, v] for y, v in zip(ys, vs)] for c, ys, vs in rows}}
    return coldopen.timeline_for(TOPIC, {"x_start": 1990, "x_end": 2023}, data, {}, rows, decision, opening)


@pytest.fixture
def env(temp_db, tmp_path, pinned_config):
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"))
    return temp_db


# ------------------------------------------------------------------ config and timing

def test_the_shipped_settings(pinned_config):
    assert coldopen.settings() == {"mode": "alternate", "seconds": 1.5, "snap_seconds": 0.2,
                                   "apply_to_existing_queue": False}


def test_settings_are_whole_frames_and_the_rewind_stays_under_0_3_s(pinned_config):
    pinned_config["cold_open"].update(seconds=1.51, snap_seconds=0.5, mode="sometimes")
    s = coldopen.settings()
    assert s["seconds"] == 1.5 and 0 < s["snap_seconds"] < 0.3
    assert s["snap_seconds"] * 30 == pytest.approx(round(s["snap_seconds"] * 30))
    assert s["mode"] == "never"   # an unknown mode never makes a cold open
    del pinned_config["cold_open"]
    assert coldopen.settings()["mode"] == "never"


def test_the_cold_open_is_added_on_top_of_an_untouched_race():
    rows = _series()
    plain = build(rows, 1990, 2023)
    tl = build(rows, 1990, 2023, cold_open=1.5, snap=0.2)
    assert tl.lead == pytest.approx(LEAD)
    assert tl.duration == pytest.approx(plain.duration + LEAD, abs=1e-5) and tl.n_frames == plain.n_frames + 51
    assert tl.end_start == pytest.approx(plain.end_start + LEAD, abs=1e-5) and tl.per_country == plain.per_country
    for a, b in zip(tl.slots, plain.slots):   # every time of the race, LEAD later: same lengths, same notes
        for k in ("start", "end", "trans_end", "draw_start", "draw_end"):
            assert getattr(a, k) == pytest.approx(getattr(b, k) + LEAD, abs=1e-5)
        assert a.onsets == pytest.approx([o + LEAD for o in b.onsets], abs=1e-5)
        assert a.knots == pytest.approx([k + LEAD for k in b.knots], abs=1e-5)
        assert [(e["year"], e["t_to"]) for e in a.events] == \
            pytest.approx([(e["year"], e["t_to"] + LEAD) for e in b.events], abs=1e-5)
    assert plain.cold_open_time(0.0) is None and tl.cold_open_time(LEAD) is None


def test_the_race_is_fitted_so_the_cold_open_lands_within_25_s():
    """5 countries are about 23.7 s at the default timing: the race is fitted to 23.3 s (699 frames), and the
    reel with a cold open has that very race, 1.7 s later, at 25.0 s (750 frames)."""
    rows = _series(names=NAMES + ("KOR",))
    plain = build(rows, 1990, 2023)
    assert plain.fit and plain.fit["before"] > 23.3 and plain.duration <= 23.3 and plain.n_frames == 699
    tl, _, state = _timeline_for(rows)
    assert state["cold_open"] is True and tl.duration <= 25.0 and tl.n_frames == 750
    for a, b in zip(tl.slots, plain.slots):
        assert a.onsets == pytest.approx([o + LEAD for o in b.onsets], abs=1e-5)


def test_a_reel_that_would_pass_25_s_is_made_without_one(pinned_config):
    """The safety net, with the fit off: a race that is still too long is made without a cold open."""
    pinned_config["timing"]["fit"] = None
    pinned_config["video"]["max_seconds"] = 25
    rows = _series(names=NAMES + ("KOR",))   # 5 countries: about 23.7 s, so 25.4 s with the cold open
    plain = build(rows, 1990, 2023)
    assert plain.duration + LEAD > 25 >= plain.duration
    tl, _, state = _timeline_for(rows)
    assert state["cold_open"] is False and tl.cold_open == 0 and tl.duration == pytest.approx(plain.duration)
    assert state["cold_open_beat"].startswith("skipped: would exceed 25s (")
    assert coldopen.summary(False, state["cold_open_beat"]) == "no · skipped: would exceed 25s"


def test_the_clip_then_the_rewind_back_to_the_start_of_its_line():
    tl, _, beat, _ = _cold(_series())
    start, end = beat["window"]
    assert tl.cold_open_time(0.0) == pytest.approx(start)
    assert tl.cold_open_time(44 / 30) == pytest.approx(start + 44 / 30)
    rewind = [tl.cold_open_time((45 + k) / 30) for k in range(6)]
    assert all(a > b for a, b in zip(rewind, rewind[1:])) and rewind[0] < end   # backwards, never past the clip
    assert rewind[-1] == pytest.approx(tl.slot_at(start).draw_start)
    assert tl.cold_open_time(51 / 30) is None


def test_the_cover_is_the_same_moment_of_the_race():
    rows = _series()
    plain = build(rows, 1990, 2023)
    tl, views, _, _ = _cold(rows)
    off, on = R.cover_seconds(plain), R.cover_seconds(tl)
    assert off == 2.35                                         # a reel without one: unchanged
    assert on == pytest.approx(off + tl.lead) and tl.cold_open_time(on) is None
    a, _, _, L = _drawn(plain, views, off)
    b, _, _, _ = _drawn(tl, views, on)
    race = slice(int(L["dots_band"][0]), int(math.ceil(L["value_band"][1])))   # dots, country, chart, year, value
    assert np.abs(a[race].astype(int) - b[race]).max() <= 3


# ------------------------------------------------------------------ the beat

def test_the_beat_is_the_most_dramatic_move_and_the_clip_shows_no_final_value():
    from pipeline.captions import ranked_moves
    tl, views, beat, _ = _cold(_series())
    first = ranked_moves({v.iso3: list(zip(v.years, v.values)) for v in views})[0]
    assert (beat["iso3"], beat["from_year"], beat["to_year"], beat["rank"]) == \
        (first["iso3"], first["from_year"], first["to_year"], 1) == ("ZAF", 2001, 2002, 1)
    assert beat["kind"] == "fall" and beat["text"].startswith("South Africa crashes ")
    assert 0 < beat["lands_at"] <= tl.cold_open - coldopen.LAND_ROOM
    chk = coldopen.window_check(tl)
    assert chk["ok"] and chk["frames"] == 51
    assert chk["latest_race_time"] < chk["clip_country_final_step_at"] <= chk["final_ranking_settled_at"] \
        < chk["end_card_at"] < chk["last_frame_at"]


def test_ranked_moves_is_still_the_sharpest_fact():
    from pipeline.captions import compute_facts
    rows = _series()
    facts = {f["kind"]: f for f in compute_facts(TOPIC, [(c, c) for c, _, _ in rows],
                                                 {c: list(zip(y, v)) for c, y, v in rows}, 1990, 2023)}
    assert (facts["sharpest"]["iso3"], facts["sharpest"]["from_year"], facts["sharpest"]["year"]) == ("ZAF", 2001, 2002)


def test_a_beat_too_close_to_the_end_gives_way_to_the_next_one():
    rows = _drop(_series(crash_at=32, crash_country=1), 3)   # the biggest crash lands one year before its line ends
    tl, _, beat, _ = _cold(rows)
    assert beat["rank"] > 1 and beat["iso3"] == "USA"
    assert any("Saudi Arabia" in p and "too close to the end" in p for p in beat["passed_over"])
    assert coldopen.window_check(tl)["ok"]


def test_the_country_that_finishes_last_is_never_the_beat():
    rows = _drop(_series(lift=0), 1)          # South Africa's crash (the biggest) leaves it last; Saudi Arabia drops too
    views = _views(rows)
    assert coldopen.spoilers(TOPIC, views) == {"ZAF": "finishes last"}
    lower_first = {**TOPIC, "rank": "lower_first"}   # last = the highest final value
    assert coldopen.spoilers(lower_first, views) == {"USA": "finishes last"}   # 1,118: the highest final value
    falls = {"highlight": ["Saudi Arabia"]}   # a question whose answer names another country: that one too
    assert coldopen.spoilers(TOPIC, views, falls) == {"ZAF": "finishes last",
                                                      "SAU": "is in the answer to the opening question"}
    _, _, beat, _ = _cold(rows, coldopen.spoilers(TOPIC, views))
    assert beat["iso3"] != "ZAF" and beat["excluded"] == {"South Africa": "finishes last"}
    assert beat["passed_over"][0] == "#1 South Africa 2001-2002: South Africa finishes last"


def test_the_next_country_takes_over_when_the_loser_has_the_top_beat():
    rows = _drop(_series(lift=0), 1)
    tl, _, state = _timeline_for(rows, OPENING)
    beat = state["beat"]
    assert state["cold_open"] is True and (beat["iso3"], beat["rank"]) == ("SAU", 2)
    assert state["cold_open_beat"].startswith("Saudi Arabia crashes ") and "move #2 of" in state["cold_open_beat"]
    assert tl.cold_open_from == beat["window"][0] and coldopen.window_check(tl)["ok"]


def test_no_cold_open_when_only_the_loser_has_a_beat():
    rows = _series(lift=0)                    # South Africa has the only crash, and finishes last
    tl, _, state = _timeline_for(rows, OPENING)
    assert state["cold_open"] is False and state["beat"] is None and tl.cold_open == 0
    assert state["cold_open_beat"] == ("no beat fits: #1 South Africa 2001-2002 and its other moves: South Africa "
                                       "finishes last")   # no other move scores the sharpest fact's bar
    assert tl.duration == pytest.approx(build(rows, 1990, 2023).duration)


def test_no_beat_no_cold_open_and_the_reason_is_kept():
    rows = _series(crash_at=None)
    tl, _, state = _timeline_for(rows)
    assert state["cold_open"] is False and state["beat"] is None
    assert state["cold_open_beat"].startswith("no beat fits: #1 ") and "not an event on screen" in state["cold_open_beat"]
    assert tl.cold_open == 0 and tl.duration == pytest.approx(build(rows, 1990, 2023).duration)


# ------------------------------------------------------------------ picture and sound

def _drawn(tl, views, t):
    r = R.Renderer(TOPIC, tl, views, opening=OPENING)
    rgb = r.frame_array(t).copy()
    boxes = list(r.cv.text_boxes)
    return rgb, [b[0] for b in boxes if b[5] == "country"], [b[:5] for b in boxes if b[5] == "hook"], r.L


def test_frame_0_is_the_beat_in_motion_under_the_same_opening_question():
    rows = _series()
    tl, views, beat, _ = _cold(rows)
    f0, country, hook, L = _drawn(tl, views, 0.0)
    f1, _, _, _ = _drawn(tl, views, 1 / 30)
    assert country == ["SOUTH AFRICA"]                        # the clip's country, not the first one
    plain = build(rows, 1990, 2023)
    _, first, plain_hook, _ = _drawn(plain, views, 0.0)
    assert hook == plain_hook and first == ["UNITED KINGDOM"]  # the same question, same size, same place
    chart = slice(int(L["chart_t"]), int(L["chart_b"]))
    sat = lambda f: (f[chart].max(axis=2).astype(int) - f[chart].min(axis=2)) > 90  # noqa: E731
    assert sat(f0).sum() > 2000                                # a drawn line from frame 0: no fade in
    assert (np.abs(f0[chart].astype(int) - f1[chart]).max(axis=2) > 40).sum() > 50   # and it moves at once
    _, race, _, _ = _drawn(tl, views, tl.lead)
    assert race == ["UNITED KINGDOM"]                          # after the rewind the race starts from the top


def test_the_cold_open_sound_has_a_stab_at_frame_0_and_the_race_keeps_its_notes():
    rows = _series()
    tl, _, beat, _ = _cold(rows)
    plain = build(rows, 1990, 2023)
    values = {s.iso3: [dict(zip(r[1], r[2]))[y] for y in s.years] for s, r in zip(tl.slots, rows)}
    mix, stems, info = audio.synthesize(tl, values, seed=1)
    _, _, without = audio.synthesize(plain, values, seed=1)
    sr = info["sample_rate"]
    assert len(mix) == int(round(tl.n_frames / tl.fps * sr))
    first = mix[: int(0.05 * sr)]
    assert 20 * math.log10(float(np.sqrt(np.mean(first ** 2))) + 1e-12) > -30   # audible in the first 50 ms
    assert info["cold_open"]["source_from"] == pytest.approx(beat["window"][0])
    assert info["cold_open"]["slot"] == beat["slot"] == 2
    assert [(n["kind"], n["midi"]) for n in info["notes"]] == [(n["kind"], n["midi"]) for n in without["notes"]]
    assert [n["t"] for n in info["notes"]] == pytest.approx([n["t"] + LEAD for n in without["notes"]], abs=1e-5)
    assert info["pluck_minus_pad_lu"] >= 6


# ------------------------------------------------------------------ alternation and tagging

def test_alternate_mode_alternates_new_reels_by_what_they_really_got(env):
    db = env

    def new(beat_fits=True):
        rid = db.create_run("t")
        d = coldopen.decide(rid)
        on = d["want"] and beat_fits
        coldopen.record(rid, d, {"cold_open": on, "cold_open_beat": "a beat; race 1-2 s" if on else
                                 d["why"] or "no beat fits: x"})
        return d["want"], db.get_run(rid)["cold_open"]

    assert new() == (True, 1)                     # the first new reel gets one
    assert new() == (False, 0)                    # the next is the control
    assert new(beat_fits=False) == (True, 0)      # wanted one, no beat fits: counts as without ...
    assert new() == (True, 1)                     # ... so the next one tries again
    st = coldopen.alternate_state()
    assert (st["renders"], st["with"], st["last"]) == (4, 2, True)
    assert json.loads(db.kv_get(coldopen.K_ALTERNATE))["renders"] == 4   # kept in the database: survives restarts
    made = db.list_runs()[0]["id"]
    assert coldopen.decide(made) == {"want": True, "why": "a beat; race 1-2 s", "counts": False}   # a re-render
    assert coldopen.decide("test-20261021-120000")["counts"] is False   # a TEST video has no run row


def test_never_and_always(env, pinned_config):
    db = env
    pinned_config["cold_open"]["mode"] = "never"
    assert coldopen.decide(db.create_run("t")) == {"want": False, "why": "cold_open.mode is never", "counts": False}
    pinned_config["cold_open"]["mode"] = "always"
    coldopen.record(db.create_run("t"), {"counts": True}, {"cold_open": True, "cold_open_beat": "b"})
    assert coldopen.decide(db.create_run("t"))["want"] is True


def test_reels_made_before_are_without_and_posts_carry_the_cold_open(env):
    from pipeline.config import run_dir
    db = env
    old = db.create_run("t")
    (run_dir(old) / "meta.json").write_text(json.dumps({"title": "x"}))
    db.create_run("t")                                         # no video yet: left alone
    assert coldopen.backfill() == 1 and coldopen.backfill() == 0
    assert (db.get_run(old)["cold_open"], db.get_run(old)["cold_open_beat"]) == (0, coldopen.BEFORE)
    assert coldopen.decide(old)["want"] is False               # never re-rendered with one by accident
    new = db.create_run("t")
    db.set_cold_open(new, True, "South Africa crashes 1,016 to -483 (2001-2002), move #1 of 9; race 6.10-7.60 s")
    db.upsert_post(new, "instagram", "live", remote_id="m1")
    post = db.get_posts(new)[0]
    assert post["cold_open"] == 1 and post["cold_open_beat"].startswith("South Africa crashes")


def test_stats_keep_skip_rate_and_total_watch_time_with_the_cold_open(env):
    db = env
    rid = db.create_run("t")
    db.set_cold_open(rid, False, coldopen.BEFORE)
    db.upsert_post(rid, "instagram", "live", remote_id="m1")
    pid = db.get_posts(rid)[0]["id"]
    db.add_stats(pid, views=10, raw={"reels_skip_rate": 31.5, "ig_reels_video_view_total_time": 12000})  # an older pull
    row = next(s for s in db.latest_stats() if s["post_id"] == pid)
    assert (row["cold_open"], row["skip_rate"], row["total_watch_s"]) == (0, 31.5, 12.0)
    db.add_stats(pid, views=12, skip_rate=29.0, total_watch_s=20.0, raw={})
    row = next(s for s in db.latest_stats() if s["post_id"] == pid)
    assert (row["skip_rate"], row["total_watch_s"]) == (29.0, 20.0)


def test_groups_average_each_metric_per_side():
    rows = [{"cold_open": 1, "views": 100, "skip_rate": 30.0, "avg_watch_s": 10.0},
            {"cold_open": 1, "views": 300, "skip_rate": None, "avg_watch_s": 14.0},
            {"cold_open": 0, "views": 200, "skip_rate": 40.0, "avg_watch_s": 9.0},
            {"cold_open": None, "views": None, "skip_rate": 50.0}]   # no stats yet: left out
    g = coldopen.groups(rows)
    assert g["with"]["n"] == 2 and g["with"]["views"] == 200 and g["with"]["skip_rate"] == 30.0
    assert g["with"]["skip_rate_n"] == 1 and g["with"]["avg_watch_s"] == 12.0
    assert g["without"]["n"] == 1 and g["without"]["skip_rate"] == 40.0 and g["without"]["reach"] is None


def test_the_baseline_is_the_last_reels_without_and_stays_the_same_reels(env):
    db = env
    posts = []
    for k in range(5):
        rid = db.create_run("t")
        db.set_cold_open(rid, k == 4, "beat" if k == 4 else coldopen.BEFORE)   # the newest has a cold open
        db.upsert_post(rid, "instagram", "live", remote_id=f"m{k}")
        pid = db.get_posts(rid)[0]["id"]
        db.add_stats(pid, views=100 + k, skip_rate=30.0 + k, raw={})
        posts.append(pid)
    base = coldopen.save_baseline(n=3)
    assert [b["post_id"] for b in base] == [posts[3], posts[2], posts[1]]
    assert [b["skip_rate"] for b in base] == [33.0, 32.0, 31.0]
    later = db.create_run("t")
    db.set_cold_open(later, False, "control side of the A/B: x")
    db.upsert_post(later, "instagram", "live", remote_id="m9")
    db.add_stats(posts[3], views=500, skip_rate=25.0, raw={})
    coldopen.refresh_baseline()
    base = coldopen.baseline()
    assert [b["post_id"] for b in base] == [posts[3], posts[2], posts[1]] and base[0]["skip_rate"] == 25.0


def test_the_review_card_says_it_in_one_line(env):
    from pipeline import review
    from pipeline.config import run_dir
    db = env
    rid = db.create_run("t")
    db.transition(rid, "awaiting_approval", reset=True)
    (run_dir(rid) / "meta.json").write_text(json.dumps({"opening": {"question": "Which country ends up last?"}}))

    def line():
        found = [ln for ln in review.card_text(db.get_run(rid)).splitlines() if "Cold open" in ln]
        assert len(found) == 1
        return found[0]

    assert line() == "🎞 Cold open: no · made before cold opens existed"
    db.set_cold_open(rid, True, "Mexico jumps 20.1 to 26.0 (2016-2017), move #1 of 9; race 3.80-5.30 s")
    assert line() == "🎞 Cold open: yes · Mexico jumps 20.1 to 26.0 (2016-2017), move #1 of 9"
    db.set_cold_open(rid, False, "control side of the A/B: the last new reel had a cold open")
    assert line() == "🎞 Cold open: no · control side of the A/B"
    db.set_cold_open(rid, False, "skipped: would exceed 25s (23.71 s + 1.70 s cold open and cut = 25.41 s)")
    assert line() == "🎞 Cold open: no · skipped: would exceed 25s"


def test_render_reel_tags_meta_and_run_and_alternates(env, monkeypatch):
    from pipeline import orchestrator
    from pipeline.config import run_dir
    from pipeline.topics import get_topic
    db = env
    topic = get_topic("gdp_per_capita")   # ranks the highest value first: the United Kingdom finishes last
    rows = _series()
    seen = []

    def fake_render_video(topic, tl, views, wav, out_mp4, thumb=None, cfg=None, on_progress=None, opening=None,
                          audio_from=None):
        seen.append(tl)
        out_mp4.write_bytes(b"v")
        thumb.write_bytes(b"c")
        return {"cover_seconds": R.cover_seconds(tl), "render_seconds": 0.1, "backend": "fake", "backend_note": "",
                "opening": {}}

    monkeypatch.setattr(R, "render_video", fake_render_video)
    monkeypatch.setattr(audio, "synthesize", lambda tl, values, seed=0: (
        np.zeros((10, 2), np.float32), {k: np.zeros((10, 2), np.float32) for k in ("pluck", "pad", "fx")},
        {"sample_rate": 48000, "lufs": -14, "true_peak_db": -2, "pluck_minus_pad_lu": 9}))
    ids = []
    for _ in range(2):
        rid = db.create_run("gdp_per_capita")
        d = run_dir(rid)
        (d / "data.json").write_text(json.dumps({"topic_id": "gdp_per_capita",
                                                 "series": {c: [[y, v] for y, v in zip(ys, vs)] for c, ys, vs in rows}}))
        (d / "pick.json").write_text(json.dumps({"countries": list(NAMES), "x_start": 1990, "x_end": 2023,
                                                 "scores": {c: {"score": 50} for c in NAMES}}))
        (d / "labels.json").write_text("{}")
        orchestrator.render_reel(rid, topic)
        ids.append(rid)
    metas = [json.loads((run_dir(r) / "meta.json").read_text()) for r in ids]
    assert [m["cold_open"] for m in metas] == [True, False]
    assert "South Africa" not in metas[0]["opening"]["highlight"]
    assert metas[0]["cold_open_detail"]["iso3"] == "ZAF" and metas[0]["cold_open_beat"].startswith("South Africa crashes")
    assert metas[0]["cold_open_detail"]["excluded"] == {"United Kingdom": "finishes last"}
    assert metas[1]["cold_open_beat"].startswith("control side")
    assert [db.get_run(r)["cold_open"] for r in ids] == [1, 0]
    assert seen[0].cold_open == 1.5 and seen[1].cold_open == 0
    assert seen[0].duration == pytest.approx(seen[1].duration + LEAD)   # added on top of the same race
    # every new reel carries the experiment marker; a re-render keeps the one it was made with (none = format 1)
    assert [m["format_version"] for m in metas] == [2, 2] and [db.get_run(r)["format_version"] for r in ids] == [2, 2]
    old = {k: v for k, v in metas[1].items() if k != "format_version"}
    (run_dir(ids[1]) / "meta.json").write_text(json.dumps(old))
    orchestrator.render_reel(ids[1], topic, opening=old["opening"])
    assert json.loads((run_dir(ids[1]) / "meta.json").read_text())["format_version"] == 1
    assert db.get_run(ids[1])["format_version"] == 1
    assert metas[0]["duration_seconds"] == pytest.approx(metas[1]["duration_seconds"] + LEAD)
    assert metas[0]["cover_seconds"] == pytest.approx(metas[1]["cover_seconds"] + LEAD)
    assert db.has_log(ids[0], "cold open: South Africa crashes") and db.has_log(ids[1], "no cold open: control side")
