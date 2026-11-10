"""One job = N copies of one source: render, fingerprint, gate, persist.

A rejected or failed attempt is re-rendered with seed index ``copy_index + 1000 * attempt``
(ARCHITECTURE §2). Already accepted copies are skipped, so an arq retry resumes the job.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
from app.services.render_plan import (
    AudioMode,
    RenderPlan,
    SourceInfo,
    apply_audio_mode,
    derive_seed,
    generate_plan,
)
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
    render_slots: int = 1  # concurrent FFmpeg renders per worker process, shared by all jobs
    ffmpeg_threads: int = 0  # threads per render; 0 = FFmpeg default (all cores)


@dataclass(frozen=True, slots=True)
class JobResult:
    job_id: int
    status: JobStatus
    output_keys: list[str]
    failed_indexes: list[int]
    resumed: int = 0  # copies accepted (and delivered) by an earlier attempt of this job


OnCopy = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class _JobContext:
    job: Job
    source_path: Path
    source: SourceInfo
    original: Fingerprint
    history: list[Fingerprint]
    out_dir: Path
    # Copies render in parallel, but each verdict must see every copy accepted before it.
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)


class JobProcessor:
    def __init__(self, jobs: JobStore, copies: CopyStore, config: ProcessorConfig) -> None:
        self._jobs = jobs
        self._copies = copies
        self._config = config
        self._render_slots = asyncio.Semaphore(max(config.render_slots, 1))

    async def process(self, job_id: int, on_copy: OnCopy | None = None) -> JobResult:
        """``on_copy(key)`` is awaited as soon as each copy is accepted (progressive delivery)."""
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
        pending = [i for i in range(job.copies) if i not in done]
        keys = await asyncio.gather(*(self._make_and_report(ctx, i, on_copy) for i in pending))
        made = dict(zip(pending, keys, strict=True))
        outputs = [
            self._output_key(job_id, i) for i in range(job.copies) if i in done or made.get(i)
        ]
        failed = [i for i in pending if made[i] is None]

        status = _final_status(len(outputs), len(failed))
        await self._jobs.set_status(job_id, status)
        logger.info("job_finished", job_id=job_id, status=status, ok=len(outputs))
        return JobResult(job_id, status, outputs, failed, resumed=len(done))

    async def _prepare(self, job: Job) -> _JobContext:
        source_path = self._config.work_dir / job.source_key
        source = await probe_source(source_path, self._config.ffprobe_bin)
        changes_sound = job.audio_mode is AudioMode.SHIFT
        original = await compute_fingerprint(
            source_path, source.duration, source.has_audio and changes_sound, self._config.tools
        )
        history = await self._copies.recent_fingerprints(
            job.source_sha256, self._config.history_limit
        )
        out_dir = self._config.work_dir / "jobs" / str(job.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        return _JobContext(job, source_path, source, original, history, out_dir)

    async def _make_and_report(
        self, ctx: _JobContext, index: int, on_copy: OnCopy | None
    ) -> str | None:
        key = await self._make_copy(ctx, index)
        if key is not None and on_copy is not None:
            await on_copy(key)
        return key

    async def _make_copy(self, ctx: _JobContext, index: int) -> str | None:
        for attempt in range(self._config.max_attempts):
            seed = derive_seed(self._config.secret, ctx.job.id, index + _ATTEMPT_STRIDE * attempt)
            plan = apply_audio_mode(
                generate_plan(seed, index, ctx.source, ctx.job.mode), ctx.job.audio_mode
            )
            tmp = ctx.out_dir / f"{index}.attempt{attempt}.mp4"
            fingerprint = await self._render(ctx, plan, tmp)
            if fingerprint is not None:
                async with ctx.gate:
                    if self._accepted(ctx, plan, fingerprint, attempt):
                        key = self._output_key(ctx.job.id, index)
                        tmp.replace(self._config.work_dir / key)
                        copy = AcceptedCopy(ctx.job.id, plan, key, fingerprint)
                        await self._copies.save_accepted(copy)
                        ctx.history.append(fingerprint)
                        return key
            tmp.unlink(missing_ok=True)
        logger.warning("copy_failed", job_id=ctx.job.id, copy_index=index)
        return None

    async def _render(self, ctx: _JobContext, plan: RenderPlan, dst: Path) -> Fingerprint | None:
        tools = self._config.tools
        try:
            async with self._render_slots:
                await render_copy(
                    plan, ctx.source_path, dst, tools.ffmpeg_bin, self._config.ffmpeg_threads
                )
            duration = (ctx.source.duration - plan.time.start_offset) / plan.time.speed
            return await compute_fingerprint(dst, duration, plan.audio is not None, tools)
        except (RenderError, ProbeError):
            logger.exception("attempt_error", job_id=ctx.job.id, copy_index=plan.copy_index)
            return None

    def _accepted(
        self, ctx: _JobContext, plan: RenderPlan, fingerprint: Fingerprint, attempt: int
    ) -> bool:
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
        return verdict.accepted

    @staticmethod
    def _output_key(job_id: int, index: int) -> str:
        return f"jobs/{job_id}/{index}.mp4"


def _final_status(ok: int, failed: int) -> JobStatus:
    if failed == 0:
        return JobStatus.DONE
    return JobStatus.PARTIAL if ok > 0 else JobStatus.FAILED


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None
