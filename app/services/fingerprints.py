"""Video/audio/file fingerprints and the quality gate that decides if a copy is unique enough.

pHash: N frames sampled evenly over the clip (not every k-th frame), so clips with different
fps, speed and start offset are still compared position-to-position. Distance is the mean
Hamming distance in bits (0..64) between aligned frames.

Chromaprint: raw fpcalc fingerprint; distance is the best bit error rate (0..1) over a range
of offsets. Unrelated audio sits around 0.5, an identical track near 0.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import statistics
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import ProbeError

PHASH_SAMPLES = 32
_FRAME_SIDE = 32
_DCT_SIDE = 8
_FRAME_BYTES = _FRAME_SIDE * _FRAME_SIDE
_COS = [
    [math.cos(math.pi * (2 * x + 1) * u / (2 * _FRAME_SIDE)) for x in range(_FRAME_SIDE)]
    for u in range(_DCT_SIDE)
]
_CHROMA_MAX_OFFSET = 40
_CHROMA_LENGTH_S = 120
_READ_CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class Fingerprint:
    sha256: str
    phash: tuple[int, ...]
    chromaprint: tuple[int, ...] | None


@dataclass(frozen=True, slots=True)
class Tools:
    ffmpeg_bin: str = "ffmpeg"
    fpcalc_bin: str = "fpcalc"


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    min_phash_to_original: float
    min_phash_to_copies: float
    min_audio_to_original: float


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    accepted: bool
    phash_to_original: float
    phash_to_nearest_copy: float | None
    audio_to_original: float | None
    reason: str | None


async def compute_fingerprint(
    path: Path, duration: float, has_audio: bool, tools: Tools
) -> Fingerprint:
    sha_task = asyncio.to_thread(file_sha256, path)
    phash_task = _video_phash(path, duration, tools.ffmpeg_bin)
    if has_audio:
        sha, phash, chroma = await asyncio.gather(
            sha_task, phash_task, _chromaprint(path, tools.fpcalc_bin)
        )
        return Fingerprint(sha256=sha, phash=phash, chromaprint=chroma)
    sha, phash = await asyncio.gather(sha_task, phash_task)
    return Fingerprint(sha256=sha, phash=phash, chromaprint=None)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


async def _run(*cmd: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise ProbeError(cmd[-1], stderr.decode(errors="replace")[-500:])
    return stdout


async def _video_phash(path: Path, duration: float, ffmpeg_bin: str) -> tuple[int, ...]:
    rate = PHASH_SAMPLES / max(duration, 0.1)
    raw = await _run(
        ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(path),
        "-vf", f"fps={rate:.6f},scale={_FRAME_SIDE}:{_FRAME_SIDE}:flags=area,format=gray",
        "-frames:v", str(PHASH_SAMPLES),
        "-f", "rawvideo", "pipe:1",
    )  # fmt: skip
    frames = [raw[i : i + _FRAME_BYTES] for i in range(0, len(raw), _FRAME_BYTES)]
    frames = [f for f in frames if len(f) == _FRAME_BYTES]
    if not frames:
        raise ProbeError(str(path), "no frames decoded for pHash")
    return tuple(await asyncio.to_thread(lambda: [phash_frame(f) for f in frames]))


async def _chromaprint(path: Path, fpcalc_bin: str) -> tuple[int, ...] | None:
    raw = await _run(fpcalc_bin, "-raw", "-json", "-length", str(_CHROMA_LENGTH_S), str(path))
    values: list[int] = json.loads(raw).get("fingerprint") or []
    return tuple(v & 0xFFFFFFFF for v in values) or None


def phash_frame(pixels: bytes) -> int:
    """64-bit DCT pHash of a 32x32 grayscale frame."""
    rows = [pixels[r * _FRAME_SIDE : (r + 1) * _FRAME_SIDE] for r in range(_FRAME_SIDE)]
    partial = [[sum(c * p for c, p in zip(cu, row, strict=True)) for row in rows] for cu in _COS]
    coefs = [sum(c * t for c, t in zip(cv, tu, strict=True)) for cv in _COS for tu in partial]
    median = statistics.median(coefs)
    bits = 0
    for coef in coefs:
        bits = (bits << 1) | (coef > median)
    return bits


def phash_distance(a: Sequence[int], b: Sequence[int]) -> float:
    if not a or not b:
        raise ValueError("empty pHash sequence")
    total = 0
    for i, value in enumerate(a):
        j = round(i * (len(b) - 1) / max(len(a) - 1, 1))
        total += min((value ^ other).bit_count() for other in b[max(0, j - 1) : j + 2])
    return total / len(a)


def chromaprint_distance(a: Sequence[int], b: Sequence[int]) -> float:
    min_overlap = max(1, min(len(a), len(b)) // 2)
    best = 1.0
    for offset in range(-_CHROMA_MAX_OFFSET, _CHROMA_MAX_OFFSET + 1):
        pairs = list(zip(a[max(offset, 0) :], b[max(-offset, 0) :], strict=False))
        if len(pairs) < min_overlap:
            continue
        errors = sum((x ^ y).bit_count() for x, y in pairs)
        best = min(best, errors / (32 * len(pairs)))
    return best


def evaluate_copy(
    candidate: Fingerprint,
    original: Fingerprint,
    previous: Sequence[Fingerprint],
    thresholds: QualityThresholds,
) -> QualityVerdict:
    to_original = phash_distance(candidate.phash, original.phash)
    to_copies = [phash_distance(candidate.phash, p.phash) for p in previous]
    nearest = min(to_copies) if to_copies else None
    audio = None
    if candidate.chromaprint is not None and original.chromaprint is not None:
        audio = chromaprint_distance(candidate.chromaprint, original.chromaprint)
    reason = _rejection_reason(candidate, previous, (to_original, nearest, audio), thresholds)
    return QualityVerdict(
        accepted=reason is None,
        phash_to_original=to_original,
        phash_to_nearest_copy=nearest,
        audio_to_original=audio,
        reason=reason,
    )


def _rejection_reason(
    candidate: Fingerprint,
    previous: Sequence[Fingerprint],
    distances: tuple[float, float | None, float | None],
    thresholds: QualityThresholds,
) -> str | None:
    to_original, nearest, audio = distances
    if any(p.sha256 == candidate.sha256 for p in previous):
        return "sha256_duplicate"
    if to_original < thresholds.min_phash_to_original:
        return "phash_close_to_original"
    if nearest is not None and nearest < thresholds.min_phash_to_copies:
        return "phash_close_to_copy"
    if audio is not None and audio < thresholds.min_audio_to_original:
        return "audio_close_to_original"
    return None


def encode_chromaprint(values: tuple[int, ...] | None) -> str | None:
    if values is None:
        return None
    return base64.b64encode(struct.pack(f"<{len(values)}I", *values)).decode()


def decode_chromaprint(text: str | None) -> tuple[int, ...] | None:
    if text is None:
        return None
    raw = base64.b64decode(text)
    return struct.unpack(f"<{len(raw) // 4}I", raw)


def encode_phash(values: tuple[int, ...]) -> list[str]:
    return [f"{v:016x}" for v in values]


def decode_phash(values: Sequence[str]) -> tuple[int, ...]:
    return tuple(int(v, 16) for v in values)
