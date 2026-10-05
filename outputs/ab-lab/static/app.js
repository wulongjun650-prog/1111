import { createDashboard } from './dashboard.mjs';
import { applyTheme } from './theme.mjs';
import { themeFor, countryBadge, deviceBadge, icon, reputationView } from './presentation.mjs';
import { createReputationAutoCheck } from './reputation.mjs';
import { createCloudflareAutoCheck } from './cloudflare-status.mjs';
import { patchConfig, rulesConfig, parseLinks, previewURL, logPath, validateUpload, sitePath } from './helpers.mjs';
import { countryLabel } from './locale.mjs';
import { createSelection } from './selection.mjs';
import { isAdmin, chooseSite, accountSitePath } from './account-ui.mjs';
import { createSourceEditor } from './source-editor.mjs';
import { createBRedirects } from './b-redirects.mjs';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const csrf = $('meta[name="csrf-token"]').content;
const target = $('meta[name="target-url"]').content;
let state = null;
let selectedSite = null;
let account = {role: document.body.dataset.accountRole};
let pendingActions = 0;
let siteLoading = true;
let catalog = null;
let dirty = false;
let editing = 0;
let configBusy = false;
let stateRequest = 0;
let logsRequest = 0;
let domainsRequest = 0;
let siteEventsRequest = 0;
let siteEventsSite = null;
let logPage = 1;
let logPages = 1;
let activeTab = 'overview';
const dashboard = createDashboard(api, () => isAdmin(account) ? '所有域名' : '我的域名');
const sourceEditor = createSourceEditor({api, on, element, confirmAction, getSite:() => state?.site,
  onSaved:async result => {toast(`${result.version.slot} 源码已保存并发布，旧版本已保留。`); await refreshState();},
});
const bRedirects = createBRedirects({api, on, element, confirmAction, getSite:() => state?.site,
  onApplied:async result => {
    if (!result.check) toast(result.cloudflare?.ok ? 'WhatsApp 号码已更换并发布，Cloudflare 缓存已清除。' : result.cloudflare ? 'WhatsApp 号码已更换。Cloudflare 缓存没清掉，请稍后再试。' : 'WhatsApp 号码已更换并发布。进线语未改。', Boolean(result.cloudflare && !result.cloudflare.ok));
    else toast(result.check.status === 'unknown' ? 'B 页已换链并发布；此链接状态未知，请确认目标页面。' : 'B 页已换链并发布，旧版本已保留。');
    await refreshState();
  },
});
const reputationChecking = new Set();
const cloudflareChecking = new Set();
const latestReputation = new Map();
const reputationAutoCheck = createReputationAutoCheck({
  check:async id=>(await api(`/api/sites/${id}/reputation/check`,{method:'POST',body:{}})).result,
  isActive:()=>activeTab === 'domains' && !document.hidden && Boolean(catalog?.google_reputation_configured),
  onUpdate:(id,update)=>{
    if (update.checking === true) reputationChecking.add(id);
    if (update.checking === false) reputationChecking.delete(id);
    if (update.result) {
      latestReputation.set(id,update.result);
      const site = catalog?.sites.find(item=>item.id === id);
      if (site) site.google_reputation = update.result;
    }
    const site = catalog?.sites.find(item=>item.id === id);
    const cell = $$('[data-reputation-site]').find(item=>item.dataset.reputationSite === id);
    if (site && cell) renderReputation(site,cell);
  },
});
const cloudflareAutoCheck = createCloudflareAutoCheck({
  check:async id=>(await api(`/api/sites/${id}/cloudflare/status`,{method:'POST',body:{}})),
  isActive:()=>activeTab === 'domains' && !document.hidden && Boolean(catalog?.cloudflare_configured),
  onUpdate:(id,update)=>{
    if (update.checking === true) cloudflareChecking.add(id);
    if (update.checking === false) cloudflareChecking.delete(id);
    const site = catalog?.sites.find(item=>item.id === id);
    if (update.result?.site && site) {
      const becameActive = site.cf_status !== 'active' && update.result.site.cf_status === 'active';
      Object.assign(site, update.result.site);
      if (becameActive) toast(`${site.domain} 的 Cloudflare 已生效。`);
    }
    if (catalog) renderDomains();
  },
});
const rulesForm = $('#rules-form');
$$('[data-tab]').forEach(button => button.querySelector('span').replaceChildren(icon(button.dataset.tab === 'accounts' ? 'logs' : button.dataset.tab)));
const selections = Object.fromEntries(['countries','languages'].map(kind => [kind,createSelection(rulesForm.elements.namedItem(kind),kind)]));
const labels = {
  domains: ['域名管理', '域名管理', '统一查看谷歌风险、接入状态、备注与访问状态。'],
  overview: ['流量总览', '流量总览', '查看访问数据、国家分布与各站点表现。'],
  content: ['A/B 内容', 'A/B 内容', '先看现在用的那一版，再换链接或发布。'],
  rules: ['访问规则', '访问规则', '设置访问条件，保存后对当前站点生效。'],
  simulate: ['规则模拟', '规则模拟', '输入访问条件，检查已保存规则的判断结果。'],
  logs: ['访问日志', '访问日志', '查看文档请求、设备、国家与分流结果。'],
  accounts: ['账号管理', '账号管理', '创建代理账号，管理访问权限与域名归属。'],
};
const reasons = { blacklist: '命中黑名单', whitelist: '命中白名单', strict_bot: '严格防爬虫', bot_marker: '匹配机器人标记', ipv4: 'IPv4 限制', device: '设备限制', os_version: '系统版本限制', blocked_cidr: '命中屏蔽网段', country: '国家 / 地区限制', country_unknown: '国家未知', language: '语言限制', visit_limit: '超过访问次数', allowed: '规则通过', pass: '规则通过', force_a: '强制 A', force_b: '强制 B', protection_off: '防护已关闭' };
const formatDate = value => {
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
};
reasons.manual = '手动指定';
const traceLabels = { blacklist: '黑名单检查', whitelist: '白名单检查', strict_bot: '严格防爬虫', bot_marker: 'UA 特征检查', ipv4: 'IPv4 检查', device: '设备检查', os_version: '系统版本检查', blocked_cidr: '自定义网段检查', country: '国家检查', country_unknown: '国家检查', language: '语言检查', visit_limit: '访问次数检查' };
const number = value => Number(value ?? 0).toLocaleString('zh-CN');
const bytes = value => Number(value) < 1024 * 1024 ? `${(Number(value) / 1024).toFixed(1)} KiB` : `${(Number(value) / 1024 / 1024).toFixed(1)} MiB`;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function toast(message, error = false) {
  const item = element('div', `toast${error ? ' error' : ''}`);
  item.setAttribute('role', error ? 'alert' : 'status');
  item.append(element('span', '', message));
  const close = element('button', '', '×');
  close.type = 'button';
  close.setAttribute('aria-label', '关闭通知');
  close.addEventListener('click', () => item.remove());
  item.append(close);
  $('#toast-region').append(item);
  if (!error) window.setTimeout(() => item.remove(), 6000);
}

function confirmAction(message) {
  return new Promise(resolve => {
    const dialog = element('dialog', 'confirm-dialog');
    const title = element('h2', '', '确认操作');
    title.id = 'confirm-title'; dialog.setAttribute('aria-labelledby', title.id);
    const text = element('p', '', message);
    const actions = element('div', 'actions');
    const cancel = element('button', 'button secondary', '取消');
    const accept = element('button', 'button primary', '确认');
    const finish = value => { dialog.close(); dialog.remove(); resolve(value); };
    cancel.addEventListener('click', () => finish(false));
    accept.addEventListener('click', () => finish(true));
    dialog.addEventListener('cancel', event => { event.preventDefault(); finish(false); });
    actions.append(cancel, accept); dialog.append(title, text, actions);
    document.body.append(dialog); dialog.showModal(); cancel.focus();
  });
}

// Every asynchronous UI event is handled through this boundary.
function on(node, event, handler) {
  node.addEventListener(event, async eventObject => {
    pendingActions++;
    updateSiteLock();
    try { await handler(eventObject); }
    catch (error) { if (!error.stale) toast(error.message || '操作失败，请稍后重试。', true); }
    finally { pendingActions--; updateSiteLock(); }
  });
}

function updateSiteLock() {
  $('#site-selector').disabled = !selectedSite || siteLoading || pendingActions > 0 || configBusy;
}

async function busy(button, action) {
  if (button?.disabled) return;
  if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
  try { return await action(); }
  finally { if (button) { button.disabled = false; button.removeAttribute('aria-busy'); } }
}

async function api(path, { method = 'GET', body, raw = false, timeout } = {}) {
  const siteAtStart = selectedSite;
  const scopedPath = accountSitePath(path, siteAtStart);
  const siteScoped = scopedPath !== path || /^\/api\/sites\/(default|[a-f0-9]{32})\//.test(path);
  const headers = {};
  if (method !== 'GET') headers['X-CSRF-Token'] = csrf;
  if (body !== undefined) headers['Content-Type'] = raw ? 'application/octet-stream' : 'application/json';
  let response;
  try {
    response = await fetch(scopedPath, { method, headers, credentials: 'same-origin', cache: 'no-store', body: body === undefined ? undefined : raw ? body : JSON.stringify(body), signal: AbortSignal.timeout(timeout || (raw ? 120000 : 20000)) });
  } catch (error) {
    throw new Error(error.name === 'TimeoutError' ? '请求超时。操作可能已完成，请刷新状态确认后再试。' : '无法连接服务，请检查服务状态后重试。');
  }
  let data;
  try { data = await response.json(); } catch { throw new Error('服务返回了无法读取的数据，请刷新后重试。'); }
  if (siteScoped && selectedSite !== siteAtStart) {
    const error = new Error('站点已切换，忽略旧响应'); error.stale = true; throw error;
  }
  if (!response.ok) {
    if (response.status === 401) { window.location.assign('/login'); throw new Error('会话已过期，请重新登录。'); }
    const messages = { 400: '提交内容有误，请检查输入。', 403: '安全校验失败，请重新加载页面后重试。', 404: '内容已不存在，请刷新状态。', 409: '配置已被其他操作更新，已重新同步。你的规则草稿仍保留，请检查后再次保存。', 413: '文件过大，上传上限为 20 MiB。', 422: '输入未通过校验，请检查 IP、CIDR、网址或数值。', 429: '操作过于频繁，请稍后重试。' };
    const error = new Error(messages[response.status] || '服务暂时无法完成操作，请稍后重试。');
    error.status = response.status;
    if (response.status === 404 && siteScoped) {
      await loadDomains();
      if (selectedSite !== siteAtStart) error.stale = true;
    }
    if ([400, 422].includes(response.status) && typeof data.detail === 'string') error.message += ` ${data.detail}`;
    throw error;
  }
  return data;
}

if ($('#logout')) on($('#logout'), 'click', async () => {
  if (!await confirmAction('退出管理后台？未保存的修改不会保留。')) return;
  await api('/api/logout', { method: 'POST', body: {} });
  window.location.replace('/login');
});

function setDirty(value) {
  dirty = value;
  $('#rules-dirty').textContent = value ? '有未保存的修改 · 刷新不会覆盖草稿' : '配置已同步';
  $('#rules-dirty').classList.toggle('dirty', value);
}

function fillRules() {
  for (const [name, value] of Object.entries(state.config.rules)) {
    const input = rulesForm.elements.namedItem(name);
    if (!input) continue;
    if (selections[name]) selections[name].set(value);
    else if (input.type === 'checkbox') input.checked = Boolean(value);
    else input.value = Array.isArray(value) ? value.join(input.tagName === 'TEXTAREA' ? '\n' : ', ') : value;
  }
  setDirty(false);
}

function syncDeliveryPanels() {
  const linkMode = state?.config?.content_mode === 'LINK';
  const linksCard = $('#links-card');
  const distributionField = $('#distribution-field');
  if (linksCard) linksCard.hidden = !linkMode;
  if (distributionField) distributionField.hidden = !linkMode;
}

function updateConfigControls() {
  syncDeliveryPanels();
  $('#routing-controls').disabled = !state || configBusy;
  $('#allowed-content-controls').disabled = !state || configBusy;
  $('#content-controls').disabled = !state || configBusy;
  $('#protection-toggle').disabled = !state || configBusy;
  $('#hero-switch').disabled = !state || configBusy;
  applyTheme(themeFor(state?.config));
  if (!state) return;
  const angel = themeFor(state.config) === 'angel';
  const forced = state.config.routing !== 'RULES';
  $('#hero-title').textContent = `放行配置 · 内容 ${angel ? 'A' : 'B'}`;
  $('#hero-description').textContent = !state.site.enabled ? '此域名已下线，配置保留，上线后恢复访问。' : forced ? '全局强制模式：所有访问固定展示此内容。' : !state.config.protection ? '规则防护已关闭：所有访客展示此内容。' : '通过规则的访问，将展示此内容。';
  $('#hero-mode').textContent = !state.site.enabled ? '域名已下线' : forced ? `全局强制 · 全部展示 ${state.config.routing.slice(-1)}` : `规则模式 · 放行后展示 ${state.config.allowed_slot}`;
  $('#sidebar-theme').textContent = `当前展示内容 ${angel ? 'A' : 'B'}`;
  $$('[data-routing]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.routing === state.config.routing)));
  const slot = state.config.allowed_slot;
  const active = state.config.routing === 'RULES';
  $$('[data-allowed-slot]').forEach(button => {
    const selected = button.dataset.allowedSlot === (button.closest('#hero-switch') ? (angel ? 'A' : 'B') : slot);
    button.setAttribute('aria-pressed', String(selected));
    button.classList.toggle('primary', selected);
    button.classList.toggle('secondary', !selected);
    const caption = button.querySelector('[data-slot-caption]');
    if (caption) caption.textContent = selected ? active ? '当前启用' : '强制展示' : '切换内容';
  });
  $('#allowed-slot-badge').textContent = `已选择 ${slot}${active ? '' : ' · 暂未生效'}`;
  $('#overview-allowed-status').textContent = `放行后展示 ${slot}${active ? '' : '（全局强制模式下暂不生效）'}`;
  const status = $('#allowed-content-status');
  status.className = active && state.config.protection ? 'subtle' : 'notice warning';
  status.textContent = !active ? `当前为全局强制 ${state.config.routing.slice(-1)}，已跳过规则。点击上方按钮可恢复按规则分流。`
    : !state.config.protection ? `注意：规则防护已关闭，所有访客都将展示 ${slot}。如需过滤，请到“概览”开启规则防护。`
    : `当前生效：拦截 → A；放行 → ${slot}。已保存的规则保持不变。`;
  const ready = state.config.content_mode === 'PAGE' ? Boolean(state.slots[slot]) : state.links.some(link => link.slot === slot);
  if (!ready) status.textContent += ` 注意：${slot} 尚未${state.config.content_mode === 'PAGE' ? '发布页面' : '配置链接'}，放行访问将返回维护提示。`;
  $('#protection-toggle').setAttribute('aria-checked', String(state.config.protection));
  $('#content-mode').value = state.config.content_mode;
  $('#distribution').value = state.config.distribution;
  $('#distribution').disabled = state.config.content_mode !== 'LINK';
}

function renderState() {
  $$('.tab-panel').forEach(panel => { panel.inert = false; });
  $('#open-target').href = state.target_url;
  $('#open-target').hidden = false;
  $('#site-context').textContent = `仅修改 ${state.site.domain || '当前站点'} 的配置 · 图表范围可独立选择`;
  if (activeTab === 'overview') dashboard.refresh();
  const geo = state.health.geoip_detail;
  $('#geoip-status').textContent = state.health.geoip ? `已就绪 · ${geo?.built_at ? new Date(geo.built_at * 1000).toLocaleDateString('zh-CN') : '本地数据库'}` : '国家库不可用 · 请检查安装';
  $('#geoip-warning').hidden = Boolean(state.health.geoip);
  $('#current-mode').textContent = state.config.content_mode === 'PAGE' ? '页面内容' : '链接跳转';
  $('#published-count').textContent = `${Object.values(state.slots).filter(Boolean).length} / 2 个内容位`;
  $('#last-sync').textContent = `最近同步 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
  $('#connection').textContent = '服务已连接';
  $('#connection').className = 'badge green';
  $('#load-error').hidden = true;
  for (const id of ['rules-fields', 'links-fields', 'simulate-fields', 'reset-counters']) $(`#${id}`).disabled = false;
  updateConfigControls();
  if (!dirty) fillRules();
  renderSlots();
  bRedirects.update(state);
  renderLinks();
}

async function refreshState() {
  if (!selectedSite) return;
  const request = ++stateRequest;
  try {
    const result = await api('/api/state');
    if (request !== stateRequest) return;
    state = result;
    renderState();
  } catch (error) {
    if (request === stateRequest) {
      $('#connection').textContent = '连接异常';
      $('#connection').className = 'badge red';
      $('#load-error').hidden = false;
    }
    throw error;
  }
}

async function saveConfig(config, ruleSave = false) {
  if (!state || configBusy) return;
  configBusy = true;
  const editAtStart = editing;
  ++stateRequest; // Ignore reads that started before this write.
  updateConfigControls();
  try {
    const updated = await api('/api/config', { method: 'PUT', body: { config, revision: state.revision } });
    ++stateRequest;
    state = updated;
    if (ruleSave && editing === editAtStart) setDirty(false);
    renderState();
    toast(ruleSave ? '访问规则已保存。' : '配置已更新，即刻生效。');
  } catch (error) {
    if (error.status === 409) {
      try { await refreshState(); } catch { toast('重新同步失败，草稿仍保留。请刷新状态后重试。', true); }
    }
    throw error;
  } finally { configBusy = false; updateConfigControls(); updateSiteLock(); }
}

function actionButton(text, action, className = 'button secondary small') {
  const button = element('button', className, text);
  button.type = 'button';
  on(button, 'click', () => busy(button, action));
  return button;
}

function renderSlots() {
  const root = $('#slot-cards');
  // Keep native file inputs intact on refresh, including any selected file.
  for (const slot of ['A', 'B']) {
    let card = $(`#slot-${slot}`);
    if (!card) {
      card = element('article', 'card'); card.id = `slot-${slot}`;
      const heading = element('div', 'slot-heading');
      heading.append(element('span', `slot-letter slot-${slot.toLowerCase()}`, slot));
      const title = element('div'); title.append(element('h2', '', `${slot} 内容`), element('small', '', slot === 'A' ? '拦截时看到的页面' : '放行后选 B 时看到的页面')); heading.append(title);
      const live = element('div', 'slot-live'); live.id = `slot-live-${slot}`;
      const form = element('form', 'upload-form');
      const label = element('label', '', '导入新版本');
      const input = element('input'); input.type = 'file'; input.accept = '.zip,.html,.htm'; input.required = true; input.name = 'file'; input.disabled = !state;
      label.append(input);
      const submit = element('button', 'button secondary small', '上传并创建版本'); submit.type = 'submit'; submit.disabled = !state;
      form.append(label, element('small', '', 'ZIP 或 HTML，最大 20 MB。上传后要点下面的「用这一版」才会生效。'), submit);
      on(form, 'submit', async event => {
        event.preventDefault();
        await busy(submit, async () => {
          const file = input.files[0]; validateUpload(file);
          input.disabled = true;
          try {
            const uploaded = await api(`/api/upload/${slot}?name=${encodeURIComponent(file.name)}`, { method: 'POST', raw: true, body: file });
            input.value = '';
            toast(`${slot} 版本已导入，尚未发布。`);
            await refreshState();
            if (slot === 'B') await bRedirects.selectVersion(uploaded.version.id);
          } finally { input.disabled = false; }
        });
      });
      const versions = element('div', 'version-list'); versions.id = `versions-${slot}`;
      card.append(heading, live, form, versions); root.append(card);
    }
    if (state) card.querySelectorAll('.upload-form input, .upload-form button').forEach(control => { if (!control.hasAttribute('aria-busy')) control.disabled = false; });
    const current = state?.versions.find(version => version.id === state.slots[slot]);
    const live = $(`#slot-live-${slot}`);
    live.replaceChildren(element('span', 'badge green', '当前发布'));
    if (current) {
      live.append(element('strong', '', formatDate(current.created)));
      live.append(element('small', '', `${current.name} · ${number(current.files)} 个文件`));
      live.append(actionButton('编辑源码', () => sourceEditor.open(current), 'button secondary small slot-source-edit'));
    } else live.append(element('strong', '', state?.slots[slot] ? '已发布版本' : '尚未发布'));
    const versions = $(`#versions-${slot}`); versions.replaceChildren();
    const items = state?.versions.filter(version => version.slot === slot) || [];
    const extras = slot === 'B' ? items.filter(version => version.id !== state?.slots.B) : [];
    if (extras.length) {
      const cleanup = element('div', 'version-cleanup');
      cleanup.append(element('small', '', state.slots.B ? `还有 ${extras.length} 个未发布版本` : `还没发布。这 ${extras.length} 个版本都可以清掉`));
      cleanup.append(actionButton('清理未发布版本', async () => {
        const message = state.slots.B
          ? `彻底删除 ${extras.length} 个未发布的 B 版本？只保留当前发布版本。源码和记录都会删除，无法恢复。`
          : `当前没有发布中的 B 版本。彻底删除全部 ${extras.length} 个 B 版本？源码和记录都会删除，无法恢复。`;
        if (!await confirmAction(message)) return;
        const result = await api('/api/versions/B/cleanup', { method: 'POST', body: {} });
        toast(`已删除 ${result.deleted} 个未发布 B 版本。`);
        await refreshState();
      }, 'button quiet small'));
      versions.append(cleanup);
    }
    if (!items.length) versions.append(element('p', 'empty-state', state ? '还没有版本。先导入一个页面。' : '连接服务后，版本会显示在这里。'));
    const publishedId = state?.slots[slot];
    const sorted = [...items].sort((a, b) => Number(b.created) - Number(a.created) || String(b.id).localeCompare(String(a.id)));
    const published = sorted.find(version => version.id === publishedId) || null;
    const newestOther = sorted.find(version => version !== published) || null;
    const visible = [published, newestOther].filter(Boolean);
    const visibleIds = new Set(visible.map(version => version.id));
    const older = sorted.filter(version => !visibleIds.has(version.id));
    const appendVersion = (version, parent) => {
      const isPublished = version.id === publishedId;
      const row = element('div', 'version-row');
      row.append(element('div', 'version-name', formatDate(version.created)));
      row.append(element('p', 'version-meta', `${version.name} · ${number(version.files)} 个文件 · ${bytes(version.bytes)}`));
      const actions = element('div', 'actions');
      actions.append(actionButton('预览', () => preview(version)));
      actions.append(actionButton('编辑源码', () => sourceEditor.open(version)));
      if (slot === 'B' && !isPublished) actions.append(actionButton('删除', async () => {
        if (!await confirmAction(`彻底删除「${version.name}」？源码和记录都会删除，无法恢复。当前发布版本不受影响。`)) return;
        await api(`/api/versions/B/${version.id}`, { method: 'DELETE' });
        toast('B 版本已删除。');
        await refreshState();
      }, 'button quiet small'));
      if (isPublished) actions.append(element('span', 'badge green', '当前发布'));
      else actions.append(actionButton('用这一版', async () => {
        if (!await confirmAction(`改用 ${formatDate(version.created)} 的「${version.name}」作为 ${slot}？当前这一版会留下来，随时可以换回去。`)) return;
        await api(`/api/publish/${slot}`, { method: 'POST', body: { version_id: version.id } });
        toast(`${slot} 已换成这一版。`); await refreshState();
      }, 'button primary small'));
      const hash = element('details', 'version-hash'); hash.append(element('summary', '', '版本校验值'), element('span', '', version.sha256));
      row.append(actions, hash); parent.append(row);
    };
    for (const version of visible) appendVersion(version, versions);
    if (older.length) {
      const details = element('details', 'version-older');
      details.open = versions.dataset.olderOpen === 'true';
      details.append(element('summary', '', `更早的 ${older.length} 个版本`));
      details.addEventListener('toggle', () => { versions.dataset.olderOpen = String(details.open); });
      for (const version of older) appendVersion(version, details);
      versions.append(details);
    }
  }
}

let previewRequest = 0;
async function preview(version) {
  const request = ++previewRequest;
  const result = await api('/api/preview', { method: 'POST', body: { version_id: version.id } });
  if (request !== previewRequest) return;
  const url = previewURL(result.url, target);
  const frame = element('iframe');
  frame.title = `${version.slot} 内容预览：${version.name}`;
  frame.setAttribute('sandbox', 'allow-scripts allow-forms');
  frame.referrerPolicy = 'no-referrer'; frame.loading = 'lazy'; frame.src = url;
  $('#preview-frame').replaceChildren(frame);
  $('#preview-title').textContent = `${version.slot} · ${version.name}`;
  $('#preview-panel').hidden = false;
  $('#preview-panel').scrollIntoView({ block: 'start', behavior: 'smooth' });
}

function closePreview() {
  ++previewRequest;
  $('#preview-frame').replaceChildren(); $('#preview-panel').hidden = true;
}

function renderLinks() {
  $('#link-count').textContent = `${number(state.links.length)} 个链接`;
  const root = $('#links-list'); root.replaceChildren();
  if (!state.links.length) root.append(element('p', 'empty-state', '还没有链接。每行一个，添加后就会开始分配。'));
  for (const link of state.links) {
    const row = element('div', 'link-row');
    row.append(element('span', `badge ${link.slot === 'A' ? 'amber' : 'indigo'}`, link.slot), element('span', 'link-url', link.url), element('span', 'link-hits', `${number(link.hits)} 次访问`));
    row.append(actionButton('删除', async () => {
      if (!await confirmAction(`删除此 ${link.slot} 链接？\n${link.url}`)) return;
      await api(`/api/links/${encodeURIComponent(link.id)}`, { method: 'DELETE' });
      toast('链接已删除。'); await refreshState();
    }, 'text-button danger'));
    root.append(row);
  }
}

async function loadAudit() {
  if (!selectedSite || !state) return;
  const request = stateRequest;
  const data = await api('/api/audit');
  if (request !== stateRequest) return;
  const root = $('#audit-list'); root.replaceChildren();
  if (!data.items.length) root.append(element('p', 'empty-state', '暂无操作记录。配置、上传与发布后可在这里查看。'));
  for (const item of data.items) {
    const row = element('div', 'audit-item');
    row.append(element('strong', '', item.action), element('small', '', `${formatDate(item.created)} · ${typeof item.detail === 'string' ? item.detail : JSON.stringify(item.detail)}`)); root.append(row);
  }
}

function logParams() {
  const form = $('#log-filters');
  return new URLSearchParams({ days: form.elements.days.value, slot: form.elements.slot.value });
}

function visitDeviceLine(details) {
  if (!details) return 'iOS 18 以上 · Safari';
  let os = details.os || '';
  let browser = details.browser || '';
  if (!os || os === '系统未知') os = 'iOS 18 以上';
  if (!browser || browser === '浏览器未知') browser = os.startsWith('iOS') || os.startsWith('macOS') ? 'Safari' : '内置浏览器';
  const osMajor = Number((os.match(/iOS\s+(\d+)/) || [])[1]);
  const safariMajor = Number((browser.match(/Safari\s+(\d+)/) || [])[1]);
  if (os.startsWith('iOS') && safariMajor > osMajor) os = `iOS ${safariMajor} 以上`;
  return `${os} · ${browser}`;
}

async function loadLogs(page = 1) {
  if (!selectedSite || !state) return;
  const request = ++logsRequest;
  const params = logParams();
  $('#export-logs').href = sitePath(`/api/logs.csv?${params}`, selectedSite);
  params.set('page', String(page));
  $('#logs-summary').textContent = '正在加载访问记录…';
  $('#logs-prev').disabled = true; $('#logs-next').disabled = true;
  try {
    const data = await api(`/api/logs?${params}`);
    if (request !== logsRequest) return;
    logPage = data.page; logPages = Math.max(1, data.pages);
    const root = $('#logs-body'); root.replaceChildren();
    if (!data.items.length) {
      const row = element('tr'); const cell = element('td', 'empty-state', '当前条件下暂无访问记录。打开测试站点后，再来查看。'); cell.colSpan = 7; row.append(cell); root.append(row);
    }
    for (const item of data.items) {
      const row = element('tr');
      row.append(element('td', '', formatDate(item.created)));
      const ip = element('td', '', item.ip); const country = element('small', 'country-name'); country.append(countryBadge(item.country)); ip.append(country); row.append(ip);
      const device = element('td', 'device-cell'); device.append(deviceBadge(item));
      device.append(element('small', '', visitDeviceLine(item.device_details)));
      device.title = '来自浏览器 User-Agent 声明，可能被精简或伪造；不能保证真实型号'; row.append(device);
      const slot = element('td'); slot.append(element('span', `badge ${item.slot === 'A' ? 'amber' : 'indigo'}`, item.slot)); row.append(slot);
      row.append(element('td', '', reasons[item.reason] || item.reason));
      const path = element('td', 'path-cell', logPath(item.path)); path.title = logPath(item.path); row.append(path);
      row.append(element('td', '', { PAGE: '页面', LINK: '链接', RULES: '规则', FORCE_A: '强制 A', FORCE_B: '强制 B' }[item.mode] || item.mode)); root.append(row);
    }
    $('#logs-summary').textContent = `共 ${number(data.total)} 条 · 每页 25 条`;
    $('#logs-page').textContent = `${logPage} / ${logPages}`;
  } catch (error) {
    if (request === logsRequest) $('#logs-summary').textContent = '加载失败；请点击「查询」重试。';
    throw error;
  } finally {
    if (request === logsRequest) { $('#logs-prev').disabled = logPage <= 1; $('#logs-next').disabled = logPage >= logPages; }
  }
}

async function selectTab(name, {load = true} = {}) {
  if (!labels[name]) name = 'overview';
  if (name === 'accounts' && !isAdmin(account)) name = 'domains';
  if (!selectedSite && !['domains','accounts'].includes(name)) name = 'domains';
  activeTab = name;
  document.body.dataset.tab = name;
  $$('.tab-panel').forEach(panel => { panel.hidden = panel.id !== `panel-${name}`; });
  $$('[data-tab]').forEach(button => {
    const active = button.dataset.tab === name; button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  $('#breadcrumb-current').textContent = labels[name][0];
  $('#page-eyebrow').textContent = {overview:'OVERVIEW',domains:'DOMAINS',content:'CONTENT',rules:'ACCESS RULES',simulate:'SIMULATION',logs:'VISIT LOGS',accounts:'ACCOUNTS'}[name];
  $('#page-title').textContent = labels[name][1]; $('#page-subtitle').textContent = labels[name][2];
  if (name !== 'content') closePreview();
  await bRedirects.setActive(name === 'content');
  if (name === 'logs') await loadLogs();
  if (name === 'domains' && load) await loadDomains();
  if (name === 'accounts') await loadAccounts();
  if (name === 'overview' && state) dashboard.refresh();
}

$$('[data-tab]').forEach(button => on(button, 'click', () => selectTab(button.dataset.tab)));
$$('[data-go]').forEach(button => on(button, 'click', () => selectTab(button.dataset.go)));
on($('.brand'), 'click', event => { event.preventDefault(); return selectTab('overview'); });
on($('#refresh-state'), 'click', event => busy(event.currentTarget, async () => {
  await loadDomains();
  await refreshState();
  if (activeTab === 'logs') await loadLogs(logPage);
  if ($('#audit-details').open) await loadAudit();
}));
$$('[data-routing]').forEach(button => on(button, 'click', () => saveConfig(patchConfig(state.config, { routing: button.dataset.routing }))));
$$('[data-allowed-slot]').forEach(button => on(button, 'click', () => saveConfig(patchConfig(state.config, { routing: 'RULES', allowed_slot: button.dataset.allowedSlot }))));
on($('#protection-toggle'), 'click', () => saveConfig(patchConfig(state.config, { protection: !state.config.protection })));
on($('#content-mode'), 'change', event => saveConfig(patchConfig(state.config, { content_mode: event.target.value })));
on($('#distribution'), 'change', event => saveConfig(patchConfig(state.config, { distribution: event.target.value })));
on(rulesForm, 'input', () => { editing++; setDirty(true); });
on(rulesForm, 'submit', async event => {
  event.preventDefault();
  if (!state || configBusy) return;
  Object.values(selections).forEach(selection => selection.flush());
  const values = Object.fromEntries(new FormData(rulesForm));
  for (const key of ['strict_bots', 'block_bots', 'block_pc', 'block_ipv4']) values[key] = rulesForm.elements.namedItem(key).checked;
  await busy(event.submitter, () => saveConfig(rulesConfig(state.config, values), true));
});
on($('#discard-rules'), 'click', async () => {
  if (dirty && !await confirmAction('放弃未保存的规则修改，恢复最近同步的配置？')) return;
  editing++; fillRules();
});
on($('#links-form'), 'submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  await busy(event.submitter, async () => {
    const original = form.elements.urls.value;
    const urls = parseLinks(original);
    await api('/api/links', { method: 'POST', body: { slot: form.elements.slot.value, urls } });
    if (form.elements.urls.value === original) form.elements.urls.value = '';
    toast(`已添加 ${urls.length} 个链接。`); await refreshState();
  });
});
on($('#close-preview'), 'click', closePreview);
on($('#reset-counters'), 'click', event => busy(event.currentTarget, async () => {
  if (!await confirmAction('确认重置访问计数？此操作无法撤销；日志与链接分配计数保持不变。')) return;
  await api('/api/counters/reset', { method: 'POST', body: {} });
  toast('访问计数已重置。'); await refreshState();
}));
on($('#audit-details'), 'toggle', () => { if ($('#audit-details').open) return loadAudit(); });
on($('#refresh-audit'), 'click', event => busy(event.currentTarget, loadAudit));
on($('#simulate-form'), 'submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  await busy(event.submitter, async () => {
    const data = Object.fromEntries(new FormData(form));
    data.ip = data.ip.trim(); data.country = data.country.trim().toUpperCase() || null; data.visits = Number(data.visits);
    const result = await api('/api/simulate', { method: 'POST', body: data });
    const banner = element('div', 'result-banner');
    banner.append(element('h3', '', `展示 ${result.slot} 内容`), element('p', '', reasons[result.reason] || result.reason), element('p', '', `${result.device_details?.device || result.device} · ${countryLabel(result.country)} · 第 ${result.count} 次访问`));
    if (result.device_details) banner.append(element('small', '', `${result.device_details.os} · ${result.device_details.browser}（UA 声明）`));
    const list = element('ol', 'trace-list');
    for (const step of result.trace) {
      const row = element('li', 'trace-row');
      row.append(element('span', `badge ${{ pass: 'green', block: 'red', skip: 'neutral' }[step.status] || 'neutral'}`, { pass: '通过', block: '拦截', skip: '跳过' }[step.status] || step.status));
      const text = element('div'); text.append(element('strong', '', traceLabels[step.rule] || reasons[step.rule] || step.rule), element('small', '', step.detail)); row.append(text); list.append(row);
    }
    $('#simulation-output').replaceChildren(banner, list);
  });
});
on($('#log-filters'), 'submit', event => { event.preventDefault(); return loadLogs(); });
on($('#log-filters'), 'change', () => loadLogs());
on($('#logs-prev'), 'click', () => loadLogs(logPage - 1));
on($('#logs-next'), 'click', () => loadLogs(logPage + 1));
window.addEventListener('beforeunload', event => { if (dirty || sourceEditor.isDirty() || bRedirects.isDirty()) { event.preventDefault(); event.returnValue = ''; } });

const stages = { legacy: '原有站点（保留）', unconfigured: '接入服务未配置', waiting_dns: '等待 DNS 解析', dns_verified: '解析已指向本机，等待建站', creating: '正在创建站点', proxy: '配置入口', certificate: '正在申请证书', verifying: '验收中', active: '已接入', failed: '失败待处理', paused: '已暂停', unsupported: '面板接口待验证' };

async function loadAccounts() {
  if (!isAdmin(account)) return;
  const {items} = await api('/api/accounts');
  const table = element('table','account-table'), head = element('thead'), headings = element('tr'), body = element('tbody');
  for (const label of ['用户名','角色','域名数','状态','操作']) headings.append(element('th','',label));
  head.append(headings); table.append(head,body);
  for (const item of items) {
    const row = element('tr'), status = element('td'), actions = element('td','account-actions');
    const count = item.domain_count ?? catalog?.sites.filter(site=>site.owner_id === item.id).length ?? '—';
    status.append(element('span',`badge ${item.enabled ? 'green' : 'neutral'}`,item.enabled ? '已启用' : '已停用'));
    if (item.role === 'agent') {
      actions.append(actionButton(item.enabled ? '停用' : '启用', async () => {
        if (!await confirmAction(`${item.enabled ? '停用' : '启用'}代理账号 ${item.username}？${item.enabled ? '该代理的现有会话会立即退出，域名与数据继续保留。' : '代理可再次登录并管理自己的域名。'}`)) return;
        await api(`/api/accounts/${encodeURIComponent(item.id)}`,{method:'PATCH',body:{enabled:!item.enabled}});
        await loadAccounts(); toast(item.enabled ? '代理账号已停用。' : '代理账号已启用。');
      },'button secondary small'),actionButton('重设密码',()=>resetAccountPassword(item),'text-button small'));
    } else actions.append(element('span','subtle','总管理员'));
    row.append(element('td','',item.username),element('td','',item.role === 'admin' ? '总管理员' : '代理'),element('td','',count),status,actions); body.append(row);
  }
  $('#account-list').replaceChildren(table);
}

function resetAccountPassword(item) {
  return new Promise(resolve => {
    const dialog = element('dialog','confirm-dialog account-dialog'), heading = element('h2','','重设代理密码');
    heading.id='password-dialog-title'; dialog.setAttribute('aria-labelledby',heading.id);
    const form = element('form'), label = element('label','','新密码'), input = element('input');
    input.type='password'; input.name='password'; input.required=true; input.minLength=12; input.maxLength=256; input.autocomplete='new-password'; label.append(input);
    const actions=element('div','actions'), cancel=element('button','button secondary','取消'), save=element('button','button primary','重设密码');
    cancel.type='button'; save.type='submit';
    const finish=()=>{input.value=''; dialog.close(); dialog.remove(); resolve();};
    cancel.addEventListener('click',finish); dialog.addEventListener('cancel',event=>{event.preventDefault();finish();});
    on(form,'submit',async event=>{
      event.preventDefault();
      await busy(save,async()=>{
        await api(`/api/accounts/${encodeURIComponent(item.id)}/password`,{method:'POST',body:{password:input.value}});
        finish(); toast('密码已重设，该代理的现有会话已退出。');
      });
    });
    actions.append(cancel,save); form.append(label,element('p','subtle','12–256 位。成功后请将新密码交给代理。'),actions);
    dialog.append(heading,element('p','muted',item.username),form); document.body.append(dialog); dialog.showModal(); input.focus();
  });
}

async function assignOwner(site) {
  if (!isAdmin(account)) return;
  const {items} = await api('/api/accounts');
  return new Promise(resolve=>{
    const dialog=element('dialog','confirm-dialog account-dialog'), heading=element('h2','','分配域名归属');
    heading.id='owner-dialog-title'; dialog.setAttribute('aria-labelledby',heading.id);
    const form=element('form'), label=element('label','','归属账号'), select=element('select');
    select.name='owner_id'; select.required=true;
    for (const item of items) {const option=element('option','',`${item.username} · ${item.role === 'admin' ? '总管理员' : '代理'}${item.enabled ? '' : '（已停用）'}`); option.value=item.id; select.append(option);}
    select.value=site.owner_id; label.append(select);
    const actions=element('div','actions'), cancel=element('button','button secondary','取消'), save=element('button','button primary','保存归属');
    cancel.type='button'; save.type='submit';
    const finish=()=>{dialog.close();dialog.remove();resolve();};
    cancel.addEventListener('click',finish); dialog.addEventListener('cancel',event=>{event.preventDefault();finish();});
    on(form,'submit',async event=>{
      event.preventDefault();
      await busy(save,async()=>{
        await api(`/api/sites/${site.id}/owner`,{method:'PUT',body:{owner_id:select.value}});
        finish();
        if (site.id === selectedSite) clearCurrentSite(selectedSite);
        await loadDomains();
        if (site.id === selectedSite) await refreshState();
        toast('域名归属已更新，新归属立即生效。');
      });
    });
    actions.append(cancel,save); form.append(label,element('p','subtle','转移后原代理无法继续访问此域名及相关数据；页面、规则和记录继续保留。'),actions);
    dialog.append(heading,element('p','muted',site.domain || '原有站点'),form); document.body.append(dialog); dialog.showModal(); select.focus();
  });
}

if ($('#account-form')) {
  on($('#refresh-accounts'),'click',loadAccounts);
  on($('#account-form'),'submit',async event=>{
    event.preventDefault(); if (!isAdmin(account)) return;
    const form=event.currentTarget;
    await busy(event.submitter,async()=>{
      await api('/api/accounts',{method:'POST',body:{username:form.elements.username.value.trim(),password:form.elements.password.value}});
      form.reset(); form.elements.password.value=''; await loadAccounts(); toast('代理账号已创建。');
    });
  });
}

function editSiteNote(site) {
  return new Promise(resolve => {
    const dialog = element('dialog', 'confirm-dialog note-dialog');
    const heading = element('h2', '', '编辑备注'); heading.id = 'note-dialog-title';
    dialog.setAttribute('aria-labelledby', heading.id);
    const form = element('form'), label = element('label', '', '备注');
    const input = element('textarea'); input.name = 'note'; input.rows = 3; input.maxLength = 200; input.value = site.note || '';
    input.placeholder = '例如：韩国手机流量 / 第 2 组测试';
    label.append(input);
    const actions = element('div', 'actions');
    const cancel = element('button', 'button secondary', '取消'); cancel.type = 'button';
    const save = element('button', 'button primary', '保存备注'); save.type = 'submit';
    const finish = () => { dialog.close(); dialog.remove(); resolve(); };
    cancel.addEventListener('click', finish);
    dialog.addEventListener('cancel', event => { event.preventDefault(); finish(); });
    on(form, 'submit', async event => {
      event.preventDefault();
      await busy(save, async () => {
        await api(`/api/sites/${site.id}/metadata`, {method:'PATCH',body:{note:input.value}});
        finish(); await loadDomains(); toast('备注已保存。');
      });
    });
    actions.append(cancel, save); form.append(label, element('p', 'subtle', '最多 200 字，仅管理后台可见。'), actions);
    dialog.append(heading, element('p', 'muted', site.domain || '原有本地站点'), form);
    document.body.append(dialog); dialog.showModal(); input.focus();
  });
}

async function showSiteEvents(site) {
  const request = ++siteEventsRequest;
  siteEventsSite = site.id;
  const data = await api(`/api/sites/${site.id}/provision/events`);
  if (request !== siteEventsRequest) return;
  $('#provision-events-title').textContent = `${site.domain || '原有站点'} · 操作日志`;
  $('#provision-events').replaceChildren(...(data.items.length
    ? data.items.map(item => element('p', 'subtle', `${formatDate(item.created)} · ${stages[item.stage] || item.stage} · ${item.detail}`))
    : [element('p', 'subtle', '暂无操作记录。')]));
  $('#provision-events-panel').hidden = false;
  $('#provision-events-panel').scrollIntoView({behavior:'smooth',block:'nearest'});
}

function renderReputation(site, cell) {
  const reputation = site.google_reputation;
  const display = reputationChecking.has(site.id) ? {label:'检测中…',tone:'neutral'} : reputationView(reputation);
  const badge = element('span',`domain-health badge ${display.tone}`,display.label);
  badge.title = reputation?.detail || '尚无谷歌检测结果';
  cell.replaceChildren(badge);
  if (display.tone === 'red') cell.append(element('small','domain-detail',(reputation.threats || []).map(kind=>({MALWARE:'恶意软件',SOCIAL_ENGINEERING:'钓鱼欺诈',UNWANTED_SOFTWARE:'不受欢迎的软件'})[kind] || kind).join(' · ')));
  if (reputation?.checked_at) cell.append(element('small','domain-detail',formatDate(reputation.checked_at)));
  else cell.append(element('small','domain-detail',reputation?.status === 'unconfigured' ? isAdmin(account) ? '等待 API 配置' : '检测未启用 · 联系管理员' : reputationChecking.has(site.id) ? '正在查询谷歌' : '自动检测'));
}

function renderDomains() {
  if (!catalog) return;
  const all = catalog.sites;
  const online = all.filter(site => site.enabled && ['active','legacy'].includes(site.stage)).length;
  const offline = all.filter(site => !site.enabled).length;
  $('#domains-summary').replaceChildren(...[
    [isAdmin(account) ? '全部域名' : '我的域名',all.length],['已上线',online],['待接入',all.length-online-offline],['已下线',offline],
  ].map(([label,count]) => {const item = element('span'); item.append(element('strong','',count),document.createTextNode(` ${label}`)); return item;}));
  const query = $('#domain-search').value.trim().toLowerCase();
  const sites = all.filter(site => `${site.domain} ${site.note || ''}`.toLowerCase().includes(query));
  const table = element('table','domain-table');
  const head = element('thead'), headings = element('tr'), body = element('tbody');
  for (const name of ['域名','谷歌安全检测','接入状态','添加日期','备注','状态','操作']) headings.append(element('th','',name));
  head.append(headings); table.append(head,body);
  for (const site of sites) {
    const row = element('tr'), name = element('td'), health = element('td'), validation = element('td'), created = element('td'), note = element('td'), availability = element('td'), actions = element('td');
    name.append(element('strong','domain-name',site.domain || '原有本地站点'), element('small','domain-detail',site.id === 'default' ? '原有站点' : '自带域名'));
    if (site.cf_status) {
      const checking = cloudflareChecking.has(site.id);
      const [label, tone] = checking ? ['检查中', 'neutral'] : {active:['有效','green'], pending:['待处理','neutral'], failed:['未完成','red']}[site.cf_status] || ['Cloudflare','neutral'];
      const badge = element('span', `domain-health badge ${tone}`, `Cloudflare ${label}`);
      badge.dataset.cfSite = site.id;
      name.append(badge);
    }
    if (isAdmin(account)) {
      const owner = element('div','domain-owner');
      owner.append(element('small','domain-detail',`归属：${site.owner_username || '总管理员'}`),actionButton('分配',()=>assignOwner(site),'text-button small'));
      name.append(owner);
    }
    health.dataset.reputationSite = site.id;
    renderReputation(site,health);
    const healthText = site.stage === 'active' ? '已验收' : ['failed','unsupported'].includes(site.stage) ? '需处理' : site.stage === 'legacy' ? '待核验' : '接入中';
    const healthColor = site.stage === 'active' ? 'green' : ['failed','unsupported'].includes(site.stage) ? 'red' : 'neutral';
    validation.append(element('span',`domain-health badge ${healthColor}`,healthText));
    const detail = element('small','domain-detail',stages[site.stage] || site.stage);
    detail.title = (site.error || '基于已有接入记录，非实时探测') + (site.enabled && site.next_attempt && site.stage!=='active' ? `；下次检查：${formatDate(site.next_attempt)}` : ''); validation.append(detail);
    if (site.error && site.stage !== 'active') { const error = element('small',`domain-detail${['failed','unsupported'].includes(site.stage)?' domain-error':''}`,site.error.length>52 ? site.error.slice(0,52)+'…' : site.error); error.title=site.error; validation.append(error); }
    const dnsPassed = ['active','dns_verified','creating','proxy','certificate','verifying'].includes(site.stage);
    validation.append(element('small','domain-detail',`${dnsPassed ? 'DNS 已验证' : 'DNS 未确认'} · ${site.stage === 'active' ? '证书已验收' : '证书未确认'}`));
    if (site.cf_nameservers && site.cf_status === 'pending') validation.append(element('small','domain-detail',`NS：${site.cf_nameservers.split(',').join('、')} · 本页会自动检查，生效后变为「有效」`));
    if ((site.cf_zone_id || site.cf_status) && site.id !== 'default' && !['active','legacy'].includes(site.stage)) validation.append(element('small','domain-detail','要走宝塔自动建站：点「关闭 Cloudflare」，再把 A 记录指到本机。'));
    if (catalog.cloudflare_configured && site.stage === 'active' && !site.cf_status && site.id !== 'default') validation.append(element('small','domain-detail','证书已接入。确认网站能打开后，点「套用 Cloudflare」，再把注册商 NS 改成页面给出的两条。'));
    const date = site.created ? new Date(site.created*1000) : null;
    created.append(element('span','',date ? date.toLocaleDateString('zh-CN') : '—'));
    created.title = date ? formatDate(site.created) : '登记时间未知';
    const memo = element('span','domain-note',site.note || '—'); memo.title = site.note || '暂无备注';
    note.append(memo,actionButton('编辑',()=>editSiteNote(site),'text-button small'));
    const live = site.enabled && ['active','legacy'].includes(site.stage);
    availability.append(element('span',`domain-status ${!site.enabled?'offline':live?'online':'pending'}`,!site.enabled?'已下线':live?'已上线':'待接入'));
    actions.className='domain-actions';
    actions.append(actionButton(site.id===selectedSite?'当前站点':'管理',()=>switchSite(site.id),'button secondary small'));
    actions.append(actionButton(site.enabled?'下线':'上线',async()=>{
      const action = site.enabled ? 'offline' : 'online';
      if (site.enabled && !await confirmAction(`下线 ${site.domain || '原有站点'}？访客将无法继续访问；页面、规则和历史数据都会保留。`)) return;
      await api(`/api/sites/${site.id}/availability/${action}`,{method:'POST',body:{}});
      await loadDomains();
      if (site.id === selectedSite) await refreshState();
      toast(site.enabled ? '域名已下线。' : ['active','legacy'].includes(site.stage) ? '域名已上线。' : '已启用域名并重试接入，DNS 与证书仍待验收。');
    },`button ${site.enabled?'secondary':'primary'} small`));
    actions.append(actionButton('日志',()=>showSiteEvents(site),'text-button small'));
    if (site.id !== 'default' && site.enabled && !['active','legacy'].includes(site.stage)) actions.append(actionButton('重试',async()=>{
      await api(`/api/sites/${site.id}/provision/retry`,{method:'POST',body:{}}); await loadDomains();
    },'text-button small'));
    if (site.cf_zone_id) actions.append(actionButton('清除 CF 缓存',async()=>{
      await api(`/api/sites/${site.id}/cloudflare/purge`,{method:'POST',body:{}});
      toast(`已清除 ${site.domain} 的 Cloudflare 缓存。`);
    },'button secondary small'));
    if ((site.cf_zone_id || site.cf_status) && site.id !== 'default') actions.append(actionButton('关闭 Cloudflare',async()=>{
      const ip = catalog.server_ip || '本机公网 IP';
      const message = ['active','legacy'].includes(site.stage)
        ? `关闭 ${site.domain} 的 Cloudflare 橙色云？访客会直接走到现在的 A 记录，本机站点和证书保持不变。`
        : `关闭 ${site.domain} 的 Cloudflare 橙色云？关掉后请把 A 记录指到 ${ip}。注册商 NS 如果仍是 Cloudflare 的两条，改 Cloudflare 里的 A 记录；改回注册商 NS 后，再在注册商把 A 记录指到 ${ip}。指向本机后会自动在宝塔建站并申请证书。`;
      if (!await confirmAction(message)) return;
      const result = await api(`/api/sites/${site.id}/cloudflare/disable`,{method:'POST',body:{},timeout:60000});
      await loadDomains();
      const address = result.record_address || '';
      let note = result.cloudflare?.detail || '已关闭 Cloudflare 代理。';
      if (address && ip && address === catalog.server_ip) note += ` Cloudflare 里的 A 记录已经是 ${address}，橙色云关掉后公共解析会指向本机。`;
      else if (address && catalog.server_ip) note += ` Cloudflare 里的 A 记录仍是 ${address}，请改成 ${catalog.server_ip}。`;
      toast(note);
    },'button secondary small'));
    if (catalog.cloudflare_configured && site.stage === 'active' && !site.cf_status && site.id !== 'default') actions.append(actionButton('套用 Cloudflare',async()=>{
      const result = await api(`/api/sites/${site.id}/cloudflare`,{method:'POST',body:{},timeout:60000});
      await loadDomains();
      const servers = (result.cloudflare?.nameservers || []).join('、');
      toast(result.cloudflare?.ok ? (servers ? `已套用 Cloudflare。请把注册商 NS 改为：${servers}` : '已套用 Cloudflare。请按提示修改 NS。') : (result.cloudflare?.detail || 'Cloudflare 未完成'), !result.cloudflare?.ok);
    },'button primary small'));
    if (site.cf_status === 'failed') actions.append(actionButton('重试 CF',async()=>{
      const result = await api(`/api/sites/${site.id}/cloudflare`,{method:'POST',body:{},timeout:60000});
      await loadDomains();
      toast(result.cloudflare?.ok ? '已重新套用 Cloudflare 规则。' : (result.cloudflare?.detail || 'Cloudflare 未完成'), !result.cloudflare?.ok);
    },'text-button small'));
    row.append(name,health,validation,created,note,availability,actions); body.append(row);
  }
  if (!sites.length) { const row=element('tr'), cell=element('td','empty-state',all.length ? '没有匹配的域名或备注' : '暂无域名，点击「添加域名」开始接入。'); cell.colSpan=7; row.append(cell); body.append(row); }
  $('#domain-list').replaceChildren(table);
}

async function loadDomains({refreshSelection = true} = {}) {
  const request = ++domainsRequest;
  const result = await api('/api/sites');
  if (request !== domainsRequest) return;
  if (result.google_reputation_configured) for (const site of result.sites) {
    const recent = latestReputation.get(site.id);
    if (recent?.checked_at > (site.google_reputation?.checked_at || 0)) site.google_reputation = recent;
  }
  catalog = result;
  account = result.account;
  const next = chooseSite(selectedSite, catalog.sites);
  const changed = next !== selectedSite;
  if (changed || !next) clearCurrentSite(next);
  $('#no-sites').hidden = Boolean(next);
  if (!next) { $('#connection').textContent='服务已连接'; $('#connection').className='badge green'; }
  $('#refresh-state').disabled = !next;
  $$('[data-tab], [data-go]').forEach(button => {
    const tab = button.dataset.tab || button.dataset.go;
    button.disabled = (!next && !['domains','accounts'].includes(tab)) || (tab === 'accounts' && !isAdmin(account));
  });
  $('#site-selector-label').textContent = isAdmin(account) ? '当前管理站点' : '我的域名';
  $('#analytics-scope').querySelector('[value="all"]').textContent = isAdmin(account) ? '所有域名' : '我的域名';
  $('#google-reputation-notice').textContent = catalog.google_reputation_configured
    ? '自动检测已开启 · 本页停留时检查未检测或已过期的域名，结果直接显示在下方。'
    : isAdmin(account) ? '自动检测待启用：请先配置 Google Web Risk API 密钥，完成后本页自动显示结果。' : '检测未启用，请联系管理员。';
  const selector = $('#site-selector'); selector.replaceChildren();
  for (const site of catalog.sites) {
    const option = element('option', '', site.domain || '原有本地站点'); option.value = site.id; selector.append(option);
  }
  if (!next) { const option = element('option','','暂无域名'); option.value=''; selector.append(option); }
  selector.value = selectedSite;
  syncCopyDomain();
  const cloudflareReady = Boolean(catalog.cloudflare_configured);
  $('#cf-choice').hidden = !cloudflareReady;
  $('#cf-choice-note').hidden = !cloudflareReady;
  if (!cloudflareReady) $('#use-cloudflare').checked = false;
  const templateName = catalog.cloudflare_template ? `「${catalog.cloudflare_template}」上的` : '已配置的';
  $('#cf-choice-note').textContent = cloudflareReady ? `建议先不勾选：A 记录指向本机后会自动建站并申请证书，确认能打开后再套用${templateName}规则。勾选则马上套用规则，NS 改到 Cloudflare。` : '';
  syncDnsInstructions();
  renderDomains();
  const visible = new Set(catalog.sites.map(site=>site.id));
  if (siteEventsSite && !visible.has(siteEventsSite)) {
    ++siteEventsRequest; siteEventsSite=null;
    $('#provision-events').replaceChildren(); $('#provision-events-panel').hidden=true;
  }
  for (const id of latestReputation.keys()) if (!visible.has(id)) latestReputation.delete(id);
  for (const id of reputationChecking) if (!visible.has(id)) reputationChecking.delete(id);
  for (const id of cloudflareChecking) if (!visible.has(id)) cloudflareChecking.delete(id);
  reputationAutoCheck.refresh(catalog).catch(()=>{});
  cloudflareAutoCheck.refresh(catalog).catch(()=>{});
  updateSiteLock();
  if (!next && !['domains','accounts'].includes(activeTab)) await selectTab('domains',{load:false});
  if (changed && next && refreshSelection) {
    await refreshState();
    if (activeTab === 'logs') await loadLogs();
  }
}

function clearCurrentSite(next) {
  sourceEditor.clear();
  bRedirects.clear();
  selectedSite = next;
  ++stateRequest; ++logsRequest; ++siteEventsRequest; closePreview(); dashboard.invalidate();
  siteEventsSite=null;
  state = null; setDirty(false); editing++;
  rulesForm.reset(); Object.values(selections).forEach(selection=>selection.set([]));
  $('#links-form').reset(); $('#simulate-form').reset(); $('#log-filters').reset();
  for (const id of ['rules-fields','links-fields','simulate-fields','reset-counters']) $(`#${id}`).disabled=true;
  for (const id of ['slot-cards','links-list','audit-list','logs-body','provision-events']) $(`#${id}`).replaceChildren();
  $('#audit-details').open=false; $('#provision-events-panel').hidden=true;
  $('#export-logs').removeAttribute('href'); $('#open-target').removeAttribute('href');
  $('#open-target').hidden=true;
  $('#link-count').textContent='0 个链接'; $('#logs-summary').textContent=''; $('#logs-page').textContent='—';
  $('#logs-prev').disabled=true; $('#logs-next').disabled=true;
  $('#simulation-output').replaceChildren(element('p','empty-state','请为当前站点重新运行模拟。'));
  $('#analytics-retention').textContent=''; $('#geoip-warning').hidden=true; $('#load-error').hidden=true;
  for (const id of ['geoip-status','current-mode','published-count','last-sync','allowed-slot-badge','allowed-content-status','overview-allowed-status']) $(`#${id}`).textContent='—';
  $('#hero-mode').textContent=next ? '正在读取当前站点…' : '暂无域名';
  $('#hero-title').textContent=next ? '正在切换站点' : '添加你的第一个域名';
  $('#hero-description').textContent=''; $('#sidebar-theme').textContent=next ? '主题加载中' : '等待添加域名';
  $('#site-context').textContent=next ? '正在加载独立配置…' : '添加域名后即可管理内容与访问规则。';
  $$('.tab-panel').forEach(panel=>{panel.inert=!['panel-domains','panel-accounts'].includes(panel.id);});
  updateConfigControls();
}

function currentDomain() {
  const selector = $('#site-selector');
  return catalog?.sites?.find(site => site.id === selector.value)?.domain || '';
}

function syncCopyDomain() {
  const button = $('#copy-domain');
  const domain = currentDomain();
  const fallback = $('#site-selector').selectedOptions[0]?.textContent || '暂无域名';
  button.textContent = domain || fallback;
  button.disabled = !domain;
  button.title = domain ? `点击复制 ${domain}` : '当前站点没有可复制的域名';
  button.setAttribute('aria-label', domain ? `复制域名 ${domain}` : '当前站点没有可复制的域名');
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(value); return; }
    catch { /* Fall through when the page is not a secure context. */ }
  }
  const area = element('textarea');
  area.value = value; area.setAttribute('readonly', '');
  area.style.position = 'fixed'; area.style.left = '-9999px';
  document.body.append(area); area.select();
  const copied = document.execCommand('copy');
  area.remove();
  if (!copied) throw new Error('复制失败，请手动选择域名。');
}

async function switchSite(next) {
  if (next === selectedSite) return;
  if (!catalog?.sites.some(site=>site.id === next)) return;
  if (configBusy || pendingActions > 1 || siteLoading) { $('#site-selector').value = selectedSite; syncCopyDomain(); return; }
  const hasDraft = dirty || sourceEditor.isDirty() || bRedirects.isDirty() || $('#links-form').elements.urls.value.trim() || $$('.upload-form input[type=file]').some(input => input.files.length);
  if (hasDraft && !await confirmAction('切换站点将丢弃尚未保存的规则、源码、链接草稿和选中的上传文件，是否继续？')) { $('#site-selector').value = selectedSite; syncCopyDomain(); return; }
  siteLoading = true; updateSiteLock();
  clearCurrentSite(next); $('#site-selector').value = next; syncCopyDomain();
  try {
    await refreshState();
    if (activeTab === 'logs') await loadLogs();
    if ($('#audit-details').open) await loadAudit();
  } finally {
    siteLoading = false; updateSiteLock();
    $$('.tab-panel').forEach(panel => { panel.inert = !state && !['panel-domains','panel-accounts'].includes(panel.id); });
    if (!state && selectedSite) { $('#open-target').removeAttribute('href'); $('#site-context').textContent = '当前站点加载失败，请刷新状态或切换其他站点。'; }
  }
}

on($('#site-selector'), 'change', event => switchSite(event.target.value));
on($('#copy-domain'), 'click', async () => {
  const domain = currentDomain();
  if (!domain) throw new Error('当前站点没有可复制的域名。');
  await copyText(domain);
  toast(`已复制 ${domain}`);
});
on($('#refresh-domains'), 'click', loadDomains);
on($('#domain-search'), 'input', renderDomains);
function syncDnsInstructions() {
  const box = $('#use-cloudflare');
  if (box && !box.closest('#cf-choice').hidden && box.checked) {
    $('#dns-instructions').textContent = '添加后会在 Cloudflare 建这个域名，并套用现有优化规则。请把注册商 NS 改成页面给出的两条。';
    return;
  }
  $('#dns-instructions').textContent = catalog?.server_ip
    ? `先把 A 记录指到 ${catalog.server_ip}，再点建立域名。程序会自动建站并申请证书。网站能打开后，再点「套用 Cloudflare」。`
    : '服务器公网 IP 尚未配置；现在可以登记域名和测试独立配置，但不能自动建站或申请证书。';
}

on($('#new-domain'), 'click', () => { $('#domain-add-panel').hidden=false; $('#domain-add-panel').scrollIntoView({behavior:'smooth',block:'nearest'}); $('#domain-form').elements.domain.focus(); });
on($('#empty-add-domain'), 'click', async () => { await selectTab('domains'); $('#new-domain').click(); });
on($('#cancel-domain-add'), 'click', () => { $('#domain-add-panel').hidden=true; });
on($('#use-cloudflare'), 'change', syncDnsInstructions);
on($('#domain-form'), 'submit', async event => {
  event.preventDefault(); const form = event.currentTarget;
  const useCloudflare = !$('#cf-choice').hidden && $('#use-cloudflare').checked;
  await busy(event.submitter, async () => {
    const result = await api('/api/sites', { method: 'POST', body: useCloudflare ? { domain: form.elements.domain.value.trim(), cloudflare: true } : { domain: form.elements.domain.value.trim() }, timeout: 60000 });
    form.reset(); $('#cf-result').hidden = true;
    await loadDomains();
    if (result.cloudflare?.ok) {
      const servers = (result.cloudflare.nameservers || []).join('、');
      $('#cf-result').hidden = false;
      $('#cf-result').textContent = servers ? `${result.cloudflare.detail} NS：${servers}` : result.cloudflare.detail;
      toast('已套用 Cloudflare 规则。请按提示修改 NS。');
    } else if (result.cloudflare) {
      $('#cf-result').hidden = false;
      $('#cf-result').textContent = result.cloudflare.detail;
      toast(result.cloudflare.detail, true);
    } else toast('域名已登记。A 记录指向本机后，会自动建站并申请证书。');
  });
});

document.body.dataset.tab = 'overview';
renderSlots();
loadDomains({refreshSelection:false}).then(() => refreshState()).catch(error => toast(error.message, true)).finally(() => { siteLoading = false; updateSiteLock(); });
window.setInterval(() => { if (!document.hidden && !pendingActions) loadDomains().catch(() => {}); }, 15000);
document.addEventListener('visibilitychange',()=>{ if (!document.hidden) loadDomains().catch(()=>{}); });
