"""Validated, backed-up, atomic owner settings writes."""
from __future__ import annotations

import io
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

import yaml

from . import config, topics

_write_lock = threading.Lock()


CONFIG_INDENT = (2, 4, 2)   # config.yaml's own list style ("  - item"); topics.yaml uses ruamel's default


def update_yaml(target: Path, edit, indent: tuple | None = None) -> Path:
    """Edit a YAML file keeping its comments and layout (no re-wrapping, lists indented as they were)."""
    with _write_lock:
        original = target.read_text(encoding="utf-8")
        try:
            from ruamel.yaml import YAML
        except ImportError:
            document = yaml.safe_load(original)
            edit(document)
            rendered = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        else:
            codec = YAML()
            codec.preserve_quotes = True
            codec.width = 4096
            if indent:
                codec.indent(mapping=indent[0], sequence=indent[1], offset=indent[2])
            document = codec.load(original)
            edit(document)
            buffer = io.StringIO()
            codec.dump(document, buffer)
            rendered = buffer.getvalue()
        yaml.safe_load(rendered)  # validate before touching either file
        backup = target.with_name(f"{target.name}.bak-{datetime.now():%Y%m%d-%H%M%S-%f}")
        shutil.copy2(target, backup)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(rendered, encoding="utf-8", newline="\n")   # the repo's line endings, also on Windows
        temporary.replace(target)
        getattr(config.get_config, "cache_clear", lambda: None)()
        return backup


def save_posting(values: dict) -> Path:
    from .accounts import PLATFORMS
    try:
        count = int(values["videos_per_day"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Enter a whole number of videos per day.")
    if not 1 <= count <= 24:
        raise ValueError("Choose between 1 and 24 videos per day.")
    times = [s.strip() for s in values.get("posting_times", "").split(",") if s.strip()]
    if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", s) for s in times):
        raise ValueError("Use times like 08:00, 18:00 (24-hour clock).")
    if len(set(times)) != len(times):
        raise ValueError("Each posting time must be different.")
    if len(times) > count:
        raise ValueError("Choose no more posting times than videos per day.")

    def edit(document):
        for p in PLATFORMS:
            document[p]["enabled"] = values.get(p + "_enabled") == "on"
            document["dry_run"][p] = values.get(p + "_dry") == "on"
        document["cadence"]["videos_per_day"] = count
        # PyYAML reads YAML 1.1: unquoted 18:00 would become the integer 1080.
        try:
            from ruamel.yaml.scalarstring import DoubleQuotedScalarString
            document["cadence"]["posting_times"] = [DoubleQuotedScalarString(t) for t in sorted(times)]
        except ImportError:
            document["cadence"]["posting_times"] = sorted(times)
    return update_yaml(config.ROOT / "config.yaml", edit, CONFIG_INDENT)


def go_live(live: list[str]) -> Path:
    """Instagram / Facebook: on and out of test mode when in `live`, off otherwise. YouTube: off,
    keeping its credentials and its own test-mode switch (so turning it back on starts in test mode)."""
    def edit(document):
        for p in ("instagram", "facebook"):
            document[p]["enabled"] = p in live
            if p in live:
                document["dry_run"][p] = False
        document["youtube"]["enabled"] = False
    return update_yaml(config.ROOT / "config.yaml", edit, CONFIG_INDENT)


def toggle_topic(topic_id: str, enabled: bool) -> Path:
    def edit(document):
        topic = next((t for t in document if t["id"] == topic_id), None)
        if not topic:
            raise ValueError("This topic no longer exists.")
        topic["enabled"] = enabled
    return update_yaml(topics.TOPICS_FILE, edit)
