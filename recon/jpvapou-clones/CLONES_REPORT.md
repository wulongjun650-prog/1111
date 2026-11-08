# jpvapou 同款 / 同源站侦查（2026-11-08）

## 目标定义

与 **www.jpvapou.com**「一模一样搭建」= 同一套 **ASP.NET MVC 5.2** 白标物流/电商后台，至少满足以下 **强指纹** 之一：

| 级别 | 判定条件 |
|------|----------|
| A（同源） | `GET /Client/GetClientInfoByID?ID=` 无登录返回 `{"Code":1,"Msg":"","Data":...}`，且含字段如 `NuclearProtesterFee`、`WareHouses`、`TaoBaoUid` |
| B（同款 UI） | `GET /Admin/Login` 含 `tailwindcss.js` + `fetch(...'/Admin/AdminLogin'...)` + 标题「系统登录 - 后台管理系统」 |
| C（弱相关） | 仅 `X-AspNetMvc-Version: 5.2` + `/Admin/Login` 存在，但无上述 API（可能是其它 .NET 系统） |

参考基线：`recon/jpvapou.com/README.md`。

## 基础设施指纹（jpvapou）

- DNS：`www.jpvapou.com` → CNAME `a1113044.us.hygjcdn.com` → **15.204.67.21**（OVH US）
- 响应：`Server: Microsoft-IIS/10.0`，`X-AspNetMvc-Version: 5.2`，`X-Cache: MISS from web server ovh-2`
- Cookie：`ClientInfo`（客户端）、`PersonnelInfo`（后台）
- 静态资源：`/Content/tailwindcss.js`、`/Content/ICO.ico`（MD5 `8363acaeab9cbb099b59b78a44127ca6`）
- 同源 IP TLS 证书 SAN 另含 **abuygmail.com**（同机多站；实测为 nginx 落地页，**非** jpvapou API 栈）

## 已使用方法与结果

| 方法 | 规模 | 同款命中 |
|------|------|----------|
| 反查 IP 15.204.67.21（Hackertarget，约 388 域名）+ HTTP 指纹 | 388 | **仅 www.jpvapou.com** |
| 反查合并 15.204.67.21/20 + 107.x + 23.x，清洗后 236 域名 | 236 | **0**（无第二实例） |
| RapidDNS 同 IP / hygjcdn 子域 | 100+ CDN 节点名 | 0 客户域名 |
| 字典爆破（jpv*/hygj*/vapou* 等）+ 指纹 | 320+ | **仅 www.jpvapou.com** |
| 公网搜索（Brave/DDG/GitHub/Leakix）`GetOrderDetailByMainOrder`、`NuclearProtesterFee` | — | 无公开索引 |
| urlscan / grep.app | — | 无结果 |
| **jcwljt.com** | 人工 | AspNet 5.2 但只有 `/Admin/Login` 老页面，**无** `/Client/*`，**排除** |
| **Kingtrans（易宇通）** | — | **Java/Tomcat**，Cookie `ClientInfo` 名相似但栈不同，**排除** |
| **admin.hyx99.cn** | — | 同标题文案的 nginx 后台，**无** jpvapou API，**排除** |

### 结论（当前可公开验证范围）

在无需 FOFA/Shodan 付费 API 的前提下，**未发现第二台与 jpvapou 共享 A 级 API 指纹的线上域名**；唯一确认实例仍为 **http://www.jpvapou.com**。

推断：该系统为 **按客户独立域名 + hygjcdn 前置** 的私有部署/SaaS 单租户实例；克隆站不会共用手法明显的固定域名，需用 **被动 DNS / 网络空间搜索引擎** 按 body 或证书批量搜。

## 建议 FOFA / Shodan 语句（有账号时）

```
body="/Client/GetClientInfoByID"
body="NuclearProtesterFee"
body="GetOrderDetailByMainOrder"
title="系统登录 - 后台管理系统" && body="tailwindcss.js"
header="X-AspNetMvc-Version: 5.2" && body="AdminLogin"
cert="abuygmail.com"
cname="us.hygjcdn.com"
```

对命中域名执行：

```bash
python3 recon/jpvapou-clones/fingerprint_probe.py --file candidates.txt --json-out hits.json
```

## 易混淆项（不是同款）

- **华运国际物流（cntrans.cn / sofreight.com）**：正规货代，与 `hygjcdn` 字符串无直接证据链，勿与 jpvapou 栈等同。
- **同 IP 海量站**：多为 CDN 共用 PoP，与 jpvapou 业务无关。

## 工具

- `fingerprint_probe.py`：批量探针（见上）
- `fofa_shodan_search.py`：FOFA/Shodan 语句批量拉域名（需 API 密钥，见 `RUNBOOK.md`）
- `candidates_rev_sanitized.txt` / `hits_rev.json`：无 API 反查候选与探针结果（当前 `hits_rev.json` 为空）

## FOFA 网页验证

对 `body="/Client/GetClientInfoByID"` 的公开结果页需登录，且提示 **824001：body 语法需注册用户** → 必须使用 FOFA API + 账号密钥后才能做 body 级全网搜。
