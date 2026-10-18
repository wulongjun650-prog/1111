# genspicy.ai / openlover.ai 抓取执行报告

执行环境：美国出口 IP（`3.226.203.3`），无需代理即可访问主站。  
产出根目录：`/workspace/capture/`

## 1. genspicy.ai 主站（Nuxt）

| 项 | 结果 |
|---|---|
| 技术栈 | `x-powered-by: Nuxt`（非交接文档推测的 Next.js） |
| Sitemap 页面 | 174 条 URL 抓取（`sitemap-pages` 43 + `sitemap-drama` 98 + `sitemap-templates` 34，其中 1 条 404：`/template/anal`） |
| `/_nuxt` 静态资源 | 96 个文件 |
| 体量 | ~18 MB |
| 路径 | `capture/genspicy.ai/`（含 `robots.txt`、`favicon.ico`、各 `sitemap-*.xml`） |
| HTML 链接 | 同域绝对 URL 已改为根相对路径 |

## 2. openlover.ai 主站（Nuxt）

| 项 | 结果 |
|---|---|
| Sitemap 页面 | 69 条 URL，全部 200 |
| `/_nuxt` 静态资源 | 235 个文件 |
| 体量 | ~9.2 MB |
| 路径 | `capture/openlover.ai/` |
| 备注 | 部分路由 SSR 返回 “Page Not Found” 壳页（如 `/explore/`），与线上行为一致 |

## 3. admin.genspicy.ai 管理后台

| 项 | 结果 |
|---|---|
| 状态 | 已完整拉取（与交接 §5 一致，本环境复核） |
| 文件 | `index.html`、`assets/index-Df_y4w_R.js`（~1.07 MB）、`assets/index-Db-q816E.css`（~362 KB） |
| 路径 | `capture/admin.genspicy.ai/` |

## 4. API 探测

### api-node.openlover.ai（Node/Express 管理 API）

- `GET /admin` → `{"code":200,"data":"Hello World!","message":"success"}`
- Swagger/OpenAPI 常见路径（`/admin/docs`、`/swagger`、`/openapi.json` 等）→ 均为 JSON `Cannot GET`，**未发现公开 API 文档**
- 交接所列 30 条 `/admin/*` 路由：未认证 GET 多为 `Cannot GET`；`/admin/genspicy/health` 返回需 `access_token`
- 完整探测日志：`capture/api-probe/probe-report.txt`

### api.openlover.ai（用户 API 网关）

- 根路径及 `/docs`、`/swagger` 等为 GCP `404 backend NotFound`
- `GET /api` → `{"code":"1404","message":"NotFound","data":null}`

### api.genspicy.ai（Go 服务）

- 探测路径均为纯文本 `404 page not found`

### 前端 bundle 提取的 API 路径

- 从三端 JS 共提取 **159** 条 `/api/...` 路径：`capture/api-probe/frontend-api-paths.txt`
- 对 genspicy 同源 API 抽样探测：`capture/api-probe/genspicy-api-sample.txt`  
  例：`/api/analytics/country` → 200；`/api/home/cached-generation-template-list` → 200 JSON

## 5. GCS 桶 openlover-public（cdn.genspicy.ai / cdn.openlover.ai）

| 项 | 结果 |
|---|---|
| 完整对象清单 | **8,554,501** 条 key |
| 清单文件 | `gcs-openlover-public/bucket-objects-full.txt`（约 890 MB） |
| 压缩清单 | `gcs-openlover-public/bucket-objects-full.txt.gz` |
| 顶层前缀统计 | `gcs-openlover-public/bucket-prefix-summary.txt`（61 个顶层前缀） |
| 主要前缀 | `genspicy-uploads`（2.16M）、`cf-outputs`（1.56M）、`video-outputs`（1.43M）、`spicy`（1.26M）等 |
| 根对象样本 | 已下载至 `gcs-openlover-public/objects/root/` 与 `armor/`（含 403 封锁页、meta、渗透测试遗留 json 等） |

**说明**：清单为列举结果；未批量下载 800 万+ 用户生成媒体文件（交接建议按需下载）。

## 6. 打包

- 归档：`/workspace/genspicy-openlover-capture.tar.gz`（含 `capture/` 全目录，桶清单为 `.gz` 形式以减小体积）

## 7. 工具脚本

- `capture/scripts/mirror_nuxt_site.py` — Sitemap + `/_nuxt` 递归镜像
- `capture/scripts/gcs_list_bucket.py` — GCS XML 分页列举
- `capture/scripts/api_probe.sh` — 管理 API 与文档路径探测
