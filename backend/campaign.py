"""Render the 30s Cantonese luxury-car clearance commercial."""
from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image

from backend import composer, music, overlays, tts
from backend.config import ASPECTS, FONT_FAMILY, JOBS_DIR, OUTPUT_DIR, ROOT

ASSETS = ROOT / "assets" / "car-ad"
SAMPLES = ROOT / "samples"

SHOTS = [
    {
        "image": "car-ad-01-showroom-wide.png",
        "duration": 3.0,
        "motion": "whip_right",
        "vo": "廿万蚊，揸走奔驰、宝马、奥迪？仲有Tesla？冇听错，真系咁笋！",
        "rate": "+28%",
        "overlay": "hook",
    },
    {
        "image": "car-ad-02-showroom-aerial.png",
        "duration": 8.0,
        "motion": "zoom_in",
        "vo": "点解咁平？三个原因：一、年尾清库存冲业绩；二、现金买断、冇中间商；三、新款让路清旧款！",
        "rate": "+12%",
        "overlay": "reasons",
    },
    {
        "image": "car-ad-03-silver-front.png",
        "duration": 2.0,
        "motion": "zoom_in",
        "vo": "原价四、五十万嘅奔驰、宝马、奥迪，而家活动价廿万左右！",
        "rate": "+8%",
        "overlay": "price",
        "vo_span": 3,
    },
    {
        "image": "car-ad-04-white-side.png",
        "duration": 2.0,
        "motion": "pan_right",
        "vo": "",
        "overlay": "price",
    },
    {
        "image": "car-ad-05-black-grille.png",
        "duration": 2.0,
        "motion": "zoom_out",
        "vo": "",
        "overlay": "price",
    },
    {
        "image": "car-ad-06-interior.png",
        "duration": 7.0,
        "motion": "zoom_in",
        "vo": "皮笼、天窗、360镜头样样齐！",
        "rate": "+6%",
        "overlay": "features",
    },
    {
        "image": "car-ad-07-people-whatsapp.png",
        "duration": 6.0,
        "motion": "zoom_in",
        "vo": "即刻入WhatsApp群……部车就系你㗎！",
        "rate": "+10%",
        "overlay": "cta",
    },
]


def make_plate(src: Path, dest: Path, width: int, height: int, motion: str) -> Path:
    im = Image.open(src).convert("RGB")
    extra_x, extra_y = (1.85, 1.12) if motion == "whip_right" else (1.32, 1.32)
    tw, th = int(width * extra_x), int(height * extra_y)
    tw += tw % 2
    th += th % 2
    ratio = im.width / im.height
    target = tw / th
    if ratio > target:
        nh = th
        nw = int(th * ratio)
    else:
        nw = tw
        nh = int(tw / ratio)
    nw += nw % 2
    nh += nh % 2
    im = im.resize((nw, nh), Image.LANCZOS)
    left = max(0, (nw - tw) // 2)
    top = max(0, (nh - th) // 2)
    im = im.crop((left, top, left + tw, top + th))
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "JPEG", quality=95)
    return dest


def _overlay_pngs(work: Path, w: int, h: int) -> dict[str, Path]:
    return {
        "hook": overlays.hook(w, h, work / "ov_hook.png"),
        "reason1": overlays.reason(w, h, "① 年尾清库存", work / "ov_r1.png"),
        "reason2": overlays.reason(w, h, "② 跳过中间商", work / "ov_r2.png"),
        "reason3": overlays.reason(w, h, "③ 新款让路", work / "ov_r3.png"),
        "price": overlays.price(w, h, work / "ov_price.png"),
        "features": overlays.features(w, h, work / "ov_feat.png"),
        "cta": overlays.cta(w, h, work / "ov_cta.png"),
    }


def _apply_overlays(clip: Path, items: list[tuple[Path, float, float]], dest: Path) -> Path:
    if not items:
        dest.write_bytes(clip.read_bytes())
        return dest
    cmd = ["ffmpeg", "-y", "-i", str(clip)]
    for png, _, _ in items:
        cmd += ["-i", str(png)]
    chain = "[0:v]"
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


def _captions(scenes: list[dict], w: int, h: int, dest: Path) -> Path:
    rows = []
    t = 0.0
    for shot in SHOTS:
        vo = shot.get("vo") or ""
        if vo:
            rows.append({"narration": vo, "duration": shot["duration"] * shot.get("vo_span", 1)})
            # skip duration add for spanned vo on later empty shots
        t += shot["duration"]
    # rebuild with absolute times
    lines = [
        f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_FAMILY},34,&H00FFFFFF,&H000000FF,&H64000000,&H80000000,0,0,0,0,100,100,0,0,1,3,0,2,40,40,48,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ]
    t = 0.0
    i = 0
    while i < len(SHOTS):
        shot = SHOTS[i]
        span = shot.get("vo_span", 1)
        vo = shot.get("vo") or ""
        dur = sum(SHOTS[i + k]["duration"] for k in range(span))
        if vo:
            start = composer._ts(t)
            end = composer._ts(t + dur - 0.08)
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{vo}\n")
        t += shot["duration"]
        i += 1
    dest.write_text("".join(lines), encoding="utf-8")
    return dest


async def render(voice: str = "wanlung") -> Path:
    w, h = ASPECTS["16:9"]
    work = JOBS_DIR / "car-ad-30s"
    work.mkdir(parents=True, exist_ok=True)
    ovs = _overlay_pngs(work, w, h)

    clips: list[Path] = []
    narrations: list[tuple[Path, float]] = []
    t = 0.0
    for i, shot in enumerate(SHOTS):
        plate = make_plate(ASSETS / shot["image"], work / f"plate_{i:02d}.jpg", w, h, shot["motion"])
        raw = work / f"clip_{i:02d}_raw.mp4"
        composer.ken_burns(plate, raw, w, h, shot["duration"], "pan_right" if shot["motion"] == "whip_right" else shot["motion"])
        ov_items: list[tuple[Path, float, float]] = []
        kind = shot["overlay"]
        if kind == "hook":
            ov_items = [(ovs["hook"], 0.0, shot["duration"])]
        elif kind == "reasons":
            ov_items = [
                (ovs["reason1"], 0.15, 2.65),
                (ovs["reason2"], 2.65, 5.35),
                (ovs["reason3"], 5.35, 8.0),
            ]
        elif kind == "price":
            ov_items = [(ovs["price"], 0.0, shot["duration"])]
        elif kind == "features":
            ov_items = [(ovs["features"], 0.2, shot["duration"])]
        elif kind == "cta":
            ov_items = [(ovs["cta"], 0.15, shot["duration"])]
        out = work / f"clip_{i:02d}.mp4"
        _apply_overlays(raw, ov_items, out)
        clips.append(out)
        if shot.get("vo"):
            raw_vo = work / f"vo_{i:02d}.mp3"
            await tts.synthesize(shot["vo"], voice, raw_vo, rate=shot.get("rate", "+0%"))
            span = sum(SHOTS[i + k]["duration"] for k in range(shot.get("vo_span", 1)))
            fitted = work / f"vo_{i:02d}.m4a"
            composer.fit_voiceover(raw_vo, fitted, span)
            narrations.append((fitted, t))
        t += shot["duration"]

    silent = composer.concat_videos(clips, work / "silent.mp4")
    total = sum(s["duration"] for s in SHOTS)
    bgm = music.write_score(work / "bgm.m4a", total + 0.4, "product", 2049)

    # Place each VO at its timestamp over silence, then mix BGM.
    vo_track = work / "vo_timeline.m4a"
    if narrations:
        cmd = ["ffmpeg", "-y"]
        for path, _ in narrations:
            cmd += ["-i", str(path)]
        filters = []
        mix_ins = []
        for idx, (_, start) in enumerate(narrations):
            delayed = f"d{idx}"
            ms = int(start * 1000)
            filters.append(f"[{idx}:a]adelay={ms}|{ms},aformat=sample_rates=44100:channel_layouts=stereo[{delayed}]")
            mix_ins.append(f"[{delayed}]")
        filters.append(
            f"{''.join(mix_ins)}amix=inputs={len(narrations)}:duration=longest:dropout_transition=0,apad=whole_dur={total:.2f}[vo]"
        )
        cmd += ["-filter_complex", ";".join(filters), "-map", "[vo]", "-t", f"{total:.2f}", "-c:a", "aac", str(vo_track)]
        composer._run(cmd)
    mix = work / "mix.m4a"
    composer._run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(vo_track),
            "-i",
            str(bgm),
            "-filter_complex",
            "[1:a]volume=0.11[bg];[0:a]volume=1.15[vo];[vo][bg]amix=inputs=2:duration=first:dropout_transition=2",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(mix),
        ]
    )
    ass = _captions(SHOTS, w, h, work / "subs.ass")
    dest = SAMPLES / "car-ad-30s.mp4"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    composer.mux(silent, mix, ass, dest)
    return dest


def main() -> None:
    path = asyncio.run(render())
    print(path)


if __name__ == "__main__":
    main()
