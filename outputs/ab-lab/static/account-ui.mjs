import { sitePath } from './helpers.mjs';

export const isAdmin = account => account?.role === 'admin';
export const chooseSite = (current, sites) => sites.some(site => site.id === current) ? current : sites[0]?.id || null;

export function accountSitePath(path, siteId) {
  if (!/^\/api\/(state|config|upload|publish|source|b-redirects|desk|preview|links|counters|simulate|logs|audit|analytics|tracking|versions)([/.?]|$)/.test(path)) return path;
  if (!siteId) throw new Error('请先添加或选择一个域名。');
  return sitePath(path, siteId);
}
