'use strict';
// node --test plugins/boss/test/paths.test.js
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const P = require('../lib/paths.js');
const PATHS_JS = path.join(__dirname, '..', 'lib', 'paths.js');
const BOSS_NODE_BIN = path.join(__dirname, '..', 'bin', 'boss-node');

const mk = () => fs.mkdtempSync(path.join(os.tmpdir(), 'boss-paths-'));
// A clean env: only what a test passes. No ORCH_*/BOSS_*/CLAUDE_* leaks in from the host.
const E = (o = {}) => Object.assign({ HOME: '/nonexistent-home' }, o);

test('default: <configDir>/boss-data when no legacy dir exists', () => {
  const cfg = mk();
  const env = E({ CLAUDE_CONFIG_DIR: cfg });
  assert.strictEqual(P.configDir(env), cfg);
  assert.strictEqual(P.dataDir(env), path.join(cfg, 'boss-data'));
  assert.strictEqual(P.runsDir(env), path.join(cfg, 'boss-data', 'runs'));
  assert.strictEqual(P.leasesDir(env), path.join(cfg, 'boss-data', 'leases'));
  assert.strictEqual(P.wfpStateDir(env), path.join(cfg, 'boss-data', 'wfp', 'state'));
  assert.strictEqual(P.wfpHandoffsDir(env), path.join(cfg, 'boss-data', 'wfp', 'handoffs'));
  assert.strictEqual(P.metricsDb(env), path.join(cfg, 'boss-data', 'metrics.db'));
  assert.strictEqual(P.errorsLog(env), path.join(cfg, 'boss-data', 'errors.log'));
  assert.strictEqual(P.progressRoot(env), path.join(cfg, 'boss'));
  assert.strictEqual(P.projectsDir(env), path.join(cfg, 'projects'));
  assert.strictEqual(P.sessionsDir(env), path.join(cfg, 'sessions'));
});

test('configDir falls back to ~/.claude', () => {
  assert.strictEqual(P.configDir({}), path.join(os.homedir(), '.claude'));
});

test('precedence: ORCH_DIR > BOSS_DATA > legacy > default', () => {
  const cfg = mk();
  fs.mkdirSync(path.join(cfg, 'orchestrator', 'runs'), { recursive: true });
  const legacy = path.join(cfg, 'orchestrator');
  assert.strictEqual(P.dataDir(E({ CLAUDE_CONFIG_DIR: cfg })), legacy, 'legacy dir with runs/ wins over default');
  assert.strictEqual(P.dataDir(E({ CLAUDE_CONFIG_DIR: cfg, BOSS_DATA: '/b' })), '/b', 'BOSS_DATA beats legacy');
  assert.strictEqual(P.dataDir(E({ CLAUDE_CONFIG_DIR: cfg, BOSS_DATA: '/b', ORCH_DIR: '/o' })), '/o', 'ORCH_DIR beats BOSS_DATA');
  const bare = mk();
  fs.mkdirSync(path.join(bare, 'orchestrator'), { recursive: true }); // no runs/: not a legacy install
  assert.strictEqual(P.dataDir(E({ CLAUDE_CONFIG_DIR: bare })), path.join(bare, 'boss-data'));
});

test('per-path overrides', () => {
  const env = E({ BOSS_DATA: '/d', ORCH_RUNS_DIR: '/r', WFP_STATE_DIR: '/w', ORCH_METRICS_DB: '/m.db',
    BOSS_PROGRESS_DIR: '/p', ORCH_PROJECTS_DIR: '/pr', ORCH_SESSIONS_DIR: '/s' });
  assert.strictEqual(P.runsDir(env), '/r');
  assert.strictEqual(P.wfpStateDir(env), '/w');
  assert.strictEqual(P.metricsDb(env), '/m.db');
  assert.strictEqual(P.progressRoot(env), '/p');
  assert.strictEqual(P.projectsDir(env), '/pr');
  assert.strictEqual(P.sessionsDir(env), '/s');
  assert.strictEqual(P.leasesDir(env), path.join('/d', 'leases'));
});

test('isScratch: only ORCH_DIR / ORCH_RUNS_DIR, never BOSS_DATA', () => {
  assert.strictEqual(P.isScratch(E({ BOSS_DATA: '/b' })), false);
  assert.strictEqual(P.isScratch(E({ BOSS_PROGRESS_DIR: '/p' })), false);
  assert.strictEqual(P.isScratch(E({ ORCH_DIR: '/o' })), true);
  assert.strictEqual(P.isScratch(E({ ORCH_RUNS_DIR: '/r' })), true);
  assert.strictEqual(P.isScratch(E()), false);
});

test('agentBase strips the plugin prefix', () => {
  assert.strictEqual(P.agentBase('boss:orch-builder'), 'orch-builder');
  assert.strictEqual(P.agentBase('orch-scout'), 'orch-scout');
  assert.strictEqual(P.agentBase('a:b:c'), 'b:c');
  assert.strictEqual(P.agentBase(undefined), '');
  assert.strictEqual(P.agentBase(null), '');
});

test('pluginRoot is the plugin dir', () => {
  assert.strictEqual(P.pluginRoot, path.resolve(__dirname, '..'));
  assert.ok(fs.existsSync(path.join(P.pluginRoot, '.claude-plugin', 'plugin.json')));
});

test('loadConfig engine: defaults, then overlay merged one level deep', () => {
  const data = mk();
  const env = E({ BOSS_DATA: data });
  let r = P.loadConfig('engine', env);
  assert.strictEqual(r.error, null);
  assert.strictEqual(r.cfg.returnCapBytes, 2048);
  assert.strictEqual(r.cfg.progress, true);
  assert.deepStrictEqual(r.cfg.metrics, { commitRepos: [], commitAuthor: null });
  assert.deepStrictEqual(r.cfg.guard, { exemptPaths: [] });
  assert.strictEqual(r.cfg.analytics.lib, null);

  fs.writeFileSync(path.join(data, 'config.json'), JSON.stringify({
    returnCapBytes: 1000, passStatuses: ['ok'], metrics: { commitRepos: ['proj'] } }));
  r = P.loadConfig('engine', env);
  assert.strictEqual(r.error, null);
  assert.strictEqual(r.cfg.returnCapBytes, 1000);
  assert.deepStrictEqual(r.cfg.passStatuses, ['ok'], 'arrays replace');
  assert.deepStrictEqual(r.cfg.metrics, { commitRepos: ['proj'], commitAuthor: null }, 'objects merge one level');
  assert.strictEqual(r.cfg.leaseTtlSec, 7200, 'untouched keys stay');
});

test('loadConfig never throws: bad overlay gives defaults plus an error', () => {
  const data = mk();
  const env = E({ BOSS_DATA: data });
  fs.writeFileSync(path.join(data, 'config.json'), '{not json');
  let r = P.loadConfig('engine', env);
  assert.ok(r.error && /config\.json/.test(r.error));
  assert.strictEqual(r.cfg.returnCapBytes, 2048);
  fs.writeFileSync(path.join(data, 'config.json'), '[1,2]');
  r = P.loadConfig('engine', env);
  assert.ok(r.error && /not a JSON object/.test(r.error));
  assert.strictEqual(r.cfg.returnCapBytes, 2048);
  r = P.loadConfig('nope', env);
  assert.deepStrictEqual(r.cfg, {});
  assert.ok(r.error);
});

test('loadConfig wfp: wfp/config.default.json plus <data>/wfp.json', () => {
  const data = mk();
  const env = E({ BOSS_DATA: data });
  const base = P.loadConfig('wfp', env);
  assert.strictEqual(base.error, null);
  assert.ok(base.cfg && typeof base.cfg === 'object' && Object.keys(base.cfg).length > 0);
  fs.writeFileSync(path.join(data, 'wfp.json'), JSON.stringify({ defaultProfile: 'balanced' }));
  const r = P.loadConfig('wfp', env);
  assert.strictEqual(r.error, null);
  assert.strictEqual(r.cfg.defaultProfile, 'balanced');
  assert.deepStrictEqual(Object.keys(r.cfg).sort(), Object.keys(Object.assign({}, base.cfg, { defaultProfile: 1 })).sort());
});

test('workerFiles: base template, role files by role, then WORKER.local.md when present', () => {
  const data = mk();
  const env = E({ BOSS_DATA: data });
  const t = (f) => path.join(P.pluginRoot, 'templates', f);
  const base = t('WORKER.md'), builder = t('WORKER.builder.md'), runtime = t('WORKER.runtime.md');
  assert.deepStrictEqual(P.workerFiles(env, 'orch-scout'), [base]);
  assert.deepStrictEqual(P.workerFiles(env, 'boss:orch-planner'), [base]);
  assert.deepStrictEqual(P.workerFiles(env, 'plan-checker'), [base]);
  assert.deepStrictEqual(P.workerFiles(env, 'orch-reviewer'), [base, runtime]);
  assert.deepStrictEqual(P.workerFiles(env, 'boss:orch-builder'), [base, builder, runtime]);
  // no role or an unknown role: every role file, so no rule is hidden
  assert.deepStrictEqual(P.workerFiles(env), [base, builder, runtime]);
  assert.deepStrictEqual(P.workerFiles(env, 'orch-unknown'), [base, builder, runtime]);
  for (const f of [base, builder, runtime]) assert.ok(fs.existsSync(f), f);
  fs.writeFileSync(path.join(data, 'WORKER.local.md'), 'x');
  assert.deepStrictEqual(P.workerFiles(env), [base, builder, runtime, path.join(data, 'WORKER.local.md')]);
  assert.deepStrictEqual(P.workerFiles(env, 'orch-scout'), [base, path.join(data, 'WORKER.local.md')]);
});

test('role split: every rule is reachable for its role, builder-only text stays out of the others', () => {
  const data = mk();
  const env = E({ BOSS_DATA: data });
  const text = (role) => P.workerFiles(env, role).map((f) => fs.readFileSync(f, 'utf8')).join('\n');
  const builderOnly = ['git commit --amend', 'sha_reason', 'git add -A', 'Reuse first'];
  const runtimeOnly = ['processes.js" register', 'UI checks:', 'pkill -f next-server'];
  const shared = ['Lean testing', 'Never grep/cat/head a `.env`', 'Find, then read', 'returnCapBytes', 'Orch-Attempt: <id>', 'Evidence'];
  for (const role of ['orch-builder', 'orch-reviewer', 'orch-scout', 'orch-planner', 'orch-plan-checker']) for (const k of shared) assert.ok(text(role).includes(k), `${role}: ${k}`);
  for (const k of [...builderOnly, ...runtimeOnly]) assert.ok(text('orch-builder').includes(k), `builder: ${k}`);
  for (const k of runtimeOnly) assert.ok(text('orch-reviewer').includes(k), `reviewer: ${k}`);
  for (const role of ['orch-reviewer', 'orch-scout', 'orch-planner', 'orch-plan-checker']) for (const k of builderOnly) assert.ok(!text(role).includes(k), `${role} must not load ${k}`);
  for (const role of ['orch-scout', 'orch-planner', 'orch-plan-checker']) for (const k of runtimeOnly) assert.ok(!text(role).includes(k), `${role} must not load ${k}`);
});

test('requiring paths.js prints nothing and writes nothing', () => {
  const data = mk();
  const r = spawnSync(process.execPath, ['-e', `require(${JSON.stringify(PATHS_JS)})`], {
    env: E({ BOSS_DATA: path.join(data, 'x') }), encoding: 'utf8' });
  assert.strictEqual(r.status, 0);
  assert.strictEqual(r.stdout + r.stderr, '');
  assert.deepStrictEqual(fs.readdirSync(data), []);
});

test('CLI: --json, plain and worker', () => {
  const data = mk();
  const env = E({ BOSS_DATA: data, CLAUDE_CONFIG_DIR: data });
  const run = (...a) => spawnSync(process.execPath, [PATHS_JS, ...a], { env, encoding: 'utf8' });
  let r = run('--json');
  assert.strictEqual(r.status, 0);
  const j = JSON.parse(r.stdout);
  assert.strictEqual(j.dataDir, data);
  assert.strictEqual(j.runsDir, path.join(data, 'runs'));
  assert.strictEqual(j.isScratch, false);
  assert.strictEqual(j.pluginRoot, P.pluginRoot);
  r = run();
  assert.strictEqual(r.status, 0);
  assert.ok(r.stdout.split('\n').includes(`dataDir=${data}`));
  const T = (f) => path.join(P.pluginRoot, 'templates', f);
  r = run('worker', 'orch-scout');
  assert.strictEqual(r.stdout.trim(), T('WORKER.md'));
  r = run('worker');
  assert.deepStrictEqual(r.stdout.trim().split('\n'), [T('WORKER.md'), T('WORKER.builder.md'), T('WORKER.runtime.md')]);
  fs.writeFileSync(path.join(data, 'WORKER.local.md'), 'x');
  r = run('worker', 'builder');
  assert.deepStrictEqual(r.stdout.trim().split('\n'), [T('WORKER.md'), T('WORKER.builder.md'), T('WORKER.runtime.md'), path.join(data, 'WORKER.local.md')]);
  assert.strictEqual(run('bogus').status, 2);
});

test('boss-node: runs node >= 22.13 and passes args through', () => {
  const r = spawnSync('/bin/sh', [BOSS_NODE_BIN, '-v'], { env: E({ BOSS_NODE: process.execPath, PATH: '/usr/bin:/bin' }), encoding: 'utf8' });
  assert.strictEqual(r.status, 0, r.stderr);
  assert.strictEqual(r.stdout.trim(), process.version);
  const [maj, min] = process.versions.node.split('.').map(Number);
  assert.ok(maj > 22 || (maj === 22 && min >= 5));
  const p = spawnSync('/bin/sh', [BOSS_NODE_BIN, '-p', '1+1'], { env: E({ BOSS_NODE: process.execPath, PATH: '/usr/bin:/bin' }), encoding: 'utf8' });
  assert.strictEqual(p.stdout.trim(), '2');
});

test('boss-node: skips a broken BOSS_NODE and a non-executable PATH node, exits 1 when none is usable', () => {
  const dir = mk();
  const broken = path.join(dir, 'node');
  fs.writeFileSync(broken, '#!/bin/sh\nexit 126\n', { mode: 0o755 });
  const old = path.join(dir, 'old', 'node');
  fs.mkdirSync(path.dirname(old));
  fs.writeFileSync(old, '#!/bin/sh\necho v20.1.0\n', { mode: 0o755 });
  // HOME has no .nvm; BOSS_NODE is too old (probed, so skipped) and the PATH node is not executable (`[ -x ]`, so skipped)
  const noexec = path.join(dir, 'noexec');
  fs.mkdirSync(noexec);
  fs.writeFileSync(path.join(noexec, 'node'), '#!/bin/sh\nexit 0\n', { mode: 0o644 });
  const none = spawnSync('/bin/sh', [BOSS_NODE_BIN, '-v'], { env: { HOME: dir, BOSS_NODE: old, PATH: `${noexec}:/usr/bin-nonexistent` }, encoding: 'utf8' });
  assert.strictEqual(none.status, 1);
  assert.match(none.stderr, /no Node >= 22\.13/);
  assert.strictEqual(none.stderr.trim().split('\n').length, 1, 'one stderr line');
  // a broken BOSS_NODE falls through to the next candidate
  const ok = spawnSync('/bin/sh', [BOSS_NODE_BIN, '-v'], { env: { HOME: dir, BOSS_NODE: broken, PATH: `${path.dirname(process.execPath)}:/usr/bin` }, encoding: 'utf8' });
  assert.strictEqual(ok.status, 0, ok.stderr);
  assert.strictEqual(ok.stdout.trim(), process.version);
});

test('boss-node: the PATH node is executed once with the caller\'s arguments, no `node -v` probe (H3)', () => {
  const dir = mk(), log = path.join(dir, 'calls.log');
  fs.writeFileSync(path.join(dir, 'node'), `#!/bin/sh\necho "$*" >> "${log}"\n`, { mode: 0o755 });
  const r = spawnSync('/bin/sh', [BOSS_NODE_BIN, 'script.js', 'verb'], { env: { HOME: dir, PATH: `${dir}:/usr/bin:/bin` }, encoding: 'utf8' });
  assert.strictEqual(r.status, 0, r.stderr);
  assert.strictEqual(fs.readFileSync(log, 'utf8'), 'script.js verb\n', 'one call, no -v probe');
  // BOSS_NODE stays probed: a too-old override is skipped and the PATH node runs
  const old = path.join(dir, 'old-node');
  fs.writeFileSync(old, '#!/bin/sh\necho v20.1.0\n', { mode: 0o755 });
  fs.writeFileSync(log, '');
  const r2 = spawnSync('/bin/sh', [BOSS_NODE_BIN, 'script.js'], { env: { HOME: dir, BOSS_NODE: old, PATH: `${dir}:/usr/bin:/bin` }, encoding: 'utf8' });
  assert.strictEqual(r2.status, 0, r2.stderr);
  assert.strictEqual(fs.readFileSync(log, 'utf8'), 'script.js\n');
});

test('runName: a run folder path (or a file in it) under the runs dir becomes the folder name; anything else is left for the validator', () => {
  const data = mk();
  const env = E({ BOSS_DATA: data });
  const runs = P.runsDir(env), dir = path.join(runs, 'my-run');
  fs.mkdirSync(dir, { recursive: true }); fs.writeFileSync(path.join(dir, 'PLAN.md'), 'x');
  assert.strictEqual(P.runName('my-run', env), 'my-run');
  assert.strictEqual(P.runName(dir, env), 'my-run');
  assert.strictEqual(P.runName(dir + '/', env), 'my-run');
  assert.strictEqual(P.runName(path.join(dir, 'PLAN.md'), env), 'my-run');
  assert.strictEqual(P.runName(path.join(runs, 'not-yet-there'), env), 'not-yet-there', 'a run folder that does not exist yet still maps');
  assert.strictEqual(P.runName(path.join(data, 'elsewhere', 'my-run'), env), path.join(data, 'elsewhere', 'my-run'));
  assert.strictEqual(P.runName('../x', env), '../x');
  assert.strictEqual(P.runName(path.join(dir, 'sub', 'deep'), env), path.join(dir, 'sub', 'deep'));
  assert.strictEqual(P.runName(undefined, env), undefined);
});
