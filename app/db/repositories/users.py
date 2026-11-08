from __future__ import annotations

import asyncpg

from app.db.base import BaseRepository


class UserRepository(BaseRepository):
    async def get_by_tg_id(self, tg_id: int) -> asyncpg.Record | None:
        return await self.fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)

    async def upsert(self, tg_id: int, username: str | None, ref_code: str) -> asyncpg.Record:
        row = await self.fetchrow(
            """
            INSERT INTO users (tg_id, username, ref_code)
            VALUES ($1, $2, $3)
            ON CONFLICT (tg_id) DO UPDATE SET username = EXCLUDED.username
            RETURNING *
            """,
            tg_id,
            username,
            ref_code,
        )
        if row is None:
            raise RuntimeError("upsert returned no row")
        return row

    async def get_balance(self, tg_id: int) -> int:
        value = await self.fetchval("SELECT balance FROM users WHERE tg_id = $1", tg_id)
        return int(value) if value is not None else 0
