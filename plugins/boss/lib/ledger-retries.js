'use strict';
// ledger.js report retries: what the retried or failed attempts cost, grouped by cause, then role, then model/effort.
// Read-only and db-free: it reads runs/*/ledger.jsonl only (no sync, no metrics.db, no node:sqlite), so a fresh install can run it.
//
// A "retried attempt" is a ledger line with firstPass false (it needed fixes, or failed). Its tokens are the cost of that attempt.
// Cause: the line's own cause; else the retry_cause recorded on the next attempt of the same row (add and the launch event write it
// there); else 'unstated'. 'unstated' also covers the literal value 'unstated' and every line from before the field existed.
const UNSTATED = 'unstated';

function causeOf(line, next) {
  for (const c of [line.cause, next && next.retry_cause]) if (typeof c === 'string' && c && c !== UNSTATED) return c;
  return UNSTATED;
}

// rowsByRun: [{ run, rows }]; filters: { run, role, repo, area } (already lower-cased, null = any). -> the data object (also the --json output).
function build(rowsByRun, filters = {}) {
  const lc = v => String(v == null ? '' : v).toLowerCase();
  const keep = x => (!filters.run || lc(x.run) === filters.run) && (!filters.role || lc(x.role) === filters.role)
    && (!filters.repo || lc(x.repo) === filters.repo) && (!filters.area || lc(x.area) === filters.area);
  const num = v => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  let lines = 0, tokens = 0;
  const groups = new Map(); // cause \u0000 role \u0000 model/effort -> { cause, role, model, effort, attempts, tokens }
  for (const { rows } of rowsByRun) {
    const byRow = new Map();
    for (const x of rows) { const k = String(x.row); if (!byRow.has(k)) byRow.set(k, []); byRow.get(k).push(x); }
    for (const list of byRow.values()) {
      list.sort((a, b) => String(a.ts || '').localeCompare(String(b.ts || '')));
      list.forEach((x, i) => {
        if (!keep(x)) return;
        lines++; tokens += num(x.tokens);
        if (x.firstPass !== false) return;
        const cause = causeOf(x, list[i + 1]), role = x.role || '-', model = x.model || '-', effort = x.effort || '-';
        const k = [cause, role, model, effort].join('\u0000');
        const g = groups.get(k) || { cause, role, model, effort, attempts: 0, tokens: 0 };
        g.attempts++; g.tokens += num(x.tokens); groups.set(k, g);
      });
    }
  }
  const list = [...groups.values()];
  const causes = {};
  for (const g of list) { const c = causes[g.cause] || (causes[g.cause] = { attempts: 0, tokens: 0 }); c.attempts += g.attempts; c.tokens += g.tokens; }
  const failed = list.reduce((s, g) => s + g.attempts, 0), failedTokens = list.reduce((s, g) => s + g.tokens, 0);
  list.sort((a, b) => causes[b.cause].tokens - causes[a.cause].tokens || a.cause.localeCompare(b.cause)
    || b.tokens - a.tokens || a.role.localeCompare(b.role) || (a.model + a.effort).localeCompare(b.model + b.effort));
  return { lines, tokens, retried: failed, retried_tokens: failedTokens, unstated: (causes[UNSTATED] || { attempts: 0 }).attempts,
    causes, groups: list };
}

function render(d, lib) {
  const { fmtTok, table } = lib, pct = (a, b) => (b ? Math.round(100 * a / b) + '%' : '-');
  if (!d.lines) return 'ledger: no attempts match';
  const out = [`retries by cause: ${d.retried} of ${d.lines} attempts needed a retry (firstPass n), tokens ${fmtTok(d.retried_tokens)} of ${fmtTok(d.tokens)} (${pct(d.retried_tokens, d.tokens)}); ` +
    `cause not stated on ${d.unstated} of ${d.retried} (${pct(d.unstated, d.retried)})`];
  if (!d.retried) return out.join('\n');
  out.push('cause: the retried line\'s own cause, else the next attempt\'s retry_cause, else unstated (a close or update --cause sets it; history before that cannot be recovered)');
  out.push(table(['cause', 'role', 'model/effort', 'attempts', 'tokens'], d.groups.map(g => [g.cause, g.role, `${g.model}/${g.effort}`, g.attempts, fmtTok(g.tokens)]), [16, 16, 24, 8, 9]));
  out.push('by cause: ' + Object.entries(d.causes).sort((a, b) => b[1].tokens - a[1].tokens)
    .map(([c, v]) => `${c} ${v.attempts} (${fmtTok(v.tokens)})`).join(', '));
  return out.join('\n');
}

module.exports = { build, render, causeOf, UNSTATED };
