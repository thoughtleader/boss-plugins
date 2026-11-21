// boss.artifacts: the files a run produced, for the Board's "Files" lists. Read-only, stat and tail-read only.
// Three sources, merged by real path (a file named twice is listed once):
//   snapshot  items[].artifacts[].path (result files the mirror already found), attributed to that item's row
//   message   absolute or ~ paths in an attempt's final message. Neither ledger.jsonl nor manifest.jsonl carries the text: a final `stop` event
//             (final: true) names `output_file`, a symlink to the agent's transcript (<projects>/<proj>/<session>/subagents/agent-<task>.jsonl);
//             the final message is that file's last assistant text. Only its tail is read, and the result is cached on path+size+mtime.
//   folder    files in the run's own folder (two levels deep), attributed by the `<row>-` filename prefix (`4c-files.png` -> row 4c)
// Which paths may be listed is decided by the caller's `roots` (the tools' run folders) plus the same rule the extension's allowedPath uses:
// under home with no dot-segment below it. The extension re-checks every path before it opens anything (see vscode/src/boardhost.mts).
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/** Files listed per run (newest first). Tunable. */
export const FILES_MAX = 40;
/** Directory entries looked at per run folder. */
export const SCAN_MAX = 300;
const SCAN_DEPTH = 2;
const TAIL_BYTES = 64 * 1024;
const ID = /^[A-Za-z0-9-]{8,64}$/;

/** extension -> kind. Source code is left out on purpose: the commit SHA covers it. */
export const KIND_BY_EXT = {
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image', svg: 'image',
  md: 'md', markdown: 'md', html: 'html', htm: 'html', txt: 'text', log: 'text', csv: 'text', json: 'text',
  pdf: 'pdf', webm: 'video', mp4: 'video', mov: 'video', vsix: 'package',
};
const SKIP_NAMES = new Set(['progress.json', 'ack.json']);
export const kindOf = (p) => KIND_BY_EXT[path.extname(String(p)).slice(1).toLowerCase()] ?? null;
/** `4c-files.png` -> `4c`; `2026-11-02-x.md` -> `2026` (the caller keeps it only when that row exists). */
export const rowFromName = (name) => /^(\d+[a-z]?)-/.exec(String(name))?.[1] ?? null;

const within = (root, p) => p === root || p.startsWith(root.endsWith(path.sep) ? root : root + path.sep);
const real = (p) => { try { return fs.realpathSync(p); } catch { return null; } };

/** May this real path be listed? Inside a trusted root, or under home with no segment below it starting with "." (so ~/.ssh and ~/.claude/sessions stay out). Never *.key. */
export function inBounds(realPath, { home, roots = [] }) {
  if (typeof realPath !== 'string' || !path.isAbsolute(realPath) || realPath.toLowerCase().endsWith('.key')) return false;
  if (roots.some((r) => within(r, realPath))) return true;
  const rel = path.relative(home, realPath);
  if (!rel || rel.startsWith('..') || path.isAbsolute(rel)) return false;
  return !rel.split(path.sep).some((s) => s.startsWith('.'));
}

// ---------- paths named in a message ----------
const PATH_RE = /(?:^|[\s`'"(\[<=,*])((?:~\/|\/(?:home|mnt|Users|tmp|opt|srv|var)\/)[^\s`'"<>()\[\]{}|,;*]+)/g;
/** Absolute and ~ paths in free text, `~` expanded, trailing punctuation and :line suffixes removed. Not checked against the disk. */
export function extractPaths(text, home = os.homedir()) {
  if (typeof text !== 'string' || !text) return [];
  const out = new Set();
  for (const m of text.matchAll(PATH_RE)) {
    let p = m[1].replace(/[.:,;!?]+$/, '').replace(/:\d+(?::\d+)?$/, '').replace(/[.:,;!?]+$/, '');
    if (p.startsWith('~/')) p = path.join(home, p.slice(2));
    if (p.length > 1 && p.length <= 600) out.add(p);
  }
  return [...out];
}

/** Text of the last assistant entry (a transcript's parsed lines, in order). */
export function lastAssistantText(entries) {
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i];
    if (e?.type !== 'assistant' && e?.message?.role !== 'assistant') continue;
    const c = e.message?.content;
    const t = typeof c === 'string' ? c : Array.isArray(c) ? c.filter((b) => b?.type === 'text' && typeof b.text === 'string').map((b) => b.text).join('\n') : '';
    if (t) return t;
  }
  return '';
}
function tailEntries(file, bytes = TAIL_BYTES) {
  let fd;
  try {
    const st = fs.statSync(file); fd = fs.openSync(file, 'r');
    const len = Math.min(st.size, bytes); const buf = Buffer.alloc(len); fs.readSync(fd, buf, 0, len, st.size - len);
    const lines = buf.toString('utf8').split('\n'); if (st.size > len) lines.shift();   // the first line of a tail is cut
    return lines.flatMap((l) => { if (!l) return []; try { return [JSON.parse(l)]; } catch { return []; } });
  } catch { return []; } finally { if (fd !== undefined) try { fs.closeSync(fd); } catch { /* closed */ } }
}

/** The transcript behind a final stop event: its output_file when that resolves inside the projects dir, else the matching agent-<task>.jsonl under the projects dir. */
export function transcriptFor(ev, projectsDir) {
  const root = real(projectsDir); if (!root) return null;
  const inside = (f) => { const r = real(f); return r && r.endsWith('.jsonl') && within(root, r) ? r : null; };
  if (typeof ev?.output_file === 'string' && path.isAbsolute(ev.output_file)) { const r = inside(ev.output_file); if (r) return r; }
  if (!ID.test(String(ev?.session)) || !ID.test(String(ev?.task_id))) return null;
  let dirs = []; try { dirs = fs.readdirSync(root); } catch { return null; }
  for (const d of dirs) { const r = inside(path.join(root, d, ev.session, 'subagents', `agent-${ev.task_id}.jsonl`)); if (r) return r; }
  return null;
}
const msgCache = new Map();   // `${file}|${size}|${mtimeMs}` -> candidate paths
function messagePaths(ev, projectsDir, home) {
  const f = transcriptFor(ev, projectsDir); if (!f) return [];
  let st; try { st = fs.statSync(f); } catch { return []; }
  const key = `${f}|${st.size}|${st.mtimeMs}`;
  let hit = msgCache.get(key);
  if (!hit) { hit = extractPaths(lastAssistantText(tailEntries(f)), home); if (msgCache.size > 400) msgCache.clear(); msgCache.set(key, hit); }
  return hit;
}

function scanDir(root, depth, out, budget) {
  let ents; try { ents = fs.readdirSync(root, { withFileTypes: true }); } catch { return; }
  for (const e of ents) {
    if (budget.n <= 0) return;
    if (e.name.startsWith('.') || e.name === 'node_modules') continue;
    const full = path.join(root, e.name);
    if (e.isDirectory()) { if (depth > 0) scanDir(full, depth - 1, out, budget); }
    else if (e.isFile()) { budget.n--; if (kindOf(e.name) && !SKIP_NAMES.has(e.name)) out.push(full); }   // symlinks are neither: they are skipped
  }
}
const rowShortOf = (id) => String(id ?? '').replace(/^(row|gate):/, '');

/**
 * Everything a run produced, newest first (all of it: the caller caps for display).
 *   dirs     folders to list (the run's own folder)
 *   snap     the progress snapshot (items[].artifacts)
 *   events   final stop events: [{ attempt, row, session, task_id, output_file }]
 *   roots    real paths of the tools' run folders; home  real home dir; projectsDir  where transcripts live
 * Returns [{ path, name, kind, row, prefix_row, via, size, mtimeMs }]: `row` from the snapshot or the stop event (explicit),
 * `prefix_row` from the filename (the caller keeps it only if that row exists in the run).
 */
export function collectArtifacts({ dirs = [], snap = null, events = [], roots = [], home = os.homedir(), projectsDir }) {
  const realHome = real(home) ?? home;
  const map = new Map();
  const add = (p, via, row) => {
    if (typeof p !== 'string' || !path.isAbsolute(p) || p.includes('\0') || !kindOf(p)) return;
    const r = real(p); if (!r || !inBounds(r, { home: realHome, roots })) return;
    const prior = map.get(r);
    if (prior) { if (!prior.row && row) prior.row = row; if (!prior.via.includes(via)) prior.via.push(via); return; }
    let st; try { st = fs.statSync(r); } catch { return; }
    if (!st.isFile()) return;
    map.set(r, { path: r, name: path.basename(r), kind: kindOf(r), row: row || null, prefix_row: rowFromName(path.basename(r)), via: [via], size: st.size, mtimeMs: Math.round(st.mtimeMs) });
  };
  for (const it of Array.isArray(snap?.items) ? snap.items : []) {
    if (!it || !Array.isArray(it.artifacts)) continue;
    const row = it.kind === 'attempt' ? rowShortOf(it.parent) : it.kind === 'row' || it.kind === 'gate' ? rowShortOf(it.id) : null;
    for (const a of it.artifacts) if (a && typeof a.path === 'string') add(a.path, 'result', row);
  }
  if (projectsDir) for (const ev of events) {
    const row = ev?.row == null ? null : String(ev.row);
    for (const p of messagePaths(ev, projectsDir, realHome)) add(p, 'message', row);
  }
  const seenDirs = new Set();
  const budget = { n: SCAN_MAX };
  for (const d of dirs) {
    const rd = typeof d === 'string' && path.isAbsolute(d) ? real(d) : null;
    if (!rd || seenDirs.has(rd) || !inBounds(rd, { home: realHome, roots })) continue;
    seenDirs.add(rd);
    const found = []; scanDir(rd, SCAN_DEPTH, found, budget);
    for (const f of found) add(f, 'folder', null);
  }
  return [...map.values()].sort((a, b) => b.mtimeMs - a.mtimeMs || a.path.localeCompare(b.path));
}
