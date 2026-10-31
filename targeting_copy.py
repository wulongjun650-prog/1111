"""Copy campaign and ad-group criteria that the snapshot previously dropped.

Location, language, device, ad schedule, and topic use the same
CampaignCriterionService / AdGroupCriterionService calls already verified for
placement exclusions and audience writes. List rows are replayed with the
target account and campaign ids. Field numbers stay whatever the server
returned; only the identity fields are rewritten.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from .portable_copy import (
    AD_GROUP_CRITERION_MUTATE_PATH,
    _filter,
    _service_error,
)


CAMPAIGN_CRITERION_LIST_PATH = "/aw_dva/_/rpc/CampaignCriterionService/List"
CAMPAIGN_CRITERION_MUTATE_PATH = "/aw_dva/_/rpc/CampaignCriterionService/Mutate"
AD_GROUP_CRITERION_LIST_PATH = "/aw_dva/_/rpc/AdGroupCriterionService/List"

# Placement list/mutate in placements.py: campaign field 2, criterion field 3,
# negative field 11, placement payload field 114. Those identity keys are not
# the targeting payload.
_CAMPAIGN_IDENTITY = frozenset({"1", "2", "3", "11"})
# AdGroupCriterionService.Mutate (see ad_group_audience_mutate_request):
# field 1 customer, field 3 ad group, field 4 criterion, field 15 negative.
_AD_GROUP_IDENTITY = frozenset({"1", "2", "3", "4", "15"})

_MUTATE_CHUNK = 20


@dataclass(frozen=True)
class CriterionSpec:
    kind: str
    level: str
    list_fields: tuple[str, ...]
    write_fields: tuple[str, ...]
    identity_keys: frozenset[str]


def _spec(
    kind: str,
    level: str,
    payload_fields: tuple[str, ...],
    *,
    write_payload: tuple[str, ...] | None = None,
) -> CriterionSpec:
    if level == "campaign":
        identity = ("campaign_id", "criterion_id", "negative")
        identity_keys = _CAMPAIGN_IDENTITY
    elif level == "ad_group":
        identity = ("campaign_id", "ad_group_id", "criterion_id", "negative")
        identity_keys = _AD_GROUP_IDENTITY
    else:
        raise ValueError(f"未知定向层级：{level}")
    write_names = write_payload if write_payload is not None else payload_fields
    write_fields = identity + write_names
    if level == "ad_group":
        # Create uses the ad group id, not the source campaign id.
        write_fields = tuple(field for field in write_fields if field != "campaign_id")
    return CriterionSpec(
        kind=kind,
        level=level,
        list_fields=identity + payload_fields,
        write_fields=write_fields,
        identity_keys=identity_keys,
    )


# Names follow the field paths this codebase already sends (placement.url,
# user_interest_id) and the public criterion attributes Google documents for
# the same resources. A path the server does not know is ignored, the row has
# no payload, and that criterion stays in targeting_gaps instead of being
# written as the wrong type.
CAMPAIGN_SPECS: tuple[CriterionSpec, ...] = (
    _spec("location", "campaign", ("location.geo_target_constant",)),
    _spec("language", "campaign", ("language.language_constant",)),
    _spec("device", "campaign", ("device.type", "bid_modifier")),
    _spec(
        "ad_schedule",
        "campaign",
        (
            "ad_schedule.day_of_week",
            "ad_schedule.start_hour",
            "ad_schedule.start_minute",
            "ad_schedule.end_hour",
            "ad_schedule.end_minute",
        ),
    ),
    _spec("topic", "campaign", ("topic.topic_constant", "topic.path")),
    _spec("keyword", "campaign", ("keyword.text", "keyword.match_type")),
    _spec("age_range", "campaign", ("age_range.type",)),
    _spec("gender", "campaign", ("gender.type",)),
    _spec("parental_status", "campaign", ("parental_status.type",)),
    # Verified write mask is "placement", while List fills it via placement.url.
    _spec("placement", "campaign", ("placement.url",), write_payload=("placement",)),
)

AD_GROUP_SPECS: tuple[CriterionSpec, ...] = (
    _spec("topic", "ad_group", ("topic.topic_constant", "topic.path")),
    _spec("keyword", "ad_group", ("keyword.text", "keyword.match_type")),
    _spec("age_range", "ad_group", ("age_range.type",)),
    _spec("gender", "ad_group", ("gender.type",)),
    _spec("location", "ad_group", ("location.geo_target_constant",)),
    _spec("placement", "ad_group", ("placement.url",), write_payload=("placement",)),
)


class HumanActionRequired(RuntimeError):
    """The next step has to be a real page click so the user can touch the key."""

    def __init__(self, reason: str, instructions: str):
        self.reason = reason
        super().__init__(instructions)


def row_has_payload(row: dict[str, Any], identity_keys: frozenset[str]) -> bool:
    return any(str(key) not in identity_keys for key in row)


def select_existing_budget(rows: list[dict[str, Any]], amount_micros: int) -> str:
    """Pick a budget already on the account with this daily amount, in micros."""
    target = (Decimal(int(amount_micros)) / Decimal(1_000_000)).quantize(Decimal("0.01"))
    ranked = sorted(rows, key=lambda row: int(row.get("status_raw") or 0) == 7)
    seen: set[str] = set()
    for row in ranked:
        budget_id = str(row.get("budget_id") or "")
        if not budget_id or budget_id in seen:
            continue
        seen.add(budget_id)
        amount = row.get("daily_budget")
        if amount is None:
            continue
        if Decimal(amount).quantize(Decimal("0.01")) == target:
            return budget_id
    return ""


def budget_gate_message(amount_micros: int, currency: str) -> str:
    amount = (Decimal(int(amount_micros)) / Decimal(1_000_000)).quantize(Decimal("0.01"))
    money = f"{currency} {amount:.2f}" if currency else f"{amount:.2f}"
    return (
        f"目标账号里没有日预算为 {money} 的现成预算。程序没有新建预算，这一步不会弹出密钥。"
        f"请先在这个账号的网页里建好这条日预算。弹出密钥时摸一下 U 盘。"
        f"建好后重新点导入，已经完成的步骤会接着做。"
    )


def final_url_gate_message(url: str) -> str:
    shown = url or "快照里的落地页"
    return (
        f"建广告时这个落地页需要你本人确认：{shown}。"
        f"请在目标账号网页里用这个网址发布一条暂停广告。弹出密钥时摸一下 U 盘。"
        f"完成后重新点导入。已经建好的系列、广告组和图片会接着用，不会重来。"
    )


def reauth_gate_message(step: str) -> str:
    return (
        f"{step}需要你在网页上亲手确认。请打开目标账号的 Google Ads 页面完成这一步。"
        f"弹出密钥时摸一下 U 盘。完成后重新点导入。"
    )


def human_gate_for_error(step: str, error: str, *, final_url: str = "") -> HumanActionRequired | None:
    text = str(error or "").upper()
    if "UNPHISHABLE" in text or ("FINAL_URL" in text and "PHISH" in text):
        return HumanActionRequired("final_url", final_url_gate_message(final_url))
    if "REAUTH" in text or "PROOF_TOKEN" in text:
        return HumanActionRequired("reauth", reauth_gate_message(step))
    return None


def _as_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    raw = response.get("1")
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    return [row for row in raw if isinstance(row, dict)]


def _list_request(ocid: str, fields: tuple[str, ...] | list[str], filters: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "1": {"3": {"1": str(ocid)}},
        "2": {"1": list(fields), "2": filters, "14": True},
    }


def _criterion_id(row: dict[str, Any], level: str, index: int, kind: str, ad_group_id: str) -> str:
    if level == "campaign":
        found = str(row.get("3") or "")
    else:
        found = str(row.get("4") or "")
    if found and found != "0":
        return found
    return f"{level}:{kind}:{ad_group_id}:{index}"


def _criterion_key(level: str, ad_group_id: str, criterion_id: str) -> str:
    return f"{level}:{ad_group_id}:{criterion_id}"


def read_targeting(page: Any, ocid: str, campaign_id: str, ad_group_ids: list[str]) -> dict[str, Any]:
    """Read criteria for one campaign. Rows without a payload are not copied."""
    campaign_criteria: list[dict[str, Any]] = []
    ad_group_criteria: list[dict[str, Any]] = []
    covered_campaign: set[str] = set()

    def take(spec: CriterionSpec, row: dict[str, Any], index: int, ad_group_id: str) -> None:
        if not row_has_payload(row, spec.identity_keys):
            return
        criterion_id = _criterion_id(row, spec.level, index, spec.kind, ad_group_id)
        if spec.level == "campaign":
            covered_campaign.add(criterion_id)
        entry = {
            "kind": spec.kind,
            "level": spec.level,
            "source_criterion_id": criterion_id,
            "source_ad_group_id": ad_group_id,
            "write_fields": list(spec.write_fields),
            "row": row,
        }
        if spec.level == "campaign":
            campaign_criteria.append(entry)
        else:
            ad_group_criteria.append(entry)

    campaign_filter = [_filter("campaign_id", campaign_id)]
    for spec in CAMPAIGN_SPECS:
        response = page.rpc(
            CAMPAIGN_CRITERION_LIST_PATH,
            _list_request(ocid, spec.list_fields, campaign_filter),
        )
        for index, row in enumerate(_as_rows(response), 1):
            take(spec, row, index, "")

    for ad_group_id in ad_group_ids:
        filters = [
            _filter("campaign_id", campaign_id),
            _filter("ad_group_id", ad_group_id),
        ]
        for spec in AD_GROUP_SPECS:
            response = page.rpc(
                AD_GROUP_CRITERION_LIST_PATH,
                _list_request(ocid, spec.list_fields, filters),
            )
            for index, row in enumerate(_as_rows(response), 1):
                take(spec, row, index, ad_group_id)

    broad = page.rpc(
        CAMPAIGN_CRITERION_LIST_PATH,
        _list_request(
            ocid,
            ("campaign_id", "criterion_id", "negative", "type"),
            campaign_filter,
        ),
    )
    gaps = []
    for index, row in enumerate(_as_rows(broad), 1):
        criterion_id = _criterion_id(row, "campaign", index, "gap", "")
        if criterion_id in covered_campaign:
            continue
        if not str(row.get("3") or ""):
            continue
        gaps.append({
            "level": "campaign",
            "source_criterion_id": criterion_id,
            "type": str(row.get("9") or row.get("5") or ""),
        })
    return {
        "campaign_criteria": campaign_criteria,
        "ad_group_criteria": ad_group_criteria,
        "targeting_gaps": gaps,
    }


def _rewrite_campaign_row(row: dict[str, Any], ocid: str, campaign_id: str, temporary_id: str) -> dict[str, Any]:
    entity = copy.deepcopy(row)
    entity["1"] = str(ocid)
    entity["2"] = str(campaign_id)
    entity["3"] = str(temporary_id)
    return entity


def _rewrite_ad_group_row(row: dict[str, Any], ocid: str, ad_group_id: str, temporary_id: str) -> dict[str, Any]:
    entity = copy.deepcopy(row)
    entity["1"] = str(ocid)
    entity["3"] = str(ad_group_id)
    entity["4"] = str(temporary_id)
    entity.pop("2", None)
    return entity


def _mutate_request(ocid: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"1": {"2": {"1": False}, "3": {"2": str(ocid)}}, "2": operations}


def apply_criteria(
    page: Any,
    ocid: str,
    campaign_id: str,
    entries: list[dict[str, Any]],
    *,
    group_map: dict[str, str],
    applied_keys: list[str],
    log: Callable[[str], None],
) -> list[str]:
    """Create criteria that are not already recorded in applied_keys. Returns warnings."""
    applied = set(applied_keys)
    pending_by_level: dict[str, list[dict[str, Any]]] = {"campaign": [], "ad_group": []}
    for entry in entries:
        level = str(entry.get("level") or "")
        source_group = str(entry.get("source_ad_group_id") or "")
        criterion_id = str(entry.get("source_criterion_id") or "")
        key = _criterion_key(level, source_group, criterion_id)
        if key in applied:
            continue
        pending_by_level.setdefault(level, []).append(entry)

    warnings: list[str] = []
    for level, pending in pending_by_level.items():
        if not pending:
            continue
        path = CAMPAIGN_CRITERION_MUTATE_PATH if level == "campaign" else AD_GROUP_CRITERION_MUTATE_PATH
        for offset in range(0, len(pending), _MUTATE_CHUNK):
            chunk = pending[offset:offset + _MUTATE_CHUNK]
            operations = []
            keys = []
            for index, entry in enumerate(chunk, 1):
                temporary_id = str(-(offset + index))
                source_group = str(entry.get("source_ad_group_id") or "")
                if level == "campaign":
                    entity = _rewrite_campaign_row(entry["row"], ocid, campaign_id, temporary_id)
                else:
                    target_group = str(group_map.get(source_group) or "")
                    if not target_group:
                        raise RuntimeError(f"定向 {entry.get('kind')} 找不到目标广告组")
                    entity = _rewrite_ad_group_row(entry["row"], ocid, target_group, temporary_id)
                operations.append({
                    "1": 1,
                    "2": list(entry.get("write_fields") or []),
                    "3": entity,
                })
                keys.append(_criterion_key(
                    level, source_group, str(entry.get("source_criterion_id") or ""),
                ))
            response = page.rpc(path, _mutate_request(ocid, operations))
            error = _service_error(response)
            if error:
                gate = human_gate_for_error(f"写入{level}定向", error)
                if gate:
                    raise gate
                raise RuntimeError(f"写入定向失败：{error}")
            applied_keys.extend(keys)
            applied.update(keys)
        log(f"已写入{level}定向 {len(pending)} 条")
    return warnings


def summarize_targeting(snapshot: dict[str, Any]) -> str:
    campaign_rows = list(snapshot.get("campaign_criteria") or [])
    group_rows = list(snapshot.get("ad_group_criteria") or [])
    gaps = list(snapshot.get("targeting_gaps") or [])
    counts: dict[str, int] = {}
    for entry in campaign_rows + group_rows:
        kind = str(entry.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
    parts = [f"{kind} {count}" for kind, count in sorted(counts.items())]
    body = "、".join(parts) if parts else "没有读到主题/地区/语言/设备/时段"
    if gaps:
        body += f"；另有 {len(gaps)} 条定向没识别"
    return body
