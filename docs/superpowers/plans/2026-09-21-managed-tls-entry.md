# Managed TLS entry Implementation Plan

> **For agentic workers:** Use executing-plans for this sequential task and test-driven-development; request independent code review before acceptance.

**Goal:** 从已配置的受管 HTTP 入口切换到指定的独立生产证书版本，提供本地可验证的失败保护，不接通线上 worker。

**Architecture:** 给 CertificateVersions 增加无写入的 load；新增 TlsEntry 复用 NginxEntry 的归属核对、nginx 命令和 guarded_config 文件事务。外层 TLS pending 跨越配置替换、检查、重载与完成记录；异常不自动重试。

**Tech Stack:** 现有 Python/cryptography/pytest 与 Nginx 文件原语，不新增依赖。

**Spec:** docs/superpowers/specs/2026-09-21-isolated-certificates-design.md，第 7–8 步。

## Global constraints

- 写入仍默认关闭，调用者持有 worker 锁且私有目录由特权服务控制。
- 不接管原有站点；必须有创建收据且当前配置等于已确认 HTTP 或最近一次 TLS 配置。
- 所有 TLS 材料来自固定私有根下已完成版本；重新验证生产证书，不使用 live 链接，不信任网页路径。
- 每个写入/回滚/重载前复核所有权和 literal True 暂停回调；检查后配置变化即停止。
- config 替换保留真实旧文件，TLS pending 在操作前持久化；重载/提交结果不明保留 pending，后续调用拒绝自动重试。
- 验证新配置失败时，只在当前配置仍等于新版本且归属/暂停仍允许时恢复之前配置；不重载，不更改其他站点。
- HTTP 保留 ACME 路径，其余跳转固定规范域名 HTTPS；TLS 仅 1.2/1.3，反代已有 8766 入口及相同可信代理头。
- 不调用 CA、不安装客户端、不实际重载服务器、不改 ManagedPanel 未完成门禁，不更新 ZIP。

## Task 1: Read-only certificate version load

Modify `outputs/ab-lab/ablab/certificate_versions.py`; test `outputs/ab-lab/tests/test_certificate_versions.py`.

Interface: `load(identity, panel_id, digest, *, now=None) -> (Path, VerifiedCertificate)`。digest 必须是 64 位小写十六进制；核对每层目录、三个版本文件、站点 owner、ready、内容摘要及真实证书有效性，不写文件。

- [x] 先写失败测试：有效版本返回同一目录和证书、默认禁写对象可只读加载、错误摘要/身份/过期/修改材料拒绝，目录与文件快照不变。

```python
path, certificate = store.load(IDENTITY, 22, version.name, now=NOW)
assert path == version and certificate.domain == IDENTITY['domain']
```

- [x] 运行目标测试观察缺少方法失败；实现只读复核并复跑。

## Task 2: TLS configuration transaction

Create `outputs/ab-lab/ablab/nginx_tls.py`, `outputs/ab-lab/tests/test_nginx_tls.py`。

Interface: `TlsEntry(nginx: NginxEntry, certificates: CertificateVersions).configure(identity, panel_id, digest, authorize, *, now=None) -> bool`。返回是否完成配置切换，不代表站点 active。`render_tls_entry(identity, version_path)` 只生成内容，拒绝危险路径字符。

- [x] 编写真实临时配置/证书测试，Nginx 子进程和面板检查使用现有替身：首次切换成功、同版本不重载、换版本保留旧版、检查失败恢复之前配置、重载失败/pending 拒绝重试、人工修改/归属变化/暂停/证书改动停止、未配置 HTTP/无收据/禁写拒绝。

```python
assert tls.configure(IDENTITY, 22, version.name, lambda: True, now=NOW)
assert 'listen 443 ssl;' in config.read_text()
assert not tls.configure(IDENTITY, 22, version.name, lambda: True, now=NOW)
```

- [x] 测试缺少实现失败后，实现固定模板与事务：读取创建收据/上次 TLS 完成收据；验证当前配置；检查证书与 nginx -t；写 TLS pending；guarded_config 替换；再次检查，失败时受控恢复；再次核对证书/归属/当前配置；重载；排他写首次完成收据或 guarded_config 更新已有收据；最后删除自己的 pending 并 fsync。
- [x] 记录变更前后内容/版本在私有 pending 中，不保存私钥。`guarded_config` 用独立 pending 路径，避免提前清除外层 TLS pending。
- [x] 全量 Python/JS 回归、独立审查、更新进度并提交；真实 Nginx TLS 语法/握手/重载需后续受控验收，不以替身冒充。

## 本地验证记录（2026-09-22）

- 先观察 load 缺少方法、TLS 缺少模块的失败，再实现。TLS 文件事务 36 项通过；新增 load 本地测试 12 项通过，另 5 项链接/权限检查在 Windows 跳过。
- 全量 Python 351 通过、26 平台跳过、2 项既有弃用警告；JavaScript 8 通过，pip check 通过。
- 独立只读审查未发现 Critical/Important；按建议补充续期收据替换前后失败和 load 的链接/权限测试。复用已有文件保存原语，不引入新依赖。
- 保留隔离工作树和默认禁写门禁。没有服务器配置变更或发布包更新；本地 Nginx 子进程为替身，真实 TLS/续期验收仍待后续执行。

## Self-review

此任务覆盖已批准的 HTTPS 配置步骤，不改变 CA 或自动建站授权范围。没有引入新的事务框架；复用现有保存旧配置的文件操作。证书命令配置审计、续期、worker 联调和线上验收继续保留未完成状态。

## Linux 局部验收补充（2026-09-22）

- [x] 经独立复核后，在宝塔终端执行摘要核验过的 v2 隔离脚本：11 项 load 检查、9 次真实 Nginx 合法配置检查、错误密钥拒绝、4 项文件事务场景通过。
- [x] Nginx 仅检查改用私有 Unix socket 的配置副本；实际 reload 由替身截断。没有修改现有站点或申请证书。完整哈希、临时目录、依赖版本与边界见 `outputs/ab-lab/deploy/PROVISIONING-PROGRESS.md`。
- [ ] 真实域名 TCP/HTTPS 握手、线上重载、CA 签发、续期以及 worker 端到端验收。以上局部测试不能替代这些步骤。
