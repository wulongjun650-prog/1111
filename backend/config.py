from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = ROOT / "jobs"
OUTPUT_DIR = ROOT / "output"


def _pick_font() -> tuple[Path, str]:
    candidates = [
        (Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"), "WenQuanYi Micro Hei"),
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), "Noto Sans CJK SC"),
        (Path("/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf"), "Noto Sans SC"),
        (Path("C:/Windows/Fonts/msyh.ttc"), "Microsoft YaHei"),
        (Path("C:/Windows/Fonts/msyhbd.ttc"), "Microsoft YaHei"),
        (Path("C:/Windows/Fonts/simhei.ttf"), "SimHei"),
        (Path("/System/Library/Fonts/PingFang.ttc"), "PingFang SC"),
        (Path("/Library/Fonts/Arial Unicode.ttf"), "Arial Unicode MS"),
    ]
    for path, name in candidates:
        if path.exists():
            return path, name
    return Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"), "WenQuanYi Micro Hei"


FONT_PATH, FONT_FAMILY = _pick_font()

FPS = 24

ASPECTS: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (720, 720),
    "21:9": (1280, 548),
}

VOICES = {
    "xiaoxiao": ("zh-CN-XiaoxiaoNeural", "晓晓 · 温柔女声"),
    "yunxi": ("zh-CN-YunxiNeural", "云希 · 清朗男声"),
    "yunyang": ("zh-CN-YunyangNeural", "云扬 · 新闻男声"),
    "xiaoyi": ("zh-CN-XiaoyiNeural", "晓伊 · 知性女声"),
    "jenny": ("en-US-JennyNeural", "Jenny · English"),
    "guy": ("en-US-GuyNeural", "Guy · English"),
    "wanlung": ("zh-HK-WanLungNeural", "云龙 · 粤语男声"),
    "hiumaan": ("zh-HK-HiuMaanNeural", "晓曼 · 粤语女声"),
    "hiugaai": ("zh-HK-HiuGaaiNeural", "晓佳 · 粤语女声"),
}

STYLES = {
    "cinematic": {
        "label": "电影感",
        "hint": "宽银幕光影、青橙对比、胶片颗粒",
        "palette": [(8, 10, 18), (28, 42, 68), (196, 92, 48), (232, 196, 140), (250, 236, 210)],
        "accent": (232, 168, 96),
        "mood": "epic",
    },
    "guofeng": {
        "label": "国风水墨",
        "hint": "远山淡墨、朱砂残阳、宣纸肌理",
        "palette": [(24, 22, 20), (72, 64, 52), (168, 48, 36), (210, 186, 142), (236, 228, 208)],
        "accent": (176, 44, 36),
        "mood": "ink",
    },
    "cyberpunk": {
        "label": "赛博朋克",
        "hint": "霓虹雨夜、品红与电青",
        "palette": [(6, 4, 16), (18, 8, 42), (255, 40, 120), (20, 220, 255), (240, 230, 255)],
        "accent": (0, 229, 255),
        "mood": "neon",
    },
    "documentary": {
        "label": "纪录片",
        "hint": "克制色温、字幕叙事、真实质感",
        "palette": [(18, 18, 16), (48, 46, 40), (120, 110, 92), (196, 188, 168), (232, 228, 216)],
        "accent": (210, 186, 120),
        "mood": "doc",
    },
    "product": {
        "label": "产品广告",
        "hint": "棚拍光、静奢材质、品牌留白",
        "palette": [(10, 10, 12), (28, 28, 32), (80, 84, 96), (186, 190, 204), (248, 248, 252)],
        "accent": (212, 175, 106),
        "mood": "studio",
    },
    "dream": {
        "label": "梦幻",
        "hint": "柔焦光斑、粉紫雾气、慢镜头",
        "palette": [(22, 16, 36), (72, 40, 88), (236, 140, 176), (160, 196, 255), (255, 236, 244)],
        "accent": (255, 176, 210),
        "mood": "soft",
    },
    "tech": {
        "label": "科技 HUD",
        "hint": "网格、扫描线、数据光点",
        "palette": [(4, 10, 18), (8, 32, 56), (16, 120, 180), (80, 220, 255), (220, 245, 255)],
        "accent": (64, 220, 255),
        "mood": "hud",
    },
    "storybook": {
        "label": "绘本童话",
        "hint": "暖色块、剪影、故事灯火",
        "palette": [(36, 22, 18), (120, 64, 36), (232, 140, 64), (255, 214, 120), (255, 244, 214)],
        "accent": (255, 168, 64),
        "mood": "tale",
    },
}

for path in (JOBS_DIR, OUTPUT_DIR):
    path.mkdir(parents=True, exist_ok=True)
