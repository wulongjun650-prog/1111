"""Offline-testable client for the BitBrowser local HTTP API.

BitBrowser 7.1.5 still defaults to ``http://127.0.0.1:54345``, but the live
port is whatever the client wrote to ``%APPDATA%\\bitbrowser\\config.json``
(``localServerAddress``). That build can also require an ``x-api-key`` header
after Local API authentication is turned on. A closed default port is
WinError 10061; a black-holed one surfaces as a timeout.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_API_BASE = "http://127.0.0.1:54345"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_PROBE_TIMEOUT = 1.5
_ADDRESS_KEYS = {"localserveraddress", "localapiurl", "localapiaddress"}
_EXPLICIT_PORT_KEYS = {"localapiport", "localserverport"}
_PORT_PARENTS = {"localapi", "localserver", "local_api"}
_TOKEN_KEYS = {"localapitoken", "x-api-key", "x_api_key"}
_TOKEN_PARENTS = {"localapi", "localserver", "local_api"}
_AUTH_MARKERS = (
    "x-api-key",
    "api-key",
    "api key",
    "unauthorized",
    "鉴权",
    "认证",
    "api token",
    "localapitoken",
)
_AUTH_MESSAGE = (
    "比特浏览器 7.1.5 开启了 Local API 鉴权。"
    "请打开比特浏览器的「系统设置 → Local API」，复制 API Token，"
    "填到本软件的「比特 Local API 设置」后再试。"
)

_working_base: str | None = None


class _Unreachable(Exception):
    def __init__(self, reason: BaseException) -> None:
        super().__init__(str(reason))
        self.reason = reason


class _AuthRejected(Exception):
    pass


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


def _read_settings() -> dict[str, Any]:
    try:
        value = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _saved_api_base() -> str | None:
    raw = _read_settings().get("api_base")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return normalize_api_base(raw)
    except ValueError:
        return None


def load_api_base() -> str:
    environment = os.environ.get("BITBROWSER_API_BASE", "").strip()
    if environment:
        return normalize_api_base(environment)
    saved = _saved_api_base()
    if saved:
        return saved
    discovered = _discovered_bases()
    if discovered:
        return discovered[0]
    return DEFAULT_API_BASE


def load_api_token() -> str:
    environment = os.environ.get("BITBROWSER_API_KEY", "").strip()
    if environment:
        return environment
    saved = _read_settings()
    if "api_token" in saved:
        return str(saved.get("api_token") or "").strip()
    return _discovered_token()


def _normalize_token(value: str) -> str:
    token = str(value or "").strip()
    if any(char in token for char in "\r\n"):
        raise ValueError("API Token 不能包含换行")
    if len(token) > 256:
        raise ValueError("API Token 过长")
    return token


def _clear_working_base() -> None:
    global _working_base
    _working_base = None


def save_local_api(api_base: str, api_token: str = "") -> Path:
    normalized = normalize_api_base(api_base)
    token = _normalize_token(api_token)
    target = settings_path()
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"api_base": normalized, "api_token": token}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
    _clear_working_base()
    return target


def save_api_base(value: str) -> Path:
    return save_local_api(value, load_api_token() if "api_token" in _read_settings() else "")


def _config_candidates() -> list[Path]:
    override = os.environ.get("BITBROWSER_CONFIG", "").strip()
    if override:
        return [Path(override)]
    names = ("bitbrowser", "BitBrowser", "bit-browser")
    roots: list[Path] = []
    for key in ("APPDATA", "LOCALAPPDATA"):
        raw = os.environ.get(key, "").strip()
        if raw:
            roots.append(Path(raw))
    home = Path.home()
    roots.append(home / "AppData" / "Roaming")
    roots.append(home / "Library" / "Application Support")
    roots.append(home / ".config")
    paths: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for name in names:
            for relative in (
                Path(name) / "config.json",
                Path(name) / "config" / "config.json",
                Path(name) / "local_api",
            ):
                path = root / relative
                if path not in seen:
                    seen.add(path)
                    paths.append(path)
    return paths


def _port_from_text(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 65535 else None
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text:
        return None
    if text.isdigit():
        port = int(text)
        return port if 1 <= port <= 65535 else None
    candidate = text if "://" in text else f"http://{text}"
    try:
        port = urllib.parse.urlsplit(candidate).port
    except ValueError:
        return None
    if port is None or not 1 <= port <= 65535:
        return None
    return port


def _base_from_port(value: object) -> str | None:
    port = _port_from_text(value)
    if port is None:
        return None
    return f"http://127.0.0.1:{port}"


def _consider_token(name: str, parent: str, value: object, tokens: list[str]) -> None:
    if not isinstance(value, str):
        return
    specific = name in _TOKEN_KEYS or (name in {"apitoken", "token"} and parent in _TOKEN_PARENTS)
    if not specific:
        return
    token = value.strip()
    if token and len(token) <= 256 and "\n" not in token and "\r" not in token:
        tokens.append(token)


def _collect_config(
    obj: object,
    bases: list[str],
    tokens: list[str],
    *,
    parent: str = "",
    depth: int = 0,
) -> None:
    if depth > 4:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            name = str(key).strip().lower()
            if name in _ADDRESS_KEYS:
                base = _base_from_port(value)
                if base:
                    bases.append(base)
            elif name in _EXPLICIT_PORT_KEYS or (name == "port" and parent in _PORT_PARENTS):
                base = _base_from_port(value)
                if base:
                    bases.append(base)
            _consider_token(name, parent, value, tokens)
            if isinstance(value, (dict, list)):
                _collect_config(value, bases, tokens, parent=name, depth=depth + 1)
        return
    if isinstance(obj, list) and depth < 2:
        for item in obj[:20]:
            if isinstance(item, (dict, list)):
                _collect_config(item, bases, tokens, parent=parent, depth=depth + 1)


def _read_config_values() -> tuple[list[str], list[str]]:
    bases: list[str] = []
    tokens: list[str] = []
    for path in _config_candidates():
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        text = raw.strip()
        if not text:
            continue
        if text[:1] in "{[":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            _collect_config(parsed, bases, tokens)
        else:
            base = _base_from_port(text.splitlines()[0])
            if base:
                bases.append(base)
        if bases or tokens:
            break
    return bases, tokens


def _discovered_bases() -> list[str]:
    bases, _tokens = _read_config_values()
    unique: list[str] = []
    for base in bases:
        if base not in unique:
            unique.append(base)
    return unique


def _discovered_token() -> str:
    _bases, tokens = _read_config_values()
    return tokens[0] if tokens else ""


def _variants(base: str) -> list[str]:
    try:
        port = urllib.parse.urlsplit(base).port
    except ValueError:
        return [base]
    if port is None:
        return [base]
    return [f"http://{host}:{port}" for host in ("127.0.0.1", "[::1]", "localhost")]


def api_base_candidates() -> list[str]:
    """Loopback addresses to try, preferred address first.

    An explicit environment address is used alone. A saved non-default
    address wins over the config file. Otherwise the port BitBrowser wrote
    to disk is tried before the historical 54345 default, because 7.1.5
    keeps the live port in that file.
    """
    ordered: list[str] = []

    def add(bases: list[str]) -> None:
        for base in bases:
            for variant in _variants(base):
                if variant not in ordered:
                    ordered.append(variant)

    environment = os.environ.get("BITBROWSER_API_BASE", "").strip()
    if environment:
        add([normalize_api_base(environment)])
        return ordered
    saved = _saved_api_base()
    discovered = _discovered_bases()
    if saved and saved != DEFAULT_API_BASE:
        add([saved])
        add(discovered)
    else:
        add(discovered)
        add([saved or DEFAULT_API_BASE])
    return ordered


def _is_auth_payload(payload: dict[str, Any]) -> bool:
    if payload.get("success") is True:
        return False
    text = f"{payload.get('msg') or ''} {payload.get('message') or ''}".lower()
    return any(marker in text for marker in _AUTH_MARKERS)


def _down_message(tried: list[str], last: BaseException | None) -> str:
    detail = f"{last}" if last is not None else "没有响应"
    shown = "、".join(tried[:6]) or DEFAULT_API_BASE
    discovered = "、".join(_discovered_bases())
    found = f"配置文件中的接口是 {discovered}。" if discovered else ""
    return (
        "比特浏览器 Local API 不可用。"
        "请确认比特浏览器 7.1.5 已经打开并登录，并且「系统设置 → Local API」里的本地接口是开启的。"
        f"{found}"
        "若开启了鉴权控制，请把 API Token 填到「比特 Local API 设置」。"
        f"（已尝试 {shown}；{detail}）"
    )


def _call(base: str, path: str, payload: dict[str, Any] | None, token: str, timeout: float) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["x-api-key"] = token
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - base is loopback-only
            raw = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp is not None else b""
        status = exc.code
    except (TimeoutError, OSError) as exc:
        raise _Unreachable(exc) from exc
    if status in (401, 403):
        raise _AuthRejected()
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _Unreachable(exc) from exc
    if not isinstance(parsed, dict):
        raise _Unreachable(ValueError("比特浏览器 Local API 返回格式无效"))
    if _is_auth_payload(parsed):
        raise _AuthRejected()
    if parsed.get("success") is False:
        raise RuntimeError(str(parsed.get("msg") or "比特浏览器请求失败"))
    return parsed


def _api(path: str, payload: dict[str, Any] | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
    global _working_base
    token = load_api_token()
    if _working_base:
        try:
            return _call(_working_base, path, payload, token, timeout)
        except _AuthRejected:
            raise RuntimeError(_AUTH_MESSAGE) from None
        except _Unreachable:
            _working_base = None
    tried: list[str] = []
    last_down: BaseException | None = None
    for base in api_base_candidates():
        tried.append(base)
        probe_timeout = min(float(timeout), _PROBE_TIMEOUT)
        try:
            if path != "/health":
                _call(base, "/health", None, token, probe_timeout)
            result = _call(base, path, payload, token, timeout)
        except _AuthRejected:
            raise RuntimeError(_AUTH_MESSAGE) from None
        except _Unreachable as exc:
            last_down = exc.reason
            continue
        _working_base = base
        return result
    raise RuntimeError(_down_message(tried, last_down))


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
