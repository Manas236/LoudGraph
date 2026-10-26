import copy
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_OWNER_CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch):
    """pipeline.config loads the owner's .env at import; no test may see (or call an API with) those values.
    Tests that need a secret set a fake one with monkeypatch.setenv."""
    from pipeline.config import ENV_NAMES
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    # the approval flow (review card, posting queue, the Telegram bot) is what most tests exercise: it is reachable
    # with AUTOPILOT=false; the autopilot's own tests (test_autopilot.py) switch it on
    monkeypatch.setenv("AUTOPILOT", "false")


@pytest.fixture(autouse=True)
def pinned_config(monkeypatch):
    """Tests never depend on the owner's live switches: every get_config() returns a copy of
    config.yaml with platforms and test mode pinned (YouTube + Instagram on, Facebook off, all in
    test mode). A test that needs live posting changes this dict. The Meta request limit is off, so
    a faked Graph call neither waits nor touches the database; its own tests turn it on."""
    from pipeline import config
    cfg = copy.deepcopy(_OWNER_CONFIG)
    cfg["youtube"]["enabled"], cfg["instagram"]["enabled"], cfg["facebook"]["enabled"] = True, True, False
    cfg["dry_run"] = {"youtube": True, "instagram": True, "facebook": True}
    cfg["meta_api"] = {"min_gap_seconds": 0}
    # never the machine's own discovered topics or MIGRATED_TO_SERVER marker (the laptop keeps one after the move)
    cfg["paths"]["topics_auto"] = "tests/_no_topics_auto.yaml"
    cfg["paths"]["migrated_marker"] = "tests/_no_MIGRATED_TO_SERVER"
    monkeypatch.setattr(config, "_read_config", lambda filename, modified: cfg)
    return cfg


@pytest.fixture
def temp_db(tmp_path, monkeypatch, pinned_config):
    """Point the DB (and the cross-process lock files) at a temp folder so tests never touch the
    real state, nor collide with a bot or dashboard that is running on this machine.
    (config.get_config itself is never replaced: a module imported during a test would keep the
    replacement, and later tests would read this test's config.)"""
    from pipeline import db, lock
    pinned_config["paths"]["db"] = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "path", lambda key: tmp_path / "test.db")
    monkeypatch.setattr(lock, "path", lambda key: tmp_path)
    db.init()
    return db
