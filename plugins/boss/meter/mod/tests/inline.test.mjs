import test from 'node:test';
import assert from 'node:assert/strict';
import { encodeInlineMessage, parseInlineMessage, inlineFallback, readInlineReport, parseReportArtifact, cleanLegacyMarkers } from '../src/inline.mjs';
import { register } from '../src/register.mjs';

const task = (id, label, state) => ({ id, label, state, wave: 'B', elapsedMs: 128000, tokens: 16000, partialTokens: false, model: 'sonnet', effort: 'high', result: state === 'done' ? 'review.md' : null, sha: null, detail: null, action: null });
const now = task('2', 'Implement reporting', 'running'), next = task('3', 'Validate the report', 'queued'), recent = task('1', 'Review completed', 'done');
const meter = {
  current: { label: 'Wave 2/3', done: 1, total: 4, elapsedMs: 128000, tokens: 16000, partialTokens: true, running: 2, state: 'running', bar: '▰▱▱▱', ticking: true },
  total: { label: 'Total', done: 4, total: 7, elapsedMs: 380000, tokens: 44000, partialTokens: true, running: 2, state: 'running', bar: '▰▰▰▰▱▱▱', ticking: true },
  waves: { label: 'Waves', done: 1, total: 3, elapsedMs: 380000, tokens: 44000, partialTokens: true, running: 1, state: 'running', bar: '▰▱▱', ticking: true },
  attention: [], tokenNote: 'Tokens are reported, not billing estimates.',
  tasks: { now: [now], next: [next], recent: [recent], all: [recent, now, next], nowCount: 1, nextCount: 1, recentCount: 1 },
};
const hash = 'a'.repeat(64), path = `/r/reports/${hash}.json`;
const artifact = () => ({ schema: 'boss.report/1', run: 'fixture', created_at: '2026-11-11T19:10:00Z', columns: 72, meter: structuredClone(meter), text: inlineFallback(meter, 72) });
const report = () => `${artifact().text}\n\n[DETAILS](file:///r/reports/${hash}.md)`;
const io = raw => ({ read: async p => { assert.equal(p, path); return raw; } });
const flatten = tree => [tree, ...(tree.children || []).filter(x => x && typeof x === 'object').flatMap(flatten)];
const keyed = (tree, key) => flatten(tree).find(n => n.props?.key === key);

test('fallback is useful without the plugin and never carries opaque payloads', () => {
  const output = encodeInlineMessage(meter, { run: 'fixture' });
  assert.match(output, /➜    Implement reporting/);
  assert.match(output, /⮡    Validate the report/);
  assert.match(output, /Details: \/boss-status fixture --details/);
  assert.doesNotMatch(output, /<!--|base64|boss-meter:v/);
  assert.deepEqual(parseInlineMessage(output), []);
});

test('immutable report matches inside mixed prose and preserves both sides exactly', async () => {
  const prefix = 'Here is **the latest progress**.\n\n';
  const suffix = '\n\nKeep this [reference](https://example.com) and explanation.';
  const payload = await readInlineReport(io(JSON.stringify(artifact())), '/r', prefix + report() + suffix);
  assert.equal(payload.prefix, prefix);
  assert.equal(payload.suffix, suffix);
  assert.deepEqual(payload.meter, meter);
  assert.equal(payload.run, 'fixture');
});

test('three summaries and historical two-row labels remain readable', () => {
  const current = artifact();
  assert.ok(parseReportArtifact(JSON.stringify(current)));
  const twoArrows = { ...current, text: current.text.replace(/^[☑☐].*\n/m, '') };
  assert.ok(parseReportArtifact(JSON.stringify(twoArrows)));
  const old = { ...twoArrows, text: twoArrows.text.replace('➜    ', 'NOW  ').replace('⮡    ', 'NEXT ') };
  assert.ok(parseReportArtifact(JSON.stringify(old)));
});

test('presentation changes to badges, ticks, clocks and ETA do not break artifact enhancement', async () => {
  const prefix = 'Preserve this explanation.\n\n', suffix = '\n\nPreserve this ending.';
  for (const [wave, tasks] of [['[W]', '[T]'], ['🆆', '🆃'], ['🅆', '🅃'], ['🆆\uFE0F', '🆃\uFE0F']]) {
    const p = artifact();
    p.text = `\`\`\`text\n${wave}  ▰       1h 02m   1,234 tokens   ETA ~5m\n${tasks}  ▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱   --:--  —  ETA pending\n☑ Done review\n➜ Implement reporting\n⮡ Validate the report\n\`\`\``;
    const message = `${prefix}${p.text}\n\n[DETAILS](file:///r/reports/${hash}.md)${suffix}`;
    const payload = await readInlineReport(io(JSON.stringify(p)), '/r', message);
    assert.ok(payload, `${wave}/${tasks}`);
    assert.deepEqual(payload.meter, meter, 'display cells never become invented task counts');
    assert.equal(payload.prefix, prefix);
    assert.equal(payload.suffix, suffix);
    assert.equal(await readInlineReport(io(JSON.stringify(p)), '/r', message.replace('ETA ~5m', 'ETA ~6m')), null, 'stored text must still match exactly');
  }
  for (const text of [
    '```text\nHere is unrelated prose.\n```',
    '```text\n[T] ▰ 00:12 4K\nUnrelated paragraph inside a report.\n```',
    '```text\n[T] ▰ 00:12 4K\nNEXT queued\nNOW active\n```',
    '```text\n[T] ▰ \u001b[31m00:12 4K\n```',
    '```text\n[T] ' + 'x'.repeat(1001) + '\n```',
  ]) assert.equal(parseReportArtifact(JSON.stringify({ ...artifact(), text })), null);
});

test('untrusted paths, altered reports and malformed artifacts remain plain text', async () => {
  let reads = 0;
  assert.equal(await readInlineReport({ read: async () => { reads++; } }, '/safe', report()), null);
  assert.equal(reads, 0);
  assert.equal(await readInlineReport(io(JSON.stringify(artifact())), '/r', report().replace('16K', '17K')), null);
  assert.equal(await readInlineReport(io('{'), '/r', report()), null);
  for (const alter of [
    p => { p.schema = 'boss.report/2'; }, p => { p.run = '../other'; }, p => { p.meter.current.tokens = -1; },
    p => { delete p.run; }, p => { p.run = 123; }, p => { p.created_at = 123; },
    p => { p.meter.tasks.all[0].result = {}; }, p => { p.meter.tasks.nowCount = -1; }, p => { p.meter.tasks = undefined; },
    p => { p.text = 'An unrelated paragraph'; }, p => { p.meter.current.label = '\u001b[31m'; },
  ]) {
    const p = artifact(); alter(p);
    assert.equal(parseReportArtifact(JSON.stringify(p)), null);
  }
  assert.equal(parseReportArtifact('x'.repeat(512 * 1024 + 1)), null);
});

test('only validated legacy comments are removed, including wrapped base64', () => {
  const old = { v: 3, run: 'fixture', columns: 72, meter };
  const wrapped = Buffer.from(JSON.stringify(old)).toString('base64url').match(/.{1,70}/g).join('\n');
  const prefix = 'Keep this prose.\n```text\nold readable meter\n```\n';
  const suffix = '\nKeep this ending.';
  assert.equal(cleanLegacyMarkers(prefix + `<!-- boss-meter:v3:${wrapped} -->` + suffix), prefix + suffix);
  assert.equal(cleanLegacyMarkers('<!-- boss-meter:v3:broken -->'), '<!-- boss-meter:v3:broken -->');
  assert.equal(cleanLegacyMarkers('<!-- unrelated -->'), '<!-- unrelated -->');
});

test('actual hook keeps bars first and reveals preserved narrative bullets only in Details', async () => {
  const handlers = [];
  register((event, matcher, fn) => { handlers.push({ event, matcher, fn }); return { catch() {} }; }, { progress_dir: '/r' });
  const handler = handlers.find(h => h.event === 'ui.render' && h.matcher.component === 'AssistantMessage').fn;
  const ui = Object.fromEntries(['Box', 'Text', 'Button', 'Markdown'].map(type => [type, props => ({ type, props, children: props.children || [] })]));
  let reads = 0;
  const $ = { env: { get: async () => '/h' }, fs: { read: async p => { reads++; assert.equal(p, path); return JSON.stringify(artifact()); } }, ui: { resolve: () => ui, invalidate() {} } };
  const e = text => ({ component: 'AssistantMessage', requestId: 'message-1', props: { text, isFirstOfReply: true }, viewport: { columns: 80 }, surface: 'vscode' });
  for (const text of ['ordinary prose', encodeInlineMessage(meter, { run: 'fixture' }), '<!-- boss-meter:v3:broken -->']) {
    const message = e(text);
    assert.equal(await handler($, message, async value => value), message);
  }
  const prefix = 'Before **bold**.\n\n```js\nconst preserved = true;\n```\n\n';
  const suffix = '\n\n- After [link](https://example.com).';
  const message = e(prefix + report() + suffix);
  const output = await handler($, message, async () => { throw new Error('unexpected fallback'); });
  assert.equal(output.children[0].props.key, 'boss-meter-inline');
  assert.equal(output.children[0].children[0].props.key, 'meter');
  assert.equal(keyed(output, 'report-prefix'), undefined);
  assert.equal(keyed(output, 'report-suffix'), undefined);
  assert.ok(keyed(output, 'summary-done'));
  assert.ok(keyed(output, 'summary-now'));
  assert.ok(keyed(output, 'summary-next'));
  for (const [key, glyph] of [['done', '☑'], ['now', '➜'], ['next', '⮡']]) {
    assert.deepEqual(keyed(output, `summary-${key}-icon-text`).children, [glyph]);
    assert.equal(keyed(output, `summary-${key}-icon-text`).props.color, key === 'done' ? undefined : '#3B82F6');
    assert.equal(keyed(output, `summary-${key}-icon`).props.width, 5);
    assert.equal(keyed(output, `summary-${key}-icon`).props.minWidth, 5);
    assert.equal(keyed(output, `summary-${key}`).props.columnGap, 0);
    assert.equal(keyed(output, `summary-${key}`).props.alignItems, 'stretch');
    const footprint = keyed(output, `summary-${key}-icon-footprint`).props;
    assert.equal(footprint.width, keyed(output, 'waves-badge').props.width);
    assert.equal(footprint.height, keyed(output, 'waves-badge').props.height);
    assert.equal(footprint.justifyContent, 'center');
    assert.equal(footprint.borderStyle, undefined);
  }
  assert.equal(keyed(output, 'inline-controls').props.marginTop, 0.5);
  keyed(output, 'inline-details').props.onPress();
  const expanded = await handler($, message, async () => { throw new Error('unexpected fallback'); });
  assert.equal(keyed(expanded, 'report-prefix').props.text, prefix);
  assert.equal(keyed(expanded, 'report-suffix').props.text, suffix);
  assert.ok(keyed(expanded, 'report-prefix-bullet'));
  assert.equal(keyed(expanded, 'report-suffix-bullet'), undefined, 'existing lists do not get a duplicate bullet');
  assert.ok(keyed(expanded, 'details-current'));
  assert.ok(keyed(expanded, 'details-queued'));
  assert.ok(keyed(expanded, 'details-results'));
  assert.ok(keyed(expanded, 'inline-token-note'));
  assert.deepEqual(keyed(expanded, 'detail-1-check').children, ['☑']);
  assert.deepEqual(keyed(expanded, 'detail-2-check').children, ['☐']);
  assert.equal(reads, 1, 'immutable artifact is reused without consulting a live run');
});

test('all active rows, actual long bars, and a single-wave job estimate enhance natively', () => {
  const p = artifact();
  p.meter.waves = null;
  p.meter.current.total = 15;
  p.meter.current.bar = '▰' + '▱'.repeat(14);
  p.meter.tasks.now = [1, 2, 3, 4].map(n => ({ ...now, id: `active-${n}`, activity: `Checking ${n}`, instructions: `Step one\nStep two` }));
  p.meter.tasks.all = [...p.meter.tasks.now];
  p.meter.tasks.nowCount = 4;
  p.meter.forecast = { checkpoint: { text: 'ETA ~3:45PM' }, finish: { text: 'ETA ~4:00PM' } };
  p.text = inlineFallback(p.meter, 72);
  assert.ok(parseReportArtifact(JSON.stringify(p)));
  p.meter.tasks.now[0].activity = '\u001b[31munsafe';
  assert.equal(parseReportArtifact(JSON.stringify(p)), null);
});
