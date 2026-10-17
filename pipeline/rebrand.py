"""Re-brand reels that are made but not posted yet: the video opens with the configured hook
(config.yaml opening.hook_template: which_last, a guess-the-loser phrasing chosen per reel by hooks.py), and the
cover, the end card answer and the caption follow.

    python run.py rebrand [--run RUN_ID ...] [--dry-run]

Only the opening question changes. The data, the country order, the timeline and the music stay exactly
as they were: the frames are drawn again from the run's own data.json, pick.json, labels.json and
timeline.json, and the new video.mp4 COPIES the audio stream of the old file (nothing is synthesised or
encoded again). The old files are kept next to the new ones as video_oldhook.mp4, thumb_oldhook.jpg,
meta_oldhook.json and captions_oldhook.json (a second re-brand keeps the first originals).

A reel whose data makes no which_last phrasing true (hooks.truths: a tie, fewer than 3 countries, a last
country whose data stops early, ...) keeps its old video and is reported. Reels are done in posting order and
each one's phrasing rotates against the reels before it (hooks.phrase_history, or the history passed in).
Published reels, and reels with a live post, are never touched. An approved reel goes back to Ready for review (a fresh approval is
needed for what will really be posted) and gets its place in the posting queue back when it is approved.
"""
from __future__ import annotations

import json
import logging
import shutil
import zlib

from . import db, hooks
from .config import run_dir

log = logging.getLogger(__name__)

STAGES = ("rendered", "awaiting_approval", "approved")   # made, not posted (publishing and later: never)
BACKUPS = {"video.mp4": "video_oldhook.mp4", "thumb.jpg": "thumb_oldhook.jpg", "meta.json": "meta_oldhook.json",
           "captions.json": "captions_oldhook.json"}


def candidates() -> list[dict]:
    """Unposted reels with a video, in posting order first (approved), then review order."""
    from .posting import queue as posting_queue
    from .review import queue as review_queue
    out = posting_queue() + review_queue() + db.list_runs(stage="rendered")
    return [r for r in out if (run_dir(r["id"]) / "video.mp4").exists()]


def _read(run_id: str, name: str):
    return json.loads((run_dir(run_id) / name).read_text(encoding="utf-8"))


def plan(run_id: str, history: list | None = None) -> dict:
    """What re-branding would do (nothing is written): the new opening, or why the reel keeps its old one.
    history: the hook_phrase of the reels before this one, in posting order (default: hooks.phrase_history())."""
    from . import render
    from .orchestrator import rerun_blocker
    from .timeline import Timeline
    from .topics import get_topic
    run = db.get_run(run_id)
    if not run or run["stage"] not in STAGES:
        return {"run_id": run_id, "action": "refused", "why": f"stage {run['stage'] if run else 'missing'}"}
    why = rerun_blocker(run_id)
    if why:
        return {"run_id": run_id, "action": "refused", "why": why}
    d = run_dir(run_id)
    topic = get_topic(run["topic_id"])
    data, pick, labels, meta = _read(run_id, "data.json"), _read(run_id, "pick.json"), _read(run_id, "labels.json"), \
        _read(run_id, "meta.json")
    tl = Timeline.load(d / "timeline.json")
    views = render.build_views(topic, pick, data, labels, tl)
    opening = render.opening_for(topic, views, pick["x_start"], pick["x_end"], seed=zlib.crc32(run_id.encode()),
                                 history=history, run_id=run_id)
    old = meta.get("opening") or {}
    want = hooks.settings()["hook_template"]
    out = {"run_id": run_id, "topic_id": run["topic_id"], "stage": run["stage"], "old_question": old.get("question"),
           "old_answer": old.get("answer"), "question": opening["question"], "answer": opening["answer"],
           "hook_template": opening["hook_template"], "hook_phrase": opening.get("hook_phrase"),
           "truth": opening.get("truth"), "choice": opening.get("choice"), "countries": [v.name for v in views],
           "_ctx": (topic, tl, views, opening, meta)}
    if opening["hook_template"] != want:
        return {**out, "action": "kept", "why": opening.get("fallback") or f"opens with {opening['question']!r}"}
    return {**out, "action": "rebrand"}


def rebrand_run(run_id: str, by: str = "rebrand", history: list | None = None) -> dict:
    """Re-render one reel with the configured hook (see the module doc). Returns plan()'s dict plus what
    was done; never raises for a reel that is kept."""
    from . import captions, render
    from .lock import exclusive, run_lock, single_instance
    from .review import back_to_review
    p = plan(run_id, history)
    if p["action"] != "rebrand":
        if p["action"] == "kept":
            db.log(run_id, p["stage"], f"re-brand: keeps its old hook ({p['why']})")
        p.pop("_ctx", None)
        return p
    topic, tl, views, opening, meta = p.pop("_ctx")
    d = run_dir(run_id)
    with single_instance(run_lock(run_id)) as mine:
        if not mine:
            return {**p, "action": "refused", "why": "another process is making this run"}
        for name, backup in BACKUPS.items():
            if (d / name).exists() and not (d / backup).exists():
                shutil.copy2(d / name, d / backup)
        if p["stage"] == "approved":
            back_to_review(run_id, f"re-branded via {by} (new opening question): needs a fresh approval",
                           keep_place=True)
        with exclusive("render", on_wait=lambda: db.log(run_id, "awaiting_approval", "waiting for another render")):
            info = render.render_video(topic, tl, views, d / "audio.wav", d / "video.mp4", thumb=d / "thumb.jpg",
                                       opening=opening, audio_from=d / BACKUPS["video.mp4"])
        meta = {**meta, "opening": opening, "hook_template": opening["hook_template"],
                "hook_phrase": opening.get("hook_phrase"), "render": info,
                "cover_seconds": info["cover_seconds"], "rebranded": {"by": by, "at": db.now(),
                                                                       "old_opening": p["old_question"]}}
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
        tmp.replace(d / "meta.json")
        db.set_hook_template(run_id, opening["hook_template"], opening.get("hook_phrase"))
        try:
            c = captions.generate_for_run(run_id, fresh=True)
            p["captions"] = [v["caption"].splitlines()[0] for v in c["variants"]]
            p["captions_note"] = captions.summary(c)
        except Exception as e:  # noqa: BLE001 - the card goes out with the plain fills
            log.warning("[%s] captions after re-brand failed: %s", run_id, e)
            c = captions.generate_for_run(run_id, llm=False, fresh=True)
            p["captions"] = [v["caption"].splitlines()[0] for v in c["variants"]]
            p["captions_note"] = f"plain fills ({type(e).__name__})"
        stage = db.get_run(run_id)["stage"]
        db.log(run_id, stage, f"re-branded via {by}: opens with \"{opening['question']}\" (was "
                              f"\"{p['old_question']}\"); same data, countries, timeline and audio track; "
                              f"old video kept as {BACKUPS['video.mp4']}")
    return {**p, "action": "rebranded", "stage_after": stage, "render_seconds": info["render_seconds"]}


def rebrand_all(run_ids: list[str] | None = None, by: str = "rebrand", send: bool = True) -> list[dict]:
    """Re-brand every unposted reel (or run_ids), oldest place first. The ones that were approved come
    first in Ready for review, in their posting order, and (send=True, Telegram on) the review card is
    asked to show the first one: the card then goes through them one at a time, as for any new reel."""
    from .review import move_to_front, send_to_telegram
    runs = [r["id"] for r in candidates()] if run_ids is None else list(run_ids)
    results = []
    for rid in runs:
        try:
            res = rebrand_run(rid, by)
        except Exception as e:  # noqa: BLE001 - report it and go on with the next reel
            log.exception("[%s] re-brand failed", rid)
            res = {"run_id": rid, "action": "failed", "why": f"{type(e).__name__}: {e}"}
        results.append(res)
        log.info("re-brand %s: %s%s", rid, res["action"], f" ({res['why']})" if res.get("why") else "")
    reapprove = [r["run_id"] for r in results if r["action"] == "rebranded" and r.get("stage") == "approved"]
    for rid in reversed(reapprove):
        move_to_front(rid)
    done = [r["run_id"] for r in results if r["action"] == "rebranded"]
    if send and done:
        try:
            from .approve_telegram import enabled
            if enabled():
                first = (reapprove or done)[0]
                send_to_telegram(first, by)
        except Exception as e:  # noqa: BLE001 - the reels still wait in Ready for review
            log.warning("re-brand: could not ask the bot for the review card: %s", e)
    return results
