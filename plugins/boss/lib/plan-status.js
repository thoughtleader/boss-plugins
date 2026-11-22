'use strict';
// plan-status: the one parser for the `## Status` table of a run's PLAN.md, and the one editor of a row's line in it.
// Plain node, no dependencies. manifest.js (planStatusRows: planDone, status, the row-ID list) and ledger.js close --plan-status share it;
// this module requires neither of them, so manifest.js can load it without ever requiring ledger.js.
// A cell holds a literal `|` as `\|`: the parser splits on unescaped pipes only and hands cells back unescaped; the editor escapes on write.

const PIPE = /(?<!\\)\|/g;

// cellSpans(line) -> [[start, end)] of every cell's raw text in the line (the leading and trailing pipe are not cells).
function cellSpans(line) {
  const cuts = [...line.matchAll(PIPE)].map(m => m.index), spans = [];
  let from = 0, k = 0;
  if (cuts.length && !line.slice(0, cuts[0]).trim()) { from = cuts[0] + 1; k = 1; }
  for (; k < cuts.length; k++) { spans.push([from, cuts[k]]); from = cuts[k] + 1; }
  if (line.slice(from).trim()) spans.push([from, line.length]);
  return spans;
}
const unesc = s => s.trim().replace(/\\\|/g, '|');
const esc = s => String(s).replace(/\|/g, '\\|');

// parseStatusTable(PLAN.md text) -> null when there is no `## Status` section or no table in it; else
// { head: [lower-cased header names], idCol, col (the Status column, -1 when missing), headLine, rows: [{ line, row, cells, spans }] }
// (line: index into txt.split(/\r?\n/); row: the '#' column, else the first, lower-cased; cells: trimmed and unescaped).
function parseStatusTable(txt) {
  if (typeof txt !== 'string') return null;
  const lines = txt.split(/\r?\n/), h = lines.findIndex(l => /^##\s+Status\b/i.test(l));
  if (h < 0) return null;
  let i = h + 1;
  while (i < lines.length && !/^##\s/.test(lines[i]) && !lines[i].trim().startsWith('|')) i++;
  if (i >= lines.length || !lines[i].trim().startsWith('|')) return null;
  const spansOf = l => cellSpans(l), cellsOf = (l, sp) => sp.map(([a, b]) => unesc(l.slice(a, b)));
  const head = cellsOf(lines[i], spansOf(lines[i])).map(c => c.toLowerCase());
  const t = { head, idCol: Math.max(0, head.indexOf('#')), col: head.indexOf('status'), headLine: i, rows: [] };
  for (let j = i + 1; j < lines.length && lines[j].trim().startsWith('|'); j++) {
    if (/^\|[\s:|-]+$/.test(lines[j].trim())) continue; // the separator row
    const spans = spansOf(lines[j]), cells = cellsOf(lines[j], spans);
    t.rows.push({ line: j, row: String(cells[t.idCol] || '').toLowerCase(), cells, spans });
  }
  return t;
}

// planStatusRows(PLAN.md text) -> [{row, status}] from the Status table, or null when there is no such table or it has no Status column.
function planStatusRows(txt) {
  const t = parseStatusTable(txt);
  if (!t || t.col < 0) return null;
  return t.rows.map(r => ({ row: r.row, status: r.cells[t.col] || '' }));
}

// The columns close --plan-status writes, found by header name (lower-cased), never by position.
const COLUMNS = {
  status: { label: 'Status', is: h => h === 'status' },
  ran: { label: 'Ran on (model/effort)', is: h => h.startsWith('ran on') },
  attempt: { label: 'Attempt', is: h => h === 'attempt' },
  firstPass: { label: 'First-pass review', is: h => h.startsWith('first-pass') },
  retries: { label: 'Retries', is: h => h === 'retries' },
  escalated: { label: 'Escalated', is: h => h === 'escalated' },
  result: { label: 'SHA / result', is: h => h.startsWith('sha') || h.startsWith('result') },
};

// editStatusRow(txt, rowId, values) -> { text } with only that row's line changed (every other byte, line endings included, as it was),
// or { error } naming the failed check. values: { status, ran, attempt, firstPass, retries?, escalated?, result? }; a key that is absent
// (or undefined) leaves its cell alone. Texts are written with `|` as `\|`.
function editStatusRow(txt, rowId, values) {
  const t = parseStatusTable(txt);
  if (!t) return { error: 'PLAN.md has no `## Status` table' };
  const want = Object.keys(values).filter(k => values[k] !== undefined), at = {};
  for (const k of want) {
    at[k] = t.head.findIndex(COLUMNS[k].is);
    if (at[k] < 0) return { error: `the Status table has no '${COLUMNS[k].label}' column` };
  }
  const hits = t.rows.filter(r => r.row === String(rowId).toLowerCase());
  if (hits.length !== 1) return { error: `the Status table has ${hits.length} lines for row ${rowId}, not exactly one` };
  const hit = hits[0], short = want.find(k => at[k] >= hit.spans.length);
  if (short) return { error: `the line for row ${rowId} has no cell under '${COLUMNS[short].label}'` };
  const parts = txt.split(/(\r?\n)/); // lines at the even indexes, their own line endings between them
  let line = parts[hit.line * 2];
  for (const k of want.sort((a, b) => at[b] - at[a])) { // right to left, so the spans of the cells still to do stay valid
    const [a, b] = hit.spans[at[k]];
    line = line.slice(0, a) + ' ' + esc(values[k]) + (b < line.length ? ' ' : '') + line.slice(b);
  }
  parts[hit.line * 2] = line;
  return { text: parts.join('') };
}

module.exports = { parseStatusTable, planStatusRows, editStatusRow, COLUMNS, esc };
