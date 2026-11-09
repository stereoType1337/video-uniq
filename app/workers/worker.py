from __future__ import annotations

from typing import Any

from arq import run_worker
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.pool import close_pool, init_pool
from app.workers.uniqualize import PROCESSOR_KEY, build_processor, uniqualize

QUEUE_NAME = "uniqualize"


async def _on_startup(ctx: dict[str, Any]) -> None:
    await init_pool()
    ctx[PROCESSOR_KEY] = build_processor()


async def _on_shutdown(_: dict[str, Any]) -> None:
    await close_pool()


class WorkerSettings:
    functions = [uniqualize]
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    queue_name = QUEUE_NAME
    max_tries = 3
    job_timeout = 900
    redis_settings = RedisSettings.from_dsn(settings.redis_url)


def main() -> None:
    setup_logging()
    run_worker(WorkerSettings)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
