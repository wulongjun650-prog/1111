from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from backend.config import STYLES


def write_score(path: Path, duration: float, style: str, seed: int) -> Path:
    """Generate a soft harmonic pad — no external samples required."""
    rng = np.random.default_rng(seed)
    sr = 44100
    n = int(sr * duration)
    t = np.arange(n) / sr
    mood = STYLES.get(style, STYLES["cinematic"])["mood"]
    roots = {
        "epic": [110, 165, 220, 330],
        "ink": [98, 147, 196, 294],
        "neon": [82, 123, 247, 370],
        "doc": [87, 130, 174, 261],
        "studio": [130, 196, 261, 392],
        "soft": [146, 220, 293, 440],
        "hud": [73, 146, 219, 292],
        "tale": [123, 185, 247, 370],
    }[mood]
    wav = np.zeros(n, dtype=np.float32)
    for i, f in enumerate(roots):
        amp = 0.11 / (i + 1)
        lfo = 0.5 + 0.5 * np.sin(2 * np.pi * (0.03 + i * 0.01) * t)
        wav += amp * lfo * np.sin(2 * np.pi * f * t + rng.random() * 6)
    noise = rng.normal(0, 0.02, n).astype(np.float32)
    wav += noise
    fade = int(sr * min(2.5, duration / 6))
    env = np.ones(n, dtype=np.float32)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    wav = np.clip(wav * env * 0.35, -1, 1)
    stereo = np.stack([wav, np.roll(wav, 220)], axis=1)
    pcm = (stereo * 32767).astype(np.int16)
    tmp = path.with_suffix(".raw")
    pcm.tofile(tmp)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(sr),
            "-ac",
            "2",
            "-i",
            str(tmp),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tmp.unlink(missing_ok=True)
    return path
