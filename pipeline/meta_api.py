"""The one place a request to Meta is sent from (Graph API and rupload, for Instagram and Facebook).

Rate limit: before a request goes out, the time of the previous one is checked, and this one waits
until `meta_api.min_gap_seconds` have passed since it. That time is kept in the database, so the
gap holds across every process: two publishers, the bot and the dashboard's account checks share it.

Each caller takes the next free moment inside one short transaction and then sleeps until that
moment without holding anything: callers queue up in order, and one that dies blocks nobody.
"""
from __future__ import annotations

import logging
import sqlite3
import time

import requests

from . import config, db

log = logging.getLogger(__name__)
K_NEXT = "meta_api:next_free"   # epoch seconds: the earliest moment the next request may be sent
DEFAULT_GAP_S = 3.0
MAX_WAIT_S = 120.0              # a mark that is far ahead (clock change, callers that died) holds nobody longer


def min_gap() -> float:
    """Seconds between two requests. config.get_config is looked up on each call: 0 turns the limit off."""
    return float((config.get_config().get("meta_api") or {}).get("min_gap_seconds", DEFAULT_GAP_S))


def reserve(now: float) -> float:
    """The moment (epoch seconds) this request may be sent: `now`, or one gap after the request
    before it. The mark is moved one gap past that moment for the next caller, in any process."""
    gap = min_gap()
    if gap <= 0:
        return now
    with db.db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT value FROM kv WHERE key=?", (K_NEXT,)).fetchone()
            try:
                free = float(row["value"]) if row else 0.0
            except (TypeError, ValueError):
                free = 0.0
            turn = max(now, min(free, now + MAX_WAIT_S))
            c.execute("INSERT INTO kv(key, value, updated_at) VALUES (?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                      (K_NEXT, repr(turn + gap), db.now()))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return turn


def wait_turn() -> float:
    """Block until this request may be sent. Returns the seconds waited."""
    now = time.time()
    try:
        wait = reserve(now) - now
    except sqlite3.Error as e:   # the limit must never be the reason a post fails: keep the gap locally
        wait = min_gap()
        log.warning("Meta API: could not read the time of the last request (%s); waiting %.1fs", e, wait)
    if wait > 0:
        if wait > min_gap() + 0.5:   # more than one gap: other requests are queued ahead of this one
            log.info("Meta API: %.0fs wait, other requests are ahead (at least %.0fs between requests)", wait, min_gap())
        time.sleep(wait)
    return max(wait, 0.0)


def get(url: str, **kwargs) -> requests.Response:
    wait_turn()
    return requests.get(url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    wait_turn()
    return requests.post(url, **kwargs)
