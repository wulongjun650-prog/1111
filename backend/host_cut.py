"""UpCar-style host cut: stand among luxury cars, then sit inside and introduce."""
from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

from backend import composer, music, overlays, tts
from backend.cantonese_cut import (
    _apply_overlays,
    _ass,
    _encode_clip,
    _trim_voice,
    make_plate,
)
from backend.config import ASPECTS, JOBS_DIR, ROOT

ASSETS = ROOT / "assets" / "host"
SAMPLES = ROOT / "samples"

# Standing = original first two 口播. Sitting = expand like the Facebook host walkthrough.
PHRASES = [
    {"text": "廿万蚊！", "shot": "center", "motion": "zoom_in", "ov": "badge"},
    {"text": "揸走奔驰、宝马、奥迪？", "shot": "medium", "motion": "zoom_in", "ov": "badge"},
    {"text": "仲有Tesla？", "shot": "tesla", "motion": "zoom_in", "ov": "badge"},
    {"text": "冇听错，真系咁笋！", "shot": "center", "motion": "zoom_out", "ov": "badge"},
    {"text": "点解咁平？", "shot": "medium", "motion": "zoom_in", "ov": None},
    {"text": "三个原因。", "shot": "medium", "motion": "zoom_in", "ov": None},
    {"text": "一，年尾清库存冲业绩。", "shot": "center", "motion": "zoom_in", "ov": "r1"},
    {"text": "二，现金买断，冇中间商。", "shot": "benz", "motion": "zoom_in", "ov": "r2"},
    {"text": "三，新款让路清旧款！", "shot": "medium", "motion": "zoom_out", "ov": "r3"},
    {"text": "坐入去先。", "shot": "driver", "motion": "zoom_in", "ov": None},
    {"text": "原价四、五十万，而家活动价廿万左右！", "shot": "driver", "motion": "zoom_in", "ov": "now"},
    {"text": "真皮笼，一坐就知几舒服。", "shot": "leather", "motion": "zoom_in", "ov": "leather"},
    {"text": "天窗打开，成个天花都系光。", "shot": "sunroof", "motion": "zoom_in", "ov": "sunroof"},
    {"text": "360镜头，中控一眼睇晒四周。", "shot": "cam360", "motion": "zoom_in", "ov": "cam"},
    {"text": "即刻入WhatsApp群。", "shot": "cta", "motion": "zoom_in", "ov": "cta"},
    {"text": "部车，就系你㗎！", "shot": "cta", "motion": "zoom_in", "ov": "cta"},
]

SHOTS = {
    "center": "host-01-center-wide.png",
    "medium": "host-02-center-medium.png",
    "tesla": "host-03-tesla.png",
    "benz": "host-04-benz.png",
    "driver": "host-05-sit-driver.png",
    "leather": "host-06-sit-leather.png",
    "sunroof": "host-07-sit-sunroof.png",
    "cam360": "host-08-sit-360.png",
    "cta": "host-09-sit-cta.png",
}

TAILS = {
    "冇听错，真系咁笋！": 0.38,
    "三，新款让路清旧款！": 0.28,
    "坐入去先。": 0.22,
    "原价四、五十万，而家活动价廿万左右！": 0.32,
    "360镜头，中控一眼睇晒四周。": 0.24,
    "部车，就系你㗎！": 0.70,
}


async def render(voice: str = "wanlung", rate: str = "+14%") -> Path:
    w, h = ASPECTS["16:9"]
    work = JOBS_DIR / "host-upcar"
    work.mkdir(parents=True, exist_ok=True)
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

    clips: list[Path] = []
    n = 0
    groups = []
    for beat in beats:
        if groups and groups[-1]["shot"] == beat["shot"] and groups[-1]["motion"] == beat["motion"]:
            groups[-1]["beats"].append(beat)
            groups[-1]["hold"] += beat["hold"]
        else:
            groups.append({"shot": beat["shot"], "motion": beat["motion"], "beats": [beat], "hold": beat["hold"]})

    for g in groups:
        plate = work / f"plate_{n:02d}.jpg"
        make_plate(ASSETS / SHOTS[g["shot"]], plate, w, h, g["motion"])
        raw = work / f"shot_{n:02d}_g.mp4"
        _encode_clip(plate, raw, w, h, g["hold"], g["motion"], "warm")
        items = []
        rel = 0.0
        for beat in g["beats"]:
            if beat["ov"]:
                items.append((ovs[beat["ov"]], rel, rel + beat["hold"]))
            rel += beat["hold"]
        out = work / f"shot_{n:02d}.mp4"
        _apply_overlays(raw, items, out)
        clips.append(out)
        n += 1

    silent = composer.concat_videos(clips, work / "silent.mp4")
    padded = []
    for i, beat in enumerate(beats):
        out = work / f"vo_pad_{i:02d}.m4a"
        composer._run(
            [
                "ffmpeg", "-y", "-i", str(beat["path"]),
                "-af", f"apad=whole_dur={beat['hold']:.2f},aformat=sample_rates=44100:channel_layouts=stereo",
                "-t", f"{beat['hold']:.2f}", "-c:a", "aac", str(out),
            ]
        )
        padded.append(out)
    vo_all = work / "vo_all.m4a"
    lst = work / "vo.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in padded), encoding="utf-8")
    composer._run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(vo_all)])
    bgm = music.write_score(work / "bgm.m4a", total + 0.4, "product", 33)
    mix = work / "mix.m4a"
    composer._run(
        [
            "ffmpeg", "-y", "-i", str(vo_all), "-i", str(bgm),
            "-filter_complex",
            "[1:a]volume=0.07[bg];[0:a]volume=1.30[vo];[vo][bg]amix=inputs=2:duration=first:dropout_transition=1",
            "-c:a", "aac", "-b:a", "192k", str(mix),
        ]
    )
    ass = _ass(captions, w, h, work / "subs.ass")
    dest = SAMPLES / "host-upcar.mp4"
    composer.mux(silent, mix, ass, dest)
    (work / "meta.json").write_text(
        json.dumps({"duration": round(total, 2), "beats": len(beats)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    zpath = SAMPLES / "host-upcar.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(dest, dest.name)
    print(dest, round(total, 2))
    return dest


def main() -> None:
    asyncio.run(render())


if __name__ == "__main__":
    main()
