"""The make queue: topics waiting to become reels, made ONE AT A TIME (this runs on a laptop).

A job is one topic: queued -> running (its run is being made by `run.py produce --run`) -> ready (it
reached review), skipped (the data was not interesting enough) or failed. The Telegram bot works the
queue on every tick (work()); `run.py batch` works it in the foreground when the bot is not running.
A job is claimed atomically before it starts, so two workers never make the same job twice.

Batches: Make a batch shows N topics on the home card first (pending_batch); Start queues them with one
batch id. A batch topic that is skipped is replaced by another distinct topic (ideas.pick_batch, never
the same dataset as the reel before it), so the batch still ends with N reels unless the ideas run out.
A failure is not replaced: it waits on the home card with Retry and Drop.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import db

log = logging.getLogger(__name__)

MAKING = ("queued", "data_ready", "picked", "labelled", "rendered")
DONE = ("ready", "skipped", "failed")
START_GRACE_S = 120            # a just-spawned maker gets this long to take its run lock
K_PREVIEW = "batch:preview"    # the batch shown on the home card, not started yet


def _age(ts: str | None) -> float:
    return (datetime.now(timezone.utc) - db.parse_ts(ts)).total_seconds() if ts else float("inf")


# ------------------------------------------------------------------ jobs

def add_job(topic_id: str, source: str = "pick", batch_id: int | None = None) -> dict:
    ts = db.now()
    with db.db() as c:
        jid = c.execute("INSERT INTO make_jobs(topic_id, state, random, source, batch_id, created_at, updated_at) "
                        "VALUES (?,'queued',?,?,?,?,?)",
                        (topic_id, int(source == "surprise"), source, batch_id, ts, ts)).lastrowid
    return get_job(jid)


def get_job(job_id: int) -> dict | None:
    with db.db() as c:
        r = c.execute("SELECT * FROM make_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def update_job(job_id: int, **fields) -> None:
    fields["updated_at"] = db.now()
    with db.db() as c:
        c.execute(f"UPDATE make_jobs SET {', '.join(k + '=?' for k in fields)} WHERE id=?", [*fields.values(), job_id])


def jobs(*states: str) -> list[dict]:
    with db.db() as c:
        rows = c.execute(f"SELECT * FROM make_jobs WHERE state IN ({','.join('?' * len(states))}) ORDER BY id",
                         states).fetchall()
    return [dict(r) for r in rows]


def job_for_run(run_id: str) -> dict | None:
    with db.db() as c:
        r = c.execute("SELECT * FROM make_jobs WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    return dict(r) if r else None


def batch_jobs(batch_id: int) -> list[dict]:
    with db.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM make_jobs WHERE batch_id=? ORDER BY id", (batch_id,))]


# ------------------------------------------------------------------ batches

def pending_batch() -> list[str]:
    try:
        return json.loads(db.kv_get(K_PREVIEW) or "[]")
    except ValueError:
        return []


def set_pending_batch(topic_ids: list[str] | None) -> None:
    db.kv_set(K_PREVIEW, json.dumps(topic_ids or []))


def start_batch(topic_ids: list[str]) -> int:
    """Queue the batch's topics in order, under one new batch id."""
    with db.db() as c:
        bid = (c.execute("SELECT MAX(batch_id) FROM make_jobs").fetchone()[0] or 0) + 1
    for tid in topic_ids:
        add_job(tid, source="batch", batch_id=bid)
    set_pending_batch(None)
    log.info("batch %s queued: %s", bid, ", ".join(topic_ids))
    return bid


def active_batch() -> dict | None:
    """The newest batch that still has work queued or running: {"id", "asked", "ready"}."""
    live = [j for j in jobs("queued", "running") if j["batch_id"]]
    if not live:
        return None
    bid = max(j["batch_id"] for j in live)
    js = batch_jobs(bid)
    return {"id": bid, "asked": sum(1 for j in js if j["source"] == "batch"),
            "ready": sum(1 for j in js if j["state"] == "ready")}


def replace(job: dict) -> dict | None:
    """A skipped batch topic makes way for another distinct one, at the back of the queue."""
    from .ideas import pick_batch
    from .topics import get_topic
    js = batch_jobs(job["batch_id"])
    asked = sum(1 for j in js if j["source"] == "batch")
    if sum(1 for j in js if j["source"] == "replacement") >= max(3, asked):
        log.info("batch %s: no more replacements (limit reached)", job["batch_id"])
        return None
    waiting = jobs("queued", "running")
    last = waiting[-1]["topic_id"] if waiting else job["topic_id"]
    try:
        after = get_topic(last)
    except KeyError:
        after = None
    counts: dict[str, int] = {}   # the batch's categories that are made or on their way
    for j in js:
        if j["state"] in ("queued", "running", "ready"):
            try:
                cat = get_topic(j["topic_id"]).get("category")
            except KeyError:
                continue
            counts[cat] = counts.get(cat, 0) + 1
    picked = pick_batch(1, exclude={j["topic_id"] for j in js}, after=after, counts=counts)
    if not picked:
        log.info("batch %s: no other topic to replace %s with", job["batch_id"], job["topic_id"])
        return None
    new = add_job(picked[0]["id"], source="replacement", batch_id=job["batch_id"])
    update_job(job["id"], dismissed=1)   # replaced, so not a notice on the home card
    log.info("batch %s: %s was skipped, replaced by %s", job["batch_id"], job["topic_id"], new["topic_id"])
    return new


# ------------------------------------------------------------------ working the queue

def making_alive(run: dict, started_at: str | None = None) -> bool:
    from .lock import held_elsewhere, run_lock
    return held_elsewhere(run_lock(run["id"])) or _age(started_at or run["updated_at"]) < START_GRACE_S


def making_now() -> list[dict]:
    """Runs being made right now by any process (it holds the run lock), e.g. a dashboard remake."""
    from .lock import held_elsewhere, run_lock
    return [r for stage in MAKING for r in db.list_runs(stage=stage) if held_elsewhere(run_lock(r["id"]))]


def busy() -> bool:
    return bool(jobs("running") or making_now())


def claim(job: dict) -> str | None:
    """Atomically take a queued job and create its run; None when another worker took it first."""
    with db.db() as c:
        taken = c.execute("UPDATE make_jobs SET state='running', updated_at=? WHERE id=? AND state='queued'",
                          (db.now(), job["id"])).rowcount == 1
    if not taken:
        return None
    rid = db.create_run(job["topic_id"])
    update_job(job["id"], run_id=rid)
    return rid


def settle(job: dict) -> str:
    """Follow a running job: when its run has left the making stages, the job is ready, skipped or
    failed (a skipped batch job is replaced). Returns the job's state."""
    run = db.get_run(job["run_id"]) if job["run_id"] else None
    if not run:
        update_job(job["id"], state="failed")
        return "failed"
    if run["stage"] in MAKING:
        if making_alive(run, started_at=job["updated_at"]):
            return "running"
        db.fail(run["id"], run["stage"], "the video maker stopped unexpectedly")
        run = db.get_run(run["id"])
    state = {"skipped": "skipped", "failed": "failed"}.get(run["stage"], "ready")
    update_job(job["id"], state=state)
    if state == "skipped" and job["batch_id"]:
        replace(get_job(job["id"]))
    return state


def work(spawn: bool = True) -> None:
    """One step: settle the running job, then start the next queued one when nothing is being made.
    spawn=True starts `run.py produce` in the background (the bot); False makes it here and waits."""
    from . import actions
    for job in jobs("running"):
        settle(job)
    if busy():
        return
    for job in jobs("queued"):
        rid = claim(job)
        if not rid:
            continue
        if spawn:
            try:
                actions.spawn("produce", "--run", rid, "--from", "fetch")
            except Exception as e:  # noqa: BLE001
                db.fail(rid, "fetch", f"could not start the video maker: {e}")
                settle(get_job(job["id"]))
        else:
            from .orchestrator import run_pipeline
            run_pipeline(rid)
            settle(get_job(job["id"]))
        return


def run_foreground(on_done=None) -> None:
    """Work the queue in this process until nothing is queued or running (`run.py batch`).
    on_done(job) is called after each job settles."""
    import time
    reported = {j["id"] for j in jobs(*DONE)}
    while jobs("queued", "running"):
        work(spawn=False)
        for j in jobs(*DONE):
            if j["id"] not in reported:
                reported.add(j["id"])
                if on_done:
                    on_done(j)
        if jobs("running") and busy():
            time.sleep(5)   # another process (e.g. the bot) is making the running job: wait for it


# ------------------------------------------------------------------ the home card's Retry / Drop

def retry(topic_id: str) -> dict:
    return add_job(topic_id, source="retry")


def dismiss_run(run_id: str) -> None:
    job = job_for_run(run_id)
    if job:
        update_job(job["id"], dismissed=1)
