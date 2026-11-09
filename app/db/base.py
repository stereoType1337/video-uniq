from __future__ import annotations

from typing import Any

import asyncpg


class BaseRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        return await self._pool.fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        return list(await self._pool.fetch(query, *args))

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._pool.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        return str(await self._pool.execute(query, *args))
