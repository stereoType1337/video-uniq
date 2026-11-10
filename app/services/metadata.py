"""Metadata profiles: what app or device a copy looks like it came from.

A profile fixes the container (mp4/mov), brand, track handler names, the encoder string
(instead of FFmpeg's "Lavc libx264"), format tags and the file name pattern. The choice and
a plausible capture time are derived from the copy's seed, so a copy stays reproducible
(the chosen values are stored in the plan JSON).

Starter profiles are APPROXIMATE (see data/metadata_profiles.json); calibrate them with
app/scripts/extract_profile.py on real exports.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO

PROFILES_FILE = Path(__file__).resolve().parent / "data" / "metadata_profiles.json"
_FTYP = b"ftyp"
_MINOR_VERSION_OFFSET = 12  # size(4) + "ftyp"(4) + major brand(4)
_FFMPEG_VENDOR = b"FFMP"
# Audience is mostly UTC+2..+5; +3 (Moscow) dominates.
_TZ_OFFSETS = ((3, 6), (2, 1), (4, 1), (5, 1), (1, 1))
_MAX_DAYS_BACK = 9
_DAY_START_H, _DAY_END_H = 8, 23


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    kind: str  # editor | camera
    weight: float
    container: str  # mp4 | mov
    brand: str
    filename: str
    video_handler: str
    audio_handler: str
    encoder: str
    vendor: str | None = None
    models: tuple[str, ...] = ()
    os_versions: tuple[str, ...] = ()
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MetadataPlan:
    profile_id: str
    filename: str
    container: str
    brand: str
    video_handler: str
    audio_handler: str
    encoder: str
    vendor: str | None
    creation_time: str  # UTC, ISO 8601 with Z
    tags: dict[str, str]


@lru_cache(maxsize=4)
def load_profiles(path: Path = PROFILES_FILE) -> tuple[Profile, ...]:
    raw: dict[str, Any] = json.loads(path.read_text())
    return tuple(to_profile(p) for p in raw["profiles"])


def profile_definition(profile: Profile) -> dict[str, Any]:
    return asdict(profile)


def to_profile(raw: dict[str, Any]) -> Profile:
    return Profile(
        id=raw["id"],
        kind=raw["kind"],
        weight=float(raw["weight"]),
        container=raw["container"],
        brand=raw["brand"],
        filename=raw["filename"],
        video_handler=raw["video_handler"],
        audio_handler=raw["audio_handler"],
        encoder=raw["encoder"],
        vendor=raw.get("vendor"),
        models=tuple(raw.get("models", ())),
        os_versions=tuple(raw.get("os_versions", ())),
        tags=dict(raw.get("tags", {})),
    )


def choose_metadata(
    seed: str, profiles: tuple[Profile, ...], now: datetime, allow_camera: bool
) -> MetadataPlan:
    rng = random.Random(f"{seed}:metadata")
    pool = [p for p in profiles if allow_camera or p.kind != "camera"]
    profile = rng.choices(pool, weights=[p.weight for p in pool])[0]
    local = _capture_time(rng, now)
    values = {
        "date": local,
        "ms": f"{rng.randint(0, 999):03d}",
        "n1": str(rng.randint(1, 9)),
        "n2": f"{rng.randint(1, 99):02d}",
        "n4": f"{rng.randint(1, 9999):04d}",
        "model": rng.choice(profile.models) if profile.models else "",
        "os": rng.choice(profile.os_versions) if profile.os_versions else "",
        "local_iso": local.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return MetadataPlan(
        profile_id=profile.id,
        filename=profile.filename.format(**values),
        container=profile.container,
        brand=profile.brand,
        video_handler=profile.video_handler,
        audio_handler=profile.audio_handler,
        encoder=profile.encoder,
        vendor=profile.vendor,
        creation_time=local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        tags={k: v.format(**values) for k, v in profile.tags.items()},
    )


def _capture_time(rng: random.Random, now: datetime) -> datetime:
    """A daytime moment in the last ~10 days, in a plausible local time zone, never future."""
    hours = rng.choices([h for h, _ in _TZ_OFFSETS], weights=[w for _, w in _TZ_OFFSETS])[0]
    tz = timezone(timedelta(hours=hours))
    now_local = now.astimezone(tz)
    days_back = rng.randint(0, _MAX_DAYS_BACK)
    if days_back == 0 and now_local.hour < _DAY_START_H + 1:
        days_back = 1
    day = (now_local - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    latest = now_local if days_back == 0 else day.replace(hour=_DAY_END_H, minute=59)
    earliest = day.replace(hour=_DAY_START_H)
    span = max(int((latest - earliest).total_seconds()) - 600, 1)  # at least 10 min ago
    return earliest + timedelta(seconds=rng.randint(0, span))


def ffmpeg_args(meta: MetadataPlan, has_audio: bool) -> list[str]:
    args = [
        "-metadata:s:v:0", f"handler_name={meta.video_handler}",
        "-metadata:s:v:0", f"encoder={meta.encoder}",
        "-metadata", f"creation_time={meta.creation_time}",
    ]  # fmt: skip
    if has_audio:
        args += ["-metadata:s:a:0", f"handler_name={meta.audio_handler}"]
        args += ["-metadata:s:a:0", "encoder="]
    if meta.vendor:
        args += ["-metadata:s:v:0", f"vendor_id={meta.vendor}"]
        if has_audio:
            args += ["-metadata:s:a:0", f"vendor_id={meta.vendor}"]
    for key, value in meta.tags.items():
        args += ["-metadata", f"{key}={value}"]
    flags = "+faststart+use_metadata_tags" if meta.tags else "+faststart"
    args += ["-movflags", flags]
    if meta.container == "mov":
        return args + ["-f", "mov"]
    return args + ["-brand", meta.brand, "-f", "mp4"]


def patch_container(path: Path, meta: MetadataPlan) -> None:
    """Fix what FFmpeg's muxer won't let us set, in place and size-preserving."""
    patch_minor_version(path)
    if meta.vendor:
        patch_vendor(path, meta.vendor)


def patch_vendor(path: Path, vendor: str) -> int:
    """The mov muxer writes vendor "FFMP" into every sample entry and ignores vendor_id.
    Replace it inside the moov box only (mdat bytes are never touched). Returns the count."""
    new = vendor.encode("ascii")[:4].ljust(4, b"\0")
    with path.open("r+b") as fh:
        found = _find_top_box(fh, b"moov")
        if found is None:
            return 0
        start, size = found
        fh.seek(start)
        box = bytearray(fh.read(size))
        count = box.count(_FFMPEG_VENDOR)
        if count:
            fh.seek(start)
            fh.write(bytes(box.replace(_FFMPEG_VENDOR, new)))
        return count


def _find_top_box(fh: BinaryIO, kind: bytes) -> tuple[int, int] | None:
    offset = 0
    while True:
        fh.seek(offset)
        header = fh.read(16)
        if len(header) < 8:
            return None
        size, box = int.from_bytes(header[:4], "big"), header[4:8]
        if size == 1:
            size = int.from_bytes(header[8:16], "big")
        if size < 8:
            return None
        if box == kind:
            return offset, size
        offset += size


def patch_minor_version(path: Path, value: int = 0) -> bool:
    """FFmpeg always writes minor_version 512; cameras and phones write 0. Same size, in place."""
    with path.open("r+b") as fh:
        head = fh.read(16)
        if len(head) < 16 or head[4:8] != _FTYP:
            return False
        fh.seek(_MINOR_VERSION_OFFSET)
        fh.write(value.to_bytes(4, "big"))
    return True
