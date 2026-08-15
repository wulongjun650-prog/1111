from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import ASPECTS, JOBS_DIR, OUTPUT_DIR, ROOT, STYLES, VOICES
from backend.jobs import plan_storyboard, render_job, store

app = FastAPI(title="映界 Lumina", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class StoryboardIn(BaseModel):
    prompt: str = Field(min_length=2, max_length=2000)
    style: str = "cinematic"
    duration: int = Field(default=30, ge=8, le=90)
    aspect: str = "16:9"
    voice: str = "xiaoxiao"
    language: str = "auto"


class RenderIn(BaseModel):
    storyboard: dict


@app.get("/api/meta")
def meta():
    return {
        "styles": {k: {"label": v["label"], "hint": v["hint"]} for k, v in STYLES.items()},
        "aspects": list(ASPECTS),
        "voices": {k: v[1] for k, v in VOICES.items()},
        "samples": [
            "一座被云海托起的仙山，少年持灯走入晨雾，寻找失落的星图。",
            "2049 年的雨夜都市，霓虹在积水里碎成光带，一名信使穿过天桥。",
            "一瓶被棚灯吻过的香水，玻璃里藏着海岸与日落，奢侈而安静。",
            "A silent astronaut drifting above a copper-colored planet, remembering home.",
        ],
        "videos": [
            {
                "id": "car-ad-30s",
                "title": "粤语车广告 30s",
                "play": "/api/samples/car-ad-30s.mp4",
                "download": "/api/download/car-ad-30s.mp4",
            },
            {
                "id": "demo-30s",
                "title": "国风短片 30s",
                "play": "/api/samples/demo-30s.mp4",
                "download": "/api/download/demo-30s.mp4",
            },
        ],
    }


@app.post("/api/storyboard")
def storyboard(body: StoryboardIn):
    return plan_storyboard(body.model_dump())


@app.post("/api/render")
async def render(body: RenderIn):
    if not body.storyboard.get("scenes"):
        raise HTTPException(400, "缺少分镜")
    job = store.create(body.storyboard)
    asyncio.create_task(render_job(job["id"]))
    return {"job_id": job["id"], "job": job}


@app.get("/api/jobs")
def list_jobs():
    return store.list()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    if not store.get(job_id):
        raise HTTPException(404, "任务不存在")

    async def gen():
        last = 0
        path = JOBS_DIR / job_id / "events.jsonl"
        while True:
            job = store.get(job_id)
            if path.exists():
                lines = path.read_text(encoding="utf-8").splitlines()
                for line in lines[last:]:
                    yield f"data: {line}\n\n"
                last = len(lines)
            if job and job["status"] in {"done", "error"}:
                yield f"data: {json.dumps({'status': job['status'], 'progress': job['progress'], 'message': job['message'], 'video': job.get('video'), 'download': job.get('download'), 'stills': job.get('stills')}, ensure_ascii=False)}\n\n"
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/still/{index}")
def still(job_id: str, index: int):
    path = JOBS_DIR / job_id / f"still_{index:02d}.png"
    if not path.exists():
        raise HTTPException(404, "画面未生成")
    return FileResponse(path, media_type="image/png")


@app.post("/api/campaigns/car-ad")
async def render_car_ad():
    from backend.campaign import render as render_campaign

    path = await render_campaign()
    return {
        "video": "/api/samples/car-ad-30s.mp4",
        "download": "/api/download/car-ad-30s.mp4",
        "file": path.name,
    }


def _sample_path(name: str) -> Path:
    safe = Path(name).name
    path = ROOT / "samples" / safe
    if not path.exists() or path.suffix not in {".mp4", ".zip"}:
        raise HTTPException(404, "样片不存在")
    return path


def _send_file(path: Path, *, download: bool, filename: str) -> FileResponse:
    media = "application/zip" if path.suffix == ".zip" else "video/mp4"
    if download:
        return FileResponse(
            path,
            media_type=media,
            filename=filename,
            content_disposition_type="attachment",
            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"},
        )
    return FileResponse(
        path,
        media_type=media,
        content_disposition_type="inline",
        headers={"Accept-Ranges": "bytes"},
    )


@app.get("/api/samples/{name}")
def sample_video(name: str):
    path = _sample_path(name)
    return _send_file(path, download=False, filename=path.name)


@app.get("/api/download/{name}")
def download_sample(name: str):
    path = _sample_path(name)
    return _send_file(path, download=True, filename=path.name)


@app.get("/api/jobs/{job_id}/video")
def video(job_id: str):
    path = OUTPUT_DIR / f"{job_id}.mp4"
    if not path.exists():
        raise HTTPException(404, "成片未就绪")
    return _send_file(path, download=False, filename=f"lumina-{job_id}.mp4")


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str):
    path = OUTPUT_DIR / f"{job_id}.mp4"
    if not path.exists():
        raise HTTPException(404, "成片未就绪")
    return _send_file(path, download=True, filename=f"lumina-{job_id}.mp4")


frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="ui")
