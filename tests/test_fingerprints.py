from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.fingerprints import (
    Fingerprint,
    QualityThresholds,
    Tools,
    chromaprint_distance,
    compute_fingerprint,
    decode_chromaprint,
    decode_phash,
    encode_chromaprint,
    encode_phash,
    evaluate_copy,
    is_audio_informative,
    phash_distance,
    phash_frame,
)

_THRESHOLDS = QualityThresholds(
    min_phash_to_original=6.0, min_phash_to_copies=5.0, min_audio_to_original=0.1
)


def _gradient(shift: int = 0) -> bytes:
    return bytes((x * 8 + y * 3 + shift) % 256 for y in range(32) for x in range(32))


def _random_ints(seed: int, count: int, bits: int) -> tuple[int, ...]:
    rng = random.Random(seed)
    return tuple(rng.getrandbits(bits) for _ in range(count))


def test_phash_is_stable_and_brightness_tolerant() -> None:
    frame = _gradient()
    assert phash_frame(frame) == phash_frame(frame)
    brighter = bytes(min(255, p + 10) for p in frame)
    assert (phash_frame(frame) ^ phash_frame(brighter)).bit_count() <= 4


def test_phash_separates_different_images() -> None:
    noise = bytes(random.Random(1).getrandbits(8) for _ in range(1024))
    assert (phash_frame(_gradient()) ^ phash_frame(noise)).bit_count() >= 16


def test_phash_distance_aligns_sequences_of_different_length() -> None:
    seq = _random_ints(1, 32, 64)
    stretched = tuple(v for v in seq for _ in range(2))  # same content, twice the samples
    assert phash_distance(seq, seq) == 0
    assert phash_distance(seq, stretched) == 0
    assert phash_distance(seq, _random_ints(2, 32, 64)) > 20


def test_chromaprint_distance_finds_offset() -> None:
    track = _random_ints(3, 300, 32)
    assert chromaprint_distance(track, track[5:]) == 0
    assert chromaprint_distance(track, _random_ints(4, 300, 32)) == pytest.approx(0.5, abs=0.05)


def test_encoding_round_trips() -> None:
    phash = _random_ints(5, 8, 64)
    chroma = _random_ints(6, 50, 32)
    assert decode_phash(encode_phash(phash)) == phash
    assert decode_chromaprint(encode_chromaprint(chroma)) == chroma
    assert decode_chromaprint(encode_chromaprint(None)) is None


def _fp(sha: str, phash: tuple[int, ...], chroma: tuple[int, ...] | None = None) -> Fingerprint:
    return Fingerprint(sha256=sha, phash=phash, chromaprint=chroma)


def test_steady_audio_is_not_informative() -> None:
    assert not is_audio_informative((0xDEADBEEF,) * 200), "a steady tone repeats itself"
    assert not is_audio_informative(_random_ints(7, 10, 32)), "too short to judge"
    assert is_audio_informative(_random_ints(8, 200, 32))


def test_gate_ignores_audio_when_source_audio_is_steady() -> None:
    steady = (0x12345678,) * 200
    original = _fp("o", _random_ints(1, 32, 64), steady)
    candidate = _fp("c", _random_ints(2, 32, 64), steady)  # BER 0 to the original
    verdict = evaluate_copy(candidate, original, [], _THRESHOLDS)
    assert verdict.accepted and verdict.audio_to_original is None


def test_gate_accepts_distinct_copy() -> None:
    original = _fp("o", _random_ints(1, 32, 64), _random_ints(10, 100, 32))
    candidate = _fp("c", _random_ints(2, 32, 64), _random_ints(11, 100, 32))
    verdict = evaluate_copy(candidate, original, [], _THRESHOLDS)
    assert verdict.accepted and verdict.reason is None


@pytest.mark.parametrize(
    ("candidate", "previous", "reason"),
    [
        (_fp("o2", _random_ints(1, 32, 64)), [], "phash_close_to_original"),
        (_fp("c", _random_ints(2, 32, 64)), [_fp("p", _random_ints(2, 32, 64))],
         "phash_close_to_copy"),
        (_fp("dup", _random_ints(2, 32, 64)), [_fp("dup", _random_ints(3, 32, 64))],
         "sha256_duplicate"),
        (_fp("c", _random_ints(2, 32, 64), _random_ints(10, 100, 32)), [],
         "audio_close_to_original"),
    ],
)  # fmt: skip
def test_gate_rejects(candidate: Fingerprint, previous: list[Fingerprint], reason: str) -> None:
    original = _fp("o", _random_ints(1, 32, 64), _random_ints(10, 100, 32))
    verdict = evaluate_copy(candidate, original, previous, _THRESHOLDS)
    assert not verdict.accepted
    assert verdict.reason == reason


@pytest.mark.skipif(shutil.which("fpcalc") is None, reason="fpcalc is not installed")
async def test_short_audio_skips_chromaprint(tmp_path: Path) -> None:
    clip = tmp_path / "short.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=d=1",
         "-f", "lavfi", "-i", "sine=d=1", "-shortest", str(clip)],
        check=True,
    )  # fmt: skip
    fingerprint = await compute_fingerprint(clip, 1.0, has_audio=True, tools=Tools())
    assert fingerprint.chromaprint is None
    assert len(fingerprint.phash) > 0
