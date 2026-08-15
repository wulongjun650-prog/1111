from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from backend.config import FONT_PATH

YELLOW = (255, 214, 0, 255)
RED = (226, 18, 32, 255)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
GREEN = (37, 211, 102, 255)


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_PATH), size)
    except OSError:
        return ImageFont.load_default()


def _text(draw: ImageDraw.ImageDraw, xy, text, size, fill=WHITE, stroke=4, anchor="lt"):
    draw.text(
        xy,
        text,
        font=_font(size),
        fill=fill,
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, 230),
        anchor=anchor,
    )


def blank(w: int, h: int) -> Image.Image:
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def save(im: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "PNG")
    return path


def hook(w: int, h: int, path: Path) -> Path:
    im = blank(w, h)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, int(h * 0.16)], fill=(0, 0, 0, 150))
    d.rectangle([0, int(h * 0.78), w, h], fill=(0, 0, 0, 170))
    _text(d, (int(w * 0.05), int(h * 0.04)), "限时清货", 28, YELLOW, 3)
    _text(d, (int(w * 0.05), int(h * 0.10)), "廿萬 揸走 奔驰 / 宝马 / 奥迪 / Tesla", 36, WHITE, 4)
    _text(d, (w // 2, int(h * 0.88)), "冇听错，真系咁笋！", 34, YELLOW, 4, anchor="mm")
    return save(im, path)


def reason(w: int, h: int, label: str, path: Path) -> Path:
    im = blank(w, h)
    d = ImageDraw.Draw(im)
    box = [int(w * 0.12), int(h * 0.38), int(w * 0.88), int(h * 0.62)]
    d.rounded_rectangle(box, radius=28, fill=(8, 8, 12, 200), outline=YELLOW, width=4)
    _text(d, (w // 2, h // 2), label, 48, YELLOW, 4, anchor="mm")
    return save(im, path)


def price(w: int, h: int, path: Path) -> Path:
    im = blank(w, h)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(
        [int(w * 0.10), int(h * 0.28), int(w * 0.90), int(h * 0.72)],
        radius=26,
        fill=(10, 10, 14, 210),
        outline=RED,
        width=5,
    )
    _text(d, (w // 2, int(h * 0.38)), "原价 40-50万", 36, WHITE, 3, anchor="mm")
    # strikethrough
    y = int(h * 0.38)
    d.line([(int(w * 0.28), y), (int(w * 0.72), y)], fill=RED, width=6)
    _text(d, (w // 2, int(h * 0.52)), "活动价 20万左右", 52, RED, 4, anchor="mm")
    _text(d, (w // 2, int(h * 0.64)), "奔驰 · 宝马 · 奥迪", 28, YELLOW, 3, anchor="mm")
    return save(im, path)


def features(w: int, h: int, path: Path) -> Path:
    im = blank(w, h)
    d = ImageDraw.Draw(im)
    labels = ["皮笼", "天窗", "360镜头"]
    gap = int(w * 0.04)
    bw = int((w - gap * 4) / 3)
    y0, y1 = int(h * 0.78), int(h * 0.93)
    for i, lab in enumerate(labels):
        x0 = gap + i * (bw + gap)
        d.rounded_rectangle([x0, y0, x0 + bw, y1], radius=18, fill=(0, 0, 0, 190), outline=YELLOW, width=3)
        _text(d, (x0 + bw // 2, (y0 + y1) // 2), lab, 32, YELLOW, 3, anchor="mm")
    return save(im, path)


def cta(w: int, h: int, path: Path) -> Path:
    im = blank(w, h)
    d = ImageDraw.Draw(im)
    d.rectangle([0, int(h * 0.74), w, h], fill=(7, 94, 84, 230))
    _text(d, (w // 2, int(h * 0.82)), "即刻入 WhatsApp 群", 40, WHITE, 4, anchor="mm")
    _text(d, (w // 2, int(h * 0.91)), "部车就系你㗎！", 36, YELLOW, 4, anchor="mm")
    return save(im, path)
