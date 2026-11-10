"""Read-mostly SQL for the admin panel. One query per screen, numbers from one source."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from app.db.base import BaseRepository
from app.db.repositories.users import User, to_user


@dataclass(frozen=True, slots=True)
class PeriodCounts:
    today: int
    week: int
    month: int
    total: int


@dataclass(frozen=True, slots=True)
class Summary:
    users: PeriodCounts  # registrations
    jobs: PeriodCounts
    copies: PeriodCounts  # copies charged (= delivered)
    stars: PeriodCounts  # Stars received
    payments: PeriodCounts
    active_jobs: int
    failed_jobs_week: int
    credits_outstanding: int
    referred_users: int


@dataclass(frozen=True, slots=True)
class UserCard:
    user: User
    created_at: datetime
    last_purchase_at: datetime | None
    utm: str | None
    jobs_total: int
    copies_delivered: int
    stars_paid: int
    invited: int


@dataclass(frozen=True, slots=True)
class LedgerRow:
    created_at: datetime
    kind: str
    amount: int
    balance_after: int


_PERIODS = """
    count(*) FILTER (WHERE {ts} >= date_trunc('day', now())) AS today,
    count(*) FILTER (WHERE {ts} >= now() - interval '7 days') AS week,
    count(*) FILTER (WHERE {ts} >= now() - interval '30 days') AS month,
    count(*) AS total
"""
_SUM_PERIODS = """
    COALESCE(sum({v}) FILTER (WHERE {ts} >= date_trunc('day', now())), 0) AS today,
    COALESCE(sum({v}) FILTER (WHERE {ts} >= now() - interval '7 days'), 0) AS week,
    COALESCE(sum({v}) FILTER (WHERE {ts} >= now() - interval '30 days'), 0) AS month,
    COALESCE(sum({v}), 0) AS total
"""


class AdminRepository(BaseRepository):
    async def summary(self) -> Summary:
        async with self._pool.acquire() as conn:
            users = await _periods(conn, f"SELECT {_PERIODS.format(ts='created_at')} FROM users")
            jobs = await _periods(conn, f"SELECT {_PERIODS.format(ts='created_at')} FROM jobs")
            delivered = _SUM_PERIODS.format(v="copies_delivered", ts="settled_at")
            copies = await _periods(
                conn, f"SELECT {delivered} FROM jobs WHERE settled_at IS NOT NULL"
            )
            paid = "FROM payments WHERE status = 'success'"
            stars = await _periods(
                conn, f"SELECT {_SUM_PERIODS.format(v='amount', ts='paid_at')} {paid}"
            )
            payments = await _periods(conn, f"SELECT {_PERIODS.format(ts='paid_at')} {paid}")
            extra = await conn.fetchrow(
                """
                SELECT
                  (SELECT count(*) FROM jobs WHERE status IN ('queued', 'processing')) AS active,
                  (SELECT count(*) FROM jobs
                    WHERE status = 'failed' AND created_at >= now() - interval '7 days') AS failed,
                  (SELECT COALESCE(sum(balance), 0) FROM users) AS credits,
                  (SELECT count(*) FROM users WHERE referred_by IS NOT NULL) AS referred
                """
            )
        return Summary(
            users=users,
            jobs=jobs,
            copies=copies,
            stars=stars,
            payments=payments,
            active_jobs=int(extra["active"]),
            failed_jobs_week=int(extra["failed"]),
            credits_outstanding=int(extra["credits"]),
            referred_users=int(extra["referred"]),
        )

    async def find_user(self, query: str) -> User | None:
        """By Telegram id or @username (case-insensitive)."""
        text = query.strip().removeprefix("@")
        if text.isdigit():
            row = await self.fetchrow("SELECT * FROM users WHERE tg_id = $1", int(text))
        else:
            row = await self.fetchrow("SELECT * FROM users WHERE lower(username) = lower($1)", text)
        return to_user(row) if row is not None else None

    async def user_card(self, user_id: int) -> UserCard | None:
        row = await self.fetchrow(
            """
            SELECT u.*,
              (SELECT count(*) FROM jobs j WHERE j.user_id = u.id) AS jobs_total,
              (SELECT COALESCE(sum(copies_delivered), 0)
                 FROM jobs j WHERE j.user_id = u.id) AS copies,
              (SELECT COALESCE(sum(amount), 0) FROM payments p
                 WHERE p.user_id = u.id AND status = 'success') AS stars,
              (SELECT count(*) FROM users r WHERE r.referred_by = u.id) AS invited
            FROM users u WHERE u.id = $1
            """,
            user_id,
        )
        if row is None:
            return None
        return UserCard(
            user=to_user(row),
            created_at=row["created_at"],
            last_purchase_at=row["last_purchase_at"],
            utm=row["utm"],
            jobs_total=int(row["jobs_total"]),
            copies_delivered=int(row["copies"]),
            stars_paid=int(row["stars"]),
            invited=int(row["invited"]),
        )

    async def ledger(self, user_id: int, limit: int = 15) -> list[LedgerRow]:
        rows = await self.fetch(
            """
            SELECT created_at, kind, amount, balance_after FROM transactions
            WHERE user_id = $1 ORDER BY id DESC LIMIT $2
            """,
            user_id,
            limit,
        )
        return [
            LedgerRow(r["created_at"], r["kind"], r["amount"], r["balance_after"]) for r in rows
        ]

    async def export_users(self) -> list[asyncpg.Record]:
        return await self.fetch(
            """
            SELECT tg_id, username, balance, mode, copies, utm, referred_by IS NOT NULL AS referred,
                   created_at, last_purchase_at
            FROM users ORDER BY id
            """
        )

    async def export_payments(self) -> list[asyncpg.Record]:
        return await self.fetch(
            """
            SELECT p.id, u.tg_id, p.pack_code, p.credits, p.amount, p.currency, p.provider,
                   p.status, p.provider_payment_id, p.created_at, p.paid_at
            FROM payments p JOIN users u ON u.id = p.user_id ORDER BY p.id
            """
        )


async def _periods(conn: asyncpg.Connection, query: str) -> PeriodCounts:
    row = await conn.fetchrow(query)
    assert row is not None
    return PeriodCounts(int(row["today"]), int(row["week"]), int(row["month"]), int(row["total"]))
