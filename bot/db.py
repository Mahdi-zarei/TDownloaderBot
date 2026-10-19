from __future__ import annotations

import re
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
CREATE INDEX IF NOT EXISTS jobs_user ON jobs (tg_id, created_at);
"""

# Columns added after the first release; created on open when missing.
MIGRATIONS = {("users", "name"): "ALTER TABLE users ADD COLUMN name TEXT"}


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    CACHED = "cached"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


DELIVERED = (JobStatus.DONE, JobStatus.CACHED)
FAILED = (JobStatus.FAILED, JobStatus.TIMEOUT)

USER_SORTS = {
    "recent": "u.last_seen DESC",
    "jobs": "delivered DESC, u.last_seen DESC",
    "bytes": "bytes DESC, u.last_seen DESC",
}

# The reason part of jobs.error, which is stored as "<reason>: <detail>".
_REASON_SQL = "CASE WHEN instr(error, ':') > 0 THEN substr(error, 1, instr(error, ':') - 1) ELSE error END"


def _now() -> int:
    return int(time.time())


def day_start() -> int:
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
        for (table, column), sql in MIGRATIONS.items():
            async with conn.execute(f"PRAGMA table_info({table})") as cur:
                if column not in {row["name"] async for row in cur}:
                    await conn.execute(sql)
        await conn.commit()
        # Jobs left running by a previous process can never finish.
        await conn.execute(
            "UPDATE jobs SET status = ?, error = 'interrupted by restart', finished_at = ? "
            "WHERE status IN (?, ?)",
            (JobStatus.FAILED, _now(), JobStatus.QUEUED, JobStatus.RUNNING),
        )
        # Privacy: links are kept only for failed requests (also clears rows written before that rule).
        await conn.execute("UPDATE jobs SET url = '' WHERE url != '' AND status NOT IN (?, ?)", FAILED)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def _rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, args) as cur:
            return [dict(row) async for row in cur]

    async def _scalar(self, sql: str, *args: Any) -> int:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return (row[0] or 0) if row else 0

    # users

    async def touch_user(self, tg_id: int, username: str | None, name: str | None = None) -> bool:
        """Upserts the user and returns whether they are banned."""
        now = _now()
        await self.conn.execute(
            "INSERT INTO users (tg_id, username, name, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username, name = excluded.name, "
            "last_seen = excluded.last_seen",
            (tg_id, username, name, now, now),
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

    async def find_user(self, query: str) -> dict[str, Any] | None:
        """By numeric id or @username (case-insensitive)."""
        q = query.strip().lstrip("@")
        if not q:
            return None
        if re.fullmatch(r"-?\d+", q):
            rows = await self._rows("SELECT * FROM users WHERE tg_id = ?", int(q))
        else:
            rows = await self._rows("SELECT * FROM users WHERE lower(username) = lower(?)", q)
        return rows[0] if rows else None

    async def users_by_ids(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return {r["tg_id"]: r for r in await self._rows(f"SELECT * FROM users WHERE tg_id IN ({marks})", *ids)}

    async def users_page(self, sort: str, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        """Users with their all-time usage, one page; also returns the total user count."""
        rows = await self._rows(
            "SELECT u.tg_id, u.username, u.name, u.first_seen, u.last_seen, u.banned, "
            "COALESCE(SUM(j.status IN (?, ?)), 0) AS delivered, COALESCE(SUM(j.status IN (?, ?)), 0) AS failed, "
            "COALESCE(SUM(j.bytes), 0) AS bytes "
            f"FROM users u LEFT JOIN jobs j ON j.tg_id = u.tg_id GROUP BY u.tg_id ORDER BY {USER_SORTS[sort]} "
            "LIMIT ? OFFSET ?",
            *DELIVERED, *FAILED, limit, offset,
        )
        return rows, await self._scalar("SELECT COUNT(*) FROM users")

    async def user_detail(self, tg_id: int, recent: int = 10) -> dict[str, Any] | None:
        users = await self._rows("SELECT * FROM users WHERE tg_id = ?", tg_id)
        if not users:
            return None
        by_status = {
            r["status"]: (r["n"], r["b"])
            for r in await self._rows(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM jobs WHERE tg_id = ? GROUP BY status",
                tg_id,
            )
        }
        by_source = await self._rows(
            "SELECT source, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM jobs "
            "WHERE tg_id = ? AND status IN (?, ?) GROUP BY source ORDER BY n DESC",
            tg_id, *DELIVERED,
        )
        return {
            "user": users[0],
            "by_status": by_status,
            "by_source": by_source,
            "today": await self._scalar("SELECT COUNT(*) FROM jobs WHERE tg_id = ? AND created_at >= ?", tg_id, day_start()),
            "recent": await self._rows(
                "SELECT id, source, url, format, status, error, bytes, created_at FROM jobs "
                "WHERE tg_id = ? ORDER BY id DESC LIMIT ?",
                tg_id, recent,
            ),
        }

    async def broadcast_ids(self) -> list[int]:
        return [r["tg_id"] for r in await self._rows("SELECT tg_id FROM users WHERE banned = 0 ORDER BY last_seen DESC")]

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

    async def delete_cache_prefix(self, prefix: str) -> int:
        # substr instead of LIKE: media ids contain "_", which LIKE treats as a wildcard.
        cur = await self.conn.execute(
            "DELETE FROM media_cache WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
        )
        await self.conn.commit()
        return cur.rowcount

    # jobs

    async def job_create(self, tg_id: int, source: str, fmt: str, egress: str = "",
                         status: str = JobStatus.QUEUED) -> int:
        """A job row without its link: job_finish stores the link only when the request failed."""
        cur = await self.conn.execute(
            "INSERT INTO jobs (tg_id, source, url, format, egress, status, created_at) VALUES (?, ?, '', ?, ?, ?, ?)",
            (tg_id, source, fmt, egress, status, _now()),
        )
        await self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def job_started(self, job_id: int) -> None:
        await self.conn.execute(
            "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?", (JobStatus.RUNNING, _now(), job_id)
        )
        await self.conn.commit()

    async def job_finish(
        self, job_id: int, status: str, *, error: str | None = None, nbytes: int = 0, url: str = ""
    ) -> None:
        await self.conn.execute(
            "UPDATE jobs SET status = ?, error = ?, bytes = ?, finished_at = ?, url = ? WHERE id = ?",
            (status, error, nbytes, _now(), url if status in FAILED else "", job_id),
        )
        await self.conn.commit()

    async def failed_job(self, job_id: int) -> dict[str, Any] | None:
        rows = await self._rows(
            f"SELECT id, tg_id, source, url, format, egress, status, created_at, {_REASON_SQL} AS reason "
            "FROM jobs WHERE id = ? AND status IN (?, ?)",
            job_id, *FAILED,
        )
        return rows[0] if rows else None

    async def recent_failures(self, limit: int) -> list[dict[str, Any]]:
        return await self._rows(
            f"SELECT id, tg_id, source, url, format, egress, status, error, created_at, {_REASON_SQL} AS reason "
            "FROM jobs WHERE status IN (?, ?) ORDER BY id DESC LIMIT ?",
            *FAILED, limit,
        )

    # statistics

    async def stats(self, since: int = 0) -> dict[str, Any]:
        """Usage since a unix time (0 for all time). The cache figures are always all-time."""
        by_status = {
            r["status"]: (r["n"], r["b"])
            for r in await self._rows(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM jobs WHERE created_at >= ? "
                "GROUP BY status",
                since,
            )
        }
        return {
            "users_total": await self._scalar("SELECT COUNT(*) FROM users"),
            "users_new": await self._scalar("SELECT COUNT(*) FROM users WHERE first_seen >= ?", since),
            "users_active": await self._scalar("SELECT COUNT(DISTINCT tg_id) FROM jobs WHERE created_at >= ?", since),
            "users_banned": await self._scalar("SELECT COUNT(*) FROM users WHERE banned = 1"),
            "jobs": {k: v[0] for k, v in by_status.items()},
            "bytes": sum(v[1] for v in by_status.values()),
            "by_source": await self._rows(
                "SELECT source, COUNT(*) AS n, COALESCE(SUM(status IN (?, ?)), 0) AS delivered, "
                "COALESCE(SUM(status IN (?, ?)), 0) AS failed, COALESCE(SUM(bytes), 0) AS bytes "
                "FROM jobs WHERE created_at >= ? GROUP BY source ORDER BY n DESC",
                *DELIVERED, *FAILED, since,
            ),
            "reasons": await self._rows(
                f"SELECT {_REASON_SQL} AS reason, COUNT(*) AS n FROM jobs WHERE created_at >= ? AND status IN (?, ?) "
                "GROUP BY reason ORDER BY n DESC LIMIT 8",
                since, *FAILED,
            ),
            "failures": await self._rows(
                "SELECT source, egress, COUNT(*) AS n FROM jobs WHERE created_at >= ? AND status IN (?, ?) "
                "GROUP BY source, egress ORDER BY n DESC",
                since, *FAILED,
            ),
            "top_users": await self._rows(
                "SELECT j.tg_id, u.username, u.name, COUNT(*) AS n, COALESCE(SUM(j.bytes), 0) AS b "
                "FROM jobs j LEFT JOIN users u ON u.tg_id = j.tg_id WHERE j.created_at >= ? AND j.status IN (?, ?) "
                "GROUP BY j.tg_id ORDER BY n DESC LIMIT 5",
                since, *DELIVERED,
            ),
            "cache_files": await self._scalar("SELECT COUNT(*) FROM media_cache"),
            "cache_bytes": await self._scalar("SELECT COALESCE(SUM(size_bytes), 0) FROM media_cache"),
        }
