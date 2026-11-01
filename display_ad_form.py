"""Fill one responsive display ad in the already-open target Ads tab.

Campaigns, ad groups, budgets, and image uploads stay on the existing
protocol path. This module only drives the ad editor. The save click is a
real page click, so the security-key prompt belongs to the page. Nothing
here sends AdGroupAdService.Batch or a forged reauth token.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .ads import normalize_creative
from .portable_copy import (
    _creative_texts,
    _matching_ads,
    _prefer_newest_ad,
    describe_unreadable_ad,
    portable_display_ad_request,
)
from .protocol_catalog import CHILD_STATUS_LABELS
from .targeting_copy import HumanActionRequired


DISPLAY_AD_FORM_LOG = "正在网页上填写自适应展示广告，点保存后由网页自己弹出密钥"
DISPLAY_AD_KEY_LOG = "请在这个广告页面上摸密钥，密钥框是网页自己弹的。不用改预算，也不用自己填广告。"
KEY_DONE_BUTTON = "密钥摸完了，读取新广告"

_FORM_ERRORS = (
    "请修正以下错误",
    "无法保存",
    "此字段为必填",
    "至少添加",
    "请添加一张",
    "字数超出",
    "超出字数限制",
)
_KEY_MARKERS = (
    "安全密钥",
    "安全钥匙",
    "使用您的安全密钥",
    "验证是您",
    "触摸您的",
    "security key",
)

DISPLAY_AD_PAGE_SCRIPT = r"""
function ggadsDisplayAd(args) {
  const ROLES = {
    url: {control: "最终网址", labels: ["最终到达网址", "最终网址", "Final URL"], forbid: ["显示路径", "手机网址"]},
    headline: {control: "短标题", labels: ["短标题", "标题", "Headline"], forbid: ["长标题", "Long headline"]},
    long_headline: {control: "长标题", labels: ["长标题", "Long headline"], forbid: []},
    description: {control: "描述", labels: ["广告内容描述", "描述", "Description"], forbid: ["长标题"]},
    business: {control: "商家名称", labels: ["商家名称", "Business name"], forbid: []},
    landscape: {control: "横图", labels: ["横向图片", "横图", "横向", "Landscape"], forbid: ["徽标", "logo", "方形", "纵向", "竖"]},
    square: {control: "方图", labels: ["方形图片", "方图", "方形", "Square"], forbid: ["徽标", "logo", "横向", "横图", "纵向", "竖", "1.91"]},
    portrait: {control: "竖图", labels: ["纵向图片", "竖图", "纵向", "竖向", "Portrait"], forbid: ["徽标", "logo", "横向", "方形"]}
  };

  function pageSaw() {
    let text = "";
    try { text = document.body ? document.body.innerText : ""; } catch (e) {}
    text = String(text || "").replace(/\s+/g, " ").trim();
    if (text.length <= 700) return text;
    return text.slice(0, 350) + " … " + text.slice(-350);
  }

  function className(el) {
    try { return String(el.className || ""); } catch (e) { return ""; }
  }

  function parentNodeOf(el) {
    if (!el) return null;
    if (el.parentElement) return el.parentElement;
    const root = el.getRootNode && el.getRootNode();
    if (root && root.host) return root.host;
    return null;
  }

  function allElements(root) {
    const out = [];
    const visit = (node) => {
      if (!node || !node.querySelectorAll) return;
      for (const el of node.querySelectorAll("*")) {
        out.push(el);
        if (el.shadowRoot) visit(el.shadowRoot);
      }
    };
    visit(root || document);
    return out;
  }

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    try {
      const style = getComputedStyle(el);
      if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) return false;
    } catch (e) {}
    return true;
  }

  function disabled(el) {
    return !!(el.disabled || (el.getAttribute && (el.getAttribute("aria-disabled") === "true" || el.hasAttribute("disabled"))));
  }

  function clickable(el) {
    const tag = el.tagName || "";
    const role = (el.getAttribute && el.getAttribute("role")) || "";
    return /BUTTON|A|MATERIAL|FAB/.test(tag) || role === "button" || role === "menuitem" || role === "option" || role === "tab";
  }

  function directText(el) {
    let text = "";
    for (const node of (el && el.childNodes) || []) {
      if (node.nodeType === 3) text += node.textContent || "";
    }
    return text.replace(/\s+/g, " ").trim();
  }

  function textOf(el) {
    const aria = (el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("label") || "")) || "";
    let inner = "";
    try { inner = el.innerText || el.textContent || ""; } catch (e) {}
    return (aria + " " + directText(el) + " " + inner).replace(/\s+/g, " ").trim();
  }

  function click(el) {
    try { el.scrollIntoView({block: "center", inline: "center"}); } catch (e) {}
    const rect = el.getBoundingClientRect();
    const eventInit = {bubbles: true, cancelable: true, composed: true, view: window, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2, button: 0};
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      try { el.dispatchEvent(new MouseEvent(type, eventInit)); } catch (e) {}
    }
    try { el.click(); } catch (e) {}
  }

  function setText(el, value) {
    const hostTag = el.tagName || "";
    const nested = (el.shadowRoot && el.shadowRoot.querySelector("input,textarea,[contenteditable=true]")) || null;
    const target = nested || el;
    try { target.focus(); } catch (e) {}
    if (target.isContentEditable) {
      target.textContent = value;
    } else if ("value" in target) {
      const proto = target.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value");
      if (setter && setter.set) setter.set.call(target, value);
      else target.value = value;
    }
    if ((hostTag === "MATERIAL-INPUT" || hostTag === "MATERIAL-TEXTAREA") && "value" in el) {
      try { el.value = value; } catch (e) {}
    }
    try {
      target.dispatchEvent(new InputEvent("input", {bubbles: true, composed: true, data: value, inputType: "insertText"}));
    } catch (e) {
      target.dispatchEvent(new Event("input", {bubbles: true, composed: true}));
    }
    target.dispatchEvent(new Event("change", {bubbles: true, composed: true}));
    target.dispatchEvent(new Event("blur", {bubbles: true, composed: true}));
    return readValue(el).indexOf(value) >= 0 || readValue(target).indexOf(value) >= 0;
  }

  function readValue(el) {
    if (!el) return "";
    if (el.isContentEditable) return String(el.textContent || "").trim();
    if ("value" in el && String(el.value || "").trim()) return String(el.value || "").trim();
    const nested = el.shadowRoot && el.shadowRoot.querySelector("input,textarea");
    if (nested && "value" in nested) return String(nested.value || "").trim();
    return "";
  }

  function specOf(role) {
    return ROLES[role] || {control: role || "页面", labels: [], forbid: []};
  }

  function matchesLabel(text, spec) {
    const value = String(text || "").replace(/\s+/g, " ").trim();
    if (!value || value.length > 32) return false;
    const low = value.toLowerCase();
    if (spec.forbid.some(item => low.includes(String(item).toLowerCase()))) return false;
    return spec.labels.some(item => {
      const label = String(item);
      return value === label || value.startsWith(label);
    });
  }

  function findLabel(spec) {
    const hits = [];
    for (const el of allElements().filter(visible)) {
      const text = directText(el) || (textOf(el).length <= 32 ? textOf(el) : "");
      if (matchesLabel(text, spec)) hits.push(el);
    }
    hits.sort((a, b) => textOf(a).length - textOf(b).length);
    return hits[0] || null;
  }

  function containsForeignLabel(root, spec, own) {
    for (const el of allElements(root)) {
      if (el === own) continue;
      const text = directText(el);
      if (!text || text.length > 32) continue;
      if (spec.labels.some(item => text === item || text.startsWith(item))) continue;
      if (spec.forbid.some(item => text === item || text.startsWith(item) || text.toLowerCase().includes(String(item).toLowerCase()))) return true;
    }
    return false;
  }

  function ownedByMaterial(el) {
    let node = el;
    for (let i = 0; i < 6 && node; i++) {
      node = parentNodeOf(node);
      if (!node) return false;
      if (node.tagName === "MATERIAL-INPUT" || node.tagName === "MATERIAL-TEXTAREA") return true;
    }
    return false;
  }

  function usableField(el) {
    if (!el || !visible(el) || disabled(el)) return false;
    const tag = el.tagName || "";
    const type = ((el.getAttribute && el.getAttribute("type")) || el.type || "").toLowerCase();
    if (type === "hidden" || type === "radio" || type === "checkbox" || type === "file" || type === "search") return false;
    if (tag === "MATERIAL-INPUT" || tag === "MATERIAL-TEXTAREA") return true;
    if (tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable) return !ownedByMaterial(el);
    return false;
  }

  function sortDoc(nodes) {
    return nodes.slice().sort((a, b) => {
      const pos = a.compareDocumentPosition(b);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
      if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
      return 0;
    });
  }

  function fieldsIn(root) {
    return sortDoc(allElements(root).filter(usableField));
  }

  function sectionRoot(spec) {
    const label = findLabel(spec);
    if (!label) return null;
    let node = label;
    let container = null;
    for (let i = 0; i < 8 && node; i++) {
      const parent = parentNodeOf(node);
      if (!parent || parent === document.body || parent === document.documentElement) break;
      if (containsForeignLabel(parent, spec, label)) break;
      if (fieldsIn(parent).length || allElements(parent).some(el => clickable(el) && visible(el))) container = parent;
      node = parent;
    }
    return container;
  }

  function fieldHint(el) {
    const bits = [];
    for (const name of ["aria-label", "label", "placeholder", "name"]) {
      const value = el.getAttribute && el.getAttribute(name);
      if (value) bits.push(value);
    }
    let node = el;
    for (let i = 0; i < 4 && node; i++) {
      node = parentNodeOf(node);
      if (!node) break;
      const direct = directText(node);
      if (direct) bits.push(direct);
      const prev = node.previousElementSibling;
      if (prev) bits.push((directText(prev) || textOf(prev)).slice(0, 40));
    }
    return bits.join(" ");
  }

  function fieldsFor(spec) {
    const section = sectionRoot(spec);
    if (section) {
      const inside = fieldsIn(section);
      if (inside.length) return inside;
    }
    return sortDoc(allElements().filter(usableField).filter(el => {
      const hint = fieldHint(el);
      const low = hint.toLowerCase();
      if (spec.forbid.some(item => low.includes(String(item).toLowerCase()))) return false;
      return spec.labels.some(item => low.includes(String(item).toLowerCase()));
    }));
  }

  function topDialog() {
    const dialogs = allElements().filter(el => {
      if (!visible(el)) return false;
      const role = (el.getAttribute && el.getAttribute("role")) || "";
      const tag = el.tagName || "";
      return role === "dialog" || role === "alertdialog" || tag === "MATERIAL-DIALOG" || tag === "DIALOG";
    });
    return dialogs.length ? dialogs[dialogs.length - 1] : null;
  }

  function fail(control, extra) {
    return Object.assign({ok: false, control: control, saw: pageSaw()}, extra || {});
  }

  function look() {
    const saw = pageSaw();
    return {
      ok: true,
      control: "页面",
      saw: saw,
      editor: /最终到达网址|最终网址|Final URL/.test(saw) && /商家名称|Business name/.test(saw),
      key: /安全密钥|安全钥匙|使用您的安全密钥|验证是您|触摸您的|security key/i.test(saw),
      menu: /自适应展示广告/.test(saw)
    };
  }

  function clickPlus() {
    const candidates = [];
    for (const el of allElements().filter(el => visible(el) && clickable(el) && !disabled(el))) {
      const text = textOf(el);
      if (/上传展示|上传的展示/.test(text)) continue;
      const tag = el.tagName || "";
      const fab = tag === "MATERIAL-FAB" || /fab/i.test(className(el));
      const named = /创建广告|新建广告|添加广告/.test(text);
      const plus = text === "+" || text === "add" || text === "add_circle";
      if (!(fab || named || plus)) continue;
      if (text.length > 24 && !named) continue;
      const rect = el.getBoundingClientRect();
      let score = rect.left + rect.top;
      if (named) score += 8000;
      if (fab) score += 4000;
      if (plus) score += 1000;
      candidates.push({el, score, text});
    }
    candidates.sort((a, b) => b.score - a.score);
    if (!candidates.length) return fail("蓝色加号");
    click(candidates[0].el);
    return {ok: true, control: "蓝色加号", saw: pageSaw(), clicked: candidates[0].text.slice(0, 80)};
  }

  function clickResponsive() {
    const hits = allElements().filter(el => visible(el) && clickable(el) && !disabled(el)).filter(el => {
      const text = textOf(el);
      if (/上传/.test(text)) return false;
      return /自适应展示广告/.test(text) || /responsive display ad/i.test(text);
    });
    hits.sort((a, b) => textOf(a).length - textOf(b).length);
    if (!hits.length) return fail("自适应展示广告");
    click(hits[0]);
    return {ok: true, control: "自适应展示广告", saw: pageSaw(), clicked: textOf(hits[0]).slice(0, 80)};
  }

  function fillSlot() {
    const spec = specOf(args.role);
    const fields = fieldsFor(spec);
    const index = Number(args.index || 0);
    if (!fields[index]) return fail(spec.control, {need_add: true});
    const ok = setText(fields[index], String(args.value || ""));
    if (!ok) return fail(spec.control);
    return {ok: true, control: spec.control, saw: pageSaw()};
  }

  function addSlot() {
    const spec = specOf(args.role);
    const root = sectionRoot(spec) || document.body;
    const buttons = allElements(root).filter(el => visible(el) && clickable(el) && !disabled(el));
    const hit = buttons.find(el => {
      const text = textOf(el);
      if (/上传/.test(text)) return false;
      return text === "+" || text === "add" || /添加/.test(text);
    });
    if (!hit) return fail(spec.control);
    click(hit);
    return {ok: true, control: spec.control, saw: pageSaw()};
  }

  function openImage() {
    const spec = specOf(args.role);
    const names = args.names || [];
    const root = sectionRoot(spec);
    if (!root) return fail(spec.control);
    const seen = textOf(root);
    if (names.some(name => name && seen.toLowerCase().includes(String(name).toLowerCase()))) {
      return {ok: true, already: true, control: spec.control, saw: pageSaw()};
    }
    const buttons = allElements(root).filter(el => visible(el) && clickable(el) && !disabled(el));
    const hit = buttons.find(el => {
      const text = textOf(el);
      if (/上传展示|上传的展示/.test(text)) return false;
      return text === "+" || text === "add" || /添加|选择图片|图片/.test(text);
    });
    if (!hit) return fail(spec.control);
    click(hit);
    return {ok: true, control: spec.control, saw: pageSaw()};
  }

  function searchFile() {
    const dialog = topDialog() || document.body;
    const filename = String(args.filename || "");
    const inputs = allElements(dialog).filter(el => {
      if (!visible(el) || disabled(el)) return false;
      const tag = el.tagName || "";
      const type = ((el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
      return type === "search" || tag === "INPUT" || tag === "TEXTAREA" || tag === "MATERIAL-INPUT";
    });
    const search = inputs.find(el => /搜索|search|名称/i.test(fieldHint(el) + " " + textOf(el))) || inputs[0];
    if (!search) return {ok: true, control: "素材选择器", saw: pageSaw(), searched: false};
    setText(search, filename);
    return {ok: true, control: "素材选择器 " + filename, saw: pageSaw(), searched: true};
  }

  function chooseFile() {
    const filename = String(args.filename || "");
    const stem = filename.replace(/\.[^.]+$/, "");
    const dialog = topDialog() || document.body;
    const hits = allElements(dialog).filter(visible).filter(el => {
      const text = textOf(el);
      if (!text || text.length > 180) return false;
      const low = text.toLowerCase();
      return low.includes(filename.toLowerCase()) || (stem && low.includes(stem.toLowerCase()));
    });
    hits.sort((a, b) => textOf(a).length - textOf(b).length);
    if (!hits.length) return fail("素材选择器 " + filename);
    click(hits[0]);
    return {ok: true, control: "素材选择器 " + filename, saw: pageSaw(), picked: textOf(hits[0]).slice(0, 80)};
  }

  function confirmPicker() {
    const dialog = topDialog();
    if (!dialog) return {ok: true, control: "素材选择器", saw: pageSaw(), closed: true};
    const buttons = allElements(dialog).filter(el => visible(el) && clickable(el) && !disabled(el));
    const labels = ["确认", "继续", "选择", "完成", "添加", "保存"];
    for (const label of labels) {
      const hit = buttons.find(el => {
        const text = textOf(el).trim();
        return (text === label || (text.includes(label) && text.length < 16)) && !/取消|关闭|上传/.test(text);
      });
      if (hit) {
        click(hit);
        return {ok: true, control: "素材选择器", saw: pageSaw(), clicked: textOf(hit).slice(0, 40)};
      }
    }
    return fail("素材选择器");
  }

  function editorOpen() {
    const saw = pageSaw();
    return /最终到达网址|最终网址|Final URL/.test(saw) && /商家名称|Business name/.test(saw);
  }

  function normStatus(text) {
    const value = String(text || "").trim();
    if (value === "已暂停" || value === "暂停") return "已暂停";
    if (value === "已启用" || value === "启用") return "已启用";
    return "";
  }

  function setStatus() {
    const want = String(args.label || "");
    if (!editorOpen()) return fail(want || "已暂停");
    const nodes = allElements().filter(el => visible(el) && clickable(el));
    const statusNodes = nodes.filter(el => normStatus(directText(el) || textOf(el)));
    statusNodes.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
    if (statusNodes.some(el => normStatus(directText(el) || textOf(el)) === want)) {
      return {ok: true, control: want, saw: pageSaw(), kept: true};
    }
    if (!statusNodes.length) return fail(want);
    click(statusNodes[0]);
    const option = allElements().filter(el => visible(el) && clickable(el)).find(el => normStatus(directText(el) || textOf(el)) === want);
    if (!option) return fail(want);
    click(option);
    return {ok: true, control: want, saw: pageSaw()};
  }

  function saveAd() {
    if (topDialog()) return fail("保存");
    if (!editorOpen()) return fail("保存");
    const labeled = allElements().filter(el => visible(el) && clickable(el) && !disabled(el)).map(el => ({el, text: textOf(el).trim()}));
    const bad = text => /取消|预览|关闭|上传|草稿/.test(text);
    let chosen = labeled.find(item => item.text === "保存广告" && !bad(item.text));
    if (!chosen) chosen = labeled.find(item => item.text === "保存" && !bad(item.text));
    if (!chosen) {
      const loose = labeled.filter(item => item.text.includes("保存") && !bad(item.text) && item.text.length < 12);
      loose.sort((a, b) => b.el.getBoundingClientRect().top - a.el.getBoundingClientRect().top);
      chosen = loose[0];
    }
    if (!chosen) return fail("保存");
    click(chosen.el);
    return {ok: true, control: "保存", saw: pageSaw(), clicked: chosen.text};
  }

  try {
    if (args.action === "look") return look();
    if (args.action === "click_plus") return clickPlus();
    if (args.action === "click_responsive") return clickResponsive();
    if (args.action === "fill_slot") return fillSlot();
    if (args.action === "add_slot") return addSlot();
    if (args.action === "open_image") return openImage();
    if (args.action === "search_file") return searchFile();
    if (args.action === "choose_file") return chooseFile();
    if (args.action === "confirm_picker") return confirmPicker();
    if (args.action === "set_status") return setStatus();
    if (args.action === "save") return saveAd();
    return fail(args.control || "未知步骤");
  } catch (err) {
    return fail(args.control || args.action || "页面", {error: String(err && err.message || err)});
  }
}
"""


def display_form_stopped_message(control: str, page_text: str) -> str:
    seen = " ".join(str(page_text or "").split())
    if len(seen) > 500:
        seen = seen[:500] + "…"
    if not seen:
        seen = "（页面上没有文字）"
    return (
        f"网页填广告停在「{control}」。\n"
        f"页面上实际看到：{seen}\n"
        "系列、广告组和已上传的图片都还在，没有重来。"
        "没有再向创建广告的协议提交。"
    )


class DisplayFormStopped(RuntimeError):
    """The ad editor stopped on one control. Campaign, group, and images stay."""

    def __init__(self, control: str, page_text: str):
        self.control = control
        self.page_text = page_text
        super().__init__(display_form_stopped_message(control, page_text))


def display_ad_list_url(current_url: str, ocid: str, campaign_id: str, ad_group_id: str) -> str:
    """Open the ad table for the campaign and ad group already created."""
    parsed = urllib.parse.urlsplit(str(current_url or ""))
    query = urllib.parse.parse_qs(parsed.query)
    kept: dict[str, str] = {}
    for key in ("euid", "__u", "__c", "uscid", "authuser", "hl", "__lu"):
        values = query.get(key) or []
        if values and str(values[0]).strip():
            kept[key] = str(values[0])
    kept["ocid"] = str(ocid)
    kept["campaignId"] = str(campaign_id)
    kept["adGroupId"] = str(ad_group_id)
    kept.setdefault("authuser", "0")
    kept.setdefault("hl", "zh_CN")
    return urllib.parse.urlunsplit((
        "https", "ads.google.com", "/aw/ads", urllib.parse.urlencode(kept), "",
    ))


def _page_expression(payload: dict[str, Any]) -> str:
    argument = json.dumps(payload, ensure_ascii=False)
    return "(() => {\n" + DISPLAY_AD_PAGE_SCRIPT + "\nreturn ggadsDisplayAd(" + argument + ");\n})()"


def _call_page(page: Any, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    raw = page.evaluate(_page_expression(payload), timeout=timeout)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {"ok": False, "control": payload.get("control") or payload.get("action") or "页面", "saw": raw}
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "control": str(payload.get("control") or payload.get("action") or "页面"),
            "saw": str(raw or ""),
        }
    return raw


def _require(page: Any, payload: dict[str, Any], control: str, *, tries: int = 1, pause: Callable[[float], None]) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for attempt in range(max(1, tries)):
        if attempt:
            pause(0.4)
        last = _call_page(page, payload)
        if last.get("ok"):
            return last
    raise DisplayFormStopped(str(last.get("control") or control), str(last.get("saw") or ""))


def _href(page: Any) -> str:
    try:
        return str(page.evaluate("window.location.href", timeout=8) or "")
    except Exception:
        return str(getattr(page, "url", "") or "")


def _open_ad_list(page: Any, url: str, *, pause: Callable[[float], None], timeout: float = 25.0) -> str:
    page.command("Page.navigate", {"url": url}, timeout=20)
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        last = _href(page)
        try:
            ready = str(page.evaluate("document.readyState", timeout=8) or "")
        except Exception:
            ready = ""
        parsed = urllib.parse.urlsplit(last)
        query = urllib.parse.parse_qs(parsed.query)
        wanted = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if (
            ready == "complete"
            and str((query.get("campaignId") or [""])[0]) == str((wanted.get("campaignId") or [""])[0])
            and str((query.get("adGroupId") or [""])[0]) == str((wanted.get("adGroupId") or [""])[0])
        ):
            return last
        if time.monotonic() >= deadline:
            raise DisplayFormStopped("广告列表", last)
        pause(0.4)


def _picker_filenames(asset: dict[str, Any], snapshot_assets: list[dict[str, Any]] | None) -> list[str]:
    """Names the asset library may show. The upload used the snapshot file name."""
    names: list[str] = []
    source_id = str(asset.get("source_asset_id") or "")
    for item in snapshot_assets or []:
        if source_id and str(item.get("source_asset_id") or "") != source_id:
            continue
        disk = Path(str(item.get("relative_path") or "")).name
        original = str(item.get("filename") or "").strip()
        for name in (disk, original):
            if name and name not in names:
                names.append(name)
    stored = str(asset.get("filename") or "").strip()
    if stored and stored not in names:
        names.append(stored)
    if not names:
        raise DisplayFormStopped(
            "素材选择器",
            f"素材 {asset.get('target_asset_id') or source_id or ''} 没有文件名，页面还没打开。",
        )
    return names


def _creative_form(source_ad: dict[str, Any]) -> dict[str, Any]:
    outer = source_ad.get("13") or {}
    mutable = outer.get("100") or {}
    creative = mutable.get("41") or {}
    final_urls = [str(value) for value in (mutable.get("1") or outer.get("5") or []) if value]
    headlines = _creative_texts(creative.get("5"))
    long_headline = " ".join(_creative_texts(creative.get("6")))
    descriptions = _creative_texts(creative.get("7"))
    business_name = " ".join(_creative_texts(creative.get("8")))
    return normalize_creative(
        final_url=final_urls[0] if final_urls else "",
        business_name=business_name,
        headlines=headlines,
        long_headline=long_headline,
        descriptions=descriptions,
    )


def _status_label(expected_status: int) -> str:
    try:
        return str(CHILD_STATUS_LABELS.get(int(expected_status)) or "")
    except (TypeError, ValueError):
        return ""


def _wrong_status(rows: list[dict[str, Any]], group_id: str, reserved: set[str], expected_status: int) -> list[dict[str, Any]]:
    fresh = []
    for row in rows:
        if str(row.get("ad_group_id") or "") != str(group_id):
            continue
        if str(row.get("ad_id") or "") in reserved:
            continue
        try:
            status = int(row.get("status_raw") or 0)
        except (TypeError, ValueError):
            status = 0
        if status in (0, 3, int(expected_status)):
            continue
        fresh.append(row)
    return fresh


def _key_visible(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker.lower() in low for marker in _KEY_MARKERS)


def _form_error(text: str) -> bool:
    raw = str(text or "")
    return any(marker in raw for marker in _FORM_ERRORS)


def _fill_values(page: Any, role: str, values: list[str], *, pause: Callable[[float], None]) -> None:
    for index, value in enumerate(values):
        result = _call_page(page, {"action": "fill_slot", "role": role, "index": index, "value": value})
        if result.get("need_add"):
            added = _call_page(page, {"action": "add_slot", "role": role})
            if not added.get("ok"):
                raise DisplayFormStopped(str(added.get("control") or role), str(added.get("saw") or result.get("saw") or ""))
            pause(0.4)
            result = _call_page(page, {"action": "fill_slot", "role": role, "index": index, "value": value})
        if not result.get("ok"):
            raise DisplayFormStopped(str(result.get("control") or role), str(result.get("saw") or ""))


def _select_images(
    page: Any,
    role: str,
    assets: list[dict[str, Any]],
    snapshot_assets: list[dict[str, Any]] | None,
    *,
    pause: Callable[[float], None],
) -> None:
    for asset in assets:
        names = _picker_filenames(asset, snapshot_assets)
        opened = _call_page(page, {"action": "open_image", "role": role, "names": names})
        if opened.get("already"):
            continue
        if not opened.get("ok"):
            raise DisplayFormStopped(str(opened.get("control") or role), str(opened.get("saw") or ""))
        pause(0.5)
        chosen: dict[str, Any] = {}
        for name in names:
            _call_page(page, {"action": "search_file", "filename": name, "role": role})
            pause(0.6)
            chosen = _call_page(page, {"action": "choose_file", "filename": name, "role": role})
            if chosen.get("ok"):
                break
        if not chosen.get("ok"):
            raise DisplayFormStopped(
                str(chosen.get("control") or f"素材选择器 {' / '.join(names)}"),
                str(chosen.get("saw") or ""),
            )
        pause(0.3)
        confirmed = _call_page(page, {"action": "confirm_picker", "role": role, "filename": name})
        if not confirmed.get("ok"):
            raise DisplayFormStopped(str(confirmed.get("control") or "素材选择器"), str(confirmed.get("saw") or ""))
        pause(0.4)


def _wait_for_paused_ad(
    page: Any,
    *,
    account: str,
    read_ads: Callable[[], list[dict[str, Any]]],
    group_id: str,
    expected_status: int,
    reserved_ids: set[str],
    pause: Callable[[float], None],
    key_timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, float(key_timeout))
    last_saw = ""
    while True:
        try:
            rows = read_ads()
        except HumanActionRequired:
            raise
        except Exception as exc:
            rows = []
            last_saw = f"读取广告列表失败：{exc}"
        matched = _matching_ads(rows, group_id, reserved_ids, expected_status)
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            return _prefer_newest_ad(matched)
        wrong = _wrong_status(rows, group_id, reserved_ids, expected_status)
        if wrong:
            raise RuntimeError(
                "网页保存之后，广告不是要的状态。"
                + describe_unreadable_ad(rows, group_id, reserved_ids, expected_status)
                + " 系列、广告组和已上传的图片都还在，没有重来。"
            )
        try:
            looked = _call_page(page, {"action": "look"})
            last_saw = str(looked.get("saw") or last_saw)
        except Exception as exc:
            if not last_saw:
                last_saw = str(exc)
        if _form_error(last_saw) and not _key_visible(last_saw):
            raise DisplayFormStopped("保存", last_saw)
        if time.monotonic() >= deadline:
            who = f"目标账号 {account}" if str(account or "").strip() else "目标账号"
            seen = " ".join(last_saw.split())
            if len(seen) > 500:
                seen = seen[:500] + "…"
            if not seen:
                seen = "（页面上没有文字）"
            raise HumanActionRequired(
                "reauth",
                (
                    f"网页填广告停在「保存」。保存已经点过，等了 3 分钟，{who} 的广告列表里还没有新的暂停广告。\n"
                    f"页面上实际看到：{seen}\n"
                    "请在比特浏览器这个目标账号的广告页面上摸密钥，密钥框是网页自己弹的。"
                    "系列、广告组和已上传的图片都还在。不用改预算，也不用自己填广告。"
                    f"摸完后点「{KEY_DONE_BUTTON}」。"
                ),
            )
        pause(0.8)


def place_display_ad_on_page(
    page: Any,
    *,
    account: str,
    ocid: str,
    campaign_id: str,
    ad_group_id: str,
    source_ad: dict[str, Any],
    landscape_assets: list[dict[str, Any]],
    square_assets: list[dict[str, Any]],
    portrait_assets: list[dict[str, Any]] | None = None,
    snapshot_assets: list[dict[str, Any]] | None = None,
    read_ads: Callable[[], list[dict[str, Any]]],
    group_id: str,
    expected_status: int,
    reserved_ids: set[str],
    log: Callable[[str], None],
    key_timeout: float = 180.0,
    pause: Callable[[float], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create one responsive display ad by clicking the logged-in Ads page.

    A paused ad already in the group is recorded and the editor is not opened.
    Images are chosen in the asset picker by the names already uploaded.
    """
    sleeper = pause or time.sleep
    reserved = {str(item) for item in reserved_ids}
    rows = read_ads()
    matched = _matching_ads(rows, group_id, reserved, expected_status)
    if len(matched) == 1:
        log(f"广告组里已经有要的广告 {matched[0].get('ad_id')}，直接记下，不再填写")
        return matched[0], False
    if len(matched) > 1:
        chosen = _prefer_newest_ad(matched)
        log(f"广告组里有 {len(matched)} 条要的广告，先用了 {chosen.get('ad_id')}，不再填写")
        return chosen, False
    pending = [
        row for row in rows
        if str(row.get("ad_group_id") or "") == str(group_id)
        and str(row.get("ad_id") or "") not in reserved
        and int(row.get("status_raw") or 0) != 3
        and int(row.get("status_raw") or 0) != int(expected_status)
    ]
    if pending:
        raise RuntimeError(
            "网页还没填。广告组里已经有一条不是目标状态的广告，没有再新建。"
            + describe_unreadable_ad(rows, group_id, reserved, expected_status)
            + " 系列、广告组和已上传的图片都还在。"
        )
    portraits = list(portrait_assets or [])
    portable_display_ad_request(
        ocid=ocid,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        temporary_id="-1",
        source_ad=source_ad,
        landscape_assets=landscape_assets,
        square_assets=square_assets,
        portrait_assets=portraits,
        target_child_status=expected_status,
    )
    creative = _creative_form(source_ad)
    label = _status_label(expected_status)
    if label not in {"已暂停", "已启用"}:
        raise DisplayFormStopped("已暂停", f"要的广告状态是 {expected_status}，页面还没打开。")
    log(DISPLAY_AD_FORM_LOG)
    current = _href(page)
    url = display_ad_list_url(current, ocid, campaign_id, ad_group_id)
    _open_ad_list(page, url, pause=sleeper)
    looked = _call_page(page, {"action": "look"})
    if not looked.get("editor"):
        _require(page, {"action": "click_plus"}, "蓝色加号", tries=3, pause=sleeper)
        _require(page, {"action": "click_responsive"}, "自适应展示广告", tries=8, pause=sleeper)
        editor: dict[str, Any] = {}
        for _attempt in range(20):
            editor = _call_page(page, {"action": "look"})
            if editor.get("editor"):
                break
            sleeper(0.4)
        else:
            raise DisplayFormStopped("自适应展示广告", str(editor.get("saw") or ""))
    _fill_values(page, "url", [creative["final_url"]], pause=sleeper)
    _fill_values(page, "headline", list(creative["headlines"]), pause=sleeper)
    _fill_values(page, "long_headline", [creative["long_headline"]], pause=sleeper)
    _fill_values(page, "description", list(creative["descriptions"]), pause=sleeper)
    _fill_values(page, "business", [creative["business_name"]], pause=sleeper)
    _select_images(page, "landscape", landscape_assets, snapshot_assets, pause=sleeper)
    _select_images(page, "square", square_assets, snapshot_assets, pause=sleeper)
    if portraits:
        _select_images(page, "portrait", portraits, snapshot_assets, pause=sleeper)
    _require(page, {"action": "set_status", "label": label}, label, tries=2, pause=sleeper)
    _require(page, {"action": "save"}, "保存", tries=2, pause=sleeper)
    log(DISPLAY_AD_KEY_LOG)
    created = _wait_for_paused_ad(
        page,
        account=account,
        read_ads=read_ads,
        group_id=group_id,
        expected_status=expected_status,
        reserved_ids=reserved,
        pause=sleeper,
        key_timeout=key_timeout,
    )
    log(f"广告列表读到暂停广告 {created.get('ad_id')}")
    return created, True
