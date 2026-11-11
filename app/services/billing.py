"""Prices, packs, accounts and the Stars purchase flow. SQL lives in BillingRepository."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace

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
from app.services.render_plan import Mode

logger = structlog.get_logger()

STARS_CURRENCY = "XTR"
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


# Draft packs. Market: UniqueVid 200 cr = 500 ⭐; Uniqly $5 = 25 copies.
PACKS: tuple[Pack, ...] = (
    Pack(code="p100", credits=100, stars=250),
    Pack(code="p300", credits=300, stars=650),
    Pack(code="p1000", credits=1000, stars=1800),
)


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
    """Telegram Stars: invoice -> pre_checkout -> successful_payment -> apply_verdict."""

    def __init__(self, billing: BillingRepository) -> None:
        self._billing = billing

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
