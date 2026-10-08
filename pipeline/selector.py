"""Choose the next topic (section 12): 70% weighted by topic weight, 30% exploration of the
least-tried topics. Topics in cooldown or retired are skipped."""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timezone

from . import db
from .config import get_config
from .topics import load_topics

log = logging.getLogger(__name__)


def eligible_topics(exclude=()) -> list[dict]:
    cfg = get_config()
    weights = db.topic_weights()
    cooldown = cfg["topic_cooldown_days"]
    now = datetime.now(timezone.utc)
    out = []
    for t in load_topics():
        if t["id"] in exclude or not t.get("enabled", True):
            continue
        events = db.topic_events(t["id"])
        if events and "already" in events[0]["reason"] and "used" in events[0]["reason"]:
            continue
        w = weights.get(t["id"])
        if w and w["retired"]:
            continue
        recent = [r for r in db.list_runs(topic_id=t["id"], limit=20)
                  if not (r["stage"] == "failed" and r["failed_stage"] == "fetch")]
        if recent and (now - db.parse_ts(recent[0]["created_at"])).total_seconds() < cooldown * 86400:
            continue
        out.append(t)
    return out


def tries(topic_id: str) -> int:
    return len([r for r in db.list_runs(topic_id=topic_id, limit=1000) if r["stage"] in ("published", "publishing")])


def choose_topic(exclude=(), rng: random.Random | None = None) -> str | None:
    rng = rng or random.Random()
    cfg = get_config()["selector"]
    cands = eligible_topics(exclude)
    if not cands:
        return None
    weights = db.topic_weights()
    known = [weights[t["id"]]["weight"] for t in cands if t["id"] in weights and weights[t["id"]]["weight"] is not None]
    if known and rng.random() < cfg["exploit_share"]:
        g = sum(known) / len(known)
        ws = [weights.get(t["id"], {}).get("weight") for t in cands]
        ws = [g if w is None else w for w in ws]
        m = max(ws)
        probs = [math.exp(w - m) for w in ws]   # weights are mean log(views)
        pick = rng.choices(cands, weights=probs, k=1)[0]
        mode = "exploit"
    else:
        counts = {t["id"]: tries(t["id"]) for t in cands}
        least = min(counts.values())
        pick = rng.choice([t for t in cands if counts[t["id"]] == least])
        mode = "explore"
    log.info("selector: %s -> %s (%d eligible)", mode, pick["id"], len(cands))
    return pick["id"]
