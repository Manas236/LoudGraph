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
    series, scored = make_pool(n=5)
    r = pick(scored, series, TOPIC)
    assert r["n"] == 5
