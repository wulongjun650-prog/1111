"""Decoupled snapshot export/import for cross-account campaign copy.

Split out of `portable_copy_workflow.PortableCampaignWorkflow` so a snapshot
can be exported once (to any directory, e.g. the desktop) and later imported
into any number of target accounts, at any later time, optionally swapping in
a local asset directory in place of the snapshot's own downloaded images.

`SnapshotExporter` only ever touches a source account; `SnapshotImporter` only
ever touches a target account. `PortableCampaignWorkflow` itself is left
unmodified so it keeps serving `entity_copy.py`'s synchronous single-target
flow; this module reuses its schema-level pure functions (validation, identity
hashing, idempotency keys) rather than duplicating them.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .account_level_copy import (
    apply_account_settings,
    apply_campaign_goals,
    ensure_account_level_state,
    read_account_level,
    summarize_account_level,
)
from .ads import (
    AdCatalog,
    ad_group_list_request,
    ad_list_request,
    campaign_list_request,
    parse_ad_groups,
    parse_ads,
    parse_campaigns,
)
from .config import data_dir, format_cid
from .image_upload import (
    download_first_preview,
    manager_ocid_from_page,
    missing_image_message,
    resolve_image_preview_urls,
    upload_images,
    validate_image,
)
from .portable_copy import (
    AD_GROUP_CRITERION_MUTATE_PATH,
    AD_GROUP_SNAPSHOT_FIELDS,
    APP_AD_SNAPSHOT_FIELDS,
    APP_CHANNEL,
    AUDIENCE_LIST_FIELD_CUSTOM_AUDIENCE_ID,
    AUDIENCE_LIST_PATH,
    CAMPAIGN_SNAPSHOT_FIELDS,
    CAMPAIGN_SHARED_SET_LIST_PATH,
    CAMPAIGN_SHARED_SET_MUTATE_PATH,
    CAMPAIGN_TARGET_STATUS,
    CHILD_TARGET_STATUS,
    CUSTOM_AUDIENCE_LIST_PATH,
    DISPLAY_AD_SNAPSHOT_FIELDS,
    DISPLAY_CHANNEL,
    PAUSED_CAMPAIGN,
    PAUSED_CHILD,
    SHARED_CRITERION_LIST_PATH,
    SHARED_CRITERION_MUTATE_PATH,
    SHARED_SET_BATCH_PATH,
    SHARED_SET_LIST_PATH,
    SHARED_SET_TYPE_NEGATIVE_KEYWORDS,
    SHARED_SET_TYPE_PLACEMENT_EXCLUSIONS,
    SUPPORTED_CHANNELS,
    _list_request,
    _service_error,
    ad_group_audience_list_request,
    ad_group_audience_mutate_request,
    append_shared_criteria_request,
    campaign_shared_set_list_request,
    campaign_shared_set_mutate_request,
    create_shared_set_and_criteria_batch_request,
    portable_ad_group_request,
    portable_app_ad_request,
    portable_campaign_batch_request,
    portable_display_ad_request,
    shared_criterion_list_request,
    shared_set_list_by_id_request,
    shared_set_list_by_name_request,
    target_custom_audience_list_request,
)
from .portable_copy_workflow import (
    SCHEMA_NOTES,
    SNAPSHOT_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    _asset_id,
    _idempotency_key,
    asset_reference_manifest,
    referenced_image_filenames,
    snapshot_identity,
    validate_snapshot,
    validate_snapshot_files,
)
from .protocol import AccountPage, ProtocolRunner
from .targeting_copy import (
    HumanActionRequired,
    apply_criteria,
    budget_gate_message,
    human_gate_for_error,
    read_targeting,
    select_existing_budget,
    summarize_targeting,
)
from .warmup import campaign_detail_request, parse_campaign_details


def compose_campaign_name(label: str) -> str:
    """`{label}-{month}-{day}` per target account, e.g. "A1-8-19"."""
    label = str(label).strip()
    if not label:
        raise ValueError("请为每个目标账户输入命名标签")
    now = datetime.now()
    return f"{label}-{now.month}-{now.day}"


class SnapshotExporter:
    """Reads one campaign from `source` and writes a self-contained snapshot.

    Never references any target account; the returned snapshot can be moved
    to any machine and imported at any later time.
    """

    def __init__(self, source: AccountPage, log: Callable[[str], None]):
        self.source = source
        self.log = log

    def _raw_snapshot(
        self, campaign_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        campaign_rows = self.source.page.rpc(
            "/aw/_/rpc/CampaignService/List",
            _list_request(self.source.ocid, CAMPAIGN_SNAPSHOT_FIELDS, campaign_id=campaign_id),
        ).get("1") or []
        campaigns = [row for row in campaign_rows if str(row.get("2") or "") == campaign_id]
        if len(campaigns) != 1:
            raise RuntimeError("无法唯一读取来源广告系列完整快照")
        channel = int(campaigns[0].get("15") or 0)
        if channel not in SUPPORTED_CHANNELS:
            raise RuntimeError(f"本地快照重建暂不支持该广告系列类型（channel={channel}）")
        ad_fields = APP_AD_SNAPSHOT_FIELDS if channel == APP_CHANNEL else DISPLAY_AD_SNAPSHOT_FIELDS
        group_rows = self.source.page.rpc(
            "/aw/_/rpc/AdGroupService/List",
            _list_request(self.source.ocid, AD_GROUP_SNAPSHOT_FIELDS, campaign_id=campaign_id),
        ).get("1") or []
        ad_rows = self.source.page.rpc(
            "/aw/_/rpc/AdGroupAdService/List",
            _list_request(self.source.ocid, ad_fields, campaign_id=campaign_id),
        ).get("1") or []
        groups = [row for row in group_rows if int(row.get("14") or 0) != 3]
        ads = [row for row in ad_rows if int(row.get("5") or 0) != 3]
        if not groups or not ads:
            raise RuntimeError("来源广告系列必须包含可重建的广告组和广告")
        return campaigns[0], groups, ads

    def _shared_sets_snapshot(
        self, campaign_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """读取来源广告系列已关联的共享集合，按子类型（否定关键词/展示位置排除）分流返回。

        两份返回列表的条目结构相同（name/type/values），只是目的地不同，对应快照顶层
        两个独立字段。SharedSetService 行字段号（2=shared_set_id、3=name、5=type）
        依据 placement_exclusion_shared_set_2026-08-20.md"关键差异"一节的删除请求/
        响应证据，非猜测；campaign_shared_set_list_request 本身的 List 端点未经抓包
        直接验证（plan 文件"关键风险"第2点），响应行字段号沿用 CampaignSharedSetService
        .Mutate 创建响应里观察到的同一套编号做外推。
        """
        ocid = self.source.ocid
        response = self.source.page.rpc(
            CAMPAIGN_SHARED_SET_LIST_PATH,
            campaign_shared_set_list_request(ocid, campaign_id),
        )
        rows = response.get("1") or []
        if isinstance(rows, dict):
            rows = [rows]
        shared_set_ids = sorted({str(row.get("2")) for row in rows if row.get("2")})
        negative_keywords: list[dict[str, Any]] = []
        shared_sets: list[dict[str, Any]] = []
        for shared_set_id in shared_set_ids:
            detail_response = self.source.page.rpc(
                SHARED_SET_LIST_PATH, shared_set_list_by_id_request(ocid, shared_set_id),
            )
            detail_rows = detail_response.get("1") or []
            if isinstance(detail_rows, dict):
                detail_rows = [detail_rows]
            detail = next(
                (row for row in detail_rows if str(row.get("2")) == shared_set_id), None,
            )
            if not detail:
                continue
            set_kind = int(detail.get("5") or 0)
            if set_kind not in (SHARED_SET_TYPE_NEGATIVE_KEYWORDS, SHARED_SET_TYPE_PLACEMENT_EXCLUSIONS):
                continue
            name = str(detail.get("3") or "")
            criterion_response = self.source.page.rpc(
                SHARED_CRITERION_LIST_PATH,
                shared_criterion_list_request(ocid, shared_set_id, set_kind),
            )
            criterion_rows = criterion_response.get("1") or []
            if isinstance(criterion_rows, dict):
                criterion_rows = [criterion_rows]
            value_field = "101" if set_kind == SHARED_SET_TYPE_NEGATIVE_KEYWORDS else "102"
            values = [
                str((row.get(value_field) or {}).get("1"))
                for row in criterion_rows
                if (row.get(value_field) or {}).get("1")
            ]
            entry = {
                "source_shared_set_id": shared_set_id, "name": name,
                "type": set_kind, "values": values,
            }
            if set_kind == SHARED_SET_TYPE_NEGATIVE_KEYWORDS:
                negative_keywords.append(entry)
            else:
                shared_sets.append(entry)
        return negative_keywords, shared_sets

    def _audiences_snapshot(
        self, campaign_id: str, groups: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """读取 Display 广告组当前关联的受众定向条件。

        包装 AudienceService.List（见 custom_audience_association_2026-08-20.md），
        响应体外层 "1" 字段在只有一条关联时是单对象而非数组（该文档"待补充"第5点），
        此处统一归一化为列表处理。CustomAudience 名称随后在本地按ID匹配解析
        （target_custom_audience_list_request 响应字段号：1=custom_audience_id、
        3=name，与 protocol.py 中 SharedLibraryManager._existing_audiences/
        _ensure_audience 已确认使用的同一套字段编号一致）。
        """
        ocid = self.source.ocid
        results: list[dict[str, Any]] = []
        pending_names: set[str] = set()
        for group in groups:
            ad_group_id = str(group.get("3") or "")
            if not ad_group_id:
                continue
            response = self.source.page.rpc(
                AUDIENCE_LIST_PATH,
                ad_group_audience_list_request(ocid, campaign_id, ad_group_id),
            )
            raw = response.get("1")
            rows = [] if raw is None else raw if isinstance(raw, list) else [raw]
            for row in rows:
                criterion_id = str(row.get("3") or "")
                custom_audience_id = row.get(AUDIENCE_LIST_FIELD_CUSTOM_AUDIENCE_ID)
                if custom_audience_id:
                    pending_names.add(str(custom_audience_id))
                    results.append({
                        "source_ad_group_id": ad_group_id,
                        "source_criterion_id": criterion_id,
                        "kind": "custom_audience",
                        "audience_id": str(custom_audience_id),
                        "name": "",
                    })
                elif criterion_id:
                    results.append({
                        "source_ad_group_id": ad_group_id,
                        "source_criterion_id": criterion_id,
                        "kind": "user_interest",
                        "audience_id": criterion_id,
                        "name": "",
                    })
        if pending_names:
            catalog_response = self.source.page.rpc(
                CUSTOM_AUDIENCE_LIST_PATH, target_custom_audience_list_request(ocid),
            )
            names = {
                str(row.get("1")): str(row.get("3") or "")
                for row in catalog_response.get("1") or []
                if str(row.get("1")) in pending_names
            }
            for entry in results:
                if entry["kind"] == "custom_audience":
                    entry["name"] = names.get(entry["audience_id"], "")
        return results

    def _manager_ocid(self) -> str:
        return manager_ocid_from_page(self.source.page, self.source.ocid)

    def _image_records(self, ads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Resolve every referenced image to one or more preview URLs.

        An exact AssetFolder hit is the normal path. Images that come back
        twice, under the child account rather than the manager, or only as a
        URL already stored on the ad are still kept. Callers reject any
        referenced image that still has no URL.
        """
        filenames = referenced_image_filenames(ads)
        if not filenames:
            return []
        previews = resolve_image_preview_urls(
            self.source.page, self.source.ocid, self._manager_ocid(),
            filenames, ads, log=self.log,
        )
        records: list[dict[str, Any]] = []
        for asset_id, filename in filenames.items():
            urls = previews.get(asset_id) or []
            if not urls:
                continue
            records.append({
                "source_asset_id": asset_id,
                "filename": filename,
                "preview_url": urls[0],
                "preview_urls": urls,
            })
        return records

    def export(self, campaign_id: str, export_dir: Path | None = None) -> dict[str, Any]:
        campaign_id = str(campaign_id)
        self.log(f"[{format_cid(self.source.cid)}] 读取广告系列、广告组、广告及素材引用")
        campaign, groups, ads = self._raw_snapshot(campaign_id)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        base = Path(export_dir).expanduser() if export_dir else data_dir() / "copy-snapshots"
        folder = base / f"{self.source.cid}-{campaign_id}-{stamp}"
        assets_dir = folder / "assets"
        assets_dir.mkdir(parents=True, exist_ok=False)
        assets = self._image_records(ads)
        manifest = asset_reference_manifest(ads)
        referenced_images = {
            ref["source_asset_id"]
            for ad_manifest in manifest
            for ref in ad_manifest["references"]
            if ref["kind"].endswith("image")
        }
        downloadable_images = {item["source_asset_id"] for item in assets}
        missing = sorted(referenced_images - downloadable_images)
        if missing:
            raise RuntimeError(missing_image_message(
                missing, "素材库没有这些图片的预览地址，广告数据里也没有",
            ))
        for index, asset in enumerate(assets, 1):
            content, mime, final_url = download_first_preview(
                self.source.page, asset,
            )
            asset.pop("preview_urls", None)
            suffix = ".png" if mime == "image/png" else ".jpg"
            relative_path = Path("assets") / f"{index:03d}-{asset['source_asset_id']}{suffix}"
            target = folder / relative_path
            target.write_bytes(content)
            asset.update({
                "relative_path": relative_path.as_posix(),
                "mime_type": mime,
                "final_url": final_url,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": str(len(content)),
            })
        channel = int(campaign.get("15") or 0)
        self.log(f"[{format_cid(self.source.cid)}] 读取否定关键词/展示位置排除共享集合关联")
        negative_keywords, shared_sets = self._shared_sets_snapshot(campaign_id)
        custom_audiences: list[dict[str, Any]] = []
        if channel == DISPLAY_CHANNEL:
            self.log(f"[{format_cid(self.source.cid)}] 读取受众定向条件关联")
            custom_audiences = self._audiences_snapshot(campaign_id, groups)
        self.log(f"[{format_cid(self.source.cid)}] 读取主题、地区、语言、设备、投放时段")
        group_ids = [str(group.get("3") or "") for group in groups if group.get("3")]
        targeting = read_targeting(self.source.page, self.source.ocid, campaign_id, group_ids)
        self.log(
            f"[{format_cid(self.source.cid)}] 定向："
            f"{summarize_targeting(targeting)}"
        )
        self.log(f"[{format_cid(self.source.cid)}] 读取账号内容适宜性和联系人转化目标")
        account_level = read_account_level(self.source.page, self.source.ocid, campaign_id)
        self.log(
            f"[{format_cid(self.source.cid)}] 账号："
            f"{summarize_account_level({'account_level': account_level})}"
        )
        snapshot: dict[str, Any] = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "created_at": datetime.now().astimezone().isoformat(),
            "source": {"cid": self.source.cid, "campaign_id": campaign_id},
            "campaign": campaign,
            "ad_groups": groups,
            "ads": ads,
            "assets": assets,
            "asset_references": manifest,
            "negative_keywords": negative_keywords,
            "shared_sets": shared_sets,
            "custom_audiences": custom_audiences,
            "campaign_criteria": targeting["campaign_criteria"],
            "ad_group_criteria": targeting["ad_group_criteria"],
            "targeting_gaps": targeting["targeting_gaps"],
            "content_suitability": None,
            "account_level": account_level,
            "schema_notes": dict(SCHEMA_NOTES),
        }
        snapshot["snapshot_id"] = snapshot_identity(snapshot)
        snapshot_path = folder / "snapshot.json"
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        self.log(
            f"本地可移植快照已保存：{snapshot_path} · {len(groups)} 个广告组 / "
            f"{len(ads)} 个广告 / {len(assets)} 个图片文件"
        )
        return {**snapshot, "snapshot_path": str(snapshot_path)}


class SnapshotImporter:
    """Recreates a previously-exported snapshot inside `target`.

    Never references any source account — the snapshot dict/file is the only
    input, so import can happen on a different machine, at a different time,
    against any number of independently-configured target accounts.
    """

    def __init__(
        self,
        target: AccountPage,
        target_runner: ProtocolRunner,
        target_manager_ocid: str,
        log: Callable[[str], None],
    ):
        self.target = target
        self.target_runner = target_runner
        self.target_manager_ocid = str(target_manager_ocid or target.ocid)
        self.log = log

    def _target_catalog(self) -> AdCatalog:
        return AdCatalog(
            parse_campaigns(self.target.page.rpc(
                "/aw/_/rpc/CampaignService/List", campaign_list_request(self.target.ocid),
            )),
            parse_ad_groups(self.target.page.rpc(
                "/aw/_/rpc/AdGroupService/List", ad_group_list_request(self.target.ocid),
            )),
            parse_ads(self.target.page.rpc(
                "/aw/_/rpc/AdGroupAdService/List", ad_list_request(self.target.ocid),
            )),
        )

    def _target_shared_sets(self, set_kind: int, name: str) -> list[dict[str, Any]]:
        response = self.target.page.rpc(
            SHARED_SET_LIST_PATH,
            shared_set_list_by_name_request(self.target.ocid, set_kind, name),
        )
        rows = response.get("1") or []
        if isinstance(rows, dict):
            rows = [rows]
        return [
            {"shared_set_id": str(row.get("2")), "name": str(row.get("3") or "")}
            for row in rows
        ]

    @staticmethod
    def _poll_one(
        reader: Callable[[], list[dict[str, Any]]],
        predicate: Callable[[dict[str, Any]], bool],
        label: str,
    ) -> dict[str, Any]:
        for attempt in range(14):
            if attempt:
                time.sleep(0.8 + attempt * 0.2)
            matches = [row for row in reader() if predicate(row)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise RuntimeError(f"{label}写后出现多个匹配对象，停止自动判断")
        raise RuntimeError(f"{label}协议返回后未能回读新对象")

    def _state_path(self, snapshot: dict[str, Any], idempotency_key: str) -> Path:
        snapshot_path = Path(str(snapshot.get("snapshot_path") or ""))
        folder = snapshot_path.parent if snapshot_path.name else data_dir() / "copy-snapshots"
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return folder / f"rebuild-{self.target.cid}-{key_hash[:20]}.json"

    @staticmethod
    def _write_state(path: Path, state: dict[str, Any]) -> None:
        state["updated_at"] = datetime.now().astimezone().isoformat()
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _load_state(
        self, snapshot: dict[str, Any], idempotency_key: str,
        *, campaign_status: int = PAUSED_CAMPAIGN, child_status: int = PAUSED_CHILD,
    ) -> tuple[Path, dict[str, Any]]:
        path = self._state_path(snapshot, idempotency_key)
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            if (
                str(state.get("snapshot_id")) != str(snapshot["snapshot_id"])
                or str(state.get("target_cid")) != str(self.target.cid)
                or str(state.get("idempotency_key")) != idempotency_key
            ):
                raise RuntimeError("幂等状态与当前快照或目标 CID 不匹配")
            state.setdefault("criterion_applied", [])
            state.setdefault("targeting_warnings", [])
            state.setdefault("shared_set_map", {})
            state.setdefault("shared_sets_associated_ids", [])
            state.setdefault("audience_sync", {})
            ensure_account_level_state(state)
            return path, state
        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "snapshot_id": snapshot["snapshot_id"],
            "target_cid": self.target.cid,
            "idempotency_key": idempotency_key,
            "status": "in_progress",
            "current_step": "initialized",
            "failure_reason": "",
            "campaign_status": campaign_status,
            "child_status": child_status,
            "created_campaign_id": "",
            "created_ad_groups": {},
            "created_ads": {},
            "asset_map": [],
            "shared_set_map": {},
            "shared_sets_associated_ids": [],
            "audience_sync": {},
            "criterion_applied": [],
            "targeting_warnings": [],
            "account_exclusion_keys": [],
            "conversion_actions_ready": [],
            "campaign_goal_applied": False,
            "inventory_applied": False,
            "account_level_warnings": [],
        }
        self._write_state(path, state)
        return path, state

    def _read_campaigns(self) -> list[dict[str, Any]]:
        return parse_campaigns(self.target.page.rpc(
            "/aw/_/rpc/CampaignService/List", campaign_list_request(self.target.ocid),
        ))

    def _read_groups(self) -> list[dict[str, Any]]:
        return parse_ad_groups(self.target.page.rpc(
            "/aw/_/rpc/AdGroupService/List", ad_group_list_request(self.target.ocid),
        ))

    def _read_ads(self, group_id: str = "") -> list[dict[str, Any]]:
        return parse_ads(self.target.page.rpc(
            "/aw/_/rpc/AdGroupAdService/List",
            ad_list_request(self.target.ocid, ad_group_id=group_id),
        ))

    @staticmethod
    def _resolve_override_assets(snapshot: dict[str, Any], override_dir: Path) -> list[str]:
        """Local replacement images, matched to snapshot assets by directory order.

        Not a hash comparison against the snapshot's own files -- these are
        deliberately different (new) bytes.  Only existence, count, and format
        (via `validate_image`) are checked; listing is restricted to files
        directly inside the resolved directory, so no relative-path traversal
        is possible.
        """
        override_dir = override_dir.expanduser().resolve()
        if not override_dir.is_dir():
            raise ValueError(f"素材目录不存在：{override_dir}")
        files = sorted(
            path for path in override_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}
        )
        expected = len(snapshot["assets"])
        if len(files) != expected:
            raise ValueError(
                f"素材目录文件数量与快照不一致：快照需要 {expected} 个，目录内找到 {len(files)} 个"
            )
        for path in files:
            validate_image(path)
        return [str(path) for path in files]

    def status(
        self, snapshot: dict[str, Any], *, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        validate_snapshot(snapshot)
        key = _idempotency_key(snapshot, self.target.cid, idempotency_key)
        path = self._state_path(snapshot, key)
        if not path.exists():
            return {
                "status": "not_started", "current_step": "not_started",
                "failure_reason": "", "state_path": str(path),
            }
        state = json.loads(path.read_text(encoding="utf-8"))
        if (
            str(state.get("snapshot_id")) != str(snapshot["snapshot_id"])
            or str(state.get("target_cid")) != str(self.target.cid)
            or str(state.get("idempotency_key")) != key
        ):
            raise RuntimeError("幂等状态与当前快照或目标 CID 不匹配")
        return {**state, "state_path": str(path)}

    def verify(
        self, snapshot: dict[str, Any], state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validate_snapshot(snapshot)
        if state is None:
            key = _idempotency_key(snapshot, self.target.cid)
            path = self._state_path(snapshot, key)
            if not path.exists():
                return {"ok": False, "reason": "未找到重建状态", "checks": {}}
            state = json.loads(path.read_text(encoding="utf-8"))
        expected_status = int(state.get("campaign_status", PAUSED_CAMPAIGN))
        expected_child_status = int(state.get("child_status", PAUSED_CHILD))
        catalog = self._target_catalog()
        campaign_id = str(state.get("created_campaign_id") or "")
        group_map = {str(k): str(v) for k, v in (state.get("created_ad_groups") or {}).items()}
        ad_map = {str(k): str(v) for k, v in (state.get("created_ads") or {}).items()}
        campaigns = [row for row in catalog.campaigns if str(row.get("campaign_id")) == campaign_id]
        groups_by_id = {str(row.get("ad_group_id")): row for row in catalog.ad_groups}
        ads_by_id = {str(row.get("ad_id")): row for row in catalog.ads}
        checks = {
            "campaign_exists_expected_status": len(campaigns) == 1 and campaigns[0].get("status_raw") == expected_status,
            "all_groups_mapped": len(group_map) == len(snapshot["ad_groups"]),
            "all_groups_exist_expected_status": bool(group_map) and all(
                target_id in groups_by_id
                and groups_by_id[target_id].get("status_raw") == expected_child_status
                and str(groups_by_id[target_id].get("campaign_id")) == campaign_id
                for target_id in group_map.values()
            ),
            "all_ads_mapped": len(ad_map) == len(snapshot["ads"]),
            "all_ads_exist_expected_status": bool(ad_map) and all(
                target_id in ads_by_id
                and ads_by_id[target_id].get("status_raw") == expected_child_status
                and str(ads_by_id[target_id].get("ad_group_id"))
                == group_map.get(str(source_ad.get("3") or ""), "")
                for source_ad in snapshot["ads"]
                for target_id in [ad_map.get(str(source_ad.get("4") or ""), "")]
            ),
        }
        shared_set_entries = list(snapshot.get("negative_keywords") or []) + list(
            snapshot.get("shared_sets") or []
        )
        if shared_set_entries and campaign_id:
            shared_set_map = {
                str(k): str(v) for k, v in (state.get("shared_set_map") or {}).items()
            }
            assoc_response = self.target.page.rpc(
                CAMPAIGN_SHARED_SET_LIST_PATH,
                campaign_shared_set_list_request(self.target.ocid, campaign_id),
            )
            assoc_rows = assoc_response.get("1") or []
            if isinstance(assoc_rows, dict):
                assoc_rows = [assoc_rows]
            associated_ids = {str(row.get("2")) for row in assoc_rows if row.get("2")}
            checks["shared_sets_associated"] = bool(shared_set_map) and all(
                shared_set_map.get(str(entry.get("source_shared_set_id"))) in associated_ids
                for entry in shared_set_entries
            )
        custom_audiences = list(snapshot.get("custom_audiences") or [])
        channel = int(snapshot["campaign"].get("15") or 0)
        if custom_audiences and channel == DISPLAY_CHANNEL:
            audience_sync = state.get("audience_sync") or {}
            expected_by_group: dict[str, dict[str, set[str]]] = {}
            for entry in custom_audiences:
                target_group_id = group_map.get(str(entry.get("source_ad_group_id") or ""), "")
                if not target_group_id:
                    continue
                bucket = expected_by_group.setdefault(
                    target_group_id, {"user_interest": set(), "custom_audience": set()},
                )
                kind = str(entry.get("kind") or "")
                if kind == "user_interest":
                    bucket["user_interest"].add(str(entry.get("audience_id")))
            audiences_synced = bool(audience_sync)
            for target_group_id, expected in expected_by_group.items():
                current_response = self.target.page.rpc(
                    AUDIENCE_LIST_PATH,
                    ad_group_audience_list_request(self.target.ocid, campaign_id, target_group_id),
                )
                current_raw = current_response.get("1")
                current_rows = (
                    [] if current_raw is None
                    else current_raw if isinstance(current_raw, list) else [current_raw]
                )
                current_user_interest = {
                    str(row.get("3"))
                    for row in current_rows
                    if row.get("3") and not row.get(AUDIENCE_LIST_FIELD_CUSTOM_AUDIENCE_ID)
                }
                if not expected["user_interest"] <= current_user_interest:
                    audiences_synced = False
            checks["audiences_synced"] = audiences_synced
        ok = all(checks.values())
        return {"ok": ok, "reason": "" if ok else "写后回读校验未全部通过", "checks": checks}

    def recreate(
        self, snapshot: dict[str, Any], *,
        idempotency_key: str | None = None,
        asset_override_dir: Path | str | None = None,
        initial_status: bool = True,
        naming_label: str = "",
        existing_budget_id: str | None = None,
    ) -> dict[str, Any]:
        validate_snapshot(snapshot)
        channel = int(snapshot["campaign"].get("15") or 0)
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"本地快照重建暂不支持该广告系列类型（channel={channel}）")
        name = compose_campaign_name(naming_label)
        target_status = CAMPAIGN_TARGET_STATUS[initial_status]
        target_child_status = CHILD_TARGET_STATUS[initial_status]
        key = _idempotency_key(snapshot, self.target.cid, idempotency_key)
        state_path, state = self._load_state(
            snapshot, key, campaign_status=target_status, child_status=target_child_status,
        )
        target_status = int(state.get("campaign_status", target_status))
        target_child_status = int(state.get("child_status", target_child_status))
        if state.get("status") == "completed":
            verification = self.verify(snapshot, state)
            if verification["ok"]:
                return self._result(snapshot, state_path, state, verification, idempotent=True)
            raise RuntimeError("已有完成记录但目标对象校验失败，禁止重复创建")
        state["status"] = "in_progress"
        state["failure_reason"] = ""
        self._write_state(state_path, state)
        wrote_this_run = False
        try:
            validate_snapshot_files(snapshot)
            ensure_account_level_state(state)
            if snapshot.get("account_level"):
                self.log(f"[{format_cid(self.target.cid)}] 补账号内容适宜性和联系人转化目标")
                if apply_account_settings(
                    self.target.page, self.target.ocid, snapshot.get("account_level"), state, self.log,
                ):
                    wrote_this_run = True
                state["current_step"] = "account_level_applied"
                self._write_state(state_path, state)
            campaign_id = str(state.get("created_campaign_id") or "")
            if not campaign_id:
                matches = [
                    row for row in self._read_campaigns()
                    if row.get("name") == name and row.get("status_raw") != 7
                ]
                if len(matches) > 1:
                    raise RuntimeError("目标 CID 已有多个同名广告系列，无法安全判断")
                if matches:
                    if matches[0].get("status_raw") != target_status:
                        raise RuntimeError("同名广告系列状态与本次重建目标不一致")
                    created_campaign = matches[0]
                else:
                    budget_id = str(existing_budget_id or "") or self._matching_budget(snapshot)
                    self.log(
                        f"[{format_cid(self.target.cid)}] 使用已有预算 {budget_id}，"
                        "不新建预算，这一步不用密钥"
                    )
                    response = self.target.page.rpc(
                        "/aw_cm/editing/_/rpc/BatchService/Batch",
                        portable_campaign_batch_request(
                            ocid=self.target.ocid, name=name,
                            source_campaign=snapshot["campaign"],
                            target_status=target_status,
                            existing_budget_id=budget_id,
                        ),
                    )
                    wrote_this_run = True
                    error = _service_error(response)
                    if error:
                        gate = human_gate_for_error("新建广告系列", error)
                        if gate:
                            raise gate
                        raise RuntimeError(
                            f"新建广告系列失败：{error}。"
                            "如果提示预算已被占用，请再建一条相同金额的日预算，然后重新导入。"
                        )
                    created_campaign = self._poll_one(
                        self._read_campaigns,
                        lambda row: row.get("name") == name
                        and row.get("status_raw") == target_status,
                        "广告系列",
                    )
                campaign_id = str(created_campaign["campaign_id"])
                state["created_campaign_id"] = campaign_id
                state["current_step"] = "campaign_created"
                self._write_state(state_path, state)
            else:
                created_campaign = next((
                    row for row in self._read_campaigns()
                    if str(row.get("campaign_id")) == campaign_id
                    and row.get("status_raw") == target_status
                ), None)
                if not created_campaign:
                    raise RuntimeError("状态记录中的目标广告系列不存在或状态不符")

            if snapshot.get("account_level"):
                if apply_campaign_goals(
                    self.target.page,
                    self.target.ocid,
                    campaign_id,
                    snapshot.get("account_level") or {},
                    state,
                    self.log,
                ):
                    wrote_this_run = True
                state["current_step"] = "campaign_goals_applied"
                self._write_state(state_path, state)

            group_map = state["created_ad_groups"]
            for index, source_group in enumerate(snapshot["ad_groups"], 1):
                source_group_id = str(source_group.get("3") or "")
                if source_group_id in group_map:
                    continue
                source_name = str(source_group.get("11") or "Ad group")
                mapped_group_ids = {str(value) for value in group_map.values()}
                recoverable_groups = [
                    row for row in self._read_groups()
                    if str(row.get("campaign_id")) == campaign_id
                    and row.get("name") == source_name
                    and row.get("status_raw") == target_child_status
                    and str(row.get("ad_group_id")) not in mapped_group_ids
                ]
                if len(recoverable_groups) > 1:
                    raise RuntimeError("发现多个未记录的同名同状态广告组，无法安全恢复")
                if recoverable_groups:
                    group_map[source_group_id] = str(recoverable_groups[0]["ad_group_id"])
                    state["current_step"] = "groups_created"
                    self._write_state(state_path, state)
                    continue
                response = self.target.page.rpc(
                    "/aw_essentials/_/rpc/AdGroupService/Mutate",
                    portable_ad_group_request(
                        ocid=self.target.ocid, campaign_id=campaign_id,
                        temporary_id=f"-{index}", source_group=source_group,
                        target_child_status=target_child_status,
                    ),
                )
                wrote_this_run = True
                error = _service_error(response)
                if error:
                    raise RuntimeError(f"新建广告组失败：{error}")
                created_group = self._poll_one(
                    self._read_groups,
                    lambda row, n=source_name: row.get("campaign_id") == campaign_id
                    and row.get("name") == n and row.get("status_raw") == target_child_status,
                    "广告组",
                )
                group_map[source_group_id] = str(created_group["ad_group_id"])
                state["current_step"] = "groups_created"
                self._write_state(state_path, state)

            criterion_entries = list(snapshot.get("campaign_criteria") or []) + list(
                snapshot.get("ad_group_criteria") or []
            )
            if criterion_entries:
                self.log(
                    f"[{format_cid(self.target.cid)}] 写入主题、地区、语言、设备、投放时段等定向"
                )
                apply_criteria(
                    self.target.page,
                    self.target.ocid,
                    campaign_id,
                    criterion_entries,
                    group_map=group_map,
                    applied_keys=state["criterion_applied"],
                    log=self.log,
                )
                wrote_this_run = True
                state["current_step"] = "criteria_applied"
                self._write_state(state_path, state)
            for gap in snapshot.get("targeting_gaps") or []:
                warning = (
                    f"有一条定向没识别，没有写入"
                    f"（criterion {gap.get('source_criterion_id') or ''}）"
                )
                if warning not in state["targeting_warnings"]:
                    state["targeting_warnings"].append(warning)

            asset_map = state["asset_map"]
            if snapshot.get("assets") and not asset_map:
                if asset_override_dir:
                    asset_paths = self._resolve_override_assets(snapshot, Path(asset_override_dir))
                else:
                    snapshot_root = Path(str(snapshot["snapshot_path"])).parent
                    asset_paths = [
                        str(snapshot_root / str(asset["relative_path"]))
                        for asset in snapshot["assets"]
                    ]
                results = upload_images(
                    self.target.page, asset_paths, account_id=self.target.ocid,
                    manager_id=self.target_manager_ocid,
                )
                wrote_this_run = True
                if len(results) != len(snapshot["assets"]) or any(not item.ok for item in results):
                    failure = next((item.error for item in results if not item.ok), "图片上传未完整返回")
                    raise RuntimeError(f"目标账户图片重传失败：{failure}")
                for source_asset, uploaded in zip(snapshot["assets"], results):
                    asset_map.append({
                        "source_asset_id": str(source_asset["source_asset_id"]),
                        "target_asset_id": str(uploaded.asset_id),
                        "filename": str(source_asset.get("filename") or uploaded.filename),
                    })
                state["current_step"] = "assets_uploaded"
                self._write_state(state_path, state)

            ad_map = state["created_ads"]
            for index, source_ad in enumerate(snapshot["ads"], 1):
                source_ad_id = str(source_ad.get("4") or "")
                if source_ad_id in ad_map:
                    continue
                source_group_id = str(source_ad.get("3") or "")
                target_group_id = str(group_map.get(source_group_id) or "")
                if not target_group_id:
                    raise RuntimeError(f"广告 {source_ad_id} 找不到目标广告组映射")
                current_ads = self._read_ads(target_group_id)
                mapped_ad_ids = {str(value) for value in ad_map.values()}
                recoverable_ads = [
                    row for row in current_ads
                    if str(row.get("ad_group_id")) == target_group_id
                    and row.get("status_raw") == target_child_status
                    and str(row.get("ad_id")) not in mapped_ad_ids
                ]
                if len(recoverable_ads) > 1:
                    raise RuntimeError("发现多个未记录的同状态广告，无法安全恢复")
                if recoverable_ads:
                    ad_map[source_ad_id] = str(recoverable_ads[0]["ad_id"])
                    state["current_step"] = "ads_created"
                    self._write_state(state_path, state)
                    continue
                before_ids = {str(row.get("ad_id")) for row in current_ads}
                creative_path = (((source_ad.get("13") or {}).get("100") or {}))
                if channel == APP_CHANNEL:
                    source_image_ids = {
                        _asset_id(item)
                        for item in ((creative_path.get("44") or {}).get("4") or [])
                        if _asset_id(item)
                    }
                    mapped_assets = [
                        item for item in asset_map
                        if item["source_asset_id"] in source_image_ids
                    ]
                    if len(mapped_assets) != len(source_image_ids):
                        raise RuntimeError(f"广告 {source_ad_id} 的图片素材映射不完整")
                    response = self.target.page.rpc(
                        "/aw_cm/editing/_/rpc/BatchService/Batch",
                        portable_app_ad_request(
                            ocid=self.target.ocid, campaign_id=campaign_id,
                            ad_group_id=target_group_id, temporary_id=f"-{index}",
                            source_ad=source_ad, target_assets=mapped_assets,
                            target_child_status=target_child_status,
                        ),
                    )
                else:
                    display_creative = creative_path.get("41") or {}
                    asset_by_source_id = {item["source_asset_id"]: item for item in asset_map}

                    def _mapped(field: str, label: str) -> list[dict[str, str]]:
                        ids = [
                            _asset_id(item) for item in (display_creative.get(field) or [])
                            if _asset_id(item)
                        ]
                        mapped = [asset_by_source_id[i] for i in ids if i in asset_by_source_id]
                        if len(mapped) != len(ids):
                            raise RuntimeError(f"广告 {source_ad_id} 的{label}素材映射不完整")
                        return mapped

                    landscape_assets = _mapped("1", "横图")
                    square_assets = _mapped("2", "方图")
                    portrait_assets = _mapped("20", "竖图")
                    response = self.target.page.rpc(
                        "/aw_cm/editing/_/rpc/BatchService/Batch",
                        portable_display_ad_request(
                            ocid=self.target.ocid, campaign_id=campaign_id,
                            ad_group_id=target_group_id, temporary_id=f"-{index}",
                            source_ad=source_ad,
                            landscape_assets=landscape_assets,
                            square_assets=square_assets,
                            portrait_assets=portrait_assets,
                            target_child_status=target_child_status,
                        ),
                    )
                wrote_this_run = True
                error = _service_error(response)
                if error:
                    final_urls = [
                        str(value)
                        for value in (
                            ((source_ad.get("13") or {}).get("100") or {}).get("1")
                            or (source_ad.get("13") or {}).get("5")
                            or []
                        )
                        if value
                    ]
                    gate = human_gate_for_error(
                        "新建广告", error, final_url=final_urls[0] if final_urls else "",
                    )
                    if gate:
                        raise gate
                    raise RuntimeError(f"新建广告失败：{error}")
                created_ad = self._poll_one(
                    lambda gid=target_group_id: self._read_ads(gid),
                    lambda row, known=before_ids: row.get("ad_group_id") == target_group_id
                    and str(row.get("ad_id")) not in known
                    and row.get("status_raw") == target_child_status,
                    "广告",
                )
                ad_map[source_ad_id] = str(created_ad["ad_id"])
                state["current_step"] = "ads_created"
                self._write_state(state_path, state)

            shared_set_map = state["shared_set_map"]
            shared_set_entries = list(snapshot.get("negative_keywords") or []) + list(
                snapshot.get("shared_sets") or []
            )
            for entry in shared_set_entries:
                source_shared_set_id = str(entry.get("source_shared_set_id") or "")
                if source_shared_set_id in shared_set_map:
                    continue
                set_kind = int(entry.get("type") or 0)
                shared_set_name = str(entry.get("name") or "")
                values = [str(value) for value in entry.get("values") or [] if str(value)]
                if not shared_set_name or not values:
                    continue
                existing_response = self.target.page.rpc(
                    SHARED_SET_LIST_PATH,
                    shared_set_list_by_name_request(self.target.ocid, set_kind, shared_set_name),
                )
                existing_rows = existing_response.get("1") or []
                if isinstance(existing_rows, dict):
                    existing_rows = [existing_rows]
                existing = next(
                    (row for row in existing_rows if str(row.get("3")) == shared_set_name), None,
                )
                if existing:
                    target_shared_set_id = str(existing.get("2"))
                    criterion_response = self.target.page.rpc(
                        SHARED_CRITERION_LIST_PATH,
                        shared_criterion_list_request(self.target.ocid, target_shared_set_id, set_kind),
                    )
                    criterion_rows = criterion_response.get("1") or []
                    if isinstance(criterion_rows, dict):
                        criterion_rows = [criterion_rows]
                    value_field = "101" if set_kind == SHARED_SET_TYPE_NEGATIVE_KEYWORDS else "102"
                    existing_values = {
                        str((row.get(value_field) or {}).get("1"))
                        for row in criterion_rows
                        if (row.get(value_field) or {}).get("1")
                    }
                    missing_values = [value for value in values if value not in existing_values]
                    if missing_values:
                        append_response = self.target.page.rpc(
                            SHARED_CRITERION_MUTATE_PATH[set_kind],
                            append_shared_criteria_request(
                                self.target.ocid, target_shared_set_id, set_kind, missing_values,
                            ),
                        )
                        error = _service_error(append_response)
                        if error:
                            raise RuntimeError(f"追加共享集合内容失败：{error}")
                else:
                    create_response = self.target.page.rpc(
                        SHARED_SET_BATCH_PATH[set_kind],
                        create_shared_set_and_criteria_batch_request(
                            self.target.ocid, set_kind, shared_set_name, values,
                        ),
                    )
                    error = _service_error(create_response)
                    if error:
                        raise RuntimeError(f"新建共享集合失败：{error}")
                    created = self._poll_one(
                        lambda sk=set_kind, n=shared_set_name: self._target_shared_sets(sk, n),
                        lambda row, n=shared_set_name: row.get("name") == n,
                        "共享集合",
                    )
                    target_shared_set_id = str(created["shared_set_id"])
                if source_shared_set_id not in state["shared_sets_associated_ids"]:
                    assoc_response = self.target.page.rpc(
                        CAMPAIGN_SHARED_SET_MUTATE_PATH[set_kind],
                        campaign_shared_set_mutate_request(
                            self.target.ocid, campaign_id, name, target_shared_set_id,
                        ),
                    )
                    error = _service_error(assoc_response)
                    if error:
                        raise RuntimeError(f"关联共享集合到广告系列失败：{error}")
                    state["shared_sets_associated_ids"].append(source_shared_set_id)
                shared_set_map[source_shared_set_id] = target_shared_set_id
                wrote_this_run = True
                state["current_step"] = "shared_sets_merged"
                self._write_state(state_path, state)

            custom_audiences = list(snapshot.get("custom_audiences") or [])
            if custom_audiences and channel == DISPLAY_CHANNEL:
                audience_sync = state["audience_sync"]
                audiences_by_group: dict[str, list[dict[str, Any]]] = {}
                for entry in custom_audiences:
                    audiences_by_group.setdefault(str(entry.get("source_ad_group_id") or ""), []).append(entry)
                for source_group_id, entries in audiences_by_group.items():
                    target_group_id = str(group_map.get(source_group_id) or "")
                    if not target_group_id or target_group_id in audience_sync:
                        continue
                    current_response = self.target.page.rpc(
                        AUDIENCE_LIST_PATH,
                        ad_group_audience_list_request(self.target.ocid, campaign_id, target_group_id),
                    )
                    current_raw = current_response.get("1")
                    current_rows = (
                        [] if current_raw is None
                        else current_raw if isinstance(current_raw, list) else [current_raw]
                    )
                    current_by_kind: dict[str, dict[str, str]] = {"user_interest": {}, "custom_audience": {}}
                    for row in current_rows:
                        criterion_id = str(row.get("3") or "")
                        custom_audience_id = row.get(AUDIENCE_LIST_FIELD_CUSTOM_AUDIENCE_ID)
                        if custom_audience_id:
                            current_by_kind["custom_audience"][str(custom_audience_id)] = criterion_id
                        elif criterion_id:
                            current_by_kind["user_interest"][criterion_id] = criterion_id
                    warnings: list[str] = []
                    desired_by_kind: dict[str, set[str]] = {"user_interest": set(), "custom_audience": set()}
                    for entry in entries:
                        kind = str(entry.get("kind") or "")
                        if kind == "user_interest":
                            desired_by_kind["user_interest"].add(str(entry.get("audience_id")))
                        elif kind == "custom_audience":
                            name = str(entry.get("name") or "")
                            catalog_response = self.target.page.rpc(
                                CUSTOM_AUDIENCE_LIST_PATH,
                                target_custom_audience_list_request(self.target.ocid),
                            )
                            match = next((
                                row for row in catalog_response.get("1") or []
                                if str(row.get("3") or "") == name
                            ), None)
                            if not match:
                                warnings.append(f"目标账户找不到同名自定义受众群：{name}")
                                continue
                            desired_by_kind["custom_audience"].add(str(match.get("1")))
                    for kind, desired_ids in desired_by_kind.items():
                        existing_ids = set(current_by_kind[kind].keys())
                        for audience_id in existing_ids - desired_ids:
                            remove_response = self.target.page.rpc(
                                AD_GROUP_CRITERION_MUTATE_PATH,
                                ad_group_audience_mutate_request(
                                    self.target.ocid, target_group_id, kind, audience_id,
                                    criterion_id=current_by_kind[kind][audience_id], remove=True,
                                ),
                            )
                            error = _service_error(remove_response)
                            if error:
                                raise RuntimeError(f"移除受众定向失败：{error}")
                        for audience_id in desired_ids - existing_ids:
                            create_response = self.target.page.rpc(
                                AD_GROUP_CRITERION_MUTATE_PATH,
                                ad_group_audience_mutate_request(
                                    self.target.ocid, target_group_id, kind, audience_id,
                                ),
                            )
                            error = _service_error(create_response)
                            if error:
                                raise RuntimeError(f"新增受众定向失败：{error}")
                    wrote_this_run = True
                    audience_sync[target_group_id] = {
                        "source_ad_group_id": source_group_id, "warnings": warnings,
                    }
                    state["current_step"] = "audiences_applied"
                    self._write_state(state_path, state)
            elif custom_audiences:
                self.log(f"[{format_cid(self.target.cid)}] 该广告类型的受众关联未验证，已跳过")

            state["current_step"] = "verifying"
            self._write_state(state_path, state)
            verification = self.verify(snapshot, state)
            if not verification["ok"]:
                raise RuntimeError(verification["reason"])
            state["status"] = "completed"
            state["current_step"] = "completed"
            self._write_state(state_path, state)
            return self._result(
                snapshot, state_path, state, verification,
                idempotent=not wrote_this_run,
            )
        except HumanActionRequired as exc:
            state["status"] = "needs_human"
            state["failure_reason"] = str(exc)
            self._write_state(state_path, state)
            raise
        except Exception as exc:
            state["status"] = "failed"
            state["failure_reason"] = str(exc)
            self._write_state(state_path, state)
            raise RuntimeError(f"可移植复制失败（状态：{state_path}）：{exc}") from exc

    def _matching_budget(self, snapshot: dict[str, Any]) -> str:
        amount = str((snapshot.get("campaign") or {}).get("17") or "")
        if not amount.isdigit() or int(amount) <= 0:
            raise RuntimeError("来源广告系列缺少有效日预算，不能匹配目标账号里的现成预算")
        currency = str((snapshot.get("campaign") or {}).get("18") or "").strip().upper()
        rows = parse_campaign_details(self.target.page.rpc(
            "/aw/_/rpc/CampaignService/List",
            campaign_detail_request(self.target.ocid),
        ))
        budget_id = select_existing_budget(rows, int(amount))
        if not budget_id:
            raise HumanActionRequired("budget", budget_gate_message(int(amount), currency))
        return budget_id

    @staticmethod
    def _result(
        snapshot: dict[str, Any], state_path: Path, state: dict[str, Any],
        verification: dict[str, Any], *, idempotent: bool,
    ) -> dict[str, Any]:
        return {
            "mode": "snapshot_import",
            "write_performed": not idempotent,
            "success": True,
            "idempotent_replay": idempotent,
            "idempotency_key": state["idempotency_key"],
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_path": str(snapshot["snapshot_path"]),
            "state_path": str(state_path),
            "step_status": state["current_step"],
            "failure_reason": "",
            "created": {"campaign_id": state["created_campaign_id"]},
            "created_campaign": {"campaign_id": state["created_campaign_id"]},
            "created_ad_groups": dict(state["created_ad_groups"]),
            "created_ads": list(state["created_ads"].values()),
            "asset_map": list(state["asset_map"]),
            "shared_set_map": dict(state.get("shared_set_map", {})),
            "audience_sync": dict(state.get("audience_sync", {})),
            "targeting_warnings": list(state.get("targeting_warnings") or []),
            "account_level_warnings": list(state.get("account_level_warnings") or []),
            "post_read_verified": verification["ok"],
            "verification": verification,
            "rebuild_strategy": "local_portable_snapshot",
            "next_step": (
                "系列已暂停。请到目标账号网页打开这条系列，点发布。弹出密钥时摸一下 U 盘。"
                if int(state.get("campaign_status") or 0) == PAUSED_CAMPAIGN
                else "系列已按你的选择启用。如果网页还要求确认，请自己点发布并摸一下 U 盘。"
            ),
        }


def batch_recreate(
    importer_factory: Callable[[str], SnapshotImporter],
    snapshot: dict[str, Any],
    targets: list[dict[str, Any]],
    *,
    asset_override_dir: Path | str | None = None,
    log: Callable[[str], None],
) -> list[dict[str, Any]]:
    """Import `snapshot` into each of `targets`, skipping failures.

    Each target dict: {"cid", "label", "initial_status"}. `importer_factory`
    lazily connects to the target account (which may live in a different
    fingerprint browser window) only when its turn comes up.
    """
    results: list[dict[str, Any]] = []
    for target in targets:
        cid = str(target["cid"])
        entry: dict[str, Any] = {"cid": cid, "cid_display": format_cid(cid), "success": False}
        try:
            importer = importer_factory(cid)
            result = importer.recreate(
                snapshot,
                idempotency_key=_idempotency_key(snapshot, cid),
                asset_override_dir=asset_override_dir,
                initial_status=bool(target["initial_status"]),
                naming_label=str(target["label"]),
            )
            entry.update(result)
            entry["success"] = True
        except Exception as exc:
            entry["error"] = str(exc)
            log(f"[{format_cid(cid)}] 复制失败，跳过：{exc}")
        results.append(entry)
    return results
