"""Hook templates: the STYLE of the opening question a reel shows on screen from frame 0, kept apart from its
topic so the stats can say which hook works (analytics.update_hook_weights; the Telegram Stats screen).

  which_last     a guess-the-loser question, worded per reel from config.yaml opening.phrasings ("Which country
                 ends up last?", "Which country loses the lead?", ...; only "Which country...?" questions since
                 2026-10-23); the exact wording is kept as hook_phrase
  who_overtakes  "Who overtakes who?"            (opener overtakes in caption_formats.json)
  other          any other question, or no on-screen hook (the reels made before 2026-10-13)

which_last phrasings. Each has a truth check, computed here from the series the video shows (truths()). A
phrasing is eligible only when its check is true and it fits on screen exactly like the first phrasing does
(render.phrasing_fits: same size and lines, so the chart never moves; its end card answer fits too).
Rotation (choose()): never a phrasing shown by one of the last opening.recent_block reels (posted, then the
posting queue, then Ready for review); among the rest the least used, then config order. When every true
phrasing was used recently, the first ends_last phrasing ("Which country ends up last?") is the fallback. The
end card shows the phrasing's own answer, so the reveal always answers the question that was asked.

Rank: a topic ranks the highest value first unless topics.yaml says `rank: lower_first` (a lower number is the
better outcome, e.g. child mortality), so "last" is the worst final rank either way. Values are compared as the
video shows them (topics.format_value): a tie on screen is no answer.

The template and the phrasing are read from the question the video really shows (meta.json "opening"), stored
on the run when it is rendered, and copied onto each post when it is published (db.upsert_post). backfill()
fills them in for every reel made before the fields existed. The weights are for reporting only.
"""
from __future__ import annotations

import json
import logging
from collections import Counter

from . import db
from .config import get_config, path

log = logging.getLogger(__name__)

WHICH_LAST, WHO_OVERTAKES, OTHER = "which_last", "who_overtakes", "other"
QUESTIONS = {"Who overtakes who?": WHO_OVERTAKES}
OPENERS = {WHICH_LAST: "ends_last", WHO_OVERTAKES: "overtakes"}   # template -> opener id (caption_formats.json)
LABELS = {WHICH_LAST: "Guess the loser", WHO_OVERTAKES: "Who overtakes who?", OTHER: "Other or no hook"}
# The phrasings shipped with the code (config.yaml has the same list): only "Which country...?" questions.
DEFAULT_PHRASINGS = [   # interleaved by check, so the rotation's ties (config order) vary the question, not just its words
    {"text": "Which country ends up last?", "check": "ends_last", "answer": "{country} ends up last: {value} in {year}"},
    {"text": "Which country falls the most?", "check": "falls_most", "spatial": True,
     "answer": "{country} falls the most: {from} to {to}"},
    {"text": "Which country loses the lead?", "check": "loses_lead",
     "answer": "{country} loses the lead to {country2} in {year}"},
    {"text": "Which country finishes last?", "check": "ends_last", "answer": "{country} finishes last: {value} in {year}"},
    {"text": "Which country sinks to last?", "check": "drops_to_last", "spatial": True,
     "answer": "{country} sinks from {rank_from} in {from_year} to last in {year}"},
]
# Wordings no longer used (the "Who...?" questions, retired 2026-10-23): a reel that shows one stays which_last,
# so the hook weight is never split. Their checks (overtaken_most, loses_ground) still work if they come back.
RETIRED_PHRASINGS = ["Who drops to the bottom?", "Who gets overtaken most?", "Who loses the most ground?",
                     "Who comes last in the end?", "Who ends up at the bottom?"]


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def settings() -> dict:
    o = get_config().get("opening") or {}
    return {"hook_template": o.get("hook_template", WHICH_LAST), "hook_explore_pct": float(o.get("hook_explore_pct", 0)),
            "recent_block": int(o.get("recent_block", 3))}


def phrasings() -> list[dict]:
    """config.yaml opening.phrasings (the shipped list when there is none), in order. The first ends_last
    phrasing is the fallback."""
    raw = (get_config().get("opening") or {}).get("phrasings") or DEFAULT_PHRASINGS
    out = []
    for p in raw:
        if isinstance(p, dict) and _norm(p.get("text")) and p.get("check"):
            out.append({"index": len(out), "text": _norm(p["text"]), "check": str(p["check"]),
                        "answer": str(p.get("answer") or ""), "spatial": bool(p.get("spatial")), "fallback": False})
    fb = next((p for p in out if p["check"] == "ends_last"), None)
    if fb:
        fb["fallback"] = True
    return out


def classify(question: str | None) -> str:
    """The template of an on-screen opening question (exact wording, as drawn on the video)."""
    q = _norm(question)
    if q in {p["text"] for p in phrasings()} | {p["text"] for p in DEFAULT_PHRASINGS} | set(RETIRED_PHRASINGS):
        return WHICH_LAST
    return QUESTIONS.get(q, OTHER)


def preferred_opener() -> str | None:
    """The opener id config.yaml asks for (None for "auto": the old choice between the top two)."""
    t = settings()["hook_template"]
    return OPENERS.get(t, None if t in ("auto", "", None) else t)


# ------------------------------------------------------------------ truth checks (pure code)

def rank_sign(topic: dict) -> int:
    """+1: the highest value ranks first (the default); -1: topics.yaml `rank: lower_first`."""
    return -1 if str(topic.get("rank", "")).strip().lower() == "lower_first" else 1


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


class Ranks:
    """The video's series, ranked the topic's way and compared as the video shows the values.
    rows: {iso3: [(year, value), ...]} as rendered; fmt: the on-screen value format."""

    def __init__(self, topic: dict, rows: dict, names: dict, fmt):
        self.sign, self.fmt, self.names = rank_sign(topic), fmt, names
        self.rows = {iso: sorted(r) for iso, r in rows.items() if r}
        self.at = {iso: dict(r) for iso, r in self.rows.items()}
        self.ends = {iso: r[-1] for iso, r in self.rows.items()}
        self.end = {iso: v for iso, (_, v) in self.ends.items()}
        self.final_year = max((y for y, _ in self.ends.values()), default=None)
        years = [set(a) for a in self.at.values()]
        self.common = sorted(set.intersection(*years)) if years else []   # years every country has data
        self.start = self.common[0] if self.common else None

    def name(self, iso: str) -> str:
        return self.names.get(iso, iso)

    def ahead(self, a: float, b: float) -> bool:
        """a ranks strictly ahead of b, also on screen (two values the video shows the same are a tie)."""
        return self.sign * a > self.sign * b and self.fmt(a) != self.fmt(b)

    def rank(self, iso: str, vals: dict) -> int:
        return 1 + sum(1 for j in vals if j != iso and self.ahead(vals[j], vals[iso]))

    def tied(self, iso: str, vals: dict) -> bool:
        return any(j != iso and self.fmt(vals[j]) == self.fmt(vals[iso]) for j in vals)

    def at_year(self, year: int) -> dict:
        return {iso: self.at[iso][year] for iso in self.rows}

    def leader(self, vals: dict) -> str | None:
        best = max(vals, key=lambda i: self.sign * vals[i])
        return None if any(j != best and not self.ahead(vals[best], vals[j]) for j in vals) else best

    def stops_early(self, iso: str) -> str | None:
        year = self.ends[iso][0]
        return None if year == self.final_year else \
            f"{self.name(iso)}'s data stops in {year}, before the video's final year {self.final_year}"


def _no(why: str) -> dict:
    return {"ok": False, "why": why}


def _yes(fields: dict, fact: str, highlight: list[str]) -> dict:
    return {"ok": True, "why": None, "fields": fields, "fact": fact, "highlight": highlight}


def _last(r: Ranks) -> tuple[str | None, str | None]:
    """(the country with the worst final rank, None) or (None, why there is no clear one). It must reach the
    video's final year and end strictly behind every other country, also as the video shows it. A country
    whose data stops a year early counts with its last value."""
    if len(r.end) < 3:
        return None, f"only {len(r.end)} countries: there is no real last place"
    iso = min(r.end, key=lambda i: r.sign * r.end[i])
    early = r.stops_early(iso)
    if early:
        return None, early
    nxt = min((i for i in r.end if i != iso), key=lambda i: r.sign * r.end[i])
    if not r.ahead(r.end[nxt], r.end[iso]):
        return None, f"tie: two countries end at {r.fmt(r.end[iso])}"
    return iso, None


def _ends_last(r: Ranks) -> dict:
    iso, why = _last(r)
    if not iso:
        return _no(why)
    year, value = r.ends[iso]
    nxt = min((i for i in r.end if i != iso), key=lambda i: r.sign * r.end[i])
    word = "lowest" if r.sign > 0 else "highest (lower is better)"
    return _yes({"country": r.name(iso), "value": r.fmt(value), "year": year},
                f"{r.name(iso)} ends {word} of {len(r.end)}: {r.fmt(value)} in {year}; next {r.name(nxt)} at "
                f"{r.fmt(r.end[nxt])}", [r.name(iso)])


def _falls_most(r: Ranks) -> dict:
    if len(r.rows) < 3:
        return _no(f"only {len(r.rows)} countries")
    fall = {i: rows[0][1] - rows[-1][1] for i, rows in r.rows.items()}
    a, b = sorted(fall, key=lambda i: fall[i], reverse=True)[:2]
    (y0, v0), (y1, v1) = r.rows[a][0], r.rows[a][-1]
    values = [v for rows in r.rows.values() for _, v in rows]
    span = (max(values) - min(values)) or 1.0
    if fall[a] <= 0 or r.fmt(v0) == r.fmt(v1):
        return _no("no country falls from its first year to its last")
    if fall[a] < 0.02 * span:
        return _no(f"the biggest fall ({r.name(a)}) is under 2% of the chart's range")
    if r.stops_early(a):
        return _no(r.stops_early(a))
    if fall[b] >= fall[a] or r.fmt(fall[a]) == r.fmt(fall[b]):
        return _no(f"tie: {r.name(a)} and {r.name(b)} fall about as much")
    (b0, bv0), (b1, bv1) = r.rows[b][0], r.rows[b][-1]
    return _yes({"country": r.name(a), "from": r.fmt(v0), "to": r.fmt(v1), "from_year": y0, "to_year": y1},
                f"{r.name(a)} falls the most: {r.fmt(v0)} in {y0} to {r.fmt(v1)} in {y1}; next {r.name(b)}, "
                f"{r.fmt(bv0)} to {r.fmt(bv1)}", [r.name(a)])


def _drops_to_last(r: Ranks) -> dict:
    iso, why = _last(r)
    if not iso:
        return _no(why)
    if r.start is None:
        return _no("there is no year in which every country has data")
    start = r.at_year(r.start)
    if r.tied(iso, start):
        return _no(f"{r.name(iso)} is tied on screen in {r.start}")
    k = r.rank(iso, start)
    if k == len(start):
        return _no(f"{r.name(iso)} is already last in {r.start}, the first year every country has data")
    year, value = r.ends[iso]
    return _yes({"country": r.name(iso), "rank_from": ordinal(k), "from_year": r.start, "value": r.fmt(value),
                 "year": year},
                f"{r.name(iso)} is {ordinal(k)} of {len(start)} in {r.start} ({r.fmt(start[iso])}) and last in {year} "
                f"({r.fmt(value)})", [r.name(iso)])


def _loses_lead(r: Ranks) -> dict:
    if len(r.rows) < 3:
        return _no(f"only {len(r.rows)} countries")
    first = r.leader(r.end)
    if not first:
        return _no("no clear leader at the end (a tie on screen)")
    if r.stops_early(first):
        return _no(r.stops_early(first))
    leads = [(y, r.leader(r.at_year(y))) for y in r.common]
    former = sorted({who for _, who in leads if who and who != first})
    if not former:
        return _no(f"no country but {r.name(first)} ever leads")
    if len(former) > 1:
        return _no(f"the lead changes hands between {', '.join(r.name(i) for i in former)} and "
                   f"{r.name(first)}: no single country loses it")
    a = former[0]
    last = max(k for k, (_, who) in enumerate(leads) if who == a)
    if last + 1 >= len(leads):
        return _no(f"{r.name(a)} still leads in {leads[last][0]}, the last year every country has data")
    year, who = leads[last + 1]
    if who != first:
        return _no(f"nobody leads clearly in {year}, the year after {r.name(a)} last leads")
    since = next(y for y, w in leads if w == a)
    return _yes({"country": r.name(a), "country2": r.name(first), "year": year},
                f"{r.name(a)} leads in {since}" + (f" to {leads[last][0]}" if leads[last][0] != since else "") +
                f"; {r.name(first)} leads from {year} and ends first ({r.fmt(r.end[first])}); no other country "
                f"ever leads", [r.name(a), r.name(first)])


def _overtaken_most(r: Ranks) -> dict:
    if r.start is None:
        return _no("there is no year in which every country has data")
    enders = [i for i in r.rows if not r.stops_early(i)]
    if len(enders) < 3:
        return _no(f"only {len(enders)} countries reach the final year")
    start = r.at_year(r.start)
    passed = {c: [d for d in enders if d != c and r.ahead(start[c], start[d]) and r.ahead(r.end[d], r.end[c])]
              for c in enders}
    a, b = sorted(enders, key=lambda c: len(passed[c]), reverse=True)[:2]
    n = len(passed[a])
    if n < 2:
        return _no(f"no country is overtaken by more than {n} other")
    if len(passed[b]) == n:
        return _no(f"tie: {r.name(a)} and {r.name(b)} are both overtaken by {n}")
    return _yes({"country": r.name(a), "n": n},
                f"{r.name(a)} is ahead of {', '.join(r.name(d) for d in passed[a])} in {r.start} and behind them all in "
                f"{r.final_year}", [r.name(a)])


def _loses_ground(r: Ranks) -> dict:
    if len(r.rows) < 3:
        return _no(f"only {len(r.rows)} countries")
    if r.start is None:
        return _no("there is no year in which every country has data")
    start = r.at_year(r.start)
    if any(r.tied(i, start) or r.tied(i, r.end) for i in r.rows):
        return _no(f"two countries are tied on screen in {r.start} or at the end")
    drop = {i: r.rank(i, r.end) - r.rank(i, start) for i in r.rows}
    a, b = sorted(drop, key=lambda i: drop[i], reverse=True)[:2]
    if drop[a] < 2:
        return _no(f"no country drops more than {max(drop[a], 0)} place(s)")
    if drop[b] == drop[a]:
        return _no(f"tie: {r.name(a)} and {r.name(b)} both drop {drop[a]} places")
    if r.stops_early(a):
        return _no(r.stops_early(a))
    k0, k1 = r.rank(a, start), r.rank(a, r.end)
    return _yes({"country": r.name(a), "rank_from": ordinal(k0), "rank_to": ordinal(k1), "from_year": r.start,
                 "year": r.final_year},
                f"{r.name(a)} drops {drop[a]} places, {ordinal(k0)} of {len(start)} in {r.start} to {ordinal(k1)} at "
                f"the end; next {r.name(b)}, {max(drop[b], 0)}", [r.name(a)])


CHECKS = {"ends_last": _ends_last, "falls_most": _falls_most, "drops_to_last": _drops_to_last,
          "loses_lead": _loses_lead, "overtaken_most": _overtaken_most, "loses_ground": _loses_ground}


def truths(topic: dict, rows: dict, names: dict, fmt) -> dict[str, dict]:
    """{check id: {"ok", "why", "fields", "fact", "highlight"}} for the series the video shows."""
    r = Ranks(topic, rows, names, fmt)
    out = {}
    for k, fn in CHECKS.items():
        try:
            out[k] = fn(r)
        except Exception as e:  # noqa: BLE001 - a broken check makes its phrasings ineligible, never a render fail
            log.warning("hook check %s failed: %s", k, e)
            out[k] = _no(f"the check failed ({type(e).__name__}: {e})")
    return out


# ------------------------------------------------------------------ rotation

def _phrase_of(run: dict) -> str | None:
    if run.get("hook_phrase"):
        return run["hook_phrase"]
    if run.get("hook_template") != WHICH_LAST:
        return None
    o = (_meta(run["id"]) or {}).get("opening") or {}   # made before hook_phrase existed
    return _norm(o.get("question")) or None


def phrase_history(exclude: str | None = None) -> list[str | None]:
    """The hook_phrase of every reel in posting order: posted (by its first post), then the posting queue, then
    Ready for review, then reels rendered but not in review yet. None for a reel that is not which_last."""
    from .posting import queue as posting_queue
    from .review import queue as review_queue
    with db.db() as c:
        posted = c.execute("SELECT r.* FROM runs r JOIN posts p ON p.run_id = r.id WHERE p.status IN "
                           "('live','uploaded','private_locked') GROUP BY r.id ORDER BY MIN(p.created_at), MIN(p.id)"
                           ).fetchall()
        made = c.execute("SELECT * FROM runs WHERE stage='rendered' ORDER BY updated_at, rowid").fetchall()
    seen, out = set(), []
    for run in [dict(r) for r in posted] + posting_queue() + review_queue() + [dict(r) for r in made]:
        if run["id"] != exclude and run["id"] not in seen:
            seen.add(run["id"])
            out.append(_phrase_of(run))
    return out


def choose(eligible: list[dict], history: list[str | None]) -> tuple[dict, str]:
    """The phrasing for a new reel and why: not one shown by the last recent_block reels; among the rest the
    least used, then config order. When all were shown recently: the fallback, else the least recently shown."""
    n = settings()["recent_block"]
    recent = set(history[-n:]) if n > 0 else set()
    used = Counter(h for h in history if h)
    fresh = [p for p in eligible if p["text"] not in recent]
    if fresh:
        p = min(fresh, key=lambda p: (used[p["text"]], p["index"]))
        return p, (f"rotation: the least used of {len(fresh)} true phrasings not shown by the last {n} reels "
                   f"(used {used[p['text']]}x before)")
    fb = next((p for p in eligible if p["fallback"]), None)
    if fb:
        return fb, f"fallback: every true phrasing was shown by one of the last {n} reels"
    last = {h: k for k, h in enumerate(history)}
    p = min(eligible, key=lambda p: (last.get(p["text"], -1), p["index"]))
    return p, f"least recent: every true phrasing was shown by one of the last {n} reels and the fallback is not true"


def which_last_opening(topic: dict, rows: dict, names: dict, fmt, history: list[str | None],
                       fits=None) -> tuple[dict | None, dict[str, str]]:
    """(the which_last opening for this reel, or None; why each phrasing that was not eligible was not).
    fits(question, answer) -> None, or why it would not fit on screen (render.phrasing_fits)."""
    t = truths(topic, rows, names, fmt)
    lower_first = rank_sign(topic) < 0
    eligible, why_not = [], {}
    for p in phrasings():
        res = t.get(p["check"])
        if res is None:
            why_not[p["text"]] = f"unknown check {p['check']!r}"
            continue
        if p["spatial"] and lower_first:
            why_not[p["text"]] = "uses up/down words, but this topic ranks the lowest value first"
            continue
        if not res["ok"]:
            why_not[p["text"]] = res["why"]
            continue
        try:
            answer = p["answer"].format(**res["fields"])
        except (KeyError, IndexError, ValueError) as e:
            why_not[p["text"]] = f"its answer {p['answer']!r} needs a field the {p['check']} check does not give ({e})"
            continue
        if not answer:
            why_not[p["text"]] = "it has no end card answer"
            continue
        bad = fits(p["text"], answer) if fits else None
        if bad:
            why_not[p["text"]] = bad
            continue
        eligible.append({**p, "answer_text": answer, "truth": res})
    if not eligible:
        return None, why_not
    p, how = choose(eligible, history)
    return {"id": WHICH_LAST, "check": p["check"], "question": p["text"], "answer": p["answer_text"],
            "highlight": p["truth"]["highlight"], "truth": p["truth"]["fact"], "hook_template": WHICH_LAST,
            "hook_phrase": p["text"], "choice": how, "eligible": [e["text"] for e in eligible],
            "not_eligible": why_not}, why_not


# ------------------------------------------------------------------ reels made before the fields existed

def _meta(run_id: str) -> dict | None:
    try:
        return json.loads((path("out") / run_id / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def of_meta(meta: dict) -> tuple[str, str | None]:
    """(hook_template, hook_phrase) of a video: the template stored when it was rendered, else the one its
    on-screen question belongs to; the phrase is that question for a which_last reel."""
    o = meta.get("opening") or {}
    tpl = o.get("hook_template") or meta.get("hook_template") or classify(o.get("question"))
    return tpl, (_norm(o.get("question")) or None) if tpl == WHICH_LAST else None


def of_run(run_id: str) -> str | None:
    """The template of a run's video, from its meta.json; None when the run has no video."""
    meta = _meta(run_id)
    return of_meta(meta)[0] if meta is not None else None


def backfill() -> list[dict]:
    """hook_template and hook_phrase for every run with a video (and its posts), from the question its video
    shows. Idempotent: only rows that differ are written. Returns [{"run_id", "topic_id", "stage", "question",
    "hook_template", "hook_phrase", "changed"}] for every run with a video."""
    out = []
    for run in sorted(db.list_runs(limit=100000), key=lambda r: r["ref"]):
        meta = _meta(run["id"])
        if meta is None:
            continue
        tpl, phrase = of_meta(meta)
        posts = db.get_posts(run["id"])
        changed = (run.get("hook_template"), run.get("hook_phrase")) != (tpl, phrase) or \
            any((p.get("hook_template"), p.get("hook_phrase")) != (tpl, phrase) for p in posts)
        if changed:
            db.set_hook_template(run["id"], tpl, phrase)
        out.append({"run_id": run["id"], "topic_id": run["topic_id"], "stage": run["stage"],
                    "question": (meta.get("opening") or {}).get("question"), "hook_template": tpl,
                    "hook_phrase": phrase, "changed": changed})
    n = sum(1 for r in out if r["changed"])
    if n:
        log.info("hook_template / hook_phrase filled in for %d run(s)", n)
    return out
