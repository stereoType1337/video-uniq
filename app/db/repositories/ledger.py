"""The only code that changes ``users.balance``. Always called inside a DB transaction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import asyncpg

from app.core.exceptions import InsufficientBalanceError


class EntryKind(StrEnum):
    TRIAL = "trial"
    PURCHASE = "purchase"
    RESERVE = "reserve"
    REFUND = "refund"
    REFERRAL = "referral"
    EXPIRE = "expire"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    user_id: int
    amount: int  # signed: negative takes credits away
    kind: EntryKind
    job_id: int | None = None
    payment_id: int | None = None
    provider: str | None = None


async def apply_entry(conn: asyncpg.Connection, entry: LedgerEntry) -> int:
    """Change the balance and record it. Returns the new balance.

    A debit never makes the balance negative: the conditional UPDATE matches no row and
    InsufficientBalanceError is raised, rolling back the surrounding transaction.
    """
    balance = await conn.fetchval(
        """
        UPDATE users SET balance = balance + $2
        WHERE id = $1 AND balance + $2 >= 0
        RETURNING balance
        """,
        entry.user_id,
        entry.amount,
    )
    if balance is None:
        current = await conn.fetchval("SELECT balance FROM users WHERE id = $1", entry.user_id)
        raise InsufficientBalanceError(entry.user_id, -entry.amount, int(current or 0))
    await conn.execute(
        """
        INSERT INTO transactions
            (user_id, amount, kind, balance_after, job_id, payment_id, provider)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        entry.user_id,
        entry.amount,
        entry.kind.value,
        balance,
        entry.job_id,
        entry.payment_id,
        entry.provider,
    )
    return int(balance)
