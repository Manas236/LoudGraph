"""LoudGraphs CLI.

    python run.py doctor
    python run.py fetch [--topic ID] [--force]
    python run.py topics-verify [--force]
    python run.py produce [--count N] [--topic ID]
    python run.py produce --run RUN_ID --from {fetch,pick,label,render,notify}
    python run.py bot
    python run.py publish [--run RUN_ID]
    python run.py stats
    python run.py dashboard
    python run.py verify [--run RUN_ID ...]
"""
from __future__ import annotations

import argparse
import logging
import sys

from pipeline.config import setup_logging

log = logging.getLogger("run")


def cmd_doctor(a):
    from pipeline.doctor import doctor
    return doctor()


def cmd_fetch(a):
    from pipeline import fetch, topics
    for t in topics.load_topics():
        if a.topic and t["id"] != a.topic:
            continue
        data = fetch.fetch_topic(t, force=a.force)
        log.info("%s: %d countries, fetched %s", t["id"], len(data["series"]), data["fetched_at"])
    return 0


def cmd_topics_verify(a):
    from pipeline.topics import verify_topics
    return verify_topics(force=a.force)


def cmd_produce(a):
    from pipeline import orchestrator
    if a.run:
        ok = orchestrator.run_pipeline(a.run, from_step=a.from_step or "fetch")
        return 0 if ok else 1
    from pipeline.config import get_config
    count = a.count or get_config()["cadence"]["videos_per_day"]
    done = orchestrator.produce(count=count, topic_id=a.topic)
    return 0 if len(done) == count else 1


def cmd_bot(a):
    from pipeline.approve_telegram import run_bot
    return run_bot()


def cmd_publish(a):
    from pipeline import orchestrator
    return orchestrator.publish_approved(run_id=a.run)


def cmd_stats(a):
    from pipeline.analytics import run_stats
    return run_stats()


def cmd_dashboard(a):
    from dashboard.app import main
    return main()


def cmd_verify(a):
    import json
    from pipeline import db
    from pipeline.verify import verify_run
    ids = a.run or [r["id"] for r in db.list_runs(limit=500) if r["stage"] in
                    ("awaiting_approval", "approved", "publishing", "published", "rendered")][: a.last]
    ok = True
    for rid in ids:
        res = verify_run(rid)
        ok &= res["passed"]
        print(json.dumps({k: res[k] for k in ("run_id", "passed", "checks", "loudness", "sync", "contact")}, indent=1))
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="run.py", description="LoudGraphs data-sonification pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("fetch")
    s.add_argument("--topic")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_fetch)
    s = sub.add_parser("topics-verify")
    s.add_argument("--force", action="store_true", help="re-download instead of using the cache")
    s.set_defaults(fn=cmd_topics_verify)
    s = sub.add_parser("produce")
    s.add_argument("--count", type=int, help="videos to make (default: cadence.videos_per_day)")
    s.add_argument("--topic", help="force a topic id instead of the selector")
    s.add_argument("--run", help="re-run an existing run")
    s.add_argument("--from", dest="from_step", choices=["fetch", "pick", "label", "render", "notify"])
    s.set_defaults(fn=cmd_produce)
    sub.add_parser("bot").set_defaults(fn=cmd_bot)
    s = sub.add_parser("publish")
    s.add_argument("--run")
    s.set_defaults(fn=cmd_publish)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    sub.add_parser("dashboard").set_defaults(fn=cmd_dashboard)
    s = sub.add_parser("verify", help="ffprobe + loudness + A/V sync + contact sheet for rendered runs")
    s.add_argument("--run", nargs="*", help="run ids (default: the most recent rendered runs)")
    s.add_argument("--last", type=int, default=3)
    s.set_defaults(fn=cmd_verify)
    a = p.parse_args(argv)
    setup_logging()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
