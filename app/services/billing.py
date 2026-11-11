"""Prices, packs, accounts and the Stars purchase flow. SQL lives in BillingRepository."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from typing import Any

import structlog

from app.db.repositories.billing import (
    BillingRepository,
    NewPayment,
    Payment,
    PaymentOutcome,
    PaymentStatus,
    ProviderVerdict,
)
from app.db.repositories.users import NewUser, User, UserRepository
from app.services.payments.ports import PaymentProvider, ProviderError, WebhookVerdict
from app.services.render_plan import Mode

logger = structlog.get_logger()

STARS_CURRENCY = "XTR"
_SECRET_FIELDS = {"SIGN", "SIGNATURE", "X-SECRET"}
STARS_PROVIDER = "stars"
_REF_PREFIX = "ref_"
_MAX_UTM_LEN = 64

# Credits per copy. Draft prices (README), compare docs/COMPETITOR_*.md before launch.
PRICE_PER_COPY: dict[Mode, int] = {Mode.BASE: 1, Mode.HD: 2, Mode.HD_PLUS: 3}


@dataclass(frozen=True, slots=True)
class Pack:
    code: str
    credits: int
    stars: int
    rub: int  # price via RUB providers (SBP / card)


# Draft packs. Market: UniqueVid 200 cr = 500 ⭐; Uniqly $5 = 25 copies.
PACKS: tuple[Pack, ...] = (
    Pack(code="p100", credits=100, stars=250, rub=299),
    Pack(code="p300", credits=300, stars=650, rub=749),
    Pack(code="p1000", credits=1000, stars=1800, rub=1990),
)
RUB_CURRENCY = "RUB"


MIN_RUN_COST = min(PRICE_PER_COPY.values())  # one copy in the cheapest mode


def job_cost(mode: Mode, copies: int) -> int:
    return PRICE_PER_COPY[mode] * copies


def find_pack(code: str) -> Pack | None:
    return next((p for p in PACKS if p.code == code), None)


def referral_link(bot_username: str, ref_code: str) -> str:
    return f"https://t.me/{bot_username}?start={_REF_PREFIX}{ref_code}"


@dataclass(frozen=True, slots=True)
class Account:
    user: User
    created: bool
    trial_credits: int  # granted right now, 0 otherwise


class AccountService:
    def __init__(
        self, users: UserRepository, billing: BillingRepository, free_credits: int
    ) -> None:
        self._users = users
        self._billing = billing
        self._free_credits = free_credits

    async def ensure(
        self, tg_id: int, username: str | None, start_arg: str | None = None
    ) -> Account:
        referrer_code, utm = _parse_start_arg(start_arg)
        new = NewUser(tg_id, username, secrets.token_urlsafe(6), referrer_code, utm)
        user, created = await self._users.register(new)
        if not created or self._free_credits <= 0:
            return Account(user, created, trial_credits=0)
        balance = await self._billing.grant_trial(user.id, self._free_credits)
        if balance is None:
            return Account(user, created, trial_credits=0)
        logger.info("user_registered", user_id=user.id, referred=user.referred_by is not None)
        return Account(replace(user, balance=balance), created, trial_credits=self._free_credits)


LEGAL_START_ARGS = ("terms", "privacy")  # deep links t.me/<bot>?start=terms


def _parse_start_arg(arg: str | None) -> tuple[str | None, str | None]:
    if not arg or arg in LEGAL_START_ARGS:
        return None, None
    if arg.startswith(_REF_PREFIX):
        return arg.removeprefix(_REF_PREFIX) or None, None
    return None, arg[:_MAX_UTM_LEN]


class PaymentFlow:
    """Stars: invoice -> pre_checkout -> successful_payment -> apply_verdict.
    RUB providers: payment(pending) -> provider link -> webhook / reconcile -> apply_verdict."""

    def __init__(
        self, billing: BillingRepository, providers: dict[str, PaymentProvider] | None = None
    ) -> None:
        self._billing = billing
        self.providers = providers or {}

    async def start_external(self, user: User, pack: Pack, provider_name: str) -> str:
        """Create our pending payment first (its id goes to the provider), then the provider
        invoice. Returns the URL to pay. A provider failure closes our payment."""
        provider = self.providers[provider_name]
        payment = await self._billing.create_payment(
            NewPayment(user.id, pack.code, pack.credits, pack.rub, RUB_CURRENCY, provider.name)
        )
        try:
            created = await provider.create_payment(payment.id, user.tg_id, pack.rub)
        except ProviderError:
            await self._billing.cancel_pending(payment.id)
            logger.exception(
                "provider_create_failed", provider=provider.name, payment_id=payment.id
            )
            raise
        await self._billing.set_provider_payment_id(payment.id, created.provider_payment_id)
        logger.info("external_payment_created", payment_id=payment.id, provider=provider.name)
        return created.payment_url

    async def process_webhook(
        self, provider_name: str, payload: dict[str, Any]
    ) -> PaymentOutcome | None:
        """Log raw -> verify -> find our payment -> apply_verdict. None: not our payment
        (answer 200 so the provider stops retrying). InvalidWebhookSignatureError propagates."""
        provider = self.providers[provider_name]
        safe = {k: v for k, v in payload.items() if k.upper() not in _SECRET_FIELDS}
        log_id = await self._billing.log_webhook(provider_name, safe)
        verdict = await provider.verify_webhook(payload)
        payment_id = await self._billing.find_payment(
            provider_name, verdict.provider_payment_id, verdict.internal_payment_id
        )
        await self._billing.mark_webhook(log_id, payment_id)
        if payment_id is None:
            logger.warning("webhook_payment_not_found", provider=provider_name)
            return None
        return await self._apply(provider, payment_id, verdict)

    async def reconcile(self, min_age_s: int, limit: int) -> list[PaymentOutcome]:
        """Lost callbacks are routine: poll providers with a status API for stuck payments."""
        pollable = tuple(n for n, p in self.providers.items() if p.can_poll)
        settled: list[PaymentOutcome] = []
        if not pollable:
            return settled
        for row in await self._billing.unsettled_payments(pollable, min_age_s, limit):
            provider = self.providers[row.provider]
            verdict = await provider.fetch_status(row.provider_payment_id)
            if verdict is None:
                continue
            outcome = await self._apply(provider, row.id, verdict)
            if outcome.just_completed:
                settled.append(outcome)
        return settled

    async def _apply(
        self, provider: PaymentProvider, payment_id: int, verdict: WebhookVerdict
    ) -> PaymentOutcome:
        outcome = await self._billing.apply_verdict(
            ProviderVerdict(
                payment_id=payment_id,
                provider_payment_id=verdict.provider_payment_id,
                amount=verdict.amount,
                payload={k: v for k, v in verdict.raw.items() if k.upper() not in _SECRET_FIELDS},
                is_success=verdict.is_success,
                is_final=verdict.is_final,
                check_amount=provider.verifies_amount,
                tolerance=1,
            )
        )
        logger.info(
            "payment_verdict",
            payment_id=payment_id,
            provider=provider.name,
            final=verdict.is_final,
            success=verdict.is_success,
            just_completed=outcome.just_completed,
        )
        return outcome

    async def start(self, user: User, pack: Pack) -> Payment:
        return await self._billing.create_payment(
            NewPayment(
                user_id=user.id,
                pack_code=pack.code,
                credits=pack.credits,
                amount=pack.stars,
                currency=STARS_CURRENCY,
                provider=STARS_PROVIDER,
            )
        )

    async def check_pre_checkout(
        self, user: User | None, payload: str, amount: int, currency: str
    ) -> str | None:
        """Returns an error text for the user, or None when the payment may proceed."""
        payment = await self._payment_from_payload(payload)
        if payment is None or user is None or payment.user_id != user.id:
            return "Счёт не найден. Откройте /buy и выберите пакет заново."
        if payment.status is not PaymentStatus.PENDING:
            return "Этот счёт уже оплачен или закрыт."
        if currency != STARS_CURRENCY or amount != payment.amount:
            return "Сумма счёта изменилась. Откройте /buy и выберите пакет заново."
        return None

    async def complete(self, verdict: ProviderVerdict) -> PaymentOutcome:
        outcome = await self._billing.apply_verdict(verdict)
        logger.info(
            "payment_settled",
            payment_id=verdict.payment_id,
            just_completed=outcome.just_completed,
            credits=outcome.payment.credits,
        )
        return outcome

    async def _payment_from_payload(self, payload: str) -> Payment | None:
        if not payload.isdigit():
            return None
        return await self._billing.get_payment(int(payload))
