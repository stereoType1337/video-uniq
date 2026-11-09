from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.db.repositories.copies import AcceptedCopy
from app.db.repositories.jobs import Job, JobStatus
from app.services.fingerprints import Fingerprint, QualityThresholds, file_sha256
from app.services.job_processor import JobProcessor, ProcessorConfig
from app.services.render_plan import Mode

pytestmark = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("ffmpeg", "ffprobe", "fpcalc")),
    reason="ffmpeg/ffprobe/fpcalc are not installed",
)

_THRESHOLDS = QualityThresholds(
    min_phash_to_original=6.0, min_phash_to_copies=5.0, min_audio_to_original=0.1
)


class FakeJobs:
    def __init__(self, job: Job) -> None:
        self.job = job
        self.statuses: list[JobStatus] = []

    async def get(self, job_id: int) -> Job | None:
        return self.job if job_id == self.job.id else None

    async def set_status(self, job_id: int, status: JobStatus) -> None:
        self.statuses.append(status)


class FakeCopies:
    def __init__(self) -> None:
        self.saved: list[AcceptedCopy] = []

    async def save_accepted(self, copy: AcceptedCopy) -> int:
        self.saved.append(copy)
        return len(self.saved)

    async def done_indexes(self, job_id: int) -> set[int]:
        return {c.plan.copy_index for c in self.saved if c.job_id == job_id}

    async def recent_fingerprints(self, source_sha256: str, limit: int) -> list[Fingerprint]:
        return [c.fingerprint for c in self.saved][-limit:]


def _make_source(work_dir: Path) -> Path:
    path = work_dir / "src" / "in.mp4"
    path.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "mandelbrot=size=640x360:rate=30,hue=H=t/2",
            "-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=48000:seed=1,lowpass=f=3000",
            "-t", "4", "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def _processor(
    work_dir: Path, copies: int, thresholds: QualityThresholds = _THRESHOLDS
) -> tuple[JobProcessor, FakeJobs, FakeCopies]:
    source = _make_source(work_dir)
    job = Job(
        id=7,
        user_id=1,
        source_sha256=file_sha256(source),
        source_key="src/in.mp4",
        mode=Mode.BASE,
        copies=copies,
        status=JobStatus.QUEUED,
    )
    jobs, store = FakeJobs(job), FakeCopies()
    config = ProcessorConfig(secret="test", work_dir=work_dir, thresholds=thresholds)
    return JobProcessor(jobs, store, config), jobs, store


async def test_job_produces_accepted_unique_copies(tmp_path: Path) -> None:
    processor, jobs, store = _processor(tmp_path, copies=3)
    result = await processor.process(7)

    assert result.status is JobStatus.DONE
    assert result.output_keys == ["jobs/7/0.mp4", "jobs/7/1.mp4", "jobs/7/2.mp4"]
    assert all((tmp_path / key).is_file() for key in result.output_keys)
    assert jobs.statuses == [JobStatus.PROCESSING, JobStatus.DONE]
    assert len({c.fingerprint.sha256 for c in store.saved}) == 3
    assert not list((tmp_path / "jobs" / "7").glob("*.attempt*.mp4")), "temp files left"


async def test_retry_resumes_without_rerendering(tmp_path: Path) -> None:
    processor, _, store = _processor(tmp_path, copies=2)
    await processor.process(7)
    first = list(store.saved)
    result = await processor.process(7)
    assert store.saved == first
    assert result.status is JobStatus.DONE and len(result.output_keys) == 2


async def test_impossible_threshold_fails_job(tmp_path: Path) -> None:
    strict = QualityThresholds(
        min_phash_to_original=65.0, min_phash_to_copies=0.0, min_audio_to_original=0.0
    )
    processor, jobs, store = _processor(tmp_path, copies=1, thresholds=strict)
    result = await processor.process(7)
    assert result.status is JobStatus.FAILED
    assert result.failed_indexes == [0] and not store.saved
    assert jobs.statuses[-1] is JobStatus.FAILED


async def test_unreadable_source_fails_job(tmp_path: Path) -> None:
    processor, _, _ = _processor(tmp_path, copies=1)
    (tmp_path / "src" / "in.mp4").write_bytes(b"not a video")
    result = await processor.process(7)
    assert result.status is JobStatus.FAILED
