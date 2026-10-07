# Data-sonification Shorts/Reels pipeline

Turns real public statistics (World Bank WDI, Our World in Data) into 30–45 s vertical videos:
each country's line chart is drawn while its data plays as music (one plucked note per year,
pitch follows the value). A human approves each video on Telegram or the local dashboard, then it
is posted to YouTube Shorts and Instagram Reels, and analytics flow back into topic selection.

Everything runs on a weak laptop (i5, 8 GB, no GPU): one render at a time, frames are streamed
straight into FFmpeg, nothing is held in memory or written per frame.

The on-screen video carries no channel name and no source line: the hook title and the data
credit (CC BY 4.0) go into the post title/description. `brand.name` in `config.yaml` is empty by
default; set it only if you want an on-screen watermark.

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
| `python run.py bot` | long-running: Telegram approvals + publishes approved runs |
| `python run.py publish [--run ID]` | publish approved runs now |
| `python run.py stats` | daily: pull views / avg % viewed, recompute topic weights, send daily summary |
| `python run.py dashboard` | local web UI on http://127.0.0.1:5055 |
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
| notify | `approve_telegram.py` | (Telegram message) |
| publish | `publish_youtube.py`, `publish_instagram.py` | `publish_<platform>.json` in dry-run |

All state lives in SQLite (`pipeline.db`, WAL): `runs`, `stage_log`, `posts`, `stats`,
`topic_weights`, plus `kv` (the Telegram `getUpdates` offset, so a bot restart never re-delivers a
button press). The dashboard and the bot only read/write that state, and their buttons call the
same functions (`pipeline/actions.py`).

- **Scorer** (`score.py`): rejects short, gappy, interpolated/flat, low-swing and negligible series;
  scores swing, zigzag reversals, biggest single-year shock and smoothness (weights in `config.yaml`).
  A series with 6+ reversals is noise: its shock points are scaled by 0.3.
- **Picker**: 6–8 countries (fills 30–45 s), maximises shape diversity, wants one rising, one falling
  and one multi-reversal series, plays the highest score last, never repeats a (topic, country set).
  A set with no falling country or a minimum pairwise shape distance below 0.25 fails as
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
Publishing stays in dry-run until you set `dry_run.<platform>: false` in `config.yaml`.
`.env.example` lists exactly the variables the code reads (checked by `tests/test_env_example.py`).

**GEMINI_API_KEY** (labels) – create a key at https://aistudio.google.com/apikey.
On first use the pipeline lists available models and picks a flash-class one (logged to
`cache/gemini_model.json`); pin one with `gemini.model` in config if you prefer.

**TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID** (approval) – talk to @BotFather → `/newbot` → token.
Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
`message.chat.id`. Only that chat can approve.

**YT_CLIENT_SECRET_FILE** (YouTube) – in Google Cloud Console: create a project, enable *YouTube Data API v3*
and *YouTube Analytics API*, configure the OAuth consent screen (External, add your Google account as
a test user), create *OAuth client ID → Desktop app*, download the JSON to `tokens/client_secret.json`
and set `YT_CLIENT_SECRET_FILE=tokens/client_secret.json`. Run `python -m pipeline.publish_youtube --auth`
once on a machine with a browser (copy `tokens/youtube_token.json` to the server afterwards).
Uploads from an unverified API project are forced to *private*; the pipeline detects this
(`private_locked`) and alerts you. Request the YouTube API audit to lift it.
An upload costs a large share of the default 10,000-unit daily quota; `youtube.max_uploads_per_day` caps it.

**IG_USER_ID / IG_ACCESS_TOKEN** (Instagram Reels) – an Instagram professional account linked to a
Facebook Page and a Meta app using *Instagram API with Facebook Login* with `instagram_basic`,
`instagram_content_publish`, `instagram_manage_insights`, `pages_read_engagement`. `IG_ACCESS_TOKEN`
is a long-lived **Page** access token; `IG_USER_ID` is the Page's `instagram_business_account.id`
(`GET /{page-id}?fields=instagram_business_account`). Videos go up with the resumable upload flow
(local file, no public host needed).

**FB_PAGE_ID** (optional Facebook Page Reels) – also grant `pages_show_list` and `pages_manage_posts`,
set `FB_PAGE_ID` and `facebook.enabled: true`; the same Page token (`IG_ACCESS_TOKEN`) is used.

## Scheduling

Default cadence: 2 videos/day (`cadence.videos_per_day`). Paths and names below are placeholders.

### Windows (Task Scheduler)

```bat
set P=C:\path\to\pipeline
schtasks /Create /TN "Shorts pipeline produce" /SC DAILY /ST 08:00 /TR "\"%P%\.venv\Scripts\python.exe\" \"%P%\run.py\" produce"
schtasks /Create /TN "Shorts pipeline stats"   /SC DAILY /ST 21:00 /TR "\"%P%\.venv\Scripts\python.exe\" \"%P%\run.py\" stats"
schtasks /Create /TN "Shorts pipeline bot"     /SC ONLOGON          /TR "\"%P%\.venv\Scripts\pythonw.exe\" \"%P%\run.py\" bot"
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
per-platform analytics, the `.env.example` audit, and that the old channel name appears nowhere.

## Data licences

World Bank WDI and Our World in Data are CC BY 4.0; every description credits the source and the
indicator. Inter is SIL OFL 1.1 (`assets/fonts/OFL.txt`); flag-icons is MIT (`assets/flags/LICENSE`).
