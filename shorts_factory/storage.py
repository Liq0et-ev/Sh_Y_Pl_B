"""SQLite persistence (same aiosqlite pattern as Vinted_Telegramm_Bot): per-chat options and job history."""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from .config import PipelineOptions

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    options TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    options TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued | running | done | failed | cancelled | interrupted
    error TEXT,
    outputs TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
"""

ACTIVE = ("queued", "running")


@dataclass
class Job:
    id: int
    chat_id: int
    source: str
    options: PipelineOptions
    status: str
    error: Optional[str]
    outputs: list[str]
    created_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(SCHEMA)
        # A crash/restart cannot resume a half-done render: mark such jobs explicitly.
        await self._db.execute(
            "UPDATE jobs SET status = 'interrupted', finished_at = ? WHERE status IN ('queued', 'running')",
            (_now(),),
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ---- per-chat default options -----------------------------------
    async def get_options(self, chat_id: int) -> PipelineOptions:
        async with self._db.execute("SELECT options FROM chat_settings WHERE chat_id = ?", (chat_id,)) as cur:
            row = await cur.fetchone()
        return PipelineOptions.from_dict(json.loads(row[0])) if row else PipelineOptions()

    async def save_options(self, chat_id: int, options: PipelineOptions) -> None:
        await self._db.execute(
            "INSERT INTO chat_settings (chat_id, options) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET options = excluded.options",
            (chat_id, json.dumps(options.to_dict())),
        )
        await self._db.commit()

    # ---- jobs ---------------------------------------------------------
    async def add_job(self, chat_id: int, source: str, options: PipelineOptions) -> int:
        cur = await self._db.execute(
            "INSERT INTO jobs (chat_id, source, options, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
            (chat_id, source, json.dumps(options.to_dict()), _now()),
        )
        await self._db.commit()
        return cur.lastrowid

    async def set_status(self, job_id: int, status: str, error: str | None = None,
                         outputs: list[str] | None = None) -> None:
        finished = _now() if status not in ACTIVE else None
        await self._db.execute(
            "UPDATE jobs SET status = ?, error = ?, outputs = COALESCE(?, outputs), finished_at = ? WHERE id = ?",
            (status, error, json.dumps(outputs) if outputs is not None else None, finished, job_id),
        )
        await self._db.commit()

    async def get_job(self, job_id: int, chat_id: int | None = None) -> Optional[Job]:
        query = "SELECT id, chat_id, source, options, status, error, outputs, created_at FROM jobs WHERE id = ?"
        params: list = [job_id]
        if chat_id is not None:
            query += " AND chat_id = ?"
            params.append(chat_id)
        async with self._db.execute(query, params) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row else None

    async def recent_jobs(self, chat_id: int, limit: int = 5) -> list[Job]:
        async with self._db.execute(
            "SELECT id, chat_id, source, options, status, error, outputs, created_at "
            "FROM jobs WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]


def _row_to_job(row) -> Job:
    return Job(
        id=row[0], chat_id=row[1], source=row[2],
        options=PipelineOptions.from_dict(json.loads(row[3])),
        status=row[4], error=row[5], outputs=json.loads(row[6]), created_at=row[7],
    )
