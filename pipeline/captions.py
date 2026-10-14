"""Post captions and hashtags.

Code computes the story facts from the video's data: the most surprising country, the biggest rise and
the biggest fall, the crossover where two countries swap rank and stay swapped, a sharp one-year move,
a peak followed by a fall, and the record high and low. ONLY these facts may put a number into a caption.

Gemini (optional; every call goes through gemini.ask_json, which falls back to the next model at once
when one is overloaded) picks hook templates from caption_formats.json and fills them with those
facts; it never supplies a number or a fact. Every variant is checked in code:
the hook must follow its template word for word, every slot must hold a country, year or value from
the facts it cites, every number must come from the facts, no em dashes, length limits, at most
captions.max_emoji emoji. A failed variant is asked for again (captions.llm_retries), then filled from
its template without the LLM.

A caption is three lines, then the data credit (CC BY 4.0 needs it) and the hashtags:
  1. the hook: a template filled with a computed fact (<= captions.hook_max_chars)
  2. one plain sentence naming the metric, the countries and the years (built in code, for search)
  3. a short question, to invite comments
Hashtags come from hashtags.json: captions.hashtags.count tags (default 1 broad, 2 niche, then topic and
country tags), never a banned one. captions.trending (off by default) re-ranks the topic tags by how
active their top posts are, through the Instagram hashtag search (30 unique tags per 7 days).

Output: out/<run_id>/captions.json (facts, variants, hashtags, the chosen variant). The review card
shows the variants and lets the owner choose one. Posting uses the chosen one only when
captions.use_for_posting is true; otherwise the card is a preview and the post keeps meta.json's caption.

The owner may also rewrite the caption and hashtags (dashboard, or Edit caption on the Telegram card):
save_edit() blocks an em dash, more than Instagram's 2,200 characters (with the credit and hashtags) or
30 hashtags, and WARNS about a number that is not in the computed facts. The generated variants stay in
captions.json next to the edit ("edit", every saved one in "edits"). The data credit is never part of the
editable text: compose() appends it when the post is made, so no edit can remove it.
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import statistics
import string
import zlib
from datetime import datetime, timedelta, timezone

from .config import ROOT, countries as config_countries, country_by_iso3, get_config, path, run_dir, secret
from .topics import format_value, get_topic, source_label

log = logging.getLogger(__name__)

FORMATS_FILE = ROOT / "caption_formats.json"
HASHTAGS_FILE = ROOT / "hashtags.json"
OUT_NAME = "captions.json"
TREND_NAME = "hashtag_trends.json"
IG_CAPTION_LIMIT = 2200
DEFAULTS = {"enabled": True, "variants": 3, "hook_max_chars": 90, "question_max_chars": 80, "max_emoji": 1,
            "llm": True, "llm_retries": 2, "use_for_posting": False,
            "hashtags": {"count": 5, "broad": 1, "niche": 2},
            "trending": {"enabled": False, "weekly_search_budget": 20, "cache_days": 7, "recent_days": 30}}
THE = {"United States", "United Kingdom", "Netherlands", "Philippines"}
FACT_ORDER = ["surprising", "biggest_rise", "biggest_fall", "crossover", "sharpest", "peak", "record_high", "record_low",
              "final_high", "final_low"]


def settings() -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in (get_config().get("captions") or {}).items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def enabled() -> bool:
    return bool(settings()["enabled"])


# ------------------------------------------------------------------ story facts (pure code)

def _rows(series: dict, isos: list[str], x_start: int, x_end: int) -> dict[str, list[tuple[int, float]]]:
    out = {}
    for iso in isos:
        rows = sorted((int(y), float(v)) for y, v in series.get(iso) or []
                      if v is not None and x_start <= int(y) <= x_end and math.isfinite(float(v)))
        if len(rows) >= 2:
            out[iso] = rows
    return out


def _sign(x: float, tol: float = 0.0) -> int:
    return 1 if x > tol else -1 if x < -tol else 0


def compute_facts(topic: dict, countries: list[tuple[str, str]], series: dict, x_start: int, x_end: int) -> list[dict]:
    """countries: [(iso3, name)] in play order; series: {iso3: [[year, value], ...]}. Values in the
    facts are display strings made by topics.format_value, exactly as the video shows them."""
    names = dict(countries)
    rows = _rows(series, [iso for iso, _ in countries], x_start, x_end)
    if not rows:
        return []
    allv = [v for r in rows.values() for _, v in r]
    span = (max(allv) - min(allv)) or 1.0
    fmt = lambda v: format_value(topic, v)  # noqa: E731
    found: dict[str, dict] = {}

    def add(kind: str, iso: str, dirs=(), **f):
        fact = {"kind": kind, "iso3": iso, "country": names[iso], "dirs": sorted(dirs)}
        for k, v in f.items():
            fact[k] = fmt(v) if k in ("from", "to", "value") else v
        found[kind] = fact

    first_last = {iso: (r[0], r[-1]) for iso, r in rows.items()}
    net = {iso: (r[-1][1] - r[0][1]) / span for iso, r in rows.items()}

    def span_fields(iso):
        (fy, fv), (ty, tv) = first_last[iso]
        return {"from_year": fy, "to_year": ty, "from": fv, "to": tv}

    if len(net) >= 3:
        med = statistics.median(net.values())
        iso = max(net, key=lambda i: (abs(net[i] - med), abs(net[i])))
        s, ms = _sign(net[iso], 0.02), _sign(med, 0.02)
        if s:
            others = [i for i in net if i != iso]
            against = bool(ms) and s != ms
            n_other_way = sum(1 for i in others if _sign(net[i], 0.02) == -s)
            only = against and not any(_sign(net[i], 0.02) == s for i in others)
            add("surprising", iso, {"up" if s > 0 else "down"}, against_trend=against, only=only,
                n_others=len(others), n_other_way=n_other_way, **span_fields(iso))
    up, down = max(net, key=net.get), min(net, key=net.get)
    if net[up] > 0.02:
        add("biggest_rise", up, {"up"}, **span_fields(up))
    if net[down] < -0.02:
        add("biggest_fall", down, {"down"}, **span_fields(down))

    best = None   # crossover: a starts below b, ends above it, and stays above from `year` on
    for a in rows:
        va = dict(rows[a])
        for b in rows:
            if a == b:
                continue
            vb = dict(rows[b])
            common = sorted(set(va) & set(vb))
            if len(common) < 3:
                continue
            d = [va[y] - vb[y] for y in common]
            if not (d[0] < 0 < d[-1]):
                continue
            k = len(d) - 1
            while k > 0 and d[k - 1] > 0:
                k -= 1
            margin = d[-1] / span
            if margin >= 0.05 and (best is None or margin > best[0]):
                best = (margin, a, b, common[k], common[k - 1])
    if best:
        _, a, b, year, before = best
        add("crossover", a, {"up"}, country2=names[b], iso3_2=b, year=year, from_year=before)

    best = None   # sharpest: a one-year move that is big for the country itself AND on the shared scale
    for iso, r in rows.items():
        vals = [v for _, v in r]
        own = (max(vals) - min(vals)) or 1.0
        for (y0, v0), (y1, v1) in zip(r, r[1:]):
            if y1 - y0 > 2:
                continue
            move_own, move_all = abs(v1 - v0) / own, abs(v1 - v0) / span
            key = (math.sqrt(move_own * move_all), move_all)
            if best is None or key > best[0]:
                best = (key, iso, y0, v0, y1, v1)
    if best and best[0][0] >= 0.1:
        _, iso, y0, v0, y1, v1 = best
        add("sharpest", iso, {"up" if v1 > v0 else "down"}, year=y1, from_year=y0, to_year=y1, **{"from": v0, "to": v1})

    best = None   # peak: a country's own high, followed by the biggest fall to the last year
    for iso, r in rows.items():
        k = max(range(len(r)), key=lambda i: r[i][1])
        if k >= len(r) - 1:
            continue
        own = (max(v for _, v in r) - min(v for _, v in r)) or 1.0
        drop = r[k][1] - r[-1][1]
        if drop / own >= 0.3 and (best is None or drop / span > best[0]):
            best = (drop / span, iso, r[k], r[-1])
    if best:
        _, iso, (py, pv), (ly, lv) = best
        add("peak", iso, {"up", "down"}, year=py, value=pv, from_year=py, to_year=ly, **{"from": pv, "to": lv})

    hi = max(((iso, y, v) for iso, r in rows.items() for y, v in r), key=lambda t: t[2])
    lo = min(((iso, y, v) for iso, r in rows.items() for y, v in r), key=lambda t: t[2])
    add("record_high", hi[0], year=hi[1], value=hi[2])
    add("record_low", lo[0], year=lo[1], value=lo[2])
    if len(rows) >= 2:   # where each country ends: its own last data year
        top = max(rows, key=lambda iso: rows[iso][-1][1])
        bottom = min(rows, key=lambda iso: rows[iso][-1][1])
        add("final_high", top, year=rows[top][-1][0], value=rows[top][-1][1])
        add("final_low", bottom, year=rows[bottom][-1][0], value=rows[bottom][-1][1])

    facts = [found[k] for k in FACT_ORDER if k in found]
    for i, f in enumerate(facts, 1):
        f["id"] = f"F{i}"
        f["text"] = fact_text(f)
    return facts


def fact_text(f: dict) -> str:
    c, k = f["country"], f["kind"]
    if k == "surprising":
        moved = "rose" if "up" in f["dirs"] else "fell"
        span = f"from {f['from']} in {f['from_year']} to {f['to']} in {f['to_year']}"
        if f.get("only"):
            return f"{c} is the only country that {moved}: {span}"
        if f.get("against_trend"):
            other = "fell" if moved == "rose" else "rose"
            return f"{c} {moved} {span}, while {f['n_other_way']} of the other {f['n_others']} {other}"
        return f"{c} moved furthest from the pack: {span}"
    if k in ("biggest_rise", "biggest_fall"):
        word = "rose" if k == "biggest_rise" else "fell"
        return f"{c} {word} the most: from {f['from']} in {f['from_year']} to {f['to']} in {f['to_year']}"
    if k == "crossover":
        return f"{c} overtook {f['country2']} in {f['year']} and stayed ahead"
    if k == "sharpest":
        return f"{c}'s biggest one-year move: from {f['from']} in {f['from_year']} to {f['to']} in {f['year']}"
    if k == "peak":
        return f"{c} peaked at {f['value']} in {f['year']}, then fell to {f['to']} by {f['to_year']}"
    if k == "record_high":
        return f"The highest value in the video: {c}, {f['value']} in {f['year']}"
    if k == "record_low":
        return f"The lowest value in the video: {c}, {f['value']} in {f['year']}"
    if k == "final_high":
        return f"{c} ends highest: {f['value']} in {f['year']}"
    return f"{c} ends lowest (last): {f['value']} in {f['year']}"


def _fact_fields(f: dict) -> dict:
    return {"country": f["country"], "country2": f.get("country2"), "year": f.get("year") or f.get("to_year"),
            "from_year": f.get("from_year"), "to_year": f.get("to_year"), "from": f.get("from"), "to": f.get("to"),
            "value": f.get("value"), "verb": "rose" if "up" in f.get("dirs", []) else "fell"}


def opening(facts: list[dict], seed: int = 0, formats: dict | None = None) -> dict | None:
    """The question the video opens with and the answer its end card reveals. Both come from
    caption_formats.json "openers" filled with a computed fact (no LLM); the first openers whose
    fact exists are preferred, and `seed` varies the choice between the top two."""
    formats = formats or load_formats()
    avail = []
    for o in formats.get("openers", []):
        for f in facts:
            if o["fact"] == f["kind"] or (o["fact"] == "surprising_only" and f["kind"] == "surprising" and f.get("only")):
                fields = _fact_fields(f)
                names = [n for _, n, _, _ in string.Formatter().parse(o["answer"]) if n]
                if all(fields.get(n) not in (None, "") for n in names):
                    avail.append((o, f, o["answer"].format(**fields)))
                break
    if not avail:
        return None
    o, f, answer = avail[seed % min(2, len(avail))]
    return {"id": o["id"], "question": o["question"], "answer": answer, "fact": f,
            "highlight": [n for n in (f["country"], f.get("country2")) if n]}


def fact_years(f: dict) -> set[str]:
    return {str(f[k]) for k in ("year", "from_year", "to_year") if f.get(k) is not None}


def fact_values(f: dict) -> set[str]:
    return {str(f[k]) for k in ("from", "to", "value") if f.get(k)}


def fact_numbers(f: dict) -> set[str]:
    nums = set(fact_years(f))
    for v in fact_values(f):
        nums.update(numbers_in(v))
    nums.update(str(f[k]) for k in ("n_others", "n_other_way") if f.get(k) is not None)
    return nums


def numbers_in(text: str) -> list[str]:
    return [t.replace(",", "") for t in re.findall(r"\d+(?:[.,]\d+)*", text)]


def metric_line(topic: dict, names: list[str], x_start: int, x_end: int) -> str:
    """Line 2: the metric, the countries and the years, in plain words (for search and topic classification)."""
    sub = topic["subtitle"]
    if len(sub) > 1 and sub[0].isupper() and sub[1].islower():
        sub = sub[0].lower() + sub[1:]
    listed = [f"the {n}" if n in THE else n for n in names]
    who = listed[0] if len(listed) == 1 else ", ".join(listed[:-1]) + " and " + listed[-1]
    return f"{topic['name']}: {sub} in {who}, {x_start} to {x_end}."


def credit_lines(topic: dict, data: dict) -> list[str]:
    src = "World Bank WDI" if topic["source"] == "worldbank" else source_label(topic)
    lines = [f"Data: {src} (CC BY 4.0)."]
    if topic["source"] == "owid" and data.get("citation"):
        lines.append(f"Original sources: {data['citation']}.")
    return lines


# ------------------------------------------------------------------ templates and validation

SLOT = re.compile(r"\[([^\[\]]+)\]")
DASHES = ("—", "―", "–", " -- ")
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿㊗㊙]")
RISE = {"rose", "rise", "rises", "rising", "risen", "climb", "climbed", "climbs", "climbing", "soar", "soared",
        "soaring", "jump", "jumped", "jumps", "surge", "surged", "grew", "grow", "grows", "growing", "boom",
        "boomed", "booming", "skyrocket", "skyrocketed", "exploded", "up", "higher", "doubled", "tripled", "spike",
        "spiked", "increase", "increased", "gained"}
FALL = {"fell", "fall", "falls", "falling", "fallen", "drop", "dropped", "drops", "dropping", "crash", "crashed",
        "plunge", "plunged", "plummet", "plummeted", "sank", "sink", "sinking", "collapse", "collapsed", "shrank",
        "shrink", "shrinking", "dive", "dived", "dove", "tank", "tanked", "down", "lower", "halved", "decline",
        "declined", "decrease", "decreased", "slump", "slumped", "lost"}
SUPERLATIVE = {"highest": {"record_high", "peak"}, "maximum": {"record_high", "peak"}, "top": {"record_high", "peak"},
               "peak": {"record_high", "peak"}, "peaked": {"peak"}, "lowest": {"record_low"},
               "minimum": {"record_low"}, "bottom": {"record_low"}, "record": {"record_high", "record_low"},
               "most": {"biggest_rise", "biggest_fall", "sharpest", "surprising"},
               "biggest": {"biggest_rise", "biggest_fall", "sharpest"},
               "largest": {"biggest_rise", "biggest_fall", "sharpest"},
               "fastest": {"biggest_rise", "biggest_fall", "sharpest"},
               "sharpest": {"sharpest"}, "steepest": {"sharpest"},
               "overtook": {"crossover"}, "overtakes": {"crossover"}, "passed": {"crossover"}}
UNVERIFIABLE = {"never", "always", "ever", "every", "all", "first", "last", "least", "smallest", "best", "worst",
                "entire", "nobody", "everyone", "whole", "historic", "unprecedented", "because", "due", "thanks",
                "caused", "causes", "cause", "blame", "war", "wars", "covid", "pandemic", "lockdown", "lockdowns",
                "crisis", "recession", "revolution", "election", "elections", "sanctions", "government", "policy",
                "policies", "law", "laws", "ban", "banned", "million", "millions", "billion", "billions",
                "thousand", "thousands", "hundred", "hundreds", "dozen", "twice", "thrice", "triple", "half",
                "percent", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"}


def load_formats() -> dict:
    d = json.loads(FORMATS_FILE.read_text(encoding="utf-8"))
    return {"hooks": [h for h in d.get("hooks", []) if h.get("id") and h.get("text")],
            "questions": [q for q in d.get("questions", []) if q.get("id") and q.get("text")],
            "openers": [o for o in d.get("openers", []) if o.get("id") and o.get("question") and o.get("answer")]}


def clean(text) -> str:
    if not isinstance(text, str):
        return ""
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(t.split())


def _core(text: str) -> str:
    """The text without trailing punctuation or emoji (a hook may end with '.', '!' or one emoji)."""
    t = clean(text)
    while t and (t[-1] in ".!?… ️‍" or EMOJI.match(t[-1])):
        t = t[:-1]
    return t


def _slot_kind(name: str) -> str:
    n = name.strip().lower()
    if n.startswith("country"):
        return "country"
    if n.startswith("year"):
        return "year"
    if n in ("x", "y", "value", "from", "to"):
        return "value"
    if n == "metric":
        return "metric"
    return "free"


def _literal(s: str) -> str:
    """A template's fixed words as a regex: exact words, any run of whitespace between them."""
    if not s:
        return ""
    parts = s.split()
    if not parts:
        return r"\s+"
    lead = r"\s+" if s[0].isspace() else ""
    trail = r"\s+" if s[-1].isspace() else ""
    return lead + r"\s+".join(re.escape(p) for p in parts) + trail


def template_regex(text: str, country_names: list[str]):
    alt = "|".join(re.escape(n) for n in sorted(country_names, key=len, reverse=True)) or r"(?!x)x"
    out, slots, pos = [], [], 0
    core = _core(text)
    for m in SLOT.finditer(core):
        out.append(_literal(core[pos:m.start()]))
        kind, g = _slot_kind(m.group(1)), f"s{len(slots)}"
        slots.append((g, m.group(1).strip(), kind))
        if kind == "country":
            out.append(rf"(?:the\s+)?(?P<{g}>{alt})")
        elif kind == "year":
            out.append(rf"(?P<{g}>\d{{4}})")
        else:
            out.append(rf"(?P<{g}>.+?)")
        pos = m.end()
    out.append(_literal(core[pos:]))
    return re.compile("".join(out), re.I), slots


def count_emoji(text: str) -> int:
    return len(EMOJI.findall(text))


class Context:
    """Everything a variant is checked against, for one video."""

    def __init__(self, topic: dict, countries: list[tuple[str, str]], facts: list[dict], x_start: int, x_end: int,
                 formats: dict | None = None, s: dict | None = None):
        self.topic, self.facts, self.x_start, self.x_end = topic, facts, x_start, x_end
        self.s = s or settings()
        self.formats = formats or load_formats()
        self.names = [n for _, n in countries]
        self.by_lower = {n.lower(): n for n in self.names}
        self.others = [c["name"] for c in config_countries() if c["name"] not in self.names]
        self.facts_by_id = {f["id"]: f for f in facts}
        self.hooks = {h["id"]: h for h in self.formats["hooks"]}
        self.questions = {q["id"]: q for q in self.formats["questions"]}
        self.metric_names = {topic.get("name", "").lower(), topic.get("subtitle", "").lower()} - {""}
        self.allowed = {str(x_start), str(x_end)}
        for f in facts:
            self.allowed |= fact_numbers(f)
        self.allowed.update(numbers_in(topic.get("subtitle", "")) + numbers_in(topic.get("name", "")))
        self.metric = metric_line(topic, self.names, x_start, x_end)

    def usable(self, hook: dict, fact: dict) -> bool:
        kinds = hook.get("facts") or ["any"]
        if "any" in kinds:
            return True
        if fact["kind"] == "surprising" and fact.get("only") and "surprising_only" in kinds:
            return True
        return fact["kind"] in kinds

    def suitable_hooks(self) -> list[dict]:
        return [h for h in self.formats["hooks"] if any(self.usable(h, f) for f in self.facts)]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z']+", text.lower()))


def _check_slots(m, slots, ctx: Context, refs: list[dict], line: str, question: bool = False) -> str | None:
    """Slots of a matched hook must hold what its cited facts say. A question is neutral, so its
    slots only need a country from the video and a year inside the video's range."""
    named = [ctx.by_lower[m.group(g).lower()] for g, _, kind in slots if kind == "country"]
    if len(named) != len(set(named)):
        return f"{line}: the same country fills two slots"
    if question:
        pool = ctx.facts
        years = {str(y) for y in range(ctx.x_start, ctx.x_end + 1)}
    else:
        pool = [f for f in refs if not named or f["country"] in named or f.get("country2") in named]
        if named and not pool:
            return f"{line}: {', '.join(named)} is not in the facts it cites"
        years = set().union(*(fact_years(f) for f in pool)) if pool else set()
    values = {v.lower() for f in pool for v in fact_values(f) if not named or f["country"] in named}
    numbers = set().union(*(fact_numbers(f) for f in pool)) if pool else set()
    dirs = set().union(*(set(f["dirs"]) for f in pool)) if pool else set()
    kinds = {f["kind"] for f in pool}
    for g, name, kind in slots:
        text = m.group(g).strip()
        if kind == "year" and text not in years:
            return f"{line}: year {text} in [{name}] is not a year of the cited facts"
        if kind == "value" and text.lower() not in values:
            return f"{line}: '{text}' in [{name}] is not a value of the cited facts (use them exactly as written)"
        if kind == "metric" and text.lower() not in ctx.metric_names:
            return f"{line}: [{name}] must be the topic name '{ctx.topic.get('name')}'"
        if kind == "free":
            bad = [n for n in numbers_in(text) if n not in numbers]
            if bad:
                return f"{line}: number {bad[0]} in [{name}] is not in the cited facts"
            words = _words(text)
            banned = sorted(words & UNVERIFIABLE)
            if banned:
                return f"{line}: '{banned[0]}' in [{name}] claims something the facts do not show"
            if words & RISE and "up" not in dirs:
                return f"{line}: [{name}] says it went up, but the cited facts do not"
            if words & FALL and "down" not in dirs:
                return f"{line}: [{name}] says it went down, but the cited facts do not"
            for w in words & set(SUPERLATIVE):
                if not SUPERLATIVE[w] & kinds:
                    return f"{line}: '{w}' in [{name}] is not backed by the cited facts"
            if "only" in words and not any(f.get("only") for f in pool):
                return f"{line}: 'only' in [{name}] is not backed by the cited facts"
    return None


def _check_text(text: str, what: str, limit: int, ctx: Context) -> str | None:
    if not text:
        return f"{what} is empty"
    if len(text) > limit:
        return f"{what} is {len(text)} characters (max {limit})"
    if any(d in text for d in DASHES):
        return f"{what} contains an em dash"
    if "#" in text or "@" in text:
        return f"{what} contains a hashtag or @mention"
    for other in ctx.others:
        if re.search(rf"\b{re.escape(other)}\b", text, re.I):
            return f"{what} names {other}, which is not in the video"
    bad = [n for n in numbers_in(text) if n not in ctx.allowed]
    if bad:
        return f"{what}: number {bad[0]} is not in the computed facts"
    return None


def validate(v: dict, ctx: Context, used: set[str]) -> str | None:
    """None when the variant is acceptable, else the reason it is not (shown to Gemini on a retry)."""
    if not isinstance(v, dict):
        return "not a JSON object"
    hook_t = ctx.hooks.get(v.get("template_id"))
    if not hook_t:
        return f"unknown template_id {v.get('template_id')!r}"
    if hook_t["id"] in used:
        return f"template {hook_t['id']} is already used by another variant"
    hook, question = clean(v.get("hook")), clean(v.get("question"))
    why = _check_text(hook, "hook", int(ctx.s["hook_max_chars"]), ctx)
    if why:
        return why
    ids = v.get("fact_ids") or []
    if not isinstance(ids, list) or not ids or any(i not in ctx.facts_by_id for i in ids):
        return f"fact_ids must list ids of the facts the hook is based on (got {ids!r})"
    refs = [ctx.facts_by_id[i] for i in ids]
    if not any(ctx.usable(hook_t, f) for f in refs):
        return f"template {hook_t['id']} does not suit the cited facts"
    rx, slots = template_regex(hook_t["text"], ctx.names)
    m = rx.fullmatch(_core(hook))
    if not m:
        return f"hook does not follow the template '{hook_t['text']}' word for word"
    why = _check_slots(m, slots, ctx, refs, "hook")
    if why:
        return why
    q_t = ctx.questions.get(v.get("question_id"))
    if not q_t:
        return f"unknown question_id {v.get('question_id')!r}"
    why = _check_text(question, "question", int(ctx.s["question_max_chars"]), ctx)
    if why:
        return why
    if not question.endswith("?"):
        return "question must end with '?'"
    rx, slots = template_regex(q_t["text"], ctx.names)
    m = rx.fullmatch(_core(question))
    if not m:
        return f"question does not follow '{q_t['text']}' word for word"
    why = _check_slots(m, slots, ctx, refs, "question", question=True)
    if why:
        return why
    if count_emoji(hook + question) > int(ctx.s["max_emoji"]):
        return f"more than {ctx.s['max_emoji']} emoji"
    return None


def caption_text(hook: str, metric: str, question: str) -> str:
    return f"{clean(hook)}\n{metric}\n{clean(question)}"


# ------------------------------------------------------------------ plain template fills (no LLM)

def _fill(fmt, fact: dict, ctx: Context) -> str | None:
    if isinstance(fmt, dict):
        fmt = fmt.get(fact["kind"]) or fmt.get("default")
    if not fmt:
        return None
    vals = {"country": fact["country"], "country2": fact.get("country2"), "year": fact.get("year") or fact.get("to_year"),
            "from_year": fact.get("from_year"), "to_year": fact.get("to_year"), "from": fact.get("from"),
            "to": fact.get("to"), "value": fact.get("value"), "metric": (ctx.topic.get("name") or "").lower()}
    fields = [f for _, f, _, _ in string.Formatter().parse(fmt) if f]
    if any(vals.get(f) in (None, "") for f in fields):
        return None
    try:
        return fmt.format(**vals)
    except (KeyError, IndexError, ValueError):
        return None


def _question_for(q: dict, fact: dict) -> str | None:
    text = q["text"]
    for m in SLOT.finditer(q["text"]):
        kind = _slot_kind(m.group(1))   # a [year] question needs a fact with a moment (crossover, move, peak, record)
        val = fact["country"] if kind == "country" else fact.get("year") if kind == "year" else None
        if val is None:
            return None
        text = text.replace(m.group(0), str(val), 1)
    return text


def _fill_variant(h: dict, ctx: Context, used: set[str], out: list[dict], questions: list[dict],
                  spread: bool) -> dict | None:
    kinds = h.get("facts") or ["any"]
    rank = lambda f: (kinds.index(f["kind"]) if f["kind"] in kinds else len(kinds), f["id"])  # noqa: E731
    for f in sorted((f for f in ctx.facts if ctx.usable(h, f)), key=rank):
        if spread and any(f["country"] == o["_country"] for o in out):
            continue
        hook = _fill(h.get("fill"), f, ctx)
        if not hook:
            continue
        for q in questions:
            if spread and any(q["id"] == o["question_id"] for o in out):
                continue
            question = _question_for(q, f)
            v = {"template_id": h["id"], "hook": hook, "question_id": q["id"], "question": question,
                 "fact_ids": [f["id"]]}
            if question and validate(v, ctx, used) is None:
                return {**v, "_country": f["country"]}
    return None


def fallback_variants(ctx: Context, n: int, used: set[str], seed: int = 0) -> list[dict]:
    """Up to n variants filled straight from the templates' 'fill' strings, each from a different
    template; the order varies per video (seed). The first pass spreads countries and questions."""
    rng = random.Random(seed)

    def strength(h: dict) -> int:   # hooks for the strongest story facts first; generic ("any") hooks last
        if "any" in (h.get("facts") or ["any"]):
            return len(FACT_ORDER)
        return min(FACT_ORDER.index(f["kind"]) for f in ctx.facts if ctx.usable(h, f))

    hooks = sorted((h for h in ctx.suitable_hooks() if h.get("fill") and h["id"] not in used),
                   key=lambda h: (strength(h), rng.random()))
    questions = list(ctx.formats["questions"])
    rng.shuffle(questions)
    out, used = [], set(used)
    for spread in (True, False):
        for h in hooks:
            if len(out) >= n:
                return out
            if h["id"] in used:
                continue
            v = _fill_variant(h, ctx, used, out, questions, spread)
            if v:
                out.append(v)
                used.add(h["id"])
    return out


# ------------------------------------------------------------------ Gemini

PROMPT = """You write Instagram Reels hooks for a short data video. The video draws one line per country for one
statistic, one country at a time, with music made from the data.

Video
- Topic: {topic}
- Metric: {metric}
- Countries (in play order): {countries}
- Years: {x_start} to {x_end}

Facts, computed from the data. These are the ONLY facts and the ONLY numbers you may use:
{facts}

Hook templates (id: text). Words in [brackets] are slots; "suits" lists the facts a template fits:
{hooks}

Question templates (id: text):
{questions}

Write {n} caption variant(s). For each one:
- Pick a hook template{exclude}. Every variant uses a different template. Prefer the most surprising facts.
- Fill its slots using only the facts above: [Country] and [Country2] are country names from the list,
  [year] is a year from a cited fact, [x] and [y] are values copied exactly as a fact writes them
  (for example "{example}"), [metric] is "{topic_lower}". Free slots like [surprising move] or [fact] restate
  a cited fact in a few casual words, with no number unless it is copied exactly from that fact.
- Keep every other word of the template exactly as written. Add nothing before or after it.
  The hook is at most {hook_max} characters.
- Pick one question template and fill its slots the same way (at most {q_max} characters, ending in "?").
- Curiosity and surprise, not a dry description. Do not explain causes, do not name events, wars, crises or
  policies, and do not claim anything the facts do not say.
- No em dashes, no hashtags, no @mentions. At most {max_emoji} emoji in the hook and question together; zero is fine.
- fact_ids lists the ids of the facts the hook is based on.
{feedback}
Reply with JSON only:
{{"variants": [{{"template_id": "...", "hook": "...", "question_id": "...", "question": "...", "fact_ids": ["F1"]}}]}}"""


def build_prompt(ctx: Context, n: int, used: set[str], feedback: str = "") -> str:
    hooks = [h for h in ctx.suitable_hooks() if h["id"] not in used]
    example = next((f["from"] for f in ctx.facts if f.get("from")), next((f["value"] for f in ctx.facts if f.get("value")), "1.0"))
    return PROMPT.format(
        topic=ctx.topic.get("name", ""), topic_lower=(ctx.topic.get("name") or "").lower(),
        metric=ctx.topic.get("subtitle", ""), countries=", ".join(ctx.names), x_start=ctx.x_start, x_end=ctx.x_end,
        facts="\n".join(f"- {f['id']}: {f['text']}" for f in ctx.facts),
        hooks="\n".join(f"- {h['id']}: {h['text']}  (suits: {', '.join(h.get('facts') or ['any'])})" for h in hooks),
        questions="\n".join(f"- {q['id']}: {q['text']}" for q in ctx.formats["questions"]),
        n=n, exclude=f" (not {', '.join(sorted(used))})" if used else "", example=example,
        hook_max=ctx.s["hook_max_chars"], q_max=ctx.s["question_max_chars"], max_emoji=ctx.s["max_emoji"],
        feedback=f"\nYour previous attempt was rejected: {feedback}. Fix that.\n" if feedback else "")


def _items(resp) -> list:
    if isinstance(resp, dict):
        resp = resp.get("variants", [resp] if "hook" in resp else [])
    return resp if isinstance(resp, list) else []


def llm_variants(ctx: Context, n: int, ask) -> tuple[list[dict], list[str]]:
    """Variants from Gemini, each validated; a failed slot is asked for again up to llm_retries times.
    ask(prompt) -> (parsed reply, model that answered). Returns (accepted variants, notes). Stops when
    no model answers (gemini.QuotaExhausted) or on any other error."""
    from .gemini import QuotaExhausted
    retries = int(ctx.s["llm_retries"])
    accepted, notes, used, reasons = [], [], set(), []

    def accept(item: dict, attempts: int, rejected: list[str], model: str) -> None:
        accepted.append({**item, "attempts": attempts, "rejected": rejected, "model": model})
        used.add(item["template_id"])

    try:
        reply, model = ask(build_prompt(ctx, n, used))
        for item in _items(reply):
            if len(accepted) >= n:
                break
            why = validate(item, ctx, used)
            if why is None:
                accept(item, 1, [], model)
            else:
                reasons.append(why)
        for slot in range(n - len(accepted)):   # each missing variant: up to `retries` more asks
            last = reasons[slot] if slot < len(reasons) else "no usable variant was returned"
            rejected = [last]
            for attempt in range(retries):
                reply, model = ask(build_prompt(ctx, 1, used, feedback=last))
                items = _items(reply)
                why = validate(items[0], ctx, used) if items else "no variant was returned"
                if why is None:
                    accept(items[0], attempt + 2, rejected, model)
                    break
                last = why
                rejected.append(why)
            else:
                notes.append(f"Gemini variant rejected {retries + 1}x, plain fill used ({'; '.join(rejected)[:300]})")
    except QuotaExhausted as e:
        notes.append(f"Gemini unavailable, plain fills used: {str(e)[:200]}")
    except Exception as e:  # noqa: BLE001 - the plain template fill takes over
        notes.append(f"Gemini failed: {type(e).__name__}: {str(e)[:160]}")
    return accepted, notes


# ------------------------------------------------------------------ hashtags

HARD_BANNED = ("fyp", "viral", "explore", "foryou", "follow4follow", "followforfollow", "like4like", "likeforlike",
               "f4f", "l4l")


def load_hashtags() -> dict:
    return json.loads(HASHTAGS_FILE.read_text(encoding="utf-8"))


def tag_ok(tag: str, banned: set[str]) -> bool:
    t = tag.lower().lstrip("#")
    return bool(re.fullmatch(r"[a-z0-9_]{2,30}", t)) and t not in banned and not any(b in t for b in HARD_BANNED)


def country_tag(name: str, pools: dict) -> str:
    return (pools.get("country_tags") or {}).get(name) or re.sub(r"[^a-z0-9]", "", name.lower())


def _rotate(pool: list[str], seed: int) -> list[str]:
    if not pool:
        return []
    k = seed % len(pool)
    return pool[k:] + pool[:k]


def pick_hashtags(topic: dict, facts: list[dict], seed: int = 0, s: dict | None = None, pools: dict | None = None,
                  trending: bool | None = None, get=None) -> tuple[list[str], list[str]]:
    """(hashtags with '#', notes). Default 5: 1 broad, 2 niche (pinned first), 1 topic, 1 country."""
    s = s or settings()
    hs = s["hashtags"]
    pools = pools or load_hashtags()
    banned = {b.lower() for b in pools.get("banned", [])}
    count = max(0, min(int(hs.get("count", 5)), 30))
    n_broad = min(int(hs.get("broad", 1)), count)
    n_niche = min(int(hs.get("niche", 2)), count - n_broad)
    rest = count - n_broad - n_niche
    n_topic = math.ceil(rest / 2)
    n_country = rest - n_topic
    clean_pool = lambda xs: [x.lower().lstrip("#") for x in xs if tag_ok(x, banned)]  # noqa: E731
    broad = _rotate(clean_pool(pools.get("broad", [])), seed)
    pinned = clean_pool(pools.get("pinned", []))
    niche = pinned + [t for t in _rotate(clean_pool(pools.get("niche", [])), seed // 7) if t not in pinned]
    topic_pool = clean_pool((pools.get("by_topic") or {}).get(topic.get("id"), []))
    cat = topic.get("category", "")
    topic_pool += [t for t in _rotate(clean_pool((pools.get("categories") or {}).get(cat, [])), seed // 3)
                   if t not in topic_pool]
    notes = []
    tr = s["trending"]
    if (tr.get("enabled") if trending is None else trending) and topic_pool:
        topic_pool, notes = trending_rank(topic_pool, tr, get=get)
    seen = []
    for f in facts:
        for name in (f["country"], f.get("country2")):
            t = country_tag(name, pools) if name else ""
            if t and t not in seen and tag_ok(t, banned):
                seen.append(t)
    picked: list[str] = []

    def take(pool, k):
        for t in pool:
            if k <= 0:
                return 0
            if t not in picked:
                picked.append(t)
                k -= 1
        return k

    left = take(broad, n_broad) + take(niche, n_niche)
    left += take(topic_pool, n_topic)
    left = take(seen, n_country + left)
    for pool in (topic_pool, niche, broad):   # a short pool: fill from the others
        left = take(pool, left)
    return ["#" + t for t in picked], notes


# ------------------------------------------------------------------ optional trending check (Instagram hashtag search)

def _trend_file():
    return path("cache") / TREND_NAME


def _trend_cache() -> dict:
    try:
        d = json.loads(_trend_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return {"searched": d.get("searched", {}), "ids": d.get("ids", {}), "scores": d.get("scores", {})}


def trending_rank(tags: list[str], tr: dict, get=None, now: datetime | None = None) -> tuple[list[str], list[str]]:
    """Re-rank candidate tags by recent activity among their top posts. Meta allows 30 unique hashtag
    searches per account per rolling 7 days; at most trending.weekly_search_budget of them are spent here
    and each result is cached for trending.cache_days. Any failure keeps the original order."""
    from . import meta_api
    from .publish_instagram import base
    get = get or meta_api.get
    now = now or datetime.now(timezone.utc)
    ig, tok = secret("IG_USER_ID"), secret("IG_ACCESS_TOKEN")
    if not (ig and tok):
        return tags, ["trending: IG_USER_ID / IG_ACCESS_TOKEN missing, order unchanged"]
    cache = _trend_cache()
    week = now - timedelta(days=7)
    in_window = {t for t, ts in cache["searched"].items() if datetime.fromisoformat(ts) >= week}
    budget = int(tr.get("weekly_search_budget", 20))
    fresh_after = now - timedelta(days=float(tr.get("cache_days", 7)))
    recent_after = now - timedelta(days=float(tr.get("recent_days", 30)))
    notes, scores = [], {}
    for t in tags:
        sc = cache["scores"].get(t)
        if sc and datetime.fromisoformat(sc["checked_at"]) >= fresh_after:
            scores[t] = sc
            continue
        if t not in in_window and len(in_window) >= budget:
            notes.append(f"trending: weekly search budget ({budget}) used, #{t} not checked")
            continue
        try:
            hid = cache["ids"].get(t)
            if not hid:
                r = get(f"{base()}/ig_hashtag_search", params={"user_id": ig, "q": t, "access_token": tok}, timeout=30)
                d = r.json()
                if r.status_code != 200 or not d.get("data"):
                    notes.append(f"trending: #{t} search failed: {(d.get('error') or {}).get('message', r.status_code)}")
                    continue
                hid = cache["ids"][t] = d["data"][0]["id"]
            cache["searched"][t] = now.isoformat()
            in_window.add(t)
            r = get(f"{base()}/{hid}/top_media", params={"user_id": ig, "fields": "timestamp,like_count,comments_count",
                                                         "limit": 10, "access_token": tok}, timeout=30)
            d = r.json()
            if r.status_code != 200:
                notes.append(f"trending: #{t} top_media failed: {(d.get('error') or {}).get('message', r.status_code)}")
                continue
            posts = d.get("data") or []
            recent = sum(1 for p in posts if p.get("timestamp") and
                         datetime.strptime(p["timestamp"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) >= recent_after)
            likes = statistics.median([p.get("like_count") or 0 for p in posts]) if posts else 0
            scores[t] = cache["scores"][t] = {"recent": recent, "median_likes": likes, "n": len(posts),
                                              "checked_at": now.isoformat()}
        except Exception as e:  # noqa: BLE001 - never let the trending check block a caption
            notes.append(f"trending: #{t} check failed: {type(e).__name__}")
    try:
        _trend_file().write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except OSError as e:
        notes.append(f"trending: cache not saved ({e})")
    known = sorted((t for t in tags if t in scores and scores[t]["recent"] > 0),
                   key=lambda t: (-scores[t]["recent"], -scores[t]["median_likes"]))
    unknown = [t for t in tags if t not in scores]
    dead = [t for t in tags if t in scores and scores[t]["recent"] == 0]
    return known + unknown + dead, notes


# ------------------------------------------------------------------ one video

def generate(topic: dict, countries: list[tuple[str, str]], series: dict, x_start: int, x_end: int, seed: int = 0,
             llm: bool | None = None, ask=None, trending: bool | None = None, get=None) -> dict:
    """Facts, `variants` caption variants and the hashtags for one video (nothing is written)."""
    s = settings()
    facts = compute_facts(topic, countries, series, x_start, x_end)
    ctx = Context(topic, countries, facts, x_start, x_end, s=s)
    n = int(s["variants"])
    notes, accepted = [], []
    use_llm = s["llm"] if llm is None else llm
    if use_llm and facts:
        if ask is None and not secret("GEMINI_API_KEY"):
            notes.append("no GEMINI_API_KEY: plain template fills")
        else:
            from . import gemini
            accepted, more = llm_variants(ctx, n, ask or (lambda prompt: gemini.ask_json(prompt, what="caption")))
            notes += more
    used = {v["template_id"] for v in accepted}
    fills = fallback_variants(ctx, n - len(accepted), used, seed) if len(accepted) < n else []
    variants = []
    for v, source in [(v, "gemini") for v in accepted] + [(v, "template") for v in fills]:
        variants.append({"template_id": v["template_id"], "template": ctx.hooks[v["template_id"]]["text"],
                         "hook": clean(v["hook"]), "question_id": v["question_id"], "question": clean(v["question"]),
                         "fact_ids": v["fact_ids"], "source": source, "model": v.get("model"),
                         "attempts": v.get("attempts", 0), "rejected": v.get("rejected", []),
                         "caption": caption_text(v["hook"], ctx.metric, v["question"])})
    if len(variants) < n:
        notes.append(f"only {len(variants)} of {n} variants could be made from the facts")
    tags, tag_notes = pick_hashtags(topic, facts, seed, s, trending=trending, get=get)
    model = ", ".join(sorted({v["model"] for v in accepted if v.get("model")})) or None   # the model(s) that served
    return {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "model": model,
            "facts": facts, "metric_line": ctx.metric, "variants": variants, "hashtags": tags, "chosen": 0,
            "notes": notes + tag_notes}


def current(c: dict) -> dict:
    """What would be posted now: the owner's edit when it is in use, else the chosen option."""
    n = len(c["variants"])
    index = min(c.get("chosen", 0), n - 1)
    edit = c.get("edit") if c.get("use_edit") else None
    if edit:
        return {"text": edit["text"], "hashtags": list(edit["hashtags"]), "edited": True, "index": index,
                "warnings": list(edit.get("warnings") or [])}
    return {"text": c["variants"][index]["caption"], "hashtags": list(c.get("hashtags") or []), "edited": False,
            "index": index, "warnings": []}


def _compose(body: str, credit: list[str], tags: list[str]) -> str:
    text = "\n\n".join(p for p in (body, "\n".join(credit), " ".join(tags)) if p)
    if len(text) > IG_CAPTION_LIMIT:   # the long source list gives way first; the CC BY line stays
        text = "\n\n".join(p for p in (body, (credit or [""])[0], " ".join(tags)) if p)[:IG_CAPTION_LIMIT]
    return text


def compose(c: dict, index: int | None = None) -> str:
    """The full post caption: the chosen option (hook, metric line, question) or the owner's edit, then
    the data credit and the hashtags. The credit is always added here, whatever the edit says."""
    if index is None:
        cur = current(c)
        return _compose(cur["text"], c.get("credit") or [], cur["hashtags"])
    return _compose(c["variants"][index]["caption"], c.get("credit") or [], c.get("hashtags") or [])


# ------------------------------------------------------------------ the owner's edit

EM_DASHES = ("—", "―")
MAX_HASHTAGS = 30          # Instagram's limit per caption
TAG = re.compile(r"#[^\s#]+")


class CaptionRejected(ValueError):
    """An edit that cannot be saved; str() is the reasons in plain words."""

    def __init__(self, errors: list[str]):
        super().__init__(" ".join(errors))
        self.errors = errors


def allowed_numbers(c: dict) -> set[str]:
    """Numbers a caption may use without a warning: the computed facts and the metric line's years."""
    nums = set(numbers_in(c.get("metric_line", "")))
    for f in c.get("facts", []):
        nums |= fact_numbers(f)
    return nums


def split_hashtags(text: str) -> tuple[str, list[str] | None]:
    """A typed caption's trailing hashtag-only lines become its hashtags (None when there are none)."""
    lines = (text or "").replace("\r\n", "\n").strip().split("\n")
    tags: list[str] = []
    while lines and lines[-1].split() and all(w.startswith("#") for w in lines[-1].split()):
        tags = lines.pop().split() + tags
    return "\n".join(lines).strip(), (tags or None)


def _tags(raw) -> list[str]:
    words = re.split(r"[\s,]+", raw) if isinstance(raw, str) else list(raw or [])
    out, seen = [], set()
    for w in words:
        w = w.strip()
        if not w:
            continue
        t = "#" + w.lstrip("#")
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _body(text: str, credit: list[str]) -> str:
    """Each line's spaces tidied, at most one blank line in a row, and the credit lines taken out
    (the credit is added at posting time, so it would otherwise appear twice)."""
    skip = {" ".join(x.split()) for x in credit}
    lines, blank = [], False
    for ln in (text or "").replace("\r\n", "\n").split("\n"):
        ln = " ".join(ln.split())
        if ln in skip:
            continue
        if not ln:
            if lines and not blank:
                lines.append("")
            blank = True
            continue
        lines.append(ln)
        blank = False
    return "\n".join(lines).strip()


def check_edit(c: dict, text: str, hashtags) -> dict:
    """{"text", "hashtags", "errors", "warnings"}: errors block saving, warnings only ask for a check."""
    credit = c.get("credit") or []
    body, tags = _body(text, credit), _tags(hashtags)
    errors, warnings = [], []
    if not body:
        errors.append("The caption is empty.")
    if any(d in body or any(d in t for t in tags) for d in EM_DASHES):
        errors.append("Remove the em dash (—): use a comma, a colon or a full stop instead.")
    bad = [t for t in tags if not re.fullmatch(r"#\w{1,100}", t)]
    if bad:
        errors.append(f"{bad[0]} is not a hashtag Instagram accepts (letters, numbers and _ only).")
    n_tags = len({t.lower() for t in tags} | {t.lower() for t in TAG.findall(body)})
    if n_tags > MAX_HASHTAGS:
        errors.append(f"There are {n_tags} hashtags; Instagram allows at most {MAX_HASHTAGS}.")
    size = len("\n\n".join(p for p in (body, (credit or [""])[0], " ".join(tags)) if p))   # the shortest posting
    if size > IG_CAPTION_LIMIT:
        errors.append(f"With the data credit and hashtags the caption is {size:,} characters; Instagram allows "
                      f"{IG_CAPTION_LIMIT:,}. Cut {size - IG_CAPTION_LIMIT:,}.")
    allowed = allowed_numbers(c)
    unknown = list(dict.fromkeys(n for n in numbers_in(TAG.sub(" ", body)) if n not in allowed))
    if unknown:
        warnings.append(f"Check {', '.join(unknown[:4])}: not in the facts computed from the data.")
    return {"text": body, "hashtags": tags, "errors": errors, "warnings": warnings}


def save_edit(run_id: str, text: str, hashtags=None, by: str = "dashboard") -> dict:
    """Validate and store the owner's caption; it is what gets posted from now on (choose() goes back to
    an option). hashtags=None: hashtag-only lines at the end of the text are the hashtags, and without any
    the current ones are kept. Raises CaptionRejected with the reasons. Returns check_edit()'s result."""
    c = load(run_id)
    if not c:
        raise ValueError("This video has no caption yet.")
    if hashtags is None:
        text, hashtags = split_hashtags(text)
        if hashtags is None:
            hashtags = current(c)["hashtags"]
    res = check_edit(c, text, hashtags)
    if res["errors"]:
        raise CaptionRejected(res["errors"])
    edit = {"text": res["text"], "hashtags": res["hashtags"], "warnings": res["warnings"], "by": by,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    c["edit"], c["use_edit"] = edit, True
    c["edits"] = (c.get("edits") or []) + [edit]
    save(run_id, c)
    return res


# ------------------------------------------------------------------ files

def _path(run_id: str):
    return run_dir(run_id) / OUT_NAME


def load(run_id: str) -> dict | None:
    try:
        c = json.loads(_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return c if c.get("variants") else None


def save(run_id: str, c: dict) -> None:
    p = _path(run_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(c, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def generate_for_run(run_id: str, llm: bool | None = None, trending: bool | None = None, ask=None, get=None) -> dict:
    """Make out/<run_id>/captions.json from the run's data.json and pick.json. A choice already made
    on the review card is kept when that variant still exists."""
    from . import db
    d = run_dir(run_id)
    data = json.loads((d / "data.json").read_text(encoding="utf-8"))
    p = json.loads((d / "pick.json").read_text(encoding="utf-8"))
    run = db.get_run(run_id) or {}
    topic = get_topic(run.get("topic_id") or data["topic_id"])
    names = country_by_iso3()
    countries = [(c, names[c]["name"] if c in names else c) for c in p["countries"]]
    c = generate(topic, countries, data["series"], p["x_start"], p["x_end"], seed=zlib.crc32(run_id.encode()),
                 llm=llm, ask=ask, trending=trending, get=get)
    c = {"run_id": run_id, "topic_id": topic["id"], **c, "credit": credit_lines(topic, data)}
    old = load(run_id)
    if old and 0 < old.get("chosen", 0) < len(c["variants"]):
        c["chosen"] = old["chosen"]
    for k in ("edit", "edits", "use_edit"):   # the owner's own caption survives a regeneration
        if old and k in old:
            c[k] = old[k]
    save(run_id, c)
    return c


def ensure(run_id: str) -> dict | None:
    """captions.json for a run that has none yet: plain template fills only (fast, no network), so
    the review card always has options. Never raises."""
    c = load(run_id)
    if c or not enabled():
        return c
    try:
        if (run_dir(run_id) / "data.json").exists() and (run_dir(run_id) / "pick.json").exists():
            return generate_for_run(run_id, llm=False, trending=False)
    except Exception as e:  # noqa: BLE001 - the card goes out without caption options
        log.warning("captions for %s could not be made: %s", run_id, e)
    return None


def choose(run_id: str, index: int) -> dict:
    c = load(run_id)
    if not c:
        raise ValueError("This video has no caption options.")
    if not 0 <= index < len(c["variants"]):
        raise ValueError("No such caption option.")
    c["chosen"], c["use_edit"] = index, False   # an edit stays stored, but the option is posted
    save(run_id, c)
    return c


def apply_to_meta(run_id: str, meta: dict) -> dict:
    """The metadata a publisher posts with: the chosen caption replaces description_instagram (used for
    Instagram and Facebook) when captions.use_for_posting is on; otherwise meta is returned unchanged."""
    s = settings()
    if not (s["enabled"] and s["use_for_posting"]):
        return meta
    try:
        c = load(run_id) or generate_for_run(run_id, llm=False, trending=False)
        return {**meta, "description_instagram": compose(c)}
    except Exception as e:  # noqa: BLE001 - posting goes ahead with the old caption
        log.warning("captions: %s posts with the old caption (%s)", run_id, e)
        return meta


def summary(c: dict) -> str:
    src = [v["source"] for v in c["variants"]]
    return (f"{len(src)} caption options ({src.count('gemini')} from Gemini, {src.count('template')} plain fills), "
            f"{len(c['hashtags'])} hashtags" + (f"; {'; '.join(c['notes'])}" if c.get("notes") else ""))[:600]


def preview_text(c: dict) -> str:
    lines = [f"Facts ({len(c['facts'])}):"] + [f"  {f['id']} {f['text']}" for f in c["facts"]] + [""]
    for i, v in enumerate(c["variants"]):
        mark = "*" if i == c.get("chosen", 0) else " "
        lines += [f"{mark}{i + 1}. [{v['template_id']}, {v['source']}]", *("    " + ln for ln in v["caption"].splitlines()), ""]
    lines.append("Hashtags: " + " ".join(c["hashtags"]))
    lines += [f"Note: {n}" for n in c.get("notes", [])]
    return "\n".join(lines)
