# 独立证书命令边界 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 实现可独立验证的 Certbot 固定参数构造与受控执行边界，不提前启用未验收的在线签发。

**Architecture:** 本阶段只实现独立证书子系统的命令边界。沿用 validate_identity 和 ProvisioningError，采用不可变参数对象和标准库 subprocess。后续证书读取/验证、不可变部署、Nginx TLS、私有状态与续期调度依次接入；本阶段不能让 ManagedPanel 的未完成门禁变成成功。

**Tech Stack:** Python 标准库，现有 pytest。

**Spec:** docs/superpowers/specs/2026-09-21-isolated-certificates-design.md

## Global Constraints

- 不调用宝塔证书申请、同步、部署或续期开关。
- 独立生产与 staging 目录，精确一个域名，固定 cert-name `ab-<32位站点ID>`。
- 不使用 shell，不接受任意参数，不继承代理/PYTHONPATH/全局 CLI 配置或 hooks。
- 未经真实 CA 条款确认不能携带同意条款参数；本阶段不实现账户注册，签发必须使用预先登记的账户 ID。
- 执行默认关闭；调用者必须持有已有工作锁、持久化意图、检查归属和暂停状态。模块本身不是可以直接向网页暴露的 API。
- 不部署服务器、不发真实 CA 请求、不改现有站点，不将本地替身测试称作真实签发成功。

## Task 1: 固定 Certbot 命令

**Files:** Create `outputs/ab-lab/ablab/certbot_command.py`; test `outputs/ab-lab/tests/test_certbot_command.py`.

**Interfaces:** `CertbotCommand(environment, account_id)` frozen dataclass; `issue(identity) -> tuple[str, ...]`; `renew(identity, dry_run=False) -> tuple[str, ...]`。环境仅 production/staging；账户仅 32 位小写 hex；二进制固定 `/usr/bin/certbot`。该固定部署路径须在部署预检确认，不检测 PATH 或自动下载安装。dry-run 仅允许 staging 对象，避免携带生产账户 ID 去 staging CA；正式续期演练的编排须明确使用独立 staging 账户和记录。

- [ ] 写参数边界测试：issue 仅一个 -d，webroot 为 identity.path，cert-name 按 ID；生产/测试 server/config/work/log/CLI 文件完全隔离。拒绝未知环境、空/注入账户、无效 identity。renew 只接受 boolean dry_run、限定 cert-name，不能全局续期或 force-renewal。

```python
cmd = CertbotCommand('staging', 'b' * 32).issue(IDENTITY)
assert cmd[0] == '/usr/bin/certbot'
assert cmd.count('-d') == 1
assert cmd[cmd.index('-d') + 1] == 'new.example.com'
assert '--agree-tos' not in cmd
assert '--no-directory-hooks' in cmd
```

- [ ] 执行 `work/verify-venv/Scripts/python.exe -m pytest outputs/ab-lab/tests/test_certbot_command.py -q`（PYTHONPATH=outputs/ab-lab），确认新增用例因为缺少实现失败。
- [ ] 实现命令构造：固定 server 端点，所有目录固定于 `/var/lib/ab-lab-certificates/<environment>/`，显式 `--config <root>/cli.ini`、`--non-interactive --no-directory-hooks --account`。issue 使用 certonly/webroot；renew 使用 `renew --cert-name`，dry-run 显式附加。
- [ ] 重新执行测试，保证参数边界测试通过。自检无多余扩展点。

## Task 2: 有门禁的本地执行

**Files:** Modify `outputs/ab-lab/ablab/certbot_command.py`; extend `outputs/ab-lab/tests/test_certbot_command.py`.

**Interfaces:** `CertbotCommand.run(identity, *, operation, enabled=False, authorize, runner=subprocess.run) -> None`。operation 仅 issue/renew/dry-run。authorize 为调用者每次执行前的归属/暂停复核，必须返回 literal True。返回 None 只表示命令退出 0，不表示成功签发或续期。

- [ ] 先写失败测试：默认关闭/非布尔 enabled、不合法操作/账户不得调用 runner；authorize False/None/1 禁止执行。执行时验证参数数组、shell=False、timeout=180、stdin/stdout/stderr=DEVNULL、固定最小环境。超时/启动异常/非零返回均抛脱敏 ProvisioningError，不自动重试、不泄露命令输出。

```python
with pytest.raises(ProvisioningError):
    command.run(IDENTITY, operation='issue', authorize=lambda: True)
assert calls == []
```

- [ ] 运行新增用例确认先失败，再实现门禁和受控 subprocess.run，重新运行。
- [ ] 完整运行 Python 与 JS 测试；独立审查新文件和接口的安全边界。
- [ ] 更新 PROVISIONING-PROGRESS，明确此模块还不能直接接入线上：专用续期配置审计、账户登记/条款、真实证书检查、TLS 部署、状态恢复和 timer 尚未完成。Git 缺少身份时不擅自配置或伪造提交。

## 后续阶段（不在本命令边界的完成声明内）

1. 私有目录/账户与 Certbot renewal 配置审计，包括拒绝未知 hooks/installer；真实证书 SAN、时间、链、公私钥检查与安全 live/archive 读取。
2. 不可变证书部署版本，复用 guarded_config 加入 HTTPS 发布与恢复；接入 ManagedPanel 和私有任务状态。
3. 独立 worker/timer、暂停及超时恢复、真实 HTTP/HTTPS 路由和续期验收证据。
4. 安装预检与指定域名 staging 演练；正式 CA 条款确认后签发。已有站点删除重建仍需单独确认。

## 执行记录

2026-09-21：本阶段接口依赖紧密，主会话顺序实施并安排独立审查。用户明确选择先整理提交并隔离工作树：先核对源码、排除运行数据和凭据，提交当前基线后使用桌面原生工作树工具。Git 作者身份缺失，已询问是否沿用已有提交的 Codex <codex@local>，未确认前不代设身份或提交。新功能实现将在隔离目录进行。

隔离前基线验证：Python 209 项通过（2 项现有依赖弃用警告），JavaScript 8 项通过。候选源码文件的凭据模式检查未发现匹配；这不是完整安全审计。工作目录、虚拟环境、运行数据、缓存、抓取资料和发布 ZIP 已被现有 .gitignore 排除。

自查：两个任务共享唯一命令对象；Task 2 使用 Task 1 的固定参数构造，不新增任意命令入口。分阶段范围明确，完整证书设计未被声明为已完成。

2026-09-21 执行进度：用户批准本仓库使用 Codex <codex@local>；已保存基线 15d2bb6，并通过桌面原生工具创建隔离工作树，分支 codex/isolated-certificates，原目录保持干净。

Task 1 与 Task 2 代码/测试步骤已执行：先观察 16 项缺失模块失败，再通过；增加执行测试后观察 15 项缺失 run 方法失败，再全部通过。独立审查指出默认 CLI 配置仍会被读取，以及续期随机等待可能超过超时；补充测试观察 7 项失败后完成修正。目前 34 项专用测试、243 项全量 Python 测试与 8 项 JavaScript 测试通过，只有 2 项已有依赖弃用警告。修正独立复核通过，无 Critical/Important 发现；仅批准命令边界阶段，不批准线上启用。

未完成的生产前置条件不由本阶段的 authorize 回调自动实现：锁与意图、账户登记、CLI/renewal 配置审计及路径权限必须在集成阶段落地。没有部署本模块、启用入口或执行真实 Certbot。

审查修正：`--config` 不能独自阻止默认配置读取。现在执行前拒绝任何 `/etc/letsencrypt/cli.ini` 或固定 HOME/XDG 默认 CLI 文件（包括链接和不可核对的路径），不删除/覆盖这些文件。部署必须确认安装版本的默认搜索路径，保障祖先目录仅 root 可写；此预检不声称抵御并发 root 修改。专用配置和续期记录仍需单独审计。续期显式设置 `--no-random-sleep-on-renew`，未来 timer 负责随机延迟。

依据：Certbot 官方 `certbot/src/certbot/_internal/constants.py` 默认配置搜索路径和 `renewal.py` 的 1–480 秒随机等待（2026-09-21 核对 main 源码）；实际安装包须另行验证，不以源码替代服务器验收。
