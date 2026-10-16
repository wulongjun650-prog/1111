# KR10.16 落地页

## 文件说明

| 文件 | 说明 |
|------|------|
| `index.html` | **单文件版**：图片已 base64 内嵌，无需 `static/` 目录即可打开 |
| `index.source.html` | 原始 HTML（引用 `static/picture/`） |
| `static/picture/` | 原始图片资源 |
| `embed_images.py` | 从 `index.source.html` 重新生成内嵌版 `index.html` |

## 重新生成内嵌 HTML

```bash
python3 embed_images.py
```
