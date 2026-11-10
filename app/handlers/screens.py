"""Screens as pure ``render_*() -> (text, keyboard)`` functions (tg-bot-blueprint pattern):
the same function draws a screen when it is opened and after every button press, so an
inline screen is edited in place instead of spawning new messages."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.core import texts
from app.db.repositories.jobs import Job
from app.db.repositories.users import User
from app.services.billing import PACKS, PRICE_PER_COPY, job_cost
from app.services.render_plan import Mode
from app.services.video_card import COPIES_OPTIONS

Screen = tuple[str, InlineKeyboardMarkup]


class SettingsAction(CallbackData, prefix="set"):
    field: str  # "mode" | "copies" | "camera"
    value: str


class BuyPack(CallbackData, prefix="buy"):
    code: str


class CardAction(CallbackData, prefix="card"):
    job_id: int
    field: str  # mode | copies | audio | start | cancel
    value: str = ""


class OpenScreen(CallbackData, prefix="open"):
    name: str  # "packs" | "settings" | "balance"


def main_menu() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text=texts.BTN_UNIQUALIZE)
    for label in (texts.BTN_BALANCE, texts.BTN_SETTINGS, texts.BTN_INVITE, texts.BTN_HELP):
        builder.button(text=label)
    builder.adjust(1, 2, 2)
    return builder.as_markup(
        resize_keyboard=True, is_persistent=True, input_field_placeholder=texts.MENU_PLACEHOLDER
    )


def render_settings(user: User) -> Screen:
    builder = InlineKeyboardBuilder()
    for mode in Mode:
        builder.button(
            text=texts.mode_button(mode.value, PRICE_PER_COPY[mode], mode is user.mode),
            callback_data=SettingsAction(field="mode", value=mode.value),
        )
    for copies in COPIES_OPTIONS:
        builder.button(
            text=texts.copies_button(copies, copies == user.copies),
            callback_data=SettingsAction(field="copies", value=str(copies)),
        )
    builder.button(
        text=texts.camera_button(user.camera_profiles),
        callback_data=SettingsAction(field="camera", value="toggle"),
    )
    builder.button(text=texts.BTN_BALANCE, callback_data=OpenScreen(name="balance"))
    builder.adjust(len(Mode), len(COPIES_OPTIONS), 1, 1)
    text = texts.settings_screen(
        user.mode.value, user.copies, PRICE_PER_COPY[user.mode], user.balance
    ) + texts.camera_hint(user.camera_profiles)
    return text, builder.as_markup()


def render_balance(user: User, ttl_days: int) -> Screen:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_BUY, callback_data=OpenScreen(name="packs"))
    builder.button(text=texts.BTN_SETTINGS, callback_data=OpenScreen(name="settings"))
    builder.adjust(2)
    cost = job_cost(user.mode, user.copies)
    text = texts.balance_screen(user.balance, user.mode.value, user.copies, cost, ttl_days)
    return text, builder.as_markup()


def render_packs() -> Screen:
    builder = InlineKeyboardBuilder()
    for pack in PACKS:
        builder.button(
            text=texts.pack_button(pack.credits, pack.stars), callback_data=BuyPack(code=pack.code)
        )
    builder.button(text="‹ Назад", callback_data=OpenScreen(name="balance"))
    builder.adjust(1)
    return texts.BUY_TITLE, builder.as_markup()


def parse_setting(action: SettingsAction, user: User) -> tuple[Mode, int] | None:
    """Validated (mode, copies) after a button press; None for forged/stale callback data."""
    if action.field == "mode" and action.value in {m.value for m in Mode}:
        return Mode(action.value), user.copies
    if action.field == "copies" and action.value.isdigit():
        copies = int(action.value)
        if copies in COPIES_OPTIONS:
            return user.mode, copies
    return None


def render_video_card(job: Job, balance: int) -> Screen:
    price = PRICE_PER_COPY[job.mode]
    cost = price * job.copies
    info = texts.CardInfo(
        job.mode.value,
        job.copies,
        job.audio_mode.value,
        price,
        balance,
        job.source_duration,
        job.source_width,
        job.source_height,
    )
    builder = InlineKeyboardBuilder()
    for mode in Mode:
        builder.button(
            text=texts.mode_button(mode.value, PRICE_PER_COPY[mode], mode is job.mode),
            callback_data=CardAction(job_id=job.id, field="mode", value=mode.value),
        )
    for copies in COPIES_OPTIONS:
        builder.button(
            text=texts.copies_button(copies, copies == job.copies),
            callback_data=CardAction(job_id=job.id, field="copies", value=str(copies)),
        )
    builder.button(
        text=texts.card_audio_button(job.audio_mode.value),
        callback_data=CardAction(job_id=job.id, field="audio", value="next"),
    )
    if balance >= cost:
        builder.button(
            text=texts.card_start_button(job.copies, cost),
            callback_data=CardAction(job_id=job.id, field="start"),
        )
    else:
        builder.button(
            text=texts.card_topup_button(cost - balance), callback_data=OpenScreen(name="packs")
        )
    builder.button(text="✖ Отмена", callback_data=CardAction(job_id=job.id, field="cancel"))
    builder.adjust(len(Mode), len(COPIES_OPTIONS), 1, 1, 1)
    return texts.card(info), builder.as_markup()
