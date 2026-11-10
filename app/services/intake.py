"""Accepting a video from a user: validate, store the source, create the job, enqueue it."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from arq.connections import ArqRedis

from app.core.exceptions import (
    FileTooLargeError,
    InsufficientBalanceError,
    ProbeError,
    SourceDownloadError,
    TooManyActiveJobsError,
)
from app.db.repositories.jobs import Job, NewJob
from app.services.billing import Account, job_cost
from app.services.fingerprints import file_sha256
from app.services.probe import probe_source
from app.services.render_plan import SourceInfo
from app.services.video_rules import (
    Rejection,
    RejectReason,
    VideoRejectedError,
    VideoRules,
    check_source,
    is_duration_allowed,
)

logger = structlog.get_logger()

_BYTES_IN_MB = 1024 * 1024
QUEUE_NAME = "uniqualize"
TASK_NAME = "uniqualize"


class AccountStore(Protocol):
    async def ensure(
        self, tg_id: int, username: str | None, start_arg: str | None = None
    ) -> Account: ...


class JobStore(Protocol):
    async def count_active(self, user_id: int) -> int: ...
    async def queue_position(self, job_id: int) -> int: ...


class ReservationStore(Protocol):
    async def create_job_reserving(self, job: NewJob, cost: int) -> Job: ...


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
    duration: int | None  # Telegram's rounded duration; documents usually have none


@dataclass(frozen=True, slots=True)
class IntakeConfig:
    work_dir: Path
    max_file_mb: int
    max_active_jobs: int
    rules: VideoRules = field(default_factory=VideoRules)
    ffprobe_bin: str = "ffprobe"


@dataclass(frozen=True, slots=True)
class IntakeDeps:
    accounts: AccountStore
    jobs: JobStore
    reservations: ReservationStore
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
        self._check_metadata(request)
        user = (await self._deps.accounts.ensure(request.tg_id, request.username)).user
        cost = job_cost(user.mode, user.copies)
        if user.balance < cost:  # cheap pre-check; the reservation below is authoritative
            raise InsufficientBalanceError(user.id, cost, user.balance)
        if await self._deps.jobs.count_active(user.id) >= self._config.max_active_jobs:
            raise TooManyActiveJobsError(user.id, self._config.max_active_jobs)

        source_key = f"src/{uuid.uuid4().hex}.mp4"
        source_path = self._config.work_dir / source_key
        source_path.parent.mkdir(parents=True, exist_ok=True)
        await self._deps.fetcher.fetch(request.file_id, source_path)
        try:
            await self._check_file(source_path)
        except (FileTooLargeError, ProbeError, VideoRejectedError):
            source_path.unlink(missing_ok=True)
            raise

        new_job = NewJob(
            user_id=user.id,
            chat_id=request.chat_id,
            status_message_id=request.status_message_id,
            source_sha256=await asyncio.to_thread(file_sha256, source_path),
            source_key=source_key,
            mode=user.mode,
            copies=user.copies,
        )
        try:
            job = await self._deps.reservations.create_job_reserving(new_job, cost)
        except InsufficientBalanceError:
            source_path.unlink(missing_ok=True)
            raise
        await on_queued(job, await self._deps.jobs.queue_position(job.id))
        await self._deps.queue.enqueue(job.id)
        logger.info("job_queued", job_id=job.id, user_id=user.id, copies=job.copies, cost=cost)
        return job

    def _check_metadata(self, request: IntakeRequest) -> None:
        """Cheap checks on what Telegram tells us, before downloading anything."""
        self._check_size(request.file_size)
        rules = self._config.rules
        if request.duration is not None and not is_duration_allowed(request.duration, rules):
            early = SourceInfo(0, 0, 0.0, float(request.duration), None)
            raise VideoRejectedError(Rejection(RejectReason.TOO_LONG, early, rules))

    async def _check_file(self, path: Path) -> SourceInfo:
        self._check_size(path.stat().st_size)
        source = await probe_source(path, self._config.ffprobe_bin)
        rejection = check_source(source, self._config.rules)
        if rejection is not None:
            raise VideoRejectedError(rejection)
        return source

    def _check_size(self, size: int | None) -> None:
        if size is not None and size > self._config.max_file_mb * _BYTES_IN_MB:
            raise FileTooLargeError(size / _BYTES_IN_MB, self._config.max_file_mb)


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
