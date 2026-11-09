from __future__ import annotations

import pytest

from app.core import texts
from app.services.render_plan import SourceInfo
from app.services.video_rules import (
    RejectReason,
    VideoRules,
    check_source,
    is_duration_allowed,
)

_RULES = VideoRules()
_MP4 = "mov,mp4,m4a,3gp,3g2,mj2"


def _src(
    w: int = 1080, h: int = 1920, fps: float = 30.0, dur: float = 15.0, fmt: str = _MP4
) -> SourceInfo:
    return SourceInfo(width=w, height=h, fps=fps, duration=dur, sample_rate=48000, container=fmt)


@pytest.mark.parametrize(
    "source",
    [
        _src(),
        _src(w=2160, h=3840, fps=29.97),  # vertical 4K30
        _src(w=1920, h=1080, fps=59.94),  # 1080p60
        _src(fps=60.5),  # VFR phone clip
        _src(dur=0.5),
        _src(dur=180.0),
    ],
)
def test_accepts(source: SourceInfo) -> None:
    assert check_source(source, _RULES) is None


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (_src(fmt="matroska,webm"), RejectReason.BAD_FORMAT),
        (_src(fmt="avi"), RejectReason.BAD_FORMAT),
        (_src(dur=0.4), RejectReason.TOO_SHORT),
        (_src(dur=180.5), RejectReason.TOO_LONG),
        (_src(w=4096, h=2160), RejectReason.TOO_LARGE_FRAME),
        (_src(w=2880, h=2880), RejectReason.TOO_LARGE_FRAME),
        (_src(fps=120.0), RejectReason.TOO_HIGH_FPS),
        (_src(w=2160, h=3840, fps=60.0), RejectReason.HIGH_FPS_FRAME),
        (_src(w=1440, h=2560, fps=50.0), RejectReason.HIGH_FPS_FRAME),
    ],
)
def test_rejects(source: SourceInfo, reason: RejectReason) -> None:
    rejection = check_source(source, _RULES)
    assert rejection is not None and rejection.reason is reason
    assert "Требования" in texts.rejected(rejection)


def test_telegram_duration_precheck_allows_rounding() -> None:
    assert is_duration_allowed(181, _RULES)
    assert not is_duration_allowed(182, _RULES)


def test_rejection_text_formats_long_duration() -> None:
    rejection = check_source(_src(dur=754.0), _RULES)
    assert rejection is not None
    assert "12 мин 34 с" in texts.rejected(rejection)
