"""Cold open: a reel opens with cold_open.seconds of its most dramatic beat, already moving at frame 0 under a
hard stab, then a quick rewind cut (cold_open.snap_seconds) back to the start of the race (config.yaml
cold_open). It is ONE variable for an A/B test on retention: the race is exactly the reel it would be without
one (every frame and note at the same race time, timeline.build), only cold_open + snap seconds later; the hook
question keeps its timing from frame 0, and the music, colours and fonts stay as they are. The reel gets longer
by that much, so one that would pass video.max_seconds (25 s) is made without a cold open ("skipped: would
exceed 25s").

The beat. captions.ranked_moves, the list the "sharpest" story fact takes its first move from, ranks every
one-year move by how dramatic it is. The cold open shows the first move that
  * is not by the country that finishes last (the end card's last row: the answer to the loser question), nor
    by a country the opening question's answer names (spoilers()),
  * scores at least captions.SHARPEST_MIN (the sharpest fact's own bar),
  * is an EVENT on screen (timeline.find_events: at least timing.event_threshold of its country's own range),
    so the video itself plays it in slow motion with the ring and the hit, and it reads with the sound off,
  * keeps the ending secret: the clip ends before its line starts its final year step (so no country's final
    value, and nothing of the final ordering, is ever in it), before the race's last final value (where the
    final ranking is settled) and before the end card; the move still lands inside the clip.
Crossovers and overtakes cannot be beats: the video draws one country at a time, each on its own scale, so two
lines never cross on screen. When nothing qualifies the reel is made without a cold open and the reason is kept.

Alternation (mode alternate). A NEW reel gets the opposite of what the last new reel really got, by render
order: a reel that should have had a cold open but had no beat counts as without, so the next one tries again.
The state is the kv row cold_open:alternate, so it survives restarts. A re-render of a run keeps the side it was
made with; TEST videos (no run row) never move it.

Tagging. cold_open (1/0) and cold_open_beat (the beat, or why there is none) are stored like the hook: in
meta.json, on the run, and copied onto each post when it is made (db.upsert_post). backfill() marks every reel
made before cold opens existed as without one. The stats loop keeps them next to hook and topic
(db.latest_stats), groups() averages the retention metrics per side and save_baseline() keeps the last reels
without one as the baseline (`python run.py coldopen`).

Reels already made are never re-rendered unless cold_open.apply_to_existing_queue is true; then
`python run.py coldopen --apply-queue` re-renders the unposted ones (apply_to_queue).
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone

from . import db
from .config import get_config, path, run_dir

log = logging.getLogger(__name__)

MODES = ("alternate", "always", "never")
K_ALTERNATE = "cold_open:alternate"   # {"renders", "with", "last", "last_run"}: new reels in render order
SNAP_MAX = 0.3                        # the rewind cut must stay under this
ARRIVE = 0.35                         # the move lands this long after the clip's first frame: already moving at frame 0
LAND_ROOM = 0.5                       # ... and at least this long before its last frame, so its ring and hit are seen
BEFORE = "made before cold opens existed"
LIVE = ("live", "uploaded", "private_locked")
BACKUPS = {"video.mp4": "video_nocoldopen.mp4", "thumb.jpg": "thumb_nocoldopen.jpg", "meta.json": "meta_nocoldopen.json",
           "timeline.json": "timeline_nocoldopen.json", "audio.json": "audio_nocoldopen.json"}


def settings() -> dict:
    """config.yaml cold_open, with the clip and the cut rounded to whole frames (a missing block = never)."""
    cfg = get_config()
    c, fps = cfg.get("cold_open") or {}, cfg["video"]["fps"]
    mode = str(c.get("mode", "never")).strip().lower()
    if mode not in MODES:
        log.warning("cold_open.mode %r is not one of %s: no cold opens", mode, ", ".join(MODES))
        mode = "never"
    frames = max(1, round(float(c.get("seconds", 1.5)) * fps))
    snap = max(1, round(float(c.get("snap_seconds", 0.2)) * fps))
    while snap > 1 and snap / fps >= SNAP_MAX:
        snap -= 1
    return {"mode": mode, "seconds": frames / fps, "snap_seconds": snap / fps,
            "apply_to_existing_queue": bool(c.get("apply_to_existing_queue", False))}


def _meta(run_id: str) -> dict | None:
    try:
        return json.loads((path("out") / run_id / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ which side a reel is on

def alternate_state() -> dict:
    try:
        st = json.loads(db.kv_get(K_ALTERNATE) or "{}")
    except ValueError:
        st = {}
    return {"renders": int(st.get("renders", 0)), "with": int(st.get("with", 0)), "last": st.get("last"),
            "last_run": st.get("last_run")}


def made_side(run_id: str) -> tuple[bool, str] | None:
    """(cold open, beat) of a run that was rendered before; None for a run without a video yet."""
    run = db.get_run(run_id)
    if run and run.get("cold_open") is not None:
        return bool(run["cold_open"]), run.get("cold_open_beat") or ""
    meta = _meta(run_id)
    if meta is None:
        return None
    on = bool(meta.get("cold_open", False))
    return on, meta.get("cold_open_beat") or ("" if on else BEFORE)


def decide(run_id: str | None, fresh: bool = False) -> dict:
    """{"want", "why", "counts"}: whether this render should try a cold open, why not, and whether it is a new
    reel that moves the alternation (record()). fresh=True: decide as for a new reel even when the run was made
    before (apply_to_queue)."""
    s = settings()
    run = db.get_run(run_id) if run_id else None
    side = None if fresh or not run_id else made_side(run_id)
    if s["mode"] == "never":
        return {"want": False, "why": "cold_open.mode is never", "counts": False}
    if side is not None:   # a re-render is not a new reel: it keeps its side of the test
        return {"want": side[0], "why": side[1] or BEFORE, "counts": False}
    if s["mode"] == "always":
        return {"want": True, "why": "", "counts": run is not None}
    st = alternate_state()
    if st["last"]:
        return {"want": False, "why": "control side of the A/B: the last new reel had a cold open",
                "counts": run is not None}
    return {"want": True, "why": "", "counts": run is not None}


def record(run_id: str, decision: dict, state: dict) -> None:
    """Store what the render really did on the run (and its posts) and, for a new reel, move the alternation."""
    db.set_cold_open(run_id, state["cold_open"], state["cold_open_beat"])
    if decision.get("counts"):
        st = alternate_state()
        db.kv_set(K_ALTERNATE, json.dumps({"renders": st["renders"] + 1, "with": st["with"] + int(state["cold_open"]),
                                           "last": bool(state["cold_open"]), "last_run": run_id}))


# ------------------------------------------------------------------ the beat

def spoilers(topic: dict, views: list, opening: dict | None = None) -> dict[str, str]:
    """{iso3: why} of the countries a cold open must not show: the one that finishes last (the end card's last
    row, in the topic's rank direction, and any that ends level with it on screen), and any country the opening
    question's answer names."""
    from .render import end_order
    from .topics import format_value
    out = {}
    if views:
        last = format_value(topic, views[end_order(topic, views)[-1]].values[-1])
        out = {v.iso3: "finishes last" for v in views if format_value(topic, v.values[-1]) == last}
    by_name = {v.name: v.iso3 for v in views}
    for name in (opening or {}).get("highlight") or []:
        if name in by_name and by_name[name] not in out:
            out[by_name[name]] = "is in the answer to the opening question"
    return out


def pick_beat(topic: dict, views: list, tl, exclude: dict[str, str] | None = None) -> tuple[dict | None, str]:
    """(the beat and the clip's place in the race, "") or (None, why no move qualifies); see the module doc.
    tl: the reel's timeline built with the cold open (its race times are the ones the clip shows); exclude:
    spoilers(), the countries whose moves are never the beat."""
    from .captions import SHARPEST_MIN, ranked_moves
    from .topics import format_value
    exclude = exclude or {}
    seconds = tl.cold_open
    rows = {v.iso3: list(zip(v.years, v.values)) for v in views}
    names = {v.iso3: v.name for v in views}
    slots = {s.iso3: s for s in tl.slots}
    settled = tl.slots[-1].onsets[-1]   # every final value has been shown: the final ranking is settled
    moves = [m for m in ranked_moves(rows) if m["score"] >= SHARPEST_MIN]
    if not moves:
        return None, f"no beat fits: no one-year move scores {SHARPEST_MIN} or more"
    passed, left_out, why_not = [], {}, []   # every move passed over; the excluded countries' first; the others
    for rank, m in enumerate(moves, 1):
        s, name = slots[m["iso3"]], names[m["iso3"]]
        what = f"#{rank} {name} {m['from_year']}-{m['to_year']}"
        if m["iso3"] in exclude:
            passed.append(f"{what}: {name} {exclude[m['iso3']]}")
            left_out.setdefault(m["iso3"], f"{what} and its other moves: {name} {exclude[m['iso3']]}")
            continue
        event, reason = s.event_for(m["to_year"]), None
        land = s.onset_of(m["to_year"])
        final_step = s.onsets[-2] if len(s.onsets) > 1 else s.onsets[-1]
        limit = min(final_step, settled, tl.end_start)
        start = min(max(s.draw_start, land - ARRIVE), limit - seconds)
        if not event:
            reason = f"{what} is not an event on screen"
        elif start < s.draw_start - 1e-9 or land - start > seconds - LAND_ROOM:
            reason = f"{what} is too close to the end of {name}'s line"
        if reason:
            passed.append(reason)
            why_not.append(reason)
            continue
        fmt = lambda x: format_value(topic, x)  # noqa: E731
        verb = "crashes" if event["kind"] == "fall" else "jumps"
        end = start + seconds
        text = (f"{name} {verb} {fmt(m['from'])} to {fmt(m['to'])} ({m['from_year']}-{m['to_year']}), move #{rank} "
                f"of {len(moves)}; race {start:.2f}-{end:.2f} s")
        return {"rank": rank, "of": len(moves), "kind": event["kind"], "iso3": m["iso3"], "country": name,
                "from_year": m["from_year"], "to_year": m["to_year"], "from": fmt(m["from"]), "to": fmt(m["to"]),
                "score": round(m["score"], 4), "slot": s.index, "window": [round(start, 6), round(end, 6)],
                "lands_at": round(land - start, 3), "final_step_at": final_step, "settled_at": settled,
                "end_card_at": tl.end_start, "excluded": {names[i]: why for i, why in exclude.items() if i in names},
                "passed_over": passed, "text": text}, ""
    more = f"; {len(why_not) - 3} more moves that do not fit either" if len(why_not) > 3 else ""
    return None, "no beat fits: " + "; ".join(list(left_out.values()) + why_not[:3]) + more


def timeline_for(topic: dict, pick: dict, data: dict, labels: dict, countries: list[tuple], decision: dict,
                 opening: dict | None = None):
    """(timeline, views, state) for a render: with a cold open when the decision wants one, the reel stays within
    video.max_seconds with it and a beat qualifies (never one by a spoilers() country of this opening); else the
    reel as it would be without (state: {"cold_open", "cold_open_beat", "beat"})."""
    from .render import build_views
    from .timeline import build
    plain = build(countries, pick["x_start"], pick["x_end"])
    views = build_views(topic, pick, data, labels, plain)
    why = decision["why"]
    if decision["want"]:
        s, cap = settings(), float(get_config()["video"]["max_seconds"])
        tl = build(countries, pick["x_start"], pick["x_end"], cold_open=s["seconds"], snap=s["snap_seconds"])
        if tl.duration > cap + 1e-6:
            why = (f"skipped: would exceed {cap:g}s ({plain.duration:.2f} s + {tl.lead:.2f} s cold open and cut = "
                   f"{tl.duration:.2f} s)")
        else:
            beat, why = pick_beat(topic, views, tl, spoilers(topic, views, opening))
            if beat:
                tl.cold_open_from = beat["window"][0]
                return tl, views, {"cold_open": True, "cold_open_beat": beat["text"], "beat": beat}
    return plain, views, {"cold_open": False, "cold_open_beat": why, "beat": None}


def window_check(tl, fps: int | None = None) -> dict:
    """The no-spoiler rule, measured on a timeline: the latest race time any cold open frame (clip or rewind)
    draws, against its line's final year step, the moment the final ranking is settled and the end card."""
    if tl.cold_open <= 0:
        return {"cold_open": False, "ok": True}
    fps = fps or tl.fps
    shown = [tl.cold_open_time(f / fps) for f in range(int(round(tl.lead * fps)))]
    s = tl.slot_at(tl.cold_open_from)
    final_step = s.onsets[-2] if len(s.onsets) > 1 else s.onsets[-1]
    settled = tl.slots[-1].onsets[-1]
    latest = max(shown)
    return {"cold_open": True, "frames": len(shown), "first_race_time": shown[0], "latest_race_time": latest,
            "clip_country_final_step_at": final_step, "final_ranking_settled_at": settled, "end_card_at": tl.end_start,
            "last_frame_at": (tl.n_frames - 1) / tl.fps,
            "ok": all(x is not None for x in shown) and latest < final_step <= settled < tl.end_start
            and all(x < (tl.n_frames - 1) / tl.fps for x in shown)}


# ------------------------------------------------------------------ wording (dashboard, Telegram)

def detail(on: bool | None, beat: str | None) -> str:
    """The beat without its race times, or why there is none in a few words."""
    text = beat or ""
    if on:
        return text.split(";")[0].strip()
    return (text.split(" (")[0] if text.startswith("skipped:") else text.split(":")[0]).strip()


def summary(on: bool | None, beat: str | None) -> str:
    """"yes · <the beat>" or "no · <why>" (the Telegram review card's line)."""
    d = detail(on, beat)
    return ("yes" if on else "no") + (f" · {d}" if d else "")


def side_of(run: dict) -> tuple[bool | None, str]:
    if run.get("cold_open") is not None:
        return bool(run["cold_open"]), run.get("cold_open_beat") or ""
    side = made_side(run["id"])
    return side if side else (None, "")


# ------------------------------------------------------------------ reels made before / the stats loop

def backfill() -> int:
    """cold_open and cold_open_beat for every run with a video that has none yet, from its meta.json (a video made
    before cold opens existed has none), on the run and its posts. Idempotent. Returns how many were filled in."""
    with db.db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM runs WHERE cold_open IS NULL").fetchall()]
    n = 0
    for rid in ids:
        meta = _meta(rid)
        if meta is None:
            continue
        on = bool(meta.get("cold_open", False))
        db.set_cold_open(rid, on, meta.get("cold_open_beat") or ("" if on else BEFORE))
        n += 1
    if n:
        log.info("cold_open filled in for %d run(s)", n)
    return n


METRICS = [("views", "views"), ("reach", "reach"), ("avg_watch_s", "avg watch s"), ("avg_view_pct", "watch % len"),
           ("total_watch_s", "total watch s"), ("skip_rate", "skip % (0-3s)")]


def _rows(min_age_hours: float = 0, now: datetime | None = None) -> list[dict]:
    plats = set(get_config()["analytics"].get("weight_platforms") or ["youtube", "instagram"])
    now = now or datetime.now(timezone.utc)
    out = []
    for r in db.latest_stats():
        if r["platform"] in plats and r["status"] in LIVE:
            r["age_hours"] = round((now - db.parse_ts(r["posted_at"])).total_seconds() / 3600, 1)
            if r["age_hours"] >= min_age_hours:
                out.append(r)
    return out


def groups(rows: list[dict]) -> dict:
    """{"with": {...}, "without": {...}}: n reels with stats, and the mean of each metric over the reels that have it."""
    out = {}
    for side, on in (("with", True), ("without", False)):
        mine = [r for r in rows if bool(r.get("cold_open")) == on and r.get("views") is not None]
        g = {"n": len(mine)}
        for k, _ in METRICS:
            vals = [r[k] for r in mine if r.get(k) is not None]
            g[k] = round(sum(vals) / len(vals), 2) if vals else None
            g[k + "_n"] = len(vals)
        out[side] = g
    return out


def grouped(now: datetime | None = None) -> dict:
    """groups() over the posted reels on analytics.weight_platforms that are analytics.min_age_hours old, plus how
    many younger ones were left out."""
    age = float(get_config()["analytics"]["min_age_hours"])
    every = _rows(0, now)
    old = [r for r in every if r["age_hours"] >= age]
    return {"min_age_hours": age, "groups": groups(old), "too_young": len(every) - len(old)}


def save_baseline(n: int = 6, now: datetime | None = None) -> list[dict]:
    """The A/B baseline in cold_open_baseline: the last n reels posted (analytics.weight_platforms) without a cold
    open, with their latest retention numbers. The first call chooses the reels; later calls (each stats pull)
    update the same reels' numbers, so the baseline never moves on to newer reels."""
    backfill()
    rows = _rows(0, now)
    with db.db() as c:
        chosen = {r["post_id"] for r in c.execute("SELECT post_id FROM cold_open_baseline").fetchall()}
    if chosen:
        picked = [r for r in rows if r["post_id"] in chosen]
    else:
        picked = sorted((r for r in rows if not r.get("cold_open")), key=lambda r: (r["posted_at"], r["post_id"]),
                        reverse=True)[:n]
    ts = db.now()
    with db.db() as c:
        for r in picked:
            c.execute("""INSERT INTO cold_open_baseline(post_id, run_id, platform, posted_at, captured_at, stats_at,
                                                        age_hours, views, reach, avg_watch_s, avg_view_pct, total_watch_s,
                                                        skip_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(post_id) DO UPDATE SET captured_at=excluded.captured_at, stats_at=excluded.stats_at,
                           age_hours=excluded.age_hours, views=excluded.views, reach=excluded.reach,
                           avg_watch_s=excluded.avg_watch_s, avg_view_pct=excluded.avg_view_pct,
                           total_watch_s=excluded.total_watch_s, skip_rate=excluded.skip_rate""",
                      (r["post_id"], r["run_id"], r["platform"], r["posted_at"], ts, r.get("fetched_at"), r["age_hours"],
                       r.get("views"), r.get("reach"), r.get("avg_watch_s"), r.get("avg_view_pct"),
                       r.get("total_watch_s"), r.get("skip_rate")))
    return baseline()


def baseline() -> list[dict]:
    with db.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM cold_open_baseline ORDER BY posted_at DESC, post_id DESC")]


def refresh_baseline() -> None:
    """Each stats pull: the saved baseline reels get their newest numbers (nothing is chosen here)."""
    if baseline():
        save_baseline()


def report_lines() -> list[str]:
    """`python run.py coldopen`: the settings, the alternation, the baseline and the averages per side."""
    from .review import topic_name
    s, st = settings(), alternate_state()
    lines = [f"cold_open: mode {s['mode']}, clip {s['seconds']:g} s, rewind {s['snap_seconds']:g} s, "
             f"apply_to_existing_queue {str(s['apply_to_existing_queue']).lower()}",
             f"alternation: {st['renders']} new reel(s) rendered, {st['with']} with a cold open; next: "
             f"{'without (control)' if st['last'] else 'with'}" if s["mode"] == "alternate" else ""]

    def num(v, k):
        return "-" if v is None else f"{v:,.0f}" if k in ("views", "reach") else f"{v:.2f}"

    head = f"{'':<38}" + "".join(f"{label:>14}" for _, label in METRICS)
    base = baseline()
    lines += ["", f"Baseline (the last {len(base)} reels posted without a cold open; numbers as of each reel's latest pull):",
              head]
    for r in base:
        run = db.get_run(r["run_id"]) or {}
        name = f"{topic_name(run.get('topic_id', ''))[:22]} {r['posted_at'][5:10]} {r['age_hours']:>4.0f}h"
        lines.append(f"{name:<38}" + "".join(f"{num(r[k], k):>14}" for k, _ in METRICS))
    g = grouped()
    lines += ["", f"Average per side, reels posted at least {g['min_age_hours']:g} h ago ({g['too_young']} younger left out):",
              head]
    for side in ("with", "without"):
        x = g["groups"][side]
        lines.append(f"{side + ' cold open (' + str(x['n']) + ' reels)':<38}" + "".join(f"{num(x[k], k):>14}" for k, _ in METRICS))
    if not g["groups"]["with"]["n"]:
        lines.append("No reel with a cold open has stats yet: the comparison fills in as they are posted and pulled.")
    return [ln for ln in lines if ln is not None]


# ------------------------------------------------------------------ reels already made

def rerender(run_id: str, by: str = "run.py coldopen") -> dict:
    """Give an unposted reel a cold open (apply_to_queue): decided like a new reel; when it gets one, its video,
    audio and timeline are made again from its own data with the SAME opening question and answer, the old files
    are kept as *_nocoldopen.*, and an approved reel goes back to Ready for review (keeping its place)."""
    from .lock import exclusive, run_lock, single_instance
    from .orchestrator import reel_inputs, render_reel, rerun_blocker
    from .review import back_to_review
    from .topics import get_topic
    run = db.get_run(run_id)
    if not run or run["stage"] not in ("rendered", "awaiting_approval", "approved"):
        return {"run_id": run_id, "action": "refused", "why": f"stage {run['stage'] if run else 'missing'}"}
    why = rerun_blocker(run_id)
    if why:
        return {"run_id": run_id, "action": "refused", "why": why}
    side = made_side(run_id)
    if side and side[0]:
        return {"run_id": run_id, "action": "kept", "why": "it already has a cold open"}
    meta = _meta(run_id) or {}
    if not meta.get("opening"):
        return {"run_id": run_id, "action": "refused", "why": "its meta.json has no opening question to keep"}
    topic, d = get_topic(run["topic_id"]), run_dir(run_id)
    with single_instance(run_lock(run_id)) as mine:
        if not mine:
            return {"run_id": run_id, "action": "refused", "why": "another process is making this run"}
        with exclusive("render", on_wait=lambda: db.log(run_id, run["stage"], "waiting for another render")):
            decision = decide(run_id, fresh=True)
            data, pick, labels, countries = reel_inputs(run_id)
            cold = timeline_for(topic, pick, data, labels, countries, decision, meta["opening"])[2]
            if not cold["cold_open"]:   # the control side, or no beat fits: the video stays exactly as it is
                record(run_id, decision, cold)
                return {"run_id": run_id, "action": "kept", "why": cold["cold_open_beat"]}
            for name, backup in BACKUPS.items():
                if (d / name).exists() and not (d / backup).exists():
                    shutil.copy2(d / name, d / backup)
            if run["stage"] == "approved":   # before the render, so the posting queue cannot post the old video
                back_to_review(run_id, f"given a cold open via {by}: needs a fresh approval", keep_place=True)
            msg, _ = render_reel(run_id, topic, opening=meta["opening"], decision=decision)
    after = _meta(run_id) or {}
    db.log(run_id, db.get_run(run_id)["stage"], f"given a cold open via {by}: {after.get('cold_open_beat')}; same data, "
                                               f"countries and opening question; old video kept as {BACKUPS['video.mp4']}")
    return {"run_id": run_id, "action": "cold_open", "beat": after.get("cold_open_beat"), "render": msg}


def apply_to_queue(by: str = "run.py coldopen") -> list[dict]:
    """Every unposted reel (rebrand.candidates: the posting queue, then Ready for review, then rendered) through
    rerender(), in that order. Refused unless cold_open.apply_to_existing_queue is true."""
    from .rebrand import candidates
    if not settings()["apply_to_existing_queue"]:
        raise ValueError("cold_open.apply_to_existing_queue is false in config.yaml: reels already made are left as "
                         "they are")
    out = []
    for run in candidates():
        try:
            out.append(rerender(run["id"], by))
        except Exception as e:  # noqa: BLE001 - report it and go on with the next reel
            log.exception("[%s] cold open re-render failed", run["id"])
            out.append({"run_id": run["id"], "action": "failed", "why": f"{type(e).__name__}: {e}"})
    return out
