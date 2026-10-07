"""Health: the per-credential checks shared by `run.py doctor` and the dashboard's health panel,
the bot heartbeat, and the OS-scheduled runs.

The credential checks call the real services, so the dashboard runs them in a background thread
at most every CHECK_EVERY_S and shows the result cached in the `kv` table.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone

from . import db
from .config import get_config, secret

log = logging.getLogger(__name__)

HEARTBEAT_EVERY_S = 30      # the bot beats at least once a minute (its long poll is 50 s)
HEARTBEAT_STALE_S = 180     # older than 3 min = the bot is down or stuck
CHECK_EVERY_S = 900
K_CREDS, K_SCHEDULE = "health:credentials", "health:schedule"


def _res(level: str, state: str, detail: str) -> dict:
    return {"level": level, "state": state, "detail": detail}


# ------------------------------------------------------------------ credentials

def check_gemini() -> dict:
    if not secret("GEMINI_API_KEY"):
        return _res("WARN", "missing", "GEMINI_API_KEY missing: videos will have no turning-point labels")
    from .labels import resolve_model
    try:
        return _res("OK", "ok", f"valid, model {resolve_model(refresh=True)}")
    except Exception as e:  # noqa: BLE001
        bad_key = "API key" in str(e) or "HTTP 400" in str(e) or "HTTP 403" in str(e)
        return _res("FAIL", "invalid" if bad_key else "error", f"GEMINI_API_KEY present but failed: {e}"[:300])


def check_telegram() -> dict:
    if not (secret("TELEGRAM_BOT_TOKEN") and secret("TELEGRAM_CHAT_ID")):
        return _res("WARN", "missing", "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing: approval via dashboard only")
    from .approve_telegram import check
    ok, detail = check()
    if ok:
        return _res("OK", "ok", detail)
    return _res("FAIL", "invalid" if ("401" in detail or "Unauthorized" in detail) else "error", detail)


def credential_checks() -> list[dict]:
    """One entry per integration: name, level (OK/WARN/FAIL for doctor), state
    (ok/missing/expired/invalid/error/disabled for the dashboard) and detail."""
    from .publish_instagram import check as ig_check, check_facebook
    from .publish_youtube import check as yt_check
    out = []
    for name, fn in (("Gemini", check_gemini), ("Telegram", check_telegram), ("YouTube", yt_check),
                     ("Instagram", ig_check), ("Facebook", check_facebook)):
        try:
            out.append({"name": name, **fn()})
        except Exception as e:  # noqa: BLE001 - a check must report, not crash
            out.append({"name": name, **_res("FAIL", "error", f"{type(e).__name__}: {e}"[:300])})
    return out


# ------------------------------------------------------------------ heartbeat

def beat(name: str = "bot", **info) -> None:
    db.kv_set(f"heartbeat:{name}", json.dumps({"ts": db.now(), "pid": os.getpid(), **info}))


def heartbeat_state(raw: str | None, now: datetime | None = None, stale_after: int = HEARTBEAT_STALE_S) -> dict:
    """state: "ok" (beat within stale_after seconds), "stale" (older) or "never"."""
    try:
        d = json.loads(raw) if raw else None
        ts = db.parse_ts(d["ts"]) if d else None
    except (ValueError, KeyError, TypeError):
        d, ts = None, None
    if ts is None:
        return {"state": "never", "age_s": None, "ts": None, "info": {}}
    age = int(((now or datetime.now(timezone.utc)) - ts).total_seconds())
    return {"state": "ok" if age <= stale_after else "stale", "age_s": age, "ts": d["ts"], "info": d}


# ------------------------------------------------------------------ scheduled runs

PS_TASKS = r"""
Get-ScheduledTask | ForEach-Object {
  $t = $_
  $cmd = ($t.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ; '
  if ($cmd -match 'run\.py') {
    $i = $t | Get-ScheduledTaskInfo
    $n = ''
    if ($i.NextRunTime) { $n = $i.NextRunTime.ToUniversalTime().ToString('s') }
    "$($t.TaskName)`t$n`t$cmd"
  }
}
"""


def scheduled_jobs() -> list[dict]:
    """OS-scheduled `run.py` jobs: Windows Task Scheduler tasks whose action runs run.py (with the
    next run time), or the user's crontab lines that run run.py (next time not computed)."""
    jobs = []
    if os.name == "nt":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            return jobs
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", PS_TASKS],
                           capture_output=True, text=True, timeout=90)
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                jobs.append({"name": parts[0], "next": parts[1] + "Z" if parts[1] else None, "command": parts[2]})
    elif shutil.which("crontab"):
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            if "run.py" in line and not line.lstrip().startswith("#"):
                jobs.append({"name": "cron", "next": None, "command": line.strip()})
    return jobs


def next_run(jobs: list[dict]) -> dict | None:
    timed = [j for j in jobs if j.get("next")]
    return min(timed, key=lambda j: j["next"]) if timed else (jobs[0] if jobs else None)


# ------------------------------------------------------------------ cache + background refresh

_refreshing = threading.Lock()


def _cached(key: str) -> dict | None:
    raw = db.kv_get(key)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _refresh_worker() -> None:
    try:
        db.kv_set(K_CREDS, json.dumps({"checked_at": db.now(), "items": credential_checks()}))
        try:
            sched = {"checked_at": db.now(), "jobs": scheduled_jobs(), "error": None}
        except Exception as e:  # noqa: BLE001
            sched = {"checked_at": db.now(), "jobs": [], "error": f"{type(e).__name__}: {e}"[:200]}
        db.kv_set(K_SCHEDULE, json.dumps(sched))
    except Exception:  # noqa: BLE001
        log.exception("health refresh failed")
    finally:
        _refreshing.release()


def refresh(force: bool = False) -> bool:
    """Start a background re-check if the cached one is older than CHECK_EVERY_S (or force).
    Returns True when a check was started."""
    cached = _cached(K_CREDS)
    if not force and cached and \
            (datetime.now(timezone.utc) - db.parse_ts(cached["checked_at"])).total_seconds() < CHECK_EVERY_S:
        return False
    if not _refreshing.acquire(blocking=False):
        return False
    threading.Thread(target=_refresh_worker, daemon=True, name="health-refresh").start()
    return True


def panel(now: datetime | None = None) -> dict:
    """Everything the dashboard's health panel shows. Reads only the DB (no network)."""
    cfg = get_config()
    creds = _cached(K_CREDS)
    sched = _cached(K_SCHEDULE)
    jobs = (sched or {}).get("jobs") or []
    return {
        "credentials": (creds or {}).get("items"),
        "checked_at": (creds or {}).get("checked_at"),
        "checking": _refreshing.locked(),
        "youtube_used": db.uploads_today("youtube", now),
        "youtube_cap": cfg["youtube"]["max_uploads_per_day"],
        "bot": heartbeat_state(db.kv_get("heartbeat:bot"), now),
        "schedule_checked": sched is not None,
        "schedule_error": (sched or {}).get("error"),
        "jobs": jobs,
        "next_run": next_run(jobs),
        "dry_run": cfg["dry_run"],
    }
