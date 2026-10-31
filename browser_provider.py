"""Browser-provider dispatch for local CDP endpoints."""

from __future__ import annotations

from . import adspower, bitbrowser


ADSPOWER = "adspower"
BITBROWSER = "bitbrowser"


def normalize_browser_type(value: str | None, *, default: str = ADSPOWER) -> str:
    normalized = str(value or default).strip().casefold()
    if normalized not in {ADSPOWER, BITBROWSER}:
        raise ValueError(f"不支持的浏览器类型：{value}")
    return normalized


def window_key(browser_type: str | None, profile_id: str) -> str:
    profile = str(profile_id or "").strip()
    if not profile:
        raise ValueError("窗口 ID 不能为空")
    return f"{normalize_browser_type(browser_type)}:{profile}"


def split_window_key(value: str) -> tuple[str, str]:
    kind, separator, profile = str(value or "").partition(":")
    if not separator:
        return ADSPOWER, str(value or "").strip()
    browser_type = normalize_browser_type(kind)
    profile = profile.strip()
    if not profile:
        raise ValueError("窗口 ID 不能为空")
    return browser_type, profile


def query_profile_info(
    profile_id: str,
    *,
    browser_type: str = ADSPOWER,
    timeout: float = 2.0,
) -> dict[str, str]:
    module = bitbrowser if normalize_browser_type(browser_type) == BITBROWSER else adspower
    return module.query_profile_info(profile_id, timeout=timeout)


def resolve_cdp(
    profile_id: str,
    *,
    browser_type: str = ADSPOWER,
    start: bool = True,
) -> str:
    module = bitbrowser if normalize_browser_type(browser_type) == BITBROWSER else adspower
    return module.resolve_cdp(profile_id, start=start)
