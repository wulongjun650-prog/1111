import test from 'node:test';
import assert from 'node:assert/strict';

const accounts = await import('../static/account-ui.mjs').catch(() => ({}));

test('account management depends only on an explicit admin role', () => {
  assert.equal(typeof accounts.isAdmin, 'function');
  assert.equal(accounts.isAdmin({username:'admin888',role:'agent'}), false);
  assert.equal(accounts.isAdmin({username:'other',role:'admin'}), true);
  assert.equal(accounts.isAdmin(null), false);
});

test('catalog loss chooses an owned site or no site, never a default fallback', () => {
  assert.equal(typeof accounts.chooseSite, 'function');
  assert.equal(accounts.chooseSite('old', [{id:'mine'}]), 'mine');
  assert.equal(accounts.chooseSite('mine', [{id:'other'},{id:'mine'}]), 'mine');
  assert.equal(accounts.chooseSite('default', []), null);
});

test('empty accounts can use catalog and account routes but cannot call site APIs', () => {
  assert.equal(typeof accounts.accountSitePath, 'function');
  for (const path of ['/api/me','/api/sites','/api/accounts','/api/accounts/admin/password','/api/logout']) {
    assert.equal(accounts.accountSitePath(path, null), path);
  }
  for (const path of ['/api/state','/api/config','/api/analytics?scope=all','/api/logs.csv','/api/logs/clear','/api/logs/clear-foreign','/api/audit','/api/preview','/api/upload/A','/api/b-redirects','/api/b-redirects/presets/1/check','/api/tracking','/api/tracking/apply','/api/versions/B/cleanup','/api/versions/B/' + 'b'.repeat(32)]) {
    assert.throws(() => accounts.accountSitePath(path, null), /域名/);
  }
  assert.equal(accounts.accountSitePath('/api/state', 'a'.repeat(32)), `/api/sites/${'a'.repeat(32)}/state`);
  assert.equal(accounts.accountSitePath('/api/b-redirects?version_id=mine', 'a'.repeat(32)), `/api/sites/${'a'.repeat(32)}/b-redirects?version_id=mine`);
  assert.equal(accounts.accountSitePath('/api/versions/B/' + 'b'.repeat(32), 'a'.repeat(32)), `/api/sites/${'a'.repeat(32)}/versions/B/${'b'.repeat(32)}`);
  assert.equal(accounts.accountSitePath('/api/versions/B/cleanup', 'a'.repeat(32)), `/api/sites/${'a'.repeat(32)}/versions/B/cleanup`);
});
