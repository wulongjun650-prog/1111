# 把一份落地页改成这套 WhatsApp 拉起

交给其他 AI 时，把**本文件**和**那一份落地 HTML** 一起贴进去，并写这一句：

> 只按 WA-LAUNCH-FOR-AGENTS.md 改跳转。文案、颜色、板块、号码、进线语都不要动。

参考实现是 19 号页 `landing/tpwrep-index.html` 与 `landing/sbmnam-index.html`。两页跳转相同，只有号码不同。规则以本文件为准。`WHATSAPP-LANDING-PLAYBOOK.md` 里「安卓兜底再走一次 intent://」已经过时，不要用。

---

## 1. 你要做的事

用户会丢进一份已经做好的落地页。你只替换「点领取之后怎么打开 WhatsApp」。

保留：

- 页面上现有的 WhatsApp 号码（只留数字，不带 `+`）
- 按钮上现有的进线语（`text=` 里已经写好的那句，原样保留，不要改写，不要加接待名）
- 按钮文字、版面、颜色、图片、日期
- 页上如果已经有 GA4，留着。不要新加 Facebook Pixel，不要新加一打开就上报的地址

改掉：

- 所有领取按钮的跳转
- 页面里旧的、和下面矩阵不一致的跳转函数。两套跳转不要并存

做完之后，20 种打开环境各走各的链接。不要收成「安卓都走 intent、苹果都走 wa.me」。

---

## 2. 不许做

1. 页面打开、滚动、停几秒，不得自动打开 WhatsApp。不得用隐藏 iframe 或 `location` 在加载时去 `wa.me`、`whatsapp://`、`intent://`。
2. 不得把 20 种环境收成一种链接。
3. 三星浏览器 `SamsungBrowser` 不得走 `intent://`。它的 UA 里有 `Chrome/`，仍然要排除。
4. 安卓 WebView 不得让主页面 `location` 跳到 `whatsapp://`。主页面会变成 `ERR_UNKNOWN_URL_SCHEME`，兜底一起没了。
5. 安卓浏览器第一次用过 `intent://` 之后，兜底重试不得再走 `intent://`。
6. 点击和跳转之间不得 `await`、不得等接口、不得先上报再跳。
7. 只有安卓 WebView 可以 `preventDefault`。其他环境改 `<a href>`，交给浏览器自己跳。
8. 不要用 `api.whatsapp.com`，不要用 `web.whatsapp.com` 当备选。
9. 进线语不能自动发送。`wa.me`、`whatsapp://`、`intent://` 都只能预填，没有自动按发送的参数。不要做自动点发送。
10. `dns-prefetch` / `preconnect` 到 `https://wa.me` 只是预热，不是拉起功能。可以留，不能拿它代替下面的矩阵，也不能在加载时 prefetch 之后再导航过去。

---

## 3. 先从丢进来的页面里抽出这三样

1. **号码。** 从现有 `wa.me/数字`、`phone=数字`、`whatsappNumber` 里取。只保留数字。一页一个号就全页用这一个。一页多个号，每个按钮保持它自己的号。
2. **进线语。** 把该按钮 `href` 里的 `text=` 解码一次，原句放回新链接，只 `encodeURIComponent` 一次。页面没有进线语就不要编一句。
3. **领取按钮。** 找出所有会打开 WhatsApp 的 `<a>`、`<button>`、`onclick`。每个都改成真正的链接：

```html
<a href="https://wa.me/页面上的号码?text=只编码一次的进线语"
   data-wa data-location="hero" role="button">原来的按钮文字</a>
```

`data-location` 用按钮原来的位置名。没有的话，页首用 `hero`，页尾用 `final`，悬浮用 `floating`，其余用简短英文。

JS 关掉时，这个静态 `href` 仍能打开 WhatsApp。所以静态链接必须是 `https://wa.me/`，不能写成 `intent://` 或 `whatsapp://`。

---

## 4. 五种环境，20 条具体机型

判断代码原样使用，不要改正则：

```js
const UA = navigator.userAgent || '';
const IS_IOS = /iPad|iPhone|iPod/.test(UA) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const IS_ANDROID = /Android/i.test(UA);
const IN_APP_RE = /FBAN|FBAV|FB_IAB|Instagram|Line\/|MicroMessenger|Twitter|TikTok|musical_ly|Bytedance|Snapchat|Pinterest|LinkedInApp/i;
const IOS_WEBVIEW = IS_IOS && (!/Safari\//.test(UA) || IN_APP_RE.test(UA));
const ANDROID_WEBVIEW = IS_ANDROID && (/; wv\)/.test(UA) || IN_APP_RE.test(UA));
const ANDROID_INTENT_OK = IS_ANDROID && /Chrome\/\d+/.test(UA) && !/SamsungBrowser|UCBrowser|Quark|MQQBrowser|Opera Mini|Firefox|FxiOS/i.test(UA);
const WA_ENV = IS_IOS ? (IOS_WEBVIEW ? 'ios_webview' : 'ios_browser')
             : IS_ANDROID ? (ANDROID_WEBVIEW ? 'android_webview' : 'android_browser')
             : 'desktop';
```

| # | 环境 | 判成 | 第一次点击 | 没拉起时 |
|---|---|---|---|---|
| 1 | Android Chrome | `android_browser`，intent 可用 | 把 `intent://` 写入 `<a href>` | 1600ms 后兜底，重试是 `wa.me` |
| 2 | Android 三星浏览器 SamsungBrowser | `android_browser`，intent 不用 | `wa.me`。三星的 `intent://` 会拆掉页面又拉不起 App | `wa.me`，1600ms |
| 3 | Android 小米浏览器 MiuiBrowser | 同上，intent 可用 | `intent://` | `wa.me`，1600ms |
| 4 | Android Edge | 同上，intent 可用 | `intent://` | `wa.me`，1600ms |
| 5 | Android Firefox | `android_browser`，intent 不可用 | `wa.me` | `wa.me`，1600ms |
| 6 | Android 华为浏览器，UA 自带 `; wv)` | `android_webview` | 隐藏 iframe 打开 `whatsapp://`。1200ms 后页面还在前台，自动再开一次 `wa.me`，只这一次 | 手动重试仍是 `whatsapp://` |
| 7 | Android 系统 WebView（`; wv)`） | `android_webview` | 同上 | 手动重试 `whatsapp://` |
| 8 | Android App 内页（自带 `; wv)`） | `android_webview` | 同上 | 手动重试 `whatsapp://` |
| 9 | Android GMA SDK WebView（`afma-sdk` 且带 `; wv)`） | `android_webview` | 同上 | 手动重试 `whatsapp://` |
| 10 | Android Facebook 内置（`FB_IAB`） | `android_webview` | 同上 | 手动重试 `whatsapp://` |
| 11 | Android 微信（`MicroMessenger`） | `android_webview` | 同上 | 手动重试 `whatsapp://` |
| 12 | iPhone Safari 新版 | `ios_browser` | `wa.me` | `wa.me`，1600ms |
| 13 | iPhone Safari 旧版 | `ios_browser` | `wa.me` | `wa.me`，1600ms |
| 14 | iPhone Chrome（`CriOS`，UA 仍带 `Safari/`） | `ios_browser` | `wa.me` | `wa.me`，1600ms |
| 15 | iPhone Google App（`GSA`，UA 仍带 `Safari/`） | `ios_browser` | `wa.me` | `wa.me`，1600ms |
| 16 | iPhone WKWebView（没有 `Safari/`） | `ios_webview` | `whatsapp://` 写入 `<a href>` | `whatsapp://`，1200ms |
| 17 | iPhone App 内页（没有 `Safari/`） | `ios_webview` | `whatsapp://` 写入 `<a href>` | `whatsapp://`，1200ms |
| 18 | iPhone 微信（没有 `Safari/`） | `ios_webview` | `whatsapp://` 写入 `<a href>` | `whatsapp://`，1200ms |
| 19 | iPhone Instagram（带 `Safari/`，但含 `Instagram`） | `ios_webview` | `whatsapp://` 写入 `<a href>` | `whatsapp://`，1200ms |
| 20 | 桌面 Chrome | `desktop` | `wa.me`，`target="_blank"` | 不弹兜底 |

同一类没单独列名的，跟着走，不要另写分支：

- iPad Safari：UA 伪装成 Mac。`MacIntel` 且 `maxTouchPoints > 1` 算第 12 条，不算第 20 条。
- Android UC、QQ、Quark、Opera Mini：跟第 5 条，走 `wa.me`。
- Android 华为浏览器如果 UA **没有** `; wv)`、也没有内置浏览器标记：跟第 1 条，走 `intent://`。不要因为它的名字叫华为就改成 WebView。
- Android Chrome Custom Tabs：跟第 1 条。只有 UA 里真有 `; wv)` 或内置浏览器标记才走第 7 到第 11 条。

链接拼法：

```js
function buildWhatsAppUrls(phone, message) {
  const text = encodeURIComponent(message);
  const universal = `https://wa.me/${phone}?text=${text}`;
  return {
    phone,
    universal,
    scheme: `whatsapp://send?phone=${phone}&text=${text}`,
    intent: `intent://send?phone=${phone}&text=${text}#Intent;scheme=whatsapp;package=com.whatsapp;S.browser_fallback_url=${encodeURIComponent(universal)};end`
  };
}
```

`intent://` 里的 `S.browser_fallback_url` 要把整条 `wa.me` 再编码一次。留言只编码一次。

---

## 5. 点击时按这个顺序

号码和进线语在点击那一刻从**被点的那条** `<a href>` 读出来。这样页面原有的换号脚本只要事先改过 `href` 里的数字，跳转就会用新号。不要在跳转函数里再请求一次分流。

```js
function readLink(anchor) {
  const href = (anchor && anchor.getAttribute('href')) || '';
  const phoneMatch = href.match(/wa\.me\/(\d+)/) || href.match(/[?&]phone=(\d+)/);
  let message = '';
  try { message = new URL(href, location.href).searchParams.get('text') || ''; } catch (e) {}
  return { phone: phoneMatch ? phoneMatch[1] : '', message };
}

function openWhatsAppScheme(url) {
  const frame = document.createElement('iframe');
  frame.style.display = 'none';
  frame.src = url;
  document.body.appendChild(frame);
  setTimeout(() => frame.remove(), 3000);
}

let redirecting = false;

function openWhatsApp(e, locationName) {
  const anchor = e && e.currentTarget && e.currentTarget.tagName === 'A' ? e.currentTarget : null;
  if (redirecting) return; // 不要 preventDefault。href 还是上一次写好的链接，浏览器照跳
  redirecting = true;
  setTimeout(() => { redirecting = false; }, 2000);

  const from = readLink(anchor);
  const phone = String((window.CONFIG && CONFIG.whatsappNumber) || from.phone || '').replace(/\D/g, '');
  const message = from.message;
  const urls = buildWhatsAppUrls(phone, message);

  let target = urls.universal, linkType = 'universal';
  if (WA_ENV === 'ios_webview') { target = urls.scheme; linkType = 'scheme'; }
  else if (WA_ENV === 'android_browser' && ANDROID_INTENT_OK) { target = urls.intent; linkType = 'intent'; }
  if (WA_ENV === 'android_webview') linkType = 'iframe';

  if (WA_ENV === 'android_webview') {
    if (e) e.preventDefault();
    openWhatsAppScheme(urls.scheme);
  } else if (anchor) {
    anchor.href = target;
    if (WA_ENV === 'desktop') { anchor.target = '_blank'; anchor.rel = 'noopener'; }
  } else if (WA_ENV === 'desktop') {
    window.open(target, '_blank', 'noopener');
  } else {
    window.location.href = target;
  }

  // 链接已经交给浏览器。下面任何一行出错都不许挡回上面。
  waAttempt = { t: Date.now(), placement: locationName || 'unknown', left: false, linkType, webHop: false };
  try {
    prepareFallback(urls, message);
    if (WA_ENV !== 'desktop') armFallback((IOS_WEBVIEW || ANDROID_WEBVIEW) ? 1200 : 1600);
  } catch (err) {}
}

document.querySelectorAll('[data-wa]').forEach(btn => {
  btn.addEventListener('click', e => openWhatsApp(e, btn.dataset.location));
});
```

2 秒解锁定时器必须紧跟在 `redirecting = true` 后面。放在函数末尾时，中途抛错会把后面的点击全锁死。

页面上如果已经有 `const CONFIG = { whatsappNumber }`，点击时用 `CONFIG.whatsappNumber` 覆盖链接里的号。另一个脚本看不见 `const CONFIG`。要让页外脚本换号，先加 `window.CONFIG = CONFIG`。换号脚本只能改号码数字和 `a[href]` 里 `wa.me/` 后面的数字，不能加点击监听，不能 `preventDefault`，不能改 `text=`，不能自己再跳一次。

---

## 6. 拉起成功，以及没拉起时的兜底

拉起成功的信号只有一个：用户点了之后，页面进入后台（`document.hidden === true`）。这时取消兜底，记 `waAttempt.left = true`。

没进后台、页面还看得见，才出兜底。

```js
const FALLBACK_BROWSER = 1600;
const FALLBACK_INAPP = 1200;

function prepareFallback(urls, message) {
  fallbackPayload = { phone: urls.phone, message, universal: urls.universal };
  const hrefByEnv = {
    ios_browser: urls.universal,
    ios_webview: urls.scheme,
    android_browser: urls.universal,
    android_webview: urls.scheme,
    desktop: urls.universal
  };
  waFallbackOpen.href = hrefByEnv[WA_ENV];
  waNumberText.textContent = 'WhatsApp 號碼：+' + urls.phone;
  waBrowserTip.classList.toggle('show', IOS_WEBVIEW || ANDROID_WEBVIEW);
}

function showFallback() {
  if (!waAttempt || waAttempt.left || document.hidden) return;
  waFallback.classList.add('show');
  waFallback.setAttribute('aria-hidden', 'false');
  if (WA_ENV === 'android_webview' && !waAttempt.webHop && fallbackPayload.universal) {
    waAttempt.webHop = true;
    try { window.location.href = fallbackPayload.universal; } catch (e) {}
  }
}

function armFallback(delay) {
  clearTimeout(fallbackTimer);
  fallbackTimer = setTimeout(showFallback, delay);
}

function markAppOpened() {
  if (!waAttempt || waAttempt.left) return;
  waAttempt.left = true;
  clearTimeout(fallbackTimer);
  waFallback.classList.remove('show');
  waFallback.setAttribute('aria-hidden', 'true');
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) markAppOpened();
  else redirecting = false;
});
window.addEventListener('pagehide', () => clearTimeout(fallbackTimer));
window.addEventListener('blur', () => { if (waAttempt && !waAttempt.left) armFallback(3500); });
window.addEventListener('focus', () => {
  if (waAttempt && !waAttempt.left && !waFallback.classList.contains('show')) armFallback(900);
});
window.addEventListener('pageshow', e => {
  if (e.persisted) { redirecting = false; clearTimeout(fallbackTimer); }
});
```

`blur` 不能直接取消定时器，也不能立刻出面板。安卓选 App、iOS 问「要在 WhatsApp 中打开吗？」都会让页面失焦，但页面还看得见。失焦改成 3500 毫秒后再看。焦点回来且面板还没出现，则 900 毫秒后再看。

安卓 WebView 的第二次 `wa.me` 写在 `showFallback` 里：1.2 秒时页面仍在前台才跳，已经进后台的不跳，只跳一次。系统选择框造成的失焦会把这一步推迟到 3500 毫秒，避免盖住正在弹出的 WhatsApp。

页面没有兜底面板就加上这一段，样式跟着页面走，不要重做视觉：

```html
<div class="wa-fallback" id="wa-fallback" aria-hidden="true">
  <div class="gate-panel" role="dialog" aria-modal="true">
    <h3 id="wa-fallback-title">未自動開啟 WhatsApp？</h3>
    <a id="wa-fallback-open" href="#">開啟 WhatsApp</a>
    <button type="button" id="wa-copy-number">複製號碼</button>
    <button type="button" id="wa-copy-msg">複製留言</button>
    <div id="wa-number-text"></div>
    <div id="wa-browser-tip">如仍未能開啟：請按右上角「⋯」，選擇「在瀏覽器中開啟」，再按一次領取按鈕。</div>
    <button type="button" id="wa-fallback-close">關閉</button>
  </div>
</div>
```

面板默认 `display:none`，带 `.show` 才显示。里面只要这四样：再开一次、复制号码、复制留言、关闭。WebView 才显示「在浏览器中打开」。不要加「改用网页版」。

复制用同步的 `document.execCommand('copy')`。部分 WebView 没有 `navigator.clipboard`，`await` 之后用户手势也没了。

兜底按钮 `#wa-fallback-open` 是普通 `<a>`。它的 `href` 由 `prepareFallback` 写成上表的重试链接。不要再套一层 `openWhatsApp`。它自己的点击只把 `linkType` 记成 `retry`。

---

## 7. 改完用这 20 条 UA 对链接

第三列是第一次点击必须交出的类型。`intent`：锚点 `href` 以 `intent://` 开头。`universal`：`https://wa.me/`。`scheme`：锚点 `href` 以 `whatsapp://` 开头。`scheme@iframe`：点击被 `preventDefault`，页面里出现 `src` 为 `whatsapp://` 的隐藏 iframe，主文档地址不变。第 20 条还要带 `target="_blank"`。

```js
const UAS = [
  ['1 Android Chrome', 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36', 'intent'],
  ['2 Android Samsung', 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/30.0 Chrome/143.0.0.0 Mobile Safari/537.36', 'universal'],
  ['3 Android Xiaomi', 'Mozilla/5.0 (Linux; Android 16; Redmi Note 14 5G Build/BP2A.250605.031.A3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.79 Mobile Safari/537.36 XiaoMi/MiuiBrowser/14.60.0-gn', 'intent'],
  ['4 Android Edge', 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Mobile Safari/537.36 EdgA/153.0.0.0', 'intent'],
  ['5 Android Firefox', 'Mozilla/5.0 (Android 16; Mobile; rv:157.0) Gecko/157.0 Firefox/157.0', 'universal'],
  ['6 Android Huawei', 'Mozilla/5.0 (Linux; Android 12; ADY-LX9 Build/HUAWEIADY-L29; wv) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.5735.196 Mobile Safari/537.36 HuaweiBrowser/17.0.7.302 HMSCore/6.16.4.352', 'scheme@iframe'],
  ['7 Android WebView', 'Mozilla/5.0 (Linux; Android 16; SM-S9360 Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/153.0.8010.36 Mobile Safari/537.36', 'scheme@iframe'],
  ['8 Android in-app wv', 'Mozilla/5.0 (Linux; Android 16; SM-S9360 Build/BP4A.251205.006; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/153.0.8010.36 Mobile Safari/537.36 aisv1/com.kmb.app1933/android/2.3.38/340', 'scheme@iframe'],
  ['9 Android GMA WebView', 'Mozilla/5.0 (Linux; Android 16; SM-A5460 Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/153.0.8010.36 Mobile Safari/537.36 (Mobile; afma-sdk-a-v261710999.252530000.1)', 'scheme@iframe'],
  ['10 Android Facebook', 'Mozilla/5.0 (Linux; Android 16; SM-A3660 Build/BP4A.251205.006) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/154.0.8037.64 Mobile Safari/537.36 [FB_IAB/FB4A;FBAV/581.0.0.45.58;IABMV/1;]', 'scheme@iframe'],
  ['11 Android WeChat', 'Mozilla/5.0 (Linux; Android 16; 25069PTEBG Build/BP2A.250605.031.A3; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 XWEB/1160289 MMWEBSDK/20260604 MicroMessenger/8.0.77.3141(0x28004D41) WeChat/arm64 Weixin NetType/WIFI Language/zh_HK ABI/arm64', 'scheme@iframe'],
  ['12 iOS Safari new', 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6.1 Mobile/15E148 Safari/604.1', 'universal'],
  ['13 iOS Safari old', 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_8_8 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6.8 Mobile/15E148 Safari/604.1', 'universal'],
  ['14 iOS Chrome', 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_6_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/154.0.8037.55 Mobile/15E148 Safari/604.1', 'universal'],
  ['15 iOS Google App', 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_6_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/441.5.989047059 Mobile/15E148 Safari/604.1', 'universal'],
  ['16 iOS WKWebView', 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148', 'scheme'],
  ['17 iOS in-app', 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 aisv1/com.kmb.app1933/ios/2.3.38/8', 'scheme'],
  ['18 iOS WeChat', 'Mozilla/5.0 (iPhone; CPU iPhone OS 26_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.78(0x18004e33) NetType/WIFI Language/zh_HK', 'scheme'],
  ['19 iOS Instagram', 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/22H352 Instagram 450.1.0.35.61 (iPhone13,4; iOS 18_7_8; zh_TW; zh-Hant; scale=3.00; 1284x2778; IABMV/1; 1084815185) Safari/604.1', 'scheme'],
  ['20 Desktop Chrome', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36', 'universal'],
];
```

再对这几条：

1. 关掉 JS，点按钮，仍然打开 HTML 里那条 `https://wa.me/号码?text=`。
2. 页面静置不出现导航，也不出现 `whatsapp://` 的 iframe。
3. 安卓浏览器第一次是 `intent://` 时，兜底按钮的 `href` 是 `wa.me`，不是另一条 `intent://`。
4. 安卓 WebView、iOS WebView 的兜底按钮是 `whatsapp://`。iOS 浏览器的兜底按钮是 `wa.me`。
5. 浏览器环境点击后不切后台：约 1600 毫秒出现兜底。WebView：约 1200 毫秒，并自动再请求一次 `wa.me`。桌面：不出现。
6. 点击后立刻让页面进入后台：兜底不出现，第二次 `wa.me` 也不发生。
7. 点击后触发 `blur`：3500 毫秒内不弹面板，也不做 WebView 的第二次跳转。
8. 2 秒内连点：不 `preventDefault`（WebView 那一次除外），每次都还有一条可用的 WhatsApp 链接。
9. `text=` 只编码一次，里面没有未编码的空格、`#`、`&`。
10. 没有新增「页面一加载就发出的」上报请求。

无头浏览器只能核对交出的链接对不对。上线前用一部 iPhone 和一部 Android 真机各点一次，确认 WhatsApp 打开，并且进线语在输入框里。
