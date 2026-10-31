from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


_API_KEY_CACHE: str | None = None


def _key_file_candidates() -> list[Path]:
    """Locations where the user may drop an ``adspower.txt`` API-key file."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "adspower.txt")
    candidates.append(Path(__file__).resolve().parent.parent / "adspower.txt")
    candidates.append(Path.cwd() / "adspower.txt")
    return candidates


def _local_api_base() -> str:
    """Base URL the running AdsPower client actually listens on.

    AdsPower Global writes the live Local API address (the port changes
    between client launches) to ``cwd_global/source/local_api``. The file
    may carry a ``local.adspower.com`` host, which can resolve to a proxy
    fake-ip, so we always talk to 127.0.0.1 with the advertised port.
    """
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return ""
    pointer = Path(appdata) / "adspower_global" / "cwd_global" / "source" / "local_api"
    try:
        raw = pointer.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError, UnicodeDecodeError):
        return ""
    port = urllib.parse.urlsplit(raw).port
    if port:
        return f"http://127.0.0.1:{port}"
    return ""


def _api_base_candidates() -> list[str]:
    candidates: list[str] = []
    env_base = os.environ.get("ADSPOWER_API_BASE", "").strip().rstrip("/")
    if env_base:
        candidates.append(env_base)
    discovered = _local_api_base()
    if discovered:
        candidates.append(discovered)
    candidates.extend(["http://local.adspower.net:50325", "http://127.0.0.1:50325"])
    seen: set[str] = set()
    ordered: list[str] = []
    for base in candidates:
        if base not in seen:
            seen.add(base)
            ordered.append(base)
    return ordered


def api_key() -> str:
    """AdsPower Local API key from ADSPOWER_API_KEY or a local adspower.txt."""
    global _API_KEY_CACHE
    if _API_KEY_CACHE is not None:
        return _API_KEY_CACHE
    key = os.environ.get("ADSPOWER_API_KEY", "").strip()
    if not key:
        for path in _key_file_candidates():
            try:
                if path.is_file():
                    key = path.read_text(encoding="utf-8").splitlines()[0].strip()
                    if key:
                        break
            except (OSError, IndexError, UnicodeDecodeError):
                continue
    _API_KEY_CACHE = key
    return key


def _valid_cached_cdp(profile_id: str) -> str | None:
    if re.fullmatch(r"[A-Za-z0-9_-]+", profile_id) is None:
        return None
    roots = []
    if os.environ.get("ADSPOWER_CACHE_DIR"):
        roots.append(Path(os.environ["ADSPOWER_CACHE_DIR"]))
    roots.append(Path("C:/.ADSPOWER_GLOBAL/cache"))
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(path for path in root.glob(f"{profile_id}_*") if path.is_dir())
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for folder in candidates:
        try:
            port = int((folder / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0])
            endpoint = f"http://127.0.0.1:{port}"
            with urllib.request.urlopen(endpoint + "/json/list", timeout=1.5) as response:  # noqa: S310
                targets = json.loads(response.read().decode("utf-8", "replace"))
            for target in targets:
                url = urllib.parse.urlsplit(str(target.get("url") or ""))
                query = urllib.parse.parse_qs(url.query)
                if url.hostname == "start.adspower.net" and (query.get("id") or [""])[0] == profile_id:
                    return endpoint
        except (OSError, ValueError, IndexError, json.JSONDecodeError):
            continue
    return None


def _api(path: str, params: dict[str, str], timeout: float = 10.0) -> dict:
    query = urllib.parse.urlencode(params)
    headers: dict[str, str] = {}
    key = api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    errors: list[str] = []
    for base in _api_base_candidates():
        try:
            request = urllib.request.Request(f"{base}{path}?{query}", headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:
            errors.append(str(exc))
    detail = "; ".join(errors)
    if any(token in detail for token in ("502", "10061", "Connection refused", "无法连接")):
        raise RuntimeError(
            "AdsPower Local API 未启动或不可访问。"
            "请在 AdsPower 的“API & MCP”页面启用 Local API 后重试。"
        )
    if "401" in detail or "403" in detail or "Unauthorized" in detail:
        raise RuntimeError(
            "AdsPower Local API 拒绝了 API Key 认证。"
            "请核对 adspower.txt（或 ADSPOWER_API_KEY）中的 key 与“API & MCP”页面一致。"
        )
    raise RuntimeError("AdsPower 本地 API 不可用: " + detail)


def _serial_number(row: dict) -> str:
    """Handle field-name differences between AdsPower desktop releases."""
    for key in ("serial_number", "serialNumber", "serial_no", "serial", "number"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def query_profile_info(profile_id: str, *, timeout: float = 2.0) -> dict[str, str]:
    """Return the real AdsPower profile serial number and display name."""
    profile_id = profile_id.strip()
    if not profile_id:
        raise ValueError("AdsPower 窗口 ID 不能为空")
    response = _api(
        "/api/v1/user/list",
        {"user_id": profile_id, "page": "1", "page_size": "100"},
        timeout=timeout,
    )
    if response.get("code") != 0:
        raise RuntimeError(f"AdsPower 查询窗口资料失败: {response.get('msg') or '未知错误'}")
    rows = (response.get("data") or {}).get("list") or []
    row = next((item for item in rows if str(item.get("user_id") or "") == profile_id), None)
    if not row:
        raise RuntimeError(f"AdsPower 中找不到窗口 ID：{profile_id}")
    return {
        "profile_id": profile_id,
        "serial_number": _serial_number(row),
        "name": str(row.get("name") or "").strip(),
    }


def resolve_cdp(profile_id: str, *, start: bool = True) -> str:
    profile_id = profile_id.strip()
    if not profile_id:
        raise ValueError("AdsPower 窗口 ID 不能为空")
    cached = _valid_cached_cdp(profile_id)
    if cached:
        return cached
    active = _api("/api/v1/browser/active", {"user_id": profile_id})
    data = active.get("data") or {}
    if active.get("code") == 0 and data.get("status") == "Active":
        return _endpoint_from_data(data)
    if not start:
        raise RuntimeError(f"AdsPower 窗口 {profile_id} 未打开")
    time.sleep(1.2)
    opened = _api("/api/v1/browser/start", {"user_id": profile_id, "open_tabs": "1"})
    if opened.get("code") != 0:
        raise RuntimeError(f"AdsPower 启动失败: {opened.get('msg')}")
    return _endpoint_from_data(opened.get("data") or {})


def _endpoint_from_data(data: dict) -> str:
    if data.get("debug_port"):
        return f"http://127.0.0.1:{data['debug_port']}"
    ws = data.get("ws") or {}
    if ws.get("puppeteer"):
        return str(ws["puppeteer"])
    raise RuntimeError("AdsPower 没有返回 CDP 调试端口")


def list_google_ads_targets(cdp_endpoint: str) -> list[dict]:
    if cdp_endpoint.startswith("ws"):
        raise RuntimeError("当前 AdsPower 只返回浏览器 WebSocket；请升级或打开 debug_port")
    with urllib.request.urlopen(cdp_endpoint.rstrip("/") + "/json/list", timeout=8) as response:  # noqa: S310
        targets = json.loads(response.read().decode("utf-8", "replace"))
    return [
        target for target in targets
        if target.get("type") == "page"
        and target.get("webSocketDebuggerUrl")
        and urllib.parse.urlsplit(str(target.get("url") or "")).hostname == "ads.google.com"
    ]


def ensure_google_ads_targets(
    cdp_endpoint: str,
    *,
    wait_seconds: float = 15.0,
    poll_interval: float = 0.5,
) -> list[dict]:
    """Return Google Ads pages, opening the account overview when none exists.

    This uses Chromium's CDP target endpoint.  It does not click the AdsPower UI
    or send a Google Ads mutate request.  The new tab is intentionally kept open
    so subsequent protocol operations can reuse the authenticated session.
    """
    targets = list_google_ads_targets(cdp_endpoint)
    if targets:
        return targets
    open_cdp_target(cdp_endpoint, "https://ads.google.com/aw/overview")
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        targets = list_google_ads_targets(cdp_endpoint)
        if any(_google_ads_target_has_account_scope(target) for target in targets):
            return targets
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.05, float(poll_interval)))
    raise RuntimeError(
        "已自动打开 Google Ads，但尚未进入账户页面。"
        "请在该 AdsPower 窗口完成 Google 登录、身份验证或选择账户后重试。"
    )


def _google_ads_target_has_account_scope(target: dict) -> bool:
    parsed = urllib.parse.urlsplit(str(target.get("url") or ""))
    query = urllib.parse.parse_qs(parsed.query)
    ocid = (query.get("ocid") or query.get("ascid") or [""])[0]
    scope = (query.get("uscid") or [ocid])[0]
    return str(ocid).isdigit() and str(scope).isdigit()


def open_cdp_target(cdp_endpoint: str, url: str) -> dict:
    """Open one temporary browser target through Chromium's read-only CDP workflow."""
    endpoint = cdp_endpoint.rstrip("/") + "/json/new?" + urllib.parse.quote(url, safe="")
    request = urllib.request.Request(endpoint, method="PUT")
    with urllib.request.urlopen(request, timeout=12) as response:  # noqa: S310
        target = json.loads(response.read().decode("utf-8", "replace"))
    if not target.get("id") or not target.get("webSocketDebuggerUrl"):
        raise RuntimeError("AdsPower 未能创建临时只读页面")
    return target


def close_cdp_target(cdp_endpoint: str, target_id: str) -> None:
    """Close a temporary Chromium target without touching the user's existing tabs."""
    if not target_id:
        return
    endpoint = cdp_endpoint.rstrip("/") + "/json/close/" + urllib.parse.quote(target_id, safe="")
    try:
        with urllib.request.urlopen(endpoint, timeout=8):  # noqa: S310
            pass
    except Exception:
        pass
