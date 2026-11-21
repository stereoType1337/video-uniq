from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message, ReplyKeyboardRemove

from app.core import texts
from app.core.config import settings
from app.handlers.screens import render_home
from app.services.billing import LEGAL_START_ARGS, AccountService
from app.services.legal_links import LegalLinks

router = Router(name="start")


async def remove_reply_keyboard(message: Message) -> None:
    """Drop the old persistent reply keyboard: Telegram removes it only via a message that
    carries ReplyKeyboardRemove, so send one and delete it straight away."""
    try:
        cleanup = await message.answer(texts.KEYBOARD_CLEANUP, reply_markup=ReplyKeyboardRemove())
        if message.bot is not None:
            await message.bot.delete_message(cleanup.chat.id, cleanup.message_id)
    except TelegramAPIError:
        pass  # cosmetic: never block the menu on it


@router.message(CommandStart())
async def handle_start(
    message: Message, command: CommandObject, accounts: AccountService, legal_links: LegalLinks
) -> None:
    if message.from_user is None:
        return
    account = await accounts.ensure(message.from_user.id, message.from_user.username, command.args)
    if not account.created:  # only users from before the inline menu can still have it
        await remove_reply_keyboard(message)
    if command.args in LEGAL_START_ARGS:  # t.me/<bot>?start=terms from the legal block
        urls = await legal_links.urls()
        link = (
            texts.terms_link(urls.terms)
            if command.args == "terms"
            else texts.privacy_link(urls.privacy)
        )
        await message.answer(link)
        return
    text, keyboard = render_home(
        account.user,
        is_admin=message.from_user.id in settings.admin_id_set,
        support=settings.support_contact,
        trial_credits=account.trial_credits,
    )
    await message.answer(text, reply_markup=keyboard)
