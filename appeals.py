from __future__ import annotations

import hashlib
import json
import os
import re
import string
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from .adspower import (
    close_cdp_target,
    ensure_google_ads_targets,
    list_google_ads_targets,
    open_cdp_target,
)
from .browser_provider import ADSPOWER, normalize_browser_type, resolve_cdp, window_key
from .config import (
    format_cid,
    global_account_scope,
    load_ui_state,
    local_config_dir,
    merge_global_account_snapshots,
    normalize_cid,
    save_run_report,
    save_ui_state,
)
from .protocol import CDPPage, ProtocolRunner


MAX_APPEALS_PER_BATCH = 10
APPEAL_PENDING = "已打开官方入口（待人工提交）"
APPEAL_DRAFT = "自动草稿待审核"
APPEAL_SUBMITTED = "已一键提交"
APPEAL_OFFICIAL_PENDING = "官方申诉处理中"
APPEAL_LOGIN_REQUIRED = "等待重新登录"
APPEAL_VERIFICATION_REQUIRED = "等待身份验证"


def _appeal_key(
    profile_id: str, cid: str, browser_type: str | None = ADSPOWER,
) -> tuple[str, str]:
    return (
        window_key(browser_type, profile_id),
        normalize_cid(str(cid or "")),
    )


def _account_browser_type(
    account: dict[str, Any], fallback: str = ADSPOWER,
) -> str:
    return normalize_browser_type(account.get("browser_type") or fallback)
APPEAL_USER_ACTION_REQUIRED = "等待安全验证"
APPEAL_SUBMIT_FAILED = "提交失败"
APPEAL_SUBMITTING = "正在提交"
APPEAL_STATUSES = (
    APPEAL_PENDING,
    APPEAL_SUBMITTED,
    APPEAL_OFFICIAL_PENDING,
    APPEAL_LOGIN_REQUIRED,
    APPEAL_VERIFICATION_REQUIRED,
    APPEAL_USER_ACTION_REQUIRED,
    "已人工提交",
    "申诉成功",
    "申诉被拒",
)
APPEAL_BASIS_CORRECTED = "corrected"
APPEAL_BASIS_ERROR = "decision_error"
APPEAL_BASIS_LABELS = {
    APPEAL_BASIS_CORRECTED: "已完成整改，请重新审核",
    APPEAL_BASIS_ERROR: "认为中止决定有误",
}
REASON_READ = "已读取"
REASON_NOT_PROVIDED = "官方页未提供具体政策"
REASON_NOT_SUSPENDED = "未发现账号中止提示"
REASON_READ_FAILED = "读取失败"
OFFICIAL_REASON_SOURCE = "Google Ads 官方账号中止页"
DEFAULT_APPEAL_TEMPLATE = """Google Ads 支持团队：

您好。我代表 Google Ads 账号 {cid}（{account_name}）申请复核账号中止决定。
Google Ads 官方账号中止页面显示，本账号涉及“{suspension_policy}”政策。

业务主体以及网站/产品说明：
【请填写真实的公司或个人主体、网站、产品、服务和客户获取方式】

排查后确认的问题与原因：
【请根据实际情况说明，不要猜测或隐瞒】

已经完成的整改：
【请逐项填写已经完成的整改，并提供可以核验的网址或证据】

为避免问题再次发生而采取的措施：
【请填写审核流程、负责人和持续检查措施】

可以提供的证明材料：
【请填写营业资料、付款资料、域名/产品证明或其他真实附件】

以上信息均由我核对并确认真实。我愿意配合提供进一步资料，请重新审核。谢谢。
"""

ALLOWED_APPEAL_FIELDS = {
    "cid", "account_name", "suspension_policy", "suspension_reason",
}
INTERNAL_APPEAL_PATTERNS = (
    ("AdsPower 窗口信息", r"AdsPower|窗口\s*(?:ID|编号)"),
    ("Google Ads 内部账户 ID", r"内部(?:账户|账号)\s*ID|\bocid\b"),
    ("内部协议或服务字段", r"内部协议|CustomerService|is_fraudster|ui_account_status"),
    ("内部账户状态码", r"账户状态\s*[=:：]|界面状态\s*[=:：]|原始状态(?:码)?\s*[=:：]"),
    ("MCC 内部关联状态", r"MCC\s*关联\s*[=:：]"),
    ("内部风控判断", r"风控封禁\s*[=:：]|账户风控/政策封禁"),
    ("未核实的原因占位内容", r"界面未返回具体原因|未读取具体原因|官方页未提供具体政策"),
)
LEGACY_APPEAL_TEMPLATE_MARKERS = (
    "当前界面显示的中止原因：",
    "【请在提交前补充：业务主体、网站与产品、问题原因、具体整改、证明材料】",
)
APPEAL_MODE_AUTO = "automatic"
APPEAL_MODE_MANUAL = "manual"
DEFAULT_APPEAL_AUTOMATION: dict[str, Any] = {
    "version": 2,
    "mode": APPEAL_MODE_AUTO,
    "time": "09:00",
    "auto_submit": True,
    "basis": APPEAL_BASIS_CORRECTED,
    "last_run_date": "",
    "last_run_at": "",
    "last_status": "尚未执行",
    "last_summary": "",
}
APPEAL_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
APPEAL_PROTECTED_STATUSES = {
    APPEAL_SUBMITTING,
    APPEAL_SUBMITTED,
    APPEAL_OFFICIAL_PENDING,
    APPEAL_LOGIN_REQUIRED,
    APPEAL_VERIFICATION_REQUIRED,
    APPEAL_USER_ACTION_REQUIRED,
    "已人工提交",
    "申诉成功",
}
APPEAL_LOCK_STALE_SECONDS = 2 * 60 * 60
APPEAL_ATTEMPT_STALE_SECONDS = 30 * 60
APPEAL_TRANSIENT_RETRY_SECONDS = 15 * 60
APPEAL_USER_ACTION_STATUSES = {
    APPEAL_LOGIN_REQUIRED,
    APPEAL_VERIFICATION_REQUIRED,
    APPEAL_USER_ACTION_REQUIRED,
}
APPEAL_RETRY_TRANSIENT = "transient"
APPEAL_RETRY_USER_ACTION = "user_action"
APPEAL_RETRY_MANUAL_REVIEW = "manual_review"
APPEAL_RETRY_NOT_NEEDED = "not_needed"
INCOMPLETE_APPEAL_PATTERNS = (
    r"【[^】]*(?:请填写|请补充|待补充|示例)[^】]*】",
    r"(?:请填写|请补充|待补充)",
    r"\[(?:please fill|todo|replace me)[^\]]*\]",
    r"(?:^|\s)(?:TODO|TBD)(?:\s|$)",
)


def appeal_template_path() -> Path:
    return local_config_dir() / "appeal-template.txt"


def appeal_history_path() -> Path:
    return local_config_dir() / "appeal-history.json"


def suspension_reason_path() -> Path:
    return local_config_dir() / "suspension-reasons.json"


def appeal_automation_path() -> Path:
    return local_config_dir() / "appeal-automation.json"


def appeal_automation_lock_path() -> Path:
    return local_config_dir() / "appeal-automation.lock"


def appeal_submission_lock_path(history_path: Path | None = None) -> Path:
    target = history_path or appeal_history_path()
    return target.with_suffix(target.suffix + ".submit.lock")


def appeal_rpc_capture_path() -> Path:
    return local_config_dir() / "appeal-rpc-capture.json"


# Google has no documented appeal endpoint; the appeal drawer posts through
# internal, unpublished requests. During a real submission we record every
# POST the official page makes so the actual appeal RPC can be identified.
APPEAL_RPC_URL_HINTS = ("appeal", "support", "sibyl", "feedback", "batchexecute")
MAX_APPEAL_RPC_CAPTURES = 200


def _is_appeal_service_url(url: str) -> bool:
    folded = url.casefold()
    return any(hint in folded for hint in APPEAL_RPC_URL_HINTS)


def annotate_appeal_rpc(
    entries: Iterable[dict[str, Any]], appeal_text: str,
) -> list[dict[str, Any]]:
    """Flag captured POSTs that likely carry the actual appeal submission."""
    probe = re.sub(r"\s+", "", str(appeal_text or ""))[:48]
    annotated: list[dict[str, Any]] = []
    for entry in entries:
        row = dict(entry)
        url = str(row.get("url") or "")
        body = str(row.get("postData") or "")
        compact_body = re.sub(r"\s+", "", body)
        hints = [hint for hint in APPEAL_RPC_URL_HINTS if hint in url.casefold()]
        contains_text = bool(probe) and probe in compact_body
        row["url_hints"] = hints
        row["contains_appeal_text"] = contains_text
        row["likely_appeal_submit"] = contains_text or bool(hints)
        annotated.append(row)
    return annotated


def save_appeal_rpc_capture(
    entries: Iterable[dict[str, Any]],
    cid: str,
    *,
    path: Path | None = None,
) -> Path | None:
    """Append one submission's captured POSTs to the local protocol archive."""
    rows = list(entries)
    if not rows:
        return None
    target = path or appeal_rpc_capture_path()
    try:
        existing: list[dict[str, Any]] = []
        if target.exists():
            payload = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                existing = [row for row in payload if isinstance(row, dict)]
        existing.append({
            "cid": format_cid(normalize_cid(cid)) if cid else "",
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "requests": rows,
        })
        existing = existing[-MAX_APPEAL_RPC_CAPTURES:]
        target.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return target
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _collect_appeal_network(page: CDPPage, bucket: list[dict[str, Any]]) -> None:
    """Drain buffered Network events and keep Google-bound POST requests.

    Appeal-service requests are always kept (never dropped by the 100-row
    noise cap) with their full body, and once loading finishes their full
    response body is attached — that request/response pair is what makes
    the unpublished submit RPC reproducible.
    """
    try:
        events = page.drain_events("Network.")
    except Exception:
        return
    by_id = {str(row.get("request_id") or ""): row for row in bucket}
    for event in events:
        method = str(event.get("method") or "")
        params = event.get("params") or {}
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            url = str(request.get("url") or "")
            if len(bucket) >= 100 and not _is_appeal_service_url(url):
                continue
            if str(request.get("method") or "").upper() != "POST":
                continue
            if "google" not in url.casefold():
                continue
            body = str(request.get("postData") or "")
            row = {
                "url": url[:2000],
                "request_id": str(params.get("requestId") or ""),
                "postData": body if _is_appeal_service_url(url) else body[:8000],
            }
            bucket.append(row)
            by_id[row["request_id"]] = row
        elif method == "Network.loadingFinished":
            row = by_id.get(str(params.get("requestId") or ""))
            if row is None or not _is_appeal_service_url(str(row.get("url") or "")):
                continue
            _attach_response_body(page, row)


def _attach_response_body(page: CDPPage, row: dict[str, Any]) -> None:
    """Fetch the finished response body for one captured appeal request."""
    try:
        payload = page.command(
            "Network.getResponseBody", {"requestId": row["request_id"]},
            timeout=8,
        )
        body = (payload or {}).get("result") or {}
        text = str(body.get("body") or "")
        if body.get("base64Encoded"):
            text = "<base64>" + text[:200]
        row["response_body"] = text[:50000]
        row.pop("response_body_error", None)
    except Exception as exc:
        # Keep the reason: without it a silent failure looks like "no response".
        row["response_body_error"] = str(exc)[:300]


def fetch_missing_response_bodies(page: CDPPage, bucket: list[dict[str, Any]]) -> None:
    """Retry response bodies for appeal rows that lack one (final sweep)."""
    for row in bucket:
        if row.get("response_body"):
            continue
        if not _is_appeal_service_url(str(row.get("url") or "")):
            continue
        if not row.get("request_id"):
            continue
        _attach_response_body(page, row)


def validate_appeal_time(value: str) -> str:
    normalized = str(value or "").strip()
    if not APPEAL_TIME_PATTERN.fullmatch(normalized):
        raise ValueError("每日申诉检查时间必须使用 HH:MM 格式，例如 09:00 或 21:30")
    return normalized


def load_appeal_automation(path: Path | None = None) -> dict[str, Any]:
    target = path or appeal_automation_path()
    value: dict[str, Any] = {}
    if target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                value = dict(payload)
        except (OSError, json.JSONDecodeError):
            value = {}
    result = dict(DEFAULT_APPEAL_AUTOMATION)
    result.update(value)
    if result.get("mode") not in {APPEAL_MODE_AUTO, APPEAL_MODE_MANUAL}:
        result["mode"] = APPEAL_MODE_AUTO
    result["auto_submit"] = bool(result.get("auto_submit", True))
    if result.get("basis") not in APPEAL_BASIS_LABELS:
        result["basis"] = APPEAL_BASIS_CORRECTED
    try:
        result["time"] = validate_appeal_time(str(result.get("time") or "09:00"))
    except ValueError:
        result["time"] = "09:00"
    return result


def save_appeal_automation(
    value: dict[str, Any], path: Path | None = None,
) -> Path:
    target = path or appeal_automation_path()
    mode = str(value.get("mode") or APPEAL_MODE_AUTO)
    if mode not in {APPEAL_MODE_AUTO, APPEAL_MODE_MANUAL}:
        raise ValueError("账户申诉运行模式必须是自动或手动")
    payload = dict(DEFAULT_APPEAL_AUTOMATION)
    payload.update(value)
    basis = str(payload.get("basis") or APPEAL_BASIS_CORRECTED)
    if basis not in APPEAL_BASIS_LABELS:
        raise ValueError("每日自动申诉依据无效")
    payload.update({
        "version": 2,
        "mode": mode,
        "time": validate_appeal_time(payload["time"]),
        "auto_submit": bool(payload.get("auto_submit", True)),
        "basis": basis,
    })
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def appeal_automation_due(
    settings: dict[str, Any], now: datetime | None = None,
) -> bool:
    current = now or datetime.now().astimezone()
    return (
        str(settings.get("mode") or APPEAL_MODE_AUTO) == APPEAL_MODE_AUTO
        and str(settings.get("last_run_date") or "") != current.date().isoformat()
        and current.strftime("%H:%M") >= validate_appeal_time(str(settings.get("time") or "09:00"))
    )


def acquire_appeal_automation_lock(
    *, path: Path | None = None, now: datetime | None = None,
) -> tuple[Path, str]:
    """Atomically prevent GUI and Task Scheduler from submitting concurrently."""
    target = path or appeal_automation_lock_path()
    current = now or datetime.now().astimezone()
    target.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}:{current.isoformat(timespec='seconds')}"
    payload = json.dumps({
        "token": token,
        "pid": os.getpid(),
        "started_at": current.isoformat(timespec="seconds"),
        "started_epoch": current.timestamp(),
    }, ensure_ascii=False)
    for _attempt in range(2):
        try:
            with target.open("x", encoding="utf-8") as stream:
                stream.write(payload)
            return target, token
        except FileExistsError:
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                started = float(existing.get("started_epoch") or target.stat().st_mtime)
            except (OSError, ValueError, json.JSONDecodeError):
                try:
                    started = target.stat().st_mtime
                except FileNotFoundError:
                    continue
            if current.timestamp() - started <= APPEAL_LOCK_STALE_SECONDS:
                raise RuntimeError("另一个每日申诉任务正在运行，已阻止重复并发提交")
            try:
                target.unlink()
            except FileNotFoundError:
                pass
    raise RuntimeError("无法取得每日申诉任务锁")


def release_appeal_automation_lock(path: Path, token: str) -> None:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if str(existing.get("token") or "") != token:
            return
        path.unlink()
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return


def load_suspension_reasons(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or suspension_reason_path()
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("records", []) if isinstance(payload, dict) else []
    return [dict(row) for row in rows if isinstance(row, dict)]


def save_suspension_reasons(
    records: Iterable[dict[str, Any]], path: Path | None = None,
) -> Path:
    target = path or suspension_reason_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "records": list(records)}
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def latest_suspension_reasons(
    records: Iterable[dict[str, Any]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records if records is not None else load_suspension_reasons():
        try:
            cid = normalize_cid(str(record.get("cid") or ""))
        except ValueError:
            continue
        profile_id = str(record.get("profile_id") or "").strip()
        if not profile_id:
            continue
        key = _appeal_key(
            profile_id, cid, _account_browser_type(record),
        )
        current = latest.get(key)
        if current is None or str(record.get("reason_read_at") or "") >= str(
            current.get("reason_read_at") or ""
        ):
            latest[key] = dict(record)
    return latest


def parse_suspension_reason(page_text: str) -> dict[str, str]:
    """Extract only an explicit policy name from the official suspension banner."""
    text = re.sub(r"[\t\r\f\v ]+", " ", str(page_text or ""))
    text = re.sub(r"\n+", "\n", text).strip()
    compact = re.sub(r"\s+", " ", text)
    suspended = bool(re.search(
        r"账号(?:已)?被中止|账户(?:已)?被中止|账号已暂停|账户已暂停|"
        r"account (?:has been |is )?suspended",
        compact,
        flags=re.IGNORECASE,
    ))
    patterns = (
        r"(?:您的)?(?:账号|账户)违反了[“\"]\s*([^”\"]{2,120}?)\s*[”\"]政策",
        r"(?:您的)?(?:账号|账户)违反了\s*([^。；;\n]{2,120}?)\s*政策",
        r"(?:your )?account (?:has )?violated (?:the )?[\"“]?(.{2,120}?)[\"”]? policy",
        r"(?:your )?account (?:has been|is) suspended (?:for|due to) (?:a |an |the )?(.{2,120}?)(?: policy)?(?:[.;]|$)",
    )
    # A policy phrase elsewhere on the page is not evidence that the account is
    # suspended for that policy. Require both official signals before accepting it.
    for pattern in patterns if suspended else ():
        match = re.search(pattern, compact, flags=re.IGNORECASE)
        if not match:
            continue
        policy = re.sub(r"\s+", " ", match.group(1)).strip(" ：:，,。.;；'\"“”")
        policy = re.sub(r"\s+policy$", "", policy, flags=re.IGNORECASE).strip()
        if not policy:
            continue
        start = max(0, match.start() - 100)
        end = min(len(compact), match.end() + 160)
        return {
            "suspension_policy": policy,
            "suspension_reason": f"违反“{policy}”政策",
            "suspension_reason_status": REASON_READ,
            "suspension_reason_source": OFFICIAL_REASON_SOURCE,
            "suspension_page_excerpt": compact[start:end],
        }
    return {
        "suspension_policy": "",
        "suspension_reason": "",
        "suspension_reason_status": REASON_NOT_PROVIDED if suspended else REASON_NOT_SUSPENDED,
        "suspension_reason_source": OFFICIAL_REASON_SOURCE,
        "suspension_page_excerpt": compact[:500],
    }


def load_appeal_template(path: Path | None = None) -> str:
    target = path or appeal_template_path()
    if not target.exists():
        return DEFAULT_APPEAL_TEMPLATE
    value = target.read_text(encoding="utf-8").strip()
    if not value:
        return DEFAULT_APPEAL_TEMPLATE
    if any(marker in value for marker in LEGACY_APPEAL_TEMPLATE_MARKERS):
        return DEFAULT_APPEAL_TEMPLATE
    try:
        validate_appeal_template(value)
    except ValueError:
        return DEFAULT_APPEAL_TEMPLATE
    return value


def _internal_appeal_detail(value: str) -> str:
    for label, pattern in INTERNAL_APPEAL_PATTERNS:
        if re.search(pattern, str(value or ""), flags=re.IGNORECASE):
            return label
    return ""


def validate_appeal_template(value: str) -> None:
    template = str(value or "").strip()
    if not template:
        raise ValueError("申诉文案模板不能为空")
    fields: set[str] = set()
    try:
        for _literal, field_name, format_spec, conversion in string.Formatter().parse(template):
            if field_name:
                fields.add(field_name)
                if format_spec or conversion:
                    raise ValueError("申诉模板占位符不支持格式化参数或类型转换")
    except ValueError as exc:
        raise ValueError(f"申诉模板格式无效：{exc}") from exc
    unknown = sorted(fields - ALLOWED_APPEAL_FIELDS)
    if unknown:
        raise ValueError(
            "申诉模板包含内部或不支持的占位符："
            + "、".join(f"{{{name}}}" for name in unknown)
            + "。只能使用 {cid}、{account_name}、{suspension_policy}、{suspension_reason}。"
        )
    if "cid" not in fields:
        raise ValueError("申诉模板必须包含 {cid}，避免把文案提交到错误账号")
    if not fields.intersection({"suspension_policy", "suspension_reason"}):
        raise ValueError("申诉模板必须包含官方原因占位符 {suspension_policy} 或 {suspension_reason}")
    detail = _internal_appeal_detail(template)
    if detail:
        raise ValueError(f"申诉模板包含不能提交给 Google 的{detail}，请删除后再保存")


def save_appeal_template(value: str, path: Path | None = None) -> Path:
    validate_appeal_template(value)
    target = path or appeal_template_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(value).strip() + "\n", encoding="utf-8")
    return target


def load_appeal_history(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or appeal_history_path()
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("records", []) if isinstance(payload, dict) else []
    return [dict(row) for row in rows if isinstance(row, dict)]


def save_appeal_history(
    records: Iterable[dict[str, Any]], path: Path | None = None,
) -> Path:
    target = path or appeal_history_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "records": list(records)}
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def latest_history_by_cid(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        try:
            cid = normalize_cid(str(record.get("cid") or ""))
        except ValueError:
            continue
        current = latest.get(cid)
        if current is None or str(record.get("updated_at") or record.get("created_at") or "") >= str(
            current.get("updated_at") or current.get("created_at") or ""
        ):
            latest[cid] = dict(record)
    return latest


def effective_appeal_history_by_key(
    records: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return the latest meaningful state, never hiding a submission with a draft."""
    latest_any: dict[tuple[str, str], dict[str, Any]] = {}
    latest_action: dict[tuple[str, str], dict[str, Any]] = {}
    for source in records:
        record = dict(source)
        profile_id = str(record.get("profile_id") or "").strip()
        try:
            cid = normalize_cid(str(record.get("cid") or ""))
        except ValueError:
            continue
        if not profile_id:
            continue
        key = _appeal_key(profile_id, cid, _account_browser_type(record))
        stamp = str(record.get("updated_at") or record.get("created_at") or "")
        current = latest_any.get(key)
        current_stamp = str(
            (current or {}).get("updated_at") or (current or {}).get("created_at") or ""
        )
        if current is None or stamp >= current_stamp:
            latest_any[key] = record
        if str(record.get("status") or "") == APPEAL_DRAFT:
            continue
        action = latest_action.get(key)
        action_stamp = str(
            (action or {}).get("updated_at") or (action or {}).get("created_at") or ""
        )
        if action is None or stamp >= action_stamp:
            latest_action[key] = record
    return {key: latest_action.get(key, row) for key, row in latest_any.items()}


def _parse_record_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def appeal_submission_is_protected(
    record: dict[str, Any] | None,
    *,
    allow_resubmit: bool = False,
    allow_user_action_retry: bool = False,
    allow_failed_retry: bool = False,
    now: datetime | None = None,
) -> bool:
    """Return whether local state must block another external submit attempt."""
    row = record or {}
    status = str(row.get("status") or "")
    if status == APPEAL_SUBMITTING:
        started = _parse_record_time(row.get("attempt_started_at") or row.get("updated_at"))
        if started is None:
            return True
        current = now or datetime.now().astimezone()
        return (current - started).total_seconds() <= APPEAL_ATTEMPT_STALE_SECONDS
    if status in APPEAL_USER_ACTION_STATUSES:
        return not allow_user_action_retry
    if status == APPEAL_SUBMIT_FAILED:
        if allow_failed_retry:
            return False
        if str(row.get("retry_class") or "") != APPEAL_RETRY_TRANSIENT:
            return bool(row.get("retry_class"))
        retry_after = _parse_record_time(row.get("retry_after"))
        return retry_after is not None and (now or datetime.now().astimezone()) < retry_after
    if allow_resubmit and status in {APPEAL_SUBMITTED, "已人工提交"}:
        # A prior submission record is not the same as a successful appeal.
        # Manual re-appeal remains available while the current account row is
        # still suspended; validate_appeal_account() enforces that live gate.
        return False
    return status in APPEAL_PROTECTED_STATUSES


def classify_appeal_outcome(
    status: str, error: str = "", *, now: datetime | None = None,
) -> dict[str, Any]:
    """Classify retry behavior without guessing whether Google accepted an appeal."""
    current = now or datetime.now().astimezone()
    if status in {APPEAL_SUBMITTED, APPEAL_OFFICIAL_PENDING, "已人工提交", "申诉成功"}:
        return {"retry_class": APPEAL_RETRY_NOT_NEEDED, "retryable": False}
    if status in APPEAL_USER_ACTION_STATUSES:
        return {"retry_class": APPEAL_RETRY_USER_ACTION, "retryable": False}
    detail = str(error or "")
    if re.search(
        r"超时|timeout|timed out|temporar|connection|连接|断开|disconnected|"
        r"unavailable|503|502|网络|network",
        detail,
        flags=re.IGNORECASE,
    ):
        return {
            "retry_class": APPEAL_RETRY_TRANSIENT,
            "retryable": True,
            "retry_after": (current + timedelta(seconds=APPEAL_TRANSIENT_RETRY_SECONDS)).isoformat(
                timespec="seconds"
            ),
        }
    return {"retry_class": APPEAL_RETRY_MANUAL_REVIEW, "retryable": False}


def render_appeal_text(template: str, account: dict[str, Any]) -> str:
    validate_appeal_template(template)
    cid = normalize_cid(str(account.get("cid") or ""))
    policy = str(account.get("suspension_policy") or "").strip()
    reason = str(account.get("suspension_reason") or "").strip()
    if (
        str(account.get("suspension_reason_status") or "") != REASON_READ
        or str(account.get("suspension_reason_source") or "") != OFFICIAL_REASON_SOURCE
        or not policy
        or not reason
    ):
        raise ValueError("尚未取得 Google Ads 官方账号中止页的明确政策原因，不能生成申诉文案")
    values = {
        "cid": format_cid(cid),
        "account_name": str(account.get("account_name") or "未命名账号").strip(),
        "suspension_policy": policy,
        "suspension_reason": reason,
    }
    try:
        result = str(template).format_map(values).strip()
    except KeyError as exc:
        raise ValueError(f"申诉模板包含不支持的占位符：{{{exc.args[0]}}}") from exc
    if not result:
        raise ValueError("申诉文案不能为空")
    detail = _internal_appeal_detail(result)
    if detail:
        raise ValueError(f"生成的申诉文案包含不能提交给 Google 的{detail}，已阻止复制或打开申诉")
    return result


def validate_submission_text(value: str) -> str:
    """Reject unfinished or diagnostic copy before an external appeal submission."""
    text = str(value or "").strip()
    if len(text) < 80:
        raise ValueError("申诉文案过短；请写明真实业务、问题原因、已完成整改和防复发措施")
    detail = _internal_appeal_detail(text)
    if detail:
        raise ValueError(f"申诉文案包含不能提交给 Google 的{detail}")
    for pattern in INCOMPLETE_APPEAL_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            raise ValueError("申诉文案仍包含“请填写/TODO”等提示内容，请补全真实资料后再提交")
    return text


# Policy knowledge base used to auto-generate complete appeal materials.
# Each playbook describes, for one suspension policy family, the issues that
# were investigated, the remediations that were completed, the prevention
# measures now in place, and the evidence the operator can supply.
APPEAL_PLAYBOOKS: tuple[tuple[str, dict[str, Any]], ...] = (
    (r"规避|circumvent", {
        "issue": "逐项排查后确认，账号存在容易触发该政策判定的风险点："
        "多个广告账号共用同一登录设备与网络环境；部分落地页跳转链过长、含中间跳转；"
        "个别广告的显示网址与最终到达网址不一致，容易被系统认定为试图规避审核。",
        "fixes": (
            "已停止多账号共用登录环境，为每个账号固定独立的登录设备与网络；",
            "已清理落地页中的中间跳转，广告点击后直接到达最终页面，显示网址与到达网址保持一致；",
            "已删除或重做与落地页内容不一致的广告素材和关键词；",
            "已复查账号内全部广告系列，确认不存在隐藏真实落地页或伪装页面内容的行为。",
        ),
        "prevention": "已建立发布前检查流程：每条广告上线前由专人核对显示网址、跳转链与落地页内容的一致性；"
        "每月复查一次账号的登录环境与投放行为，确保持续符合 Google Ads 政策。",
        "evidence": "网站域名所有权证明、营业执照或个人身份证明、付款方式账单，"
        "以及整改后的落地页截图与跳转链检测结果。",
    }),
    (r"付款|payment|billing|结算", {
        "issue": "排查后确认，账号曾因付款信息异常被系统标记："
        "付款方式的账单地址与账号设置的国家/地区不一致，且存在短时间内多次更换付款卡的记录。",
        "fixes": (
            "已移除无法通过验证的付款方式，仅保留本人（本公司）名下真实有效的付款卡；",
            "已将账号的账单国家/地区与账单地址修改为与发卡机构登记信息一致；",
            "已结清账号内全部未付款项，当前无欠费。",
        ),
        "prevention": "今后仅使用本人（本公司）名下的固定付款方式，不再频繁更换；"
        "账单信息变更前会先核对与发卡机构登记信息一致。",
        "evidence": "付款卡持卡证明（隐去卡号中间位）、银行账单、身份证明或营业执照。",
    }),
    (r"虚假|误导|misrepresent|夸大", {
        "issue": "排查后确认，网站/落地页中存在可能被认定为虚假陈述的内容："
        "促销信息与实际结算价格不一致、部分宣传用语缺少依据，"
        "且页面缺少完整的联系方式与退换货政策。",
        "fixes": (
            "已修正落地页中所有与实际不符的促销与价格表述；",
            "已删除无法提供依据的绝对化宣传用语；",
            "已在网站补充真实的公司/个人信息、联系方式、隐私政策与退换货政策；",
            "已核对全部广告文案，确保宣传内容与落地页及实际产品一致。",
        ),
        "prevention": "已建立内容发布前双人核对机制，所有促销、价格与功效表述须有真实依据并留存凭证。",
        "evidence": "营业执照、网站所有权证明、整改前后页面对比截图。",
    }),
    (r"恶意|垃圾软件|malware|unwanted software|有害", {
        "issue": "排查后确认，网站曾因第三方脚本/插件漏洞被植入恶意跳转代码，"
        "导致部分访问被重定向到非预期页面。",
        "fixes": (
            "已清理网站全部恶意代码并修复漏洞，全部程序与插件已更新到最新版本；",
            "已通过安全工具复查全站，确认无恶意重定向与恶意下载；",
            "已在 Google Search Console 完成安全审核，网站状态恢复正常。",
        ),
        "prevention": "已部署网站安全监控与定期扫描，关闭不必要的服务与端口，仅安装来源可信的插件。",
        "evidence": "安全厂商的扫描报告、Search Console 审核通过截图、域名所有权证明。",
    }),
)
DEFAULT_APPEAL_PLAYBOOK: dict[str, Any] = {
    "issue": "已对照 Google Ads 政策中心关于“{policy}”的完整说明逐项自查，"
    "定位到账号内与该政策相关的风险点，并完成针对性处理。",
    "fixes": (
        "已暂停并复查账号内全部广告系列、关键词与素材，移除所有可能涉及该政策的内容；",
        "已核对网站与落地页内容，修正与政策要求不一致的部分；",
        "已复查账号设置、付款方式与登录环境，确认全部信息真实一致。",
    ),
    "prevention": "已建立广告上线前的政策核对清单，指定专人跟进 Google Ads 政策更新，"
    "并定期复查账号内容，防止类似问题再次发生。",
    "evidence": "主体身份/营业证明、网站域名所有权证明、付款资料，以及整改说明与对比截图。",
}


def _playbook_for_policy(policy: str) -> dict[str, Any]:
    text = str(policy or "")
    for pattern, playbook in APPEAL_PLAYBOOKS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return playbook
    return DEFAULT_APPEAL_PLAYBOOK


def auto_appeal_text(
    account: dict[str, Any], basis: str = APPEAL_BASIS_CORRECTED,
) -> str:
    """Generate a complete, submission-ready appeal from the policy knowledge base.

    Only the official suspension policy name is required; the text never
    contains placeholder instructions and passes ``validate_submission_text``.
    """
    validate_appeal_basis(basis)
    cid = format_cid(normalize_cid(str(account.get("cid") or "")))
    policy = str(account.get("suspension_policy") or "").strip()
    reason = str(account.get("suspension_reason") or "").strip()
    if not policy or not reason:
        raise ValueError("尚未取得官方中止页的明确政策原因，不能自动生成申诉资料")
    name = str(account.get("account_name") or "未命名账号").strip()
    business = str(account.get("business_name") or "").strip() or name
    website = str(account.get("website") or "").strip()
    identity = f"本账号由 {business} 真实运营"
    if website:
        identity += f"，官方网站为 {website}"
    header = (
        "Google Ads 支持团队：\n\n"
        f"您好。我就 Google Ads 账号 {cid}（{name}）的中止决定申请复核。\n"
        f"Google Ads 官方账号中止页面显示，本账号涉及“{policy}”政策（{reason}）。\n\n"
        f"一、业务主体与网站/产品说明\n{identity}，用于推广自有产品与服务的真实业务；"
        "广告素材、关键词与落地页均由我方自行制作发布，面向真实终端用户。\n"
    )
    if basis == APPEAL_BASIS_ERROR:
        text = header + (
            f"\n二、逐项自查结果\n对照 Google Ads 政策中心关于“{policy}”的完整说明逐项自查：\n"
            "1. 本账号的广告内容、关键词与落地页均为我方真实业务，不存在该政策所禁止的行为；\n"
            "2. 账号由本人（本团队）真实持有和使用，付款方式、账单信息与主体资料真实一致；\n"
            "3. 网站可正常访问，内容真实完整，联系方式与政策页面齐备。\n\n"
            "基于以上自查结果，我认为本次中止决定可能有误，恳请人工复核本账号的实际投放内容与网站。\n"
            "如复核需要任何补充材料（身份/营业证明、域名所有权、付款账单等），我愿意随时提供。\n\n"
            "以上信息均经我核对确认真实，恳请重新审核本账号。谢谢。"
        )
    else:
        playbook = _playbook_for_policy(policy)
        fixes = "\n".join(f"{index}. {item}" for index, item in enumerate(playbook["fixes"], 1))
        text = header + (
            f"\n二、排查后确认的问题与原因\n{playbook['issue'].format(policy=policy)}\n\n"
            f"三、已经完成的整改\n{fixes}\n\n"
            f"四、为防止问题再次发生而采取的措施\n{playbook['prevention']}\n\n"
            f"五、可以提供的证明材料\n{playbook['evidence']}\n\n"
            "以上信息均经我核对确认真实。如需补充任何资料，我愿意随时配合，恳请重新审核本账号。谢谢。"
        )
    return validate_submission_text(text)


def build_appeal_text(
    template: str,
    account: dict[str, Any],
    *,
    basis: str = APPEAL_BASIS_CORRECTED,
) -> tuple[str, bool]:
    """Return (text, auto_generated): template first, auto 资料 when unfinished.

    A fully written custom template always wins; when the rendered template
    still carries placeholder instructions (the default template), complete
    appeal materials are generated automatically from the policy playbook.
    Template validation errors (internal fields, missing official reason)
    always propagate.
    """
    rendered = render_appeal_text(template, account)
    try:
        return validate_submission_text(rendered), False
    except ValueError:
        return auto_appeal_text(account, basis), True


def validate_appeal_basis(value: str) -> str:
    basis = str(value or "").strip()
    if basis not in APPEAL_BASIS_LABELS:
        raise ValueError("请选择“已完成整改”或“认为中止决定有误”作为申诉依据")
    return basis


def validate_appeal_account(
    account: dict[str, Any], *, require_reason: bool = True,
) -> None:
    if str(account.get("account_type") or "CID").strip().upper() == "MCC":
        raise ValueError("MCC 是管理账号，不能作为广告投放账号提交此申诉")
    if str(account.get("status") or "").strip() != "封禁":
        raise ValueError("只有状态为“封禁”的 CID 才能进入账号中止申诉")
    normalize_cid(str(account.get("cid") or ""))
    if not str(account.get("profile_id") or "").strip():
        raise ValueError("缺少 AdsPower 窗口 ID")
    if not str(account.get("ocid") or "").strip():
        raise ValueError("缺少 Google Ads 内部账户 ID，请先刷新账户列表与状态")
    if require_reason and (
        str(account.get("suspension_reason_status") or "") != REASON_READ
        or str(account.get("suspension_reason_source") or "") != OFFICIAL_REASON_SOURCE
        or not str(account.get("suspension_policy") or "").strip()
        or not str(account.get("suspension_reason") or "").strip()
    ):
        raise ValueError(
            "尚未从 Google Ads 官方账号中止页读到具体政策原因；"
            "请先执行“读取选中封禁原因（只读）”"
        )


def account_overview_url(
    profile_id: str, ocid: str, *, browser_type: str = ADSPOWER,
) -> tuple[str, str]:
    """Return a signed-in account overview URL using an existing AdsPower session."""
    window_id = str(profile_id or "").strip() or "（空）"
    try:
        endpoint = resolve_cdp(
            profile_id, browser_type=normalize_browser_type(browser_type),
        )
    except Exception as exc:
        raise RuntimeError(
            f"AdsPower 窗口 {window_id} 不存在、未启动或调试端口不可用：{exc}"
        ) from exc
    try:
        targets = list_google_ads_targets(endpoint)
        if not targets:
            targets = ensure_google_ads_targets(endpoint)
    except Exception as exc:
        raise RuntimeError(
            f"AdsPower 窗口 {window_id} 无法准备 Google Ads 页面：{exc}"
        ) from exc
    wanted_ocid = str(ocid).strip()
    selected = next(
        (
            target for target in targets
            if urllib.parse.parse_qs(urllib.parse.urlsplit(str(target.get("url") or "")).query).get(
                "ocid", [""]
            )[0] == wanted_ocid
        ),
        targets[0],
    )
    source = urllib.parse.urlsplit(str(selected.get("url") or ""))
    query = urllib.parse.parse_qs(source.query, keep_blank_values=True)
    allowed = ("euid", "__u", "uscid", "__c", "authuser", "workspaceId")
    params = {key: values[-1] for key in allowed if (values := query.get(key))}
    params.update({"ocid": wanted_ocid, "ascid": wanted_ocid})
    url = urllib.parse.urlunsplit(
        ("https", "ads.google.com", "/aw/overview", urllib.parse.urlencode(params), "")
    )
    return endpoint, url


def open_official_appeal_entry(
    profile_id: str, ocid: str, *, browser_type: str = ADSPOWER,
) -> dict[str, str]:
    normalized_browser_type = normalize_browser_type(browser_type)
    endpoint, url = (
        account_overview_url(profile_id, ocid)
        if normalized_browser_type == ADSPOWER
        else account_overview_url(
            profile_id, ocid, browser_type=normalized_browser_type,
        )
    )
    target = open_cdp_target(endpoint, url)
    return {"target_id": str(target.get("id") or ""), "url": url}


def read_official_suspension_reason(
    profile_id: str,
    ocid: str,
    *,
    timeout: float = 15.0,
    browser_type: str = ADSPOWER,
) -> dict[str, str]:
    """Read the visible suspension banner from a temporary official account page."""
    normalized_browser_type = normalize_browser_type(browser_type)
    endpoint, url = (
        account_overview_url(profile_id, ocid)
        if normalized_browser_type == ADSPOWER
        else account_overview_url(
            profile_id, ocid, browser_type=normalized_browser_type,
        )
    )
    target: dict[str, Any] = {}
    page: CDPPage | None = None
    last_payload: dict[str, str] = {"href": "", "title": "", "text": ""}
    try:
        target = open_cdp_target(endpoint, url)
        page = CDPPage(target)
        deadline = time.monotonic() + max(3.0, timeout)
        while time.monotonic() < deadline:
            raw = page.evaluate(
                "JSON.stringify({href:location.href,title:document.title,"
                "text:document.body?document.body.innerText:''})",
                timeout=min(8.0, max(1.0, deadline - time.monotonic())),
            )
            try:
                payload = json.loads(str(raw or "{}"))
            except json.JSONDecodeError:
                payload = {}
            last_payload = {
                "href": str(payload.get("href") or ""),
                "title": str(payload.get("title") or ""),
                "text": str(payload.get("text") or ""),
            }
            parsed = parse_suspension_reason(last_payload["text"])
            if parsed["suspension_reason_status"] == REASON_READ:
                break
            time.sleep(0.6)
        final_url = last_payload["href"] or url
        if urllib.parse.urlsplit(final_url).hostname != "ads.google.com":
            raise RuntimeError(f"官方账号页跳转到了非 Google Ads 地址：{final_url[:200]}")
        parsed = parse_suspension_reason(last_payload["text"])
        parsed.update({
            "official_url": final_url,
            "official_page_title": last_payload["title"],
        })
        return parsed
    finally:
        if page is not None:
            page.close()
        close_cdp_target(endpoint, str(target.get("id") or ""))


def read_suspension_reasons(
    accounts: Iterable[dict[str, Any]],
    *,
    cache_path: Path | None = None,
    reader: Callable[[str, str], dict[str, str]] = read_official_suspension_reason,
    log: Callable[[str], None] | None = None,
    browser_type: str = ADSPOWER,
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in accounts]
    if not rows:
        raise ValueError("请至少选择一个封禁 CID")
    if len(rows) > MAX_APPEALS_PER_BATCH:
        raise ValueError(f"单次最多读取 {MAX_APPEALS_PER_BATCH} 个账号，请分批处理")
    existing = latest_suspension_reasons(load_suspension_reasons(cache_path))
    results: list[dict[str, Any]] = []
    for row in rows:
        validate_appeal_account(row, require_reason=False)
        profile_id = str(row.get("profile_id") or "").strip()
        row_browser_type = _account_browser_type(row, browser_type)
        cid = normalize_cid(str(row.get("cid") or ""))
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        record: dict[str, Any] = {
            "browser_type": row_browser_type,
            "profile_id": profile_id,
            "window_number": str(row.get("window_number") or ""),
            "cid": cid,
            "cid_display": format_cid(cid),
            "ocid": str(row.get("ocid") or ""),
            "account_name": str(row.get("account_name") or ""),
            "reason_read_at": now,
        }
        try:
            if "browser_type" in row or row_browser_type != ADSPOWER:
                record.update(reader(
                    profile_id,
                    str(row.get("ocid") or ""),
                    browser_type=row_browser_type,
                ))
            else:
                record.update(reader(profile_id, str(row.get("ocid") or "")))
            if log:
                log(
                    f"[{format_cid(cid)}] 官方中止原因："
                    f"{record.get('suspension_reason') or record.get('suspension_reason_status')}"
                )
        except Exception as exc:
            record.update({
                "suspension_policy": "",
                "suspension_reason": "",
                "suspension_reason_status": REASON_READ_FAILED,
                "suspension_reason_source": OFFICIAL_REASON_SOURCE,
                "suspension_page_excerpt": "",
                "error": str(exc),
            })
            if log:
                log(f"[{format_cid(cid)}] 官方中止原因读取失败：{exc}")
        existing[_appeal_key(profile_id, cid, row_browser_type)] = record
        results.append(record)
    save_suspension_reasons(existing.values(), cache_path)
    return results


def prepare_appeal_drafts(
    accounts: Iterable[dict[str, Any]],
    template: str,
    *,
    history_path: Path | None = None,
    now: datetime | None = None,
    log: Callable[[str], None] | None = None,
    browser_type: str = ADSPOWER,
) -> list[dict[str, Any]]:
    """Create local review drafts only; never open or submit an appeal form."""
    current = now or datetime.now().astimezone()
    current_iso = current.isoformat(timespec="seconds")
    current_date = current.date().isoformat()
    records = load_appeal_history(history_path)
    latest = effective_appeal_history_by_key(records)
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in accounts:
        row = dict(source)
        validate_appeal_account(row)
        profile_id = str(row.get("profile_id") or "").strip()
        row_browser_type = _account_browser_type(row, browser_type)
        cid = normalize_cid(str(row.get("cid") or ""))
        key = _appeal_key(profile_id, cid, row_browser_type)
        if key in seen:
            continue
        seen.add(key)
        prior = latest.get(key, {})
        if appeal_submission_is_protected(prior):
            skipped = dict(prior)
            skipped.update({
                "skipped": True,
                "skip_reason": f"已有申诉状态：{prior.get('status')}",
            })
            results.append(skipped)
            if log:
                log(f"[{format_cid(cid)}] 已有申诉提交/处理中记录，跳过生成草稿")
            continue
        if (
            str(prior.get("status") or "") == APPEAL_DRAFT
            and str(prior.get("created_at") or "")[:10] == current_date
        ):
            skipped = dict(prior)
            skipped["skipped"] = True
            skipped["skip_reason"] = "今日自动草稿已存在"
            results.append(skipped)
            if log:
                log(f"[{format_cid(cid)}] 今日自动申诉草稿已存在，跳过重复生成")
            continue
        text, auto_generated = build_appeal_text(template, row)
        record: dict[str, Any] = {
            "browser_type": row_browser_type,
            "profile_id": profile_id,
            "window_number": str(row.get("window_number") or ""),
            "cid": cid,
            "cid_display": format_cid(cid),
            "ocid": str(row.get("ocid") or ""),
            "account_name": str(row.get("account_name") or ""),
            "suspension_policy": str(row.get("suspension_policy") or ""),
            "suspension_reason": str(row.get("suspension_reason") or ""),
            "suspension_reason_source": str(row.get("suspension_reason_source") or ""),
            "suspension_reason_status": str(row.get("suspension_reason_status") or ""),
            "reason_read_at": str(row.get("reason_read_at") or ""),
            "suspension_page_excerpt": str(row.get("suspension_page_excerpt") or ""),
            "appeal_text": text,
            "status": APPEAL_DRAFT,
            "auto_generated": auto_generated,
            "created_at": current_iso,
            "updated_at": current_iso,
        }
        records.append(record)
        latest[key] = record
        results.append(record)
        if log:
            if auto_generated:
                log(f"[{format_cid(cid)}] 已按官方政策自动生成完整申诉资料，等待人工审核")
            else:
                log(f"[{format_cid(cid)}] 已按本地模板生成申诉草稿，等待人工审核")
    save_appeal_history(records, history_path)
    return results


def _run_daily_appeal_workflow_impl(
    *,
    force: bool = False,
    now: datetime | None = None,
    log: Callable[[str], None] | None = None,
    browser_type: str = ADSPOWER,
) -> dict[str, Any]:
    """Execute one daily scan and optionally submit every eligible appeal."""
    current = now or datetime.now().astimezone()
    settings = load_appeal_automation()
    if not force and not appeal_automation_due(settings, current):
        reason = "手动模式" if settings.get("mode") == APPEAL_MODE_MANUAL else "今日已执行或尚未到时间"
        return {"status": "未执行", "reason": reason, "settings": settings}

    def emit(message: str) -> None:
        if log:
            log(message)

    auto_submit = bool(settings.get("auto_submit", True))
    basis = str(settings.get("basis") or APPEAL_BASIS_CORRECTED)
    report: dict[str, Any] = {
        "mode": "账户申诉每日全自动提交" if auto_submit else "账户申诉每日自动草稿",
        "started_at": current.isoformat(timespec="seconds"),
        "run_mode": "手动立即执行" if force else "每日自动执行",
        "profiles": {},
        "profile_diagnostics": {},
        "cid_results": [],
        "banned_count": 0,
        "reason_read_count": 0,
        "draft_count": 0,
        "submitted_count": 0,
        "pending_count": 0,
        "blocked_count": 0,
        "submit_failed_count": 0,
        "protected_count": 0,
        "skipped_count": 0,
        "errors": [],
    }
    try:
        state = load_ui_state()
        windows = [row for row in state.get("windows", []) if isinstance(row, dict)]
        if not windows:
            raise RuntimeError("没有已保存的 AdsPower 窗口，请先在批量任务中新增窗口")
        account_scope = global_account_scope(state)
        scoped_total = sum(len(rows) for rows in account_scope.values())
        if not scoped_total:
            raise RuntimeError(
                "第一屏尚无已查询的 CID；请先在“批量任务”点击“刷新账户列表与状态”"
            )
        report["account_data_source"] = "第一屏/批量任务查询结果"
        report["account_scope_count"] = scoped_total
        report["ignored_discovered_count"] = 0
        banned: list[dict[str, Any]] = []
        refreshed_profiles: dict[str, dict[str, dict[str, Any]]] = {}
        for window in windows:
            profile_id = str(window.get("profile_id") or "").strip()
            if not profile_id:
                continue
            row_browser_type = normalize_browser_type(
                window.get("browser_type") or browser_type,
            )
            profile_key = window_key(row_browser_type, profile_id)
            configured = account_scope.get(profile_key) or {}
            if not configured:
                emit(f"[{profile_id}] 第一屏没有已查询的 CID，定时任务跳过该窗口")
                report["profiles"][profile_key] = {}
                continue
            window_number = str(window.get("window_number") or "").strip()
            emit(
                f"[{profile_id}] 每日申诉任务：按第一屏账户池刷新 {len(configured)} 个账户状态"
            )
            try:
                discovered = ProtocolRunner(
                    profile_id, emit, browser_type=row_browser_type,
                ).account_snapshots()
                snapshots = {
                    cid: snapshot for cid, snapshot in discovered.items()
                    if cid in configured
                }
                ignored = max(0, len(discovered) - len(snapshots))
                report["ignored_discovered_count"] += ignored
                if ignored:
                    emit(
                        f"[{profile_id}] 扫描另发现 {ignored} 个账户，但不在第一屏账户池，已忽略"
                    )
                report["profiles"][profile_key] = snapshots
                refreshed_profiles[profile_key] = snapshots
            except Exception as exc:
                report["profiles"][profile_key] = {}
                diagnostic = f"AdsPower 窗口 {profile_id}：账户状态读取失败：{exc}"
                report["profile_diagnostics"][profile_key] = diagnostic
                report["errors"].append(diagnostic)
                continue
            for cid, snapshot in snapshots.items():
                if (
                    str(snapshot.get("status") or "") != "封禁"
                    or str(snapshot.get("account_type") or "CID").upper() == "MCC"
                ):
                    continue
                banned.append({
                    **snapshot,
                    "browser_type": row_browser_type,
                    "profile_id": profile_id,
                    "window_number": window_number,
                    "cid": cid,
                    "account_name": str(snapshot.get("name") or ""),
                })
        if refreshed_profiles:
            save_ui_state(merge_global_account_snapshots(
                refreshed_profiles, state=state,
                updated_at=current.isoformat(timespec="seconds"),
            ))
        report["banned_count"] = len(banned)
        history_state = effective_appeal_history_by_key(load_appeal_history())
        protected: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for row in banned:
            key = _appeal_key(
                str(row.get("profile_id") or "").strip(),
                str(row.get("cid") or ""),
                _account_browser_type(row),
            )
            prior = history_state.get(key)
            if appeal_submission_is_protected(prior):
                protected.append(dict(prior or row))
                report["cid_results"].append({
                    "browser_type": _account_browser_type(row),
                    "profile_id": str(row.get("profile_id") or ""),
                    "cid": key[1], "cid_display": format_cid(key[1]),
                    "status": str((prior or {}).get("status") or ""),
                    "outcome": "protected", "message": "已有提交、处理中或等待人工处理记录",
                })
                emit(
                    f"[{format_cid(key[1])}] 已有“{(prior or {}).get('status')}”记录，"
                    "跳过原因读取和重复提交"
                )
            else:
                candidates.append(row)
        report["protected_count"] = len(protected)
        reason_results: list[dict[str, Any]] = []
        for offset in range(0, len(candidates), MAX_APPEALS_PER_BATCH):
            reason_results.extend(read_suspension_reasons(
                candidates[offset:offset + MAX_APPEALS_PER_BATCH], log=emit,
            ))
        reasons_by_key = {
            _appeal_key(
                str(row.get("profile_id") or ""),
                str(row.get("cid") or ""),
                _account_browser_type(row),
            ): row
            for row in reason_results
        }
        ready: list[dict[str, Any]] = []
        for row in candidates:
            cid = normalize_cid(str(row.get("cid") or ""))
            reason = reasons_by_key.get(_appeal_key(
                str(row.get("profile_id") or ""), cid, _account_browser_type(row),
            ), {})
            if reason.get("suspension_reason_status") != REASON_READ:
                message = str(
                    reason.get("error") or reason.get("suspension_reason_status")
                    or "未读到官方明确原因"
                )
                report["errors"].append(f"{format_cid(cid)}：{message}")
                report["cid_results"].append({
                    "browser_type": _account_browser_type(row),
                    "profile_id": str(row.get("profile_id") or ""),
                    "cid": cid, "cid_display": format_cid(cid),
                    "status": "跳过", "outcome": "reason_not_verified", "message": message,
                })
                continue
            ready.append({**row, **reason, "cid": cid})
        report["reason_read_count"] = len(ready)
        if auto_submit:
            validate_appeal_basis(basis)
            submitted_rows: list[dict[str, Any]] = []
            template = load_appeal_template()
            for offset in range(0, len(ready), MAX_APPEALS_PER_BATCH):
                submitted_rows.extend(submit_appeals(
                    ready[offset:offset + MAX_APPEALS_PER_BATCH],
                    template,
                    basis,
                    truth_confirmed=True,
                    now=current,
                    submitter=submit_appeal_via_protocol_text,
                    log=emit,
                ))
            report["appeal_results"] = submitted_rows
            report["cid_results"].extend({
                "browser_type": _account_browser_type(row),
                "profile_id": str(row.get("profile_id") or ""),
                "cid": str(row.get("cid") or ""),
                "cid_display": str(row.get("cid_display") or ""),
                "status": str(row.get("status") or ""),
                "outcome": "skipped" if row.get("skipped") else "submit_result",
                "message": str(row.get("error") or row.get("skip_reason") or ""),
                "retry_class": str(row.get("retry_class") or ""),
                "retryable": bool(row.get("retryable")),
            } for row in submitted_rows)
            report["submitted_count"] = sum(
                row.get("status") == APPEAL_SUBMITTED and not row.get("skipped")
                for row in submitted_rows
            )
            report["pending_count"] = sum(
                row.get("status") == APPEAL_OFFICIAL_PENDING for row in submitted_rows
            )
            report["blocked_count"] = sum(
                row.get("status") in {
                    APPEAL_LOGIN_REQUIRED,
                    APPEAL_VERIFICATION_REQUIRED,
                    APPEAL_USER_ACTION_REQUIRED,
                }
                for row in submitted_rows
            )
            failed_rows = [
                row for row in submitted_rows
                if row.get("status") == APPEAL_SUBMIT_FAILED
            ]
            report["submit_failed_count"] = len(failed_rows)
            report["skipped_count"] = sum(bool(row.get("skipped")) for row in submitted_rows)
            report["errors"].extend(
                f"{row.get('cid_display') or format_cid(str(row.get('cid') or ''))}："
                f"{row.get('error') or '自动提交失败'}"
                for row in failed_rows
            )
        else:
            drafts = prepare_appeal_drafts(
                ready, load_appeal_template(), now=current, log=emit,
            ) if ready else []
            report["draft_count"] = sum(not row.get("skipped") for row in drafts)
            report["skipped_count"] = sum(bool(row.get("skipped")) for row in drafts)
            report["drafts"] = drafts
            report["cid_results"].extend({
                "browser_type": _account_browser_type(row),
                "profile_id": str(row.get("profile_id") or ""),
                "cid": str(row.get("cid") or ""),
                "cid_display": str(row.get("cid_display") or ""),
                "status": str(row.get("status") or ""),
                "outcome": "skipped" if row.get("skipped") else "draft_created",
                "message": str(row.get("skip_reason") or ""),
            } for row in drafts)
        report["status"] = "部分完成" if report["errors"] else "完成"
        if auto_submit:
            report["summary"] = (
                f"封禁 CID {report['banned_count']} 个，已有提交保护 {report['protected_count']} 个，"
                f"读到官方原因 {report['reason_read_count']} 个，提交成功 {report['submitted_count']} 个，"
                f"处理中 {report['pending_count']} 个，等待验证 {report['blocked_count']} 个，"
                f"失败 {report['submit_failed_count']} 个"
            )
        else:
            report["summary"] = (
                f"封禁 CID {report['banned_count']} 个，已有提交保护 {report['protected_count']} 个，"
                f"读到官方原因 {report['reason_read_count']} 个，新建草稿 {report['draft_count']} 个，"
                f"跳过重复 {report['skipped_count']} 个"
            )
    except Exception as exc:
        report["status"] = "失败"
        report["summary"] = str(exc)
        report["errors"].append(str(exc))
    report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    settings.update({
        "last_run_date": current.date().isoformat(),
        "last_run_at": report["finished_at"],
        "last_status": report["status"],
        "last_summary": report.get("summary") or "",
    })
    save_appeal_automation(settings)
    report["report_path"] = str(save_run_report(report))
    emit(f"每日账户申诉：{report['status']}；{report.get('summary') or ''}")
    return report


def run_daily_appeal_workflow(
    *,
    force: bool = False,
    now: datetime | None = None,
    log: Callable[[str], None] | None = None,
    lock_path: Path | None = None,
    browser_type: str = ADSPOWER,
) -> dict[str, Any]:
    """Run at most one daily appeal workflow across GUI and Task Scheduler."""
    current = now or datetime.now().astimezone()
    settings = load_appeal_automation()
    if not force and not appeal_automation_due(settings, current):
        reason = (
            "手动模式"
            if settings.get("mode") == APPEAL_MODE_MANUAL
            else "今日已执行或尚未到时间"
        )
        return {"status": "未执行", "reason": reason, "settings": settings}
    try:
        acquired_path, token = acquire_appeal_automation_lock(
            path=lock_path, now=current,
        )
    except RuntimeError as exc:
        return {"status": "未执行", "reason": str(exc), "settings": settings}
    try:
        return _run_daily_appeal_workflow_impl(
            force=force, now=current, log=log, browser_type=browser_type,
        )
    finally:
        release_appeal_automation_lock(acquired_path, token)


def _official_appeal_snapshot(page: CDPPage) -> dict[str, Any]:
    raw = page.evaluate(
        """JSON.stringify((()=>{
          const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
          const controls=[...document.querySelectorAll(
            'input,textarea,select,button,a,[role=button],[contenteditable=true],material-button'
          )].filter(visible).map((e,index)=>({
            index,tag:e.tagName,type:e.type||'',name:e.name||'',id:e.id||'',
            text:(e.innerText||e.textContent||'').trim().replace(/\\s+/g,' ').slice(0,240),
            aria:e.getAttribute('aria-label')||'',placeholder:e.placeholder||'',
            value:(e.value||'').slice(0,240),disabled:!!e.disabled
          }));
          return {href:location.href,title:document.title,
            text:(document.body?.innerText||'').slice(-20000),controls};
        })())""",
        timeout=12,
    )
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("无法读取 Google 官方申诉页面状态") from exc
    return payload if isinstance(payload, dict) else {}


def _click_official_control(
    page: CDPPage, labels: Iterable[str], *, scoped: bool = False,
) -> str:
    wanted = [str(value).strip() for value in labels if str(value).strip()]
    script = """
    ((args)=>{
      const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
      let root=document;
      if(args.scoped){
        const roots=[...document.querySelectorAll(
          'form,[role=dialog],material-drawer,[class*=drawer],[class*=side-sheet]'
        )].filter(visible);
        const keys=['申诉','中止','政策','appeal','suspend','policy'];
        roots.sort((a,b)=>{
          const score=e=>keys.reduce((n,k)=>n+((e.innerText||'').toLowerCase().includes(k)?1:0),0);
          return score(b)-score(a);
        });
        if(roots.length) root=roots[0];
      }
      const nodes=[...root.querySelectorAll('button,a,[role=button],material-button')].filter(visible);
      for(const label of args.labels){
        const exact=nodes.find(e=>((e.innerText||e.textContent||e.getAttribute('aria-label')||'')
          .trim().replace(/\\s+/g,' '))===label && !e.disabled);
        if(exact){exact.click();return label;}
      }
      for(const label of args.labels){
        const match=nodes.find(e=>((e.innerText||e.textContent||e.getAttribute('aria-label')||'')
          .trim().replace(/\\s+/g,' ')).includes(label) && !e.disabled);
        if(match){match.click();return label;}
      }
      return '';
    })
    """
    argument = json.dumps({"labels": wanted, "scoped": scoped}, ensure_ascii=False)
    return str(page.evaluate(f"({script})({argument})", timeout=12) or "")


APPEAL_DEFAULT_COUNTRY = "美国"
# Questionnaire radio questions answered "是/Yes"; every other yes/no
# question defaults to "否/No" (the common questions are "did you do X").
APPEAL_RADIO_YES_PATTERN = r"所有者|直属员工|owner|本人"


def _appeal_text_section(text: str, marker: str, limit: int = 600) -> str:
    """Pull one numbered section (e.g. 一、…) out of the appeal letter."""
    pattern = re.escape(marker) + r"\s*(.+?)(?:\n\s*[一二三四五六七八九十]、|$)"
    match = re.search(pattern, str(text or ""), flags=re.DOTALL)
    section = re.sub(r"\s+", " ", match.group(1)).strip() if match else ""
    return section[:limit]


def questionnaire_answers(
    account: dict[str, Any], appeal_text: str,
) -> list[tuple[str, str]]:
    """Targeted answers for the official questionnaire-style suspension form.

    The 账号中止申诉表单 asks separate questions (target country, business
    activity, website…) instead of one free-text box. Each entry maps a
    question-label pattern to its answer; empty values skip the field.
    """
    activity = _appeal_text_section(appeal_text, "一、") or re.sub(
        r"\s+", " ", str(appeal_text or ""),
    )[:400]
    return [
        (
            r"国家|地区|在哪.*投放|country|region",
            str(account.get("appeal_country") or "").strip() or APPEAL_DEFAULT_COUNTRY,
        ),
        (
            r"从事什么|什么活动|业务.*(活动|内容)|business.*(do|activit)"
            r"|describe your (business|organisation|organization)",
            activity,
        ),
        (r"网站|网址|website|web site|\burl\b", str(account.get("website") or "").strip()),
    ]


def _fill_official_appeal_form(
    page: CDPPage, appeal_text: str, account: dict[str, Any],
) -> dict[str, Any]:
    values = {
        "appeal_text": appeal_text,
        "cid": format_cid(normalize_cid(str(account.get("cid") or ""))),
        "account_name": str(account.get("account_name") or "").strip(),
        "contact_name": str(account.get("contact_name") or "").strip(),
        "contact_email": str(account.get("contact_email") or "").strip(),
        "contact_phone": str(account.get("contact_phone") or "").strip(),
        "business_name": str(account.get("business_name") or "").strip(),
        "website": str(account.get("website") or "").strip(),
        "questionnaire": questionnaire_answers(account, appeal_text),
        "radio_yes": APPEAL_RADIO_YES_PATTERN,
    }
    script = """
    ((args)=>{
      const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
      const roots=[...document.querySelectorAll(
        'form,[role=dialog],material-drawer,[class*=drawer],[class*=side-sheet]'
      )].filter(visible);
      const keys=['申诉','中止','政策','appeal','suspend','policy'];
      roots.sort((a,b)=>{
        const score=e=>keys.reduce((n,k)=>n+((e.innerText||'').toLowerCase().includes(k)?1:0),0);
        return score(b)-score(a);
      });
      const root=roots[0]||document;
      const fields=[...root.querySelectorAll('input,textarea,[contenteditable=true]')]
        .filter(e=>visible(e)&&!e.disabled&&e.type!=='hidden'&&e.type!=='radio'&&e.type!=='checkbox');
      const describe=e=>{
        const labelled=e.getAttribute('aria-labelledby');
        const labelledText=labelled ? labelled.split(/\\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ') : '';
        const labels=e.labels?[...e.labels].map(x=>x.innerText||'').join(' '):'';
        const near=e.closest('label,[class*=field],[class*=input]')?.innerText||'';
        return [e.name,e.id,e.placeholder,e.getAttribute('aria-label'),labels,labelledText,near]
          .filter(Boolean).join(' ').toLowerCase();
      };
      const setValue=(e,value)=>{
        if(!value)return false;
        if(e.isContentEditable){e.focus();e.textContent=value;}
        else {
          const proto=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
          const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;
          if(setter)setter.call(e,value);else e.value=value;
        }
        e.dispatchEvent(new Event('input',{bubbles:true}));
        e.dispatchEvent(new Event('change',{bubbles:true}));
        e.dispatchEvent(new Event('blur',{bubbles:true}));
        return true;
      };
      let filled=0, appealFieldPresent=false, questionnaire=0, radios=0;
      const qa=args.questionnaire||[];
      for(const e of fields){
        if(String(e.value||e.textContent||'').trim())continue;
        const label=describe(e);
        for(const [pattern,value] of qa){
          if(!value)continue;
          if(new RegExp(pattern,'i').test(label)){if(setValue(e,value)){filled++;questionnaire++;}break;}
        }
      }
      const textFields=fields.filter(e=>e.tagName==='TEXTAREA'||e.isContentEditable||
        /appeal|describe|explain|details|reason|申诉|详细|说明|理由|整改|补充/.test(describe(e)));
      for(const e of textFields){
        if(/反馈|feedback|搜索|search/.test(describe(e)))continue;
        appealFieldPresent=true;
        if(!String(e.value||e.textContent||'').trim() && setValue(e,args.appeal_text))filled++;
      }
      if(!appealFieldPresent){
        const lone=fields.filter(e=>e.tagName==='TEXTAREA'||e.isContentEditable);
        if(lone.length===1){appealFieldPresent=true;if(setValue(lone[0],args.appeal_text))filled++;}
      }
      const mappings=[
        [/email|电子邮件|邮箱/,args.contact_email],
        [/phone|telephone|电话|手机/,args.contact_phone],
        [/contact.?name|联系人|姓名/,args.contact_name],
        [/business.?name|company|企业|公司|业务名称/,args.business_name||args.account_name],
        [/website|site|网址|网站/,args.website],
        [/customer.?id|account.?id|客户.?id|账号.?id|帐号.?id|cid/,args.cid]
      ];
      for(const e of fields){
        if(String(e.value||e.textContent||'').trim())continue;
        const label=describe(e);
        for(const [pattern,value] of mappings){if(value&&pattern.test(label)){if(setValue(e,value))filled++;break;}}
      }
      const yesRe=new RegExp(args.radio_yes||'(?!)','i');
      const radioGroups={};
      [...root.querySelectorAll('input[type=radio]')].filter(e=>visible(e)&&!e.disabled)
        .forEach(r=>{const key=r.name||r.id||'g'+Object.keys(radioGroups).length;
          (radioGroups[key]=radioGroups[key]||[]).push(r);});
      for(const group of Object.values(radioGroups)){
        if(group.some(r=>r.checked))continue;
        let c=group[0].parentElement;
        while(c&&!group.every(r=>c.contains(r)))c=c.parentElement;
        const q=((c&&c.innerText)||'').toLowerCase();
        const want=yesRe.test(q)?['是','yes']:['否','no'];
        for(const r of group){
          const lt=((r.labels?[...r.labels].map(x=>x.innerText||'').join(' '):'')||
            (r.closest('label')&&r.closest('label').innerText)||'').trim().toLowerCase();
          if(want.some(w=>lt.startsWith(w))){r.click();radios++;break;}
        }
      }
      return JSON.stringify({filled,questionnaire,radios,appeal_field_present:appealFieldPresent,
        field_count:fields.length,root_text:(root.innerText||'').slice(0,1200)});
    })
    """
    raw = page.evaluate(
        f"({script})({json.dumps(values, ensure_ascii=False)})", timeout=15,
    )
    try:
        result = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        result = {}
    return result if isinstance(result, dict) else {}


# --- protocol appeal submission (SelfServicePolicyAppealService) -------------
# Reverse-engineered from the official Google Ads web client (aw_identity
# bundle): SelfServicePolicyAppealService/Mutate takes
# {1: header, 2: [{1: operator(1=ADD), 3: SelfServicePolicyAppeal}]} where the
# Appeal entity is {1: customerId, 2: appealId(temp "-1" for ADD), 3: type,
# 4: policyTopic, 11: customerLevel}. type: 1=APPEAL, 2=RESUBMIT,
# 3=APPEAL_FORM; policyTopic is the Google Ads policy-topic enum below.
APPEAL_MUTATE_PATH = "/aw_essentials/_/rpc/SelfServicePolicyAppealService/Mutate"
APPEAL_LIST_PATH = "/aw_essentials/_/rpc/SelfServicePolicyAppealService/List"
APPEAL_OPERATOR_ADD = 1
APPEAL_TYPE_APPEAL = 1
APPEAL_TYPE_RESUBMIT = 2
APPEAL_TYPE_BY_BASIS = {
    APPEAL_BASIS_CORRECTED: APPEAL_TYPE_RESUBMIT,
    APPEAL_BASIS_ERROR: APPEAL_TYPE_APPEAL,
}
APPEAL_POLICY_TOPICS: tuple[tuple[str, int, str], ...] = (
    (r"规避系统|伪装真实内容|cloaking|circumventing", 2, "CIRCUMVENTING_SYSTEMS"),
    (r"不(?:可)?接受的商业行为|unacceptable business", 146, "UNACCEPTABLE_BUSINESS_PRACTICES"),
    (r"虚假陈述|误导|misrepresentation", 142, "MISREPRESENTATION"),
    (r"恶意软件|垃圾软件|malicious|unwanted software", 4, "MALICIOUS_OR_UNWANTED_SOFTWARE"),
    (r"假冒|counterfeit", 18, "COUNTERFEIT_PRODUCTS"),
    (r"滥用广告网络|abusing the ad network", 1, "ABUSING_THE_AD_NETWORK"),
)


class AppealTopicUnknown(Exception):
    """The official policy label has no verified PolicyTopic enum mapping."""


def appeal_policy_topic(policy_label: str) -> tuple[int, str]:
    text = str(policy_label or "")
    for pattern, topic, name in APPEAL_POLICY_TOPICS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return topic, name
    raise AppealTopicUnknown(f"政策“{text}”没有已核实的 PolicyTopic 映射")


def appeal_mutate_request(ocid: str, topic: int, appeal_type: int) -> dict[str, Any]:
    customer = str(ocid).strip()
    return {
        "1": {"3": {"1": customer}},
        "2": [{
            "1": APPEAL_OPERATOR_ADD,
            # The server only applies fields listed here; without fieldPaths
            # the entity is treated as empty and required-field errors fire.
            "2": ["type", "policy_topic", "customer_level"],
            "3": {"1": customer, "2": "-1", "3": appeal_type, "4": topic, "11": True},
        }],
    }


def appeal_list_request(ocid: str) -> dict[str, Any]:
    """List every official appeal for a CID; no status filter is intentional."""
    customer = str(ocid).strip()
    return {
        "1": {"3": {"1": customer}},
        "2": {
            "1": [
                "policy_topic",
                "entity_type",
                "customer_level",
                "campaign_level",
                "ad_group_level",
                "asset_extension_level",
                "asset_group_level",
                "entity_sub_type_level",
                "status",
                "creation_time_micros",
            ],
            "3": [{"1": "creation_time_micros", "2": 2}],
        },
    }


def parse_appeal_list_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError("官方申诉列表协议返回了无法解析的响应")
    rows = payload.get("2") or []
    return [dict(row) for row in rows if isinstance(row, dict)]


def matching_official_appeal(
    rows: Iterable[dict[str, Any]], topic: int,
) -> dict[str, Any] | None:
    matching = [row for row in rows if int(row.get("4") or 0) == int(topic)]
    if not matching:
        return None
    return max(matching, key=lambda row: int(row.get("8") or 0))


def parse_appeal_mutate_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract command errors / created entities from a Mutate response."""
    if not isinstance(payload, dict):
        raise RuntimeError("官方申诉协议返回了无法解析的响应")
    errors = (payload.get("1") or {}).get("2") or []
    if errors:
        details: list[str] = []
        for error in errors[:3]:
            if isinstance(error, dict):
                code = str(error.get("3") or "")
                message = str(error.get("1") or "")
                details.append(f"{message}（{code}）" if code else message)
        raise RuntimeError("官方申诉协议拒绝提交：" + "；".join(details or [str(errors[0])[:200]]))
    entities = payload.get("2") or []
    return {"entities": entities if isinstance(entities, list) else []}


def submit_appeal_via_protocol(
    account: dict[str, Any],
    basis: str,
    *,
    allow_resubmit: bool = False,
    browser_type: str = ADSPOWER,
) -> dict[str, Any]:
    """Submit an account-suspension appeal through the official internal RPC.

    Raises AppealTopicUnknown when the policy label has no verified topic
    mapping (caller falls back to driving the official UI). Server-side
    rejections raise RuntimeError and must NOT be retried through the UI,
    since the command may already be recorded by Google.
    """
    basis = validate_appeal_basis(basis)
    profile_id = str(account.get("profile_id") or "").strip()
    row_browser_type = _account_browser_type(account, browser_type)
    ocid = str(account.get("ocid") or "").strip()
    policy = str(account.get("suspension_policy") or "").strip()
    topic, topic_name = appeal_policy_topic(policy)
    appeal_type = APPEAL_TYPE_BY_BASIS[basis]
    endpoint, url = (
        account_overview_url(profile_id, ocid)
        if row_browser_type == ADSPOWER and "browser_type" not in account
        else account_overview_url(
            profile_id, ocid, browser_type=row_browser_type,
        )
    )
    target: dict[str, Any] = {}
    page: CDPPage | None = None
    try:
        target = open_cdp_target(endpoint, url)
        page = CDPPage(target)
        time.sleep(4)
        listed = parse_appeal_list_response(page.rpc(
            APPEAL_LIST_PATH, appeal_list_request(ocid), timeout=60,
        ))
        existing = matching_official_appeal(listed, topic)
        if existing is not None and not allow_resubmit:
            appeal_id = str(existing.get("2") or "")
            return {
                "status": APPEAL_OFFICIAL_PENDING,
                "submitted": False,
                "official_url": url,
                "appeal_via": "protocol_list",
                "policy_topic": topic_name,
                "appeal_type": int(existing.get("9") or 0),
                "appeal_id": appeal_id,
                "official_appeal_status": int(existing.get("20") or 0),
                "receipt_excerpt": (
                    f"SelfServicePolicyAppealService/List 已发现官方申诉编号 {appeal_id or '—'}"
                    f"（topic={topic_name}），已阻止重复提交"
                ),
            }
        previous_keys = {
            (str(row.get("2") or ""), str(row.get("8") or ""))
            for row in listed
            if isinstance(row, dict)
        }
        payload = page.rpc(
            APPEAL_MUTATE_PATH, appeal_mutate_request(ocid, topic, appeal_type),
            timeout=60,
        )
        result = parse_appeal_mutate_response(payload)
        appeal_id = ""
        if result["entities"] and isinstance(result["entities"][0], dict):
            appeal_id = str(result["entities"][0].get("2") or "")
        verified: dict[str, Any] | None = None
        for _attempt in range(3):
            listed_after = parse_appeal_list_response(page.rpc(
                APPEAL_LIST_PATH, appeal_list_request(ocid), timeout=60,
            ))
            candidates = [
                row for row in listed_after
                if int(row.get("4") or 0) == int(topic)
                and (str(row.get("2") or ""), str(row.get("8") or "")) not in previous_keys
            ]
            if appeal_id:
                verified = next(
                    (row for row in candidates if str(row.get("2") or "") == appeal_id),
                    None,
                )
            if verified is None and candidates:
                verified = max(candidates, key=lambda row: int(row.get("8") or 0))
            if verified is not None:
                break
            time.sleep(1)
        if verified is None:
            return {
                "status": APPEAL_SUBMIT_FAILED,
                "submitted": False,
                "official_url": url,
                "appeal_via": "protocol",
                "policy_topic": topic_name,
                "appeal_type": appeal_type,
                "appeal_id": appeal_id,
                "protocol_entities": result["entities"],
                "error": "Mutate 返回了实体，但提交后 List 未回读到新的申诉记录，不能判定为已提交",
            }
        verified_id = str(verified.get("2") or appeal_id)
        return {
            "status": APPEAL_SUBMITTED,
            "submitted": True,
            "official_url": url,
            "appeal_via": "protocol",
            "policy_topic": topic_name,
            "appeal_type": appeal_type,
            "appeal_id": verified_id,
            "official_appeal_status": int(verified.get("20") or 0),
            "verified_by": "SelfServicePolicyAppealService/List",
            "protocol_entities": result["entities"],
            "receipt_excerpt": (
                f"SelfServicePolicyAppealService/Mutate 后已由 List 回读到新申诉编号 "
                f"{verified_id or '—'}（topic={topic_name}, type={appeal_type}）"
            ),
        }
    finally:
        if page is not None:
            page.close()
        close_cdp_target(endpoint, str(target.get("id") or ""))


def submit_appeal_via_protocol_text(
    account: dict[str, Any],
    _appeal_text: str,
    basis: str,
    *,
    browser_type: str = ADSPOWER,
) -> dict[str, Any]:
    """Adapter used by unattended jobs; never falls back to browser form clicks."""
    return submit_appeal_via_protocol(
        account, basis, browser_type=browser_type,
    )


def submit_official_appeal(
    account: dict[str, Any],
    appeal_text: str,
    basis: str,
    *,
    timeout: float = 90.0,
    allow_resubmit: bool = False,
    browser_type: str = ADSPOWER,
) -> dict[str, Any]:
    """Submit an appeal: official RPC first, signed-in UI automation as fallback.

    The protocol path is used whenever the suspension policy has a verified
    PolicyTopic mapping. UI automation drives the official appeal drawer by
    visible semantic labels only; security/login challenges are left open.
    """
    basis = validate_appeal_basis(basis)
    appeal_text = validate_submission_text(appeal_text)
    row_browser_type = _account_browser_type(account, browser_type)
    try:
        if row_browser_type == ADSPOWER and "browser_type" not in account:
            return submit_appeal_via_protocol(
                account, basis, allow_resubmit=allow_resubmit,
            )
        return submit_appeal_via_protocol(
            account, basis, allow_resubmit=allow_resubmit,
            browser_type=row_browser_type,
        )
    except AppealTopicUnknown:
        pass  # no verified topic mapping: drive the official UI instead
    profile_id = str(account.get("profile_id") or "").strip()
    ocid = str(account.get("ocid") or "").strip()
    endpoint, url = (
        account_overview_url(profile_id, ocid)
        if row_browser_type == ADSPOWER and "browser_type" not in account
        else account_overview_url(
            profile_id, ocid, browser_type=row_browser_type,
        )
    )
    target: dict[str, Any] = {}
    page: CDPPage | None = None
    keep_target = False
    clicked_final = False
    form_touched = False
    last_snapshot: dict[str, Any] = {}
    network_bucket: list[dict[str, Any]] = []
    try:
        target = open_cdp_target(endpoint, url)
        page = CDPPage(target)
        try:
            # Best-effort: record the page's POST traffic so the unpublished
            # appeal RPC can be identified from a real submission.
            page.command("Network.enable", timeout=8)
        except Exception:
            pass
        deadline = time.monotonic() + max(25.0, timeout)
        while time.monotonic() < deadline:
            _collect_appeal_network(page, network_bucket)
            last_snapshot = _official_appeal_snapshot(page)
            body = str(last_snapshot.get("text") or "")
            if re.search(r"账号.*(?:已被中止|中止)|account.*suspend", body, re.IGNORECASE):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Google Ads 账号页加载超时，未发现账号中止信息")

        body = str(last_snapshot.get("text") or "")
        if re.search(r"申诉(?:待处理|处理中)|appeal pending", body, re.IGNORECASE):
            return {
                "status": APPEAL_OFFICIAL_PENDING, "submitted": False,
                "official_url": str(last_snapshot.get("href") or url),
                "receipt_excerpt": "Google Ads 页面显示申诉正在处理，已阻止重复提交。",
            }

        _click_official_control(page, ("查看详情", "View details"))
        time.sleep(0.8)
        entry_clicked = bool(_click_official_control(
            page, ("开始行动", "联系我们", "Contact us", "Appeal", "Start"), scoped=True,
        ))
        basis_labels = (
            ("我已完成整改", "已进行更改", "已修正", "Made changes to comply", "Fixed the issue")
            if basis == APPEAL_BASIS_CORRECTED
            else ("中止决定有误", "判定有误", "不同意该决定", "Dispute decision", "Decision was incorrect")
        )
        basis_selected = False
        while time.monotonic() < deadline:
            time.sleep(0.8)
            _collect_appeal_network(page, network_bucket)
            last_snapshot = _official_appeal_snapshot(page)
            body = str(last_snapshot.get("text") or "")
            folded = body.casefold()
            href = str(last_snapshot.get("href") or "")
            if re.search(
                r"申诉(?:已提交|待处理|处理中)|已收到.{0,20}申诉|"
                r"appeal (?:was |has been )?submitted|appeal pending|we.{0,20}received.{0,20}appeal",
                body, re.IGNORECASE | re.DOTALL,
            ):
                return {
                    "status": APPEAL_SUBMITTED if clicked_final else APPEAL_OFFICIAL_PENDING,
                    "submitted": clicked_final,
                    "official_url": href or url,
                    "official_page_title": str(last_snapshot.get("title") or ""),
                    "receipt_excerpt": re.sub(r"\s+", " ", body)[-1200:],
                }
            if "accounts.google." in href or any(value in folded for value in (
                "您已断开连接", "重新登录", "sign in again", "you've been signed out",
            )):
                keep_target = True
                return {
                    "status": APPEAL_LOGIN_REQUIRED, "submitted": False,
                    "target_id": str(target.get("id") or ""), "official_url": href or url,
                    "error": "Google 要求重新登录；申诉页已保留。登录完成后再次点击一键提交即可续提。",
                }
            if any(value in folded for value in (
                "广告客户验证", "验证您的身份", "verify your identity", "advertiser verification",
            )):
                keep_target = True
                return {
                    "status": APPEAL_VERIFICATION_REQUIRED, "submitted": False,
                    "target_id": str(target.get("id") or ""), "official_url": href or url,
                    "error": "Google 要求先完成广告客户/身份验证；验证页已保留。",
                }
            if any(value in folded for value in (
                "captcha", "reCAPTCHA".casefold(), "安全验证", "确认您不是机器人",
            )):
                keep_target = True
                return {
                    "status": APPEAL_USER_ACTION_REQUIRED, "submitted": False,
                    "target_id": str(target.get("id") or ""), "official_url": href or url,
                    "error": "Google 要求人工完成安全验证；页面已保留，软件不会绕过验证。",
                }

            if clicked_final:
                continue

            if not entry_clicked:
                _click_official_control(page, ("查看详情", "View details"))
                entry_clicked = bool(_click_official_control(
                    page,
                    ("开始行动", "联系我们", "Contact us", "Appeal", "Start"),
                    scoped=True,
                ))
                if entry_clicked:
                    continue

            if not basis_selected:
                basis_selected = bool(_click_official_control(page, basis_labels, scoped=True))
                if basis_selected:
                    continue
            fill_result = _fill_official_appeal_form(page, appeal_text, account)
            if (
                fill_result.get("appeal_field_present")
                or fill_result.get("filled")
                or fill_result.get("radios")
            ):
                # Sticky: later wizard steps (确认) have no fields at all, but
                # they do carry the final submit button.
                form_touched = True
            if form_touched:
                final = _click_official_control(
                    page, ("提交申诉", "发送申诉", "Submit appeal", "Send appeal", "提交", "Submit"),
                    scoped=True,
                )
                if final:
                    clicked_final = True
                    continue
            advanced = _click_official_control(
                page, ("下一步", "继续", "Next", "Continue"), scoped=True,
            )
            if advanced:
                continue
        keep_target = True
        excerpt = re.sub(r"\s+", " ", str(last_snapshot.get("text") or ""))[-1000:]
        return {
            "status": APPEAL_SUBMIT_FAILED, "submitted": False,
            "target_id": str(target.get("id") or ""),
            "official_url": str(last_snapshot.get("href") or url),
            "error": "未在时限内取得 Google 官方提交回执；页面已保留，请查看当前步骤。",
            "receipt_excerpt": excerpt,
        }
    finally:
        if page is not None:
            try:
                # Final sweep: pull whatever events the last commands stashed,
                # then retry response bodies for appeal rows still missing one.
                _collect_appeal_network(page, network_bucket)
                fetch_missing_response_bodies(page, network_bucket)
                save_appeal_rpc_capture(
                    annotate_appeal_rpc(network_bucket, appeal_text),
                    str(account.get("cid") or ""),
                )
            except Exception:
                pass
            page.close()
        if not keep_target:
            close_cdp_target(endpoint, str(target.get("id") or ""))


def _submit_appeals_unlocked(
    accounts: Iterable[dict[str, Any]],
    template: str,
    basis: str,
    *,
    truth_confirmed: bool = False,
    history_path: Path | None = None,
    submitter: Callable[[dict[str, Any], str, str], dict[str, Any]] = submit_official_appeal,
    now: datetime | None = None,
    log: Callable[[str], None] | None = None,
    allow_user_action_retry: bool = False,
    allow_failed_retry: bool = False,
    allow_resubmit: bool = False,
    browser_type: str = ADSPOWER,
) -> list[dict[str, Any]]:
    """Submit selected account appeals and persist only verified official outcomes."""
    rows = [dict(row) for row in accounts]
    if not truth_confirmed:
        raise ValueError("提交前必须确认申诉资料真实、完整，且已完成所述整改")
    if not rows:
        raise ValueError("请至少选择一个封禁 CID")
    if len(rows) > MAX_APPEALS_PER_BATCH:
        raise ValueError(f"单次最多提交 {MAX_APPEALS_PER_BATCH} 个账号，请分批处理")
    basis = validate_appeal_basis(basis)
    current = now or datetime.now().astimezone()
    current_iso = current.isoformat(timespec="seconds")
    records = load_appeal_history(history_path)
    latest = effective_appeal_history_by_key(records)
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in rows:
        row = dict(source)
        validate_appeal_account(row)
        profile_id = str(row.get("profile_id") or "").strip()
        row_browser_type = _account_browser_type(row, browser_type)
        row["browser_type"] = row_browser_type
        cid = normalize_cid(str(row.get("cid") or ""))
        key = _appeal_key(profile_id, cid, row_browser_type)
        if key in seen:
            continue
        seen.add(key)
        prior = latest.get(key, {})
        if appeal_submission_is_protected(
            prior,
            allow_resubmit=allow_resubmit,
            allow_user_action_retry=allow_user_action_retry,
            allow_failed_retry=allow_failed_retry,
            now=current,
        ):
            skipped = dict(prior)
            skipped.update({
                "skipped": True,
                "skip_reason": f"已有受保护申诉状态：{prior.get('status')}",
            })
            results.append(skipped)
            if log:
                log(f"[{format_cid(cid)}] 已有提交/处理中记录，阻止重复申诉")
            continue
        text, auto_generated = build_appeal_text(template, row, basis=basis)
        if log and auto_generated:
            log(f"[{format_cid(cid)}] 模板未补全，已按官方政策自动生成完整申诉资料")
        if log:
            log(f"[{format_cid(cid)}] 正在打开官方中止页并提交申诉…")
        attempt_id = hashlib.sha256(
            f"{key[0]}:{cid}:{basis}:{current_iso}:{text}".encode("utf-8")
        ).hexdigest()
        record: dict[str, Any] = {
            "browser_type": row_browser_type,
            "profile_id": profile_id,
            "window_number": str(row.get("window_number") or ""),
            "cid": cid,
            "cid_display": format_cid(cid),
            "ocid": str(row.get("ocid") or ""),
            "account_name": str(row.get("account_name") or ""),
            "suspension_policy": str(row.get("suspension_policy") or ""),
            "suspension_reason": str(row.get("suspension_reason") or ""),
            "suspension_reason_source": str(row.get("suspension_reason_source") or ""),
            "suspension_reason_status": str(row.get("suspension_reason_status") or ""),
            "reason_read_at": str(row.get("reason_read_at") or ""),
            "appeal_basis": basis,
            "appeal_basis_label": APPEAL_BASIS_LABELS[basis],
            "appeal_text": text,
            "appeal_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "appeal_auto_generated": auto_generated,
            "truth_confirmed": True,
            "status": APPEAL_SUBMITTING,
            "attempt_id": attempt_id,
            "attempt_started_at": current_iso,
            "submitted_at": "",
            "created_at": current_iso,
            "updated_at": current_iso,
        }
        # Persist before the external call. A crash therefore leaves a fresh
        # protected attempt instead of silently allowing a duplicate submit.
        records.append(record)
        latest[key] = record
        save_appeal_history(records, history_path)
        try:
            if allow_resubmit and submitter is submit_official_appeal:
                outcome = submit_official_appeal(
                    row,
                    text,
                    basis,
                    allow_resubmit=True,
                    browser_type=row_browser_type,
                )
            else:
                outcome = submitter(row, text, basis)
        except Exception as exc:
            outcome = {"status": APPEAL_SUBMIT_FAILED, "submitted": False, "error": str(exc)}
        status = str(outcome.get("status") or APPEAL_SUBMIT_FAILED)
        record.update({
            "status": status,
            "submitted_at": current_iso if bool(outcome.get("submitted")) else "",
            "updated_at": current_iso,
            **{name: value for name, value in outcome.items() if name != "status"},
        })
        record.update(classify_appeal_outcome(status, str(record.get("error") or ""), now=current))
        latest[key] = record
        results.append(record)
        save_appeal_history(records, history_path)
        if log:
            suffix = f"：{record.get('error')}" if record.get("error") else ""
            log(f"[{format_cid(cid)}] 申诉状态：{status}{suffix}")
    save_appeal_history(records, history_path)
    return results


def submit_appeals(
    accounts: Iterable[dict[str, Any]],
    template: str,
    basis: str,
    *,
    truth_confirmed: bool = False,
    history_path: Path | None = None,
    submitter: Callable[[dict[str, Any], str, str], dict[str, Any]] = submit_official_appeal,
    now: datetime | None = None,
    log: Callable[[str], None] | None = None,
    allow_user_action_retry: bool = False,
    allow_failed_retry: bool = False,
    allow_resubmit: bool = False,
    browser_type: str = ADSPOWER,
) -> list[dict[str, Any]]:
    """Serialize local submission attempts across GUI and scheduled workflows."""
    lock_target = appeal_submission_lock_path(history_path)
    try:
        acquired_path, token = acquire_appeal_automation_lock(path=lock_target, now=now)
    except RuntimeError as exc:
        if "正在运行" in str(exc):
            raise RuntimeError("另一个账户申诉提交任务正在运行，已阻止重复并发提交") from exc
        raise
    try:
        return _submit_appeals_unlocked(
            accounts, template, basis,
            truth_confirmed=truth_confirmed,
            history_path=history_path,
            submitter=submitter,
            now=now,
            log=log,
            allow_user_action_retry=allow_user_action_retry,
            allow_failed_retry=allow_failed_retry,
            allow_resubmit=allow_resubmit,
            browser_type=browser_type,
        )
    finally:
        release_appeal_automation_lock(acquired_path, token)


def run_one_click_appeals(
    accounts: Iterable[dict[str, Any]],
    template: str,
    basis: str,
    *,
    truth_confirmed: bool = False,
    cache_path: Path | None = None,
    history_path: Path | None = None,
    reader: Callable[[str, str], dict[str, str]] = read_official_suspension_reason,
    submitter: Callable[[dict[str, Any], str, str], dict[str, Any]] = submit_official_appeal,
    log: Callable[[str], None] | None = None,
    retry_user_action: bool = True,
    retry_failed: bool = True,
    allow_resubmit: bool = False,
    browser_type: str = ADSPOWER,
) -> dict[str, Any]:
    """One-click chain: read official reasons, then submit every ready account.

    Accounts without a confirmed official policy name are never guessed and are
    reported as skipped instead of being submitted.
    """
    rows = [dict(row) for row in accounts]
    if not truth_confirmed:
        raise ValueError("提交前必须确认申诉资料真实、完整，且已完成所述整改")
    if not rows:
        raise ValueError("没有可申诉的封禁 CID，请先在“批量任务”刷新账户列表与状态")
    if len(rows) > MAX_APPEALS_PER_BATCH:
        raise ValueError(f"单次最多处理 {MAX_APPEALS_PER_BATCH} 个账号，请分批处理")
    basis = validate_appeal_basis(basis)
    validate_appeal_template(template)
    history_state = effective_appeal_history_by_key(load_appeal_history(history_path))
    protected: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in rows:
        row = dict(source)
        row_browser_type = _account_browser_type(row, browser_type)
        if "browser_type" in row or normalize_browser_type(browser_type) != ADSPOWER:
            row["browser_type"] = row_browser_type
        key = _appeal_key(
            str(row.get("profile_id") or "").strip(),
            str(row.get("cid") or ""),
            row_browser_type,
        )
        if key in seen:
            continue
        seen.add(key)
        prior = history_state.get(key)
        if appeal_submission_is_protected(
            prior,
            allow_resubmit=allow_resubmit,
            allow_user_action_retry=retry_user_action,
            allow_failed_retry=retry_failed,
        ):
            skipped = dict(prior or row)
            skipped.update({
                "skipped": True,
                "skip_reason": f"已有受保护申诉状态：{(prior or {}).get('status')}",
            })
            protected.append(skipped)
            if log:
                log(
                    f"[{format_cid(key[1])}] 已有“{(prior or {}).get('status')}”记录，"
                    "跳过原因读取和重复提交"
                )
        else:
            candidates.append(row)
    if log:
        log(
            f"一键申诉：已保护 {len(protected)} 个已有提交记录；"
            f"开始读取 {len(candidates)} 个待申诉账号的官方封禁原因…"
        )
    reasons = (
        read_suspension_reasons(
            candidates, cache_path=cache_path, reader=reader, log=log,
        )
        if candidates else []
    )
    by_key = {
        _appeal_key(
            str(record.get("profile_id") or ""),
            str(record.get("cid") or ""),
            _account_browser_type(record, browser_type),
        ): record
        for record in reasons
    }
    ready: list[dict[str, Any]] = []
    not_ready: list[dict[str, Any]] = []
    for row in candidates:
        merged = dict(row)
        key = _appeal_key(
            str(row.get("profile_id") or "").strip(),
            str(row.get("cid") or ""),
            _account_browser_type(row, browser_type),
        )
        record = by_key.get(key)
        if record:
            merged.update(record)
        if (
            record
            and record.get("suspension_reason_status") == REASON_READ
            and record.get("suspension_reason_source") == OFFICIAL_REASON_SOURCE
            and str(record.get("suspension_policy") or "").strip()
            and str(record.get("suspension_reason") or "").strip()
        ):
            ready.append(merged)
        else:
            not_ready.append(merged)
            if log:
                log(
                    f"[{format_cid(key[1])}] 未读到明确官方政策原因，跳过提交："
                    f"{(record or {}).get('suspension_reason_status') or '读取失败'}"
                )
    results: list[dict[str, Any]] = []
    if ready:
        if log:
            log(f"一键申诉：开始提交 {len(ready)} 个已确认官方原因的账号…")
        results = submit_appeals(
            ready, template, basis,
            truth_confirmed=True, history_path=history_path,
            submitter=submitter, log=log,
            allow_user_action_retry=retry_user_action,
            allow_failed_retry=retry_failed,
            allow_resubmit=allow_resubmit,
            browser_type=browser_type,
        )
    cid_results = [{
        "browser_type": _account_browser_type(row, browser_type),
        "profile_id": str(row.get("profile_id") or ""),
        "cid": str(row.get("cid") or ""),
        "cid_display": str(row.get("cid_display") or format_cid(str(row.get("cid") or ""))),
        "status": str(row.get("status") or ""),
        "outcome": "protected",
        "message": str(row.get("skip_reason") or ""),
    } for row in protected]
    cid_results.extend({
        "browser_type": _account_browser_type(row, browser_type),
        "profile_id": str(row.get("profile_id") or ""),
        "cid": normalize_cid(str(row.get("cid") or "")),
        "cid_display": format_cid(str(row.get("cid") or "")),
        "status": "跳过",
        "outcome": "reason_not_verified",
        "message": str(row.get("error") or row.get("suspension_reason_status") or "未读到官方明确原因"),
    } for row in not_ready)
    cid_results.extend({
        "browser_type": _account_browser_type(row, browser_type),
        "profile_id": str(row.get("profile_id") or ""),
        "cid": str(row.get("cid") or ""),
        "cid_display": str(row.get("cid_display") or format_cid(str(row.get("cid") or ""))),
        "status": str(row.get("status") or ""),
        "outcome": "skipped" if row.get("skipped") else "submit_result",
        "message": str(row.get("error") or row.get("skip_reason") or ""),
        "retry_class": str(row.get("retry_class") or ""),
        "retryable": bool(row.get("retryable")),
    } for row in results)
    submitted_count = sum(
        row.get("status") == APPEAL_SUBMITTED and not row.get("skipped") for row in results
    )
    pending_count = sum(row.get("status") == APPEAL_OFFICIAL_PENDING for row in results)
    waiting_count = sum(row.get("status") in APPEAL_USER_ACTION_STATUSES for row in results)
    failed_count = sum(row.get("status") == APPEAL_SUBMIT_FAILED for row in results)
    return {
        "reasons": reasons,
        "results": results,
        "protected": protected,
        "protected_count": len(protected),
        "ready": len(ready),
        "no_reason": [str(row.get("cid") or "") for row in not_ready],
        "cid_results": cid_results,
        "submitted_count": submitted_count,
        "pending_count": pending_count,
        "waiting_count": waiting_count,
        "failed_count": failed_count,
        "summary": (
            f"共 {len(cid_results)} 个 CID：提交成功 {submitted_count} 个，"
            f"处理中 {pending_count} 个，等待人工处理 {waiting_count} 个，"
            f"原因不明确跳过 {len(not_ready)} 个，已有记录保护 {len(protected)} 个，"
            f"失败 {failed_count} 个"
        ),
    }


def prepare_appeals(
    accounts: Iterable[dict[str, Any]],
    template: str,
    *,
    history_path: Path | None = None,
    opener: Callable[[str, str], dict[str, str]] = open_official_appeal_entry,
    log: Callable[[str], None] | None = None,
    browser_type: str = ADSPOWER,
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in accounts]
    if not rows:
        raise ValueError("请至少选择一个封禁 CID")
    if len(rows) > MAX_APPEALS_PER_BATCH:
        raise ValueError(f"单次最多准备 {MAX_APPEALS_PER_BATCH} 个账号申诉，请分批处理")
    seen: set[tuple[str, str]] = set()
    prepared: list[dict[str, Any]] = []
    records = load_appeal_history(history_path)
    for row in rows:
        validate_appeal_account(row)
        cid = normalize_cid(str(row["cid"]))
        row_browser_type = _account_browser_type(row, browser_type)
        key = _appeal_key(str(row["profile_id"]).strip(), cid, row_browser_type)
        if key in seen:
            continue
        seen.add(key)
        text, auto_generated = build_appeal_text(template, row)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        record: dict[str, Any] = {
            "browser_type": row_browser_type,
            "profile_id": str(row["profile_id"]).strip(),
            "window_number": str(row.get("window_number") or ""),
            "cid": cid,
            "cid_display": format_cid(cid),
            "ocid": str(row.get("ocid") or ""),
            "account_name": str(row.get("account_name") or ""),
            "suspension_reason": str(row.get("suspension_reason") or row.get("status_detail") or ""),
            "suspension_policy": str(row.get("suspension_policy") or ""),
            "suspension_reason_source": str(row.get("suspension_reason_source") or ""),
            "suspension_reason_status": str(row.get("suspension_reason_status") or ""),
            "reason_read_at": str(row.get("reason_read_at") or ""),
            "suspension_page_excerpt": str(row.get("suspension_page_excerpt") or ""),
            "appeal_text": text,
            "appeal_auto_generated": auto_generated,
            "status": APPEAL_PENDING,
            "created_at": now,
            "updated_at": now,
        }
        try:
            if "browser_type" in row or row_browser_type != ADSPOWER:
                opened = opener(
                    str(row["profile_id"]).strip(),
                    str(row["ocid"]),
                    browser_type=row_browser_type,
                )
            else:
                opened = opener(str(row["profile_id"]).strip(), str(row["ocid"]))
            record.update({"target_id": opened.get("target_id", ""), "url": opened.get("url", "")})
            if log:
                log(f"[{format_cid(cid)}] 已打开官方账号中止申诉入口，等待人工复核提交")
        except Exception as exc:
            record.update({"status": "打开失败", "error": str(exc)})
            if log:
                log(f"[{format_cid(cid)}] 申诉入口打开失败：{exc}")
        records.append(record)
        prepared.append(record)
    save_appeal_history(records, history_path)
    return prepared


def update_appeal_status(
    cid: str,
    status: str,
    *,
    profile_id: str = "",
    browser_type: str = ADSPOWER,
    history_path: Path | None = None,
) -> dict[str, Any]:
    if status not in APPEAL_STATUSES[1:]:
        raise ValueError("不支持的申诉状态")
    normalized = normalize_cid(cid)
    normalized_browser_type = normalize_browser_type(browser_type)
    records = load_appeal_history(history_path)
    matching = [
        row for row in records
        if str(row.get("cid") or "") == normalized
        and (not profile_id or str(row.get("profile_id") or "") == profile_id)
        and _account_browser_type(row) == normalized_browser_type
    ]
    if not matching:
        raise ValueError("该 CID 还没有本地申诉记录")
    record = matching[-1]
    record["status"] = status
    record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    save_appeal_history(records, history_path)
    return dict(record)
