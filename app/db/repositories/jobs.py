from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import asyncpg

from app.db.base import BaseRepository
from app.services.render_plan import AudioMode, Mode, SourceInfo


class JobStatus(StrEnum):
    DRAFT = "draft"  # video card shown, no credits taken yet
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"  # draft dismissed or replaced by a newer video
    EXPIRED = "expired"  # draft never started


_FINAL_STATUSES = (
    JobStatus.DONE,
    JobStatus.PARTIAL,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.EXPIRED,
)
_ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.PROCESSING)


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    user_id: int
    chat_id: int
    status_message_id: int | None
    source_sha256: str
    source_key: str
    mode: Mode
    copies: int
    status: JobStatus
    credits_reserved: int = 0
    settled: bool = False
    audio_mode: AudioMode = AudioMode.SHIFT
    source_duration: float | None = None
    source_width: int | None = None
    source_height: int | None = None
    camera_profiles: bool = False


@dataclass(frozen=True, slots=True)
class NewJob:
    user_id: int
    chat_id: int
    status_message_id: int | None
    source_sha256: str
    source_key: str
    mode: Mode
    copies: int
    audio_mode: AudioMode = AudioMode.SHIFT
    source: SourceInfo | None = None
    camera_profiles: bool = False


INSERT_JOB_SQL = """
    INSERT INTO jobs (user_id, chat_id, status_message_id, source_sha256, source_key,
                      mode, copies, credits_reserved, status, audio_mode,
                      source_duration, source_width, source_height, camera_profiles)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
    RETURNING *
"""


def job_insert_args(
    job: NewJob, credits_reserved: int, status: JobStatus = JobStatus.QUEUED
) -> tuple[object, ...]:
    src = job.source
    return (
        job.user_id,
        job.chat_id,
        job.status_message_id,
        job.source_sha256,
        job.source_key,
        job.mode.value,
        job.copies,
        credits_reserved,
        status.value,
        job.audio_mode.value,
        src.duration if src is not None else None,
        src.width if src is not None else None,
        src.height if src is not None else None,
        job.camera_profiles,
    )


class JobRepository(BaseRepository):
    async def create(self, job: NewJob) -> Job:
        """Create a queued job without reserving credits (billing uses create_job_reserving)."""
        return await self._insert(job, JobStatus.QUEUED)

    async def create_draft(self, job: NewJob) -> Job:
        """A video card: replaces the user's previous unstarted card."""
        await self.execute(
            "UPDATE jobs SET status = $2, finished_at = now() WHERE user_id = $1 AND status = $3",
            job.user_id,
            JobStatus.CANCELLED.value,
            JobStatus.DRAFT.value,
        )
        return await self._insert(job, JobStatus.DRAFT)

    async def update_draft(
        self, job_id: int, user_id: int, choice: tuple[Mode, int, AudioMode]
    ) -> Job | None:
        """None when the card is not this user's or is no longer a draft."""
        mode, copies, audio = choice
        row = await self.fetchrow(
            """
            UPDATE jobs SET mode = $3, copies = $4, audio_mode = $5
            WHERE id = $1 AND user_id = $2 AND status = 'draft'
            RETURNING *
            """,
            job_id,
            user_id,
            mode.value,
            copies,
            audio.value,
        )
        return to_job(row) if row is not None else None

    async def cancel_draft(self, job_id: int, user_id: int) -> bool:
        result = await self.execute(
            """
            UPDATE jobs SET status = 'cancelled', finished_at = now()
            WHERE id = $1 AND user_id = $2 AND status = 'draft'
            """,
            job_id,
            user_id,
        )
        return result.endswith(" 1")

    async def expire_drafts(self, older_than_minutes: int) -> int:
        result = await self.execute(
            """
            UPDATE jobs SET status = 'expired', finished_at = now()
            WHERE status = 'draft' AND created_at < now() - make_interval(mins => $1)
            """,
            older_than_minutes,
        )
        return int(result.split()[-1])

    async def get(self, job_id: int) -> Job | None:
        row = await self.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
        return to_job(row) if row is not None else None

    async def set_status(self, job_id: int, status: JobStatus) -> None:
        await self.execute(
            """
            UPDATE jobs
            SET status = $2,
                finished_at = CASE WHEN $2 = ANY($3::text[]) THEN now() ELSE NULL END
            WHERE id = $1
            """,
            job_id,
            status.value,
            [s.value for s in _FINAL_STATUSES],
        )

    async def count_active(self, user_id: int) -> int:
        value = await self.fetchval(
            "SELECT count(*) FROM jobs WHERE user_id = $1 AND status = ANY($2::text[])",
            user_id,
            [s.value for s in _ACTIVE_STATUSES],
        )
        return int(value)

    async def queue_position(self, job_id: int) -> int:
        """How many queued jobs were created before this one."""
        value = await self.fetchval(
            "SELECT count(*) FROM jobs WHERE status = $1 AND id < $2",
            JobStatus.QUEUED.value,
            job_id,
        )
        return int(value)

    async def _insert(self, job: NewJob, status: JobStatus) -> Job:
        row = await self.fetchrow(INSERT_JOB_SQL, *job_insert_args(job, 0, status))
        if row is None:
            raise RuntimeError("job insert returned no row")
        return to_job(row)


def to_job(row: asyncpg.Record) -> Job:
    return Job(
        id=row["id"],
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        status_message_id=row["status_message_id"],
        source_sha256=row["source_sha256"],
        source_key=row["source_key"],
        mode=Mode(row["mode"]),
        copies=row["copies"],
        status=JobStatus(row["status"]),
        credits_reserved=row["credits_reserved"],
        settled=row["settled_at"] is not None,
        audio_mode=AudioMode(row["audio_mode"]),
        source_duration=row["source_duration"],
        source_width=row["source_width"],
        source_height=row["source_height"],
        camera_profiles=row["camera_profiles"],
    )
