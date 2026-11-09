from __future__ import annotations

from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import SimpleFilesPathWrapper, TelegramAPIServer

from app.core.config import settings


def build_bot() -> Bot:
    """Bot bound to the local telegram-bot-api server when one is configured."""
    if not settings.telegram_api_url:
        return Bot(token=settings.bot_token)
    server = TelegramAPIServer.from_base(
        settings.telegram_api_url,
        is_local=True,
        wrap_local_file=SimpleFilesPathWrapper(
            server_path=Path(settings.telegram_files_server_dir),
            local_path=Path(settings.telegram_files_local_dir),
        ),
    )
    return Bot(token=settings.bot_token, session=AiohttpSession(api=server))
