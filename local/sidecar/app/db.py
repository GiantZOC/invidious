"""SQLite storage for the sidecar.

The sidecar owns every row in this file. It never reads Invidious's Postgres.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    ucid       TEXT PRIMARY KEY,
    author     TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS videos (
    video_id           TEXT PRIMARY KEY,
    title              TEXT,
    ucid               TEXT,
    author             TEXT,
    length_seconds     INTEGER,
    published          INTEGER,
    views              INTEGER,
    live_now           INTEGER,
    premiere_timestamp INTEGER,
    source             TEXT,
    fetched_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS watches (
    video_id      TEXT PRIMARY KEY,
    first_seen_at INTEGER NOT NULL,
    source        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    src_video TEXT NOT NULL,
    dst_video TEXT NOT NULL,
    seen_at   INTEGER NOT NULL,
    PRIMARY KEY (src_video, dst_video)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    ucid       TEXT PRIMARY KEY,
    author     TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_items (
    video_id   TEXT PRIMARY KEY,
    ucid       TEXT,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    at          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_prefs (
    ucid    TEXT PRIMARY KEY,
    grp     TEXT,
    weight  REAL NOT NULL DEFAULT 1.0,
    muted   INTEGER NOT NULL DEFAULT 0,
    min_len INTEGER,
    max_len INTEGER
);

CREATE TABLE IF NOT EXISTS rules (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    pattern TEXT NOT NULL,
    at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_ucid ON videos (ucid);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst_video);
CREATE INDEX IF NOT EXISTS idx_watches_seen ON watches (first_seen_at);
CREATE INDEX IF NOT EXISTS idx_feedback_target ON feedback (target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_channel_prefs_grp ON channel_prefs (grp);
"""


class Database:
    """One connection guarded by a lock.

    FastAPI runs ``def`` endpoints in a threadpool and the collector runs on the
    event loop, so the connection is shared across threads. SQLite serialises
    writers itself; the lock keeps our own read-modify-write helpers consistent.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        parent = Path(self.path).parent
        parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    def query(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def query_one(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def scalar(self, sql: str, args: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, args)
        return row[0] if row is not None and len(row) > 0 else default

    def execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        rows = list(rows)
        with self._lock:
            cur = self.conn.executemany(sql, rows)
            self.conn.commit()
            return cur.rowcount

    # -- meta -------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
