"""Pure campaign-control policy and guarded execution helpers.

This module deliberately contains no Google Ads client, RPC path, or browser
automation.  It turns read-only facts into decisions.  A caller may hand the
result to :func:`execute_write_plans`, but writes are disabled by default and
require an explicit upper-layer authorization flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Iterable, Mapping


class CampaignAction(str, Enum):
    """Actions emitted by the policy layer.

    ``REVIEW_THEME_CONVERSIONS`` and ``RECOMMEND_PAUSE`` are recommendations,
    never executable mutations.  This prevents cost heuristics from silently
    pausing a campaign.
    """

    KEEP = "keep"
    PAUSE = "pause"
    ALLOW_LOW_BUDGET_WHITE = "allow_low_budget_white"
    REVIEW_THEME_CONVERSIONS = "review_theme_conversions"
    RECOMMEND_PAUSE = "recommend_pause"


@dataclass(frozen=True)
class CampaignFacts:
    campaign_id: str
    enabled: bool
    daily_budget: Decimal | int | float | str
    is_white_volume: bool = False

    def __post_init__(self) -> None:
        if not str(self.campaign_id).strip():
            raise ValueError("campaign_id 不能为空")
        object.__setattr__(self, "campaign_id", str(self.campaign_id).strip())
        object.__setattr__(self, "daily_budget", _non_negative_decimal(
            self.daily_budget, "daily_budget",
        ))


@dataclass(frozen=True)
class CampaignDecision:
    campaign_id: str
    action: CampaignAction
    reason: str
    requires_write: bool = False
    desired_enabled: bool | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


def evaluate_daily_switch(
    campaigns: Iterable[CampaignFacts | Mapping[str, Any]],
    *,
    run_today: bool,
    white_daily_budget_limit: Decimal | int | float | str,
) -> tuple[CampaignDecision, ...]:
    """Apply the daily master switch without performing any writes.

    When ``run_today`` is false, an already-running low-budget white-volume
    campaign is exempt.  All other running campaigns receive a PAUSE decision.
    A paused exempt campaign is *not* automatically enabled.
    """

    if type(run_today) is not bool:
        raise TypeError("run_today 必须是 bool")
    limit = _non_negative_decimal(
        white_daily_budget_limit, "white_daily_budget_limit",
    )
    decisions: list[CampaignDecision] = []
    for raw in campaigns:
        campaign = _campaign_facts(raw)
        details = {
            "run_today": run_today,
            "daily_budget": str(campaign.daily_budget),
            "white_daily_budget_limit": str(limit),
            "is_white_volume": campaign.is_white_volume,
        }
        if run_today:
            decisions.append(CampaignDecision(
                campaign.campaign_id,
                CampaignAction.KEEP,
                "今日投放总开关已开启；保留当前状态",
                details=details,
            ))
            continue

        exempt = campaign.is_white_volume and campaign.daily_budget <= limit
        if exempt:
            decisions.append(CampaignDecision(
                campaign.campaign_id,
                CampaignAction.ALLOW_LOW_BUDGET_WHITE,
                "今日不跑广告；该系列属于白量且预算未超过上限，可保留当前状态",
                details=details,
            ))
        elif campaign.enabled:
            decisions.append(CampaignDecision(
                campaign.campaign_id,
                CampaignAction.PAUSE,
                "今日不跑广告；非低预算白量系列应暂停",
                requires_write=True,
                desired_enabled=False,
                details=details,
            ))
        else:
            decisions.append(CampaignDecision(
                campaign.campaign_id,
                CampaignAction.KEEP,
                "今日不跑广告；系列已经暂停，无需操作",
                desired_enabled=False,
                details=details,
            ))
    return tuple(decisions)


@dataclass(frozen=True)
class CostControlPolicy:
    """Cost thresholds expressed as a ratio over the target CPA."""

    pause_over_target_ratio: Decimal | int | float | str = Decimal("0.30")

    def __post_init__(self) -> None:
        ratio = _non_negative_decimal(
            self.pause_over_target_ratio, "pause_over_target_ratio",
        )
        object.__setattr__(self, "pause_over_target_ratio", ratio)


@dataclass(frozen=True)
class CostMetrics:
    campaign_id: str
    target_cost_per_conversion: Decimal | int | float | str
    actual_cost_per_conversion: Decimal | int | float | str
    theme_conversions: int = 0

    def __post_init__(self) -> None:
        if not str(self.campaign_id).strip():
            raise ValueError("campaign_id 不能为空")
        object.__setattr__(self, "campaign_id", str(self.campaign_id).strip())
        target = _decimal(self.target_cost_per_conversion, "target_cost_per_conversion")
        if target <= 0:
            raise ValueError("target_cost_per_conversion 必须大于 0")
        actual = _non_negative_decimal(
            self.actual_cost_per_conversion, "actual_cost_per_conversion",
        )
        if type(self.theme_conversions) is not int or self.theme_conversions < 0:
            raise ValueError("theme_conversions 必须是非负整数")
        object.__setattr__(self, "target_cost_per_conversion", target)
        object.__setattr__(self, "actual_cost_per_conversion", actual)


def evaluate_cost_control(
    metrics: CostMetrics | Mapping[str, Any],
    policy: CostControlPolicy | None = None,
) -> CampaignDecision:
    """Return a cost recommendation; never return an executable write.

    Normal cost is left alone.  A slight overrun asks the operator to inspect
    theme conversions.  Only an overrun strictly above the configured ratio
    recommends pausing, and that recommendation still needs human review.
    """

    item = metrics if isinstance(metrics, CostMetrics) else CostMetrics(**metrics)
    settings = policy or CostControlPolicy()
    target = item.target_cost_per_conversion
    actual = item.actual_cost_per_conversion
    pause_threshold = target * (Decimal("1") + settings.pause_over_target_ratio)
    over_ratio = (actual / target) - Decimal("1")
    details = {
        "target_cost_per_conversion": str(target),
        "actual_cost_per_conversion": str(actual),
        "over_target_ratio": str(over_ratio),
        "pause_over_target_ratio": str(settings.pause_over_target_ratio),
        "pause_threshold": str(pause_threshold),
        "theme_conversions": item.theme_conversions,
    }
    if actual <= target:
        return CampaignDecision(
            item.campaign_id,
            CampaignAction.KEEP,
            "转化成本正常，不调整系列",
            details=details,
        )
    if actual <= pause_threshold:
        return CampaignDecision(
            item.campaign_id,
            CampaignAction.REVIEW_THEME_CONVERSIONS,
            "转化成本略超目标；先检查主题转化，不自动暂停",
            details=details,
        )
    return CampaignDecision(
        item.campaign_id,
        CampaignAction.RECOMMEND_PAUSE,
        "转化成本超过配置比例；建议人工复核后暂停",
        details=details,
    )


class DisapprovalState(str, Enum):
    DISAPPROVED = "disapproved"
    WHITE_CONTENT_REPLACED = "white_content_replaced"
    OLD_AD_REMOVED = "old_ad_removed"
    NEW_AD_CREATED_PAUSED = "new_ad_created_paused"
    APPROVED_AWAITING_SETTINGS_REVIEW = "approved_awaiting_settings_review"
    SETTINGS_REVIEWED = "settings_reviewed"
    READY_TO_START = "ready_to_start"
    STARTED = "started"


class DisapprovalEvent(str, Enum):
    REPLACE_WITH_WHITE_CONTENT = "replace_with_white_content"
    REMOVE_OLD_AD = "remove_old_ad"
    CREATE_NEW_AD_PAUSED = "create_new_ad_paused"
    MARK_APPROVED = "mark_approved"
    REVIEW_SETTINGS = "review_settings"
    ALLOW_START = "allow_start"
    START = "start"
    REPORT_REDISAPPROVED = "report_redisapproved"


_DISAPPROVAL_TRANSITIONS: dict[
    tuple[DisapprovalState, DisapprovalEvent], DisapprovalState
] = {
    (DisapprovalState.DISAPPROVED, DisapprovalEvent.REPLACE_WITH_WHITE_CONTENT):
        DisapprovalState.WHITE_CONTENT_REPLACED,
    (DisapprovalState.WHITE_CONTENT_REPLACED, DisapprovalEvent.REMOVE_OLD_AD):
        DisapprovalState.OLD_AD_REMOVED,
    (DisapprovalState.OLD_AD_REMOVED, DisapprovalEvent.CREATE_NEW_AD_PAUSED):
        DisapprovalState.NEW_AD_CREATED_PAUSED,
    (DisapprovalState.NEW_AD_CREATED_PAUSED, DisapprovalEvent.MARK_APPROVED):
        DisapprovalState.APPROVED_AWAITING_SETTINGS_REVIEW,
    (DisapprovalState.APPROVED_AWAITING_SETTINGS_REVIEW, DisapprovalEvent.REVIEW_SETTINGS):
        DisapprovalState.SETTINGS_REVIEWED,
    (DisapprovalState.SETTINGS_REVIEWED, DisapprovalEvent.ALLOW_START):
        DisapprovalState.READY_TO_START,
    (DisapprovalState.READY_TO_START, DisapprovalEvent.START):
        DisapprovalState.STARTED,
    (DisapprovalState.NEW_AD_CREATED_PAUSED, DisapprovalEvent.REPORT_REDISAPPROVED):
        DisapprovalState.DISAPPROVED,
}


@dataclass(frozen=True)
class DisapprovalWorkflow:
    """Immutable state machine for replacing one disapproved ad."""

    old_ad_id: str
    state: DisapprovalState = DisapprovalState.DISAPPROVED
    new_ad_id: str = ""
    history: tuple[DisapprovalEvent, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.old_ad_id).strip():
            raise ValueError("old_ad_id 不能为空")
        object.__setattr__(self, "old_ad_id", str(self.old_ad_id).strip())
        if not isinstance(self.state, DisapprovalState):
            object.__setattr__(self, "state", DisapprovalState(self.state))

    @property
    def may_start(self) -> bool:
        return self.state in {DisapprovalState.READY_TO_START, DisapprovalState.STARTED}

    def advance(
        self,
        event: DisapprovalEvent | str,
        *,
        new_ad_id: str = "",
    ) -> "DisapprovalWorkflow":
        normalized = event if isinstance(event, DisapprovalEvent) else DisapprovalEvent(event)
        next_state = _DISAPPROVAL_TRANSITIONS.get((self.state, normalized))
        if next_state is None:
            raise ValueError(f"拒登处理状态 {self.state.value} 不允许事件 {normalized.value}")
        resolved_new_ad_id = self.new_ad_id
        if normalized is DisapprovalEvent.CREATE_NEW_AD_PAUSED:
            resolved_new_ad_id = str(new_ad_id).strip()
            if not resolved_new_ad_id:
                raise ValueError("创建暂停广告后必须记录 new_ad_id")
        return replace(
            self,
            state=next_state,
            new_ad_id=resolved_new_ad_id,
            history=(*self.history, normalized),
        )


@dataclass(frozen=True)
class WritePlan:
    """A mutation intent.  Constructing a plan never executes it."""

    operation: str
    target_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.operation).strip() or not str(self.target_id).strip():
            raise ValueError("写入计划必须包含 operation 和 target_id")


@dataclass(frozen=True)
class ExecutionRecord:
    plan: WritePlan
    status: str
    write_performed: bool
    result: Any = None


def write_plans_for_decisions(
    decisions: Iterable[CampaignDecision],
) -> tuple[WritePlan, ...]:
    """Convert only explicit executable decisions into mutation intents."""

    return tuple(
        WritePlan("pause_campaign", item.campaign_id, {"enabled": False})
        for item in decisions
        if item.action is CampaignAction.PAUSE and item.requires_write
    )


def execute_write_plans(
    plans: Iterable[WritePlan],
    writer: Callable[[WritePlan], Any],
    *,
    dry_run: bool = True,
    authorized: bool = False,
) -> tuple[ExecutionRecord, ...]:
    """Execute plans only when both dry-run is off and authorization is set.

    Authorization is checked for the entire batch before the first writer call,
    preventing partial writes caused by a missing approval flag.
    """

    if type(dry_run) is not bool or type(authorized) is not bool:
        raise TypeError("dry_run 和 authorized 必须是 bool")
    queued = tuple(plans)
    if dry_run:
        return tuple(ExecutionRecord(plan, "dry_run", False) for plan in queued)
    if not authorized:
        raise PermissionError("协议写入需要人工或上层显式授权")
    records: list[ExecutionRecord] = []
    for plan in queued:
        records.append(ExecutionRecord(plan, "executed", True, writer(plan)))
    return tuple(records)


def _campaign_facts(raw: CampaignFacts | Mapping[str, Any]) -> CampaignFacts:
    if isinstance(raw, CampaignFacts):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("campaigns 必须包含 CampaignFacts 或映射")
    return CampaignFacts(**raw)


def _decimal(value: Decimal | int | float | str, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是数字")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是数字") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name} 必须是有限数字")
    return number


def _non_negative_decimal(
    value: Decimal | int | float | str,
    field_name: str,
) -> Decimal:
    number = _decimal(value, field_name)
    if number < 0:
        raise ValueError(f"{field_name} 不能小于 0")
    return number
