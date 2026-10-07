"""Turning-point labels (section 5).

Code finds each country's turning point. Gemini (optional) is only asked for a 2-4 word event
tag for that year; it never supplies or edits a number. Anything that fails validation means
"no label", which is always acceptable.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

import numpy as np
import requests

from .config import get_config, path, secret
from .score import zigzag

log = logging.getLogger(__name__)


class QuotaExhausted(RuntimeError):
    pass


# ------------------------------------------------------------------ turning point (pure code)

def turning_point(rows, start_year: int, cfg: dict | None = None) -> dict | None:
    cfg = cfg or get_config()
    rows = [r for r in rows if r[0] >= start_year]
    if len(rows) < 3:
        return None
    years = np.array([r[0] for r in rows])
    y = np.array([r[1] for r in rows], dtype=float)
    rng = float(y.max() - y.min())
    if rng <= 0:
        return None
    d1 = np.diff(y)
    k = int(np.argmax(np.abs(d1)))
    shock = abs(d1[k]) / rng
    if shock >= cfg["labels"]["shock_threshold"]:
        i, kind = k + 1, "shock"
    else:
        pivots, _ = zigzag(y, cfg["scorer"]["reversal_threshold"] * rng)
        interior = list(range(1, len(pivots) - 1))
        if interior:
            j = max(interior, key=lambda j: abs(y[pivots[j + 1]] - y[pivots[j]]))
            i = pivots[j]
            kind = "peak" if y[pivots[j + 1]] < y[i] else "trough"
        else:
            i, kind = k + 1, "shock"
    return {"year": int(years[i]), "kind": kind, "index": int(i), "value": float(y[i])}


# ------------------------------------------------------------------ validation (pure code)

def validate(resp, detected_year: int, cfg: dict | None = None) -> tuple[str | None, str]:
    """Return (label, reason). label is None when rejected."""
    lc = (cfg or get_config())["labels"]
    if not isinstance(resp, dict):
        return None, "not a JSON object"
    label, ey, conf = resp.get("label"), resp.get("event_year"), resp.get("confident")
    if conf is not True:
        return None, "not confident"
    if not isinstance(label, str) or not label.strip():
        return None, "no label"
    label = " ".join(label.strip().strip(".").split())
    if isinstance(ey, bool) or not isinstance(ey, int):
        try:
            ey = int(ey)
        except (TypeError, ValueError):
            return None, "event_year not an integer"
    if abs(ey - detected_year) > lc["year_tolerance"]:
        return None, f"event_year {ey} not within ±{lc['year_tolerance']} of {detected_year}"
    if len(label.split()) > lc["max_words"]:
        return None, f"more than {lc['max_words']} words"
    if len(label) > lc["max_chars"]:
        return None, f"longer than {lc['max_chars']} chars"
    # Gemini must not supply numbers: the only digits allowed are a year near the turning point.
    for num in re.findall(r"\d+", label):
        if not (len(num) == 4 and abs(int(num) - detected_year) <= lc["year_tolerance"]):
            return None, f"contains a number ({num})"
    if "%" in label or "$" in label:
        return None, "contains a quantity"
    return label, "ok"


def parse_json_text(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# ------------------------------------------------------------------ Gemini

def _gcfg() -> dict:
    return get_config()["gemini"]


def _model_cache():
    return path("cache") / "gemini_model.json"


def list_models() -> list[dict]:
    key = secret("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing")
    out, token = [], None
    while True:
        params = {"pageSize": 1000}
        if token:
            params["pageToken"] = token
        r = requests.get(f"{_gcfg()['api_base']}/models", params=params,
                         headers={"x-goog-api-key": key}, timeout=_gcfg()["timeout"])
        if r.status_code != 200:
            raise RuntimeError(f"models list HTTP {r.status_code}: {r.text[:300]}")
        d = r.json()
        out += d.get("models", [])
        token = d.get("nextPageToken")
        if not token:
            return out


def choose_flash_model(models: list[dict], prefer=None, avoid=None) -> str | None:
    g = _gcfg()
    prefer = prefer or g["prefer"]
    avoid = avoid or g["avoid"]
    cands = []
    for m in models:
        name = m.get("name", "")
        short = name.split("/")[-1].lower()
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if not any(p in short for p in prefer) or any(a in short for a in avoid):
            continue
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", short)] or [0.0]
        cands.append((nums[0], -len(short), name.split("/")[-1]))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][2]


def resolve_model(refresh: bool = False) -> str:
    """Model from config, or on first run list models, pick a flash-class one and log it."""
    g = _gcfg()
    if g["model"] != "auto":
        return g["model"]
    f = _model_cache()
    if f.exists() and not refresh:
        return json.loads(f.read_text(encoding="utf-8"))["model"]
    models = list_models()
    m = choose_flash_model(models)
    if not m:
        raise RuntimeError(f"no flash-class model with generateContent among {len(models)} models")
    f.write_text(json.dumps({"model": m, "chosen_at": datetime.now(timezone.utc).isoformat(),
                             "available": [x.get("name") for x in models]}, indent=1), encoding="utf-8")
    log.info("Gemini: chose model %s from %d listed models", m, len(models))
    return m


PROMPT = """You help caption a data video. One country's statistic shows a clear turning point.

Country: {country}
Metric: {metric}
Turning point year: {year} ({kind})
Values around it: {values}

Name the well-known real-world event that explains this change, in 2 to 4 words, for example
"2011 revolution", "Oil price crash", "COVID-19 lockdowns", "Fukushima disaster", "Euro debt crisis".
Rules:
- Only name an event you are confident actually happened in {country} (or globally) around {year}
  and plausibly caused this move. If unsure, set "confident" to false.
- Do not include any numbers except a year.
- event_year is the year the event happened.
Reply with JSON only: {{"label": string, "event_year": integer, "confident": boolean}}"""


def ask_gemini(model: str, prompt: str) -> dict | None:
    key = secret("GEMINI_API_KEY")
    g = _gcfg()
    url = f"{g['api_base']}/models/{model}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    delay = 2.0
    for attempt in range(g["max_retries"] + 1):
        try:
            r = requests.post(url, json=body, headers={"x-goog-api-key": key}, timeout=g["timeout"])
        except requests.RequestException as e:
            log.warning("Gemini request error %s (attempt %d)", e, attempt + 1)
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code == 200:
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                return parse_json_text("".join(p.get("text", "") for p in parts))
            except (KeyError, IndexError, ValueError):
                log.warning("Gemini: unexpected response shape: %s", r.text[:300])
                return None
        if r.status_code == 429 or r.status_code >= 500:
            txt = r.text
            if r.status_code == 429 and ("PerDay" in txt or "per day" in txt.lower()):
                raise QuotaExhausted(txt[:300])
            log.info("Gemini HTTP %s, backing off %.0fs", r.status_code, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
    raise QuotaExhausted("rate limited after retries")


def _label_cache() -> dict:
    f = path("cache") / "labels.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _save_label_cache(c: dict) -> None:
    (path("cache") / "labels.json").write_text(json.dumps(c, indent=1), encoding="utf-8")


def label_run(topic: dict, countries: list[str], series: dict, indicator_name: str, names: dict) -> dict:
    """Returns {iso3: {"year", "kind", "label" (str|None), "reason"}} and logs why labels are missing."""
    cfg = get_config()
    out = {}
    for iso in countries:
        tp = turning_point(series[iso], topic["start_year"], cfg)
        out[iso] = {**(tp or {}), "label": None, "reason": "no turning point" if not tp else "pending"}
    if not cfg["labels"]["enabled"]:
        for v in out.values():
            v["reason"] = "labels disabled in config"
        return out
    if not secret("GEMINI_API_KEY"):
        log.info("labels: GEMINI_API_KEY missing, continuing without labels")
        for v in out.values():
            v["reason"] = "no GEMINI_API_KEY"
        return out
    try:
        model = resolve_model()
    except Exception as e:  # noqa: BLE001
        log.warning("labels: cannot resolve Gemini model (%s); continuing without labels", e)
        for v in out.values():
            v["reason"] = f"model unavailable: {e}"[:200]
        return out
    cache = _label_cache()
    exhausted = False
    for iso in countries:
        v = out[iso]
        if "year" not in v:
            continue
        ck = f"{topic['id']}|{iso}|{v['year']}"
        if ck in cache:
            resp = cache[ck]["response"]
        elif exhausted:
            v["reason"] = "Gemini quota exhausted"
            continue
        else:
            rows = {y: val for y, val in series[iso]}
            around = [y for y in range(v["year"] - 3, v["year"] + 4) if y in rows]
            values = ", ".join(f"{y}: {rows[y]:.4g}" for y in around)
            prompt = PROMPT.format(country=names[iso], metric=f"{indicator_name} ({topic['subtitle']})",
                                   year=v["year"], kind=v["kind"], values=values)
            try:
                resp = ask_gemini(model, prompt)
            except QuotaExhausted as e:
                log.warning("labels: Gemini quota exhausted (%s); remaining countries get no label", e)
                exhausted = True
                v["reason"] = "Gemini quota exhausted"
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("labels: Gemini failed for %s: %s", iso, e)
                v["reason"] = f"Gemini error: {e}"[:200]
                continue
            cache[ck] = {"response": resp, "model": model, "ts": datetime.now(timezone.utc).isoformat()}
            _save_label_cache(cache)
        label, reason = validate(resp, v["year"], cfg)
        v["label"], v["reason"], v["raw"] = label, reason, resp
    return out
