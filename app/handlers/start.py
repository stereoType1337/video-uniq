from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.core import texts
from app.services.billing import AccountService

router = Router(name="start")


@router.message(CommandStart())
async def handle_start(message: Message, command: CommandObject, accounts: AccountService) -> None:
    if message.from_user is None:
        return
    account = await accounts.ensure(message.from_user.id, message.from_user.username, command.args)
    await message.answer(texts.welcome(account.trial_credits))
