import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch):
    """pipeline.config loads the owner's .env at import; no test may see (or call an API with) those values.
    Tests that need a secret set a fake one with monkeypatch.setenv."""
    from pipeline.config import SECRET_NAMES
    for name in SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)


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
