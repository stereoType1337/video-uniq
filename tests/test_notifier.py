from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import FSInputFile

from app.core import texts
from app.db.repositories.billing import Settlement
from app.db.repositories.jobs import Job, JobStatus
from app.services.job_processor import JobResult
from app.services.notifier import JobNotifier
from app.services.render_plan import Mode


class FakeBot:
    def __init__(self, fail_edit: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_edit = fail_edit

    async def send_document(self, chat_id: int, document: FSInputFile, **_: object) -> None:
        self.calls.append(("document", document.filename))

    async def edit_message_text(self, text: str, **_: object) -> None:
        if self.fail_edit:
            raise TelegramBadRequest(EditMessageText(text=text), "message to edit not found")
        self.calls.append(("edit", text))

    async def send_message(self, chat_id: int, text: str) -> None:
        self.calls.append(("message", text))


def _job(status_message_id: int | None = 5) -> Job:
    return Job(1, 1, 100, status_message_id, "sha", "src/a.mp4", Mode.BASE, 3, JobStatus.QUEUED)


def _notifier(bot: FakeBot, tmp_path: Path) -> JobNotifier:
    return JobNotifier(cast(Bot, bot), tmp_path)


async def test_delivers_documents_then_reports(tmp_path: Path) -> None:
    bot = FakeBot()
    notifier = _notifier(bot, tmp_path)
    result = JobResult(1, JobStatus.PARTIAL, ["jobs/1/0.mp4", "jobs/1/1.mp4"], [2])
    sent = await notifier.send_copies(_job(), result)
    await notifier.finish(_job(), sent, Settlement(1, reserved=3, charged=2, refunded=1, balance=7))
    assert sent == 2
    assert bot.calls == [
        ("document", "0.mp4"),
        ("document", "1.mp4"),
        ("edit", texts.done(2, 2, 7)),
        ("message", texts.partial(2, 1, 1)),
    ]


async def test_nothing_delivered_reports_failure_and_refund(tmp_path: Path) -> None:
    bot = FakeBot()
    settlement = Settlement(1, reserved=3, charged=0, refunded=3, balance=10)
    await _notifier(bot, tmp_path).finish(_job(), 0, settlement)
    assert bot.calls == [("edit", texts.failed(3))]


async def test_falls_back_to_new_message_when_edit_fails(tmp_path: Path) -> None:
    bot = FakeBot(fail_edit=True)
    await _notifier(bot, tmp_path).processing(_job())
    assert bot.calls == [("message", texts.processing(3))]


async def test_delivery_sends_each_copy_with_progress(tmp_path: Path) -> None:
    bot = FakeBot()
    delivery = _notifier(bot, tmp_path).delivery(_job())
    await delivery("jobs/1/2.mp4")
    await delivery("jobs/1/0.mp4")
    assert delivery.sent == 2
    assert bot.calls == [
        ("document", "2.mp4"),
        ("edit", texts.progress(1, 3)),
        ("document", "0.mp4"),
        ("edit", texts.progress(2, 3)),
    ]
