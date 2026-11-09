from __future__ import annotations

import json
from dataclasses import dataclass

from app.db.base import BaseRepository
from app.services.fingerprints import (
    Fingerprint,
    decode_chromaprint,
    decode_phash,
    encode_chromaprint,
    encode_phash,
)
from app.services.render_plan import RenderPlan

_DONE = "done"


@dataclass(frozen=True, slots=True)
class AcceptedCopy:
    job_id: int
    plan: RenderPlan
    output_key: str
    fingerprint: Fingerprint


class CopyRepository(BaseRepository):
    async def save_accepted(self, copy: AcceptedCopy) -> int:
        """Insert the copy and its fingerprint in one statement (atomic)."""
        copy_id = await self.fetchval(
            """
            WITH c AS (
                INSERT INTO copies (job_id, copy_index, seed, plan_json, output_key, status)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                RETURNING id
            )
            INSERT INTO fingerprints (copy_id, phash_json, chromaprint, sha256)
            SELECT c.id, $7::jsonb, $8, $9 FROM c
            RETURNING copy_id
            """,
            copy.job_id,
            copy.plan.copy_index,
            copy.plan.seed,
            json.dumps(copy.plan.to_json()),
            copy.output_key,
            _DONE,
            json.dumps(encode_phash(copy.fingerprint.phash)),
            encode_chromaprint(copy.fingerprint.chromaprint),
            copy.fingerprint.sha256,
        )
        return int(copy_id)

    async def done_indexes(self, job_id: int) -> set[int]:
        rows = await self.fetch(
            "SELECT copy_index FROM copies WHERE job_id = $1 AND status = $2", job_id, _DONE
        )
        return {int(r["copy_index"]) for r in rows}

    async def recent_fingerprints(self, source_sha256: str, limit: int) -> list[Fingerprint]:
        """Fingerprints of earlier copies made from the same source file, newest first."""
        rows = await self.fetch(
            """
            SELECT f.phash_json, f.chromaprint, f.sha256
            FROM fingerprints f
            JOIN copies c ON c.id = f.copy_id
            JOIN jobs j ON j.id = c.job_id
            WHERE j.source_sha256 = $1
            ORDER BY c.id DESC
            LIMIT $2
            """,
            source_sha256,
            limit,
        )
        return [
            Fingerprint(
                sha256=r["sha256"],
                phash=decode_phash(json.loads(r["phash_json"])),
                chromaprint=decode_chromaprint(r["chromaprint"]),
            )
            for r in rows
        ]
