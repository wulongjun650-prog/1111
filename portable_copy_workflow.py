from __future__ import annotations

import copy
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
)
from .portable_copy import (
    AD_GROUP_SNAPSHOT_FIELDS,
    APP_AD_SNAPSHOT_FIELDS,
    APP_CHANNEL,
    CAMPAIGN_SNAPSHOT_FIELDS,
    CAMPAIGN_TARGET_STATUS,
    CHILD_TARGET_STATUS,
    DISPLAY_AD_SNAPSHOT_FIELDS,
    PAUSED_CAMPAIGN,
    PAUSED_CHILD,
    SUPPORTED_CHANNELS,
    _list_request,
    _service_error,
    portable_ad_group_request,
    portable_app_ad_request,
    portable_campaign_batch_request,
    portable_display_ad_request,
)
from .protocol import AccountPage, ProtocolRunner
from .targeting_copy import HumanActionRequired


SNAPSHOT_SCHEMA_VERSION = 3
STATE_SCHEMA_VERSION = 1
SCHEMA_NOTES = {
    "negative_keywords": "replicated",
    "shared_sets": "replicated",
    "custom_audiences": "replicated",
    "campaign_criteria": "replicated",
    "ad_group_criteria": "replicated",
    "content_suitability": "replicated",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _idempotency_key(
    snapshot: dict[str, Any], target_cid: str, provided: str | None = None,
) -> str:
    raw = str(provided) if provided is not None else _canonical_json({
        "snapshot_id": snapshot["snapshot_id"], "target_cid": str(target_cid),
    })
    if not raw.strip():
        raise ValueError("幂等键不能为空")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _asset_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    asset = item.get("1")
    if not isinstance(asset, dict):
        return ""
    return str(asset.get("1") or "")


def referenced_image_filenames(ads: list[dict[str, Any]]) -> dict[str, str]:
    """Asset id to filename for image slots the manifest treats as images.

    Display uses landscape/square/portrait. App campaigns use the image list.
    Text assets are not included, even when they still carry a source asset id.
    """
    names: dict[str, str] = {}
    for ad in ads:
        mutable = (ad.get("13") or {}).get("100") or {}
        app_assets = mutable.get("44")
        display_creative = mutable.get("41")
        if isinstance(app_assets, dict) and app_assets:
            creative, fields = app_assets, ("4",)
        elif isinstance(display_creative, dict) and display_creative:
            creative, fields = display_creative, ("1", "2", "20")
        else:
            continue
        for field in fields:
            values = creative.get(field) or []
            if not isinstance(values, list):
                continue
            for wrapper in values:
                if not isinstance(wrapper, dict):
                    continue
                asset = wrapper.get("1") if isinstance(wrapper.get("1"), dict) else {}
                asset_id = str(asset.get("1") or "")
                if asset_id:
                    names.setdefault(
                        asset_id, str(asset.get("2") or f"asset-{asset_id}.png"),
                    )
    return names


def asset_reference_manifest(ads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return explicit per-ad references while retaining the complete raw ad snapshot."""
    result: list[dict[str, Any]] = []
    app_labels = {
        "2": "headline",
        "3": "description",
        "4": "image",
        "5": "youtube_video",
        "6": "html5_media_bundle",
    }
    display_labels = {
        "1": "landscape_image",
        "2": "square_image",
        "20": "portrait_image",
        "5": "headline",
        "6": "long_headline",
        "7": "description",
    }
    for ad in ads:
        mutable = (ad.get("13") or {}).get("100") or {}
        app_assets = mutable.get("44")
        display_creative = mutable.get("41")
        if isinstance(app_assets, dict) and app_assets:
            creative, labels = app_assets, app_labels
        elif isinstance(display_creative, dict) and display_creative:
            creative, labels = display_creative, display_labels
        else:
            creative, labels = {}, {}
        references: list[dict[str, str]] = []
        if isinstance(creative, dict):
            for field, values in creative.items():
                if not isinstance(values, list):
                    continue
                for value in values:
                    asset_id = _asset_id(value)
                    if asset_id:
                        references.append({
                            "kind": labels.get(str(field), f"field_{field}"),
                            "source_asset_id": asset_id,
                        })
        result.append({
            "source_ad_id": str(ad.get("4") or ""),
            "source_ad_group_id": str(ad.get("3") or ""),
            "references": references,
        })
    return result


def snapshot_identity(snapshot: dict[str, Any]) -> str:
    """Stable identity independent of timestamps, absolute paths, and browser OCIDs."""
    assets = [{
        "source_asset_id": str(item.get("source_asset_id") or ""),
        "filename": str(item.get("filename") or ""),
        "mime_type": str(item.get("mime_type") or ""),
        "sha256": str(item.get("sha256") or ""),
        "size": str(item.get("size") or ""),
    } for item in snapshot.get("assets") or []]
    content = {
        "source_cid": str((snapshot.get("source") or {}).get("cid") or ""),
        "source_campaign_id": str(
            (snapshot.get("source") or {}).get("campaign_id") or ""
        ),
        "campaign": snapshot.get("campaign") or {},
        "ad_groups": snapshot.get("ad_groups") or [],
        "ads": snapshot.get("ads") or [],
        "assets": assets,
        "asset_references": snapshot.get("asset_references") or [],
        "negative_keywords": snapshot.get("negative_keywords") or [],
        "shared_sets": snapshot.get("shared_sets") or [],
        "custom_audiences": snapshot.get("custom_audiences") or [],
    }
    # Older snapshots were hashed without these keys. Include them only when
    # the file actually has them, so existing snapshot_id values still match.
    for key in ("campaign_criteria", "ad_group_criteria", "targeting_gaps"):
        if key in snapshot:
            content[key] = snapshot.get(key) or []
    # account_level is new. Old files omit it, so their snapshot_id stays valid.
    if "account_level" in snapshot:
        content["account_level"] = snapshot.get("account_level") or {}
    return _sha256_json(content)


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    if int(snapshot.get("schema_version") or 0) != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("不支持的本地复制快照版本")
    campaign = snapshot.get("campaign")
    groups = snapshot.get("ad_groups")
    ads = snapshot.get("ads")
    if not isinstance(campaign, dict) or not isinstance(groups, list) or not isinstance(ads, list):
        raise ValueError("本地复制快照结构不完整")
    if not groups or not ads:
        raise ValueError("本地复制快照必须包含广告组和广告")
    campaign_id = str((snapshot.get("source") or {}).get("campaign_id") or "")
    group_ids = [str(row.get("3") or "") for row in groups]
    ad_ids = [str(row.get("4") or "") for row in ads]
    if not campaign_id or any(not value for value in group_ids + ad_ids):
        raise ValueError("本地复制快照缺少来源对象 ID")
    if len(set(group_ids)) != len(group_ids) or len(set(ad_ids)) != len(ad_ids):
        raise ValueError("本地复制快照包含重复对象 ID")
    if any(str(row.get("2") or "") != campaign_id for row in groups):
        raise ValueError("本地复制快照包含不属于来源广告系列的广告组")
    group_id_set = set(group_ids)
    if any(str(row.get("3") or "") not in group_id_set for row in ads):
        raise ValueError("本地复制快照包含无法映射到广告组的广告")
    actual_id = snapshot_identity(snapshot)
    expected_id = str(snapshot.get("snapshot_id") or "")
    if expected_id and expected_id != actual_id:
        raise ValueError("本地复制快照内容校验失败")


def validate_snapshot_files(snapshot: dict[str, Any]) -> None:
    """Validate portable asset paths and bytes before any target mutation."""
    snapshot_path = Path(str(snapshot.get("snapshot_path") or ""))
    if not snapshot_path.name:
        raise ValueError("本地复制快照缺少 snapshot_path")
    root = snapshot_path.parent.resolve()
    for asset in snapshot.get("assets") or []:
        relative = Path(str(asset.get("relative_path") or ""))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("本地复制快照包含不安全的素材路径")
        path = (root / relative).resolve()
        if root != path and root not in path.parents:
            raise ValueError("本地复制快照素材路径越界")
        if not path.is_file():
            raise ValueError(f"本地复制素材不存在：{relative.as_posix()}")
        content = path.read_bytes()
        if str(len(content)) != str(asset.get("size") or ""):
            raise ValueError(f"本地复制素材大小校验失败：{relative.as_posix()}")
        if hashlib.sha256(content).hexdigest() != str(asset.get("sha256") or ""):
            raise ValueError(f"本地复制素材 SHA256 校验失败：{relative.as_posix()}")


class PortableCampaignWorkflow:
    """Resumable source snapshot -> target recreation workflow.

    It deliberately never calls CampaignCopyService.  All target mutations are
    explicit creates whose requested status is PAUSED, followed by read-only
    verification.
    """

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
        return copy.deepcopy(campaigns[0]), copy.deepcopy(groups), copy.deepcopy(ads)

    @staticmethod
    def _image_records(ads: list[dict[str, Any]]) -> list[dict[str, str]]:
        records: dict[str, dict[str, str]] = {}
        for ad in ads:
            for wrapper in ((ad.get("13") or {}).get("84") or {}).get("5") or []:
                asset = ((wrapper.get("1") or {}).get("1") or {})
                asset_id = str(asset.get("1") or "")
                metadata = wrapper.get("4") or {}
                preview = next((
                    str(item.get("2")) for item in metadata.get("6") or []
                    if isinstance(item, dict) and item.get("2")
                ), str((metadata.get("7") or {}).get("3") or ""))
                if asset_id and preview:
                    records.setdefault(asset_id, {
                        "source_asset_id": asset_id,
                        "filename": str(asset.get("2") or f"asset-{asset_id}.png"),
                        "preview_url": preview,
                    })
        return list(records.values())

    def _with_resolved_previews(
        self, ads: list[dict[str, Any]], assets: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Fill image slots that the ad payload did not already give a URL for."""
        filenames = referenced_image_filenames(ads)
        have = {item["source_asset_id"] for item in assets}
        missing = [asset_id for asset_id in filenames if asset_id not in have]
        if not missing:
            return assets
        previews = resolve_image_preview_urls(
            self.source.page, self.source.ocid,
            manager_ocid_from_page(self.source.page, self.source.ocid),
            missing, ads, log=self.log,
        )
        for asset_id in missing:
            urls = previews.get(asset_id) or []
            if not urls:
                continue
            assets.append({
                "source_asset_id": asset_id,
                "filename": filenames[asset_id],
                "preview_url": urls[0],
                "preview_urls": urls,
            })
        return assets

    @staticmethod
    def _require_image_previews(
        ads: list[dict[str, Any]], assets: list[dict[str, str]],
    ) -> None:
        referenced = set(referenced_image_filenames(ads))
        downloadable = {item["source_asset_id"] for item in assets if item.get("preview_url") or item.get("preview_urls")}
        missing = sorted(referenced - downloadable)
        if missing:
            raise RuntimeError(missing_image_message(
                missing, "素材库没有这些图片的预览地址，广告数据里也没有",
            ))

    def export(self, campaign_id: str) -> dict[str, Any]:
        campaign_id = str(campaign_id)
        self.log(f"[{format_cid(self.source.cid)}] 读取广告系列、广告组、广告及素材引用")
        campaign, groups, ads = self._raw_snapshot(campaign_id)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        folder = data_dir() / "copy-snapshots" / f"{self.source.cid}-{campaign_id}-{stamp}"
        assets_dir = folder / "assets"
        assets_dir.mkdir(parents=True, exist_ok=False)
        assets = self._image_records(ads)
        assets = self._with_resolved_previews(ads, assets)
        self._require_image_previews(ads, assets)
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
        manifest = asset_reference_manifest(ads)
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
            "negative_keywords": [],
            "shared_sets": [],
            "custom_audiences": [],
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
            "account_exclusion_keys": [],
            "conversion_actions_ready": [],
            "campaign_goal_applied": False,
            "inventory_applied": False,
            "account_level_warnings": [],
        }
        self._write_state(path, state)
        return path, state

    @staticmethod
    def _campaign_name(snapshot: dict[str, Any], idempotency_key: str) -> str:
        base = str((snapshot.get("campaign") or {}).get("11") or "App copy")
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"{base}（GGAds {key_hash[:10]}）"

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

    def status(
        self, snapshot: dict[str, Any], *, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Read the durable step/failure state without creating or mutating anything."""
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
        """Read-only post-write verification interface, also usable by GUI recovery."""
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
        ok = all(checks.values())
        return {"ok": ok, "reason": "" if ok else "写后回读校验未全部通过", "checks": checks}

    def recreate(
        self, snapshot: dict[str, Any], *, idempotency_key: str | None = None,
        initial_status: bool = True,
    ) -> dict[str, Any]:
        validate_snapshot(snapshot)
        channel = int(snapshot["campaign"].get("15") or 0)
        if channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"本地快照重建暂不支持该广告系列类型（channel={channel}）")
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
            name = self._campaign_name(snapshot, key)
            if not campaign_id:
                matches = [
                    row for row in self._read_campaigns()
                    if row.get("name") == name and row.get("status_raw") != 7
                ]
                if len(matches) > 1:
                    raise RuntimeError("目标 CID 已有多个同幂等标记广告系列")
                if matches:
                    if matches[0].get("status_raw") != target_status:
                        raise RuntimeError("同幂等标记广告系列状态与本次重建目标不一致")
                    created_campaign = matches[0]
                else:
                    response = self.target.page.rpc(
                        "/aw_cm/editing/_/rpc/BatchService/Batch",
                        portable_campaign_batch_request(
                            ocid=self.target.ocid, name=name,
                            source_campaign=snapshot["campaign"],
                            target_status=target_status,
                        ),
                    )
                    wrote_this_run = True
                    error = _service_error(response)
                    if error:
                        raise RuntimeError(f"新建广告系列失败：{error}")
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

            asset_map = state["asset_map"]
            if snapshot.get("assets") and not asset_map:
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

    @staticmethod
    def _result(
        snapshot: dict[str, Any], state_path: Path, state: dict[str, Any],
        verification: dict[str, Any], *, idempotent: bool,
    ) -> dict[str, Any]:
        return {
            "mode": "portable_campaign_rebuild",
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
            "post_read_verified": verification["ok"],
            "verification": verification,
            "rebuild_strategy": "local_portable_snapshot",
        }
