"""The breakout model the autopilot picks reels with. Instagram only; Facebook is posted to but never scored.

Views are bimodal: a reel breaks out (1.6k-3.4k) or stays near a ~150 floor, decided within about 20 h. So the model
scores BREAKOUT PROBABILITY, not mean views.

  * Snapshots: the insights job (every 6 h) adds a stats row per reel; take_snapshots() keeps the row nearest 24 h
    (20-36 h) and nearest 72 h (from 66 h) of each post as its snapshots, with their real age.
  * Outcome: the 72 h views. A reel younger than that is estimated from its 24 h snapshot times the account's typical
    72 h / 24 h ratio (the median over posts that have both); with neither it is not scored yet.
  * Data: Instagram posts since autopilot.scoring_since (the rename to Graphony, 14 Oct 2026). A post made with an
    older video.format_version counts autopilot.old_format_weight (0.5).
  * Breakout: outcome >= max(breakout_multiple x the median outcome of the last breakout_window posts, breakout_floor).
  * Features of a reel: topic category, hook_template, hook_phrase, cold_open, slot time, length bucket.
  * Beta-Binomial per feature value: prior Beta(s * base, s * (1 - base)), s = prior_strength (4 posts) and base the
    account's (weighted) breakout rate, plus the weighted breakouts and non-breakouts of the posts with that value.
    One lucky reel moves its value from base to (4 base + 1) / 5, no further.
  * Reel score = sum over its features of logit(posterior mean) - logit(base): 0 is an average reel. A Thompson draw
    replaces each posterior mean with one draw from that Beta (one draw per feature value, shared by every reel that
    has it, so identical reels tie and the interest scorer breaks the tie).
  * Categories: exploited with >= exploit_min_posts scored posts. Retired from exploit with >= retire_min_posts and the
    retire_quantile (90%) upper credible bound of its breakout rate below half the base rate; explored once more
    retry_days (30) after it was retired; the outcome of that retry retires it again (or not). State: `categories`.
"""
from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median

from . import db
from .config import get_config, run_dir

log = logging.getLogger(__name__)

FEATURES = ["category", "hook_template", "hook_phrase", "cold_open", "slot", "length"]
LIVE = ("live", "uploaded", "private_locked")
DEFAULTS = {"scoring_since": "2026-10-14", "old_format_weight": 0.5, "snapshot_hours": [24, 72], "breakout_multiple": 5,
            "breakout_floor": 500, "breakout_window": 30, "prior_strength": 4, "exploit_min_posts": 4,
            "retire_min_posts": 6, "retire_quantile": 0.9, "retry_days": 30, "length_buckets": [21.5, 23.35]}
WINDOWS = {24: (20.0, 36.0), 72: (66.0, float("inf"))}   # the ages a snapshot may be taken at, per target
EPS = 1e-6


def settings() -> dict:
    return {**DEFAULTS, **(get_config().get("autopilot") or {})}


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _since() -> str:
    s = settings()["scoring_since"]
    return (s.isoformat() if hasattr(s, "isoformat") else str(s))[:10] + "T00:00:00Z"


def logit(p: float) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


# ------------------------------------------------------------------ snapshots

def instagram_posts() -> list[dict]:
    """Live Instagram posts since scoring_since, with their run's fields, oldest first."""
    with db.db() as c:
        rows = c.execute(
            """SELECT p.id post_id, p.run_id, p.created_at posted_at, p.slot, r.topic_id, r.score interest,
                      COALESCE(p.hook_template, r.hook_template) hook_template,
                      COALESCE(p.hook_phrase, r.hook_phrase) hook_phrase,
                      COALESCE(p.cold_open, r.cold_open) cold_open,
                      COALESCE(p.format_version, r.format_version) format_version, r.country_set
               FROM posts p JOIN runs r ON r.id = p.run_id
               WHERE p.platform='instagram' AND p.status IN ('live','uploaded','private_locked') AND p.created_at >= ?
               ORDER BY p.created_at, p.id""", (_since(),)).fetchall()
    return [dict(r) for r in rows]


def take_snapshots(now: datetime | None = None) -> int:
    """For each Instagram post and each target age (24, 72 h) without a snapshot yet: the stats row nearest the target
    inside its window becomes the snapshot (its real age is kept). Returns how many were added."""
    now = _now(now)
    added = 0
    with db.db() as c:
        have = {(r["post_id"], r["target_hours"]) for r in c.execute("SELECT post_id, target_hours FROM snapshots")}
    for p in instagram_posts():
        posted = db.parse_ts(p["posted_at"])
        for target in settings()["snapshot_hours"]:
            target = int(target)
            if (p["post_id"], target) in have:
                continue
            lo, hi = WINDOWS.get(target, (target * 0.9, target * 1.5))
            with db.db() as c:
                rows = c.execute("SELECT fetched_at, views FROM stats WHERE post_id=? AND views IS NOT NULL",
                                 (p["post_id"],)).fetchall()
            cands = []
            for r in rows:
                age = (db.parse_ts(r["fetched_at"]) - posted).total_seconds() / 3600
                if lo <= age <= hi and db.parse_ts(r["fetched_at"]) <= now:
                    cands.append((abs(age - target), age, r))
            if not cands:
                continue
            _, age, r = min(cands, key=lambda x: (x[0], x[1]))
            with db.db() as c:
                c.execute("INSERT OR IGNORE INTO snapshots(post_id, target_hours, age_hours, views, fetched_at) "
                          "VALUES (?,?,?,?,?)", (p["post_id"], target, round(age, 2), r["views"], r["fetched_at"]))
            added += 1
    if added:
        log.info("insights: %d snapshot(s) added", added)
    return added


def snapshots() -> dict[int, dict[int, dict]]:
    with db.db() as c:
        out: dict[int, dict[int, dict]] = {}
        for r in c.execute("SELECT * FROM snapshots"):
            out.setdefault(r["post_id"], {})[r["target_hours"]] = dict(r)
    return out


# ------------------------------------------------------------------ features

_TOPICS: dict | None = None


def category_of(topic_id: str) -> str:
    global _TOPICS
    if _TOPICS is None:
        from .topics import load_topics
        _TOPICS = {t["id"]: t.get("category") or "other" for t in load_topics()}
    if topic_id not in _TOPICS:
        _TOPICS = None
        from .topics import load_topics
        _TOPICS = {t["id"]: t.get("category") or "other" for t in load_topics()}
    return _TOPICS.get(topic_id, "other")


def reset_cache() -> None:
    global _TOPICS
    _TOPICS = None


def duration_of(run_id: str) -> float | None:
    try:
        return float(json.loads((run_dir(run_id) / "meta.json").read_text(encoding="utf-8"))["duration_seconds"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def length_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    lo, hi = (float(x) for x in settings()["length_buckets"])
    return "short" if seconds < lo else "mid" if seconds <= hi else "long"


def slot_of(posted_at: str, slot: str | None) -> str:
    """The slot a post went out in: posts.slot, else its IST time when within 45 min of a slot, else "other"."""
    if slot:
        return slot
    from .autopilot import ist, slot_times
    t = ist(db.parse_ts(posted_at))
    mins = t.hour * 60 + t.minute
    for s in slot_times():
        h, m = map(int, s.split(":"))
        if abs(mins - (h * 60 + m)) <= 45:
            return s
    return "other"


def features(run: dict, slot: str | None = None, posted_at: str | None = None) -> dict[str, str]:
    """The six features of a reel (run or post row). slot: the slot it is being considered for."""
    return {"category": category_of(run["topic_id"]),
            "hook_template": run.get("hook_template") or "other",
            "hook_phrase": run.get("hook_phrase") or "none",
            "cold_open": "yes" if run.get("cold_open") else "no",
            "slot": slot or (slot_of(posted_at, run.get("slot")) if posted_at else "other"),
            "length": length_bucket(duration_of(run["run_id"] if "run_id" in run else run["id"]))}


# ------------------------------------------------------------------ outcomes and the model

def outcomes(now: datetime | None = None) -> list[dict]:
    """Every scorable Instagram post: its features, its 72 h views (actual, or estimated from 24 h: "estimated"), and
    its weight. Posts with neither snapshot yet are left out."""
    s = settings()
    snaps = snapshots()
    pairs = [snaps[p][72]["views"] / snaps[p][24]["views"] for p in snaps
             if 24 in snaps[p] and 72 in snaps[p] and snaps[p][24]["views"]]
    ratio = median(pairs) if pairs else 1.0
    from .coldopen import format_version
    current = format_version()
    out = []
    for p in instagram_posts():
        sp = snaps.get(p["post_id"], {})
        if 72 in sp and sp[72]["views"] is not None:
            v72, est = float(sp[72]["views"]), False
        elif 24 in sp and sp[24]["views"] is not None:
            v72, est = float(sp[24]["views"]) * ratio, True
        else:
            continue
        w = 1.0 if int(p["format_version"] or 1) >= current else float(s["old_format_weight"])
        out.append({**p, "views72": round(v72, 1), "estimated": est, "weight": w,
                    "features": features(p, posted_at=p["posted_at"])})
    return out


@dataclass
class Model:
    rows: list[dict]
    threshold: float
    base: float
    ratio_note: str = ""
    post: dict = field(default_factory=dict)       # (feature, value) -> (alpha, beta, n posts)

    def posterior(self, f: str, v: str) -> tuple[float, float, int]:
        s = float(settings()["prior_strength"])
        return self.post.get((f, v), (s * self.base, s * (1 - self.base), 0))

    def mean(self, f: str, v: str) -> float:
        a, b, _ = self.posterior(f, v)
        return a / (a + b)

    def contribution(self, f: str, v: str) -> float:
        return logit(self.mean(f, v)) - logit(self.base)

    def score(self, feats: dict[str, str]) -> tuple[float, dict]:
        """(the reel's score, per feature {value, n, rate, log_odds})."""
        parts = {}
        for f in FEATURES:
            v = feats.get(f, "other")
            a, b, n = self.posterior(f, v)
            parts[f] = {"value": v, "n": n, "rate": round(a / (a + b), 4), "log_odds": round(self.contribution(f, v), 4)}
        return round(sum(x["log_odds"] for x in parts.values()), 4), parts

    def draws(self, rng: random.Random) -> "Draws":
        return Draws(self, rng)

    def category_posts(self, cat: str) -> int:
        return self.posterior("category", cat)[2]

    def upper(self, cat: str, q: float | None = None) -> float:
        from scipy.stats import beta as beta_dist
        a, b, _ = self.posterior("category", cat)
        return float(beta_dist.ppf(float(q or settings()["retire_quantile"]), a, b))


class Draws:
    """One Thompson draw per feature value, shared by every reel scored with it."""

    def __init__(self, model: Model, rng: random.Random):
        self.model, self.rng, self.cache = model, rng, {}

    def contribution(self, f: str, v: str) -> float:
        if (f, v) not in self.cache:
            a, b, _ = self.model.posterior(f, v)
            self.cache[(f, v)] = logit(self.rng.betavariate(a, b)) - logit(self.model.base)
        return self.cache[(f, v)]

    def score(self, feats: dict[str, str]) -> tuple[float, dict]:
        parts = {f: {"value": feats.get(f, "other"), "draw_log_odds": round(self.contribution(f, feats.get(f, "other")), 4)}
                 for f in FEATURES}
        return round(sum(x["draw_log_odds"] for x in parts.values()), 4), parts


def threshold_of(rows: list[dict]) -> float:
    s = settings()
    last = [r["views72"] for r in rows][-int(s["breakout_window"]):]
    med = median(last) if last else 0.0
    return max(float(s["breakout_multiple"]) * med, float(s["breakout_floor"]))


def build(rows: list[dict] | None = None, now: datetime | None = None) -> Model:
    """The model from the scorable posts (outcomes()): threshold, base rate and every feature value's posterior."""
    rows = outcomes(now) if rows is None else rows
    s = settings()
    thr = threshold_of(rows)
    for r in rows:
        r["breakout"] = r["views72"] >= thr
    wsum = sum(r["weight"] for r in rows)
    base = (sum(r["weight"] * r["breakout"] for r in rows) / wsum) if wsum else 0.25
    base = min(max(base, 0.02), 0.98)
    k = float(s["prior_strength"])
    acc: dict = {}
    for r in rows:
        for f in FEATURES:
            v = r["features"].get(f, "other")
            a, b, n = acc.get((f, v), (k * base, k * (1 - base), 0))
            acc[(f, v)] = (a + r["weight"] * r["breakout"], b + r["weight"] * (1 - r["breakout"]), n + 1)
    return Model(rows=rows, threshold=thr, base=base, post=acc)


# ------------------------------------------------------------------ categories: exploit, explore, retired

def catalog_categories() -> set[str]:
    from .topics import load_topics
    return {t.get("category") or "other" for t in load_topics() if t.get("enabled", True)}


def posted_counts() -> dict[str, int]:
    """Instagram posts per category since scoring_since, scored or not (for "untested")."""
    out: dict[str, int] = {}
    for p in instagram_posts():
        c = category_of(p["topic_id"])
        out[c] = out.get(c, 0) + 1
    return out


def _retired_rows() -> dict[str, dict]:
    with db.db() as c:
        return {r["category"]: dict(r) for r in c.execute("SELECT * FROM categories")}


def refresh_categories(model: Model, now: datetime | None = None) -> dict[str, dict]:
    """Retire / un-retire categories by the rule above and return every category's state:
    {"posts", "rate", "upper", "retired", "retry_due", "exploit", "explore"}."""
    now = _now(now)
    s = settings()
    stored = _retired_rows()
    cats = catalog_categories() | {r["features"]["category"] for r in model.rows} | set(stored)
    out = {}
    for cat in sorted(cats):
        n = model.category_posts(cat)
        upper = model.upper(cat)
        bad = n >= int(s["retire_min_posts"]) and upper < 0.5 * model.base
        row = stored.get(cat)
        retired_at = db.parse_ts(row["retired_at"]) if row and row.get("retired_at") else None
        reason = f"{n} posts, {s['retire_quantile']:.0%} upper bound {upper:.2f} < half the base rate {model.base:.2f}"
        if bad and retired_at is None:
            retired_at = now
            _set_retired(cat, now, reason)
            log.info("category %s retired: %s", cat, reason)
        elif not bad and retired_at is not None:
            retired_at = None
            _set_retired(cat, None, f"un-retired: {n} posts, upper bound {upper:.2f}")
            log.info("category %s un-retired (upper bound %.2f)", cat, upper)
        retry_due = False
        if retired_at is not None:
            due_from = retired_at + timedelta(days=float(s["retry_days"]))
            tried = [r for r in model.rows if r["features"]["category"] == cat and db.parse_ts(r["posted_at"]) >= due_from]
            if tried and bad:   # the retry was posted and scored, and it is still bad: retired again, a new clock
                retired_at = now
                _set_retired(cat, now, "retired again after its retry: " + reason)
                due_from = now + timedelta(days=float(s["retry_days"]))
            pending = any(category_of(p["topic_id"]) == cat and db.parse_ts(p["posted_at"]) >= due_from
                          for p in instagram_posts())
            retry_due = now >= due_from and not pending
        exploit = n >= int(s["exploit_min_posts"]) and retired_at is None
        out[cat] = {"posts": n, "rate": round(model.mean("category", cat), 4), "upper": round(upper, 4),
                    "retired": retired_at is not None, "retry_due": retry_due, "exploit": exploit,
                    "explore": (n < int(s["exploit_min_posts"]) and retired_at is None) or retry_due}
    return out


def _set_retired(cat: str, when: datetime | None, reason: str) -> None:
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None
    with db.db() as c:
        c.execute("INSERT INTO categories(category, retired_at, retired_reason, updated_at) VALUES (?,?,?,?) "
                  "ON CONFLICT(category) DO UPDATE SET retired_at=excluded.retired_at, "
                  "retired_reason=excluded.retired_reason, updated_at=excluded.updated_at",
                  (cat, stamp, reason, db.now()))


def untested_categories(states: dict[str, dict] | None = None) -> list[str]:
    """Categories in the topic catalog with no Instagram post since scoring_since."""
    counts = posted_counts()
    return sorted(c for c in catalog_categories() if not counts.get(c))


def cold_open_chance(model: Model | None = None, rng: random.Random | None = None, n: int = 4000) -> float:
    """P(a reel with a cold open breaks out more often than one without), by Monte Carlo over the two posteriors,
    clamped to autopilot.cold_open_min .. cold_open_max so both sides keep being tested."""
    model = model or build()
    rng = rng or random.Random(12345)
    a1, b1, _ = model.posterior("cold_open", "yes")
    a0, b0, _ = model.posterior("cold_open", "no")
    wins = sum(rng.betavariate(a1, b1) > rng.betavariate(a0, b0) for _ in range(n))
    ap = get_config().get("autopilot") or {}
    return min(max(wins / n, float(ap.get("cold_open_min", 0.2))), float(ap.get("cold_open_max", 0.8)))
