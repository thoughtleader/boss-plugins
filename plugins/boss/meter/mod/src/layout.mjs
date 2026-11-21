import { clockHours, clockParts, windowedBar, formatClock, formatTokens, summaryTaskText } from '../../meter.mjs';
import { clean } from './registry.mjs';

function markerColumn(ui, { key, textKey, shapeKey, glyph, surface = 'vscode', border = false, color }) {
  const { Box, Text } = ui;
  return Box({ key, width: 5, minWidth: 5, flexShrink: 0, flexWrap: 'nowrap', alignItems: 'center', children: [
    Box({ key: shapeKey, width: 3, minWidth: 3, ...(surface === 'terminal' ? {} : { height: 1.2, minHeight: 1.2 }), ...(border && surface !== 'terminal' ? { borderStyle: 'single' } : {}), justifyContent: 'center', alignItems: 'center', flexShrink: 0, flexWrap: 'nowrap', children: [
      Text({ key: textKey, ...(color ? { color } : {}), wrap: 'wrap', children: [surface === 'terminal' && border ? `[${glyph}]` : glyph] }),
    ] }),
  ] });
}

// Use Box widths, not whitespace or Text.tabular: the VS Code mod renderer
// ignores mono/tabular. The same numeric boxes appear in both rows.
export function meterTree(ui, meter, columns = 60, { surface = 'vscode' } = {}) {
  const { Box, Text } = ui;
  const text = (key, value, dim = false, preserve = false) => Text({ key, dimColor: dim, wrap: preserve ? 'wrap' : 'truncate-end', children: [value] });
  const cell = (key, width, value, dim = false, right = false, preserve = false) => Box({ key, width, minWidth: width, flexShrink: 0, flexWrap: 'nowrap', justifyContent: right ? 'flex-end' : 'flex-start', children: [text(`${key}-text`, value, dim, preserve)] });
  const tok = s => formatTokens(s.tokens);
  const hours = clockHours(meter);
  const clock = s => formatClock(s.elapsedMs, { hours });
  const eta = key => {
    const f = meter.forecast;
    if (!f) return '';
    const value = point => (point?.text || '—').replace(/^ETA\s+/, '');
    if (key === 'waves') return value(f.finish);
    const current = f.currentWave ?? (!meter.waves ? f.finish : null);
    return current ? value(current) : '';
  };
  const scopes = [
    ...(meter.waves ? [{ key: 'waves', label: 'Waves', scope: meter.waves, dim: true }] : []),
    { key: 'tasks', label: 'Tasks', scope: meter.current, dim: false },
  ];
  const widths = {
    // VS Code uses proportional-font ch widths. A bar glyph is wider than a
    // digit, and "Waves" exceeds five ch; reserve room for the actual ink.
    label: 5,
    progress: 24,
    time: Math.max(5, ...scopes.map(s => clock(s.scope).length)),
    tokens: Math.max(6, ...scopes.map(s => tok(s.scope).length)),
    // AM/PM and ~ are wider than digits in the host's proportional font.
    eta: Math.max(0, ...scopes.map(s => eta(s.key) ? eta(s.key).length + 2 : 0)),
  };
  const fixedWidth = widths.label + widths.time + widths.tokens + 4;
  const wide = columns >= fixedWidth + 24;
  const etaInline = columns >= fixedWidth + 24 + (widths.eta ? widths.eta + 2 : 0);
  // Spend spare width on full-size ticks. Keep both metric rows aligned and
  // retire the oldest completed ticks only when the track runs out of room.
  widths.progress = wide ? Math.max(24, columns - fixedWidth - (etaInline && widths.eta ? widths.eta + 2 : 0)) : Math.max(24, columns - widths.label);
  const cells = Math.floor(widths.progress / 2);
  const spacer = key => Box({ key, width: 2, minWidth: 2, flexShrink: 0, children: [] });
  const badge = key => markerColumn(ui, { key: `${key}-label`, textKey: `${key}-label-text`, shapeKey: `${key}-badge`, glyph: key === 'waves' ? '🆆' : '🆃', surface });
  const time = (key, scope) => {
    const parts = clockParts(clock(scope));
    return Box({ key: `${key}-time`, width: widths.time, minWidth: widths.time, flexShrink: 0, flexWrap: 'nowrap', justifyContent: 'flex-end', children: scope.hideElapsed ? [] : [
      Text({ key: `${key}-time-text`, wrap: 'wrap', children: [
        ...(parts.dim ? [Text({ key: `${key}-time-leading`, dimColor: true, children: [parts.dim] })] : []),
        Text({ key: `${key}-time-active`, children: [parts.active] }),
      ] }),
    ] });
  };
  const progress = (key, scope) => {
    const bar = windowedBar(scope, cells).text;
    const filled = bar.match(/^▰*/)[0];
    const empty = bar.slice(filled.length);
    return Box({ key: `${key}-progress`, width: widths.progress, minWidth: widths.progress, flexGrow: 1, flexShrink: 0, flexWrap: 'nowrap', children: [
      Text({ key: `${key}-progress-text`, wrap: 'wrap', children: [
        ...(filled ? [Text({ key: `${key}-filled`, wrap: 'wrap', children: [filled] })] : []),
        ...(empty ? [Text({ key: `${key}-empty`, dimColor: true, wrap: 'wrap', children: [
          Text({ key: `${key}-empty-ink`, dimColor: true, wrap: 'wrap', children: [empty] }),
        ] })] : []),
      ] }),
    ] });
  };
  const metrics = ({ key, scope, dim }) => Box({
    key: `${key}-values`, flexDirection: 'row', flexWrap: 'nowrap', columnGap: 0,
    children: [
      time(key, scope),
      spacer(`${key}-gap-b`),
      cell(`${key}-tokens`, widths.tokens, tok(scope), dim, true),

    ],
  });
  const etaContinuation = key => Box({ key: `${key}-eta-line`, flexDirection: 'row', flexWrap: 'nowrap', children: [
    Box({ key: `${key}-eta-gutter`, width: widths.label, minWidth: widths.label, flexShrink: 0, children: [] }),
    text(`${key}-eta`, eta(key), false, true),
  ] });
  const row = item => {
    const { key, label, scope, dim } = item;
    if (wide) {
      const main = Box({
      key, flexDirection: 'row', flexWrap: 'nowrap', columnGap: 0, alignItems: 'center',
      children: [
        badge(key),
        progress(key, scope),
        spacer(`${key}-gap-a`),
        time(key, scope),
        spacer(`${key}-gap-b`),
        cell(`${key}-tokens`, widths.tokens, tok(scope), dim, true),
      ...(eta(key) && etaInline ? [spacer(`${key}-gap-eta`), cell(`${key}-eta`, widths.eta, eta(key), false, false, true)] : []),
      ],
    });
      return eta(key) && !etaInline ? Box({ key: `${key}-block`, flexDirection: 'column', children: [main, etaContinuation(key)] }) : main;
    }
    // Break by complete groups, never by individual cells. Keep each scope's
    // bar above its clock/usage pair; the two scopes share all numeric widths.
    const barFits = columns >= widths.label + widths.progress;
    const valuesFit = columns >= widths.time + widths.tokens + 2;
    return Box({ key, flexDirection: 'column', children: [
      Box({ key: `${key}-heading`, flexDirection: barFits ? 'row' : 'column', flexWrap: 'nowrap', columnGap: 0, alignItems: 'center', children: [
        badge(key),
        progress(key, scope),
      ] }),
      ...(valuesFit ? [metrics(item)] : [
        text(`${key}-time`, scope.hideElapsed ? '' : clock(scope), dim),
        text(`${key}-tokens`, tok(scope), dim),
      ]),
      ...(eta(key) ? [etaContinuation(key)] : []),
    ] });
  };
  return Box({ key: 'meter', width: '100%', minWidth: 0, flexDirection: 'column', rowGap: surface === 'terminal' ? 0 : 0.15, children: scopes.map(row) });
}

function taskSummary(ui, meter, { surface = 'vscode' } = {}) {
  const { Box, Text } = ui;
  if (!meter.tasks) return [];
  const completed = meter.tasks.recent.filter(task => ['done', 'completed'].includes(task.state));
  const groups = [
    ['done', completed.length ? '☑' : '☐', completed, 'No new completions'],
    ['now', '➜', meter.tasks.now, meter.total.state === 'done' ? 'Complete' : 'No active tasks'],
    ['next', '⮡', meter.tasks.next, 'None queued'],
  ];
  return [Box({ key: 'task-summary', flexDirection: 'column', marginTop: 0.5, children: groups.map(([key, arrow, tasks, empty]) => Box({
    key: `summary-${key}`, flexDirection: 'column', columnGap: 0, alignItems: 'stretch', children: (tasks.length ? tasks : key === 'done' ? [] : [null]).filter(task => !task || summaryTaskText(task, { completed: key === 'done' })).map((task, index) => Box({
      key: `summary-${key}-${task?.id || 'empty'}`, flexDirection: 'row', flexWrap: 'nowrap', columnGap: 0, alignItems: 'flex-start', children: [
        markerColumn(ui, { key: `summary-${key}-icon${index || ''}`, textKey: `summary-${key}-icon-text${index || ''}`, shapeKey: `summary-${key}-icon-footprint${index || ''}`, glyph: arrow, surface, color: key === 'done' ? undefined : '#3B82F6' }),
        Box({ key: `summary-${key}-content-${index}`, flexDirection: 'column', minWidth: 0, flexGrow: 1, flexShrink: 1, children: [Text({ wrap: 'wrap', children: [task ? summaryTaskText(task, { completed: key === 'done' }) : empty] })] }),
      ],
    })),
  })) })];
}

function taskDetails(ui, meter, options = {}) {
  const { Box, Text, Button } = ui;
  if (!meter.tasks) return [];
  const done = new Set(['done', 'completed', 'failed', 'cancelled', 'ended', 'interrupted', 'not_run', 'skipped', 'dead']);
  const queued = new Set(['queued', 'pending']);
  const groups = [
    ['CURRENT', meter.tasks.all.filter(t => !done.has(t.state) && !queued.has(t.state))],
    ['QUEUED', meter.tasks.all.filter(t => queued.has(t.state))],
    ['RESULTS', meter.tasks.all.filter(t => done.has(t.state))],
  ];
  return groups.filter(([, tasks]) => tasks.length).map(([name, tasks]) => Box({ key: `details-${name.toLowerCase()}`, flexDirection: 'column', marginTop: 0.5, children: [
    Text({ dimColor: true, children: [name] }),
    ...tasks.map(task => {
      const expanded = options.expandedTasks?.has(task.id) === true;
      const notes = [...new Set([task.instructions, task.result, task.sha, task.detail, task.action].filter(value => value && value.trim() !== task.label.trim()))];
      const provenance = [task.wave, task.elapsedMs == null ? null : formatClock(task.elapsedMs), task.tokens == null ? null : formatTokens(task.tokens), [task.model, task.effort].filter(Boolean).join(' / ')].filter(Boolean).join(' · ');
      return Box({ key: `detail-${task.id}`, flexDirection: 'row', flexWrap: 'nowrap', columnGap: 1, children: [
        Box({ width: 2, minWidth: 2, flexShrink: 0, children: [Text({ key: `detail-${task.id}-check`, children: [['done', 'completed'].includes(task.state) ? '☑' : '☐'] })] }),
        Box({ flexDirection: 'column', flexGrow: 1, children: [
          Text({ wrap: 'wrap', children: [`${task.label} (${task.state.replace(/_/g, ' ')})`] }),
          ...(task.activity ? [Text({ wrap: 'wrap', children: [task.activity] })] : []),
          ...(provenance ? [Text({ dimColor: true, wrap: 'wrap', children: [provenance] })] : []),
          ...(notes.length ? [Box({ key: `task-controls-${task.id}`, flexDirection: 'row', justifyContent: 'flex-start', alignItems: 'flex-start', children: [
            Button({ key: `task-toggle-${task.id}`, label: expanded ? 'Hide task details' : 'Task details', dimColor: true, onPress: () => options.toggleTask?.(task.id) }),
          ] })] : []),
          ...(expanded ? notes.map((value, index) => Text({ key: `detail-${task.id}-${index}`, dimColor: true, wrap: 'wrap', children: [value] })) : []),
        ] }),
      ] });
    }),
  ] }));
}

function forecastDetails(ui, meter) {
  const { Box, Text } = ui;
  const f = meter.forecast;
  if (!f) return [];
  const date = at => (typeof at === 'number' ? Number.isFinite(at) : Number.isFinite(Date.parse(at))) ? new Date(at).toLocaleString('en-US', { timeZone: 'America/Los_Angeles', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : null;
  const lines = [
    `Current wave: ${meter.current.done} completed, ${meter.current.total - meter.current.done} unfinished. Earlier completed ticks may be hidden to keep upcoming work visible.`,
    f.checkpoint ? `Next reviewable result: ${f.checkpoint.label || 'Next result'} — ${f.checkpoint.text}` : null,
    f.originalCheckpointAt ? `Original result forecast: ${date(f.originalCheckpointAt) || f.originalCheckpointAt}` : null,
    f.originalFinishAt ? `Original job forecast: ${date(f.originalFinishAt) || f.originalFinishAt}` : null,
    meter.total.waitingMs > 0 ? `Waiting for your input: ${formatClock(meter.total.waitingMs)}` : null,
    f.basis,
    ...(f.delayHistory || []).map(item => `${date(item.at) || 'Earlier update'}: ~${Math.ceil(item.delayMs / 60000)} min delay — ${item.reason}`),
    f.delayMs > 0 ? `Forecast delay: ~${Math.ceil(f.delayMs / 60000)} min${f.reason ? ` — ${f.reason}` : ''}` : null,
  ].filter(Boolean);
  return [Box({ key: 'forecast-details', flexDirection: 'column', marginTop: 0.5, children: lines.map((line, i) => Text({ key: `forecast-note-${i}`, dimColor: true, wrap: 'wrap', children: [line] })) })];
}

export function renderPane(ui, state, callbacks, columns = 60, options = {}) {
  const { Box, Text, Button } = ui;
  const rows = [];
  const add = (key, value, dim = false, color) => rows.push(Text({ key, dimColor: dim, color, wrap: 'wrap', children: [value] }));
  if (state.loading) add('loading', 'Loading progress…', true);
  else if (!state.entry) {
    add('picker-heading', state.missing ? 'That run is no longer available. Choose a run.' : 'Choose a run');
    if (!state.entries.length) add('empty', 'No progress reports available.', true);
    for (const entry of state.entries.slice(0, 30)) rows.push(Button({
      key: `choose-${entry.key}`, label: `${entry.title} · ${clean(entry.snapshot.state)}`,
      onPress: () => callbacks.select(entry.key),
    }));
  } else {
    rows.push(meterTree(ui, state.meter, columns, options), ...taskSummary(ui, state.meter, options));
    for (const [index, message] of state.meter.attention.slice(0, 2).entries()) add(`attention-${index}`, clean(message, 300), false, 'warning');
    rows.push(Box({ key: 'meter-controls', flexDirection: 'row', columnGap: 1, marginTop: 0.5, children: [
      Button({ key: 'toggle-details', label: state.details ? 'HIDE DETAILS' : 'DETAILS', dimColor: true, onPress: callbacks.details }),
      Button({ key: 'choose-run', label: 'Change run', dimColor: true, onPress: callbacks.change }),
    ] }));
    if (state.details) {
      rows.push(...taskDetails(ui, state.meter, options), ...forecastDetails(ui, state.meter));
      add('run-title', state.entry.title, true);
      add('run-key', state.entry.key, true);
      if (state.meter.tokenNote) add('token-note', state.meter.tokenNote, true);
      add('time-note', 'Elapsed excludes recorded waits for your input. Both clocks use HH:MM:SS once either reaches one hour. Parallel agents do not multiply elapsed time.', true);
    }
  }
  if (state.errors.length) add('read-errors', `${state.errors.length} unreadable report${state.errors.length === 1 ? '' : 's'}`, true);
  return Box({ key: 'boss-meter-pane', flexDirection: 'column', children: rows });
}

export function renderInline(ui, payload, expanded, toggle, columns = 72, options = {}) {
  const { Box, Text, Button, Markdown } = ui;
  const meter = payload.meter;
  const rows = [meterTree(ui, meter, columns, options), ...taskSummary(ui, meter, options)];
  for (const [i, message] of meter.attention.entries()) rows.push(Text({ key: `inline-attention-${i}`, color: 'warning', wrap: 'wrap', children: [message] }));
  rows.push(Box({ key: 'inline-controls', flexDirection: 'row', columnGap: 1, marginTop: 0.5, children: [
    Button({ key: 'inline-details', label: expanded ? 'HIDE DETAILS' : 'DETAILS', dimColor: true, onPress: toggle }),
  ] }));
  if (expanded) {
    for (const [key, source] of [['report-prefix', payload.prefix], ['report-suffix', payload.suffix]]) {
      if (!source?.trim()) continue;
      if (/^\s*(?:[-*+]\s+|\d+[.)]\s+)/.test(source)) {
        rows.push(Markdown({ key, text: source }));
        continue;
      }
      // Keep the original Markdown intact. A separate marker makes the note
      // a visual bullet without rewriting code fences, links or existing lists.
      rows.push(Box({ key: `${key}-bullet`, flexDirection: 'row', flexWrap: 'nowrap', columnGap: 1, children: [
        Box({ width: 2, minWidth: 2, flexShrink: 0, children: [Text({ children: ['•'] })] }),
        Box({ flexDirection: 'column', flexGrow: 1, children: [Markdown({ key, text: source })] }),
      ] }));
    }
    rows.push(...taskDetails(ui, meter, options), ...forecastDetails(ui, meter));
    rows.push(Text({ key: 'inline-run', dimColor: true, children: [payload.run] }));
    if (meter.tokenNote) rows.push(Text({ key: 'inline-token-note', dimColor: true, wrap: 'wrap', children: [meter.tokenNote] }));
    rows.push(Text({ key: 'inline-time-note', dimColor: true, wrap: 'wrap', children: ['Elapsed excludes recorded waits for your input. Both clocks use HH:MM:SS once either reaches one hour. This message records the values when it was posted.'] }));
  }
  return Box({ key: 'boss-meter-inline', width: '100%', minWidth: 0, flexGrow: 1, flexDirection: 'column', children: rows });
}
