import test from 'node:test';
import assert from 'node:assert/strict';
import { alarmDecision, clampRefresh, clampThreshold } from '../static/visit-watch.mjs';

test('the alarm plays only while monitoring is on and the minute count stays over the line', () => {
  assert.deepEqual(alarmDecision(false, false, 40, 20), {play: false, silenced: false});
  assert.deepEqual(alarmDecision(true, false, 19, 20), {play: false, silenced: false});
  assert.deepEqual(alarmDecision(true, false, 20, 20), {play: true, silenced: false});
  assert.deepEqual(alarmDecision(true, true, 25, 20), {play: false, silenced: true});
  assert.deepEqual(alarmDecision(true, true, 4, 20), {play: false, silenced: false});
});

test('refresh accepts ten seconds and rejects a burst', () => {
  assert.equal(clampRefresh(10), 10);
  assert.equal(clampRefresh(0), 0);
  assert.equal(clampRefresh(''), 0);
  assert.equal(clampRefresh(1), 5);
  assert.equal(clampRefresh(999), 300);
  assert.equal(clampThreshold(''), 20);
  assert.equal(clampThreshold(20), 20);
  assert.equal(clampThreshold(0), 20);
});
