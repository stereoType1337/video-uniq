"""Money-moving SQL: reservations, settlement, payments, trial, referral, expiry.

Every method runs in one DB transaction and goes through ``ledger.apply_entry``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

import asyncpg

from app.core.exceptions import PaymentAmountMismatchError, PaymentNotFoundError
from app.db.base import BaseRepository
from app.db.repositories.jobs import INSERT_JOB_SQL, Job, NewJob, job_insert_args, to_job
from app.db.repositories.ledger import EntryKind, LedgerEntry, apply_entry


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Payment:
    id: int
    user_id: int
    pack_code: str
    credits: int
    amount: int
    currency: str
    provider: str
    status: PaymentStatus


@dataclass(frozen=True, slots=True)
class NewPayment:
    user_id: int
    pack_code: str
    credits: int
    amount: int
    currency: str
    provider: str


@dataclass(frozen=True, slots=True)
class ProviderVerdict:
    payment_id: int
    provider_payment_id: str
    amount: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    payment: Payment
    just_completed: bool  # notify the user only when True
    balance: int | None


@dataclass(frozen=True, slots=True)
class Settlement:
    user_id: int
    reserved: int
    charged: int
    refunded: int
    balance: int


@dataclass(frozen=True, slots=True)
class ReferralReward:
    referrer_id: int
    referrer_tg_id: int
    referred_tg_id: int


class BillingRepository(BaseRepository):
    async def create_job_reserving(self, job: NewJob, cost: int) -> Job:
        """Create the job and take ``cost`` credits atomically.

        InsufficientBalanceError rolls everything back, so no job exists without payment."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(INSERT_JOB_SQL, *job_insert_args(job, cost))
            created = to_job(row)
            entry = LedgerEntry(job.user_id, -cost, EntryKind.RESERVE, job_id=created.id)
            await apply_entry(conn, entry)
            return created

    async def settle_job(self, job_id: int, delivered: int) -> Settlement | None:
        """Charge only delivered copies, refund the rest. None if already settled."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE jobs
                SET settled_at = now(),
                    copies_delivered = LEAST($2, copies),
                    credits_charged = credits_reserved * LEAST($2, copies) / copies
                WHERE id = $1 AND settled_at IS NULL
                RETURNING user_id, credits_reserved, credits_charged
                """,
                job_id,
                max(delivered, 0),
            )
            if row is None:
                return None
            refund = row["credits_reserved"] - row["credits_charged"]
            balance = await self._balance(conn, row["user_id"])
            if refund > 0:
                entry = LedgerEntry(row["user_id"], refund, EntryKind.REFUND, job_id=job_id)
                balance = await apply_entry(conn, entry)
            return Settlement(
                row["user_id"], row["credits_reserved"], row["credits_charged"], refund, balance
            )

    async def stale_unsettled_jobs(self, finished_minutes: int, running_minutes: int) -> list[int]:
        """Jobs whose worker died between processing and settlement, or never finished."""
        rows = await self.fetch(
            """
            SELECT id FROM jobs
            WHERE settled_at IS NULL AND (
                (status IN ('done', 'partial', 'failed')
                 AND finished_at < now() - make_interval(mins => $1))
                OR (status = 'processing' AND created_at < now() - make_interval(mins => $2))
            )
            ORDER BY id
            """,
            finished_minutes,
            running_minutes,
        )
        return [int(r["id"]) for r in rows]

    async def create_payment(self, payment: NewPayment) -> Payment:
        row = await self.fetchrow(
            """
            INSERT INTO payments (user_id, pack_code, credits, amount, currency, provider)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            payment.user_id,
            payment.pack_code,
            payment.credits,
            payment.amount,
            payment.currency,
            payment.provider,
        )
        if row is None:
            raise RuntimeError("payment insert returned no row")
        return _to_payment(row)

    async def get_payment(self, payment_id: int) -> Payment | None:
        row = await self.fetchrow("SELECT * FROM payments WHERE id = $1", payment_id)
        return _to_payment(row) if row is not None else None

    async def apply_verdict(self, verdict: ProviderVerdict) -> PaymentOutcome:
        """The single settlement path for payments. Idempotent: a replayed verdict for a
        settled payment returns just_completed=False and credits nothing."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM payments WHERE id = $1 FOR UPDATE", verdict.payment_id
            )
            if row is None:
                raise PaymentNotFoundError(verdict.payment_id)
            payment = _to_payment(row)
            if payment.status is not PaymentStatus.PENDING:
                return PaymentOutcome(payment, just_completed=False, balance=None)
            if verdict.amount < payment.amount:
                raise PaymentAmountMismatchError(payment.id, payment.amount, verdict.amount)
            await conn.execute(
                """
                UPDATE payments
                SET status = 'success', provider_payment_id = $2,
                    provider_payload = $3::jsonb, paid_at = now()
                WHERE id = $1
                """,
                payment.id,
                verdict.provider_payment_id,
                json.dumps(verdict.payload),
            )
            await conn.execute(
                "UPDATE users SET last_purchase_at = now() WHERE id = $1", payment.user_id
            )
            entry = LedgerEntry(
                payment.user_id,
                payment.credits,
                EntryKind.PURCHASE,
                payment_id=payment.id,
                provider=payment.provider,
            )
            balance = await apply_entry(conn, entry)
            done = replace(payment, status=PaymentStatus.SUCCESS)
            return PaymentOutcome(done, just_completed=True, balance=balance)

    async def grant_trial(self, user_id: int, credits: int) -> int | None:
        """Free credits once per user. Returns the new balance, or None if already granted."""
        async with self._pool.acquire() as conn, conn.transaction():
            granted = await conn.fetchval(
                """
                UPDATE users SET trial_granted_at = now()
                WHERE id = $1 AND trial_granted_at IS NULL
                RETURNING id
                """,
                user_id,
            )
            if granted is None:
                return None
            return await apply_entry(conn, LedgerEntry(user_id, credits, EntryKind.TRIAL))

    async def reward_referral(self, referred_id: int, bonus: int) -> ReferralReward | None:
        """Both sides get ``bonus`` once, after the invited user's first delivered job."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE users u SET referral_rewarded_at = now()
                FROM users r
                WHERE u.id = $1 AND u.referred_by = r.id AND u.referral_rewarded_at IS NULL
                RETURNING r.id AS referrer_id, r.tg_id AS referrer_tg_id, u.tg_id AS referred_tg_id
                """,
                referred_id,
            )
            if row is None:
                return None
            await apply_entry(conn, LedgerEntry(referred_id, bonus, EntryKind.REFERRAL))
            await apply_entry(conn, LedgerEntry(row["referrer_id"], bonus, EntryKind.REFERRAL))
            return ReferralReward(row["referrer_id"], row["referrer_tg_id"], row["referred_tg_id"])

    async def admin_adjust(self, user_id: int, amount: int) -> int:
        """Manual credit grant (positive) or removal (negative) from the admin panel."""
        async with self._pool.acquire() as conn, conn.transaction():
            return await apply_entry(conn, LedgerEntry(user_id, amount, EntryKind.ADMIN))

    async def expire_credits(self, ttl_days: int) -> int:
        """Burn balances of users whose last purchase is older than ``ttl_days``.

        Users who never bought (trial/referral credits only) are not affected.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, balance FROM users
                WHERE balance > 0 AND last_purchase_at < now() - make_interval(days => $1)
                FOR UPDATE SKIP LOCKED
                """,
                ttl_days,
            )
            for row in rows:
                entry = LedgerEntry(row["id"], -row["balance"], EntryKind.EXPIRE)
                await apply_entry(conn, entry)
            return len(rows)

    @staticmethod
    async def _balance(conn: asyncpg.Connection, user_id: int) -> int:
        return int(await conn.fetchval("SELECT balance FROM users WHERE id = $1", user_id))


def _to_payment(row: asyncpg.Record) -> Payment:
    return Payment(
        id=row["id"],
        user_id=row["user_id"],
        pack_code=row["pack_code"],
        credits=row["credits"],
        amount=row["amount"],
        currency=row["currency"],
        provider=row["provider"],
        status=PaymentStatus(row["status"]),
    )
