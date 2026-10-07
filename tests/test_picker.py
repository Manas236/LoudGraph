import numpy as np
import pytest

from pipeline.picker import NotEnoughData, pick, z_curve, distance
from pipeline.score import score_series

YEARS = list(range(1990, 2024))
T = np.arange(len(YEARS))
TOPIC = {"id": "t", "start_year": 1990, "min_meaningful": 0.1}


def make_pool(seed=0, n=16):
    rng = np.random.default_rng(seed)
    shapes = []
    for i in range(n):
        kind = i % 4
        if kind == 0:   # rising
            v = 10 + (0.5 + 0.1 * i) * T
        elif kind == 1:  # falling
            v = 60 - (0.5 + 0.1 * i) * T
        elif kind == 2:  # double hump (>= 2 reversals)
            v = 30 + 10 * np.sin(T / (3 + 0.3 * i))
        else:            # V
            v = np.abs(T - (10 + i % 10)) * (1 + 0.1 * i) + 15
        shapes.append(v + rng.normal(0, 0.4, len(T)))
    series = {f"C{i:02d}": [[y, float(x)] for y, x in zip(YEARS, v)] for i, v in enumerate(shapes)}
    scored = []
    for iso, rows in series.items():
        s = score_series(rows, 1990, 0.1)
        s["iso3"] = iso
        scored.append(s)
    return series, scored


def test_pick_constraints_and_order():
    series, scored = make_pool()
    r = pick(scored, series, TOPIC)
    assert 6 <= r["n"] <= 8 and len(r["countries"]) == r["n"]
    assert len(set(r["countries"])) == r["n"]
    assert all(r["constraints"].values()), r["constraints"]
    sc = [r["scores"][c]["score"] for c in r["countries"]]
    assert sc == sorted(sc), "lower drama first, best last"
    best = max((s for s in scored if s["ok"]), key=lambda s: s["score"])
    assert r["countries"][-1] == best["iso3"]


def test_pick_is_diverse():
    series, scored = make_pool()
    r = pick(scored, series, TOPIC)
    curves = {c: z_curve(series[c], 1990, 2023) for c in series}
    chosen_min = r["min_pairwise_distance"]
    # a naive top-n-by-score set should not be more diverse than the greedy choice
    top = [s["iso3"] for s in sorted((s for s in scored if s["ok"]), key=lambda s: -s["score"])[: r["n"]]]
    naive_min = min(distance(curves[a], curves[b]) for i, a in enumerate(top) for b in top[i + 1:])
    assert chosen_min >= naive_min - 1e-9


def test_never_repeats_a_set():
    series, scored = make_pool()
    used = set()
    for i in range(5):
        r = pick(scored, series, TOPIC, exclude_sets=used, seed=i)
        assert r["country_set"] not in used
        used.add(r["country_set"])


def test_not_enough_data():
    series, scored = make_pool(n=4)
    with pytest.raises(NotEnoughData, match="not enough interesting data"):
        pick(scored, series, TOPIC)


def test_five_passing_is_allowed():
    rng = np.random.default_rng(1)
    crash = 50 + 0.6 * T
    crash[20:] -= 25
    shapes = [10 + 0.8 * T, 60 - 0.9 * T, 30 + 10 * np.sin(T / 3), np.abs(T - 12) * 1.5 + 15, crash]
    series = {f"F{i}": [[y, float(x)] for y, x in zip(YEARS, v + rng.normal(0, 0.3, len(T)))]
              for i, v in enumerate(shapes)}
    r = pick(_scored(series), series, TOPIC)
    assert r["n"] == 5


def _scored(series):
    out = []
    for iso, rows in series.items():
        s = score_series(rows, 1990, 0.1)
        s["iso3"] = iso
        out.append(s)
    return out


def test_low_variety_when_nothing_falls():
    """B2: no country with net < -0.5 -> the run must fail as low variety, not render a bland set."""
    from pipeline.picker import LowVariety
    rng = np.random.default_rng(3)
    series = {f"R{i:02d}": [[y, float(x)] for y, x in zip(YEARS, 10 + (0.5 + 0.05 * i) * T + 3 * np.sin(T / (2 + i))
                                                         + rng.normal(0, 0.3, len(T)))] for i in range(12)}
    with pytest.raises(LowVariety, match="low variety: no falling country"):
        pick(_scored(series), series, TOPIC)


def test_low_variety_when_shapes_are_alike():
    from pipeline.picker import LowVariety
    rng = np.random.default_rng(4)
    base = np.abs(T - 17) * 1.5 + 20
    series = {}
    for i in range(12):   # the same V (with a falling tail) for everyone
        v = base.copy()
        v[25:] = v[25] - (T[25:] - 25) * 4
        series[f"S{i:02d}"] = [[y, float(x)] for y, x in zip(YEARS, v * (1 + 0.01 * i) + rng.normal(0, 0.2, len(T)))]
    with pytest.raises(LowVariety, match="shapes too alike"):
        pick(_scored(series), series, TOPIC)


def test_drops_countries_when_slowmo_makes_it_too_long():
    rng = np.random.default_rng(5)
    series = {}
    for i in range(16):   # every series has several >= 25% one-year moves (events)
        v = 50 + 30 * np.sign(np.sin(T / (1.6 + 0.1 * i))) + rng.normal(0, 0.5, len(T))
        if i % 3 == 0:
            v = v - 2.5 * T          # falling ones
        elif i % 3 == 1:
            v = v + 2.5 * T          # rising ones
        series[f"E{i:02d}"] = [[y, float(x)] for y, x in zip(YEARS, v)]
    r = pick(_scored(series), series, TOPIC)
    assert r["duration_seconds"] <= 45 + 1e-6
    assert r["dropped_for_length"] and r["n"] < 8
    assert r["constraints"]["net_down"]
