#!/usr/bin/env node
// boss.board/1: one aggregator for the Boss board. Spec: the Boss board brief ("Jobs", "Data layer").
//   buildBoard(sources, nowMs)  pure: no fs, no /proc, no clock. Liveness comes in through sources.probe.
//   loadSources(paths, nowMs)   reads the real sources, read-only, and returns the `sources` object.
//   CLI: node board.mjs --json | node board.mjs [--text]
// Reads: boss runs snapshots, ~/.claude/sessions/<pid>.json (pid, sessionId, cwd, name, status, statusUpdatedAt, procStart, startedAt only;
// never *.key, never a socket), orchestrator/leases, orchestrator/runs/<run>/{ledger,manifest}.jsonl, wfp/state/<sid>.json, boss/inbox/<pid>/, boss/ack.json (what the owner marked done / snoozed / moved on; see ack.mjs).
// Files per run (artifacts.mjs): run-folder listing (stat only), the snapshot's result paths, and the tail of each finished attempt's transcript
// (~/.claude/projects/*/<session>/subagents/agent-*.jsonl, named by the manifest's final stop event) for paths in its final message.
// Never reads metrics.db. Every string from a source passes through plain() (ANSI/control strip, length cap). Missing data is null, never 0.
// plain() duplicates vscode/src/safe.mts on purpose: this file is plain .mjs so the extension and the CLI can both import it.
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { liveness, listRuns, readSnapshot, procStartTicks, bootId } from '../lib/progress.mjs';
import paths from '../lib/paths.js'; // the one path resolver the hooks use (CommonJS; default import)
import { filterBoard, readAck, ackPath } from './ack.mjs';
import { collectArtifacts, FILES_MAX } from './artifacts.mjs';

export const SCHEMA = 'boss.board/1';
const DAY_MS = 86400000;
const TERMINAL = ['done', 'failed', 'cancelled'];
const MAX = { runs: 30, councils: 10, sessions: 60, needs: 100, attempts: 200 };

// ---------- text safety ----------
export function plain(s, max = 200) {
  if (typeof s !== 'string') return '';
  const t = s.replace(/\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '').replace(/[\x00-\x1f\x7f]/g, ' ').replace(/\s+/g, ' ').trim();
  return t.length <= max ? t : t.slice(0, max - 1) + '…';
}
const S = (s, n = 200) => plain(s, n) || null; // null when absent or empty
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
/** The snapshot's writer identity, trimmed: what the host needs to prove a pid is still the run's process before it signals it (council Stop). */
const writerOf = (w) => (w && typeof w === 'object' && !Array.isArray(w) ? { kind: S(w.kind, 16), pid: Number.isInteger(w.pid) ? w.pid : null, start_ticks: Number.isInteger(w.start_ticks) ? w.start_ticks : null, boot_id: S(w.boot_id, 64) } : null);
const iso = (ms) => (Number.isFinite(ms) ? new Date(ms).toISOString() : null);
const ms = (s) => { if (!s) return null; const t = Date.parse(s); return Number.isNaN(t) ? null : t; };
const base = (p) => (typeof p === 'string' && p ? path.basename(p.replace(/\/+$/, '')) : '');

/** "no progress for X" only when X is a real stretch of quiet (a minute or more); never "0s". */
function quietAge(sec) { return Number.isFinite(sec) && sec >= 60 ? fmtAgo(sec) : null; }
/** Plain statement for a run whose writer is gone (liveness status 'dead'). */
function orphanDetail(live) {
  const r = String(live.reason || '');
  const pid = /writer pid (\d+)/.exec(r)?.[1];
  let what = 'its writer stopped';
  if (/not running/.test(r)) what = pid ? `its writer (pid ${pid}) is no longer running` : 'its writer is no longer running';
  else if (/reused/.test(r)) what = pid ? `its writer (pid ${pid}) exited and the pid was reused` : 'its writer exited';
  else if (/earlier boot/.test(r)) what = 'its writer is from before the last reboot';
  else if (/owner session gone|orphaned/.test(r)) what = 'the session that owns it is gone';
  const q = quietAge(live.no_progress_s);
  return `Orphaned: ${what}${q ? `; last progress ${q} ago` : ''}`;
}
export function fmtAgo(sec) {
  if (sec == null || !Number.isFinite(sec)) return null;
  const s = Math.max(0, Math.round(sec));
  if (s < 90) return `${s}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

// ---------- glyphs and strip ----------
// Chat progress-view language: ● done ◐ running ○ pending ◆ gate ✕ blocked/failed ↻ retry, ┃ between waves. Extras: – skipped, ⊘ cancelled.
export function glyph(state, { gate = false, retry = false } = {}) {
  if (state === 'running') return retry ? '↻' : '◐';
  if (state === 'done') return '●';
  if (state === 'blocked' && gate) return '◆'; // a gate waiting on the owner (gaps_found) is a gate, not a blocked row
  if (state === 'failed' || state === 'blocked') return '✕';
  if (state === 'cancelled') return '⊘';
  if (state === 'skipped' || state === 'not_run') return '–';
  if (state === 'interrupted') return '⊘';
  if (state === 'queued' || state === 'waiting_input') return gate ? '◆' : state === 'waiting_input' ? '◆' : '○';
  return '○';
}
export function stripFor(waves) {
  return waves.filter((w) => w.rows.length).map((w) => w.rows.map((r) => `${r.glyph}${r.short}`).join(' ')).join(' ┃ ');
}

// ---------- run folding ----------
const rowShort = (id) => String(id).replace(/^(row|gate):/, '');
/** Plan row order: 2 < 3 < 3a < 3b < 3c < 4 < 10. */
export function rowCmp(a, b) {
  const pa = /^(\d+)([a-z]?)/.exec(String(a)) || [0, 1e9, String(a)], pb = /^(\d+)([a-z]?)/.exec(String(b)) || [0, 1e9, String(b)];
  return (+pa[1] - +pb[1]) || String(pa[2]).localeCompare(String(pb[2]));
}
const isRowKind = (k) => k === 'row' || k === 'gate';

/** How rows of a run that is not going anywhere read: `stale` = a row left "running" is shown as interrupted (its attempts too, so no live clock runs);
 *  `notRun` = a queued row will never launch (finished or Stopped run): "not run"; `held` = queued rows of a Stopping run: "held" (nothing new launches). */
const rowView = (v, state) => (v?.stale && state === 'running' ? 'interrupted' : v?.notRun && state === 'queued' ? 'not_run' : v?.held && state === 'queued' ? 'held' : state);
function foldOrchestrator(snap, ledger, view) {
  const items = Array.isArray(snap.items) ? snap.items.filter((i) => i && typeof i === 'object' && typeof i.id === 'string') : [];
  const attemptsByRow = new Map();
  for (const it of items) if (it.kind === 'attempt') { const a = attemptsByRow.get(it.parent) || []; a.push(it); attemptsByRow.set(it.parent, a); }
  const ledgerById = new Map((ledger || []).map((l) => [l.id, l]));
  let nAttempts = 0;
  const mkAttempt = (a) => {
    const m = a.meta || {}; const attemptId = m.attempt_id || a.label; const L = ledgerById.get(attemptId) || ledgerById.get(a.label);
    const arts = Array.isArray(a.artifacts) ? a.artifacts : [];
    const sha = arts.find((x) => x?.sha)?.sha ?? null;
    const result = arts.find((x) => x?.label === 'result' && x.path)?.path ?? null;
    const st = ms(a.started_at), en = ms(a.ended_at);
    nAttempts++;
    return {
      id: S(attemptId, 128) || S(a.id, 128), name: attemptId && a.label && a.label !== attemptId ? S(a.label, 200) : null, n: num(a.attempt), state: rowView(view, S(a.state, 20)),
      launched_at: S(m.launched_at, 32), tools: num(L?.tools) ?? num(m.tools), ms: num(L?.ms) ?? num(m.ms),
      agent: S(m.agent, 40), model: S(a.model, 40), effort: S(a.effort, 20),
      started_at: S(a.started_at, 32), ended_at: S(a.ended_at, 32), duration_s: st != null && en != null ? Math.round((en - st) / 1000) : (num(L?.ms) != null ? Math.round(L.ms / 1000) : null),
      tokens: num(L?.tokens) ?? num(m.tokens), first_pass: typeof L?.firstPass === 'boolean' ? L.firstPass : typeof m.first_pass === 'boolean' ? m.first_pass : null,
      escalated: typeof L?.escalated === 'boolean' ? L.escalated : typeof m.escalated === 'boolean' ? m.escalated : null,
      retries: num(L?.retries) ?? num(m.retries), sha: S(sha, 40), result: S(result, 400),
      tokens_source: L && num(L.tokens) != null ? 'ledger' : num(m.tokens) != null ? 'snapshot' : null,
    };
  };
  const mkRow = (r) => {
    const atts = (attemptsByRow.get(r.id) || []).map(mkAttempt);
    const last = atts[atts.length - 1];
    const rstate = rowView(view, S(r.state, 20) || 'queued');
    const retry = rstate === 'running' && atts.length > 1;
    const tokSum = atts.some((a) => a.tokens != null) ? atts.reduce((s, a) => s + (a.tokens ?? 0), 0) : null;
    const gate = r.kind === 'gate' || r.meta?.gate === true;
    const starts = atts.map((a) => ms(a.started_at)).filter((x) => x != null), ends = atts.map((a) => ms(a.ended_at)).filter((x) => x != null);
    const running = atts.filter((a) => a.state === 'running' && a.started_at);   // live clocks are rendered client-side from these timestamps, never sent as ticking numbers
    const durs = atts.filter((a) => a.state !== 'running' && a.duration_s != null);
    const launched = atts.map((a) => ms(a.launched_at) ?? ms(a.started_at)).filter((x) => x != null);
    const toolSum = atts.some((a) => a.tools != null) ? atts.reduce((n, a) => n + (a.tools ?? 0), 0) : null;
    return {
      id: S(r.id, 64), short: S(rowShort(r.id), 24) || '?', label: S(r.label, 200), state: rstate, gate, retry,
      glyph: glyph(rstate, { gate, retry }),
      model: last?.model ?? S(r.model, 40), effort: last?.effort ?? S(r.effort, 20), agent: last?.agent ?? S(r.meta?.agent, 60),
      plan_status: S(r.meta?.plan_status, 300), error: S(r.error, 400), detail: S(r.detail, 400),
      started_at: S(r.started_at, 32) || (starts.length ? iso(Math.min(...starts)) : null), ended_at: S(r.ended_at, 32) || (ends.length && rstate === 'done' ? iso(Math.max(...ends)) : null),
      updated_at: S(r.updated_at, 32),
      tokens: tokSum, sha: [...atts].reverse().find((a) => a.sha)?.sha ?? null, result: [...atts].reverse().find((a) => a.result)?.result ?? null,
      first_pass: last?.first_pass ?? null, escalated: atts.some((a) => a.escalated === true) ? true : atts.length && atts.every((a) => a.escalated === false) ? false : null,
      name: S(r.label, 200), added_at: launched.length ? iso(Math.min(...launched)) : null, tools: toolSum,
      duration_s: durs.length ? durs.reduce((n, a) => n + a.duration_s, 0) : null, live_since: running.length ? running[0].started_at : null,
      attempts: atts.slice(-6),
      attempt_count: atts.length,
    };
  };
  const waveItems = items.filter((i) => i.kind === 'wave');
  const waves = waveItems.map((w) => ({ id: S(w.id, 32), label: S(w.label, 40), state: S(w.state, 20), rows: [] }));
  const byWave = new Map(waveItems.map((w, i) => [w.id, waves[i]]));
  let loose = null;
  for (const r of items.filter((i) => isRowKind(i.kind))) {
    let w = byWave.get(r.parent);
    if (!w) { loose ||= { id: null, label: 'rows', state: null, rows: [] }; w = loose; }
    w.rows.push(mkRow(r));
  }
  if (loose) {
    // a row the plan table gave no wave (e.g. 3c) sits in the wave of the nearest earlier row, in plan order, instead of after every wave
    const withWave = waves.filter((w) => w.rows.length);
    if (!withWave.length) waves.push(loose);
    else for (const r of loose.rows) {
      const all = withWave.flatMap((w) => w.rows.map((x) => ({ x, w })));
      const before = all.filter((e) => rowCmp(e.x.short, r.short) < 0).sort((a, b) => rowCmp(b.x.short, a.x.short))[0];
      const w = before ? before.w : withWave[0];
      const at = w.rows.findIndex((x) => rowCmp(x.short, r.short) > 0);
      if (at < 0) w.rows.push(r); else w.rows.splice(at, 0, r);
    }
  }
  const rows = waves.flatMap((w) => w.rows);
  return { waves, rows, attemptCount: nAttempts };
}

// ---------- the board ----------
export function buildBoard(sources, nowMs = Date.now()) {
  const src = sources || {};
  const probe = src.probe || { bootId: () => null, startTicks: () => null, startEpoch: () => null };
  const now = nowMs / 1000;
  const dayStart = new Date(nowMs).setHours(0, 0, 0, 0); // local calendar day
  const markFor = (pid) => src.readMarks?.[pid];

  // --- sessions: alive ones only (pid still the same process), no claude-mem observers
  const aliveSession = (d) => {
    if (!d || !Number.isInteger(d.pid)) return false;
    if (probe.noProc) { // no /proc (macOS, Git Bash): no start ticks to compare, so the pid must answer and, when known, its start time must match
      if (!probe.alive?.(d.pid)) return false;
      if (d.startedAt && probe.startEpoch) { const e = probe.startEpoch(d.pid); return e != null && Math.abs(e - d.startedAt / 1000) <= 30; } // a start time that cannot be read is not a match: hidden, never a reused pid shown as live
      return true;
    }
    const ticks = probe.startTicks(d.pid);
    if (ticks == null) return false;
    if (d.procStart != null && d.procStart !== '') return String(ticks) === String(d.procStart);
    if (d.startedAt && probe.startEpoch) { const e = probe.startEpoch(d.pid); return e != null && Math.abs(e - d.startedAt / 1000) <= 30; }
    return true;
  };
  const rawSessions = (src.sessions || []).filter((d) => aliveSession(d) && !String(d.cwd || '').includes('/observer-sessions'));
  const bySid = new Map(rawSessions.map((d) => [d.sessionId, d]));
  const sessProbe = {
    bootId: probe.bootId, startTicks: probe.startTicks,
    session: (owner) => {
      const d = (owner?.session_id && bySid.get(owner.session_id)) || (owner?.pid && rawSessions.find((x) => x.pid === owner.pid));
      return d ? { pid: d.pid, status: ['busy', 'idle'].includes(d.status) ? d.status : 'unknown' } : null;
    },
  };

  // --- ledger by run (attempt tokens), launches today by run/session
  const ledger = src.ledger || {};
  const launches = Array.isArray(src.launches) ? src.launches : null; // [{run, attempt, session, ts}]
  const sessionOfAttempt = new Map((launches || []).map((l) => [l.attempt, l.session]));

  // --- files a run produced (collected by loadSources): newest first, capped; a filename's row prefix counts only when that row exists
  const filesFor = (snap, id, rows) => {
    const all = src.artifacts?.[`${snap.tool}/${id}`];
    if (!Array.isArray(all)) return { artifacts: [], artifacts_total: 0 };
    const known = new Set(rows.map((r) => r.short));
    const items = all.filter((a) => a && typeof a.path === 'string' && a.path.length <= 600 && !/[\x00-\x1f\x7f]/.test(a.path)).map((a) => ({
      path: a.path, name: S(a.name || base(a.path), 120), kind: S(a.kind, 12), row: a.row && known.has(a.row) ? a.row : a.prefix_row && known.has(a.prefix_row) ? a.prefix_row : null,
      via: S(a.via?.[0], 12), size: num(a.size), mtime_ms: num(a.mtimeMs),
    })).sort((x, y) => (y.mtime_ms ?? 0) - (x.mtime_ms ?? 0) || x.path.localeCompare(y.path));
    return { artifacts: items.slice(0, FILES_MAX), artifacts_total: items.length };
  };

  // --- runs and councils
  const runs = [], councils = [], needs = [];
  const snapByRun = new Map();
  const stoppedRuns = new Set(); // runs the owner stopped with nothing left running: they need nothing from the owner
  const add = (n) => needs.push({ run_id: null, session: null, since: null, ...n });

  for (const e of src.snapshots || []) {
    const snap = e?.snap;
    if (!snap || typeof snap !== 'object' || Array.isArray(snap)) continue;
    const id = S(snap.run_id, 128); if (!id) continue;
    snapByRun.set(`${snap.tool}/${id}`, snap); if (!snapByRun.has(id)) snapByRun.set(id, snap);
    const live = liveness(snap, { now, probe: sessProbe, resumedAt: src.resumedAt ?? null });
    const paused = snap.paused === true || (snap.state === 'waiting_input' && /^paused/i.test(String(snap.phase || '')));
    const terminal = TERMINAL.includes(snap.state);
    const endMs = ms(snap.ended_at) ?? ms(snap.updated_at) ?? (e.mtimeMs ?? null);
    if (terminal && (endMs == null || nowMs - endMs > DAY_MS)) continue; // active + last 24 h
    const items = Array.isArray(snap.items) ? snap.items : [];
    const runningRows = items.filter((i) => i && (isRowKind(i.kind) || i.kind === 'seat') && i.state === 'running');
    const runningRowIds = new Set(runningRows.map((i) => i.id));
    // a running attempt under a row that is still queued counts too: the run is working, so its session is not idle
    const running0 = runningRows.length + items.filter((i) => i && i.kind === 'attempt' && i.state === 'running' && !runningRowIds.has(i.parent)).length;
    const running = paused || terminal ? 0 : running0; // rows left "running" by a pause are stale
    // Owner Stop (orchestrator runs only): the marker blocks new launches, it kills nothing. Stopping = marker + a live attempt still finishing;
    // Stopped = marker + nothing running (a paused run, or a dead writer, has nothing left to finish). Derived here at read time, never written to the snapshot.
    const cancel = snap.tool === 'orchestrator' && !terminal && e.cancel && typeof e.cancel === 'object' ? e.cancel : null;
    const stop = cancel ? { state: running > 0 && live.status !== 'dead' ? 'stopping' : 'stopped', since: S(cancel.requested_at, 32), by: S(cancel.by, 40), reason: S(cancel.reason, 80), nonce: S(cancel.nonce, 16) } : null;
    if (stop?.state === 'stopped') stoppedRuns.add(id);
    const ownerSid = snap.owner?.session_id;
    const ownerLive = ownerSid ? bySid.get(ownerSid) : null;
    const common = {
      id, tool: S(snap.tool, 32), title: S(snap.title, 200), summary: S(snap.summary, 300), state: S(snap.state, 20), status: live.status, status_reason: S(live.reason, 120),
      paused, stop, phase: S(snap.phase, 80), cwd: S(snap.cwd, 300), source_dir: S(snap.source_dir, 400), events_path: S(snap.events_path, 400),
      created_at: S(snap.created_at, 32), updated_at: S(snap.updated_at, 32), ended_at: S(snap.ended_at, 32), last_progress_at: S(snap.last_progress_at, 32),
      no_progress_s: live.no_progress_s, quiet: live.quiet, running,
      counts: (Array.isArray(snap.counts) ? snap.counts : []).slice(0, 6).map((c) => ({ unit: S(c?.unit, 16), label: S(c?.label, 40), done: num(c?.done), total: num(c?.total), running: num(c?.running), failed: num(c?.failed) })),
      owner: ownerSid ? { session_id: S(ownerSid, 64), name: ownerLive ? S(ownerLive.name, 80) : null, alive: !!ownerLive, pid: ownerLive ? ownerLive.pid : null, status: ownerLive && ['busy', 'idle'].includes(ownerLive.status) ? ownerLive.status : null } : null,
    };
    const planPath = common.source_dir ? `${common.source_dir}/PLAN.md` : null;

    if (snap.tool === 'council') {
      const seats = items.filter((i) => i?.kind === 'seat');
      const replaced = new Set(items.map((i) => (typeof i?.replaces === 'string' ? i.replaces.split('/')[0] : null)).filter(Boolean));
      const seatRows = seats.map((s) => {
        const m = s.meta || {}; const sub = m.substituted;
        const standIn = items.find((i) => i?.kind === 'stand-in' && i.parent === s.id);
        return {
          id: S(s.id, 64), label: S(s.label, 60), model: S(s.model, 80), provider: S(m.provider, 40), state: S(s.state, 20),
          phase: S(m.phase, 30), elapsed_s: num(m.elapsed_s), last_byte_age_s: num(m.last_byte_age_s), out_bytes: num(m.out_bytes),
          detail: S(s.detail, 300), error: S(s.error, 300),
          stand_in: sub || standIn ? { model: S(sub?.model ?? standIn?.model, 80), reason: S(sub?.reason ?? standIn?.detail, 300) } : null,
          glyph: glyph(s.state),
        };
      });
      const chairItem = items.find((i) => i?.kind === 'chair');
      const chair = chairItem ? { state: S(chairItem.state, 20), model: S(chairItem.model, 80), glyph: glyph(chairItem.state), detail: S(chairItem.detail, 300), error: S(chairItem.error, 300) } : null;
      const roundN = /(?:^|-)round-(\d+)/i.exec(id)?.[1] ?? /^round\s+(\d+)/i.exec(common.title || '')?.[1] ?? null;
      const topic = S((common.title || '').replace(/^round\s+\d+\s*/i, ''), 200) || common.title;
      councils.push({ ...common, ...filesFor(snap, id, []), writer: writerOf(snap.writer), topic, round: roundN ? Number(roundN) : null, agents: seatRows.length + (chair ? 1 : 0), seats: seatRows, chair });
      if (!terminal) {
        for (const s of seats) if (s.state === 'failed' && !replaced.has(s.id) && !items.some((i) => i?.kind === 'stand-in' && i.parent === s.id))
          add({ kind: 'failed', run_id: id, title: `${common.title || id}: seat ${S(s.label, 40)} failed`, detail: S(s.error || s.detail, 300) || 'seat failed, no stand-in', since: S(s.ended_at || snap.updated_at, 32), action: { type: 'open_file', path: common.source_dir } });
        if (chair?.state === 'failed') add({ kind: 'failed', run_id: id, title: `${common.title || id}: chair failed`, detail: chair.error || chair.detail, since: S(snap.updated_at, 32), action: { type: 'open_file', path: common.source_dir } });
      }
    } else {
      // a finished or Stopped run launches nothing: its rows read as their last state ("interrupted", "not run"), never running or queued; a paused run's running rows are stale too
      const folded = foldOrchestrator(snap, ledger[id], { stale: terminal || paused || stop?.state === 'stopped', notRun: terminal || stop?.state === 'stopped', held: stop?.state === 'stopping' });
      const strip = plain(stripFor(folded.waves), 800);
      const pending = folded.rows.filter((r) => r.state === 'queued').slice(0, 3).map((r) => ({ id: r.id, short: r.short, label: r.label }));
      const toks = folded.rows.some((r) => r.tokens != null) ? folded.rows.reduce((s, r) => s + (r.tokens ?? 0), 0) : null;
      const fp = folded.rows.filter((r) => r.first_pass != null);
      const liveAtts = folded.rows.flatMap((r) => r.attempts.filter((a) => a.state === 'running' && a.started_at).map((a) => ({ id: r.id, short: r.short, name: r.name || r.label, model: a.model, effort: a.effort, since: a.started_at })))
        .sort((a, b) => ms(a.since) - ms(b.since));
      const activeDone = folded.rows.flatMap((r) => r.attempts).filter((a) => a.state !== 'running' && a.duration_s != null).reduce((n, a) => n + a.duration_s, 0);
      runs.push({
        ...common, ...filesFor(snap, id, folded.rows), goal: common.title, waves: folded.waves, strip, next_pending: pending,
        time: { active_done_s: activeDone, live: terminal || paused ? [] : liveAtts.slice(0, 8) },
        totals: { rows: folded.rows.length, attempts: folded.attemptCount, tokens: toks, first_pass: fp.length ? fp.filter((r) => r.first_pass).length : null, first_pass_of: fp.length || null },
      });
      if (!terminal && !paused) {
        const seen = new Set();
        // a gate is resolved once the gate row is done or any later row in the run has finished (rows are in plan order)
        const resolvedGate = (rowId) => {
          const i = folded.rows.findIndex((r) => r.id === rowId);
          return i >= 0 && folded.rows[i].gate && folded.rows.slice(i).some((r) => r.state === 'done');
        };
        for (const a of Array.isArray(snap.attention) ? snap.attention : []) {
          if (!a || typeof a !== 'object') continue;
          if (a.item) seen.add(a.item);
          const isGate = a.kind === 'approval';
          if (isGate && a.item && resolvedGate(a.item)) continue;
          const options = (Array.isArray(a.options) ? a.options : []).map((o) => S(o, 60)).filter(Boolean).slice(0, 8);
          add({ kind: isGate ? 'gate' : 'attention', run_id: id, title: S(folded.rows.find((r) => r.id === a.item)?.label, 160) || common.title || id, detail: S(a.text, 300), since: S(a.since, 32) || S(snap.updated_at, 32), action: { type: 'open_file', path: planPath }, ...(options.length ? { options } : {}) });
        }
        for (const r of folded.rows) {
          if (seen.has(r.id) || (r.gate && resolvedGate(r.id))) continue;
          const where = r.result || planPath;
          if (r.state === 'blocked') add({ kind: 'blocked', run_id: id, title: r.label || r.id, detail: r.error || r.detail || r.plan_status || 'row blocked', since: r.updated_at || r.ended_at || common.updated_at, action: { type: 'open_file', path: where } });
          else if (r.state === 'waiting_input') add({ kind: r.gate ? 'gate' : 'blocked', run_id: id, title: r.label || r.id, detail: r.detail || r.plan_status || 'waiting for you', since: r.updated_at || common.updated_at, action: { type: 'open_file', path: where } });
          else if (r.state === 'failed') add({ kind: 'failed', run_id: id, title: r.label || r.id, detail: r.error || r.detail || r.plan_status || 'row failed', since: r.ended_at || r.updated_at || common.updated_at, action: { type: 'open_file', path: where } });
        }
      }
    }
    // run-level: paused, stalled/dead, failed
    if (paused && !terminal) add({ kind: 'paused_run', run_id: id, title: common.title || id, detail: 'paused (lease released), plan unfinished', since: common.updated_at, action: { type: 'copy', text: snap.tool === 'orchestrator' ? `/orch-resume ${id}` : `python3 ~/.claude/council/council.py status --run ${common.source_dir || ''}` } });
    else if (!terminal && live.status === 'dead') {
      // the process that writes progress is gone: that is an orphaned run, not a stalled one
      add({ kind: 'orphaned', run_id: id, title: common.title || id, detail: orphanDetail(live), since: S(snap.last_progress_at, 32) || common.updated_at, action: { type: 'open_file', path: common.events_path || common.source_dir } });
      common.stalled = true;
    } else if (!terminal && live.status === 'stalled') {
      const npAge = quietAge(live.no_progress_s);
      add({ kind: 'stalled', run_id: id, title: common.title || id, detail: `${live.reason || live.status}${npAge ? `; no progress for ${npAge}` : ''}`, since: S(snap.last_progress_at, 32) || common.updated_at, action: { type: 'open_file', path: common.events_path || common.source_dir } });
      common.stalled = true;
    } else if (!terminal && snap.tool === 'council' && live.quiet) {
      add({ kind: 'stalled', run_id: id, title: common.title || id, detail: quietAge(live.no_progress_s) ? `no new output for ${quietAge(live.no_progress_s)}` : 'no new output', since: S(snap.last_progress_at, 32) || common.updated_at, action: { type: 'open_file', path: common.source_dir } });
    }
    if (snap.state === 'failed') add({ kind: 'failed', run_id: id, title: common.title || id, detail: S(snap.summary, 300) || 'run failed', since: S(snap.ended_at || snap.updated_at, 32), action: { type: 'open_file', path: common.events_path || common.source_dir } });
  }
  const runById = new Map(runs.map((r) => [r.id, r]));
  const stalledRuns = new Set(needs.filter((n) => n.kind === 'stalled' || n.kind === 'orphaned').map((n) => n.run_id));

  // --- leases -> session map
  const leasesBySid = new Map();
  for (const l of src.leases || []) {
    if (!l || typeof l.run !== 'string' || typeof l.session !== 'string') continue;
    const a = leasesBySid.get(l.session) || []; a.push(l); leasesBySid.set(l.session, a);
  }

  // --- inbox
  const inboxFor = (pid) => {
    const ib = src.inbox?.[pid]; if (!ib) return null;
    const mark = markFor(pid); const cutoff = mark != null ? mark : nowMs - DAY_MS;
    const unread = (ib.replies || []).filter((r) => (ms(r.at) ?? 0) > cutoff);
    return { unread: unread.length, basis: mark != null ? 'marks' : 'last24h', pending: num(ib.pending), first: unread[unread.length - 1] || null, lastAt: unread.length ? unread[unread.length - 1].at : null };
  };

  // --- sessions
  const today = { date: new Date(dayStart).toDateString(), start: iso(dayStart), runs: [], sessions: [], tokens: null, attempts: null, launches: null, note: 'local calendar day; session launches_total is the wfp counter for the whole session' };
  const sessions = [];
  for (const d of rawSessions) {
    const cwd = S(d.cwd, 300);
    const status = ['busy', 'idle'].includes(d.status) ? d.status : null;
    const since = num(d.statusUpdatedAt) != null ? iso(d.statusUpdatedAt) : null;
    const leases = (leasesBySid.get(d.sessionId) || []).map((l) => {
      const exp = ms(l.expires);
      return { run: S(l.run, 128), expired: exp != null ? exp < nowMs : null, renewed: S(l.renewed || l.acquired, 32) };
    });
    const runIds = leases.map((l) => l.run);
    const heldRuns = runIds.map((r) => runById.get(r)).filter(Boolean);
    const runningRows = heldRuns.flatMap((r) => r.waves.flatMap((w) => w.rows).filter((x) => x.state === 'running').map((x) => ({ run_id: r.id, row: x.short, label: x.label, model: x.model, effort: x.effort })));
    const ib = inboxFor(d.pid);
    const w = src.wfp?.[d.sessionId]; const wp = w?.policy;
    const sess = {
      pid: d.pid, session_id: S(d.sessionId, 64), name: S(d.name, 80), cwd, project: base(cwd) || null, status, since,
      lease_runs: leases, running: runningRows, current: runningRows[0] || null,
      unread: ib ? ib.unread : null, unread_basis: ib ? ib.basis : null, inbox_pending: ib ? ib.pending : null,
      launches_total: num(w?.launches), max_launches: num(wp?.maxLaunches),
    };
    sessions.push(sess);

    // needs: idle with an unfinished leased run and nothing running in it
    if (status === 'idle') for (const r of heldRuns) {
      const run = r; const term = TERMINAL.includes(run.state);
      if (term || run.paused || run.running > 0 || stalledRuns.has(run.id)) continue;
      add({ kind: 'idle_run_unfinished', run_id: run.id, session: sess.session_id, title: `${sess.name || sess.project || 'session'}: idle, run unfinished`, detail: `${run.title || run.id}${run.summary ? ` · ${run.summary}` : ''}`, since: since, action: { type: 'focus', pid: d.pid, session_id: sess.session_id, name: sess.name, cwd } });
    }
    if (sess.launches_total != null && sess.max_launches != null && sess.launches_total >= sess.max_launches)
      add({ kind: 'launch_cap', session: sess.session_id, title: `${sess.name || sess.project || 'session'}: launch cap reached`, detail: `${sess.launches_total}/${sess.max_launches} launches (wfp)`, since: null, action: { type: 'focus', pid: d.pid, session_id: sess.session_id, name: sess.name, cwd } });
    if (ib && ib.unread > 0)
      add({ kind: 'unread_inbox', session: sess.session_id, title: `${sess.name || sess.project || 'session'}: ${ib.unread} unread repl${ib.unread === 1 ? 'y' : 'ies'}`, detail: S(ib.first?.text, 200) || 'reply waiting', since: S(ib.lastAt, 32), action: { type: 'message', pid: d.pid, session_id: sess.session_id, name: sess.name } });
  }

  // --- lease conflicts
  for (const l of src.leases || []) {
    if (!l || typeof l.run !== 'string' || typeof l.session !== 'string') continue;
    const run = runById.get(l.run); const rawSnap = snapByRun.get(l.run);
    const holder = bySid.get(l.session); const exp = ms(l.expires);
    const unexpired = exp != null && exp > nowMs;
    const live = rawSnap && !TERMINAL.includes(rawSnap.state) && !(run?.paused);
    if (stalledRuns.has(l.run)) continue;
    const since = S(l.renewed || l.acquired, 32);
    const ownerSid = rawSnap?.owner?.session_id;
    if (!holder && (live || unexpired)) {
      add({ kind: 'lease_conflict', run_id: S(l.run, 128), title: `${S(l.run, 128)}: lease held by a dead session`, detail: `holder ${S(l.session, 8)} is gone${unexpired ? '; lease not yet expired' : ''}`, since, action: { type: 'copy', text: `node ~/.claude/orchestrator/manifest.js activity --run ${S(l.run, 128)}` } });
    } else if (holder && ownerSid && ownerSid !== l.session && bySid.get(ownerSid) && live) {
      add({ kind: 'lease_conflict', run_id: S(l.run, 128), title: `${S(l.run, 128)}: two sessions claim the run`, detail: `lease: ${S(holder.name, 40) || S(l.session, 8)}; snapshot owner: ${S(bySid.get(ownerSid).name, 40) || S(ownerSid, 8)}`, since, action: { type: 'copy', text: `node ~/.claude/orchestrator/manifest.js activity --run ${S(l.run, 128)}` } });
    }
  }

  // --- today
  const runToday = new Map();
  const sessTok = new Map(), sessAttempts = new Map(), sessLaunches = new Map();
  let totTok = null, totAtt = 0, anyLedger = false;
  for (const [run, lines] of Object.entries(ledger)) {
    for (const l of lines || []) {
      const t = ms(l.ts); if (t == null || t < dayStart) continue;
      const r = runToday.get(run) || { run_id: S(run, 128), tokens: null, attempts: 0, first_pass: 0, launches: null };
      r.attempts++; totAtt++; anyLedger = true;
      if (num(l.tokens) != null) { r.tokens = (r.tokens ?? 0) + l.tokens; totTok = (totTok ?? 0) + l.tokens; }
      if (l.firstPass === true) r.first_pass++;
      runToday.set(run, r);
      const sid = sessionOfAttempt.get(l.id);
      if (sid) { sessAttempts.set(sid, (sessAttempts.get(sid) || 0) + 1); if (num(l.tokens) != null) sessTok.set(sid, (sessTok.get(sid) || 0) + l.tokens); }
    }
  }
  if (launches) for (const l of launches) {
    const t = ms(l.ts); if (t == null || t < dayStart) continue;
    const r = runToday.get(l.run) || { run_id: S(l.run, 128), tokens: null, attempts: 0, first_pass: 0, launches: null };
    r.launches = (r.launches ?? 0) + 1; runToday.set(l.run, r);
    if (l.session) sessLaunches.set(l.session, (sessLaunches.get(l.session) || 0) + 1);
  }
  today.runs = [...runToday.values()].sort((a, b) => (b.tokens ?? -1) - (a.tokens ?? -1)).slice(0, 30);
  today.tokens = anyLedger ? totTok : null;
  today.attempts = anyLedger ? totAtt : null;
  today.launches = launches ? [...sessLaunches.values()].reduce((a, b) => a + b, 0) + (launches.filter((l) => !l.session && (ms(l.ts) ?? 0) >= dayStart).length) : null;
  for (const s of sessions) {
    const sid = s.session_id; const hasMap = launches != null && sessLaunches.has(sid);
    today.sessions.push({
      session_id: sid, name: s.name, launches_today: hasMap ? sessLaunches.get(sid) : null, launches_total: s.launches_total, max_launches: s.max_launches,
      tokens: sessTok.has(sid) ? sessTok.get(sid) : null, attempts: sessAttempts.has(sid) ? sessAttempts.get(sid) : null,
    });
  }

  // --- order
  const rank = { gate: 0, blocked: 1, failed: 2, orphaned: 3, stalled: 3, idle_run_unfinished: 4, launch_cap: 5, lease_conflict: 6, paused_run: 7, attention: 8, unread_inbox: 9 };
  for (const n of needs) {
    n.title = plain(n.title, 200); n.detail = plain(n.detail || '', 300) || null; n.id = `${n.kind}:${n.run_id || n.session || ''}:${plain(n.title, 40)}`;
    if (n.action?.path === undefined && n.action?.type === 'open_file') n.action = { ...n.action, path: null };
    // every item keeps one primary action: with no file to open, copy the resume command (or the title when no run is known)
    if (n.action?.type === 'open_file' && !n.action.path) {
      const tool = n.run_id ? (snapByRun.get(n.run_id)?.tool ?? null) : null;
      n.action = n.run_id && tool !== 'council' ? { type: 'copy', text: `/orch-resume ${n.run_id}` } : { type: 'copy', text: n.title };
    }
  }
  // one session can appear twice (a resumed conversation whose old process is still alive): session-scoped items are listed once
  const seenSess = new Set();
  const uniq = needs.filter((n) => {
    if (!n.session || (n.kind !== 'launch_cap' && n.kind !== 'idle_run_unfinished')) return true;
    const key = `${n.kind}|${n.session}|${n.run_id || ''}`;
    if (seenSess.has(key)) return false; seenSess.add(key); return true;
  });
  needs.length = 0; needs.push(...uniq.filter((n) => !(n.run_id && stoppedRuns.has(n.run_id))));   // a Stopped run raises no gate, paused, stalled, orphaned, idle or lease item
  needs.sort((a, b) => {
    const ta = ms(a.since), tb = ms(b.since);
    if (ta == null && tb == null) return (rank[a.kind] ?? 99) - (rank[b.kind] ?? 99);
    if (ta == null) return 1; if (tb == null) return -1;
    return ta - tb || (rank[a.kind] ?? 99) - (rank[b.kind] ?? 99);
  });
  const rs = (a, b) => (ms(b.updated_at) ?? 0) - (ms(a.updated_at) ?? 0);
  runs.sort((a, b) => (TERMINAL.includes(a.state) ? 1 : 0) - (TERMINAL.includes(b.state) ? 1 : 0) || rs(a, b));
  councils.sort(rs);
  sessions.sort((a, b) => (a.project || '').localeCompare(b.project || '') || (a.name || '').localeCompare(b.name || '') || a.pid - b.pid);

  // what the owner already dealt with (src.ack, from ack.json): done / snoozed needs move to hidden_needs, abandoned paused runs leave runs. One filter for every consumer.
  return filterBoard({
    schema: SCHEMA, generated_at: iso(nowMs),
    needs_you: needs.slice(0, MAX.needs), sessions: sessions.slice(0, MAX.sessions), runs: runs.slice(0, MAX.runs), councils: councils.slice(0, MAX.councils), today,
  }, src.ack || null, nowMs);
}

// ---------- loader (read-only) ----------
export function defaultPaths(env = process.env) {
  // Same resolver the hooks use (lib/paths.js): ORCH_DIR / BOSS_DATA / legacy ~/.claude/orchestrator / <config>/boss-data.
  // The BOSS_* names below stay as explicit per-source overrides.
  const bossDir = env.BOSS_PROGRESS_DIR || paths.progressRoot(env);
  return {
    boss: bossDir,
    sessions: env.BOSS_SESSIONS_DIR || paths.sessionsDir(env),
    leases: paths.leasesDir(env), runs: paths.runsDir(env),
    wfp: env.BOSS_WFP_STATE || paths.wfpStateDir(env),
    inbox: env.BOSS_INBOX_DIR || path.join(bossDir, 'inbox'),
  };
}
/** The owner's Stop marker for a run (<registry dir>/control/cancel, written by the Boss extension): null when absent; present but unreadable still counts. */
function readCancel(dir) {
  const f = path.join(dir, 'control', 'cancel');
  let st; try { st = fs.lstatSync(f); } catch { return null; }
  let j = null;
  if (st.isFile() && st.size <= 4096) { try { const o = JSON.parse(fs.readFileSync(f, 'utf8')); if (o && typeof o === 'object' && !Array.isArray(o)) j = o; } catch { /* malformed: still a stop */ } }
  return { requested_at: typeof j?.requested_at === 'string' && !Number.isNaN(Date.parse(j.requested_at)) ? j.requested_at : iso(st.mtimeMs), by: j?.by ?? null, reason: j?.reason ?? null, nonce: j?.nonce ?? null };
}
const readJson = (f) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return null; } };
const readLines = (f, tail = 0) => {
  try {
    let txt;
    if (tail) { const st = fs.statSync(f); const fd = fs.openSync(f, 'r'); const len = Math.min(st.size, tail); const buf = Buffer.alloc(len); fs.readSync(fd, buf, 0, len, st.size - len); fs.closeSync(fd); txt = buf.toString('utf8'); }
    else txt = fs.readFileSync(f, 'utf8');
    return txt.split('\n').flatMap((l) => { if (!l) return []; try { return [JSON.parse(l)]; } catch { return []; } });
  } catch { return []; }
};

const hasProc = () => fs.existsSync('/proc/self/stat');
/** Does the pid answer? Signal 0; EPERM means it exists but is not ours. Used where /proc cannot say. */
function pidAnswers(pid) { try { process.kill(pid, 0); return true; } catch (e) { return e?.code === 'EPERM'; } }

export function realProbe() {
  if (!hasProc()) { // no /proc: liveness by signal 0, start time from ps (null when ps cannot say); the identity checks that need start ticks stay unknown
    const startEpoch = (pid) => {
      try { const t = Date.parse(execFileSync('ps', ['-o', 'lstart=', '-p', String(Number(pid))], { encoding: 'utf8', timeout: 2000, env: { ...process.env, LC_ALL: 'C' }, stdio: ['ignore', 'pipe', 'ignore'] }).trim()); return Number.isNaN(t) ? null : t / 1000; } catch { return null; }
    };
    return { noProc: true, bootId, startTicks: () => null, startEpoch, alive: pidAnswers };
  }
  let btime;
  const startEpoch = (pid) => {
    const t = procStartTicks(pid); if (t == null) return null;
    if (btime === undefined) { try { const m = fs.readFileSync('/proc/stat', 'utf8').match(/^btime (\d+)$/m); btime = m ? Number(m[1]) : null; } catch { btime = null; } }
    return btime == null ? null : btime + t / 100;
  };
  return { bootId, startTicks: procStartTicks, startEpoch };
}

/** Files per run (artifacts.mjs), cached a few seconds per run: the extension rebuilds the board about every 2 s and the folder and transcript reads need not repeat that often. */
const artifactCache = new Map();
function loadArtifacts(paths, snapshots, finals, nowMs) {
  const ttl = paths.artifactTtlMs ?? 6000;
  const home = os.homedir();
  const councilRuns = process.env.BOSS_COUNCIL_RUNS || path.join(home, '.claude-council', 'runs');
  const roots = [paths.runs, councilRuns].map((r) => { try { return fs.realpathSync(r); } catch { return null; } }).filter(Boolean);
  const projectsDir = process.env.BOSS_PROJECTS_DIR || path.join(home, '.claude', 'projects');
  const out = {};
  for (const e of snapshots) {
    const snap = e.snap, id = snap?.run_id;
    if (typeof id !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(id)) continue;
    const key = `${snap.tool}/${id}`;
    const dirs = [path.join(paths.runs, id), snap.source_dir].filter((d) => typeof d === 'string');
    const sig = `${key}|${dirs.join('|')}|${paths.runs}`;
    const hit = artifactCache.get(sig);
    if (hit && ttl > 0 && nowMs >= hit.at && nowMs - hit.at < ttl) { out[key] = hit.items; continue; }
    const items = collectArtifacts({ dirs, snap, events: finals[id] || [], roots, home, projectsDir });
    if (artifactCache.size > 200) artifactCache.clear();
    artifactCache.set(sig, { at: nowMs, items }); out[key] = items;
  }
  return out;
}

export function loadSources(paths = defaultPaths(), nowMs = Date.now()) {
  const dayStart = new Date(nowMs).setHours(0, 0, 0, 0);
  // snapshots (same size cap as the extension reader)
  const snapshots = [];
  let dirs = []; try { dirs = listRuns(paths.boss); } catch { dirs = []; }
  for (const dir of dirs) {
    let st; try { st = fs.statSync(path.join(dir, 'progress.json')); } catch { continue; }
    if (st.size > 600 * 1024) continue;
    const snap = readSnapshot(dir); if (snap) snapshots.push({ dir, snap, mtimeMs: st.mtimeMs, cancel: readCancel(dir) });
  }
  // sessions: only files whose pid dir exists (cheap pre-filter; buildBoard re-checks start time)
  const sessions = []; const procOk = hasProc();
  let names = []; try { names = fs.readdirSync(paths.sessions).filter((n) => /^\d+\.json$/.test(n)); } catch { names = []; }
  for (const n of names) {
    const pid = Number(n.slice(0, -5)); if (!(procOk ? fs.existsSync(`/proc/${pid}`) : pidAnswers(pid))) continue;
    const d = readJson(path.join(paths.sessions, n)); if (!d || d.pid !== pid) continue;
    const { sessionId, cwd, name, status, statusUpdatedAt, procStart, startedAt } = d; // the only fields read
    sessions.push({ pid, sessionId, cwd, name, status, statusUpdatedAt, procStart, startedAt });
  }
  // leases
  const leases = [];
  try { for (const n of fs.readdirSync(paths.leases)) if (n.endsWith('.json')) { const l = readJson(path.join(paths.leases, n)); if (l && l.v === 1) leases.push(l); } } catch { /* none */ }
  // wfp: live sessions only
  const wfp = {};
  for (const s of sessions) if (s.sessionId && /^[A-Za-z0-9-]{8,64}$/.test(s.sessionId)) { const w = readJson(path.join(paths.wfp, `${s.sessionId}.json`)); if (w && w.policy) wfp[s.sessionId] = { launches: w.launches, policy: { maxLaunches: w.policy.maxLaunches } }; }
  // ledger + launches: runs with a live snapshot, or ledger/manifest touched today
  const want = new Set(snapshots.map((e) => e.snap.run_id).filter((r) => typeof r === 'string' && /^[A-Za-z0-9._-]{1,128}$/.test(r)));
  let runDirs = []; try { runDirs = fs.readdirSync(paths.runs); } catch { runDirs = []; }
  const ledger = {}; const launches = []; let sawManifest = false;
  const finals = {};   // run -> final stop events (where each finished attempt's transcript is), for the files lists
  for (const r of runDirs) {
    const lf = path.join(paths.runs, r, 'ledger.jsonl'), mf = path.join(paths.runs, r, 'manifest.jsonl');
    let lm = 0, mm = 0; try { lm = fs.statSync(lf).mtimeMs; } catch { /* none */ } try { mm = fs.statSync(mf).mtimeMs; } catch { /* none */ }
    if (lm && (want.has(r) || lm >= dayStart)) ledger[r] = readLines(lf);
    if (mm && (mm >= dayStart || want.has(r))) {
      const evs = readLines(mf);
      if (mm >= dayStart) { sawManifest = true; const voided = new Set(evs.filter((e) => e?.type === 'void' && e.attempt).map((e) => e.attempt)); for (const e of evs) if (e?.type === 'launch' && typeof e.ts === 'string' && !voided.has(e.attempt)) launches.push({ run: r, attempt: e.attempt, session: e.session ?? null, ts: e.ts }); }
      if (want.has(r)) finals[r] = evs.filter((e) => e?.type === 'stop' && e.final === true && e.attempt).map((e) => ({ attempt: e.attempt, row: e.row ?? null, session: e.session ?? null, task_id: e.task_id ?? null, output_file: e.output_file ?? null }));
    }
  }
  const artifacts = loadArtifacts(paths, snapshots, finals, nowMs);
  // inbox for live sessions
  const inbox = {};
  for (const s of sessions) {
    const dir = path.join(paths.inbox, String(s.pid)); let ents;
    try { ents = fs.readdirSync(dir); } catch { continue; }
    const meta = readJson(path.join(dir, 'meta.json'));
    if (meta && meta.start_ticks != null && procStartTicks(s.pid) !== meta.start_ticks) continue; // pid reused
    const rf = path.join(dir, 'replies.jsonl'); let replies = [];
    try { const st = fs.lstatSync(rf); if (st.isFile() && st.uid === process.getuid?.()) replies = readLines(rf, 64 * 1024).filter((r) => typeof r?.msg_id === 'string' && typeof r.text === 'string').map((r) => ({ msg_id: r.msg_id, text: r.text, at: typeof r.at === 'string' ? r.at : '' })); } catch { /* none */ }
    inbox[s.pid] = { replies, pending: ents.filter((n) => /\.json$/.test(n) && n !== 'meta.json').length };
  }
  const ack = readAck(paths.ack || ackPath(paths.boss));
  return { snapshots, sessions, leases, wfp, ledger, artifacts, launches: sawManifest || launches.length ? launches : [], inbox, ack, probe: realProbe() };
}

// ---------- text mode ----------
export function renderText(b) {
  const L = [];
  L.push(`boss board ${b.generated_at}`);
  L.push(`needs you (${b.needs_you.length})`);
  for (const n of b.needs_you.slice(0, 12)) L.push(`  ! ${n.kind.padEnd(19)} ${n.title}${n.detail ? ` - ${n.detail}` : ''}`);
  L.push(`sessions (${b.sessions.length})`);
  for (const s of b.sessions) L.push(`  ${s.status === 'busy' ? '◐' : s.status === 'idle' ? '○' : '?'} ${s.name || s.pid} [${s.project || '-'}]${s.lease_runs.length ? ` lease ${s.lease_runs.map((l) => l.run).join(',')}` : ''}${s.current ? ` row ${s.current.row} ${s.current.model || ''}/${s.current.effort || ''}` : ''}${s.unread ? ` unread ${s.unread}` : ''}`);
  L.push(`runs (${b.runs.length})`);
  for (const r of b.runs) L.push(`  ${r.id} ${r.state}${r.paused ? ' (paused)' : ''}${r.stop ? ` (${r.stop.state})` : ''}  ${r.strip}`);
  for (const c of b.councils) L.push(`  council ${c.id} ${c.state}  seats ${c.seats.map((s) => `${s.glyph}${s.label}`).join(' ')}${c.chair ? ` chair ${c.chair.glyph}` : ''}`);
  L.push(`today: tokens ${b.today.tokens ?? 'unknown'} · attempts ${b.today.attempts ?? 'unknown'} · launches ${b.today.launches ?? 'unknown'}`);
  return L.join('\n');
}

// ---------- CLI ----------
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const t0 = process.hrtime.bigint();
  const board = buildBoard(loadSources(), Date.now());
  if (args.includes('--json')) process.stdout.write(JSON.stringify(board, null, args.includes('--pretty') ? 2 : 0) + '\n');
  else process.stdout.write(renderText(board) + '\n');
  if (args.includes('--time')) process.stderr.write(`board: ${Number(process.hrtime.bigint() - t0) / 1e6} ms\n`);
}
