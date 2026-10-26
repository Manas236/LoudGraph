"""Choose one video's country set (section 4)."""
from __future__ import annotations

import random

import numpy as np

from .config import get_config
from .timeline import build, country_count_bounds


class NotEnoughData(RuntimeError):
    pass


class LowVariety(NotEnoughData):
    """The best set we can build is bland (no falling country, or shapes too alike)."""


def z_curve(rows, x0: int, x1: int, m: int = 64) -> np.ndarray:
    """Resample a series onto a common year grid and z-normalise it."""
    ys = np.array([r[0] for r in rows if x0 <= r[0] <= x1], dtype=float)
    vs = np.array([r[1] for r in rows if x0 <= r[0] <= x1], dtype=float)
    grid = np.linspace(x0, x1, m)
    c = np.interp(grid, ys, vs)
    sd = c.std()
    return (c - c.mean()) / sd if sd > 0 else c * 0


def distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sqrt(np.mean((a - b) ** 2)))


def min_pairwise(curves: dict, sel: list[str]) -> float:
    if len(sel) < 2:
        return float("inf")
    return min(distance(curves[a], curves[b]) for i, a in enumerate(sel) for b in sel[i + 1:])


def target_count(n_passing: int, cfg: dict | None = None) -> int:
    cfg = cfg or get_config()
    n_min, n_max = country_count_bounds(cfg)
    n_max = min(n_max, cfg["picker"]["max_countries"])
    if n_passing < cfg["picker"]["min_countries"]:
        raise NotEnoughData(f"not enough interesting data: {n_passing} countries pass the scorer "
                            f"(need {cfg['picker']['min_countries']})")
    # With fewer than n_min passing, the timeline stretches each country to still fill min_seconds.
    return min(n_max, n_passing)


def _greedy(pool: list[dict], n: int, curves: dict, pc: dict) -> list[str]:
    by = {s["iso3"]: s for s in pool}
    sel = [pool[0]["iso3"]]  # best score is always in (it plays last)

    def best(cands):
        return max(cands, key=lambda iso: (round(min(distance(curves[iso], curves[x]) for x in sel), 6),
                                           by[iso]["score"]))

    constraints = [
        lambda s: s["net"] < pc["net_down"],   # falling first: it is the hard one (B2)
        lambda s: s["net"] > pc["net_up"],
        lambda s: s["reversals"] >= pc["reversal_story"],
    ]
    for ok in constraints:
        if len(sel) >= n or any(ok(by[x]) for x in sel):
            continue
        cands = [s["iso3"] for s in pool if s["iso3"] not in sel and ok(s)]
        if cands:
            sel.append(best(cands))
    while len(sel) < n:
        cands = [s["iso3"] for s in pool if s["iso3"] not in sel]
        sel.append(best(cands))
    return sel


def _variety_problem(sel: list[str], by: dict, curves: dict, pc: dict) -> str | None:
    if pc.get("require_falling", True) and not any(by[c]["net"] < pc["net_down"] for c in sel):
        return f"no falling country (net < {pc['net_down']})"
    d = min_pairwise(curves, sel)
    if d < pc.get("min_variety_distance", 0.0):
        return f"shapes too alike (min pairwise distance {d:.3f} < {pc['min_variety_distance']})"
    return None


def _rows_in(series_rows, x0, x1):
    return [(int(y), float(v)) for y, v in series_rows if x0 <= y <= x1]


def _has_year(series_rows, year: int) -> bool:
    return any(int(y) == year for y, _ in series_rows)


def final_year(cands: list[dict], series: dict, n: int, n_min: int) -> int:
    """The reel's last year: the latest year that at least n candidates (else at least n_min) have a value for, so
    every country in the reel has data through its final year (a series that stops earlier is left out)."""
    years = sorted({int(y) for s in cands for y, _ in series[s["iso3"]]}, reverse=True)
    for need in (n, n_min):
        for y in years:
            if sum(1 for s in cands if _has_year(series[s["iso3"]], y)) >= need:
                return y
    raise NotEnoughData(f"not enough interesting data: fewer than {n_min} countries share a final year")


def pick(scored: list[dict], series: dict, topic: dict, exclude_sets: set[str] | None = None,
         seed: int | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or get_config()
    pc = cfg["picker"]
    exclude_sets = exclude_sets or set()
    passing = sorted((s for s in scored if s["ok"]), key=lambda s: -s["score"])
    n = target_count(len(passing), cfg)
    cands = passing[: pc["top_k"]]
    if not any(s["net"] < pc["net_down"] for s in cands):
        falling = [s for s in passing if s["net"] < pc["net_down"]]
        if falling:
            cands = cands + falling[:1]
    x0 = topic["start_year"]
    x1 = final_year(cands, series, n, pc["min_countries"])
    cands = [s for s in cands if _has_year(series[s["iso3"]], x1)]   # every country has data through the last year
    n = min(n, len(cands))
    curves = {s["iso3"]: z_curve(series[s["iso3"]], x0, x1) for s in cands}
    by = {s["iso3"]: s for s in cands}
    rng = random.Random(seed)
    sel, attempts, variety_problem, good_but_used = None, 0, None, False
    for attempt in range(pc["max_attempts"]):
        if attempt == 0:
            pool = cands
        elif len(cands) <= n:
            break
        else:
            # random restart: drop a random subset (keeping >= n) so a different set comes out
            pool = sorted(rng.sample(cands, rng.randint(n, len(cands) - 1)), key=lambda s: -s["score"])
        attempts += 1
        trial = _greedy(pool, n, curves, pc)
        why = _variety_problem(trial, by, curves, pc)
        used = ",".join(sorted(trial)) in exclude_sets
        if why:
            variety_problem = variety_problem or why
            continue
        if used:
            good_but_used = True
            continue
        sel = trial
        break
    if sel is None:
        # a bland set is "low variety" whether or not it was used before; only when a varied set
        # existed and had already been posted is the topic simply exhausted
        if good_but_used:
            raise NotEnoughData("every varied country set we can build for this topic has already been used")
        raise LowVariety(f"low variety: {variety_problem}")
    ordered = sorted(sel, key=lambda iso: by[iso]["score"])  # lowest drama first, best LAST

    # Slow-mo events lengthen the video: the timeline is fitted first (timing.fit: dead time, then slow-mo, then
    # line speed; data years are never cut), then the least interesting country is dropped until it fits.
    dropped = []
    vmax = cfg["video"]["max_seconds"]
    while True:
        rows = {c: _rows_in(series[c], x0, x1) for c in ordered}
        tl = build([(c, [y for y, _ in rows[c]], [v for _, v in rows[c]]) for c in ordered], x0, x1, cfg)
        if tl.duration <= vmax + 1e-6:
            break
        if len(ordered) <= pc["min_countries"]:
            how = (f" at the shortest timing (hold {tl.fit['hold']} s, slow-mo {tl.fit['event_slowmo']}x, line "
                   f"{tl.fit['draw_share']:.0%} of its time)" if tl.fit else "")
            raise NotEnoughData(f"video {tl.duration:.1f}s > {vmax}s even with {len(ordered)} countries{how}, "
                                f"without cutting data years")
        removable = [c for c in ordered
                     if _variety_problem([x for x in ordered if x != c], by, curves, pc) is None] or ordered
        drop = min(removable, key=lambda c: by[c]["score"])
        ordered.remove(drop)
        dropped.append(drop)
    # the video opens on the first country's line at t = 0: it must have data from x0 (the best stays last)
    starts = {c: min(y for y, _ in _rows_in(series[c], x0, x1)) for c in ordered}
    if starts[ordered[0]] > x0:
        for k in range(1, len(ordered) - 1):
            if starts[ordered[k]] <= x0:
                trial = [ordered[k]] + ordered[:k] + ordered[k + 1:]
                rows = {c: _rows_in(series[c], x0, x1) for c in trial}
                tl2 = build([(c, [y for y, _ in rows[c]], [v for _, v in rows[c]]) for c in trial], x0, x1, cfg)
                if tl2.duration <= vmax + 1e-6:   # the first slot's slow-mo steps differ slightly
                    ordered, tl = trial, tl2
                break
    chosen = [by[i] for i in ordered]
    key = ",".join(sorted(ordered))
    return {
        "countries": ordered,
        "country_set": key,
        "n": len(ordered),
        "x_start": x0,
        "x_end": x1,
        "duration_seconds": tl.duration,
        "dropped_for_length": dropped,
        "min_pairwise_distance": round(min_pairwise(curves, ordered), 4),
        "constraints": {
            "net_up": any(s["net"] > pc["net_up"] for s in chosen),
            "net_down": any(s["net"] < pc["net_down"] for s in chosen),
            "reversal_story": any(s["reversals"] >= pc["reversal_story"] for s in chosen),
        },
        "mean_score": round(float(np.mean([s["score"] for s in chosen])), 2),
        "scores": {s["iso3"]: s for s in chosen},
        "candidates": [{"iso3": s["iso3"], "score": s["score"]} for s in cands],
        "attempts": attempts,
    }
