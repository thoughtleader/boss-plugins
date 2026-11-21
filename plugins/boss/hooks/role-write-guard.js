#!/usr/bin/env node
// role-write-guard: PreToolUse (Write|Edit) hook for the read-only orchestrator roles (contract C5).
// planner, plan-checker, reviewer and scout may Write/Edit only under the runs dir and /tmp/claude-<uid>/ (or claude-<uid>/ under TMPDIR),
// after resolving symlinks. Plugin agents ignore frontmatter `hooks:`, so this is a hooks.json entry that
// keys on the hook input's top-level `agent_type` (`boss:orch-planner` -> `orch-planner` via paths.agentBase).
// Any other agent type, and the main chat (no agent_type), passes through untouched.
// Deny = exit 2 with the reason on stderr. Input that cannot be parsed passes through: the role is unknown then.
'use strict';
const fs = require('fs'), os = require('os'), path = require('path');
const paths = require(path.join(__dirname, '..', 'lib', 'paths.js'));

const ROLES = new Set(['orch-planner', 'orch-plan-checker', 'orch-reviewer', 'orch-scout']);

// Resolve symlinks for a path that may not exist yet: realpath the nearest existing ancestor, append the rest.
// A dangling symlink on the way is unresolvable, so it returns '' (blocked).
function real(p) {
  let cur = p; const rest = [];
  for (;;) {
    let r = null;
    try { r = fs.realpathSync(cur); } catch (e) { /* not (yet) there */ }
    if (r !== null) return path.join(r, ...rest);
    let link = false;
    try { fs.lstatSync(cur); link = true; } catch (e) { /* absent */ }
    if (link || cur === path.dirname(cur)) return '';
    rest.unshift(path.basename(cur)); cur = path.dirname(cur);
  }
}

function roots() {
  const uid = typeof process.getuid === 'function' ? process.getuid() : 0;
  // /tmp/claude-<uid> (Linux), plus the same name under the OS temp dir (TMPDIR; macOS keeps the harness scratch there, Windows has no /tmp)
  const scratch = [`/tmp/claude-${uid}`, path.join(os.tmpdir(), `claude-${uid}`)];
  return [paths.runsDir(), ...scratch].map(r => real(path.resolve(r))).filter(Boolean);
}

// -> null when allowed, else the deny message
function check(input) {
  if (!input || typeof input !== 'object') return null;
  if (!ROLES.has(paths.agentBase(input.agent_type))) return null;
  const ti = input.tool_input && typeof input.tool_input === 'object' ? input.tool_input : {};
  const raw = typeof ti.file_path === 'string' ? ti.file_path : '';
  const p = path.resolve(typeof input.cwd === 'string' && input.cwd ? input.cwd : process.cwd(), raw);
  const t = raw ? real(p) : '';
  const rs = roots();
  const ok = t !== '' && rs.some(r => t === r || t.startsWith(r + path.sep));
  if (ok) return null;
  const shown = paths.runsDir().replace(/\/+$/, '');
  return `[orch-guard] Write/Edit for this role go only under ${shown}/ or /tmp/claude-<uid>/ (symlinks resolved). Blocked: ${p}${t && t !== p ? ' -> ' + t : ''}`;
}

module.exports = { check, real };

if (require.main === module) {
  let raw = ''; process.stdin.setEncoding('utf8');
  process.stdin.on('data', d => { raw += d; });
  process.stdin.on('end', () => {
    let msg = null;
    try { msg = check(JSON.parse(raw || '{}')); } catch (e) { msg = null; }
    if (msg) { process.stderr.write(msg + '\n'); process.exit(2); }
    process.exit(0);
  });
  process.stdin.on('error', () => process.exit(0));
}
