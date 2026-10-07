# Build report (2026-10-07)

Built on Windows 11, Python 3.11.0 venv (`.venv`), FFmpeg 9.0.2, i5 with 6 threads and 8 GB RAM.
Everything lives in the project root folder. 15 commits, one or more per milestone (`git log`).
All publishers stayed in dry-run. No credentials were present, so nothing was posted anywhere.

## What works

| # | Milestone | Evidence |
|---|---|---|
| 0 | Constraints | `pathlib` only, no shell scripts; Windows/Linux file lock (`pipeline/lock.py`) gives one render at a time; frames are piped to FFmpeg stdin with no per-frame files; `.gitignore` covers `.env`, `tokens/`, `cache/`, `out/`, `*.db`. `git status` after a full run shows none of them tracked. |
| 1 | Repo layout + state machine | `pipeline/db.py` (SQLite WAL, tables `runs`, `stage_log`, `posts`, `stats`, `topic_weights`). Illegal transitions raise errors and every transition is logged. `tests/test_db.py` passes (5 tests). |
| 2 | Data + topics | `python run.py topics-verify` fetched all 37 seed topics from the live World Bank API and OWID: **34 verified, 3 dropped** (see Topics). Series are cached in `cache/series/` with `fetched_at` and re-fetched after 30 days. Only the 47 pool countries are kept, so aggregates never enter. |
| 3 | Scorer | `pipeline/score.py`. Weights and thresholds are in `config.yaml`. `tests/test_score.py`: flat, linear-interpolated, monotone, V-shape, noisy and crash series, plus gaps, low swing and `min_meaningful` (10 tests). |
| 4 | Picker | 6–9 countries fit 30–45 s; the config caps it at 8. Greedy max-min z-distance, net>0.5 / net<−0.5 / reversals≥2 constraints, best score last, never repeats a (topic, set), and fails with "not enough interesting data" below 5. `tests/test_picker.py` (5 tests). |
| 5 | Labels | Turning point is found in code (largest single-year move if ≥25% of range, else the pivot before the largest zigzag leg). Gemini call, model auto-selection, retry/backoff, quota handling and a cache keyed by (topic, country, year) are implemented. `validate()` rejects unconfident answers, more than 4 words or 24 chars, a year off by more than ±1, and any number except a nearby year. `tests/test_labels.py` (7 tests). **Not exercised live: no GEMINI_API_KEY**, so every run logs `0/8 labels accepted; no label because: no GEMINI_API_KEY` and renders without tags. |
| 6 | Timeline | `pipeline/timeline.py` is the only timing source. Audio onsets and the line head both read `Slot.onsets` / `Slot.year_pos`. `tests/test_timeline.py` (6 tests). |
| 7 | Audio | Karplus-Strong plucks (IIR with a fractional-delay allpass; autocorrelation shows pitch within 0.05% from 165 to 1319 Hz), detuned-saw pad with a sub, swept-noise whoosh, end chord of all pad roots (all from A minor pentatonic, so no semitone clashes). Master: 30 Hz HPF, 15 kHz LPF, look-ahead limiter, −14 LUFS (pyloudnorm). `tests/test_audio.py` (4 tests). |
| 8 | Render | skia-python 144 backend (logged in `stage_log` and `meta.json`) with a Pillow 2× fallback that was rendered and tested (`test_pillow_fallback_renders_in_safe_zone`). Inter (OFL) and flag-icons (MIT, SVG plus rasterised PNG) are vendored. Layout constants put safe zones at x ≤ 950 and y ≤ 1536. `tests/test_layout.py` asserts the constants **and** renders real frames (long names, wide tick labels, label tags, end card) to check every recorded text box (11 tests). Each run writes `meta.json`, `thumb.jpg` and `video.mp4`. Render takes about 60–75 s for a 40 s video (18 fps). |
| 9 | Telegram | `pipeline/approve_telegram.py`: long-poll `getUpdates`, `sendVideo` with Approve / Reject / Regenerate buttons, only `TELEGRAM_CHAT_ID` accepted, failure alerts, `/status`, daily summary (sent by `run.py stats`). `tests/test_telegram.py` covers the logic with the Bot API mocked (4 tests). **Not exercised live: no token.** |
| 10 | Dashboard | `python run.py dashboard` was started and fetched with curl: `/`, `/run/<id>`, `/analytics`, `/topics` and the thumbnail all returned 200. `netstat` showed `127.0.0.1:5055 LISTENING` only, and binding to 0.0.0.0 is refused. CSRF token on all POSTs, media allowlist. Buttons call `pipeline/actions.py`, the same functions the bot calls. `tests/test_dashboard.py` (5 tests). |
| 11 | Publishing | YouTube: resumable `videos.insert`, categoryId 27, title ≤ 100 chars, read-back that detects `private_locked` and alerts, daily cap. Instagram: endpoints **checked against the current Meta docs** (examples show v25.0): REELS container with `upload_type=resumable`, upload to `rupload.facebook.com/ig-api-upload/...` with `Authorization: OAuth`, `offset` and `file_size` headers, `status_code` polling, `media_publish`. A `video_url` mode is marked BLOCKED without a public host. Optional FB Page Reels (`video_reels` start / upload / finish). In dry-run the approved test run went to `published` with both posts `dry_run` and the exact requests (token redacted) in `publish_youtube.json` / `publish_instagram.json`. |
| 12 | Analytics + selector | `run.py stats` ran: it pulled stats for 0 posts (none live) and logged the daily summary. Insights metrics were checked against the Meta docs: `plays` is gone; the code uses `views,reach,likes,comments,ig_reels_avg_watch_time`. Weight is mean log(views) after 72 h, shrunk toward the global mean below 3 posts; a topic retires after 3 bottom-quartile posts. Selector is 70% weighted / 30% least-tried and respects cooldown. `tests/test_analytics_selector.py` (4 tests). |
| 13 | CLI + scheduling | `doctor`, `fetch`, `topics-verify`, `produce`, `bot`, `publish`, `stats`, `dashboard`, plus `verify`. `python run.py doctor` gives **0 failing, 4 warnings** (the 4 missing credentials). README documents Windows Task Scheduler, Linux cron + systemd, and `screen`. Cadence is 2/day (`produce` defaults to it). |
| 14 | Verification gate | Results below. |

### Verification gate

1. `python -m pytest`: **61 passed**.
2. `python run.py produce --count 3` (all publishers dry-run). The selector picked 3 different verified topics: **landlines, life_expectancy, nuclear_share**. 3/3 reached `awaiting_approval`. An earlier `air_passengers` run was also re-verified.
3. and 4. `python run.py verify --run ...` (ffprobe, FFmpeg `ebur128` on the final MP4, spectral-flux onsets in `audio.wav` vs timeline):

| run | topic | size | fps | duration | audio | integrated | true peak | notes matched | median sync error |
|---|---|---|---|---|---|---|---|---|---|
| r20261007-141336-9435 | landlines | 1080×1920 | 30 | 39.9 s | aac 48 kHz | −14.0 LUFS | −1.7 dBFS | 278/278 | 7.6 ms |
| r20261007-141505-ed3d | life_expectancy | 1080×1920 | 30 | 39.9 s | aac 48 kHz | −14.0 LUFS | −1.7 dBFS | 272/272 | 7.6 ms |
| r20261007-141634-8357 | nuclear_share | 1080×1920 | 30 | 39.9 s | aac 48 kHz | −14.1 LUFS | −1.9 dBFS | 288/288 | 7.6 ms |
| r20261007-135650-f350 | air_passengers | 1080×1920 | 30 | 39.9 s | aac 48 kHz | −14.1 LUFS | −1.6 dBFS | 232/232 | 7.6 ms |

   All checks pass (limit: median ≤ 40 ms). Full results are in each `out/<run>/verify.json`.

5. I looked at every contact sheet and fixed what was wrong:
   * Plain crossfades overlapped two country names ("INDEXESIA"). Transitions now dip through the shared background.
   * Y tick labels were crossed by the line. They now sit in a left gutter.
   * Label tags were drawn on top of the line. Placement now scores 8 candidate positions against the finished line.
   * The middle x-axis label read 2006 for 1990–2023 because of banker's rounding. Fixed to half-up.
   * The Pillow fallback ignored text alpha, so the watermark was bright white. Fixed.
   * All 32 flags in the final sheets match their countries. No text is in the unsafe zones.

   I also measured the final MP4 rather than trusting the WAV. FFmpeg's native AAC encoder pushed true peak to **+3.7 dBFS** on one video (WAV −2.6 dBTP). The cause is Perceptual Noise Substitution re-synthesising the plucks' noise-burst attacks. Encoding with `-aac_pns 0` makes the MP4 peak match the WAV. A post-encode guard stays in place as a safety net; it did not need to act on the final renders.
6. Telegram: skipped, because there are no credentials. Callback handling is covered by mocked tests only.

## Sample videos and contact sheets

| topic | video | contact sheet |
|---|---|---|
| landlines | `out/r20261007-141336-9435/video.mp4` | `out/r20261007-141336-9435/contact.png` |
| life_expectancy | `out/r20261007-141505-ed3d/video.mp4` | `out/r20261007-141505-ed3d/contact.png` |
| nuclear_share | `out/r20261007-141634-8357/video.mp4` | `out/r20261007-141634-8357/contact.png` |
| air_passengers (extra) | `out/r20261007-135650-f350/video.mp4` | `out/r20261007-135650-f350/contact.png` |

Each folder also has `meta.json` (title, descriptions, tags, sources), `thumb.jpg`, `audio.wav`, `timeline.json` and `verify.json`. The air_passengers run also has `publish_*.json` from the dry-run publish.

## Topics

**Verified (34).** The columns show usable pool countries / countries that pass the scorer / latest data year.

| category | topics |
|---|---|
| economy | inflation (44/38/2025), gdp_growth (47/47/2025), gdp_per_capita (47/46/2025), unemployment (47/45/2025, modelled), military_spending (46/42/2024), remittances (44/21/2025), manufacturing_share (47/34/2025), tourism_arrivals (41/37/2020) |
| people | fertility_rate_un (47/43/2023, OWID UN WPP), female_labour_participation (47/29/2025, modelled), population_growth (47/45/2025), net_migration (47/44/2025), median_age (47/7/2023) |
| health | life_expectancy (47/8/2023), child_mortality (47/16/2024), health_spending (46/46/2024), alcohol_consumption (47/30/2020), homicide_rate (33/21/2024), meat_supply (47/43/2023) |
| energy | nuclear_share (47/19/2025), coal_share (47/26/2025), renewables_share (47/39/2025), solar_share (46/11/2025), hydro_share (46/36/2025), oil_per_capita (42/42/2024) |
| tech | internet_users (47/34/2025), mobile_subscriptions (47/41/2024), landlines (47/42/2024), broadband (43/36/2024), rnd_spending (30/23/2024) |
| planet | co2_per_capita (47/45/2024), temperature_anomaly (47/47/2025), air_passengers (45/43/2023), cereal_yield (47/47/2024) |

**Dropped (3), commented out in `topics.yaml` with the reason:**
* `fertility_rate` (WDI `SP.DYN.TFR.IN`): the World Bank API returns "Invalid value" for this code today. Confirmed with a direct `curl`. Replaced by the OWID UN WPP series (`fertility_rate_un`), which verified.
* `refugees_by_origin` (WDI `SM.POP.REFG.OR`): the API says "The indicator was not found. It may have been deleted or archived." Confirmed with `curl`.
* `forest_area` (WDI `AG.LND.FRST.ZS`): fetched fine, but 0 countries pass the scorer. The FAO series is interpolated between assessments, so it would sound like one repeated note.

## Blocked on the owner

Put everything in `.env` (template: `.env.example`). Then run `python run.py doctor`, and once it shows OK, set `dry_run.<platform>: false` in `config.yaml`.

1. **Gemini (labels)**: create an API key at https://aistudio.google.com/apikey and set `GEMINI_API_KEY=...`. The first run lists the models and picks a flash-class one (written to `cache/gemini_model.json`). The model-selection code has not been run against the live list.
2. **Telegram (approval)**: create a bot with @BotFather (`/newbot`) and set `TELEGRAM_BOT_TOKEN`. Send the bot a message, read `chat.id` from `https://api.telegram.org/bot<TOKEN>/getUpdates`, and set `TELEGRAM_CHAT_ID`. Then run `python run.py bot`. Until then, approval works from the dashboard.
3. **YouTube**:
   * In Google Cloud Console, create a project and enable *YouTube Data API v3* and *YouTube Analytics API*.
   * Configure the OAuth consent screen (External; add your account as a test user).
   * Create Credentials → OAuth client ID → *Desktop app*, and save the JSON as `tokens/client_secret.json`.
   * Set `YT_CLIENT_SECRET_FILE=tokens/client_secret.json` and run `python -m pipeline.publish_youtube --auth` once.
   * Uploads stay **private** until the project passes Google's YouTube API audit. The pipeline will mark these `private_locked` and alert you.
4. **Instagram Reels**:
   * You need an Instagram professional account linked to a Facebook Page.
   * Create a Meta app using *Instagram API with Facebook Login*, with `instagram_basic`, `instagram_content_publish`, `instagram_manage_insights` and `pages_read_engagement`.
   * Set `IG_ACCESS_TOKEN` to a long-lived **Page** access token, and `IG_USER_ID` to the Page's `instagram_business_account.id`.
   * Optional FB Page Reels: also grant `pages_show_list` and `pages_manage_posts`, set `FB_PAGE_ID`, and set `facebook.enabled: true`.
5. **Scheduling**: register the Task Scheduler or cron jobs from the README. Nothing has been scheduled on this machine.

## Known weaknesses (noticed, not fixed)

* **Scorer weights as specified let pure noise tie a clean story.** White noise gets the minimum reversal points (5), but its large year-to-year jumps earn nearly full shock points (25). A synthetic noise series scored 66.9 against 65.2 for a clean V. Real noisy topics (`gdp_growth`, `temperature_anomaly`) pass for 47/47 countries. A fix would be to scale shock down when reversals ≥ 6; it is not applied, to keep the specified formula.
* **Smooth topics give low-diversity sets.** life_expectancy (min pairwise distance 0.09) and median_age only have 7–8 passing countries, almost all rising, so the "one falling" constraint cannot be met.
* **Live APIs are unproven.** Gemini, Telegram, YouTube, Instagram and Facebook calls are written from current docs but have never had a real credential. Expect first-run fixes.
* **Sync is checked audio vs timeline, not on decoded video.** The line head moves on 33 ms frame steps, and the detector shows a constant ~7.6 ms analysis offset rather than a timing error.
* **The y-axis is autoscaled per country and not zero-based.** This is deliberate for drama, and the swing ≥ 0.15 filter prevents near-flat lines. Each country also has its own pitch range, so pitch compares years within a country, not countries with each other.
* **Topic weights mix YouTube and Instagram views on one log scale.** Platforms with different reach will bias it.
* **The Telegram `getUpdates` offset is in memory only.** After a bot restart, Telegram may re-deliver recent button presses. They are handled as "already approved/rejected", so nothing breaks, but the owner may see an extra notice.
* **Detached jobs are not tracked.** Dashboard and bot actions launch `run.py` as detached processes (logs in `cache/logs/job-*.log`). There is no job table, so a crashed job only shows up as a run stuck in its stage.
* **Re-running a run from a stage rewrites its files in place.** `produce --run X --from render` on a published run resets it to `awaiting_approval`, and the posted video's files are overwritten.
* **The Pillow fallback is plainer.** It has a flat fill instead of a gradient and ring-approximated glow; frames take ~58 ms vs ~38 ms with skia.
* **Some data ends early.** `tourism_arrivals` stops in 2020 and `alcohol_consumption` in 2020 (WHO), so those videos end at those years.


---

# Fix pass 01 (2026-10-07)

State before this pass is tagged `v0-first-build`. All publishers stayed in dry-run. `python -m pytest`: **89 passed**.

## A. Owner's complaints

### A1. One centre axis
- **Changed:**
  - Every centred element sits on x = 540, with symmetric 130 px side margins (`render.py`: `MARGIN`, `CX`). This also clears the right-12% zone, because 950 ≤ 88% of 1080.
  - Text is centred by its **ink** bounds, not its advance width (`canvas.ink()`), so the measured pixel centre is exact.
  - The flag and the country name are centred as one group.
  - The chart's gridlines span 130–950 symmetrically. Data is inset 34 px on both sides, so the line's glow stays inside the gridlines.
  - Y-axis tick labels are inside the plot, small and left-aligned, just above their gridline. There is no gutter.
  - Progress dots all have the same outer size, so the active dot can't skew the row.
- **Tests:**
  - `test_every_centred_element_is_on_x540_in_pixels[skia|pillow]` renders mid-country, transition and end-card frames and measures each element's non-background pixel extent from the image, the same way the owner measured. It asserts a centre of 539.5 ± 2 px (the pixel centre of 0–1079) and margins equal within 4 px.
  - `verify` repeats the same measurement on frames decoded from every MP4 (the `centred_540` check below).
- **Measured on the new air_passengers MP4** (`r20261007-152531-9d50`, mid-Germany frame), in the same format as the owner's table:

| element | x range | centre |
|---|---|---|
| header (metric) | 137-942 | 539.5 |
| progress dots | 418-660 | 539.0 |
| flag + country name | 270-808 | 539.0 |
| chart incl. y labels | 130-949 | 539.5 |
| year | 436-643 | 539.5 |
| value | 322-757 | 539.5 |
| end-card title | 148-931 | 539.5 |
| end-card rows | 129-949 | 539.0 |
| end-card CTA | 289-790 | 539.5 |

  Before (v0): everything was centred at about 507, while the watermark sat at 540.

- **Overlap fix found while reviewing contact sheets:** a line passing a tick label at the left edge used to run through the text (US "19.0%", South Africa "5.0%", Venezuela "20.0"). Tick labels are now drawn above the line with a 5 px background halo, so the line goes visibly behind them. `test_line_never_shows_through_a_tick_label[skia|pillow]` checks this, and I confirmed the test fails when the halo is turned off.

### A2. No title pill
- The pill is gone. The hook title stays only in `meta.json` as the post title.
- The metric subtitle is now the header: bold white text, sized 54→40 px to fit on one line, or two lines at 54→34 px if it can't.
- The stack (header → dots → country → chart → year → value) is computed by `compute_layout()` with fixed gaps and vertically centred in y 0–1536. For a one-line header it runs from y ≈ 169 to y ≈ 1367. The dead band between the subtitle and the chart is gone.
- **Tests:** `test_no_pill_no_source_no_brand_and_text_in_safe_zone`, `test_header_is_the_metric_and_tick_labels_are_inside_the_chart`, `test_stack_is_vertically_centred_in_safe_area` (top and bottom margins within 40 px), `test_every_topic_header_fits_two_lines`.

### A3. No source text, no brand
- "Source: …" is removed from every frame, including the end card. The data credit stays in the post description (CC BY attribution).
- `brand.name` is now `""`, and nothing is drawn when it is empty. `test_brand_draws_only_when_configured` covers the opt-in case.
- The old channel name was removed from rendered text, meta, descriptions, tags, Telegram captions, the dashboard title (now "Pipeline"), the User-Agent, the doctor header, the CLI description, and the README (neutral placeholders: "Shorts pipeline", `/opt/shorts-pipeline`, `shorts-pipeline-bot.service`).
- The SQLite file was renamed from the old name to `pipeline.db` (a plain file rename; all runs kept).
- `tests/test_brand.py` greps every text file in the repo, excluding `.git`, `.venv`, `out` and `cache`, and finds no mention.
- **Not changed:**
  - The local folder name, as you allowed.
  - Earlier git commits carry the old name as author name; rewriting published history is destructive, so I left it. New commits use a neutral author.
  - Videos rendered before this pass under `out/` still have the old watermark burned in. I didn't delete them; say the word and they go.

### A4. Audio follows the data
- **Stems:** `pluck.wav`, `pad.wav` and `fx.wav` are rendered separately, levelled by measured loudness, mixed and mastered. Each stem is scaled by the master gain and kept in `out/<run>/`.
- **Plucks lead:** the pad is set 9 LU under the plucks. Plucks decay faster (T60 0.3 s, about 250 ms audible) and have a brighter attack (brightness 0.9 plus a 2 ms high-passed pick transient).
  - Measured pluck minus pad on all 4 new videos: see the gate table (≥ 6 required).
- **Pitch:**
  - The scale now covers 3 octaves (16 notes).
  - `note_indices()` guarantees that a move of 15% or more of the range changes the note by at least 2 scale steps. `test_big_moves_always_change_the_note_by_two_steps` runs this over 300 random walks.
- **Pad tracks the data:**
  - The low-pass cutoff (180 → 2200 Hz) and gain (0.2 → 1.0) follow the value the line head shows, smoothed over 150 ms.
  - Total detune is 3 cents (it was 9), so there is no audible pulsing.
  - The constant sub under the pad is gone.
- **Events (move ≥ 25% of range):**
  - `timeline.py` gives the step into an event 3× the duration. The slot gets longer, and video and audio both read the new knots.
  - The pluck stem plays a scale run from the old note to the new one: a fall is heard as a falling run, a jump as a rising one.
  - Falls add a short 75→42 Hz sub hit and duck the pad to 5% for 0.35 s ("riser-less silence"). Jumps add a short bright accent.
  - The head dot pulses harder and an expanding ring marks the landing point.
  - The picker rebuilds the timeline after slow-mo and drops the least interesting country if the video would exceed 45 s; homicide_rate dropped Sweden.
- **Tests** (`tests/test_audio.py`, synthetic 3-country track):

| test | measured |
|---|---|
| Spearman(values, detected note), per country on the pluck stem | 0.988 / 0.988 / 0.999 |
| Pluck vs pad stem | −12.88 vs −21.18 LUFS (**8.30 LU**) |
| Flat-crash-recover, pad stem in the flat years (1992–2001) | RMS 0.0822, centroid 1518 Hz |
| Same, crash year and the year after (2003–2005) | RMS 0.0010, centroid 238 Hz |
| Event run detected on the pluck stem | 10 strictly descending notes (MIDI 86 → 52) |
| Low band (< 90 Hz) of the fx stem around falls vs elsewhere | > 10× (`test_sub_bass_only_on_falls`) |

- **Crash test clip** (`out/crash_test/analysis.json`): slow-mo step 0.36 s vs normal 0.12 s; pad stem flat years RMS 0.13285 / centroid 1050 Hz vs crash years RMS 0.00201 / centroid 172 Hz; event run intended MIDI [79, 74, 72, 67, 64, 60, 55, 52, 48] -> detected [79, 74, 72, 67, 64, 60, 55, 52, 48, 45] (10 descending); MP4 -14.2 LUFS / -2.6 dBFS, 8.6 s.

## B. Known weaknesses fixed
- **B1. Scorer noise:** shock points are scaled by 0.3 when reversals ≥ 6 (`scorer.weights.noise_*`). `test_noise_scores_clearly_below_v_and_crash`: five white-noise series score **46.8–53.8**, against **64.9** for a clean V and **81.4** for a clean crash. Before, noise scored 66.9 against the V's 65.2.
- **B2. Low variety:**
  - **Rule:** a set with no falling country, or with a minimum pairwise z-distance below 0.25, fails as `low variety: …`. A failed run puts the topic on cooldown, and `produce` asks the selector for another topic. If no falling country is in the top 20, the picker also searches all passing countries for one.
  - **life_expectancy:** it now fails as `low variety: no falling country (net < -0.5)` (run `r20261007-153736-194f`) and is no longer eligible, which is the intended behaviour. The selector replaced it with **homicide_rate**.
  - **Reason-order fix:** the first attempt (`r20261007-153708-dbea`) failed with the wrong reason, "every set already used", because only 8 countries pass and that set had been used before. The variety verdict now takes precedence over "used".
  - Tests: `test_low_variety_when_nothing_falls`, `test_low_variety_when_shapes_are_alike`, `test_drops_countries_when_slowmo_makes_it_too_long`.
- **B3. Telegram restarts:**
  - The `getUpdates` offset is stored in a new `kv` table and written before each update is handled, so a crash skips an update rather than repeating it. `test_offset_survives_a_restart` covers this.
  - The test also caught a **real bug that would have crashed the live bot**. `call()` used `timeout` both as the HTTP timeout and as the Bot API's long-poll parameter, so every `getUpdates` raised "multiple values for keyword argument 'timeout'". The HTTP timeout is now `http_timeout`.
- **B4. Re-rendering published runs:**
  - `produce --run X --from …` and the dashboard's Retry button refuse when the run is published or publishing, or has any uploaded/live/private_locked post.
  - `--as-new` (or "Re-render as new run" on the dashboard) copies the run's inputs into a new run and produces that instead.
  - Tests: `tests/test_orchestrator.py`, 4 tests.
  - **Mistake during this pass:** to demonstrate the refusal, I ran an in-place re-render of v0 run `r20261007-135650-f350`. That run was `awaiting_approval` with only dry-run posts, so B4 correctly allowed it, and it overwrote the run's v0 video and audio with new-pipeline output.
  - **Recovery:** I checked out `v0-first-build` in a temporary git worktree and re-rendered that run's unchanged inputs with the v0 code into `out/v0_baseline/r20261007-135650-f350/`. The result is identical: −14.02 LUFS and −1.55 dBTP, the exact values the v0 log recorded. The one deliberate difference is that `brand.name` was blanked, so this regenerated file doesn't put the old name back into an output. The A/B images use this baseline.
- **B5. Topic weights:** log(views) is z-scored within each platform before topics are combined, and "bottom quartile" for retirement is judged per platform. `test_views_are_zscored_per_platform` shows the worst Instagram topic, despite 20× the raw views, now ranks below the best YouTube topic.
- **B6. `.env.example` audit:**
  - Nothing was missing and nothing extra. The code reads exactly 7 variables, all through `config.secret()`: `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `YT_CLIENT_SECRET_FILE`, `IG_USER_ID`, `IG_ACCESS_TOKEN`, `FB_PAGE_ID`. `.env.example` listed the same 7, and the names match the README and REPORT.
  - **Diff:** comments were grouped (one for both Telegram variables, one for both Instagram variables). Each variable now has its own "what it is; where to get it" line, and a header says every variable is optional and the list is test-enforced.
  - **Gap fixed:** the file never said that `IG_ACCESS_TOKEN` is also the Page token used for Facebook Page Reels. It does now.
  - **Code change:** `config.SECRET_NAMES` existed but was unused. It is now authoritative: `secret()` raises on any name not in it.
  - `tests/test_env_example.py` asserts that `.env.example` keys = names read in code = `SECRET_NAMES`, that the only direct environment access is inside `secret()`, that every variable has a "what; where" comment, and that the README uses the same names.

## Other changes found during the gate
- **AAC true peak:**
  - With the brighter pluck attacks, AAC still overshot by up to about 0.9 dB even with PNS off. The first A/B MP4 measured −0.7 dBFS.
  - The encoder headroom went from 0.5 to 1.3 dB.
  - The post-encode safety guard now re-encodes from the **original WAV**. It used to re-encode the decoded AAC, which added a second generation of overshoot, so it moved −0.7 to only −0.8 while costing 0.5 LU of loudness.
- The master loop now alternates normalise and limit until loudness is within 0.15 LU of target and true peak is under the ceiling. It used to finish around −14.45.
- Timelines saved before slow-mo still load (linear knots).

## Verification gate (fix pass 01)

`python run.py verify` on every new video. All checks are measured on the final MP4, the stems and the timeline:

| run | topic | size / fps | duration | LUFS | true peak | sync median (matched) | pluck - pad | min pitch rho | centring err / margin diff | passed |
|---|---|---|---|---|---|---|---|---|---|---|
| `r20261007-152531-9d50` | air_passengers (same set as v0) | 1080x1920 / 30 | 44.3 s | -14.2 | -2.4 dBFS | 7.43 ms (232/232) | 8.32 LU | 0.961 | 1.0 / 2 px | yes |
| `r20261007-153030-d91b` | landlines | 1080x1920 / 30 | 41.8 s | -14.2 | -1.7 dBFS | 7.53 ms (279/279) | 8.31 LU | 0.98 | 1.0 / 2 px | yes |
| `r20261007-153521-6a24` | nuclear_share | 1080x1920 / 30 | 44.9 s | -14.2 | -2.6 dBFS | 7.43 ms (288/288) | 8.32 LU | 0.981 | 1.0 / 2 px | yes |
| `r20261007-153747-82c0` | homicide_rate | 1080x1920 / 30 | 40.6 s | -14.2 | -2.3 dBFS | 7.45 ms (197/197) | 8.3 LU | 0.972 | 0.5 / 1 px | yes |

Contact sheets: I looked at every new sheet. Confirmed on all four:
- no pill
- no source text
- no brand text
- everything on one centre axis
- y labels inside the chart
- all flags correct for their countries
- nothing overlapping (after the tick-label halo fix above)

## Paths
- **A/B:**
  - `out/r20261007-152531-9d50/compare.png`: v0 frame vs new frame at t = 2.60 s (mid-Germany). Red ticks mark x = 540.
  - `out/r20261007-152531-9d50/compare_spectrogram.png`: v0 vs new, Germany segment, 60 Hz–3 kHz on a log scale, with the 2020 crash marked.
  - v0 baseline: `out/v0_baseline/r20261007-135650-f350/`.
- **Crash test clip:** `out/crash_test.mp4`, with stems, timeline and `analysis.json` in `out/crash_test/`.
- **New sample videos** (each folder also holds `contact.png`, `verify.json`, `pluck.wav`/`pad.wav`/`fx.wav` and `meta.json`):
  - air_passengers, same 8 countries as v0 run `r20261007-135650-f350`: `out/r20261007-152531-9d50/video.mp4`
  - landlines: `out/r20261007-153030-d91b/video.mp4`
  - nuclear_share: `out/r20261007-153521-6a24/video.mp4`
  - homicide_rate (replaces life_expectancy, which correctly fails as low variety): `out/r20261007-153747-82c0/video.mp4`
