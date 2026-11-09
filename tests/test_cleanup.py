from __future__ import annotations

import os
from pathlib import Path

from app.services.cleanup import remove_expired

_NOW = 1_000_000.0
_DAY = 86_400.0


def _touch(path: Path, age: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (_NOW - age, _NOW - age))
    return path


def test_removes_only_expired_files_and_keeps_bot_api_state(tmp_path: Path) -> None:
    old_src = _touch(tmp_path / "src" / "a.mp4", 2 * _DAY)
    fresh_src = _touch(tmp_path / "src" / "b.mp4", 60)
    old_copy = _touch(tmp_path / "jobs" / "7" / "0.mp4", 2 * _DAY)
    old_tg_media = _touch(tmp_path / "tg" / "123:abc" / "documents" / "file_1.mp4", 2 * _DAY)
    binlog = _touch(tmp_path / "tg" / "123:abc" / "td.binlog", 2 * _DAY)
    unrelated = _touch(tmp_path / "other" / "keep.txt", 2 * _DAY)

    removed = remove_expired(tmp_path, ttl_seconds=_DAY, now=_NOW)

    assert removed == 3
    assert not old_src.exists() and not old_copy.exists() and not old_tg_media.exists()
    assert fresh_src.exists() and binlog.exists() and unrelated.exists()
    assert not (tmp_path / "jobs" / "7").exists(), "empty job dir is pruned"
    assert (tmp_path / "src").is_dir()


def test_missing_dirs_are_fine(tmp_path: Path) -> None:
    assert remove_expired(tmp_path, ttl_seconds=1, now=_NOW) == 0
