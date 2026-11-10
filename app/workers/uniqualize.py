from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any

import structlog

from app.core.config import settings
from app.core.exceptions import JobNotFoundError
from app.db.pool import get_pool
from app.db.repositories.billing import BillingRepository
from app.db.repositories.copies import CopyRepository
from app.db.repositories.jobs import JobRepository, JobStatus
from app.db.repositories.profiles import ProfileRepository
from app.services.cleanup import remove_expired
from app.services.fingerprints import QualityThresholds, Tools
from app.services.job_processor import JobProcessor, ProcessorConfig
from app.services.notifier import JobNotifier

logger = structlog.get_logger()

PROCESSOR_KEY = "processor"
NOTIFIER_KEY = "notifier"
JOBS_KEY = "jobs"
BILLING_KEY = "billing"


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
        render_slots=settings.render_slots,
        ffmpeg_threads=settings.ffmpeg_threads,
    )
    return JobProcessor(JobRepository(pool), CopyRepository(pool), config, ProfileRepository(pool))


async def uniqualize(ctx: dict[str, Any], job_id: int) -> dict[str, Any]:
    jobs: JobRepository = ctx[JOBS_KEY]
    billing: BillingRepository = ctx[BILLING_KEY]
    processor: JobProcessor = ctx[PROCESSOR_KEY]
    notifier: JobNotifier = ctx[NOTIFIER_KEY]
    job = await jobs.get(job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    if job.settled:  # an arq retry after the job was already paid out
        logger.info("job_already_settled", job_id=job_id)
        return {"job_id": job_id, "skipped": True}

    await notifier.processing(job)
    delivery = notifier.delivery(job)
    result = await processor.process(job_id, on_copy=delivery)
    # copies accepted before an arq retry were already sent by that attempt
    sent = delivery.sent + result.resumed
    settlement = await billing.settle_job(job_id, sent)
    await notifier.finish(job, sent, settlement)
    if sent > 0:
        reward = await billing.reward_referral(job.user_id, settings.referral_bonus)
        if reward is not None:
            await notifier.referral_rewarded(reward, settings.referral_bonus)
    return asdict(result) | {"delivered": sent}


async def cleanup_files(_: dict[str, Any]) -> int:
    removed = await asyncio.to_thread(
        remove_expired, Path(settings.work_dir), settings.files_ttl_hours * 3600
    )
    logger.info("cleanup_done", removed=removed)
    return removed


async def expire_credits(ctx: dict[str, Any]) -> int:
    billing: BillingRepository = ctx[BILLING_KEY]
    expired = await billing.expire_credits(settings.credit_ttl_days)
    logger.info("credits_expired", users=expired)
    return expired


async def refund_stale_jobs(ctx: dict[str, Any]) -> int:
    """A worker that died between rendering and settlement leaves credits reserved forever.
    Such jobs are closed as failed and fully refunded (user-favourable, never double-charges)."""
    jobs: JobRepository = ctx[JOBS_KEY]
    billing: BillingRepository = ctx[BILLING_KEY]
    notifier: JobNotifier = ctx[NOTIFIER_KEY]
    expired = await jobs.expire_drafts(settings.draft_ttl_minutes)
    if expired:
        logger.info("drafts_expired", count=expired)
    stale = await billing.stale_unsettled_jobs(
        settings.stale_finished_minutes, settings.stale_running_minutes
    )
    for job_id in stale:
        job = await jobs.get(job_id)
        if job is None:
            continue
        if job.status is JobStatus.PROCESSING:
            await jobs.set_status(job_id, JobStatus.FAILED)
        settlement = await billing.settle_job(job_id, delivered=0)
        if settlement is not None:
            logger.warning("stale_job_refunded", job_id=job_id, refunded=settlement.refunded)
            await notifier.finish(job, 0, settlement)
    return len(stale)
