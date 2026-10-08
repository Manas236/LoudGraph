"""The bot owns the optional daily production times configured in Settings."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db
from .config import get_config


def local_now():
    return datetime.now(ZoneInfo(get_config()["cadence"].get("timezone", "Asia/Kolkata")))


def next_time(now=None):
    current = now or local_now()
    times = get_config()["cadence"].get("posting_times", [])
    options = []
    for value in times:
        hour, minute = map(int, value.split(":"))
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        options.append(candidate if candidate > current else candidate + timedelta(days=1))
    return min(options) if options else None


def tick(now=None):
    from . import actions
    current = now or local_now()
    cadence = get_config()["cadence"]
    times = sorted(cadence.get("posting_times", []))
    for index, value in enumerate(times):
        if current.strftime("%H:%M") != value:
            continue
        count = cadence["videos_per_day"] // len(times) + (index < cadence["videos_per_day"] % len(times))
        if not count:
            continue
        key = f"schedule:{current:%Y-%m-%d}:{value}"
        # A restart or two bot processes must not produce a second batch.
        with db.db() as c:
            claimed = c.execute("INSERT OR IGNORE INTO kv(key,value,updated_at) VALUES (?,'started',?)", (key, db.now())).rowcount
        if claimed:
            try:
                actions.spawn("produce", "--count", str(count))
            except Exception:
                with db.db() as c:
                    c.execute("DELETE FROM kv WHERE key=?", (key,))
                raise
