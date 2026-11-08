from __future__ import annotations

import structlog
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.pool import close_pool, init_pool
from app.handlers.start import router as start_router

logger = structlog.get_logger()


async def _on_startup(bot: Bot) -> None:
    await init_pool()
    await bot.set_webhook(settings.webhook_url, drop_pending_updates=True)
    logger.info("webhook_set", host=settings.webhook_host)


async def _on_shutdown(bot: Bot) -> None:
    await bot.delete_webhook()
    await close_pool()
    logger.info("shutdown_complete")


def build_app() -> web.Application:
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(start_router)
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
