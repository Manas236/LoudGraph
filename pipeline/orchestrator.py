"""Runs a video through the stages. Every stage reads its inputs from out/<run_id>/ (or the DB)
and writes its outputs there, so any stage can be re-run alone:

    fetch  -> data_ready         data.json, scores.json
    pick   -> picked             pick.json
    label  -> labelled           labels.json
    render -> rendered           timeline.json, audio.wav, audio.json, video.mp4, meta.json, thumb.jpg
    notify -> awaiting_approval  (Telegram message if configured)
"""
from __future__ import annotations

import json
import logging
import traceback
import zlib

from . import db
from .config import country_by_iso3, get_config, run_dir
from .picker import NotEnoughData

log = logging.getLogger(__name__)

STEPS = ["fetch", "pick", "label", "render", "notify"]
BEFORE = {"fetch": "queued", "pick": "data_ready", "label": "picked", "render": "labelled", "notify": "rendered"}
AFTER = {"fetch": "data_ready", "pick": "picked", "label": "labelled", "render": "rendered", "notify": "awaiting_approval"}


def _read(run_id: str, name: str):
    return json.loads((run_dir(run_id) / name).read_text(encoding="utf-8"))


def _write(run_id: str, name: str, obj) -> None:
    (run_dir(run_id) / name).write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")


def alert(text: str) -> None:
    try:
        from .approve_telegram import enabled, send_message
        if enabled():
            send_message(text)
    except Exception as e:  # noqa: BLE001 - alerts must never break the pipeline
        log.warning("telegram alert failed: %s", e)


# ------------------------------------------------------------------ steps

def step_fetch(run_id: str, topic: dict) -> tuple[str, dict]:
    from .fetch import fetch_topic
    from .score import score_topic
    data = fetch_topic(topic)
    scored = score_topic(data, topic)
    _write(run_id, "data.json", data)
    _write(run_id, "scores.json", scored)
    n_ok = sum(1 for s in scored if s["ok"])
    return f"{len(data['series'])} countries fetched ({data['fetched_at']}), {n_ok} pass the scorer", {}


def step_pick(run_id: str, topic: dict) -> tuple[str, dict]:
    from .picker import pick
    data, scored = _read(run_id, "data.json"), _read(run_id, "scores.json")
    exclude = db.used_country_sets(topic["id"], except_run=run_id)
    seed = zlib.crc32(run_id.encode())
    p = pick(scored, data["series"], topic, exclude_sets=exclude, seed=seed)
    _write(run_id, "pick.json", p)
    names = country_by_iso3()
    msg = f"{p['n']} countries: " + ", ".join(names[c]["name"] for c in p["countries"]) + \
          f" (mean score {p['mean_score']}, min distance {p['min_pairwise_distance']})"
    return msg, {"countries": p["countries"], "country_set": p["country_set"], "score": p["mean_score"]}


def step_label(run_id: str, topic: dict) -> tuple[str, dict]:
    from .labels import label_run
    data, p = _read(run_id, "data.json"), _read(run_id, "pick.json")
    names = {k: v["name"] for k, v in country_by_iso3().items()}
    labels = label_run(topic, p["countries"], data["series"], data.get("indicator_name") or topic["subtitle"], names)
    _write(run_id, "labels.json", labels)
    n = sum(1 for v in labels.values() if v.get("label"))
    reasons = sorted({v["reason"] for v in labels.values() if not v.get("label")})
    return f"{n}/{len(labels)} labels accepted" + (f"; no label because: {'; '.join(reasons)}" if reasons else ""), {}


def step_render(run_id: str, topic: dict) -> tuple[str, dict]:
    from . import audio, render
    from .lock import exclusive
    from .timeline import build, fits
    d = run_dir(run_id)
    data, p, labels = _read(run_id, "data.json"), _read(run_id, "pick.json"), _read(run_id, "labels.json")
    with exclusive("render", on_wait=lambda: db.log(run_id, "labelled", "waiting for another render to finish")):
        series = data["series"]
        rows = {c: [(y, v) for y, v in series[c] if p["x_start"] <= y <= p["x_end"]] for c in p["countries"]}
        tl = build([(c, [y for y, _ in rows[c]], [v for _, v in rows[c]]) for c in p["countries"]],
                   p["x_start"], p["x_end"])
        if not fits(tl):
            raise RuntimeError(f"timeline is {tl.duration:.1f}s with slow-mo events, outside "
                               f"{get_config()['video']['min_seconds']}-{get_config()['video']['max_seconds']}s; re-pick")
        tl.save(d / "timeline.json")
        values = {s.iso3: [dict(rows[s.iso3])[y] for y in s.years] for s in tl.slots}
        wav, stems, ainfo = audio.synthesize(tl, values, seed=zlib.crc32(run_id.encode()))
        audio.write_wav(d / "audio.wav", wav, ainfo["sample_rate"])
        for name, x in stems.items():
            audio.write_wav(d / f"{name}.wav", x, ainfo["sample_rate"])
        _write(run_id, "audio.json", ainfo)
        views = render.build_views(topic, p, data, labels, tl)
        info = render.render_video(topic, tl, views, d / "audio.wav", d / "video.mp4", thumb=d / "thumb.jpg")
        meta = render.build_meta(run_id, topic, data, p, labels, views, tl, ainfo)
        meta["render"] = info
        _write(run_id, "meta.json", meta)
    n_ev = sum(len(s.events) for s in tl.slots)
    msg = (f"rendered {tl.n_frames} frames ({tl.n_frames / tl.fps:.1f}s, {n_ev} slow-mo events) with "
           f"{info['backend']} backend [{info['backend_note']}] in {info['render_seconds']}s; audio "
           f"{ainfo['lufs']} LUFS, true peak {ainfo['true_peak_db']} dBTP, pluck stem "
           f"{ainfo['pluck_minus_pad_lu']} LU above pad")
    return msg, {}


def step_notify(run_id: str, topic: dict) -> tuple[str, dict]:
    return "ready for approval (dashboard" + (" + Telegram)" if _telegram_on() else " only; Telegram not configured)"), {}


def _telegram_on() -> bool:
    try:
        from .approve_telegram import enabled
        return enabled()
    except Exception:  # noqa: BLE001
        return False


STEP_FN = {"fetch": step_fetch, "pick": step_pick, "label": step_label, "render": step_render, "notify": step_notify}
LIVE = {"uploaded", "live", "private_locked"}
INPUTS = {"pick": ["data.json", "scores.json"], "label": ["data.json", "scores.json", "pick.json"],
          "render": ["data.json", "scores.json", "pick.json", "labels.json"],
          "notify": ["data.json", "scores.json", "pick.json", "labels.json", "timeline.json", "audio.wav",
                     "pluck.wav", "pad.wav", "fx.wav", "audio.json", "video.mp4", "meta.json", "thumb.jpg"]}


class RefuseRerun(RuntimeError):
    pass


def rerun_blocker(run_id: str) -> str | None:
    """A run that is published or has a live post must never be re-rendered in place."""
    run = db.get_run(run_id)
    if run["stage"] in ("published", "publishing"):
        return f"run {run_id} is {run['stage']}"
    live = [p["platform"] for p in db.get_posts(run_id) if p["status"] in LIVE]
    if live:
        return f"run {run_id} has live posts on {', '.join(live)}"
    return None


def copy_run(run_id: str, from_step: str) -> str:
    """New run with the same topic whose inputs up to `from_step` are copied from `run_id`."""
    import shutil
    src = db.get_run(run_id)
    new_id = db.create_run(src["topic_id"], regen_of=run_id)
    sd, nd = run_dir(run_id), run_dir(new_id)
    for name in INPUTS.get(from_step, []):
        if (sd / name).exists():
            shutil.copy2(sd / name, nd / name)
    fields = {}
    for step in STEPS[:STEPS.index(from_step)]:
        if step == "pick":
            fields = {"countries": src["countries"], "country_set": src["country_set"], "score": src["score"]}
        db.transition(new_id, AFTER[step], f"copied {step} outputs from {run_id}", **(fields if step == "pick" else {}))
    return new_id


def run_pipeline(run_id: str, from_step: str = "fetch") -> bool:
    from .topics import get_topic
    run = db.get_run(run_id)
    if not run:
        raise SystemExit(f"no run {run_id}")
    topic = get_topic(run["topic_id"])
    why = rerun_blocker(run_id)
    if why:
        db.log(run_id, run["stage"], f"refused to re-run from {from_step}: {why}")
        raise RefuseRerun(f"{why}; re-render it as a new run instead "
                          f"(python run.py produce --run {run_id} --from {from_step} --as-new)")
    if run["stage"] != BEFORE[from_step]:
        db.transition(run_id, BEFORE[from_step], f"retry from {from_step}", reset=True)
    for step in STEPS[STEPS.index(from_step):]:
        log.info("[%s] %s ...", run_id, step)
        try:
            msg, fields = STEP_FN[step](run_id, topic)
        except NotEnoughData as e:
            db.fail(run_id, step, str(e))
            log.warning("[%s] %s failed: %s", run_id, step, e)
            return False
        except Exception as e:  # noqa: BLE001 - record every failure in the state machine
            (run_dir(run_id) / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            db.fail(run_id, step, f"{type(e).__name__}: {e}")
            log.exception("[%s] %s failed", run_id, step)
            alert(f"❌ {run_id} ({topic['id']}) failed at {step}: {type(e).__name__}: {e}"[:900])
            return False
        db.transition(run_id, AFTER[step], msg, **fields)
        log.info("[%s] %s: %s", run_id, AFTER[step], msg)
    if _telegram_on():
        try:
            from .approve_telegram import send_for_approval
            send_for_approval(run_id)
        except Exception as e:  # noqa: BLE001 - dashboard approval still works
            db.log(run_id, "awaiting_approval", f"telegram send failed: {e}")
    return True


def produce(count: int, topic_id: str | None = None) -> list[str]:
    from .selector import choose_topic
    db.init()
    done, tried = [], set()
    attempts = 0
    while len(done) < count and attempts < count * 4:
        attempts += 1
        tid = topic_id or choose_topic(exclude=tried)
        if not tid:
            log.warning("no eligible topic left (all in cooldown, retired or tried this batch)")
            break
        tried.add(tid)
        rid = db.create_run(tid)
        log.info("produce: run %s topic %s", rid, tid)
        if run_pipeline(rid):
            done.append(rid)
        elif topic_id:
            break
    log.info("produce: %d/%d runs reached awaiting_approval: %s", len(done), count, done)
    return done


# ------------------------------------------------------------------ publishing

def publish_run(run_id: str) -> bool:
    if not db.claim(run_id, "approved", "publishing", "publishing started"):
        log.info("publish %s: not in approved state (or already claimed)", run_id)
        return False
    cfg = get_config()
    d = run_dir(run_id)
    meta = _read(run_id, "meta.json")
    video = d / "video.mp4"
    platforms = []
    from . import publish_instagram, publish_youtube
    platforms.append(("youtube", publish_youtube.publish))
    if cfg["instagram"]["enabled"]:
        platforms.append(("instagram", publish_instagram.publish))
    if cfg["facebook"]["enabled"]:
        platforms.append(("facebook", publish_instagram.publish_facebook))
    statuses = {}
    for name, fn in platforms:
        try:
            statuses[name] = fn(run_id, meta, video)
        except Exception as e:  # noqa: BLE001
            log.exception("publish %s %s failed", run_id, name)
            db.upsert_post(run_id, name, "failed", message=f"{type(e).__name__}: {e}"[:500])
            statuses[name] = "failed"
            alert(f"❌ publish {name} failed for {run_id}: {e}"[:900])
    good = {"uploaded", "live", "private_locked", "dry_run"}
    summary = ", ".join(f"{k}={v}" for k, v in statuses.items())
    if any(v == "pending" for v in statuses.values()):
        # deferred (e.g. daily upload cap): back to approved; publishers skip platforms already done
        db.transition(run_id, "approved", f"deferred, will retry: {summary}", reset=True)
        return False
    if any(v in good for v in statuses.values()):
        db.transition(run_id, "published", f"published: {summary}")
        return True
    db.fail(run_id, "publish", f"all platforms failed: {summary}")
    return False


def publish_approved(run_id: str | None = None) -> int:
    runs = [db.get_run(run_id)] if run_id else db.list_runs(stage="approved")
    n = 0
    for r in runs:
        if r and publish_run(r["id"]):
            n += 1
    log.info("published %d run(s)", n)
    return 0
