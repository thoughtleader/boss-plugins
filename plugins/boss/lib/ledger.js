#!/usr/bin/env node
// ledger: per-run token/outcome ledger for the orchestrator. Plain node, no dependencies.
// One JSONL file per run: <data dir>/runs/<run>/ledger.jsonl (lib/paths.js resolves the data dir; env ORCH_RUNS_DIR overrides the runs dir, for tests).
// One line per ATTEMPT of a plan row, so first-pass stats land on the model/effort that actually ran that attempt.
// Usage: node "$BOSS_LIB/ledger.js" help
'use strict';
const fs = require('fs'), path = require('path');
const paths = require('./paths.js');
const ORCH_DIR = paths.dataDir(); // same resolution as manifest.js
const RUNS = paths.runsDir();
let manifestMod = null; // manifest.js is required lazily (ingest, --attempt), so add/show/update never depend on it
const MF = () => manifestMod || (manifestMod = require(path.join(__dirname, 'manifest.js')));
let leaseMod = null; // lease-store.js is required lazily (ingest only: --sid / --run, and the no-sid live-holder check)
const LSM = () => leaseMod || (leaseMod = require(path.join(__dirname, 'lease-store.js')));
const ROLES = ['planning', 'plan-check', 'discovery', 'implementation', 'diagnosis', 'review', 'default'];
const MODELS = ['haiku', 'sonnet', 'opus'], EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']; // the Claude ranking
const EFFORTS_ACCEPTED = [...EFFORTS, 'ultra', 'unknown']; // ultra: Codex rows; unknown: effort not recorded (kept out of ranking and stats)
const isCodex = x => x.model === 'codex' || /^gpt-/.test(String(x.model)) || /^council(-\d{1,2}seat)?$/.test(String(x.model)); // non-Claude rows (Codex, gpt-*, council): own section in show, never ranked against Claude
const MODES = ['subagent', 'wave', 'workflow', 'agent-team', 'background-bash']; // background-bash: a council run or other shell job (no Agent notice)
const SMALL = 5; // a model/effort with fewer recorded outcomes than this gets its rate flagged as anecdotal

class UsageError extends Error {}
const fail = msg => { throw new UsageError(msg); };
const HELP = `ledger.js: per-run token/outcome ledger, one JSONL line per attempt of a plan row
  add    --run <run> --row <n> --role <role> --agent <type> --model <m> --effort <e> --mode <mode>
         --tokens N --tools N --ms N --repo <repo> --area <area> [--first-pass y|n] [--retries N] [--escalated y|n] [--retry-kind continue|fresh] [--attempt <id>]
         [--cause owner-tune|review-findings|failed|tooling]
  update --attempt <id> | --run <run> --ts <iso> | --run <run> --row <n>
         [--first-pass y|n] [--cause owner-tune|review-findings|failed|tooling] [--retries N] [--escalated y|n] [--retry-kind continue|fresh] [--tokens N] [--tools N] [--ms N]
  close  --attempt <id> --first-pass y|n --outcome accepted|retry|blocked|dropped [--cause owner-tune|review-findings|failed|tooling] [--retries N] [--escalated y|n]
         the row close: first-pass, cause and outcome in one call (one line edit; one outcome event with via close, cause and outcome when the
         run has a manifest). --cause is required with --first-pass n or --outcome retry, and refused with --first-pass y unless owner-tune.
         Prints 'ledger: closed <id>: firstPass=<b> cause=<c> outcome=<o> closed=<iso>'. Exit 2 on a usage error or when the line is missing.
  backfill-models [--run R] [--since <date|iso>] [--dry-run] [--json]
         fills model_id (the resolved model, e.g. claude-opus-5-5) on ledger lines that lack it: from the attempt's stop events, else the
         subagent transcript's model attachment (agent-<task>.jsonl under the start's or the launch/stop sessions' subagents folders,
         \$ORCH_PROJECTS_DIR). Idempotent (only lines still without model_id are written, marked model_id_from: backfill; manifest.jsonl is
         never edited). Prints 'backfill-models: <runs> runs, <lines> lines: already A, resolved R (stop S, transcript T), written W,
         unresolved U', then the resolved models, then each unresolved line with its reason: codex, no-id, no-launch, no-stop,
         no-transcript, no-model-record. --dry-run writes nothing.
  show   [--run <run>] [--repo <repo>] [--area <area>] [--role <role>] [--last N]     (lists the last 20 attempts of all runs by default;
         the stats cover every attempt matching the filters, n is printed)
  ingest [--file F | --transcript P [--tail-bytes N|all]] [--session-id S] [--meta <meta.json>] [--sid S [--run R]]
         reads completion notices (stdin when no file is given; --transcript sweeps the enqueue / queued_command records of a
         main transcript, the last sweepTailBytes by default) and prints one line per notice:
         <id|->: stop <new|dup|none|skipped:<why>>, ledger <written|updated|same|none:<why>>
         --sid S (else env WFP_SID; an empty --sid= means no session id, with no env fallback) gates the write: only runs S
         holds a lease on (live, or expired and still S's) may be written. --run R (needs a sid; R's folder must exist) adds R
         to that set unless a session other than S holds R live. --session-id is the transcript owner (meta.json lookup) only.
  sync   [--rebuild] [--no-transcripts] [--no-git] [--full-git]     derives metrics.db (\$ORCH_METRICS_DB, default ${path.join(ORCH_DIR, 'metrics.db')}) from every runs/*/ledger.jsonl and
         manifest.jsonl: one transaction, upsert by stable id, idempotent; --rebuild drops and reloads it. The JSONL files stay the
         record; the db is derived, gitignored and rebuildable (node:sqlite, loaded only by sync and report). sync also loads the
         transcripts (\$ORCH_PROJECTS_DIR, default <config dir>/projects: main, subagent and workflow-agent jsonl, incremental by stored
         byte offset) and the wfp .tel.json snapshots (\$ORCH_WFP_STATE); --no-transcripts skips both, report never loads them.
         --rebuild (and a schema bump) reloads only the ledger/manifest tables: every tx_* table, wfp_telemetry and prices are kept
         and migrated in place (Claude Code deletes transcripts after 30 days; metrics.db is the record). sync also stores owner
         commits from git log (tx_git_commits; --no-git skips, --full-git rescans from the first transcript day) and prints a
         catch-up note when the previous sync is over 7 days old.
  cost   [--run R] [--limit N] [--json]     read-only (no lock, no sync, no db): with --run, line 1 'cost: run R: <rows> rows,
         <attempts> attempts, tokens <sum>, tools <sum>, time <sum>', then one line per row (row number, then letter): attempts,
         model/effort (the accepted attempt's, else the latest's), tokens, tools, time, retries (attempts - 1), escalated (y if
         any attempt was), first-pass (the first attempt's y/n/-). A null number shows '?' (the known part's sum before it) and
         an 'unknown:' line counts those attempts. Without --run: one line per run that has a ledger.jsonl, newest first
         (--limit, default 20, 1-50): run, rows, attempts, tokens, time, retries, escalations (rows escalated). Token and time
         counts only, no prices. A run with no ledger prints 'cost: run R has no ledger yet' (exit 0).
  cron   install | remove | status     the weekly sync: a user crontab line, Sunday 03:00, nice/ionice, output to cron-sync.log (idempotent; no sudo; status says
         if the cron daemon is missing). Not a hook and not a service.
  dashboard [--no-sync] [--out PATH] [--open] [--commits] [--json]
         on demand only (no server, no hook): runs sync (unless --no-sync), then writes ONE self-contained HTML page (data embedded,
         no network) to --out (default ${path.join(ORCH_DIR, 'dashboard.html')}, gitignored) and prints its path: runs (ledger
         numbers as cost, plus transcript main/subagent tokens and units), by model/effort and role, before/after sessions, planning
         churn, data quality. --open opens it in Windows (wslview, else explorer.exe), else just prints the path. --commits also
         rescans git logs from the first transcript day (commits are otherwise stored by every sync). --json prints the data object instead of a page.
  report tokens-per-accepted|first-pass [--run <run>] [--role <role>] [--repo <repo>] [--area <area>] [--json] [--no-sync]
         syncs (this writes metrics.db, creating it on a first run, and says so on stderr), then reads it; --no-sync reads the db as it
         stands and writes nothing (exit 2 when there is no db yet, and a stale-sync note on stderr): the same role lines and numbers as show (tok/accepted by role and accepted model/effort;
         first-pass rate by model/effort), Claude and Codex in their own sections. Staleness catch-up: when the last transcript sync is over
         2 days old (env ORCH_SYNC_STALE_DAYS; 'off' disables) it also syncs transcripts first (no git scan) and prints a note on stderr;
         dashboard --no-sync prints the same note instead of syncing. The Sunday cron stays; this covers the weeks WSL was off.
  report retries [--run <run>] [--role <role>] [--repo <repo>] [--area <area>] [--json]
         what the attempts that needed a retry (ledger lines with firstPass n) cost, by cause, role and model/effort: attempts and tokens per
         group, a by-cause line, and the share of all attempts and tokens. Cause = the line's own cause, else the next attempt's
         retry_cause, else unstated (the share not stated is printed; history from before a cause was required cannot be recovered).
         Read-only: it reads runs/*/ledger.jsonl only (no sync, no metrics.db, no node:sqlite) and writes nothing.
Semantics
  - One line per attempt: run add after every completion notice, including the retry. A second add for the same
    run + row on a different model/effort is the retry attempt: --retries and --escalated are filled in (escalated = the
    step went up) unless given. The same model/effort again is refused as a duplicate unless --retries >= 1 is given.
    add --attempt <id> makes <id> the line id (refused if a line with that id exists); without it the id is
    <run>#<row>.<n>, n one past the row's highest attempt number (an explicit id suffix, else the line's ordinal in the row).
  - update edits exactly one line (outcomes: --first-pass after review; also retries/escalated/numbers): --attempt <id> picks
    the line with that id (and appends an outcome event when the run has a manifest.jsonl); --run R --ts <iso> picks the line
    whose ts is exactly that value (the route for lines without an id); --run R --row N works only while the row has one line,
    and is refused (exit 2) when it has more: name one with --attempt (or --ts).
  - ingest writes a stop event to <run>/manifest.jsonl and the attempt's ledger line (id = the attempt ID) only for a notice
    that maps to an attempt (an open continuation by task-id, a start event by tool-use-id, or the ID in the description).
    A notice without an attempt ID writes no ledger line and exits 0; the same notice twice (prompt + sweep) is one event
    and one line. Model and effort come from meta.json, else the [model/effort] label, else the launch event.
  - The ingest gate (OD-3). ACTIVE is never read. With a session id the allowed set is its held runs (plus --run R): a
    notice whose ID names a run outside it gives none:not-held, and an ID-less notice is matched (continuation by task-id,
    start by tool-use-id) against the allowed runs; found in 2+ of them it gives none:ambiguous. Neither writes anything.
    With no session id (no --sid, --sid=, or WFP_SID empty) only the run of the notice's own ID is read, and only while no
    session holds it live (lease none or expired; a live lease gives none:not-held); an ID-less notice gives none:no-attempt.
  - retry_kind (continue | fresh) says how an attempt n>1 was made: continue = the same builder resumed (manifest.js launch --continue), fresh = a new
    agent. The notice ingest sets it from the launch event; add defaults an attempt n>1 to fresh (--retry-kind overrides); attempt 1 has none, and
    lines written before the field have none (reported as unlabelled). update --retry-kind continue|fresh sets it by hand; report first-pass splits by it.
  - cause (owner-tune | review-findings | failed | tooling) says why a row was retried. On the retried attempt's line it is cause (set by
    update / add / close with --first-pass n, and by manifest.js launch --continue or a fresh launch n>1 through update); on the retry's own
    line it is retry_cause (add of a retry line, which stays append-only, and the notice ingest from the launch event). Without --cause, update,
    add and a launch record unstated and print one warning line on stderr; close refuses a missing cause with --first-pass n or --outcome
    retry (see close). Lines from before the fields have neither. report first-pass
    adds, after each 'retry kind (<family>):' line, 'retry kind (<family>) by cause: owner-tune N (next-attempt tokens T), review-findings N,
    failed N, tooling N, unstated N, unlabelled N; not stated U of R retries (P%)' (not stated = unstated + unlabelled; --json: retry_causes).
  - A SendMessage continuation gets its own line with its notice's tokens as reported; that context includes the predecessor's, so the row's token sum counts the shared context twice (no delta is computed).
  - --first-pass y: this attempt passed its first review; n: it needed fixes or failed. show groups by role, then model/effort:
    first-pass rate over the attempts at that step (rates from fewer than ${SMALL} outcomes are marked *), and tok/accepted =
    all tokens of the rows (retries included) / rows with an accepted attempt (firstPass y), '-' when none. A row counts
    under its accepted attempt's model/effort; rows with no accepted attempt are listed as 'no outcome'.
  - Every added line gets id <run>#<row>.<attempt>. Effort 'unknown' (also tokens / tools / ms 'unknown' = null) stays out of the
    ranking and stats and is reported as 'unknown effort: N'. Codex rows (model codex or gpt-*, effort may be ultra) and council
    rows (model council or council-<N>seat, mode background-bash, effort unknown or the chair's) form their own section in show and get no auto escalation.
  - Rows with no Agent notice (PROTOCOL step 5.6): a council run is added by hand with --model council-<N>seat --mode background-bash; a Workflow
    row with --mode workflow; any other background shell job with --mode background-bash.
  - Filters are exact and case-insensitive. --repo given as a path keeps its last segment (~/my-app -> my-app).
  - roles: ${ROLES.join(', ')}. efforts: ${EFFORTS_ACCEPTED.join(', ')}. modes: ${MODES.join(', ')} (case and a (...) note ignored).
    models: haiku, sonnet, opus, fable, codex, council or council-<N>seat, gpt-* or a full claude-* id. Numbers may use , or _ separators; --tokens also takes
    88.3k / 1.2M; --ms is milliseconds or a duration such as 150s, 2.5m, "2m 30s", 1h2m.
  - Files: ${RUNS}/<run>/ledger.jsonl (the run folder is created on first add, and add says so).`;

// ---- validation ------------------------------------------------------------------
const RETRY_KINDS = ['continue', 'fresh'];
// P31: why a row was retried. A line's cause names why the attempt after it was made (it did not pass first time, or the owner tuned it).
// Missing on a retry or a first-pass n: recorded as 'unstated' with one warning line; lines from before the field read as unlabelled.
const CAUSES = ['owner-tune', 'review-findings', 'failed', 'tooling'], UNSTATED = 'unstated';
const OUTCOMES = ['accepted', 'retry', 'blocked', 'dropped'];
const causeWarn = what => console.error(`ledger: warning: ${what} without --cause; recorded as ${UNSTATED} (--cause ${CAUSES.join('|')})`);
const V = {
  run: v => { const raw = v; v = paths.runName(v); if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(v) || v.includes('..')) fail(`--run '${raw}': use the run folder name, or the path of a run folder under ${RUNS} (letters, digits, . _ -; no '..')`); return v; },
  row: v => { if (!/^\d{1,4}[a-z]?$/i.test(v)) fail(`--row '${v}': a plan row number such as 3 or 3b`); return /^\d+$/.test(v) ? +v : v.toLowerCase(); },
  role: v => { const s = v.toLowerCase(); if (!ROLES.includes(s)) fail(`--role '${v}': one of ${ROLES.join(', ')}`); return s; },
  agent: v => { if (!/^[A-Za-z0-9][A-Za-z0-9:_.-]{0,79}$/.test(v)) fail(`--agent '${v}': the agent type, e.g. orch-builder`); return paths.agentBase(v) || v; }, // 'boss:orch-builder' is stored as 'orch-builder'
  model: v => { const s = v.toLowerCase(); if (!/^(haiku|sonnet|opus|fable|codex|council(-\d{1,2}seat)?|gpt-[a-z0-9.-]+|claude-[a-z0-9.-]+(\[1m\])?)$/.test(s)) fail(`--model '${v}': haiku, sonnet, opus, fable, codex, council or council-<N>seat, gpt-* or a full claude-* id`); return s; },
  effort: v => { const s = v.toLowerCase(); if (!EFFORTS_ACCEPTED.includes(s)) fail(`--effort '${v}': one of ${EFFORTS_ACCEPTED.join(', ')}`); return s; },
  mode: v => { const s = String(v).toLowerCase().replace(/\([^)]*\)/g, ' ').trim().replace(/\s+/g, '-'); if (!MODES.includes(s)) fail(`--mode '${v}': one of ${MODES.join(', ')} (case and a (...) note are ignored; 'agent team' is agent-team)`); return s; },
  int: name => v => { const s = String(v).replace(/[,_]/g, ''); if (!/^\d{1,12}$/.test(s)) fail(`--${name} '${v}': a whole number >= 0${name === 'ms' ? ' (milliseconds)' : ''}`); return +s; },
  retryKind: v => { const s = String(v).toLowerCase(); if (!RETRY_KINDS.includes(s)) fail(`--retry-kind '${v}': one of ${RETRY_KINDS.join(', ')}`); return s; },
  cause: v => { const s = String(v).toLowerCase(); if (![...CAUSES, UNSTATED].includes(s)) fail(`--cause '${v}': one of ${CAUSES.join(', ')}`); return s; },
  outcome: v => { const s = String(v).toLowerCase(); if (!OUTCOMES.includes(s)) fail(`--outcome '${v}': one of ${OUTCOMES.join(', ')}`); return s; },
  yn: name => v => { const s = String(v).toLowerCase(); if (['y', 'yes', 'true'].includes(s)) return true; if (['n', 'no', 'false'].includes(s)) return false; return fail(`--${name} '${v}': y or n`); },
  text: name => v => { const s = String(v).trim(); if (!s || s.length > 80 || /[\r\n\t]/.test(s)) fail(`--${name} '${v}': 1-80 characters on one line`); return s; },
};
const repoV = v => { let s = V.text('repo')(v); if (s.includes('/')) s = path.basename(s.replace(/\/+$/, '')); if (!s) fail(`--repo '${v}': a repo name`); return s; };
const tokV = v => { // F-30: the converted value must be a safe integer (no 1e400k, no 99999999999999999999k)
  const s = String(v).replace(/[,_\s]/g, ''), m = s.match(/^(\d+(?:\.\d+)?)([kKmM])$/);
  const n = m ? Math.round(+m[1] * (/k/i.test(m[2]) ? 1e3 : 1e6)) : /^\d{1,12}$/.test(s) ? +s : NaN;
  if (!Number.isSafeInteger(n)) fail(`--tokens '${v}': a whole number, or 88.3k / 1.2M`);
  return n;
};
const orUnknown = parse => v => String(v).trim().toLowerCase() === 'unknown' ? null : parse(v); // tools / ms: 'unknown' stores null
const msV = v => { // completion notices may show ms or a duration: 150000 | 150s | 2.5m | "2m 30s" | 1h2m | 450ms
  const s = String(v).replace(/[,_\s]/g, '').toLowerCase(); if (/^\d{1,12}$/.test(s)) return +s;
  const m = s.match(/^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+)ms)?$/);
  if (!m || !s) return fail(`--ms '${v}': milliseconds, or a duration such as 150s, 2.5m, "2m 30s"`);
  return Math.round((+(m[1] || 0)) * 36e5 + (+(m[2] || 0)) * 6e4 + (+(m[3] || 0)) * 1e3 + (+(m[4] || 0)));
};
const stepRank = x => { if (isCodex(x)) return null; const m = MODELS.findIndex(n => String(x.model).includes(n)), e = EFFORTS.indexOf(x.effort); return m < 0 || e < 0 ? null : m * 10 + e; };
const FIELDS = { // flag: [record key, parser, required by add, settable by update]
  run: ['run', V.run, true, false], row: ['row', V.row, true, false], role: ['role', V.role, true, false], agent: ['agent', V.agent, true, false],
  model: ['model', V.model, true, false], effort: ['effort', V.effort, true, false], mode: ['mode', V.mode, true, false],
  tokens: ['tokens', orUnknown(tokV), true, true], tools: ['tools', orUnknown(V.int('tools')), true, true], ms: ['ms', orUnknown(msV), true, true],
  repo: ['repo', repoV, true, false], area: ['area', V.text('area'), true, false],
  'first-pass': ['firstPass', V.yn('first-pass'), false, true], retries: ['retries', V.int('retries'), false, true], escalated: ['escalated', V.yn('escalated'), false, true],
  'retry-kind': ['retry_kind', V.retryKind, false, true], cause: ['cause', V.cause, false, true],
};
const SETTABLE = Object.keys(FIELDS).filter(f => FIELDS[f][3]).map(f => '--' + f).join(' ');

function parseFlags(argv) {
  const o = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--') || a === '--') fail(`unexpected argument '${a}' (flags look like --name value; a stray word usually means an unquoted value that contains a space, or a flag missing its --)`);
    let name = a.slice(2), val;
    const eq = name.indexOf('=');
    if (eq >= 0) { val = name.slice(eq + 1); name = name.slice(0, eq); }
    else { val = argv[i + 1]; if (val === undefined || val.startsWith('--')) fail(`--${name} needs a value (${val === undefined ? 'nothing follows it' : `the next argument is the flag '${val}'`}); quote a value that starts with --`); i++; }
    if (Object.prototype.hasOwnProperty.call(o, name)) fail(`--${name} is given twice ('${o[name]}' and '${val}'); give it once`);
    o[name] = val;
  }
  return o;
}

// ---- storage ---------------------------------------------------------------------
const ledgerFile = run => path.join(RUNS, run, 'ledger.jsonl');
function withLock(file, fn) { // mkdir lock next to the file: add and update never interleave a read-modify-write
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const lock = file + '.lock';
  for (let i = 0; ; i++) {
    try { fs.mkdirSync(lock); break; } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      try { if (Date.now() - fs.statSync(lock).mtimeMs > 5000) { fs.rmdirSync(lock); continue; } } catch {}
      if (i >= 80) throw new Error(`ledger is locked (${lock}); retry, or remove that folder if no other ledger.js is running`);
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25);
    }
  }
  try { return fn(); } finally { try { fs.rmdirSync(lock); } catch {} }
}
function readRows(file) {
  let txt = ''; try { txt = fs.readFileSync(file, 'utf8'); } catch { return { rows: [], bad: 0 }; }
  const rows = []; let bad = 0;
  for (const l of txt.split('\n')) { if (!l.trim()) continue; try { const x = JSON.parse(l); if (x && x.run != null && x.row != null) rows.push(x); else bad++; } catch { bad++; } }
  return { rows, bad };
}

// ---- commands --------------------------------------------------------------------
function attemptV(v, flag) { // an attempt ID <run>#<row>.<n> (manifest.js parseAttemptId: the row lower-cased)
  const a = MF().parseAttemptId(v);
  if (!a) fail(`--${flag} '${v}': an attempt ID such as my-run#3.1 (<run>#<row>.<n>)`);
  V.run(a.run); return a;
}
const rowVal = r => /^\d+$/.test(String(r)) ? +r : String(r).toLowerCase(); // the ledger stores a numeric row as a number (V.row)
function idN(x, run, row) { // a line's attempt number from its explicit id suffix, or null
  const pre = `${run}#${row}.`;
  if (typeof x.id !== 'string' || !x.id.startsWith(pre)) return null;
  const t = x.id.slice(pre.length); return /^\d{1,3}$/.test(t) ? +t : null;
}
function add(o) {
  const att = o.attempt; o = Object.assign({}, o); delete o.attempt;
  for (const f of Object.keys(o)) if (!FIELDS[f]) fail(`add: unknown flag --${f} (flags: ${Object.keys(FIELDS).concat('attempt').map(x => '--' + x).join(' ')})`);
  const missing = Object.keys(FIELDS).filter(f => FIELDS[f][2] && o[f] === undefined);
  if (missing.length) fail(`add: missing ${missing.map(f => '--' + f).join(' ')}`);
  const r = { ts: new Date().toISOString() };
  for (const [f, [key, parse]] of Object.entries(FIELDS)) r[key] = o[f] !== undefined ? parse(o[f]) : key === 'retries' ? 0 : key === 'escalated' ? false : null;
  let want = null;
  if (att !== undefined) {
    want = attemptV(att, 'attempt');
    if (want.run !== r.run || want.row !== String(r.row).toLowerCase()) fail(`add: --attempt ${want.id} is not run ${r.run} row ${r.row}`);
  }
  if (r.retry_kind == null) delete r.retry_kind; // set below for an attempt n>1; attempt 1 has none
  const cause = r.cause; delete r.cause; // P31: on this line only with --first-pass n; on a retry it is the predecessor's (below)
  if (r.firstPass === false) r.cause = cause || UNSTATED;
  const file = ledgerFile(r.run), created = !fs.existsSync(path.dirname(file));
  let note = '', warn = r.firstPass === false && !cause ? 'add --first-pass n' : null;
  const n = withLock(file, () => {
    const all = readRows(file).rows;
    if (want && all.some(x => x.id === want.id)) fail(`add: a line with id ${want.id} already exists; record outcomes with: ledger.js close --attempt ${want.id} --first-pass y|n --outcome accepted|retry|blocked|dropped (--cause with n or retry)`);
    const prior = all.filter(x => x.run === r.run && String(x.row) === String(r.row)), prev = prior.length;
    if (cause && !prev && r.firstPass !== false) fail('add: --cause goes with --first-pass n, or with a retry line (then it says why this retry was made)');
    if (prev) { // a retry attempt: a new model/effort, or an explicit --retries >= 1; the same step again is most likely a duplicate
      const last = prior[prev - 1], same = last.model === r.model && last.effort === r.effort;
      if (o.retries !== undefined ? !(r.retries >= 1) : same) fail(`add: run ${r.run} row ${r.row} is already logged at ${last.model}/${last.effort} (${prev} attempt${prev > 1 ? 's' : ''}). Outcomes go through: ledger.js update --run ${r.run} --row ${r.row} --first-pass y|n (or --attempt <id> once the row has more than one line). A retry on the same model/effort needs --retries ${prev}.`);
      if (o.retries === undefined) r.retries = prev;
      const a = stepRank(last), b = stepRank(r);
      if (o.escalated === undefined) r.escalated = a != null && b != null && b > a;
      if (o['retry-kind'] === undefined) r.retry_kind = 'fresh'; // a continuation is only known from its launch event: the notice ingest sets 'continue'
      note = ` (retry ${r.retries}${r.escalated ? `, escalated ${last.model}/${last.effort} -> ${r.model}/${r.effort}` : ''})`;
      r.retry_cause = cause || UNSTATED; // add stays append-only (earlier lines are never edited): the retry line carries why it was made
      if (!cause) warn = warn || 'add of a retry';
    }
    // row 4's ordinal rule, made collision-safe: one past the highest attempt number in the row (explicit id suffix, else ordinal)
    const k = want ? want.n : 1 + prior.reduce((m, x, i) => Math.max(m, idN(x, r.run, String(r.row).toLowerCase()) ?? i + 1), 0);
    fs.appendFileSync(file, JSON.stringify(Object.assign({ ts: r.ts, id: want ? want.id : `${r.run}#${r.row}.${k}` }, r)) + '\n'); // stable line ID: run#row.attempt
    return k;
  });
  console.log(`ledger: added ${r.run} row ${r.row} attempt ${n}${note} -> ${file}${created ? ' (new run folder)' : ''}`);
  if (warn) causeWarn(warn);
}
// F-18: update edits exactly one line, chosen by --attempt <id>, by --run + --ts <iso>, or by --run + --row while the row has one line.
function update(o, extra = null) { // extra (close only): {set: fields stored on the line beside the FIELDS ones, verb}
  const att = o.attempt, tsSel = o.ts; o = Object.assign({}, o); delete o.attempt; delete o.ts;
  for (const f of Object.keys(o)) {
    if (!FIELDS[f]) fail(`update: unknown flag --${f} (flags: --attempt --ts --run --row ${SETTABLE})`);
    if (f !== 'run' && f !== 'row' && !FIELDS[f][3]) fail(`update: --${f} can't be changed (settable: ${SETTABLE}); a different model/effort is a new attempt: add with --retries`);
  }
  if (att !== undefined && tsSel !== undefined) fail('update: give --attempt <id> or --ts <iso>, not both');
  let run, row = null, want = null;
  if (att !== undefined) {
    want = attemptV(att, 'attempt'); run = want.run;
    if (o.run !== undefined && V.run(o.run) !== run) fail(`update: --run ${o.run} disagrees with --attempt ${want.id}`);
    if (o.row !== undefined && String(V.row(o.row)).toLowerCase() !== want.row) fail(`update: --row ${o.row} disagrees with --attempt ${want.id}`);
  } else {
    if (o.run === undefined || (o.row === undefined && tsSel === undefined)) fail('update: needs --attempt <id>, or --run with --ts <iso> or --row <n>');
    run = V.run(o.run);
    if (o.row !== undefined) row = V.row(o.row);
    if (tsSel !== undefined && !Number.isFinite(Date.parse(String(tsSel)))) fail(`--ts '${tsSel}': the line's exact ISO ts, e.g. 2026-10-20T12:00:00.000Z`);
  }
  const set = {};
  for (const [f, v] of Object.entries(o)) if (f !== 'run' && f !== 'row') set[FIELDS[f][0]] = FIELDS[f][1](v);
  if (!Object.keys(set).length) fail(`update: nothing to set; give one of ${SETTABLE}`);
  const file = ledgerFile(run);
  if (!fs.existsSync(file)) fail(`update: run ${run} has no ledger yet (${file}); add the row first`);
  const now = new Date().toISOString();
  let warn = false;
  const rec = withLock(file, () => {
    const lines = fs.readFileSync(file, 'utf8').split('\n'), hits = [];
    lines.forEach((l, i) => {
      if (!l.trim()) return; let x; try { x = JSON.parse(l); } catch { return; }
      if (!x || x.run == null || x.row == null) return;
      if (want ? x.id === want.id : x.run === run && (row === null || String(x.row) === String(row)) && (tsSel === undefined || x.ts === String(tsSel))) hits.push({ i, x });
    });
    if (want && !hits.length) fail(`no ledger line ${want.id} (notice not ingested yet? manifest.js show --run ${run})`);
    if (want && hits.length > 1) fail(`update: ${hits.length} lines carry id ${want.id}; fix the ledger by hand (${file})`);
    if (tsSel !== undefined && hits.length !== 1) fail(`update: run ${run} has ${hits.length} lines${row !== null ? ` in row ${row}` : ''} with ts ${tsSel}; exactly one must match`);
    if (!want && tsSel === undefined) {
      if (!hits.length) fail(`update: run ${run} has no row ${row}; add it first`);
      if (hits.length > 1) fail(`row ${row} has ${hits.length} attempts: ${hits.map(h => h.x.id || `ts ${h.x.ts}`).join(', ')}; name one with --attempt <id> (or --ts <iso> for a line without an id)`);
    }
    const { i, x } = hits[0];
    if (set.firstPass === false && set.cause === undefined && x.cause == null) { set.cause = UNSTATED; warn = true; } // P31: never a silent n
    const out = Object.assign(x, set, extra ? extra.set : {}, { updated: now });
    lines[i] = JSON.stringify(out);
    const tmp = `${file}.${process.pid}.tmp`; fs.writeFileSync(tmp, lines.join('\n')); fs.renameSync(tmp, file);
    return out;
  });
  const what = want ? want.id : tsSel !== undefined ? `${run} line ts ${tsSel}` : `${run} row ${row}`;
  const shown = Object.assign({}, set, extra ? extra.set : {});
  console.log(`ledger: ${extra ? 'closed' : 'updated'} ${what}: ${Object.entries(shown).map(([k, v]) => `${k}=${v}`).join(' ')}`);
  if (warn) causeWarn(`${extra ? extra.verb : 'update'} --first-pass n`);
  if (want && fs.existsSync(path.join(RUNS, run, 'manifest.jsonl'))) { // design §8: the outcome event, only when the run has a manifest
    try {
      MF().appendEvent(run, Object.assign({ type: 'outcome', attempt: want.id, row: want.row, key: `${want.id}:${now}`, ts: now, via: extra ? extra.verb : 'ledger', session: process.env.WFP_SID || null,
        first_pass: rec.firstPass ?? null, retries: rec.retries ?? null, escalated: rec.escalated ?? null, set: Object.keys(shown) },
        rec.cause != null ? { cause: rec.cause } : {}, extra && rec.outcome != null ? { outcome: rec.outcome } : {}));
    } catch (e) { console.error(`ledger: note: outcome event not written (${e && e.message})`); }
  }
}
// P31 row close: first-pass, cause and the row's outcome in one call (one line edit and one outcome event).
// --cause is required when --first-pass is n or --outcome is retry; it is refused with --first-pass y unless it is owner-tune.
function close(o) {
  const allowed = ['attempt', 'first-pass', 'outcome', 'cause', 'retries', 'escalated'];
  for (const f of Object.keys(o)) if (!allowed.includes(f)) fail(`close: unknown flag --${f} (flags: ${allowed.map(x => '--' + x).join(' ')})`);
  const miss = ['attempt', 'first-pass', 'outcome'].filter(f => o[f] === undefined);
  if (miss.length) fail(`close: missing ${miss.map(f => '--' + f).join(' ')} (close --attempt <id> --first-pass y|n --outcome ${OUTCOMES.join('|')} [--cause ${CAUSES.join('|')}])`);
  const fp = V.yn('first-pass')(o['first-pass']), outcome = V.outcome(o.outcome), cause = o.cause !== undefined ? V.cause(o.cause) : null;
  if (cause === UNSTATED) fail(`close: --cause '${o.cause}': one of ${CAUSES.join(', ')}`);
  if (!cause && (fp === false || outcome === 'retry')) fail(`close: --cause ${CAUSES.join('|')} is required with ${fp === false ? '--first-pass n' : '--outcome retry'}`);
  if (cause && fp === true && cause !== 'owner-tune') fail(`close: --cause ${cause} with --first-pass y; a passed attempt is retried only as owner-tune`);
  const u = { attempt: o.attempt, 'first-pass': o['first-pass'] };
  if (cause) u.cause = cause;
  for (const f of ['retries', 'escalated']) if (o[f] !== undefined) u[f] = o[f];
  update(u, { verb: 'close', set: { outcome, closed: new Date().toISOString() } });
}
const fmtTok = n => n == null ? '-' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
const fmtMs = n => n == null ? '-' : n >= 36e5 ? (n / 36e5).toFixed(1) + 'h' : n >= 6e4 ? (n / 6e4).toFixed(1) + 'm' : Math.round(n / 1e3) + 's';
const yn = b => b === true ? 'y' : b === false ? 'n' : '-';
const cut = (s, w) => { s = String(s); return s.length > w ? s.slice(0, w - 1) + '~' : s; };
function table(head, rows, max) {
  const w = head.map((h, i) => Math.min(max[i] || 99, Math.max(h.length, ...rows.map(r => String(r[i]).length))));
  const line = r => r.map((c, i) => (i === r.length - 1 ? cut(c, w[i]) : cut(c, w[i]).padEnd(w[i]))).join('  ');
  return [line(head), ...rows.map(line)].join('\n');
}
const avgOf = v => v.length ? v.reduce((s, x) => s + x, 0) / v.length : null;
const sumOf = v => v.reduce((s, x) => s + (x == null ? 0 : x), 0);
// Stats for one set of attempts: by role, then model/effort. A row (run#row) counts under its ACCEPTED attempt's
// model/effort (an attempt with firstPass true) with every attempt's tokens folded in, so no token is counted twice.
// tok/accepted = all tokens of the role's rows (retries included) / rows that have an accepted attempt; '-' when none.
function statBlock(atts, sortGroups) {
  const byRow = new Map();
  for (const x of atts) { const k = `${x.run}#${x.row}`; if (!byRow.has(k)) byRow.set(k, []); byRow.get(k).push(x); }
  const byRole = new Map();
  for (const list of byRow.values()) { const r = list[0].role || 'default'; if (!byRole.has(r)) byRole.set(r, []); byRole.get(r).push(list); }
  let anecdotal = false;
  for (const role of [...byRole.keys()].sort((a, b) => (ROLES.indexOf(a) + 1 || 99) - (ROLES.indexOf(b) + 1 || 99))) {
    const rowsOfRole = byRole.get(role), g = new Map(), none = { rows: 0, tok: 0 };
    let accepted = 0, tokAll = 0, nAtt = 0;
    const grp = x => { const key = `${x.model}/${x.effort}`; if (!g.has(key)) g.set(key, { key, model: x.model, effort: x.effort, att: [], rows: 0, tok: 0 }); return g.get(key); };
    for (const list of rowsOfRole) {
      const tok = sumOf(list.map(x => x.tokens)); tokAll += tok; nAtt += list.length;
      for (const x of list) grp(x).att.push(x);
      const acc = [...list].reverse().find(x => x.firstPass === true);
      if (acc) { accepted++; const a = grp(acc); a.rows++; a.tok += tok; } else { none.rows++; none.tok += tok; }
    }
    console.log(`\nrole ${role}: ${rowsOfRole.length} rows, ${nAtt} attempts, tokens ${fmtTok(tokAll)}, accepted ${accepted}, tok/accepted ${accepted ? fmtTok(Math.round(tokAll / accepted)) : '-'}`);
    const groups = sortGroups([...g.values()]);
    const body = groups.map(a => {
      const out = a.att.filter(x => x.firstPass === true || x.firstPass === false), y = out.filter(x => x.firstPass).length;
      if (out.length && out.length < SMALL) anecdotal = true;
      return [a.key, a.rows, fmtTok(a.rows ? a.tok : null), a.rows ? fmtTok(Math.round(a.tok / a.rows)) : '-', out.length ? `${y}/${out.length} ${Math.round(100 * y / out.length)}%${out.length < SMALL ? '*' : ''}` : 'no outcomes',
        a.att.some(x => x.tools != null) ? Math.round(avgOf(a.att.filter(x => x.tools != null).map(x => x.tools))) : '-', fmtMs(avgOf(a.att.filter(x => x.ms != null).map(x => x.ms)))];
    });
    if (none.rows) body.push(['no outcome', none.rows, fmtTok(none.tok), '-', '-', '-', '-']);
    console.log(table(['model/effort', 'rows', 'tokens', 'tok/accepted', 'first-pass', 'avg tools', 'avg time'], body, [20, 5, 8, 12, 14, 9, 8]));
  }
  if (anecdotal) console.log(`* fewer than ${SMALL} outcomes: anecdotal; cite it as such, don't generalize from it`);
}
function show(o) {
  for (const f of Object.keys(o)) if (!['run', 'repo', 'area', 'role', 'last'].includes(f)) fail(`show: unknown flag --${f} (filters: --run --repo --area --role --last)`);
  const last = o.last === undefined ? 20 : V.int('last')(o.last); if (last < 1) fail(`--last '${o.last}': 1 or more`);
  const run = o.run !== undefined ? V.run(o.run) : null, repo = o.repo !== undefined ? repoV(o.repo).toLowerCase() : null, area = o.area !== undefined ? V.text('area')(o.area).toLowerCase() : null, role = o.role !== undefined ? V.role(o.role) : null;
  let files = [];
  if (run) files = [ledgerFile(run)]; else { try { files = fs.readdirSync(RUNS).map(d => path.join(RUNS, d, 'ledger.jsonl')); } catch {} }
  let all = [], bad = 0;
  for (const f of files) { const r = readRows(f); all = all.concat(r.rows); bad += r.bad; }
  if (bad) console.error(`ledger: skipped ${bad} unreadable line(s)`);
  const filt = [run && `run=${run}`, repo && `repo=${repo}`, area && `area=${area}`, role && `role=${role}`].filter(Boolean).join(' ') || 'all runs';
  const rows = all.filter(x => (!repo || String(x.repo || '').toLowerCase() === repo) && (!area || String(x.area || '').toLowerCase() === area) && (!role || x.role === role))
    .sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  if (!rows.length) return console.log(`ledger: no attempts match (${filt}) under ${RUNS}`);
  const shown = rows.slice(-last); // --last limits the listed rows only; the stats below read every matching row (A-07)
  console.log(`ledger: ${shown.length} of ${rows.length} attempts (${filt}; newest last)`);
  console.log(table(['run', 'row', 'role', 'agent', 'model/effort', 'mode', 'tokens', 'tools', 'time', '1st', 'retry', 'esc', 'repo/area'],
    shown.map(x => [x.run, x.row, x.role, x.agent, `${x.model}/${x.effort}`, x.mode, fmtTok(x.tokens), x.tools ?? '-', fmtMs(x.ms), yn(x.firstPass), x.retries ?? 0, yn(x.escalated), `${x.repo}/${x.area}`]),
    [28, 5, 14, 18, 18, 10, 7, 5, 6, 3, 5, 3, 32]));
  const known = rows.filter(x => x.effort !== 'unknown'), unknown = rows.length - known.length;
  const claude = known.filter(x => !isCodex(x)), codex = known.filter(isCodex);
  console.log(`\nstats: n=${rows.length} attempts over every row matching (${filt}); --last only limits the list above. first-pass = passed its first review; a row counts under its accepted attempt's model/effort, retries' tokens folded in.`);
  if (unknown) console.log(`unknown effort: ${unknown} (left out of the stats and the ranking)`);
  const rank = a => { const m = MODELS.findIndex(x => String(a.model).includes(x)); return (m < 0 ? -1 : m) * 10 + EFFORTS.indexOf(a.effort); };
  statBlock(claude, gs => gs.sort((a, b) => rank(b) - rank(a)));
  if (codex.length) {
    console.log(`\n== codex (own section: never ranked against Claude, no auto escalation; also council rows) ==`);
    statBlock(codex, gs => gs.sort((a, b) => a.key.localeCompare(b.key)));
  }
}

// ---- notice ingest (design §4) ------------------------------------------------------
// parseNotices(text) -> [{task_id, tool_use_id, output_file, status, summary, description, phase, final, tokens, tools, ms, result_bytes}
// | {skip: <why>, task_id}]. <result> is worker text, so header tags come from before <result> and <usage> from after </result>.
const SUMMARY_RE = /^Agent "([\s\S]*)" (finished|is waiting on its own background work|failed: [\s\S]*|was stopped by (?:Claude|user)|stopped at its \d+-turn limit[\s\S]*)$/;
// A turn-limit stop ("stopped at its 80-turn limit (partial result; ...)", status completed) is a final stop with a partial result:
// phase stopped-turn-limit, status turn-limit (so the progress item reads waiting_input, not done and not failed). `launch --continue` accepts it.
const FINAL_PHASES = ['finished', 'failed', 'killed', 'stopped-turn-limit'];
const tagRe = t => new RegExp(`<${t}>([\\s\\S]*?)</${t}>`, 'g');
const firstTag = (s, t) => { const m = tagRe(t).exec(s); return m ? m[1].trim() : null; };
const lastTag = (s, t) => { const re = tagRe(t); let m, v = null; while ((m = re.exec(s))) v = m[1]; return v; };
const numOrNull = v => { const s = String(v == null ? '' : v).trim(); return /^\d{1,15}$/.test(s) ? +s : null; };
function parseNotices(text) {
  text = String(text == null ? '' : text);
  const OPEN = '<task-notification>', starts = [];
  for (let i = text.indexOf(OPEN); i >= 0; i = text.indexOf(OPEN, i + 1)) if (text.slice(i + OPEN.length, i + OPEN.length + 200).includes('<task-id>')) starts.push(i);
  return starts.map((st, k) => {
    const block = text.slice(st, k + 1 < starts.length ? starts[k + 1] : text.length);
    const ri = block.indexOf('<result>'), re = block.lastIndexOf('</result>');
    const head = ri >= 0 ? block.slice(0, ri) : block, tail = ri >= 0 ? (re > ri ? block.slice(re + 9) : '') : block;
    const body = ri >= 0 ? block.slice(ri + 8, re > ri ? re : block.length) : null;
    const n = { task_id: firstTag(head, 'task-id'), tool_use_id: firstTag(head, 'tool-use-id'), output_file: firstTag(head, 'output-file'),
      status: firstTag(head, 'status'), summary: firstTag(head, 'summary') };
    if (!n.task_id) return { skip: 'no task-id' };
    const m = SUMMARY_RE.exec(n.summary || '');
    if (!m) return { skip: 'not an agent notice', task_id: n.task_id }; // background command, monitor, dynamic workflow
    n.description = m[1];
    n.phase = m[2] === 'finished' ? 'finished' : m[2].startsWith('is waiting') ? 'waiting' : m[2].startsWith('failed') ? 'failed' : m[2].startsWith('stopped at its') ? 'stopped-turn-limit' : 'killed';
    if (n.phase === 'stopped-turn-limit') n.status = 'turn-limit';
    n.final = FINAL_PHASES.includes(n.phase);
    const u = lastTag(tail, 'usage') || '';
    n.tokens = numOrNull(firstTag(u, 'subagent_tokens')); n.tools = numOrNull(firstTag(u, 'tool_uses')); n.ms = numOrNull(firstTag(u, 'duration_ms'));
    n.result_bytes = body == null ? null : Buffer.byteLength(body);
    return n;
  });
}
const SAFE_ID = s => typeof s === 'string' && /^[A-Za-z0-9._-]{1,128}$/.test(s) && !s.includes('..');
const readJson = f => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return null; } };
const isFile = f => { try { return fs.statSync(f).isFile(); } catch { return false; } };
// meta.json for a notice: --meta, then the start's (manifest metaFor), then <dirname(transcript)>/<session>/subagents/, then
// $ORCH_PROJECTS_DIR/*/<launch session>/subagents/. -> {meta, dir} (dir: the subagents folder, for agent-<task>.jsonl), or {}
function findMeta(n, opts, start, launch) {
  if (opts.meta) { const m = readJson(opts.meta); return m ? { meta: m, dir: path.dirname(opts.meta) } : {}; }
  const M = MF();
  if (start) { const m = M.metaFor(start); if (m) return { meta: m, dir: path.dirname(m._file) }; }
  if (!SAFE_ID(n.task_id)) return {};
  const name = `agent-${n.task_id}.meta.json`, dirs = [];
  if (opts.transcriptPath && SAFE_ID(opts.sessionId)) dirs.push(path.join(path.dirname(opts.transcriptPath), opts.sessionId, 'subagents'));
  if (launch && SAFE_ID(launch.session)) { let ps = []; try { ps = fs.readdirSync(M.PROJECTS).sort(); } catch {} for (const d of ps) dirs.push(path.join(M.PROJECTS, d, launch.session, 'subagents')); }
  for (const d of dirs) { const m = readJson(path.join(d, name)); if (m && (!m.toolUseId || !n.tool_use_id || m.toolUseId === n.tool_use_id)) return { meta: m, dir: d }; }
  return dirs.length && opts.transcriptPath && SAFE_ID(opts.sessionId) ? { dir: dirs[0] } : {};
}
// The subagent transcript: model_id from the `model` attachment record (line 4, about 14 KB in: .attachment.identity.modelId), else the first
// assistant record's message.model; effort_observed from the first assistant record. The head is read in 256 KB chunks up to HEAD_MAX (the first
// assistant record sits past 330 KB in 2026-11 transcripts; P29) and stops once both are known. For a killed or failed notice without usage,
// tokens = max(input + cache_creation + cache_read + output), tools = distinct tool_use ids (whole file; skipped over 20 MB).
const HEAD_CHUNK = 262144, HEAD_MAX = 1048576;
const modelOk = v => typeof v === 'string' && v && !/^</.test(v); // '<synthetic>' is not a model
function transcriptFacts(f, wantNumbers, maxHead = HEAD_MAX) {
  const out = { effort_observed: null, model_id: null, tokens: null, tools: null };
  let size; try { size = fs.statSync(f).size; } catch { return out; }
  let att = null, msgModel = null, max = null; const tu = new Set();
  const scan = l => { // -> true when the head scan may stop
    if (att == null && l.includes('"attachment"') && l.includes('"model"')) {
      let x; try { x = JSON.parse(l); } catch { x = null; }
      const a = x && x.type === 'attachment' && x.attachment;
      if (a && a.type === 'model' && a.identity && modelOk(a.identity.modelId)) att = a.identity.modelId;
    }
    if (!l.includes('"assistant"')) return false;
    let x; try { x = JSON.parse(l); } catch { return false; }
    if (!x || x.type !== 'assistant') return false;
    const msg = x.message || {};
    if (msgModel == null && modelOk(msg.model)) msgModel = String(msg.model);
    if (out.effort_observed == null && x.effort != null) out.effort_observed = String(x.effort);
    if (wantNumbers) {
      const u = msg.usage || {}, t = ['input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens'].reduce((s, k) => s + (Number.isFinite(u[k]) ? u[k] : 0), 0);
      if (msg.usage && (max == null || t > max)) max = t;
      for (const c of Array.isArray(msg.content) ? msg.content : []) if (c && c.type === 'tool_use' && c.id) tu.add(c.id);
      return false;
    }
    return att != null || msgModel != null; // the first assistant record is read: effort is its field or absent (older transcripts)
  };
  try {
    if (wantNumbers && size <= 20 * 1024 * 1024) { for (const l of fs.readFileSync(f, 'utf8').split('\n')) scan(l); }
    else {
      wantNumbers = false;
      const fd = fs.openSync(f, 'r'), cap = Math.min(size, maxHead);
      try {
        let pos = 0, carry = Buffer.alloc(0);
        while (pos < cap) {
          const buf = Buffer.alloc(Math.min(HEAD_CHUNK, cap - pos)), got = fs.readSync(fd, buf, 0, buf.length, pos);
          if (got <= 0) break;
          pos += got;
          const all = Buffer.concat([carry, buf.subarray(0, got)]), nl = all.lastIndexOf(10);
          const whole = nl >= 0 ? all.subarray(0, nl) : (pos >= cap ? all : Buffer.alloc(0));
          carry = nl >= 0 ? all.subarray(nl + 1) : (pos >= cap ? Buffer.alloc(0) : all);
          let done = false;
          for (const l of whole.toString('utf8').split('\n')) if (scan(l)) { done = true; break; }
          if (done) break;
        }
      } finally { fs.closeSync(fd); }
    }
  } catch { return out; }
  out.model_id = att || msgModel; // null when neither record is there: never guessed from the alias
  if (wantNumbers) { out.tokens = max; out.tools = tu.size; }
  return out;
}
function subagentFacts(dir, taskId, wantNumbers) {
  if (!dir || !SAFE_ID(taskId)) return { effort_observed: null, model_id: null, tokens: null, tools: null };
  return transcriptFacts(path.join(dir, `agent-${taskId}.jsonl`), wantNumbers);
}
function pick(cands, parse) { // first [value, from] whose value parses; -> [value, from] or [null, null]
  for (const [v, from] of cands) { if (v == null || v === '') continue; try { return [parse(String(v)), from]; } catch {} }
  return [null, null];
}
const bySeq = (a, b) => (a.seq || 0) - (b.seq || 0);
const lastOf = a => a.length ? a[a.length - 1] : null;
// A `waiting` notice with status completed (the agent's turn ended, it still owns background work) is the final stop until a later true final one
// arrives: it writes the ledger line, and the finished notice then updates that line (ensureLedgerLine's `updated`), never a second one.
const isNoticeFinal = e => e.type === 'stop' && e.phase !== 'agent-stopped' && (e.final === true || (e.phase === 'waiting' && e.status === 'completed'));
// One notice -> its output line. opts: {via, ts (the source record's timestamp; sweep), usage (queued_command JSON),
// transcriptPath, sessionId, meta, held}. The prompt / CLI path has no ts: the notice is always newer than any launch (rule 1).
// held (OD-3 gate): an array of run names is the allowed set (an ID outside it: none:not-held; an ID-less notice is matched
// against it, and a match in 2+ runs is none:ambiguous); undefined is no session id (B1): the ID's own run only, and only
// while no session holds that run live (lease none or expired; else none:not-held); an ID-less notice finds nothing.
// liveHeld(run): any session holds run's lease live (per-run file, else the legacy file for that run). With no session id
// every live holder counts as another session's. lease-store never throws; a run name it rejects reads as no lease. With
// neither leases/<run>.json nor ACTIVE.lease present there is no lease to read, so lease-store is not loaded (lazy, as above).
function liveHeld(run) {
  if (!SAFE_ID(run)) return false;
  if (!fs.existsSync(path.join(paths.leasesDir(), `${run}.json`)) && !fs.existsSync(path.join(ORCH_DIR, 'ACTIVE.lease'))) return false;
  const LS = LSM(), r = LS.readRunLease(ORCH_DIR, run); return !!(r && r.lease && LS.isLive(r.lease));
}
function ingestNotice(n, opts) {
  if (n.skip) return `-: skip (${n.skip})`;
  const M = MF();
  const u = opts.usage && typeof opts.usage === 'object' ? opts.usage : null; // prefer the JSON usage (queued_command)
  const nt = u && Number.isFinite(u.totalTokens) ? u.totalTokens : n.tokens, nk = u && Number.isFinite(u.toolUses) ? u.toolUses : n.tools, nm = u && Number.isFinite(u.durationMs) ? u.durationMs : n.ms;
  const desc = M.parseDescription(n.description), descA = desc.attempt ? M.parseAttemptId(desc.attempt) : null;
  const gated = Array.isArray(opts.held), held = gated ? opts.held.filter(r => typeof r === 'string') : null;
  if (gated && descA && !held.includes(descA.run)) return `${descA.id}: stop none, ledger none:not-held`;
  if (!gated && descA && liveHeld(descA.run)) return `${descA.id}: stop none, ledger none:not-held`; // B1: no sid, a live holder
  const cands = descA ? [descA.run] : gated ? held : []; // ACTIVE is never a candidate
  const runs = [...new Set(cands)].filter(r => SAFE_ID(r) && fs.existsSync(path.join(RUNS, r)));
  const evOf = new Map(runs.map(r => [r, M.readEvents(r).events]));
  const newer = L => !opts.ts || !Number.isFinite(Date.parse(opts.ts)) || Date.parse(opts.ts) > Date.parse(L.ts);
  const AMBIG = `-: stop none, ledger none:ambiguous`;
  let attempt = null, run = null, start = null, unplanned = null;
  const r1 = []; // rule 1 (G4): an open continuation launched for this task-id, older than the notice
  for (const r of runs) {
    const evs = evOf.get(r), fin = new Set(evs.filter(isNoticeFinal).map(e => e.attempt));
    const L = evs.filter(e => e.type === 'launch' && e.continues && e.attempt && e.task_id === n.task_id && !fin.has(e.attempt) && newer(e)).sort(bySeq);
    if (L.length) r1.push({ run: r, attempt: lastOf(L).attempt });
  }
  if (r1.length > 1) return AMBIG; // only an ID-less notice has 2+ candidate runs
  if (r1.length) { attempt = r1[0].attempt; run = r1[0].run; }
  if (!attempt && n.tool_use_id) { // rule 2: a start with this tool-use-id
    const r2 = [];
    for (const r of runs) { const s = lastOf(evOf.get(r).filter(e => e.type === 'start' && e.tool_use_id === n.tool_use_id).sort(bySeq)); if (s) r2.push({ run: r, start: s }); }
    if (r2.length > 1) return AMBIG;
    if (r2.length && r2[0].start.attempt) { attempt = r2[0].start.attempt; run = r2[0].run; start = r2[0].start; }
    else if (r2.length) unplanned = r2[0];
  }
  if (!attempt && !unplanned && descA) { attempt = descA.id; run = descA.run; } // rule 3: the ID in the description
  if (!attempt && !unplanned) return `-: stop none, ledger none:no-attempt`;
  if (!attempt) run = unplanned.run, start = unplanned.start;
  const evs = evOf.get(run) || [], mine = attempt ? evs.filter(e => e.attempt === attempt).sort(bySeq) : [];
  const launch = lastOf(mine.filter(e => e.type === 'launch'));
  if (attempt && !start) start = lastOf(mine.filter(e => e.type === 'start' && e.tool_use_id === n.tool_use_id)) || lastOf(mine.filter(e => e.type === 'start'));
  const { meta, dir } = findMeta(n, opts, start, launch);
  const fill = !n.final || n.phase === 'finished' ? false : nt == null && nk == null; // killed / failed without usage
  const sf = subagentFacts(dir, n.task_id, fill);
  const [model, model_from] = pick([[meta && meta.model, 'meta'], [desc.model, 'label'], [launch && launch.model, 'launch']], V.model);
  const [effort0, effort_from] = pick([[meta && meta.effort, 'meta'], [desc.effort, 'label'], [launch && launch.effort, 'launch']], V.effort);
  const [agent] = pick([[meta && meta.agentType, 'meta'], [start && start.agent, 'start'], [launch && launch.agent, 'launch']], V.agent);
  let tokens = nt, tools = nk, numbers_from = nt != null || nk != null || nm != null ? 'notice' : null;
  if (fill && (sf.tokens != null || sf.tools != null)) { tokens = sf.tokens; tools = sf.tools; numbers_from = 'transcript'; }
  const row = attempt ? M.parseAttemptId(attempt).row : start && start.row != null ? start.row : null;
  const ev = { type: 'stop', attempt, row, key: `${n.task_id}:${n.phase}:${nk == null ? '-' : nk}:${nm == null ? '-' : nm}`, via: opts.via || 'cli',
    session: SAFE_ID(opts.sessionId) ? opts.sessionId : null, task_id: n.task_id, tool_use_id: n.tool_use_id || null, status: n.status || null,
    phase: n.phase, final: n.final, tokens, tools, ms: nm, numbers_from, agent: agent || null, model, effort: effort0 || (attempt ? 'unknown' : null),
    model_from, effort_from: effort0 ? effort_from : attempt ? 'default' : null, model_id: sf.model_id, effort_observed: sf.effort_observed,
    result_bytes: n.result_bytes, output_file: n.output_file || null, renotice: evs.some(e => e.type === 'stop' && e.task_id === n.task_id && e.phase !== 'agent-stopped') };
  if (opts.ts && Number.isFinite(Date.parse(opts.ts))) ev.ts = new Date(Date.parse(opts.ts)).toISOString();
  const r = M.appendEvent(run, ev); // a missing run folder is logged to errors.log and skipped
  if (r.skipped) return `${attempt || '-'}: stop skipped:${r.reason}, ledger none:${r.reason}`;
  const owner = r.dup ? r.event.attempt : attempt; // a dup belongs to whichever attempt first recorded it
  return `${owner || '-'}: stop ${r.dup ? 'dup' : 'new'}, ledger ${owner ? ensureLedgerLine(owner) : 'none:unplanned'}`;
}
// ensureLedgerLine(attempt) -> written | updated | same | none:<why>. Runs after every stop append (dup included), so a
// repeated sweep heals a missed write. Numbers come from the attempt's final notice stops (design §4).
function ensureLedgerLine(attempt) {
  const M = MF(), a = M.parseAttemptId(attempt);
  if (!a) return 'none:bad-id';
  const evs = M.readEvents(a.run).events.filter(e => e.attempt === a.id).sort(bySeq);
  const L = lastOf(evs.filter(e => e.type === 'launch'));
  if (!L) return 'none:no-launch';
  // A turn-limit stop is a partial result: it feeds the line only while no real final notice (finished / failed / killed / waiting) exists. A real
  // notice always wins, whenever either one was ingested (a sweep or renotice can ingest the older turn-limit notice last), so a line written from a
  // real notice is never overwritten with turn-limit numbers, and a line first written from a turn-limit notice is healed by the real one.
  const SF = evs.filter(isNoticeFinal), SR = SF.filter(e => e.phase !== 'stopped-turn-limit'), S = SR.length ? SR : SF;
  if (!S.length) return 'none:no-final-stop';
  const tokens = [...S].reverse().map(s => s.tokens).find(v => v != null) ?? null; // a context size: the latest, never summed
  const seg = k => { // a drop in tools or ms starts a new segment (a resume); within a segment the latest replaces, across they add
    let total = null, cur = null, prev = null;
    for (const s of S) {
      const drop = prev && ((s.tools != null && prev.tools != null && s.tools < prev.tools) || (s.ms != null && prev.ms != null && s.ms < prev.ms));
      if (drop) { if (cur != null) total = (total || 0) + cur; cur = null; }
      if (s[k] != null) cur = s[k];
      prev = { tools: s.tools != null ? s.tools : prev && prev.tools, ms: s.ms != null ? s.ms : prev && prev.ms };
    }
    return cur != null ? (total || 0) + cur : total;
  };
  const tools = seg('tools'), ms = seg('ms');
  const model_id = [...S].reverse().map(s => s.model_id).find(v => typeof v === 'string' && v) // resolved model from the notice's subagent transcript,
    || [...evs].reverse().filter(e => e.type === 'stop').map(s => s.model_id).find(v => typeof v === 'string' && v) || null; // else the SubagentStop's; null when unknown
  const [model] = pick([...S].reverse().map(s => [s.model, 'stop']).concat([[L.model, 'launch']]), V.model);
  if (!model) return 'none:no-model';
  const [effort] = pick([...S].reverse().filter(s => s.effort !== 'unknown').map(s => [s.effort, 'stop']).concat([[L.effort, 'launch']]), V.effort);
  const st = lastOf(evs.filter(e => e.type === 'start'));
  const [agent] = pick([...S].reverse().map(s => [s.agent, 'stop']).concat([[st && st.agent, 'start'], [L.agent, 'launch']]), V.agent);
  const prevL = a.n > 1 ? lastOf(M.readEvents(a.run).events.filter(e => e.type === 'launch' && e.attempt === `${a.run}#${a.row}.${a.n - 1}`).sort(bySeq)) : null;
  const rec = { ts: S[0].ts, id: a.id, run: a.run, row: rowVal(a.row), role: L.role || 'default', agent: agent || 'unknown', model, effort: effort || 'unknown',
    mode: L.mode || null, tokens, tools, ms, repo: L.repo || null, area: L.area || null, firstPass: null, retries: a.n - 1, escalated: false };
  if (model_id) rec.model_id = model_id; // only when known: lines without it keep the classic field set (sync reads a missing key as NULL)
  if (a.n > 1) rec.retry_kind = L.continues ? 'continue' : 'fresh'; // the launch event's `continues` (launch --continue); attempt 1 has none
  if (a.n > 1 && typeof L.cause === 'string' && L.cause) rec.retry_cause = L.cause; // P31: the launch's --cause (or unstated); older launches have none
  const ra = prevL ? stepRank(prevL) : null, rb = stepRank(rec); rec.escalated = ra != null && rb != null && rb > ra;
  const file = ledgerFile(a.run);
  return withLock(file, () => {
    let txt = ''; try { txt = fs.readFileSync(file, 'utf8'); } catch {}
    const lines = txt.split('\n');
    let idx = -1;
    lines.forEach((l, i) => { if (idx < 0 && l.trim()) { try { if (JSON.parse(l).id === a.id) idx = i; } catch {} } });
    if (idx < 0) { fs.appendFileSync(file, (txt && !txt.endsWith('\n') ? '\n' : '') + JSON.stringify(rec) + '\n'); return 'written'; }
    const x = JSON.parse(lines[idx]);
    const fillKind = x.retry_kind == null && rec.retry_kind != null; // heal a line written before the field (or by add) from the launch event; never override a set value
    const fillMid = x.model_id == null && rec.model_id != null; // same for the resolved model ID
    const fillCause = x.retry_cause == null && rec.retry_cause != null; // and the launch's retry cause
    if (x.tokens === rec.tokens && x.tools === rec.tools && x.ms === rec.ms && !fillKind && !fillMid && !fillCause) return 'same';
    Object.assign(x, { tokens: rec.tokens, tools: rec.tools, ms: rec.ms, updated: new Date().toISOString() }, fillKind ? { retry_kind: rec.retry_kind } : {}, fillMid ? { model_id: rec.model_id } : {},
      fillCause ? { retry_cause: rec.retry_cause } : {});
    lines[idx] = JSON.stringify(x);
    const tmp = `${file}.${process.pid}.tmp`; fs.writeFileSync(tmp, lines.join('\n')); fs.renameSync(tmp, file);
    return 'updated';
  });
}
// ingestText(text, opts) -> output lines, one per notice block. Never throws for one bad notice: the line says error.
function ingestText(text, opts = {}) {
  return parseNotices(text).map(n => { try { return ingestNotice(n, opts); } catch (e) { return `${n.task_id || '-'}: error ${e && e.message}`; } });
}
// The sweep: the enqueue (queue-operation) and queued_command records of a main transcript. tailBytes: N, or 'all'.
// opts.stateFile (the hooks pass one): remember where the last sweep stopped, so the next one reads only the bytes added since. The state is
// {file, ino, offset, held}: offset is a line start (just past the last newline read), so no line is ever split across two sweeps (a trailing record without its newline is read again; the offset also stops at the first notice whose ingest threw or hit a busy lock, so it is retried); `held` is
// the sorted held-run list the sweep ran with. A full tail sweep (no state) runs again when the file is another file or shrank (rotated or
// rewritten) or the held set changed (a notice skipped as not-held may belong to a run held now). Without opts.stateFile nothing is remembered.
function sweepState(file) { try { const x = JSON.parse(fs.readFileSync(file, 'utf8')); return x && typeof x === 'object' && !Array.isArray(x) ? x : null; } catch { return null; } }
function ingestTranscript(file, opts = {}) {
  const tb = opts.tailBytes === 'all' ? Infinity : Number.isFinite(+opts.tailBytes) && opts.tailBytes !== null && opts.tailBytes !== undefined ? +opts.tailBytes : +MF().cfg.sweepTailBytes || 4194304;
  let st; try { st = fs.statSync(file); } catch (e) { throw new Error(`transcript not readable: ${file} (${e.code || e.message})`); }
  const size = st.size, heldKey = Array.isArray(opts.held) ? opts.held.filter(r => typeof r === 'string').sort().join(',') : null;
  let from = tb >= size ? 0 : size - tb, atLineStart = from === 0;
  const prev = opts.stateFile ? sweepState(opts.stateFile) : null;
  if (prev && prev.file === file && prev.ino === st.ino && prev.held === heldKey && Number.isInteger(prev.offset) && prev.offset > 0 && prev.offset <= size && prev.offset >= from) { from = prev.offset; atLineStart = true; }
  const buf = Buffer.alloc(size - from), fd = fs.openSync(file, 'r');
  try { fs.readSync(fd, buf, 0, buf.length, from); } finally { fs.closeSync(fd); }
  const lastNl = buf.lastIndexOf(0x0a); // what follows it is a record still being written: read now, but not remembered (the next sweep starts at it)
  const lines = buf.toString('utf8').split('\n');
  let pos = 0, retryAt = -1; // pos: byte offset of the current line from `from`; retryAt: where the first line whose ingest failed starts
  if (!atLineStart) pos += Buffer.byteLength(lines.shift()) + 1; // a partial first line is skipped silently
  const sessionId = opts.sessionId || path.basename(file).replace(/\.jsonl$/, ''), out = [];
  for (const l of lines) {
    const at0 = pos; pos += Buffer.byteLength(l) + 1;
    const enq = l.includes('"queue-operation"') && l.includes('"enqueue"') && l.includes('<task-notification>'), qc = l.includes('"queued_command"') && l.includes('task-notification');
    if (!enq && !qc) continue;
    let x; try { x = JSON.parse(l); } catch { continue; }
    const at = x && x.attachment;
    const asText = v => typeof v === 'string' ? v : Array.isArray(v) ? v.map(c => (c && typeof c.text === 'string' ? c.text : typeof c === 'string' ? c : '')).join('') : '';
    const text = asText(x && x.content) || asText(at && at.prompt);
    if (!text) continue;
    const res = ingestText(text, { via: 'sweep', ts: x.timestamp, usage: at && at.usage, transcriptPath: file, sessionId, held: opts.held });
    out.push(...res);
    if (retryAt < 0 && res.some(r => /^\S+: error |stop skipped:lock-busy/.test(r))) retryAt = at0; // transient (thrown error, busy lock): the next sweep starts here again; a missing run folder is not retried
  }
  let offset = from + lastNl + 1; if (retryAt >= 0) offset = Math.min(offset, from + retryAt); // never past the first notice that failed to ingest (re-ingest of the ones after it is deduped)
  if (opts.stateFile && (lastNl >= 0 || atLineStart)) saveSweepState(opts.stateFile, { file, ino: st.ino, offset, held: heldKey }, prev); // no newline read from the middle of a line: no line start to remember
  return out;
}
function saveSweepState(stateFile, next, prev) { // never throws: a lost state only means the next sweep reads the tail again
  try {
    if (prev && prev.file === next.file && prev.ino === next.ino && prev.offset === next.offset && prev.held === next.held) return;
    const dir = path.dirname(stateFile); fs.mkdirSync(dir, { recursive: true });
    if (!prev) { // a new session: drop the state of sessions untouched for 14 days (the files are tiny, this only keeps the folder from growing for ever)
      for (const f of fs.readdirSync(dir)) { try { const q = path.join(dir, f); if (Date.now() - fs.statSync(q).mtimeMs > 14 * 86400e3) fs.rmSync(q, { force: true }); } catch {} }
    }
    const tmp = `${stateFile}.${process.pid}.tmp`; fs.writeFileSync(tmp, JSON.stringify(Object.assign({ ts: new Date().toISOString() }, next))); fs.renameSync(tmp, stateFile);
  } catch {}
}
function ingest(o) {
  onlyIngestFlags(o);
  if (o.file !== undefined && o.transcript !== undefined) fail('ingest: give --file or --transcript, not both');
  if (o['tail-bytes'] !== undefined && o.transcript === undefined) fail('ingest: --tail-bytes goes with --transcript');
  if (o['session-id'] !== undefined && !SAFE_ID(o['session-id'])) fail(`--session-id '${o['session-id']}': a session id`);
  const held = gateOf(o);
  let lines;
  if (o.transcript !== undefined) {
    const tb = o['tail-bytes'] === undefined ? undefined : String(o['tail-bytes']).toLowerCase() === 'all' ? 'all' : V.int('tail-bytes')(o['tail-bytes']);
    if (!isFile(o.transcript)) fail(`--transcript '${o.transcript}': not a readable file`);
    lines = ingestTranscript(o.transcript, { tailBytes: tb, sessionId: o['session-id'], held });
  } else {
    let text;
    try { text = fs.readFileSync(o.file !== undefined ? o.file : 0, 'utf8'); } catch (e) { fail(`ingest: can't read ${o.file !== undefined ? o.file : 'stdin'} (${e.code || e.message})`); }
    if (o.meta !== undefined && !isFile(o.meta)) fail(`--meta '${o.meta}': not a readable file`);
    lines = ingestText(text, { via: 'cli', sessionId: o['session-id'], meta: o.meta, held });
  }
  for (const l of lines) console.log(l);
}
function onlyIngestFlags(o) { for (const f of Object.keys(o)) if (!['file', 'transcript', 'tail-bytes', 'session-id', 'meta', 'sid', 'run'].includes(f)) fail(`ingest: unknown flag --${f} (flags: --file --transcript --tail-bytes --session-id --meta --sid --run)`); }
// The CLI gate (OD-3): --sid S, else env WFP_SID; an explicit empty --sid= is no session id (no env fallback). -> the allowed
// run names (S's held runs: live, or expired and still S's; plus --run R unless another session holds R live), or undefined.
function gateOf(o) {
  let sid = null;
  if (o.sid !== undefined) { if (o.sid !== '') { if (!SAFE_ID(o.sid)) fail(`--sid '${o.sid}': a session id`); sid = o.sid; } }
  else { const e = process.env.WFP_SID; if (SAFE_ID(e)) sid = e; }
  let R = null;
  if (o.run !== undefined) {
    R = V.run(o.run);
    if (!sid) fail('ingest: --run needs a session id (--sid S, or env WFP_SID)');
    if (!fs.existsSync(path.join(RUNS, R))) fail(`ingest: --run ${R}: run folder ${path.join(RUNS, R)} not found`);
  }
  if (!sid) return undefined;
  const LS = LSM(), held = LS.heldBy(ORCH_DIR, sid).map(x => x.run);
  if (R && !held.includes(R)) {
    const r = LS.readRunLease(ORCH_DIR, R), L = r && r.lease;
    if (L && L.session !== sid && LS.isLive(L)) console.error(`ledger: note: --run ${R} is held live by ${String(L.session).slice(0, 8)}; not added (its notices give none:not-held)`);
    else held.push(R);
  }
  return held;
}

// ---- backfill-models (P29): fill model_id on ledger lines that lack it, from the attempt's stop events, else its subagent transcript --
// Reasons a line stays unresolved, in check order: codex (no Claude transcript), no-id, no-launch (no launch event in the manifest), no-stop
// (no stop with a task id), no-transcript (no agent-<task>.jsonl under the start's or the sessions' subagents folders: deleted after 30 days, or
// never there), no-model-record (the transcript has neither the model attachment nor an assistant record in its head).
const BACKFILL_REASONS = ['codex', 'no-id', 'no-launch', 'no-stop', 'no-transcript', 'no-model-record'];
function resolveModelId(x, evOf) {
  if (isCodex(x)) return { reason: 'codex' };
  const a = typeof x.id === 'string' ? MF().parseAttemptId(x.id) : null;
  if (!a) return { reason: 'no-id' };
  const M = MF(), evs = evOf(a.run).filter(e => e.attempt === a.id).sort(bySeq);
  if (!evs.some(e => e.type === 'launch')) return { reason: 'no-launch' };
  const stops = evs.filter(e => e.type === 'stop');
  const fromStop = [...stops].reverse().map(s => s.model_id).find(modelOk);
  if (fromStop) return { model_id: fromStop, from: 'stop' };
  const tasks = [...new Set([...stops.filter(isNoticeFinal), ...stops].map(s => s.task_id).filter(SAFE_ID))];
  if (!tasks.length) return { reason: 'no-stop' };
  const starts = evs.filter(e => e.type === 'start'), dirs = [];
  for (const s of starts) { const m = M.metaFor(s); if (m) dirs.push(path.dirname(m._file)); if (s.transcript_path && SAFE_ID(s.session)) dirs.push(path.join(path.dirname(String(s.transcript_path)), s.session, 'subagents')); }
  const sessions = [...new Set(evs.map(e => e.session).filter(SAFE_ID))];
  if (sessions.length) { let ps = []; try { ps = fs.readdirSync(M.PROJECTS).sort(); } catch {} for (const d of ps) for (const s of sessions) dirs.push(path.join(M.PROJECTS, d, s, 'subagents')); }
  let file = null;
  for (const t of tasks) { for (const d of [...new Set(dirs)]) { const f = path.join(d, `agent-${t}.jsonl`); if (isFile(f)) { file = f; break; } } if (file) break; }
  if (!file) return { reason: 'no-transcript' };
  const mid = transcriptFacts(file, false).model_id;
  return mid ? { model_id: mid, from: 'transcript', file } : { reason: 'no-model-record', file };
}
function backfillModels(argv) {
  const dry = argv.includes('--dry-run'), asJson = argv.includes('--json'), o = parseFlags(argv.filter(a => a !== '--dry-run' && a !== '--json'));
  for (const f of Object.keys(o)) if (!['run', 'since'].includes(f)) fail(`backfill-models: unknown flag --${f} (backfill-models [--run R] [--since <date>] [--dry-run] [--json])`);
  const since = o.since !== undefined ? Date.parse(String(o.since)) : null;
  if (o.since !== undefined && !Number.isFinite(since)) fail(`--since '${o.since}': a date or ISO time, e.g. 2026-11-11`);
  let runs = [];
  if (o.run !== undefined) { const r = V.run(o.run); if (!isFile(ledgerFile(r))) fail(`backfill-models: run ${r} has no ledger (${ledgerFile(r)})`); runs = [r]; }
  else { try { runs = fs.readdirSync(RUNS).filter(d => SAFE_ID(d) && isFile(ledgerFile(d))).sort(); } catch {} }
  const evCache = new Map(), evOf = r => { if (!evCache.has(r)) { let e = []; try { e = MF().readEvents(r).events; } catch {} evCache.set(r, e); } return evCache.get(r); };
  const sum = { dry_run: dry, since: o.since ?? null, runs: runs.length, lines: 0, already: 0, resolved: 0, from_stop: 0, from_transcript: 0, written: 0, unresolved: 0,
    reasons: Object.fromEntries(BACKFILL_REASONS.map(k => [k, 0])), failed: [], by_model: {} };
  for (const run of runs) {
    const file = ledgerFile(run), { rows } = readRows(file), fills = new Map();
    for (const x of rows) {
      if (since != null && x.ts && Date.parse(x.ts) < since) continue;
      sum.lines++;
      if (modelOk(x.model_id)) { sum.already++; continue; }
      const r = resolveModelId(x, evOf);
      if (r.model_id) { sum.resolved++; sum[r.from === 'stop' ? 'from_stop' : 'from_transcript']++; sum.by_model[r.model_id] = (sum.by_model[r.model_id] || 0) + 1; fills.set(x.id, r.model_id); }
      else { sum.unresolved++; sum.reasons[r.reason]++; sum.failed.push({ id: x.id || null, run, row: x.row, ts: x.ts || null, model: x.model || null, reason: r.reason }); }
    }
    if (dry || !fills.size) continue;
    sum.written += withLock(file, () => { // re-read under the lock: only lines still without model_id are filled (idempotent)
      const lines = fs.readFileSync(file, 'utf8').split('\n'); let n = 0;
      lines.forEach((l, i) => { if (!l.trim()) return; let x; try { x = JSON.parse(l); } catch { return; }
        if (x && typeof x.id === 'string' && fills.has(x.id) && !modelOk(x.model_id)) { x.model_id = fills.get(x.id); x.model_id_from = 'backfill'; lines[i] = JSON.stringify(x); n++; } });
      if (n) { const tmp = `${file}.${process.pid}.tmp`; fs.writeFileSync(tmp, lines.join('\n')); fs.renameSync(tmp, file); }
      return n;
    });
  }
  if (asJson) return console.log(JSON.stringify(sum, null, 2));
  console.log(`backfill-models${dry ? ' (dry run: nothing written)' : ''}: ${sum.runs} runs, ${sum.lines} lines${sum.since ? ` since ${sum.since}` : ''}: already ${sum.already}, resolved ${sum.resolved} (stop ${sum.from_stop}, transcript ${sum.from_transcript})${dry ? '' : `, written ${sum.written}`}, unresolved ${sum.unresolved}`);
  if (sum.resolved) console.log(`resolved: ${Object.entries(sum.by_model).map(([k, v]) => `${k} ${v}`).join(', ')}`);
  if (sum.unresolved) {
    console.log(`unresolved: ${BACKFILL_REASONS.filter(k => sum.reasons[k]).map(k => `${k} ${sum.reasons[k]}`).join(', ')}`);
    for (const f of sum.failed) console.log(`  ${f.id || `${f.run} row ${f.row} ts ${f.ts}`}: ${f.reason}`);
  }
}

// ---- metrics store (design §12, row 13b): metrics.js (node:sqlite) is required here only, never by add/update/show/ingest --
const METRICS_LIB = { ROLES, MODELS, EFFORTS, SMALL, isCodex, fmtTok, fmtMs, table };
const metricsMod = () => require(path.join(__dirname, 'metrics.js'));
// C8 analytics seam: dashboard and cron live in the boss-analytics plugin. Its lib dir comes from config analytics.lib, else env BOSS_ANALYTICS_LIB.
function analyticsMod(name) {
  const cfg = paths.loadConfig('engine').cfg, dir = (cfg.analytics && cfg.analytics.lib) || process.env.BOSS_ANALYTICS_LIB;
  const file = dir ? path.join(dir, `${name}.js`) : null;
  if (!file || !fs.existsSync(file)) fail(`${name}: needs an analytics add-on (not published)`);
  return require(file);
}
function syncCmd(argv) {
  const FL = ['--rebuild', '--no-transcripts', '--no-git', '--full-git'], rebuild = argv.includes('--rebuild'), tx = !argv.includes('--no-transcripts'), rest = argv.filter(a => !FL.includes(a));
  if (rest.length) fail(`sync: unexpected argument '${rest[0]}' (sync [--rebuild] [--no-transcripts] [--no-git] [--full-git])`);
  const s = metricsMod().sync({ rebuild, tx, noGit: argv.includes('--no-git'), gitFull: argv.includes('--full-git') }, METRICS_LIB);
  console.log(`ledger: synced ${s.db} (${s.mode}): attempts ${s.attempts}, events ${s.events}, runs ${s.runs}; files ${s.files}, lines ${s.lines}; upserted ${s.upserted}, unchanged ${s.unchanged}, deleted ${s.deleted}; bad lines ${s.bad_lines}, skipped ${s.skipped_lines}, dup ids ${s.dup_ids}`);
  if (s.tx) console.log(`ledger: transcripts: ${s.tx.files_seen} files (${s.tx.files_read} read ${(s.tx.bytes_read / 1048576).toFixed(1)} MB, ${s.tx.files_skipped} unchanged, ${s.tx.files_reset} reset), calls +${s.tx.calls_upserted} (now ${s.tx.tx_calls}), dup ids ${s.tx.dup_ids}, bad lines ${s.tx.bad_lines}; wfp snapshots read ${s.tx.wfp_files}; ${s.tx.ms} ms`);
  if (s.tx) syncNotes(s.tx);
}
function syncNotes(t) { // catch-up: the weekly cron missed (or never ran) when the previous sync is over 7 days old
  if (t.sha_backfilled) console.log(`ledger: sha backfill: ${t.sha_backfilled} older files re-read for commit SHAs`);
  if (t.git_repos != null) console.log(`ledger: git commits: ${t.git_repos} repos scanned since ${t.git_since}, +${t.git_new} new (${t.tx_git_commits} stored), ${t.git_ms} ms`);
  const age = t.prev_sync ? (Date.now() - Date.parse(t.prev_sync)) / 864e5 : null;
  if (age != null && age > 7) console.log(`ledger: note: the previous sync was ${age.toFixed(1)} days ago (weekly cron should run Sunday 03:00; check: ledger.js cron status). Caught up now.`);
}
function dashboardCmd(argv) {
  const D = analyticsMod('dashboard'); // before anything else: a core-only install exits 2 here
  const B = ['--no-sync', '--open', '--commits', '--json'], o = {}; let out = path.join(ORCH_DIR, 'dashboard.html');
  for (let i = 0; i < argv.length; i++) {
    if (B.includes(argv[i])) o[argv[i].slice(2)] = true;
    else if (argv[i] === '--out' && argv[i + 1]) out = path.resolve(argv[++i]);
    else if (argv[i].startsWith('--out=')) out = path.resolve(argv[i].slice(6));
    else fail(`dashboard: unexpected argument '${argv[i]}' (dashboard [--no-sync] [--out PATH] [--open] [--commits] [--json])`);
  }
  const M = metricsMod(), t0 = Date.now();
  if (!o['no-sync']) { const s = M.sync({ tx: true, gitFull: !!o.commits }, METRICS_LIB); if (!o.json) { console.log(`ledger: synced ${s.db}: attempts ${s.attempts}, runs ${s.runs}${s.tx ? `; transcripts ${s.tx.files_read} read, calls +${s.tx.calls_upserted} (now ${s.tx.tx_calls}), ${s.tx.ms} ms` : ''}`); if (s.tx) syncNotes(s.tx); } }
  if (o['no-sync']) { const age = M.txSyncAgeDays(); if (age != null && age > M.staleDays()) console.error(`${M.staleMsg(age)} --no-sync reads it as it stands; drop --no-sync to catch up.`); }
  if (!fs.existsSync(M.DB_PATH)) fail(`dashboard: no metrics.db at ${M.DB_PATH}; run without --no-sync`);
  const db = M.openDb(M.DB_PATH, true);
  let data; try { data = D.build(db, { costOfRun, readRows }, { runsDir: RUNS, retention: M.retention(db) }); } finally { db.close(); }
  if (o.json) return console.log(JSON.stringify(data));
  fs.mkdirSync(path.dirname(out), { recursive: true }); fs.writeFileSync(out, D.render(data));
  console.log(`ledger: dashboard ${out} (${(fs.statSync(out).size / 1024).toFixed(0)} KB, ${Date.now() - t0} ms${data.commit_mode === 'git' ? `, ${data.quality.git_commits} git commits` : ''})`);
  if (data.retention && data.retention.sessions) console.log(`ledger: retention: ${data.retention.sessions} sessions will be deleted by Claude Code ${data.retention.days_left < 0.5 ? 'at any time' : `within ${Math.ceil(data.retention.days_left)} days`}: ${data.retention.loaded} loaded, ${data.retention.not_loaded} not loaded`);
  if (o.open) { const how = D.openFile(out); console.log(how ? `ledger: opened with ${how}` : `ledger: could not open it here; open ${out}`); }
}
function cronCmd(argv) {
  const C = analyticsMod('cron'), sub = argv[0];
  if (!['install', 'remove', 'status'].includes(sub) || argv.length > 1) fail('cron: install | remove | status');
  if (sub === 'status') return C.printStatus(C.status());
  try {
    const r = sub === 'install' ? C.install() : C.remove();
    const mark = path.join(ORCH_DIR, 'cron-removed'); // tells doctor the missing cron line is a choice (install deletes it)
    try { if (sub === 'remove') { fs.mkdirSync(ORCH_DIR, { recursive: true }); fs.writeFileSync(mark, `${new Date().toISOString()}\n`); } else fs.rmSync(mark, { force: true }); } catch {}
    console.log(sub === 'install' ? `cron: ${r.changed ? 'installed' : 'already installed'}: ${r.line}` : `cron: ${r.changed ? 'removed' : 'nothing to remove'}`);
  } catch (e) { fail(`cron ${sub}: ${e.message}`); }
  if (sub === 'install') C.printStatus(C.status());
}
function reportCmd(argv) {
  const name = argv[0], names = ['tokens-per-accepted', 'first-pass', 'retries'];
  if (!names.includes(name)) fail(`report: name one of ${names.join(' | ')} (report <name> [--run R] [--role R] [--repo R] [--area A] [--json] [--no-sync])`);
  const asJson = argv.includes('--json'), noSync = argv.includes('--no-sync'), o = parseFlags(argv.slice(1).filter(a => a !== '--json' && a !== '--no-sync'));
  for (const f of Object.keys(o)) if (!['run', 'role', 'repo', 'area'].includes(f)) fail(`report: unknown flag --${f} (filters: --run --role --repo --area, and --json, --no-sync)`);
  const filters = { run: o.run !== undefined ? V.run(o.run) : null, role: o.role !== undefined ? V.role(o.role) : null, // the same validators as show
    repo: o.repo !== undefined ? repoV(o.repo).toLowerCase() : null, area: o.area !== undefined ? V.text('area')(o.area).toLowerCase() : null };
  if (name === 'retries') return retriesCmd(filters, asJson); // reads the ledger files only: no sync, no db (--no-sync is moot)
  const M = metricsMod(), existed = fs.existsSync(M.DB_PATH);
  if (noSync && !existed) fail(`report --no-sync: no metrics.db at ${M.DB_PATH} yet; run 'ledger.js sync' (or drop --no-sync: report then creates it from the run ledgers)`);
  const r = M.report(name, filters, METRICS_LIB, noSync ? { sync: false } : {});
  if (r.note) console.error(r.note); // stderr: --json and piped stdout stay clean
  if (!noSync && !existed) console.error(`ledger: created ${M.DB_PATH} from the run ledgers (a derived cache; delete it any time, or use report --no-sync to never write it)`);
  if (name === 'first-pass' && r.json && r.json.matched) causeSplit(r, filters);
  console.log(asJson ? JSON.stringify(r.json, null, 2) : r.text);
}
function retriesCmd(filters, asJson) {
  const R = require(path.join(__dirname, 'ledger-retries.js')); let dirs = []; // read-only: the ledger.jsonl files, no lock, no db
  try { dirs = fs.readdirSync(RUNS).filter(d => isFile(ledgerFile(d))).sort(); } catch {}
  let bad = 0; const byRun = [];
  for (const d of dirs) { if (filters.run && d.toLowerCase() !== filters.run) continue; const r = readRows(ledgerFile(d)); bad += r.bad; byRun.push({ run: d, rows: r.rows }); }
  if (bad) console.error(`ledger: skipped ${bad} unreadable line(s)`);
  const d = R.build(byRun, filters);
  console.log(asJson ? JSON.stringify(d, null, 2) : R.render(d, METRICS_LIB));
}
// P31 split (first-pass report): every attempt that has a later attempt in its row is one retry, counted under its cause: the retried line's
// cause, else the next line's retry_cause (add and the launch event record it there), a stated value before 'unstated'; tokens = the next
// attempt's. 'unstated' = recorded without --cause; 'unlabelled' = lines from before the fields.
// Read from the db report() just synced (attempts.raw, so no schema change), with the report's filters.
const CAUSE_ROWS = [...CAUSES, UNSTATED, 'unlabelled'];
function causeSplit(r, p) {
  const M = metricsMod(), db = M.openDb(M.DB_PATH, true);
  let rows;
  try {
    rows = db.prepare(`WITH a AS (SELECT run, row, n, tokens, is_codex, json_extract(raw, '$.cause') AS cause, json_extract(raw, '$.retry_cause') AS rcause FROM attempts
        WHERE (:run IS NULL OR run = :run) AND (:role IS NULL OR role = :role)
          AND (:repo IS NULL OR lower(coalesce(repo,'')) = :repo) AND (:area IS NULL OR lower(coalesce(area,'')) = :area)),
      nx AS (SELECT a.is_codex, a.cause, (SELECT b.n FROM a b WHERE b.run = a.run AND b.row = a.row AND b.n > a.n ORDER BY b.n LIMIT 1) AS next_n, a.run, a.row FROM a),
      pr AS (SELECT nx.is_codex, nx.cause AS pc, b.rcause AS sc, b.tokens AS next_tokens FROM nx JOIN a b ON b.run = nx.run AND b.row = nx.row AND b.n = nx.next_n)
      SELECT CASE WHEN is_codex = 1 THEN 'codex' ELSE 'claude' END AS family,
        CASE WHEN pc IS NOT NULL AND pc <> '${UNSTATED}' THEN pc WHEN sc IS NOT NULL AND sc <> '${UNSTATED}' THEN sc
             WHEN pc IS NOT NULL OR sc IS NOT NULL THEN '${UNSTATED}' ELSE 'unlabelled' END AS cause,
        COUNT(*) AS retries, SUM(next_tokens) AS tokens
      FROM pr GROUP BY 1, 2`).all({ run: p.run ?? null, role: p.role ?? null, repo: p.repo ?? null, area: p.area ?? null });
  } finally { db.close(); }
  const lines = [];
  if (rows.length) r.json.retry_causes = {};
  for (const family of ['claude', 'codex']) {
    const fr = rows.filter(x => x.family === family);
    if (!fr.length) continue;
    const by = {}, tot = fr.reduce((s, x) => s + Number(x.retries), 0);
    for (const c of CAUSE_ROWS) { const x = fr.find(y => y.cause === c); by[c] = { retries: x ? Number(x.retries) : 0, tokens: x && x.tokens != null ? Number(x.tokens) : 0 }; }
    for (const x of fr) if (!CAUSE_ROWS.includes(x.cause)) by[x.cause] = { retries: Number(x.retries), tokens: x.tokens != null ? Number(x.tokens) : 0 }; // a hand-edited value
    const ns = by[UNSTATED].retries + by.unlabelled.retries;
    r.json.retry_causes[family] = { retries: tot, not_stated: ns, unstated: by[UNSTATED].retries, unlabelled: by.unlabelled.retries, causes: by };
    lines.push(`retry kind (${family}) by cause: ${Object.entries(by).map(([c, v]) => `${c} ${v.retries}${v.retries ? ` (next-attempt tokens ${fmtTok(v.tokens)})` : ''}`).join(', ')}; ` +
      `not stated ${ns} of ${tot} retries${tot ? ` (${Math.round(100 * ns / tot)}%)` : ''}`);
  }
  if (!lines.length) return; // no retries match: the report as it was
  const t = r.text.split('\n'); // one line per family, right after that family's 'retry kind (<family>):' line
  for (const l of lines) { const fam = l.slice(12, l.indexOf(')')), at = t.findIndex(x => x.startsWith(`retry kind (${fam}):`)); if (at >= 0) t.splice(at + 1, 0, l); else t.push(l); }
  r.text = t.join('\n');
}

// ---- cost (read-only: readRows only; no lock, no sync, no db) ---------------------------
const rowKey = r => { const m = String(r).match(/^(\d+)([a-z]*)$/i); return m ? [+m[1], m[2].toLowerCase()] : [Infinity, String(r)]; };
const byRowKey = (a, b) => { const x = rowKey(a), y = rowKey(b); return x[0] - y[0] || (x[1] < y[1] ? -1 : x[1] > y[1] ? 1 : 0); };
const byTs = (a, b) => String(a.ts || '').localeCompare(String(b.ts || ''));
const sumKnown = (list, k) => { const v = list.map(x => x[k]); return { sum: sumOf(v), unknown: v.filter(x => x == null).length, n: v.length }; };
const fmtPart = (p, f) => p.unknown === p.n ? '?' : f(p.sum) + (p.unknown ? '?' : ''); // '?' marks a sum with unknown attempts in it
function costOfRun(run, rows) {
  const byRow = new Map();
  for (const x of rows) { const k = String(x.row); if (!byRow.has(k)) byRow.set(k, []); byRow.get(k).push(x); }
  const out = [...byRow.keys()].sort(byRowKey).map(k => {
    const list = byRow.get(k).sort(byTs), acc = [...list].reverse().find(x => x.firstPass === true), pick = acc || list[list.length - 1];
    return { row: list[0].row, attempts: list.length, model: pick.model ?? null, effort: pick.effort ?? null,
      tokens: sumKnown(list, 'tokens'), tools: sumKnown(list, 'tools'), ms: sumKnown(list, 'ms'),
      retries: list.length - 1, escalated: list.some(x => x.escalated === true), first_pass: list[0].firstPass ?? null };
  });
  const tot = { tokens: sumKnown(rows, 'tokens'), tools: sumKnown(rows, 'tools'), ms: sumKnown(rows, 'ms') };
  return { run, rows: out.length, attempts: rows.length, tokens: tot.tokens, tools: tot.tools, ms: tot.ms,
    retries: out.reduce((s, r) => s + r.retries, 0), escalations: out.filter(r => r.escalated).length,
    last_ts: rows.map(x => String(x.ts || '')).sort().pop() || '', table: out };
}
const partJson = p => ({ sum: p.sum, unknown: p.unknown });
function cost(argv) {
  const asJson = argv.includes('--json'), o = parseFlags(argv.filter(a => a !== '--json'));
  for (const f of Object.keys(o)) if (!['run', 'limit'].includes(f)) fail(`cost: unknown flag --${f} (cost [--run R] [--limit N] [--json])`);
  if (o.run !== undefined && o.limit !== undefined) fail('cost: --limit applies to the run list (no --run) only');
  if (o.run !== undefined) {
    const run = V.run(o.run), { rows, bad } = readRows(ledgerFile(run));
    if (bad) console.error(`ledger: skipped ${bad} unreadable line(s)`);
    if (!rows.length) return console.log(asJson ? JSON.stringify({ run, ledger: false }, null, 2) : `cost: run ${run} has no ledger yet`);
    const c = costOfRun(run, rows);
    if (asJson) return console.log(JSON.stringify({ run, ledger: true, rows: c.rows, attempts: c.attempts, tokens: partJson(c.tokens), tools: partJson(c.tools), ms: partJson(c.ms),
      retries: c.retries, escalations: c.escalations, table: c.table.map(r => ({ ...r, tokens: partJson(r.tokens), tools: partJson(r.tools), ms: partJson(r.ms) })) }, null, 2));
    console.log(`cost: run ${run}: ${c.rows} rows, ${c.attempts} attempts, tokens ${fmtPart(c.tokens, fmtTok)}, tools ${fmtPart(c.tools, String)}, time ${fmtPart(c.ms, fmtMs)}`);
    console.log(table(['row', 'attempts', 'model/effort', 'tokens', 'tools', 'time', 'retries', 'escalated', 'first-pass'],
      c.table.map(r => [r.row, r.attempts, `${r.model ?? '-'}/${r.effort ?? '-'}`, fmtPart(r.tokens, fmtTok), fmtPart(r.tools, String), fmtPart(r.ms, fmtMs), r.retries, yn(r.escalated), yn(r.first_pass)]),
      [5, 8, 24, 9, 6, 7, 7, 9, 10]));
    const unk = [['tokens', c.tokens], ['tools', c.tools], ['time', c.ms]].filter(([, p]) => p.unknown).map(([k, p]) => `${k} on ${p.unknown} attempt${p.unknown === 1 ? '' : 's'}`);
    if (unk.length) console.log(`unknown: ${unk.join(', ')}`);
    return;
  }
  const limit = o.limit === undefined ? 20 : V.int('limit')(o.limit); if (limit < 1 || limit > 50) fail(`--limit '${o.limit}': 1-50`);
  let dirs = []; try { dirs = fs.readdirSync(RUNS).filter(d => isFile(ledgerFile(d))); } catch {}
  let bad = 0; const runs = [];
  for (const d of dirs) { const r = readRows(ledgerFile(d)); bad += r.bad; if (r.rows.length) runs.push(costOfRun(d, r.rows)); }
  if (bad) console.error(`ledger: skipped ${bad} unreadable line(s)`);
  runs.sort((a, b) => b.last_ts.localeCompare(a.last_ts) || b.run.localeCompare(a.run));
  const shown = runs.slice(0, limit);
  if (asJson) return console.log(JSON.stringify({ runs: runs.length, shown: shown.length, list: shown.map(c => ({ run: c.run, rows: c.rows, attempts: c.attempts,
    tokens: partJson(c.tokens), ms: partJson(c.ms), retries: c.retries, escalations: c.escalations, last_ts: c.last_ts })) }, null, 2));
  if (!runs.length) return console.log(`cost: no run has a ledger yet under ${RUNS}`);
  console.log(`cost: ${shown.length} of ${runs.length} runs with a ledger (newest first)`);
  console.log(table(['run', 'rows', 'attempts', 'tokens', 'time', 'retries', 'escalations'],
    shown.map(c => [c.run, c.rows, c.attempts, fmtPart(c.tokens, fmtTok), fmtPart(c.ms, fmtMs), c.retries, c.escalations]), [40, 5, 8, 9, 7, 7, 11]));
}

// ---- entry -----------------------------------------------------------------------
function main(argv) {
  const cmd = argv[0];
  if (!cmd || cmd === 'help' || cmd === '--help' || cmd === '-h') return console.log(HELP);
  if (cmd === 'sync' || cmd === 'report') return (cmd === 'sync' ? syncCmd : reportCmd)(argv.slice(1)); // raw argv: boolean flags, report name
  if (cmd === 'cost') return cost(argv.slice(1)); // raw argv: --json is a boolean flag
  if (cmd === 'dashboard') return dashboardCmd(argv.slice(1)); // raw argv: boolean flags
  if (cmd === 'cron') return cronCmd(argv.slice(1));
  if (cmd === 'backfill-models') return backfillModels(argv.slice(1)); // raw argv: --dry-run and --json are boolean flags
  const fn = { add, update, close, show, ingest }[cmd];
  if (!fn) fail(`unknown command '${cmd}' (add | update | show | ingest | help); row close: close; model backfill: backfill-models; read-only: cost; metrics: sync | report | dashboard | cron`);
  fn(parseFlags(argv.slice(1)));
}
module.exports = { parseNotices, ingestText, ingestTranscript, ensureLedgerLine, transcriptFacts, main };
if (require.main === module) {
  try { main(process.argv.slice(2)); } catch (e) {
    if (e instanceof UsageError) { console.error(`ledger: error: ${e.message}\n(usage: node "$BOSS_LIB/ledger.js" help)`); process.exit(2); }
    console.error(`ledger: failed: ${e && e.message}`); process.exit(1);
  }
}
