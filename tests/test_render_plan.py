from __future__ import annotations

import pytest

from app.services.render_plan import (
    MODE_LIMITS,
    AudioMode,
    Mode,
    RenderPlan,
    SourceInfo,
    apply_audio_mode,
    derive_seed,
    generate_plan,
)

_LANDSCAPE = SourceInfo(width=1920, height=1080, fps=30.0, duration=8.0, sample_rate=48000)
_VERTICAL = SourceInfo(width=1080, height=1920, fps=29.97, duration=15.0, sample_rate=44100)
_SILENT = SourceInfo(width=1280, height=720, fps=24.0, duration=5.0, sample_rate=None)


def _plans(source: SourceInfo, mode: Mode, count: int = 200) -> list[RenderPlan]:
    return [generate_plan(derive_seed("secret", 1, i), i, source, mode) for i in range(count)]


def test_seed_is_deterministic_and_unique() -> None:
    assert derive_seed("s", 7, 3) == derive_seed("s", 7, 3)
    seeds = {derive_seed("s", job, idx) for job in range(20) for idx in range(50)}
    assert len(seeds) == 1000
    assert derive_seed("s", 7, 3) != derive_seed("other", 7, 3)


def test_same_seed_gives_same_plan() -> None:
    seed = derive_seed("secret", 42, 0)
    assert generate_plan(seed, 0, _LANDSCAPE, Mode.BASE) == generate_plan(
        seed, 0, _LANDSCAPE, Mode.BASE
    )


def test_plans_do_not_repeat() -> None:
    plans = _plans(_LANDSCAPE, Mode.BASE)
    signatures = {(p.geometry, p.time, p.color) for p in plans}
    assert len(signatures) == len(plans)


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("source", [_LANDSCAPE, _VERTICAL, _SILENT])
def test_geometry_is_valid(source: SourceInfo, mode: Mode) -> None:
    limit = MODE_LIMITS[mode].max_short_side
    for plan in _plans(source, mode):
        g = plan.geometry
        assert g.out_w % 2 == 0 and g.out_h % 2 == 0, "yuv420p needs even dimensions"
        assert min(g.out_w, g.out_h) <= limit
        assert 0 <= g.crop_x <= g.scale_w - g.crop_w
        assert 0 <= g.crop_y <= g.scale_h - g.crop_h
        assert 0 <= g.pad_x <= g.pad_w <= 24 and 0 <= g.pad_y <= g.pad_h <= 24
        assert abs(g.sar_num / g.sar_den - 1) <= 0.0005


def test_time_and_color_ranges_base() -> None:
    for plan in _plans(_LANDSCAPE, Mode.BASE):
        assert 0.01 <= abs(plan.time.speed - 1) <= 0.06 + 1e-9
        assert 24 <= plan.time.fps <= 30.15
        assert 0 <= plan.time.start_offset <= 0.25
        assert abs(plan.color.hue) <= 4
        assert plan.color.vignette is None and plan.color.noise is None
        assert 20 <= plan.codec.crf <= 26


def test_visible_effects_only_in_hd_plus() -> None:
    assert all(p.color.vignette is not None for p in _plans(_LANDSCAPE, Mode.HD_PLUS, 20))
    assert all(p.color.noise is None for p in _plans(_LANDSCAPE, Mode.HD, 20))


def test_silent_source_has_no_audio_plan() -> None:
    assert all(p.audio is None for p in _plans(_SILENT, Mode.BASE, 20))
    assert all(p.audio is not None for p in _plans(_LANDSCAPE, Mode.BASE, 20))


def test_plan_serialises_to_json_dict() -> None:
    data = _plans(_LANDSCAPE, Mode.BASE, 1)[0].to_json()
    assert data["mode"] == "base"
    assert set(data["geometry"]) >= {"scale_w", "crop_w", "sar_num"}


def test_audio_modes_only_strip_sound_changes() -> None:
    plan = _plans(_LANDSCAPE, Mode.BASE, 1)[0]
    assert plan.audio is not None
    assert apply_audio_mode(plan, AudioMode.SHIFT) == plan
    assert apply_audio_mode(plan, AudioMode.MUTE).audio is None
    kept = apply_audio_mode(plan, AudioMode.KEEP)
    assert kept.audio is not None
    assert (kept.audio.pitch, kept.audio.eq_gain, kept.audio.delay_ms) == (1.0, 0.0, 0)
    assert kept.time == plan.time and kept.geometry == plan.geometry, "video untouched"
