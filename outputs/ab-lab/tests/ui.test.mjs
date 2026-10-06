import test from 'node:test';
import assert from 'node:assert/strict';

const helpers = await import('../static/helpers.mjs').catch(() => ({}));

test('Chinese country and language input normalizes to rule codes', () => {
  const next = helpers.rulesConfig({rules:{}}, {countries:'韩国，中国香港,KR', languages:'韩语,中文,ko',window_hours:24});
  assert.deepEqual(next.rules.countries, ['KR','HK']);
  assert.deepEqual(next.rules.languages, ['ko','zh']);
  assert.throws(() => helpers.rulesConfig({rules:{}}, {countries:'不存在的国家'}));
  assert.throws(() => helpers.rulesConfig({rules:{}}, {languages:'不存在的语言'}));
});

test('Chinese searches expose a selectable Korean result and meaningful country labels', async () => {
  const locale = await import('../static/locale.mjs');
  assert.equal(locale.searchOptions('countries', '韩国')[0].code, 'KR');
  assert.equal(locale.searchOptions('languages', '韩语')[0].code, 'ko');
  assert.match(locale.countryLabel('KR'), /韩国/);
  assert.equal(locale.countryShort('HK'), '香港');
  assert.equal(locale.countryParen('HK'), '（香港）');
  assert.equal(locale.countryParen(null), '（未知）');
  assert.match(locale.countryLabel(null), /未知/);
  assert.equal(locale.normalizeSelection('languages', 'zh-CN')[0], 'zh-cn');
});

test('site paths are explicit and preserve global endpoints', () => {
  assert.equal(helpers.sitePath('/api/config', 'default'), '/api/sites/default/config');
  assert.equal(helpers.sitePath('/api/tracking/apply', 'default'), '/api/sites/default/tracking/apply');
  assert.equal(helpers.sitePath('/api/upload/A?name=a.zip', 'a'.repeat(32)), `/api/sites/${'a'.repeat(32)}/upload/A?name=a.zip`);
  assert.equal(helpers.sitePath('/api/logout', 'default'), '/api/logout');
  assert.equal(helpers.sitePath('/api/sites', 'default'), '/api/sites');
  assert.throws(() => helpers.sitePath('/api/state', '../x'));
});

test('quick config updates preserve rules and do not mutate server state', () => {
  assert.equal(typeof helpers.patchConfig, 'function');
  const original = { routing: 'RULES', protection: true, rules: { blacklist: ['1.2.3.4'], min_ios: 16 } };
  const updated = helpers.patchConfig(original, { routing: 'FORCE_B' });
  assert.deepEqual(updated, { routing: 'FORCE_B', protection: true, rules: { blacklist: ['1.2.3.4'], min_ios: 16 } });
  updated.rules.blacklist.push('5.6.7.8');
  assert.deepEqual(original.rules.blacklist, ['1.2.3.4']);
});

test('rule edits normalize lists and preserve unedited configuration', () => {
  assert.equal(typeof helpers.rulesConfig, 'function');
  const base = { routing: 'FORCE_A', distribution: 'equal', rules: { future_option: true } };
  const result = helpers.rulesConfig(base, {
    countries: 'hk, US, hk', languages: 'zh-CN, en', blacklist: '1.2.3.4\n5.6.7.8',
    whitelist: '', blocked_cidrs: '192.0.2.0/24', bot_markers: 'bot, crawler',
    block_bots: true, strict_bots: true, block_ipv4: false, block_pc: true,
    min_android: '12', min_ios: '16', max_visits: '10', window_hours: '24',
  });
  assert.deepEqual(result.rules.countries, ['HK', 'US']);
  assert.deepEqual(result.rules.blacklist, ['1.2.3.4', '5.6.7.8']);
  assert.equal(result.rules.min_ios, 16);
  assert.equal(result.rules.strict_bots, true);
  assert.equal(result.rules.future_option, true);
  assert.equal(result.routing, 'FORCE_A');
  assert.deepEqual(base.rules, { future_option: true });
  assert.throws(() => helpers.rulesConfig(base, { countries: 'CHINA' }), /国家/);
  assert.throws(() => helpers.rulesConfig(base, { countries: '', window_hours: '0' }), /时间窗口/);
  assert.throws(() => helpers.rulesConfig(base, { min_ios: '16.5' }), /版本/);
});

test('batch URLs reject executable schemes, credentials and invalid lines', () => {
  assert.equal(typeof helpers.parseLinks, 'function');
  assert.deepEqual(helpers.parseLinks('https://example.com/a\n\nhttps://example.com/a\nhttp://example.org'), ['https://example.com/a', 'http://example.org/']);
  for (const input of ['javascript:alert(1)', 'data:text/html,hello', 'https://u:p@example.com', 'not a url', '']) {
    assert.throws(() => helpers.parseLinks(input));
  }
});

test('preview URLs remain on the target origin and log paths omit query and fragment', () => {
  assert.equal(typeof helpers.previewURL, 'function');
  assert.equal(helpers.previewURL('http://127.0.0.1:8766/preview/one', 'http://127.0.0.1:8766'), 'http://127.0.0.1:8766/preview/one');
  assert.throws(() => helpers.previewURL('http://127.0.0.1:8765/admin', 'http://127.0.0.1:8766'));
  assert.throws(() => helpers.previewURL('javascript:alert(1)', 'http://127.0.0.1:8766'));
  assert.equal(helpers.logPath('/offer?token=secret#private'), '/offer');
  assert.equal(helpers.logPath('https://example.com/p?q=secret'), '/p');
});

test('file checks reject empty, oversized and unsupported uploads', () => {
  assert.equal(typeof helpers.validateUpload, 'function');
  assert.doesNotThrow(() => helpers.validateUpload({ name: '站点.ZIP', size: 20 * 1024 * 1024 }));
  assert.throws(() => helpers.validateUpload({ name: 'site.zip', size: 20 * 1024 * 1024 + 1 }));
  assert.throws(() => helpers.validateUpload({ name: 'site.html', size: 0 }));
  assert.throws(() => helpers.validateUpload({ name: 'site.exe', size: 1 }));
});
