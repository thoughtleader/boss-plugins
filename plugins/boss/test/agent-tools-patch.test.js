'use strict';
// agent-tools-patch is opt-in: nothing in the plugin dir changes unless config.json sets patchPluginAgents: true.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { spawnSync } = require('child_process');

const HOOK = path.join(__dirname, '..', 'hooks', 'agent-tools-patch.js');
const AGENTS = path.join(__dirname, '..', 'agents');
const tools = require(HOOK);

const mk = () => fs.mkdtempSync(path.join(os.tmpdir(), 'atp-'));
const agent = (t) => `---\nname: orch-x\ntools: ${t}\nmodel: sonnet\n---\n\nbody\n`;
function setup(cfg) {
  const data = mk(), dir = mk();
  fs.writeFileSync(path.join(dir, 'orch-scout.md'), agent('Read, Grep'));
  fs.writeFileSync(path.join(dir, 'orch-builder.md'), agent('Read, Bash'));
  if (cfg) fs.writeFileSync(path.join(data, 'config.json'), JSON.stringify(cfg));
  return { env: { BOSS_DATA: data }, dir, data };
}
const read = (dir, f) => fs.readFileSync(path.join(dir, f), 'utf8');
const EXTRA = { '*': ['mcp__all__*'], 'orch-scout': ['mcp__example__*'] };

test('off by default: extraAgentTools alone changes nothing', () => {
  const { env, dir } = setup({ extraAgentTools: EXTRA });
  assert.strictEqual(tools.run({ env, dir }), 0);
  assert.strictEqual(read(dir, 'orch-scout.md'), agent('Read, Grep'));
  assert.strictEqual(read(dir, 'orch-builder.md'), agent('Read, Bash'));
});

test('off when patchPluginAgents is not exactly true, or no config at all', () => {
  for (const v of [false, 'true', 1, null]) {
    const { env, dir } = setup({ patchPluginAgents: v, extraAgentTools: EXTRA });
    assert.strictEqual(tools.run({ env, dir }), 0, String(v));
    assert.strictEqual(read(dir, 'orch-scout.md'), agent('Read, Grep'));
  }
  const { env, dir } = setup(null);
  assert.strictEqual(tools.run({ env, dir }), 0);
});

test('opt-in: adds the missing tools, per agent and for "*", and is idempotent', () => {
  const { env, dir } = setup({ patchPluginAgents: true, extraAgentTools: EXTRA });
  assert.strictEqual(tools.run({ env, dir }), 2);
  assert.ok(read(dir, 'orch-scout.md').includes('tools: Read, Grep, mcp__all__*, mcp__example__*\n'));
  assert.ok(read(dir, 'orch-builder.md').includes('tools: Read, Bash, mcp__all__*\n'));
  assert.strictEqual(tools.run({ env, dir }), 0);
});

test('opt-in without extraAgentTools does nothing', () => {
  const { env, dir } = setup({ patchPluginAgents: true });
  assert.strictEqual(tools.run({ env, dir }), 0);
  assert.strictEqual(read(dir, 'orch-scout.md'), agent('Read, Grep'));
});

test('hook CLI without opt-in leaves the real plugin agents byte-identical and exits 0', () => {
  const sum = () => fs.readdirSync(AGENTS).sort().map((f) => f + crypto.createHash('sha1').update(fs.readFileSync(path.join(AGENTS, f))).digest('hex')).join();
  const before = sum();
  const { data } = setup({ extraAgentTools: { '*': ['mcp__zzz__*'] } });
  const r = spawnSync(process.execPath, [HOOK], { env: { ...process.env, BOSS_DATA: data, ORCH_DIR: '' }, encoding: 'utf8' });
  assert.strictEqual(r.status, 0);
  assert.strictEqual(r.stdout + r.stderr, '');
  assert.strictEqual(sum(), before);
});

// ---- --lift: undo the patch before a merge, re-apply after (skip-worktree hides the edit from `git status` but not from `git merge`) ----
const git = (cwd, ...args) => spawnSync('git', ['-c', 'user.name=t', '-c', 'user.email=scratch', '-c', 'commit.gpgsign=false', ...args], { cwd, encoding: 'utf8' });
const flags = (dir) => git(dir, 'ls-files', '-v').stdout.split('\n').filter(Boolean).map((l) => l.slice(0, 1) + ' ' + l.slice(2)).sort();
// a scratch git repo whose agents/ folder holds two committed agent files, patched by run() (so both are skip-worktree)
function repo(cfg = { patchPluginAgents: true, extraAgentTools: EXTRA }) {
  const root = mk(), dir = path.join(root, 'agents'), data = mk();
  fs.mkdirSync(dir);
  fs.writeFileSync(path.join(dir, 'orch-scout.md'), agent('Read, Grep'));
  fs.writeFileSync(path.join(dir, 'orch-builder.md'), agent('Read, Bash'));
  fs.writeFileSync(path.join(data, 'config.json'), JSON.stringify(cfg));
  assert.strictEqual(git(root, 'init', '-q', '-b', 'main').status, 0);
  git(root, 'add', '.'); assert.strictEqual(git(root, 'commit', '-q', '-m', 'base').status, 0);
  return { root, dir, env: { BOSS_DATA: data } };
}
const cli = (...args) => spawnSync(process.execPath, [HOOK, ...args], { env: { ...process.env, ORCH_DIR: '' }, encoding: 'utf8' });

test('lift: restores the committed text, clears the flag, and a second run gives back the patched line and the flag', () => {
  const { root, dir, env } = repo();
  assert.strictEqual(tools.run({ env, dir }), 2);
  assert.ok(read(dir, 'orch-scout.md').includes('tools: Read, Grep, mcp__all__*, mcp__example__*\n'));
  assert.deepStrictEqual(flags(dir), ['S orch-builder.md', 'S orch-scout.md']);
  assert.strictEqual(git(root, 'status', '--porcelain').stdout, '', 'the edit is hidden from git status');
  const r = tools.lift({ dir });
  assert.deepStrictEqual(r.results.map((x) => [x.file, x.status]), [['orch-builder.md', 'lifted'], ['orch-scout.md', 'lifted']]);
  assert.strictEqual(r.refused, 0);
  assert.strictEqual(read(dir, 'orch-scout.md'), agent('Read, Grep'));
  assert.strictEqual(read(dir, 'orch-builder.md'), agent('Read, Bash'));
  assert.deepStrictEqual(flags(dir), ['H orch-builder.md', 'H orch-scout.md']);
  assert.strictEqual(git(root, 'status', '--porcelain').stdout, '');
  // lifting again: nothing is patched any more
  assert.deepStrictEqual(tools.lift({ dir }).results.map((x) => x.status), ['not patched', 'not patched']);
  // run again (the no-argument call) re-applies the patch and the flag
  assert.strictEqual(tools.run({ env, dir }), 2);
  assert.ok(read(dir, 'orch-scout.md').includes('tools: Read, Grep, mcp__all__*, mcp__example__*\n'));
  assert.deepStrictEqual(flags(dir), ['S orch-builder.md', 'S orch-scout.md']);
  assert.strictEqual(tools.run({ env, dir }), 0, 'idempotent');
});

test('lift: a file with a second local change is refused and left as it was; the others still lift', () => {
  const { dir, env } = repo();
  tools.run({ env, dir });
  const edited = read(dir, 'orch-scout.md').replace('body', 'body, edited by hand');
  fs.writeFileSync(path.join(dir, 'orch-scout.md'), edited);
  const r = tools.lift({ dir });
  assert.deepStrictEqual(r.results.map((x) => [x.file, x.status]), [['orch-builder.md', 'lifted'], ['orch-scout.md', 'refused']]);
  assert.match(r.results[1].why, /outside the tools: line/);
  assert.strictEqual(r.refused, 1);
  assert.strictEqual(read(dir, 'orch-scout.md'), edited, 'refused file untouched');
  assert.deepStrictEqual(flags(dir), ['H orch-builder.md', 'S orch-scout.md'], 'the refused file keeps its flag');
  // a tools: line that lost a committed entry is refused too
  fs.writeFileSync(path.join(dir, 'orch-scout.md'), agent('Read, mcp__all__*'));
  assert.match(tools.lift({ dir, files: ['orch-scout.md'] }).results[0].why, /lacks committed entries \(Grep\)/);
});

test('lift CLI: one line per file, names the refused file, exit 1 on a refusal; a path argument lifts only that file', () => {
  const { dir, env } = repo();
  tools.run({ env, dir });
  const scout = path.join(dir, 'orch-scout.md'), builder = path.join(dir, 'orch-builder.md');
  const ok = cli('--lift', scout);
  assert.strictEqual(ok.status, 0); assert.strictEqual(ok.stdout, 'lifted: orch-scout.md\n'); assert.strictEqual(ok.stderr, '');
  assert.strictEqual(read(dir, 'orch-scout.md'), agent('Read, Grep'));
  assert.ok(read(dir, 'orch-builder.md').includes('mcp__all__*'), 'the file not named is left patched');
  fs.writeFileSync(builder, read(dir, 'orch-builder.md') + 'extra line\n');
  const bad = cli('--lift', builder, scout);
  assert.strictEqual(bad.status, 1);
  assert.strictEqual(bad.stdout, 'refused: orch-builder.md: it differs from the committed version outside the tools: line\nnot patched: orch-scout.md (not marked skip-worktree)\n');
  assert.ok(read(dir, 'orch-builder.md').endsWith('extra line\n'));
  const outside = cli('--lift', path.join(mk(), 'x.md'));       // not a git checkout: reported, not a crash
  assert.strictEqual(outside.status, 0); assert.match(outside.stdout, /^not patched: x\.md/);
});

test('merge into a patched checkout: refused while patched, works after lift, and run re-applies the patch on the merged text', () => {
  const { root, dir, env } = repo();
  git(root, 'checkout', '-q', '-b', 'other');
  fs.writeFileSync(path.join(dir, 'orch-scout.md'), agent('Read, Grep').replace('body', 'body, new rules'));
  git(root, 'commit', '-q', '-am', 'change the scout'); git(root, 'checkout', '-q', 'main');
  tools.run({ env, dir });
  assert.strictEqual(git(root, 'status', '--porcelain').stdout, '', 'status prints nothing: the old pre-merge check passes');
  const refused = git(root, 'merge', '--ff-only', 'other');
  assert.notStrictEqual(refused.status, 0); assert.match(refused.stderr, /local changes|overwritten/i);
  assert.strictEqual(tools.lift({ dir }).refused, 0);
  const merged = git(root, 'merge', '--ff-only', 'other');
  assert.strictEqual(merged.status, 0, merged.stderr);
  tools.run({ env, dir });
  assert.ok(read(dir, 'orch-scout.md').includes('body, new rules'));
  assert.ok(read(dir, 'orch-scout.md').includes('tools: Read, Grep, mcp__all__*, mcp__example__*\n'));
  assert.deepStrictEqual(flags(dir), ['S orch-builder.md', 'S orch-scout.md']);
});

test('no argument: unchanged (silent, exit 0, nothing patched without the opt-in; patches a plain folder as before), and --lift in a non-checkout says not patched', () => {
  const sum = () => fs.readdirSync(AGENTS).sort().map((f) => f + crypto.createHash('sha1').update(fs.readFileSync(path.join(AGENTS, f))).digest('hex')).join();
  const before = sum();
  const { data } = setup({ patchPluginAgents: false, extraAgentTools: EXTRA });
  const r = spawnSync(process.execPath, [HOOK], { env: { ...process.env, BOSS_DATA: data, ORCH_DIR: '' }, encoding: 'utf8' });
  assert.deepStrictEqual([r.status, r.stdout, r.stderr], [0, '', '']);
  assert.strictEqual(sum(), before);
  // a plain (non-git) dir: run patches as before and --lift reports "not patched"
  const { env, dir } = setup({ patchPluginAgents: true, extraAgentTools: EXTRA });
  assert.strictEqual(tools.run({ env, dir }), 2);
  assert.deepStrictEqual(tools.lift({ dir }).results.map((x) => x.status), ['not patched', 'not patched']);
});

test('lift: an edit above the tools: line (description, model) is refused, exit 1, file bytes and the S flag unchanged', () => {
  for (const [what, from, to] of [['description', 'name: orch-x', 'name: orch-x\ndescription: hand edited'], ['model', 'model: sonnet', 'model: opus'], ['name', 'name: orch-x', 'name: orch-y']]) {
    const { dir, env } = repo();
    tools.run({ env, dir });
    const file = path.join(dir, 'orch-scout.md'), edited = read(dir, 'orch-scout.md').replace(from, to);
    assert.notStrictEqual(edited, read(dir, 'orch-scout.md'), what);
    fs.writeFileSync(file, edited);
    const before = flags(dir);
    const r = cli('--lift', file);
    assert.strictEqual(r.status, 1, what);
    assert.strictEqual(r.stdout, 'refused: orch-scout.md: it differs from the committed version outside the tools: line\n', what);
    assert.strictEqual(fs.readFileSync(file, 'utf8'), edited, what + ': bytes unchanged');
    assert.deepStrictEqual(flags(dir), before, what + ': flag unchanged');
  }
});

test('lift: all or nothing per file: with index.lock held the text and the flag both stay as they were, and the line says refused', () => {
  const { root, dir, env } = repo();
  tools.run({ env, dir });
  const patched = read(dir, 'orch-scout.md'), before = flags(dir);
  fs.writeFileSync(path.join(root, '.git', 'index.lock'), '');
  const r = cli('--lift', path.join(dir, 'orch-scout.md'));
  assert.strictEqual(r.status, 1);
  assert.match(r.stdout, /^refused: orch-scout\.md: could not lift: /);
  assert.strictEqual(read(dir, 'orch-scout.md'), patched, 'the patched text is back');
  assert.deepStrictEqual(flags(dir), before);
  fs.rmSync(path.join(root, '.git', 'index.lock'));
  assert.strictEqual(cli('--lift', path.join(dir, 'orch-scout.md')).stdout, 'lifted: orch-scout.md\n', 'a retry lifts cleanly');
  assert.strictEqual(read(dir, 'orch-scout.md'), agent('Read, Grep'));
});
