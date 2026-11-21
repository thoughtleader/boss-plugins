#!/usr/bin/env node
// orchestrator-guard: PreToolUse warn-only hook for the orchestrator protocol (the main chat orchestrates and delegates).
// Small contained work runs inline in the main chat, so single calls never warn. The hook counts the
// main chat's non-exempt Edit/Write/MultiEdit/NotebookEdit/Bash/Read/Grep/Glob calls; once a run of them reaches AFTER
// (default 6, run broken by an idle gap of GAP_MS) it adds ONE soft "consider delegating the rest" reminder per session.
// Not detectable here: a tool result >~10k tokens (PreToolUse sees no result) and browser MCP use (not in the matcher).
// Subagent calls carry `agent_id` in hook stdin and are always silent.
// CLI: node orchestrator-guard.js off|on|status [--sid=<id>]   Global kill: ORCHESTRATOR_GUARD=off
// Fails open: any error -> exit 0, no output.
// Paths: every location comes from lib/paths.js. Warning slots live in <data dir>/guard-state (ORCH_GUARD_STATE overrides),
// never under the plugin dir. Extra exempt prefixes: config.json guard.exemptPaths (lib/defaults.json).
'use strict';
const fs = require('fs'), path = require('path');
const paths = require(path.join(__dirname, '..', 'lib', 'paths.js'));
const STATE = process.env.ORCH_GUARD_STATE ? path.resolve(process.env.ORCH_GUARD_STATE) : path.join(path.resolve(paths.dataDir()), 'guard-state');
const WFP_STATE = paths.wfpStateDir();
const AFTER = Math.max(1, parseInt(process.env.ORCH_GUARD_AFTER, 10) || 6), GAP_MS = 3 * 60e3, TTL_MS = 7 * 86400e3;
const TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Bash', 'Read', 'Grep', 'Glob']);
const READ_TOOLS = new Set(['Read', 'Grep', 'Glob']);
const sidOk = s => typeof s === 'string' && /^[A-Za-z0-9_-]{8,80}$/.test(s);
const f = (sid, ext) => path.join(STATE, sid + '.' + ext);
const exists = p => { try { fs.statSync(p); return true; } catch { return false; } };

const HOME = process.env.HOME || ''; // `~` and $HOME in a command are expanded the way the shell would
const CONF = path.resolve(paths.configDir()); // legacy layout: <config dir>/{wfp,hooks,orchestrator}/... (forwarder scripts keep these command paths valid)
const PLUGIN = paths.pluginRoot;
const DATA = path.resolve(paths.dataDir());
const RUNS = path.resolve(paths.runsDir());
const LEASES = path.resolve(paths.leasesDir());
const MEMORY_ROOT = path.resolve(paths.projectsDir());
const esc = s => s.replace(/[.*+?^$|(){}\[\]\\]/g, '\\$&');
const norm = s => String(s || '').replace(/\$\{?BOSS_LIB\}?/g, path.join(PLUGIN, 'lib')).replace(/\$\{?HOME\}?/g, HOME).replace(/(^|[\s'"=])~(?=\/)/g, '$1' + HOME);
// Housekeeping: a call that STARTS with one of the installed scripts (A-10: not any */ledger.js), and nothing compound after it.
// Installed = the plugin's own lib/wfp/hooks scripts, or the legacy <config dir> paths that forwarder scripts keep alive.
const HOUSEKEEPING = new RegExp('^(node )?["\']?(' + esc(PLUGIN) + '/(wfp/wfp\\.js|hooks/orchestrator-guard\\.js|lib/ledger\\.js|lib/manifest\\.js)|' + esc(CONF) + '/(wfp/wfp\\.js|hooks/orchestrator-guard\\.js|orchestrator/ledger\\.js|orchestrator/manifest\\.js))["\']?( |$)');
// Memory: the memory dir is the first operand of a plain file utility (not an interpreter), nothing compound after it.
const MEMORY_CMD = new RegExp('^(?!(?:node|bash|sh|zsh|python3?|perl|ruby|env|xargs|sudo|eval|exec)\\b)[\\w.-]+( -{1,2}[\\w=-]+)* ["\']?' + esc(MEMORY_ROOT) + '/[^/\\s\'"]+/memory(/|$|[\\s\'"])');
const MEMORY_PATH = new RegExp('^' + esc(MEMORY_ROOT) + '/[^/]+/memory(/|$)');
const META = /[;&|<>`$(){}\n\\*?]/; // anything compound, redirected, substituted or globbed is not "trivial"
const strip2 = s => s.replace(/\s+2>\/dev\/null\s*$/, '').trim(); // a trailing stderr discard is harmless
const GIT_RO = /^git( -C \S+)? (status( -{1,2}[\w-]+)*|log( \S+)*|diff --stat( \S+)*|show --stat( \S+)*|rev-parse( \S+)*|branch( (--show-current|--list|--all|--remotes|--verbose|-[arv]|-vv))*)$/;
const GIT_OUT = /\s(--output\b|-o\S*)/; // A-09: git log/diff/show can write a file
const FILE_RO = /^(cat|head|wc)( -{1,2}[\w=-]+( \d+)?)* ([^\s-]\S*)$/; // flags, then exactly one path
const PLAIN = [/^ls( \S+)*$/, /^pwd$/];
const JQ_FLAGS = new Set(['-r', '-c', '-e', '-s', '-S', '-M', '-R', '-n', '-j', '--raw-output', '--compact-output', '--exit-status', '--slurp', '--sort-keys']);
const PIPE = /^([^|]+?) \| (jq|head|wc)((?: -{1,2}[\w=-]+(?: \d+)?)*)(?: ('[^'\n]*'|[\w.\[\]-]+))?$/;

function trivial(x) {
  const c = strip2(x);
  if (META.test(c)) return false;
  if (GIT_RO.test(c)) return !GIT_OUT.test(c);
  const m = c.match(FILE_RO);
  if (m) return !path.basename(m[4].replace(/["']/g, '')).startsWith('.env'); // A-09: .env reads warn
  return PLAIN.some(r => r.test(c));
}
function allowedBash(cmd) {
  const bare = strip2(norm(cmd).trim().replace(/^rtk( proxy)? /, ''));
  if (META.test(bare)) { // only one vetted pipe survives
    const m = bare.match(PIPE);
    if (!m || !trivial(m[1])) return false;
    if (m[2] !== 'jq') return !m[4];
    return m[3].trim().split(/\s+/).filter(Boolean).every(x => !x.startsWith('-') || JQ_FLAGS.has(x));
  }
  return HOUSEKEEPING.test(bare) || MEMORY_CMD.test(bare) || trivial(bare);
}

function resolved(p) { // absolute, no '..' anywhere, else null
  const n = norm(p).trim();
  if (!n || n.includes('..') || !path.isAbsolute(n)) return null;
  return path.resolve(n);
}
const under = (r, dir) => r === dir || r.startsWith(dir + path.sep);
function extraExempt(r) { // config.json guard.exemptPaths: absolute (or ~/) prefixes; config trouble never breaks the hook
  try {
    const list = paths.loadConfig('engine').cfg.guard.exemptPaths;
    return Array.isArray(list) && list.some(x => { const e = resolved(x); return !!e && under(r, e); });
  } catch { return false; }
}
function writeExempt(p) { // Edit/Write: memory, run folders, leases/, the legacy ACTIVE.lease (still read by lease-store.js/manifest.js), guard.exemptPaths
  const r = resolved(p); if (!r) return false;
  return MEMORY_PATH.test(r) || under(r, RUNS) || under(r, LEASES) || r === path.join(DATA, 'ACTIVE.lease') || extraExempt(r);
}
function readExempt(ti) { // Read/Grep/Glob: anything under the data dir, the plugin dir, memory dirs and guard.exemptPaths
  const p = ti.file_path || ti.path || (String(ti.pattern || '').startsWith('/') ? ti.pattern : '');
  const r = resolved(p); if (!r) return false;
  return MEMORY_PATH.test(r) || under(r, DATA) || under(r, PLUGIN) || extraExempt(r);
}

function runLength(sid) { // record this call; return the length of the current run (calls with gaps < GAP_MS)
  fs.mkdirSync(STATE, { recursive: true });
  const now = Date.now(), file = f(sid, 'calls');
  fs.appendFileSync(file, now + '\n'); // append is atomic enough for parallel tool calls
  const ts = fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
  let n = 0; for (let k = ts.length - 1; k >= 0; k--) { if (k < ts.length - 1 && ts[k + 1] - ts[k] > GAP_MS) break; n++; }
  if (ts.length > 50) fs.writeFileSync(file, ts.slice(-20).join('\n') + '\n');
  return n;
}
function claimSlot(sid) { // atomic: concurrent hooks can't double-remind
  try { fs.closeSync(fs.openSync(f(sid, 'b1'), 'wx')); return true; } catch { return false; }
}
function cleanup() {
  try { for (const x of fs.readdirSync(STATE)) { const p = path.join(STATE, x); if (Date.now() - fs.statSync(p).mtimeMs > TTL_MS) fs.rmSync(p, { force: true }); } } catch {}
}

function hook(i) {
  if (String(process.env.ORCHESTRATOR_GUARD || '').toLowerCase() === 'off') return;
  if (!i || i.agent_id) return; // subagent / teammate call: never warn
  const sid = i.session_id, tool = i.tool_name;
  if (!sidOk(sid) || !TOOLS.has(tool)) return;
  const ti = i.tool_input || {};
  if (tool === 'Bash') { if (allowedBash(ti.command)) return; }
  else if (READ_TOOLS.has(tool)) { if (readExempt(ti)) return; }
  else if (writeExempt(ti.file_path || ti.notebook_path)) return;
  if (exists(f(sid, 'off')) || exists(f(sid, 'b1'))) return;
  const n = runLength(sid); if (n < AFTER) return;
  if (!claimSlot(sid)) return;
  cleanup();
  process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse',
    additionalContext: '[orchestrator] Main chat has run ' + n + ' tool calls inline in a row (last: ' + tool + '). Inline is fine for small contained work; if the rest of this task is more than a few calls, reads unknown code, or produces big output, consider delegating it to an Agent subagent with a [model/effort] label.' } }));
}

function resolveSid(argv) { // same resolution as wfp.js: --sid=, WFP_SID (set at SessionStart), newest wfp telemetry for cwd
  const a = argv.find(x => x.startsWith('--sid=')); if (a) return a.slice(6);
  if (sidOk(process.env.WFP_SID)) return process.env.WFP_SID;
  let best = null;
  try { for (const x of fs.readdirSync(WFP_STATE)) if (x.endsWith('.tel.json')) { try { const t = JSON.parse(fs.readFileSync(path.join(WFP_STATE, x), 'utf8')); if (t && t.cwd === process.cwd() && (!best || t.ts > best.ts)) best = { ts: t.ts, sid: x.replace('.tel.json', '') }; } catch {} } } catch {}
  return best && best.sid;
}
function cli(argv) {
  const cmd = argv[0] || 'status';
  if (!['off', 'on', 'status'].includes(cmd)) return console.log('orchestrator-guard off|on|status [--sid=<id>]  (global kill: ORCHESTRATOR_GUARD=off)');
  const sid = resolveSid(argv);
  if (!sidOk(sid)) return console.log('orchestrator-guard: cannot resolve session (no WFP_SID, no wfp telemetry for this cwd). Pass --sid=<id>.');
  fs.mkdirSync(STATE, { recursive: true });
  if (cmd === 'off') fs.writeFileSync(f(sid, 'off'), String(Date.now()));
  if (cmd === 'on') { fs.rmSync(f(sid, 'off'), { force: true }); fs.rmSync(f(sid, 'b1'), { force: true }); fs.rmSync(f(sid, 'calls'), { force: true }); }
  const used = exists(f(sid, 'b1')) ? 1 : 0;
  const env = String(process.env.ORCHESTRATOR_GUARD || '').toLowerCase() === 'off' ? ' (ORCHESTRATOR_GUARD=off: globally disabled)' : '';
  console.log(`orchestrator-guard: session ${sid.slice(0, 8)} ${exists(f(sid, 'off')) ? 'OFF' : 'on'}, reminders ${used}/1${env}`);
}

if (process.argv.length > 2) { try { cli(process.argv.slice(2)); } catch (e) { console.log('orchestrator-guard: ' + (e && e.message)); } }
else {
  setTimeout(() => process.exit(0), 3000).unref();
  let raw = ''; process.stdin.setEncoding('utf8');
  process.stdin.on('data', d => { if (raw.length < 4e6) raw += d; });
  process.stdin.on('end', () => { try { hook(JSON.parse(raw || '{}')); } catch {} process.exit(0); });
  process.stdin.on('error', () => process.exit(0));
}
