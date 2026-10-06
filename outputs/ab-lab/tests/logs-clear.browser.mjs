import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import path from 'node:path';

const {chromium} = createRequire(import.meta.url)('playwright');
const root = path.resolve(import.meta.dirname, '..');
const document = execFileSync(process.env.PYTHON || 'python3', ['-X', 'utf8', '-c',
  'from jinja2 import Environment,FileSystemLoader; print(Environment(loader=FileSystemLoader("templates")).get_template("index.html").render(account={"role":"agent"},username="viewer",deployment=False,csrf_token="csrf",target_url="http://127.0.0.1:9000"))'], {cwd: root, encoding: 'utf8'});
const siteId = 'a'.repeat(32);
const site = {id: siteId, domain: 'mine.example.com', enabled: true, stage: 'active'};
const state = {site, config: {routing: 'RULES', allowed_slot: 'B', protection: true, content_mode: 'PAGE', distribution: 'random', rules: {}}, versions: [], slots: {A: null, B: null}, links: [], health: {geoip: true, geoip_detail: {}}, target_url: 'http://127.0.0.1:9000', revision: 1};
const analytics = {period: '7d', end: new Date().toISOString(), tz_offset: 0, summary: {total: 0, allowed: 0, blocked: 0, rate: 0, a: 0, b: 0, other: 0}, trend: [], domains: [], countries: [], reasons: [], recent: []};
const row = (id, country, visitPath) => ({id, created: id, country, device: 'mobile', slot: 'B', reason: 'allowed', path: visitPath, mode: 'PAGE', device_details: null});

test('clearing other countries sits beside search and keeps the Hong Kong rows', async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  const server = http.createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(document); return; }
    if (req.url.startsWith('/static/')) {
      try { res.setHeader('Content-Type', req.url.endsWith('.css') ? 'text/css' : 'text/javascript'); res.end(await fs.readFile(path.join(root, req.url))); }
      catch { res.writeHead(404); res.end(); }
      return;
    }
    res.writeHead(404); res.end();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  const clears = [];
  let cleared = false;
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname === '/api/sites') return route.fulfill({json: {account: {role: 'agent'}, sites: [site], google_reputation_configured: false}});
    if (pathname === `/api/sites/${siteId}/state`) return route.fulfill({json: state});
    if (pathname === `/api/sites/${siteId}/analytics`) return route.fulfill({json: analytics});
    if (pathname === `/api/sites/${siteId}/logs` && request.method() === 'GET') {
      const items = cleared
        ? [row(1, 'HK', '/hk'), row(2, 'HK', '/edge')]
        : [row(1, 'HK', '/hk'), row(2, 'HK', '/edge'), row(3, 'US', '/us')];
      return route.fulfill({json: {items, total: items.length, page: 1, pages: 1}});
    }
    if (pathname === `/api/sites/${siteId}/logs/clear-foreign` && request.method() === 'POST') {
      clears.push(JSON.parse(request.postData() || '{}'));
      cleared = true;
      return route.fulfill({json: {deleted: 1, kept: 2}});
    }
    return route.fulfill({status: 404, json: {detail: pathname}});
  });
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => document.querySelector('#connection')?.textContent === '服务已连接');
    await page.click('[data-tab="logs"]');
    await page.waitForFunction(() => [...document.querySelectorAll('#logs-body img.country-flag')].some(img => img.getAttribute('src') === '/static/flags/us.svg'));
    const search = await page.locator('#log-filters button[type="submit"]').boundingBox();
    const button = await page.locator('#clear-foreign-logs').boundingBox();
    assert.equal(await page.locator('#clear-foreign-logs').textContent(), '一键清除非投放地区');
    assert.ok(button.x > search.x + search.width - 1);
    assert.ok(Math.abs(button.y - search.y) < 12);
    await page.click('#clear-foreign-logs');
    await page.locator('dialog.confirm-dialog').waitFor();
    assert.match(await page.locator('dialog.confirm-dialog p').textContent(), /显示（香港）的会留下/);
    await page.locator('dialog.confirm-dialog button', {hasText: '取消'}).click();
    assert.equal(clears.length, 0);
    assert.match(await page.locator('#logs-body img.country-flag').last().getAttribute('src'), /\/static\/flags\/us\.svg$/);
    assert.match(await page.locator('#logs-body').textContent(), /美国/);
    await page.click('#clear-foreign-logs');
    await page.locator('dialog.confirm-dialog button', {hasText: '确认'}).click();
    await page.waitForFunction(() => document.querySelector('#toast-region')?.textContent?.includes('留下 2 条'));
    assert.deepEqual(clears, [{}]);
    const body = await page.locator('#logs-body').textContent();
    assert.match(body, /香港/);
    assert.doesNotMatch(body, /美国/);
    assert.match(await page.locator('#logs-body img.country-flag').first().getAttribute('src'), /\/static\/flags\/hk\.svg$/);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});
