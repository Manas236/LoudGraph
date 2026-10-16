# Graphony: "Which country ends up last?" for every reel, insights audit, hook tracking

Pass of 2026-10-16 (IST). Nothing was posted. Published reels were not touched. The previous REPORT.md
(fix pass of 2026-10-08) is in git history.

## Short answer

- **Insights are being pulled, and the token is fine.** It is a Page token, it never expires, and it
  has every scope needed (`instagram_manage_insights`, `read_insights`, `pages_read_engagement`). Every
  requested Instagram metric is accepted. **No token regeneration is needed.** Stats had stopped for
  three reasons, all now fixed:
  1. The bot had been off since 15 Oct 17:21 IST.
  2. A restart did not pull straight away.
  3. Facebook was never pulled.
  Because of this, the breakout reel had **no stats row at all**.
- **Breakout vs a low reel posted 20 minutes before it:** 2,125 vs 159 views, 1,535 vs 129 reach,
  12.1 s vs 9.6 s average watch. Both had 0 shares and 0 saves.
- **Every Graphony reel now opens with "Which country ends up last?"** (`opening.hook_template:
  which_last`, `hook_explore_pct: 0`). It uses the breakout reel's own generator logic, and the hook is
  only used when it is true of the rendered data.
- **All 5 unposted reels were re-branded.** None had to fall back to its old hook. Each new video keeps
  the original data, country order and timeline. Its audio stream is byte-identical to the old file.
  All 5 are in Ready for review, and the review card is on Telegram now.
- **One layout difference:** below the hook, the metric subtitle wraps to two lines on 4 of the 5 reels,
  so their dots row sits lower than the breakout's. Details are under "Re-brand" below.

## 0. Safety

| step | done |
|---|---|
| git | Working tree was clean at `8de081d` (nothing to commit before the change) |
| DB backup | The DB is `pipeline.db` (there is no `state.db`). Copied with the SQLite backup API to `pipeline.db.bak_20261016_1114` (integrity ok, 26 runs, 21 stats rows). `*.db.bak_*` was added to `.gitignore` |
| processes | No bot or dashboard was running at the start (the last bot heartbeat was 2026-10-15 17:21 IST), so nothing needed stopping |
| posting | Nothing was posted. Before the restart, the 2 approved reels went back to review, so the posting queue was empty. The missed 15 Oct slot was then closed as "nothing to post" (`posting:slot:2026-10-15`) |
| file backups | Every re-rendered reel keeps `video_oldhook.mp4`, `thumb_oldhook.jpg`, `meta_oldhook.json` and `captions_oldhook.json` |

## 1. Insights audit

All of these are read-only GET calls, on Graph API v25.0.

| # | check | result | evidence |
|---|---|---|---|
| a1 | Stats pull implemented | PASS | `pipeline/analytics.py` `collect()`: IG media insights into the `stats` table |
| a2 | Stats pull has run | PASS | 21 rows, from 2026-10-13T11:09:34Z to the latest at **2026-10-15T10:30:08Z** |
| a3 | Every posted reel covered | **FAIL** (before the fix) | Only 6 IG posts had rows. The breakout reel (IG post 79) and Coal power (post 71) had none. The last pull ran 19 minutes before the coal reel posted, and no pull ran after that. Facebook had **never** been pulled |
| b1 | Scheduled in the bot loop | PASS | `approve_telegram._bot_loop` calls `analytics.tick()`, which pulls when 6 h have passed since the last pull (`analytics.every_hours`) |
| b2 | Scheduled anywhere else | none | Task Scheduler has no Graphony task (only two unrelated Cloudflare tasks). The dashboard's **Check stats now** is a manual `run.py stats --no-summary` |
| b3 | Pull actually happening | **FAIL** (before the fix) | The bot had been down since 15 Oct 17:21 IST. A restart also waited 6 h after the last pull before pulling again |
| c | Token scopes (`debug_token`) | **PASS** | type PAGE, `is_valid` true, `expires_at` 0 (never), data access until 2027-01-06. Scopes: read_insights, pages_show_list, business_management, instagram_basic, **instagram_manage_insights**, instagram_content_publish, **pages_read_engagement**, pages_manage_posts, public_profile. Nothing missing |
| d | IG metric names (current docs) | **PASS** | All 9 requested metrics are accepted together: views, reach, likes, comments, shares, saved, total_interactions, ig_reels_avg_watch_time, ig_reels_video_view_total_time. `reels_skip_rate` is also accepted. The Meta docs list `plays`, `clips_replays_count`, `ig_reels_aggregated_all_plays_count` and `impressions` as deprecated; `views` replaced them. The code already used `views`, so no rename was needed. None were dropped |
| e | FB Page reels (posting is on) | PASS, 1 dropped | `/{video-id}/video_insights?period=lifetime`: 8 metrics accepted (fb_reels_total_plays, blue_reels_play_count, fb_reels_replay_count, post_video_avg_time_watched, post_video_view_time, post_video_social_actions, post_video_likes_by_reaction_type, post_video_followers). **`post_impressions_unique` is refused** (`(#100) The value must be a valid insights metric`), so it was dropped |

**Branches.** No scope is missing, so no token action is needed. No metric name was wrong; the code
was restructured anyway, so that a renamed metric is dropped instead of losing the whole pull
(see 2).

### Breakout vs a low reel

Instagram, at 2026-10-16 11:45 IST. The low reel is Coal power, posted the same day 20 minutes earlier
with "Who overtakes who?".

| | Child mortality (breakout) | Coal power (low) |
|---|---|---|
| hook | Which country ends up last? | Who overtakes who? |
| views | **2,125** | 159 |
| reach | **1,535** | 129 |
| avg watch time | **12.1 s** (51.5% of 23.5 s) | 9.6 s (44.9% of 21.5 s) |
| total watch time | 5.4 h | 0.35 h |
| likes / comments | 5 / 0 | 1 / 0 |
| shares / saves | 0 / 0 | 0 / 0 |
| Facebook plays | 0 | 0 |

Every other Instagram reel is at 118-149 views. All 8 Facebook reels together have 7 plays.

## 2. Stats made reliable

- **In the bot, on startup, then every 6 h.** `analytics.tick()` now pulls on the first loop after the
  bot starts, then every `analytics.every_hours`. It runs on a background thread. There was no
  separate scheduled stats task to disable. Instead, a machine-wide `stats` lock makes sure the bot's
  pull and the dashboard's Check stats now never run at the same time: a second pull returns at once.
- **Backfill.** Every pull asks about every reel ever posted, on both Instagram and Facebook. The
  first pull after the restart therefore filled in all 8 IG and all 8 FB reels, including the
  breakout.
- **History.** Each pull adds one row per post with `fetched_at`. Nothing is overwritten. New columns
  `shares`, `saved` and `avg_watch_s` sit next to views, reach, likes, comments and `avg_view_pct`, and
  everything Meta returned stays in `raw`.
- **Failures.** Errors are now caught per reel and per platform, and logged with the token redacted.
  If Meta refuses one metric name, it is dropped and the others are asked for again. A failed pull is
  retried on the next cycle. It runs on its own thread, so approvals and the posting queue keep going.
  The test below shows Meta hanging and then failing while posting still ticks and an approval goes
  through.
- **Weights.** Topic weights and hook weights use `analytics.weight_platforms` (YouTube + Instagram).
  Facebook rows are kept but left out of the weights. With every FB reel at 0-4 plays, including them
  would only double each reel's count and push all weights toward zero.

## 3. Hook template, tracked apart from the topic

- **New fields.** `posts.hook_template` and `runs.hook_template`, added by the existing idempotent
  migration list in `db.py`. A post copies its run's template when it is created.
- **How it's set.** The template is read from the question the video really shows (meta.json
  `opening.question`):

  | question | template |
  |---|---|
  | "Which country ends up last?" | `which_last` |
  | "Who overtakes who?" | `who_overtakes` |
  | anything else, or no on-screen hook | `other` |

  It is set at render time, and `run.py hooks` or every stats pull fills it in. A second run changed
  0 rows.
- **Hook weight.** It uses the same formula as the topic weight (`analytics.compute_weights` with
  `key="hook_template"`):
  1. Only posts at least 72 h old count, using their latest views.
  2. log(views) is z-scored per platform.
  3. Each template's weight is the mean z.
  4. With fewer than 3 posts, the weight is shrunk toward the global mean.

  The weights are stored in `hook_weights`. They are for reporting only; config decides the hook.
  Right now only `other` has posts old enough (4 posts, weight +0.00). `who_overtakes` posts pass 72 h
  on 16, 17 and 18 Oct. The breakout reel is the first `which_last` post and passes 72 h on
  **2026-10-18 16:21 IST**.
- **Telegram home card.** The 📊 Stats button now shows the last 10 reels, each with its hook, topic,
  views, average watch time and shares, then the average views per hook (2,125 for `which_last` vs 136
  for `who_overtakes` and 136 for `other`). The card is still a single card shown one screen at a time.

### Backfill mapping (every run with a video)

| run | topic | stage | on-screen hook | hook_template | IG views |
|---|---|---|---|---|---|
| r20261007-135650-f350 | air_passengers | rejected | (none) | other | |
| r20261007-141336-9435 | landlines | rejected | (none) | other | |
| r20261007-141505-ed3d | life_expectancy | rejected | (none) | other | |
| r20261007-141634-8357 | nuclear_share | rejected | (none) | other | |
| r20261007-152531-9d50 | air_passengers | published | (none) | other | 149 |
| r20261007-153030-d91b | landlines | published | (none) | other | 140 |
| r20261007-153521-6a24 | nuclear_share | published | (none) | other | 120 |
| r20261007-153747-82c0 | homicide_rate | published | (none) | other | 135 |
| r20261013-163924-6028 | meat_supply | published | Who overtakes who? | who_overtakes | 118 |
| r20261014-110322-1c91 | unemployment | published | Who overtakes who? | who_overtakes | 131 |
| r20261014-110459-f0bc | female_labour_participation | rejected | Which country falls the most? | other | |
| r20261014-110651-8461 | coal_share | published | Who overtakes who? | who_overtakes | 159 |
| r20261014-110740-beb5 | tourism_arrivals | rejected | Which country falls the most? | other | |
| r20261014-110830-eede | child_mortality | published | Which country ends up last? | **which_last** | **2,125** |
| r20261014-110924-753f | rnd_spending | approved, now in review | Which country ends up last? | which_last | |
| r20261014-111011-76c0 | co2_per_capita | approved, now in review | Who overtakes who? (old), Which country ends up last? (new) | which_last | |
| r20261014-154124-ee99 | population_growth | awaiting approval | Which country ends up last? | which_last | |
| r20261014-154350-107f | oil_per_capita | awaiting approval | Which country ends up last? | which_last | |
| r20261014-154607-8185 | inflation | awaiting approval | Who overtakes who? (old), Which country ends up last? (new) | which_last | |

The four reels from 7-9 Oct show no question on screen; DIAGNOSIS.md notes the title was taken off
the frame. They count as `other`.

## 4. "Which country ends up last?" as the default

- **Same logic as the breakout.** The breakout's hook came from `captions.opening()`. It fills the
  `ends_last` opener ("{country} ends up last: {value} in {year}") from the computed `final_low` fact:
  the country whose own last value is the lowest. So for this hook, "last" means the lowest final
  value, whatever the metric; for child mortality that is Chile, at 0.7%. Under the old logic, a seed
  picked between the two strongest openers (overtakes, then ends_last). That is why some reels got one
  hook and some the other.
- **Nothing new invented.** `opening()` now takes `prefer` (the configured opener). It fills it exactly
  as before. If the preferred opener cannot be used, or for `hook_explore_pct` % of the seeds, the old
  choice is made among the other openers.
- **Config.** `opening.hook_template: which_last` and `opening.hook_explore_pct: 0` are in
  config.yaml. `auto` restores the old behaviour.
- **Truth check.** `captions.check_ends_last` runs on the rendered data (the views' years and values).
  The hook is refused when:
  - there are fewer than 3 countries;
  - the claimed country's data does not reach the video's final year;
  - another country ends at or below it, also compared as shown on screen (0.7% vs 0.7% counts as a
    tie).

  A country whose own data stops a year early is compared on its last value, exactly as the end card
  ranks it. The R&D reel has this case: Ireland and the UK end in 2023 at 1.59% and 2.68%, while
  Mexico is at 0.25% in 2024. When a new reel falls back, its log says why.
- **Captions.** New hooks and questions are marked `"opener": "ends_last"` in caption_formats.json. A
  `which_last` reel uses only those, and no other reel uses them. They tease the payoff without naming
  it, for example "Guess which country ends up last", "Don't scroll before you see who ends up last",
  "Only one of these countries ends up last in 2025", and "Did your guess for last place match the
  ending?". The Gemini prompt also says not to give the answer away. The caption rules are
  unchanged: the template wording must be followed word for word, numbers can come only from computed
  facts (here, only the final year), no em dashes, and the data credit is added at posting. Video
  length is unchanged at 20-25 s.

## 5. Re-brand of unposted reels (`run.py rebrand`)

The five reels below were the only Graphony reels between rendered and published: none were in the
`rendered` stage, and nothing was mid-posting.

| reel | stage before, now | old hook | new hook (end-card answer) | check | captions |
|---|---|---|---|---|---|
| Research spending (753f) | approved, now in review | Which country ends up last? | Which country ends up last? (Mexico, 0.25% in 2024) | true | plain fills (Gemini 503) |
| Carbon emissions (76c0) | approved, now in review | Who overtakes who? | Which country ends up last? (Cuba, 2.2 t in 2024) | true | plain fills (Gemini 503) |
| Population growth (ee99) | awaiting approval | Which country ends up last? | same (Sweden, +0.25% in 2025) | true | Gemini |
| Oil use per person (107f) | awaiting approval | Which country ends up last? | same (Bangladesh, 925 in 2024) | true | Gemini |
| Inflation (8185) | awaiting approval | Who overtakes who? | Which country ends up last? (China, 0.1% in 2025) | true | Gemini |

**No reel fell back to its old hook.** Three already had the hook; they were still re-rendered, so the
cover and captions match.

- **What the new video reuses.** The frames are drawn again from each run's own data.json, pick.json,
  labels.json and **timeline.json**. The new MP4 **copies the old file's audio stream**
  (`ffmpeg -c:a copy`); nothing is synthesised or encoded again.
- **Queue order.** The two approved reels went back to review with their posting places (5 and 6)
  saved in `runs.prior_post_pos`. They are first in Ready for review, in their posting order. When
  re-approved, they take those places back, ahead of anything approved after them (`posting.next_pos`
  leaves those places free).
- **Telegram.** The bot opened the existing review card on the first of them. The card then moves
  through all five, one at a time.
- **Posting.** It resumes on the normal one-reel-a-day queue (21:00 IST) as soon as reels are approved.

### Verification of each re-rendered reel

| check | result |
|---|---|
| Hook on frame 0 | "Which country ends up last?": the same two lines, boxes, 88 px size, colour (#f5f7fa on #0b0d12), and cover time (2.35 s = `thumb_offset`) as the breakout. **Pixel diff of the hook band vs the breakout: 0.0**, in frame 0 and in the cover, on all 5 |
| Metric subtitle | Same position (first line), colour (#c3cad6) and size (48 px) as the breakout. Population growth's shorter subtitle fits at 52 px |
| Dots row | Same dot size, spacing and active colour (#22d3ee); one dot per country (4 or 5) |
| Audio | **MD5 of the audio stream identical to `video_oldhook.mp4`** on all 5. Durations are identical too: 21.57, 24.77, 22.73, 24.77 and 22.60 s |
| Verification gate (`run.py verify`) | **PASSED on all 5**: -14.2 LUFS, true peak -1.9 to -2.6 dBTP, A/V sync ~7.5 ms, centring, opening frames, duration |

**Layout difference, flagged.** The breakout's subtitle, "% of newborns who die before age 5", fits
on one line at 48 px. Four of the new subtitles are longer, so the same layout code wraps them to two
lines:
- "Research & development spending, / % of GDP"
- "CO2 from fossil fuels & industry, / tonnes per person"
- "Oil energy used per person, kWh per / year"
- "Consumer prices, % rise vs the year / before"

On those four, the dots row, flag and chart start **57 px lower** than in the breakout. On Population
growth they start 4 px lower. The hook itself is identical, and this is the same layout the breakout
was made with. Matching the breakout's positions exactly would need either shorter subtitles in
topics.yaml or a one-line, smaller subtitle. Both are design changes, so neither was made here.

## 6. After the restart

| check | result |
|---|---|
| pytest | **296 passed**: 285 before, plus 11 new in `tests/test_hooks.py`. The new tests cover the backfill mapping and its idempotence; the "last" check (true, false, tie, on-screen tie, fewer than 3 countries, data that stops early); which_last as the default, falling back on a tie, plus explore and auto; captions that tease without naming the answer; re-brand (old files kept, audio copied, published reels refused, a tie kept, queue order restored); a pull on startup; refused metrics and FB values; and **Meta hanging and then failing during a pull while the bot loop keeps posting and an approval goes through** |
| one bot instance | One `run.py bot` holds the `bot` lock: venv launcher PID 17308, interpreter PID 9340, which matches the heartbeat. Dashboard started too (`/ping` answers `graphony-dashboard`) |
| home card responds | The pinned card (message 37) was edited live by the restarted bot: "👀 Ready for your review: 5", "📅 Approved, waiting to post: 0", "📣 Last posted: Child mortality, … 2,125 views" |
| stats pull ran and wrote rows | 11:45:26 to 11:46:18 IST: Instagram 8 posts, Facebook 8 posts, so **16 new rows** (21 → 37) |
| review card | Message 45 is showing Research spending (video), the first of the 5 |

## Files

- **New:** `pipeline/hooks.py` (template tracking, backfill), `pipeline/rebrand.py`,
  `tests/test_hooks.py`.
- **Changed:**
  - `analytics.py`: FB pull, per-metric fallback, pull on startup, lock, hook weights, weight
    platforms.
  - `captions.py`: `check_ends_last`, `opening(prefer, explore)`, opener-specific caption pools,
    `fresh` captions.
  - `render.py`: config-driven opening, `audio_from`.
  - `db.py`: migrations, `hook_weights`, a post inherits its template, stats columns.
  - `home.py`: Stats screen.
  - `review.py`, `actions.py`, `posting.py`: posting place kept across a re-approval.
  - `orchestrator.py`: records the template and any fallback.
  - `run.py`: `rebrand`, `hooks`.
  - Also `caption_formats.json`, `config.yaml` and the README.

## Open notes

- **Captions.** Gemini was overloaded (HTTP 503 on both models) for Research spending and Carbon
  emissions. They got the plain template fills, which pass the same checks. A later
  `run.py captions --run …` would ask Gemini again.
- **Facebook.** The Page still gets 0-4 plays a reel. Its stats are kept but not used in the weights.
- **Country hashtag.** It comes from the strongest fact and is never chosen to be the answer, but in
  principle it could be the last country.
