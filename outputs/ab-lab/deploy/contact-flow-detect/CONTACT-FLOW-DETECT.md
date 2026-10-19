# contact-gate 落地页 ABC / CBA 自动识别

给 `getContactFlowMode` + `#contact-gate` 这种新落地页用。旧版 `CONFIG.directMode` 逻辑不变。

## 页面里两种模式（和 URL 一致）

| 页面模式 | 访客体验 |
|----------|----------|
| **ABC** | 先弹 `#contact-gate` 问卷，再进 WhatsApp |
| **CBA** | 不弹窗，点按钮直达 WhatsApp |

URL：`?code=ABC` / `?code=CBA`（或 `?mode=`）。页面脚本默认 `'ABC'` 时，上传后控制台显示 **CBA**（有问卷）。

## 控制台「秘密武器」命名（和页面相反）

| 控制台 | 含义 | 对应页面 `CONTACT_FLOW_DEFAULT` |
|--------|------|----------------------------------|
| **ABC** | 已取消弹窗，直达 WhatsApp | `CBA` |
| **CBA** | 问卷已换回 | `ABC` |

上传 zip 并发布 B 页后，`/api/state` 的 `direct_mode` 会自动识别；秘密武器可只改 `SETTINGS.CONTACT_FLOW_DEFAULT`，不动其它 JS。

## 部署

1. 用本分支 `outputs/ab-lab/ablab/redirects.py` 覆盖服务器 `/opt/ab-lab/ablab/redirects.py`（或只替换 `read_direct_mode` / `set_direct_mode` 及相关 helper）。
2. `python -m py_compile ablab/redirects.py`
3. `systemctl restart ab-lab-admin`（不必重启 `ab-lab-target`）
4. 重新上传或发布一次 B 页，确认 `direct_mode` 与秘密武器切换正常

## 落地页建议（可选）

在 `SETTINGS` 里预留一行，方便控制台写入：

```javascript
CONTACT_FLOW_DEFAULT: 'ABC',
```

`getContactFlowMode` 里 fallback 建议写成：

```javascript
const code=String(params.get('code')||params.get('mode')||SETTINGS.CONTACT_FLOW_DEFAULT||'ABC').trim().toUpperCase();
```

没有这两行也能识别；第一次点秘密武器时会自动注入。

参考完整页：`cursor/cta-bounce-activity-ticker-21de` 分支的 `index.html`。
