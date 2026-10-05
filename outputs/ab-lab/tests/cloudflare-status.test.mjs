import test from 'node:test';
import assert from 'node:assert/strict';
const cloudflare = await import('../static/cloudflare-status.mjs');

const site = (id, status = 'pending') => ({id, cf_status: status, cf_zone_id: 'c'.repeat(32)});
const catalog = sites => ({cloudflare_configured: true, sites});

test('a hidden page or an unconfigured server does not check Cloudflare', async () => {
  let active = false, calls = 0;
  const checker = cloudflare.createCloudflareAutoCheck({check: async () => calls++, onUpdate: () => {}, isActive: () => active});
  await checker.refresh(catalog([site('a')]));
  active = true;
  await checker.refresh({...catalog([site('a')]), cloudflare_configured: false});
  await checker.refresh(catalog([site('done', 'active'), {id: 'plain', cf_status: '', cf_zone_id: ''}]));
  assert.equal(calls, 0);
});

test('pending domains are checked one at a time and an active result stops the next pass', async () => {
  const calls = [];
  const checker = cloudflare.createCloudflareAutoCheck({
    check: async id => { calls.push(id); return {site: {id, cf_status: 'active'}}; },
    onUpdate: (id, update) => { if (update.result) catalogSites.find(item => item.id === id).cf_status = update.result.site.cf_status; },
    isActive: () => true,
    now: () => 1000,
  });
  const catalogSites = [site('a'), site('b', 'active')];
  await checker.refresh(catalog(catalogSites));
  assert.deepEqual(calls, ['a']);
  await checker.refresh(catalog(catalogSites));
  assert.deepEqual(calls, ['a']);
});

test('the same pending domain is not checked again before the interval', async () => {
  let now = 1000, calls = 0;
  const checker = cloudflare.createCloudflareAutoCheck({
    check: async () => { calls++; return {site: {cf_status: 'pending'}}; },
    onUpdate: () => {},
    isActive: () => true,
    now: () => now,
    interval: 20,
  });
  const data = catalog([site('a')]);
  await checker.refresh(data);
  now = 1019;
  await checker.refresh(data);
  assert.equal(calls, 1);
  now = 1020;
  await checker.refresh(data);
  assert.equal(calls, 2);
});

test('a failed request stays pending and can be retried after the interval', async () => {
  let now = 1000, calls = 0;
  const errors = [];
  const checker = cloudflare.createCloudflareAutoCheck({
    check: async () => { calls++; throw new Error('offline'); },
    onUpdate: (id, update) => { if (update.error) errors.push(id); },
    isActive: () => true,
    now: () => now,
    interval: 20,
  });
  await checker.refresh(catalog([site('a')]));
  assert.deepEqual(errors, ['a']);
  now = 1019;
  await checker.refresh(catalog([site('a')]));
  assert.equal(calls, 1);
});
