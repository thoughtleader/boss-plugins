'use strict';
// /boss:status compact adapter (skills/status/compact.js): falls back to the task report without the meter files,
// and with them reads the snapshot, writes the report artifact and advances report-state. Scratch dirs only; the
// meter files are small stubs that record what the adapter passed in.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs'), os = require('os'), path = require('path');
const { spawnSync } = require('child_process');

const PLUGIN = process.env.ORCH_TEST_ROOT || path.resolve(__dirname, '..');
const COMPACT = path.join(PLUGIN, 'skills', 'status', 'compact.js');
const PROGRESS = path.join(PLUGIN, 'skills', 'status', 'progress.js');

function sandbox() {
  const T = fs.mkdtempSync(path.join(os.tmpdir(), 'status-compact-'));
  const env = Object.assign({}, process.env, {
    ORCH_DIR: path.join(T, 'orch'), ORCH_RUNS_DIR: path.join(T, 'orch', 'runs'), BOSS_ORCH_RUNS: path.join(T, 'orch', 'runs'),
    BOSS_PROGRESS_DIR: path.join(T, 'boss'), BOSS_METER_DIR: path.join(T, 'meter'), BOSS_DATA: path.join(T, 'data'), COLUMNS: '60',
  });
  delete env.WFP_SID;
  const runDir = path.join(env.ORCH_RUNS_DIR, 'r1'); fs.mkdirSync(runDir, { recursive: true }); fs.mkdirSync(env.BOSS_METER_DIR, { recursive: true });
  fs.writeFileSync(path.join(runDir, 'PLAN.md'),
    '| # | Subtask | Short name | Activity | Agent | Model/Effort | Wave | Status |\n|---|---|---|---|---|---|---|---|\n| 1 | Build it | Build | Writing | orch-builder | sonnet/high | 1 | pending |\n');
  const run = (script, ...args) => spawnSync(process.execPath, [script, ...args], { env, encoding: 'utf8' });
  const meterStubs = () => {
    const d = env.BOSS_METER_DIR;
    fs.writeFileSync(path.join(d, 'meter.mjs'), "export function meterFromSnapshot(snap, o) { const it = snap.items.find(i => i.id === 'row:1'); return { attention: [], rows: snap.items.length, label: it && it.short_label, activity: it && it.activity, ledger: o.ledger.length }; }\n");
    fs.writeFileSync(path.join(d, 'reporting.mjs'), "export const reportingSnapshot = s => s;\nexport const reportingState = () => ({ delayHistory: [], delayAdded: false, marker: 'saved' });\n");
    fs.writeFileSync(path.join(d, 'reports.mjs'), "import fs from 'node:fs/promises'; import path from 'node:path';\nexport const reportText = m => 'TEXT ' + m.label;\nexport async function writeReportArtifact(m, { root, run, columns }) { await fs.mkdir(path.join(root, 'reports'), { recursive: true }); await fs.writeFile(path.join(root, 'reports', run + '.md'), 'x'); return { output: 'TEXT ' + m.label + ' ' + m.activity + ' cols=' + columns + '\\n\\n[DETAILS](file:///x)' }; }\n");
  };
  return { T, env, run, meterStubs, done: () => fs.rmSync(T, { recursive: true, force: true }) };
}

test('compact: without the meter files it prints the --details task report', () => {
  const sb = sandbox();
  try {
    const out = sb.run(COMPACT, 'r1'), det = sb.run(PROGRESS, 'r1', '--details');
    assert.equal(out.status, 0, out.stderr);
    assert.equal(out.stdout, det.stdout);
    assert.match(out.stdout, /Build it/);
  } finally { sb.done(); }
});

test('compact: progress.js delegates to compact.js unless --details; usage errors exit 2', () => {
  const sb = sandbox();
  try {
    sb.meterStubs();
    const out = sb.run(PROGRESS, 'r1');
    assert.equal(out.status, 0, out.stderr);
    assert.match(out.stdout, /DETAILS/);
    assert.notEqual(out.stdout, sb.run(PROGRESS, 'r1', '--details').stdout);
    assert.equal(sb.run(COMPACT).status, 2);
    assert.equal(sb.run(COMPACT, 'r1', '--bogus').status, 2);
    assert.equal(sb.run(COMPACT, '../x').status, 2);
  } finally { sb.done(); }
});

test('compact: with the meter files it folds the plan in memory (reporting columns reach the meter), saves the report and advances report-state', () => {
  const sb = sandbox();
  try {
    sb.meterStubs();
    const out = sb.run(COMPACT, 'r1');
    assert.equal(out.status, 0, out.stderr);
    assert.match(out.stdout, /TEXT Build Writing cols=60/);
    assert.match(out.stdout, /\[DETAILS\]\(file:\/\/\/x\)/);
    assert.ok(fs.existsSync(path.join(sb.env.BOSS_PROGRESS_DIR, 'reports', 'r1.md')), 'report artifact under the progress root');
    const st = JSON.parse(fs.readFileSync(path.join(sb.env.BOSS_PROGRESS_DIR, 'report-state', 'orchestrator', 'r1.json'), 'utf8'));
    assert.equal(st.marker, 'saved');
    assert.equal(fs.existsSync(path.join(sb.env.BOSS_PROGRESS_DIR, 'runs', 'orchestrator', 'r1', 'progress.json')), false, 'the snapshot cache is never written by the reader');
    const json = JSON.parse(sb.run(COMPACT, 'r1', '--json').stdout);
    assert.equal(json.label, 'Build');
  } finally { sb.done(); }
});

test('compact and progress.js accept a run folder path under the runs dir as well as the name; other paths exit 2 and name the argument', () => {
  const sb = sandbox();
  try {
    sb.meterStubs();
    const runPath = path.join(sb.env.ORCH_RUNS_DIR, 'r1');
    const byName = sb.run(COMPACT, 'r1'), byPath = sb.run(COMPACT, runPath);
    assert.equal(byPath.status, 0, byPath.stderr);
    assert.equal(byPath.stdout, byName.stdout);
    assert.equal(sb.run(COMPACT, runPath + '/').status, 0, 'trailing slash');
    assert.equal(sb.run(COMPACT, path.join(runPath, 'PLAN.md')).status, 0, 'a file inside the run folder');
    const det = sb.run(PROGRESS, runPath, '--details');
    assert.equal(det.status, 0, det.stderr);
    assert.equal(det.stdout, sb.run(PROGRESS, 'r1', '--details').stdout);
    const bad = sb.run(PROGRESS, path.join(sb.T, 'elsewhere', 'r1'), '--details');
    assert.equal(bad.status, 2);
    assert.match(bad.stderr, /bad run '.*elsewhere.*r1'/);
    assert.equal(sb.run(COMPACT, '../x').status, 2);
  } finally { sb.done(); }
});
