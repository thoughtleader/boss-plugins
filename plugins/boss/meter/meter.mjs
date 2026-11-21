// Shared, pure progress meter for chat, the Claude mod, and the Boss webview.
// No host APIs: all timestamps and usage arrive from the reporting contract.
const FINISHED = new Set(['done', 'completed', 'failed', 'cancelled', 'skipped', 'not_run', 'interrupted']);
const DONE = new Set(['done', 'completed']);
const QUIET = new Set(['queued', 'pending', 'skipped', 'not_run', 'held']);
const HALTED = new Set(['done', 'completed', 'failed', 'cancelled', 'ended', 'dead', 'interrupted', 'paused', 'stopped']);
const list = value => Array.isArray(value) ? value : [];
const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const stamp = value => { const n = typeof value === 'number' ? value : typeof value === 'string' ? Date.parse(value) : NaN; return Number.isFinite(n) ? n : null; };
const clean = (value, cap = 160) => typeof value === 'string' ? value.replace(/\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|$)/g, '').replace(/[\x00-\x1f\x7f-\x9f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, cap) : '';
const min = values => { const ns = values.filter(n => n != null); return ns.length ? Math.min(...ns) : null; };
const max = values => { const ns = values.filter(n => n != null); return ns.length ? Math.max(...ns) : null; };
const unique = (values, key = value => value.id) => [...new Map(values.map((value, index) => [key(value) ?? `anonymous:${index}`, value])).values()];
const rowKey = row => {
  const id = String(row.id || '').replace(/^(row|gate):/, '');
  const match = /^(\d+)([a-z]?)/i.exec(id);
  return match ? [Number(match[1]), match[2].toLowerCase()] : [Infinity, id];
};
const compareRows = (a, b) => {
  const x = rowKey(a), y = rowKey(b);
  return x[0] - y[0] || x[1].localeCompare(y[1]);
};

/** One visible segment per actual task/wave; blank cells reserve alignment. */
function bar(done, total) {
  const count = Math.max(0, Math.floor(total));
  const filled = Math.min(count, Math.max(0, Math.floor(done)));
  return '▰'.repeat(filled) + '▱'.repeat(count - filled);
}

export function displayBar(scope, cells = 12) {
  const total = Math.floor(number(scope?.total) ?? 0);
  const actual = bar(number(scope?.done) ?? 0, total);
  return actual.padEnd(Math.max(total, Number.isSafeInteger(cells) && cells > 0 ? cells : 12), ' ');
}

/** Keep upcoming work in view without changing tick size or underlying totals. */
export function windowedBar(scope, cells = 12) {
  const capacity = Math.max(1, Math.floor(number(cells) ?? 12));
  const total = Math.floor(number(scope?.total) ?? 0);
  const done = Math.min(total, Math.floor(number(scope?.done) ?? 0));
  const pending = total - done;
  if (pending > capacity) {
    const shown = Math.max(0, capacity - 1);
    return { text: '▱'.repeat(shown) + '…', hiddenDone: done, hiddenUpcoming: pending - shown };
  }
  const shownDone = Math.min(done, capacity - pending);
  return { text: '▰'.repeat(shownDone) + '▱'.repeat(pending), hiddenDone: done - shownDone, hiddenUpcoming: 0 };
}

export function progressLabel(scope) {
  return scope.total > 12 ? `${Math.floor(scope.done / scope.total * 100)}%` : `${scope.done}/${scope.total}`;
}

/** Bounded strings keep the elapsed column stable as clocks cross a unit boundary. */
export function formatElapsed(value, { compact = false } = {}) {
  if (number(value) == null) return '—';
  const seconds = Math.floor(value / 1000), minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60), days = Math.floor(hours / 24);
  const pad = n => String(n).padStart(2, '0');
  if (days >= 1000) return '999d+';
  if (days) return compact ? `${days}d${pad(hours % 24)}h` : `${days}d ${pad(hours % 24)}h`;
  if (hours) return compact ? `${hours}h${pad(minutes % 60)}m` : `${hours}h ${pad(minutes % 60)}m`;
  return compact ? `${minutes}m${pad(seconds % 60)}s` : `${minutes}m ${pad(seconds % 60)}s`;
}

/** Matched elapsed clocks; hours deliberately expand beyond two digits. */
export function formatClock(value, { hours = false } = {}) {
  if (number(value) == null) return '—';
  const seconds = Math.floor(value / 1000);
  if (hours || seconds >= 3600) return `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

/** Both displayed rows switch to hours together. Hidden totals do not change a Tasks-only clock. */
export function clockHours(meter) {
  return (number(meter?.current?.elapsedMs) ?? 0) >= 3600000
    || (meter?.waves != null && (number(meter?.total?.elapsedMs) ?? 0) >= 3600000);
}

/** Dim only the leading zero prefix; interior zeros retain their normal weight. */
export function clockParts(clock) {
  const value = typeof clock === 'string' ? clock : '';
  if (!/^\d{2,}:\d{2}(?::\d{2})?$/.test(value)) return { dim: '', active: value };
  const significant = value.search(/[1-9]/);
  const split = significant < 0 ? value.length - 1 : significant;
  return { dim: value.slice(0, split), active: value.slice(split) };
}

export function formatTokens(value) {
  if (number(value) == null) return '—';
  if (value >= 1e15) return '999T+';
  if (value < 1000) return String(Math.floor(value));
  for (const [limit, unit] of [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) {
    if (value >= limit) {
      const n = value / limit;
      // Truncate: a reported subtotal must not round up into unreported usage.
      return `${Math.floor(n * (n < 100 ? 10 : 1)) / (n < 100 ? 10 : 1)}${unit}`;
    }
  }
  return '—';
}

// Compact rows are authored summaries, never clipped instruction text.
// When a readable label is unavailable, the full task still lives in Details.
export function summaryTaskText(task, { completed = false } = {}) {
  let label = clean(task?.label, 1000);
  const plain = value => value.length <= 64 && value.split(/\s+/).length <= 11
    && !/[`<>{}]|\b\w+_\w+\b|(?:^|\s)\/\S|(?:--|->|=>)|[…]/u.test(value);
  if (!label || !plain(label)) return null;
  if (completed) return label;
  if (['blocked', 'waiting_input', 'failed', 'retry', 'paused'].includes(task.state)) label += ` (${task.state.replace(/_/g, ' ')})`;
  const extra = clean(task.waitingReason ? `waiting for ${task.waitingReason}` : task.activity, 1000);
  return extra && extra !== label && plain(extra) && label.length + extra.length + 3 <= 96 ? `${label} — ${extra}` : label;
}

function outputMetadata(item) {
  const artifacts = list(item.artifacts);
  return {
    model: clean(item.model, 128) || null,
    effort: clean(item.effort, 32) || null,
    result: clean(item.result || artifacts.find(a => a?.label === 'result' && a.path)?.path, 1024) || null,
    sha: clean(item.sha || artifacts.find(a => a?.sha)?.sha, 40) || null,
  };
}

function normalizedAttempt(attempt, ledger) {
  const id = attempt.meta?.attempt_id || attempt.id || attempt.label;
  const recorded = ledger.get(id) ?? ledger.get(attempt.label);
  return {
    ...outputMetadata(attempt),
    id,
    state: clean(attempt.state, 32) || 'queued',
    started: stamp(attempt.started_at),
    ended: stamp(attempt.ended_at),
    updated: stamp(attempt.updated_at),
    tokens: number(recorded?.tokens) ?? number(attempt.tokens) ?? number(attempt.meta?.tokens),
  };
}

function normalizedRow(row, attempts, board = false) {
  const as = unique(attempts);
  const state = clean(row.state, 32) || 'queued';
  const known = as.filter(a => a.tokens != null);
  const truncated = number(row.attempt_count) != null && row.attempt_count > as.length;
  const directTokens = number(row.tokens) ?? number(row.meta?.tokens);
  // Board rows carry all-attempt totals even though their detail list retains only 6.
  const tokens = board && truncated && directTokens != null ? directTokens
    : as.length ? (known.length ? known.reduce((sum, a) => sum + a.tokens, 0) : null) : directTokens;
  const active = ['running', 'retry'].includes(state) || as.some(a => ['running', 'retry'].includes(a.state));
  const used = as.length > 0 || !QUIET.has(state) || stamp(row.started_at) != null;
  const latestStart = max([stamp(row.started_at), ...as.map(a => a.started)]);
  const reportedEnd = max([stamp(row.ended_at), ...as.map(a => a.ended)]);
  // A previous attempt's end cannot close work that demonstrably started later.
  const ended = reportedEnd != null && (latestStart == null || reportedEnd >= latestStart) ? reportedEnd : null;
  const metadata = outputMetadata(row);
  const newest = [...as].reverse();
  const current = newest.find(a => ['running', 'retry'].includes(a.state)) ?? newest[0];
  return {
    id: row.id, parent: row.parent, state,
    label: clean(row.short_label || row.meta?.short_label || row.label || row.name || row.id, 1000),
    activity: clean(row.activity || row.meta?.activity, 160) || null,
    instructions: clean(row.instructions || row.meta?.instructions || row.label || row.name, 12000) || null,
    estimateMs: number(row.estimate_ms) ?? number(row.meta?.estimate_ms),
    reviewable: typeof row.reviewable === 'boolean' ? row.reviewable : typeof row.meta?.reviewable === 'boolean' ? row.meta.reviewable : row.kind === 'gate' || row.meta?.gate === true ? true : null,
    dependsOn: list(row.depends_on || row.meta?.depends_on).map(String),
    started: min([stamp(row.started_at), ...as.map(a => a.started)]),
    ended,
    updated: stamp(row.updated_at),
    tokens,
    partialTokens: active || as.some(a => a.tokens == null && (!QUIET.has(a.state) || a.started != null)) || (used && tokens == null) || truncated,
    running: active,
    attempts: as,
    model: current?.model || metadata.model,
    effort: current?.effort || metadata.effort,
    result: metadata.result || newest.find(a => a.result)?.result || null,
    sha: metadata.sha || newest.find(a => a.sha)?.sha || null,
    detail: clean(row.error || row.detail || row.plan_status || row.meta?.plan_status, 2000),
  };
}

// Merge overlapping recorded waits before subtracting from wall time.
function waitDuration(run, start, end) {
  if (start == null || end == null) return 0;
  const intervals = list(run.reporting?.waits).map(w => [Math.max(start, stamp(w.started_at) ?? end), Math.min(end, stamp(w.ended_at) ?? end)])
    .filter(([a, b]) => b > a).sort((a, b) => a[0] - b[0]);
  let total = 0, through = start;
  for (const [a, b] of intervals) { total += Math.max(0, b - Math.max(a, through)); through = Math.max(through, b); }
  return total;
}

function stopped(run) {
  return run.paused === true || /^paused/i.test(String(run.phase || '')) || HALTED.has(run.state) || HALTED.has(run.status) || run.stop?.state === 'stopped';
}

function totals(label, rows, run, nowMs, all = false) {
  const total = rows.length, done = rows.filter(r => DONE.has(r.state)).length;
  const allFinished = total > 0 && rows.every(r => FINISHED.has(r.state));
  const freeze = stopped(run) || run.reporting?.waiting === true || (run.state === 'waiting_input' && !rows.some(r => r.running));
  const running = freeze ? 0 : rows.filter(r => r.running).length;
  const started = min(rows.map(r => r.started)) ?? (all && rows.some(r => !QUIET.has(r.state)) ? stamp(run.started_at) : null);
  const lastEnd = max(rows.map(r => r.ended));
  const updated = stamp(run.updated_at) ?? max(rows.map(r => r.updated));
  const missingEnd = rows.some(r => r.started != null && r.ended == null);
  const runEnd = stamp(run.ended_at);
  const validRunEnd = runEnd != null && (max(rows.map(r => r.started)) == null || runEnd >= max(rows.map(r => r.started))) ? runEnd : null;
  // A finished row without an end makes an earlier sibling's end unsafe. A run-end
  // timestamp can close the scope; an unrelated metadata update cannot.
  const end = allFinished ? (missingEnd ? validRunEnd : lastEnd ?? validRunEnd)
    : freeze ? stamp(run.reporting?.waiting_since) ?? stamp(run.ended_at) ?? stamp(run.stop?.since) ?? updated ?? lastEnd : nowMs;
  const ticking = started != null && !freeze && !allFinished;
  const waitingMs = waitDuration(run, started, allFinished ? end : nowMs);
  const elapsedMs = started == null || end == null ? null : Math.max(0, end - started - waitDuration(run, started, end));
  const known = rows.filter(r => r.tokens != null);
  const tokens = known.length ? known.reduce((sum, r) => sum + r.tokens, 0) : null;
  const state = run.paused || /^paused/i.test(String(run.phase || '')) ? 'paused'
    : freeze ? clean(run.stop?.state === 'stopped' ? 'stopped' : HALTED.has(run.state) ? run.state : run.status || run.state, 32)
      : running ? 'running' : rows.some(r => r.state === 'waiting_input' || r.state === 'blocked') ? 'waiting_input'
        : allFinished ? (done === total ? 'done' : 'ended') : 'queued';
  return { label, done, total, elapsedMs, waitingMs, tokens, partialTokens: rows.some(r => r.partialTokens), running, state, bar: bar(done, total), ticking };
}

function taskReport(run, waves, rows, nowMs) {
  const waveByRow = new Map(waves.flatMap(w => {
    const raw = clean(w.label || w.id, 80).replace(/^wave[:\s]*/i, '');
    const label = w.id == null ? null : `Wave ${/^[a-z]$/i.test(raw) ? raw.toUpperCase() : raw}`;
    return w.rows.map(r => [r.id, label]);
  }));
  const ordered = unique([...waves.flatMap(w => w.rows), ...rows]);
  const paused = run.paused === true || /^paused/i.test(String(run.phase || '')) || run.state === 'paused' || run.status === 'paused';
  const halt = stopped(run);
  const terminal = !paused && (run.stop?.state === 'stopped' || [run.state, run.status].some(state => HALTED.has(state) && !['dead', 'paused'].includes(state)));
  const explicit = new Map(list(run.attention).filter(a => a && typeof a === 'object' && a.item && clean(a.text)).map(a => [a.item, clean(a.text, 2000)]));
  const all = ordered.map(row => {
    const state = halt && row.running && !FINISHED.has(row.state) ? (paused ? 'paused' : 'interrupted')
      : terminal && ['queued', 'pending', 'held'].includes(row.state) ? 'not_run' : row.state;
    const needsAction = ['blocked', 'waiting_input', 'failed'].includes(state);
    return {
      id: clean(String(row.id ?? ''), 128), label: row.label, activity: row.activity, instructions: row.instructions, state, wave: waveByRow.get(row.id) ?? null,
      elapsedMs: totals(row.label, [row], run, nowMs).elapsedMs,
      tokens: row.tokens, partialTokens: row.partialTokens,
      model: row.model, effort: row.effort, result: row.result, sha: row.sha,
      detail: row.detail || null,
      waitingReason: row.dependsOn.filter(id => !DONE.has(ordered.find(other => String(other.id) === id)?.state)).map(id => ordered.find(other => String(other.id) === id)?.label || id).join(', ') || null,
      action: explicit.get(row.id) || (needsAction ? row.detail || (state === 'waiting_input' ? 'Waiting for input' : state) : null),
    };
  });
  const now = all.filter(row => ['running', 'retry', 'paused', 'blocked', 'waiting_input', 'failed'].includes(row.state));
  // Queue order is not a dependency/readiness assertion.
  const next = all.filter(row => ['queued', 'pending', 'held'].includes(row.state));
  const endedById = new Map(ordered.map(row => [clean(String(row.id ?? ''), 128), row.ended]));
  const recent = all.filter(row => DONE.has(row.state)).sort((a, b) =>
    (endedById.get(b.id) ?? -Infinity) - (endedById.get(a.id) ?? -Infinity) || all.indexOf(b) - all.indexOf(a));
  const seen = new Set(list(run.reporting?.previous_completed_ids));
  const newlyCompleted = recent.filter(task => !seen.has(task.id));
  return { now, next, recent: newlyCompleted, all, nowCount: now.length, nextCount: next.length, recentCount: newlyCompleted.length };
}

function assemble(run, waves, rows, attention, nowMs) {
  if (rows.some(r => r.running)) run = { ...run, paused: false,
    phase: /^paused/i.test(String(run.phase || '')) ? 'active' : run.phase,
    state: run.state === 'paused' ? 'running' : run.state,
    status: run.status === 'paused' ? 'running' : run.status,
    reporting: { ...run.reporting, waiting: false } };
  // A live later wave should be shown even when a prior gate remains unresolved.
  const active = waves.find(w => w.rows.some(r => r.running))
    ?? waves.find(w => w.rows.some(r => !FINISHED.has(r.state)))
    ?? waves[waves.length - 1];
  const label = active && active.id != null ? (() => {
    const raw = clean(active.label || active.id).replace(/^wave[:\s]*/i, '') || '?';
    const name = /^[a-z]$/i.test(raw) ? raw.toUpperCase() : raw;
    return `Wave ${name}${/^\d+$/.test(raw) && waves.length > 1 ? `/${waves.length}` : ''}`;
  })() : 'Run';
  const current = totals(label, active?.rows ?? rows, run, nowMs);
  const total = totals('Total', rows, run, nowMs, true);
  const realWaves = waves.filter(w => w.id != null);
  const wavesDone = realWaves.filter(w => w.rows.length > 0 && w.rows.every(r => DONE.has(r.state))).length;
  const waveScope = realWaves.length > 1 ? {
    ...total, hideElapsed: !realWaves.slice(1).some(w => w.rows.some(r => r.started != null || r.running || FINISHED.has(r.state))), label: 'Waves', done: wavesDone, total: realWaves.length,
    running: stopped(run) ? 0 : realWaves.filter(w => w.rows.some(r => r.running)).length,
    bar: bar(wavesDone, realWaves.length),
  } : null;
  const partial = current.partialTokens || total.partialTokens;
  const reported = current.tokens != null || total.tokens != null;
  const tokenNote = partial
    ? 'Reported tokens are incomplete while active or unreported attempts are missing. Continuations may repeat prior context.'
    : reported ? 'Reported tokens, not billed usage. Continuations may repeat prior context.' : null;
  return { forecast: forecastFor(run, waves, rows, nowMs, active), waves: waveScope, current, total, tasks: taskReport(run, waves, rows, nowMs), attention: unique(attention.map(text => ({ id: text, text }))).map(a => a.text), tokenNote };
}

function forecastFor(run, waves, rows, nowMs, active) {
  const waiting = run.reporting?.waiting === true || (run.state === 'waiting_input' && !rows.some(r => r.running));
  const completedDurations = rows.filter(r => DONE.has(r.state) && r.started != null && r.ended != null && r.ended > r.started)
    .map(r => Math.max(1000, r.ended - r.started - waitDuration(run, r.started, r.ended))).sort((a, b) => a - b);
  const observed = completedDurations.length ? completedDurations[Math.floor(completedDurations.length / 2)] : null;
  const fallback = observed ?? 300000;
  const unfinished = rows.filter(r => !FINISHED.has(r.state));
  const estimates = new Map(rows.map(row => {
    const estimate = row.estimateMs ?? fallback;
    const elapsed = totals(row.label, [row], run, nowMs).elapsedMs ?? 0;
    const remaining = FINISHED.has(row.state) ? 0 : row.started != null ? Math.max(Math.min(60000, estimate), estimate - elapsed) : estimate;
    return [row.id, remaining];
  }));
  const ends = new Map();
  const waveStarts = new Map();
  let finishMs = 0;
  // Waves are sequential; independent tasks within a wave run in parallel.
  // Explicit dependencies within a wave extend the critical path.
  for (const wave of waves.length ? waves : [{ rows }]) {
    const base = finishMs, members = new Map(wave.rows.map(r => [String(r.id), r]));
    waveStarts.set(wave, base);
    const visit = (row, trail = new Set()) => {
      if (ends.has(row.id)) return ends.get(row.id);
      if (trail.has(row.id)) return base + estimates.get(row.id); // malformed cycles remain approximate
      const nextTrail = new Set([...trail, row.id]);
      const dependencyEnd = Math.max(base, ...row.dependsOn.map(id => members.has(id) ? visit(members.get(id), nextTrail) : ends.get(id) ?? base));
      const end = FINISHED.has(row.state) ? 0 : dependencyEnd + estimates.get(row.id);
      ends.set(row.id, end); return end;
    };
    finishMs = Math.max(base, ...wave.rows.map(row => visit(row)));
  }
  const reviewable = unfinished.filter(r => r.reviewable === true || (r.reviewable == null && /\b(review|verify|verification|demo|preview|deliver|result)\b/i.test(r.label)));
  const checkpointRow = [...reviewable].sort((a, b) => (ends.get(a.id) ?? Infinity) - (ends.get(b.id) ?? Infinity))[0];
  const firstOpenWave = waves.find(w => w.rows.some(r => !FINISHED.has(r.state)));
  const checkpointMs = checkpointRow ? ends.get(checkpointRow.id) : firstOpenWave ? Math.max(...firstOpenWave.rows.map(r => ends.get(r.id) ?? 0)) : finishMs;
  const currentRows = active?.rows ?? rows;
  const currentWaveMs = Math.max(0, ...currentRows.map(row => (ends.get(row.id) ?? 0) - (waveStarts.get(active) ?? 0)));
  const originalFinishAt = stamp(run.reporting?.forecast?.original_finish_at) ?? nowMs + finishMs;
  const originalCheckpointAt = stamp(run.reporting?.forecast?.original_checkpoint_at) ?? nowMs + checkpointMs;
  const timezone = run.reporting?.timezone || 'America/Los_Angeles';
  const timeText = value => {
    try { return new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', hour12: true, timeZone: timezone }).format(value).replace(/\s/g, ''); }
    catch { return new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', hour12: true, timeZone: 'UTC' }).format(value).replace(/\s/g, '') + ' UTC'; }
  };
  const finishedAt = !unfinished.length ? max(rows.map(r => r.ended)) ?? stamp(run.ended_at) : null;
  const item = (remainingMs, label, scopeRows = rows) => {
    const complete = scopeRows.every(r => FINISHED.has(r.state));
    const endedAt = complete ? max(scopeRows.map(r => r.ended)) ?? stamp(run.ended_at) : null;
    return { remainingMs, at: waiting ? null : endedAt ?? nowMs + remainingMs, label, approximate: true,
      text: complete ? scopeRows.every(r => DONE.has(r.state)) ? 'Complete' : 'Ended with unfinished results' : ['failed', 'cancelled', 'stopped', 'dead'].includes(run.state || run.status) ? 'Stopped' : waiting ? `~${Math.max(1, Math.ceil(remainingMs / 60000))} min after you respond` : `~${timeText(nowMs + remainingMs)}` };
  };
  const basis = `${observed == null ? 'Rough initial estimate: five minutes per task without an explicit estimate.' : 'Rough estimate using median completed-task duration where no explicit estimate is recorded.'} Independent tasks in a wave are assumed parallel; waves are sequential.${checkpointRow ? '' : ' Checkpoint is the next result (estimated); no explicit reviewable task is recorded.'}`;
  const slip = Math.max(0, (finishedAt ?? nowMs + finishMs) - originalFinishAt);
  const delayMs = slip >= 60000 ? slip : 0;
  const reason = clean(run.reporting?.forecast?.reason, 300) || (waiting ? 'Waiting for your input' : rows.some(r => r.attempts.length > 1) ? 'Estimate includes retried work' : delayMs ? 'Remaining work is taking longer than the original forecast' : null);
  return { currentWave: item(currentWaveMs, active?.label || 'Current tasks', currentRows), checkpoint: item(checkpointMs, checkpointRow?.label || 'Next result (estimated)'), finish: item(finishMs, 'Whole job'), originalFinishAt, originalCheckpointAt, waiting, basis, delayMs, reason };
}

function attentionFor(run, rows) {
  const explicit = list(run.attention);
  const attention = explicit.map(a => clean(typeof a === 'string' ? a : a?.text)).filter(Boolean);
  const covered = new Set(explicit.filter(a => a && typeof a === 'object' && clean(a.text)).map(a => a.item));
  for (const row of rows) if (['blocked', 'waiting_input', 'failed'].includes(row.state) && !covered.has(row.id)) {
    const reason = row.detail || (row.state === 'waiting_input' ? 'waiting for input' : row.state);
    attention.push(`${row.label || 'Task'}: ${reason}`);
  }
  if (run.state === 'failed' && !attention.length) attention.push(clean(run.summary) || 'Run failed');
  return attention;
}

/** Fold one canonical boss.progress/1 snapshot. Ledger may fill usage omitted by a snapshot. */
export function meterFromSnapshot(snapshot, { nowMs = Date.now(), ledger = [] } = {}) {
  const run = snapshot && typeof snapshot === 'object' ? snapshot : {};
  const items = unique(list(run.items).filter(i => i && typeof i === 'object'));
  // Outcome updates often omit usage. Keep the latest non-null reported count.
  const ledgerMap = new Map(), legacy = new Map();
  for (const [index, item] of list(ledger).entries()) if (item && typeof item === 'object') {
    const map = item.id != null ? ledgerMap : legacy;
    const key = item.id ?? (item.ts != null ? `legacy:${item.row}:${item.ts}` : `legacy:${index}`);
    const previous = map.get(key);
    map.set(key, { ...previous, ...item, tokens: number(item.tokens) ?? previous?.tokens ?? null });
  }
  const attempts = items.filter(i => i.kind === 'attempt' || i.kind === 'call');
  const rowItems = items.filter(i => ['row', 'gate', 'seat', 'chair'].includes(i.kind));
  const recorded = [...ledgerMap.values(), ...legacy.values()];
  const rows = rowItems.map(r => {
    let children = attempts.filter(a => a.parent === r.id).map(a => normalizedAttempt(a, ledgerMap));
    if (!children.length) {
      const rowId = String(r.id).replace(/^(row|gate):/, '').toLowerCase();
      children = recorded.filter(item => String(item.row ?? /#(\d+[a-z]?)\./i.exec(String(item.id))?.[1] ?? '').toLowerCase() === rowId)
        .map((item, index) => ({ id: item.id ?? `legacy:${rowId}:${item.ts ?? index}`, state: 'reported', started: null, ended: null, updated: null, tokens: number(item.tokens) }));
    }
    return normalizedRow(r, children);
  });
  const waves = items.filter(i => i.kind === 'wave').map(w => ({ id: w.id, label: w.label, rows: rows.filter(r => r.parent === w.id) }));
  const grouped = new Set(waves.flatMap(w => w.rows.map(r => r.id)));
  const loose = rows.filter(r => !grouped.has(r.id));
  if (loose.length) {
    const withRows = waves.filter(w => w.rows.length);
    if (!withRows.length) waves.push({ id: null, label: 'Run', rows: loose });
    else for (const row of loose) {
      // Same rule as board.mjs: an inserted, unassigned plan row follows its nearest earlier row.
      const before = withRows.flatMap(w => w.rows.map(r => ({ row: r, wave: w })))
        .filter(entry => compareRows(entry.row, row) < 0)
        .sort((a, b) => compareRows(b.row, a.row))[0];
      const group = before?.wave ?? withRows[0];
      group.rows.push(row);
      group.rows.sort(compareRows);
    }
  }
  return assemble(run, waves.filter(w => w.rows.length), rows, attentionFor(run, rows), nowMs);
}

/** Fold the existing Boss board shape without losing totals hidden by its attempt-detail cap. */
export function meterFromBoardRun(run, { nowMs = Date.now() } = {}) {
  run = run && typeof run === 'object' ? run : {};
  const emptyLedger = new Map();
  const make = row => normalizedRow(row, list(row.attempts).map(a => normalizedAttempt(a, emptyLedger)), true);
  const waves = list(run.waves).map(w => ({ id: w.id, label: w.label, rows: unique(list(w.rows)).map(make) }));
  let rows = unique(waves.flatMap(w => w.rows));
  if (!rows.length) {
    rows = unique(list(run.rows)).map(make);
    if (rows.length) waves.push({ id: null, label: 'Run', rows });
  }
  return assemble(run, waves.filter(w => w.rows.length), rows, attentionFor(run, rows), nowMs);
}

const pad = (value, width, right = false) => right ? value.padStart(width) : value.padEnd(width);

/** Two aligned bars: all waves first, then tasks in the current wave. No visual counts. */
export function renderMeterText(meter, { columns = 72, fenced = true } = {}) {
  const width = Number.isFinite(columns) ? Math.max(24, Math.floor(columns)) : 72;
  const scopes = meter.waves ? [
    { label: '🆆', progress: meter.waves, values: meter.total },
    { label: '🆃', progress: meter.current, values: meter.current },
  ] : [{ label: '🆃', progress: meter.current, values: meter.current }];
  const compact = width < 40;
  const separator = compact ? ' ' : '  ', gap = compact ? ' ' : '  ';
  const hours = clockHours(meter);
  const times = scopes.map(row => formatClock(row.values.elapsedMs, { hours }));
  const tokens = scopes.map(row => formatTokens(row.values.tokens));
  const timeWidth = Math.max(hours ? 8 : 5, ...times.map(s => s.length));
  const tokenWidth = Math.max(6, ...tokens.map(s => s.length));
  // Preserve prominent bars, numeric values, and alignment. Extremely small
  // panels may overflow; never reduce the track to dots or truncate the clock.
  const barWidth = Math.max(8, Math.min(24, width - (3 + gap.length + 2 * separator.length + timeWidth + tokenWidth + (meter.forecast ? 12 : 0))));
  const forecastText = point => (point?.text || '—').replace(/^ETA\s+/, '');
  const lines = scopes.map((row, index) => {
    const progress = windowedBar(row.progress, barWidth).text;
    const forecast = row.label === '🆆' ? meter.forecast?.finish : meter.forecast?.currentWave ?? (!meter.waves ? meter.forecast?.finish : null);
    return `${row.label}${gap}${pad(progress, barWidth)}${separator}${pad(row.progress.hideElapsed ? '' : times[index], timeWidth, true)}${separator}${pad(tokens[index], tokenWidth, true)}${forecast ? '  ' + forecastText(forecast) : ''}`;
  });
  if (meter.tasks) {
    const completed = meter.tasks.recent.filter(task => DONE.has(task.state));
    completed.forEach(task => { const text = summaryTaskText(task, { completed: true }); if (text) lines.push(`☑    ${text}`); });
    if (meter.tasks.now.length) meter.tasks.now.forEach(task => { const text = summaryTaskText(task); if (text) lines.push(`➜    ${text}`); });
    else lines.push(`➜    ${DONE.has(meter.total.state) ? 'Complete' : 'No active tasks'}`);
    if (meter.tasks.next.length) meter.tasks.next.forEach(task => { const text = summaryTaskText(task); if (text) lines.push(`⮡    ${text}`); });
    else lines.push('⮡    None queued');
  }
  const text = lines.map(line => line.trimEnd()).join('\n');
  return fenced ? `\`\`\`text\n${text}\n\`\`\`` : text;
}
