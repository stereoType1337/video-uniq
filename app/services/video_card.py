"""The video card: a draft job the user tunes (mode, copies, sound) and then starts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

from app.core.exceptions import AppError, TooManyActiveJobsError
from app.db.repositories.jobs import Job, JobStatus
from app.db.repositories.users import User
from app.services.billing import job_cost
from app.services.render_plan import AudioMode, Mode

logger = structlog.get_logger()

COPIES_OPTIONS = (1, 3, 5, 10)
AUDIO_CYCLE = (AudioMode.SHIFT, AudioMode.KEEP, AudioMode.MUTE)

Choice = tuple[Mode, int, AudioMode]
OnQueued = Callable[[Job, int], Awaitable[None]]


class CardExpiredError(AppError):
    def __init__(self, job_id: int) -> None:
        super().__init__(f"Card {job_id} is no longer a draft")
        self.job_id = job_id


class CardJobs(Protocol):
    async def get(self, job_id: int) -> Job | None: ...
    async def update_draft(self, job_id: int, user_id: int, choice: Choice) -> Job | None: ...
    async def cancel_draft(self, job_id: int, user_id: int) -> bool: ...
    async def count_active(self, user_id: int) -> int: ...
    async def queue_position(self, job_id: int) -> int: ...


class CardBilling(Protocol):
    async def confirm_draft(self, draft: Job, cost: int) -> Job | None: ...


class CardUsers(Protocol):
    async def update_settings(
        self, user_id: int, mode: Mode, copies: int, audio_mode: AudioMode | None = None
    ) -> User: ...


class CardQueue(Protocol):
    async def enqueue(self, job_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class CardDeps:
    jobs: CardJobs
    billing: CardBilling
    users: CardUsers
    queue: CardQueue


def next_choice(job: Job, field: str, value: str) -> Choice | None:
    """Validated choice after a button press; None for forged or unknown values."""
    if field == "mode" and value in {m.value for m in Mode}:
        return Mode(value), job.copies, job.audio_mode
    if field == "copies" and value.isdigit() and int(value) in COPIES_OPTIONS:
        return job.mode, int(value), job.audio_mode
    if field == "audio" and value == "next":
        following = AUDIO_CYCLE[(AUDIO_CYCLE.index(job.audio_mode) + 1) % len(AUDIO_CYCLE)]
        return job.mode, job.copies, following
    return None


class VideoCardService:
    def __init__(self, deps: CardDeps, max_active_jobs: int) -> None:
        self._deps = deps
        self._max_active_jobs = max_active_jobs

    async def change(self, job_id: int, user: User, field: str, value: str) -> Job:
        draft = await self._draft(job_id, user)
        choice = next_choice(draft, field, value)
        if choice is None:
            return draft
        updated = await self._deps.jobs.update_draft(job_id, user.id, choice)
        if updated is None:
            raise CardExpiredError(job_id)
        await self._deps.users.update_settings(user.id, *choice)  # remember as the new default
        return updated

    async def start(self, job_id: int, user: User, on_queued: OnQueued) -> Job:
        """Reserve credits and queue. ``on_queued`` runs before enqueue so a fast worker's
        progress message cannot be overwritten by the queue message."""
        draft = await self._draft(job_id, user)
        if await self._deps.jobs.count_active(user.id) >= self._max_active_jobs:
            raise TooManyActiveJobsError(user.id, self._max_active_jobs)
        cost = job_cost(draft.mode, draft.copies)
        job = await self._deps.billing.confirm_draft(draft, cost)  # InsufficientBalanceError
        if job is None:
            raise CardExpiredError(job_id)
        await on_queued(job, await self._deps.jobs.queue_position(job.id))
        await self._deps.queue.enqueue(job.id)
        logger.info("job_queued", job_id=job.id, user_id=user.id, copies=job.copies, cost=cost)
        return job

    async def cancel(self, job_id: int, user: User) -> bool:
        return await self._deps.jobs.cancel_draft(job_id, user.id)

    async def _draft(self, job_id: int, user: User) -> Job:
        job = await self._deps.jobs.get(job_id)
        if job is None or job.user_id != user.id or job.status is not JobStatus.DRAFT:
            raise CardExpiredError(job_id)
        return job
