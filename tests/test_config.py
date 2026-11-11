from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from app.core.bot import webhook_ip
from app.core.config import Settings


def test_webhook_url_is_built_from_host_and_token() -> None:
    cfg = Settings(
        bot_token="123:abc",
        webhook_host="https://bot.example.com/",
        service_secret="x" * 64,
        _env_file=None,
    )
    assert cfg.webhook_path == "/webhook/123:abc"
    assert cfg.webhook_url == "https://bot.example.com/webhook/123:abc"


async def test_webhook_ip_picks_address_on_the_bot_api_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = {
        "video_uniq_tg_api": ["172.19.0.3"],
        "video_uniq_bot": ["172.18.0.7", "172.19.0.9"],  # also on webapps_network
    }

    async def fake_getaddrinfo(host: str, *_: object, **__: object) -> list[tuple[object, ...]]:
        if host not in addresses:
            raise OSError(host)
        return [(socket.AF_INET, 0, 0, "", (ip, 0)) for ip in addresses[host]]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    url, api = "http://video_uniq_bot:8080", "http://video_uniq_tg_api:8081"
    assert await webhook_ip(url, api) == "172.19.0.9"
    assert await webhook_ip(url, "") is None, "cloud Bot API: let Telegram resolve DNS"
    assert await webhook_ip("http://unknown:1", api) is None


def test_inline_env_comment_is_not_a_value(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SUPPORT_CONTACT=            # @username поддержки\nCONTACT_EMAIL=a@b.c\n")
    cfg = Settings(bot_token="1:x", webhook_host="http://h", service_secret="s", _env_file=env)
    assert cfg.support_contact == ""
    assert cfg.contact_email == "a@b.c"
