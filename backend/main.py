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
    voice: str = "wanlung"
    language: str = "auto"


class RenderIn(BaseModel):
    storyboard: dict


@app.get("/api/meta")
def meta():
    videos = [
        {"id": "host-upcar", "title": "主持人站中间再坐车里 30s（静帧剪辑）", "play": "/api/samples/host-upcar.mp4", "download": "/api/download/host-upcar.mp4"},
        {"id": "cantonese-v1", "title": "V1 稳阵男声 16:9", "play": "/api/samples/cantonese-v1.mp4", "download": "/api/download/cantonese-v1.mp4"},
    ]
    talking = ROOT / "samples" / "host-talking.mp4"
    if talking.exists():
        videos.insert(
            0,
            {
                "id": "host-talking",
                "title": "本机真人开口（SadTalker）",
                "play": "/api/samples/host-talking.mp4",
                "download": "/api/download/host-talking.mp4",
            },
        )
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
        "videos": videos,
        "talking_host": _talking_status(),
    }


def _talking_status():
    from backend.talking_host import diagnose

    return diagnose()


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


@app.get("/api/talking-host/status")
def talking_host_status():
    from backend.talking_host import diagnose

    return diagnose()


@app.post("/api/campaigns/host-upcar")
async def render_host_upcar():
    from backend.host_cut import render as render_host

    path = await render_host()
    return {
        "video": "/api/samples/host-upcar.mp4",
        "download": "/api/download/host-upcar.mp4",
        "file": path.name,
    }


@app.post("/api/campaigns/cantonese-pack")
async def render_cantonese_pack():
    from backend.cantonese_cut import render_all

    paths = await render_all()
    return {
        "files": [p.name for p in paths],
        "zip": "/api/download/cantonese-v1-v5.zip",
        "videos": [f"/api/samples/{p.name}" for p in paths],
    }


@app.post("/api/campaigns/talking-host")
async def render_talking_host():
    from backend.jobs import _emit
    from backend.talking_host import TalkingNeedGPU, diagnose, render as render_talking

    info = diagnose()
    if not info["ok"]:
        raise HTTPException(status_code=412, detail=info)

    job = store.create(
        {
            "title": "本机真人开口",
            "scenes": [{"index": 0, "narration": "talking-host", "beat": "stand"}],
            "style": "product",
            "aspect": "16:9",
            "voice": "wanlung",
            "seed": 1,
        }
    )
    job_id = job["id"]

    async def run() -> None:
        store.update(job_id, status="running")
        work = JOBS_DIR / job_id
        work.mkdir(parents=True, exist_ok=True)
        try:
            def prog(pct: int, message: str, extra: dict | None = None) -> None:
                _emit(job_id, pct, message, extra)

            path = await render_talking(progress=prog, work=work)
            rel = "/api/samples/host-talking.mp4"
            dl = "/api/download/host-talking.mp4"
            store.update(
                job_id,
                status="done",
                progress=100,
                message=f"成片完成 {path.name}",
                video=rel,
                download=dl,
            )
            _emit(job_id, 100, "成片完成", {"status": "done", "video": rel, "download": dl})
        except TalkingNeedGPU as exc:
            store.update(job_id, status="error", error=exc.info["message"], message=exc.info["message"])
            _emit(job_id, 0, exc.info["message"], {"status": "error"})
        except Exception as exc:
            import traceback

            store.update(job_id, status="error", error=str(exc), message=f"失败：{exc}")
            _emit(job_id, store.get(job_id)["progress"], f"失败：{exc}", {"status": "error"})
            (work / "error.log").write_text(traceback.format_exc(), encoding="utf-8")

    asyncio.create_task(run())
    return {"job_id": job["id"], "job": job}


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
