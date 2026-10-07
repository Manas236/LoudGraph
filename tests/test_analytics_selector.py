import random
from datetime import datetime, timedelta, timezone

from pipeline.analytics import compute_weights

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def row(topic, views, hours_ago, platform="youtube"):
    return {"topic_id": topic, "views": views, "platform": platform,
            "posted_at": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")}


def test_weights_shrink_and_age_filter():
    rows = [row("a", 10000, 100), row("a", 12000, 100), row("a", 8000, 100),
            row("b", 100, 100),
            row("c", 999999, 10)]          # too young, ignored
    w = compute_weights(rows, 72, 3, 3, now=NOW)
    assert "c" not in w
    assert w["a"]["n_posts"] == 3 and w["a"]["weight"] == w["a"]["mean_z"]
    # b has 1 post: pulled toward the global mean, so above its own mean
    assert w["b"]["weight"] > w["b"]["mean_z"]
    assert w["a"]["weight"] > w["b"]["weight"]


def test_views_are_zscored_per_platform():
    """B5: Instagram's bigger raw numbers must not dominate. Topic 'yt' is the best YouTube topic,
    'ig' is the WORST Instagram topic but has far more raw views than anything on YouTube."""
    rows = ([row("yt", v, 100, "youtube") for v in (900, 1000, 1100)]
            + [row("ytlow", v, 100, "youtube") for v in (90, 100, 110)]
            + [row("ig", v, 100, "instagram") for v in (20000, 21000, 22000)]
            + [row("igtop", v, 100, "instagram") for v in (200000, 210000, 220000)])
    w = compute_weights(rows, 72, 3, 3, now=NOW)
    assert w["yt"]["weight"] > w["ig"]["weight"]
    assert w["igtop"]["weight"] > w["ig"]["weight"] and w["yt"]["weight"] > w["ytlow"]["weight"]
    assert abs(w["yt"]["weight"] - w["igtop"]["weight"]) < 0.05   # both are "the best on their platform"


def test_retire_after_three_bottom_quartile_posts():
    # 12 posts, so the bottom quartile holds 3 of them
    rows = [row("good", v, 100) for v in (5000, 6000, 7000, 8000, 5500, 6500, 7500, 8500)] + \
           [row("bad", v, 100) for v in (10, 12, 11)] + [row("mid", 900, 100)]
    w = compute_weights(rows, 72, 3, 3, now=NOW)
    assert w["bad"]["retired"] and not w["good"]["retired"] and not w["mid"]["retired"]


def test_selector_respects_cooldown_and_retirement(temp_db, monkeypatch):
    from pipeline import selector
    topics = [{"id": t} for t in ("t1", "t2", "t3")]
    monkeypatch.setattr(selector, "load_topics", lambda: topics)
    db = temp_db
    db.create_run("t1")                              # recent -> cooldown
    db.set_topic_weight("t2", 1.0, 3, 1.0, retired=True, retired_reason="x")
    picks = {selector.choose_topic(rng=random.Random(i)) for i in range(20)}
    assert picks == {"t3"}


def test_selector_exploit_prefers_heavier_topics(temp_db, monkeypatch):
    from pipeline import selector
    monkeypatch.setattr(selector, "load_topics", lambda: [{"id": "hi"}, {"id": "lo"}])
    db = temp_db
    db.set_topic_weight("hi", 10.0, 5, 10.0)
    db.set_topic_weight("lo", 4.0, 5, 4.0)
    rng = random.Random(0)
    picks = [selector.choose_topic(rng=rng) for _ in range(200)]
    assert picks.count("hi") > 150
