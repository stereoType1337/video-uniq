from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from app.core import texts
from app.core.exceptions import InsufficientBalanceError, TooManyActiveJobsError
from app.db.repositories.jobs import Job
from app.handlers.screens import CardAction, Screen, render_video_card
from app.services.billing import AccountService
from app.services.video_card import CardExpiredError, VideoCardService

router = Router(name="card")


@router.callback_query(CardAction.filter(F.field.in_({"mode", "copies", "audio"})))
async def handle_card_change(
    query: CallbackQuery,
    callback_data: CardAction,
    accounts: AccountService,
    cards: VideoCardService,
) -> None:
    user = (await accounts.ensure(query.from_user.id, query.from_user.username)).user
    try:
        job = await cards.change(
            callback_data.job_id, user, callback_data.field, callback_data.value
        )
    except CardExpiredError:
        await _replace(query, texts.CARD_EXPIRED)
        return
    await _redraw(query, render_video_card(job, user.balance))


@router.callback_query(CardAction.filter(F.field == "start"))
async def handle_card_start(
    query: CallbackQuery,
    callback_data: CardAction,
    accounts: AccountService,
    cards: VideoCardService,
) -> None:
    user = (await accounts.ensure(query.from_user.id, query.from_user.username)).user

    async def on_queued(job: Job, position: int) -> None:
        await _replace(query, texts.queued(position, job.copies), answer=False)

    try:
        await cards.start(callback_data.job_id, user, on_queued)
    except InsufficientBalanceError as exc:
        await query.answer(texts.not_enough_credits(exc.required, exc.balance), show_alert=True)
        return
    except TooManyActiveJobsError as exc:
        await query.answer(texts.TOO_MANY_JOBS.format(limit=exc.limit), show_alert=True)
        return
    except CardExpiredError:
        await _replace(query, texts.CARD_EXPIRED)
        return
    await query.answer()


@router.callback_query(CardAction.filter(F.field == "cancel"))
async def handle_card_cancel(
    query: CallbackQuery,
    callback_data: CardAction,
    accounts: AccountService,
    cards: VideoCardService,
) -> None:
    user = (await accounts.ensure(query.from_user.id, query.from_user.username)).user
    cancelled = await cards.cancel(callback_data.job_id, user)
    await _replace(query, texts.CARD_CANCELLED if cancelled else texts.CARD_EXPIRED)


async def _redraw(query: CallbackQuery, screen: Screen) -> None:
    text, keyboard = screen
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
    await query.answer()


async def _replace(query: CallbackQuery, text: str, answer: bool = True) -> None:
    """Turn the card into a plain message (no buttons)."""
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=None)
    if answer:
        await query.answer()
