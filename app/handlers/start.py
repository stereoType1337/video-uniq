from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.core import texts
from app.handlers.screens import main_menu
from app.services.billing import LEGAL_START_ARGS, AccountService
from app.services.legal_links import LegalLinks

router = Router(name="start")


@router.message(CommandStart())
async def handle_start(
    message: Message, command: CommandObject, accounts: AccountService, legal_links: LegalLinks
) -> None:
    if message.from_user is None:
        return
    account = await accounts.ensure(message.from_user.id, message.from_user.username, command.args)
    if command.args in LEGAL_START_ARGS:  # t.me/<bot>?start=terms from the legal block
        urls = await legal_links.urls()
        link = (
            texts.terms_link(urls.terms)
            if command.args == "terms"
            else texts.privacy_link(urls.privacy)
        )
        await message.answer(link, reply_markup=main_menu())
        return
    await message.answer(texts.welcome(account.trial_credits), reply_markup=main_menu())
