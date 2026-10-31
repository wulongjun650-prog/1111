"""Offline-testable client for the BitBrowser local HTTP API."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_API_BASE = "http://127.0.0.1:54345"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def settings_path() -> Path:
    root = Path(os.environ.get("APPDATA") or Path.home()) / "GGAdsProtocolTool" / "local-config"
    root.mkdir(parents=True, exist_ok=True)
    return root / "bitbrowser.json"


def normalize_api_base(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        raise ValueError("比特浏览器 Local API 地址格式无效")
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("比特浏览器 Local API 必须使用本机 HTTP 地址")
    valid = not (parsed.username or parsed.password or parsed.query or parsed.fragment)
    try:
        valid = valid and (parsed.port is None or 1 <= parsed.port <= 65535)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("比特浏览器 Local API 地址格式无效")
    return text


def load_api_base() -> str:
    environment = os.environ.get("BITBROWSER_API_BASE", "").strip()
    if environment:
        return normalize_api_base(environment)
    try:
        value = json.loads(settings_path().read_text(encoding="utf-8"))
        return normalize_api_base(str(value.get("api_base") or DEFAULT_API_BASE))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return DEFAULT_API_BASE


def save_api_base(value: str) -> Path:
    normalized = normalize_api_base(value)
    target = settings_path()
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"api_base": normalized}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def _api(path: str, payload: dict[str, Any] | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
    request = urllib.request.Request(
        load_api_base() + path,
        data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - base is loopback-only
            result = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"比特浏览器 Local API 不可用：{exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("比特浏览器 Local API 返回格式无效")
    if result.get("success") is False:
        raise RuntimeError(str(result.get("msg") or "比特浏览器请求失败"))
    return result


def health(*, timeout: float = 2.0) -> None:
    response = _api("/health", timeout=timeout)
    if response and response.get("success") is not True:
        raise RuntimeError("比特浏览器健康检查响应格式无效")


def running_pids(*, timeout: float = 5.0) -> dict[str, int]:
    response = _api("/browser/pids/all", timeout=timeout)
    if "data" in response:
        raw = response["data"]
        if not isinstance(raw, dict):
            raise RuntimeError("比特浏览器运行窗口响应格式无效")
    else:
        raw = response
    if not isinstance(raw, dict):
        raise RuntimeError("比特浏览器运行窗口响应格式无效")
    result: dict[str, int] = {}
    for profile_id, pid in raw.items():
        profile_text = str(profile_id).strip()
        if profile_text in {"", "success", "msg", "data"}:
            continue
        try:
            pid_text = str(pid).strip()
            if not pid_text or not pid_text.isdigit():
                continue
            result[profile_text] = int(pid_text)
        except (TypeError, ValueError):
            continue
    return result


def list_profiles(*, timeout: float = 10.0) -> list[dict[str, Any]]:
    active = running_pids(timeout=timeout)
    rows: list[dict[str, Any]] = []
    seen_profile_ids: set[str] = set()
    for page in range(10000):
        response = _api("/browser/list", {"page": page, "pageSize": 100}, timeout=timeout)
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise RuntimeError("比特浏览器窗口列表响应格式无效")
        page_rows = data["list"]
        normalized_page: list[tuple[str, dict[str, Any]]] = []
        for item in page_rows:
            if not isinstance(item, dict):
                continue
            profile_id = str(item.get("id") or "").strip()
            if not profile_id:
                continue
            normalized_page.append((profile_id, item))
        page_ids = [profile_id for profile_id, _item in normalized_page]
        if (
            (page_rows and not page_ids)
            or len(page_ids) != len(set(page_ids))
            or not seen_profile_ids.isdisjoint(page_ids)
        ):
            raise RuntimeError("比特浏览器窗口列表分页响应不完整或重复返回窗口 ID")
        for profile_id, item in normalized_page:
            rows.append(
                {
                    "profile_id": profile_id,
                    "serial_number": str(item.get("seq") or ""),
                    "name": str(item.get("name") or "").strip(),
                    "remark": str(item.get("remark") or "").strip(),
                    "running": profile_id in active,
                    "source": "bitbrowser",
                }
            )
        seen_profile_ids.update(page_ids)
        try:
            total = int(data.get("totalNum") or data.get("total") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("比特浏览器窗口列表总数格式无效") from exc
        if total < 0:
            raise RuntimeError("比特浏览器窗口列表总数格式无效")
        if (total and len(rows) >= total) or (not total and len(page_rows) < 100):
            return rows
        if not page_rows:
            raise RuntimeError("比特浏览器窗口列表分页响应不完整")
    raise RuntimeError("比特浏览器窗口分页超过安全上限")


def query_profile_info(profile_id: str, *, timeout: float = 2.0) -> dict[str, str]:
    target = str(profile_id or "").strip()
    if not target:
        raise RuntimeError(f"比特浏览器中找不到窗口 ID：{target}")
    row = next((item for item in list_profiles(timeout=timeout) if item["profile_id"] == target), None)
    if row is None:
        raise RuntimeError(f"比特浏览器中找不到窗口 ID：{target}")
    return {key: str(row.get(key) or "") for key in ("profile_id", "serial_number", "name", "source")}


def normalize_cdp_endpoint(value: str) -> str:
    text = str(value or "").strip()
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urllib.parse.urlsplit(text)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in _LOOPBACK_HOSTS
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
        port = parsed.port
    except ValueError:
        valid = False
        port = None
    if not valid or port is None:
        raise ValueError("比特浏览器返回了非本机 CDP 地址")
    return f"http://127.0.0.1:{port}"


def resolve_cdp(profile_id: str, *, start: bool = True, timeout: float = 30.0) -> str:
    target = str(profile_id or "").strip()
    if not target:
        raise RuntimeError("比特浏览器窗口 ID 不能为空")
    if not start and target not in running_pids(timeout=min(timeout, 5.0)):
        raise RuntimeError("比特浏览器窗口尚未启动")
    response = _api("/browser/open", {"id": target}, timeout=timeout)
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("比特浏览器没有返回可用的 CDP 地址")
    raw_http = str(data.get("http") or "").strip()
    try:
        return normalize_cdp_endpoint(raw_http)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
