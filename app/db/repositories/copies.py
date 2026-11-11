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
                INSERT INTO copies
                    (job_id, copy_index, seed, plan_json, output_key, status, profile_id)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $10)
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
            copy.plan.metadata.profile_id if copy.plan.metadata is not None else None,
        )
        return int(copy_id)

    async def done_keys(self, job_id: int) -> dict[int, str]:
        """copy_index -> output_key of copies already accepted (an arq retry resumes)."""
        rows = await self.fetch(
            "SELECT copy_index, output_key FROM copies WHERE job_id = $1 AND status = $2",
            job_id,
            _DONE,
        )
        return {int(r["copy_index"]): r["output_key"] for r in rows}

    async def purge_fingerprints(self, older_than_days: int) -> int:
        """Retention from the privacy policy: drop fingerprints of old copies."""
        result = await self.execute(
            """
            DELETE FROM fingerprints f USING copies c, jobs j
            WHERE f.copy_id = c.id AND c.job_id = j.id
              AND j.created_at < now() - make_interval(days => $1)
            """,
            older_than_days,
        )
        return int(result.split()[-1])

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
