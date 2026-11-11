from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.core import texts
from app.core.config import settings
from app.db.repositories.users import UserRepository
from app.handlers.screens import (
    OpenScreen,
    Screen,
    SettingsAction,
    main_menu,
    parse_setting,
    render_balance,
    render_packs,
    render_settings,
)
from app.services.billing import AccountService, job_cost

router = Router(name="menu")


@router.message(Command("menu"))
@router.message(F.text == texts.BTN_UNIQUALIZE)
async def handle_menu(message: Message, accounts: AccountService) -> None:
    if message.from_user is None:
        return
    user = (await accounts.ensure(message.from_user.id, message.from_user.username)).user
    cost = job_cost(user.mode, user.copies)
    hint = texts.menu_hint(user.mode.value, user.copies, cost)
    await message.answer(hint, reply_markup=main_menu())


@router.message(Command("settings"))
@router.message(F.text == texts.BTN_SETTINGS)
async def handle_settings(message: Message, accounts: AccountService) -> None:
    if message.from_user is None:
        return
    user = (await accounts.ensure(message.from_user.id, message.from_user.username)).user
    text, keyboard = render_settings(user)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("balance"))
@router.message(F.text == texts.BTN_BALANCE)
async def handle_balance(message: Message, accounts: AccountService) -> None:
    if message.from_user is None:
        return
    user = (await accounts.ensure(message.from_user.id, message.from_user.username)).user
    text, keyboard = render_balance(user, settings.credit_ttl_days)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("buy"))
async def handle_buy(message: Message) -> None:
    text, keyboard = render_packs()
    await message.answer(text, reply_markup=keyboard)


@router.message(Command("help"))
@router.message(F.text == texts.BTN_HELP)
async def handle_help(message: Message) -> None:
    await message.answer(
        texts.HELP.format(
            max_mb=settings.max_file_mb,
            ttl=settings.credit_ttl_days,
            files_ttl=settings.files_ttl_hours,
        ),
        reply_markup=main_menu(),
    )


@router.message(Command("terms"))
async def handle_terms(message: Message) -> None:
    await message.answer(texts.terms_link(settings.public_url))


@router.message(Command("privacy"))
async def handle_privacy(message: Message) -> None:
    await message.answer(texts.privacy_link(settings.public_url))


@router.message(Command("paysupport"))
async def handle_paysupport(message: Message) -> None:
    await message.answer(
        texts.paysupport(settings.support_contact, settings.contact_email, settings.public_url)
    )


@router.message(Command("support"))
async def handle_support(message: Message) -> None:
    await message.answer(texts.support(settings.support_contact))


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
        await query.answer("Эта кнопка устарела, откройте /settings заново.", show_alert=True)
        return
    mode, copies = change
    if (mode, copies) != (user.mode, user.copies):
        user = await users.update_settings(user.id, mode, copies)
    await _redraw(query, render_settings(user))


@router.callback_query(OpenScreen.filter())
async def handle_open_screen(
    query: CallbackQuery, callback_data: OpenScreen, accounts: AccountService
) -> None:
    user = (await accounts.ensure(query.from_user.id, query.from_user.username)).user
    screens = {
        "settings": lambda: render_settings(user),
        "balance": lambda: render_balance(user, settings.credit_ttl_days),
        "packs": render_packs,
    }
    render = screens.get(callback_data.name)
    if render is None:
        await query.answer()
        return
    await _redraw(query, render())


async def _redraw(query: CallbackQuery, screen: Screen) -> None:
    text, keyboard = screen
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
    await query.answer()
