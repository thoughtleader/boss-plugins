// doctor.js: health checks for the orchestrator's state, loaded by `manifest.js doctor` (never by the hooks).
// run(opts, deps): opts = the parsed flags {sid?, check?, json?}; deps come from manifest.js so nothing is re-derived here:
// {ORCH_DIR, RUNS, LS (lease-store), releaseRun, leaseState, activityOf, scanAgents, planDone, frontmatter, fold, ROLES, DEFAULTS,
// RUN_OK, sid8, hhmm, fail}. Root = env ORCH_CLAUDE_ROOT (default paths.configDir()); settings = env ORCH_SETTINGS (default <root>/settings.json);
// plugin root = env ORCH_PLUGIN_ROOT (tests only; default paths.pluginRoot); wfp config = paths.loadConfig('wfp'); analytics lib = config analytics.lib or BOSS_ANALYTICS_LIB.
// CHECKS is one table {id, check(ctx) -> findings[], fix?(ctx, findings) -> lines[]}; a finding is {level: ok|info|warn|error, check,
// msg, fixable, fix?}. Every check is read-only: no lock, no write, no ingest; git runs with GIT_OPTIONAL_LOCKS=0. Only --fix <ids|all>
// writes, through each check's fix(): manifest's releaseRun(force=false, ownOnly=true), git worktree prune/remove (no --force),
// and rename into <root>/.trash/doctor-<UTC ts>/. No fix deletes a file itself or touches another session's lease.
// Requiring this file prints nothing.
'use strict';
const fs = require('fs'), path = require('path'), { spawnSync } = require('child_process');
const paths = require('./paths.js');

const LOCK_OLD_MS = 5 * 60 * 1000, OPEN_OLD_MIN = 24 * 60;
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
const LEVELS = ['ok', 'info', 'warn', 'error'];
const finding = (level, check, msg, extra = {}) => Object.assign({ level, check, msg, fixable: false }, extra);
const readTxt = f => { try { return fs.readFileSync(f, 'utf8'); } catch { return null; } };
const statOf = f => { try { return fs.lstatSync(f); } catch { return null; } };
const listDir = d => { try { return fs.readdirSync(d, { withFileTypes: true }); } catch { return null; } };
const ageMin = (now, ms) => Math.max(0, Math.floor((now - ms) / 60000));

// ---- leases: unreadable or misnamed files, own expired / own live on a done run (fixable), foreign (info), old lock dirs ----
function oldLocks(ctx, dir, label) { // *.lock dirs in dir older than 5 min -> info findings
  const out = [];
  for (const e of listDir(dir) || []) {
    if (!e.isDirectory() || !e.name.endsWith('.lock')) continue;
    const st = statOf(path.join(dir, e.name));
    if (st && ctx.now - st.mtimeMs > LOCK_OLD_MS) out.push(finding('info', 'leases', `lock dir ${label}${e.name} is ${ageMin(ctx.now, st.mtimeMs)}m old (a crashed writer; the next writer clears it after 5 s)`));
  }
  return out;
}
function leaseFinding(ctx, run, lease, legacy) {
  const { deps, sid, now } = ctx, live = deps.LS.isLive(lease, now), tag = legacy ? ' (legacy ACTIVE.lease)' : '';
  const when = live ? `live until ${deps.hhmm(lease.expires)}` : `expired ${deps.hhmm(lease.expires)}`;
  if (!sid) return finding('info', 'leases', `${run}: held by ${deps.sid8(lease.session)}, ${when}${tag}`);
  if (lease.session !== sid) return finding('info', 'leases', `${run}: foreign lease, holder ${deps.sid8(lease.session)}, ${when}${tag}; never touched`);
  if (!live) return act(finding('warn', 'leases', `${run}: own lease expired ${deps.hhmm(lease.expires)}${tag}`, { fixable: true, fix: '--fix leases' }), { run, legacy });
  if (deps.planDone(readTxt(path.join(ctx.RUNS, run, 'PLAN.md')))) return act(finding('warn', 'leases', `${run}: own live lease on a done run${tag}`, { fixable: true, fix: '--fix leases' }), { run, legacy });
  return finding('ok', 'leases', `${run}: own lease, ${when}${tag}`);
}
function checkLeases(ctx) {
  const { deps } = ctx, dir = path.join(ctx.ORCH_DIR, 'leases'), out = [];
  const ents = listDir(dir);
  for (const e of ents || []) {
    if (e.isDirectory() && e.name.endsWith('.lock')) continue; // oldLocks below
    if (e.name.includes('.tmp-')) continue; // a writer's tmp file (tmp + link/rename)
    if (!e.isFile() || !e.name.endsWith('.json')) { out.push(finding('warn', 'leases', `unexpected entry leases/${e.name}`)); continue; }
    const run = e.name.slice(0, -5);
    if (!deps.RUN_OK(run)) { out.push(finding('warn', 'leases', `misnamed lease file leases/${e.name} (not a run name)`)); continue; }
    const r = deps.LS.readRunLease(ctx.ORCH_DIR, run);
    if (!r || r.legacy) { out.push(finding('warn', 'leases', `unreadable lease file leases/${e.name} (bad JSON, bad session, or its run is not ${run}); reads as no lease`)); continue; }
    out.push(leaseFinding(ctx, run, r.lease, false));
  }
  for (const x of deps.LS.listLeases(ctx.ORCH_DIR)) if (x.legacy) out.push(leaseFinding(ctx, x.run, x.lease, true));
  out.push(...oldLocks(ctx, dir, 'leases/'), ...oldLocks(ctx, ctx.ORCH_DIR, ''));
  for (const e of listDir(ctx.RUNS) || []) if (e.isDirectory()) out.push(...oldLocks(ctx, path.join(ctx.RUNS, e.name), `runs/${e.name}/`));
  if (!out.length) out.push(finding('ok', 'leases', ents ? 'no leases' : 'no leases dir'));
  return out;
}

// ---- runs (info): folders without PLAN.md, old open attempts with no live lease --------------------------------------------
function checkRuns(ctx) {
  const { deps, sid, now } = ctx, out = [];
  const names = (listDir(ctx.RUNS) || []).filter(e => e.isDirectory() && deps.RUN_OK(e.name)).map(e => e.name).sort();
  for (const run of names) {
    const d = path.join(ctx.RUNS, run), files = (listDir(d) || []).map(e => e.name), has = f => files.includes(f);
    if (!has('PLAN.md') && !has('HANDOFF.md')) {
      const kind = has('BRIEF.md') ? 'brief-only' : (has('ledger.jsonl') || has('manifest.jsonl')) ? 'ledger/manifest only (tiny/small)'
        : files.length ? `empty (no brief, plan, ledger or manifest; ${files.length} other file(s))` : 'empty';
      out.push(finding('info', 'runs', `${run}: no PLAN.md: ${kind}`));
    }
    if (!has('manifest.jsonl')) continue;
    let F; try { F = deps.fold(run, { sid, now }); } catch (e) { out.push(finding('warn', 'runs', `${run}: manifest unreadable (${e && e.message})`)); continue; }
    const stale = F.attempts.filter(a => a.age_min != null && a.age_min > OPEN_OLD_MIN);
    if (!stale.length) continue;
    const L = deps.leaseState(sid, run, now).state;
    if (L !== 'none' && L !== 'expired') continue;
    const oldest = stale.reduce((a, b) => (b.age_min > a.age_min ? b : a)), done = deps.planDone(readTxt(path.join(d, 'PLAN.md')));
    out.push(finding('info', 'runs', `${run}: ${stale.length} open attempt(s) older than 24 h with no stop event (oldest ${oldest.id}, ${Math.floor(oldest.age_min / 60)}h), lease ${L}${done ? ', plan done' : ''}`,
      done ? {} : { fix: `/boss:resume ${run}` }));
  }
  if (!out.length) out.push(finding('ok', 'runs', `${names.length} run folder(s), nothing to report`));
  return out;
}

// ---- active: a legacy ACTIVE file is inert (leases replaced it); existence only, never its contents -----------------------
function checkActive(ctx) {
  if (!statOf(path.join(ctx.ORCH_DIR, 'ACTIVE'))) return [finding('ok', 'active', 'no ACTIVE file')];
  return [finding('info', 'active', 'a legacy ACTIVE file exists in the data dir: nothing reads it any more, safe to remove')];
}

// ---- config: orchestrator/config.json read raw (manifest's cfg swallows errors); key types follow manifest's DEFAULTS ----------
function checkConfig(ctx) {
  const file = path.join(ctx.ORCH_DIR, 'config.json'), txt = readTxt(file);
  if (txt == null) return [finding(statOf(file) ? 'error' : 'ok', 'config', statOf(file) ? 'config.json exists but cannot be read' : 'no config.json; defaults apply')];
  let x; try { x = JSON.parse(txt); } catch (e) { return [finding('error', 'config', `config.json does not parse (${e.message}); manifest.js falls back to defaults`)]; }
  if (!x || typeof x !== 'object' || Array.isArray(x)) return [finding('error', 'config', 'config.json is not a JSON object')];
  const out = [];
  for (const [k, dv] of Object.entries(ctx.deps.DEFAULTS)) {
    if (!(k in x)) continue;
    if (typeof dv === 'number' && !(typeof x[k] === 'number' && Number.isFinite(x[k]) && x[k] > 0)) out.push(finding('error', 'config', `${k} must be a positive number (got ${JSON.stringify(x[k])})`));
    if (Array.isArray(dv) && !Array.isArray(x[k])) out.push(finding('error', 'config', `${k} must be an array (got ${JSON.stringify(x[k])})`));
  }
  return out.length ? out : [finding('ok', 'config', `config.json parses (${Object.keys(x).length} keys)`)];
}

// ---- wfp: the merged wfp config (wfp/config.default.json overlaid with <data>/wfp.json), shape only --------------------------
function checkWfp(ctx) {
  const { deps } = ctx, ROLES = deps.ROLES, out = [], err = m => out.push(finding('error', 'wfp', m));
  const { cfg: x, error: cfgErr } = paths.loadConfig('wfp');
  if (cfgErr) out.push(finding('error', 'wfp', `wfp config: ${cfgErr}`));
  const profiles = x && typeof x.profiles === 'object' && !Array.isArray(x.profiles) && x.profiles ? x.profiles : null;
  if (!profiles) { err('wfp config has no profiles object'); return out; }
  const names = Object.keys(profiles), dp = x.defaultProfile == null ? null : x.defaultProfile;
  if (dp !== null && !names.includes(dp)) err(`defaultProfile '${dp}' names no profile (${names.join(', ')})`);
  for (const p of names) {
    const roles = profiles[p] && profiles[p].roles;
    for (const r of ROLES) {
      const e = roles && roles[r];
      if (!e || typeof e.model !== 'string' || !e.model) err(`profile ${p}: role ${r} has no model`);
      else if (!EFFORTS.includes(e.effort)) err(`profile ${p}: role ${r} effort '${e.effort}' is not one of ${EFFORTS.join(', ')}`);
    }
  }
  const ur = x.ultracodeRaise && typeof x.ultracodeRaise === 'object' ? x.ultracodeRaise : {};
  for (const p of names) if (!names.includes(ur[p])) err(`ultracodeRaise has no profile for '${p}' (got ${JSON.stringify(ur[p])})`);
  for (const [t, v] of Object.entries(x.typeRoles && typeof x.typeRoles === 'object' ? x.typeRoles : {})) {
    const role = typeof v === 'string' ? v : v && v.role;
    if (!ROLES.includes(role)) err(`typeRoles ${t}: role '${role}' is not one of ${ROLES.join(', ')}`);
    for (const b of (v && Array.isArray(v.byDescription) ? v.byDescription : [])) if (!ROLES.includes(b && b.role)) err(`typeRoles ${t}: byDescription role '${b && b.role}' is not one of ${ROLES.join(', ')}`);
  }
  if (!out.some(f => f.level === 'error' || f.level === 'warn')) out.unshift(finding('ok', 'wfp', `wfp config: ${names.length} profiles, default ${dp === null ? 'none' : dp}`));
  return out;
}

// ---- shared for the row-5 checks and fixes ------------------------------------------------------------------------------
// act(f, data): attach what a fix needs to a finding without changing its JSON shape (non-enumerable).
const act = (f, data) => Object.defineProperty(f, 'act', { value: data, enumerable: false });
const gitEnv = () => Object.assign({}, process.env, { GIT_OPTIONAL_LOCKS: '0' });
const git = (cwd, args) => spawnSync('git', ['-C', cwd, ...args], { env: gitEnv(), encoding: 'utf8' });
const utcTs = now => new Date(now).toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');

// ---- worktrees: prunable entries (fixable: git worktree prune); merged, clean, quiet run worktrees (fixable: git worktree remove) ----
// commonDirOf(dir): what `git rev-parse --path-format=absolute --git-common-dir` prints, read from the files git itself reads, with no process spawned
// (a few hundred launch events name a few dozen paths; spawning git for each cost seconds). Walks up to the first `.git`: a directory is the common dir;
// a file ('gitdir: <worktree git dir>') points at a worktree git dir whose `commondir` file names it. Anything else (bare repo, odd layout, unreadable)
// returns null and the caller asks git.
function commonDirOf(dir) {
  try {
    for (let d = path.resolve(dir); ; d = path.dirname(d)) {
      const g = path.join(d, '.git'), st = statOf(g);
      if (st) {
        if (st.isDirectory()) return fs.realpathSync(g);
        const m = /^gitdir:\s*(.+?)\s*$/m.exec(readTxt(g) || ''); if (!m) return null;
        const wt = path.resolve(d, m[1]), cd = (readTxt(path.join(wt, 'commondir')) || '').trim();
        return cd ? fs.realpathSync(path.resolve(wt, cd)) : null;
      }
      if (path.dirname(d) === d) return null;
    }
  } catch { return null; }
}
// Repos: <root> plus the distinct existing `git` dirs of launch events in RUNS/*/manifest.jsonl, deduped by git common dir.
function reposOf(ctx) {
  const cand = new Set([ctx.root]); // a Set: each distinct path is resolved once, not once per launch event (hundreds of events share a few dozen paths)
  for (const e of listDir(ctx.RUNS) || []) {
    if (!e.isDirectory()) continue;
    for (const line of (readTxt(path.join(ctx.RUNS, e.name, 'manifest.jsonl')) || '').split('\n')) {
      if (!line.includes('"launch"') || !line.includes('"git"')) continue;
      try { const ev = JSON.parse(line); if (ev && ev.type === 'launch' && typeof ev.git === 'string' && ev.git) cand.add(ev.git); } catch {}
    }
  }
  const seen = new Map();
  for (const c of cand) {
    if (!statOf(c)) continue;
    let common = commonDirOf(c);
    if (!common) { const r = git(c, ['rev-parse', '--path-format=absolute', '--git-common-dir']); if (r.status !== 0) continue; common = r.stdout.trim(); }
    if (!seen.has(common)) seen.set(common, c);
  }
  return [...seen.entries()].map(([common, dir]) => ({ common, dir }));
}
function defaultBranch(dir) { // origin/HEAD, else main, else master, else null (then nothing is fixable)
  const o = git(dir, ['symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD']);
  if (o.status === 0 && o.stdout.trim()) return o.stdout.trim();
  for (const b of ['main', 'master']) if (git(dir, ['rev-parse', '--verify', '--quiet', `refs/heads/${b}`]).status === 0) return b;
  return null;
}
function parseWorktrees(txt) {
  const out = []; let cur = null;
  for (const line of txt.split('\n')) {
    if (line.startsWith('worktree ')) { cur = { path: line.slice(9) }; out.push(cur); continue; }
    if (!cur || !line) continue;
    const sp = line.indexOf(' '), k = sp < 0 ? line : line.slice(0, sp), v = sp < 0 ? true : line.slice(sp + 1);
    cur[k] = v;
  }
  return out;
}
function checkWorktrees(ctx) {
  const { deps, sid, now } = ctx, out = [];
  let scan; const scanOnce = () => (scan = scan || deps.scanAgents(sid, now));
  for (const repo of reposOf(ctx)) {
    const l = git(repo.dir, ['worktree', 'list', '--porcelain']);
    if (l.status !== 0) { out.push(finding('info', 'worktrees', `${repo.dir}: git worktree list failed`)); continue; }
    const all = parseWorktrees(l.stdout), main = all.length ? all[0].path : repo.dir, wts = all.slice(1); // the first entry is the main worktree
    let def; // resolved on the first run worktree: most repos have none, and each lookup is two or three git calls
    const other = [];
    for (const w of wts) {
      const br = typeof w.branch === 'string' ? w.branch.replace(/^refs\/heads\//, '') : null;
      const label = `${w.path}${br ? ` [${br}]` : w.detached ? ' [detached]' : ''}`;
      if (w.prunable) { out.push(act(finding('warn', 'worktrees', `${label}: prunable (${w.prunable === true ? 'gone' : w.prunable})`, { fixable: true, fix: '--fix worktrees' }), { kind: 'prune', repo: repo.dir })); continue; }
      const m = br && /^[^/].*\/([^/]+)$/.exec(br), run = m && deps.RUN_OK(m[1]) && statOf(path.join(ctx.RUNS, m[1])) ? m[1] : null;
      if (!run) { other.push(path.basename(w.path)); continue; }
      const why = [];
      if (w.locked) why.push('locked');
      if (def === undefined) def = defaultBranch(repo.dir);
      if (!def) why.push('no default branch (origin/HEAD, main or master)');
      else if (git(repo.dir, ['merge-base', '--is-ancestor', `refs/heads/${br}`, def]).status !== 0) why.push(`not merged into ${def}`);
      const st = git(w.path, ['status', '--porcelain']);
      if (st.status !== 0) why.push('git status failed'); else if (st.stdout.trim()) why.push('uncommitted changes');
      const L = deps.leaseState(sid, run, now);
      if (L.state === 'other') why.push(`lease live under ${deps.sid8(L.lease && L.lease.session)}`);
      let A = null; try { A = deps.activityOf(run, sid, 30, now, scanOnce()).R.state; } catch { A = 'unknown'; }
      if (A !== 'quiet') why.push(`activity ${A}`);
      if (why.length) { out.push(finding('info', 'worktrees', `${label}: run ${run}: kept (${why.join('; ')})`)); continue; }
      out.push(act(finding('info', 'worktrees', `${label}: run ${run}: merged into ${def}, clean, lease ${L.state}, quiet; removable (branch kept)`, { fixable: true, fix: '--fix worktrees' }), { kind: 'remove', repo: repo.dir, path: w.path }));
    }
    if (other.length) out.push(finding('info', 'worktrees', `${main}: ${other.length} other worktree(s), not on a <prefix>/<run> branch; report only: ${other.slice(0, 5).join(', ')}${other.length > 5 ? ', ...' : ''}`));
  }
  if (!out.length) out.push(finding('ok', 'worktrees', 'no linked worktrees'));
  return out;
}
function fixWorktrees(ctx, fs_) {
  const lines = [], pruned = new Set();
  for (const f of fs_) {
    const a = f.act; if (!a) continue;
    if (a.kind === 'prune') {
      if (pruned.has(a.repo)) continue; pruned.add(a.repo);
      const r = git(a.repo, ['worktree', 'prune']);
      lines.push(r.status === 0 ? `fixed worktrees: git -C ${a.repo} worktree prune` : `failed worktrees: git -C ${a.repo} worktree prune: ${(r.stderr || '').trim()}`);
    } else if (a.kind === 'remove') { // never --force: git refuses a dirty or locked worktree on its own
      const r = git(a.repo, ['worktree', 'remove', a.path]);
      lines.push(r.status === 0 ? `fixed worktrees: git -C ${a.repo} worktree remove ${a.path} (branch kept)` : `failed worktrees: worktree remove ${a.path}: ${(r.stderr || '').trim()}`);
    }
  }
  return lines;
}

// ---- backups: *.bak-* copies; *.bak-fix* and settings.json.bak-orch-*/bak-wfp-* beyond the newest 2 move to <root>/.trash ------
// Scanned under <root>: the data dir (when inside it), wfp, commands, hooks (two levels), plus settings.json.bak-* at the top.
function backupFiles(ctx) {
  const out = [], root = ctx.root, isBak = n => n.includes('.bak-');
  const walk = (rel, depth) => {
    for (const e of listDir(path.join(root, rel)) || []) {
      const r = path.join(rel, e.name);
      if (e.isFile() && isBak(e.name)) out.push(r);
      else if (e.isDirectory() && depth > 1 && e.name !== 'node_modules' && !e.name.startsWith('.')) walk(r, depth - 1);
    }
  };
  const rel = path.relative(root, ctx.ORCH_DIR), dataRel = rel && !rel.startsWith('..') && !path.isAbsolute(rel) ? [rel] : [];
  for (const d of [...dataRel, 'wfp', 'commands', 'hooks']) walk(d, 2);
  for (const e of listDir(root) || []) if (e.isFile() && e.name.startsWith('settings.json.bak-')) out.push(e.name);
  return out.sort().map(rel => { const st = statOf(path.join(root, rel)); return { rel, size: st ? st.size : 0, mtime: st ? st.mtimeMs : 0 }; });
}
function checkBackups(ctx) {
  const files = backupFiles(ctx), out = [];
  if (!files.length) return [finding('ok', 'backups', 'no *.bak-* files')];
  const fixable = new Set(files.filter(f => /\.bak-fix/.test(path.basename(f.rel))).map(f => f.rel));
  for (const fam of ['orch', 'wfp']) {
    const re = new RegExp(`^settings\\.json\\.bak-${fam}-`);
    files.filter(f => re.test(f.rel)).sort((a, b) => b.mtime - a.mtime || b.rel.localeCompare(a.rel)).slice(2).forEach(f => fixable.add(f.rel));
  }
  const bytes = files.reduce((s, f) => s + f.size, 0), oldest = files.reduce((a, b) => (b.mtime < a.mtime ? b : a));
  out.push(finding('info', 'backups', `${files.length} backup file(s), ${bytes} bytes, oldest ${oldest.rel} (${new Date(oldest.mtime).toISOString().slice(0, 10)})`));
  for (const rel of [...fixable].sort()) out.push(act(finding('warn', 'backups', `${rel}: ${/\.bak-fix/.test(rel) ? 'leftover *.bak-fix copy' : 'older than the newest 2 of its kind'}`, { fixable: true, fix: '--fix backups' }), { rel }));
  return out;
}
function fixBackups(ctx, fs_) { // rename only, never delete: <root>/.trash/doctor-<UTC ts>/<relpath>
  const lines = [], dest = path.join(ctx.root, '.trash', `doctor-${utcTs(ctx.now)}`);
  for (const f of fs_) {
    if (!f.act) continue;
    const to = path.join(dest, f.act.rel);
    try { fs.mkdirSync(path.dirname(to), { recursive: true }); fs.renameSync(path.join(ctx.root, f.act.rel), to); lines.push(`fixed backups: ${f.act.rel} -> ${path.relative(ctx.root, to)}`); }
    catch (e) { lines.push(`failed backups: ${f.act.rel}: ${e.message}`); }
  }
  return lines;
}

// ---- hooks: the plugin's hooks/hooks.json is present, parsed and points at scripts that exist; settings.json must not register them too ----
const HOOK_EVENTS = ['SessionStart', 'SubagentStart', 'SubagentStop', 'PreToolUse', 'UserPromptSubmit', 'Stop'];
const ORCH_CMD = /hooks\/orch-(attempts|return-cap|stop-gate)\.js|hooks\/orchestrator-guard\.js|hooks\/role-write-guard\.js|wfp\/wfp\.js/;
const groupCmds = g => ((g && g.hooks) || []).map(h => (h && h.command) || '');
const PLUGIN_VAR = /\$\{CLAUDE_PLUGIN_ROOT\}/;
function checkHooks(ctx) {
  const out = [], hf = path.join(ctx.pluginRoot, 'hooks', 'hooks.json'), txt = readTxt(hf);
  if (txt == null) out.push(finding('error', 'hooks', `plugin hooks file ${hf} is missing or unreadable`));
  else {
    let hj = null; try { hj = JSON.parse(txt); } catch (e) { out.push(finding('error', 'hooks', `plugin hooks file ${hf} does not parse (${e.message})`)); }
    if (hj) {
      const H = hj.hooks && typeof hj.hooks === 'object' && !Array.isArray(hj.hooks) ? hj.hooks : null;
      if (!H) out.push(finding('error', 'hooks', 'hooks.json has no {"hooks": {...}} wrapper, so Claude Code registers nothing'));
      else {
        const miss = HOOK_EVENTS.filter(e => !Array.isArray(H[e]) || !H[e].length);
        if (miss.length) out.push(finding('warn', 'hooks', `hooks.json registers nothing for ${miss.join(', ')}`));
        const bad = new Set();
        for (const gs of Object.values(H)) for (const g of Array.isArray(gs) ? gs : []) for (const c of groupCmds(g)) {
          for (const m of c.matchAll(/"\$\{CLAUDE_PLUGIN_ROOT\}\/([^"]+)"/g)) if (!statOf(path.join(ctx.pluginRoot, m[1]))) bad.add(m[1]);
          if (!PLUGIN_VAR.test(c) && c.trim()) bad.add(`(command without \${CLAUDE_PLUGIN_ROOT}: ${c.slice(0, 60)})`);
        }
        if (bad.size) out.push(finding('error', 'hooks', `hooks.json names files that do not exist: ${[...bad].join(', ')}`));
        if (!miss.length && !bad.size) out.push(finding('ok', 'hooks', `plugin hooks registered for ${HOOK_EVENTS.length} events; every command names an existing script`));
      }
    }
  }
  const st = readTxt(ctx.settings);
  if (st != null) {
    let cur = null; try { cur = JSON.parse(st); } catch (e) { out.push(finding('warn', 'hooks', `settings file ${ctx.settings} does not parse (${e.message}); duplicate hook entries not checked`)); }
    if (cur) {
      const dup = [];
      for (const [e, gs] of Object.entries((cur && cur.hooks) || {})) if (Array.isArray(gs)) for (const g of gs) for (const c of groupCmds(g)) { const m = ORCH_CMD.exec(c); if (m) dup.push(`${e}: ${m[0]}`); }
      if (dup.length) out.push(finding('warn', 'hooks', `settings.json still registers ${dup.length} orchestrator hook(s) (${dup.slice(0, 3).join('; ')}${dup.length > 3 ? '; ...' : ''}); with the plugin enabled every hook fires twice (double ledger lines, double launch counts)`, { fix: 'remove those entries from settings.json (hooks.json already registers them), or disable the plugin' }));
    }
  }
  return out;
}

// ---- runtime: Node version, a writable data dir, and the skills the plugin ships (counted from skills/*/ on disk, not a list) ----
const SKILLS = ['start', 'approve', 'away', 'cost', 'doctor', 'runs', 'status', 'stop', 'resume', 'memclean', 'council']; // the skills a complete install has
function checkRuntime(ctx) {
  const out = [], [maj, min] = process.versions.node.split('.').map(Number);
  if (maj > 22 || (maj === 22 && min >= 13)) out.push(finding('ok', 'runtime', `Node ${process.versions.node} (needs 22.13 or newer)`));
  else out.push(finding('error', 'runtime', `Node ${process.versions.node} is too old: the engine needs 22.13 or newer (node:sqlite)`));
  try { fs.accessSync(ctx.ORCH_DIR, fs.constants.W_OK); out.push(finding('ok', 'runtime', 'data dir is writable')); }
  catch (e) { out.push(finding(statOf(ctx.ORCH_DIR) ? 'error' : 'info', 'runtime', statOf(ctx.ORCH_DIR) ? `data dir ${ctx.ORCH_DIR} is not writable (${e.code})` : `data dir ${ctx.ORCH_DIR} does not exist yet; the first run creates it`)); }
  // the count is computed: the skills this plugin must ship (a deleted folder is still caught) plus every skills/<name>/ on disk, so a new skill is counted without an edit here
  const onDisk = (listDir(path.join(ctx.pluginRoot, 'skills')) || []).filter(e => e.isDirectory()).map(e => e.name);
  const all = [...new Set([...SKILLS, ...onDisk])].sort();
  const missing = all.filter(n => !statOf(path.join(ctx.pluginRoot, 'skills', n, 'SKILL.md')));
  out.push(missing.length ? finding('warn', 'runtime', `plugin skills missing: ${missing.join(', ')}`) : finding('ok', 'runtime', `all ${all.length} plugin skills present`));
  return out;
}

// ---- leases fix: release own expired leases and own live leases on done runs, through manifest's releaseRun(force=false,
// ownOnly=true): it unlinks only a lease this session holds (another session's lease reads 'skipped'). Never --force.
function fixLeases(ctx, fs_) {
  if (!ctx.sid) return ['skipped leases: needs --sid'];
  const lines = [];
  for (const f of fs_) {
    const a = f.act; if (!a) continue;
    if (a.legacy) { lines.push(`skipped leases: ${a.run}: legacy ACTIVE.lease is not released here`); continue; }
    let r; try { r = ctx.deps.releaseRun(a.run, ctx.sid, false, true); } catch (e) { lines.push(`failed leases: ${a.run}: ${e.message}`); continue; }
    lines.push(r && r.P && !r.skipped && !r.refused ? `fixed leases: released own lease on ${a.run}` : `skipped leases: ${a.run}: ${r && r.none ? 'no lease' : 'not this session\'s lease'}`);
  }
  return lines;
}

// ---- retention: the weekly metrics sync (crontab line + running cron daemon, via the boss-analytics lib) and the age of the last sync ----
function analyticsLib() { // C8: config analytics.lib, else BOSS_ANALYTICS_LIB; null when neither is set
  const c = paths.loadConfig('engine').cfg.analytics, v = (c && typeof c.lib === 'string' && c.lib) || process.env.BOSS_ANALYTICS_LIB || '';
  return v || null;
}
function checkRetention(ctx) {
  const out = [], lib = analyticsLib();
  if (!lib) out.push(finding('info', 'retention', 'weekly sync scheduler not checked: no boss-analytics lib configured (config.json analytics.lib or BOSS_ANALYTICS_LIB)'));
  else {
    let C; try { C = require(path.join(lib, 'cron.js')); } catch (e) { C = null; out.push(finding('error', 'retention', `cron.js failed to load from ${lib}: ${e.message}`)); }
    if (C) {
      const st = C.status();
      if (!st.crontab) out.push(finding('warn', 'retention', 'crontab is not installed: the weekly metrics sync cannot be scheduled (sudo apt install cron)'));
      else if (!st.installed) {
        // Removed on purpose: `ledger.js cron remove` leaves <data>/cron-removed (cron install deletes it), or config.json sets analytics.weeklySync false.
        // That is a choice, not a fault; a setup that wants the sync still gets the warning.
        const acfg = paths.loadConfig('engine').cfg.analytics, off = (acfg && acfg.weeklySync === false) ? 'config analytics.weeklySync is false'
          : fs.existsSync(path.join(paths.dataDir(), 'cron-removed')) ? 'removed with ledger.js cron remove' : null;
        out.push(off ? finding('info', 'retention', `weekly metrics sync not scheduled on purpose (${off}); node "$BOSS_LIB/ledger.js" cron install turns it back on`)
          : finding('warn', 'retention', 'weekly metrics sync not scheduled: node "$BOSS_LIB/ledger.js" cron install (or set analytics.weeklySync false in config.json to stop this warning)'));
      }
      else out.push(finding('ok', 'retention', 'weekly metrics sync scheduled (Sunday 03:00)'));
      if (st.installed && st.daemon.running === false) out.push(finding('warn', 'retention', `cron daemon is not running, so the weekly sync never fires; needs sudo: ${st.fix.filter(f => /cron/.test(f) && /sudo/.test(f)).join(' | ')}`));
    }
  }
  const dbp = paths.metricsDb();
  if (fs.existsSync(dbp)) {
    try {
      const M = require('./metrics.js'), db = M.openDb(dbp, true);
      try {
        const r = M.retention(db, { now: ctx.now });
        if (r.last_sync_age_days == null) out.push(finding('warn', 'retention', 'metrics.db has no transcript sync yet: node "$BOSS_LIB/ledger.js" sync'));
        else if (r.last_sync_age_days > 7) out.push(finding('warn', 'retention', `last transcript sync ${r.last_sync_age_days.toFixed(1)} days ago (over 7)`));
        else out.push(finding('ok', 'retention', `last transcript sync ${r.last_sync_age_days.toFixed(1)} days ago`));
        if (r.not_loaded) out.push(finding('warn', 'retention', `${r.not_loaded} sessions older than ${r.warn_days} days are not loaded and Claude Code deletes transcripts after ${r.cleanup_days} days: run ledger.js sync now`));
      } finally { db.close(); }
    } catch (e) { out.push(finding('info', 'retention', `metrics.db not readable: ${e.message}`)); }
  }
  return out;
}

const CHECKS = [
  { id: 'leases', check: checkLeases, fix: fixLeases },
  { id: 'wfp', check: checkWfp },
  { id: 'config', check: checkConfig },
  { id: 'runs', check: checkRuns },
  { id: 'active', check: checkActive },
  { id: 'worktrees', check: checkWorktrees, fix: fixWorktrees },
  { id: 'backups', check: checkBackups, fix: fixBackups },
  { id: 'hooks', check: checkHooks },
  { id: 'runtime', check: checkRuntime },
  { id: 'retention', check: checkRetention },
];

function run(opts, deps) {
  const ids = CHECKS.map(c => c.id), fixIds = CHECKS.filter(c => c.fix).map(c => c.id);
  let want = ids, fixWant = [];
  if (opts.check !== undefined) {
    want = String(opts.check).split(',').map(s => s.trim()).filter(Boolean);
    if (!want.length) deps.fail(`doctor: --check needs check ids (${ids.join(', ')})`);
    for (const w of want) if (!ids.includes(w)) deps.fail(`doctor: unknown check '${w}' (checks: ${ids.join(', ')})`);
  }
  if (opts.fix !== undefined) {
    fixWant = String(opts.fix).trim() === 'all' ? fixIds : String(opts.fix).split(',').map(s => s.trim()).filter(Boolean);
    if (!fixWant.length) deps.fail(`doctor: --fix needs ids or all (fixable checks: ${fixIds.join(', ')})`);
    for (const w of fixWant) if (!fixIds.includes(w)) deps.fail(`doctor: --fix '${w}' has no fix (fixable checks: ${fixIds.join(', ')})`);
    fixWant = fixWant.filter(w => want.includes(w));
  }
  const root = process.env.ORCH_CLAUDE_ROOT || paths.configDir();
  const ctx = { root, pluginRoot: process.env.ORCH_PLUGIN_ROOT || paths.pluginRoot, settings: process.env.ORCH_SETTINGS || path.join(root, 'settings.json'), ORCH_DIR: deps.ORCH_DIR, RUNS: deps.RUNS,
    sid: opts.sid || null, now: Date.now(), deps };
  const runCheck = c => { try { return c.check(ctx); } catch (e) { return [finding('error', c.id, `check failed: ${e && e.message}`)]; } };
  const byCheck = new Map(), fixed = [];
  for (const c of CHECKS) if (want.includes(c.id)) byCheck.set(c.id, runCheck(c));
  for (const c of CHECKS) { // each applied fix prints its lines, then its check runs again
    if (!fixWant.includes(c.id)) continue;
    if (c.id === 'leases' && !ctx.sid) { fixed.push('skipped leases: needs --sid'); continue; }
    const todo = byCheck.get(c.id).filter(f => f.fixable);
    if (!todo.length) continue;
    let lines; try { lines = c.fix(ctx, todo) || []; } catch (e) { lines = [`failed ${c.id}: ${e && e.message}`]; }
    fixed.push(...lines);
    ctx.now = Date.now(); byCheck.set(c.id, runCheck(c));
  }
  const findings = [].concat(...byCheck.values());
  for (const f of findings) if (!LEVELS.includes(f.level)) f.level = 'error';
  const warn = findings.filter(f => f.level === 'warn').length, error = findings.filter(f => f.level === 'error').length;
  const fixable = [...new Set(findings.filter(f => f.fixable).map(f => f.check))];
  if (opts.json) {
    console.log(JSON.stringify(Object.assign({ root, orch_dir: ctx.ORCH_DIR, settings: ctx.settings, sid: ctx.sid, checks: ids.filter(i => want.includes(i)), findings, warn, error, fixable },
      opts.fix !== undefined ? { fixed } : {})));
    return;
  }
  for (const l of fixed) console.log(l);
  for (const f of findings) console.log(`${f.level} ${f.check}: ${f.msg}${f.fix ? ` · fix: ${f.fix}` : ''}`);
  if (!ctx.sid && want.includes('leases')) console.log('note: no session id, so no lease reads as own (pass --sid)');
  console.log(`doctor: ${warn} warn, ${error} error; fixable: ${fixable.length ? fixable.join(',') : 'none'}`);
}

module.exports = { run, CHECKS };
