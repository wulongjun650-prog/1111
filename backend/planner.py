from __future__ import annotations

import hashlib
import re
from typing import Any

from backend.config import STYLES

SCENE_TEMPLATES_ZH = [
    ("开场定调", "建立世界观与情绪，用大远景把观众拉进故事。"),
    ("核心意象", "把主题凝成一个可被看见的画面，让记忆落点。"),
    ("冲突或转折", "节奏上扬，光影更锋利，信息开始密集。"),
    ("细节特写", "靠近主体，用光斑与材质说话。"),
    ("人物或产品", "让主角登场，构图留出呼吸。"),
    ("高潮释放", "色彩与运动同时加压，给出最强一击。"),
    ("余韵回望", "镜头拉开，情绪落下，留下余味。"),
    ("收束字幕", "片名与一句收束语，像电影终章。"),
]

SCENE_TEMPLATES_EN = [
    ("Opening", "Establish the world with a wide, breathing shot."),
    ("Motif", "Lock the theme into one unforgettable image."),
    ("Turn", "Raise the pulse. Sharper light, denser information."),
    ("Close-up", "Move in. Let texture and bokeh speak."),
    ("Subject", "The hero arrives. Leave air in the frame."),
    ("Peak", "Color and motion hit together."),
    ("Afterglow", "Pull back. Let the feeling linger."),
    ("Title card", "End on the name and one last line."),
]


def _is_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _sentences(prompt: str) -> list[str]:
    parts = re.split(r"[。！？!?\n；;]+", prompt.strip())
    return [p.strip(" ，,") for p in parts if p.strip(" ，,")]


def _scene_count(duration: int) -> int:
    if duration <= 12:
        return 3
    if duration <= 20:
        return 4
    if duration <= 35:
        return 5
    if duration <= 50:
        return 6
    return 8


def _motifs(prompt: str) -> list[str]:
    table = [
        (r"山|峰|岳|mountain|peak|alps", "mountain"),
        (r"海|洋|浪|湖|ocean|sea|wave|lake", "ocean"),
        (r"城|市|楼|夜|city|skyline|urban|night", "city"),
        (r"星|宇|空|宇宙|galaxy|space|star|cosmos", "space"),
        (r"林|树|森|forest|tree|wood", "forest"),
        (r"花|春|樱|flower|blossom|garden", "garden"),
        (r"沙|漠|荒|desert|dune", "desert"),
        (r"雪|冬|冰|snow|ice|winter", "snow"),
        (r"雨|雾|rain|fog|mist", "rain"),
        (r"科技|未来|AI|数据|tech|future|cyber|data", "tech"),
        (r"产品|品牌|奢|product|luxury|brand", "product"),
        (r"人|少年|女孩|英雄|character|hero|girl|boy", "figure"),
        (r"龙|神话|仙|myth|dragon|immortal", "myth"),
        (r"日|阳|夕|sun|sunset|dawn", "sun"),
        (r"月|moon", "moon"),
    ]
    found: list[str] = []
    for pattern, tag in table:
        if re.search(pattern, prompt, re.I) and tag not in found:
            found.append(tag)
    if not found:
        found = ["mountain", "space"]
    return found[:4]


def _title(prompt: str, chinese: bool) -> str:
    cleaned = re.sub(r"\s+", " ", prompt).strip()
    if len(cleaned) <= 12:
        return cleaned
    if chinese:
        head = re.split(r"[，,。！!？?\s]", cleaned)[0]
        return (head[:10] if head else cleaned[:10]) + (" · 映界" if len(head) < 8 else "")
    words = cleaned.split()
    return " ".join(words[:5])


def _narration(prompt: str, idx: int, total: int, chinese: bool, title: str) -> str:
    sents = _sentences(prompt)
    if sents:
        base = sents[idx % len(sents)]
    else:
        base = prompt
    if chinese:
        wraps = [
            f"{title}。{base}。",
            f"镜头推进。{base}",
            f"在这一刻，{base}",
            f"{base}。世界因此被点亮。",
            f"记住这个画面。{base}",
            f"{base}。故事仍在呼吸。",
            f"风停之后，{base}",
            f"{title}，就此落幕。",
        ]
    else:
        wraps = [
            f"{title}. {base}.",
            f"The camera leans in. {base}",
            f"In this moment, {base}",
            f"{base}. The world catches fire.",
            f"Hold this frame. {base}",
            f"{base}. The story is still breathing.",
            f"After the wind, {base}",
            f"{title}. Cut to black.",
        ]
    text = wraps[idx % len(wraps)]
    if idx == 0 and chinese:
        text = f"{title}。{base}"
    if idx == total - 1 and chinese:
        text = f"{base}。{title}，完。"
    return text[:80]


def plan(
    prompt: str,
    style: str,
    duration: int,
    aspect: str,
    voice: str,
    language: str = "auto",
) -> dict[str, Any]:
    chinese = _is_chinese(prompt) if language == "auto" else language.startswith("zh")
    style = style if style in STYLES else "cinematic"
    n = _scene_count(duration)
    templates = SCENE_TEMPLATES_ZH if chinese else SCENE_TEMPLATES_EN
    motifs = _motifs(prompt)
    title = _title(prompt, chinese)
    seed = int(hashlib.sha256(prompt.encode()).hexdigest()[:8], 16)

    raw = [duration / n] * n
    raw[0] *= 1.08
    raw[-1] *= 1.12
    scale = duration / sum(raw)
    durs = [round(x * scale, 2) for x in raw]
    drift = duration - sum(durs)
    durs[-1] = round(durs[-1] + drift, 2)

    scenes = []
    for i in range(n):
        tpl = templates[i % len(templates)]
        motif = motifs[i % len(motifs)]
        scenes.append(
            {
                "index": i,
                "beat": tpl[0],
                "title": tpl[0],
                "visual": tpl[1],
                "motif": motif,
                "narration": _narration(prompt, i, n, chinese, title),
                "duration": max(2.4, durs[i]),
                "motion": ["zoom_in", "pan_right", "zoom_out", "pan_left"][i % 4],
            }
        )

    return {
        "title": title,
        "prompt": prompt.strip(),
        "style": style,
        "aspect": aspect,
        "voice": voice,
        "language": "zh" if chinese else "en",
        "duration": duration,
        "seed": seed,
        "motifs": motifs,
        "scenes": scenes,
        "logline": (prompt.strip()[:72] + "…") if len(prompt.strip()) > 72 else prompt.strip(),
    }
