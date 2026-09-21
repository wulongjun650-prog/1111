# 多域名开发预览版（非自动建站完成版）

## 当前能用什么

后台有域名管理和站点选择器。每个已登记域名有独立内容、规则、放行后 A/B 选择、发布版本、链接、计数及日志。原站与账号保留不变。公网访客按登记 Host 选择站点，未知 Host 拒绝；新站初始无页面，不复制原站内容。

这是 **v2 开发预览包**，不是已通过宝塔验收的正式升级包。最关键的“解析完成后自动在宝塔建站、创建入口目录、配置反代和签发/续期证书”尚未完成。目前只实现 DNS 检查及面板单项只读能力检查，禁止任何面板写入。不要直接替换正在生产使用的 v1.1；建议用备份副本做本地验收。

## 本地验收

1. 解压到新目录，安装 requirements.txt，运行 `python run.py --data <独立测试数据目录>`。
2. 后台「域名管理」登记两个演示域名，例如 `one.example.com`、`two.example.com`。无需真实 DNS。
3. 选择第一个域名，上传并发布自己的 A/B 页面，选择放行后 A。
4. 选择第二个域名，确认页面为空、默认放行后 B。分别配置并验证互不影响。
5. 本地测试入口为 `http://127.0.0.1:8766/_sites/<站点ID>/`。真实公网模式只允许域名访问；此路径仅在原访客域名提供带签名的版本预览。

本地路径模式的根相对资源（如 `/style.css`）会落在原站，故本地和签名预览需使用相对资源路径。正式精确域名入口才是完整静态站点的验收方式。

## 数据与备份

旧数据继续位于原 data_dir，账号不动。首次登记旧站前自动对旧 `app.db` 做 SQLite 一致性备份到 `backups/pre-multidomain.db`，包括 WAL 已提交内容。此备份只是数据库迁移保险，不包含页面文件，**不能代替完整备份**。

新增 `registry.db` 记录域名；新站业务数据为 `sites/<不可变ID>/app.db` 与 pages/。站点数据目录在首次访问管理业务时初始化。站点ID不从域名/请求头构造路径。目录仍必须在网站公开根目录之外。

升级前停止本程序 admin/target（如已启动 dns 也停止），完整备份 `/opt/ab-lab`、`/etc/ab-lab` 和 `/var/lib/ab-lab`，并保留旧 v1.1 压缩包。不要删除旧数据，不要重新运行只支持首次安装的 install.sh。未来经服务器验收后的升级会更新整个 ablab、static、templates 和 run.py；不要只替换个别文件或覆盖 config.json。

回滚时先停止本程序的所有服务，再恢复旧代码和备份配置；原站业务表没有本轮结构变化。保留新增 sites/ 与 registry.db 以免丢失新站数据，v1.1 不读取它们。涉及中途业务写入时先另做完整快照，再选择恢复点。

## 可选 DNS 检测（不会建站）

`/etc/ab-lab/config.json` 可增加 `"server_ip": "你的服务器公网IPv4"`，不接受私网、回环或多播地址，不根据截图/浏览器自动填写。不要改原 admin_origin、target_origin 或 data_dir。

`python run.py --service dns --config /etc/ab-lab/config.json` 每约60秒扫描登记任务，公共解析暂不符合条件时指数退避到最多一小时。DNS 成功后明确显示“面板接口待验证”，不会显示已接入。暂停会停用访客访问和检测，不删除站点；重试恢复访问并重新排队，仍不会建站。

如在测试服务器运行常驻检测，可把随包 `deploy/ab-lab-dns.service` 安装到 `/etc/systemd/system/ab-lab-dns.service`，随后 daemon-reload、enable --now ab-lab-dns。这是可选 DNS 只读服务，与 Web 共用应用用户；**不是持有面板密钥的独立建站服务**。

DNS 查询使用固定 Google Public DNS HTTPS API，发送域名与记录类型，设置 edns_client_subnet=0.0.0.0/0。TLS 校验开启、不跟随重定向、单请求超时5秒、响应大小有上限。A 记录必须全部为预期 IPv4，存在任何 AAAA 则阻止后续；这是单一公共递归解析视角，不能保证全球传播完成。

## 面板兼容性阻塞点

`python deploy/inspect_panel.py` 只在终端询问面板地址和密钥，不保存密钥，仅请求 GetPHPVersion。不要把密钥放到网页、聊天或安装包。远程面板必须是可验证 HTTPS，不提供跳过 TLS 验证的选项。脚本只证明已知 v2 响应中存在 Static/00，不能证明面板整体兼容；当前无论检查结果如何都不启用写入。

在目标 aaPanel/宝塔8.0.4完成如下核对之前，不能实现并宣称“一改 DNS 就全自动”：反代创建/修改精确接口；检查配置并安全重载的返回契约；域名别名冲突的完整枚举；HTTP-01申请、失败退避和续期；外部调用超时后站点所有权核对；不影响现有25个站点的实际验证。后续需要对应版本的脱敏接口资料或经用户授权的独立测试站联调。不能上传含 Cookie、API 密钥、认证签名的原始 HAR。

参考：[aaPanel 官方 API](https://www.aapanel.com/docs/api/api-list.html)、[网站接口](https://www.aapanel.com/docs/api/site.html)、[Google DNS JSON API](https://developers.google.com/speed/public-dns/docs/doh/json)。参考文档与目标版本的真实兼容性不是同一件事。
