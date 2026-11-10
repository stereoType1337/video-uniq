from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from app.db.repositories.copies import AcceptedCopy
from app.db.repositories.jobs import Job, JobStatus
from app.services import job_processor as jp
from app.services.fingerprints import Fingerprint, QualityThresholds, file_sha256, phash_distance
from app.services.job_processor import JobProcessor, ProcessorConfig
from app.services.probe import probe_source
from app.services.render_plan import AudioMode, Mode

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
        chat_id=100,
        status_message_id=None,
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


async def test_parallel_copies_respect_slots_and_stay_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, peak = 0, 0
    real_render = jp.render_copy

    async def tracking_render(*args: object, **kwargs: object) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await real_render(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            active -= 1

    monkeypatch.setattr(jp, "render_copy", tracking_render)
    processor, _, store = _processor(tmp_path, copies=5)
    processor._config = replace(processor._config, render_slots=3, ffmpeg_threads=1)
    processor._render_slots = asyncio.Semaphore(3)

    result = await processor.process(7)

    assert result.status is JobStatus.DONE and len(result.output_keys) == 5
    assert result.output_keys == [f"jobs/7/{i}.mp4" for i in range(5)], "ordered by index"
    assert 2 <= peak <= 3, f"renders must overlap but never exceed the slots (peak={peak})"
    prints = [c.fingerprint.phash for c in store.saved]
    pairs = [phash_distance(a, b) for k, a in enumerate(prints) for b in prints[k + 1 :]]
    assert min(pairs) >= _THRESHOLDS.min_phash_to_copies, "gate saw every accepted copy"


async def test_copies_are_reported_as_they_are_accepted(tmp_path: Path) -> None:
    processor, _, store = _processor(tmp_path, copies=3)
    reported: list[tuple[str, int]] = []

    async def on_copy(key: str) -> None:
        reported.append((key, len(store.saved)))

    result = await processor.process(7, on_copy=on_copy)
    assert sorted(k for k, _ in reported) == result.output_keys
    assert all(saved >= 1 for _, saved in reported), "reported only after being saved"


@pytest.mark.parametrize("audio_mode", [AudioMode.MUTE, AudioMode.KEEP])
async def test_sound_choice_is_honoured_and_not_gated(
    tmp_path: Path, audio_mode: AudioMode
) -> None:
    processor, jobs, _ = _processor(tmp_path, copies=2)
    jobs.job = replace(jobs.job, audio_mode=audio_mode)
    result = await processor.process(7)
    assert result.status is JobStatus.DONE, "keeping the sound must not fail the audio check"
    out = await probe_source(tmp_path / result.output_keys[0])
    assert (out.sample_rate is None) is (audio_mode is AudioMode.MUTE)
