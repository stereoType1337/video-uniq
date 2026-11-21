"""Screens as pure ``render_*() -> (text, keyboard)`` functions (tg-bot-blueprint pattern):
the same function draws a screen when it is opened and after every button press, so an
inline screen is edited in place instead of spawning new messages."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core import texts
from app.db.repositories.jobs import Job
from app.db.repositories.users import User
from app.services.billing import PACKS, PRICE_PER_COPY, Pack, job_cost, referral_link
from app.services.legal_links import LegalUrls
from app.services.payments.ports import PaymentProvider
from app.services.render_plan import Mode
from app.services.video_card import COPIES_OPTIONS

Screen = tuple[str, InlineKeyboardMarkup]


class SettingsAction(CallbackData, prefix="set"):
    field: str  # "mode" | "copies" | "camera"
    value: str


class BuyPack(CallbackData, prefix="buy"):
    code: str


class PayWith(CallbackData, prefix="pay"):
    code: str
    method: str  # "stars" or a provider name


STARS_METHOD = "stars"


class CardAction(CallbackData, prefix="card"):
    job_id: int
    field: str  # mode | copies | audio | start | cancel
    value: str = ""


class OpenScreen(CallbackData, prefix="open"):
    name: str  # home | settings | balance | packs | help | ref | support | paysupport


class AdminAction(CallbackData, prefix="adm"):
    action: str  # home | search | card | grant | ledger | export_* | profiles | profile_toggle
    user_id: int = 0
    amount: int = 0
    profile: str = ""


def _home_button(builder: InlineKeyboardBuilder) -> None:
    builder.button(text=texts.BTN_HOME, callback_data=OpenScreen(name="home"))


def support_url(contact: str) -> str | None:
    """A link for the support button: '@name' / 't.me/name' / any https URL; None otherwise."""
    contact = contact.strip()
    if contact.startswith("https://"):
        return contact
    if contact.startswith("t.me/"):
        return f"https://{contact}"
    if contact.startswith("@") and len(contact) > 1:
        return f"https://t.me/{contact[1:]}"
    return None


def render_home(user: User, *, is_admin: bool, support: str, trial_credits: int = 0) -> Screen:
    """The single entry screen (NotSpy pattern): inline buttons only, no reply keyboard."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_SETTINGS, callback_data=OpenScreen(name="settings"), style="primary"
    )
    builder.button(
        text=texts.BTN_BALANCE, callback_data=OpenScreen(name="balance"), style="primary"
    )
    builder.button(text=texts.BTN_BUY, callback_data=OpenScreen(name="packs"), style="success")
    builder.button(text=texts.BTN_INVITE, callback_data=OpenScreen(name="ref"))
    builder.button(text=texts.BTN_HELP, callback_data=OpenScreen(name="help"))
    url = support_url(support)
    if url:
        builder.button(text=texts.BTN_SUPPORT, url=url)
    else:
        builder.button(text=texts.BTN_SUPPORT, callback_data=OpenScreen(name="support"))
    sizes = [2, 1, 2, 1]
    if is_admin:
        builder.button(text=texts.BTN_ADMIN, callback_data=AdminAction(action="home"))
        sizes.append(1)
    builder.adjust(*sizes)
    cost = job_cost(user.mode, user.copies)
    text = texts.home(user.balance, user.mode.value, user.copies, cost, trial_credits)
    return text, builder.as_markup()


def render_help(legal: LegalUrls, *, max_mb: int, ttl_days: int, files_ttl_hours: int) -> Screen:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_TERMS, url=legal.terms)
    builder.button(text=texts.BTN_PRIVACY, url=legal.privacy)
    builder.button(text=texts.BTN_PAYSUPPORT, callback_data=OpenScreen(name="paysupport"))
    _home_button(builder)
    builder.adjust(2, 1, 1)
    text = texts.HELP.format(max_mb=max_mb, ttl=ttl_days, files_ttl=files_ttl_hours)
    return text, builder.as_markup()


def render_referral(bot_username: str, ref_code: str, bonus: int) -> Screen:
    link = referral_link(bot_username, ref_code)
    builder = InlineKeyboardBuilder()
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(texts.SHARE_TEXT)}"
    builder.button(text=texts.BTN_SHARE, url=share, style="success")
    _home_button(builder)
    builder.adjust(1)
    return texts.referral(link, bonus), builder.as_markup()


def render_text(text: str, back: str = "home") -> Screen:
    """A plain info screen (support, payment questions) with one way back."""
    builder = InlineKeyboardBuilder()
    if back == "home":
        _home_button(builder)
    else:
        builder.button(text=texts.BTN_BACK, callback_data=OpenScreen(name=back))
    return text, builder.as_markup()


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
    _home_button(builder)
    builder.adjust(len(Mode), len(COPIES_OPTIONS), 1, 2)
    text = texts.settings_screen(
        user.mode.value, user.copies, PRICE_PER_COPY[user.mode], user.balance
    ) + texts.camera_hint(user.camera_profiles)
    return text, builder.as_markup()


def render_balance(user: User, ttl_days: int) -> Screen:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_BUY, callback_data=OpenScreen(name="packs"), style="success")
    builder.button(text=texts.BTN_SETTINGS, callback_data=OpenScreen(name="settings"))
    _home_button(builder)
    builder.adjust(2, 1)
    cost = job_cost(user.mode, user.copies)
    text = texts.balance_screen(user.balance, user.mode.value, user.copies, cost, ttl_days)
    return text, builder.as_markup()


def render_packs(legal: LegalUrls) -> Screen:
    builder = InlineKeyboardBuilder()
    for pack in PACKS:
        builder.button(
            text=texts.pack_button(pack.credits, pack.stars), callback_data=BuyPack(code=pack.code)
        )
    builder.button(text=texts.BTN_BACK, callback_data=OpenScreen(name="balance"))
    _home_button(builder)
    builder.adjust(*([1] * len(PACKS)), 2)
    return (
        f"{texts.BUY_TITLE}\n\n{texts.legal_block(legal.terms, legal.privacy)}",
        builder.as_markup(),
    )


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


def render_pay_methods(pack: Pack, providers: Mapping[str, PaymentProvider]) -> Screen:
    builder = InlineKeyboardBuilder()
    for name, provider in providers.items():
        builder.button(
            text=texts.pay_method_button(provider.label, pack.rub, "₽"),
            callback_data=PayWith(code=pack.code, method=name),
        )
    builder.button(
        text=texts.pay_method_button("⭐ Telegram Stars", pack.stars, "⭐"),
        callback_data=PayWith(code=pack.code, method=STARS_METHOD),
    )
    builder.button(text=texts.BTN_BACK, callback_data=OpenScreen(name="packs"))
    builder.adjust(1)
    return texts.pay_methods_title(pack.credits), builder.as_markup()


def render_pay_link(pack: Pack, url: str) -> Screen:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.pay_link_button(pack.rub), url=url, style="success")
    return texts.pay_link(pack.credits, pack.rub), builder.as_markup()
