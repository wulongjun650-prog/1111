from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import date, datetime, timezone
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from websockets.sync.client import connect as ws_connect

from .adspower import (
    close_cdp_target,
    ensure_google_ads_targets,
    list_google_ads_targets,
    open_cdp_target,
)
from .browser_provider import ADSPOWER, normalize_browser_type, resolve_cdp as resolve_browser_cdp
from .app_categories import ALL_APP_CATEGORY_IDS
from .config import (
    Assignment,
    format_cid,
    normalize_cid,
    normalize_terms,
    normalize_websites,
    resolve_named_dictionary,
    resolve_object_names,
    strip_content_comments,
)
from .reporting import ReportPeriod, coerce_report_period, protocol_day
from .protocol_catalog import (
    CAMPAIGN_LIST_PATH,
    CAMPAIGN_STATUS_ENABLED,
    CAMPAIGN_STATUS_LABELS,
    CAMPAIGN_STATUS_MUTATE_PATH,
    CAMPAIGN_STATUS_PAUSED,
    CAMPAIGN_STATUS_REMOVED,
)


# Both entries are Google Ads custom-audience/custom-segment strategies.
# xq = people with interests or purchase intentions; ss = people who searched
# these terms on Google. Keep this mapping centralized so type 4 is never
# mistaken for a normal keyword object.
CUSTOM_AUDIENCE_TYPE_BY_KIND = {"xq": 1, "ss": 4}
CUSTOM_AUDIENCE_KIND_BY_TYPE = {
    value: key for key, value in CUSTOM_AUDIENCE_TYPE_BY_KIND.items()
}


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def campaign_daily_spend_request(
    ocid: str, report_date: date | ReportPeriod,
) -> dict[str, Any]:
    """Read a CampaignService cost snapshot for one day or the all-time range."""
    period = coerce_report_period(report_date)
    return {
        "1": {
            "3": {"1": str(ocid)},
            "5": "RequestBuilder.campaignsForWorkspace",
        },
        "2": {
            "1": ["campaign_id", "name", "status", "currency_code", "stats.cost"],
            "4": {
                "1": protocol_day(period.start),
                "2": protocol_day(period.end),
                "4": "0",
            },
        },
    }


# 广告系列状态与协议路径统一由 protocol_catalog 管理，防止不同页面漂移。
# 最新 fixture 与只读实测：3=启用、5=暂停、7=移除。
BID_TYPE_LABELS = {2: "手动点击付费", 13: "智能出价"}


def campaign_list_request(ocid: str) -> dict[str, Any]:
    """List campaigns with status/budget/bid type."""
    return {
        "1": {"3": {"1": str(ocid)}, "5": "RequestBuilder.campaignsForWorkspace"},
        "2": {"1": ["campaign_id", "name", "status", "budget_amount", "bid_config.type"]},
    }


def parse_campaign_list(payload: dict[str, Any], *, expected_ocid: str) -> list[dict[str, Any]]:
    service_errors = ((payload.get("5") or {}).get("2") or [])
    if service_errors:
        first = service_errors[0] if isinstance(service_errors[0], dict) else {}
        raise PermissionError(str(first.get("1") or first.get("13") or "系列列表读取失败"))
    campaigns: list[dict[str, Any]] = []
    for raw in payload.get("1") or []:
        if not isinstance(raw, dict) or not raw.get("2"):
            continue
        row_ocid = str(raw.get("1") or "")
        if row_ocid and row_ocid != str(expected_ocid):
            continue
        status = int(raw.get("12") or 0)
        bid = raw.get("32") or {}
        bid_type = int(bid.get("1") or 0) if isinstance(bid, dict) else 0
        micros = str(raw.get("17") or "0")
        try:
            budget = f"{Decimal(micros) / Decimal(1_000_000):.2f}"
        except InvalidOperation:
            budget = ""
        campaigns.append({
            "campaign_id": str(raw.get("2") or ""),
            "name": str(raw.get("11") or ""),
            "status": status,
            "status_label": CAMPAIGN_STATUS_LABELS.get(status, f"未知({status})"),
            "budget_micros": micros,
            "budget": budget,
            "bid_type": bid_type,
            "bid_label": BID_TYPE_LABELS.get(bid_type, str(bid_type) if bid_type else ""),
        })
    return campaigns


def set_campaign_status_request(ocid: str, campaign_id: str, status: int) -> dict[str, Any]:
    """CampaignService/Mutate 状态更新（op 3 = update，mask status）。"""
    if type(status) is not int or status not in CAMPAIGN_STATUS_LABELS:
        raise ValueError(f"非法系列状态值: {status}（只支持 3=启用 5=暂停 7=移除）")
    if type(ocid) is not str or re.fullmatch(r"[1-9][0-9]{0,19}", ocid) is None:
        raise ValueError("ocid 必须是规范数字 ID")
    if (
        type(campaign_id) is not str
        or re.fullmatch(r"[1-9][0-9]{0,19}", campaign_id) is None
    ):
        raise ValueError("campaign_id 必须是规范数字 ID")
    return {
        "1": {"2": {"1": False}, "3": {"2": ocid}},
        "2": [{"1": 3, "2": ["status"], "3": {"1": ocid, "2": campaign_id, "12": status}}],
    }


def mutate_service_errors(payload: dict[str, Any]) -> list[str]:
    """提取 Mutate 响应里的业务错误码（空列表 = 成功）。"""
    errors = []
    for item in ((payload.get("3") or {}).get("2") or []):
        if isinstance(item, dict):
            errors.append(str(item.get("3") or item.get("1") or "未知错误"))
    return errors


def parse_daily_spend(
    payload: dict[str, Any], *, expected_ocid: str,
    report_date: date | ReportPeriod,
) -> dict[str, Any]:
    period = coerce_report_period(report_date)
    service_errors = ((payload.get("5") or {}).get("2") or [])
    if service_errors:
        first = service_errors[0] if isinstance(service_errors[0], dict) else {}
        raise PermissionError(str(first.get("1") or first.get("13") or "消耗读取失败"))
    projection = payload.get("2") or {}
    fields = projection.get("1") or [
        item.get("3") for item in (projection.get("2") or [])
        if isinstance(item, dict) and item.get("3")
    ]
    total_micros = Decimal("0")
    currencies: set[str] = set()
    campaigns = []
    for raw in payload.get("1") or []:
        if not isinstance(raw, dict):
            continue
        row_ocid = str(raw.get("1") or "")
        if row_ocid and row_ocid != str(expected_ocid):
            raise PermissionError(f"消耗数据属于其他内部账户 {row_ocid}")
        values = ((raw.get("200") or {}).get("1") or [])
        mapped = {
            str(fields[index]): values[index]
            for index in range(min(len(fields), len(values)))
        }
        raw_cost = mapped.get("stats.cost", mapped.get("cost", 0))
        try:
            cost_micros = Decimal(str(raw_cost or 0))
        except InvalidOperation as exc:
            raise ValueError("stats.cost 不是有效数字") from exc
        if not cost_micros.is_finite() or cost_micros < 0:
            raise ValueError("stats.cost 必须是非负有限数字")
        currency = str(raw.get("18") or mapped.get("currency_code") or "").strip().upper()
        if currency:
            currencies.add(currency)
        total_micros += cost_micros
        campaigns.append({
            "campaign_id": str(raw.get("2") or ""),
            "name": str(raw.get("11") or ""),
            "status": int(raw.get("12") or 0),
            "cost": str(cost_micros / Decimal(1_000_000)),
        })
    if len(currencies) > 1:
        raise ValueError("同一 CID 返回了多个消耗币种")
    currency = next(iter(currencies), "")
    amount = total_micros / Decimal(1_000_000)
    display = f"{currency + ' ' if currency else ''}{amount:.2f}"
    return {
        "daily_spend": display,
        "daily_spend_amount": str(amount),
        "daily_spend_currency": currency,
        "daily_spend_status": "可用",
        "daily_spend_date": period.label,
        "daily_spend_detail": (
            f"CampaignService/List 汇总 {len(campaigns)} 个广告系列；"
            f"范围 {period.start.isoformat()} 至 {period.end.isoformat()}"
        ),
        "daily_spend_campaigns": campaigns,
    }


def _json_response(text: str) -> Any:
    value = text.strip()
    if value.startswith(")]}'"):
        value = value.split("\n", 1)[-1]
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Google Ads 返回的不是有效 JSON: {value[:300]}") from exc


_MONEY_RE = re.compile(
    r"(?P<currency>(?:[A-Z]{1,3})?[$€£¥₹₫]|[A-Z]{3})?\s*"
    r"(?P<amount>-?\d[\d,]*(?:\.\d+)?)\s*(?P<suffix>[A-Z]{3})?"
)


def parse_money(value: str) -> tuple[Decimal, str] | None:
    match = _MONEY_RE.search(str(value).replace("\xa0", " ").strip())
    if not match:
        return None
    try:
        amount = Decimal(match.group("amount").replace(",", ""))
    except InvalidOperation:
        return None
    currency = str(match.group("currency") or match.group("suffix") or "").strip()
    return amount, currency


def format_money(amount: Decimal, currency: str) -> str:
    number = f"{amount:,.2f}"
    if not currency:
        return number
    if currency.endswith(("$", "€", "£", "¥", "₹", "₫")) or currency in "$€£¥₹₫":
        return f"{currency}{number}"
    return f"{currency} {number}"


def require_positive_balance(
    balance: dict[str, Any], *, cid: str = "", action: str = "广告创建",
) -> tuple[Decimal, str]:
    """Validate that a freshly read Billing balance is trustworthy and positive."""
    prefix = f"CID {format_cid(cid)} " if cid else ""
    status = str(balance.get("balance_status") or "未知")
    detail = str(balance.get("balance_detail") or "").strip()
    if status != "可用":
        suffix = f"：{detail}" if detail else ""
        raise PermissionError(
            f"{prefix}{action}要求余额可识别且大于 0；余额读取状态为“{status}”{suffix}"
        )
    parsed = parse_money(str(balance.get("balance") or ""))
    if not parsed:
        raise PermissionError(
            f"{prefix}{action}要求余额可识别且大于 0；无法解析余额“{balance.get('balance') or '—'}”"
        )
    amount, currency = parsed
    if amount <= 0:
        raise PermissionError(
            f"{prefix}{action}要求余额大于 0；当前余额为 {format_money(amount, currency)}"
        )
    return amount, currency


def extract_billing_balance(payload: dict[str, Any]) -> dict[str, str]:
    """Extract a trustworthy account-level balance from a rendered Billing page."""
    rows = payload.get("rows") or []
    header: list[str] = []
    for row in rows:
        candidate = [str(value).strip() for value in row.get("headers") or []]
        if any("剩余账号预算" in value or "Remaining account budget" in value for value in candidate):
            header = candidate
            break
    if header:
        remaining_index = next(
            index for index, value in enumerate(header)
            if "剩余账号预算" in value or "Remaining account budget" in value
        )
        amount_index = next((
            index for index, value in enumerate(header)
            if value in {"预算金额", "Budget amount"}
        ), -1)
        spend_index = next((
            index for index, value in enumerate(header)
            if value in {"支出金额", "Amount spent", "Spend"}
        ), -1)
        status_index = next((
            index for index, value in enumerate(header)
            if value in {"状态", "Status"}
        ), -1)
        candidates: list[tuple[list[str], tuple[Decimal, str]]] = []
        for row in rows:
            cells = [str(value).strip() for value in row.get("cells") or []]
            if len(cells) <= remaining_index:
                continue
            money = parse_money(cells[remaining_index].splitlines()[0])
            if money:
                candidates.append((cells, money))
        if candidates:
            active = [
                item for item in candidates
                if status_index >= 0 and len(item[0]) > status_index
                and any(label in item[0][status_index].casefold() for label in ("有效", "active"))
            ]
            cells, (remaining, currency) = (active or candidates)[0]
            budget = parse_money(cells[amount_index].splitlines()[0]) if amount_index >= 0 and len(cells) > amount_index else None
            spent = parse_money(cells[spend_index].splitlines()[0]) if spend_index >= 0 and len(cells) > spend_index else None
            status = cells[status_index].splitlines()[0] if status_index >= 0 and len(cells) > status_index else ""
            detail_parts = ["Billing 账号预算表直接返回"]
            if budget:
                detail_parts.append(f"预算={format_money(*budget)}")
            if spent:
                detail_parts.append(f"支出={format_money(*spent)}")
            if status:
                detail_parts.append(f"状态={status}")
            return {
                "balance": format_money(remaining, currency),
                "currency": currency,
                "balance_type": "剩余账号预算",
                "balance_status": "可用",
                "balance_detail": "；".join(detail_parts),
            }

    lines = [str(value).strip() for value in payload.get("lines") or [] if str(value).strip()]
    for index, line in enumerate(lines):
        normalized = line.replace("：", ":")
        folded = normalized.casefold()
        if folded in {"当前余额", "可用余额", "余额", "current balance", "available balance", "balance"} or any(
            folded.startswith(prefix) for prefix in (
                "当前余额:", "可用余额:", "余额:", "current balance:", "available balance:", "balance:"
            )
        ):
            inline = normalized.split(":", 1)[1] if ":" in normalized else ""
            money = parse_money(inline) or (parse_money(lines[index + 1]) if index + 1 < len(lines) else None)
            if money:
                return {
                    "balance": format_money(*money),
                    "currency": money[1],
                    "balance_type": "Billing 当前余额",
                    "balance_status": "可用",
                    "balance_detail": "Billing 结算概览直接返回",
                }

    budget = None
    spent = None
    status = ""
    for index, line in enumerate(lines):
        normalized = line.replace("：", ":")
        folded = normalized.casefold()
        if normalized.startswith("预算金额:") or folded.startswith("budget amount:"):
            budget = parse_money(normalized.split(":", 1)[1])
        elif folded in {"支出金额", "已支出", "amount spent", "spend"} or any(
            folded.startswith(prefix) for prefix in ("支出金额:", "已支出:", "amount spent:", "spend:")
        ):
            inline = normalized.split(":", 1)[1] if ":" in normalized else ""
            spent = parse_money(inline) or (parse_money(lines[index + 1]) if index + 1 < len(lines) else None)
        elif normalized.startswith("状态:") or folded.startswith("status:"):
            status = normalized.split(":", 1)[1].strip()
    if budget and spent and budget[1] == spent[1]:
        remaining = budget[0] - spent[0]
        return {
            "balance": format_money(remaining, budget[1]),
            "currency": budget[1],
            "balance_type": "剩余账号预算（计算值）",
            "balance_status": "可用",
            "balance_detail": (
                f"Billing 结算概览：预算 {format_money(*budget)} - 支出 {format_money(*spent)}"
                + (f"；状态={status}" if status else "")
            ),
        }

    unavailable_labels = (
        "无法通过关联的经理账号访问结算设置",
        "没有账号预算",
        "未设置结算",
        "billing setup is unavailable",
        "no account budget",
    )
    reason = next((line for line in lines if any(label in line.casefold() for label in unavailable_labels)), "")
    return {
        "balance": "—",
        "currency": "",
        "balance_type": "未提供",
        "balance_status": "不可用",
        "balance_detail": reason or "此账户的 Billing 页面未提供可识别的账户余额或剩余账号预算",
    }


class CDPPage:
    def __init__(self, target: dict):
        self.url = str(target.get("url") or "")
        self.ws = ws_connect(target["webSocketDebuggerUrl"], max_size=None, open_timeout=15)
        self.counter = 0
        self.pending_events: list[dict] = []

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def _await_response(self, call_id: int, timeout: float) -> dict:
        while True:
            message = json.loads(self.ws.recv(timeout=timeout))
            if message.get("id") != call_id:
                # Domain events (Network.*, Page.*, …) arrive interleaved with
                # command responses; stash them for drain_events().
                if message.get("method"):
                    self.pending_events.append(message)
                continue
            if message.get("error"):
                raise RuntimeError(f"CDP 执行失败: {message['error']}")
            return message.get("result") or {}

    def command(self, method: str, params: dict | None = None, *, timeout: float = 15.0) -> dict:
        """Send any CDP domain command (e.g. Network.enable) and await its result."""
        self.counter += 1
        call_id = self.counter
        payload: dict[str, Any] = {"id": call_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.ws.send(_compact(payload))
        return self._await_response(call_id, timeout)

    def poll_events(self) -> None:
        """Non-blocking pull: move already-arrived socket messages into pending_events.

        Domain events that land while the main flow is sleeping (no command in
        flight) sit in the socket buffer until some command's _await_response
        happens to read them. drain_events() calls this first so those events
        are not stranded.
        """
        while True:
            try:
                message = json.loads(self.ws.recv(timeout=0))
            except Exception:
                # TimeoutError means "nothing buffered"; anything else means the
                # socket is unusable — either way there is nothing more to pull.
                return
            if message.get("method"):
                self.pending_events.append(message)
            # Command responses found here are orphans (we always await
            # responses synchronously); they carry no usable id mapping, so
            # they are dropped.

    def drain_events(self, method_prefix: str = "") -> list[dict]:
        """Return buffered domain events, optionally filtered by method prefix."""
        self.poll_events()
        events, self.pending_events = self.pending_events, []
        if method_prefix:
            return [event for event in events if str(event.get("method") or "").startswith(method_prefix)]
        return events

    def evaluate(self, expression: str, timeout: float = 45.0) -> Any:
        self.counter += 1
        call_id = self.counter
        self.ws.send(_compact({
            "id": call_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
        }))
        result = self._await_response(call_id, timeout)
        if result.get("exceptionDetails"):
            raise RuntimeError(f"页面脚本异常: {result['exceptionDetails']}")
        return (result.get("result") or {}).get("value")

    def rpc(self, path: str, request: dict, *, timeout: float = 60.0) -> Any:
        parsed = urllib.parse.urlsplit(self.url)
        query = urllib.parse.parse_qs(parsed.query)
        session = {
            "hl": (query.get("hl") or ["zh_CN"])[0],
            "ps": "aw",
            "f.sid": "1",
        }
        for key in ("__u", "__c"):
            if query.get(key):
                session[key] = query[key][0]
        if query.get("euid"):
            session["__lu"] = query["euid"][0]
        js = """
        (async (args) => {
          let source = document.documentElement.outerHTML;
          for (const script of document.scripts) source += script.textContent || '';
          const match = source.match(/AA[A-Za-z0-9_\\-]{18,}:\\d{13}/);
          if (!match) return JSON.stringify({status: 0, error: '页面中未找到 XSRF 令牌'});
          const form = new URLSearchParams(args.session);
          form.set('__ar', args.ar);
          const response = await fetch(args.path + '?authuser=0&xt=awn', {
            method: 'POST', credentials: 'include',
            headers: {
              'Content-Type': 'application/x-www-form-urlencoded',
              'X-Same-Domain': '1',
              'X-Framework-Xsrf-Token': match[0]
            },
            body: form.toString()
          });
          return JSON.stringify({status: response.status, text: await response.text()});
        })
        """
        arg = _compact({"path": path, "session": session, "ar": _compact(request)})
        raw = self.evaluate(f"({js})({_compact(json.loads(arg))})", timeout=timeout)
        envelope = json.loads(raw)
        if envelope.get("status") != 200:
            raise RuntimeError(
                f"RPC {path} 失败，HTTP {envelope.get('status')}: "
                f"{envelope.get('error') or envelope.get('text', '')[:500]}"
            )
        payload = _json_response(envelope.get("text") or "")
        if isinstance(payload, dict):
            # List responses legitimately use payload["2"]["2"] for projection
            # descriptors (for example field 3 = "stats.cost"). Service errors
            # live under payload["5"]["2"]; do not reject metric projections.
            service_scope = payload.get("5")
            server_errors = service_scope.get("2") if isinstance(service_scope, dict) else None
            if payload.get("error") or server_errors:
                message = _compact(payload)
                raise RuntimeError(f"Google Ads RPC 拒绝请求: {message[:800]}")
        return payload


def customer_read_request(ocid: str) -> dict:
    return {
        "1": {"3": {"1": ocid}},
        "2": {
            "1": [
                "customer_id",
                "customer_info.external_customer_id",
                "customer_info.descriptive_name",
                "customer_info.status",
                "customer_info.ui_account_status",
                "customer_info.is_hidden",
                "customer_info.is_manager",
                "customer_info.is_fraudster",
            ],
            "2": [{"1": "customer_id", "2": 1, "4": [{"3": ocid}]}],
        },
    }


def client_customer_list_request(scope_ocid: str) -> dict:
    return {
        "1": {"3": {"1": scope_ocid}},
        "2": {
            "1": ["customer_id", "external_customer_id", "descriptive_name", "status", "is_manager"]
        },
    }


def user_by_gaia_request() -> dict:
    """空请求体：读取当前登录身份的 user_id，与任何具体账户 scope 无关。"""
    return {}


def user_customer_access_list_request(user_id: str) -> dict:
    """身份级别 RPC：返回当前登录 Google 身份下可直接访问的全部顶层账户（含 MCC）。

    与 ClientCustomerService/List 不同，这个请求不依赖任何已打开标签页停留在哪个
    具体子账户 scope，因此可以在只打开了某个子账户的窗口里，反查出其所属 MCC。
    """
    return {
        "1": {"3": {}},
        "2": {
            "1": [
                "customer_id",
                "user_id",
                "descriptive_name",
                "external_customer_id",
                "is_manager",
                "creation_time_millis",
                "customer_status",
                "primary_account_type",
            ],
            "2": [{"1": "user_id", "2": 1, "4": [{"3": user_id}]}],
        },
    }


def shared_set_list_request(ocid: str) -> dict:
    return {
        "1": {"3": {"1": ocid}},
        "2": {
            "1": ["shared_set_id", "customer_id", "name", "type", "placement_count", "status"],
            "2": [
                {"1": "customer_id", "2": 1, "4": [{"3": ocid}]},
                {"1": "type", "2": 1, "4": [{"3": "17"}]},
            ],
            "14": True,
        },
    }


def shared_criterion_list_request(ocid: str, shared_set_id: str) -> dict:
    return {
        "1": {"3": {"1": ocid}, "5": "TABLE"},
        "2": {
            "1": ["display_name", "type", "mobile_app_category_id", "website_url", "mobile_app_category", "website"],
            "2": [
                {"1": "shared_set_id", "2": 1, "4": [{"3": shared_set_id}]},
                {"1": "customer_id", "2": 1, "4": [{"3": ocid}]},
            ],
            "14": True,
        },
    }


def create_shared_set_request(ocid: str, name: str) -> dict:
    return {
        "1": {"2": {"1": False}, "3": {"2": ocid}},
        "2": [{
            "1": 1,
            "2": ["name", "type", "shared_set_id"],
            "3": {"1": ocid, "2": "-1", "3": name, "5": 17},
        }],
    }


def rename_shared_set_request(ocid: str, shared_set_id: str, name: str) -> dict:
    return {
        "1": {"2": {"1": False}, "3": {"2": ocid}},
        "2": [{
            "1": 3,
            "2": ["name"],
            "3": {"1": ocid, "2": shared_set_id, "3": name, "5": 17},
        }],
    }


def delete_shared_set_request(ocid: str, shared_set_id: str) -> dict:
    return {
        "1": {"2": {"1": False}, "3": {"2": ocid}},
        "2": [{
            "1": 2,
            "2": ["shared_set_id"],
            "3": {"1": ocid, "2": shared_set_id},
        }],
    }


def add_shared_criteria_request(ocid: str, shared_set_id: str, kind: str, values: list[Any]) -> dict:
    operations = []
    for index, value in enumerate(values, 1):
        entity: dict[str, Any] = {
            "1": ocid, "2": shared_set_id, "3": str(-index),
            "9": 23 if kind == "app" else 28,
        }
        if kind == "app":
            entity["104"] = {"1": int(value)}
            fields = ["shared_set_id", "mobile_app_category"]
        elif kind == "web":
            entity["102"] = {"1": str(value)}
            fields = ["shared_set_id", "website"]
        else:
            raise ValueError(f"未知排除类型: {kind}")
        operations.append({"1": 1, "2": fields, "3": entity})
    return {"1": {"2": {"1": False}, "3": {"2": ocid}}, "2": operations}


def remove_shared_criteria_request(ocid: str, rows: list[dict[str, Any]]) -> dict:
    operations = []
    for row in rows:
        operations.append({
            "1": 2,
            "2": ["criterion_id"],
            "3": {
                "1": ocid,
                "2": str(row["shared_set_id"]),
                "3": str(row["criterion_id"]),
            },
        })
    return {"1": {"2": {"1": False}, "3": {"2": ocid}}, "2": operations}


def custom_audience_mutate_request(ocid: str, name: str, audience_type: int, terms: list[str]) -> dict:
    return {
        "1": {"3": {"2": ocid}},
        "2": [{
            "1": 1,
            "2": ["name", "type", "tokens", "custom_audience_id"],
            "3": {
                "1": "-1", "2": ocid, "3": name,
                "6": [{"2": 1, "3": term} for term in terms],
                "7": audience_type,
            },
        }],
    }


def custom_audience_update_request(
    ocid: str, audience_id: str, name: str, audience_type: int, terms: list[str]
) -> dict:
    return {
        "1": {"3": {"2": ocid}},
        "2": [{
            "1": 3,
            "2": ["name", "type", "tokens"],
            "3": {
                "1": audience_id, "2": ocid, "3": name,
                "6": [{"2": 1, "3": term} for term in terms],
                "7": audience_type,
            },
        }],
    }


def custom_audience_delete_request(ocid: str, audience_id: str) -> dict:
    return {
        "1": {"3": {"2": ocid}},
        "2": [{
            "1": 2,
            "2": ["custom_audience_id"],
            "3": {"1": audience_id, "2": ocid},
        }],
    }


def custom_audience_list_request(ocid: str) -> dict:
    return {
        # List requires both scope_customer_id (1) and operating_customer_id (2).
        "1": {"3": {"1": ocid, "2": ocid}},
        "2": {"1": ["custom_audience_id", "customer_id", "name", "type", "tokens"]},
    }


@dataclass
class AccountPage:
    cid: str
    ocid: str
    page: CDPPage
    name: str = ""
    link_status: int = 0
    customer_status: int = 0
    ui_account_status: int = 0
    status: str = "未知"
    status_detail: str = "尚未读取账户详情"
    is_fraudster: bool = False
    is_manager: bool = False
    parent_mcc_cids: list[str] = field(default_factory=list)
    depth: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "cid": self.cid,
            "ocid": self.ocid,
            "name": self.name,
            "status": self.status,
            "status_detail": self.status_detail,
            "link_status": self.link_status,
            "customer_status": self.customer_status,
            "ui_account_status": self.ui_account_status,
            "is_fraudster": self.is_fraudster,
            "is_manager": self.is_manager,
            "account_type": "MCC" if self.is_manager else "CID",
            "parent_mcc_cids": list(self.parent_mcc_cids),
            "parent_mcc": " / ".join(self.parent_mcc_cids),
            "depth": self.depth,
        }


def classify_account_status(
    link_status: int,
    customer_status: int,
    ui_account_status: int,
    *,
    is_fraudster: bool = False,
    detail_error: str = "",
) -> tuple[str, str]:
    """Translate the internal account enums while preserving their raw values."""
    raw = (
        f"账户状态={customer_status or '未知'}，"
        f"界面状态={ui_account_status or '未知'}，"
        f"MCC关联={link_status or '未知'}，"
        f"风控封禁={'是' if is_fraudster else '否'}"
    )
    if is_fraudster:
        return "封禁", f"Google Ads 账户风控/政策封禁；{raw}"
    if customer_status == 4:
        return "封禁", f"Google Ads 账户已暂停；{raw}"
    if customer_status == 2:
        return "不可用", f"账户已停用；{raw}"
    if customer_status == 3:
        return "不可用", f"账户已取消；{raw}"
    if customer_status == 5:
        return "不可用", f"账户已关闭；{raw}"
    if link_status == 2:
        return "不可用", f"MCC 关联已停用；{raw}"
    if customer_status == 1 and link_status in (0, 1):
        return "可用", f"账户正常；{raw}"
    if detail_error and link_status == 1:
        return "状态未知", f"MCC 关联正常，但账户详情读取失败：{detail_error}；{raw}"
    if detail_error:
        return "读取失败", f"账户详情读取失败：{detail_error}；{raw}"
    return "状态未知", raw


class ProtocolRunner:
    def __init__(
        self,
        profile_id: str,
        log: Callable[[str], None] | None = None,
        *,
        browser_type: str = ADSPOWER,
    ):
        self.profile_id = str(profile_id).strip()
        self.browser_type = normalize_browser_type(browser_type)
        self.log = log or (lambda _message: None)
        self._pages: list[CDPPage] = []
        self._temp_target_ids: list[str] = []
        self._endpoint = ""
        self.report: dict[str, Any] = {}

    def close(self) -> None:
        for page in self._pages:
            page.close()
        self._pages.clear()
        for target_id in self._temp_target_ids:
            close_cdp_target(self._endpoint, target_id)
        self._temp_target_ids.clear()

    def _identity_top_level_scopes(self, endpoint: str) -> tuple[CDPPage | None, list[str]]:
        """Discover every top-level account (incl. MCC) the logged-in identity can access.

        Open tabs only expose whatever scope the user happened to navigate to, so a
        window sitting on one child CID never reveals its parent MCC or sibling
        accounts. The account-switcher page (/nav/selectaccount) is backed by
        UserByGaiaService/Get + UserCustomerAccessService/List, which return the
        full top-level list for the current identity regardless of which tab is
        open. This is best-effort: any failure here just falls back to the
        existing open-tab-only discovery.

        Unlike an /aw/* tab (which has a "selected customer" baked into its session
        and gets AUTH_ERROR_USER_CANNOT_ACCESS_CUSTOMER when queried for a sibling
        scope), this /nav/selectaccount page has no selected customer, so it can
        also serve as the carrier page for querying every newly discovered scope.
        The caller is responsible for closing the returned page/target.
        """
        target = open_cdp_target(endpoint, "https://ads.google.com/nav/selectaccount?authuser=0")
        page = CDPPage(target)
        try:
            gaia_response = None
            last_error: Exception | None = None
            for attempt in range(10):
                if attempt:
                    time.sleep(1.0)
                try:
                    gaia_response = page.rpc("/nav/_/rpc/UserByGaiaService/Get", user_by_gaia_request())
                    break
                except Exception as exc:  # page still loading / xsrf token not rendered yet
                    last_error = exc
            if gaia_response is None:
                raise last_error or RuntimeError("身份识别页面加载超时")
            user_id = str((gaia_response.get("1") or {}).get("2") or "")
            if not user_id:
                raise RuntimeError("UserByGaiaService/Get 未返回 user_id")
            access_response = page.rpc(
                "/nav/_/rpc/UserCustomerAccessService/List",
                user_customer_access_list_request(user_id),
            )
            scopes = [
                str(row.get("1") or "")
                for row in access_response.get("2") or []
                if str(row.get("1") or "").isdigit()
            ]
        except Exception:
            page.close()
            close_cdp_target(endpoint, str(target.get("id") or ""))
            raise
        self._pages.append(page)
        self._temp_target_ids.append(str(target.get("id") or ""))
        return page, scopes

    def discover_accounts(self, wanted_cids: set[str] | None = None) -> dict[str, AccountPage]:
        endpoint = resolve_browser_cdp(self.profile_id, browser_type=self.browser_type)
        self._endpoint = endpoint
        targets = list_google_ads_targets(endpoint)
        if not targets:
            self.log(
                f"[{self.profile_id}] 未发现 Google Ads 页面，正在通过调试协议自动打开"
            )
            targets = ensure_google_ads_targets(endpoint)
        accounts: dict[str, AccountPage] = {}
        errors: list[str] = []
        # Start from every manager scope represented by an open tab, then recurse
        # into each child whose ClientCustomer row says is_manager=true.
        scope_targets: dict[str, dict] = {}
        for target in targets:
            parsed = urllib.parse.urlsplit(str(target.get("url") or ""))
            query = urllib.parse.parse_qs(parsed.query)
            ocid = (query.get("ocid") or query.get("ascid") or [""])[0]
            scope = (query.get("uscid") or [ocid])[0]
            if not ocid.isdigit() or not scope.isdigit():
                continue
            scope_targets.setdefault(scope, target)
        queue: list[tuple[str, CDPPage, str, str, int]] = []
        for scope, target in scope_targets.items():
            page = CDPPage(target)
            self._pages.append(page)
            queue.append((scope, page, "", "", 0))
        try:
            identity_page, identity_scopes = self._identity_top_level_scopes(endpoint)
        except Exception as exc:
            identity_page, identity_scopes = None, []
            errors.append(f"身份级别账户发现失败，仅使用已打开标签页范围: {exc}")
        if identity_page is not None:
            for scope in identity_scopes:
                if scope not in scope_targets:
                    scope_targets[scope] = {}
                    queue.append((scope, identity_page, "", "", 0))
        visited_scopes: set[str] = set()
        while queue:
            scope, page, scope_cid_hint, parent_mcc_cid, scope_depth = queue.pop(0)
            if scope in visited_scopes:
                continue
            if scope_depth > 20:
                errors.append(f"scope={scope}: MCC 层级超过 20，已停止继续展开")
                continue
            try:
                response = page.rpc(
                    "/aw/_/rpc/ClientCustomerService/List",
                    client_customer_list_request(scope),
                )
                visited_scopes.add(scope)
                rows = response.get("1") or []
                scope_cid = next(
                    (
                        str(row.get("3") or "")
                        for row in rows
                        if str(row.get("1") or "") == scope
                    ),
                    scope_cid_hint,
                )
                for row in rows:
                    cid = str(row.get("3") or "")
                    ocid = str(row.get("1") or "")
                    if len(cid) == 10 and cid.isdigit() and ocid.isdigit():
                        is_manager = bool(row.get("4"))
                        is_scope = ocid == scope
                        row_parent = parent_mcc_cid if is_scope else scope_cid
                        row_depth = scope_depth if is_scope else scope_depth + 1
                        existing = accounts.get(cid)
                        if existing:
                            existing.is_manager = existing.is_manager or is_manager
                            existing.depth = min(existing.depth, row_depth)
                            if row_parent and row_parent not in existing.parent_mcc_cids:
                                existing.parent_mcc_cids.append(row_parent)
                        else:
                            accounts[cid] = AccountPage(
                                cid,
                                ocid,
                                page,
                                name=str(row.get("2") or ""),
                                link_status=int(row.get("9") or 0),
                                is_manager=is_manager,
                                parent_mcc_cids=[row_parent] if row_parent else [],
                                depth=row_depth,
                            )
                        if is_manager and not is_scope and ocid not in visited_scopes:
                            queue.append((ocid, page, cid, scope_cid, row_depth))
                if wanted_cids and wanted_cids.issubset(accounts):
                    break
            except Exception as exc:
                errors.append(f"scope={scope}: {exc}")
        if errors and accounts:
            self.log(f"[{self.profile_id}] 部分范围读取失败，已跳过: " + " | ".join(errors))
        if not accounts:
            detail = ("；" + " | ".join(errors[:3])) if errors else ""
            raise RuntimeError("无法从已打开页面读取 CID" + detail)
        enrich_cids = sorted(wanted_cids or set(accounts))
        for cid in enrich_cids:
            account = accounts.get(cid)
            if not account:
                continue
            detail_error = ""
            try:
                response = account.page.rpc(
                    "/aw/_/rpc/CustomerService/List",
                    customer_read_request(account.ocid),
                )
                row = next(iter(response.get("1") or []), {})
                info = row.get("8") or {}
                account.name = str(info.get("2") or account.name or "")
                account.customer_status = int(info.get("13") or 0)
                account.ui_account_status = int(info.get("32") or 0)
                account.is_manager = bool(info.get("7")) or account.is_manager
                account.is_fraudster = bool(info.get("80"))
            except Exception as exc:
                detail_error = str(exc)[:240]
            account.status, account.status_detail = classify_account_status(
                account.link_status,
                account.customer_status,
                account.ui_account_status,
                is_fraudster=account.is_fraudster,
                detail_error=detail_error,
            )
        labels = [
            f"{format_cid(cid)}({('MCC' if accounts[cid].is_manager else 'CID')} / "
            f"{accounts[cid].name or '未命名'} / {accounts[cid].status})"
            for cid in sorted(accounts)
            if not wanted_cids or cid in wanted_cids
        ]
        self.log(("发现指定账户: " if wanted_cids else "发现账户: ") + ", ".join(labels))
        return accounts

    def account_snapshots(
        self, wanted_cids: set[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Read-only account names/statuses; ``None`` returns the whole window scope."""
        try:
            accounts = self.discover_accounts(wanted_cids)
            return {
                cid: account.snapshot()
                for cid, account in accounts.items()
                if wanted_cids is None or cid in wanted_cids
            }
        finally:
            self.close()

    @staticmethod
    def _billing_url(account: AccountPage, route: str) -> str:
        parsed = urllib.parse.urlsplit(account.page.url)
        source = urllib.parse.parse_qs(parsed.query)
        params = {
            key: source[key][0]
            for key in ("euid", "__u", "__c", "authuser", "uscid")
            if source.get(key)
        }
        params.update({
            "ocid": account.ocid,
            "ascid": account.ocid,
            "hl": "zh_CN",
        })
        if not params.get("uscid"):
            params["uscid"] = account.ocid
        return f"https://ads.google.com{route}?{urllib.parse.urlencode(params)}"

    def _billing_payload(self, account: AccountPage, route: str) -> dict[str, Any]:
        if not self._endpoint:
            raise RuntimeError("尚未连接 AdsPower CDP")
        target = open_cdp_target(self._endpoint, self._billing_url(account, route))
        page = CDPPage(target)
        payload: dict[str, Any] = {}
        expression = r"""
        (() => {
          const lines = (document.body?.innerText || '')
            .split(/\n+/).map(value => value.trim()).filter(Boolean).slice(0, 320);
          const rows = [...document.querySelectorAll('[role="row"]')]
            .map(row => ({
              headers: [...row.querySelectorAll('[role="columnheader"]')]
                .map(cell => cell.innerText.trim()),
              cells: [...row.querySelectorAll('[role="cell"],[role="gridcell"]')]
                .map(cell => cell.innerText.trim())
            }))
            .filter(row => row.headers.length || row.cells.length).slice(0, 120);
          return {url: location.href, title: document.title, lines, rows};
        })()
        """
        try:
            for attempt in range(12):
                if attempt:
                    time.sleep(0.4)
                payload = page.evaluate(expression, timeout=20) or {}
                extracted = extract_billing_balance(payload)
                if extracted.get("balance_status") == "可用":
                    return payload
                # Billing is rendered in stages. Navigation and placeholder rows
                # appear before the account-budget values, so the presence of any
                # grid cell (or the copyright footer) does not mean loading is done.
                # Keep polling until a trustworthy amount is parsed or the bounded
                # timeout is exhausted.
            return payload
        finally:
            page.close()
            close_cdp_target(self._endpoint, str(target.get("id") or ""))

    def _account_balance(self, account: AccountPage) -> dict[str, str]:
        summary_payload = self._billing_payload(account, "/aw/billing/summary")
        summary = extract_billing_balance(summary_payload)
        if summary.get("balance_status") == "可用":
            return summary
        self.log(
            f"[{format_cid(account.cid)}] 结算概览未识别余额，继续读取账户预算页（只读）"
        )
        budgets = extract_billing_balance(
            self._billing_payload(account, "/aw/billing/budgets")
        )
        if budgets.get("balance_status") == "可用":
            return budgets
        return {
            **summary,
            "balance_detail": (
                f"结算概览：{summary.get('balance_detail') or '未识别'}；"
                f"账户预算页：{budgets.get('balance_detail') or '未识别'}"
            ),
        }

    def require_positive_account_balance(
        self, account: AccountPage, *, action: str = "广告创建",
    ) -> dict[str, str]:
        """Re-read Billing immediately before a write and enforce balance > 0."""
        cid_display = format_cid(account.cid)
        self.log(f"[{cid_display}] {action}前读取目标 CID 余额（只读）")
        try:
            result = self._account_balance(account)
        except Exception as exc:
            raise PermissionError(
                f"CID {cid_display} {action}要求余额可识别且大于 0；余额读取失败：{exc}"
            ) from exc
        amount, currency = require_positive_balance(result, cid=account.cid, action=action)
        self.log(
            f"[{cid_display}] 余额门槛通过：{format_money(amount, currency)} > 0，可以{action}"
        )
        return result

    def account_balances(
        self,
        wanted_cids: set[str] | None = None,
    ) -> dict[str, dict[str, str]]:
        """Read account-level Billing balances without modifying Google Ads."""
        try:
            accounts = self.discover_accounts(wanted_cids)
            results: dict[str, dict[str, str]] = {}
            for cid in sorted(accounts):
                account = accounts[cid]
                if account.is_manager or (wanted_cids is not None and cid not in wanted_cids):
                    continue
                self.log(f"[{format_cid(cid)}] 读取 Billing 账户余额（只读）")
                try:
                    result = self._account_balance(account)
                except Exception as exc:
                    result = {
                        "balance": "—",
                        "currency": "",
                        "balance_type": "读取失败",
                        "balance_status": "读取失败",
                        "balance_detail": str(exc),
                    }
                result.update({"cid": cid, "ocid": account.ocid})
                results[cid] = result
                self.log(
                    f"[{format_cid(cid)}] 余额：{result.get('balance')} · "
                    f"{result.get('balance_type')} · {result.get('balance_status')}"
                )
            return results
        finally:
            self.close()

    def _account_daily_spend(
        self, account: AccountPage, report_date: date | ReportPeriod | None = None,
    ) -> dict[str, Any]:
        period = coerce_report_period(report_date or datetime.now().astimezone().date())
        response = account.page.rpc(
            CAMPAIGN_LIST_PATH,
            campaign_daily_spend_request(account.ocid, period),
        )
        return parse_daily_spend(response, expected_ocid=account.ocid, report_date=period)

    # ---------------------------------------------------------- 系列管控
    def campaigns_overview(self, wanted_cids: set[str] | None = None) -> dict[str, dict[str, Any]]:
        """读取各账户的广告系列列表（状态/预算/出价类型）。"""
        try:
            accounts = self.discover_accounts(wanted_cids)
            results: dict[str, dict[str, Any]] = {}
            for cid in sorted(accounts):
                account = accounts[cid]
                if account.is_manager or (wanted_cids is not None and cid not in wanted_cids):
                    continue
                try:
                    response = account.page.rpc(
                        CAMPAIGN_LIST_PATH,
                        campaign_list_request(account.ocid),
                    )
                    campaigns = parse_campaign_list(response, expected_ocid=account.ocid)
                    results[cid] = {"cid": cid, "ocid": account.ocid, "campaigns": campaigns}
                    self.log(f"[{format_cid(cid)}] 系列 {len(campaigns)} 个")
                except Exception as exc:
                    results[cid] = {"cid": cid, "ocid": account.ocid, "campaigns": [], "error": str(exc)}
                    self.log(f"[{format_cid(cid)}] 系列读取失败: {exc}")
            return results
        finally:
            self.close()

    def set_campaigns_status(
        self,
        wanted_cids: set[str],
        target_status: int,
        *,
        campaign_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """批量暂停/启用系列；写后逐个读回校验。campaign_ids=None 时操作全部可改系列。"""
        if target_status not in CAMPAIGN_STATUS_LABELS:
            raise ValueError(f"非法目标状态: {target_status}")
        label = CAMPAIGN_STATUS_LABELS[target_status]
        try:
            accounts = self.discover_accounts(wanted_cids)
            results: dict[str, Any] = {}
            for cid in sorted(accounts):
                account = accounts[cid]
                if account.is_manager or cid not in wanted_cids:
                    continue
                entry = {"changed": [], "skipped": [], "failed": []}
                try:
                    response = account.page.rpc(
                        CAMPAIGN_LIST_PATH,
                        campaign_list_request(account.ocid),
                    )
                    campaigns = parse_campaign_list(response, expected_ocid=account.ocid)
                    for camp in campaigns:
                        if campaign_ids is not None and camp["campaign_id"] not in campaign_ids:
                            continue
                        if camp["status"] == CAMPAIGN_STATUS_REMOVED:
                            entry["skipped"].append({**camp, "reason": "已移除不可改"})
                            continue
                        if camp["status"] == target_status:
                            entry["skipped"].append({**camp, "reason": f"已是{label}"})
                            continue
                        try:
                            payload = account.page.rpc(
                                CAMPAIGN_STATUS_MUTATE_PATH,
                                set_campaign_status_request(account.ocid, camp["campaign_id"], target_status),
                            )
                            errors = mutate_service_errors(payload)
                            if errors:
                                entry["failed"].append({**camp, "error": "; ".join(errors)})
                                continue
                            # 读回校验
                            time.sleep(0.6)
                            verify = parse_campaign_list(
                                account.page.rpc(
                                    CAMPAIGN_LIST_PATH,
                                    campaign_list_request(account.ocid),
                                ),
                                expected_ocid=account.ocid,
                            )
                            current = next((c for c in verify if c["campaign_id"] == camp["campaign_id"]), None)
                            if current and current["status"] == target_status:
                                entry["changed"].append(camp["name"])
                                self.log(f"[{format_cid(cid)}] {camp['name']} → {label} ✔")
                            else:
                                entry["failed"].append({**camp, "error": "写后读回状态未变"})
                        except Exception as exc:
                            entry["failed"].append({**camp, "error": str(exc)})
                except Exception as exc:
                    entry["failed"].append({"name": "<列表读取>", "error": str(exc)})
                results[cid] = entry
                self.log(
                    f"[{format_cid(cid)}] 系列{label}：改 {len(entry['changed'])} / "
                    f"跳过 {len(entry['skipped'])} / 失败 {len(entry['failed'])}"
                )
            return results
        finally:
            self.close()

    def account_daily_spends(
        self,
        wanted_cids: set[str] | None = None,
        report_date: date | ReportPeriod | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Read account spend for one day or an explicit report range."""
        period = coerce_report_period(report_date or datetime.now().astimezone().date())
        try:
            accounts = self.discover_accounts(wanted_cids)
            results: dict[str, dict[str, Any]] = {}
            for cid in sorted(accounts):
                account = accounts[cid]
                if account.is_manager or (wanted_cids is not None and cid not in wanted_cids):
                    continue
                self.log(f"[{format_cid(cid)}] 读取 {period.label}日期消耗（只读）")
                try:
                    result = self._account_daily_spend(account, period)
                except Exception as exc:
                    result = {
                        "daily_spend": "—",
                        "daily_spend_amount": "",
                        "daily_spend_currency": "",
                        "daily_spend_status": "读取失败",
                        "daily_spend_date": period.label,
                        "daily_spend_detail": str(exc),
                        "daily_spend_campaigns": [],
                    }
                result.update({"cid": cid, "ocid": account.ocid})
                results[cid] = result
                self.log(
                    f"[{format_cid(cid)}] {period.label}消耗：{result.get('daily_spend')} · "
                    f"{result.get('daily_spend_status')}"
                )
            return results
        finally:
            self.close()

    def run(
        self,
        assignments: list[Assignment],
        config: dict[str, Any],
        enabled: set[str],
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        self.report = {
            "version": 1,
            "profile_id": self.profile_id,
            "mode": "preflight" if dry_run else "write",
            "started_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "accounts": [],
        }
        try:
            # Resolve every reference and validate content before opening CDP or
            # sending even read-only requests.
            prepared = {
                assignment.cid: (
                    resolve_named_dictionary(config, assignment.dictionary),
                    resolve_object_names(config, assignment.dictionary),
                )
                for assignment in assignments
            }
            wanted_cids = {assignment.cid for assignment in assignments}
            accounts = self.discover_accounts(wanted_cids)
            self.report["discovered_accounts"] = [
                accounts[cid].snapshot() for cid in sorted(accounts)
            ]
            for assignment in assignments:
                account = accounts.get(assignment.cid)
                if not account:
                    raise RuntimeError(
                        f"CID {format_cid(assignment.cid)} 不在当前窗口的可访问账户范围内；"
                        "请在该 AdsPower 窗口打开其所属 MCC 或账户页面后重试"
                    )
                content, object_names = prepared[assignment.cid]
                account_report = {
                    "cid": assignment.cid,
                    "ocid": account.ocid,
                    "account_name": account.name,
                    "account_type": "MCC" if account.is_manager else "CID",
                    "is_manager": account.is_manager,
                    "parent_mcc_cids": list(account.parent_mcc_cids),
                    "depth": account.depth,
                    "account_status": account.status,
                    "account_status_detail": account.status_detail,
                    "account_status_codes": {
                        "customer": account.customer_status,
                        "ui": account.ui_account_status,
                        "mcc_link": account.link_status,
                        "is_fraudster": account.is_fraudster,
                    },
                    "dictionary": assignment.dictionary,
                    "actions": [],
                }
                self.report["accounts"].append(account_report)
                if account.is_manager:
                    reason = "该账户是 MCC 管理账户，仅用于发现下级 CID，不执行内容写入"
                    account_report["actions"].append({
                        "kind": "account",
                        "result": "skipped",
                        "reason": reason,
                    })
                    self.log(f"[{format_cid(account.cid)}] {reason}")
                    continue
                if not dry_run and account.status != "可用":
                    reason = f"账户状态为{account.status}，已跳过全部写入：{account.status_detail}"
                    account_report["actions"].append({
                        "kind": "account",
                        "result": "skipped",
                        "reason": reason,
                    })
                    self.log(f"[{format_cid(account.cid)}] {reason}")
                    continue
                self._process_account(
                    account, content, object_names, enabled, account_report["actions"], dry_run=dry_run
                )
            self.report["success"] = True
            self.report["finished_at"] = datetime.now(timezone.utc).astimezone().isoformat()
            action_count = sum(len(item["actions"]) for item in self.report["accounts"])
            self.log(f"汇总：{len(assignments)} 个 CID，{action_count} 项处理结果")
            return self.report
        except Exception as exc:
            self.report["success"] = False
            self.report["error"] = str(exc)
            self.report["finished_at"] = datetime.now(timezone.utc).astimezone().isoformat()
            raise
        finally:
            self.close()

    def _process_account(
        self,
        account: AccountPage,
        content: dict[str, list[Any]],
        object_names: dict[str, str],
        enabled: set[str],
        actions: list[dict[str, Any]],
        *,
        dry_run: bool,
    ) -> None:
        self.log(
            f"[{format_cid(account.cid)}] {account.name or '未命名账户'} · {account.status} · "
            f"内部账户 {account.ocid}"
        )
        if "app" in enabled:
            app_ids = [int(value) for value in strip_content_comments(content["app"])]
            unknown = sorted(set(app_ids) - set(ALL_APP_CATEGORY_IDS))
            if unknown:
                raise ValueError(f"app 包含未知类别 ID: {unknown[:5]}")
            actions.append(self._ensure_shared_list(account, "app", object_names["app"], app_ids, dry_run))
        if "web" in enabled:
            actions.append(self._ensure_shared_list(
                account, "web", object_names["web"], normalize_websites(content["web"]), dry_run
            ))
        if "xq" in enabled:
            actions.append(self._ensure_audience(
                account, "xq", object_names["xq"], CUSTOM_AUDIENCE_TYPE_BY_KIND["xq"],
                normalize_terms(content["xq"], "xq"), dry_run
            ))
        if "ss" in enabled:
            actions.append(self._ensure_audience(
                account, "ss", object_names["ss"], CUSTOM_AUDIENCE_TYPE_BY_KIND["ss"],
                normalize_terms(content["ss"], "ss"), dry_run
            ))

    def _shared_sets(self, account: AccountPage) -> dict[str, str]:
        response = account.page.rpc(
            "/aw_dva/_/rpc/SharedSetService/List",
            shared_set_list_request(account.ocid),
        )
        return {
            str(row.get("3")): str(row.get("2"))
            for row in (response.get("1") or [])
            if row.get("2") and row.get("3")
        }

    def _find_shared_set_with_retry(self, account: AccountPage, name: str) -> str | None:
        for attempt in range(5):
            shared_set_id = self._shared_sets(account).get(name)
            if shared_set_id:
                return shared_set_id
            if attempt < 4:
                time.sleep(0.7 * (attempt + 1))
        return None

    def _criterion_values(self, account: AccountPage, shared_set_id: str, kind: str) -> set[Any]:
        response = account.page.rpc(
            "/aw_dva/_/rpc/SharedCriterionService/List",
            shared_criterion_list_request(account.ocid, shared_set_id),
        )
        existing: set[Any] = set()
        for row in response.get("1") or []:
            if kind == "app":
                value = row.get("7") or (row.get("104") or {}).get("1")
                if value is not None:
                    existing.add(int(value))
            else:
                value = row.get("8") or (row.get("102") or {}).get("1")
                if value:
                    existing.add(str(value).lower())
        return existing

    def _ensure_shared_list(
        self,
        account: AccountPage,
        kind: str,
        name: str,
        desired: list[Any],
        dry_run: bool,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": kind, "object_name": name, "desired": len(desired)}
        if not desired:
            self.log(f"[{format_cid(account.cid)}] {name}: 内容为空，跳过")
            result.update(status="empty", added=0)
            return result
        shared_sets = self._shared_sets(account)
        shared_set_id = shared_sets.get(name)
        if not shared_set_id:
            if dry_run:
                self.log(f"[{format_cid(account.cid)}] 将创建排除列表 {name}，共 {len(desired)} 项")
                result.update(status="would_create", added=len(desired))
                return result
            account.page.rpc(
                "/aw_dva/_/rpc/SharedSetService/Mutate",
                create_shared_set_request(account.ocid, name),
            )
            shared_set_id = self._find_shared_set_with_retry(account, name)
            if not shared_set_id:
                raise RuntimeError(f"创建排除列表 {name} 后未能读回列表 ID")
            self.log(f"[{format_cid(account.cid)}] 已创建排除列表 {name}")
            result["created_list"] = True

        existing = self._criterion_values(account, shared_set_id, kind)
        missing = [value for value in desired if value not in existing]
        if not missing:
            self.log(f"[{format_cid(account.cid)}] {name}: 已完整存在，无需修改")
            result.update(status="unchanged", added=0, existing=len(existing))
            return result
        if dry_run:
            self.log(f"[{format_cid(account.cid)}] {name}: 将新增 {len(missing)} 项")
            result.update(status="would_add", added=len(missing), existing=len(existing))
            return result
        # Keep requests comfortably below browser RPC body limits.
        for start in range(0, len(missing), 100):
            batch = missing[start:start + 100]
            account.page.rpc(
                "/aw_dva/_/rpc/SharedCriterionService/Mutate",
                add_shared_criteria_request(account.ocid, shared_set_id, kind, batch),
            )
        verified: set[Any] = set()
        for attempt in range(4):
            verified = self._criterion_values(account, shared_set_id, kind)
            if all(value in verified for value in desired):
                break
            if attempt < 3:
                time.sleep(0.8 * (attempt + 1))
        unverified = [value for value in desired if value not in verified]
        if unverified:
            raise RuntimeError(
                f"{name} 写入后校验失败，仍缺少 {len(unverified)} 项；"
                f"示例: {unverified[:3]}"
            )
        self.log(f"[{format_cid(account.cid)}] {name}: 已新增 {len(missing)} 项")
        result.update(status="added", added=len(missing), verified_total=len(verified))
        return result

    def _existing_audiences(self, account: AccountPage) -> dict[str, dict[str, Any]]:
        response = account.page.rpc(
            "/aw_dva/_/rpc/CustomAudienceService/List",
            custom_audience_list_request(account.ocid),
        )
        rows = response.get("1") or [] if isinstance(response, dict) else []
        return {
            str(row.get("3")): row
            for row in rows
            if isinstance(row, dict) and row.get("3")
        }

    def _ensure_audience(
        self,
        account: AccountPage,
        kind: str,
        name: str,
        audience_type: int,
        terms: list[str],
        dry_run: bool,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": kind, "object_name": name, "desired": len(terms)}
        if not terms:
            self.log(f"[{format_cid(account.cid)}] {name}: 内容为空，跳过")
            result.update(status="empty", added=0)
            return result
        existing = self._existing_audiences(account)
        if name in existing:
            current_type = int(existing[name].get("7") or 0)
            suffix = "" if current_type == audience_type else f"，但类型为 {current_type}"
            self.log(f"[{format_cid(account.cid)}] 自定义受众 {name} 已存在{suffix}，跳过（不会覆盖）")
            result.update(status="exists", added=0, existing_type=current_type)
            return result
        if dry_run:
            self.log(f"[{format_cid(account.cid)}] 将创建自定义受众 {name}，共 {len(terms)} 项")
            result.update(status="would_create", added=len(terms))
            return result
        account.page.rpc(
            "/aw_displayads/audiencecenter/_/rpc/CustomAudienceService/Mutate",
            custom_audience_mutate_request(account.ocid, name, audience_type, terms),
        )
        verified = None
        for attempt in range(5):
            verified = self._existing_audiences(account).get(name)
            if verified:
                break
            if attempt < 4:
                time.sleep(0.8 * (attempt + 1))
        if not verified:
            raise RuntimeError(f"自定义受众 {name} 写入后未能读回，校验失败")
        if int(verified.get("7") or 0) != audience_type:
            raise RuntimeError(f"自定义受众 {name} 写入后类型校验失败")
        self.log(f"[{format_cid(account.cid)}] 已创建自定义受众 {name}，共 {len(terms)} 项")
        result.update(status="created", added=len(terms), audience_id=str(verified.get("1") or ""))
        return result


class RemoteManager:
    """One-account CRUD facade used by the desktop management pages."""

    def __init__(
        self,
        profile_id: str,
        cid: str,
        log: Callable[[str], None] | None = None,
        *,
        browser_type: str = ADSPOWER,
    ):
        self.runner = ProtocolRunner(profile_id, log, browser_type=browser_type)
        self.cid = normalize_cid(cid)
        accounts = self.runner.discover_accounts({self.cid})
        account = accounts.get(self.cid)
        if not account:
            self.runner.close()
            raise RuntimeError(f"CID {format_cid(self.cid)} 不在当前窗口的可访问账户范围内")
        self.account = account

    def close(self) -> None:
        self.runner.close()

    def __enter__(self) -> "RemoteManager":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _shared_rows(self) -> list[dict[str, Any]]:
        response = self.account.page.rpc(
            "/aw_dva/_/rpc/SharedSetService/List",
            shared_set_list_request(self.account.ocid),
        )
        return list(response.get("1") or [])

    def _criterion_rows(self, shared_set_id: str) -> list[dict[str, Any]]:
        response = self.account.page.rpc(
            "/aw_dva/_/rpc/SharedCriterionService/List",
            shared_criterion_list_request(self.account.ocid, shared_set_id),
        )
        rows: list[dict[str, Any]] = []
        for raw in response.get("1") or []:
            criterion_type = int(raw.get("9") or 0)
            if criterion_type == 23:
                value = raw.get("7") or (raw.get("104") or {}).get("1")
                kind = "app"
                value = int(value) if value is not None else None
            elif criterion_type == 28:
                value = raw.get("8") or (raw.get("102") or {}).get("1")
                kind = "web"
                value = str(value).lower() if value else None
            else:
                kind, value = "other", raw.get("12")
            rows.append({
                "shared_set_id": str(raw.get("2") or shared_set_id),
                "criterion_id": str(raw.get("3") or ""),
                "kind": kind,
                "value": value,
                "display_name": str(raw.get("12") or value or ""),
            })
        return rows

    def list_objects(self) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        for row in self._shared_rows():
            shared_set_id = str(row.get("2") or "")
            criteria = self._criterion_rows(shared_set_id)
            kinds = {item["kind"] for item in criteria if item["kind"] != "other"}
            kind = next(iter(kinds)) if len(kinds) == 1 else "mixed"
            objects.append({
                "group": "exclusion",
                "kind": kind,
                "name": str(row.get("3") or ""),
                "id": shared_set_id,
                "count": len(criteria),
                "values": [item["value"] for item in criteria if item["value"] is not None],
                "criteria": criteria,
            })
        audiences = self.runner._existing_audiences(self.account)
        for name, row in audiences.items():
            audience_type = int(row.get("7") or 0)
            kind = CUSTOM_AUDIENCE_KIND_BY_TYPE.get(audience_type, "audience")
            terms = [str(token.get("3") or "") for token in (row.get("6") or []) if token.get("3")]
            objects.append({
                "group": "audience",
                "kind": kind,
                "name": name,
                "id": str(row.get("1") or ""),
                "count": len(terms),
                "values": terms,
                "audience_type": audience_type,
            })
        return objects

    def create_object(self, kind: str, name: str, values: list[Any]) -> None:
        name = name.strip()
        if not name:
            raise ValueError("名称不能为空")
        existing = self.list_objects()
        if any(item["name"] == name and item["group"] == ("exclusion" if kind in {"app", "web"} else "audience") for item in existing):
            raise ValueError(f"同类对象中已经存在名称 {name}")
        if kind in {"app", "web"}:
            desired = self._normalize_values(kind, values)
            if not desired:
                raise ValueError("排除内容不能为空")
            self.account.page.rpc(
                "/aw_dva/_/rpc/SharedSetService/Mutate",
                create_shared_set_request(self.account.ocid, name),
            )
            shared_set_id = self.runner._find_shared_set_with_retry(self.account, name)
            if not shared_set_id:
                raise RuntimeError("创建后未能读回排除列表")
            for start in range(0, len(desired), 100):
                self.account.page.rpc(
                    "/aw_dva/_/rpc/SharedCriterionService/Mutate",
                    add_shared_criteria_request(
                        self.account.ocid, shared_set_id, kind, desired[start:start + 100]
                    ),
                )
            self._verify_shared(shared_set_id, name, kind, desired)
        elif kind in {"xq", "ss"}:
            terms = normalize_terms(values, kind)
            if not terms:
                raise ValueError("受众内容不能为空")
            audience_type = CUSTOM_AUDIENCE_TYPE_BY_KIND[kind]
            self.account.page.rpc(
                "/aw_displayads/audiencecenter/_/rpc/CustomAudienceService/Mutate",
                custom_audience_mutate_request(self.account.ocid, name, audience_type, terms),
            )
            self._verify_audience(name, audience_type, terms)
        else:
            raise ValueError(f"不支持的对象类型: {kind}")

    def update_object(self, item: dict[str, Any], new_name: str, values: list[Any]) -> None:
        kind = str(item["kind"])
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("名称不能为空")
        if kind in {"app", "web"}:
            shared_set_id = str(item["id"])
            desired = self._normalize_values(kind, values)
            if not desired:
                raise ValueError("排除内容不能为空")
            if new_name != item["name"]:
                self.account.page.rpc(
                    "/aw_dva/_/rpc/SharedSetService/Mutate",
                    rename_shared_set_request(self.account.ocid, shared_set_id, new_name),
                )
            current_rows = self._criterion_rows(shared_set_id)
            current = {row["value"]: row for row in current_rows if row["kind"] == kind}
            desired_set = set(desired)
            remove_rows = [row for value, row in current.items() if value not in desired_set]
            add_values = [value for value in desired if value not in current]
            for start in range(0, len(remove_rows), 100):
                self.account.page.rpc(
                    "/aw_dva/_/rpc/SharedCriterionService/Mutate",
                    remove_shared_criteria_request(self.account.ocid, remove_rows[start:start + 100]),
                )
            for start in range(0, len(add_values), 100):
                self.account.page.rpc(
                    "/aw_dva/_/rpc/SharedCriterionService/Mutate",
                    add_shared_criteria_request(
                        self.account.ocid, shared_set_id, kind, add_values[start:start + 100]
                    ),
                )
            self._verify_shared(shared_set_id, new_name, kind, desired)
        elif kind in {"xq", "ss"}:
            terms = normalize_terms(values, kind)
            if not terms:
                raise ValueError("受众内容不能为空")
            audience_type = CUSTOM_AUDIENCE_TYPE_BY_KIND[kind]
            self.account.page.rpc(
                "/aw_displayads/audiencecenter/_/rpc/CustomAudienceService/Mutate",
                custom_audience_update_request(
                    self.account.ocid, str(item["id"]), new_name, audience_type, terms
                ),
            )
            self._verify_audience(new_name, audience_type, terms)
        else:
            raise ValueError("混合排除列表暂不支持直接编辑，请先在 Google Ads 中拆分")

    def delete_object(self, item: dict[str, Any]) -> None:
        if item["group"] == "exclusion":
            self.account.page.rpc(
                "/aw_dva/_/rpc/SharedSetService/Mutate",
                delete_shared_set_request(self.account.ocid, str(item["id"])),
            )
            for attempt in range(5):
                if all(str(row.get("2")) != str(item["id"]) for row in self._shared_rows()):
                    return
                time.sleep(0.8 * (attempt + 1))
            raise RuntimeError("删除后仍能读到排除列表，校验失败")
        self.account.page.rpc(
            "/aw_displayads/audiencecenter/_/rpc/CustomAudienceService/Mutate",
            custom_audience_delete_request(self.account.ocid, str(item["id"])),
        )
        for attempt in range(5):
            existing = self.runner._existing_audiences(self.account)
            if all(str(row.get("1")) != str(item["id"]) for row in existing.values()):
                return
            time.sleep(0.8 * (attempt + 1))
        raise RuntimeError("删除后仍能读到自定义受众，校验失败")

    @staticmethod
    def _normalize_values(kind: str, values: list[Any]) -> list[Any]:
        if kind == "app":
            result = list(dict.fromkeys(int(value) for value in strip_content_comments(values)))
            unknown = set(result) - set(ALL_APP_CATEGORY_IDS)
            if unknown:
                raise ValueError(f"未知应用类别 ID: {sorted(unknown)[:5]}")
            return result
        return normalize_websites(values)

    def _verify_shared(self, shared_set_id: str, name: str, kind: str, desired: list[Any]) -> None:
        for attempt in range(5):
            names = {str(row.get("2")): str(row.get("3")) for row in self._shared_rows()}
            rows = self._criterion_rows(shared_set_id)
            actual = {row["value"] for row in rows if row["kind"] == kind}
            if names.get(shared_set_id) == name and actual == set(desired):
                return
            time.sleep(0.8 * (attempt + 1))
        raise RuntimeError("排除列表写入后内容或名称校验失败")

    def _verify_audience(self, name: str, audience_type: int, terms: list[str]) -> None:
        for attempt in range(5):
            row = self.runner._existing_audiences(self.account).get(name)
            actual = [str(token.get("3") or "") for token in (row or {}).get("6", []) if token.get("3")]
            if row and int(row.get("7") or 0) == audience_type and set(actual) == set(terms):
                return
            time.sleep(0.8 * (attempt + 1))
        raise RuntimeError("自定义受众写入后内容或名称校验失败")
