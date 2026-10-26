"""Graphony autopilot jobs (systemd timers on the server, deploy/):

    python -m pipeline generate [--dry-run] [--count N]
    python -m pipeline post --slot 1|2
    python -m pipeline post --dry-run [--slot 1|2] [--day YYYY-MM-DD | tomorrow]
    python -m pipeline insights
    python -m pipeline health
    python -m pipeline dashboard

`run.py` keeps every other command (doctor, produce, stats, bot, ...).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta


def main(argv=None) -> int:
    from .config import setup_logging
    p = argparse.ArgumentParser(prog="python -m pipeline", description="Graphony autopilot jobs")
    sub = p.add_subparsers(dest="job", required=True)
    s = sub.add_parser("generate", help="top up the pool of ready reels (03:00 IST)")
    s.add_argument("--dry-run", action="store_true", help="say what it would do and render ONE test video that never "
                                                          "joins the pool")
    s.add_argument("--count", type=int, help="reels to make (default: the pool refill rule)")
    s = sub.add_parser("post", help="pick and post the reel of slot 1 (14:30 IST) or 2 (16:00 IST)")
    s.add_argument("--slot", type=int, choices=[1, 2])
    s.add_argument("--dry-run", action="store_true", help="print the pick(s) with their score breakdown; post nothing, "
                                                          "write nothing")
    s.add_argument("--day", help="dry run: the day to plan (YYYY-MM-DD or tomorrow; default today)")
    sub.add_parser("insights", help="pull the insights of every posted reel (every 6 h)")
    sub.add_parser("health", help="token, disk, pool and insights checks; old media cleanup (09:00 IST)")
    sub.add_parser("dashboard", help="the local dashboard on 127.0.0.1 (DASHBOARD_PORT, default 5055)")
    a = p.parse_args(argv)
    setup_logging()
    from . import autopilot, db, generator, jobs
    if a.job == "dashboard":
        from dashboard.app import main as dashboard
        return dashboard()
    if a.job == "generate":
        if a.dry_run:   # renders a TEST video only: allowed even where MIGRATED_TO_SERVER exists
            db.init()
            return generator.generate(count=1, dry=True)
        return autopilot.run_job("generate", generator.generate, count=a.count)
    if a.job == "post":
        from . import autopick
        if a.dry_run:
            db.init()
            day = autopilot.ist().date() + timedelta(days=1) if a.day == "tomorrow" else \
                date.fromisoformat(a.day) if a.day else autopilot.ist().date()
            for d in autopick.plan(day, indexes=(1, 2) if not a.slot else tuple(range(1, a.slot + 1))):
                if not a.slot or d["index"] == a.slot:
                    print(autopick.describe(d) + "\n")
            print("dry run: nothing was posted or written.")
            return 0
        if not a.slot:
            p.error("post needs --slot 1 or --slot 2")
        return autopilot.run_job("post", autopick.post, a.slot)
    if a.job == "insights":
        return autopilot.run_job("insights", jobs.insights)
    if a.job == "health":
        return autopilot.run_job("health", jobs.health)
    return 2


if __name__ == "__main__":
    sys.exit(main())
