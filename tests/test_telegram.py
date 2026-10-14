"""The button-driven Telegram bot: the pinned home card, New reel, Make a batch, the one-at-a-time review
card, notes and errors. The Bot API is faked (no network); topic scores are pinned."""
import json
import random
import re

import pytest

OWNER = 555
FLOW = ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]
RUN_ID = re.compile(r"r\d{8}-\d{6}-[0-9a-f]{4}")
YEARS = list(range(2000, 2011))
SERIES = {"IND": [[y, 1 + 3 * i] for i, y in enumerate(YEARS)],
          "USA": [[y, 20 - i * 0.2] for i, y in enumerate(YEARS)],
          "FRA": [[y, v] for y, v in zip(YEARS, [10, 13, 16, 19, 22, 25, 20, 15, 10, 7, 5])],
          "JPN": [[y, 15 - i * 0.3] for i, y in enumerate(YEARS)]}


class FakeTelegram:
    """Records every Bot API call; sendVideo / sendMessage return fresh message ids."""

    def __init__(self):
        self.calls, self.next_id, self.fail, self.updates = [], 100, {}, []

    def __call__(self, method, http_timeout=30, files=None, **kw):
        self.calls.append((method, kw))
        if self.fail.get(method):
            raise self.fail[method].pop(0)
        if method in ("sendVideo", "sendMessage"):
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "getUpdates":
            return [u for u in self.updates if u["update_id"] >= kw.get("offset", 0)]
        if method == "getMe":
            return {"username": "test_bot"}
        return True

    def of(self, method):
        return [kw for m, kw in self.calls if m == method]

    def answers(self):
        return [kw["text"] for kw in self.of("answerCallbackQuery")]

    def sent(self):
        return [m for m, _ in self.calls if m in ("sendMessage", "sendVideo")]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


SCORES = {"inflation": 95, "unemployment": 90, "coal_share": 88, "renewables_share": 87, "solar_share": 86,
          "hydro_share": 85, "internet_users": 84, "broadband": 83, "mobile_subscriptions": 82, "co2_per_capita": 81,
          "cereal_yield": 80, "child_mortality": 79, "health_spending": 78, "net_migration": 77,
          "population_growth": 76, "gdp_growth": 75}


@pytest.fixture
def env(temp_db, tmp_path, monkeypatch, pinned_config):
    from pipeline import accounts, actions, approve_telegram as tg, review
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"),
                                  tokens=str(tmp_path / "tokens"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(OWNER))
    fake = FakeTelegram()
    monkeypatch.setattr(tg, "call", fake)
    spawned = []
    monkeypatch.setattr(actions, "spawn", lambda *a: spawned.append(a) or 0)
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram", "facebook"])
    monkeypatch.setattr(review, "topic_score", lambda t: SCORES.get(t["id"], 0.0))
    clock = Clock()
    bot = tg.Bot(clock=clock, rng=random.Random(3))
    return {"tg": tg, "db": temp_db, "fake": fake, "spawned": spawned, "bot": bot, "clock": clock}


def waiting(db, topic="inflation", question="Who overtakes who?"):
    """A reel waiting for review, with its video, metadata and caption options."""
    from pipeline import captions
    from pipeline.config import run_dir
    rid = db.create_run(topic)
    for st in FLOW:
        db.transition(rid, st, **({"countries": ["IND", "USA", "FRA", "JPN"]} if st == "picked" else {}))
    d = run_dir(rid)
    (d / "video.mp4").write_bytes(b"\0" * 64)
    (d / "meta.json").write_text(json.dumps({"title": f"Video about {topic}", "duration_seconds": 22.4,
                                             "opening": {"question": question}}))
    (d / "data.json").write_text(json.dumps({"topic_id": topic, "series": SERIES}))
    (d / "pick.json").write_text(json.dumps({"countries": ["IND", "USA", "FRA", "JPN"], "x_start": 2000, "x_end": 2010}))
    captions.generate_for_run(rid, llm=False)
    return rid


def say(e, text, chat=OWNER, user=OWNER, reply_to=None):
    e["fake"].next_id += 1
    m = {"message_id": e["fake"].next_id, "chat": {"id": chat, "type": "private"}, "from": {"id": user}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    e["bot"].on_message(m)
    return m["message_id"]


def press(e, data, message_id, chat=OWNER, user=OWNER):
    before = len(e["fake"].of("answerCallbackQuery"))
    e["bot"].on_callback({"id": "cb", "data": data, "from": {"id": user},
                          "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}}})
    answers = e["fake"].answers()
    return answers[-1] if len(answers) > before else None


def menu(e):
    from pipeline import home
    say(e, "/menu")
    return home.home_msg()


def home_text(e):
    """What the home card shows now (its last edit or the message that sent it)."""
    from pipeline import home
    mid = home.home_msg()
    texts = [kw["text"] for m, kw in e["fake"].calls if m == "editMessageText" and kw["message_id"] == mid]
    return texts[-1] if texts else [kw["text"] for kw in e["fake"].of("sendMessage")
                                    if kw.get("reply_markup", {}).get("inline_keyboard") and "🏠" in kw["text"]][-1]


def buttons(markup):
    return [b["text"] for row in markup["inline_keyboard"] for b in row]


def tick(e, seconds=5):
    e["clock"].t += seconds
    e["bot"].tick()


# ------------------------------------------------------------------ the menu and the home card

def test_menu_is_the_only_command_and_start_opens_the_pinned_home_card(env):
    from pipeline import home
    tg, fake = env["tg"], env["fake"]
    tg.register_commands()
    assert [c["command"] for c in fake.of("setMyCommands")[0]["commands"]] == ["menu"]
    typed = say(env, "/start")
    first = home.home_msg()
    assert fake.of("pinChatMessage")[-1]["message_id"] == first
    assert {"chat_id": str(OWNER), "message_id": typed} in fake.of("deleteMessage")      # the /start itself goes
    card = fake.of("sendMessage")[-1]
    assert card["text"].startswith("🏠 Graphony") and buttons(card["reply_markup"]) == [
        "🆕 New reel", "📦 Make a batch", "👀 Review ready reels (0)", "📅 Posting queue (0)", "📊 Stats", "👤 Account"]
    say(env, "/menu")                                         # a new /menu moves the one home card down
    assert home.home_msg() != first and {"chat_id": str(OWNER), "message_id": first} in fake.of("deleteMessage")
    assert fake.of("pinChatMessage")[-1]["message_id"] == home.home_msg()
    say(env, "/new")                                          # an old command just brings the menu
    assert len([k for k in fake.of("sendMessage") if k["text"].startswith("🏠")]) == 3


def test_the_first_bot_start_sends_the_home_card_and_a_restart_only_edits_it(env):
    from pipeline import home
    tg, fake = env["tg"], env["fake"]
    tg.start(env["bot"])
    assert len(fake.sent()) == 1 and home.home_msg() and fake.of("setMyCommands")
    waiting(env["db"])
    restarted = tg.Bot(clock=env["clock"])
    tg.start(restarted)
    assert len(fake.sent()) == 1                                # no second home card
    assert "Ready for your review: 1" in fake.of("editMessageText")[-1]["text"]


def test_home_card_says_what_is_happening_in_plain_words(env):
    """Making now with its step, waiting to make, ready for review, approved, last posted, and a problem."""
    from pipeline import home, makequeue
    db = env["db"]
    for topic in ("unemployment", "coal_share"):
        waiting(db, topic)
    approved = waiting(db, "broadband")
    db.transition(approved, "approved")
    posted = waiting(db, "internet_users")
    db.transition(posted, "approved")
    db.transition(posted, "publishing")
    db.upsert_post(posted, "instagram", "live", remote_id="m1", url="https://instagram.com/reel/x")
    db.transition(posted, "published")
    post = db.get_posts(posted)[0]
    db.add_stats(post["id"], views=1234)
    making = makequeue.add_job("inflation")
    for tid in ("solar_share", "hydro_share"):
        makequeue.add_job(tid)
    rid = makequeue.claim(making)
    lines = home.home_view()[0].splitlines()
    assert "🛠 Making now: Inflation (step 1 of 4: getting data)" in lines
    db.transition(rid, "data_ready")
    db.set_progress(rid, 0.47)
    text, markup = home.home_view()
    assert "🛠 Making now: Inflation (step 2 of 4: rendering 40%)" in text
    assert "⏳ Waiting to make: 2 (Solar power, Hydropower)" in text
    assert "👀 Ready for your review: 2" in text and "📅 Approved, waiting to post: 1" in text
    assert f"📣 Last posted: Internet access, {home.when(post['created_at'])}, 1,234 views" in text
    assert "👀 Review ready reels (2)" in buttons(markup)
    for st in ("picked", "labelled", "rendered"):
        db.transition(rid, st)
    assert "(step 3 of 4: writing captions)" in home.home_view()[0]
    db.fail(rid, "render", f"RuntimeError: timeline is 26.1s for {rid}, outside 20-25s; re-pick")
    text, markup = home.home_view()
    assert "⚠️ Rendering failed for Inflation: timeline is 26.1s for this video, outside 20-25s; re-pick." in text
    assert buttons(markup)[:2] == ["🔁 Retry Inflation", "🗑 Drop it"]
    assert not RUN_ID.search(text + json.dumps(markup)) and "RuntimeError" not in text and "Traceback" not in text
    assert "🛠 Making now: nothing" in text


def test_retry_and_drop_on_a_problem(env):
    from pipeline import home, makequeue
    db = env["db"]
    hm = menu(env)
    job = makequeue.add_job("inflation")
    rid = makequeue.claim(job)
    db.fail(rid, "fetch", "ConnectionError: network unreachable")
    ref = db.get_run(rid)["ref"]
    assert "Rendering failed for Inflation: The internet connection was interrupted." in home.home_view()[0]
    assert press(env, f"h:retry:{ref}", hm) == "Trying again."
    assert [j["topic_id"] for j in makequeue.jobs("queued")] == ["inflation"] and not home.problems()
    rid2 = makequeue.claim(makequeue.jobs("queued")[0])
    db.fail(rid2, "render", "ffmpeg: broken pipe")
    assert press(env, f"h:drop:{db.get_run(rid2)['ref']}", hm) == "Dropped."
    assert not home.problems() and press(env, f"h:drop:{db.get_run(rid2)['ref']}", hm) == "That problem is already gone."


# ------------------------------------------------------------------ New reel

def test_new_reel_offers_five_unmade_ideas_from_mixed_categories(env):
    from pipeline import home, ideas, makequeue
    db, fake = env["db"], env["fake"]
    waiting(db, "inflation")                                   # already made: never suggested again
    hm = menu(env)
    assert press(env, "h:new", hm) == "Topic ideas"
    edit = fake.of("editMessageText")[-1]
    picks = [b["callback_data"][7:] for row in edit["reply_markup"]["inline_keyboard"] for b in row
             if b["callback_data"].startswith("h:pick:")]
    assert len(picks) == 5 and "inflation" not in picks
    cats = [ideas.pool()[[t["id"] for t in ideas.pool()].index(p)]["category"] for p in picks]
    assert len(set(cats)) == 5                                  # five categories in five ideas
    assert buttons(edit["reply_markup"])[-4:] == ["🔄 More ideas", "🎲 Surprise me", "⌨️ Type my own topic",
                                                  "🏠 Back to Home"]
    press(env, "h:more:1", hm)
    more = [b["callback_data"][7:] for row in fake.of("editMessageText")[-1]["reply_markup"]["inline_keyboard"]
            for b in row if b["callback_data"].startswith("h:pick:")]
    assert not set(more) & set(picks)
    assert press(env, f"h:pick:{picks[0]}", hm).startswith("Added: ")
    assert [j["topic_id"] for j in makequeue.jobs("queued")] == [picks[0]]
    assert home.screen()["name"] == "home" and "✅ Added to the make queue" in home_text(env)
    assert press(env, f"h:pick:{picks[0]}", hm) == "That topic already has a reel."
    tick(env)
    assert f"Making now: {home.review.topic_name(picks[0])} (step 1 of 4: getting data)" in home_text(env)
    assert env["spawned"][-1][:2] == ("produce", "--run")


def test_surprise_me_and_typing_my_own_topic(env):
    from pipeline import home, makequeue
    fake = env["fake"]
    hm = menu(env)
    assert press(env, "h:surprise", hm).startswith("Added: ")
    assert makequeue.jobs("queued")[0]["source"] == "surprise"
    assert press(env, "h:type", hm) == "Reply with your topic."
    prompt = fake.of("sendMessage")[-1]
    assert prompt["reply_markup"]["force_reply"] is True and home.awaiting()["kind"] == "topic"
    reply = say(env, "coal power please", reply_to=fake.next_id)
    assert makequeue.jobs("queued")[-1]["topic_id"] == "coal_share" and home.awaiting() is None
    deleted = [d["message_id"] for d in fake.of("deleteMessage")]
    assert fake.next_id - 1 in deleted and reply in deleted     # the question and the answer are tidied away
    press(env, "h:type", hm)
    say(env, "zebra crossings")
    assert home.screen()["name"] == "typed" and "I don't have a dataset for that" in home_text(env)
    press(env, "h:type", hm)
    say(env, "electricity")                                     # several matches: tap the one meant
    shown = fake.of("editMessageText")[-1]
    assert "Tap the one you meant" in shown["text"] and buttons(shown["reply_markup"])[:2] == ["Solar power · energy", "Hydropower · energy"]


# ------------------------------------------------------------------ Make a batch

@pytest.mark.parametrize("seed", range(6))
def test_batch_topics_are_distinct_varied_and_deduplicated(env, seed):
    from pipeline import ideas, makequeue
    from pipeline.topics import get_topic
    db = env["db"]
    for made in ("inflation", "coal_share"):
        waiting(db, made)
    makequeue.add_job("unemployment")                           # queued counts as made
    batch = ideas.pick_batch(7, random.Random(seed))
    ids = [t["id"] for t in batch]
    assert len(ids) == 7 and len(set(ids)) == 7
    assert not {"inflation", "coal_share", "unemployment"} & set(ids)
    for a, b in zip(batch, batch[1:]):
        assert ideas.dataset_key(a) != ideas.dataset_key(b)     # never the same dataset back to back
        assert a["category"] != b["category"]
    assert len({t["category"] for t in batch}) >= 5
    assert ideas.dataset_key(get_topic("coal_share")) == ideas.dataset_key(get_topic("solar_share")) == "owid:energy-data"
    assert ideas.dataset_key(get_topic("internet_users")) == ideas.dataset_key(get_topic("broadband")) == "wb:IT.NET"


def test_make_a_batch_shows_the_topics_first_then_starts_them(env):
    from pipeline import home, makequeue
    fake = env["fake"]
    hm = menu(env)
    press(env, "h:batch", hm)
    assert buttons(fake.of("editMessageText")[-1]["reply_markup"]) == ["3 reels", "5 reels", "7 reels", "🏠 Home"]
    press(env, "h:bn:7", hm)
    first = makequeue.pending_batch()
    shown = fake.of("editMessageText")[-1]
    assert len(first) == 7 and shown["text"].startswith("📦 Batch of 7, not started yet:")
    assert buttons(shown["reply_markup"]) == ["▶️ Start making these 7", "🔀 Shuffle: pick others", "✖️ Cancel", "🏠 Home"]
    assert makequeue.jobs("queued") == [] and env["spawned"] == []          # nothing starts before Start
    press(env, "h:bshuffle", hm)
    second = makequeue.pending_batch()
    assert len(second) == 7 and not set(first) & set(second)
    press(env, "h:bcancel", hm)
    assert makequeue.pending_batch() == [] and home.screen()["name"] == "home"
    press(env, "h:bn:3", hm)
    chosen = makequeue.pending_batch()
    assert press(env, "h:bgo", hm) == "Started."
    jobs = makequeue.jobs("queued")
    assert [j["topic_id"] for j in jobs] == chosen and len({j["batch_id"] for j in jobs}) == 1
    tick(env)
    assert len(env["spawned"]) == 1                               # one at a time
    assert "📦 Batch: 0 of 3 ready" in home_text(env) and "⏳ Waiting to make: 2" in home_text(env)


def test_a_skipped_batch_topic_is_replaced_and_a_failed_one_is_shown(env):
    from pipeline import home, ideas, makequeue
    from pipeline.topics import get_topic
    db = env["db"]
    bid = makequeue.start_batch(["inflation", "coal_share", "internet_users"])
    tick(env)
    first = makequeue.jobs("running")[0]
    db.transition(first["run_id"], "data_ready")
    db.skip(first["run_id"], "low variety: no falling country (net < -0.5)")
    tick(env)
    batch = makequeue.batch_jobs(bid)
    assert [j["source"] for j in batch] == ["batch", "batch", "batch", "replacement"]
    new = get_topic(batch[-1]["topic_id"])
    assert new["id"] not in ("inflation", "coal_share", "internet_users")
    assert ideas.dataset_key(new) != "wb:IT.NET" and new["category"] != "tech"   # it follows internet users
    assert new["category"] not in ("energy", "tech")              # a category the batch does not have yet
    assert not [p for p in home.problems() if p["run"]["id"] == first["run_id"]]   # replaced, not a problem
    second = makequeue.jobs("running")[0]
    db.fail(second["run_id"], "render", "ffmpeg: broken pipe")
    tick(env)
    assert len(makequeue.batch_jobs(bid)) == 4                    # a failure is not replaced ...
    assert "⚠️ Rendering failed for Coal power" in home.home_view()[0]   # ... it waits on the home card


# ------------------------------------------------------------------ making, one at a time

def test_rendering_is_one_at_a_time_with_progress_only_on_the_home_card_and_one_ready_note(env):
    from pipeline import makequeue
    db, fake = env["db"], env["fake"]
    hm = menu(env)
    for tid in ("inflation", "coal_share", "internet_users"):
        makequeue.add_job(tid)
    tick(env)
    assert len(env["spawned"]) == 1
    job = makequeue.jobs("running")[0]
    sent_before = len(fake.sent())
    for st in FLOW[:3]:
        db.transition(job["run_id"], st)
    db.set_progress(job["run_id"], 0.61)
    tick(env)
    assert "rendering 60%" in home_text(env) and len(fake.sent()) == sent_before   # progress: edits only
    assert all(e["message_id"] == hm for e in fake.of("editMessageText"))
    db.transition(job["run_id"], "rendered")
    db.transition(job["run_id"], "awaiting_approval")
    tick(env)
    notes = [kw for kw in fake.of("sendMessage") if kw["text"].startswith("🔔")]
    assert [n["text"] for n in notes] == ["🔔 Reel ready: Inflation. Tap Review."]
    assert buttons(notes[0]["reply_markup"]) == ["👀 Review it now"]
    assert len(env["spawned"]) == 2                               # the next one starts
    job2 = makequeue.jobs("running")[0]
    for st in FLOW:
        db.transition(job2["run_id"], st)
    tick(env)
    assert len([kw for kw in fake.of("sendMessage") if kw["text"].startswith("🔔")]) == 1   # the first is unread
    assert "Ready for your review: 2" in home_text(env)
    note_id = int(env["db"].kv_get("home:ready_note"))
    assert press(env, "r:open", note_id) == "2 reels to review."
    assert {"chat_id": str(OWNER), "message_id": note_id} in fake.of("deleteMessage") and not env["db"].kv_get("home:ready_note")


# ------------------------------------------------------------------ the review card

def test_review_shows_one_reel_at_a_time_and_the_same_card_moves_on(env):
    from pipeline import captions, review
    db, fake = env["db"], env["fake"]
    a, b, c = waiting(db, "inflation"), waiting(db, "coal_share", "Which country ends last?"), waiting(db, "broadband")
    hm = menu(env)
    assert press(env, "h:review", hm) == "3 reels to review."
    assert len(fake.of("sendVideo")) == 1
    card = fake.of("sendVideo")[0]
    text = card["caption"]
    assert text.startswith("🎬 Reel 1 of 3 for review: Inflation") and "❓ Hook question: Who overtakes who?" in text
    opts = captions.load(a)
    assert all(v["hook"] in text and v["question"] in text for v in opts["variants"])
    assert " ".join(opts["hashtags"]) in text and len(text) <= 1024 and not RUN_ID.search(text)
    assert buttons(card["reply_markup"]) == ["✔️ Caption 1", "Use caption 2", "Use caption 3", "✏️ Edit caption",
                                             "✅ Approve: add to posting queue", "❌ Reject: don't post", "⏭ Skip for now",
                                             "🏠 Home"]
    mid = review.card_state()["mid"]
    ref = lambda rid: db.get_run(rid)["ref"]  # noqa: E731
    assert press(env, f"v:ok:{ref(a)}", mid).startswith("Approved: it posts ")
    assert db.get_run(a)["stage"] == "approved" and env["spawned"] == []     # it waits in the posting queue
    media = fake.of("editMessageMedia")[-1]
    assert media["message_id"] == mid and media["media"]["caption"].startswith("🎬 Reel 1 of 2 for review: Coal power")
    assert len(fake.of("sendVideo")) == 1                          # the same card, no new message
    assert press(env, f"v:ok:{ref(a)}", mid) == review.STALE       # the reel already moved on
    assert press(env, f"v:skip:{ref(b)}", mid) == "Skipped for now: it stays in Ready for review."
    assert fake.of("editMessageMedia")[-1]["media"]["caption"].startswith("🎬 Reel 1 of 1 for review: Home broadband")
    answer = press(env, f"v:no:{ref(c)}", mid)
    assert answer == "Rejected: it won't be posted. 1 skipped reel stays in Ready for review."
    assert db.get_run(c)["stage"] == "rejected" and db.get_run(b)["stage"] == "awaiting_approval"
    assert review.card_state() is None and {"chat_id": str(OWNER), "message_id": mid} in fake.of("deleteMessage")
    assert press(env, f"v:ok:{ref(b)}", mid) == review.STALE       # a closed card does nothing
    assert len(fake.of("sendVideo")) == 1 and not [k for k in fake.of("sendMessage") if k["text"].startswith("🔔")]


def test_home_on_the_review_card_closes_it_and_reopening_starts_at_the_front(env):
    from pipeline import review
    db = env["db"]
    a, b = waiting(db, "inflation"), waiting(db, "coal_share")
    hm = menu(env)
    press(env, "h:review", hm)
    mid = review.card_state()["mid"]
    press(env, f"v:skip:{db.get_run(a)['ref']}", mid)
    assert press(env, "v:home", mid) == "Back to Home."
    assert review.card_state() is None
    press(env, "h:review", hm)
    assert review.card_state()["run"] == b                          # a skipped reel went to the back


def test_caption_options_and_edit_caption_on_the_review_card(env):
    from pipeline import captions, home, review
    db, fake = env["db"], env["fake"]
    rid = waiting(db, "inflation")
    hm = menu(env)
    press(env, "h:review", hm)
    mid, ref = review.card_state()["mid"], db.get_run(rid)["ref"]
    assert press(env, f"v:cap:{ref}:1", mid) == "Caption 2 chosen."
    edit = fake.of("editMessageCaption")[-1]
    assert edit["message_id"] == mid and buttons(edit["reply_markup"])[:3] == ["Use caption 1", "✔️ Caption 2", "Use caption 3"]
    assert press(env, f"v:edit:{ref}", mid) == "Reply with the new caption."
    prompt_id = fake.next_id
    assert fake.of("sendMessage")[-1]["reply_markup"]["force_reply"] is True
    bad = say(env, "India rises — fast", reply_to=prompt_id)
    refused = fake.of("sendMessage")[-1]
    assert refused["text"].startswith("❌ Not saved: Remove the em dash") and "edit" not in captions.load(rid)
    say(env, "India went from 1.0 to 31.0 by 2010.\nWho saw 1999 coming?\n#india #inflation", reply_to=fake.next_id)
    c = captions.load(rid)
    assert c["edit"]["text"] == "India went from 1.0 to 31.0 by 2010.\nWho saw 1999 coming?"
    assert c["edit"]["hashtags"] == ["#india", "#inflation"] and home.awaiting() is None
    deleted = {d["message_id"] for d in fake.of("deleteMessage")}
    assert {prompt_id, bad, fake.next_id} <= deleted                 # the prompts and replies are tidied away
    shown = fake.of("editMessageCaption")[-1]
    assert shown["message_id"] == mid and "✍️ Caption: your own text" in shown["caption"]
    assert "⚠️ Check 1999" in shown["caption"] and "#india #inflation" in shown["caption"]
    assert len(fake.of("sendVideo")) == 1                            # the same card was updated
    assert captions.compose(c).endswith("Data: World Bank WDI (CC BY 4.0).\n\n#india #inflation")


def test_the_dashboard_moves_the_card_on_and_can_send_a_reel_to_it(env):
    from pipeline import actions, health, review
    db, fake = env["db"], env["fake"]
    a, b, c = waiting(db, "inflation"), waiting(db, "coal_share"), waiting(db, "broadband")
    hm = menu(env)
    press(env, "h:review", hm)
    actions.reject(a, "dashboard")
    tick(env)
    assert review.card_state()["run"] == b and len(fake.of("editMessageMedia")) == 1
    health.beat("bot")
    assert actions.send_to_telegram(c, "dashboard") == "Sent to Telegram — approve it there."
    assert review.telegram_state(c) == "next" and env["spawned"] == []
    tick(env)
    assert review.card_state()["run"] == c and review.telegram_state(c) == "card"
    assert len(fake.of("sendVideo")) == 1                            # still the one card


# ------------------------------------------------------------------ after posting

def test_one_short_message_with_the_link_after_a_reel_posts(env):
    from pipeline import home, review
    db, fake = env["db"], env["fake"]
    rid = waiting(db, "inflation")
    hm = menu(env)
    tick(env)                                                         # first tick: earlier posts are known
    press(env, "h:review", hm)
    press(env, f"v:ok:{db.get_run(rid)['ref']}", review.card_state()["mid"])
    db.transition(rid, "publishing")
    tick(env)
    assert "📤 Posting now: Inflation" in home_text(env)
    db.upsert_post(rid, "instagram", "live", url="https://www.instagram.com/reel/abc/")
    db.upsert_post(rid, "facebook", "failed", message="ConnectionError: network down")
    db.transition(rid, "published")
    tick(env)
    tick(env)
    posted = [k["text"] for k in fake.of("sendMessage") if k["text"].startswith("✅ Posted")]
    assert posted == ["✅ Posted: Inflation\n📸 Instagram: https://www.instagram.com/reel/abc/\n"
                      "📘 Facebook: failed (The internet connection was interrupted)"]
    text = home_text(env)
    assert "📣 Last posted: Inflation, today" in text
    assert "⚠️ Posting failed for Inflation on Facebook: The internet connection was interrupted." in text
    assert press(env, f"h:retry:{db.get_run(rid)['ref']}:facebook", hm) == "Trying again."
    assert env["spawned"][-1] == ("publish", "--run", rid, "--platform", "facebook")


# ------------------------------------------------------------------ safety

def test_only_the_owner_chat_is_listened_to(env):
    from pipeline import home
    db, fake = env["db"], env["fake"]
    hm = menu(env)
    before = len(fake.calls)
    say(env, "/menu", chat=999, user=999)
    say(env, "/menu", chat=OWNER, user=999)
    press(env, "h:new", hm, chat=999, user=999)
    press(env, "h:batch", hm, chat=OWNER, user=999)
    assert len(fake.calls) == before and home.screen()["name"] == "home"


def test_old_and_stale_buttons_do_nothing(env):
    from pipeline import makequeue, review
    db = env["db"]
    rid = waiting(db)
    hm = menu(env)
    for data in ("c:a:1", "n:inflation", "f:3", "g:x", "a:" + rid):     # buttons from the old bot
        assert press(env, data, hm) == review.STALE
    assert press(env, "h:pick:coal_share", hm + 40) == review.STALE    # a home button on another message
    assert press(env, f"v:ok:{db.get_run(rid)['ref']}", hm) == review.STALE   # no review card is open
    assert press(env, "r:open", hm) == review.STALE
    assert makequeue.jobs("queued") == [] and db.get_run(rid)["stage"] == "awaiting_approval"


def test_offset_survives_a_restart(env):
    """The getUpdates offset lives in the DB, so a restarted bot never handles a button press twice."""
    from pipeline import makequeue
    db, bot, fake = env["db"], env["bot"], env["fake"]
    hm = menu(env)
    fake.updates = [{"update_id": 41, "callback_query": {
        "id": "cb", "data": "h:pick:coal_share", "from": {"id": OWNER},
        "message": {"message_id": hm, "chat": {"id": OWNER, "type": "private"}}}}]
    assert env["tg"].poll_once(1, bot) == 1
    assert [j["topic_id"] for j in makequeue.jobs("queued")] == ["coal_share"] and db.kv_get("telegram_offset") == "42"
    assert env["tg"].poll_once(1, bot) == 0
    assert [kw.get("offset") for kw in fake.of("getUpdates")] == [None, 42]


def test_a_second_bot_exits_at_once(temp_db, tmp_path, monkeypatch, capsys):
    from pipeline import approve_telegram as bot, lock
    monkeypatch.setattr(lock, "path", lambda key: tmp_path)
    monkeypatch.setattr(bot, "_bot_loop", lambda: pytest.fail("a second bot must not start"))
    with lock.single_instance("bot") as mine:
        assert mine
        assert bot.run_bot() == 0
    assert "already running" in capsys.readouterr().out
    monkeypatch.setattr(bot, "_bot_loop", lambda: 7)
    assert bot.run_bot() == 7  # the lock was released, so the next bot starts


# ------------------------------------------------------------------ throttling and 429

def test_edits_are_throttled_per_message_but_a_tap_is_answered_at_once(env):
    tg, fake, clock = env["tg"], env["fake"], env["clock"]
    ed = tg.Editor(clock=clock)
    assert ed.edit(7, "video", "one", {}) is True
    clock.t += 1
    assert ed.edit(7, "video", "two", {}) is False
    assert ed.edit(7, "video", "three", {}) is False      # the latest text replaces the waiting one
    assert ed.edit(8, "video", "other message", {}) is True
    clock.t += 1.9
    ed.flush()
    assert [e["caption"] for e in fake.of("editMessageCaption")] == ["one", "other message"]
    clock.t += 0.1
    ed.flush()
    assert [e["caption"] for e in fake.of("editMessageCaption")] == ["one", "other message", "three"]
    ed.touch(9)                                            # just sent: its first edit waits too
    assert ed.edit(9, "text", "hi", {}) is False
    assert ed.edit(9, "text", "tapped", {}, now=True) is True


def test_429_waits_for_retry_after(env):
    tg, fake, clock = env["tg"], env["fake"], env["clock"]
    fake.fail["editMessageCaption"] = [tg.TelegramError("editMessageCaption", 429, "Too Many Requests", retry_after=7)]
    ed = tg.Editor(clock=clock)
    assert ed.edit(7, "video", "one", {}) is False
    clock.t += 3
    ed.flush()
    assert ed.edit(7, "video", "one", {}, now=True) is False   # not even a tap jumps a 429 wait
    clock.t += 3.9
    ed.flush()
    assert len(fake.of("editMessageCaption")) == 1        # only the refused attempt
    clock.t += 0.2
    ed.flush()
    assert len(fake.of("editMessageCaption")) == 2 and not ed.pending


def test_call_raises_retry_after_from_the_api(monkeypatch):
    from pipeline import approve_telegram as tg

    class R:
        status_code = 429

        def json(self):
            return {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 5",
                    "parameters": {"retry_after": 5}}
    monkeypatch.setattr(tg.requests, "post", lambda *a, **k: R())
    with pytest.raises(tg.TelegramError) as e:
        tg.call("editMessageCaption")
    assert e.value.retry_after == 5 and e.value.code == 429


def test_run_py_batch_makes_the_queue_here_one_at_a_time(env, monkeypatch):
    """Without the bot, `run.py batch` works the same make queue in the foreground."""
    from pipeline import makequeue, orchestrator
    db = env["db"]
    made = []

    def run_pipeline(rid, from_step="fetch"):
        assert not [j for j in makequeue.jobs("running") if j["run_id"] != rid]   # never two at once
        made.append(db.get_run(rid)["topic_id"])
        if len(made) == 2:
            db.transition(rid, "data_ready")
            db.skip(rid, "low variety: no falling country")
            return False
        for st in FLOW:
            db.transition(rid, st)
        return True
    monkeypatch.setattr(orchestrator, "run_pipeline", run_pipeline)
    makequeue.start_batch(["inflation", "coal_share", "internet_users"])
    reported = []
    makequeue.run_foreground(on_done=lambda j: reported.append((j["topic_id"], j["state"])))
    assert made[:3] == ["inflation", "coal_share", "internet_users"] and len(made) == 4   # the skipped one is replaced
    assert [s for _, s in reported] == ["ready", "skipped", "ready", "ready"] and env["spawned"] == []
    assert makequeue.jobs("queued", "running") == []


# ------------------------------------------------------------------ the posting queue screen

def test_posting_queue_screen_with_dates_and_buttons_per_reel(env):
    from pipeline import home, posting, review
    db, fake = env["db"], env["fake"]
    a, b, c = waiting(db, "inflation"), waiting(db, "coal_share"), waiting(db, "broadband")
    hm = menu(env)
    press(env, "h:review", hm)
    for rid in (a, b, c):
        press(env, f"v:ok:{db.get_run(rid)['ref']}", review.card_state()["mid"])
    assert env["spawned"] == [] and [r["id"] for r in posting.queue()] == [a, b, c]
    sched = posting.schedule()
    first = posting.when_text(sched[0]["when"])
    assert f"📅 Approved, waiting to post: 3, next post {first}" in home.home_view()[0]
    assert press(env, "h:queue", hm) == "Posting queue"
    shown = fake.of("editMessageText")[-1]
    lines = shown["text"].splitlines()
    assert lines[0] == "📅 Posting queue: one reel a day at 21:00 IST"
    assert lines[2:5] == [f"{i}. {review.topic_name(s['run']['topic_id'])} · {posting.when_text(s['when'])}"
                          for i, s in enumerate(sched, 1)]
    assert buttons(shown["reply_markup"]) == [
        "⬇️ Move 1 down", "📤 Post 1 now", "↩️ Unapprove 1",
        "⬆️ Move 2 up", "⬇️ Move 2 down", "📤 Post 2 now", "↩️ Unapprove 2",
        "⬆️ Move 3 up", "📤 Post 3 now", "↩️ Unapprove 3", "🏠 Home"]
    ref = lambda rid: db.get_run(rid)["ref"]  # noqa: E731
    assert press(env, f"h:qup:{ref(c)}", hm) == "Moved Home broadband up."
    assert [r["id"] for r in posting.queue()] == [a, c, b]
    assert press(env, f"h:qun:{ref(a)}", hm) == "Unapproved: it is back in Ready for review."
    assert db.get_run(a)["stage"] == "awaiting_approval" and review.queue()[0]["id"] == a
    assert press(env, f"h:qnow:{ref(b)}", hm) == "Post it now?"
    confirm = fake.of("editMessageText")[-1]
    assert confirm["text"].startswith("📤 Post Coal power now?") and "Instagram and Facebook" in confirm["text"]
    assert buttons(confirm["reply_markup"]) == ["✅ Yes, post it now", "⬅️ Back to the queue", "🏠 Home"]
    assert env["spawned"] == []                                 # nothing posts before Yes
    assert press(env, f"h:qyes:{ref(b)}", hm) == "Posting now."
    assert env["spawned"] == [("publish", "--run", b)] and posting.starting() == b
    assert "📤 Posting now: Coal power (starting)" in home_text(env)
    assert press(env, f"h:qyes:{ref(a)}", hm) == "That reel is no longer waiting to post."


def test_a_note_on_the_home_card_goes_away_after_ten_minutes(env):
    from datetime import timedelta
    from pipeline import home
    s = home.set_screen("home", note="✅ Added to the make queue: Coal power. Making it now.")
    assert "Added to the make queue" in home.home_view(s)[0]
    later = home._now() + timedelta(minutes=11)
    assert "Added to the make queue" not in home.home_view(s, later)[0]
