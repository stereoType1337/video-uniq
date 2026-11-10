"""/admin: summary, user search and card, credit grants, CSV exports (tg-bot-blueprint admin-panel).

Access: Telegram ids in ADMIN_IDS. Every screen is render_*() -> (text, keyboard) and is
edited in place; every callback is answered.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

import asyncpg
import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from arq.connections import ArqRedis

from app.core import texts
from app.core.config import settings
from app.core.exceptions import InsufficientBalanceError
from app.db.repositories.admin import AdminRepository, PeriodCounts, Summary, UserCard
from app.db.repositories.billing import BillingRepository
from app.db.repositories.profiles import ProfileRepository, ProfileState
from app.services.intake import QUEUE_NAME

logger = structlog.get_logger()

Screen = tuple[str, InlineKeyboardMarkup]
GRANT_STEPS = (10, 50, 100, -10)


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in settings.admin_id_set


class AdminAction(CallbackData, prefix="adm"):
    action: str  # home | search | card | grant | ledger | export_* | profiles | profile_toggle
    user_id: int = 0
    amount: int = 0
    profile: str = ""


class AdminSearch(StatesGroup):
    waiting_query = State()


router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


# --- screens ---------------------------------------------------------------------------


def _row(title: str, c: PeriodCounts) -> str:
    return (
        f"{title}: сегодня {c.today:,} · 7 дн. {c.week:,} · 30 дн. {c.month:,} · всего {c.total:,}"
    )


def render_home(s: Summary) -> Screen:
    text = "\n".join(
        [
            "🛠 Админка",
            "",
            _row("👤 Новые юзеры", s.users),
            _row("🎬 Задачи", s.jobs),
            _row("📦 Выдано копий", s.copies),
            _row("💳 Оплат", s.payments),
            _row("⭐ Получено Stars", s.stars),
            "",
            f"⏳ В очереди / в работе: {s.active_jobs}",
            f"❌ Упавших задач за 7 дн.: {s.failed_jobs_week}",
            f"💰 Кредитов на балансах: {s.credits_outstanding:,}",
            f"🎯 Пришли по рефералке: {s.referred_users:,}",
        ]
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти пользователя", callback_data=AdminAction(action="search"))
    builder.button(text="🔄 Обновить", callback_data=AdminAction(action="home"))
    builder.button(text="📥 Юзеры CSV", callback_data=AdminAction(action="export_users"))
    builder.button(text="📥 Платежи CSV", callback_data=AdminAction(action="export_payments"))
    builder.button(text="🎭 Профили метаданных", callback_data=AdminAction(action="profiles"))
    builder.button(text="🗄 Бэкап сейчас", callback_data=AdminAction(action="backup"))
    builder.button(text="🧪 Тест алерта", callback_data=AdminAction(action="alert_test"))
    builder.adjust(1, 1, 2, 1, 2)
    return text, builder.as_markup()


def render_card(card: UserCard) -> Screen:
    u = card.user
    text = "\n".join(
        [
            f"👤 {('@' + u.username) if u.username else 'без username'} · tg_id {u.tg_id}",
            f"Зарегистрирован: {_date(card.created_at)}"
            + (f" · UTM: {card.utm}" if card.utm else ""),
            f"Пришёл по рефералке: {'да' if u.referred_by is not None else 'нет'}"
            f" · пригласил: {card.invited}",
            "",
            f"💰 Баланс: {u.balance} кр.",
            f"⚙️ Настройки: {u.mode.value}, {texts.copies_word(u.copies)}"
            f", звук {u.audio_mode.value}",
            f"🎬 Задач: {card.jobs_total} · выдано копий: {card.copies_delivered}",
            f"⭐ Оплачено: {card.stars_paid} Stars"
            + (
                f" · последняя покупка {_date(card.last_purchase_at)}"
                if card.last_purchase_at
                else ""
            ),
        ]
    )
    builder = InlineKeyboardBuilder()
    for step in GRANT_STEPS:
        builder.button(
            text=f"{step:+d} кр.",
            callback_data=AdminAction(action="grant", user_id=u.id, amount=step),
        )
    builder.button(text="📜 Операции", callback_data=AdminAction(action="ledger", user_id=u.id))
    builder.button(text="🔍 Найти другого", callback_data=AdminAction(action="search"))
    builder.button(text="◀️ В админку", callback_data=AdminAction(action="home"))
    builder.adjust(len(GRANT_STEPS), 1, 2)
    return text, builder.as_markup()


def render_profiles(states: list[ProfileState]) -> Screen:
    lines = ["🎭 Профили метаданных", "Нажмите, чтобы включить или выключить. 📱 — камеры", ""]
    builder = InlineKeyboardBuilder()
    for state in states:
        p = state.profile
        icon = "✅" if state.enabled else "⛔"
        kind = "📱" if p.kind == "camera" else "🎬"
        lines.append(f"{icon} {kind} {p.id} · вес {p.weight:g} · {p.container} · {p.filename}")
        builder.button(
            text=f"{icon} {p.id}",
            callback_data=AdminAction(action="profile_toggle", profile=p.id),
        )
    builder.button(text="◀️ В админку", callback_data=AdminAction(action="home"))
    builder.adjust(2)
    return "\n".join(lines), builder.as_markup()


def _date(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value is not None else "—"


def _back_to_card(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ К карточке", callback_data=AdminAction(action="card", user_id=user_id))
    return builder.as_markup()


# --- handlers --------------------------------------------------------------------------


@router.message(Command("admin"))
async def handle_admin(message: Message, state: FSMContext, admin: AdminRepository) -> None:
    await state.clear()
    text, keyboard = render_home(await admin.summary())
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(AdminAction.filter(F.action == "home"))
async def handle_home(query: CallbackQuery, state: FSMContext, admin: AdminRepository) -> None:
    await state.clear()
    await _redraw(query, render_home(await admin.summary()))


@router.callback_query(AdminAction.filter(F.action == "search"))
async def handle_search(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSearch.waiting_query)
    if isinstance(query.message, Message):
        await query.message.answer("Пришлите tg_id или @username. /admin — отмена.")
    await query.answer()


@router.message(AdminSearch.waiting_query, F.text)
async def handle_search_query(message: Message, state: FSMContext, admin: AdminRepository) -> None:
    user = await admin.find_user(message.text or "")
    card = await admin.user_card(user.id) if user is not None else None
    if card is None:
        await message.answer("Не нашёл. Пришлите другой tg_id или @username, /admin — отмена.")
        return
    await state.clear()
    text, keyboard = render_card(card)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(AdminAction.filter(F.action == "card"))
async def handle_card(
    query: CallbackQuery, callback_data: AdminAction, admin: AdminRepository
) -> None:
    card = await admin.user_card(callback_data.user_id)
    if card is None:
        await query.answer("Пользователь не найден", show_alert=True)
        return
    await _redraw(query, render_card(card))


@router.callback_query(AdminAction.filter(F.action == "grant"))
async def handle_grant(
    query: CallbackQuery,
    callback_data: AdminAction,
    admin: AdminRepository,
    billing: BillingRepository,
) -> None:
    if callback_data.amount not in GRANT_STEPS:
        await query.answer("Неизвестная сумма", show_alert=True)
        return
    try:
        balance = await billing.admin_adjust(callback_data.user_id, callback_data.amount)
    except InsufficientBalanceError:
        await query.answer("На балансе меньше, чем нужно списать", show_alert=True)
        return
    card = await admin.user_card(callback_data.user_id)
    if card is not None:
        await _redraw(query, render_card(card), notice=f"Баланс: {balance} кр.")


@router.callback_query(AdminAction.filter(F.action == "ledger"))
async def handle_ledger(
    query: CallbackQuery, callback_data: AdminAction, admin: AdminRepository
) -> None:
    rows = await admin.ledger(callback_data.user_id)
    lines = ["📜 Последние операции", ""] + [
        f"{_date(r.created_at)} · {r.kind} · {r.amount:+d} → {r.balance_after}" for r in rows
    ]
    if not rows:
        lines.append("Операций нет")
    await _redraw(query, ("\n".join(lines), _back_to_card(callback_data.user_id)))


@router.callback_query(AdminAction.filter(F.action == "profiles"))
async def handle_profiles(query: CallbackQuery, profiles: ProfileRepository) -> None:
    await _redraw(query, render_profiles(await profiles.states()))


@router.callback_query(AdminAction.filter(F.action == "profile_toggle"))
async def handle_profile_toggle(
    query: CallbackQuery, callback_data: AdminAction, profiles: ProfileRepository
) -> None:
    enabled = await profiles.toggle(callback_data.profile)
    if enabled is None:
        await query.answer("Профиль не найден", show_alert=True)
        return
    notice = f"{callback_data.profile}: {'включён' if enabled else 'выключен'}"
    await _redraw(query, render_profiles(await profiles.states()), notice=notice)


@router.callback_query(AdminAction.filter(F.action == "backup"))
async def handle_backup(query: CallbackQuery, arq_redis: ArqRedis) -> None:
    await arq_redis.enqueue_job("backup_database", _queue_name=QUEUE_NAME)
    await query.answer("Бэкап поставлен в очередь, файл придёт сюда", show_alert=True)


@router.callback_query(AdminAction.filter(F.action == "alert_test"))
async def handle_alert_test(query: CallbackQuery) -> None:
    logger.error("alert_test", requested_by=query.from_user.id)
    await query.answer("Тестовая ошибка записана, алерт придёт в течение секунд")


@router.callback_query(AdminAction.filter(F.action.in_({"export_users", "export_payments"})))
async def handle_export(
    query: CallbackQuery, callback_data: AdminAction, admin: AdminRepository
) -> None:
    users = callback_data.action == "export_users"
    rows = await (admin.export_users() if users else admin.export_payments())
    name = "users.csv" if users else "payments.csv"
    if isinstance(query.message, Message):
        await query.message.answer_document(
            BufferedInputFile(to_csv(rows), filename=name), caption=f"{name}: {len(rows)} строк"
        )
    await query.answer()


def to_csv(rows: list[asyncpg.Record]) -> bytes:
    """UTF-8 with BOM so Excel opens Cyrillic correctly."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if rows:
        writer.writerow(rows[0].keys())
        writer.writerows([list(r.values()) for r in rows])
    return buffer.getvalue().encode("utf-8-sig")


async def _redraw(query: CallbackQuery, screen: Screen, notice: str | None = None) -> None:
    text, keyboard = screen
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
    await query.answer(notice)
