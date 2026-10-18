# 人机验证开关更新

这份说明只给人机验证的四个开关。旧宝塔上的其它功能不要动。不要用新仓库整份覆盖 `web.py`、`cloudflare.py`、`sites.py`、`app.js`。旧文件里已经有的函数，只按下面的锚点补差异。

同目录的 `human_check.py` 是新文件，整份复制到旧程序的 `ablab/human_check.py`。它的 sha256 是 `e3b786426c38270e1ba907eca15501c9c707290b28bdf4cded1680bfd34feb67`。复制后核对这个值，对不上就停。

## 四个开关

登记在 `sites.cf_challenge`，按位叠加，默认 0，部署时不要给任何域名打开。

| 位 | 开关 | 访客看到的 |
| --- | --- | --- |
| 1 | 进入落地 | 一进页面先过 Cloudflare 人机验证 |
| 2 | 点击按钮 | 一点按钮先过 Cloudflare 人机验证，通过后才继续 |
| 4 | 浏览时加载 | 页面先完整打开，人机验证排在正文最下方，自己完成，不挡内容，不用点 |
| 8 | 等待人机点击 | 页面先打开。验证还没通过时，点按钮或链接不会跳走，只提示「请稍等，正在验证是否是真人」。通过后按钮恢复 |

「浏览时加载」和「等待人机点击」共用页面底部同一个验证组件。只开「等待人机点击」时，组件也要出现，否则验证永远完不成，按钮会一直被拦住。

页面速度：正文先出来。验证逻辑放在页面最后的 `ab-human-browse.js`，脚本带 `defer`。Cloudflare 的 `api.js` 用 `async` 再加载。提示条只在有人提前点按钮时才创建。不要把验证脚本写进页头，不要用会挡住第一屏的浮层。

## 不要做的事

- 不要重启 `ab-lab-target`。访客进程每次读页面文件，改完文件即可。`ActiveEnterTimestamp` 必须仍是原来的值。
- 只重启 `ab-lab-admin`。
- 不要改缓存规则，不要改 `browser_cache_ttl`，不要调用会顺手改缓存时间的清理函数。页面内容变了，只对这一条域名发 `POST /zones/{zone_id}/purge_cache`，正文 `{"purge_everything": true}`。
- 部署过程中不要替客户把开关打开。
- 不要打印或提交 Cloudflare 令牌。
- 验证源码不要写进落地页 HTML。源码只放在旁边的 `ab-human-browse.js`。HTML 里只留样式、空的 `<div id="ab-human-browse">` 和 `<script src="ab-human-browse.js" defer>`。

## 令牌

「浏览时加载」和「等待人机点击」会调用 `POST/PUT /accounts/{account_id}/challenges/widgets`。现有令牌如果只有区域权限，这里会 403。要在同一把令牌上加：账户 → Turnstile → 编辑。令牌内容不用换。没加之前，这两个开关会保存失败，提示密钥还不能添加页面里的人机验证。另外两个开关不依赖这项权限。

组件固定为 `mode: non-interactive`、`clearance_level: jschallenge`，名字 `双子星浏览时加载`。这样手机上不会再弹出要手点的勾选框。

## 1. 放入新文件

把本目录的 `human_check.py` 复制为旧程序的 `ablab/human_check.py`。所有者、权限跟同目录的 `ablab/web.py` 一样。

这个文件负责：

- 位 2：在已发布的 HTML 末尾写入或去掉 `<script id="ab-human-click">`。
- 位 4 或位 8：在 HTML 末尾写入底部容器，并在同一目录写 `ab-human-browse.js`。
- 位 8：脚本里拦住 `a` 和 `button` 的点击，提示「请稍等，正在验证是否是真人」；Turnstile 回调之后放行，并让 `window.abHumanPending` 变成 false。位 2 的点击脚本如果先注册，看到 `window.abHumanPending` 为真就直接返回，把这一下交给等待逻辑。

`stamp_published(store, button=False, browse=False, sitekey='', wait=False)` 只改当前槽位里已经发布的 `.html` / `.htm`。`browse or wait` 为真才写组件。关掉时删掉带 `/* ab-human-browse */` 开头的那个 js，不要删客户自己的同名文件。

## 2. 改 `ablab/sites.py`

在建表迁移里，这三列没有就加上，已有就跳过：

```python
('cf_challenge', 'INTEGER NOT NULL DEFAULT 0'),
('cf_challenge_level', "TEXT NOT NULL DEFAULT ''"),
('cf_turnstile_sitekey', "TEXT NOT NULL DEFAULT ''"),
```

关闭 Cloudflare 时把域名清回普通记录的那几条 `UPDATE sites`，这三列也要清空：`cf_challenge=0`，`cf_challenge_level=''`，`cf_turnstile_sitekey=''`。不要改别的列。

`set_challenge` 接受 0 到 15。位 4 或位 8 打开时必须有 `0x` 开头的 sitekey，并写入 `cf_turnstile_sitekey`。关掉这两位时把 sitekey 存成空字符串。事件文案按打开的开关拼接：进入落地、点击按钮、浏览时加载、等待人机点击。

## 3. 改 `ablab/cloudflare.py`

如果旧文件已经有 `set_human_challenge`，保留「进入落地」和「点击按钮」的原有防火墙、页面规则逻辑，只把浏览组件补成下面这样。如果完全没有这组函数，再把入口检查、按钮规则、浏览组件一起加上。不要改 `_copy`、`_limit_browser_cache`、SSL、DNS。

常量：

```python
BUTTON_QUERY = 'ab_human=1'
BROWSE_WIDGET = '双子星浏览时加载'
SITEKEY_RE = re.compile(r'^0x[A-Za-z0-9_-]{20,80}$')
WIDGET_ROOM = 10
```

`set_human_challenge(..., browse=False, had_browse=False)` 里，`browse` 表示「底部组件要开着」。调用方在「浏览时加载」或「等待人机点击」任一打开时传 `browse=True`。`had_browse` 用旧的 `cf_challenge & 12`。组件写入失败且这次是要打开时，把错误换成「这把 Cloudflare 密钥还不能添加页面里的人机验证」。返回的 `sitekey` 只在 `browse` 为真时带上。

`_write_widget` 的正文固定为：

```python
payload = {'name': BROWSE_WIDGET, 'domains': domains, 'mode': 'non-interactive', 'clearance_level': 'jschallenge'}
```

已有同名组件且域名已在里面、模式已经是 `non-interactive` 时，直接返回原来的 sitekey，不要再建一个。模式仍是 `managed` 时，用同一个 sitekey 再 PUT 一次，改成上面的正文。

`_widget_view` 要读出 `mode`。读失败不要把密钥写进异常文本。

## 4. 改 `ablab/web.py`

顶部：

```python
from .human_check import stamp_published
```

请求模型在原有字段上增加，旧后台没这两个字段时默认关：

```python
class ChallengeSwitch(StrictModel):
    entry: bool = Field(strict=True)
    button: bool = Field(strict=True)
    browse: bool = Field(default=False, strict=True)
    wait: bool = Field(default=False, strict=True)
```

发布、换页之类已经会调用 `after_site_change` 的地方，在里面补一次盖章，让新上传的页面也带上当前开关。读的是库里的 `cf_challenge`，不要在这里改开关：

```python
mode = site.get('cf_challenge') or 0
stamp_published(
    registry.store(site_id),
    button=bool(mode & 2),
    browse=bool(mode & 4),
    wait=bool(mode & 8),
    sitekey=site.get('cf_turnstile_sitekey') or '',
)
```

关闭 Cloudflare 代理之前，若 `cf_challenge` 非 0，先把四个人机开关都关掉，再盖章去掉页面里的脚本。`had_browse` 用 `bool((site.get('cf_challenge') or 0) & 12)`。

`POST /api/sites/{site_id}/cloudflare/challenge`：

```python
mode = (1 if body.entry else 0) | (2 if body.button else 0) | (4 if body.browse else 0) | (8 if body.wait else 0)
widget = body.browse or body.wait
changed = cloudflare.set_human_challenge(
    site['cf_zone_id'], site['domain'], body.entry, body.button, site.get('cf_challenge_level') or '',
    browse=widget, had_browse=bool((site.get('cf_challenge') or 0) & 12),
)
updated = registry.set_challenge(site['id'], mode, changed.get('previous') or '', changed.get('sitekey') or '')
if stamp_published(
    registry.store(site['id']), button=body.button, browse=body.browse, wait=body.wait,
    sitekey=changed.get('sitekey') or '',
):
    # 只清这一条域名的边缘缓存，不要改缓存时间
    ...
```

观察账号不能改。`default` 站点和没接入 Cloudflare 的域名拒绝。返回里带 `entry`、`button`、`browse`、`wait` 和 `detail`。文案：

- 进入落地：访客一进入落地页，会先看到 Cloudflare 的人机验证
- 点击按钮：访客一点按钮，会先看到 Cloudflare 的人机验证，通过后才继续打开
- 浏览时加载：落地页先显示内容，人机验证排在页面最下方，自己完成，不用点按钮，也不挡住内容
- 等待人机点击：验证还没通过时，点按钮不会打开，页面提示请稍等，正在验证是否是真人

## 5. 改后台页面

`static/app.js` 里，域名行已经有 `site.cf_zone_id` 且不是 `default`、操作者不是观察账号时，画出四个开关。每个请求都带上另外三个的当前值，避免点一个把别的关掉：

```javascript
const entryOn = Boolean(mode & 1);
const buttonOn = Boolean(mode & 2);
const browseOn = Boolean(mode & 4);
const waitOn = Boolean(mode & 8);
const challengeBody = (entry, button, browse, wait) => ({entry, button, browse, wait});
```

四个标签按顺序是：进入落地、点击按钮、浏览时加载、等待人机点击。请求 `POST /api/sites/${site.id}/cloudflare/challenge`，超时用 60000。成功后刷新域名列表，并用返回的 `detail` 做提示。

旧文件如果没有 `humanSwitch`，加上这个小函数，不要改别的按钮：

```javascript
function humanSwitch(site, label, checked, action) {
  const row = element('label', 'domain-challenge-row');
  row.append(document.createTextNode(label));
  const toggle = element('button', 'switch');
  toggle.type = 'button';
  toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-checked', checked ? 'true' : 'false');
  toggle.setAttribute('aria-label', `${site.domain || '这个域名'} ${label}`);
  toggle.append(element('span'));
  on(toggle, 'click', action);
  row.append(toggle);
  return row;
}
```

`static/dashboard.css` 只加这些样式。已有 `.domain-challenge` 时，把 `min-width` 改成 `176px`，好放下四个开关：

```css
.domain-challenge { display: inline-flex; flex-direction: column; gap: 4px; min-width: 176px; padding: 4px 6px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-soft); color: var(--ink-soft); }
.domain-challenge-title { font-size: 11px; font-weight: 750; }
.domain-challenge-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 11px; font-weight: 650; white-space: nowrap; }
.domain-actions .domain-challenge .switch { width: 37px; height: 21px; min-height: 21px; padding: 3px; }
```

## 6. 部署

只上传这六个路径：

- `ablab/human_check.py`（新文件，用本目录那份）
- `ablab/sites.py`
- `ablab/cloudflare.py`
- `ablab/web.py`
- `static/app.js`
- `static/dashboard.css`

第六个是样式。不要上传测试、交接或其它业务文件。

顺序：

1. 在服务器上对将要覆盖的已有文件做 sha256。和你改之前的副本不一致就停。`human_check.py` 原来不存在是正常的。
2. 备份到 `/tmp/ab-bak-human-check.tgz`，不要备份进网站目录。
3. 解开到 `/opt/ab-lab`。权限、所有者参照原来的 `ablab/web.py` 和 `static/app.js`。
4. `/opt/ab-lab/.venv/bin/python -m py_compile ablab/human_check.py ablab/sites.py ablab/cloudflare.py ablab/web.py`。失败就把备份拷回去，不要重启。
5. `systemctl restart ab-lab-admin`。确认它是 active。确认 `ab-lab-target` 的 `ActiveEnterTimestamp` 没变，并且仍是 active。
6. `curl` 后台登录页应返回 200。用浏览器 UA。不要用 `127.0.0.1:8765/login` 判断，那个 403 是边界。
7. 只读看 `sites.cf_challenge`。部署本身不得把原来是 0 的域名改成非 0。原来已经打开的开关保持原值。

静态文件换上后，后台要强制刷新一次才看得到新开关。

## 7. 验收

后台域名行应有四个开关，默认都是关。

打开「浏览时加载」后，落地页先出内容，验证框在正文最下方，页面上看不到 JavaScript 源码。手机上不用再点勾选框。地址栏使用 `https://`。

再打开「等待人机点击」后，验证完成前点按钮，页面上方出现「请稍等，正在验证是否是真人」，链接不跳走。验证完成后同一下可以点出去，提示消失。

只开「进入落地」或「点击按钮」时，行为与改之前一致，并且不要求 Turnstile 权限。
