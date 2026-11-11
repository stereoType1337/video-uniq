from __future__ import annotations

from app.db.base import BaseRepository


class AppSettingsRepository(BaseRepository):
    async def get(self, key: str) -> str | None:
        value = await self.fetchval("SELECT value FROM app_settings WHERE key = $1", key)
        return str(value) if value is not None else None

    async def set(self, key: str, value: str) -> None:
        await self.execute(
            """
            INSERT INTO app_settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            key,
            value,
        )

    async def delete(self, key: str) -> None:
        await self.execute("DELETE FROM app_settings WHERE key = $1", key)
