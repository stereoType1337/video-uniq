from __future__ import annotations

from pathlib import Path
from typing import Any

from aiogram import Bot
from arq import cron, run_worker
from arq.connections import RedisSettings

from app.core import alerts
from app.core.bot import build_bot
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.migrate import run_migrations
from app.db.pool import close_pool, get_pool, init_pool
from app.db.repositories.billing import BillingRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.profiles import ProfileRepository
from app.services.intake import QUEUE_NAME
from app.services.metadata import load_profiles
from app.services.notifier import JobNotifier
from app.workers.uniqualize import (
    BILLING_KEY,
    BOT_KEY,
    JOBS_KEY,
    NOTIFIER_KEY,
    PROCESSOR_KEY,
    backup_database,
    build_processor,
    cleanup_files,
    expire_credits,
    refund_stale_jobs,
    uniqualize,
)


async def _on_startup(ctx: dict[str, Any]) -> None:
    pool = await init_pool()
    await run_migrations(pool)
    await ProfileRepository(pool).sync(load_profiles())
    bot = build_bot()
    ctx[BOT_KEY] = bot
    alerts.install(alerts.AlertSender(bot, settings.admin_id_set, ctx["redis"], component="worker"))
    ctx[JOBS_KEY] = JobRepository(get_pool())
    ctx[BILLING_KEY] = BillingRepository(get_pool())
    ctx[PROCESSOR_KEY] = build_processor()
    ctx[NOTIFIER_KEY] = JobNotifier(bot, Path(settings.work_dir))


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    bot: Bot | None = ctx.get(BOT_KEY)
    if bot is not None:
        await bot.session.close()
    await close_pool()


class WorkerSettings:
    functions = [uniqualize, backup_database]  # backup also on demand from /admin
    # arq's WorkerCoroutine protocol wants *args/**kwargs in the signature; ours needs none
    cron_jobs = [
        cron(cleanup_files, minute=17, unique=True),  # type: ignore[arg-type]
        cron(refund_stale_jobs, minute=set(range(0, 60, 10)), unique=True),
        cron(expire_credits, hour=3, minute=5, unique=True),
        cron(backup_database, hour=3, minute=30, unique=True, timeout=1800),
    ]
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    queue_name = QUEUE_NAME
    max_tries = 3
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.job_timeout_s
    redis_settings = RedisSettings.from_dsn(settings.redis_url)


def main() -> None:
    setup_logging()
    run_worker(WorkerSettings)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
