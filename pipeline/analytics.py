"""Stats (section 12): `run.py stats`, and inside the bot's own process (tick(), on a background thread):
once when the bot starts, then every analytics.every_hours, so it keeps running wherever the bot runs,
without an OS scheduler. Only one pull runs at a time on the machine (the "stats" lock), so the bot's
pull and the dashboard's Check stats now never overlap.

Every pull asks about EVERY reel ever posted (so the first one is the backfill) and adds one row per
post to the stats table with its time; rows are never overwritten, so a reel's growth stays readable.

YouTube Data API -> views; YouTube Analytics API -> averageViewPercentage.
Instagram media insights (checked against the Meta docs and live on 2026-10-16; `plays` is gone, `views`
replaced it): views, reach, likes, comments, shares, saved, total_interactions, ig_reels_avg_watch_time,
ig_reels_video_view_total_time, reels_skip_rate.
Facebook Page reels, /{video-id}/video_insights (lifetime): fb_reels_total_plays (= views),
blue_reels_play_count, fb_reels_replay_count, post_video_avg_time_watched, post_video_view_time,
post_video_social_actions, post_video_likes_by_reaction_type, post_video_followers
(post_impressions_unique is refused for Page reels).
A metric Meta refuses is dropped and the rest asked for again (_insights). Everything Meta returns is
kept in stats.raw; views, reach, likes, comments, shares, saved, the average watch time (seconds, and as a
% of the video's length), the total watch time (seconds) and Instagram's reels_skip_rate (% of views that
skipped within the first 3 seconds; Facebook has none) get their own columns. Meta gives no retention curve
or completion rate for reels.

Then the topic weights and the hook-template weights are recomputed from the platforms in
analytics.weight_platforms (views z-scored per platform). Each post carries its run's hook and cold open
(coldopen.py) next to its topic, so the stats can be grouped by any of them (`python run.py coldopen`).
The Telegram summary goes out at most once per analytics.summary_every_hours. A failed pull is logged and
tried again on the next cycle; it never stops the bot.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timezone

import numpy as np

from . import db, meta_api
from .config import get_config, run_dir, secret

log = logging.getLogger(__name__)
IG_METRICS = ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions", "ig_reels_avg_watch_time",
              "ig_reels_video_view_total_time", "reels_skip_rate"]
FB_METRICS = ["fb_reels_total_plays", "blue_reels_play_count", "fb_reels_replay_count", "post_video_avg_time_watched",
              "post_video_view_time", "post_video_social_actions", "post_video_likes_by_reaction_type",
              "post_video_followers"]
K_LAST_RUN, K_LAST_SUMMARY = "stats:last_run", "stats:last_summary"
_refused: dict[str, set[str]] = {"instagram": set(), "facebook": set()}   # metrics Meta refused, per process


def _live_posts(platform: str) -> list[dict]:
    return [p for p in db.get_posts() if p["platform"] == platform and p["remote_id"]
            and p["status"] in ("uploaded", "live", "private_locked")]


def _values(d: dict) -> dict:
    vals = {}
    for m in d.get("data", []):
        if m.get("values"):
            vals[m["name"]] = m["values"][0].get("value")
        elif m.get("total_value"):
            vals[m["name"]] = m["total_value"].get("value")
    return vals


def _json(r) -> dict:
    try:
        d = r.json()
    except ValueError:
        d = {"error": {"message": f"HTTP {r.status_code}: {r.text[:200]}"}}
    return d if isinstance(d, dict) else {"error": {"message": str(d)[:200]}}


def _ok(r, d: dict) -> bool:
    return r.status_code == 200 and "error" not in d


def _metric_refused(d: dict) -> bool:
    e = d.get("error") or {}
    return e.get("code") == 100 and "metric" in str(e.get("message", "")).lower()


def _insights(platform: str, url: str, metrics: list[str], tok: str, **params) -> dict:
    """{metric: value} for one reel. When Meta refuses a metric name (#100 "...valid insights metric"),
    each metric is asked for alone: the refused ones are dropped (logged, and skipped for the rest of this
    process) and the others still land. Raises RuntimeError when the reel itself cannot be read."""
    want = [m for m in metrics if m not in _refused[platform]]
    r = meta_api.get(url, params={"metric": ",".join(want), **params, "access_token": tok}, timeout=30)
    d = _json(r)
    if _ok(r, d):
        return _values(d)
    if not _metric_refused(d) or len(want) == 1:
        raise RuntimeError(f"HTTP {r.status_code}: {(d.get('error') or {}).get('message', d)}")
    vals, refused = {}, []
    for m in want:
        r = meta_api.get(url, params={"metric": m, **params, "access_token": tok}, timeout=30)
        d = _json(r)
        if _ok(r, d):
            vals.update(_values(d))
        elif _metric_refused(d):
            refused.append(m)
        else:
            raise RuntimeError(f"HTTP {r.status_code}: {(d.get('error') or {}).get('message', d)}")
    if not vals:   # nothing at all: the reel is the problem, not the metric names
        raise RuntimeError(f"every metric was refused for this reel ({', '.join(refused)})")
    for m in refused:
        _refused[platform].add(m)
        log.warning("stats %s: Meta refuses the metric %s; it is left out from now on", platform, m)
    return vals


def fetch_youtube() -> int:
    posts = _live_posts("youtube")
    if not posts:
        return 0
    from googleapiclient.discovery import build
    from .publish_youtube import get_credentials
    creds = get_credentials()
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    ya = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    by_id = {p["remote_id"]: p for p in posts}
    ids = list(by_id)
    stats = {}
    for i in range(0, len(ids), 50):
        for it in yt.videos().list(part="statistics", id=",".join(ids[i:i + 50])).execute().get("items", []):
            s = it.get("statistics", {})
            stats[it["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                               "comments": int(s.get("commentCount", 0))}
    start = min(p["created_at"][:10] for p in posts)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    avp = {}
    for i in range(0, len(ids), 200):
        try:
            rep = ya.reports().query(ids="channel==MINE", startDate=start, endDate=today,
                                     metrics="views,averageViewPercentage", dimensions="video",
                                     filters="video==" + ",".join(ids[i:i + 200])).execute()
            for row in rep.get("rows", []):
                avp[row[0]] = float(row[2])
        except Exception as e:  # noqa: BLE001 - analytics lags ~48 h; views still count
            log.warning("YouTube Analytics query failed: %s", e)
    for vid, p in by_id.items():
        s = stats.get(vid)
        if s is None:
            continue
        db.add_stats(p["id"], views=s["views"], avg_view_pct=avp.get(vid), likes=s["likes"], comments=s["comments"],
                     raw={"statistics": s, "averageViewPercentage": avp.get(vid)})
    return len(stats)


def fetch_instagram() -> int:
    posts = _live_posts("instagram")
    tok = secret("IG_ACCESS_TOKEN")
    if not posts or not tok:
        return 0
    from .publish_instagram import base
    n = 0
    for p in posts:
        try:
            vals = _insights("instagram", f"{base()}/{p['remote_id']}/insights", IG_METRICS, tok)
        except Exception as e:  # noqa: BLE001 - one reel failing must not stop the others
            log.warning("IG insights %s failed: %s", p["remote_id"], meta_api.redact(str(e))[:300])
            continue
        ms = vals.get("ig_reels_avg_watch_time")
        skip = vals.get("reels_skip_rate")
        db.add_stats(p["id"], views=vals.get("views"), reach=vals.get("reach"), likes=vals.get("likes"),
                     comments=vals.get("comments"), shares=vals.get("shares"), saved=vals.get("saved"),
                     avg_watch_s=_seconds(ms), avg_view_pct=_watch_pct(p["run_id"], ms), raw=vals,
                     total_watch_s=_seconds(vals.get("ig_reels_video_view_total_time")),
                     skip_rate=skip if isinstance(skip, (int, float)) else None)
        n += 1
    return n


def _action(d, name: str) -> int | None:
    """One count out of a breakdown like post_video_social_actions ({"SHARE": 2, "COMMENT": 1})."""
    if not isinstance(d, dict):
        return None
    return sum(v for k, v in d.items() if str(k).lower() == name and isinstance(v, (int, float)))


def fetch_facebook() -> int:
    """Facebook Page reels (video_insights, lifetime). They are kept like the Instagram ones but are not
    in analytics.weight_platforms by default."""
    posts = _live_posts("facebook")
    tok = secret("IG_ACCESS_TOKEN")
    if not posts or not tok:
        return 0
    from .publish_instagram import base
    n = 0
    for p in posts:
        try:
            vals = _insights("facebook", f"{base('facebook')}/{p['remote_id']}/video_insights", FB_METRICS, tok,
                             period="lifetime")
        except Exception as e:  # noqa: BLE001 - one reel failing must not stop the others
            log.warning("FB insights %s failed: %s", p["remote_id"], meta_api.redact(str(e))[:300])
            continue
        social, reactions = vals.get("post_video_social_actions"), vals.get("post_video_likes_by_reaction_type")
        ms = vals.get("post_video_avg_time_watched")
        db.add_stats(p["id"], views=vals.get("fb_reels_total_plays", vals.get("blue_reels_play_count")),
                     likes=sum(reactions.values()) if isinstance(reactions, dict) else None,
                     comments=_action(social, "comment"), shares=_action(social, "share"),
                     avg_watch_s=_seconds(ms), avg_view_pct=_watch_pct(p["run_id"], ms), raw=vals,
                     total_watch_s=_seconds(vals.get("post_video_view_time")))
        n += 1
    return n


def _seconds(ms) -> float | None:
    return round(ms / 1000, 2) if isinstance(ms, (int, float)) else None


def _watch_pct(run_id: str, ms) -> float | None:
    """Average watch time (ms, as Meta gives it) as a % of the video's length."""
    try:
        dur = json.loads((run_dir(run_id) / "meta.json").read_text(encoding="utf-8"))["duration_seconds"]
        return round(100 * ms / 1000 / dur, 1)
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
        return None


def compute_weights(rows: list[dict], min_age_hours: float, shrink_n: int, retire_after: int,
                    now: datetime | None = None, key: str = "topic_id") -> dict[str, dict]:
    """rows: latest stats per post with `key` (topic_id, or hook_template), platform, views, posted_at (ISO Z).

    Views are compared WITHIN each platform first: log(views) is z-scored per platform, so one
    platform's scale cannot dominate. weight = mean z of a topic's posts older than min_age_hours,
    shrunk toward the global mean when it has fewer than shrink_n posts. A topic is retired when it
    has >= retire_after posts and every one of them is in the bottom quartile of its platform.
    The hook-template weights use the very same formula with key="hook_template"."""
    now = now or datetime.now(timezone.utc)
    ok = [r for r in rows if r.get("views") is not None and r.get(key)
          and (now - db.parse_ts(r["posted_at"])).total_seconds() >= min_age_hours * 3600]
    if not ok:
        return {}
    z, q1 = {}, {}
    for plat in {r.get("platform", "") for r in ok}:
        mine = [r for r in ok if r.get("platform", "") == plat]
        logs = np.array([math.log1p(r["views"]) for r in mine])
        mu, sd = float(logs.mean()), float(logs.std())
        for r, x in zip(mine, logs):
            z[id(r)] = (x - mu) / sd if sd > 0 else 0.0
        q1[plat] = float(np.percentile([r["views"] for r in mine], 25))
    g = float(np.mean(list(z.values())))
    out = {}
    for tid in sorted({r[key] for r in ok}):
        mine = [r for r in ok if r[key] == tid]
        n = len(mine)
        m = float(np.mean([z[id(r)] for r in mine]))
        w = m if n >= shrink_n else (n * m + (shrink_n - n) * g) / shrink_n
        retired = n >= retire_after and all(r["views"] <= q1[r.get("platform", "")] for r in mine)
        out[tid] = {"weight": round(w, 4), "n_posts": n,
                    "mean_log_views": round(float(np.mean([math.log1p(r["views"]) for r in mine])), 4),
                    "mean_z": round(m, 4), "retired": retired,
                    "retired_reason": f"{n} posts all in the bottom quartile of their platform" if retired else None}
    return out


def weight_rows() -> list[dict]:
    """Latest stats of the posts on analytics.weight_platforms (Facebook is left out by default)."""
    plats = set(get_config()["analytics"].get("weight_platforms") or ["youtube", "instagram"])
    return [r for r in db.latest_stats() if r["platform"] in plats]


def update_weights() -> dict:
    cfg = get_config()
    w = compute_weights(weight_rows(), cfg["analytics"]["min_age_hours"], cfg["analytics"]["shrink_n"],
                        cfg["selector"]["retire_after"])
    for tid, v in w.items():
        db.set_topic_weight(tid, v["weight"], v["n_posts"], v["mean_log_views"], v["retired"], v["retired_reason"])
    return w


def hook_weights() -> dict:
    """Weight per hook template, the topic-weight formula over hook_template instead of topic (posts older
    than analytics.min_age_hours, z-scored log views, shrunk toward the mean under analytics.shrink_n posts).
    Reporting only: config.yaml opening.hook_template decides which hook new reels use."""
    ac = get_config()["analytics"]
    return compute_weights(weight_rows(), ac["min_age_hours"], ac["shrink_n"], 10 ** 9, key="hook_template")


def update_hook_weights() -> dict:
    w = hook_weights()
    for tpl, v in w.items():
        db.set_hook_weight(tpl, v["weight"], v["n_posts"], v["mean_log_views"])
    return w


def collect() -> dict:
    """Pull every platform's stats into the stats table and recompute the topic and hook weights. Only
    one pull runs at a time on this machine; a second one returns at once."""
    from .lock import single_instance
    db.init()
    with single_instance("stats") as mine:
        if not mine:
            log.info("stats: another pull is running (the bot or Check stats now); this one is skipped")
            return {"skipped": "another stats pull is running"}
        return _collect()


def _collect() -> dict:
    from . import coldopen, hooks
    db.kv_set(K_LAST_RUN, db.now())
    out = {}
    try:
        hooks.backfill()   # reels made before hook_template existed
    except Exception as e:  # noqa: BLE001 - the pull itself matters more
        log.warning("hook_template backfill failed: %s", e)
    try:
        coldopen.backfill()   # reels made before cold opens existed: without one
    except Exception as e:  # noqa: BLE001 - the pull itself matters more
        log.warning("cold_open backfill failed: %s", e)
    for name, fn in (("youtube", fetch_youtube), ("instagram", fetch_instagram), ("facebook", fetch_facebook)):
        try:
            out[name] = fn()
            log.info("stats %s: %d posts updated", name, out[name])
        except Exception as e:  # noqa: BLE001 - one platform failing must not stop the other
            out[name] = f"failed: {type(e).__name__}"
            log.warning("stats %s failed: %s", name, meta_api.redact(str(e))[:300])
    w = update_weights()
    out["topics_weighted"] = len(w)
    out["hooks_weighted"] = len(update_hook_weights())
    log.info("weights updated for %d topics and %d hook templates", len(w), out["hooks_weighted"])
    try:
        coldopen.refresh_baseline()   # the saved baseline reels get their newest numbers
    except Exception as e:  # noqa: BLE001 - the pull itself matters more
        log.warning("cold open baseline refresh failed: %s", e)
    return out


def send_summary() -> None:
    db.kv_set(K_LAST_SUMMARY, db.now())
    try:
        from .approve_telegram import daily_summary_text, enabled, send_message
        if enabled():
            send_message(daily_summary_text())
        else:
            log.info("daily summary (Telegram not configured):\n%s", daily_summary_text())
    except Exception as e:  # noqa: BLE001
        log.warning("daily summary failed: %s", e)


def run_stats(summary: bool = True) -> int:
    """`run.py stats`: collect now (and send the summary)."""
    collect()
    if summary:
        send_summary()
    return 0


def _hours_since(key: str, now: datetime | None = None) -> float:
    ts = db.kv_get(key)
    if not ts:
        return float("inf")
    try:
        return ((now or datetime.now(timezone.utc)) - db.parse_ts(ts)).total_seconds() / 3600
    except ValueError:
        return float("inf")


_thread: threading.Thread | None = None
_pulled_here = False   # this process has not pulled yet: the bot pulls once as soon as it starts


def tick(now: datetime | None = None, background: bool = True) -> bool:
    """Called from the bot loop: collect once when the bot starts, then when analytics.every_hours have
    passed since the last run (kept in the database); the Telegram summary at most once per
    analytics.summary_every_hours. Runs on a background thread so the bot keeps answering, approving and
    posting; a pull that fails is logged and tried again next cycle. Returns True when a run was started."""
    global _thread, _pulled_here
    ac = get_config()["analytics"]
    if _thread and _thread.is_alive():
        return False
    if _pulled_here and _hours_since(K_LAST_RUN, now) < float(ac.get("every_hours", 6)):
        return False
    _pulled_here = True
    db.kv_set(K_LAST_RUN, db.now())   # claimed before the thread starts: the next tick does not start another

    def job():
        try:
            res = collect()
            log.info("stats (in the bot): %s", res)
            if _hours_since(K_LAST_SUMMARY, now) >= float(ac.get("summary_every_hours", 24)):
                send_summary()
        except Exception:  # noqa: BLE001 - the bot must keep running
            log.exception("stats in the bot failed")

    if not background:
        job()
        return True
    _thread = threading.Thread(target=job, name="stats", daemon=True)
    _thread.start()
    return True


def collect_now() -> bool:
    """The Stats screen's Check stats now: collect on a background thread unless a collection is running.
    Returns True when one was started. Read-only Meta calls; no summary message."""
    global _thread
    if _thread and _thread.is_alive():
        return False

    def job():
        try:
            log.info("stats (asked for on Telegram): %s", collect())
        except Exception:  # noqa: BLE001 - the bot must keep running
            log.exception("stats in the bot failed")

    _thread = threading.Thread(target=job, name="stats", daemon=True)
    _thread.start()
    return True
