"""Topic ideas for the Telegram New reel and Make a batch screens.

Every idea passes the existing interest filter: selector.eligible_topics (enabled, not retired, not
resting, sets not used up) and review.topic_score > 0 (enough countries pass the data scorer, from the
local series cache, never the network). Ideas are de-duplicated against the library: every topic that
already has a video (made, waiting for review, approved, posted or rejected), is being made, or waits in
the make queue.

Variety: suggestions interleave the categories (economy, people, health, energy, tech, planet), and a
batch takes the least-used category each time and never puts two topics from the same dataset or metric
family back to back (dataset_key).
"""
from __future__ import annotations

import random
import re

from . import db

STOP = {"the", "a", "an", "of", "in", "and", "or", "per", "by", "to", "for", "on", "how", "what", "much", "many",
        "rate", "share", "data", "about", "reel", "video", "topic", "please", "make", "me", "some", "with", "vs"}
# plain words people type -> words that appear in topics.yaml (names, titles, subtitles)
SYNONYMS = {"carbon": "co2", "emissions": "co2", "pollution": "co2", "babies": "births fertility",
            "baby": "births fertility", "birth": "births fertility", "kids": "children", "jobs": "unemployment",
            "jobless": "unemployment", "work": "workforce", "rich": "income gdp", "richer": "income gdp",
            "wealth": "income gdp", "money": "income gdp", "economy": "economic gdp", "prices": "inflation",
            "price": "inflation", "phones": "phone mobile landline", "phone": "mobile landline",
            "web": "internet", "online": "internet", "flights": "air passengers flying", "flying": "air",
            "planes": "air flying", "army": "military", "weapons": "military", "murder": "homicide",
            "murders": "homicide", "crime": "homicide", "killings": "homicide", "drinking": "alcohol",
            "drink": "alcohol", "beer": "alcohol", "wine": "alcohol", "food": "meat cereal", "farming": "cereal",
            "crops": "cereal", "climate": "temperature co2", "warming": "temperature", "heat": "temperature",
            "electricity": "electricity", "power": "electricity", "factories": "manufacturing",
            "industry": "manufacturing", "tourists": "tourism", "travel": "tourism", "migrants": "migration",
            "immigration": "migration", "science": "research", "doctors": "health", "hospitals": "health",
            "ageing": "age", "aging": "age", "old": "age", "population": "population", "life": "life",
            "death": "mortality", "deaths": "mortality", "solar": "solar", "sun": "solar", "dams": "hydro",
            "water": "hydro", "nuclear": "nuclear", "coal": "coal", "oil": "oil", "petrol": "oil",
            "renewable": "renewables", "green": "renewables", "broadband": "broadband", "wifi": "broadband"}


def dataset_key(topic: dict) -> str:
    """The dataset or metric family a topic comes from: OWID's bulk energy / CO2 tables count as one
    dataset each, an OWID grapher chart as one, and World Bank indicators by their first two code
    parts (NY.GDP growth and NY.GDP per person are one family, IT.NET internet users and broadband too)."""
    code = str(topic.get("code", ""))
    if topic.get("source") == "owid":
        if code.startswith("github:"):
            return "owid:" + code[len("github:"):].split("/")[0]
        return "owid:" + code.split("/")[0]
    return "wb:" + ".".join(code.split(".")[:2])


def library() -> set[str]:
    """Topics already made or posted (any run that is not skipped and did not fail before review),
    being made, waiting in the make queue, or in a batch that is shown but not started yet."""
    from .makequeue import jobs, pending_batch
    made = {r["topic_id"] for r in db.list_runs(limit=100000)
            if r["stage"] != "skipped" and not (r["stage"] == "failed" and r["failed_stage"] != "publish")}
    return made | {j["topic_id"] for j in jobs("queued", "running")} | {t for t in pending_batch()}


def pool(exclude=()) -> list[dict]:
    """Eligible, unmade topics that pass the interest filter, best first, each with its "score"."""
    from .review import topic_score
    from .selector import eligible_topics
    skip = library() | set(exclude)
    out = []
    for t in eligible_topics(exclude=skip):
        if len(t["id"]) > 56:           # it has to fit in a Telegram button's 64-byte callback data
            continue
        s = topic_score(t)
        if s > 0:
            out.append({**t, "score": s})
    out.sort(key=lambda t: -t["score"])
    return out


def mixed(topics: list[dict]) -> list[dict]:
    """Interleave the categories: the best of each category first (best category first), then the
    second best of each, and so on."""
    by_cat: dict[str, list[dict]] = {}
    for t in topics:
        by_cat.setdefault(t.get("category") or "other", []).append(t)
    cats = sorted(by_cat, key=lambda c: -by_cat[c][0]["score"])
    out = []
    for i in range(max((len(v) for v in by_cat.values()), default=0)):
        out += [by_cat[c][i] for c in cats if i < len(by_cat[c])]
    return out


def suggestions(n: int = 5, page: int = 0, exclude=()) -> list[dict]:
    """Page `page` of n mixed-category ideas; pages wrap around, so More ideas never runs dry."""
    ideas = mixed(pool(exclude))
    if len(ideas) <= n:
        return ideas
    start = (page * n) % len(ideas)
    return (ideas + ideas)[start:start + n]


def surprise(rng: random.Random | None = None, exclude=()) -> dict | None:
    ideas = pool(exclude)
    if not ideas:
        return None
    return (rng or random.Random()).choices(ideas, weights=[t["score"] for t in ideas], k=1)[0]


def pick_batch(n: int, rng: random.Random | None = None, exclude=(), after: dict | None = None,
               counts: dict[str, int] | None = None) -> list[dict]:
    """n distinct topics in making order: each step takes a topic from the least-used category, never the
    same category or dataset as the one before it when there is any other choice (never the same
    dataset), a dataset not used yet in the batch when possible, weighted by interest score. `after` is
    the topic the batch continues from and `counts` its categories so far (for a replacement)."""
    rng = rng or random.Random()
    cands = pool(exclude)
    chosen: list[dict] = []
    used_cats: dict[str, int] = dict(counts or {})
    while len(chosen) < n:
        prev = chosen[-1] if chosen else after
        free = [t for t in cands if t not in chosen and (not prev or dataset_key(t) != dataset_key(prev))]
        options = [t for t in free if not prev or t.get("category") != prev.get("category")] or free
        if not options:
            break
        least = min(used_cats.get(t.get("category"), 0) for t in options)
        options = [t for t in options if used_cats.get(t.get("category"), 0) == least]
        fresh = [t for t in options if dataset_key(t) not in {dataset_key(c) for c in chosen}] or options
        pick = rng.choices(fresh, weights=[t["score"] for t in fresh], k=1)[0]
        chosen.append(pick)
        used_cats[pick.get("category")] = used_cats.get(pick.get("category"), 0) + 1
    return chosen


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def match(text: str, limit: int = 5) -> dict:
    """Topics matching what the owner typed: {"free": topics that can be made now, best first, "made":
    topics that already have a reel or are queued, "clear": True when the best free one is the only match
    or leads the next by 2 points}. Words are matched against each topic's name, title, subtitle and id
    (a few everyday synonyms included); a word in the name counts double."""
    from .topics import load_topics
    asked = []
    for w in _words(text):
        if w in STOP or len(w) < 2:
            continue
        asked += [w] + _words(SYNONYMS.get(w, ""))
    if not asked:
        return {"free": [], "made": [], "clear": False}
    free_ids = {t["id"] for t in pool()}
    made = library()
    scored = []
    for t in load_topics():
        if not t.get("enabled", True):
            continue
        name = set(_words(t.get("name", "")))
        hay = name | set(_words(" ".join([t["id"].replace("_", " "), t.get("title", ""), t.get("subtitle", "")])))
        score = 0
        for w in dict.fromkeys(asked):
            hit = w in hay or (len(w) >= 4 and any(h.startswith(w) or w.startswith(h) for h in hay if len(h) >= 4))
            if hit:
                score += 2 if (w in name or any(h.startswith(w) for h in name if len(w) >= 4)) else 1
        if score:
            scored.append((score, t))
    scored.sort(key=lambda st: -st[0])
    free = [(s, t) for s, t in scored if t["id"] in free_ids][:limit]
    gone = [t for s, t in scored if t["id"] in made][:limit]
    clear = len(free) == 1 or (len(free) > 1 and free[0][0] >= free[1][0] + 2)
    return {"free": [t for _, t in free], "made": gone, "clear": clear}
