import { test } from 'node:test';
import assert from 'node:assert/strict';
import { meterFromSnapshot, meterFromBoardRun, renderMeterText, displayBar, progressLabel, formatElapsed, formatClock, clockHours, clockParts, formatTokens } from '../meter.mjs';

const NOW = Date.parse('2026-11-11T20:10:00Z');
const at = seconds => new Date(NOW + seconds * 1000).toISOString();
const wave = (id, extra = {}) => ({ id: `wave:${id}`, kind: 'wave', label: `wave ${id}`, ...extra });
const row = (id, state, w = 'A', extra = {}) => ({ id: `row:${id}`, parent: w == null ? null : `wave:${w}`, kind: 'row', label: `Task ${id}`, state, ...extra });
const attempt = (id, state, extra = {}) => ({ id: `row:${id}/a1`, parent: `row:${id}`, kind: 'attempt', label: `run#${id}.1`, state, ...extra });
const snapshot = (items, extra = {}) => ({ schema: 'boss.progress/1', tool: 'orchestrator', state: 'running', items, updated_at: at(-15), started_at: at(-600), ...extra });
const build = (items, extra = {}, options = {}) => meterFromSnapshot(snapshot(items, extra), { nowMs: NOW, ...options });
const meterLines = text => text.split('\n').filter(line => /^[🆆🆃]/u.test(line));

test('parallel agents produce wall time, not summed attempt duration; latest active wave is shown', () => {
  const meter = build([
    wave('A'), row(1, 'done'), attempt(1, 'done', { started_at: at(-500), ended_at: at(-380), meta: { tokens: 28000, ms: 120000 } }),
    wave('B'), row(2, 'running', 'B'), attempt(2, 'running', { started_at: at(-128) }),
    row(3, 'done', 'B'), attempt(3, 'done', { started_at: at(-100), ended_at: at(-30), meta: { tokens: 16000, ms: 70000 } }),
    row(4, 'queued', 'B'), row(5, 'queued', 'B'),
  ]);
  assert.equal(meter.current.label, 'Wave B');
  assert.equal(meter.waves.done, 1);
  assert.equal(meter.waves.total, 2);
  assert.equal(meter.waves.running, 1);
  assert.equal(meter.waves.bar, '▰▱');
  assert.equal(meter.waves.elapsedMs, meter.total.elapsedMs);
  assert.equal(meter.current.elapsedMs, 128000);
  assert.equal(meter.total.elapsedMs, 500000);
  assert.equal(meter.current.running, 1);
  assert.equal(meter.current.bar, '▰▱▱▱');
  assert.equal(meter.current.tokens, 16000);
  assert.equal(meter.total.tokens, 44000);
  assert.equal(meter.total.partialTokens, true);
  assert.match(meter.tokenNote, /Reported tokens/);
  assert.match(meter.tokenNote, /Continuations/);
  const lines = renderMeterText(meter, { fenced: false }).split('\n');
  assert.match(lines[0], /^🆆.*08:20.*44K/);
  assert.match(lines[1], /^🆃.*02:08.*16K/);
  assert.deepEqual(lines.slice(2), ['☑    Task 3', '☑    Task 1', '➜    Task 2', '⮡    Task 4', '⮡    Task 5']);
});

test('retry attempts count once, ledger fills usage, and parent row token aggregate is not counted again', () => {
  const first = attempt(1, 'failed', { meta: { attempt_id: 'run#1.1', tokens: 100 }, started_at: at(-120), ended_at: at(-90) });
  const second = attempt(1, 'done', { id: 'row:1/a2', label: 'run#1.2', started_at: at(-80), ended_at: at(-20), meta: { attempt_id: 'run#1.2' } });
  const meter = build([wave('A'), row(1, 'done', 'A', { tokens: 999999 }), first, first, second], {}, { ledger: [{ id: 'run#1.2', tokens: 200 }, { id: 'run#1.2', tokens: 250 }, { id: 'run#1.2', firstPass: true }] });
  assert.equal(meter.total.tokens, 350);
  assert.equal(meter.total.partialTokens, false);
  assert.equal(meter.total.elapsedMs, 100000);
  assert.equal(meter.total.ticking, false);
});

test('legacy plan-only rows use their ledger records without inventing attempt durations', () => {
  const meter = build([wave('A'), row(1, 'done'), row(2, 'done'), row(3, 'queued')], {}, { ledger: [
    { id: 'run#1.1', row: 1, tokens: 100, ms: 50000 },
    { id: 'run#1.2', row: 1, tokens: 200, ms: 30000 },
    { id: 'run#1.2', row: 1, firstPass: true },
    { row: 2, ts: at(-30), tokens: 50, ms: 40000 },
    { row: 2, ts: at(-30), firstPass: true },
    { row: 2, ts: at(-20), tokens: null, ms: 40000 },
  ] });
  assert.equal(meter.current.tokens, 350);
  assert.equal(meter.current.partialTokens, true);
  assert.equal(meter.current.elapsedMs, null);
});

test('missing usage remains unknown, recorded zero is valid, queued tasks do not imply missing usage', () => {
  const none = build([wave('A'), row(1, 'queued')]);
  assert.equal(none.current.tokens, null);
  assert.equal(none.current.partialTokens, false);
  assert.equal(none.current.elapsedMs, null);
  assert.equal(none.total.elapsedMs, null);
  const zero = build([wave('A'), row(1, 'done'), attempt(1, 'done', { meta: { tokens: 0 } }), row(2, 'queued')]);
  assert.equal(zero.current.tokens, 0);
  assert.equal(zero.current.partialTokens, false);
  const missing = build([wave('A'), row(1, 'done'), attempt(1, 'done', { started_at: at(-60), ended_at: at(-20) })]);
  assert.equal(missing.current.tokens, null);
  assert.equal(missing.current.partialTokens, true);
});

test('paused, terminal, and dead reports freeze the clocks and running count', () => {
  const items = [wave('A'), row(1, 'running'), attempt(1, 'running', { started_at: at(-120) })];
  for (const extra of [{ state: 'failed' }, { status: 'dead' }, { stop: { state: 'stopped', since: at(-15) } }]) {
    const meter = build(items, extra);
    assert.equal(meter.current.elapsedMs, 105000);
    assert.equal(meter.current.running, 0);
    assert.equal(meter.current.ticking, false);
    assert.equal(meter.total.ticking, false);
  }
  const finished = build(items, { state: 'done', ended_at: at(-30) });
  assert.equal(finished.current.elapsedMs, 90000);
  assert.equal(build(items, { state: 'done', status: 'running' }).current.state, 'done');
});

test('letter wave labels are uppercased without a misleading denominator', () => {
  const meter = build([wave('a'), row(1, 'done', 'a'), wave('b'), row(2, 'running', 'b')]);
  assert.equal(meter.current.label, 'Wave B');
});

test('unassigned inserted rows stay in the nearest preceding wave, matching the Board', () => {
  const meter = build([
    wave(1), row(1, 'done', 1), row(2, 'done', 1),
    wave(2), row(3, 'running', 2), row(4, 'queued', 2),
    row('3a', 'queued', null),
  ]);
  assert.equal(meter.current.label, 'Wave 2/2');
  assert.equal(meter.current.total, 3);
  assert.equal(meter.total.total, 5);
});

test('no wave and empty snapshot are valid; counts do not manufacture elapsed time or token usage', () => {
  const noWave = build([row(1, 'done', null), attempt(1, 'done', { started_at: at(-60), ended_at: at(-20), meta: { tokens: 42 } })]);
  assert.equal(noWave.current.label, 'Run');
  assert.equal(noWave.current.bar, '▰');
  assert.equal(noWave.current.elapsedMs, 40000);
  assert.equal(noWave.waves, null);
  const noWaveText = renderMeterText(noWave, { fenced: false });
  assert.equal(meterLines(noWaveText).length, 1);
  assert.match(noWaveText, /^🆃/);
  assert.match(noWaveText, /➜    Complete\n⮡    None queued/);
  const empty = meterFromSnapshot(null, { nowMs: NOW });
  assert.equal(empty.current.total, 0);
  assert.equal(empty.current.tokens, null);
  assert.equal(empty.current.elapsedMs, null);
  assert.equal(empty.current.bar, '');
});

test('wave completion counts only waves whose tasks all succeeded', () => {
  const meter = build([
    wave('A'), row(1, 'done', 'A'), row(2, 'failed', 'A'),
    wave('B'), row(3, 'done', 'B'), row(4, 'done', 'B'),
    wave('C'), row(5, 'skipped', 'C'),
  ]);
  assert.equal(meter.waves.done, 1);
  assert.equal(meter.waves.total, 3);
  assert.equal(meter.waves.bar, '▰▱▱');
  const concurrent = build([wave(1), row(1, 'running', 1), row(2, 'running', 1), wave(2), row(3, 'running', 2)]);
  assert.equal(concurrent.total.running, 3);
  assert.equal(concurrent.waves.running, 2);
});

test('display bars reserve track space but only actual items receive ticks', () => {
  assert.equal(displayBar({ done: 0, total: 4 }), '▱▱▱▱        ');
  assert.equal(displayBar({ done: 1, total: 4 }), '▰▱▱▱        ');
  assert.equal(displayBar({ done: 1, total: 2 }), '▰▱          ');
  assert.equal(displayBar({ done: 4, total: 4 }), '▰▰▰▰        ');
  assert.equal(displayBar({ done: 999, total: 1000 }).endsWith('▱'), true);
  assert.equal(displayBar(null), ' '.repeat(12));
  assert.equal(displayBar({ done: 1, total: 4 }, 8), '▰▱▱▱    ');
  const meter = build([wave('A'), row(1, 'done'), wave('B'), row(2, 'running', 'B'), row(3, 'queued', 'B')]);
  const lines = meterLines(renderMeterText(meter, { fenced: false }));
  assert.equal(lines[0].match(/[▰▱]+/)[0].length, 2);
  assert.equal(lines[1].match(/[▰▱]+/)[0].length, 2);
});

test('large task sets retain one tick per task', () => {
  const meter = build([wave(1), ...Array.from({ length: 40 }, (_, i) => row(i, i < 10 ? 'done' : 'queued', 1))]);
  assert.equal(meter.current.bar, '▰'.repeat(10) + '▱'.repeat(30));
  assert.equal(progressLabel(meter.current), '25%');
});

test('board total uses full row usage when the attempt list is capped, and never adds parent totals twice', () => {
  const run = {
    state: 'running', updated_at: at(-10),
    waves: [{ id: 'wave:A', label: 'wave A', rows: [{ id: 'row:1', state: 'running', started_at: at(-180), tokens: 800, attempt_count: 8,
      attempts: Array.from({ length: 6 }, (_, i) => ({ id: `run#1.${i + 3}`, state: i < 5 ? 'done' : 'running', started_at: at(-180 + i * 20), ended_at: i < 5 ? at(-165 + i * 20) : null, tokens: i < 5 ? 100 : null })),
    }] }],
  };
  const meter = meterFromBoardRun(run, { nowMs: NOW });
  assert.equal(meter.current.tokens, 800);
  assert.equal(meter.current.partialTokens, true);
  assert.equal(meter.current.elapsedMs, 180000);
  assert.equal(meter.current.ticking, true);
  const paused = meterFromBoardRun({ ...run, paused: true }, { nowMs: NOW });
  assert.equal(paused.current.elapsedMs, 180000);
  assert.equal(paused.current.ticking, true);
  const finished = structuredClone(run);
  finished.state = 'done'; finished.waves[0].rows[0].state = 'done';
  finished.waves[0].rows[0].attempts = finished.waves[0].rows[0].attempts.map(a => ({ ...a, state: 'done', tokens: 100 }));
  assert.equal(meterFromBoardRun(finished, { nowMs: NOW }).current.partialTokens, true, 'hidden history cannot prove complete token coverage');
});

test('a missing completion timestamp cannot freeze elapsed before a later task started', () => {
  const items = [wave('A'), row(1, 'done', 'A', { started_at: at(-100), ended_at: at(-60) }),
    row(2, 'done', 'A', { started_at: at(-50), updated_at: at(-10) })];
  const missing = build(items, { state: 'done' });
  assert.equal(missing.current.elapsedMs, null);
  assert.equal(missing.total.elapsedMs, null);
  const knownEnd = build(items, { state: 'done', ended_at: at(-10) });
  assert.equal(knownEnd.current.elapsedMs, 90000);
  const retry = build([wave('A'), row(1, 'done'), attempt(1, 'failed', { started_at: at(-100), ended_at: at(-60) }),
    attempt(1, 'done', { id: 'row:1/a2', started_at: at(-50) })], { state: 'done' });
  assert.equal(retry.current.elapsedMs, null);
});

test('hidden first-wave timer reserves columns without hiding tokens', () => {
  const meter = build([wave('A'), row(1, 'running', 'A', { started_at: at(-500), tokens: 16000 }), wave('B'), row(2, 'queued', 'B')]);
  assert.equal(meter.waves.hideElapsed, true);
  const initial = meterLines(renderMeterText(meter, { fenced: false }));
  assert.doesNotMatch(initial[0], /08:20/);
  assert.match(initial[1], /08:20/);
  assert.equal(initial[0].indexOf('16K'), initial[1].indexOf('16K'));
  meter.waves.hideElapsed = false;
  const later = meterLines(renderMeterText(meter, { fenced: false }));
  assert.equal(later[0].indexOf('08:20'), later[1].indexOf('08:20'));
  assert.equal(initial[0].indexOf('16K'), later[0].indexOf('16K'));
});

test('both visible clocks switch to hours together, including unknown and no-wave clocks', () => {
  const meter = build([wave('A'), row(0, 'done'), wave('B'), row(1, 'running', 'B'), attempt(1, 'running', { started_at: at(-600) })]);
  delete meter.forecast;
  meter.total.elapsedMs = 3599000;
  assert.equal(clockHours(meter), false);
  let lines = renderMeterText(meter, { fenced: false }).split('\n');
  assert.match(lines[0], /59:59/);
  assert.match(lines[1], /10:00/);
  meter.total.elapsedMs = 3600000;
  assert.equal(clockHours(meter), true);
  lines = renderMeterText(meter, { fenced: false }).split('\n');
  assert.match(lines[0], /01:00:00/);
  assert.match(lines[1], /00:10:00/);
  assert.equal(lines[0].indexOf('01:00:00'), lines[1].indexOf('00:10:00'));
  meter.current.elapsedMs = null;
  lines = renderMeterText(meter, { fenced: false }).split('\n');
  assert.match(lines[1], /🆃\s+[▰▱]+\s+—\s+—$/);
  assert.equal(lines[0].length, lines[1].length, 'unknown values stay in aligned numeric columns');
  meter.current.elapsedMs = 600000;
  meter.waves = null;
  assert.equal(clockHours(meter), false, 'hidden total does not force hours into a Tasks-only display');
  const single = renderMeterText(meter, { fenced: false });
  assert.match(single, /10:00/);
  assert.doesNotMatch(single, /00:10:00/);
  assert.equal(meterLines(single).length, 1);
  meter.current.elapsedMs = 3600000;
  assert.equal(clockHours(meter), true);
  assert.match(renderMeterText(meter, { fenced: false }), /01:00:00/);
});

test('input text cannot inject new lines, terminal escapes, or fences into the meter', () => {
  const meter = build([wave('A', { label: '\x1b[31mwave A\nBAD' }), row(1, 'blocked', 'A', { label: '\x1b[31mTask 1\n```', error: '\x1b[31merror\nextra' })]);
  assert.equal(meter.attention[0], 'Task 1 ```: error extra');
  assert.ok(!renderMeterText(meter).includes('\x1b'));
  assert.equal(renderMeterText(meter, { fenced: false }).split('\n').length, 2);
  assert.doesNotMatch(renderMeterText(meter), /Task 1/, 'unsafe label belongs only in Details and attention');
  assert.equal(renderMeterText(meter).match(/```/g).length, 2, 'task labels cannot terminate the code fence');
});

test('blocked and failed tasks stay visible even when no detail was recorded', () => {
  const meter = build([wave('A'), row(1, 'blocked'), row(2, 'failed'), row(3, 'waiting_input')]);
  assert.deepEqual(meter.attention, ['Task 1: blocked', 'Task 2: failed', 'Task 3: waiting for input']);
  const explicit = build([wave('A'), row(1, 'blocked')], { attention: [{ item: 'row:1', text: 'Choose the review scope' }] });
  assert.deepEqual(explicit.attention, ['Choose the review scope']);
  assert.deepEqual(build([], { state: 'failed' }).attention, ['Run failed']);
  const board = meterFromBoardRun({ state: 'running', rows: [{ id: '1', label: 'Checks', state: 'blocked' }] }, { nowMs: NOW });
  assert.deepEqual(board.attention, ['Checks: blocked']);
});

test('large values preserve alignment and long elapsed hours never wrap', () => {
  const current = { label: 'Wave 999/1000', done: 999, total: 1000, bar: '▰'.repeat(11) + '▱', elapsedMs: 6000000, tokens: 1e20, partialTokens: true };
  const meter = { waves: { ...current, label: 'Waves' }, current, total: { ...current, label: 'Total' }, attention: [], tokenNote: null };
  for (const columns of [72, 60, 48, 40, 32, 24]) {
    const lines = renderMeterText(meter, { columns, fenced: false }).split('\n');
    const visibleTicks = lines[0].match(/[▰▱]+/)[0];
    assert.ok(visibleTicks.length <= 24);
    assert.ok(visibleTicks.endsWith('▱'), 'the remaining task is retained when older completions are omitted');
    assert.equal(meter.current.total, 1000);
    assert.equal(lines[0].indexOf('01:40:00'), lines[1].indexOf('01:40:00'));
    assert.equal(lines[0].length, lines[1].length);
    assert.match(lines[0], /01:40:00/);
    assert.doesNotMatch(lines[0], /\d+\/\d+|%|tok|[│|*]/);
    assert.ok(lines.every(line => line.match(/[▰▱]+/)[0].length >= 8));
  }
  meter.current.elapsedMs = meter.total.elapsedMs = 1e15;
  const honest = renderMeterText(meter, { columns: 24, fenced: false });
  assert.match(honest, /277777777:46:40/);
});

test('time and tokens have stable unit formatting without rounding reported usage up', () => {
  assert.equal(formatElapsed(128000), '2m 08s');
  assert.equal(formatElapsed(3720000), '1h 02m');
  assert.equal(formatElapsed(null), '—');
  assert.equal(formatElapsed(128000, { compact: true }), '2m08s');
  assert.equal(formatClock(128000), '02:08');
  assert.equal(formatClock(3599000), '59:59');
  assert.equal(formatClock(3600000), '01:00:00');
  assert.equal(formatClock(3720000), '01:02:00');
  assert.equal(formatClock(6000000), '01:40:00');
  assert.equal(formatClock(600000, { hours: true }), '00:10:00');
  assert.equal(formatClock(360000000), '100:00:00');
  assert.equal(formatClock(0), '00:00');
  assert.equal(formatClock(0, { hours: true }), '00:00:00');
  assert.equal(formatClock(null), '—');
  assert.equal(formatClock(null, { hours: true }), '—');
  assert.equal(formatTokens(1999), '1.9K');
  assert.equal(formatTokens(64400), '64.4K');
  assert.equal(formatTokens(0), '0');
  assert.equal(formatTokens(null), '—');
});

test('clock emphasis stops at the first significant digit and leaves interior zeros intact', () => {
  for (const [clock, dim, active] of [
    ['00:04:32', '00:0', '4:32'], ['01:35:05', '0', '1:35:05'],
    ['00:00', '00:0', '0'], ['00:00:00', '00:00:0', '0'],
    ['10:00', '', '10:00'], ['100:00:00', '', '100:00:00'], ['—', '', '—'],
  ]) assert.deepEqual(clockParts(clock), { dim, active });
});

test('task summaries show all active work while full details retain plan order and actual attempt metadata', () => {
  const items = [
    wave('A'), row(1, 'done', 'A', { model: 'planned', effort: 'low' }),
    attempt(1, 'failed', { model: 'old', started_at: at(-100), ended_at: at(-80), meta: { tokens: 100 }, artifacts: [{ label: 'result', path: '/run/old.md' }] }),
    attempt(1, 'done', { id: 'row:1/a2', model: 'actual', effort: 'high', started_at: at(-70), ended_at: at(-50), meta: { tokens: 200 }, artifacts: [{ label: 'result', path: '/run/review.md' }, { label: 'commit', sha: '123abcd' }] }),
    row(2, 'blocked', 'A', { meta: { plan_status: 'Choose rollout scope' } }),
    wave('B'), row(3, 'running', 'B', { model: 'planned' }), attempt(3, 'running', { model: 'actual-live', effort: 'medium', started_at: at(-20) }),
    row(4, 'running', 'B'), row(5, 'queued', 'B'), row(6, 'queued', 'B'), row(7, 'queued', 'B'),
    row('3a', 'queued', null),
  ];
  const before = structuredClone(items);
  const meter = build(items, { attention: [{ item: 'row:2', text: 'Select the rollout scope in PLAN.md' }] });
  assert.deepEqual(items, before, 'report folding does not mutate source items');
  assert.deepEqual(meter.tasks.all.map(task => task.id), ['row:1', 'row:2', 'row:3', 'row:3a', 'row:4', 'row:5', 'row:6', 'row:7']);
  assert.deepEqual(meter.tasks.now.map(task => task.id), ['row:2', 'row:3', 'row:4']);
  assert.equal(meter.tasks.nowCount, 3);
  assert.deepEqual(meter.tasks.next.map(task => task.id), ['row:3a', 'row:5', 'row:6', 'row:7']);
  assert.equal(meter.tasks.nextCount, 4);
  assert.equal(meter.tasks.now[0].action, 'Select the rollout scope in PLAN.md');
  assert.equal(meter.tasks.now[0].detail, 'Choose rollout scope');
  assert.equal(meter.tasks.now[1].model, 'actual-live');
  assert.equal(meter.tasks.now[1].effort, 'medium');
  assert.equal(meter.tasks.now[1].elapsedMs, 20000);
  assert.equal(meter.tasks.now[1].tokens, null);
  assert.equal(meter.tasks.now[1].wave, 'Wave B');
  assert.deepEqual(meter.tasks.recent.map(task => [task.id, task.model, task.effort, task.result, task.sha, task.tokens, task.elapsedMs]), [
    ['row:1', 'actual', 'high', '/run/review.md', '123abcd', 300, 50000],
  ]);
  const text = renderMeterText(meter, { fenced: false });
  assert.match(text, /➜    Task 2 \(blocked\)\n➜    Task 3\n➜    Task 4/);
  assert.match(text, /⮡    Task 3a\n⮡    Task 5\n⮡    Task 6\n⮡    Task 7/);
  assert.doesNotMatch(text, /ready/i);
});

test('recent completions and board result metadata survive the compact summary', () => {
  const rows = Array.from({ length: 5 }, (_, i) => ({ id: `row:${i + 1}`, label: `Task ${i + 1}`, state: 'done', started_at: at(-100), ended_at: at(-80 + i * 10), tokens: i * 10,
    model: 'sonnet', effort: 'high', result: `/run/${i + 1}.md`, sha: `123abc${i}`, attempts: [] }));
  const meter = meterFromBoardRun({ state: 'done', waves: [{ id: 'wave:A', label: 'wave A', rows }] }, { nowMs: NOW });
  assert.deepEqual(meter.tasks.recent.map(task => task.id), ['row:5', 'row:4', 'row:3', 'row:2', 'row:1']);
  assert.equal(meter.tasks.recentCount, 5);
  assert.equal(meter.tasks.all.length, 5);
  assert.equal(meter.tasks.recent[0].result, '/run/5.md');
  assert.equal(meter.tasks.recent[0].sha, '123abc4');
  assert.equal(meter.tasks.recent[0].elapsedMs, 60000);
  assert.equal(meter.tasks.all[0].tokens, 0);
  assert.match(renderMeterText(meter), /☑    Task 5\n☑    Task 4\n☑    Task 3\n☑    Task 2\n☑    Task 1\n➜/);
  assert.match(renderMeterText(meter), /➜    Complete\n⮡    None queued/);
});

test('completion summary excludes failures and stays aligned when no successful work is known', () => {
  for (const meter of [build([]), build([wave('A'), row(1, 'failed'), row(2, 'skipped'), row(3, 'cancelled'), row(4, 'queued')])]) {
    const lines = renderMeterText(meter, { fenced: false }).split('\n');
    const summaries = lines.filter(line => /^[☑☐➜⮡]/.test(line));
    assert.equal(summaries.length, 2);
    assert.ok(summaries.every(line => !/^[☑☐]/.test(line)));
    assert.deepEqual(summaries.map(line => line.slice(1, 5)), ['    ', '    ']);
    assert.ok(lines.slice(0, lines.length - 2).every(line => /^[🆆🆃]/u.test(line) || /^ +(?:~|Complete|Stopped|Ended)/.test(line)), 'bars remain above all task summaries');
  }
  const unknownUsage = build([wave('A'), row(1, 'done'), row(2, 'completed'), row(3, 'failed')]);
  assert.equal(unknownUsage.tasks.all[0].tokens, null);
  assert.equal(unknownUsage.tasks.all[0].elapsedMs, null);
  // Defensive rendering also excludes a failed task accidentally placed in recent.
  unknownUsage.tasks.recent.unshift(unknownUsage.tasks.all[2]);
  unknownUsage.tasks.recentCount = 3;
  const text = renderMeterText(unknownUsage, { fenced: false });
  assert.match(text, /☑    Task 2\n☑    Task 1\n➜/);
  assert.doesNotMatch(text.split('\n').find(line => line.startsWith('☑')), /Task 3|more|done|completed/);
});

test('paused and terminal task summaries do not claim stale tasks are still running or ready', () => {
  const items = [wave('A'), row(1, 'running'), attempt(1, 'running', { started_at: at(-120) }), row(2, 'queued')];
  const paused = build(items, { paused: true });
  assert.equal(paused.tasks.now[0].state, 'running');
  assert.equal(paused.tasks.now[0].elapsedMs, 120000);
  assert.equal(paused.tasks.next[0].state, 'queued');
  assert.match(renderMeterText(paused), /➜    Task 1/);
  const ended = build(items, { state: 'cancelled', ended_at: at(-30) });
  assert.deepEqual(ended.tasks.all.map(task => task.state), ['interrupted', 'not_run']);
  assert.equal(ended.tasks.nowCount, 0);
  assert.equal(ended.tasks.nextCount, 0);
  const dead = build(items, { status: 'dead' });
  assert.deepEqual(dead.tasks.all.map(task => task.state), ['interrupted', 'queued'], 'a dead session does not cancel its remaining plan');
  assert.equal(dead.tasks.nextCount, 1);
});
