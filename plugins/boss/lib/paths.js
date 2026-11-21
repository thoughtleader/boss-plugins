'use strict';
// The one path and config resolver for the boss plugin (contract C1).
// Plain CommonJS, no dependencies. Nothing runs on require: every function reads the
// environment (and checks the legacy dir) at call time, so tests can change env between calls.
//
// Data dir precedence: ORCH_DIR (tests) > BOSS_DATA > legacy <configDir>/orchestrator when its
// runs/ exists > <configDir>/boss-data. CLAUDE_PLUGIN_DATA is deliberately not used: it is set
// in hook env but not in the Bash tool, so a path derived from it would split hooks from CLI calls.

const fs = require('fs');
const os = require('os');
const path = require('path');

const pluginRoot = path.resolve(__dirname, '..');

const isObj = (x) => x !== null && typeof x === 'object' && !Array.isArray(x);
const envStr = (env, k) => (typeof env[k] === 'string' && env[k] !== '' ? env[k] : '');

function configDir(env = process.env) {
  return envStr(env, 'CLAUDE_CONFIG_DIR') || path.join(os.homedir(), '.claude');
}

function dataDir(env = process.env) {
  if (envStr(env, 'ORCH_DIR')) return env.ORCH_DIR;
  if (envStr(env, 'BOSS_DATA')) return env.BOSS_DATA;
  const legacy = path.join(configDir(env), 'orchestrator');
  try { if (fs.existsSync(path.join(legacy, 'runs'))) return legacy; } catch (_) { /* fall through */ }
  return path.join(configDir(env), 'boss-data');
}

const runsDir = (env = process.env) => envStr(env, 'ORCH_RUNS_DIR') || path.join(dataDir(env), 'runs');
const leasesDir = (env = process.env) => path.join(dataDir(env), 'leases');
const wfpStateDir = (env = process.env) => envStr(env, 'WFP_STATE_DIR') || path.join(dataDir(env), 'wfp', 'state');
const wfpHandoffsDir = (env = process.env) => path.join(dataDir(env), 'wfp', 'handoffs');
const metricsDb = (env = process.env) => envStr(env, 'ORCH_METRICS_DB') || path.join(dataDir(env), 'metrics.db');
const errorsLog = (env = process.env) => path.join(dataDir(env), 'errors.log');
const progressRoot = (env = process.env) => envStr(env, 'BOSS_PROGRESS_DIR') || path.join(configDir(env), 'boss');
const projectsDir = (env = process.env) => envStr(env, 'ORCH_PROJECTS_DIR') || path.join(configDir(env), 'projects');
const sessionsDir = (env = process.env) => envStr(env, 'ORCH_SESSIONS_DIR') || path.join(configDir(env), 'sessions');

// Test mode: ORCH_DIR or ORCH_RUNS_DIR set. BOSS_DATA never counts, so a real relocated data dir keeps progress on.
const isScratch = (env = process.env) => !!(envStr(env, 'ORCH_DIR') || envStr(env, 'ORCH_RUNS_DIR'));

function readJson(file) {
  let raw;
  try { raw = fs.readFileSync(file, 'utf8'); } catch (e) { return { missing: e.code === 'ENOENT', error: `${path.basename(file)} unreadable (${e.message})` }; }
  try { return { value: JSON.parse(raw) }; } catch (e) { return { error: `${path.basename(file)} is not valid JSON (${e.message})` }; }
}

// Overlay merged one level deep: plain-object values merge key by key, anything else replaces.
function overlay(base, over) {
  const out = Object.assign({}, base);
  for (const k of Object.keys(over)) {
    out[k] = isObj(base[k]) && isObj(over[k]) ? Object.assign({}, base[k], over[k]) : over[k];
  }
  return out;
}

// loadConfig('engine') = lib/defaults.json + <data>/config.json; loadConfig('wfp') = wfp/config.default.json + <data>/wfp.json.
// Never throws: {cfg, error}. A bad overlay is ignored (cfg = base) and reported; a missing overlay is not an error.
function loadConfig(name, env = process.env) {
  let baseFile, overFile;
  if (name === 'engine') { baseFile = path.join(pluginRoot, 'lib', 'defaults.json'); overFile = path.join(dataDir(env), 'config.json'); }
  else if (name === 'wfp') { baseFile = path.join(pluginRoot, 'wfp', 'config.default.json'); overFile = path.join(dataDir(env), 'wfp.json'); }
  else return { cfg: {}, error: `unknown config "${name}"` };
  const errors = [];
  let base = {};
  const b = readJson(baseFile);
  if (b.error) errors.push(b.error);
  else if (!isObj(b.value)) errors.push(`${path.basename(baseFile)} is not a JSON object`);
  else base = b.value;
  const o = readJson(overFile);
  let cfg = base;
  if (o.error) { if (!o.missing) errors.push(`${path.basename(overFile)} ignored (${o.error}); using defaults`); }
  else if (!isObj(o.value)) errors.push(`${path.basename(overFile)} ignored (not a JSON object); using defaults`);
  else cfg = overlay(base, o.value);
  return { cfg, error: errors.length ? errors.join('; ') : null };
}

// Worker contract files, in reading order: the shared base, the role files the role needs, then the user's optional local rules.
// Role files: builder (commits, git, coverage) and runtime (UI checks, servers, background jobs). `role` accepts `builder`,
// `orch-builder` or `boss:orch-builder`. No role, or a role not listed here, gets every role file, so no rule is ever hidden.
const ROLE_FILES = {
  builder: ['WORKER.builder.md', 'WORKER.runtime.md'],
  reviewer: ['WORKER.runtime.md'],
  scout: [],
  planner: [],
  'plan-checker': [],
};
function workerFiles(env = process.env, role) {
  const dir = path.join(pluginRoot, 'templates');
  const key = typeof role === 'string' ? agentBase(role).replace(/^orch-/, '') : '';
  const extra = Object.prototype.hasOwnProperty.call(ROLE_FILES, key) ? ROLE_FILES[key] : [...new Set(Object.values(ROLE_FILES).flat())].sort();
  const files = [path.join(dir, 'WORKER.md'), ...extra.map((f) => path.join(dir, f))];
  const local = path.join(dataDir(env), 'WORKER.local.md');
  try { if (fs.existsSync(local)) files.push(local); } catch (_) { /* ignore */ }
  return files;
}

// `boss:orch-builder` -> `orch-builder`. Used by every agent-type match, so old and new ledger lines compare.
function agentBase(type) {
  if (typeof type !== 'string') return '';
  const i = type.indexOf(':');
  return i === -1 ? type : type.slice(i + 1);
}

// A run given as a path (the run folder, or a file directly inside it) becomes the folder name; a bare name passes through.
// Only a path whose run folder sits directly under the runs dir is rewritten, so '../x' and other paths stay for the validator to refuse.
function runName(v, env = process.env) {
  const s = typeof v === 'string' ? v : '';
  if (!s.includes('/')) return v;
  const real = (p) => { try { return fs.realpathSync(p); } catch (_) { return path.resolve(p); } };
  const runs = real(runsDir(env));
  let abs = path.resolve(s);
  try { if (fs.statSync(abs).isFile()) abs = path.dirname(abs); } catch (_) { /* not there: judge the path as given */ }
  abs = real(abs);
  return path.dirname(abs) === runs ? path.basename(abs) : v;
}

function all(env = process.env) {
  return {
    pluginRoot, configDir: configDir(env), dataDir: dataDir(env), runsDir: runsDir(env), leasesDir: leasesDir(env),
    wfpStateDir: wfpStateDir(env), wfpHandoffsDir: wfpHandoffsDir(env), metricsDb: metricsDb(env), errorsLog: errorsLog(env),
    progressRoot: progressRoot(env), projectsDir: projectsDir(env), sessionsDir: sessionsDir(env), isScratch: isScratch(env),
  };
}

function main(argv) {
  if (argv[0] === 'worker') { for (const f of workerFiles(process.env, argv[1])) console.log(f); return 0; }
  if (argv.length && argv[0] !== '--json') { console.error('usage: paths.js [--json] | paths.js worker [role]'); return 2; }
  const a = all();
  if (argv[0] === '--json') console.log(JSON.stringify(a, null, 2));
  else for (const k of Object.keys(a)) console.log(`${k}=${a[k]}`);
  return 0;
}

module.exports = {
  pluginRoot, configDir, dataDir, runsDir, leasesDir, wfpStateDir, wfpHandoffsDir, metricsDb, errorsLog,
  progressRoot, projectsDir, sessionsDir, isScratch, loadConfig, workerFiles, agentBase, runName, all, main,
};

if (require.main === module) process.exit(main(process.argv.slice(2)));
