import { test } from 'node:test';
import assert from 'node:assert/strict';
import { reportingSnapshot, reportingState } from '../reporting.mjs';
import { meterFromSnapshot } from '../meter.mjs';
import { createRequire } from 'node:module';
const { buildSnapshot } = createRequire(import.meta.url)('../../lib/progress-mirror.js');

const start = Date.parse('2026-11-11T20:00:00Z');
const at = seconds => new Date(start + seconds * 1000).toISOString();
const source = state => ({ tool: 'orchestrator', run_id: 'history-fixture', state, started_at: at(0), updated_at: at(60), items: [
  { id: 'row:1', kind: 'row', label: 'Build', state: 'done', started_at: at(0), ended_at: at(30) },
  { id: 'row:2', kind: 'gate', label: 'Review result', state: 'queued', estimate_ms: 120000 },
] });

test('successive published reports retain original forecast and advance completion boundary', () => {
  const first = reportingSnapshot(source('running'), null, { nowMs: start + 60000 });
  const meter = meterFromSnapshot(first, { nowMs: start + 60000 });
  const saved = reportingState(first, meter, null, start + 60000);
  const next = reportingSnapshot(source('running'), saved, { nowMs: start + 120000 });
  const nextMeter = meterFromSnapshot(next, { nowMs: start + 120000 });
  assert.equal(meter.tasks.recent.length, 0, 'first publication establishes a baseline without replaying history');
  assert.equal(nextMeter.tasks.recent.length, 0);
  assert.equal(nextMeter.forecast.originalFinishAt, meter.forecast.originalFinishAt);
  assert.equal(nextMeter.tasks.all.length, 2);
  const completed = source('running'); completed.items[1].state = 'done';
  const after = reportingSnapshot(completed, saved, { nowMs: start + 180000 });
  const afterMeter = meterFromSnapshot(after, { nowMs: start + 180000 });
  assert.deepEqual(afterMeter.tasks.recent.map(task => task.id), ['row:2']);
  const afterState = reportingState(after, afterMeter, saved, start + 180000);
  const repeated = meterFromSnapshot(reportingSnapshot(completed, afterState), { nowMs: start + 240000 });
  assert.equal(repeated.tasks.recent.length, 0, 'new completion is reported once');
});

test('recorded owner wait freezes clocks, survives resume, and does not consume execution time', () => {
  const waiting = reportingSnapshot(source('waiting_input'), null, { nowMs: start + 120000 });
  const before = meterFromSnapshot(waiting, { nowMs: start + 120000 });
  const later = meterFromSnapshot(waiting, { nowMs: start + 180000 });
  assert.equal(before.total.elapsedMs, later.total.elapsedMs);
  assert.match(later.forecast.finish.text, /after you respond/);
  const saved = reportingState(waiting, before, null, start + 120000);
  const resumedSource = source('running'); resumedSource.updated_at = at(180);
  const resumed = reportingSnapshot(resumedSource, saved, { nowMs: start + 180000 });
  assert.equal(resumed.reporting.waits[0].ended_at, at(180));
  assert.equal(meterFromSnapshot(resumed, { nowMs: start + 180000 }).total.waitingMs, 120000);
});

test('a paused flag does not freeze tasks that are still active', () => {
  const snap = source('waiting_input'); snap.paused = true;
  snap.items[1].state = 'running'; snap.items[1].started_at = at(30);
  const enriched = reportingSnapshot(snap, null, { nowMs: start + 90000 });
  assert.equal(enriched.reporting.waiting, false);
  assert.equal(enriched.reporting.waits.length, 0);
  const first = meterFromSnapshot(enriched, { nowMs: start + 90000 });
  const next = meterFromSnapshot(enriched, { nowMs: start + 120000 });
  assert.equal(next.total.elapsedMs - first.total.elapsedMs, 30000);
});

test('lease events close an observed wait at actual resume rather than next report', () => {
  const waiting = reportingSnapshot(source('waiting_input'), null, { nowMs: start + 120000 });
  const meter = meterFromSnapshot(waiting, { nowMs: start + 120000 });
  const saved = reportingState(waiting, meter, null, start + 120000);
  const resumedSource = source('running'); resumedSource.updated_at = at(240);
  const events = [ { seq: 1, type: 'lease', action: 'release', ts: at(60) }, { seq: 2, type: 'lease', action: 'acquire', ts: at(180) } ];
  const next = reportingSnapshot(resumedSource, saved, { events, nowMs: start + 240000 });
  assert.deepEqual(next.reporting.waits, [{ started_at: at(60), ended_at: at(180) }]);
});

test('optional plan reporting fields reach the model without changing dispatch state', () => {
  const txt = '| # | Subtask | Short name | Activity | Estimate | Depends on | Reviewable | Status |\n|---|---|---|---|---|---|---|---|\n| 1 | Implement the complete reporting contract | Reporting | Checking forecasts | 7m | | no | running |\n| 2 | Inspect the result | Result | | 2m | 1 | yes | pending |';
  const snap = buildSnapshot('fields-fixture', [], { plan: { txt }, cfg: {}, now: start, parseAttemptId: () => null });
  const row = snap.items.find(item => item.id === 'row:1');
  assert.equal(row.state, 'running'); assert.equal(row.estimate_ms, 420000);
  const meter = meterFromSnapshot(snap, { nowMs: start });
  assert.equal(meter.tasks.now[0].label, 'Reporting');
  assert.equal(meter.tasks.now[0].activity, 'Checking forecasts');
  assert.match(meter.tasks.now[0].instructions, /complete reporting contract/);
  assert.equal(meter.tasks.next[0].waitingReason, 'Reporting');
});

test('delay notice remains available after the retained history reaches its limit', () => {
  const snap = reportingSnapshot(source('running'), null, { nowMs: start + 60000 });
  const meter = meterFromSnapshot(snap, { nowMs: start + 60000 });
  meter.forecast.delayMs = 120000;
  const saved = { ...reportingState(snap, meter), activeKey: 'previous-task', delayHistory: Array.from({ length: 50 }, () => ({ at: at(0) })) };
  const next = reportingState(snap, meter, saved);
  assert.equal(next.delayHistory.length, 50);
  assert.equal(next.delayAdded, true);
});
