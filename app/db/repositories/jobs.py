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


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    user_id: int
    source_sha256: str
    source_key: str
    mode: Mode
    copies: int
    status: JobStatus


@dataclass(frozen=True, slots=True)
class NewJob:
    user_id: int
    source_sha256: str
    source_key: str
    mode: Mode
    copies: int


class JobRepository(BaseRepository):
    async def create(self, job: NewJob) -> Job:
        row = await self.fetchrow(
            """
            INSERT INTO jobs (user_id, source_sha256, source_key, mode, copies)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            job.user_id,
            job.source_sha256,
            job.source_key,
            job.mode.value,
            job.copies,
        )
        if row is None:
            raise RuntimeError("job insert returned no row")
        return _to_job(row)

    async def get(self, job_id: int) -> Job | None:
        row = await self.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
        return _to_job(row) if row is not None else None

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


def _to_job(row: asyncpg.Record) -> Job:
    return Job(
        id=row["id"],
        user_id=row["user_id"],
        source_sha256=row["source_sha256"],
        source_key=row["source_key"],
        mode=Mode(row["mode"]),
        copies=row["copies"],
        status=JobStatus(row["status"]),
    )
