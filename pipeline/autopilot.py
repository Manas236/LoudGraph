"""Autopilot: the channel runs with no human input. AUTOPILOT=true in .env (the default when it is missing).

Reels go ready -> pool -> picker -> post: the daily generation run tops up the pool of ready reels (generator.py),
and at each slot time the picker (autopick.py) chooses one by its breakout model (learn.py) and posts it. Nothing is
approved by a person and no Telegram bot listens: alerts (alerts.py) go out as direct sendMessage calls only. The
approval code (review card, posting queue, approve_telegram.py) stays in place, unreachable while AUTOPILOT is on;
AUTOPILOT=false brings it back.

Jobs: `python -m pipeline generate | post --slot 1|2 | insights | health`, run by systemd timers on the server
(deploy/). Each job:
  * refuses to run while the MIGRATED_TO_SERVER file exists (paths.migrated_marker): the laptop the channel moved
    away from can never post, alert or generate again, whatever is double-clicked there;
  * holds one cross-platform file lock (filelock) shared by every job, so generate, post and insights never overlap;
  * on a crash: one alert, and a healthchecks.io /fail ping.

`run.py bot` with AUTOPILOT on runs loop(): the same jobs at the same times from one long-running process, for a
machine without systemd timers (Bot.bat on Windows). Each job runs as its own `python -m pipeline <job>` process.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import alerts, db
from .config import ROOT, get_config, path, secret

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
LOCK_NAME = "jobs"
LOCK_WAIT_S = {"generate": 3600, "post": 45 * 60, "insights": 3600, "health": 3600}
REFUSED = 3   # exit code of a job refused by the MIGRATED_TO_SERVER marker


def on() -> bool:
    """AUTOPILOT in .env: on unless it says false / 0 / no / off."""
    v = (secret("AUTOPILOT") or "true").strip().lower()
    return v not in ("false", "0", "no", "off")


def settings() -> dict:
    return dict(get_config().get("autopilot") or {})


def marker():
    return ROOT / get_config()["paths"].get("migrated_marker", "MIGRATED_TO_SERVER")


def migrated() -> bool:
    """The channel moved to a server: this machine must never post (or alert, or generate) again."""
    return marker().exists()


MIGRATED_TEXT = ("MIGRATED_TO_SERVER exists in this folder: Graphony runs on the server now, so nothing is posted "
                 "from this machine. Delete that file only if the server is shut down for good.")


class JobLockTimeout(RuntimeError):
    pass


def job_lock(job: str, wait_s: float | None = None):
    """The one lock every autopilot job holds (cache/locks/jobs.lock, released by the OS if the process dies)."""
    from filelock import FileLock
    f = path("cache") / "locks" / f"{LOCK_NAME}.lock"
    f.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(f), timeout=LOCK_WAIT_S.get(job, 3600) if wait_s is None else wait_s)


def run_job(job: str, fn, *args, lock_wait_s: float | None = None, **kwargs) -> int:
    """Run one job: the MIGRATED_TO_SERVER refusal, the job lock, and on any crash an alert and a /fail ping.
    fn returns an exit code (0 = fine). A job that ends normally clears its own crash alert."""
    from filelock import Timeout
    if migrated():
        log.warning("%s refused: %s", job, MIGRATED_TEXT)
        print(MIGRATED_TEXT)
        return REFUSED
    db.init()
    try:
        with job_lock(job, lock_wait_s):
            code = fn(*args, **kwargs) or 0
    except Timeout:
        waited = LOCK_WAIT_S.get(job, 3600) if lock_wait_s is None else lock_wait_s
        log.error("%s: another Graphony job held the job lock for %.0f min", job, waited / 60)
        alerts.fire(f"crash:{job}", f"the {job} job could not start: another job held the lock for {waited / 60:.0f} min",
                    "a stuck job: systemctl list-timers 'graphony*' and journalctl -u 'graphony-*' on the server")
        alerts.ping(ok=False)
        return 1
    except Exception as e:  # noqa: BLE001 - every crash becomes one alert, then the job ends
        log.exception("%s job crashed", job)
        alerts.fire(f"crash:{job}", f"the {job} job crashed ({type(e).__name__}: {str(e)[:160]})",
                    f"journalctl -u graphony-{job.split()[0]} on the server, and cache/logs/pipeline.log")
        alerts.ping(ok=False)
        return 1
    alerts.clear(f"crash:{job}")
    return code


# ------------------------------------------------------------------ the IST clock of the jobs

def ist(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(IST)


def slot_times() -> list[str]:
    """The two posting slots, "14:30" and "16:00" (posting.time), in order."""
    from .posting import times
    return [t.strftime("%H:%M") for t in times()]


# ------------------------------------------------------------------ one process instead of systemd timers

SCHEDULE = [("generate", "03:00"), ("health", "09:00"), ("post 1", "14:30"), ("post 2", "16:00")]
INSIGHTS_EVERY_H = 6


def due_jobs(now: datetime | None = None) -> list[tuple[str, str]]:
    """(job, kv key) of every job that is due and was not started yet: each daily job once a day from its time (IST),
    within the late-slot window for the posts; insights every 6 h. The kv key is claimed before the job starts."""
    local = ist(now)
    out = []
    slots = slot_times()
    late = timedelta(minutes=float(settings().get("late_minutes", 90)))
    for job, hhmm in SCHEDULE:
        if job.startswith("post"):
            hhmm = slots[int(job.split()[1]) - 1]
        h, m = map(int, hhmm.split(":"))
        at = local.replace(hour=h, minute=m, second=0, microsecond=0)
        if local < at or (job.startswith("post") and local - at > late):
            continue
        key = f"autopilot:loop:{job}:{local.date().isoformat()}"
        if not db.kv_get(key):
            out.append((job, key))
    block = local.replace(hour=(local.hour // INSIGHTS_EVERY_H) * INSIGHTS_EVERY_H, minute=0, second=0, microsecond=0)
    key = f"autopilot:loop:insights:{block:%Y-%m-%dT%H}"
    if not db.kv_get(key):
        out.append(("insights", key))
    return out


def _claim(key: str) -> bool:
    with db.db() as c:
        return c.execute("INSERT OR IGNORE INTO kv(key, value, updated_at) VALUES (?,?,?)",
                         (key, "started", db.now())).rowcount == 1


def loop(once: bool = False, runner=None) -> int:
    """`run.py bot` with AUTOPILOT on: start each due job as `python -m pipeline <job>`, one at a time. No Telegram
    polling. runner(argv) replaces subprocess.run (tests)."""
    if migrated():
        log.warning("autopilot loop refused: %s", MIGRATED_TEXT)
        print(MIGRATED_TEXT)
        return REFUSED
    db.init()
    run = runner or (lambda argv: subprocess.run(argv, cwd=ROOT).returncode)
    print("Graphony autopilot is running: it makes, picks and posts reels by itself. Keep this window open.")
    from . import health
    while True:
        try:
            health.beat("bot", autopilot=True)
            for job, key in due_jobs():
                if _claim(key):
                    argv = [sys.executable, "-m", "pipeline", *job.split()]
                    if job.startswith("post"):
                        argv = [sys.executable, "-m", "pipeline", "post", "--slot", job.split()[1]]
                    log.info("autopilot loop: starting %s", " ".join(argv[2:]))
                    run(argv)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001 - keep the long-running loop alive
            log.warning("autopilot loop error: %s", e)
        if once:
            return 0
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            return 0
