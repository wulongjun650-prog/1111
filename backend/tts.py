from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from backend.config import VOICES


def probe_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    raw = subprocess.check_output(cmd, text=True)
    data = json.loads(raw)
    return float(data["format"]["duration"])


async def synthesize(text: str, voice_key: str, dest: Path, rate: str = "+0%") -> float:
    dest.parent.mkdir(parents=True, exist_ok=True)
    voice = VOICES.get(voice_key, VOICES["xiaoxiao"])[0]
    try:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(str(dest))
        if dest.exists() and dest.stat().st_size > 500:
            return probe_duration(dest)
    except Exception:
        pass
    return await asyncio.to_thread(_silent, dest, max(2.2, min(8.0, len(text) * 0.18)))


def _silent(dest: Path, seconds: float) -> float:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=44100:cl=stereo",
            "-t",
            f"{seconds:.2f}",
            "-q:a",
            "9",
            str(dest.with_suffix(".mp3")),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    out = dest.with_suffix(".mp3")
    if dest.suffix != ".mp3":
        out.replace(dest)
    return seconds
