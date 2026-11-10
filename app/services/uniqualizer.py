"""Turns a RenderPlan into a single-pass FFmpeg command and runs it."""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from app.core.exceptions import RenderError
from app.services.metadata import ffmpeg_args as metadata_args
from app.services.metadata import patch_container
from app.services.render_plan import AudioPlan, RenderPlan

logger = structlog.get_logger()

_SEI_NAL_TYPE = 6  # x264 writes its settings into SEI; strip them


def build_command(
    plan: RenderPlan, src: Path, dst: Path, ffmpeg_bin: str = "ffmpeg", threads: int = 0
) -> list[str]:
    """``threads`` > 0 pins x264 and filter threads (several FFmpeg processes share the CPU)."""
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{plan.time.start_offset:.3f}",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-vf",
        build_video_filters(plan),
    ]
    if plan.audio is None:
        cmd.append("-an")
    else:
        cmd += ["-map", "0:a:0", "-af", build_audio_filters(plan.audio, plan.time.speed)]
        # bitexact: FFmpeg's AAC encoder otherwise embeds "Lavc<version>" in the bitstream
        cmd += ["-c:a", "aac", "-b:a", f"{plan.audio.bitrate_k}k", "-flags:a", "+bitexact"]
    if threads > 0:
        cmd += ["-threads", str(threads), "-filter_threads", str(threads)]
    cmd += _codec_args(plan)
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact"]
    if plan.metadata is None:
        cmd += ["-movflags", "+faststart"]
    else:
        cmd += metadata_args(plan.metadata, has_audio=plan.audio is not None)
    cmd += ["-shortest", str(dst)]
    return cmd


def build_video_filters(plan: RenderPlan) -> str:
    g, c, t = plan.geometry, plan.color, plan.time
    filters = [
        f"scale={g.scale_w}:{g.scale_h}:flags=bicubic",
        f"crop={g.crop_w}:{g.crop_h}:{g.crop_x}:{g.crop_y}",
    ]
    if g.pad_w or g.pad_h:
        filters.append(f"pad={g.out_w}:{g.out_h}:{g.pad_x}:{g.pad_y}:black")
    sar_max = max(g.sar_num, g.sar_den)
    filters.append(f"setsar=r={g.sar_num}/{g.sar_den}:max={sar_max}")
    filters.append(
        f"eq=brightness={c.brightness}:contrast={c.contrast}"
        f":saturation={c.saturation}:gamma={c.gamma}"
    )
    filters.append(f"hue=h={c.hue}")
    if c.vignette is not None:
        filters.append(f"vignette=angle={c.vignette}")
    if c.noise is not None:
        filters.append(f"noise=alls={c.noise}:allf=t")
    filters += [f"setpts=PTS/{t.speed}", f"fps={t.fps}", "format=yuv420p"]
    return ",".join(filters)


def build_audio_filters(audio: AudioPlan, speed: float) -> str:
    shifted_rate = round(audio.sample_rate * audio.pitch)
    real_pitch = shifted_rate / audio.sample_rate
    filters = [
        f"asetrate={shifted_rate}",
        f"aresample={audio.sample_rate}",
        f"atempo={speed / real_pitch:.6f}",
        f"equalizer=f={audio.eq_freq}:t=q:w=1:g={audio.eq_gain}",
    ]
    if audio.delay_ms > 0:
        filters.append(f"adelay={audio.delay_ms}:all=1")
    return ",".join(filters)


def _codec_args(plan: RenderPlan) -> list[str]:
    codec = plan.codec
    return [
        "-c:v",
        "libx264",
        "-preset",
        codec.preset,
        "-crf",
        str(codec.crf),
        "-g",
        str(codec.gop),
        "-profile:v",
        codec.profile,
        "-pix_fmt",
        "yuv420p",
        "-bsf:v",
        f"filter_units=remove_types={_SEI_NAL_TYPE}",
    ]


async def render_copy(
    plan: RenderPlan, src: Path, dst: Path, ffmpeg_bin: str = "ffmpeg", threads: int = 0
) -> None:
    cmd = build_command(plan, src, dst, ffmpeg_bin, threads)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        tail = stderr.decode(errors="replace")[-2000:]
        logger.error("render_failed", copy_index=plan.copy_index, code=process.returncode)
        raise RenderError(plan.copy_index, tail)
    if plan.metadata is not None:
        await asyncio.to_thread(patch_container, dst, plan.metadata)
    logger.info("render_done", copy_index=plan.copy_index, seed=plan.seed[:12])
