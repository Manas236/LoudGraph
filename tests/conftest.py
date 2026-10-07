import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the DB at a temp file so tests never touch the real state."""
    from pipeline import config, db
    cfg = dict(config.get_config())
    cfg["paths"] = dict(cfg["paths"], db=str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(db, "path", lambda key: tmp_path / "test.db")
    db.init()
    return db
