import test from 'node:test';
import assert from 'node:assert/strict';
const reputation = await import('../static/reputation.mjs').catch(() => ({}));

const site = (id, status = 'unchecked', extra = {}) => ({id,google_reputation:{status,...extra}});
const catalog = sites => ({google_reputation_configured:true,sites});

test('automatic checks are limited to missing, expired, or retryable results', () => {
  for (const status of ['unconfigured','unavailable']) assert.equal(reputation.reputationDue({status},1000),false);
  assert.equal(reputation.reputationDue({status:'unchecked'},1000),true);
  assert.equal(reputation.reputationDue({status:'expired'},1000),true);
  assert.equal(reputation.reputationDue({status:'clean',expires_at:1100},1000),false);
  assert.equal(reputation.reputationDue({status:'flagged',expires_at:1100},1000),false);
  assert.equal(reputation.reputationDue({status:'clean',expires_at:999},1000),true);
  assert.equal(reputation.reputationDue({status:'error',checked_at:950},1000),false);
  assert.equal(reputation.reputationDue({status:'error',checked_at:940},1000),true);
});

test('unconfigured or hidden pages never start automatic checks', async () => {
  let active = false, calls = 0;
  const checker = reputation.createReputationAutoCheck({check:async()=>calls++,onUpdate:()=>{},isActive:()=>active});
  await checker.refresh(catalog([site('a')]));
  active = true;
  await checker.refresh({...catalog([site('a')]),google_reputation_configured:false});
  assert.equal(calls,0);
});

test('auto checks run serially and update each row without navigation', async () => {
  const calls = [], updates = [];
  const result = {status:'clean',expires_at:2000,checked_at:1000};
  const checker = reputation.createReputationAutoCheck({
    check:async id=>{calls.push(id);return result;},
    onUpdate:(id,update)=>updates.push({id,...update}), isActive:()=>true,now:()=>1000,
  });
  await checker.refresh(catalog([site('a'),site('b','clean',{expires_at:2000}),site('c','expired')]));
  assert.deepEqual(calls,['a','c']);
  assert.deepEqual(updates,[{id:'a',checking:true},{id:'a',result},{id:'a',checking:false},{id:'c',checking:true},{id:'c',result},{id:'c',checking:false}]);
});

test('overlapping list refreshes cannot duplicate pending checks', async () => {
  let release, calls = 0;
  const gate = new Promise(resolve=>{release=resolve;});
  const checker = reputation.createReputationAutoCheck({check:async()=>{calls++;await gate;return {status:'clean'};},onUpdate:()=>{},isActive:()=>true,now:()=>1000});
  const first = checker.refresh(catalog([site('a')]));
  await checker.refresh(catalog([site('a')]));
  assert.equal(calls,1);
  release(); await first;
  await checker.refresh(catalog([site('a')]));
  assert.equal(calls,1);
});

test('leaving the domain page stops new requests after the active check completes', async () => {
  let active = true;
  const calls = [];
  const checker = reputation.createReputationAutoCheck({check:async id=>{calls.push(id);active=false;return {status:'clean'};},onUpdate:()=>{},isActive:()=>active});
  await checker.refresh(catalog([site('a'),site('b')]));
  assert.deepEqual(calls,['a']);
});

test('network failure is shown inline and retry is throttled, not treated as clean', async () => {
  let now = 1000, calls = 0;
  const results = [];
  const checker = reputation.createReputationAutoCheck({check:async()=>{calls++;throw new Error('offline');},onUpdate:(_,update)=>{if(update.result)results.push(update.result);},isActive:()=>true,now:()=>now});
  const data = catalog([site('a')]);
  await checker.refresh(data);
  assert.equal(results[0].status,'error');
  now=1059; await checker.refresh(data); assert.equal(calls,1);
  now=1060; await checker.refresh(data); assert.equal(calls,2);
});
