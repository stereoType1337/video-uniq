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
    is_success: bool = True
    is_final: bool = True  # False (provider PENDING): leave the payment untouched
    check_amount: bool = True  # unsigned callbacks and Stars: compare with our record
    tolerance: int = 0  # Platega may round; overpayment (provider fee) is always fine


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    payment: Payment
    just_completed: bool  # notify the user only when True
    balance: int | None
    tg_id: int | None = None  # buyer, for the notification


@dataclass(frozen=True, slots=True)
class PendingPayment:
    id: int
    provider: str
    provider_payment_id: str


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

    async def confirm_draft(self, draft: Job, cost: int) -> Job | None:
        """Start a video card: reserve ``cost`` and queue it, atomically.

        Matches the exact mode/copies the user saw, so a concurrent card change cannot run
        at the old price. None if the card is gone (already started, cancelled, expired);
        InsufficientBalanceError leaves it a draft.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE jobs SET status = 'queued', credits_reserved = $5
                WHERE id = $1 AND user_id = $2 AND status = 'draft'
                  AND mode = $3 AND copies = $4
                RETURNING *
                """,
                draft.id,
                draft.user_id,
                draft.mode.value,
                draft.copies,
                cost,
            )
            if row is None:
                return None
            await apply_entry(
                conn, LedgerEntry(draft.user_id, -cost, EntryKind.RESERVE, job_id=draft.id)
            )
            return to_job(row)

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
        """The single settlement path for every provider (webhook, Stars, reconcile).
        Idempotent: a replay for a settled payment returns just_completed=False."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM payments WHERE id = $1 FOR UPDATE", verdict.payment_id
            )
            if row is None:
                raise PaymentNotFoundError(verdict.payment_id)
            payment = _to_payment(row)
            if payment.status is not PaymentStatus.PENDING or not verdict.is_final:
                return PaymentOutcome(payment, just_completed=False, balance=None)
            if verdict.check_amount and verdict.amount < payment.amount - verdict.tolerance:
                raise PaymentAmountMismatchError(payment.id, payment.amount, verdict.amount)
            if not verdict.is_success:
                await conn.execute(
                    "UPDATE payments SET status = 'cancelled' WHERE id = $1", payment.id
                )
                return PaymentOutcome(replace(payment, status=PaymentStatus.CANCELLED), False, None)
            return await self._settle_success(conn, payment, verdict)

    async def _settle_success(
        self, conn: asyncpg.Connection, payment: Payment, verdict: ProviderVerdict
    ) -> PaymentOutcome:
        await conn.execute(
            """
            UPDATE payments
            SET status = 'success', provider_payment_id = COALESCE(provider_payment_id, $2),
                provider_payload = $3::jsonb, paid_at = now()
            WHERE id = $1
            """,
            payment.id,
            verdict.provider_payment_id,
            json.dumps(verdict.payload),
        )
        tg_id = await conn.fetchval(
            "UPDATE users SET last_purchase_at = now() WHERE id = $1 RETURNING tg_id",
            payment.user_id,
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
        return PaymentOutcome(done, just_completed=True, balance=balance, tg_id=tg_id)

    async def set_provider_payment_id(self, payment_id: int, provider_payment_id: str) -> None:
        await self.execute(
            "UPDATE payments SET provider_payment_id = $2 WHERE id = $1",
            payment_id,
            provider_payment_id,
        )

    async def cancel_pending(self, payment_id: int) -> None:
        await self.execute(
            "UPDATE payments SET status = 'cancelled' WHERE id = $1 AND status = 'pending'",
            payment_id,
        )

    async def log_webhook(self, provider: str, payload: dict[str, Any]) -> int:
        """Stored before verification: the audit trail for disputed callbacks."""
        return int(
            await self.fetchval(
                "INSERT INTO webhook_logs (provider, payload) VALUES ($1, $2::jsonb) RETURNING id",
                provider,
                json.dumps(payload),
            )
        )

    async def mark_webhook(self, log_id: int, payment_id: int | None) -> None:
        await self.execute(
            "UPDATE webhook_logs SET processed_at = now(), payment_id = $2 WHERE id = $1",
            log_id,
            payment_id,
        )

    async def find_payment(
        self, provider: str, provider_payment_id: str, internal_id: int | None
    ) -> int | None:
        """Lookup order from the blueprint: provider id -> our id carried in the provider
        payload -> a numeric provider id as our id. The provider must match every time."""
        candidates: list[tuple[str, object]] = [
            ("provider_payment_id = $2", provider_payment_id),
        ]
        if internal_id is not None:
            candidates.append(("id = $2", internal_id))
        if provider_payment_id.isdigit():
            candidates.append(("id = $2", int(provider_payment_id)))
        for where, value in candidates:
            if value in ("", None):
                continue
            found = await self.fetchval(
                f"SELECT id FROM payments WHERE provider = $1 AND {where}",  # noqa: S608
                provider,
                value,
            )
            if found is not None:
                return int(found)
        return None

    async def unsettled_payments(
        self, providers: tuple[str, ...], min_age_s: int, limit: int
    ) -> list[PendingPayment]:
        rows = await self.fetch(
            """
            SELECT id, provider, provider_payment_id FROM payments
            WHERE status = 'pending' AND provider = ANY($1::text[])
              AND provider_payment_id IS NOT NULL
              AND created_at < now() - make_interval(secs => $2)
            ORDER BY id LIMIT $3
            """,
            list(providers),
            min_age_s,
            limit,
        )
        return [PendingPayment(r["id"], r["provider"], r["provider_payment_id"]) for r in rows]

    async def close_stale_pending(self, older_than_hours: int) -> int:
        """Never-paid invoices: closed, not deleted (terminal rows are history)."""
        result = await self.execute(
            """
            UPDATE payments SET status = 'cancelled'
            WHERE status = 'pending' AND created_at < now() - make_interval(hours => $1)
            """,
            older_than_hours,
        )
        return int(result.split()[-1])

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
