"""Per-country-series interestingness (section 3).

A series is a list of [year, value]. `score_series` returns a dict of features plus
`ok` / `reason` / `score` (0-100). Weights and thresholds come from config `scorer`.
"""
from __future__ import annotations

import numpy as np

from .config import get_config


def _cfg() -> dict:
    return get_config()["scorer"]


def zigzag(y: np.ndarray, threshold: float) -> tuple[list[int], int]:
    """Indices of zigzag pivots (first and last point included) and the number of
    direction changes, where a leg must move at least `threshold` (absolute units)."""
    n = len(y)
    if n < 2 or threshold <= 0:
        return [0, max(n - 1, 0)], 0
    pivots = [0]
    direction = 0
    lo_i = hi_i = 0
    ext_i = 0
    reversals = 0
    for i in range(1, n):
        if direction == 0:
            if y[i] < y[lo_i]:
                lo_i = i
            if y[i] > y[hi_i]:
                hi_i = i
            if y[i] - y[lo_i] >= threshold:
                direction, ext_i = 1, i
                if lo_i != 0:
                    pivots.append(lo_i)
            elif y[hi_i] - y[i] >= threshold:
                direction, ext_i = -1, i
                if hi_i != 0:
                    pivots.append(hi_i)
        elif direction == 1:
            if y[i] > y[ext_i]:
                ext_i = i
            elif y[ext_i] - y[i] >= threshold:
                pivots.append(ext_i)
                direction, ext_i = -1, i
                reversals += 1
        else:
            if y[i] < y[ext_i]:
                ext_i = i
            elif y[i] - y[ext_i] >= threshold:
                pivots.append(ext_i)
                direction, ext_i = 1, i
                reversals += 1
    if direction != 0 and pivots[-1] != ext_i:
        pivots.append(ext_i)
    if pivots[-1] != n - 1:
        pivots.append(n - 1)
    return pivots, reversals


def score_series(rows, start_year: int, min_meaningful: float, cfg: dict | None = None) -> dict:
    c = cfg or _cfg()
    w = c["weights"]
    rows = [r for r in rows if r[0] >= start_year]
    years = np.array([r[0] for r in rows], dtype=int)
    y = np.array([r[1] for r in rows], dtype=float)
    out = {"n": int(len(y)), "ok": False, "reason": None, "score": 0.0}
    if len(y) < c["min_points"]:
        out["reason"] = f"only {len(y)} points (< {c['min_points']})"
        return out
    gaps = np.diff(years)
    out["first_year"], out["last_year"] = int(years[0]), int(years[-1])
    if gaps.max() > c["max_gap_years"]:
        g = int(np.argmax(gaps))
        out["reason"] = f"gap {years[g]}-{years[g + 1]}"
        return out
    max_abs = float(np.max(np.abs(y)))
    out["max_abs"] = max_abs
    if max_abs < min_meaningful:
        out["reason"] = f"max {max_abs:.3g} < min_meaningful {min_meaningful}"
        return out
    rng = float(y.max() - y.min())
    if rng <= 0:
        out["reason"] = "flat (zero range)"
        return out
    d1 = np.diff(y)
    d2 = np.diff(y, 2)
    interp = float(np.mean(np.abs(d2) <= c["interp_tol"] * rng)) if len(d2) else 0.0
    flat = float(np.mean(np.abs(d1) <= c["flat_tol"] * rng))
    mean_abs = float(np.mean(np.abs(y)))
    swing = rng / mean_abs if mean_abs > 0 else float("inf")
    pivots, reversals = zigzag(y, c["reversal_threshold"] * rng)
    k = int(np.argmax(np.abs(d1)))
    shock = float(abs(d1[k]) / rng)
    net = float((y[-1] - y[0]) / rng)
    out.update(
        interp=round(interp, 3), flat=round(flat, 3), swing=round(swing, 3), reversals=int(reversals),
        shock=round(shock, 3), shock_year=int(years[k + 1]), net=round(net, 3),
        pivot_years=[int(years[i]) for i in pivots], min=float(y.min()), max=float(y.max()),
    )
    if interp + flat > c["max_interp_flat"]:
        out["reason"] = f"interpolated/flat {interp + flat:.2f} > {c['max_interp_flat']}"
        return out
    if swing < c["min_swing"]:
        out["reason"] = f"swing {swing:.3f} < {c['min_swing']}"
        return out
    rev_table = {int(k2): v for k2, v in w["reversals"].items() if str(k2) != "other"}
    parts = {
        "swing": min(swing / w["swing_full"], 1.0) * w["swing_max"],
        "reversals": rev_table.get(reversals, w["reversals"]["other"]),
        "shock": min(shock / w["shock_full"], 1.0) * w["shock_max"],
        "smooth": (1.0 - interp) * w["smooth_max"],
    }
    out["parts"] = {k2: round(v, 2) for k2, v in parts.items()}
    out["score"] = round(float(sum(parts.values())), 2)
    out["ok"] = True
    return out


def score_topic(data: dict, topic: dict) -> list[dict]:
    """Score every pool country of a fetched topic. Adds topic-level freshness checks
    (series must reach near the latest year and start near start_year).
    Returns a list sorted by score (passing first), each item carrying `iso3`."""
    dcfg = get_config()["data"]
    series = data["series"]
    latest = max((rows[-1][0] for rows in series.values() if rows), default=None)
    out = []
    for iso3, rows in series.items():
        s = score_series(rows, topic["start_year"], topic["min_meaningful"])
        s["iso3"] = iso3
        if s["ok"]:
            if latest - s["last_year"] > dcfg["max_stale_years"]:
                s.update(ok=False, reason=f"stale: ends {s['last_year']}, topic latest {latest}")
            elif s["first_year"] - topic["start_year"] > dcfg["max_late_start_years"]:
                s.update(ok=False, reason=f"starts late: {s['first_year']}")
        out.append(s)
    out.sort(key=lambda s: (not s["ok"], -s["score"]))
    return out
