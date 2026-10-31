"""Sanitized protocol contracts synchronized from the sibling ggadscode workspace.

Only non-secret contract metadata is copied here.  Captured account identifiers,
cookies, XSRF values and replay bodies deliberately stay out of this project and
out of the packaged executable.
"""

from __future__ import annotations

from dataclasses import dataclass


SOURCE_MANIFEST = "ggadscode/protocols/v1_display/manifest.json"
SOURCE_MANIFEST_VERSION = 1
SOURCE_MANIFEST_LAST_MODIFIED = "2026-08-09T14:53:53+08:00"
SOURCE_MANIFEST_SHA256 = (
    "770f48b45ad11dc9880f05ff8816bf6c7f61d053eb950978ac2292b202306e4a"
)
SOURCE_PROTOCOL_TREE_FILE_COUNT = 46
SOURCE_PROTOCOL_TREE_SHA256 = (
    "38bebc3b0101d2386bd1e75d1272f81b7614c5b27382d89291db3591ea78ad6f"
)
SOURCE_EVIDENCE_SNAPSHOT_FILE_COUNT = 352
SOURCE_EVIDENCE_SNAPSHOT_SHA256 = (
    "925c0fe2d31bf59ff32b57080a6484e7b5bb16219ae1d30928c73b688c3875e8"
)
SOURCE_WORKTREE_SYNCED_AT = "2026-08-14T11:15:58+08:00"
SOURCE_WORKTREE_FINGERPRINTS = {
    "cap_stream.py": "0e644f1b348fe66aa8c9fd04da8fa0d9da545c1cfeb94d7aeb67f5f9d8d8a71e",
    "ggadscode/core.py": "9d4fe41f69ee94901e2b71f83e97902291569775f30f077192228aba971b2525",
    "ggadscode/ops.py": "9f6091f2f0ef6f49f31e89252ab9af46a4311793d8a63ed2f1e63044c67a1b14",
    "ggadscode/capture.py": "b24697c2bf58a2cf16129374fff468b3c82b2b6d6647cc9cf5402d24c6bfde59",
    "ggadscode/rpc_capture.py": "e62a13eba7fb554e906c72f50b99235c5eb9670a69385ae7c95f6f20647ea0e4",
    "ggadscode/campaign_readback.py": (
        "80fbe207c0f4f8d3ef2384cc57c32eebe226dfbfac3ef98a5f829c74811ca432"
    ),
    "ggadscode/customer.py": "7df2b1473e14aa0815cb0f697ba7e4e0d40f7c0a896c09bf55d7cb3233da055a",
    "ggadscode/hierarchy_readback.py": "4ebbbcfb2a1be56b2524cfae1d12e26145aac37e25e6b63df1252a1e332eb460",
    "ggadscode/asset_readback.py": "c0eadf8295a406b64785ca534e284e81fe9952352e4be5a32aa25f6f550ad0bf",
    "ggadscode/targeting_readback.py": "1d5df3485bd6fad50f80fd5bfddab76cc36f8dd3f8505e7391b66a8d28410c22",
    "ggadscode/reporting.py": "18f7947fb2462aeb80a0e92a2344ea1c59185a67d67092b9a97f35476c656704",
    "ggadscode/recommendation_readback.py": "293ae9ea5e8494291d49562ed579819f5433368b70d96458458e78ad99ea352a",
    "ggadscode/catalog_audit.py": "e3a9eaccde9d25406b2b08b98de0493b8227a09c3ae063b9b5265d162db9e139",
    "ggadscode/cdp_page.py": "9c81a89bab9f2f47057818efbd672b2998e03111f8cb569be0e6211cee893ec0",
    "ggadscode/auth.py": "36f3aed945fe06cb1d55145cb9c90e14e12d59c707d9467d49424cc9e6b09591",
    "ggadscode/v1_display.py": "05e069dc1bcf88920fc9ddaf5cd28788d74fa0bb8d05951c05cab6ba10d829f7",
}
SOURCE_EVIDENCE_TEST_FINGERPRINTS = {
    "tests/test_cap_stream.py": "2fc6f5b481f7ef604ca315fde53f370e855cd3052c98d882bb3a7056cd27f536",
    "tests/test_capture.py": "9974ac91c968df6dcca5459be8d0b118b333a4f621f55ccce547c4bcee6359c2",
    "tests/test_rpc_capture.py": "12f6b4997ae962f2545d74cc381347a788fdd373d73797e79e4b727cae864d5a",
    "tests/test_v1_display_protocol_manifest.py": "738113296825824b3731a4f399c7fbf1723486403ea71855e0bf634cee30a0d1",
    "tests/test_campaign_readback.py": "e714c68b09163a9ec74676bbdbdd3645a2917eefd2b15a8f473a45bb25a81fb4",
    "tests/test_customer_protocol.py": "196d5a480d657f87ce141fb333c999af0c4eac0e5b1ada3d35c29758edf29377",
    "tests/test_hierarchy_readback.py": "9cedc647f38dcc8d3365201df33acbb1100130d20b6658f7c7425191173fa00a",
    "tests/test_asset_readback.py": "47ec3721ad8d357cb88f0dc4e1a72a1f1a13361886565c4ed259916e3efb1bbc",
    "tests/test_targeting_readback.py": "f760e59d0faa84c91b81ecb92c5e55bc154d447d38ecc1951c495960de15bcac",
    "tests/test_reporting.py": "7a195b9e323c0601c6b97a9e19397f1146a3b91bd25225b2ed7efe5ab40b79d4",
    "tests/test_recommendation_readback.py": "dab4788b5c069a30e4866498fba52929f77432ba475e38d4a176b7680b3b02a5",
    "tests/test_catalog_audit.py": "08a26211326b41ffcb388b84f4cf80b1088276788c034dfd19135da7a5302e24",
    "tests/test_core_dynamic_status.py": "09a9d4307ad50a9e20d3aca3878292af50a1ae281ae697e65dee078807997941",
    "tests/test_core_connlayer.py": "eeeed267fa7634d3510739921902fc3cc28193ae0e2617ce1551152140e097f9",
    "tests/test_ops.py": "ef1ad3e6f8755801098a936a989b9d0bc3291101ed6878cd2cf831172aa61451",
}


# Campaign and child-resource status values are different protocol enums.
# These values are backed by the normalized campaign fixture, the live pause
# fixture and a 2026-08-13 read-only CampaignService/List verification.
CAMPAIGN_STATUS_ENABLED = 3
CAMPAIGN_STATUS_PAUSED = 5
CAMPAIGN_STATUS_REMOVED = 7
CAMPAIGN_STATUS_LABELS = {
    CAMPAIGN_STATUS_ENABLED: "已启用",
    CAMPAIGN_STATUS_PAUSED: "已暂停",
    CAMPAIGN_STATUS_REMOVED: "已移除",
}

CHILD_STATUS_ENABLED = 1
CHILD_STATUS_PAUSED = 2
CHILD_STATUS_REMOVED = 3
CHILD_STATUS_LABELS = {
    CHILD_STATUS_ENABLED: "已启用",
    CHILD_STATUS_PAUSED: "已暂停",
    CHILD_STATUS_REMOVED: "已移除",
}


# Frequently used endpoints.  Read paths come from the current readback code;
# write paths are only exposed when the source has an observed contract.
CLIENT_CUSTOMER_LIST_PATH = "/aw/_/rpc/ClientCustomerService/List"
CAMPAIGN_LIST_PATH = "/aw/_/rpc/CampaignService/List"
CAMPAIGN_STATUS_MUTATE_PATH = "/aw_cm/editing/_/rpc/CampaignService/Mutate"
CAMPAIGN_CREATE_PATH = "/aw/_/rpc/CampaignService/Mutate"
CAMPAIGN_MODIFY_BATCH_PATH = "/aw_essentials/_/rpc/BatchService/Batch"
CAMPAIGN_DRAFT_PAUSE_BATCH_PATH = "/aw_cm/editing/_/rpc/BatchService/Batch"
AD_GROUP_LIST_PATH = "/aw/_/rpc/AdGroupService/List"
AD_GROUP_CREATE_PATH = "/aw/_/rpc/AdGroupService/Mutate"
AD_GROUP_MUTATE_PATH = "/aw_essentials/_/rpc/AdGroupService/Mutate"
AD_GROUP_AD_LIST_PATH = "/aw/_/rpc/AdGroupAdService/List"
AD_GROUP_AD_MUTATE_PATH = "/aw_cm/editing/_/rpc/AdGroupAdService/Mutate"
EDITING_BATCH_PATH = "/aw_cm/editing/_/rpc/BatchService/Batch"
CHANNEL_ASSET_LIST_PATH = "/aw_cm/_/rpc/ChannelAssetService/List"
ASSET_FOLDER_READBACK_PATH = "/aw_cm/editing/_/rpc/AssetFolderService/List"
CRITERIA_LIST_PATH = "/aw_dva/_/rpc/NegativeTargetedContentService/List"
CUSTOM_AUDIENCE_INITIAL_DATA_PATH = "/aw_displayads/audiencecenter/InitialData"
TOPIC_CONSTANT_INITIAL_DATA_PATH = "/aw_dva/InitialData"
RECOMMENDATION_INITIAL_DATA_PATH = "/aw_prime/opportunities/InitialData"

# The general status-only Campaign Mutate route is present in the current
# ggadscode worktree and its request shape is unit-tested there.  It is newer
# than the 2026-08-09 manifest, whose campaign.pause row is intentionally
# limited to the separately captured draft-backed Batch flow.
WORKTREE_PROTOCOLS = {
    "campaign.status": {
        "status": "worktree_contract_verified",
        "service": "CampaignService",
        "method": "Mutate",
        "request_path": CAMPAIGN_STATUS_MUTATE_PATH,
        "update_mask": ("status",),
        "status_enums": (
            CAMPAIGN_STATUS_ENABLED,
            CAMPAIGN_STATUS_PAUSED,
            CAMPAIGN_STATUS_REMOVED,
        ),
        "source": "ggadscode/core.py::mutate_campaign_status",
        "safety_gates": (
            "native integer status",
            "canonical numeric account and campaign IDs",
            "target-account-bound browser page",
            "strict transport and response JSON",
            "caller write-after-readback verification",
        ),
    },
}


@dataclass(frozen=True, slots=True)
class ProtocolContract:
    key: str
    status: str
    service: str = ""
    method: str = ""
    request_path: str = ""
    scope: str = ""

    @property
    def is_live_read(self) -> bool:
        return self.status == "live_read_verified"

    @property
    def is_live_write(self) -> bool:
        return self.status == "live_write_verified"

    @property
    def is_blocked(self) -> bool:
        return self.status == "blocked_write"


def _contract(
    key: str,
    status: str,
    service: str = "",
    method: str = "",
    request_path: str = "",
    scope: str = "",
) -> ProtocolContract:
    return ProtocolContract(key, status, service, method, request_path, scope)


# Sanitized 33-entry projection of the latest V1 Display manifest.  Empty paths
# mean the manifest has evidence/fixtures but does not publish one replayable
# endpoint; callers must not invent an endpoint from the service name.
PROTOCOLS = {
    item.key: item
    for item in (
        _contract("customer.list", "normalized_account_lock", "ClientCustomerService", "List"),
        _contract("campaign.list", "live_read_verified", "CampaignService", "List", CAMPAIGN_LIST_PATH),
        _contract("campaign_budget.list", "live_read_verified", "CampaignService", "List", CAMPAIGN_LIST_PATH),
        _contract("campaign.create", "live_write_verified", "CampaignService", "Mutate", CAMPAIGN_CREATE_PATH, "create paused"),
        _contract("campaign.modify", "live_write_verified", "BatchService", "Batch", CAMPAIGN_MODIFY_BATCH_PATH),
        _contract("campaign.pause", "live_write_verified", "BatchService", "Batch", CAMPAIGN_DRAFT_PAUSE_BATCH_PATH, "draft-backed campaign only"),
        _contract("campaign.remove", "live_write_verified", "BatchService", "Batch"),
        _contract("ad_group.list", "live_read_verified", "AdGroupService", "List", AD_GROUP_LIST_PATH),
        _contract("ad_group.create", "live_write_verified", "AdGroupService", "Mutate", AD_GROUP_CREATE_PATH, "create paused"),
        _contract("ad_group.modify", "live_write_verified", "AdGroupService", "Mutate", AD_GROUP_MUTATE_PATH),
        _contract("ad_group.pause", "live_write_verified", "AdGroupService", "Mutate", AD_GROUP_MUTATE_PATH),
        _contract("ad_group.remove", "live_write_verified", "BatchService", "Batch"),
        _contract("ad_group_ad.list", "live_read_verified", "AdGroupAdService", "List", AD_GROUP_AD_LIST_PATH),
        _contract("ad_group_ad.create", "live_write_verified", "BatchService", "Batch", EDITING_BATCH_PATH, "create paused"),
        _contract("ad_group_ad.modify", "live_write_verified", "BatchService", "Batch", EDITING_BATCH_PATH),
        _contract("ad_group_ad.pause", "live_write_verified", "AdGroupAdService", "Mutate", AD_GROUP_AD_MUTATE_PATH),
        _contract("ad_group_ad.remove", "live_write_verified", "BatchService", "Batch", EDITING_BATCH_PATH),
        _contract("asset.list", "live_read_verified", "ChannelAssetService", "List", CHANNEL_ASSET_LIST_PATH),
        _contract("asset_association.list", "live_read_verified", "BatchService", "Batch"),
        _contract("asset.upload", "live_write_verified", scope="signed-in Google resumable upload; AssetFolder readback required"),
        _contract("asset_association.modify", "live_write_verified", "BatchService", "Batch", EDITING_BATCH_PATH),
        _contract("criteria.list", "live_read_verified", "NegativeTargetedContentService", "List", CRITERIA_LIST_PATH),
        _contract("custom_audience.list", "live_read_verified", request_path=CUSTOM_AUDIENCE_INITIAL_DATA_PATH),
        _contract("topic_constant.list", "live_read_verified", request_path=TOPIC_CONSTANT_INITIAL_DATA_PATH),
        _contract("placement.website_exclusion", "live_write_verified", scope="CampaignCriterion or AdGroupCriterion; negative placement"),
        _contract("placement.app_exclusion", "live_write_verified", scope="type 22 app or type 23 app category"),
        _contract("custom_audience.interests", "live_write_verified", scope="custom audience type 1; members replace"),
        _contract("custom_audience.search_terms", "live_write_verified", scope="custom audience type 4; members replace"),
        _contract("ad_group.topics", "live_write_verified", scope="AdGroupCriterion type 42"),
        _contract("reporting.campaign_daily", "live_read_verified", "CampaignService", "List", CAMPAIGN_LIST_PATH, "explicit date range required"),
        _contract("recommendation.list", "live_read_verified", request_path=RECOMMENDATION_INITIAL_DATA_PATH),
        _contract("recommendation.apply", "blocked_write", scope="no executable internal endpoint"),
        _contract("recommendation.dismiss", "live_write_verified", "SuggestionService", "Dismiss"),
        _contract(
            "shared_set.negative_keywords", "live_write_verified",
            "SharedSetService", "Mutate", "/aw_cm/_/rpc/BatchService/Batch",
            "type=1; paired with SharedCriterionService.Mutate in same batch",
        ),
        _contract(
            "shared_set.placement_exclusion", "live_write_verified",
            "SharedSetService", "Mutate", "/aw_dva/_/rpc/BatchService/Batch",
            "type=17; paired with SharedCriterionService.Mutate in same batch",
        ),
        _contract(
            "campaign_shared_set.association", "live_write_verified",
            "CampaignSharedSetService", "Mutate",
            scope="CREATE/REMOVE; namespace varies by shared set subtype (aw_cm vs aw_dva)",
        ),
        _contract(
            "ad_group_criterion.audience_association", "live_write_verified",
            "AdGroupCriterionService", "Mutate", "/aw_dva/_/rpc/AdGroupCriterionService/Mutate",
            "UserInterest field 50 or CustomAudience field 108; read via AudienceService.List",
        ),
    )
}


def protocol_contract(key: str) -> ProtocolContract:
    try:
        return PROTOCOLS[key]
    except KeyError as exc:
        raise KeyError(f"未知协议目录项：{key}") from exc


def require_live_write(key: str) -> ProtocolContract:
    """Return one writable contract, failing closed for blocked/unknown entries."""
    contract = protocol_contract(key)
    if not contract.is_live_write:
        raise PermissionError(f"协议 {key} 当前状态为 {contract.status}，禁止写入")
    return contract


def protocol_sync_summary() -> dict[str, object]:
    counts: dict[str, int] = {}
    for contract in PROTOCOLS.values():
        counts[contract.status] = counts.get(contract.status, 0) + 1
    return {
        "source": SOURCE_MANIFEST,
        "manifest_version": SOURCE_MANIFEST_VERSION,
        "manifest_sha256": SOURCE_MANIFEST_SHA256,
        "evidence_snapshot_file_count": SOURCE_EVIDENCE_SNAPSHOT_FILE_COUNT,
        "evidence_snapshot_sha256": SOURCE_EVIDENCE_SNAPSHOT_SHA256,
        "source_worktree_synced_at": SOURCE_WORKTREE_SYNCED_AT,
        "protocol_count": len(PROTOCOLS),
        "status_counts": counts,
    }
