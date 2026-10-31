"""Google Ads image Asset upload through the signed-in AdsPower page.

The browser page performs the resumable requests, so its cookies, proxy and
Google Ads session are used.  Python only validates the local file, transfers
its bytes to the page over CDP, and parses the returned Asset ID.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import struct
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PROTO_INT64 = (1 << 63) - 1
# Responsive Display protocol field 20 is the portrait-marketing-image list.
# It was previously mislabeled as a square logo list.  Real ads in the signed-in
# account resolve those Assets to 941 x 1672 images (9:16), so keep the role
# names aligned with the protocol payload all the way from import to creation.
ROLE_RATIOS = {"landscape": 1.91, "square": 1.0, "portrait": 9 / 16}
ROLE_ALLOWED_RATIOS = {
    "landscape": (1.91,),
    "square": (1.0,),
    "portrait": (9 / 16, 4 / 5),
}
ROLE_LABELS = {"landscape": "横图", "square": "方图", "portrait": "竖图"}
ALLOWED_RATIOS = tuple(
    dict.fromkeys(ratio for ratios in ROLE_ALLOWED_RATIOS.values() for ratio in ratios)
)
_UPLOAD_CONTEXT = b"\x20\x40\x58\x07\x58\x26\x58\x1d\x60\x04"
ASSET_FOLDER_FIELDS = [
    "asset_id", "creation_time_micros", "asset_type", "asset_status",
    "duration_millis", "file_size", "width", "height", "aspect_ratio",
    "original_asset_id", "asset_thumbnail_url",
    "google_hosted_video_asset_thumbnails",
    "google_hosted_audio_video_transcodes", "google_hosted_audio_video_id",
    "asset_ui_last_used_millis", "asset_preview_url", "source_detail",
    "image_eligibility", "image_mime_type", "image_hosting_key",
    "image_has_video", "stock_id", "crop_bounding_box", "asset_source",
    "asset_generation_data", "asset_generation_data_source_type",
    "synthetic_content_advertiser_input_status", "source_details",
    "internal_metadata", "youtube_video_id", "youtube_channel_title",
    "youtube_aspect_ratio", "youtube_data_unavailable", "folder_id",
    "folder_status", "level", "name", "parent_id",
    "last_ui_updated_timestamp_micros", "asset_usage_policy_status",
    "asset_usage_policy_details", "brand_verification_status",
    "brand_verification_failure_reasons", "merged_asset_status",
]
REFERENCE_IMAGE_HOST_SUFFIXES = (
    ".googlesyndication.com", ".googleusercontent.com", ".gstatic.com",
)
MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ImageInfo:
    path: str
    filename: str
    mime_type: str
    size: int
    width: int
    height: int
    ratio: float
    content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImageUploadResult:
    ok: bool
    asset_id: str
    filename: str
    size: int
    width: int
    height: int
    content_sha256: str
    start_status: int
    finalize_status: int
    error: str = ""
    error_code: str = ""
    upload_status: str = ""
    start_attempts: int = 0
    finalize_attempts: int = 0
    deduplicated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"{path.name}：PNG 文件无效")
    return struct.unpack(">II", header[16:24])


def _read_jpeg_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        if stream.read(2) != b"\xff\xd8":
            raise ValueError(f"{path.name}：JPEG 文件无效")
        while True:
            marker_start = stream.read(1)
            if not marker_start:
                break
            if marker_start != b"\xff":
                continue
            marker = stream.read(1)
            while marker == b"\xff":
                marker = stream.read(1)
            if not marker:
                break
            code = marker[0]
            if code in {0xD8, 0xD9}:
                continue
            raw_length = stream.read(2)
            if len(raw_length) != 2:
                break
            length = struct.unpack(">H", raw_length)[0]
            if length < 2:
                break
            if code in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            }:
                data = stream.read(5)
                if len(data) == 5:
                    height, width = struct.unpack(">HH", data[1:5])
                    return width, height
                break
            stream.seek(length - 2, os.SEEK_CUR)
    raise ValueError(f"{path.name}：无法读取 JPEG 尺寸")


def validate_image(path: str | os.PathLike[str]) -> ImageInfo:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"图片不存在：{resolved}")
    size = resolved.stat().st_size
    if size <= 0:
        raise ValueError(f"{resolved.name}：图片为空")
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"{resolved.name}：图片超过 5 MB")
    suffix = resolved.suffix.lower()
    if suffix == ".png":
        mime_type = "image/png"
        width, height = _read_png_dimensions(resolved)
    elif suffix in {".jpg", ".jpeg"}:
        mime_type = "image/jpeg"
        width, height = _read_jpeg_dimensions(resolved)
    else:
        raise ValueError(f"{resolved.name}：仅支持 PNG、JPG、JPEG")
    if width <= 0 or height <= 0:
        raise ValueError(f"{resolved.name}：图片尺寸无效")
    ratio = width / height
    if min(abs(ratio - allowed) for allowed in ALLOWED_RATIOS) > 0.03:
        raise ValueError(
            f"{resolved.name}：比例 {ratio:.3f} 不支持，应为 1.91:1、1:1、9:16 或 4:5"
        )
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return ImageInfo(
        str(resolved), resolved.name, mime_type, size, width, height,
        round(ratio, 4), digest,
    )


def validate_role_image(path: str | os.PathLike[str], role: str) -> ImageInfo:
    if role not in ROLE_RATIOS:
        raise ValueError(f"未知图片用途：{role}")
    info = validate_image(path)
    expected = ROLE_ALLOWED_RATIOS[role]
    if min(abs(info.ratio - ratio) for ratio in expected) > 0.03:
        expected_label = {
            "landscape": "1.91:1", "square": "1:1", "portrait": "9:16 或 4:5",
        }[role]
        raise ValueError(
            f"{info.filename}：{ROLE_LABELS[role]}要求 {expected_label}，"
            f"当前为 {info.width}:{info.height}（{info.ratio:.3f}:1）"
        )
    return info


def classify_image_role(path: str | os.PathLike[str]) -> tuple[str, ImageInfo, str]:
    """Classify a Responsive Display marketing image from its real dimensions."""
    info = validate_image(path)
    if abs(info.ratio - ROLE_RATIOS["landscape"]) <= 0.03:
        return "landscape", info, f"比例 {info.ratio:.3f}:1，自动识别为横图"
    if abs(info.ratio - ROLE_RATIOS["square"]) <= 0.03:
        return "square", info, "比例 1:1，默认识别为方图"
    if abs(info.ratio - 9 / 16) <= 0.03:
        return "portrait", info, f"比例 {info.ratio:.3f}:1，自动识别为 9:16 竖图"
    if abs(info.ratio - 4 / 5) <= 0.03:
        return "portrait", info, f"比例 {info.ratio:.3f}:1，自动识别为 4:5 竖图"
    raise ValueError(
        f"{info.filename}：比例 {info.ratio:.3f}:1 暂不用于当前响应式展示广告；"
        "请使用 1.91:1 横图、1:1 方图、9:16 或 4:5 竖图"
    )


def classify_image_roles(
    paths: Iterable[str | os.PathLike[str]],
) -> tuple[dict[str, list[ImageInfo]], list[dict[str, str]]]:
    """Classify many files without aborting the whole selection on one bad file."""
    grouped: dict[str, list[ImageInfo]] = {key: [] for key in ROLE_RATIOS}
    classified: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        try:
            role, info, reason = classify_image_role(path)
        except Exception as exc:
            classified.append({
                "path": str(path), "filename": Path(path).name, "role": "",
                "role_label": "未导入", "reason": str(exc), "error": str(exc),
            })
            continue
        key = (role, info.content_sha256)
        duplicate = key in seen
        if not duplicate:
            grouped[role].append(info)
            seen.add(key)
        classified.append({
            "path": info.path, "filename": info.filename, "role": role,
            "role_label": ROLE_LABELS[role],
            "reason": reason + ("；内容重复，已跳过" if duplicate else ""),
            "error": "", "duplicate": "1" if duplicate else "",
        })
    return grouped, classified


def validate_role_images(paths: dict[str, list[str]]) -> dict[str, list[ImageInfo]]:
    result: dict[str, list[ImageInfo]] = {key: [] for key in ROLE_RATIOS}
    seen: set[tuple[str, str]] = set()
    for role in ROLE_RATIOS:
        for path in paths.get(role) or []:
            info = validate_role_image(path, role)
            key = (role, info.content_sha256)
            if key not in seen:
                result[role].append(info)
                seen.add(key)
    return result


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("protobuf varint 不能为负数")
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _field_varint(number: int, value: int) -> bytes:
    return _encode_varint(number << 3) + _encode_varint(value)


def _field_bytes(number: int, value: bytes) -> bytes:
    return _encode_varint((number << 3) | 2) + _encode_varint(len(value)) + value


def _numeric_id(value: str | int, label: str) -> int:
    text = str(value or "").replace("-", "")
    if not text.isdigit():
        raise ValueError(f"{label} 必须是数字 ID")
    numeric = int(text)
    if numeric <= 0 or numeric > MAX_PROTO_INT64:
        raise ValueError(f"{label} 必须是正 int64")
    return numeric


def build_upload_metadata(filename: str, size: int, account_id: str | int, manager_id: str | int) -> str:
    account = _numeric_id(account_id, "account_id")
    manager = _numeric_id(manager_id, "manager_id")
    raw = b"".join((
        _field_bytes(1, filename.encode("utf-8")),
        _field_varint(2, account),
        _field_varint(3, 2),
        _field_bytes(6, b"m"),
        _field_varint(9, int(size)),
        _field_varint(10, account),
        _field_varint(11, manager),
        _field_bytes(14, _UPLOAD_CONTEXT),
    ))
    return base64.b64encode(raw).decode("ascii")


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if offset >= len(payload):
            raise ValueError("protobuf varint 截断")
        current = payload[offset]
        offset += 1
        if index == 9 and current > 1:
            raise ValueError("protobuf varint 超过 uint64")
        value |= (current & 0x7F) << (index * 7)
        if current < 0x80:
            return value, offset
    raise ValueError("protobuf varint 无效")


def extract_asset_id(finalize_response: str) -> str:
    try:
        payload = base64.b64decode(str(finalize_response).strip(), validate=True)
    except Exception as exc:
        raise ValueError("上传响应不是有效 base64") from exc
    offset = 0
    while offset < len(payload):
        tag, offset = _read_varint(payload, offset)
        field_number, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, offset = _read_varint(payload, offset)
            if field_number == 2:
                if not 0 < value <= MAX_PROTO_INT64:
                    raise ValueError("上传响应中的 Asset ID 无效")
                return str(value)
        elif wire_type == 1:
            offset += 8
        elif wire_type == 2:
            length, offset = _read_varint(payload, offset)
            offset += length
        elif wire_type == 5:
            offset += 4
        else:
            raise ValueError(f"上传响应包含不支持的 protobuf wire type：{wire_type}")
        if offset > len(payload):
            raise ValueError("上传响应 protobuf 已截断")
    raise ValueError("上传响应中没有 Asset ID")


def asset_folder_list_request(
    account_id: str | int,
    manager_id: str | int,
    *,
    offset: int = 0,
    limit: int = 50,
    asset_id: str | int | None = None,
) -> dict[str, Any]:
    account = str(_numeric_id(account_id, "account_id"))
    manager = str(_numeric_id(manager_id, "manager_id"))
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("素材分页要求 offset >= 0 且 limit 为 1..100")
    filters = []
    if asset_id is not None:
        asset = str(_numeric_id(asset_id, "asset_id"))
        filters.append({"1": "asset_id", "2": 3, "4": [{"3": asset}]})
    return {
        "1": {"3": {"1": account, "3": manager}},
        "2": {
            "1": list(ASSET_FOLDER_FIELDS),
            "2": filters,
            "3": [
                {"1": "folder_id", "2": 2},
                {"1": "last_ui_updated_timestamp_micros", "2": 2},
            ],
            "7": {"1": int(offset), "2": int(limit)},
            "14": True,
        },
        "3": [
            {"1": "enabledAssetTypes", "2": "(2)"},
            {"1": "enableSubfolderSearch", "2": "true"},
            {"1": "isAggregatedPolicyEnabled", "2": "true"},
            {"1": "isExtensionsPolicyEnabled", "2": "false"},
            {"1": "enableVideoEditability", "2": "false"},
            {"1": "enableTfaa", "2": "true"},
        ],
    }


def asset_folder_readback_request(
    account_id: str | int, manager_id: str | int, asset_id: str | int,
) -> dict[str, Any]:
    return asset_folder_list_request(
        account_id, manager_id, offset=0, limit=50, asset_id=asset_id,
    )


def parse_asset_folder_readback(
    payload: dict[str, Any], *, account_id: str | int,
) -> list[dict[str, Any]]:
    expected_account = str(_numeric_id(account_id, "account_id"))
    rows = payload.get("1") or [] if isinstance(payload, dict) else []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_account = str(row.get("1") or "")
        if row_account != expected_account:
            raise PermissionError(f"素材回读属于其他内部账户：{row_account}")
        asset_id = str(row.get("2") or "")
        if not asset_id.isdigit():
            continue
        result.append({
            "account_id": row_account,
            "asset_id": asset_id,
            "name": str(row.get("5") or ""),
            "asset_type_raw": int(row.get("8") or 0),
            "asset_status_raw": int(row.get("9") or 0),
            "file_size": int(row.get("14") or 0),
            "width": int(row.get("15") or 0),
            "height": int(row.get("16") or 0),
            "preview_url": str(row.get("24") or row.get("17") or ""),
            "aspect_ratio": float(row.get("62") or 0),
        })
    return result


_DOWNLOAD_PREVIEW_JS = r"""
async (args) => {
  try {
    const parsed = new URL(args.url);
    const allowed = args.hostSuffixes.some(
      (suffix) => parsed.hostname === suffix.slice(1) || parsed.hostname.endsWith(suffix)
    );
    if (parsed.protocol !== 'https:' || !allowed) {
      return {ok: false, error: '预览地址不属于允许的 Google 图片域名'};
    }
    const response = await fetch(parsed.href, {credentials: 'include', redirect: 'follow'});
    if (!response.ok) return {ok: false, status: response.status, error: `HTTP ${response.status}`};
    const blob = await response.blob();
    if (!String(blob.type || '').toLowerCase().startsWith('image/')) {
      return {ok: false, status: response.status, error: `响应不是图片：${blob.type || '未知类型'}`};
    }
    if (blob.size <= 0 || blob.size > args.maxBytes) {
      return {ok: false, status: response.status, error: `图片大小 ${blob.size} 超出允许范围`};
    }
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = '';
    const step = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += step) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + step));
    }
    return {
      ok: true, status: response.status, mimeType: blob.type,
      finalUrl: response.url, size: bytes.length, dataBase64: btoa(binary)
    };
  } catch (error) {
    return {ok: false, error: String(error)};
  }
}
"""


def download_preview_image(
    page: Any | None, preview_url: str, *, max_bytes: int = MAX_REFERENCE_IMAGE_BYTES,
) -> tuple[bytes, str, str]:
    """Download a protocol-resolved Google preview, with a CORS-safe fallback."""
    import urllib.request
    from urllib.parse import urlsplit

    def allowed_url(value: str) -> bool:
        candidate = urlsplit(str(value or "").strip())
        candidate_host = str(candidate.hostname or "").lower()
        return candidate.scheme == "https" and any(
            candidate_host == suffix[1:] or candidate_host.endswith(suffix)
            for suffix in REFERENCE_IMAGE_HOST_SUFFIXES
        )

    parsed = urlsplit(str(preview_url or "").strip())
    if not allowed_url(parsed.geturl()):
        raise ValueError("预览地址不属于允许的 Google 图片域名")
    if not 1 <= int(max_bytes) <= 25 * 1024 * 1024:
        raise ValueError("图片下载上限必须在 1 字节到 25 MB 之间")
    args = {
        "url": parsed.geturl(), "maxBytes": int(max_bytes),
        "hostSuffixes": list(REFERENCE_IMAGE_HOST_SUFFIXES),
    }
    page_error = ""
    try:
        if page is None:
            raise RuntimeError("跳过浏览器跨域下载")
        raw = page.evaluate(
            f"({_DOWNLOAD_PREVIEW_JS})({json.dumps(args, separators=(',', ':'))})",
            timeout=90.0,
        )
        response = raw if isinstance(raw, dict) else {}
        if response.get("ok"):
            try:
                content = base64.b64decode(
                    str(response.get("dataBase64") or ""), validate=True,
                )
            except (ValueError, binascii.Error) as exc:
                raise RuntimeError("图片下载结果不是有效 base64") from exc
            expected_size = int(response.get("size") or 0)
            if not content or len(content) != expected_size or len(content) > int(max_bytes):
                raise RuntimeError("图片下载大小校验失败")
            mime_type = str(response.get("mimeType") or "").split(";", 1)[0].lower()
            if not mime_type.startswith("image/"):
                raise RuntimeError("图片下载结果的 MIME 类型无效")
            final_url = str(response.get("finalUrl") or parsed.geturl())
            if not allowed_url(final_url):
                raise RuntimeError("图片下载被重定向到非 Google 图片域名")
            return content, mime_type, final_url
        page_error = str(response.get("error") or "浏览器页面下载返回格式无效")
    except Exception as exc:
        page_error = str(exc)

    request = urllib.request.Request(
        parsed.geturl(), headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as direct:  # noqa: S310
            final_url = str(direct.geturl() or parsed.geturl())
            if not allowed_url(final_url):
                raise RuntimeError("图片直连下载被重定向到非 Google 图片域名")
            mime_type = str(direct.headers.get_content_type() or "").lower()
            if not mime_type.startswith("image/"):
                raise RuntimeError(f"图片直连响应不是图片：{mime_type or '未知类型'}")
            declared = int(direct.headers.get("Content-Length") or 0)
            if declared > int(max_bytes):
                raise RuntimeError(f"图片大小 {declared} 超过下载上限")
            content = direct.read(int(max_bytes) + 1)
    except Exception as exc:
        raise RuntimeError(f"浏览器下载失败：{page_error}；直连下载失败：{exc}") from exc
    if not content or len(content) > int(max_bytes):
        raise RuntimeError("图片直连下载大小校验失败")
    return content, mime_type, final_url


def verify_uploaded_asset_readback(
    result: ImageUploadResult, rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not result.ok:
        raise ValueError("只有成功完成上传的图片才能回读校验")
    matches = [row for row in rows if row.get("asset_id") == result.asset_id]
    if len(matches) != 1:
        raise RuntimeError(f"Asset {result.asset_id} 回读数量不是 1")
    row = matches[0]
    if row.get("asset_type_raw") != 2:
        raise RuntimeError(f"Asset {result.asset_id} 回读类型不是图片")
    if row.get("asset_status_raw") not in {0, 1}:
        raise RuntimeError(f"Asset {result.asset_id} 回读状态不可用：{row.get('asset_status_raw')}")
    if row.get("width") != result.width or row.get("height") != result.height:
        raise RuntimeError(
            f"Asset {result.asset_id} 回读尺寸不一致："
            f"{row.get('width')}×{row.get('height')} != {result.width}×{result.height}"
        )
    if row.get("file_size") and row.get("file_size") != result.size:
        raise RuntimeError(
            f"Asset {result.asset_id} 回读文件大小不一致："
            f"{row.get('file_size')} != {result.size}"
        )
    return {**row, "verified": True}


_UPLOAD_JS = r"""
async (args) => {
  const retryable = (s) => s === 408 || s === 425 || s === 429 || (s >= 500 && s <= 504);
  const sleep = (n) => new Promise((resolve) => setTimeout(resolve, args.retryBaseMs * Math.pow(2, n - 1)));
  const timedFetch = async (url, options, timeoutMs = args.timeoutMs) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try { return await fetch(url, {...options, signal: controller.signal}); }
    finally { clearTimeout(timer); }
  };
  const fail = (errorCode, error, extra = {}) => ({ok: false, errorCode, error, ...extra});
  try {
    const binary = atob(args.dataBase64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    if (bytes.length !== args.size) return fail("BYTE_LENGTH_MISMATCH", "传输到页面的图片长度不一致");
    let uploadUrl = "", startStatus = 0, startAttempts = 0, startError = "";
    for (let attempt = 1; attempt <= args.maxAttempts; attempt++) {
      startAttempts = attempt;
      try {
        const response = await timedFetch("/ads/assets/upload/?authuser=0", {
          method: "POST", credentials: "include",
          headers: {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": String(bytes.length),
            "X-Goog-Upload-Header-Content-Type": args.mimeType,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"
          }, body: args.metadata
        });
        startStatus = response.status;
        uploadUrl = response.headers.get("x-goog-upload-url") || "";
        if (response.ok && uploadUrl) break;
        startError = `HTTP ${response.status}`;
        if (!retryable(response.status)) break;
      } catch (error) { startError = String(error); }
      if (attempt < args.maxAttempts) await sleep(attempt);
    }
    if (!uploadUrl) return fail("START_FAILED", `建立上传会话失败：${startError}`, {startStatus, startAttempts});
    const parsed = new URL(uploadUrl, location.origin);
    const host = parsed.hostname.toLowerCase();
    const allowed = parsed.protocol === "https:" && (
      host === "google.com" || host.endsWith(".google.com") ||
      host === "googleapis.com" || host.endsWith(".googleapis.com") ||
      host === "googleusercontent.com" || host.endsWith(".googleusercontent.com")
    );
    if (!allowed) return fail("INVALID_UPLOAD_URL", "Google 返回了非 Google HTTPS 上传地址");
    uploadUrl = parsed.href;
    let finalizeStatus = 0, finalizeAttempts = 0, finalizeError = "", offset = 0;
    for (let attempt = 1; attempt <= args.maxAttempts; attempt++) {
      finalizeAttempts = attempt;
      try {
        const response = await timedFetch(uploadUrl, {
          method: "POST", credentials: "include",
          headers: {
            "Content-Type": args.mimeType,
            "X-Goog-Upload-Offset": String(offset),
            "X-Goog-Upload-Command": offset === bytes.length ? "finalize" : "upload, finalize"
          }, body: offset === bytes.length ? new Uint8Array() : bytes.slice(offset)
        });
        finalizeStatus = response.status;
        const text = await response.text();
        if (response.ok) return {
          ok: true, response: text, startStatus, finalizeStatus, startAttempts,
          finalizeAttempts, uploadStatus: response.headers.get("x-goog-upload-status") || "final"
        };
        finalizeError = `HTTP ${response.status}`;
        if (!retryable(response.status)) break;
      } catch (error) {
        try {
          const query = await timedFetch(uploadUrl, {
            method: "POST", credentials: "include", headers: {"X-Goog-Upload-Command": "query"}
          }, 10000);
          const status = query.headers.get("x-goog-upload-status") || "";
          const received = Number(query.headers.get("x-goog-upload-size-received") || "0");
          if (status === "final") return fail("FINALIZE_OUTCOME_UNKNOWN", "上传已完成但响应丢失，为避免重复上传已停止", {startStatus, finalizeStatus, startAttempts, finalizeAttempts});
          if (!query.ok || status !== "active" || !Number.isSafeInteger(received) || received < 0 || received > bytes.length) {
            return fail("FINALIZE_OUTCOME_UNKNOWN", "响应丢失后无法确认上传会话状态", {startStatus, finalizeStatus, startAttempts, finalizeAttempts});
          }
          offset = received; finalizeError = String(error);
        } catch (queryError) {
          return fail("FINALIZE_OUTCOME_UNKNOWN", `查询上传会话失败：${String(queryError)}`, {startStatus, finalizeStatus, startAttempts, finalizeAttempts});
        }
      }
      if (attempt < args.maxAttempts) await sleep(attempt);
    }
    return fail("FINALIZE_FAILED", `图片数据上传失败：${finalizeError}`, {startStatus, finalizeStatus, startAttempts, finalizeAttempts});
  } catch (error) { return fail("UPLOAD_EXCEPTION", String(error)); }
}
"""


def upload_image(
    page: Any,
    path: str | os.PathLike[str],
    *,
    account_id: str | int,
    manager_id: str | int,
    max_attempts: int = 3,
) -> ImageUploadResult:
    info = validate_image(path)
    metadata = build_upload_metadata(info.filename, info.size, account_id, manager_id)
    encoded = base64.b64encode(Path(info.path).read_bytes()).decode("ascii")
    args = {
        "dataBase64": encoded,
        "size": info.size,
        "metadata": metadata,
        "mimeType": info.mime_type,
        "maxAttempts": max_attempts,
        "retryBaseMs": 250,
        "timeoutMs": 30_000,
    }
    try:
        raw = page.evaluate(
            f"({_UPLOAD_JS})({json.dumps(args, ensure_ascii=False, separators=(',', ':'))})",
            timeout=120.0,
        )
        response = raw if isinstance(raw, dict) else {"ok": False, "error": "页面返回格式无效"}
    except Exception as exc:
        response = {"ok": False, "errorCode": "PAGE_EVALUATION_FAILED", "error": str(exc)}
    common = {
        "filename": info.filename, "size": info.size, "width": info.width,
        "height": info.height, "content_sha256": info.content_sha256,
        "start_status": int(response.get("startStatus") or 0),
        "finalize_status": int(response.get("finalizeStatus") or 0),
        "upload_status": str(response.get("uploadStatus") or ""),
        "start_attempts": int(response.get("startAttempts") or 0),
        "finalize_attempts": int(response.get("finalizeAttempts") or 0),
    }
    if not response.get("ok"):
        return ImageUploadResult(
            ok=False, asset_id="", error=str(response.get("error") or "图片上传失败"),
            error_code=str(response.get("errorCode") or "UPLOAD_FAILED"), **common,
        )
    try:
        asset_id = extract_asset_id(str(response.get("response") or ""))
    except ValueError as exc:
        return ImageUploadResult(ok=False, asset_id="", error=str(exc), error_code="ASSET_ID_PARSE_FAILED", **common)
    return ImageUploadResult(ok=True, asset_id=asset_id, **common)


def upload_images(
    page: Any,
    paths: Iterable[str | os.PathLike[str]],
    *,
    account_id: str | int,
    manager_id: str | int,
) -> list[ImageUploadResult]:
    results: list[ImageUploadResult] = []
    completed: dict[str, ImageUploadResult] = {}
    for path in paths:
        info = validate_image(path)
        key = f"{info.size}:{info.content_sha256}"
        if key in completed:
            result = replace(completed[key], filename=info.filename, deduplicated=True)
        else:
            result = upload_image(page, info.path, account_id=account_id, manager_id=manager_id)
            if result.ok:
                completed[key] = result
        results.append(result)
        if not result.ok:
            break
    return results
