from __future__ import annotations

import os
import shutil
from pathlib import Path

import asyncpg
import pytest

from app.services.backup import BackupError, make_backup, rotate


def test_rotate_keeps_newest(tmp_path: Path) -> None:
    for stamp in ("20261101-000000", "20261102-000000", "20261103-000000"):
        (tmp_path / f"uniq-{stamp}.dump").write_bytes(b"x")
    (tmp_path / "other.txt").write_bytes(b"keep me")
    assert rotate(tmp_path, keep=2) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "other.txt",
        "uniq-20261102-000000.dump",
        "uniq-20261103-000000.dump",
    ]


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump is not installed")
async def test_backup_is_a_restorable_archive(pool: asyncpg.Pool, tmp_path: Path) -> None:
    dsn = os.environ["TEST_DATABASE_URL"]
    await pool.execute("INSERT INTO users (tg_id, ref_code) VALUES (1, 'r')")
    backup = await make_backup(dsn, tmp_path, keep=14)
    assert backup.path.exists() and backup.size > 0
    assert backup.toc_entries > 10, "tables, indexes and data of every migration"


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump is not installed")
async def test_backup_failure_raises(tmp_path: Path) -> None:
    with pytest.raises(BackupError) as info:
        await make_backup("postgresql://nobody@localhost:1/none", tmp_path, keep=14)
    assert info.value.step == "pg_dump"
    assert not list(tmp_path.glob("*.dump"))
