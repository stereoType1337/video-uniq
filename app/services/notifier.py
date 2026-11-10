"""Reporting job progress and delivering copies back to the user's chat."""

from __future__ import annotations

from pathlib import Path

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile

from app.core import texts
from app.db.repositories.billing import ReferralReward, Settlement
from app.db.repositories.jobs import Job
from app.services.job_processor import JobResult

logger = structlog.get_logger()


class JobNotifier:
    def __init__(self, bot: Bot, work_dir: Path) -> None:
        self._bot = bot
        self._work_dir = work_dir

    async def processing(self, job: Job) -> None:
        await self._status(job, texts.processing(job.copies))

    async def send_copies(self, job: Job, result: JobResult) -> int:
        """Returns how many copies actually reached the user: only those are charged."""
        sent = 0
        for key in result.output_keys:
            sent += await self._send_copy(job, key)
        return sent

    async def finish(self, job: Job, sent: int, settlement: Settlement | None) -> None:
        refunded = settlement.refunded if settlement is not None else 0
        if sent == 0:
            await self._status(job, texts.failed(refunded))
            return
        charged = settlement.charged if settlement is not None else 0
        balance = settlement.balance if settlement is not None else None
        await self._status(job, texts.done(sent, charged, balance))
        if sent < job.copies:
            await self._message(job, texts.partial(sent, job.copies - sent, refunded))

    async def referral_rewarded(self, reward: ReferralReward, bonus: int) -> None:
        for chat_id, text in (
            (reward.referrer_tg_id, texts.referral_rewarded(bonus)),
            (reward.referred_tg_id, texts.referral_welcome_bonus(bonus)),
        ):
            try:
                await self._bot.send_message(chat_id, text)
            except TelegramAPIError:
                logger.info("referral_notify_failed", chat_id=chat_id)

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
        except TelegramForbiddenError:
            logger.info("user_blocked_bot", job_id=job.id)
        except TelegramAPIError:
            logger.exception("notify_failed", job_id=job.id)
