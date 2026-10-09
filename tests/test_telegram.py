"""The Telegram control channel and its one-at-a-time review queue, with the Bot API faked (no network)."""
import json

import pytest

OWNER = 555
FLOW = ["data_ready", "picked", "labelled", "rendered", "awaiting_approval"]


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


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(temp_db, tmp_path, monkeypatch, pinned_config):
    from pipeline import accounts, actions, approve_telegram as tg
    pinned_config["paths"].update(out=str(tmp_path / "out"), cache=str(tmp_path / "cache"),
                                  tokens=str(tmp_path / "tokens"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(OWNER))
    fake = FakeTelegram()
    monkeypatch.setattr(tg, "call", fake)
    spawned = []
    monkeypatch.setattr(actions, "spawn", lambda *a: spawned.append(a) or 0)
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram", "facebook"])
    clock = Clock()
    bot = tg.Bot(clock=clock)
    return {"tg": tg, "db": temp_db, "fake": fake, "spawned": spawned, "bot": bot, "clock": clock}


def waiting(db, topic="inflation", title=None):
    from pipeline.config import run_dir
    rid = db.create_run(topic)
    for st in FLOW:
        db.transition(rid, st, **({"countries": ["IND", "USA"]} if st == "picked" else {}))
    d = run_dir(rid)
    (d / "video.mp4").write_bytes(b"\0" * 64)
    (d / "meta.json").write_text(json.dumps({"title": title or f"Video about {topic}", "duration_seconds": 41.6}))
    return rid


def press(e, data, message_id, chat=OWNER, user=OWNER, chat_type="private"):
    e["bot"].on_callback({"id": "cb", "data": data, "from": {"id": user},
                          "message": {"message_id": message_id, "chat": {"id": chat, "type": chat_type}}})
    return e["fake"].answers()[-1] if e["fake"].of("answerCallbackQuery") else None


def card_button(card, action):
    return f"c:{action}:{card['id']}"


def publisher_finishes(db, rid, **status):
    for platform, st in status.items():
        db.upsert_post(rid, platform, st, url=f"https://example.com/{platform}" if st == "live" else None,
                       message="boom" if st == "failed" else None)
    db.transition(rid, "publishing")
    db.transition(rid, "published")


# ------------------------------------------------------------------ one card at a time

def test_only_one_open_card_at_a_time(env):
    from pipeline import review
    db, bot, fake = env["db"], env["bot"], env["fake"]
    first, second, third = waiting(db, "inflation"), waiting(db, "gdp_growth"), waiting(db, "unemployment")
    bot.tick()
    bot.tick()
    bot.dispatch()
    assert len(fake.of("sendVideo")) == 1
    card = review.current_card()
    assert card["run_id"] == first and card["state"] == "open"
    assert review.create_card(second) is None            # the DB refuses a second open card
    sent = fake.of("sendVideo")[0]
    assert [b["text"] for b in sent["reply_markup"]["inline_keyboard"][0]] == ["✅ Approve", "❌ Reject", "⏭ Later"]
    caption = sent["caption"]
    assert caption.splitlines()[0] == "🎬 Video about inflation" and "⏱ 42 s" in caption
    assert "India, United States" in caption and "score" not in caption.lower()


def test_next_card_waits_for_decision_and_posting(env):
    from pipeline import review
    db, bot, fake, clock = env["db"], env["bot"], env["fake"], env["clock"]
    first, second = waiting(db, "inflation"), waiting(db, "gdp_growth")
    bot.tick()
    card = review.current_card()
    assert press(env, card_button(card, "a"), card["message_id"]) == "Approved — posting now."
    assert env["spawned"] == [("publish", "--run", first)]
    db.upsert_post(first, "instagram", "uploading")
    for _ in range(3):
        clock.t += 5
        bot.tick()
    assert len(fake.of("sendVideo")) == 1                 # still posting: no next card
    caption = fake.of("editMessageCaption")[-1]["caption"]
    assert "✅ Approved" in caption and "📸 Instagram: uploading…" in caption and "📘 Facebook: waiting" in caption
    publisher_finishes(db, first, instagram="live", facebook="live")
    clock.t += 5
    bot.tick()
    done = review.get_card(card["id"])
    assert done["state"] == "done" and done["slot"] is None
    assert len(fake.of("sendVideo")) == 2 and review.current_card()["run_id"] == second
    clock.t += 5
    bot.tick()
    final = [e["caption"] for e in fake.of("editMessageCaption") if e["message_id"] == card["message_id"]][-1]
    assert "Instagram: posted — https://example.com/instagram" in final and final.endswith("🏁 Done")
    assert len(final) <= 1024


def test_reject_and_later_move_the_queue(env):
    from pipeline import review
    db, bot, fake, clock = env["db"], env["bot"], env["fake"], env["clock"]
    first, second = waiting(db, "inflation"), waiting(db, "gdp_growth")
    bot.tick()
    card = review.current_card()
    assert press(env, card_button(card, "l"), card["message_id"]) == "Moved to the back of the queue."
    assert [r["id"] for r in review.queue()] == [second, first]
    card2 = review.current_card()
    assert card2["run_id"] == second and len(fake.of("sendVideo")) == 2
    assert press(env, card_button(card2, "r"), card2["message_id"]) == "Rejected."
    assert db.get_run(second)["stage"] == "rejected"
    assert review.current_card()["run_id"] == first        # the Later one comes back after the others
    card3 = review.current_card()
    press(env, card_button(card3, "l"), card3["message_id"])
    clock.t += 5
    bot.tick()
    assert review.current_card() is None                   # the only one left rests instead of bouncing back


# ------------------------------------------------------------------ stale buttons, double approvals

def test_stale_buttons_do_nothing(env):
    from pipeline import review
    db, bot = env["db"], env["bot"]
    first, second = waiting(db, "inflation"), waiting(db, "gdp_growth")
    bot.tick()
    card = review.current_card()
    assert press(env, f"a:{first}", 9) == "This card is stale"                          # a pre-queue message
    assert press(env, f"r:{second}", 8) == "This card is stale"
    assert press(env, card_button(card, "a"), card["message_id"] + 50) == "This card is stale"  # wrong message
    assert press(env, f"c:a:{card['id'] + 7}", card["message_id"]) == "This card is stale"    # wrong card
    assert press(env, "g:whatever", 3) == "This card is stale"
    assert db.get_run(first)["stage"] == "awaiting_approval" and env["spawned"] == []
    press(env, card_button(card, "r"), card["message_id"])
    assert press(env, card_button(card, "a"), card["message_id"]) == "This card is stale"   # closed card
    assert db.get_run(first)["stage"] == "rejected" and env["spawned"] == []


def test_double_approval_posts_once(env, monkeypatch, pinned_config):
    from pipeline import accounts, actions, orchestrator, publish_instagram, review
    from pipeline.lock import publish_lock, single_instance
    db, bot = env["db"], env["bot"]
    rid = waiting(db)
    bot.tick()
    card = review.current_card()
    press(env, card_button(card, "a"), card["message_id"])
    assert press(env, card_button(card, "a"), card["message_id"]) == "Already approved — it is posting."
    with pytest.raises(db.TransitionError):
        actions.approve(rid, "dashboard")                  # the dashboard button after Telegram
    assert env["spawned"] == [("publish", "--run", rid)]
    # the publisher itself: a second run of it, or one running in parallel, uploads nothing again
    calls = []
    pinned_config["youtube"]["enabled"] = False               # Instagram is this run's only destination
    monkeypatch.setattr(accounts, "ready_destinations", lambda run: ["instagram"])
    monkeypatch.setattr(publish_instagram, "publish", lambda *a: calls.append(a) or "dry_run")
    (env["tg"].run_dir(rid) / "meta.json").write_text(json.dumps({"title": "t"}))
    with single_instance(publish_lock(rid)):
        assert orchestrator.publish_run(rid) is False      # another publisher holds this run
    assert orchestrator.publish_run(rid) is True
    assert orchestrator.publish_run(rid) is False
    assert len(calls) == 1


def test_dashboard_decision_updates_the_card_and_advances(env):
    from pipeline import actions, review
    db, bot, fake, clock = env["db"], env["bot"], env["fake"], env["clock"]
    first, second, third = waiting(db, "inflation"), waiting(db, "gdp_growth"), waiting(db, "unemployment")
    bot.tick()
    card = review.current_card()
    actions.reject(first, "dashboard")
    clock.t += 5
    bot.tick()
    assert review.get_card(card["id"])["outcome"] == "rejected"
    edit = [e for e in fake.of("editMessageCaption") if e["message_id"] == card["message_id"]][-1]
    assert edit["caption"].endswith("❌ Rejected in the dashboard") and edit["reply_markup"] == {"inline_keyboard": []}
    card2 = review.current_card()
    assert card2["run_id"] == second
    actions.approve(second, "dashboard")
    assert press(env, card_button(card2, "a"), card2["message_id"]) == "Already handled in the dashboard."
    assert env["spawned"] == [("publish", "--run", second)]           # posted once
    clock.t += 5
    bot.tick()
    assert review.get_card(card2["id"])["state"] == "posting"
    assert "✅ Approved in the dashboard" in fake.of("editMessageCaption")[-1]["caption"]
    assert len(fake.of("sendVideo")) == 2                             # waits for posting


def test_restart_resumes_the_same_card(env):
    from pipeline import approve_telegram, review
    db, fake = env["db"], env["fake"]
    rid = waiting(db)
    env["bot"].tick()
    card = review.current_card()
    restarted = approve_telegram.Bot(clock=env["clock"])
    restarted.tick()
    restarted.tick()
    assert len(fake.of("sendVideo")) == 1 and review.current_card()["id"] == card["id"]
    env["bot"] = restarted
    assert press(env, card_button(card, "a"), card["message_id"]) == "Approved — posting now."
    assert db.get_run(rid)["stage"] == "approved"


def test_a_card_left_half_sent_by_a_crash_is_sent_again(env):
    from pipeline import review
    rid = waiting(env["db"])
    cid = review.create_card(rid)                        # the bot died during sendVideo
    env["bot"].tick()
    assert review.get_card(cid)["outcome"] == "unsent"
    assert review.current_card()["run_id"] == rid and len(env["fake"].of("sendVideo")) == 1


# ------------------------------------------------------------------ posting problems

def test_a_stage_over_ten_minutes_fails_on_the_card_and_the_queue_advances(env):
    from pipeline import review
    from pipeline.lock import publish_lock, single_instance
    db, bot, fake, clock = env["db"], env["bot"], env["fake"], env["clock"]
    first, second = waiting(db, "inflation"), waiting(db, "gdp_growth")
    bot.tick()
    card = review.current_card()
    press(env, card_button(card, "a"), card["message_id"])
    db.transition(first, "publishing")
    db.upsert_post(first, "instagram", "uploading")
    old = "2020-01-01T00:00:00Z"
    with db.db() as c:
        c.execute("UPDATE posts SET updated_at=?", (old,))
        c.execute("UPDATE review_cards SET approved_at=?", (old,))
    with single_instance(publish_lock(first)):             # the publisher is still running, stuck on Instagram
        clock.t += 5
        bot.tick()
        done = review.get_card(card["id"])
        assert done["state"] == "done" and done["watch"] == 1   # finished, but still following the upload
        assert review.current_card()["run_id"] == second
        assert "still posting" in press(env, f"c:t:{card['id']}:instagram", card["message_id"])  # never doubled
    text, markup = review.card_view(done)
    db.upsert_post(first, "instagram", "live", url="https://example.com/late")   # it finishes after all
    db.transition(first, "published")
    clock.t += 5
    bot.tick()
    late = review.get_card(card["id"])
    assert late["watch"] == 0 and "Instagram: posted — https://example.com/late" in review.card_view(late)[0]
    assert "Instagram: failed — took longer than 10 minutes" in text
    assert "Facebook: failed — didn't start within 10 minutes" in text
    assert [b[0]["callback_data"] for b in markup["inline_keyboard"]] == [f"c:t:{card['id']}:instagram",
                                                                            f"c:t:{card['id']}:facebook"]


def test_failed_platform_gets_retry_on_the_card(env):
    from pipeline import review
    db, bot, clock = env["db"], env["bot"], env["clock"]
    rid = waiting(db)
    bot.tick()
    card = review.current_card()
    press(env, card_button(card, "a"), card["message_id"])
    publisher_finishes(db, rid, instagram="live", facebook="failed")
    clock.t += 5
    bot.tick()
    done = review.get_card(card["id"])
    text, markup = review.card_view(done)
    assert "📘 Facebook: failed — boom" in text and text.endswith("🏁 Done")
    assert markup["inline_keyboard"] == [[{"text": "🔁 Retry Facebook", "callback_data": f"c:t:{card['id']}:facebook"}]]
    assert press(env, f"c:t:{card['id']}:instagram", card["message_id"]) == "This card is stale"
    assert press(env, f"c:t:{card['id']}:facebook", card["message_id"]) == "Trying Facebook again."
    assert env["spawned"][-1] == ("publish", "--run", rid, "--platform", "facebook")
    assert "Facebook: retrying…" in review.card_view(review.get_card(card["id"]))[0]


def test_caption_stays_under_the_telegram_limit(env):
    from pipeline import review
    long = review.fit("🎬 " + "x" * 3000, ["✅ Approved", "📸 Instagram: posted — https://e.com/" + "y" * 200])
    assert len(long) <= 1024 and long.endswith("y" * 50)


# ------------------------------------------------------------------ throttling and 429

def test_edits_are_throttled_per_message(env):
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


def test_429_waits_for_retry_after(env):
    tg, fake, clock = env["tg"], env["fake"], env["clock"]
    fake.fail["editMessageCaption"] = [tg.TelegramError("editMessageCaption", 429, "Too Many Requests", retry_after=7)]
    ed = tg.Editor(clock=clock)
    assert ed.edit(7, "video", "one", {}) is False
    clock.t += 3
    ed.flush()
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


# ------------------------------------------------------------------ chat filtering and commands

def test_only_the_owner_chat_is_listened_to(env):
    from pipeline import review
    db, bot, fake = env["db"], env["bot"], env["fake"]
    rid = waiting(db)
    bot.tick()
    card = review.current_card()
    before = len(fake.calls)
    bot.on_message({"chat": {"id": 999, "type": "private"}, "from": {"id": 999}, "text": "/status"})
    bot.on_message({"chat": {"id": -100, "type": "group"}, "from": {"id": OWNER}, "text": "/new"})
    bot.on_message({"chat": {"id": OWNER, "type": "private"}, "from": {"id": 999}, "text": "/pause"})
    press(env, card_button(card, "a"), card["message_id"], chat=999, user=999)
    press(env, card_button(card, "a"), card["message_id"], chat=OWNER, user=999)
    assert len(fake.calls) == before                       # not even an answer
    assert db.get_run(rid)["stage"] == "awaiting_approval" and not review.paused()
    bot.on_message({"chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}, "text": "/help"})
    assert "/new" in fake.of("sendMessage")[-1]["text"]


def test_commands_are_registered(env):
    tg, fake = env["tg"], env["fake"]
    tg.register_commands()
    names = [c["command"] for c in fake.of("setMyCommands")[0]["commands"]]
    assert names == ["new", "ready", "status", "pause", "resume", "help"]


def test_pause_and_resume_persist(env):
    from pipeline import approve_telegram, review
    db, bot, fake = env["db"], env["bot"], env["fake"]
    waiting(db)
    msg = {"chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}
    bot.on_message({**msg, "text": "/pause"})
    approve_telegram.Bot(clock=env["clock"]).tick()        # a restarted bot stays paused
    assert review.paused() and fake.of("sendVideo") == []
    bot.on_message({**msg, "text": "/resume"})
    assert not review.paused() and len(fake.of("sendVideo")) == 1


def test_status_shows_render_card_queue_and_test_mode(env, pinned_config):
    from pipeline import approve_telegram as tg
    db, bot = env["db"], env["bot"]
    waiting(db, "inflation", "Prices, played as music")
    waiting(db, "gdp_growth")
    bot.tick()
    text = tg.status_text()
    assert text.startswith("📊 Graphony status")
    assert "🛠 Nothing rendering" in text and "Open card: Prices, played as music (waiting for your decision)" in text
    assert "📥 Review queue: 1 waiting" in text and "Test mode: ON" in text
    pinned_config["dry_run"]["instagram"] = False
    assert "Test mode: on for YouTube; live on Instagram" in tg.status_text()
    assert tg.daily_summary_text().startswith("📊 Graphony · daily summary")


def test_new_offers_three_unmade_topics_and_renders_one_at_a_time(env, monkeypatch):
    from pipeline import review
    db, bot, fake, clock = env["db"], env["bot"], env["fake"], env["clock"]
    scores = {"inflation": 90, "gdp_growth": 80, "unemployment": 70, "cereal_yield": 60, "broadband": 50}
    monkeypatch.setattr(review, "topic_score", lambda t: scores.get(t["id"], 1))
    waiting(db, "inflation")                                  # already has a video: never suggested again
    msg = {"chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}
    bot.on_message({**msg, "text": "/new"})
    menu = fake.of("sendMessage")[-1]
    picks = [row[0]["callback_data"] for row in menu["reply_markup"]["inline_keyboard"]]
    assert picks == ["n:gdp_growth", "n:unemployment", "n:cereal_yield", "n:*"]
    menu_id = fake.next_id
    assert press(env, "n:gdp_growth", menu_id) == "Starting."
    assert press(env, "n:unemployment", menu_id) == "This card is stale"    # a menu works once
    job = review.jobs("running")[0]
    assert env["spawned"] == [("produce", "--run", job["run_id"], "--from", "fetch")]
    assert fake.of("editMessageText")[-1]["text"].endswith("📥 Fetching data…")
    bot.on_message({**msg, "text": "/new"})                    # while that one renders
    assert "your pick will wait its turn" in fake.of("sendMessage")[-1]["text"]
    assert press(env, "n:unemployment", fake.next_id) == "Queued: another video is rendering."
    assert len(env["spawned"]) == 1
    second_menu = fake.next_id
    # the first one turns out too boring: said plainly, then the queued one starts
    db.transition(job["run_id"], "data_ready")
    db.skip(job["run_id"], "low variety: no falling country (net < -0.5)")
    clock.t += 5
    bot.tick()
    clock.t += 5
    bot.tick()
    first_text = [e["text"] for e in fake.of("editMessageText") if e["message_id"] == menu_id][-1]
    assert "😴 Skipped" in first_text and "every country moved the same way" in first_text
    assert "⚠️" not in first_text and "error" not in first_text.lower()
    assert len(env["spawned"]) == 2 and review.jobs("running")[0]["message_id"] == second_menu


def test_new_render_reaches_the_review_queue(env):
    from pipeline import review
    db, bot, fake, clock = env["db"], env["bot"], env["fake"], env["clock"]
    job = review.add_job("gdp_growth", 42)
    bot.advance_jobs()
    rid = review.get_job(job["id"])["run_id"]
    for st in FLOW[:3]:
        db.transition(rid, st)
    db.set_progress(rid, .45)
    clock.t += 5
    bot.tick()
    assert fake.of("editMessageText")[-1]["text"].endswith("🎨 Rendering 45%")
    for st in FLOW[3:]:
        db.transition(rid, st)
    clock.t += 5
    bot.tick()
    assert [e["text"] for e in fake.of("editMessageText") if e["message_id"] == 42][-1].endswith("✅ Ready: it's in the review queue.")
    assert review.current_card()["run_id"] == rid


def test_ready_lists_unposted_videos_and_moves_one_to_the_front(env):
    from pipeline import review
    db, bot, fake = env["db"], env["bot"], env["fake"]
    first, second, third = waiting(db, "inflation"), waiting(db, "gdp_growth"), waiting(db, "unemployment")
    posted = waiting(db, "broadband")
    db.transition(posted, "approved")
    db.upsert_post(posted, "instagram", "live")
    bot.tick()                                                 # first is on the open card
    bot.on_message({"chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}, "text": "/ready"})
    rows = fake.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
    refs = [int(r[0]["callback_data"][2:]) for r in rows]
    assert refs == [db.get_run(third)["ref"], db.get_run(second)["ref"]]   # newest first, no open/posted ones
    assert press(env, f"f:{db.get_run(third)['ref']}", fake.next_id) == "Next in the review queue."
    assert [r["id"] for r in review.queue()][:2] == [third, first]


# ------------------------------------------------------------------ polling

def test_offset_survives_a_restart(env):
    """B3: the getUpdates offset lives in the DB, so a restarted bot never re-delivers a button press."""
    from pipeline import review
    db, bot, fake = env["db"], env["bot"], env["fake"]
    rid = waiting(db)
    bot.tick()
    card = review.current_card()
    fake.updates = [{"update_id": 41, "callback_query": {
        "id": "cb", "data": card_button(card, "a"), "from": {"id": OWNER},
        "message": {"message_id": card["message_id"], "chat": {"id": OWNER, "type": "private"}}}}]
    assert env["tg"].poll_once(1, bot) == 1
    assert db.get_run(rid)["stage"] == "approved" and db.kv_get("telegram_offset") == "42"
    assert env["tg"].poll_once(1, bot) == 0                # "restart": nothing is delivered twice
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
