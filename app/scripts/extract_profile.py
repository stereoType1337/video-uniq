"""Draft a metadata profile from REAL exports of an app or device.

    python -m app.scripts.extract_profile --id capcut_ios --kind editor IMG_1.MOV IMG_2.MOV

Prints a JSON entry for app/services/data/metadata_profiles.json: container, brand, track
handlers, encoder string, vendor, format tags and a file-name pattern guessed from the names
(digits -> {n4} / date patterns). Review it by hand: values that differ between the samples
(model, OS) become lists; anything identifying a real person (GPS, serials) is dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

_DROP_TAGS = re.compile(r"gps|location|serial|owner|uuid|identifier", re.IGNORECASE)
_DATE_NAME = re.compile(r"(\d{8})_(\d{6})")


def exif(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["exiftool", "-j", "-G1", "-a", "-api", "LargeFileSupport=1", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data: dict[str, Any] = json.loads(out)[0]
    return data


def stream_tags(path: Path) -> dict[str, dict[str, str]]:
    """codec_type -> tags of the first stream of that type (handler_name, vendor_id, ...)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result: dict[str, dict[str, str]] = {}
    for stream in json.loads(out).get("streams", []):
        result.setdefault(stream.get("codec_type", ""), stream.get("tags", {}))
    return result


def name_pattern(name: str) -> str:
    pattern = _DATE_NAME.sub("{date:%Y%m%d_%H%M%S}", name)
    return re.sub(r"(?<![%{])\d{4}(?=\.)", "{n4}", pattern)


def draft_profile(profile_id: str, kind: str, paths: list[Path]) -> dict[str, Any]:
    samples = [exif(p) for p in paths]
    first = samples[0]
    tags: dict[str, set[str]] = {}
    for s in samples:
        for key, value in s.items():
            if key.startswith(("Keys:", "ItemList:", "UserData:")) and not _DROP_TAGS.search(key):
                tags.setdefault(key.split(":", 1)[1], set()).add(str(value))
    major = str(first.get("QuickTime:MajorBrand", ""))
    streams = stream_tags(paths[0])
    video, audio = streams.get("video", {}), streams.get("audio", {})
    vendor = video.get("vendor_id", "")
    return {
        "id": profile_id,
        "kind": kind,
        "weight": 1,
        "container": "mov" if "QuickTime" in major else "mp4",
        "brand": "qt" if "QuickTime" in major else major.split()[0].lower(),
        "filename": name_pattern(paths[0].name),
        "video_handler": video.get("handler_name", ""),
        "audio_handler": audio.get("handler_name", ""),
        "encoder": video.get("encoder") or first.get("Track1:CompressorName", ""),
        "vendor": vendor if vendor and "[0]" not in vendor else None,
        "_observed": {
            "minor_version": first.get("QuickTime:MinorVersion"),
            "compatible_brands": first.get("QuickTime:CompatibleBrands"),
            "format_tags": {k: sorted(v) for k, v in tags.items()},
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--id", required=True)
    parser.add_argument("--kind", choices=("editor", "camera"), required=True)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps(draft_profile(args.id, args.kind, args.files), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
