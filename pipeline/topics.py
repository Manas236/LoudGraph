"""topics.yaml loading and `topics-verify`."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import yaml

from .config import ROOT, get_config, path

log = logging.getLogger(__name__)
TOPICS_FILE = ROOT / "topics.yaml"
REQUIRED = ["id", "source", "code", "title", "subtitle", "unit_format", "start_year", "min_meaningful",
            "modelled_data_warning"]


def load_topics() -> list[dict]:
    with open(TOPICS_FILE, encoding="utf-8") as f:
        topics = yaml.safe_load(f) or []
    for t in topics:
        missing = [k for k in REQUIRED if k not in t]
        if missing:
            raise ValueError(f"topic {t.get('id')} missing {missing}")
    return topics


def get_topic(topic_id: str) -> dict:
    for t in load_topics():
        if t["id"] == topic_id:
            return t
    raise KeyError(f"topic {topic_id} not in topics.yaml (or commented out)")


def format_value(topic: dict, v: float) -> str:
    return topic["unit_format"].format(v * topic.get("value_scale", 1.0))


def source_label(topic: dict) -> str:
    return "World Bank" if topic["source"] == "worldbank" else "Our World in Data"


def verify_report() -> dict:
    f = path("cache") / "topics_verify.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def dropped_topics() -> list[dict]:
    """Commented-out topic blocks with their DROPPED reason, parsed from topics.yaml."""
    out = []
    text = TOPICS_FILE.read_text(encoding="utf-8")
    for m in re.finditer(r"^# DROPPED ([^:\n]+): (.*)\n# - id: (\S+)", text, re.M):
        out.append({"id": m.group(3), "date": m.group(1), "reason": m.group(2)})
    return out


def _split_blocks(text: str) -> list[tuple[str, str | None]]:
    """Split topics.yaml into (chunk, topic_id_or_None). Active topic blocks start with '- id:'."""
    lines = text.splitlines(keepends=True)
    chunks, cur, cur_id = [], [], None
    for ln in lines:
        m = re.match(r"^- id:\s*(\S+)", ln)
        if m:
            if cur:
                chunks.append(("".join(cur), cur_id))
            cur, cur_id = [ln], m.group(1)
        elif cur_id is not None and (ln.startswith("  ") or ln.strip() == ""):
            cur.append(ln)
        else:
            if cur_id is not None:
                chunks.append(("".join(cur), cur_id))
                cur, cur_id = [], None
            cur.append(ln)
    if cur:
        chunks.append(("".join(cur), cur_id))
    return chunks


def _comment_out(block: str, reason: str) -> str:
    body = block.rstrip("\n")
    trail = block[len(body):]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    commented = "\n".join("# " + ln if ln.strip() else "#" for ln in body.splitlines())
    return f"# DROPPED {date}: {reason}\n{commented}{trail}"


def verify_topics(force: bool = False) -> int:
    from .fetch import FetchError, fetch_topic
    from .score import score_topic

    cfg = get_config()
    need_usable = cfg["data"]["verify_min_countries"]
    need_pass = cfg["picker"]["min_countries"]
    min_pts = cfg["scorer"]["min_points"]
    text = TOPICS_FILE.read_text(encoding="utf-8")
    chunks = _split_blocks(text)
    report = verify_report()
    new_chunks, kept, dropped = [], [], []
    for chunk, tid in chunks:
        if tid is None:
            new_chunks.append(chunk)
            continue
        topic = yaml.safe_load(chunk)[0]
        reason, info = None, {}
        try:
            data = fetch_topic(topic, force=force)
            usable = [iso for iso, rows in data["series"].items()
                      if sum(1 for y, _ in rows if y >= topic["start_year"]) >= min_pts]
            scored = score_topic(data, topic)
            passing = [s["iso3"] for s in scored if s["ok"]]
            info = {
                "indicator_name": data["indicator_name"],
                "countries_with_data": len(data["series"]),
                "usable": len(usable),
                "passing": len(passing),
                "passing_countries": passing,
                "wb_modelled": data.get("wb_modelled", False),
                "latest_year": max((r[-1][0] for r in data["series"].values()), default=None),
            }
            if len(usable) < need_usable:
                reason = f"only {len(usable)} pool countries with >= {min_pts} points since {topic['start_year']} (need {need_usable})"
            elif len(passing) < need_pass:
                reason = f"only {len(passing)} countries pass the interestingness scorer (need {need_pass})"
            if data.get("wb_modelled") and not topic["modelled_data_warning"]:
                log.warning("%s: WDI name says modelled but modelled_data_warning is false", tid)
        except FetchError as e:
            reason = f"fetch failed: {e}"
        except Exception as e:  # noqa: BLE001 - record and keep going
            reason = f"error: {type(e).__name__}: {e}"
        entry = {"status": "verified" if reason is None else "dropped", "reason": reason,
                 "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **info}
        report[tid] = entry
        if reason:
            dropped.append((tid, reason))
            new_chunks.append(_comment_out(chunk, reason))
            log.warning("DROP %-28s %s", tid, reason)
        else:
            kept.append(tid)
            new_chunks.append(chunk)
            log.info("OK   %-28s usable=%d passing=%d  %s", tid, info["usable"], info["passing"], info["indicator_name"][:60])
    TOPICS_FILE.write_text("".join(new_chunks), encoding="utf-8", newline="\n")
    (path("cache") / "topics_verify.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nverified {len(kept)} topics, dropped {len(dropped)}")
    for tid, r in dropped:
        print(f"  dropped {tid}: {r}")
    return 0
