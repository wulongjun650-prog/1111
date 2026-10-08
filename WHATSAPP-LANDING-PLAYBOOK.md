# WhatsApp 跳转落地页改造手册

> 给下一个 AI 看的施工手册。拿到一份普通的落地页源码 + 这份手册（+ 可选的 Google Ads 展示位置报表截图），按本手册改造，交付一个"必跳、先跳后报、按环境选链接、有兜底"的页面。每一步都写了**怎么做**和**为什么**，不要只抄代码不看原因——原因决定了哪些地方不能随意改。

---

## 0. 目标与原则

**场景**：Google 站外展示 / 应用内广告（AdMob、Ad Manager 投放在 AASTOCKS、etnet 等 App 内，以及手机网站）→ 手机落地页 → 用户点按钮 → WhatsApp 被拉起并带预填留言 → 客服收到询盘。

**每一次点击都是付费的。** 没跳成功 = 广告费白花 + 客源永久流失。因此：

1. **跳转第一，其它一切第二。** 打点、转化、分流、线索上报都可以晚、可以补、可以丢；跳转不可以。
2. **用户点击 = 浏览器默认导航。** 让浏览器像处理普通链接一样处理跳转，这是宿主 App / 浏览器最容易放行的方式。
3. **按环境选链接。** 没有一种链接在所有环境都最优；用错会出确认框、错误页、或静默无反应。
4. **永远有保底。** JS 失效有静态链接保底；App 没被拉起有兜底面板；统计没发出去有本地队列补发。
5. **不自作聪明。** 不自动跳转、不"先上报再跳"、不在点击和跳转之间等任何东西。

---

## 1. 拿到一份普通源码：先体检

逐条扫描，命中即修。右栏是"为什么是坑"，改造时要能向业主解释。

| 检查项 | 常见写法 | 为什么是坑 |
|---|---|---|
| 跳转按钮是 `<button>` + JS `location.href` | `onclick="window.location='https://wa.me/…'"` | JS 没加载完 / 报错 / 被宿主拦脚本时按钮完全失效；某些 WebView 对脚本发起的导航比用户点链接更严格 |
| 用 `api.whatsapp.com/send` | `https://api.whatsapp.com/send?phone=` | 它和 `wa.me` 不是一回事：`wa.me` 是 Universal Link / App Link 注册域名，系统直接拉起 App；`api.whatsapp.com` 先落到网页再引导，多一跳、多一个失败点 |
| iOS 优先用 `whatsapp://` | "iOS 用 scheme 更快" | Safari 对自定义 scheme 弹"要在 WhatsApp 中打开吗？"确认框；没装 WhatsApp 时报"地址无效"；Universal Link 则一步到位无弹窗 |
| 自动跳转 | `setTimeout(() => location.href = wa, 800)` 或加载即跳 | 非用户手势发起的导航会被 Safari / Chrome 拦截并留警告；宿主 App 会判定恶意；用户还没看清就被带走，信任归零 |
| 先上报再跳 | `gtag('event', …, { event_callback: () => location.href = wa })` | gtag 没加载、被广告拦截器挡掉、网络慢 → 回调永远不来 → 永远不跳。这是最常见的丢客原因 |
| 跳转和兜底互相竞速 | 点击后同时开 `whatsapp://` 和定时器跳 `wa.me` | 两条导航打架，常见结果是 App 拉起又被 `wa.me` 页面盖回去，或出现两次确认框 |
| 宣称"Android 会出选择器" | 用 `intent://` 是为了"避免弹出应用选择器" | 带 `package=` 的 `intent://` 才能指定 App；`wa.me` App Link 已验证域名也不会出选择器。要搞清楚原因再用 |
| 缺 `gtag('config', 'AW-…')` | 只有 `G-` 的 config，却发 `send_to: 'AW-…/…'` 的转化 | 没有对 AW 账号 config，转化事件可能根本不会发出，广告账号里看不到转化 |
| `<link rel="preconnect" crossorigin>` 指向 gtag | `<link rel="preconnect" href="https://www.googletagmanager.com" crossorigin>` | gtag.js 是 no-cors 脚本请求，带 `crossorigin` 建立的连接用不上，白白多开一条连接 |
| `user-scalable=no` | viewport 禁止缩放 | 可访问性问题，iOS 已忽略，Chrome 会在 Lighthouse 扣分，无任何收益 |
| 遮罩 `backdrop-filter: blur()` | 弹层背景模糊 | 低端 Android 上掉帧明显，影响点击响应 |
| 第三方 Pixel 同步加载 | `<script src="fbevents.js">` 在 head | 阻塞首屏；Pixel 可以先建队列再延后加载，所有调用照常排队 |
| 滚动监听每帧读布局 | `onscroll` 里读 `offsetHeight` | 强制同步排版，低端机滚动卡顿；高度只在 resize 时读，滚动用 rAF 合并 |

---

## 2. App 内流量的跳转专项（最核心的一章）

### 2.1 先搞清楚流量真正落在哪个浏览器里

"从 App 点广告过来"不等于"落在 App 的 WebView 里"。要分三类：

**A. Google Mobile Ads SDK（AdMob / Ad Manager）投放的 App 内广告**
点击后的行为是 SDK 内置的、宿主 App 改不了：

| 平台 | 实际打开环境 | UA 特征 |
|---|---|---|
| iOS | Safari 或 SFSafariViewController（Safari 引擎、Safari 规则） | 含 `Safari/`，不含 `FBAN`/`Instagram` 等 |
| Android | Chrome 或 Chrome Custom Tabs | 含 `Chrome/`，不含 `; wv)` |

AASTOCKS、etnet、Money18 等财经 App 的 Google 广告全部属于这一类。**它们的流量走浏览器路径，宿主 WebView 不在链路上。** 这是最强的两条路径，只要页面对"iOS 浏览器"和"Android Chromium 浏览器"的处理正确，这类流量就是安全的。

**B. 自带内置浏览器的社交 App**
Facebook / Instagram / Messenger / LINE / 微信 / TikTok / Twitter / Snapchat 等，点击链接在它们自己的 WebView 打开：

| 平台 | 环境 | UA 特征 |
|---|---|---|
| iOS | WKWebView | 不含 `Safari/`，或含 `FBAN` / `FBAV` / `Instagram` / `Line/` / `MicroMessenger` 等 |
| Android | 系统 WebView | 含 `; wv)`，或含 `FB_IAB` / `Instagram` 等 |

**C. 开发者自己用 WebView 打开链接的 App**
UA 特征同 B（iOS 不含 `Safari/`；Android 含 `; wv)`）。少数财经 App 自己的推送、内嵌资讯页会落到这里，但 Google 广告点击不会。

### 2.2 环境判断代码（必须原样使用）

```js
const UA = navigator.userAgent || '';
const IS_IOS = /iPad|iPhone|iPod/.test(UA) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const IS_ANDROID = /Android/i.test(UA);
const IN_APP_RE = /FBAN|FBAV|FB_IAB|Instagram|Line\/|MicroMessenger|Twitter|TikTok|musical_ly|Bytedance|Snapchat|Pinterest|LinkedInApp/i;
// iOS：Safari / SFSafariViewController / Chrome / Google App 的 UA 都帶 "Safari/"，宿主 App 自建的 WKWebView 不帶
const IOS_WEBVIEW = IS_IOS && (!/Safari\//.test(UA) || IN_APP_RE.test(UA));
// Android：系統 WebView 的 UA 帶 "; wv)"；Chrome Custom Tabs 與 Chrome 相同
const ANDROID_WEBVIEW = IS_ANDROID && (/; wv\)/.test(UA) || IN_APP_RE.test(UA));
// intent:// 只有 Chromium 內核穩定支援；Firefox、UC、QQ、Quark、Opera Mini 改用 wa.me
const ANDROID_INTENT_OK = IS_ANDROID && /Chrome\/\d+/.test(UA) && !/UCBrowser|Quark|MQQBrowser|Opera Mini|Firefox|FxiOS/i.test(UA);
const WA_ENV = IS_IOS ? (IOS_WEBVIEW ? 'ios_webview' : 'ios_browser')
             : IS_ANDROID ? (ANDROID_WEBVIEW ? 'android_webview' : 'android_browser')
             : 'desktop';
```

为什么每条规则这样写：
- `navigator.platform === 'MacIntel' && maxTouchPoints > 1`：iPadOS 13+ 的 Safari 默认伪装成 Mac，不加这条 iPad 会被当桌面。
- iOS 用"是否含 `Safari/`"判断 WebView：这是 Apple 的 WKWebView 与 Safari 系 UA 的唯一稳定差异。Chrome iOS、Firefox iOS、Google App 都是 Safari 引擎包装，UA 都带 `Safari/`，行为与 Safari 一致（Universal Link 可用）。
- `IN_APP_RE` 兜住 Facebook iOS 这种**带 `Safari/` 却是 WKWebView** 的特例，以及 Android 上不带 `; wv)` 的内置浏览器。
- `ANDROID_INTENT_OK`：`intent://` 是 **Chrome 的功能，不是 Android 系统功能**。非 Chromium 浏览器要么不认、要么行为不一致；它们对 `wa.me` App Link 的支持反而更稳定。

### 2.3 iOS 两条路，为什么不能混

| 环境 | 用 | 原因 |
|---|---|---|
| `ios_browser`（Safari / SFSafariViewController / Chrome iOS / Google App） | `https://wa.me/<号码>?text=…` | Universal Link：系统识别域名直接拉起 WhatsApp，**无确认框**；没装时落到 wa.me 网页引导安装。用 `whatsapp://` 反而弹确认框，没装时报"地址无效" |
| `ios_webview`（Facebook / Instagram / LINE 内置、自建 WKWebView） | `whatsapp://send?phone=<号码>&text=…` | WKWebView 里 Universal Link **是否触发由宿主决定**，社交 App 内置浏览器普遍不放行，结果是 `wa.me` 在 WebView 内打开网页版，用户要再点一次且常被卡住。`whatsapp://` 走的是宿主的 URL 处理回调，主流社交 App 都放行到系统 |

### 2.4 Android 三条路，为什么

| 环境 | 用 | 原因 |
|---|---|---|
| `android_browser` 且 `ANDROID_INTENT_OK`（Chrome / Custom Tabs / 三星 / 小米 / 华为 / Edge） | `intent://send?phone=<号码>&text=…#Intent;scheme=whatsapp;package=com.whatsapp;S.browser_fallback_url=<encoded wa.me>;end` | 指定 `package` → 系统直接拉起 WhatsApp，**不出选择器**；没装 → Chrome 自动转到 `browser_fallback_url`（wa.me），再由已安装的 Business 版或网页接手。比裸 `wa.me` 少一次 App Link 验证失败的风险（部分机型用户曾手动取消 WhatsApp 的"默认打开"） |
| `android_browser` 且非 Chromium（Firefox / UC / QQ / Quark / Opera Mini） | `https://wa.me/…` | 这些浏览器对 `intent://` 支持不稳定（有的直接当无效链接），对 App Link 支持更好 |
| `android_webview`（Facebook / Instagram 内置、自建 WebView） | `preventDefault()` + **隐藏 iframe** 加载 `whatsapp://send?…`，随后兜底面板提供主框架重试 | 见下 |

**为什么 Android WebView 用隐藏 iframe 而不是 `intent://`（这条曾被其它 AI 质疑，结论如下）：**
- `intent://` 是 Chrome 浏览器实现的，WebView 本身**不解析** `intent://`；在 WebView 里它和 `whatsapp://` 一样，都只是一个非 http 的 URL，交给宿主 `shouldOverrideUrlLoading` 处理。宿主处理就能跳，不处理两者都不行——`intent://` 没有额外优势，反而多一个宿主可能不认识的格式。
- 主框架直接导航到 `whatsapp://`，宿主不处理时 WebView 会显示 `ERR_UNKNOWN_URL_SCHEME` 错误页，**落地页被替换掉**，用户看到的是报错，兜底面板也没了。
- 隐藏 iframe 里的 `whatsapp://` 导航**同样会回调宿主 `shouldOverrideUrlLoading`**（WebView 对子框架的非 http 导航也回调）。宿主放行就跳；不放行只是 iframe 失败，**主页面完好**，1.2 秒后兜底面板出现。
- "Chrome 已封杀 iframe 唤起"说的是 **Chrome 浏览器**对子框架自定义协议导航的策略（需要用户手势 / 沙箱限制），与 WebView 无关；而我们在 Chrome 里走的是 `intent://` 主框架导航，根本不用 iframe。
- 兜底面板里的"重试"按钮是**用户点击 → 主框架 `whatsapp://`**，覆盖"宿主只处理主框架不处理子框架"的少数情况。iframe 先试、主框架后试，顺序不能反。

### 2.5 兜底面板的 App 内设计

| 设计点 | 做法 | 原因 |
|---|---|---|
| 触发时机 | 跳转交出后 N 毫秒页面**仍可见**（`!document.hidden`）才弹 | 页面转入后台 = App 已拉起，不能再弹 |
| 时间 | 浏览器 1600ms，App 内 1200ms | 浏览器要等系统确认流程；App 内宿主即时决定放不放行，可以更快提示 |
| 失焦处理 | `blur` 时**推迟** 3500ms，`focus` 回来 900ms 后再判 | 系统弹出"要用 WhatsApp 打开吗？"时页面失焦但仍可见，此时弹面板会盖住系统对话框；`blur` 不能直接取消定时器，否则用户取消了系统框就永远没兜底 |
| 面板内容 | 重试（主框架链接）/ 复制号码 / 复制留言 / "在浏览器中打开"提示 | 全是用户主动操作，不会再触发任何自动导航 |
| 不放什么 | **不放"改用网页链接"** | WebView 里 `web.whatsapp.com` 几乎必然失败；多一个失败的选项降低信任 |
| 浏览器提示 | 只在 `IOS_WEBVIEW || ANDROID_WEBVIEW` 显示 | 浏览器环境里这句话是噪音 |
| 成功标志 | `visibilitychange → hidden` 记为已离开，清定时器、关面板 | 这是唯一可靠的"App 已拉起"信号 |
| 复制实现 | 先同步 `execCommand('copy')`，再 `navigator.clipboard` | 部分 WebView 没有 clipboard API；`await` 之后会失去用户手势导致复制失败 |

### 2.6 App 内流量的真机验收

无头浏览器只能验证"交给系统的链接对不对"，验证不了"系统有没有真的拉起 WhatsApp"。上线前必须：在目标 App（截图里点击量前几名）内点一次真实广告或测试广告，iPhone 和 Android 各一台，确认 WhatsApp 弹出并带预填留言。这是整条链路里唯一需要真机的一步，不能省。

---

## 3. 看懂展示位置报表并据此改造

业主会直接把 Google Ads「展示位置」报表截图发过来。这不是给你"参考"的，是**改造输入**：它告诉你钱花在哪、人从哪来，你要确保这些入口的每一次点击都是有效跳转。

### 3.1 流程：输入 → 推断 → 动作 → 验证

对点击量排名靠前的每一行（通常前 10 行覆盖 90% 以上流量）：

1. **输入**：读"展示位置"名称、"类型"、点击次数、转化率。
2. **推断**：按 3.2 的映射表判断这一行的用户会落在什么浏览器环境、UA 长什么样、现有代码会判成哪个 `WA_ENV`、选哪条链接。
3. **动作**：对照第 2 章，这条链接在该环境是否最优。是 → 不动；不是 → 改环境判断或链接选择，并说明原因。
4. **验证**：用该环境的典型 UA 跑第 7 章的矩阵测试，确认该行的跳转类型正确。

### 3.2 行类型 → 环境映射

| 报表里的写法 | 入口类型 | 实际打开环境 | 预期 `WA_ENV` | 预期链接 | `{placement}` 参数值格式 |
|---|---|---|---|---|---|
| `Mobile App: … (Google Play), by …` | Android App 内 Google 广告（GMA SDK） | Chrome / Chrome Custom Tabs | `android_browser` + `ANDROID_INTENT_OK` | `intent://` | `mobileapp::2-<包名>` 如 `mobileapp::2-com.aastocks.dzh` |
| `Mobile App: … (iTunes App Store), by …` | iOS App 内 Google 广告（GMA SDK） | Safari / SFSafariViewController | `ios_browser` | `wa.me` | `mobileapp::1-<iTunes ID>` 如 `mobileapp::1-123456789` |
| 类型「网站」，如 `etnet.com.hk`、`aastocks.com` | 手机网页版广告 | 用户自己的浏览器：iOS 几乎全是 Safari 系；Android 以 Chrome 为主，但 Firefox / UC / 三星等占比高于 App 内流量 | 按 UA 分别落到 `ios_browser` / `android_browser`，非 Chromium 走 `wa.me` | 随 UA | 域名 |
| 类型「视频」/ YouTube | YouTube App 内 | iOS：SFSafariViewController；Android：Custom Tabs | 同 App 内 | 同上 | `youtube.com` 或频道 |
| `Mobile App: Facebook` / `Instagram`（若通过 Google 投放到这些 App，极少见） | 社交 App 内置浏览器 | WKWebView / Android WebView | `ios_webview` / `android_webview` | `whatsapp://` / 隐藏 iframe | `mobileapp::…` |

**判断要点**：只要写着 `Mobile App:` 且是通过 Google Ads 投放的，就是 GMA SDK 打开的浏览器环境，不是宿主 WebView。不要因为它是"App"就给它加 WebView 分支。

### 3.3 异常信号判读

| 信号 | 可能原因 | 怎么分辨 | 动作 |
|---|---|---|---|
| 同一 App 的 iOS 行转化率显著低于 Android 行（差 2 倍以上） | ① iOS 转化统计丢失：SFSafariViewController / ITP 下 gclid 与转化对不上，或 Pixel/gtag 被延迟加载前用户已离开 ② iOS 跳转确实失败 | 看 GA4 按 `ad_placement` 维度的 `whatsapp_click` → `wa_app_opened` 比例。比例正常（>85%）→ 是①统计问题；比例低 → 是②跳转问题 | ①：确认转化事件走 `track()` 延迟补报队列、`gtag('config','AW-…')` 存在、考虑启用 Enhanced Conversions；②：用该 App 的 iOS 真机复现，检查 UA 是否被误判为 WebView |
| 某行 `wa_fallback_shown` 比例异常高（>15%） | 该环境 App 没被拉起 | 对照该行 UA 的 `WA_ENV` 和链接类型 | 多半是环境误判（例如某品牌浏览器 UA 不含 `Chrome/` 却走了 `intent://`），修正正则 |
| 某行点击多但 `whatsapp_click` 事件接近 0 | 页面没加载完就被关掉，或 JS 报错 | 看 GA4 `page_view` 与 `whatsapp_click` 的比值；看 Search Console / 真机加载速度 | 性能优化（第 6 章）；检查控制台错误 |
| 「网站」类型行的 Android 转化率低于 App 内行 | 非 Chromium 浏览器占比高 | 看 `wa_env` 维度下 UA 分布 | 确认 `ANDROID_INTENT_OK` 排除列表覆盖了出现的浏览器 |

### 3.4 示范：一张真实截图的逐行分析

业主 2022-10 至 2026-10 的展示位置报表，按点击排序前 10 行：

| # | 展示位置 | 点击 | 转化率 | 推断环境 | 现有代码路径 | 判断 |
|---|---|---|---|---|---|---|
| 1 | Mobile App: M+ Mobile - Real-Time HK/US/CN (Google Play), by AASTOCKS | 6,319 | 3.06% | Android · Chrome/Custom Tabs | `android_browser` → `intent://` | 最强路径，已覆盖 |
| 2 | Mobile App: AASTOCKS M+ Mobile (iTunes App Store) | 5,586 | 1.31% | iOS · Safari/SFSVC | `ios_browser` → `wa.me` | 最强路径，已覆盖；**转化率仅为 Android 版 43%，按 3.3 第一条排查** |
| 3 | Mobile App: Money18 Real-time Stock Quote (Google Play), by ON.CC | 1,178 | 2.76% | Android · Chrome/Custom Tabs | `intent://` | 已覆盖 |
| 4 | Mobile App: etnet MQ Pro (Mobile) (Google Play) | 987 | 4.36% | Android | `intent://` | 已覆盖，表现最好 |
| 5 | Mobile App: etnet MQ Pro (iTunes App Store) | 853 | 1.17% | iOS | `wa.me` | 已覆盖；**转化率仅为 Android 版 27%，同样排查** |
| 6 | Mobile App: etnet 財經·生活 經濟通 (iTunes App Store) | 269 | 1.86% | iOS | `wa.me` | 已覆盖 |
| 7 | Mobile App: etnet 財經·生活 經濟通 (Google Play) | 126 | 5.56% | Android | `intent://` | 已覆盖 |
| 8 | Mobile App: AASTOCKS Market+ 智財迅 (iTunes App Store) | 117 | 0.00% | iOS | `wa.me` | 样本小，暂不结论；若持续为 0，用该 App 真机点一次 |
| 9 | etnet.com.hk（网站） | 87 | 4.60% | 手机浏览器，随 UA | 随 UA | 已覆盖 |
| 10 | aastocks.com（网站） | 84 | 4.76% | 手机浏览器，随 UA | 随 UA | 已覆盖 |

**结论**：前 10 行 100% 落在 `ios_browser` / `android_browser` 两条最强路径，无需新增分支。**唯一值得行动的信号是 iOS 转化率系统性偏低**（两组 App 都是 Android 的 1/3 ~ 1/2，不像自然差异）。动作：① 确认 `ad_placement` 已挂到所有事件；② 在 GA4 按 `ad_placement` 看 iOS 行的 `wa_app_opened / whatsapp_click`；③ 比例正常则是统计丢失，考虑 Enhanced Conversions 或离线转化回传；比例低则 iOS 真机复现。

### 3.5 让报表和落地页数据对得上

Google Ads 广告系列的「最终到达网址后缀」填：

```
utm_source=google&utm_medium=display&placement={placement}&creative={creative}&device={device}
```

落地页读取 `placement` / `creative` / `device` 存 localStorage 90 天，所有 GA4 事件带 `ad_placement`，线索表带「廣告版位」「廣告素材」。这样报表里每一行都能在 GA4 里按 `ad_placement` 找到对应的 `whatsapp_click → wa_app_opened → wa_fallback_shown` 漏斗。

---

## 4. 点击处理的标准顺序

### 4.1 按钮必须是 `<a href>`，带静态保底链接

```html
<a class="cta" href="https://wa.me/85257980601?text=%E4%BD%A0%E5%A5%BD…" data-wa data-location="hero" id="hero-cta" role="button">免費領取今日潛力黑馬名單</a>
```

- `href` 写死完整 `wa.me` 链接（含编码后的默认留言）。JS 没加载、报错、被拦截，点击仍由浏览器按链接跳。
- 不用 `<button>`；不用 `href="#"`；不用 `javascript:`。
- 问卷类页面：触发问卷的按钮可以是 `<button>`，但**最终确认按钮必须是 `<a href>`**。

### 4.2 `goWhatsApp` 的顺序（必须原样保持）

```js
function goWhatsApp(e, locationName) {
  const anchor = e && e.currentTarget && e.currentTarget.tagName === 'A' ? e.currentTarget : null;
  if (redirecting) return;                 // 2 秒內重複點擊：href 仍是上次的正確連結，預設導航照跳
  redirecting = true;
  setTimeout(() => { redirecting = false; }, 2000);   // 解鎖定時器緊跟上鎖，中途報錯也不會鎖死

  const leadCode = CONFIG.leadEndpoint ? makeLeadCode() : '';
  const message = buildMessage(leadCode);
  const urls = buildWhatsAppUrls(message);

  let target = urls.universal;
  if (WA_ENV === 'ios_webview') target = urls.scheme;
  else if (WA_ENV === 'android_browser' && ANDROID_INTENT_OK) target = urls.intent;

  if (WA_ENV === 'android_webview') {
    if (e) e.preventDefault();
    openInHiddenFrame(urls.scheme);
  } else if (anchor) {
    anchor.href = target;                  // 交給瀏覽器預設導航 —— 跳轉到此已經發生
    if (WA_ENV === 'desktop') { anchor.target = '_blank'; anchor.rel = 'noopener'; }
  } else if (WA_ENV === 'desktop') {
    window.open(target, '_blank');
  } else {
    window.location.href = target;
  }

  // ---------- 以下全部在跳轉已交給瀏覽器之後才執行，各自 try/catch ----------
  waAttempt = { t: Date.now(), placement: locationName || 'unknown', left: false };
  try {
    prepareFallback(urls, message);
    if (WA_ENV !== 'desktop') armFallback((IOS_WEBVIEW || ANDROID_WEBVIEW) ? CONFIG.fallbackDelayInApp : CONFIG.fallbackDelay);
  } catch (err) {}
  try { sendLead({ /* … */ }); } catch (err) {}
  try { if (typeof fbq === 'function') { fbq('track', 'Contact', { /* … */ }); } } catch (err) {}
  track('whatsapp_click', { event_label: locationName || 'unknown', /* … */ });
  track('conversion', { send_to: CONFIG.googleSendTo });
}
```

为什么：
- **改 `href` 而不是 `location.href`**：点击事件结束后浏览器按 `href` 做默认导航，这与"用户亲手点链接"完全等价，是宿主放行概率最高的导航类型；同时任何后续代码抛错都不影响已排队的导航。
- **解锁定时器紧跟上锁**：如果放在函数末尾，中途抛错会让 `redirecting` 永远为 true，之后所有点击都被 `return`。
- **重复点击不阻止默认导航**：`return` 时不 `preventDefault`，`href` 仍是上次写入的正确链接，浏览器照跳。
- **桌面走 `_blank`**：桌面 `wa.me` 会打开 WhatsApp Web / 桌面版，保留落地页。
- **bfcache**：`pageshow` 且 `persisted` 时重置 `redirecting`、清兜底定时器——从 WhatsApp 返回时页面可能从缓存恢复，状态必须重置。

### 4.3 链接构造

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

- 号码只保留数字，不带 `+`。
- 留言 `encodeURIComponent` 一次，**不要二次编码**；换行用 `\n`，编码后为 `%0A`。
- `intent://` 的 `S.browser_fallback_url` 必须再编码一次（它本身是参数值）。
- 验证方法：按 Android `Intent.parseUri` 规则解析 `intent://` 应能还原为 `whatsapp://send?phone=…&text=…`，`package` 为 `com.whatsapp`，fallback 以 `https://wa.me/` 开头。

---

## 5. 上报：可延后、可补发

### 5.1 为什么需要队列
gtag.js 是异步加载的。用户从 App 点广告进来、2 秒内点按钮跳走，gtag 大概率还没就绪——直接 `gtag('event')` 只是推进 `dataLayer`，gtag.js 加载后能否发出取决于页面是否还活着。用户已经在 WhatsApp 了，页面被冻结或杀掉，事件就丢了。

### 5.2 实现

```js
const PENDING_KEY = 'econman_pending_events';
function gtagReady() { return typeof gtag === 'function' && !!window.google_tag_manager; }

function track(name, params) {
  const payload = Object.assign({ transport_type: 'beacon', wa_env: WA_ENV, ad_placement: CLICK_IDS.placement || '(none)' }, params);
  if (gtagReady()) { try { gtag('event', name, payload); } catch (e) {} return; }
  try {
    const list = JSON.parse(localStorage.getItem(PENDING_KEY) || '[]');
    list.push({ name, params: payload, ts: Date.now() });
    localStorage.setItem(PENDING_KEY, JSON.stringify(list.slice(-50)));
  } catch (e) { try { gtag('event', name, payload); } catch (e2) {} }
}

function flushPendingEvents(force) {
  if (!gtagReady() && !(force && typeof gtag === 'function')) return;
  let list = [];
  try { list = JSON.parse(localStorage.getItem(PENDING_KEY) || '[]'); } catch (e) {}
  if (!list.length) return;
  try { localStorage.removeItem(PENDING_KEY); } catch (e) {}
  const cutoff = Date.now() - 7 * 86400000;
  list.filter(ev => ev && ev.ts > cutoff).forEach(ev => {
    try { gtag('event', ev.name, Object.assign({}, ev.params, { deferred: 1 })); } catch (e) {}
  });
}
['load', 'pageshow'].forEach(evt => window.addEventListener(evt, flushPendingEvents));
document.addEventListener('visibilitychange', () => { if (!document.hidden) flushPendingEvents(); });
const flushPoll = setInterval(() => { flushPendingEvents(); if (gtagReady()) clearInterval(flushPoll); }, 800);
setTimeout(() => { clearInterval(flushPoll); flushPendingEvents(true); }, 30000);
```

- `window.google_tag_manager` 存在 = gtag.js 真正加载完成，这是比 `typeof gtag` 可靠的就绪信号。
- `transport_type: 'beacon'`：页面卸载时也能发出。
- 补发带 `deferred: 1`，GA4 里能区分即时和补发。
- 从 WhatsApp 返回（`pageshow` / `visibilitychange`）、下次打开页面（`load`）都会补发；保留 7 天，最多 50 条。
- 30 秒仍未就绪：强制推入 `dataLayer`，避免事件积压。

### 5.3 必备配置
- `gtag('config', 'G-XXXX')` **和** `gtag('config', 'AW-XXXX')` 都要有。
- 转化：`track('conversion', { send_to: 'AW-XXXX/LABEL' })`。
- 事件统一带 `wa_env`、`ad_placement`；新增 `wa_app_opened`（页面转后台时）、`wa_fallback_shown`、`wa_fallback_click`。
- 广告点击 ID（`gclid` / `gbraid` / `wbraid` / `utm_*` / `placement` / `creative` / `device`）首次进入时存 localStorage 90 天，为离线转化回传准备。

---

## 6. 性能改造清单（按优先级）

| 优先级 | 改动 | 原因 |
|---|---|---|
| 高 | `<link rel="preconnect" href="https://www.googletagmanager.com">`（**不带** `crossorigin`）；其余第三方域 `dns-prefetch` | gtag 是 no-cors 脚本，带 crossorigin 的连接用不上 |
| 高 | 首屏横幅 `preload` + `fetchpriority="high"`；背景图 `fetchpriority="low"` | LCP 直接决定用户是否等到按钮出现 |
| 高 | Meta Pixel 先建队列 `fbq` 桩，`load` 后 1.2 秒或首次 `pointerdown`/`touchstart`/`scroll` 才加载脚本 | 不阻塞首屏；所有 `fbq()` 调用照常排队 |
| 中 | 去掉 `user-scalable=no` | 无收益、扣分 |
| 中 | 去掉遮罩 `backdrop-filter` | 低端 Android 掉帧 |
| 中 | 滚动监听只读 `scrollY`，高度在 `resize` 时读，rAF 合并 | 避免每帧强制排版 |
| 中 | 图片 `decoding="async"`、`loading="lazy"`（首屏除外） | 解码不阻塞主线程 |
| 低 | 去掉 `text-rendering: optimizeLegibility` | 长文本上的排版开销 |
| 低 | CTA 动效只用 `transform` / `opacity`，离屏时 `animation-play-state: paused`，尊重 `prefers-reduced-motion` | 合成层动画不触发重排 |

---

## 7. 改造完成后的验收清单

每一项都要跑，任何一项不过不能交付。用无头 Chrome（puppeteer）+ 请求拦截（只放行本地文件），在 `window` 捕获 click 事件记录最终 `href`、`target`、`defaultPrevented` 并 `preventDefault` 以免真的导航。

### 7.1 环境矩阵（16 种 UA）

| UA | 预期 |
|---|---|
| iPhone Safari / Chrome iOS / Firefox iOS / Google App | `https://wa.me/` |
| iPhone Facebook / Instagram / LINE 内置、一般 WKWebView（无 `Safari/`） | `whatsapp://` |
| Android Chrome / Samsung Browser | `intent://`，且能按 `Intent.parseUri` 规则解析，`package=com.whatsapp`，fallback 为 `wa.me` |
| Android Firefox / UC | `https://wa.me/` |
| Android 一般 WebView（`; wv)`）/ Facebook / Instagram 内置 | `defaultPrevented === true` 且页面出现 `src` 为 `whatsapp://` 的 iframe |
| 桌面 Chrome | `https://wa.me/` 且 `target="_blank"` |

每种 UA 还要检查：留言首句符合预期、结构正确（换行、字段）、`text=` 参数无未编码的 `#`/`&`/空白、未配置回传时不带编号、**无控制台错误**。

### 7.2 静态检查
- 无重复 `id`。
- 跳转按钮自带 `https://wa.me/<号码>?text=` 保底链接。
- 源码中无任何自动跳转模式（加载即跳、定时跳）。
- 页面静置 4 秒：无导航、无 iframe。

### 7.3 鲁棒性
- 注入一个会在点击处理中途抛错的补丁（如让 `buildMessage` 抛异常）：`href` 仍为静态保底、`defaultPrevented === false`。
- 2 秒内连点 3 次：每次 `href` 都是正确链接且未被阻止。
- 派发 `pageshow { persisted: true }`：`redirecting` 回到 `false`。

### 7.4 兜底面板
- iOS WKWebView UA 点击后不切后台：1.2 秒后面板出现；重试链接为 `whatsapp://`；号码文字正确；浏览器提示显示；复制按钮文案变化；可关闭。
- 点击后立刻把 `document.hidden` 伪造为 true 并派发 `visibilitychange`：2 秒后面板**不**出现。

### 7.5 上报
- gtag 未就绪时点击：`localStorage.econman_pending_events` 含 `whatsapp_click` 和 `conversion`。
- 设置 `window.google_tag_manager = {}` 后 1.2 秒：队列清空，`dataLayer` 中出现带 `deferred: 1` 的这两个事件。
- 带 `?gclid=TEST&placement=mobileapp::2-com.test` 进入：`econman_click_ids` 保存了两者。

### 7.6 真机
按 2.6 执行。

---

## 8. 与第三方系统共存（分流 / A/B / 换号）

业主可能同时使用一套 A/B 分流系统，由服务器在发页面时向 `</body>` 前注入一段选号脚本。已验证可接受的做法与边界：

**可接受**（已在线上验证）：
- 号码池内嵌在注入脚本里，**在浏览器端随机抽**，不发任何请求；抽中的号存 `localStorage`，同一访客重访不变。
- 脚本只做：`CONFIG.whatsappNumber = 抽中号`、`CONFIG.receptionist = 名字`、用正则把所有 `a[href]` 中 `wa.me/数字` 和 `phone=数字` 的**数字段**替换为抽中号、改接待文字。
- 不加事件监听、不 `preventDefault`、不改 `href` 的其它部分、不发请求、不触发任何导航。
- 落地页所有号码相关逻辑（跳转链接、兜底号码、复制号码、线索上报）都从 `CONFIG.whatsappNumber` **在点击时现算**，所以只要注入脚本在 `</body>` 前同步执行，整条链路自动用新号，落地页代码零改动。

**必须守住的边界**：
- 响应头若有 `Content-Security-Policy: sandbox …`，**必须包含 `allow-top-navigation-to-custom-protocols` 和 `allow-popups`**（以及 `allow-scripts`、`allow-same-origin`、`allow-forms`）。沙箱默认禁止导航到 `whatsapp://`、`intent://` 等自定义协议，删掉这两个旗标 → iOS App 内与 Android 全部跳转被浏览器**静默拦截**，页面毫无反应。
- CDN（如 Cloudflare）若缓存 HTML：关分流 / 改号码池 / 换号 / 更新页面后必须清缓存，否则旧版本最长继续发到 TTL 到期。由于抽号在浏览器端，缓存**不影响分流本身**。
- 页面被 iframe 嵌入（`frame-ancestors` 允许某域名）仅可用于后台预览，**广告流量必须顶层打开**：子框架导航通常不触发 Universal Link、`wa.me` 本身拒绝被嵌入、沙箱 iframe 不能把顶层导航到 https。

**检查方法**：用手机 UA `curl -D -` 线上地址，看响应头和 `</body>` 前的注入内容；剥掉注入脚本后与源码 diff；用真实响应头在本地重放并跑第 7 章矩阵。

---

## 9. 进线语（预填留言）

- 首句直接表达"我要领取 X"，让客服一眼识别来源；后续用空行分隔的"字段：值"结构，便于客服和自动化解析。
- 随机拼接：各池独立抽一句，`crypto.getRandomValues` 取模；每次点击重抽；抽中的组合随 `whatsapp_click` 上报（`wa_interest` / `wa_priority`）用于分析哪种话术开聊率高。
- 可选线索编号：仅在配置了 `leadEndpoint` 时生成 6 位（去掉 0/O/1/I/L 等易混字符）追加到留言末尾，客服回填开聊 / 成交用于离线转化。未配置时**不**加编号，避免客户看到无意义的编码。
- 留言里不要放 URL、不要放表情符号以外的特殊字符（`#`、`&` 会破坏参数）。

---

## 10. 附录：可直接复用的代码块

从已验证的 `index-direct.html` 抽出。标注"原样"的不要改；标注"按页面调整"的只改标注的地方。

### A. CONFIG（按页面调整：号码、接待、转化标签、回传地址）
```js
const CONFIG = {
  whatsappNumber: '85257980601',
  receptionist: 'Chloe',
  googleSendTo: 'AW-11421360067/Z7iXCK6y948dEMO_kMYq',
  fallbackDelay: 1600,
  fallbackDelayInApp: 1200,
  leadEndpoint: ''
};
```

### B. 环境判断（原样）
见 2.2。

### C. 链接构造（原样）
见 4.3。

### D. 点击处理（原样；`sendLead` / `fbq` 两段可按页面删减字段）
见 4.2。

### E. 兜底面板 HTML（按页面调整：文案）
```html
<div class="wa-fallback" id="wa-fallback" aria-hidden="true">
  <div class="gate-panel" role="dialog" aria-modal="true" aria-labelledby="wa-fallback-title">
    <div class="gate-handle"></div>
    <h3 class="gate-title" id="wa-fallback-title">未自動開啟 WhatsApp？</h3>
    <p class="gate-desc">按下面按鈕再試一次，打開後直接按「發送」即可領取名單。</p>
    <a class="cta" id="wa-fallback-open" href="#">開啟 WhatsApp 領取名單</a>
    <div class="wa-alt">
      <button type="button" id="wa-copy-number">複製 WhatsApp 號碼</button>
      <button type="button" id="wa-copy-msg">複製留言內容</button>
    </div>
    <div class="wa-number" id="wa-number-text"></div>
    <div class="wa-browser-tip" id="wa-browser-tip">如仍未能開啟：請按右上角「⋯」，選擇「在瀏覽器中開啟」，再按一次領取按鈕。</div>
    <button class="wa-close" type="button" id="wa-fallback-close">關閉</button>
  </div>
</div>
```

### F. 兜底面板 CSS（按页面调整：颜色、圆角）
```css
.gate-panel{width:100%;max-width:520px;background:linear-gradient(180deg,#fcfbf8 0%,#f6f3ed 100%);border-radius:28px 28px 0 0;box-shadow:0 -16px 42px rgba(13,23,39,.18);padding:14px 18px calc(18px + env(safe-area-inset-bottom));overflow:hidden}
.gate-handle{width:54px;height:5px;border-radius:999px;background:#d8d2c7;margin:0 auto 13px}
.gate-title{margin:14px 0 0;font-size:22px;line-height:1.32;color:#1b2943;font-weight:950}
.gate-desc{margin:7px 0 0;color:#747d8c;font-size:12px;line-height:1.55}
.wa-fallback{position:fixed;inset:0;z-index:70;display:none;align-items:flex-end;justify-content:center;background:rgba(14,22,35,.52);padding:18px 12px calc(12px + env(safe-area-inset-bottom))}
.wa-fallback.show{display:flex}
.wa-fallback .gate-panel{text-align:center}
.wa-alt{display:flex;gap:9px;margin-top:10px}
.wa-alt button{flex:1;min-height:46px;display:flex;align-items:center;justify-content:center;border:1px solid #dfdad1;background:#fff;color:#33435f;border-radius:13px;font-size:13px;font-weight:900;cursor:pointer}
.wa-number{margin-top:10px;font-size:12px;color:#5d6a7d;font-weight:800}
.wa-browser-tip{display:none;margin-top:10px;padding:9px 11px;border-radius:12px;background:#fff6e8;border:1px solid #f0ddb7;color:#76531d;font-size:11px;line-height:1.5;font-weight:800}
.wa-browser-tip.show{display:block}
.wa-close{margin-top:12px;border:0;background:none;color:#8e95a0;font-size:12px;font-weight:800;padding:8px;cursor:pointer}
```

### G. 兜底面板 JS（原样）
```js
function openInHiddenFrame(url) {
  const frame = document.createElement('iframe');
  frame.style.display = 'none';
  frame.src = url;
  document.body.appendChild(frame);
  setTimeout(() => frame.remove(), 3000);
}

const waFallback = document.getElementById('wa-fallback');
const waFallbackOpen = document.getElementById('wa-fallback-open');
const waCopyNumber = document.getElementById('wa-copy-number');
const waCopyMsg = document.getElementById('wa-copy-msg');
const waNumberText = document.getElementById('wa-number-text');
const waBrowserTip = document.getElementById('wa-browser-tip');
let waAttempt = null, fallbackTimer = 0, fallbackPayload = { phone: '', message: '' };

function prepareFallback(urls, message) {
  fallbackPayload = { phone: urls.phone, message };
  const hrefByEnv = {
    ios_browser: urls.universal,
    ios_webview: urls.scheme,
    android_browser: ANDROID_INTENT_OK ? urls.intent : urls.universal,
    android_webview: urls.scheme,
    desktop: urls.universal
  };
  waFallbackOpen.href = hrefByEnv[WA_ENV];
  waNumberText.textContent = `WhatsApp 號碼：+${urls.phone}`;
  waBrowserTip.classList.toggle('show', IOS_WEBVIEW || ANDROID_WEBVIEW);
}
function showFallback() {
  if (!waAttempt || waAttempt.left || document.hidden) return;
  waFallback.classList.add('show'); waFallback.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  track('wa_fallback_shown', { placement: waAttempt.placement });
}
function hideFallback() {
  if (!waFallback.classList.contains('show')) return;
  waFallback.classList.remove('show'); waFallback.setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
}
function armFallback(delay) { clearTimeout(fallbackTimer); fallbackTimer = setTimeout(showFallback, delay); }
function markLeft() {
  if (!waAttempt || waAttempt.left) return;
  waAttempt.left = true; clearTimeout(fallbackTimer); hideFallback();
  track('wa_app_opened', { placement: waAttempt.placement, wa_ms: Date.now() - waAttempt.t });
}
document.addEventListener('visibilitychange', () => { if (document.hidden) markLeft(); else redirecting = false; });
window.addEventListener('pagehide', () => clearTimeout(fallbackTimer));
window.addEventListener('blur', () => { if (waAttempt && !waAttempt.left) armFallback(3500); });
window.addEventListener('focus', () => { if (waAttempt && !waAttempt.left && !waFallback.classList.contains('show')) armFallback(900); });
window.addEventListener('pageshow', e => { if (e.persisted) { redirecting = false; clearTimeout(fallbackTimer); } });

function legacyCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
  document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
  let ok = false; try { ok = document.execCommand('copy'); } catch (e) {}
  ta.remove(); return ok;
}
function copyText(text, btn, okLabel) {
  const original = btn.textContent;
  const done = () => { btn.textContent = okLabel; setTimeout(() => { btn.textContent = original; }, 1800); };
  if (legacyCopy(text)) { done(); return; }
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, () => {});
}
waFallbackOpen.addEventListener('click', () => { waAttempt = { t: Date.now(), placement: 'fallback', left: false }; track('wa_fallback_click', {}); });
waCopyNumber.addEventListener('click', () => { copyText(`+${fallbackPayload.phone}`, waCopyNumber, '已複製號碼 ✓'); track('wa_copy_number', {}); });
waCopyMsg.addEventListener('click', () => { copyText(fallbackPayload.message, waCopyMsg, '已複製留言 ✓'); track('wa_copy_message', {}); });
document.getElementById('wa-fallback-close').addEventListener('click', hideFallback);
waFallback.addEventListener('click', e => { if (e.target === waFallback) hideFallback(); });

document.querySelectorAll('[data-wa]').forEach(btn => btn.addEventListener('click', e => goWhatsApp(e, btn.dataset.location)));
```

### H. 广告点击 ID 保存（原样）
```js
const CLICK_IDS = (() => {
  const KEY = 'econman_click_ids', TTL = 90 * 24 * 3600 * 1000;
  const q = new URLSearchParams(location.search);
  const fresh = {};
  ['gclid', 'gbraid', 'wbraid', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'placement', 'creative', 'device'].forEach(k => {
    const v = q.get(k); if (v) fresh[k] = v.slice(0, 300);
  });
  let stored = {};
  try { stored = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) {}
  if (!stored.ts || Date.now() - stored.ts > TTL) stored = {};
  if (fresh.gclid || fresh.gbraid || fresh.wbraid) {
    stored = Object.assign({}, fresh, { ts: Date.now() });
    try { localStorage.setItem(KEY, JSON.stringify(stored)); } catch (e) {}
  }
  return Object.assign({}, stored, fresh);
})();
```

### I. 上报队列（原样）
见 5.2。

---

## 11. 交付前自问

1. 把所有 `<script>` 删掉，点按钮还能跳到 `wa.me` 吗？
2. 点击处理里有没有任何一行在"把链接交给浏览器"之前可能抛错、等待、或发请求？
3. 五种 `WA_ENV` 各自选的链接，我能说出为什么是它而不是别的吗？
4. 用户点了按钮但 WhatsApp 没出来，1.2–1.6 秒后他看到了什么？
5. gtag 没加载完用户就走了，这次转化还能被记上吗？什么时候？
6. 业主发来的展示位置报表前 10 行，我逐行确认过它们落在哪条路径了吗？
7. 第 7 章的每一项我都真的跑过了吗？

七个问题都能肯定回答，才算完成。
