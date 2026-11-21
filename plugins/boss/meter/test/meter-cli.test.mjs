import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { readInlineReport } from '../mod/src/inline.mjs';

const cli = fileURLToPath(new URL('../../skills/status/progress.js', import.meta.url));
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'boss-meter-cli-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const registry = path.join(root, 'registry'), runs = path.join(root, 'runs'), id = 'fixture-meter';
  const snapDir = path.join(registry, 'runs/orchestrator', id), runDir = path.join(runs, id);
  fs.mkdirSync(snapDir, { recursive: true }); fs.mkdirSync(runDir, { recursive: true });
  const stamp = n => new Date(Date.now() - n * 1000).toISOString();
  const snap = {
    schema: 'boss.progress/1', tool: 'orchestrator', run_id: id, state: 'running', phase: 'wave B',
    started_at: stamp(380), updated_at: stamp(1), seq: 7,
    items: [
      { id: 'wave:A', kind: 'wave', label: 'wave A', state: 'done' },
      { id: 'row:1', kind: 'row', parent: 'wave:A', label: 'Fixture one', state: 'done' },
      { id: 'a1', kind: 'attempt', parent: 'row:1', state: 'done', started_at: stamp(380), ended_at: stamp(128), meta: { attempt_id: id + '#1.1', tokens: 28000 } },
      { id: 'wave:B', kind: 'wave', label: 'wave B', state: 'running' },
      { id: 'row:2', kind: 'row', parent: 'wave:B', label: 'Fixture two', state: 'running' },
      { id: 'a2', kind: 'attempt', parent: 'row:2', state: 'running', started_at: stamp(128), meta: { attempt_id: id + '#2.1' } },
      { id: 'row:3', kind: 'gate', parent: 'wave:B', label: 'Fixture review', state: 'queued' },
    ], attention: [],
  };
  const file = path.join(snapDir, 'progress.json'); fs.writeFileSync(file, JSON.stringify(snap));
  return { id, root, registry, file, runDir, env: { ...process.env, BOSS_PROGRESS_DIR: registry, BOSS_ORCH_RUNS: runs, COLUMNS: '72' } };
}

test('chat CLI preserves run state and writes readable linked task details without opaque markers', async t => {
  const f = fixture(t), before = fs.readFileSync(f.file, 'utf8');
  const out = execFileSync(process.execPath, [cli, f.id], { env: f.env, encoding: 'utf8' });
  assert.match(out, /```/); assert.match(out, /🆆/); assert.match(out, /🆃/); assert.match(out, /28K/);
  assert.match(out, /\[DETAILS\]\(file:\/\//);
  assert.doesNotMatch(out, /boss-meter:v|<!--|Partial usage/);
  assert.doesNotMatch(out, /Next up|SHA \/ result/);
  const bars = /```text\n([\s\S]*?)\n```/.exec(out)[1].split('\n').filter(line => /^[🆆🆃]/.test(line));
  assert.equal(bars.length, 2);
  assert.doesNotMatch(bars.join('\n'), /\d+\/\d+|%|tok|\d+[hms]|│|\*/);
  assert.match(bars.join('\n'), /\d{2}:\d{2}/);
  assert.equal(bars[0].search(/\d{2}:\d{2}/), bars[1].search(/\d{2}:\d{2}/));
  assert.equal(fs.readFileSync(f.file, 'utf8'), before);
  assert.deepEqual(fs.readdirSync(f.runDir), []);
  assert.match(out, /➜    Fixture two\n⮡    Fixture review/);
  assert.doesNotMatch(out, /☑/, 'first publication does not replay pre-existing completions');
  assert.match(out, /^```text\n🆆[^\n]+\n🆃[^\n]+\n➜/);
  const prefix = 'The merge is done. Final review is now running.\n\n', suffix = '\n\nI will report the review result here.';
  const report = await readInlineReport({ read: file => fs.promises.readFile(file, 'utf8') }, f.registry, prefix + out.trimEnd() + suffix);
  assert.ok(report, 'the actual CLI output must enhance even when surrounded by ordinary prose');
  assert.equal(report.prefix, prefix); assert.equal(report.suffix, suffix);
  assert.equal(report.meter.tasks.now[0].label, 'Fixture two');
  assert.equal(report.meter.tasks.next[0].label, 'Fixture review');
  const markdown = fs.readFileSync(report.reportPath, 'utf8');
  assert.match(markdown, /In progress \/ needs attention/); assert.match(markdown, /Upcoming/);
  assert.match(markdown, /Fixture two/); assert.match(markdown, /Fixture review/);
  assert.equal(fs.readdirSync(path.join(f.registry, 'reports')).length, 2);
});

test('both chat clocks use hour format when only the total has reached one hour', t => {
  const f = fixture(t), snap = JSON.parse(fs.readFileSync(f.file));
  snap.items.find(i => i.id === 'a1').started_at = new Date(Date.now() - 3600_000).toISOString();
  fs.writeFileSync(f.file, JSON.stringify(snap));
  const out = execFileSync(process.execPath, [cli, f.id], { env: f.env, encoding: 'utf8' });
  const rows = /```text\n([\s\S]*?)\n```/.exec(out)[1].split('\n');
  assert.match(rows[0], /01:00:\d{2}/); assert.match(rows[1], /00:02:\d{2}/);
  assert.equal(rows[0].search(/\d{2}:\d{2}:\d{2}/), rows[1].search(/\d{2}:\d{2}:\d{2}/));
  assert.doesNotMatch(rows.join('\n'), /│|\*/);
});

test('chat omits the completion row when finished work has only failed', t => {
  const f = fixture(t), snap = JSON.parse(fs.readFileSync(f.file));
  snap.items.find(item => item.id === 'row:1').state = 'failed';
  snap.items.find(item => item.id === 'a1').state = 'failed';
  fs.writeFileSync(f.file, JSON.stringify(snap));
  const before = fs.readFileSync(f.file, 'utf8');
  const out = execFileSync(process.execPath, [cli, f.id], { env: f.env, encoding: 'utf8' });
  assert.match(out, /^```text\n🆆[^\n]+\n🆃[^\n]+\n➜/);
  assert.doesNotMatch(out, /☑/);
  assert.equal(fs.readFileSync(f.file, 'utf8'), before);
  assert.deepEqual(fs.readdirSync(f.runDir), []);
});

test('a report too large for inline rendering keeps every attention item in plain text', t => {
  const f = fixture(t), snap = JSON.parse(fs.readFileSync(f.file));
  snap.attention = Array.from({ length: 10 }, (_, i) => ({ id: `issue-${i}`, kind: 'error', text: `Fixture issue ${i}` }));
  fs.writeFileSync(f.file, JSON.stringify(snap));
  const out = execFileSync(process.execPath, [cli, f.id], { env: f.env, encoding: 'utf8' });
  assert.match(out, /Fixture issue 9/); assert.doesNotMatch(out, /<!-- boss-meter:v\d:/);
});

test('unwritable report storage retains task summaries and a useful detail command without mutating the run', t => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.registry, 'reports'), 'not a directory');
  const out = execFileSync(process.execPath, [cli, f.id], { env: f.env, encoding: 'utf8' });
  assert.match(out, /➜\s+Fixture two/); assert.match(out, /⮡\s+Fixture review/);
  assert.match(out, /Details: \/boss:status fixture-meter --details/);
  assert.doesNotMatch(out, /boss-meter:v|<!--|\[DETAILS\]/);
  assert.deepEqual(fs.readdirSync(f.runDir), []);
});

test('JSON mode exposes canonical partial usage, preserving missing active tokens', t => {
  const f = fixture(t);
  const meter = JSON.parse(execFileSync(process.execPath, [cli, f.id, '--json'], { env: f.env, encoding: 'utf8' }));
  assert.equal(meter.total.tokens, 28000); assert.equal(meter.total.partialTokens, true);
  assert.equal(meter.current.tokens, null); assert.equal(meter.current.total, 2);
  assert.equal(meter.waves.done, 1); assert.equal(meter.waves.total, 2);
});

test('unknown runs and unsupported snapshots fail without making records', t => {
  const f = fixture(t);
  assert.equal(spawnSync(process.execPath, [cli, 'missing'], { env: f.env }).status, 2);
  const snap = JSON.parse(fs.readFileSync(f.file)); snap.schema = 'boss.progress/99'; fs.writeFileSync(f.file, JSON.stringify(snap));
  const bad = spawnSync(process.execPath, [cli, f.id], { env: f.env, encoding: 'utf8' });
  assert.equal(bad.status, 2); assert.match(bad.stderr, /Unsupported/);
  assert.deepEqual(fs.readdirSync(f.runDir), []);
});

test('legacy plan-only run folds in memory and accepts alphabetic waves', t => {
  const f = fixture(t); fs.unlinkSync(f.file);
  fs.writeFileSync(path.join(f.runDir, 'PLAN.md'), '| # | Subtask | Wave | Status |\n|---|---|---|---|\n| 1 | Prep | A | done |\n| 2 | Check | B | pending |\n');
  const before = fs.readdirSync(f.runDir);
  const meter = JSON.parse(execFileSync(process.execPath, [cli, f.id, '--json'], { env: f.env, encoding: 'utf8' }));
  assert.equal(meter.total.done, 1); assert.equal(meter.total.total, 2);
  assert.match(meter.current.label, /B/); assert.equal(meter.total.tokens, null);
  assert.deepEqual(fs.readdirSync(f.runDir), before); assert.equal(fs.existsSync(f.file), false);
});

test('stale snapshot rebuild preserves cached artifacts by stable attempt ID without touching run files', t => {
  const f = fixture(t), snap = JSON.parse(fs.readFileSync(f.file));
  snap.seq = 0;
  snap.items.find(item => item.id === 'a1').artifacts = [
    { label: 'result', path: '/unavailable/1-review.md' }, { label: 'commit', sha: '123abcd' },
  ];
  const original = JSON.stringify(snap);
  fs.writeFileSync(f.file, original);
  const events = [
    { seq: 1, type: 'launch', attempt: f.id + '#1.1', row: '1', ts: '2026-11-11T19:00:00Z', subtask: 'Fixture one' },
    { seq: 2, type: 'start', attempt: f.id + '#1.1', ts: '2026-11-11T19:00:10Z' },
    { seq: 3, type: 'stop', attempt: f.id + '#1.1', ts: '2026-11-11T19:01:00Z', final: true, status: 'completed', tokens: 28000 },
  ];
  const manifestPath = path.join(f.runDir, 'manifest.jsonl'), manifest = events.map(event => JSON.stringify(event)).join('\n') + '\n';
  fs.writeFileSync(manifestPath, manifest);
  const meter = JSON.parse(execFileSync(process.execPath, [cli, f.id, '--json'], { env: f.env, encoding: 'utf8' }));
  assert.equal(meter.tasks.all[0].result, '/unavailable/1-review.md');
  assert.equal(meter.tasks.all[0].sha, '123abcd');
  assert.equal(fs.readFileSync(f.file, 'utf8'), original, 'in-memory rebuild leaves the cached snapshot intact');
  assert.equal(fs.readFileSync(manifestPath, 'utf8'), manifest);
  assert.deepEqual(fs.readdirSync(f.runDir), ['manifest.jsonl']);
  assert.equal(fs.existsSync(path.join(f.registry, 'reports')), false, 'JSON mode remains entirely read-only');
});

test('missing snapshot reads matched result files and rejects ambiguous legacy retry artifacts', t => {
  const f = fixture(t);
  fs.unlinkSync(f.file);
  const explicit = path.join(f.runDir, 'explicit-review.md');
  const files = {
    'explicit-review.md': `---\nattempt: "${f.id}#1.1"\nsha: 111aaaa # verified commit\n---\nResult\n`,
    '2-old.md': `---\nattempt: ${f.id}#2.1\nsha: 222aaaa\n---\nOld attempt\n`,
    '2-new.md': `---\nattempt: '${f.id}#2.2'\nsha: '222bbbb'\n---\nNew attempt\n`,
    '3-ambiguous.md': '---\nsha: 333aaaa\n---\nNo attempt identity; unsafe for a retry\n',
    '4-legacy.md': '---\nsha: 444aaaa\n---\nOne attempt only\n',
  };
  for (const [name, content] of Object.entries(files)) fs.writeFileSync(path.join(f.runDir, name), content);
  const events = [];
  for (const [row, count] of [[1, 1], [2, 2], [3, 2], [4, 1]]) {
    for (let n = 1; n <= count; n++) {
      const base = { attempt: `${f.id}#${row}.${n}`, row: String(row) };
      events.push({ ...base, seq: events.length + 1, type: 'launch', ts: '2026-11-11T19:00:00Z', ...(row === 1 ? { result: explicit } : {}) });
      events.push({ ...base, seq: events.length + 1, type: 'start', ts: '2026-11-11T19:00:10Z' });
      events.push({ ...base, seq: events.length + 1, type: 'stop', ts: '2026-11-11T19:01:00Z', final: true, status: n === count ? 'completed' : 'failed', tokens: 100 });
    }
  }
  const manifest = events.map(event => JSON.stringify(event)).join('\n') + '\n';
  fs.writeFileSync(path.join(f.runDir, 'manifest.jsonl'), manifest);
  const before = new Map(fs.readdirSync(f.runDir).map(name => [name, fs.readFileSync(path.join(f.runDir, name), 'utf8')]));
  const meter = JSON.parse(execFileSync(process.execPath, [cli, f.id, '--json'], { env: f.env, encoding: 'utf8' }));
  const tasks = new Map(meter.tasks.all.map(task => [task.id, task]));
  assert.equal(tasks.get('row:1').result, explicit);
  assert.equal(tasks.get('row:1').sha, '111aaaa');
  assert.equal(tasks.get('row:2').result, path.join(f.runDir, '2-new.md'));
  assert.equal(tasks.get('row:2').sha, '222bbbb');
  assert.equal(tasks.get('row:3').result, null);
  assert.equal(tasks.get('row:3').sha, null);
  assert.equal(tasks.get('row:4').result, path.join(f.runDir, '4-legacy.md'));
  assert.equal(tasks.get('row:4').sha, '444aaaa');
  assert.deepEqual(fs.readdirSync(f.runDir), [...before.keys()]);
  for (const [name, content] of before) assert.equal(fs.readFileSync(path.join(f.runDir, name), 'utf8'), content);
  assert.equal(fs.existsSync(f.file), false);
});
