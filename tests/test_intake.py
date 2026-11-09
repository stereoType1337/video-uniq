from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.exceptions import FileTooLargeError, TooManyActiveJobsError
from app.db.repositories.jobs import Job, JobStatus, NewJob
from app.services.fingerprints import file_sha256
from app.services.intake import IntakeConfig, IntakeDeps, IntakeRequest, IntakeService
from app.services.render_plan import Mode

_MB = 1024 * 1024


class FakeUsers:
    async def upsert(self, tg_id: int, username: str | None, ref_code: str) -> dict[str, Any]:
        return {"id": 1, "tg_id": tg_id, "mode": "hd", "copies": 5}


class FakeJobs:
    def __init__(self, active: int = 0, ahead: int = 0) -> None:
        self.created: list[NewJob] = []
        self.active = active
        self.ahead = ahead

    async def create(self, job: NewJob) -> Job:
        self.created.append(job)
        return Job(
            id=len(self.created),
            user_id=job.user_id,
            chat_id=job.chat_id,
            status_message_id=job.status_message_id,
            source_sha256=job.source_sha256,
            source_key=job.source_key,
            mode=job.mode,
            copies=job.copies,
            status=JobStatus.QUEUED,
        )

    async def count_active(self, user_id: int) -> int:
        return self.active

    async def queue_position(self, job_id: int) -> int:
        return self.ahead


class FakeFetcher:
    def __init__(self, payload: bytes = b"video-bytes") -> None:
        self.payload = payload

    async def fetch(self, file_id: str, dst: Path) -> None:
        dst.write_bytes(self.payload)


class EventLog:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def enqueue(self, job_id: int) -> None:
        self.events.append(f"enqueue:{job_id}")

    async def on_queued(self, job: Job, position: int) -> None:
        self.events.append(f"queued:{job.id}:{position}")


def _service(
    tmp_path: Path, jobs: FakeJobs, log: EventLog, fetcher: FakeFetcher | None = None
) -> IntakeService:
    deps = IntakeDeps(
        users=FakeUsers(),  # type: ignore[arg-type]
        jobs=jobs,
        fetcher=fetcher or FakeFetcher(),
        queue=log,
    )
    return IntakeService(deps, IntakeConfig(tmp_path, max_file_mb=1, max_active_jobs=2))


def _request(size: int | None = 100) -> IntakeRequest:
    return IntakeRequest(
        tg_id=42, username="u", chat_id=42, status_message_id=9, file_id="f", file_size=size
    )


async def test_accept_stores_source_and_enqueues_after_status(tmp_path: Path) -> None:
    jobs, log = FakeJobs(ahead=3), EventLog()
    job = await _service(tmp_path, jobs, log).accept(_request(), log.on_queued)

    assert log.events == ["queued:1:3", "enqueue:1"], "status must be shown before enqueue"
    stored = tmp_path / job.source_key
    assert stored.read_bytes() == b"video-bytes"
    assert job.source_sha256 == file_sha256(stored)
    assert (job.mode, job.copies, job.chat_id, job.status_message_id) == (Mode.HD, 5, 42, 9)


async def test_rejects_large_file_before_download(tmp_path: Path) -> None:
    jobs, log = FakeJobs(), EventLog()
    with pytest.raises(FileTooLargeError):
        await _service(tmp_path, jobs, log).accept(_request(size=2 * _MB), log.on_queued)
    assert not jobs.created and not log.events


async def test_rejects_large_file_when_size_unknown(tmp_path: Path) -> None:
    jobs, log = FakeJobs(), EventLog()
    service = _service(tmp_path, jobs, log, FakeFetcher(b"x" * (2 * _MB)))
    with pytest.raises(FileTooLargeError):
        await service.accept(_request(size=None), log.on_queued)
    assert not jobs.created


async def test_limits_active_jobs(tmp_path: Path) -> None:
    jobs, log = FakeJobs(active=2), EventLog()
    with pytest.raises(TooManyActiveJobsError):
        await _service(tmp_path, jobs, log).accept(_request(), log.on_queued)
    assert not log.events
