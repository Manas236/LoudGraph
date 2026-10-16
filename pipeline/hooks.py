"""Hook templates: which opening question a reel shows on screen from frame 0, kept apart from its topic so
the stats can say which hook works (analytics.update_hook_weights; the Telegram Stats screen).

  which_last     "Which country ends up last?"   (opener ends_last in caption_formats.json)
  who_overtakes  "Who overtakes who?"            (opener overtakes)
  other          any other question, or no on-screen hook (the reels made before 2026-10-13)

The template is read from the question the video really shows (meta.json "opening"), stored on the run
when it is rendered, and copied onto each post when it is published (db.upsert_post). backfill() fills it
in for every reel made before the field existed. config.yaml opening.hook_template decides which hook new
reels use; the weights are for reporting only.
"""
from __future__ import annotations

import json
import logging

from . import db
from .config import get_config, path

log = logging.getLogger(__name__)

WHICH_LAST, WHO_OVERTAKES, OTHER = "which_last", "who_overtakes", "other"
QUESTIONS = {"Which country ends up last?": WHICH_LAST, "Who overtakes who?": WHO_OVERTAKES}
OPENERS = {WHICH_LAST: "ends_last", WHO_OVERTAKES: "overtakes"}   # template -> opener id
LABELS = {WHICH_LAST: "Which country ends up last?", WHO_OVERTAKES: "Who overtakes who?",
          OTHER: "Other or no hook"}


def classify(question: str | None) -> str:
    """The template of an on-screen opening question (exact wording, as drawn on the video)."""
    return QUESTIONS.get(" ".join((question or "").split()), OTHER)


def settings() -> dict:
    o = get_config().get("opening") or {}
    return {"hook_template": o.get("hook_template", WHICH_LAST), "hook_explore_pct": float(o.get("hook_explore_pct", 0))}


def preferred_opener() -> str | None:
    """The opener id config.yaml asks for (None for "auto": the old choice between the top two)."""
    t = settings()["hook_template"]
    return OPENERS.get(t, None if t in ("auto", "", None) else t)


def of_run(run_id: str) -> str | None:
    """The template of a run's video, from its meta.json; None when the run has no video."""
    try:
        meta = json.loads((path("out") / run_id / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return classify((meta.get("opening") or {}).get("question"))


def backfill() -> list[dict]:
    """hook_template for every run with a video (and its posts), from the question its video shows.
    Idempotent: only rows that differ are written. Returns [{"run_id", "topic_id", "stage", "question",
    "hook_template", "changed"}] for every run with a video."""
    out = []
    for run in sorted(db.list_runs(limit=100000), key=lambda r: r["ref"]):
        tpl = of_run(run["id"])
        if tpl is None:
            continue
        posts = db.get_posts(run["id"])
        changed = run.get("hook_template") != tpl or any(p.get("hook_template") != tpl for p in posts)
        if changed:
            db.set_hook_template(run["id"], tpl)
        try:
            q = (json.loads((path("out") / run["id"] / "meta.json").read_text(encoding="utf-8")).get("opening")
                 or {}).get("question")
        except (OSError, ValueError):
            q = None
        out.append({"run_id": run["id"], "topic_id": run["topic_id"], "stage": run["stage"], "question": q,
                    "hook_template": tpl, "changed": changed})
    n = sum(1 for r in out if r["changed"])
    if n:
        log.info("hook_template filled in for %d run(s)", n)
    return out
