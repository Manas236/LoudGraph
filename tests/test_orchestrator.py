"""B4: published / live runs are never re-rendered in place; --as-new copies the inputs."""
import json

import pytest


@pytest.fixture
def orch(temp_db, tmp_path, monkeypatch):
    from pipeline import config, orchestrator
    monkeypatch.setattr(orchestrator, "run_dir", lambda rid: (tmp_path / rid).mkdir(exist_ok=True) or tmp_path / rid)
    return orchestrator, temp_db, tmp_path


def _published_run(db, d):
    rid = db.create_run("inflation")
    for st in ["data_ready", "picked", "labelled", "rendered", "awaiting_approval", "approved", "publishing",
               "published"]:
        kw = {"countries": ["IND", "BRA"], "country_set": "BRA,IND", "score": 50.0} if st == "picked" else {}
        db.transition(rid, st, **kw)
    (d / rid).mkdir(exist_ok=True)
    for name in ("data.json", "scores.json", "pick.json", "labels.json"):
        (d / rid / name).write_text(json.dumps({"from": rid, "file": name}), encoding="utf-8")
    return rid


def test_refuses_published_run(orch):
    o, db, d = orch
    rid = _published_run(db, d)
    with pytest.raises(o.RefuseRerun, match="published"):
        o.run_pipeline(rid, "render")
    assert db.get_run(rid)["stage"] == "published"          # untouched
    assert any("refused" in e["message"] for e in db.get_log(rid))


def test_refuses_run_with_live_post(orch):
    o, db, d = orch
    rid = db.create_run("inflation")
    for st in ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]:
        db.transition(rid, st)
    db.upsert_post(rid, "youtube", "live", remote_id="x")
    assert "live posts on youtube" in o.rerun_blocker(rid)
    db.upsert_post(rid, "youtube", "dry_run")                 # a dry run is not a live post
    assert o.rerun_blocker(rid) is None


def test_copy_run_copies_inputs_and_state(orch):
    o, db, d = orch
    rid = _published_run(db, d)
    new = o.copy_run(rid, "render")
    r = db.get_run(new)
    assert r["stage"] == "labelled" and r["regen_of"] == rid and r["country_set"] == "BRA,IND"
    for name in ("data.json", "scores.json", "pick.json", "labels.json"):
        assert json.loads((d / new / name).read_text())["from"] == rid
    assert db.get_run(rid)["stage"] == "published"


def test_dashboard_retry_is_refused_for_published(orch, monkeypatch):
    from pipeline import actions
    o, db, d = orch
    monkeypatch.setattr(actions, "spawn", lambda *a: 0)
    rid = _published_run(db, d)
    with pytest.raises(ValueError, match="refused"):
        actions.retry(rid, "render", "dashboard")
    assert actions.rerender_as_new(rid, "render", "dashboard").startswith("re-rendering")
