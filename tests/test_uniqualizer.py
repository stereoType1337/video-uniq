from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.probe import parse_probe, probe_source
from app.services.render_plan import Mode, RenderPlan, SourceInfo, derive_seed, generate_plan
from app.services.uniqualizer import build_audio_filters, build_command, render_copy

_SOURCE = SourceInfo(width=1280, height=720, fps=30.0, duration=8.0, sample_rate=48000)
_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _plan(index: int = 0, source: SourceInfo = _SOURCE, mode: Mode = Mode.BASE) -> RenderPlan:
    return generate_plan(derive_seed("secret", 1, index), index, source, mode)


def test_command_strips_metadata_and_sei() -> None:
    cmd = build_command(_plan(), Path("in.mp4"), Path("out.mp4"))
    joined = " ".join(cmd)
    assert "-map_metadata -1" in joined
    assert "filter_units=remove_types=6" in joined
    assert cmd[-1] == "out.mp4"


def test_command_without_audio_disables_audio() -> None:
    silent = SourceInfo(width=640, height=360, fps=25.0, duration=3.0, sample_rate=None)
    cmd = build_command(_plan(source=silent), Path("in.mp4"), Path("out.mp4"))
    assert "-an" in cmd and "-af" not in cmd


def test_audio_tempo_compensates_pitch() -> None:
    audio = _plan().audio
    assert audio is not None
    filters = build_audio_filters(audio, speed=1.03)
    rate = int(filters.split("asetrate=")[1].split(",")[0])
    tempo = float(filters.split("atempo=")[1].split(",")[0])
    # resampling changes speed by rate/sr; atempo must bring the total to the planned speed
    assert rate / audio.sample_rate * tempo == pytest.approx(1.03, rel=1e-5)


def test_parse_probe_reads_streams() -> None:
    info = parse_probe(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1080,
                    "height": 1920,
                    "avg_frame_rate": "30000/1001",
                },
                {"codec_type": "audio", "sample_rate": "44100"},
            ],
            "format": {"duration": "12.5", "format_name": "mov,mp4"},
        }
    )
    assert (info.width, info.height, info.sample_rate) == (1080, 1920, 44100)
    assert info.container == "mov,mp4"
    assert info.fps == pytest.approx(29.97, abs=0.01)


@pytest.mark.parametrize(
    "rotation_fields",
    [{"side_data_list": [{"rotation": -90}]}, {"tags": {"rotate": "270"}}],
)
def test_parse_probe_swaps_dimensions_for_rotated_video(rotation_fields: dict[str, object]) -> None:
    video = {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"}
    info = parse_probe({"streams": [video | rotation_fields], "format": {"duration": "5"}})
    assert (info.width, info.height) == (1080, 1920)


def test_parse_probe_rejects_audio_only() -> None:
    with pytest.raises(ValueError):
        parse_probe({"streams": [{"codec_type": "audio"}], "format": {"duration": "1"}})


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("media") / "src.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg is not installed")
@pytest.mark.parametrize("mode", list(Mode))
async def test_render_produces_planned_output(
    sample_video: Path, tmp_path: Path, mode: Mode
) -> None:
    source = await probe_source(sample_video)
    for index in range(3):
        plan = _plan(index, source, mode)
        dst = tmp_path / f"copy_{mode}_{index}.mp4"
        await render_copy(plan, sample_video, dst)
        out = await probe_source(dst)
        assert (out.width, out.height) == (plan.geometry.out_w, plan.geometry.out_h)
        assert out.sample_rate == 48000
        expected = (source.duration - plan.time.start_offset) / plan.time.speed
        assert out.duration == pytest.approx(expected, abs=0.25)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg is not installed")
async def test_rotated_phone_video_stays_portrait(sample_video: Path, tmp_path: Path) -> None:
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-display_rotation", "90",
         "-i", str(sample_video), "-c", "copy", str(rotated)],
        check=True,
    )  # fmt: skip
    source = await probe_source(rotated)
    assert source.height > source.width
    plan = _plan(0, source)
    dst = tmp_path / "out.mp4"
    await render_copy(plan, rotated, dst)
    out = await probe_source(dst)
    assert (out.width, out.height) == (plan.geometry.out_w, plan.geometry.out_h)
    assert out.height > out.width
