#!/usr/bin/env node
// orch-stop-gate: PreToolUse (Agent|Task) gate behind the Boss board's Stop. Plain node, regex only, no dependencies.
// The owner stops an orchestrator run in Boss: the extension writes <boss root>/runs/orchestrator/<run>/control/cancel
// (root = paths.progressRoot(): $BOSS_PROGRESS_DIR, else <config dir>/boss). manifest.js launch refuses a stopped run (exit 4); this hook is the second
// wall: it denies an Agent call whose description or brief carries an attempt ID of a stopped run (the description's
// `<run>#<row>.<n>`, or an `Orch-Attempt: <run>#<row>.<n>` line in the prompt).
// Rules:
//  - Presence of the cancel file is the whole contract (a malformed file still denies). Only a deny is ever printed.
//  - Fail open: any error, an unreadable control dir, a missing file, input that is not JSON -> exit 0 with no output.
//  - A call that carries no attempt ID is attributed like orch-attempts.js (D1 chain, step 3): when the calling session holds exactly one run lease
//    and that run is stopped, deny. Needs session_id and lease-store.js (lazy; any failure = no lease = pass). Two or more held runs: pass.
//  - Subagent calls (agent_id in the input) pass: the stop is for the main session that launches rows.
//  - A scratch ORCH_DIR / ORCH_RUNS_DIR without BOSS_PROGRESS_DIR is skipped, like manifest.js (tests never read the real registry).
//  - Registered by hooks/hooks.json, in the same PreToolUse group as orch-attempts.js pretool.
'use strict';
const fs = require('fs'), path = require('path');
const paths = require(path.join(__dirname, '..', 'lib', 'paths.js'));

const RUN_BODY = '([A-Za-z0-9][A-Za-z0-9._-]{0,99})';
const DESC_ID_RE = new RegExp('^\\s*(?:\\[[^\\]\\s]+\\]\\s*)?' + RUN_BODY + '#\\d{1,4}[A-Za-z]?\\.[1-9]\\d{0,2}(?=\\s|$)');
const BRIEF_ID_RE = new RegExp('^[ \\t]*Orch-Attempt:[ \\t]*' + RUN_BODY + '#\\d{1,4}[A-Za-z]?\\.[1-9]\\d{0,2}[ \\t]*$', 'gm');
const RUN_OK = s => typeof s === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(s) && !s.includes('..');

function skipped() { return !process.env.BOSS_PROGRESS_DIR && paths.isScratch(); }
function cancelFile(run) {
  const base = paths.progressRoot();
  return path.join(base, 'runs', 'orchestrator', run, 'control', 'cancel');
}
function namedRuns(ti) { // runs the call carries an attempt ID for: Orch-Attempt lines first, then the description
  const out = [], add = r => { if (RUN_OK(r) && !out.includes(r)) out.push(r); };
  for (const m of String(ti.prompt == null ? '' : ti.prompt).matchAll(BRIEF_ID_RE)) add(m[1]);
  const d = DESC_ID_RE.exec(String(ti.description == null ? '' : ti.description)); if (d) add(d[1]);
  return out;
}
function heldRuns(sid) { // the runs this session's leases cover (live, or expired and still its own); [] on any failure
  try {
    if (typeof sid !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(sid) || sid.includes('..')) return [];
    const h = require(path.join(__dirname, '..', 'lib', 'lease-store.js')).heldBy(paths.dataDir(), sid);
    return Array.isArray(h) ? h.map(x => x && x.run).filter(RUN_OK) : [];
  } catch { return []; }
}
function decide(input) { // -> deny object or null
  if (!input || typeof input !== 'object' || input.agent_id || skipped()) return null;
  const ti = input.tool_input && typeof input.tool_input === 'object' ? input.tool_input : {};
  let runs = namedRuns(ti);
  if (!runs.length) { const held = heldRuns(input.session_id); if (held.length === 1) runs = held; } // no ID: the session's only lease
  for (const run of runs) {
    let present = false;
    try { fs.lstatSync(cancelFile(run)); present = true; } catch { present = false; }
    if (present) return { hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny',
      permissionDecisionReason: `Run ${run} was stopped by the owner in Boss: launch nothing new for it. Finish or park the running rows and tell the owner. (Undo: Boss board > Undo stop, or rm ${cancelFile(run)})` } };
  }
  return null;
}
function main() {
  let raw = '';
  try { raw = fs.readFileSync(0, 'utf8'); } catch { return; }
  let input; try { input = JSON.parse(raw); } catch { return; }
  let out = null; try { out = decide(input); } catch { out = null; }
  if (out) process.stdout.write(JSON.stringify(out) + '\n');
}
if (require.main === module) { try { main(); } catch { /* fail open */ } process.exit(0); }
module.exports = { decide, namedRuns };
