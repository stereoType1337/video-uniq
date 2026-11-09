from __future__ import annotations

from pathlib import Path

import structlog
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.bot import build_bot
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.pool import close_pool, init_pool
from app.db.repositories.jobs import JobRepository
from app.db.repositories.users import UserRepository
from app.handlers.start import router as start_router
from app.handlers.upload import router as upload_router
from app.services.intake import (
    ArqJobQueue,
    IntakeConfig,
    IntakeDeps,
    IntakeService,
    TelegramFileFetcher,
)
from app.services.video_rules import VideoRules

logger = structlog.get_logger()

_REDIS_KEY = "arq_redis"


async def _on_startup(bot: Bot, dispatcher: Dispatcher) -> None:
    pool = await init_pool()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    dispatcher[_REDIS_KEY] = redis
    dispatcher["intake"] = IntakeService(
        IntakeDeps(
            users=UserRepository(pool),
            jobs=JobRepository(pool),
            fetcher=TelegramFileFetcher(bot),
            queue=ArqJobQueue(redis),
        ),
        IntakeConfig(
            work_dir=Path(settings.work_dir),
            max_file_mb=settings.max_file_mb,
            max_active_jobs=settings.max_active_jobs,
            rules=VideoRules(
                min_duration_s=settings.min_duration_s, max_duration_s=settings.max_duration_s
            ),
            ffprobe_bin=settings.ffprobe_bin,
        ),
    )
    await bot.set_webhook(settings.webhook_url, drop_pending_updates=True)
    logger.info(
        "webhook_set", host=settings.webhook_host, local_api=bool(settings.telegram_api_url)
    )


async def _on_shutdown(bot: Bot, dispatcher: Dispatcher) -> None:
    await bot.delete_webhook()
    redis: ArqRedis | None = dispatcher.get(_REDIS_KEY)
    if redis is not None:
        await redis.aclose()
    await close_pool()
    logger.info("shutdown_complete")


def build_app() -> web.Application:
    bot = build_bot()
    dp = Dispatcher()
    dp.include_router(start_router)
    dp.include_router(upload_router)
    dp.startup.register(_on_startup)
    dp.shutdown.register(_on_shutdown)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=settings.webhook_path)
    setup_application(app, dp, bot=bot)
    app.router.add_get("/health", _health)
    return app


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="OK")


def main() -> None:
    setup_logging()
    web.run_app(build_app(), host="0.0.0.0", port=settings.webhook_port)


if __name__ == "__main__":
    main()
