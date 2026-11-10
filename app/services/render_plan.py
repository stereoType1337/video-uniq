"""Deterministic render plans: one seed -> one point in the parameter space.

All functions here are pure. The same (seed, copy_index, source, mode) always yields the
same plan, so any delivered copy can be reproduced from its stored seed.
"""

from __future__ import annotations

import hashlib
import hmac
import random
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class Mode(StrEnum):
    BASE = "base"
    HD = "hd"
    HD_PLUS = "hd_plus"


@dataclass(frozen=True, slots=True)
class ModeLimits:
    max_short_side: int
    intensity: float
    visible_effects: bool


MODE_LIMITS: dict[Mode, ModeLimits] = {
    Mode.BASE: ModeLimits(max_short_side=720, intensity=1.0, visible_effects=False),
    Mode.HD: ModeLimits(max_short_side=1080, intensity=1.3, visible_effects=False),
    Mode.HD_PLUS: ModeLimits(max_short_side=1080, intensity=1.3, visible_effects=True),
}

# Measured on 8 vCPU (2026-11-10): veryfast 13.7 HD copies/min, faster 9.1, fast 8.0,
# medium 7.0, with no gain in pHash distance (computed on decoded frames).
_PRESETS = ("veryfast",)
_PROFILES = ("high", "main")
_EQ_FREQS = (250, 500, 1000, 2000, 4000)
_AUDIO_BITRATES_K = (96, 128, 160)
# Measured: zoom below ~4 % or |pitch shift| below ~0.5 % leaves the copy too close to the
# source (pHash < 5 bits, Chromaprint BER < 0.1), so the gate would reject it anyway.
_MIN_ZOOM = 1.045
_MIN_PITCH_SHIFT = 0.006
_MIN_FPS = 24.0
_MAX_FPS = 60.0


@dataclass(frozen=True, slots=True)
class SourceInfo:
    width: int
    height: int
    fps: float
    duration: float
    sample_rate: int | None  # None when the source has no audio stream
    container: str = ""  # ffprobe format_name, e.g. "mov,mp4,m4a,3gp,3g2,mj2"

    @property
    def has_audio(self) -> bool:
        return self.sample_rate is not None


@dataclass(frozen=True, slots=True)
class GeometryPlan:
    scale_w: int
    scale_h: int
    crop_w: int
    crop_h: int
    crop_x: int
    crop_y: int
    pad_w: int
    pad_h: int
    pad_x: int
    pad_y: int
    sar_num: int
    sar_den: int

    @property
    def out_w(self) -> int:
        return self.crop_w + self.pad_w

    @property
    def out_h(self) -> int:
        return self.crop_h + self.pad_h


@dataclass(frozen=True, slots=True)
class TimePlan:
    speed: float  # > 1 means faster playback
    fps: float
    start_offset: float


@dataclass(frozen=True, slots=True)
class ColorPlan:
    brightness: float
    contrast: float
    saturation: float
    gamma: float
    hue: float  # degrees
    vignette: float | None  # lens angle in radians, HD+ only
    noise: int | None  # temporal noise strength, HD+ only


@dataclass(frozen=True, slots=True)
class AudioPlan:
    sample_rate: int
    pitch: float  # 1.0 keeps the tone
    eq_freq: int
    eq_gain: float  # dB
    delay_ms: int
    bitrate_k: int


@dataclass(frozen=True, slots=True)
class CodecPlan:
    crf: int
    preset: str
    gop: int
    profile: str


@dataclass(frozen=True, slots=True)
class RenderPlan:
    seed: str
    copy_index: int
    mode: Mode
    geometry: GeometryPlan
    time: TimePlan
    color: ColorPlan
    audio: AudioPlan | None
    codec: CodecPlan

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def derive_seed(secret: str, job_id: int, copy_index: int) -> str:
    message = f"{job_id}:{copy_index}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def generate_plan(seed: str, copy_index: int, source: SourceInfo, mode: Mode) -> RenderPlan:
    rng = random.Random(seed)
    limits = MODE_LIMITS[mode]
    time_plan = _time(rng, source, limits)
    return RenderPlan(
        seed=seed,
        copy_index=copy_index,
        mode=mode,
        geometry=_geometry(rng, source, limits),
        time=time_plan,
        color=_color(rng, limits),
        audio=_audio(rng, source),
        codec=_codec(rng, time_plan.fps),
    )


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def _geometry(rng: random.Random, source: SourceInfo, limits: ModeLimits) -> GeometryPlan:
    k = limits.intensity
    scale = min(1.0, limits.max_short_side / min(source.width, source.height))
    base_w, base_h = source.width * scale, source.height * scale
    pad_w = _even(rng.randint(8, 24)) if rng.random() < 0.4 else 0
    pad_h = _even(rng.randint(8, 24)) if rng.random() < 0.4 else 0
    crop_w = _even(min(base_w * rng.uniform(1 - 0.04 * k, 0.99), base_w - pad_w))
    crop_h = _even(min(base_h * rng.uniform(1 - 0.04 * k, 0.99), base_h - pad_h))
    zoom = rng.uniform(_MIN_ZOOM, 1 + 0.08 * k)
    scale_w = max(_even(base_w * zoom), crop_w)
    scale_h = max(_even(base_h * zoom), crop_h)
    sar_num, sar_den = _sar(rng)
    return GeometryPlan(
        scale_w=scale_w,
        scale_h=scale_h,
        crop_w=crop_w,
        crop_h=crop_h,
        crop_x=rng.randint(0, scale_w - crop_w),
        crop_y=rng.randint(0, scale_h - crop_h),
        pad_w=pad_w,
        pad_h=pad_h,
        pad_x=rng.randint(0, pad_w),
        pad_y=rng.randint(0, pad_h),
        sar_num=sar_num,
        sar_den=sar_den,
    )


def _sar(rng: random.Random) -> tuple[int, int]:
    if rng.random() >= 0.4:
        return 1, 1
    den = rng.randint(100_000, 220_000)
    delta = rng.randint(1, den // 2000) * rng.choice((-1, 1))  # up to 0.05 %
    return den + delta, den


def _time(rng: random.Random, source: SourceInfo, limits: ModeLimits) -> TimePlan:
    k = limits.intensity
    magnitude = rng.uniform(0.01, min(0.06 * k, 0.08))
    speed = round(1 + magnitude * rng.choice((-1, 1)), 3)
    min_fps = min(source.fps, _MIN_FPS)
    fps = min(max(source.fps * rng.uniform(0.93, 1.005), min_fps), _MAX_FPS)
    max_offset = min(0.25, source.duration * 0.05)
    return TimePlan(
        speed=speed,
        fps=round(fps, 2),
        start_offset=round(rng.uniform(0, max_offset), 3),
    )


def _color(rng: random.Random, limits: ModeLimits) -> ColorPlan:
    k = limits.intensity
    vignette = round(rng.uniform(0.2, 0.4), 3) if limits.visible_effects else None
    noise = rng.randint(2, 5) if limits.visible_effects else None
    return ColorPlan(
        brightness=round(rng.uniform(-0.02, 0.02) * k, 4),
        contrast=round(1 + rng.uniform(-0.04, 0.04) * k, 4),
        saturation=round(1 + rng.uniform(-0.13, 0.08) * k, 4),
        gamma=round(1 + rng.uniform(-0.03, 0.03) * k, 4),
        hue=round(rng.uniform(-4, 4) * k, 2),
        vignette=vignette,
        noise=noise,
    )


def _audio(rng: random.Random, source: SourceInfo) -> AudioPlan | None:
    if source.sample_rate is None:
        return None
    return AudioPlan(
        sample_rate=source.sample_rate,
        pitch=round(1 + rng.uniform(_MIN_PITCH_SHIFT, 0.02) * rng.choice((-1, 1)), 4),
        eq_freq=rng.choice(_EQ_FREQS),
        eq_gain=round(rng.uniform(-3, 3), 2),
        delay_ms=rng.randint(0, 30),
        bitrate_k=rng.choice(_AUDIO_BITRATES_K),
    )


def _codec(rng: random.Random, fps: float) -> CodecPlan:
    return CodecPlan(
        crf=rng.randint(20, 26),
        preset=rng.choice(_PRESETS),
        gop=int(fps * rng.randint(2, 10)),
        profile=rng.choice(_PROFILES),
    )
