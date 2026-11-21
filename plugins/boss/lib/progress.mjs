// boss.progress/1 helpers (node, no deps). Mirrors progress.py; spec: PROGRESS.md in this folder.
import fs from 'node:fs';
import path from 'node:path';
import paths from './paths.js'; // the one path resolver (CommonJS; default import)

export const SCHEMA = 'boss.progress/1';
export const MAJOR = 1;
export const TERMINAL = ['done', 'failed', 'cancelled'];
const SAFE = /^[A-Za-z0-9._-]{1,128}$/;
const TOOL = /^[a-z][a-z0-9-]{0,31}$/;
export const MAX_BYTES = 512 * 1024;
const CAPS = { title: 200, summary: 500, label: 200, error: 2000, detail: 2000, text: 1000 };
export const STALL_MIN_S = 60, STALL_FACTOR = 5, NO_PROGRESS_S = 300, RETAIN_DAYS = 7;
const CTRL = /\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]/g;
const CLK_TCK = 100; // Linux USER_HZ; node has no sysconf

// ---------- paths ----------
export const root = () => paths.progressRoot();
function mkdir(p) { fs.mkdirSync(p, { recursive: true }); fs.chmodSync(p, 0o700); }
export function runDir(tool, runId, base) {
  if (!TOOL.test(tool || '') || !SAFE.test(runId || '') || runId === '.' || runId === '..')
    throw new Error(`unsafe tool/run_id: ${tool}/${runId}`);
  return path.join(base || root(), 'runs', tool, runId);
}
export const iso = (ms = Date.now()) => new Date(ms).toISOString();
export function parseTs(s) { if (!s) return null; const t = Date.parse(s); return Number.isNaN(t) ? null : t / 1000; }

// ---------- /proc ----------
export function procStartTicks(pid) {
  let data;
  try { data = fs.readFileSync(`/proc/${Number(pid)}/stat`, 'utf8'); } catch { return null; }
  return Number(data.slice(data.lastIndexOf(')') + 2).split(' ')[19]); // comm may hold spaces and ')'
}
export function bootId() {
  try { return fs.readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim(); } catch { return null; }
}
function procStartEpoch(pid) {
  const ticks = procStartTicks(pid);
  if (ticks == null) return null;
  try {
    const m = fs.readFileSync('/proc/stat', 'utf8').match(/^btime (\d+)$/m);
    return m ? Number(m[1]) + ticks / CLK_TCK : null;
  } catch { return null; }
}
export function writerIdentity(kind = 'process', pid = process.pid) {
  return { pid, start_ticks: procStartTicks(pid), boot_id: bootId(), kind };
}

// ---------- writer ----------
function clip(s, n) {
  if (typeof s !== 'string') return s;
  s = s.replace(CTRL, '');
  return s.length <= n ? s : s.slice(0, n - 1) + '…';
}
export function clean(snap) {
  for (const k of ['title', 'summary']) if (k in snap) snap[k] = clip(snap[k], CAPS[k]);
  for (const it of snap.items || []) for (const k of ['label', 'error', 'detail']) if (k in it) it[k] = clip(it[k], CAPS[k]);
  for (const a of snap.attention || []) a.text = clip(a.text, CAPS.text);
  return snap;
}
export function register(tool, runId, { sourceDir = null, title = null, base } = {}) {
  const d = runDir(tool, runId, base);
  for (const p of [base || root(), path.dirname(path.dirname(d)), path.dirname(d), d]) mkdir(p);
  let fd;
  try { fd = fs.openSync(path.join(d, 'run.json'), 'wx', 0o600); } catch (e) { if (e.code === 'EEXIST') return d; throw e; }
  fs.writeSync(fd, JSON.stringify({ schema: SCHEMA, tool, run_id: runId, created_at: iso(), source_dir: sourceDir, title: clip(title, CAPS.title) }));
  fs.closeSync(fd);
  return d;
}
export function writeSnapshot(snap, { base } = {}) {
  snap.schema ??= SCHEMA;
  snap.updated_at ??= iso();
  const d = register(snap.tool, snap.run_id, { sourceDir: snap.source_dir ?? null, title: snap.title ?? null, base });
  const data = Buffer.from(JSON.stringify(clean(snap)));
  if (data.length > MAX_BYTES) throw new Error(`snapshot ${data.length} bytes > ${MAX_BYTES} cap`);
  const tmp = path.join(d, `.progress.json.${process.pid}.tmp`);
  fs.writeFileSync(tmp, data, { mode: 0o600 });
  fs.chmodSync(tmp, 0o600);
  fs.renameSync(tmp, path.join(d, 'progress.json'));
  return path.join(d, 'progress.json');
}
export function prune({ base, days = RETAIN_DAYS, now = Date.now() / 1000 } = {}) {
  const runs = path.join(base || root(), 'runs'); const removed = [];
  if (!fs.existsSync(runs)) return removed;
  for (const tool of fs.readdirSync(runs)) for (const rid of fs.readdirSync(path.join(runs, tool))) {
    const d = path.join(runs, tool, rid);
    const ref = [path.join(d, 'progress.json'), path.join(d, 'run.json')].find((p) => fs.existsSync(p)) || d;
    if (now - fs.statSync(ref).mtimeMs / 1000 > days * 86400) { fs.rmSync(d, { recursive: true, force: true }); removed.push(d); }
  }
  return removed;
}

// ---------- reader ----------
export function listRuns(base) {
  const runs = path.join(base || root(), 'runs');
  if (!fs.existsSync(runs)) return [];
  return fs.readdirSync(runs).sort().flatMap((t) => fs.readdirSync(path.join(runs, t)).sort().map((r) => path.join(runs, t, r)));
}
export function readSnapshot(d) {
  try { return JSON.parse(fs.readFileSync(path.join(d, 'progress.json'), 'utf8')); } catch { return null; }
}
export function major(snap) {
  const m = /^boss\.progress\/(\d+)$/.exec(String(snap?.schema ?? ''));
  return m ? Number(m[1]) : null;
}

// THE ONE adapter onto the config dir's sessions/ folder. Reads <pid>.json only: never *.key, never a socket.
export function sessionState(owner, sessionsDir = paths.sessionsDir()) {
  const { session_id: sid, pid, start_ticks: st } = owner || {};
  let names = [`${pid}.json`];
  if (!pid) { try { names = fs.readdirSync(sessionsDir).filter((n) => /^\d+\.json$/.test(n)).sort(); } catch { names = []; } }
  let doc = null;
  for (const n of names) {
    let d; try { d = JSON.parse(fs.readFileSync(path.join(sessionsDir, n), 'utf8')); } catch { continue; }
    if (!sid || d.sessionId === sid) { doc = d; break; }
  }
  if (!doc || !Number.isInteger(doc.pid)) return null;
  if (st != null) { if (procStartTicks(doc.pid) !== st) return null; }
  else {
    const start = procStartEpoch(doc.pid);
    if (start == null) return null;
    if (doc.startedAt && Math.abs(start - doc.startedAt / 1000) > 30) return null; // pid reused
  }
  return { pid: doc.pid, status: ['busy', 'idle'].includes(doc.status) ? doc.status : 'unknown' };
}
export const defaultProbe = (sessionsDir) => ({ bootId, startTicks: procStartTicks, session: (o) => sessionState(o, sessionsDir) });

export function liveness(snap, { now = Date.now() / 1000, probe = defaultProbe(), resumedAt = null } = {}) {
  const out = { status: 'unknown', reason: null, heartbeat_age_s: null, no_progress_s: null, quiet: false };
  const r1 = (x) => Math.round(x * 10) / 10;
  if (major(snap) !== MAJOR) return { ...out, status: 'unsupported', reason: `schema ${snap?.schema}` };
  if (TERMINAL.includes(snap.state)) return { ...out, status: 'ended', reason: snap.state };
  const lp = parseTs(snap.last_progress_at);
  if (lp != null) out.no_progress_s = r1(now - lp);
  // Paused on purpose (orchestrator lease released before the plan finished): not live, not dead, nothing to watch.
  if (snap.paused === true) return { ...out, status: 'waiting_input', reason: 'paused by owner' };
  const w = snap.writer || {};
  if (w.kind === 'oneshot') {
    if (!snap.owner) return { ...out, reason: 'no owner session recorded' };
    const s = probe.session(snap.owner);
    if (!s) return { ...out, status: 'dead', reason: 'owner session gone (orphaned)' };
    out.status = { busy: 'running', idle: 'waiting_input' }[s.status] || 'unknown';
    out.reason = `owner session ${s.status}`;
  } else {
    const b = probe.bootId();
    if (w.boot_id && b && w.boot_id !== b) return { ...out, status: 'dead', reason: 'writer from an earlier boot' };
    const ticks = probe.startTicks(w.pid);
    if (ticks == null) return { ...out, status: 'dead', reason: `writer pid ${w.pid} not running` };
    if (w.start_ticks != null && ticks !== w.start_ticks) return { ...out, status: 'dead', reason: `writer pid ${w.pid} reused` };
    const hb = parseTs(snap.heartbeat_at || snap.updated_at);
    out.heartbeat_age_s = hb == null ? null : r1(now - hb);
    const stall = Math.max(STALL_MIN_S, STALL_FACTOR * Number(snap.heartbeat_interval_s || 0));
    if (hb != null && now - hb > stall) {
      if (resumedAt != null && now - resumedAt < stall) { out.status = 'running'; out.reason = 'resume grace'; }
      else return { ...out, status: 'stalled', reason: `no heartbeat for ${Math.floor(now - hb)}s` };
    } else out.status = snap.state === 'waiting_input' ? 'waiting_input' : 'running';
  }
  if (out.status === 'running' && out.no_progress_s != null && out.no_progress_s > NO_PROGRESS_S) out.quiet = true;
  return out;
}

export function claimNotification(tool, runId, key, { base } = {}) {
  if (!SAFE.test(key || '')) throw new Error(`unsafe claim key ${key}`);
  const d = path.join(runDir(tool, runId, base), 'claims');
  mkdir(d);
  try { fs.writeFileSync(path.join(d, key), JSON.stringify({ pid: process.pid, at: iso() }), { flag: 'wx', mode: 0o600 }); return true; }
  catch (e) { if (e.code === 'EEXIST') return false; throw e; }
}

// ---------- validation (keyword subset; see PROGRESS.md) ----------
const isType = (v, t) => ({
  object: () => v !== null && typeof v === 'object' && !Array.isArray(v), array: () => Array.isArray(v),
  string: () => typeof v === 'string', boolean: () => typeof v === 'boolean', null: () => v === null,
  integer: () => Number.isInteger(v), number: () => typeof v === 'number',
}[t]());
export function validate(doc, schema, node = schema, p = '$') {
  if (node.$ref) node = schema.$defs[node.$ref.split('/').pop()];
  const errs = [];
  if (node.type) {
    const types = [].concat(node.type);
    if (!types.some((t) => isType(doc, t))) return [`${p}: expected ${types.join('|')}`];
  }
  if (node.enum && !node.enum.includes(doc)) errs.push(`${p}: ${JSON.stringify(doc)} not in enum`);
  if (typeof doc === 'string') {
    if (node.maxLength != null && [...doc].length > node.maxLength) errs.push(`${p}: longer than ${node.maxLength}`);
    if (node.pattern && !new RegExp(node.pattern, 'u').test(doc)) errs.push(`${p}: does not match ${node.pattern}`);
  }
  if (typeof doc === 'number' && node.minimum != null && doc < node.minimum) errs.push(`${p}: below ${node.minimum}`);
  if (isType(doc, 'object')) {
    for (const k of node.required || []) if (!(k in doc)) errs.push(`${p}: missing ${k}`);
    for (const [k, sub] of Object.entries(node.properties || {})) if (k in doc) errs.push(...validate(doc[k], schema, sub, `${p}.${k}`));
  }
  if (Array.isArray(doc)) {
    if (node.maxItems != null && doc.length > node.maxItems) errs.push(`${p}: more than ${node.maxItems} items`);
    if (node.items) doc.forEach((v, i) => errs.push(...validate(v, schema, node.items, `${p}[${i}]`)));
  }
  return errs;
}
