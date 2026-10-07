"""Local dashboard (section 10). Binds to 127.0.0.1 only; on a server reach it through an SSH tunnel:
    ssh -L 5055:127.0.0.1:5055 user@server
Every button calls the same functions as the Telegram bot (pipeline/actions.py)."""
from __future__ import annotations

import ipaddress
import json
import re
import secrets
from datetime import datetime, timezone

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_from_directory, url_for

from pipeline import actions, db
from pipeline.config import country_by_iso3, get_config, path, setup_logging
from pipeline.orchestrator import STEPS
from pipeline.topics import dropped_topics, load_topics, verify_report

RUN_RE = re.compile(r"^r\d{8}-\d{6}-[0-9a-f]{4}$")
MEDIA = {"video.mp4", "thumb.jpg", "contact.png"}
BOARD_STAGES = ["queued", "data_ready", "picked", "labelled", "rendered", "awaiting_approval", "approved",
                "publishing", "published", "rejected", "failed"]

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
CSRF = secrets.token_hex(16)


@app.context_processor
def inject():
    return {"csrf": CSRF, "brand": get_config()["brand"]["name"] or "Pipeline", "names": country_by_iso3()}


@app.before_request
def check_csrf():
    if request.method == "POST" and request.form.get("csrf") != CSRF:
        abort(403)


def _run_json(run_id: str, name: str):
    f = path("out") / run_id / name
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def _ago(ts: str | None) -> str:
    if not ts:
        return ""
    s = (datetime.now(timezone.utc) - db.parse_ts(ts)).total_seconds()
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return f"{int(s)}s ago"


app.jinja_env.filters["ago"] = _ago


def _board():
    db.init()
    runs = db.list_runs(limit=300)
    cols = {s: [] for s in BOARD_STAGES}
    for r in runs:
        r["has_thumb"] = (path("out") / r["id"] / "thumb.jpg").exists()
        cols.setdefault(r["stage"], []).append(r)
    return cols


@app.route("/")
def board():
    return render_template("board.html", cols=_board(), stages=BOARD_STAGES)


@app.route("/fragment/board")
def board_fragment():
    return render_template("_board_cols.html", cols=_board(), stages=BOARD_STAGES)


@app.route("/run/<run_id>")
def run_detail(run_id):
    if not RUN_RE.match(run_id):
        abort(404)
    run = db.get_run(run_id)
    if not run:
        abort(404)
    pick = _run_json(run_id, "pick.json") or {}
    return render_template(
        "run.html", run=run, log=db.get_log(run_id), posts=db.get_posts(run_id), pick=pick,
        labels=_run_json(run_id, "labels.json") or {}, meta=_run_json(run_id, "meta.json") or {},
        has_video=(path("out") / run_id / "video.mp4").exists(),
        has_contact=(path("out") / run_id / "contact.png").exists(), steps=STEPS)


@app.route("/media/<run_id>/<name>")
def media(run_id, name):
    if not RUN_RE.match(run_id) or name not in MEDIA:
        abort(404)
    return send_from_directory(path("out") / run_id, name)


def _act(run_id, fn, *args):
    if not RUN_RE.match(run_id):
        abort(404)
    try:
        res = fn(run_id, *args)
        flash(f"{fn.__name__}: {res}")
        if fn is actions.regenerate:
            return redirect(url_for("run_detail", run_id=res))
    except db.TransitionError as e:
        flash(f"not allowed: {e}")
    except Exception as e:  # noqa: BLE001
        flash(f"error: {type(e).__name__}: {e}")
    return redirect(url_for("run_detail", run_id=run_id))


@app.post("/run/<run_id>/approve")
def approve(run_id):
    return _act(run_id, actions.approve, "dashboard")


@app.post("/run/<run_id>/reject")
def reject(run_id):
    return _act(run_id, actions.reject, "dashboard")


@app.post("/run/<run_id>/regenerate")
def regenerate(run_id):
    return _act(run_id, actions.regenerate, "dashboard")


@app.post("/run/<run_id>/retry")
def retry(run_id):
    step = request.form.get("step", "")
    if step not in STEPS:
        abort(400)
    return _act(run_id, lambda rid, by: actions.retry(rid, step, by), "dashboard")


@app.post("/run/<run_id>/rerender")
def rerender(run_id):
    step = request.form.get("step", "")
    if step not in STEPS:
        abort(400)
    return _act(run_id, lambda rid, by: actions.rerender_as_new(rid, step, by), "dashboard")


@app.post("/run/<run_id>/publish")
def publish(run_id):
    return _act(run_id, actions.publish, "dashboard")


@app.route("/analytics")
def analytics():
    db.init()
    rows = db.latest_stats()
    weights = sorted(db.topic_weights().values(), key=lambda w: -(w["weight"] or 0))
    return render_template("analytics.html", rows=rows, weights=weights)


@app.route("/topics")
def topics():
    db.init()
    cfg = get_config()
    rep = verify_report()
    weights = db.topic_weights()
    now = datetime.now(timezone.utc)
    items = []
    for t in load_topics():
        runs = db.list_runs(topic_id=t["id"], limit=50)
        last = runs[0]["created_at"] if runs else None
        cool = None
        if last:
            left = cfg["topic_cooldown_days"] - (now - db.parse_ts(last)).total_seconds() / 86400
            cool = round(left, 1) if left > 0 else None
        items.append({"t": t, "v": rep.get(t["id"], {}), "w": weights.get(t["id"]), "runs": len(runs),
                      "last": last, "cool": cool})
    return render_template("topics.html", items=items, dropped=dropped_topics(), rep=rep)


@app.route("/api/board")
def api_board():
    return jsonify({s: [r["id"] for r in rs] for s, rs in _board().items()})


def main() -> int:
    setup_logging()
    dc = get_config()["dashboard"]
    host = dc["host"]
    if host == "localhost":
        host = "127.0.0.1"
    if not ipaddress.ip_address(host).is_loopback:
        raise SystemExit(f"refusing to bind dashboard to {host}: loopback only (use an SSH tunnel)")
    print(f"Dashboard: http://{host}:{dc['port']}/")
    app.run(host=host, port=dc["port"], debug=False, threaded=True)
    return 0
