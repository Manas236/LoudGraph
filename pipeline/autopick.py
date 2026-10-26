"""The autopilot's picker: at each slot time it chooses one reel from the ready pool and posts it.

The pool: reels made and ready (stage awaiting_approval, or approved from the dashboard), with their video on disk.
A reel is ELIGIBLE for a slot when
  * it opens with a "Which country...?" question (hook_template which_last),
  * its topic + country set was never posted,
  * every country in it has data for the reel's final year,
  * it is not the category, and does not share the hook's answer country, of the day's other post.

Every day has one EXPLOIT post and one EXPLORE post. Slot 1 (14:30 IST) is the exploit post on
autopilot.anchor_day and every second day after it, and slot 2 (16:00) is; on the days between it is the other way
round, so the slot time never confounds the comparison.
  * Exploit: the highest-scoring reel (learn.Model.score) of a category with >= exploit_min_posts scored posts that is
    not retired. Ties: the interest scorer (the reel's own scorer score).
  * Explore: a reel of a category with fewer posts, or of a retired category due its retry, chosen by Thompson
    sampling (learn.Draws). Ties (reels with the same features): the interest scorer.
  * No explore candidate: the second-best exploit reel (the best one stays for the day's exploit slot), logged.
  * No exploit candidate: Thompson sampling over the whole pool, logged.
  * No category with >= exploit_min_posts posts yet: Thompson sampling over the whole pool for both slots.
Selection runs at slot time, on the newest stats. Every pick writes a `decisions` row: the slot, the reel, its score
breakdown, the top 3 runners-up, and the reels that were eligible.

Pool upkeep (the generate job): each day a reel was eligible and not picked adds 1 to its loss count; at
autopilot.pool_max_losses it is DROPPED: its media files are deleted, its row is kept (stage dropped).

The post job (`python -m pipeline post --slot 1|2`): refuses to run more than late_minutes after its slot time or
within min_gap_minutes of the previous post (one alert), claims the slot atomically so it never posts twice, posts
synchronously, and retries a failed platform once; still failing, one alert. A successful post pings healthchecks.io.
"""
from __future__ import annotations

import json
import logging
import random
import time as _time
from datetime import date, datetime, timedelta, timezone

from . import alerts, db, learn
from .autopilot import ist, settings as ap_settings, slot_times
from .config import get_config, run_dir

log = logging.getLogger(__name__)
POOL_STAGES = ("awaiting_approval", "approved")
MEDIA_SUFFIXES = {".mp4", ".wav", ".jpg", ".jpeg", ".png", ".mov", ".webm", ".mkv"}
RETRY_WAIT_S = 60


# ------------------------------------------------------------------ the pool

def _read(run_id: str, name: str) -> dict | None:
    try:
        return json.loads((run_dir(run_id) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pool() -> list[dict]:
    """Ready reels with their video on disk, oldest first."""
    with db.db() as c:
        rows = c.execute(f"SELECT rowid AS ref, * FROM runs WHERE stage IN ({','.join('?' * len(POOL_STAGES))}) "
                         "AND media_deleted_at IS NULL ORDER BY created_at, rowid", POOL_STAGES).fetchall()
    return [db._run_dict(r) for r in rows if (run_dir(r["id"]) / "video.mp4").exists()]


def answer_countries(run_id: str) -> set[str]:
    """The country names the hook's answer is about (the end card's highlight)."""
    o = (_read(run_id, "meta.json") or {}).get("opening") or {}
    return {str(n) for n in (o.get("highlight") or []) if n}


def posted_sets() -> set[tuple[str, str]]:
    """(topic, country set) of every reel that went live anywhere."""
    with db.db() as c:
        rows = c.execute("SELECT DISTINCT r.topic_id, r.country_set FROM runs r JOIN posts p ON p.run_id = r.id "
                         "WHERE p.status IN ('live','uploaded','private_locked') AND r.country_set IS NOT NULL").fetchall()
    return {(r["topic_id"], r["country_set"]) for r in rows}


def data_through_end(run_id: str) -> str | None:
    """None when every country of the reel has a value for its final year, else why not."""
    data, p = _read(run_id, "data.json"), _read(run_id, "pick.json")
    if not data or not p:
        return "its data.json or pick.json is missing"
    end = int(p["x_end"])
    short = [c for c in p["countries"] if not any(int(y) == end for y, _ in data["series"].get(c, []))]
    return f"no {end} data for {', '.join(short)}" if short else None


def hook_ok(run: dict) -> str | None:
    if run.get("hook_template") != "which_last" or not str(run.get("hook_phrase") or "").startswith("Which country"):
        return f"its hook is not a \"Which country...?\" question ({run.get('hook_phrase') or run.get('hook_template')})"
    return None


def blockers(run: dict, other: dict | None, posted: set) -> str | None:
    """Why a pool reel cannot post in this slot, or None."""
    why = hook_ok(run)
    if why:
        return why
    if (run["topic_id"], run.get("country_set")) in posted:
        return "this topic + country set was posted before"
    why = data_through_end(run["id"])
    if why:
        return why
    if other:
        if learn.category_of(run["topic_id"]) == other["category"]:
            return f"same category ({other['category']}) as today's other post"
        shared = answer_countries(run["id"]) & set(other.get("answer") or [])
        if shared:
            return f"shares the hook answer country {', '.join(sorted(shared))} with today's other post"
    return None


# ------------------------------------------------------------------ roles and the day

def slot_label(index: int) -> str:
    return slot_times()[index - 1]


def _anchor() -> date:
    a = ap_settings().get("anchor_day") or "2026-10-27"
    return a if isinstance(a, date) else date.fromisoformat(str(a))


def role_for(day: date, index: int) -> str:
    """"exploit" or "explore": slot 1 is exploit on anchor_day and every 2nd day after it, slot 2 the days between."""
    first_exploit = (day - _anchor()).days % 2 == 0
    return "exploit" if (index == 1) == first_exploit else "explore"


def decisions_on(day: date) -> list[dict]:
    with db.db() as c:
        rows = c.execute("SELECT * FROM decisions WHERE day=? ORDER BY id", (day.isoformat(),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("breakdown", "runners_up"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        out.append(d)
    return out


def other_post(day: date, index: int, planned: list[dict] | None = None) -> dict | None:
    """Today's other slot's pick: {"run_id", "category", "answer", "role"}."""
    other = slot_label(2 if index == 1 else 1)
    rows = [d for d in (planned or []) if d["slot"] == other and d.get("run_id")]
    if not rows:
        rows = [d for d in decisions_on(day) if d["slot"] == other and d.get("run_id")]
    if not rows:
        return None
    d = rows[-1]
    return {"run_id": d["run_id"], "category": d["category"], "answer": sorted(answer_countries(d["run_id"])),
            "role": d["role"]}


# ------------------------------------------------------------------ choosing

def _row(run: dict, score: float, parts: dict, slot: str) -> dict:
    return {"run_id": run["id"], "topic_id": run["topic_id"], "category": learn.category_of(run["topic_id"]),
            "score": score, "parts": parts, "interest": float(run.get("score") or 0.0), "slot": slot}


def choose(index: int, day: date, model: learn.Model, states: dict, planned: list[dict] | None = None,
           rng: random.Random | None = None, runs: list[dict] | None = None) -> dict:
    """The decision for slot `index` on `day`: {"day", "slot", "role", "mode", "run_id", "category", "score",
    "breakdown", "runners_up", "note", "eligible", "excluded"} (run_id None when nothing can post)."""
    rng = rng or random.Random()
    slot = slot_label(index)
    role = role_for(day, index)
    other = other_post(day, index, planned)
    posted = posted_sets()
    eligible, excluded = [], {}
    for run in (pool() if runs is None else runs):
        why = blockers(run, other, posted)
        if why:
            excluded[run["id"]] = why
        else:
            eligible.append(run)
    state = lambda r: states.get(learn.category_of(r["topic_id"]), {})  # noqa: E731
    feats = {r["id"]: learn.features(r, slot=slot) for r in eligible}
    exploit = sorted((_row(r, *model.score(feats[r["id"]]), slot) for r in eligible if state(r).get("exploit")),
                     key=lambda x: (-x["score"], -x["interest"]))
    explore_runs = [r for r in eligible if state(r).get("explore")]
    draws = model.draws(rng)

    def thompson(runs_):
        return sorted((_row(r, *draws.score(feats[r["id"]]), slot) for r in runs_),
                      key=lambda x: (-x["score"], -x["interest"]))

    any_exploitable = any(s.get("posts", 0) >= int(learn.settings()["exploit_min_posts"]) for s in states.values())
    note, mode = "", role
    if not any_exploitable:
        mode = "thompson"
        live = [r for r in eligible if not (state(r).get("retired") and not state(r).get("retry_due"))] or eligible
        ranked = thompson(live)
        note = f"no category has {learn.settings()['exploit_min_posts']} scored posts yet: Thompson sampling over the pool"
    elif role == "exploit" and exploit:
        ranked = exploit
    elif role == "exploit":
        mode = "thompson"
        live = [r for r in eligible if not (state(r).get("retired") and not state(r).get("retry_due"))] or eligible
        ranked = thompson(live)
        note = "no reel of an exploited category is eligible: Thompson sampling over the pool"
    elif explore_runs:
        ranked = thompson(explore_runs)
    else:
        mode = "explore->exploit"
        if (other and other["role"] == "exploit") or len(exploit) < 2:
            ranked = exploit
            note = "no explore-eligible reel: the best remaining exploit reel posts instead"
        else:   # the best stays for today's exploit slot, so not its category either (the day's posts differ)
            second = next((x for x in exploit[1:] if x["category"] != exploit[0]["category"]), exploit[1])
            ranked = [second] + [x for x in exploit if x is not second]
            note = "no explore-eligible reel: the second-best exploit reel posts instead"
    choice = ranked[0] if ranked else None
    if choice is None and not note:
        note = "no eligible reel in the pool"
    elif choice is None:
        note += "; no eligible reel in the pool"
    return {"day": day.isoformat(), "slot": slot, "index": index, "role": role, "mode": mode,
            "run_id": choice["run_id"] if choice else None, "topic_id": choice["topic_id"] if choice else None,
            "category": choice["category"] if choice else None, "score": choice["score"] if choice else None,
            "breakdown": {"parts": choice["parts"], "interest": choice["interest"], "threshold": round(model.threshold, 1),
                          "base_rate": round(model.base, 4), "scored_posts": len(model.rows),
                          "category_state": state({"topic_id": choice["topic_id"]}) if choice else None,
                          "eligible": [r["id"] for r in eligible], "excluded": excluded} if choice else
            {"eligible": [], "excluded": excluded},
            "runners_up": [{k: x[k] for k in ("run_id", "topic_id", "category", "score", "interest")} for x in ranked[1:4]],
            "note": note, "eligible": [r["id"] for r in eligible], "excluded": excluded}


def record(decision: dict) -> int:
    with db.db() as c:
        return c.execute("INSERT INTO decisions(day, slot, role, run_id, category, score, breakdown, runners_up, note, "
                         "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (decision["day"], decision["slot"], decision["mode"], decision["run_id"], decision["category"],
                          decision["score"], json.dumps(decision["breakdown"]), json.dumps(decision["runners_up"]),
                          decision["note"], db.now())).lastrowid


def plan(day: date, indexes=(1, 2), rng: random.Random | None = None) -> list[dict]:
    """Dry run: the decisions the given slots of `day` would make now, in slot order, writing nothing."""
    model = learn.build()
    states = learn.refresh_categories(model)
    out: list[dict] = []
    for i in sorted(indexes):
        out.append(choose(i, day, model, states, planned=out, rng=rng))
    return out


def describe(d: dict) -> str:
    """A decision in plain lines (the dry run and the dashboard)."""
    from .review import topic_name
    head = f"{d['day']} {d['slot']} IST  {d['role'].upper()}" + (f" ({d['mode']})" if d["mode"] != d["role"] else "")
    if not d["run_id"]:
        return "\n".join([head, f"  nothing to post: {d['note']}"] +
                         [f"  excluded {k}: {v}" for k, v in d["excluded"].items()])
    b = d["breakdown"]
    lines = [head, f"  -> {topic_name(d['topic_id'])} [{d['category']}] {d['run_id']}  score {d['score']:+.3f}  "
                   f"interest {b['interest']:.1f}",
             f"     base breakout rate {b['base_rate']:.2f} over {b['scored_posts']} scored posts, "
             f"breakout = 72 h views >= {b['threshold']:.0f}"]
    for f, p in b["parts"].items():
        if "log_odds" in p:
            lines.append(f"     {f:<13} {str(p['value'])[:34]:<34} n={p['n']:<3} rate {p['rate']:.2f}  {p['log_odds']:+.3f}")
        else:
            lines.append(f"     {f:<13} {str(p['value'])[:34]:<34} Thompson draw {p['draw_log_odds']:+.3f}")
    for r in d["runners_up"]:
        lines.append(f"     runner-up: {topic_name(r['topic_id'])} [{r['category']}] {r['score']:+.3f}")
    if d["note"]:
        lines.append(f"     note: {d['note']}")
    for k, v in d["excluded"].items():
        lines.append(f"     excluded {k}: {v}")
    return "\n".join(lines)


# ------------------------------------------------------------------ losses, drops, media

def delete_media(run_id: str) -> int:
    """Delete a reel's video, audio and image files (its JSON stays); mark the run. Returns bytes freed."""
    freed = 0
    d = run_dir(run_id)
    for f in d.rglob("*"):
        if f.is_file() and f.suffix.lower() in MEDIA_SUFFIXES:
            try:
                freed += f.stat().st_size
                f.unlink()
            except OSError as e:
                log.warning("could not delete %s: %s", f, e)
    with db.db() as c:
        c.execute("UPDATE runs SET media_deleted_at=? WHERE id=?", (db.now(), run_id))
    return freed


def drop(run_id: str, why: str) -> None:
    run = db.get_run(run_id)
    if not run or run["stage"] not in POOL_STAGES:
        return
    db.transition(run_id, "dropped", f"dropped from the pool: {why}")
    freed = delete_media(run_id)
    log.info("pool: dropped %s (%s), %.0f MB freed", run_id, why, freed / 1e6)


def count_losses(today: date | None = None) -> list[str]:
    """For every day before `today` with decisions whose losses are not counted yet: each reel that was eligible in
    one of that day's slots and was not picked that day gets one loss (at most one per day); a reel reaching
    pool_max_losses is dropped. Returns the dropped run ids."""
    today = today or ist().date()
    limit = int(ap_settings().get("pool_max_losses", 5))
    with db.db() as c:
        days = [r["day"] for r in c.execute("SELECT DISTINCT day FROM decisions WHERE day < ? ORDER BY day",
                                            (today.isoformat(),))]
    dropped = []
    for day in days:
        key = f"autopilot:losses_counted:{day}"
        if db.kv_get(key):
            continue
        rows = decisions_on(date.fromisoformat(day))
        picked = {d["run_id"] for d in rows if d["run_id"]}
        eligible = set()
        for d in rows:
            eligible |= set((d["breakdown"] or {}).get("eligible") or [])
        for rid in sorted(eligible - picked):
            with db.db() as c:
                c.execute("UPDATE runs SET pool_losses = pool_losses + 1, pool_loss_day=? WHERE id=? AND "
                          "(pool_loss_day IS NULL OR pool_loss_day < ?) AND stage IN ('awaiting_approval','approved')",
                          (day, rid, day))
            run = db.get_run(rid)
            if run and run["stage"] in POOL_STAGES and run["pool_losses"] >= limit:
                drop(rid, f"eligible and not picked on {run['pool_losses']} days")
                dropped.append(rid)
        db.kv_set(key, db.now())
    return dropped


def cleanup_posted_media(now: datetime | None = None) -> int:
    """Delete the media of reels posted more than autopilot.posted_media_days ago (rows kept). Returns how many."""
    now = now or datetime.now(timezone.utc)
    days = float(ap_settings().get("posted_media_days", 14))
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.db() as c:
        rows = c.execute("SELECT r.id FROM runs r WHERE r.stage IN ('published','failed') AND r.media_deleted_at IS NULL "
                         "AND EXISTS (SELECT 1 FROM posts p WHERE p.run_id=r.id AND p.status IN "
                         "('live','uploaded','private_locked')) AND (SELECT MIN(created_at) FROM posts p WHERE "
                         "p.run_id=r.id AND p.status IN ('live','uploaded','private_locked')) < ?", (cutoff,)).fetchall()
    n = 0
    for r in rows:
        delete_media(r["id"])
        n += 1
    if n:
        log.info("media of %d reel(s) posted over %g days ago deleted", n, days)
    return n


# ------------------------------------------------------------------ the post job

def slot_time(index: int, now: datetime) -> datetime:
    """The most recent start of slot `index` at or before now (15 min of slack for a timer that fires early)."""
    local = ist(now)
    h, m = map(int, slot_label(index).split(":"))
    at = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if at > local + timedelta(minutes=15):
        at -= timedelta(days=1)
    return at


def last_post_time() -> datetime | None:
    from .posting import last_post
    return last_post()


def late_reason(index: int, now: datetime) -> str | None:
    """Why the late-slot guard refuses this post job, or None."""
    s = ap_settings()
    at = slot_time(index, now)
    late = (now - at.astimezone(timezone.utc)).total_seconds() / 60
    if late > float(s.get("late_minutes", 90)):
        return f"the {at:%H:%M} slot of {at:%a %d %b} started {late:.0f} min ago (more than {s.get('late_minutes', 90)})"
    last = last_post_time()
    if last and (now - last).total_seconds() / 60 < float(s.get("min_gap_minutes", 60)):
        return (f"the previous post went out {(now - last).total_seconds() / 60:.0f} min ago "
                f"(less than {s.get('min_gap_minutes', 60)})")
    return None


def _claim(key: str, what: str) -> bool:
    with db.db() as c:
        return c.execute("INSERT OR IGNORE INTO kv(key, value, updated_at) VALUES (?,?,?)",
                         (key, what, db.now())).rowcount == 1


def _approve(run: dict, slot: str, now: datetime) -> None:
    from .accounts import enabled_platforms
    from .posting import K_LAST, K_LAST_RUN, next_pos
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if run["stage"] == "awaiting_approval":
        db.transition(run["id"], "approved", f"picked by the autopilot for the {slot} slot",
                      platforms=json.dumps(enabled_platforms()), approved_by=f"autopilot {stamp}", approved_at=stamp,
                      post_pos=next_pos())
    elif not run.get("approved_by"):
        with db.db() as c:
            c.execute("UPDATE runs SET approved_by=?, approved_at=? WHERE id=?", (f"autopilot {stamp}", stamp, run["id"]))
    db.set_post_slot(run["id"], slot)
    db.kv_set(K_LAST, stamp)
    db.kv_set(K_LAST_RUN, run["id"])


def failed_platforms(run_id: str) -> dict[str, str]:
    from .accounts import account_states, destinations
    run = db.get_run(run_id)
    posts = {p["platform"]: p for p in db.get_posts(run_id)}
    out = {}
    for p in destinations(run):
        post = posts.get(p)
        if not post or post["status"] not in ("live", "uploaded", "private_locked", "dry_run"):
            acc = account_states()[p]
            why = None if acc["connected"] else f"account {acc['state']}: {acc['detail']}".strip(": ")
            out[p] = (post or {}).get("message") or why or run.get("error") or "not posted"
    return out


def refresh_accounts() -> None:
    """Re-check the enabled Meta accounts now (read-only), so the publisher does not go by a stale cached check."""
    from .accounts import enabled_platforms
    from .actions import test_account
    for p in enabled_platforms():
        if p in ("instagram", "facebook"):
            try:
                res = test_account(p)
                if res.get("state") != "ok":
                    log.warning("account check %s: %s %s", p, res.get("state"), res.get("detail"))
            except Exception as e:  # noqa: BLE001 - the publisher reports its own failure
                log.warning("account check %s failed: %s", p, e)


def publish_with_retry(run_id: str, wait_s: float = RETRY_WAIT_S) -> dict[str, str]:
    """Post now; a platform that failed is tried once more after wait_s. Returns what still failed {platform: why}."""
    from .orchestrator import publish_run
    publish_run(run_id)
    bad = failed_platforms(run_id)
    if not bad:
        return {}
    log.warning("post %s failed on %s; one retry in %.0f s", run_id, ", ".join(bad), wait_s)
    _time.sleep(wait_s)
    for p in bad:
        run = db.get_run(run_id)
        if run["stage"] != "approved":
            db.transition(run_id, "approved", f"autopilot retry of {p}", reset=True)
        publish_run(run_id, p)
    bad = failed_platforms(run_id)
    live = [p["platform"] for p in db.get_posts(run_id) if p["status"] in ("live", "uploaded", "private_locked")]
    if live and db.get_run(run_id)["stage"] != "published":
        db.transition(run_id, "published", f"published on {', '.join(live)}", reset=True)
    return bad


def post(index: int, now: datetime | None = None, rng: random.Random | None = None, wait_s: float = RETRY_WAIT_S) -> int:
    """The post job for slot 1 or 2 (run inside autopilot.run_job). Exit code 0 also when the slot is skipped."""
    from .review import topic_name
    now = now or datetime.now(timezone.utc)
    slot = slot_label(index)
    at = slot_time(index, now)
    key = f"posting:slot:{at.date().isoformat()}T{slot}"
    if db.kv_get(key):   # this slot already ran (a re-run, the loop and a timer): nothing to do, nothing to say
        log.info("post slot %d of %s already ran: nothing to do", index, at.date())
        return 0
    why = late_reason(index, now)
    if why:
        log.warning("post slot %d skipped by the late-slot guard: %s", index, why)
        alerts.fire("post_skipped", f"the {slot} IST post was skipped: {why}",
                    "the server was down or busy at slot time (uptime, journalctl -u graphony-post@*)")
        return 0
    if not _claim(key, "autopilot"):
        log.info("post slot %d of %s already ran: nothing to do", index, at.date())
        return 0
    model = learn.build(now=now)
    states = learn.refresh_categories(model, now)
    decision = choose(index, at.date(), model, states, rng=rng)
    record(decision)
    log.info("autopilot pick:\n%s", describe(decision))
    if not decision["run_id"]:
        return 0
    run = db.get_run(decision["run_id"])
    refresh_accounts()
    _approve(run, slot, now)
    bad = publish_with_retry(run["id"], wait_s)
    name = topic_name(run["topic_id"])
    if bad:
        detail = "; ".join(f"{p}: {str(w)[:120]}" for p, w in bad.items())
        alerts.fire("post_failed", f"the {slot} IST post ({name}) failed after 1 retry ({detail})",
                    "the Meta token and account (python -m pipeline health), then cache/logs/pipeline.log")
        alerts.ping(ok=False)
        return 1
    alerts.clear("post_failed")
    alerts.clear("post_skipped")
    alerts.ping(ok=True)
    log.info("autopilot posted %s (%s) in the %s slot", run["id"], name, slot)
    return 0
