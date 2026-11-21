import { test } from 'node:test';
import assert from 'node:assert/strict';
import { meterFromSnapshot, renderMeterText, summaryTaskText } from '../meter.mjs';
import { reportMarkdown } from '../reports.mjs';
import { renderInline } from '../mod/src/layout.mjs';

const instruction = 'Synthesis: one drift table grouped by the shared rule each drift breaks, with proposed fix rows for stage 3';
const routeInstruction = 'Audit index pages: /, /work, /articles, /notes, /tags/<tag>';
const nowMs = Date.parse('2026-11-11T20:00:00Z');
const build = items => meterFromSnapshot({ state: 'running', items, updated_at: new Date(nowMs).toISOString() }, { nowMs });
const ui = Object.fromEntries(['Box', 'Text', 'Button', 'Markdown'].map(type => [type, props => ({ type, props, children: props.children || [] })]));
const keyed = (tree, key) => tree && typeof tree === 'object' ? tree.props?.key === key ? tree : (tree.children || []).map(child => keyed(child, key)).find(Boolean) : undefined;
const text = tree => typeof tree === 'string' ? tree : (tree?.children || []).map(text).join('');

test('instruction and route-heavy rows stay in Details instead of becoming clipped summaries', () => {
  const meter = build([
    { id: 'long', kind: 'row', label: instruction, state: 'done' },
    { id: 'routes', kind: 'row', label: routeInstruction, state: 'done' },
    { id: 'clear', kind: 'row', label: 'Check page layouts', state: 'running', activity: 'Comparing headings' },
  ]);
  const compact = renderMeterText(meter);
  assert.doesNotMatch(compact, /Synthesis|Audit index|\/tags|☑/);
  assert.match(compact, /Check page layouts — Comparing headings/);
  assert.equal(meter.tasks.all.find(task => task.id === 'long').instructions, instruction);
  const markdown = reportMarkdown({ run: 'fixture', created_at: new Date(nowMs).toISOString(), text: compact, meter });
  assert.ok(markdown.includes(instruction), 'full instructions remain in the durable report');
  assert.match(markdown, /Audit index pages/);
  const collapsed = renderInline(ui, { run: 'fixture', meter }, false, () => {}, 50);
  assert.doesNotMatch(text(keyed(collapsed, 'task-summary')), /Synthesis|Audit index/);
  const expanded = renderInline(ui, { run: 'fixture', meter }, true, () => {}, 50, { expandedTasks: new Set(['long']) });
  assert.ok(text(expanded).includes(instruction), 'hidden row is recoverable through task Details');
});

test('authored short names replace long task instructions without losing those instructions', () => {
  const meter = build([{ id: 'named', kind: 'row', label: instruction, short_label: 'Summarize layout issues', state: 'done' }]);
  assert.match(renderMeterText(meter), /☑    Summarize layout issues/);
  assert.equal(meter.tasks.all[0].instructions, instruction);
  assert.equal(summaryTaskText({ label: 'Audit /about and /for/<token>' }), null);
  assert.equal(summaryTaskText({ label: 'Review must_haves' }), null);
  assert.equal(summaryTaskText({ label: 'Review the combined result', activity: instruction }), 'Review the combined result');
});

test('every visible summary has a shrinkable text column beside its fixed marker', () => {
  const meter = build([{ id: 'clear', kind: 'row', label: 'Check page layouts', state: 'running', activity: 'Comparing headings' }]);
  const tree = renderInline(ui, { run: 'fixture', meter }, false, () => {}, 30);
  assert.equal(keyed(tree, 'summary-now').props.alignItems, 'stretch');
  assert.equal(keyed(tree, 'summary-now-icon').props.flexShrink, 0);
  const content = keyed(tree, 'summary-now-content-0');
  assert.equal(content.props.minWidth, 0);
  assert.equal(content.props.flexShrink, 1);
  assert.equal(content.props.flexGrow, 1);
  assert.equal(content.children[0].props.wrap, 'wrap');
});
