from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("WEBHOOK_HOST", "https://bot.test")
os.environ.setdefault("SERVICE_SECRET", "0" * 64)

_MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "db" / "migrations"


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    """Fresh schema in TEST_DATABASE_URL (an empty, disposable database). Skips without it."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if dsn is None:
        pytest.skip("TEST_DATABASE_URL is not set")
    db = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    async with db.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for sql in sorted(_MIGRATIONS.glob("*.sql")):
            await conn.execute(sql.read_text())
    yield db
    await db.close()
