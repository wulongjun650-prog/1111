"""GA4 内部协议层。

借用 AdsPower 窗口中已登录的 Google 会话，直接调用 Google Analytics 4
前端使用的内部 JSON RPC（/analytics/app/data/v2/...），不模拟鼠标键盘。

协议要点（均为真实抓包确认）：

- 认证：浏览器 Cookie 中的 ``GA_XSRF_TOKEN`` 值作为 ``X-GAFE4-XSRF-TOKEN``
  请求头发送；GET 大多可直接读取，POST/写操作必须携带该头。令牌会轮换，
  因此每次调用都在页面里现读 Cookie，并在 errorCode 3 时重读重试一次。
- 响应带 ``)]}'`` 前缀，数据在 ``default`` 字段，错误在 ``er`` 字段。
- ``dataset`` 参数格式为 ``a<账号ID>p<媒体资源ID>``。

已验证的调用：

- 账号/媒体资源列表：GET  /admin/accounttree
- 创建媒体资源：      POST /admin/property
- 数据流列表/创建：   GET+POST /admin/streams
- 可关联 Ads 账户：   GET  /admin/integrations/adwordslinkslist/linkableaccounts
- Ads 关联列表/创建： GET+POST /admin/integrations/adwordslinks
- 转化报表：          POST /venus（指标 conversions，维度 nth_day/event_name 等）
"""
from __future__ import annotations

import json
import re
import urllib.parse
import uuid
from typing import Any, Callable

from .adspower import close_cdp_target, open_cdp_target
from .browser_provider import ADSPOWER, normalize_browser_type, resolve_cdp
from .protocol import CDPPage, _json_response

GA4_HOME_URL = "https://analytics.google.com/analytics/web/"
_API_BASE = "/analytics/app/data/v2"

INDUSTRIES = {
    "科技": "TECHNOLOGY",
    "艺术与娱乐": "ARTS_AND_ENTERTAINMENT",
    "车辆": "AUTOMOTIVE",
    "美容与健身": "BEAUTY_AND_FITNESS",
    "图书与文学": "BOOKS_AND_LITERATURE",
    "工商业": "BUSINESS_AND_INDUSTRIAL_MARKETS",
    "金融": "FINANCE",
    "餐饮": "FOOD_AND_DRINK",
    "游戏": "GAMES",
    "健康": "HEALTH",
    "其他": "OTHER",
}

BUSINESS_SIZES = {
    "小型 - 1 至 10 名员工": 1,
    "中型 - 11 至 100 名员工": 2,
    "大型 - 101 至 500 名员工": 3,
    "超大型 - 501 名以上的员工": 4,
}

BUSINESS_OBJECTIVES = {
    "发掘潜在客户": 1,
    "提高销售额": 2,
    "了解网站和应用流量": 3,
    "查看用户互动度和留存率": 4,
    "其他业务目标": 5,
}


def dataset_for(account_id: str, property_id: str) -> str:
    """``a<账号>p<媒体资源>`` 形式的 dataset 参数。"""
    account = re.sub(r"\D", "", str(account_id))
    prop = re.sub(r"\D", "", str(property_id))
    if not account or not prop:
        raise ValueError(f"账号/媒体资源 ID 无效: {account_id!r}/{property_id!r}")
    return f"a{account}p{prop}"


def _unwrap(payload: Any, action: str) -> Any:
    """从 ``)]}'`` 外壳中取 ``default`` 数据；``er`` 非空则抛错。"""
    if not isinstance(payload, dict):
        raise RuntimeError(f"GA4 {action} 返回了非 JSON 结构: {str(payload)[:300]}")
    error = payload.get("er")
    if isinstance(error, dict) and error.get("errorCode"):
        raise RuntimeError(
            f"GA4 {action} 被拒绝: errorCode={error.get('errorCode')} "
            f"http={error.get('httpStatus')} {json.dumps(error.get('messageSet') or {}, ensure_ascii=False)[:300]}"
        )
    return payload.get("default")


def build_property_body(
    name: str,
    *,
    currency: str = "USD",
    timezone: str = "Asia/Shanghai",
    industry: str = "TECHNOLOGY",
    business_size: int = 1,
    objectives: list[int] | None = None,
) -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("媒体资源名称不能为空")
    if len(name) > 100:
        raise ValueError("媒体资源名称过长（最多 100 字符）")
    return {
        "sdk": 1,
        "type": 2,
        "industry": industry,
        "creationFunnel": 1,
        "currency": currency,
        "timeZone": timezone,
        "name": name,
        "businessSize": int(business_size),
        "businessObjective": list(objectives or [1]),
    }


def build_stream_body(name: str, url: str) -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("数据流名称不能为空")
    url = str(url or "").strip()
    url = re.sub(r"^https?://", "", url).strip("/")
    if not url or "." not in url:
        raise ValueError(f"网站网址无效: {url!r}")
    return {
        "name": name,
        "webData": {
            "defaultUrl": f"https://{url}",
            "enhancedMeasurement": {
                "settingsEnabled": True,
                "pageViews": {"isEnabled": True, "pageChangesEnabled": True},
                "scrolls": {"isEnabled": True},
                "outboundClicks": {"isEnabled": True},
                "siteSearch": {
                    "isEnabled": True,
                    "searchQueryParameter": "q,s,search,query,keyword",
                    "urlQueryParameter": "",
                },
                "videoEngagement": {"isEnabled": True},
                "fileDownloads": {"isEnabled": True},
                "formInteractions": {"isEnabled": True},
            },
        },
    }


def build_ads_link_body(
    cid: str,
    descriptive_name: str,
    internal_customer_id: str = "",
    *,
    personalized_ads: bool = True,
    autotagging: bool = True,
) -> dict[str, Any]:
    cid_digits = re.sub(r"\D", "", str(cid))
    if len(cid_digits) != 10:
        raise ValueError(f"Google Ads CID 无效: {cid!r}")
    account: dict[str, Any] = {
        "externalCustomerId": cid_digits,
        "descriptiveName": str(descriptive_name or "").strip() or f"CID {cid_digits}",
    }
    if internal_customer_id:
        account["internalCustomerId"] = re.sub(r"\D", "", str(internal_customer_id))
    return {
        "adwordsAccount": [account],
        "linkType": 3,
        "personalizedAdvertisingEnabled": bool(personalized_ads),
        "autotaggingForAddedAccountsEnabled": bool(autotagging),
    }


def build_report_body(
    property_id: str,
    dimensions: list[str],
    metrics: list[str],
    start_date: str,
    end_date: str,
    *,
    limit: int = 100,
    grand_total: bool = True,
) -> dict[str, Any]:
    if not dimensions and not metrics:
        raise ValueError("报表至少需要一个维度或指标")
    return {
        "entity": {"propertyId": re.sub(r"\D", "", str(property_id)), "identityBlendingStrategy": 2},
        "requests": [
            {
                "dimensions": [{"name": d, "isSecondary": False} for d in dimensions],
                "dimensionFilters": [],
                "metrics": [{"name": m, "isInvisible": False, "isSecondary": False} for m in metrics],
                "metricFilters": [],
                "requestGrandTotal": bool(grand_total),
                "dateRanges": [{"startDate": start_date, "endDate": end_date}],
                "rowAxis": {"fieldNames": list(dimensions), "sorts": []},
                "limit": int(limit),
                "offset": 0,
                "metaAggTypes": [],
            }
        ],
        "reportTitle": "ggads-ga4-report",
        "guid": str(uuid.uuid4()).upper(),
    }


def _cell_value(cell: Any) -> str:
    if isinstance(cell, dict):
        value = cell.get("value")
        return "" if value is None else str(value)
    return "" if cell is None else str(cell)


def parse_report_rows(payload: Any) -> list[dict[str, str]]:
    """把 venus 响应解析为 ``[{维度/指标名: 值}]`` 行列表。"""
    default = _unwrap(payload, "报表查询")
    responses = (default or {}).get("responses") or []
    rows: list[dict[str, str]] = []
    for response in responses:
        dim_names = [str(d) for d in response.get("dimensions") or []]
        met_names = [str((m or {}).get("id") or "") for m in response.get("metrics") or []]
        for row in response.get("responseRows") or []:
            item: dict[str, str] = {}
            dims = row.get("dimensionValues") or []
            mets = row.get("metricValues") or row.get("metricCompoundValues") or []
            for i, name in enumerate(dim_names):
                item[name] = _cell_value(dims[i]) if i < len(dims) else ""
            for i, name in enumerate(met_names):
                item[name] = _cell_value(mets[i]) if i < len(mets) else ""
            rows.append(item)
    return rows


_FETCH_JS = """
(async (args) => {
  async function once() {
    const token = (document.cookie.match(/GA_XSRF_TOKEN=([^;]+)/) || [])[1] || '';
    const headers = {'X-Same-Domain': '1', 'X-GAFE4-XSRF-TOKEN': token};
    if (args.body !== null) headers['Content-Type'] = 'application/json';
    const resp = await fetch(args.url, {
      method: args.method, credentials: 'include', headers,
      body: args.body === null ? undefined : JSON.stringify(args.body)
    });
    return {status: resp.status, text: await resp.text()};
  }
  let r = await once();
  if (r.status !== 200 && /"errorCode"\\s*:\\s*3/.test(r.text)) r = await once();
  return JSON.stringify({status: r.status, text: r.text.slice(0, 200000)});
})
"""


class GA4Runner:
    """一个 AdsPower 窗口的 GA4 协议会话。"""

    def __init__(
        self,
        profile_id: str,
        log: Callable[[str], None] | None = None,
        *,
        browser_type: str = ADSPOWER,
    ):
        self.profile_id = profile_id.strip()
        self.browser_type = normalize_browser_type(browser_type)
        self.log = log or (lambda message: None)
        self._endpoint = ""
        self._target: dict[str, Any] = {}
        self.page: CDPPage | None = None

    def connect(self) -> None:
        self._endpoint = resolve_cdp(
            self.profile_id, browser_type=self.browser_type,
        )
        self._target = open_cdp_target(self._endpoint, GA4_HOME_URL)
        self.page = CDPPage(self._target)
        self._wait_ready()

    def _wait_ready(self, timeout: float = 60.0) -> None:
        assert self.page is not None
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                href = self.page.evaluate("location.href", timeout=10)
                ready = self.page.evaluate("document.readyState", timeout=10)
                if "analytics.google.com" in str(href) and ready == "complete":
                    return
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError("GA4 页面加载超时；请确认该窗口的 Google 账号已登录")

    def close(self) -> None:
        if self.page is not None:
            self.page.close()
            self.page = None
        if self._endpoint and self._target.get("id"):
            close_cdp_target(self._endpoint, str(self._target["id"]))
            self._target = {}

    def __enter__(self) -> "GA4Runner":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ RPC
    def _call(self, method: str, path: str, body: Any = None, *, dataset: str = "", action: str = "", query: dict[str, str] | None = None) -> Any:
        if self.page is None:
            raise RuntimeError("GA4Runner 尚未连接")
        params: dict[str, str] = dict(query or {})
        params["hl"] = "zh_CN"
        params["gamonitor"] = "gafe"
        if dataset:
            params["dataset"] = dataset
        base = path if path.startswith("/analytics/") else f"{_API_BASE}{path}"
        url = f"{base}?{urllib.parse.urlencode(params)}"
        arg = json.dumps({"url": url, "method": method, "body": body}, ensure_ascii=False)
        raw = self.page.evaluate(f"({_FETCH_JS})({arg})", timeout=60)
        envelope = json.loads(raw)
        status = envelope.get("status")
        text = str(envelope.get("text") or "")
        if status == 200 and not text.replace(")]}'", "").strip():
            return {}  # DELETE 等写操作可能返回空 200
        try:
            payload = _json_response(text)
        except RuntimeError:
            raise RuntimeError(f"GA4 {action or path} HTTP {status}: {text[:300]}") from None
        if status != 200:
            error = payload.get("er") if isinstance(payload, dict) else None
            detail = ""
            if isinstance(error, dict):
                detail = f" errorCode={error.get('errorCode')} {json.dumps(error.get('messageSet') or {}, ensure_ascii=False)[:200]}"
            raise RuntimeError(f"GA4 {action or path} HTTP {status}{detail}")
        return _unwrap(payload, action or path)

    # ------------------------------------------------------------ 账号/媒体资源
    def list_accounts(self) -> list[dict[str, Any]]:
        """返回 [{account_id, account_name, properties:[{property_id, property_name}]}]。"""
        default = self._call("GET", "/admin/accounttree", action="账号列表")
        accounts: list[dict[str, Any]] = []
        for acc in (default or {}).get("accounts") or []:
            if acc.get("trashed"):
                continue
            properties = [
                {"property_id": str(p.get("id") or ""), "property_name": str(p.get("name") or "")}
                for p in acc.get("properties") or []
                if not p.get("trashed")
            ]
            accounts.append({
                "account_id": str(acc.get("id") or ""),
                "account_name": str(acc.get("name") or ""),
                "properties": properties,
            })
        return accounts

    def create_property(
        self,
        account_id: str,
        name: str,
        *,
        currency: str = "USD",
        timezone: str = "Asia/Shanghai",
        industry: str = "TECHNOLOGY",
        business_size: int = 1,
        objectives: list[int] | None = None,
    ) -> dict[str, str]:
        """创建 GA4 媒体资源，返回 {property_id, dataset}。"""
        body = build_property_body(
            name, currency=currency, timezone=timezone,
            industry=industry, business_size=business_size, objectives=objectives,
        )
        # 创建接口的 dataset 用账号级占位（真实抓包即为当前数据集上下文）
        default = self._call("POST", "/admin/property", body, dataset=f"a{account_id}p0", action="创建媒体资源")
        dataset = str((default or {}).get("dataset") or "")
        match = re.fullmatch(r"a(\d+)p(\d+)", dataset)
        if not match:
            raise RuntimeError(f"GA4 创建媒体资源后未返回 dataset: {dataset!r}")
        return {"property_id": match.group(2), "dataset": dataset}

    # ---------------------------------------------------------------- 数据流
    def list_streams(self, dataset: str) -> list[dict[str, str]]:
        default = self._call("GET", "/admin/streams", dataset=dataset, action="数据流列表")
        streams: list[dict[str, str]] = []
        for item in (default or {}).get("items") or []:
            streams.append({
                "stream_id": str(item.get("entityId") or ""),
                "name": str(item.get("name") or ""),
                "url": str((item.get("webData") or {}).get("defaultUrl") or ""),
                "measurement_id": str(item.get("gTrackingId") or ""),
            })
        return streams

    def create_stream(self, dataset: str, name: str, url: str) -> dict[str, str]:
        body = build_stream_body(name, url)
        default = self._call("POST", "/admin/streams", body, dataset=dataset, action="创建数据流")
        return {
            "stream_id": str((default or {}).get("entityId") or ""),
            "name": str((default or {}).get("name") or ""),
            "url": str(((default or {}).get("webData") or {}).get("defaultUrl") or ""),
            "measurement_id": str((default or {}).get("gTrackingId") or ""),
        }

    # ------------------------------------------------------------ Google Ads 关联
    def list_ads_links(self, dataset: str) -> list[dict[str, str]]:
        default = self._call("GET", "/admin/integrations/adwordslinks", dataset=dataset, action="Ads 关联列表")
        links: list[dict[str, str]] = []
        for item in (default or {}).get("items") or []:
            account = (item.get("adwordsAccount") or [{}])[0]
            links.append({
                "link_uid": str(item.get("linkUid") or ""),
                "cid": str(account.get("externalCustomerId") or ""),
                "account_name": str(account.get("descriptiveName") or ""),
                "linked_by": str(item.get("linkedBy") or ""),
                "personalized_ads": str(bool(item.get("adwordsDataSharingEnabled", True))),
            })
        return links

    def list_linkable_ads_accounts(self, dataset: str) -> list[dict[str, str]]:
        """该 Google 账号可关联的 Google Ads 账户（含 MCC 子账户）。"""
        default = self._call(
            "GET", "/admin/integrations/adwordslinkslist/linkableaccounts",
            dataset=dataset, action="可关联 Ads 账户",
        )
        accounts: list[dict[str, str]] = []
        for cid, item in ((default or {}).get("accounts") or {}).items():
            accounts.append({
                "cid": str(item.get("externalCustomerId") or cid),
                "account_name": str(item.get("descriptiveName") or ""),
                "is_mcc": str(bool(item.get("mcmAccount"))),
                "internal_customer_id": str(item.get("internalCustomerId") or ""),
            })
        return accounts

    def create_ads_link(
        self,
        dataset: str,
        cid: str,
        descriptive_name: str,
        internal_customer_id: str = "",
        *,
        personalized_ads: bool = True,
        autotagging: bool = True,
    ) -> dict[str, str]:
        body = build_ads_link_body(
            cid, descriptive_name, internal_customer_id,
            personalized_ads=personalized_ads, autotagging=autotagging,
        )
        default = self._call("POST", "/admin/integrations/adwordslinks", body, dataset=dataset, action="创建 Ads 关联")
        link_uid = str((default or {}).get("linkUid") or "")
        if not link_uid:
            raise RuntimeError("GA4 创建 Ads 关联后未返回 linkUid")
        return {"link_uid": link_uid, "cid": re.sub(r"\D", "", str(cid))}

    def delete_ads_link(self, dataset: str, link_uid: str) -> None:
        link_uid = str(link_uid or "").strip()
        if not link_uid:
            raise ValueError("link_uid 不能为空")
        self._call("DELETE", f"/admin/integrations/adwordslinks/{urllib.parse.quote(link_uid, safe='')}", dataset=dataset, action="删除 Ads 关联")

    # ---------------------------------------------------------------- 转化报表
    def query_report(
        self,
        dataset: str,
        property_id: str,
        dimensions: list[str],
        metrics: list[str],
        start_date: str,
        end_date: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, str]]:
        body = build_report_body(property_id, dimensions, metrics, start_date, end_date, limit=limit)
        params = {"accessmode": "read", "dataset": dataset, "hl": "zh_CN", "gamonitor": "gafe"}
        url = f"{_API_BASE}/venus?{urllib.parse.urlencode(params)}"
        if self.page is None:
            raise RuntimeError("GA4Runner 尚未连接")
        arg = json.dumps({"url": url, "method": "POST", "body": body}, ensure_ascii=False)
        raw = self.page.evaluate(f"({_FETCH_JS})({arg})", timeout=90)
        envelope = json.loads(raw)
        if envelope.get("status") != 200:
            raise RuntimeError(f"GA4 报表查询 HTTP {envelope.get('status')}: {str(envelope.get('text') or '')[:300]}")
        return parse_report_rows(_json_response(str(envelope.get("text") or "")))

    def query_conversions_by_event(
        self, dataset: str, property_id: str, start_date: str, end_date: str,
    ) -> list[dict[str, str]]:
        """按事件名的转化/事件数。"""
        return self.query_report(
            dataset, property_id,
            ["event_name"], ["conversions", "event_count"],
            start_date, end_date, limit=250,
        )

    def query_conversions_by_day(
        self, dataset: str, property_id: str, start_date: str, end_date: str,
    ) -> list[dict[str, str]]:
        """按日期的转化趋势。"""
        return self.query_report(
            dataset, property_id,
            ["nth_day"], ["conversions", "event_count", "active_users"],
            start_date, end_date, limit=92,
        )

    # ---------------------------------------------------------------- 关键事件
    def _conversion_settings(self, dataset: str) -> dict[str, Any]:
        return self._call("GET", "/mobile/conversionsettings", dataset=dataset, action="关键事件列表")

    def list_key_events(self, dataset: str) -> list[dict[str, str]]:
        default = self._conversion_settings(dataset)
        return [
            {"entity_id": str(e.get("entityId") or ""), "event_name": str(e.get("eventName") or "")}
            for e in (default or {}).get("conversionEvents") or []
        ]

    def _update_key_events(self, dataset: str, events: list[dict[str, Any]], action: str) -> None:
        current = self._conversion_settings(dataset)
        body = {
            "entityId": current.get("entityId"),
            "conversionEventNames": [],
            "conversionEvents": events,
        }
        self._call("PUT", "/mobile/conversionsettings", body, dataset=dataset, action=action)

    def mark_key_event(self, dataset: str, event_name: str) -> None:
        """把事件标记为关键事件（事件不存在也可先标记）。"""
        event_name = str(event_name or "").strip()
        if not event_name:
            raise ValueError("事件名称不能为空")
        current = self._conversion_settings(dataset)
        events = [dict(e) for e in current.get("conversionEvents") or []]
        if any(str(e.get("eventName")) == event_name for e in events):
            return
        events.append({"eventName": event_name})
        self._update_key_events(dataset, events, "标记关键事件")

    def unmark_key_event(self, dataset: str, event_name: str) -> None:
        current = self._conversion_settings(dataset)
        events = [
            dict(e) for e in current.get("conversionEvents") or []
            if str(e.get("eventName")) != event_name
        ]
        self._update_key_events(dataset, events, "取消关键事件")

    # ---------------------------------------------------------------- 设置类
    def get_data_retention(self, dataset: str) -> dict[str, Any]:
        return self._call("GET", "/admin/dataretention", dataset=dataset, action="读取数据保留")

    def set_data_retention(self, dataset: str, event_months: int = 14) -> dict[str, Any]:
        """事件级数据保留期（2 或 14 个月）。"""
        if event_months not in (2, 14):
            raise ValueError("数据保留期只能是 2 或 14 个月")
        current = self.get_data_retention(dataset)
        body = {
            "userLevelDataTtlMode": current.get("userLevelDataTtlMode", 1),
            "eventLevelDataTtlMonthsValue": event_months,
            "userLevelDataTtlMonthsValue": current.get("userLevelDataTtlMonthsValue", 14),
        }
        return self._call("PUT", "/admin/dataretention", body, dataset=dataset, action="设置数据保留")

    def get_attribution(self, dataset: str) -> dict[str, Any]:
        return self._call("GET", "/admin/attributionmodel", dataset=dataset, action="读取归因设置")

    def set_attribution(self, dataset: str, **overrides: Any) -> dict[str, Any]:
        """更新归因设置；overrides 覆盖 GET 到的字段（如 defaultEventAttributionModel）。"""
        body = self.get_attribution(dataset)
        body.update(overrides)
        return self._call("PUT", "/admin/attributionmodel", body, dataset=dataset, action="设置归因")

    def get_google_signals(self, dataset: str) -> dict[str, Any]:
        return self._call("GET", "/admin/googlesignals", dataset=dataset, action="读取 Google 信号")

    def set_google_signals(self, dataset: str, enabled: bool) -> dict[str, Any]:
        """启用/停用 Google 信号；启用即代表确认条款（isConsented=true）。"""
        current = self.get_google_signals(dataset)
        body = dict(current)
        body["isConsented"] = True
        body["isEnabled"] = bool(enabled)
        return self._call("PUT", "/admin/googlesignals", body, dataset=dataset, action="设置 Google 信号")

    # ------------------------------------------------------------ 自定义定义
    def list_custom_definitions(self, dataset: str, scope: str = "event") -> list[dict[str, Any]]:
        """scope: event / user / session / item。"""
        path = "/reporting/customdefinitions" if scope == "event" else f"/reporting/customdefinitions/{scope}"
        default = self._call("GET", path, dataset=dataset, action="自定义定义列表")
        items: list[dict[str, Any]] = []
        for item in (default or {}).get("items") or []:
            items.append({
                "entity_id": str(item.get("entityId") or ""),
                "display_name": str(item.get("displayName") or ""),
                "param_name": str(item.get("paramName") or ""),
                "description": str(item.get("description") or ""),
                "type": "指标" if item.get("definitionType") == 1 else "维度",
                "scope": str(item.get("scope") or ""),
            })
        return items

    def create_custom_definition(
        self,
        dataset: str,
        display_name: str,
        param_name: str,
        description: str = "",
        *,
        scope: str = "event",
        definition_type: int = 0,
        measurement_unit: int = 0,
    ) -> dict[str, Any]:
        """创建自定义维度（definition_type=0）或自定义指标（=1）。scope: event/user/session/item。"""
        display_name = str(display_name or "").strip()
        param_name = str(param_name or "").strip()
        if not display_name or not param_name:
            raise ValueError("维度名称和参数名不能为空")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", param_name):
            raise ValueError(f"参数名只能包含字母/数字/下划线: {param_name!r}")
        path = "/reporting/customdefinitions" if scope == "event" else f"/reporting/customdefinitions/{scope}"
        body = {
            "paramName": param_name,
            "scope": 0,
            "description": str(description or ""),
            "definitionType": int(definition_type),
            "displayName": display_name,
        }
        if definition_type == 1:
            body["measurementUnit"] = int(measurement_unit)
        return self._call("POST", path, body, dataset=dataset, action="创建自定义定义")

    def archive_custom_definition(self, dataset: str, display_name: str, scope: str = "event") -> None:
        """归档自定义定义（按 displayName 删除，真实抓包确认）。"""
        display_name = str(display_name or "").strip()
        if not display_name:
            raise ValueError("display_name 不能为空")
        path = "/reporting/customdefinitions" if scope == "event" else f"/reporting/customdefinitions/{scope}"
        self._call("DELETE", path, dataset=dataset, action="归档自定义定义", query={"displayName": display_name})

    # ---------------------------------------------------------------- 受众
    def list_audiences(self, dataset: str) -> list[dict[str, Any]]:
        default = self._call("GET", "/shared/enhancedaudience", dataset=dataset, action="受众列表")
        items: list[dict[str, Any]] = []
        for item in (default or {}).get("items") or []:
            items.append({
                "entity_id": str(item.get("entityId") or ""),
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "membership_days": str(item.get("membershipDurationInDays") or ""),
            })
        return items

    @staticmethod
    def build_event_audience_body(
        name: str,
        event_name: str,
        *,
        days: int = 30,
        description: str = "",
        event_count_op: int = 3,
        event_count: str = "0",
        lookback_days: int = 7,
    ) -> dict[str, Any]:
        """事件计数类受众（如“近 7 天未触发 session_start 的用户”）。

        结构来自 2026-08-09 对官方“7 天不活跃的用户”模板的抓包。
        comparisonType: 3 表示“=”，lookback 用动态回溯天数。
        """
        name = str(name or "").strip()
        event_name = str(event_name or "").strip()
        if not name or not event_name:
            raise ValueError("受众名称和事件名不能为空")
        count_filter = {"value": "event_count", "comparisonType": int(event_count_op), "comparisonValue": str(event_count)}
        condition = {"filterConditions": [{"eventFilter": {
            "eventName": event_name,
            "eventCountFilter": count_filter,
            "lookbackForEventCountFilterInDays": int(lookback_days),
            "isDynamicLookbackForEventCountFilter": True,
        }}]}
        expression = {"scope": 3, "andGroup": [condition]}
        segment_filter = {"scope": 3, "expressions": [expression], "isUsingVenusNames": True}
        return {
            "name": name,
            "description": str(description or ""),
            "membershipDurationInDays": int(days),
            "audienceFilter": {"segmentFilter": segment_filter},
            "publishingSource": 2,
            "enhancedAudienceTemplateType": 10,
            "exportDestinations": [],
        }

    def create_audience(self, dataset: str, body: dict[str, Any]) -> dict[str, Any]:
        result = self._call("POST", "/shared/enhancedaudience", body, dataset=dataset, action="创建受众")
        errors = (result or {}).get("validationErrors")
        if isinstance(errors, dict) and errors.get("errorDetail"):
            raise RuntimeError(f"GA4 受众条件校验失败: {json.dumps(errors, ensure_ascii=False)[:300]}")
        if not (result or {}).get("entityId"):
            raise RuntimeError("GA4 创建受众后未返回 entityId")
        return result

    def create_event_audience(self, dataset: str, name: str, event_name: str, **kwargs: Any) -> dict[str, Any]:
        return self.create_audience(dataset, self.build_event_audience_body(name, event_name, **kwargs))

    def delete_audience(self, dataset: str, entity_id: str) -> None:
        entity_id = str(entity_id or "").strip()
        if not entity_id:
            raise ValueError("entity_id 不能为空")
        self._call("DELETE", f"/shared/enhancedaudience/{entity_id}", dataset=dataset, action="删除受众")

    # -------------------------------------------------------- Measurement Protocol
    def list_mp_secrets(self, dataset: str) -> list[dict[str, str]]:
        default = self._call("GET", "/admin/measurementprotocol", dataset=dataset, action="MP 密钥列表")
        return [
            {
                "entity_id": str(i.get("entityId") or ""),
                "name": str(i.get("name") or ""),
                "secret": str(i.get("secret") or ""),
                "stream_id": str(i.get("parentStreamId") or ""),
            }
            for i in (default or {}).get("items") or []
        ]

    def create_mp_secret(self, dataset: str, stream_id: str, name: str) -> dict[str, str]:
        """创建 Measurement Protocol API 密钥（自动完成条款确认）。"""
        stream_id = re.sub(r"\D", "", str(stream_id))
        name = str(name or "").strip()
        if not stream_id or not name:
            raise ValueError("stream_id 和名称不能为空")
        settings = self._call("GET", "/admin/measurementprotocol/settings", dataset=dataset, action="MP 设置")
        if settings and not settings.get("attested"):
            self._call(
                "PUT", "/admin/measurementprotocol/settings",
                {"entityId": settings.get("entityId"), "attested": True},
                dataset=dataset, action="MP 条款确认",
            )
        result = self._call(
            "POST", "/admin/measurementprotocol",
            {"name": name, "parentStreamId": stream_id},
            dataset=dataset, action="创建 MP 密钥",
        )
        return {
            "entity_id": str((result or {}).get("entityId") or ""),
            "name": str((result or {}).get("name") or ""),
            "secret": str((result or {}).get("secret") or ""),
            "stream_id": stream_id,
        }

    def delete_mp_secret(self, dataset: str, entity_id: str) -> None:
        entity_id = str(entity_id or "").strip()
        if not entity_id:
            raise ValueError("entity_id 不能为空")
        self._call("DELETE", f"/admin/measurementprotocol/{entity_id}", dataset=dataset, action="删除 MP 密钥")

    # ------------------------------------------------------- 媒体资源/数据流更新
    def update_property(self, dataset: str, property_id: str, **fields: Any) -> dict[str, Any]:
        """更新媒体资源字段（如 propertyName/timeZone/currency）；先读后写合并。"""
        property_id = re.sub(r"\D", "", str(property_id))
        current = self._call("GET", f"/admin/property/{property_id}", dataset=dataset, action="读取媒体资源")
        body = dict(current or {})
        body.update(fields)
        return self._call("PUT", f"/admin/property/{property_id}", body, dataset=dataset, action="更新媒体资源")

    def delete_property(self, dataset: str, property_id: str) -> None:
        """删除媒体资源（进回收站，35 天内可恢复）。"""
        property_id = re.sub(r"\D", "", str(property_id))
        if not property_id:
            raise ValueError("property_id 不能为空")
        self._call("DELETE", f"/admin/property/{property_id}", dataset=dataset, action="删除媒体资源")

    def get_stream(self, dataset: str, stream_id: str, stream_type: str = "web") -> dict[str, Any]:
        stream_id = re.sub(r"\D", "", str(stream_id))
        return self._call("GET", f"/admin/streams/{stream_type}/{stream_id}", dataset=dataset, action="读取数据流详情")

    def update_stream(self, dataset: str, stream_id: str, stream_type: str = "web", **fields: Any) -> dict[str, Any]:
        """更新数据流（如 name）；先读详情再合并写回。"""
        stream_id = re.sub(r"\D", "", str(stream_id))
        current = self.get_stream(dataset, stream_id, stream_type)
        body = dict(current or {})
        body.update(fields)
        return self._call("PUT", f"/admin/streams/{stream_type}/{stream_id}", body, dataset=dataset, action="更新数据流")

    def delete_stream(self, dataset: str, stream_id: str, stream_type: str = "web") -> None:
        stream_id = re.sub(r"\D", "", str(stream_id))
        if not stream_id:
            raise ValueError("stream_id 不能为空")
        self._call("DELETE", f"/admin/streams/{stream_type}/{stream_id}", dataset=dataset, action="删除数据流")

    # ------------------------------------------------------------ 其他产品关联
    # linkType 枚举（2026-08-09 抓包确认）：1=Google Ads 3=BigQuery 6=Search Console
    PRODUCT_LINK_TYPES = {"google_ads": 1, "bigquery": 3, "search_console": 6}

    def list_product_links(self, dataset: str, link_type: int) -> list[dict[str, Any]]:
        """按 linkType 列出已建立的产品关联。"""
        default = self._call(
            "GET", "/admin/integrations/linklist",
            dataset=dataset, action="产品关联列表", query={"linkType": str(int(link_type))},
        )
        return list((default or {}).get("items") or [])

    def list_linkable_entities(self, dataset: str, link_type: int) -> list[dict[str, Any]]:
        """按 linkType 列出可关联的外部实体（SC 资源 / BigQuery 项目等）。"""
        default = self._call(
            "GET", "/admin/integrations/linkableentitylist",
            dataset=dataset, action="可关联实体列表",
            query={"linkType": str(int(link_type)), "nextPageToken": ""},
        )
        return list((default or {}).get("items") or [])

    def create_product_link(self, dataset: str, link_type: int, body: dict[str, Any]) -> dict[str, Any]:
        """建立产品关联。body 为对应产品的 link 对象（如 {"searchConsoleLink": {...}}）。

        注意：当前 Google 账号下没有可关联的 SC 资源/GCP 项目，创建路径未经真机
        验证；服务端拒绝时会原样抛出错误详情。
        """
        return self._call(
            "POST", "/admin/integrations/linklist",
            {"linkType": int(link_type), **body},
            dataset=dataset, action="建立产品关联",
        )

    def list_bigquery_projects(self, dataset: str) -> list[dict[str, Any]]:
        """可关联的 BigQuery 项目（走 linkableentitylist linkType=3，已验证路径）。

        GCP 项目全集在 cloudresourcemanager（OAuth 体系），GA4 侧的可关联项目
        就是这个列表；当前账号返回空即没有可关联项目。
        """
        return self.list_linkable_entities(dataset, self.PRODUCT_LINK_TYPES["bigquery"])

    # ------------------------------------------------------------ 事件创建规则
    def list_event_rules(self, dataset: str, stream_id: str) -> list[dict[str, Any]]:
        """某数据流的事件创建规则（从现有事件派生新事件）。"""
        stream_id = re.sub(r"\D", "", str(stream_id))
        default = self._call(
            "GET", "/admin/eventcreaterule", dataset=dataset,
            action="事件规则列表", query={"streamEntityId": stream_id},
        )
        rules: list[dict[str, Any]] = []
        for item in (default or {}).get("items") or []:
            conditions = [
                f"{c.get('fieldName')}:{c.get('value')}"
                for c in item.get("ruleTriggerConditions") or []
            ]
            rules.append({
                "entity_id": str(item.get("entityId") or ""),
                "destination_event": str(item.get("destinationEventName") or ""),
                "conditions": ", ".join(conditions),
            })
        return rules

    def create_event_rule(
        self,
        dataset: str,
        stream_id: str,
        destination_event: str,
        conditions: list[dict[str, str]],
        *,
        copy_parameters: bool = True,
    ) -> dict[str, Any]:
        """创建事件派生规则；conditions 形如 [{"field": "page_location", "value": "/thanks"}]。

        comparisonType=1 + isNegated=False 为 2026-08-09 真机验证的可写组合。
        """
        stream_id = re.sub(r"\D", "", str(stream_id))
        destination_event = str(destination_event or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", destination_event):
            raise ValueError(f"派生事件名只能含字母/数字/下划线: {destination_event!r}")
        if not stream_id:
            raise ValueError("stream_id 不能为空")
        trigger = []
        for cond in conditions:
            field = str(cond.get("field") or "").strip()
            value = str(cond.get("value") or "").strip()
            if not field or not value:
                raise ValueError("条件的字段和值不能为空")
            trigger.append({"fieldName": field, "comparisonType": 1, "isNegated": False, "value": value})
        body = {
            "streamEntityId": stream_id,
            "destinationEventName": destination_event,
            "copyParametersFromSource": bool(copy_parameters),
            "ruleTriggerConditions": trigger,
            "parameterMutations": [],
        }
        return self._call("POST", "/admin/eventcreaterule", body, dataset=dataset, action="创建事件规则")

    def delete_event_rule(self, dataset: str, rule_id: str) -> None:
        rule_id = re.sub(r"\D", "", str(rule_id))
        if not rule_id:
            raise ValueError("rule_id 不能为空")
        self._call("DELETE", f"/admin/eventcreaterule/{rule_id}", dataset=dataset, action="删除事件规则")

    # ---------------------------------------------------------------- 实时报表
    def query_realtime(self, dataset: str, property_id: str) -> dict[str, Any]:
        """实时概览：当前活跃用户数 + 最近 30 分钟逐分钟曲线。"""
        base_request = {
            "dimensionFilters": [],
            "metrics": [{"name": "active_users", "isInvisible": False, "isSecondary": False}],
            "metricFilters": [],
            "realtimeTimePeriod": 1,
            "requestGrandTotal": True,
            "dateRanges": [],
        }
        body = {
            "entity": {"propertyId": re.sub(r"\D", "", str(property_id)), "identityBlendingStrategy": 2},
            "requests": [
                {"dimensions": [], **base_request},
                {
                    "dimensions": [{"name": "realtime_timestamp_micros", "isSecondary": False}],
                    "rowAxis": {"fieldNames": ["realtime_timestamp_micros"], "sorts": []},
                    **base_request,
                },
            ],
            "reportTitle": "ggads-realtime",
            "guid": str(uuid.uuid4()).upper(),
        }
        if self.page is None:
            raise RuntimeError("GA4Runner 尚未连接")
        params = {"dataset": dataset, "hl": "zh_CN", "gamonitor": "gafe", "accessmode": "read",
                  "pageId": "ScionRealtimeReport/rt-realtime"}
        url = f"/analytics/v2/realtime/venus/getData?{urllib.parse.urlencode(params)}"
        arg = json.dumps({"url": url, "method": "POST", "body": body}, ensure_ascii=False)
        raw = self.page.evaluate(f"({_FETCH_JS})({arg})", timeout=60)
        envelope = json.loads(raw)
        if envelope.get("status") != 200:
            raise RuntimeError(f"GA4 实时报表 HTTP {envelope.get('status')}: {str(envelope.get('text') or '')[:200]}")
        default = _unwrap(_json_response(str(envelope.get("text") or "")), "实时报表")
        responses = (default or {}).get("responses") or []
        result: dict[str, Any] = {"active_users": "0", "timeline": []}
        if responses:
            totals = (responses[0].get("totalsRow") or {}).get("metricCompoundValues") or []
            if totals:
                result["active_users"] = str(totals[0].get("value") or "0")
        if len(responses) > 1:
            for row in responses[1].get("responseRows") or []:
                dims = row.get("dimensionValues") or []
                mets = row.get("metricValues") or []
                result["timeline"].append({
                    "ts": str((dims[0] or {}).get("value") or "") if dims else "",
                    "active_users": str((mets[0] or {}).get("value") or "") if mets else "",
                })
        return result
