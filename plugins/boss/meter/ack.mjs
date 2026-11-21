// boss.ack/1: what the owner has dealt with on the Board, shared by every window. One file: <boss root>/ack.json (~/.claude/boss/ack.json).
// Plain .mjs on purpose (like board.mjs): the extension bundles it and node --test imports it.
//   filterBoard(board, ack, nowMs)   pure: Needs-you split into visible / hidden, abandoned runs moved out of runs
//   heldRunIds(board)  pure: runs whose notifications are held back (abandoned, or a snoozed need)
//   ackNeed / snoozeNeed / abandonRun / restoreEntry   pure edits of an ack object
//   readAck / writeAck / updateAck   file I/O, atomic (temp file in the same dir + rename), tolerant of a missing or corrupt file
//
// Entries are keyed by the need id and carry the need's `since` stamp. A need whose `since` differs from the stored one is a NEW event and shows again.
// A need with no `since` (launch_cap) is stamped with its detail text instead; if both are empty the stamp is '' and an ack lasts until restored.
import fs from 'node:fs';
import path from 'node:path';

export const ACK_SCHEMA = 'boss.ack/1';
export const SNOOZE_HOUR_MS = 3_600_000;
/** "Until tomorrow" means the next local 08:00. */
export const TOMORROW_HOUR = 8;
const KEEP_ACK_MS = 30 * 86_400_000, KEEP_SNOOZE_MS = 86_400_000, KEEP_ABANDON_MS = 90 * 86_400_000;
export const MOVED_ON_REASONS = ['moved on', 'finished elsewhere', 'not needed'];

export const ackPath = (bossRoot) => path.join(bossRoot, 'ack.json');
export const emptyAck = () => ({ schema: ACK_SCHEMA, acks: {}, snoozes: {}, abandoned: {} });
const isObj = (v) => v && typeof v === 'object' && !Array.isArray(v);
const str = (v, n = 200) => (typeof v === 'string' ? v.replace(/[\x00-\x1f\x7f]/g, ' ').slice(0, n) : '');
const tms = (s) => { const t = Date.parse(s); return Number.isNaN(t) ? null : t; };
const iso = (ms) => new Date(ms).toISOString();

/** Coerce anything read from disk into a well-formed ack object (unknown keys dropped). */
export function normalize(raw) {
  const a = emptyAck();
  if (!isObj(raw)) return a;
  for (const [k, v] of Object.entries(isObj(raw.acks) ? raw.acks : {})) if (isObj(v) && typeof v.since === 'string') a.acks[k] = { since: v.since, at: str(v.at, 32), run_id: str(v.run_id, 128) || null };
  for (const [k, v] of Object.entries(isObj(raw.snoozes) ? raw.snoozes : {})) if (isObj(v) && typeof v.since === 'string' && tms(v.until) != null) a.snoozes[k] = { since: v.since, until: v.until, at: str(v.at, 32), run_id: str(v.run_id, 128) || null };
  for (const [k, v] of Object.entries(isObj(raw.abandoned) ? raw.abandoned : {})) if (isObj(v)) a.abandoned[k] = { since: str(v.since, 32), at: str(v.at, 32), reason: str(v.reason, 80) || 'moved on', title: str(v.title, 200), tool: str(v.tool, 20) };
  return a;
}

// ---------- file I/O ----------
export function readAck(file) {
  let txt; try { txt = fs.readFileSync(file, 'utf8'); } catch { return emptyAck(); }
  try { return normalize(JSON.parse(txt)); } catch { return emptyAck(); }
}
/** Atomic write: a temp file in the same directory, then rename over the target. Returns false on failure (never throws). */
export function writeAck(file, ack) {
  const tmp = `${file}.${process.pid}.${Math.random().toString(36).slice(2, 8)}.tmp`;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(tmp, JSON.stringify(ack, null, 2) + '\n', { mode: 0o600 });
    fs.renameSync(tmp, file);
    return true;
  } catch { try { fs.unlinkSync(tmp); } catch { /* none */ } return false; }
}
/** Read the file fresh, apply `edit(ack)` (returns the new ack), prune, write. A corrupt file is kept as ack.json.bad before it is replaced. */
export function updateAck(file, edit, nowMs = Date.now()) {
  let fresh;
  try {
    const txt = fs.readFileSync(file, 'utf8');
    try { fresh = normalize(JSON.parse(txt)); } catch { try { fs.copyFileSync(file, `${file}.bad`); } catch { /* none */ } fresh = emptyAck(); }
  } catch { fresh = emptyAck(); }
  const next = prune(edit(fresh) || fresh, nowMs);
  return writeAck(file, next) ? next : null;
}

// ---------- pure edits ----------
/** The stamp an ack is tied to. */
export const stampOf = (need) => (need && (need.since || need.detail)) || '';

export function ackNeed(ack, need, nowMs) {
  const a = normalize(ack); const k = need.id;
  a.acks[k] = { since: stampOf(need), at: iso(nowMs), run_id: need.run_id || null };
  delete a.snoozes[k];
  return a;
}
export function snoozeNeed(ack, need, untilMs, nowMs) {
  const a = normalize(ack); const k = need.id;
  a.snoozes[k] = { since: stampOf(need), until: iso(untilMs), at: iso(nowMs), run_id: need.run_id || null };
  delete a.acks[k];
  return a;
}
/** Mark a paused run abandoned. `run` is the board's run card (id, title, tool, updated_at). */
export function abandonRun(ack, run, reason, nowMs) {
  const a = normalize(ack);
  a.abandoned[run.id] = { since: str(run.updated_at, 32), at: iso(nowMs), reason: MOVED_ON_REASONS.includes(reason) ? reason : 'moved on', title: str(run.title || run.id, 200), tool: str(run.tool, 20) };
  return a;
}
/** Bring back whatever `key` names: a hidden need id, or an abandoned run id. */
export function restoreEntry(ack, key) {
  const a = normalize(ack);
  delete a.acks[key]; delete a.snoozes[key]; delete a.abandoned[key];
  return a;
}
export function prune(ack, nowMs) {
  const a = normalize(ack);
  for (const [k, v] of Object.entries(a.acks)) { const t = tms(v.at); if (t != null && nowMs - t > KEEP_ACK_MS) delete a.acks[k]; }
  for (const [k, v] of Object.entries(a.snoozes)) { const t = tms(v.until); if (t != null && nowMs - t > KEEP_SNOOZE_MS) delete a.snoozes[k]; }
  for (const [k, v] of Object.entries(a.abandoned)) { const t = tms(v.at); if (t != null && nowMs - t > KEEP_ABANDON_MS) delete a.abandoned[k]; }
  return a;
}
/** End of a snooze: kind '1h' | 'tomorrow'. */
export function snoozeUntil(kind, nowMs) {
  if (kind === '1h') return nowMs + SNOOZE_HOUR_MS;
  if (kind === 'tomorrow') { const d = new Date(nowMs); d.setDate(d.getDate() + 1); d.setHours(TOMORROW_HOUR, 0, 0, 0); return d.getTime(); }
  return null;
}

// ---------- reading ----------
/** null when the need is visible, else { kind: 'done' | 'snoozed', at, until? }. */
export function hiddenState(ack, need, nowMs) {
  if (!ack || !need) return null;
  const stamp = stampOf(need);
  const d = ack.acks?.[need.id];
  if (d && d.since === stamp) return { kind: 'done', at: d.at || null };
  const s = ack.snoozes?.[need.id];
  if (s && s.since === stamp && (tms(s.until) ?? 0) > nowMs) return { kind: 'snoozed', at: s.at || null, until: s.until };
  return null;
}
/** The abandoned entry for a run, only while the run is still paused at the same updated_at; a resumed or changed run comes back. */
export function abandonedFor(ack, run) {
  const e = ack?.abandoned?.[run?.id];
  return e && run.paused === true && e.since === (run.updated_at || '') ? e : null;
}
/** Run ids whose notifications are held back: abandoned runs, and runs with a snoozed need. Read from a filtered board (see filterBoard). */
export function heldRunIds(board) {
  const out = new Set();
  for (const x of board?.abandoned || []) out.add(x.id);
  for (const n of board?.hidden_needs || []) if (n.hidden?.kind === 'snoozed' && n.run_id) out.add(n.run_id);
  return out;
}

/**
 * The one filter every consumer reads. Returns a board with:
 *   needs_you      visible items only
 *   hidden_needs   done / snoozed items ({...need, hidden: {kind, at, until?}})
 *   runs, councils without abandoned paused runs
 *   abandoned      [{ id, title, tool, reason, at }] (collapsed group on the Board; restorable)
 * Needs of an abandoned run leave Needs-you altogether (the Abandoned group is their home).
 */
export function filterBoard(board, ack, nowMs = Date.now()) {
  if (!board || typeof board !== 'object') return board;
  const a = ack ? normalize(ack) : emptyAck();
  const abandoned = [];
  const gone = new Set();
  const keep = (r) => {
    const e = abandonedFor(a, r);
    if (!e) return true;
    gone.add(r.id); abandoned.push({ id: r.id, title: e.title || r.title || r.id, tool: e.tool || r.tool || null, reason: e.reason, at: e.at || null }); return false;
  };
  const runs = (board.runs || []).filter(keep), councils = (board.councils || []).filter(keep);
  const visible = [], hidden = [];
  for (const n of board.needs_you || []) {
    if (n.run_id && gone.has(n.run_id)) continue;
    const h = hiddenState(a, n, nowMs);
    if (h) hidden.push({ ...n, hidden: h }); else visible.push(n);
  }
  abandoned.sort((x, y) => (tms(y.at) ?? 0) - (tms(x.at) ?? 0));
  return { ...board, needs_you: visible, hidden_needs: hidden.slice(0, 100), runs, councils, abandoned: abandoned.slice(0, 30) };
}
