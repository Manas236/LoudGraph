"""Post captions and hashtags (pipeline/captions.py): facts computed in code, every variant checked in
code, Gemini retried then replaced by plain template fills, hashtag pools, the review card's caption
options, and posting with the chosen caption only when captions.use_for_posting is on. No network."""
import json
from datetime import datetime, timedelta, timezone

import pytest

TOPIC = {"id": "test_topic", "name": "Test metric", "subtitle": "Widgets per 100 people", "unit_format": "{:.1f}",
         "category": "tech", "source": "owid", "code": "x", "title": "t", "start_year": 2000, "min_meaningful": 0,
         "modelled_data_warning": False}
COUNTRIES = [("IND", "India"), ("USA", "United States"), ("FRA", "France"), ("JPN", "Japan")]
YEARS = list(range(2000, 2011))
SERIES = {
    "IND": list(zip(YEARS, [1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 30])),     # the only riser; passes everyone
    "USA": list(zip(YEARS, [20, 20, 20, 19, 19, 19, 19, 18, 18, 18, 18])),
    "FRA": list(zip(YEARS, [10, 13, 16, 19, 22, 25, 20, 15, 10, 7, 5])),       # peaks in 2005, then falls hard
    "JPN": list(zip(YEARS, [15, 15, 14, 14, 13, 13, 13, 12, 12, 12, 12])),
}


@pytest.fixture
def ctx():
    from pipeline import captions
    facts = captions.compute_facts(TOPIC, COUNTRIES, SERIES, 2000, 2010)
    return captions.Context(TOPIC, COUNTRIES, facts, 2000, 2010)


def fid(ctx, kind):
    return next(f["id"] for f in ctx.facts if f["kind"] == kind)


def variant(ctx, template_id, hook, kind, question_id="comes_last", question="Guess which country comes last?"):
    return {"template_id": template_id, "hook": hook, "question_id": question_id, "question": question,
            "fact_ids": [fid(ctx, kind)]}


# ------------------------------------------------------------------ facts

def test_facts_are_computed_from_the_data(ctx):
    by = {f["kind"]: f for f in ctx.facts}
    assert list(by) == ["surprising", "biggest_rise", "biggest_fall", "crossover", "sharpest", "peak",
                        "record_high", "record_low", "final_high", "final_low"]
    assert (by["final_high"]["country"], by["final_high"]["value"]) == ("India", "30.0")
    assert (by["final_low"]["country"], by["final_low"]["value"], by["final_low"]["year"]) == ("France", "5.0", 2010)
    s = by["surprising"]
    assert (s["country"], s["dirs"], s["only"], s["n_other_way"], s["n_others"]) == ("India", ["up"], True, 3, 3)
    assert (by["biggest_rise"]["country"], by["biggest_rise"]["from"], by["biggest_rise"]["to"]) == ("India", "1.0", "30.0")
    assert (by["biggest_fall"]["country"], by["biggest_fall"]["from"], by["biggest_fall"]["to"]) == ("France", "10.0", "5.0")
    c = by["crossover"]
    assert (c["country"], c["country2"], c["year"], c["from_year"]) == ("India", "France", 2007, 2006)
    assert by["sharpest"]["country"] == "France" and by["sharpest"]["dirs"] == ["down"]
    assert (by["peak"]["country"], by["peak"]["year"], by["peak"]["value"], by["peak"]["to"]) == ("France", 2005, "25.0", "5.0")
    assert (by["record_high"]["country"], by["record_high"]["value"], by["record_high"]["year"]) == ("India", "30.0", 2010)
    assert (by["record_low"]["country"], by["record_low"]["value"], by["record_low"]["year"]) == ("India", "1.0", 2000)
    assert by["surprising"]["text"] == "India is the only country that rose: from 1.0 in 2000 to 30.0 in 2010"


def test_facts_use_only_the_video_years_and_skip_short_series():
    from pipeline import captions
    series = {**SERIES, "JPN": [(1990, 999.0), (2000, 15.0)]}   # out of range + one point in range: dropped
    facts = captions.compute_facts(TOPIC, COUNTRIES, series, 2000, 2010)
    assert all(f["country"] != "Japan" for f in facts)
    assert all("999" not in json.dumps(f) for f in facts)


def test_metric_line_names_metric_countries_and_years():
    from pipeline import captions
    line = captions.metric_line(TOPIC, [n for _, n in COUNTRIES], 2000, 2010)
    assert line == "Test metric: widgets per 100 people in India, the United States, France and Japan, 2000 to 2010."


# ------------------------------------------------------------------ validation

def test_a_true_variant_passes(ctx):
    from pipeline import captions
    assert captions.validate(variant(ctx, "went_from", "India went from 1.0 to 30.0", "biggest_rise"), ctx, set()) is None
    assert captions.validate(variant(ctx, "watch_at", "Watch France at 2005", "peak"), ctx, set()) is None
    assert captions.validate(variant(ctx, "pass_in", "Nobody expected India to pass France in 2007", "crossover",
                                     "see_coming", "Did you see France coming?"), ctx, set()) is None
    assert captions.validate(variant(ctx, "other_way", "Only India went the other way", "surprising"), ctx, set()) is None
    assert captions.validate(variant(ctx, "wait_for", "Wait for the United States \U0001F440", "record_high"),
                             ctx, set()) is not None   # the US is not in the cited fact


@pytest.mark.parametrize("template_id,hook,kind,why", [
    ("went_from", "India went from 1.0 to 31.0", "biggest_rise", "31.0"),                 # invented number
    ("went_from", "India went from 1 to 30", "biggest_rise", "not in the computed facts"),  # values not as written
    ("wait_for", "Wait for India—", "surprising", "em dash"),
    ("wait_for", "Just wait for India", "surprising", "word for word"),
    ("wait_for", "Wait for Brazil", "surprising", "Brazil"),                                # not in the video
    ("wait_for", "Wait for Japan", "biggest_rise", "not in the facts it cites"),            # fact is about India
    ("really_said", "India really said down we go", "biggest_rise", "went down"),          # wrong direction
    ("really_said", "India really said lockdown", "biggest_rise", "lockdown"),             # a cause / event
    ("really_said", "India really said twice as much", "biggest_rise", "twice"),           # a number word
    ("watch_at", "Watch India at 2003", "biggest_rise", "2003"),                          # no fact has 2003
    ("watch_at", "Watch India at 2005", "crossover", "year 2005"),                        # France's year, not India's
    ("really_said", "India really said " + "so much more " * 8, "biggest_rise", "characters"),
    ("wait_for", "Wait for India #data", "surprising", "hashtag"),
])
def test_bad_variants_are_rejected(ctx, template_id, hook, kind, why):
    from pipeline import captions
    reason = captions.validate(variant(ctx, template_id, hook, kind), ctx, set())
    assert reason and why in reason


def test_questions_templates_emoji_and_reuse_are_checked(ctx):
    from pipeline import captions
    ok = variant(ctx, "wait_for", "Wait for India", "surprising")
    assert captions.validate(ok, ctx, set()) is None
    assert "already used" in captions.validate(ok, ctx, {"wait_for"})
    assert "unknown template" in captions.validate({**ok, "template_id": "nope"}, ctx, set())
    assert "end with" in captions.validate({**ok, "question": "Guess which country comes last"}, ctx, set())
    assert "word for word" in captions.validate({**ok, "question": "Which country wins?"}, ctx, set())
    assert "emoji" in captions.validate({**ok, "hook": "Wait for India \U0001F525\U0001F525"}, ctx, set())
    assert captions.validate({**ok, "hook": "Wait for India \U0001F525"}, ctx, set()) is None
    assert "fact_ids" in captions.validate({**ok, "fact_ids": []}, ctx, set())
    assert "fact_ids" in captions.validate({**ok, "fact_ids": ["F99"]}, ctx, set())


# ------------------------------------------------------------------ Gemini, retries, plain fills

def good(ctx, template_id, hook, kind):
    return variant(ctx, template_id, hook, kind)


def test_a_failed_variant_is_retried_twice_then_filled_plainly(ctx):
    from pipeline import captions
    calls = []
    first = {"variants": [good(ctx, "wait_for", "Wait for India", "surprising"),
                          good(ctx, "watch_at", "Watch France at 2005", "peak"),
                          good(ctx, "went_from", "India went from 1.0 to 99.0", "biggest_rise")]}
    bad = {"variants": [good(ctx, "went_from", "India went from 2.0 to 30.0", "biggest_rise")]}

    def ask(model, prompt):
        calls.append(prompt)
        return first if len(calls) == 1 else bad

    accepted, notes = captions.llm_variants(ctx, 3, ask, "m")
    assert len(calls) == 3 and len(accepted) == 2                      # 1 ask for 3, then 2 retries for the bad one
    assert "rejected 3x" in notes[0] and "99.0" in notes[0]
    assert "Your previous attempt was rejected" in calls[1] and "not wait_for, watch_at" in calls[1]
    fills = captions.fallback_variants(ctx, 1, {v["template_id"] for v in accepted})
    assert len(fills) == 1 and fills[0]["template_id"] not in {"wait_for", "watch_at"}


def test_a_retry_that_passes_is_kept(ctx):
    from pipeline import captions
    answers = [{"variants": [good(ctx, "wait_for", "Wait for India", "surprising")]},
               {"variants": [good(ctx, "watch_at", "Watch France at 2005", "peak")]},
               {"variants": [good(ctx, "went_from", "France went from 10.0 to 5.0", "biggest_fall")]}]
    accepted, notes = captions.llm_variants(ctx, 3, lambda m, p: answers.pop(0), "m")
    assert [v["template_id"] for v in accepted] == ["wait_for", "watch_at", "went_from"]
    assert [v["attempts"] for v in accepted] == [1, 2, 2] and notes == []


def test_generate_falls_back_when_gemini_quota_is_gone(monkeypatch):
    from pipeline import captions
    from pipeline.labels import QuotaExhausted

    def ask(model, prompt):
        raise QuotaExhausted("per day")

    c = captions.generate(TOPIC, COUNTRIES, SERIES, 2000, 2010, seed=1, ask=ask)
    assert [v["source"] for v in c["variants"]] == ["template"] * 3
    assert len({v["template_id"] for v in c["variants"]}) == 3
    assert any("quota" in n for n in c["notes"])
    for v in c["variants"]:
        hook, metric, question = v["caption"].split("\n")
        assert metric.startswith("Test metric: widgets per 100 people") and question.endswith("?")
        assert len(hook) <= 90 and "—" not in v["caption"]


def test_without_a_key_the_plain_fills_are_used():
    from pipeline import captions
    c = captions.generate(TOPIC, COUNTRIES, SERIES, 2000, 2010, seed=3)
    assert len(c["variants"]) == 3 and c["model"] is None and "no GEMINI_API_KEY" in c["notes"][0]


def test_every_shipped_template_fills_into_a_valid_caption(ctx):
    """Each hook in caption_formats.json, filled plainly from a fact it suits, passes the same checks
    Gemini's output must pass (so the fallback can never produce an untrue or malformed caption)."""
    from pipeline import captions
    checked = 0
    for h in captions.load_formats()["hooks"]:
        facts = [f for f in ctx.facts if ctx.usable(h, f)]
        if not facts:
            continue
        hook = captions._fill(h["fill"], facts[0], ctx)
        v = {"template_id": h["id"], "hook": hook, "question_id": "comes_last",
             "question": "Guess which country comes last?", "fact_ids": [facts[0]["id"]]}
        assert captions.validate(v, ctx, set()) is None, (h["id"], hook)
        checked += 1
    assert checked >= 18


def test_caption_formats_and_hashtag_pools_are_clean():
    from pipeline import captions
    f = captions.load_formats()
    assert 15 <= len(f["hooks"]) <= 20 and len({h["id"] for h in f["hooks"]}) == len(f["hooks"])
    for t in [h["text"] for h in f["hooks"]] + [q["text"] for q in f["questions"]]:
        assert not any(d in t for d in captions.DASHES)
    assert all(q["text"].endswith("?") for q in f["questions"])
    pools = captions.load_hashtags()
    banned = set(pools["banned"])
    assert {"fyp", "viral", "explore", "reels"} <= banned
    tags = pools["broad"] + pools["niche"] + pools["pinned"] + [t for v in pools["categories"].values() for t in v] + \
        [t for v in pools["by_topic"].values() for t in v] + list(pools["country_tags"].values())
    assert all(captions.tag_ok(t, banned) for t in tags), [t for t in tags if not captions.tag_ok(t, banned)]
    from pipeline.topics import load_topics
    assert {t["id"] for t in load_topics()} <= set(pools["by_topic"])


# ------------------------------------------------------------------ hashtags

def test_default_hashtags_are_1_broad_2_niche_1_topic_1_country(ctx):
    from pipeline import captions
    pools = captions.load_hashtags()
    tags, notes = captions.pick_hashtags({**TOPIC, "id": "nuclear_share"}, ctx.facts, seed=5)
    plain = [t.lstrip("#") for t in tags]
    assert len(tags) == 5 and len(set(tags)) == 5 and all(t.startswith("#") for t in tags) and notes == []
    assert plain[0] in pools["broad"] and plain[1] == "sonification" and plain[2] in pools["niche"]
    assert plain[3] in pools["by_topic"]["nuclear_share"] and plain[4] == "india"   # the most surprising country


def test_hashtag_count_is_configurable_and_banned_tags_never_appear(ctx):
    from pipeline import captions
    pools = {"broad": ["fyp", "data", "charts"], "pinned": [], "niche": ["viralreels", "infographic", "explorepage", "dataviz2"],
             "categories": {"tech": ["reels", "technology", "tech"]}, "by_topic": {}, "country_tags": {"United States": "usa"},
             "banned": ["reels"]}
    s = captions.settings()
    for count in (3, 8):
        s["hashtags"] = {"count": count, "broad": 1, "niche": 2}
        tags, _ = captions.pick_hashtags(TOPIC, ctx.facts, seed=0, s=s, pools=pools)
        plain = [t.lstrip("#") for t in tags]
        assert len(plain) == len(set(plain)) == min(count, 8)
        assert not {"fyp", "viralreels", "explorepage", "reels"} & set(plain)


# ------------------------------------------------------------------ trending check (opt-in)

class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


def test_trending_reranks_within_the_weekly_budget_and_caches(tmp_path, monkeypatch, pinned_config):
    from pipeline import captions
    pinned_config["paths"]["cache"] = str(tmp_path / "cache")
    monkeypatch.setenv("IG_USER_ID", "7")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    fresh = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    old = (now - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    media = {"id_a": [{"timestamp": old, "like_count": 5}] * 10,
             "id_b": [{"timestamp": fresh, "like_count": 50}] * 6 + [{"timestamp": old}] * 4,
             "id_c": [{"timestamp": fresh, "like_count": 9}] * 9}
    asked = []

    def get(url, params=None, timeout=None):
        assert params["access_token"] == "tok"
        asked.append(url.rsplit("/", 1)[-1])
        if url.endswith("ig_hashtag_search"):
            return Response({"data": [{"id": "id_" + params["q"]}]})
        return Response({"data": media[url.rsplit("/", 2)[-2]]})

    tr = {"weekly_search_budget": 2, "cache_days": 7, "recent_days": 30}
    order, notes = captions.trending_rank(["a", "b", "c"], tr, get=get, now=now)
    assert order == ["b", "c", "a"] or order == ["b", "a", "c"]
    assert order[0] == "b" and any("budget (2) used, #c" in n for n in notes)   # only 2 unique tags searched
    asked.clear()
    order, notes = captions.trending_rank(["a", "b"], tr, get=get, now=now + timedelta(days=1))
    assert asked == [] and order == ["b", "a"]                                        # cached, no new searches
    _, notes = captions.trending_rank(["c"], tr, get=get, now=now + timedelta(days=8))
    assert asked and "top_media" in asked                                            # a week later: budget is free again


# ------------------------------------------------------------------ files, choice, posting

def get_owner_flag():
    import yaml
    from pipeline.config import ROOT
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))["captions"]["use_for_posting"]


def make_run(db, run_dir, topic="homicide_rate"):
    rid = db.create_run(topic)
    d = run_dir(rid)
    (d / "data.json").write_text(json.dumps({"topic_id": topic, "series": {k: [list(r) for r in v] for k, v in SERIES.items()},
                                             "citation": "UNODC (2025)"}))
    (d / "pick.json").write_text(json.dumps({"countries": ["IND", "USA", "FRA", "JPN"], "x_start": 2000, "x_end": 2010}))
    (d / "meta.json").write_text(json.dumps({"title": "Murder rates, played as music", "duration_seconds": 41.0,
                                             "description_instagram": "Old caption.\n\n#dataviz"}))
    (d / "video.mp4").write_bytes(b"\0" * 64)
    return rid


@pytest.fixture
def files(temp_db, tmp_path, pinned_config):
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"))
    from pipeline.config import run_dir
    return temp_db, run_dir


def test_generate_for_run_writes_captions_and_keeps_the_choice(files):
    from pipeline import captions
    db, run_dir = files
    rid = make_run(db, run_dir)
    c = captions.generate_for_run(rid, llm=False)
    assert (run_dir(rid) / "captions.json").exists() and len(c["variants"]) == 3 and c["chosen"] == 0
    assert c["credit"] == ["Data: Our World in Data (CC BY 4.0).", "Original sources: UNODC (2025)."]
    captions.choose(rid, 2)
    assert captions.generate_for_run(rid, llm=False)["chosen"] == 2
    with pytest.raises(ValueError):
        captions.choose(rid, 7)


def test_posting_keeps_the_old_caption_unless_use_for_posting_is_on(files, pinned_config):
    from pipeline import captions
    db, run_dir = files
    rid = make_run(db, run_dir)
    meta = json.loads((run_dir(rid) / "meta.json").read_text())
    assert get_owner_flag() is True                      # the owner's config posts the chosen caption
    pinned_config["captions"]["use_for_posting"] = False
    assert captions.apply_to_meta(rid, meta) is meta and not (run_dir(rid) / "captions.json").exists()
    pinned_config["captions"]["use_for_posting"] = True
    c = captions.generate_for_run(rid, llm=False)
    captions.choose(rid, 1)
    text = captions.apply_to_meta(rid, meta)["description_instagram"]
    hook, metric, question = text.split("\n\n")[0].split("\n")
    assert hook == c["variants"][1]["hook"] and question.endswith("?") and metric == c["metric_line"]
    assert "Data: Our World in Data (CC BY 4.0)." in text and text.endswith(" ".join(c["hashtags"]))
    assert "—" not in text and len(text) <= 2200


def test_the_publisher_posts_the_chosen_caption_only_when_switched_on(files, pinned_config, monkeypatch):
    from pipeline import accounts, captions, orchestrator, posted, publish_instagram
    db, run_dir = files
    pinned_config["dry_run"].update(instagram=False)
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram"])
    monkeypatch.setattr(posted, "earlier_attempt_live", lambda *a: None)
    seen = []
    monkeypatch.setattr(publish_instagram, "publish", lambda rid, meta, video: seen.append(meta["description_instagram"]) or "live")
    for on in (False, True):
        pinned_config["captions"]["use_for_posting"] = on
        rid = make_run(db, run_dir)
        for st in ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]:
            db.transition(rid, st)
        db.transition(rid, "approved", platforms=json.dumps(["instagram"]), approved_by="test")
        captions.generate_for_run(rid, llm=False)
        assert orchestrator._publish_run(rid, None) is True
    assert seen[0] == "Old caption.\n\n#dataviz"
    assert seen[1] == captions.compose(captions.load(rid)) and seen[1] != seen[0]


# ------------------------------------------------------------------ the Telegram review card

class FakeTelegram:
    def __init__(self):
        self.calls, self.next_id = [], 100

    def __call__(self, method, http_timeout=30, files=None, **kw):
        self.calls.append((method, kw))
        if method in ("sendVideo", "sendMessage"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def of(self, method):
        return [kw for m, kw in self.calls if m == method]


@pytest.fixture
def bot(files, monkeypatch):
    from pipeline import accounts, actions, approve_telegram as tg
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    fake = FakeTelegram()
    monkeypatch.setattr(tg, "call", fake)
    monkeypatch.setattr(actions, "spawn", lambda *a: 0)
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram"])
    clock = [1000.0]
    b = tg.Bot(clock=lambda: clock[0])
    return {"bot": b, "fake": fake, "db": files[0], "run_dir": files[1], "clock": clock}


def waiting(e):
    rid = make_run(e["db"], e["run_dir"])
    for st in ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]:
        e["db"].transition(rid, st, **({"countries": ["IND", "USA", "FRA", "JPN"]} if st == "picked" else {}))
    return rid


def press(e, data, mid):
    e["bot"].on_callback({"id": "cb", "data": data, "from": {"id": 555},
                          "message": {"message_id": mid, "chat": {"id": 555, "type": "private"}}})
    return e["fake"].of("answerCallbackQuery")[-1]["text"]


def test_the_card_offers_the_caption_options_and_a_choice(bot, pinned_config):
    from pipeline import captions, review
    pinned_config["captions"]["use_for_posting"] = False   # the card then says it is only a preview
    rid = waiting(bot)
    bot["bot"].tick()                                       # no captions.json yet: plain fills are made on the spot
    sent = bot["fake"].of("sendVideo")[0]
    c = captions.load(rid)
    assert c and len(c["variants"]) == 3
    text, rows = sent["caption"], sent["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["✅ Approve", "❌ Reject", "⏭ Later"]
    assert [b["text"] for b in rows[1]] == ["✅ Caption 1", "Caption 2", "Caption 3"]
    assert all(v["hook"] in text and v["question"] in text for v in c["variants"])
    assert c["metric_line"] in text and " ".join(c["hashtags"]) in text and "Preview only" in text
    assert len(text) <= 1024
    card = review.current_card()
    assert press(bot, f"c:v:{card['id']}:1", card["message_id"]) == "Caption 2 chosen."
    assert captions.load(rid)["chosen"] == 1
    bot["clock"][0] += 5                                    # past the per-message edit throttle
    bot["bot"].editor.flush()
    edit = bot["fake"].of("editMessageCaption")[-1]
    assert [b["text"] for b in edit["reply_markup"]["inline_keyboard"][1]] == ["Caption 1", "✅ Caption 2", "Caption 3"]
    assert press(bot, f"c:v:{card['id']}:9", card["message_id"]) == "No such caption option."
    assert press(bot, f"c:a:{card['id']}", card["message_id"]) == "Approved — posting now."
    after = review.card_view(review.get_card(card["id"]))[0]
    assert "✍️ Post caption 2:" in after and c["variants"][1]["hook"] in after and c["variants"][0]["hook"] not in after
    assert press(bot, f"c:v:{card['id']}:0", card["message_id"]) == "This card is stale"
    assert captions.load(rid)["chosen"] == 1


def test_a_card_without_data_still_goes_out(bot):
    from pipeline import review
    rid = waiting(bot)
    (bot["run_dir"](rid) / "pick.json").unlink()
    bot["bot"].tick()
    sent = bot["fake"].of("sendVideo")[0]
    assert len(sent["reply_markup"]["inline_keyboard"]) == 1 and "Post caption" not in sent["caption"]
    assert review.current_card()["run_id"] == rid


def test_the_video_opening_question_and_its_answer_come_from_the_facts(ctx):
    """The question the video opens with names no number; the end card's answer is filled from a fact in code."""
    from pipeline import captions
    seen = set()
    for seed in range(4):
        o = captions.opening(ctx.facts, seed)
        seen.add(o["id"])
        assert o["question"].endswith("?") and not captions.numbers_in(o["question"])
        assert o["fact"]["country"] in o["answer"] and o["highlight"][0] == o["fact"]["country"]
        assert all(n in ctx.allowed for n in captions.numbers_in(o["answer"]))
    assert seen == {"overtakes", "ends_last"}                         # the two strongest openers, varied by seed
    assert captions.opening(ctx.facts, 0)["answer"] == "India overtakes France in 2007"
    assert captions.opening(ctx.facts, 1)["answer"] == "France ends up last: 5.0 in 2010"
