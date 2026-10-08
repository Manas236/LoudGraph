"""Config, paths, secrets and logging. Everything is resolved relative to the project root."""
from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

SECRET_NAMES = [
    "GEMINI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "YT_CLIENT_SECRET_FILE",
    "IG_USER_ID",
    "IG_ACCESS_TOKEN",
    "FB_PAGE_ID",
]


def get_config() -> dict:
    target = ROOT / "config.yaml"
    return _read_config(str(target), target.stat().st_mtime_ns)


@lru_cache(maxsize=1)
def _read_config(filename: str, modified: int) -> dict:
    with open(filename, encoding="utf-8") as f:
        return yaml.safe_load(f)


get_config.cache_clear = _read_config.cache_clear


def brand_name() -> str:
    """The product name shown to the owner (dashboard, Telegram, doctor). Not drawn on videos."""
    return (get_config().get("brand") or {}).get("name") or "Graphony"


def path(key: str) -> Path:
    """Resolve a `paths.<key>` entry to an absolute Path (directories are created)."""
    p = ROOT / get_config()["paths"][key]
    if key != "db":
        p.mkdir(parents=True, exist_ok=True)
    return p


def run_dir(run_id: str) -> Path:
    d = path("out") / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def secret(name: str) -> str | None:
    """The ONLY way code reads the environment. SECRET_NAMES must match .env.example (a test checks)."""
    if name not in SECRET_NAMES:
        raise KeyError(f"{name} is not a known secret; add it to SECRET_NAMES and .env.example")
    v = os.environ.get(name, "").strip()
    return v or None


def countries() -> list[dict]:
    return get_config()["countries"]


def country_by_iso3() -> dict[str, dict]:
    return {c["iso3"]: c for c in countries()}


def setup_logging(level: int = logging.INFO) -> None:
    if logging.getLogger().handlers:
        return
    # Windows consoles default to cp1252; captions and alerts contain emoji and non-ASCII names
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    log_dir = path("cache") / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(log_dir / "pipeline.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(sh)
    root.addHandler(fh)
    for noisy in ("urllib3", "googleapiclient.discovery_cache", "werkzeug", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
