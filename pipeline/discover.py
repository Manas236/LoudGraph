"""New topic categories for the autopilot, from the free World Bank and OWID catalogs (no key).

When fewer than autopilot.discover_below categories in the topic catalog are untested (no Instagram post yet), the
generation run calls run(): it takes the World Bank's topic list (api.worldbank.org/v2/topic) and, for each topic
whose category the catalog does not have yet (CATEGORIES maps it to a short category name), looks at
  * that topic's World Development Indicators (api.worldbank.org/v2/topic/{id}/indicator, source 2), and
  * Our World in Data grapher charts whose slug has one of the category's words (ourworldindata.org/sitemap.xml).
Each candidate is fetched (fetch.py, so the series cache is the one the reel will use) and must pass
  1. the interest scorer, as `topics-verify` asks of every topic: >= data.verify_min_countries usable countries and
     >= picker.min_countries passing score.py;
  2. labels: Gemini names it (name, title, a one-line subtitle that fits the video like every other topic, and
     whether a lower value is the better outcome). Gemini only names and labels: every number in the topic
     (start_year, min_meaningful, unit_format) is derived from the data. Without an answer from Gemini the
     candidate waits for a later run;
  3. the "Which country...?" truth check: a country set picked as for a real reel (picker.pick) has at least one
     configured phrasing whose check is true of its data (hooks.which_last_opening).
A category is added (source=auto, kept as `origin: auto` in topics_auto.yaml, since `source` already names the data
provider) with its 2-4 passing topics; at most autopilot.discover_per_week new categories in any 7 days. A category
that yields too few topics is not tried again for 30 days.
"""
from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from statistics import median

import requests
import yaml

from . import db
from .config import country_by_iso3, get_config

log = logging.getLogger(__name__)
WB_TOPICS = "https://api.worldbank.org/v2/topic?format=json&per_page=100"
WB_INDICATORS = "https://api.worldbank.org/v2/topic/{id}/indicator?format=json&per_page=3000"
OWID_SITEMAP = "https://ourworldindata.org/sitemap.xml"
WDI_SOURCE = "2"
MIN_TOPICS, MAX_TOPICS = 2, 4
CANDIDATES_PER_CATEGORY = 14
RETRY_FAILED_DAYS = 30
UA = {"User-Agent": "graphony-autopilot/1.0 (data sonification reels)"}

# World Bank topic -> category; None = already covered by a catalog category (or not a subject)
CATEGORIES = {
    "Agriculture & Rural Development": "farming", "Aid Effectiveness": "aid", "Economy & Growth": None,
    "Education": "education", "Energy & Mining": None, "Environment": None, "Financial Sector": "finance",
    "Health": None, "Infrastructure": "infrastructure", "Social Protection & Labor": "work", "Poverty": "poverty",
    "Private Sector": "business", "Public Sector": "government", "Science & Technology": None,
    "Social Development": "society", "Urban Development": "cities", "Gender": "gender",
    "Millenium development goals": None, "Climate Change": None, "External Debt": "debt", "Trade": "trade",
}
OWID_WORDS = {
    "farming": ["crop", "cereal", "agricultur", "fertilizer", "livestock", "yield"], "aid": ["aid", "assistance"],
    "education": ["school", "education", "literacy", "enrolment", "enrollment", "pupil"],
    "finance": ["bank", "credit", "financial"], "infrastructure": ["road", "rail", "sanitation", "drinking-water"],
    "work": ["labor", "labour", "employment", "working-hours", "pension"], "poverty": ["poverty", "gini", "inequality"],
    "business": ["business", "firms"], "government": ["government", "tax-revenue", "public-spending"],
    "society": ["happiness", "trust", "marriage", "suicide"], "cities": ["urban", "cities", "slum"],
    "gender": ["women", "female", "gender", "girls"], "debt": ["debt"], "trade": ["export", "import", "trade"],
}
SKIP_NAME = re.compile(r"current US\$|current LCU|constant LCU|\bLCU\b|number of|\(number\)|, total\)|millions|"
                       r"billions|thousands|PPP\)|\bSDR\b|scale|rating|CPIA|index \(|GNI \(|GDP \(", re.I)
NAME_PROMPT = """You label a data series for a short vertical video that races countries on one chart.
Series: {name}
Unit: {unit}
About: {note}
Category: {category}

Reply with JSON only:
{{"name": "2 to 3 words naming the topic, e.g. Child mortality",
 "title": "a short catchy title, at most 38 characters, e.g. If inflation had a sound",
 "subtitle": "what the number IS, with its unit, at most 34 characters, e.g. Consumer prices, % rise in a year",
 "lower_is_better": true or false (true when a lower value is the better outcome)}}
Write all three in sentence case (only the first word and names capitalised).
Use no number in name, title or subtitle unless it is in the series name above (like "per 1,000 people")."""


def _get_json(url: str):
    r = requests.get(url, headers=UA, timeout=get_config()["data"]["http_timeout"])
    r.raise_for_status()
    return r.json()


def _auto_file():
    from .topics import auto_topics_file
    return auto_topics_file()


def _save(topics: list[dict]) -> None:
    from .topics import load_auto_topics
    f = _auto_file()
    data = load_auto_topics() + topics
    tmp = f.with_suffix(".yaml.tmp")
    tmp.write_text("# Topics the autopilot discovered (pipeline/discover.py). Server state: not in git.\n" +
                   yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    tmp.replace(f)


def quota_left(now: datetime | None = None) -> int:
    """New categories still allowed this week (autopilot.discover_per_week in any 7 days)."""
    from .topics import load_auto_topics
    now = now or datetime.now(timezone.utc)
    per = int((get_config().get("autopilot") or {}).get("discover_per_week", 3))
    week = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = {t.get("category") for t in load_auto_topics() if str(t.get("added_at", "")) >= week}
    return max(0, per - len(recent))


def _slug(text: str, n: int = 24) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:n].strip("_")


# ------------------------------------------------------------------ candidates

def wb_candidates(topic_id: str, rng: random.Random) -> list[dict]:
    data = _get_json(WB_INDICATORS.format(id=topic_id))
    rows = data[1] if isinstance(data, list) and len(data) > 1 and data[1] else []
    out = [{"source": "worldbank", "code": r["id"], "name": r.get("name") or r["id"], "unit": r.get("unit") or "",
            "note": (r.get("sourceNote") or "")[:400]}
           for r in rows if str((r.get("source") or {}).get("id")) == WDI_SOURCE and not SKIP_NAME.search(r.get("name") or "")]
    rng.shuffle(out)
    return out


_SITEMAP: list[str] | None = None


def owid_candidates(category: str, rng: random.Random) -> list[dict]:
    global _SITEMAP
    words = OWID_WORDS.get(category) or []
    if not words:
        return []
    if _SITEMAP is None:
        r = requests.get(OWID_SITEMAP, headers=UA, timeout=get_config()["data"]["http_timeout"])
        r.raise_for_status()
        _SITEMAP = re.findall(r"<loc>https://ourworldindata\.org/grapher/([^<]+)</loc>", r.text)
    slugs = [s for s in _SITEMAP if any(w in s for w in words)]
    rng.shuffle(slugs)
    out = []
    for slug in slugs[:6]:
        try:
            m = _get_json(get_config()["data"]["owid_metadata_url"].format(slug=slug))
        except Exception:  # noqa: BLE001 - one chart less
            continue
        cols = [(k, v) for k, v in (m.get("columns") or {}).items() if v.get("type", "Numeric") == "Numeric"]
        if len(cols) != 1:   # one numeric column: a single series per country
            continue
        col, meta = cols[0]
        out.append({"source": "owid", "code": f"{slug}/{col}", "name": meta.get("titleShort") or slug,
                    "unit": meta.get("unit") or meta.get("shortUnit") or "", "note": (meta.get("descriptionShort") or "")[:400]})
    return out


# ------------------------------------------------------------------ one candidate

def numbers_from_data(series: dict, unit: str, name: str) -> dict | None:
    """start_year, min_meaningful and unit_format, all from the data (None when the series cannot make a reel)."""
    cfg = get_config()
    need = int(cfg["data"]["verify_min_countries"])
    years = sorted({int(y) for rows in series.values() for y, _ in rows})
    start = next((y for y in years if y >= 1990 and sum(1 for rows in series.values()
                                                         if any(int(r[0]) == y for r in rows)) >= need), None)
    if start is None or max(years) - start < 15:
        return None
    peaks = [max(abs(float(v)) for _, v in rows) for rows in series.values() if rows]
    mid = median(peaks) if peaks else 0
    if not mid or mid > 1e7:
        return None
    m = 0.05 * mid
    digits = -int(f"{m:e}".split("e")[1]) + 1
    min_meaningful = round(m, max(digits, 0))
    pct = "%" in unit or "%" in name or "percent" in unit.lower()
    if pct:
        fmt = "{:.1f}%"
    elif mid >= 1000:
        fmt = "{:,.0f}"
    elif mid >= 100:
        fmt = "{:.0f}"
    elif mid >= 10:
        fmt = "{:.1f}"
    else:
        fmt = "{:.2f}"
    return {"start_year": start, "min_meaningful": min_meaningful or m, "unit_format": fmt}


def label(cand: dict, category: str, ask=None) -> dict | None:
    """Gemini's name, title, subtitle and direction for a candidate, checked; None when it gave none usable."""
    from . import gemini
    from .render import _measure_canvas, header_fit
    prompt = NAME_PROMPT.format(name=cand["name"], unit=cand["unit"] or "(see the name)", note=cand["note"] or "-",
                                category=category)
    try:
        reply, _ = (ask or (lambda p: gemini.ask_json(p, what="topic name")))(prompt)
    except Exception as e:  # noqa: BLE001 - no label: the candidate waits for a later run
        log.info("discover: no Gemini label for %s (%s)", cand["code"], e)
        return None
    if not isinstance(reply, dict):
        return None
    out = {k: " ".join(str(reply.get(k) or "").split()) for k in ("name", "title", "subtitle")}
    if not all(out.values()) or len(out["name"]) > 28 or len(out["title"]) > 44:
        return None
    allowed = set(re.findall(r"\d[\d,.]*", cand["name"] + " " + cand["unit"]))
    for k in ("name", "title", "subtitle"):
        if any(n not in allowed for n in re.findall(r"\d[\d,.]*", out[k])):
            return None   # a number Gemini made up
    size, lines = header_fit(_measure_canvas(), out["subtitle"])
    if len(lines) != 1 or size < 44:
        return None   # the subtitle must sit on one line, like every catalog topic's
    out["lower_first"] = bool(reply.get("lower_is_better"))
    return out


def truth_check(topic: dict, data: dict) -> str | None:
    """None when a reel of this topic can open with a true "Which country...?" question, else why not."""
    from .hooks import which_last_opening
    from .picker import NotEnoughData, pick
    from .score import score_topic
    from .topics import format_value
    try:
        p = pick(score_topic(data, topic), data["series"], topic, seed=1)
    except NotEnoughData as e:
        return str(e)
    names = {k: v["name"] for k, v in country_by_iso3().items()}
    rows = {c: [(int(y), float(v)) for y, v in data["series"][c] if p["x_start"] <= int(y) <= p["x_end"]]
            for c in p["countries"]}
    opening, why_not = which_last_opening(topic, rows, names, lambda v: format_value(topic, v), history=[])
    return None if opening else "no \"Which country...?\" question is true of it: " + "; ".join(why_not.values())[:200]


def evaluate(cand: dict, category: str, ask=None) -> dict | None:
    """The topic for a candidate when it passes every check, else None (logged)."""
    from .fetch import fetch_topic
    from .score import score_topic
    cfg = get_config()
    tid = f"auto_{_slug(category, 12)}_{_slug(cand['name'], 28)}"
    probe = {"id": tid, "source": cand["source"], "code": cand["code"], "start_year": 1990}
    try:
        data = fetch_topic(probe)
    except Exception as e:  # noqa: BLE001
        log.info("discover: %s not fetched (%s)", cand["code"], e)
        return None
    nums = numbers_from_data(data["series"], cand["unit"] or "", cand["name"])
    if not nums:
        log.info("discover: %s: too little data since 1990", cand["code"])
        return None
    topic = {"id": tid, "name": cand["name"][:28], "category": category, "source": cand["source"], "code": cand["code"],
             "title": cand["name"][:40], "subtitle": cand["name"][:34], **nums,
             "modelled_data_warning": "model" in cand["name"].lower()}
    scored = score_topic({**data, "series": {k: [r for r in v if int(r[0]) >= topic["start_year"]]
                                              for k, v in data["series"].items()}}, topic)
    usable = sum(1 for s in scored if s.get("n", 0) >= cfg["scorer"]["min_points"])
    passing = sum(1 for s in scored if s["ok"])
    if usable < cfg["data"]["verify_min_countries"] or passing < cfg["picker"]["min_countries"]:
        log.info("discover: %s: %d usable, %d passing the scorer", cand["code"], usable, passing)
        return None
    labels = label(cand, category, ask)
    if not labels:
        return None
    topic.update(name=labels["name"], title=labels["title"], subtitle=labels["subtitle"])
    if labels["lower_first"]:
        topic["rank"] = "lower_first"
    why = truth_check(topic, data)
    if why:
        log.info("discover: %s: %s", cand["code"], why)
        return None
    return {**topic, "origin": "auto", "indicator": cand["name"][:120],
            "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


# ------------------------------------------------------------------ the run

def run(max_new: int | None = None, rng: random.Random | None = None, ask=None) -> list[str]:
    """Discover up to max_new new categories (default: this week's quota). Returns the categories added."""
    from .learn import catalog_categories, reset_cache
    rng = rng or random.Random()
    max_new = quota_left() if max_new is None else max_new
    if max_new <= 0:
        return []
    have = catalog_categories()
    wb = _get_json(WB_TOPICS)
    topics = {(t.get("value") or "").strip(): t["id"] for t in (wb[1] if len(wb) > 1 else [])}
    added = []
    order = list(topics.items())
    rng.shuffle(order)
    for name, wb_id in order:
        cat = CATEGORIES.get(name, _slug(name.split("&")[0], 16) or None)
        if not cat or cat in have or len(added) >= max_new:
            continue
        failed = db.kv_get(f"discover:failed:{cat}")
        if failed and datetime.now(timezone.utc) - db.parse_ts(failed) < timedelta(days=RETRY_FAILED_DAYS):
            continue
        cands = []
        try:
            cands = wb_candidates(wb_id, rng)[:CANDIDATES_PER_CATEGORY]
        except Exception as e:  # noqa: BLE001
            log.info("discover: World Bank indicators of %s unavailable: %s", name, e)
        try:
            cands += owid_candidates(cat, rng)
        except Exception as e:  # noqa: BLE001
            log.info("discover: OWID charts for %s unavailable: %s", cat, e)
        found = []
        for cand in cands:
            t = evaluate(cand, cat, ask)
            if t and t["id"] not in {x["id"] for x in found}:
                found.append(t)
                log.info("discover: %s passes: %s (%s)", cat, t["name"], t["code"])
            if len(found) >= MAX_TOPICS:
                break
        if len(found) >= MIN_TOPICS:
            _save(found)
            added.append(cat)
            have.add(cat)
            log.info("discover: new category %s with %d topics: %s", cat, len(found), ", ".join(t["name"] for t in found))
        else:
            db.kv_set(f"discover:failed:{cat}", db.now())
            log.info("discover: %s: only %d topic(s) passed; not added (tried again in %d days)", cat, len(found),
                     RETRY_FAILED_DAYS)
    reset_cache()
    if added:
        db.kv_set("discover:last", json.dumps({"at": db.now(), "added": added}))
    return added
