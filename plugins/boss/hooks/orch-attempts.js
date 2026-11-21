#!/usr/bin/env node
// orch-attempts: orchestrator attempt hooks (D4 design section 5, row 14). Plain node, no dependencies.
// Modes (argv[2]): pretool (PreToolUse Agent|Task), prompt (UserPromptSubmit), stop (Stop), substop (SubagentStop),
// session (SessionStart compact|resume). Registered by hooks/hooks.json, which calls it through bin/boss-node.
// Rules:
//  - Fail open: always exit 0; never a permissionDecision, never a Stop decision; errors go to one line in
//    <data dir>/errors.log (capped at 256 KB by dropping the older half).
//  - One lease per run (per-run lease design D-1, D-2): $ORCH_DIR/leases/<run>.json, or the legacy ACTIVE.lease
//    for its own run only. Every lease read goes through lib/lease-store.js (loaded in try/catch; missing = holds
//    nothing). A session's held runs are lease-store heldBy(sid): live, or expired and still its own.
//  - A hook writes into run R's manifest only when this session holds R's lease (D1). The run: (1) the attempt ID ends the
//    chain: its run if held, else nothing (pretool may still nudge); (2) no ID: the runs the brief names (Orch-Attempt line,
//    runs/<run> paths), only when exactly one of them is held; substop: the held run whose manifest has this agent's start;
//    (3) else the session's only lease; (4) else nothing, and one errors.log line. ACTIVE is never read.
//  - A session that holds no lease passes straight through: nothing written; nothing printed, except pretool's nudge (an
//    orch-* launch whose brief names a run nobody holds live). Session mode is silent for it. It loads lease-store.js only.
//  - Notice ingest (prompt, sweep) is gated (OD-3): ledger.js gets held = this session's held runs, so a notice whose ID
//    names another run writes nothing (none:not-held), an ID-less one is matched only against held runs, and a start found
//    in 2+ of them writes nothing (none:ambiguous, one errors.log line). ACTIVE is never read by any mode.
//  - Every mode except substop returns at once when the input carries agent_id (inside a subagent).
//  - manifest.js / ledger.js are loaded lazily and only for a lease holder; the sqlite store is never loaded here.
// Paths come from lib/paths.js (BOSS_DATA; ORCH_DIR, ORCH_RUNS_DIR, ORCH_PROJECTS_DIR in tests, read by manifest.js and ledger.js too).
// Agent types are matched on agentBase(type), so `boss:orch-scout` and `orch-scout` are the same agent.
'use strict';
const fs = require('fs'), path = require('path');
const paths = require(path.join(__dirname, '..', 'lib', 'paths.js'));
const ORCH_DIR = paths.dataDir();
const RUNS = paths.runsDir();
const LIB = path.join(__dirname, '..', 'lib');
const MANIFEST_CLI = `node "${path.join(LIB, 'manifest.js')}"`;
const LOG_CAP = 262144;
const MODE = process.argv[2] || '';
const SID_OK = s => typeof s === 'string' && /^[A-Za-z0-9._-]{1,128}$/.test(s) && !s.includes('..');
const RUN_OK = s => typeof s === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(s) && !s.includes('..');
const hhmm = iso => { const t = Date.parse(iso); return Number.isFinite(t) ? new Date(t).toISOString().slice(11, 16) + ' UTC' : '?'; };
const sid8 = s => String(s || '').slice(0, 8);
const reEsc = s => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const readJson = f => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return null; } };
let manifestMod = null, ledgerMod = null;
const MF = () => manifestMod || (manifestMod = require(path.join(LIB, 'manifest.js')));
const LG = () => ledgerMod || (ledgerMod = require(path.join(LIB, 'ledger.js')));

function log(msg) { // one line; the file is capped by dropping its older half. Never throws.
  try {
    const f = paths.errorsLog(); fs.mkdirSync(path.dirname(f), { recursive: true });
    try {
      if (fs.statSync(f).size > LOG_CAP) {
        const t = fs.readFileSync(f, 'utf8'), k = t.indexOf('\n', Math.floor(t.length / 2));
        const tmp = `${f}.${process.pid}.tmp`; fs.writeFileSync(tmp, k >= 0 ? t.slice(k + 1) : ''); fs.renameSync(tmp, f);
      }
    } catch {}
    fs.appendFileSync(f, `${new Date().toISOString()} orch-attempts ${MODE}: ${String(msg).replace(/\s+/g, ' ').slice(0, 400)}\n`);
  } catch {}
}
let leaseStore; // lease-store.js, loaded once; null when it cannot be loaded (then this session holds nothing)
function LS() {
  if (leaseStore !== undefined) return leaseStore;
  try { leaseStore = require(path.join(LIB, 'lease-store.js')); } catch (e) { leaseStore = null; log(`lease-store.js not loaded (${e && e.code || e && e.message}); treated as holding no lease`); }
  return leaseStore;
}
function heldRuns(sid) { // the runs whose lease this session holds (live, or expired and still its own), newest renewed first
  if (!SID_OK(sid)) return [];
  const S = LS(); if (!S) return [];
  try { const h = S.heldBy(ORCH_DIR, sid); return Array.isArray(h) ? h.filter(x => x && RUN_OK(x.run) && x.lease) : []; } catch (e) { log(`heldBy: ${e && e.message}`); return []; }
}
function runLease(run) { // {lease, legacy, file} for that run, or null (none, unreadable, or lease-store missing)
  const S = LS(); if (!S || !RUN_OK(run)) return null;
  try { return S.readRunLease(ORCH_DIR, run); } catch { return null; }
}
const isLive = (L, now = Date.now()) => { const e = L ? Date.parse(L.expires) : NaN; return Number.isFinite(e) && e > now; };
const ctx = (ev, lines) => lines.length ? { hookSpecificOutput: { hookEventName: ev, additionalContext: lines.join('\n') } } : null;
const runExists = r => { try { return fs.statSync(path.join(RUNS, r)).isDirectory(); } catch { return false; } };

// Nudge (Stage A, 1b; per-run): a main session launching an orch-* agent for a run that no session holds live (lease absent,
// or expired and not this session's own) gets one context line asking it to take that run's lease if it drives the run. The
// run comes from the brief first: the prompt's `Orch-Attempt: <run>#<n>` line, then an attempt ID in the description, then a
// `runs/<run>` path in the prompt (each only when that run folder exists). A brief that names no run gets no nudge (OD-1;
// ACTIVE is never read). Only that run's lease counts: another session's live lease on it silences the nudge, a lease on a
// different run does not. Never denies, never writes, never loads manifest.js.
const RUN_BODY = '([A-Za-z0-9][A-Za-z0-9._-]{0,99})';
const DESC_ID_RE = new RegExp('^\\s*(?:\\[[^\\]\\s]+\\]\\s*)?' + RUN_BODY + '#\\d{1,4}[A-Za-z]?\\.[1-9]\\d{0,2}(?=\\s|$)');
const BRIEF_ID_RE = new RegExp('^[ \\t]*Orch-Attempt:[ \\t]*' + RUN_BODY + '#\\d{1,4}[A-Za-z]?\\.[1-9]\\d{0,2}[ \\t]*$', 'gm');
const BRIEF_PATH_RE = new RegExp('runs/' + RUN_BODY + '(?![A-Za-z0-9._-])', 'g');
function briefRuns(ti) { // every existing run the brief names, deduped, in priority order: Orch-Attempt lines, description ID, runs/ paths
  const prompt = String(ti.prompt == null ? '' : ti.prompt), out = [];
  const add = r => { if (RUN_OK(r) && !out.includes(r) && runExists(r)) out.push(r); };
  for (const m of prompt.matchAll(BRIEF_ID_RE)) add(m[1]);
  const d = DESC_ID_RE.exec(String(ti.description == null ? '' : ti.description)); if (d) add(d[1]);
  for (const m of prompt.matchAll(BRIEF_PATH_RE)) add(m[1]);
  return out;
}
const briefRun = ti => briefRuns(ti)[0] || null; // the run the brief names first, or null
function nudge(i) {
  const ti = i.tool_input && typeof i.tool_input === 'object' ? i.tool_input : {};
  if (!/^orch-/.test(paths.agentBase(ti.subagent_type))) return null;
  if (!LS()) return null; // no lease state to read: no nudge
  const run = briefRun(ti);
  if (!run) return null; // the brief names no run: silent
  const r = runLease(run), L = r ? r.lease : null;
  if (L && (L.session === i.session_id || isLive(L))) return null; // this session's own, or live for another session
  const why = L ? `The run lease expired (last held by ${sid8(L.session)} for run ${L.run})` : 'No session holds the run lease';
  return ctx('PreToolUse', [`[orch] ${why}, and this session is launching ${ti.subagent_type} for run ${run}. If this session drives that run, take the lease now: ${MANIFEST_CLI} lease acquire --run ${run} --sid=$WFP_SID, and launch rows through manifest.js launch. If it doesn't drive the run, ignore this.`]);
}

// PreToolUse (Agent|Task), main session, lease holder only: append a `start` event to the run D1 picks. Never denies, never
// rewrites the call. A non-holder, or a holder launching an ID for a run it does not hold, gets at most the nudge above.
function pretool(i) {
  if (i.agent_id) return null;
  const H = heldRuns(i.session_id); if (!H.length) return nudge(i);
  const held = H.map(h => h.run);
  const M = MF(), ti = i.tool_input && typeof i.tool_input === 'object' ? i.tool_input : {};
  const d = M.parseDescription(ti.description), a = d.attempt ? M.parseAttemptId(d.attempt) : null;
  let run;
  if (a) { if (!held.includes(a.run)) return nudge(i); run = a.run; } // D1 (1): the ID ends the chain
  else {
    const named = briefRuns(ti).filter(r => held.includes(r));
    run = named.length === 1 ? named[0] : held.length === 1 && !named.length ? held[0] : null; // D1 (2), then (3)
    if (!run) { // D1 (4)
      log(`unplanned start ${typeof i.tool_use_id === 'string' ? i.tool_use_id : '?'} not written: this session holds ${held.length} leases (${held.slice().sort().join(', ')}) and the brief names ${named.length ? named.join(', ') : 'none of them'}`);
      return null;
    }
  }
  // The Boss Stop gate (orch-stop-gate.js, the sibling PreToolUse hook, runs in parallel) denies a call that names a stopped run. A call it
  // denies never starts, so it must not leave a `start` event (the board would show the run as still working, Stopping). Same decide() as the gate.
  try { if (require('./orch-stop-gate.js').decide(i)) { log(`start ${typeof i.tool_use_id === 'string' ? i.tool_use_id : '?'} not written: the stop gate denies this call (stopped run)`); return null; } } catch { /* gate missing or broken: write the start as before */ }
  const tuid = typeof i.tool_use_id === 'string' && i.tool_use_id ? i.tool_use_id : null;
  if (!tuid) { log(`no tool_use_id; start for ${a ? a.id : 'an unplanned launch'} not written`); return null; }
  const notes = [];
  const ev = { type: 'start', key: tuid, attempt: a ? a.id : null, row: a ? a.row : null, via: 'pretool', session: i.session_id,
    tool_use_id: tuid, agent: ti.subagent_type == null ? null : paths.agentBase(ti.subagent_type) || null, // subagent_type without the plugin prefix (row 17 compares it with the stop's agent_type)
    model: ti.model == null ? null : String(ti.model), effort: ti.effort == null ? null : String(ti.effort),
    description: String(ti.description == null ? '' : ti.description).slice(0, 1000), background: !!ti.run_in_background, named: !!ti.name,
    transcript_path: typeof i.transcript_path === 'string' ? i.transcript_path : null,
    unplanned: !a, unknown_attempt: false, id_in_brief: false, foreign_lease: false };
  if (a) {
    if (!runExists(a.run)) { log(`run folder missing: ${path.join(RUNS, a.run)} (start ${tuid} for ${a.id} not written)`); return ctx('PreToolUse', [`[orch] ${a.id}: run folder ${a.run} not found; start not recorded.`]); }
    ev.unknown_attempt = !M.hasLaunch(a.run, a.id); // === no launch event for this ID in readEvents(a.run), without parsing every line
    ev.id_in_brief = new RegExp(`^[ \\t]*Orch-Attempt:[ \\t]*${reEsc(a.id)}[ \\t]*$`, 'm').test(String(ti.prompt == null ? '' : ti.prompt));
    if (ev.unknown_attempt) notes.push(`[orch] ${a.id}: no launch event for this ID; run manifest.js launch first (recorded as unknown-attempt).`);
    if (!ev.id_in_brief) notes.push(`[orch] ${a.id}: the brief lacks the line Orch-Attempt: ${a.id}; the commit trailer depends on it.`);
  } else if (!runExists(run)) { log(`lease run folder missing: ${path.join(RUNS, run)} (unplanned start ${tuid} not written)`); return null; }
  const r = M.appendEvent(run, ev, { hook: true });
  if (r.skipped) return ctx('PreToolUse', a ? [`[orch] ${a.id}: start not recorded (${r.reason}); manifest.js reconcile rebuilds it.`] : []);
  return ctx('PreToolUse', notes);
}

function ledgerNumbers(id) { // tokens/tools/ms of the ledger line with this id, or null
  const a = MF().parseAttemptId(id); if (!a) return null;
  let txt = ''; try { txt = fs.readFileSync(path.join(RUNS, a.run, 'ledger.jsonl'), 'utf8'); } catch { return null; }
  for (const l of txt.split('\n')) { if (!l.includes(id)) continue; try { const x = JSON.parse(l); if (x && x.id === a.id) return x; } catch {} }
  return null;
}
function sweep(i, lines, held) { // the main transcript's enqueue / queued_command records (ledger.ingestTranscript: the default tail the first time, then from the saved offset), gated to held
  const tp = i.transcript_path;
  if (typeof tp !== 'string' || !tp) return;
  try { if (!fs.statSync(tp).isFile()) { log(`transcript is not a file: ${tp}`); return; } } catch { return; } // not written yet
  const stateFile = SID_OK(i.session_id) ? path.join(ORCH_DIR, 'sweep-state', `${i.session_id}.json`) : undefined; // where this session's last sweep stopped: only the bytes added since are read
  try { lines.push(...LG().ingestTranscript(tp, { sessionId: i.session_id, held, stateFile })); } catch (e) { log(`sweep ${tp}: ${e && e.message}`); }
}
function logIngestErrors(lines) { // errors and skipped stops one line each; ambiguous notices one line per hook call (prompt + sweep may both see one)
  let amb = 0;
  for (const l of lines) { if (/ledger none:ambiguous$/.test(l)) amb++; else if (/^\S+: error /.test(l) || /stop skipped:/.test(l)) log(`ingest: ${l}`); }
  if (amb) log(`ingest: -: stop none, ledger none:ambiguous${amb > 1 ? ` (x${amb})` : ''}`);
}

// Compaction-gap hint (prompt only). A compaction can drop the chat's memory of running attempts, and a notice that lands
// during one can be missed. For each held run: an attempt with a launch but no notice recorded for 2+ minutes (launched in
// the last 24 h), or a lease that expired within the last hour (read before this prompt renews it). One line, at most once per
// run per 10 minutes (state: $ORCH_DIR/gap-hint-state.json, {run: epoch ms}). "compaction detected" is added when the
// transcript tail holds a compact_boundary record from the last 30 minutes. Read-only apart from that state file; fail open.
// Not flagged (the hint is for attempts that were lost, not ones in flight or finished): an attempt with an outcome (closed with
// ledger.js close), one superseded by a later attempt on its row (retry or continuation), and one this session started less than
// gapInflightSec ago (config, default GAP_INFLIGHT_MS = 2 h). All read from the manifest the fold already reads: no extra file
// read. A session's id survives a compaction, so a young attempt of this session stays quiet here even across one; the
// SessionStart (compact|resume) line already tells that chat to /boss:resume, which reconciles running rows.
const GAP_INFLIGHT_MS = 7200000, GAP_MIN_MS = 120000, GAP_MAX_MS = 86400000, GAP_LEASE_MS = 3600000, GAP_REPEAT_MS = 600000, COMPACT_MS = 1800000, TAIL_BYTES = 262144;
function compactionSeen(tp, now) { // a compact_boundary record in the transcript's tail dated within COMPACT_MS
  try {
    const fd = fs.openSync(tp, 'r');
    try {
      const size = fs.fstatSync(fd).size, n = Math.min(size, TAIL_BYTES), buf = Buffer.alloc(n);
      fs.readSync(fd, buf, 0, n, size - n);
      for (const l of buf.toString('utf8').split('\n')) {
        if (!l.includes('"compact_boundary"')) continue;
        let x; try { x = JSON.parse(l); } catch { continue; }
        const t = x && x.subtype === 'compact_boundary' ? Date.parse(x.timestamp) : NaN;
        if (Number.isFinite(t) && now - t < COMPACT_MS) return true;
      }
    } finally { fs.closeSync(fd); }
  } catch {}
  return false;
}
function gapHints(i, H, now) { // H = heldRuns() read before the renew; -> context lines
  const stateFile = path.join(ORCH_DIR, 'gap-hint-state.json'), st = readJson(stateFile), state = st && typeof st === 'object' && !Array.isArray(st) ? st : {};
  const lines = []; let dirty = false, compacted = null;
  let inflightMs = GAP_INFLIGHT_MS; try { const c = Number(MF().cfg.gapInflightSec); if (Number.isFinite(c) && c > 0) inflightMs = c * 1000; } catch {}
  for (const h of H) {
    const run = h.run, last = Number(state[run]);
    if (Number.isFinite(last) && last <= now && now - last < GAP_REPEAT_MS) continue;
    const exp = Date.parse(h.lease.expires), expired = Number.isFinite(exp) && exp <= now && now - exp < GAP_LEASE_MS;
    let ids = [];
    if (runExists(run)) { try { ids = MF().openAttemptIds(run, { now, minAgeMs: GAP_MIN_MS, maxAgeMs: GAP_MAX_MS, skipSuperseded: true, inflightSid: i.session_id, inflightMs }); } catch (e) { log(`gap fold ${run}: ${e && e.message}`); } }
    if (!ids.length && !expired) continue;
    if (compacted === null) compacted = typeof i.transcript_path === 'string' && i.transcript_path ? compactionSeen(i.transcript_path, now) : false;
    const parts = []; if (ids.length) parts.push(`${ids.length} attempt(s) with no recorded notice (${ids.join(', ')})`); if (expired) parts.push('an expired lease');
    lines.push(`[orch] possible compaction gap${compacted ? ' (compaction detected)' : ''}: run ${run} has ${parts.join(' and ')}. Run: ${MANIFEST_CLI} reconcile --run ${run} --sid=$WFP_SID, then ListAgents.`);
    state[run] = now; dirty = true;
  }
  if (dirty) { try { const tmp = `${stateFile}.${process.pid}.tmp`; fs.writeFileSync(tmp, JSON.stringify(state)); fs.renameSync(tmp, stateFile); } catch (e) { log(`gap state: ${e && e.message}`); } }
  return lines;
}

// UserPromptSubmit, lease holder only: ingest the notices in the prompt, then sweep (both gated to the held runs), then renew.
// One context line per new ledger line, then the compaction-gap hints (gapHints).
function prompt(i) {
  if (i.agent_id) return null;
  const H = heldRuns(i.session_id), held = H.map(h => h.run); if (!held.length) return null;
  const now = Date.now(), lines = [], text = typeof i.prompt === 'string' ? i.prompt : '';
  if (text.includes('<task-notification>')) {
    try { lines.push(...LG().ingestText(text, { via: 'prompt', transcriptPath: typeof i.transcript_path === 'string' ? i.transcript_path : undefined, sessionId: i.session_id, held })); }
    catch (e) { log(`ingest prompt: ${e && e.message}`); }
  }
  sweep(i, lines, held);
  let gap = []; try { gap = gapHints(i, H, now); } catch (e) { log(`gap hint: ${e && e.message}`); } // before the renew: it revives an expired lease
  MF().renewIfMine(i.session_id, { hook: true });
  logIngestErrors(lines);
  const ids = []; for (const l of lines) { const m = /^(\S+): stop (?:new|dup), ledger written$/.exec(l); if (m && m[1] !== '-' && !ids.includes(m[1])) ids.push(m[1]); }
  const show = v => v == null ? 'unknown' : String(v);
  return ctx('UserPromptSubmit', ids.map(id => { const x = ledgerNumbers(id) || {};
    return `[orch] ledger: ${id} tokens ${show(x.tokens)}, tools ${show(x.tools)}, ms ${show(x.ms)}. Close: ledger.js close --attempt ${id} (+ --first-pass --outcome)`; }).concat(gap));
}

// Stop (end of each main turn), lease holder only: sweep for mid-turn notices (gated to the held runs), then renew. Silent.
function stop(i) {
  if (i.agent_id) return null;
  const held = heldRuns(i.session_id).map(h => h.run); if (!held.length) return null;
  const lines = []; sweep(i, lines, held);
  MF().renewIfMine(i.session_id, { hook: true });
  logIngestErrors(lines);
  try { require(path.join(LIB, 'processes.js')).create({dir: paths.dataDir()}).report({session: i.session_id}, true); }
  catch (e) { log(`process report: ${e && e.message}`); }
  return null;
}

// SubagentStop, lease holder only: an `agent-stopped` stop (final:false) for an agent that maps to an attempt or a start. Silent.
// D1: the attempt's run when this session holds it (an ID for a run it does not hold: nothing); without an ID, the held run
// whose manifest has a `start` with this agent's toolUseId (the start pretool wrote). Two such runs: nothing, one log line.
function substop(i) {
  const H = heldRuns(i.session_id); if (!H.length) return null;
  if (!SID_OK(i.agent_id)) return null;
  let mf = null;
  if (typeof i.agent_transcript_path === 'string' && /\.jsonl$/.test(i.agent_transcript_path)) mf = i.agent_transcript_path.replace(/\.jsonl$/, '.meta.json');
  else if (typeof i.transcript_path === 'string' && i.transcript_path) mf = path.join(path.dirname(i.transcript_path), i.session_id, 'subagents', `agent-${i.agent_id}.meta.json`);
  const meta = mf ? readJson(mf) : null;
  if (!meta || typeof meta !== 'object') return null;
  const held = H.map(h => h.run);
  const M = MF(), d = M.parseDescription(meta.description), a = d.attempt ? M.parseAttemptId(d.attempt) : null;
  let run = null, attempt = null, row = null;
  if (a) {
    if (!held.includes(a.run)) return null; // D1 (1): not this session's run
    if (!runExists(a.run)) { log(`run folder missing: ${path.join(RUNS, a.run)} (substop ${i.agent_id} for ${a.id} not written)`); return null; }
    run = a.run; attempt = a.id; row = a.row;
  } else if (typeof meta.toolUseId === 'string' && meta.toolUseId) {
    const hits = [];
    for (const r of held) {
      if (!runExists(r)) continue;
      const s = M.readEvents(r).events.find(e => e.type === 'start' && e.tool_use_id === meta.toolUseId);
      if (s) hits.push({ r, s });
    }
    if (hits.length > 1) { log(`substop ${i.agent_id} not written: start ${meta.toolUseId} is in ${hits.length} held runs (${hits.map(h => h.r).join(', ')})`); return null; }
    if (hits.length === 1) { run = hits[0].r; attempt = hits[0].s.attempt || null; row = hits[0].s.row == null ? null : hits[0].s.row; }
  }
  if (!run) return null; // unmapped: nothing
  const msg = i.last_assistant_message == null ? '' : typeof i.last_assistant_message === 'string' ? i.last_assistant_message : JSON.stringify(i.last_assistant_message);
  const rb = Buffer.byteLength(msg);
  // P29: the resolved model from the subagent transcript's head (its `model` attachment sits near byte 14k; a 256 KB cap keeps this cheap).
  // The notice ingest falls back to this stop's model_id when its own read finds none. Fail open: null.
  let modelId = null;
  try { const tf = mf.replace(/\.meta\.json$/, '.jsonl'); modelId = LG().transcriptFacts(tf, false, 262144).model_id || null; } catch (e) { log(`substop model_id: ${e && e.message}`); }
  M.appendEvent(run, { type: 'stop', key: `${i.agent_id}:substop:${i.stop_hook_active ? 1 : 0}:${rb}`, attempt, row, via: 'substop', session: i.session_id,
    task_id: i.agent_id, tool_use_id: typeof meta.toolUseId === 'string' ? meta.toolUseId : null, status: null, phase: 'agent-stopped', final: false,
    tokens: null, tools: null, ms: null, numbers_from: null,
    agent: i.agent_type == null ? null : paths.agentBase(i.agent_type) || null, // SubagentStop agent_type without the plugin prefix (row 17 compares it with the start's subagent_type)
    model: meta.model == null ? null : String(meta.model), effort: meta.effort == null ? null : String(meta.effort),
    model_from: meta.model == null ? null : 'meta', effort_from: meta.effort == null ? null : 'meta', model_id: modelId, effort_observed: null,
    result_bytes: rb, output_file: null, renotice: false }, { hook: true });
  // Reporting only: failures cannot affect the task-stop decision.
  try { if (attempt) require(path.join(LIB, 'processes.js')).create({dir: paths.dataDir()}).report({attempt, session: i.session_id}, true); }
  catch (e) { log(`process report: ${e && e.message}`); }
  return null;
}

// SessionStart (compact|resume), A-05 / CT-12. No file writes.
// One held lease (live, or expired and still its own): CT-12 for that run. Several: one line naming them (by run name).
// None: silent (OD-1); ACTIVE is never read.
function session(i) {
  if (i.agent_id) return null;
  const sid = i.session_id, now = Date.now(), H = heldRuns(sid);
  const norm = p => String(p || '').replace(/\/+$/, '');
  if (H.length > 1) {
    const names = H.map(h => h.run).sort();
    return ctx('SessionStart', [`[orch] This session holds leases for runs ${names.join(', ')}. Run /boss:resume <run> for the run you drive before launching anything.`]);
  }
  if (!H.length) return null; // holds no lease: silent, whatever ACTIVE says
  const L = H[0].lease;
  const runPath = L.path ? norm(L.path) : path.join(RUNS, H[0].run);
  const held = isLive(L, now) ? 'this session' : `expired since ${hhmm(L.expires)}`;
  const name = path.basename(runPath), shown = runPath === path.join(RUNS, name) ? name : runPath; // the run name when it sits in the runs dir (/boss:resume resolves it), else the full path
  return ctx('SessionStart', [`[orch] Active run: ${shown} (lease: ${held}). Run /boss:resume ${name} before launching anything (it reconciles running rows).`]);
}

const MODES = { pretool, prompt, stop, substop, session };
process.on('uncaughtException', e => { log(`uncaught: ${e && e.message}`); process.exit(0); });
if (require.main === module) {
  if (!MODES[MODE]) { log(`unknown mode '${String(MODE).slice(0, 40)}'`); process.exit(0); }
  const chunks = [];
  process.stdin.on('data', d => chunks.push(d));
  process.stdin.on('error', () => process.exit(0));
  process.stdin.on('end', () => {
    let i = null;
    try { i = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'); } catch (e) { log(`bad hook JSON: ${e && e.message}`); process.exit(0); }
    try {
      if (!i || typeof i !== 'object' || Array.isArray(i)) { log('hook input is not a JSON object'); process.exit(0); }
      const o = MODES[MODE](i);
      if (o) process.stdout.write(JSON.stringify(o));
    } catch (e) { log(`${e && e.message}`); }
    process.exit(0);
  });
}
