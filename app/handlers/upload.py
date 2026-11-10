from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from app.core import texts
from app.core.config import settings
from app.core.exceptions import (
    FileTooLargeError,
    InsufficientBalanceError,
    ProbeError,
    SourceDownloadError,
    TooManyActiveJobsError,
)
from app.handlers.screens import render_video_card
from app.services.intake import IntakeRequest, IntakeService
from app.services.video_rules import VideoRejectedError

router = Router(name="upload")

_VIDEO_DOCUMENT = F.document.mime_type.startswith("video/")


@router.message(F.video | _VIDEO_DOCUMENT)
async def handle_video(message: Message, intake: IntakeService) -> None:
    media = message.video or message.document
    if media is None or message.from_user is None:
        return
    if message.video is not None:  # sent as a "video", i.e. already recompressed by Telegram
        await message.answer(texts.COMPRESSED_HINT.strip())
    status = await message.answer(texts.DOWNLOADING)
    request = IntakeRequest(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        chat_id=message.chat.id,
        status_message_id=status.message_id,
        file_id=media.file_id,
        file_size=media.file_size,
        duration=message.video.duration if message.video is not None else None,
    )
    try:
        result = await intake.accept(request)
    except FileTooLargeError as exc:
        await status.edit_text(texts.TOO_LARGE.format(size=exc.size_mb, limit=exc.limit_mb))
    except InsufficientBalanceError as exc:
        await status.edit_text(texts.not_enough_credits(exc.required, exc.balance))
    except TooManyActiveJobsError as exc:
        await status.edit_text(texts.TOO_MANY_JOBS.format(limit=exc.limit))
    except VideoRejectedError as exc:
        await status.edit_text(texts.rejected(exc.rejection))
    except ProbeError:
        await status.edit_text(texts.UNREADABLE)
    except SourceDownloadError:
        await status.edit_text(texts.DOWNLOAD_FAILED)
    else:
        text, keyboard = render_video_card(result.draft, result.user.balance)
        await status.edit_text(text, reply_markup=keyboard)


@router.message(F.document | F.photo | F.animation | F.video_note)
async def handle_not_video(message: Message) -> None:
    await message.answer(texts.NOT_A_VIDEO.format(limit=settings.max_file_mb))
