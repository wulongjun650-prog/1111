# 双子星交接（2026-10-17）

给下一个代理。用户用中文，先回答再改。仓库 `github.com/wulongjun650-prog/1111`，产品在 `outputs/ab-lab`。后台 `https://hhucuq.top/`。这是付费流量用的 A/B 落地页，用户口头叫双子星。

这份文档覆盖到提交 `779134f`（2026-10-16 13:31:32 UTC，`Add per-domain Cloudflare checks for page entry and button clicks.`）。安装包由 `outputs/ab-lab/deploy/pack-release.sh` 打出，根目录有一份同文的 `交接.md`。包里没有数据库、没有令牌、没有 `__pycache__`。

当前工作分支是 `cursor/cf-human-check-a152`，草稿 PR #86，基线 `cursor/wa-trust-watch-a152`。不要从 `main` 接着改，`main` 上没有这套功能。新分支名用 `cursor/<英文小写描述>-a152`。PR 只用仓库的 PR 工具，默认草稿。

## 现在不要做的事

- 不要在这台已经装好的机器上跑 `deploy/install.sh`。它看到 `/opt/ab-lab`、`/var/lib/ab-lab` 或 `ab-lab` 用户就会停，而且它是首次安装，会拒绝覆盖。不要跑 `configure.py --set-password`。
- 不要覆盖 `/var/lib/ab-lab` 或 `/etc/ab-lab/config.json`。不要删 `/var/lib/ab-lab/backups/visits-20261006-055839/`。代码备份在 `/var/backups/ab-lab-code-20261006-*`，Nginx 备份在 `/var/backups/ab-lab-nginx-20261006-123806`，都不要当垃圾删。
- 不要重启 `ab-lab-target`。它从 `Sun 2026-10-11 12:16:36 UTC` 起一直在吐落地页。广告可能开着。改 Python 只重启 `ab-lab-admin`。只改 `static/` 的 js/css 不用重启。访客进程读的是磁盘上的页面文件，但进程里的 Python 仍是 10 月 11 日那一版，见下文「两个进程不是同一份代码」。
- 不要 `curl` 落地页的 `GET /`。那会写一条真实访问。看响应头用 `HEAD`。`HEAD` 有时是 `DYNAMIC`，不要用它判断缓存坏了。
- 不要删访问记录，不要自己点「一键清除非投放地区」，不要 `UPDATE` 已有 `events` 行，不要编造访问。
- 不要把 Cloudflare 缓存规则改成 `cache: false`。不要改边缘 TTL 的 `72000` 和 `override_origin`。五个已接入域名现在都是：规则名 VIP，`cache: true`，`edge_ttl` 为 `override_origin` / `72000`，`browser_ttl` 为 `respect_origin`，站点设置 `browser_cache_ttl` 为 `0`。这是 2026-10-17 只读核对的结果，五个域名一致，不是某一台被单独改坏。不要自作主张改回浏览器 72000，也不要再关缓存。
- 不要在部署或验证时打开任何人机验证开关。`cf_challenge` 现在全是 0。尤其不要动已经不在登记库里的 `xjtbjr.vip`，不要把它建回来。
- 不要恢复已经从登记库消失的旧域名：`fafhkj.vip`、`gsjvtk.vip`、`dsfnqh.vip`、`cafkjb.vip`。2026-10-06 的旧交接还列着它们，以现在的 `registry.db` 为准。
- 不要给 `nnuggc.top` 套 Cloudflare。它是 `target_origin`，登记 id 是 `default`，阶段 `legacy`，没有 zone。
- 不要把令牌、面板密码、完整手机号打进日志或新的提交说明。Cloudflare 令牌只在服务器 `/etc/ab-lab/cloudflare.env`。面板账号留在下面「部署」那一句，`/tmp/panel_client.py` 用正则从这一句读，句子格式不能改。
- 不要提交 `__pycache__`、`*.pyc`、`.pytest_cache`、`node_modules`。

## 线上布局

| 用途 | 位置 |
| --- | --- |
| 代码 | `/opt/ab-lab`，虚拟环境 `/opt/ab-lab/.venv`，服务器 Python 3.14.4。本机开发常常是 3.12，用 `python3`，PATH 里不一定有 `python` |
| 配置 | `/etc/ab-lab/config.json`。键只有 `admin_origin`、`target_origin`、`data_dir`、`geoip_path`、`server_ip`。没有 `registry_dir`，登记库就在数据目录 |
| 后台 / 访客源 | `https://hhucuq.top` / `https://nnuggc.top`。公网 IP `23.27.169.214` |
| Cloudflare | `/etc/ab-lab/cloudflare.env`，变量 `AB_CLOUDFLARE_TOKEN`、`AB_CLOUDFLARE_TEMPLATE=kk003.vip`。模板站用来复制缓存和优化规则 |
| Google / VMOS | `/etc/ab-lab/google.env`、`/etc/ab-lab/vmos.env`。三个文件都挂在 `ab-lab-admin` 的 `EnvironmentFiles` 上 |
| 数据 | `/var/lib/ab-lab`。登记库 `registry.db`。总库和原有站点 `/var/lib/ab-lab/app.db`。每个自带域名 `sites/<32位id>/app.db` 加 `pages/` |
| 管理服务 | `ab-lab-admin`，`127.0.0.1:8765`。本次人机验证重启后的启动时间是 `Fri 2026-10-16 13:32:50 UTC` |
| 落地页服务 | `ab-lab-target`，`127.0.0.1:8766`。启动时间必须仍是 `Sun 2026-10-11 12:16:36 UTC` |
| 起源轮询 | `ab-lab-origin` 在跑，启动时间 `Tue 2026-10-13 13:23:16 UTC`。它负责直连 DNS 的建站和证书。`ab-lab-dns`、`ab-lab-provision` 当前没有出现在 `systemctl list-units 'ab-lab*'` 里 |
| Nginx | `/www/server/nginx/sbin/nginx`，站点配置在 `/www/server/panel/vhost/nginx/<域名>.conf` |
| 国家库 | `/opt/ab-lab/resources/country.mmdb`（DB-IP Country Lite）。配置里的 `geoip_path` 为空时用这个 |

源站中间件给响应加 `cache-control: no-store`。VIP 的边缘规则会盖住它，边缘可以 `HIT`，`max-age` 来自边缘 TTL 72000。浏览器 TTL 已是 `respect_origin`，浏览器会看到源站的 `no-store`。

直连 `http://127.0.0.1:8765/login` 返回 403 是预期。公网模式还要 `X-Real-IP` 或可接受的 `CF-Connecting-IP`，以及 `X-Forwarded-Proto: https`，Host 必须是 `hhucuq.top`。从服务器 `curl https://hhucuq.top/login` 应是 200，页面里有「双子星」。未登录拿 `/static/app.js` 是 401。管理端公开路径只有 `/login`、`/static/login.js`、`/static/app.css`。

本机 DNS 经常解析不了公网名字。GitHub 需要 `/etc/hosts`：`140.82.112.3 github.com`、`140.82.112.5 api.github.com`。查别的域名用 `dig +short 名字 @8.8.8.8`。

## 两个进程不是同一份代码

磁盘上的 `/opt/ab-lab` 已经是 `779134f`。2026-10-17 核对过这六个文件的 sha256，服务器和这份源码一致：

| 文件 | sha256 |
| --- | --- |
| `ablab/human_check.py` | `8705379f1e998326a82974a4b736b894525a0d6b2318e4265c859c2757d32ac3` |
| `ablab/web.py` | `6a6be961e72ee5c158bcfdd4e7ff9508c8a93a1892b0e5319ac5bfe9b141f3c7` |
| `ablab/cloudflare.py` | `74b5039b2edead0a6328992353f6cc3ea8fb82a97a4763fa4a1b253443e0246d` |
| `ablab/sites.py` | `86d0a17fb64dee31c6f8fcc2f7d74aacf78d6f5167e93c13552fcc3c9ad68ea7` |
| `static/app.js` | `c30c97ed5015cd46ea58b8cf17343de17a7d03b2d503c2097fb280627731d6bb` |
| `static/dashboard.css` | `70e6a5f740dcc5e1294c2bc6ac7edc12b16b64622c935b9ea31558f499edab78` |

`ab-lab-admin` 在 10 月 16 日重启过，管理端跑的是这份新代码。`ab-lab-target` 没有重启，访客请求走的 Python 仍是 10 月 11 日 12:16 载入的那一版。Python 不会按磁盘重新 import。

所以：

- 写进已发布 HTML 的内容，目标进程每次 `read_bytes`，立刻生效。人机验证的点击脚本、换号改过的页面、重新发布的 A/B，都属这一类。
- 只存在于 `web.py` 请求路径里的新行为（新的 CSP、新的注入、新的 beacon 分支）在重启 target 之前不会作用到访客。10 月 11 日 12:16 之后的提交，凡是要靠 target 进程里的 Python 才生效的，现在都还没进访客进程。
- 登记库多出来的列（`cf_challenge`、`cf_challenge_level`）是 admin 启动时 `ALTER` 加上的，默认 0。旧的 target 用 `SELECT *` 再转 dict，多出来的列不会把旧进程打崩。不要为了「对齐代码」去重启 target。

用户明确允许并且广告已停之前，不要重启 target。重启之后必须把新的 `ActiveEnterTimestamp` 写进这份交接，并给受影响的落地页各清一次该域名自己的缓存。VIP 的边缘 72000 不要动。

## 现在的域名

2026-10-17 从 `registry.db` 读到的全部站点：

| 域名 | 阶段 | Cloudflare | 人机验证 | 观察 |
| --- | --- | --- | --- | --- |
| `fctjgf.vip` | active | 有效 | 关 | 否 |
| `gvwtyr.vip` | active | 有效 | 关 | 被观察的投放域名。id `32c37c116b9c425983de17d9194b9c80` |
| `hgkuyy.vip` | active | 有效 | 关 | 否。id `8a84a578e32d467594688c186081dc9d` |
| `hxqppy.vip` | active | 有效 | 关 | 观察域名，读取 `gvwtyr.vip`。id `09c343cdc9564eb3aaf34aedacd37213` |
| `nhuttb.vip` | active | 有效 | 关 | 否 |
| `nnuggc.top` | legacy | 没有 | 关 | 否。这是原有站点，id `default` |

总管理员账号用户名是 `admin888`，账号 id 是 `admin`。截图里的「归属：admin888」就是总管理员本人，不是另一个代理。观察号用户名是 `guancha`，id `c0fbe3810feb49cd97f9ed02b7cb86c0`，一个观察号只能绑一个域名。

`hxqppy.vip` 在 2026-10-16 被设成观察域名，跟着 `gvwtyr.vip`。原先承担观察的是 `nnuggc.top`，同一个观察号挪走后，`nnuggc.top` 的 `mirror_source` 和 `observer_account` 被清空，页面文件没有改回去。`hgkuyy.vip` 在 10 月 13 日当过观察域名，后来观察号挪到 `nnuggc.top` 时已经被清掉，没有单独写「已取消观察」事件。取消观察只改这两列，不换已发布的 A/B。

`hgkuyy.vip` 的坑：取消观察之后用户仍看到 B 页。原因是 A 槽当时发布的就是 B 页那一份 `10.15.zip`（标题「講經MAN｜10月15日港股名單｜免費領取」，`directMode` 为 `ABC`）。规则是 `routing=RULES`、`allowed_slot=A`、防护开、国家列表空，过和不过都进 A 槽，所以看到的是那份落地。2026-10-17 已把 A 槽改回 `40.14白.zip`（版本 id 前缀 `77a2e640`，没有 `directMode`）。B 槽仍是 `10.15.zip`（前缀 `8cc1d276`），没有删。改完后只清了 `hgkuyy.vip` 自己的 Cloudflare 缓存。用户自己的浏览器还要强制刷新一次。

`hxqppy.vip` 设为观察时跑过 `sync_mirror_content`：A/B、号码池、跳转会从 `gvwtyr.vip` 拷过去，HTML 里的 `directMode` 会被清空以停在问卷。拷贝会删掉观察域名自己的号码池再写入来源的号码，不删访问记录。当时也清过 `hxqppy.vip` 的缓存。

## 人机验证（已上线，全部关闭）

域名管理里，已套 Cloudflare、不是原有站点、不是观察号登录时，管理旁边有一组「人机验证」，两个开关：

- 进入落地：访客一打开页面就看到 Cloudflare 的人机验证。广告点击进来也会先过这一关。生产令牌写不了自定义防火墙，这条实际是把该 zone 的 `security_level` 设为 `under_attack`，并记住原来的级别（`medium` 这一类）。关掉时改回记住的级别。不要把 `under_attack` 存成恢复值。
- 点击按钮：不改整个 zone 的安全级别。只加一条页面规则，URL 模式 `*{域名}/*ab_human=1*`，动作 `security_level=under_attack`，优先级 1。同时把 `<script id="ab-human-click">` 写进当前已发布的 HTML。访客点 `a` 或 `button` 时先去 `?ab_human=1`，通过验证后再回到原地址并继续原来的链接或按钮。`#` 和 `javascript:` 链接不拦截。

两个可以一起开。`sites.cf_challenge`：0 关，1 只有进入落地，2 只有点击按钮，3 两个都开。`cf_challenge_level` 只在进入落地打开时保存先前的安全级别。接口 `POST /api/sites/{id}/cloudflare/challenge`，正文严格为 `{entry: bool, button: bool}`。观察号会被拒。没有 zone 的域名没有这两个开关。

点击脚本在 `ablab/human_check.py`。目标进程从磁盘读已发布文件，所以开关不用重启 target。第二次打开同一状态不会再写文件、也不会再 `POST` 页面规则。写过文件才 `purge`。`after_site_change` 会按当前按钮开关重新盖章，盖章失败只记异常类型，不挡住发布。

令牌能力（在 `fctjgf.vip` 上只读或用一条不会命中真实流量的页面规则试过，试完已删，`PROBE_RULE_LEFT 0`）：

- 可以：读写 `settings/security_level`，读写删页面规则。
- 不可以：`http_request_firewall_custom` 返回 403，错误体只有 `message`，没有 `code`。旧的 `/firewall/rules` 和 Turnstile `challenges/widgets` 是 403，code `10000`。
- `AUTH_CODES` 含 6003、9106、9109、10000。HTTP 401/403 也算拒绝，即使 code 是 0 或空。`soft=True` 时，403 且 code 0 仍返回 `None`，不要把这种软失败当成成功。
- 进入落地因此永远走 `under_attack`，不是 WAF 的 `managed_challenge`。代码仍会先试防火墙，403 再回退。
- 免费版页面规则大约 3 条。我们的预检只在其他规则不少于 50 条时提前失败；真满了会从接口报错。不要为了腾位置去删别人的规则。
- 没有在任何一个线上域名真正打开过这两个开关，所以没有人看到过线上的 Cloudflare 验证页。不要为了验收去打开。

`purge()` 还会调用 `_limit_browser_cache`：站点 `browser_cache_ttl` 大于 3600 就改成 0，缓存规则里 `browser_ttl` 模式是 `override_origin` 且秒数大于 3600 就改成 `respect_origin`。边缘 TTL 不动。现在五个域名已经是这个形状，再 purge 一次不应再改 VIP。尽管如此，不要为了试开关去 purge 别的域名。

## 访问规则和 A/B

- A 是拦截页，B 是放行后可以选的页。默认放行展示 B。`FORCE_A` / `FORCE_B` 跳过规则。`allowed_slot=A` 时，拦截和放行都是 A。
- `decide()`：强制路由优先；防护关则全部走 `allowed_slot`；黑名单到 A；白名单走 `allowed_slot`；然后爬虫、IPv4、设备、系统版本、网段、国家、语言、次数。国家列表为空表示不按国家拦。
- 页面里的 `directMode`：`ABC` 跳过弹层直接走，`ABCABC` 是第三档，`CBA` 或空值把问卷选择留在页上。观察域名同步会把拷过去的 HTML 里的 `directMode` 清空。取消观察不会把模式改回来。
- 「换成这个号码」时，页面上已经是这个号码就不要再复制一版 B。`replace_bundle` 在选中位置已经是目标号码时返回 `(None, 0)`。换到另一个号要立刻把绿色「当前号码」移过去。号码真的改了并且有 zone 才清缓存。
- 落地页 CSP 的 sandbox 必须留着 `allow-popups`、`allow-popups-to-escape-sandbox`、`allow-top-navigation-to-custom-protocols`。桌面新落地用 `window.open` 打开 `wa.me`，手机把地址设成 `whatsapp://`。不要另加不加限定的 `allow-top-navigation`。改 CSP 要重启 target 才生效，并且每个已接入域名都要清一次缓存，否则边缘还会把旧 CSP 留最多 20 小时。

## 访问记录

表 `events`：`id, created, ip, country, device, slot, reason, path, mode, device_details`。新事件会删掉 30 天以前的，或只留最新约 10000 条。条数到顶不是删库。

不要 `UPDATE events`。旧行里大量 IP 带 `/`，国家常被存成 `JP`，那是当时的 Cloudflare 日本边缘，不能事后编。`shown_country` 只在国家是 `JP`、IP 字符串里有 `/`、并且网段落在 `CLOUDFLARE_NETS` 时把显示改成香港。完整地址保持原国家。界面上这些旧行国家是「香港」加香港旗，IP 列是「—」。CSV 里带 `/` 的 IP 导出为空。

新记录必须是完整 IP 和这个 IP 在本地 mmdb 里的国家。查不到、非全球单播、或 `XX`/`ZZ` 就存空，界面显示「未知」。不要猜成香港。

`restore_visitor_ip`：只有 `CF-Connecting-IP` 是全球单播，并且 `X-Real-IP` 落在 Cloudflare 网段，才用 `CF-Connecting-IP`。客户端自己带的 `CF-Connecting-IP` 不算。Nginx 必须继续 `proxy_set_header X-Real-IP $remote_addr` 和 `proxy_set_header CF-Connecting-IP $http_cf_connecting_ip`。不要改成客户端自己填的头。

导出是所选时间窗口里的全部记录，不是屏幕上那 200 条预览。

「一键清除非投放地区」只删当前域名里显示成非香港的行。不要自己点。

国旗在 `/static/flags/{code}.svg`。香港显示红旗和「香港」，没有括号。确认框里的「（香港）」别改。打包更新到现有服务器时不要覆盖整个 `static/flags` 和 `static/*.png`，除非这次改动就是这些文件。

## 观察域名还会碰到的行为

- 设为观察：`PUT /api/sites/{观察域名id}/mirror`，正文同时给 `source_id` 和 `observer_id`，或者两个都空。只填一个会被拒。观察域名不能观察自己，投放域名不能是另一个观察域名。同一个观察号原先绑的域名会被清掉，那一次不一定写「已取消观察」事件。
- 设置更寻：在投放域名上把现有观察域名改成跟着它。访问记录留在原地。没有观察域名时会失败。
- 导入记录：把某个域名的访问记录拷到观察域名，再导一次会再叠一份。不改设置更寻。
- 删除投放域名时可以询问把访问记录留在观察域名。观察域名自己的记录不能再「保留到观察域名」。
- 观察号只能看绑给他的那一个域名，不能改人机验证，不能在观察域名上清空投放域名的记录。
- 同步失败时绑定仍然在，页面可能没拷完。不要为了重试去删记录。

## 代码地图

都在 `outputs/ab-lab/ablab/`。

| 文件 | 作用 |
| --- | --- |
| `web.py` | 管理端和访客端。`create_admin` / `create_target`。人机验证接口、观察绑定、发布后清缓存都在这里 |
| `sites.py` | `registry.db`。域名、Cloudflare 列、`cf_challenge`、观察绑定 |
| `store.py` | 每个域名自己的 `app.db`：配置、A/B 版本、号码、访问事件 |
| `cloudflare.py` | 套用、灰云、清缓存、人机验证。令牌不出服务器 |
| `human_check.py` | 点击按钮时写进已发布 HTML 的那一段脚本 |
| `rules.py` | `decide()`。UA 只是声明，不是身份 |
| `redirects.py` | 静态扫描跳转和 `directMode`。不执行页面脚本 |
| `mirror.py` | 把投放页拷到观察域名，并清空 `directMode` |
| `preserve.py` | 删域名时把访问行留在观察域名 |
| `watrust.py` | VMOS 云手机上看 WhatsApp 发送页，自动换号。管理端关掉页面后也要继续跑 |
| `page_implant.py` | 上传 B 页时补点击和唤醒 beacon，不动谷歌代码 |
| `page_title.py` | 只改 GA4 用来当网页标题的 `<title>` |
| `contact_config.py` | 缺的 `CONFIG.contactApp` 和对应链接才写，已有的键不动 |
| `tracking.py` | 保存并写入谷歌代码 |
| `campaign.py` / `flow.py` | 投放表和漏斗，只读，不改页面 |
| `analytics.py` | 后台统计 |
| `crawlers.py` | 官方爬虫网段。超级防爬虫开关针对谷歌官方身份，不要改成只挡别人、放行谷歌 |
| `devices.py` | 只用于显示的 UA 解析 |
| `auth.py` | 管理员、代理、观察号。scrypt，8 小时会话 |
| `origin_setup.py` | `ab-lab-origin` 用的建站和证书 |
| `panel_sites.py` / `managed_panel.py` / `nginx_entry.py` | 宝塔建站和 Nginx 入口。写操作有归属核对 |
| `provisioning.py` 及 `certificate_*.py` / `certbot_*.py` / `production_renewal.py` | 证书和接入。`deploy/PROVISIONING-PROGRESS.md` 停在 2026-09-21，当时写自动写入未完成。现在 `ab-lab-origin` 已经在跑，不要按那份旧进度把 origin 停掉，也不要把它当成还能随便重做的草稿 |
| `reputation.py` | 谷歌风险检测 |
| `speedcheck.py` | PageSpeed 和地区探测。Chrome / Lighthouse 在管理服务器的路径上找 |
| `site_erasure.py` | 永久删除一个已登记域名的本产品文件 |
| `archives.py` | ZIP 解压限额：20MiB、500 文件、解压 100MiB、单文件 20MiB |
| `settings.py` | 后台域名和访客域名 |
| `desk.py` / `desk_window.py` / `linkcheck.py` | 号码工作台和链接检查 |

静态界面主要是 `static/app.js`、`static/dashboard.css`、`static/b-redirects.mjs`、`templates/index.html`。管理端所有响应都是 `no-store`，用户刷新就能拿到新的 `app.js`。

`run.py` 的服务：`admin`、`target`、`dns`、`origin`、`provision`、`renew`。生产启动必须带 `--config` 或 provision/renew 的 `--private-config`。不要用本地免登录的 `python3 run.py` 代替 systemd。

## 部署

面板在 `http://23.27.169.214:31429/cb40576e`，账号 `jailzdht`，密码 `c0444c8b`。默认 curl 的 User-Agent 会得到 nginx 404；浏览器 UA 才能打开登录页。登录后地址是 `/apsess_.../`。令牌在 `#request_token_head` 的 `token` 属性，请求头 `x-http-token`。

执行命令：`POST /<session>/files?action=ExecShell`，表单字段 `shell`、`path=/tmp`。读输出：`GetFileBody`。面板输出里会混着上一次的 `EXIT:0`。先 `echo` 一个新 nonce，脚本最后 `echo EXIT:$?`，只接受同时含这个 nonce 和 `EXIT:` 的内容。本机封装在 `/tmp/deploy_scope.py` 的 `run()`：命令里不能有 `{`、`}` 和换行。安装脚本和 tar 要先 base64，再在服务器上解码。`wait_output` 只打印最后大约 3000 字，完整正文在服务器 `/tmp/ab-panel-scope.txt`。

heredoc 后面紧跟分号，或某一行以分号开头，bash 会语法错误，输出文件不会生成。发出去之前在本地 `bash -n`。

更新现有服务器的顺序：

1. 先在服务器上对将要覆盖的文件做 sha256。和这份交接或当前提交不一致就停，不要覆盖。
2. 备份到 `/tmp/ab-bak-*`，不要备份进网站目录。
3. 解开新文件到 `/opt/ab-lab`，`chown` / `chmod` 参照原来的 `ablab/web.py`。
4. `/opt/ab-lab/.venv/bin/python -m py_compile`。失败就把备份拷回去，不要重启。
5. 只有 Python 变更才 `systemctl restart ab-lab-admin`。确认 `ab-lab-target` 的 `ActiveEnterTimestamp` 仍是 `Sun 2026-10-11 12:16:36 UTC`，两个需要的单元仍是 active。
6. 用 `https://hhucuq.top/login` 看 200。不要用裸的 `127.0.0.1:8765/login` 判断服务挂了，那个 403 是边界。
7. 只读确认没有域名的 `cf_challenge` 被打开，没有人机验证页面规则残留。

`/tmp/panel_client.py` 和 `/tmp/deploy_scope.py` 在代理机器的 `/tmp`，不在产品仓库里。不要把它们提交进 git。

只换静态：复制到 `/opt/ab-lab/static/`，权限参照原文件，不重启。不要 `rm -rf /opt/ab-lab/static`。

## 安装包

在仓库根目录：

```bash
bash outputs/ab-lab/deploy/pack-release.sh
```

默认写出 `outputs/ab-lab/deploy/dist/ab-lab-<UTC日期>.tar.gz`。包内是 `ab-lab-<日期>/ab-lab/`（当前源码，不含缓存和 data）和 `ab-lab-<日期>/交接.md`，另有 `SHA256SUMS`。`resources/country.mmdb` 要留在包里，大约 8MB。

这份包是源码包，不是给现有服务器一键覆盖的升级器。新机器首次安装才读 `deploy/BAOTA.md` 并执行 `sudo bash deploy/install.sh`。那份说明里「尚未在你的服务器部署」只对一台全新机器成立。这台 `23.27.169.214` 已经在跑，用上面的更新顺序。

`deploy/PROVISIONING-PROGRESS.md`、`UPDATE-*.md`、`VERIFICATION*.md` 是当时的进度，和 2026-10-17 的线上不一致时，以这份交接和 `registry.db` 为准。

## 测试

在 `outputs/ab-lab`：

```bash
python3 -m pytest -q
node --test tests/ui.test.mjs
node --check static/app.js
```

和人机验证直接相关的是 `python3 -m pytest tests/test_cloudflare.py -q`，2026-10-16 是 32 项通过。依赖会打出 Starlette / anyio 的弃用警告，不是用例失败。

本机管理页可以用 `tests/test_cloudflare.py` 里的 `Api` 做假 Cloudflare，`firewall_forbidden=True` 才会走生产那条 `under_attack` 回退。Playwright 从 `/tmp` 用 `.mjs` 会报 `require is not defined`，用 `.cjs`。管理端 CSP 会挡住 `page.wait_for_function` 里的字符串脚本。无头 Chrome 用 `/usr/bin/google-chrome` 和 `--no-sandbox`。

## 用户已经确认过的口径

- 换号不要无故产生新 B 版本。换到另一个号要立刻移动「当前号码」。
- 旧访问保持原样。精确 IP 上线之后的新访问，IP 和国家都要是那一次连接的真实值。
- 国旗加中文名。香港写「香港」，不要括号。确认框里的「（香港）」别改。
- 边缘缓存保持 VIP、`cache: true`、72000、`override_origin`。不要再关缓存。浏览器 TTL 现在是 `respect_origin`，不要顺手改。
- 清缓存解决的是边缘上的旧页面。用户自己的浏览器还要强制刷新一次。
- 人机验证两个开关都要有，默认关。进入落地是打开页面就验证；点击按钮是页面先打开，点了才验证，通过后继续原来的点击。
- 取消观察不会自动把页面换回 A。要换 A 页就发布 A 槽里真正的白页版本。
- 回答用中文，先回答问题，再改东西。
