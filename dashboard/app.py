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

from pipeline import actions, db, health
from pipeline.config import country_by_iso3, get_config, path, setup_logging
from pipeline.orchestrator import STEPS
from pipeline.topics import dropped_topics, load_topics, verify_report

RUN_RE = re.compile(r"^r\d{8}-\d{6}-[0-9a-f]{4}$")
MEDIA = {"video.mp4", "thumb.jpg", "contact.png"}

# The board shows a video's journey in plain words. Every DB stage belongs to exactly one lane.
LANES = [
    ("data", "Data secured", ("queued", "data_ready", "picked", "labelled")),
    ("made", "Video made", ("rendered",)),
    ("waiting", "Waiting for approval", ("awaiting_approval",)),
    ("approved", "Approved", ("approved", "publishing")),
    ("shipped", "Shipped", ("published",)),
    ("failed", "Failed", ("failed",)),
    ("rejected", "Rejected", ("rejected",)),
]
LANE_OF = {stage: key for key, _, stages in LANES for stage in stages}
# what is happening to a run that sits in a stage (the next step is running)
DOING = {"queued": "getting data", "data_ready": "picking countries", "picked": "writing labels",
         "labelled": "making the video", "publishing": "publishing"}
STALL_S = 30 * 60
PLATFORMS = [("youtube", "YT"), ("instagram", "IG"), ("facebook", "FB")]
CHIP = {"pending": "pending", "uploading": "uploading", "uploaded": "uploading", "live": "live",
        "private_locked": "private-locked", "dry_run": "dry-run", "failed": "failed"}
LANE_CARDS = 12

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


def _dur(s: float | int | None) -> str:
    if s is None:
        return "?"
    s = max(int(s), 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def _until(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        s = (db.parse_ts(ts) - datetime.now(timezone.utc)).total_seconds()
    except ValueError:
        return ts
    return f"in {_dur(s)}" if s >= 0 else f"{_dur(-s)} ago"


app.jinja_env.filters["ago"] = _ago
app.jinja_env.filters["dur"] = _dur
app.jinja_env.filters["until"] = _until


def lane_for(stage: str) -> str:
    return LANE_OF[stage]


def chip_state(post: dict | None, stage: str) -> str:
    """Chip colour for one platform: the post's status, or pending/none when nothing was posted."""
    if post:
        return CHIP.get(post["status"], post["status"])
    return "none" if stage in ("rejected", "failed") else "pending"


_titles: dict[str, tuple[float, str | None]] = {}


def _title(run_id: str) -> str | None:
    f = path("out") / run_id / "meta.json"
    try:
        mt = f.stat().st_mtime
    except OSError:
        return None
    if _titles.get(run_id, (None,))[0] != mt:
        try:
            _titles[run_id] = (mt, json.loads(f.read_text(encoding="utf-8")).get("title"))
        except (OSError, ValueError):
            return None
    return _titles[run_id][1]


def _card(r: dict, posts: dict, telegram_sent: set, now: datetime) -> dict:
    stage = r["stage"]
    in_stage = (now - db.parse_ts(r["updated_at"])).total_seconds()
    progress = r.get("progress") if stage == "labelled" else None
    fresh = r.get("progress_at") and (now - db.parse_ts(r["progress_at"])).total_seconds() < 60
    enabled = {"youtube": True, "instagram": get_config()["instagram"]["enabled"],
               "facebook": get_config()["facebook"]["enabled"]}
    chips = []
    for plat, short in PLATFORMS:
        p = posts.get((r["id"], plat))
        if not (enabled[plat] or p):
            continue
        state = chip_state(p, stage)
        link = p["url"] if p and p.get("url") and state in ("live", "private-locked", "uploading") else None
        chips.append({"short": short, "platform": plat, "state": state, "url": link,
                      "title": f"{plat}: {state}" + (f" - {p['message']}" if p and p.get("message") else "")})
    return {
        "id": r["id"], "topic": r["topic_id"], "stage": stage, "title": _title(r["id"]),
        "thumb": (path("out") / r["id"] / "thumb.jpg").exists(),
        "in_stage_s": in_stage, "doing": DOING.get(stage),
        "progress": round(progress * 100) if progress is not None else None,
        "stalled": stage in DOING and in_stage > STALL_S and not fresh,
        "telegram_sent": r["id"] in telegram_sent,
        "chips": chips, "failed_stage": r.get("failed_stage"), "error": r.get("error"),
        "retry_step": r.get("failed_stage") if r.get("failed_stage") in actions.RETRY_STEPS else None,
        "n_countries": len(r["countries"]),
    }


def _lanes(now: datetime | None = None) -> list[dict]:
    db.init()
    now = now or datetime.now(timezone.utc)
    posts = {(p["run_id"], p["platform"]): p for p in db.get_posts()}
    sent = db.runs_with_log("telegram: sent")
    lanes = [{"key": k, "name": n, "cards": [], "total": 0} for k, n, _ in LANES]
    by_key = {lane["key"]: lane for lane in lanes}
    for r in db.list_runs(limit=500):
        lane = by_key[lane_for(r["stage"])]
        lane["total"] += 1
        if len(lane["cards"]) < LANE_CARDS:
            lane["cards"].append(_card(r, posts, sent, now))
    return lanes


def _board_context() -> dict:
    if not app.config.get("TESTING"):
        health.refresh()          # background re-check when the cached one is stale; never blocks
    lanes = _lanes()
    from pipeline.approve_telegram import enabled as telegram_on
    return {"lanes": lanes, "health": health.panel(), "telegram_on": telegram_on(),
            "busy": any(c["doing"] and not c["stalled"] for lane in lanes for c in lane["cards"])}


@app.route("/")
def board():
    return render_template("board.html", **_board_context())


@app.route("/fragment/board")
def board_fragment():
    return render_template("_board_cols.html", **_board_context())


@app.post("/make")
def make_video():
    try:
        flash(f"Make a video now: {actions.make_video('dashboard')}")
    except Exception as e:  # noqa: BLE001
        flash(f"not started: {e}")
    return redirect(url_for("board"))


@app.post("/health/recheck")
def health_recheck():
    flash("re-checking credentials in the background..." if health.refresh(force=True) else "a check is already running")
    return redirect(url_for("board"))


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
    back = redirect(url_for("board")) if request.form.get("next") == "board" else None
    try:
        res = fn(run_id, *args)
        flash(f"{run_id}: {res}")
        if fn is actions.regenerate:
            return back or redirect(url_for("run_detail", run_id=res))
    except db.TransitionError as e:
        flash(f"not allowed: {e}")
    except Exception as e:  # noqa: BLE001
        flash(f"error: {type(e).__name__}: {e}")
    return back or redirect(url_for("run_detail", run_id=run_id))


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
    if step not in actions.RETRY_STEPS:
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
    return jsonify({lane["key"]: [c["id"] for c in lane["cards"]] for lane in _lanes()})


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
