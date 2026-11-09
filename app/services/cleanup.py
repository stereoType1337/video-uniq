"""Removes sources and copies older than the TTL. Nothing user-supplied is kept longer."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

_OWN_DIRS = ("src", "jobs")
# telegram-bot-api keeps its state (td.binlog) in tg/<bot>/; only media folders are ours to wipe
_TG_MEDIA_GLOBS = ("tg/*/documents", "tg/*/videos", "tg/*/animations", "tg/*/temp")


def remove_expired(work_dir: Path, ttl_seconds: float, now: float | None = None) -> int:
    """Delete files older than ``ttl_seconds``; prune empty job/source directories."""
    deadline = (time.time() if now is None else now) - ttl_seconds
    removed = 0
    for path in _candidate_files(work_dir):
        if path.stat().st_mtime < deadline:
            path.unlink(missing_ok=True)
            removed += 1
    for name in _OWN_DIRS:
        root = work_dir / name
        if root.is_dir():
            for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
                if not any(directory.iterdir()):
                    directory.rmdir()
    return removed


def _candidate_files(work_dir: Path) -> Iterator[Path]:
    roots = [work_dir / name for name in _OWN_DIRS]
    roots += [p for pattern in _TG_MEDIA_GLOBS for p in work_dir.glob(pattern)]
    for root in roots:
        if root.is_dir():
            yield from (p for p in root.rglob("*") if p.is_file())
