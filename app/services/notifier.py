"""Reporting job progress and delivering copies back to the user's chat."""

from __future__ import annotations

from pathlib import Path

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import FSInputFile

from app.core import texts
from app.db.repositories.jobs import Job, JobStatus
from app.services.job_processor import JobResult

logger = structlog.get_logger()


class JobNotifier:
    def __init__(self, bot: Bot, work_dir: Path) -> None:
        self._bot = bot
        self._work_dir = work_dir

    async def processing(self, job: Job) -> None:
        await self._status(job, texts.processing(job.copies))

    async def deliver(self, job: Job, result: JobResult) -> None:
        sent = 0
        for key in result.output_keys:
            sent += await self._send_copy(job, key)
        if result.status is JobStatus.FAILED or sent == 0:
            await self._status(job, texts.FAILED)
            return
        await self._status(job, texts.done(sent))
        if result.failed_indexes:
            await self._message(job, texts.partial(sent, len(result.failed_indexes)))

    async def _send_copy(self, job: Job, key: str) -> int:
        path = self._work_dir / key
        try:
            # As a document: Telegram keeps the file byte-for-byte, metadata included.
            await self._bot.send_document(
                job.chat_id,
                FSInputFile(path, filename=path.name),
                disable_content_type_detection=True,
            )
        except TelegramAPIError:
            logger.exception("copy_send_failed", job_id=job.id, key=key)
            return 0
        return 1

    async def _status(self, job: Job, text: str) -> None:
        if job.status_message_id is not None:
            try:
                await self._bot.edit_message_text(
                    text, chat_id=job.chat_id, message_id=job.status_message_id
                )
                return
            except TelegramBadRequest:
                logger.debug("status_edit_failed", job_id=job.id, exc_info=True)
        await self._message(job, text)

    async def _message(self, job: Job, text: str) -> None:
        try:
            await self._bot.send_message(job.chat_id, text)
        except TelegramAPIError:
            logger.exception("notify_failed", job_id=job.id)
