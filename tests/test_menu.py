"""Menu and settings: pure screens + the real aiogram Dispatcher over a fake Telegram session."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from typing import Any

import asyncpg
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from aiogram.types import (
    User as TgUser,
)

from app.core import texts
from app.core.config import settings
from app.db.repositories.admin import AdminRepository
from app.db.repositories.billing import BillingRepository
from app.db.repositories.users import User, UserRepository
from app.handlers.admin import AdminAction
from app.handlers.admin import router as admin_router
from app.handlers.billing import router as billing_router
from app.handlers.menu import router as menu_router
from app.handlers.screens import (
    SettingsAction,
    parse_setting,
    render_balance,
    render_packs,
    render_settings,
)
from app.handlers.start import router as start_router
from app.services.billing import PACKS, AccountService, PaymentFlow
from app.services.render_plan import Mode

_USER = User(1, 42, "u", 25, Mode.HD, 5, "ref", None)
_NOW = dt.datetime(2026, 11, 10, tzinfo=dt.UTC)


def _buttons(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


# --- pure screens ----------------------------------------------------------------------


def test_settings_screen_marks_current_choice_and_cost() -> None:
    text, keyboard = render_settings(_USER)
    labels = _buttons(keyboard)
    assert "✅ HD · 2 кр." in labels and "Base · 1 кр." in labels
    assert "✅ 5" in labels and "10" in labels
    assert "5 × 2 кр. = 10 кр." in text and "На балансе 25 кр." in text


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("mode", "hd_plus", (Mode.HD_PLUS, 5)),
        ("copies", "10", (Mode.HD, 10)),
        ("copies", "7", None),  # not an offered option
        ("copies", "-1", None),
        ("mode", "ultra", None),
        ("price", "0", None),
    ],
)
def test_parse_setting_rejects_forged_values(
    field: str, value: str, expected: tuple[Mode, int] | None
) -> None:
    assert parse_setting(SettingsAction(field=field, value=value), _USER) == expected


def test_balance_and_packs_screens() -> None:
    text, keyboard = render_balance(_USER, ttl_days=30)
    assert "25 кр." in text and "стоит 10 кр." in text
    assert texts.BTN_BUY in _buttons(keyboard)
    _, packs = render_packs()
    assert _buttons(packs)[: len(PACKS)] == [texts.pack_button(p.credits, p.stars) for p in PACKS]


# --- dispatcher over a fake Telegram session -------------------------------------------


class FakeSession(BaseSession):
    """Records every Bot API call and returns minimal valid results."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        self.calls.append(method)
        if isinstance(method, SendMessage):
            chat = Chat(id=int(method.chat_id), type="private")
            result: Any = Message(
                message_id=len(self.calls), date=_NOW, chat=chat, text=method.text
            )
            return result  # type: ignore[no-any-return]
        return True  # type: ignore[return-value]

    async def stream_content(self, *args: object, **kwargs: object) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        return None


class Harness:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.session = FakeSession()
        self.bot = Bot("42:TEST", session=self.session)
        self.dp = Dispatcher()
        for router in (start_router, menu_router, billing_router):
            router._parent_router = None  # routers are module singletons; re-attach per test
            self.dp.include_router(router)
        users, billing = UserRepository(pool), BillingRepository(pool)
        self.dp["users"] = users
        self.dp["accounts"] = AccountService(users, billing, free_credits=5)
        self.dp["payments"] = PaymentFlow(billing)
        self._update_id = 0
        self.tg_user = TgUser(id=42, is_bot=False, first_name="T", username="t")
        self.chat = Chat(id=42, type="private")

    async def text(self, text: str) -> list[TelegramMethod[Any]]:
        message = Message(
            message_id=1000, date=_NOW, chat=self.chat, from_user=self.tg_user, text=text
        )
        return await self._feed(Update(update_id=self._next(), message=message))

    async def press(self, data: str, message_id: int = 7) -> list[TelegramMethod[Any]]:
        origin = Message(message_id=message_id, date=_NOW, chat=self.chat, text="screen")
        query = CallbackQuery(
            id="q", from_user=self.tg_user, chat_instance="c", data=data, message=origin
        )
        return await self._feed(Update(update_id=self._next(), callback_query=query))

    async def _feed(self, update: Update) -> list[TelegramMethod[Any]]:
        start = len(self.session.calls)
        await self.dp.feed_update(self.bot, update)
        return self.session.calls[start:]

    def _next(self) -> int:
        self._update_id += 1
        return self._update_id


@pytest.fixture
def harness(pool: asyncpg.Pool) -> Harness:
    return Harness(pool)


async def test_start_shows_menu_and_grants_trial(harness: Harness) -> None:
    (call,) = await harness.text("/start")
    assert isinstance(call, SendMessage) and "5 кредитов" in call.text
    assert isinstance(call.reply_markup, ReplyKeyboardMarkup)
    labels = [b.text for row in call.reply_markup.keyboard for b in row]
    assert labels == [
        texts.BTN_UNIQUALIZE,
        texts.BTN_BALANCE,
        texts.BTN_SETTINGS,
        texts.BTN_INVITE,
        texts.BTN_HELP,
    ]


@pytest.mark.parametrize(
    ("button", "needle"),
    [
        (texts.BTN_UNIQUALIZE, "Base, 5 копий — 5 кр."),
        (texts.BTN_BALANCE, "💰 Баланс: 5 кр."),
        (texts.BTN_SETTINGS, "⚙️ Настройки"),
        (texts.BTN_HELP, "Как это работает"),
        ("/support", "Поддержка"),
        ("/buy", texts.BUY_TITLE),
    ],
)
async def test_menu_buttons_route_to_screens(harness: Harness, button: str, needle: str) -> None:
    calls = await harness.text(button)
    sent = [c for c in calls if isinstance(c, SendMessage)]
    assert sent and needle in sent[-1].text


async def test_settings_change_is_saved_and_screen_edited_in_place(
    harness: Harness, pool: asyncpg.Pool
) -> None:
    await harness.text("/start")
    calls = await harness.press(SettingsAction(field="mode", value="hd_plus").pack())
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert edit.message_id == 7, "the settings message is edited, not re-sent"
    assert "HD+" in edit.text and "5 × 3 кр. = 15 кр." in edit.text
    await harness.press(SettingsAction(field="copies", value="10").pack())
    row = await pool.fetchrow("SELECT mode, copies FROM users WHERE tg_id = 42")
    assert (row["mode"], row["copies"]) == ("hd_plus", 10)


async def test_forged_setting_is_refused(harness: Harness, pool: asyncpg.Pool) -> None:
    await harness.text("/start")
    calls = await harness.press(SettingsAction(field="copies", value="1000").pack())
    answer = next(c for c in calls if isinstance(c, AnswerCallbackQuery))
    assert answer.show_alert and not any(isinstance(c, EditMessageText) for c in calls)
    assert await pool.fetchval("SELECT copies FROM users WHERE tg_id = 42") == 5


async def test_balance_screen_opens_packs_in_place(harness: Harness) -> None:
    await harness.text("/start")
    calls = await harness.press("open:packs")
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert edit.text == texts.BUY_TITLE


# --- admin -----------------------------------------------------------------------------


@pytest.fixture
def admin_harness(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> Harness:
    monkeypatch.setattr(settings, "admin_ids", "42")
    harness = Harness(pool)
    admin_router._parent_router = None
    harness.dp.include_router(admin_router)
    harness.dp["admin"] = AdminRepository(pool)
    harness.dp["billing"] = BillingRepository(pool)
    return harness


async def test_admin_is_hidden_from_non_admins(
    harness: Harness, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "admin_ids", "1")
    admin_router._parent_router = None
    harness.dp.include_router(admin_router)
    harness.dp["admin"] = AdminRepository(pool)
    assert await harness.text("/admin") == []


async def test_admin_summary_search_and_grant(admin_harness: Harness, pool: asyncpg.Pool) -> None:
    await admin_harness.text("/start")
    (summary,) = await admin_harness.text("/admin")
    assert isinstance(summary, SendMessage) and "Новые юзеры: сегодня 1" in summary.text

    await admin_harness.press(AdminAction(action="search").pack())
    (card,) = await admin_harness.text("@t")
    assert isinstance(card, SendMessage) and "tg_id 42" in card.text and "Баланс: 5" in card.text

    user_id = await pool.fetchval("SELECT id FROM users WHERE tg_id = 42")
    calls = await admin_harness.press(
        AdminAction(action="grant", user_id=user_id, amount=50).pack()
    )
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert "Баланс: 55 кр." in edit.text
    forged = await admin_harness.press(
        AdminAction(action="grant", user_id=user_id, amount=100000).pack()
    )
    assert any(isinstance(c, AnswerCallbackQuery) and c.show_alert for c in forged)
    assert await pool.fetchval("SELECT balance FROM users WHERE tg_id = 42") == 55
    kinds = [r["kind"] for r in await pool.fetch("SELECT kind FROM transactions ORDER BY id")]
    assert kinds == ["trial", "admin"]
