"""What sources we accept. Pure checks over ffprobe data; limits mirror the market
(0.5 s – 3 min, up to 4K@30 or 1080p@60, mp4/mov) and keep a single job from hogging a worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.exceptions import AppError
from app.services.render_plan import SourceInfo

_MOV_FAMILY = "mov"  # ffprobe reports mp4, mov and 3gp as "mov,mp4,m4a,3gp,3g2,mj2"
_FPS_TOLERANCE = 1.0  # 59.94 and 60.5 (VFR phones) both count as 60


class RejectReason(StrEnum):
    BAD_FORMAT = "bad_format"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    TOO_LARGE_FRAME = "too_large_frame"
    TOO_HIGH_FPS = "too_high_fps"
    HIGH_FPS_FRAME = "high_fps_frame"


@dataclass(frozen=True, slots=True)
class VideoRules:
    min_duration_s: float = 0.5
    max_duration_s: float = 180.0
    max_long_side: int = 3840
    max_short_side: int = 2160
    max_fps: float = 60.0
    base_fps: float = 30.0  # above this only frames up to high_fps_short_side are allowed
    high_fps_short_side: int = 1080


@dataclass(frozen=True, slots=True)
class Rejection:
    reason: RejectReason
    source: SourceInfo
    rules: VideoRules


def check_source(source: SourceInfo, rules: VideoRules) -> Rejection | None:
    reason = _first_violation(source, rules)
    return Rejection(reason, source, rules) if reason is not None else None


def is_duration_allowed(duration_s: float, rules: VideoRules) -> bool:
    """Pre-download check on Telegram's integer duration (rounded, so allow +1 s)."""
    return duration_s <= rules.max_duration_s + 1


def _first_violation(source: SourceInfo, rules: VideoRules) -> RejectReason | None:
    long_side = max(source.width, source.height)
    short_side = min(source.width, source.height)
    if _MOV_FAMILY not in source.container.split(","):
        return RejectReason.BAD_FORMAT
    if source.duration < rules.min_duration_s:
        return RejectReason.TOO_SHORT
    if source.duration > rules.max_duration_s:
        return RejectReason.TOO_LONG
    if long_side > rules.max_long_side or short_side > rules.max_short_side:
        return RejectReason.TOO_LARGE_FRAME
    if source.fps > rules.max_fps + _FPS_TOLERANCE:
        return RejectReason.TOO_HIGH_FPS
    if source.fps > rules.base_fps + _FPS_TOLERANCE and short_side > rules.high_fps_short_side:
        return RejectReason.HIGH_FPS_FRAME
    return None


class VideoRejectedError(AppError):
    def __init__(self, rejection: Rejection) -> None:
        super().__init__(f"Video rejected: {rejection.reason}")
        self.rejection = rejection
