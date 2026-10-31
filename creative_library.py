"""Local reusable image and ad-copy plans."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import local_config_dir


DEFAULT_CREATIVE_LIBRARY: dict[str, Any] = {
    "version": 2,
    "image_plans": {},
    "copy_plans": {},
}
IMAGE_ROLES = ("landscape", "square", "portrait")


def _migrate_image_roles(images: dict[str, Any]) -> dict[str, list[str]]:
    """Normalize v1's mislabeled logo bucket into real image roles."""
    from .image_upload import validate_role_image

    normalized = {role: list(images.get(role) or []) for role in IMAGE_ROLES}
    for raw_path in images.get("logo") or []:
        path = str(raw_path)
        destination = "square"
        try:
            validate_role_image(path, "portrait")
            destination = "portrait"
        except Exception:
            # v1 only accepted 1:1 files as "logo"; those are square marketing
            # images, not protocol field 20 portrait images.
            pass
        if path not in normalized[destination]:
            normalized[destination].append(path)
    return normalized


def _migrate_library_in_place(data: dict[str, Any]) -> None:
    image_plans = data.get("image_plans")
    if isinstance(image_plans, dict):
        for plan in image_plans.values():
            if isinstance(plan, dict) and isinstance(plan.get("images"), dict):
                plan["images"] = _migrate_image_roles(plan["images"])
    data["version"] = 2


def creative_library_path() -> Path:
    return local_config_dir() / "ad-creative-library.json"


def creative_assets_dir() -> Path:
    target = local_config_dir() / "creative-assets"
    target.mkdir(parents=True, exist_ok=True)
    return target


def import_image_files(
    images: dict[str, list[str]], *, target_dir: Path | None = None,
) -> dict[str, list[str]]:
    """Copy image files into content-addressed local managed storage."""
    normalized = _migrate_image_roles(images)
    result = {role: [] for role in IMAGE_ROLES}
    target_dir = target_dir or creative_assets_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    for role in result:
        for raw_path in normalized.get(role) or []:
            source = Path(raw_path).expanduser().resolve()
            if not source.is_file():
                raise ValueError(f"图片不存在：{source}")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target = target_dir / f"{digest}{source.suffix.lower()}"
            if not target.exists():
                shutil.copy2(source, target)
            stored = str(target.resolve())
            if stored not in result[role]:
                result[role].append(stored)
    return result


def _lines(value: Any, label: str, maximum: int, limit: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label}必须是列表")
    result: list[str] = []
    for raw in value:
        text = str(raw).strip()
        if not text:
            continue
        if len(text) > limit:
            raise ValueError(f"{label}单条最多 {limit} 个字符")
        if text not in result:
            result.append(text)
    if len(result) > maximum:
        raise ValueError(f"{label}最多 {maximum} 条")
    return result


def validate_creative_library(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("广告素材库必须是 JSON 对象")
    _migrate_library_in_place(data)
    for section in ("image_plans", "copy_plans"):
        if not isinstance(data.get(section), dict):
            raise ValueError(f"广告素材库缺少 {section}")
    for name, plan in data["image_plans"].items():
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError("图片方案名称不能为空且最多 120 个字符")
        if not isinstance(plan, dict):
            raise ValueError(f"图片方案 {name} 格式无效")
        images = plan.get("images") or {}
        if not isinstance(images, dict):
            raise ValueError(f"图片方案 {name} 的 images 格式无效")
        for role in IMAGE_ROLES:
            paths = images.get(role) or []
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                raise ValueError(f"图片方案 {name} 的 {role} 必须是路径列表")
    for name, plan in data["copy_plans"].items():
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError("文案方案名称不能为空且最多 120 个字符")
        if not isinstance(plan, dict):
            raise ValueError(f"文案方案 {name} 格式无效")
        _lines(plan.get("headlines") or [], f"文案方案 {name} 的短标题", 5, 30)
        _lines(plan.get("descriptions") or [], f"文案方案 {name} 的描述", 5, 90)
        if len(str(plan.get("business_name") or "")) > 25:
            raise ValueError(f"文案方案 {name} 的商家名称最多 25 个字符")
        if len(str(plan.get("long_headline") or "")) > 90:
            raise ValueError(f"文案方案 {name} 的长标题最多 90 个字符")


def load_creative_library(path: Path | None = None) -> dict[str, Any]:
    target = path or creative_library_path()
    if not target.exists():
        return copy.deepcopy(DEFAULT_CREATIVE_LIBRARY)
    with target.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    _migrate_library_in_place(data)
    validate_creative_library(data)
    return data


def save_creative_library(data: dict[str, Any], path: Path | None = None) -> Path:
    validate_creative_library(data)
    target = path or creative_library_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def put_plan(
    data: dict[str, Any],
    kind: str,
    name: str,
    value: dict[str, Any],
    *,
    old_name: str = "",
) -> None:
    section = {"image": "image_plans", "copy": "copy_plans"}.get(kind)
    if not section:
        raise ValueError(f"未知素材方案类型：{kind}")
    clean_name = str(name).strip()
    if not clean_name:
        raise ValueError("方案名称不能为空")
    plans = data[section]
    if clean_name != old_name and clean_name in plans:
        raise ValueError(f"方案名称已存在：{clean_name}")
    previous = plans.get(old_name) if old_name else None
    created_at = str((previous or {}).get("created_at") or timestamp())
    payload = copy.deepcopy(value)
    payload["created_at"] = created_at
    payload["updated_at"] = timestamp()
    if old_name and old_name != clean_name:
        plans.pop(old_name, None)
    plans[clean_name] = payload
    validate_creative_library(data)


def delete_plan(data: dict[str, Any], kind: str, name: str) -> None:
    section = {"image": "image_plans", "copy": "copy_plans"}.get(kind)
    if not section:
        raise ValueError(f"未知素材方案类型：{kind}")
    if name not in data[section]:
        raise ValueError(f"找不到方案：{name}")
    del data[section][name]


def _unique_name(plans: dict[str, Any], requested: str) -> str:
    if requested not in plans:
        return requested
    index = 2
    while f"{requested} ({index})" in plans:
        index += 1
    return f"{requested} ({index})"


def import_creative_template(
    data: dict[str, Any], template_path: str | Path,
    *, managed_dir: Path | None = None,
) -> list[dict[str, str]]:
    """Merge a JSON template, resolving image paths relative to the JSON file."""
    from .image_upload import validate_role_image

    source_path = Path(template_path).expanduser().resolve()
    with source_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("素材模板必须是 JSON 对象")
    if "library" in payload:
        payload = payload["library"]
    if not isinstance(payload, dict):
        raise ValueError("素材模板 library 格式无效")
    image_plans = payload.get("image_plans") or {}
    copy_plans = payload.get("copy_plans") or {}
    if not isinstance(image_plans, dict) or not isinstance(copy_plans, dict):
        raise ValueError("模板必须包含 image_plans/copy_plans 对象")
    if not image_plans and not copy_plans:
        raise ValueError("模板中没有图片方案或文案方案")

    working = copy.deepcopy(data)
    imported: list[dict[str, str]] = []
    for requested, raw_item in image_plans.items():
        if not isinstance(raw_item, dict):
            raise ValueError(f"图片方案 {requested} 格式无效")
        resolved = {role: [] for role in IMAGE_ROLES}
        images = raw_item.get("images") or {}
        if not isinstance(images, dict):
            raise ValueError(f"图片方案 {requested} 的 images 必须是对象")
        for role in resolved:
            raw_paths = images.get(role) or []
            if not isinstance(raw_paths, list):
                raise ValueError(f"图片方案 {requested} 的 {role} 必须是路径列表")
            for raw_path in raw_paths:
                candidate = Path(str(raw_path)).expanduser()
                if not candidate.is_absolute():
                    candidate = source_path.parent / candidate
                resolved[role].append(validate_role_image(candidate.resolve(), role).path)
        for raw_path in images.get("logo") or []:
            candidate = Path(str(raw_path)).expanduser()
            if not candidate.is_absolute():
                candidate = source_path.parent / candidate
            candidate = candidate.resolve()
            try:
                resolved["portrait"].append(
                    validate_role_image(candidate, "portrait").path
                )
            except Exception:
                resolved["square"].append(validate_role_image(candidate, "square").path)
        if not any(resolved.values()):
            raise ValueError(f"图片方案 {requested} 没有图片")
        final_name = _unique_name(working["image_plans"], str(requested).strip())
        put_plan(
            working, "image", final_name,
            {
                "note": str(raw_item.get("note") or "模板导入"),
                "images": import_image_files(resolved, target_dir=managed_dir),
            },
        )
        imported.append({"kind": "image", "source_name": str(requested), "name": final_name})
    for requested, raw_item in copy_plans.items():
        if not isinstance(raw_item, dict):
            raise ValueError(f"文案方案 {requested} 格式无效")
        raw_headlines = raw_item.get("headlines") or []
        raw_descriptions = raw_item.get("descriptions") or []
        if not isinstance(raw_headlines, list) or not isinstance(raw_descriptions, list):
            raise ValueError(f"文案方案 {requested} 的标题和描述必须是列表")
        final_name = _unique_name(working["copy_plans"], str(requested).strip())
        value = {
            "note": str(raw_item.get("note") or "模板导入"),
            "final_url": str(raw_item.get("final_url") or ""),
            "business_name": str(raw_item.get("business_name") or ""),
            "long_headline": str(raw_item.get("long_headline") or ""),
            "headlines": list(raw_headlines),
            "descriptions": list(raw_descriptions),
        }
        if not any(value.get(key) for key in (
            "final_url", "business_name", "long_headline", "headlines", "descriptions"
        )):
            raise ValueError(f"文案方案 {requested} 没有广告内容")
        put_plan(working, "copy", final_name, value)
        imported.append({"kind": "copy", "source_name": str(requested), "name": final_name})
    validate_creative_library(working)
    data.clear()
    data.update(working)
    return imported


def export_creative_template(
    data: dict[str, Any], template_path: str | Path,
    *, selection: tuple[str, str] | None = None,
) -> Path:
    """Export portable JSON plus a sibling directory containing image files."""
    validate_creative_library(data)
    target = Path(template_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    selected = {"image_plans": {}, "copy_plans": {}}
    if selection:
        kind, name = selection
        section = {"image": "image_plans", "copy": "copy_plans"}.get(kind)
        if not section or name not in data[section]:
            raise ValueError("找不到要导出的素材方案")
        selected[section][name] = copy.deepcopy(data[section][name])
    else:
        selected = {
            "image_plans": copy.deepcopy(data["image_plans"]),
            "copy_plans": copy.deepcopy(data["copy_plans"]),
        }
    if not selected["image_plans"] and not selected["copy_plans"]:
        raise ValueError("素材库中没有可导出的方案")
    asset_dir = target.parent / f"{target.stem}-assets"
    for item in selected["image_plans"].values():
        for role, paths in (item.get("images") or {}).items():
            exported_paths = []
            for raw_path in paths or []:
                source = Path(raw_path).expanduser().resolve()
                if not source.is_file():
                    raise ValueError(f"导出图片不存在：{source}")
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                asset_dir.mkdir(parents=True, exist_ok=True)
                destination = asset_dir / f"{digest}{source.suffix.lower()}"
                if not destination.exists():
                    shutil.copy2(source, destination)
                exported_paths.append(destination.relative_to(target.parent).as_posix())
            item["images"][role] = exported_paths
    payload = {
        "template_type": "GGAdsAdCreativeLibrary",
        "version": 1,
        "library": selected,
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target
