import math

import pytest

from pipeline.config import get_config
from pipeline.timeline import Timeline, build, country_count_bounds, per_country_seconds, total_seconds

YEARS = list(range(1990, 2024))


def test_count_bounds_default():
    # 0.4 intro + 4.5 s/country + 3.5 end card within 30-45 s -> 6..9 countries
    assert country_count_bounds() == (6, 9)


def test_total_seconds_in_range_for_bounds():
    cfg = get_config()
    n_min, n_max = country_count_bounds()
    for n in range(n_min, n_max + 1):
        assert cfg["video"]["min_seconds"] <= total_seconds(n) <= cfg["video"]["max_seconds"]


def test_stretch_when_few_countries():
    # 5 countries at 4.5 s would be 26.4 s, so each is stretched to fill 30 s
    assert per_country_seconds(5) > 4.5
    assert math.isclose(total_seconds(5), get_config()["video"]["min_seconds"])


def test_layout_and_onsets():
    tl = build([("IND", YEARS), ("BRA", YEARS), ("USA", YEARS[3:])], 1990, 2023)
    cfg = get_config()["timing"]
    s0 = tl.slots[0]
    assert s0.start == pytest.approx(cfg["intro"])
    assert s0.trans_end - s0.start == pytest.approx(cfg["transition"])
    assert s0.end - s0.draw_end == pytest.approx(cfg["hold"])
    assert tl.slots[1].start == pytest.approx(s0.end)
    # first year at draw start, last year at draw end, evenly spaced
    assert s0.onsets[0] == pytest.approx(s0.draw_start)
    assert s0.onsets[-1] == pytest.approx(s0.draw_end)
    steps = [b - a for a, b in zip(s0.onsets, s0.onsets[1:])]
    assert max(steps) - min(steps) < 1e-5
    # a late-starting series keeps its year position on the shared axis
    s2 = tl.slots[2]
    assert s2.onsets[0] == pytest.approx(s2.onset_of(1993))
    assert s2.onsets[0] > s2.draw_start
    assert tl.duration == pytest.approx(tl.end_start + cfg["end_card"])
    assert tl.n_frames == math.ceil(tl.duration * tl.fps - 1e-9)


def test_year_pos_matches_onsets():
    tl = build([("IND", YEARS)], 1990, 2023)
    s = tl.slots[0]
    for y, o in zip(s.years, s.onsets):
        assert s.year_pos(o) == pytest.approx(y)
        assert s.reached_index(o) == s.years.index(y)
    assert s.year_pos(0) == 1990 and s.year_pos(1e9) == 2023
    assert s.reached_index(s.draw_start - 0.01) == -1


def test_roundtrip(tmp_path):
    tl = build([("IND", YEARS), ("BRA", YEARS)], 1990, 2023)
    p = tmp_path / "tl.json"
    tl.save(p)
    tl2 = Timeline.load(p)
    assert tl2.all_onsets() == tl.all_onsets()
    assert tl2.slot_at(tl.slots[1].start + 0.01).iso3 == "BRA"
    assert tl2.slot_at(tl.end_start + 0.1) is None
