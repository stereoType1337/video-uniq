"""Error alerts to the admins' Telegram (tg-bot-best-practices §5: ErrorReporter).

Every ERROR-level event — structlog ``logger.error/exception`` or a stdlib ``logging`` record
(aiogram, arq) — becomes one message to ADMIN_IDS. Identical alerts are deduplicated for
DEDUP_S across the bot and the worker via Redis (SET NX EX). Sending never blocks the caller
and a failure to send never produces another alert.

Single process-wide sender (module state): one bot or worker process per container.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import MutableMapping
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from redis.asyncio import Redis

_MAX_TEXT = 3500  # Telegram limit is 4096; keep room for the header
_DEDUP_S = 300
_SKIP_KEYS = {"event", "level", "timestamp", "exc_info", "exception", "stack_info"}
_internal = logging.getLogger("app.alerts.internal")  # never forwarded


class AlertSender:
    def __init__(self, bot: Bot, chat_ids: frozenset[int], dedup: Redis, component: str) -> None:
        self._bot = bot
        self._chat_ids = chat_ids
        self._dedup = dedup
        self._component = component
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(self, key: str, text: str) -> None:
        """Fire-and-forget; safe to call from any code running inside the event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. at interpreter shutdown): drop rather than block
        task = loop.create_task(self._send(key, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, key: str, text: str) -> None:
        digest = hashlib.sha1(f"{self._component}:{key}".encode()).hexdigest()
        try:
            if not await self._dedup.set(f"alert:{digest}", "1", ex=_DEDUP_S, nx=True):
                return
            body = f"🚨 {self._component}\n{text}"[:_MAX_TEXT]
            for chat_id in self._chat_ids:
                await self._bot.send_message(chat_id, body)
        except (TelegramAPIError, OSError):
            _internal.debug("alert_send_failed", exc_info=True)
        except Exception:  # noqa: BLE001 - an alert must never take the process down
            _internal.debug("alert_send_crashed", exc_info=True)


_sender: AlertSender | None = None


def install(sender: AlertSender) -> None:
    """Route ERROR events to ``sender``: structlog via ``structlog_processor`` (already in
    the processor chain), stdlib logging via a root handler."""
    global _sender
    _sender = sender
    root = logging.getLogger()
    if not any(isinstance(h, _StdlibAlertHandler) for h in root.handlers):
        root.addHandler(_StdlibAlertHandler())


def structlog_processor(
    _: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    if _sender is not None and method_name in {"error", "exception", "critical"}:
        event = str(event_dict.get("event", "error"))
        details = "\n".join(f"{k}={v}" for k, v in event_dict.items() if k not in _SKIP_KEYS)
        trace = str(event_dict.get("exception", ""))[-1500:]
        _sender.submit(event + trace[-200:], f"{event}\n{details}\n{trace}".strip())
    return event_dict


class _StdlibAlertHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        if _sender is None or record.name.startswith("app.alerts"):
            return
        try:
            text = self.format(record)[-2000:]
        except Exception:  # noqa: BLE001 - formatting must not break logging
            text = record.getMessage()
        _sender.submit(f"{record.name}:{record.getMessage()[:200]}", f"{record.name}\n{text}")
