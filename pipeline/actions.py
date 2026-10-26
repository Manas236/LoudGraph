"""The only operations the dashboard and the Telegram bot perform. Both call these functions."""
from __future__ import annotations

import logging
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from . import db
from .config import ROOT, path, get_config

log = logging.getLogger(__name__)


def spawn(*args: str) -> int:
    """Run `python run.py <args>` in the background (detached) and return its pid."""
    logf = path("cache") / "logs" / f"job-{datetime.now():%Y%m%d-%H%M%S}-{args[0]}.log"
    logf.parent.mkdir(parents=True, exist_ok=True)
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kw["start_new_session"] = True
    with open(logf, "ab") as f:
        p = subprocess.Popen([sys.executable, str(ROOT / "run.py"), *args], cwd=ROOT, stdout=f, stderr=f,
                             stdin=subprocess.DEVNULL, **kw)
    log.info("spawned %s (pid %s), log %s", args, p.pid, logf)
    return p.pid


def approve(run_id: str, by: str, publish_now: bool = False, platforms: list[str] | None = None) -> str:
    """Approve a reel: it joins the back of the posting queue (posting.py's daily slots), or takes back
    the place it had before it was sent back for a fresh approval (review.back_to_review keep_place).
    publish_now=True posts it at once instead (Post now)."""
    from .accounts import enabled_platforms
    from .posting import next_pos, post_now
    chosen = enabled_platforms() if platforms is None else platforms
    if any(p not in enabled_platforms() for p in chosen):
        raise ValueError("Choose an enabled platform.")
    run = db.get_run(run_id)
    prior = run.get("prior_post_pos") if run else None
    place = f"back in its place ({prior:g})" if prior is not None else "in the posting queue"
    # approved_by marks an approval made by this code: the publisher refuses to post live without it
    db.transition(run_id, "approved", f"approved via {by}: {place}", platforms=json.dumps(chosen),
                  approved_by=f"{by} {db.now()}", approved_at=db.now(),
                  post_pos=prior if prior is not None else next_pos(), prior_post_pos=None)
    if publish_now:
        post_now(run_id, by)
    return "approved"


def reject(run_id: str, by: str) -> str:
    db.transition(run_id, "rejected", f"rejected via {by}")
    return "rejected"


def send_to_telegram(run_id: str, by: str) -> str:
    """The dashboard's "Send to Telegram for approval": the bot shows this video on its review card
    now, and it is approved (or not) there. Nothing is posted by this."""
    from . import health, review
    from .accounts import account_states, telegram_switch
    if not account_states()["telegram"]["configured"]:
        raise ValueError("Telegram is not connected. Set it up in Settings first.")
    if not telegram_switch():
        raise ValueError("Telegram is switched off. Turn it on in the sidebar first.")
    message = review.send_to_telegram(run_id, by)
    if health.heartbeat_state(db.kv_get("heartbeat:bot"))["state"] != "ok":
        return "Sent to Telegram — the bot is stopped, so double-click Bot.bat and the card will arrive."
    return message


def regenerate(run_id: str, by: str) -> str:
    """Same topic, new country set: reject the current run (if it is waiting) and start a new one."""
    run = db.get_run(run_id)
    if run["stage"] == "awaiting_approval":
        db.transition(run_id, "rejected", f"replaced via {by} (remake requested)", replaced=1)
    new_id = db.create_run(run["topic_id"], regen_of=run_id)
    db.log(run_id, db.get_run(run_id)["stage"], f"regenerate via {by}: new run {new_id}")
    spawn("produce", "--run", new_id, "--from", "fetch")
    return new_id


STEPS = ("fetch", "pick", "label", "render", "notify")
RETRY_STEPS = STEPS + ("publish",)
MAKE_GUARD_S = 60


def make_video(by: str) -> str:
    """The 'Make a video now' button: the same as `python run.py produce --count 1`, in the background."""
    last = db.kv_get("make_video_at")
    if last and (datetime.now(timezone.utc) - db.parse_ts(last)).total_seconds() < MAKE_GUARD_S:
        raise ValueError("a video was started less than a minute ago; it will appear on the board")
    db.kv_set("make_video_at", db.now())
    pid = spawn("produce", "--count", "1")
    log.info("make video requested via %s (pid %s)", by, pid)
    return "Making a video — it will appear in Review shortly."


BATCH_SIZES = (3, 5, 7)


def make_batch(count: int, by: str) -> str:
    """The dashboard's Make a batch: the same topics Telegram's Make a batch would choose, queued in the
    make queue and made one at a time, by the bot when it runs, else by `run.py batch` in the background."""
    from . import ideas, makequeue
    from .lock import held_elsewhere
    if count not in BATCH_SIZES:
        raise ValueError("Choose 3, 5 or 7 videos.")
    picks = ideas.pick_batch(count)
    if not picks:
        raise ValueError("No topic is free right now. Turn more topics on in Settings.")
    makequeue.start_batch([t["id"] for t in picks])
    log.info("batch of %d requested via %s", len(picks), by)
    names = ", ".join(t["name"] for t in picks)
    if held_elsewhere("bot"):
        return f"Batch of {len(picks)} queued ({names}). The bot makes them one at a time."
    spawn("batch")   # a second `run.py batch` exits at once and the running one takes these too
    return f"Making {len(picks)} videos one at a time: {names}."


STATS_GUARD_S = 120


def refresh_stats(by: str) -> str:
    """Check stats now: `run.py stats --no-summary` in the background (read-only calls to the platforms)."""
    last = db.kv_get("stats_refresh_at")
    if last and (datetime.now(timezone.utc) - db.parse_ts(last)).total_seconds() < STATS_GUARD_S:
        raise ValueError("Stats are already being checked. Views update in a minute or two.")
    db.kv_set("stats_refresh_at", db.now())
    pid = spawn("stats", "--no-summary")
    log.info("stats refresh requested via %s (pid %s)", by, pid)
    return "Checking stats — views in Library update in a minute or two."


def set_telegram(enabled: bool, by: str) -> str:
    """The dashboard's Telegram switch. Off: the bot stops listening and clears its buttons from the chat,
    but still makes and posts reels. On: it picks up again (taps made while off are dropped)."""
    from . import health
    from .accounts import account_states
    from .settings import set_telegram as save
    if enabled and not account_states()["telegram"]["configured"]:
        raise ValueError("Telegram is not set up yet. Follow “How to connect” for Telegram in Settings.")
    save(enabled)
    log.info("telegram switched %s via %s", "on" if enabled else "off", by)
    running = health.heartbeat_state(db.kv_get("heartbeat:bot"))["state"] == "ok"
    if not enabled:
        return "Telegram is off — the bot leaves your chat alone. Reels are still made and posted."
    return "Telegram is on — review cards and notes go to your phone again." + (
        "" if running else " Double-click Bot.bat to start the bot.")


def retry(run_id: str, from_step: str, by: str) -> str:
    """Re-run a run in place from a stage. Refused for published runs / runs with live posts.
    from_step="publish" re-publishes a run whose publish failed (platforms already done are skipped)."""
    from .orchestrator import rerun_blocker
    if from_step not in RETRY_STEPS:
        raise ValueError(f"bad step {from_step}")
    if from_step == "publish":
        run = db.get_run(run_id)
        if run["stage"] != "failed" or run["failed_stage"] != "publish":
            raise ValueError("retry from publish is only for a run whose publish failed")
        db.transition(run_id, "approved", f"retry publish requested via {by}", reset=True)
        db.set_post_slot(run_id, None)   # a retry is not a queue slot
        spawn("publish", "--run", run_id)
        return "publishing again"
    why = rerun_blocker(run_id)
    if why:
        raise ValueError(f"refused: {why}. Use 're-render as new run' instead.")
    db.log(run_id, db.get_run(run_id)["stage"], f"retry from {from_step} requested via {by}")
    spawn("produce", "--run", run_id, "--from", from_step)
    return "retrying"


def rerender_as_new(run_id: str, from_step: str, by: str) -> str:
    """Copy the run's inputs (up to from_step) into a new run and produce that one."""
    if from_step not in STEPS:
        raise ValueError(f"bad step {from_step}")
    db.log(run_id, db.get_run(run_id)["stage"], f"re-render as new run from {from_step} requested via {by}")
    spawn("produce", "--run", run_id, "--from", from_step, "--as-new")
    return "re-rendering as a new run (see the board)"


def publish(run_id: str, by: str) -> str:
    from .accounts import ready_destinations
    run = db.get_run(run_id)
    if not run or run["stage"] != "approved":
        raise ValueError("Only an approved video can be posted.")
    if not ready_destinations(run):
        return "Approved, not posted — connect an account in Settings."
    db.log(run_id, db.get_run(run_id)["stage"], f"publish requested via {by}")
    spawn("publish", "--run", run_id)
    return "publishing"


def edit_title(run_id: str, title: str, by: str) -> str:
    """Serialize editing with approval; publishers read the atomically replaced metadata file."""
    title = " ".join(title.split())
    if not title or len(title) > 100:
        raise ValueError("Use a title between 1 and 100 characters.")
    target = path("out") / run_id / "meta.json"
    with db.db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["stage"] != "awaiting_approval":
                raise ValueError("Only a video waiting for review can be edited.")
            meta = json.loads(target.read_text(encoding="utf-8"))
            old = meta.get("title", "")
            meta["title"] = title
            for key in ("description_youtube", "description_instagram"):
                if key in meta:
                    meta[key] = title + meta[key][len(old):] if old and meta[key].startswith(old) else title + "\n\n" + meta[key]
            temp = target.with_suffix(".json.tmp")
            temp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(target)
            c.execute("UPDATE runs SET title=? WHERE id=?", (title, run_id))
            c.execute("INSERT INTO stage_log(run_id,stage,message,ts) VALUES (?,'awaiting_approval',?,?)",
                      (run_id, f"title edited via {by}", db.now()))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return "Title saved"


CAPTION_STAGES = ("awaiting_approval", "approved")   # the caption can change until posting starts


def _caption_run(run_id: str) -> dict:
    run = db.get_run(run_id)
    if not run or run["stage"] not in CAPTION_STAGES:
        raise ValueError("The caption can only be changed before the video is posted.")
    return run


def edit_caption(run_id: str, text: str, hashtags, by: str) -> dict:
    """The owner's own caption (dashboard or Telegram). hashtags: a list, a space/comma separated string,
    or None to take them from the text's last lines (keeping the current ones when there are none).
    Raises ValueError (captions.CaptionRejected) with the reasons; returns {"message", "warnings"}."""
    from . import captions
    run = _caption_run(run_id)
    res = captions.save_edit(run_id, text, hashtags, by)
    db.log(run_id, run["stage"], f"caption edited via {by}" + (f"; warnings: {'; '.join(res['warnings'])}"
                                                               if res["warnings"] else ""))
    message = "Caption saved. The data credit is added when it posts."
    if res["warnings"]:
        message += " " + " ".join(res["warnings"])
    return {"message": message, "warnings": res["warnings"]}


def choose_caption(run_id: str, index: int, by: str) -> str:
    from . import captions
    run = _caption_run(run_id)
    captions.choose(run_id, index)
    db.log(run_id, run["stage"], f"caption option {index + 1} chosen via {by}")
    return f"Caption {index + 1} chosen."


def retry_platform(run_id: str, platform: str, by: str) -> str:
    """Post one platform again. Refused while a publisher for this run is still running, so a
    slow upload is never doubled; an upload whose publisher died counts as failed."""
    from .accounts import ready_destinations
    from .lock import held_elsewhere, publish_lock
    run = db.get_run(run_id)
    if held_elsewhere(publish_lock(run_id)):
        raise ValueError("This video is still posting. Wait for it to finish, then try again.")
    post = {p["platform"]: p for p in db.get_posts(run_id)}.get(platform)
    status = post["status"] if post else None
    if status in ("live", "uploaded", "private_locked"):
        raise ValueError("This video is already posted there.")
    died = status in (None, "uploading", "pending") and bool(run.get("approved_by")) and \
        run["stage"] in ("approved", "publishing", "published", "failed")
    if status != "failed" and not died:
        raise ValueError("Only a failed upload can be retried.")
    if platform not in ready_destinations(run):
        raise ValueError("Connect and enable this account in Settings first.")
    db.transition(run_id, "approved", f"retry {platform} via {by}", reset=True)
    db.set_post_slot(run_id, None)   # a retry is not a queue slot
    spawn("publish", "--run", run_id, "--platform", platform)
    return "Trying the upload again"


def dismiss_attention(key: str, version: str, by: str) -> str:
    db.kv_set(f"dismissed:{key}", version)
    return "Dismissed"


def test_account(service: str, force: bool = False) -> dict:
    """The Settings page Test button (read-only). force=True also checks a platform that is off."""
    from . import health, publish_instagram, publish_youtube
    functions = {"youtube": publish_youtube.check, "instagram": publish_instagram.check,
                 "facebook": lambda: publish_instagram.check_facebook(force=force), "telegram": health.check_telegram,
                 "gemini": health.check_gemini}
    if service not in functions:
        raise ValueError("Unknown service")
    from .accounts import SERVICES
    try:
        result = {"name": SERVICES[service], **functions[service]()}
    except Exception as e:
        result = {"name": SERVICES[service], "state": "error", "level": "FAIL", "detail": str(e)}
    cached = health._cached(health.K_CREDS) or {"items": []}
    cached["items"] = [r for r in cached["items"] if r["name"] != SERVICES[service]] + [result]
    cached["checked_at"] = db.now()
    db.kv_set(health.K_CREDS, json.dumps(cached))
    return result


def save_posting(values: dict) -> str:
    from .settings import save_posting as save
    save(values)
    return "Posting settings saved"


def toggle_topic(topic_id: str, enabled: bool) -> str:
    from .settings import toggle_topic as save
    save(topic_id, enabled)
    return "Topic settings saved"
