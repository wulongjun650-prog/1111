# Certificate versions Implementation Plan

> **For agentic workers:** Use executing-plans for this tightly coupled task, test-driven-development and requesting-code-review. Steps use checkbox syntax.

**Goal:** 保存校验通过的生产证书到独立、不可覆盖的版本目录，不修改线上配置。

**Architecture:** `CertificateVersions` 复用 CertificateVerifier、受限常规文件读取、排他文件发布和目录 fsync。站点归属记录先于版本保存，版本完成记录最后发布；任何残缺/冲突保留现场并停止。调用者必须持有现有 worker 锁，特权根目录由安装流程预建。

**Tech Stack:** Python 标准库、已有 cryptography 和 pytest；不新增依赖。

**Spec:** docs/superpowers/specs/2026-09-21-isolated-certificates-design.md，第 7 步。

## Global Constraints

- 根目录固定由特权部署配置提供，默认 `/var/lib/ab-lab-certificates`；网页不可指定路径。
- 生产目录 `production/deployed/ab-<site_id>/<sha256>`，目录 0700、文件 0600，拒绝链接/硬链接/不安全权限；Windows 仅作开发验证。
- 写入默认关闭；每次目录/文件写入前要求归属及暂停复核回调返回 literal True。
- 输入为原始 PEM，发布时重新验证为 production，不信任自行构造的 VerifiedCertificate。版本摘要覆盖归一化完整链和私钥，链变化也生成新版本。
- 站点归属记录包含完整 identity 和正整数 panel_id；复用站点/版本必须逐字节校验记录和材料，不接管、不修复、不覆盖、不自动清理。
- 完成记录最后写入，仅完整版本可返回；没有 active 指针，不改 Nginx，不调用 CA，不更新 ZIP。
- 不声称可以抵御同权限恶意管理员并发修改；依赖 worker 锁、私有目录和服务隔离。

## Task 1: Immutable version publication

Files: create `outputs/ab-lab/ablab/certificate_versions.py`, `outputs/ab-lab/tests/test_certificate_versions.py`; update progress document.

Interface: `CertificateVersions(verifier, base_dir=Path('/var/lib/ab-lab-certificates'), *, writes_enabled=False)`; `publish(identity, panel_id, fullchain, private_key, authorize, *, now=None) -> Path`。构造函数不写文件；返回已完成版本目录，未来 TLS 提交步骤必须再次验证归属/材料，不把 Path 当作验收证据。

- [x] 编写真实证书/真实临时文件失败测试：

```python
version = store.publish(IDENTITY, 22, chain, key, lambda: True, now=NOW)
assert (version / 'fullchain.pem').read_bytes() == chain
assert (version / 'privkey.pem').read_bytes() == key
assert (version / 'ready.json').is_file()
assert store.publish(IDENTITY, 22, chain, key, lambda: True, now=NOW) == version
```

覆盖默认禁写、非 True 授权、暂停、错误身份/面板 ID、staging/错误密钥、同站归属变更、旧版本不变、残缺/被修改版本不修复、写中断保留、权限和硬链接拒绝、符号链接拒绝。

- [x] 运行 `python -m pytest outputs/ab-lab/tests/test_certificate_versions.py -q`，确认因缺少发布能力失败。
- [x] 实现最小发布流程：

```python
verified = verifier.verify(identity, 'production', fullchain, private_key, now=now)
digest = sha256(verified.fullchain + b'\0' + verified.private_key).hexdigest()
# Require private base; create/check production/deployed directories.
# Create/check ab-ID owner.json first. mkdir(version) must not overwrite.
# If version already exists, validate every file and ready.json; never repair.
# Publish fullchain.pem, privkey.pem, ready.json in order using exclusive_text.
# Re-read exact bytes + receipt and sync version directory before return.
```

所有 I/O 异常统一脱敏为 ProvisioningError，保留已写内容；fsync 父目录确保新目录持久化。现有目录/文件不 chmod，拒绝不安全状态。
- [x] 运行目标测试、全量 Python/JS、独立复查。记录平台跳过与未完成的线上步骤，不将局部测试算作自动建站验收。
- [x] 更新进度，随代码保存到隔离分支。

## Self-review

本任务只覆盖已批准第 7 步；不扩大到证书安装、注册、TLS 切换或续期 timer。复用现有文件原语，不另建事务框架。站点级 owner 记录阻止不同证书版本绕过归属检查，ready 标记仅表示完整落盘，不表示已上线。

## 执行证据与剩余验收

- 先建立接口占位，目标测试 26 项因 NotImplementedError 失败；实现后稳定文件读取测试暴露 Windows path-stat / fd-stat 的 ctime 差异。
- 在真实临时文件复现：dev/ino/size/mtime/mode/nlink 一致，ctime 不同。仅在 Windows 跨 API 对比中略去 ctime；同 API 前后仍检查 ctime，Linux 跨 API 检查不放宽。独立新增回归先失败后通过。
- 独立只读审查未发现 Critical/Important 问题。补充两项 ctime-only 变化测试和九项撤权边界测试。内存中暂时禁用 ctime 检查时，两项回归均正确失败；未改磁盘实现。
- 最后目标测试 60 项通过、21 项 Windows 平台跳过。完整 Linux 探针生成器 `deploy/build_cert_versions_probe.py` 已准备，提取生产代码并现场生成测试 CA；不依赖 pytest，不联系 CA，不修改既有证书。
- 全量回归：303 项 Python 通过、21 项平台跳过、2 项既有弃用警告；8 项 JavaScript 通过；git diff --check 无空白错误。
- 浏览器页面读取/导航多次超时，本轮探针尚未上传或运行。旧读取器探针的 Linux 通过记录不覆盖这次修改后的读取器或新版本模块。
- 未接通 TLS 配置、配置审计、续期 timer、ManagedPanel 或后台入口；自动建站仍未完成，服务器现有站点与服务未改变。
