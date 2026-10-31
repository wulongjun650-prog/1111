from __future__ import annotations

import json
import os
import re
import copy
import tempfile
from datetime import datetime, timedelta
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .app_categories import ALL_APP_CATEGORY_IDS
from .browser_provider import ADSPOWER, normalize_browser_type, split_window_key, window_key


DEFAULT_OBJECT_NAMES = {"app": "app", "web": "web", "xq": "xq", "ss": "ss"}
ACCOUNT_CACHE_VERSION = 2
ACCOUNT_CACHE_MAX_AGE_SECONDS = 30 * 60
ACCOUNT_CACHE_SOURCE = "批量任务/刷新账户列表与状态"
WARMUP_TEMPLATE_VERSION = 1
WARMUP_TEMPLATE_NAME = "固定白量模板"


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "dictionaries": {
        "默认": {
            "app": "@ALL_APP_CATEGORIES",
            "web": [],
            "xq": [],
            "ss": [],
            "object_names": dict(DEFAULT_OBJECT_NAMES),
        }
    },
}


def data_dir() -> Path:
    target = Path(os.environ.get("APPDATA") or Path.home()) / "GGAdsProtocolTool"
    target.mkdir(parents=True, exist_ok=True)
    return target


def local_config_dir() -> Path:
    target = data_dir() / "local-config"
    target.mkdir(parents=True, exist_ok=True)
    return target


def config_path() -> Path:
    """Dedicated local content-library file (never a Google Ads remote object)."""
    return local_config_dir() / "content-library.json"


def legacy_config_path() -> Path:
    return data_dir() / "config.json"


def ui_state_path() -> Path:
    return data_dir() / "ui-state.json"


def account_cache_path() -> Path:
    """Return the dedicated, local-only account status cache path."""
    return local_config_dir() / "account-cache.json"


def warmup_template_path() -> Path:
    """Return the single selected local warmup-template path."""
    return local_config_dir() / "warmup-template.json"


@dataclass(frozen=True)
class AccountCacheLoadResult:
    """A fail-closed account-cache read result.

    Only ``fresh`` results expose account rows through ``accounts``.  Missing,
    damaged, invalid, or expired files remain on disk for diagnosis but cannot
    accidentally become an account source.
    """

    status: str
    payload: dict[str, Any] | None = None
    reason: str = ""

    @property
    def fresh(self) -> bool:
        return self.status == "fresh" and isinstance(self.payload, dict)

    @property
    def accounts(self) -> dict[str, dict[str, dict[str, Any]]]:
        if not self.fresh:
            return {}
        value = self.payload.get("accounts")
        return value if isinstance(value, dict) else {}


def _aware_cache_datetime(value: datetime | str, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"账户缓存 {label} 必须包含时区")
    return parsed


def _normalize_account_snapshots(
    snapshots: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    if not isinstance(snapshots, dict):
        raise ValueError("账户缓存 snapshots 必须是对象")
    normalized: dict[str, dict[str, dict[str, Any]]] = {}
    for raw_profile, raw_rows in snapshots.items():
        try:
            browser_type, profile = split_window_key(str(raw_profile or ""))
            profile_key = window_key(browser_type, profile)
        except ValueError as exc:
            raise ValueError("账户缓存窗口 ID 或账户列表格式无效") from exc
        if not isinstance(raw_rows, dict):
            raise ValueError("账户缓存窗口 ID 或账户列表格式无效")
        rows: dict[str, dict[str, Any]] = {}
        for raw_cid, raw_snapshot in raw_rows.items():
            cid = normalize_cid(str(raw_cid or ""))
            if not isinstance(raw_snapshot, dict):
                raise ValueError(f"账户缓存 {profile_key}/{cid} 状态数据格式无效")
            snapshot = copy.deepcopy(raw_snapshot)
            snapshot["cid"] = cid
            rows[cid] = snapshot
        normalized[profile_key] = rows
    return normalized


def _cache_scope_windows(
    accounts: dict[str, dict[str, dict[str, Any]]],
    metadata: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    return [
        {
            "browser_type": browser_type,
            "profile_id": profile,
            "window_number": metadata.get(key, {}).get("window_number", ""),
            "window_name": metadata.get(key, {}).get("window_name", ""),
            "metadata_refreshed_at": metadata.get(key, {}).get(
                "metadata_refreshed_at", ""
            ),
            "account_count": len(accounts[key]),
        }
        for key in sorted(accounts)
        for browser_type, profile in [split_window_key(key)]
    ]


def _migrate_v1_account_cache(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate legacy raw-profile cache keys in memory without refreshing it."""
    migrated = copy.deepcopy(payload)
    scope = migrated.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("范围元数据缺失")
    raw_accounts = migrated.get("accounts")
    accounts = _normalize_account_snapshots(raw_accounts)
    expected_by_profile = {}
    for raw_profile in raw_accounts:
        profile = str(raw_profile or "").strip()
        browser_type, profile_id = split_window_key(profile)
        expected_by_profile[profile] = sorted(
            accounts[window_key(browser_type, profile_id)]
        )
    if scope.get("profile_ids") != sorted(raw_accounts):
        raise ValueError("窗口范围与账户数据不一致")
    if scope.get("accounts_by_profile") != expected_by_profile:
        raise ValueError("CID 范围与账户数据不一致")
    if scope.get("account_count") != sum(len(rows) for rows in raw_accounts.values()):
        raise ValueError("账户数量与账户数据不一致")
    if not isinstance(scope.get("windows"), list):
        raise ValueError("窗口元数据格式无效")
    legacy_windows = {
        str(item.get("profile_id") or "").strip(): item
        for item in scope.get("windows") or []
        if isinstance(item, dict) and str(item.get("profile_id") or "").strip()
    }
    metadata = {
        key: {
            "window_number": str(legacy_windows.get(profile, {}).get("window_number") or "").strip(),
            "window_name": str(legacy_windows.get(profile, {}).get("window_name") or "").strip(),
            "metadata_refreshed_at": str(
                legacy_windows.get(profile, {}).get("metadata_refreshed_at") or ""
            ).strip(),
        }
        for key in accounts
        for _, profile in [split_window_key(key)]
    }
    migrated["version"] = ACCOUNT_CACHE_VERSION
    migrated["accounts"] = accounts
    migrated["scope"] = {
        "profile_ids": sorted(accounts),
        "account_count": sum(len(rows) for rows in accounts.values()),
        "accounts_by_profile": {key: sorted(rows) for key, rows in sorted(accounts.items())},
        "windows": _cache_scope_windows(accounts, metadata),
    }
    return migrated


def _atomic_write_json(target: Path, value: dict[str, Any]) -> None:
    """Durably stage JSON beside ``target`` and atomically replace it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def save_account_cache(
    snapshots: dict[str, dict[str, dict[str, Any]]],
    *,
    windows: list[dict[str, Any]] | None = None,
    refreshed_at: datetime | str | None = None,
    max_age_seconds: int = ACCOUNT_CACHE_MAX_AGE_SECONDS,
    path: Path | None = None,
) -> Path:
    """Persist one complete successful account refresh as a single snapshot."""
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds <= 0:
        raise ValueError("账户缓存有效期必须是正整数秒")
    accounts = _normalize_account_snapshots(snapshots)
    refreshed = _aware_cache_datetime(
        refreshed_at or datetime.now().astimezone(), "refreshed_at"
    )
    expires = refreshed + timedelta(seconds=max_age_seconds)
    window_metadata: dict[str, dict[str, str]] = {}
    for item in windows or []:
        if not isinstance(item, dict):
            continue
        try:
            key = window_key(item.get("browser_type"), item.get("profile_id"))
        except ValueError:
            continue
        if key in accounts:
            window_metadata[key] = {
                "window_number": str(
                    item.get("window_number") or item.get("serial_number") or ""
                ).strip(),
                "window_name": str(
                    item.get("window_name") or item.get("name") or ""
                ).strip(),
                "metadata_refreshed_at": str(
                    item.get("metadata_refreshed_at")
                    or item.get("resolved_at")
                    or ""
                ).strip(),
            }
    accounts_by_profile = {profile: sorted(rows) for profile, rows in sorted(accounts.items())}
    payload = {
        "version": ACCOUNT_CACHE_VERSION,
        "source": ACCOUNT_CACHE_SOURCE,
        "refreshed_at": refreshed.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "max_age_seconds": max_age_seconds,
        "scope": {
            "profile_ids": sorted(accounts),
            "account_count": sum(len(rows) for rows in accounts.values()),
            "accounts_by_profile": accounts_by_profile,
            "windows": _cache_scope_windows(accounts, window_metadata),
        },
        "accounts": accounts,
    }
    target = path or account_cache_path()
    _atomic_write_json(target, payload)
    return target


def load_account_cache(
    path: Path | None = None,
    *,
    now: datetime | None = None,
) -> AccountCacheLoadResult:
    """Load a fresh, internally consistent cache or return a safe fallback status."""
    target = path or account_cache_path()
    if not target.exists():
        return AccountCacheLoadResult("missing", reason="账户缓存尚未创建")
    try:
        with target.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return AccountCacheLoadResult("corrupt", reason=f"账户缓存无法读取：{exc}")
    try:
        if not isinstance(payload, dict):
            raise ValueError("版本不受支持")
        if payload.get("version") == 1:
            payload = _migrate_v1_account_cache(payload)
        if payload.get("version") != ACCOUNT_CACHE_VERSION:
            raise ValueError("版本不受支持")
        if payload.get("source") != ACCOUNT_CACHE_SOURCE:
            raise ValueError("来源标识不匹配")
        max_age = payload.get("max_age_seconds")
        if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age <= 0:
            raise ValueError("有效期无效")
        refreshed = _aware_cache_datetime(payload.get("refreshed_at"), "refreshed_at")
        declared_expiry = _aware_cache_datetime(payload.get("expires_at"), "expires_at")
        accounts = _normalize_account_snapshots(payload.get("accounts"))
        scope = payload.get("scope")
        if not isinstance(scope, dict):
            raise ValueError("范围元数据缺失")
        expected_by_profile = {
            profile: sorted(rows) for profile, rows in sorted(accounts.items())
        }
        if scope.get("profile_ids") != sorted(accounts):
            raise ValueError("窗口范围与账户数据不一致")
        if scope.get("accounts_by_profile") != expected_by_profile:
            raise ValueError("CID 范围与账户数据不一致")
        if scope.get("account_count") != sum(len(rows) for rows in accounts.values()):
            raise ValueError("账户数量与账户数据不一致")
        if not isinstance(scope.get("windows"), list):
            raise ValueError("窗口元数据格式无效")
        scope_window_keys = set()
        for window in scope["windows"]:
            if not isinstance(window, dict):
                raise ValueError("窗口元数据格式无效")
            key = window_key(window.get("browser_type"), window.get("profile_id"))
            if window.get("account_count") != len(accounts.get(key) or {}):
                raise ValueError("窗口账户数量与账户数据不一致")
            scope_window_keys.add(key)
        if scope_window_keys != set(accounts):
            raise ValueError("窗口元数据与账户数据不一致")
        hard_expiry = min(declared_expiry, refreshed + timedelta(seconds=max_age))
        current = _aware_cache_datetime(now or datetime.now().astimezone(), "now")
        payload["accounts"] = accounts
    except (TypeError, ValueError) as exc:
        return AccountCacheLoadResult("invalid", reason=f"账户缓存结构无效：{exc}")
    if current >= hard_expiry:
        return AccountCacheLoadResult(
            "expired", payload=payload,
            reason=f"账户缓存已于 {hard_expiry.isoformat(timespec='seconds')} 过期",
        )
    return AccountCacheLoadResult("fresh", payload=payload)


def account_cache_wanted(
    cache: AccountCacheLoadResult,
    *,
    allowed_profiles: set[str] | None = None,
) -> dict[str, set[str]]:
    """Return ordinary CID scope for read-only consumers such as daily spend."""
    wanted: dict[str, set[str]] = {}
    for profile, rows in cache.accounts.items():
        if allowed_profiles is not None and profile not in allowed_profiles:
            continue
        wanted[profile] = {
            cid for cid, snapshot in rows.items()
            if str(snapshot.get("account_type") or "CID").strip().upper() != "MCC"
            and not bool(snapshot.get("is_manager"))
        }
    return wanted


def account_cache_metadata(
    cache: AccountCacheLoadResult,
    profile: str,
    cid: str,
) -> dict[str, str] | None:
    """Return display-only account basics from a fresh cache."""
    try:
        normalized_cid = normalize_cid(cid)
        browser_type, profile_id = split_window_key(profile)
        profile_key = window_key(browser_type, profile_id)
    except ValueError:
        return None
    snapshot = cache.accounts.get(profile_key, {}).get(normalized_cid)
    if not isinstance(snapshot, dict):
        return None
    window_number = ""
    scope = (cache.payload or {}).get("scope") or {}
    for window in scope.get("windows") or []:
        if not isinstance(window, dict):
            continue
        try:
            if window_key(window.get("browser_type"), window.get("profile_id")) == profile_key:
                window_number = str(window.get("window_number") or "")
                break
        except ValueError:
            continue
    return {
        "window_number": window_number,
        "account_name": str(snapshot.get("name") or snapshot.get("account_name") or ""),
        "account_status": str(snapshot.get("status") or "状态未知"),
        "account_type": str(
            snapshot.get("account_type") or ("MCC" if snapshot.get("is_manager") else "CID")
        ),
    }


def normalize_window_metadata(
    profile: str,
    metadata: dict[str, Any],
    *,
    resolved_at: datetime | str | None = None,
    source: str = "adspower",
) -> dict[str, str]:
    """Normalize one verified AdsPower window-number lookup result."""
    profile_id = str(profile or "").strip()
    if not profile_id or not isinstance(metadata, dict):
        raise ValueError("窗口编号资料格式无效")
    returned_profile = str(metadata.get("profile_id") or profile_id).strip()
    if returned_profile != profile_id:
        raise ValueError(
            f"窗口编号资料与目标不一致：期望 {profile_id}，实际 {returned_profile}"
        )
    serial_number = str(
        metadata.get("serial_number") or metadata.get("window_number") or ""
    ).strip()
    if not serial_number:
        raise ValueError("AdsPower 已返回窗口资料，但没有返回编号字段")
    timestamp_value = (
        resolved_at
        or metadata.get("metadata_refreshed_at")
        or metadata.get("resolved_at")
        or datetime.now().astimezone()
    )
    timestamp = _aware_cache_datetime(timestamp_value, "window metadata resolved_at")
    return {
        "profile_id": profile_id,
        "serial_number": serial_number,
        "name": str(metadata.get("name") or metadata.get("window_name") or "").strip(),
        "resolved_at": timestamp.isoformat(timespec="seconds"),
        "source": str(metadata.get("source") or source).strip() or source,
    }


def account_cache_window_metadata(
    cache: AccountCacheLoadResult,
    profile: str,
    *,
    browser_type: str = ADSPOWER,
) -> dict[str, str] | None:
    """Return a fresh, previously verified window number from the account cache."""
    if not cache.fresh:
        return None
    try:
        key = window_key(browser_type, profile)
        _, profile_id = split_window_key(key)
    except ValueError:
        return None
    scope = (cache.payload or {}).get("scope") or {}
    for window in scope.get("windows") or []:
        if not isinstance(window, dict):
            continue
        try:
            if window_key(window.get("browser_type"), window.get("profile_id")) != key:
                continue
        except ValueError:
            continue
        window_number = str(window.get("window_number") or "").strip()
        metadata_refreshed_at = str(
            window.get("metadata_refreshed_at") or ""
        ).strip()
        if not window_number or not metadata_refreshed_at:
            return None
        try:
            return normalize_window_metadata(
                profile_id,
                {
                    "profile_id": profile_id,
                    "serial_number": window_number,
                    "name": window.get("window_name") or "",
                    "resolved_at": metadata_refreshed_at,
                    "source": "account-cache",
                },
            )
        except ValueError:
            return None
    return None


def update_account_cache_window_metadata(
    metadata: dict[str, dict[str, Any]],
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Atomically update verified window numbers without extending account TTL.

    Missing, damaged, invalid, or expired account caches are left untouched.  A
    window refresh therefore cannot turn stale account data into a fresh account
    snapshot.
    """
    target = path or account_cache_path()
    cache = load_account_cache(target, now=now)
    if not cache.fresh:
        return False
    payload = copy.deepcopy(cache.payload or {})
    scope = payload.get("scope") or {}
    profiles = set(payload.get("accounts") or {})
    windows = {
        window_key(item.get("browser_type"), item.get("profile_id")): dict(item)
        for item in scope.get("windows") or []
        if isinstance(item, dict)
    }
    changed = False
    for raw_profile, raw_metadata in metadata.items():
        try:
            browser_type, profile_id = split_window_key(raw_profile)
            profile_key = window_key(browser_type, profile_id)
        except ValueError:
            continue
        if profile_key not in profiles:
            continue
        normalized = normalize_window_metadata(profile_id, raw_metadata)
        item = windows.setdefault(
            profile_key,
            {
                "browser_type": browser_type,
                "profile_id": profile_id,
                "account_count": len(payload["accounts"].get(profile_key) or {}),
            },
        )
        replacement = {
            "window_number": normalized["serial_number"],
            "window_name": normalized["name"],
            "metadata_refreshed_at": normalized["resolved_at"],
        }
        if any(item.get(key) != value for key, value in replacement.items()):
            item.update(replacement)
            changed = True
    if not changed:
        return False
    scope["windows"] = [windows[profile] for profile in sorted(windows)]
    payload["scope"] = scope
    _atomic_write_json(target, payload)
    return True


def normalize_window_record(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["browser_type"] = normalize_browser_type(result.get("browser_type"))
    result["profile_id"] = str(result.get("profile_id") or "").strip()
    if not result["profile_id"]:
        raise ValueError("窗口 ID 不能为空")
    return result


def _normalize_ui_state(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    windows = result.get("windows")
    if isinstance(windows, list):
        normalized_windows = []
        for item in windows:
            if not isinstance(item, dict):
                continue
            normalized = _safe_normalize_window_record(item)
            if normalized is not None:
                normalized_windows.append(normalized)
        result["windows"] = normalized_windows
    return result


def _safe_normalize_window_record(value: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return normalize_window_record(value)
    except ValueError:
        return None


def load_ui_state() -> dict[str, Any]:
    target = ui_state_path()
    if not target.exists():
        return {}
    try:
        with target.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return _normalize_ui_state(value) if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_ui_state(value: dict[str, Any]) -> Path:
    target = ui_state_path()
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_normalize_ui_state(value), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(target)
    return target


def global_account_scope(
    state: dict[str, Any] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return the account whitelist saved by the first (batch task) screen."""
    source = state if state is not None else load_ui_state()
    scope: dict[str, dict[str, dict[str, Any]]] = {}
    for window in source.get("windows", []) if isinstance(source, dict) else []:
        if not isinstance(window, dict):
            continue
        try:
            profile_key = window_key(window.get("browser_type"), window.get("profile_id"))
        except ValueError:
            continue
        rows: dict[str, dict[str, Any]] = {}
        for item in window.get("cids", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                cid = normalize_cid(str(item.get("cid") or ""))
            except ValueError:
                continue
            rows[cid] = dict(item)
        scope[profile_key] = rows
    return scope


def merge_global_account_snapshots(
    snapshots: dict[str, dict[str, dict[str, Any]]],
    *,
    state: dict[str, Any] | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Refresh saved first-screen rows without importing newly discovered CIDs."""
    merged = _normalize_ui_state(state) if state is not None else load_ui_state()
    if not isinstance(merged, dict):
        merged = {}
    try:
        normalized_snapshots = _normalize_account_snapshots(snapshots)
    except ValueError:
        normalized_snapshots = {}
    timestamp = updated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    updated_rows = 0
    for window in merged.get("windows", []) or []:
        if not isinstance(window, dict):
            continue
        try:
            profile_key = window_key(window.get("browser_type"), window.get("profile_id"))
        except ValueError:
            continue
        by_cid = normalized_snapshots.get(profile_key) or {}
        for row in window.get("cids", []) or []:
            if not isinstance(row, dict):
                continue
            try:
                cid = normalize_cid(str(row.get("cid") or ""))
            except ValueError:
                continue
            live = by_cid.get(cid)
            if not isinstance(live, dict):
                continue
            mappings = {
                "name": "account_name",
                "ocid": "ocid",
                "account_type": "account_type",
                "parent_mcc": "parent_mcc",
                "depth": "depth",
                "status": "status",
                "status_detail": "status_detail",
            }
            for source_key, target_key in mappings.items():
                if source_key in live:
                    value = live.get(source_key)
                    if source_key == "parent_mcc" and isinstance(value, list):
                        value = " / ".join(str(item) for item in value if str(item).strip())
                    row[target_key] = value if value is not None else ""
            row["updated_at"] = timestamp.replace("T", " ")[:19]
            updated_rows += 1
    account_count = sum(
        len(window.get("cids", []) or [])
        for window in merged.get("windows", []) or []
        if isinstance(window, dict)
    )
    merged["global_account_data"] = {
        "source": "批量任务/刷新账户列表与状态",
        "updated_at": timestamp,
        "window_count": len(global_account_scope(merged)),
        "account_count": account_count,
        "updated_rows": updated_rows,
    }
    return merged


def save_run_report(report: dict[str, Any]) -> Path:
    folder = data_dir() / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = folder / f"run-{stamp}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or config_path()
    if path is None and not target.exists() and legacy_config_path().exists():
        with legacy_config_path().open("r", encoding="utf-8") as handle:
            migrated = json.load(handle)
        validate_config(migrated)
        save_config(migrated, target)
        return migrated
    if not target.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
    with target.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    validate_config(data)
    return data


def save_config(data: dict[str, Any], path: Path | None = None) -> Path:
    validate_config(data)
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def validate_config(data: dict[str, Any]) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("dictionaries"), dict):
        raise ValueError("配置必须包含 dictionaries 对象")
    if not data["dictionaries"]:
        raise ValueError("至少需要一个字典")
    for name, value in data["dictionaries"].items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("字典名称不能为空")
        if not isinstance(value, (dict, list, str)):
            raise ValueError(f"字典 {name} 的值格式无效")


def _expand(value: Any, dictionaries: dict[str, Any], stack: tuple[str, ...]) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        result: list[Any] = []
        for item in value:
            result.extend(_expand(item, dictionaries, stack))
        return result
    if isinstance(value, str) and value.startswith("@"):
        name = value[1:]
        if name == "ALL_APP_CATEGORIES":
            return list(ALL_APP_CATEGORY_IDS)
        if name in stack:
            raise ValueError("字典循环引用: " + " -> ".join((*stack, name)))
        if name not in dictionaries:
            raise ValueError(f"找不到引用字典: @{name}")
        return _expand(dictionaries[name], dictionaries, (*stack, name))
    return [value]


def resolve_named_dictionary(data: dict[str, Any], name: str) -> dict[str, list[Any]]:
    validate_config(data)
    dictionaries = data["dictionaries"]
    if name not in dictionaries or not isinstance(dictionaries[name], dict):
        raise ValueError(f"找不到内容字典: {name}")
    source = dictionaries[name]
    return {
        key: _expand(source.get(key, []), dictionaries, (name,))
        for key in ("app", "web", "xq", "ss")
    }


def resolve_object_names(data: dict[str, Any], name: str) -> dict[str, str]:
    """Resolve the Google Ads object name mapped by one content plan."""
    validate_config(data)
    source = data["dictionaries"].get(name)
    if not isinstance(source, dict):
        raise ValueError(f"找不到内容字典: {name}")
    configured = source.get("object_names") or {}
    if not isinstance(configured, dict):
        raise ValueError(f"内容方案 {name} 的 Google Ads 名称映射格式无效")
    result: dict[str, str] = {}
    for key, default in DEFAULT_OBJECT_NAMES.items():
        value = str(configured.get(key) or default).strip()
        if not value:
            raise ValueError(f"内容方案 {name} 的 {key} Google Ads 名称不能为空")
        if len(value) > 120:
            raise ValueError(f"内容方案 {name} 的 {key} Google Ads 名称超过 120 个字符")
        result[key] = value
    if result["app"] == result["web"]:
        raise ValueError(f"内容方案 {name} 的 app 与 web 排除列表名称不能相同")
    if result["xq"] == result["ss"]:
        raise ValueError(f"内容方案 {name} 的 xq 与 ss 自定义受众名称不能相同")
    return result


def normalize_cid(value: str) -> str:
    cid = re.sub(r"\D", "", value)
    if len(cid) != 10:
        raise ValueError(f"CID 格式应为 532-793-9237（也兼容输入 10 位纯数字）：{value}")
    return cid


def format_cid(value: str) -> str:
    """Return a Google Ads customer ID in the familiar 123-456-7890 form."""
    cid = normalize_cid(value)
    return f"{cid[:3]}-{cid[3:6]}-{cid[6:]}"


def normalize_warmup_template(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the single selected local warmup-template reference."""
    if not isinstance(value, dict):
        raise ValueError("固定白量模板格式无效")
    version = value.get("version", WARMUP_TEMPLATE_VERSION)
    if version != WARMUP_TEMPLATE_VERSION:
        raise ValueError("固定白量模板版本不受支持")
    source_cid = normalize_cid(str(value.get("source_cid") or ""))
    campaign_name = str(value.get("campaign_name") or "App-1").strip()
    if not campaign_name or len(campaign_name) > 120:
        raise ValueError("固定白量模板广告系列名称无效")
    try:
        budget = Decimal(str(value.get("target_daily_budget") or "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("固定白量模板目标日预算必须是有效数字") from exc
    if not budget.is_finite() or budget <= 0:
        raise ValueError("固定白量模板目标日预算必须大于 0")
    if budget != budget.quantize(Decimal("0.01")):
        raise ValueError("固定白量模板目标日预算最多保留两位小数")
    saved_at = _aware_cache_datetime(value.get("saved_at"), "warmup template saved_at")
    return {
        "version": WARMUP_TEMPLATE_VERSION,
        "template_id": "fixed",
        "name": str(value.get("name") or WARMUP_TEMPLATE_NAME).strip()
        or WARMUP_TEMPLATE_NAME,
        "source_cid": source_cid,
        "campaign_name": campaign_name,
        "target_daily_budget": f"{budget:.2f}",
        "saved_at": saved_at.isoformat(timespec="seconds"),
        "source": "本地固定模板",
    }


def load_warmup_template(path: Path | None = None) -> dict[str, Any] | None:
    target = path or warmup_template_path()
    if not target.exists():
        return None
    try:
        with target.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"固定白量模板无法读取：{exc}") from exc
    return normalize_warmup_template(value)


def save_warmup_template(
    source_cid: str,
    target_daily_budget: Decimal | str | int | float,
    *,
    campaign_name: str = "App-1",
    name: str = WARMUP_TEMPLATE_NAME,
    saved_at: datetime | str | None = None,
    path: Path | None = None,
) -> Path:
    payload = normalize_warmup_template({
        "version": WARMUP_TEMPLATE_VERSION,
        "name": name,
        "source_cid": source_cid,
        "campaign_name": campaign_name,
        "target_daily_budget": target_daily_budget,
        "saved_at": saved_at or datetime.now().astimezone(),
    })
    target = path or warmup_template_path()
    _atomic_write_json(target, payload)
    return target


@dataclass(frozen=True)
class Assignment:
    cid: str
    dictionary: str


def parse_assignments(text: str, default_dictionary: str) -> list[Assignment]:
    result: list[Assignment] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        left, sep, right = line.partition("=")
        cid = normalize_cid(left)
        dictionary = right.strip() if sep else default_dictionary
        if not dictionary:
            raise ValueError(f"第 {line_no} 行缺少字典名")
        if cid in seen:
            raise ValueError(f"CID 重复: {format_cid(cid)}")
        seen.add(cid)
        result.append(Assignment(cid, dictionary))
    if not result:
        raise ValueError("请至少填写一个 CID")
    return result


def normalize_websites(items: list[Any]) -> list[str]:
    result: list[str] = []
    for raw in strip_content_comments(items):
        value = str(raw).strip()
        if not value or _is_website_note(value):
            continue
        parsed = urlsplit(value if "://" in value else "//" + value)
        host = (parsed.hostname or "").lower().strip(".")
        if not host or "." not in host or " " in host:
            raise ValueError(f"网站格式无效: {value}")
        if host.startswith("www."):
            host = host[4:]
        if host not in result:
            result.append(host)
    return result


_WEBSITE_HEADER_LABELS = {
    "url",
    "website",
    "website url",
    "placement url",
    "网址",
    "网站",
    "网站网址",
    "域名",
    "展示位置",
    "展示位置网址",
    "展示位置网站",
    "排除网址",
    "排除网站",
}


def _is_website_note(value: str) -> bool:
    """Recognize human-only headings commonly pasted above website lists."""
    text = value.strip().lstrip("\ufeff").strip()
    plain = text.strip("：:").strip()
    folded = re.sub(r"\s+", " ", plain).casefold()
    if folded in _WEBSITE_HEADER_LABELS:
        return True
    if re.fullmatch(r"[-=*_·•—–\s]+", text):
        return True
    if re.fullmatch(r"[【\[].+[】\]]", text):
        return True
    if re.match(r"^(?:备注|注释|说明|标题|note|comment)\s*[：:]", text, flags=re.IGNORECASE):
        return True
    # Examples: “韩国排除网址去重版” and “日常排除网站”. A real domain
    # still contains a dot and therefore isn't swallowed by this rule.
    if "." not in text and re.search(r"(?:网址|网站|域名|展示位置)", text):
        return True
    return False


def normalize_terms(items: list[Any], label: str) -> list[str]:
    result: list[str] = []
    for raw in strip_content_comments(items):
        value = str(raw).strip()
        if not value:
            continue
        if len(value) > 80:
            raise ValueError(f"{label} 单项超过 80 个字符: {value[:30]}…")
        if value not in result:
            result.append(value)
    return result


def strip_content_comments(items: list[Any]) -> list[str]:
    """Remove human notes before values are sent to Google Ads.

    Supports C-style blocks (including multi-line blocks), full-line ``//`` or
    ``#`` comments, and whitespace-prefixed trailing comments.
    """
    text = "\n".join(str(item) for item in items)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    result: list[str] = []
    for raw in text.splitlines():
        value = raw.strip()
        if not value or value.startswith("//") or value.startswith("#"):
            continue
        value = re.split(r"\s+(?://|#)", value, maxsplit=1)[0].strip()
        if value:
            result.append(value)
    return result
