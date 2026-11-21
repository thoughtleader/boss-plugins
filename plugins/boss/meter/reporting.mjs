// Presentation history only. No run control or filesystem access in this module.
const terminal = state => ['done', 'completed', 'failed', 'cancelled'].includes(state);
const stamp = value => { const n = typeof value === 'number' ? value : Date.parse(value); return Number.isFinite(n) ? n : null; };
const iso = value => new Date(value).toISOString();

export function reportingSnapshot(snapshot, previous = null, { events = [], nowMs = Date.now() } = {}) {
  const snap = { ...snapshot, items: (snapshot.items || []).map(item => ({ ...item })) };
  const identity = `${snap.tool}/${snap.run_id}`;
  if (previous?.key !== identity || previous?.startedAt !== (snap.started_at || snap.created_at || null)) previous = null;
  const active = snap.items.some(item => ['row', 'gate', 'seat', 'chair'].includes(item.kind) && ['running', 'retry'].includes(item.state));
  const waiting = !active && !terminal(snap.state) && (snap.paused || ['waiting_input', 'paused'].includes(snap.state));
  const waits = (snap.reporting?.waits || previous?.waits || []).map(wait => ({ ...wait }));
  // Manifest lease history identifies real idle intervals, including between reports.
  if (events.length) {
    let released = null;
    const running = new Set();
    for (const event of [...events].sort((a, b) => (a.seq || 0) - (b.seq || 0))) {
      if (event.type === 'start') running.add(event.attempt || event.agent_id);
      if (event.type === 'stop' && (event.final || event.phase === 'waiting')) running.delete(event.attempt || event.agent_id);
      if (event.type === 'lease' && event.action === 'release' && !running.size) released = event.ts;
      if ((event.type === 'lease' && ['acquire', 'renew', 'steal', 'migrate'].includes(event.action)) || event.type === 'start') {
        if (released && stamp(event.ts) > stamp(released)) {
          const existing = waits.find(w => w.started_at === released);
          if (existing) existing.ended_at = event.ts;
          else waits.push({ started_at: released, ended_at: event.ts });
        }
        released = null;
      }
    }
    if (released && waiting && !waits.some(w => w.started_at === released)) waits.push({ started_at: released });
  }
  const open = waits.find(wait => !wait.ended_at);
  if (waiting && !open) waits.push({ started_at: snap.waiting_since || snap.updated_at || iso(nowMs) });
  if (!waiting && open) open.ended_at = snap.updated_at && stamp(snap.updated_at) >= stamp(open.started_at) ? snap.updated_at : iso(nowMs);
  const oldForecast = previous?.forecast;
  snap.reporting = {
    ...snap.reporting, waits, waiting,
    waiting_since: waiting ? waits.find(wait => !wait.ended_at)?.started_at : null,
    // Without a prior report, existing completions are history, not new work.
    previous_completed_ids: previous?.completedIds ?? snap.items.filter(item => ['row', 'gate', 'seat', 'chair'].includes(item.kind) && ['done', 'completed'].includes(item.state)).map(item => item.id),
    forecast: {
      ...snap.reporting?.forecast,
      original_finish_at: oldForecast?.originalFinishAt || oldForecast?.finish?.at || snap.reporting?.forecast?.original_finish_at,
      original_checkpoint_at: oldForecast?.originalCheckpointAt || oldForecast?.checkpoint?.at || snap.reporting?.forecast?.original_checkpoint_at,
    },
  };
  return snap;
}

export function reportingState(snapshot, meter, previous = null, nowMs = Date.now()) {
  if (previous?.key !== `${snapshot.tool}/${snapshot.run_id}` || previous?.startedAt !== (snapshot.started_at || snapshot.created_at || null)) previous = null;
  const forecast = meter.forecast;
  const history = [...(previous?.delayHistory || [])];
  const activeKey = meter.tasks.now.map(t => `${t.id}:${t.activity || t.state}`).join('|');
  // Record a changed forecast at task transitions, rather than a new notice every clock tick.
  const delayAdded = !!(forecast?.delayMs > 0 && previous && activeKey !== previous.activeKey);
  if (delayAdded) {
    history.push({ at: iso(nowMs), delayMs: forecast.delayMs, reason: forecast.reason || 'Work is taking longer than the original estimate', finishAt: forecast.finish.at });
  }
  return {
    schema: 'boss.reporting/1', key: `${snapshot.tool}/${snapshot.run_id}`,
    startedAt: snapshot.started_at || snapshot.created_at || null,
    updatedAt: iso(nowMs), completedIds: meter.tasks.all.filter(t => ['done', 'completed'].includes(t.state)).map(t => t.id),
    activeKey, waits: snapshot.reporting?.waits || [], forecast, delayHistory: history.slice(-50), delayAdded,
  };
}
