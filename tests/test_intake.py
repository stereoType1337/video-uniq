from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.exceptions import (
    FileTooLargeError,
    InsufficientBalanceError,
    ProbeError,
    TooManyActiveJobsError,
)
from app.db.repositories.jobs import Job, JobStatus, NewJob
from app.db.repositories.users import User
from app.services.billing import Account
from app.services.fingerprints import file_sha256
from app.services.intake import IntakeConfig, IntakeDeps, IntakeRequest, IntakeService
from app.services.render_plan import Mode
from app.services.video_rules import RejectReason, VideoRejectedError

pytestmark = pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is not installed")

_MB = 1024 * 1024


class FakeAccounts:
    def __init__(self, balance: int = 100) -> None:
        self.balance = balance

    async def ensure(
        self, tg_id: int, username: str | None, start_arg: str | None = None
    ) -> Account:
        user = User(1, tg_id, username, self.balance, Mode.HD, 5, "ref", None)
        return Account(user, created=False, trial_credits=0)


class FakeJobs:
    def __init__(self, active: int = 0) -> None:
        self.created: list[NewJob] = []
        self.active = active

    async def create_draft(self, job: NewJob) -> Job:
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
            status=JobStatus.DRAFT,
            audio_mode=job.audio_mode,
        )

    async def count_active(self, user_id: int) -> int:
        return self.active


def _make_clip(path: Path, duration: float, fmt: str = "mp4") -> Path:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=320x568:rate=30:duration={duration}",
            "-c:v", "libx264" if fmt == "mp4" else "mpeg4", "-f", fmt, str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("clips")
    return {
        "ok": _make_clip(root / "ok.mp4", 1.0),
        "short": _make_clip(root / "short.mp4", 0.2),
        "mkv": _make_clip(root / "clip.mkv", 1.0, fmt="matroska"),
    }


class FakeFetcher:
    def __init__(self, source: Path | None = None, payload: bytes | None = None) -> None:
        self.source = source
        self.payload = payload

    async def fetch(self, file_id: str, dst: Path) -> None:
        if self.payload is not None:
            dst.write_bytes(self.payload)
        elif self.source is not None:
            shutil.copyfile(self.source, dst)


def _service(
    tmp_path: Path, jobs: FakeJobs, fetcher: FakeFetcher, balance: int = 100
) -> IntakeService:
    deps = IntakeDeps(accounts=FakeAccounts(balance), jobs=jobs, fetcher=fetcher)
    return IntakeService(deps, IntakeConfig(tmp_path, max_file_mb=1, max_active_jobs=2))


def _request(size: int | None = 100, duration: int | None = None) -> IntakeRequest:
    return IntakeRequest(
        tg_id=42,
        username="u",
        chat_id=42,
        status_message_id=9,
        file_id="f",
        file_size=size,
        duration=duration,
    )


async def test_accept_stores_source_and_enqueues_after_status(
    tmp_path: Path, clips: dict[str, Path]
) -> None:
    jobs = FakeJobs()
    service = _service(tmp_path, jobs, FakeFetcher(clips["ok"]))
    result = await service.accept(_request())
    job = result.draft

    assert job.status is JobStatus.DRAFT, "credits are taken only when the card is started"
    assert jobs.created[0].source is not None and jobs.created[0].source.width == 320
    stored = tmp_path / job.source_key
    assert stored.read_bytes() == clips["ok"].read_bytes()
    assert job.source_sha256 == file_sha256(stored)
    assert (job.mode, job.copies, job.chat_id, job.status_message_id) == (Mode.HD, 5, 42, 9)


async def test_rejects_when_balance_too_low_before_download(tmp_path: Path) -> None:
    jobs = FakeJobs()
    fetcher = FakeFetcher(payload=b"must not be fetched")
    service = _service(tmp_path, jobs, fetcher, balance=0)
    with pytest.raises(InsufficientBalanceError) as info:
        await service.accept(_request())
    assert (info.value.required, info.value.balance) == (1, 0), "not even one Base copy"
    assert not (tmp_path / "src").exists() and not jobs.created


async def test_rejects_large_file_before_download(tmp_path: Path) -> None:
    jobs = FakeJobs()
    service = _service(tmp_path, jobs, FakeFetcher(payload=b""))
    with pytest.raises(FileTooLargeError):
        await service.accept(_request(size=2 * _MB))
    assert not jobs.created


async def test_rejects_large_file_when_size_unknown(tmp_path: Path) -> None:
    jobs = FakeJobs()
    service = _service(tmp_path, jobs, FakeFetcher(payload=b"x" * (2 * _MB)))
    with pytest.raises(FileTooLargeError):
        await service.accept(_request(size=None))
    assert not jobs.created


async def test_limits_active_jobs(tmp_path: Path) -> None:
    jobs = FakeJobs(active=2)
    service = _service(tmp_path, jobs, FakeFetcher(payload=b""))
    with pytest.raises(TooManyActiveJobsError):
        await service.accept(_request())
    assert not jobs.created


async def test_rejects_long_video_before_download(tmp_path: Path) -> None:
    jobs = FakeJobs()
    fetcher = FakeFetcher(payload=b"must not be fetched")
    with pytest.raises(VideoRejectedError) as info:
        await _service(tmp_path, jobs, fetcher).accept(_request(duration=600))
    assert info.value.rejection.reason is RejectReason.TOO_LONG
    assert not (tmp_path / "src").exists()


@pytest.mark.parametrize(
    ("clip", "reason"), [("short", RejectReason.TOO_SHORT), ("mkv", RejectReason.BAD_FORMAT)]
)
async def test_rejects_after_probe_and_removes_file(
    tmp_path: Path, clips: dict[str, Path], clip: str, reason: RejectReason
) -> None:
    jobs = FakeJobs()
    service = _service(tmp_path, jobs, FakeFetcher(clips[clip]))
    with pytest.raises(VideoRejectedError) as info:
        await service.accept(_request())
    assert info.value.rejection.reason is reason
    assert not list((tmp_path / "src").iterdir()), "rejected source must be deleted"
    assert not jobs.created


async def test_unreadable_file_is_removed(tmp_path: Path) -> None:
    jobs = FakeJobs()
    service = _service(tmp_path, jobs, FakeFetcher(payload=b"not a video at all"))
    with pytest.raises(ProbeError):
        await service.accept(_request())
    assert not list((tmp_path / "src").iterdir())
