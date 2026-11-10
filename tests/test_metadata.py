from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest

from app.db.repositories.profiles import ProfileRepository
from app.services.metadata import (
    MetadataPlan,
    _find_top_box,
    choose_metadata,
    ffmpeg_args,
    load_profiles,
    patch_container,
)
from app.services.probe import probe_source
from app.services.render_plan import Mode, derive_seed, generate_plan
from app.services.uniqualizer import render_copy

_NOW = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)
_PROFILES = load_profiles()
_FFMPEG_MARKERS = (b"Lavf", b"Lavc", b"x264", b"FFmpeg", b"FFMP")


def _many(allow_camera: bool, count: int = 300) -> list[MetadataPlan]:
    return [
        choose_metadata(derive_seed("t", 1, i), _PROFILES, _NOW, allow_camera) for i in range(count)
    ]


def test_starter_profiles_load_and_are_complete() -> None:
    assert {p.kind for p in _PROFILES} == {"editor", "camera"}
    for p in _PROFILES:
        assert p.container in {"mp4", "mov"} and p.filename and p.encoder
        assert "lav" not in p.encoder.lower(), "the point is to hide FFmpeg"


def test_choice_is_deterministic_per_seed() -> None:
    seed = derive_seed("t", 9, 3)
    assert choose_metadata(seed, _PROFILES, _NOW, True) == choose_metadata(
        seed, _PROFILES, _NOW, True
    )


def test_camera_profiles_only_when_allowed() -> None:
    cameras = {p.id for p in _PROFILES if p.kind == "camera"}
    assert not {m.profile_id for m in _many(allow_camera=False)} & cameras
    assert {m.profile_id for m in _many(allow_camera=True)} & cameras


def test_capture_time_is_recent_daytime_and_never_future() -> None:
    for meta in _many(allow_camera=True):
        utc = datetime.strptime(meta.creation_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert _NOW - timedelta(days=11) < utc <= _NOW - timedelta(minutes=10)
    iphone = [m for m in _many(True, 600) if m.profile_id == "iphone"]
    for meta in iphone:
        local = datetime.strptime(
            meta.tags["com.apple.quicktime.creationdate"], "%Y-%m-%dT%H:%M:%S%z"
        )
        assert 8 <= local.hour <= 23
        assert meta.filename.startswith("IMG_") and meta.filename.endswith(".MOV")


def test_ffmpeg_args_by_container() -> None:
    by_id = {m.profile_id: m for m in _many(True, 600)}
    mov = ffmpeg_args(by_id["iphone"], has_audio=True)
    mp4 = ffmpeg_args(by_id["samsung"], has_audio=False)
    assert mov[-2:] == ["-f", "mov"] and "-brand" not in mov
    assert mp4[-4:] == ["-brand", "mp42", "-f", "mp4"]
    assert "handler_name=Core Media Audio" in mov and not any("s:a" in a for a in mp4)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
@pytest.mark.parametrize("profile_id", [p.id for p in _PROFILES])
async def test_render_leaves_no_ffmpeg_traces_in_container(tmp_path: Path, profile_id: str) -> None:
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x568:d=2",
         "-f", "lavfi", "-i", "sine=d=2", "-shortest", str(src)],
        check=True,
    )  # fmt: skip
    info = await probe_source(src)
    profile = next(p for p in _PROFILES if p.id == profile_id)
    meta = choose_metadata("seed", (profile,), _NOW, allow_camera=True)
    plan = replace(generate_plan("seed", 0, info, Mode.BASE), metadata=meta)
    dst = tmp_path / meta.filename
    await render_copy(plan, src, dst)

    data = dst.read_bytes()
    assert data[12:16] == b"\0\0\0\0", "minor_version patched to 0"
    with dst.open("rb") as fh:
        moov = _find_top_box(fh, b"moov")
    assert moov is not None
    header = data[: moov[0] + moov[1]]  # ftyp + moov: everything except the media payload
    assert not [m for m in _FFMPEG_MARKERS if m in header], profile_id
    out = await probe_source(dst)
    assert out.width == plan.geometry.out_w


def test_patch_container_ignores_non_mp4(tmp_path: Path) -> None:
    junk = tmp_path / "x.mp4"
    junk.write_bytes(b"not a container at all")
    meta = choose_metadata("s", _PROFILES, _NOW, True)
    patch_container(junk, replace(meta, vendor="appl"))
    assert junk.read_bytes() == b"not a container at all"


async def test_sync_keeps_admin_choices(pool: asyncpg.Pool) -> None:
    repo = ProfileRepository(pool)
    await repo.sync(_PROFILES)
    assert {p.id for p in await repo.enabled()} == {p.id for p in _PROFILES}
    assert await repo.toggle("capcut") is False
    await pool.execute("UPDATE metadata_profiles SET weight = 9 WHERE id = 'inshot_android'")

    changed = tuple(replace(p, encoder="NEW") if p.id == "capcut" else p for p in _PROFILES)
    await repo.sync(changed)

    enabled = {p.id: p for p in await repo.enabled()}
    assert "capcut" not in enabled, "admin switched it off; sync must not switch it back"
    assert enabled["inshot_android"].weight == 9
    states = {s.profile.id: s for s in await repo.states()}
    assert states["capcut"].profile.encoder == "NEW", "definition follows the repo file"
    assert await repo.toggle("missing") is None
