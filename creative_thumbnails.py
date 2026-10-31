"""Bounded, side-effect-free thumbnail decoding for the local creative library."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable, Literal

from PIL import Image, ImageOps, UnidentifiedImageError


THUMBNAIL_SIZE = (96, 64)
MAX_THUMBNAIL_FILE_BYTES = 25 * 1024 * 1024
MAX_THUMBNAIL_SOURCE_PIXELS = 40_000_000

ThumbnailStatus = Literal["ready", "empty", "missing", "damaged"]


@dataclass(frozen=True)
class CreativeThumbnail:
    status: ThumbnailStatus
    path: str = ""
    png_data: bytes = b""
    source_width: int = 0
    source_height: int = 0
    message: str = ""


def _decode_thumbnail(
    path: Path,
    *,
    size: tuple[int, int],
    max_file_bytes: int,
    max_source_pixels: int,
) -> CreativeThumbnail:
    if not path.is_file():
        return CreativeThumbnail("missing", str(path), message="图片文件不存在")
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        return CreativeThumbnail("missing", str(path), message=str(exc))
    if file_size > max_file_bytes:
        return CreativeThumbnail(
            "damaged", str(path), message=f"图片文件过大，无法生成缩略图（{file_size} 字节）",
        )
    try:
        with Image.open(path) as source:
            source_width, source_height = source.size
            if source_width <= 0 or source_height <= 0:
                raise ValueError("图片尺寸无效")
            if source_width * source_height > max_source_pixels:
                raise ValueError(
                    f"图片像素过大，无法生成缩略图（{source_width}×{source_height}）"
                )
            # Animated formats use their first frame. JPEG may decode at a lower
            # resolution through draft(), reducing peak memory before thumbnail().
            source.seek(0)
            source.draft("RGB", size)
            preview = ImageOps.exif_transpose(source).convert("RGBA")
            preview.thumbnail(size, Image.Resampling.LANCZOS)
            background = Image.new("RGBA", preview.size, "white")
            background.alpha_composite(preview)
            output = BytesIO()
            background.convert("RGB").save(output, format="PNG", optimize=False)
        return CreativeThumbnail(
            "ready",
            str(path),
            output.getvalue(),
            source_width,
            source_height,
        )
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError) as exc:
        return CreativeThumbnail("damaged", str(path), message=str(exc))


def load_creative_thumbnail(
    paths: Iterable[str | Path],
    *,
    size: tuple[int, int] = THUMBNAIL_SIZE,
    max_file_bytes: int = MAX_THUMBNAIL_FILE_BYTES,
    max_source_pixels: int = MAX_THUMBNAIL_SOURCE_PIXELS,
) -> CreativeThumbnail:
    """Return the first safe preview, or a clear aggregate failure status.

    The returned PNG is intentionally memory-only. The creative-library JSON
    continues to contain file paths rather than image binary data.
    """
    normalized = [Path(raw).expanduser().resolve() for raw in paths if str(raw).strip()]
    if not normalized:
        return CreativeThumbnail("empty", message="方案中没有图片")
    failures: list[CreativeThumbnail] = []
    for path in normalized:
        result = _decode_thumbnail(
            path,
            size=size,
            max_file_bytes=max_file_bytes,
            max_source_pixels=max_source_pixels,
        )
        if result.status == "ready":
            return result
        failures.append(result)
    damaged = next((result for result in failures if result.status == "damaged"), None)
    if damaged:
        return damaged
    return failures[0]
