"""Five coherent Cantonese car-ad cuts. Picture follows 口播, no chipmunk squeeze."""
from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

from PIL import Image

from backend import composer, music, overlays, tts
from backend.config import ASPECTS, JOBS_DIR, ROOT

ASSETS = ROOT / "assets" / "car-ad"
SAMPLES = ROOT / "samples"

BLOCKS = [
    ("hook", "廿万蚊，揸走奔驰、宝马、奥迪？仲有Tesla？冇听错，真系咁笋！"),
    ("why", "点解咁平？三个原因：一、年尾清库存冲业绩；二、现金买断、冇中间商；三、新款让路清旧款！"),
    ("price", "原价四、五十万嘅奔驰、宝马、奥迪，而家活动价廿万左右！"),
    ("spec", "皮笼、天窗、360镜头样样齐！"),
    ("cta", "即刻入WhatsApp群，部车就系你㗎！"),
]

# Short beats so Edge TTS does not park on commas. Picture cuts on each beat.
PHRASES = [
    {"text": "廿万蚊！", "block": "hook", "shot": "wide", "motion": "whip_right", "ov": "badge"},
    {"text": "揸走奔驰、宝马、奥迪？", "block": "hook", "shot": "wide", "motion": "whip_right", "ov": "badge"},
    {"text": "仲有Tesla？", "block": "hook", "shot": "tesla", "motion": "zoom_in", "ov": "badge"},
    {"text": "冇听错，真系咁笋！", "block": "hook", "shot": "wide", "motion": "zoom_out", "ov": "badge"},
    {"text": "点解咁平？", "block": "why", "shot": "aerial", "motion": "zoom_in", "ov": None},
    {"text": "三个原因。", "block": "why", "shot": "aerial", "motion": "zoom_in", "ov": None},
    {"text": "一，年尾清库存冲业绩。", "block": "why", "shot": "aerial", "motion": "zoom_in", "ov": "r1"},
    {"text": "二，现金买断，冇中间商。", "block": "why", "shot": "aerial", "motion": "zoom_in", "ov": "r2"},
    {"text": "三，新款让路清旧款！", "block": "why", "shot": "aerial", "motion": "zoom_in", "ov": "r3"},
    {"text": "原价四、五十万嘅奔驰。", "block": "price", "shot": "benz", "motion": "zoom_in", "ov": "was"},
    {"text": "宝马、奥迪，", "block": "price", "shot": "bmw", "motion": "pan_right", "ov": "was"},
    {"text": "而家活动价廿万左右！", "block": "price", "shot": "audi", "motion": "zoom_out", "ov": "now"},
    {"text": "皮笼。", "block": "spec", "shot": "leather", "motion": "zoom_in", "ov": "leather"},
    {"text": "天窗。", "block": "spec", "shot": "sunroof", "motion": "zoom_in", "ov": "sunroof"},
    {"text": "360镜头，样样齐！", "block": "spec", "shot": "cam360", "motion": "zoom_in", "ov": "cam"},
    {"text": "即刻入WhatsApp群。", "block": "cta", "shot": "person", "motion": "zoom_in", "ov": "cta"},
    {"text": "部车，就系你㗎！", "block": "cta", "shot": "person", "motion": "zoom_in", "ov": "cta"},
]

LANDSCAPE = {
    "wide": "car-ad-01-showroom-wide.png",
    "aerial": "car-ad-02-showroom-aerial.png",
    "benz": "car-ad-03-silver-front.png",
    "bmw": "car-ad-04-white-side.png",
    "audi": "car-ad-05-black-grille.png",
    "tesla": "car-ad-08-tesla.png",
    "leather": "car-ad-06-interior.png",
    "sunroof": "car-ad-09-sunroof.png",
    "cam360": "car-ad-10-360.png",
    "person": "car-ad-07-people-whatsapp.png",
}

PORTRAIT = {
    "wide": "car-ad-v-01-wide.png",
    "aerial": "car-ad-v-02-aerial.png",
    "benz": "car-ad-v-03-silver.png",
    "bmw": "car-ad-v-04-white.png",
    "audi": "car-ad-v-05-black.png",
    "tesla": "car-ad-v-08-tesla.png",
    "leather": "car-ad-v-06-leather.png",
    "sunroof": "car-ad-v-09-sunroof.png",
    "cam360": "car-ad-v-10-360.png",
    "person": "car-ad-v-07-person.png",
}

GRADES = {
    "warm": "eq=contrast=1.05:saturation=1.06:gamma=1.02",
    "punch": "eq=contrast=1.10:saturation=1.10:brightness=0.015",
    "cinematic": "eq=contrast=1.07:saturation=1.02:gamma=1.01",
}

TAILS = {
    "冇听错，真系咁笋！": 0.42,
    "三个原因。": 0.12,
    "一，年尾清库存冲业绩。": 0.16,
    "二，现金买断，冇中间商。": 0.16,
    "三，新款让路清旧款！": 0.28,
    "而家活动价廿万左右！": 0.34,
    "360镜头，样样齐！": 0.28,
    "部车，就系你㗎！": 0.72,
}

VERSIONS = [
    {"id": "v1", "title": "V1 稳阵男声 16:9", "voice": "wanlung", "rate": "+14%", "aspect": "16:9", "breath": 0.14, "grade": "warm"},
    {"id": "v2", "title": "V2 旺场男声 9:16", "voice": "wanlung", "rate": "+20%", "aspect": "9:16", "breath": 0.08, "grade": "punch"},
    {"id": "v3", "title": "V3 亲切女声 9:16", "voice": "hiumaan", "rate": "+12%", "aspect": "9:16", "breath": 0.16, "grade": "warm"},
    {"id": "v4", "title": "V4 利落女声 16:9", "voice": "hiugaai", "rate": "+18%", "aspect": "16:9", "breath": 0.09, "grade": "punch"},
    {"id": "v5", "title": "V5 完整男声 9:16", "voice": "wanlung", "rate": "+10%", "aspect": "9:16", "breath": 0.20, "grade": "cinematic"},
]


def _pack(aspect: str) -> dict[str, str]:
    return PORTRAIT if aspect == "9:16" else LANDSCAPE


def make_plate(src: Path, dest: Path, width: int, height: int, motion: str) -> Path:
    im = Image.open(src).convert("RGB")
    extra_x, extra_y = (1.72, 1.10) if motion == "whip_right" else (1.28, 1.28)
    tw, th = int(width * extra_x), int(height * extra_y)
    tw += tw % 2
    th += th % 2
    ratio = im.width / im.height
    target = tw / th
    if ratio > target:
        nh, nw = th, int(th * ratio)
    else:
        nw, nh = tw, int(tw / ratio)
    nw += nw % 2
    nh += nh % 2
    im = im.resize((nw, nh), Image.LANCZOS)
    left = max(0, (nw - tw) // 2)
    top = max(0, (nh - th) // 2)
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.crop((left, top, left + tw, top + th)).save(dest, "JPEG", quality=94)
    return dest


def _mark(marks: list[dict], *needles: str) -> float | None:
    for m in marks:
        for n in needles:
            if n and n in m["text"]:
                return float(m["offset"])
    return None


def _splits(marks: list[dict], keys: list[tuple[str, ...]], duration: float) -> list[float]:
    hits = [_mark(marks, *k) for k in keys]
    if any(h is None for h in hits) or not hits:
        n = len(keys) + 1
        return [duration * i / n for i in range(n)]
    times = [0.0] + [h for h in hits if h is not None]
    # ensure increasing
    cleaned = [times[0]]
    for t in times[1:]:
        cleaned.append(max(t, cleaned[-1] + 0.35))
    if cleaned[-1] >= duration - 0.2:
        cleaned[-1] = max(cleaned[-2] + 0.35, duration * 0.55)
    cleaned.append(duration)
    return cleaned


def _apply_overlays(clip: Path, items: list[tuple[Path, float, float]], dest: Path) -> Path:
    if not items:
        dest.write_bytes(clip.read_bytes())
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
    cmd += ["-filter_complex", ";".join(filters), "-map", f"[{last}]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", str(dest)]
    composer._run(cmd)
    return dest


def _encode_clip(plate: Path, dest: Path, w: int, h: int, dur: float, motion: str, grade: str) -> Path:
    fade = 0.07 if dur < 1.8 else 0.12
    fade_out = max(0.04, dur - fade)
    kb_motion = "pan_right" if motion == "whip_right" else motion
    # reuse ken_burns then grade
    raw = dest.with_name(dest.stem + "_raw.mp4")
    composer.ken_burns(plate, raw, w, h, dur, kb_motion)
    vf = f"{GRADES[grade]},format=yuv420p"
    composer._run(
        ["ffmpeg", "-y", "-i", str(raw), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", str(dest)]
    )
    return dest


def _ass(captions: list[tuple[float, float, str]], w: int, h: int, dest: Path) -> Path:
    size = 32 if h > w else 34
    margin = 90 if h > w else 52
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,WenQuanYi Micro Hei,{size},&H00FFFFFF,&H000000FF,&H64000000,&H80000000,0,0,0,0,100,100,0,0,1,3,0,2,36,36,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for start, end, text in captions:
        lines.append(f"Dialogue: 0,{composer._ts(start)},{composer._ts(end)},Default,,0,0,0,,{text}\n")
    dest.write_text("".join(lines), encoding="utf-8")
    return dest


def _trim_voice(src: Path, dest: Path) -> float:
    composer._run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-af",
            "silenceremove=start_periods=1:start_duration=0.03:start_threshold=-34dB:detection=peak,"
            "areverse,silenceremove=start_periods=1:start_duration=0.05:start_threshold=-34dB:detection=peak,areverse",
            "-c:a",
            "aac",
            str(dest),
        ]
    )
    return tts.probe_duration(dest)


async def render_version(ver: dict) -> Path:
    w, h = ASPECTS[ver["aspect"]]
    work = JOBS_DIR / f"cantonese-{ver['id']}"
    work.mkdir(parents=True, exist_ok=True)
    pack = _pack(ver["aspect"])
    ovs = {
        "badge": overlays.hook_badge(w, h, work / "ov_badge.png"),
        "r1": overlays.reason(w, h, "① 年尾清库存", work / "ov_r1.png"),
        "r2": overlays.reason(w, h, "② 跳过中间商", work / "ov_r2.png"),
        "r3": overlays.reason(w, h, "③ 新款让路", work / "ov_r3.png"),
        "was": overlays.price_was(w, h, work / "ov_was.png"),
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
        await tts.synthesize(phrase["text"], ver["voice"], raw, rate=ver["rate"])
        trimmed = work / f"vo_{i:02d}.m4a"
        dur = max(0.45, _trim_voice(raw, trimmed))
        tail = TAILS.get(phrase["text"], 0.0)
        if ver["id"] == "v5":
            tail *= 1.45
        hold = dur + ver["breath"] + tail
        beats.append({**phrase, "path": trimmed, "dur": dur, "hold": hold, "t": t})
        captions.append((t, t + dur, phrase["text"].strip("。，")))
        t += hold
    total = t

    clips: list[Path] = []
    n = 0

    def add(name: str, dur: float, motion: str, overlay_items):
        nonlocal n
        plate = work / f"plate_{n:02d}.jpg"
        make_plate(ASSETS / pack[name], plate, w, h, motion)
        raw = work / f"shot_{n:02d}_g.mp4"
        _encode_clip(plate, raw, w, h, dur, motion, ver["grade"])
        out = work / f"shot_{n:02d}.mp4"
        _apply_overlays(raw, overlay_items, out)
        clips.append(out)
        n += 1

    groups = []
    for beat in beats:
        if groups and groups[-1]["shot"] == beat["shot"] and groups[-1]["motion"] == beat["motion"]:
            groups[-1]["beats"].append(beat)
            groups[-1]["hold"] += beat["hold"]
        else:
            groups.append({"shot": beat["shot"], "motion": beat["motion"], "beats": [beat], "hold": beat["hold"]})

    for g in groups:
        items = []
        rel = 0.0
        for beat in g["beats"]:
            if beat["ov"]:
                items.append((ovs[beat["ov"]], rel, rel + beat["hold"]))
            rel += beat["hold"]
        add(g["shot"], g["hold"], g["motion"], items)

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

    bgm = music.write_score(work / "bgm.m4a", total + 0.5, "product", 88 + ord(ver["id"][-1]))
    mix = work / "mix.m4a"
    composer._run(
        [
            "ffmpeg", "-y", "-i", str(vo_all), "-i", str(bgm),
            "-filter_complex",
            "[1:a]volume=0.08[bg];[0:a]volume=1.28[vo];[vo][bg]amix=inputs=2:duration=first:dropout_transition=1",
            "-c:a", "aac", "-b:a", "192k", str(mix),
        ]
    )

    ass = _ass(captions, w, h, work / "subs.ass")
    dest = SAMPLES / f"cantonese-{ver['id']}.mp4"
    composer.mux(silent, mix, ass, dest)
    meta = {
        "id": ver["id"],
        "title": ver["title"],
        "voice": ver["voice"],
        "rate": ver["rate"],
        "aspect": ver["aspect"],
        "duration": round(total, 2),
        "beats": [{"text": b["text"], "dur": round(b["dur"], 2), "hold": round(b["hold"], 2), "shot": b["shot"]} for b in beats],
    }
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"id": ver["id"], "duration": meta["duration"], "beats": len(beats)}, ensure_ascii=False))
    return dest


async def render_all() -> list[Path]:
    paths = []
    for ver in VERSIONS:
        paths.append(await render_version(ver))
    zpath = SAMPLES / "cantonese-v1-v5.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, p.name)
    print(zpath)
    return paths


def main() -> None:
    asyncio.run(render_all())


if __name__ == "__main__":
    main()
