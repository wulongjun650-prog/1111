/**
 * 講經MAN 落地頁 · WhatsApp 離線轉化回傳（Google Sheets + Apps Script，免伺服器）
 *
 * 流程：
 *   落地頁點擊 → 生成編號寫入 WhatsApp 預填留言，並把 gclid 等資料 POST 到本腳本 → 記入「Leads」表
 *   客服收到帶編號嘅 WhatsApp → 喺表格剔「已開聊」/「已成交」，或用手機開客服標記頁輸入編號
 *   本腳本每小時重建「Ads_Upload」表 → Google Ads 每日定時從呢張表導入離線轉化
 *
 * 部署：
 *   1. 新建一個 Google 表格 → 擴充功能 → Apps Script，把本檔案全部貼入（SETTINGS 按需修改）
 *   2. 揀函數 setup → 執行一次（會要求授權），自動建表、建每小時觸發器，並喺「執行記錄」印出客服密鑰
 *   3. 部署 → 新增部署作業 → 類型「網頁應用程式」；執行身分「我」；存取權「所有人」
 *      複製 .../exec 網址，填入 index.html 的 CONFIG.leadEndpoint
 *   4. 客服標記頁：.../exec?key=<setup 印出的密鑰>（加入手機主畫面）；忘記密鑰可再執行 showAdminKey
 *   5. Google Ads → 目標 → 轉換 → 新增 → 匯入 → 「CRM、檔案或其他資料來源」→「追蹤點擊轉換」
 *      建立兩個轉換動作，名稱必須同 SETTINGS 一致；
 *      再到 工具 → 上傳 → 時間表 → 來源揀 Google 試算表，貼本表格網址，揀「Ads_Upload」工作表，每日執行
 *   修改程式碼後要「管理部署作業 → 編輯 → 新版本」，網址不變。
 */

const SETTINGS = {
  TIMEZONE: 'Asia/Hong_Kong',
  CHAT_CONVERSION_NAME: 'WhatsApp開聊',
  CHAT_CONVERSION_VALUE: '',
  DEAL_CONVERSION_NAME: 'WhatsApp成交',
  CURRENCY: 'HKD',
  // Google Ads 只接受點擊後 90 日內嘅離線轉化
  MAX_CLICK_AGE_DAYS: 90
};

const LEADS_SHEET = 'Leads';
const UPLOAD_SHEET = 'Ads_Upload';
const COLS = [
  '時間', '編號', '已開聊', '開聊時間', '已成交', '成交時間', '成交金額',
  'gclid', 'gbraid', 'wbraid', '分類', '重點', '變體', '位置', '環境',
  'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term',
  '號碼', '接待', '頁面', 'UA'
];
const C = Object.fromEntries(COLS.map((name, i) => [name, i + 1]));
const CODE_RE = /^[A-Z0-9]{6}$/;

function setup() {
  const ss = SpreadsheetApp.getActive();
  ss.setSpreadsheetTimeZone(SETTINGS.TIMEZONE);

  const leads = ss.getSheetByName(LEADS_SHEET) || ss.insertSheet(LEADS_SHEET);
  if (leads.getLastRow() === 0) leads.appendRow(COLS);
  leads.setFrozenRows(1);
  leads.getRange(1, 1, 1, COLS.length).setFontWeight('bold').setBackground('#fff4d6');
  // 剔選框由 doPost 逐行加入：整列 insertCheckboxes 會寫入 false，令 appendRow 跳到表尾
  const maxRows = leads.getMaxRows() - 1;
  leads.getRange(2, C['時間'], maxRows, 1).setNumberFormat('yyyy-MM-dd HH:mm:ss');
  leads.getRange(2, C['開聊時間'], maxRows, 1).setNumberFormat('yyyy-MM-dd HH:mm:ss');
  leads.getRange(2, C['成交時間'], maxRows, 1).setNumberFormat('yyyy-MM-dd HH:mm:ss');

  if (!ss.getSheetByName(UPLOAD_SHEET)) ss.insertSheet(UPLOAD_SHEET);
  rebuildUpload();

  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'rebuildUpload')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('rebuildUpload').timeBased().everyHours(1).create();
  showAdminKey();
}

/** 密鑰首次自動生成並存入指令碼屬性，唔會出現喺程式碼入面 */
function getAdminKey_() {
  const props = PropertiesService.getScriptProperties();
  let key = props.getProperty('ADMIN_KEY');
  if (!key) {
    key = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '').slice(0, 8);
    props.setProperty('ADMIN_KEY', key);
  }
  return key;
}

function showAdminKey() {
  console.log('客服標記頁：<部署網址>/exec?key=' + getAdminKey_());
}

/** 落地頁 sendBeacon 上報入口 */
function doPost(e) {
  let data;
  try {
    data = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  } catch (err) {
    return text_('bad json');
  }
  const code = String(data.code || '').toUpperCase();
  if (!CODE_RE.test(code)) return text_('bad code');

  const clean = v => String(v == null ? '' : v).slice(0, 300).replace(/^[=+\-@]/, "'$&");
  const row = new Array(COLS.length).fill('');
  row[C['時間'] - 1] = new Date();
  row[C['編號'] - 1] = code;
  row[C['已開聊'] - 1] = false;
  row[C['已成交'] - 1] = false;
  [
    ['gclid', 'gclid'], ['gbraid', 'gbraid'], ['wbraid', 'wbraid'],
    ['分類', 'interest'], ['重點', 'priority'], ['變體', 'variant'], ['位置', 'placement'], ['環境', 'wa_env'],
    ['utm_source', 'utm_source'], ['utm_medium', 'utm_medium'], ['utm_campaign', 'utm_campaign'],
    ['utm_content', 'utm_content'], ['utm_term', 'utm_term'],
    ['號碼', 'number'], ['接待', 'receptionist'], ['頁面', 'page'], ['UA', 'ua']
  ].forEach(([col, key]) => { row[C[col] - 1] = clean(data[key]); });

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const sheet = SpreadsheetApp.getActive().getSheetByName(LEADS_SHEET);
    sheet.appendRow(row);
    sheet.getRange(sheet.getLastRow(), C['已開聊']).insertCheckboxes();
    sheet.getRange(sheet.getLastRow(), C['已成交']).insertCheckboxes();
  } finally {
    lock.releaseLock();
  }
  return text_('ok');
}

/** 客服標記頁：.../exec?key=ADMIN_KEY */
function doGet(e) {
  const key = (e && e.parameter && e.parameter.key) || '';
  if (key !== getAdminKey_()) return text_('forbidden');
  return HtmlService.createHtmlOutput(markPageHtml_(key))
    .setTitle('WhatsApp 轉化標記')
    .addMetaTag('viewport', 'width=device-width,initial-scale=1');
}

/** 客服標記頁透過 google.script.run 呼叫 */
function markLead(key, code, type, value) {
  if (key !== getAdminKey_()) throw new Error('無權限');
  code = String(code || '').trim().toUpperCase();
  if (!CODE_RE.test(code)) throw new Error('編號格式唔啱（應為 6 位英文字母／數字）');

  const sheet = SpreadsheetApp.getActive().getSheetByName(LEADS_SHEET);
  const cell = sheet.getRange(2, C['編號'], Math.max(sheet.getLastRow() - 1, 1), 1)
    .createTextFinder(code).matchEntireCell(true).findNext();
  if (!cell) throw new Error('搵唔到編號 ' + code + '（用戶可能刪咗編號，或者上報未到）');

  const r = cell.getRow();
  const now = new Date();
  if (type === 'deal') {
    sheet.getRange(r, C['已成交']).setValue(true);
    if (!sheet.getRange(r, C['成交時間']).getValue()) sheet.getRange(r, C['成交時間']).setValue(now);
    if (value !== '' && value != null && !isNaN(Number(value))) sheet.getRange(r, C['成交金額']).setValue(Number(value));
  }
  // 成交必然已開聊
  sheet.getRange(r, C['已開聊']).setValue(true);
  if (!sheet.getRange(r, C['開聊時間']).getValue()) sheet.getRange(r, C['開聊時間']).setValue(now);

  rebuildUpload();
  const gclid = sheet.getRange(r, C['gclid']).getValue();
  return code + (type === 'deal' ? ' 已標記成交' : ' 已標記開聊') +
    (gclid ? '，會回傳 Google Ads' : '，但冇 gclid（可能係 iOS 隱私流量或非廣告流量），不會回傳');
}

/** 喺表格直接剔選框時自動補時間 */
function onEdit(e) {
  const range = e && e.range;
  if (!range || range.getSheet().getName() !== LEADS_SHEET || range.getRow() < 2) return;
  const col = range.getColumn();
  if (range.getValue() !== true) return;
  const sheet = range.getSheet();
  const r = range.getRow();
  if (col === C['已開聊'] && !sheet.getRange(r, C['開聊時間']).getValue()) {
    sheet.getRange(r, C['開聊時間']).setValue(new Date());
  }
  if (col === C['已成交']) {
    if (!sheet.getRange(r, C['成交時間']).getValue()) sheet.getRange(r, C['成交時間']).setValue(new Date());
    sheet.getRange(r, C['已開聊']).setValue(true);
    if (!sheet.getRange(r, C['開聊時間']).getValue()) sheet.getRange(r, C['開聊時間']).setValue(new Date());
  }
}

/** 按 Google Ads「點擊轉換」上傳範本重建 Ads_Upload；重複上傳相同 gclid + 名稱 + 時間會被 Google Ads 自動去重 */
function rebuildUpload() {
  const ss = SpreadsheetApp.getActive();
  const leads = ss.getSheetByName(LEADS_SHEET);
  const upload = ss.getSheetByName(UPLOAD_SHEET) || ss.insertSheet(UPLOAD_SHEET);
  const out = [
    ['Parameters:TimeZone=' + SETTINGS.TIMEZONE, '', '', '', ''],
    ['Google Click ID', 'Conversion Name', 'Conversion Time', 'Conversion Value', 'Conversion Currency']
  ];

  const last = leads.getLastRow();
  if (last >= 2) {
    const rows = leads.getRange(2, 1, last - 1, COLS.length).getValues();
    const minClick = Date.now() - SETTINGS.MAX_CLICK_AGE_DAYS * 86400000;
    const fmt = d => Utilities.formatDate(new Date(d), SETTINGS.TIMEZONE, 'yyyy-MM-dd HH:mm:ss');
    rows.forEach(row => {
      const gclid = row[C['gclid'] - 1];
      const clickAt = row[C['時間'] - 1];
      if (!gclid || !clickAt || new Date(clickAt).getTime() < minClick) return;
      if (row[C['已開聊'] - 1] === true && row[C['開聊時間'] - 1]) {
        out.push([gclid, SETTINGS.CHAT_CONVERSION_NAME, fmt(row[C['開聊時間'] - 1]), SETTINGS.CHAT_CONVERSION_VALUE, SETTINGS.CHAT_CONVERSION_VALUE ? SETTINGS.CURRENCY : '']);
      }
      if (row[C['已成交'] - 1] === true && row[C['成交時間'] - 1]) {
        const value = row[C['成交金額'] - 1];
        out.push([gclid, SETTINGS.DEAL_CONVERSION_NAME, fmt(row[C['成交時間'] - 1]), value === '' ? '' : value, value === '' ? '' : SETTINGS.CURRENCY]);
      }
    });
  }

  upload.clearContents();
  upload.getRange(1, 1, out.length, 5).setNumberFormat('@').setValues(out);
}

function text_(s) {
  return ContentService.createTextOutput(s).setMimeType(ContentService.MimeType.TEXT);
}

function markPageHtml_(key) {
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"PingFang TC","Noto Sans TC",sans-serif;background:#f6f4ef;color:#17233a}
    .box{max-width:420px;margin:0 auto;padding:22px 18px}
    h1{font-size:20px;margin:0 0 6px}p{margin:0 0 16px;color:#697386;font-size:13px;line-height:1.5}
    input{width:100%;box-sizing:border-box;font-size:22px;letter-spacing:.2em;text-transform:uppercase;padding:14px;border:1px solid #dfdad1;border-radius:12px;margin-bottom:10px;text-align:center}
    .amt{font-size:16px;letter-spacing:0}
    button{width:100%;min-height:52px;border:0;border-radius:12px;font-size:16px;font-weight:800;margin-top:8px;color:#fff;background:#1f9d62}
    button.deal{background:#e77c15}button:disabled{opacity:.6}
    #msg{margin-top:14px;padding:12px;border-radius:12px;background:#fff;font-size:14px;line-height:1.5;display:none}
    #msg.err{color:#c94b3f}
  </style></head><body><div class="box">
    <h1>WhatsApp 轉化標記</h1>
    <p>輸入客人留言入面嘅 6 位編號。客人有實質回覆／查詢 → 標記開聊；確認付費 → 標記成交。</p>
    <input id="code" maxlength="6" placeholder="編號" autocomplete="off" autocapitalize="characters">
    <input id="amt" class="amt" inputmode="decimal" placeholder="成交金額（可留空）">
    <button id="chat">標記已開聊</button>
    <button id="deal" class="deal">標記已成交</button>
    <div id="msg"></div>
  </div><script>
    var KEY=${JSON.stringify(key)};
    function run(type){
      var code=document.getElementById('code').value.trim().toUpperCase();
      var amt=document.getElementById('amt').value.trim();
      var msg=document.getElementById('msg');
      var btns=document.querySelectorAll('button');
      btns.forEach(function(b){b.disabled=true});
      google.script.run
        .withSuccessHandler(function(t){msg.className='';msg.textContent=t;msg.style.display='block';btns.forEach(function(b){b.disabled=false});document.getElementById('code').value='';document.getElementById('amt').value='';})
        .withFailureHandler(function(err){msg.className='err';msg.textContent=err.message||err;msg.style.display='block';btns.forEach(function(b){b.disabled=false});})
        .markLead(KEY,code,type,amt);
    }
    document.getElementById('chat').onclick=function(){run('chat')};
    document.getElementById('deal').onclick=function(){run('deal')};
  </script></body></html>`;
}
