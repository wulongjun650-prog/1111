#!/usr/bin/env python3
"""Compress landing images and write optimized assets under static/picture/."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "static" / "picture"
OUT = ROOT / "static" / "picture"  # overwrite optimized outputs

# 200KB / 400KB (KiB) caps requested for delivery
MAX_IMAGE_BYTES = 200 * 1024 - 512  # small margin under 200KiB/file
MAX_HTML_BYTES = 400 * 1024
LONG_MAX_WIDTH = 540  # matches .app-container max-width


def save_webp_bytes(im: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="WEBP", quality=quality, method=6)
    return buf.getvalue()


def save_jpeg_bytes(im: Image.Image, quality: int) -> bytes:
    if im.mode in ("RGBA", "P"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def compress_long_webp(src: Path, dest: Path) -> int:
    im = Image.open(src)
    if im.mode == "P":
        im = im.convert("RGB")
    w, h = im.size
    if w > LONG_MAX_WIDTH:
        nh = int(h * LONG_MAX_WIDTH / w)
        im = im.resize((LONG_MAX_WIDTH, nh), Image.Resampling.LANCZOS)

    lo, hi = 20, 95
    best = save_webp_bytes(im, lo)
    while lo <= hi:
        mid = (lo + hi) // 2
        data = save_webp_bytes(im, mid)
        if len(data) <= MAX_IMAGE_BYTES:
            best = data
            lo = mid + 1
        else:
            hi = mid - 1

    if len(best) > MAX_IMAGE_BYTES:
        # Still too large: shrink width further
        scale = 0.85
        while len(best) > MAX_IMAGE_BYTES and im.width > 360:
            nw = int(im.width * scale)
            nh = int(im.height * scale)
            im = im.resize((nw, nh), Image.Resampling.LANCZOS)
            best = save_webp_bytes(im, 60)
            scale *= 0.9

    dest.write_bytes(best)
    return len(best)


def compress_icon_jpg(src: Path, dest: Path) -> int:
    im = Image.open(src)
    im = im.resize((96, 96), Image.Resampling.LANCZOS)
    lo, hi = 40, 95
    best = save_jpeg_bytes(im, lo)
    while lo <= hi:
        mid = (lo + hi) // 2
        data = save_jpeg_bytes(im, mid)
        if len(data) <= MAX_IMAGE_BYTES:
            best = data
            lo = mid + 1
        else:
            hi = mid - 1
    dest.write_bytes(best)
    return len(best)


def main() -> None:
    long_src = SRC / "lch.webp"
    icon_src = SRC / "2.jpg"
    if not long_src.exists() or not icon_src.exists():
        raise SystemExit("Missing source images in static/picture/")

    # Keep originals once for re-runs from zip
    orig_long = SRC / "lch.original.webp"
    orig_icon = SRC / "2.original.jpg"
    if not orig_long.exists():
        orig_long.write_bytes(long_src.read_bytes())
    if not orig_icon.exists():
        orig_icon.write_bytes(icon_src.read_bytes())

    long_n = compress_long_webp(orig_long, long_src)
    icon_n = compress_icon_jpg(orig_icon, icon_src)
    print(f"lch.webp -> {long_n} bytes")
    print(f"2.jpg -> {icon_n} bytes")
    if long_n > MAX_IMAGE_BYTES or icon_n > MAX_IMAGE_BYTES:
        raise SystemExit("Image exceeds 200KB limit")


if __name__ == "__main__":
    main()
