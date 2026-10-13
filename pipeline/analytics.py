"""Stats (section 12): `run.py stats`, and every analytics.every_hours inside the bot's own process
(tick(), on a background thread), so it keeps running wherever the bot runs, without an OS scheduler.

YouTube Data API -> views; YouTube Analytics API -> averageViewPercentage;
Instagram media insights -> views / reach / likes / comments, plus the Reels metrics checked on
2026-10-13 (reels_skip_rate, shares, saved, total_interactions, ig_reels_avg_watch_time,
ig_reels_video_view_total_time) kept in stats.raw; avg_view_pct = average watch time / video length.
Then topic weights are recomputed (views z-scored per platform). The Telegram summary goes out at
most once per analytics.summary_every_hours.
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
IG_METRICS = "views,reach,likes,comments,ig_reels_avg_watch_time"
IG_REELS_METRICS = IG_METRICS + ",reels_skip_rate,shares,saved,total_interactions,ig_reels_video_view_total_time"
K_LAST_RUN, K_LAST_SUMMARY = "stats:last_run", "stats:last_summary"


def _live_posts(platform: str) -> list[dict]:
    return [p for p in db.get_posts() if p["platform"] == platform and p["remote_id"]
            and p["status"] in ("uploaded", "live", "private_locked")]


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
        for metrics in (IG_REELS_METRICS, IG_METRICS):   # the Reels-only metrics fail on other media types
            r = meta_api.get(f"{base()}/{p['remote_id']}/insights", params={"metric": metrics, "access_token": tok},
                             timeout=30)
            d = r.json()
            if r.status_code == 200 and "error" not in d:
                break
        if r.status_code != 200 or "error" in d:
            log.warning("IG insights %s failed: %s", p["remote_id"], d.get("error", d))
            continue
        vals = {}
        for m in d.get("data", []):
            if m.get("values"):
                vals[m["name"]] = m["values"][0].get("value")
            elif m.get("total_value"):
                vals[m["name"]] = m["total_value"].get("value")
        db.add_stats(p["id"], views=vals.get("views"), reach=vals.get("reach"), likes=vals.get("likes"),
                     comments=vals.get("comments"), avg_view_pct=_watch_pct(p["run_id"], vals), raw=vals)
        n += 1
    return n


def _watch_pct(run_id: str, vals: dict) -> float | None:
    """Average watch time as a % of the video's length (ig_reels_avg_watch_time is in ms)."""
    try:
        dur = json.loads((run_dir(run_id) / "meta.json").read_text(encoding="utf-8"))["duration_seconds"]
        return round(100 * vals["ig_reels_avg_watch_time"] / 1000 / dur, 1)
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
        return None


def compute_weights(rows: list[dict], min_age_hours: float, shrink_n: int, retire_after: int,
                    now: datetime | None = None) -> dict[str, dict]:
    """rows: latest stats per post with topic_id, platform, views, posted_at (ISO Z).

    Views are compared WITHIN each platform first: log(views) is z-scored per platform, so one
    platform's scale cannot dominate. weight = mean z of a topic's posts older than min_age_hours,
    shrunk toward the global mean when it has fewer than shrink_n posts. A topic is retired when it
    has >= retire_after posts and every one of them is in the bottom quartile of its platform."""
    now = now or datetime.now(timezone.utc)
    ok = [r for r in rows if r.get("views") is not None
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
    for tid in sorted({r["topic_id"] for r in ok}):
        mine = [r for r in ok if r["topic_id"] == tid]
        n = len(mine)
        m = float(np.mean([z[id(r)] for r in mine]))
        w = m if n >= shrink_n else (n * m + (shrink_n - n) * g) / shrink_n
        retired = n >= retire_after and all(r["views"] <= q1[r.get("platform", "")] for r in mine)
        out[tid] = {"weight": round(w, 4), "n_posts": n,
                    "mean_log_views": round(float(np.mean([math.log1p(r["views"]) for r in mine])), 4),
                    "mean_z": round(m, 4), "retired": retired,
                    "retired_reason": f"{n} posts all in the bottom quartile of their platform" if retired else None}
    return out


def update_weights() -> dict:
    cfg = get_config()
    w = compute_weights(db.latest_stats(), cfg["analytics"]["min_age_hours"], cfg["analytics"]["shrink_n"],
                        cfg["selector"]["retire_after"])
    for tid, v in w.items():
        db.set_topic_weight(tid, v["weight"], v["n_posts"], v["mean_log_views"], v["retired"], v["retired_reason"])
    return w


def collect() -> dict:
    """Pull every platform's stats into the stats table and recompute topic weights."""
    db.init()
    db.kv_set(K_LAST_RUN, db.now())
    out = {}
    for name, fn in (("youtube", fetch_youtube), ("instagram", fetch_instagram)):
        try:
            out[name] = fn()
            log.info("stats %s: %d posts updated", name, out[name])
        except Exception as e:  # noqa: BLE001 - one platform failing must not stop the other
            out[name] = f"failed: {type(e).__name__}"
            log.warning("stats %s failed: %s", name, e)
    w = update_weights()
    out["topics_weighted"] = len(w)
    log.info("topic weights updated for %d topics", len(w))
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


def tick(now: datetime | None = None, background: bool = True) -> bool:
    """Called from the bot loop: collect when analytics.every_hours have passed since the last run
    (kept in the database, so a restart neither repeats nor skips it); the Telegram summary at most
    once per analytics.summary_every_hours. Runs on a background thread so the bot keeps answering.
    Returns True when a run was started."""
    global _thread
    ac = get_config()["analytics"]
    if (_thread and _thread.is_alive()) or _hours_since(K_LAST_RUN, now) < float(ac.get("every_hours", 6)):
        return False
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
