"""The Meta request limit: every Graph request waits until the one before it is old enough, and the
time of the last request is shared through the database, so the gap holds across processes."""
import inspect
import threading

import pytest


@pytest.fixture
def limit(temp_db, pinned_config, monkeypatch):
    """The limit on (3 s), with a clock that only moves when the code under test sleeps."""
    from pipeline import meta_api
    pinned_config["meta_api"]["min_gap_seconds"] = 3

    class Clock:
        now, slept = 1000.0, []

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.slept.append(round(seconds, 3))
            self.now += seconds
    clock = Clock()
    monkeypatch.setattr(meta_api, "time", clock)   # only this module's clock: time.time() and time.sleep()
    return meta_api, clock


def test_a_request_waits_until_the_last_one_is_old_enough(limit, monkeypatch):
    meta_api, clock = limit
    sent = []
    monkeypatch.setattr(meta_api.requests, "get", lambda url, **kw: sent.append(("GET", url, clock.now)) or "response")
    monkeypatch.setattr(meta_api.requests, "post", lambda url, **kw: sent.append(("POST", url, clock.now)) or "response")
    assert meta_api.get("https://graph/a", timeout=30) == "response"      # nothing before it: goes at once
    meta_api.post("https://graph/b", data={"x": 1})                        # right behind it: waits a full gap
    clock.now += 1                                                         # one second of other work
    meta_api.get("https://graph/c")                                        # only the missing two seconds
    clock.now += 60
    meta_api.get("https://graph/d")                                        # long after: no wait
    assert clock.slept == [3, 2]
    assert [(m, u, t) for m, u, t in sent] == [("GET", "https://graph/a", 1000), ("POST", "https://graph/b", 1003),
                                               ("GET", "https://graph/c", 1006), ("GET", "https://graph/d", 1066)]


def test_the_gap_is_shared_by_everything_that_uses_the_database(limit):
    """Publishers, the bot and the dashboard are separate processes: each takes the next free moment
    from the database, so requests sent at the same instant still leave one gap apart, in order."""
    meta_api, _ = limit
    turns, errors = [], []

    def caller():
        try:
            turns.append(meta_api.reserve(1000.0))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=caller) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sorted(turns) == [1000.0 + 3 * i for i in range(8)]


def test_the_gap_can_be_changed_or_turned_off(limit, pinned_config, monkeypatch):
    meta_api, clock = limit
    monkeypatch.setattr(meta_api.requests, "get", lambda url, **kw: None)
    pinned_config["meta_api"]["min_gap_seconds"] = 10
    meta_api.get("a")
    meta_api.get("b")
    assert clock.slept == [10]
    pinned_config["meta_api"]["min_gap_seconds"] = 0
    monkeypatch.setattr(meta_api.db, "db", lambda: pytest.fail("off: the database is not even asked"))
    meta_api.get("c")
    meta_api.get("d")
    assert clock.slept == [10]
    del pinned_config["meta_api"]                                          # an older config.yaml: the default applies
    assert meta_api.min_gap() == meta_api.DEFAULT_GAP_S == 3


def test_a_mark_far_in_the_future_never_holds_a_request_for_long(limit):
    meta_api, clock = limit
    from pipeline import db
    db.kv_set(meta_api.K_NEXT, repr(clock.now + 86400))                   # the PC clock was set back a day
    assert meta_api.wait_turn() == meta_api.MAX_WAIT_S
    db.kv_set(meta_api.K_NEXT, "not a number")
    assert meta_api.wait_turn() == 0


def test_the_limit_never_stops_a_post(limit, monkeypatch):
    """If the time of the last request cannot be read, the request still goes out, one gap later."""
    meta_api, clock = limit
    import sqlite3

    def locked(now):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(meta_api, "reserve", locked)
    sent = []
    monkeypatch.setattr(meta_api.requests, "post", lambda url, **kw: sent.append(url))
    meta_api.post("https://graph/media_publish")
    assert sent == ["https://graph/media_publish"] and clock.slept == [3]


def test_every_request_to_meta_goes_through_the_limit():
    """No module talks to the Graph API (or rupload) with `requests` directly."""
    from pipeline import analytics, posted, publish_instagram
    for module in (publish_instagram, posted, analytics):
        source = inspect.getsource(module)
        assert "requests.get(" not in source and "requests.post(" not in source, module.__name__
        assert "meta_api.get(" in source, module.__name__


def test_a_publish_spaces_its_own_requests(limit, monkeypatch, tmp_path, pinned_config):
    """The whole Instagram publish through the faked Graph API: five requests, each a gap after the last."""
    meta_api, clock = limit
    from pipeline import db, publish_instagram as pi
    pinned_config["paths"].update(out=str(tmp_path / "out"))
    pinned_config["dry_run"]["instagram"] = False
    pinned_config["instagram"]["poll_seconds"] = 0
    monkeypatch.setenv("IG_USER_ID", "7")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(pi, "time", clock)
    monkeypatch.setattr(pi, "get_config", lambda: pinned_config)   # not a config an earlier test left in the module
    times = []

    class R:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    def post(url, **kw):
        times.append(clock.now)
        return R({"id": "c1", "success": True})

    def get(url, **kw):
        times.append(clock.now)
        return R({"status_code": "FINISHED", "permalink": "https://instagram.com/reel/x"})
    monkeypatch.setattr(meta_api.requests, "post", post)
    monkeypatch.setattr(meta_api.requests, "get", get)
    rid = db.create_run("inflation")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\0" * 16)
    assert pi.publish(rid, {"description_instagram": "caption"}, video) == "live"
    assert times == [1000, 1003, 1006, 1009, 1012]      # container, upload, status, publish, permalink
