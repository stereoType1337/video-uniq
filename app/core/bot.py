from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit

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


async def webhook_ip(webhook_url: str, api_url: str) -> str | None:
    """IP of this container on the network shared with the local Bot API server.

    telegram-bot-api remembers the IP it resolved for the webhook host; after a container
    re-create Docker hands out a new IP and webhooks hit "Connection refused" forever.
    Passing ip_address to setWebhook on every start pins the current one. None when the
    cloud Bot API is used or the hosts cannot be resolved.
    """
    own_host, api_host = urlsplit(webhook_url).hostname, urlsplit(api_url).hostname
    if not api_url or own_host is None or api_host is None:
        return None
    loop = asyncio.get_running_loop()
    try:
        peer = str((await loop.getaddrinfo(api_host, None, family=socket.AF_INET))[0][4][0])
        own = {
            str(ai[4][0]) for ai in await loop.getaddrinfo(own_host, None, family=socket.AF_INET)
        }
    except OSError:
        return None
    peer_net = ipaddress.ip_network(f"{peer}/16", strict=False)
    shared = sorted(ip for ip in own if ipaddress.ip_address(ip) in peer_net)
    return shared[0] if shared else None
