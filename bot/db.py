from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Kind, Sent

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_cache (
    key        TEXT PRIMARY KEY,
    file_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    caption    TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    tg_id      INTEGER PRIMARY KEY,
    username   TEXT,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    banned     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER NOT NULL,
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    format      TEXT NOT NULL DEFAULT '',
    egress      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,
    error       TEXT,
    bytes       INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    started_at  INTEGER,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created_at);
"""


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    CACHED = "cached"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


def _now() -> int:
    return int(time.time())


def _day_start() -> int:
    now = _now()
    return now - now % 86400


class DB:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: Path) -> DB:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        await conn.commit()
        # Jobs left running by a previous process can never finish.
        await conn.execute(
            "UPDATE jobs SET status = ?, error = 'interrupted by restart', finished_at = ? "
            "WHERE status IN (?, ?)",
            (JobStatus.FAILED, _now(), JobStatus.QUEUED, JobStatus.RUNNING),
        )
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # users

    async def touch_user(self, tg_id: int, username: str | None) -> bool:
        """Upserts the user and returns whether they are banned."""
        now = _now()
        await self.conn.execute(
            "INSERT INTO users (tg_id, username, first_seen, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username, last_seen = excluded.last_seen",
            (tg_id, username, now, now),
        )
        await self.conn.commit()
        return await self.is_banned(tg_id)

    async def is_banned(self, tg_id: int) -> bool:
        async with self.conn.execute("SELECT banned FROM users WHERE tg_id = ?", (tg_id,)) as cur:
            row = await cur.fetchone()
        return bool(row and row["banned"])

    async def set_banned(self, tg_id: int, banned: bool) -> None:
        now = _now()
        await self.conn.execute(
            "INSERT INTO users (tg_id, first_seen, last_seen, banned) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET banned = excluded.banned",
            (tg_id, now, now, int(banned)),
        )
        await self.conn.commit()

    # media cache

    async def get_cache(self, key: str) -> Sent | None:
        async with self.conn.execute(
            "SELECT file_id, kind, size_bytes, caption FROM media_cache WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Sent(kind=Kind(row["kind"]), file_id=row["file_id"], size_bytes=row["size_bytes"], caption=row["caption"])

    async def cached_keys(self, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        marks = ",".join("?" * len(keys))
        async with self.conn.execute(f"SELECT key FROM media_cache WHERE key IN ({marks})", keys) as cur:
            return {row["key"] async for row in cur}

    async def put_cache(self, key: str, sent: Sent) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO media_cache (key, file_id, kind, size_bytes, caption, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, sent.file_id, str(sent.kind), sent.size_bytes, sent.caption, _now()),
        )
        await self.conn.commit()

    async def delete_cache(self, key: str) -> None:
        await self.conn.execute("DELETE FROM media_cache WHERE key = ?", (key,))
        await self.conn.commit()

    # jobs

    async def job_create(self, tg_id: int, source: str, url: str, fmt: str, egress: str = "",
                         status: str = JobStatus.QUEUED) -> int:
        cur = await self.conn.execute(
            "INSERT INTO jobs (tg_id, source, url, format, egress, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tg_id, source, url, fmt, egress, status, _now()),
        )
        await self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def job_started(self, job_id: int) -> None:
        await self.conn.execute(
            "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?", (JobStatus.RUNNING, _now(), job_id)
        )
        await self.conn.commit()

    async def job_finish(self, job_id: int, status: str, *, error: str | None = None, nbytes: int = 0) -> None:
        await self.conn.execute(
            "UPDATE jobs SET status = ?, error = ?, bytes = ?, finished_at = ? WHERE id = ?",
            (status, error, nbytes, _now(), job_id),
        )
        await self.conn.commit()

    async def stats_today(self) -> dict[str, Any]:
        since = _day_start()
        q = self.conn.execute

        async def scalar(sql: str, *args: Any) -> int:
            async with q(sql, args) as cur:
                row = await cur.fetchone()
            return row[0] if row else 0

        users_total = await scalar("SELECT COUNT(*) FROM users")
        users_today = await scalar("SELECT COUNT(*) FROM users WHERE last_seen >= ?", since)
        users_new = await scalar("SELECT COUNT(*) FROM users WHERE first_seen >= ?", since)
        async with q(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM jobs WHERE created_at >= ? GROUP BY status",
            (since,),
        ) as cur:
            by_status = {row["status"]: (row["n"], row["b"]) async for row in cur}
        async with q(
            "SELECT source, egress, COUNT(*) AS n FROM jobs WHERE created_at >= ? AND status IN (?, ?) "
            "GROUP BY source, egress ORDER BY n DESC",
            (since, JobStatus.FAILED, JobStatus.TIMEOUT),
        ) as cur:
            failures = [(row["source"], row["egress"], row["n"]) async for row in cur]
        return {
            "users_total": users_total,
            "users_today": users_today,
            "users_new": users_new,
            "jobs": {k: v[0] for k, v in by_status.items()},
            "bytes": sum(v[1] for v in by_status.values()),
            "failures": failures,
        }
