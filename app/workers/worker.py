from __future__ import annotations

from pathlib import Path
from typing import Any

from aiogram import Bot
from arq import cron, run_worker
from arq.connections import RedisSettings

from app.core.bot import build_bot
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.pool import close_pool, get_pool, init_pool
from app.db.repositories.jobs import JobRepository
from app.services.intake import QUEUE_NAME
from app.services.notifier import JobNotifier
from app.workers.uniqualize import (
    JOBS_KEY,
    NOTIFIER_KEY,
    PROCESSOR_KEY,
    build_processor,
    cleanup_files,
    uniqualize,
)

_BOT_KEY = "bot"


async def _on_startup(ctx: dict[str, Any]) -> None:
    await init_pool()
    bot = build_bot()
    ctx[_BOT_KEY] = bot
    ctx[JOBS_KEY] = JobRepository(get_pool())
    ctx[PROCESSOR_KEY] = build_processor()
    ctx[NOTIFIER_KEY] = JobNotifier(bot, Path(settings.work_dir))


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    bot: Bot | None = ctx.get(_BOT_KEY)
    if bot is not None:
        await bot.session.close()
    await close_pool()


class WorkerSettings:
    functions = [uniqualize]
    # arq's WorkerCoroutine protocol wants *args/**kwargs in the signature; ours needs none
    cron_jobs = [cron(cleanup_files, minute=17, unique=True)]  # type: ignore[arg-type]
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    queue_name = QUEUE_NAME
    max_tries = 3
    job_timeout = 900
    redis_settings = RedisSettings.from_dsn(settings.redis_url)


def main() -> None:
    setup_logging()
    run_worker(WorkerSettings)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
