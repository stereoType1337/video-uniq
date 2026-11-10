"""SQL tests against a real Postgres (see the ``pool`` fixture in conftest)."""

from __future__ import annotations

import asyncpg
import pytest

from app.db.repositories.copies import AcceptedCopy, CopyRepository
from app.db.repositories.jobs import JobRepository, JobStatus, NewJob
from app.db.repositories.users import NewUser, User, UserRepository
from app.services.fingerprints import Fingerprint
from app.services.render_plan import Mode, SourceInfo, derive_seed, generate_plan


async def _user(pool: asyncpg.Pool, tg_id: int) -> User:
    user, _ = await UserRepository(pool).register(NewUser(tg_id, None, f"ref{tg_id}"))
    return user


async def test_job_and_copy_round_trip(pool: asyncpg.Pool) -> None:
    user = await _user(pool, 100)
    jobs, copies = JobRepository(pool), CopyRepository(pool)
    job = await jobs.create(NewJob(user.id, 100, 5, "abc", "src/in.mp4", Mode.HD, 2))
    assert job.status is JobStatus.QUEUED

    source = SourceInfo(width=1280, height=720, fps=30.0, duration=8.0, sample_rate=48000)
    plan = generate_plan(derive_seed("s", job.id, 0), 0, source, job.mode)
    fingerprint = Fingerprint(sha256="f0", phash=(2**64 - 1, 5), chromaprint=(2**32 - 1, 0))
    await copies.save_accepted(AcceptedCopy(job.id, plan, "jobs/1/0.mp4", fingerprint))

    assert await copies.done_keys(job.id) == {0: "jobs/1/0.mp4"}
    assert await copies.recent_fingerprints("abc", 10) == [fingerprint]
    assert await copies.recent_fingerprints("other", 10) == []

    await jobs.set_status(job.id, JobStatus.DONE)
    finished = await pool.fetchval("SELECT finished_at FROM jobs WHERE id = $1", job.id)
    assert finished is not None
    stored = await pool.fetchval("SELECT plan_json->>'seed' FROM copies")
    assert stored == plan.seed


async def test_duplicate_copy_index_is_rejected(pool: asyncpg.Pool) -> None:
    user = await _user(pool, 101)
    job = await JobRepository(pool).create(NewJob(user.id, 101, None, "x", "k", Mode.BASE, 1))
    source = SourceInfo(width=640, height=360, fps=25.0, duration=3.0, sample_rate=None)
    plan = generate_plan("seed", 0, source, Mode.BASE)
    copy = AcceptedCopy(job.id, plan, "k0", Fingerprint("h", (1,), None))
    repo = CopyRepository(pool)
    await repo.save_accepted(copy)
    with pytest.raises(asyncpg.UniqueViolationError):
        await repo.save_accepted(copy)
    assert await pool.fetchval("SELECT count(*) FROM fingerprints") == 1


async def test_active_count_and_queue_position(pool: asyncpg.Pool) -> None:
    jobs = JobRepository(pool)
    a, b = await _user(pool, 201), await _user(pool, 202)
    first = await jobs.create(NewJob(a.id, 201, None, "s1", "k1", Mode.BASE, 1))
    second = await jobs.create(NewJob(b.id, 202, None, "s2", "k2", Mode.BASE, 1))
    third = await jobs.create(NewJob(a.id, 201, None, "s3", "k3", Mode.BASE, 1))

    assert await jobs.count_active(a.id) == 2
    assert [await jobs.queue_position(j.id) for j in (first, second, third)] == [0, 1, 2]

    await jobs.set_status(first.id, JobStatus.PROCESSING)
    assert await jobs.queue_position(third.id) == 1
    await jobs.set_status(first.id, JobStatus.DONE)
    assert await jobs.count_active(a.id) == 1
