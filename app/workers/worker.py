from __future__ import annotations

from arq import run_worker
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.logging import setup_logging

QUEUE_NAME = "uniqualize"


class WorkerSettings:
    functions: list[object] = []
    queue_name = QUEUE_NAME
    max_tries = 3
    job_timeout = 900
    redis_settings = RedisSettings.from_dsn(settings.redis_url)


def main() -> None:
    setup_logging()
    run_worker(WorkerSettings)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
