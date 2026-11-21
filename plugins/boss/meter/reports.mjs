// Immutable presentation artifacts. Run state, plans, leases and ledgers are never written.
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { renderMeterText, formatClock, formatTokens } from './meter.mjs';

const MAX_BYTES = 512 * 1024;
const escape = value => String(value ?? '').replace(/[\\`*_\[\]<>]/g, '\\$&');
const code = value => '`' + String(value).replace(/`/g, "'") + '`';

export function reportText(meter, columns = 72) {
  return [renderMeterText(meter, { columns, fenced: true }),
    ...meter.attention.map(text => `Attention: ${text}`)].join('\n');
}

export function reportMarkdown(report) {
  const lines = [`# ${escape(report.run)}`, '', `Snapshot captured ${report.created_at}.`, '', report.text, ''];
  const tasks = report.meter.tasks?.all ?? [];
  const groups = [
    ['In progress / needs attention', task => ['running', 'blocked', 'waiting_input', 'retry', 'interrupted'].includes(task.state)],
    ['Upcoming', task => ['queued', 'pending', 'held'].includes(task.state)],
    ['Completed', task => ['done', 'completed'].includes(task.state)],
  ];
  const shown = new Set();
  const add = task => {
    shown.add(task.id);
    const meta = [task.state.replace(/_/g, ' '), task.wave,
      task.model ? `${task.model}${task.effort ? '/' + task.effort : ''}` : null,
      task.elapsedMs == null ? null : formatClock(task.elapsedMs),
      task.tokens == null ? null : `${formatTokens(task.tokens)} reported tokens`].filter(Boolean).map(escape);
    const result = [task.result, task.sha].filter((value, index, all) => value && all.indexOf(value) === index).map(code);
    lines.push(`- [${['done', 'completed'].includes(task.state) ? 'x' : ' '}] **${escape(task.label)}** (${escape(task.id)})${task.activity ? ' — ' + escape(task.activity) : ''} — ${meta.join(' · ')}${task.waitingReason ? '. Waiting for ' + escape(task.waitingReason) : ''}`);
    const notes = [...new Set([task.instructions, task.detail, task.action].filter(Boolean))];
    if (notes.length || result.length) lines.push('', '<details>', `<summary>${escape(task.label)} — instructions and notes</summary>`, '', ...notes.map(note => escape(note)), ...(result.length ? ['', 'Result: ' + result.join(' · ')] : []), '', '</details>', '');
  };
  for (const [title, includes] of groups) {
    const items = tasks.filter(includes);
    if (!items.length) continue;
    lines.push(`## ${title}`, ''); items.forEach(add); lines.push('');
  }
  const other = tasks.filter(task => !shown.has(task.id));
  if (other.length) { lines.push('## Other tasks', ''); other.forEach(add); lines.push(''); }
  const forecast = report.meter.forecast;
  if (forecast) {
    const date = value => value == null ? 'Unknown' : new Date(value).toLocaleString('en-US', { timeZone: 'America/Los_Angeles', timeZoneName: 'short' });
    lines.push('## Forecast', '', `- Next reviewable result: ${escape(forecast.checkpoint.label)} — ${escape(forecast.checkpoint.text)}`,
      ...(forecast.currentWave ? [`- Current wave: ${escape(forecast.currentWave.text)}`] : []),
      `- Whole job: ${escape(forecast.finish.text)}`, `- Original result forecast: ${date(forecast.originalCheckpointAt)}`,
      `- Original job forecast: ${date(forecast.originalFinishAt)}`, `- Waiting for input: ${formatClock(report.meter.total.waitingMs || 0)}`, '', escape(forecast.basis), '');
    for (const delay of forecast.delayHistory || []) lines.push(`- ${escape(delay.at)}: ${escape(delay.reason)}; revised finish ${date(delay.finishAt)}.`);
    lines.push('');
  }
  lines.push('## Reporting basis', '',
    'Upcoming tasks follow plan order. Waiting reasons reflect recorded dependencies; readiness is unknown when dependencies are not recorded.', '',
    'Elapsed clocks exclude recorded intervals when all work is waiting for owner input. Parallel agents do not multiply elapsed time. Both meter clocks switch to HH:MM:SS when either reaches an hour. This report preserves the values when posted.', '');
  if (report.meter.tokenNote) lines.push(escape(report.meter.tokenNote), '');
  return lines.join('\n');
}

async function writeImmutable(file, content) {
  try { await fs.writeFile(file, content, { flag: 'wx', mode: 0o600 }); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    const stat = await fs.lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink() || await fs.readFile(file, 'utf8') !== content) throw new Error('Report artifact already exists with different content');
  }
}

export async function writeReportArtifact(meter, { root, run, columns = 72, createdAt = new Date().toISOString() }) {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(run)) throw new Error('Invalid report run');
  const text = reportText(meter, columns);
  const report = { schema: 'boss.report/1', run, created_at: createdAt, columns, meter, text };
  const json = JSON.stringify(report);
  if (Buffer.byteLength(json) > MAX_BYTES) throw new Error('Report exceeds supported size');
  const id = createHash('sha256').update(json).digest('hex');
  const dir = path.resolve(root, 'reports');
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  const stat = await fs.lstat(dir);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('Report folder must be a directory');
  const jsonPath = path.join(dir, `${id}.json`), markdownPath = path.join(dir, `${id}.md`);
  await writeImmutable(jsonPath, json);
  await writeImmutable(markdownPath, reportMarkdown(report));
  const href = pathToFileURL(markdownPath).href;
  return { report, id, jsonPath, markdownPath, href, output: `${text}\n\n[DETAILS](${href})` };
}
