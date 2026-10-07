"""SQLite state store (WAL). The run state machine lives here; nothing else holds hidden state."""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import path

STAGES = [
    "queued", "data_ready", "picked", "labelled", "rendered",
    "awaiting_approval", "approved", "rejected", "publishing", "published", "failed",
]
# Forward edges of the state machine. Any stage may also go to "failed".
NEXT = {
    "queued": {"data_ready"},
    "data_ready": {"picked"},
    "picked": {"labelled"},
    "labelled": {"rendered"},
    "rendered": {"awaiting_approval"},
    "awaiting_approval": {"approved", "rejected"},
    "approved": {"publishing"},
    "publishing": {"published"},
    "published": set(),
    "rejected": set(),
    "failed": set(),
}
POST_STATUSES = {"pending", "uploaded", "live", "private_locked", "failed", "dry_run"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    failed_stage TEXT,
    error TEXT,
    countries TEXT,
    country_set TEXT,
    score REAL,
    regen_of TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_stage ON runs(stage);
CREATE INDEX IF NOT EXISTS idx_runs_topic ON runs(topic_id);
CREATE TABLE IF NOT EXISTS stage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_run ON stage_log(run_id);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    remote_id TEXT,
    url TEXT,
    message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, platform)
);
CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    views INTEGER,
    avg_view_pct REAL,
    reach INTEGER,
    likes INTEGER,
    comments INTEGER,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_stats_post ON stats(post_id);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_weights (
    topic_id TEXT PRIMARY KEY,
    weight REAL,
    n_posts INTEGER NOT NULL DEFAULT 0,
    mean_log_views REAL,
    retired INTEGER NOT NULL DEFAULT 0,
    retired_reason TEXT,
    updated_at TEXT NOT NULL
);
"""


class TransitionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(path("db"), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init() -> None:
    with db() as c:
        c.executescript(SCHEMA)


# ---------------------------------------------------------------- runs

def new_run_id() -> str:
    return "r" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def create_run(topic_id: str, regen_of: str | None = None) -> str:
    init()
    rid = new_run_id()
    ts = now()
    with db() as c:
        c.execute(
            "INSERT INTO runs(id, topic_id, stage, regen_of, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (rid, topic_id, "queued", regen_of, ts, ts),
        )
        msg = f"created for topic {topic_id}" + (f" (regenerate of {regen_of})" if regen_of else "")
        c.execute("INSERT INTO stage_log(run_id, stage, message, ts) VALUES (?,?,?,?)", (rid, "queued", msg, ts))
    return rid


def get_run(run_id: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _run_dict(r) if r else None


def _run_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["countries"] = json.loads(d["countries"]) if d.get("countries") else []
    return d


def list_runs(stage: str | None = None, topic_id: str | None = None, limit: int = 500) -> list[dict]:
    q, args = "SELECT * FROM runs WHERE 1=1", []
    if stage:
        q += " AND stage=?"
        args.append(stage)
    if topic_id:
        q += " AND topic_id=?"
        args.append(topic_id)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with db() as c:
        return [_run_dict(r) for r in c.execute(q, args).fetchall()]


def log(run_id: str, stage: str, message: str) -> None:
    with db() as c:
        c.execute("INSERT INTO stage_log(run_id, stage, message, ts) VALUES (?,?,?,?)", (run_id, stage, message, now()))


def get_log(run_id: str) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM stage_log WHERE run_id=? ORDER BY id", (run_id,)).fetchall()]


def has_log(run_id: str, needle: str) -> bool:
    with db() as c:
        r = c.execute("SELECT 1 FROM stage_log WHERE run_id=? AND message LIKE ? LIMIT 1", (run_id, f"%{needle}%")).fetchone()
    return r is not None


def transition(run_id: str, new_stage: str, message: str = "", *, reset: bool = False, **fields) -> None:
    """Move a run to `new_stage`, validating the edge, and log it.

    reset=True allows going back to an earlier stage (used by retry-from-stage).
    Extra keyword fields (countries, score, country_set, error, failed_stage) are written too.
    """
    if new_stage not in STAGES:
        raise TransitionError(f"unknown stage {new_stage}")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT stage FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise TransitionError(f"no run {run_id}")
            cur = row["stage"]
            ok = new_stage == "failed" or new_stage in NEXT[cur] or reset
            if not ok:
                raise TransitionError(f"{run_id}: illegal transition {cur} -> {new_stage}")
            sets, args = ["stage=?", "updated_at=?"], [new_stage, now()]
            if new_stage != "failed":
                sets += ["failed_stage=NULL", "error=NULL"]
            for k, v in fields.items():
                if k == "countries":
                    v = json.dumps(v)
                sets.append(f"{k}=?")
                args.append(v)
            args.append(run_id)
            c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", args)
            prefix = f"reset from {cur}. " if reset and new_stage not in NEXT[cur] else ""
            c.execute(
                "INSERT INTO stage_log(run_id, stage, message, ts) VALUES (?,?,?,?)",
                (run_id, new_stage, prefix + (message or f"{cur} -> {new_stage}"), now()),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def claim(run_id: str, from_stage: str, to_stage: str, message: str = "") -> bool:
    """Atomically move from_stage -> to_stage. Returns False if another process got there first."""
    with db() as c:
        cur = c.execute(
            "UPDATE runs SET stage=?, updated_at=? WHERE id=? AND stage=?",
            (to_stage, now(), run_id, from_stage),
        )
        if cur.rowcount != 1:
            return False
        c.execute(
            "INSERT INTO stage_log(run_id, stage, message, ts) VALUES (?,?,?,?)",
            (run_id, to_stage, message or f"{from_stage} -> {to_stage}", now()),
        )
    return True


def fail(run_id: str, stage: str, error: str) -> None:
    transition(run_id, "failed", f"failed at {stage}: {error}", failed_stage=stage, error=error[:2000])


def used_country_sets(topic_id: str, except_run: str | None = None) -> set[str]:
    """Country sets already used for a topic by other runs (re-running a run may keep its own set)."""
    with db() as c:
        rows = c.execute(
            "SELECT country_set FROM runs WHERE topic_id=? AND country_set IS NOT NULL AND id IS NOT ?",
            (topic_id, except_run),
        ).fetchall()
    return {r["country_set"] for r in rows}


# ---------------------------------------------------------------- posts

def upsert_post(run_id: str, platform: str, status: str, remote_id: str | None = None,
                url: str | None = None, message: str | None = None) -> None:
    if status not in POST_STATUSES:
        raise ValueError(f"bad post status {status}")
    ts = now()
    with db() as c:
        c.execute(
            """INSERT INTO posts(run_id, platform, status, remote_id, url, message, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id, platform) DO UPDATE SET
                 status=excluded.status,
                 remote_id=COALESCE(excluded.remote_id, posts.remote_id),
                 url=COALESCE(excluded.url, posts.url),
                 message=excluded.message,
                 updated_at=excluded.updated_at""",
            (run_id, platform, status, remote_id, url, message, ts, ts),
        )
        c.execute(
            "INSERT INTO stage_log(run_id, stage, message, ts) VALUES (?,?,?,?)",
            (run_id, "publishing", f"{platform}: {status}" + (f" ({message})" if message else ""), ts),
        )


def get_posts(run_id: str | None = None) -> list[dict]:
    with db() as c:
        if run_id:
            rows = c.execute("SELECT * FROM posts WHERE run_id=? ORDER BY platform", (run_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def uploads_today(platform: str) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db() as c:
        r = c.execute(
            "SELECT COUNT(*) n FROM posts WHERE platform=? AND status IN ('uploaded','live','private_locked') "
            "AND substr(created_at,1,10)=?",
            (platform, today),
        ).fetchone()
    return r["n"]


# ---------------------------------------------------------------- stats + weights

def add_stats(post_id: int, views=None, avg_view_pct=None, reach=None, likes=None, comments=None, raw=None) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO stats(post_id, fetched_at, views, avg_view_pct, reach, likes, comments, raw) VALUES (?,?,?,?,?,?,?,?)",
            (post_id, now(), views, avg_view_pct, reach, likes, comments, json.dumps(raw) if raw is not None else None),
        )


def latest_stats() -> list[dict]:
    """Latest stats row per post joined with its post and run."""
    with db() as c:
        rows = c.execute(
            """SELECT p.id post_id, p.run_id, p.platform, p.status, p.url, p.remote_id, p.created_at posted_at,
                      r.topic_id, s.views, s.avg_view_pct, s.reach, s.likes, s.comments, s.fetched_at
               FROM posts p JOIN runs r ON r.id = p.run_id
               LEFT JOIN stats s ON s.id = (SELECT MAX(id) FROM stats WHERE post_id = p.id)
               ORDER BY p.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def set_topic_weight(topic_id: str, weight: float | None, n_posts: int, mean_log_views: float | None,
                     retired: bool = False, retired_reason: str | None = None) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO topic_weights(topic_id, weight, n_posts, mean_log_views, retired, retired_reason, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(topic_id) DO UPDATE SET weight=excluded.weight, n_posts=excluded.n_posts,
                 mean_log_views=excluded.mean_log_views, retired=excluded.retired,
                 retired_reason=excluded.retired_reason, updated_at=excluded.updated_at""",
            (topic_id, weight, n_posts, mean_log_views, int(retired), retired_reason, now()),
        )


def topic_weights() -> dict[str, dict]:
    init()
    with db() as c:
        return {r["topic_id"]: dict(r) for r in c.execute("SELECT * FROM topic_weights").fetchall()}


# ---------------------------------------------------------------- small persistent settings

def kv_get(key: str, default: str | None = None) -> str | None:
    init()
    with db() as c:
        r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def kv_set(key: str, value: str) -> None:
    init()
    with db() as c:
        c.execute("INSERT INTO kv(key, value, updated_at) VALUES (?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  (key, value, now()))
