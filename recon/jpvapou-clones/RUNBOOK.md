# 同款站批量发现（FOFA / Shodan）

## 1. 在 Cursor 里绑定密钥（已发起绑定请求）

请在 **Cursor → Secrets** 中为目录 `recon/jpvapou-clones` 添加（不要把值贴到聊天里）：

| 变量 | 用途 |
|------|------|
| `SHODAN_API_KEY` | [Shodan Account](https://account.shodan.io/) → API Key |
| `FOFA_EMAIL` | FOFA 注册邮箱 |
| `FOFA_KEY` | FOFA 个人中心 → API Key |

说明：`body=` 类 FOFA 语法需**注册用户 + API**（网页搜也会提示 824001）。

密钥绑定后，Cloud Agent 宿主机仍可能读不到明文；本地或 CI 可执行下方命令，把 `candidates.txt` / `hits.json` 提交到仓库即可。

## 2. 一键搜索 + 指纹验证

```bash
cd recon/jpvapou-clones
export FOFA_EMAIL=... FOFA_KEY=... SHODAN_API_KEY=...   # 或从环境注入
python3 fofa_shodan_search.py --out candidates_fofa.txt --json search_results.json --probe
```

仅反查 IP（无 API）：

```bash
curl -sS 'https://api.hackertarget.com/reverseiplookup/?q=15.204.67.21' \
  | python3 -c "import sys,re; [print(x) for x in sys.stdin if re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', x.strip())]" \
  > candidates_rev.txt
python3 fingerprint_probe.py --file candidates_rev.txt --json-out hits_rev.json
```

## 3. 判定标准

- `same_api_idor`：**与 jpvapou 同源**（优先）
- `same_admin_ui`：同款后台 UI，需再验 API

## 4. 当前无 API 时的结论

见 `CLONES_REPORT.md`：反查合并约 **238** 可解析客户型域名 + **0** 个 A 级命中（除 `www.jpvapou.com`）。
