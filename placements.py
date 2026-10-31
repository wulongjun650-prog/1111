from __future__ import annotations

import time
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .ads import campaign_list_request, parse_campaigns
from .browser_provider import ADSPOWER
from .config import format_cid, normalize_cid, normalize_websites
from .protocol import AccountPage, ProtocolRunner
from .reporting import ReportPeriod, coerce_report_period, protocol_day


PLACEMENT_REPORT_FIELDS = [
    "advertising_channel_type",
    "advertising_channel_sub_type",
    "currency_code",
    "display_name",
    "placement",
    "target_url",
    "placement_type",
    "campaign_name",
    "campaign_id",
    "ad_group_name",
    "ad_group_id",
    "stats.impressions",
    "stats.interactions",
    "stats.interaction_types",
    "stats.interaction_rate",
    "stats.average_cost",
    "stats.cost",
    "stats.conversion_rate",
    "stats.conversions",
    "stats.cost_per_conversion",
    "customer_id",
]


def placement_report_request(
    ocid: str, report_date: date | ReportPeriod, *, offset: int = 0, limit: int = 10000,
) -> dict[str, Any]:
    period = coerce_report_period(report_date)
    return {
        "1": {"3": {"1": str(ocid)}, "5": "TABLE"},
        "2": {
            "1": list(PLACEMENT_REPORT_FIELDS),
            "2": [
                {"1": "campaign_status", "2": 3, "4": [{"3": "3"}, {"3": "5"}]},
                {"1": "ad_group_status", "2": 3, "4": [{"3": "1"}, {"3": "2"}]},
                {"1": "customer_id", "2": 1, "4": [{"3": str(ocid)}], "5": 1},
            ],
            "3": [{"1": "stats.interactions", "2": 2}],
            "4": {
                "1": protocol_day(period.start),
                "2": protocol_day(period.end),
                "8": "0",
            },
            "7": {"1": int(offset), "2": int(limit)},
            "14": True,
        },
        "3": [
            {"1": "USE_JOIN_FOR_SEGMENTATION", "2": "TRUE"},
            {"1": "INVERT_GROUPING_ENTITIES", "2": "TRUE"},
            {"1": "PUSH_ID_FILTERS_INTO_LABEL_SUBVIEW", "2": "TRUE"},
        ],
    }


def _decimal(value: Any, label: str) -> Decimal:
    text = str(value or "0").strip()
    if text in {"", "--"}:
        return Decimal("0")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label}不是有效数字：{text}") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label}必须是非负有限数字")
    return result


def parse_placement_report(
    payload: dict[str, Any], *, expected_ocid: str,
    report_date: date | ReportPeriod,
) -> list[dict[str, Any]]:
    period = coerce_report_period(report_date)
    service_errors = ((payload.get("5") or {}).get("2") or [])
    if service_errors:
        raise PermissionError(str(service_errors[0]))
    rows: list[dict[str, Any]] = []
    for raw in payload.get("1") or []:
        if not isinstance(raw, dict):
            continue
        row_ocid = str(raw.get("2") or "")
        if row_ocid and row_ocid != str(expected_ocid):
            raise PermissionError(f"展示位置数据属于其他内部账户 {row_ocid}")
        metrics = list((raw.get("200") or {}).get("1") or [])
        metrics.extend(["0"] * (9 - len(metrics)))
        impressions = int(_decimal(metrics[0], "展示次数"))
        interactions = int(_decimal(metrics[1], "互动次数"))
        average_cost = _decimal(metrics[4], "平均费用") / Decimal(1_000_000)
        cost = _decimal(metrics[5], "费用") / Decimal(1_000_000)
        conversions = _decimal(metrics[7], "转化次数")
        placement_type_raw = int(raw.get("11") or 0)
        placement = str(raw.get("6") or raw.get("5") or raw.get("19") or "").strip()
        normalized = ""
        if placement_type_raw == 2 and placement:
            try:
                normalized = normalize_websites([placement])[0]
            except (ValueError, IndexError):
                normalized = ""
        rows.append({
            "date": period.label,
            "ad_group_id": str(raw.get("1") or ""),
            "ocid": row_ocid,
            "placement_id": str(raw.get("3") or ""),
            "campaign_id": str(raw.get("4") or ""),
            "display_name": str(raw.get("5") or placement),
            "placement": placement,
            "normalized_placement": normalized,
            "target_url": str(raw.get("19") or ""),
            "ad_group_name": str(raw.get("7") or ""),
            "campaign_name": str(raw.get("8") or ""),
            "currency": str(raw.get("9") or ""),
            "placement_type_raw": placement_type_raw,
            "placement_type": "网站" if placement_type_raw == 2 else f"其他({placement_type_raw})",
            "channel_raw": int(raw.get("21") or 0),
            "impressions": impressions,
            "interactions": interactions,
            "interaction_rate": str(metrics[3]),
            "average_cost": str(average_cost),
            "cost": str(cost),
            "conversion_rate": str(metrics[6]),
            "conversions": str(conversions),
            "cost_per_conversion": str(_decimal(metrics[8], "每次转化费用") / Decimal(1_000_000)),
            "eligible_for_campaign_exclusion": bool(normalized),
        })
    return rows


def campaign_placement_list_request(ocid: str, campaign_id: str) -> dict[str, Any]:
    return {
        "1": {"3": {"1": str(ocid)}},
        "2": {
            "1": [
                "customer_id", "campaign_id", "criterion_id",
                "negative", "type", "placement.url",
            ],
            "2": [{"1": "campaign_id", "2": 1, "4": [{"3": str(campaign_id)}]}],
            "14": True,
        },
    }


def parse_campaign_placement_exclusions(payload: dict[str, Any]) -> list[dict[str, str]]:
    result = []
    for row in payload.get("1") or []:
        placement = str(((row.get("114") or {}).get("1")) or "").strip()
        if not placement or row.get("11") is not True:
            continue
        normalized = normalize_websites([placement])[0]
        result.append({
            "campaign_id": str(row.get("2") or ""),
            "criterion_id": str(row.get("3") or ""),
            "placement": placement,
            "normalized_placement": normalized,
        })
    return result


def campaign_placement_exclusion_mutate_request(
    ocid: str, entries: list[tuple[str, str]],
) -> dict[str, Any]:
    operations = []
    for index, (campaign_id, placement) in enumerate(entries, 1):
        operations.append({
            "1": 1,
            "2": ["campaign_id", "criterion_id", "negative", "placement"],
            "3": {
                "1": str(ocid),
                "2": str(campaign_id),
                "3": str(-index),
                "11": True,
                "114": {"1": str(placement)},
            },
        })
    return {"1": {"2": {"1": False}, "3": {"2": str(ocid)}}, "2": operations}


class PlacementManager:
    """Read placement performance and create campaign-level website exclusions."""

    def __init__(
        self,
        profile_id: str,
        cid: str,
        log: Callable[[str], None] | None = None,
        *,
        browser_type: str = ADSPOWER,
    ):
        self.profile_id = str(profile_id)
        self.cid = normalize_cid(cid)
        self.log = log or (lambda _text: None)
        self.runner = ProtocolRunner(
            self.profile_id, self.log, browser_type=browser_type,
        )
        try:
            accounts = self.runner.discover_accounts({self.cid})
            account = accounts.get(self.cid)
            if not account:
                raise RuntimeError(f"CID {format_cid(self.cid)} 不在当前窗口的可访问范围内")
            if account.is_manager:
                raise ValueError("MCC 没有展示位置报表，请选择普通 CID")
            self.account: AccountPage = account
        except Exception:
            self.runner.close()
            raise

    def close(self) -> None:
        self.runner.close()

    def __enter__(self) -> "PlacementManager":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def query(self, report_date: date | ReportPeriod) -> list[dict[str, Any]]:
        period = coerce_report_period(report_date)
        self.log(f"[{format_cid(self.cid)}] 读取 {period.label}展示位置（只读）")
        rows: list[dict[str, Any]] = []
        page_size = 10000
        previous_keys: set[tuple[str, str, str]] = set()
        for page_index in range(100):
            offset = page_index * page_size
            payload = self.account.page.rpc(
                "/aw_dva/_/rpc/AutoPlacementService/List",
                placement_report_request(
                    self.account.ocid, period, offset=offset, limit=page_size,
                ),
                timeout=120,
            )
            page_rows = parse_placement_report(
                payload, expected_ocid=self.account.ocid, report_date=period,
            )
            page_keys = {
                (
                    str(row.get("campaign_id") or ""),
                    str(row.get("ad_group_id") or ""),
                    str(row.get("placement_id") or row.get("placement") or ""),
                )
                for row in page_rows
            }
            if page_index and page_rows and page_keys == previous_keys:
                raise RuntimeError("展示位置服务忽略分页 offset，已停止，避免重复第一页被当成完整数据")
            rows.extend(page_rows)
            if len(page_rows) < page_size:
                break
            previous_keys = page_keys
        else:
            raise RuntimeError("展示位置超过 100 万条，已停止读取，请缩小日期范围")
        self.log(
            f"[{format_cid(self.cid)}] 展示位置读取完成：{len(rows)} 条 · "
            f"范围 {period.start.isoformat()} 至 {period.end.isoformat()}"
        )
        return rows

    def _campaigns(self) -> dict[str, dict[str, Any]]:
        payload = self.account.page.rpc(
            "/aw/_/rpc/CampaignService/List", campaign_list_request(self.account.ocid)
        )
        return {str(row["campaign_id"]): row for row in parse_campaigns(payload)}

    def _exclusions(self, campaign_id: str) -> list[dict[str, str]]:
        payload = self.account.page.rpc(
            "/aw_dva/_/rpc/CampaignCriterionService/List",
            campaign_placement_list_request(self.account.ocid, campaign_id),
        )
        return parse_campaign_placement_exclusions(payload)

    @staticmethod
    def _selected_keys(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for row in rows:
            if int(row.get("placement_type_raw") or 0) != 2:
                raise ValueError(f"当前只支持网站展示位置排除：{row.get('placement') or '未知对象'}")
            normalized = normalize_websites([
                row.get("normalized_placement") or row.get("placement") or ""
            ])[0]
            campaign_id = str(row.get("campaign_id") or "")
            if not campaign_id:
                raise ValueError("展示位置缺少广告系列 ID")
            keys.add((campaign_id, normalized))
        if not keys:
            raise ValueError("请至少选择一个网站展示位置")
        return keys

    def preflight(
        self, rows: list[dict[str, Any]], report_date: date | ReportPeriod,
    ) -> dict[str, Any]:
        selected = self._selected_keys(rows)
        fresh_rows = self.query(report_date)
        fresh_keys = {
            (str(row.get("campaign_id") or ""), str(row.get("normalized_placement") or ""))
            for row in fresh_rows if row.get("eligible_for_campaign_exclusion")
        }
        missing_from_report = sorted(selected - fresh_keys)
        if missing_from_report:
            raise PermissionError(
                "所选展示位置已不在该日期的最新报表中："
                + "、".join(value for _campaign, value in missing_from_report[:5])
            )
        campaigns = self._campaigns()
        grouped: dict[str, list[str]] = {}
        for campaign_id, placement in sorted(selected):
            campaign = campaigns.get(campaign_id)
            if not campaign or campaign.get("status_raw") == 7:
                raise PermissionError(f"广告系列 {campaign_id} 不存在或已移除")
            grouped.setdefault(campaign_id, []).append(placement)
        groups = []
        for campaign_id, placements in grouped.items():
            existing_rows = self._exclusions(campaign_id)
            existing = {row["normalized_placement"] for row in existing_rows}
            groups.append({
                "campaign_id": campaign_id,
                "campaign_name": str(campaigns[campaign_id].get("name") or campaign_id),
                "campaign_status": str(campaigns[campaign_id].get("status") or "未知"),
                "selected": placements,
                "already_excluded": [value for value in placements if value in existing],
                "would_add": [value for value in placements if value not in existing],
            })
        return {
            "mode": "placement_exclusion_preflight",
            "write_performed": False,
            "profile_id": self.profile_id,
            "cid": self.cid,
            "date": coerce_report_period(report_date).label,
            "selected_count": len(selected),
            "would_add_count": sum(len(group["would_add"]) for group in groups),
            "already_excluded_count": sum(len(group["already_excluded"]) for group in groups),
            "campaigns": groups,
        }

    def exclude(
        self, rows: list[dict[str, Any]], report_date: date | ReportPeriod,
    ) -> dict[str, Any]:
        if self.account.status != "可用":
            raise PermissionError(
                f"CID {format_cid(self.cid)} 当前状态为“{self.account.status}”，禁止修改排除对象"
            )
        plan = self.preflight(rows, report_date)
        entries = [
            (group["campaign_id"], placement)
            for group in plan["campaigns"] for placement in group["would_add"]
        ]
        if not entries:
            return {**plan, "mode": "placement_exclusion", "success": True, "post_read_verified": True}
        self.log(
            f"[{format_cid(self.cid)}] 从 {len(plan['campaigns'])} 个广告系列排除 "
            f"{len(entries)} 个网站展示位置"
        )
        response = self.account.page.rpc(
            "/aw_dva/_/rpc/CampaignCriterionService/Mutate",
            campaign_placement_exclusion_mutate_request(self.account.ocid, entries),
        )
        expected_by_campaign: dict[str, set[str]] = {}
        for campaign_id, placement in entries:
            expected_by_campaign.setdefault(campaign_id, set()).add(placement)
        missing: dict[str, set[str]] = expected_by_campaign
        for attempt in range(7):
            if attempt:
                time.sleep(0.7 * attempt)
            missing = {}
            for campaign_id, expected in expected_by_campaign.items():
                current = {row["normalized_placement"] for row in self._exclusions(campaign_id)}
                absent = expected - current
                if absent:
                    missing[campaign_id] = absent
            if not missing:
                break
        if missing:
            detail = "；".join(
                f"{campaign_id}: {', '.join(sorted(values))}"
                for campaign_id, values in missing.items()
            )
            raise RuntimeError(f"排除协议已返回，但写后回读缺少：{detail}")
        self.log(f"[{format_cid(self.cid)}] 广告系列展示位置排除成功并完成写后回读")
        return {
            **plan,
            "mode": "placement_exclusion",
            "write_performed": True,
            "success": True,
            "created_count": len(entries),
            "response_received": isinstance(response, dict),
            "post_read_verified": True,
        }
