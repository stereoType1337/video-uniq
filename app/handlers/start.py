from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router(name="start")

_WELCOME = (
    "Пришлите видео файлом до 150 МБ. Я сделаю несколько уникальных копий, "
    "которые соцсети считают новыми, а зрители не отличат от оригинала.\n\n"
    "Команды: /menu — главное меню, /balance — баланс, /support — поддержка."
)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(_WELCOME)
