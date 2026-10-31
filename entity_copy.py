from __future__ import annotations

import time
from typing import Any, Callable

from .ads import (
    AdCatalog,
    ad_group_list_request,
    ad_list_request,
    build_batch_request,
    campaign_list_request,
    parse_ad_groups,
    parse_ads,
    parse_campaigns,
    public_ad,
)
from .browser_provider import ADSPOWER, normalize_browser_type
from .config import format_cid, normalize_cid
from .protocol import AccountPage, CAMPAIGN_STATUS_PAUSED, ProtocolRunner
from .protocol_catalog import CAMPAIGN_STATUS_MUTATE_PATH
from .portable_copy_workflow import PortableCampaignWorkflow


COPY_CAMPAIGN = "campaign"
COPY_AD_GROUP = "ad_group"
COPY_AD = "ad"
COPY_KINDS = {COPY_CAMPAIGN, COPY_AD_GROUP, COPY_AD}


def _selector(field: str, value: str) -> list[dict[str, Any]]:
    return [{"1": [{"1": str(field), "2": {"3": str(value)}}]}]


def entity_copy_request(
    *,
    kind: str,
    source_ocid: str,
    target_ocid: str,
    manager_ocid: str,
    source_id: str,
    target_parent_id: str = "",
) -> dict[str, Any]:
    """Build the Google Ads native copy/paste request for one hierarchy level."""
    if kind not in COPY_KINDS:
        raise ValueError(f"不支持的复制层级：{kind}")
    source_spec = {
        COPY_CAMPAIGN: ("Campaign", "campaign_id"),
        COPY_AD_GROUP: ("AdGroup", "ad_group_id"),
        COPY_AD: ("AdGroupAd", "ad.ad_id"),
    }[kind]
    target_spec = {
        COPY_CAMPAIGN: ("Customer", "customer_id", target_ocid),
        COPY_AD_GROUP: ("Campaign", "campaign_id", target_parent_id),
        COPY_AD: ("AdGroup", "ad_group_id", target_parent_id),
    }[kind]
    if kind != COPY_CAMPAIGN and not target_parent_id:
        raise ValueError("复制广告组或广告时必须指定目标父级")

    source_ocid = str(source_ocid)
    target_ocid = str(target_ocid)
    manager_ocid = str(manager_ocid)
    return {
        "1": {
            "1": {
                "3": source_spec[0],
                "4": {
                    "1": _selector(source_spec[1], source_id),
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
                "3": target_spec[0],
                "4": {
                    "1": _selector(target_spec[1], target_spec[2]),
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
            # Ask Google to keep newly pasted delivery objects paused.
            "11": True,
            "12": {},
        }
    }


def campaign_pause_request(ocid: str, campaign_id: str) -> dict[str, Any]:
    return {
        "1": {"3": {"2": str(ocid)}},
        "2": [{
            "1": 3,
            "2": ["status"],
            "3": {
                "1": str(ocid), "2": str(campaign_id),
                "12": CAMPAIGN_STATUS_PAUSED,
            },
        }],
    }


def ad_group_pause_request(ocid: str, campaign_id: str, ad_group_id: str) -> dict[str, Any]:
    return {
        "1": {"3": {"2": str(ocid)}},
        "2": [{
            "1": 3,
            "2": ["status"],
            "3": {
                "1": str(ocid), "2": str(campaign_id),
                "3": str(ad_group_id), "14": 2,
            },
        }],
    }


def ad_pause_request(
    ocid: str, campaign_id: str, ad_group_id: str, ad_id: str,
) -> dict[str, Any]:
    return {
        "1": {"2": {"1": False}, "3": {"2": str(ocid)}},
        "2": [{
            "1": 3,
            "2": ["status"],
            "3": {
                "1": str(ocid), "2": str(campaign_id),
                "3": str(ad_group_id), "4": str(ad_id), "5": 2,
            },
        }],
    }


class EntityCopyManager:
    """Copy entities between CIDs; full campaigns use a portable local snapshot."""

    def __init__(
        self,
        source_profile: str,
        source_cid: str,
        target_profile: str,
        target_cid: str,
        log: Callable[[str], None] | None = None,
        *,
        source_browser_type: str = ADSPOWER,
        target_browser_type: str = ADSPOWER,
    ):
        self.source_profile = str(source_profile)
        self.target_profile = str(target_profile)
        self.source_browser_type = normalize_browser_type(source_browser_type)
        self.target_browser_type = normalize_browser_type(target_browser_type)
        self.source_cid = normalize_cid(source_cid)
        self.target_cid = normalize_cid(target_cid)
        self.log = log or (lambda _text: None)
        self.source_runner = ProtocolRunner(
            self.source_profile, self.log, browser_type=self.source_browser_type,
        )
        self.target_runner = (
            self.source_runner
            if (
                self.source_profile == self.target_profile
                and self.source_browser_type == self.target_browser_type
            )
            else ProtocolRunner(
                self.target_profile, self.log, browser_type=self.target_browser_type,
            )
        )
        try:
            if self.target_runner is self.source_runner:
                # One MCC directory response can contain many sibling CIDs, but
                # only enrich/read the two explicitly selected accounts.
                self.source_accounts = self.source_runner.discover_accounts({
                    self.source_cid, self.target_cid,
                })
                self.target_accounts = self.source_accounts
            else:
                self.source_accounts = self.source_runner.discover_accounts({
                    self.source_cid,
                })
                self.target_accounts = self.target_runner.discover_accounts({
                    self.target_cid,
                })
            self.source = self._account(self.source_accounts, self.source_cid, "来源")
            self.target = self._account(self.target_accounts, self.target_cid, "目标")
            self.native_runner: ProtocolRunner | None = None
            self.native_accounts: dict[str, AccountPage] = {}
            if self.source_cid in self.source_accounts and self.target_cid in self.source_accounts:
                self.native_runner = self.source_runner
                self.native_accounts = self.source_accounts
            elif self.source_cid in self.target_accounts and self.target_cid in self.target_accounts:
                self.native_runner = self.target_runner
                self.native_accounts = self.target_accounts
        except Exception:
            self.close()
            raise

    @staticmethod
    def _account(accounts: dict[str, AccountPage], cid: str, label: str) -> AccountPage:
        account = accounts.get(cid)
        if not account:
            raise RuntimeError(f"{label} CID {format_cid(cid)} 不在所选窗口的可访问范围内")
        if account.is_manager:
            raise ValueError(f"{label}不能选择 MCC，请选择普通 CID")
        return account

    def close(self) -> None:
        if getattr(self, "target_runner", None) is not getattr(self, "source_runner", None):
            self.target_runner.close()
        if getattr(self, "source_runner", None):
            self.source_runner.close()

    def __enter__(self) -> "EntityCopyManager":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @staticmethod
    def _catalog(account: AccountPage) -> AdCatalog:
        campaigns = parse_campaigns(account.page.rpc(
            "/aw/_/rpc/CampaignService/List", campaign_list_request(account.ocid)
        ))
        groups = parse_ad_groups(account.page.rpc(
            "/aw/_/rpc/AdGroupService/List", ad_group_list_request(account.ocid)
        ))
        ads = parse_ads(account.page.rpc(
            "/aw/_/rpc/AdGroupAdService/List", ad_list_request(account.ocid)
        ))
        return AdCatalog(campaigns, groups, ads)

    @staticmethod
    def _catalog_level(account: AccountPage, kind: str, target_parent_id: str) -> AdCatalog:
        """Read only the copied level while polling, avoiding three full table reads."""
        if kind == COPY_CAMPAIGN:
            campaigns = parse_campaigns(account.page.rpc(
                "/aw/_/rpc/CampaignService/List", campaign_list_request(account.ocid)
            ))
            return AdCatalog(campaigns, [], [])
        if kind == COPY_AD_GROUP:
            groups = parse_ad_groups(account.page.rpc(
                "/aw/_/rpc/AdGroupService/List", ad_group_list_request(account.ocid)
            ))
            return AdCatalog([], groups, [])
        ads = parse_ads(account.page.rpc(
            "/aw/_/rpc/AdGroupAdService/List",
            ad_list_request(account.ocid, ad_group_id=target_parent_id),
        ))
        return AdCatalog([], [], ads)

    def catalogs(self) -> dict[str, AdCatalog]:
        source = self._catalog(self.source)
        target = source if self.source is self.target else self._catalog(self.target)
        self.log(
            f"复制来源 {format_cid(self.source_cid)} → 目标 {format_cid(self.target_cid)}："
            "层级结构读取完成（只读）"
        )
        return {"source": source, "target": target}

    @staticmethod
    def _find(rows: list[dict[str, Any]], key: str, value: str, label: str) -> dict[str, Any]:
        matches = [row for row in rows if str(row.get(key)) == str(value)]
        if len(matches) != 1:
            raise ValueError(f"未能唯一定位{label} {value}")
        return matches[0]

    def _native_copy_accounts(self) -> tuple[AccountPage, AccountPage]:
        source = self.native_accounts.get(self.source_cid)
        target = self.native_accounts.get(self.target_cid)
        if source and target:
            return source, target
        raise PermissionError(
            "跨窗口原生复制不可执行：没有一个 Google 登录会话同时拥有来源 CID "
            f"{format_cid(self.source_cid)} 和目标 CID {format_cid(self.target_cid)} 的权限。"
            "Google CampaignCopyService 不能合并两个 AdsPower 窗口的 Cookie。"
            "请在任一窗口登录/授权一个能同时访问两边账户的 Google/MCC 账号后重新读取结构；"
            "当前两个独立登录只能分别读取，不能直接执行原生跨户复制。"
        )

    def _manager_ocid(self, source: AccountPage) -> str:
        for parent_cid in source.parent_mcc_cids:
            parent = self.native_accounts.get(parent_cid)
            if parent and parent.is_manager:
                return parent.ocid
        return source.ocid

    @staticmethod
    def _manager_ocid_in(account: AccountPage, accounts: dict[str, AccountPage]) -> str:
        for parent_cid in account.parent_mcc_cids:
            parent = accounts.get(parent_cid)
            if parent and parent.is_manager:
                return parent.ocid
        return account.ocid

    def preflight(
        self,
        source_catalog: AdCatalog,
        target_catalog: AdCatalog,
        *,
        kind: str,
        source_campaign_id: str,
        source_ad_group_id: str = "",
        source_ad_id: str = "",
        target_campaign_id: str = "",
        target_ad_group_id: str = "",
    ) -> dict[str, Any]:
        if kind not in COPY_KINDS:
            raise ValueError("请选择复制层级")
        source_campaign = self._find(
            source_catalog.campaigns, "campaign_id", source_campaign_id, "来源广告系列"
        )
        if source_campaign.get("status_raw") == 7:
            raise ValueError("来源广告系列已移除")
        source_id = source_campaign_id
        source_object: dict[str, Any] = source_campaign
        target_parent_id = ""
        target_parent: dict[str, Any] = {
            "cid": self.target_cid, "cid_display": format_cid(self.target_cid)
        }

        if kind in {COPY_AD_GROUP, COPY_AD}:
            source_group = self._find(
                source_catalog.ad_groups, "ad_group_id", source_ad_group_id, "来源广告组"
            )
            if source_group.get("campaign_id") != source_campaign_id:
                raise ValueError("来源广告组不属于所选来源广告系列")
            if source_group.get("status_raw") == 3:
                raise ValueError("来源广告组已移除")
            target_campaign = self._find(
                target_catalog.campaigns, "campaign_id", target_campaign_id, "目标广告系列"
            )
            if target_campaign.get("status_raw") == 7:
                raise ValueError("目标广告系列已移除")
            if target_campaign.get("channel_raw") != source_campaign.get("channel_raw"):
                raise ValueError("来源与目标广告系列类型不一致")
            source_id = source_ad_group_id
            source_object = source_group
            target_parent_id = target_campaign_id
            target_parent = target_campaign

        if kind == COPY_AD:
            source_ad = self._find(source_catalog.ads, "ad_id", source_ad_id, "来源广告")
            if source_ad.get("ad_group_id") != source_ad_group_id:
                raise ValueError("来源广告不属于所选来源广告组")
            if source_ad.get("status_raw") == 3:
                raise ValueError("来源广告已移除")
            target_group = self._find(
                target_catalog.ad_groups, "ad_group_id", target_ad_group_id, "目标广告组"
            )
            if target_group.get("campaign_id") != target_campaign_id:
                raise ValueError("目标广告组不属于所选目标广告系列")
            if target_group.get("status_raw") == 3:
                raise ValueError("目标广告组已移除")
            source_id = source_ad_id
            source_object = public_ad(source_ad)
            target_parent_id = target_ad_group_id
            target_parent = target_group

        native_source = self.native_accounts.get(self.source_cid)
        native_target = self.native_accounts.get(self.target_cid)
        # CampaignCopyService is intentionally not a dependency of full
        # cross-CID campaign copying.  Even with a shared login session, first
        # materialize a portable snapshot and then perform explicit paused
        # creates in the target account.  Granular ad-group/ad copy retains the
        # existing native path because those operations are not full snapshots.
        native_copy = bool(native_source and native_target and kind != COPY_CAMPAIGN)
        if not native_copy and kind != COPY_CAMPAIGN:
            raise PermissionError(
                "两个独立 AdsPower 登录之间，目前本地快照重建支持完整广告系列；"
                "单独复制广告组或广告时，请选择同一权限会话，或直接复制其所属广告系列。"
            )
        request = None
        if native_copy:
            request = entity_copy_request(
                kind=kind,
                source_ocid=native_source.ocid,
                target_ocid=native_target.ocid,
                manager_ocid=self._manager_ocid(native_source),
                source_id=source_id,
                target_parent_id=target_parent_id,
            )
        return {
            "mode": "entity_copy_preflight",
            "write_performed": False,
            "kind": kind,
            "source_profile": self.source_profile,
            "source_cid": self.source_cid,
            "target_profile": self.target_profile,
            "target_cid": self.target_cid,
            "source": source_object,
            "target_parent": target_parent,
            "protocol": {
                "path": (
                    "/aw_essentials/_/rpc/CampaignCopyService/ExecuteAsyncCopy"
                    if native_copy else "local://copy-snapshots/recreate"
                ),
                "native_copy": native_copy,
                "rebuild_strategy": "native_copy" if native_copy else "local_snapshot",
                "pause_after_copy": True,
                "source_entity": request["1"]["1"]["3"] if request else "CampaignSnapshot",
                "target_entity": request["1"]["2"]["3"] if request else "CampaignRecreate",
                "shared_permission_session": (
                    self.native_runner.profile_id
                    if native_copy and self.native_runner else ""
                ),
                "snapshot_contains": (
                    ["campaign", "budget", "ad_groups", "ads", "text_assets", "image_files"]
                    if not native_copy else []
                ),
            },
            "_request": request,
            "_source_id": source_id,
            "_target_parent_id": target_parent_id,
        }

    @staticmethod
    def _ids(catalog: AdCatalog, kind: str, target_parent_id: str) -> set[str]:
        if kind == COPY_CAMPAIGN:
            return {row["campaign_id"] for row in catalog.campaigns}
        if kind == COPY_AD_GROUP:
            return {
                row["ad_group_id"] for row in catalog.ad_groups
                if row.get("campaign_id") == target_parent_id
            }
        return {
            row["ad_id"] for row in catalog.ads
            if row.get("ad_group_id") == target_parent_id
        }

    @staticmethod
    def _new_rows(catalog: AdCatalog, kind: str, target_parent_id: str, before: set[str]) -> list[dict[str, Any]]:
        if kind == COPY_CAMPAIGN:
            return [
                row for row in catalog.campaigns
                if row["campaign_id"] not in before and row.get("status_raw") != 7
            ]
        if kind == COPY_AD_GROUP:
            return [
                row for row in catalog.ad_groups
                if row["ad_group_id"] not in before
                and row.get("campaign_id") == target_parent_id
                and row.get("status_raw") != 3
            ]
        return [
            row for row in catalog.ads
            if row["ad_id"] not in before
            and row.get("ad_group_id") == target_parent_id
            and row.get("status_raw") != 3
        ]

    def _pause(self, kind: str, item: dict[str, Any]) -> None:
        if kind == COPY_CAMPAIGN:
            self.target.page.rpc(
                CAMPAIGN_STATUS_MUTATE_PATH,
                campaign_pause_request(self.target.ocid, item["campaign_id"]),
            )
        elif kind == COPY_AD_GROUP:
            self.target.page.rpc(
                "/aw_essentials/_/rpc/AdGroupService/Mutate",
                ad_group_pause_request(
                    self.target.ocid, item["campaign_id"], item["ad_group_id"]
                ),
            )
        else:
            self.target.page.rpc(
                "/aw_cm/editing/_/rpc/BatchService/Batch",
                build_batch_request(ad_pause_request(
                    self.target.ocid, item["campaign_id"],
                    item["ad_group_id"], item["ad_id"],
                )),
            )

    @staticmethod
    def _is_paused(kind: str, item: dict[str, Any]) -> bool:
        return item.get("status_raw") == (5 if kind == COPY_CAMPAIGN else 2)

    def copy(
        self,
        source_catalog: AdCatalog,
        target_catalog: AdCatalog,
        **parameters: Any,
    ) -> dict[str, Any]:
        if self.source.status != "可用":
            raise PermissionError(f"来源 CID 状态为“{self.source.status}”")
        if self.target.status != "可用":
            raise PermissionError(f"目标 CID 状态为“{self.target.status}”")
        plan = self.preflight(source_catalog, target_catalog, **parameters)
        balance = self.target_runner.require_positive_account_balance(
            self.target, action="复制创建广告对象",
        )
        kind = str(plan["kind"])
        if not bool((plan.get("protocol") or {}).get("native_copy")):
            self.log(
                f"{format_cid(self.source_cid)} → {format_cid(self.target_cid)}："
                "使用本地快照重建，不要求同一 Google 登录同时拥有两边权限"
            )
            rebuilder = PortableCampaignWorkflow(
                self.source, self.target, self.target_runner,
                self._manager_ocid_in(self.target, self.target_accounts), self.log,
            )
            snapshot = rebuilder.export(str(plan["_source_id"]))
            result = rebuilder.recreate(snapshot)
            clean_plan = {key: value for key, value in plan.items() if not key.startswith("_")}
            return {**clean_plan, **result, "target_balance": balance}

        native_source, _native_target = self._native_copy_accounts()
        target_parent_id = str(plan["_target_parent_id"])
        fresh_before = self._catalog_level(self.target, kind, target_parent_id)
        before_ids = self._ids(fresh_before, kind, target_parent_id)
        self.log(
            f"{format_cid(self.source_cid)} → {format_cid(self.target_cid)}："
            f"发送原生 {kind} 复制协议"
        )
        response = native_source.page.rpc(
            "/aw_essentials/_/rpc/CampaignCopyService/ExecuteAsyncCopy",
            plan["_request"],
        )
        created: dict[str, Any] | None = None
        for attempt in range(14):
            time.sleep(1.0 + attempt * 0.25)
            current = self._catalog_level(self.target, kind, target_parent_id)
            candidates = self._new_rows(current, kind, target_parent_id, before_ids)
            if len(candidates) == 1:
                created = candidates[0]
                break
            if len(candidates) > 1:
                raise RuntimeError("复制后出现多个新对象，无法唯一回读校验")
        if not created:
            raise RuntimeError(
                "复制协议已返回，但目标 CID 未出现新对象。请确认当前 Google 登录同时具有来源和目标权限。"
            )
        if not self._is_paused(kind, created):
            self.log("新副本不是暂停状态，立即发送暂停协议")
            self._pause(kind, created)
        verified: dict[str, Any] | None = None
        for attempt in range(7):
            if attempt:
                time.sleep(0.8 * attempt)
            current = self._catalog_level(self.target, kind, target_parent_id)
            object_id = str(created.get({
                COPY_CAMPAIGN: "campaign_id", COPY_AD_GROUP: "ad_group_id", COPY_AD: "ad_id",
            }[kind]))
            rows = self._new_rows(current, kind, target_parent_id, before_ids)
            id_key = {
                COPY_CAMPAIGN: "campaign_id", COPY_AD_GROUP: "ad_group_id", COPY_AD: "ad_id",
            }[kind]
            verified = next((row for row in rows if str(row.get(id_key)) == object_id), None)
            if verified and self._is_paused(kind, verified):
                break
        if not verified or not self._is_paused(kind, verified):
            raise RuntimeError("复制后未能回读暂停状态，禁止把本次任务标记为成功")
        clean_plan = {key: value for key, value in plan.items() if not key.startswith("_")}
        return {
            **clean_plan,
            "mode": "entity_copy",
            "write_performed": True,
            "success": True,
            "created": public_ad(verified) if kind == COPY_AD else dict(verified),
            "post_read_verified": True,
            "response_received": isinstance(response, dict),
            "target_balance": balance,
        }
