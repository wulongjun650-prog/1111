import {parseLinks} from './helpers.mjs';

export function createBRedirects({api, on, element, confirmAction, getSite, onApplied}) {
  const root = document.querySelector('#b-redirects');
  let session = null, active = false, queueGeneration = 0, checkQueue = Promise.resolve();
  const header = element('div', 'card-heading');
  const title = element('div'); title.append(element('p', 'eyebrow', 'B / QUICK REDIRECT'), element('h2', '', 'B 页快捷换链'));
  const count = element('span', 'badge neutral', '尚未扫描'); count.id = 'b-redirect-count';
  header.append(title, count);
  const current = element('p', 'b-redirect-current'); current.id = 'b-redirect-current';
  const description = element('p', 'muted', '自动查找 B 源码中的跳转地址。使用预设链接时，先检测，再替换勾选位置并发布新 B 版本；旧版本保留。');
  const toolbar = element('div', 'b-redirect-toolbar');
  const versionLabel = element('label', '', '扫描 B 版本');
  const version = element('select'); version.id = 'b-redirect-version'; versionLabel.append(version);
  const rescan = element('button', 'button secondary small', '重新扫描'); rescan.type = 'button';
  toolbar.append(versionLabel, rescan);
  const details = element('details', 'b-redirect-details'); details.open = true;
  const summary = element('summary', '', '跳转位置');
  const occurrences = element('div', 'b-redirect-occurrences'); occurrences.id = 'b-redirect-occurrences';
  details.append(summary, occurrences);
  const warnings = element('div', 'b-redirect-warnings');
  const form = element('form', 'b-redirect-add-form');
  const urlsLabel = element('label', 'grow', '预设链接池');
  const urls = element('textarea'); urls.id = 'b-redirect-urls'; urls.rows = 3; urls.required = true;
  urls.placeholder = '每行一个完整的 https:// 或 http:// 链接，最多 30 条'; urlsLabel.append(urls);
  const noteLabel = element('label', '', '备注（可选）');
  const note = element('input'); note.id = 'b-redirect-note'; note.maxLength = 300; noteLabel.append(note);
  const add = element('button', 'button secondary', '保存预设链接'); add.id = 'b-redirect-add'; add.type = 'submit';
  form.append(urlsLabel, noteLabel, add);
  const poolHint = element('small', '', '保存后依次自动检测；使用链接前会再次检测。检测结果仅代表本次观察。');
  const splitPanel = element('div', 'b-split'); splitPanel.id = 'b-redirect-split';
  const splitTitle = element('div'); splitTitle.append(element('strong', '', '分流器'), element('span', 'b-split-status', '已关闭'));
  const splitHelp = element('p', 'muted', '开启后，B 页里识别到的跳转位置会在页面发出前换成该访客的一条链接。同一个访客始终是同一条。关闭后恢复当前发布版本里的地址。点击后直接跳转，没有中转页。');
  const splitControls = element('div', 'b-split-controls');
  const splitToggle = element('button', 'button secondary small', '开启分流'); splitToggle.type = 'button'; splitToggle.id = 'b-split-toggle';
  const splitModeLabel = element('label', '', '分配方式');
  const splitMode = element('select'); splitMode.id = 'b-split-mode';
  splitMode.append(element('option', '', '随机'), element('option', '', '按概率'));
  splitMode.options[0].value = 'random'; splitMode.options[1].value = 'weighted';
  splitModeLabel.append(splitMode);
  const splitSave = element('button', 'button primary small', '保存分流设置'); splitSave.type = 'button'; splitSave.id = 'b-split-save';
  splitControls.append(splitToggle, splitModeLabel, splitSave);
  splitPanel.append(splitTitle, splitHelp, splitControls);
  const presets = element('div', 'b-redirect-presets'); presets.id = 'b-redirect-presets';
  const message = element('p', 'b-redirect-message'); message.id = 'b-redirect-message'; message.hidden = true;
  message.setAttribute('role', 'status');
  root.append(header, current, description, toolbar, details, warnings, form, poolHint, splitPanel, presets, message);

  const isCurrent = value => Boolean(value && session === value && value.siteId === getSite()?.id);
  const selected = value => value.occurrences.filter(item => !value.excluded.has(item.key));
  const activePreset = value => (value.active && value.active.version_id === value.published) ? value.active.preset_id : null;
  const isDirty = () => Boolean(urls.value.trim() || note.value.trim() || session?.selectionDirty || session?.splitDirty);
  const blankSplit = () => ({enabled:false, mode:'random', members:[]});
  const date = timestamp => timestamp ? new Date(Number(timestamp) * 1000).toLocaleString('zh-CN', {hour12:false}) : '尚未检测';
  const endpoint = value => `/api/b-redirects${value.versionId ? `?version_id=${encodeURIComponent(value.versionId)}` : ''}`;
  const kinds = {anchor:'页面链接', meta_refresh:'Meta 跳转', js_location:'JS 跳转', js_variable:'跳转变量'};

  function showMessage(text = '', error = false) {
    message.textContent = text; message.hidden = !text;
    message.classList.toggle('error', error); message.setAttribute('role', error ? 'alert' : 'status');
  }

  function updateControls() {
    const value = session, busy = !value || value.busy || value.loading;
    version.disabled = busy || !value.versions.length;
    rescan.disabled = busy;
    urls.disabled = !value || value.busy; note.disabled = urls.disabled; add.disabled = urls.disabled;
    splitToggle.disabled = !value || value.busy; splitMode.disabled = splitToggle.disabled; splitSave.disabled = splitToggle.disabled;
    presets.querySelectorAll('.b-split-join input').forEach(input => { input.disabled = splitToggle.disabled; });
    root.setAttribute('aria-busy', String(Boolean(value?.loading || value?.busy)));
    occurrences.querySelectorAll('input').forEach(input => {input.disabled = busy;});
    for (const row of presets.querySelectorAll('[data-preset-id]')) {
      const checking = value?.checking.has(Number(row.dataset.presetId));
      row.querySelectorAll('button').forEach(button => {
        button.disabled = busy || checking || (button.dataset.action === 'apply' && (!value.versionId || !selected(value).length));
      });
    }
    if (!value) {count.textContent = '尚未扫描'; return;}
    count.textContent = value.loading ? '正在扫描…' : `${value.occurrences.length} 处跳转 · ${new Set(value.occurrences.map(item => item.url)).size} 个地址 · 已选 ${selected(value).length} 处`;
    summary.textContent = `跳转位置 · ${selected(value).length} / ${value.occurrences.length} 处已选择`;
  }

  function renderVersions(value) {
    version.replaceChildren();
    if (!value.published) {const empty = element('option', '', '当前 B（尚未发布）'); empty.value = ''; version.append(empty);}
    for (const item of value.versions) {
      const option = element('option', '', `${item.name}${item.id === value.published ? ' · 当前发布' : ' · 未发布'}`);
      option.value = item.id; version.append(option);
    }
    version.value = value.versionId || '';
    const live = value.versions.find(item => item.id === value.published);
    current.replaceChildren(element('strong', '', `当前 B 发布：${live?.name || (value.published ? '已发布版本' : '尚未发布')}`));
    if (value.active?.version_id === value.published) current.append(element('span', '', `当前使用：${value.active.url}`));
    if (value.versionId && value.versionId !== value.published) current.append(element('small', '', '正在扫描未发布版本；使用链接会发布此版本的换链副本。'));
  }

  function renderOccurrences(value) {
    occurrences.replaceChildren(); warnings.replaceChildren();
    if (!value.occurrences.length) occurrences.append(element('p', 'empty-state', value.versionId ? '此版本没有识别到可替换的跳转地址。' : '尚未发布 B 页面。可先导入 B 版本，或保存预设链接。'));
    for (const item of value.occurrences) {
      const label = element('label', 'b-redirect-occurrence');
      const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.checked = !value.excluded.has(item.key);
      checkbox.dataset.key = item.key; checkbox.value = item.id;
      const info = element('span'); info.append(element('strong', '', `${item.path}:${item.line} · ${kinds[item.kind] || item.kind}`), element('small', '', item.url));
      checkbox.addEventListener('change', () => {
        if (!isCurrent(value)) return;
        if (checkbox.checked) value.excluded.delete(item.key); else value.excluded.add(item.key);
        value.selectionDirty = true; updateControls();
      });
      label.append(checkbox, info); occurrences.append(label);
    }
    for (const warning of value.warnings) warnings.append(element('p', 'subtle', warning));
    warnings.append(element('small', '', '仅替换可静态识别的位置；动态拼接、运行时生成等跳转可能需要手动编辑源码。'));
    updateControls();
  }

  function memberWeight(value, id) {
    return value.split?.members.find(item => item.preset_id === id)?.weight || 1;
  }

  function rememberMember(value, id, checked, weight) {
    value.splitDirty = true;
    const members = (value.split.members || []).filter(item => item.preset_id !== id);
    if (checked) members.push({preset_id: id, weight: Math.min(100, Math.max(1, Number(weight) || 1))});
    value.split.members = members;
    renderSplit(value);
  }

  function renderSplit(value) {
    const split = value.split || blankSplit();
    splitToggle.textContent = split.enabled ? '关闭分流' : '开启分流';
    splitToggle.setAttribute('aria-pressed', String(Boolean(split.enabled)));
    splitMode.value = split.mode === 'weighted' ? 'weighted' : 'random';
    root.classList.toggle('split-weighted', splitMode.value === 'weighted');
    const status = splitPanel.querySelector('.b-split-status');
    const count = split.members.length;
    status.textContent = split.enabled ? `已开启 · ${split.mode === 'weighted' ? '按概率' : '随机'} · ${count} 条` : '已关闭';
  }

  function renderPresets(value) {
    presets.replaceChildren();
    if (!value.presets.length) presets.append(element('p', 'empty-state', '还没有预设链接。保存后可一键换入 B 页面。'));
    const statuses = {normal:'正常', abnormal:'不正常', unknown:'状态未知', unchecked:'未检测'};
    const activeId = activePreset(value);
    for (const item of value.presets) {
      const inUse = item.id === activeId;
      const row = element('div', `b-redirect-preset${inUse ? ' active' : ''}`); row.dataset.presetId = item.id;
      const info = element('div', 'b-redirect-preset-info');
      const participate = element('label', 'b-split-join');
      const join = element('input'); join.type = 'checkbox'; join.checked = (value.split?.members || []).some(member => member.preset_id === item.id);
      const weight = element('input', 'b-split-weight'); weight.type = 'number'; weight.min = '1'; weight.max = '100'; weight.value = String(memberWeight(value, item.id));
      weight.setAttribute('aria-label', '比重');
      participate.append(join, element('span', '', '参与分流'), weight);
      join.addEventListener('change', () => { if (isCurrent(value)) rememberMember(value, item.id, join.checked, weight.value); });
      weight.addEventListener('change', () => { if (isCurrent(value) && join.checked) rememberMember(value, item.id, true, weight.value); });
      info.append(participate);
      if (inUse) info.append(element('span', 'b-redirect-active-badge', '当前使用中'));
      info.append(element('strong', 'b-redirect-url', item.url));
      if (item.note) info.append(element('p', 'subtle', item.note));
      const check = item.check || {status:'unchecked'};
      const status = element('span', `b-redirect-check ${check.status}`, value.checking.has(item.id) ? '正在检测…' : statuses[check.status] || '状态未知');
      const checkLine = element('div', 'b-redirect-check-line');
      checkLine.append(status, element('small', '', `${check.platform || '网址'} · ${date(check.checked_at)}`));
      info.append(checkLine);
      if (check.detail) info.append(element('small', 'b-redirect-check-detail', check.detail));
      const actions = element('div', 'actions');
      for (const [action, text] of [['apply','使用此链接'],['check','检测'],['delete','删除']]) {
        const button = element('button', `button ${action === 'apply' ? 'primary' : 'secondary'} small`, text);
        button.type = 'button'; button.dataset.action = action;
        on(button, 'click', () => action === 'apply' ? apply(value, item) : action === 'check' ? checkPreset(value, item.id) : deletePreset(value, item));
        actions.append(button);
      }
      row.append(info, actions); presets.append(row);
    }
    renderSplit(value);
    updateControls();
  }

  async function scan(value, {presetsOnly = false} = {}) {
    if (!isCurrent(value)) return;
    const request = ++value.request;
    const presetsRevision = value.presetsRevision;
    if (!presetsOnly) {value.loading = true; updateControls();}
    try {
      const result = await api(endpoint(value));
      if (!isCurrent(value) || request !== value.request) return;
      if (presetsRevision === value.presetsRevision) value.presets = result.presets;
      value.active = result.active;
      if (!value.splitDirty) value.split = result.split || blankSplit();
      if (!presetsOnly) {
        value.versionId = result.version?.id || null; value.expectedPublished = result.published_version;
        value.occurrences = result.occurrences; value.warnings = result.warnings;
        value.scanned = true;
        renderVersions(value); renderOccurrences(value);
      }
      renderPresets(value);
    } catch (error) {
      if (isCurrent(value) && request === value.request && !error.stale) showMessage(`扫描失败，当前选择仍保留。${error.message}`, true);
    } finally {
      if (isCurrent(value) && request === value.request) {value.loading = false; updateControls();}
    }
  }

  async function checkPreset(value, id) {
    if (!isCurrent(value) || value.checking.has(id)) return;
    value.checking.add(id); renderPresets(value);
    try {
      const result = await api(`/api/b-redirects/presets/${id}/check`, {method:'POST', body:{}});
      if (isCurrent(value)) {++value.presetsRevision; value.presets = value.presets.map(item => item.id === id ? result.preset : item);}
    } catch (error) {
      if (isCurrent(value) && !error.stale) showMessage(`检测未完成。${error.message}`, true);
    } finally {
      if (isCurrent(value)) {value.checking.delete(id); renderPresets(value);}
    }
  }

  function checkAdded(value, ids) {
    const generation = queueGeneration;
    for (const id of ids) {
      checkQueue = checkQueue.then(() => {
        if (active && generation === queueGeneration && isCurrent(value)) return checkPreset(value, id);
      });
    }
  }

  async function apply(value, preset) {
    if (!isCurrent(value) || value.busy || value.loading || value.checking.has(preset.id) || !value.versionId || !selected(value).length) return;
    value.busy = true; updateControls(); showMessage('正在检测链接并换入 B 页面…');
    try {
      const result = await api('/api/b-redirects/apply', {method:'POST', body:{version_id:value.versionId, preset_id:preset.id, occurrence_ids:selected(value).map(item => item.id), expected_published:value.expectedPublished}});
      if (!isCurrent(value)) return;
      value.versionId = result.version.id; value.published = result.version.id;
      value.expectedPublished = result.version.id; value.selectionDirty = false;
      if (!value.versions.some(item => item.id === result.version.id)) value.versions.push(result.version);
      value.occurrences = []; renderVersions(value); renderOccurrences(value);
      let refreshFailed = false;
      try {await onApplied(result);}
      catch (error) {if (!isCurrent(value) || error.stale) return; refreshFailed = true;}
      if (!isCurrent(value)) return;
      await scan(value);
      if (isCurrent(value)) showMessage(refreshFailed
        ? `B 页已换链并发布，但状态刷新失败。请刷新状态确认当前版本。${result.check.status === 'unknown' ? '此链接状态未知，请确认目标页面。' : ''}`
        : result.check.status === 'unknown'
        ? `已替换 ${result.changed} 处并发布 B；此链接状态未知。${result.check.detail || '请确认目标页面。'}`
        : `已替换 ${result.changed} 处并发布 B，旧版本已保留。`, refreshFailed);
    } catch (error) {
      if (!isCurrent(value) || error.stale) return;
      if (error.status === 400) await scan(value, {presetsOnly:true});
      if (isCurrent(value)) showMessage(error.status === 409
        ? '当前 B 发布版本已变化，换链选择仍保留。请刷新状态并检查当前 B 版本，再使用此链接。'
        : error.message, true);
    } finally {
      if (isCurrent(value)) {value.busy = false; updateControls();}
    }
  }

  async function deletePreset(value, preset) {
    if (!isCurrent(value) || value.busy || value.checking.has(preset.id)) return;
    if (!await confirmAction('删除此预设链接？删除只移除链接池记录，已发布 B 页中的地址不会改变。') || !isCurrent(value)) return;
    value.busy = true; updateControls();
    try {
      await api(`/api/b-redirects/presets/${preset.id}`, {method:'DELETE'});
      if (isCurrent(value)) {++value.presetsRevision; value.presets = value.presets.filter(item => item.id !== preset.id); renderPresets(value); showMessage('预设已删除；已发布 B 页保留原地址。');}
    } finally {if (isCurrent(value)) {value.busy = false; updateControls();}}
  }

  on(form, 'submit', async event => {
    event.preventDefault(); const value = session;
    if (!isCurrent(value) || value.busy) return;
    const links = parseLinks(urls.value);
    if (links.length > 30) throw new Error('每次最多保存 30 条预设链接。');
    value.busy = true; updateControls(); showMessage();
    try {
      const result = await api('/api/b-redirects/presets', {method:'POST', body:{urls:links, note:note.value.trim()}});
      if (!isCurrent(value)) return;
      const oldIds = new Set(value.presets.map(item => item.id));
      ++value.presetsRevision;
      value.presets = [...value.presets.filter(item => !result.presets.some(saved => saved.id === item.id)), ...result.presets];
      urls.value = ''; note.value = ''; renderPresets(value);
      showMessage(`已保存 ${result.presets.length} 条预设，正在依次检测。`);
      value.busy = false; updateControls();
      checkAdded(value, result.presets.filter(item => !oldIds.has(item.id)).map(item => item.id));
    } finally {if (isCurrent(value)) {value.busy = false; updateControls();}}
  });
  on(splitToggle, 'click', () => {
    const value = session;
    if (!isCurrent(value) || value.busy) return;
    value.splitDirty = true;
    value.split = {...(value.split || blankSplit()), enabled: !value.split?.enabled};
    renderSplit(value);
  });
  on(splitMode, 'change', () => {
    const value = session;
    if (!isCurrent(value) || value.busy) return;
    value.splitDirty = true;
    value.split = {...(value.split || blankSplit()), mode: splitMode.value};
    renderSplit(value);
  });
  on(splitSave, 'click', async () => {
    const value = session;
    if (!isCurrent(value) || value.busy) return;
    const split = value.split || blankSplit();
    value.busy = true; updateControls();
    try {
      const result = await api('/api/b-redirects/split', {method:'PUT', body:{enabled:Boolean(split.enabled), mode:split.mode === 'weighted' ? 'weighted' : 'random', members:(split.members || []).map(item => ({preset_id:item.preset_id, weight:split.mode === 'weighted' ? item.weight : 1}))}});
      if (!isCurrent(value)) return;
      value.split = result.split; value.splitDirty = false; renderPresets(value);
      showMessage(result.split.enabled ? '分流已开启。同一个访客会一直打开分到的那一条。' : '分流已关闭。B 页恢复当前发布版本里的地址。');
    } catch (error) {
      if (isCurrent(value) && !error.stale) showMessage(error.message, true);
    } finally {
      if (isCurrent(value)) {value.busy = false; updateControls();}
    }
  });
  on(version, 'change', () => selectVersion(version.value));
  on(rescan, 'click', () => {showMessage(); return session && scan(session);});

  function update(state) {
    if (!state?.site) return clear();
    let value = session;
    if (value?.siteId !== state.site.id) {
      clear();
      value = {siteId:state.site.id, versionId:state.slots.B, published:state.slots.B, expectedPublished:null, versions:[], occurrences:[], warnings:[], presets:[], presetsRevision:0, active:null, split:blankSplit(), splitDirty:false, excluded:new Set(), selectionDirty:false, checking:new Set(), request:0, busy:false, loading:false, scanned:false};
      session = value;
    }
    const changed = value.published !== state.slots.B;
    value.versions = state.versions.filter(item => item.slot === 'B');
    value.published = state.slots.B;
    const missingSelection = value.versionId && !value.versions.some(item => item.id === value.versionId);
    if (changed || missingSelection) {++value.request; value.loading = false; value.versionId = value.published; value.scanned = false; value.occurrences = []; renderOccurrences(value);}
    renderVersions(value); updateControls();
    if (active && !value.scanned && !value.loading) scan(value);
  }

  async function selectVersion(id) {
    const value = session;
    if (!isCurrent(value) || value.busy) return;
    if (value.versionId !== (id || null)) {value.occurrences = []; renderOccurrences(value);}
    value.versionId = id || null; value.scanned = false; showMessage(); renderVersions(value);
    if (active) await scan(value);
  }

  function setActive(next) {
    active = next;
    if (!active) ++queueGeneration;
    else if (session && isCurrent(session)) return scan(session);
  }

  function clear() {
    ++queueGeneration; session = null;
    urls.value = ''; note.value = ''; version.replaceChildren(); current.replaceChildren(); occurrences.replaceChildren(); warnings.replaceChildren(); presets.replaceChildren();
    renderSplit({split:blankSplit()});
    showMessage(); updateControls();
  }

  updateControls();
  return {update, clear, selectVersion, setActive, isDirty};
}
