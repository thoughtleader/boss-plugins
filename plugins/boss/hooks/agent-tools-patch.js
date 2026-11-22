#!/usr/bin/env node
// agent-tools-patch: SessionStart hook. OPT-IN, off by default: it edits files inside the installed plugin, which Claude
// Code offers no supported way to avoid (see "No override mechanism" below), so a published install never does it unasked.
//
// When enabled it appends the owner's own MCP tool patterns to the installed orch agents' `tools:` frontmatter.
// Config (<dataDir>/config.json), both keys required:
//   "patchPluginAgents": true                                                          // the opt-in
//   "extraAgentTools": { "*": ["mcp__x__*"], "orch-scout": ["mcp__example__*"] }          // "*" = every agent; keys accept `boss:` prefix
// Idempotent (only missing entries are added), silent on any failure, never blocks the session.
// In a git checkout the patched files are marked skip-worktree, which hides the edit from `git status` (it prints nothing) but not from
// `git merge`: a merge of a branch that changes one of those files is refused ("Your local changes would be overwritten").
// Before a merge into such a checkout, lift the patch, merge, then run the patcher again:
//   node <plugin dir>/hooks/agent-tools-patch.js --lift [<file>...]    (no file = every patched agent file)
//   ...git merge...
//   node <plugin dir>/hooks/agent-tools-patch.js                        (re-applies the patch and the flag; this is also what the session-start hook does)
// --lift prints one line per file: lifted (flag cleared, committed text restored), not patched, or refused: why. It only lifts a file whose
// sole difference from the committed version is extra entries on its `tools:` line; any other local change is refused and left untouched
// (exit 1). `git ls-files -v | grep '^[Sh]'` lists every skip-worktree or assume-unchanged file of a checkout.
// Agents load at session start, so a first patch takes effect from the next session. A plugin update replaces the patched
// files; the next session patches the new ones again.
//
// No override mechanism (Claude Code sub-agents and plugins docs, checked 2026-11-18): a plugin agent is registered as
// `<plugin>:<name>` (here `boss:orch-builder`, the only form the plan rows call). A user (`~/.claude/agents/`), project or
// `--agents` agent is a different identifier, because a frontmatter `name` cannot contain `:`; the documented
// same-name priority never pairs a user file with a plugin agent. No setting adds tools to a plugin agent's `tools:` list,
// and `permissions.allow` only grants permission session-wide. So the only way to widen the tools is to edit the file.
'use strict';
const fs = require('fs'), path = require('path'), cp = require('child_process');
const paths = require(path.join(__dirname, '..', 'lib', 'paths.js'));

function patch(text, extras) {
  const m = text.match(/^(---\r?\n[\s\S]*?^tools:[ \t]*)([^\r\n]*)/m);
  if (!m) return null;
  const have = m[2].split(',').map((s) => s.trim()).filter(Boolean);
  const add = extras.filter((t) => !have.includes(t));
  if (!add.length) return null;
  return text.slice(0, m.index) + m[1] + have.concat(add).join(', ') + text.slice(m.index + m[0].length);
}

const gitOut = (dir, args) => cp.execFileSync('git', ['-C', dir].concat(args), { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
const toolsOf = (text) => { const m = text.match(/^(---\r?\n[\s\S]*?^tools:[ \t]*)([^\r\n]*)/m); return m ? { m, list: m[2].split(',').map((x) => x.trim()).filter(Boolean) } : null; };

// lift(opts): undo the patch on skip-worktree agent files so a merge can touch them. opts.dir (the agents dir) and opts.files (a bare name is
// looked up in dir, anything with a path separator is taken as a path; default: every .md in dir) are for tests and the CLI.
// -> {results: [{file, status: 'lifted'|'not patched'|'refused', why?}], refused: n}
function lift(opts = {}) {
  const base = opts.dir || path.join(__dirname, '..', 'agents');
  const given = opts.files && opts.files.length ? opts.files : fs.readdirSync(base).filter((x) => x.endsWith('.md')).sort();
  const results = [];
  for (const g of given) {
    const file = g.includes(path.sep) ? path.resolve(g) : path.join(base, g), dir = path.dirname(file), f = path.basename(file), res = { file: f };
    results.push(res);
    let flag = '';
    try { flag = gitOut(dir, ['ls-files', '-v', '--', f]).slice(0, 1); } catch (_) { res.status = 'not patched'; res.why = 'not a git checkout, or not a tracked file'; continue; }
    if (flag !== 'S') { res.status = 'not patched'; res.why = flag ? 'not marked skip-worktree' : 'not tracked'; continue; }
    try {
      const cur = fs.readFileSync(file, 'utf8'), committed = gitOut(dir, ['show', 'HEAD:./' + f]);
      const a = toolsOf(cur), b = toolsOf(committed);
      if (!a || !b) { res.status = 'refused'; res.why = 'no tools: line found'; continue; }
      const missing = b.list.filter((t) => !a.list.includes(t));
      // the CURRENT text with only the value of its tools: line swapped for the committed one must be the committed text: any other difference (above or below that line) refuses
      const rebuilt = cur.slice(0, a.m.index) + a.m[1] + b.m[2] + cur.slice(a.m.index + a.m[0].length);
      if (missing.length || rebuilt !== committed) { res.status = 'refused'; res.why = missing.length ? 'its tools: line lacks committed entries (' + missing.join(', ') + ')' : 'it differs from the committed version outside the tools: line'; continue; }
      // all or nothing per file: text and flag both change, or neither (put the old text back when the flag cannot be cleared)
      fs.writeFileSync(file, committed);
      try { gitOut(dir, ['update-index', '--no-skip-worktree', f]); }
      catch (e) { fs.writeFileSync(file, cur); throw e; }
      res.status = 'lifted';
    } catch (e) { res.status = 'refused'; res.why = 'could not lift: ' + String(e && e.message).split('\n')[0]; }
  }
  return { results, refused: results.filter((r) => r.status === 'refused').length };
}

// opts.dir (the agents dir) and opts.env are for tests.
function run(opts = {}) {
  let cfg; try { cfg = JSON.parse(fs.readFileSync(path.join(paths.dataDir(opts.env || process.env), 'config.json'), 'utf8')); } catch (_) { return 0; }
  if (!cfg || cfg.patchPluginAgents !== true) return 0;
  const map = cfg.extraAgentTools;
  if (!map || typeof map !== 'object' || Array.isArray(map)) return 0;
  const dir = opts.dir || path.join(__dirname, '..', 'agents');
  let n = 0;
  for (const f of fs.readdirSync(dir).filter((x) => x.endsWith('.md'))) {
    const base = f.replace(/\.md$/, '');
    const extras = [].concat(map['*'] || [], map[base] || [], map['boss:' + base] || []).filter((t) => typeof t === 'string' && t);
    if (!extras.length) continue;
    const file = path.join(dir, f);
    try {
      const out = patch(fs.readFileSync(file, 'utf8'), extras);
      if (out) {
        fs.writeFileSync(file, out); n++;
        // Directory-source installs read the repo itself: hide the local edit from git so it is never committed or published.
        try { cp.execFileSync('git', ['-C', dir, 'update-index', '--skip-worktree', f], { stdio: 'ignore' }); } catch (_) { /* not a git checkout */ }
      }
    } catch (_) { /* read-only install: skip */ }
  }
  return n;
}

if (require.main === module) {
  const argv = process.argv.slice(2);
  if (argv[0] === '--lift') {
    // the one command-line use: print a line per file, exit 1 when any file was refused
    let r;
    try { r = lift({ files: argv.slice(1) }); }
    catch (e) { console.error('agent-tools-patch --lift: ' + String(e && e.message).split('\n')[0]); process.exit(1); }
    for (const x of r.results) console.log(x.status === 'refused' ? `refused: ${x.file}: ${x.why}` : x.status === 'not patched' ? `not patched: ${x.file} (${x.why})` : `lifted: ${x.file}`);
    process.exit(r.refused ? 1 : 0);
  }
  try { run(); } catch (_) { /* never block */ } process.exit(0);
}
module.exports = { patch, run, lift };
