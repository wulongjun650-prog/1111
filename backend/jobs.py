from __future__ import annotations

import asyncio
import json
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

from backend import composer, music, planner, renderer, tts
from backend.config import ASPECTS, JOBS_DIR, OUTPUT_DIR


Progress = Callable[[int, str, dict[str, Any]], None]


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    def create(self, storyboard: dict[str, Any]) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        record = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "任务已入队",
            "storyboard": storyboard,
            "stills": [],
            "video": None,
            "error": None,
            "created_at": time.time(),
        }
        self._jobs[job_id] = record
        return record

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        rows = []
        for job in sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True):
            rows.append({k: job[k] for k in ("id", "status", "progress", "message", "video", "created_at")})
            rows[-1]["title"] = job["storyboard"].get("title")
        return rows[:24]

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        self._jobs[job_id].update(fields)
        return self._jobs[job_id]


store = JobStore()


def _emit(job_id: str, progress: int, message: str, extra: dict[str, Any] | None = None) -> None:
    payload = {"progress": progress, "message": message}
    if extra:
        payload.update(extra)
    store.update(job_id, progress=progress, message=message, **{k: v for k, v in (extra or {}).items() if k in {"stills", "video", "status"}})
    path = JOBS_DIR / job_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), **payload}, ensure_ascii=False) + "\n")


async def render_job(job_id: str) -> None:
    job = store.get(job_id)
    if not job:
        return
    board = job["storyboard"]
    work = JOBS_DIR / job_id
    work.mkdir(parents=True, exist_ok=True)
    store.update(job_id, status="running")
    try:
        await asyncio.to_thread(_render_sync, job_id, board, work)
        store.update(job_id, status="done", progress=100, message="成片完成")
        _emit(job_id, 100, "成片完成", {"status": "done", "video": store.get(job_id).get("video")})
    except Exception as exc:
        store.update(job_id, status="error", error=str(exc), message=f"失败：{exc}")
        _emit(job_id, store.get(job_id)["progress"], f"失败：{exc}", {"status": "error"})
        (work / "error.log").write_text(traceback.format_exc(), encoding="utf-8")


def _render_sync(job_id: str, board: dict[str, Any], work: Path) -> None:
    width, height = ASPECTS.get(board["aspect"], ASPECTS["16:9"])
    scenes = board["scenes"]
    stills: list[str] = []
    n = max(1, len(scenes))

    _emit(job_id, 4, "正在绘制分镜画面…")
    for i, scene in enumerate(scenes):
        still = work / f"still_{i:02d}.png"
        renderer.render_still(
            width=width,
            height=height,
            style=board["style"],
            motif=scene["motif"],
            title=board["title"],
            beat=scene["beat"],
            index=i,
            seed=int(board["seed"]),
            path=still,
        )
        stills.append(f"/api/jobs/{job_id}/still/{i}")
        pct = 8 + int(32 * (i + 1) / n)
        _emit(job_id, pct, f"画面 {i + 1}/{n} · {scene['beat']}", {"stills": stills})
        store.update(job_id, stills=stills)

    _emit(job_id, 42, "正在录制旁白…")
    narrations: list[Path] = []
    for i, scene in enumerate(scenes):
        raw = work / f"vo_raw_{i:02d}.mp3"
        fitted = work / f"vo_{i:02d}.m4a"
        asyncio.run(tts.synthesize(scene["narration"], board["voice"], raw))
        composer.fit_voiceover(raw, fitted, scene["duration"])
        narrations.append(fitted)
        _emit(job_id, 42 + int(18 * (i + 1) / n), f"旁白 {i + 1}/{n}")

    (work / "storyboard.json").write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")

    _emit(job_id, 62, "正在生成配乐…")
    total = sum(s["duration"] for s in scenes)
    bgm = music.write_score(work / "bgm.m4a", total + 1.5, board["style"], int(board["seed"]))

    _emit(job_id, 68, "正在做运镜与转场…")
    clips: list[Path] = []
    for i, scene in enumerate(scenes):
        clip = work / f"clip_{i:02d}.mp4"
        composer.ken_burns(
            work / f"still_{i:02d}.png",
            clip,
            width,
            height,
            scene["duration"],
            scene["motion"],
        )
        clips.append(clip)
        _emit(job_id, 68 + int(16 * (i + 1) / n), f"运镜 {i + 1}/{n}")

    silent = work / "silent.mp4"
    composer.concat_videos(clips, silent)
    _emit(job_id, 86, "正在混合声轨…")
    audio = composer.mix_soundtrack(narrations, [s["duration"] for s in scenes], bgm, work / "mix.m4a")
    ass = composer.write_ass(scenes, width, height, work / "subs.ass")
    _emit(job_id, 92, "正在压字幕与成片…")
    outfile = OUTPUT_DIR / f"{job_id}.mp4"
    composer.mux(silent, audio, ass, outfile)
    rel = f"/api/jobs/{job_id}/video"
    store.update(job_id, video=rel, stills=stills)
    _emit(job_id, 98, "封装完成", {"video": rel, "stills": stills})


def plan_storyboard(payload: dict[str, Any]) -> dict[str, Any]:
    return planner.plan(
        prompt=payload["prompt"],
        style=payload.get("style") or "cinematic",
        duration=int(payload.get("duration") or 30),
        aspect=payload.get("aspect") or "16:9",
        voice=payload.get("voice") or "xiaoxiao",
        language=payload.get("language") or "auto",
    )
