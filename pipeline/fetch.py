"""World Bank (wbgapi) + Our World in Data loaders. Every series is cached under cache/series/.

Output shape (also the cache file shape):
    {
      "topic_id", "source", "code", "start_year",
      "indicator_name", "citation", "unit", "wb_modelled",
      "fetched_at",
      "series": {"IND": [[1990, 30.2], [1991, 30.4], ...], ...}   # pool countries only, sorted, no NaN
    }
"""
from __future__ import annotations

import io
import json
import logging
import math
import re
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from .config import country_by_iso3, get_config, path

log = logging.getLogger(__name__)
UA = {"User-Agent": "data-sonification-pipeline/1.0 (contact via repo owner)"}


class FetchError(RuntimeError):
    pass


def _cache_file(topic_id: str):
    d = path("cache") / "series"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{topic_id}.json"


def _age_days(iso_ts: str) -> float:
    t = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_cached(topic_id: str) -> dict | None:
    f = _cache_file(topic_id)
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def fetch_topic(topic: dict, force: bool = False) -> dict:
    cfg = get_config()
    cached = load_cached(topic["id"])
    if (
        cached and not force
        and cached.get("code") == topic["code"]
        and cached.get("start_year") == topic["start_year"]
        and _age_days(cached["fetched_at"]) < cfg["cache_days"]
    ):
        return cached
    if topic["source"] == "worldbank":
        data = _fetch_worldbank(topic)
    elif topic["source"] == "owid":
        data = _fetch_owid(topic)
    else:
        raise FetchError(f"unknown source {topic['source']}")
    data.update(topic_id=topic["id"], source=topic["source"], code=topic["code"],
                start_year=topic["start_year"], fetched_at=_now())
    _cache_file(topic["id"]).write_text(json.dumps(data), encoding="utf-8")
    log.info("fetched %s (%s %s): %d pool countries", topic["id"], topic["source"], topic["code"], len(data["series"]))
    return data


def _clean(rows) -> list[list]:
    out = []
    for y, v in rows:
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fv):
            out.append([int(y), fv])
    out.sort()
    return out


# ------------------------------------------------------------------ World Bank

def _fetch_worldbank(topic: dict) -> dict:
    import wbgapi as wb

    pool = list(country_by_iso3())
    code = topic["code"]
    meta = None
    for attempt in range(4):  # the WB API returns sporadic non-JSON error pages; retry before giving up
        try:
            meta = wb.series.get(code)
            break
        except Exception as e:  # noqa: BLE001 - wbgapi raises several types
            if attempt == 3:
                raise FetchError(f"WDI code {code} lookup failed after 4 tries: {e}") from e
            log.warning("WDI lookup %s failed (%s), retrying", code, e)
            time.sleep(3 * (attempt + 1))
    name = meta.get("value") if isinstance(meta, dict) else str(meta)
    last = datetime.now().year
    df = None
    for attempt in range(3):
        try:
            df = wb.data.DataFrame(code, economy=pool, time=range(topic["start_year"], last + 1),
                                   labels=False, numericTimeKeys=True)
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise FetchError(f"WDI data request for {code} failed: {e}") from e
            time.sleep(2 * (attempt + 1))
    series = {}
    if df is not None and not df.empty:
        for iso3, row in df.iterrows():
            if iso3 not in pool:
                continue
            rows = _clean(row.items())
            if rows:
                series[iso3] = rows
    return {
        "indicator_name": name,
        "citation": "World Bank, World Development Indicators",
        "unit": None,
        "wb_modelled": "model" in (name or "").lower(),
        "series": series,
    }


# ------------------------------------------------------------------ OWID

def _http_get(url: str) -> requests.Response:
    timeout = get_config()["data"]["http_timeout"]
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (400, 403, 404):
                break
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise FetchError(f"GET {url} failed: {last}")


def _bulk_csv(dataset: str):
    cfg = get_config()
    urls = cfg["data"]["owid_bulk"]
    if dataset not in urls:
        raise FetchError(f"unknown OWID bulk dataset {dataset}; known: {list(urls)}")
    d = path("cache") / "owid"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"owid-{dataset}.csv"
    stamp = d / f"owid-{dataset}.fetched"
    fresh = f.exists() and stamp.exists() and _age_days(stamp.read_text().strip()) < cfg["cache_days"]
    if not fresh:
        r = _http_get(urls[dataset])
        f.write_bytes(r.content)
        stamp.write_text(_now())
    return f


def _fetch_owid(topic: dict) -> dict:
    cfg = get_config()
    pool = set(country_by_iso3())
    code = topic["code"]
    slug, _, column = code.rpartition("/")
    if not slug or not column:
        raise FetchError(f"OWID code must be '<slug>/<column>' or 'github:<dataset>/<column>', got {code}")
    if slug.startswith("github:"):
        dataset = slug.split(":", 1)[1]
        f = _bulk_csv(dataset)
        head = pd.read_csv(f, nrows=0).columns
        if column not in head:
            raise FetchError(f"column {column} not in owid/{dataset}")
        df = pd.read_csv(f, usecols=["iso_code", "year", column]).rename(columns={"iso_code": "code"})
        name = f"{column} (OWID {dataset})"
        citation = f"Our World in Data, owid/{dataset}"
        unit = None
        cb_url = cfg["data"].get("owid_codebook", {}).get(dataset)
        if cb_url:
            try:
                cb = pd.read_csv(io.StringIO(_http_get(cb_url).text))
                row = cb[cb["column"] == column]
                if not row.empty:
                    r0 = row.iloc[0]
                    name = str(r0.get("title") or name)
                    unit = None if pd.isna(r0.get("unit")) else str(r0.get("unit"))
                    if not pd.isna(r0.get("source")):
                        citation = re.sub(r"\s*\[https?://[^\]]*\]", "", str(r0["source"])).strip()
            except Exception as e:  # noqa: BLE001 - the codebook only improves labels
                log.warning("OWID codebook for %s unavailable: %s", dataset, e)
    else:
        r = _http_get(cfg["data"]["owid_grapher_url"].format(slug=slug))
        df = pd.read_csv(io.StringIO(r.text))
        if column not in df.columns:
            raise FetchError(f"column {column} not in grapher/{slug}; have {list(df.columns)[:8]}")
        df = df[["code", "year", column]]
        name, citation, unit = slug, f"Our World in Data, {slug}", None
        try:
            m = _http_get(cfg["data"]["owid_metadata_url"].format(slug=slug)).json()
            col = m.get("columns", {}).get(column, {})
            name = col.get("titleLong") or col.get("titleShort") or slug
            unit = col.get("unit")
            citation = (m.get("chart", {}) or {}).get("citation") or citation
        except Exception as e:  # noqa: BLE001 - metadata is optional
            log.warning("OWID metadata for %s unavailable: %s", slug, e)
    df = df.dropna(subset=["code"])
    df = df[df["code"].isin(pool) & ~df["code"].astype(str).str.startswith("OWID_")]
    df = df[df["year"] >= topic["start_year"]]
    series = {}
    for iso3, g in df.groupby("code"):
        rows = _clean(zip(g["year"], g[column]))
        if rows:
            series[iso3] = rows
    return {"indicator_name": name, "citation": citation, "unit": unit, "wb_modelled": False, "series": series}
