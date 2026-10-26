import math

import pytest

from pipeline.config import get_config
from pipeline.timeline import Timeline, build, country_count_bounds, find_events, fits, per_country_seconds, total_seconds

YEARS = list(range(1990, 2024))


def test_count_bounds_default():
    # no intro + 4.2 s/country + 2.5 s end card within 20-23.3 s, with the dead time trimmed -> 5 countries
    assert country_count_bounds() == (5, 5)


def test_every_count_in_the_bounds_fits_after_the_fit():
    cfg = get_config()
    n_min, n_max = country_count_bounds()
    for n in range(n_min, n_max + 1):
        assert cfg["video"]["min_seconds"] <= total_seconds(n)              # the default pace, before any fit
        assert fits(build([(c, YEARS) for c in "ABCDE"[:n]], 1990, 2023))


def _eventful(n: int, cuts: int = 3) -> list[tuple]:
    """n countries whose series crash `cuts` times (each crash an event: slow-mo)."""
    out = []
    for i in range(n):
        vals = [50.0 + 0.1 * k for k in range(len(YEARS))]
        for c in range(cuts):
            k = 5 + c * 9 + i % 3
            vals = vals[:k] + [v - 30.0 * (c % 2 * 2 - 1) for v in vals[k:]]
        out.append((f"C{i}", YEARS, vals))
    return out


def test_the_fit_trims_dead_time_first_then_slow_mo_and_never_cuts_years():
    cfg = get_config()
    t = cfg["timing"]
    tl = build(_eventful(5, cuts=1), 1990, 2023)
    assert tl.fit and tl.fit["before"] > 23.3 >= tl.duration and tl.n_frames <= 699
    assert t["fit"]["hold_min"] <= tl.hold < t["hold"] and tl.event_slowmo == t["event_slowmo"]   # dead time was enough
    many = build(_eventful(5, cuts=4), 1990, 2023)
    assert many.fit["before"] > tl.fit["before"] and many.duration <= 23.3
    assert many.hold == pytest.approx(t["fit"]["hold_min"]) and many.transition == pytest.approx(t["fit"]["transition_min"])
    assert many.event_slowmo < t["event_slowmo"]                                     # then less slow motion
    for s in many.slots:                                                             # every year is still drawn
        assert (s.x_start, s.x_end) == (1990, 2023) and s.years == YEARS and len(s.knots) == len(YEARS)
    short = build(_eventful(4, cuts=0), 1990, 2023)
    assert short.fit is None and short.hold == t["hold"]                             # nothing to fit: untouched


def test_a_race_that_cannot_fit_is_returned_at_its_shortest_and_does_not_fit():
    tl = build([(c, YEARS) for c in "ABCDEFG"], 1990, 2023)                          # 7 countries
    assert tl.fit and not tl.fit["fits"] and not fits(tl) and tl.fit["draw_share"] == pytest.approx(0.9)


def test_stretch_when_few_countries():
    # 4 countries at 4.2 s would be 19.3 s, so each is stretched to fill 20 s
    assert per_country_seconds(4) > 4.2
    assert math.isclose(total_seconds(4), get_config()["video"]["min_seconds"])


def test_layout_and_onsets_without_events():
    tl = build([("IND", YEARS), ("BRA", YEARS), ("USA", YEARS[3:])], 1990, 2023)
    cfg = get_config()["timing"]
    s0 = tl.slots[0]
    assert s0.start == pytest.approx(cfg["intro"]) == 0
    assert s0.trans_end == s0.start == s0.onsets[0]          # the first line moves from frame 0, no transition
    s1 = tl.slots[1]
    assert s1.trans_end - s1.start == pytest.approx(cfg["transition"])
    assert s0.end - s0.start == pytest.approx(s1.end - s1.start)   # same length: the first draws over the transition time
    assert s0.end - s0.draw_end == pytest.approx(cfg["hold"])
    assert tl.slots[1].start == pytest.approx(s0.end)
    assert s0.onsets[0] == pytest.approx(s0.draw_start)
    assert s0.onsets[-1] == pytest.approx(s0.draw_end)
    steps = [b - a for a, b in zip(s0.onsets, s0.onsets[1:])]
    assert max(steps) - min(steps) < 1e-5
    s2 = tl.slots[2]
    assert s2.onsets[0] == pytest.approx(s2.onset_of(1993)) and s2.onsets[0] > s2.draw_start
    assert tl.duration == pytest.approx(tl.end_start + cfg["end_card"])
    assert tl.n_frames == math.ceil(tl.duration * tl.fps - 1e-9)


def test_year_pos_matches_onsets():
    vals = [10.0] * 15 + [2.0] + [3.0 + 0.2 * k for k in range(len(YEARS) - 16)]
    tl = build([("IND", YEARS, vals)], 1990, 2023)
    s = tl.slots[0]
    for y, o in zip(s.years, s.onsets):
        assert s.year_pos(o) == pytest.approx(y)
        assert s.reached_index(o) == s.years.index(y)
    assert s.year_pos(0) == 1990 and s.year_pos(1e9) == 2023
    assert s.reached_index(s.draw_start - 0.01) == -1


def test_event_gets_three_times_the_step_and_lengthens_the_slot():
    vals = [10.0] * 15 + [2.0] + [2.0 + 0.1 * k for k in range(len(YEARS) - 16)]  # crash into 2005
    flat = build([("A", YEARS, [10.0 + 0.01 * k for k in range(len(YEARS))])], 1990, 2023)
    tl = build([("A", YEARS, vals)], 1990, 2023)
    s = tl.slots[0]
    assert [(e["year"], e["kind"]) for e in s.events] == [(2005, "fall")]
    normal = s.onsets[1] - s.onsets[0]
    event_step = s.onset_of(2005) - s.onset_of(2004)
    assert event_step == pytest.approx(3 * normal, rel=1e-6)
    assert s.end - s.start == pytest.approx((flat.slots[0].end - flat.slots[0].start) + 2 * normal, rel=1e-6)
    e = s.events[0]
    assert e["t_from"] == pytest.approx(s.onset_of(2004)) and e["t_to"] == pytest.approx(s.onset_of(2005))
    # the line head crosses the slow step at a third of the normal speed
    mid = (e["t_from"] + e["t_to"]) / 2
    assert s.year_pos(mid) == pytest.approx(2004.5)


def test_find_events_threshold():
    ev = find_events([1, 2, 3, 4], [0.0, 10.0, 7.6, 7.0], 0.25)
    assert [(e["year"], e["kind"]) for e in ev] == [(2, "jump")]   # -24% is not an event
    assert find_events([1, 2], [5.0, 5.0], 0.25) == []


def test_fits_and_roundtrip(tmp_path):
    tl = build([("IND", YEARS), ("BRA", YEARS)], 1990, 2023)
    assert fits(tl)              # 2 countries are stretched to exactly 20 s
    tl10 = build([(c, YEARS) for c in "ABCDEFGHIJ"], 1990, 2023)
    assert not fits(tl10)        # 10 x 4.2 + 2.5 = 44.5 s
    p = tmp_path / "tl.json"
    tl.save(p)
    tl2 = Timeline.load(p)
    assert tl2.all_onsets() == tl.all_onsets()
    assert (tl2.hook_hold, tl2.hook_shrink) == (get_config()["timing"]["hook_hold"], get_config()["timing"]["hook_shrink"])
    assert tl2.slot_at(tl.slots[1].start + 0.01).iso3 == "BRA"
    assert tl2.slot_at(tl.end_start + 0.1) is None
