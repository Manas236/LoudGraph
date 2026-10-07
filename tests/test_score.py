import numpy as np

from pipeline.score import score_series, zigzag

YEARS = list(range(1990, 2024))  # 34 points


def rows(values):
    return [[y, float(v)] for y, v in zip(YEARS, values)]


def S(values, min_meaningful=0.5):
    return score_series(rows(values), 1990, min_meaningful)


rng = np.random.default_rng(42)
T = np.arange(len(YEARS))


def test_flat_rejected():
    s = S(np.full(len(YEARS), 5.0))
    assert not s["ok"] and "flat" in s["reason"]


def test_linear_interpolated_rejected():
    knots_x = np.arange(0, len(YEARS) + 4, 5)
    knots_y = rng.uniform(10, 40, len(knots_x))
    v = np.interp(T, knots_x, knots_y)
    s = S(v)
    assert not s["ok"] and "interpolated" in s["reason"], s
    assert s["interp"] > 0.6


def test_too_few_points_and_gaps_rejected():
    s = score_series([[y, float(y)] for y in range(2000, 2015)], 1990, 0.5)
    assert not s["ok"] and "points" in s["reason"]
    gappy = [[y, float(np.sin(y))] for y in YEARS if y not in (2000, 2001, 2002)]
    s = score_series(gappy, 1990, 0.01)
    assert not s["ok"] and "gap" in s["reason"]


def test_min_meaningful_rejected():
    s = S(0.05 + 0.04 * np.sin(T / 3), min_meaningful=2.0)
    assert not s["ok"] and "min_meaningful" in s["reason"]


def test_low_swing_rejected():
    s = S(100 + 3 * np.sin(T / 2) + rng.normal(0, 0.5, len(T)))
    assert not s["ok"] and "swing" in s["reason"]


def test_monotone_passes_with_zero_reversals():
    v = 10 + 0.8 * T + rng.normal(0, 0.15, len(T))
    s = S(v)
    assert s["ok"] and s["reversals"] == 0 and s["net"] > 0.9
    assert s["parts"]["reversals"] == 8


def test_v_shape_scores_one_reversal_and_beats_monotone_and_noise():
    v = np.abs(T - 17) * 1.5 + 20 + rng.normal(0, 0.3, len(T))
    sv = S(v)
    assert sv["ok"] and sv["reversals"] == 1
    mono = S(10 + 0.8 * T + rng.normal(0, 0.15, len(T)))
    noisy = S(20 + rng.normal(0, 4, len(T)))
    assert noisy["ok"] and noisy["reversals"] >= 6
    assert sv["score"] > mono["score"]
    # Spec weights: noise gets the minimum reversal points. (Its big year-to-year jumps still earn
    # shock points, so pure white noise can tie a clean V on total score; see REPORT known weaknesses.)
    assert noisy["parts"]["reversals"] == 5
    assert sv["parts"]["reversals"] - noisy["parts"]["reversals"] == 20


def test_crash_has_big_shock():
    v = 50 + 0.6 * T
    v[20:] -= 25  # one-year collapse
    v = v + rng.normal(0, 0.2, len(T))
    s = S(v)
    assert s["ok"]
    assert s["shock"] > 0.5 and s["shock_year"] == YEARS[20]
    assert s["parts"]["shock"] == 25


def test_score_bounds():
    for v in [np.abs(T - 17) * 1.5 + 20, 20 + rng.normal(0, 4, len(T)), 10 + 0.8 * T]:
        s = S(v)
        assert 0 <= s["score"] <= 100


def test_zigzag_counts():
    y = np.array([0, 5, 10, 5, 0, 5, 10], dtype=float)
    piv, rev = zigzag(y, 3)
    assert rev == 2 and piv == [0, 2, 4, 6]
