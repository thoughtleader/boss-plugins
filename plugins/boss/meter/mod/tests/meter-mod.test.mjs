import test from 'node:test';
import assert from 'node:assert/strict';
import { meterFromSnapshot } from '../../meter.mjs';
import { readRegistry, chooseRun, ownedRun } from '../src/registry.mjs';
import { meterTree, renderPane } from '../src/layout.mjs';
import { register } from '../src/register.mjs';

const NOW = Date.parse('2026-11-11T19:10:00Z');
const iso = seconds => new Date(NOW - seconds * 1000).toISOString();
const fixture = (id = 'fixture', owner = 'mine') => ({
  schema: 'boss.progress/1', tool: 'orchestrator', run_id: id, title: `Fixture ${id}`,
  owner: { session_id: owner }, state: 'running', started_at: iso(380), updated_at: iso(1),
  items: [
    { id: 'wave:A', kind: 'wave', label: 'wave A', state: 'done' },
    { id: 'row:1', parent: 'wave:A', kind: 'row', label: 'Setup', state: 'done', started_at: iso(380), ended_at: iso(128), meta: { tokens: 28000 } },
    { id: 'wave:B', kind: 'wave', label: 'wave B', state: 'running' },
    { id: 'row:2', parent: 'wave:B', kind: 'row', label: 'Implement', state: 'done', started_at: iso(128), ended_at: iso(20), meta: { tokens: 16000 } },
    { id: 'row:3', parent: 'wave:B', kind: 'row', label: 'Validate', state: 'running', started_at: iso(80) },
    { id: 'row:4', parent: 'wave:B', kind: 'gate', label: 'Review', state: 'queued' },
    { id: 'row:5', parent: 'wave:B', kind: 'row', label: 'Deliver', state: 'queued' },
  ], attention: [],
});
const element = type => props => ({ type, props, children: props.children || [] });
const ui = Object.fromEntries(['Box', 'Text', 'Button'].map(t => [t, element(t)]));
const flatten = tree => [tree, ...(tree.children || []).filter(x => x && typeof x === 'object').flatMap(flatten)];
const keyed = (tree, key) => flatten(tree).find(n => n.props?.key === key);
const ink = tree => typeof tree === 'string' ? tree : (tree.children || []).map(ink).join('');
const textOf = tree => tree.type === 'Text' ? ink(tree) : (tree.children || []).filter(x => x && typeof x === 'object').map(textOf).join('\n');
const entry = (snapshot, key = `${snapshot.tool}/${snapshot.run_id}`) => ({ key, snapshot, title: snapshot.title });

function ioFor(files) {
  return {
    read: async path => { if (!files.has(path)) throw new Error('not found'); return files.get(path); },
    list: async path => {
      const result = new Map();
      for (const p of files.keys()) if (p.startsWith(path + '/')) {
        const parts = p.slice(path.length + 1).split('/');
        result.set(parts[0], { name: parts[0], kind: parts.length > 1 ? 'dir' : 'file', mtimeMs: NOW, isLink: false });
      }
      return [...result.values()];
    },
  };
}

test('registry preserves valid identity and omits invalid or mismatched reports', async () => {
  const files = new Map([
    ['/r/runs/orchestrator/fixture/progress.json', JSON.stringify(fixture())],
    ['/r/runs/orchestrator/bad/progress.json', '{'],
    ['/r/runs/orchestrator/wrong/progress.json', JSON.stringify(fixture('different'))],
    ['/r/runs/orchestrator/new/progress.json', JSON.stringify({ ...fixture('new'), schema: 'boss.progress/2' })],
  ]);
  const r = await readRegistry(ioFor(files), '/r');
  assert.deepEqual(r.entries.map(x => x.key), ['orchestrator/fixture']);
  assert.equal(r.errors.length, 3);
});

test('automatic selection requires exactly one owned active run', () => {
  const mine = entry(fixture());
  const other = entry(fixture('other', 'someone-else'));
  const extra = entry(fixture('extra'));
  assert.equal(chooseRun([other], 'mine', null), null);
  assert.equal(chooseRun([mine, other], 'mine', null), mine);
  assert.equal(chooseRun([mine, extra], 'mine', null), null);
  assert.equal(chooseRun([mine, other], 'mine', other.key), other);
  assert.equal(chooseRun([mine], 'mine', 'orchestrator/vanished'), null);
  assert.equal(ownedRun([other], 'mine'), null);
});

test('wide rows have identical metric positions and right-aligned values', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  const tree = meterTree(ui, meter, 90);
  for (const part of ['label', 'progress', 'gap-a', 'time', 'gap-b', 'tokens']) {
    assert.equal(keyed(tree, `tasks-${part}`).props.width, keyed(tree, `waves-${part}`).props.width, part);
  }
  for (const row of ['tasks', 'waves']) for (const part of ['time', 'tokens']) {
    assert.equal(keyed(tree, `${row}-${part}`).props.justifyContent, 'flex-end');
    assert.equal(keyed(tree, `${row}-${part}`).props.flexShrink, 0);
    assert.equal(keyed(tree, `${row}-${part}`).props.flexWrap, 'nowrap');
    assert.equal(keyed(tree, row).props.flexWrap, 'nowrap');
  }
  assert.match(textOf(tree), /▰▱▱▱/);
  assert.match(textOf(tree), /02:08/);
  assert.match(textOf(tree), /06:20/);
  assert.match(textOf(tree), /44K/);
  assert.match(textOf(tree), /🆆/);
  assert.match(textOf(tree), /🆃/);
  assert.doesNotMatch(textOf(tree), /1\/4|%|tok|Total|Wave 2|[│|*]/);
  for (const [key, scope] of [['tasks', meter.current], ['waves', meter.waves]]) {
    assert.equal(keyed(tree, `${key}-badge`).props.width, 3);
    assert.equal(keyed(tree, `${key}-badge`).props.height, 1.2);
    assert.equal(keyed(tree, `${key}-badge`).props.borderStyle, undefined);
    const displayed = ink(keyed(tree, `${key}-progress-text`));
    assert.equal(displayed.length, scope.total, 'visible ticks represent actual items');
    assert.ok(keyed(tree, `${key}-progress`).props.width >= 2 * displayed.length, 'bar glyphs occupy more than one ch');
    assert.equal(keyed(tree, `${key}-label-text`).props.wrap, 'wrap', 'labels must not be ellipsized');
    assert.ok(!String(keyed(tree, `${key}-progress-text`).props.wrap).startsWith('truncate'), 'a bar must never be ellipsized');
    assert.doesNotMatch(displayed, /\n/, 'all visible ticks stay on one line');
    assert.notEqual(keyed(tree, `${key}-filled`).props.dimColor, true);
    assert.equal(keyed(tree, `${key}-empty`).props.dimColor, true);
    assert.equal(keyed(tree, `${key}-empty-ink`).props.dimColor, true);
  }
});

test('narrow layout preserves meter and uses matching metric rows', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  const tree = meterTree(ui, meter, 24);
  assert.ok(keyed(tree, 'tasks-heading'));
  assert.equal(ink(keyed(tree, 'tasks-progress-text')).length, 4);
  assert.equal(ink(keyed(tree, 'waves-progress-text')).length, 2);
  assert.equal(keyed(tree, 'tasks-progress').props.width, keyed(tree, 'waves-progress').props.width);
  assert.equal(keyed(tree, 'tasks-values').props.flexWrap, 'nowrap');
  assert.equal(keyed(tree, 'waves-values').props.flexWrap, 'nowrap');
  assert.equal(keyed(tree, 'tasks-time').props.width, keyed(tree, 'waves-time').props.width);
  assert.equal(keyed(tree, 'tasks-tokens').props.width, keyed(tree, 'waves-tokens').props.width);
  assert.match(textOf(tree), /▰▱▱▱/);
  const tiny = meterTree(ui, meter, 20);
  assert.ok(keyed(tiny, 'tasks-progress'));
  assert.ok(keyed(tiny, 'tasks-values'));
  assert.ok(keyed(tiny, 'waves-values'));
});

test('paired native clocks switch together to hour notation', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  meter.total.elapsedMs = meter.waves.elapsedMs = 3600000;
  const tree = meterTree(ui, meter, 90);
  assert.equal(ink(keyed(tree, 'waves-time')), '01:00:00');
  assert.equal(ink(keyed(tree, 'tasks-time')), '00:02:08');
  assert.equal(keyed(tree, 'waves-time').props.width, 8);
  assert.equal(keyed(tree, 'tasks-time').props.width, 8);
  assert.equal(ink(keyed(tree, 'tasks-time-leading')), '00:0');
  assert.equal(ink(keyed(tree, 'tasks-time-active')), '2:08');
  assert.equal(keyed(tree, 'tasks-time-leading').props.dimColor, true);
});

test('details are optional and provide the reported-token basis', () => {
  const snap = fixture();
  const state = { entry: entry(snap), meter: meterFromSnapshot(snap, { nowMs: NOW }), entries: [], errors: [], details: false };
  const actions = { details() {}, change() {}, select() {} };
  const compact = renderPane(ui, state, actions, 90);
  assert.equal(compact.children[0].props.key, 'meter');
  assert.equal(keyed(compact, 'run-title'), undefined);
  assert.equal(keyed(compact, 'token-note'), undefined);
  assert.equal(keyed(compact, 'partial-note'), undefined);
  assert.ok(keyed(compact, 'toggle-details'));
  assert.equal(keyed(compact, 'toggle-details').props.dimColor, true);
  const details = renderPane(ui, { ...state, details: true }, actions, 90);
  assert.ok(keyed(details, 'token-note'));
  assert.match(textOf(details), /Validate/);
});

test('completed summary uses successful task records and keeps critical attention visible', () => {
  const snap = fixture();
  const meter = meterFromSnapshot(snap, { nowMs: NOW });
  const done = meter.tasks.recent[0];
  const successes = [1, 2, 3, 4].map(id => ({ ...done, id: `done-${id}`, label: `Completed ${id}`, state: 'done' }));
  const failure = { ...done, id: 'failed', label: 'Failed review', state: 'failed' };
  meter.tasks.recent = [failure, successes[0], successes[1]];
  meter.tasks.all = [...meter.tasks.all.filter(t => !['done', 'completed'].includes(t.state)), ...successes, failure];
  meter.attention = ['Approval is required before continuing.'];
  const state = { entry: entry(snap), meter, entries: [], errors: [], details: false };
  const actions = { details() {}, change() {}, select() {} };
  const tree = renderPane(ui, state, actions, 90);
  const summary = textOf(keyed(tree, 'summary-done'));
  assert.match(summary, /Completed 1[\s\S]*Completed 2/);
  assert.doesNotMatch(summary, /Failed review|Completed 3|Completed 4|more/);
  assert.ok(keyed(tree, 'attention-0'));
  meter.tasks.recent = [];
  meter.tasks.all = meter.tasks.all.filter(t => !['done', 'completed'].includes(t.state));
  const empty = renderPane(ui, state, actions, 90);
  assert.equal(textOf(keyed(empty, 'summary-done')), '');
  assert.equal(keyed(empty, 'summary-done-icon-text'), undefined);
});

function engine(files, sid = 'mine') {
  const io = ioFor(files), handlers = [], calls = { status: [], open: [], cancelled: 0, invalidated: 0 };
  let tick;
  const $ = {
    session: { id: async () => sid }, env: { get: async k => k === 'HOME' ? '/h' : undefined },
    fs: io, clock: { now: async () => NOW, every: (_ms, fn) => { tick = fn; return { cancel() { calls.cancelled++; } }; } },
    command: { register: async () => {} },
    ui: {
      status: s => calls.status.push(s), invalidate: () => { calls.invalidated++; },
      open: async p => { calls.open.push(p); return { isPlaced: true }; }, resolve: () => ui,
    },
  };
  register((event, matcher, fn) => { handlers.push({ event, matcher: typeof matcher === 'function' ? null : matcher, fn: fn || matcher }); return { catch() {} }; }, { progress_dir: '/r' });
  const fire = (event, e = {}) => handlers.find(h => h.event === event && (!h.matcher || Object.entries(h.matcher).every(([k, v]) => e[k] === v))).fn($, e, async x => x);
  return { $, fire, calls, tick: async () => { tick(); await new Promise(resolve => setImmediate(resolve)); } };
}

test('lifecycle opens only on command, updates its owner status and never writes data', async () => {
  const files = new Map([['/r/runs/orchestrator/fixture/progress.json', JSON.stringify(fixture())]]);
  const e = engine(files);
  await e.fire('session.start');
  assert.equal(e.calls.open.length, 0);
  assert.match(e.calls.status.at(-1), /🆃 ▰▱▱▱ +02:08  16K/);
  await e.fire('command.run', { command: 'boss-meter', args: '' });
  assert.equal(e.calls.open.length, 1);
  const tree = await e.fire('ui.render', { component: 'Pane', requestId: 'boss-meter', props: { bodyColumns: 80 } });
  assert.ok(keyed(tree, 'meter'));
  await keyed(tree, 'toggle-details').props.onPress();
  const detail = await e.fire('ui.render', { component: 'Pane', requestId: 'boss-meter', props: { bodyColumns: 80 } });
  assert.ok(keyed(detail, 'token-note'));
  await e.fire('session.end');
  assert.equal(e.calls.cancelled, 1);
  assert.equal(e.calls.status.at(-1), undefined);
});

test('inspecting another session stays explicit and never publishes its status as ours', async () => {
  const files = new Map([['/r/runs/orchestrator/other/progress.json', JSON.stringify(fixture('other', 'someone-else'))]]);
  const e = engine(files);
  await e.fire('session.start');
  let tree = await e.fire('ui.render', { component: 'Pane', requestId: 'boss-meter', props: { bodyColumns: 80 } });
  assert.ok(keyed(tree, 'picker-heading'));
  assert.equal(keyed(tree, 'meter'), undefined);
  await keyed(tree, 'choose-orchestrator/other').props.onPress();
  tree = await e.fire('ui.render', { component: 'Pane', requestId: 'boss-meter', props: { bodyColumns: 80 } });
  assert.ok(keyed(tree, 'meter'));
  assert.ok(e.calls.status.every(value => value === undefined));
});

test('the open pane retains its owned run after it finishes', async () => {
  const path = '/r/runs/orchestrator/fixture/progress.json';
  const files = new Map([[path, JSON.stringify(fixture())]]);
  const e = engine(files);
  await e.fire('session.start');
  const done = fixture();
  done.state = 'done'; done.ended_at = iso(0);
  done.items = done.items.map(i => ({ ...i, state: 'done', ended_at: iso(0) }));
  files.set(path, JSON.stringify(done));
  await e.tick();
  const tree = await e.fire('ui.render', { component: 'Pane', requestId: 'boss-meter', props: { bodyColumns: 80 } });
  assert.ok(keyed(tree, 'meter'));
  assert.equal(keyed(tree, 'picker-heading'), undefined);
  assert.equal(e.calls.status.at(-1), undefined);
});

test('first-wave elapsed reserves the same column as the revealed second-wave clock', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  meter.waves.hideElapsed = true;
  const hidden = meterTree(ui, meter, 100);
  assert.equal(ink(keyed(hidden, 'waves-time')), '');
  assert.equal(keyed(hidden, 'waves-time').props.width, keyed(hidden, 'tasks-time').props.width);
  meter.waves.hideElapsed = false;
  const shown = meterTree(ui, meter, 100);
  for (const key of ['waves-time', 'waves-progress', 'waves-tokens']) assert.equal(keyed(hidden, key).props.width, keyed(shown, key).props.width);
});

test('single-wave display shows one endpoint and keeps reviewable forecast in Details', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  meter.waves = null;
  meter.total.waitingMs = 120000;
  meter.forecast = { checkpoint: { text: '~4 min after you respond' }, finish: { text: '~12 min after you respond' }, originalFinishAt: NOW, originalCheckpointAt: NOW, waiting: true, basis: 'Initial rough estimate', delayMs: 0 };
  const state = { entry: entry(fixture()), meter, entries: [], errors: [], details: true };
  const tree = renderPane(ui, state, {}, 100);
  assert.equal(keyed(tree, 'waves'), undefined);
  assert.equal(ink(keyed(tree, 'tasks-eta')), '~12 min after you respond');
  assert.match(textOf(tree), /Next reviewable result: Next result — ~4 min after you respond/);
  assert.match(textOf(tree), /Waiting for your input: 02:00/);
  assert.match(textOf(tree), /Original job forecast: Nov 11/);
});

test('every parallel task has its own activity and task disclosures are independent', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  const source = meter.tasks.now[0];
  meter.tasks.now = [1, 2, 3, 4].map(n => ({ ...source, id: `active-${n}`, label: `Task ${n}`, activity: `Checking ${n}`, instructions: `Full instructions ${n}` }));
  meter.tasks.all = [...meter.tasks.now];
  meter.tasks.nowCount = 4;
  const state = { entry: entry(fixture()), meter, entries: [], errors: [], details: true };
  const expandedTasks = new Set();
  const options = { expandedTasks, toggleTask: id => expandedTasks.has(id) ? expandedTasks.delete(id) : expandedTasks.add(id) };
  const compact = renderPane(ui, state, {}, 100, options);
  for (let n = 1; n <= 4; n++) assert.match(textOf(keyed(compact, `summary-now-active-${n}`)), new RegExp(`Task ${n} — Checking ${n}`));
  assert.doesNotMatch(textOf(compact), /Full instructions/);
  keyed(compact, 'task-toggle-active-2').props.onPress();
  const open = renderPane(ui, state, {}, 100, options);
  assert.match(textOf(open), /Full instructions 2/);
  assert.doesNotMatch(textOf(open), /Full instructions [134]/);
});

test('registry loads only matching optional presentation history', async () => {
  const snap = fixture();
  const files = new Map([
    ['/r/runs/orchestrator/fixture/progress.json', JSON.stringify(snap)],
    ['/r/report-state/orchestrator/fixture.json', JSON.stringify({ schema: 'boss.reporting/1', key: 'orchestrator/fixture', startedAt: snap.started_at, completedIds: ['row:1'] })],
  ]);
  let result = await readRegistry(ioFor(files), '/r');
  assert.deepEqual(result.entries[0].reporting.completedIds, ['row:1']);
  files.set('/r/report-state/orchestrator/fixture.json', JSON.stringify({ schema: 'boss.reporting/1', key: 'orchestrator/other', startedAt: snap.started_at }));
  result = await readRegistry(ioFor(files), '/r');
  assert.equal(result.entries[0].reporting, null);
  assert.deepEqual(result.errors, []);
});

test('ETAs wrap independently without moving incumbent desktop metric columns', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  const withEta = meterTree(ui, meter, 48);
  const withoutEta = meterTree(ui, { ...meter, forecast: null }, 48);
  for (const scope of ['tasks', 'waves']) {
    assert.equal(keyed(withEta, scope).props.flexDirection, 'row');
    assert.equal(keyed(withEta, `${scope}-block`).props.flexDirection, 'column');
    assert.ok(keyed(withEta, `${scope}-eta`));
    for (const part of ['label', 'progress', 'gap-a', 'time', 'gap-b', 'tokens']) assert.equal(keyed(withEta, `${scope}-${part}`).props.width, keyed(withoutEta, `${scope}-${part}`).props.width);
  }
});

test('wider native tracks use space before retiring completed ticks', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  meter.current.total = 24;
  meter.current.done = 15;
  const compact = meterTree(ui, meter, 44);
  const wide = meterTree(ui, meter, 100);
  const compactTicks = ink(keyed(compact, 'tasks-progress-text'));
  const wideTicks = ink(keyed(wide, 'tasks-progress-text'));
  assert.equal(compactTicks, '▰'.repeat(3) + '▱'.repeat(9));
  assert.equal(wideTicks, '▰'.repeat(15) + '▱'.repeat(9));
  assert.doesNotMatch(compactTicks + wideTicks, /\n/);
  assert.ok(keyed(wide, 'tasks-progress').props.width > keyed(compact, 'tasks-progress').props.width);
  assert.equal(keyed(wide, 'tasks-progress').props.width, keyed(wide, 'waves-progress').props.width);
  assert.equal(meter.current.done, 15);
  assert.equal(meter.current.total, 24);
  assert.equal(keyed(wide, 'meter').props.width, '100%');
});

test('the thirteenth tick remains beside recent completions rather than on a new line', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  meter.current.total = 13;
  meter.current.done = 12;
  const tree = meterTree(ui, meter, 44);
  assert.equal(ink(keyed(tree, 'tasks-progress-text')), '▰'.repeat(11) + '▱');
  assert.equal(keyed(tree, 'tasks-time').props.width, keyed(tree, 'waves-time').props.width);
});

test('task disclosures stay quiet and omit instructions that only repeat the short name', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  const source = meter.tasks.now[0];
  meter.tasks.all = [
    { ...source, id: 'duplicate', label: 'Validate', activity: null, instructions: 'Validate', result: null, sha: null, detail: null, action: null },
    { ...source, id: 'instructions', label: 'Validate', instructions: 'Check behavior using fixtures' },
  ];
  const tree = renderPane(ui, { entry: entry(fixture()), meter, entries: [], errors: [], details: true }, {}, 48);
  assert.equal(keyed(tree, 'task-toggle-duplicate'), undefined);
  assert.equal(keyed(tree, 'task-toggle-instructions').props.dimColor, true);
  assert.equal(keyed(tree, 'task-controls-instructions').props.flexDirection, 'row');
  assert.equal(keyed(tree, 'task-controls-instructions').props.justifyContent, 'flex-start');
  assert.equal(keyed(tree, 'tasks-eta-gutter').props.width, keyed(tree, 'tasks-label').props.width);
});

test('native task ETA follows its wave, not the later reviewable checkpoint', () => {
  const meter = meterFromSnapshot(fixture(), { nowMs: NOW });
  meter.forecast = { currentWave: { text: '~3:10PM' }, checkpoint: { text: '~3:20PM' }, finish: { text: '~3:30PM' } };
  const tree = meterTree(ui, meter, 100);
  assert.equal(ink(keyed(tree, 'waves-eta')), '~3:30PM');
  assert.equal(ink(keyed(tree, 'tasks-eta')), '~3:10PM');
  assert.doesNotMatch(textOf(tree), /3:20PM/);
});
