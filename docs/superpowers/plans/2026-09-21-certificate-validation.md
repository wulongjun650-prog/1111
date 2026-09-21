# Certificate validation Implementation Plan

> **For agentic workers:** Use executing-plans for the sequential implementation and requesting-code-review before acceptance. Steps use checkbox syntax.

**Goal:** 验证真实 X.509 证书材料，安全读取 Certbot live/archive 文件，不签发或部署证书。

**Architecture:** `CertificateVerifier` 只校验内存中的 PEM，明确区分生产和 staging 信任根；`read_certbot_material` 只读取固定站点的两个文件。部署模块必须使用读出的同一份已验证字节，不能重新从 live 路径读取。当前不接通 ManagedPanel 的上线门禁。

**Tech Stack:** Python 标准库、cryptography==50.0.1（PyPI 与官方版本文档已核对）、现有 pytest。新增依赖用于正式 X.509 路径验证，避免自写签名/证书链算法。

**Spec:** docs/superpowers/specs/2026-09-21-isolated-certificates-design.md

## Global Constraints

- 不修改面板 API、真实 CA、现有证书或 ZIP 发布包；补充 Linux 验证仅在服务器新建私有临时夹具目录运行读取器探针。
- 精确单域名 SAN；验证当前有效期、TLS server 用途、可信链、公私钥匹配。
- 信任根由特权部署配置提供，不能从待验证 fullchain 或网页输入取得；生产和 staging 根集合不得重叠。
- 私钥不出现在 repr、异常消息或日志。不可读取、越界、链接异常和权限异常全部拒绝。
- Certbot live 文件的直接链接是唯一允许的链接；所有父目录和 archive 目标禁止链接。同一次读取必须属于同一归档代次。
- POSIX 才有 UID/权限位强制验证；Windows 开发测试不能替代 Linux 权限验收。调用者需持有 worker 锁，私有目录由 root 控制，不声称防止恶意 root 并发修改。

## Task 1: 内存证书校验

Files: create `outputs/ab-lab/ablab/certificates.py`, `outputs/ab-lab/tests/test_certificates.py`; modify `outputs/ab-lab/requirements.txt`.

Interface: `CertificateVerifier(production_roots: bytes, staging_roots: bytes)`; `verify(identity, environment, fullchain: bytes, private_key: bytes, *, now=None) -> VerifiedCertificate`。结果 frozen dataclass 保存 domain/environment/fingerprint/not_after/fullchain/private_key，PEM 字段 repr=False。`require_production()` 拒绝 staging，仅作为内部部署前检查，不视为实际 HTTPS 验收。

- [ ] 使用测试中现场生成的 EC 根 CA/中间 CA/服务器证书写失败用例：正确链与匹配密钥成功；额外/错误/缺失 SAN、过期、尚未生效、错误私钥、不可信根、断链、错误签名、staging 送生产均拒绝。

```python
verified = verifier.verify(IDENTITY, 'production', chain_pem, key_pem, now=NOW)
assert verified.domain == 'new.example.com'
assert 'PRIVATE KEY' not in repr(verified)
with pytest.raises(ProvisioningError):
    verifier.verify(IDENTITY, 'staging', chain_pem, key_pem, now=NOW)
```

- [ ] 运行 `python -m pytest tests/test_certificates.py -q`，观察因缺少实现失败。
- [ ] 实现有大小上限的 PEM 解析、精确 SAN、公钥 DER 对比、默认 webpki `PolicyBuilder().store(Store(roots)).time(now).build_server_verifier(DNSName(domain)).verify(leaf, intermediates)`，保留完整有序可信链、归一化输出私钥 PKCS8；所有材料解析/验证异常转为脱敏 ProvisioningError，不更改默认 PKI 策略。
- [ ] 重新运行测试，确认通过；更新固定依赖并确认 pip check。

## Task 2: Certbot 专用读取器

Files: extend `outputs/ab-lab/ablab/certificates.py`, `outputs/ab-lab/tests/test_certificates.py`.

Interface: `read_certbot_material(base_dir: Path, environment, identity) -> tuple[bytes, bytes]`。base_dir 由特权 worker 固定为 `/var/lib/ab-lab-certificates`，测试使用 tmp_path；网页不得传入路径。

- [ ] 真实临时文件测试：合法 `config/live/ab-ID/{fullchain,privkey}.pem` 指向同站点 `config/archive/ab-ID/{fullchain,privkey}N.pem` 成功；跨站/跨环境/符号链接链、错代次、普通 live 文件、空/超大文件拒绝。

```python
chain, key = read_certbot_material(base, 'staging', IDENTITY)
assert chain == known_chain_pem
assert key == known_key_pem
```

- [ ] 先运行新增失败用例，再实现路径约束、唯一代次正则、lstat/open/fstat 常规文件检查、有界读取、POSIX 所有者与私钥 0600 检查。读取前后复核链接目标，任何变化拒绝。
- [ ] 全量 Python/JS 回归、独立代码审查，修复具体缺陷并复核；提交隔离分支、更新 PROVISIONING-PROGRESS，明确仍未实现证书落盘部署/配置审计/timer/真实验收。

## 自查与执行

Task 1 消费字节、Task 2 产生字节，接口不互相依赖文件格式之外的内部状态。两个任务属于已确认设计第 6 步，未扩大线上权限。保留既有 Nginx no_symlinks 边界不变。

## 执行结果

- [x] Task 1：先观察缺失实现的失败，再完成 23 项真实 X.509 用例；固定 cryptography 50.0.1，pip check 通过。
- [x] Task 2：读取器及失败用例已实现；Windows 缺少符号链接权限，14 项链接测试和 3 项 POSIX 权限测试跳过，不宣称为本地通过。
- [x] 真实 Linux 补充验证：从当前源码提取读取器的标准库探针 18 项通过，额外覆盖 FIFO；夹具保留于 `/tmp/ab-lab-cert-read-cme30nze`，没有读取线上私钥或改站点。提取源码 SHA-256：`c32059753fdf949091b7547e4d414e307f07af6b0b42a927b5e1fa3135f3cad1`。
- [x] 独立只读审查未发现 Critical/Important 问题。全量 266 项 Python 通过、17 项平台跳过、2 项既有弃用警告；8 项 JavaScript 通过。
- [x] 进度记录保存，随本阶段代码提交。

Linux 探针只验证文件读取层，不等同于 Linux 完整 X.509/worker 验收。证书版本落盘、专用配置审计、TLS 配置、timer 和真实建站验收仍未实现。
