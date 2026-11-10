"""Money tests against a real Postgres (``pool`` fixture skips without TEST_DATABASE_URL).

Covers the blueprint checklist: a replayed payment credits once, a short payment does not
credit, concurrent settlement credits once, reservations never overdraw, the ledger always
adds up to the balance.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from app.core.exceptions import (
    InsufficientBalanceError,
    PaymentAmountMismatchError,
    PaymentNotFoundError,
)
from app.db.repositories.billing import BillingRepository, PaymentStatus, ProviderVerdict
from app.db.repositories.jobs import JobRepository, JobStatus, NewJob
from app.db.repositories.users import User, UserRepository
from app.services.billing import (
    PACKS,
    AccountService,
    PaymentFlow,
    job_cost,
)
from app.services.render_plan import Mode

_FREE = 5


def _accounts(pool: asyncpg.Pool) -> AccountService:
    return AccountService(UserRepository(pool), BillingRepository(pool), free_credits=_FREE)


async def _ledger_matches_balances(pool: asyncpg.Pool) -> None:
    mismatched = await pool.fetch(
        """
        SELECT u.id FROM users u
        LEFT JOIN transactions t ON t.user_id = u.id
        GROUP BY u.id, u.balance
        HAVING u.balance <> COALESCE(sum(t.amount), 0)
        """
    )
    assert not mismatched, "every balance must equal the sum of its ledger entries"


async def _funded_user(pool: asyncpg.Pool, tg_id: int, credits: int) -> User:
    account = await _accounts(pool).ensure(tg_id, None)
    flow, billing = PaymentFlow(BillingRepository(pool)), BillingRepository(pool)
    payment = await flow.start(account.user, PACKS[0])
    await pool.execute("UPDATE payments SET credits = $2 WHERE id = $1", payment.id, credits)
    await billing.apply_verdict(ProviderVerdict(payment.id, f"ch{tg_id}", payment.amount, {}))
    user = await UserRepository(pool).get(account.user.id)
    assert user is not None
    return user


def _job(user: User, copies: int = 5, mode: Mode = Mode.BASE) -> NewJob:
    return NewJob(user.id, user.tg_id, None, "sha", "src/x.mp4", mode, copies)


# --- accounts, trial, referral ---------------------------------------------------------


async def test_trial_is_granted_once(pool: asyncpg.Pool) -> None:
    accounts = _accounts(pool)
    first = await accounts.ensure(1, "a", "promo_tiktok")
    again = await accounts.ensure(1, "a2")
    assert first.created and first.trial_credits == _FREE and first.user.balance == _FREE
    assert not again.created and again.trial_credits == 0 and again.user.balance == _FREE
    assert await pool.fetchval("SELECT utm FROM users WHERE tg_id = 1") == "promo_tiktok"
    await _ledger_matches_balances(pool)


async def test_referral_binds_only_on_registration(pool: asyncpg.Pool) -> None:
    accounts = _accounts(pool)
    referrer = (await accounts.ensure(10, None)).user
    invited = (await accounts.ensure(11, None, f"ref_{referrer.ref_code}")).user
    late = (await accounts.ensure(12, None)).user
    late_again = (await accounts.ensure(12, None, f"ref_{referrer.ref_code}")).user
    self_ref = (await accounts.ensure(10, None, f"ref_{referrer.ref_code}")).user

    assert invited.referred_by == referrer.id
    assert late.referred_by is None and late_again.referred_by is None
    assert self_ref.referred_by is None


async def test_referral_reward_paid_once_to_both(pool: asyncpg.Pool) -> None:
    accounts, billing = _accounts(pool), BillingRepository(pool)
    referrer = (await accounts.ensure(20, None)).user
    invited = (await accounts.ensure(21, None, f"ref_{referrer.ref_code}")).user

    reward = await billing.reward_referral(invited.id, bonus=30)
    assert reward is not None and reward.referrer_tg_id == 20 and reward.referred_tg_id == 21
    assert await billing.reward_referral(invited.id, bonus=30) is None
    assert await billing.reward_referral(referrer.id, bonus=30) is None, "not referred"
    balances = await pool.fetch("SELECT tg_id, balance FROM users ORDER BY tg_id")
    assert [(r["tg_id"], r["balance"]) for r in balances] == [(20, _FREE + 30), (21, _FREE + 30)]
    await _ledger_matches_balances(pool)


# --- reservation and settlement --------------------------------------------------------


async def test_reservation_takes_credits_or_creates_nothing(pool: asyncpg.Pool) -> None:
    billing = BillingRepository(pool)
    user = (await _accounts(pool).ensure(30, None)).user  # 5 free credits

    with pytest.raises(InsufficientBalanceError) as info:
        await billing.create_job_reserving(_job(user, 5, Mode.HD), job_cost(Mode.HD, 5))
    assert (info.value.required, info.value.balance) == (10, 5)
    assert await pool.fetchval("SELECT count(*) FROM jobs") == 0

    job = await billing.create_job_reserving(_job(user, 5), job_cost(Mode.BASE, 5))
    assert job.credits_reserved == 5
    assert await pool.fetchval("SELECT balance FROM users WHERE id = $1", user.id) == 0
    await _ledger_matches_balances(pool)


async def test_concurrent_reservations_never_overdraw(pool: asyncpg.Pool) -> None:
    billing = BillingRepository(pool)
    user = (await _accounts(pool).ensure(31, None)).user  # 5 credits, each job costs 3
    results = await asyncio.gather(
        billing.create_job_reserving(_job(user, 3), 3),
        billing.create_job_reserving(_job(user, 3), 3),
        return_exceptions=True,
    )
    assert sum(isinstance(r, InsufficientBalanceError) for r in results) == 1
    assert await pool.fetchval("SELECT balance FROM users WHERE id = $1", user.id) == 2
    await _ledger_matches_balances(pool)


async def test_settlement_charges_only_delivered_and_runs_once(pool: asyncpg.Pool) -> None:
    billing = BillingRepository(pool)
    user = await _funded_user(pool, 32, credits=100)
    job = await billing.create_job_reserving(_job(user, 5, Mode.HD_PLUS), 15)

    first, second = await asyncio.gather(
        billing.settle_job(job.id, 3), billing.settle_job(job.id, 3)
    )
    settled = [s for s in (first, second) if s is not None]
    assert len(settled) == 1, "concurrent settlement must apply once"
    s = settled[0]
    assert (s.reserved, s.charged, s.refunded) == (15, 9, 6)
    assert s.balance == 100 + _FREE - 9
    assert await billing.settle_job(job.id, 5) is None
    assert (await JobRepository(pool).get(job.id)).settled  # type: ignore[union-attr]
    await _ledger_matches_balances(pool)


async def test_failed_job_is_fully_refunded(pool: asyncpg.Pool) -> None:
    billing = BillingRepository(pool)
    user = (await _accounts(pool).ensure(33, None)).user
    job = await billing.create_job_reserving(_job(user, 5), 5)
    settlement = await billing.settle_job(job.id, delivered=0)
    assert settlement is not None and settlement.refunded == 5 and settlement.balance == _FREE


async def test_stale_jobs_are_listed(pool: asyncpg.Pool) -> None:
    billing, jobs = BillingRepository(pool), JobRepository(pool)
    user = await _funded_user(pool, 34, credits=100)
    done = await billing.create_job_reserving(_job(user, 1), 1)
    running = await billing.create_job_reserving(_job(user, 1), 1)
    fresh = await billing.create_job_reserving(_job(user, 1), 1)
    await jobs.set_status(done.id, JobStatus.DONE)
    await jobs.set_status(running.id, JobStatus.PROCESSING)
    await jobs.set_status(fresh.id, JobStatus.DONE)
    await pool.execute(
        "UPDATE jobs SET finished_at = now() - interval '1 hour' WHERE id = $1", done.id
    )
    await pool.execute(
        "UPDATE jobs SET created_at = now() - interval '3 hours' WHERE id = $1", running.id
    )
    assert await billing.stale_unsettled_jobs(10, 120) == [done.id, running.id]


# --- payments --------------------------------------------------------------------------


async def test_payment_credits_once_even_if_replayed(pool: asyncpg.Pool) -> None:
    billing, flow = BillingRepository(pool), PaymentFlow(BillingRepository(pool))
    user = (await _accounts(pool).ensure(40, None)).user
    pack = PACKS[0]
    payment = await flow.start(user, pack)
    verdict = ProviderVerdict(payment.id, "charge-1", pack.stars, {"total_amount": pack.stars})

    outcomes = await asyncio.gather(flow.complete(verdict), flow.complete(verdict))
    assert sorted(o.just_completed for o in outcomes) == [False, True]
    replay = await flow.complete(verdict)
    assert not replay.just_completed and replay.balance is None

    balance = await pool.fetchval("SELECT balance FROM users WHERE id = $1", user.id)
    assert balance == _FREE + pack.credits
    assert (await billing.get_payment(payment.id)).status is PaymentStatus.SUCCESS  # type: ignore[union-attr]
    assert await pool.fetchval(
        "SELECT last_purchase_at IS NOT NULL FROM users WHERE id = $1", user.id
    )
    await _ledger_matches_balances(pool)


async def test_underpaid_or_unknown_payment_does_not_credit(pool: asyncpg.Pool) -> None:
    flow = PaymentFlow(BillingRepository(pool))
    user = (await _accounts(pool).ensure(41, None)).user
    payment = await flow.start(user, PACKS[0])
    with pytest.raises(PaymentAmountMismatchError):
        await flow.complete(ProviderVerdict(payment.id, "c", PACKS[0].stars - 1, {}))
    with pytest.raises(PaymentNotFoundError):
        await flow.complete(ProviderVerdict(999_999, "c2", 1, {}))
    assert await pool.fetchval("SELECT balance FROM users WHERE id = $1", user.id) == _FREE
    assert await pool.fetchval("SELECT status FROM payments WHERE id = $1", payment.id) == "pending"


async def test_pre_checkout_validation(pool: asyncpg.Pool) -> None:
    flow = PaymentFlow(BillingRepository(pool))
    owner = (await _accounts(pool).ensure(42, None)).user
    stranger = (await _accounts(pool).ensure(43, None)).user
    pack = PACKS[1]
    payment = await flow.start(owner, pack)
    payload = str(payment.id)

    assert await flow.check_pre_checkout(owner, payload, pack.stars, "XTR") is None
    assert await flow.check_pre_checkout(stranger, payload, pack.stars, "XTR") is not None
    assert await flow.check_pre_checkout(owner, payload, pack.stars - 1, "XTR") is not None
    assert await flow.check_pre_checkout(owner, payload, pack.stars, "USD") is not None
    assert await flow.check_pre_checkout(owner, "garbage", pack.stars, "XTR") is not None
    await flow.complete(ProviderVerdict(payment.id, "ch", pack.stars, {}))
    assert await flow.check_pre_checkout(owner, payload, pack.stars, "XTR") is not None


# --- expiry ----------------------------------------------------------------------------


async def test_purchased_credits_expire_trial_only_do_not(pool: asyncpg.Pool) -> None:
    billing = BillingRepository(pool)
    buyer = await _funded_user(pool, 50, credits=40)
    trial_only = (await _accounts(pool).ensure(51, None)).user
    await pool.execute(
        "UPDATE users SET last_purchase_at = now() - interval '31 days' WHERE id = $1", buyer.id
    )
    assert await billing.expire_credits(ttl_days=30) == 1
    assert await pool.fetchval("SELECT balance FROM users WHERE id = $1", buyer.id) == 0
    assert await pool.fetchval("SELECT balance FROM users WHERE id = $1", trial_only.id) == _FREE
    assert await billing.expire_credits(ttl_days=30) == 0
    await _ledger_matches_balances(pool)
