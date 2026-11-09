"""Accepting a video from a user: validate, store the source, create the job, enqueue it."""

from __future__ import annotations

import asyncio
import secrets
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import asyncpg
import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from arq.connections import ArqRedis

from app.core.exceptions import FileTooLargeError, SourceDownloadError, TooManyActiveJobsError
from app.db.repositories.jobs import Job, NewJob
from app.services.fingerprints import file_sha256
from app.services.render_plan import Mode

logger = structlog.get_logger()

_BYTES_IN_MB = 1024 * 1024
QUEUE_NAME = "uniqualize"
TASK_NAME = "uniqualize"


class UserStore(Protocol):
    async def upsert(self, tg_id: int, username: str | None, ref_code: str) -> asyncpg.Record: ...


class JobStore(Protocol):
    async def create(self, job: NewJob) -> Job: ...
    async def count_active(self, user_id: int) -> int: ...
    async def queue_position(self, job_id: int) -> int: ...


class FileFetcher(Protocol):
    async def fetch(self, file_id: str, dst: Path) -> None: ...


class JobQueue(Protocol):
    async def enqueue(self, job_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class IntakeRequest:
    tg_id: int
    username: str | None
    chat_id: int
    status_message_id: int | None
    file_id: str
    file_size: int | None


@dataclass(frozen=True, slots=True)
class IntakeConfig:
    work_dir: Path
    max_file_mb: int
    max_active_jobs: int


@dataclass(frozen=True, slots=True)
class IntakeDeps:
    users: UserStore
    jobs: JobStore
    fetcher: FileFetcher
    queue: JobQueue


OnQueued = Callable[[Job, int], Awaitable[None]]


class IntakeService:
    def __init__(self, deps: IntakeDeps, config: IntakeConfig) -> None:
        self._deps = deps
        self._config = config

    async def accept(self, request: IntakeRequest, on_queued: OnQueued) -> Job:
        """``on_queued`` runs before the job hits the queue, so a fast worker cannot be
        overtaken by the "you are in the queue" message."""
        self._check_size(request.file_size)
        user = await self._deps.users.upsert(request.tg_id, request.username, _ref_code())
        if await self._deps.jobs.count_active(user["id"]) >= self._config.max_active_jobs:
            raise TooManyActiveJobsError(user["id"], self._config.max_active_jobs)

        source_key = f"src/{uuid.uuid4().hex}.mp4"
        source_path = self._config.work_dir / source_key
        source_path.parent.mkdir(parents=True, exist_ok=True)
        await self._deps.fetcher.fetch(request.file_id, source_path)
        self._check_size(source_path.stat().st_size)

        job = await self._deps.jobs.create(
            NewJob(
                user_id=user["id"],
                chat_id=request.chat_id,
                status_message_id=request.status_message_id,
                source_sha256=await asyncio.to_thread(file_sha256, source_path),
                source_key=source_key,
                mode=Mode(user["mode"]),
                copies=user["copies"],
            )
        )
        await on_queued(job, await self._deps.jobs.queue_position(job.id))
        await self._deps.queue.enqueue(job.id)
        logger.info("job_queued", job_id=job.id, user_id=user["id"], copies=job.copies)
        return job

    def _check_size(self, size: int | None) -> None:
        if size is not None and size > self._config.max_file_mb * _BYTES_IN_MB:
            raise FileTooLargeError(size / _BYTES_IN_MB, self._config.max_file_mb)


def _ref_code() -> str:
    return secrets.token_urlsafe(6)


class TelegramFileFetcher:
    """With a local Bot API server the file is already on our disk: move it, don't copy."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def fetch(self, file_id: str, dst: Path) -> None:
        try:
            file = await self._bot.get_file(file_id)
        except TelegramAPIError as exc:
            raise SourceDownloadError(file_id, str(exc)) from exc
        if file.file_path is None:
            raise SourceDownloadError(file_id, "no file_path")
        api = self._bot.session.api
        if api.is_local:
            local = Path(api.wrap_local_file.to_local(file.file_path))
            await asyncio.to_thread(shutil.move, local, dst)
            return
        await self._bot.download_file(file.file_path, destination=dst, timeout=300)


class ArqJobQueue:
    def __init__(self, redis: ArqRedis) -> None:
        self._redis = redis

    async def enqueue(self, job_id: int) -> None:
        await self._redis.enqueue_job(
            TASK_NAME, job_id, _job_id=f"job:{job_id}", _queue_name=QUEUE_NAME
        )
