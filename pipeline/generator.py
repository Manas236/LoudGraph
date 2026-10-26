"""The autopilot's daily generation run (`python -m pipeline generate`, 03:00 IST).

  1. Yesterday's pool losses are counted; reels at autopilot.pool_max_losses are dropped (autopick.count_losses).
  2. Less than autopilot.min_free_gb free on disk: generation pauses (posting goes on) and one alert goes out.
  3. Fewer than autopilot.discover_below untested categories in the topic catalog: new categories are discovered
     from the World Bank and OWID catalogs (discover.py, at most discover_per_week a week).
  4. The pool is topped up: ready = reels in the pool; it makes min(pool_max_new, pool_target - ready) reels, none
     when ready >= pool_target. At least pool_min_explore of the pool's reels are kept explore-eligible: the first
     new reels go to explore-eligible categories until there are that many.
  5. Topics: enabled, rested (topic_cooldown_days), interesting (review.topic_score > 0, the scorer on the cached
     series), not of a category retired from exploit (unless its retry is due). Each new reel's category is drawn by
     Thompson sampling on the category breakout posteriors (learn.py), at most 3 reels of one category in the pool;
     within it, a topic weighted by its interest score. A topic that turns out dull (skipped) makes way for another,
     up to 5 tries per reel. Each reel's cold open is decided by coldopen.decide (mode thompson).
  6. Alerts: 0 reels made when the pool needed some (a Gemini outage that blocked it is named as such), and fewer
     than 3 ready reels after the run.
"""
from __future__ import annotations

import logging
import random
import shutil

from . import alerts, autopick, db, learn
from .autopilot import ist, settings as ap_settings
from .config import ROOT, get_config

log = logging.getLogger(__name__)
MAX_PER_CATEGORY = 3
TRIES_PER_REEL = 5


def free_gb() -> float:
    return shutil.disk_usage(ROOT).free / 1e9


def check_disk() -> bool:
    """True when there is room to generate; else one alert (cleared once there is room again)."""
    need = float(ap_settings().get("min_free_gb", 5))
    free = free_gb()
    if free < need:
        alerts.fire("disk", f"only {free:.1f} GB free on disk (under {need:g} GB): generation is paused, posting goes on",
                    "df -h on the server; old reels in out/ (posted media is deleted after 14 days)")
        return False
    alerts.clear("disk", f"disk space is back ({free:.1f} GB free)")
    return True


def check_pool(n: int | None = None) -> int:
    n = len(autopick.pool()) if n is None else n
    if n < 3:
        alerts.fire("pool_low", f"only {n} ready reel(s) in the pool (under 3)",
                    "the generate job's log (journalctl -u graphony-generate) and cache/logs/pipeline.log")
    else:
        alerts.clear("pool_low", f"the pool has {n} ready reels again")
    return n


def plan_count(ready: int) -> int:
    s = ap_settings()
    target, most = int(s.get("pool_target", 10)), int(s.get("pool_max_new", 5))
    return 0 if ready >= target else min(most, target - ready)


def explore_count(runs: list[dict], states: dict) -> int:
    return sum(1 for r in runs if states.get(learn.category_of(r["topic_id"]), {}).get("explore"))


def candidate_topics(exclude=()) -> list[dict]:
    from .review import topic_score
    from .selector import eligible_topics
    out = []
    for t in eligible_topics(exclude=exclude):
        s = topic_score(t)
        if s > 0:
            out.append({**t, "score": s})
    return out


def choose_topic(cands: list[dict], states: dict, model: learn.Model, in_pool: dict[str, int], explore: bool,
                 rng: random.Random) -> dict | None:
    """One topic: its category by a Thompson draw (explore=True: only explore-eligible categories), then a topic of
    that category weighted by interest score."""
    def usable(t):
        st = states.get(t.get("category") or "other", {})
        if st.get("retired") and not st.get("retry_due"):
            return False
        if explore and not st.get("explore", True):
            return False
        return in_pool.get(t.get("category") or "other", 0) < MAX_PER_CATEGORY
    ok = [t for t in cands if usable(t)]
    if not ok:
        return None
    draws = {}
    for cat in {t.get("category") or "other" for t in ok}:
        a, b, _ = model.posterior("category", cat)
        draws[cat] = rng.betavariate(a, b)
    cat = max(draws, key=lambda c: (draws[c], c))
    mine = [t for t in ok if (t.get("category") or "other") == cat]
    return rng.choices(mine, weights=[t["score"] for t in mine], k=1)[0]


def generate(count: int | None = None, dry: bool = False, rng: random.Random | None = None) -> int:
    """The generation run (inside autopilot.run_job). dry=True: say what it would do and render ONE test video
    (out/test-*, no database row: never in the pool, never posted). Returns an exit code."""
    from . import gemini, orchestrator
    rng = rng or random.Random()
    if not dry:
        dropped = autopick.count_losses(ist().date())
        if dropped:
            log.info("pool: dropped %d reel(s) after %s days unpicked", len(dropped), ap_settings().get("pool_max_losses"))
        autopick.cleanup_posted_media()
    if not dry and not check_disk():
        return 0
    model = learn.build()
    states = learn.refresh_categories(model)
    _discover(states, dry)
    learn.reset_cache()
    states = learn.refresh_categories(model)
    runs = autopick.pool()
    ready = len(runs)
    n = plan_count(ready) if count is None else count
    need_explore = max(0, int(ap_settings().get("pool_min_explore", 3)) - explore_count(runs, states))
    in_pool: dict[str, int] = {}
    for r in runs:
        c = learn.category_of(r["topic_id"])
        in_pool[c] = in_pool.get(c, 0) + 1
    log.info("generate: %d ready (%d explore-eligible), making %d (at least %d explore)", ready,
             explore_count(runs, states), n, min(n, need_explore))
    if n <= 0:
        check_pool(ready)
        return 0
    before = gemini.ALL_FAILED["count"]
    made, tried = [], set()
    for k in range(n):
        explore = k < need_explore
        for _ in range(TRIES_PER_REEL):
            topic = choose_topic(candidate_topics(exclude=tried), states, model, in_pool, explore, rng)
            if topic is None and explore:
                topic = choose_topic(candidate_topics(exclude=tried), states, model, in_pool, False, rng)
            if topic is None:
                break
            tried.add(topic["id"])
            cat = topic.get("category") or "other"
            if dry:
                print(f"dry run: would make {topic['name']} [{cat}] ({'explore' if states.get(cat, {}).get('explore') else 'exploit'}"
                      f" category), interest {topic['score']:.1f}")
                rid = orchestrator.make_test_video(topic["id"])
                print(f"dry run: TEST video {rid} rendered (not in the pool, never posted): "
                      f"{(ROOT / get_config()['paths']['out'] / rid / 'video.mp4')}")
                made.append(rid)
                return 0
            rid = db.create_run(topic["id"])
            db.log(rid, "queued", f"made by the autopilot's generation run ({'explore' if explore else 'pool'} pick, "
                                  f"category {cat})")
            if orchestrator.run_pipeline(rid):
                made.append(rid)
                in_pool[cat] = in_pool.get(cat, 0) + 1
                break
            if db.get_run(rid)["stage"] != "skipped":
                log.warning("generate: %s failed: %s", topic["id"], db.get_run(rid).get("error"))
    log.info("generate: made %d of %d: %s", len(made), n, made)
    if not made:
        if gemini.ALL_FAILED["count"] > before:
            alerts.fire("gen_zero", f"generation made 0 reels: every Gemini model failed ({gemini.ALL_FAILED['last'][:160]})",
                        "the Gemini key and quota (python -m pipeline health), gemini.fallback_models in config.yaml")
        else:
            alerts.fire("gen_zero", f"generation made 0 of {n} reels the pool needed",
                        "journalctl -u graphony-generate and the failed runs in cache/logs/pipeline.log")
    else:
        alerts.clear("gen_zero", f"generation works again ({len(made)} reel(s) made)")
    check_pool()
    return 0


def _discover(states: dict, dry: bool) -> None:
    """Step 3: new categories when fewer than discover_below untested ones remain. Never breaks the run."""
    s = ap_settings()
    untested = learn.untested_categories(states)
    if len(untested) >= int(s.get("discover_below", 5)):
        return
    try:
        from . import discover
        if dry:
            print(f"dry run: {len(untested)} untested categories (< {s.get('discover_below', 5)}): the real run would "
                  f"discover up to {discover.quota_left()} new ones this week")
            return
        added = discover.run(max_new=discover.quota_left())
        if added:
            log.info("discover: new categories %s", ", ".join(added))
    except Exception as e:  # noqa: BLE001 - discovery is a bonus; the pool is made from what exists
        log.warning("category discovery failed: %s", e)
