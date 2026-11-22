import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { collectArtifacts, extractPaths, lastAssistantText, rowFromName, kindOf, inBounds, FILES_MAX } from '../artifacts.mjs';
import { buildBoard, loadSources } from '../board.mjs';

const mk = () => {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'boss-art-')));
  const home = path.join(tmp, 'home'), runs = path.join(home, '.claude', 'orchestrator', 'runs'), projects = path.join(home, '.claude', 'projects');
  for (const d of [runs, projects, path.join(home, 'work'), path.join(home, '.ssh')]) fs.mkdirSync(d, { recursive: true });
  const put = (f, body = 'x', mtime) => { fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, body); if (mtime) fs.utimesSync(f, mtime / 1000, mtime / 1000); return f; };
  return { tmp, home, runs, projects, put, done: () => fs.rmSync(tmp, { recursive: true, force: true }) };
};
// extractPaths only reads /var, /tmp, /home ... paths; macOS temp dirs really live under /private/var, which names the same file
const shown = (f) => f.replace(/^\/private(?=\/(?:var|tmp)\/)/, '');
/** A subagent transcript whose last assistant message is `text`. */
const transcript = (projects, session, task, text) => {
  const f = path.join(projects, '-proj', session, 'subagents', `agent-${task}.jsonl`); fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, [{ type: 'user', message: { role: 'user', content: 'go' } }, { type: 'assistant', message: { role: 'assistant', content: [{ type: 'thinking', thinking: 'hm' }, { type: 'text', text }] } }, { type: 'api-request' }].map((e) => JSON.stringify(e)).join('\n') + '\n');
  return f;
};

test('extractPaths: absolute and ~ paths only, trailing punctuation and :line suffixes removed, ~ expanded', () => {
  const t = 'Wrote `/home/u/work/a.png`, see (~/work/b.md). Also /home/u/work/c.html:12:3 and **/home/u/x/d.png**; relative src/x.ts is ignored. /etc/passwd is not a home path.';
  assert.deepEqual(extractPaths(t, '/home/u'), ['/home/u/work/a.png', '/home/u/work/b.md', '/home/u/work/c.html', '/home/u/x/d.png']);
  assert.deepEqual(extractPaths('', '/h'), []); assert.deepEqual(extractPaths(null, '/h'), []);
});
test('lastAssistantText: text of the last assistant entry, thinking and api entries skipped', () => {
  const e = [{ type: 'assistant', message: { content: [{ type: 'text', text: 'first' }] } }, { type: 'assistant', message: { content: [{ type: 'thinking', thinking: 'x' }, { type: 'text', text: 'last' }] } }, { type: 'api-request' }];
  assert.equal(lastAssistantText(e), 'last');
  assert.equal(lastAssistantText([{ type: 'user' }]), '');
});
test('rowFromName, kindOf', () => {
  assert.equal(rowFromName('4c-files-run-300.png'), '4c'); assert.equal(rowFromName('2b-stuck.md'), '2b'); assert.equal(rowFromName('DECISIONS.md'), null);
  assert.equal(kindOf('a.PNG'), 'image'); assert.equal(kindOf('a.md'), 'md'); assert.equal(kindOf('a.mts'), null); assert.equal(kindOf('manifest.jsonl'), null);
});

test('collect: run folder, snapshot result and final-message paths merge by real path; explicit row beats the filename prefix; newest first', () => {
  const m = mk();
  try {
    const runDir = path.join(m.runs, 'r1');
    m.put(path.join(runDir, '4-controls.md'), 'x', 3000); m.put(path.join(runDir, '4c-shot.png'), 'x', 5000); m.put(path.join(runDir, 'DECISIONS.md'), 'x', 1000);
    m.put(path.join(runDir, 'ledger.jsonl')); m.put(path.join(runDir, 'manifest.jsonl')); m.put(path.join(runDir, 'progress.json')); m.put(path.join(runDir, '.hidden.png')); m.put(path.join(runDir, 'code.mts'));
    const shot = m.put(path.join(m.home, 'work', 'out.png'), 'x', 4000);
    const session = 'sess-aaaaaaaa', task = 'task-bbbbbbbb';
    transcript(m.projects, session, task, `Done. Screenshot ${shown(shot)} and ${shown(path.join(runDir, '4c-shot.png'))}; missing /home/nobody/zzz.png`);
    const snap = { items: [{ id: 'row:4', kind: 'row', artifacts: [{ label: 'result', path: path.join(runDir, '4-controls.md') }] }, { id: 'row:4/a1', parent: 'row:4', kind: 'attempt', artifacts: [{ label: 'commit', path: null, sha: 'abc1234' }] }] };
    const out = collectArtifacts({ dirs: [runDir, runDir], snap, events: [{ attempt: 'r1#4c.1', row: '4c', session, task_id: task, output_file: '/tmp/gone/agent.output' }], roots: [fs.realpathSync(m.runs)], home: m.home, projectsDir: m.projects });
    assert.deepEqual(out.map((a) => a.name), ['4c-shot.png', 'out.png', '4-controls.md', 'DECISIONS.md'], 'newest first; machinery, dotfiles, code and missing files left out; each file once');
    const by = Object.fromEntries(out.map((a) => [a.name, a]));
    assert.equal(by['4-controls.md'].row, '4', 'snapshot row item'); assert.deepEqual(by['4-controls.md'].via, ['result', 'folder']);
    assert.equal(by['out.png'].row, '4c', 'row of the attempt whose message named it'); assert.deepEqual(by['out.png'].via, ['message']);
    assert.equal(by['4c-shot.png'].row, '4c'); assert.deepEqual(by['4c-shot.png'].via, ['message', 'folder']);
    assert.equal(by['DECISIONS.md'].row, null); assert.equal(by['DECISIONS.md'].prefix_row, null);
    assert.equal(by['4c-shot.png'].prefix_row, '4c'); assert.equal(by['4c-shot.png'].kind, 'image'); assert.equal(by['out.png'].size, 1);
  } finally { m.done(); }
});

test('collect: output_file symlink is used when it resolves inside projects; a broken link falls back to session/task; links elsewhere are ignored', () => {
  const m = mk();
  try {
    const a = m.put(path.join(m.home, 'work', 'a.png')), b = m.put(path.join(m.home, 'work', 'b.png'));
    const session = 'sess-cccccccc';
    const t1 = transcript(m.projects, session, 'task-1111aaaa', `see ${shown(a)}`);
    const link = path.join(m.tmp, 'tasks', 'x.output'); fs.mkdirSync(path.dirname(link), { recursive: true }); fs.symlinkSync(t1, link);
    const outside = m.put(path.join(m.tmp, 'elsewhere.jsonl'), JSON.stringify({ type: 'assistant', message: { content: [{ type: 'text', text: `leak ${shown(b)}` }] } }) + '\n');
    const link2 = path.join(m.tmp, 'tasks', 'y.output'); fs.symlinkSync(outside, link2);
    transcript(m.projects, session, 'task-2222bbbb', `fallback ${shown(b)}`);
    const ev = (task, f) => ({ attempt: 'r#1.1', row: '1', session, task_id: task, output_file: f });
    const run = (events) => collectArtifacts({ events, roots: [], home: m.home, projectsDir: m.projects }).map((x) => x.name).sort();
    assert.deepEqual(run([ev('task-1111aaaa', link)]), ['a.png']);
    assert.deepEqual(run([ev('task-2222bbbb', path.join(m.tmp, 'tasks', 'dangling.output'))]), ['b.png'], 'dangling link: found by session and task id');
    assert.deepEqual(run([ev('task-9999zzzz', link2)]), [], 'a link that leaves the projects dir is not read');
    assert.deepEqual(run([{ attempt: 'x', row: '1', session: '../..', task_id: '../x', output_file: null }]), [], 'ids that could walk the tree find nothing');
  } finally { m.done(); }
});

test('collect: only trusted run folders and home paths without dot segments; symlinks out and *.key are refused', () => {
  const m = mk();
  try {
    const ok = m.put(path.join(m.home, 'work', 'ok.png')), dot = m.put(path.join(m.home, '.ssh', 'id.png')), key = m.put(path.join(m.home, 'work', 'a.key'));
    const outside = m.put(path.join(m.tmp, 'outside', 'o.png'));
    fs.symlinkSync(path.join(m.tmp, 'outside'), path.join(m.home, 'work', 'escape'));
    const runDir = path.join(m.runs, 'r2'); m.put(path.join(runDir, 'in-root.png'));
    fs.symlinkSync(outside, path.join(runDir, 'link.png'));
    const snap = { items: [{ id: 'row:1', kind: 'row', artifacts: [dot, key, outside, path.join(m.home, 'work', 'escape', 'o.png'), ok, '../x.png', 'rel.png'].map((p) => ({ label: 'result', path: p })) }] };
    const out = collectArtifacts({ dirs: [runDir, '/etc', path.join(m.home, '.ssh')], snap, roots: [fs.realpathSync(m.runs)], home: m.home });
    assert.deepEqual(out.map((a) => a.name).sort(), ['in-root.png', 'ok.png']);
    assert.equal(inBounds(dot, { home: m.home, roots: [] }), false); assert.equal(inBounds(key, { home: m.home, roots: [] }), false);
    assert.equal(inBounds(path.join(fs.realpathSync(m.runs), 'r2', 'x.png'), { home: m.home, roots: [fs.realpathSync(m.runs)] }), true, 'a run folder is trusted even under a dot dir');
  } finally { m.done(); }
});

test('board: run.artifacts is capped and newest first, a filename prefix counts only for a row that exists, and the total stays honest', () => {
  const m = mk();
  try {
    const runDir = path.join(m.runs, 'r3');
    for (let i = 0; i < FILES_MAX + 5; i++) m.put(path.join(runDir, `f${String(i).padStart(2, '0')}.png`), 'x', 1_000_000 + i * 1000);
    m.put(path.join(runDir, '1-has-row.md'), 'x', 5_000_000); m.put(path.join(runDir, '9-no-row.md'), 'x', 4_000_000); m.put(path.join(runDir, '2026-11-02-date.md'), 'x', 3_000_000);
    const snap = { schema: 'boss.progress/1', tool: 'orchestrator', run_id: 'r3', title: 'g', state: 'running', source_dir: runDir, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), seq: 1, items: [{ id: 'row:1', parent: null, kind: 'row', label: 'one', state: 'running' }], attention: [], counts: [], writer: { pid: 1, start_ticks: null, boot_id: 'b', kind: 'oneshot' } };
    // loadSources wires it up: snapshot on disk -> run folder + trusted run root -> src.artifacts
    const bossDir = path.join(m.tmp, 'boss'); fs.mkdirSync(path.join(bossDir, 'runs', 'orchestrator', 'r3'), { recursive: true });
    fs.writeFileSync(path.join(bossDir, 'runs', 'orchestrator', 'r3', 'progress.json'), JSON.stringify(snap));
    const prevHome = process.env.HOME; process.env.HOME = m.home;   // os.homedir() follows HOME on linux
    try {
      const src = loadSources({ boss: bossDir, sessions: path.join(m.tmp, 's'), leases: path.join(m.tmp, 'l'), runs: m.runs, wfp: path.join(m.tmp, 'w'), inbox: path.join(m.tmp, 'i'), artifactTtlMs: 0 }, Date.now());
      assert.equal(src.snapshots.length, 1);
      assert.equal(src.artifacts['orchestrator/r3'].length, FILES_MAX + 8, 'loadSources collects everything; buildBoard caps for display');
    } finally { process.env.HOME = prevHome; }
    // buildBoard is pure: feed it what loadSources would
    const items = collectArtifacts({ dirs: [runDir], snap, roots: [fs.realpathSync(m.runs)], home: m.home });
    const b = buildBoard({ snapshots: [{ snap }], artifacts: { 'orchestrator/r3': items }, probe: { bootId: () => 'b', startTicks: () => null, startEpoch: () => null } }, Date.now());
    const run = b.runs[0];
    assert.equal(run.artifacts.length, FILES_MAX); assert.equal(run.artifacts_total, FILES_MAX + 8);
    assert.deepEqual(run.artifacts.slice(0, 3).map((a) => a.name), ['1-has-row.md', '9-no-row.md', '2026-11-02-date.md']);
    assert.deepEqual(run.artifacts.slice(0, 3).map((a) => a.row), ['1', null, null], 'prefix 9 and 2026 name no row of this run');
    assert.ok(run.artifacts.every((a, i, arr) => i === 0 || arr[i - 1].mtime_ms >= a.mtime_ms), 'newest first');
    assert.deepEqual(Object.keys(run.artifacts[0]).sort(), ['kind', 'mtime_ms', 'name', 'path', 'row', 'size', 'via']);
    assert.deepEqual(buildBoard({ snapshots: [{ snap }], probe: { bootId: () => 'b', startTicks: () => null, startEpoch: () => null } }, Date.now()).runs[0].artifacts, [], 'no collected files: an empty list, not missing');
  } finally { m.done(); }
});
