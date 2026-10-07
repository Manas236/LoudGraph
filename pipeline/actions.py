"""The only operations the dashboard and the Telegram bot perform. Both call these functions."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

from . import db
from .config import ROOT, path

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


def approve(run_id: str, by: str, publish_now: bool = True) -> str:
    db.transition(run_id, "approved", f"approved via {by}")
    if publish_now:
        spawn("publish", "--run", run_id)
    return "approved"


def reject(run_id: str, by: str) -> str:
    db.transition(run_id, "rejected", f"rejected via {by}")
    return "rejected"


def regenerate(run_id: str, by: str) -> str:
    """Same topic, new country set: reject the current run (if it is waiting) and start a new one."""
    run = db.get_run(run_id)
    if run["stage"] == "awaiting_approval":
        db.transition(run_id, "rejected", f"rejected via {by} (regenerate requested)")
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
    return "started: the new run appears under 'Data secured' in a few seconds"


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
    db.log(run_id, db.get_run(run_id)["stage"], f"publish requested via {by}")
    spawn("publish", "--run", run_id)
    return "publishing"
