# Graphony

Graphony is a data-sonification Shorts/Reels pipeline. It turns real public statistics (World Bank WDI, Our World in Data) into 30–45 s vertical videos:
each country's line chart is drawn while its data plays as music (one plucked note per year,
pitch follows the value). A human approves each video on Telegram or the local dashboard, then it
is posted to Instagram Reels and Facebook Page Reels (YouTube Shorts is supported and currently
turned off), and analytics flow back into topic selection.

Everything runs on a weak laptop (i5, 8 GB, no GPU): one render at a time, frames are streamed
straight into FFmpeg, nothing is held in memory or written per frame.

The on-screen video carries no channel name and no source line: the hook title and the data
credit (CC BY 4.0) go into the post title/description. `brand.name: "Graphony"` in `config.yaml` is
the product name shown in the dashboard, Telegram messages and `doctor`; it is drawn on the video
only if you also set `render.watermark: true` (default `false`).

## Setup

Requirements: Python 3.11+, FFmpeg on PATH. Same steps on Windows and Linux.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in what you have; everything works in dry-run without it
python run.py doctor          # checks ffmpeg, fonts, flags, dirs, credentials, Gemini model
python run.py topics-verify   # fetches every topic, comments out any that cannot make a video
```

Fonts (Inter, OFL) and flags (flag-icons, MIT) are vendored in `assets/`.

## Commands

| command | what it does |
|---|---|
| `python run.py doctor` | environment + credential checks (never changes anything) |
| `python run.py fetch [--topic ID] [--force]` | refresh cached series (`cache/series/`, re-fetched after `cache_days`) |
| `python run.py topics-verify` | verify every topic in `topics.yaml`; failures are commented out with a reason |
| `python run.py produce [--count N] [--topic ID]` | choose topics and take each run to `awaiting_approval` (default N = `cadence.videos_per_day`) |
| `python run.py produce --run ID --from render` | re-run one run from a stage (`fetch`, `pick`, `label`, `render`, `notify`); refused for published runs or runs with a live post |
| `python run.py produce --run ID --from render --as-new` | copy the run's inputs into a NEW run and produce that (the way to re-render a published run) |
| `python run.py bot` | long-running: the Telegram review queue and commands, scheduled videos, publishes approved runs |
| `python run.py go-live [--check-only]` | read-only Instagram + Facebook checks; for those that pass, turn posting on and test mode off (old test-mode approvals go back to review first) |
| `python run.py publish [--run ID]` | publish approved runs now |
| `python run.py stats` | daily: pull views / avg % viewed, recompute topic weights, send daily summary |
| `python run.py dashboard` | local web UI on http://127.0.0.1:5055 (Review, Library, Settings) |
| `python run.py verify [--run ID ...]` | ffprobe, loudness, A/V sync, stem balance, pitch-vs-data, pixel centring, contact sheet |
| `python run.py crash-test` | `out/crash_test.mp4`: a synthetic flat-crash-recover series, to hear the event treatment |
| `python run.py compare --old ID --new ID` | old vs new frame (`compare.png`) and spectrogram (`compare_spectrogram.png`) |
| `python -m pipeline.publish_youtube --auth` | one-time YouTube OAuth (opens a browser) |

## How a video is made

```
queued -> data_ready -> picked -> labelled -> rendered -> awaiting_approval -> approved | rejected
       -> publishing -> published            (any stage -> failed, with stage + error)
```

| stage | module | writes to `out/<run_id>/` |
|---|---|---|
| fetch | `pipeline/fetch.py`, `score.py` | `data.json`, `scores.json` |
| pick | `pipeline/picker.py` | `pick.json` |
| label | `pipeline/labels.py` (Gemini, optional) | `labels.json` |
| render | `timeline.py`, `audio.py`, `render.py` | `timeline.json`, `audio.wav`, `pluck.wav`, `pad.wav`, `fx.wav`, `video.mp4`, `meta.json`, `thumb.jpg` |
| notify | `review.py` | (joins the review queue; the bot sends one card at a time) |
| publish | `publish_youtube.py`, `publish_instagram.py` | `publish_<platform>.json` in dry-run |

All state lives in SQLite (`pipeline.db`, WAL): `runs` (incl. live render `progress` and the review
`queue_pos`), `review_cards` (the Telegram card on screen), `make_jobs` (`/new` requests), `stage_log`,
`posts`, `stats`, `topic_weights`, plus `kv` (the Telegram `getUpdates` offset, so a bot restart never
re-delivers a button press; the bot heartbeat; cached health checks). The dashboard and the bot only
read/write that state, and their buttons call the same functions (`pipeline/actions.py`).

### Telegram bot

Double-click **Bot.bat** and leave it open. The bot only listens to the chat in `TELEGRAM_CHAT_ID`;
messages and buttons from anyone else are ignored.

**The review queue, in plain words.** Every finished video waits in one queue, oldest first. The
bot shows you **one video at a time**: a review card with the video and four short lines (title,
what it measures, the countries, the length) and three buttons:

- **✅ Approve**: the same message then updates itself while it posts: Approved, Instagram
  uploading, Instagram posted (with the link), Facebook uploading, Facebook posted (with the link),
  Done. If a platform fails, or a step takes longer than 10 minutes, the card says so in a few words
  and shows a **🔁 Retry** button for that platform.
- **❌ Reject**: the video is dropped (it stays in the dashboard Library under Rejected).
- **⏭ Later**: the video goes to the back of the queue. If it is the only one waiting, it rests for
  15 minutes instead of coming straight back.

The next card arrives only after you decide on the current one and, if you approved it, after its
posting has finished (posted or failed). Approving or rejecting in the dashboard counts too: the card
says "in the dashboard" and the queue moves on. A video is never posted twice, even if you approve it
in both places or tap a button twice. Buttons on older messages answer "This card is stale" and do
nothing. If the bot restarts, it carries on with the same card instead of sending it again.

**Commands** (also in the bot's menu):

| command | what it does |
|---|---|
| `/new` | offers 3 topics that have no video yet (the most interesting by the data scorer) and a Random button. Tap one and a status message follows it: Fetching data, Rendering, Ready. A ready video joins the review queue. Only one video renders at a time: a pick made while another renders waits its turn, and the message says so. If the topic turns out too boring (every country moved the same way), the message says that plainly. |
| `/ready` | lists up to 8 finished videos that are not posted yet, newest first. Tap one to make it the next card. |
| `/status` | one message: what is rendering, the open card, how many videos wait, and whether test mode is on. |
| `/pause`, `/resume` | stop or restart sending review cards. The open card keeps working, and the pause survives a restart. |
| `/help` | the list of commands. |

Message edits are limited to one every 3 seconds per message, and the bot waits whenever Telegram
asks it to slow down (HTTP 429).

### Dashboard

Double-click **Dashboard.bat**: it starts the dashboard in its window and opens
http://127.0.0.1:5055 in your browser as soon as it answers (if it is already running, it only opens
the browser). `python run.py dashboard` and `python dashboard/app.py` start the same server, and
`.vscode/launch.json` has *Dashboard* and *Bot* configurations. **Bot.bat** starts `run.py bot`; a
second copy notices the first and exits. Everything is local; no frontend build or internet
connection is needed for the dashboard.

- **Review** (`/`): one large, playable video, with **Approve & post**, **Remake** and **Reject**.
  Use A / M / X, or left/right arrows to move through the thumbnail queue. Click the hook title
  and press Enter to save it to the post title and captions. Platform chips choose where this
  video posts. Disconnected accounts are disabled; you can still approve and the video waits in
  Library. The bot posts waiting approvals after you connect the accounts, or use **Post now**
  in the Library drawer. Remake keeps the topic, chooses new countries and marks the old version
  replaced. A progress card refreshes every two seconds. Only actual errors need attention.
- **Library** (`/library`): Posted, Approved, not posted, and Rejected tabs. Thumbnails show the
  middle of the first country's chart. Open a card for the player, platform stats, live links and
  a targeted retry when an upload failed. Test-only posts remain in Approved, not posted.
- **Settings** (`/settings`): account connection instructions and individual doctor checks,
  platform on/off and test-mode switches, daily video count and times, and topic controls.
  YAML changes are backed up as `config.yaml.bak-<timestamp>` or `topics.yaml.bak-<timestamp>`;
  ruamel.yaml preserves comments. Secrets are edited in `.env` outside the dashboard. Restart
  Dashboard.bat and Bot.bat after changing secrets, then use the account's **Test** button.
  Heartbeat, upload quota, schedule and logs are under **Advanced**.

The single status line prioritizes a stopped configured Telegram bot, failed uploads, test mode,
missing posting accounts, then All good. Selection skips are recorded as topic events, not failed
videos. Production tries up to five topics per requested video before asking you to review topics.
The migration retains old picker-failed runs and logs, but removes them from failure lists.

**Posting times** use `cadence.timezone` (Asia/Kolkata by default). Keep **Bot.bat** running:
it makes the configured daily number of videos across those times; videos only post after your
approval. A schedule is optional. Use either these times or a Task Scheduler produce task, to avoid
two batches. Settings changes are picked up by running processes without restarting them.

- **Scorer** (`score.py`): rejects short, gappy, interpolated/flat, low-swing and negligible series;
  scores swing, zigzag reversals, biggest single-year shock and smoothness (weights in `config.yaml`).
  A series with 6+ reversals is noise: its shock points are scaled by 0.3.
- **Picker**: 6–8 countries (fills 30–45 s), maximises shape diversity, wants one rising, one falling
  and one multi-reversal series, plays the highest score last, never repeats a (topic, country set).
  A set with no falling country or a minimum pairwise shape distance below 0.25 is skipped as
  **low variety** (the topic goes on cooldown and the selector picks another). If slow-mo events push
  the video past 45 s, the least interesting country is dropped.
- **Timeline** (`timeline.py`) is the single source of timing. A year whose move is at least 25% of the
  country's range is an **event**: the step into it lasts 3x longer, for both the video and the audio.
- **Audio** (three stems, mixed and kept for verification):
  `pluck` is the lead (Karplus-Strong, ~250 ms decay, 3 octaves of a minor-pentatonic scale per
  country key; a move of 15%+ of the range always changes the note by at least 2 steps; events play
  a fast scale run from the old note to the new one); `pad` is one detuned-by-3-cents note per
  country whose low-pass cutoff and gain follow the value, ducking to near silence after a fall;
  `fx` holds the whooshes, a short sub hit on falls and a bright accent on jumps (no sub-bass
  elsewhere). The pad sits 9 LU under the plucks. Master: −14 LUFS, ≤ −1 dBTP after AAC.
- **Layout** (`render.py`): one centre axis (x = 540) with symmetric 130 px margins; top to bottom:
  metric header, progress dots, flag + country, chart (tick labels inside the plot), year, value;
  the stack is vertically centred in the area above the bottom 20% (Shorts/Reels UI).
- **Labels**: code finds the turning point; Gemini may only name the event (2–4 words) and is
  rejected unless confident, short, within ±1 year and free of numbers. No label is fine.

## Credentials (`.env`)

Everything is optional; missing pieces fall back to dry-run / dashboard-only and `doctor` says so.
Each platform has its own on/off and test-mode switch (Settings → Posting, or `<platform>.enabled`
and `dry_run.<platform>` in `config.yaml`). In test mode nothing is posted: the request is written to
`out/<run>/publish_<platform>.json`. `python run.py go-live` runs the read-only Instagram and
Facebook checks and, for the ones that pass, turns posting on and test mode off, turns YouTube off,
and first sends every video approved during test mode back to review: nothing posts live without a
fresh approval.
`.env.example` lists exactly the variables the code reads (checked by `tests/test_env_example.py`).

**GEMINI_API_KEY** (labels) – create a key at https://aistudio.google.com/apikey.
On first use the pipeline lists available models and picks a flash-class one (logged to
`cache/gemini_model.json`); pin one with `gemini.model` in config if you prefer.

**TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID** (approval) – talk to @BotFather → `/newbot` → token.
Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
`message.chat.id`. Only that chat can approve.

**YT_CLIENT_SECRET_FILE** (YouTube) – in Google Cloud Console:
1. Create a project and enable *YouTube Data API v3* and *YouTube Analytics API*.
2. Configure the OAuth consent screen (*Google Auth Platform*): user type **External**.
3. Set its **publishing status to "In production"** (*Audience → Publish app*), **not "Testing"**. In
   Testing, Google expires the refresh token 7 days after you authorise, so uploads would stop after a
   week. You do not need Google's app verification: an unverified app in production is fine for a
   single owner. The one cost is a "Google hasn't verified this app" screen during `--auth`; click
   *Advanced → Go to … (unsafe)* once.
4. Create *OAuth client ID → Desktop app*, download the JSON to `tokens/client_secret.json` and set
   `YT_CLIENT_SECRET_FILE=tokens/client_secret.json`.
5. Run `python -m pipeline.publish_youtube --auth` once on a machine with a browser (copy
   `tokens/youtube_token.json` to the server afterwards). If the consent screen was in Testing when you
   authorised, switch it to In production and run `--auth` again: the old token still dies after 7 days.

`doctor` (and Settings account tests) refreshes the stored token on every check, so a dead
refresh token shows up as **expired** the same day, with this fix in the message. A publish with a
dead token is marked failed and sends a Telegram alert; it never stops silently.
Uploads from an API project that has not passed Google's YouTube API audit are forced to *private*;
the pipeline detects this (`private_locked`) and alerts you. Request the audit to lift it (that is a
different review from the consent screen's verification).
An upload costs a large share of the default 10,000-unit daily quota; `youtube.max_uploads_per_day` caps it.
The cap counts uploads since midnight Pacific time, when YouTube resets the quota.

**IG_USER_ID / IG_ACCESS_TOKEN** (Instagram Reels) – an Instagram professional account linked to a
Facebook Page and a Meta app using *Instagram API with Facebook Login* with `instagram_basic`,
`instagram_content_publish`, `instagram_manage_insights`, `pages_read_engagement`. `IG_ACCESS_TOKEN`
is a long-lived **Page** access token; `IG_USER_ID` is the Page's `instagram_business_account.id`
(`GET /{page-id}?fields=instagram_business_account`). Videos go up with the resumable upload flow
(local file, no public host needed).

**FB_PAGE_ID** (Facebook Page Reels) – the same Page token (`IG_ACCESS_TOKEN`) must also have
`pages_show_list`, `pages_read_engagement` and `pages_manage_posts`. Set `FB_PAGE_ID` and
`facebook.enabled: true`. Reels go up with the Page `video_reels` flow (start, upload, finish with
`video_state=PUBLISHED`), then the bot follows Facebook's processing until it is published. The
Facebook Test (and `go-live`) checks that the token is a Page token for that Page with all three
permissions, and names any that are missing.

## Scheduling

Default cadence: 2 videos/day (`cadence.videos_per_day`). Paths and names below are placeholders.

### Windows (Task Scheduler)

```bat
set P=C:\path\to\pipeline
schtasks /Create /TN "Graphony produce" /SC DAILY /ST 08:00 /TR "\"%P%\.venv\Scripts\python.exe\" \"%P%\run.py\" produce"
schtasks /Create /TN "Graphony stats"   /SC DAILY /ST 21:00 /TR "\"%P%\.venv\Scripts\python.exe\" \"%P%\run.py\" stats"
schtasks /Create /TN "Graphony bot"     /SC ONLOGON          /TR "\"%P%\.venv\Scripts\pythonw.exe\" \"%P%\run.py\" bot"
```

In the task's properties tick *Run whether user is logged on or not* for the daily jobs, and on the
bot task set *If the task fails, restart every 5 minutes*. Logs go to `cache/logs/pipeline.log`.
Start the dashboard when needed with `python run.py dashboard`.

### Linux (cron + systemd)

```cron
# crontab -e
0 7 * * *   cd /opt/shorts-pipeline && .venv/bin/python run.py produce >> cache/logs/cron.log 2>&1
0 21 * * *  cd /opt/shorts-pipeline && .venv/bin/python run.py stats   >> cache/logs/cron.log 2>&1
```

`/etc/systemd/system/shorts-pipeline-bot.service`:

```ini
[Unit]
Description=Shorts pipeline approval bot + publisher
After=network-online.target

[Service]
WorkingDirectory=/opt/shorts-pipeline
ExecStart=/opt/shorts-pipeline/.venv/bin/python run.py bot
Restart=always
RestartSec=30
User=pipeline

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now shorts-pipeline-bot`. Create `shorts-pipeline-dashboard.service` the same
way with `run.py dashboard`. Without systemd, run long jobs inside `screen` so they survive the SSH
session: `screen -dmS pipeline-bot .venv/bin/python run.py bot` (re-attach with `screen -r pipeline-bot`),
and do the same for a long manual `produce` run.

The dashboard binds to 127.0.0.1 only. From your laptop: `ssh -L 5055:127.0.0.1:5055 user@server`,
then open http://127.0.0.1:5055.

## Tests

```bash
python -m pytest
```

Covers the scorer on synthetic series (incl. noise vs V vs crash), picker constraints and low variety,
timeline maths incl. slow-mo, **pixel-measured centring** of rendered frames (both backends), safe
zones, audio (pitch-vs-data Spearman on the pluck stem, stem balance, crash darkens the pad, event
runs), label validation, the state machine, re-render refusal, Telegram offset persistence,
per-platform analytics, the `.env.example` audit, that the old channel name appears nowhere, that
`brand.name` alone never puts text on a frame (only `render.watermark` does), the
dashboard review queue, title and platform choices, skip recovery and migration, YAML backups,
CSRF, video Range requests, status priority, the connect steps naming every `.env` variable,
`python dashboard/app.py` run directly, the single-instance bot, and the bot heartbeat,
YouTube token-refresh detection and the Pacific-time quota day; and the Telegram review queue: one
open card at a time, the next card waiting for the decision and the posting, stale buttons, a double
approval posting once, Later and /ready ordering, /new topics and one render at a time, restart resume,
the 10-minute stage limit, edit throttling and 429, chat filtering, the test-mode reset before going
live, the Facebook permission check, and YouTube off skipping its upload.

## Data licences

World Bank WDI and Our World in Data are CC BY 4.0; every description credits the source and the
indicator. Inter is SIL OFL 1.1 (`assets/fonts/OFL.txt`); flag-icons is MIT (`assets/flags/LICENSE`).

### Dashboard screenshots

`out/dashboard_screens/v3/` holds desktop (1440x900) and phone (390x844) screenshots.
To reproduce them with headless Microsoft Edge, install the optional QA dependency with
`.venv\Scripts\python.exe -m pip install websocket-client`, then run
`.venv\Scripts\python.exe scripts\dashboard_screenshots.py`. The harness uses disposable SQLite
copies and disables subprocess actions. It never approves or publishes from the real database.
