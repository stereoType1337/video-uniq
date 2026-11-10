from __future__ import annotations

import logging

import structlog

from app.core import alerts

_NOISY_LOGGERS = ("aiogram", "httpx", "httpcore", "asyncpg", "arq")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(format="%(message)s", level=level)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            alerts.structlog_processor,  # ERROR events -> admins' Telegram (no-op until installed)
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
