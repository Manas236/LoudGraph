"""Plain-language, read-only page models. Internal identifiers stay out of page HTML."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from pipeline import db, health
from pipeline.accounts import PLATFORMS, account_states, destinations, enabled_platforms
from pipeline.config import country_by_iso3, get_config, path
from pipeline.errors import explain_error
from pipeline.topics import load_topics

LIVE = {"live", "uploaded", "private_locked"}
MAKING = {"queued": "getting data", "data_ready": "choosing countries", "picked": "writing labels",
          "labelled": "rendering", "rendered": "getting the preview ready"}


def read_json(run, name):
    try:
        return json.loads((path("out") / run["id"] / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ago(ts):
    if not ts:
        return "Never"
    seconds = max(0, (datetime.now(timezone.utc) - db.parse_ts(ts)).total_seconds())
    for word, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {word}{'s' if n != 1 else ''} ago"
    return "just now"


def status_line(telegram_configured, heartbeat, failed_count, enabled, dry_run, accounts):
    if telegram_configured and heartbeat["state"] != "ok":
        return {"tone": "red", "text": "Bot is stopped — double-click Bot.bat", "href": "/settings#advanced"}
    if failed_count:
        return {"tone": "red", "text": f"{failed_count} video{'s' if failed_count != 1 else ''} failed to post — see Library",
                "href": "/library?tab=approved&failed=1"}
    testing = [p for p in enabled if dry_run.get(p, True)]
    if enabled and len(testing) == len(enabled):
        return {"tone": "amber", "text": "Test mode — nothing is actually posted", "href": "/settings#posting"}
    if testing:
        names = ", ".join(PLATFORMS.get(p, p) for p in testing)
        return {"tone": "amber", "text": f"Test mode on for {names} — not actually posted there",
                "href": "/settings#posting"}
    count = sum(not accounts[p]["connected"] for p in enabled)
    if count:
        return {"tone": "amber", "text": f"{count} account{'s' if count != 1 else ''} not connected — Settings",
                "href": "/settings#accounts"}
    return {"tone": "green", "text": "All good", "href": "/settings#accounts"}


def status():
    states = account_states()
    failed = {p["run_id"] for p in db.get_posts() if p["status"] == "failed"}
    return status_line(states["telegram"]["configured"], health.heartbeat_state(db.kv_get("heartbeat:bot")),
                       len(failed), enabled_platforms(), get_config()["dry_run"], states)


def short_views(value):
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def video(run, topics=None, accounts=None, stats=None):
    topics = topics or {t["id"]: t for t in load_topics()}
    accounts = accounts or account_states()
    topic = topics.get(run["topic_id"], {})
    meta = read_json(run, "meta.json")
    names = country_by_iso3()
    countries = [names[c] for c in run["countries"] if c in names]
    if not countries:
        countries = [names[c["iso3"]] for c in meta.get("countries", []) if c.get("iso3") in names]
    labels = read_json(run, "labels.json")
    posts = {p["platform"]: p for p in db.get_posts(run["id"])}
    selected = [] if run["stage"] == "rejected" else destinations(run)
    stats = stats if stats is not None else db.latest_stats()
    by_platform = {s["platform"]: s for s in stats if s["run_id"] == run["id"]}
    platforms = []
    words = {"live": "live", "uploaded": "posting…", "uploading": "posting…", "pending": "waiting to post",
             "private_locked": "private — needs YouTube approval", "dry_run": "test only — not posted",
             "failed": "failed — tap to see why"}
    for p, name in PLATFORMS.items():
        post = posts.get(p)
        state = post["status"] if post else "off" if p not in selected else "waiting"
        platforms.append({"key": p, "name": name, "enabled": p in enabled_platforms(),
                          "connected": accounts[p]["connected"], "test": get_config()["dry_run"].get(p, True),
                          "selected": p in selected, "state": state, "stats": by_platform.get(p, {}),
                          "line": words.get(state, "off" if state == "off" else
                                             "not connected" if not accounts[p]["connected"] else "not posted yet"),
                          "drawer_line": "failed" if state == "failed" else None,
                          "url": post.get("url") if post else None,
                          "error": explain_error(post.get("message"), "publish", name) if post and state == "failed" else None})
    return {"ref": run["ref"], "title": run.get("title") or meta.get("title") or topic.get("title", "A new video"),
            "name": topic.get("name", topic.get("title", "A new video")), "subtitle": topic.get("subtitle", ""),
            "countries": countries, "labels": [{"country": names[c]["name"], **value} for c, value in labels.items()
                                                 if c in names and isinstance(value, dict) and value.get("label")],
            "platforms": platforms, "connected": any(p["connected"] for p in platforms if p["enabled"]),
            "has_video": (path("out") / run["id"] / "video.mp4").exists(), "age": ago(run["created_at"]),
            "replaced": bool(run.get("replaced")), "stage": run["stage"]}


def attention():
    candidates = []
    runs = db.list_runs(limit=10000)
    by_id = {r["id"]: r for r in runs}
    for post in db.get_posts():
        if post["status"] == "failed" and post["run_id"] in by_id:
            run = by_id[post["run_id"]]
            candidates.append({**explain_error(post["message"], "publish", PLATFORMS[post["platform"]]),
                               "key": f"post-{post['id']}", "version": post["updated_at"] + (post["message"] or ""),
                               "ref": run["ref"], "platform": post["platform"], "step": "publish"})
    for run in runs:
        if run["stage"] == "failed" and not any(p.get("ref") == run["ref"] for p in candidates):
            candidates.append({**explain_error(run["error"], run["failed_stage"]), "key": f"run-{run['ref']}",
                               "version": run["updated_at"] + (run["error"] or ""), "ref": run["ref"],
                               "step": run["failed_stage"], "platform": None})
    for service, state in account_states().items():
        if state["state"] in ("expired", "invalid"):  # found by a check, not an upload
            reason = "the access token expired" if state["state"] == "expired" else "the saved key was not accepted"
            candidates.append({"sentence": f"{state['name']} is not connected: {reason}.",
                               "fix": f"Follow “How to connect” for {state['name']} in Settings, then press Test.",
                               "section": "accounts", "details": state["detail"],
                               "key": f"account-{service}", "version": state["detail"], "service": service})
    selection = db.kv_get("attention:selection")
    if selection:
        obj = json.loads(selection)
        candidates.append({"sentence": obj["sentence"], "fix": "Review your topics in Settings.", "section": "topics",
                           "key": "selection", "version": obj["ts"], "details": "", "selection": True})
    for item in candidates:
        item["version"] = hashlib.sha256(item["version"].encode()).hexdigest()[:20]
        if db.kv_get("dismissed:" + item["key"]) != item["version"]:
            item["details"] = re.sub(r"r\d{8}-\d{6}-[0-9a-f]{4}", "[video]", item["details"])
            for topic in load_topics():
                item["details"] = item["details"].replace(topic["id"], topic.get("name", topic["title"]))
            return item
    return None


STALLED_S = 30 * 60  # a stage with no change and no render progress for this long has probably stopped


def activity():
    topics = {t["id"]: t for t in load_topics()}
    now = datetime.now(timezone.utc)
    out = []
    for r in db.list_runs(limit=10000):
        if r["stage"] not in MAKING:
            continue
        last = max((ts for ts in (r["updated_at"], r.get("progress_at")) if ts), key=db.parse_ts)
        out.append({"name": topics.get(r["topic_id"], {}).get("name", "New video"), "doing": MAKING[r["stage"]],
                    "progress": round(r["progress"] * 100) if r["progress"] is not None else None,
                    "stalled": ago(last) if (now - db.parse_ts(last)).total_seconds() > STALLED_S else None})
    return out


def next_video():
    from pipeline.schedule import next_time
    scheduled = next_time()
    if scheduled:
        return f"Next video is made at {scheduled:%H:%M} ({scheduled:%d %b}, {scheduled.tzname()})"
    jobs = (health._cached(health.K_SCHEDULE) or {}).get("jobs", [])
    next_job = health.next_run([j for j in jobs if "produce" in j.get("command", "")])
    if next_job and next_job.get("next"):
        from zoneinfo import ZoneInfo
        when = db.parse_ts(next_job["next"]).astimezone(ZoneInfo(get_config()["cadence"].get("timezone", "Asia/Kolkata")))
        return f"Next video is made at {when:%H:%M} ({when:%d %b}, {when.tzname()})"
    return "No schedule set"


def library_groups():
    groups = {"posted": [], "approved": [], "rejected": []}
    posts = db.get_posts()
    live_ids = {p["run_id"] for p in posts if p["status"] in LIVE}
    for run in db.list_runs(limit=10000):
        if run["id"] in live_ids:
            groups["posted"].append(run)
        elif run["stage"] in ("approved", "publishing", "published") or (run["stage"] == "failed" and run["failed_stage"] == "publish"):
            groups["approved"].append(run)
        elif run["stage"] == "rejected":
            groups["rejected"].append(run)
    return groups


def topic_rows():
    now = datetime.now(timezone.utc)
    weights = db.topic_weights()
    stats = db.latest_stats()
    out = []
    for topic in load_topics():
        runs = db.list_runs(topic_id=topic["id"], limit=10000)
        used = [r for r in runs if r["country_set"] and r["stage"] != "skipped"]
        events = db.topic_events(topic["id"])
        last = runs[0]["created_at"] if runs else None
        until = db.parse_ts(last) + timedelta(days=get_config()["topic_cooldown_days"]) if last else None
        status = "Ready"
        if until and until > now:
            status = f"Resting until {until:%d %b %Y}"
        if events:
            reason = events[0]["reason"].lower()
            if "already" in reason and "used" in reason:
                status = "Used up: no new country sets left"
            elif "low variety" in reason and until and until > now:
                status = "Skipped: every country moves the same way"
        if weights.get(topic["id"], {}).get("retired"):
            status = "Retired: low views"
        views = [s["views"] for s in stats if s["topic_id"] == topic["id"] and s["views"] is not None]
        out.append({"topic": topic, "used": len(used), "last": ago(used[0]["created_at"]) if used else "Never",
                    "average": short_views(round(sum(views) / len(views))) if views else "—", "status": status})
    return out
