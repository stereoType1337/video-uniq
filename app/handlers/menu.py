"""Inline menu (NotSpy pattern): one entry screen from /start, every other screen is opened
by a button and edited in place. The only listed command is /start; /terms, /privacy and
/paysupport keep working for links and Telegram's payment rules but are not advertised."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis

from app.core import texts
from app.core.config import settings
from app.db.repositories.users import User, UserRepository
from app.handlers.screens import (
    OpenScreen,
    Screen,
    SettingsAction,
    parse_setting,
    render_balance,
    render_help,
    render_home,
    render_packs,
    render_referral,
    render_settings,
    render_text,
)
from app.handlers.start import remove_reply_keyboard
from app.services.billing import AccountService
from app.services.legal_links import LegalLinks

router = Router(name="menu")
fallback_router = Router(name="fallback")  # included last: anything nobody else handled


def _paysupport_text(terms_url: str) -> str:
    return texts.paysupport(settings.support_contact, settings.contact_email, terms_url)


@router.message(Command("terms"))
async def handle_terms(message: Message, legal_links: LegalLinks) -> None:
    await message.answer(texts.terms_link((await legal_links.urls()).terms))


@router.message(Command("privacy"))
async def handle_privacy(message: Message, legal_links: LegalLinks) -> None:
    await message.answer(texts.privacy_link((await legal_links.urls()).privacy))


@router.message(Command("paysupport"))
async def handle_paysupport(message: Message, legal_links: LegalLinks) -> None:
    text, keyboard = render_text(_paysupport_text((await legal_links.urls()).terms))
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(SettingsAction.filter())
async def handle_setting_change(
    query: CallbackQuery,
    callback_data: SettingsAction,
    accounts: AccountService,
    users: UserRepository,
) -> None:
    user = (await accounts.ensure(query.from_user.id, query.from_user.username)).user
    if callback_data.field == "camera":
        user = await users.toggle_camera_profiles(user.id)
        await _redraw(query, render_settings(user))
        return
    change = parse_setting(callback_data, user)
    if change is None:
        await query.answer("Эта кнопка устарела, откройте ⚙️ Настройки заново.", show_alert=True)
        return
    mode, copies = change
    if (mode, copies) != (user.mode, user.copies):
        user = await users.update_settings(user.id, mode, copies)
    await _redraw(query, render_settings(user))


@router.callback_query(OpenScreen.filter())
async def handle_open_screen(
    query: CallbackQuery,
    callback_data: OpenScreen,
    bot: Bot,
    accounts: AccountService,
    legal_links: LegalLinks,
) -> None:
    user = (await accounts.ensure(query.from_user.id, query.from_user.username)).user
    screen = await _screen(callback_data.name, user, query.from_user.id, bot, legal_links)
    if screen is None:
        await query.answer()
        return
    await _redraw(query, screen)


async def _screen(
    name: str, user: User, tg_id: int, bot: Bot, legal_links: LegalLinks
) -> Screen | None:
    match name:
        case "home":
            return render_home(
                user, is_admin=tg_id in settings.admin_id_set, support=settings.support_contact
            )
        case "settings":
            return render_settings(user)
        case "balance":
            return render_balance(user, settings.credit_ttl_days)
        case "packs":
            return render_packs(await legal_links.urls())
        case "help":
            return render_help(
                await legal_links.urls(),
                max_mb=settings.max_file_mb,
                ttl_days=settings.credit_ttl_days,
                files_ttl_hours=settings.files_ttl_hours,
            )
        case "ref":
            username = settings.bot_username or (await bot.me()).username or ""
            return render_referral(username, user.ref_code, settings.referral_bonus)
        case "support":
            return render_text(texts.support(settings.support_contact))
        case "paysupport":
            return render_text(_paysupport_text((await legal_links.urls()).terms), back="help")
    return None


@fallback_router.message(F.chat.type == "private", F.text)
async def handle_unknown_text(
    message: Message, accounts: AccountService, arq_redis: Redis | None = None
) -> None:
    """Old reply-keyboard buttons, retired commands, random text: back to the inline menu."""
    if message.from_user is None:
        return
    await remove_reply_keyboard(message, arq_redis)
    user = (await accounts.ensure(message.from_user.id, message.from_user.username)).user
    text, keyboard = render_home(
        user,
        is_admin=message.from_user.id in settings.admin_id_set,
        support=settings.support_contact,
    )
    await message.answer(text, reply_markup=keyboard)


async def _redraw(query: CallbackQuery, screen: Screen) -> None:
    text, keyboard = screen
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
    await query.answer()
