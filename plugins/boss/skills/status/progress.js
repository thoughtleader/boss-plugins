#!/usr/bin/env node
// Progress view for /boss:status (skills/start/PROGRESS-VIEW.md, loaded from PROTOCOL.md "Progress view"). Read-only.
// usage: node "${CLAUDE_PLUGIN_ROOT}/skills/status/progress.js" <run folder name or path> [--details]
// Sources: lib/manifest.js `status --run <run> --json` (rows, lease, tokens), <run>/PLAN.md's plan table (subtask, wave,
// model/effort, agent type, in plan order), <run>/manifest.jsonl (launch/start events: subtask, role and last-activity
// time for runs without a plan table), and the ledger rows manifest.js already folded in.
// Row state comes from the row's attempts (`manifest.js show --run <run> --json`, latest attempt wins); PLAN.md's Status
// column only decides rows that have no attempts (and a plan cell that says blocked still blocks a finished attempt).
'use strict';
// The default is the shared, bounded meter. Keep the former task report on demand.
if (!process.argv.includes('--details')) {
  require('./compact.js');
  return;
}
const fs = require('fs'), path = require('path');
const { execFileSync } = require('child_process');
const paths = require(path.resolve(__dirname, '../../lib/paths.js'));

const MANIFEST = path.join(paths.pluginRoot, 'lib', 'manifest.js');
const runArg = process.argv.slice(2).find(a => !a.startsWith('--'));
const run = runArg ? paths.runName(runArg) : runArg; // a run folder path under the runs dir works as well as the name
if (!run || !/^[A-Za-z0-9._-]+$/.test(run)) { console.error(`progress.js: bad run '${runArg === undefined ? '' : runArg}': give the run folder name, or the path of a run folder under ${paths.runsDir()}`); process.exit(2); }

let st;
try {
  st = JSON.parse(execFileSync(process.execPath, [MANIFEST, 'status', '--run', run, '--json'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
} catch (e) { console.error(`progress: manifest.js status failed for ${run}: ${String(e.stderr || e.message).trim().split('\n')[0]}`); process.exit(2); }

// attempts per row, from the manifest fold (null when show fails: rows then fall back to status's own field)
let atts = new Map();
try {
  const sh = JSON.parse(execFileSync(process.execPath, [MANIFEST, 'show', '--run', run, '--json'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
  for (const a of sh.attempts || []) {
    if (a.row == null) continue;
    const k = String(a.row).toLowerCase(); if (!atts.has(k)) atts.set(k, []); atts.get(k).push(a);
  }
  for (const l of atts.values()) l.sort((x, y) => (x.n || 0) - (y.n || 0));
} catch { atts = new Map(); }

const dir = path.join(paths.runsDir(), run);
const readTxt = f => { try { return fs.readFileSync(path.join(dir, f), 'utf8'); } catch { return null; } };
const ROW_ID = /^\d{1,4}[a-z]?$/;
const clean = s => String(s == null ? '' : s).replace(/[`*]/g, '').replace(/\s+/g, ' ').trim();
const clip = (s, n) => s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s;
const keyOf = id => { const m = /^(\d+)([a-z]?)$/.exec(id); return m ? [parseInt(m[1], 10), m[2]] : [1e9, id]; };
const cmpKey = (a, b) => (a[0] - b[0]) || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0);

// ---- PLAN.md plan table(s): first table whose header has "#" and "Subtask"; first occurrence of a row id wins ----
const splitRow = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(c => c.replace(/\\\|/g, '|').trim());
function planTable(txt) {
  const out = new Map(); // id -> {subtask, agent, model, effort, wave}
  if (!txt) return out;
  const lines = txt.split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].trim().startsWith('|')) continue;
    const head = splitRow(lines[i]).map(c => clean(c).toLowerCase());
    if (!head.includes('#') || !head.includes('subtask')) continue;
    const col = n => head.indexOf(n), cAgent = head.findIndex(h => h.startsWith('agent'));
    for (i += 2; i < lines.length && lines[i].trim().startsWith('|'); i++) {
      const c = splitRow(lines[i]), id = clean(c[0]).toLowerCase();
      if (!ROW_ID.test(id) || out.has(id)) continue;
      const w = col('wave') >= 0 ? /\d+/.exec(c[col('wave')] || '') : null;
      const word = x => { const m = /^[a-z]+/i.exec(clean(x)); return m ? m[0].toLowerCase() : null; };
      out.set(id, { subtask: clean(c[col('subtask')]), agent: cAgent >= 0 ? clean(c[cAgent]) : '', model: col('model') >= 0 ? word(c[col('model')]) : null,
        effort: col('effort') >= 0 ? word(c[col('effort')]) : null, wave: w ? parseInt(w[0], 10) : null, why: col('why') >= 0 ? clean(c[col('why')]) : '' });
    }
    i--;
  }
  return out;
}
const plan = planTable(readTxt('PLAN.md'));

// ---- manifest.jsonl: subtask from launch/start events, last-activity time per row ----
const ev = new Map(); // id -> {subtask, role, ts}
for (const l of (readTxt('manifest.jsonl') || '').split('\n')) {
  if (!l.trim()) continue;
  let e; try { e = JSON.parse(l); } catch { continue; }
  if (e.row == null) continue;
  const id = String(e.row).toLowerCase(), o = ev.get(id) || { subtask: null, role: null, ts: 0 };
  if (e.type === 'launch') { if (e.subtask) o.subtask = e.subtask; if (e.role) o.role = e.role; }
  if (e.type === 'start' && e.description && !o.subtask) {
    const m = /^\[[^\]]*\]\s*\S+#\S+\s+(.+)$/.exec(e.description); if (m) o.subtask = m[1];
  }
  const t = Date.parse(e.ts); if (t > o.ts) o.ts = t;
  ev.set(id, o);
}

// ---- rows ----
const byId = new Map(st.rows.map(r => [String(r.row).toLowerCase(), r]));
const order = [...plan.keys()];
for (const id of [...byId.keys()].filter(id => !plan.has(id)).sort((a, b) => cmpKey(keyOf(a), keyOf(b)))) { // rows with attempts but no plan line
  const k = keyOf(id), at = order.findIndex(o => cmpKey(keyOf(o), k) > 0);
  if (plan.size && at >= 0) order.splice(at, 0, id); else order.push(id);
}
if (!plan.size) { order.length = 0; order.push(...[...byId.keys()].sort((a, b) => cmpKey(keyOf(a), keyOf(b)))); }

const GATE = /\b(review|gate|integrat\w*|pre-ship)\b/i;
const rows = order.map(id => {
  const m = byId.get(id) || {}, p = plan.get(id) || {}, e = ev.get(id) || {};
  const s = String(m.status || '').toLowerCase();
  let state, openNow = !!m.open || /\brunning\b/.test(s);
  const al = atts.get(id) || [], lat = al[al.length - 1], as = lat ? String(lat.state || '').toLowerCase() : '';
  const planCell = m.status_from === 'plan' ? s : '';
  if (lat && ['allocated', 'started', 'stopped-unreported', 'waiting'].includes(as)) { state = al.length > 1 ? 'retry' : 'running'; openNow = true; }
  else if (lat && (as === 'finished' || as === 'closed')) {
    // a finished attempt counts as done unless the plan cell says blocked (a review's gaps_found is closed by its fix row)
    state = /\b(blocked|orphaned)\b/.test(planCell) ? 'blocked' : 'done';
    openNow = false;
  }
  else if (lat && /^(killed|failed|stopped|closed-stale)/.test(as)) { state = 'blocked'; openNow = false; }
  else if (/\b(blocked|orphaned)\b|^(killed|failed|stopped)/.test(s)) state = 'blocked';
  else if (m.done) state = 'done';
  else if (openNow) state = (m.attempts || 0) > 1 ? 'retry' : 'running';
  else state = (m.attempts || 0) > 1 ? 'retry' : 'pending';
  const subtask = clean(p.subtask || m.subtask || e.subtask || '');
  const gate = /reviewer/i.test(p.agent || '') || e.role === 'review' || GATE.test(subtask) || /pre-ship/i.test(p.why || '');
  return { id, state, gate, running: state === 'running' || (state === 'retry' && openNow), wave: p.wave == null ? null : p.wave,
    subtask: subtask || '(no subtask recorded)', model: p.model || m.model || '?', effort: p.effort || m.effort || '?', sha: m.sha && m.sha !== 'none' ? m.sha : null,
    result: m.result || null, status: clean(m.status || ''), ts: e.ts || 0 };
});

// effective wave: a row without one takes the previous row's (the first takes the lowest known); stable sort by it
const known = rows.map(r => r.wave).filter(w => w != null);
const haveWaves = known.length > 0;
rows.forEach((r, i) => { r.i = i; });
if (haveWaves) {
  let prev = null; for (const r of rows) { r.ew = r.wave != null ? r.wave : prev; if (r.ew != null) prev = r.ew; }
  for (const r of rows) if (r.ew == null) r.ew = Math.min(...known);
  rows.sort((a, b) => (a.ew - b.ew) || (a.i - b.i));
}

// ---- view ----
const total = rows.length, done = rows.filter(r => r.state === 'done').length;
const running = rows.filter(r => r.running).length, blocked = rows.filter(r => r.state === 'blocked').length;
const pendingRows = rows.filter(r => r.state === 'pending' || (r.state === 'retry' && !r.running));
const filled = total ? (done === total ? 8 : Math.min(7, Math.round(done / total * 8))) : 0;
const bar = '▰'.repeat(filled) + '▱'.repeat(8 - filled);
const open = rows.filter(r => r.state !== 'done');
const maxWave = haveWaves ? Math.max(...rows.map(r => r.ew)) : 0;
const curWave = haveWaves ? (open.length ? Math.min(...open.map(r => r.ew)) : maxWave) : 0;
const head = [`${bar} ${done}/${total}`];
if (haveWaves) head.push(`wave ${curWave} of ${maxWave}`);
head.push(`${running} running`, `${pendingRows.length} pending`);
if (blocked) head.push(`${blocked} blocked`);

const glyph = r => r.state === 'done' ? '●' : r.state === 'blocked' ? '✕' : r.state === 'retry' ? '↻' : r.running ? '◐' : r.gate ? '◆' : '○';
const compact = total > 24; // keep the strip short on big plans: finished rows lose their number and pack together
const parts = []; let prevW = null, packed = false;
for (const r of rows) {
  if (haveWaves && prevW != null && r.ew !== prevW) { parts.push('┃'); packed = false; }
  prevW = r.ew;
  if (compact && r.state === 'done') { if (packed) parts[parts.length - 1] += '●'; else { parts.push('●'); packed = true; } }
  else { parts.push(glyph(r) + r.id); packed = false; }
}
const strip = parts.join(' ');

// table: running, blocked, retry, plus the last 2 done (most recent activity first picked, shown in plan order)
const lastDone = rows.filter(r => r.state === 'done').sort((a, b) => (b.ts - a.ts) || (b.i - a.i)).slice(0, 2);
const shown = new Set([...rows.filter(r => r.running || r.state === 'blocked' || r.state === 'retry'), ...lastDone]);
const home = process.env.HOME || process.env.USERPROFILE || '';
const where = r => r.sha ? r.sha.slice(0, 7) : r.result ? (home ? r.result.replace(home, '~') : r.result) : '-';
const word = r => r.state === 'retry' ? (r.running ? 'retry (running)' : 'retry') : r.state;
const tbl = rows.filter(r => shown.has(r));
const out = [head.join(' · '), strip, ''];
if (tbl.length) {
  out.push('| # | subtask | status | SHA / result |', '|---|---|---|---|');
  for (const r of tbl) out.push(`| ${r.id} | ${clip(r.subtask, 60).replace(/\|/g, '/')} | ${word(r)} | ${where(r)} |`);
  out.push('');
}
if (pendingRows.length) {
  out.push('Next up');
  for (const r of pendingRows.slice(0, 3)) out.push(`└ ${r.id} [${r.model}/${r.effort}] ${clip(r.subtask, 70)}${r.wave != null ? ` (wave ${r.wave})` : ''}`);
  if (pendingRows.length > 3) out.push(`+ ${pendingRows.length - 3} more · /boss:status ${run} --full`);
} else out.push(open.length ? 'Next up: nothing pending (open rows are running or blocked)' : 'Next up: nothing, every row is done');

const tok = st.tokens && st.tokens.n && st.tokens.unknown < st.tokens.n ? (n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n))(st.tokens.sum) : '-';
const L = st.lease || {};
out.push('', `run ${run} · preset ${st.preset || '?'} · plan ${plan.size ? 'yes' : st.plan ? 'yes (no plan table)' : 'no (rows from attempts)'} · tokens ${tok} · lease ${L.state || '?'}${L.state && L.state !== 'none' && L.state !== 'mine' && L.holder ? ' ' + String(L.holder).slice(0, 8) : ''}`);
console.log(out.join('\n'));
