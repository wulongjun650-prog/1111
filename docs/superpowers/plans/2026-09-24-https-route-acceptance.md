# HTTPS And Route Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans and superpowers:test-driven-development. Steps use checkbox syntax for tracking.

**Goal:** 用服务器指定 IP 上的真实 TLS 握手和应用内精确站点响应证明 HTTPS 与域名路由；续期证据仍保持失败关闭。

**Architecture:** 目标应用增加一个不经过上传内容的只读 `/.well-known/ab-lab-route/<site-id>` 响应。特权验收组件绕过公共 DNS，直连已配置服务器 IP，但使用站点域名作为 SNI 和 Host；系统信任链/主机名验证、已部署叶证书指纹和精确 JSON 路由身份必须同时匹配。`ManagedPanel.verify()` 复核现有证书部署后调用该组件；未注入续期证明时固定返回 `renewal: false`，因此 worker 不能进入 active。

**Tech Stack:** Python 标准库 `socket` / `ssl` / `http.client`、现有 FastAPI、证书不可变版本存储、pytest；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-09-21-isolated-certificates-design.md`

## Global Constraints

- 验收只读，不申请证书、不重载 Nginx、不修改宝塔站点。
- 连接目标必须是部署配置中的明确 IP；TLS SNI、HTTP Host 和证书域名必须是同一个规范域名。
- 使用系统默认 TLS 验证，不允许关闭链或主机名校验。
- 响应有严格大小上限，只接受 200、JSON、`no-store` 和精确站点身份。
- 每个慢边界前后复核 literal `True` 授权。
- 续期演练及其不可伪造的持久化收据不在本计划内；缺失时必须明确为 false。

---

### Task 1: Application-owned route proof

**Files:**
- Modify: `outputs/ab-lab/ablab/web.py`
- Create: `outputs/ab-lab/tests/test_route_proof.py`

- [x] 先写失败测试：正确 host/site 返回精确证明；错误 site、未登记 host、暂停站点和 HEAD 均不能伪造 GET 证明。
- [x] 在上传内容 catch-all 之前实现最小只读路由。
- [x] 运行聚焦测试。

### Task 2: TLS and route probe

**Files:**
- Create: `outputs/ab-lab/ablab/certificate_acceptance.py`
- Create: `outputs/ab-lab/tests/test_certificate_acceptance.py`

- [x] 先写失败测试：直连固定 IP、SNI/Host、系统 TLS context、证书指纹、响应状态/类型/大小/JSON 和授权变化。
- [x] 实现有界 transport 与验收组合；默认续期证据为 false。
- [x] 运行聚焦测试。

### Task 3: Managed panel wiring and verification

**Files:**
- Modify: `outputs/ab-lab/ablab/managed_panel.py`
- Modify: `outputs/ab-lab/tests/test_managed_panel.py`
- Modify: `outputs/ab-lab/deploy/PROVISIONING-PROGRESS.md`

- [x] 先写失败测试：证书恢复后传递当前不可变摘要；验收组件与部署版本存储必须一致；无验收组件继续失败关闭。
- [x] 实现最小接线并确认 worker 对 `renewal: false` 不会 active。
- [x] 跑聚焦和完整回归、独立审查、更新进度并提交。

## Verification Record

- TDD red：路由证明先返回 404，验收模块不存在，`ManagedPanel` 不接受验收组件；共观察到 37 项预期失败。
- 聚焦 green：路由、证书验收、受管面板和 worker 共 68 项通过。
- 完整本地回归：479 项 Python 通过、33 项 Windows 平台跳过、2 项既有依赖弃用警告；8 项 JavaScript 通过；`pip check` 与 `git diff --check` 通过。
- 独立只读复查核对固定 IP、SNI/Host、系统 TLS、证书指纹、上传内容隔离、暂停/TOCTOU 和失败关闭；未发现 Critical 或 Important 问题。
- 所有网络行为均为测试替身；没有连接真实 443、申请证书、重载 Nginx 或修改服务器。

## Not Completed By This Plan

- staging 证书账户、真实 `renew --dry-run`、续期收据和独立 timer。
- 生产 CA 账户/条款、真实证书申请、线上 443 验收或服务器部署。
- 任何现有宝塔站点的删除、接管或修改。
