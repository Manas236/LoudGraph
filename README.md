# LoudGraphs

Turns real public statistics (World Bank WDI, Our World in Data) into 30–45 s vertical videos:
each country's line chart is drawn while its data plays as music (one plucked note per year,
pitch follows the value). A human approves each video on Telegram or the local dashboard, then it
is posted to YouTube Shorts and Instagram Reels, and analytics flow back into topic selection.

Everything runs on a weak laptop (i5, 8 GB, no GPU): one render at a time, frames are streamed
straight into FFmpeg, nothing is held in memory or written per frame.

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
| `python run.py produce --run ID --from render` | re-run one run from a stage (`fetch`, `pick`, `label`, `render`, `notify`) |
| `python run.py bot` | long-running: Telegram approvals + publishes approved runs |
| `python run.py publish [--run ID]` | publish approved runs now |
| `python run.py stats` | daily: pull views / avg % viewed, recompute topic weights, send daily summary |
| `python run.py dashboard` | local web UI on http://127.0.0.1:5055 |
| `python run.py verify [--run ID ...]` | ffprobe + loudness + A/V sync check + contact sheet |
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
| render | `timeline.py`, `audio.py`, `render.py` | `timeline.json`, `audio.wav`, `video.mp4`, `meta.json`, `thumb.jpg` |
| notify | `approve_telegram.py` | (Telegram message) |
| publish | `publish_youtube.py`, `publish_instagram.py` | `publish_<platform>.json` in dry-run |

All state lives in SQLite (`loudgraphs.db`, WAL): `runs`, `stage_log`, `posts`, `stats`,
`topic_weights`. The dashboard and the bot only read/write that state, and their buttons call the
same functions (`pipeline/actions.py`).

- **Scorer** (`score.py`): rejects short, gappy, interpolated/flat, low-swing and negligible series;
  scores swing, zigzag reversals, biggest single-year shock and smoothness (weights in `config.yaml`).
- **Picker**: 6–8 countries (fills 30–45 s), maximises shape diversity, wants one rising, one falling
  and one multi-reversal series, plays the highest score last, never repeats a (topic, country set).
- **Timeline** (`timeline.py`) is the single source of timing: audio note onsets and the moment the
  line reaches each year both read from it.
- **Audio**: Karplus-Strong plucks on a minor-pentatonic scale per country key, detuned-saw pad,
  noise whoosh between countries, a chord of all keys on the end card; −14 LUFS, ≤ −1 dBTP after AAC.
- **Labels**: code finds the turning point; Gemini may only name the event (2–4 words) and is
  rejected unless confident, short, within ±1 year and free of numbers. No label is fine.

## Credentials (`.env`)

Everything is optional; missing pieces fall back to dry-run / dashboard-only and `doctor` says so.
Publishing stays in dry-run until you set `dry_run.<platform>: false` in `config.yaml`.

**Gemini** (labels) – create a key at https://aistudio.google.com/apikey → `GEMINI_API_KEY`.
On first use the pipeline lists available models and picks a flash-class one (logged to
`cache/gemini_model.json`); pin one with `gemini.model` in config if you prefer.

**Telegram** (approval) – talk to @BotFather → `/newbot` → token into `TELEGRAM_BOT_TOKEN`.
Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
`message.chat.id` into `TELEGRAM_CHAT_ID`. Only that chat can approve.

**YouTube** – in Google Cloud Console: create a project, enable *YouTube Data API v3* and
*YouTube Analytics API*, configure the OAuth consent screen (External, add your Google account as a
test user), create *OAuth client ID → Desktop app*, download the JSON to `tokens/client_secret.json`
and set `YT_CLIENT_SECRET_FILE=tokens/client_secret.json`. Run `python -m pipeline.publish_youtube --auth`
once on a machine with a browser (copy `tokens/youtube_token.json` to the server afterwards).
Note: uploads from an unverified API project are forced to *private*; the pipeline detects this
(`private_locked`) and alerts you. Request the YouTube API audit to lift it.
An upload costs a large share of the default 10,000-unit daily quota; `youtube.max_uploads_per_day` caps it.

**Instagram Reels** – needs an Instagram professional account linked to a Facebook Page and a Meta
app using *Instagram API with Facebook Login* with `instagram_basic`, `instagram_content_publish`,
`instagram_manage_insights`, `pages_read_engagement` (plus `pages_show_list`, `pages_manage_posts` for
optional Facebook Page Reels). Put a long-lived **Page** access token in `IG_ACCESS_TOKEN`, and the
`instagram_business_account.id` of that Page (`GET /{page-id}?fields=instagram_business_account`) in
`IG_USER_ID`. Videos are sent with the resumable upload flow (local file, no public host needed).
Optional Facebook Page Reels: set `FB_PAGE_ID` and `facebook.enabled: true`.

## Scheduling

Default cadence: 2 videos/day (`cadence.videos_per_day`). Paths below are examples.

### Windows (Task Scheduler)

```bat
set LG=C:\Users\you\Desktop\LoudGraphs
schtasks /Create /TN "LoudGraphs produce" /SC DAILY /ST 08:00 /TR "\"%LG%\.venv\Scripts\python.exe\" \"%LG%\run.py\" produce"
schtasks /Create /TN "LoudGraphs stats"   /SC DAILY /ST 21:00 /TR "\"%LG%\.venv\Scripts\python.exe\" \"%LG%\run.py\" stats"
schtasks /Create /TN "LoudGraphs bot"     /SC ONLOGON          /TR "\"%LG%\.venv\Scripts\pythonw.exe\" \"%LG%\run.py\" bot"
```

In the task's properties tick *Run whether user is logged on or not* for the daily jobs, and on the
bot task set *If the task fails, restart every 5 minutes*. Logs go to `cache/logs/pipeline.log`.
Start the dashboard when needed with `python run.py dashboard`.

### Linux (cron + systemd)

```cron
# crontab -e
0 7 * * *   cd /opt/loudgraphs && .venv/bin/python run.py produce >> cache/logs/cron.log 2>&1
0 21 * * *  cd /opt/loudgraphs && .venv/bin/python run.py stats   >> cache/logs/cron.log 2>&1
```

`/etc/systemd/system/loudgraphs-bot.service`:

```ini
[Unit]
Description=LoudGraphs approval bot + publisher
After=network-online.target

[Service]
WorkingDirectory=/opt/loudgraphs
ExecStart=/opt/loudgraphs/.venv/bin/python run.py bot
Restart=always
RestartSec=30
User=loudgraphs

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now loudgraphs-bot`. Create `loudgraphs-dashboard.service` the same way
with `run.py dashboard`. Without systemd, run long jobs inside `screen` so they survive the SSH
session: `screen -dmS lg-bot .venv/bin/python run.py bot` (re-attach with `screen -r lg-bot`), and do
the same for a long manual `produce` run.

The dashboard binds to 127.0.0.1 only. From your laptop: `ssh -L 5055:127.0.0.1:5055 user@server`,
then open http://127.0.0.1:5055.

## Tests

```bash
python -m pytest
```

Covers the scorer on synthetic series (flat, linear-interpolated, monotone, V, noisy, crash), picker
constraints, timeline maths, safe-zone constants and rendered text boxes, label validation, the
state machine, audio mastering, analytics/selector and the dashboard.

## Data licences

World Bank WDI and Our World in Data are CC BY 4.0; every description credits the source and the
indicator. Inter is SIL OFL 1.1 (`assets/fonts/OFL.txt`); flag-icons is MIT (`assets/flags/LICENSE`).
