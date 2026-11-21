// metrics: the derived query/report layer over the orchestrator's JSONL files (design 6-d4-design.md §12, row 13b).
// JSONL stays the write path. metrics.db ($ORCH_METRICS_DB, default <data dir>/metrics.db; paths.js) is derived, gitignored and
// rebuildable: `ledger.js sync [--rebuild]` is its only writer, `ledger.js report ...` reads it with {readOnly:true}.
// node:sqlite is loaded here and only here; ledger.js requires this file lazily inside `sync` and `report`, so add, update,
// show, ingest, manifest.js and every hook never load it. No npm dependency.
'use strict';
const fs = require('fs'), path = require('path'), { execFileSync } = require('child_process');
const paths = require('./paths.js'); // every path and the engine config come from here
const RUNS = paths.runsDir();
const DB_PATH = paths.metricsDb();
const PROJECTS = paths.projectsDir();
const WFP_STATE = process.env.ORCH_WFP_STATE || paths.wfpStateDir();
const SCHEMA_VERSION = 6; // 2: cw_1h, tx_files/tx_agents/tx_edits/tx_shas/tx_sync_log/prices, tx views, transcript + wfp_tel active; 3: tx_files.pending_tu/sha_v, tx_git_commits, tx_meta; 5: attempts.model_id and events.model_id (resolved model ID); 6: helper column (claude-mem observer + double-shot-latte sessions, loaded but left out of every view), advisor adv_* columns from usage.iterations, fable price family; rebuild and bumps keep every tx table

let sqliteMod = null;
function sqlite() { // silence only node:sqlite's ExperimentalWarning (3-hook-facts (i)); every other warning still prints
  if (sqliteMod) return sqliteMod;
  const emit = process.emit;
  process.emit = function (name, data, ...rest) {
    if (name === 'warning' && data && data.name === 'ExperimentalWarning' && /SQLite/.test(String(data.message))) return false;
    return emit.call(this, name, data, ...rest);
  };
  sqliteMod = require('node:sqlite');
  return sqliteMod;
}

// ---- schema (§12.2) ------------------------------------------------------------------
const DDL = `
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (source TEXT PRIMARY KEY, status TEXT NOT NULL CHECK (status IN ('active','reserved')),
  path_glob TEXT NOT NULL, key_desc TEXT NOT NULL, tbl TEXT);
CREATE TABLE IF NOT EXISTS sync_log (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
  mode TEXT NOT NULL,
  files INTEGER, lines INTEGER, upserted INTEGER, unchanged INTEGER, deleted INTEGER,
  bad_lines INTEGER, skipped_lines INTEGER, dup_ids INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS runs (run TEXT PRIMARY KEY, dir TEXT NOT NULL, has_ledger INTEGER NOT NULL, has_manifest INTEGER NOT NULL,
  n_attempts INTEGER NOT NULL, n_events INTEGER NOT NULL, first_ts TEXT, last_ts TEXT,
  source TEXT NOT NULL DEFAULT 'fs', source_ref TEXT NOT NULL, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY, id_derived INTEGER NOT NULL, run TEXT NOT NULL, row TEXT NOT NULL, n INTEGER,
  ts TEXT, updated TEXT, role TEXT, agent TEXT, model TEXT, model_id TEXT, effort TEXT, mode TEXT,
  tokens INTEGER, tools INTEGER, ms INTEGER, repo TEXT, area TEXT,
  first_pass INTEGER, retries INTEGER, escalated INTEGER,
  is_codex INTEGER NOT NULL, effort_known INTEGER NOT NULL,
  src_file TEXT NOT NULL, src_line INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'ledger', source_ref TEXT NOT NULL,
  raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, id_derived INTEGER NOT NULL, run TEXT NOT NULL, seq INTEGER NOT NULL,
  type TEXT NOT NULL, ts TEXT, attempt TEXT, row TEXT, key TEXT, via TEXT, session_id TEXT,
  tool_use_id TEXT, task_id TEXT, agent TEXT, model TEXT, model_id TEXT, effort TEXT, effort_observed TEXT,
  tokens INTEGER, tools INTEGER, ms INTEGER, status TEXT, phase TEXT, final INTEGER, action TEXT, bytes INTEGER,
  src_file TEXT NOT NULL, src_line INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'manifest', source_ref TEXT NOT NULL,
  raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_att_run_row ON attempts(run, row);
CREATE INDEX IF NOT EXISTS ix_ev_attempt ON events(attempt);
CREATE INDEX IF NOT EXISTS ix_ev_task ON events(task_id);
CREATE INDEX IF NOT EXISTS ix_ev_tool ON events(tool_use_id);
CREATE INDEX IF NOT EXISTS ix_ev_session ON events(session_id);
-- Transcript source (row 2, schema 2): tx_calls is one row per API message (deduped), tx_files the per-file resume state.
CREATE TABLE IF NOT EXISTS tx_calls (session_id TEXT NOT NULL, message_id TEXT NOT NULL, agent_id TEXT, request_id TEXT, ts TEXT,
  model TEXT, effort TEXT, is_sidechain INTEGER, input_tokens INTEGER, cache_creation_input_tokens INTEGER,
  cache_read_input_tokens INTEGER, output_tokens INTEGER, cw_1h INTEGER,
  inline_n INTEGER, agent_n INTEGER, -- schema 4: main-chat messages only: tool_use blocks that are inline work / Agent calls (NULL = not counted yet, see inlineBackfill)
  source TEXT NOT NULL DEFAULT 'transcript', source_ref TEXT NOT NULL,
  helper INTEGER NOT NULL DEFAULT 0, -- schema 6: 1 = a helper session (HELPER_DIR: claude-mem observer, double-shot-latte); every view leaves these out
  adv_n INTEGER, adv_model TEXT, adv_in INTEGER, adv_out INTEGER, adv_cr INTEGER, adv_cw INTEGER, adv_cw1h INTEGER, -- schema 6: advisor sub-calls (usage.iterations type advisor_message; NOT in the top-level usage), NULL = none seen
  PRIMARY KEY (session_id, message_id));
CREATE INDEX IF NOT EXISTS ix_tx_msg ON tx_calls(message_id);
CREATE INDEX IF NOT EXISTS ix_tx_agent ON tx_calls(agent_id);
CREATE INDEX IF NOT EXISTS ix_tx_sess_ts ON tx_calls(session_id, ts);
-- per-file resume state + per-file session counters (v_tx_sessions is a view that sums these per session_id)
CREATE TABLE IF NOT EXISTS tx_files (path TEXT PRIMARY KEY, kind TEXT NOT NULL, session_id TEXT, agent_id TEXT, dir TEXT,
  size INTEGER NOT NULL, mtime_ms INTEGER NOT NULL, offset INTEGER NOT NULL, first_ts TEXT, last_ts TEXT, active_ms INTEGER NOT NULL DEFAULT 0,
  human_turns INTEGER NOT NULL DEFAULT 0, agent_calls INTEGER NOT NULL DEFAULT 0, orch_calls INTEGER NOT NULL DEFAULT 0,
  compactions INTEGER NOT NULL DEFAULT 0, scanned_at TEXT, pending_tu TEXT, sha_v INTEGER NOT NULL DEFAULT 0,
  inline_v INTEGER NOT NULL DEFAULT 0, -- inline_v 1 = this file's tx_calls rows carry inline_n / agent_n
  helper INTEGER NOT NULL DEFAULT 0, adv_v INTEGER NOT NULL DEFAULT 0); -- schema 6: helper as tx_calls.helper; adv_v 1 = this file's rows carry the adv_* columns (see advBackfill)
CREATE INDEX IF NOT EXISTS ix_txf_session ON tx_files(session_id);
CREATE TABLE IF NOT EXISTS tx_agents (session_id TEXT NOT NULL, agent_id TEXT NOT NULL, agent_type TEXT, description TEXT, tool_use_id TEXT,
  spawn_depth INTEGER, model TEXT, effort TEXT, attempt TEXT, src_file TEXT, PRIMARY KEY (session_id, agent_id));
CREATE INDEX IF NOT EXISTS ix_txa_tool ON tx_agents(tool_use_id);
CREATE TABLE IF NOT EXISTS tx_edits (src_file TEXT NOT NULL, file_path TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (src_file, file_path));
CREATE TABLE IF NOT EXISTS tx_shas (src_file TEXT NOT NULL, sha TEXT NOT NULL, PRIMARY KEY (src_file, sha));
-- schema 3: owner commits from git log (survive rebuild like every tx_* table); tx_meta = loader key/values (git scan time)
CREATE TABLE IF NOT EXISTS tx_git_commits (sha TEXT PRIMARY KEY, repo TEXT NOT NULL, t TEXT NOT NULL, subj TEXT);
CREATE TABLE IF NOT EXISTS tx_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tx_sync_log (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, ms INTEGER,
  files_seen INTEGER, files_read INTEGER, files_skipped INTEGER, files_reset INTEGER, bytes_read INTEGER, calls_upserted INTEGER,
  dup_ids INTEGER, bad_lines INTEGER, wfp_files INTEGER);
-- $/M-token weights (PRICES below); seeded once, so a PRICES change reaches an existing db only through metrics.js reseed-prices. unit = cost / the opus fresh-input price.
CREATE TABLE IF NOT EXISTS prices (family TEXT PRIMARY KEY, in_p REAL NOT NULL, out_p REAL NOT NULL, cr_p REAL NOT NULL, cw5_p REAL NOT NULL, cw1h_p REAL NOT NULL);
CREATE TABLE IF NOT EXISTS wfp_telemetry (session_id TEXT NOT NULL, ts INTEGER NOT NULL, cwd TEXT, ctx_tokens INTEGER, ctx_window INTEGER,
  used_pct REAL, model TEXT, effort TEXT, api_equiv_usd REAL, five_hour_pct REAL, seven_day_pct REAL, tel_source TEXT,
  source TEXT NOT NULL DEFAULT 'wfp_tel', source_ref TEXT NOT NULL, PRIMARY KEY (session_id, ts));
CREATE INDEX IF NOT EXISTS ix_ev_sess_ts ON events(session_id, ts);
CREATE TABLE IF NOT EXISTS gsd_metrics (id TEXT PRIMARY KEY, session_id TEXT, agent_id TEXT, agent_type TEXT, description TEXT,
  fresh_tokens INTEGER, cache_read_tokens INTEGER, recorded_at TEXT, raw TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'gsd_metrics', source_ref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS codex_runs (id TEXT PRIMARY KEY, run TEXT, row TEXT, attempt TEXT, log_path TEXT NOT NULL, model TEXT,
  tokens_used INTEGER, started TEXT, finished TEXT,
  source TEXT NOT NULL DEFAULT 'codex_log', source_ref TEXT NOT NULL);
CREATE VIEW IF NOT EXISTS v_attempt_keys AS
  SELECT DISTINCT attempt, session_id, tool_use_id, task_id FROM events
  WHERE attempt IS NOT NULL AND type IN ('start','stop','return_over_cap');
`;
const SOURCES = [ // §12.7: ledger, manifest and fs are active; the rest get schema room only
  ['ledger', 'active', 'runs/*/ledger.jsonl', 'id <run>#<row>.<n> (derived for a line without one: its ordinal in the row)', 'attempts'],
  ['manifest', 'active', 'runs/*/manifest.jsonl', 'id <run>#e<seq> (derived for a line without one: <run>#e<line>)', 'events'],
  ['fs', 'active', 'runs/*/', 'run folder name', 'runs'],
  ['transcript', 'active', '<projects dir>/<slug>/<sid>.jsonl, <slug>/<sid>/subagents/**/agent-*.jsonl', '(session_id, message.id), max output_tokens kept; message_id also unique across sessions', 'tx_calls'],
  ['wfp_tel', 'active', '<wfp state dir>/*.tel.json', '(session_id, ts)', 'wfp_telemetry'],
  ['gsd_metrics', 'reserved', '<workspace>/.planning/**/metrics/session.jsonl', '<file>:<line>', 'gsd_metrics'],
  ['codex_log', 'reserved', 'runs/<run>/codex-*.log', '<run>/<file>', 'codex_runs'],
  ['dashboard', 'reserved', 'none (a reader only, readOnly)', 'none (the v_* views)', null],
];

// BASE (§12.5): the prefix of every report statement and view. Extra columns beyond the design's: g_ord / row_ord / ord give
// show's first-appearance order for exact ties (rows in ts order, then attempts in ts order).
const STEP = `coalesce(model,'null') || '/' || coalesce(effort,'null')`; // show's key `${model}/${effort}`
const BASE = filtered => `WITH f AS (
  SELECT *, CASE is_codex WHEN 1 THEN 'codex' ELSE 'claude' END AS family
  FROM attempts WHERE effort_known = 1${filtered ? `
    AND (:run IS NULL OR run = :run) AND (:role IS NULL OR role = :role)
    AND (:repo IS NULL OR lower(coalesce(repo,'')) = :repo) AND (:area IS NULL OR lower(coalesce(area,'')) = :area)` : ''}),
o AS (
  SELECT f.*,
    ROW_NUMBER() OVER (PARTITION BY is_codex, run, row ORDER BY ts, src_file, src_line) AS k_first,
    ROW_NUMBER() OVER (PARTITION BY is_codex, run, row
                       ORDER BY (first_pass = 1) DESC, ts DESC, src_file DESC, src_line DESC) AS k_acc,
    ROW_NUMBER() OVER (PARTITION BY is_codex ORDER BY ts, src_file, src_line) AS g_ord
  FROM f),
r AS (
  SELECT is_codex, family, run, row,
    MAX(CASE WHEN k_first = 1 THEN coalesce(nullif(role,''),'default') END) AS role,
    COUNT(*) AS n_att, SUM(coalesce(tokens,0)) AS tok,
    MAX(CASE WHEN first_pass = 1 THEN 1 ELSE 0 END) AS accepted,
    MAX(CASE WHEN k_acc = 1 AND first_pass = 1 THEN ${STEP} END) AS acc_step,
    MIN(g_ord) AS row_ord
  FROM o GROUP BY is_codex, family, run, row)
`;
const Q_ROLES = `SELECT family, role, COUNT(*) AS rows, SUM(n_att) AS attempts, SUM(tok) AS tokens, SUM(accepted) AS accepted,
  CASE WHEN SUM(accepted) > 0 THEN CAST(ROUND(1.0 * SUM(tok) / SUM(accepted)) AS INTEGER) END AS tok_per_accepted,
  MIN(row_ord) AS ord
FROM r GROUP BY is_codex, family, role`;
const Q_STEPS_ACC = `SELECT family, role, acc_step AS step, COUNT(*) AS rows, SUM(tok) AS tokens,
  CAST(ROUND(1.0 * SUM(tok) / COUNT(*)) AS INTEGER) AS tok_per_accepted
FROM r WHERE accepted = 1 GROUP BY is_codex, family, role, acc_step
UNION ALL
SELECT family, role, 'no outcome' AS step, COUNT(*) AS rows, SUM(tok) AS tokens, NULL AS tok_per_accepted
FROM r WHERE accepted = 0 GROUP BY is_codex, family, role`;
const Q_FIRST_PASS = `SELECT o.family, r.role, ${STEP.replace(/model|effort/g, 'o.$&')} AS step, o.model AS model, o.effort AS effort, COUNT(*) AS attempts,
  SUM(o.first_pass IS NOT NULL) AS outcomes,
  SUM(CASE WHEN o.first_pass = 1 THEN 1 ELSE 0 END) AS y,
  CASE WHEN SUM(o.first_pass IS NOT NULL) > 0
       THEN CAST(ROUND(100.0 * SUM(CASE WHEN o.first_pass = 1 THEN 1 ELSE 0 END) / SUM(o.first_pass IS NOT NULL)) AS INTEGER) END AS pct,
  SUM(o.first_pass IS NOT NULL) BETWEEN 1 AND 4 AS anecdotal,
  CAST(ROUND(AVG(o.tools)) AS INTEGER) AS avg_tools, AVG(o.ms) AS avg_ms,
  MIN(r.row_ord * 10000000 + o.g_ord) AS ord
FROM o JOIN r ON r.is_codex = o.is_codex AND r.run = o.run AND r.row = o.row
GROUP BY o.is_codex, o.family, r.role, o.model, o.effort`;
// retry kind split (first-pass report): how each attempt was made. The ledger line's retry_kind (continue | fresh) is read from raw, so old lines
// and old databases need no migration; a retry (retries > 0) with no retry_kind is 'unlabelled', everything else is a 'first' attempt.
const Q_RETRY_KINDS = `SELECT family, CASE WHEN json_extract(raw, '$.retry_kind') IN ('continue', 'fresh') THEN json_extract(raw, '$.retry_kind')
    WHEN coalesce(retries, 0) > 0 THEN 'unlabelled' ELSE 'first' END AS kind,
  COUNT(*) AS attempts, SUM(first_pass IS NOT NULL) AS outcomes, SUM(CASE WHEN first_pass = 1 THEN 1 ELSE 0 END) AS y
FROM f GROUP BY family, kind`;
const RETRY_KINDS = ['first', 'continue', 'fresh', 'unlabelled'];
const VIEWS = `
CREATE VIEW IF NOT EXISTS v_rows AS ${BASE(false)} SELECT * FROM r;
CREATE VIEW IF NOT EXISTS v_tokens_per_accepted AS ${BASE(false)} ${Q_ROLES};
CREATE VIEW IF NOT EXISTS v_first_pass AS ${BASE(false)} ${Q_FIRST_PASS};
`;

// ---- transcript views (row 2). Weight = orch-eval weigh(): unit = (in*in_p + out*out_p + cr*cr_p + cw5*cw5_p + cw1h*cw1h_p) / opus in_p.
// [family, in, out, cr, cw5, cw1h] $/M, checked 2026-11-18 against platform.claude.com/docs/en/about-claude/pricing for the current
// generation (Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.2 / Mythos 5.2): cache read 0.05x input (Haiku 0.1x, Fable 0.025x), 5m write 1.25x, 1h write 2x.
// Not modelled: Haiku's long-prompt tier (over 100K input tokens: $0.50 / $2.50) and older versions in the same family that price differently.
const PRICES = [
  ['opus', 4, 20, 0.20, 5, 8], ['sonnet', 2, 10, 0.10, 2.5, 4], ['haiku', 0.1, 0.5, 0.01, 0.125, 0.2], ['fable', 8, 40, 0.20, 10, 16]];
// model name -> price family; Fable and Mythos have their own row (until schema 6 they were priced as opus)
const FAM = col => `CASE WHEN ${col} LIKE '%fable%' OR ${col} LIKE '%mythos%' THEN 'fable' WHEN ${col} LIKE '%opus%' THEN 'opus'
    WHEN ${col} LIKE '%haiku%' THEN 'haiku' WHEN ${col} LIKE '%sonnet%' THEN 'sonnet' END`;
const COST = (i, o, cr, cw, h, p) => `(coalesce(${i},0) * ${p}.in_p + coalesce(${o},0) * ${p}.out_p + coalesce(${cr},0) * ${p}.cr_p
      + max(coalesce(${cw},0) - coalesce(${h},0), 0) * ${p}.cw5_p + coalesce(${h},0) * ${p}.cw1h_p)`;
const OPUS_IN = `(SELECT in_p FROM prices WHERE family = 'opus')`;
const RUN_AT = col => `coalesce(
    (SELECT e.run FROM events e WHERE e.session_id = c.session_id AND e.ts <= ${col} ORDER BY e.ts DESC, e.seq DESC LIMIT 1),
    (SELECT e.run FROM events e WHERE e.session_id = c.session_id ORDER BY e.ts ASC, e.seq ASC LIMIT 1))`;
const TX_VIEWS = `
-- (a) main-chat call -> run: the latest events row (launch/start/stop/outcome/lease...) of the same session at or before the call's ts;
-- a call before the session's first run event goes to the first run. Sessions with no run events have no rows here (inline sessions).
CREATE VIEW IF NOT EXISTS v_tx_call_run AS
  SELECT c.session_id, c.message_id, c.ts, ${RUN_AT('c.ts')} AS run FROM tx_calls c
  WHERE c.is_sidechain = 0 AND c.helper = 0 AND EXISTS (SELECT 1 FROM events e WHERE e.session_id = c.session_id);
-- (b) subagent agent_id -> attempt. Priority 1 stop.task_id = agentId, 2 start.tool_use_id = meta toolUseId, 3 "<run>#<row>.<n>" in the meta description.
-- One row per agent_id (a SendMessage continuation shares the agentId: all its calls go to the lowest-priority-number, then lowest-id attempt).
CREATE VIEW IF NOT EXISTS v_tx_agent_attempt AS
  WITH k AS (
    SELECT task_id AS agent_id, attempt, 1 AS p FROM events WHERE type = 'stop' AND task_id IS NOT NULL AND attempt IS NOT NULL
    UNION ALL
    SELECT a.agent_id, e.attempt, 2 FROM tx_agents a JOIN events e ON e.tool_use_id = a.tool_use_id AND e.type = 'start' AND e.attempt IS NOT NULL
    UNION ALL
    SELECT a.agent_id, a.attempt, 3 FROM tx_agents a WHERE a.attempt IS NOT NULL AND EXISTS (SELECT 1 FROM attempts t WHERE t.id = a.attempt)),
  r AS (SELECT agent_id, attempt, p, ROW_NUMBER() OVER (PARTITION BY agent_id ORDER BY p, attempt) AS rn FROM k)
  SELECT agent_id, attempt, p AS via FROM r WHERE rn = 1;
-- one row per call with its weight (family from the model name, unknown family = weight 0), helper sessions included (helper 1).
-- The adv_* columns weigh the advisor sub-calls at the advisor model's own prices; they are never part of tokens / usd / units.
CREATE VIEW IF NOT EXISTS v_tx_call_w_all AS
  SELECT c.session_id, c.message_id, c.agent_id, c.ts, c.model, c.effort, c.is_sidechain, c.fam AS family,
    c.inline_n, c.agent_n,
    c.input_tokens AS t_in, c.output_tokens AS t_out, c.cache_read_input_tokens AS t_cr, c.cache_creation_input_tokens AS t_cw,
    coalesce(c.cw_1h, 0) AS t_cw1h,
    coalesce(c.input_tokens,0) + coalesce(c.output_tokens,0) + coalesce(c.cache_read_input_tokens,0) + coalesce(c.cache_creation_input_tokens,0) AS tokens,
    coalesce(${COST('c.input_tokens', 'c.output_tokens', 'c.cache_read_input_tokens', 'c.cache_creation_input_tokens', 'c.cw_1h', 'p')} / 1e6, 0) AS usd,
    coalesce(${COST('c.input_tokens', 'c.output_tokens', 'c.cache_read_input_tokens', 'c.cache_creation_input_tokens', 'c.cw_1h', 'p')} / ${OPUS_IN}, 0) AS units,
    c.helper, c.source_ref, c.adv_n, c.adv_model, c.adv_fam AS adv_family,
    coalesce(c.adv_in,0) + coalesce(c.adv_out,0) + coalesce(c.adv_cr,0) + coalesce(c.adv_cw,0) AS adv_tokens,
    coalesce(${COST('c.adv_in', 'c.adv_out', 'c.adv_cr', 'c.adv_cw', 'c.adv_cw1h', 'ap')} / 1e6, 0) AS adv_usd,
    coalesce(${COST('c.adv_in', 'c.adv_out', 'c.adv_cr', 'c.adv_cw', 'c.adv_cw1h', 'ap')} / ${OPUS_IN}, 0) AS adv_units
  FROM (SELECT *, ${FAM('model')} AS fam, ${FAM('adv_model')} AS adv_fam FROM tx_calls) c
  LEFT JOIN prices p ON p.family = c.fam LEFT JOIN prices ap ON ap.family = c.adv_fam;
-- every view below reads this one (or filters helper itself): helper sessions are out of every total by default
CREATE VIEW IF NOT EXISTS v_tx_call_w AS SELECT * FROM v_tx_call_w_all WHERE helper = 0;
-- helper sessions on their own (schema 6), by folder and model; cw_5m / cw_1h split the cache writes by TTL
CREATE VIEW IF NOT EXISTS v_tx_helper AS
  SELECT f.dir, c.model, c.family, COUNT(*) AS calls, COUNT(DISTINCT c.session_id) AS sessions, SUM(c.tokens) AS tokens,
    SUM(c.t_cr) AS cr, SUM(max(coalesce(c.t_cw,0) - c.t_cw1h, 0)) AS cw_5m, SUM(c.t_cw1h) AS cw_1h, SUM(c.units) AS units, SUM(c.usd) AS usd
  FROM v_tx_call_w_all c LEFT JOIN tx_files f ON f.path = c.source_ref WHERE c.helper = 1 GROUP BY f.dir, c.model, c.family;
-- advisor sub-calls (schema 6) by the executor's family and the advisor model
CREATE VIEW IF NOT EXISTS v_tx_advisor AS
  SELECT family, adv_family, adv_model, COUNT(*) AS calls, SUM(adv_n) AS adv_calls, SUM(adv_tokens) AS adv_tokens, SUM(adv_units) AS adv_units, SUM(adv_usd) AS adv_usd
  FROM v_tx_call_w WHERE adv_n > 0 GROUP BY family, adv_family, adv_model;
-- every call with run / attempt / role. Main-chat: run by (a), attempt NULL, role 'main'. Subagent: attempt by (b), run and role from attempts,
-- role 'unattributed' when no attempt matches (Workflow agents, pre-v2 or unplanned launches).
CREATE VIEW IF NOT EXISTS v_tx_call_attr AS
  SELECT c.session_id, c.message_id, c.agent_id, c.ts, c.model, c.effort, c.is_sidechain, c.family, c.inline_n, c.agent_n, c.t_in, c.t_out, c.t_cr, c.t_cw, c.t_cw1h,
    c.tokens, c.usd, c.units,
    CASE c.is_sidechain WHEN 0 THEN NULL ELSE aa.attempt END AS attempt,
    CASE c.is_sidechain WHEN 0 THEN ${RUN_AT('c.ts')} ELSE a.run END AS run,
    CASE c.is_sidechain WHEN 0 THEN 'main' ELSE coalesce(a.role, 'unattributed') END AS role,
    c.adv_n, c.adv_tokens, c.adv_units
  FROM v_tx_call_w c
  LEFT JOIN v_tx_agent_attempt aa ON c.is_sidechain = 1 AND aa.agent_id = c.agent_id
  LEFT JOIN attempts a ON a.id = aa.attempt;
-- (c) per-run totals = main chat + subagents, raw tokens (in+out+cr+cw) and cost-weighted units
CREATE VIEW IF NOT EXISTS v_tx_run_totals AS
  SELECT run,
    SUM(is_sidechain = 0) AS main_calls, coalesce(SUM(CASE WHEN is_sidechain = 0 THEN tokens END), 0) AS main_tokens,
    coalesce(SUM(CASE WHEN is_sidechain = 0 THEN units END), 0) AS main_units,
    SUM(is_sidechain = 1) AS sub_calls, coalesce(SUM(CASE WHEN is_sidechain = 1 THEN tokens END), 0) AS sub_tokens,
    coalesce(SUM(CASE WHEN is_sidechain = 1 THEN units END), 0) AS sub_units,
    COUNT(*) AS calls, SUM(tokens) AS tokens, SUM(units) AS units, SUM(usd) AS usd, SUM(adv_units) AS adv_units
  FROM v_tx_call_attr WHERE run IS NOT NULL GROUP BY run;
-- per attempt: subagent calls only; ledger_tokens is attempts.tokens (a different measure, see 2-loader.md)
CREATE VIEW IF NOT EXISTS v_tx_attempt_totals AS
  SELECT t.attempt, a.run, a.row, a.role, a.model AS ledger_model, a.effort AS ledger_effort, a.tokens AS ledger_tokens,
    COUNT(*) AS calls, SUM(t.tokens) AS tokens, SUM(t.units) AS units, SUM(t.usd) AS usd, SUM(t.adv_tokens) AS adv_tokens, SUM(t.adv_units) AS adv_units
  FROM v_tx_call_attr t JOIN attempts a ON a.id = t.attempt WHERE t.attempt IS NOT NULL GROUP BY t.attempt;
-- (d) per model/effort (the call's own, exact) and per role
CREATE VIEW IF NOT EXISTS v_tx_by_model_effort AS
  SELECT family, model, effort, COUNT(*) AS calls, SUM(tokens) AS tokens, SUM(t_out) AS out_tokens, SUM(units) AS units, SUM(usd) AS usd
  FROM v_tx_call_w GROUP BY family, model, effort;
CREATE VIEW IF NOT EXISTS v_tx_by_role AS
  SELECT role, COUNT(*) AS calls, SUM(tokens) AS tokens, SUM(units) AS units, SUM(usd) AS usd FROM v_tx_call_attr GROUP BY role;
-- subagent spend no attempt claims (Workflow agents, pre-v2, non-orchestrated Explore/gsd agents), by agent type
CREATE VIEW IF NOT EXISTS v_tx_unattributed AS
  SELECT coalesce(g.agent_type, '?') AS agent_type, COUNT(*) AS calls, COUNT(DISTINCT t.agent_id) AS agents, SUM(t.tokens) AS tokens, SUM(t.units) AS units
  FROM v_tx_call_attr t LEFT JOIN tx_agents g ON g.session_id = t.session_id AND g.agent_id = t.agent_id
  WHERE t.is_sidechain = 1 AND t.attempt IS NULL GROUP BY 1;
-- per attempt model/effort from the ledger step (what was asked for) next to what ran
CREATE VIEW IF NOT EXISTS v_tx_by_step AS
  SELECT ledger_model AS model, ledger_effort AS effort, role, COUNT(*) AS attempts, SUM(calls) AS calls, SUM(tokens) AS tokens, SUM(units) AS units
  FROM v_tx_attempt_totals GROUP BY ledger_model, ledger_effort, role;
-- sessions: per-file counters summed per session + call totals; kind as orch-eval 2-analyze.js:15
CREATE VIEW IF NOT EXISTS v_tx_sessions AS
  SELECT f.session_id, MAX(f.dir) AS dir, MIN(f.first_ts) AS first_ts, MAX(f.last_ts) AS last_ts,
    SUM(CASE WHEN f.kind = 'main' THEN f.active_ms ELSE 0 END) AS active_ms,
    SUM(f.human_turns) AS human_turns, SUM(f.agent_calls) AS agent_calls, SUM(f.orch_calls) AS orch_calls, SUM(f.compactions) AS compactions,
    SUM(f.kind <> 'main') AS n_subagents, MIN(CASE WHEN f.kind = 'main' THEN f.inline_v END) AS inline_v,
    CASE WHEN SUM(f.orch_calls) > 0 THEN 'orch' WHEN SUM(f.agent_calls) > 0 THEN 'agents-other' ELSE 'inline' END AS kind,
    (SELECT COUNT(DISTINCT s.sha) FROM tx_shas s JOIN tx_files g ON g.path = s.src_file WHERE g.session_id = f.session_id) AS n_shas
  FROM tx_files f WHERE f.kind IN ('main','sub','wf') AND f.helper = 0 GROUP BY f.session_id;
`;


// ---- source records -> table rows (§12.3) --------------------------------------------
const str = v => v == null ? null : typeof v === 'object' ? JSON.stringify(v) : String(v);
const int = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
const b01 = v => v === true ? 1 : v === false ? 0 : null;
const minS = (a, b) => b == null ? a : a == null || b < a ? b : a, maxS = (a, b) => b == null ? a : a == null || b > a ? b : a;
const ATT_COLS = ['id', 'id_derived', 'run', 'row', 'n', 'ts', 'updated', 'role', 'agent', 'model', 'model_id', 'effort', 'mode', 'tokens', 'tools', 'ms',
  'repo', 'area', 'first_pass', 'retries', 'escalated', 'is_codex', 'effort_known', 'src_file', 'src_line', 'source', 'source_ref', 'raw'];
const EV_COLS = ['id', 'id_derived', 'run', 'seq', 'type', 'ts', 'attempt', 'row', 'key', 'via', 'session_id', 'tool_use_id', 'task_id', 'agent',
  'model', 'model_id', 'effort', 'effort_observed', 'tokens', 'tools', 'ms', 'status', 'phase', 'final', 'action', 'bytes', 'src_file', 'src_line', 'source', 'source_ref', 'raw'];
const RUN_COLS = ['run', 'dir', 'has_ledger', 'has_manifest', 'n_attempts', 'n_events', 'first_ts', 'last_ts', 'source', 'source_ref', 'raw'];
// Upsert by the stable key; a row whose every column is unchanged is left alone (changes 0 = unchanged).
const upsertSql = (t, cols, pk) => `INSERT INTO ${t} (${cols.join(', ')}) VALUES (${cols.map(c => ':' + c).join(', ')})
  ON CONFLICT(${pk}) DO UPDATE SET ${cols.filter(c => c !== pk).map(c => `${c} = excluded.${c}`).join(', ')}
  WHERE ${cols.filter(c => c !== pk).map(c => `${t}.${c} IS NOT excluded.${c}`).join(' OR ')}`;

function readLines(file) { // -> [{line, text}] for non-empty lines, or null when the file is missing
  let txt; try { txt = fs.readFileSync(file, 'utf8'); } catch { return null; }
  const out = []; txt.split('\n').forEach((t, i) => { if (t.trim()) out.push({ line: i + 1, text: t }); });
  return out;
}
const parse = t => { try { const x = JSON.parse(t); return x && typeof x === 'object' && !Array.isArray(x) ? x : undefined; } catch { return undefined; } };

function attemptRow(x, dir, line, ordinal, lib) {
  const run = String(x.run), row = String(x.row), derived = !(typeof x.id === 'string' && x.id);
  const id = derived ? `${run}#${row.toLowerCase()}.${ordinal}` : x.id, m = /\.(\d+)$/.exec(id);
  const src = `${dir}/ledger.jsonl`;
  return { id, id_derived: derived ? 1 : 0, run, row, n: m ? +m[1] : null, ts: str(x.ts), updated: str(x.updated), role: str(x.role), agent: str(x.agent),
    model: str(x.model), model_id: str(x.model_id), effort: str(x.effort), mode: str(x.mode), tokens: int(x.tokens), tools: int(x.tools), ms: int(x.ms), repo: str(x.repo), area: str(x.area),
    first_pass: b01(x.firstPass), retries: int(x.retries), escalated: b01(x.escalated), is_codex: lib.isCodex(x) ? 1 : 0, effort_known: x.effort !== 'unknown' ? 1 : 0,
    src_file: src, src_line: line, source: 'ledger', source_ref: `${src}:${line}`, raw: JSON.stringify(x) };
}
function eventRow(x, dir, line) {
  const derived = !(typeof x.id === 'string' && x.id), src = `${dir}/manifest.jsonl`;
  return { id: derived ? `${dir}#e${line}` : x.id, id_derived: derived ? 1 : 0, run: str(x.run) || dir, seq: Number.isInteger(x.seq) ? x.seq : line,
    type: String(x.type), ts: str(x.ts), attempt: str(x.attempt), row: str(x.row), key: str(x.key), via: str(x.via), session_id: str(x.session),
    tool_use_id: str(x.tool_use_id), task_id: str(x.task_id), agent: str(x.agent), model: str(x.model), model_id: str(x.model_id), effort: str(x.effort), effort_observed: str(x.effort_observed),
    tokens: int(x.tokens), tools: int(x.tools), ms: int(x.ms), status: str(x.status), phase: str(x.phase), final: b01(x.final), action: str(x.action),
    bytes: int(x.bytes) ?? int(x.result_bytes), src_file: src, src_line: line, source: 'manifest', source_ref: `${src}:${line}`, raw: JSON.stringify(x) };
}
// Read every runs/*/{ledger,manifest}.jsonl, in sorted folder order then line order (deterministic rowids, first line wins a dup id).
function collect(runsDir, lib) {
  let dirs = []; try { dirs = fs.readdirSync(runsDir, { withFileTypes: true }).filter(d => d.isDirectory()).map(d => d.name).sort((a, b) => a < b ? -1 : a > b ? 1 : 0); } catch {}
  const att = [], ev = [], runs = [], detail = {}, seenA = new Set(), seenE = new Set();
  const tot = { files: 0, lines: 0, bad_lines: 0, skipped_lines: 0, dup_ids: 0 };
  for (const dir of dirs) {
    const L = readLines(path.join(runsDir, dir, 'ledger.jsonl')), E = readLines(path.join(runsDir, dir, 'manifest.jsonl'));
    if (!L && !E) continue;
    const run = { run: dir, dir: path.join(runsDir, dir), has_ledger: L ? 1 : 0, has_manifest: E ? 1 : 0, n_attempts: 0, n_events: 0, first_ts: null, last_ts: null };
    const count = (file, lines) => { tot.files++; tot.lines += lines.length; return (detail[`${dir}/${file}`] = { lines: lines.length, bad: 0, skipped: 0, dup: 0 }); };
    if (L) {
      const d = count('ledger.jsonl', L), ord = new Map();
      for (const { line, text } of L) {
        const x = parse(text); if (x === undefined) { d.bad++; continue; }
        if (x.run == null || x.row == null) { d.skipped++; continue; }
        const k = `${x.run}#${String(x.row).toLowerCase()}`; ord.set(k, (ord.get(k) || 0) + 1); // the ordinal counts every readable line of the row
        const r = attemptRow(x, dir, line, ord.get(k), lib);
        if (seenA.has(r.id)) { d.dup++; continue; }
        seenA.add(r.id); att.push(r); run.n_attempts++;
        if (typeof r.ts === 'string') { run.first_ts = minS(run.first_ts, r.ts); run.last_ts = maxS(run.last_ts, r.ts); }
      }
    }
    if (E) {
      const d = count('manifest.jsonl', E);
      for (const { line, text } of E) {
        const x = parse(text); if (x === undefined) { d.bad++; continue; }
        if (x.type == null || x.type === '') { d.skipped++; continue; }
        const r = eventRow(x, dir, line);
        if (seenE.has(r.id)) { d.dup++; continue; }
        seenE.add(r.id); ev.push(r); run.n_events++;
        if (typeof r.ts === 'string') { run.first_ts = minS(run.first_ts, r.ts); run.last_ts = maxS(run.last_ts, r.ts); }
      }
    }
    run.source = 'fs'; run.source_ref = `${dir}/`; run.raw = JSON.stringify(run);
    runs.push(run);
  }
  for (const d of Object.values(detail)) { tot.bad_lines += d.bad; tot.skipped_lines += d.skipped; tot.dup_ids += d.dup; }
  return { att, ev, runs, detail, tot };
}

function tablesOf(db) { return db.prepare(`SELECT type, name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'`).all(); }
// Tables that hold transcript-derived data (and the price edits) outlive --rebuild and every schema bump: Claude Code deletes transcripts after
// cleanupPeriodDays, so metrics.db is the only record of them. Only the ledger/manifest-derived tables and all views are dropped and reloaded.
const keepTable = n => /^tx_/.test(n) || n === 'wfp_telemetry' || n === 'prices';
function dropDerived(db) {
  const all = tablesOf(db);
  for (const t of all.filter(t => t.type === 'view')) db.exec(`DROP VIEW IF EXISTS "${t.name.replace(/"/g, '""')}"`);
  for (const t of all.filter(t => t.type === 'table' && !keepTable(t.name))) db.exec(`DROP TABLE IF EXISTS "${t.name.replace(/"/g, '""')}"`);
}
// In-place migration of the kept tables: any column the current DDL declares and the live table lacks is added with ALTER TABLE ADD COLUMN
// (the declaration comes from a scratch :memory: db built from DDL). A NOT NULL column needs a DEFAULT; a new table is made by DDL itself.
function migrateKept(db) {
  const mem = new (sqlite().DatabaseSync)(':memory:'); const done = [];
  try {
    mem.exec(DDL);
    for (const t of tablesOf(db).filter(t => t.type === 'table' && keepTable(t.name))) {
      const want = mem.prepare(`PRAGMA table_info("${t.name}")`).all(); if (!want.length) continue;
      const have = new Set(db.prepare(`PRAGMA table_info("${t.name}")`).all().map(c => c.name));
      for (const c of want.filter(c => !have.has(c.name))) {
        if (c.pk) throw new Error(`metrics: cannot migrate ${t.name}.${c.name} in place (primary key)`);
        if (c.notnull && c.dflt_value == null) throw new Error(`metrics: cannot migrate ${t.name}.${c.name} in place (NOT NULL without DEFAULT)`);
        db.exec(`ALTER TABLE "${t.name}" ADD COLUMN "${c.name}" ${c.type}${c.notnull ? ' NOT NULL' : ''}${c.dflt_value != null ? ' DEFAULT ' + c.dflt_value : ''}`); done.push(`${t.name}.${c.name}`);
      }
    }
  } finally { mem.close(); }
  return done;
}
function createSchema(db) {
  migrateKept(db); db.exec(DDL); db.exec(VIEWS); db.exec(TX_VIEWS);
  db.prepare(`INSERT INTO schema_version (version, note) VALUES (?, ?) ON CONFLICT(version) DO NOTHING`)
    .run(SCHEMA_VERSION, 'orchestrator metrics.db, design 6-d4-design.md §12 (row 13b)');
  const s = db.prepare(upsertSql('sources', ['source', 'status', 'path_glob', 'key_desc', 'tbl'], 'source'));
  for (const [source, status, path_glob, key_desc, tbl] of SOURCES) s.run({ source, status, path_glob, key_desc, tbl });
  const pr = db.prepare(`INSERT OR IGNORE INTO prices (family, in_p, out_p, cr_p, cw5_p, cw1h_p) VALUES (?, ?, ?, ?, ?, ?)`); // seeded once; edits to the rows stay
  for (const r of PRICES) pr.run(...r);
}
function openDb(dbPath, readOnly) {
  const { DatabaseSync } = sqlite();
  if (!readOnly) fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  const db = new DatabaseSync(dbPath, readOnly ? { readOnly: true } : {});
  db.exec('PRAGMA busy_timeout = 5000');
  return db;
}

// ---- transcript loader (row 2) --------------------------------------------------------
const TX_SKIP_DIR = /tmp-octopus/;
// Helper sessions (schema 6): other plugins' headless sessions (claude-mem's observer, double-shot-latte). Loaded and tagged helper = 1 so
// their spend is visible (v_tx_helper), but every other view leaves them out; their main-file calls carry no inline_n / agent_n.
const HELPER_DIR = /observer-sessions|double-shot-latte/;
const COMMIT_CMD = /\bgit(?:\s+-[cC]\s+\S+)*\s+commit\b/; // 2-scan.js:33
const HEX_RE = /\b[0-9a-f]{7,40}\b/g; // `git commit -q` prints nothing; the command's tail (log -1, rev-parse) holds the hex
const SHA_RE = /\[(?:[\w./+-]+|detached HEAD)(?: \(root-commit\))? ([0-9a-f]{7,12})\] /g; // 2-scan.js:31
const ATTEMPT_RE = /([A-Za-z0-9][\w.-]*#\d+[a-z]?\.\d+)/; // "<run>#<row>.<n>" in a meta description
const ORCH_DESC = [/^\[(opus|sonnet|haiku|auto|fable)/i, /#\d+[a-z]?\.\d+/]; // 2-scan.js:47
const TX_CALL_COLS = ['session_id', 'message_id', 'agent_id', 'request_id', 'ts', 'model', 'effort', 'is_sidechain', 'input_tokens',
  'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens', 'cw_1h', 'inline_n', 'agent_n', 'source', 'source_ref',
  'helper', 'adv_n', 'adv_model', 'adv_in', 'adv_out', 'adv_cr', 'adv_cw', 'adv_cw1h'];
const ADV_COLS = ['adv_n', 'adv_in', 'adv_out', 'adv_cr', 'adv_cw', 'adv_cw1h'];
// usage.iterations (advisor tool): entries of type advisor_message are the advisor model's own sub-call, billed at its model's prices and
// NOT included in the top-level usage (which sums the executor's 'message' iterations). -> {adv_n, adv_model, adv_in, ...} or all null.
function advOf(u) {
  const it = Array.isArray(u && u.iterations) ? u.iterations.filter(x => x && x.type === 'advisor_message') : [];
  if (!it.length) return { adv_n: null, adv_model: null, adv_in: null, adv_out: null, adv_cr: null, adv_cw: null, adv_cw1h: null };
  const s = k => it.reduce((a, x) => a + (int(x[k]) ?? 0), 0);
  return { adv_n: it.length, adv_model: str((it.find(x => x.model) || {}).model), adv_in: s('input_tokens'), adv_out: s('output_tokens'),
    adv_cr: s('cache_read_input_tokens'), adv_cw: s('cache_creation_input_tokens'),
    adv_cw1h: it.reduce((a, x) => a + (int(x.cache_creation && x.cache_creation.ephemeral_1h_input_tokens) ?? 0), 0) };
}
const advMax = (a, b) => { // per field max of two adv_* sets (a message id repeats over several lines); returns the adv_* fields only
  const o = { adv_model: a.adv_model || b.adv_model || null };
  for (const k of ADV_COLS) o[k] = a[k] == null ? (b[k] ?? null) : b[k] == null ? a[k] : Math.max(a[k], b[k]);
  return o;
};
// Inline work = a main-chat tool_use that does the work itself (not Agent/Task, not orchestration housekeeping like wfp/ledger/manifest calls,
// AskUserQuestion, SendMessage, memory/work-state MCP). Counted per API message so output tokens can be attributed (inline_n > 0 -> its output_tokens).
const INLINE_TOOLS = /^(Bash|Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit|WebFetch|WebSearch|mcp__chrome-devtools__.+)$/;
const HOUSEKEEPING_CMD = /(wfp\/wfp|orchestrator\/(ledger|manifest)|hooks\/orchestrator-guard)\.js\b/;
function toolKind(c) { // -> 'agent' | 'inline' | null
  if (c.name === 'Agent' || c.name === 'Task') return 'agent';
  if (!INLINE_TOOLS.test(c.name || '')) return null;
  if (c.name === 'Bash' && c.input && typeof c.input.command === 'string' && HOUSEKEEPING_CMD.test(c.input.command)) return null;
  return 'inline';
}
const id0 = (m, e) => m.id || e.uuid;
const FILE_COUNTERS = ['active_ms', 'human_turns', 'agent_calls', 'orch_calls', 'compactions'];

// Walk <projects>/<slug>/: <sid>.jsonl (main), <sid>/subagents/**.jsonl (sub; workflows one level deeper = wf). Sorted, so dedupe claims are deterministic.
function txWalk(root) {
  const out = [], ls = d => { try { return fs.readdirSync(d, { withFileTypes: true }).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0); } catch { return []; } };
  const walkSub = (dir, f) => { for (const x of ls(dir)) { const p = path.join(dir, x.name);
    if (x.isDirectory()) { if (x.name !== 'tool-results') walkSub(p, f); } else if (x.name.endsWith('.jsonl')) out.push(Object.assign({ path: p, kind: /[\\/]workflows[\\/]/.test(p) ? 'wf' : 'sub' }, f(x.name))); } };
  for (const slug of ls(root)) {
    if (!slug.isDirectory() || TX_SKIP_DIR.test(slug.name)) continue;
    const sd = path.join(root, slug.name), helper = HELPER_DIR.test(slug.name) ? 1 : 0;
    for (const x of ls(sd)) {
      if (x.isFile() && x.name.endsWith('.jsonl')) out.push({ path: path.join(sd, x.name), kind: 'main', dir: slug.name, session_id: x.name.slice(0, -6), agent_id: null, helper });
      else if (x.isDirectory()) { const sub = path.join(sd, x.name, 'subagents'); if (ls(sub).length) walkSub(sub, n => ({ dir: slug.name, session_id: x.name, agent_id: (/^agent-(.+)\.jsonl$/.exec(n) || [])[1] || null, helper })); }
    }
  }
  return out;
}


// Commit SHA harvest (2-scan.js:61-62): a Bash tool_use whose command is `git commit` (not --dry-run) puts its id in st.pending; the matching
// tool_result's text then yields every [branch sha] and every 7-40 hex word. Pending ids persist in tx_files.pending_tu across chunks and syncs.
function noteCommits(content, st) {
  for (const c of Array.isArray(content) ? content : []) {
    if (c && c.type === 'tool_use' && c.name === 'Bash' && c.id && c.input && typeof c.input.command === 'string' && COMMIT_CMD.test(c.input.command) && !/--dry-run/.test(c.input.command)) st.pending.add(c.id);
  }
}
function harvestResult(line, st, shas) { // line is a raw tool_result line; parses only when it names a pending id
  let hit = false; for (const id of st.pending) if (line.includes(id)) { hit = true; break; }
  if (!hit) return;
  let e; try { e = JSON.parse(line); } catch { return; }
  for (const c of (e && e.message && Array.isArray(e.message.content)) ? e.message.content : []) {
    if (!c || c.type !== 'tool_result' || !st.pending.has(c.tool_use_id)) continue;
    const t = typeof c.content === 'string' ? c.content : JSON.stringify(c.content || '');
    for (const m of t.matchAll(SHA_RE)) shas.add(m[1].slice(0, 7));
    for (const m of t.matchAll(HEX_RE)) shas.add(m[0].slice(0, 7));
    st.pending.delete(c.tool_use_id);
  }
}
// SHA-only pass over one chunk (backfill of files loaded before schema 3): no counters, no tx_calls.
function shaChunk(text, st, shas) {
  for (const line of text.split('\n')) {
    if (!line) continue;
    if (line.includes('"tool_result"')) { if (line.includes(' changed, ')) for (const m of line.matchAll(SHA_RE)) shas.add(m[1].slice(0, 7)); if (st.pending.size) harvestResult(line, st, shas); continue; }
    if (line.includes('"name":"Bash"') && line.includes('commit') && line.includes('"tool_use"')) { let e; try { e = JSON.parse(line); } catch { continue; } if (e && e.message) noteCommits(e.message.content, st); }
  }
}

// Parse one chunk of whole lines (text ends at a newline) -> deltas. Cheap prefilters run on the raw string before JSON.parse.
function txChunk(text, f, st) {
  const isMain = f.kind === 'main', calls = new Map(), edits = new Map(), shas = new Set(), seenTu = new Set();
  const d = { human_turns: 0, agent_calls: 0, orch_calls: 0, compactions: 0, active_ms: 0, bad: 0, first_ts: null, last_ts: st.last_ts, calls, edits, shas }, kinds = new Map();
  let lastMs = st.last_ts ? Date.parse(st.last_ts) : null;
  for (const line of text.split('\n')) {
    if (!line) continue;
    const isToolResult = line.includes('"tool_result"');
    if (isToolResult) { if (line.includes(' changed, ')) for (const m of line.matchAll(SHA_RE)) shas.add(m[1].slice(0, 7)); if (st.pending.size) harvestResult(line, st, shas); continue; }
    const hasUsage = line.includes('"usage"'), isUser = !hasUsage && line.includes('"type":"user"'), isSys = !hasUsage && !isUser && line.includes('"type":"system"') && /compact/i.test(line);
    if (!hasUsage && !isUser && !isSys) continue;
    let e; try { e = JSON.parse(line); } catch { d.bad++; continue; }
    if (!e || typeof e !== 'object') continue;
    if (typeof e.timestamp === 'string') {
      const ms = Date.parse(e.timestamp);
      if (Number.isFinite(ms)) { if (lastMs != null && ms - lastMs > 0 && ms - lastMs < 15 * 60e3) d.active_ms += ms - lastMs; lastMs = ms;
        d.first_ts = minS(d.first_ts, e.timestamp); d.last_ts = maxS(d.last_ts, e.timestamp); }
    }
    if (e.type === 'assistant' && e.message && e.message.usage && e.message.model !== '<synthetic>') {
      const m = e.message, u = m.usage, id = m.id || e.uuid;
      if (id) {
        const out = int(u.output_tokens) ?? 0, prev = calls.get(id), adv = prev ? advMax(advOf(u), prev) : advOf(u);
        if (!prev || out >= prev.output_tokens) calls.set(id, Object.assign({ session_id: f.session_id, message_id: id, agent_id: e.agentId || f.agent_id, request_id: str(e.requestId), ts: str(e.timestamp),
          model: str(m.model), effort: str(e.perTurnEffort || e.effort), is_sidechain: isMain ? 0 : 1, input_tokens: int(u.input_tokens) ?? 0,
          cache_creation_input_tokens: int(u.cache_creation_input_tokens) ?? 0, cache_read_input_tokens: int(u.cache_read_input_tokens) ?? 0, output_tokens: out,
          cw_1h: int(u.cache_creation && u.cache_creation.ephemeral_1h_input_tokens) ?? 0, source: 'transcript', source_ref: f.path, helper: f.helper ? 1 : 0 }, adv));
        else Object.assign(prev, adv);
      }
      for (const c of Array.isArray(m.content) ? m.content : []) {
        if (!c || c.type !== 'tool_use' || (c.id && seenTu.has(c.id))) continue;
        if (c.id) seenTu.add(c.id);
        if (c.name === 'Bash') noteCommits([c], st);
        if (isMain && id0(m, e)) { const k = toolKind(c); if (k) { const v = kinds.get(id0(m, e)) || { i: 0, a: 0 }; v[k === 'agent' ? 'a' : 'i']++; kinds.set(id0(m, e), v); } }
        if (isMain && (c.name === 'Agent' || c.name === 'Task')) { d.agent_calls++; const desc = (c.input && c.input.description) || ''; if (ORCH_DESC.some(r => r.test(desc))) d.orch_calls++; }
        if (/^(Edit|Write|MultiEdit)$/.test(c.name) && c.input && typeof c.input.file_path === 'string') edits.set(c.input.file_path, (edits.get(c.input.file_path) || 0) + 1);
      }
    } else if (isUser && isMain && e.message && !e.isMeta && !e.isCompactSummary) { // owner turns, 2-scan.js:53-60
      const mc = e.message.content;
      const txt = typeof mc === 'string' ? mc : Array.isArray(mc) && !mc.some(c => c && c.type === 'tool_result') ? mc.filter(c => c && c.type === 'text').map(c => c.text).join(' ') : null;
      if (txt && !/^<(local-command|command-message|system-reminder|task-notification)/.test(txt.trim()) && !txt.startsWith('Caveat:')) d.human_turns++;
    } else if (isSys && isMain && /compact/i.test(e.subtype || '')) d.compactions++;
  }
  for (const [id, r] of calls) { if (!isMain || f.helper) { r.inline_n = null; r.agent_n = null; continue; } const v = kinds.get(id); r.inline_n = v ? v.i : 0; r.agent_n = v ? v.a : 0; }
  return d;
}

// Read [offset, EOF) in chunks that end at a newline (byte-exact: the cut is found on the Buffer, a partial last line stays unread).
function txReadFile(f, st, onChunk) {
  const fd = fs.openSync(f.path, 'r');
  try {
    let pos = st.offset, want = 8 << 20;
    while (pos < f.size) {
      const len = Math.min(want, f.size - pos), buf = Buffer.allocUnsafe(len);
      let got = 0; while (got < len) { const n = fs.readSync(fd, buf, got, len - got, pos + got); if (!n) break; got += n; }
      const nl = buf.subarray(0, got).lastIndexOf(10);
      if (nl < 0) { if (pos + got < f.size && got === want) { want *= 2; continue; } break; } // a line longer than the chunk: grow; else a partial tail: stop
      onChunk(buf.toString('utf8', 0, nl + 1), nl + 1); pos += nl + 1; st.offset = pos;
    }
  } finally { fs.closeSync(fd); }
}

function loadMeta(f) { // subagents/agent-<id>.meta.json -> tx_agents row
  let m = {}; try { m = JSON.parse(fs.readFileSync(f.path.replace(/\.jsonl$/, '.meta.json'), 'utf8')); } catch {}
  const desc = str(m.description), a = desc && ATTEMPT_RE.exec(desc);
  return { session_id: f.session_id, agent_id: f.agent_id, agent_type: str(m.agentType), description: desc, tool_use_id: str(m.toolUseId),
    spawn_depth: int(m.spawnDepth), model: str(m.model), effort: str(m.effort), attempt: a ? a[1] : null, src_file: f.path };
}

// collectTx(db, opts) -> stats. Own short transactions (a batch of files each), run after the ledger/manifest transaction has committed.
function collectTx(db, opts = {}) {
  const t0 = Date.now(), started = new Date().toISOString(), root = opts.projectsDir || PROJECTS;
  const files = txWalk(root), S = { files_seen: files.length, files_read: 0, files_skipped: 0, files_reset: 0, bytes_read: 0, calls_upserted: 0, dup_ids: 0, bad_lines: 0, wfp_files: 0 };
  const known = new Map(db.prepare('SELECT * FROM tx_files').all().map(r => [r.path, r]));
  const q = {
    call: db.prepare(`INSERT INTO tx_calls (${TX_CALL_COLS.join(', ')}) VALUES (${TX_CALL_COLS.map(c => ':' + c).join(', ')})
      ON CONFLICT(session_id, message_id) DO UPDATE SET output_tokens = max(excluded.output_tokens, tx_calls.output_tokens),
        inline_n = CASE WHEN excluded.inline_n IS NULL THEN tx_calls.inline_n ELSE max(excluded.inline_n, coalesce(tx_calls.inline_n, 0)) END,
        agent_n = CASE WHEN excluded.agent_n IS NULL THEN tx_calls.agent_n ELSE max(excluded.agent_n, coalesce(tx_calls.agent_n, 0)) END,
        adv_model = coalesce(tx_calls.adv_model, excluded.adv_model),
        ${ADV_COLS.map(k => `${k} = CASE WHEN excluded.${k} IS NULL THEN tx_calls.${k} ELSE max(excluded.${k}, coalesce(tx_calls.${k}, 0)) END`).join(',\n        ')}
      WHERE excluded.output_tokens > tx_calls.output_tokens OR coalesce(excluded.inline_n, 0) > coalesce(tx_calls.inline_n, 0) OR coalesce(excluded.agent_n, 0) > coalesce(tx_calls.agent_n, 0)
        OR ${ADV_COLS.map(k => `coalesce(excluded.${k}, 0) > coalesce(tx_calls.${k}, 0)`).join(' OR ')}`),
    other: db.prepare('SELECT session_id FROM tx_calls WHERE message_id = ? LIMIT 1'),
    edit: db.prepare(`INSERT INTO tx_edits (src_file, file_path, n) VALUES (?, ?, ?) ON CONFLICT(src_file, file_path) DO UPDATE SET n = n + excluded.n`),
    sha: db.prepare('INSERT OR IGNORE INTO tx_shas (src_file, sha) VALUES (?, ?)'),
    agent: db.prepare(upsertSql('tx_agents', ['session_id', 'agent_id', 'agent_type', 'description', 'tool_use_id', 'spawn_depth', 'model', 'effort', 'attempt', 'src_file'], 'session_id, agent_id')),
    file: db.prepare(`INSERT INTO tx_files (path, kind, session_id, agent_id, dir, size, mtime_ms, offset, first_ts, last_ts, active_ms, human_turns, agent_calls, orch_calls, compactions, scanned_at, pending_tu, sha_v, inline_v, helper, adv_v)
      VALUES (:path, :kind, :session_id, :agent_id, :dir, :size, :mtime_ms, :offset, :first_ts, :last_ts, :active_ms, :human_turns, :agent_calls, :orch_calls, :compactions, :scanned_at, :pending_tu, :sha_v, :inline_v, :helper, :adv_v)
      ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime_ms = excluded.mtime_ms, offset = excluded.offset, first_ts = excluded.first_ts, last_ts = excluded.last_ts,
        active_ms = excluded.active_ms, human_turns = excluded.human_turns, agent_calls = excluded.agent_calls, orch_calls = excluded.orch_calls, compactions = excluded.compactions, scanned_at = excluded.scanned_at,
        pending_tu = excluded.pending_tu, inline_v = excluded.inline_v, helper = excluded.helper, adv_v = excluded.adv_v`),
  };
  let open = 0, bytes = 0;
  const begin = () => { if (!open) db.exec('BEGIN IMMEDIATE'); open++; };
  const commit = (force) => { if (open && (force || open >= 100 || bytes >= (32 << 20))) { db.exec('COMMIT'); open = 0; bytes = 0; } };
  try {
    for (const w of files) {
      let stt; try { stt = fs.statSync(w.path); } catch { continue; }
      const f = Object.assign({}, w, { size: stt.size, mtime_ms: Math.floor(stt.mtimeMs) }), prev = known.get(f.path);
      if (prev && prev.offset <= f.size && (prev.offset === f.size || (prev.size === f.size && prev.mtime_ms === f.mtime_ms))) { S.files_skipped++; continue; }
      begin();
      const st = { offset: 0, last_ts: null, first_ts: null, active_ms: 0, human_turns: 0, agent_calls: 0, orch_calls: 0, compactions: 0, pending: new Set() };
      if (prev && prev.offset <= f.size) { try { for (const id of JSON.parse(prev.pending_tu || '[]')) st.pending.add(id); } catch {} }
      if (prev && prev.offset <= f.size) Object.assign(st, { offset: prev.offset, last_ts: prev.last_ts, first_ts: prev.first_ts, active_ms: prev.active_ms,
        human_turns: prev.human_turns, agent_calls: prev.agent_calls, orch_calls: prev.orch_calls, compactions: prev.compactions });
      else if (prev) { // the file shrank (rewritten): drop what it contributed and reload it whole
        S.files_reset++; db.prepare('DELETE FROM tx_calls WHERE source_ref = ?').run(f.path); db.prepare('DELETE FROM tx_edits WHERE src_file = ?').run(f.path); db.prepare('DELETE FROM tx_shas WHERE src_file = ?').run(f.path);
      }
      const startOff = st.offset;
      txReadFile(f, st, (text) => {
        const d = txChunk(text, f, st);
        S.bad_lines += d.bad;
        for (const r of d.calls.values()) {
          const o = q.other.get(r.message_id);
          if (o && o.session_id !== r.session_id) { S.dup_ids++; continue; } // a forked / resumed copy of a call another session already holds
          if (Number(q.call.run(r).changes) > 0) S.calls_upserted++;
        }
        for (const [p, n] of d.edits) q.edit.run(f.path, p, n);
        for (const s of d.shas) q.sha.run(f.path, s);
        for (const k of FILE_COUNTERS) st[k] += d[k];
        st.first_ts = minS(st.first_ts, d.first_ts); st.last_ts = maxS(st.last_ts, d.last_ts);
      });
      S.bytes_read += st.offset - startOff; bytes += st.offset - startOff; S.files_read++;
      if (f.kind !== 'main' && f.agent_id) q.agent.run(loadMeta(f));
      q.file.run({ path: f.path, kind: f.kind, session_id: f.session_id, agent_id: f.agent_id, dir: f.dir, size: f.size, mtime_ms: f.mtime_ms, offset: st.offset,
        first_ts: st.first_ts, last_ts: st.last_ts, active_ms: st.active_ms, human_turns: st.human_turns, agent_calls: st.agent_calls, orch_calls: st.orch_calls,
        compactions: st.compactions, scanned_at: started, pending_tu: st.pending.size ? JSON.stringify([...st.pending].slice(-50)) : null, sha_v: 1,
        inline_v: (!prev || prev.offset > f.size) ? 1 : prev.inline_v, helper: f.helper ? 1 : 0, adv_v: (!prev || prev.offset > f.size) ? 1 : prev.adv_v });
      commit(false);
    }
    commit(true);
    S.wfp_files = collectWfp(db, opts, commit, begin);
    commit(true);
    S.sha_backfilled = shaBackfill(db, begin, commit); commit(true);
    S.inline_backfilled = inlineBackfill(db, begin, commit, opts.inlineBudgetMs ?? (+process.env.ORCH_INLINE_BACKFILL_MS || 2000)); commit(true);
    S.adv_backfilled = advBackfill(db, begin, commit, opts.advBudgetMs ?? (+process.env.ORCH_ADV_BACKFILL_MS || 2000)); commit(true);
    if (!opts.noGit) { begin(); Object.assign(S, collectGit(db, opts, started)); commit(true); }
    S.prev_sync = (db.prepare('SELECT finished_at FROM tx_sync_log ORDER BY id DESC LIMIT 1').get() || {}).finished_at || null;
    begin(); db.prepare(`INSERT INTO tx_sync_log (started_at, finished_at, ms, files_seen, files_read, files_skipped, files_reset, bytes_read, calls_upserted, dup_ids, bad_lines, wfp_files)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(started, new Date().toISOString(), Date.now() - t0, S.files_seen, S.files_read, S.files_skipped, S.files_reset, S.bytes_read,
      S.calls_upserted, S.dup_ids, S.bad_lines, S.wfp_files); commit(true);
  } catch (e) { if (open) { try { db.exec('ROLLBACK'); } catch {} } throw e; }
  S.ms = Date.now() - t0;
  for (const t of ['tx_calls', 'tx_files', 'tx_agents', 'wfp_telemetry', 'tx_git_commits', 'tx_shas']) S[t] = Number(db.prepare(`SELECT COUNT(*) AS n FROM ${t}`).get().n);
  return S;
}


// One-off (per file, sha_v 0 -> 1): files loaded before schema 3 never saw the `git commit -q` results. Re-read each file still on disk, SHAs only.
// A file already deleted by Claude Code keeps whatever SHAs it had; its calls and counters are untouched either way.
function shaBackfill(db, begin, commit) {
  const rows = db.prepare(`SELECT path, offset, size FROM tx_files WHERE kind != 'wfp_tel' AND sha_v < 1`).all();
  const ins = db.prepare('INSERT OR IGNORE INTO tx_shas (src_file, sha) VALUES (?, ?)'), done = db.prepare(`UPDATE tx_files SET sha_v = 1, pending_tu = CASE WHEN offset >= :size THEN :pend ELSE pending_tu END WHERE path = :path`);
  let n = 0;
  for (const r of rows) {
    let stt; try { stt = fs.statSync(r.path); } catch { continue; } // gone from disk: nothing to read; sha_v stays 0 (a restored copy would be scanned)
    begin(); const st = { offset: 0, pending: new Set() }, shas = new Set(), f = { path: r.path, size: stt.size };
    txReadFile(f, st, text => shaChunk(text, st, shas));
    for (const h of shas) ins.run(r.path, h);
    done.run({ path: r.path, size: st.offset, pend: st.pending.size ? JSON.stringify([...st.pending].slice(-50)) : null }); n++; commit(false);
  }
  return n;
}

// Incremental backfill (schema 4): main files loaded before inline_n existed are re-read once, newest first, until the time budget is spent
// (next sync continues). Counts are set absolutely over [0, tx_files.offset), so they equal what a fresh load would store. Lean pass: only
// tool_use lines are parsed. Sessions whose main file has inline_v 0 are left out of the inline-share comparison (drivers.js).
function inlineBackfill(db, begin, commit, budgetMs) {
  const t0 = Date.now(), rows = db.prepare(`SELECT path, offset FROM tx_files WHERE kind = 'main' AND inline_v < 1 ORDER BY last_ts DESC`).all();
  const zero = db.prepare('UPDATE tx_calls SET inline_n = 0, agent_n = 0 WHERE source_ref = ? AND is_sidechain = 0'), set = db.prepare('UPDATE tx_calls SET inline_n = ?, agent_n = ? WHERE source_ref = ? AND message_id = ? AND is_sidechain = 0'),
    done = db.prepare('UPDATE tx_files SET inline_v = 1 WHERE path = ?');
  let n = 0;
  for (const r of rows) {
    if (Date.now() - t0 > budgetMs) break;
    let stt; try { stt = fs.statSync(r.path); } catch { continue; }
    if (stt.size < r.offset) continue; // rewritten: the next load resets it
    begin(); const st = { offset: 0 }, kinds = new Map();
    txReadFile({ path: r.path, size: r.offset }, st, text => {
      for (const line of text.split('\n')) {
        if (!line.includes('"tool_use"') || !line.includes('"usage"')) continue;
        let e; try { e = JSON.parse(line); } catch { continue; }
        const m = e && e.type === 'assistant' && e.message; if (!m || !Array.isArray(m.content)) continue;
        const id = id0(m, e); if (!id) continue;
        const v = kinds.get(id) || { i: 0, a: 0, seen: new Set() };
        for (const c of m.content) { if (!c || c.type !== 'tool_use' || (c.id && v.seen.has(c.id))) continue; if (c.id) v.seen.add(c.id); const k = toolKind(c); if (k) v[k === 'agent' ? 'a' : 'i']++; }
        kinds.set(id, v);
      }
    });
    zero.run(r.path);
    for (const [id, v] of kinds) if (v.i || v.a) set.run(v.i, v.a, r.path, id);
    done.run(r.path); n++; commit(false);
  }
  return n;
}

// Incremental backfill (schema 6): files loaded before the adv_* columns existed are re-read once, newest first, within a time budget
// (env ORCH_ADV_BACKFILL_MS, default 2000; the next sync continues). A chunk without the word advisor_message is skipped unparsed.
function advBackfill(db, begin, commit, budgetMs) {
  const t0 = Date.now(), rows = db.prepare(`SELECT path, offset FROM tx_files WHERE kind IN ('main','sub','wf') AND adv_v < 1 ORDER BY last_ts DESC`).all();
  const set = db.prepare(`UPDATE tx_calls SET adv_n = :adv_n, adv_model = :adv_model, adv_in = :adv_in, adv_out = :adv_out, adv_cr = :adv_cr, adv_cw = :adv_cw, adv_cw1h = :adv_cw1h
    WHERE source_ref = :path AND message_id = :id`), done = db.prepare('UPDATE tx_files SET adv_v = 1 WHERE path = ?');
  let n = 0;
  for (const r of rows) {
    if (Date.now() - t0 > budgetMs) break;
    let stt; try { stt = fs.statSync(r.path); } catch { continue; } // gone from disk: its calls keep adv NULL
    if (stt.size < r.offset) continue; // rewritten: the next load resets it
    begin(); const st = { offset: 0 }, advs = new Map();
    txReadFile({ path: r.path, size: r.offset }, st, text => {
      if (!text.includes('advisor_message')) return;
      for (const line of text.split('\n')) {
        if (!line.includes('advisor_message') || !line.includes('"usage"')) continue;
        let e; try { e = JSON.parse(line); } catch { continue; }
        const m = e && e.type === 'assistant' && e.message; if (!m || !m.usage) continue;
        const id = m.id || e.uuid, a = advOf(m.usage); if (!id || a.adv_n == null) continue;
        advs.set(id, advs.has(id) ? advMax(a, advs.get(id)) : a);
      }
    });
    for (const [id, a] of advs) set.run(Object.assign({ path: r.path, id }, a));
    done.run(r.path); n++; commit(false);
  }
  return n;
}

// Commits from git log into tx_git_commits (sha7 PK, so worktrees and clones dedupe). Incremental: --since the last scan minus 3 days
// (committer date moves on rebase/amend); the first scan starts at the earliest transcript day. opts.gitFull rescans from the start.
// Repos come from config.json `metrics.commitRepos` (paths relative to $HOME, or absolute); empty means none are scanned.
// `metrics.commitAuthor` filters by author; null means no filter. Env overrides (tests): ORCH_COMMIT_REPOS (colon list), ORCH_COMMIT_AUTHOR, ORCH_COMMIT_HOME.
const commitConfig = () => { const m = paths.loadConfig('engine').cfg.metrics; return m && typeof m === 'object' ? m : {}; };
const commitRepos = cc => (Array.isArray(cc.commitRepos) ? cc.commitRepos : []).filter(x => typeof x === 'string' && x);
function collectGit(db, opts, started) {
  const t0 = Date.now(), home = opts.home || process.env.ORCH_COMMIT_HOME || process.env.HOME || process.env.USERPROFILE || '', cc = commitConfig();
  const author = process.env.ORCH_COMMIT_AUTHOR || (typeof cc.commitAuthor === 'string' && cc.commitAuthor ? cc.commitAuthor : null);
  const repoList = process.env.ORCH_COMMIT_REPOS ? process.env.ORCH_COMMIT_REPOS.split(':') : commitRepos(cc);
  const repos = repoList.filter(r => typeof r === 'string' && r && (home || path.isAbsolute(r))).map(r => path.resolve(home, r));
  const last = opts.gitFull ? null : (db.prepare(`SELECT value FROM tx_meta WHERE key = 'git_scanned_at'`).get() || {}).value;
  const first = (db.prepare(`SELECT min(first_ts) AS m FROM tx_files`).get() || {}).m;
  const since = last ? new Date(Date.parse(last) - 3 * 864e5).toISOString().slice(0, 10) : first ? new Date(Date.parse(first) - 864e5).toISOString().slice(0, 10) : '2026-09-01';
  const ins = db.prepare('INSERT OR IGNORE INTO tx_git_commits (sha, repo, t, subj) VALUES (?, ?, ?, ?)');
  let scanned = 0, added = 0;
  for (const r of repos) {
    if (!fs.existsSync(path.join(r, '.git'))) continue;
    let out = '';
    try { out = execFileSync('git', ['-C', r, 'log', '--all', `--since=${since}`, ...(author ? [`--author=${author}`] : []), '--format=%H|%aI|%s'], { encoding: 'utf8', maxBuffer: 64e6, timeout: 30000, stdio: ['ignore', 'pipe', 'ignore'], env: Object.assign({}, process.env, { GIT_OPTIONAL_LOCKS: '0' }) }); } catch { continue; }
    scanned++;
    for (const l of out.split('\n').filter(Boolean)) { const [h, t, ...sub] = l.split('|'); if (Number(ins.run(h.slice(0, 7), path.basename(r), t, sub.join('|')).changes) > 0) added++; }
  }
  db.prepare(`INSERT INTO tx_meta (key, value) VALUES ('git_scanned_at', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value`).run(started);
  return { git_repos: scanned, git_new: added, git_since: since, git_ms: Date.now() - t0 };
}

// Retention: Claude Code deletes transcripts older than cleanupPeriodDays (settings.json, default 30). Sessions with a file older than
// (cleanup - 5) days that is not fully loaded (no tx_files row, or offset < size) are at risk. Uses txWalk, so skipped dirs never count.
function readCleanupDays() {
  try { const n = Number(JSON.parse(fs.readFileSync(process.env.ORCH_SETTINGS || path.join(paths.configDir(), 'settings.json'), 'utf8')).cleanupPeriodDays); if (n > 0) return n; } catch {}
  return 30;
}
function retention(db, opts = {}) {
  const now = opts.now || Date.now(), cleanup = readCleanupDays(), warn = cleanup - 5, files = txWalk(opts.projectsDir || PROJECTS).filter(w => !w.helper); // helper sessions never count
  const known = new Map(db.prepare(`SELECT path, offset FROM tx_files WHERE kind != 'wfp_tel'`).all().map(r => [r.path, r.offset]));
  const sess = new Map(); let oldest = null;
  for (const w of files) {
    let st; try { st = fs.statSync(w.path); } catch { continue; }
    const age = (now - st.mtimeMs) / 864e5; if (age <= warn) continue;
    const s = sess.get(w.session_id) || { files: 0, loaded: true, oldest: 0 };
    s.files++; s.oldest = Math.max(s.oldest, age); if (!(known.has(w.path) && known.get(w.path) >= st.size)) s.loaded = false; sess.set(w.session_id, s);
    oldest = oldest == null ? age : Math.max(oldest, age);
  }
  const L = [...sess.values()], logs = db.prepare('SELECT finished_at FROM tx_sync_log ORDER BY id DESC LIMIT 2').all();
  const lastS = logs[0] ? logs[0].finished_at : null, prevS = logs[1] ? logs[1].finished_at : null, ago = t => t ? (now - Date.parse(t)) / 864e5 : null;
  return { cleanup_days: cleanup, warn_days: warn, sessions: L.length, files: L.reduce((a, s) => a + s.files, 0), loaded: L.filter(s => s.loaded).length, not_loaded: L.filter(s => !s.loaded).length,
    oldest_age_days: oldest, days_left: oldest == null ? null : Math.max(0, +(cleanup - oldest).toFixed(1)), last_sync: lastS, last_sync_age_days: ago(lastS), prev_sync: prevS, prev_sync_age_days: ago(prevS) };
}

// wfp_telemetry: <wfp state dir>/<sid>.tel.json is ONE overwritten snapshot per session (1-sources.md); loaded as is, (session_id, ts) keyed, so
// history only accumulates if sync runs between snapshots. Skipped by size/mtime like transcripts. Returns the files read.
function collectWfp(db, opts, commit, begin) {
  const dir = opts.wfpDir || WFP_STATE; let names = []; try { names = fs.readdirSync(dir).filter(n => n.endsWith('.tel.json')).sort(); } catch {}
  const known = new Map(db.prepare(`SELECT path, size, mtime_ms FROM tx_files WHERE kind = 'wfp_tel'`).all().map(r => [r.path, r]));
  const up = db.prepare(upsertSql('wfp_telemetry', ['session_id', 'ts', 'cwd', 'ctx_tokens', 'ctx_window', 'used_pct', 'model', 'effort', 'api_equiv_usd', 'five_hour_pct', 'seven_day_pct', 'tel_source', 'source', 'source_ref'], 'session_id, ts'));
  const fl = db.prepare(`INSERT INTO tx_files (path, kind, session_id, size, mtime_ms, offset) VALUES (?, 'wfp_tel', ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime_ms = excluded.mtime_ms, offset = excluded.offset`);
  let n = 0;
  for (const name of names) {
    const p = path.join(dir, name); let stt; try { stt = fs.statSync(p); } catch { continue; }
    const k = known.get(p), mt = Math.floor(stt.mtimeMs); if (k && k.size === stt.size && k.mtime_ms === mt) continue;
    let x; try { x = JSON.parse(fs.readFileSync(p, 'utf8')); } catch { continue; }
    if (!x || typeof x.ts !== 'number') continue;
    begin(); const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
    up.run({ session_id: name.slice(0, -9), ts: x.ts, cwd: str(x.cwd), ctx_tokens: num(x.ctxTokens), ctx_window: num(x.window), used_pct: num(x.usedPct), model: str(x.model), effort: str(x.effort),
      api_equiv_usd: num(x.apiEquivUsd), five_hour_pct: num(x.fiveHourPct), seven_day_pct: num(x.sevenDayPct), tel_source: str(x.source), source: 'wfp_tel', source_ref: p });
    fl.run(p, name.slice(0, -9), stt.size, mt, stt.size); n++; commit(false);
  }
  return n;
}

// sync({rebuild, tx, dbPath, runsDir, projectsDir, wfpDir}, lib) -> stats. tx:true also loads transcripts + wfp snapshots (ledger.js sync does; report does not). One transaction for the ledger/manifest part; any error rolls back and rethrows (the db is unchanged).
function sync(opts = {}, lib) {
  if (!lib || typeof lib.isCodex !== 'function') throw new Error('metrics.sync: lib.isCodex missing (call it through ledger.js sync)');
  const dbPath = opts.dbPath || DB_PATH, runsDir = opts.runsDir || RUNS, started = new Date().toISOString();
  const db = openDb(dbPath, false);
  try {
    db.exec('BEGIN IMMEDIATE');
    try {
      let mode = opts.rebuild ? 'rebuild' : 'sync';
      const names = new Set(tablesOf(db).map(t => t.name));
      if (mode === 'sync' && names.size) { // step 2: a different (or missing) schema_version drops and recreates everything
        const v = names.has('schema_version') ? db.prepare('SELECT max(version) AS v FROM schema_version').get().v : null;
        if (v !== SCHEMA_VERSION) mode = 'rebuild-auto';
      }
      if (mode !== 'sync') dropDerived(db);
      createSchema(db);
      const c = collect(runsDir, lib);
      let upserted = 0, unchanged = 0, deleted = 0;
      const load = (t, cols, pk, rows, where) => {
        const st = db.prepare(upsertSql(t, cols, pk)), seen = new Set();
        for (const r of rows) { seen.add(r[pk]); if (Number(st.run(r).changes) > 0) upserted++; else unchanged++; }
        const del = db.prepare(`DELETE FROM ${t} WHERE ${pk} = ?`); // step 5: rows from a deleted run folder or line
        for (const { k } of db.prepare(`SELECT ${pk} AS k FROM ${t} WHERE ${where}`).all()) if (!seen.has(k)) { del.run(k); deleted++; }
      };
      load('attempts', ATT_COLS, 'id', c.att, `source = 'ledger'`);
      load('events', EV_COLS, 'id', c.ev, `source = 'manifest'`);
      load('runs', RUN_COLS, 'run', c.runs, `source = 'fs'`);
      const stats = { mode, files: c.tot.files, lines: c.tot.lines, upserted, unchanged, deleted, bad_lines: c.tot.bad_lines,
        skipped_lines: c.tot.skipped_lines, dup_ids: c.tot.dup_ids, detail: c.detail };
      db.prepare(`INSERT INTO sync_log (started_at, finished_at, mode, files, lines, upserted, unchanged, deleted, bad_lines, skipped_lines, dup_ids, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(started, new Date().toISOString(), mode, stats.files, stats.lines, upserted, unchanged, deleted,
        stats.bad_lines, stats.skipped_lines, stats.dup_ids, JSON.stringify(c.detail));
      for (const t of ['attempts', 'events', 'runs']) stats[t] = Number(db.prepare(`SELECT COUNT(*) AS n FROM ${t}`).get().n);
      db.exec('COMMIT');
      if (opts.tx) { stats.tx = collectTx(db, opts); touchTxMarker(dbPath); } // after the ledger/manifest commit; its own short transactions; report never sets opts.tx
      return Object.assign(stats, { db: dbPath });
    } catch (e) { try { db.exec('ROLLBACK'); } catch {} throw e; }
  } finally { db.close(); }
}

// Staleness catch-up (2026-11-11): the weekly cron misses when WSL is off (11-01, 11-08), and Claude Code deletes transcripts after 30 days,
// so a reader that finds the last transcript sync older than staleDays() runs a transcript sync itself instead of trusting the db.
// txSyncAgeDays -> days since the last tx_sync_log row, or null (no db, unreadable, or never transcript-synced: nothing to catch up on).
function staleDays() { // env ORCH_SYNC_STALE_DAYS (default 2); 'off' or a non-number disables the catch-up
  const v = process.env.ORCH_SYNC_STALE_DAYS;
  if (v === undefined || v === '') return 2;
  const n = Number(v); return Number.isFinite(n) && n >= 0 ? n : Infinity;
}
function txSyncAgeDays(dbPath = DB_PATH, now = Date.now()) {
  if (!fs.existsSync(dbPath)) return null;
  let db; try { db = openDb(dbPath, true); } catch { return null; }
  try {
    const r = db.prepare('SELECT finished_at FROM tx_sync_log ORDER BY id DESC LIMIT 1').get();
    const t = r && r.finished_at ? Date.parse(r.finished_at) : NaN;
    return Number.isFinite(t) ? Math.max(0, (now - t) / 864e5) : null;
  } catch { return null; } finally { db.close(); }
}
// <db>.txsync: touched after every transcript sync, so the SessionStart hook hooks/stale-sync.js can read the transcript-sync age with one
// stat, without opening the db (no hook loads sqlite). Its mtime is the sync's finish time; a missing marker reads as stale there.
const txMarker = (dbPath = DB_PATH) => dbPath + '.txsync';
function touchTxMarker(dbPath) { try { fs.writeFileSync(txMarker(dbPath), new Date().toISOString() + '\n'); } catch {} }
const staleMsg = age => `ledger: note: the last transcript sync was ${age.toFixed(1)} days ago (the weekly cron missed, or WSL was off; limit ${staleDays()}).`;

// report(name, filters, lib, opts) -> {text, json, note?}. Syncs first (the writer), then queries a readOnly handle. Mirrors show (§12.5).
// When the last transcript sync is stale it syncs transcripts too (no git scan) and returns a note for stderr; opts.catchUp false skips that.
// opts.sync false (report --no-sync) reads metrics.db as it stands: no sync, no catch-up, nothing written, and the db must already exist.
const REPORTS = ['tokens-per-accepted', 'first-pass'];
function report(name, filters = {}, lib, opts = {}) {
  if (!REPORTS.includes(name)) throw new Error(`metrics.report: unknown report '${name}'`);
  const noSync = opts.sync === false, age = opts.catchUp === false ? null : txSyncAgeDays(opts.dbPath || DB_PATH);
  const catchUp = !noSync && age != null && age > staleDays();
  const st = noSync ? {} : sync({ dbPath: opts.dbPath, runsDir: opts.runsDir, tx: catchUp, noGit: true }, lib);
  const note = noSync ? (age != null && age > staleDays() ? `${staleMsg(age)} --no-sync reads it as it stands; drop --no-sync to catch up.` : null) : catchUp ? `${staleMsg(age)} Ran a transcript catch-up first: ${st.tx ? `${st.tx.files_read} files read, calls +${st.tx.calls_upserted}` : 'done'}.` : null;
  const p = { run: filters.run ?? null, role: filters.role ?? null, repo: filters.repo ?? null, area: filters.area ?? null };
  const db = openDb(opts.dbPath || DB_PATH, true);
  let roles, acc, fp, rk, unknown, matched;
  try {
    roles = db.prepare(BASE(true) + Q_ROLES).all(p);
    acc = db.prepare(BASE(true) + Q_STEPS_ACC).all(p);
    fp = db.prepare(BASE(true) + Q_FIRST_PASS).all(p);
    rk = name === 'first-pass' ? db.prepare(BASE(true) + Q_RETRY_KINDS).all(p) : [];
    const W = `(:run IS NULL OR run = :run) AND (:role IS NULL OR role = :role)
      AND (:repo IS NULL OR lower(coalesce(repo,'')) = :repo) AND (:area IS NULL OR lower(coalesce(area,'')) = :area)`;
    const cnt = db.prepare(`SELECT COUNT(*) AS n, SUM(effort_known = 0) AS u FROM attempts WHERE ${W}`).get(p);
    matched = Number(cnt.n); unknown = Number(cnt.u || 0);
  } finally { db.close(); }
  const { ROLES, MODELS, EFFORTS, SMALL, fmtTok, fmtMs, table } = lib;
  const rank = s => { const m = MODELS.findIndex(x => String(s.model).includes(x)); return (m < 0 ? -1 : m) * 10 + EFFORTS.indexOf(s.effort); };
  const roleIdx = r => ROLES.indexOf(r) + 1 || 99;
  const filt = [p.run && `run=${p.run}`, p.repo && `repo=${p.repo}`, p.area && `area=${p.area}`, p.role && `role=${p.role}`].filter(Boolean).join(' ') || 'all runs';
  const out = [`ledger: report ${name} (${filt}) from ${st.db}; synced: attempts ${st.attempts}, events ${st.events}, runs ${st.runs}, upserted ${st.upserted}, bad lines ${st.bad_lines}`];
  const json = { report: name, filters: p, catch_up: catchUp ? { age_days: +age.toFixed(2), calls_upserted: st.tx ? st.tx.calls_upserted : null } : null, sync: Object.assign({}, st, { detail: undefined, tx: undefined }), matched, unknown_effort: unknown, dup_ids: st.dup_ids, families: {} };
  if (!matched) { out.push(`ledger: no attempts match (${filt})`); return { text: out.join('\n'), json, note }; }
  if (unknown) out.push(`unknown effort: ${unknown} (left out of the stats and the ranking)`);
  if (st.dup_ids > 0) out.push(`dup ids: ${st.dup_ids} (report may differ from show)`);
  let anecdotal = false;
  for (const family of ['claude', 'codex']) {
    const rs = roles.filter(r => r.family === family).sort((a, b) => roleIdx(a.role) - roleIdx(b.role) || a.ord - b.ord);
    if (!rs.length) continue;
    out.push('', `== ${family} ==`);
    json.families[family] = [];
    for (const R of rs) {
      out.push('', `role ${R.role}: ${R.rows} rows, ${R.attempts} attempts, tokens ${fmtTok(R.tokens)}, accepted ${R.accepted}, tok/accepted ${R.accepted ? fmtTok(R.tok_per_accepted) : '-'}`);
      const steps = fp.filter(s => s.family === family && s.role === R.role).sort((a, b) => a.ord - b.ord);
      if (family === 'claude') steps.sort((a, b) => rank(b) - rank(a)); else steps.sort((a, b) => a.step.localeCompare(b.step)); // stable: ties keep first appearance
      const accOf = s => acc.find(a => a.family === family && a.role === R.role && a.step === s);
      const none = accOf('no outcome');
      const jr = { role: R.role, rows: R.rows, attempts: R.attempts, tokens: R.tokens, accepted: R.accepted, tok_per_accepted: R.tok_per_accepted ?? null, steps: [], no_outcome: none ? { rows: none.rows, tokens: none.tokens } : null };
      const body = [];
      for (const s of steps) {
        const a = accOf(s.step), rows = a ? a.rows : 0;
        if (s.anecdotal) anecdotal = true;
        jr.steps.push({ step: s.step, rows, tokens: a ? a.tokens : null, tok_per_accepted: a ? a.tok_per_accepted : null, attempts: s.attempts, outcomes: s.outcomes,
          first_pass: s.y, pct: s.pct ?? null, anecdotal: !!s.anecdotal, avg_tools: s.avg_tools ?? null, avg_ms: s.avg_ms ?? null });
        if (name === 'tokens-per-accepted') body.push([s.step, rows, fmtTok(rows ? a.tokens : null), rows ? fmtTok(a.tok_per_accepted) : '-']);
        else body.push([s.step, s.attempts, s.outcomes ? `${s.y}/${s.outcomes} ${s.pct}%${s.outcomes < SMALL ? '*' : ''}` : 'no outcomes',
          s.avg_tools ?? '-', fmtMs(s.avg_ms ?? null)]);
      }
      if (name === 'tokens-per-accepted') {
        if (none) body.push(['no outcome', none.rows, fmtTok(none.tokens), '-']);
        out.push(table(['model/effort', 'rows', 'tokens', 'tok/accepted'], body, [20, 5, 8, 12]));
      } else out.push(table(['model/effort', 'attempts', 'first-pass', 'avg tools', 'avg time'], body, [20, 8, 14, 9, 8]));
      json.families[family].push(jr);
    }
    if (name === 'first-pass') { // attempts by how they were made: first, continue (same builder resumed), fresh (new agent), unlabelled (a retry from before the field)
      const kinds = {}, cells = [];
      for (const k of RETRY_KINDS) {
        const x = rk.find(r => r.family === family && r.kind === k), n = x ? Number(x.attempts) : 0, oc = x ? Number(x.outcomes) : 0, y = x ? Number(x.y) : 0;
        kinds[k] = { attempts: n, outcomes: oc, first_pass: y };
        cells.push(`${k} ${n}${oc ? ` (${y}/${oc} first-pass)` : ''}`);
      }
      (json.retry_kinds = json.retry_kinds || {})[family] = kinds;
      out.push('', `retry kind (${family}): ${cells.join(', ')}`);
    }
  }
  if (name === 'first-pass' && anecdotal) out.push(`* fewer than ${SMALL} outcomes: anecdotal; cite it as such, don't generalize from it`);
  return { text: out.join('\n'), json, note };
}

// reseedPrices(dbPath, {dryRun}) -> {db, changes: [{family, col, old, new}], added: [family], dry_run}. The prices table is seeded once
// (INSERT OR IGNORE), so a PRICES edit reaches an existing db only through this: every family row that differs from PRICES is set to it,
// a missing family is added, equal rows are left alone (a second run changes nothing). A family in the db that PRICES lacks is kept.
const PRICE_COLS = ['in_p', 'out_p', 'cr_p', 'cw5_p', 'cw1h_p'];
function reseedPrices(dbPath = DB_PATH, opts = {}) {
  if (!fs.existsSync(dbPath)) throw new Error(`metrics: no db at ${dbPath}`);
  const db = openDb(dbPath, !!opts.dryRun), changes = [], added = [];
  try {
    const has = db.prepare(`SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'prices'`).get();
    const cur = new Map(has ? db.prepare('SELECT * FROM prices').all().map(r => [r.family, r]) : []);
    for (const [family, ...v] of PRICES) {
      const r = cur.get(family);
      if (!r) { added.push(family); for (let i = 0; i < PRICE_COLS.length; i++) changes.push({ family, col: PRICE_COLS[i], old: null, new: v[i] }); continue; }
      PRICE_COLS.forEach((c, i) => { if (Number(r[c]) !== v[i]) changes.push({ family, col: c, old: Number(r[c]), new: v[i] }); });
    }
    if (!opts.dryRun && changes.length) {
      if (!has) db.exec(`CREATE TABLE IF NOT EXISTS prices (family TEXT PRIMARY KEY, in_p REAL NOT NULL, out_p REAL NOT NULL, cr_p REAL NOT NULL, cw5_p REAL NOT NULL, cw1h_p REAL NOT NULL)`);
      const up = db.prepare(`INSERT INTO prices (family, in_p, out_p, cr_p, cw5_p, cw1h_p) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(family) DO UPDATE SET in_p = excluded.in_p, out_p = excluded.out_p, cr_p = excluded.cr_p, cw5_p = excluded.cw5_p, cw1h_p = excluded.cw1h_p`);
      const fams = new Set(changes.map(c => c.family));
      db.exec('BEGIN IMMEDIATE');
      try { for (const r of PRICES) if (fams.has(r[0])) up.run(...r); db.exec('COMMIT'); } catch (e) { try { db.exec('ROLLBACK'); } catch {} throw e; }
    }
  } finally { db.close(); }
  return { db: dbPath, changes, added, dry_run: !!opts.dryRun };
}

// CLI (not a ledger.js verb): node metrics.js reseed-prices [--dry-run] [--db <path>] [--json]. Default db: $ORCH_METRICS_DB / <data dir>/metrics.db.
function cli(argv) {
  const [verb, ...rest] = argv, o = { dryRun: false, json: false, db: DB_PATH };
  const usage = 'usage: metrics.js reseed-prices [--dry-run] [--db <path>] [--json]';
  if (verb !== 'reseed-prices') { console.error(usage); return 2; }
  for (let i = 0; i < rest.length; i++) {
    if (rest[i] === '--dry-run') o.dryRun = true; else if (rest[i] === '--json') o.json = true;
    else if (rest[i] === '--db' && rest[i + 1]) o.db = rest[++i]; else { console.error(usage); return 2; }
  }
  let r; try { r = reseedPrices(o.db, { dryRun: o.dryRun }); } catch (e) { console.error(String(e.message || e)); return 1; }
  if (o.json) { console.log(JSON.stringify(r)); return 0; }
  const by = new Map(); for (const c of r.changes) { if (!by.has(c.family)) by.set(c.family, []); by.get(c.family).push(c); }
  console.log(`metrics: reseed-prices ${r.dry_run ? '(dry run) ' : ''}${r.db}: ${by.size ? `${by.size} famil${by.size === 1 ? 'y' : 'ies'} ${r.dry_run ? 'would change' : 'changed'}` : 'no change, every family matches'}`);
  for (const [f, cs] of by) console.log(`  ${f}${r.added.includes(f) ? ' (new)' : ''}: ${cs.map(c => `${c.col} ${c.old == null ? '-' : c.old} -> ${c.new}`).join(', ')}`);
  return 0;
}
if (require.main === module) process.exitCode = cli(process.argv.slice(2));

module.exports = { txSyncAgeDays, staleDays, staleMsg, txMarker, SCHEMA_VERSION, DB_PATH, RUNS, PROJECTS, WFP_STATE, REPORTS, PRICES, commitRepos, commitConfig, sync, report, openDb, retention, reseedPrices };
