from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from app.db.base import BaseRepository
from app.services.render_plan import AudioMode, Mode


@dataclass(frozen=True, slots=True)
class User:
    id: int
    tg_id: int
    username: str | None
    balance: int
    mode: Mode
    copies: int
    ref_code: str
    referred_by: int | None
    audio_mode: AudioMode = AudioMode.SHIFT
    camera_profiles: bool = False


@dataclass(frozen=True, slots=True)
class NewUser:
    tg_id: int
    username: str | None
    ref_code: str
    referrer_code: str | None = None  # from /start ref_<code>; only used on first registration
    utm: str | None = None


class UserRepository(BaseRepository):
    async def get_by_tg_id(self, tg_id: int) -> User | None:
        row = await self.fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)
        return to_user(row) if row is not None else None

    async def get(self, user_id: int) -> User | None:
        row = await self.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return to_user(row) if row is not None else None

    async def register(self, new: NewUser) -> tuple[User, bool]:
        """Insert or refresh the user. Returns (user, created). Referrer and UTM are written
        only on creation; a self-referral or unknown code is silently ignored."""
        row = await self.fetchrow(
            """
            INSERT INTO users (tg_id, username, ref_code, referred_by, utm)
            VALUES ($1, $2, $3,
                    (SELECT id FROM users WHERE ref_code = $4 AND tg_id <> $1),
                    $5)
            ON CONFLICT (tg_id) DO UPDATE SET username = EXCLUDED.username
            RETURNING *, (xmax = 0) AS created
            """,
            new.tg_id,
            new.username,
            new.ref_code,
            new.referrer_code,
            new.utm,
        )
        if row is None:
            raise RuntimeError("user upsert returned no row")
        return to_user(row), bool(row["created"])

    async def update_settings(
        self, user_id: int, mode: Mode, copies: int, audio_mode: AudioMode | None = None
    ) -> User:
        row = await self.fetchrow(
            """
            UPDATE users SET mode = $2, copies = $3, audio_mode = COALESCE($4, audio_mode)
            WHERE id = $1 RETURNING *
            """,
            user_id,
            mode.value,
            copies,
            audio_mode.value if audio_mode is not None else None,
        )
        if row is None:
            raise RuntimeError(f"user {user_id} vanished")
        return to_user(row)

    async def toggle_camera_profiles(self, user_id: int) -> User:
        row = await self.fetchrow(
            "UPDATE users SET camera_profiles = NOT camera_profiles WHERE id = $1 RETURNING *",
            user_id,
        )
        if row is None:
            raise RuntimeError(f"user {user_id} vanished")
        return to_user(row)

    async def get_balance(self, tg_id: int) -> int:
        value = await self.fetchval("SELECT balance FROM users WHERE tg_id = $1", tg_id)
        return int(value) if value is not None else 0


def to_user(row: asyncpg.Record) -> User:
    return User(
        id=row["id"],
        tg_id=row["tg_id"],
        username=row["username"],
        balance=row["balance"],
        mode=Mode(row["mode"]),
        copies=row["copies"],
        ref_code=row["ref_code"],
        referred_by=row["referred_by"],
        audio_mode=AudioMode(row["audio_mode"]),
        camera_profiles=row["camera_profiles"],
    )
