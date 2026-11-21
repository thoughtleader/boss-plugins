'use strict';
// progress-mirror: builds the boss.progress/1 snapshot (<progress root>/PROGRESS.md) for one orchestrator run from its
// manifest events. Plain node, no dependencies, no I/O of its own: manifest.js passes in everything it reads (PLAN.md text,
// result-file lookup, owner lookup) so buildSnapshot is a pure function of its inputs and is tested without a filesystem.
//
// The snapshot is a CACHE of the manifest: buildSnapshot(events, ...) replays every event, so the incremental write that
// appendLocked does on each append and `manifest.js progress --rebuild` produce the same document by construction.
// Fold rules (row 0 adoption notes):
//   lease     acquire|renew|steal|migrate -> holder; release -> no holder (a release on a finished plan ends the run)
//   launch    attempt item row:<r>/a<n>, queued, with the launch model/effort
//   start     running, started_at; start model/effort override launch
//   stop      only final:true closes an attempt (status completed -> done, else failed; a turn-limit stop -> waiting_input, not failed); stop.model_id / effort_observed
//             override both; non-final stops are ignored
//   outcome   meta.first_pass / retries / escalated
//   a row's state comes from its newest attempt (PLAN.md Status cell only as fallback); attempt n replaces attempt n-1
// Attention hook point: <run>/attention.json (an array of boss.progress attention items) is folded in when present,
// and a plan row whose Status cell is a gate status (cfg.gateStatuses) becomes an approval item.

const SCHEMA = 'boss.progress/1';
const TERMINAL = ['done', 'failed', 'cancelled'];
const ROW_ID = /^\d{1,4}[a-z]?$/;
const MAX_ITEMS = 1000, MAX_ATTENTION = 32;
const OPEN_STATES = ['allocated', 'started', 'stopped-unreported'];
const LEASE_HOLD = ['acquire', 'renew', 'steal', 'migrate'];
const DEFAULT_PASS = ['done', 'passed', 'pass'];
const DEFAULT_GATE = ['gaps_found', 'revise', 'human_needed'];

const bySeq = (a, b) => (a.seq || 0) - (b.seq || 0);
const last = a => (a.length ? a[a.length - 1] : null);
const cellsOf = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
const word = s => { const m = /^[a-z_-]+/.exec(String(s || '').trim().toLowerCase()); return m ? m[0] : ''; };
const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length <= n ? s : s.slice(0, n - 1) + '…'; };
const rowCmp = (a, b) => {
  const pa = /^(\d+)([a-z]?)$/.exec(String(a)) || [0, 1e9, String(a)], pb = /^(\d+)([a-z]?)$/.exec(String(b)) || [0, 1e9, String(b)];
  return (+pa[1] - +pb[1]) || String(pa[2]).localeCompare(String(pb[2]));
};
const waveCmp = (a, b) => rowCmp(a, b);

// parsePlan(PLAN.md text) -> {order: [row...], rows: Map(row -> {row, subtask, agent, model, effort, wave, status})}.
// Tolerant: every markdown table whose header has a '#' column contributes, and so does any `| <row> | ... |` line after a note line (it reads against the latest header); later tables fill gaps and their non-empty
// Status wins (a `## Status` table is the maintained one). Anything it cannot read is skipped, never an error.
function parsePlan(txt) {
  const out = { order: [], rows: new Map() };
  if (typeof txt !== 'string') return out;
  const lines = txt.split(/\r?\n/);
  const isSep = l => /^\|[\s:|-]+$/.test(String(l || '').trim());
  let head = null;   // the latest table header; a `| <row> | ... |` line outside a table (after a note line) still reads against it
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line.startsWith('|')) continue;
    if (isSep(line)) continue;
    const prevIsTable = i > 0 && lines[i - 1].trim().startsWith('|');
    if (isSep(lines[i + 1]) || (!prevIsTable && !head)) {   // a header: the line above a separator, or the first table line before any header was seen
      const h = cellsOf(line).map(c => c.toLowerCase()), idc = h.indexOf('#');
      head = idc < 0 ? null : { h, idc };
      continue;
    }
    if (!head) continue;
    const { h: hd, idc } = head;
    const col = (...names) => { for (const n of names) { const k = hd.indexOf(n); if (k >= 0) return k; } return -1; };
    const sc = col('subtask', 'task'), ac = col('agent', 'agent type'), mec = col('model/effort'), mc = col('model'), ec = col('effort'), wc = col('wave'), stc = col('status');
    {
      const c = cellsOf(line), id = String(c[idc] || '').replace(/[`*]/g, '').toLowerCase();
      if (!ROW_ID.test(id)) continue;
      let r = out.rows.get(id);
      if (!r) { r = { row: id, subtask: null, agent: null, model: null, effort: null, wave: null, status: null }; out.rows.set(id, r); out.order.push(id); }
      const put = (k, v) => { if (v != null && v !== '') r[k] = v; };
      if (sc >= 0) put('subtask', c[sc]);
      if (ac >= 0) put('agent', c[ac]);
      if (mec >= 0 && c[mec]) { const [m, e] = c[mec].split('/'); put('model', m && m.trim()); put('effort', e && e.trim()); }
      if (mc >= 0) put('model', c[mc]);
      if (ec >= 0) put('effort', c[ec]);
      if (wc >= 0) { const w = String(c[wc] || '').toLowerCase().replace(/^wave\s*/, '').trim(); if (/^[a-z0-9]{1,4}$/.test(w)) put('wave', w); }
      if (stc >= 0) put('status', c[stc]);
      for (const [key, names] of Object.entries({ short_label: ['short name', 'short label'], activity: ['activity'], depends_on: ['depends on', 'dependencies'], estimate: ['estimate', 'estimate (min)'], reviewable: ['reviewable'], instructions: ['instructions'] })) {
        const k = col(...names); if (k >= 0) put(key, c[k]);
      }
    }
  }
  return out;
}

// groupAttempts(events) -> Map(attemptId -> {id, row, n, launches, starts, stops, outcomes})
function groupAttempts(events, parseAttemptId) {
  const m = new Map(), voided = new Set();
  for (const e of events) {
    if (e.attempt && e.type === 'void') { voided.add(e.attempt); continue; } // `manifest.js void`: allocated, never launched
    if (!e.attempt || !['launch', 'start', 'stop', 'outcome'].includes(e.type)) continue;
    if (!m.has(e.attempt)) {
      const a = parseAttemptId(e.attempt);
      m.set(e.attempt, { id: e.attempt, row: a ? a.row : e.row != null ? String(e.row).toLowerCase() : null, n: a ? a.n : e.n || 1, launches: [], starts: [], stops: [], outcomes: [] });
    }
    m.get(e.attempt)[{ launch: 'launches', start: 'starts', stop: 'stops', outcome: 'outcomes' }[e.type]].push(e);
  }
  for (const id of voided) { const g = m.get(id); if (g && !g.starts.length && !g.stops.length) m.delete(id); } // a voided attempt that never ran leaves no item
  for (const g of m.values()) for (const k of ['launches', 'starts', 'stops', 'outcomes']) g[k].sort(bySeq);
  return m;
}

// attemptState(g) -> queued | running | done | failed  (+ the raw manifest state for meta)
// A turn-limit stop (phase stopped-turn-limit) is a partial result, not a failure: the attempt reads `waiting_input` (the schema's not-done, not-failed, not-terminal state: the meter shows ◆ and a needs-you item until the row is continued or accepted; detail and meta.stop_status say turn-limit), and
// it speaks for the attempt only when strictly LATER than every real final stop, by its own ts (an equal ts is not later; seq only when a ts is missing). Same rule as
// manifest.js pickStop (copied: this module is pure); ingest order must not let a late-swept turn-limit notice override a real finish.
const TURN_LIMIT = 'stopped-turn-limit';
const TL_DETAIL = 'stopped at its turn limit (partial result): continue it with launch --continue, or accept the partial';
const laterStop = (a, b) => { const ta = Date.parse(a.ts), tb = Date.parse(b.ts); return Number.isFinite(ta) && Number.isFinite(tb) ? ta > tb : (a.seq || 0) > (b.seq || 0); };
function attemptState(g) {
  const fin = g.stops.filter(s => s.final === true), real = fin.filter(s => s.phase !== TURN_LIMIT);
  const pick = last(fin.filter(s => s.phase === TURN_LIMIT && real.every(r => laterStop(s, r)))) || last(real);
  if (pick) return { state: pick.phase === TURN_LIMIT ? 'waiting_input' : pick.status === 'completed' ? 'done' : 'failed', open: false, fin: pick };
  // The agent finished its turn and went idle ("is waiting"): a non-final `waiting` stop newer than the last start. Nobody continued it, so it is done, not running.
  const idle = last(g.stops.filter(s => s.phase === 'waiting' && s.status === 'completed'));
  if (idle && (!g.starts.length || (last(g.starts).seq || 0) < (idle.seq || 0))) return { state: 'done', open: false, fin: idle, idle: true };
  if (g.starts.length || g.stops.length) return { state: 'running', open: true, fin: null };
  return { state: 'queued', open: true, fin: null };
}

// leaseView(events) -> {seen, holder, expires, released, releasedAt, lastHolder}
function leaseView(events) {
  const v = { seen: false, holder: null, expires: null, released: false, releasedAt: null, lastHolder: null };
  for (const e of events.filter(x => x.type === 'lease').sort(bySeq)) {
    if (LEASE_HOLD.includes(e.action)) { v.seen = true; v.holder = e.holder || e.session || null; v.lastHolder = v.holder; v.expires = e.expires || null; v.released = false; v.releasedAt = null; }
    else if (e.action === 'release') { v.seen = true; v.holder = null; v.expires = null; v.released = true; v.releasedAt = e.ts || null; }
  }
  return v;
}

const cellState = (cell, pass, gate) => {
  const w = word(cell);
  if (!w) return null;
  if (w === 'skipped' || w === 'dropped') return 'skipped';
  if (pass.includes(w) || ['finished', 'closed', 'done'].includes(w)) return 'done';
  if (gate.includes(w) || w === 'blocked' || w === 'orphaned') return 'blocked';
  if (w === 'failed') return 'failed';
  if (['running', 'started'].includes(w)) return 'running';
  return 'queued';
};
const isGate = (p, launch) => /review|plan-check/i.test((p && p.agent) || (launch && launch.agent) || '') || /\b(review|gate|integration)\b/i.test((p && p.subtask) || '');
// Human name for an attempt/row from a start event's description: `[model/effort] <attempt-id> <subtask>` -> `<subtask>`.
// A bare description (no label) is kept whole; `[auto] <id> text` and `[m/e] text` both work. Model/effort never become the name.
const nameFromDesc = d => {
  const t = String(d == null ? '' : d).replace(/^\s*\[[^\]]*\]\s*/, '').replace(/^\S+#\d+[a-z]?\.\d+\s*/, '').trim();
  return t ? clip(t, 200) : null;
};
const hex = s => (typeof s === 'string' && /^[0-9a-f]{7,40}$/i.test(s.trim()) ? s.trim() : null);

// buildSnapshot(run, events, ctx) -> boss.progress/1 document (not yet cleaned or written).
// ctx: {runDir, plan: {txt, fm, mtimeMs} | null, cfg, parseAttemptId, resultFor(row, launch, attemptId, n) -> {path, fm, match}|null,
//       ownerFor(sessionId) -> owner|null, writer, attentionFile: array|null, now (ms)}
function buildSnapshot(run, events, ctx) {
  const cfg = ctx.cfg || {}, pass = (cfg.passStatuses || DEFAULT_PASS).map(String), gateWords = (cfg.gateStatuses || DEFAULT_GATE).map(String);
  const evs = events.filter(e => e && typeof e.type === 'string').slice().sort(bySeq);
  const plan = ctx.plan ? parsePlan(ctx.plan.txt) : { order: [], rows: new Map() }, fm = (ctx.plan && ctx.plan.fm) || {};
  const groups = groupAttempts(evs, ctx.parseAttemptId);
  const lease = leaseView(evs), lastEv = last(evs);
  const iso = ms => new Date(ms).toISOString();
  const lastSession = [...evs].reverse().map(e => e.session).find(Boolean) || null;

  // ---- attempts and rows
  const byRow = new Map();
  for (const g of groups.values()) { if (!g.row) continue; if (!byRow.has(g.row)) byRow.set(g.row, []); byRow.get(g.row).push(g); }
  for (const list of byRow.values()) list.sort((a, b) => a.n - b.n);
  const rowIds = [...plan.order];
  for (const r of [...byRow.keys()].sort(rowCmp)) if (!plan.rows.has(r)) rowIds.push(r);

  const attemptItems = new Map(); // row -> [item]
  const rowInfo = new Map();      // row -> {state, item}
  for (const row of rowIds) {
    const p = plan.rows.get(row) || null, atts = byRow.get(row) || [];
    const items = [];
    const descName = [...atts].reverse().map(g => nameFromDesc(last(g.starts) && last(g.starts).description)).find(Boolean) || null;
    for (const g of atts) {
      const L = last(g.launches), S = last(g.starts), st = attemptState(g), O = last(g.outcomes);
      const lm = L && L.model ? `${L.model}${L.effort ? '/' + L.effort : ''}` : null;
      const model = (st.fin && st.fin.model_id) || (S && S.model) || (L && L.model) || null;
      const effort = (st.fin && st.fin.effort_observed) || (S && S.effort) || (L && L.effort) || null;
      // label = the human subtask (launch.subtask, else the start description tail, else the plan row's subtask, else the id); the id stays in meta.attempt_id
      const name = (L && L.subtask && clip(L.subtask, 200)) || (S && nameFromDesc(S.description)) || (p && p.subtask && clip(p.subtask, 200)) || descName;
      const it = { id: `row:${row}/a${g.n}`, parent: `row:${row}`, kind: 'attempt', label: name || g.id, state: st.state, attempt: g.n };
      const prev = atts.filter(x => x.n < g.n).pop();
      if (prev) it.replaces = `row:${row}/a${prev.n}`;
      if (model && model !== 'unknown') it.model = model;
      if (effort && effort !== 'unknown') it.effort = effort;
      if (S && S.ts) it.started_at = S.ts;
      if (st.fin && st.fin.ts) it.ended_at = st.fin.ts;
      it.updated_at = (last([...g.launches, ...g.starts, ...g.stops, ...g.outcomes].sort(bySeq)) || {}).ts;
      const art = [];
      let res = null; try { res = ctx.resultFor ? ctx.resultFor(row, L, g.id, atts.length) : null; } catch { res = null; }
      const meta = { attempt_id: g.id };
      if (L && L.ts) meta.launched_at = L.ts;
      if (res && res.match) {
        const sha = hex(res.fm && res.fm.sha);
        if (sha) art.push({ label: 'commit', path: null, sha });
        art.push({ label: 'result', path: res.path, sha: null });
        if (res.fm && res.fm.sha_reason) meta.sha_reason = clip(res.fm.sha_reason, 200);
      }
      if (art.length) it.artifacts = art;
      const agent = (L && L.agent) || (S && S.agent) || (p && p.agent);
      if (agent) meta.agent = agent;
      if (L && L.why) meta.why = clip(L.why, 100);
      if (lm && model && lm !== `${model}${effort ? '/' + effort : ''}`) meta.launch_model = lm;
      if (O) for (const k of ['first_pass', 'retries', 'escalated']) if (O[k] !== undefined && O[k] !== null) meta[k] = O[k];
      if (st.state === 'waiting_input' && st.fin && st.fin.phase === TURN_LIMIT) it.detail = TL_DETAIL;
      if (st.fin) {
        if (st.fin.status && st.fin.status !== 'completed') meta.stop_status = clip(st.fin.status, 40);
        for (const k of ['tokens', 'tools', 'ms']) if (Number.isFinite(st.fin[k])) meta[k] = st.fin[k];
      }
      if (Object.keys(meta).length) it.meta = meta;
      it._open = st.open;
      items.push(it);
    }
    attemptItems.set(row, items);

    // row state: open attempt > blocked/gate cell > done cell > newest attempt > failed cell > running cell > queued
    const newest = last(items), cs = p ? cellState(p.status, pass, gateWords) : null;
    let state;
    if (items.some(i => i._open && i.state === 'running')) state = 'running';
    else if (newest && newest._open) state = 'queued';
    else if (cs === 'blocked') state = 'blocked';
    else if (cs === 'done' || cs === 'skipped') state = cs;
    else if (newest) state = newest.state;
    else if (cs === 'failed' || cs === 'running') state = cs;
    else state = 'queued';
    const L1 = last((byRow.get(row) || []).flatMap(g => g.launches).sort(bySeq));
    const it = { id: `row:${row}`, parent: null, kind: isGate(p, L1) ? 'gate' : 'row', label: clip((p && p.subtask) || (L1 && L1.subtask) || descName || `row ${row}`, 200), state };
    if (state === 'waiting_input' && newest && newest.state === 'waiting_input' && newest.detail) it.detail = newest.detail;
    // Optional reporting fields enrich presentation without affecting dispatch.
    if (p) {
      it.instructions = clip(p.instructions || p.subtask || '', 4000);
      if (p.short_label) it.short_label = clip(p.short_label, 100);
      if (p.activity) it.activity = clip(p.activity, 200);
      if (p.depends_on) it.depends_on = p.depends_on.split(/[,;\s]+/).filter(id => ROW_ID.test(id)).map(id => `row:${id}`);
      if (p.reviewable) it.reviewable = /^(yes|true|y)$/i.test(p.reviewable);
      const estimate = /^\s*(\d+(?:\.\d+)?)\s*(m|min|minutes?|h|hours?|s|seconds?)?\s*$/i.exec(p.estimate || '');
      if (estimate) it.estimate_ms = Number(estimate[1]) * (/^h/i.test(estimate[2] || '') ? 3600000 : /^s/i.test(estimate[2] || '') ? 1000 : 60000);
    }
    if (p && p.model) it.model = p.model;
    if (p && p.effort) it.effort = p.effort;
    const meta = {};
    if (it.kind === 'gate') meta.gate = true;
    if (p && p.status) meta.plan_status = clip(p.status, 200);
    if (p && p.agent) meta.agent = p.agent;
    if (Object.keys(meta).length) it.meta = meta;
    rowInfo.set(row, { state, item: it, wave: p ? p.wave : null, cs });
  }

  // ---- waves
  const waveIds = [...new Set([...rowInfo.values()].map(r => r.wave).filter(Boolean))].sort(waveCmp);
  const waveRows = w => rowIds.filter(r => rowInfo.get(r).wave === w);
  const tally = rows => {
    const t = { done: 0, total: rows.length, running: 0, failed: 0, blocked: 0 };
    for (const r of rows) { const s = rowInfo.get(r).state; if (s === 'done' || s === 'skipped') t.done++; else if (s === 'running') t.running++; else if (s === 'failed') t.failed++; else if (s === 'blocked') t.blocked++; }
    return t;
  };
  const waveState = rows => {
    const t = tally(rows);
    if (t.total && t.done === t.total) return 'done';
    if (t.running) return 'running';
    if (t.failed) return 'failed';
    if (t.blocked) return 'blocked';
    return t.done ? 'running' : 'queued';
  };
  const items = [];
  const pushRow = (row, parent) => {
    const info = rowInfo.get(row);
    info.item.parent = parent;
    items.push(info.item);
    for (const a of attemptItems.get(row)) { delete a._open; items.push(a); }
  };
  for (const w of waveIds) {
    items.push({ id: `wave:${w}`, parent: null, kind: 'wave', label: `wave ${w}`, state: waveState(waveRows(w)) });
    for (const r of waveRows(w)) pushRow(r, `wave:${w}`);
  }
  for (const r of rowIds) if (!rowInfo.get(r).wave || !waveIds.includes(rowInfo.get(r).wave)) pushRow(r, null);
  // size cap: drop attempt items of finished rows first (oldest rows first), then give up quietly at the cap
  if (items.length > MAX_ITEMS) {
    for (const r of rowIds) {
      if (items.length <= MAX_ITEMS) break;
      if (rowInfo.get(r).state !== 'done') continue;
      for (let k = items.length - 1; k >= 0; k--) if (items[k].kind === 'attempt' && items[k].parent === `row:${r}`) items.splice(k, 1);
    }
    items.length = Math.min(items.length, MAX_ITEMS);
  }

  // ---- run state
  const total = tally(rowIds);
  const anyOpen = [...attemptItems.values()].some(l => l.some(i => i.state === 'queued' || i.state === 'running'));
  const finishedPlan = rowIds.length > 0 && total.done === total.total && !anyOpen;
  const noPlanFinish = !ctx.plan && !anyOpen && total.failed === 0;
  let state = 'running', phase = null, endedAt = null, paused = false;
  const curWave = waveIds.find(w => waveState(waveRows(w)) !== 'done') || null;
  if (curWave) phase = `wave ${curWave}`; else if (waveIds.length) phase = `wave ${waveIds[waveIds.length - 1]}`; else phase = 'rows';
  if (lease.released || (!lease.seen && finishedPlan)) {
    if (finishedPlan || (lease.released && noPlanFinish)) { state = 'done'; phase = 'finished'; endedAt = lease.releasedAt || (lastEv && lastEv.ts) || null; }
    else if (lease.released && !anyOpen && !ctx.plan && total.failed > 0) { state = 'failed'; phase = 'finished'; endedAt = lease.releasedAt; }
    else if (lease.released) { state = 'waiting_input'; phase = 'paused (lease released)'; paused = true; }
  }

  // ---- counts, summary
  const counts = [{ unit: 'rows', label: 'rows', done: total.done, total: total.total, running: total.running, failed: total.failed }];
  if (curWave) { const t = tally(waveRows(curWave)); counts.push({ unit: 'wave', label: `wave ${curWave}`, done: t.done, total: t.total, running: t.running, failed: t.failed }); }
  const sum = [`${total.done}/${total.total} rows done`];
  if (total.running) sum.push(`${total.running} running`);
  if (total.failed) sum.push(`${total.failed} failed`);
  if (total.blocked) sum.push(`${total.blocked} blocked`);
  if (state === 'waiting_input') sum.push('paused');

  // ---- attention
  const attention = [];
  const planMs = ctx.plan && ctx.plan.mtimeMs;
  const sinceFor = row => { const l = last((byRow.get(row) || []).flatMap(g => [...g.stops, ...g.outcomes]).sort(bySeq)); return (l && l.ts) || (planMs ? iso(planMs) : (lastEv && lastEv.ts) || iso(ctx.now || Date.now())); };
  if (!TERMINAL.includes(state)) {
    for (const r of rowIds) {
      const info = rowInfo.get(r);
      if (info.cs === 'blocked' && gateWords.includes(word(plan.rows.get(r).status)) && info.state === 'blocked')
        attention.push({ id: `gate-row-${r}`, kind: 'approval', item: `row:${r}`, text: `Row ${r} is ${word(plan.rows.get(r).status)}: needs your decision`, since: sinceFor(r),
          answered_by: { via: 'session', message_kind: 'answer', gate_hash: null, command: null, path: null } });
    }
    for (const a of Array.isArray(ctx.attentionFile) ? ctx.attentionFile : []) {
      if (!a || typeof a !== 'object' || !/^[A-Za-z0-9._-]{1,128}$/.test(String(a.id || '')) || typeof a.text !== 'string') continue;
      attention.push({ id: String(a.id), kind: ['question', 'approval', 'error'].includes(a.kind) ? a.kind : 'question', item: typeof a.item === 'string' ? a.item : null, text: a.text,
        since: typeof a.since === 'string' && !Number.isNaN(Date.parse(a.since)) ? a.since : (lastEv && lastEv.ts) || iso(ctx.now || Date.now()),
        options: Array.isArray(a.options) ? a.options.filter(x => typeof x === 'string').slice(0, 8) : undefined,
        answered_by: a.answered_by && typeof a.answered_by === 'object' ? a.answered_by : { via: 'session', message_kind: 'answer', gate_hash: null, command: null, path: null } });
    }
    attention.length = Math.min(attention.length, MAX_ATTENTION);
  }

  // ---- envelope
  const firstEv = evs[0];
  const progressEvs = evs.filter(e => ['launch', 'start', 'stop', 'outcome'].includes(e.type) && (e.type !== 'stop' || e.final === true || e.phase !== 'agent-stopped'));
  const lp = last(progressEvs) || lastEv;
  const ownerSid = lease.holder || lease.lastHolder || lastSession;
  const launchIds = new Set(evs.filter(e => e.type === 'launch').map(e => e.attempt));
  const snap = {
    schema: SCHEMA, tool: 'orchestrator', run_id: run,
    title: clip(fm.title || fm.goal || run, 200), cwd: fm.cwd || null,
    source_dir: ctx.runDir || null, events_path: ctx.runDir ? `${ctx.runDir}/manifest.jsonl` : null,
    state, phase, summary: clip(sum.join(' · '), 500),
    created_at: firstEv ? firstEv.ts : null, started_at: firstEv ? firstEv.ts : null,
    updated_at: (lastEv && lastEv.ts) || iso(ctx.now || Date.now()), ended_at: endedAt,
    heartbeat_at: null, heartbeat_interval_s: null, last_progress_at: lp ? lp.ts : null,
    seq: lastEv && Number.isSafeInteger(lastEv.seq) ? lastEv.seq : 0,
    writer: ctx.writer || { pid: process.pid, start_ticks: null, boot_id: null, kind: 'oneshot' },
    owner: ctx.ownerFor ? ctx.ownerFor(ownerSid) : ownerSid ? { session_id: ownerSid, pid: null, start_ticks: null, inbox: null } : null,
    counts, items, attention,
    ext: { orchestrator: { tier: fm.tier || null, plan_mode: fm.mode || null, launches: launchIds.size, events: evs.length,
      unplanned_starts: evs.filter(e => e.type === 'start' && !e.attempt).length,
      lease: { holder: lease.holder, expires: lease.expires, released_at: lease.releasedAt } } },
  };
  if (paused) snap.paused = true;   // additive within boss.progress/1: lease released before the plan finished (PROGRESS.md section 3)
  return snap;
}

module.exports = { SCHEMA, buildSnapshot, parsePlan, leaseView, groupAttempts, attemptState };
