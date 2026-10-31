from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


ALL_REPORT_START = date(2000, 1, 1)
ALL_REPORT_LABEL = "全部"


@dataclass(frozen=True)
class ReportPeriod:
    start: date
    end: date
    label: str
    is_all: bool = False

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("报表开始日期不能晚于结束日期")


def single_day_period(value: date) -> ReportPeriod:
    return ReportPeriod(value, value, value.isoformat(), False)


def all_dates_period(today: date | None = None) -> ReportPeriod:
    end = today or datetime.now().astimezone().date()
    return ReportPeriod(ALL_REPORT_START, end, ALL_REPORT_LABEL, True)


def coerce_report_period(value: date | ReportPeriod) -> ReportPeriod:
    return value if isinstance(value, ReportPeriod) else single_day_period(value)


def parse_report_period(value: str, *, today: date | None = None) -> ReportPeriod:
    current = today or datetime.now().astimezone().date()
    normalized = str(value or "").strip()
    if normalized.casefold() in {"全部", "all", "all time", "all-time"}:
        return all_dates_period(current)
    try:
        day = date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("日期必须是 YYYY-MM-DD，或填写“全部”") from exc
    if day > current:
        raise ValueError("查询日期不能晚于今天")
    return single_day_period(day)


_ALL_TOKENS = {"全部", "all", "all time", "all-time"}


def _parse_range_endpoint(value: str, fallback: date, current: date, name: str) -> date:
    normalized = str(value or "").strip()
    if normalized.casefold() in _ALL_TOKENS:
        return fallback
    try:
        day = date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name}必须是 YYYY-MM-DD，或填写“全部”") from exc
    if day > current:
        raise ValueError(f"{name}不能晚于今天")
    return day


def parse_report_range(
    start_value: str, end_value: str, *, today: date | None = None,
) -> ReportPeriod:
    """Parse a start/end date pair; “全部” means 2000-01-01 / today."""
    current = today or datetime.now().astimezone().date()
    start_all = str(start_value or "").strip().casefold() in _ALL_TOKENS
    end_all = str(end_value or "").strip().casefold() in _ALL_TOKENS
    start = _parse_range_endpoint(start_value, ALL_REPORT_START, current, "开始日期")
    end = _parse_range_endpoint(end_value, current, current, "结束日期")
    is_all = start_all and end_all
    if is_all:
        return all_dates_period(current)
    label = start.isoformat() if start == end else f"{start.isoformat()}~{end.isoformat()}"
    return ReportPeriod(start, end, label, False)


def protocol_day(value: date) -> dict[str, int]:
    return {"1": value.year, "2": value.month, "3": value.day}
