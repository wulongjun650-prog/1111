import {parseLinks} from './helpers.mjs';

export function createBRedirects({api, on, element, confirmAction, getSite, onApplied}) {
  const root = document.querySelector('#b-redirects');
  let session = null, active = false, queueGeneration = 0, checkQueue = Promise.resolve(), trustQueue = Promise.resolve(), trustTimer = 0, trustFlight = false, splitTimer = 0, splitEpoch = 0;
  const TRUST_LABELS = {trust:'出现信任弹窗', clear:'正常', unconfirmed:'未确认', unchecked:'未检测'};
  const TRUST_CLASS = {trust:'abnormal', clear:'normal', unconfirmed:'unknown'};
  const TRUST_MIN_SECONDS = 10;
  const TRUST_MAX_SECONDS = 86400;
  const TRUST_INTERVAL_KEY = 'ab-lab-wa-trust-seconds';
  function storedTrustSeconds() {
    try {
      const saved = Number(localStorage.getItem(TRUST_INTERVAL_KEY));
      if (Number.isInteger(saved) && saved >= TRUST_MIN_SECONDS && saved <= TRUST_MAX_SECONDS) return saved;
    } catch { /* 浏览器不让保存时，继续用 10 秒 */ }
    return TRUST_MIN_SECONDS;
  }
  let trustSeconds = storedTrustSeconds();
  const header = element('div', 'card-heading');
  const title = element('div'); title.append(element('h2', '', 'B 页换链'));
  const count = element('span', 'badge neutral', '尚未扫描'); count.id = 'b-redirect-count';
  header.append(title, count);
  const current = element('div', 'b-now'); current.id = 'b-redirect-current';
  const description = element('p', 'muted', '勾选要换的位置。普通链接点「换成这条」。WhatsApp 号码在下面的号码池里换，进线语不动。');
  const message = element('p', 'b-redirect-message'); message.id = 'b-redirect-message'; message.hidden = true;
  message.setAttribute('role', 'status');
  const splitPanel = element('div', 'b-split'); splitPanel.id = 'b-redirect-split';
  const splitCopy = element('div', 'b-split-copy');
  splitCopy.append(element('strong', '', '分流'), element('p', 'b-split-status', '关闭时，访客都走当前这条跳转'));
  const splitModeLabel = element('label', 'b-split-mode-label', '分配'); splitModeLabel.hidden = true;
  const splitMode = element('select'); splitMode.id = 'b-split-mode';
  splitMode.append(element('option', '', '随机'), element('option', '', '按概率'));
  splitMode.options[0].value = 'random'; splitMode.options[1].value = 'weighted';
  splitModeLabel.append(splitMode);
  const splitToggle = element('button', 'switch'); splitToggle.type = 'button'; splitToggle.id = 'b-split-toggle';
  splitToggle.setAttribute('role', 'switch'); splitToggle.setAttribute('aria-checked', 'false'); splitToggle.setAttribute('aria-label', '分流');
  splitToggle.append(element('span'));
  splitPanel.append(splitCopy, splitModeLabel, splitToggle);
  const presets = element('div', 'b-redirect-presets'); presets.id = 'b-redirect-presets';
  const form = element('form', 'b-redirect-add-form');
  const urlsLabel = element('label', 'grow', '添加预设');
  const urls = element('textarea'); urls.id = 'b-redirect-urls'; urls.rows = 2; urls.required = true;
  urls.placeholder = '每行一个 https:// 链接，最多 30 条'; urlsLabel.append(urls);
  const noteLabel = element('label', '', '备注');
  const note = element('input'); note.id = 'b-redirect-note'; note.maxLength = 300; note.placeholder = '可选'; noteLabel.append(note);
  const add = element('button', 'button secondary', '添加链接'); add.id = 'b-redirect-add'; add.type = 'submit';
  form.append(urlsLabel, noteLabel, add);
  const poolHint = element('small', 'b-redirect-hint', '添加后会自动检测。检测只代表这一次看到的结果。分流只用这些链接。');
  const waBox = element('section', 'b-wa'); waBox.id = 'b-wa';
  const waHead = element('div', 'b-wa-head');
  waHead.append(element('strong', '', 'WS 号码'), element('p', 'b-wa-status', '只换号码。进线语、按钮和链接结构保持不动。'));
  const waNoteLine = element('p', 'b-wa-trust-note', ''); waNoteLine.id = 'b-wa-trust-note';
  waNoteLine.textContent = trustNote(false);
  const waIntervalRow = element('div', 'b-wa-interval');
  const waIntervalLabel = element('label', '', '复查间隔');
  const waInterval = element('input'); waInterval.id = 'b-wa-interval'; waInterval.type = 'number';
  waInterval.min = String(TRUST_MIN_SECONDS); waInterval.max = String(TRUST_MAX_SECONDS); waInterval.step = '1';
  waInterval.inputMode = 'numeric'; waInterval.value = String(trustSeconds);
  waInterval.setAttribute('aria-describedby', 'b-wa-trust-note');
  waIntervalLabel.append(waInterval);
  const waIntervalApply = element('button', 'button secondary', '按这个时间'); waIntervalApply.id = 'b-wa-interval-apply'; waIntervalApply.type = 'button';
  waIntervalRow.append(waIntervalLabel, element('span', 'b-wa-interval-unit', '秒'), waIntervalApply);
  const waAlarm = element('div', 'wa-trust-alarm'); waAlarm.id = 'wa-trust-alarm'; waAlarm.hidden = true; waAlarm.setAttribute('role', 'alert');
  const waAlarmText = element('p', '', ''); waAlarm.append(waAlarmText); document.body.append(waAlarm);
  const trustAudio = createTrustAlarm();
  const waList = element('div', 'b-wa-list'); waList.id = 'b-redirect-numbers';
  const waForm = element('form', 'b-redirect-add-form');
  const waLabel = element('label', 'grow', '添加号码');
  const waNumbers = element('textarea'); waNumbers.id = 'b-wa-numbers'; waNumbers.rows = 2; waNumbers.required = true;
  waNumbers.placeholder = '每行一个号码，例如 85264150954'; waLabel.append(waNumbers);
  const waNoteLabel = element('label', '', '备注');
  const waNote = element('input'); waNote.id = 'b-wa-note'; waNote.maxLength = 300; waNote.placeholder = '可选'; waNoteLabel.append(waNote);
  const waAdd = element('button', 'button secondary', '添加号码'); waAdd.id = 'b-wa-add'; waAdd.type = 'submit';
  waForm.append(waLabel, waNoteLabel, waAdd);
  waBox.dataset.trustSeconds = String(trustSeconds);
  waBox.append(waHead, waNoteLine, waIntervalRow, waList, waForm);
  waIntervalApply.addEventListener('click', applyTrustSeconds);
  waInterval.addEventListener('keydown', event => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    applyTrustSeconds();
  });
  const toolbar = element('div', 'b-redirect-toolbar');
  const versionLabel = element('label', '', '从这一版换');
  const version = element('select'); version.id = 'b-redirect-version'; versionLabel.append(version);
  const rescan = element('button', 'button quiet small', '重新扫描'); rescan.type = 'button';
  toolbar.append(versionLabel, rescan);
  const details = element('details', 'b-redirect-details'); details.open = true;
  const summary = element('summary', '', '跳转位置');
  const occurrences = element('div', 'b-redirect-occurrences'); occurrences.id = 'b-redirect-occurrences';
  details.append(summary, occurrences);
  const warnings = element('div', 'b-redirect-warnings');
  root.append(header, current, description, message, splitPanel, presets, form, poolHint, waBox, toolbar, details, warnings);

  const isCurrent = value => Boolean(value && session === value && value.siteId === getSite()?.id);
  const selected = value => value.occurrences.filter(item => !value.excluded.has(item.key));
  const linkSelected = value => selected(value).filter(item => item.kind !== 'whatsapp_number');
  const numberSelected = value => selected(value).filter(item => item.kind === 'whatsapp_number');
  const activePreset = value => (value.active && value.active.version_id === value.published) ? value.active.preset_id : null;
  const isDirty = () => Boolean(urls.value.trim() || note.value.trim() || waNumbers.value.trim() || waNote.value.trim() || session?.selectionDirty || session?.splitDirty);
  const blankSplit = () => ({enabled:false, mode:'random', members:[]});
  const date = timestamp => timestamp ? new Date(Number(timestamp) * 1000).toLocaleString('zh-CN', {hour12:false}) : '尚未检测';
  const endpoint = value => `/api/b-redirects${value.versionId ? `?version_id=${encodeURIComponent(value.versionId)}` : ''}`;
  const kinds = {anchor:'页面链接', meta_refresh:'Meta 跳转', js_location:'JS 跳转', js_variable:'跳转变量', whatsapp_number:'WhatsApp 号码'};
  const occurrenceLabel = item => {
    if (item.kind === 'whatsapp_number') return 'WhatsApp 号码';
    let host = '';
    try { host = new URL(item.url, 'https://local.invalid').hostname.toLowerCase().replace(/^www\./, ''); }
    catch { host = ''; }
    if (host === 'wa.me' || host === 'whatsapp.com' || host.endsWith('.whatsapp.com') || String(item.url).toLowerCase().startsWith('whatsapp:')) return 'WhatsApp';
    return kinds[item.kind] || item.kind;
  };
  const splitPayload = split => {
    const mode = split?.mode === 'weighted' ? 'weighted' : 'random';
    return {enabled:Boolean(split?.enabled), mode, members:(split?.members || []).map(item => ({preset_id:item.preset_id, weight:mode === 'weighted' ? Math.min(100, Math.max(1, Number(item.weight) || 1)) : 1}))};
  };
  const sameSplit = (left, right) => JSON.stringify(splitPayload(left)) === JSON.stringify(splitPayload(right));

  function showMessage(text = '', error = false) {
    message.textContent = text; message.hidden = !text;
    message.classList.toggle('error', error); message.setAttribute('role', error ? 'alert' : 'status');
  }

  function showNumberStatus(text) {
    const status = waBox.querySelector('.b-wa-status');
    if (status && text) status.textContent = text;
  }

  function paintTrustAlarm(value) {
    const alarming = Boolean(value && [...liveNumbers(value)].some(phone => value.numbers.find(row => row.phone === phone)?.trust?.status === 'trust'));
    waAlarm.hidden = !alarming;
    waAlarmText.textContent = alarming ? '当前号码出现信任弹窗。' : '';
    if (alarming) trustAudio.start(); else trustAudio.stop();
  }

  function trustNote(polling) {
    return polling
      ? `当前号码每 ${trustSeconds} 秒复查。出现信任弹窗会报警，直接进聊天会解除。`
      : `自动复查未开。要先分别看到一次信任弹窗和一次正常，才会每 ${trustSeconds} 秒看当前号码。`;
  }

  function applyTrustSeconds() {
    const raw = waInterval.value.trim();
    const seconds = Number(raw);
    if (!/^\d+$/.test(raw) || !Number.isInteger(seconds)) {
      showMessage('请填写整数秒。', true);
      return;
    }
    if (seconds < TRUST_MIN_SECONDS) {
      showMessage('最短 10 秒。', true);
      return;
    }
    if (seconds > TRUST_MAX_SECONDS) {
      showMessage('最长 86400 秒。', true);
      return;
    }
    trustSeconds = seconds;
    waInterval.value = String(seconds);
    try { localStorage.setItem(TRUST_INTERVAL_KEY, String(seconds)); } catch { /* 记不住也按这次填写的间隔走 */ }
    syncTrustTimer(session);
    showMessage(`已改为每 ${seconds} 秒复查。`);
  }

  function syncTrustTimer(value) {
    clearInterval(trustTimer); trustTimer = 0;
    waBox.dataset.trustPoll = value?.trustPoll ? '1' : '0';
    waBox.dataset.trustSeconds = String(trustSeconds);
    waNoteLine.textContent = trustNote(Boolean(value?.trustPoll));
    if (!value?.trustPoll || !isCurrent(value)) return;
    trustTimer = setInterval(() => pollCurrent(session), trustSeconds * 1000);
  }

  async function checkNumber(value, id) {
    if (!isCurrent(value) || value.trustChecking.has(id)) return;
    value.trustChecking.add(id); renderNumbers(value);
    try {
      const result = await api(`/api/b-redirects/numbers/${id}/trust`, {method:'POST', body:{}});
      if (!isCurrent(value)) return;
      value.trustPoll = Boolean(result.poll_enabled);
      value.numbersRevision += 1;
      value.numbers = value.numbers.map(item => item.id === id ? result.number : item);
      renderNumbers(value); paintTrustAlarm(value); syncTrustTimer(value);
    } catch (error) {
      if (isCurrent(value) && !error.stale) showMessage(`信任检测未完成。${error.message}`, true);
    } finally {
      if (isCurrent(value)) {value.trustChecking.delete(id); renderNumbers(value); paintTrustAlarm(value);}
    }
  }

  function checkAddedNumbers(value, ids) {
    const generation = queueGeneration;
    for (const id of ids) {
      trustQueue = trustQueue.then(() => {
        if (active && generation === queueGeneration && isCurrent(value)) return checkNumber(value, id);
      });
    }
  }

  async function pollCurrent(value) {
    if (!value || !isCurrent(value) || !value.trustPoll || document.hidden || trustFlight || value.trustChecking.size) return;
    const ids = value.numbers.filter(item => liveNumbers(value).has(item.phone)).map(item => item.id);
    if (!ids.length) return;
    trustFlight = true;
    try {
      for (const id of ids) {
        if (!isCurrent(value) || !value.trustPoll) return;
        await checkNumber(value, id);
      }
    } finally {trustFlight = false;}
  }

  function updateControls() {
    const value = session, busy = !value || value.busy || value.loading;
    version.disabled = busy || !value?.versions.length;
    rescan.disabled = busy;
    urls.disabled = !value || value.busy; note.disabled = urls.disabled; add.disabled = urls.disabled;
    waNumbers.disabled = urls.disabled; waNote.disabled = urls.disabled; waAdd.disabled = urls.disabled;
    splitToggle.disabled = !value || value.busy; splitMode.disabled = splitToggle.disabled || splitModeLabel.hidden;
    presets.querySelectorAll('.b-split-join input').forEach(input => { input.disabled = splitToggle.disabled; });
    root.setAttribute('aria-busy', String(Boolean(value?.loading || value?.busy)));
    occurrences.querySelectorAll('input').forEach(input => {input.disabled = busy;});
    for (const row of presets.querySelectorAll('[data-preset-id]')) {
      const checking = value?.checking.has(Number(row.dataset.presetId));
      row.querySelectorAll('button').forEach(button => {
        button.disabled = busy || checking || (button.dataset.action === 'apply' && (!value.versionId || !linkSelected(value).length));
      });
    }
    for (const row of waList.querySelectorAll('[data-number-id]')) {
      row.querySelectorAll('button').forEach(button => {
        button.disabled = busy || (button.dataset.action === 'recheck' && Boolean(value?.trustChecking?.has(Number(row.dataset.numberId)))) || (button.dataset.action === 'apply-number' && (!value?.versionId || !numberSelected(value).length));
      });
    }
    if (!value) {count.textContent = '尚未扫描'; return;}
    count.textContent = value.loading ? '扫描中' : `${value.occurrences.length} 处`;
    summary.textContent = `跳转位置 · 已选 ${selected(value).length} / ${value.occurrences.length}`;
  }

  function renderNow(value) {
    current.replaceChildren();
    if (!value) return;
    const live = value.versions.find(item => item.id === value.published);
    const versionText = live ? `${date(live.created)} · ${live.name}` : (value.published ? '已发布版本' : '尚未发布');
    const jump = value.active?.version_id === value.published && value.active?.url ? value.active.url : '还没有';
    const saved = value.savedSplit || blankSplit();
    const splitText = value.splitSaving ? '正在保存' : saved.enabled ? (saved.mode === 'weighted' ? '按概率' : '随机') : '已关闭';
    for (const [label, text] of [['版本', versionText], ['跳转', jump], ['分流', splitText]]) {
      const item = element('div', 'b-now-item');
      item.append(element('span', '', label), element('strong', '', text));
      current.append(item);
    }
    if (value.versionId && value.versionId !== value.published) current.append(element('small', '', '正在扫描未发布版本。换成这条会按这一版发布。'));
  }

  function renderVersions(value) {
    version.replaceChildren();
    if (!value.published) {const empty = element('option', '', '当前 B（尚未发布）'); empty.value = ''; version.append(empty);}
    for (const item of value.versions) {
      const option = element('option', '', `${date(item.created)} · ${item.name}${item.id === value.published ? ' · 当前发布' : ''}`);
      option.value = item.id; version.append(option);
    }
    version.value = value.versionId || '';
    renderNow(value);
  }

  function renderOccurrences(value) {
    occurrences.replaceChildren(); warnings.replaceChildren();
    if (!value.occurrences.length) occurrences.append(element('p', 'empty-state', value.versionId ? '这一版里没有找到能换的跳转。' : '还没有发布 B 页面。可以先导入，或先把链接存下来。'));
    for (const item of value.occurrences) {
      const label = element('label', 'b-redirect-occurrence');
      const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.checked = !value.excluded.has(item.key);
      checkbox.dataset.key = item.key; checkbox.value = item.id;
      const info = element('span'); info.append(element('strong', '', `${item.path}:${item.line} · ${occurrenceLabel(item)}`), element('small', '', item.url));
      checkbox.addEventListener('change', () => {
        if (!isCurrent(value)) return;
        if (checkbox.checked) value.excluded.delete(item.key); else value.excluded.add(item.key);
        value.selectionDirty = true; updateControls();
      });
      label.append(checkbox, info); occurrences.append(label);
    }
    for (const warning of value.warnings) warnings.append(element('p', 'subtle', warning));
    warnings.append(element('small', '', '每条单独勾选。没勾的保持原样。WhatsApp 号码只换号码本身。'));
    updateControls();
  }

  function memberWeight(value, id) {
    return value.split?.members.find(item => item.preset_id === id)?.weight || 1;
  }

  function renderSplit(value) {
    const split = value?.split || blankSplit();
    splitToggle.setAttribute('aria-checked', String(Boolean(split.enabled)));
    splitMode.value = split.mode === 'weighted' ? 'weighted' : 'random';
    splitModeLabel.hidden = !split.enabled;
    root.classList.toggle('split-on', Boolean(split.enabled));
    root.classList.toggle('split-weighted', Boolean(split.enabled) && splitMode.value === 'weighted');
    const status = splitPanel.querySelector('.b-split-status');
    if (!value || value.splitSaving) status.textContent = value?.splitSaving ? '正在保存…' : '关闭时，访客都走当前这条跳转';
    else if (!split.enabled) status.textContent = '关闭时，访客都走当前这条跳转';
    else if (!(split.members || []).length) status.textContent = '勾上之后才会开始分流。';
    else status.textContent = split.mode === 'weighted' ? '按概率。同一访客始终同一条。' : '随机。同一访客始终同一条。';
    renderNow(value);
    updateControls();
  }

  function rememberMember(value, id, checked, weight, immediate = true) {
    const split = value.split || blankSplit();
    const members = (split.members || []).filter(item => item.preset_id !== id);
    if (checked) members.push({preset_id:id, weight:Math.min(100, Math.max(1, Number(weight) || 1))});
    let enabled = Boolean(split.enabled);
    if (enabled && !members.length && value.savedSplit?.enabled) enabled = false;
    value.split = {...split, enabled, members};
    renderSplit(value);
    queueSplitSave(value, immediate || !enabled);
  }

  function queueSplitSave(value, immediate = false) {
    clearTimeout(splitTimer); splitTimer = 0;
    if (!isCurrent(value)) return;
    if (sameSplit(value.split, value.savedSplit || blankSplit())) {
      value.splitDirty = false;
      if (message.textContent === '先勾选要参与的链接') showMessage();
      updateControls();
      return;
    }
    const split = value.split || blankSplit();
    if (split.enabled && !(split.members || []).length) {
      value.splitDirty = true; showMessage('先勾选要参与的链接'); updateControls(); return;
    }
    value.splitDirty = true;
    const run = () => commitSplit(value);
    if (immediate) run(); else splitTimer = setTimeout(run, 250);
  }

  async function commitSplit(value) {
    if (!isCurrent(value)) return;
    if (value.splitSaving) {value.splitAgain = true; return;}
    const split = value.split || blankSplit();
    if (split.enabled && !(split.members || []).length) {value.splitDirty = true; showMessage('先勾选要参与的链接'); return;}
    const epoch = splitEpoch, sent = splitPayload(split);
    value.splitSaving = true; renderSplit(value);
    try {
      const result = await api('/api/b-redirects/split', {method:'PUT', body:sent});
      if (!isCurrent(value) || epoch !== splitEpoch) return;
      value.savedSplit = result.split || blankSplit();
      if (!value.splitAgain && sameSplit(value.split, sent)) {
        value.split = result.split || blankSplit(); value.splitDirty = false; renderSplit(value);
        showMessage(value.split.enabled ? '分流已开。同一访客始终同一条。' : '分流已关。都走当前这条跳转。');
      }
    } catch (error) {
      if (isCurrent(value) && epoch === splitEpoch && !error.stale) showMessage(error.message, true);
    } finally {
      if (isCurrent(value) && epoch === splitEpoch) {
        value.splitSaving = false;
        const again = value.splitAgain; value.splitAgain = false;
        if (again || !sameSplit(value.split, value.savedSplit)) queueSplitSave(value, true);
        else renderSplit(value);
      }
    }
  }

  function renderPresets(value) {
    presets.replaceChildren();
    if (!value.presets.length) presets.append(element('p', 'empty-state', '还没有预设。先把要用的链接加进来。'));
    const statuses = {normal:'正常', abnormal:'不正常', unknown:'状态未知', unchecked:'未检测'};
    const activeId = activePreset(value);
    for (const item of value.presets) {
      const inUse = item.id === activeId;
      const row = element('div', `b-redirect-preset${inUse ? ' active' : ''}`); row.dataset.presetId = item.id;
      const participate = element('div', 'b-split-join');
      const joinLabel = element('label', 'b-split-check');
      const join = element('input'); join.type = 'checkbox'; join.checked = (value.split?.members || []).some(member => member.preset_id === item.id);
      const weight = element('input', 'b-split-weight'); weight.type = 'number'; weight.min = '1'; weight.max = '100'; weight.value = String(memberWeight(value, item.id));
      weight.setAttribute('aria-label', '比重');
      joinLabel.append(join, element('span', '', '参与'));
      participate.append(joinLabel, weight);
      join.addEventListener('change', () => { if (isCurrent(value)) rememberMember(value, item.id, join.checked, weight.value, true); });
      weight.addEventListener('input', () => { if (isCurrent(value) && join.checked) rememberMember(value, item.id, true, weight.value, false); });
      const info = element('div', 'b-redirect-preset-info');
      const titleLine = element('div', 'b-redirect-title-line');
      titleLine.append(element('strong', 'b-redirect-url', item.url));
      if (inUse) titleLine.append(element('span', 'b-redirect-active-badge', '当前使用中'));
      info.append(titleLine);
      if (item.note) info.append(element('p', 'subtle', item.note));
      const check = item.check || {status:'unchecked'};
      const status = element('span', `b-redirect-check ${check.status}`, value.checking.has(item.id) ? '正在检测…' : statuses[check.status] || '状态未知');
      const checkLine = element('div', 'b-redirect-check-line');
      checkLine.append(status, element('small', '', `${check.platform || '网址'} · ${date(check.checked_at)}`));
      info.append(checkLine);
      if (check.detail) info.append(element('small', 'b-redirect-check-detail', check.detail));
      const actions = element('div', 'actions');
      const applyText = inUse ? '再换一次' : '换成这条';
      const applyClass = inUse ? 'button quiet small' : 'button primary small';
      for (const [action, text, className] of [['check','检测','button secondary small'],['delete','删除','button quiet small'],['apply',applyText,applyClass]]) {
        const button = element('button', className, text);
        button.type = 'button'; button.dataset.action = action;
        on(button, 'click', () => action === 'apply' ? apply(value, item) : action === 'check' ? checkPreset(value, item.id) : deletePreset(value, item));
        actions.append(button);
      }
      row.append(participate, info, actions); presets.append(row);
    }
    renderSplit(value);
  }

  async function scan(value, {presetsOnly = false} = {}) {
    if (!isCurrent(value)) return;
    const request = ++value.request;
    const presetsRevision = value.presetsRevision;
    const numbersRevision = value.numbersRevision;
    if (!presetsOnly) {value.loading = true; updateControls();}
    try {
      const result = await api(endpoint(value));
      if (!isCurrent(value) || request !== value.request) return;
      if (presetsRevision === value.presetsRevision) value.presets = result.presets;
      if (numbersRevision === value.numbersRevision) value.numbers = result.numbers || [];
      value.trustPoll = Boolean(result.trust_poll);
      value.active = result.active;
      if (!value.splitDirty) {value.split = result.split || blankSplit(); value.savedSplit = value.split;}
      if (!presetsOnly) {
        value.versionId = result.version?.id || null; value.expectedPublished = result.published_version;
        value.occurrences = result.occurrences; value.warnings = result.warnings;
        value.scanned = true;
        renderVersions(value); renderOccurrences(value);
      }
      renderPresets(value); renderNumbers(value); syncTrustTimer(value);
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

  function liveNumbers(value) {
    if (!value || value.versionId !== value.published) return new Set();
    return new Set(value.occurrences.filter(item => item.kind === 'whatsapp_number').map(item => item.url));
  }

  function renderNumbers(value) {
    waList.replaceChildren();
    const saved = value?.numbers || [];
    if (!saved.length) waList.append(element('p', 'empty-state', '还没有号码。每行一个，只填数字。'));
    const current = liveNumbers(value);
    for (const item of saved) {
      const inUse = current.has(item.phone);
      const row = element('div', `b-wa-row${inUse ? ' active' : ''}`); row.dataset.numberId = item.id;
      const info = element('div', 'b-redirect-preset-info');
      const titleLine = element('div', 'b-redirect-title-line');
      titleLine.append(element('strong', 'b-redirect-url', item.phone));
      if (inUse) titleLine.append(element('span', 'b-redirect-active-badge', '当前号码'));
      info.append(titleLine);
      if (item.note) info.append(element('p', 'subtle', item.note));
      const trust = item.trust || {status:'unchecked'};
      const checking = Boolean(value?.trustChecking?.has(item.id));
      const trustLine = element('div', 'b-redirect-check-line');
      trustLine.append(element('span', `b-redirect-check ${checking ? 'unknown' : (TRUST_CLASS[trust.status] || '')}`, checking ? '正在检测…' : (TRUST_LABELS[trust.status] || '未检测')));
      if (!checking && trust.detail) trustLine.append(element('small', 'b-redirect-check-detail', trust.detail));
      info.append(trustLine);
      const actions = element('div', 'actions');
      for (const [action, text, className] of [['recheck','再测一次','button secondary small'],['delete','删除','button quiet small'],['apply-number','换成这个号码','button primary small']]) {
        const button = element('button', className, text);
        button.type = 'button'; button.dataset.action = action;
        on(button, 'click', () => action === 'apply-number' ? applyNumber(value, item) : action === 'recheck' ? checkNumber(value, item.id) : deleteNumber(value, item));
        actions.append(button);
      }
      row.append(info, actions); waList.append(row);
    }
    paintTrustAlarm(value);
    updateControls();
  }

  function parseNumbers(text) {
    const lines = String(text).split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (!lines.length) throw new Error('请至少填写一个 WhatsApp 号码。');
    const phones = lines.map((line, index) => {
      const digits = line.replace(/\D/g, '');
      if (!/^\d{8,15}$/.test(digits)) throw new Error(`第 ${index + 1} 行需为 8 到 15 位数字。`);
      return digits;
    });
    return [...new Set(phones)];
  }

  async function apply(value, preset) {
    if (!isCurrent(value) || value.busy || value.loading || value.checking.has(preset.id) || !value.versionId || !linkSelected(value).length) return;
    const links = linkSelected(value);
    if (links.every(row => row.url === preset.url)) { showMessage('这些位置已经是这条链接，没有新版本。'); return; }
    value.busy = true; updateControls(); showMessage('正在检测，并换进 B 页…');
    try {
      const result = await api('/api/b-redirects/apply', {method:'POST', body:{version_id:value.versionId, preset_id:preset.id, occurrence_ids:linkSelected(value).map(item => item.id), expected_published:value.expectedPublished}});
      if (!isCurrent(value)) return;
      if (!result.changed) { showMessage('这些位置已经是这条链接，没有新版本。'); return; }
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

  async function applyNumber(value, item) {
    if (!isCurrent(value) || value.busy || value.loading || !value.versionId || !numberSelected(value).length) return;
    const targets = numberSelected(value);
    if (targets.every(row => row.url === item.phone)) {
      renderNumbers(value);
      const text = '这个号码已经是当前号码。';
      showMessage(text); showNumberStatus(text);
      try { await onApplied({changed:0}); } catch (error) { if (!isCurrent(value) || error.stale) return; }
      return;
    }
    const previous = value.occurrences.map(row => ({...row}));
    const targetIds = new Set(targets.map(row => row.id));
    value.occurrences = value.occurrences.map(row => targetIds.has(row.id) ? {...row, url:item.phone} : row);
    renderOccurrences(value); renderNumbers(value);
    value.busy = true; updateControls(); showMessage('正在更换 WhatsApp 号码…'); showNumberStatus('正在把当前号码换成 ' + item.phone + '…');
    try {
      const result = await api('/api/b-redirects/numbers/apply', {method:'POST', body:{version_id:value.versionId, number_id:item.id, occurrence_ids:targets.map(row => row.id), expected_published:value.expectedPublished}});
      if (!isCurrent(value)) return;
      if (!result.changed) {
        value.occurrences = previous; renderOccurrences(value); renderNumbers(value);
        const text = '这个号码已经是当前号码。';
        showMessage(text); showNumberStatus(text);
        try { await onApplied({changed:0}); } catch (error) { if (!isCurrent(value) || error.stale) return; }
        return;
      }
      value.versionId = result.version.id; value.published = result.version.id;
      value.expectedPublished = result.version.id; value.selectionDirty = false;
      if (!value.versions.some(saved => saved.id === result.version.id)) value.versions.push(result.version);
      renderVersions(value); renderOccurrences(value); renderNumbers(value);
      let refreshFailed = false;
      try {await onApplied(result);}
      catch (error) {if (!isCurrent(value) || error.stale) return; refreshFailed = true;}
      if (!isCurrent(value)) return;
      await scan(value);
      const cacheNote = result.cloudflare?.ok ? ' Cloudflare 缓存已清除。' : result.cloudflare ? ' 号码已换，但 Cloudflare 缓存没清掉。' : '';
      const done = refreshFailed
        ? '号码已更换并发布 B，但状态刷新失败。请刷新状态确认当前版本。进线语未改。' + cacheNote
        : `已换成 ${item.phone}。进线语未改。` + cacheNote;
      if (isCurrent(value)) { showMessage(done, refreshFailed || Boolean(result.cloudflare && !result.cloudflare.ok)); showNumberStatus(`当前号码已换成 ${item.phone}。`); }
    } catch (error) {
      if (isCurrent(value) && !error.stale) { value.occurrences = previous; renderOccurrences(value); renderNumbers(value); showNumberStatus('没有换成这个号码。'); }
      if (!isCurrent(value) || error.stale) return;
      if (error.status === 400) await scan(value);
      if (isCurrent(value)) showMessage(error.status === 409
        ? '当前 B 发布版本已变化，号码选择仍保留。请刷新状态后再换。'
        : error.message, true);
    } finally {
      if (isCurrent(value)) {value.busy = false; updateControls();}
    }
  }

  async function deleteNumber(value, item) {
    if (!isCurrent(value) || value.busy) return;
    if (!await confirmAction('从号码池删掉这个？已经发布的 B 页不会变。') || !isCurrent(value)) return;
    value.busy = true; updateControls();
    try {
      await api(`/api/b-redirects/numbers/${item.id}`, {method:'DELETE'});
      if (isCurrent(value)) {value.numbersRevision += 1; value.numbers = value.numbers.filter(saved => saved.id !== item.id); renderNumbers(value); showMessage('已从号码池删除。发布中的 B 页还是原号码。');}
    } finally {if (isCurrent(value)) {value.busy = false; updateControls();}}
  }

  async function deletePreset(value, preset) {
    if (!isCurrent(value) || value.busy || value.checking.has(preset.id)) return;
    if (!await confirmAction('从链接池删掉这条？已经发布的 B 页不会变。') || !isCurrent(value)) return;
    value.busy = true; updateControls();
    try {
      await api(`/api/b-redirects/presets/${preset.id}`, {method:'DELETE'});
      if (isCurrent(value)) {++value.presetsRevision; value.presets = value.presets.filter(item => item.id !== preset.id); renderPresets(value); showMessage('已从链接池删除。发布中的 B 页还是原地址。');}
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
      showMessage(`已添加 ${result.presets.length} 条，正在逐条检测。`);
      value.busy = false; updateControls();
      checkAdded(value, result.presets.filter(item => !oldIds.has(item.id)).map(item => item.id));
    } finally {if (isCurrent(value)) {value.busy = false; updateControls();}}
  });
  on(waForm, 'submit', async event => {
    event.preventDefault(); const value = session;
    if (!isCurrent(value) || value.busy) return;
    const phones = parseNumbers(waNumbers.value);
    if (phones.length > 30) throw new Error('每次最多保存 30 个号码。');
    value.busy = true; updateControls(); showMessage();
    try {
      const result = await api('/api/b-redirects/numbers', {method:'POST', body:{phones, note:waNote.value.trim()}});
      if (!isCurrent(value)) return;
      const oldIds = new Set(value.numbers.map(item => item.id));
      value.numbersRevision += 1;
      value.numbers = [...value.numbers.filter(item => !result.numbers.some(saved => saved.id === item.id)), ...result.numbers];
      waNumbers.value = ''; waNote.value = ''; renderNumbers(value);
      showMessage(`已添加 ${result.numbers.length} 个号码，正在检测信任弹窗。`);
      value.busy = false; updateControls();
      checkAddedNumbers(value, result.numbers.filter(item => !oldIds.has(item.id)).map(item => item.id));
    } finally {if (isCurrent(value)) {value.busy = false; updateControls();}}
  });
  on(splitToggle, 'click', () => {
    const value = session;
    if (!isCurrent(value) || value.busy) return;
    const split = value.split || blankSplit();
    value.split = {...split, enabled:!split.enabled};
    renderSplit(value);
    if (value.split.enabled && !(value.split.members || []).length) {
      value.splitDirty = true; showMessage('先勾选要参与的链接'); clearTimeout(splitTimer); splitTimer = 0; updateControls(); return;
    }
    queueSplitSave(value, true);
  });
  on(splitMode, 'change', () => {
    const value = session;
    if (!isCurrent(value) || value.busy) return;
    value.split = {...(value.split || blankSplit()), mode:splitMode.value};
    renderSplit(value);
    queueSplitSave(value, false);
  });
  on(version, 'change', () => selectVersion(version.value));
  on(rescan, 'click', () => {showMessage(); return session && scan(session);});

  function update(state) {
    if (!state?.site) return clear();
    let value = session;
    if (value?.siteId !== state.site.id) {
      clear();
      value = {siteId:state.site.id, versionId:state.slots.B, published:state.slots.B, expectedPublished:null, versions:[], occurrences:[], warnings:[], presets:[], numbers:[], presetsRevision:0, numbersRevision:0, trustPoll:false, trustChecking:new Set(), active:null, split:blankSplit(), savedSplit:blankSplit(), splitDirty:false, splitSaving:false, splitAgain:false, excluded:new Set(), selectionDirty:false, checking:new Set(), request:0, busy:false, loading:false, scanned:false};
      session = value;
    }
    if (value.busy) {
      for (const item of state.versions.filter(item => item.slot === 'B')) {
        if (!value.versions.some(saved => saved.id === item.id)) value.versions.push(item);
      }
      renderVersions(value); updateControls();
      return;
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
    clearTimeout(splitTimer); splitTimer = 0; ++splitEpoch; ++queueGeneration; clearInterval(trustTimer); trustTimer = 0; trustFlight = false; session = null;
    waAlarm.hidden = true; trustAudio.stop(); waBox.dataset.trustPoll = '0'; waNoteLine.textContent = trustNote(false);
    urls.value = ''; note.value = ''; waNumbers.value = ''; waNote.value = ''; version.replaceChildren(); occurrences.replaceChildren(); warnings.replaceChildren(); presets.replaceChildren();
    renderNumbers(null); renderSplit(null);
    showMessage(); updateControls();
  }

  function createTrustAlarm() {
    let context = null, timer = 0;
    function current() {
      const Factory = window.AudioContext || window.webkitAudioContext;
      if (!Factory) return null;
      context = context || new Factory();
      return context;
    }
    return {
      start() {
        const audio = current();
        if (!audio || timer) return;
        if (audio.state === 'suspended') audio.resume();
        const beep = () => {
          try {
            const tone = audio.createOscillator(), gain = audio.createGain();
            tone.type = 'square'; tone.frequency.value = 880;
            gain.gain.setValueAtTime(0.0001, audio.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.05, audio.currentTime + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.28);
            tone.connect(gain).connect(audio.destination);
            tone.start(); tone.stop(audio.currentTime + 0.3);
          } catch { /* A blocked sound still leaves the banner up. */ }
        };
        beep(); timer = window.setInterval(beep, 700);
      },
      stop() {clearInterval(timer); timer = 0;}
    };
  }

  renderNumbers(null);
  return {update, clear, selectVersion, setActive, isDirty};
}
