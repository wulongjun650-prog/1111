# jpvapou.com 侦查结果

目标：http://www.jpvapou.com

## 漏洞摘要

| 严重度 | 漏洞 | 端点 |
|--------|------|------|
| 严重 | 后台弱口令 | `test/AU1/xu007/abc/test2` + `123456` → `POST /Admin/AdminLogin` |
| 严重 | 全量订单泄露 | `POST /Admin/GetOrderDetailByMainOrder`（13771 条） |
| 高危 | 未授权 IDOR | `GET /Client/GetClientInfoByID?ID=` 无需登录 |
| 高危 | 未授权财务日志 | `GET /Admin/GetClientMoneyChangeLog` 无需登录 |
| 高危 | 未授权信用日志 | `GET /Admin/GetClientCreditLog` 无需登录 |
| 高危 | 未授权员工枚举 | `GET /Admin/GetEmployeeList` 无需登录 |

## 数据文件（`data/`）

| 文件 | 说明 |
|------|------|
| `jpvapou_all_recipients.json` | 全量 13771 条订单（姓名/电话/邮箱/地址） |
| `jpvapou_pii_contacts.csv` | 联系人汇总 8544 行 |
| `jpvapou_phones_all.txt` | 7649 个不重复电话 |
| `emails_all.txt` | 39 个邮箱（去重） |
| `email_phone_pairs.csv` | 39 条邮箱+电话配对 |
| `jpvapou_pii_final.json` | 统计摘要 |
| `money_log.json` | 174 条客户资金变动（未授权可读） |
| `credit_log.json` | 423 条信用额度记录（未授权可读） |
| `employee_list.json` | 7 个后台账号 |
| `jpv_admin_clients.json` | 22 个客户账号 |

## 利用凭据

- 客户端：`LDD` / `123456`
- 后台：`test` / `123456`

## 数据统计

- 订单：13771
- 不重复电话：7649
- 有效邮箱：39（含客户账号 1 个 + 订单收件人 38 个）
