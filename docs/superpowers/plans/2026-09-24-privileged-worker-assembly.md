# Privileged Worker Assembly Implementation Plan

**Goal:** 把已验证的面板、证书、TLS、HTTPS 路由与续期演练组件装配为独立 root worker，同时让 Web/DNS 服务继续无权读取面板凭据和私钥。

**Design approval:** 已确认的独立证书设计要求专用特权 worker；上一阶段交付说明明确下一步为 worker/timer，用户回复“继续”。

**Minimal architecture:** `ManagedPanel` 增加只能调用一次的证书管线绑定；新私有装配模块只接受固定目录和严格 JSON 字段，读取 root-only 信任根与 aaPanel 本机配置。`run.py --service provision` 使用该装配循环处理登记站点。服务单元仅随包提供，不由现有安装器自动启用。

### Task 1: One-time certificate pipeline binding

- Modify `ablab/managed_panel.py`
- Modify `tests/test_managed_panel.py`
- [x] 先写失败测试：允许一次绑定、拒绝错版本存储、拒绝二次替换。
- [x] 最小实现并跑测试。

### Task 2: Strict private config and assembly

- Create `ablab/privileged_worker.py`
- Create `tests/test_privileged_worker.py`
- [ ] 先写失败测试：固定 schema/字段、literal enable、账户、loopback 面板、绝对数据路径、私有文件权限与未知字段。
- [ ] 装配 `PanelSites → ManagedPanel → CertificateVersions/TlsEntry → CertificateDeployment → CertificateAcceptance/RenewalRehearsal → ProvisioningWorker`。
- [ ] 读取生产/staging 信任根时使用现有受限读取器。

### Task 3: Disabled-by-default service entry

- Modify `run.py`
- Create `deploy/ab-lab-provision.service`
- Modify deployment tests/progress
- [ ] 新 service 必须显式私有配置且以 root 运行；现有 installer 不安装/启用它。
- [ ] 聚焦、完整回归、独立审查后提交。

## Not Completed

- 真实私有配置/账户初始化、CA 条款、服务器安装或服务启用。
- 生产证书日常续期与 timer（独立后续计划）。
