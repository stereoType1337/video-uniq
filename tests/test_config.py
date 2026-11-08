from __future__ import annotations

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
