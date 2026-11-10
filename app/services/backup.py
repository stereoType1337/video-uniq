"""Daily database backup: pg_dump (custom format), verified with pg_restore --list, kept
locally (rotated) and sent to the admins' Telegram as the off-server copy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from app.core.exceptions import AppError

logger = structlog.get_logger()

_PREFIX = "uniq-"
_SUFFIX = ".dump"


class BackupError(AppError):
    def __init__(self, step: str, stderr: str) -> None:
        super().__init__(f"Backup failed at {step}: {stderr[-300:]}")
        self.step = step
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class Backup:
    path: Path
    size: int
    toc_entries: int  # objects in the archive; 0 would mean an empty/broken dump


async def make_backup(database_url: str, directory: Path, keep: int) -> Backup:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = directory / f"{_PREFIX}{stamp}{_SUFFIX}"
    try:
        await _run(
            "pg_dump", "pg_dump", "--format=custom", f"--dbname={database_url}", "-f", str(path)
        )
        listing = await _run("pg_restore", "pg_restore", "--list", str(path))
        entries = sum(1 for line in listing.splitlines() if line and not line.startswith(";"))
        if entries == 0:
            raise BackupError("verify", "archive lists no objects")
    except BackupError:
        path.unlink(missing_ok=True)  # a broken dump must never count as a backup in rotation
        raise
    removed = rotate(directory, keep)
    logger.info("backup_done", file=path.name, size=path.stat().st_size, rotated=removed)
    return Backup(path, path.stat().st_size, entries)


def rotate(directory: Path, keep: int) -> int:
    dumps = sorted(directory.glob(f"{_PREFIX}*{_SUFFIX}"), reverse=True)  # names sort by time
    for old in dumps[keep:]:
        old.unlink(missing_ok=True)
    return max(len(dumps) - keep, 0)


async def _run(step: str, *cmd: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise BackupError(step, stderr.decode(errors="replace"))
    return stdout.decode(errors="replace")
