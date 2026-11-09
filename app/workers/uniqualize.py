from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.db.pool import get_pool
from app.db.repositories.copies import CopyRepository
from app.db.repositories.jobs import JobRepository
from app.services.fingerprints import QualityThresholds, Tools
from app.services.job_processor import JobProcessor, ProcessorConfig

PROCESSOR_KEY = "processor"


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
    processor: JobProcessor = ctx[PROCESSOR_KEY]
    result = await processor.process(job_id)
    return asdict(result)
