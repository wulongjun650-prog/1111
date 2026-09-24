# Renewal Rehearsal Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans and superpowers:test-driven-development. Steps use checkbox syntax for tracking.

**Goal:** 用隔离 staging 账户先建立受控 webroot lineage，再执行一次 `renew --dry-run`，将结果绑定当前生产证书版本并持久化为失败关闭的续期证明。

**Architecture:** 新增一个小型 staging 状态机，使用每个站点/生产证书摘要独立的私有 JSON 事务记录。外部命令之前先持久化 intent；未知结果不重试。首次没有 staging lineage 时只签发 staging 测试证书，随后严格审计其续期配置，再持久化 dry-run intent 并执行一次演练。只有命令成功、配置复核和动态授权均通过才提交 verified 收据。`CertificateAcceptance` 继续只接受 literal `True` 的续期证明。

**Tech Stack:** 现有 `CertbotCommand`、私有配置审计、受限文件操作、pytest；不新增依赖。

**Official behavior checked:** Certbot 官方指南说明 `--dry-run` 对 `renew`/`certonly` 使用 staging，获得但不保存测试证书；默认不运行 deploy hook。现有命令仍显式禁用目录 hooks，且 staging 目录/账户与 production 分离。

**Spec:** `docs/superpowers/specs/2026-09-21-isolated-certificates-design.md`

## Constraints

- 只接受 `CertbotCommand(environment='staging')`；不部署 staging 证书。
- 默认禁写；不注册账户、不接受条款、不推断邮箱。
- `issue_intent` 可通过已生成且审计通过的 staging renewal config 恢复；否则不重发。
- `dry_run_intent` 结果不可从文件可靠推断，自动流程永不重跑，须人工核对。
- 明确的命令启动前拒绝可安全回滚 intent；命令进入启动边界后的异常保留 intent。
- 收据绑定 identity、panel_id、staging account 和 production digest；换证后必须重新演练。

### Task 1: Durable rehearsal state machine

**Files:**
- Create: `outputs/ab-lab/ablab/renewal_rehearsal.py`
- Create: `outputs/ab-lab/tests/test_renewal_rehearsal.py`

- [x] 先写失败测试：默认禁用、首次 issue→audit→dry-run、已有 lineage、生产摘要变化、暂停、明确未启动、issue 未知结果恢复、dry-run 未知结果不重试、篡改和外部对象。
- [x] 实现最小追加/条件替换状态机，复用现有安全文件原语。
- [x] 跑聚焦测试。

### Task 2: Acceptance integration and regression

**Files:**
- Modify: `outputs/ab-lab/tests/test_certificate_acceptance.py`
- Modify: `outputs/ab-lab/deploy/PROVISIONING-PROGRESS.md`

- [x] 验证仅 literal `True` 收据完成第三项证明，异常/真值不误报。
- [x] 跑完整回归、独立审查、更新进度并提交。

## Verification Record

- TDD red：新增模块不存在时 16 项预期失败；独立审查指出命令固定目录与可注入审计目录可能分离后，新增单一 `command.root` 测试先以 8 项失败复现。
- 聚焦 green：Certbot 命令、配置审计、续期演练、验收、受管面板和 worker 共 198 项通过、7 项平台跳过；目录统一修复后核心 71 项通过。
- 完整本地回归（修复前基线）：500 项 Python 通过、33 项平台跳过、2 项既有弃用警告。最终文件状态另按完成前验证记录执行。
- 独立复查最初发现一个 Important：演练命令与审计可指向不同证书根。现在 `CertbotCommand` 的参数、cwd、lineage 和审计全部使用唯一只读 `command.root`，复审确认问题消除且无剩余 Critical/Important。
- 未调用真实 Certbot、CA、Nginx 或服务器；测试命令和 lineage 均为本地替身。

## Not Completed By This Plan

- 创建 staging/production CA 账户、接受当前 CA 条款或运行真实 CA 请求。
- 生产续期 timer、证书变化检测、更新部署和真实服务器演练。
- 特权 worker 服务装配、线上 443/80 验收或发布 ZIP。
