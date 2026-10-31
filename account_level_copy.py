"""Copy account-level content suitability and the 联系人 conversion goal.

Campaign copy alone leaves B on its own goals and its own account exclusions.
This module reads those account settings from A and adds only what B lacks.

NegativeTargetedContentService.List is the verified read. The customer-negative
list and every mutate path are the same shell with the service or method
swapped; they are not separately captured. A spec that errors is recorded and
skipped. The export stops only when every exclusion spec errors, or when Google
asks for the USB key.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable

from .portable_copy import _filter, _service_error
from .targeting_copy import HumanActionRequired, human_gate_for_error


VERIFIED_EXCLUSION_LIST_PATH = "/aw_dva/_/rpc/NegativeTargetedContentService/List"
# Not in the protocol catalog. Used only after the verified list errors.
FALLBACK_EXCLUSION_LIST_PATH = "/aw_dva/_/rpc/CustomerNegativeCriterionService/List"
EXCLUSION_LIST_PATHS = (VERIFIED_EXCLUSION_LIST_PATH, FALLBACK_EXCLUSION_LIST_PATH)

CUSTOMER_LIST_PATH = "/aw/_/rpc/CustomerService/List"
# Inferred by swapping List for Mutate on the verified customer read path.
CUSTOMER_MUTATE_PATH = "/aw/_/rpc/CustomerService/Mutate"
INVENTORY_FIELD = "video_brand_safety_suitability"

CONVERSION_LIST_PATHS = (
    "/aw/_/rpc/ConversionTypeService/List",
    "/aw/_/rpc/ConversionGoalService/List",
)
CONVERSION_LIST_FIELDS = ("conversion_type_id", "name", "category", "status", "type")
CONVERSION_WRITE_FIELDS = ("name", "category", "status", "type")
CONTACT_NAMES = frozenset({"联系人", "联系"})

CAMPAIGN_GOAL_LIST_PATH = "/aw/_/rpc/CampaignService/List"
# Inferred service name from the Ads app. Not a captured contract.
CAMPAIGN_GOAL_MUTATE_PATH = "/aw/_/rpc/CampaignConversionGoalService/Mutate"
CAMPAIGN_GOAL_FIELDS = (
    "campaign_id",
    "campaign_conversion_goal.category",
    "campaign_conversion_goal.origin",
    "campaign_conversion_goal.biddable",
)

_MUTATE_CHUNK = 20
_IDENTITY_KEYS = frozenset({"1", "3"})


@dataclass(frozen=True)
class ExclusionSpec:
    kind: str
    section: str
    list_fields: tuple[str, ...]
    write_fields: tuple[str, ...]


def _spec(kind: str, section: str, payload: tuple[str, ...], write_payload: tuple[str, ...]) -> ExclusionSpec:
    return ExclusionSpec(
        kind=kind,
        section=section,
        list_fields=("criterion_id", "type", *payload),
        write_fields=("customer_id", "criterion_id", *write_payload),
    )


# Content labels cover both 排除的敏感内容 and 排除的类型和标签.
# Topics and content keywords are not on the public customer-negative resource;
# their field paths may be ignored, and that spec is then recorded as a gap.
SUITABILITY_SPECS: tuple[ExclusionSpec, ...] = (
    _spec("content_label", "排除的敏感内容和类型标签", ("content_label.type",), ("content_label",)),
    _spec("website", "排除的网站", ("placement.url",), ("placement",)),
    _spec("youtube_channel", "排除的 YouTube 频道", ("youtube_channel.channel_id",), ("youtube_channel",)),
    _spec("youtube_video", "排除的 YouTube 视频", ("youtube_video.video_id",), ("youtube_video",)),
    _spec("app", "排除的应用", ("mobile_application.app_id",), ("mobile_application",)),
    _spec(
        "app_category",
        "排除的应用类别",
        ("mobile_app_category.mobile_app_category_constant",),
        ("mobile_app_category",),
    ),
    _spec("content_keyword", "排除的内容关键字", ("keyword.text", "keyword.match_type"), ("keyword",)),
    _spec("content_theme", "排除的内容主题", ("topic.topic_constant", "topic.path"), ("topic",)),
)


def ensure_account_level_state(state: dict[str, Any]) -> None:
    state.setdefault("account_exclusion_keys", [])
    state.setdefault("conversion_actions_ready", [])
    state.setdefault("campaign_goal_applied", False)
    state.setdefault("inventory_applied", False)
    state.setdefault("account_level_warnings", [])


def _as_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    raw = response.get("1")
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    return [row for row in raw if isinstance(row, dict)]


def _customer_list_request(ocid: str, fields: tuple[str, ...] | list[str], *, filtered: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"1": list(fields), "14": True}
    if filtered:
        body["2"] = [_filter("customer_id", ocid)]
    return {"1": {"3": {"1": str(ocid)}}, "2": body}


def _campaign_list_request(ocid: str, campaign_id: str, fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        "1": {"3": {"1": str(ocid)}},
        "2": {"1": list(fields), "2": [_filter("campaign_id", campaign_id)], "14": True},
    }


def _mutate_request(ocid: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"1": {"2": {"1": False}, "3": {"2": str(ocid)}}, "2": operations}


def _call(page: Any, path: str, request: dict[str, Any], step: str) -> dict[str, Any]:
    try:
        response = page.rpc(path, request)
    except HumanActionRequired:
        raise
    except Exception as exc:
        gate = human_gate_for_error(step, str(exc))
        if gate:
            raise gate
        raise RuntimeError(str(exc)) from exc
    if not isinstance(response, dict):
        raise RuntimeError(f"{step}没有返回数据")
    error = _service_error(response)
    if error:
        gate = human_gate_for_error(step, error)
        if gate:
            raise gate
        raise RuntimeError(error)
    return response


def _row_has_payload(row: dict[str, Any]) -> bool:
    return any(str(key) not in _IDENTITY_KEYS for key in row)


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        found: list[str] = []
        for item in value.values():
            found.extend(_walk_strings(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_walk_strings(item))
        return found
    return []


def is_contact_action(row: dict[str, Any]) -> bool:
    """True when the row is the named 联系人 goal, not every conversion on A."""
    return any(text in CONTACT_NAMES for text in _walk_strings(row))


def _normalize(value: Any, ignore_ids: set[str]) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize(item, ignore_ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, ignore_ids) for item in value]
    text = str(value).strip()
    if text in ignore_ids:
        return ""
    return text


def exclusion_fingerprint(row: dict[str, Any], ignore_ids: set[str]) -> str:
    """Identity of one exclusion without the account id or the criterion id."""
    cleaned = {
        str(key): _normalize(value, ignore_ids)
        for key, value in row.items()
        if str(key) not in _IDENTITY_KEYS
    }
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _replace_known_ids(
    value: Any,
    source_ocid: str,
    target_ocid: str,
    source_campaign_id: str,
    target_campaign_id: str,
) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_known_ids(item, source_ocid, target_ocid, source_campaign_id, target_campaign_id)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_known_ids(item, source_ocid, target_ocid, source_campaign_id, target_campaign_id)
            for item in value
        ]
    text = str(value).strip()
    if source_ocid and text == str(source_ocid):
        return str(target_ocid)
    if source_campaign_id and target_campaign_id and text == str(source_campaign_id):
        return str(target_campaign_id)
    return value


def retarget_exclusion(
    row: dict[str, Any],
    *,
    target_ocid: str,
    temporary_id: str,
    source_ocid: str,
    source_campaign_id: str = "",
) -> dict[str, Any]:
    """Point one account exclusion at B. Nested topic and app-category ids stay."""
    entity = copy.deepcopy(row)
    for key, value in list(entity.items()):
        if isinstance(value, (dict, list)):
            entity[key] = _replace_known_ids(value, source_ocid, target_ocid, source_campaign_id, "")
            continue
        text = str(value).strip()
        if str(key) == "1" or (source_ocid and text == str(source_ocid)):
            entity[key] = str(target_ocid)
        elif str(key) == "3" or (text.isdigit() and len(text) >= 8):
            entity[key] = str(temporary_id)
    entity["1"] = str(target_ocid)
    entity["3"] = str(temporary_id)
    return entity


def retarget_customer_row(row: dict[str, Any], *, target_ocid: str, source_ocid: str) -> dict[str, Any]:
    """Rewrite the customer id and leave the suitability value in place."""
    entity = copy.deepcopy(row)
    for key, value in list(entity.items()):
        if isinstance(value, (dict, list)):
            entity[key] = _replace_known_ids(value, source_ocid, target_ocid, "", "")
            continue
        if str(key) == "1" or (source_ocid and str(value).strip() == str(source_ocid)):
            entity[key] = str(target_ocid)
    entity["1"] = str(target_ocid)
    return entity


def retarget_goal_row(
    row: dict[str, Any],
    *,
    target_ocid: str,
    target_campaign_id: str,
    temporary_id: str,
    source_ocid: str,
    source_campaign_id: str,
) -> dict[str, Any]:
    entity = copy.deepcopy(row)
    for key, value in list(entity.items()):
        if isinstance(value, (dict, list)):
            entity[key] = _replace_known_ids(
                value, source_ocid, target_ocid, source_campaign_id, target_campaign_id,
            )
            continue
        text = str(value).strip()
        if source_ocid and text == str(source_ocid):
            entity[key] = str(target_ocid)
        elif source_campaign_id and text == str(source_campaign_id):
            entity[key] = str(target_campaign_id)
        elif text.isdigit() and len(text) >= 8:
            entity[key] = str(temporary_id)
    return entity


def _owned_by_account(row: dict[str, Any], ocid: str) -> bool:
    owner = str(row.get("1") or "").strip()
    if not owner or owner == str(ocid):
        return True
    return not (owner.isdigit() and len(owner) >= 6)


def _list_rows(page: Any, ocid: str, path: str, fields: tuple[str, ...], step: str) -> list[dict[str, Any]]:
    errors: list[str] = []
    for filtered in (True, False):
        try:
            response = _call(
                page, path, _customer_list_request(ocid, fields, filtered=filtered), step,
            )
            rows = [row for row in _as_rows(response) if _owned_by_account(row, ocid)]
            return rows
        except HumanActionRequired:
            raise
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("；".join(errors))


def _list_spec(page: Any, ocid: str, spec: ExclusionSpec) -> tuple[list[dict[str, Any]], str]:
    errors: list[str] = []
    for path in EXCLUSION_LIST_PATHS:
        try:
            rows = _list_rows(page, ocid, path, spec.list_fields, f"读取{spec.section}")
            return rows, path
        except HumanActionRequired:
            raise
        except Exception as exc:
            errors.append(f"{path}：{exc}")
    raise RuntimeError("；".join(errors))


def _remember(warnings: list[str], text: str) -> None:
    if text and text not in warnings:
        warnings.append(text)


def read_account_level(page: Any, ocid: str, campaign_id: str) -> dict[str, Any]:
    """Read A's inventory type, exclusions, 联系人 actions, and campaign goals."""
    ocid = str(ocid)
    campaign_id = str(campaign_id)
    gaps: list[dict[str, str]] = []
    found: dict[str, dict[str, Any]] = {}
    failed = 0
    ignore = {ocid, campaign_id}

    for spec in SUITABILITY_SPECS:
        try:
            rows, path = _list_spec(page, ocid, spec)
        except HumanActionRequired:
            raise
        except Exception as exc:
            failed += 1
            gaps.append({
                "section": spec.section,
                "kind": spec.kind,
                "reason": str(exc),
            })
            continue
        for index, row in enumerate(rows, 1):
            if not _row_has_payload(row):
                continue
            fingerprint = exclusion_fingerprint(row, ignore)
            current = found.get(fingerprint)
            if current is None:
                found[fingerprint] = {
                    "kind": spec.kind,
                    "section": spec.section,
                    "source_criterion_id": str(row.get("3") or f"{spec.kind}:{index}"),
                    "write_fields": list(spec.write_fields),
                    "mutate_path": path.replace("/List", "/Mutate"),
                    "row": row,
                }
                continue
            # The same row came back for another field mask. Keep one copy and
            # union the write fields so a mask the server ignored still names
            # the payload that is actually in the row.
            for field in spec.write_fields:
                if field not in current["write_fields"]:
                    current["write_fields"].append(field)
    exclusions = list(found.values())

    if failed == len(SUITABILITY_SPECS):
        detail = "；".join(item["reason"] for item in gaps[:2])
        raise RuntimeError(f"账号内容适宜性全部读取失败：{detail}")

    inventory: dict[str, Any] | None = None
    try:
        rows = _list_rows(
            page, ocid, CUSTOMER_LIST_PATH,
            ("customer_id", INVENTORY_FIELD),
            "读取广告资源类型",
        )
        if len(rows) == 1 and exclusion_fingerprint(rows[0], {ocid}) != "{}":
            inventory = {"row": rows[0]}
        else:
            gaps.append({
                "section": "广告资源类型",
                "kind": "inventory",
                "reason": "没有返回具体的广告资源类型",
            })
    except HumanActionRequired:
        raise
    except Exception as exc:
        gaps.append({"section": "广告资源类型", "kind": "inventory", "reason": str(exc)})

    conversion_actions: list[dict[str, Any]] = []
    conversion_errors: list[str] = []
    conversion_path = ""
    for path in CONVERSION_LIST_PATHS:
        try:
            rows = _list_rows(page, ocid, path, CONVERSION_LIST_FIELDS, "读取转化目标")
            conversion_path = path
            conversion_actions = [
                {
                    "write_fields": list(CONVERSION_WRITE_FIELDS),
                    "mutate_path": path.replace("/List", "/Mutate"),
                    "row": row,
                }
                for row in rows
                if is_contact_action(row)
            ]
            break
        except HumanActionRequired:
            raise
        except Exception as exc:
            conversion_errors.append(f"{path}：{exc}")
    if not conversion_path:
        gaps.append({
            "section": "联系人转化目标",
            "kind": "conversion_action",
            "reason": "；".join(conversion_errors),
        })

    campaign_goals: list[dict[str, Any]] = []
    try:
        response = _call(
            page,
            CAMPAIGN_GOAL_LIST_PATH,
            _campaign_list_request(ocid, campaign_id, CAMPAIGN_GOAL_FIELDS),
            "读取系列转化目标",
        )
        for row in _as_rows(response):
            if _row_has_payload(row):
                campaign_goals.append({
                    "write_fields": list(CAMPAIGN_GOAL_FIELDS),
                    "mutate_path": CAMPAIGN_GOAL_MUTATE_PATH,
                    "row": row,
                })
    except HumanActionRequired:
        raise
    except Exception as exc:
        gaps.append({"section": "系列转化目标", "kind": "campaign_goal", "reason": str(exc)})

    return {
        "source_ocid": ocid,
        "source_campaign_id": campaign_id,
        "inventory": inventory,
        "exclusions": exclusions,
        "conversion_actions": conversion_actions,
        "campaign_goals": campaign_goals,
        "gaps": gaps,
    }


def _source_ids(account_level: dict[str, Any]) -> tuple[str, str]:
    return (
        str(account_level.get("source_ocid") or ""),
        str(account_level.get("source_campaign_id") or ""),
    )


def _note_export_gaps(account_level: dict[str, Any], state: dict[str, Any]) -> None:
    warnings = state["account_level_warnings"]
    for gap in account_level.get("gaps") or []:
        section = str(gap.get("section") or "内容适宜性")
        reason = str(gap.get("reason") or "")
        _remember(warnings, f"{section}没从 A 读到，B 上不会自动补。{reason}".strip())


def apply_inventory(
    page: Any,
    target_ocid: str,
    account_level: dict[str, Any],
    state: dict[str, Any],
    log: Callable[[str], None],
) -> bool:
    ensure_account_level_state(state)
    if state.get("inventory_applied"):
        return False
    inventory = account_level.get("inventory") or {}
    source_row = inventory.get("row") if isinstance(inventory, dict) else None
    if not isinstance(source_row, dict):
        state["inventory_applied"] = True
        return False
    source_ocid, _campaign_id = _source_ids(account_level)
    try:
        current = _list_rows(
            page, target_ocid, CUSTOMER_LIST_PATH,
            ("customer_id", INVENTORY_FIELD),
            "读取目标广告资源类型",
        )
    except HumanActionRequired:
        raise
    except Exception as exc:
        current = []
        log(f"目标账号的广告资源类型没读到，仍按 A 的设置写入：{exc}")
    source_key = exclusion_fingerprint(source_row, {source_ocid})
    if len(current) == 1 and exclusion_fingerprint(current[0], {str(target_ocid)}) == source_key:
        state["inventory_applied"] = True
        log("广告资源类型和 A 一样，不改")
        return False
    entity = retarget_customer_row(source_row, target_ocid=target_ocid, source_ocid=source_ocid)
    _call(
        page,
        CUSTOMER_MUTATE_PATH,
        _mutate_request(target_ocid, [{
            "1": 3,
            "2": [INVENTORY_FIELD],
            "3": entity,
        }]),
        "写入广告资源类型",
    )
    state["inventory_applied"] = True
    log("广告资源类型已改成和 A 一样")
    return True


def missing_exclusions(
    entries: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
    *,
    source_ignore: set[str],
    target_ignore: set[str],
    applied: set[str],
) -> list[tuple[str, dict[str, Any]]]:
    target_keys = {exclusion_fingerprint(row, target_ignore) for row in target_rows}
    missing: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for entry in entries:
        row = entry.get("row")
        if not isinstance(row, dict):
            continue
        fingerprint = exclusion_fingerprint(row, source_ignore)
        if fingerprint in seen or fingerprint in applied or fingerprint in target_keys:
            continue
        seen.add(fingerprint)
        missing.append((fingerprint, entry))
    return missing


def apply_exclusions(
    page: Any,
    target_ocid: str,
    account_level: dict[str, Any],
    state: dict[str, Any],
    log: Callable[[str], None],
) -> bool:
    ensure_account_level_state(state)
    source_ocid, source_campaign_id = _source_ids(account_level)
    source_ignore = {item for item in (source_ocid, source_campaign_id) if item}
    target_ignore = {str(target_ocid)}
    applied = {str(item) for item in state.get("account_exclusion_keys") or []}
    wrote = False
    failed = 0
    present = [entry for entry in account_level.get("exclusions") or [] if isinstance(entry, dict)]
    kinds = {str(entry.get("kind") or "") for entry in present}
    for spec in SUITABILITY_SPECS:
        entries = [entry for entry in present if str(entry.get("kind") or "") == spec.kind]
        if not entries:
            continue
        try:
            target_rows, path = _list_spec(page, target_ocid, spec)
        except HumanActionRequired:
            raise
        except Exception as exc:
            failed += 1
            _remember(state["account_level_warnings"], f"{spec.section}没能写入 B：{exc}")
            continue
        already = []
        for entry in entries:
            row = entry.get("row")
            if not isinstance(row, dict):
                continue
            fingerprint = exclusion_fingerprint(row, source_ignore)
            if fingerprint in applied:
                continue
            if any(exclusion_fingerprint(target, target_ignore) == fingerprint for target in target_rows):
                already.append(fingerprint)
        if already:
            applied.update(already)
            state["account_exclusion_keys"] = sorted(applied)
        pending = missing_exclusions(
            entries, target_rows,
            source_ignore=source_ignore, target_ignore=target_ignore, applied=applied,
        )
        if not pending:
            log(f"{spec.section}：B 已经有了，不重复加")
            continue
        mutate_path = str(entries[0].get("mutate_path") or path.replace("/List", "/Mutate"))
        for offset in range(0, len(pending), _MUTATE_CHUNK):
            chunk = pending[offset:offset + _MUTATE_CHUNK]
            operations = []
            keys = []
            for index, (fingerprint, entry) in enumerate(chunk, 1):
                operations.append({
                    "1": 1,
                    "2": list(entry.get("write_fields") or spec.write_fields),
                    "3": retarget_exclusion(
                        entry["row"],
                        target_ocid=target_ocid,
                        temporary_id=str(-(offset + index)),
                        source_ocid=source_ocid,
                        source_campaign_id=source_campaign_id,
                    ),
                })
                keys.append(fingerprint)
            _call(
                page,
                mutate_path,
                _mutate_request(target_ocid, operations),
                f"写入{spec.section}",
            )
            applied.update(keys)
            state["account_exclusion_keys"] = sorted(applied)
            wrote = True
        log(f"{spec.section}：已补上 {len(pending)} 条")
    if kinds and failed == len(kinds):
        raise RuntimeError("账号排除项全部写入失败：" + "；".join(state["account_level_warnings"][-2:]))
    return wrote


def apply_conversion_actions(
    page: Any,
    target_ocid: str,
    account_level: dict[str, Any],
    state: dict[str, Any],
    log: Callable[[str], None],
) -> bool:
    ensure_account_level_state(state)
    entries = [
        entry for entry in account_level.get("conversion_actions") or []
        if isinstance(entry, dict) and isinstance(entry.get("row"), dict) and is_contact_action(entry["row"])
    ]
    if not entries:
        log("A 上没有名为「联系人」的转化目标，不新建")
        return False
    source_ocid, _campaign_id = _source_ids(account_level)
    ready = {str(item) for item in state.get("conversion_actions_ready") or []}
    wrote = False
    list_errors: list[str] = []
    target_rows: list[dict[str, Any]] | None = None
    list_path = ""
    for path in CONVERSION_LIST_PATHS:
        try:
            target_rows = _list_rows(page, target_ocid, path, CONVERSION_LIST_FIELDS, "读取目标转化目标")
            list_path = path
            break
        except HumanActionRequired:
            raise
        except Exception as exc:
            list_errors.append(str(exc))
    if target_rows is None:
        raise RuntimeError("读取 B 的转化目标失败：" + "；".join(list_errors))
    existing = {exclusion_fingerprint(row, {str(target_ocid)}) for row in target_rows if is_contact_action(row)}
    pending: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for entry in entries:
        fingerprint = exclusion_fingerprint(entry["row"], {source_ocid})
        if fingerprint in seen or fingerprint in ready or fingerprint in existing:
            ready.add(fingerprint)
            continue
        seen.add(fingerprint)
        pending.append((fingerprint, entry))
    state["conversion_actions_ready"] = sorted(ready)
    if not pending:
        log("联系人转化目标 B 已经有了，不重复创建")
        return False
    for offset in range(0, len(pending), _MUTATE_CHUNK):
        chunk = pending[offset:offset + _MUTATE_CHUNK]
        operations = []
        keys = []
        for index, (fingerprint, entry) in enumerate(chunk, 1):
            operations.append({
                "1": 1,
                "2": list(entry.get("write_fields") or CONVERSION_WRITE_FIELDS),
                "3": retarget_exclusion(
                    entry["row"],
                    target_ocid=target_ocid,
                    temporary_id=str(-(offset + index)),
                    source_ocid=source_ocid,
                ),
            })
            keys.append(fingerprint)
        mutate_path = str(chunk[0][1].get("mutate_path") or list_path.replace("/List", "/Mutate"))
        _call(page, mutate_path, _mutate_request(target_ocid, operations), "创建联系人转化目标")
        ready.update(keys)
        state["conversion_actions_ready"] = sorted(ready)
        wrote = True
    log(f"已在 B 创建联系人转化目标 {len(pending)} 个。网页跟踪代码需要你自己装，程序不会装。")
    return wrote


def apply_campaign_goals(
    page: Any,
    target_ocid: str,
    campaign_id: str,
    account_level: dict[str, Any],
    state: dict[str, Any],
    log: Callable[[str], None],
) -> bool:
    """Replay A's biddable goals onto the new campaign. No campaign, no write."""
    ensure_account_level_state(state)
    if state.get("campaign_goal_applied"):
        return False
    if not str(campaign_id):
        return False
    entries = [
        entry for entry in account_level.get("campaign_goals") or []
        if isinstance(entry, dict) and isinstance(entry.get("row"), dict)
    ]
    if not entries:
        state["campaign_goal_applied"] = True
        return False
    source_ocid, source_campaign_id = _source_ids(account_level)
    try:
        response = _call(
            page,
            CAMPAIGN_GOAL_LIST_PATH,
            _campaign_list_request(target_ocid, campaign_id, CAMPAIGN_GOAL_FIELDS),
            "读取目标系列转化目标",
        )
        current = _as_rows(response)
    except HumanActionRequired:
        raise
    except Exception as exc:
        raise RuntimeError(f"读取新系列的转化目标失败：{exc}") from exc
    source_ignore = {item for item in (source_ocid, source_campaign_id) if item}
    target_ignore = {str(target_ocid), str(campaign_id)}
    current_keys = {exclusion_fingerprint(row, target_ignore) for row in current}
    pending: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for entry in entries:
        fingerprint = exclusion_fingerprint(entry["row"], source_ignore)
        if fingerprint in seen or fingerprint in current_keys:
            continue
        seen.add(fingerprint)
        pending.append((fingerprint, entry))
    if not pending:
        state["campaign_goal_applied"] = True
        log("新系列的转化目标和 A 一样，不重复加")
        return False
    operations = []
    for index, (_fingerprint, entry) in enumerate(pending, 1):
        operations.append({
            "1": 1,
            "2": list(entry.get("write_fields") or CAMPAIGN_GOAL_FIELDS),
            "3": retarget_goal_row(
                entry["row"],
                target_ocid=target_ocid,
                target_campaign_id=str(campaign_id),
                temporary_id=str(-index),
                source_ocid=source_ocid,
                source_campaign_id=source_campaign_id,
            ),
        })
    mutate_path = str(entries[0].get("mutate_path") or CAMPAIGN_GOAL_MUTATE_PATH)
    _call(
        page,
        mutate_path,
        _mutate_request(target_ocid, operations),
        "写入系列转化目标",
    )
    state["campaign_goal_applied"] = True
    log(f"新系列已使用 A 的转化目标 {len(pending)} 条")
    return True


def apply_account_settings(
    page: Any,
    target_ocid: str,
    account_level: dict[str, Any] | None,
    state: dict[str, Any],
    log: Callable[[str], None],
) -> bool:
    """Add A's account-level settings that B does not already have."""
    ensure_account_level_state(state)
    if not isinstance(account_level, dict) or not account_level:
        return False
    _note_export_gaps(account_level, state)
    wrote = apply_inventory(page, target_ocid, account_level, state, log)
    wrote = apply_exclusions(page, target_ocid, account_level, state, log) or wrote
    wrote = apply_conversion_actions(page, target_ocid, account_level, state, log) or wrote
    return wrote


def summarize_account_level(snapshot: dict[str, Any]) -> str:
    data = snapshot.get("account_level")
    if not isinstance(data, dict) or not data:
        return ""
    parts: list[str] = []
    inventory = data.get("inventory")
    if isinstance(inventory, dict) and isinstance(inventory.get("row"), dict):
        parts.append("广告资源类型")
    actions = data.get("conversion_actions") or []
    if actions:
        parts.append(f"联系人 {len(actions)}")
    goals = data.get("campaign_goals") or []
    if goals:
        parts.append(f"系列转化目标 {len(goals)}")
    counts: dict[str, int] = {}
    for entry in data.get("exclusions") or []:
        if not isinstance(entry, dict):
            continue
        section = str(entry.get("section") or entry.get("kind") or "")
        counts[section] = counts.get(section, 0) + 1
    for section, count in counts.items():
        parts.append(f"{section} {count}")
    gaps = data.get("gaps") or []
    body = "、".join(parts) if parts else "没有读到可复制的账号设置"
    if gaps:
        body += f"；{len(gaps)} 项没读到"
    return body
