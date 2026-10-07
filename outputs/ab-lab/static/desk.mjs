export function createDesk({api, on, element, livePhone, switchLive, screenPath = () => '/api/desk/screen', readOnly = false}) {
  const root = document.querySelector('#desk-root');
  if (!root) return {setActive() {}};
  const TICKET_KEY = 'ab-lab-desk-tickets';
  const SHEET_KEY = 'ab-lab-desk-sheet';
  let tickets = loadTickets();
  let active = false;
  let timer = 0;
  let flight = false;
  let onlineHeld = false;

  const head = element('div', 'card-heading');
  head.append(element('h2', '', '工单与投手'));
  const note = element('p', 'muted', '点「立即检查」后，下面会出现服务器上的窗口画面。验证框出来时，直接点画面里的方框。在线 APP 超过 3，或当前号码离线，就换掉正在发布的 B 页号码。进线按工单和号码分开计。消耗先乘 1.15，再除以进线得到成本。');
  const message = element('p', 'desk-message'); message.hidden = true;
  const alarm = element('div', 'desk-alarm'); alarm.hidden = true; alarm.setAttribute('role', 'alert');
  const alarmText = element('p', '', ''); alarm.append(alarmText);

  const ticketForm = element('form', 'desk-ticket-form');
  const url = field(ticketForm, '工单链接', 'url', 'https://admin.haiwangweb.com/web#/accountshow/…');
  const name = field(ticketForm, '名称', 'name', '例如 鳄鱼-梵高');
  const password = field(ticketForm, '查看密码', 'password', '工单页面上的密码');
  password.type = 'password'; password.autocomplete = 'off';
  const add = element('button', 'button secondary', '添加工单'); add.type = 'submit';
  ticketForm.append(add);
  const ticketList = element('div', 'desk-list');
  const check = element('button', 'button primary', '立即检查'); check.type = 'button'; check.id = 'desk-check';
  const screen = element('figure', 'desk-screen'); screen.hidden = true;
  const screenNote = element('p', '', '下面是服务器上的窗口。看到验证方框，就点画面里的那个方框。');
  const screenImg = element('img'); screenImg.id = 'desk-screen'; screenImg.alt = '服务器上的工单窗口';
  screen.append(screenNote, screenImg);

  const stats = element('div', 'desk-stats');
  const buyerForm = element('form', 'desk-buyer-form');
  const date = field(buyerForm, '日期', 'date', '10/06');
  const project = field(buyerForm, '项目', 'project', 'HK项目');
  const buyer = field(buyerForm, '投手', 'buyer', 'AJ');
  const phone = field(buyerForm, '号码', 'phone', '只填数字');
  const recharge = field(buyerForm, '充值', 'recharge', '0'); recharge.inputMode = 'decimal';
  const balance = field(buyerForm, '余额', 'balance', '0'); balance.inputMode = 'decimal';
  const spend = field(buyerForm, '消耗', 'spend', '图床消耗'); spend.inputMode = 'decimal';
  const leads = field(buyerForm, '进线', 'leads', '今天进线人数'); leads.inputMode = 'decimal';
  const quoteButton = element('button', 'button secondary', '生成金额'); quoteButton.type = 'submit'; quoteButton.id = 'desk-quote';
  buyerForm.append(quoteButton);
  const preview = element('pre', 'desk-preview'); preview.id = 'desk-preview';
  const sheetRow = element('form', 'desk-sheet-form');
  const sheet = field(sheetRow, '表格链接', 'spreadsheet', 'https://docs.google.com/spreadsheets/d/…/edit');
  sheet.value = loadSheet();
  const write = element('button', 'button primary', '写入这个表'); write.type = 'submit'; write.id = 'desk-write';
  sheetRow.append(write);

  root.append(head, note, message, alarm, ticketForm, ticketList, check, screen, stats, buyerForm, preview, sheetRow);
  renderTickets();

  function field(form, label, id, placeholder) {
    const wrap = element('label', '', label);
    const input = element('input'); input.id = `desk-${id}`; input.placeholder = placeholder;
    wrap.append(input); form.append(wrap); return input;
  }

  function loadTickets() {
    try {
      const saved = JSON.parse(localStorage.getItem(TICKET_KEY) || '[]');
      return Array.isArray(saved) ? saved.filter(item => item && item.url).slice(0, 20) : [];
    } catch { return []; }
  }

  function loadSheet() {
    try { return localStorage.getItem(SHEET_KEY) || ''; } catch { return ''; }
  }

  function saveTickets() {
    const stored = tickets.map(item => ({url: item.url, name: item.name || '', password: item.password || ''}));
    try { localStorage.setItem(TICKET_KEY, JSON.stringify(stored)); } catch { /* 这个浏览器记不住工单 */ }
  }

  function showMessage(text = '', error = false) {
    message.textContent = text; message.hidden = !text;
    message.classList.toggle('error', error);
  }

  function renderTickets() {
    ticketList.replaceChildren();
    if (!tickets.length) ticketList.append(element('p', 'empty-state', '还没有工单。'));
    tickets.forEach((item, index) => {
      const row = element('div', 'desk-ticket');
      row.append(element('strong', '', item.name || item.url), element('small', '', item.url));
      const remove = element('button', 'button quiet small', '移除'); remove.type = 'button';
      on(remove, 'click', () => { if (readOnly) return; tickets.splice(index, 1); saveTickets(); renderTickets(); });
      row.append(remove); ticketList.append(row);
    });
  }

  function renderStats(report) {
    stats.replaceChildren();
    const total = element('p', 'desk-total', `今天进线 ${report.total}`);
    stats.append(total);
    if (!report.rows.length) stats.append(element('p', 'empty-state', '这些工单今天没有进线明细。'));
    for (const row of report.rows) {
      const line = element('div', 'desk-lead');
      line.append(element('strong', '', row.name), element('span', '', row.phone), element('span', '', `${row.leads} 个`));
      stats.append(line);
    }
  }

  function beep() {
    const Factory = window.AudioContext || window.webkitAudioContext;
    if (!Factory) return;
    try {
      const audio = beep.context || new Factory();
      beep.context = audio;
      if (audio.state === 'suspended') audio.resume();
      const tone = audio.createOscillator(), gain = audio.createGain();
      tone.type = 'square'; tone.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, audio.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.05, audio.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.28);
      tone.connect(gain).connect(audio.destination);
      tone.start(); tone.stop(audio.currentTime + 0.3);
    } catch { /* 发不出声音时，警报文字仍然留着 */ }
  }

  let screenTimer = 0;
  let screenObject = '';

  function showScreen() {
    screen.hidden = false;
    clearInterval(screenTimer);
    const pull = async () => {
      try {
        const response = await fetch(`${screenPath()}?t=${Date.now()}`, {credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok) return;
        const blob = await response.blob();
        if (!blob.size) return;
        const nextUrl = URL.createObjectURL(blob);
        if (screenObject) URL.revokeObjectURL(screenObject);
        screenObject = nextUrl;
        screenImg.src = nextUrl;
      } catch { /* 画面还没出来时继续等 */ }
    };
    pull();
    screenTimer = setInterval(pull, 1000);
  }

  function hideScreen() {
    clearInterval(screenTimer);
    screenTimer = 0;
    screen.hidden = true;
  }

  async function review(manual = false) {
    if (readOnly || flight) return;
    if (!tickets.length) { showMessage('先添加工单。', true); return; }
    flight = true; check.disabled = true;
    showScreen();
    try {
      const report = await api('/api/desk/review', {method: 'POST', body: {phone: livePhone() || '', tickets}, timeout: 200000});
      renderStats(report);
      alarm.hidden = true; alarmText.textContent = '';
      if (report.switch !== 'online') onlineHeld = false;
      if (!manual && report.switch === 'online' && onlineHeld) {
        showMessage('在线人数仍超过 3。这一轮已经换过号，不再连续换。');
        return;
      }
      if (!report.switch) { showMessage('当前号码不用换。'); return; }
      const reason = report.switch === 'offline' ? '当前号码离线' : '在线人数超过 3';
      const switched = await switchLive({avoid: report.offline_phones || []});
      if (switched.ok) {
        if (report.switch === 'online') onlineHeld = true;
        showMessage(`${reason}，已自动换成下一个预存号码。`);
        return;
      }
      alarm.hidden = false;
      alarmText.textContent = `${reason}。${switched.detail || '没有换成这个号码。'}`;
      beep();
      showMessage(alarmText.textContent, true);
    } catch (error) {
      if (!error.stale) showMessage(error.message, true);
    } finally { hideScreen(); flight = false; check.disabled = false; }
  }

  on(ticketForm, 'submit', event => {
    event.preventDefault();
    if (readOnly) return;
    const link = url.value.trim();
    if (!link.includes('accountshow/')) { showMessage('工单链接不对。', true); return; }
    if (tickets.some(item => item.url === link)) { showMessage('这个工单已经在列表里。', true); return; }
    tickets.push({url: link, name: name.value.trim(), password: password.value});
    url.value = ''; name.value = ''; password.value = '';
    saveTickets(); renderTickets(); showMessage('已添加工单。');
  });
  on(check, 'click', () => review(true));
  on(screenImg, 'click', event => {
    if (readOnly || !screenImg.naturalWidth || !screenImg.clientWidth) return;
    const x = event.offsetX * screenImg.naturalWidth / screenImg.clientWidth;
    const y = event.offsetY * screenImg.naturalHeight / screenImg.clientHeight;
    api('/api/desk/screen/click', {method: 'POST', body: {x, y}}).catch(() => {});
  });
  on(buyerForm, 'submit', async event => {
    event.preventDefault();
    if (readOnly) return;
    try {
      const quote = await api('/api/desk/quote', {method: 'POST', body: values()});
      preview.textContent = quote.text;
      showMessage('金额已生成。');
    } catch (error) {
      if (!error.stale) showMessage(error.message, true);
    }
  });
  on(sheetRow, 'submit', async event => {
    event.preventDefault();
    if (readOnly) return;
    try { localStorage.setItem(SHEET_KEY, sheet.value.trim()); } catch { /* 记不住表格链接也继续提交 */ }
    try {
      const written = await api('/api/desk/sheet', {method: 'POST', body: {...values(), spreadsheet: sheet.value.trim()}, timeout: 60000});
      preview.textContent = written.text;
      showMessage('已写入表格。');
    } catch (error) {
      if (!error.stale) showMessage(error.message, true);
    }
  });

  function values() {
    return {
      date: date.value.trim(), project: project.value.trim(), buyer: buyer.value.trim(), phone: phone.value.trim(),
      recharge: recharge.value.trim() || '0', balance: balance.value.trim() || '0', spend: spend.value.trim() || '0', leads: leads.value.trim() || '0',
    };
  }

  function setActive(next) {
    active = next;
    clearInterval(timer); timer = 0;
    if (!active || readOnly) return;
    timer = setInterval(() => { if (active && !document.hidden && tickets.length) review(); }, 30000);
  }

  return {setActive};
}
