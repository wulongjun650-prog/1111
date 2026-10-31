from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .ads import (
    AdCatalog,
    RESPONSIVE_DISPLAY_TYPE,
    ad_group_list_request,
    ad_list_request,
    build_batch_request,
    campaign_list_request,
    image_asset_wrapper,
    parse_ad_groups,
    parse_ads,
    parse_campaigns,
)
from .config import data_dir, format_cid
from .image_upload import download_preview_image, upload_images
from .protocol import AccountPage, CAMPAIGN_STATUS_ENABLED, CAMPAIGN_STATUS_PAUSED, ProtocolRunner
from .protocol_catalog import CHILD_STATUS_ENABLED, CHILD_STATUS_PAUSED


APP_CHANNEL = 7
DISPLAY_CHANNEL = 2  # 与 ads.py 使用的同一内部协议枚举值一致（已被生产代码验证）
SEARCH_CHANNEL = None  # 尚未通过真实账户数据核实该内部协议下搜索广告系列的渠道枚举值，暂不支持
SUPPORTED_CHANNELS = {APP_CHANNEL, DISPLAY_CHANNEL}
PAUSED_CAMPAIGN = CAMPAIGN_STATUS_PAUSED
PAUSED_CHILD = CHILD_STATUS_PAUSED
CAMPAIGN_TARGET_STATUS = {True: PAUSED_CAMPAIGN, False: CAMPAIGN_STATUS_ENABLED}
CHILD_TARGET_STATUS = {True: PAUSED_CHILD, False: CHILD_STATUS_ENABLED}

# 共享排除集合（SharedSetService）子类型：与实地抓包证据一致
# （.protocol-cycle/evidence/negative_keyword_shared_set_2026-08-20.md /
#   placement_exclusion_shared_set_2026-08-20.md）。
SHARED_SET_TYPE_NEGATIVE_KEYWORDS = 1
SHARED_SET_TYPE_PLACEMENT_EXCLUSIONS = 17
SHARED_CRITERION_TYPE_KEYWORD = 19
SHARED_CRITERION_TYPE_PLACEMENT = 28
KEYWORD_MATCH_TYPE_BROAD = 3
# 两个子类型各自的入口子应用命名空间不同（实测确认，非猜测），关联/创建/删除均按此区分。
SHARED_SET_BATCH_PATH = {
    SHARED_SET_TYPE_NEGATIVE_KEYWORDS: "/aw_cm/_/rpc/BatchService/Batch",
    SHARED_SET_TYPE_PLACEMENT_EXCLUSIONS: "/aw_dva/_/rpc/BatchService/Batch",
}
CAMPAIGN_SHARED_SET_MUTATE_PATH = {
    SHARED_SET_TYPE_NEGATIVE_KEYWORDS: "/aw_cm/_/rpc/CampaignSharedSetService/Mutate",
    SHARED_SET_TYPE_PLACEMENT_EXCLUSIONS: "/aw_dva/_/rpc/CampaignSharedSetService/Mutate",
}
# 受众定向（AdGroupCriterionService）字段号：抓包证据见
# .protocol-cycle/evidence/custom_audience_association_2026-08-20.md
AUDIENCE_FIELD_USER_INTEREST = "50"
AUDIENCE_FIELD_CUSTOM_AUDIENCE = "108"
_AUDIENCE_FIELD_NAME = {
    AUDIENCE_FIELD_USER_INTEREST: "user_interest_id",
    AUDIENCE_FIELD_CUSTOM_AUDIENCE: "custom_audience_id",
}
# AudienceService.List 响应行使用与 AdGroupCriterionService.Mutate 完全不同的字段编号
# 空间（两者是不同的 proto 消息，字段名相同不代表字段号相同）。实测确认：field_path
# 名 "custom_audience_id" 在 List 响应里落在字段 75（已用 CustomAudienceService.List
# 交叉核实：SUV 记录 "75"="1001105812" 与其真实 custom_audience_id 完全一致），字段
# 108 只在 Mutate 请求体里有效。field_path 名 "user_interest_id" 在 List 响应里没有
# 对应字段（服务端静默忽略未知/只写字段名，不报错也不返回数据）；UserInterest 类型的
# 可用 ID 就是该行本身的 criterion_id（字段3，全局分类目录ID，账户间通用）。
AUDIENCE_LIST_FIELD_CUSTOM_AUDIENCE_ID = "75"
_SHARED_CRITERION_TYPE = {
    SHARED_SET_TYPE_NEGATIVE_KEYWORDS: SHARED_CRITERION_TYPE_KEYWORD,
    SHARED_SET_TYPE_PLACEMENT_EXCLUSIONS: SHARED_CRITERION_TYPE_PLACEMENT,
}
# List 读取实测（负排除关键词/展示位置排除两种子类型的 SharedSetService.List /
# SharedCriterionService.List "无差异"，见 placement_exclusion_shared_set_2026-08-20.md
# 首个对比表格），统一走 aw_dva；SharedCriterionService.Mutate 的独立追加调用（非打包创建）
# 命名空间沿用 SHARED_SET_BATCH_PATH 同款按子类型区分的假设，未单独抓包核实，标记为待验证。
SHARED_SET_LIST_PATH = "/aw_dva/_/rpc/SharedSetService/List"
SHARED_CRITERION_LIST_PATH = "/aw_dva/_/rpc/SharedCriterionService/List"
# CampaignSharedSetService.List：命名空间未单独抓包核实，按 SharedSetService.List /
# SharedCriterionService.List 已验证的"两种子类型统一走 aw_dva"模式做同类推断，
# 若服务端拒绝需要按子类型改用 CAMPAIGN_SHARED_SET_MUTATE_PATH 同款拆分（plan 文件
# "关键风险"第2点，实现导出阶段读取逻辑时应用真实请求交叉验证一次）。
CAMPAIGN_SHARED_SET_LIST_PATH = "/aw_dva/_/rpc/CampaignSharedSetService/List"
SHARED_CRITERION_MUTATE_PATH = {
    SHARED_SET_TYPE_NEGATIVE_KEYWORDS: "/aw_cm/_/rpc/SharedCriterionService/Mutate",
    SHARED_SET_TYPE_PLACEMENT_EXCLUSIONS: "/aw_dva/_/rpc/SharedCriterionService/Mutate",
}
# AudienceService.List 命名空间：见 custom_audience_association_2026-08-20.md 的
# "List 读取"章节，独立调用命名空间为 aw_dva（非 AdGroupCriterionService）。
AUDIENCE_LIST_PATH = "/aw_dva/_/rpc/AudienceService/List"
AD_GROUP_CRITERION_MUTATE_PATH = "/aw_dva/_/rpc/AdGroupCriterionService/Mutate"
CUSTOM_AUDIENCE_LIST_PATH = "/aw_dva/_/rpc/CustomAudienceService/List"

CAMPAIGN_SNAPSHOT_FIELDS = [
    "campaign_id", "name", "status", "type", "advertising_channel_type",
    "advertising_channel_sub_type", "budget_id", "budget_name", "budget_amount",
    "currency_code", "bid_config.type", "bid_config.max_conversions.scheme",
    # bid_config 是 oneof 结构，type 只是判别符字段；ADD 时服务端要求判别符对应的
    # 子消息本身也被显式设置（哪怕子字段全为默认值），否则报 FIELD_ERROR_REQUIRED
    # bidding_strategy。因此这里必须逐一列出每种出价策略分支的子字段，缺一个分支就会
    # 导致该分支类型的广告系列快照导出不全、写入目标账户时报错（2026-08-20 实测确认）。
    "bid_config.manual_cpc.enhanced_cpc_enabled",
    "bid_config.manual_cpm.active_view_cpm_enabled",
    "bid_config.target_spend.enhanced_cpc_enabled",
    "bid_config.max_conversions.target_cpa",
    "bid_config.max_conversion_value.target_roas",
    "bid_config.target_roas.target_roas",
    "bid_config.target_impression_share.target_impression_share_micros",
    "mutable_universal_app_campaign_info.bidding_strategy_goal_type",
    "mutable_universal_app_campaign_info.app_id",
    "contains_eu_political_advertising",
]
AD_GROUP_SNAPSHOT_FIELDS = [
    "campaign_id", "ad_group_id", "name", "status", "type",
    "advertising_channel_type", "advertising_channel_sub_type", "campaign_status",
]
APP_AD_SNAPSHOT_FIELDS = [
    "campaign_id", "ad_group_id", "ad.ad_id", "ad.name", "status", "ad.type",
    "ad.final_urls", "ad.mutable_data", "ad.id", "ad.visible_type",
    "ad.universal_app_ad.headlines", "ad.universal_app_ad.descriptions",
    "ad.universal_app_ad.images", "ad.universal_app_ad.youtube_videos",
    "ad.universal_app_ad.html5_media_bundles", "ad.universal_app_ad.mandatory_ad_text",
    "ad.universal_app_ad.app_deep_link", "ad.universal_app_ad.app_custom_detail_pages",
    "policy_info", "advertising_channel_type", "advertising_channel_sub_type",
]
DISPLAY_AD_SNAPSHOT_FIELDS = [
    "campaign_id", "ad_group_id", "ad.ad_id", "ad.name", "status", "ad.type",
    "ad.final_urls", "ad.mutable_data", "ad.id", "ad.visible_type",
    "ad.responsive_display_ad.headlines", "ad.responsive_display_ad.long_headline",
    "ad.responsive_display_ad.descriptions", "ad.responsive_display_ad.business_name",
    "ad.responsive_display_ad.marketing_images", "ad.responsive_display_ad.square_marketing_images",
    "ad.responsive_display_ad.portrait_marketing_images",
    "policy_info", "advertising_channel_type", "advertising_channel_sub_type",
]
# Search（响应式搜索广告）字段路径尚未通过真实账户数据核实，暂不对外提供快照/重建能力。


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _filter(field: str, value: str) -> dict[str, Any]:
    return {"1": str(field), "2": 1, "4": [{"3": str(value)}]}


def _list_request(
    ocid: str, fields: list[str], *, campaign_id: str,
) -> dict[str, Any]:
    return {
        "1": {"3": {"1": str(ocid)}},
        "2": {
            "1": list(fields), "2": [_filter("campaign_id", campaign_id)], "14": True,
        },
    }


def shared_set_list_by_id_request(ocid: str, shared_set_id: str) -> dict[str, Any]:
    """按真实 shared_set_id 读取共享集合详情（导出阶段：已知关联ID，反查名称/类型）。

    与 shared_set_list_by_name_request 共用同一份 field_paths，仅过滤条件从
    customer_id+type+name 换成 customer_id+shared_set_id，服务与字段结构未变，
    未单独抓包验证按 ID 过滤这一具体组合，属于按 _filter 通用范式的推断实现。
    """
    return {
        "1": {"3": {"1": str(ocid)}},
        "2": {
            "1": ["shared_set_id", "customer_id", "name", "type", "status"],
            "2": [
                _filter("customer_id", ocid),
                _filter("shared_set_id", shared_set_id),
            ],
            "14": True,
        },
    }


def shared_set_list_by_name_request(ocid: str, set_kind: int, name: str) -> dict[str, Any]:
    """按名称查询目标账户是否已有同名共享集合（合并追加场景的前置查询）。

    字段结构参照 protocol.py 的 shared_set_list_request，但该函数硬编码 type=17，
    此处改为按 set_kind 参数化，并新增按 name 的 EQUALS 过滤——name 过滤未在抓包证据
    中直接验证（证据只覆盖了 Mutate 场景），是按现有 filter 结构范式做的推断，若服务端
    不支持该过滤条件，调用方应退化为不带 name 过滤后本地比对。
    """
    return {
        "1": {"3": {"1": str(ocid)}},
        "2": {
            "1": ["shared_set_id", "customer_id", "name", "type", "status"],
            "2": [
                _filter("customer_id", ocid),
                _filter("type", set_kind),
                _filter("name", name),
            ],
            "14": True,
        },
    }


def shared_criterion_list_request(ocid: str, shared_set_id: str, set_kind: int) -> dict[str, Any]:
    """读取共享集合内已有 criterion，用于合并追加时判断哪些内容已存在。

    与 protocol.py 中同名函数签名不同（多一个 set_kind 参数），两者服务不同场景，
    不应混用；此处按 set_kind 选择 value 字段名（keyword vs website）。
    """
    value_field = "keyword" if set_kind == SHARED_SET_TYPE_NEGATIVE_KEYWORDS else "website"
    return {
        "1": {"3": {"1": str(ocid)}},
        "2": {
            "1": ["shared_set_id", "criterion_id", "type", value_field],
            "2": [
                _filter("shared_set_id", shared_set_id),
                _filter("customer_id", ocid),
            ],
            "14": True,
        },
    }


def _shared_criterion_operation(
    ocid: str, shared_set_id: str, set_kind: int, value: str, *, temporary_id: str,
) -> dict[str, Any]:
    criterion_type = _SHARED_CRITERION_TYPE.get(set_kind)
    if criterion_type is None:
        raise ValueError(f"不支持的共享集合子类型: {set_kind}")
    entity: dict[str, Any] = {
        "1": str(ocid), "2": str(shared_set_id), "3": str(temporary_id),
        "9": criterion_type,
    }
    if set_kind == SHARED_SET_TYPE_NEGATIVE_KEYWORDS:
        entity["101"] = {"1": str(value), "2": KEYWORD_MATCH_TYPE_BROAD}
        fields = ["shared_set_id", "keyword"]
    else:
        entity["102"] = {"1": str(value)}
        fields = ["shared_set_id", "website"]
    return {"1": 1, "2": fields, "3": entity}


def create_shared_set_and_criteria_batch_request(
    ocid: str, set_kind: int, name: str, values: list[str],
) -> dict[str, Any]:
    """一次性打包 SharedSetService.Mutate(CREATE) + 若干 SharedCriterionService.Mutate(CREATE)。

    字段来源：negative_keyword_shared_set_2026-08-20.md 第1节 / placement_exclusion_shared_set_2026-08-20.md
    第1节。共享集合的临时ID "-1" 在同一批次内被 criterion 的 shared_set_id 引用，
    与两份证据文档记录的真实抓包顺序一致。
    """
    if set_kind not in _SHARED_CRITERION_TYPE:
        raise ValueError(f"不支持的共享集合子类型: {set_kind}")
    if not values:
        raise ValueError("共享集合至少需要一条排除内容")
    shared_set = {
        "1": {"2": {"1": False}, "3": {"2": str(ocid)}},
        "2": [{
            "1": 1,
            "2": ["name", "type", "shared_set_id"],
            "3": {"1": str(ocid), "2": "-1", "3": str(name), "5": set_kind},
        }],
    }
    criteria = {
        "1": {"2": {"1": False}, "3": {"2": str(ocid)}},
        "2": [
            _shared_criterion_operation(ocid, "-1", set_kind, value, temporary_id=str(-(index + 1)))
            for index, value in enumerate(values)
        ],
    }
    return {
        "2": [_compact(shared_set), _compact(criteria)],
        "3": [
            {"1": "ads.awapps.anji.proto.infra.sharedset.SharedSetService", "2": "Mutate"},
            {"1": "ads.awapps.anji.proto.infra.sharedcriterion.SharedCriterionService", "2": "Mutate"},
        ],
    }


def append_shared_criteria_request(
    ocid: str, shared_set_id: str, set_kind: int, values: list[str],
) -> dict[str, Any]:
    """向已存在的共享集合追加缺失内容（合并追加场景，shared_set_id 为真实ID）。"""
    if not values:
        raise ValueError("待追加内容为空")
    operations = [
        _shared_criterion_operation(ocid, shared_set_id, set_kind, value, temporary_id=str(-(index + 1)))
        for index, value in enumerate(values)
    ]
    return {"1": {"2": {"1": False}, "3": {"2": str(ocid)}}, "2": operations}


def campaign_shared_set_mutate_request(
    ocid: str, campaign_id: str, campaign_name: str, shared_set_id: str, *,
    remove: bool = False,
) -> dict[str, Any]:
    """关联/取消关联广告系列与共享集合。

    字段结构来自两份证据文档的第2/3节：请求体是单个 operation 对象（无外层auth/list
    包装），与本文件其余 Mutate 构造函数的结构不同，已按实测原样保留，不额外套壳。
    set_kind 只影响调用方选择 CAMPAIGN_SHARED_SET_MUTATE_PATH 的 URL 命名空间，
    不影响请求体本身（两种子类型的请求体结构完全一致）。
    """
    return {
        "1": 2 if remove else 1,
        "2": ["campaign_id", "shared_set_id"],
        "3": {
            "1": str(ocid), "2": str(shared_set_id), "3": str(campaign_id),
            "5": str(campaign_name),
        },
    }


def campaign_shared_set_list_request(ocid: str, campaign_id: str) -> dict[str, Any]:
    """读取指定广告系列当前已关联的共享集合（campaign ↔ shared_set 关联表）。

    未在抓包证据中直接验证，是按本文件 _list_request 的通用范式（认证壳 + field_paths
    + campaign_id 过滤）做的推断实现——两份共享集合证据文档只覆盖了关联/取消关联的
    Mutate 场景，List 场景待实现时用真实请求交叉验证一次（plan 文件"关键风险"第2点）。
    """
    return {
        "1": {"3": {"1": str(ocid)}},
        "2": {
            "1": ["campaign_id", "shared_set_id", "shared_set.name", "shared_set.type"],
            "2": [_filter("campaign_id", campaign_id)],
            "14": True,
        },
    }


def shared_set_delete_request(ocid: str, current_object: dict[str, Any]) -> dict[str, Any]:
    """删除共享集合（对称能力保留，recreate() 主路径不调用）。

    删除请求体里"值恒为1的未知字段"字段号随子类型漂移（否定关键词=16，展示位置排除=9，
    见 placement_exclusion_shared_set_2026-08-20.md"关键差异"一节），因此不在此处硬编码，
    要求调用方先用 SharedSetService.List 读取该共享集合的完整字段状态后原样传入
    current_object，本函数只负责把 operator 翻转为 REMOVE(2) 后提交。
    """
    entity = copy.deepcopy(current_object)
    entity["1"] = str(ocid)
    return {
        "1": {"2": {"1": False}, "3": {"2": str(ocid)}},
        "2": [{"1": 2, "2": list(entity.keys()), "3": entity}],
    }


def ad_group_audience_list_request(ocid: str, campaign_id: str, ad_group_id: str) -> dict[str, Any]:
    """读取广告组当前关联的受众定向条件（UserInterest/CustomAudience）。

    包装真实接口 AudienceService.List（命名空间 aw_dva），依据
    custom_audience_association_2026-08-20.md"List 读取"章节第1小节的独立调用抓包：
    field_paths 固定为 campaign_id/ad_group_id/criterion_id/attachment_level/status，
    按 campaign_id EQUALS + ad_group_id IN（附带兼容值"0"）过滤。抓包证据文档记录的
    请求体没有独立的 ocid 认证字段，但真实联调时服务端返回
    "ListRequest invoked without scope_customer_id specified"，证明抓包记录遗漏了这个
    字段；比照本文件其余 List 函数的 `"1": {"3": {"1": ocid}}` 包裹结构补齐。响应体外层
    "1" 字段在只有一条关联时是单对象而非数组，调用方解析时需要兼容单对象/数组两种形态。

    追加 custom_audience_id 字段路径——实测确认该 field_path 名在 List 响应里对应
    字段 75（AUDIENCE_LIST_FIELD_CUSTOM_AUDIENCE_ID，已用 CustomAudienceService.List
    交叉核实一致），与 Mutate 请求体的字段108是完全不同的编号空间。不追加
    user_interest_id：该字段名在 List 响应里读不到任何值（服务端静默忽略），
    UserInterest 类型改用该行自身的 criterion_id（字段3）作为可用ID。

    分页 `"7"` 字段此前误写成 `{"1": 0, "2": 1}`，把返回条数硬编码成1条，导致真实有
    多条受众定向关联的广告组被误判为"只关联1条"——这不是接口限制，是调用方的bug。
    改为 offset=0, limit=100（该内部服务单个广告组的受众定向条件不会达到这个量级）。
    """
    return {
        "1": {"3": {"1": str(ocid)}},
        "2": {
            "1": [
                "campaign_id", "ad_group_id", "criterion_id", "attachment_level", "status",
                "custom_audience_id",
            ],
            "2": [
                _filter("campaign_id", campaign_id),
                {"1": "ad_group_id", "2": 3, "4": [{"3": str(ad_group_id)}, {"3": "0"}]},
            ],
            "3": [{"1": "status"}],
            "7": {"1": 0, "2": 100},
        },
    }


def ad_group_audience_mutate_request(
    ocid: str, ad_group_id: str, audience_kind: str, audience_id: str, *,
    criterion_id: str | None = None, remove: bool = False,
) -> dict[str, Any]:
    """CREATE 或 REMOVE 一条受众定向条件，字段来自 custom_audience_association_2026-08-20.md。

    audience_kind："user_interest"（字段50）或 "custom_audience"（字段108），两者互斥，
    不能混用同一请求。REMOVE 需要服务端此前生成的真实 criterion_id（字段4）。
    """
    if audience_kind == "user_interest":
        field = AUDIENCE_FIELD_USER_INTEREST
    elif audience_kind == "custom_audience":
        field = AUDIENCE_FIELD_CUSTOM_AUDIENCE
    else:
        raise ValueError(f"未知受众类型: {audience_kind}")
    entity: dict[str, Any] = {
        "1": str(ocid), "3": str(ad_group_id), "15": False, field: str(audience_id),
    }
    fields = ["ad_group_id", "criterion_id", "is_negative", _AUDIENCE_FIELD_NAME[field]]
    if remove:
        if not criterion_id:
            raise ValueError("移除受众定向需要提供 criterion_id")
        entity["4"] = str(criterion_id)
    return {
        "1": {"2": {"1": False}, "3": {"2": str(ocid)}},
        "2": [{"1": 2 if remove else 1, "2": fields, "3": entity}],
    }


def target_custom_audience_list_request(ocid: str) -> dict[str, Any]:
    """列出目标账户全部 CustomAudience，供调用方按名称在本地过滤匹配。

    该服务不支持服务端按名称过滤（与 protocol.py 的 custom_audience_list_request
    结构一致），此处在 portable_copy.py 内独立定义一份，避免与 protocol.py 同名函数
    产生跨模块引用歧义（两者服务不同功能入口，字段结构目前恰好相同，但不应互相依赖）。
    """
    return {
        "1": {"3": {"1": str(ocid), "2": str(ocid)}},
        "2": {"1": ["custom_audience_id", "customer_id", "name", "type", "tokens"]},
    }


def portable_campaign_batch_request(
    *, ocid: str, name: str, source_campaign: dict[str, Any],
    target_status: int = PAUSED_CAMPAIGN,
    existing_budget_id: str | None = None,
) -> dict[str, Any]:
    """Create a campaign (App or Display) using temporary IDs.

    默认会同时新建一个私有预算（budget.amount 写入）。但 BudgetService.Mutate 只要
    涉及 amount 字段，无论新建还是修改、无论金额多小，都会被服务端要求携带只能由真实
    浏览器交互生成的 reauth proof token，纯 RPC 直连必定报
    AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED（2026-08-20 实测确认，与金额大小无关）。
    传入 existing_budget_id 可以让本次请求完全不touch BudgetService，campaign 直接
    引用一个已经存在（金额已经设置好）的 budget_id，从而绕开这个人机验证限制。
    """
    channel = int(source_campaign.get("15") or 0)
    if channel not in SUPPORTED_CHANNELS:
        raise ValueError(f"本地快照重建暂不支持该广告系列类型（channel={channel}）")
    app_info = copy.deepcopy(source_campaign.get("133") or {})
    if channel == APP_CHANNEL and not str(app_info.get("4") or "").strip():
        raise ValueError("来源应用广告系列缺少应用包名")
    bid_config = copy.deepcopy(source_campaign.get("32") or {})
    if not bid_config:
        raise ValueError("来源广告系列缺少出价配置")

    requests: list[dict[str, Any]] = []
    services: list[dict[str, str]] = []
    if existing_budget_id:
        budget_id_ref = str(existing_budget_id)
    else:
        amount = str(source_campaign.get("17") or "0")
        if not amount.isdigit() or int(amount) <= 0:
            raise ValueError("来源广告系列缺少有效日预算")
        budget = {
            "1": {"3": {"2": str(ocid)}},
            "2": [{
                "1": 1,
                "2": [
                    "is_explicitly_shared", "budget_id", "delivery_method",
                    "period", "amount", "type",
                ],
                "3": {
                    "1": str(ocid), "2": "-1", "4": amount, "5": False,
                    "7": 1, "11": 2, "14": 1,
                },
            }],
        }
        requests.append(_compact(budget))
        services.append({"1": "ads.awapps.anji.proto.infra.budget.BudgetService", "2": "Mutate"})
        budget_id_ref = "-1"
    campaign_fields = [
        "campaign_id", "name", "status", "advertising_channel_type",
        "budget_id", "bid_config.type",
        # bid_config 是 oneof；ADD 时服务端要求判别符（type）对应的子消息本身也在
        # field_paths 中被显式列出，否则报 FIELD_ERROR_REQUIRED bidding_strategy
        # （2026-08-20 实测确认）。必须与 CAMPAIGN_SNAPSHOT_FIELDS 的 bid_config 分支保持同步。
        "bid_config.manual_cpc.enhanced_cpc_enabled",
        "bid_config.manual_cpm.active_view_cpm_enabled",
        "bid_config.target_spend.enhanced_cpc_enabled",
        "bid_config.max_conversions.scheme",
        "bid_config.max_conversions.target_cpa",
        "bid_config.max_conversion_value.target_roas",
        "bid_config.target_roas.target_roas",
        "bid_config.target_impression_share.target_impression_share_micros",
        "mutable_universal_app_campaign_info.bidding_strategy_goal_type",
        "mutable_universal_app_campaign_info.app_id",
        "contains_eu_political_advertising",
    ]
    campaign_entity = {
        "1": str(ocid), "2": "-2", "11": str(name), "12": target_status,
        "15": channel, "16": budget_id_ref,
        "32": bid_config, "133": app_info,
        "354": int(source_campaign.get("354") or 2),
    }
    # advertising_channel_sub_type 是可选字段；源系列没有值时传 0 会触发
    # SERVER_ERROR_INTERNAL（2026-08-20 实测确认），因此只有源数据真正带值时才写入。
    sub_type = source_campaign.get("19")
    if sub_type:
        campaign_fields.append("advertising_channel_sub_type")
        campaign_entity["19"] = int(sub_type)
    campaign = {
        "1": {"3": {"2": str(ocid)}},
        "2": [{"1": 1, "2": campaign_fields, "3": campaign_entity}],
    }
    requests.append(_compact(campaign))
    services.append({"1": "ads.awapps.anji.proto.infra.campaign.CampaignService", "2": "Mutate"})
    return {"2": requests, "3": services}


def portable_ad_group_request(
    *, ocid: str, campaign_id: str, temporary_id: str, source_group: dict[str, Any],
    target_child_status: int = PAUSED_CHILD,
) -> dict[str, Any]:
    return {
        "1": {"3": {"2": str(ocid)}},
        "2": [{
            "1": 1,
            "2": ["campaign_id", "ad_group_id", "name", "status"],
            "3": {
                "1": str(ocid), "2": str(campaign_id), "3": str(temporary_id),
                "11": str(source_group.get("11") or "Ad group"), "14": target_child_status,
            },
        }],
    }


def _without_source_text_id(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    clone = copy.deepcopy(item)
    asset = clone.get("1")
    if isinstance(asset, dict):
        asset.pop("1", None)
    return clone


def _without_source_text_ids(items: Any) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        cleaned.append(_without_source_text_id(item))
    return cleaned


def portable_app_ad_request(
    *, ocid: str, campaign_id: str, ad_group_id: str, temporary_id: str,
    source_ad: dict[str, Any], target_assets: list[dict[str, str]],
    target_child_status: int = PAUSED_CHILD,
) -> dict[str, Any]:
    outer_source = source_ad.get("13") or {}
    mutable = copy.deepcopy(outer_source.get("100") or {})
    app_ad = copy.deepcopy(mutable.get("44") or {})
    if not app_ad:
        raise ValueError("来源广告不是可重建的 Universal App Ad")
    app_ad["2"] = _without_source_text_ids(app_ad.get("2"))
    app_ad["3"] = _without_source_text_ids(app_ad.get("3"))
    if target_assets:
        app_ad["4"] = [{
            "1": {
                "1": str(asset["target_asset_id"]),
                "2": str(asset.get("filename") or ""),
                "17": 1,
            },
        } for asset in target_assets]
    else:
        app_ad.pop("4", None)
    mutable["9"] = str(temporary_id)
    mutable["44"] = app_ad
    outer = {
        "3": int(outer_source.get("3") or 104),
        "19": int(outer_source.get("19") or 39),
        "47": str(temporary_id),
        "100": mutable,
    }
    request = {
        "1": {"2": {"1": False}, "3": {"2": str(ocid)}},
        "2": [{
            "1": 1,
            "2": ["ad.mutable_data", "status"],
            "3": {
                "1": str(ocid), "2": str(campaign_id), "3": str(ad_group_id),
                "4": str(temporary_id), "5": target_child_status, "13": outer,
            },
        }],
    }
    return build_batch_request(request)


def portable_display_ad_request(
    *, ocid: str, campaign_id: str, ad_group_id: str, temporary_id: str,
    source_ad: dict[str, Any],
    landscape_assets: list[dict[str, str]],
    square_assets: list[dict[str, str]],
    portrait_assets: list[dict[str, str]] | None = None,
    target_child_status: int = PAUSED_CHILD,
) -> dict[str, Any]:
    """Rebuild a Responsive Display Ad, carrying over the source ad's full creative.

    Field paths (ad['13']['100']['41'], creative fields '1'/'2'/'20'/'5'/'6'/'7'/'8')
    are confirmed against ads.py's real AdManager implementation for this same
    internal protocol; unlike ads.build_create_request() (which builds a fresh
    manually-edited ad), this carries over the source ad's complete creative.
    """
    outer_source = source_ad.get("13") or {}
    mutable = copy.deepcopy(outer_source.get("100") or {})
    creative = copy.deepcopy(mutable.get("41") or {})
    if not creative:
        raise ValueError("来源广告不是可重建的响应式展示广告")
    if not landscape_assets or not square_assets:
        raise ValueError("响应式展示广告必须同时包含横图和方图素材映射")
    creative["1"] = [image_asset_wrapper(a["target_asset_id"], a.get("filename") or "") for a in landscape_assets]
    creative["2"] = [image_asset_wrapper(a["target_asset_id"], a.get("filename") or "") for a in square_assets]
    if portrait_assets:
        creative["20"] = [image_asset_wrapper(a["target_asset_id"], a.get("filename") or "") for a in portrait_assets]
    else:
        creative.pop("20", None)
    creative["5"] = _without_source_text_ids(creative.get("5"))
    creative["6"] = _without_source_text_id(creative.get("6"))
    creative["7"] = _without_source_text_ids(creative.get("7"))
    mutable["9"] = str(temporary_id)
    mutable["41"] = creative
    final_urls = [str(value) for value in (mutable.get("1") or outer_source.get("5") or []) if value]
    outer = {
        "3": int(outer_source.get("3") or 103),
        "5": final_urls,
        "19": RESPONSIVE_DISPLAY_TYPE,
        "25": final_urls[0] if final_urls else "",
        "47": str(temporary_id),
        "100": mutable,
    }
    request = {
        "1": {"2": {"1": False}, "3": {"2": str(ocid)}},
        "2": [{
            "1": 1,
            "2": ["ad.mutable_data", "status"],
            "3": {
                "1": str(ocid), "2": str(campaign_id), "3": str(ad_group_id),
                "4": str(temporary_id), "5": target_child_status, "13": outer,
            },
        }],
    }
    return build_batch_request(request)


def _service_error(value: Any) -> str:
    if isinstance(value, dict):
        # 字段号 "5"/"2" 只在错误响应结构里代表错误列表；同样的字段号在其他消息
        # 类型里可能被复用为字符串/数字（如 ad_group 响应的 "21.5" 是出价金额），
        # 递归遍历真实数据时会撞见这种巧合，必须逐层类型防护，不能假设是 dict。
        field5 = value.get("5")
        errors = (field5.get("2") if isinstance(field5, dict) else None) or []
        if errors and isinstance(errors, list):
            return str(errors[0])
        for nested in value.values():
            found = _service_error(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _service_error(nested)
            if found:
                return found
    elif isinstance(value, str) and value.startswith(("MUTATE_ERROR_", "INTERNAL_ERROR")):
        return value
    return ""


AD_CREATE_ATTEMPTS = 3


def _ad_status_name(status: Any) -> str:
    return {1: "已启用", 2: "已暂停", 3: "已移除"}.get(int(status or 0), f"状态 {status}")


def describe_unreadable_ad(
    rows: list[dict[str, Any]],
    group_id: str,
    known_ids: set[str],
    expected_status: int,
) -> str:
    """Say what the ad group actually contained when the new paused ad was missing."""
    expected = _ad_status_name(expected_status)
    fresh = [
        row for row in rows
        if str(row.get("ad_group_id") or "") == str(group_id)
        and str(row.get("ad_id") or "") not in known_ids
        and int(row.get("status_raw") or 0) != 3
    ]
    if not fresh:
        return "广告组里没有新广告"
    names = "、".join(_ad_status_name(row.get("status_raw")) for row in fresh[:3])
    if len(fresh) > 3:
        names += f" 等 {len(fresh)} 条"
    return f"新广告是{names}，不是{expected}"


def _matching_ads(
    rows: list[dict[str, Any]], group_id: str, known_ids: set[str], expected_status: int,
) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if str(row.get("ad_group_id") or "") == str(group_id)
        and str(row.get("ad_id") or "") not in known_ids
        and int(row.get("status_raw") or 0) == int(expected_status)
    ]


def place_ad_with_retries(
    *,
    read_ads: Callable[[], list[dict[str, Any]]],
    submit: Callable[[], Any],
    poll_new_ad: Callable[[set[str]], dict[str, Any]],
    group_id: str,
    expected_status: int,
    reserved_ids: set[str],
    log: Callable[[str], None],
    gate_for_error: Callable[[str], BaseException | None] | None = None,
    attempts: int = AD_CREATE_ATTEMPTS,
    pause: Callable[[float], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create one ad, then read it back. Try up to three times, then say why.

    A paused ad already in the group is reused. An ad with another status is not
    created again. A USB-key prompt stops immediately.
    """
    reasons: list[str] = []
    submitted = False
    known = {str(item) for item in reserved_ids}
    sleeper = pause or time.sleep
    for attempt in range(1, attempts + 1):
        try:
            rows = read_ads()
        except Exception as exc:
            if type(exc).__name__ == "HumanActionRequired":
                raise
            reasons.append(f"第{attempt}次：读取广告失败：{exc}")
            if attempt < attempts:
                sleeper(1)
            continue
        matched = _matching_ads(rows, group_id, known, expected_status)
        if len(matched) == 1:
            if attempt > 1:
                log(f"第{attempt}次读到广告")
            return matched[0], submitted
        if len(matched) > 1:
            raise RuntimeError("广告组里有多条新的暂停广告，无法判断用哪一条")
        fresh = [
            row for row in rows
            if str(row.get("ad_group_id") or "") == str(group_id)
            and str(row.get("ad_id") or "") not in known
            and int(row.get("status_raw") or 0) != 3
        ]
        if fresh:
            reasons.append(f"第{attempt}次：{describe_unreadable_ad(rows, group_id, known, expected_status)}")
            if attempt < attempts:
                sleeper(1)
            continue
        if attempt > 1:
            log(f"广告还没出现，第{attempt}次再试")
        seen = {
            str(row.get("ad_id") or "")
            for row in rows
            if str(row.get("ad_group_id") or "") == str(group_id) and row.get("ad_id")
        }
        try:
            response = submit()
        except Exception as exc:
            if type(exc).__name__ == "HumanActionRequired":
                raise
            reasons.append(f"第{attempt}次：{exc}")
            if attempt < attempts:
                sleeper(1)
            continue
        submitted = True
        error = _service_error(response)
        if error:
            gate = gate_for_error(error) if gate_for_error else None
            if gate:
                raise gate
            reasons.append(f"第{attempt}次：{error}")
            if attempt < attempts:
                sleeper(1)
            continue
        try:
            return poll_new_ad(seen), submitted
        except Exception as exc:
            if type(exc).__name__ == "HumanActionRequired":
                raise
            text = str(exc)
            if "多个匹配" in text:
                raise
            try:
                latest = read_ads()
            except Exception as read_exc:
                if type(read_exc).__name__ == "HumanActionRequired":
                    raise
                latest = []
                text = f"读取广告失败：{read_exc}"
            else:
                matched = _matching_ads(latest, group_id, known, expected_status)
                if len(matched) == 1:
                    return matched[0], submitted
                if len(matched) > 1:
                    raise RuntimeError("广告组里有多条新的暂停广告，无法判断用哪一条")
                text = describe_unreadable_ad(latest, group_id, seen | known, expected_status)
            reasons.append(f"第{attempt}次：{text}")
            if attempt < attempts:
                sleeper(1)
    detail = " ".join(reasons) if reasons else "广告组里没有新广告"
    raise RuntimeError(f"广告试了 {attempts} 次还没成功。{detail}")


class PortableCampaignRebuilder:
    """Read one campaign to local files, then recreate it in another browser session."""

    def __init__(
        self,
        source: AccountPage,
        target: AccountPage,
        target_runner: ProtocolRunner,
        target_manager_ocid: str,
        log: Callable[[str], None],
    ):
        self.source = source
        self.target = target
        self.target_runner = target_runner
        self.target_manager_ocid = str(target_manager_ocid or target.ocid)
        self.log = log

    def _raw_snapshot(self, campaign_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        campaign_rows = self.source.page.rpc(
            "/aw/_/rpc/CampaignService/List",
            _list_request(self.source.ocid, CAMPAIGN_SNAPSHOT_FIELDS, campaign_id=campaign_id),
        ).get("1") or []
        campaigns = [row for row in campaign_rows if str(row.get("2") or "") == campaign_id]
        if len(campaigns) != 1:
            raise RuntimeError("无法唯一读取来源广告系列完整快照")
        group_rows = self.source.page.rpc(
            "/aw/_/rpc/AdGroupService/List",
            _list_request(self.source.ocid, AD_GROUP_SNAPSHOT_FIELDS, campaign_id=campaign_id),
        ).get("1") or []
        ad_rows = self.source.page.rpc(
            "/aw/_/rpc/AdGroupAdService/List",
            _list_request(self.source.ocid, APP_AD_SNAPSHOT_FIELDS, campaign_id=campaign_id),
        ).get("1") or []
        groups = [row for row in group_rows if int(row.get("14") or 0) != 3]
        ads = [row for row in ad_rows if int(row.get("5") or 0) != 3]
        if not groups:
            raise RuntimeError("来源广告系列没有可重建的广告组")
        if not ads:
            raise RuntimeError("来源广告系列没有可重建的广告")
        return copy.deepcopy(campaigns[0]), copy.deepcopy(groups), copy.deepcopy(ads)

    @staticmethod
    def _image_records(ads: list[dict[str, Any]]) -> list[dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for ad in ads:
            for wrapper in ((ad.get("13") or {}).get("84") or {}).get("5") or []:
                asset = ((wrapper.get("1") or {}).get("1") or {})
                asset_id = str(asset.get("1") or "")
                metadata = wrapper.get("4") or {}
                preview = ""
                for candidate in metadata.get("6") or []:
                    if isinstance(candidate, dict) and candidate.get("2"):
                        preview = str(candidate["2"])
                        break
                if not preview:
                    preview = str((metadata.get("7") or {}).get("3") or "")
                if asset_id and preview:
                    result.setdefault(asset_id, {
                        "source_asset_id": asset_id,
                        "filename": str(asset.get("2") or f"asset-{asset_id}.png"),
                        "preview_url": preview,
                    })
        return list(result.values())

    def export(self, campaign_id: str) -> dict[str, Any]:
        campaign_id = str(campaign_id)
        self.log(f"[{format_cid(self.source.cid)}] 读取所选广告系列完整快照")
        campaign, groups, ads = self._raw_snapshot(campaign_id)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        folder = data_dir() / "copy-snapshots" / f"{self.source.cid}-{campaign_id}-{stamp}"
        assets_dir = folder / "assets"
        assets_dir.mkdir(parents=True, exist_ok=False)
        assets = self._image_records(ads)
        for index, asset in enumerate(assets, 1):
            self.log(f"本地快照：下载图片 {index}/{len(assets)} · {asset['filename']}")
            content, mime, final_url = download_preview_image(
                self.source.page, asset["preview_url"],
            )
            suffix = ".png" if mime == "image/png" else ".jpg"
            safe_name = f"{index:03d}-{asset['source_asset_id']}{suffix}"
            target = assets_dir / safe_name
            target.write_bytes(content)
            asset.update({
                "local_path": str(target), "mime_type": mime, "final_url": final_url,
                "sha256": hashlib.sha256(content).hexdigest(), "size": str(len(content)),
            })
        snapshot = {
            "schema_version": 1,
            "created_at": datetime.now().astimezone().isoformat(),
            "source": {
                "cid": self.source.cid, "ocid": self.source.ocid,
                "campaign_id": campaign_id,
            },
            "campaign": campaign, "ad_groups": groups, "ads": ads, "assets": assets,
        }
        snapshot_path = folder / "snapshot.json"
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        self.log(
            f"本地快照已保存：{snapshot_path} · {len(groups)} 个广告组 / "
            f"{len(ads)} 个广告 / {len(assets)} 张图片"
        )
        return {**snapshot, "snapshot_path": str(snapshot_path)}

    def _target_catalog(self) -> AdCatalog:
        campaigns = parse_campaigns(self.target.page.rpc(
            "/aw/_/rpc/CampaignService/List", campaign_list_request(self.target.ocid),
        ))
        groups = parse_ad_groups(self.target.page.rpc(
            "/aw/_/rpc/AdGroupService/List", ad_group_list_request(self.target.ocid),
        ))
        ads = parse_ads(self.target.page.rpc(
            "/aw/_/rpc/AdGroupAdService/List", ad_list_request(self.target.ocid),
        ))
        return AdCatalog(campaigns, groups, ads)

    def _unique_name(self, source_name: str, catalog: AdCatalog) -> str:
        names = {str(row.get("name") or "") for row in catalog.campaigns if row.get("status_raw") != 7}
        if source_name not in names:
            return source_name
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"{source_name}（复制 {stamp}）"

    @staticmethod
    def _poll_one(reader: Callable[[], list[dict[str, Any]]], predicate: Callable[[dict[str, Any]], bool], label: str) -> dict[str, Any]:
        for attempt in range(14):
            if attempt:
                time.sleep(0.8 + attempt * 0.2)
            matches = [row for row in reader() if predicate(row)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise RuntimeError(f"{label}写后出现多个匹配对象，停止自动判断")
        raise RuntimeError(f"{label}协议返回后未能回读新对象")

    def recreate(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        campaign = snapshot["campaign"]
        if int(campaign.get("15") or 0) != APP_CHANNEL:
            raise ValueError("本地快照重建目前只支持应用广告系列")
        before = self._target_catalog()
        name = self._unique_name(str(campaign.get("11") or "App copy"), before)
        self.log(f"[{format_cid(self.target.cid)}] 1/4 新建预算和暂停应用广告系列")
        response = self.target.page.rpc(
            "/aw_cm/editing/_/rpc/BatchService/Batch",
            portable_campaign_batch_request(
                ocid=self.target.ocid, name=name, source_campaign=campaign,
            ),
        )
        error = _service_error(response)
        if error:
            raise RuntimeError(f"新建广告系列失败：{error}")

        def campaigns() -> list[dict[str, Any]]:
            return parse_campaigns(self.target.page.rpc(
                "/aw/_/rpc/CampaignService/List", campaign_list_request(self.target.ocid),
            ))
        created_campaign = self._poll_one(
            campaigns,
            lambda row: row.get("name") == name and row.get("status_raw") == PAUSED_CAMPAIGN,
            "广告系列",
        )
        campaign_id = str(created_campaign["campaign_id"])

        source_groups = snapshot["ad_groups"]
        group_map: dict[str, str] = {}
        for index, source_group in enumerate(source_groups, 1):
            self.log(f"[{format_cid(self.target.cid)}] 2/4 新建暂停广告组 {index}/{len(source_groups)}")
            response = self.target.page.rpc(
                "/aw_essentials/_/rpc/AdGroupService/Mutate",
                portable_ad_group_request(
                    ocid=self.target.ocid, campaign_id=campaign_id,
                    temporary_id=f"-{index}", source_group=source_group,
                ),
            )
            error = _service_error(response)
            if error:
                raise RuntimeError(f"新建广告组失败：{error}")
            source_group_id = str(source_group.get("3") or "")
            source_name = str(source_group.get("11") or "Ad group")

            def groups() -> list[dict[str, Any]]:
                return parse_ad_groups(self.target.page.rpc(
                    "/aw/_/rpc/AdGroupService/List", ad_group_list_request(self.target.ocid),
                ))
            created_group = self._poll_one(
                groups,
                lambda row, n=source_name: (
                    row.get("campaign_id") == campaign_id and row.get("name") == n
                    and row.get("status_raw") == PAUSED_CHILD
                ),
                "广告组",
            )
            group_map[source_group_id] = str(created_group["ad_group_id"])

        asset_map: list[dict[str, str]] = []
        assets = snapshot.get("assets") or []
        if assets:
            self.log(f"[{format_cid(self.target.cid)}] 3/4 重新上传 {len(assets)} 张图片")
            upload_results = upload_images(
                self.target.page,
                [str(asset["local_path"]) for asset in assets],
                account_id=self.target.ocid,
                manager_id=self.target_manager_ocid,
            )
            if len(upload_results) != len(assets) or any(not result.ok for result in upload_results):
                failure = next((result.error for result in upload_results if not result.ok), "图片上传未完整返回")
                raise RuntimeError(f"目标账户图片重传失败：{failure}")
            for source_asset, uploaded in zip(assets, upload_results):
                asset_map.append({
                    "source_asset_id": str(source_asset["source_asset_id"]),
                    "target_asset_id": str(uploaded.asset_id),
                    "filename": str(source_asset.get("filename") or uploaded.filename),
                })

        source_ads = snapshot["ads"]
        created_ads: list[dict[str, Any]] = []
        for index, source_ad in enumerate(source_ads, 1):
            source_group_id = str(source_ad.get("3") or "")
            target_group_id = group_map.get(source_group_id)
            if not target_group_id:
                raise RuntimeError(f"广告 {source_ad.get('4')} 找不到目标广告组映射")
            source_image_ids = {
                str(((item.get("1") or {}).get("1") or ""))
                for item in ((((source_ad.get("13") or {}).get("100") or {}).get("44") or {}).get("4") or [])
            }
            mapped_assets = [
                item for item in asset_map if item["source_asset_id"] in source_image_ids
            ]
            self.log(f"[{format_cid(self.target.cid)}] 4/4 新建暂停广告 {index}/{len(source_ads)}")
            response = self.target.page.rpc(
                "/aw_cm/editing/_/rpc/BatchService/Batch",
                portable_app_ad_request(
                    ocid=self.target.ocid, campaign_id=campaign_id,
                    ad_group_id=target_group_id, temporary_id=f"-{index}",
                    source_ad=source_ad, target_assets=mapped_assets,
                ),
            )
            error = _service_error(response)
            if error:
                raise RuntimeError(f"新建广告失败：{error}")

            def ads() -> list[dict[str, Any]]:
                return parse_ads(self.target.page.rpc(
                    "/aw/_/rpc/AdGroupAdService/List",
                    ad_list_request(self.target.ocid, ad_group_id=target_group_id),
                ))
            existing_ids = {
                row["ad_id"] for row in before.ads if row.get("ad_group_id") == target_group_id
            }
            created_ad = self._poll_one(
                ads,
                lambda row, known=existing_ids: (
                    row.get("ad_group_id") == target_group_id
                    and row.get("ad_id") not in known
                    and row.get("status_raw") == PAUSED_CHILD
                ),
                "广告",
            )
            created_ads.append(created_ad)

        return {
            "mode": "portable_campaign_rebuild",
            "write_performed": True,
            "success": True,
            "snapshot_path": str(snapshot["snapshot_path"]),
            "created": created_campaign,
            "created_campaign": created_campaign,
            "created_ad_groups": group_map,
            "created_ads": [str(row.get("ad_id") or "") for row in created_ads],
            "asset_map": asset_map,
            "post_read_verified": True,
            "rebuild_strategy": "local_snapshot",
        }
