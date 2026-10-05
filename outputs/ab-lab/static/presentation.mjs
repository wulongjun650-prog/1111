import { options, countryLabel } from './locale.mjs';

const regions = new Set(options.countries.map(item => item.code));
export function themeFor(config) {
  if (!config) return 'loading';
  const slot = config.routing === 'FORCE_A' ? 'A' : config.routing === 'FORCE_B' ? 'B' : config.allowed_slot;
  return slot === 'A' ? 'angel' : 'demon';
}
export function flagPath(code) {
  const normalized = String(code || '').toUpperCase();
  return regions.has(normalized) ? `/static/flags/${normalized.toLowerCase()}.svg` : null;
}
export function outcomeFor(reason) {
  if (['allowed','pass','whitelist','protection_off'].includes(reason)) return 'allowed';
  return ['blacklist','strict_bot','bot_marker','ipv4','device','os_version','blocked_cidr','country','country_unknown','language','visit_limit'].includes(reason) ? 'blocked' : 'other';
}
export function reputationView(result, now = Date.now() / 1000) {
  let status = result?.status || 'unchecked';
  if (['clean','flagged'].includes(status) && !(typeof result.expires_at === 'number' && result.expires_at > now)) status = 'expired';
  const labels = {clean:'未检出风险',flagged:'发现风险',unconfigured:'未配置',unchecked:'未检测',unavailable:'不适用',error:'检测失败',expired:'结果已过期'};
  return {label:labels[status] || '未检测',tone:status === 'clean' ? 'green' : status === 'flagged' ? 'red' : 'neutral'};
}
export const reasonLabels = {blacklist:'命中黑名单',strict_bot:'严格防爬虫',bot_marker:'机器人 UA 标记',ipv4:'IPv4 限制',device:'设备限制',os_version:'系统版本限制',blocked_cidr:'屏蔽网段',country:'国家／地区限制',country_unknown:'国家未知',language:'语言限制',visit_limit:'访问次数超限'};
export function node(tag, className = '', text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}
export function svgNode(tag, attrs = {}) {
  const result = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attrs).forEach(([key,value]) => result.setAttribute(key,String(value)));
  return result;
}
const icons = {
  overview:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  content:'M4 4h16v16H4z M4 9h16 M9 9v11',
  rules:'M4 7h16 M4 17h16 M8 4v6 M16 14v6',
  simulate:'m9 3 6 0 M10 3v6L4 19q-1 2 2 2h12q3 0 2-2L14 9V3 M8 14h8',
  logs:'M7 3h10l3 3v15H4V3z M8 9h8 M8 13h8 M8 17h5',
  domains:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M3 12h18 M12 3c-6 6-6 12 0 18 6-6 6-12 0-18',
  mobile:'M8 2h8q2 0 2 2v16q0 2-2 2H8q-2 0-2-2V4q0-2 2-2 M10 18h4',
  desktop:'M3 4h18v12H3z M12 16v4 M8 21h8',
  unknown:'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M10 8a2 2 0 0 1 4 0c0 2-2 2-2 4 M12 16h.01',
  arrow:'M5 17 19 3 M7 3h12v12',
};
export function icon(name) {
  const svg = svgNode('svg',{viewBox:'0 0 24 24',width:20,height:20,fill:'none',stroke:'currentColor','stroke-width':1.6,'stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'});
  svg.append(svgNode('path',{d:icons[name] || icons.unknown}));
  return svg;
}
export function countryBadge(code) {
  const wrap = node('span','country-label');
  const path = flagPath(code);
  if (path) { const img = node('img','country-flag'); img.src = path; img.alt = ''; img.width = 24; img.height = 18; wrap.append(img); }
  else wrap.append(icon('domains'));
  wrap.append(node('span','',path ? countryLabel(code).split(' · ')[0] : '未知国家'));
  return wrap;
}
export function deviceBadge(item) {
  const wrap = node('span','device-label');
  const named = item.device_details?.device || '';
  const desktop = item.device === 'desktop' || /Windows|Mac|Linux|Chromebook|电脑/.test(named);
  const kind = desktop ? 'desktop' : 'mobile';
  wrap.append(icon(kind),node('span','',kind === 'desktop' ? '电脑' : '手机'));
  wrap.title = item.device_details ? `${item.device_details.device} · ${item.device_details.os} · ${item.device_details.browser}（UA 声明）` : '根据浏览器 User-Agent 判断';
  return wrap;
}
