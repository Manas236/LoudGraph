"""The only operations the dashboard and the Telegram bot perform. Both call these functions."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime

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


def retry(run_id: str, from_step: str, by: str) -> str:
    if from_step not in ("fetch", "pick", "label", "render", "notify"):
        raise ValueError(f"bad step {from_step}")
    db.log(run_id, db.get_run(run_id)["stage"], f"retry from {from_step} requested via {by}")
    spawn("produce", "--run", run_id, "--from", from_step)
    return "retrying"


def publish(run_id: str, by: str) -> str:
    db.log(run_id, db.get_run(run_id)["stage"], f"publish requested via {by}")
    spawn("publish", "--run", run_id)
    return "publishing"
