"""A3: the old channel name must not appear anywhere the pipeline produces text."""
import re

from pipeline.config import ROOT, get_config

BANNED = re.compile(r"loud\s*graphs", re.I)
SKIP_DIRS = {".git", ".venv", "out", "cache", "__pycache__", ".pytest_cache", "tokens"}
TEXT_EXT = {".py", ".md", ".yaml", ".yml", ".html", ".txt", ".json", ".example", ".toml", ".cfg", ".ini", ""}


def test_brand_is_graphony_and_the_watermark_is_off_by_default():
    assert get_config()["brand"]["name"] == "Graphony"
    assert get_config()["render"]["watermark"] is False


def test_name_not_in_repo_text():
    hits = []
    for p in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts) or not p.is_file():
            continue
        if p.suffix.lower() not in TEXT_EXT or p.name == "test_brand.py":
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            # the local checkout folder may keep its name (owner's call); nothing else may mention it
            if BANNED.search(line.replace("Desktop/LoudGraphs", "").replace("Desktop\\LoudGraphs", "")):
                hits.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:80]}")
    assert not hits, hits
