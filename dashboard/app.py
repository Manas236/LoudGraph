"""Local, server-rendered Review / Posting queue / Library / Settings dashboard.

Start it with Dashboard.bat, `python run.py dashboard`, or directly with `python dashboard/app.py`.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
import sys
from pathlib import Path

if not __package__:  # started as a script: make the repo root importable, as run.py does
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from markupsafe import Markup, escape

from dashboard import viewmodels as vm
from dashboard.media import chart_thumbnail
from pipeline import actions, db, health, posting
from pipeline import review as review_queue
from pipeline.accounts import PLATFORMS, account_states, ready_destinations, telegram_switch
from pipeline.config import brand_name, get_config, path, setup_logging
from pipeline.errors import explain_error

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
CSRF = secrets.token_hex(32)
RUN_RE = re.compile(r"r\d{8}-\d{6}-[0-9a-f]{4}")
PING = "graphony-dashboard"  # Dashboard.bat checks this to know the port is already ours


@app.before_request
def check_csrf():
    if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), CSRF):
        abort(403)
    if request.endpoint == "ping":
        return None
    db.init()
    if not app.config.get("TESTING"):
        health.refresh()


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
    if response.mimetype in ("text/html", "application/json"):
        response.headers["Cache-Control"] = "no-store"
    return response


THEMES = ("light", "dark")


@app.context_processor
def inject():
    theme = request.cookies.get("theme")   # set by the theme button (dashboard.js); light until changed
    return {"csrf": CSRF, "brand": brand_name(), "status": vm.status(), "nav": vm.nav(),
            "theme": theme if theme in THEMES else "light"}


app.jinja_env.filters["views"] = vm.short_views


@app.template_filter("code")
def code_spans(text):
    """`backticks` in owner instructions become <code>; everything else stays escaped."""
    return Markup(re.sub(r"`([^`]+)`", r"<code>\1</code>", str(escape(text))))


def get_run(ref):
    run = db.run_by_ref(ref)
    if not run:
        abort(404)
    return run


def review_context():
    waiting = review_queue.queue()   # the same order as the Telegram review cards
    videos = [vm.video(r) for r in waiting]
    selected = request.args.get("selected", type=int)
    current = next((v for v in videos if v["ref"] == selected), videos[0] if videos else None)
    run = next((r for r in waiting if current and r["ref"] == current["ref"]), None)
    telegram = {"on": account_states()["telegram"]["configured"] and telegram_switch(),
                "state": review_queue.telegram_state(run["id"]) if run else None}
    return {"videos": videos, "video": current, "position": videos.index(current) + 1 if current else 0,
            "next_video": vm.next_video(), "telegram": telegram}


@app.get("/ping")
def ping():
    return PING, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/")
def review():
    return render_template("review.html", **review_context(), making=vm.activity(), attention=vm.attention())


@app.get("/fragment/review")
def review_fragment():
    return render_template("_review.html", **review_context())


@app.get("/api/review")
def review_state():
    nav = vm.nav()
    return jsonify(waiting=[r["ref"] for r in review_queue.queue()],
                   activity=render_template("_activity.html", making=vm.activity(), attention=vm.attention()),
                   status=render_template("_status.html"),
                   counts={"review": nav["review"], "queue": nav["queue"]}, bot=nav["bot"],
                   telegram=nav["telegram"]["line"])


@app.get("/queue")
def queue_page():
    return render_template("queue.html", rows=vm.queue_rows(), rule=vm.posting_rule())


@app.get("/fragment/queue")
def queue_fragment():
    return render_template("_queue.html", rows=vm.queue_rows())


@app.get("/library")
def library():
    groups = vm.library_groups()
    tab = request.args.get("tab", "posted")
    if tab not in groups:
        abort(404)
    stats = db.latest_stats()
    runs = groups[tab]
    failed_only = request.args.get("failed") == "1"
    if failed_only:
        failed_ids = {p["run_id"] for p in db.get_posts() if p["status"] == "failed"}
        runs = [r for group in groups.values() for r in group if r["id"] in failed_ids]
    return render_template("library.html", tab=tab, counts={k: len(v) for k, v in groups.items()},
                           videos=[vm.video(r, stats=stats) for r in runs], failed_only=failed_only)


@app.get("/video/<int:ref>/drawer")
def drawer(ref):
    run = get_run(ref)
    video = vm.video(run)
    video["platforms"].sort(key=lambda p: p["state"] != "failed")
    return render_template("_drawer.html", video=video, run=run, logs=db.get_log(run["id"]))


@app.get("/video/<int:ref>/media/<name>")
def media(ref, name):
    run = get_run(ref)
    if name == "preview.jpg":
        target = chart_thumbnail(run)
        if not target:
            return send_file(app.root_path + "/static/preview.svg", mimetype="image/svg+xml")
    elif name == "video.mp4":
        target = path("out") / run["id"] / name
    else:
        abort(404)
    if not target.exists():
        abort(404)
    return send_file(target, conditional=True)


@app.get("/flags/<iso>.svg")
def flag(iso):
    if not re.fullmatch("[a-z]{2}", iso):
        abort(404)
    return send_file(path("flags") / (iso + ".svg"), conditional=True)


@app.get("/font/inter.ttf")
def font():
    """Inter, the typeface of the videos and the brand, from assets/fonts (no web font service)."""
    return send_file(path("fonts") / "Inter-Variable.ttf", mimetype="font/ttf", conditional=True, max_age=86400)


@app.get("/settings")
def settings():
    from dashboard import setup_steps
    logfile = path("cache") / "logs" / "pipeline.log"
    lines = logfile.read_text(encoding="utf-8", errors="replace").splitlines()[-50:] if logfile.exists() else []
    return render_template("settings.html", accounts=account_states(), setup=setup_steps, config=get_config(),
                           platforms=PLATFORMS, topics=vm.topic_rows(), health=health.panel(),
                           next_video=vm.next_video(), logs="\n".join(lines))


def result(fn, back="review"):
    try:
        message = fn()
    except (ValueError, db.TransitionError) as error:
        message = str(error)
        if RUN_RE.search(message):
            message = "This video has already changed. Refresh and try again."
        code = 409
    except Exception as error:
        app.logger.exception("Dashboard action failed")
        message = explain_error(str(error))["sentence"]
        code = 500
    else:
        code = 200
    if request.headers.get("Accept") == "application/json":
        return jsonify(message=message), code
    flash(message)
    return redirect(url_for(back))


@app.post("/video/<int:ref>/<operation>")
def video_action(ref, operation):
    run = get_run(ref)
    rid = run["id"]
    if operation not in {"approve", "telegram", "reject", "remake", "title", "retry", "publish", "up", "down",
                         "unapprove"}:
        abort(404)

    def act():
        if operation == "approve":
            chosen = request.form.getlist("platform") if request.form.get("platforms_present") else None
            actions.approve(rid, "dashboard", platforms=chosen)
            ready = ready_destinations(db.get_run(rid))
            if not ready:
                return "Approved — waiting in Library until accounts are connected"
            return (f"Approved — in the posting queue: posts {posting.when_for(rid)} to "
                    + ", ".join(PLATFORMS[p] for p in ready))
        if operation == "telegram":
            return actions.send_to_telegram(rid, "dashboard")
        if operation == "reject":
            actions.reject(rid, "dashboard")
            return "Rejected — moved to Library"
        if operation == "remake":
            if run["stage"] != "awaiting_approval":
                raise ValueError("This video has already been reviewed.")
            actions.regenerate(rid, "dashboard")
            return "Making a new version with different countries"
        if operation == "title":
            return actions.edit_title(rid, request.form.get("title", ""), "dashboard")
        if operation == "retry":
            if request.form.get("platform"):
                return actions.retry_platform(rid, request.form["platform"], "dashboard")
            return actions.retry(rid, run["failed_stage"], "dashboard")
        if operation == "publish":
            return posting.post_now(rid, "dashboard")
        if operation in ("up", "down"):
            if not posting.move(rid, -1 if operation == "up" else 1):
                return "Already " + ("first" if operation == "up" else "last") + " in the queue"
            return "Moved " + operation
        if operation == "unapprove":
            return posting.unapprove(rid, "dashboard")
    return result(act, "queue_page" if operation in ("up", "down", "unapprove") else "review")


@app.post("/video/<int:ref>/caption")
def caption_action(ref):
    """Use one of the caption options, or save the owner's own caption + hashtags."""
    rid = get_run(ref)["id"]

    def act():
        if request.form.get("choose") is not None:
            return actions.choose_caption(rid, request.form.get("choose", type=int, default=-1), "dashboard")
        return actions.edit_caption(rid, request.form.get("text", ""), request.form.get("hashtags", ""),
                                    "dashboard")["message"]
    return result(act)


@app.post("/make")
def make_video():
    return result(lambda: actions.make_video("dashboard"))


@app.post("/make/batch")
def make_batch():
    return result(lambda: actions.make_batch(request.form.get("count", type=int, default=0), "dashboard"))


@app.post("/stats/refresh")
def refresh_stats():
    return result(lambda: actions.refresh_stats("dashboard"), "library")


@app.post("/settings/telegram")
def switch_telegram():
    return result(lambda: actions.set_telegram(request.form.get("enabled") == "on", "dashboard"), "settings")


@app.post("/attention/dismiss")
def dismiss():
    item = vm.attention()
    if not item or item["key"] != request.form.get("key") or item["version"] != request.form.get("version"):
        return jsonify(message="This message has already changed."), 409
    return result(lambda: actions.dismiss_attention(item["key"], item["version"], "dashboard"))


@app.post("/settings/accounts/<service>/test")
def test_account(service):
    if service not in account_states():
        abort(404)
    check = actions.test_account(service)
    label = "Connected ✓" if check["state"] == "ok" else "Expired — reconnect" if check["state"] in ("expired", "invalid") else "Not connected"
    if check["state"] == "disabled":
        label = "Off — enable this platform in Posting to test it."
    detail = explain_error(check["detail"], service=check["name"])
    message = label if check["state"] in ("ok", "missing", "disabled") else label + ". " + detail["sentence"] + " " + detail["fix"]
    if request.headers.get("Accept") == "application/json":
        return jsonify(message=message)
    flash(message)
    return redirect(url_for("settings"))


@app.post("/settings/posting")
def save_posting():
    return result(lambda: actions.save_posting(request.form), "settings")


@app.post("/settings/topics/<topic_id>")
def toggle_topic(topic_id):
    return result(lambda: actions.toggle_topic(topic_id, request.form.get("enabled") == "on"), "settings")


@app.get("/run/<run_id>")
def old_detail(run_id):
    run = db.get_run(run_id) if RUN_RE.fullmatch(run_id) else None
    if not run:
        abort(404)
    return redirect(url_for("review", selected=run["ref"]) if run["stage"] == "awaiting_approval" else
                    url_for("library", tab="approved", video=run["ref"]))


@app.get("/topics")
def old_topics():
    return redirect("/settings#topics")


@app.get("/analytics")
def old_analytics():
    return redirect("/library")


def main() -> int:
    setup_logging()
    dc = get_config()["dashboard"]
    host = "127.0.0.1" if dc["host"] == "localhost" else dc["host"]
    if not ipaddress.ip_address(host).is_loopback:
        raise SystemExit(f"refusing to bind dashboard to {host}: loopback only (use an SSH tunnel)")
    print(f"{brand_name()} dashboard: http://{host}:{dc['port']}/  (keep this window open; close it to stop)")
    app.run(host=host, port=dc["port"], debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
