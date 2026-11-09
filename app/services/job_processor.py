"""One job = N copies of one source: render, fingerprint, gate, persist.

A rejected or failed attempt is re-rendered with seed index ``copy_index + 1000 * attempt``
(ARCHITECTURE §2). Already accepted copies are skipped, so an arq retry resumes the job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import structlog

from app.core.exceptions import JobNotFoundError, ProbeError, RenderError
from app.db.repositories.copies import AcceptedCopy
from app.db.repositories.jobs import Job, JobStatus
from app.services.fingerprints import (
    Fingerprint,
    QualityThresholds,
    Tools,
    compute_fingerprint,
    evaluate_copy,
)
from app.services.probe import probe_source
from app.services.render_plan import RenderPlan, SourceInfo, derive_seed, generate_plan
from app.services.uniqualizer import render_copy

logger = structlog.get_logger()

_ATTEMPT_STRIDE = 1000


class JobStore(Protocol):
    async def get(self, job_id: int) -> Job | None: ...
    async def set_status(self, job_id: int, status: JobStatus) -> None: ...


class CopyStore(Protocol):
    async def save_accepted(self, copy: AcceptedCopy) -> int: ...
    async def done_indexes(self, job_id: int) -> set[int]: ...
    async def recent_fingerprints(self, source_sha256: str, limit: int) -> list[Fingerprint]: ...


@dataclass(frozen=True, slots=True)
class ProcessorConfig:
    secret: str
    work_dir: Path
    thresholds: QualityThresholds
    tools: Tools = field(default_factory=Tools)
    ffprobe_bin: str = "ffprobe"
    max_attempts: int = 4
    history_limit: int = 500


@dataclass(frozen=True, slots=True)
class JobResult:
    job_id: int
    status: JobStatus
    output_keys: list[str]
    failed_indexes: list[int]


@dataclass(slots=True)
class _JobContext:
    job: Job
    source_path: Path
    source: SourceInfo
    original: Fingerprint
    history: list[Fingerprint]
    out_dir: Path


class JobProcessor:
    def __init__(self, jobs: JobStore, copies: CopyStore, config: ProcessorConfig) -> None:
        self._jobs = jobs
        self._copies = copies
        self._config = config

    async def process(self, job_id: int) -> JobResult:
        job = await self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        await self._jobs.set_status(job_id, JobStatus.PROCESSING)
        try:
            ctx = await self._prepare(job)
        except ProbeError:
            logger.exception("source_unreadable", job_id=job_id)
            await self._jobs.set_status(job_id, JobStatus.FAILED)
            return JobResult(job_id, JobStatus.FAILED, [], list(range(job.copies)))

        done = await self._copies.done_indexes(job_id)
        outputs = [self._output_key(job_id, i) for i in sorted(done)]
        failed: list[int] = []
        for index in range(job.copies):
            if index in done:
                continue
            key = await self._make_copy(ctx, index)
            if key is None:
                failed.append(index)
            else:
                outputs.append(key)

        status = _final_status(len(outputs), len(failed))
        await self._jobs.set_status(job_id, status)
        logger.info("job_finished", job_id=job_id, status=status, ok=len(outputs))
        return JobResult(job_id, status, outputs, failed)

    async def _prepare(self, job: Job) -> _JobContext:
        source_path = self._config.work_dir / job.source_key
        source = await probe_source(source_path, self._config.ffprobe_bin)
        original = await compute_fingerprint(
            source_path, source.duration, source.has_audio, self._config.tools
        )
        history = await self._copies.recent_fingerprints(
            job.source_sha256, self._config.history_limit
        )
        out_dir = self._config.work_dir / "jobs" / str(job.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        return _JobContext(job, source_path, source, original, history, out_dir)

    async def _make_copy(self, ctx: _JobContext, index: int) -> str | None:
        for attempt in range(self._config.max_attempts):
            seed = derive_seed(self._config.secret, ctx.job.id, index + _ATTEMPT_STRIDE * attempt)
            plan = generate_plan(seed, index, ctx.source, ctx.job.mode)
            tmp = ctx.out_dir / f"{index}.attempt{attempt}.mp4"
            fingerprint = await self._render_checked(ctx, plan, tmp, attempt)
            if fingerprint is None:
                tmp.unlink(missing_ok=True)
                continue
            key = self._output_key(ctx.job.id, index)
            tmp.replace(self._config.work_dir / key)
            await self._copies.save_accepted(AcceptedCopy(ctx.job.id, plan, key, fingerprint))
            ctx.history.append(fingerprint)
            return key
        logger.warning("copy_failed", job_id=ctx.job.id, copy_index=index)
        return None

    async def _render_checked(
        self, ctx: _JobContext, plan: RenderPlan, dst: Path, attempt: int
    ) -> Fingerprint | None:
        try:
            await render_copy(plan, ctx.source_path, dst, self._config.tools.ffmpeg_bin)
            duration = (ctx.source.duration - plan.time.start_offset) / plan.time.speed
            fingerprint = await compute_fingerprint(
                dst, duration, ctx.source.has_audio, self._config.tools
            )
        except (RenderError, ProbeError):
            logger.exception("attempt_error", job_id=ctx.job.id, copy_index=plan.copy_index)
            return None
        verdict = evaluate_copy(fingerprint, ctx.original, ctx.history, self._config.thresholds)
        logger.info(
            "copy_verdict",
            job_id=ctx.job.id,
            copy_index=plan.copy_index,
            attempt=attempt,
            accepted=verdict.accepted,
            reason=verdict.reason,
            phash_to_original=round(verdict.phash_to_original, 2),
            phash_to_copy=_round(verdict.phash_to_nearest_copy),
            audio_to_original=_round(verdict.audio_to_original),
        )
        return fingerprint if verdict.accepted else None

    @staticmethod
    def _output_key(job_id: int, index: int) -> str:
        return f"jobs/{job_id}/{index}.mp4"


def _final_status(ok: int, failed: int) -> JobStatus:
    if failed == 0:
        return JobStatus.DONE
    return JobStatus.PARTIAL if ok > 0 else JobStatus.FAILED


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None
