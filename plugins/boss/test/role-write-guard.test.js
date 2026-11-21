'use strict';
// node --test plugins/boss/test/role-write-guard.test.js
// hooks/role-write-guard.js: read-only roles may Write/Edit only under the runs dir and /tmp/claude-<uid>/ (symlinks resolved).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const GUARD = path.join(__dirname, '..', 'hooks', 'role-write-guard.js');
const HOOKS_JSON = path.join(__dirname, '..', 'hooks', 'hooks.json');
const UID = typeof process.getuid === 'function' ? process.getuid() : 0;

// Scratch data dir; every env var that could point elsewhere is replaced.
const scratch = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'role-guard-')));
const DATA = path.join(scratch, 'data');
const RUNS = path.join(DATA, 'runs');
const OUTSIDE = path.join(scratch, 'outside');
fs.mkdirSync(path.join(RUNS, 'r1'), { recursive: true });
fs.mkdirSync(OUTSIDE, { recursive: true });
fs.symlinkSync(OUTSIDE, path.join(RUNS, 'r1', 'dirlink'));                         // dir symlink out of runs/
fs.writeFileSync(path.join(OUTSIDE, 'target.md'), 'x');
fs.symlinkSync(path.join(OUTSIDE, 'target.md'), path.join(RUNS, 'r1', 'filelink.md')); // file symlink out of runs/
fs.symlinkSync(path.join(scratch, 'nowhere'), path.join(RUNS, 'r1', 'dangling.md'));    // dangling symlink
const ENV = { PATH: process.env.PATH, HOME: scratch, BOSS_DATA: DATA };
test.after(() => fs.rmSync(scratch, { recursive: true, force: true }));

function run(input, env = ENV) {
  const stdin = typeof input === 'string' ? input : JSON.stringify(input);
  const r = spawnSync(process.execPath, [GUARD], { input: stdin, env, encoding: 'utf8' });
  return { code: r.status, out: r.stdout, err: r.stderr };
}
const call = (agent_type, file_path, extra = {}) => ({
  session_id: 'sess-role-guard-1', hook_event_name: 'PreToolUse', tool_name: 'Write',
  agent_type, agent_id: 'a1', tool_input: { file_path, content: 'x' }, ...extra,
});
const ROLES = ['boss:orch-planner', 'boss:orch-plan-checker', 'boss:orch-reviewer', 'boss:orch-scout'];

test('allowed: a path under the runs dir passes for every read-only role', () => {
  for (const role of ROLES) {
    const r = run(call(role, path.join(RUNS, 'r1', '1-result.md')));
    assert.strictEqual(r.code, 0, `${role}: ${r.err}`);
    assert.strictEqual(r.err, '');
  }
});

test('allowed: a new file in a directory that does not exist yet under runs/', () => {
  assert.strictEqual(run(call('boss:orch-planner', path.join(RUNS, 'new-run', 'deep', 'PLAN.md'))).code, 0);
});

test('allowed: /tmp/claude-<uid>/ (the session scratchpad)', () => {
  assert.strictEqual(run(call('boss:orch-scout', `/tmp/claude-${UID}/anything/notes.md`)).code, 0);
});

test('allowed: claude-<uid>/ under TMPDIR too (macOS, Windows scratch); a sibling name under TMPDIR is still blocked', () => {
  const tmp = path.join(scratch, 'tmpdir'); fs.mkdirSync(tmp, { recursive: true });
  const env = { ...ENV, TMPDIR: tmp, TMP: tmp, TEMP: tmp };
  assert.strictEqual(run(call('boss:orch-scout', path.join(tmp, `claude-${UID}`, 'a', 'notes.md')), env).code, 0);
  assert.strictEqual(run(call('boss:orch-scout', path.join(tmp, 'other', 'notes.md')), env).code, 2);
});

test('allowed: a relative path resolves against the hook input cwd', () => {
  assert.strictEqual(run(call('boss:orch-reviewer', 'r1/rel.md', { cwd: RUNS })).code, 0);
});

test('allowed: ORCH_RUNS_DIR moves the allowed root', () => {
  const alt = path.join(scratch, 'alt-runs'); fs.mkdirSync(alt, { recursive: true });
  const env = { ...ENV, ORCH_RUNS_DIR: alt };
  assert.strictEqual(run(call('boss:orch-planner', path.join(alt, 'x.md')), env).code, 0);
  assert.strictEqual(run(call('boss:orch-planner', path.join(RUNS, 'x.md')), env).code, 2);
});

test('blocked: a path outside the allowed roots exits 2 with the guard message', () => {
  for (const role of ROLES) {
    const target = path.join(scratch, 'repo', 'src.js');
    const r = run(call(role, target));
    assert.strictEqual(r.code, 2, role);
    assert.match(r.err, /^\[orch-guard\] Write\/Edit for this role go only under .*\/runs\/ or \/tmp\/claude-<uid>\/ \(symlinks resolved\)\. Blocked: /);
    assert.ok(r.err.includes(target));
    assert.strictEqual(r.out, '');
  }
});

test('blocked: Edit tool input is held to the same rule', () => {
  const r = run(call('boss:orch-plan-checker', '/etc/hosts', { tool_name: 'Edit' }));
  assert.strictEqual(r.code, 2);
});

test('blocked: .. out of runs/ is resolved before the check', () => {
  assert.strictEqual(run(call('boss:orch-planner', path.join(RUNS, '..', 'config.json'))).code, 2);
});

test('blocked: a sibling dir sharing the runs/ name prefix', () => {
  assert.strictEqual(run(call('boss:orch-planner', RUNS + '-evil/x.md')).code, 2);
});

test('blocked: symlink escape through a directory link, and the message shows the real target', () => {
  const r = run(call('boss:orch-planner', path.join(RUNS, 'r1', 'dirlink', 'new.md')));
  assert.strictEqual(r.code, 2);
  assert.ok(r.err.includes(' -> ' + path.join(OUTSIDE, 'new.md')), r.err);
});

test('blocked: symlink escape through a file link', () => {
  assert.strictEqual(run(call('boss:orch-reviewer', path.join(RUNS, 'r1', 'filelink.md'))).code, 2);
});

test('blocked: a dangling symlink inside runs/', () => {
  assert.strictEqual(run(call('boss:orch-scout', path.join(RUNS, 'r1', 'dangling.md'))).code, 2);
});

test('blocked: missing or non-string file_path fails closed for a read-only role', () => {
  assert.strictEqual(run({ agent_type: 'boss:orch-planner', tool_name: 'Write', tool_input: {} }).code, 2);
  assert.strictEqual(run({ agent_type: 'boss:orch-planner', tool_name: 'Write', tool_input: { file_path: 5 } }).code, 2);
  assert.strictEqual(run({ agent_type: 'boss:orch-planner', tool_name: 'Write' }).code, 2);
});

test('pass-through: a builder, any other agent type, and the main chat are never blocked', () => {
  const outside = path.join(scratch, 'repo', 'src.js');
  for (const t of ['boss:orch-builder', 'orch-builder', 'general-purpose', 'other:orch-thing', 'Explore']) {
    const r = run(call(t, outside));
    assert.strictEqual(r.code, 0, t);
    assert.strictEqual(r.err, '');
  }
  const main = call(undefined, outside); delete main.agent_type; delete main.agent_id;
  assert.strictEqual(run(main).code, 0);
});

test('agentBase: bare and namespaced names are both guarded', () => {
  assert.strictEqual(run(call('orch-planner', '/etc/hosts')).code, 2);
  assert.strictEqual(run(call('boss:orch-planner', '/etc/hosts')).code, 2);
});

test('pass-through: unparseable stdin cannot name a role, so it never blocks', () => {
  for (const junk of ['', 'garbage', '[]', 'null', '0', '"str"']) assert.strictEqual(run(junk).code, 0, JSON.stringify(junk));
});

test('state: the guard writes nothing (no files appear in the data dir)', () => {
  const before = fs.readdirSync(DATA).sort();
  run(call('boss:orch-planner', path.join(RUNS, 'r1', 'x.md')));
  run(call('boss:orch-planner', '/etc/hosts'));
  assert.deepStrictEqual(fs.readdirSync(DATA).sort(), before);
});

test('hooks.json: a PreToolUse Write|Edit entry runs this script through boss-node', () => {
  const h = JSON.parse(fs.readFileSync(HOOKS_JSON, 'utf8'));
  const groups = h.hooks.PreToolUse.filter(g => g.matcher === 'Write|Edit');
  assert.strictEqual(groups.length, 1);
  const cmds = groups[0].hooks.map(x => x.command);
  assert.strictEqual(cmds.length, 1);
  assert.match(cmds[0], /^sh -c '.*' "\$\{CLAUDE_PLUGIN_ROOT\}\/bin\/boss-node" "\$\{CLAUDE_PLUGIN_ROOT\}\/hooks\/role-write-guard\.js"$/);
  assert.ok(!cmds[0].includes('agent_id'), 'prefiltered on role names, never on agent_id: subagents are the target');
  assert.ok(fs.existsSync(GUARD));
});

// Win 2: the sh prefilter in front of role-write-guard.js. The command string comes from hooks.json and runs under /bin/sh with a stub
// BOSS_NODE ("-v" passes boss-node's version gate; any other call logs a line, then runs the real node).
const PCMD = (() => { const h = JSON.parse(fs.readFileSync(HOOKS_JSON, 'utf8')); return h.hooks.PreToolUse.find(g => g.matcher === 'Write|Edit').hooks[0].command; })();
const STUB = path.join(scratch, 'stubnode'), FLAG = path.join(scratch, 'node.calls');
fs.writeFileSync(STUB, `#!/bin/sh\n[ "$1" = "-v" ] && { echo v99.0.0; exit 0; }\necho call >> "${FLAG}"\nexec "${process.execPath}" "$@"\n`, { mode: 0o755 });
function viaHook(input, root = path.join(__dirname, '..')) {
  fs.rmSync(FLAG, { force: true });
  const stdin = typeof input === 'string' ? input : JSON.stringify(input);
  const r = spawnSync('/bin/sh', ['-c', PCMD], { input: stdin, encoding: 'utf8', env: { ...ENV, CLAUDE_PLUGIN_ROOT: root, BOSS_NODE: STUB } });
  return { code: r.status, out: r.stdout, err: r.stderr, calls: fs.existsSync(FLAG) ? fs.readFileSync(FLAG, 'utf8').split('\n').filter(Boolean).length : 0 };
}

test('prefilter: a builder or the main chat writing never starts node', () => {
  for (const input of [call('boss:orch-builder', '/etc/hosts'), call('orch-builder', '/etc/hosts'), call('general-purpose', '/etc/hosts'),
    { session_id: 'sess-role-guard-1', tool_name: 'Write', tool_input: { file_path: '/etc/hosts', content: 'x' } }]) {
    const r = viaHook(input);
    assert.deepStrictEqual([r.code, r.calls, r.out, r.err], [0, 0, '', ''], JSON.stringify(input).slice(0, 80));
  }
});

test('prefilter: each read-only role still reaches node and is denied (exit 2 passes through), compact and pretty-printed JSON', () => {
  for (const role of ROLES.concat(['orch-planner', 'orch-scout'])) {
    for (const input of [JSON.stringify(call(role, '/etc/hosts')), JSON.stringify(call(role, '/etc/hosts'), null, 2)]) {
      const r = viaHook(input);
      assert.deepStrictEqual([r.code, r.calls], [2, 1], role);
      assert.match(r.err, /^\[orch-guard\] /);
    }
    const ok = viaHook(call(role, path.join(RUNS, 'r1', 'x.md')));
    assert.deepStrictEqual([ok.code, ok.calls], [0, 1], role + ' under runs/ passes, via node');
  }
});

test('prefilter: a role name in the file text only costs a node run, which still lets a builder through', () => {
  const r = viaHook(call('boss:orch-builder', '/etc/hosts', { tool_input: { file_path: '/etc/hosts', content: 'this mentions orch-scout and orch-reviewer' } }));
  assert.deepStrictEqual([r.code, r.calls, r.out], [0, 1, '']);
});

test('prefilter: empty and malformed stdin exit 0 without node', () => {
  for (const junk of ['', 'garbage', '[]']) { const r = viaHook(junk); assert.deepStrictEqual([r.code, r.calls], [0, 0], JSON.stringify(junk)); }
});

test('prefilter: the output of the real node is what the hook returns for a denied role (same stderr as running the guard directly)', () => {
  const input = call('boss:orch-reviewer', '/etc/hosts');
  assert.strictEqual(viaHook(input).err, run(input).err);
});
