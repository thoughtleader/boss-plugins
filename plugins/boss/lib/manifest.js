#!/usr/bin/env node
// manifest: per-run attempt manifest for the orchestrator (D4). Plain node, no dependencies.
// One append-only JSONL file per run: <runs>/<run>/manifest.jsonl. Events: launch, start, stop, outcome, lease, return_over_cap.
// Env overrides (tests): ORCH_DIR (leases/<run>.json, the legacy ACTIVE.lease, config.json, errors.log; ACTIVE is never read), ORCH_RUNS_DIR (default $ORCH_DIR/runs),
// ORCH_PROJECTS_DIR (default <config dir>/projects). Paths come from lib/paths.js. Never requires ledger.js: ingest runs as a child process.
// Usage: node "$BOSS_LIB/manifest.js" help   (activity --run R: read-only check for another live driver, run before a lease acquire)
'use strict';
const fs = require('fs'), path = require('path'), { spawnSync } = require('child_process');
const paths = require('./paths.js');
const ORCH_DIR = paths.dataDir();
const RUNS = paths.runsDir();
const PROJECTS = paths.projectsDir();
const DEFAULTS = (() => { try { return JSON.parse(fs.readFileSync(path.join(paths.pluginRoot, 'lib', 'defaults.json'), 'utf8')); } catch { return {}; } })(); // shipped engine defaults (lib/defaults.json); <data>/config.json overrides key by key
const { cfg, error: cfgError } = paths.loadConfig('engine'); // reported by the CLI only: require() must print nothing
const ROLES = ['planning', 'plan-check', 'discovery', 'implementation', 'diagnosis', 'review', 'default'];
const EFFORTS_ACCEPTED = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'unknown'];
const MODES = ['subagent', 'wave', 'workflow', 'agent-team', 'background-bash']; // background-bash: a council run or other shell job (no Agent notice)
const EVENT_TYPES = ['start', 'stop', 'outcome', 'return_over_cap']; // `event` CLI; launch and lease have their own writers
const COMMON = ['id', 'seq', 'type', 'ts', 'run', 'attempt', 'row', 'key', 'via', 'session'];
const VIAS = ['cli', 'pretool', 'prompt', 'sweep', 'substop', 'cap', 'handback', 'reconcile', 'ledger'];
const FINAL_PHASES = ['finished', 'failed', 'killed', 'stopped-turn-limit']; // stopped-turn-limit: "stopped at its N-turn limit" (partial result), final for launch --continue
const OPEN = ['allocated', 'started', 'stopped-unreported', 'waiting'];
const STALE_LOCK_MS = 5000;

class UsageError extends Error {}
class Refused extends Error {}  // a refused launch: exit 2, printed without the usage hint
class LockBusy extends Error {}
class LeaseConflict extends Error {} // another live session holds the lease: exit 3
class Stopped extends Error {} // the owner pressed Stop in Boss (control/cancel exists): exit 4
const fail = msg => { throw new UsageError(msg); };
const refuse = msg => { throw new Refused(msg); };

// ---- attempt IDs (design §1) -------------------------------------------------------
const ID_BODY = '([A-Za-z0-9][A-Za-z0-9._-]{0,99})#(\\d{1,4}[A-Za-z]?)\\.([1-9]\\d{0,2})';
const ID_RE = new RegExp('^' + ID_BODY + '$');
const DESC_RE = new RegExp('^\\s*(?:\\[[^\\]\\s]+\\]\\s*)?' + ID_BODY + '(?=\\s|$)');
const LABEL_RE = /^\s*\[([a-z0-9.-]+)\/([a-z]+)\]/i;
function parseAttemptId(s) { // -> {id, run, row, n} with the row lower-cased, or null
  const m = ID_RE.exec(String(s == null ? '' : s));
  if (!m || m[1].includes('..')) return null;
  const row = m[2].toLowerCase(), n = +m[3];
  return { id: `${m[1]}#${row}.${n}`, run: m[1], row, n };
}
function parseDescription(d) { // only the first token after the [m/e] label counts; [auto] gives no label values
  d = String(d == null ? '' : d);
  const m = DESC_RE.exec(d), l = LABEL_RE.exec(d), a = m ? parseAttemptId(`${m[1]}#${m[2]}.${m[3]}`) : null;
  return { attempt: a ? a.id : null, model: l ? l[1].toLowerCase() : null, effort: l ? l[2].toLowerCase() : null };
}

// ---- validation (ledger.js style) ---------------------------------------------------
const CAUSES = ['owner-tune', 'review-findings', 'failed', 'tooling']; // P31; ledger.js has the same list
const V = {
  run: v => { const raw = v; v = paths.runName(v); if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(v) || v.includes('..')) fail(`--run '${raw}': use the run folder name, or the path of a run folder under ${RUNS} (letters, digits, . _ -; no '..')`); return v; },
  row: v => { if (!/^\d{1,4}[a-z]?$/i.test(String(v))) fail(`--row '${v}': a plan row number such as 3 or 3b`); return String(v).toLowerCase(); },
  role: v => { const s = String(v).toLowerCase(); if (!ROLES.includes(s)) fail(`--role '${v}': one of ${ROLES.join(', ')}`); return s; },
  agent: v => { if (!/^[A-Za-z0-9][A-Za-z0-9:_.-]{0,79}$/.test(v)) fail(`--agent '${v}': the agent type, e.g. orch-builder`); return paths.agentBase(v) || v; }, // 'boss:orch-builder' is stored as 'orch-builder'
  model: v => { const s = String(v).toLowerCase(); if (!/^(haiku|sonnet|opus|fable|codex|council(-\d{1,2}seat)?|gpt-[a-z0-9.-]+|claude-[a-z0-9.-]+(\[1m\])?)$/.test(s)) fail(`--model '${v}': haiku, sonnet, opus, fable, codex, council or council-<N>seat, gpt-* or a full claude-* id`); return s; },
  effort: v => { const s = String(v).toLowerCase(); if (!EFFORTS_ACCEPTED.includes(s)) fail(`--effort '${v}': one of ${EFFORTS_ACCEPTED.join(', ')}`); return s; },
  mode: v => { const s = String(v).toLowerCase().replace(/\([^)]*\)/g, ' ').trim().replace(/\s+/g, '-'); if (!MODES.includes(s)) fail(`--mode '${v}': one of ${MODES.join(', ')} (case and a (...) note are ignored)`); return s; },
  text: (name, max) => v => { const s = String(v).trim(); if (!s || s.length > max || /[\r\n\t]/.test(s)) fail(`--${name} '${v}': 1-${max} characters on one line`); return s; },
  sid: v => { if (!/^[A-Za-z0-9._-]{1,128}$/.test(v) || v.includes('..')) fail(`--sid '${v}': a session id (letters, digits, . _ -)`); return v; },
  file: name => v => { const s = String(v).trim(); if (!s || /[\r\n\t]/.test(s)) fail(`--${name} '${v}': a path`); return path.resolve(s); },
  kind: v => { const s = String(v).toLowerCase(); if (!['creation', 'mechanical', 'standard'].includes(s)) fail(`--kind '${v}': creation, mechanical or standard`); return s; },
  cause: v => { const s = String(v).toLowerCase(); if (!CAUSES.includes(s)) fail(`--cause '${v}': one of ${CAUSES.join(', ')}`); return s; },
  sha: v => { if (!/^[0-9a-f]{4,40}$/i.test(v)) fail(`--base '${v}': a commit SHA (4-40 hex)`); return v.toLowerCase(); },
};
const repoV = v => { let s = V.text('repo', 80)(v); if (s.includes('/')) s = path.basename(s.replace(/\/+$/, '')); if (!s) fail(`--repo '${v}': a repo name`); return s; };
function parseFlags(argv, bools = []) {
  const o = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--') || a === '--') fail(`unexpected argument '${a}' (flags look like --name value; a stray word usually means an unquoted value that contains a space, or a flag missing its --)`);
    let name = a.slice(2), val;
    const eq = name.indexOf('=');
    if (eq >= 0) { val = name.slice(eq + 1); name = name.slice(0, eq); if (bools.includes(name)) fail(`--${name} takes no value (got '${val}')`); }
    else if (bools.includes(name)) val = true;
    else { val = argv[i + 1]; if (val === undefined || val.startsWith('--')) fail(`--${name} needs a value (${val === undefined ? 'nothing follows it' : `the next argument is the flag '${val}'`}); quote a value that starts with --`); i++; }
    if (Object.prototype.hasOwnProperty.call(o, name)) fail(`--${name} is given twice ('${o[name]}' and '${val}'); give it once`);
    o[name] = val;
  }
  return o;
}
const onlyFlags = (cmd, o, allowed) => { for (const f of Object.keys(o)) if (!allowed.includes(f)) fail(`${cmd}: unknown flag --${f} (flags: ${allowed.map(x => '--' + x).join(' ')})`); };
function sessionOf(o) { // --sid, else env WFP_SID; null when neither. An empty --sid= counts as absent (design §3 no-session path)
  if (o.sid !== undefined && o.sid !== '') return V.sid(o.sid);
  const e = process.env.WFP_SID;
  return e && /^[A-Za-z0-9._-]{1,128}$/.test(e) && !e.includes('..') ? e : null;
}

// ---- storage -----------------------------------------------------------------------
const runDir = run => path.join(RUNS, run);
const manifestFile = run => path.join(runDir(run), 'manifest.jsonl');
const sleep = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
function logError(msg) { try { fs.mkdirSync(ORCH_DIR, { recursive: true }); fs.appendFileSync(path.join(ORCH_DIR, 'errors.log'), `${new Date().toISOString()} manifest: ${msg}\n`); } catch {} }
function withLock(file, waitMs, fn) { // mkdir lock next to the file (ledger.js style), stale after 5 s, wait bounded by waitMs
  const lock = file + '.lock', t0 = Date.now();
  for (;;) {
    try { fs.mkdirSync(lock); break; } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      try { if (Date.now() - fs.statSync(lock).mtimeMs > STALE_LOCK_MS) { fs.rmdirSync(lock); continue; } } catch {}
      if (Date.now() - t0 >= waitMs) throw new LockBusy(`manifest is locked (${lock}); retry, or remove that folder if no other manifest.js is running`);
      sleep(25);
    }
  }
  try { return fn(); } finally { try { fs.rmdirSync(lock); } catch {} }
}
function readEvents(run) { // every line carries its own id; a bad line is counted, never guessed
  let txt = '';
  try { txt = fs.readFileSync(manifestFile(run), 'utf8'); } catch { return { events: [], bad: 0, lines: 0, maxSeq: 0, torn: false }; }
  const events = []; let bad = 0, lines = 0, maxSeq = 0;
  for (const l of txt.split('\n')) {
    if (!l.trim()) continue;
    lines++;
    let x; try { x = JSON.parse(l); } catch { bad++; continue; }
    if (!x || typeof x !== 'object' || Array.isArray(x) || typeof x.type !== 'string' || typeof x.id !== 'string') { bad++; continue; }
    events.push(x);
    if (Number.isSafeInteger(x.seq) && x.seq > maxSeq) maxSeq = x.seq;
  }
  return { events, bad, lines, maxSeq, torn: txt.length > 0 && !txt.endsWith('\n') };
}
function appendLocked(run, ev) { // caller holds the manifest lock
  if (!ev || typeof ev.type !== 'string' || !ev.type) throw new Error('appendEvent: type is required');
  if (typeof ev.key !== 'string' || !ev.key) throw new Error('appendEvent: key is required');
  const r = readEvents(run);
  const dup = r.events.find(x => x.type === ev.type && x.key === ev.key);
  if (dup) return { id: dup.id, dup: true, event: dup };
  const seq = 1 + Math.max(r.lines, r.maxSeq);
  const out = { id: `${run}#e${seq}`, seq, type: ev.type, ts: ev.ts || new Date().toISOString(), run, attempt: ev.attempt == null ? null : ev.attempt,
    row: ev.row == null ? null : String(ev.row).toLowerCase(), key: ev.key, via: ev.via || 'cli', session: ev.session == null ? null : ev.session };
  for (const [k, v] of Object.entries(ev)) if (!COMMON.includes(k)) out[k] = v;
  fs.appendFileSync(manifestFile(run), (r.torn ? '\n' : '') + JSON.stringify(out) + '\n'); // a torn last line stays its own (bad) line
  publishProgress(run, r.events.concat([out]), { acquired: out.type === 'lease' && ['acquire', 'steal'].includes(out.action) }); // same lock, never throws
  return { id: out.id, dup: false, event: out };
}
// ---- boss.progress/1 publisher (<progress root>/PROGRESS.md) --------------------------------------------------------
// The snapshot is a cache of the manifest: every append replays all events into <root>/runs/orchestrator/<run>/progress.json while
// the caller still holds the manifest lock (so one publisher, no read-modify-write by hooks). Failure never reaches the caller: it is
// swallowed and logged once per process. Root: paths.progressRoot() ($BOSS_PROGRESS_DIR, else <config dir>/boss). Scratch ORCH_DIR / ORCH_RUNS_DIR without BOSS_PROGRESS_DIR: off, so tests never fill the real registry.
let progressLibs = null, progressWarned = false;
const progressBase = () => process.env.BOSS_PROGRESS_DIR || undefined;
const progressOn = () => cfg.progress !== false && process.env.ORCH_PROGRESS !== 'off' && (!!process.env.BOSS_PROGRESS_DIR || !paths.isScratch()); // a scratch ORCH_DIR / ORCH_RUNS_DIR (tests) publishes only with an explicit BOSS_PROGRESS_DIR
const sessionsDir = () => paths.sessionsDir();
function loadProgressLibs() {
  if (!progressLibs) progressLibs = { PM: require(path.join(__dirname, 'progress-mirror.js')), PH: require(process.env.BOSS_PROGRESS_LIB || path.join(__dirname, 'progress.mjs')) };
  return progressLibs;
}
function publishProgress(run, events, opts = {}) { // -> {file, snap} | null
  if (!progressOn()) return null;
  try {
    const { PM, PH } = loadProgressLibs(), base = progressBase(), dir = PH.runDir('orchestrator', run, base);
    const planFile = path.join(runDir(run), 'PLAN.md');
    let plan = null; try { const st = fs.statSync(planFile); plan = { txt: fs.readFileSync(planFile, 'utf8'), fm: frontmatter(planFile) || {}, mtimeMs: st.mtimeMs }; } catch {}
    let attentionFile = null; try { attentionFile = JSON.parse(fs.readFileSync(path.join(runDir(run), 'attention.json'), 'utf8')); } catch {}
    const prev = PH.readSnapshot(dir);
    const ownerFor = sid => { // the lease holder's session -> its pid (sessions/*.json, via the one adapter); a still-valid previous answer is reused
      if (!sid) return null;
      const po = prev && prev.owner;
      if (po && po.session_id === sid && po.pid && po.start_ticks != null && PH.procStartTicks(po.pid) === po.start_ticks) return po;
      const s = PH.sessionState({ session_id: sid }, sessionsDir());
      return { session_id: sid, pid: s ? s.pid : null, start_ticks: s ? PH.procStartTicks(s.pid) : null, inbox: null };
    };
    const snap = PM.buildSnapshot(run, events, { runDir: runDir(run), plan, cfg, parseAttemptId, attentionFile, ownerFor, now: Date.now(), writer: PH.writerIdentity('oneshot'),
      resultFor: (row, launch, id, n) => resultFor(run, row, launch, id, n) });
    const file = PH.writeSnapshot(snap, { base });
    if (opts.acquired) { try { PH.prune({ base }); } catch (e) { logError(`progress prune: ${e && e.message}`); } } // retention: writers only, on a live acquire (never on replay)
    return { file, snap };
  } catch (e) {
    if (opts.strict) throw e;
    if (!progressWarned) { progressWarned = true; logError(`progress: snapshot for ${run} not written: ${e && e.message}`); }
    return null;
  }
}
// appendEvent(run, ev, {hook, waitMs, create}): dedupe by type+key, seq/id assignment, one appendFileSync under the lock.
// hook: true -> a busy lock or a missing run folder skips the write and logs one errors.log line ({skipped: true}).
function appendEvent(run, ev, opts = {}) {
  const hook = !!opts.hook, waitMs = opts.waitMs != null ? opts.waitMs : hook ? cfg.lockWaitMsHook : cfg.lockWaitMsCli;
  if (!fs.existsSync(runDir(run))) {
    if (!opts.create) { logError(`run folder missing: ${runDir(run)} (${ev && ev.type} ${ev && ev.key} not written)`); return { skipped: true, reason: 'no-run-folder' }; }
    fs.mkdirSync(runDir(run), { recursive: true });
  }
  try { return withLock(manifestFile(run), waitMs, () => appendLocked(run, ev)); } catch (e) {
    if (hook && e instanceof LockBusy) { logError(`lock busy, skipped ${ev.type} ${ev.key} in ${run}`); return { skipped: true, reason: 'lock-busy' }; }
    throw e;
  }
}
function readLedger(run) { // -> lines of <run>/ledger.jsonl (bad lines skipped)
  let txt = ''; try { txt = fs.readFileSync(path.join(runDir(run), 'ledger.jsonl'), 'utf8'); } catch { return []; }
  const out = [];
  for (const l of txt.split('\n')) { if (!l.trim()) continue; try { const x = JSON.parse(l); if (x && x.run != null && x.row != null) out.push(x); } catch {} }
  return out;
}
function ledgerMaxN(run, row) { // a line's n: its explicit id suffix, else its ordinal within the row in file order (row 4's rule)
  let ord = 0, max = 0;
  for (const x of readLedger(run)) {
    if (x.run !== run || String(x.row).toLowerCase() !== row) continue;
    ord++;
    const a = typeof x.id === 'string' ? parseAttemptId(x.id) : null;
    const n = a && a.run === run && a.row === row ? a.n : ord;
    if (n > max) max = n;
  }
  return max;
}

// ---- folding -----------------------------------------------------------------------
const bySeq = (a, b) => (a.seq || 0) - (b.seq || 0);
const last = arr => arr.length ? arr[arr.length - 1] : null;
function groupByAttempt(events) {
  const m = new Map();
  for (const e of events) {
    if (!e.attempt) continue;
    if (!m.has(e.attempt)) m.set(e.attempt, { id: e.attempt, launches: [], starts: [], stops: [], outcomes: [], caps: [], voids: [] });
    const g = m.get(e.attempt), k = { launch: 'launches', start: 'starts', stop: 'stops', outcome: 'outcomes', return_over_cap: 'caps', void: 'voids' }[e.type];
    if (k) g[k].push(e);
  }
  for (const g of m.values()) for (const k of ['launches', 'starts', 'stops', 'outcomes', 'caps', 'voids']) g[k].sort(bySeq);
  return m;
}
const finalStops = g => g.stops.filter(s => s.final === true);
// The stop that speaks for an attempt. A turn-limit stop (phase stopped-turn-limit) is a partial result: it speaks only when it is strictly
// LATER than every real stop of the attempt, by its own ts (an equal ts is not later; seq only when a ts is missing). Ingest order must not decide: a turn-limit notice that
// is swept or renoticed late gets a higher seq than the finished notice it preceded, and must not override it. Real stops keep seq order.
// `stops` is in seq order (groupByAttempt); -> the stop or null. (progress-mirror.js keeps a copy of this rule: it is a pure module.)
const TURN_LIMIT = 'stopped-turn-limit';
const laterStop = (a, b) => { const ta = Date.parse(a.ts), tb = Date.parse(b.ts); return Number.isFinite(ta) && Number.isFinite(tb) ? ta > tb : (a.seq || 0) > (b.seq || 0); };
const pickStop = stops => {
  const real = stops.filter(s => s.phase !== TURN_LIMIT);
  return last(stops.filter(s => s.phase === TURN_LIMIT && real.every(r => laterStop(s, r)))) || last(real);
};
// A voided attempt was allocated (a launch event) but never launched, and `void --attempt` said so. It is left out of fold's attempts, reconcile,
// launch's reuse rule and the progress items. An attempt that later shows a start or a stop is real again, so the void no longer counts.
const isVoided = g => g.voids.length > 0 && !g.starts.length && !g.stops.length;
// The agent finished its turn but still owns background work ("is waiting on its own background work", status completed): the harness
// sends only this interim notice. It counts as the final stop when no real final notice came and no start is newer (same rule as
// progress-mirror.js attemptState); a later true final notice still wins (callers try finalStops first). -> the stop or null
const idleStop = g => { const w = last(g.stops.filter(s => s.phase === 'waiting' && s.status === 'completed')); return w && (!g.starts.length || (last(g.starts).seq || 0) < (w.seq || 0)) ? w : null; };
const noticeStops = g => g.stops.filter(s => s.phase !== 'agent-stopped');
function metaFor(start) { // the meta.json under <dirname(transcript_path)>/<session>/subagents/ whose toolUseId matches, or null
  if (!start || !start.transcript_path || !start.tool_use_id || !start.session) return null;
  const sess = String(start.session);
  if (!/^[A-Za-z0-9._-]{1,128}$/.test(sess) || sess.includes('..')) return null;
  const dir = path.join(path.dirname(String(start.transcript_path)), sess, 'subagents');
  let names; try { names = fs.readdirSync(dir); } catch { return null; }
  for (const f of names) {
    if (!/^agent-.*\.meta\.json$/.test(f)) continue;
    try { const m = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')); if (m && m.toolUseId === start.tool_use_id) return Object.assign({ _file: path.join(dir, f) }, m); } catch {}
  }
  return null;
}
function stateOf(g) {
  const notice = noticeStops(g);
  if (notice.length) return String(pickStop(notice).phase || 'finished');
  if (g.stops.length) return 'stopped-unreported';
  return g.starts.length ? 'started' : 'allocated';
}
function rowCmp(a, b) { // row number, then letter
  const pa = /^(\d+)([a-z]?)$/.exec(String(a)) || [0, 1e9, String(a)], pb = /^(\d+)([a-z]?)$/.exec(String(b)) || [0, 1e9, String(b)];
  return (+pa[1] - +pb[1]) || String(pa[2]).localeCompare(String(pb[2]));
}
// fold(run, {row, now, sid}) -> {run, lease, attempts, unplanned, bad_lines}; attempts in show order (A-16: open first, oldest first)
function fold(run, opts = {}) {
  const { events, bad } = readEvents(run), now = opts.now || Date.now(), row = opts.row != null ? String(opts.row).toLowerCase() : null;
  const groups = groupByAttempt(events), ledgerIds = new Set(readLedger(run).map(x => x.id).filter(Boolean));
  const attempts = [], voided = [];
  for (const g of groups.values()) {
    if (isVoided(g)) { voided.push(g.id); continue; }
    const a = parseAttemptId(g.id), launch = last(g.launches), anyEv = g.launches[0] || g.starts[0] || g.stops[0] || g.outcomes[0] || g.caps[0];
    const r = a ? a.row : launch ? launch.row : anyEv.row;
    if (row && String(r) !== row) continue;
    const state = stateOf(g), fin = finalStops(g), ledger = ledgerIds.has(g.id), open = OPEN.includes(state);
    const since = (last(g.starts) || launch || anyEv).ts, sinceMs = Date.parse(since);
    const flags = [];
    if (!launch || g.starts.some(s => s.unknown_attempt)) flags.push('unknown-attempt');
    if (g.starts.some(s => s.id_in_brief === false)) flags.push('no-brief-line');
    if (g.starts.some(s => s.foreign_lease)) flags.push('foreign-lease');
    if (g.caps.length) flags.push(`over-cap:${last(g.caps).action}`);
    if (fin.filter(x => x.phase !== TURN_LIMIT).length >= 2) flags.push('renotice'); // a turn-limit notice is a partial result, not a repeat of the finish
    if (fin.length && !ledger) flags.push('no-ledger-line');
    if (launch && launch.continues) flags.push(`continuation-of ${launch.continues}`);
    attempts.push({ id: g.id, row: r, n: a ? a.n : launch ? launch.n : null, state,
      age_min: open && Number.isFinite(sinceMs) ? Math.max(0, Math.floor((now - sinceMs) / 60000)) : null, _since: Number.isFinite(sinceMs) ? sinceMs : 0,
      launch, starts: g.starts, stops: g.stops, outcome: last(g.outcomes), caps: g.caps, ledger, flags });
  }
  attempts.sort((x, y) => {
    const ox = OPEN.includes(x.state), oy = OPEN.includes(y.state);
    if (ox !== oy) return ox ? -1 : 1;
    if (ox) return x._since - y._since;
    return rowCmp(x.row, y.row) || (x.n || 0) - (y.n || 0);
  });
  for (const x of attempts) delete x._since;
  const unplanned = row ? [] : events.filter(e => e.type === 'start' && !e.attempt).sort(bySeq).map(s => {
    const stops = events.filter(e => e.type === 'stop' && !e.attempt && s.tool_use_id && e.tool_use_id === s.tool_use_id).sort(bySeq);
    const notice = stops.filter(e => e.phase !== 'agent-stopped');
    return { tool_use_id: s.tool_use_id || null, ts: s.ts, agent: s.agent || null, description: s.description || null,
      state: notice.length ? String(last(notice).phase) : stops.length ? 'stopped-unreported' : 'started', start: s, stops };
  });
  const sid = opts.sid || null;
  const out = { run, lease: leaseSummary(leaseState(sid, run, now), sid), attempts, unplanned, bad_lines: bad };
  if (voided.length) out.voided = voided.sort(); // only when there is one: the shape stays as it was for runs with none
  return out;
}

// Hook hot-path helpers (orch-attempts.js). Each gives exactly the answer of the fold()/readEvents() expression in its comment and
// skips the work that answer does not need (the ledger read, the lease read, the unplanned list, flags and sorting of every attempt).
// openAttemptIds(run, {now, minAgeMs, maxAgeMs}) === fold(run, {now}).attempts.filter(a => a.launch && a.age_min != null &&
//   a.age_min * 60000 >= minAgeMs && a.age_min * 60000 < maxAgeMs && !a.outcome).map(a => a.id)   (same ids, same order: oldest `since` first, ties in first-event order)
// An attempt with an outcome event was closed by the owner (ledger.js close): it is not open whatever state its last notice left (a `waiting` notice stays in OPEN), so it is no gap candidate.
// Two opt-in filters for the compaction-gap hint, both off unless asked (so the equality above holds without them), both from the events already read:
//   skipSuperseded: an attempt is left out when a later non-voided attempt exists on its row and subtask (higher n, or a launch whose `continues` names it): the owner retried or
//     continued it. Attempts of other subtasks of the row are parallel work, not retries, and stay.
//   inflightSid / inflightMs: an attempt whose start (else launch) was written by session inflightSid, younger than inflightMs, is still running in that chat and is left out.
function openAttemptIds(run, opts = {}) {
  const { events } = readEvents(run), now = opts.now || Date.now(), out = [], groups = groupByAttempt(events);
  let maxN = null, continued = null;
  const slot = (g, a, l) => `${a ? a.row : l.row}\u0000${l.subtask || ''}`; // the row and subtask an attempt belongs to
  if (opts.skipSuperseded) {
    maxN = new Map(); continued = new Set();
    for (const g of groups.values()) {
      const l = last(g.launches); if (!l || isVoided(g)) continue;
      const a = parseAttemptId(g.id), k = slot(g, a, l), n = a ? a.n : l.n;
      if (Number.isFinite(n) && n > (maxN.get(k) || 0)) maxN.set(k, n);
      if (l.continues) continued.add(l.continues);
    }
  }
  for (const g of groups.values()) {
    const launch = last(g.launches); if (!launch || isVoided(g) || g.outcomes.length || !OPEN.includes(stateOf(g))) continue; // fold() leaves voided attempts out; a closed attempt (outcome event) is not open
    const first = last(g.starts) || launch, sinceMs = Date.parse(first.ts); if (!Number.isFinite(sinceMs)) continue; // fold: age_min null
    const age = Math.max(0, Math.floor((now - sinceMs) / 60000)) * 60000;
    if (!(age >= opts.minAgeMs && age < opts.maxAgeMs)) continue;
    if (maxN) {
      const a = parseAttemptId(g.id);
      if (continued.has(g.id) || (a && a.n < (maxN.get(slot(g, a, launch)) || 0))) continue;
    }
    if (opts.inflightSid && first.session === opts.inflightSid && age < opts.inflightMs) continue;
    out.push({ id: g.id, sinceMs });
  }
  return out.sort((x, y) => x.sinceMs - y.sinceMs).map(x => x.id); // Array#sort is stable, as in fold()
}
// hasLaunch(run, id) === readEvents(run).events.some(e => e.type === 'launch' && e.attempt === id)
// For an id made of letters, digits and . _ # - (every id parseAttemptId returns), JSON.stringify never escapes a character, so a line
// whose parsed attempt is that id holds it verbatim: other lines are skipped without JSON.parse. Any other id: every line is parsed.
function hasLaunch(run, id) {
  let txt = ''; try { txt = fs.readFileSync(manifestFile(run), 'utf8'); } catch { return false; }
  const plain = typeof id === 'string' && /^[A-Za-z0-9._#-]+$/.test(id);
  for (const l of txt.split('\n')) {
    if (!l.trim() || (plain && !l.includes(id))) continue;
    let x; try { x = JSON.parse(l); } catch { continue; }
    if (!x || typeof x !== 'object' || Array.isArray(x) || typeof x.type !== 'string' || typeof x.id !== 'string') continue; // readEvents counts these as bad
    if (x.type === 'launch' && x.attempt === id) return true;
  }
  return false;
}

// ---- ingest child (design §7 step 0(b): launch --continue and reconcile) ---------------------------------------------
// runIngest(transcript, sid, run) -> {status: ok|unavailable|failed, lines}. The OD-3 gate is ledger.js's: the child always gets
// --sid=<sid or ''> explicitly, so it never re-derives a sid from the inherited WFP_SID, and --run R only when sid is set and
// no other session holds R live (ledger.js checks that again itself). With no sid ledger.js writes only into the notice's
// own run, and only while no session holds that run live (B1).
function runIngest(transcript, sid, run) {
  const args = [path.join(__dirname, 'ledger.js'), 'ingest', '--transcript', transcript, '--tail-bytes', 'all', `--sid=${sid || ''}`];
  if (sid && run && RUN_OK(run) && fs.existsSync(runDir(run)) && leaseState(sid, run).state !== 'other') args.push('--run', run);
  const r = spawnSync(process.execPath, args, { encoding: 'utf8', timeout: 60000 });
  if (r.status === 0) return { status: 'ok', lines: String(r.stdout || '').split('\n').filter(l => l.trim()) };
  const err = String(r.stderr || '');
  if (r.status === 2 && /unknown command/.test(err)) return { status: 'unavailable', lines: ['ingest unavailable'] };
  const first = err.split('\n').find(l => l.trim()) || (r.error && r.error.message) || `exit ${r.status}`;
  return { status: 'failed', lines: [`ingest failed: ${first}`] };
}
const exists = f => { try { return fs.statSync(f).isFile(); } catch { return false; } };
function transcriptFor(groups, id) { // P's latest start; else up P's continues chain; else $ORCH_PROJECTS_DIR/*/<session of P's launch>.jsonl
  const seen = new Set();
  for (let cur = id; cur && !seen.has(cur);) {
    seen.add(cur);
    const g = groups.get(cur); if (!g) break;
    const st = g.starts.filter(s => s.transcript_path && exists(String(s.transcript_path)));
    if (st.length) return String(last(st).transcript_path);
    const L = last(g.launches); cur = L && L.continues;
  }
  const P = groups.get(id), L = P && last(P.launches), sess = L && L.session;
  if (sess && /^[A-Za-z0-9._-]{1,128}$/.test(sess) && !sess.includes('..')) {
    let dirs = []; try { dirs = fs.readdirSync(PROJECTS); } catch {}
    for (const d of dirs.sort()) { const f = path.join(PROJECTS, d, `${sess}.jsonl`); if (exists(f)) return f; }
  }
  return null;
}

// ---- lease (per-run lease design D-1 to D-10): one lease per run in $ORCH_DIR/leases/<run>.json; ACTIVE itself is never written --
// Reads go through lease-store.js. Writers take the run's lock (leases/<run>.json.lock) and re-read strictly under it: a per-run
// file that exists but is not a readable lease is an error, never "no lease". The legacy global $ORCH_DIR/ACTIVE.lease counts for
// its own run only (D-2); the first write on that run moves it to leases/<run>.json (legacy lock first, then the run's lock, never
// the other way round). New code never writes ACTIVE.lease.
let leaseStore = null; // loaded on first use, so commands that never touch a lease (event, ledger.js ingest) work without it
const LS = () => leaseStore || (leaseStore = require(path.join(__dirname, 'lease-store.js')));
const LEGACY = path.join(ORCH_DIR, 'ACTIVE.lease');
const LEASES = path.join(ORCH_DIR, 'leases');
const runLeaseFile = run => path.join(LEASES, `${run}.json`);
const SID_OK = s => typeof s === 'string' && /^[A-Za-z0-9._-]{1,128}$/.test(s) && !s.includes('..'); // same rules as lease-store.js
const RUN_OK = s => typeof s === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(s) && !s.includes('..');
const isoAt = ms => new Date(ms).toISOString();
const hhmm = iso => { const t = Date.parse(iso); return Number.isFinite(t) ? new Date(t).toISOString().slice(11, 16) + ' UTC' : '?'; };
const sid8 = s => String(s || '').slice(0, 8);
const ttlSec = () => { const t = +cfg.leaseTtlSec; return Number.isFinite(t) && t > 0 ? Math.round(t) : 7200; };
const renewBelowSec = () => { const t = +cfg.leaseRenewWhenLeftSec; return Number.isFinite(t) ? t : 3600; };
// leaseStateOf(lease-store result | null, sid, now) -> {state: mine|other|expired|none, lease, own (held by sid, live or expired), live,
// left_sec, legacy, file}. With no sid a live lease reads 'other' (mine cannot be told apart); callers that print it say so.
function leaseStateOf(r, sid, now) {
  if (!r || !r.lease) return { state: 'none', lease: null, own: false, live: false, left_sec: null, legacy: false, file: null };
  const lease = r.lease, exp = Date.parse(lease.expires), live = LS().isLive(lease, now), own = !!sid && lease.session === sid;
  return { state: !live ? 'expired' : own ? 'mine' : 'other', lease, own, live, left_sec: Number.isFinite(exp) ? Math.round((exp - now) / 1000) : null,
    legacy: !!r.legacy, file: r.file || null };
}
// leaseState(sid, run, now) -> that run's state (D-3): its per-run file, else the legacy file when it names this run. Read-only.
function leaseState(sid, run, now = Date.now()) { return leaseStateOf(RUN_OK(run) ? LS().readRunLease(ORCH_DIR, run) : null, sid, now); }
// lockedState: leaseState for a writer holding the run's lock. Absent means ENOENT/ENOTDIR only; anything else is an error.
function lockedState(sid, run) {
  const now = Date.now(), r = LS().readRunLease(ORCH_DIR, run), file = runLeaseFile(run);
  if (r && !r.legacy) return Object.assign(leaseStateOf(r, sid, now), { now });
  let code = null; try { fs.lstatSync(file); } catch (e) { code = e && e.code; }
  if (code !== 'ENOENT' && code !== 'ENOTDIR') throw new Error(`lease file ${file} exists but is not a readable lease for run ${run}${code ? ` (${code})` : ''}; fix it, or remove it if no session holds that run`);
  return Object.assign(leaseStateOf(r, sid, now), { now });
}
const leaseHeldMsg = L => `lease: held by ${sid8(L.session)} until ${hhmm(L.expires)} (run ${L.run}); wait for expiry, or ask the owner and rerun with --force`;
const legacyHeldMsg = L => `lease: held by ${sid8(L.session)} until ${hhmm(L.expires)} (run ${L.run}) through the legacy ACTIVE.lease; --force does not take a live legacy lease: wait for expiry, or have that session renew or release it`;
function leaseSummary(st, sid) {
  const L = st.lease;
  return { state: st.state, holder: L ? L.session : null, run: L ? L.run || null : null, path: L ? L.path || null : null,
    acquired: L ? L.acquired || null : null, renewed: L ? L.renewed || null : null, expires: L ? L.expires || null : null, left_sec: st.left_sec, sid: sid || null,
    legacy: !!st.legacy };
}
const tmpFor = file => `${file}.tmp-${process.pid}-${Math.random().toString(36).slice(2, 8)}`; // never ends in .json: lease-store skips it
function createLease(file, obj) { // D-4: O_EXCL-style create (tmp + link); false when the file already exists
  const tmp = tmpFor(file);
  try {
    fs.writeFileSync(tmp, JSON.stringify(obj) + '\n');
    try { fs.linkSync(tmp, file); return true; } catch (e) { if (e.code === 'EEXIST') return false; throw e; }
  } finally { try { fs.unlinkSync(tmp); } catch {} }
}
function replaceLease(file, obj) { // renew, steal, expired takeover: tmp + rename
  const tmp = tmpFor(file);
  try { fs.writeFileSync(tmp, JSON.stringify(obj) + '\n'); fs.renameSync(tmp, file); } catch (e) { try { fs.unlinkSync(tmp); } catch {} throw e; }
}
const runLock = (run, waitMs, fn) => { fs.mkdirSync(LEASES, { recursive: true }); return withLock(runLeaseFile(run), waitMs, fn); };
function leaseEvent(run, action, sid, extra, hook) { // a lease event goes to the named run's manifest; a missing run folder is logged, not fatal
  if (!RUN_OK(run)) return null;
  const ts = new Date().toISOString();
  try {
    return appendEvent(run, Object.assign({ type: 'lease', key: `${action}:${ts}`, ts, attempt: null, row: null, via: 'cli', session: sid || null, action,
      holder: null, prev_holder: null, expires: null, ttl_sec: null }, extra), { hook: !!hook });
  } catch (e) { logError(`lease event ${action} for ${run} not written: ${e.message}`); return null; }
}
// D-2 (a)(b): move a legacy lease for `run` into leases/<run>.json before a write on that run. Its holder migrates on any write;
// another session only on takeover (acquire, launch, release) of an expired legacy lease. A live legacy lease of another session,
// or a malformed one, is left alone. The process whose link-create succeeds appends the one migrate event, after both locks.
function migrateLegacy(run, sid, by, waitMs, hook, takeover) {
  const r0 = LS().readRunLease(ORCH_DIR, run);
  if (!r0 || !r0.legacy) return false;
  const may = L => L.session === sid || (!!takeover && !LS().isLive(L, Date.now()));
  if (!may(r0.lease)) return false;
  const m = withLock(LEGACY, waitMs, () => {
    let bytes; try { bytes = fs.readFileSync(LEGACY, 'utf8'); } catch (e) { if (e.code === 'ENOENT' || e.code === 'ENOTDIR') return null; throw e; }
    let L; try { L = JSON.parse(bytes); } catch { return null; }
    if (!L || typeof L !== 'object' || Array.isArray(L) || L.run !== run || !SID_OK(L.session) || !may(L)) return null;
    return runLock(run, waitMs, () => {
      const file = runLeaseFile(run);
      let code = null; try { fs.lstatSync(file); } catch (e) { code = e && e.code; }
      const created = code === 'ENOENT' ? createLease(file, L) : false; // an existing per-run file already shadows the legacy lease
      let now; try { now = fs.readFileSync(LEGACY, 'utf8'); } catch { now = null; }
      if (now === bytes) fs.unlinkSync(LEGACY); // compare, then unlink: never a newer legacy file
      return created ? L : null;
    });
  });
  if (m) leaseEvent(run, 'migrate', sid, { holder: m.session, prev_holder: null, acquired: m.acquired || null, expires: m.expires || null, ttl_sec: m.ttl_sec || null, by }, hook);
  return !!m;
}
const RETRY = Symbol('retry');
// withRunLease: migrate first (D-2 f), then fn(state) under the run's lock. A legacy lease that should have moved (it appeared after
// the migration step) sends the caller back to migrate; the legacy lock is never taken inside the run's lock.
function withRunLease(run, sid, by, opts, fn) {
  const waitMs = opts.hook ? cfg.lockWaitMsHook : cfg.lockWaitMsCli;
  for (let i = 0; i < 3; i++) {
    migrateLegacy(run, sid, by, waitMs, opts.hook, opts.takeover);
    const out = runLock(run, waitMs, () => {
      const st = lockedState(sid, run);
      if (st.legacy && (st.own || (opts.takeover && !st.live))) return RETRY;
      return fn(st);
    });
    if (out !== RETRY) return out;
  }
  throw new Error(`lease for run ${run}: the legacy ACTIVE.lease could not be moved to ${runLeaseFile(run)}; retry`);
}
// acquireRun -> {refused, prev, legacy} | {mine, prev} (keepMine) | {action: acquire|renew|steal, L, prev}. D-4: none -> link-create
// (EEXIST: re-evaluate). keepMine (launch, D-6): a lease this session already holds live is left as it is (renewIfMine renews it when low).
function acquireRun(run, sid, force, dir, by, keepMine) {
  const ttl = ttlSec();
  return withRunLease(run, sid, by, { takeover: true }, st0 => {
    let st = st0;
    for (let i = 0; i < 3; i++) {
      const now = st.now, prev = st.lease;
      if (st.state === 'other' && st.legacy) return { refused: true, prev, legacy: true }; // D-2 (c)(d): even with --force
      if (st.state === 'other' && !force) return { refused: true, prev };
      if (st.state === 'mine' && keepMine) return { mine: true, prev };
      const renew = st.state === 'mine', action = st.state === 'other' ? 'steal' : renew ? 'renew' : 'acquire';
      const L = { v: 1, run, path: dir, session: sid, acquired: renew && prev.acquired ? prev.acquired : isoAt(now), renewed: isoAt(now), expires: isoAt(now + ttl * 1000), ttl_sec: ttl };
      if (st.state !== 'none') { replaceLease(runLeaseFile(run), L); return { action, L, prev }; }
      if (createLease(runLeaseFile(run), L)) return { action, L, prev };
      st = lockedState(sid, run);
    }
    throw new Error(`lease for run ${run}: could not create ${runLeaseFile(run)}; retry`);
  });
}
// renewRun -> {L} | {refused, st} | {skipped} (belowSec given and more than that left)
function renewRun(run, sid, by, hook, belowSec) {
  return withRunLease(run, sid, by, { hook }, st => {
    if (!st.own) return { refused: true, st };
    if (belowSec != null && st.left_sec != null && st.left_sec >= belowSec) return { skipped: true };
    const ttl = ttlSec(), L = Object.assign({}, st.lease, { renewed: isoAt(st.now), expires: isoAt(st.now + ttl * 1000), ttl_sec: ttl });
    replaceLease(runLeaseFile(run), L); return { L };
  });
}
// releaseRun -> {none} | {P} | {refused, P, legacy} | {skipped, P} (ownOnly and no longer this session's)
function releaseRun(run, sid, force, ownOnly) {
  return withRunLease(run, sid, 'release', { takeover: !ownOnly }, st => {
    if (!st.lease) return { none: true };
    if (ownOnly && !st.own) return { skipped: true, P: st.lease };
    if (st.state === 'other' && st.legacy) return { refused: true, P: st.lease, legacy: true };
    if (st.state === 'other' && !force) return { refused: true, P: st.lease };
    fs.unlinkSync(runLeaseFile(run)); return { P: st.lease };
  });
}
// renewIfMine(sid, {hook, run, by}): renew each lease sid holds (live, or expired and not since taken) with less than
// leaseRenewWhenLeftSec left; with run, that run only. Never throws, never prints: launch and the hook modes call it fail-open.
function renewIfMine(sid, opts = {}) {
  try {
    if (!SID_OK(sid)) return { renewed: false };
    const lim = renewBelowSec(), by = opts.by || (opts.hook ? 'hook' : 'renew');
    const runs = opts.run !== undefined ? (RUN_OK(opts.run) ? [opts.run] : []) : LS().heldBy(ORCH_DIR, sid).map(x => x.run);
    const done = [];
    for (const run of runs) {
      try {
        const st0 = leaseState(sid, run);
        if (!st0.own || (st0.left_sec != null && st0.left_sec >= lim)) continue;
        const r = renewRun(run, sid, by, !!opts.hook, lim);
        if (!r || !r.L) continue;
        leaseEvent(run, 'renew', sid, { holder: sid, expires: r.L.expires, ttl_sec: r.L.ttl_sec }, opts.hook);
        done.push({ run, expires: r.L.expires });
      } catch (e) { logError(`renewIfMine ${run}: ${e && e.message}`); }
    }
    return done.length ? { renewed: true, expires: done[0].expires, runs: done.map(x => x.run) } : { renewed: false };
  } catch (e) { logError(`renewIfMine: ${e && e.message}`); return { renewed: false, error: String(e && e.message) }; }
}
// heldLeases(sid) -> [{run, file, state, lease, own, live, left_sec, legacy}] for every lease sid holds, newest renewed first. Read-only.
function heldLeases(sid) {
  try { const now = Date.now(); return LS().heldBy(ORCH_DIR, sid).map(x => Object.assign({ run: x.run }, leaseStateOf(x, sid, now))); } catch { return []; }
}
function needSid(cmd, o) { const s = sessionOf(o); if (!s) fail(`lease ${cmd}: needs a session id (--sid S, or env WFP_SID)`); return s; }
const refusedEvent = (run, sid, P) => leaseEvent(run, 'refused', sid, { holder: P.session, prev_holder: P.session, expires: P.expires || null, ttl_sec: P.ttl_sec || null });
function leaseAcquire(o) {
  onlyFlags('lease acquire', o, ['run', 'sid', 'force']);
  if (o.run === undefined) fail('lease acquire: needs --run');
  const run = V.run(o.run), sid = needSid('acquire', o), dir = path.resolve(runDir(run));
  if (!fs.existsSync(dir)) fail(`lease acquire: run folder ${dir} not found`);
  const r = acquireRun(run, sid, !!o.force, dir, 'acquire');
  if (r.refused) { refusedEvent(run, sid, r.prev); throw new LeaseConflict(r.legacy ? legacyHeldMsg(r.prev) : leaseHeldMsg(r.prev)); }
  const prevHolder = r.prev && r.prev.session !== sid ? r.prev.session : null;
  leaseEvent(run, r.action, sid, { holder: sid, prev_holder: prevHolder, expires: r.L.expires, ttl_sec: r.L.ttl_sec });
  console.log(`lease: ${r.action} by ${sid8(sid)} until ${hhmm(r.L.expires)} (run ${run})${prevHolder ? `; previous holder ${sid8(prevHolder)}` : ''}`);
}
function leaseRenew(o) { // without --run: every lease this session holds (exit 3 when it holds none); with --run: that run only
  onlyFlags('lease renew', o, ['sid', 'run']);
  const sid = needSid('renew', o), run = o.run !== undefined ? V.run(o.run) : null;
  const runs = run ? [run] : LS().heldBy(ORCH_DIR, sid).map(x => x.run);
  if (!runs.length) throw new LeaseConflict('lease: no lease to renew; run lease acquire --run <run> first');
  let conflict = null;
  for (const R of runs) {
    const r = renewRun(R, sid, 'renew', false, null);
    if (r.refused) {
      const P = r.st.lease;
      if (!P) { conflict = conflict || `lease: no lease to renew for run ${R}; run lease acquire --run ${R} first`; continue; }
      refusedEvent(R, sid, P);
      conflict = conflict || (r.st.live ? (r.st.legacy ? legacyHeldMsg(P) : leaseHeldMsg(P)) : `lease: expired, and held by ${sid8(P.session)}, not ${sid8(sid)}; run lease acquire --run ${R}`);
      continue;
    }
    leaseEvent(R, 'renew', sid, { holder: sid, expires: r.L.expires, ttl_sec: r.L.ttl_sec });
    console.log(`lease: renew by ${sid8(sid)} until ${hhmm(r.L.expires)} (run ${R})`);
  }
  if (conflict) throw new LeaseConflict(conflict);
}
function leaseRelease(o) { // without --run: every lease this session holds, never another session's; with --run: today's rules for R
  onlyFlags('lease release', o, ['sid', 'run', 'force']);
  const sid = needSid('release', o);
  if (o.force && o.run === undefined) fail('lease release: --force needs --run R (a bare release frees only this session\'s own leases)');
  const run = o.run !== undefined ? V.run(o.run) : null;
  const runs = run ? [run] : LS().heldBy(ORCH_DIR, sid).map(x => x.run);
  let conflict = null, released = 0;
  for (const R of runs) {
    const r = releaseRun(R, sid, !!o.force, !run);
    if (r.none) continue;
    if (r.skipped) { console.error(`manifest: lease for run ${R} is no longer this session's (held by ${sid8(r.P.session)}); left alone`); continue; }
    if (r.refused) { refusedEvent(R, sid, r.P); conflict = conflict || (r.legacy ? legacyHeldMsg(r.P) : leaseHeldMsg(r.P)); continue; }
    const prevHolder = r.P.session !== sid ? r.P.session : null;
    leaseEvent(R, 'release', sid, { holder: null, prev_holder: prevHolder, expires: r.P.expires || null, ttl_sec: r.P.ttl_sec || null });
    console.log(`lease: released by ${sid8(sid)} (run ${R})${prevHolder ? `; it was held by ${sid8(prevHolder)}` : ''}`);
    released++;
  }
  if (conflict) throw new LeaseConflict(conflict);
  if (!released) console.log('lease: none (nothing to release)');
}
function leaseCheck(o) {
  onlyFlags('lease check', o, ['sid', 'run', 'json']);
  const sid = sessionOf(o);
  if (o.run !== undefined) { // one run: today's block for it
    const run = V.run(o.run), st = leaseState(sid, run), S = leaseSummary(st, sid);
    if (o.json) return console.log(JSON.stringify(S));
    console.log(`state: ${S.state}`);
    if (S.holder) {
      console.log(`holder: ${S.holder}`);
      console.log(`run: ${S.run} (${S.path})`);
      console.log(`expires: ${S.expires} (${hhmm(S.expires)}, ${st.left_sec != null ? (st.left_sec >= 0 ? `${Math.floor(st.left_sec / 60)}m left` : `expired ${Math.floor(-st.left_sec / 60)}m ago`) : '?'})`);
      if (st.legacy) console.log('legacy: yes');
    }
    if (!sid && st.live) console.log('note: no session id given, so mine and other are not told apart (pass --sid)');
    return;
  }
  // no --run: line 1 is this session's own view (mine|expired|none, never other), then every lease in the dir
  const now = Date.now(), sums = LS().listLeases(ORCH_DIR).map(x => leaseSummary(leaseStateOf(x, sid, now), sid));
  const own = sid ? sums.filter(S => S.holder === sid) : [];
  const state = own.some(S => S.state === 'mine') ? 'mine' : own.length ? 'expired' : 'none';
  if (o.json) return console.log(JSON.stringify({ state, sid: sid || null, leases: sums }));
  console.log(`state: ${state}`);
  console.log(`leases: ${sums.length}`);
  for (const S of sums) console.log(leaseLine(S));
  if (!sid) console.log('note: no session id given, so state is none and mine and other are not told apart (pass --sid)');
}
function leaseLine(S) {
  if (S.state === 'none') return 'lease: none';
  return `lease: ${S.state}  holder ${sid8(S.holder)}  run ${S.run}  ${S.state === 'expired' ? 'expired since' : 'until'} ${hhmm(S.expires)}${S.legacy ? '  (legacy ACTIVE.lease)' : ''}${!S.sid && S.state === 'other' ? '  (no session id: mine and other not told apart)' : ''}`;
}

// ---- commands ----------------------------------------------------------------------
const HELP = `manifest.js: per-run attempt manifest (<runs>/<run>/manifest.jsonl, append-only)
  launch --run R --row N --role <role> --agent <type> --model <m> --effort <e> --mode <mode> --repo <repo> --area <area>
         [--git <repo path>] [--branch B] [--base SHA] [--worktree P] [--result P] [--subtask T] [--why W] [--kind creation|mechanical|standard] [--continue <attempt-id> [--tune]] [--new] [--verbose] [--sid S]
         [--cause owner-tune|review-findings|failed|tooling]
         prints the attempt ID (<run>#<row>.<n>) on stdout line 1 and 'new' or 'reused' on line 2. With --continue the predecessor's ingest sweep
         runs first but its lines (and the lease note) go to stderr AFTER the ID, so '2>&1 | head -1' still gives the ID. The sweep replays every notice
         in the transcript, other runs' too: only the predecessor's own lines and failures are kept, plus one count of the rest; --verbose lists them all.
         A repeat of the same --continue <id> (the continuation not started yet) returns the already-allocated continuation ID ('reused'), like a
         plain launch. A new continuation sets the predecessor's ledger first_pass to n (a retry-kind continue, ledger.js update); --tune marks an owner
         tune round instead and leaves the label alone (the launch event records tune:true). An explicit later ledger.js update still wins.
         --cause says why a retry was made: on a continuation, or a fresh launch n>1 whose predecessor ran (a stop or a ledger line), the launch
         event records cause (--tune is owner-tune) and the predecessor's line gets it with its first-pass n (a fresh retry flips only an unset
         first-pass; owner-tune never flips). Without --cause it records unstated and prints, after the ID lines, 'manifest: warning: retry
         launch without --cause; recorded as unstated (--cause owner-tune|review-findings|failed|tooling)' on stderr. Never refused for it.
         --holdout-seed is accepted and ignored (no holdout is rolled, no holdout line is printed); old holdout:true events stay readable.
         A launch above the active wfp preset's bar for its role with no Why (--why or the plan row's Why cell) still succeeds and prints, after the lines above,
         'above bar: <role> <model>/<effort> (bar <model>/<effort>), no --why', and records above_bar:true on the launch event (a repeat writes no second event).
         The launch event also records plan_mode (PLAN.md frontmatter mode:), why (--why, else the plan row's Why cell) and
         row_kind (--kind, else creation/mechanical/standard from the Why; null when there is no Why or --kind, never "standard") for drivers.js; missing ones are null.
  void   --attempt <run>#<row>.<n> [--reason <text>] [--sid S]     (an attempt that was allocated but never launched; refused once it has a start, stop or ledger line)
  event  --run R --type start|stop|outcome|return_over_cap --json '<fields>' [--sid S]     (tests and repair)
  show   --run R [--row N] [--json] [--sid S]
  lease  acquire --run R --sid S [--force] | renew --sid S [--run R] | release --sid S [--run R] [--force] | check [--run R] [--sid S] [--json]
         one lease per run in leases/<run>.json (TTL leaseTtlSec); a session may hold several. A legacy ACTIVE.lease counts
         only for its own run and moves to leases/<run>.json on its holder's next write, or when another session takes the
         run over after it expired. ACTIVE itself is never read or written.
  reconcile --run R [--row N[,N...]] [--sid S] [--json]
         rebuilds missing starts from meta.json, sweeps start transcripts (ledger.js ingest, child process), then prints
         row | attempt | verdict (done, unknown <reasons>, legacy) | evidence | next (advance, gate, wait, redispatch, ask, old-rule).
         The sweep writes only into runs the --sid session holds, plus R unless another session holds R live; with no session
         id, only into a notice's own run, and only while no session holds it live. The start rebuild is not gated.
  activity --run R [--minutes 15] [--sid S] [--json]
         read-only: is another session still driving R? Prints state: recent|quiet, the newest signal and up to 5 more,
         agent-live none|weak|strong, handoff. Run it before any lease acquire on resume; lease check 'none' does not mean idle.
  resumable [--sid S] [--limit N (1-50, default 6)] [--minutes 15] [--json]
         read-only list for an argument-less /orch-resume (it lists; the caller asks; nothing is picked). Candidates: run
         folders with HANDOFF.md (handoff, newest first), then PLAN.md-only folders (plan, newest first), inspected until N
         runs that are not done are found. Prints 'resumable: <run>  <kind> <iso> (<age>m)  preset <p|?>  lease none|expired|mine'
         or 'excluded: <run>  done | lease other: holder <sid8> until HH:MM UTC, activity quiet|recent | activity recent:
         <newest kind> <age>m', then 'candidates: <k> of <n> checked'. done = PLAN.md's ## Status table has rows and none is
         pending, running, blocked or orphaned; done runs are listed last and do not count toward N. --json: {candidates,
         excluded, checked}. ACTIVE is not read. It writes nothing and always exits 0 (2 on usage).
  status [--run R] [--limit N (1-50, default 20)] [--sid S] [--json]
         read-only. --run R: 'status: run R  preset <p|?>  plan yes|no  rows <done>/<total>', then one line per row (the union of
         PLAN.md's ## Status rows, manifest attempts and ledger rows): row | subtask | status | attempts | latest | model/effort |
         sha | tokens | time (status = the Status cell, else the latest attempt's state; sha = the result file's sha:; tokens and
         time = ledger sums, '?' for unknown), then the lease line. A row is done when its status starts with a passStatuses word
         or 'finished'. Without --run: one line per run folder, 'run  rows d/t  open k  tokens <sum>  lease none|mine|other
         <sid8>|expired <sid8>', not-done runs first, newest first within each group. No lock, no ingest, no write; exits 0 (2 usage).
  runs [--sid S] [--limit N (1-50, default 6)] [--minutes 15] [--json]
         read-only overview of every run folder, in sections: 'live: <run>  holder <sid8>[ (this session)]  until HH:MM UTC
         activity quiet|recent|no-folder' per live lease ('live: none' without one); then exactly the lines resumable prints for
         the same --sid, --limit and --minutes; then 'finished: <run>  <iso>' per done run (PLAN.md's Status table, as resumable
         judges it), newest first, at most N, with '(k open attempt(s) with no stop event)' when old attempts never stopped; then
         'other: <k> folders without PLAN.md or HANDOFF.md[: up to 5 names]'. --json: {live, resumable, finished, finished_total,
         other}. No lock, no ingest, no write; exits 0 (2 usage).
  progress --run R [--rebuild] [--json]
         the run's boss.progress/1 snapshot (<progress root>/PROGRESS.md), kept current by every manifest append under the manifest lock at
         <root>/runs/orchestrator/<run>/progress.json (root: $BOSS_PROGRESS_DIR, else <config dir>/boss; config progress:false or ORCH_PROGRESS=off
         turns publishing off; a scratch ORCH_DIR/ORCH_RUNS_DIR publishes only with an explicit BOSS_PROGRESS_DIR). Prints 'progress: <run> state <s> seq <n>/<manifest seq> <summary>' and the file path; --rebuild replays all
         manifest events (+ PLAN.md, result files, lease events) into it first: crash recovery, and runs that began before the publisher.
         A run ends (state done) when its lease is released with the plan finished; a release on an unfinished plan is 'waiting_input' (paused).
         Optional <run>/attention.json (array of {id, kind, text, item?, since?, options?}) is shown as attention items; a plan row whose Status
         is a gate status (gaps_found, revise, human_needed) becomes an approval item. A publish failure never fails a launch or hook.
  doctor [--check id[,id...]] [--sid S] [--json] [--fix id[,id...]|all]
         health checks (lib/doctor.js), read-only: leases (unreadable or misnamed files; own expired leases and own live
         leases on done runs: warn, fixable; foreign leases: info, never fixable; *.lock dirs over 5 min: info), wfp (the shipped
         wfp config plus the <data>/wfp.json overlay: profiles, roles, efforts, ultracodeRaise, typeRoles), config (<data>/config.json),
         runs (folders without PLAN.md; open attempts over 24 h with lease none or expired), active (a legacy ACTIVE file: info),
         worktrees (prunable entries; merged, clean, quiet run worktrees), backups (*.bak-* copies; *.bak-fix* and settings.json
         .bak-orch-*/.bak-wfp-* beyond the newest 2: fixable), hooks (the plugin hooks.json is present and parsed, every script it
         names exists; warns when settings.json still registers orchestrator hooks), runtime (Node version, data dir writable, the
         plugin skills present). --fix leases|worktrees|backups|all applies only the fixable findings: releases this session's
         leases (needs --sid; never --force, never another session's), git worktree prune / remove (branch kept), moves backups
         to <root>/.trash/doctor-<UTC ts>/; prints 'fixed <check>: ...' per fix, then that check again.
         Prints '<level> <check>: <msg>[ · fix: <hint>]' per finding (level ok|info|warn|error), then 'doctor: <w> warn, <e> error;
         fixable: <ids|none>'. Root = env ORCH_CLAUDE_ROOT (default the config dir). Unknown --check ids exit 2; otherwise exits 0.
Semantics
  - launch reuses the row's latest attempt when it never started (no stop, no start backed by a meta.json): same ID, 'reused'.
    It refuses (exit 2) while that attempt runs (a meta.json-backed start, or any stop, and no final stop) and while a
    continuation has no final stop. --new always allocates a fresh n. n = 1 + max(launch events, ledger lines) for the row.
  - void --attempt A marks an allocated-but-never-launched attempt (a collision's phantom ID): show lists it under 'voided' (show --json: voided[]), and
    reconcile, status, launch's reuse rule and the progress items skip it; n is never reused. Refused when A has a start/stop event or a ledger line.
  - A turn-limit notice ("stopped at its N-turn limit", status completed, partial result) is stored as a final stop, phase stopped-turn-limit,
    status turn-limit: launch --continue accepts it, and the ledger line carries its numbers only while the attempt has no real final notice. It speaks for
    the attempt only when strictly later (by ts) than every real final stop: a late-ingested older one never overrides a finish. The progress item reads waiting_input.
  - launch --continue P ingests P's transcript first (ledger.js ingest, as a child process, gated like reconcile's sweep: the
    caller's held runs plus R), then refuses (exit 2) until P has a final notice stop; the new launch records continues: P and
    P's task_id.
  - event dedupes by type + key; the key is derived from the fields when --json gives none.
  - show lists open attempts first (allocated, started, stopped-unreported, waiting; oldest first, with their age), then the rest
    by row and n, then unplanned starts (no attempt ID), then the lease line.
  - launch with a session id refuses (exit 3, no launch event) while another session holds a live lease on that run. Otherwise
    it takes the run's lease when it is free (none or expired: an acquire event with by=launch and a note on stderr; stdout is
    unchanged), moves this session's legacy ACTIVE.lease for that run to leases/<run>.json, and renews this session's lease on
    that run when less than leaseRenewWhenLeftSec is left. Leases on other runs never block it. A launch event is
    written again on reuse only when model or effort changed.
  - lease acquire, and renew/release with --run R, refuse (exit 3) while another session holds R live; --force takes it (steal)
    or drops it, except a live legacy ACTIVE.lease, which only expiry or its holder ends. Without --run, renew and release act
    on every lease the session holds and never on another session's (renew exits 3 when it holds none; release --force needs
    --run). check --run R prints R's block (legacy: yes for the legacy file); check without --run prints state: mine|expired|none
    for the session, then leases: <n> and one line per lease. show prints the run's own lease line.
  - activity signals: result files (<row>-*.md) and PLAN.md at the top of the run folder, ledger.jsonl, the newest manifest
    event that is not a lease event, and live agents (transcript touched within 3 min) of other sessions whose brief or
    meta.json names the run: strong = an attempt ID of the run or the path runs/R, weak = the bare run name. recent = any
    file/event signal within --minutes, or any live agent; HANDOFF.md with nothing newer than it + 120 s is quiet (a clean
    handoff). --sid (or WFP_SID) excludes the caller's own agents. It writes nothing and always exits 0 (2 on usage).
  - launch refuses (exit 4, no event, before the lease gate, no bypass flag) while the owner has stopped the run in Boss: the file
    <boss root>/runs/orchestrator/<run>/control/cancel exists. Boss > Undo stop removes it.
  - Exit codes: 0 ok, 2 usage error or a refused launch, 3 lease conflict, 4 run stopped by owner, 1 internal error. --sid falls back to env WFP_SID;
    an empty --sid= counts as no session id.
  - Files: ${RUNS}/<run>/manifest.jsonl; config ${path.join(ORCH_DIR, 'config.json')}.`;

// planRowWhy(PLAN.md text, row) -> the Why cell of the row in the first markdown table whose header has both '#' and 'Why', or null.
function planRowWhy(txt, row) {
  if (typeof txt !== 'string') return null;
  const lines = txt.split(/\r?\n/), cells = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].trim().startsWith('|')) continue;
    const head = cells(lines[i]).map(c => c.toLowerCase()), idc = head.indexOf('#'), wc = head.indexOf('why');
    if (idc < 0 || wc < 0) { while (i + 1 < lines.length && lines[i + 1].trim().startsWith('|')) i++; continue; }
    for (let j = i + 1; j < lines.length && lines[j].trim().startsWith('|'); j++) {
      if (/^\|[\s:|-]+$/.test(lines[j].trim())) continue;
      const c = cells(lines[j]);
      if (String(c[idc] || '').toLowerCase() === row) return (c[wc] || '').slice(0, 200) || null;
    }
    return null;
  }
  return null;
}
const kindOfWhy = why => !why ? null : /creation|try-?it|new feature|design/i.test(why) ? 'creation' : /mechanical/i.test(why) ? 'mechanical' : 'standard';
function launchPlanFields(run, row, why, kind) {
  let txt = null; try { txt = fs.readFileSync(path.join(runDir(run), 'PLAN.md'), 'utf8'); } catch {}
  let planMode = null;
  if (txt != null) { const fm = frontmatter(path.join(runDir(run), 'PLAN.md')) || {}; const m = /^([abc])\b/i.exec(String(fm.mode || '').trim()); if (m) planMode = m[1].toUpperCase(); }
  const w = why != null ? why : planRowWhy(txt, row);
  return { plan_mode: planMode, why: w, row_kind: kind || kindOfWhy(w) };
}

// Above-bar check (launch warns, never blocks): the active wfp preset's bar for the row's role comes from wfp.js itself
// (`escalate <role> --json`, which applies the session policy, ultracode and overrides), never from its printed table.
// ORCH_WFP_JS overrides the script path (tests). Any failure (no policy, no wfp, bad JSON) gives null: no warning.
const BAR_MODELS = ['haiku', 'sonnet', 'opus'], BAR_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
function wfpBar(role, sid) {
  try {
    const js = process.env.ORCH_WFP_JS || path.join(__dirname, '..', 'wfp', 'wfp.js');
    if (!fs.existsSync(js)) return null;
    const r = spawnSync(process.execPath, [js, 'escalate', role, '--json', ...(sid ? [`--sid=${sid}`] : [])], { encoding: 'utf8', timeout: 5000 });
    if (r.status !== 0) return null;
    const b = JSON.parse(String(r.stdout || '').trim()).bar;
    return b && typeof b.model === 'string' ? { model: b.model, effort: typeof b.effort === 'string' ? b.effort : null } : null;
  } catch { return null; }
}
function aboveBar(model, effort, bar) { // model rank first, then effort rank; an unranked name never counts as above
  const mi = BAR_MODELS.indexOf(model), bi = bar ? BAR_MODELS.indexOf(bar.model) : -1;
  if (mi < 0 || bi < 0) return false;
  if (mi !== bi) return mi > bi;
  const ei = BAR_EFFORTS.indexOf(effort), bei = BAR_EFFORTS.indexOf(bar.effort);
  return ei >= 0 && bei >= 0 && ei > bei;
}

// ---- owner Stop (Boss board): <boss root>/runs/orchestrator/<run>/control/cancel ----------------------------------------
// The marker is written by the Boss extension and removed by its Undo. Presence is the whole contract (an unreadable or malformed
// file still counts); only the best-effort timestamp in the message comes from parsing it. Independent of progress publishing, so it
// holds with progress:false / ORCH_PROGRESS=off. A scratch ORCH_DIR / ORCH_RUNS_DIR without BOSS_PROGRESS_DIR skips it (tests stay hermetic).
function stopMarker(run) { // {file, at} when the owner stopped this run, else null. Never throws.
  try {
    if (!process.env.BOSS_PROGRESS_DIR && paths.isScratch()) return null;
    const base = paths.progressRoot();
    const file = path.join(base, 'runs', 'orchestrator', run, 'control', 'cancel');
    let st; try { st = fs.lstatSync(file); } catch { return null; }
    let at = null;
    if (st.isFile() && st.size < 4096) { try { const j = JSON.parse(fs.readFileSync(file, 'utf8')); if (j && typeof j.requested_at === 'string') at = j.requested_at.slice(0, 32); } catch { /* malformed: still stopped */ } }
    return { file, at: at || new Date(st.mtimeMs).toISOString() };
  } catch { return null; }
}

function launch(o) {
  const REQ = ['run', 'row', 'role', 'agent', 'model', 'effort', 'mode', 'repo', 'area'];
  onlyFlags('launch', o, [...REQ, 'git', 'branch', 'base', 'worktree', 'result', 'subtask', 'why', 'kind', 'continue', 'new', 'tune', 'verbose', 'cause', 'sid', 'holdout-seed']);
  const missing = REQ.filter(f => o[f] === undefined);
  if (missing.length) fail(`launch: missing ${missing.map(f => '--' + f).join(' ')}`);
  const run = V.run(o.run), row = V.row(o.row), model = V.model(o.model), effort = V.effort(o.effort), sid = sessionOf(o);
  const stopped = stopMarker(run); // before the lease gate, --continue and the same-ID repeat: a stopped run hands out no ID, no flag bypasses it
  if (stopped) throw new Stopped(`run ${run} stopped by owner (Boss Stop at ${stopped.at}); not launching. Undo: Boss board > Undo stop, or rm ${stopped.file}`);
  const fields = { role: V.role(o.role), agent: V.agent(o.agent), model, effort, mode: V.mode(o.mode), repo: repoV(o.repo), area: V.text('area', 80)(o.area),
    git: o.git !== undefined ? V.file('git')(o.git) : null, branch: o.branch !== undefined ? V.text('branch', 200)(o.branch) : null,
    base: o.base !== undefined ? V.sha(o.base) : null, worktree: o.worktree !== undefined ? V.file('worktree')(o.worktree) : null,
    result: o.result !== undefined ? V.file('result')(o.result) : null, subtask: o.subtask !== undefined ? V.text('subtask', 200)(o.subtask) : null };
  // usage-driver fields (metrics run row 6b; read by drivers.js from the launch event's raw JSON, no schema change): the run's planning
  // mode (PLAN.md frontmatter mode:), the row's Why (--why, else the plan table's Why cell) and a creation/mechanical/standard kind
  // (--kind, else derived from the Why). Missing PLAN.md or an unparseable row gives null; never fails the launch.
  Object.assign(fields, launchPlanFields(V.run(o.run), V.row(o.row), o.why !== undefined ? V.text('why', 200)(o.why) : null,
    o.kind !== undefined ? V.kind(o.kind) : null));
  // No holdout: no roll, no holdout line; --holdout-seed stays an accepted no-op for old callers.
  if (o.tune && o.continue === undefined) fail('--tune applies to --continue only');
  // P31: why a retry was made. --tune is cause owner-tune; a retry launch (a continuation, or a fresh attempt n>1 whose predecessor ran) without
  // --cause records 'unstated' and prints one warning line after the ID. Never refused for a missing cause.
  let cause = o.cause !== undefined ? V.cause(o.cause) : null;
  if (o.tune) { if (cause && cause !== 'owner-tune') fail(`--tune is cause owner-tune; drop --cause ${cause} or pass --cause owner-tune`); cause = 'owner-tune'; }
  // Above the active preset's bar with no Why (--why, else the plan row's Why cell): print one warning line, record above_bar:true on the
  // launch event. The launch still succeeds. With a Why, at or below the bar, or no readable bar: nothing printed or recorded.
  let warnAbove = null;
  if (!fields.why) { const bar = wfpBar(fields.role, sid); if (bar && aboveBar(model, effort, bar)) { fields.above_bar = true;
    warnAbove = `above bar: ${fields.role} ${model}/${effort} (bar ${bar.model}/${bar.effort || '-'}), no --why`; } }
  if (sid) { // D-6 (per-run gate): another session's live lease on this run refuses (exit 3) before anything runs (a read-only
    // early check; the binding one is under the run's lock below). Leases on other runs never block.
    const st = leaseState(sid, run);
    if (st.state === 'other') throw new LeaseConflict(st.legacy ? legacyHeldMsg(st.lease) : leaseHeldMsg(st.lease));
  }
  let cont = null;
  const notes = [], flushNotes = () => { while (notes.length) console.error(notes.shift()); };
  if (o.continue !== undefined) {
    cont = parseAttemptId(o.continue);
    if (!cont) fail(`--continue '${o.continue}': an attempt ID such as ${run}#3.1`);
    if (cont.run !== run) fail(`--continue ${cont.id}: not an attempt of run ${run}`);
    // G8: the ingest runs first, without the manifest lock (the ingest child's appendEvent takes it). Its output is held back and goes to
    // stderr AFTER the ID (flushNotes below), so stdout line 1 is the ID and '2>&1 | head -1' still shows it; a refusal flushes it first.
    const T = transcriptFor(groupByAttempt(readEvents(run).events), cont.id);
    if (T) {
      // The ingest child replays every notice in the transcript, other runs' too. Without --verbose only this attempt's lines (and any failure) are kept.
      const lines = runIngest(T, sid, run).lines, mine = l => l.startsWith(`${cont.id}:`) || /^ingest (failed|unavailable)/.test(l);
      for (const l of o.verbose ? lines : lines.filter(mine)) notes.push(`manifest: ${l}`);
      if (!o.verbose && lines.length > lines.filter(mine).length) notes.push(`manifest: ingest: ${lines.length - lines.filter(mine).length} other notice line(s) not shown (--verbose lists them)`);
    }
    else notes.push(`manifest: no transcript for ${cont.id}; checking the manifest as it stands`);
  }
  try {
  fs.mkdirSync(runDir(run), { recursive: true });
  let took = null;
  if (sid) { // D-6 acquire-when-free: under the run's lease lock, before the ID is allocated. A legacy lease is migrated first
    // (its holder's launch, or another session's takeover of an expired one; D-2 f). none/expired -> acquire; this session's
    // live lease -> left alone, renewed below when low; another session's live lease -> exit 3 and no launch event.
    const r = acquireRun(run, sid, false, path.resolve(runDir(run)), 'launch', true);
    if (r.refused) throw new LeaseConflict(r.legacy ? legacyHeldMsg(r.prev) : leaseHeldMsg(r.prev));
    if (r.action) {
      const prevHolder = r.prev && r.prev.session !== sid ? r.prev.session : null;
      leaseEvent(run, r.action, sid, { holder: sid, prev_holder: prevHolder, expires: r.L.expires, ttl_sec: r.L.ttl_sec, by: 'launch' });
      took = { L: r.L, prevHolder };
    } else renewIfMine(sid, { run, by: 'launch' });
  }
  const out = withLock(manifestFile(run), cfg.lockWaitMsCli, () => {
    const events = readEvents(run).events, groups = groupByAttempt(events);
    let contStop = null;
    if (cont) {
      const P = groups.get(cont.id);
      if (!P || !P.launches.length) refuse(`${cont.id} has no launch event in run ${run}; nothing to continue`);
      contStop = pickStop(finalStops(P)) || idleStop(P);
      if (!contStop) refuse(`${cont.id} has no final stop yet; wait for its notice`);
    }
    const rowLaunches = events.filter(e => e.type === 'launch' && String(e.row) === row && e.attempt); // voided ones still count for n: an ID is never reused
    let latest = null;
    for (const e of rowLaunches) if (!(groups.get(e.attempt) && isVoided(groups.get(e.attempt))) && (!latest || (e.n || 0) > (latest.n || 0))) latest = e;
    const g = latest ? groups.get(latest.attempt) : null;
    let kind = 'new';
    if (g && !o.new && !finalStops(g).length && !idleStop(g)) {
      const L = last(g.launches);
      const started = g.stops.length || g.starts.some(s => metaFor(s));
      // A repeated --continue <same predecessor> whose continuation has not started is idempotent, like a plain launch: the already-allocated ID.
      const sameCont = cont && L.continues === cont.id && !started;
      if ((L.continues && !sameCont) || started) refuse(`${g.id} is still running; wait for its notice, or pass --new`);
      if (!cont || sameCont) kind = 'reused'; // a phantom (denied call) or never started; a continuation takes over only its own phantom, never a plain launch's
    }
    let id, n, key;
    if (kind === 'reused') {
      id = g.id; n = latest.n;
      const L = last(g.launches);
      if (L.model === model && L.effort === effort) return { id, kind }; // same model/effort: nothing written
      // Dedupe-key fix (row 12, deviation from design rev 3): a reuse launch's key carries its per-attempt launch ordinal, so an
      // A->B->A model/effort flip appends a third launch instead of deduping against the first, and "latest launch" stays right.
      key = `${id}:${model}/${effort}#${g.launches.length + 1}`;
    } else {
      n = 1 + Math.max(0, ...rowLaunches.map(e => e.n || 0), ledgerMaxN(run, row));
      if (n > 999) fail(`launch: row ${row} would get attempt ${n}; the ID allows 1-999`);
      id = `${run}#${row}.${n}`; key = `${id}:${model}/${effort}`;
    }
    // A fresh retry: a new attempt n>1 (no --continue) whose predecessor ran (a stop in the manifest, or a ledger line). A phantom (denied call,
    // never started) is no retry: --new past it records no cause and flips nothing.
    let prevId = null;
    if (!cont && kind === 'new' && n > 1) {
      const pid = `${run}#${row}.${n - 1}`, P = groups.get(pid);
      if ((P && P.stops.length) || readLedger(run).some(x => x.id === pid)) prevId = pid;
    }
    const retry = !!(cont || prevId);
    appendLocked(run, Object.assign({ type: 'launch', attempt: id, row, key, via: 'cli', session: sid, n }, fields,
      { continues: cont ? cont.id : null, task_id: contStop ? contStop.task_id || null : null }, cont && o.tune ? { tune: true } : {},
      retry ? { cause: cause || 'unstated' } : {}));
    return { id, kind, prevId, retry };
  });
  console.log(`${out.id}\n${out.kind}`);
  if (warnAbove) console.log(warnAbove);
  flushNotes();
  if (out.retry && !cause && out.kind === 'new') console.error(`manifest: warning: retry launch without --cause; recorded as unstated (--cause ${CAUSES.join('|')})`);
  // A new continuation is a retry-kind continue: the predecessor did not pass first time. Once, at allocation (never on a repeat); --tune marks
  // an owner tune round and leaves the label (cause owner-tune only). Later explicit 'ledger.js update' calls still win; a missing ledger line
  // only prints a note. A fresh retry (P31) flips its predecessor the same way, but only while that line has no first-pass yet.
  const ledgerSet = (id, args) => spawnSync(process.execPath, [path.join(__dirname, 'ledger.js'), 'update', '--attempt', id, ...args], { encoding: 'utf8', timeout: 20000 });
  const why = r => (String(r.stderr || '').split('\n').find(l => l.trim()) || `exit ${r.status}`).slice(0, 160);
  const c = cause || 'unstated';
  if (cont && out.kind === 'new' && !o.tune) {
    const r = ledgerSet(cont.id, ['--first-pass', 'n', '--cause', c]);
    if (r.status !== 0) console.error(`note: first-pass of ${cont.id} not set to n: ${why(r)}`);
    else console.error(`note: ${cont.id} first-pass set to n (continued by ${out.id}; cause ${c}; --tune to keep the label)`);
  } else if (cont && out.kind === 'new') {
    const r = ledgerSet(cont.id, ['--cause', 'owner-tune']);
    if (r.status !== 0) console.error(`note: cause of ${cont.id} not recorded: ${why(r)}`);
  } else if (out.prevId && out.kind === 'new') {
    const P = readLedger(run).find(x => x.id === out.prevId);
    const args = P && P.firstPass == null && c !== 'owner-tune' ? ['--first-pass', 'n', '--cause', c] : P && P.cause == null ? ['--cause', c] : null;
    if (!P) console.error(`note: first-pass of ${out.prevId} not set to n: no ledger line yet (record it with ledger.js close --attempt ${out.prevId} once its notice is in)`);
    else if (args) {
      const r = ledgerSet(out.prevId, args);
      if (r.status !== 0) console.error(`note: ${out.prevId} not updated: ${why(r)}`);
      else console.error(`note: ${out.prevId} ${args[0] === '--first-pass' ? 'first-pass set to n' : 'first-pass kept'} (fresh retry ${out.id}; cause ${c})`);
    }
  }
  if (took) console.error(`note: lease acquired by ${sid8(sid)} until ${hhmm(took.L.expires)} (run ${run})${took.prevHolder ? `; previous holder ${sid8(took.prevHolder)}` : ''}`);
  if (!sid) console.error('note: no session id, lease not checked; hooks will not record this run');
  } catch (e) { flushNotes(); throw e; } // a refusal or lease conflict: the held-back ingest lines still print, before the error message
}

function deriveKey(type, f) {
  const need = (...ks) => { for (const k of ks) if (f[k] == null || f[k] === '') fail(`event ${type}: --json needs "${k}" (or an explicit "key")`); };
  if (type === 'start') { need('tool_use_id'); return String(f.tool_use_id); }
  if (type === 'stop') {
    need('task_id', 'phase');
    if (f.phase === 'agent-stopped' || f.via === 'substop') return `${f.task_id}:substop:${f.stop_hook_active ? 1 : 0}:${f.result_bytes == null ? 0 : f.result_bytes}`;
    return `${f.task_id}:${f.phase}:${f.tools == null ? '-' : f.tools}:${f.ms == null ? '-' : f.ms}`;
  }
  if (type === 'outcome') { need('attempt'); return `${f.attempt}:${f.ts}`; }
  need('task_id', 'action'); return `${f.task_id}:${f.action}`; // return_over_cap
}
function eventCmd(o) {
  onlyFlags('event', o, ['run', 'type', 'json', 'sid']);
  for (const f of ['run', 'type', 'json']) if (o[f] === undefined) fail(`event: missing --${f}`);
  const run = V.run(o.run), type = String(o.type);
  if (!EVENT_TYPES.includes(type)) fail(`--type '${o.type}': one of ${EVENT_TYPES.join(', ')} (launch and lease have their own commands)`);
  let f; try { f = JSON.parse(o.json); } catch (e) { fail(`--json: not valid JSON (${e.message})`); }
  if (!f || typeof f !== 'object' || Array.isArray(f)) fail('--json: a JSON object of event fields');
  for (const k of ['id', 'seq', 'type', 'run']) if (k in f) fail(`--json: "${k}" is set by manifest.js`);
  if (f.attempt != null) {
    const a = parseAttemptId(f.attempt);
    if (!a) fail(`--json attempt '${f.attempt}': an attempt ID such as ${run}#3.1`);
    if (a.run !== run) fail(`--json attempt ${a.id}: not an attempt of run ${run}`);
    f.attempt = a.id; if (f.row == null) f.row = a.row;
  } else f.attempt = null;
  if (f.row != null) f.row = V.row(f.row);
  if (f.ts != null && !Number.isFinite(Date.parse(f.ts))) fail(`--json ts '${f.ts}': an ISO timestamp`);
  f.ts = f.ts || new Date().toISOString();
  if (f.via != null && !VIAS.includes(f.via)) fail(`--json via '${f.via}': one of ${VIAS.join(', ')}`);
  if (f.session == null) f.session = sessionOf(o);
  if (type === 'stop' && f.final === undefined) f.final = FINAL_PHASES.includes(f.phase);
  if (f.key == null) f.key = deriveKey(type, f); else f.key = String(f.key);
  const r = appendEvent(run, Object.assign(f, { type }), { create: true });
  console.log(`${r.id} ${r.dup ? 'dup' : 'new'}`);
}

function show(o) {
  onlyFlags('show', o, ['run', 'row', 'json', 'sid']);
  if (o.run === undefined) fail('show: needs --run');
  const run = V.run(o.run), row = o.row !== undefined ? V.row(o.row) : null, F = fold(run, { row, sid: sessionOf(o) });
  if (o.json) return console.log(JSON.stringify(F));
  const file = manifestFile(run);
  if (!fs.existsSync(file)) return console.log(`manifest: run ${run} has no manifest yet (${file})`);
  const open = F.attempts.filter(a => OPEN.includes(a.state));
  console.log(`manifest: run ${run}${row ? ` row ${row}` : ''}: ${F.attempts.length} attempt(s), ${open.length} open${F.bad_lines ? `, ${F.bad_lines} unreadable line(s)` : ''}`);
  const line = a => {
    const L = a.launch || {};
    return [a.id, a.state, a.age_min != null ? `age ${a.age_min}m` : null, L.model ? `${L.model}/${L.effort}` : null, L.agent || null,
      a.ledger ? 'ledger' : null, a.flags.length ? `[${a.flags.join(', ')}]` : null, L.subtask || null].filter(Boolean).join('  ');
  };
  if (open.length) { console.log('open:'); for (const a of open) console.log('  ' + line(a)); }
  const rest = F.attempts.filter(a => !OPEN.includes(a.state));
  if (rest.length) { console.log('attempts:'); for (const a of rest) console.log('  ' + line(a)); }
  if (F.voided) console.log(`voided: ${F.voided.join(', ')}`);
  if (F.unplanned.length) {
    console.log('unplanned:');
    for (const u of F.unplanned) console.log('  ' + [u.tool_use_id || '-', u.state, u.agent, u.description].filter(Boolean).join('  '));
  }
  console.log(leaseLine(F.lease));
}

// ---- reconcile (design §7; A-19) -------------------------------------------------------------------------------
function frontmatter(file) { // flat `key: value` pairs between the first two --- lines; an inline ` # note` is kept as <key>#comment
  let txt; try { txt = fs.readFileSync(file, 'utf8'); } catch { return null; }
  const lines = txt.split(/\r?\n/), i0 = lines.findIndex(l => l.trim() === '---');
  const i1 = i0 < 0 ? -1 : lines.findIndex((l, i) => i > i0 && l.trim() === '---');
  if (i1 < 0) return null;
  const fm = {};
  for (const l of lines.slice(i0 + 1, i1)) {
    const m = /^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$/.exec(l); if (!m) continue;
    let v = m[2].trim();
    if (!/^["']/.test(v)) { const h = v.search(/\s#/); if (h >= 0) { fm[m[1] + '#comment'] = v.slice(h).replace(/^\s*#\s*/, '').trim(); v = v.slice(0, h).trim(); } }
    fm[m[1]] = v.replace(/^"(.*)"$/, '$1').replace(/^'(.*)'$/, '$1');
  }
  return fm;
}
// resultFor(run, row, launch, id, nAttempts) -> {path, fm, match} or null. launch.result, else <run>/<row>-*.md; a file whose
// `attempt` is this ID wins; an attempt-less one matches only when the row has exactly one attempt.
function resultFor(run, row, launch, id, nAttempts) {
  let files = [];
  if (launch && launch.result) files = [String(launch.result)];
  else { let names = []; try { names = fs.readdirSync(runDir(run)); } catch {} files = names.filter(f => f.toLowerCase().startsWith(row + '-') && f.endsWith('.md')).map(f => path.join(runDir(run), f)); }
  const c = [];
  for (const f of files) { const fm = frontmatter(f); if (!fm) continue; let mt = 0; try { mt = fs.statSync(f).mtimeMs; } catch {} c.push({ path: f, fm, mt }); }
  if (!c.length) return null;
  c.sort((a, b) => b.mt - a.mt);
  const exact = id ? c.find(x => x.fm.attempt === id) : null;
  if (exact) return { path: exact.path, fm: exact.fm, match: true };
  const bare = c.find(x => !x.fm.attempt);
  if (bare) return { path: bare.path, fm: bare.fm, match: !id || nAttempts === 1 };
  return { path: c[0].path, fm: c[0].fm, match: false };
}
function gitOut(dir, args) { // stdout, or null on any git error
  try { const r = spawnSync('git', ['-C', dir, ...args], { encoding: 'utf8', timeout: 30000 }); return r.status === 0 ? String(r.stdout || '') : null; } catch { return null; }
}
function trailerCommit(dir, id) { // A-19: every ref; the newest hit wins and more than one is flagged
  const out = gitOut(dir, ['log', '--all', '--format=%H', `--grep=^Orch-Attempt: ${id.replace(/[.[\]\\*^$]/g, '\\$&')}$`]);
  if (out == null) return { sha: null, error: true };
  const hits = out.split('\n').map(s => s.trim()).filter(Boolean);
  if (!hits.length) return { sha: null };
  const br = gitOut(dir, ['for-each-ref', '--contains', hits[0], '--format=%(refname:short)', 'refs/heads', 'refs/remotes']);
  return { sha: hits[0], branches: br ? br.split('\n').map(s => s.trim()).filter(Boolean) : [], multiple: hits.length > 1, hits: hits.length };
}
const inList = (v, xs) => Array.isArray(xs) && xs.map(String).includes(String(v));
function rebuildStarts(run, g, notes) { // step 0(a), G6: a launch with a session, not a continuation, and no meta.json-backed start
  const L = last(g.launches);
  if (!L || L.continues || g.starts.some(s => metaFor(s))) return 0;
  if (!SID_OK(L.session)) { notes.push(`${g.id}: no session on launch; start not rebuildable`); return 0; }
  let dirs = []; try { dirs = fs.readdirSync(PROJECTS).sort(); } catch {}
  let n = 0;
  for (const d of dirs) {
    const jsonl = path.join(PROJECTS, d, `${L.session}.jsonl`), sub = path.join(PROJECTS, d, L.session, 'subagents');
    if (!exists(jsonl)) continue;
    let names = []; try { names = fs.readdirSync(sub).sort(); } catch {}
    for (const f of names) {
      if (!/^agent-.*\.meta\.json$/.test(f)) continue;
      let m; try { m = JSON.parse(fs.readFileSync(path.join(sub, f), 'utf8')); } catch { continue; }
      if (!m || !m.toolUseId || parseDescription(m.description).attempt !== g.id) continue;
      const r = appendEvent(run, { type: 'start', attempt: g.id, row: L.row, key: String(m.toolUseId), via: 'reconcile', session: L.session,
        tool_use_id: String(m.toolUseId), agent: m.agentType || null, model: m.model == null ? null : m.model, effort: m.effort == null ? null : m.effort,
        description: String(m.description || '').slice(0, 200), background: null, named: null, transcript_path: jsonl, unplanned: false, unknown_attempt: false,
        id_in_brief: null, foreign_lease: null });
      if (r && !r.dup && !r.skipped) { n++; notes.push(`${g.id}: start rebuilt from ${path.join(sub, f)}`); }
    }
  }
  return n;
}
function judge(run, row, g, nAttempts, sid) { // steps 2-3 for one attempt
  const L = last(g.launches), id = g.id;
  const stop = pickStop(g.stops.filter(s => s.final === true && s.phase !== 'agent-stopped')) || idleStop(g);
  const metas = g.starts.map(s => ({ s, m: metaFor(s) })).filter(x => x.m);
  const unfinished = metas.filter(x => x.m.finished !== true);
  const live = !stop && unfinished.some(x => sid && x.s.session === sid);
  const otherUnfinished = !stop && unfinished.some(x => !sid || x.s.session !== sid);
  const res = resultFor(run, row, L, id, nAttempts);
  const status = res && res.match ? String(res.fm.status || '') : null;
  const commit = L.git ? trailerCommit(L.git, id) : null;
  const reasons = [];
  if (!stop) {
    if (live) reasons.push('live');
    else if (otherUnfinished) reasons.push('unfinished-other-session');
    else if (g.stops.some(s => s.phase === 'agent-stopped') && !noticeStops(g).length) reasons.push('notice-pending');
    else reasons.push('no-stop');
  }
  if (!res) reasons.push('no-result');
  else if (!res.match) reasons.push('result-attempt-mismatch');
  else if (!inList(status, cfg.passStatuses) && !inList(status, cfg.gateStatuses)) reasons.push(`result-status:${status || 'none'}`);
  const sha = res && res.match ? String(res.fm.sha || '').trim() : null, shaNone = sha != null && /^none$/i.test(sha);
  const shaReason = res && res.match ? (res.fm.sha_reason || res.fm['sha#comment'] || '') : '';
  if (commit && commit.sha) {
    if (sha && !(/^[0-9a-f]{4,40}$/i.test(sha) && commit.sha.startsWith(sha.toLowerCase()))) reasons.push('sha-mismatch');
  } else if (sha == null) { if (L.git) reasons.push('no-trailer-commit'); }
  else if (shaNone) { if (!shaReason && !inList(L.role, cfg.readOnlyRoles)) reasons.push('sha-none-without-reason'); }
  else if (sha) reasons.push('sha-without-trailer');
  else reasons.push(L.git ? 'no-trailer-commit' : 'sha-none-without-reason');
  const verdict = reasons.length ? 'unknown' : 'done';
  let next;
  if (verdict === 'done') next = inList(status, cfg.passStatuses) ? 'advance' : 'gate';
  else if (reasons.includes('live')) next = 'wait';
  else if (!g.stops.length && !res && !(commit && commit.sha) && !unfinished.length) next = 'redispatch';
  else next = 'ask';
  const ev = {
    stop: stop ? { phase: stop.phase, task_id: stop.task_id || null, ts: stop.ts } : null,
    result: res ? { path: res.path, match: res.match, status: res.fm.status || null, sha: res.fm.sha || null, sha_reason: shaReason || null, attempt: res.fm.attempt || null } : null,
    commit: commit ? (commit.sha ? { sha: commit.sha, branches: commit.branches, multiple: commit.multiple } : { sha: null, error: !!commit.error }) : null,
    git: L.git || null, live, unfinished_other_session: otherUnfinished, git_status: null,
  };
  if (next === 'redispatch' && L.git) { const s = gitOut(L.git, ['status', '--short']); ev.git_status = s == null ? null : s.split('\n').filter(l => l.trim()); }
  const flags = commit && commit.multiple ? ['multiple-commits'] : [];
  return { row, attempt: id, verdict, reasons, status, next, flags, evidence: ev };
}
function judgeLegacy(run, row) { // no launch event: the old rule (result status/sha, else HANDOFF's subject-prefix commit check)
  const res = resultFor(run, row, null, null, 1);
  return { row, attempt: null, verdict: 'legacy', reasons: [], status: res ? String(res.fm.status || '') : null, next: 'old-rule', flags: [],
    evidence: { stop: null, result: res ? { path: res.path, match: true, status: res.fm.status || null, sha: res.fm.sha || null, sha_reason: res.fm.sha_reason || res.fm['sha#comment'] || null, attempt: res.fm.attempt || null } : null,
      commit: null, git: null, live: false, unfinished_other_session: false, git_status: null } };
}
function reconcile(o) {
  onlyFlags('reconcile', o, ['run', 'row', 'sid', 'json']);
  if (o.run === undefined) fail('reconcile: needs --run');
  const run = V.run(o.run), sid = sessionOf(o);
  const rowsArg = o.row !== undefined ? [...new Set(String(o.row).split(',').map(s => V.row(s.trim())))] : null;
  if (!fs.existsSync(runDir(run))) fail(`reconcile: run folder ${runDir(run)} not found`);
  const notes = [], sweep = [];
  let groups = groupByAttempt(readEvents(run).events);
  const launchedRows = () => [...new Set([...groups.values()].filter(g => g.launches.length && !isVoided(g)).map(g => String(last(g.launches).row)))].sort(rowCmp);
  const rows = rowsArg || launchedRows();
  for (const g of groups.values()) if (g.launches.length && !isVoided(g) && rows.includes(String(last(g.launches).row))) rebuildStarts(run, g, notes); // 0(a)
  const transcripts = [...new Set(readEvents(run).events.filter(e => e.type === 'start' && e.transcript_path).map(e => String(e.transcript_path)))].sort();
  for (const P of transcripts) { // 0(b), G1: ledger.js ingest as a child process, never require()
    if (!exists(P)) { sweep.push(`ingest skipped: ${P} (missing)`); continue; }
    for (const l of runIngest(P, sid, run).lines) sweep.push(l);
  }
  groups = groupByAttempt(readEvents(run).events);
  const out = [];
  for (const row of rows) {
    const att = [...groups.values()].filter(g => g.launches.length && !isVoided(g) && String(last(g.launches).row) === row);
    if (!att.length) { out.push(judgeLegacy(run, row)); continue; }
    let g = att[0]; for (const x of att) if ((last(x.launches).n || 0) > (last(g.launches).n || 0)) g = x;
    out.push(judge(run, row, g, att.length, sid));
  }
  if (o.json) return console.log(JSON.stringify({ run, sid: sid || null, notes, sweep, rows: out }));
  console.log(`reconcile: run ${run}${sid ? ` (session ${sid8(sid)})` : ' (no session id: no attempt counts as live)'}`);
  for (const l of [...notes, ...sweep]) console.log(`  ${l}`);
  console.log('row | attempt | verdict | evidence | next');
  for (const r of out) {
    const e = r.evidence, sh = x => String(x).slice(0, 7);
    const ev = [e.stop ? `stop ${e.stop.phase}` : 'stop none',
      e.result ? `result ${e.result.status || '?'}@${e.result.path}${e.result.match ? '' : ' (other attempt)'}` : 'result none'];
    if (r.verdict !== 'legacy') ev.push(e.commit ? (e.commit.sha ? `commit ${sh(e.commit.sha)}@${e.commit.branches.join(',') || '?'}` : 'commit none') : `commit n/a (no --git)`);
    if (e.result && /^none$/i.test(String(e.result.sha || ''))) ev.push(`sha: none${e.result.sha_reason ? ` (${e.result.sha_reason})` : ''}`);
    else if (r.verdict === 'legacy' && e.result && e.result.sha) ev.push(`sha ${e.result.sha}`);
    if (r.flags.length) ev.push(`[${r.flags.join(', ')}]`);
    const verdict = r.verdict === 'unknown' ? `unknown ${r.reasons.join(',')}` : r.verdict === 'done' ? `done ${r.status}` : `legacy ${r.status || 'no-result'}`;
    const next = r.next === 'gate' ? `gate (${r.status})` : r.next === 'old-rule' ? (e.result ? 'old-rule (result file)' : "old-rule (HANDOFF's commit check)") : r.next;
    console.log(`${r.row} | ${r.attempt || '-'} | ${verdict} | ${ev.join(' · ')} | ${next}`);
    if (e.git_status) { console.log(`  git -C ${e.git} status --short:${e.git_status.length ? '' : ' (clean)'}`); for (const l of e.git_status) console.log(`    ${l}`); }
  }
}

// ---- activity (read-only liveness check for /orch-resume; the lease alone cannot tell a lease-less driver) ---------
// Signals, each with its time: (i) top-level result files (<row>-*.md) and PLAN.md, (ii) ledger.jsonl, (iii) the newest
// manifest event that is not a lease event (its ts: a lease release at rollover always touches the file), (iv) live agents
// of OTHER sessions: $PROJECTS/*/<sid>/subagents/agent-*.jsonl touched within ACTIVITY_AGENT_MIN whose brief (first user
// message only) or meta.json names the run. state is recent when any of (i)-(iii) is within --minutes or any (iv) exists,
// unless HANDOFF.md exists and no signal is newer than its mtime + ACTIVITY_HANDOFF_SLACK_SEC (a clean handoff: quiet).
const ACTIVITY_AGENT_MIN = 3, ACTIVITY_HANDOFF_SLACK_SEC = 120, BRIEF_READ_BYTES = 262144;
const RESULT_RE = /^\d+[a-z]?-.*\.md$/i;
const mtimeOf = f => { try { const s = fs.statSync(f); return s.isFile() ? s.mtimeMs : null; } catch { return null; } };
function briefText(file) { // the first user message of an agent transcript (string or text blocks), from a bounded read
  let buf; try { const fd = fs.openSync(file, 'r'); try { buf = Buffer.alloc(BRIEF_READ_BYTES); buf = buf.subarray(0, fs.readSync(fd, buf, 0, BRIEF_READ_BYTES, 0)); } finally { fs.closeSync(fd); } } catch { return ''; }
  const lines = buf.toString('utf8').split('\n');
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]; if (!l.trim()) continue;
    let x; try { x = JSON.parse(l); } catch { if (i === 0) return l.replace(/\\n/g, '\n').replace(/\\"/g, '"'); continue; } // a first line longer than the read: its raw text
    if (!x || x.type !== 'user' || !x.message) continue;
    const c = x.message.content;
    if (typeof c === 'string') return c;
    if (Array.isArray(c)) return c.map(b => (b && typeof b.text === 'string' ? b.text : '')).join('\n');
    return '';
  }
  return '';
}
// scanAgents(sid, now): one pass over $PROJECTS/*/<session>/subagents/agent-*.jsonl touched within ACTIVITY_AGENT_MIN, the
// caller's own session skipped -> [{path, session, mtime, meta, brief}] in walk order. resumable scans once for every run.
function scanAgents(sid, now) {
  const out = [], cut = now - ACTIVITY_AGENT_MIN * 60000;
  let projects = []; try { projects = fs.readdirSync(PROJECTS); } catch { return out; }
  for (const p of projects.sort()) {
    let sessions = []; try { sessions = fs.readdirSync(path.join(PROJECTS, p), { withFileTypes: true }).filter(d => d.isDirectory()).map(d => d.name); } catch { continue; }
    for (const s of sessions.sort()) {
      if (sid && s === sid) continue; // the caller's own agents (a resume's scouts) never count
      const dir = path.join(PROJECTS, p, s, 'subagents');
      let names = []; try { names = fs.readdirSync(dir); } catch { continue; }
      for (const f of names) {
        if (!/^agent-.*\.jsonl$/.test(f)) continue;
        const file = path.join(dir, f), mt = mtimeOf(file);
        if (mt == null || mt < cut) continue;
        let meta = null; try { meta = JSON.parse(fs.readFileSync(file.replace(/\.jsonl$/, '.meta.json'), 'utf8')); } catch {}
        out.push({ path: file, session: s, mtime: mt, meta, brief: briefText(file) });
      }
    }
  }
  return out;
}
function liveAgents(run, sid, now, scan = scanAgents(sid, now)) { // signal (iv): [{path, session, mtime, tier, why, agent}]
  const out = [], esc = run.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const attemptRe = new RegExp(`^[ \\t]*Orch-Attempt:[ \\t]*${esc}#`, 'm'), pathRe = new RegExp(`runs/${esc}(?![A-Za-z0-9._-])`), nameRe = new RegExp(`(?<![A-Za-z0-9._-])${esc}(?![A-Za-z0-9._-])`);
  for (const x of scan) {
    const meta = x.meta, md = meta && parseDescription(meta.description).attempt, ma = md ? parseAttemptId(md) : null, t = x.brief;
    let tier = null, why = null;
    if (ma && ma.run === run) { tier = 'strong'; why = `meta description ${ma.id}`; }
    else if (attemptRe.test(t)) { tier = 'strong'; why = 'brief has Orch-Attempt for the run'; }
    else if (pathRe.test(t)) { tier = 'strong'; why = `brief names runs/${run}`; }
    else if (nameRe.test(t)) { tier = 'weak'; why = 'brief names the run (no path, no attempt line)'; }
    if (tier) out.push({ path: x.path, session: x.session, mtime: x.mtime, tier, why, agent: meta && meta.agentType ? String(meta.agentType) : null });
  }
  return out.sort((a, b) => b.mtime - a.mtime);
}
const minutesOf = o => { // --minutes (default 15), shared by activity and resumable
  if (o.minutes === undefined) return 15;
  const m = Number(o.minutes);
  if (!/^\d+(\.\d+)?$/.test(String(o.minutes)) || !(m > 0) || m > 10080) fail(`--minutes '${o.minutes}': a number of minutes above 0 (at most 10080)`);
  return m;
};
// activityOf(run, sid, minutes, now[, scan]) -> {R (activity --json), sig, agents, hmt}. Pure read; the run folder is assumed to exist.
function activityOf(run, sid, minutes, now, scan) {
  const dir = runDir(run), sig = [];
  let names = []; try { names = fs.readdirSync(dir); } catch {}
  for (const f of names.sort()) {
    if (f !== 'PLAN.md' && !RESULT_RE.test(f)) continue; // HANDOFF*.md, ACTIVE, manifest.jsonl and folders (packs/) are not signals here
    const mt = mtimeOf(path.join(dir, f)); if (mt != null) sig.push({ kind: f === 'PLAN.md' ? 'plan' : 'result', path: path.join(dir, f), mtime: mt });
  }
  const lmt = mtimeOf(path.join(dir, 'ledger.jsonl')); if (lmt != null) sig.push({ kind: 'ledger', path: path.join(dir, 'ledger.jsonl'), mtime: lmt });
  let evNewest = null;
  for (const e of readEvents(run).events) { if (e.type === 'lease') continue; const t = Date.parse(e.ts); if (Number.isFinite(t) && (!evNewest || t > evNewest.t)) evNewest = { t, e }; }
  if (evNewest) sig.push({ kind: 'event', path: manifestFile(run), mtime: evNewest.t, detail: `${evNewest.e.type} ${evNewest.e.id}` });
  const agents = liveAgents(run, sid, now, scan || scanAgents(sid, now));
  for (const a of agents) sig.push({ kind: 'agent', path: a.path, mtime: a.mtime, tier: a.tier, session: a.session, detail: `${a.tier}, session ${sid8(a.session)}${a.agent ? `, ${a.agent}` : ''}: ${a.why}` });
  sig.sort((a, b) => b.mtime - a.mtime);
  const hmt = mtimeOf(path.join(dir, 'HANDOFF.md')), cutMs = now - minutes * 60000;
  const within = sig.filter(s => s.kind !== 'agent' && s.mtime >= cutMs);
  const afterHandoff = hmt == null ? sig : sig.filter(s => s.mtime > hmt + ACTIVITY_HANDOFF_SLACK_SEC * 1000);
  let state, reason;
  if (hmt != null && !afterHandoff.length) { state = 'quiet'; reason = `clean handoff: nothing newer than HANDOFF.md + ${ACTIVITY_HANDOFF_SLACK_SEC}s`; }
  else {
    const hits = hmt == null ? within : within.filter(s => afterHandoff.includes(s)), live = hmt == null ? agents.length : afterHandoff.filter(s => s.kind === 'agent').length;
    state = hits.length || live ? 'recent' : 'quiet';
    reason = state === 'recent' ? [hits.length ? `${hits.length} file/event signal(s) within ${minutes}m` : null, live ? `${live} live agent(s) of another session within ${ACTIVITY_AGENT_MIN}m` : null].filter(Boolean).join('; ')
      : `nothing within ${minutes}m and no live agent within ${ACTIVITY_AGENT_MIN}m`;
  }
  const ageMin = ms => Math.max(0, Math.floor((now - ms) / 60000));
  const J = s => Object.assign({ kind: s.kind, path: s.path, ts: isoAt(s.mtime), age_min: ageMin(s.mtime) }, s.detail ? { detail: s.detail } : {}, s.tier ? { tier: s.tier, session: s.session } : {});
  const tierOf = agents.length ? (agents.some(a => a.tier === 'strong') ? 'strong' : 'weak') : 'none';
  const R = { run, state, reason, minutes, agent_minutes: ACTIVITY_AGENT_MIN, now: isoAt(now), newest: sig.length ? J(sig[0]) : null, signals: sig.slice(0, 6).map(J),
    agents_live: tierOf, handoff: hmt != null ? isoAt(hmt) : null, sid: sid || null, sid_excluded: !!sid };
  return { R, sig, agents, hmt };
}
function activity(o) {
  onlyFlags('activity', o, ['run', 'minutes', 'sid', 'json']);
  if (o.run === undefined) fail('activity: needs --run');
  const run = V.run(o.run), dir = runDir(run), sid = sessionOf(o), now = Date.now();
  const minutes = minutesOf(o);
  let st; try { st = fs.statSync(dir); } catch {} if (!st || !st.isDirectory()) fail(`activity: run folder ${dir} not found`);
  const { R, sig, agents, hmt } = activityOf(run, sid, minutes, now);
  if (o.json) return console.log(JSON.stringify(R));
  const { state, reason } = R, tierOf = R.agents_live, ageMin = ms => Math.max(0, Math.floor((now - ms) / 60000));
  const line = s => `${s.kind} ${s.path} ${isoAt(s.mtime)} (${ageMin(s.mtime)}m ago)${s.detail ? `  [${s.detail}]` : ''}`;
  console.log(`state: ${state}`);
  console.log(`reason: ${reason}`);
  console.log(sig.length ? `newest: ${line(sig[0])}` : 'newest: none');
  for (const s of sig.slice(1, 6)) console.log(`  ${line(s)}`);
  console.log(`agent-live: ${tierOf}${agents.length ? ` (${agents.length} agent transcript(s) of other sessions touched within ${ACTIVITY_AGENT_MIN}m)` : ''}`);
  console.log(`handoff: ${hmt != null ? isoAt(hmt) : 'none'}`);
  console.log('lease: not consulted (lease check none does not mean idle)');
  if (!sid) console.log('note: no session id, so the caller\'s own agents are not excluded (pass --sid)');
}

// ---- resumable (read-only candidate list for an argument-less /orch-resume; OD-1 (A): it lists, the skill asks, nothing auto-picks) ----
// planDone(PLAN.md text) -> true when the `## Status` section's first table has data rows and none of their Status cells says
// pending, running, blocked or orphaned (W1). No PLAN.md, no Status section, no table or no data rows: not done.
const NOT_DONE_RE = /\b(pending|running|blocked|orphaned)\b/i;
// planStatusRows(PLAN.md text) -> [{row, status}] from the `## Status` section's first table (row: the '#' column, else the first,
// lower-cased), or null when there is no such table or it has no Status column. Shared by planDone and status.
function planStatusRows(txt) {
  if (typeof txt !== 'string') return null;
  const lines = txt.split(/\r?\n/), h = lines.findIndex(l => /^##\s+Status\b/i.test(l));
  if (h < 0) return null;
  let i = h + 1;
  while (i < lines.length && !/^##\s/.test(lines[i]) && !lines[i].trim().startsWith('|')) i++;
  if (i >= lines.length || !lines[i].trim().startsWith('|')) return null;
  const cells = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  const head = cells(lines[i]).map(c => c.toLowerCase()), col = head.indexOf('status'), idCol = Math.max(0, head.indexOf('#'));
  if (col < 0) return null;
  const rows = [];
  for (let j = i + 1; j < lines.length && lines[j].trim().startsWith('|'); j++) {
    if (/^\|[\s:|-]+$/.test(lines[j].trim())) continue; // the separator row
    const c = cells(lines[j]);
    rows.push({ row: String(c[idCol] || '').toLowerCase(), status: c[col] || '' });
  }
  return rows;
}
function planDone(txt) {
  const rows = planStatusRows(txt);
  return !!rows && rows.length > 0 && !rows.some(r => NOT_DONE_RE.test(r.status));
}
const runsLimitOf = (o, dflt) => { // --limit, shared by resumable and runs
  if (o.limit === undefined) return dflt;
  const n = Number(o.limit); if (!/^\d+$/.test(String(o.limit)) || n < 1 || n > 50) fail(`--limit '${o.limit}': a whole number of runs from 1 to 50`);
  return n;
};
const runFolders = () => { try { return fs.readdirSync(RUNS, { withFileTypes: true }).filter(d => d.isDirectory() && RUN_OK(d.name)).map(d => d.name); } catch { return []; } };
// resumableCore({sid, now, minutes, limit[, scan]}) -> {candidates, excluded, checked, done, scan}: resumable's data, shared with runs.
// excluded holds the not-done exclusions only; done the done runs inspected on the way. Pure read.
function resumableCore({ sid, now, minutes, limit, scan = null }) {
  const readTxt = f => { try { return fs.readFileSync(f, 'utf8'); } catch { return null; } };
  const names = runFolders();
  const pool = [];
  for (const run of names) {
    const hmt = mtimeOf(path.join(runDir(run), 'HANDOFF.md')), pmt = mtimeOf(path.join(runDir(run), 'PLAN.md'));
    if (hmt != null) pool.push({ run, kind: 'handoff', mtime: hmt }); else if (pmt != null) pool.push({ run, kind: 'plan', mtime: pmt });
  }
  pool.sort((a, b) => (a.kind === b.kind ? 0 : a.kind === 'handoff' ? -1 : 1) || (b.mtime - a.mtime) || (a.run < b.run ? -1 : a.run > b.run ? 1 : 0));
  const ageMin = ms => Math.max(0, Math.floor((now - ms) / 60000));
  const candidates = [], excluded = [], done = [];
  let checked = 0, found = 0;
  for (const c of pool) {
    if (found >= limit) break;
    checked++;
    const base = { run: c.run, kind: c.kind, mtime: isoAt(c.mtime), age_min: ageMin(c.mtime) };
    try {
      const plan = readTxt(path.join(runDir(c.run), 'PLAN.md'));
      if (planDone(plan)) { done.push(Object.assign(base, { reason: 'done' })); continue; }
      found++;
      const hf = c.kind === 'handoff' ? frontmatter(path.join(runDir(c.run), 'HANDOFF.md')) : null, pf = frontmatter(path.join(runDir(c.run), 'PLAN.md'));
      const preset = (hf && hf.preset) || (pf && pf.preset) || null;
      const L = leaseState(sid, c.run, now);
      if (!scan) scan = scanAgents(sid, now); // one transcript scan per call
      const A = activityOf(c.run, sid, minutes, now, scan).R;
      Object.assign(base, { preset, lease: L.state, activity: A.state });
      if (L.state === 'other') excluded.push(Object.assign(base, { reason: 'lease-other', holder: L.lease.session, until: L.lease.expires || null }));
      else if (A.state === 'recent') excluded.push(Object.assign(base, { reason: 'activity-recent', newest: A.newest ? { kind: A.newest.kind, age_min: A.newest.age_min } : null }));
      else candidates.push(base);
    } catch (e) { excluded.push(Object.assign(base, { reason: 'unreadable', error: String(e && e.message || e).slice(0, 200) })); }
  }
  return { candidates, excluded, checked, done, scan };
}
function resumableLines(R, sid) { // the text resumable prints, line by line (runs prints the same lines)
  const out = [], ex = [...R.excluded, ...R.done];
  for (const c of R.candidates) out.push(`resumable: ${c.run}  ${c.kind} ${c.mtime} (${c.age_min}m)  preset ${c.preset || '?'}  lease ${c.lease}`);
  for (const x of ex) {
    const why = x.reason === 'done' ? 'done'
      : x.reason === 'lease-other' ? `lease other: holder ${sid8(x.holder)} until ${hhmm(x.until)}, activity ${x.activity}`
      : x.reason === 'activity-recent' ? `activity recent: ${x.newest ? `${x.newest.kind} ${x.newest.age_min}m` : '?'}`
      : `unreadable: ${x.error}`;
    out.push(`excluded: ${x.run}  ${why}`);
  }
  out.push(`candidates: ${R.candidates.length} of ${R.checked} checked`);
  if (!sid) out.push('note: no session id, so every live lease reads as other and the caller\'s own agents are not excluded (pass --sid)');
  return out;
}
const resumableJson = R => ({ candidates: R.candidates, excluded: [...R.excluded, ...R.done], checked: R.checked });
function resumable(o) {
  onlyFlags('resumable', o, ['sid', 'limit', 'minutes', 'json']);
  const sid = sessionOf(o), now = Date.now(), minutes = minutesOf(o), limit = runsLimitOf(o, 6);
  const R = resumableCore({ sid, now, minutes, limit });
  if (o.json) return console.log(JSON.stringify(resumableJson(R)));
  for (const l of resumableLines(R, sid)) console.log(l);
}

// ---- runs (read-only overview: live leases, resumable's lines, finished runs, other folders; no lock, no ingest, no write) ----
function runs(o) {
  onlyFlags('runs', o, ['sid', 'limit', 'minutes', 'json']);
  const sid = sessionOf(o), now = Date.now(), minutes = minutesOf(o), limit = runsLimitOf(o, 6);
  const R = resumableCore({ sid, now, minutes, limit });
  let scan = R.scan; const scanOnce = () => scan || (scan = scanAgents(sid, now)); // one transcript scan per call
  const live = LS().listLeases(ORCH_DIR).filter(x => LS().isLive(x.lease, now)).map(x => {
    let activity = 'no-folder';
    try { if (fs.statSync(runDir(x.run)).isDirectory()) activity = activityOf(x.run, sid, minutes, now, scanOnce()).R.state; } catch {}
    return { run: x.run, holder: x.lease.session || null, mine: !!sid && x.lease.session === sid, until: x.lease.expires || null, activity, legacy: !!x.legacy };
  });
  const finished = [], other = [];
  for (const run of runFolders().sort()) {
    const pmt = mtimeOf(path.join(runDir(run), 'PLAN.md')), hmt = mtimeOf(path.join(runDir(run), 'HANDOFF.md'));
    if (pmt == null && hmt == null) { other.push(run); continue; }
    let txt = null; try { txt = fs.readFileSync(path.join(runDir(run), 'PLAN.md'), 'utf8'); } catch {}
    if (planDone(txt)) finished.push({ run, mtime: Math.max(pmt || 0, hmt || 0) });
  }
  finished.sort((a, b) => (b.mtime - a.mtime) || (a.run < b.run ? -1 : a.run > b.run ? 1 : 0));
  const shown = finished.slice(0, limit).map(f => { // open: attempts with no final stop (old data); the done plan still wins
    let open = null; try { open = fold(f.run, { sid, now }).attempts.filter(a => OPEN.includes(a.state)).length; } catch {}
    return { run: f.run, ts: isoAt(f.mtime), open };
  });
  if (o.json) return console.log(JSON.stringify({ live, resumable: resumableJson(R), finished: shown, finished_total: finished.length, other: { count: other.length, names: other.slice(0, 5) } }));
  if (!live.length) console.log('live: none');
  for (const x of live) console.log(`live: ${x.run}  holder ${sid8(x.holder)}${x.mine ? ' (this session)' : ''}  until ${hhmm(x.until)}  activity ${x.activity}${x.legacy ? '  (legacy ACTIVE.lease)' : ''}`);
  for (const l of resumableLines(R, sid)) console.log(l);
  if (!shown.length) console.log('finished: none');
  for (const f of shown) console.log(`finished: ${f.run}  ${f.ts}${f.open ? `  (${f.open} open attempt(s) with no stop event)` : ''}`);
  if (finished.length > shown.length) console.log(`finished: ${finished.length - shown.length} more (--limit up to 50)`);
  console.log(`other: ${other.length} folders without PLAN.md or HANDOFF.md${other.length ? `: ${other.slice(0, 5).join(', ')}${other.length > 5 ? ', ...' : ''}` : ''}`);
}

// ---- doctor (the checks live in doctor.js, loaded only by this verb; deps are passed in, nothing is re-derived there) --------
function doctor(o) {
  onlyFlags('doctor', o, ['sid', 'check', 'fix', 'json']);
  return require(path.join(__dirname, 'doctor.js')).run({ sid: sessionOf(o), check: o.check, fix: o.fix, json: !!o.json },
    { ORCH_DIR, RUNS, LS: LS(), releaseRun, leaseState, activityOf, scanAgents, planDone, frontmatter, fold, ROLES, DEFAULTS, RUN_OK, sid8, hhmm, fail });
}

// ---- status (read-only per-row view of one run, or one line per run folder; no lock, no ingest, no write) --------------
const fmtTok = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n); // ledger.js formats
const fmtMs = n => n >= 36e5 ? (n / 36e5).toFixed(1) + 'h' : n >= 6e4 ? (n / 6e4).toFixed(1) + 'm' : Math.round(n / 1e3) + 's';
const sumOf = (xs, k) => { const p = { n: xs.length, sum: 0, unknown: 0 }; for (const x of xs) { const v = x[k]; if (typeof v === 'number' && Number.isFinite(v)) p.sum += v; else p.unknown++; } return p; };
const fmtPart = (p, f) => !p.n ? '-' : p.unknown === p.n ? '?' : f(p.sum) + (p.unknown ? '?' : ''); // '?' marks a sum with unknown lines in it
const ROW_ID = /^\d{1,4}[a-z]?$/;
const isDoneStatus = s => { const w = /^[a-z_-]+/.exec(String(s || '').trim().toLowerCase()); return !!w && (cfg.passStatuses.map(String).includes(w[0]) || ['finished', 'closed', 'skipped', 'dropped'].includes(w[0])); }; // same done words as NOT_DONE_RE lets through on a plan cell
function statusOf(run, sid, now) { // -> the per-row view; reads PLAN.md, manifest.jsonl, ledger.jsonl, result files and the lease
  const planTxt = (() => { try { return fs.readFileSync(path.join(runDir(run), 'PLAN.md'), 'utf8'); } catch { return null; } })();
  const pf = planTxt != null ? frontmatter(path.join(runDir(run), 'PLAN.md')) : null;
  const cellOf = new Map();
  for (const r of planStatusRows(planTxt) || []) if (ROW_ID.test(r.row) && !cellOf.has(r.row)) cellOf.set(r.row, r.status);
  const F = fold(run, { sid, now }), ledger = readLedger(run).filter(x => x.run === run);
  const ids = new Set(cellOf.keys());
  for (const a of F.attempts) if (a.row != null) ids.add(String(a.row).toLowerCase());
  for (const x of ledger) ids.add(String(x.row).toLowerCase());
  const rows = [...ids].sort(rowCmp).map(row => {
    const atts = F.attempts.filter(a => String(a.row).toLowerCase() === row).sort((x, y) => (x.n || 0) - (y.n || 0));
    const lat = last(atts), L = lat && lat.launch, lines = ledger.filter(x => String(x.row).toLowerCase() === row);
    const lastLine = last(lines), sub = (last(atts.filter(a => a.launch && a.launch.subtask)) || {}).launch;
    const res = resultFor(run, row, L, lat ? lat.id : null, atts.length);
    const cell = cellOf.has(row) ? cellOf.get(row) : null;
    return { row, subtask: sub ? sub.subtask : null, status: cell != null && cell !== '' ? cell : lat ? lat.state : null, status_from: cell != null && cell !== '' ? 'plan' : lat ? 'attempt' : null,
      attempts: Math.max(atts.length, lines.length), latest: lat ? lat.id : lastLine && typeof lastLine.id === 'string' ? lastLine.id : null,
      model: L && L.model ? L.model : lastLine ? lastLine.model || null : null, effort: L && L.effort ? L.effort : lastLine ? lastLine.effort || null : null,
      sha: res && res.fm.sha ? res.fm.sha : null, result: res ? res.path : null, open: atts.some(a => OPEN.includes(a.state)),
      tokens: sumOf(lines, 'tokens'), ms: sumOf(lines, 'ms') };
  });
  // a plan cell counts as done like planDone does (closed/skipped/dropped too); a status taken from an attempt state uses isDoneStatus (same words, unknown states stay not done)
  for (const r of rows) r.done = r.status_from === 'plan' ? !NOT_DONE_RE.test(r.status) : isDoneStatus(r.status);
  return { run, preset: pf && pf.preset ? pf.preset : null, plan: planTxt != null, rows_done: rows.filter(r => r.done).length, rows_total: rows.length,
    open: F.attempts.filter(a => OPEN.includes(a.state)).length, tokens: sumOf(ledger, 'tokens'), ms: sumOf(ledger, 'ms'), rows, lease: F.lease };
}
function padTable(rows) { const w = rows[0].map((_, i) => Math.max(...rows.map(r => String(r[i]).length))); return rows.map(r => r.map((c, i) => i === r.length - 1 ? String(c) : String(c).padEnd(w[i])).join('  ')); }
const leaseWord = S => S.state === 'none' ? 'none' : S.state === 'mine' ? 'mine' : `${S.state} ${sid8(S.holder)}`;
const clip = (s, n) => s.length > n ? s.slice(0, n - 1) + '~' : s;
function status(o) {
  onlyFlags('status', o, ['run', 'limit', 'sid', 'json']);
  const sid = sessionOf(o), now = Date.now();
  if (o.run !== undefined) {
    if (o.limit !== undefined) fail('status: --limit lists runs; it does not go with --run');
    const run = V.run(o.run);
    let st; try { st = fs.statSync(runDir(run)); } catch {} if (!st || !st.isDirectory()) fail(`status: run folder ${runDir(run)} not found`);
    const S = statusOf(run, sid, now);
    if (o.json) return console.log(JSON.stringify(S));
    console.log(`status: run ${run}  preset ${S.preset || '?'}  plan ${S.plan ? 'yes' : 'no'}  rows ${S.rows_done}/${S.rows_total}`);
    if (S.rows.length) for (const l of padTable([['row', 'subtask', 'status', 'attempts', 'latest', 'model/effort', 'sha', 'tokens', 'time'],
      ...S.rows.map(r => [r.row, r.subtask ? clip(r.subtask, 40) : '-', r.status || '-', r.attempts, r.latest || '-', r.model || r.effort ? `${r.model || '?'}/${r.effort || '?'}` : '-',
        r.sha ? (/^[0-9a-f]{8,40}$/i.test(r.sha) ? r.sha.slice(0, 7) : r.sha) : '-', fmtPart(r.tokens, fmtTok), fmtPart(r.ms, fmtMs)])])) console.log(l);
    console.log(leaseLine(S.lease));
    return;
  }
  let limit = 20;
  if (o.limit !== undefined) { limit = Number(o.limit); if (!/^\d+$/.test(String(o.limit)) || limit < 1 || limit > 50) fail(`--limit '${o.limit}': a whole number of runs from 1 to 50`); }
  let names = []; try { names = fs.readdirSync(RUNS, { withFileTypes: true }).filter(d => d.isDirectory() && RUN_OK(d.name)).map(d => d.name); } catch {}
  const all = names.map(run => {
    const S = statusOf(run, sid, now);
    const mt = Math.max(...['', 'PLAN.md', 'manifest.jsonl', 'ledger.jsonl'].map(f => mtimeOf(path.join(runDir(run), f)) || 0));
    return { run, done: S.rows_total > 0 && S.rows_done === S.rows_total, rows_done: S.rows_done, rows_total: S.rows_total, open: S.open, tokens: S.tokens, mtime: mt, lease: S.lease };
  });
  all.sort((a, b) => (a.done === b.done ? 0 : a.done ? 1 : -1) || (b.mtime - a.mtime) || (a.run < b.run ? -1 : a.run > b.run ? 1 : 0));
  const shown = all.slice(0, limit);
  if (o.json) return console.log(JSON.stringify({ runs: shown.map(x => Object.assign({}, x, { mtime: x.mtime ? isoAt(x.mtime) : null })), total: all.length }));
  if (!shown.length) return console.log('status: no run folders');
  for (const l of padTable(shown.map(x => [x.run, `rows ${x.rows_done}/${x.rows_total}`, `open ${x.open}`, `tokens ${fmtPart(x.tokens, fmtTok)}`, `lease ${leaseWord(x.lease)}`]))) console.log(l);
  if (all.length > shown.length) console.log(`(${all.length - shown.length} more; --limit up to 50)`);
}

// void --attempt <id> [--reason <text>] [--sid S]: mark an attempt that was allocated (launch event) but never launched, e.g. an ID handed out by a
// row-number collision. Appends a `void` event; fold, reconcile, launch's reuse rule and the progress items then skip it. Refused (exit 2) once the
// attempt has a start or stop event or a ledger line (it ran: close it with ledger.js close). A repeat is a no-op. The ID is never handed out again.
function voidCmd(o) {
  onlyFlags('void', o, ['attempt', 'reason', 'sid']);
  if (o.attempt === undefined) fail('void: needs --attempt <run>#<row>.<n>');
  const a = parseAttemptId(o.attempt);
  if (!a) fail(`--attempt '${o.attempt}': an attempt ID such as my-run#3.2 (<run>#<row>.<n>)`);
  V.run(a.run);
  const reason = o.reason !== undefined ? V.text('reason', 200)(o.reason) : null, sid = sessionOf(o);
  if (!fs.existsSync(manifestFile(a.run))) fail(`void: run ${a.run} has no manifest (${manifestFile(a.run)})`);
  const res = withLock(manifestFile(a.run), cfg.lockWaitMsCli, () => {
    const g = groupByAttempt(readEvents(a.run).events).get(a.id);
    if (!g || !g.launches.length) refuse(`void: ${a.id} has no launch event in run ${a.run}; nothing to void`);
    if (g.starts.length || g.stops.length) refuse(`void: ${a.id} has start/stop events, so it ran; record its outcome with ledger.js close --attempt ${a.id} instead`);
    if (readLedger(a.run).some(x => x.id === a.id)) refuse(`void: ${a.id} has a ledger line, so it ran; record its outcome with ledger.js close --attempt ${a.id} instead`);
    if (g.voids.length) return 'already';
    appendLocked(a.run, { type: 'void', attempt: a.id, row: a.row, key: `${a.id}:void`, via: 'cli', session: sid, reason });
    return 'voided';
  });
  console.log(`${a.id} ${res === 'already' ? 'already voided' : 'voided'}`);
}

// progress --run R [--rebuild] [--json]: the run's boss.progress/1 snapshot. Without --rebuild it only reads (and says when the snapshot is
// behind the manifest); --rebuild replays every manifest event under the manifest lock and rewrites it (crash recovery, runs that began before
// the publisher existed). Exit 0 ok, 2 usage, 1 on a write failure.
function progressCmd(o) {
  onlyFlags('progress', o, ['run', 'rebuild', 'json']);
  if (o.run === undefined) fail('progress: needs --run');
  const run = V.run(o.run);
  if (!fs.existsSync(runDir(run))) fail(`progress: run folder ${runDir(run)} not found`);
  if (!progressOn()) fail('progress: publishing is off (config progress:false, ORCH_PROGRESS=off, or a scratch ORCH_DIR without BOSS_PROGRESS_DIR)');
  const { PH } = loadProgressLibs(), base = progressBase(), dir = PH.runDir('orchestrator', run, base);
  let snap, file = path.join(dir, 'progress.json');
  if (o.rebuild) {
    if (!fs.existsSync(manifestFile(run))) fs.writeFileSync(manifestFile(run), '', { flag: 'a' });
    const r = withLock(manifestFile(run), cfg.lockWaitMsCli, () => publishProgress(run, readEvents(run).events, { strict: true }));
    snap = r.snap; file = r.file;
  } else snap = PH.readSnapshot(dir);
  if (!snap) return console.log(`progress: none for ${run} (run: manifest.js progress --run ${run} --rebuild)`);
  if (o.json) return console.log(JSON.stringify(snap));
  const max = readEvents(run).maxSeq;
  console.log(`progress: ${run}  state ${snap.state}  seq ${snap.seq}/${max}${snap.seq < max ? '  (behind the manifest: --rebuild)' : ''}  ${snap.summary || ''}`.trimEnd());
  console.log(`file: ${file}`);
}

// ---- entry -------------------------------------------------------------------------
function main(argv) {
  const cmd = argv[0];
  if (!cmd || cmd === 'help' || cmd === '--help' || cmd === '-h') return console.log(HELP);
  if (cmd === 'lease') {
    const sub = { acquire: [leaseAcquire, ['force']], renew: [leaseRenew, []], release: [leaseRelease, ['force']], check: [leaseCheck, ['json']] }[argv[1]];
    if (!sub) fail(`lease: unknown subcommand '${argv[1] || ''}' (acquire | renew | release | check)`);
    if (cfgError) console.error(`manifest: ${cfgError}`);
    return sub[0](parseFlags(argv.slice(2), sub[1]));
  }
  const spec = { launch: [launch, ['new', 'tune', 'verbose']], void: [voidCmd, []], event: [eventCmd, []], show: [show, ['json']], reconcile: [reconcile, ['json']], activity: [activity, ['json']], resumable: [resumable, ['json']], status: [status, ['json']], runs: [runs, ['json']], doctor: [doctor, ['json']], progress: [progressCmd, ['rebuild', 'json']] }[cmd];
  if (!spec) fail(`unknown command '${cmd}' (launch | void | event | show | lease | reconcile | activity | resumable | help); read-only: status | runs | doctor`);
  if (cfgError) console.error(`manifest: ${cfgError}`);
  spec[0](parseFlags(argv.slice(1), spec[1]));
}
module.exports = { main, ORCH_DIR, RUNS, PROJECTS, cfg, parseAttemptId, parseDescription, appendEvent, readEvents, fold, openAttemptIds, hasLaunch, runIngest, metaFor, withLock,
  leaseState, renewIfMine, heldLeases };
if (require.main === module) {
  try { main(process.argv.slice(2)); } catch (e) {
    if (e instanceof UsageError) { console.error(`manifest: error: ${e.message}\n(usage: node "$BOSS_LIB/manifest.js" help)`); process.exit(2); }
    if (e instanceof Refused) { console.error(`manifest: ${e.message}`); process.exit(2); }
    if (e instanceof LeaseConflict) { console.error(`manifest: ${e.message}`); process.exit(3); }
    if (e instanceof Stopped) { console.error(`manifest: ${e.message}`); process.exit(4); }
    console.error(`manifest: failed: ${e && e.message}`); process.exit(1);
  }
}
