from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from app.core import texts
from app.core.config import settings
from app.core.exceptions import FileTooLargeError, SourceDownloadError, TooManyActiveJobsError
from app.db.repositories.jobs import Job
from app.services.intake import IntakeRequest, IntakeService

router = Router(name="upload")

_VIDEO_DOCUMENT = F.document.mime_type.startswith("video/")


@router.message(F.video | _VIDEO_DOCUMENT)
async def handle_video(message: Message, intake: IntakeService) -> None:
    media = message.video or message.document
    if media is None or message.from_user is None:
        return
    status = await message.answer(texts.DOWNLOADING)
    hint = texts.COMPRESSED_HINT if message.video is not None else ""

    async def on_queued(job: Job, position: int) -> None:
        await status.edit_text(texts.queued(position, job.copies) + hint)

    request = IntakeRequest(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        chat_id=message.chat.id,
        status_message_id=status.message_id,
        file_id=media.file_id,
        file_size=media.file_size,
    )
    try:
        await intake.accept(request, on_queued)
    except FileTooLargeError as exc:
        await status.edit_text(texts.TOO_LARGE.format(size=exc.size_mb, limit=exc.limit_mb))
    except TooManyActiveJobsError as exc:
        await status.edit_text(texts.TOO_MANY_JOBS.format(limit=exc.limit))
    except SourceDownloadError:
        await status.edit_text(texts.DOWNLOAD_FAILED)


@router.message(F.document | F.photo | F.animation | F.video_note)
async def handle_not_video(message: Message) -> None:
    await message.answer(texts.NOT_A_VIDEO.format(limit=settings.max_file_mb))
