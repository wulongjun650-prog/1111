from __future__ import annotations

import subprocess
from pathlib import Path

from backend.config import FONT_PATH, FPS
from backend.tts import probe_duration


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2500:]
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {tail}")


def ken_burns(
    still: Path,
    dest: Path,
    width: int,
    height: int,
    duration: float,
    motion: str,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # still is 1.28x; pan/zoom inside the oversized plate.
    dur = max(0.8, duration)
    if motion == "zoom_in":
        scale = f"scale=trunc(iw*(1+0.16*t/{dur})/2)*2:trunc(ih*(1+0.16*t/{dur})/2)*2:eval=frame"
        crop = f"crop={width}:{height}:(in_w-{width})/2:(in_h-{height})/2"
    elif motion == "zoom_out":
        scale = f"scale=trunc(iw*(1.16-0.16*t/{dur})/2)*2:trunc(ih*(1.16-0.16*t/{dur})/2)*2:eval=frame"
        crop = f"crop={width}:{height}:(in_w-{width})/2:(in_h-{height})/2"
    elif motion == "pan_left":
        scale = f"scale=-2:{height}"
        crop = f"crop={width}:{height}:'max(0,in_w-{width})*(1-t/{dur})':(in_h-{height})/2"
    else:
        scale = f"scale=-2:{height}"
        crop = f"crop={width}:{height}:'max(0,in_w-{width})*t/{dur}':(in_h-{height})/2"

    fade_out_start = max(0.05, dur - 0.35)
    vf = (
        f"{scale},{crop},setsar=1,fps={FPS},format=yuv420p,"
        f"fade=t=in:st=0:d=0.35,fade=t=out:st={fade_out_start:.2f}:d=0.35"
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(still),
            "-t",
            f"{dur:.2f}",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-an",
            str(dest),
        ]
    )
    return dest


def write_ass(scenes: list[dict], width: int, height: int, dest: Path) -> Path:
    fontsize = 36 if height >= width else 42
    margin_v = 72 if height >= width else 64
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,WenQuanYi Micro Hei,{fontsize},&H00E8DCC8,&H000000FF,&H64101010,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,48,48,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    t = 0.0
    for scene in scenes:
        start = _ts(t + 0.15)
        end = _ts(t + scene["duration"] - 0.2)
        text = scene["narration"].replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")
        t += scene["duration"]
    dest.write_text("".join(lines), encoding="utf-8")
    return dest


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def concat_videos(clips: list[Path], dest: Path) -> Path:
    lst = dest.with_suffix(".txt")
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lst),
            "-c",
            "copy",
            str(dest),
        ]
    )
    return dest


def fit_voiceover(src: Path, dest: Path, target: float) -> Path:
    """Keep each line of VO inside the planned scene length (~30s totals stay ~30s)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = max(0.8, target)
    actual = probe_duration(src) if src.exists() and src.stat().st_size > 400 else 0.0
    filters = [f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"]
    if actual > target * 1.03:
        tempo = actual / target
        while tempo > 2.0:
            filters.append("atempo=2.0")
            tempo /= 2.0
        filters.append(f"atempo={max(0.5, tempo):.4f}")
    filters.append(f"apad=whole_dur={target:.2f}")
    fade_out = max(0.08, target - 0.22)
    filters.append("afade=t=in:d=0.12")
    filters.append(f"afade=t=out:st={fade_out:.2f}:d=0.2")
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-af",
            ",".join(filters),
            "-t",
            f"{target:.2f}",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(dest),
        ]
    )
    return dest


def mix_soundtrack(narrations: list[Path], scene_durs: list[float], bgm: Path, dest: Path) -> Path:
    """Pad each VO to its scene length, concat, then duck under a pad."""
    padded = []
    work = dest.parent
    for i, (vo, dur) in enumerate(zip(narrations, scene_durs)):
        out = work / f"vo_pad_{i:02d}.m4a"
        _run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(vo),
                "-af",
                f"apad=whole_dur={dur:.2f},afade=t=in:d=0.12,afade=t=out:st={max(0.1, dur - 0.25):.2f}:d=0.22",
                "-t",
                f"{dur:.2f}",
                "-c:a",
                "aac",
                str(out),
            ]
        )
        padded.append(out)
    vo_all = work / "vo_all.m4a"
    lst = work / "vo.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in padded), encoding="utf-8")
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(vo_all)])
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(vo_all),
            "-i",
            str(bgm),
            "-filter_complex",
            "[1:a]volume=0.16[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2,loudnorm=I=-16:LRA=11:TP=-1.5",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(dest),
        ]
    )
    return dest


def mux(video: Path, audio: Path, ass: Path, dest: Path) -> Path:
    fontsdir = str(FONT_PATH.parent)
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-vf",
            f"subtitles='{ass.as_posix()}':fontsdir='{fontsdir}'",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    return dest
