import test from 'node:test';
import assert from 'node:assert/strict';
import {sitePath} from '../static/helpers.mjs';
import * as dashboard from '../static/dashboard.mjs';
const presentation = await import('../static/presentation.mjs').catch(() => ({}));

test('theme reflects effective routing rather than inactive allowed preference', () => {
  assert.equal(presentation.themeFor({routing:'RULES',allowed_slot:'A'}),'angel');
  assert.equal(presentation.themeFor({routing:'RULES',allowed_slot:'B'}),'demon');
  assert.equal(presentation.themeFor({routing:'FORCE_A',allowed_slot:'B'}),'angel');
  assert.equal(presentation.themeFor({routing:'FORCE_B',allowed_slot:'A'}),'demon');
  assert.equal(presentation.themeFor(null),'loading');
});
test('flags use local validated region assets with a neutral unknown fallback', () => {
  assert.equal(presentation.flagPath('kr'),'/static/flags/kr.svg');
  assert.equal(presentation.flagPath(null),null);
  assert.equal(presentation.flagPath('../../evil'),null);
  assert.equal(presentation.flagPath('ZZ'),null);
});
test('analytics requests remain explicitly scoped to the selected site', () => {
  assert.equal(sitePath('/api/analytics?scope=all&period=7d','a'.repeat(32)),`/api/sites/${'a'.repeat(32)}/analytics?scope=all&period=7d`);
});
test('A display is not confused with blocked traffic; manual remains separate', () => {
  assert.equal(presentation.outcomeFor('allowed'),'allowed');
  assert.equal(presentation.outcomeFor('whitelist'),'allowed');
  assert.equal(presentation.outcomeFor('country'),'blocked');
  assert.equal(presentation.outcomeFor('manual'),'other');
  assert.equal(presentation.outcomeFor('unrecognized'),'other');
});
test('today trend stops at current local hour instead of plotting future zero traffic', () => {
  const trend = Array.from({length:24},(_,i)=>({label:`${String(i).padStart(2,'0')}:00`,total:i}));
  const data = {period:'today',end:'2026-10-02T05:30:00+00:00',tz_offset:480,trend};
  assert.equal(dashboard.visibleTrend(data).length,14);
  assert.equal(dashboard.visibleTrend({...data,period:'yesterday'}).length,24);
  assert.equal(dashboard.visibleTrend({...data,end:'2026-10-01T16:00:00+00:00'}).length,1);
});

test('Google reputation never presents missing or expired results as healthy', () => {
  for (const status of ['unchecked','unconfigured','unavailable','error','unexpected']) {
    assert.equal(presentation.reputationView({status},100).tone,'neutral');
  }
  assert.equal(presentation.reputationView({status:'clean',expires_at:200},100).label,'未检出风险');
  assert.equal(presentation.reputationView({status:'flagged',expires_at:200},100).tone,'red');
  assert.equal(presentation.reputationView({status:'clean',expires_at:99},100).label,'结果已过期');
  assert.equal(presentation.reputationView({status:'flagged',expires_at:99},100).tone,'neutral');
  assert.equal(presentation.reputationView({status:'clean'},100).tone,'neutral');
});

