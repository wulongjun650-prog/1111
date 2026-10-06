# AB Lab 交接

给下一个窗口。先看「现在不要做的事」和「Cloudflare 加速」。用户用中文沟通，要直接回答，不要先改规则再解释。

仓库：`github.com/wulongjun650-prog/1111`。产品代码在 `outputs/ab-lab`。后台对外是 `https://hhucuq.top/`。这是付费流量用的 A/B 落地页系统（用户口头叫双子星）。

最新代码在分支 `cursor/purge-landing-cache-e8f4`（草稿 PR #29）。不要从 `main` 接着改 AB Lab，`main` 上没有这套功能。

## 现在不要做的事

- 不要再改 Cloudflare 的加速规则。VIP 缓存已经换回原样。用户要加速。切换 A/B 只清一次缓存，不要把 `cache` 改成 `false`，不要改 72000，不要改 `override_origin`。
- 不要删访问记录，不要自己点「一键清除非投放地区」，不要 `UPDATE` 已有 `events` 行，不要编造访问。
- 不要删 `/var/lib/ab-lab/backups/visits-20261006-055839/`。
- 不要覆盖 `/var/lib/ab-lab` 或 `/etc/ab-lab/config.json`。不要跑 `install.sh`，不要跑 `configure.py --set-password`。
- 不要随便重启 `ab-lab-target`。它在吐落地页，广告可能开着。改 Python 行为时只重启 `ab-lab-admin`。只改 `static/` 下的 js/css 不用重启。
- 不要 `curl` 落地页的 `GET /`。那会写一条真实访问。看响应头用 `HEAD`。
- 不要把面板密码、Cloudflare 令牌、完整手机号写进仓库或提交。
- 不要提交 `__pycache__`、`*.pyc`、`node_modules`、`package.json`、`package-lock.json`。

## 线上布局

| 用途 | 位置 |
| --- | --- |
| 代码 | `/opt/ab-lab`，虚拟环境 `/opt/ab-lab/.venv`（服务器上是 Python 3.14） |
| 配置 | `/etc/ab-lab/config.json` |
| Cloudflare 环境 | `/etc/ab-lab/cloudflare.env`（`AB_CLOUDFLARE_TOKEN`、`AB_CLOUDFLARE_TEMPLATE=kk003.vip`） |
| Google 环境 | `/etc/ab-lab/google.env` |
| 数据 | `/var/lib/ab-lab`。登记库 `registry.db`。每个域名 `sites/<32位id>/app.db`。`default` 用 `/var/lib/ab-lab/app.db` |
| 管理服务 | `ab-lab-admin`，`127.0.0.1:8765` |
| 落地页服务 | `ab-lab-target`，`127.0.0.1:8766` |
| 起源轮询 | `ab-lab-origin` |
| Nginx | `/www/server/nginx/sbin/nginx`，站点配置在 `/www/server/panel/vhost/nginx/<域名>.conf` |

源站中间件给所有响应加 `cache-control: no-store`。不经本机回环、域名不对的直连会 403。静态文件挂在 `/static`，每次请求读磁盘，换 js/css 不用重启。

管理端公开路径只有 `/login`、`/static/login.js`、`/static/app.css`。别的静态要登录。`app.js` 在管理端未登录是 401。

本机 DNS 经常解析不了公网名字。GitHub 需要 `/etc/hosts`：`140.82.112.3 github.com`、`140.82.112.5 api.github.com`。查别的域名可用 `dig +short 名字 @8.8.8.8`。

## 域名

登记库里核对过的（2026-10-06）：

| 域名 | Cloudflare |
| --- | --- |
| `nnuggc.top` | 没有 zone，不要按 CF 站点去改它 |
| `fafhkj.vip` | 已接入，规则 VIP |
| `gsjvtk.vip` | 已接入，规则 VIP |
| `dsfnqh.vip` | 已接入，规则 VIP。站点 id `80938ccca5964c3a99f105c57b62dfeb`，库在 `/var/lib/ab-lab/sites/80938ccca5964c3a99f105c57b62dfeb/app.db` |
| `hxqppy.vip` | 已接入，规则 VIP |
| `cafkjb.vip` | 已接入，规则 VIP |
| `kk003.vip` | 模板站（`AB_CLOUDFLARE_TEMPLATE`）。新域名套用规则时从这里复制。规则也是 VIP |

六个落地页 vhost 都反代 `127.0.0.1:8766`，并且有 `proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;`。`X-Real-IP` 必须仍是 `$remote_addr`（Cloudflare 边缘地址）。不要改成客户端头。

`dsfnqh.vip` 内容（核对时）：

- A 版本 `640b847a1e9246e4b30f7322887e1472`，标题「港學財訊 | 零代碼·純宏觀商業認知」，约 13888 字节，没有 WhatsApp。
- B 版本 `74a44dc12c6a4b4bbe2aa889babac3b6`，标题「講經MAN｜10月4日港股精選名單」，约 29068 字节，有 WhatsApp。
- 当时配置是 `routing=RULES`、`allowed_slot=A`、页面模式、防护开、国家列表空、严格爬虫关。用户可能已经再切过，以数据库为准，不要靠这份快照。

## Cloudflare 加速（已经换回，禁止再关）

每个已接入域名有一条缓存规则，名字 **VIP**，表达式 `(http.host eq "该域名")`，启用：

- `cache: true`
- `edge_ttl`: `override_origin`，`default: 72000`（20 小时）
- `browser_ttl`: `override_origin`，`default: 72000`
- 站点设置 `browser_cache_ttl` 仍是 `14400`，`cache_level` 仍是 `aggressive`。这两项没有改过。

源站虽然发 `no-store`，这条规则会盖掉它，浏览器看到 `cache-control: max-age=72000`，`cf-cache-status` 可以是 `HIT`。这是用户要的加速。

2026-10-06 曾经误把这些域名和模板改成 `cache: false`（描述写成「不缓存落地页」）。用户反对。已经全部改回上面的 VIP。随后 `GET https://dsfnqh.vip/` 两次都是 `HIT` + `max-age=72000`。`HEAD` 有时显示 `DYNAMIC`，不要用 HEAD 判断缓存坏了。

切换 A/B 时只做一次 `purge_everything`。代码在 `outputs/ab-lab/ablab/web.py` 的 `purge_site_cache`：保存配置（`PUT /config`）和发布版本（`POST /publish/{slot}`）时，该域名有 `cf_zone_id` 且服务器配了 Cloudflare，就清一次。换号只有内容真的变了才清。失败不影响保存，接口里带 `cloudflare: {ok: false, detail}`。前端提示「Cloudflare 缓存已清除，请刷新落地页。」

清的是 Cloudflare 边缘上的那一份。访客自己的浏览器还可能留着旧页面，最长 20 小时。用户自己要看新页面时，强制刷新一次（Ctrl+F5）。新访客在清缓存之后会拿到新页面，然后再被缓存 20 小时。

## 切换 A 仍看到 B 时怎么判断

2026-10-06 的 `dsfnqh.vip` 就是这个情况：数据库已经是 A，源站 `GET`（绕过缓存或缓存未命中）返回「港學財訊」，Cloudflare 上的首页缓存仍是 B「講經MAN」，`cf-cache-status: HIT`，`cfOrigin` 耗时 0。

首页英雄区两个按钮是 `data-allowed-slot`，点击会把 `routing` 设为 `RULES`，`allowed_slot` 设为 A 或 B。规则页还有「全部访问 A / 全部访问 B」（`FORCE_A` / `FORCE_B`），那会跳过规则。

`decide()`：强制路由优先；否则防护关或白名单用 `allowed_slot`；规则拦截展示 A；全部通过后展示 `allowed_slot`。所以 `allowed_slot=A` 时，过和不过都是 A。

## 访问记录

表 `events`：`id, created, ip, country, device, slot, reason, path, mode, device_details`。每次新事件会删掉 30 天以前的，或只留最新约 10000 条（`id <= max(id)-10000`）。所以条数到顶不一定是删库。

### 旧记录不要改

不要 `UPDATE events`。旧行里大量 IP 带 `/`（`/24` 或 `/48`），国家常被存成 `JP`。那是当时的 Cloudflare 日本边缘网段，真实访客 IP 没有存下来，不能事后编。

`shown_country(ip, country)` 只在同时满足时把显示改成 `HK`：存的国家是 `JP`，并且 `ip` 字符串里有 `/`，并且这个网段落在 `CLOUDFLARE_NETS` 里。完整地址保持原国家。`172.64.215.1` 这种完整地址即使国家是 `JP` 也显示日本。

界面上这些旧行的国家是「香港」加香港旗，IP 列是「—」。CSV 里带 `/` 的 IP 导出为空。接口 JSON 仍返回库存的 ip，给判断和测试用。

### 新记录必须是真实 IP 和该 IP 的国家

`store.event` 把 IP 规范成 `str(ipaddress.ip_address(ip))`，不再打 `/24` `/48`。国家是本地 MaxMind（`/opt/ab-lab/resources/country.mmdb`，或配置里的 `geoip_path`）对这个完整地址的查询。查不到、非全球单播、或代码是 `XX`/`ZZ` 时存空，界面显示「未知」。不要猜成香港。

`restore_visitor_ip(x-real-ip, cf-connecting-ip)`：只有 `CF-Connecting-IP` 是一个全球单播地址，并且 `X-Real-IP` 落在 Cloudflare 网段里，才用 `CF-Connecting-IP`。否则用 `X-Real-IP`。客户端自己带的 `CF-Connecting-IP` 不算。`2001:db8::1` 不是全球单播，会丢掉。IPv6 测试用 `2001:4860:4860::8888`。

计数 `count()` 本来就哈希完整 IP。

2026-10-06 用美国出口访问 `dsfnqh.vip`（缓存未命中，请求打到源站），库里的国家是 `US`，分流 `A`，原因 `allowed`。同一时段最近的香港记录，用这个 mmdb 查也是 `HK`，不是程序写成香港。国家库路径存在且可用。

## 国家列的国旗

访问日志（`static/app.js`）和总览最近访问（`static/dashboard.mjs`）用 `countryBadge`。旗在 `/static/flags/{code}.svg`（flag-icons，许可证 `static/flags-LICENSE.txt`）。香港显示红旗和「香港」，没有括号。`countryParen` 仍是「（香港）」，只给清除按钮的确认文案和 locale 测试。

样式在 `static/app.css` 末尾：国家名 13px、字重 600，中文字体栈，旗 22×16。图表里的 `.country-rank .country-label` 仍是 11px，不要被访问表的规则盖掉。

这三个静态文件已在线上。用户要看新样式需强制刷新。服务器上的 `static/flags` 不要删。打包发布时 tar 排除 `static/flags` 和 `static/*.png`，避免覆盖线上旗和图。

## 一键清除非投放地区

按钮在访问日志「查询」右边。`POST /api/logs/clear-foreign`，只删当前域名里**显示成非香港**的行。显示成香港的都留，包括旧的 Cloudflare 日本网段。确认文案仍写「显示（香港）的会留下」。

用户明确怕误删。功能可以留着，不要自己执行。审计动作 `logs_foreign_cleared`。

## WhatsApp 与 B 版本

- 页面上已经是这个号码时，再点「换成这个号码」不能再复制一版 B。`replace_bundle` 在选中位置已经是目标号码时返回 `(None, 0)`。`apply_whatsapp_number` 返回现有 B 版本且 `changed=0`。
- 点另一个号码：绿色「当前号码」要移过去，页面电话要变。
- 点的就是当前号码：前端直接返回，提示「这个号码已经是当前号码。」不发新版本。
- `apply_redirects` 在 `version is None` 时仍会更新 `redirect_active`。
- 号码真的改了并且有 `cf_zone_id` 才清 Cloudflare。
- 删除未发布 B 版本的 API 必须带站点范围。站点路径要包含 `versions` 和 `logs`。`/api/sites/{id}/...` 和 `/api/...` 都挂了。
- 扫描把 `CONFIG.whatsappNumber` 的数字标成 `whatsapp_number`。当前号码的判断：已发布版本上，某个 `whatsapp_number` 出现位置的号码（只看数字，8–15 位）等于池里的号码。

## 访问备份（只读，不要删）

`/var/lib/ab-lab/backups/visits-20261006-055839/`，权限 700。当时条数：`nnuggc.top` 29，`fafhkj.vip` 35，`hxqppy.vip` 47，`gsjvtk.vip` 24，`dsfnqh.vip` 8616，`cafkjb.vip` 1077。

精确 IP 上线后某次只读核对的条数（部署本身没删行）：`cafkjb.vip` 1585，`dsfnqh.vip` 10000，`fafhkj.vip` 43，`gsjvtk.vip` 37，`hxqppy.vip` 71，`nnuggc.top` 46，合计 11782。之后还有真实流量和测试访问，以现库为准。`dsfnqh` 到 10000 是保留上限，不是被清掉。

代码备份（不要当垃圾删）：

- `/var/backups/ab-lab-code-20261006-105915`
- `/var/backups/ab-lab-code-20261006-113326`
- `/var/backups/ab-lab-code-20261006-123806`
- Nginx：`/var/backups/ab-lab-nginx-20261006-123806`
- 后来换静态文件时还有 `/var/backups/ab-lab-static-<时间戳>/`

## 部署

面板在 `http://23.27.169.214:31429/cb40576e`，账号 `jailzdht`。密码不要写进仓库，也不要写进交接文件。默认 curl 的 User-Agent 会得到 nginx 404；浏览器 UA 才能打开登录页。登录后地址是 `/apsess_.../`。令牌在 `#request_token_head` 的 `token` 属性，请求头 `x-http-token`。

执行命令：`POST /<session>/files?action=ExecShell`，表单字段 `shell`、`path=/tmp`。读输出：`GetFileBody`，路径自定。面板会读到上一次的 `EXIT:0`。输出文件里先 `echo` 一个新 nonce，脚本最后 `echo EXIT:$?`，只接受同时含这个 nonce 和 `EXIT:` 的内容。

包装必须是分组加重定向，nonce 在第一行，`echo EXIT` 前要有换行。heredoc 后面紧跟分号，或某一行以分号开头，bash 会语法错误，输出文件不会生成。发出去之前在本地 `bash -n`。

只换静态：复制到 `/opt/ab-lab/static/`，`chmod 644`，`chown` 参照原文件，不重启。Python 变更：备份原文件，覆盖，用 `/opt/ab-lab/.venv/bin/python -m py_compile`，然后 `systemctl restart ab-lab-admin`。不要重启 `ab-lab-target`，除非用户明确允许并且广告已停。不要 `rm -rf /opt/ab-lab/static`。

本机 Playwright 用 `createRequire('/workspace/outputs/ab-lab/package.json')('playwright')`。从 `/tmp` 当 ESM 导入会失败。

## Git 栈

分支名 `cursor/<描述>-e8f4`，全小写。PR 用仓库的 PR 工具，草稿。AB Lab 是一条叠在 `import/ab-lab-full` 上的链，不要把这些 PR 的基改成 `main`。

从新到旧：

| PR | 头分支 | 基分支 | 内容 |
| --- | --- | --- | --- |
| #29 | `cursor/purge-landing-cache-e8f4` | #28 的分支 | 保存配置或发布时清该域名 CF 缓存。本文档也在这条分支上 |
| #28 | `cursor/visit-country-flags-e8f4` | #27 | 国家列国旗和更大的中文 |
| #27 | `cursor/exact-visit-ip-e8f4` | #26 | 新访问记完整 IP 和该 IP 的国家；nginx 转发 `CF-Connecting-IP` |
| #26 | `cursor/clear-non-hk-visits-e8f4` | #25 | 一键清除非投放地区 |
| #25 | `cursor/move-current-number-e8f4` | #24 | 换号时移动「当前号码」 |
| #24 | `cursor/stop-duplicate-b-versions-e8f4` | #23 | 号码已在页面上时不再复制 B |
| #23 | `cursor/scoped-version-delete-e8f4` | #22 | 删除 B 版本按当前域名 |
| #22 | `cursor/real-visitor-country-e8f4` | #21 | 访客国家；旧的日本边缘网段显示成香港 |
| #21 | `cursor/ga4-send-visits-e8f4` | #20 | 已发布页把访问发给 GA4 |
| #20 | `cursor/visit-rate-alarm-e8f4` | #19 | 按域名监控流速 |
| #19 | `cursor/ga4-conversion-codes-e8f4` | #18 | GA4 与转化代码写入 A 或 B |
| #18 | `cursor/ios-log-strict-bots-e8f4` | #17 | 访问日志里的 iOS 18+，严格爬虫 |
| #17 | `cursor/cloudflare-strict-https-e8f4` | #16 | Cloudflare 回源用严格 HTTPS |
| #16 | `cursor/disable-cloudflare-e8f4` | #15 | 关闭 Cloudflare 代理（灰云）的按钮 |
| #15 | `cursor/optional-cloudflare-e8f4` | #14 | 先建站和证书，再可选套 Cloudflare |
| #14 | `cursor/detect-built-jumps-e8f4` | #13 | 扫描落地页跳转 |
| #13 | `cursor/content-page-ui-e8f4` | #12 | 内容页布局 |
| #12 | `cursor/b-split-e8f4` | #11 | B 页分流：随机或概率，同一访客固定一条 |
| #11 | `cursor/delete-b-versions-e8f4` | `import/ab-lab-full` | 删除未发布的 B 版本 |
| #10 | `cursor/b-redirect-active-marker-e8f4` | `import/ab-lab-full` | 标记当前 B 页跳转。和 #11 平行，不要把它当成 #11 的父提交 |

`#1` 到 `#9` 是别的草稿，和这套 AB Lab 无关。不要去改。

本地测试工作目录是 `outputs/ab-lab`。解释器是 `python3`，不一定有 `python`。和这次行为相关的测试：

- `python3 -m pytest tests/test_visitor_details.py tests/test_cloudflare.py::test_saving_the_visible_slot_purges_that_domains_cache tests/test_http.py -q`
- `node --test tests/logs-clear.browser.mjs`（在 `outputs/ab-lab` 里，Playwright 的 Chromium 可用）

`logs-clear` 浏览器测试要等 `#logs-body` 里任意一面旗的 `src` 等于 `/static/flags/us.svg`，不要只用 `querySelector` 的第一面旗（第一面可能是香港）。

## 用户已经确认过的口径

- 换号不要无故产生新 B 版本。换到另一个号要立刻移动「当前号码」。
- 旧访问保持原样。从精确 IP 上线之后的新访问，IP 和国家都要是那一次连接的真实值。
- 国旗加中文名，字重要看清楚。香港写「香港」，不要括号。确认框里的「（香港）」别改。
- 加速规则保持 VIP / 20 小时。切换 A 和 B 时清一次 Cloudflare 缓存。不要再关缓存。
- 清缓存解决的是边缘上的旧页面。用户自己的浏览器还要强制刷新一次才能丢掉本地的旧页。
- 回答用中文，先回答问题，再改东西。不要先改 Cloudflare 规则再解释。
