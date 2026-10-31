from __future__ import annotations

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .browser_provider import ADSPOWER
from .config import data_dir, format_cid, normalize_cid
from .image_upload import (
    ROLE_LABELS,
    asset_folder_list_request,
    asset_folder_readback_request,
    download_preview_image,
    parse_asset_folder_readback,
    upload_images,
    validate_role_images,
    verify_uploaded_asset_readback,
)
from .protocol import AccountPage, ProtocolRunner


RESPONSIVE_DISPLAY_TYPE = 35
PAUSED_AD_STATUS = 2
DISPLAY_CHANNEL = 2


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _status(value: Any, labels: dict[int, str]) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "未知"
    return labels.get(number, f"未知({number})")


def campaign_list_request(ocid: str) -> dict[str, Any]:
    return {
        "1": {"3": {"1": ocid}},
        "2": {
            "1": ["campaign_id", "name", "status", "advertising_channel_type"],
            "14": True,
        },
    }


def ad_group_list_request(ocid: str) -> dict[str, Any]:
    return {
        "1": {"3": {"1": ocid}},
        "2": {
            "1": ["campaign_id", "ad_group_id", "name", "status"],
            "14": True,
        },
    }


def ad_list_request(
    ocid: str,
    *,
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
) -> dict[str, Any]:
    filters = []
    if campaign_id:
        filters.append({"1": "campaign_id", "2": 1, "4": [{"3": str(campaign_id)}]})
    if ad_group_id:
        filters.append({"1": "ad_group_id", "2": 1, "4": [{"3": str(ad_group_id)}]})
    query: dict[str, Any] = {
        "1": [
            "campaign_id",
            "ad_group_id",
            "ad.ad_id",
            "ad.name",
            "status",
            "ad.type",
            "ad.final_urls",
            "ad.mutable_data",
        ],
        "14": True,
    }
    if filters:
        query["2"] = filters
    return {"1": {"3": {"1": ocid}}, "2": query}


def parse_campaigns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in payload.get("1") or []:
        if not isinstance(raw, dict) or not raw.get("2"):
            continue
        status_raw = int(raw.get("12") or 0)
        channel_raw = int(raw.get("15") or 0)
        rows.append({
            "campaign_id": str(raw["2"]),
            "name": str(raw.get("11") or ""),
            "status_raw": status_raw,
            "status": _status(status_raw, {3: "已启用", 5: "已暂停", 7: "已移除"}),
            "channel_raw": channel_raw,
            "channel": {2: "展示广告", 7: "应用广告"}.get(channel_raw, f"其他({channel_raw})"),
        })
    return rows


def parse_ad_groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in payload.get("1") or []:
        if not isinstance(raw, dict) or not raw.get("2") or not raw.get("3"):
            continue
        status_raw = int(raw.get("14") or 0)
        rows.append({
            "campaign_id": str(raw["2"]),
            "ad_group_id": str(raw["3"]),
            "name": str(raw.get("11") or ""),
            "status_raw": status_raw,
            "status": _status(status_raw, {1: "已启用", 2: "已暂停", 3: "已移除"}),
        })
    return rows


def _asset_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    inner = value.get("1") or {}
    text = inner.get("3") or {}
    return str(text.get("1") or "")


def _asset_rows(value: Any) -> list[dict[str, Any]]:
    return [copy.deepcopy(row) for row in (value or []) if isinstance(row, dict)]


def _asset_names(rows: list[dict[str, Any]]) -> list[str]:
    result = []
    for wrapper in rows:
        asset = wrapper.get("1") or {}
        name = str(asset.get("2") or asset.get("1") or "")
        if name:
            result.append(name)
    return result


def _asset_ids(rows: list[dict[str, Any]]) -> list[str]:
    result = []
    for wrapper in rows:
        asset = wrapper.get("1") or {}
        asset_id = str(asset.get("1") or "")
        if asset_id:
            result.append(asset_id)
    return result


def image_asset_wrapper(asset_id: str, filename: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {"1": str(asset_id), "17": 1}
    if filename:
        value["2"] = str(filename)
    return {"1": value}


def parse_ads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for raw in payload.get("1") or []:
        if not isinstance(raw, dict) or not all(raw.get(key) for key in ("2", "3", "4")):
            continue
        outer = raw.get("13") or {}
        mutable = outer.get("100") or {}
        creative = mutable.get("41") or {}
        is_responsive = isinstance(creative, dict) and bool(creative)
        status_raw = int(raw.get("5") or 0)
        landscape = _asset_rows(creative.get("1")) if is_responsive else []
        square = _asset_rows(creative.get("2")) if is_responsive else []
        portraits = _asset_rows(creative.get("20")) if is_responsive else []
        headlines = [text for text in (_asset_text(row) for row in creative.get("5") or []) if text]
        long_headline = _asset_text(creative.get("6"))
        descriptions = [text for text in (_asset_text(row) for row in creative.get("7") or []) if text]
        final_urls = [str(value) for value in (mutable.get("1") or outer.get("5") or []) if value]
        result.append({
            "campaign_id": str(raw["2"]),
            "ad_group_id": str(raw["3"]),
            "ad_id": str(raw["4"]),
            "name": str(raw.get("11") or ""),
            "status_raw": status_raw,
            "status": _status(status_raw, {1: "已启用", 2: "已暂停", 3: "已移除"}),
            "ad_type": "响应式展示广告" if is_responsive else "其他广告",
            "ad_type_raw": RESPONSIVE_DISPLAY_TYPE if is_responsive else int(outer.get("19") or 0),
            "final_urls": final_urls,
            "business_name": str(creative.get("8") or "") if is_responsive else "",
            "headlines": headlines,
            "long_headline": long_headline,
            "descriptions": descriptions,
            "landscape_assets": landscape,
            "square_assets": square,
            "portrait_assets": portraits,
            "asset_names": {
                "landscape": _asset_names(landscape),
                "square": _asset_names(square),
                "portrait": _asset_names(portraits),
            },
            "_outer": copy.deepcopy(outer),
            "_mutable": copy.deepcopy(mutable),
            "_creative": copy.deepcopy(creative),
        })
    return result


def public_ad(ad: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in ad.items() if not key.startswith("_")}


def _clean_lines(values: list[str], label: str, *, maximum: int, limit: int) -> list[str]:
    result = []
    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        if len(value) > limit:
            raise ValueError(f"{label}超过 {limit} 个字符：{value[:30]}…")
        if value not in result:
            result.append(value)
    if not result:
        raise ValueError(f"至少填写 1 条{label}")
    if len(result) > maximum:
        raise ValueError(f"{label}最多 {maximum} 条")
    return result


def normalize_creative(
    *,
    final_url: str,
    business_name: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
) -> dict[str, Any]:
    url = str(final_url).strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("最终网址必须是有效的 HTTPS 地址")
    business = str(business_name).strip()
    if not business:
        raise ValueError("商家名称不能为空")
    if len(business) > 25:
        raise ValueError("商家名称最多 25 个字符")
    long_text = str(long_headline).strip()
    if not long_text:
        raise ValueError("长标题不能为空")
    if len(long_text) > 90:
        raise ValueError("长标题最多 90 个字符")
    return {
        "final_url": url,
        "business_name": business,
        "headlines": _clean_lines(headlines, "短标题", maximum=5, limit=30),
        "long_headline": long_text,
        "descriptions": _clean_lines(descriptions, "描述", maximum=5, limit=90),
    }


def _text_asset(text: str) -> dict[str, Any]:
    # Do not carry the source text Asset ID into edited copy. Google creates or
    # resolves the text Asset while keeping image Assets explicitly associated.
    return {"1": {"3": {"1": text}, "17": 1}}


def build_create_request(
    ocid: str,
    campaign_id: str,
    ad_group_id: str,
    source_ad: dict[str, Any],
    creative_fields: dict[str, Any],
    *,
    temporary_ad_id: str = "-1",
    asset_overrides: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if source_ad.get("ad_type") != "响应式展示广告":
        raise ValueError("参考广告不是响应式展示广告")
    overrides = asset_overrides or {}
    landscape = _asset_rows(overrides.get("landscape") or source_ad.get("landscape_assets"))
    square = _asset_rows(overrides.get("square") or source_ad.get("square_assets"))
    if not landscape or not square:
        raise ValueError("参考广告必须同时包含横图和方图")
    normalized = normalize_creative(**creative_fields)
    creative = copy.deepcopy(source_ad.get("_creative") or {})
    creative["1"] = landscape
    creative["2"] = square
    creative["5"] = [_text_asset(value) for value in normalized["headlines"]]
    creative["6"] = _text_asset(normalized["long_headline"])
    creative["7"] = [_text_asset(value) for value in normalized["descriptions"]]
    creative["8"] = normalized["business_name"]
    portraits = _asset_rows(
        overrides.get("portrait") or source_ad.get("portrait_assets")
    )
    if portraits:
        creative["20"] = portraits

    mutable = copy.deepcopy(source_ad.get("_mutable") or {})
    mutable["1"] = [normalized["final_url"]]
    mutable["9"] = temporary_ad_id
    mutable["41"] = creative
    outer = {
        "3": int((source_ad.get("_outer") or {}).get("3") or 103),
        "5": [normalized["final_url"]],
        "19": RESPONSIVE_DISPLAY_TYPE,
        "25": normalized["final_url"],
        "47": temporary_ad_id,
        "100": mutable,
    }
    return {
        "1": {"2": {"1": False}, "3": {"2": str(ocid)}},
        "2": [{
            "1": 1,
            "2": ["ad.mutable_data", "status"],
            "3": {
                "1": str(ocid),
                "2": str(campaign_id),
                "3": str(ad_group_id),
                "4": temporary_ad_id,
                "5": PAUSED_AD_STATUS,
                "13": outer,
            },
        }],
    }


def build_batch_request(mutate_request: dict[str, Any]) -> dict[str, Any]:
    return {
        "2": [_compact(mutate_request)],
        "3": [{
            "1": "ads.awapps.anji.proto.infra.adgroupad.AdGroupAdService",
            "2": "Mutate",
        }],
    }


def creative_matches(
    ad: dict[str, Any],
    fields: dict[str, Any],
    expected_assets: dict[str, list[str]] | None = None,
) -> bool:
    normalized = normalize_creative(**fields)
    text_matches = (
        ad.get("status_raw") == PAUSED_AD_STATUS
        and ad.get("final_urls") == [normalized["final_url"]]
        and ad.get("business_name") == normalized["business_name"]
        and ad.get("headlines") == normalized["headlines"]
        and ad.get("long_headline") == normalized["long_headline"]
        and ad.get("descriptions") == normalized["descriptions"]
    )
    if not text_matches or expected_assets is None:
        return text_matches
    return all(
        sorted(_asset_ids(ad.get(f"{role}_assets") or []))
        == sorted(str(value) for value in asset_ids)
        for role, asset_ids in expected_assets.items()
    )


@dataclass
class AdCatalog:
    campaigns: list[dict[str, Any]]
    ad_groups: list[dict[str, Any]]
    ads: list[dict[str, Any]]

    def public(self) -> dict[str, Any]:
        return {
            "campaigns": copy.deepcopy(self.campaigns),
            "ad_groups": copy.deepcopy(self.ad_groups),
            "ads": [public_ad(ad) for ad in self.ads],
        }


def display_ad_hierarchy(catalog: AdCatalog) -> list[dict[str, Any]]:
    """Return Display campaigns with their groups and every responsive Display ad."""
    hierarchy: list[dict[str, Any]] = []
    campaigns = [
        row for row in catalog.campaigns
        if row.get("channel_raw") == DISPLAY_CHANNEL and row.get("status_raw") != 7
    ]
    for campaign in campaigns:
        groups = [
            row for row in catalog.ad_groups
            if row.get("campaign_id") == campaign.get("campaign_id")
            and row.get("status_raw") != 3
        ]
        group_nodes = []
        for group in groups:
            ads = [
                row for row in catalog.ads
                if row.get("ad_group_id") == group.get("ad_group_id")
                and row.get("ad_type") == "响应式展示广告"
                and row.get("status_raw") != 3
            ]
            group_nodes.append({"ad_group": group, "ads": ads})
        hierarchy.append({"campaign": campaign, "ad_groups": group_nodes})
    return hierarchy


class AdManager:
    """Protocol-only responsive Display ad reader and paused-copy creator."""

    def __init__(
        self,
        profile_id: str,
        cid: str,
        log: Callable[[str], None] | None = None,
        *,
        browser_type: str = ADSPOWER,
    ):
        self.profile_id = profile_id
        self.cid = normalize_cid(cid)
        self.log = log or (lambda _text: None)
        self.runner = ProtocolRunner(profile_id, self.log, browser_type=browser_type)
        accounts = self.runner.discover_accounts({self.cid})
        self.accounts = accounts
        account = accounts.get(self.cid)
        if not account:
            self.runner.close()
            raise RuntimeError(f"CID {format_cid(self.cid)} 不在当前窗口的可访问账户范围内")
        if account.is_manager:
            self.runner.close()
            raise ValueError("MCC 不能直接创建广告，请选择普通 CID")
        self.account: AccountPage = account

    def _manager_ocid(self) -> str:
        query = parse_qs(urlsplit(self.account.page.url).query)
        uscid = str((query.get("uscid") or [""])[0])
        if uscid.isdigit():
            return uscid
        for parent_cid in self.account.parent_mcc_cids:
            parent = self.accounts.get(parent_cid)
            if parent and parent.ocid.isdigit():
                return parent.ocid
        return self.account.ocid

    def close(self) -> None:
        self.runner.close()

    def __enter__(self) -> "AdManager":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _campaigns(self) -> list[dict[str, Any]]:
        response = self.account.page.rpc(
            "/aw/_/rpc/CampaignService/List",
            campaign_list_request(self.account.ocid),
        )
        return parse_campaigns(response)

    def _ad_groups(self) -> list[dict[str, Any]]:
        response = self.account.page.rpc(
            "/aw/_/rpc/AdGroupService/List",
            ad_group_list_request(self.account.ocid),
        )
        return parse_ad_groups(response)

    def _ads(self, campaign_id: str | None = None, ad_group_id: str | None = None) -> list[dict[str, Any]]:
        response = self.account.page.rpc(
            "/aw/_/rpc/AdGroupAdService/List",
            ad_list_request(self.account.ocid, campaign_id=campaign_id, ad_group_id=ad_group_id),
        )
        return parse_ads(response)

    def _readback_uploaded_asset(self, upload_result: Any) -> dict[str, Any]:
        latest_rows: list[dict[str, Any]] = []
        last_error = "尚未读到素材"
        for attempt in range(7):
            if attempt:
                time.sleep(0.7 * attempt)
            try:
                response = self.account.page.rpc(
                    "/aw_cm/editing/_/rpc/AssetFolderService/List",
                    asset_folder_readback_request(
                        self.account.ocid, self._manager_ocid(), upload_result.asset_id,
                    ),
                )
            except RuntimeError as exc:
                last_error = str(exc)
                continue
            latest_rows = parse_asset_folder_readback(
                response, account_id=self.account.ocid,
            )
            try:
                verified = verify_uploaded_asset_readback(upload_result, latest_rows)
                self.log(
                    f"[{format_cid(self.cid)}] Asset {upload_result.asset_id} "
                    "已通过素材库回读校验"
                )
                return verified
            except RuntimeError as exc:
                last_error = str(exc)
                continue
        raise RuntimeError(
            f"Asset {upload_result.asset_id} 上传后未能从目标 CID 素材库完整回读；"
            f"最后读到 {len(latest_rows)} 行；{last_error}"
        )

    def image_assets(self, *, max_pages: int = 200) -> dict[str, Any]:
        """Read the complete image Asset catalog using verified 50-row pages."""
        page_size = 50
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page_index in range(max_pages):
            offset = page_index * page_size
            response = self.account.page.rpc(
                "/aw_cm/editing/_/rpc/AssetFolderService/List",
                asset_folder_list_request(
                    self.account.ocid, self._manager_ocid(),
                    offset=offset, limit=page_size,
                ),
            )
            page_rows = parse_asset_folder_readback(
                response, account_id=self.account.ocid,
            )
            new_rows = [row for row in page_rows if row["asset_id"] not in seen]
            if page_rows and not new_rows:
                raise RuntimeError(
                    f"素材分页 offset={offset} 没有产生新 Asset ID，协议可能忽略了分页字段"
                )
            for row in new_rows:
                seen.add(row["asset_id"])
                rows.append(row)
            self.log(
                f"[{format_cid(self.cid)}] 素材分页 {page_index + 1}："
                f"读取 {len(page_rows)} 条，累计 {len(rows)} 条"
            )
            if len(page_rows) < page_size:
                return {
                    "profile_id": self.profile_id,
                    "cid": self.cid,
                    "ocid": self.account.ocid,
                    "rows": rows,
                    "total": len(rows),
                    "protocol_pages": page_index + 1,
                    "protocol_page_size": page_size,
                    "complete": True,
                }
        raise RuntimeError(f"素材超过 {max_pages * page_size} 条，已停止继续分页以避免无限查询")

    @staticmethod
    def _preview_extension(mime_type: str, content: bytes) -> str:
        mime = str(mime_type or "").lower()
        if content.startswith(b"\x89PNG\r\n\x1a\n") or mime == "image/png":
            return ".png"
        if content.startswith(b"\xff\xd8\xff") or mime in {"image/jpeg", "image/jpg"}:
            return ".jpg"
        if content.startswith((b"GIF87a", b"GIF89a")) or mime == "image/gif":
            return ".gif"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP" or mime == "image/webp":
            return ".webp"
        raise ValueError(f"Google 图片格式暂不支持：{mime_type or '未知'}")

    def reference_images(
        self, ad: dict[str, Any], *, cache_root: Path | None = None,
        downloader: Callable[[Any | None, str], tuple[bytes, str, str]] = download_preview_image,
    ) -> list[dict[str, Any]]:
        """Resolve and cache every image used by one responsive Display ad."""
        if str(ad.get("ad_type") or "") != "响应式展示广告":
            raise ValueError("只有响应式展示广告可以读取参考图片")
        ad_id = str(ad.get("ad_id") or "")
        if not ad_id:
            raise ValueError("参考广告缺少广告 ID")
        target = (cache_root or (data_dir() / "reference-images")) / self.cid / ad_id
        target.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for role in ("landscape", "square", "portrait"):
            for wrapper in ad.get(f"{role}_assets") or []:
                asset = wrapper.get("1") or {}
                asset_id = str(asset.get("1") or "")
                if not asset_id.isdigit() or asset_id in seen:
                    continue
                seen.add(asset_id)
                name = str(asset.get("2") or asset_id)
                row: dict[str, Any] = {
                    "role": role, "role_label": ROLE_LABELS[role],
                    "asset_id": asset_id, "name": name, "local_path": "",
                    "preview_url": "", "width": 0, "height": 0,
                    "cache_hit": False, "error": "",
                }
                try:
                    response = self.account.page.rpc(
                        "/aw_cm/editing/_/rpc/AssetFolderService/List",
                        asset_folder_readback_request(
                            self.account.ocid, self._manager_ocid(), asset_id,
                        ),
                        timeout=90,
                    )
                    matches = [
                        item for item in parse_asset_folder_readback(
                            response, account_id=self.account.ocid,
                        )
                        if item.get("asset_id") == asset_id
                    ]
                    if len(matches) != 1:
                        raise RuntimeError(f"Asset {asset_id} 素材回读数量不是 1")
                    metadata = matches[0]
                    row.update({
                        "name": str(metadata.get("name") or name),
                        "preview_url": str(metadata.get("preview_url") or ""),
                        "width": int(metadata.get("width") or 0),
                        "height": int(metadata.get("height") or 0),
                        "file_size": int(metadata.get("file_size") or 0),
                    })
                    if not row["preview_url"]:
                        raise RuntimeError(f"Asset {asset_id} 没有预览地址")
                    cached = next((
                        path for path in target.glob(f"{asset_id}.*")
                        if path.is_file() and path.stat().st_size > 0
                    ), None)
                    if cached is not None:
                        row["local_path"] = str(cached)
                        row["cache_hit"] = True
                except Exception as exc:
                    row["error"] = str(exc)
                rows.append(row)

        pending = [row for row in rows if row["preview_url"] and not row["local_path"]]

        def download(row: dict[str, Any]) -> dict[str, str]:
            content, mime_type, final_url = downloader(
                None, str(row["preview_url"]),
            )
            extension = self._preview_extension(mime_type, content)
            cached = target / f"{row['asset_id']}{extension}"
            cached.write_bytes(content)
            return {
                "local_path": str(cached), "mime_type": mime_type,
                "download_url": final_url,
            }

        if pending:
            with ThreadPoolExecutor(max_workers=min(4, len(pending))) as executor:
                futures = {executor.submit(download, row): row for row in pending}
                for future in as_completed(futures):
                    row = futures[future]
                    try:
                        row.update(future.result())
                    except Exception as exc:
                        row["error"] = str(exc)

        for row in rows:
            if row["local_path"]:
                self.log(
                    f"[{format_cid(self.cid)}] {row['role_label']} Asset {row['asset_id']} "
                    f"{'命中缓存' if row['cache_hit'] else '已下载'}"
                )
            else:
                self.log(
                    f"[{format_cid(self.cid)}] {row['role_label']} Asset {row['asset_id']} "
                    f"图片读取失败：{row['error'] or '未知原因'}"
                )
        self.log(
            f"[{format_cid(self.cid)}] 参考广告 {ad_id} 图片完成："
            f"成功 {sum(bool(row['local_path']) for row in rows)} / 共 {len(rows)} 张"
        )
        return rows

    def upload_image_plan(
        self, image_paths: dict[str, list[str]],
    ) -> dict[str, Any]:
        """Upload a local image plan and verify every returned Asset ID."""
        if self.account.status != "可用":
            raise PermissionError(
                f"CID {format_cid(self.cid)} 当前状态为“{self.account.status}”，禁止上传素材："
                f"{self.account.status_detail}"
            )
        balance = self.runner.require_positive_account_balance(
            self.account, action="上传图片素材",
        )
        validated = validate_role_images(image_paths)
        flat = [
            (role, item)
            for role in ("landscape", "square", "portrait")
            for item in validated[role]
        ]
        if not flat:
            raise ValueError("图片方案中没有可上传的图片")
        self.log(f"[{format_cid(self.cid)}] 开始上传图片方案，共 {len(flat)} 张")
        results = upload_images(
            self.account.page,
            [item.path for _role, item in flat],
            account_id=self.account.ocid,
            manager_id=self._manager_ocid(),
        )
        rows: list[dict[str, Any]] = []
        for index, result in enumerate(results):
            role, _item = flat[index]
            row = {"role": role, "role_label": ROLE_LABELS[role], **result.to_dict()}
            rows.append(row)
            if not result.ok:
                uploaded = [item["asset_id"] for item in rows if item["ok"]]
                note = f"；已成功的 Asset ID：{', '.join(uploaded)}" if uploaded else ""
                raise RuntimeError(
                    f"{ROLE_LABELS[role]} {result.filename} 上传失败："
                    f"{result.error_code} / {result.error}{note}"
                )
            row["readback"] = self._readback_uploaded_asset(result)
        if len(results) != len(flat):
            raise RuntimeError("图片上传队列未完整执行")
        return {
            "mode": "creative_asset_upload",
            "write_performed": True,
            "success": True,
            "profile_id": self.profile_id,
            "cid": self.cid,
            "ocid": self.account.ocid,
            "target_balance": balance,
            "uploaded_images": rows,
            "asset_ids": [row["asset_id"] for row in rows],
            "post_read_verified": all((row.get("readback") or {}).get("verified") for row in rows),
        }

    def catalog(self) -> AdCatalog:
        campaigns = self._campaigns()
        groups = self._ad_groups()
        ads = self._ads()
        self.log(
            f"[{format_cid(self.cid)}] 已读取 {len(campaigns)} 个广告系列、"
            f"{len(groups)} 个广告组、{len(ads)} 个广告（只读）"
        )
        return AdCatalog(campaigns, groups, ads)

    @staticmethod
    def _find(rows: list[dict[str, Any]], key: str, value: str, label: str) -> dict[str, Any]:
        matches = [row for row in rows if str(row.get(key)) == str(value)]
        if len(matches) != 1:
            raise ValueError(f"未能唯一定位{label} {value}")
        return matches[0]

    def preflight(
        self,
        catalog: AdCatalog,
        *,
        source_ad_id: str,
        target_campaign_id: str,
        target_ad_group_id: str,
        creative_fields: dict[str, Any],
        image_paths: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        source = self._find(catalog.ads, "ad_id", source_ad_id, "参考广告")
        campaign = self._find(catalog.campaigns, "campaign_id", target_campaign_id, "目标广告系列")
        group = self._find(catalog.ad_groups, "ad_group_id", target_ad_group_id, "目标广告组")
        if campaign.get("channel_raw") != DISPLAY_CHANNEL:
            raise ValueError("目标广告系列不是展示广告系列")
        if campaign.get("status_raw") == 7 or group.get("status_raw") == 3:
            raise ValueError("目标广告系列或广告组已移除")
        if group.get("campaign_id") != campaign.get("campaign_id"):
            raise ValueError("目标广告组不属于所选广告系列")
        request = build_create_request(
            self.account.ocid,
            target_campaign_id,
            target_ad_group_id,
            source,
            creative_fields,
        )
        local = validate_role_images(image_paths or {})
        operation = request["2"][0]["3"]
        return {
            "mode": "preflight",
            "write_performed": False,
            "profile_id": self.profile_id,
            "cid": self.cid,
            "ocid": self.account.ocid,
            "source": public_ad(source),
            "target": {
                "campaign_id": target_campaign_id,
                "campaign_name": campaign.get("name"),
                "campaign_status": campaign.get("status"),
                "ad_group_id": target_ad_group_id,
                "ad_group_name": group.get("name"),
                "ad_group_status": group.get("status"),
            },
            "creative": normalize_creative(**creative_fields),
            "assets": {
                role: {
                    "source_count": len(source.get(f"{role}_assets") or []),
                    "local_upload_count": len(local[role]),
                    "effective_source": "本地上传" if local[role] else "参考广告",
                    "local_files": [item.to_dict() for item in local[role]],
                }
                for role in ("landscape", "square", "portrait")
            },
            "protocol": {
                "path": "/aw_cm/editing/_/rpc/BatchService/Batch",
                "inner_service": "AdGroupAdService/Mutate",
                "operation": "CREATE",
                "update_mask": request["2"][0]["2"],
                "status": "PAUSED",
                "status_enum": operation["5"],
                "temporary_ad_id": operation["4"],
                "image_upload": (
                    "正式创建时先上传并取得 Asset ID；预检不上传"
                    if any(local.values()) else "不上传，沿用参考广告 Asset ID"
                ),
            },
        }

    def create_paused(
        self,
        catalog: AdCatalog,
        *,
        source_ad_id: str,
        target_campaign_id: str,
        target_ad_group_id: str,
        creative_fields: dict[str, Any],
        image_paths: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        if self.account.status != "可用":
            raise PermissionError(
                f"CID {format_cid(self.cid)} 当前状态为“{self.account.status}”，禁止创建广告："
                f"{self.account.status_detail}"
            )
        balance = self.runner.require_positive_account_balance(
            self.account, action="创建广告",
        )
        plan = self.preflight(
            catalog,
            source_ad_id=source_ad_id,
            target_campaign_id=target_campaign_id,
            target_ad_group_id=target_ad_group_id,
            creative_fields=creative_fields,
            image_paths=image_paths,
        )
        source = self._find(catalog.ads, "ad_id", source_ad_id, "参考广告")
        validated = validate_role_images(image_paths or {})
        flat = [
            (role, item)
            for role in ("landscape", "square", "portrait")
            for item in validated[role]
        ]
        upload_rows: list[dict[str, Any]] = []
        asset_overrides: dict[str, list[dict[str, Any]]] = {}
        if flat:
            self.log(
                f"[{format_cid(self.cid)}] 开始上传 {len(flat)} 张本地图片到目标 CID 素材库"
            )
            upload_results = upload_images(
                self.account.page,
                [item.path for _role, item in flat],
                account_id=self.account.ocid,
                manager_id=self._manager_ocid(),
            )
            for index, result in enumerate(upload_results):
                role, _item = flat[index]
                row = {"role": role, "role_label": ROLE_LABELS[role], **result.to_dict()}
                upload_rows.append(row)
                if result.ok:
                    row["readback"] = self._readback_uploaded_asset(result)
                    asset_overrides.setdefault(role, []).append(
                        image_asset_wrapper(result.asset_id, result.filename)
                    )
                    self.log(
                        f"[{format_cid(self.cid)}] {ROLE_LABELS[role]}上传成功："
                        f"{result.filename} → Asset {result.asset_id}"
                    )
            failed = next((row for row in upload_rows if not row["ok"]), None)
            if failed:
                uploaded_ids = [row["asset_id"] for row in upload_rows if row["ok"]]
                orphan_note = (
                    f"；此前已上传的 Asset ID：{', '.join(uploaded_ids)}（仍保留在目标素材库）"
                    if uploaded_ids else ""
                )
                raise RuntimeError(
                    f"{failed['role_label']} {failed['filename']} 上传失败："
                    f"{failed['error_code']} / {failed['error']}{orphan_note}"
                )
            if len(upload_results) != len(flat):
                raise RuntimeError("图片上传队列未完整执行")
        uploaded_note = ""
        if upload_rows:
            uploaded_note = (
                "；本次已上传的 Asset ID："
                + ", ".join(row["asset_id"] for row in upload_rows)
                + "（素材仍保留在目标 CID 素材库）"
            )
        try:
            before = self._ads(target_campaign_id, target_ad_group_id)
        except Exception as exc:
            raise RuntimeError(f"创建前读取目标广告组失败：{exc}{uploaded_note}") from exc
        before_ids = {ad["ad_id"] for ad in before}
        mutate = build_create_request(
            self.account.ocid,
            target_campaign_id,
            target_ad_group_id,
            source,
            creative_fields,
            asset_overrides=asset_overrides,
        )
        expected_assets = {
            role: _asset_ids(asset_overrides.get(role) or source.get(f"{role}_assets") or [])
            for role in ("landscape", "square", "portrait")
        }
        self.log(
            f"[{format_cid(self.cid)}] 创建暂停广告：{plan['target']['campaign_name']} / "
            f"{plan['target']['ad_group_name']}"
        )
        try:
            response = self.account.page.rpc(
                "/aw_cm/editing/_/rpc/BatchService/Batch",
                build_batch_request(mutate),
            )
        except Exception as exc:
            raise RuntimeError(f"暂停广告创建协议失败：{exc}{uploaded_note}") from exc
        latest: list[dict[str, Any]] = []
        for attempt in range(7):
            if attempt:
                time.sleep(0.8 * attempt)
            try:
                latest = self._ads(target_campaign_id, target_ad_group_id)
            except Exception as exc:
                raise RuntimeError(f"创建后回读广告失败：{exc}{uploaded_note}") from exc
            created = [
                ad for ad in latest
                if ad["ad_id"] not in before_ids
                and creative_matches(ad, creative_fields, expected_assets)
            ]
            if len(created) == 1:
                item = created[0]
                self.log(f"[{format_cid(self.cid)}] 创建成功并回读校验：广告 {item['ad_id']}（已暂停）")
                return {
                    **plan,
                    "mode": "ad_create",
                    "write_performed": True,
                    "success": True,
                    "created_ad": public_ad(item),
                    "response_received": isinstance(response, dict),
                    "post_read_verified": True,
                    "target_balance": balance,
                    "uploaded_images": upload_rows,
                    "verified_asset_ids": expected_assets,
                }
            if len(created) > 1:
                raise RuntimeError(f"写后发现多个相同的新广告，无法唯一校验{uploaded_note}")
        new_ids = sorted(ad["ad_id"] for ad in latest if ad["ad_id"] not in before_ids)
        raise RuntimeError(
            "Google Ads 已返回协议响应，但写后未找到内容完全匹配且状态为暂停的新广告。"
            + (f" 新出现的广告 ID：{', '.join(new_ids)}" if new_ids else " 没有出现新广告 ID。")
            + uploaded_note
        )
