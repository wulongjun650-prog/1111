from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from backend.config import FONT_PATH, STYLES


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_PATH), size)
    except OSError:
        return ImageFont.load_default()


def _noise(h: int, w: int, cell: int, rng: np.random.Generator) -> np.ndarray:
    sh, sw = max(2, h // cell), max(2, w // cell)
    small = (rng.random((sh, sw)) * 255).astype(np.uint8)
    return np.asarray(Image.fromarray(small).resize((w, h), Image.BICUBIC), dtype=np.float32) / 255.0


def _lerp(a, b, t):
    t = np.clip(t, 0.0, 1.0)
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    return a * (1 - t[..., None]) + b * t[..., None]


def _vignette(h: int, w: int, strength: float = 0.55) -> np.ndarray:
    y = np.linspace(-1, 1, h)[:, None]
    x = np.linspace(-1, 1, w)[None, :]
    r = np.sqrt(x * x * 1.1 + y * y)
    return np.clip(1.0 - strength * np.power(r, 2.2), 0.15, 1.0)


def _glow(img: Image.Image, radius: int = 18, alpha: float = 0.45) -> Image.Image:
    blur = img.filter(ImageFilter.GaussianBlur(radius))
    return Image.blend(img, ImageEnhance.Brightness(blur).enhance(1.35), alpha)


def _draw_mountains(draw: ImageDraw.ImageDraw, w: int, h: int, rng: np.random.Generator, color, y0: float, amp: float, layers: int = 1):
    for _ in range(layers):
        pts = []
        steps = 18
        for i in range(steps + 1):
            x = int(w * i / steps)
            y = int(h * y0 + rng.normal(0, h * amp * 0.12) + np.sin(i * 0.7 + rng.random()) * h * amp)
            pts.append((x, y))
        pts += [(w, h), (0, h)]
        draw.polygon(pts, fill=tuple(int(c) for c in color))


def _add_stars(arr: np.ndarray, rng: np.random.Generator, n: int, color=(255, 255, 255)):
    h, w = arr.shape[:2]
    ys = rng.integers(0, h, n)
    xs = rng.integers(0, w, n)
    bri = rng.uniform(0.4, 1.0, n)
    for y, x, b in zip(ys, xs, bri):
        arr[y, x] = np.clip(arr[y, x] * (1 - b) + np.array(color) * b, 0, 255)
        if 1 <= y < h - 1 and 1 <= x < w - 1 and b > 0.85:
            arr[y - 1 : y + 2, x] = np.clip(arr[y - 1 : y + 2, x] + 40, 0, 255)
            arr[y, x - 1 : x + 2] = np.clip(arr[y, x - 1 : x + 2] + 40, 0, 255)


def _disk(h, w, cx, cy, rx, ry):
    y = np.arange(h)[:, None]
    x = np.arange(w)[None, :]
    return np.clip(1.0 - np.sqrt(((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2), 0, 1)


def _base_sky(h, w, palette, rng, motif: str) -> np.ndarray:
    n1 = _noise(h, w, 48, rng)
    n2 = _noise(h, w, 16, rng)
    yy = np.linspace(0, 1, h)[:, None]
    t = np.clip(yy * 0.75 + n1 * 0.35 + n2 * 0.12, 0, 1)
    c0, c1, c2, c3, c4 = palette
    low = _lerp(c0, c1, t)
    mid = _lerp(c1, c2, t)
    high = _lerp(c2, c4, np.clip(t * 1.2 - 0.2, 0, 1))
    mix = np.where(t[..., None] < 0.45, low, np.where(t[..., None] < 0.75, mid, high))
    if motif in {"space", "tech"}:
        mix = _lerp(c0, c1, n1 * 0.7 + n2 * 0.3)
    return mix


def _paint_motif(img: Image.Image, motif: str, style_key: str, palette, rng: np.random.Generator) -> Image.Image:
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    accent = STYLES[style_key]["accent"]
    mood = STYLES[style_key]["mood"]

    if motif in {"mountain", "forest", "snow", "desert"}:
        for i, y0, amp in [(0, 0.58, 0.10), (1, 0.68, 0.14), (2, 0.80, 0.10)]:
            col = palette[min(i + 1, 3)]
            shade = tuple(max(0, int(c * (0.45 + i * 0.18))) for c in col)
            _draw_mountains(draw, w, h, rng, shade, y0, amp)
        if motif == "forest":
            for _ in range(28):
                x = int(rng.integers(0, w))
                th = int(h * rng.uniform(0.18, 0.42))
                bw = int(rng.uniform(4, 14))
                draw.polygon([(x, h), (x + bw, h), (x + bw // 2, h - th)], fill=palette[1] + (180,))

    if motif in {"ocean"}:
        for i in range(7):
            y = int(h * (0.55 + i * 0.06))
            col = tuple(int(palette[2][j] * (0.5 + i * 0.07)) for j in range(3)) + (140,)
            draw.arc([int(-w * 0.2), y, int(w * 1.2), y + int(h * 0.18)], 0, 180, fill=col, width=3)

    if motif in {"city", "tech"}:
        rng_h = rng.integers(int(h * 0.12), int(h * 0.55), 42)
        for i, bh in enumerate(rng_h):
            x = int(i * w / 42 + rng.integers(-6, 6))
            bw = int(w / 42 * rng.uniform(0.45, 1.3))
            y = h - bh
            draw.rectangle([x, y, x + bw, h], fill=palette[0] + (230,))
            if rng.random() > 0.35:
                for wy in range(y + 8, h - 8, 10):
                    for wx in range(x + 3, x + bw - 3, 8):
                        if rng.random() > 0.55:
                            draw.rectangle([wx, wy, wx + 3, wy + 4], fill=accent + (200,))

    if motif in {"space"}:
        rgb = img.convert("RGB")
        arr = np.asarray(rgb).astype(np.float32)
        _add_stars(arr, rng, int(w * h * 0.0008), palette[4])
        img = Image.fromarray(arr.astype(np.uint8), "RGB").convert("RGBA")
        draw = ImageDraw.Draw(img, "RGBA")

    if motif in {"sun", "desert"} or mood in {"epic", "ink", "tale"}:
        cx, cy = int(w * rng.uniform(0.3, 0.72)), int(h * rng.uniform(0.18, 0.38))
        r = int(min(w, h) * rng.uniform(0.07, 0.14))
        for k, a in [(3.2, 40), (1.8, 80), (1.0, 220)]:
            rr = int(r * k)
            draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=accent + (a,))

    if motif in {"moon"}:
        cx, cy, r = int(w * 0.72), int(h * 0.22), int(min(w, h) * 0.08)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(236, 232, 220, 230))
        draw.ellipse([cx - r + int(r * 0.4), cy - r, cx + r + int(r * 0.4), cy + r], fill=(0, 0, 0, 0))

    if motif in {"garden"}:
        for _ in range(40):
            x, y = int(rng.integers(0, w)), int(rng.integers(int(h * 0.45), h))
            r = int(rng.uniform(4, 16))
            col = palette[int(rng.integers(2, 5))] + (160,)
            draw.ellipse([x - r, y - r, x + r, y + r], fill=col)

    if motif in {"rain"} or mood == "neon":
        for _ in range(90):
            x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
            draw.line([(x, y), (x + 2, y + int(h * 0.04))], fill=(200, 220, 255, 50), width=1)

    if motif in {"figure", "myth"}:
        base_x = w // 2
        draw.ellipse([base_x - 18, int(h * 0.52), base_x + 18, int(h * 0.52) + 36], fill=(20, 16, 14, 210))
        draw.polygon(
            [(base_x, int(h * 0.58)), (base_x - 40, int(h * 0.92)), (base_x + 40, int(h * 0.92))],
            fill=(16, 14, 18, 210),
        )

    if mood == "hud":
        for i in range(0, w, 48):
            draw.line([(i, 0), (i, h)], fill=accent + (28,), width=1)
        for i in range(0, h, 48):
            draw.line([(0, i), (w, i)], fill=accent + (28,), width=1)
        draw.rectangle([int(w * 0.08), int(h * 0.08), int(w * 0.92), int(h * 0.92)], outline=accent + (90), width=2)

    if mood == "studio":
        ped = [int(w * 0.28), int(h * 0.72), int(w * 0.72), int(h * 0.86)]
        draw.ellipse(ped, fill=(40, 42, 52, 180))
        draw.ellipse([int(w * 0.42), int(h * 0.38), int(w * 0.58), int(h * 0.72)], fill=accent + (200,))

    return img


def _letterbox(img: Image.Image, style_key: str) -> Image.Image:
    if style_key not in {"cinematic", "documentary"}:
        return img
    w, h = img.size
    if h >= w:
        bar = int(h * 0.08)
    else:
        bar = int(h * 0.10)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, bar], fill=(0, 0, 0))
    draw.rectangle([0, h - bar, w, h], fill=(0, 0, 0))
    return img


def _overlay_text(img: Image.Image, title: str, beat: str, index: int, style_key: str) -> Image.Image:
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    title_size = max(28, int(min(w, h) * 0.062))
    beat_size = max(14, int(min(w, h) * 0.028))
    tf = _font(title_size)
    bf = _font(beat_size)
    accent = STYLES[style_key]["accent"]
    margin = int(w * 0.07)
    y = int(h * (0.72 if h >= w else 0.68))
    if index == 0:
        draw.rectangle([margin, y - 8, margin + 48, y - 4], fill=accent + (220,))
        draw.text((margin, y), title[:16], font=tf, fill=(248, 244, 236, 240))
        draw.text((margin, y + title_size + 10), beat, font=bf, fill=accent + (200,))
    else:
        draw.text((margin, int(h * 0.88)), f"{index + 1:02d}  {beat}", font=bf, fill=(236, 228, 214, 180))
    return img


def render_still(
    *,
    width: int,
    height: int,
    style: str,
    motif: str,
    title: str,
    beat: str,
    index: int,
    seed: int,
    path: Path,
) -> Path:
    rng = np.random.default_rng(seed + index * 97)
    style = style if style in STYLES else "cinematic"
    palette = STYLES[style]["palette"]

    sky = _base_sky(height, width, palette, rng, motif)
    n = _noise(height, width, 7, rng)
    sky = np.clip(sky + (n[..., None] - 0.5) * 18, 0, 255)

    sun = _disk(height, width, width * 0.35, height * 0.28, width * 0.35, height * 0.28)
    sky = np.clip(sky + sun[..., None] * np.array(palette[3], dtype=np.float32) * 0.22, 0, 255)
    sky *= _vignette(height, width)[..., None]

    img = Image.fromarray(sky.astype(np.uint8), "RGB").convert("RGBA")
    img = _paint_motif(img, motif, style, palette, rng)
    img = _glow(img, radius=12, alpha=0.28)
    grain = _noise(height, width, 3, rng)
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    arr = np.clip(arr + (grain[..., None] - 0.5) * 16, 0, 255)
    img = Image.fromarray(arr.astype(np.uint8), "RGB")
    img = _letterbox(img, style)
    img = _overlay_text(img.convert("RGBA"), title, beat, index, style).convert("RGB")

    path.parent.mkdir(parents=True, exist_ok=True)
    # Render slightly larger so Ken Burns has room to move.
    oversized = img.resize((int(width * 1.28), int(height * 1.28)), Image.LANCZOS)
    oversized.save(path, "PNG", optimize=True)
    return path
