"""Every Gemini call goes through ask_json(): labels (labels.py) and captions (captions.py).

Models are tried in order: gemini.model (the primary, gemini-2.5-flash; "auto" = list the models once,
pick a flash-class one and cache the choice in cache/gemini_model.json), then each of
gemini.fallback_models (gemini-3.8-flash).
An overloaded model (HTTP 503), a rate limit or exhausted quota (429), any other 5xx, an unknown
model (404) or a network error / timeout moves on to the next model AT ONCE: no minutes of retries.
The model that answered is logged and returned. When none answers, QuotaExhausted is raised and the
caller goes on without Gemini (no label; a plain template caption).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import requests

from .config import get_config, path, secret

log = logging.getLogger(__name__)


class QuotaExhausted(RuntimeError):
    """No Gemini model answered: every one was overloaded, rate limited, out of quota or unreachable."""


ALL_FAILED = {"count": 0, "last": ""}   # this process's calls where every model failed (the autopilot's alert reads it)


def _gcfg() -> dict:
    return get_config()["gemini"]


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


def _model_cache():
    return path("cache") / "gemini_model.json"


def resolve_model(refresh: bool = False) -> str:
    """The primary model: gemini.model, or for "auto" the flash-class model picked on the first run."""
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


def models() -> list[str]:
    """The primary model, then gemini.fallback_models, without repeats."""
    out = []
    try:
        out.append(resolve_model())
    except Exception as e:  # noqa: BLE001 - the fallbacks still work without the model list
        log.warning("Gemini: primary model unavailable (%s); using the fallback models", e)
    for m in _gcfg().get("fallback_models") or []:
        if m and m not in out:
            out.append(m)
    return out


def _why(r: requests.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return f"HTTP {r.status_code} {err.get('status', '')} {err.get('message', '')}".strip()[:160]
    except ValueError:
        return f"HTTP {r.status_code}"


def ask_json(prompt: str, what: str = "call", temperature: float = 0.2) -> tuple[object, str]:
    """(parsed JSON reply or None if it was not JSON, the model that answered). Raises QuotaExhausted
    when no model answered, RuntimeError for a request Gemini refuses outright (400/401/403)."""
    key = secret("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing")
    g = _gcfg()
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"}}
    tried = []
    for model in models():
        url = f"{g['api_base']}/models/{model}:generateContent"
        try:
            r = requests.post(url, json=body, headers={"x-goog-api-key": key}, timeout=g["timeout"])
        except requests.RequestException as e:
            tried.append(f"{model}: {type(e).__name__}")
            log.warning("Gemini %s: %s did not answer (%s); trying the next model", what, model, type(e).__name__)
            continue
        if r.status_code == 200:
            note = f" (after {'; '.join(tried)})" if tried else ""
            log.info("Gemini %s: served by %s%s", what, model, note)
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                return parse_json_text("".join(p.get("text", "") for p in parts)), model
            except (KeyError, IndexError, ValueError):
                log.warning("Gemini %s: unexpected response shape from %s: %s", what, model, r.text[:300])
                return None, model
        if r.status_code in (404, 429) or r.status_code >= 500:
            tried.append(f"{model}: {_why(r)}")
            log.warning("Gemini %s: %s %s; trying the next model", what, model, _why(r))
            continue
        raise RuntimeError(f"Gemini {model} {_why(r)}")
    ALL_FAILED["count"] += 1
    ALL_FAILED["last"] = "; ".join(tried) or "no model configured"
    raise QuotaExhausted("no Gemini model answered: " + ("; ".join(tried) or "no model configured"))
