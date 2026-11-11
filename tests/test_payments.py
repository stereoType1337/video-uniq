"""RUB providers (Kassa AI, Platega): the money checklist from tg-bot-blueprint payments.md,
driven through the real aiohttp routes and a real Postgres (``pool`` fixture)."""

from __future__ import annotations

from typing import Any, cast

import asyncpg
import pytest
from aiogram import Bot
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.db.repositories.billing import BillingRepository
from app.db.repositories.users import NewUser, User, UserRepository
from app.handlers.screens import render_pay_link, render_pay_methods
from app.services.billing import PACKS, PaymentFlow
from app.services.payments.kassai import KassaiCards, KassaiSBP
from app.services.payments.platega import Platega, amount_of
from app.services.payments.ports import (
    CreatedPayment,
    PaymentProvider,
    ProviderError,
    WebhookVerdict,
)
from app.web import payments as web_payments

PACK = PACKS[0]  # 100 cr, 299 RUB
_PLATEGA_HEADERS = {"X-MerchantId": "m-1", "X-Secret": "s-1"}


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class StubProvider(PaymentProvider):
    """Stands in for a provider HTTP API (no network in tests)."""

    def __init__(self, provider: PaymentProvider, fail: bool = False) -> None:
        self._real = provider
        self.name, self.label = provider.name, provider.label
        self.verifies_amount, self.can_poll = provider.verifies_amount, True
        self.fail = fail
        self.status: dict[str, WebhookVerdict] = {}

    async def create_payment(self, payment_id: int, tg_id: int, amount: int) -> CreatedPayment:
        if self.fail:
            raise ProviderError(self.name, "down")
        return CreatedPayment(f"tx-{payment_id}", f"https://pay.test/{payment_id}")

    async def verify_webhook(self, payload: dict[str, Any]) -> WebhookVerdict:
        return await self._real.verify_webhook(payload)

    async def fetch_status(self, provider_payment_id: str) -> WebhookVerdict | None:
        return self.status.get(provider_payment_id)


class StubPlatega(Platega):
    """Real Platega auth and parsing, stubbed network calls."""

    def __init__(self) -> None:
        super().__init__("m-1", "s-1", 2, "https://uniq.test", 10)
        self.status: dict[str, WebhookVerdict] = {}

    async def create_payment(self, payment_id: int, tg_id: int, amount: int) -> CreatedPayment:
        return CreatedPayment(f"tx-{payment_id}", f"https://pay.test/{payment_id}")

    async def fetch_status(self, provider_payment_id: str) -> WebhookVerdict | None:
        return self.status.get(provider_payment_id)


def _providers() -> dict[str, PaymentProvider]:
    kassa_args = ("1234", "api-key", "secret-2", "uniq.test")
    return {
        "platega": Platega("m-1", "s-1", 2, "https://uniq.test", 10),
        "kassai_sbp": KassaiSBP(*kassa_args),
        "kassai_cards": KassaiCards(*kassa_args),
    }


async def _user(pool: asyncpg.Pool, tg_id: int = 500) -> User:
    user, _ = await UserRepository(pool).register(NewUser(tg_id, None, f"r{tg_id}"))
    return user


async def _client(flow: PaymentFlow, bot: FakeBot) -> TestClient:
    app = web.Application()
    web_payments.setup(app)
    app[web_payments.FLOW_KEY] = flow
    app[web_payments.BOT_KEY] = cast(Bot, bot)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _balance(pool: asyncpg.Pool, user: User) -> int:
    return int(await pool.fetchval("SELECT balance FROM users WHERE id = $1", user.id))


async def _status(pool: asyncpg.Pool, payment_id: int) -> str:
    return str(await pool.fetchval("SELECT status FROM payments WHERE id = $1", payment_id))


async def _external(
    pool: asyncpg.Pool, provider: str, user: User
) -> tuple[PaymentFlow, StubProvider | StubPlatega, int]:
    providers = _providers()
    stub: StubProvider | StubPlatega = (
        StubPlatega() if provider == "platega" else StubProvider(providers[provider])
    )
    providers[provider] = stub
    flow = PaymentFlow(BillingRepository(pool), providers)
    url = await flow.start_external(user, PACK, provider)
    payment_id = int(url.rsplit("/", 1)[1])
    return flow, stub, payment_id


# --- Kassa AI ----------------------------------------------------------------------------


def _kassa_payload(payment_id: int, amount: int, cur_id: str = "44") -> dict[str, str]:
    kassa = cast(KassaiSBP, _providers()["kassai_sbp"])
    order = f"tx-{payment_id}"
    return {
        "MERCHANT_ID": "1234",
        "AMOUNT": str(amount),
        "MERCHANT_ORDER_ID": order,
        "CUR_ID": cur_id,
        "SIGN": kassa.webhook_sign(str(amount), order),
    }


async def test_kassai_credits_once_and_rejects_bad_signature(pool: asyncpg.Pool) -> None:
    user = await _user(pool)
    flow, _, payment_id = await _external(pool, "kassai_sbp", user)
    bot = FakeBot()
    client = await _client(flow, bot)
    try:
        forged = _kassa_payload(payment_id, PACK.rub) | {"SIGN": "0" * 32}
        assert (await client.post("/payments/kassai/webhook", data=forged)).status == 403
        assert await _balance(pool, user) == 0

        good = _kassa_payload(payment_id, PACK.rub)
        for _ in range(2):  # replay must not double-credit
            response = await client.post("/payments/kassai/webhook", data=good)
            assert response.status == 200 and await response.text() == "YES"
        assert await _balance(pool, user) == PACK.credits
        assert await _status(pool, payment_id) == "success"
        assert bot.sent == [
            (user.tg_id, f"✅ Оплата получена: +{PACK.credits} кр. На балансе {PACK.credits} кр.")
        ]

        probe = await client.post("/payments/kassai/webhook", data={})
        assert probe.status == 200 and await probe.text() == "YES"
        stored = await pool.fetchval("SELECT payload FROM webhook_logs ORDER BY id LIMIT 1")
        assert "SIGN" not in stored, "signatures are never stored"
    finally:
        await client.close()


async def test_kassai_unknown_order_is_acknowledged(pool: asyncpg.Pool) -> None:
    flow = PaymentFlow(BillingRepository(pool), _providers())
    client = await _client(flow, FakeBot())
    try:
        response = await client.post("/payments/kassai/webhook", data=_kassa_payload(999, 10))
        assert response.status == 200
        assert await pool.fetchval("SELECT count(*) FROM webhook_logs") == 1
    finally:
        await client.close()


# --- Platega -----------------------------------------------------------------------------


def _platega(payment_id: int, status: str, amount: object = PACK.rub) -> dict[str, object]:
    return {
        "id": f"tx-{payment_id}",
        "status": status,
        "amount": amount,
        "payload": str(payment_id),
    }


@pytest.mark.parametrize(
    ("status", "amount", "expected_status", "credited"),
    [
        ("PENDING", PACK.rub, "pending", 0),  # no verdict yet: untouched
        ("CANCELED", PACK.rub, "cancelled", 0),
        ("CONFIRMED", PACK.rub - 50, "pending", 0),  # underpaid: no credit, alert
        ("CONFIRMED", PACK.rub + 9, "success", PACK.credits),  # provider fee on top: fine
        ("CONFIRMED", f"{PACK.rub}.00 RUB", "success", PACK.credits),
    ],
)
async def test_platega_verdicts(
    pool: asyncpg.Pool, status: str, amount: object, expected_status: str, credited: int
) -> None:
    user = await _user(pool)
    flow, _, payment_id = await _external(pool, "platega", user)
    client = await _client(flow, FakeBot())
    try:
        unauthorized = await client.post(
            "/payments/platega/webhook", json=_platega(payment_id, "CONFIRMED")
        )
        assert unauthorized.status == 401
        response = await client.post(
            "/payments/platega/webhook",
            json=_platega(payment_id, status, amount),
            headers=_PLATEGA_HEADERS,
        )
        assert response.status == 200
        assert await _status(pool, payment_id) == expected_status
        assert await _balance(pool, user) == credited
    finally:
        await client.close()


async def test_reconcile_recovers_lost_callback_once(pool: asyncpg.Pool) -> None:
    user = await _user(pool)
    flow, stub, payment_id = await _external(pool, "platega", user)
    stub.status[f"tx-{payment_id}"] = await stub.verify_webhook(_platega(payment_id, "CONFIRMED"))

    assert await flow.reconcile(min_age_s=90, limit=10) == [], "fresh payments are skipped"
    await pool.execute(
        "UPDATE payments SET created_at = now() - interval '5 minutes' WHERE id = $1", payment_id
    )
    settled = await flow.reconcile(min_age_s=90, limit=10)
    assert [o.payment.id for o in settled] == [payment_id] and settled[0].tg_id == user.tg_id
    assert await flow.reconcile(min_age_s=90, limit=10) == [], "never twice"
    assert await _balance(pool, user) == PACK.credits


async def test_provider_failure_closes_our_payment(pool: asyncpg.Pool) -> None:
    user = await _user(pool)
    providers = _providers()
    providers["kassai_sbp"] = StubProvider(providers["kassai_sbp"], fail=True)
    flow = PaymentFlow(BillingRepository(pool), providers)
    with pytest.raises(ProviderError):
        await flow.start_external(user, PACK, "kassai_sbp")
    assert await pool.fetchval("SELECT status FROM payments") == "cancelled"


async def test_stale_invoices_are_closed(pool: asyncpg.Pool) -> None:
    user = await _user(pool)
    _, _, payment_id = await _external(pool, "platega", user)
    billing = BillingRepository(pool)
    assert await billing.close_stale_pending(24) == 0
    await pool.execute(
        "UPDATE payments SET created_at = now() - interval '2 days' WHERE id = $1", payment_id
    )
    assert await billing.close_stale_pending(24) == 1
    assert await _status(pool, payment_id) == "cancelled"


def test_platega_amount_shapes() -> None:
    assert amount_of({"amount": 299}) == 299
    assert amount_of({"amount": "299.00 RUB"}) == 299
    assert amount_of({"paymentDetails": {"amount": "299"}}) == 299
    assert amount_of({"paymentDetails": "51.21 RUB"}) == 51
    assert amount_of({}) == 0


def test_kassai_request_signature_is_order_independent() -> None:
    kassa = cast(KassaiSBP, _providers()["kassai_sbp"])
    a = kassa.sign_request({"b": 2, "a": 1})
    assert a == kassa.sign_request({"a": 1, "b": 2}) and len(a) == 64


def test_pay_methods_screen_lists_rub_then_stars() -> None:
    text, keyboard = render_pay_methods(PACK, _providers())
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert labels == [
        "🏦 СБП / QR — 299 ₽",
        "🏦 СБП — 299 ₽",
        "💳 Карта РФ — 299 ₽",
        "⭐ Telegram Stars — 250 ⭐",
        "‹ Назад",
    ]
    assert "100 кредитов" in text
    _, link = render_pay_link(PACK, "https://pay.test/1")
    assert link.inline_keyboard[0][0].url == "https://pay.test/1"
