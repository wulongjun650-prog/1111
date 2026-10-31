from __future__ import annotations

import time
import json
from decimal import Decimal
from typing import Any, Callable

from .browser_provider import ADSPOWER, normalize_browser_type, window_key
from .config import (
    AccountCacheLoadResult,
    account_cache_metadata,
    format_cid,
    normalize_cid,
    normalize_warmup_template,
)
from .protocol import (
    AccountPage,
    CAMPAIGN_STATUS_ENABLED,
    CAMPAIGN_STATUS_PAUSED,
    CAMPAIGN_STATUS_REMOVED,
    ProtocolRunner,
    parse_money,
)
from .protocol_catalog import CAMPAIGN_STATUS_MUTATE_PATH


# A reference account is operational configuration, not protocol data.  Keep no
# built-in customer ID here: a stale or suspended account must never be selected
# implicitly by a new build.
SOURCE_CID = ""
SOURCE_CAMPAIGN_NAME = "App-1"
APP_CHANNEL = 7
CAMPAIGN_ENABLED = CAMPAIGN_STATUS_ENABLED
CAMPAIGN_PAUSED = CAMPAIGN_STATUS_PAUSED
CAMPAIGN_REMOVED = CAMPAIGN_STATUS_REMOVED
DEFAULT_DAILY_BUDGET = Decimal("2.00")


def build_warmup_template_dry_run(
    template: dict[str, Any],
    targets: list[dict[str, Any]],
    cache: AccountCacheLoadResult,
) -> dict[str, Any]:
    """Build a deterministic local preview without opening AdsPower or Google Ads."""
    fixed = normalize_warmup_template(template)
    budget = Decimal(fixed["target_daily_budget"])
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in targets:
        profile_id = str(raw.get("profile_id") or "").strip()
        browser_type = normalize_browser_type(raw.get("browser_type"))
        profile_key = window_key(browser_type, profile_id) if profile_id else ""
        cid = normalize_cid(str(raw.get("cid") or ""))
        key = (profile_key, cid)
        if not profile_id or key in seen:
            continue
        seen.add(key)
        cached_snapshot = cache.accounts.get(profile_key, {}).get(cid)
        cached_basics = account_cache_metadata(cache, profile_key, cid)
        if isinstance(cached_snapshot, dict) and cached_basics is not None:
            account_type = cached_basics["account_type"].strip().upper() or "CID"
            account_name = cached_basics["account_name"]
            account_status = cached_basics["account_status"]
            window_number = cached_basics["window_number"]
            data_source = "统一账户缓存"
            if account_type == "MCC" or bool(cached_snapshot.get("is_manager")):
                eligible = False
                reason = "MCC 管理账户不能作为投放目标"
            elif account_status != "可用":
                eligible = False
                reason = f"缓存账户状态为“{account_status}”，演练已阻断"
            else:
                eligible = True
                reason = "账户基础状态可用；正式执行前仍须只读核验余额、币种、权限和系列"
        elif cache.fresh:
            account_type = str(raw.get("account_type") or "CID").strip().upper()
            account_name = str(raw.get("account_name") or "")
            account_status = str(raw.get("account_status") or raw.get("status") or "状态未知")
            window_number = str(raw.get("window_number") or "")
            data_source = "界面保存数据（缓存缺少目标）"
            eligible = False
            reason = "本次完整账户缓存中没有该目标；请先在批量任务主动刷新账户列表与状态"
        else:
            account_type = str(raw.get("account_type") or "CID").strip().upper()
            account_name = str(raw.get("account_name") or "")
            account_status = str(raw.get("account_status") or raw.get("status") or "状态未知")
            window_number = str(raw.get("window_number") or "")
            data_source = f"界面保存数据（统一缓存 {cache.status}）"
            eligible = False
            reason = "统一账户缓存不可用或已过期；演练只展示范围，真实执行前必须主动刷新"
        rows.append({
            "browser_type": browser_type,
            "profile_id": profile_id,
            "window_number": window_number,
            "cid": cid,
            "cid_display": format_cid(cid),
            "account_name": account_name,
            "account_status": account_status,
            "account_type": account_type,
            "data_source": data_source,
            "eligible_for_scan": eligible,
            "planned_action": (
                f"只读扫描余额/币种/权限/{fixed['campaign_name']} → 条件满足时复制或复用暂停系列"
                f" → 写入并回读预算 USD {budget:.2f} → 启用并回读（本次均未执行）"
            ),
            "reason": reason,
        })
    eligible_count = sum(bool(row["eligible_for_scan"]) for row in rows)
    refreshed_at = str((cache.payload or {}).get("refreshed_at") or "")
    expires_at = str((cache.payload or {}).get("expires_at") or "")
    return {
        "version": 1,
        "mode": "warmup_template_dry_run",
        "dry_run": True,
        "account_accessed": False,
        "writes_executed": False,
        "template": fixed,
        "cache": {
            "status": cache.status,
            "refreshed_at": refreshed_at,
            "expires_at": expires_at,
        },
        "target_count": len(rows),
        "eligible_for_scan_count": eligible_count,
        "target_daily_budget": f"{budget:.2f}",
        "selected_theoretical_daily_budget": f"{budget * len(rows):.2f}",
        "eligible_theoretical_daily_budget": f"{budget * eligible_count:.2f}",
        "targets": rows,
    }


def validate_daily_budget(value: Decimal | str | int | float) -> Decimal:
    try:
        amount = Decimal(str(value).strip())
    except Exception as exc:
        raise ValueError("目标日预算必须是有效数字") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("目标日预算必须大于 0")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("目标日预算最多保留两位小数")
    return amount.quantize(Decimal("0.01"))


def campaign_detail_request(ocid: str) -> dict[str, Any]:
    return {
        "1": {"3": {"1": str(ocid)}},
        "2": {
            "1": [
                "campaign_id", "name", "status", "advertising_channel_type",
                "budget_id", "budget_name", "budget_amount", "currency_code",
                "primary_status", "primary_status_reasons",
            ],
            "14": True,
        },
    }


def parse_campaign_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in payload.get("1") or []:
        if not isinstance(raw, dict) or not raw.get("2"):
            continue
        status_raw = int(raw.get("12") or 0)
        amount_micros = int(raw.get("17") or 0)
        rows.append({
            "campaign_id": str(raw["2"]),
            "name": str(raw.get("11") or ""),
            "status_raw": status_raw,
            "status": {
                CAMPAIGN_ENABLED: "已启用",
                CAMPAIGN_PAUSED: "已暂停",
                CAMPAIGN_REMOVED: "已移除",
            }.get(status_raw, f"未知({status_raw})"),
            "channel_raw": int(raw.get("15") or 0),
            "budget_id": str(raw.get("16") or ""),
            "daily_budget": Decimal(amount_micros) / Decimal(1_000_000),
            "currency": str(raw.get("18") or ""),
            "primary_status_raw": int(raw.get("171") or 0),
            "primary_status_reasons": list(raw.get("172") or []),
        })
    return rows


def campaign_copy_request(
    *,
    source_ocid: str,
    target_ocid: str,
    manager_ocid: str,
    campaign_id: str,
) -> dict[str, Any]:
    source_ocid = str(source_ocid)
    target_ocid = str(target_ocid)
    manager_ocid = str(manager_ocid)
    return {
        "1": {
            "1": {
                "3": "Campaign",
                "4": {
                    "1": [{"1": [{"1": "campaign_id", "2": {"3": str(campaign_id)}}]}],
                    "2": {
                        "1": {"3": {"1": source_ocid}},
                        "2": {
                            "2": [{
                                "1": "entity_owner_info.primary_account_type",
                                "2": 3,
                                "4": [{"3": "30"}, {"3": "17"}],
                            }],
                            "16": [{"1": "entity_owner_info.primary_account_type"}],
                        },
                    },
                },
                "5": source_ocid,
                "9": manager_ocid,
            },
            "2": {
                "3": "Customer",
                "4": {
                    "1": [{"1": [{"1": "customer_id", "2": {"3": target_ocid}}]}],
                    "2": {"1": {"3": {"1": target_ocid}}},
                },
                "5": target_ocid,
                "9": manager_ocid,
            },
            "6": {"1": False, "4": False},
            "10": {
                "1": False,
                "3": ["AWN_BULK_COPY_PERFORMANCE_IMPROVEMENTS"],
            },
            # Google Ads native "pause new campaigns after paste" option.
            "11": True,
            "12": {},
        }
    }


def campaign_status_request(ocid: str, campaign_id: str, status: int) -> dict[str, Any]:
    if status not in {CAMPAIGN_ENABLED, CAMPAIGN_PAUSED}:
        raise ValueError("只允许启用或暂停广告系列")
    return {
        "1": {"2": {"1": False}, "3": {"2": str(ocid)}},
        "2": [{
            "1": 3,
            "2": ["status"],
            "3": {
                "1": str(ocid),
                "2": str(campaign_id),
                "12": status,
            },
        }],
    }


def campaign_budget_request(ocid: str, budget_id: str, amount: Decimal | str) -> dict[str, Any]:
    """Build the captured BudgetService/Mutate payload for one daily budget."""
    daily_budget = validate_daily_budget(amount)
    micros = str(int(daily_budget * Decimal(1_000_000)))
    budget = {
        "1": str(ocid),
        "2": str(budget_id),
        "4": micros,
        "5": False,
        "7": 1,
        "13": "0",
        "14": 1,
    }
    return {
        "1": {"3": {"2": str(ocid)}},
        "2": [
            {"1": 4, "2": ["amount", "total_amount"], "3": dict(budget)},
            {
                "1": 3,
                "2": ["amount", "period", "delivery_method"],
                "3": dict(budget),
            },
        ],
    }


def campaign_budget_batch_request(
    ocid: str, budget_id: str, amount: Decimal | str
) -> dict[str, Any]:
    """Wrap BudgetService/Mutate exactly as the Google Ads Batch endpoint expects."""
    request = campaign_budget_request(ocid, budget_id, amount)
    return {
        "2": [json.dumps(request, ensure_ascii=False, separators=(",", ":"))],
        "3": [{
            "1": "ads.awapps.anji.proto.infra.budget.BudgetService",
            "2": "Mutate",
        }],
    }


def balance_amount(balance: dict[str, Any]) -> tuple[Decimal | None, str]:
    if balance.get("balance_status") != "可用":
        return None, normalize_currency(str(balance.get("currency") or ""))
    parsed = parse_money(str(balance.get("balance") or ""))
    if not parsed:
        return None, normalize_currency(str(balance.get("currency") or ""))
    return parsed[0], normalize_currency(parsed[1] or str(balance.get("currency") or ""))


def normalize_currency(value: str) -> str:
    currency = str(value or "").strip().upper()
    aliases = {
        "$": "USD", "US$": "USD", "USD$": "USD",
        "CA$": "CAD", "C$": "CAD", "A$": "AUD", "AU$": "AUD",
        "HK$": "HKD", "NZ$": "NZD", "SG$": "SGD",
        "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR",
    }
    return aliases.get(currency, currency)


def choose_app_campaign(campaigns: list[dict[str, Any]]) -> dict[str, Any] | None:
    active = [
        row for row in campaigns
        if row.get("channel_raw") == APP_CHANNEL
        and row.get("status_raw") != CAMPAIGN_REMOVED
        and str(row.get("name") or "").casefold().startswith(SOURCE_CAMPAIGN_NAME.casefold())
    ]
    exact = [row for row in active if row.get("name") == SOURCE_CAMPAIGN_NAME]
    return (exact or active or [None])[0]


class WarmupManager:
    """Balance-gated App campaign copy/enable workflow for one AdsPower window."""

    def __init__(
        self,
        profile_id: str,
        log: Callable[[str], None] | None = None,
        *,
        source_cid: str,
        target_daily_budget: Decimal | str = DEFAULT_DAILY_BUDGET,
        browser_type: str = ADSPOWER,
    ):
        self.profile_id = profile_id
        self.log = log or (lambda _text: None)
        if not str(source_cid or "").strip():
            raise ValueError("必须显式选择白量参考 CID，软件不再内置固定账户")
        self.source_cid = normalize_cid(source_cid)
        self.target_daily_budget = validate_daily_budget(target_daily_budget)
        self.runner = ProtocolRunner(profile_id, self.log, browser_type=browser_type)
        self.accounts = self.runner.discover_accounts()

    def close(self) -> None:
        self.runner.close()

    def __enter__(self) -> "WarmupManager":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _campaigns(self, account: AccountPage) -> list[dict[str, Any]]:
        response = account.page.rpc(
            "/aw/_/rpc/CampaignService/List",
            campaign_detail_request(account.ocid),
        )
        return parse_campaign_details(response)

    def _source(self) -> tuple[AccountPage, dict[str, Any]]:
        source = self.accounts.get(self.source_cid)
        if not source:
            raise RuntimeError(
                f"AdsPower 窗口 {self.profile_id} 无法访问参考 CID "
                f"{format_cid(self.source_cid)}"
            )
        if source.is_manager:
            raise ValueError("白量参考账户必须是普通 CID，不能选择 MCC")
        if source.status != "可用":
            raise PermissionError(f"白量参考 CID 状态为“{source.status}”")
        matches = [
            row for row in self._campaigns(source)
            if row.get("name") == SOURCE_CAMPAIGN_NAME
            and row.get("channel_raw") == APP_CHANNEL
            and row.get("status_raw") != CAMPAIGN_REMOVED
        ]
        if len(matches) != 1:
            raise RuntimeError("未能唯一定位参考应用广告系列 App-1")
        campaign = matches[0]
        if campaign["currency"] != "USD":
            raise PermissionError(
                f"参考 App-1 币种必须为 USD；当前为 {campaign['currency'] or '未知'}"
            )
        return source, campaign

    def _manager_ocid(self, account: AccountPage) -> str:
        for parent_cid in account.parent_mcc_cids:
            parent = self.accounts.get(parent_cid)
            if parent and parent.is_manager:
                return parent.ocid
        return account.ocid

    def _balance(self, account: AccountPage) -> dict[str, Any]:
        self.log(f"[{format_cid(account.cid)}] 读取余额门槛（只读）")
        try:
            return self.runner._account_balance(account)
        except Exception as exc:
            return {
                "balance": "—",
                "currency": "",
                "balance_status": "读取失败",
                "balance_type": "读取失败",
                "balance_detail": str(exc),
            }

    def _daily_spend(self, account: AccountPage) -> dict[str, Any]:
        self.log(f"[{format_cid(account.cid)}] 读取今日消耗（只读）")
        try:
            return self.runner._account_daily_spend(account)
        except Exception as exc:
            return {
                "daily_spend": "—",
                "daily_spend_status": "读取失败",
                "daily_spend_date": "",
                "daily_spend_detail": str(exc),
            }

    def scan(self, wanted_cids: set[str] | None = None) -> dict[str, Any]:
        source, source_campaign = self._source()
        wanted = {normalize_cid(value) for value in wanted_cids} if wanted_cids else None
        rows = []
        for cid in sorted(self.accounts):
            account = self.accounts[cid]
            if account.is_manager or (wanted is not None and cid not in wanted):
                continue
            balance = self._balance(account) if account.status == "可用" else {
                "balance": "—",
                "currency": "",
                "balance_status": "已跳过",
                "balance_type": "账户不可用",
                "balance_detail": account.status_detail,
            }
            spend = self._daily_spend(account) if account.status == "可用" else {
                "daily_spend": "—",
                "daily_spend_status": "已跳过",
                "daily_spend_date": "",
                "daily_spend_detail": account.status_detail,
            }
            amount, currency = balance_amount(balance)
            campaigns = self._campaigns(account) if account.status == "可用" else []
            existing = choose_app_campaign(campaigns)
            eligible = True
            reasons = []
            action = "将复制、调整预算并启用"
            if account.status != "可用":
                eligible = False
                reasons.append(f"账户状态={account.status}")
            if amount is None:
                eligible = False
                reasons.append("余额不可识别")
            elif amount <= 0:
                eligible = False
                reasons.append("余额不大于 0")
            if currency and currency != source_campaign["currency"]:
                eligible = False
                reasons.append(f"币种 {currency} 与参考系列 USD 不一致")
            if existing:
                if existing["status_raw"] == CAMPAIGN_ENABLED:
                    action = "已启用，无需操作"
                    eligible = False
                    reasons.append("App 系列已经启用")
                elif existing["status_raw"] == CAMPAIGN_PAUSED:
                    if existing["daily_budget"] == self.target_daily_budget:
                        action = "将启用现有系列"
                    else:
                        action = "将调整预算并启用现有系列"
                else:
                    eligible = False
                    action = "状态不支持"
                    reasons.append(f"系列状态={existing['status']}")
            rows.append({
                "profile_id": self.profile_id,
                "cid": cid,
                "cid_display": format_cid(cid),
                "ocid": account.ocid,
                "account_name": account.name,
                "account_status": account.status,
                "balance": balance.get("balance") or "—",
                "balance_amount": str(amount) if amount is not None else "",
                "currency": currency,
                "balance_type": balance.get("balance_type") or "",
                "balance_detail": balance.get("balance_detail") or "",
                "daily_spend": spend.get("daily_spend") or "—",
                "daily_spend_status": spend.get("daily_spend_status") or "",
                "daily_spend_date": spend.get("daily_spend_date") or "",
                "daily_spend_detail": spend.get("daily_spend_detail") or "",
                "campaign_id": str((existing or {}).get("campaign_id") or ""),
                "campaign_name": str((existing or {}).get("name") or ""),
                "campaign_status": str((existing or {}).get("status") or "不存在"),
                "current_daily_budget": (
                    str(existing.get("daily_budget")) if existing else ""
                ),
                "target_daily_budget": str(self.target_daily_budget),
                "daily_budget": str(self.target_daily_budget),
                "eligible": eligible,
                "selected": eligible,
                "action": action,
                "reason": "；".join(reasons) if reasons else "余额和账户状态符合条件",
            })
        return {
            "profile_id": self.profile_id,
            "source": {
                "cid": self.source_cid,
                "cid_display": format_cid(self.source_cid),
                "ocid": source.ocid,
                **source_campaign,
            },
            "rows": rows,
        }

    def _enable(self, account: AccountPage, campaign_id: str) -> None:
        account.page.rpc(
            CAMPAIGN_STATUS_MUTATE_PATH,
            campaign_status_request(account.ocid, campaign_id, CAMPAIGN_ENABLED),
        )

    def _set_budget(self, account: AccountPage, budget_id: str) -> None:
        if not budget_id:
            raise RuntimeError("App 系列缺少预算 ID，不能安全修改日预算")
        account.page.rpc(
            "/aw_essentials/_/rpc/BatchService/Batch",
            campaign_budget_batch_request(
                account.ocid, budget_id, self.target_daily_budget
            ),
        )

    def execute(self, target_cids: set[str]) -> list[dict[str, Any]]:
        source, source_campaign = self._source()
        wanted = {normalize_cid(value) for value in target_cids}
        results = []
        for cid in sorted(wanted):
            account = self.accounts.get(cid)
            result = {"cid": cid, "cid_display": format_cid(cid), "success": False}
            try:
                if not account or account.is_manager:
                    raise ValueError("目标 CID 不存在或是 MCC")
                if account.status != "可用":
                    raise PermissionError(f"账户状态为 {account.status}")
                fresh_balance = self._balance(account)
                fresh_spend = self._daily_spend(account)
                amount, currency = balance_amount(fresh_balance)
                if amount is None or amount <= 0:
                    raise PermissionError("执行前余额必须可识别且大于 0")
                if currency and currency != source_campaign["currency"]:
                    raise PermissionError("目标余额币种与参考 App-1 不一致")
                before = self._campaigns(account)
                campaign = choose_app_campaign(before)
                created = False
                if not campaign:
                    before_ids = {row["campaign_id"] for row in before}
                    self.log(f"[{format_cid(cid)}] 复制参考 App-1（新副本先暂停）")
                    source.page.rpc(
                        "/aw_essentials/_/rpc/CampaignCopyService/ExecuteAsyncCopy",
                        campaign_copy_request(
                            source_ocid=source.ocid,
                            target_ocid=account.ocid,
                            manager_ocid=self._manager_ocid(source),
                            campaign_id=source_campaign["campaign_id"],
                        ),
                    )
                    campaign = None
                    for attempt in range(12):
                        time.sleep(1.0 + attempt * 0.25)
                        current = self._campaigns(account)
                        candidates = [
                            row for row in current
                            if row["campaign_id"] not in before_ids
                            and row["channel_raw"] == APP_CHANNEL
                            and row["status_raw"] != CAMPAIGN_REMOVED
                        ]
                        if len(candidates) == 1:
                            campaign = candidates[0]
                            break
                        if len(candidates) > 1:
                            raise RuntimeError("复制后出现多个新应用广告系列，无法唯一校验")
                    if not campaign:
                        raise RuntimeError("复制协议返回后未读到新的应用广告系列")
                    created = True
                if campaign["currency"] != source_campaign["currency"]:
                    raise PermissionError("目标 App 系列币种与参考系列不一致")
                if campaign["status_raw"] == CAMPAIGN_ENABLED:
                    raise PermissionError("目标 App 系列已经启用，本次不重复修改或投放")
                if campaign["status_raw"] != CAMPAIGN_PAUSED:
                    raise PermissionError(f"目标 App 系列状态不支持启用：{campaign['status']}")
                if campaign["daily_budget"] != self.target_daily_budget:
                    self.log(
                        f"[{format_cid(cid)}] 调整日预算：{campaign['currency']} "
                        f"{campaign['daily_budget']:.2f} → {self.target_daily_budget:.2f}"
                    )
                    self._set_budget(account, campaign["budget_id"])
                    budget_verified = None
                    for attempt in range(6):
                        if attempt:
                            time.sleep(0.8 * attempt)
                        current = self._campaigns(account)
                        budget_verified = next(
                            (row for row in current if row["campaign_id"] == campaign["campaign_id"]),
                            None,
                        )
                        if (
                            budget_verified
                            and budget_verified["daily_budget"] == self.target_daily_budget
                        ):
                            break
                    if (
                        not budget_verified
                        or budget_verified["daily_budget"] != self.target_daily_budget
                    ):
                        raise RuntimeError("修改日预算后未能回读目标金额，已停止启用")
                    campaign = budget_verified
                if campaign["status_raw"] != CAMPAIGN_ENABLED:
                    self.log(f"[{format_cid(cid)}] 余额大于 0，启用 App 系列 {campaign['campaign_id']}")
                    self._enable(account, campaign["campaign_id"])
                verified = None
                for attempt in range(6):
                    if attempt:
                        time.sleep(0.8 * attempt)
                    current = self._campaigns(account)
                    verified = next(
                        (row for row in current if row["campaign_id"] == campaign["campaign_id"]),
                        None,
                    )
                    if (
                        verified
                        and verified["status_raw"] == CAMPAIGN_ENABLED
                        and verified["daily_budget"] == self.target_daily_budget
                    ):
                        break
                if not verified or verified["status_raw"] != CAMPAIGN_ENABLED:
                    raise RuntimeError("启用后未能回读已启用状态")
                if verified["daily_budget"] != self.target_daily_budget:
                    raise RuntimeError("启用后回读日预算与目标金额不一致")
                result.update({
                    "success": True,
                    "action": "copied_and_enabled" if created else "enabled_existing",
                    "campaign_id": verified["campaign_id"],
                    "campaign_name": verified["name"],
                    "status": verified["status"],
                    "daily_budget": str(verified["daily_budget"]),
                    "currency": verified["currency"],
                    "balance": fresh_balance.get("balance"),
                    "daily_spend": fresh_spend.get("daily_spend"),
                    "daily_spend_date": fresh_spend.get("daily_spend_date"),
                })
            except Exception as exc:
                result["error"] = str(exc)
            results.append(result)
        return results
