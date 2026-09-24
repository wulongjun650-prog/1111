# Certificate Deployment Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans and superpowers:test-driven-development. Steps use checkbox syntax for tracking.

**Goal:** 组合已完成的 Certbot、材料读取、不可变版本和 TLS 事务，并把首次证书处理接入受管面板；最终 HTTPS 验收继续失败关闭。

**Architecture:** 新增一个小型特权编排对象，只负责“执行一次申请”和“从现有 Certbot 结果部署”两条路径。`ProvisioningWorker` 提供每个写入边界都可调用的暂停/世代回调；`ManagedPanel` 再叠加面板归属检查。命令结果未知时不重试，后续恢复只允许读取现有材料并部署。

**Tech Stack:** 现有 Python、pytest、Certbot 命令边界、证书读取器、`CertificateVersions` 与 `TlsEntry`；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-09-21-isolated-certificates-design.md`

## Global Constraints

- 只接受 production 命令；staging 演练和正式账户初始化是后续独立步骤。
- 默认禁写；不创建 CA 账户、不接受条款、不自动重试 Certbot。
- 每个外部命令、版本发布和 TLS 写入都必须重新核对 literal `True` 授权。
- 命令退出 0 后仍须读取、验证并发布真实材料；失败保留现场。
- 本任务不实现真实 HTTPS、路由和续期证明，`ManagedPanel.verify()` 继续失败关闭，站点不能进入 active。

---

### Task 1: Certificate deployment composition

**Files:**
- Create: `outputs/ab-lab/ablab/certificate_deployment.py`
- Create: `outputs/ab-lab/tests/test_certificate_deployment.py`

**Interfaces:**
- Consumes: `CertbotCommand.run()`, `read_certbot_material()`, `CertificateVersions.publish()`, `TlsEntry.configure()`.
- Produces: `CertificateDeployment.issue(identity, panel_id, authorize) -> str` and `deploy_existing(identity, panel_id, authorize) -> str`; returns the immutable version digest, not an acceptance proof.

- [x] Write failing tests for default-disabled behavior, production-only construction, successful issue/deploy, malformed material, pause between boundaries, command timeout and existing-material reconciliation without a second command.
- [x] Run the focused test and confirm failure because the module is absent.
- [x] Implement the smallest composition; keep material reader injectable only at this privileged internal boundary so tests never touch `/var/lib` or a CA.
- [x] Run the focused tests and full regression suite.

### Task 2: Managed panel and worker authorization wiring

**Files:**
- Modify: `outputs/ab-lab/ablab/managed_panel.py`
- Modify: `outputs/ab-lab/ablab/provisioning_jobs.py`
- Modify: `outputs/ab-lab/tests/test_managed_panel.py`
- Modify: `outputs/ab-lab/tests/test_provisioning_jobs.py`

**Interfaces:**
- `ManagedPanel(..., certificate_deployment=None)` preserves fail-closed default construction.
- `ManagedPanel.certificate(identity, panel_id, authorize)` combines current panel ownership with the worker pause/generation callback.
- Worker passes `lambda: self._current(site)`; a non-literal result or changed generation blocks the command or later writes.

- [x] Write failing tests proving the injected pipeline is called only for the exact owned site and receives a callback that stops after pause/generation change.
- [x] Update the worker test double and assert certificate intent still prevents a second issue attempt after interruption.
- [x] Implement minimal wiring. `verify()` may reconcile existing Certbot material after an uncertain command, but always fails closed until the three external proofs exist.
- [x] Run focused and complete tests, review the diff, update deployment progress, and commit.

## Verification Record

- TDD red: 11 tests failed because `ablab.certificate_deployment` did not exist; managed-panel/worker interface tests then failed because the new authorization parameters and recovery path were absent.
- Focused green: 93 certificate deployment, Certbot command, managed panel and worker tests passed.
- Full local regression: 458 Python tests passed, 33 platform-specific tests skipped on Windows, with 2 existing dependency deprecation warnings; 8 JavaScript tests passed. `pip check` and `git diff --check` passed.
- The real-composition test uses a local test CA and real certificate verification/version/TLS file transactions. It does not contact a CA or reload the server.
- Independent review found two Important races. Authorization is now checked both before and after panel inspection. A dedicated `ProvisioningNotStarted` result distinguishes pre-launch denial/audit failure from an uncertain subprocess result; only the former restores `certificate_intent` to `configured` for safe retry. Once Certbot has returned, later cancellation is converted back to an uncertain deployment result so it can never authorize a second order. Re-review found no remaining Critical or Important issues.

## Not Completed By This Plan

- Private CA account creation, current Terms of Service acceptance, real staging or production issuance.
- HTTPS handshake/route proof, renewal dry-run proof, independent renewal timer and final active transition.
- Server deployment or modification of existing BaoTa sites.
