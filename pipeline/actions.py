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


def approve(run_id: str, by: str, publish_now: bool = True, platforms: list[str] | None = None) -> str:
    from .accounts import enabled_platforms, ready_destinations
    chosen = enabled_platforms() if platforms is None else platforms
    if any(p not in enabled_platforms() for p in chosen):
        raise ValueError("Choose an enabled platform.")
    db.transition(run_id, "approved", f"approved via {by}", platforms=json.dumps(chosen))
    if publish_now and ready_destinations(db.get_run(run_id)):
        spawn("publish", "--run", run_id)
    return "approved"


def reject(run_id: str, by: str) -> str:
    db.transition(run_id, "rejected", f"rejected via {by}")
    return "rejected"


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


def retry_platform(run_id: str, platform: str, by: str) -> str:
    from .accounts import ready_destinations
    run = db.get_run(run_id)
    if run["stage"] == "publishing":
        raise ValueError("This video is already posting.")
    if not any(p["platform"] == platform and p["status"] == "failed" for p in db.get_posts(run_id)):
        raise ValueError("Only a failed upload can be retried.")
    if platform not in ready_destinations(run):
        raise ValueError("Connect and enable this account in Settings first.")
    db.transition(run_id, "approved", f"retry {platform} via {by}", reset=True)
    spawn("publish", "--run", run_id, "--platform", platform)
    return "Trying the upload again"


def dismiss_attention(key: str, version: str, by: str) -> str:
    db.kv_set(f"dismissed:{key}", version)
    return "Dismissed"


def test_account(service: str) -> dict:
    from . import health, publish_instagram, publish_youtube
    functions = {"youtube": publish_youtube.check, "instagram": publish_instagram.check,
                 "facebook": publish_instagram.check_facebook, "telegram": health.check_telegram,
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
