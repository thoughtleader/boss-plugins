'use strict';
// boss.progress/1 publisher tests (orchestrator/progress-mirror.js + manifest.js hook-in). Usage: node --test ~/.claude/orchestrator/test/progress-mirror.test.js
// Everything runs in a scratch ORCH_DIR / BOSS_PROGRESS_DIR; the real ~/.claude/boss registry is never touched.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs'), os = require('os'), path = require('path');
const { spawnSync, spawn } = require('child_process');
const NO_PROC = fs.existsSync('/proc/self/stat') ? false : 'no /proc here (macOS): this reads /proc, which is Linux only (council README, Platform)';

const CL = process.env.ORCH_TEST_ROOT || path.resolve(__dirname, '..');
const MAN = path.join(CL, 'lib', 'manifest.js');
const PM = require(path.join(CL, 'lib', 'progress-mirror.js'));
const SCHEMA = JSON.parse(fs.readFileSync(path.join(CL, 'lib', 'progress.schema.json'), 'utf8'));
let PH; // progress.mjs, loaded lazily through require(esm)
const ph = () => PH || (PH = require(path.join(CL, 'lib', 'progress.mjs')));

function sandbox(extra = {}) {
  const T = fs.mkdtempSync(path.join(os.tmpdir(), 'orch-progress-'));
  fs.writeFileSync(path.join(T, 'fake-wfp.js'), 'console.log(JSON.stringify({role:process.argv[3],bar:{model:"sonnet",effort:"high"}}));');
  const env = Object.assign({}, process.env, { ORCH_DIR: path.join(T, 'orch'), ORCH_RUNS_DIR: path.join(T, 'orch', 'runs'), ORCH_PROJECTS_DIR: path.join(T, 'p'),
    ORCH_METRICS_DB: path.join(T, 'm.db'), ORCH_SETTINGS: path.join(T, 's.json'), ORCH_WFP_JS: path.join(T, 'fake-wfp.js'), ORCH_SESSIONS_DIR: path.join(T, 'sessions'),
    BOSS_PROGRESS_DIR: path.join(T, 'boss') }, extra);
  delete env.WFP_SID;
  fs.mkdirSync(path.join(T, 'sessions'), { recursive: true });
  const man = (...args) => spawnSync(process.execPath, [MAN, ...args], { env, encoding: 'utf8' });
  const runDir = run => { const d = path.join(env.ORCH_RUNS_DIR, run); fs.mkdirSync(d, { recursive: true }); return d; };
  const snapFile = run => path.join(env.BOSS_PROGRESS_DIR, 'runs', 'orchestrator', run, 'progress.json');
  const snap = run => JSON.parse(fs.readFileSync(snapFile(run), 'utf8'));
  const launch = (run, row, sid, extra = []) => man('launch', '--run', run, '--row', row, '--role', 'implementation', '--agent', 'orch-builder', '--model', 'sonnet', '--effort', 'high',
    '--mode', 'subagent', '--repo', 'x', '--area', 'y', '--sid', sid, ...extra);
  const ev = (run, type, fields) => man('event', '--run', run, '--type', type, '--json', JSON.stringify(fields));
  const lines = run => fs.readFileSync(path.join(env.ORCH_RUNS_DIR, run, 'manifest.jsonl'), 'utf8').split('\n').filter(Boolean);
  return { T, env, man, runDir, snapFile, snap, launch, ev, lines, done: () => fs.rmSync(T, { recursive: true, force: true }) };
}
const valid = s => assert.deepEqual(ph().validate(s, SCHEMA), []);
const strip = s => { const c = JSON.parse(JSON.stringify(s)); delete c.writer; return c; };

// ---------------------------------------------------------------- pure fold
const parseAttemptId = id => { const m = /^(.+)#(\d+[a-z]?)\.(\d+)$/.exec(id); return m ? { id, run: m[1], row: m[2], n: +m[3] } : null; };
const E = (seq, type, f) => Object.assign({ id: `r#e${seq}`, seq, type, ts: `2026-10-31T10:00:${String(seq).padStart(2, '0')}.000Z`, run: 'r', attempt: null, row: null, key: `k${seq}`, session: 'S1' }, f);
const ctx = (over = {}) => Object.assign({ runDir: '/x/runs/r', plan: null, cfg: {}, parseAttemptId, ownerFor: sid => ({ session_id: sid, pid: null, start_ticks: null, inbox: null }), now: Date.parse('2026-10-31T11:00:00Z'),
  writer: { pid: 1, start_ticks: null, boot_id: null, kind: 'oneshot' } }, over);

test('fold: launch, start override, non-final stop ignored, final stop closes, outcome meta', () => {
  const base = [
    E(1, 'launch', { attempt: 'r#1.1', row: '1', n: 1, model: 'sonnet', effort: 'high', agent: 'orch-builder', why: 'creation' }),
  ];
  let s = PM.buildSnapshot('r', base, ctx());
  let a = s.items.find(i => i.id === 'row:1/a1');
  assert.equal(a.state, 'queued'); assert.equal(a.model, 'sonnet'); assert.equal(s.seq, 1);

  const started = base.concat([E(2, 'start', { attempt: 'r#1.1', row: '1', model: 'opus', effort: 'medium' })]);
  s = PM.buildSnapshot('r', started, ctx()); a = s.items.find(i => i.id === 'row:1/a1');
  assert.equal(a.state, 'running'); assert.equal(a.model, 'opus'); assert.equal(a.effort, 'medium'); assert.equal(a.meta.launch_model, 'sonnet/high'); assert.ok(a.started_at);
  assert.equal(s.items.find(i => i.id === 'row:1').state, 'running');

  const substop = started.concat([E(3, 'stop', { attempt: 'r#1.1', row: '1', final: false, phase: 'agent-stopped', status: null })]);
  s = PM.buildSnapshot('r', substop, ctx());
  assert.equal(s.items.find(i => i.id === 'row:1/a1').state, 'running', 'a non-final stop never closes an attempt');

  const fin = substop.concat([E(4, 'stop', { attempt: 'r#1.1', row: '1', final: true, phase: 'finished', status: 'completed', model_id: 'claude-opus-x', effort_observed: 'high', tokens: 99 }),
    E(5, 'outcome', { attempt: 'r#1.1', row: '1', first_pass: true, retries: 0, escalated: false })]);
  s = PM.buildSnapshot('r', fin, ctx()); a = s.items.find(i => i.id === 'row:1/a1');
  assert.equal(a.state, 'done'); assert.equal(a.model, 'claude-opus-x'); assert.equal(a.effort, 'high'); assert.ok(a.ended_at);
  assert.deepEqual([a.meta.first_pass, a.meta.retries, a.meta.escalated, a.meta.tokens], [true, 0, false, 99]);
  assert.equal(s.seq, 5);
  valid(s);

  const failed = started.concat([E(4, 'stop', { attempt: 'r#1.1', row: '1', final: true, phase: 'failed', status: 'failed' })]);
  s = PM.buildSnapshot('r', failed, ctx());
  assert.equal(s.items.find(i => i.id === 'row:1/a1').state, 'failed'); assert.equal(s.items.find(i => i.id === 'row:1').state, 'failed');
});

test('fold: a voided attempt (allocated, never launched) leaves no item; one that later ran is real again', () => {
  const base = [E(1, 'launch', { attempt: 'r#1.1', row: '1', n: 1, model: 'sonnet', effort: 'high', agent: 'orch-builder' }), E(2, 'launch', { attempt: 'r#1.2', row: '1', n: 2, model: 'sonnet', effort: 'high', agent: 'orch-builder' })];
  let s = PM.buildSnapshot('r', base, ctx());
  assert.deepEqual(s.items.filter(i => i.kind === 'attempt').map(i => i.id), ['row:1/a1', 'row:1/a2']);
  const voided = base.concat([E(3, 'void', { attempt: 'r#1.2', row: '1' })]);
  s = PM.buildSnapshot('r', voided, ctx());
  assert.deepEqual(s.items.filter(i => i.kind === 'attempt').map(i => i.id), ['row:1/a1'], 'the voided attempt is gone');
  const ran = voided.concat([E(4, 'start', { attempt: 'r#1.2', row: '1' })]);
  s = PM.buildSnapshot('r', ran, ctx());
  assert.deepEqual(s.items.filter(i => i.kind === 'attempt').map(i => i.id), ['row:1/a1', 'row:1/a2'], 'a start after the void: it ran, so it counts');
});

test('fold: an idle "waiting" stop (completed, newer than the last start) ends the attempt as done; a newer start reopens it; failed status does not', () => {
  const ev = [
    E(1, 'launch', { attempt: 'r#1.1', row: '1', n: 1, model: 'sonnet', effort: 'high', agent: 'orch-builder' }),
    E(2, 'start', { attempt: 'r#1.1', row: '1' }),
    E(3, 'stop', { attempt: 'r#1.1', row: '1', final: false, phase: 'agent-stopped', status: null }),
    E(4, 'stop', { attempt: 'r#1.1', row: '1', final: false, phase: 'waiting', status: 'completed', tokens: 500, tools: 7, ms: 4000 }),
  ];
  let s = PM.buildSnapshot('r', ev, ctx());
  let a = s.items.find(i => i.id === 'row:1/a1');
  assert.equal(a.state, 'done'); assert.ok(a.ended_at); assert.equal(a.meta.tokens, 500);
  assert.equal(s.items.find(i => i.id === 'row:1').state, 'done');
  assert.equal(s.counts[0].running, 0, 'no running row');
  valid(s);
  // a PLAN cell that says done agrees; the idle attempt no longer overrides it
  s = PM.buildSnapshot('r', ev, ctx({ plan: { rows: new Map([['1', { row: '1', subtask: 'x', status: 'done' }]]), order: ['1'], mtimeMs: 0 } }));
  assert.equal(s.items.find(i => i.id === 'row:1').state, 'done');
  // continued: a newer start puts it back to running
  s = PM.buildSnapshot('r', ev.concat([E(5, 'start', { attempt: 'r#1.1', row: '1' })]), ctx());
  assert.equal(s.items.find(i => i.id === 'row:1/a1').state, 'running');
  // a waiting stop that is older than the last start does not count
  s = PM.buildSnapshot('r', [ev[0], ev[1], ev[3], E(5, 'start', { attempt: 'r#1.1', row: '1' })], ctx());
  assert.equal(s.items.find(i => i.id === 'row:1/a1').state, 'running');
  // waiting with a non-completed status stays running
  s = PM.buildSnapshot('r', [ev[0], ev[1], E(4, 'stop', { attempt: 'r#1.1', row: '1', final: false, phase: 'waiting', status: 'failed' })], ctx());
  assert.equal(s.items.find(i => i.id === 'row:1/a1').state, 'running');
});

test('fold: a provisional waiting stop (provisional: true) keeps the attempt open and running, says what it waits on, and a final or promoted stop ends it', () => {
  const base = [
    E(1, 'launch', { attempt: 'r#1.1', row: '1', n: 1, model: 'sonnet', effort: 'high', agent: 'orch-builder' }),
    E(2, 'start', { attempt: 'r#1.1', row: '1' }),
    E(3, 'stop', { attempt: 'r#1.1', row: '1', final: false, phase: 'waiting', status: 'completed', provisional: true, tokens: 500, tools: 7, ms: 4000 }),
  ];
  let s = PM.buildSnapshot('r', base, ctx());
  let a = s.items.find(i => i.id === 'row:1/a1'), row = s.items.find(i => i.id === 'row:1');
  assert.equal(a.state, 'running'); assert.equal(a.detail, 'waiting on its own background work'); assert.equal(a.ended_at, undefined);
  assert.equal(row.state, 'running'); assert.equal(row.activity, 'waiting on its own background work');
  assert.equal(s.counts[0].running, 1, 'a running row, not a finished one');
  valid(s);
  // a plan row's own activity cell wins over the generated one
  s = PM.buildSnapshot('r', base, ctx({ plan: { txt: '| # | Subtask | Activity | Status |\n|---|---|---|---|\n| 1 | x | writing tests | running |\n', fm: {}, mtimeMs: 0 } }));
  assert.equal(s.items.find(i => i.id === 'row:1').activity, 'writing tests');
  // the true final notice replaces it
  s = PM.buildSnapshot('r', base.concat([E(4, 'stop', { attempt: 'r#1.1', row: '1', final: true, phase: 'finished', status: 'completed', tokens: 900 })]), ctx());
  a = s.items.find(i => i.id === 'row:1/a1'); assert.equal(a.state, 'done'); assert.equal(a.detail, undefined); assert.equal(a.meta.tokens, 900);
  // a promoted final stop (ledger.js close / waiting-timeout) ends it too
  s = PM.buildSnapshot('r', base.concat([E(4, 'stop', { attempt: 'r#1.1', row: '1', final: true, phase: 'finished', status: 'completed', promoted: 'close', tokens: 500 })]), ctx());
  assert.equal(s.items.find(i => i.id === 'row:1/a1').state, 'done'); assert.equal(s.items.find(i => i.id === 'row:1').state, 'done');
  // the promoted stop's own ts is the (later) promotion time: the attempt's ended_at is the waiting notice's time (ended_at on the stop); ts still orders it
  const promoted = E(4, 'stop', { attempt: 'r#1.1', row: '1', final: true, phase: 'finished', status: 'completed', promoted: 'waiting-timeout', ts: '2026-10-31T14:00:00.000Z', ended_at: '2026-10-31T10:00:03.000Z', tokens: 500 });
  s = PM.buildSnapshot('r', base.concat([promoted]), ctx());
  a = s.items.find(i => i.id === 'row:1/a1');
  assert.equal(a.state, 'done'); assert.equal(a.ended_at, '2026-10-31T10:00:03.000Z', 'the waiting notice time, not the promotion time'); assert.equal(a.meta.tokens, 500);
  assert.equal(a.updated_at, '2026-10-31T14:00:00.000Z', 'updated_at keeps reading the write time');
  valid(s);
  // a promoted stop recorded before the field existed (no ended_at), or with an unreadable one, reads exactly as before: its ts
  for (const bad of [undefined, 'not a date', 5]) {
    const old = Object.assign({}, promoted); if (bad === undefined) delete old.ended_at; else old.ended_at = bad;
    assert.equal(PM.buildSnapshot('r', base.concat([old]), ctx()).items.find(i => i.id === 'row:1/a1').ended_at, '2026-10-31T14:00:00.000Z', String(bad));
  }
  // a newer start (resumed): running as before, without the waiting note being the last word
  s = PM.buildSnapshot('r', base.concat([E(4, 'start', { attempt: 'r#1.1', row: '1' })]), ctx());
  assert.equal(s.items.find(i => i.id === 'row:1/a1').state, 'running'); assert.equal(s.items.find(i => i.id === 'row:1/a1').detail, undefined);
});

test('names: attempt and row are labelled by the human subtask, not the attempt id or model/effort', () => {
  const evs = [
    E(1, 'launch', { attempt: 'r#1.1', row: '1', n: 1, model: 'sonnet', effort: 'high', subtask: 'Explicit launch subtask' }),
    E(2, 'start', { attempt: 'r#1.1', row: '1', description: '[sonnet/high] r#1.1 Ignored: launch subtask wins' }),
    E(3, 'launch', { attempt: 'r#3c.1', row: '3c', n: 1, model: 'opus', effort: 'high', subtask: null }),
    E(4, 'start', { attempt: 'r#3c.1', row: '3c', description: '[opus/high] r#3c.1 Combined design: B home + A job page' }),
    E(5, 'launch', { attempt: 'r#3c.2', row: '3c', n: 2, model: 'opus', effort: 'high', subtask: null }),
    E(6, 'launch', { attempt: 'r#4.1', row: '4', n: 1, model: 'opus', effort: 'high', subtask: null }),
    E(7, 'start', { attempt: 'r#4.1', row: '4', description: '[auto] r#4.1 ' }),
  ];
  const s = PM.buildSnapshot('r', evs, ctx());
  const L = id => s.items.find(i => i.id === id).label;
  assert.equal(L('row:1/a1'), 'Explicit launch subtask');
  assert.equal(L('row:3c/a1'), 'Combined design: B home + A job page');
  assert.equal(L('row:3c'), 'Combined design: B home + A job page', 'row takes the newest start description');
  assert.equal(L('row:3c/a2'), 'Combined design: B home + A job page', 'no start of its own: the row\'s name');
  assert.equal(L('row:4/a1'), 'r#4.1', 'nothing to name it by: the id is the last resort');
  assert.equal(s.items.find(i => i.id === 'row:3c/a2').meta.attempt_id, 'r#3c.2');
  assert.ok(s.items.find(i => i.id === 'row:3c/a1').meta.launched_at);
  const plan = { txt: '| # | Subtask | Agent | Model | Effort | Wave | Status |\n|--|--|--|--|--|--|--|\n| 4 | Plan row four | orch-builder | opus | high | 1 | |\n', fm: {}, mtimeMs: 0 };
  assert.equal(PM.buildSnapshot('r', evs, ctx({ plan })).items.find(i => i.id === 'row:4/a1').label, 'Plan row four', 'plan subtask beats the id');
});

test('fold: a new attempt replaces the previous one and decides the row state; row order and replay are stable', () => {
  const evs = [
    E(1, 'launch', { attempt: 'r#2.1', row: '2', n: 1 }), E(2, 'start', { attempt: 'r#2.1', row: '2' }),
    E(3, 'stop', { attempt: 'r#2.1', row: '2', final: true, status: 'failed' }),
    E(4, 'launch', { attempt: 'r#2.2', row: '2', n: 2 }), E(5, 'start', { attempt: 'r#2.2', row: '2' }),
  ];
  const s = PM.buildSnapshot('r', evs, ctx());
  const a2 = s.items.find(i => i.id === 'row:2/a2');
  assert.equal(a2.replaces, 'row:2/a1'); assert.equal(a2.attempt, 2); assert.equal(s.items.find(i => i.id === 'row:2').state, 'running');
  assert.equal(s.items.find(i => i.id === 'row:2/a1').state, 'failed');
  assert.deepEqual(PM.buildSnapshot('r', evs.slice().reverse(), ctx()).items, s.items, 'event order in the input does not matter');
  valid(s);
});

test('plan: tolerant table parse, waves, gates, scoped counts, attention from a gate status', () => {
  const plan = `---\nrun: r\ntier: small\nmode: B\ngoal: ship it\n---\n# Plan\n| # | Subtask | Agent | Model/Effort | Wave | Status |\n|---|---|---|---|---|---|\n` +
    `| 1 | Build A | orch-builder | sonnet/high | 1 | done |\n| 2 | Build B | orch-builder | sonnet/high | 1 | pending |\n| 3 | Review | orch-reviewer | sonnet/high | 2 | gaps_found |\n| x | junk row | - | - | - | - |\n`;
  const evs = [E(1, 'launch', { attempt: 'r#2.1', row: '2', n: 1 }), E(2, 'start', { attempt: 'r#2.1', row: '2' })];
  const s = PM.buildSnapshot('r', evs, ctx({ plan: { txt: plan, fm: { tier: 'small', mode: 'B', goal: 'ship it' }, mtimeMs: Date.parse('2026-10-31T10:30:00Z') } }));
  assert.equal(s.title, 'ship it'); assert.equal(s.ext.orchestrator.tier, 'small'); assert.equal(s.ext.orchestrator.plan_mode, 'B');
  assert.deepEqual(s.items.filter(i => i.kind === 'wave').map(i => [i.id, i.state]), [['wave:1', 'running'], ['wave:2', 'blocked']]);
  assert.equal(s.items.find(i => i.id === 'row:3').kind, 'gate');
  assert.equal(s.items.find(i => i.id === 'row:3').parent, 'wave:2');
  assert.equal(s.items.find(i => i.id === 'row:1').parent, 'wave:1');
  assert.equal(s.phase, 'wave 1');
  assert.deepEqual(s.counts.map(c => [c.unit, c.label, c.done, c.total, c.running]), [['rows', 'rows', 1, 3, 1], ['wave', 'wave 1', 1, 2, 1]]);
  assert.equal(s.attention.length, 1); assert.equal(s.attention[0].item, 'row:3'); assert.equal(s.attention[0].kind, 'approval');
  assert.equal(s.items.find(i => i.id === 'row:x'), undefined, 'a non-row id is skipped');
  valid(s);
  assert.equal(PM.buildSnapshot('r', evs, ctx({ plan: { txt: 'not a plan', fm: {}, mtimeMs: 1 } })).items.filter(i => i.kind === 'wave').length, 0, 'no Wave column: no wave items');
});

// Events shaped like the follow-ups run's manifest: a continuation is a launch with `continues`, `task_id` and `cause` and never gets a start event;
// its stop (final:true) is attributed to the continuation attempt, and the predecessor only gets a non-final `agent-stopped` line.
test('continuation: a launch with `continues` and no stop yet is running (active task), until its final stop arrives', () => {
  const plan = '| # | Subtask | Short name | Activity | Agent | Model/Effort | Wave | Status |\n|---|---|---|---|---|---|---|---|\n| 4 | Orchestrator small fixes | Small fixes | Fixing the review notes | orch-builder | sonnet/high | 1 | pending |\n';
  const pctx = () => ctx({ plan: { txt: plan, fm: {}, mtimeMs: Date.parse('2026-10-31T09:00:00Z') } });
  const first = [
    E(1, 'launch', { attempt: 'r#4.1', row: '4', n: 1, model: 'sonnet', effort: 'high', agent: 'orch-builder', task_id: 'ad73' }),
    E(2, 'start', { attempt: 'r#4.1', row: '4', description: '[sonnet/high] r#4.1 Orchestrator small fixes' }),
    E(3, 'stop', { attempt: 'r#4.1', row: '4', final: true, phase: 'finished', status: 'completed' }),
  ];
  const cont = E(4, 'launch', { attempt: 'r#4.2', row: '4', n: 2, model: 'sonnet', effort: 'high', agent: 'orch-builder', continues: 'r#4.1', task_id: 'ad73', cause: 'review-findings' });
  let s = PM.buildSnapshot('r', first.concat([cont]), pctx());
  let a2 = s.items.find(i => i.id === 'row:4/a2'), row = s.items.find(i => i.id === 'row:4');
  assert.equal(a2.state, 'running', 'no start event ever comes for a continuation'); assert.equal(a2.started_at, cont.ts);
  assert.equal(row.state, 'running'); assert.equal(row.short_label, 'Small fixes'); assert.equal(row.activity, 'Fixing the review notes');
  assert.deepEqual(s.counts[0].running, 1); assert.match(s.summary, /1 running/);
  valid(s);
  // the final stop (attributed to the continuation) ends it; a non-final stop on the predecessor does not
  const substop = E(5, 'stop', { attempt: 'r#4.1', row: '4', final: false, phase: 'agent-stopped', status: null });
  assert.equal(PM.buildSnapshot('r', first.concat([cont, substop]), pctx()).items.find(i => i.id === 'row:4/a2').state, 'running');
  s = PM.buildSnapshot('r', first.concat([cont, substop, E(6, 'stop', { attempt: 'r#4.2', row: '4', final: true, phase: 'finished', status: 'completed' })]), pctx());
  assert.equal(s.items.find(i => i.id === 'row:4/a2').state, 'done'); assert.equal(s.items.find(i => i.id === 'row:4').state, 'done');
  // a plain retry launch (no `continues`) with no start is still queued
  s = PM.buildSnapshot('r', first.concat([E(4, 'launch', { attempt: 'r#4.2', row: '4', n: 2, model: 'sonnet', effort: 'high' })]), pctx());
  assert.equal(s.items.find(i => i.id === 'row:4/a2').state, 'queued'); assert.equal(s.items.find(i => i.id === 'row:4').state, 'queued');
});

test('gate: no "needs your decision" line while the fix round runs or has finished after the review; shown when nothing is being fixed', () => {
  const plan = '| # | Subtask | Agent | Model/Effort | Wave | Status |\n|---|---|---|---|---|---|\n| 4 | Small fixes | orch-builder | sonnet/high | 1 | done |\n| 7 | Review the branch | orch-reviewer | opus/high | 2 | gaps_found |\n';
  const pctx = () => ctx({ plan: { txt: plan, fm: {}, mtimeMs: Date.parse('2026-10-31T09:00:00Z') } });
  const fixed = [ // row 4 built and finished, then the review (row 7) finished with gaps
    E(1, 'launch', { attempt: 'r#4.1', row: '4', n: 1, model: 'sonnet', effort: 'high' }), E(2, 'start', { attempt: 'r#4.1', row: '4' }),
    E(3, 'stop', { attempt: 'r#4.1', row: '4', final: true, phase: 'finished', status: 'completed' }),
    E(4, 'launch', { attempt: 'r#7.1', row: '7', n: 1, model: 'opus', effort: 'high' }), E(5, 'start', { attempt: 'r#7.1', row: '7' }),
    E(6, 'stop', { attempt: 'r#7.1', row: '7', final: true, phase: 'finished', status: 'completed' }),
  ];
  const decision = s => s.attention.filter(a => /needs your decision/.test(a.text));
  const row7 = s => s.items.find(i => i.id === 'row:7');
  let s = PM.buildSnapshot('r', fixed, pctx());
  assert.equal(decision(s).length, 1, 'nothing under repair: the owner decides'); assert.equal(decision(s)[0].text, 'Row 7 is gaps_found: needs your decision'); assert.equal(row7(s).state, 'blocked');
  // the fix round: row 4 continued with cause review-findings, no start event, no stop yet
  const cont = E(7, 'launch', { attempt: 'r#4.2', row: '4', n: 2, model: 'sonnet', effort: 'high', continues: 'r#4.1', task_id: 't1', cause: 'review-findings' });
  s = PM.buildSnapshot('r', fixed.concat([cont]), pctx());
  assert.equal(decision(s).length, 0, 'the fix round is running'); assert.equal(s.attention.length, 0); assert.equal(row7(s).state, 'queued', 'the gate waits for the fix round, it is not blocked');
  assert.equal(s.items.find(i => i.id === 'row:4').state, 'running'); valid(s);
  // finished after the review: still no decision line
  s = PM.buildSnapshot('r', fixed.concat([cont, E(8, 'stop', { attempt: 'r#4.2', row: '4', final: true, phase: 'finished', status: 'completed' })]), pctx());
  assert.equal(decision(s).length, 0, 'the fix round has finished after the review');
  // B1: a review that needs a person (human_needed) always stays blocked and keeps its line, whatever is being fixed
  const hn = ctx({ plan: { txt: plan.replace('gaps_found', 'human_needed'), fm: {}, mtimeMs: Date.parse('2026-10-31T09:00:00Z') } });
  s = PM.buildSnapshot('r', fixed.concat([cont]), hn);
  assert.equal(row7(s).state, 'blocked'); assert.deepEqual(s.attention.map(a => a.text), ['Row 7 is human_needed: needs your decision']);
  // `revise` is repairable like gaps_found
  s = PM.buildSnapshot('r', fixed.concat([cont]), ctx({ plan: { txt: plan.replace('gaps_found', 'revise'), fm: {}, mtimeMs: Date.parse('2026-10-31T09:00:00Z') } }));
  assert.equal(row7(s).state, 'queued'); assert.equal(s.attention.length, 0);
  // C2: a fix round that was closed `blocked` or `dropped` (ledger.js close --outcome) is not a fix round: the line comes back; `accepted` or `retry` still count
  const fin = E(8, 'stop', { attempt: 'r#4.2', row: '4', final: true, phase: 'finished', status: 'completed' });
  for (const [outcome, lines] of [['blocked', 1], ['dropped', 1], ['accepted', 0], ['retry', 0]]) {
    s = PM.buildSnapshot('r', fixed.concat([cont, fin, E(9, 'outcome', { attempt: 'r#4.2', row: '4', outcome })]), pctx());
    assert.equal(decision(s).length, lines, outcome); assert.equal(row7(s).state, lines ? 'blocked' : 'queued', outcome);
  }
  // a fix round that ended before the review does not count; neither does a continuation with another cause, nor a queued (never started) fresh launch
  const early = [fixed[0], fixed[1], E(3, 'launch', { attempt: 'r#4.2', row: '4', n: 2, model: 'sonnet', effort: 'high', continues: 'r#4.1', cause: 'review-findings' }),
    E(4, 'stop', { attempt: 'r#4.2', row: '4', final: true, phase: 'finished', status: 'completed' }), ...fixed.slice(3).map(e => Object.assign({}, e, { seq: e.seq + 2, id: `r#e${e.seq + 2}`, ts: `2026-10-31T10:00:${String(e.seq + 2).padStart(2, '0')}.000Z` }))];
  assert.equal(decision(PM.buildSnapshot('r', early, pctx())).length, 1, 'a fix launched before the review is not a fix of its findings');
  assert.equal(decision(PM.buildSnapshot('r', fixed.concat([Object.assign({}, cont, { cause: 'owner-tune' })]), pctx())).length, 1, 'a continuation for another cause is not a fix round');
  assert.equal(decision(PM.buildSnapshot('r', fixed.concat([E(7, 'launch', { attempt: 'r#4.2', row: '4', n: 2, cause: 'review-findings' })]), pctx())).length, 1, 'a launch that has not started is not running');
  // `Depends on` names the rows a review covers: a retry for review findings of a row it does not name is not its fix round
  const dplan = plan.replace('| Agent | Model/Effort | Wave | Status |\n|---|---|---|---|---|---|', '| Depends on | Agent | Model/Effort | Wave | Status |\n|---|---|---|---|---|---|---|')
    .replace('| 4 | Small fixes | ', '| 4 | Small fixes | | ').replace('| 7 | Review the branch | ', '| 7 | Review the branch | %D% | ');
  const dctx = d => ctx({ plan: { txt: dplan.replace('%D%', d), fm: {}, mtimeMs: Date.parse('2026-10-31T09:00:00Z') } });
  assert.equal(decision(PM.buildSnapshot('r', fixed.concat([cont]), dctx('4'))).length, 0, 'row 4 is named: its fix round counts');
  assert.equal(decision(PM.buildSnapshot('r', fixed.concat([cont]), dctx('5'))).length, 1, 'row 4 is not named: another review\'s fix round');
  // a later attempt of the review row itself (re-check) that finished: the gate was the first attempt's result, so no decision line (the cell may be stale);
  // when that re-check's own result still says gaps, the decision line is back
  const recheck = fixed.concat([E(7, 'launch', { attempt: 'r#7.2', row: '7', n: 2 }), E(8, 'start', { attempt: 'r#7.2', row: '7' }), E(9, 'stop', { attempt: 'r#7.2', row: '7', final: true, phase: 'finished', status: 'completed' })]);
  const resultFor = second => (row, L, id) => (row === '7' ? { path: `/x/7-${id}.md`, fm: { status: id === 'r#7.1' ? 'gaps_found' : second }, match: true } : null);
  assert.equal(decision(PM.buildSnapshot('r', recheck, Object.assign(pctx(), { resultFor: resultFor('passed') }))).length, 0, 'the re-check finished after the gaps result');
  assert.equal(decision(PM.buildSnapshot('r', recheck, Object.assign(pctx(), { resultFor: resultFor('gaps_found') }))).length, 1, 'the re-check found gaps too');
  assert.equal(decision(PM.buildSnapshot('r', recheck, pctx())).length, 1, 'no result files known: the newest finish is the review, nothing came after it');
  // a later attempt of the review row itself (re-check) running: no decision line either
  s = PM.buildSnapshot('r', fixed.concat([E(7, 'launch', { attempt: 'r#7.2', row: '7', n: 2 }), E(8, 'start', { attempt: 'r#7.2', row: '7' })]), pctx());
  assert.equal(decision(s).length, 0); assert.equal(row7(s).state, 'running');
});

test('plan: optional reporting columns (short name, activity, depends on, estimate, reviewable, instructions) become item fields', () => {
  const plan = '| # | Subtask | Short name | Activity | Depends on | Estimate | Reviewable | Instructions | Agent | Model/Effort | Wave | Status |\n|---|---|---|---|---|---|---|---|---|---|---|---|\n' +
    '| 1 | Build the whole thing with a long subtask | Build | Writing code | | 10 | no | Do the build exactly | orch-builder | sonnet/high | 1 | pending |\n' +
    '| 2 | Review it | Review | Reading diff | 1, x, 1b | 1.5h | yes | | orch-reviewer | sonnet/high | 2 | pending |\n' +
    '| 3 | Quick | | | | 30s | | | orch-builder | sonnet/high | 2 | pending |\n';
  const p = PM.parsePlan(plan);
  assert.equal(p.rows.get('1').short_label, 'Build'); assert.equal(p.rows.get('2').depends_on, '1, x, 1b');
  const s = PM.buildSnapshot('r', [], ctx({ plan: { txt: plan, fm: {}, mtimeMs: Date.parse('2026-10-31T10:30:00Z') } }));
  const r1 = s.items.find(i => i.id === 'row:1'), r2 = s.items.find(i => i.id === 'row:2'), r3 = s.items.find(i => i.id === 'row:3');
  assert.equal(r1.short_label, 'Build'); assert.equal(r1.activity, 'Writing code'); assert.equal(r1.instructions, 'Do the build exactly');
  assert.equal(r1.estimate_ms, 600000); assert.equal(r1.reviewable, false); assert.equal(r1.depends_on, undefined);
  assert.deepEqual(r2.depends_on, ['row:1', 'row:1b']); assert.equal(r2.reviewable, true); assert.equal(r2.estimate_ms, 5400000);
  assert.equal(r2.instructions, 'Review it', 'instructions fall back to the subtask');
  assert.equal(r3.estimate_ms, 30000); assert.equal(r3.short_label, undefined); assert.equal(r3.reviewable, undefined);
  valid(s);
  const plain = PM.buildSnapshot('r', [], ctx({ plan: { txt: '| # | Subtask | Status |\n|---|---|---|\n| 1 | Only | pending |\n', fm: {}, mtimeMs: 1 } })).items.find(i => i.id === 'row:1');
  assert.equal(plain.short_label, undefined); assert.equal(plain.estimate_ms, undefined); assert.equal(plain.instructions, 'Only');
});

test('plan: a row line after a note line (not the first contiguous table) still reads against the latest header', () => {
  const txt = '| # | Subtask | Agent | Model/Effort | Wave | Status |\n|---|---|---|---|---|---|\n| 1 | A | orch-builder | sonnet/high | 1 | done |\n\nLaunches: cap 15\n' +
    '| 7 | Late row | orch-builder | opus/medium | 2 | pending |\n| x | junk | - | - | - | - |\n\n| Name | Value |\n|---|---|\n| 9 | not a row |\n';
  const p = PM.parsePlan(txt);
  assert.deepEqual(p.order, ['1', '7']);
  assert.deepEqual([p.rows.get('7').subtask, p.rows.get('7').model, p.rows.get('7').effort, p.rows.get('7').wave, p.rows.get('7').status], ['Late row', 'opus', 'medium', '2', 'pending']);
});

test('attention.json hook point is folded in and sanitised; terminal runs carry none', () => {
  const att = [{ id: 'q1', kind: 'question', text: 'Which preset?', options: ['a', 'b'] }, { id: 'bad id', text: 'x' }, { text: 'no id' }, 'junk'];
  const s = PM.buildSnapshot('r', [E(1, 'launch', { attempt: 'r#1.1', row: '1', n: 1 })], ctx({ attentionFile: att }));
  assert.deepEqual(s.attention.map(a => a.id), ['q1']); assert.equal(s.attention[0].answered_by.via, 'session'); valid(s);
});

// ---------------------------------------------------------------- CLI: publisher in the append path
test('append publishes: valid snapshot, seq = last event seq, launch output unchanged', () => {
  const sb = sandbox(); try {
    sb.runDir('r1');
    const l = sb.launch('r1', '1', 'S1');
    assert.equal(l.status, 0); assert.match(l.stdout, /^r1#1\.1\nnew\n/); assert.equal(l.stderr.trim().split('\n').length, 1, 'only the existing lease note on stderr');
    sb.ev('r1', 'start', { attempt: 'r1#1.1', row: '1', key: 'tu1', model: 'opus', effort: 'medium' });
    const s = sb.snap('r1');
    valid(s); assert.equal(s.seq, sb.lines('r1').length); assert.equal(s.writer.kind, 'oneshot'); assert.equal(s.owner.session_id, 'S1');
    assert.equal(s.items.find(i => i.id === 'row:1/a1').model, 'opus');
    assert.equal(fs.statSync(sb.snapFile('r1')).mode & 0o777, 0o600);
    const tmp = fs.readdirSync(path.dirname(sb.snapFile('r1'))).filter(f => f.endsWith('.tmp')); assert.deepEqual(tmp, []);
    // a duplicate event writes nothing and leaves the snapshot alone
    const before = fs.readFileSync(sb.snapFile('r1'), 'utf8');
    sb.ev('r1', 'start', { attempt: 'r1#1.1', row: '1', key: 'tu1' });
    assert.equal(fs.readFileSync(sb.snapFile('r1'), 'utf8'), before);
  } finally { sb.done(); }
});

test('a scratch ORCH_DIR without BOSS_PROGRESS_DIR publishes nothing (existing suites stay hermetic and byte-identical)', () => {
  const sb = sandbox(); try {
    delete sb.env.BOSS_PROGRESS_DIR; sb.runDir('r1');
    const before = fs.readdirSync(sb.env.ORCH_DIR).sort();
    const l = sb.launch('r1', '1', 'S1'); assert.equal(l.status, 0);
    assert.deepEqual(fs.readdirSync(sb.env.ORCH_DIR).sort(), before.concat(before.includes('leases') ? [] : ['leases']).sort().filter((x, i, a) => a.indexOf(x) === i), 'no boss/ dir appears under ORCH_DIR');
    assert.ok(!fs.existsSync(path.join(sb.env.ORCH_DIR, 'boss')));
    assert.equal(sb.man('progress', '--run', 'r1').status, 2);
  } finally { sb.done(); }
});

test('lock: 12 concurrent appenders leave one consistent snapshot (seq = line count) and no leftovers', async () => {
  const sb = sandbox(); try {
    sb.runDir('rc'); sb.launch('rc', '1', 'S1');
    await Promise.all(Array.from({ length: 12 }, (_, i) => new Promise(res => {
      const p = spawn(process.execPath, [MAN, 'event', '--run', 'rc', '--type', 'outcome', '--json', JSON.stringify({ attempt: 'rc#1.1', row: '1', key: `o${i}`, first_pass: true })], { env: sb.env, stdio: 'ignore' });
      p.on('exit', res);
    })));
    const n = sb.lines('rc').length, s = sb.snap('rc');
    assert.equal(n, 2 + 12); assert.equal(s.seq, n); valid(s);
    assert.ok(!fs.existsSync(path.join(sb.env.ORCH_RUNS_DIR, 'rc', 'manifest.jsonl.lock')), 'lock released');
    assert.deepEqual(fs.readdirSync(path.dirname(sb.snapFile('rc'))).filter(f => f.endsWith('.tmp')), []);
  } finally { sb.done(); }
});

test('lock: a stale lock (dead writer) is recovered and the append still publishes', () => {
  const sb = sandbox(); try {
    const d = sb.runDir('rs'); sb.launch('rs', '1', 'S1');
    const lock = path.join(d, 'manifest.jsonl.lock'); fs.mkdirSync(lock);
    const old = new Date(Date.now() - 60000); fs.utimesSync(lock, old, old);
    const r = sb.ev('rs', 'outcome', { attempt: 'rs#1.1', row: '1', key: 'o1', first_pass: false });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(sb.snap('rs').seq, sb.lines('rs').length);
  } finally { sb.done(); }
});

test('rebuild: a deleted or stale snapshot is reproduced from the manifest; progress without --rebuild reports "behind"', () => {
  const sb = sandbox(); try {
    sb.runDir('rb'); sb.launch('rb', '1', 'S1');
    sb.ev('rb', 'start', { attempt: 'rb#1.1', row: '1', key: 't1' });
    sb.ev('rb', 'stop', { attempt: 'rb#1.1', row: '1', key: 's1', final: true, phase: 'finished', status: 'completed' });
    const live = strip(sb.snap('rb'));
    fs.rmSync(sb.snapFile('rb'));
    assert.match(sb.man('progress', '--run', 'rb').stdout, /^progress: none for rb/);
    const r = sb.man('progress', '--run', 'rb', '--rebuild'); assert.equal(r.status, 0, r.stderr); assert.match(r.stdout, /state running\s+seq 4\/4/);
    assert.deepEqual(strip(sb.snap('rb')), live, 'rebuild equals the incrementally built snapshot');
    // stale: write an old seq, then ask without --rebuild
    const s = sb.snap('rb'); s.seq = 2; fs.writeFileSync(sb.snapFile('rb'), JSON.stringify(s));
    assert.match(sb.man('progress', '--run', 'rb').stdout, /seq 2\/4\s+\(behind the manifest: --rebuild\)/);
    // a run folder with no manifest at all still rebuilds (plan only)
    const d = sb.runDir('np'); fs.writeFileSync(path.join(d, 'PLAN.md'), '| # | Subtask | Status |\n|---|---|---|\n| 1 | Only row | pending |\n');
    const r2 = sb.man('progress', '--run', 'np', '--rebuild'); assert.equal(r2.status, 0, r2.stderr);
    const np = sb.snap('np'); valid(np); assert.equal(np.seq, 0); assert.equal(np.items[0].state, 'queued');
    assert.equal(sb.man('progress').status, 2); assert.equal(sb.man('progress', '--run', 'nope').status, 2);
  } finally { sb.done(); }
});

test('terminal: a lease release on a finished plan ends the run; on an unfinished plan it pauses; a new acquire resumes', () => {
  const sb = sandbox(); try {
    const d = sb.runDir('rt'); sb.launch('rt', '1', 'S1');
    fs.writeFileSync(path.join(d, 'PLAN.md'), '| # | Subtask | Agent | Model/Effort | Status |\n|---|---|---|---|---|\n| 1 | Row one | orch-builder | sonnet/high | pending |\n| 2 | Row two | orch-builder | sonnet/high | pending |\n');
    sb.ev('rt', 'start', { attempt: 'rt#1.1', row: '1', key: 't1' });
    sb.ev('rt', 'stop', { attempt: 'rt#1.1', row: '1', key: 's1', final: true, phase: 'finished', status: 'completed' });
    assert.equal(sb.snap('rt').state, 'running');
    // release with row 2 still pending -> paused, not terminal
    assert.equal(sb.man('lease', 'release', '--sid', 'S1', '--run', 'rt').status, 0);
    let s = sb.snap('rt'); assert.equal(s.state, 'waiting_input'); assert.match(s.phase, /paused/); assert.equal(s.paused, true); assert.equal(s.ended_at, null); assert.equal(s.owner.session_id, 'S1'); valid(s);
    // resume, finish the plan, release -> done
    assert.equal(sb.man('lease', 'acquire', '--run', 'rt', '--sid', 'S1').status, 0);
    assert.equal(sb.snap('rt').state, 'running'); assert.equal('paused' in sb.snap('rt'), false);
    fs.writeFileSync(path.join(d, 'PLAN.md'), '| # | Subtask | Agent | Model/Effort | Status |\n|---|---|---|---|---|\n| 1 | Row one | orch-builder | sonnet/high | done |\n| 2 | Row two | orch-builder | sonnet/high | done |\n');
    assert.equal(sb.man('lease', 'release', '--sid', 'S1', '--run', 'rt').status, 0);
    s = sb.snap('rt'); assert.equal(s.state, 'done'); assert.ok(s.ended_at); assert.equal(s.phase, 'finished'); assert.equal(s.ext.orchestrator.lease.holder, null); valid(s);
    assert.equal(ph().liveness(s).status, 'ended');
    // rebuild gives the same answer (holder and release come from lease events)
    const before = strip(s); sb.man('progress', '--run', 'rt', '--rebuild'); assert.deepEqual(strip(sb.snap('rt')), before);
  } finally { sb.done(); }
});

test('terminal: a plan-less run ends done on release, failed when an attempt failed', () => {
  const sb = sandbox(); try {
    sb.runDir('ra'); sb.launch('ra', '1', 'S1'); sb.ev('ra', 'start', { attempt: 'ra#1.1', row: '1', key: 't1' });
    sb.ev('ra', 'stop', { attempt: 'ra#1.1', row: '1', key: 's1', final: true, phase: 'finished', status: 'completed' });
    sb.man('lease', 'release', '--sid', 'S1', '--run', 'ra'); assert.equal(sb.snap('ra').state, 'done');
    sb.runDir('rf'); sb.launch('rf', '1', 'S1'); sb.ev('rf', 'start', { attempt: 'rf#1.1', row: '1', key: 't1' });
    sb.ev('rf', 'stop', { attempt: 'rf#1.1', row: '1', key: 's1', final: true, phase: 'failed', status: 'failed' });
    sb.man('lease', 'release', '--sid', 'S1', '--run', 'rf'); assert.equal(sb.snap('rf').state, 'failed');
  } finally { sb.done(); }
});

test('owner: the lease holder session maps to its pid through sessions/<pid>.json; a stale answer is not reused', { skip: NO_PROC }, () => {
  const sb = sandbox(); try {
    sb.runDir('ro');
    const sess = { pid: process.pid, sessionId: 'S-OWN', startedAt: Math.round(Date.now() - process.uptime() * 1000), status: 'busy', kind: 'interactive' };
    fs.writeFileSync(path.join(sb.env.ORCH_SESSIONS_DIR, `${process.pid}.json`), JSON.stringify(sess));
    fs.writeFileSync(path.join(sb.env.ORCH_SESSIONS_DIR, `${process.pid}.key`), 'secret', { mode: 0o000 }); // must never be opened
    sb.launch('ro', '1', 'S-OWN');
    let s = sb.snap('ro');
    assert.equal(s.owner.pid, process.pid); assert.equal(s.owner.start_ticks, ph().procStartTicks(process.pid));
    assert.equal(ph().liveness(s, { probe: ph().defaultProbe(sb.env.ORCH_SESSIONS_DIR) }).status, 'running');
    // the session goes idle -> waiting_input; goes away -> dead
    sess.status = 'idle'; fs.writeFileSync(path.join(sb.env.ORCH_SESSIONS_DIR, `${process.pid}.json`), JSON.stringify(sess));
    assert.equal(ph().liveness(s, { probe: ph().defaultProbe(sb.env.ORCH_SESSIONS_DIR) }).status, 'waiting_input');
    fs.rmSync(path.join(sb.env.ORCH_SESSIONS_DIR, `${process.pid}.json`));
    assert.equal(ph().liveness(s, { probe: ph().defaultProbe(sb.env.ORCH_SESSIONS_DIR) }).status, 'dead');
    // a holder with no session file keeps pid null
    sb.runDir('ro2'); sb.launch('ro2', '1', 'S-NONE'); s = sb.snap('ro2'); assert.equal(s.owner.pid, null); assert.equal(s.owner.session_id, 'S-NONE');
  } finally { sb.done(); }
});

test('a publish failure is swallowed: same output, exit 0, one errors.log line; off switches work', () => {
  const sb = sandbox(); try {
    sb.runDir('rx');
    fs.writeFileSync(path.join(sb.T, 'blocker'), 'file, not a dir');
    const bad = Object.assign({}, sb.env, { BOSS_PROGRESS_DIR: path.join(sb.T, 'blocker', 'sub') });
    const run = (env, ...a) => spawnSync(process.execPath, [MAN, ...a], { env, encoding: 'utf8' });
    const args = ['launch', '--run', 'rx', '--row', '1', '--role', 'implementation', '--agent', 'orch-builder', '--model', 'sonnet', '--effort', 'high', '--mode', 'subagent', '--repo', 'x', '--area', 'y', '--sid', 'S1'];
    const r = run(bad, ...args); assert.equal(r.status, 0, r.stderr); assert.match(r.stdout, /^rx#1\.1\nnew\n/);
    assert.equal(sb.lines('rx').length, 2, 'the manifest events were still written (acquire + launch)');
    const log = fs.readFileSync(path.join(sb.env.ORCH_DIR, 'errors.log'), 'utf8').split('\n').filter(l => /progress: snapshot for rx not written/.test(l));
    assert.equal(log.length, 1, 'logged once per process even though two events failed');
    // switches
    sb.runDir('ry'); const off = Object.assign({}, sb.env, { ORCH_PROGRESS: 'off' });
    assert.equal(run(off, ...args.map(x => (x === 'rx' ? 'ry' : x))).status, 0);
    assert.ok(!fs.existsSync(sb.snapFile('ry')));
    assert.equal(run(off, 'progress', '--run', 'ry').status, 2);
    // an unreadable PLAN.md folder entry / garbage attention.json do not break the publish
    const d = sb.runDir('rz'); fs.writeFileSync(path.join(d, 'attention.json'), '{not json'); fs.mkdirSync(path.join(d, 'PLAN.md'));
    assert.equal(sb.launch('rz', '1', 'S1').status, 0); valid(sb.snap('rz'));
  } finally { sb.done(); }
});

test('prune runs on a live acquire only, never on replay, and only inside the registry', () => {
  const sb = sandbox(); try {
    const old = path.join(sb.env.BOSS_PROGRESS_DIR, 'runs', 'council', 'ancient'); fs.mkdirSync(old, { recursive: true });
    fs.writeFileSync(path.join(old, 'run.json'), '{}'); const t = new Date(Date.now() - 9 * 86400000); fs.utimesSync(path.join(old, 'run.json'), t, t);
    const d = sb.runDir('rp'); sb.ev('rp', 'outcome', { attempt: 'rp#1.1', row: '1', key: 'o1' });
    assert.ok(fs.existsSync(old), 'an ordinary append does not prune');
    sb.man('progress', '--run', 'rp', '--rebuild'); assert.ok(fs.existsSync(old), 'rebuild does not prune');
    assert.equal(sb.man('lease', 'acquire', '--run', 'rp', '--sid', 'S1').status, 0);
    assert.ok(!fs.existsSync(old), 'lease acquire prunes registry entries older than 7 days');
    assert.ok(fs.existsSync(d), 'the orchestrator run folder is never touched');
  } finally { sb.done(); }
});

// review gap 1: a turn-limit stop (partial result) is waiting_input (unfinished, visible), not failed; it never overrides a real final stop that is not older, whatever the ingest order
test('fold: a turn-limit stop reads waiting_input (valid, not terminal; detail + meta.stop_status), and a real final stop that is not older always wins', () => {
  const launch = E(1, 'launch', { attempt: 'r#1.1', row: '1', n: 1, model: 'sonnet', effort: 'high', agent: 'orch-builder' }), start = E(2, 'start', { attempt: 'r#1.1', row: '1', tool_use_id: 't1' });
  const stop = (seq, phase, status, sec) => E(seq, 'stop', { attempt: 'r#1.1', row: '1', task_id: 'k', phase, status, final: true, ts: `2026-10-31T10:00:${String(sec).padStart(2, '0')}.000Z` });
  const states = evs => { const s = PM.buildSnapshot('r', evs, ctx()); valid(s); return s.items.filter(i => i.id.startsWith('row:1')).map(i => `${i.id}:${i.state}`).join(' '); };
  const tl = (seq, sec) => stop(seq, 'stopped-turn-limit', 'turn-limit', sec), fin = (seq, sec) => stop(seq, 'finished', 'completed', sec);
  assert.equal(states([launch, start, tl(3, 30)]), 'row:1:waiting_input row:1/a1:waiting_input');
  assert.equal(PM.buildSnapshot('r', [launch, start, tl(3, 30)], ctx()).items.find(i => i.id === 'row:1/a1').meta.stop_status, 'turn-limit');
  const only = PM.buildSnapshot('r', [launch, start, tl(3, 30)], ctx());
  assert.match(only.items.find(i => i.id === 'row:1').detail, /turn limit/);
  assert.match(only.items.find(i => i.id === 'row:1/a1').detail, /turn limit/);
  assert.equal(only.state, 'running', 'the run is not terminal and not failed');
  assert.equal(only.counts[0].failed, 0);
  assert.equal(PM.buildSnapshot('r', [launch, start, fin(3, 40), tl(4, 30)], ctx()).items.find(i => i.id === 'row:1').detail, undefined);
  assert.equal(states([launch, start, fin(3, 40), tl(4, 30)]), 'row:1:done row:1/a1:done', 'turn-limit ingested late with an older ts');
  assert.equal(states([launch, start, tl(3, 30), fin(4, 40)]), 'row:1:done row:1/a1:done', 'turn-limit, then a real continuation finish');
  assert.equal(states([launch, start, fin(3, 40), tl(4, 40)]), 'row:1:done row:1/a1:done', 'an equal ts is not later');
  assert.equal(states([launch, start, fin(3, 40), tl(4, 50)]), 'row:1:waiting_input row:1/a1:waiting_input', 'a really later turn-limit stop speaks');
  assert.equal(states([launch, start, stop(3, 'failed', 'failed', 40), tl(4, 30)]), 'row:1:failed row:1/a1:failed', 'a failed stop wins over an older turn-limit one');
});
