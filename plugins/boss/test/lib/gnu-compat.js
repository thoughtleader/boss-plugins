#!/usr/bin/env node
// GNU-flavoured date, touch, stat and md5sum for hosts that ship the BSD versions (macOS). Used only by test/lib/compat-bin, only
// when the real tool lacks the GNU flag the suites use; never by the shipped plugin. Supports exactly the forms the suites call:
//   date [-u] [-d SPEC | --date=SPEC] [+FORMAT]   SPEC: @EPOCH, an ISO timestamp, "now", "[+-]N unit", "N unit ago" (sec min hour day week)
//   touch -d SPEC FILE...                          sets atime and mtime (creates a missing file, like touch)
//   stat -c FORMAT FILE...                         %n %s %Y %.9Y %X %F %a (lstat, like GNU stat)
//   md5sum [FILE...]                               "<hash>  <name>", name "-" for stdin
'use strict';
const fs = require('node:fs');
const crypto = require('node:crypto');
const [tool, ...args] = process.argv.slice(2);
const UNIT = { s: 1, sec: 1, second: 1, m: 60, min: 60, minute: 60, h: 3600, hour: 3600, d: 86400, day: 86400, w: 604800, week: 604800 };

function parseSpec(spec) {
  spec = String(spec).trim();
  if (spec === 'now' || spec === '') return Date.now();
  let m = /^@(-?\d+(?:\.\d+)?)$/.exec(spec);
  if (m) return Number(m[1]) * 1000;
  m = /^([+-]?)\s*(\d+)\s*([a-z]+?)s?(\s+ago)?$/i.exec(spec);
  if (m && UNIT[m[3].toLowerCase()]) {
    let sign = m[1] === '-' ? -1 : 1;
    if (m[4]) sign = -sign;
    return Date.now() + sign * Number(m[2]) * UNIT[m[3].toLowerCase()] * 1000;
  }
  const t = Date.parse(/^\d{4}-\d\d-\d\d \d\d:\d\d/.test(spec) ? spec.replace(' ', 'T') : spec);
  if (!Number.isNaN(t)) return t;
  console.error(`${tool}: invalid date '${spec}'`); process.exit(1);
}

function strftime(fmt, ms, utc) {
  const d = new Date(ms);
  const g = (u, l) => (utc ? d[`getUTC${u}`]() : d[`get${l || u}`]());
  const p = (n, w = 2) => String(n).padStart(w, '0');
  const off = utc ? 0 : -d.getTimezoneOffset();
  const map = {
    Y: () => g('FullYear'), m: () => p(g('Month') + 1), d: () => p(g('Date')), H: () => p(g('Hours')), M: () => p(g('Minutes')),
    S: () => p(g('Seconds')), s: () => Math.floor(ms / 1000), N: () => p(Math.round(ms % 1000) * 1e6, 9), e: () => String(g('Date')).padStart(2, ' '),
    F: () => `${g('FullYear')}-${p(g('Month') + 1)}-${p(g('Date'))}`, T: () => `${p(g('Hours'))}:${p(g('Minutes'))}:${p(g('Seconds'))}`,
    z: () => `${off < 0 ? '-' : '+'}${p(Math.floor(Math.abs(off) / 60))}${p(Math.abs(off) % 60)}`, Z: () => (utc ? 'UTC' : 'LOCAL'), '%': () => '%',
  };
  return fmt.replace(/%(.)/g, (all, c) => (map[c] ? String(map[c]()) : all));
}

if (tool === 'date') {
  let utc = false, spec = null, fmt = null;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === '-u' || a === '--utc') utc = true;
    else if (a === '-d') spec = args[++i];
    else if (a.startsWith('--date=')) spec = a.slice(7);
    else if (a.startsWith('+')) fmt = a.slice(1);
  }
  const ms = spec === null ? Date.now() : parseSpec(spec);
  console.log(strftime(fmt === null ? '%Y-%m-%dT%H:%M:%S%z' : fmt, ms, utc));
} else if (tool === 'touch') {
  let spec = null; const files = [];
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '-d') spec = args[++i]; else if (args[i] === '--') continue; else files.push(args[i]);
  }
  const t = parseSpec(spec) / 1000;
  let rc = 0;
  for (const f of files) {
    try { if (!fs.existsSync(f)) fs.closeSync(fs.openSync(f, 'a')); fs.utimesSync(f, t, t); } catch (e) { console.error(`touch: ${f}: ${e.message}`); rc = 1; }
  }
  process.exit(rc);
} else if (tool === 'stat') {
  let fmt = null; const files = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === '-c' || a === '--format') fmt = args[++i]; else if (a.startsWith('-c')) fmt = a.slice(2); else if (a.startsWith('--format=')) fmt = a.slice(9); else files.push(a);
  }
  let rc = 0;
  for (const f of files) {
    let st; try { st = fs.lstatSync(f, { bigint: true }); } catch (e) { console.error(`stat: cannot stat '${f}': No such file or directory`); rc = 1; continue; }
    const kind = st.isSymbolicLink() ? 'symbolic link' : st.isDirectory() ? 'directory' : st.isFile() ? (st.size === 0n ? 'regular empty file' : 'regular file') : 'special file';
    const map = {
      n: f, s: String(st.size), Y: String(st.mtimeNs / 1000000000n), X: String(st.atimeNs / 1000000000n), F: kind, a: (st.mode & 0o7777n).toString(8),
      '%': '%',
    };
    const frac = String(st.mtimeNs % 1000000000n).padStart(9, '0');
    console.log(fmt.replace(/%(\.9Y|.)/g, (all, c) => (c === '.9Y' ? `${st.mtimeNs / 1000000000n}.${frac}` : (c in map ? map[c] : all))));
  }
  process.exit(rc);
} else if (tool === 'md5sum') {
  const hash = (buf) => crypto.createHash('md5').update(buf).digest('hex');
  let rc = 0;
  if (args.length === 0) console.log(`${hash(fs.readFileSync(0))}  -`);
  for (const f of args) {
    try { console.log(`${hash(fs.readFileSync(f))}  ${f}`); } catch (e) { console.error(`md5sum: ${f}: No such file or directory`); rc = 1; }
  }
  process.exit(rc);
} else { console.error(`gnu-compat: unknown tool ${tool}`); process.exit(2); }
