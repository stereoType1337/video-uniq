from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

import structlog

from app.core.config import settings
from app.core.exceptions import JobNotFoundError
from app.db.pool import get_pool
from app.db.repositories.copies import CopyRepository
from app.db.repositories.jobs import JobRepository
from app.services.cleanup import remove_expired
from app.services.fingerprints import QualityThresholds, Tools
from app.services.job_processor import JobProcessor, ProcessorConfig
from app.services.notifier import JobNotifier

logger = structlog.get_logger()

PROCESSOR_KEY = "processor"
NOTIFIER_KEY = "notifier"
JOBS_KEY = "jobs"


def build_processor() -> JobProcessor:
    pool = get_pool()
    config = ProcessorConfig(
        secret=settings.service_secret,
        work_dir=Path(settings.work_dir),
        thresholds=QualityThresholds(
            min_phash_to_original=settings.min_phash_to_original,
            min_phash_to_copies=settings.min_phash_to_copies,
            min_audio_to_original=settings.min_audio_to_original,
        ),
        tools=Tools(ffmpeg_bin=settings.ffmpeg_bin, fpcalc_bin=settings.fpcalc_bin),
        ffprobe_bin=settings.ffprobe_bin,
        max_attempts=settings.max_render_attempts,
        history_limit=settings.fingerprint_history,
    )
    return JobProcessor(JobRepository(pool), CopyRepository(pool), config)


async def uniqualize(ctx: dict[str, Any], job_id: int) -> dict[str, Any]:
    jobs: JobRepository = ctx[JOBS_KEY]
    processor: JobProcessor = ctx[PROCESSOR_KEY]
    notifier: JobNotifier = ctx[NOTIFIER_KEY]
    job = await jobs.get(job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    await notifier.processing(job)
    result = await processor.process(job_id)
    await notifier.deliver(job, result)
    return asdict(result)


async def cleanup_files(_: dict[str, Any]) -> int:
    removed = await asyncio.to_thread(
        remove_expired, Path(settings.work_dir), settings.files_ttl_hours * 3600
    )
    logger.info("cleanup_done", removed=removed)
    return removed
