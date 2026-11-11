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
from app.db.repositories.app_settings import AppSettingsRepository
from app.db.repositories.billing import BillingRepository
from app.db.repositories.jobs import Job, JobRepository, JobStatus, NewJob
from app.db.repositories.profiles import ProfileRepository
from app.db.repositories.users import NewUser, User, UserRepository
from app.handlers.admin import AdminAction
from app.handlers.admin import router as admin_router
from app.handlers.billing import router as billing_router
from app.handlers.card import router as card_router
from app.handlers.menu import router as menu_router
from app.handlers.screens import (
    CardAction,
    SettingsAction,
    parse_setting,
    render_balance,
    render_packs,
    render_settings,
    render_video_card,
)
from app.handlers.start import router as start_router
from app.services.billing import PACKS, AccountService, PaymentFlow
from app.services.legal_links import LegalLinks, LegalUrls
from app.services.metadata import load_profiles
from app.services.render_plan import Mode, SourceInfo
from app.services.video_card import CardDeps, VideoCardService

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
    legal = LegalUrls("https://t/terms", "https://t/privacy", False, False)
    packs_text, packs = render_packs(legal)
    assert _buttons(packs)[: len(PACKS)] == [texts.pack_button(p.credits, p.stars) for p in PACKS]
    assert "Продолжая, вы принимаете" in packs_text and "https://t/privacy" in packs_text


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
        self.dp["legal_links"] = LegalLinks(AppSettingsRepository(pool), "https://site.test")
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
        ("/buy", "Продолжая, вы принимаете"),
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
    assert edit.text.startswith(texts.BUY_TITLE) and "https://site.test/terms" in edit.text


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


# --- video card ------------------------------------------------------------------------


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[int] = []

    async def enqueue(self, job_id: int) -> None:
        self.enqueued.append(job_id)


@pytest.fixture
def card_harness(pool: asyncpg.Pool) -> tuple[Harness, FakeQueue]:
    harness, queue = Harness(pool), FakeQueue()
    card_router._parent_router = None
    harness.dp.include_router(card_router)
    users, billing, jobs = UserRepository(pool), BillingRepository(pool), JobRepository(pool)
    harness.dp["cards"] = VideoCardService(
        CardDeps(jobs=jobs, billing=billing, users=users, queue=queue), max_active_jobs=2
    )
    return harness, queue


async def _draft(pool: asyncpg.Pool, tg_id: int = 42) -> int:
    user = await UserRepository(pool).get_by_tg_id(tg_id)
    assert user is not None
    source = SourceInfo(width=720, height=1280, fps=30.0, duration=12.0, sample_rate=48000)
    job = await JobRepository(pool).create_draft(
        NewJob(user.id, tg_id, 7, "sha", "src/a.mp4", user.mode, user.copies, source=source)
    )
    return job.id


def _press_card(job_id: int, field: str, value: str = "") -> str:
    return CardAction(job_id=job_id, field=field, value=value).pack()


def test_card_shows_choice_and_price() -> None:
    text, keyboard = render_video_card(
        Job(1, 1, 1, 7, "s", "k", Mode.HD, 5, JobStatus.DRAFT, source_duration=12.0,
            source_width=720, source_height=1280),
        balance=7,
    )  # fmt: skip
    assert "🎬 Видео 12 с · 720×1280" in text and "5 × 2 кр. = 10 кр." in text
    assert texts.card_topup_button(3) in _buttons(keyboard), "not enough credits -> top up"


async def test_card_change_start_and_double_start(
    card_harness: tuple[Harness, FakeQueue], pool: asyncpg.Pool
) -> None:
    harness, queue = card_harness
    await harness.text("/start")  # 5 free credits, Base x5
    job_id = await _draft(pool)

    calls = await harness.press(_press_card(job_id, "copies", "3"))
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert "3 × 1 кр. = 3 кр." in edit.text
    calls = await harness.press(_press_card(job_id, "audio", "next"))
    assert "Звук: оригинал" in next(c for c in calls if isinstance(c, EditMessageText)).text

    calls = await harness.press(_press_card(job_id, "start"))
    queued = next(c for c in calls if isinstance(c, EditMessageText))
    assert "Видео принято" in queued.text and queued.reply_markup is None
    assert queue.enqueued == [job_id]
    row = await pool.fetchrow("SELECT status, copies, audio_mode FROM jobs WHERE id = $1", job_id)
    assert tuple(row) == ("queued", 3, "keep")
    assert await pool.fetchval("SELECT balance FROM users WHERE tg_id = 42") == 2
    user = await pool.fetchrow("SELECT copies, audio_mode FROM users WHERE tg_id = 42")
    assert tuple(user) == (3, "keep"), "card choice becomes the new default"

    again = await harness.press(_press_card(job_id, "start"))
    assert any(isinstance(c, EditMessageText) and c.text == texts.CARD_EXPIRED for c in again)
    assert queue.enqueued == [job_id], "a second tap never charges or queues twice"


async def test_card_start_with_too_few_credits_alerts(
    card_harness: tuple[Harness, FakeQueue], pool: asyncpg.Pool
) -> None:
    harness, queue = card_harness
    await harness.text("/start")
    job_id = await _draft(pool)
    await harness.press(_press_card(job_id, "mode", "hd"))  # 5 x 2 = 10 > 5
    calls = await harness.press(_press_card(job_id, "start"))
    assert any(isinstance(c, AnswerCallbackQuery) and c.show_alert for c in calls)
    assert queue.enqueued == []
    assert await pool.fetchval("SELECT status FROM jobs WHERE id = $1", job_id) == "draft"


async def test_card_cancel_and_foreign_card(
    card_harness: tuple[Harness, FakeQueue], pool: asyncpg.Pool
) -> None:
    harness, _ = card_harness
    await harness.text("/start")
    await UserRepository(pool).register(NewUser(99, None, "other"))
    foreign = await _draft(pool, tg_id=99)
    calls = await harness.press(_press_card(foreign, "start"))
    assert any(isinstance(c, EditMessageText) and c.text == texts.CARD_EXPIRED for c in calls)
    assert await pool.fetchval("SELECT status FROM jobs WHERE id = $1", foreign) == "draft"

    own = await _draft(pool)
    calls = await harness.press(_press_card(own, "cancel"))
    assert any(isinstance(c, EditMessageText) and c.text == texts.CARD_CANCELLED for c in calls)
    assert await pool.fetchval("SELECT status FROM jobs WHERE id = $1", own) == "cancelled"


async def test_camera_profiles_toggle_in_settings(harness: Harness, pool: asyncpg.Pool) -> None:
    await harness.text("/start")
    calls = await harness.press(SettingsAction(field="camera", value="toggle").pack())
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert "Профили камер включены" in edit.text
    assert await pool.fetchval("SELECT camera_profiles FROM users WHERE tg_id = 42") is True


async def test_admin_profiles_screen_toggles(admin_harness: Harness, pool: asyncpg.Pool) -> None:
    repo = ProfileRepository(pool)
    await repo.sync(load_profiles())
    admin_harness.dp["profiles"] = repo
    calls = await admin_harness.press(AdminAction(action="profiles").pack())
    screen = next(c for c in calls if isinstance(c, EditMessageText))
    assert "✅ 📱 iphone" in screen.text and "✅ 🎬 capcut" in screen.text
    calls = await admin_harness.press(AdminAction(action="profile_toggle", profile="iphone").pack())
    assert "⛔ 📱 iphone" in next(c for c in calls if isinstance(c, EditMessageText)).text
    assert "iphone" not in {p.id for p in await repo.enabled()}


@pytest.mark.parametrize(
    ("copies", "button"),
    [
        (1, "▶ Сделать 1 копию — 1 кр."),
        (3, "▶ Сделать 3 копии — 3 кр."),
        (5, "▶ Сделать 5 копий — 5 кр."),
    ],
)
def test_russian_forms_on_buttons(copies: int, button: str) -> None:
    assert texts.card_start_button(copies, copies) == button
    assert "файл ниже" in texts.done(1, 1, 0) and "каждая" not in texts.done(1, 1, 0)


@pytest.mark.parametrize(
    ("command", "needle"),
    [
        ("/terms", "/terms"),
        ("/privacy", "/privacy"),
        ("/paysupport", "14 дней"),
    ],
)
async def test_legal_commands(harness: Harness, command: str, needle: str) -> None:
    (call,) = await harness.text(command)
    assert isinstance(call, SendMessage) and needle in call.text


async def test_start_legal_deeplink_is_not_utm(harness: Harness, pool: asyncpg.Pool) -> None:
    (call,) = await harness.text("/start terms")
    assert isinstance(call, SendMessage) and "https://site.test/terms" in call.text
    assert await pool.fetchval("SELECT utm FROM users WHERE tg_id = 42") is None


async def test_admin_sets_and_resets_legal_links(
    admin_harness: Harness, pool: asyncpg.Pool
) -> None:
    await admin_harness.text("/start")
    calls = await admin_harness.press(AdminAction(action="legal").pack())
    screen = next(c for c in calls if isinstance(c, EditMessageText))
    assert "Юридические ссылки" in screen.text and "по умолчанию" in screen.text

    await admin_harness.press(AdminAction(action="legal_set", profile="terms").pack())
    (bad,) = await admin_harness.text("not a link")
    assert isinstance(bad, SendMessage) and "https://" in bad.text
    (ok,) = await admin_harness.text("https://telegra.ph/oferta-11-11")
    assert isinstance(ok, SendMessage) and "Сохранено" in ok.text

    (terms,) = await admin_harness.text("/terms")
    assert isinstance(terms, SendMessage) and "https://telegra.ph/oferta-11-11" in terms.text
    (privacy,) = await admin_harness.text("/privacy")
    assert isinstance(privacy, SendMessage) and "https://site.test/privacy" in privacy.text

    await admin_harness.press(AdminAction(action="legal_reset").pack())
    (terms,) = await admin_harness.text("/terms")
    assert isinstance(terms, SendMessage) and "https://site.test/terms" in terms.text
