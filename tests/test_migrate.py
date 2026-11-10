from __future__ import annotations

import asyncpg

from app.db.migrate import MIGRATIONS_DIR, run_migrations


async def test_fresh_database_gets_every_migration_once(pool: asyncpg.Pool) -> None:
    # the fixture already ran the runner on an empty schema
    names = {r["name"] for r in await pool.fetch("SELECT name FROM schema_migrations")}
    assert names == {p.name for p in MIGRATIONS_DIR.glob("*.sql")}
    assert await run_migrations(pool) == [], "second run applies nothing"


async def test_initdb_database_is_baselined_not_reapplied(pool: asyncpg.Pool) -> None:
    """Production state on 2026-11-10: 001+002 applied by docker-entrypoint-initdb.d."""
    async with pool.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for name in ("001_init.sql", "002_billing.sql"):
            await conn.execute((MIGRATIONS_DIR / name).read_text())
        await conn.execute(
            "INSERT INTO users (tg_id, ref_code) VALUES (1, 'r');"
            "INSERT INTO jobs (user_id, chat_id, source_sha256, source_key, mode, copies,"
            " credits_reserved, credits_charged, settled_at)"
            " VALUES (1, 1, 's', 'k', 'base', 3, 3, 2, now())"
        )
    applied = await run_migrations(pool)
    assert applied == [p.name for p in sorted(MIGRATIONS_DIR.glob("*.sql"))][2:]
    assert await pool.fetchval("SELECT copies_delivered FROM jobs") == 2, "backfilled"
