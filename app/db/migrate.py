"""Applies app/db/migrations/*.sql in order, once each, on startup of the bot and the worker.

A Postgres advisory lock serialises concurrent starts. Databases created before this runner
existed (schema made by docker-entrypoint-initdb.d) are baselined: the files that were applied
that way are recorded without being re-run.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import structlog

logger = structlog.get_logger()

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_LOCK_ID = 7_301_112  # arbitrary, project-wide
# Applied by docker-entrypoint-initdb.d before the runner existed (deployed 2026-11-10).
_INITDB_BASELINE = ("001_init.sql", "002_billing.sql")


async def run_migrations(pool: asyncpg.Pool, directory: Path = MIGRATIONS_DIR) -> list[str]:
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
        try:
            await _ensure_table(conn)
            done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            applied: list[str] = []
            for path in sorted(directory.glob("*.sql")):
                if path.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute(
                        "INSERT INTO schema_migrations (name) VALUES ($1)", path.name
                    )
                applied.append(path.name)
                logger.info("migration_applied", name=path.name)
            return applied
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)


async def _ensure_table(conn: asyncpg.Connection) -> None:
    exists = await conn.fetchval("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
    if exists:
        return
    legacy = await conn.fetchval("SELECT to_regclass('public.users') IS NOT NULL")
    await conn.execute(
        """
        CREATE TABLE schema_migrations (
            name       TEXT        PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    if legacy:
        await conn.executemany(
            "INSERT INTO schema_migrations (name) VALUES ($1)", [(n,) for n in _INITDB_BASELINE]
        )
        logger.info("migrations_baselined", files=list(_INITDB_BASELINE))
