#!/usr/bin/env node
// agent-tools-patch: SessionStart hook. OPT-IN, off by default: it edits files inside the installed plugin, which Claude
// Code offers no supported way to avoid (see "No override mechanism" below), so a published install never does it unasked.
//
// When enabled it appends the owner's own MCP tool patterns to the installed orch agents' `tools:` frontmatter.
// Config (<dataDir>/config.json), both keys required:
//   "patchPluginAgents": true                                                          // the opt-in
//   "extraAgentTools": { "*": ["mcp__x__*"], "orch-scout": ["mcp__example__*"] }          // "*" = every agent; keys accept `boss:` prefix
// Idempotent (only missing entries are added), silent on any failure, never blocks the session.
// In a git checkout the patched files are marked skip-worktree (undo: git update-index --no-skip-worktree <file>, then git checkout it).
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

if (require.main === module) { try { run(); } catch (_) { /* never block */ } process.exit(0); }
module.exports = { patch, run };
