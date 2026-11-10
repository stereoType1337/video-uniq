from __future__ import annotations

import asyncio
import logging
from typing import cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from redis.asyncio import Redis

from app.core import alerts


class FakeRedis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, name: str, value: str, *, ex: int, nx: bool) -> bool | None:
        if nx and name in self.keys:
            return None
        self.keys.add(name)
        return True


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.fail:
            raise TelegramNetworkError(SendMessage(chat_id=chat_id, text=text), "down")
        self.sent.append((chat_id, text))


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> FakeBot:
    fake = FakeBot()
    sender = alerts.AlertSender(
        cast(Bot, fake), frozenset({1, 2}), cast(Redis, FakeRedis()), "test"
    )
    monkeypatch.setattr(alerts, "_sender", sender)
    return fake


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_structlog_error_reaches_every_admin_once(bot: FakeBot) -> None:
    for _ in range(3):
        alerts.structlog_processor(None, "error", {"event": "render_failed", "job_id": 7})
    alerts.structlog_processor(None, "info", {"event": "render_done"})
    await _drain()
    assert sorted(chat for chat, _ in bot.sent) == [1, 2], "deduplicated, info ignored"
    assert "render_failed" in bot.sent[0][1] and "job_id=7" in bot.sent[0][1]


async def test_stdlib_errors_are_forwarded(bot: FakeBot, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = alerts._StdlibAlertHandler()
    record = logging.LogRecord(
        "aiogram.event", logging.ERROR, __file__, 1, "Cause exception", None, None
    )
    handler.emit(record)
    handler.emit(
        logging.LogRecord("app.alerts.internal", logging.ERROR, __file__, 1, "x", None, None)
    )
    await _drain()
    assert len(bot.sent) == 2 and "aiogram.event" in bot.sent[0][1]


async def test_send_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = FakeBot(fail=True)
    sender = alerts.AlertSender(cast(Bot, failing), frozenset({1}), cast(Redis, FakeRedis()), "t")
    monkeypatch.setattr(alerts, "_sender", sender)
    alerts.structlog_processor(None, "error", {"event": "boom"})
    await _drain()  # no exception escapes, nothing sent
    assert failing.sent == []


def test_no_sender_or_no_loop_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts, "_sender", None)
    assert alerts.structlog_processor(None, "error", {"event": "x"}) == {"event": "x"}
