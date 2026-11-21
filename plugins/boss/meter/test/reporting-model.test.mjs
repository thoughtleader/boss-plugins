import { test } from 'node:test';
import assert from 'node:assert/strict';
import { meterFromSnapshot, renderMeterText } from '../meter.mjs';
const now = Date.parse('2026-11-12T06:00:00Z');
const at = delta => new Date(now + delta).toISOString();
const row = (id, state, extra = {}) => ({ id, kind:'row', label: id, state, ...extra });
const build = (items, reporting = {}, extra = {}, nowMs = now) => meterFromSnapshot({state:'running', updated_at:at(0), items, reporting, ...extra}, {nowMs});

test('waiting freezes clocks, resumed work excludes merged overlapping wait intervals', () => {
  const items = [row('build','waiting_input',{started_at:at(-600000), estimate_ms:1200000})];
  const reporting = {waiting:true,waiting_since:at(-120000),waits:[{started_at:at(-120000)}]};
  const a=build(items,reporting),b=build(items,reporting,{},now+300000);
  assert.equal(a.total.elapsedMs,480000);
  assert.equal(a.total.ticking,false);
  assert.equal(a.total.elapsedMs,b.total.elapsedMs);
  assert.match(a.forecast.finish.text,/min after you respond/);
  assert.equal(a.forecast.finish.at,null);
  const resumed=build([row('build','running',{started_at:at(-600000)})],{waits:[{started_at:at(-300000),ended_at:at(-120000)},{started_at:at(-240000),ended_at:at(-60000)}]});
  assert.equal(resumed.total.elapsedMs,360000);
});

test('waiting for one task does not freeze another active task', () => {
  const meter=build([row('a','waiting_input',{started_at:at(-120000)}),row('b','running',{started_at:at(-120000)})],{waiting:true,waiting_since:at(-60000)});
  assert.equal(meter.total.ticking,true);
  assert.equal(meter.total.elapsedMs,120000);
  assert.equal(meter.forecast.waiting,false);
});

test('explicit dependency critical path predicts reviewable checkpoint before whole job', () => {
  const items=[{id:'wave:1',kind:'wave'},row('build','queued',{parent:'wave:1',estimate_ms:120000}),row('review','queued',{parent:'wave:1',depends_on:['build'],estimate_ms:60000,reviewable:true}),{id:'wave:2',kind:'wave'},row('ship','queued',{parent:'wave:2',estimate_ms:90000})];
  const meter=build(items,{forecast:{original_finish_at:at(180000),original_checkpoint_at:at(120000)}});
  assert.equal(meter.forecast.checkpoint.remainingMs,180000);
  assert.equal(meter.forecast.finish.remainingMs,270000);
  assert.equal(meter.forecast.originalFinishAt,now+180000);
  assert.equal(meter.forecast.delayMs,90000);
  assert.equal(meter.tasks.next.find(t=>t.id==='review').waitingReason,'build');
  assert.match(meter.forecast.finish.text,/^~/);
});

test('task ETA ends the displayed wave while the final review remains in the job ETA', () => {
  const meter = build([
    { id: 'wave:1', kind: 'wave', label: 'Build' },
    row('build', 'running', { parent: 'wave:1', started_at: at(-60000), estimate_ms: 180000 }),
    { id: 'wave:2', kind: 'wave', label: 'Review' },
    row('review', 'queued', { parent: 'wave:2', reviewable: true, estimate_ms: 180000 }),
  ]);
  assert.equal(meter.forecast.currentWave.remainingMs, 120000);
  assert.equal(meter.forecast.finish.remainingMs, 300000);
  assert.equal(meter.forecast.checkpoint.remainingMs, 300000);
  const lines = renderMeterText(meter, { fenced: false }).split('\n');
  assert.ok(lines[0].endsWith(meter.forecast.finish.text));
  assert.ok(lines[1].endsWith(meter.forecast.currentWave.text));
  assert.notEqual(meter.forecast.currentWave.text, meter.forecast.finish.text);
});

test('current wave follows the displayed active wave even with an earlier unresolved gate', () => {
  const meter = build([
    { id: 'wave:1', kind: 'wave', label: 'Earlier gate' },
    row('gate', 'blocked', { parent: 'wave:1', estimate_ms: 900000 }),
    { id: 'wave:2', kind: 'wave', label: 'Active work' },
    row('build', 'running', { parent: 'wave:2', estimate_ms: 180000, started_at: at(-60000) }),
  ]);
  assert.equal(meter.current.label, 'Wave Active work');
  assert.equal(meter.forecast.currentWave.remainingMs, 120000);
});

test('single-wave report shows one forecast because current tasks and job share an endpoint', () => {
  const meter = build([row('build', 'running', { estimate_ms: 180000, started_at: at(-60000) })]);
  assert.equal(meter.forecast.currentWave.remainingMs, meter.forecast.finish.remainingMs);
  const text = renderMeterText(meter, { fenced: false });
  assert.equal(text.split('\n').filter(line => /~\d/.test(line)).length, 1);
});

test('DONE is a report delta; activity and full instructions survive concise titles', () => {
  const meter=build([row('a','done'),row('b','done'),row('c','running',{label:'A full instruction '.repeat(20),short_label:'Layout',activity:'Checking wrapping'})],{previous_completed_ids:['a']});
  assert.deepEqual(meter.tasks.recent.map(t=>t.id),['b']);
  assert.equal(meter.tasks.all.length,3);
  assert.equal(meter.tasks.now[0].label,'Layout');
  assert.match(meter.tasks.now[0].instructions,/A full instruction/);
  assert.match(renderMeterText(meter),/➜    Layout — Checking wrapping/);
});

test('single waves hide W and unsuccessful terminal runs do not claim Complete', () => {
  const meter=build([{id:'wave:1',kind:'wave'},row('a','failed',{parent:'wave:1'})],{}, {state:'failed'});
  assert.equal(meter.waves,null);
  assert.notEqual(meter.forecast.finish.text,'Complete');
});

test('gate metadata selects reviewable checkpoint while explicit false takes precedence', () => {
  const items = [row('build','queued',{estimate_ms:60000}),row('Approval','queued',{kind:'gate',estimate_ms:120000}),row('review excluded','queued',{kind:'gate',reviewable:false,estimate_ms:1000})];
  const meter = build(items);
  assert.equal(meter.forecast.checkpoint.label,'Approval');
  assert.equal(meter.forecast.checkpoint.remainingMs,120000);
  const metadata = build([row('Sign-off','queued',{meta:{gate:true},estimate_ms:90000})]);
  assert.equal(metadata.forecast.checkpoint.label,'Sign-off');
  const excluded = build([row('Review','queued',{kind:'gate',reviewable:false,estimate_ms:90000})]);
  assert.equal(excluded.forecast.checkpoint.label,'Next result (estimated)');
});

test('retry rows remain visible and active without a child attempt', () => {
  const meter = build([row('Retrying checks','retry',{started_at:at(-60000),activity:'Second attempt'})],{waiting:true,waiting_since:at(-30000)},{paused:true,state:'waiting_input'});
  assert.equal(meter.tasks.now.length,1);
  assert.equal(meter.tasks.now[0].state,'retry');
  assert.equal(meter.current.running,1);
  assert.equal(meter.total.ticking,true);
  assert.equal(meter.forecast.waiting,false);
  assert.match(renderMeterText(meter),/➜    Retrying checks \(retry\) — Second attempt/);
});
