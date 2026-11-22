import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as P from '../../lib/progress.mjs';
const NO_PROC = fs.existsSync('/proc/self/stat') ? false : 'no /proc here (macOS): this reads /proc, which is Linux only (council README, Platform)';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const METER = path.dirname(HERE);
const PLUGIN = path.dirname(METER);
const SCHEMA = JSON.parse(fs.readFileSync(path.join(PLUGIN, 'lib', 'progress.schema.json'), 'utf8'));
// The Python writer (progress.py) ships with council; BOSS_PROGRESS_LIB overrides its folder.
const PY_LIB = process.env.BOSS_PROGRESS_LIB ? path.dirname(process.env.BOSS_PROGRESS_LIB) : path.join(PLUGIN, 'council');
const CASES = JSON.parse(fs.readFileSync(path.join(HERE, 'liveness-cases.json'), 'utf8'));
const ex = (n) => JSON.parse(fs.readFileSync(path.join(METER, 'examples', n), 'utf8'));
const mode = (p) => fs.statSync(p).mode & 0o777;
function tmpRoot() {
  const t = fs.mkdtempSync(path.join(os.tmpdir(), 'boss-'));
  process.env.BOSS_PROGRESS_DIR = path.join(t, 'boss');
  return t;
}
const snap = (o = {}) => ({ tool: 'orchestrator', run_id: 'r1', state: 'running', seq: 1, items: [], writer: P.writerIdentity('oneshot'), ...o });

test('atomic write: modes, no temp left, readable', () => {
  tmpRoot();
  const p = P.writeSnapshot(snap());
  const d = path.dirname(p);
  assert.equal(mode(p), 0o600);
  for (const x of [P.root(), path.join(P.root(), 'runs'), path.dirname(d), d]) assert.equal(mode(x), 0o700, x);
  assert.deepEqual(fs.readdirSync(d).sort(), ['progress.json', 'run.json']);
  assert.equal(P.readSnapshot(d).schema, P.SCHEMA);
  assert.deepEqual(P.validate(P.readSnapshot(d), SCHEMA), []);
});

test('run.json is write-once; hygiene + caps', () => {
  tmpRoot();
  P.writeSnapshot(snap({ title: 'first' }));
  P.writeSnapshot(snap({ title: 'a\x1b[31mred\x07' + 't'.repeat(300), items: [{ id: 'i', kind: 'row', label: 'l', state: 'failed', error: 'e'.repeat(5000) }] }));
  const d = P.runDir('orchestrator', 'r1');
  assert.equal(JSON.parse(fs.readFileSync(path.join(d, 'run.json'), 'utf8')).title, 'first');
  const got = P.readSnapshot(d);
  assert.ok(got.title.startsWith('ared'));
  assert.equal(got.title.length, 200);
  assert.equal(got.items[0].error.length, 2000);
});

test('unsafe ids rejected', () => {
  for (const r of ['../x', 'a/b', '', '..']) assert.throws(() => P.runDir('council', r));
  assert.throws(() => P.claimNotification('council', 'r1', '../x'));
});

test('prune removes old runs only', () => {
  tmpRoot();
  P.writeSnapshot(snap()); P.writeSnapshot(snap({ run_id: 'r2' }));
  fs.utimesSync(path.join(P.runDir('orchestrator', 'r1'), 'progress.json'), 0, 0);
  assert.deepEqual(P.prune().map((d) => path.basename(d)), ['r1']);
  assert.deepEqual(P.listRuns().map((d) => path.basename(d)), ['r2']);
});

test('examples validate; validator rejects bad docs; unknown fields allowed', () => {
  for (const n of ['council.json', 'orchestrator.json']) assert.deepEqual(P.validate(ex(n), SCHEMA), [], n);
  const bad = ex('orchestrator.json');
  bad.state = 'exploded'; bad.items[0].label = 'x'.repeat(201); delete bad.writer;
  assert.equal(P.validate(bad, SCHEMA).length, 3);
  const extra = ex('council.json'); extra.future = 1; extra.items[0].new_thing = 2;
  assert.deepEqual(P.validate(extra, SCHEMA), []);
});

test('liveness: shared cases (same file as python)', () => {
  const now = P.parseTs(CASES.now);
  const d = CASES.probe;
  const probe = { bootId: () => d.boot_id, startTicks: (pid) => d.procs[String(pid)] ?? null,
    session: (o) => (d.sessions[o.session_id] ? { pid: 1, status: d.sessions[o.session_id] } : null) };
  for (const c of CASES.cases) {
    const s = { ...CASES.base, ...c.snap };
    const got = P.liveness(s, { now, probe, resumedAt: P.parseTs(c.resumed_at) });
    for (const [k, v] of Object.entries(c.expect)) assert.equal(got[k], v, `${c.name}: ${JSON.stringify(got)}`);
  }
});

test('liveness: real probe on this process', { skip: NO_PROC }, () => {
  const s = { ...CASES.base, writer: P.writerIdentity(), heartbeat_at: P.iso(), updated_at: P.iso() };
  assert.equal(P.liveness(s).status, 'running');
});

test('session adapter: by id / pid, skips .key, rejects dead + reused', { skip: NO_PROC }, () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sess-'));
  const me = process.pid;
  const btime = Number(fs.readFileSync('/proc/stat', 'utf8').match(/^btime (\d+)$/m)[1]);
  const startedMs = Math.round((btime + P.procStartTicks(me) / 100) * 1000);
  const w = (n, o) => fs.writeFileSync(path.join(dir, n), JSON.stringify(o));
  w(`${me}.json`, { pid: me, sessionId: 'sid-me', startedAt: startedMs, status: 'idle' });
  w('999999999.json', { pid: 999999999, sessionId: 'sid-dead', startedAt: startedMs, status: 'busy' });
  w('1.json', { pid: me, sessionId: 'sid-old', startedAt: startedMs - 86400000, status: 'busy' });
  const key = path.join(dir, `${me}.0123abcd.key`); fs.writeFileSync(key, 'secret'); fs.chmodSync(key, 0);
  assert.deepEqual(P.sessionState({ session_id: 'sid-me' }, dir), { pid: me, status: 'idle' });
  assert.equal(P.sessionState({ pid: me }, dir).status, 'idle');
  assert.equal(P.sessionState({ session_id: 'sid-dead' }, dir), null);
  assert.equal(P.sessionState({ session_id: 'sid-old' }, dir), null);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('notification claim dedupes', () => {
  tmpRoot();
  assert.equal(P.claimNotification('council', 'r1', 'done'), true);
  assert.equal(P.claimNotification('council', 'r1', 'done'), false);
  assert.equal(P.claimNotification('council', 'r1', 'failed'), true);
  assert.equal(mode(path.join(P.runDir('council', 'r1'), 'claims')), 0o700);
});

test('cross-language: python-written snapshot reads + claims in node', { skip: !fs.existsSync(path.join(PY_LIB, 'progress.py')) && 'council/progress.py is not in this tree' }, async () => {
  const { execFileSync } = await import('node:child_process');
  tmpRoot();
  execFileSync('python3', ['-B', '-c', `import sys; sys.path.insert(0, ${JSON.stringify(PY_LIB)}); import progress as P
P.write_snapshot({"tool":"council","run_id":"x1","state":"done","seq":3,"items":[],"writer":P.writer_identity()})
print(P.claim_notification("council","x1","done"))`], { env: process.env });
  const s = P.readSnapshot(P.runDir('council', 'x1'));
  assert.equal(s.seq, 3);
  assert.equal(P.liveness(s).status, 'ended');
  assert.equal(P.claimNotification('council', 'x1', 'done'), false);
});
