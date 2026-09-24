# 自动接入开发进度（2026-09-21）

这是源码开发进度说明，不是自动建站发布版。现有 ZIP 未更新，现有服务器未升级或改站点。

## 2026-09-21 真实证书代码范围核对，待架构选择

- 只读解析服务器 `/www/server/panel/class/acme_v2.py` 的 AST，未导入或执行面板模块。`get_account_info`、`save_cert`、`sub_all_cert`、`register` 四个函数的源码 SHA-256 均与缓存官方参考代码一致。
- 确认 `save_cert` 调用 `sub_all_cert`；后者扫描站点证书、vhost SSL、邮件证书目录，按同 issuer 组织、同 SAN 集合、旧证书到期不晚于新证书的条件更新证书及私钥，然后调用 web service reload。它不是只按目标站点 ID 写入。未读取或输出任何私钥内容。
- 实际模块目录为 `/www/server/panel/class_v2/ssl_domainModelV2`。`api.py` 中 `switch_auto_renew`、`cert_deploy_sites`、`ssl_tasks_status` 的源码摘要与参考一致。部署非追加模式会移除不在请求列表中的旧部署并关闭那些站点的 SSL；续期开关是 toggle，不是幂等设置。
- `apply_new_ssl` 的实际源码摘要为 `4fa3efddf3390023b6401da40c56dbf37f39c9f35efb4657ba2d536221918454`，参考为 `e91ea7acad205f06e33cd6138a54d60064123f856c897dcd5707cca6041fad8c`，不应直接假定接口实现完全相同。批量 AST 检查在另一个非明文 Python 模块处解析失败，未尝试解码/绕过；前述 api.py 摘要已成功输出。
- 未调用证书申请、注册、部署、续期开关或“账户查询”（后者也可能注册/创建计划任务）。未改证书文件、Nginx 配置、服务、密钥或测试站点。
- 待用户决定：继续针对宝塔共享证书流程实现影响范围预检，或改用系统独立的证书签发/续期目录和任务，仅为受管新站点配置证书。推荐独立管理，但这改变原先依赖宝塔证书接口的方案，不能在未确认时安装新客户端或更改证书生命周期。选择后仍须实现与真实验收，不代表自动建站已完成。

## 2026-09-21 终端恢复后的真实 Linux 检查

- 已在真实服务器运行源码中六个文件操作函数的隔离探针：替换时保留实际旧文件及权限、排他发布、模拟并发修改、替换后模拟进程中断、不支持替换时停止，以及符号链接拒绝，6 项全部通过。
- 先在 `/tmp` 运行，发现它与 Nginx 配置目录设备号不同；随后在 `/www/server/panel/vhost` 下新建私有随机测试目录重跑，`same_device_as_nginx=true`，6 项仍全部通过。没有写入 Nginx 在线配置目录或运行重载。测试夹具留在两处 `ab-lab-io-probe-*` 私有目录，供复核；未清理既有文件。
- 探针由 `work/build_io_probe.py` 从源码提取文件操作函数；函数文本 SHA-256 为 `a96abc1d8f2028cc52843c4c12aa88e81681259936636f9a7de610b07447267c`。目录 fsync 在真实 Linux 上执行成功，但这不是实际断电恢复实验，也不是完整 worker 验收。
- 真实只读服务检查：`ab-lab-admin`、`ab-lab-target` 均 loaded/active，User/Group 均为 `ab-lab`；`ab-lab-dns` 为 not-found/inactive；应用虚拟环境解释器存在。没有安装、启动、重启或修改服务。
- 本机认证查询 `/v2/data?action=getData`，`table=binding,list=True,search=''`，返回整数 `status=0`、空列表。未输出签名或密钥。首次诊断脚本有换行转义语法错误，修正后执行成功；失败版本未执行查询。
- `PanelSites` 现同时核对根域名绑定和子目录绑定；子目录占用目标域名/泛域名、绑定数据不完整，或已受管站点新增子目录绑定时停止创建/接管。不相关的有效绑定不阻塞新建。新增 5 项测试先失败后修复通过；有数据的行结构依据缓存官方源码，本机空列表仅验证响应封装。
- 最新全量验证：**209 项 Python、8 项 JavaScript 测试通过**，两个既有依赖弃用警告。独立复查重跑相关 28 项通过，无确认缺陷。本轮只增加冲突检查与验证证据，尚未打包或部署；证书/续期、手工 Nginx 配置冲突、worker 隔离与联调仍未完成。

## 2026-09-21 复查修复与验证

- 已复现并修复：并发修改时普通替换会丢失人工配置、回滚前缺少归属复核、`..` 绕过私有目录位置检查、Unicode 域名别名与 ASCII 域名漏判，以及大配置的 JSON 备份超过读取上限。
- 配置文件仍限制 256 KiB；备份记录独立限制 2 MiB，覆盖 JSON 转义膨胀。测试覆盖大量反斜线和恰好达到配置大小上限的往返读取。
- 私有记录采用排他发布，不覆盖既有记录。配置写入前持久化 `.pending` 标记；替换时保留真正被换出的版本，不仅保存早期读取的基线。Linux 使用 `renameat2(RENAME_EXCHANGE)`；Windows 开发测试使用 `ReplaceFileW` 的备份参数。两者没有普通覆盖式 rename 降级路径。
- 现场快照位于配置目录下新建的 `.ab-lab-*` 私有子目录（Linux 模式 0700，避免跨文件系统替换），不匹配标准 `*.conf` include。快照不自动清理。发生冲突/进程中断/结果不明确时保留 `.pending`，阻止重载和后续自动重试，需管理员核对当前配置与快照后再处理；不要直接清除标记。
- **并发限制：**替换不是与宝塔管理员操作之间的条件锁。发现竞态时，新内容可能已经在配置路径，但旧内容仍在现场快照中，程序不会重载或自动覆盖回来。部署前仍需确保受管目录权限、单 worker 锁，以及核对真实 Nginx include 范围；不能承诺隔离同权限管理员的并发行为。
- 本机全量验证：204 项 Python 测试通过，8 项 JavaScript 测试通过；两个既有依赖弃用警告仍在。独立只读复查在修复后重跑相关 50 项测试通过，无新增确认缺陷。HTTP/面板及 Nginx 进程仍为测试替身，文件读写和 Windows 替换操作使用真实临时文件。
- Linux 替换、目录持久化与权限验证尚未完成：本机无已安装 WSL；真实宝塔终端显示空白、连接指示红色，刷新后回到面板入口地址，浏览器控制超时。没有因此对服务、站点或证书做变更。
- 原生文件操作依据：[Linux rename(2)](https://man7.org/linux/man-pages/man2/rename.2.html)、[Microsoft ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew)。Windows 失败可能留下部分完成结果，因此不清理现场文件，也不把异常视作“未写入”。

## 新增：建站与 HTTP 入口适配组件

- `panel_sites.py` 封装实际 v2 域名绑定查询、站点分页查询、静态 `AddSite` 请求。限定单个精确域名、固定 ID 路径、PHP 静态版本 `00`，不创建 FTP/数据库，不自动申请 SSL；超时不重发。
- 域名、别名与泛域名冲突会停止接入。站点备注保存 `ab-lab:<id>:<owner>` 标记，结合私有任务记录核对归属；标记不是管理员权限隔离的替代品。
- `managed_panel.py` 连接 API 与真实文件系统检查：入口目录、同名 Nginx 配置或历史创建备份已存在时拒绝新建；创建后只对一致的面板身份捕获初始配置。
- `nginx_entry.py` 在私有目录保存原配置，核对原文/目标原文，先运行 `nginx -t`，保留被替换版本后再次 `nginx -t`，通过后才重载。检查失败时只有归属及配置仍一致才尝试恢复；恢复也采用同样的现场保留机制。其他站点不回滚、不清空。
- HTTP 入口带独立 ACME 路径，反代固定本机 `8766`、传递当前精确域名，覆盖客户端 IP/协议头并清空其他转发头。当前没有 HTTPS 配置；应用仍拒绝生产 HTTP 页面访问，不能据此宣称网站上线。
- 写入默认关闭，组件尚未与常驻任务入口连接。本地测试使用真实临时文件，HTTP 和 Nginx 进程为替身。另在目标服务器用现有 Nginx 执行了独立内存配置 `nginx -t`，退出码为 0；没有启动此配置、没有修改在线配置或重载。
- 内存语法检查内容由 `render_http_entry()` 生成，包在最小 `events {}` / `http {}` 中，测试域名为 `syntax-check.example.com`。本地/服务器 SHA-256 一致：`47976f33751c9766d6dddc49bf9e917723d74aca084b06463e84b694c96b2565`。首次使用管道 `/dev/stdin` 失败，随后改为可寻址 memfd 文件 `/proc/self/fd/<fd>` 成功；这不是现有全量配置合并或 HTTP 行为验证。
- 在真实面板上新增的只读验证：全量域名绑定接口返回 `status=0`、50 条绑定，测试站点 ID 22 对应 apex 和 `www`，`pid` 均为整数。这不是建站写入验收。

## 已实现、可在本地验证

- `ablab/provisioning_jobs.py`：使用真实 SQLite 保存创建意图、归属标记、面板站点 ID 和阶段。任务状态不依赖进程内存。
- 独立任务目录与网页可写数据目录分离；Linux 要求目录由当前服务账号持有且权限为 `0700`。部署时还须确保网页服务与建站服务不是同一个 Unix 账号。
- OS 文件锁阻止两个建站进程同时工作。服务退出后锁自动释放。
- 创建超时/重启时先查询归属；已有同名站点不接管，归属不一致不覆盖。已记录创建意图但查不到站点时停止并要求人工核对，不盲目再次创建。
- 暂停会阻止后续写入，但无法撤销已在远端执行中的请求。明确尚未发送就暂停的创建/证书操作会恢复可重试检查点；重新启用后先核对持久化记录。
- 配置阶段要求适配器幂等并在写入时再次检查归属。证书请求先记意图，最多调用一次；结果不明确时仅验收，不反复提交证书订单。
- 只有 HTTPS、入口路由、续期三项明确验证通过才标记 `active`；模拟测试结果不是公网验收证据。
- `PanelPreflight.from_local_config()` 适配 aaPanel 8.0.4 本机内部 token 格式，避免重复哈希。只接受 `127.0.0.1` 目标及仅本机白名单；签名放入 POST 正文，不放 URL。

## 本机只读检查命令

仅在已同步本轮源码的服务器上运行，使用拥有面板配置读取权限的账号。请不要为网页服务增加面板密钥读取权限。

```bash
/opt/ab-lab/.venv/bin/python /opt/ab-lab/deploy/inspect_panel.py \
  --local-config --address http://127.0.0.1:34734
```

端口应替换为实际面板端口。命令不重置密钥、不更改白名单、不申请证书、不创建站点，也不输出内部 token。仍保留交互输入界面明文 API 密钥的旧检查方式；两种凭据来源不能混用签名算法。

此前在测试服务器 Linux 终端用独立只读诊断验证过：静态能力查询和站点列表查询返回整数状态 `0`，测试域名 `dmvfvj.vip` 的面板 ID 为 `22`。新版检查脚本本身尚未在服务器部署执行。

## 尚未完成，不能开启自动写入

- `ProvisioningWorker` 已有开发中的 `ManagedPanel` 适配器，但证书与最终验收方法明确报未完成，也没有接入 `run.py --service dns`。DNS 服务继续显示自动写入禁用。
- 尚需完成：目标版本子目录绑定/手工配置等冲突覆盖核对、已实现写接口的受控真实验收、证书签发/续期及 HTTPS 路由验收、服务部署和权限隔离。
- 官方源码中的证书保存会扫描多个证书目录，更新同品牌、同 SAN 集合、到期不晚于新证书的副本，并非只按站点 ID 操作；证书部署缺省替换模式还会移除旧部署关联。调用前必须确认准确范围，不能以模拟返回代替验证。
- 证书注册代码内置同意 ACME 条款，首次注册前须用户确认；获取账户信息的方法也可能注册账户/创建计划任务，不视作只读诊断。现阶段不调用这些接口。
- 仅文件锁和私有目录检查不是完整权限隔离；需要专用服务账号、安装配置和部署验收。
- 创建意图已写入但请求尚未发送就发生进程崩溃，仍会进入保守的人工核对状态（不包括正常处理的暂停）。证书意图存在但实际因崩溃未发送/请求失败时，不会因后台点重试重新申请；后续必须提供安全的订单核对流程。
- 既有三个测试站点均未删除或接管。任何删除重建演练须先备份，再确认具体操作。

## 2026-09-21 独立证书命令层（仅本地开发）

- 在隔离工作树分支 `codex/isolated-certificates` 增加 `CertbotCommand`：固定二进制/CA/目录、精确单域名、预登记账户 ID、指定证书续期，测试与正式环境分开。dry-run 只允许 staging 对象。
- 执行默认关闭；运行前要求 literal True 的归属/暂停复核结果。命令不经 shell，清理继承环境、限制超时、不返回进程输出、失败不自动重试。命令退出 0 只表示进程成功退出，不代表证书更新。
- 默认 CLI 文件存在、路径含链接或不可核对时拒绝执行，保留现有配置；显式限制 HOME/XDG。续期关闭 Certbot 内部随机等待，避免等待超过命令超时。安装版本的搜索路径与目录权限仍需真实部署核对。
- 34 项新测试；全量 Python 243 项通过（2 项已有弃用警告）、JavaScript 8 项通过。外部进程为替身，没有申请真实证书。
- 此模块没有接入线上入口；`ManagedPanel.certificate()` / `verify()` 仍拒绝标记成功。账户与条款确认、专用配置审计（包括 hooks）、目录权限、真实证书校验、不可变版本、TLS 配置、续期服务及端到端验收仍需继续实现。
- 原始目录基线已提交为 `15d2bb6`；未改服务器、未更新发布 ZIP。

## 2026-09-21 证书材料校验与受限读取器

- 新增离线真实 X.509 校验：精确单域名 SAN、当前有效期、服务器用途、完整有序可信链、公私钥匹配；生产/staging 信任根隔离，返回同一份归一化 PEM 字节，私钥不进入 repr 或错误文本。依赖固定 cryptography 50.0.1。
- 只允许 Certbot live 的最后一级链接；目标必须是同环境、同站点、同代次 archive 常规文件。检查大小、链接、硬链接、POSIX 所有者/权限及读取前后状态；不修改材料。
- 全量 Python 266 项通过、17 项平台跳过、2 项既有弃用警告；JavaScript 8 项通过，pip check 通过。Windows 跳过不是 Linux 验收。
- 真实 Linux 标准库读取探针 18 项通过，提取源码哈希 `c32059753fdf949091b7547e4d414e307f07af6b0b42a927b5e1fa3135f3cad1`，夹具保留 `/tmp/ab-lab-cert-read-cme30nze`。生成器为 `deploy/build_cert_reader_probe.py`。探针未读取现有证书、未安装依赖、未访问 CA 或更改站点；尚未运行 Linux 完整 X.509 测试。
- 独立审查无 Critical/Important 问题。模块尚未接入线上；不可变版本、配置审计、TLS 切换、续期服务和受控真实建站仍待完成，不能开启自动写入或发布为已完成。

## 2026-09-21 独立证书版本目录（尚未接线上）

- `CertificateVersions.publish()` 重新校验原始 PEM 为 production，然后保存到 `production/deployed/ab-ID/<sha256>`；摘要覆盖完整链与密钥，不直接跟随 live 链接。
- 站点级 owner 记录核对完整身份与面板 ID；文件排他创建、完成记录最后发布。相同版本逐字节复核后复用，残缺/冲突目录不覆盖、不修复。新证书另建版本，旧版本保留。
- 目录 0700、文件 0600；默认禁写，每次创建目录/发布文件前重新核对归属和暂停。沿用 worker 锁与特权私有目录约束，不声称可隔离同权限恶意管理员。
- 修复 Windows 硬链接发布后的 ctime 跨 API 差异；保留每种 API 的前后变化检测和 Linux 全部检查。两项 ctime 变化回归已通过，禁用检查的内存变异测试能正确捕获缺陷。补充各写入边界撤权检查。
- 独立审查无 Critical/Important 问题；目标测试 60 项通过、21 项平台跳过。新增生成器 `deploy/build_cert_versions_probe.py` 为后续 Linux 隔离验证准备，不安装依赖、不联系 CA。
- 最新完整回归：**303 项 Python 通过、21 项平台跳过、2 项既有弃用警告；8 项 JavaScript 通过**。
- **本轮 Linux 探针未运行**：宝塔浏览器页面读取和导航多次超时，未上传脚本；不能引用上一轮 Linux 结果证明本轮新代码通过。没有更改站点、证书、Nginx 或服务，也没有更新发布 ZIP。
- 尚需完成 Linux 新代码验证、专用 Certbot 配置审计、TLS 配置事务、续期服务、worker 接入和受控真实建站验收。自动写入继续关闭。

## 2026-09-21 补充：新读取器与证书版本 Linux 隔离验证

- 经宝塔文件上传，将生成的隔离脚本放在 `/tmp/cert-versions-probe.py`。执行前在同一 Python 进程读取字节、核对 SHA-256 `346868d1ccd6e2e449b1a7a329f51226123d2e810484482133f69387809a87c8`，再编译执行同一份字节；终端输出 `AB_PROBE_HASH_OK`。
- 新读取器 18 项通过，提取源码哈希 `7833d0f09421836e4b348ec0bfd72497de099dd9c921b548ba9412eae74f1821`。
- 新版本发布 15 项通过，现场生成测试 CA/中间证书/服务器证书并执行真实验证、排他文件写入、目录 fsync、权限检查。涵盖重复不写、更新保留旧版、缺失完成记录、密钥修改、硬链接、目录链接、权限错误、归属冲突、模拟写中断、暂停、staging 拒绝和默认禁写。提取源码/测试载荷哈希 `ee9adb0f16bb4e0b3d63f16ac76ed8165c2d15651cbf03b9ffb51f751401b49f`。
- 终端明确输出 `READER_PASS 18`、`VERSION_PASS 15`、`CRYPTOGRAPHY 46.0.5`。这是服务器已有依赖，不等同于项目固定的 50.0.1 部署验收；没有安装或升级依赖。
- 保留夹具 `/tmp/ab-lab-cert-read-emuewum4`、`/tmp/ab-lab-cert-versions-uyt4ohz8` 和上传的测试脚本供核对（版本目录拼写于 09-22 按宝塔文件列表更正）。只使用这些新建临时测试对象，不读取真实私钥、不联系 CA、不改网站/Nginx/服务配置。
- 此证据补上上一节未完成的局部 Linux 验证，仍不是完整 worker、线上 HTTPS 或自动建站验收。

## 2026-09-22 受管 HTTPS 配置事务（仅本地验证）

- `CertificateVersions.load()` 只读核对身份、目录权限、文件类型与链接、完成记录、内容摘要和真实证书有效性；拒绝残缺或被改版本，不修复文件。
- 新增 `TlsEntry`：仅接受本系统创建记录对应、当前配置未被人工修改的站点。HTTP 保留 ACME 验证路径，其他请求跳转规范域名 HTTPS；TLS 1.2/1.3 使用独立生产证书版本，并复用既有代理头设置。
- 切换前持久化外层 pending，保存真实旧配置；先检查后重载，每个操作边界重新核对归属和暂停。语法检查失败只恢复仍属本次事务的配置；续期失败恢复之前 HTTPS，而不是退回 HTTP。重载或完成记录写入结果不明时保留 pending 并拒绝自动重试。
- 同一已提交版本不重复写配置或重载；续期发布新版本保留旧证书和配置。未解除 `ManagedPanel.certificate()` / `verify()` 门禁，也未接通线上 worker。
- 新增 48 项本地可执行测试和 5 项平台专用测试；全量 Python **351 通过、26 平台跳过、2 项既有弃用警告**，JavaScript **8 通过**，pip check 通过。文件及证书材料真实，Nginx/面板调用为替身，不能据此宣称真实 Nginx 验收完成。
- 独立审查未发现 Critical/Important 问题；补充了续期完成记录替换前后中断、load 链接与 POSIX 权限回归。后者在本次 Windows 环境跳过，需后续 Linux 验证。
- 此轮未更改服务器或现有三个测试站点，未安装依赖、调用 CA、重载 Nginx 或更新发布 ZIP。上节 Linux 探针不覆盖本轮新增 load/TLS 代码。
- 仍需：真实 Linux/Nginx TLS 检查及握手、专用 Certbot 配置和 hooks 审计、续期服务、worker 接入、受控真实建站与 HTTPS 路由/续期验收。自动建站尚未完成，自动写入继续关闭。

## 2026-09-22 补充：真实 Linux/Nginx 隔离检查通过

- 新增生成器 `deploy/build_tls_probe.py`，从当前生产函数和测试证书夹具生成自包含脚本，不安装依赖、不读取真实站点配置或密钥。服务器执行的是 `/tmp/tls-isolated-probe-v2.py`，同一进程先读取字节并核对 SHA-256 `843bfc4db693b800630e5096fc8b18dddbd076f66afdf88936aae552a312ee36`，再编译执行同一份字节。
- 审查发现 `nginx -t` 仍可能绑定监听 socket，且可以创建空 PID 文件。根据 [Nginx socket 源码](https://github.com/nginx/nginx/blob/master/src/core/ngx_connection.c) 和 [PID 源码](https://github.com/nginx/nginx/blob/master/src/core/ngx_cycle.c) 修正探针：测试副本只允许把已知 80/443 模板改为私有目录内 Unix socket，未知监听形式拒绝；PID 文件允许不存在或为空，不允许写入进程号。所有日志、临时文件、PID 和 include 均在临时根下。
- 旧 `/tmp/tls-isolated-probe.py` 已上传但**从未执行，不应使用**；修正后的 v2 经独立复核无剩余 Critical/Important 问题后才执行。两份脚本保留供核对。
- 终端明确输出 `AB_TLS_HASH_OK` 与最终 `AB_TLS_DONE`，并返回 root 提示符。读取器 18 项、版本发布 15 项再次通过；新增 load 11 项通过，覆盖链接、硬链接、权限、篡改、过期、归属与摘要，确认拒绝时不修复文件。
- `AB_TLS_PROBE` 输出 `nginx_valid_checks: 9`、`mismatched_key_rejected: true`、`transaction_pass: 4`、`reload_simulated: 3`、`real_reload: false`、`listeners: private-unix-sockets`。真实 Nginx 验证语法/PEM 加载，Linux 文件替换与保留备份也实际执行；首次 TLS、同版不重载、更新保留旧版、失败回滚/阻止重试通过。归属接口与重载结果为替身，未启动工作进程。
- TLS 夹具保留 `/tmp/ab-lab-tls-probe-bz705k64`。TLS 提取源码摘要 `74d944328e44fbf3c5e8df56fce318e17c207046b2deb66b254801aeb58c4649`，版本源码摘要 `de70333901198c455c663ed26d0feec20f9ed20059aa2e4f70447ee4eb556e2f`。服务器使用既有 cryptography 46.0.5，不等同于固定 50.0.1 安装验收。
- 该证据不覆盖真实 TCP 80/443 监听、HTTPS 握手、线上 reload、CA 签发或续期。未更改现有站点、服务或发布 ZIP，自动写入继续关闭。
- 新增 7 项探针边界回归先失败后通过；最新完整本地回归 **358 项 Python 通过、26 平台跳过、2 项既有弃用警告，8 项 JavaScript 通过**。

## 2026-09-22 私有 Certbot 配置审计（本地开发，尚未上线）

- 命令入口新增强制只读检查：专用目录、仅含注释的私有 CLI 文件、指定站点续期配置。核对精确账户、CA、域名、webroot、live/archive 路径；拒绝 hooks、installer、未知选项、重复项、额外域名和歧义语法，不自动改写。
- 复用现有受限文件读取器：限制大小，拒绝符号链接、硬链接、非普通文件和不安全 POSIX 权限。新申请遇到目标已有配置/材料目录即停止；`.conf.new` 残留需要人工核对，禁止静默覆盖。
- Certbot 子进程显式设置 `umask=0077`，不依赖启动者的默认权限。正常 LF/CRLF 支持，注释中夹带裸 CR/控制字符拒绝；ARI 时间要求无时区的规范时间，避免和上游 naive datetime 比较失败。
- 按 [Certbot 官方用户指南](https://eff-certbot.readthedocs.io/en/stable/using.html) 及官方 `storage.py` / `renewal.py` 核对配置和续期逻辑；该检查器刻意只接受受管配置子集，不声称兼容任意 Certbot 历史配置。实际安装版本、默认配置搜索路径与真实生成配置仍须隔离验收。
- 独立审查指出空 CLI 应允许；已以失败测试复现并修正。共享受限读取器仅增加默认关闭的空文件选项，只有 CLI 开启，证书材料和续期文件仍禁止为空；其他安全检查不变。补齐密钥参数、ARI 小数秒拒绝、续期文件权限与所有者测试；复审无剩余问题。
- 最终完整本地回归：**436 项 Python 通过、33 平台跳过、2 项既有弃用警告；8 项 JavaScript 通过；pip check / git diff --check 通过**。本轮新增代码和读取器调整尚未运行 Linux 验收；不复用上轮探针当作新代码验收。
- 未更改服务器配置，没有安装客户端、注册账户、接受条款、申请证书或更新 ZIP。账户、完整材料复核、worker 接入、独立 timer、真实建站和 HTTPS/续期验收仍未完成；自动写入保持关闭。

## 2026-09-23 服务器客户端只读核对

- 通过用户已登录的宝塔终端运行只读检查，看到 `AB_CERTBOT_INSPECT_DONE` 并返回 root 提示符。PATH 中没有 Certbot，系统 Python 元数据中没有 Certbot / ConfigObj。
- `apt-cache policy certbot` 显示 Installed `(none)`、Candidate `4.0.0-4`，来源为 Ubuntu archive。`apt-get -s install certbot` 仅模拟安装，输出 Certbot、python3-certbot、ACME / ConfigObj 等依赖计划；没有实际安装。
- 需要按实际客户端版本做隔离配置兼容测试，不能仅依据 5.8.0 文档认定 4.0.0-4 部署通过。后续安装还要核对并隔离包自带默认续期任务；未批准前不执行安装、mask 或服务改动。

## 2026-09-23 客户端安装授权与前置核对

- 用户明确回复“允许”，授权从 Ubuntu 源安装 Certbot 4.0.0-4 及依赖，并隔离默认续期任务；不包含 CA 账户注册、条款接受、正式签发或网站改动。
- 安装前终端检查：uid 0；`/etc/letsencrypt`、`/etc/cron.d/certbot`、`/etc/systemd/system/certbot.timer` 和 `.service` 均不存在；两个 unit 均 `not-found` / `inactive`。安装模拟退出 0，计划新增 15 个软件包、删除 0 个。
- 安装采用不升级/不删除现有包、不安装推荐插件的参数；先屏蔽新默认 systemd 服务/定时器，并核对 cron 隔离。最终安装及隔离结果须以下实际终端结果为准，不以模拟输出视为安装成功。

### 实际操作与断线核对

- 安装前执行 `systemctl mask certbot.service certbot.timer`，终端确认两个新建 `/etc/systemd/system/` 链接指向 `/dev/null`，两个 unit 均 `masked` / `inactive`。未停用既有其他服务。
- 核对 cron 原路径、隔离目标均不存在，且不存在旧 diversion 后，执行 `dpkg-divert --local --add --rename --divert /etc/ab-lab-certbot.cron.disabled /etc/cron.d/certbot`。包自带 cron 将保存到 cron 搜索目录外；登记由包管理器保留，未删除文件。
- 实际安装命令：`DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get -y --no-install-recommends --no-remove --no-upgrade -o DPkg::Lock::Timeout=30 install certbot=4.0.0-4`。
- 安装后浏览器终端断线并重连，未看到原命令完整输出。没有盲重试；重连后 `dpkg-query` 确认 certbot 和 python3-certbot 均 `install ok installed 4.0.0-4`，没有 apt-get / dpkg PID 输出；默认 service / timer 再次确认 `masked` / `inactive`，`AB_STATUS_DONE` 后返回 root 提示符。
- 默认任务隔离是可恢复的包管理配置，不是删除；如未来需要恢复，必须先核对独立续期任务与默认扫描范围，再针对这两个 mask 和这条 diversion 作恢复，不能直接开启全局续期。
- 安装后实际运行 `/usr/bin/certbot --version` 退出 0，输出 `certbot 4.0.0`；`--help all` 退出 0。帮助包含 `--no-directory-hooks` / `--config-dir` / `--webroot-path`，没有显示 `--no-random-sleep-on-renew`，继续核对已安装代码，不能凭帮助字符串缺失就认定参数不支持。
- cron 活跃路径不存在，隔离文件存在。`dpkg --audit` 退出 0 且 stdout 为空。安装包新增 `/etc/letsencrypt/cli.ini`；现有命令审计会拒绝该默认配置，因此仍未放开业务证书命令。
- 已读取服务器实际安装的 `certbot._internal.cli` 对应选项代码：`--no-random-sleep-on-renew` 使用 `action="store_false"`、`dest="random_sleep_on_renew"`，并设置 `help=argparse.SUPPRESS`。帮助不显示是隐藏参数，不是缺少支持；此核对没有发出续期或 CA 请求。
- 客户端安装与默认续期隔离完成，但私有配置兼容验证尚未完成：保留安装包新增的全局 CLI，不删除、不临时绕过审计。尚未创建 CA 账户、接受条款、申请证书、启用独立续期任务、重载 Nginx 或上线自动建站。
- 安装后最终服务核对显示 `APP_STATUS ab-lab-admin.service active`、`APP_STATUS ab-lab-target.service active`，随后返回 root 提示符。本轮只修改安装记录，未更改应用源码或发布包；原有本地测试结果不作为新的线上 HTTPS 验收证据。

## 2026-09-23 补充：安装版参数检查与默认 CLI 隔离

- 服务器实际安装代码报告默认 CLI 搜索路径为 `/etc/letsencrypt/cli.ini` 和 `~/.config/letsencrypt/cli.ini`。只运行 `cli.prepare_and_parse_args()`，没有调用 Certbot 主流程、认证插件准备、签发或续期。
- 首次 certonly 参数测试使用不存在的虚拟 webroot，被参数解析阶段拒绝；改用已有 `/tmp` 后解析完成。域名和账户均为虚拟值，未创建站点。此时 authenticator/installer 为 None、webroot_map 为空，说明解析本身尚未完成插件选择和续期配置生成；不能把 `storage.relevant_values()` 的这次输出当成实际签发后的续期文件兼容证据。
- 对 `/etc/letsencrypt/cli.ini` 核对 dpkg conffile 记录和实际 MD5，均为 `d409bf370c29a96fe865b5536b2135da`；文件为 root 所有、普通文件、0644、单链接，隔离目标不存在且无旧 diversion。该文件安装前不存在，确认是本次包新增的未修改默认配置。
- 再次用上述类型、权限、链接数、校验值和目标不存在条件作前置保护，执行 `mv -n -- /etc/letsencrypt/cli.ini /etc/ab-lab-certbot.cli.disabled`。终端输出 `AB_CONFIG_ISOLATED`，原默认路径不存在，恢复副本校验值未变；没有删除配置，也没有改动宝塔证书目录。
- 这是保留副本的移动，不是 CLI conffile 的 dpkg diversion；不能声称软件升级后一定保持缺失。未来包操作若重新生成默认 CLI，现有运行前审计仍应拒绝执行，待人工核对。需要恢复时也须先暂停独立证书执行、确认原路径未被重新创建，再恢复这个确切副本，禁止覆盖新配置。
- 移动后重新执行实际安装版 renew 参数解析：显式私有 config/work/logs、虚拟账户/证书名、staging CA、`--no-directory-hooks`、`--no-random-sleep-on-renew` 和 `--dry-run`。断言 random_sleep_on_renew=False、dry_run=True、三种 hook 均空；终端输出 `AB_RENEW_PARSE_OK` 和 `AB_PARSE_ONLY_NO_ISSUANCE`。仅解析参数，**没有执行 dry-run 续期或联系 CA**。
- 最终 `systemctl show`：certbot.service / certbot.timer 均 masked / inactive；ab-lab-admin.service / ab-lab-target.service 均 loaded / active。未重载 Nginx、改变现有网站、创建账户、接受条款或申请证书。
- 尚未完成：实际生成续期配置与私有审计的 Linux 兼容验证、私有账户/目录初始化、worker certificate/verify 接入、独立续期任务和真实建站 HTTPS 验收。本轮仅更新部署记录，不改业务代码或发布 ZIP，不把旧回归结果当作本轮端到端验收。自动建站仍未完成，自动写入保持关闭。

## 2026-09-24 证书部署编排与 worker 授权（本地开发）

- 新增默认禁写的 `CertificateDeployment`，只组合已经独立测试过的 production Certbot 命令、受限 live/archive 读取、生产证书校验、不可变版本发布与 TLS 配置事务。构造时拒绝 staging 命令和 TLS/版本存储不一致。
- `issue()` 只执行一次固定证书命令；命令超时、非零或结果不明确时不在同一次调用中读取结果或自动重试。`deploy_existing()` 只读取已有材料、发布版本并幂等配置 TLS，供持久化 `certificate_intent` 后的恢复路径使用。
- `ManagedPanel` 默认仍没有证书部署对象；只有特权部署显式注入后才可进入证书流程。每个命令和写入边界同时复核 worker 的站点世代/暂停状态以及面板精确身份、路径、owner 和 panel_id。
- worker 将动态授权回调传给申请与恢复。暂停会递增世代并使在途后续写入停止；`certificate_intent` 恢复只部署现有材料，不再次申请。恢复完成后 `verify()` 仍明确报 HTTPS、路由与续期验收未接入，因此真实 `ManagedPanel` 不能把站点标记 active。
- 测试遵循红绿顺序：先观察新模块缺失和接口参数缺失失败，再实现。新增测试包含命令结果未知、边界间暂停、错误材料、默认/真值禁写、精确归属、恢复不重复申请，以及一条使用真实测试 CA、真实不可变版本和真实 TLS 文件事务的组合测试；未连接外部 CA。
- 独立审查发现并复现两个重要边界：面板查询期间暂停可能使查询前授权过期；命令启动前明确拒绝仍会保留一次性意图。现在授权在查询前后各复核一次；新增 `ProvisioningNotStarted`，只有确认未启动的禁用、配置审计或授权拒绝才将任务恢复为 `configured`。命令已进入启动边界后的超时、非零和异常继续视为结果未知，不自动重试；Certbot 已返回后发生的部署暂停也会转换为未知部署结果，绝不恢复成可重新申请状态。修正后独立复审没有剩余 Critical 或 Important 问题。
- 最新完整本地回归：**458 项 Python 通过、33 项平台跳过、2 项既有依赖弃用警告；8 项 JavaScript 通过；pip check 与 git diff --check 通过**。相关 certificate/Certbot/managed-panel/worker 聚焦测试 93 项通过。
- 本轮尚未部署源码、创建证书账户、接受 CA 条款、申请真实证书、启用 timer、重载线上 Nginx 或更改现有宝塔站点。完整 HTTPS 握手、目标应用路由、续期演练和服务器端 worker 接入仍未完成，自动建站继续保持未完成状态。

## 2026-09-24 HTTPS 与应用路由验收（本地开发）

- 目标应用新增独立于上传内容的 `/.well-known/ab-lab-route/<site-id>` 只读证明。中间件先按精确 Host 查找已启用登记站点，端点再核对请求站点 ID；错误 ID、未知或暂停域名不能得到证明，上传页面的 catch-all 不能覆盖该路由。
- 新增只读 `TlsRouteProbe`：直连部署配置中的固定公网 IPv4:443，不依赖公共 DNS 的当前结果；同时用规范站点域名作为 TLS SNI 和 HTTP Host。使用系统默认 TLS 信任与主机名校验，不提供关闭验证的选项。
- 验收同时比较线上叶证书 DER 的 SHA-256 与不可变生产版本复核得到的指纹，并严格要求 200、单一 JSON content type、`no-store`、4 KiB 上限和精确的站点 ID/域名响应。证书部署与验收必须共享同一个版本存储实例。
- 每个证书读取、面板归属查询和外部 TLS 慢边界前后继续复核 worker 暂停/世代状态。`ManagedPanel.verify()` 可以返回 HTTPS 与路由两项真实证明；未注入独立续期证明时固定返回 `renewal: false`，worker 仍不能把站点标记 active。
- 测试先出现 37 项预期失败后实现；聚焦 68 项通过。完整本地回归 **479 项 Python 通过、33 项平台跳过、2 项既有弃用警告；8 项 JavaScript 通过；pip check 与 git diff --check 通过**。独立复查未发现 Critical 或 Important 问题。
- 本轮没有连接真实 443、申请证书、接受 CA 条款、重载 Nginx、修改服务器或发布 ZIP。staging 续期演练、持久化续期证明、独立 timer、特权服务装配和真实线上验收仍未完成；自动建站继续保持未完成状态。

## 2026-09-24 staging 续期演练证明（本地开发）

- 新增默认禁写的 `RenewalRehearsal`。首次没有 staging lineage 时，先持久化 `issue_intent`，再用已登记的独立 staging 账户签发测试证书；严格审计生成的 webroot 续期配置后，才写入 `issued` 收据。
- dry-run 前先持久化 `dry_run_intent`，再运行一次受控 `renew --dry-run`。只有命令成功返回、续期配置再次通过审计且暂停/归属仍有效，才发布绑定完整 identity、panel_id、staging account 和当前生产证书摘要的 `verified` 收据。换生产证书摘要后必须重新演练。
- issue 结果未知时，仅当既有 staging lineage 可严格审计才恢复，绝不重发签发；dry-run 不保存可供事后复核的证书，因此结果未知时永久停止自动重试并要求人工核对。只有 `ProvisioningNotStarted` 明确保证命令未启动时才删除本次 intent。
- 证明记录是私有目录中的排他发布文件，读取时复用普通文件、单链接、权限、大小和并发快照检查。staging 证书永不传入生产版本存储或 Nginx。
- 根据 Certbot 官方指南核对：`--dry-run` 使用测试服务器、获取但不保存测试证书，默认不运行 deploy hook。系统仍显式关闭目录 hooks，并使用预登记 staging 账户及私有目录。
- 独立审查发现并复现一个 Important：测试可注入证书根可能与真实 Certbot 固定执行目录分离。现已由 `CertbotCommand.root` 成为参数、cwd、lineage 和审计的唯一目录来源；复审确认问题消除且无剩余 Critical/Important。
- 本地 TDD 先观察 16 项模块缺失失败，目录统一修复另观察 8 项失败；相关核心 71 项通过，较宽聚焦 198 项通过、7 项平台跳过。完整回归在最终提交前重新执行。
- 本轮没有注册账户、接受 CA 条款、联系 staging/production CA、运行真实 dry-run、修改服务器或发布 ZIP。生产续期 timer、证书变化部署、服务装配和指定域名真实验收仍未完成；自动建站继续保持未完成状态。

## 2026-09-24 特权 worker 装配（本地开发）

- 新增严格的 root-only 私有 JSON 配置：字段集合与 schema 必须精确匹配，拒绝重复 JSON 键，`enabled` 只接受字面布尔值 `true`；面板地址只能是 `127.0.0.1` 根地址，生产/staging 使用不同的 32 位账户 ID，服务器 IP 必须是字符串形式的公网 IPv4，所有文件和数据路径必须是无 `..` 的绝对路径。
- 私有配置、面板令牌文件和两套信任根复用现有受限读取器，拒绝符号链接、硬链接、非普通文件、超限内容以及 Linux 下非当前用户所有或组/其他用户可读写的文件。Web 与 DNS 服务既不接收私有配置参数，也不会读取面板令牌、账户 ID、信任根或私钥。
- 装配顺序固定为 `PanelSites → ManagedPanel/NginxEntry → CertificateVerifier/CertificateVersions/TlsEntry → CertificateDeployment → RenewalRehearsal/CertificateAcceptance → ProvisioningWorker`。生产与 staging 账户分离，部署与线上验收共享同一个不可变版本存储；面板证书管线只允许绑定一次。
- 新增独立 `--service provision --private-config ...` 入口与 root systemd unit。直接运行也会在读取私有配置前核对有效 UID。缺少私有配置、把私有配置传给 Web 服务或同时传入公共配置都会在启动前失败。unit 用 `StateDirectory` 以 0700 初始化 worker/证书根，等待 admin/target，并允许程序在宝塔既有 `/www/wwwroot` 下创建固定的受管子目录；现有安装脚本没有复制、安装、启用或启动它。
- 聚焦测试先确认新模块、调度入口、服务文件和 CLI 门禁缺失，再实现。独立审查发现面板令牌文件读取不够严格、重复 JSON 键可覆盖门禁、整数 IP 被归一化、target 启动竞态和 clean-host 目录启动失败；均先用回归测试复现再修复。最终复审确认无剩余 Critical/Important；最终完整回归结果以下方提交前命令为准。
- 本轮尚未创建真实私有配置、初始化 CA 账户、接受条款、联系 CA、安装/启用服务、重载 Nginx 或修改宝塔站点。独立生产续期 timer、真实 Linux 权限/客户端兼容验证与指定域名端到端验收仍未完成，自动建站尚未上线。

### 回归命令（本地开发）

```bash
python -m pytest tests -q
node --test tests/ui.test.mjs
```

任务测试用真实登记表和私有 SQLite；只有远程面板与 DNS 是测试替身。测试覆盖超时恢复、身份冲突、目录冲突、暂停、证书异常、第二进程抢锁和进程中断，不把测试替身当作真实面板验收。
