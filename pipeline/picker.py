"""Choose one video's country set (section 4)."""
from __future__ import annotations

import random

import numpy as np

from .config import get_config
from .timeline import country_count_bounds


class NotEnoughData(RuntimeError):
    pass


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
        lambda s: s["net"] > pc["net_up"],
        lambda s: s["net"] < pc["net_down"],
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


def pick(scored: list[dict], series: dict, topic: dict, exclude_sets: set[str] | None = None,
         seed: int | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or get_config()
    pc = cfg["picker"]
    exclude_sets = exclude_sets or set()
    passing = [s for s in scored if s["ok"]]
    n = target_count(len(passing), cfg)
    cands = sorted(passing, key=lambda s: -s["score"])[: pc["top_k"]]
    x0 = topic["start_year"]
    x1 = max(s["last_year"] for s in cands)
    curves = {s["iso3"]: z_curve(series[s["iso3"]], x0, x1) for s in cands}
    rng = random.Random(seed)
    key, sel, attempts = None, [], 0
    for attempt in range(pc["max_attempts"]):
        if attempt == 0:
            pool = cands
        elif len(cands) <= n:
            break
        else:
            # random restart: drop a random subset (keeping >= n) so a different set comes out
            pool = sorted(rng.sample(cands, rng.randint(n, len(cands) - 1)), key=lambda s: -s["score"])
        attempts += 1
        sel = _greedy(pool, n, curves, pc)
        key = ",".join(sorted(sel))
        if key not in exclude_sets:
            break
        key = None
    if key is None:
        raise NotEnoughData("every country set we can build for this topic has already been used")
    by = {s["iso3"]: s for s in cands}
    ordered = sorted(sel, key=lambda iso: by[iso]["score"])  # lowest drama first, best LAST
    chosen = [by[i] for i in ordered]
    return {
        "countries": ordered,
        "country_set": key,
        "n": n,
        "x_start": x0,
        "x_end": x1,
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
