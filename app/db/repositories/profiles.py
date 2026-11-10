from __future__ import annotations

import json
from dataclasses import dataclass, replace

from app.db.base import BaseRepository
from app.services.metadata import Profile, profile_definition, to_profile


@dataclass(frozen=True, slots=True)
class ProfileState:
    profile: Profile
    enabled: bool


class ProfileRepository(BaseRepository):
    async def sync(self, profiles: tuple[Profile, ...]) -> None:
        """Upsert definitions from the repo file. New profiles take the file's weight;
        existing rows keep their admin-set weight and enabled flag."""
        await self._pool.executemany(
            """
            INSERT INTO metadata_profiles (id, kind, weight, definition)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (id) DO UPDATE
              SET kind = EXCLUDED.kind, definition = EXCLUDED.definition, updated_at = now()
            """,
            [(p.id, p.kind, p.weight, json.dumps(profile_definition(p))) for p in profiles],
        )

    async def enabled(self) -> tuple[Profile, ...]:
        rows = await self.fetch(
            "SELECT weight, definition FROM metadata_profiles WHERE enabled AND weight > 0"
            " ORDER BY id"
        )
        return tuple(
            replace(to_profile(json.loads(r["definition"])), weight=float(r["weight"]))
            for r in rows
        )

    async def states(self) -> list[ProfileState]:
        rows = await self.fetch(
            "SELECT weight, enabled, definition FROM metadata_profiles ORDER BY kind DESC, id"
        )
        return [
            ProfileState(
                replace(to_profile(json.loads(r["definition"])), weight=float(r["weight"])),
                bool(r["enabled"]),
            )
            for r in rows
        ]

    async def toggle(self, profile_id: str) -> bool | None:
        value = await self.fetchval(
            """
            UPDATE metadata_profiles SET enabled = NOT enabled, updated_at = now()
            WHERE id = $1 RETURNING enabled
            """,
            profile_id,
        )
        return bool(value) if value is not None else None
