"""本机 GPU 真人开口：SadTalker 驱动同一主持人说话、转头，再按站立→坐车剪辑。

云端没有 NVIDIA 显卡，必须在用户本机运行。不要用 Ken Burns 幻灯片冒充开口。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

from backend import composer, music, overlays
from backend.cantonese_cut import _ass, _trim_voice
from backend.config import ASPECTS, FPS, JOBS_DIR, ROOT
from backend.host_cut import ASSETS, PHRASES, SHOTS, TAILS
from backend import tts

SAMPLES = ROOT / "samples"
VENDOR = ROOT / "vendor"
SAD_ROOT = Path(os.environ.get("SADTALKER_ROOT") or (VENDOR / "SadTalker"))
SAD_VENV = VENDOR / "sadtalker-venv"

# 站在豪车中间讲前两段口播，再坐进车里介绍配置。每段都是 SadTalker 开口，不是推拉静帧。
TAKES = [
    {"id": "stand", "title": "站在豪车中间开口", "image": "host-01-center-wide.png", "start": 0, "end": 9},
    {"id": "sit", "title": "坐进驾驶位", "image": "host-05-sit-driver.png", "start": 9, "end": 11},
    {"id": "leather", "title": "皮笼", "image": "host-06-sit-leather.png", "start": 11, "end": 12},
    {"id": "sunroof", "title": "天窗", "image": "host-07-sit-sunroof.png", "start": 12, "end": 13},
    {"id": "cam360", "title": "360镜头", "image": "host-08-sit-360.png", "start": 13, "end": 14},
    {"id": "cta", "title": "WhatsApp收束", "image": "host-09-sit-cta.png", "start": 14, "end": 16},
]

CHECKPOINT_FILES = [
    "mapping_00109-model.pth.tar",
    "mapping_00229-model.pth.tar",
    "SadTalker_V0.0.2_256.safetensors",
]
GFPGAN_FILES = [
    "alignment_WFLW_4HG.pth",
    "detection_Resnet50_Final.pth",
    "parsing_parsenet.pth",
]

Progress = Callable[..., None]


class TalkingNeedGPU(RuntimeError):
    def __init__(self, info: dict):
        super().__init__(info.get("message") or "本机无法真人开口")
        self.info = info


def _win() -> bool:
    return os.name == "nt"


def setup_commands() -> list[str]:
    if _win():
        return [
            r"powershell -ExecutionPolicy Bypass -File scripts\setup_talking_host.ps1",
            r"python -m backend.talking_host",
        ]
    return [
        "bash scripts/setup_talking_host.sh",
        "PYTHONPATH=. python3 -m backend.talking_host",
    ]


def _sadtalker_python() -> Path | None:
    env = os.environ.get("SADTALKER_PYTHON")
    if env:
        p = Path(env)
        if p.exists():
            return p
    for p in (
        SAD_VENV / "Scripts" / "python.exe",
        SAD_VENV / "bin" / "python",
        SAD_VENV / "bin" / "python3",
    ):
        if p.exists():
            return p
    return None


def _gpu_csv() -> str | None:
    try:
        return subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            text=True,
            timeout=8,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def _gpu_memory_mb() -> int:
    raw = _gpu_csv()
    if not raw:
        return 0
    try:
        mem = raw.splitlines()[0].split(",")[-1].strip().lower().replace("mib", "").strip()
        return int(float(mem.split()[0]))
    except Exception:
        return 0


def _cuda_in_venv(py: Path) -> tuple[bool, str]:
    code = (
        "import torch; "
        "ok=bool(torch.cuda.is_available()); "
        "name=torch.cuda.get_device_name(0) if ok else ''; "
        "print(('1' if ok else '0')+'|'+name)"
    )
    try:
        out = subprocess.check_output([str(py), "-c", code], text=True, timeout=30, stderr=subprocess.STDOUT)
        flag, _, name = out.strip().partition("|")
        return flag.strip() == "1", name.strip()
    except Exception as exc:
        return False, str(exc)[-200:]


def _checkpoints_ok() -> bool:
    ckpt = SAD_ROOT / "checkpoints"
    if not all((ckpt / name).exists() for name in CHECKPOINT_FILES):
        return False
    weights = SAD_ROOT / "gfpgan" / "weights"
    return all((weights / name).exists() for name in GFPGAN_FILES)


def diagnose() -> dict:
    gpu = _gpu_csv()
    py = _sadtalker_python()
    sad = (SAD_ROOT / "inference.py").exists()
    ckpt = _checkpoints_ok()
    cuda_ok, cuda_name = _cuda_in_venv(py) if py else (False, "")
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    missing: list[str] = []
    if not ffmpeg_ok:
        missing.append("FFmpeg 不在 PATH（Windows 可 winget install Gyan.FFmpeg）")
    if not gpu:
        missing.append("没有检测到 NVIDIA 显卡（nvidia-smi 失败）")
    if not sad:
        missing.append("还没安装 SadTalker（缺少 vendor/SadTalker/inference.py）")
    if not py:
        missing.append("还没创建 SadTalker 虚拟环境 vendor/sadtalker-venv")
    elif not cuda_ok:
        missing.append("SadTalker 环境里的 PyTorch 用不了 CUDA，需要重装 GPU 版 torch")
    if sad and not ckpt:
        missing.append("SadTalker 模型权重还没下完")

    ok = not missing
    if ok:
        message = f"本机可以真人开口：{gpu or cuda_name}。点按钮或运行 python -m backend.talking_host"
    else:
        message = "本机还不能真人开口。云端没有显卡，请在你自己的 NVIDIA 电脑上先跑安装脚本。\n" + "；".join(missing)

    return {
        "ok": ok,
        "gpu": gpu,
        "cuda": cuda_ok,
        "cuda_name": cuda_name,
        "sadtalker": str(SAD_ROOT) if sad else None,
        "python": str(py) if py else None,
        "checkpoints": ckpt,
        "ffmpeg": ffmpeg_ok,
        "missing": missing,
        "message": message,
        "setup": setup_commands(),
    }


def _note(progress: Progress | None, pct: int, msg: str, extra: dict | None = None) -> None:
    print(f"[{pct:3d}%] {msg}", flush=True)
    if not progress:
        return
    try:
        progress(pct, msg, extra or {})
    except TypeError:
        progress(pct, msg)


def _active_takes() -> list[dict]:
    if os.environ.get("TALKING_HOST_QUICK") == "1":
        return [
            {"id": "stand", "title": "站在豪车中间开口", "image": "host-01-center-wide.png", "start": 0, "end": 9},
            {"id": "sit", "title": "坐进车里介绍", "image": "host-05-sit-driver.png", "start": 9, "end": 16},
        ]
    return TAKES


def _pick_size() -> int:
    env = os.environ.get("SADTALKER_SIZE")
    if env in {"256", "512"}:
        return int(env)
    return 512 if _gpu_memory_mb() >= 10000 else 256


def _concat_audio(beats: list[dict], dest: Path) -> Path:
    padded = []
    work = dest.parent
    for i, beat in enumerate(beats):
        out = work / f"{dest.stem}_pad_{i:02d}.m4a"
        composer._run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(beat["path"]),
                "-af",
                f"apad=whole_dur={beat['hold']:.2f},aformat=sample_rates=16000:channel_layouts=mono",
                "-t",
                f"{beat['hold']:.2f}",
                "-c:a",
                "aac",
                str(out),
            ]
        )
        padded.append(out)
    lst = dest.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in padded), encoding="utf-8")
    merged = dest.with_suffix(".m4a")
    composer._run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(merged)])
    composer._run(
        ["ffmpeg", "-y", "-i", str(merged), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    )
    return dest


def _latest_mp4(folder: Path) -> Path:
    mp4s = [p for p in folder.rglob("*.mp4") if p.is_file() and p.stat().st_size > 1000]
    if not mp4s:
        raise RuntimeError(f"SadTalker 没有输出 mp4：{folder}")
    full = [p for p in mp4s if "full" in p.name.lower()]
    pool = full or mp4s
    return max(pool, key=lambda p: p.stat().st_mtime)


def _has_audio(path: Path) -> bool:
    try:
        raw = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            text=True,
        )
        return bool(raw.strip())
    except Exception:
        return False


def _fit_talking(src: Path, audio: Path, dest: Path, w: int, h: int, seconds: float) -> Path:
    dur = max(0.4, seconds)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-i",
        str(audio),
        "-filter_complex",
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},tpad=stop_mode=clone:stop=-1,fps={FPS},format=yuv420p[v]",
        "-map",
        "[v]",
        "-map",
        "1:a",
        "-t",
        f"{dur:.2f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-ar",
        "44100",
        "-ac",
        "2",
        str(dest),
    ]
    composer._run(cmd)
    return dest


def _overlay_keep_audio(clip: Path, items: list[tuple[Path, float, float]], dest: Path) -> Path:
    if not items:
        shutil.copyfile(clip, dest)
        return dest
    cmd = ["ffmpeg", "-y", "-i", str(clip)]
    for png, _, _ in items:
        cmd += ["-i", str(png)]
    filters = []
    last = "0:v"
    for i, (_, start, end) in enumerate(items, start=1):
        out = "vout" if i == len(items) else f"v{i}"
        filters.append(
            f"[{last}][{i}:v]overlay=0:0:enable='between(t,{start:.2f},{end:.2f})'[{out}]"
        )
        last = out
    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        f"[{last}]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-ar",
        "44100",
        "-ac",
        "2",
        str(dest),
    ]
    composer._run(cmd)
    return dest


def _concat_reencode(clips: list[Path], dest: Path) -> Path:
    lst = dest.with_suffix(".txt")
    lst.write_text("".join(f"file '{c.resolve().as_posix()}'\n" for c in clips), encoding="utf-8")
    composer._run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(lst),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(dest),
        ]
    )
    return dest


def _run_sadtalker(image: Path, wav: Path, result_dir: Path, size: int, log: Path) -> Path:
    py = _sadtalker_python()
    if not py:
        raise TalkingNeedGPU(diagnose())
    result_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(py),
        "-u",
        "inference.py",
        "--driven_audio",
        str(wav.resolve()),
        "--source_image",
        str(image.resolve()),
        "--result_dir",
        str(result_dir.resolve()),
        "--checkpoint_dir",
        str((SAD_ROOT / "checkpoints").resolve()),
        "--preprocess",
        "full",
        "--size",
        str(size),
        "--expression_scale",
        "1.28",
        "--pose_style",
        "2",
        "--batch_size",
        "1",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SAD_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    if _win():
        env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8", errors="replace") as fh:
        fh.write(" ".join(cmd) + "\n\n")
        fh.flush()
        proc = subprocess.run(
            cmd,
            cwd=str(SAD_ROOT),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=3600,
        )
    if proc.returncode != 0:
        tail = log.read_text(encoding="utf-8", errors="replace")[-2500:]
        if size > 256 and ("out of memory" in tail.lower() or "CUDA out of memory" in tail):
            shutil.rmtree(result_dir, ignore_errors=True)
            return _run_sadtalker(image, wav, result_dir, 256, log)
        raise RuntimeError(f"SadTalker 失败（exit {proc.returncode}）：\n{tail}")
    return _latest_mp4(result_dir)


async def render(progress: Progress | None = None, work: Path | None = None, voice: str = "wanlung", rate: str = "+14%") -> Path:
    info = diagnose()
    if not info["ok"]:
        raise TalkingNeedGPU(info)

    w, h = ASPECTS["16:9"]
    work = work or (JOBS_DIR / "talking-host")
    work.mkdir(parents=True, exist_ok=True)
    size = _pick_size()
    _note(progress, 3, f"锁定人设，SadTalker size={size}")

    stills = []
    for i, name in enumerate(["host-01-center-wide.png", "host-05-sit-driver.png"]):
        dest = work / f"still_{i:02d}.png"
        shutil.copyfile(ASSETS / name, dest)
        stills.append(f"/api/jobs/{work.name}/still/{i}")
    _note(progress, 6, "人设已锁定：同一男人站车中间再坐进驾驶位", {"stills": stills})

    ovs = {
        "badge": overlays.hook_badge(w, h, work / "ov_badge.png"),
        "r1": overlays.reason(w, h, "① 年尾清库存", work / "ov_r1.png"),
        "r2": overlays.reason(w, h, "② 跳过中间商", work / "ov_r2.png"),
        "r3": overlays.reason(w, h, "③ 新款让路", work / "ov_r3.png"),
        "now": overlays.price_now(w, h, work / "ov_now.png"),
        "leather": overlays.feature_one(w, h, "皮笼", work / "ov_leather.png"),
        "sunroof": overlays.feature_one(w, h, "天窗", work / "ov_sun.png"),
        "cam": overlays.feature_one(w, h, "360镜头", work / "ov_360.png"),
        "cta": overlays.cta(w, h, work / "ov_cta.png"),
    }

    beats = []
    captions = []
    t = 0.0
    _note(progress, 8, "正在录粤语口播…")
    for i, phrase in enumerate(PHRASES):
        raw = work / f"vo_{i:02d}.mp3"
        await tts.synthesize(phrase["text"], voice, raw, rate=rate)
        trimmed = work / f"vo_{i:02d}.m4a"
        dur = max(0.45, _trim_voice(raw, trimmed))
        hold = dur + 0.13 + TAILS.get(phrase["text"], 0.0)
        beats.append({**phrase, "path": trimmed, "dur": dur, "hold": hold, "t": t})
        captions.append((t, t + dur, phrase["text"].strip("。，")))
        t += hold
    total = t

    takes = _active_takes()
    clips: list[Path] = []
    for n, take in enumerate(takes):
        chunk = beats[take["start"] : take["end"]]
        if not chunk:
            continue
        hold = sum(b["hold"] for b in chunk)
        wav = work / f"take_{take['id']}.wav"
        _concat_audio(chunk, wav)
        pct = 18 + int(62 * n / max(1, len(takes)))
        _note(progress, pct, f"本机 GPU 驱动开口 {n + 1}/{len(takes)} · {take['title']}")
        sad_dir = work / f"sad_{take['id']}"
        if sad_dir.exists():
            shutil.rmtree(sad_dir, ignore_errors=True)
        image = ASSETS / take["image"]
        if not image.exists() and take["id"] in SHOTS:
            image = ASSETS / SHOTS[take["id"]]
        raw_mp4 = _run_sadtalker(image, wav, sad_dir, size, work / f"sad_{take['id']}.log")
        fitted = work / f"take_{take['id']}_fit.mp4"
        _fit_talking(raw_mp4, wav, fitted, w, h, hold)
        items = []
        rel = 0.0
        for beat in chunk:
            if beat.get("ov"):
                items.append((ovs[beat["ov"]], rel, rel + beat["hold"]))
            rel += beat["hold"]
        out = work / f"take_{take['id']}.mp4"
        _overlay_keep_audio(fitted, items, out)
        clips.append(out)

    _note(progress, 84, "拼接站立开口与坐车介绍…")
    silentish = work / "talking_concat.mp4"
    _concat_reencode(clips, silentish)

    bgm = music.write_score(work / "bgm.m4a", total + 0.4, "product", 41)
    mix = work / "mix.m4a"
    composer._run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(silentish),
            "-i",
            str(bgm),
            "-filter_complex",
            "[0:a]volume=1.22[vo];[1:a]volume=0.07[bg];[vo][bg]amix=inputs=2:duration=first:dropout_transition=1",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(mix),
        ]
    )
    video_only = work / "talking_video.mp4"
    composer._run(
        ["ffmpeg", "-y", "-i", str(silentish), "-an", "-c:v", "copy", str(video_only)]
    )
    ass = _ass(captions, w, h, work / "subs.ass")
    dest = SAMPLES / "host-talking.mp4"
    _note(progress, 92, "烧字幕、混背景音乐…")
    composer.mux(video_only, mix, ass, dest)
    zpath = SAMPLES / "host-talking.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(dest, dest.name)
    meta = {
        "duration": round(total, 2),
        "beats": len(beats),
        "takes": [t["id"] for t in takes],
        "size": size,
        "gpu": info.get("gpu"),
    }
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _note(progress, 100, f"成片 {dest} · {meta['duration']}s")
    print(dest, meta["duration"])
    return dest


def main() -> None:
    if "--status" in sys.argv:
        print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
        return
    info = diagnose()
    if not info["ok"]:
        print(info["message"])
        print("安装：")
        for line in info["setup"]:
            print("  ", line)
        raise SystemExit(2)
    asyncio.run(render())


if __name__ == "__main__":
    main()
