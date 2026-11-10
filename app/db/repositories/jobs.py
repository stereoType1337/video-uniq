from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import asyncpg

from app.db.base import BaseRepository
from app.services.render_plan import Mode


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"


_FINAL_STATUSES = (JobStatus.DONE, JobStatus.PARTIAL, JobStatus.FAILED)
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


@dataclass(frozen=True, slots=True)
class NewJob:
    user_id: int
    chat_id: int
    status_message_id: int | None
    source_sha256: str
    source_key: str
    mode: Mode
    copies: int


INSERT_JOB_SQL = """
    INSERT INTO jobs (user_id, chat_id, status_message_id, source_sha256, source_key,
                      mode, copies, credits_reserved)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    RETURNING *
"""


def job_insert_args(job: NewJob, credits_reserved: int) -> tuple[object, ...]:
    return (
        job.user_id,
        job.chat_id,
        job.status_message_id,
        job.source_sha256,
        job.source_key,
        job.mode.value,
        job.copies,
        credits_reserved,
    )


class JobRepository(BaseRepository):
    async def create(self, job: NewJob) -> Job:
        """Create a job without reserving credits (billing uses create_job_reserving)."""
        row = await self.fetchrow(INSERT_JOB_SQL, *job_insert_args(job, 0))
        if row is None:
            raise RuntimeError("job insert returned no row")
        return to_job(row)

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
    )
