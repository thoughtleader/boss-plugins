#!/usr/bin/env node
// wfp: usage-aware workflow policy. Local, no model calls. Every hook path fails open (exit 0, no output).
// Facts (telemetry, counters) live here; judgment lives in CLAUDE.md. See REFERENCE.md.
// Presets: config (config.default.json + the user's <data>/wfp.json) profiles.<name>.roles.<role> = {model, effort} is the bar, the default AND the minimum. A missing model is
// filled from the bar; a launch at or above the bar passes silently; below the bar it passes with one advisory note (never a block).
// `ultracode on` (session) raises the bar one profile step (config ultracodeRaise). Legacy `roleModels` profiles/policies still work.
'use strict';
const fs = require('fs'), path = require('path'), cp = require('child_process');
const paths = require('../lib/paths.js'); // the one path/config resolver: state, handoffs and errors.log live under the data dir, never the plugin dir
const STATE = paths.wfpStateDir(), SELF = path.join(__dirname, 'wfp.js'), WFP = `node "${SELF}"`, WFP_CMD = 'node "$BOSS_LIB/../wfp/wfp.js"'; // WFP_CMD: the same command by env var (BOSS_LIB is exported at session start), for deny text that must not carry an absolute path. WFP: the command the deny/help texts tell the main chat to run
const now = () => Math.floor(Date.now() / 1000);
const sidOk = s => typeof s === 'string' && /^[A-Za-z0-9_-]{8,80}$/.test(s);
// A-18: an unreadable config still fails open, but is surfaced once per session. A bad overlay (<data>/wfp.json) falls back to the shipped
// config.default.json; only a broken default file leaves {} (built-in defaults).
const cfgRes = (() => { try { return paths.loadConfig('wfp'); } catch (e) { return { cfg: {}, error: String(e && e.message) }; } })();
const cfg = cfgRes.cfg && typeof cfgRes.cfg === 'object' ? cfgRes.cfg : {}, cfgErr = cfgRes.error ? String(cfgRes.error).slice(0, 240) : null;
const OVERLAY = path.join(paths.dataDir(), 'wfp.json');
const elog = line => { try { const f = paths.errorsLog(); fs.mkdirSync(path.dirname(f), { recursive: true }); fs.appendFileSync(f, `${new Date().toISOString()} ${line}\n`); } catch {} };
const C = (k, d) => (cfg[k] !== undefined ? cfg[k] : d);
// Every deny of a launch is one JSONL line in <data>/wfp/denials.jsonl ({ts, sid, tool, hook, key, agent, decision, reason}), so the owner can see which
// rule fires most. decision 'deny' = the launch was refused; 'loop-guard-pass' = the repeat limit let it through. Capped at 256 KB (older half dropped). Never throws.
const DENIALS = path.join(paths.dataDir(), 'wfp', 'denials.jsonl'), DENIALS_CAP = 262144;
function denyLog(i, key, type, decision, reason) {
  try {
    fs.mkdirSync(path.dirname(DENIALS), { recursive: true });
    try {
      if (fs.statSync(DENIALS).size > DENIALS_CAP) { const t = fs.readFileSync(DENIALS, 'utf8'), k = t.indexOf('\n', Math.floor(t.length / 2)); const tmp = `${DENIALS}.${process.pid}.tmp`; fs.writeFileSync(tmp, k >= 0 ? t.slice(k + 1) : ''); fs.renameSync(tmp, DENIALS); }
    } catch {}
    fs.appendFileSync(DENIALS, JSON.stringify({ ts: new Date().toISOString(), sid: i.session_id, tool: i.tool_name || 'Agent', hook: 'pretool', key, agent: type, decision, reason: String(reason).replace(/\s+/g, ' ').slice(0, 300) }) + '\n');
  } catch {}
}
const rd = f => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return null; } };
const wr = (f, o) => { const t = `${f}.${process.pid}.tmp`; fs.writeFileSync(t, JSON.stringify(o)); fs.renameSync(t, f); };
const sf = (sid, ext = '') => path.join(STATE, `${sid}${ext}.json`);
function update(sid, fn) { // lock + read-modify-write, bounded
  fs.mkdirSync(STATE, { recursive: true });
  const lock = sf(sid, '.lock');
  for (let i = 0; i < 10; i++) {
    try { fs.mkdirSync(lock); break; } catch {
      try { if (Date.now() - fs.statSync(lock).mtimeMs > 2000) fs.rmdirSync(lock); } catch {}
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25);
      if (i === 9) { // F-16: give up locking but still persist (atomic wr) and leave one errors.log line; the lock isn't ours, so it isn't removed
        const s = rd(sf(sid)) || {}, r = fn(s); wr(sf(sid), s);
        elog(`update lock exhausted, wrote without the lock sid=${String(sid).slice(0, 8)}`);
        return r;
      }
    }
  }
  try { const s = rd(sf(sid)) || {}; const r = fn(s); wr(sf(sid), s); return r; } finally { try { fs.rmdirSync(lock); } catch {} }
}
const out = o => process.stdout.write(JSON.stringify(o));
const ctxOut = (ev, text, sm) => out(Object.assign({ hookSpecificOutput: { hookEventName: ev, additionalContext: text } }, sm ? { systemMessage: sm } : {}));
function cfgNotice(s) { // A-18: once per session (state flag): one errors.log line + the text for a systemMessage
  if (!cfgErr || s.cfgWarned) return null;
  s.cfgWarned = now();
  elog(`config failed to parse (${cfgErr}); running on the shipped defaults`);
  return `[wfp] The wfp config failed to parse (${cfgErr}). wfp is running on the shipped defaults (or on built-ins when config.default.json itself is broken), so your overrides from ${OVERLAY} do not apply. Fix the file.`;
}
const once = (s, key, cool) => { s.warned = s.warned || {}; if (s.warned[key] && now() - s.warned[key] < cool) return false; s.warned[key] = now(); return true; };
const k = n => n == null ? 'unknown' : n >= 1000 ? Math.round(n / 1000) + 'k' : String(n);

// ---- presets: role -> bar {model, effort} ---------------------------------------
const ROLES = ['planning', 'plan-check', 'discovery', 'implementation', 'diagnosis', 'review', 'default'];
const MODELS = ['haiku', 'sonnet', 'opus'], EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
const mrank = m => { const x = String(m || '').toLowerCase().match(/opus|sonnet|haiku/); return x ? MODELS.indexOf(x[0]) + 1 : null; }; // unknown names (fable, inherit) stay unranked
const erank = e => { const i = EFFORTS.indexOf(String(e || '').toLowerCase()); return i < 0 ? null : i + 1; };
const stepTxt = (m, e) => m ? (e ? `${m}/${e}` : m) : '-';
const cmpStep = (a, b) => (mrank(a.model) - mrank(b.model)) || ((erank(a.effort) || 0) - (erank(b.effort) || 0));
const baseName = p => (p && typeof p.profile === 'string' && p.profile.split('+')[0]) || null; // 'quality+custom+custom' -> 'quality'
const raiseName = n => (n && (C('ultracodeRaise', {})[n] || n)) || null;
function rolesOf(pr) { // {role: {model, effort}} from a profile or policy in either shape: `roles` (current) or legacy `roleModels` (+ `effort`)
  const t = {}, put = (r, f, v) => { if (typeof v === 'string' && v) (t[r] = t[r] || { model: null, effort: null })[f] = v; };
  for (const [r, v] of Object.entries((pr && pr.roles) || {})) { put(r, 'model', v && v.model); put(r, 'effort', v && v.effort); }
  for (const [r, m] of Object.entries((pr && pr.roleModels) || {})) put(r, 'model', m);
  for (const [r, e] of Object.entries((pr && pr.effort) || {})) put(r, 'effort', e);
  return t;
}
function overridesOf(p) { // session `set role.X=` / `set effort.X=`. A legacy policy (no `roles`) holds a full roleModels copy of its preset:
  if (!p) return {};      // un-customized, that copy is stale and is dropped so the live preset applies; customized (`+custom`), it is kept as explicit.
  const P = cfg.profiles || {}, n = baseName(p), stale = !p.roles && !String(p.profile || '').includes('+') && !!(n && P[n]);
  return rolesOf({ roleModels: stale ? null : p.roleModels, effort: p.effort });
}
function barTable(p, ultra) { // effective role -> bar. The preset is read from config by name at use time, so config edits and the migration reach open sessions.
  const P = cfg.profiles || {}, n = baseName(p), use = ultra ? raiseName(n) : n;
  const t = use && P[use] ? rolesOf(P[use]) : rolesOf({ roles: p && p.roles }); // preset gone or unnamed: the policy's own copy
  for (const [r, v] of Object.entries(overridesOf(p))) { t[r] = Object.assign({ model: null, effort: null }, t[r]); if (v.model) t[r].model = v.model; if (v.effort) t[r].effort = v.effort; }
  return t;
}
function barOf(t, role) { const r = t[role] || {}, d = t.default || {}; return { model: r.model || d.model || null, effort: r.effort || (r.model ? null : d.effort) || null }; }
function belowOk(p, ultra, role) { // may rows of this role go below the bar (clearly mechanical rows only)? A preset without the list allows it (legacy).
  const P = cfg.profiles || {}, n = baseName(p), use = ultra ? raiseName(n) : n, l = (use && P[use] && P[use].belowBarAllowed) || (p && p.belowBarAllowed);
  return !Array.isArray(l) || l.includes(role);
}
function nextStep(x) { // escalation ladder: a stronger model at the same effort; at opus, one effort level up; null above opus/max
  const m = mrank(x.model), e = erank(x.effort) || 3;
  if (m == null) return null;
  if (m < MODELS.length) return { model: MODELS[m], effort: x.effort || 'high' };
  return e < EFFORTS.length ? { model: 'opus', effort: EFFORTS[e] } : null;
}
function barNote(s, p, ultra, role, type, model, effort, b) { // advisory only (additionalContext, never a permission decision); at or above the bar is silent
  if (p.override || role === 'default' || type === 'fork' || !model || !b.model) return null; // fork ignores the model; default = unclassified work, no bar to judge
  const m = mrank(model), bm = mrank(b.model); if (m == null || bm == null) return null; // unranked model names are never judged
  const e = erank(effort), be = erank(b.effort);
  if (!(m < bm || (m === bm && e != null && be != null && e < be))) return null; // a launch without effort is judged on its model only
  if ((s.barNotes || 0) >= C('barNoteMaxPerSession', 6) || !once(s, `bar:${role}:${stepTxt(model, effort)}`, C('warnCooldownSec', 1200))) return null;
  s.barNotes = (s.barNotes || 0) + 1;
  const n = (ultra ? raiseName(baseName(p)) : baseName(p)) || 'custom', bar = stepTxt(b.model, b.effort), got = stepTxt(model, effort);
  return belowOk(p, ultra, role)
    ? `[wfp] Advisory, not blocked: ${type} (${role}) launched at ${got}, below the ${n} bar ${bar}. Fine for a clearly mechanical row with a Why in the plan; otherwise stop it and relaunch at ${bar}.`
    : `[wfp] Advisory, not blocked: ${type} (${role}) launched at ${got}, below the ${n} bar ${bar}, and ${n} never allows going below the bar for ${role}. Stop it and relaunch at ${bar} unless the owner approved this.`;
}

function tel(sid) {
  const t = rd(sf(sid, '.tel'));
  if (!t) return null;
  t.age = now() - t.ts; t.fresh = t.age <= C('telemetryFreshSec', 300);
  return t;
}
function typeRole(rawType, desc) { // config typeRoles: exact agent type -> "role" or {role, byDescription: [{match, role}]}; null if unmapped. A plugin prefix (boss:orch-builder) is stripped first.
  const type = paths.agentBase(rawType), tr = C('typeRoles', {}), m = type && Object.prototype.hasOwnProperty.call(tr, type) ? tr[type] : null;
  if (typeof m === 'string') return m;
  if (!m || typeof m !== 'object') return null;
  for (const x of m.byDescription || []) { try { if (desc && new RegExp(x.match, 'i').test(String(desc))) return x.role; } catch {} }
  return typeof m.role === 'string' ? m.role : null;
}
function roleOf(type, desc) {
  const tr = typeRole(type, desc); if (tr) return tr; // exact agent type first: a description word can't reroute an orch-* row
  const hay = paths.agentBase(type).toLowerCase(); // A-08: an unmapped type is judged on its subagent_type only; byDescription is the one description route
  for (const r of C('roles', [])) { try { if (new RegExp(r.match, 'i').test(hay)) return r.role; } catch {} }
  return 'default';
}
function defaultPolicy() { // lasting default (config.json defaultProfile; null/missing = none): same shape as `profile <name>`
  const n = C('defaultProfile', null), pr = n && (cfg.profiles || {})[n];
  return pr ? Object.assign({ set: true, mode: 'delegate', override: false }, JSON.parse(JSON.stringify(pr)), { profile: n, fromDefault: true }) : null;
}
function policyLine(p, ultra) {
  if (!p || !p.set) return (C('defaultProfile', null) ? `no policy yet; default profile ${C('defaultProfile')} applies on first launch` : 'no workflow policy set this session') + (ultra ? ' (ultracode on: bar one step up)' : '');
  const t = barTable(p, ultra), bar = [...ROLES, ...Object.keys(t).filter(r => !ROLES.includes(r))].filter(r => t[r]).map(r => `${r}=${stepTxt(t[r].model, t[r].effort)}`).join(' ');
  return `mode=${p.mode} profile=${p.profile || 'custom'}${ultra ? ` ultracode=on(bar=${raiseName(baseName(p)) || 'custom'})` : ''} bar[${bar}] maxConcurrent=${p.maxConcurrent ?? '-'} maxLaunches=${p.maxLaunches ?? '-'} askBeforeOpus=${!!p.askBeforeOpus} override=${!!p.override}`;
}

// ---- hooks -------------------------------------------------------------------
function hTelemetry(i) { // statusline stdin JSON -> compact fact file
  if (!sidOk(i.session_id)) return;
  const cw = i.context_window || {}, u = cw.current_usage;
  const used = u ? (u.input_tokens || 0) + (u.cache_creation_input_tokens || 0) + (u.cache_read_input_tokens || 0) : null;
  const rl = i.rate_limits || {};
  fs.mkdirSync(STATE, { recursive: true });
  wr(sf(i.session_id, '.tel'), {
    ts: now(), cwd: (i.workspace && i.workspace.current_dir) || i.cwd || null,
    ctxTokens: used, window: cw.context_window_size || null, usedPct: cw.used_percentage ?? null,
    model: (i.model && (i.model.id || i.model.display_name)) || null, effort: (i.effort && i.effort.level) || null,
    apiEquivUsd: i.cost && i.cost.total_cost_usd != null ? i.cost.total_cost_usd : null,
    fiveHourPct: rl.five_hour ? rl.five_hour.used_percentage : null, sevenDayPct: rl.seven_day ? rl.seven_day.used_percentage : null
  });
}
const HARD_KEYS = ['main', 'launch', 'name']; // D8 + A-06: denies that ignore the loop guard
function hPretool(i) {
  const sid = i.session_id; if (!sidOk(sid) || i.agent_id) return; // main-agent launches only
  const ti = i.tool_input || {}, type = paths.agentBase(ti.subagent_type) || 'general-purpose'; // plugin agents arrive as boss:orch-*; every match below uses the base name
  let res = null, cfgMsg = null;
  update(sid, s => {
    cfgMsg = cfgNotice(s);
    if (!(s.policy && s.policy.set)) { const d = defaultPolicy(); if (d) { s.policy = d; s.preflightAsked = s.preflightAsked || now(); } } // lasting default: no preflight
    const p = s.policy || {}; s.launches = s.launches || 0; s.denyStreak = s.denyStreak || {};
    const streak = k => (s.denyStreak[k] = (s.denyStreak[k] || 0) + 1);
    const reset = () => { s.denyStreak = {}; };
    const Q = "Use AskUserQuestion (options first = recommended, labelled '(Recommended)') plus a second multiSelect question with two options: 'Make this my default for the rest of this conversation' and 'Also silence [wfp] warnings this session' (= override=true). ";
    // D8: main-only and the launch cap never give way (only the owner lifts them). model, conc and opus keep the loop guard: past
    // denyStreakLimit the launch goes through with a note naming the key (A-15) and one errors.log line.
    const passed = [];
    const deny = (key, why) => {
      const n = streak(key);
      if (!HARD_KEYS.includes(key) && n > C('denyStreakLimit', 2)) {
        passed.push(key);
        elog(`pretool loop-guard let the '${key}' deny pass (repeat ${n}, limit ${C('denyStreakLimit', 2)}) sid=${sid.slice(0, 8)} type=${type}`);
        denyLog(i, key, type, 'loop-guard-pass', why);
        return false;
      }
      denyLog(i, key, type, 'deny', why);
      res = { hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: why } }; return true;
    };
    const light = C('lightAgentTypes', []).includes(type);
    // 0. A-06: a named Agent call becomes a teammate in the main checkout (agent teams are enabled): denied unless the session policy has agentTeam=true
    if (typeof ti.name === 'string' && ti.name.trim() && !p.agentTeam) { if (deny('name', `[wfp] This Agent call has name=${JSON.stringify(ti.name.slice(0, 40))}, and a named call spawns a teammate in the main checkout (agent teams are on). Relaunch the same call without \`name\`. If the row is an approved agent-team row, the owner's word is needed: \`${WFP} set agentTeam=true --sid=$WFP_SID\`. This deny never gives way on retries.`)) return; }
    // 1. explicit main-only
    if (p.set && p.mode === 'main-only' && !p.override) { if (deny('main', `[wfp] Policy for this session is main-agent-only. Do this work in the main agent and do not retry the launch: this deny never gives way on retries. Only the owner lifts it; on their word run \`${WFP} set mode=delegate --sid=$WFP_SID\` (or \`set override=true\`).`)) return; }
    // 2. preflight: no policy yet and not a light agent
    if (!p.set && !p.override) {
      s.unpoliced = s.unpoliced || 0;
      s.unpoliced++;
      const needs = !light || s.unpoliced > C('preflightAfterUnpolicedLaunches', 3);
      if (needs && !s.preflightAsked) {
        s.preflightAsked = now();
        denyLog(i, 'preflight', type, 'deny', '[wfp] No delegation policy yet: ask the owner for a policy (AskUserQuestion), record it, relaunch.');
        res = { hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: `[wfp] No delegation policy yet. Ask ONE AskUserQuestion: Economy/Balanced/Quality/Max (recommended first, "(Recommended)" in its label), Customize, Main-agent-only; plus a multiSelect: make this my default for this conversation / silence [wfp] warnings this session (override=true). Put the preflight (stages, roles+models, effort, concurrency, planned launch total, tradeoffs) in the recommended option's preview. Never print it as text before the call; never ask as a plain-text question. Then record the answer with \`${WFP_CMD} profile <${Object.keys(cfg.profiles || {}).join('|')}>\` or \`... set k=v\`, and relaunch. Asked once; a repeat launch without a policy is allowed.` } };
        return;
      }
      if (!p.set) { s.launches++; return; } // asked already: proceed with caller's own model
    }
    const o = !!p.override;
    // 3. caps
    const active = activeCount(s);
    if (!o && p.maxLaunches && s.launches >= p.maxLaunches) { if (deny('launch', `[wfp] Launch cap reached (${s.launches}/${p.maxLaunches}). This deny never gives way on retries: do not retry. Finish in the main agent, or ask the owner with AskUserQuestion once (finish in main agent (Recommended) / raise the cap). The cap is 2 per row plus 2 spare at approval; an approved plan patch adds 2 per new row, and away mode may widen it for rows it adds, within the PROTOCOL.md step 1 limit, logged in DECISIONS.md. Only the owner lifts it beyond that; on their word run \`${WFP} set maxLaunches=N --sid=$WFP_SID\` (or \`set override=true\`).`)) return; }
    if (!o && p.maxConcurrent && active >= p.maxConcurrent) { if (deny('conc', `[wfp] ${active} agents already active (max ${p.maxConcurrent}). Wait for one to finish, then launch.`)) return; }
    // 4. model / effort. The preset bar is the default AND the minimum: a missing model is filled (deny-with-correction); at or above
    //    the bar passes silently (escalations, justified max rows); below the bar passes with one advisory note (barNote). No preset sets
    //    askBeforeOpus: only an explicit session `set askBeforeOpus=true` gates Opus (ask once, then cap).
    const role = roleOf(type, ti.description), ultra = !!s.ultracode, b = barOf(barTable(p, ultra), role), want = b.model;
    const upd = Object.assign({}, ti), req = ti.model || null; let changed = false, final = req;
    if (want) {
      if (!req) { upd.model = want; final = want; changed = true; }
      else if (p.askBeforeOpus && !p.opusApproved && mrank(req) === 3 && mrank(want) != null && mrank(want) < 3) {
        if (!p.opusOnce && !s.opusAsked) {
          s.opusAsked = now();
          if (deny('opus', `[wfp] Policy: ask before Opus. ${Q}Question: allow Opus for ${role} work? Options: Use ${want} (Recommended) / Allow Opus this once. Once: run \`${WFP} set opusOnce=true\` then relaunch with opus. Use ${want}: relaunch with model ${want}. If the default box is ticked: run \`set opusApproved=true\` (Opus chosen) or nothing (${want} chosen: capping stays the default).`)) return;
        }
        if (p.opusOnce) { p.opusOnce = false; s.opusAsked = 0; }
        else { upd.model = want; final = want; changed = true; }
      }
    }
    if (p.effort && p.effort[role] && !ti.effort) { upd.effort = p.effort[role]; changed = true; } // explicit session `set effort.<role>=` stays enforced
    // F-09: a launch with no effort gets the bar effort. A missing model is filled above (changed already); an explicit model at or below the bar
    // (a ranked name) is corrected for effort alone; above the bar or unranked, no effort is left to the session default (never forced down).
    // The `[model/effort]` label at the start of the description (the orchestrator's own launch label, same shape as manifest.js LABEL_RE) is the
    // caller's stated effort: when it names the model that will run and ranks above the bar's effort, it is kept instead of the bar's. Never
    // below the bar, never for another model, and only where the bar would have filled an effort anyway (an above-bar model is left alone).
    let fromLabel = false;
    if (!upd.effort && b.effort && type !== 'fork' && (changed || (mrank(req) != null && mrank(want) != null && mrank(req) <= mrank(want)))) {
      const lb = /^\s*\[([a-z0-9.-]+)\/([a-z]+)\]/i.exec(String(ti.description == null ? '' : ti.description));
      if (lb && mrank(lb[1]) != null && mrank(lb[1]) === mrank(final) && erank(lb[2]) != null && erank(lb[2]) > (erank(b.effort) || 0)) { upd.effort = lb[2].toLowerCase(); fromLabel = true; }
      else upd.effort = b.effort;
      changed = true;
    }
    const lblNote = fromLabel ? ' (effort kept from the description label)' : '';
    let rewrite = false;
    if (changed && C('correctionMode', 'deny') === 'updatedInput') rewrite = true; // F-28: rewrite the call in place (all fields kept); 'deny' = deny-with-correction, the proven path
    else if (changed) { // deterministic deny-with-correction; relaunch with the right model passes
      const fix = [upd.model !== ti.model ? `model=${upd.model}` : '', upd.effort !== ti.effort ? `effort=${upd.effort}` : ''].filter(Boolean).join(' ');
      if (deny('model', `[wfp] Session policy for ${role} agents: relaunch this same Agent call with ${fix}${lblNote}. No need to ask the user.`)) return;
    }
    s.launches++; reset();
    const ran = changed && !rewrite ? req : final; // a correction suppressed by the streak guard launches as requested; a rewrite launches corrected
    s.pending = s.pending || []; s.pending.push({ type, role, req, final: ran || final, t: now(), tuid: i.tool_use_id || null, desc: String(ti.description || '').slice(0, 200) }); s.pending = s.pending.slice(-8);
    const notes = passed.map(x => `[wfp] Loop guard: the '${x}' deny was repeated past denyStreakLimit (${C('denyStreakLimit', 2)}), so this launch went through as requested. Check the launch and tell the owner if it matters.`);
    const note = barNote(s, p, ultra, role, type, ran, ti.effort, b); if (note) notes.push(note);
    if (rewrite) {
      const fix = [upd.model !== ti.model ? `model=${upd.model}` : '', upd.effort !== ti.effort ? `effort=${upd.effort}` : ''].filter(Boolean).join(' ');
      notes.unshift(`[wfp] Session policy for ${role} agents: this launch was rewritten to ${fix}${lblNote}.`);
      res = { hookSpecificOutput: { hookEventName: 'PreToolUse', updatedInput: upd, additionalContext: notes.join('\n') } }; // no permissionDecision: the normal permission flow still applies
    } else if (notes.length) res = { hookSpecificOutput: { hookEventName: 'PreToolUse', additionalContext: notes.join('\n') } };
  });
  if (cfgMsg) res = Object.assign(res || {}, { systemMessage: cfgMsg });
  if (res) out(res);
}
function activeCount(s) { // F-16: running agents, plus admitted launches (pending, under 60 s old) whose SubagentStart hasn't registered yet
  const fresh = Object.values(s.active || {}).filter(a => now() - a.t < C('activeAgentStaleSec', 1800)), pool = fresh.slice();
  let n = fresh.length;
  for (const p of (s.pending || [])) {
    if (p.started || now() - p.t >= 60) continue;
    const j = pool.findIndex(a => a.type === p.type && a.t >= p.t - 1); // a matching active entry is the same launch: counted once
    if (j >= 0) pool.splice(j, 1); else n++;
  }
  return n;
}
function hSubStart(i) {
  if (!sidOk(i.session_id) || !i.agent_id) return;
  update(i.session_id, s => { s.active = s.active || {}; s.active[i.agent_id] = { t: now(), type: paths.agentBase(i.agent_type) };
    const pend = (s.pending || []).find(x => !x.started && x.type === paths.agentBase(i.agent_type)); if (pend) pend.started = now(); // F-16: this admitted launch is now in `active`, so it no longer counts as pending
    for (const [id, a] of Object.entries(s.active)) if (now() - a.t > 2 * C('activeAgentStaleSec', 1800)) delete s.active[id]; });
}
function hSubStop(i) {
  if (!sidOk(i.session_id) || !i.agent_id) return;
  let eff = null;
  try { // bounded read of the subagent transcript to observe the effective model
    const fd = fs.openSync(i.agent_transcript_path, 'r'), b = Buffer.alloc(65536), n = fs.readSync(fd, b, 0, 65536, 0); fs.closeSync(fd);
    const m = b.toString('utf8', 0, n).match(/"model":"([^"]+)"/); eff = m ? m[1] : null;
  } catch {}
  let meta = null; // F-15: subagents/agent-<id>.meta.json sits next to the transcript (agentType, description, toolUseId, ...); bounded, fail-open
  try {
    const fd = fs.openSync(String(i.agent_transcript_path).replace(/\.jsonl$/, '') + '.meta.json', 'r'), b = Buffer.alloc(16384), n = fs.readSync(fd, b, 0, 16384, 0); fs.closeSync(fd);
    const m = JSON.parse(b.toString('utf8', 0, n)); if (m && typeof m === 'object') meta = m;
  } catch {}
  update(i.session_id, s => {
    const a = (s.active || {})[i.agent_id]; if (s.active) delete s.active[i.agent_id];
    // F-15 match order: tool_use_id, then description (200-char cut on both sides), then type only when exactly one same-type entry remains; else requested is unknown. The match is consumed.
    const list = s.pending = s.pending || [], desc = meta ? String(meta.description || '').slice(0, 200) : '';
    let k = -1;
    if (meta && meta.toolUseId) k = list.findIndex(x => x.tuid && x.tuid === meta.toolUseId);
    if (k < 0 && desc) k = list.findIndex(x => x.desc && x.desc === desc);
    let unknown = false;
    if (k < 0) { const c = list.map((x, j) => x.type === paths.agentBase(i.agent_type) ? j : -1).filter(j => j >= 0); if (c.length === 1) k = c[0]; else if (c.length > 1) unknown = true; }
    const pend = k >= 0 ? list.splice(k, 1)[0] : {};
    s.agents = s.agents || []; s.agents.push({ type: paths.agentBase(i.agent_type), requested: unknown ? 'unknown' : (pend.req || null), policyModel: pend.final || null, effective: eff, secs: a ? now() - a.t : null });
    s.agents = s.agents.slice(-12);
  });
}
function fromTranscript(sid, tp, cwd) { // statusline is not rendered in the VS Code extension: fall back to a bounded tail read
  try {
    const fd = fs.openSync(tp, 'r'), sz = fs.fstatSync(fd).size, n = Math.min(sz, 262144), b = Buffer.alloc(n);
    fs.readSync(fd, b, 0, n, sz - n); fs.closeSync(fd);
    const ls = b.toString('utf8').split('\n');
    for (let j = ls.length - 1; j >= 0; j--) {
      if (!ls[j].includes('"usage"')) continue;
      let o; try { o = JSON.parse(ls[j]); } catch { continue; }
      const m = o.message; if (!m || !m.usage || !m.model || m.model === '<synthetic>' || o.isSidechain) continue;
      const u = m.usage, used = (u.input_tokens || 0) + (u.cache_creation_input_tokens || 0) + (u.cache_read_input_tokens || 0);
      const old = rd(sf(sid, '.tel')) || {};
      const t = { ts: now(), source: 'transcript', cwd, ctxTokens: used, window: /\[1m\]|1m/i.test(m.model) ? 1000000 : (old.window || null), usedPct: null, model: m.model, effort: old.effort || null, apiEquivUsd: null, fiveHourPct: null, sevenDayPct: null };
      wr(sf(sid, '.tel'), t); return;
    }
  } catch {}
}
// ---- prompt parser (F-04) -------------------------------------------------------
// Only the owner's own words may change the session policy. Completion notices (<task-notification> blocks, which carry subagent
// text) are stripped first; quoted/code spans are masked with spaces (indices stay valid, words never glue into a new match);
// a setter match is skipped when it is negated (3 words before it), sits in a question, is a past-tense recap (count setters),
// or is scoped to a topic (override). The first match that survives every filter applies.
const ownerText = t => String(t || '').replace(/<task-notification\b[^>]*>[\s\S]*?(?:<\/task-notification>|$)/gi, ' ');
const blank = m => m.replace(/[^\n]/g, ' ');
function maskQuotes(t) {
  return t.replace(/```[\s\S]*?(?:```|$)/g, blank).replace(/`[^`\n]*`/g, blank)
    .replace(/"[^"]*"/g, blank).replace(/\u201c[^\u201d]*\u201d/g, blank).replace(/\u2018[^\u2019\n]*\u2019(?!\w)/g, blank)
    .replace(/(^|[\s(\[:,;])('[^'\n]+?')(?=$|[\s.,;:!?)\]])/g, (m, a, q) => a + blank(q));
}
const NEG = /(?:^| )(?:not|never|cannot|dont|\w*n['\u2019]t|no longer)(?= |$)/i; // within the 3 words before a match ("no" alone is not a negation)
const RECAP = /\b(?:last (?:week|time|night|month|run|session|plan|sprint)|yesterday|earlier|previously|before that|in the (?:old|previous|last|earlier|first) (?:plan|run|session|version|workflow)|used to|we (?:had|used|ran|did)|there were|it was|had)\b/i;
function firstHit(re, t, fam) {
  for (const m of t.matchAll(new RegExp(re.source, re.flags.includes('g') ? re.flags : re.flags + 'g'))) {
    const pre = t.slice(0, m.index), st = Math.max(pre.lastIndexOf('.'), pre.lastIndexOf('!'), pre.lastIndexOf('?'), pre.lastIndexOf('\n')) + 1;
    const ws = pre.slice(st).trim().split(/\s+/).filter(Boolean).slice(-3).map(w => w.replace(/[^\w'\u2019]/g, ''));
    if (NEG.test(ws.join(' '))) continue; // negated: not, never, cannot, n't, no longer
    const tail = t.slice(m.index + m[0].length), e = tail.search(/[.!?\n]/);
    if (e >= 0 && tail[e] === '?') continue; // interrogative: the sentence holding the match ends with '?'
    if (fam === 'count' && RECAP.test(pre.slice(st))) continue; // past-tense recap of a count
    if (fam === 'override' && /^\s+(?:about|regarding|re:|concerning)\b/i.test(tail)) continue; // scoped to a topic, not a session-wide lift
    return m;
  }
  return null;
}
function hPrompt(i) {
  const sid = i.session_id; if (!sidOk(sid)) return;
  const text = ownerText(i.prompt), q = maskQuotes(text), lines = [];
  let t = tel(sid);
  if ((!t || !t.fresh) && i.transcript_path) { fromTranscript(sid, i.transcript_path, i.cwd); t = tel(sid) || t; }
  if (i.effort && i.effort.level && t && !t.effort) t.effort = i.effort.level;
  update(sid, s => {
    // explicit natural-language choices that are unambiguous enough to apply deterministically (owner text only; see the F-04 parser above)
    const ch = [];
    const setp = (kv, label) => { s.policy = s.policy || defaultPolicy() || { set: true, mode: 'delegate', roleModels: {} }; s.policy.set = true; Object.assign(s.policy, kv); ch.push(label); };
    if (firstHit(/\b(no sub-?agents?|main[- ]agent only|use the main agent only|without (any )?sub-?agents?)\b/i, q, 'main')) setp({ mode: 'main-only' }, 'mode=main-only');
    let m;
    if ((m = firstHit(/\bmax(?:imum)?(?: of)? (\d+) agents?\b(?: at once| concurrent\w*)?/i, q, 'count'))) setp({ maxConcurrent: +m[1] }, `maxConcurrent=${m[1]}`);
    if ((m = firstHit(/\b(\d+) (?:total )?(?:agent )?launches\b/i, q, 'count'))) setp({ maxLaunches: +m[1] }, `maxLaunches=${m[1]}`);
    if (firstHit(/just finish this approved workflow|stop repeating (the same|that|this) warning|stop warning me/i, q, 'override')) setp({ override: true }, 'override=on (caps/warnings off for this session)');
    if (ch.length) lines.push(`[wfp] policy updated from your message: ${ch.join(', ')}.`);
    // reassess request: always answer with facts, no dedupe
    if (/taking too long|burning (usage|credits|tokens|through)|fresh chat|new chat|would a (fresh|new)|usage (is )?(high|burning)/i.test(text))
      lines.push(`[wfp] reassess requested. Measured: ${telLine(t)}. ${policyLine(s.policy, s.ultracode)}. Active/launched: ${Object.keys(s.active || {}).length}/${s.launches || 0}.`);
    // context checkpoint (advisory, deduped by level + cooldown + growth)
    const o = s.policy && s.policy.override;
    if (!o && t && t.fresh && t.ctxTokens != null) {
      const lvl = C('contextCheckpointsTokens', []).filter(x => t.ctxTokens >= x).length;
      const pct = t.window ? Math.round(100 * t.ctxTokens / t.window) : null;
      const hit = lvl > 0 || (pct != null && pct >= C('contextCheckpointPctOfWindow', 70));
      const lw = s.lastWarn || { lvl: 0, tok: 0, ts: 0 };
      const grew = t.ctxTokens - lw.tok >= C('warnMinGrowthTokens', 40000);
      if (hit && (lvl > lw.lvl || (grew && now() - lw.ts > C('warnCooldownSec', 1200)))) {
        s.lastWarn = { lvl, tok: t.ctxTokens, ts: now() };
        lines.push(`[wfp] context checkpoint: ${telLine(t)}. Advisory only; assess continue/compact/clear/separate chat per CLAUDE.md "Usage workflow" if the work is at a natural pause.`);
      }
    }
  });
  if (lines.length) ctxOut('UserPromptSubmit', lines.join('\n'));
}
function hSession(i) {
  const sid = i.session_id; if (!sidOk(sid)) return;
  // stale cleanup (bounded)
  try { const ttl = C('stateTtlDays', 7) * 86400e3; for (const f of fs.readdirSync(STATE)) { const p = path.join(STATE, f); if (Date.now() - fs.statSync(p).mtimeMs > ttl) fs.rmSync(p, { recursive: true, force: true }); } } catch {}
  // BOSS_LIB: the engine path, exported on every session start, resume and compact, so `$BOSS_LIB/...` commands resolve even when /boss:start never ran
  if (process.env.CLAUDE_ENV_FILE) { // idempotent: a line is appended only when the file's last `export NAME=` line is not already this one (the file grows once per value change, not once per compaction)
    try {
      const f = process.env.CLAUDE_ENV_FILE, have = (() => { try { return fs.readFileSync(f, 'utf8').split('\n'); } catch { return []; } })();
      const want = [`export WFP_SID=${sid}`, `export BOSS_LIB='${path.join(paths.pluginRoot, 'lib').replace(/'/g, `'\\''`)}'`];
      const add = want.filter(l => { const name = l.slice(0, l.indexOf('=') + 1), mine = have.filter(x => x.startsWith(name)); return mine[mine.length - 1] !== l; });
      if (add.length) fs.appendFileSync(f, add.join('\n') + '\n');
    } catch {}
  }
  let cm = null; if (cfgErr) update(sid, s => { cm = cfgNotice(s); });
  if (i.source === 'startup' || i.source === 'clear') { if (cm) out({ systemMessage: cm }); return; }
  const s = rd(sf(sid));
  if (s && s.policy && s.policy.set) ctxOut('SessionStart', `[wfp] session workflow policy (restored after ${i.source}): ${policyLine(s.policy, s.ultracode)}. launches=${s.launches || 0}.`, cm);
  else if (cm) out({ systemMessage: cm });
}
function telLine(t) {
  if (!t) return 'telemetry unknown';
  const pct = t.window && t.ctxTokens != null ? ` (${Math.round(100 * t.ctxTokens / t.window)}% of ${k(t.window)} window)` : '';
  const stale = t.fresh ? '' : ` [STALE ${Math.round(t.age / 60)}m old]`;
  return `context ${k(t.ctxTokens)}${pct}, model ${t.model || 'unknown'}, effort ${t.effort || 'unknown'}${stale}`;
}

// ---- CLI ---------------------------------------------------------------------
function resolveSid(argv, mutating) { // -> {sid} | {bad} | {ambiguous: [sid...]}. F-17: --sid is validated before any path is built; a mutating command won't guess among live sessions.
  const a = argv.find(x => x.startsWith('--sid='));
  if (a) return sidOk(a.slice(6)) ? { sid: a.slice(6) } : { bad: a.slice(6) };
  if (sidOk(process.env.WFP_SID)) return { sid: process.env.WFP_SID };
  let best = null; const fresh = []; // fallback: newest telemetry for this cwd
  try { for (const f of fs.readdirSync(STATE)) if (f.endsWith('.tel.json')) { const t = rd(path.join(STATE, f)); if (!t || t.cwd !== process.cwd()) continue; const id = f.replace('.tel.json', ''); if (!best || t.ts > best.ts) best = { ts: t.ts, sid: id }; if (now() - t.ts < 1800) fresh.push(id); } } catch {}
  if (mutating && fresh.length > 1) return { ambiguous: fresh };
  return { sid: best && best.sid };
}
function coerce(v) { return v === 'true' ? true : v === 'false' ? false : /^\d+$/.test(v) ? +v : v; }
const flag = (args, name) => { const a = args.find(x => x.startsWith(`--${name}=`)); return a ? a.slice(name.length + 3) : null; };
function activePolicy(sid) { // the session's policy, else the lasting default (what the hook would apply at the next launch)
  const s = (sid && rd(sf(sid))) || {};
  if (s.policy && s.policy.set) return { s, p: s.policy, src: 'this session' };
  const d = defaultPolicy(); return { s, p: d, src: d ? (sid ? 'lasting default, no session policy yet' : 'lasting default, session not resolved') : null };
}
function showProfile(args, sid) { // profile show [<name>] [--ultracode=on|off]: role -> bar now, and with ultracode one step up
  const P = cfg.profiles || {}, name = args.find(x => !x.startsWith('--')), uf = flag(args, 'ultracode');
  let { s, p, src } = activePolicy(sid);
  if (name) { if (!P[name]) return console.log(`wfp: unknown profile '${name}'; profiles: ${Object.keys(P).join(', ')}`); p = Object.assign(JSON.parse(JSON.stringify(P[name])), { set: true, profile: name }); src = 'config'; }
  if (!p) return console.log(`wfp: no session policy and no lasting default. Try: profile show <${Object.keys(P).join('|')}>`);
  const ultra = uf ? uf === 'on' : !!s.ultracode, n = baseName(p) || 'custom', up = raiseName(baseName(p)) || 'custom';
  const lo = barTable(p, false), hi = barTable(p, true), extra = [...new Set([...Object.keys(lo), ...Object.keys(hi)])].filter(r => !ROLES.includes(r));
  const pad = (x, w) => String(x).padEnd(w);
  const lines = [`wfp preset ${n} (${src}) · ultracode ${ultra ? 'on' : 'off'} · maxConcurrent ${p.maxConcurrent ?? '-'} · maxLaunches ${p.maxLaunches ?? '-'} · askBeforeOpus ${p.askBeforeOpus ? 'yes' : 'no'}`,
    pad('role', 16) + pad(`${n}${ultra ? '' : ' *'}`, 18) + pad(`ultracode: ${up}${ultra ? ' *' : ''}`, 23) + `below the bar (${ultra ? up : n})`];
  for (const r of [...ROLES, ...extra]) { const a = barOf(lo, r), c = barOf(hi, r); lines.push(pad(r, 16) + pad(stepTxt(a.model, a.effort), 18) + pad(stepTxt(c.model, c.effort), 23) + (belowOk(p, ultra, r) ? 'mechanical rows only' : 'never')); }
  const ov = Object.entries(overridesOf(p)).map(([r, v]) => `${r}=${stepTxt(v.model, v.effort)}`).join(' ');
  if (ov) lines.push(`session overrides (in both columns): ${ov}`);
  if (n === up) lines.push(`ultracode leaves ${n} unchanged (top preset).`);
  lines.push('* = active bar: the default AND the minimum per role. Above it is always allowed (give the Why); below it only for clearly mechanical rows (give the Why).');
  lines.push(`Failed row: \`${WFP} escalate <role> [<model>/<effort>]\` gives the one retry step; a second failure = blocked, take it to the owner.`);
  return console.log(lines.join('\n'));
}
function escalate(args, sid) { // escalate <role|agent-type> [<model>[/<effort>]] [--ultracode=on|off] [--json]
  const pos = args.filter(x => !x.startsWith('--')), json = args.includes('--json'), uf = flag(args, 'ultracode'), tr = C('typeRoles', {});
  let role = pos[0]; if (role && !ROLES.includes(role)) role = typeRole(role, '') || role; // an agent type maps to its base role (no description here)
  if (!role || !ROLES.includes(role)) return console.log(`wfp: usage: escalate <role> [<model>[/<effort>]] [--json]. Roles: ${ROLES.join(', ')}; agent types: ${Object.keys(tr).join(', ')}`);
  const { s, p } = activePolicy(sid);
  if (!p) return console.log('wfp: no session policy and no lasting default; set a profile first');
  const ultra = uf ? uf === 'on' : !!s.ultracode, n = (ultra ? raiseName(baseName(p)) : baseName(p)) || 'custom', bar = barOf(barTable(p, ultra), role);
  let from = bar;
  if (pos[1]) { const [m, e] = pos[1].split('/'); if (mrank(m) == null || (e && erank(e) == null)) return console.log(`wfp: cannot read '${pos[1]}'; use <${MODELS.join('|')}>[/<${EFFORTS.join('|')}>]`); from = { model: m, effort: e || bar.effort }; }
  if (mrank(from.model) == null) return console.log(`wfp: the ${role} bar model '${from.model}' is not ranked; pass the step that ran, e.g. escalate ${role} sonnet/high`);
  const below = mrank(bar.model) != null && cmpStep(from, bar) < 0, to = below ? bar : nextStep(from);
  if (json) return console.log(JSON.stringify({ role, preset: n, ultracode: ultra, bar, from, to, then: 'blocked' }));
  const head = `wfp escalate: ${role} failed at ${stepTxt(from.model, from.effort)} (${n} bar ${stepTxt(bar.model, bar.effort)})`;
  return console.log(to ? `${head} -> retry once at ${stepTxt(to.model, to.effort)}${below ? ' (back up to the bar)' : ''}. If that fails too, mark the row blocked and take it to the owner.`
    : `${head} -> no step above ${stepTxt(from.model, from.effort)}. Mark the row blocked and take it to the owner.`);
}
function cli(argv) {
  const cmd = argv[0] || 'status', names = Object.keys(cfg.profiles || {}).join('|'), a1 = argv.slice(1).filter(x => !x.startsWith('--'))[0];
  const mutating = ['set', 'ultracode', 'reset', 'handoff'].includes(cmd) || (cmd === 'profile' && a1 !== 'show');
  const rs = resolveSid(argv, mutating), sid = rs.sid;
  if (cfgErr) console.error(`wfp: warning: the wfp config failed to parse (${cfgErr}); running on the shipped defaults.`);
  if (cmd === 'help') return console.log(`wfp status | profile <${names}> (this session) | profile show [<name>] [--ultracode=on|off] (role -> bar table) | default [<profile>|none] (lasting default for new sessions, defaultProfile in the data dir's wfp.json) | ultracode on|off (mirror of the owner's ultracode: bar one step up) | escalate <role> [<model>/<effort>] [--json] (retry step for a failed row) | set k=v ... (mode, maxConcurrent, maxLaunches, askBeforeOpus, opusApproved, override, agentTeam, role.<role>=<model>, effort.<role>=<lvl>) | reset | handoff | json`);
  if (cmd === 'default') { // view/change the lasting default: read-modify-write of <data>/wfp.json only (the plugin dir is never written)
    const v = argv[1], cur = C('defaultProfile', null);
    if (!v) return console.log(`wfp: default profile ${cur || 'none'} (profiles: ${Object.keys(cfg.profiles || {}).join(', ')}, none)`);
    if (v !== 'none' && !(cfg.profiles || {})[v]) return console.log('wfp: unknown profile; use one of ' + Object.keys(cfg.profiles || {}).join(', ') + ', none');
    let cur_ = {};
    if (fs.existsSync(OVERLAY)) { // never clobber an overlay we cannot read: it holds the user's presets and policy
      try { cur_ = JSON.parse(fs.readFileSync(OVERLAY, 'utf8')); } catch (e) { return console.log(`wfp: ${OVERLAY} does not parse (${String(e && e.message).slice(0, 120)}); fix it first. Nothing changed.`); }
      if (!cur_ || typeof cur_ !== 'object' || Array.isArray(cur_)) return console.log(`wfp: ${OVERLAY} is not a JSON object; fix it first. Nothing changed.`);
    }
    cur_.defaultProfile = v === 'none' ? null : v;
    fs.mkdirSync(path.dirname(OVERLAY), { recursive: true });
    const tmp = `${OVERLAY}.${process.pid}.tmp`; fs.writeFileSync(tmp, JSON.stringify(cur_, null, 2) + '\n'); fs.renameSync(tmp, OVERLAY);
    return console.log(`wfp: default profile ${v === 'none' ? 'none (preflight asks again in new sessions)' : v + ' (applies to every session without its own policy)'}. Saved in ${OVERLAY}.`);
  }
  const args = argv.slice(1).filter(x => !x.startsWith('--sid='));
  if (cmd === 'profile' && args[0] === 'show') return showProfile(args.slice(1), sid); // read-only: falls back to the lasting default without a session
  if (cmd === 'escalate') return escalate(args, sid);
  if (rs.bad !== undefined) return console.log(`wfp: invalid --sid '${String(rs.bad).slice(0, 40)}': a session id is 8-80 characters of A-Z a-z 0-9 _ -. Nothing was changed.`);
  if (rs.ambiguous) return console.log(`wfp: refusing to guess the session: ${rs.ambiguous.length} sessions for this cwd have telemetry under 30 min old (${rs.ambiguous.map(x => x.slice(0, 8)).join(', ')}). Pass --sid=<id> (or set WFP_SID). Nothing was changed.`);
  if (!sid) return console.log('wfp: cannot resolve session (no WFP_SID, no telemetry for this cwd). Pass --sid=<id>.');
  if (cmd === 'status' || cmd === 'json') {
    const s = rd(sf(sid)) || {}, t = tel(sid);
    if (cmd === 'json') return console.log(JSON.stringify({ sid, tel: t, state: s }, null, 1));
    const lines = [`session ${sid.slice(0, 8)}`, `measured : ${telLine(t)}`,
      `usage    : 5h ${t && t.fiveHourPct != null ? t.fiveHourPct + '%' : 'unknown'} | 7d ${t && t.sevenDayPct != null ? t.sevenDayPct + '%' : 'unknown'} (subscription utilization, only if reported)`,
      `cost     : ${t && t.apiEquivUsd != null ? '$' + t.apiEquivUsd.toFixed(2) + ' API-equivalent (NOT your subscription bill)' : 'unknown'}`,
      `policy   : ${policyLine(s.policy, s.ultracode)}`, `agents   : active ${Object.keys(s.active || {}).length}, launches ${s.launches || 0}`];
    for (const a of (s.agents || []).slice(-5)) lines.push(`  - ${a.type}: requested ${a.requested || 'none'} -> policy ${a.policyModel || 'n/a'} -> effective ${a.effective || 'unknown'}${a.secs != null ? ` (${a.secs}s)` : ''}`);
    return console.log(lines.join('\n'));
  }
  if (cmd === 'profile') {
    const pr = (cfg.profiles || {})[args[0]]; if (!pr) return console.log('profiles: ' + Object.keys(cfg.profiles || {}).join(', ') + ' (or: profile show [<name>])');
    update(sid, s => { s.policy = Object.assign({ set: true, mode: 'delegate', override: false }, JSON.parse(JSON.stringify(pr)), { profile: args[0] }); s.preflightAsked = now(); s.launches = s.launches || 0; });
    return console.log(`wfp: profile ${args[0]} applied to this session only.`);
  }
  if (cmd === 'ultracode') { // session flag kept outside s.policy, so a profile switch doesn't drop it; `reset` clears it
    const v = args[0], ok = v === 'on' || v === 'off';
    if (ok) update(sid, s => { s.ultracode = v === 'on'; });
    const { s, p } = activePolicy(sid), n = baseName(p) || 'custom', on = !!s.ultracode;
    return console.log(`wfp: ultracode ${on ? 'on' : 'off'} for this session: ${on ? `bar raised one step (${n} -> ${raiseName(baseName(p)) || 'custom'})` : `bar = ${n}`}. This only mirrors the owner's ultracode switch for the presets; it does not turn ultracode on or off.${ok ? '' : ' Usage: ultracode on|off'}`);
  }
  if (cmd === 'set') {
    update(sid, s => {
      s.policy = s.policy || defaultPolicy() || { set: true, mode: 'delegate', roleModels: {} }; s.policy.set = true;
      { const pn = baseName(s.policy); if (!s.policy.roles && !String(s.policy.profile || '').includes('+') && pn && (cfg.profiles || {})[pn]) delete s.policy.roleModels; } // F-29: a stale legacy copy is dropped before '+custom' makes it look explicit
      s.policy.profile = s.policy.profile ? s.policy.profile + '+custom' : null;
      for (const kv of args) { const [key, ...r] = kv.split('='), v = coerce(r.join('=')); const m = key.match(/^(role|effort)\.([\w-]+)$/);
        if (key === 'ultracode') { s.ultracode = v === true || v === 'on'; continue; }
        if (m) { const f = m[1] === 'role' ? 'roleModels' : 'effort'; s.policy[f] = s.policy[f] || {}; s.policy[f][m[2]] = v; } else s.policy[key] = v; }
      s.preflightAsked = s.preflightAsked || now();
    });
    const st = rd(sf(sid)) || {};
    return console.log('wfp: ' + policyLine(st.policy, st.ultracode));
  }
  if (cmd === 'reset') { try { fs.rmSync(sf(sid)); } catch {} return console.log('wfp: session policy cleared.'); }
  if (cmd === 'handoff') {
    const g = c => { try { return cp.execSync(c, { encoding: 'utf8', timeout: 3000, stdio: ['ignore', 'pipe', 'ignore'] }).trim(); } catch { return 'unknown'; } };
    const s = rd(sf(sid)) || {}, t = tel(sid), fp = path.join(paths.wfpHandoffsDir(), `${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '')}-${sid.slice(0, 8)}.md`);
    fs.mkdirSync(path.dirname(fp), { recursive: true });
    fs.writeFileSync(fp, `# Handoff ${new Date().toISOString()}\n\n## Objective + completion criteria\nTODO\n\n## Environment (measured)\n- cwd: ${process.cwd()}\n- branch: ${g('git rev-parse --abbrev-ref HEAD')}\n- worktree: ${g('git rev-parse --show-toplevel')}\n- ${telLine(t)}\n\n## Policy (carry over only if still wanted)\n- ${policyLine(s.policy, s.ultracode)}\n\n## Changed files (git status --short)\n\`\`\`\n${g('git status --short | head -40')}\n\`\`\`\n\n## Decisions / constraints that must survive\nTODO\n\n## Validation done + results\nTODO\n\n## Outstanding issues\nTODO\n\n## Exact next action\nTODO\n`);
    return console.log(fp);
  }
  console.log('unknown command; try: wfp help');
}

// ---- entry -------------------------------------------------------------------
const mode = process.argv[2];
if (['telemetry', 'pretool', 'substart', 'substop', 'prompt', 'session'].includes(mode)) {
  let raw = ''; process.stdin.setEncoding('utf8'); process.stdin.on('data', d => { if (raw.length < 4e6) raw += d; });
  process.stdin.on('end', () => { try { const i = JSON.parse(raw || '{}'); ({ telemetry: hTelemetry, pretool: hPretool, substart: hSubStart, substop: hSubStop, prompt: hPrompt, session: hSession })[mode](i); } catch (e) { try { fs.appendFileSync(path.join(DIR, 'errors.log'), `${new Date().toISOString()} ${mode} ${String(e && e.message).slice(0, 200)}\n`); } catch {} } process.exit(0); });
} else cli(process.argv.slice(2));
