import { patchConfig, rulesConfig, parseLinks, previewURL, logPath, validateUpload, sitePath } from './helpers.mjs';
import { countryLabel } from './locale.mjs';
import { createSelection } from './selection.mjs';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const csrf = $('meta[name="csrf-token"]').content;
const target = $('meta[name="target-url"]').content;
let state = null;
let selectedSite = 'default';
let pendingActions = 0;
let siteLoading = true;
let catalog = null;
let dirty = false;
let editing = 0;
let configBusy = false;
let stateRequest = 0;
let logsRequest = 0;
let logPage = 1;
let logPages = 1;
let activeTab = 'overview';
const rulesForm = $('#rules-form');
const selections = Object.fromEntries(['countries','languages'].map(kind => [kind,createSelection(rulesForm.elements.namedItem(kind),kind)]));
const labels = {
  domains: ['域名管理', '每个域名，各自独立。', '登记域名、查看解析信息与接入进度，在同一后台管理各站点。'],
  overview: ['概览', '一切，尽在掌握。', '管理内容、配置规则，了解每一次访问的去向。'],
  content: ['A/B 内容', '两种内容，自由掌控。', '导入、预览与发布，每个版本都可随时回退。'],
  rules: ['访问规则', '让访问，按你的规则进行。', '用清晰的条件定义分流，让每次判断都有依据。'],
  simulate: ['规则模拟', '在上线之前，验证想法。', '模拟访问条件，逐条查看已保存规则的判断过程。'],
  logs: ['访问日志', '每一次访问，都有迹可循。', '查看真实文档请求，追踪分流结果与判断原因。'],
};
const reasons = { blacklist: '命中黑名单', whitelist: '命中白名单', bot_marker: '匹配机器人标记', ipv4: 'IPv4 限制', device: '设备限制', os_version: '系统版本限制', blocked_cidr: '命中屏蔽网段', country: '国家 / 地区限制', country_unknown: '国家未知', language: '语言限制', visit_limit: '超过访问次数', allowed: '规则通过', pass: '规则通过', force_a: '强制 A', force_b: '强制 B', protection_off: '防护已关闭' };
const formatDate = value => {
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleString('zh-CN', { hour12: false });
};
reasons.manual = '手动指定';
const traceLabels = { blacklist: '黑名单检查', whitelist: '白名单检查', bot_marker: 'UA 特征检查', ipv4: 'IPv4 检查', device: '设备检查', os_version: '系统版本检查', blocked_cidr: '自定义网段检查', country: '国家检查', country_unknown: '国家检查', language: '语言检查', visit_limit: '访问次数检查' };
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
  $('#site-selector').disabled = siteLoading || pendingActions > 0 || configBusy;
}

async function busy(button, action) {
  if (button?.disabled) return;
  if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
  try { return await action(); }
  finally { if (button) { button.disabled = false; button.removeAttribute('aria-busy'); } }
}

async function api(path, { method = 'GET', body, raw = false } = {}) {
  const siteAtStart = selectedSite;
  const scopedPath = sitePath(path, siteAtStart);
  const headers = {};
  if (method !== 'GET') headers['X-CSRF-Token'] = csrf;
  if (body !== undefined) headers['Content-Type'] = raw ? 'application/octet-stream' : 'application/json';
  let response;
  try {
    response = await fetch(scopedPath, { method, headers, credentials: 'same-origin', cache: 'no-store', body: body === undefined ? undefined : raw ? body : JSON.stringify(body), signal: AbortSignal.timeout(raw ? 120000 : 20000) });
  } catch (error) {
    throw new Error(error.name === 'TimeoutError' ? '请求超时。操作可能已完成，请刷新状态确认后再试。' : '无法连接服务，请检查服务状态后重试。');
  }
  let data;
  try { data = await response.json(); } catch { throw new Error('服务返回了无法读取的数据，请刷新后重试。'); }
  if (scopedPath !== path && selectedSite !== siteAtStart) {
    const error = new Error('站点已切换，忽略旧响应'); error.stale = true; throw error;
  }
  if (!response.ok) {
    if (response.status === 401) { window.location.assign('/login'); throw new Error('会话已过期，请重新登录。'); }
    const messages = { 400: '提交内容有误，请检查输入。', 403: '安全校验失败，请重新加载页面后重试。', 404: '内容已不存在，请刷新状态。', 409: '配置已被其他操作更新，已重新同步。你的规则草稿仍保留，请检查后再次保存。', 413: '文件过大，上传上限为 20 MiB。', 422: '输入未通过校验，请检查 IP、CIDR、网址或数值。', 429: '操作过于频繁，请稍后重试。' };
    const error = new Error(messages[response.status] || '服务暂时无法完成操作，请稍后重试。');
    error.status = response.status;
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

function updateConfigControls() {
  $('#routing-controls').disabled = !state || configBusy;
  $('#allowed-content-controls').disabled = !state || configBusy;
  $('#content-controls').disabled = !state || configBusy;
  $('#protection-toggle').disabled = !state || configBusy;
  if (!state) return;
  $$('[data-routing]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.routing === state.config.routing)));
  const slot = state.config.allowed_slot;
  const active = state.config.routing === 'RULES';
  $$('[data-allowed-slot]').forEach(button => {
    const selected = button.dataset.allowedSlot === slot;
    button.setAttribute('aria-pressed', String(selected));
    button.classList.toggle('primary', selected);
    button.classList.toggle('secondary', !selected);
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
  $('#site-context').textContent = `${state.site.domain || '原有本地站点'} · 当前页面的规则、内容、计数和日志仅属于这个站点`;
  for (const key of ['total', 'allowed', 'blocked', 'today']) $(`#stat-${key}`).textContent = number(state.stats[key]);
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
  renderLinks();
}

async function refreshState() {
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
      const title = element('div'); title.append(element('h2', '', `${slot} 内容`), element('small', '', slot === 'A' ? '拦截时展示；也可选为放行后的内容' : '放行后选择 B 时展示')); heading.append(title);
      const live = element('div', 'slot-live'); live.id = `slot-live-${slot}`;
      const form = element('form', 'upload-form');
      const label = element('label', '', '导入新版本');
      const input = element('input'); input.type = 'file'; input.accept = '.zip,.html,.htm'; input.required = true; input.name = 'file'; input.disabled = !state;
      label.append(input);
      const submit = element('button', 'button secondary small', '上传并创建版本'); submit.type = 'submit'; submit.disabled = !state;
      form.append(label, element('small', '', 'ZIP / HTML · 最大 20 MiB · 上传后需手动发布'), submit);
      on(form, 'submit', async event => {
        event.preventDefault();
        await busy(submit, async () => {
          const file = input.files[0]; validateUpload(file);
          input.disabled = true;
          try {
            await api(`/api/upload/${slot}?name=${encodeURIComponent(file.name)}`, { method: 'POST', raw: true, body: file });
            input.value = '';
            toast(`${slot} 版本已导入，尚未发布。`);
            await refreshState();
          } finally { input.disabled = false; }
        });
      });
      const versions = element('div', 'version-list'); versions.id = `versions-${slot}`;
      card.append(heading, live, form, versions); root.append(card);
    }
    if (state) card.querySelectorAll('.upload-form input, .upload-form button').forEach(control => { if (!control.hasAttribute('aria-busy')) control.disabled = false; });
    const current = state?.versions.find(version => version.id === state.slots[slot]);
    const live = $(`#slot-live-${slot}`);
    live.replaceChildren(element('small', '', '当前发布'), element('strong', '', current?.name || (state?.slots[slot] ? '已发布版本' : '尚未发布内容')));
    if (current) live.append(element('small', '', `${formatDate(current.created)} · ${number(current.files)} 个文件`));
    const versions = $(`#versions-${slot}`); versions.replaceChildren();
    const items = state?.versions.filter(version => version.slot === slot) || [];
    if (!items.length) versions.append(element('p', 'empty-state', state ? '还没有版本。导入一个站点，开始你的实验。' : '连接服务后，版本将显示在这里。'));
    for (const version of items) {
      const published = version.id === state.slots[slot];
      const row = element('div', 'version-row');
      const name = element('div', 'version-name', version.name);
      const meta = element('p', 'version-meta', `${formatDate(version.created)} · ${number(version.files)} 个文件 · ${bytes(version.bytes)}`);
      const actions = element('div', 'actions');
      actions.append(actionButton('预览', () => preview(version)));
      if (published) actions.append(element('span', 'badge green', '当前发布'));
      else actions.append(actionButton('发布此版本', async () => {
        if (!await confirmAction(`将「${version.name}」发布至 ${slot}？将替换当前版本；旧版本仍可回退。`)) return;
        await api(`/api/publish/${slot}`, { method: 'POST', body: { version_id: version.id } });
        toast(`${slot} 内容已发布。`); await refreshState();
      }, 'button primary small'));
      const hash = element('details', 'version-hash'); hash.append(element('summary', '', '版本校验值'), element('span', '', version.sha256));
      row.append(name, meta, actions, hash); versions.append(row);
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
  if (!state.links.length) root.append(element('p', 'empty-state', '暂无链接。添加后，切换到「链接跳转」模式即可使用。'));
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
  const data = await api('/api/audit');
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

async function loadLogs(page = 1) {
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
      const ip = element('td', '', item.ip); ip.append(element('small', 'country-name', countryLabel(item.country))); row.append(ip);
      const details = item.device_details;
      const device = element('td', 'device-cell', details?.device || ({ mobile: '移动设备', desktop: '桌面设备', unknown: '未知设备' }[item.device] || item.device));
      device.append(element('small', '', details ? `${details.os} · ${details.browser}` : '旧记录未采集设备详情'));
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

async function selectTab(name) {
  if (!labels[name]) name = 'overview';
  activeTab = name;
  $$('.tab-panel').forEach(panel => { panel.hidden = panel.id !== `panel-${name}`; });
  $$('[data-tab]').forEach(button => {
    const active = button.dataset.tab === name; button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  $('#breadcrumb-current').textContent = labels[name][0];
  $('#page-title').textContent = labels[name][1]; $('#page-subtitle').textContent = labels[name][2];
  if (name !== 'content') closePreview();
  if (name === 'logs') await loadLogs();
  if (name === 'domains') await loadDomains();
}

$$('[data-tab]').forEach(button => on(button, 'click', () => selectTab(button.dataset.tab)));
$$('[data-go]').forEach(button => on(button, 'click', () => selectTab(button.dataset.go)));
on($('.brand'), 'click', event => { event.preventDefault(); return selectTab('overview'); });
on($('#refresh-state'), 'click', event => busy(event.currentTarget, async () => {
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
  for (const key of ['block_bots', 'block_pc', 'block_ipv4']) values[key] = rulesForm.elements.namedItem(key).checked;
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
window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });

const stages = { legacy: '原有站点（保留）', unconfigured: '接入服务未配置', waiting_dns: '等待 DNS 解析', dns_verified: '解析已验证', creating: '正在创建站点', proxy: '配置入口', certificate: '配置证书', verifying: '验收中', active: '已接入', failed: '失败待处理', paused: '已暂停', unsupported: '面板接口待验证' };

async function loadDomains() {
  catalog = await api('/api/sites');
  const selector = $('#site-selector'); selector.replaceChildren();
  for (const site of catalog.sites) {
    const option = element('option', '', site.domain || '原有本地站点'); option.value = site.id; selector.append(option);
  }
  selector.value = selectedSite;
  $('#dns-instructions').textContent = catalog.server_ip
    ? `解析类型：A ｜ 记录值：${catalog.server_ip} ｜ 主机记录：按你填写的完整域名，在域名服务商处选择对应子域名（根域名通常为 @）。`
    : '服务器公网 IP 尚未配置；现在可以登记域名和测试独立配置，但不能自动建站或申请证书。';
  const root = $('#domain-list'); root.replaceChildren();
  for (const site of catalog.sites) {
    const row = element('div', 'domain-row');
    const text = element('div', 'grow');
    text.append(element('h3', '', site.domain || '原有本地站点'), element('p', 'subtle', `${stages[site.stage] || site.stage} · ${site.enabled ? '启用' : '暂停'}${site.id === 'default' ? '' : ' · 证书：' + (site.stage === 'active' ? '验收通过' : '未确认')}`));
    if (site.error) text.append(element('p', 'domain-error', site.error));
    if (site.next_attempt && site.stage !== 'active') text.append(element('small', '', `下次检查：${formatDate(site.next_attempt)}`));
    const actions = element('div', 'actions');
    actions.append(actionButton(site.id === selectedSite ? '当前站点' : '管理此站', () => switchSite(site.id)));
    if (site.id !== 'default') {
      actions.append(actionButton('接入日志', async () => {
        const data = await api(`/api/sites/${site.id}/provision/events`);
        $('#provision-events-title').textContent = `${site.domain} · 接入日志`;
        $('#provision-events').replaceChildren(...data.items.map(item => element('p', 'subtle', `${formatDate(item.created)} · ${stages[item.stage] || item.stage} · ${item.detail}`)));
        $('#provision-events-panel').hidden = false;
      }));
      actions.append(actionButton('重试接入', async () => {
        await api(`/api/sites/${site.id}/provision/retry`, { method: 'POST', body: {} }); await loadDomains();
      }));
      if (site.enabled) actions.append(actionButton('暂停', async () => {
        if (!await confirmAction(`暂停 ${site.domain} 的接入和访问？不会删除任何页面或宝塔站点。`)) return;
        await api(`/api/sites/${site.id}/provision/pause`, { method: 'POST', body: {} }); await loadDomains();
      }));
    }
    row.append(text, actions); root.append(row);
  }
}

async function switchSite(next) {
  if (next === selectedSite) return;
  if (configBusy || pendingActions > 1 || siteLoading) { $('#site-selector').value = selectedSite; return; }
  const hasDraft = dirty || $('#links-form').elements.urls.value.trim() || $$('.upload-form input[type=file]').some(input => input.files.length);
  if (hasDraft && !await confirmAction('切换站点将丢弃尚未保存的规则、链接草稿和选中的上传文件，是否继续？')) { $('#site-selector').value = selectedSite; return; }
  siteLoading = true; updateSiteLock();
  $('#site-context').textContent = '正在切换站点并加载独立配置…';
  $('#open-target').removeAttribute('href');
  $$('.tab-panel').forEach(panel => { panel.inert = true; });
  selectedSite = next; $('#site-selector').value = next;
  ++stateRequest; ++logsRequest; closePreview();
  setDirty(false); editing++; state = null;
  $('#slot-cards').replaceChildren(); $('#links-form').reset();
  $('#simulation-output').replaceChildren(element('p', 'empty-state', '请为当前站点重新运行模拟。'));
  $('#audit-list').replaceChildren(); $('#logs-body').replaceChildren();
  $('#export-logs').href = sitePath(`/api/logs.csv?${logParams()}`, selectedSite);
  try {
    await refreshState();
    if (activeTab === 'logs') await loadLogs();
    if ($('#audit-details').open) await loadAudit();
  } finally {
    siteLoading = false; updateSiteLock();
    $$('.tab-panel').forEach(panel => { panel.inert = !state && panel.id !== 'panel-domains'; });
    if (!state) { $('#open-target').removeAttribute('href'); $('#site-context').textContent = '当前站点加载失败，请刷新状态或切换其他站点。'; }
  }
}

on($('#site-selector'), 'change', event => switchSite(event.target.value));
on($('#refresh-domains'), 'click', loadDomains);
on($('#domain-form'), 'submit', async event => {
  event.preventDefault(); const form = event.currentTarget;
  await busy(event.submitter, async () => {
    await api('/api/sites', { method: 'POST', body: { domain: form.elements.domain.value.trim() } });
    form.reset(); await loadDomains(); toast('域名已登记。请按页面提示设置 DNS 解析。');
  });
});

renderSlots();
Promise.all([loadDomains(), refreshState()]).catch(error => toast(error.message, true)).finally(() => { siteLoading = false; updateSiteLock(); });
window.setInterval(() => { if (activeTab === 'domains' && !document.hidden && !pendingActions) loadDomains().catch(() => {}); }, 15000);
