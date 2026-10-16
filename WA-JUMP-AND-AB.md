# 现行落地页跳转与 AB 接入说明

给其他 AI 做**另一种落地页**时用。文案、颜色、板块可以换。下面这一套拉起 WhatsApp 的做法，按现在线上页原样搬，不要自行改成“更统一”的跳法。

事实来源是 10.16 号版这两份页，两页只有号码不同：

- `landing/tpwrep-index.html`，默认号 `85294602874`
- `landing/sbmnam-index.html`，默认号 `85265453614`

旧文件 `WHATSAPP-LANDING-PLAYBOOK.md` 里，安卓浏览器的兜底重试仍写着再走一次 `intent://`。**现行代码不是这样。** 第一次用 `intent://` 没拉起之后，兜底按钮改走 `https://wa.me/`。以本文为准。

---

## 1. 做新落地页时只守这几条

1. 领取按钮必须是真正的 `<a href="https://wa.me/号码?text=已编码留言">`，并带 `data-wa` 和 `data-location`。
2. 页面打开、滚动、停几秒，都不得自动跳 WhatsApp。
3. 用户一点，先把本次该用的链接交给浏览器，再做兜底和统计。
4. 点击和跳转之间不得 `await`、不得等接口、不得先上报再跳。
5. 第 3 节的 20 种打开环境各用各的链接。不得收成一种。
6. 没拉起时出兜底面板。时间：浏览器 1600 毫秒，App 内 1200 毫秒。失焦改等 3500 毫秒。页面进后台视为已经拉起，取消面板。
7. 现在这两页**不向分流系统上传任何数据**。`eventEndpoint` 和 `leadEndpoint` 都是空字符串。新落地页默认也保持空。业主没有明确说“打开上报”时，不要填网址，不要在加载时发 `page_view`。
8. AB 只决定“这一位访客用哪个号”。号必须在用户点击之前就已经写在页面里。分流失败就用页面里原来的号，立刻跳。

---

## 2. 拉起率怎么定义

拉起成功的唯一信号：用户点了领取之后，页面进入后台（`document.hidden === true`）。这表示系统已经把 WhatsApp 拉到前台。

这时调用 `markAppOpened()`：记下 `waAttempt.left = true`，清掉兜底定时器，关掉兜底面板。

没进后台、页面还看得见，才弹出“未自动开启 WhatsApp？”。

所以：

- 拉起率 = 进入后台的次数 / 领取点击次数
- 同一次点击用同一个 `transaction_id` 把 `whatsapp_click` 和 `wa_app_opened` 串起来
- 分组看：`wa_env` × `link_type` × 广告版位 `placement`
- `link_type` 只有这几个值：`universal`、`scheme`、`intent`、`iframe`、兜底重试是 `retry`

现在上报口是关的，这两条事件不会发到分流系统。函数留着，地址为空时 `sendEvent` 第一行就返回。不要为了算拉起率在加载时加回传。

---

## 3. 五种环境，各走哪条链接

先算环境，再选链接。判断代码原样使用：

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

上面的判断收成 5 类。下面 20 条是现行页逐条对过的打开环境，做新落地页时要按名字保留，不要并成“安卓一种、苹果一种”。

兜底时间：浏览器类 1600 毫秒，WebView 类 1200 毫秒，桌面不弹。安卓浏览器第一次走 `intent://` 的，兜底重试改 `wa.me`。

| # | 环境 | 判成 | 第一次点击 | 兜底重试 |
|---|---|---|---|---|
| 1 | Android Chrome | `android_browser`，intent 可用 | `intent://` 写入 `<a href>` | `wa.me`，1600ms |
| 2 | Android 三星浏览器 SamsungBrowser | `android_browser`，intent 不用 | `wa.me`。三星的 `intent://` 会拆掉页面又拉不起 App | `wa.me`，1600ms |
| 3 | Android 小米浏览器 MiuiBrowser | 同上 | `intent://` | `wa.me`，1600ms |
| 4 | Android Edge | 同上 | `intent://` | `wa.me`，1600ms |
| 5 | Android Firefox | `android_browser`，intent 不可用 | `wa.me` | `wa.me`，1600ms |
| 6 | Android 华为浏览器 | `android_webview`。这条 UA 自带 `; wv)`，所以不走 intent | 隐藏 iframe 打开 `whatsapp://`。1200 毫秒后页面还在前台，自动再开 `wa.me` | 手动重试仍是 `whatsapp://`，并显示“在浏览器中打开” |
| 7 | Android 系统 WebView（`; wv)`） | `android_webview` | 同上：先 iframe，1200 毫秒仍在前台再自动开 `wa.me` | 手动重试 `whatsapp://`，1200ms |
| 8 | Android App 内页（九巴 App 这类自带 `; wv)` 的壳） | `android_webview` | 同上 | 手动重试 `whatsapp://`，1200ms |
| 9 | Android GMA SDK WebView（UA 含 `afma-sdk` 且带 `; wv)`） | `android_webview` | 同上 | 手动重试 `whatsapp://`，1200ms |
| 10 | Android Facebook 内置（`FB_IAB`） | `android_webview` | 同上 | 手动重试 `whatsapp://`，1200ms |
| 11 | Android 微信（`MicroMessenger`，通常还带 `; wv)`） | `android_webview` | 同上 | 手动重试 `whatsapp://`，1200ms |
| 12 | iPhone Safari 新版 | `ios_browser` | `wa.me` | `wa.me`，1600ms |
| 13 | iPhone Safari 旧版 | `ios_browser` | `wa.me` | `wa.me`，1600ms |
| 14 | iPhone Chrome（`CriOS`，UA 仍带 `Safari/`） | `ios_browser` | `wa.me` | `wa.me`，1600ms |
| 15 | iPhone Google App（`GSA`，UA 仍带 `Safari/`） | `ios_browser` | `wa.me` | `wa.me`，1600ms |
| 16 | iPhone WKWebView（没有 `Safari/`） | `ios_webview` | `whatsapp://` 写入 `<a href>` | `whatsapp://`，1200ms |
| 17 | iPhone App 内页（九巴 App 这类没有 `Safari/` 的壳） | `ios_webview` | `whatsapp://` 写入 `<a href>` | `whatsapp://`，1200ms |
| 18 | iPhone 微信（`MicroMessenger`，没有 `Safari/`） | `ios_webview` | `whatsapp://` 写入 `<a href>` | `whatsapp://`，1200ms |
| 19 | iPhone Instagram（UA 带 `Safari/`，但含 `Instagram`，仍算 WebView） | `ios_webview` | `whatsapp://` 写入 `<a href>` | `whatsapp://`，1200ms |
| 20 | 桌面 Chrome | `desktop` | `wa.me`，`target="_blank"` | 不弹兜底 |

同一类里没单独列名、但规则相同的，也要跟着走，不要另写分支：

- iPad Safari：UA 会伪装成 Mac，靠 `MacIntel` 且 `maxTouchPoints > 1` 算进 `ios_browser`，走第 12 条，不走第 20 条。
- iPhone Firefox、LINE、Facebook：带 `Safari/` 且命中内置浏览器名单的走第 19 条；没有 `Safari/` 的走第 16 条。
- Android UC、QQ、Quark、Opera Mini：跟第 5 条，走 `wa.me`。
- Android 普通 Chrome Custom Tabs：跟第 1 条，走 `intent://`。只有 UA 里真有 `; wv)` 或内置浏览器标记时才走第 7 到第 11 条。

给其他 AI 验收时用的 20 条 UA，原样照抄：

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

第三列预期：`intent` 是锚点 `href` 以 `intent://` 开头；`universal` 是 `https://wa.me/`；`scheme` 是锚点 `href` 以 `whatsapp://` 开头；`scheme@iframe` 是点击被 `preventDefault`，并且页面里出现 `src` 为 `whatsapp://` 的隐藏 iframe。桌面那条还要带 `target="_blank"`。

链接拼法：

```js
function buildWhatsAppUrls(message) {
  const phone = String(CONFIG.whatsappNumber || '').replace(/\D/g, '');
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

号码只留数字，不带 `+`。留言只 `encodeURIComponent` 一次。`intent://` 里的 `S.browser_fallback_url` 要把整条 `wa.me` 再编码一次。

不要用 `api.whatsapp.com`。不要用 `web.whatsapp.com` 当备选。

### 为什么安卓 WebView 用隐藏 iframe

主页面直接跳 `whatsapp://` 时，宿主不认这个协议，WebView 会整页变成 `ERR_UNKNOWN_URL_SCHEME`，兜底面板一起没了。iframe 里的 `whatsapp://` 同样会交给宿主；宿主放行就拉起，不放行只是 iframe 失败，页面还在。1.2 秒后页面仍在前台，先弹出兜底，并自动再打开一次 `https://wa.me/`。这一次是 https，不会把页面打成错误页。已经进后台的不跳。只自动跳一次。兜底按钮本身仍是主框架 `whatsapp://`，给宿主只放行主框架自定义协议的情况。

三星浏览器不走 `intent://`。它的 UA 里虽然有 `Chrome/`，但 `SamsungBrowser` 被排除在 `ANDROID_INTENT_OK` 之外，第一次点击直接写 `wa.me`。Chrome、小米、华为浏览器（UA 不带 `; wv)`）、Edge 仍走 `intent://`。

```js
function openWhatsAppScheme(url) {
  const frame = document.createElement('iframe');
  frame.style.display = 'none';
  frame.src = url;
  document.body.appendChild(frame);
  setTimeout(() => frame.remove(), 3000);
}
```

### 为什么安卓浏览器的重试不用 intent

第一次已经用过 `intent://`。用户还能看见兜底，说明这条路没有把 App 拉起来。重试改走 `wa.me`，让系统按 App Link 再交一次。两条路不要连点两次。

---

## 4. 点击时的顺序

按钮示例（号码和留言按那一页自己的来）：

```html
<a class="cta" id="hero-cta" data-wa data-location="hero" role="button"
   href="https://wa.me/85294602874?text=%E6%83%B3%E6%94%9E%E4%BB%BD...">按鈕文字</a>
```

页上每个领取按钮都是这样。位置名现在用 `hero`、`final`、`floating`。兜底按钮的 `href` 可以先写 `#`，点击主按钮之后由 `prepareFallback` 写成上表里的重试链接。

```js
function openWhatsApp(e, locationName) {
  const anchor = e && e.currentTarget && e.currentTarget.tagName === 'A' ? e.currentTarget : null;
  if (redirecting) return;
  redirecting = true;
  setTimeout(() => { redirecting = false; }, 2000);

  const leadCode = CONFIG.leadEndpoint ? makeLeadCode() : '';
  const txId = Date.now().toString(36) + '-' + makeLeadCode();
  const message = buildLeadMessage(leadCode);
  const urls = buildWhatsAppUrls(message);

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
    window.open(target, '_blank');
  } else {
    window.location.href = target;
  }

  waAttempt = { t: Date.now(), placement: locationName || 'unknown', left: false, linkType, txId };
  try {
    prepareFallback(urls, message);
    if (WA_ENV !== 'desktop') armFallback((IOS_WEBVIEW || ANDROID_WEBVIEW) ? CONFIG.fallbackDelayInApp : CONFIG.fallbackDelay);
  } catch (err) {}

  // 跳转已经交给浏览器。下面失败也不许影响上面。
  try { postLead({ /* 见第 6 节，地址为空则什么都不发 */ }); } catch (err) {}
  sendPageView();
  trackEvent('whatsapp_click', { event_label: locationName || 'unknown', landing_variant: currentKey, lead_code: leadCode });
  trackEvent('conversion', { send_to: CONFIG.googleSendTo, transaction_id: txId });
  sendEvent('whatsapp_click', { transaction_id: txId, lead_code: leadCode, variant: currentKey, gtag_ready: gtagReady() });
}
```

必须保持的细节：

- `redirecting` 的 2 秒解锁定时器紧跟在上锁后面。放在函数末尾的话，中途抛错会把后面的点击全锁死。
- 2 秒内又点了一次：直接 `return`，**不要** `preventDefault`。此时 `href` 还是上一次写好的链接，浏览器照跳。
- 只有安卓 WebView 才 `preventDefault`。其他环境改 `href`，让浏览器自己跳。JS 半路出错时，HTML 里写死的 `wa.me` 仍能跳。
- 从 WhatsApp 返回、页面从往返缓存恢复时：`pageshow` 且 `event.persisted` 为真，把 `redirecting` 设回 `false`，并清掉兜底定时器。

绑定：

```js
document.querySelectorAll('[data-wa]').forEach(btn => {
  btn.addEventListener('click', e => openWhatsApp(e, btn.dataset.location));
});
```

---

## 5. 兜底面板

时间写在配置里，不要改数字：

```js
fallbackDelay: 1600,       // 普通浏览器
fallbackDelayInApp: 1200   // iOS WebView 或 Android WebView
```

```js
function prepareFallback(urls, message) {
  fallbackPayload = { phone: urls.phone, message };
  const hrefByEnv = {
    ios_browser: urls.universal,
    ios_webview: urls.scheme,
    android_browser: urls.universal,
    android_webview: urls.scheme,
    desktop: urls.universal
  };
  waFallbackOpen.href = hrefByEnv[WA_ENV];
  waNumberText.textContent = `WhatsApp 號碼：+${urls.phone}`;
  waBrowserTip.classList.toggle('show', IOS_WEBVIEW || ANDROID_WEBVIEW);
}

function showFallback() {
  if (!waAttempt || waAttempt.left || document.hidden) return;
  waFallback.classList.add('show');
  waFallback.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  trackEvent('wa_fallback_shown', { placement: waAttempt.placement });
  sendEvent('wa_fallback_shown', { transaction_id: waAttempt.txId || '', wa_ms: Date.now() - waAttempt.t });
}

function armFallback(delay) {
  clearTimeout(fallbackTimer);
  fallbackTimer = setTimeout(showFallback, delay);
}

function markAppOpened() {
  if (!waAttempt || waAttempt.left) return;
  waAttempt.left = true;
  clearTimeout(fallbackTimer);
  hideFallback();
  trackEvent('wa_app_opened', { placement: waAttempt.placement, wa_ms: Date.now() - waAttempt.t });
  sendEvent('wa_app_opened', { transaction_id: waAttempt.txId || '', wa_ms: Date.now() - waAttempt.t });
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
window.addEventListener('pageshow', e => { if (e.persisted) { redirecting = false; clearTimeout(fallbackTimer); } });
```

`blur` 时不能直接取消定时器。安卓选 App、iOS 问“要在 WhatsApp 中打开吗？”都会让页面失焦，但页面还看得见。这时若立刻出面板，会盖住系统框；若直接取消，用户关掉系统框之后就再也没有兜底。所以失焦改成 3500 毫秒后再看，焦点回来且面板还没出现则 900 毫秒后再看。

面板里只放四样：再开一次、复制号码、复制留言、关闭。WebView 里再加一句：请用右上角「⋯」选「在浏览器中打开」。不要加“改用网页版”。

复制先走同步的 `document.execCommand('copy')`。部分 WebView 没有 `navigator.clipboard`，`await` 之后用户手势也没了。

兜底按钮自己的点击只记 `wa_fallback_click`，把 `linkType` 记成 `retry`。它是普通 `<a>`，跳转靠上面写好的 `href`，不要再套一层 `openWhatsApp`。

面板需要这些 id：`wa-fallback`、`wa-fallback-open`、`wa-fallback-close`、`wa-copy-number`、`wa-copy-msg`、`wa-number-text`、`wa-browser-tip`。

---

## 6. 和 AB 分流怎么配合

AB 的工作只有一件：这一位访客用号码池里的哪一个号。跳转函数不需要知道分流存在。

### 6.1 现在的上报状态

```js
leadEndpoint: '',    // 空：不生成留言编号，不向线索表 POST
eventEndpoint: ''    // 空：不向分流系统 POST
```

`sendEvent` 和 `postLead` 发现地址是空的就返回。`sendPageView()` 只在 `openWhatsApp` 里面调用一次，而且同样因为地址为空不会发出去。加载页面不会向分流系统发 `page_view`。

页头可以有业主指定的 GA4。16 号页是 `G-ES9MEXDPGX`。这是谷歌自己的统计，不是分流上传。不要自行加 Facebook Pixel，不要加 `hhucuq.top` 或任何一打开就 `sendBeacon` 的地址。

### 6.2 换号时 AB 可以动哪里

点击时号码从 `CONFIG.whatsappNumber` 现算。静态 `href` 是 JS 失效时的保底，里面的数字要和配置里的号一致。

现行页写成 `const CONFIG`，这个绑定外面另一个 `<script>` **看不见**。所以不要假设在 `</body>` 前再插一段脚本就能执行 `CONFIG.whatsappNumber = 新号`。

允许的做法，二选一：

1. **服务端在把 HTML 发给浏览器之前换号。** 同时替换 `whatsappNumber: "..."` 和所有 `https://wa.me/数字` 里的数字。接待名字如需更换，同样只替换 `CONFIG.receptionist` 和页面上的接待文字。不改跳转函数。
2. **必须用注入脚本时**，落地页先把配置挂出去：`window.CONFIG = CONFIG`。注入脚本放在主脚本之后、`</body>` 之前，同步执行，号码池写在脚本里，不发请求。同一访客用 `localStorage` 记住抽中的号。脚本只做这三件事：
   - `window.CONFIG.whatsappNumber = 抽中的号`
   - `window.CONFIG.receptionist = 对应名字`（这一页的进线语不使用接待名，可改可不改）
   - 用正则只替换 `a[href]` 里 `wa.me/` 后面和 `phone=` 后面的数字

注入脚本不得：加点击监听、`preventDefault`、改 `text=`、发请求、`location` 跳转、包一层自己的跳转函数。

分流失败、号码池为空、脚本报错：什么都不改，用户点到的就是 HTML 里原来的号。不要等服务器，不要弹错误。

广告流量必须在顶层窗口打开。放在 iframe 里时，Universal Link 和自定义协议经常被静默拦住。

如果响应头有 `Content-Security-Policy: sandbox`，必须带上：

- `allow-scripts`
- `allow-same-origin`
- `allow-forms`
- `allow-popups`
- `allow-top-navigation-to-custom-protocols`

少了最后一项，`whatsapp://` 和 `intent://` 会被浏览器静默拦住，页面看起来像按钮坏了。

CDN 缓存了 HTML 时，换号码池或换页面后要清缓存。抽号若发生在浏览器里，缓存不影响抽号本身，但会影响发出去的是哪一版页面。

### 6.3 以后业主明确要求打开分流上报时

只填 `CONFIG.eventEndpoint`。仍然遵守：

- 不在加载时发送。`page_view` 只在用户点了领取按钮之后补一条。
- 全部放在跳转交给浏览器之后。
- `POST`，`Content-Type: text/plain`，body 是 JSON。优先 `navigator.sendBeacon`，失败再用 `fetch(..., { mode: 'no-cors', keepalive: true })`。
- 每条都包在 `try/catch` 里。地址打不开也要能跳。

共有字段：`event`、`ts`、`since_load_ms`、`wa_env`、`link_type`、`cta`、`transaction_id`、`placement_app`、`creative`、`device`、`gclid`、`gbraid`、`wbraid`、`number`、`visible`、`page`、`ua`。

事件顺序：

1. `whatsapp_click`（另带 `lead_code`、`variant`、`gtag_ready`）
2. 拉起成功：`wa_app_opened`，另带 `wa_ms`（点击到进后台的毫秒）
3. 没拉起：`wa_fallback_shown`，另带 `wa_ms`
4. 之后用户的动作：`wa_fallback_click`、`wa_copy_number`、`wa_copy_message`

拉起率就是 `wa_app_opened` 的条数除以 `whatsapp_click` 的条数。按 `wa_env`、`link_type`、`placement_app` 分组，哪一格低就查哪一格的链接类型，不要改整个矩阵。

`leadEndpoint` 仍默认空。只有要在留言末尾加 6 位编号、让客服回填时才填。编号字符集去掉 `0/O/1/I/L`。没填地址就不要把编号写进留言。

广告参数在第一次带 `gclid` / `gbraid` / `wbraid` 进入时写入 `localStorage` 键 `econman_click_ids`，保留 90 天。读取的参数名：`gclid`、`gbraid`、`wbraid`、`utm_source`、`utm_medium`、`utm_campaign`、`utm_content`、`utm_term`、`placement`、`creative`、`device`。最终到达网址后缀：

```
utm_source=google&utm_medium=display&placement={placement}&creative={creative}&device={device}
```

---

## 7. 交给新落地页之前对一下

第 3 节那 20 条 UA 都要跑过，链接类型和表里一致。下面每条也都要过。任何一条被改掉，拉起率就会在某一类手机上掉。

1. 关掉 JS，点按钮，仍然打开 HTML 里那条 `https://wa.me/号码?text=`。
2. 页面静置不出现导航，也不出现 `whatsapp://` 的 iframe。
3. iPhone Safari、Chrome、Firefox、Google App：`href` 变成 `https://wa.me/`。
4. iPad（UA 伪装成 Mac，但 `maxTouchPoints > 1`）按 iPhone 浏览器处理，不按桌面处理。
5. iPhone Facebook、Instagram、LINE，以及没有 `Safari/` 的 WKWebView：`href` 变成 `whatsapp://send?phone=`。
6. Android Chrome、小米、Edge：`href` 变成 `intent://`，`package=com.whatsapp`，`browser_fallback_url` 解码后是 `https://wa.me/`。三星浏览器 `SamsungBrowser` 的 `href` 是 `https://wa.me/`，不是 `intent://`。
7. Android Firefox、UC、QQ、Quark、Opera Mini：`href` 是 `https://wa.me/`，不是 `intent://`。
8. Android WebView（`; wv)`）和 Android Facebook / Instagram：`defaultPrevented` 为真，页面里出现 `src` 以 `whatsapp://` 开头的隐藏 iframe，主文档地址不变。
9. 桌面：`https://wa.me/` 且 `target="_blank"`。
10. 安卓浏览器第一次是 `intent://` 时，兜底按钮的 `href` 是 `wa.me`，不是另一条 `intent://`。
11. 安卓 WebView 的兜底按钮是 `whatsapp://`。iOS WebView 的兜底按钮也是 `whatsapp://`。iOS 浏览器的兜底按钮是 `wa.me`。
12. 浏览器环境点击后不切后台：约 1600 毫秒出现兜底。WebView：约 1200 毫秒。桌面：不出现。
13. 点击后立刻让页面进入后台：兜底不出现，并记一次拉起成功。
14. 点击后触发 `blur`：3500 毫秒内不弹面板。
15. 2 秒内连点：不 `preventDefault`（WebView 那一次除外），每次都还有一条可用的 WhatsApp 链接。
16. `pageshow` 且 `persisted === true` 之后，下一次点击还能再跳。
17. 跳转代码前面故意抛错：静态 `wa.me` 仍在，且除 WebView 外没有被 `preventDefault`。
18. `text=` 只编码一次，里面没有未编码的空格、`#`、`&`。
19. `eventEndpoint` 为空时，加载和点击都不向分流域名发请求。
20. AB 换号只改配置里的号码和 `href` 里的数字段。五种环境的链接类型与换号前一致。

无头浏览器只能核对“交出的链接对不对”。上线前用目标 App 里的一部 iPhone 和一部 Android 真机各点一次，确认 WhatsApp 打开并且预填留言在输入框里。
