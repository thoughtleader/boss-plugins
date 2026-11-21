import { reportingSnapshot, reportingState } from '../../reporting.mjs';
import { meterFromSnapshot, clockHours, windowedBar, formatClock, formatTokens } from '../../meter.mjs';
import { readRegistry, chooseRun, ownedRun } from './registry.mjs';
import { renderPane, renderInline } from './layout.mjs';
import { parseInlineMessage, readInlineReport, cleanLegacyMarkers } from './inline.mjs';
export { encodeInlineMessage } from './inline.mjs';

const PANE = 'boss-meter';
const COMMAND = 'boss-meter';

let options = {};
let timer, busy = false, registered = false, root, sid = null, selectedKey = null, lastAutoKey = null, generation = 0;
let choosing = false, details = false, status = null;
let state = { loading: true, entries: [], errors: [], entry: null, meter: null, details: false, missing: false };
const inlineExpanded = new Map();
const taskExpansions = new Map();
function expansionOptions(key, invalidate) {
  return { expandedTasks: taskExpansions.get(key) || new Set(), toggleTask: id => {
    if (!taskExpansions.has(key)) {
      if (taskExpansions.size >= 200) taskExpansions.delete(taskExpansions.keys().next().value);
      taskExpansions.set(key, new Set());
    }
    const set = taskExpansions.get(key);
    if (set.has(id)) set.delete(id); else set.add(id);
    invalidate();
  } };
}
const reportCache = new Map();
const liveHistory = new Map();
function liveMeter(entry, nowMs) {
  if (!entry) return null;
  const memory = liveHistory.get(entry.key);
  const posted = entry.reporting;
  const previous = posted && (!memory || Date.parse(posted.updatedAt) > Date.parse(memory.updatedAt)) ? posted : memory || posted;
  const snapshot = reportingSnapshot(entry.snapshot, previous, { nowMs });
  // Polling is not publication: keep DONE relative to the latest posted report.
  snapshot.reporting.previous_completed_ids = posted?.completedIds ?? memory?.completedIds ?? snapshot.reporting.previous_completed_ids;
  const meter = meterFromSnapshot(snapshot, { nowMs });
  const state = reportingState(snapshot, meter, previous, nowMs);
  if (meter.forecast) meter.forecast.delayHistory = state.delayHistory;
  state.completedIds = snapshot.reporting.previous_completed_ids;
  if (liveHistory.size >= 120 && !liveHistory.has(entry.key)) liveHistory.delete(liveHistory.keys().next().value);
  liveHistory.set(entry.key, state);
  return meter;
}

async function progressRoot($) {
  if (!root) {
    const home = await $.env.get('HOME') || '';
    root = String(options.progress_dir || await $.env.get('BOSS_PROGRESS_DIR') || `${home}/.claude/boss`);
  }
  return root;
}

async function readReport($, path) {
  if (reportCache.has(path)) return reportCache.get(path);
  const raw = await $.fs.read(path);
  if (raw.length <= 512 * 1024) {
    if (reportCache.size >= 32) reportCache.delete(reportCache.keys().next().value);
    reportCache.set(path, raw);
  }
  return raw;
}

async function drawInline($, e, next) {
  const source = cleanLegacyMarkers(e.props?.text);
  const passthrough = source === e.props?.text ? e : { ...e, props: { ...e.props, text: source } };
  if (!parseInlineMessage(source).length) return next(passthrough);
  const payload = await readInlineReport({ read: p => readReport($, p) }, await progressRoot($), source);
  if (!payload) return next(passthrough);
  const key = `${e.component}:${e.requestId || payload.run}`;
  // The transcript viewport is shared with the above-prompt band in VS Code,
  // not measured at this message. Leave a small gutter before choosing a grid.
  const columns = Math.max(16, Math.min(240, Number(e.viewport?.columns) || payload.columns) - 4);
  const ui = $.ui.resolve(e);
  const meter = renderInline(ui, payload, inlineExpanded.get(key) === true, () => {
    if (inlineExpanded.size >= 200 && !inlineExpanded.has(key)) inlineExpanded.delete(inlineExpanded.keys().next().value);
    inlineExpanded.set(key, inlineExpanded.get(key) !== true);
    $.ui.invalidate('ui.render');
  }, columns, { surface: e.surface, ...expansionOptions(key, () => $.ui.invalidate('ui.render')) });
  return ui.Box({ key: 'boss-meter-message', width: '100%', minWidth: 0, flexGrow: 1, flexDirection: 'column', children: [meter] });
}

  async function tick($) {
    if (busy) return;
    busy = true;
    const before = generation;
    try {
      sid = await $.session.id();
      await progressRoot($);
      const nowMs = await $.clock.now();
      const result = await readRegistry({ list: p => $.fs.list(p), read: p => $.fs.read(p) }, root);
      if (before !== generation) return;
      const matched = chooseRun(result.entries, sid, selectedKey);
      const previous = !selectedKey && lastAutoKey ? result.entries.find(e => e.key === lastAutoKey && e.snapshot.owner?.session_id === sid) : null;
      const entry = choosing ? null : matched || previous || null;
      if (!selectedKey && entry) lastAutoKey = entry.key;
      state = { ...result, loading: false, entry, meter: liveMeter(entry, nowMs), details, missing: !!selectedKey && !entry && !choosing };

      // A global picker may inspect other sessions, but its selection never
      // becomes this session's status. Only exact owner identity earns that.
      const own = ownedRun(result.entries, sid);
      const m = own && own.key === entry?.key ? state.meter : liveMeter(own, nowMs);
      const hours = m ? clockHours(m) : false;
      const nextStatus = m ? `${m.waves ? `🆆 ${windowedBar(m.waves, 12).text.padEnd(12)}  ${m.waves.hideElapsed ? ' '.repeat(formatClock(m.total.elapsedMs, { hours }).length) : formatClock(m.total.elapsedMs, { hours })}  ${formatTokens(m.total.tokens)}  ` : ''}🆃 ${windowedBar(m.current, 12).text.padEnd(12)}  ${formatClock(m.current.elapsedMs, { hours })}  ${formatTokens(m.current.tokens)}` : undefined;
      if (nextStatus !== status) { status = nextStatus; $.ui.status(nextStatus); }
      $.ui.invalidate('ui.render');
    } catch {
      if (before !== generation) return;
      state = { ...state, loading: false, entry: null, meter: null, errors: ['registry'] };
      if (status !== undefined) { status = undefined; $.ui.status(undefined); }
      $.ui.invalidate('ui.render');
    } finally { busy = false; }
  }

  async function ensure($) {
    if (!registered) {
      await $.command.register({ name: COMMAND, description: 'Show compact wave progress, elapsed time, and reported tokens' });
      registered = true;
    }
    if (!timer) timer = $.clock.every(Math.max(1, Math.min(60, Number(options.poll_seconds) || 3)) * 1000, () => void tick($));
  }

export function register(on, config = {}) {
  options = config;
  timer = undefined; busy = false; registered = false; root = undefined; sid = null; selectedKey = null; lastAutoKey = null; generation = 0;
  choosing = false; details = false; status = null;
  inlineExpanded.clear();
  taskExpansions.clear();
  reportCache.clear();
  liveHistory.clear();
  state = { loading: true, entries: [], errors: [], entry: null, meter: null, details: false, missing: false };
  on('session.start', async ($, e, next) => { await ensure($); await tick($); return next(e); })
    .catch(async ($, e, next) => { if (next.called) throw new Error(next.error.message); return next(e); });
  on('prompt.submit', async ($, e, next) => { await ensure($); return next(e); })
    .catch(async ($, e, next) => { if (next.called) throw new Error(next.error.message); return next(e); });
  on('session.end', async ($, e, next) => {
    generation++; timer?.cancel(); timer = undefined; sid = null; selectedKey = null; lastAutoKey = null; choosing = false; details = false;
    inlineExpanded.clear();
  taskExpansions.clear();
    reportCache.clear();
  liveHistory.clear();
    state = { loading: true, entries: [], errors: [], entry: null, meter: null, details: false, missing: false };
    status = undefined; $.ui.status(undefined);
    return next(e);
  }).catch(async ($, e, next) => { if (next.called) throw new Error(next.error.message); return next(e); });
  on('command.run', { command: COMMAND }, async ($, e) => {
    await ensure($);
    const requested = String(e.args || '').trim();
    if (requested && /^[a-z][a-z0-9-]{0,31}\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(requested)) { selectedKey = requested; choosing = false; }
    await tick($);
    await $.ui.open({ id: PANE, title: 'Boss meter' });
    return {};
  }).catch(async () => ({ text: 'Boss meter could not open. Try /boss-meter again.' }));
  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== PANE) return next(e);
    const refresh = async () => { await tick($); };
    return renderPane($.ui.resolve(e), state, {
      select: async key => { selectedKey = key; choosing = false; details = false; await refresh(); },
      details: async () => { details = !details; state = { ...state, details }; $.ui.invalidate('ui.render'); },
      change: async () => { selectedKey = null; choosing = true; details = false; await refresh(); },
    }, Math.max(16, Number(e.props.bodyColumns) || 60), { surface: e.surface, ...expansionOptions(`pane:${state.entry?.key || ''}`, () => $.ui.invalidate('ui.render')) });
  });
  on('ui.render', { component: 'AssistantMessage' }, drawInline)
    .catch(async ($, e, next) => { if (next.called) throw new Error(next.error.message); return next(e); });
  on('ui.render', { component: 'CommandOutput' }, drawInline)
    .catch(async ($, e, next) => { if (next.called) throw new Error(next.error.message); return next(e); });
}
