"""Daily-budget currency conversion for cross-account snapshot copy.

Scope is deliberately narrow: only `campaign["17"]` (daily budget, micros) is
converted.  Amounts nested inside bid strategy config (`campaign["32"]`) are
never touched here -- callers must surface an explicit "not converted, verify
manually" notice for those instead of silently applying this module's rate.

No default 1:1 fallback on fetch failure: a wrong silent rate is a monetary
risk, so `fetch_rate()` always raises rather than guessing.
"""
from __future__ import annotations

import json
import time
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .config import data_dir

RATE_API_URL = "https://open.er-api.com/v6/latest/{base}"
CACHE_TTL_SECONDS = 6 * 3600


def _cache_path(base: str) -> Path:
    folder = data_dir() / "fx-cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{base.upper()}.json"


def _load_cached_rates(base: str) -> dict[str, Any] | None:
    path = _cache_path(base)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(payload.get("fetched_at") or 0) > CACHE_TTL_SECONDS:
        return None
    rates = payload.get("rates")
    return payload if isinstance(rates, dict) and rates else None


def _fetch_rates(base: str, timeout: float) -> dict[str, Any]:
    url = RATE_API_URL.format(base=base.upper())
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8", "replace"))
    if str(payload.get("result") or "") != "success":
        raise RuntimeError(f"汇率接口返回失败：{payload.get('error-type') or payload}")
    rates = payload.get("rates")
    if not isinstance(rates, dict) or not rates:
        raise RuntimeError("汇率接口未返回有效汇率表")
    return {"fetched_at": time.time(), "base": base.upper(), "rates": rates}


def fetch_rate(base: str, quote: str, *, timeout: float = 5.0) -> Decimal:
    """Return how many `quote` units one `base` unit is worth."""
    base = str(base).strip().upper()
    quote = str(quote).strip().upper()
    if not base or not quote:
        raise ValueError("币种代码不能为空")
    if base == quote:
        return Decimal(1)
    payload = _load_cached_rates(base)
    if payload is None:
        payload = _fetch_rates(base, timeout)
        _cache_path(base).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    rates = payload["rates"]
    if quote not in rates:
        raise RuntimeError(f"汇率接口未包含目标币种 {quote}")
    return Decimal(str(rates[quote]))


def convert_micros(
    amount_micros: int, base: str, quote: str, *, manual_rate: Decimal | None = None,
) -> int:
    """Convert a protobuf micros amount (1 unit = 1_000_000 micros)."""
    rate = manual_rate if manual_rate is not None else fetch_rate(base, quote)
    converted = (Decimal(int(amount_micros)) * Decimal(rate)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP,
    )
    return int(converted)


def demo() -> None:
    assert convert_micros(1_000_000, "USD", "USD") == 1_000_000
    assert convert_micros(2_000_000, "USD", "JPY", manual_rate=Decimal("150")) == 300_000_000
    assert convert_micros(0, "USD", "EUR", manual_rate=Decimal("0.9")) == 0
    try:
        fetch_rate("USD", "")
        raise AssertionError("空币种应当报错")
    except ValueError:
        pass
    print("currency_rates demo OK")


if __name__ == "__main__":
    demo()
