#!/usr/bin/env node
// orch-return-cap: return byte cap for orch-* subagents (D4 design section 6, row 16). Plain node, no dependencies.
// Modes (argv[2]):
//   substop   SubagentStop. Measures last_assistant_message. Registered by hooks/hooks.json.
//   handback  PreToolUse SubagentHandback (fires inside the subagent). Measures tool_input.message. Built and tested on fake
//             JSON only; not registered (auto mode is not in use here, see <run>/16-return-cap.md).
// Config (lib/defaults.json overlaid by <data dir>/config.json, via paths.loadConfig('engine')): returnCapBytes (2048), returnCapAgentTypes ("^orch-"), returnCapMode ("flag" | "block-once").
//   flag        over the cap: append return_over_cap {action:"flagged"} to the manifest; never blocks.
//   block-once  over the cap: the first time per agent, block (SubagentStop decision) or deny (handback) with a reason that tells
//               the agent to move the detail into its result file and end with the WORKER.md summary; the event is
//               {action:"blocked"}. The second time (stop_hook_active, or a blocked event already recorded): append
//               {action:"passed-after-block"} and let it pass. "Once" rides on appendEvent's type+key dedupe under the
//               manifest lock (key <agent_id>:blocked). If that record can't be written, it never blocks.
// Rules (same as orch-attempts.js, owner 2026-10-20):
//  - Fail open: always exit 0. Errors go to one line in <data dir>/errors.log (capped at 256 KB by dropping the older half).
//  - Fast path: agent_type not matching, or bytes <= cap, returns before the lease or manifest.js is touched.
//  - Only a session that holds a run's lease (leases/<run>.json in the data dir, or the legacy ACTIVE.lease for its own run only; live,
//    or expired and still its own; read through lib/lease-store.js) writes or blocks. Any other session passes straight
//    through: nothing written, nothing printed, nothing logged. ACTIVE never picks a run.
//  - Which held run gets the event (owner decision D1): the attempt ID in meta.json ends the chain (write to that run if this
//    session holds it, else nothing). With no ID: the held run whose manifest has a `start` with this agent's toolUseId (two such
//    runs: nothing, one log line); else the session's only lease; else nothing plus one errors.log line. No record, no block.
//  - lease-store.js missing or throwing: one errors.log line, and the session counts as holding no lease. A per-run lease file
//    that can't be read cleanly reads as "not held" (lease-store.js never falls back to the legacy file then).
// Paths come from lib/paths.js (BOSS_DATA; ORCH_DIR, ORCH_RUNS_DIR in tests, also read by manifest.js).
// returnCapAgentTypes is matched on agentBase(agent_type), so `boss:orch-scout` matches `^orch-`.
'use strict';
const fs = require('fs'), path = require('path');
const paths = require(path.join(__dirname, '..', 'lib', 'paths.js'));
const ORCH_DIR = paths.dataDir();
const RUNS = paths.runsDir();
const LIB = path.join(__dirname, '..', 'lib');
const LOG_CAP = 262144;
const MODE = process.argv[2] || '';
const DEFAULTS = { returnCapBytes: 2048, returnCapAgentTypes: '^orch-', returnCapMode: 'flag' };
const CAP_MODES = ['flag', 'block-once'];
const SID_OK = s => typeof s === 'string' && /^[A-Za-z0-9._-]{1,128}$/.test(s) && !s.includes('..');
const RUN_OK = s => typeof s === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(s) && !s.includes('..');
const readJson = f => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return null; } };
const runExists = r => { try { return fs.statSync(path.join(RUNS, r)).isDirectory(); } catch { return false; } };
let manifestMod = null, leaseStore;
const MF = () => manifestMod || (manifestMod = require(path.join(LIB, 'manifest.js')));

function log(msg) { // one line; the file is capped by dropping its older half. Never throws.
  try {
    const f = paths.errorsLog(); fs.mkdirSync(path.dirname(f), { recursive: true });
    try {
      if (fs.statSync(f).size > LOG_CAP) {
        const t = fs.readFileSync(f, 'utf8'), k = t.indexOf('\n', Math.floor(t.length / 2));
        const tmp = `${f}.${process.pid}.tmp`; fs.writeFileSync(tmp, k >= 0 ? t.slice(k + 1) : ''); fs.renameSync(tmp, f);
      }
    } catch {}
    fs.appendFileSync(f, `${new Date().toISOString()} orch-return-cap ${MODE}: ${String(msg).replace(/\s+/g, ' ').slice(0, 400)}\n`);
  } catch {}
}
function config() { // config.json over the defaults; a bad value logs one line and falls back
  const L = paths.loadConfig('engine');
  if (L.error) log(`config: ${L.error}; using the shipped defaults`);
  const c = Object.assign({}, DEFAULTS, L.cfg && typeof L.cfg === 'object' && !Array.isArray(L.cfg) ? L.cfg : {});
  if (!Number.isSafeInteger(c.returnCapBytes) || c.returnCapBytes < 1) { log(`returnCapBytes '${c.returnCapBytes}' invalid; using ${DEFAULTS.returnCapBytes}`); c.returnCapBytes = DEFAULTS.returnCapBytes; }
  try { c.typeRe = new RegExp(String(c.returnCapAgentTypes)); } catch { log(`returnCapAgentTypes '${c.returnCapAgentTypes}' is not a regex; using ${DEFAULTS.returnCapAgentTypes}`); c.typeRe = new RegExp(DEFAULTS.returnCapAgentTypes); }
  if (!CAP_MODES.includes(c.returnCapMode)) { log(`returnCapMode '${c.returnCapMode}' unknown; using flag`); c.returnCapMode = 'flag'; }
  return c;
}
function LS() { // lease-store.js (fs and path only), loaded lazily; a failure logs one line and the session holds nothing
  if (leaseStore !== undefined) return leaseStore;
  try { leaseStore = require(path.join(LIB, 'lease-store.js')); } catch (e) { leaseStore = null; log(`lease-store.js not loaded (${e && e.code || e && e.message}); treated as holding no lease`); }
  return leaseStore;
}
function heldRuns(sid) { // the runs whose lease this session holds (live, or expired and still its own), newest renewed first
  if (!SID_OK(sid)) return [];
  const S = LS(); if (!S) return [];
  try { const h = S.heldBy(ORCH_DIR, sid); return Array.isArray(h) ? h.filter(x => x && RUN_OK(x.run) && x.lease).map(x => x.run) : []; } catch (e) { log(`heldBy: ${e && e.message}`); return []; }
}
function metaPath(i, inside) { // the agent's meta.json. Inside the subagent (handback) transcript_path may be the agent's own file.
  const named = p => typeof p === 'string' && path.basename(p) === `agent-${i.agent_id}.jsonl` ? p.replace(/\.jsonl$/, '.meta.json') : null;
  if (inside && named(i.transcript_path)) return named(i.transcript_path);
  if (typeof i.agent_transcript_path === 'string' && /\.jsonl$/.test(i.agent_transcript_path)) return i.agent_transcript_path.replace(/\.jsonl$/, '.meta.json');
  if (typeof i.transcript_path === 'string' && i.transcript_path && SID_OK(i.session_id))
    return path.join(path.dirname(i.transcript_path), i.session_id, 'subagents', `agent-${i.agent_id}.meta.json`);
  return null;
}
function locate(i, held, inside) { // D1 -> {run, attempt, row, tool_use_id} or null. held: the runs this session holds (non-empty)
  const mf = metaPath(i, inside), meta = mf ? readJson(mf) : null;
  const M = MF(), d = meta && typeof meta === 'object' ? M.parseDescription(meta.description) : { attempt: null };
  const a = d.attempt ? M.parseAttemptId(d.attempt) : null;
  const tuid = meta && typeof meta.toolUseId === 'string' && meta.toolUseId ? meta.toolUseId : null;
  if (a) { // D1 (1): the attempt ID ends the chain
    if (!held.includes(a.run)) return null; // not this session's run: nothing, silently
    if (!runExists(a.run)) { log(`run folder missing: ${path.join(RUNS, a.run)} (cap event for ${a.id}, agent ${i.agent_id}, not written)`); return null; }
    return { run: a.run, attempt: a.id, row: a.row, tool_use_id: tuid };
  }
  if (tuid) { // D1 (2): the held run whose manifest has a start with this agent's toolUseId
    const hits = [];
    for (const r of held) {
      if (!runExists(r)) continue;
      const s = M.readEvents(r).events.find(e => e.type === 'start' && e.tool_use_id === tuid);
      if (s) hits.push({ r, s });
    }
    if (hits.length > 1) { log(`cap event for agent ${i.agent_id} not written: start ${tuid} is in ${hits.length} held runs (${hits.map(h => h.r).sort().join(', ')})`); return null; }
    if (hits.length === 1) { const s = hits[0].s; return { run: hits[0].r, attempt: s.attempt || null, row: s.row != null ? s.row : null, tool_use_id: tuid }; }
  }
  if (held.length === 1) { // D1 (3): the session's only lease
    if (!runExists(held[0])) { log(`lease run folder missing: ${path.join(RUNS, held[0])} (cap event for agent ${i.agent_id} not written)`); return null; }
    return { run: held[0], attempt: null, row: null, tool_use_id: tuid };
  }
  log(`cap event for agent ${i.agent_id} not written: this session holds ${held.length} leases (${held.slice().sort().join(', ')}) and nothing maps the agent to one`); // D1 (4)
  return null;
}

// Shared core. text: the measured message; inside: true for handback. Returns the hook output object or null.
function check(i, text, inside) {
  if (!i || typeof i.agent_type !== 'string' || !SID_OK(i.agent_id)) return null;
  const c = config();
  if (!c.typeRe.test(paths.agentBase(i.agent_type))) return null;
  const bytes = Buffer.byteLength(text), cap = c.returnCapBytes;
  if (bytes <= cap) return null;
  const held = heldRuns(i.session_id); if (!held.length) return null; // another session's agent: pass, silently
  const loc = locate(i, held, inside); if (!loc) return null;
  const M = MF(), kind = inside ? 'handback' : 'subagent-stop';
  const ev = action => ({ type: 'return_over_cap', key: `${i.agent_id}:${action}`, attempt: loc.attempt, row: loc.row, via: inside ? 'handback' : 'cap',
    session: i.session_id, task_id: i.agent_id, tool_use_id: loc.tool_use_id, agent_type: paths.agentBase(i.agent_type), bytes, cap, action, mode: kind,
    cap_mode: c.returnCapMode });
  if (c.returnCapMode === 'flag') { M.appendEvent(loc.run, ev('flagged'), { hook: true }); return null; }
  // block-once
  if (!inside && i.stop_hook_active === true) { M.appendEvent(loc.run, ev('passed-after-block'), { hook: true }); return null; }
  const r = M.appendEvent(loc.run, ev('blocked'), { hook: true });
  if (!r || r.skipped) return null; // no record, no block: "once" can't be guaranteed
  if (r.dup) { M.appendEvent(loc.run, ev('passed-after-block'), { hook: true }); return null; }
  if (inside) return { hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny',
    permissionDecisionReason: `[orch] Your handback message is ${bytes} bytes; the cap is ${cap}. Move the detail into your result file and hand back the WORKER.md summary (at most 5 lines).` } };
  return { decision: 'block', reason: `[orch] Your final message is ${bytes} bytes; the cap is ${cap}. Move the detail into your result file and end with the WORKER.md summary (at most 5 lines).` };
}
const asText = v => v == null ? '' : typeof v === 'string' ? v : JSON.stringify(v);
function substop(i) { return check(i, asText(i.last_assistant_message), false); }
function handback(i) {
  if (i.tool_name != null && i.tool_name !== 'SubagentHandback') return null;
  const ti = i.tool_input && typeof i.tool_input === 'object' ? i.tool_input : {};
  return check(i, asText(ti.message), true);
}

const MODES = { substop, handback };
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
