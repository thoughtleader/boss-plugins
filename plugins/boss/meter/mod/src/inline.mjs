// Reports remain readable without the mod. Native enhancement reads only an
// immutable companion artifact under the configured Boss reports directory.
import { renderMeterText } from '../../meter.mjs';

const MAX_BYTES = 512 * 1024;
const RUN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const STATES = new Set(['queued', 'running', 'waiting_input', 'done', 'completed', 'failed', 'cancelled', 'ended', 'dead', 'interrupted', 'paused', 'stopped', 'stopping', 'pending']);
const safeText = (s, max) => typeof s === 'string' && s.length <= max && !/[\x00-\x1f\x7f-\x9f]/.test(s);
const value = n => n === null || (typeof n === 'number' && Number.isFinite(n) && n >= 0 && n <= 1e15);
const int = n => Number.isSafeInteger(n) && n >= 0 && n <= 1e6;

function scopeOk(s) {
  return s && typeof s === 'object' && !Array.isArray(s) && safeText(s.label, 80)
    && int(s.done) && int(s.total) && s.done <= s.total
    && value(s.elapsedMs) && value(s.tokens)
    && typeof s.partialTokens === 'boolean' && int(s.running) && s.running <= s.total
    && STATES.has(s.state) && typeof s.ticking === 'boolean'
    && typeof s.bar === 'string' && /^[▰▱ ]{0,1000}$/.test(s.bar);
}
function taskOk(t) {
  return t && typeof t === 'object' && safeText(t.id, 200) && safeText(t.label, 1000) && safeText(t.state, 80)
    && value(t.elapsedMs) && value(t.tokens) && typeof t.partialTokens === 'boolean'
    && ['wave', 'model', 'effort', 'result', 'sha', 'detail', 'action'].every(k => t[k] === null || safeText(t[k], 4000))
    && ['activity', 'waitingReason'].every(k => t[k] == null || safeText(t[k], 4000))
    && (t.instructions == null || (typeof t.instructions === 'string' && t.instructions.length <= 16000 && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/.test(t.instructions)));
}
function meterOk(m, legacy = false) {
  if (!m || typeof m !== 'object' || Array.isArray(m) || !scopeOk(m.current) || !scopeOk(m.total)
    || !(m.waves === null || scopeOk(m.waves)) || !Array.isArray(m.attention) || m.attention.length > 8
    || !m.attention.every(s => safeText(s, 500)) || !(m.tokenNote === null || safeText(m.tokenNote, 1500))) return false;
  if (legacy && m.tasks == null) return true;
  const t = m.tasks;
  return t && typeof t === 'object'
    && [['now', 1000], ['next', 1000], ['recent', 1000], ['all', 1000]].every(([k, max]) => Array.isArray(t[k]) && t[k].length <= max && t[k].every(taskOk))
    && ['nowCount', 'nextCount', 'recentCount'].every(k => int(t[k]));
}
function reportTextOk(text) {
  if (typeof text !== 'string' || text.length > 256 * 1024 || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/.test(text)) return false;
  const fenced = /^```(?:text)?\r?\n([\s\S]*?)\r?\n```((?:\r?\nAttention: [^\r\n]{1,500}){0,8})$/.exec(text);
  if (!fenced) return false;
  const lines = fenced[1].split(/\r?\n/);
  // The artifact model carries meaning; its exact stored text proves which
  // message fragment belongs to it. Do not couple recognition to typography,
  // tick counts, clock/token formatting, or optional columns such as ETA.
  const row = /^(\[W\]|\[T\]|🆆|🆃|🅆|🅃)[\uFE0E\uFE0F]?[ \t]+(?=\S).{1,1000}$/u;
  const waveLabels = new Set(['[W]', '🆆', '🅆']);
  const rows = [];
  while (lines.length && row.test(lines[0])) rows.push(waveLabels.has(row.exec(lines.shift())[1]) ? 'W' : 'T');
  if (!rows.length || rows.length > 2 || rows.at(-1) !== 'T' || (rows.length === 2 && rows[0] !== 'W') || lines.length > 3000) return false;
  if (/^ +(?:Whole job: .{1,500}|(?:~|Complete|Stopped|Ended with unfinished results).{0,500})$/.test(lines[0] || '')) lines.shift();
  const summary = /^(☑|☐|DONE|NOW|➜|NEXT|⮡)[\uFE0E\uFE0F]?[ \t]+(?=\S).{1,1000}$/u;
  const order = { '☑': 0, '☐': 0, DONE: 0, NOW: 1, '➜': 1, NEXT: 2, '⮡': 2 };
  let previous = -1;
  for (const line of lines) {
    const match = summary.exec(line);
    if (!match || order[match[1]] < previous) return false;
    previous = order[match[1]];
  }
  return true;
}

export function parseReportArtifact(raw) {
  if (typeof raw !== 'string' || raw.length > MAX_BYTES || new TextEncoder().encode(raw).length > MAX_BYTES) return null;
  try {
    const p = JSON.parse(raw);
    if (p?.schema !== 'boss.report/1' || typeof p.run !== 'string' || !RUN.test(p.run)
      || typeof p.created_at !== 'string' || !Number.isFinite(Date.parse(p.created_at))
      || !Number.isInteger(p.columns) || p.columns < 20 || p.columns > 240 || !meterOk(p.meter) || !reportTextOk(p.text)) return null;
    return p;
  } catch { return null; }
}

export function inlineFallback(meter, columns = 72) {
  return renderMeterText(meter, { columns, fenced: true });
}

// Compatibility export for callers that cannot create a report artifact. This
// never emits opaque comments, even when the native plugin is unavailable.
export function encodeInlineMessage(meter, { run, columns = 72 } = {}) {
  if (!RUN.test(run || '')) throw new Error('Invalid report run');
  return `${inlineFallback(meter, columns)}\n\nDetails: /boss-status ${run} --details`;
}

export function parseInlineMessage(message) {
  if (typeof message !== 'string' || message.length > 100000) return [];
  const refs = [];
  const pattern = /\[DETAILS\]\((file:\/\/[^\s()]+\/reports\/([a-f0-9]{64})\.md)\)/g;
  for (const match of message.matchAll(pattern)) {
    if (refs.length >= 8) break;
    try {
      const path = decodeURIComponent(match[1].slice('file://'.length));
      if (!path.startsWith('/') || /[\x00-\x1f?#\\]/.test(path)) continue;
      refs.push({ path, hash: match[2], start: match.index, end: match.index + match[0].length });
    } catch { /* A malformed link remains ordinary Markdown. */ }
  }
  return refs;
}

export async function readInlineReport(io, root, message) {
  const base = `${String(root).replace(/\/+$/, '')}/reports/`;
  for (const ref of parseInlineMessage(message)) {
    if (ref.path !== `${base}${ref.hash}.md`) continue;
    let p;
    try { p = parseReportArtifact(await io.read(`${base}${ref.hash}.json`)); } catch { continue; }
    if (!p) continue;
    const beforeLink = message.slice(0, ref.start);
    const trimmed = beforeLink.trimEnd();
    if (!trimmed.endsWith(p.text)) continue;
    const start = trimmed.length - p.text.length;
    if (start > 0 && message[start - 1] !== '\n') continue;
    return { ...p, prefix: message.slice(0, start), suffix: message.slice(ref.end), reportPath: ref.path };
  }
  return null;
}

// Migration cleanup removes only a validated old payload comment, including
// base64 line wrapping. The surrounding human report and prose stay untouched.
export function cleanLegacyMarkers(message) {
  if (typeof message !== 'string' || message.length > 100000) return message;
  return message.replace(/<!--\s*boss-meter:v3:([A-Za-z0-9_\-\s]{1,16000}?)\s*-->/g, (whole, encoded) => {
    try {
      const normalized = encoded.replace(/\s/g, '');
      const bytes = atob(normalized.replace(/-/g, '+').replace(/_/g, '/'));
      if (bytes.length > 8192) return whole;
      const p = JSON.parse(new TextDecoder().decode(Uint8Array.from(bytes, c => c.charCodeAt(0))));
      return p?.v === 3 && RUN.test(p.run) && Number.isInteger(p.columns) && p.columns >= 20 && p.columns <= 240 && meterOk(p.meter, true) ? '' : whole;
    } catch { return whole; }
  });
}
