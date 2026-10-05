import { normalizeSelection } from './locale.mjs';

export function patchConfig(config, patch) {
  return Object.assign(structuredClone(config), patch);
}

export function sitePath(path, siteId) {
  if (!/^(default|[a-f0-9]{32})$/.test(siteId)) throw new Error('站点 ID 无效');
  if (/^\/api\/(state|config|upload|publish|source|b-redirects|preview|links|counters|simulate|logs|audit|analytics|tracking)([/.?]|$)/.test(path)) {
    return `/api/sites/${siteId}${path.slice(4)}`;
  }
  return path;
}

function list(value) {
  return [...new Set(String(value ?? '').split(/[,，\n]+/).map(item => item.trim()).filter(Boolean))];
}

export function rulesConfig(config, values) {
  const next = structuredClone(config);
  const countries = normalizeSelection('countries', values.countries);
  const rules = { ...next.rules, countries, languages: normalizeSelection('languages', values.languages) };
  for (const key of ['blacklist', 'whitelist', 'blocked_cidrs', 'bot_markers']) rules[key] = list(values[key]);
  for (const key of ['block_bots', 'strict_bots', 'block_ipv4', 'block_pc']) rules[key] = Boolean(values[key]);
  for (const [key, label, minimum, fallback] of [['min_android', 'Android 版本', 0, 0], ['min_ios', 'iOS 版本', 0, 0], ['max_visits', '访问次数', 0, 0], ['window_hours', '时间窗口', 1, 24]]) {
    const number = Number(values[key] ?? fallback);
    if (!Number.isInteger(number) || number < minimum) throw new Error(`${label}须为有效整数。`);
    rules[key] = number;
  }
  next.rules = rules;
  return next;
}

export function parseLinks(value) {
  const lines = String(value).split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (!lines.length) throw new Error('请至少填写一个链接。');
  return [...new Set(lines.map((line, index) => {
    let url;
    try { url = new URL(line); } catch { throw new Error(`第 ${index + 1} 行不是完整的网址。`); }
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error(`第 ${index + 1} 行请使用不含账号密码的 HTTP(S) 链接。`);
    return url.href;
  }))];
}

export function previewURL(value, target) {
  const base = new URL(target);
  const url = new URL(value, base);
  if (!['http:', 'https:'].includes(url.protocol) || url.origin !== base.origin || url.username || url.password) throw new Error('预览地址不属于测试站点，已停止加载。');
  return url.href;
}

export function logPath(value) {
  try { return new URL(String(value || '/'), 'http://log.invalid').pathname; }
  catch { return String(value || '/').split(/[?#]/)[0]; }
}

export function validateUpload(file) {
  if (!file || !/\.(zip|html?)$/i.test(file.name)) throw new Error('请选择 ZIP 或 HTML 文件。');
  if (!file.size) throw new Error('文件为空，请选择有效的站点文件。');
  if (file.size > 20 * 1024 * 1024) throw new Error('文件超过 20 MiB，请精简后重试。');
}
