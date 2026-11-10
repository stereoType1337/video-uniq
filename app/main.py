from __future__ import annotations

from pathlib import Path

import structlog
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.bot import build_bot, webhook_ip
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.migrate import run_migrations
from app.db.pool import close_pool, init_pool
from app.db.repositories.admin import AdminRepository
from app.db.repositories.billing import BillingRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.profiles import ProfileRepository
from app.db.repositories.users import UserRepository
from app.handlers.admin import router as admin_router
from app.handlers.billing import router as billing_router
from app.handlers.card import router as card_router
from app.handlers.menu import router as menu_router
from app.handlers.start import router as start_router
from app.handlers.upload import router as upload_router
from app.services.billing import AccountService, PaymentFlow
from app.services.intake import (
    ArqJobQueue,
    IntakeConfig,
    IntakeDeps,
    IntakeService,
    TelegramFileFetcher,
)
from app.services.metadata import load_profiles
from app.services.video_card import CardDeps, VideoCardService
from app.services.video_rules import VideoRules

logger = structlog.get_logger()

_REDIS_KEY = "arq_redis"

BOT_COMMANDS = [
    BotCommand(command="start", description="Главная"),
    BotCommand(command="settings", description="Режим и число копий"),
    BotCommand(command="balance", description="Баланс и цены"),
    BotCommand(command="buy", description="Пополнить баланс"),
    BotCommand(command="ref", description="Пригласить друга"),
    BotCommand(command="help", description="Как это работает"),
    BotCommand(command="support", description="Поддержка"),
]


async def _on_startup(bot: Bot, dispatcher: Dispatcher) -> None:
    pool = await init_pool()
    await run_migrations(pool)
    profiles = ProfileRepository(pool)
    await profiles.sync(load_profiles())
    dispatcher["profiles"] = profiles
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    users, billing = UserRepository(pool), BillingRepository(pool)
    jobs = JobRepository(pool)
    dispatcher["admin"] = AdminRepository(pool)
    dispatcher["cards"] = VideoCardService(
        CardDeps(jobs=jobs, billing=billing, users=users, queue=ArqJobQueue(redis)),
        max_active_jobs=settings.max_active_jobs,
    )
    dispatcher["billing"] = billing
    accounts = AccountService(users, billing, settings.free_credits)
    dispatcher[_REDIS_KEY] = redis
    dispatcher["users"] = users
    dispatcher["accounts"] = accounts
    dispatcher["payments"] = PaymentFlow(billing)
    dispatcher["intake"] = IntakeService(
        IntakeDeps(accounts=accounts, jobs=jobs, fetcher=TelegramFileFetcher(bot)),
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
    await bot.set_my_commands(BOT_COMMANDS)
    ip = await webhook_ip(settings.webhook_url, settings.telegram_api_url)
    await bot.set_webhook(
        settings.webhook_url,
        ip_address=ip,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )
    logger.info(
        "webhook_set", host=settings.webhook_host, ip=ip, local_api=bool(settings.telegram_api_url)
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
    fsm = RedisStorage.from_url(settings.redis_url, key_builder=DefaultKeyBuilder(prefix="fsm"))
    dp = Dispatcher(storage=fsm)
    dp.include_router(admin_router)  # first: its FSM text input must win over menu buttons
    dp.include_router(start_router)
    dp.include_router(menu_router)
    dp.include_router(billing_router)
    dp.include_router(upload_router)
    dp.include_router(card_router)
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
