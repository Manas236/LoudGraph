# Graphony on the server

Linux (Ubuntu 22.04 or 24.04), user `ubuntu`, project in `/home/ubuntu/graphony`. Python 3.10 to 3.12.
The channel runs on autopilot (`AUTOPILOT=true`, the default): no approvals, no Telegram bot. Telegram only
receives alerts, and only when something breaks.

## Install

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip ffmpeg git sqlite3
cd /home/ubuntu
git clone graphony_main.bundle graphony          # from the migration zip (or: git clone https://github.com/Manas236/LoudGraph graphony)
cd graphony && git checkout main && git remote set-url origin https://github.com/Manas236/LoudGraph.git
# copy from the zip: .env, tokens/, pipeline.db, cache/, out/  (check them against MANIFEST.txt with sha256sum)
test ! -e MIGRATED_TO_SERVER                     # must NOT exist here: it stops all posting
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                    # the full suite passes
.venv/bin/python run.py doctor                   # ffmpeg, fonts, credentials
.venv/bin/python -m pipeline post --dry-run --day tomorrow   # tomorrow's two picks, nothing posted
```

In `.env` set `HEALTHCHECK_URL` (a healthchecks.io check, period 1 day, grace 3 h; it is pinged after every
post). `AUTOPILOT` empty or `true`. `DASHBOARD_PORT` only if 5055 is taken.

With 1 GB of RAM, add swap first (generation is capped at 1.5 GB):
`sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`
(and add `/swapfile none swap sw 0 0` to `/etc/fstab`).

## Start

```bash
sudo cp deploy/graphony-*.service deploy/graphony-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now graphony-generate.timer graphony-post-1.timer graphony-post-2.timer \
     graphony-insights.timer graphony-health.timer graphony-dashboard.service
systemctl list-timers 'graphony*'
```

| Job | UTC | IST |
|---|---|---|
| generate (top up the pool) | 21:30 | 03:00 |
| health (token, disk, pool, insights) | 03:30 | 09:00 |
| post slot 1 | 09:00 | 14:30 |
| post slot 2 | 10:30 | 16:00 |
| insights | every 6 h at :15 | |

The timers are `Persistent=true`: a job missed while the server was off runs at boot, but a post job more
than 90 min after its slot (or within 60 min of the last post) is refused and sends an alert.

## Look at it (only when an alert says so)

- Logs: `journalctl -u graphony-post-1 -n 100`, or `cache/logs/pipeline.log`.
- Dashboard: `ssh -L 5055:127.0.0.1:5055 ubuntu@<server>`, then http://127.0.0.1:5055 (Autopilot page:
  every pick with its score breakdown).
- Run a job by hand: `sudo systemctl start graphony-insights` (or `.venv/bin/python -m pipeline health`).
- Back up the database while running: `sqlite3 pipeline.db ".backup backups/pipeline-$(date +%F).db"`.
  Also keep `topics_auto.yaml` (the categories the autopilot discovered), `.env` and `tokens/`.

## Update the code

```bash
cd /home/ubuntu/graphony && git pull && .venv/bin/pip install -r requirements.txt
sudo cp deploy/graphony-*.service deploy/graphony-*.timer /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl restart graphony-dashboard
```
