"""Graphony: data-sonification pipeline CLI.

    python run.py doctor
    python run.py fetch [--topic ID] [--force]
    python run.py topics-verify [--force]
    python run.py produce [--count N] [--topic ID]
    python run.py produce --run RUN_ID --from {fetch,pick,label,render,notify} [--as-new]
    python run.py bot
    python run.py publish [--run RUN_ID]
    python run.py stats [--no-summary]
    python run.py dashboard
    python run.py verify [--run RUN_ID ...]
    python run.py crash-test
    python run.py compare --old RUN_ID --new RUN_ID
    python run.py captions --run RUN_ID [RUN_ID ...] [--no-llm] [--trending]
    python run.py preview (--topic ID | --from-run RUN_ID) [--no-send]
    python run.py go-live [--check-only]
    python run.py account
    python run.py batch [--count N] [--queue-only]
    python run.py rebrand [--run RUN_ID ...] [--dry-run] [--no-send]
    python run.py hooks
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
        step = a.from_step or "fetch"
        run_id = a.run
        if a.as_new:
            run_id = orchestrator.copy_run(a.run, step)
            log.info("re-rendering %s as new run %s (inputs copied up to %s)", a.run, run_id, step)
        try:
            ok = orchestrator.run_pipeline(run_id, from_step=step)
        except orchestrator.RefuseRerun as e:
            log.error("refused: %s", e)
            return 2
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
    return orchestrator.publish_approved(run_id=a.run, platform=a.platform)


def cmd_stats(a):
    from pipeline.analytics import run_stats
    return run_stats(summary=not a.no_summary)


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


def cmd_crash_test(a):
    import json
    from pipeline.compare import crash_test
    res = crash_test()
    print(json.dumps({k: v for k, v in res.items() if k != "render"}, indent=1))
    return 0


def cmd_compare(a):
    import json
    from pipeline.compare import compare_runs
    print(json.dumps(compare_runs(a.old, a.new), indent=1))
    return 0


def cmd_captions(a):
    from pipeline import captions
    for rid in a.run:
        c = captions.generate_for_run(rid, llm=False if a.no_llm else None, trending=True if a.trending else None)
        print(f"=== {rid}\n{captions.preview_text(c)}\n")
    return 0


def cmd_preview(a):
    import json
    from pipeline import orchestrator, verify
    from pipeline.config import run_dir
    if not (a.topic or a.from_run):
        log.error("give --topic ID or --from-run RUN_ID")
        return 2
    rid = orchestrator.make_test_video(a.topic, a.from_run)
    frames = run_dir(rid) / "opening_frames"
    frames.mkdir(exist_ok=True)
    report = verify.opening_check(rid, out_dir=frames)
    print(json.dumps({"test_run": rid, **report}, indent=1))
    if not a.no_send:
        from pipeline.approve_telegram import enabled, send_test_preview
        if enabled():
            send_test_preview(rid, report)
            log.info("TEST preview %s sent to Telegram (not posted, not in the review queue)", rid)
        else:
            log.warning("Telegram is not configured; the TEST video is at %s", run_dir(rid) / "video.mp4")
    return 0 if report["ok"] else 1


def cmd_go_live(a):
    from pipeline import golive
    report = golive.run(apply=not a.check_only)
    golive.print_report(report)
    return 0 if report["passed"] else 1


def cmd_account(a):
    """The connected Instagram account and Facebook Page, read live from Meta by id (read-only)."""
    from pipeline.approve_telegram import account_text
    print(account_text())
    return 0


def cmd_batch(a):
    """The Telegram Make a batch, from the command line: queue N distinct topics (same choice: unmade,
    varied categories, never the same dataset back to back), then make the queue here one at a time
    (unless --queue-only, or the bot is running: it makes them itself)."""
    import json
    from pipeline import db, ideas, makequeue, review
    from pipeline.config import run_dir
    from pipeline.lock import held_elsewhere, single_instance
    db.init()
    if a.count:
        picks = ideas.pick_batch(a.count)
        if len(picks) < a.count:
            log.warning("only %d topics are free right now", len(picks))
        bid = makequeue.start_batch([t["id"] for t in picks])
        print(f"Batch {bid} queued: " + ", ".join(f"{t['name']} ({t['category']})" for t in picks))
    if a.queue_only:
        return 0
    if held_elsewhere("bot"):
        print("The bot is running: it makes the queued reels itself, one at a time.")
        return 0

    def done(job):
        name = review.topic_name(job["topic_id"])
        run = db.get_run(job["run_id"]) if job["run_id"] else None
        if job["state"] == "ready" and run:
            try:
                q = json.loads((run_dir(run["id"]) / "meta.json").read_text(encoding="utf-8"))["opening"]["question"]
            except (OSError, ValueError, KeyError):
                q = ""
            print(f"READY    {name}: {q}", flush=True)
        elif job["state"] == "skipped" and run:
            print(f"SKIPPED  {name}: {review.skip_reason(run['id'])}", flush=True)
        else:
            print(f"FAILED   {name}: {review.plain(run['error'] if run else 'no run')}", flush=True)
    with single_instance("batch") as mine:
        if not mine:   # e.g. Make a batch pressed twice in the dashboard: one worker makes the whole queue
            print("Another window is already making the queue: it makes these too, one at a time.")
            return 0
        makequeue.run_foreground(on_done=done)
    return 0


def cmd_rebrand(a):
    """Re-render the reels that are made but not posted with the configured opening question
    (opening.hook_template), keeping their data, countries and audio track; see pipeline/rebrand.py."""
    import json
    from pipeline import db, rebrand
    db.init()
    if a.dry_run:
        ids = a.run or [r["id"] for r in rebrand.candidates()]
        for rid in ids:
            p = rebrand.plan(rid)
            p.pop("_ctx", None)
            print(json.dumps(p, indent=1, ensure_ascii=False))
        return 0
    res = rebrand.rebrand_all(a.run, by="run.py rebrand", send=not a.no_send)
    print(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    return 0 if all(r["action"] in ("rebranded", "kept") for r in res) else 1


def cmd_hooks(a):
    """Fill in hook_template for every reel (from the question its video shows) and print it, with the
    average views and the weight per hook template."""
    from pipeline import analytics, db, hooks
    from pipeline.config import get_config
    db.init()
    posted = {p["run_id"]: p for p in db.latest_stats() if p["platform"] == "instagram"}
    for r in hooks.backfill():
        s = posted.get(r["run_id"])
        views = f"{s['views']} views" if s and s["views"] is not None else ""
        print(f"{r['run_id']}  {r['topic_id']:<28} {r['stage']:<18} {r['hook_template']:<14} "
              f"{r['question'] or '(no on-screen hook)'}  {views}")
    for tpl, w in analytics.update_hook_weights().items():
        print(f"weight {tpl}: {w['weight']:+.3f} over {w['n_posts']} post(s) at least "
              f"{get_config()['analytics']['min_age_hours']} h old")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="run.py", description="Graphony: data-sonification Shorts/Reels pipeline")
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
    s.add_argument("--as-new", action="store_true",
                   help="copy the run's inputs into a NEW run and run that (required for published/live runs)")
    s.set_defaults(fn=cmd_produce)
    sub.add_parser("bot").set_defaults(fn=cmd_bot)
    s = sub.add_parser("publish")
    s.add_argument("--run")
    s.add_argument("--platform", choices=["youtube", "instagram", "facebook"])
    s.set_defaults(fn=cmd_publish)
    s = sub.add_parser("stats", help="pull stats now (the bot also does it every analytics.every_hours)")
    s.add_argument("--no-summary", action="store_true", help="do not send the Telegram summary")
    s.set_defaults(fn=cmd_stats)
    sub.add_parser("dashboard").set_defaults(fn=cmd_dashboard)
    s = sub.add_parser("verify", help="ffprobe + loudness + A/V sync + contact sheet for rendered runs")
    s.add_argument("--run", nargs="*", help="run ids (default: the most recent rendered runs)")
    s.add_argument("--last", type=int, default=3)
    s.set_defaults(fn=cmd_verify)
    sub.add_parser("crash-test", help="render out/crash_test.mp4 from a synthetic flat-crash-recover series").set_defaults(
        fn=cmd_crash_test)
    s = sub.add_parser("compare", help="old vs new frame + spectrogram for two runs of the same set")
    s.add_argument("--old", required=True)
    s.add_argument("--new", required=True)
    s.set_defaults(fn=cmd_compare)
    s = sub.add_parser("captions", help="(re)make the caption options and hashtags of runs (out/<run>/captions.json)")
    s.add_argument("--run", nargs="+", required=True)
    s.add_argument("--no-llm", action="store_true", help="plain template fills only, no Gemini")
    s.add_argument("--trending", action="store_true", help="re-rank topic hashtags with the Instagram hashtag search")
    s.set_defaults(fn=cmd_captions)
    s = sub.add_parser("preview", help="render a TEST video outside the review queue, check its opening, "
                                        "send it to Telegram marked TEST (never posted)")
    s.add_argument("--topic", help="topic id (fetches its data)")
    s.add_argument("--from-run", help="reuse a past run's data.json + scores.json")
    s.add_argument("--no-send", action="store_true", help="do not send it to Telegram")
    s.set_defaults(fn=cmd_preview)
    s = sub.add_parser("go-live", help="check Instagram + Facebook (read-only), then leave test mode for those that pass")
    s.add_argument("--check-only", action="store_true", help="only run the checks; change nothing")
    s.set_defaults(fn=cmd_go_live)
    s = sub.add_parser("batch", help="queue N distinct topics (like Make a batch on Telegram) and make the queue "
                                     "here, one at a time, unless the bot is running")
    s.add_argument("--count", type=int, help="topics to add first (none: just make what is queued)")
    s.add_argument("--queue-only", action="store_true", help="only queue them; the bot makes them")
    s.set_defaults(fn=cmd_batch)
    sub.add_parser("account", help="show the connected Instagram account and Facebook Page, read live by id "
                                   "(read-only)").set_defaults(fn=cmd_account)
    s = sub.add_parser("rebrand", help="re-render unposted reels with the configured opening question "
                                       "(same data, countries and audio track; old files kept as *_oldhook.*)")
    s.add_argument("--run", nargs="*", help="run ids (default: every reel made but not posted)")
    s.add_argument("--dry-run", action="store_true", help="only say what would change")
    s.add_argument("--no-send", action="store_true", help="do not ask the bot to show them on the review card")
    s.set_defaults(fn=cmd_rebrand)
    sub.add_parser("hooks", help="fill in and list each reel's hook template, with the hook weights").set_defaults(
        fn=cmd_hooks)
    a = p.parse_args(argv)
    setup_logging()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
