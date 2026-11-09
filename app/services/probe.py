from __future__ import annotations

import asyncio
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.core.exceptions import ProbeError
from app.services.render_plan import SourceInfo


async def probe_source(path: Path, ffprobe_bin: str = "ffprobe") -> SourceInfo:
    process = await asyncio.create_subprocess_exec(
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise ProbeError(str(path), stderr.decode(errors="replace")[-500:])
    try:
        return parse_probe(json.loads(stdout))
    except (ValueError, KeyError) as exc:
        raise ProbeError(str(path), str(exc)) from exc


def parse_probe(data: dict[str, Any]) -> SourceInfo:
    streams: list[dict[str, Any]] = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ValueError("unknown duration")
    width, height = int(video["width"]), int(video["height"])
    if _rotation(video) % 180 == 90:
        # Phones store portrait video as a landscape frame + display rotation. FFmpeg applies
        # the rotation while decoding, so everything downstream must see display dimensions.
        width, height = height, width
    return SourceInfo(
        width=width,
        height=height,
        fps=_parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate", "0/1")),
        duration=duration,
        sample_rate=int(audio["sample_rate"]) if audio is not None else None,
        container=str(data.get("format", {}).get("format_name", "")),
    )


def _rotation(video: dict[str, Any]) -> int:
    for side_data in video.get("side_data_list", []):
        if "rotation" in side_data:
            return abs(round(float(side_data["rotation"])))
    return abs(int(video.get("tags", {}).get("rotate", 0)))  # older muxers


def _parse_rate(raw: str) -> float:
    num, _, den = raw.partition("/")
    rate = Fraction(int(num), int(den or "1")) if int(den or "1") != 0 else Fraction(0)
    if rate <= 0:
        raise ValueError(f"bad frame rate {raw!r}")
    return float(rate)
