#!/usr/bin/env node
// stale-sync: SessionStart hook (P30; run example-run, row 18). It replaces the weekly cron sync, which missed
// whenever WSL was off. When the last transcript sync is older than the threshold, it starts `ledger.js sync` as a detached background
// process under a lock and returns at once. The threshold is the lib's staleDays() rule: env ORCH_SYNC_STALE_DAYS, default 2; 'off' or a
// non-number disables it. The hook never opens the db and loads no sqlite: the age is the mtime of <db>.txsync, which every transcript
// sync touches. A missing marker next to an existing db reads as stale; with no db at all there is nothing to catch up on (as the lib).
// Reads no stdin, prints nothing (SessionStart stdout would reach the model), and never blocks or fails the session.
// Lock <db>.sync.lock, created with O_EXCL and holding {pid, ts}. It is taken over only when it is older than 2 h, or when its pid is gone
// and it is older than 30 s (the hand-over from this hook to its background child). The child appends one record per run to <db>.sync.log.
'use strict';
const fs = require('fs'), path = require('path'), os = require('os'), cp = require('child_process');
const paths = require(path.join(__dirname, '..', 'lib', 'paths.js'));
const LOCK_MAX_MS = 2 * 3600e3, LOCK_GRACE_MS = 30e3, SYNC_TIMEOUT_MS = 30 * 60e3;

function thresholdDays(env = process.env) { // the lib's staleDays(), kept in step by a test
  const v = env.ORCH_SYNC_STALE_DAYS;
  if (v === undefined || v === '') return 2;
  const n = Number(v); return Number.isFinite(n) && n >= 0 ? n : Infinity;
}
function files(env = process.env) { const db = paths.metricsDb(env); return { db, marker: db + '.txsync', lock: db + '.sync.lock', log: db + '.sync.log' }; }
function mtime(p) { try { return fs.statSync(p).mtimeMs; } catch (_) { return null; } }
function isStale(f, now = Date.now(), env = process.env) {
  const lim = thresholdDays(env);
  if (lim === Infinity || mtime(f.db) == null) return false;
  const m = mtime(f.marker);
  return m == null || (now - m) / 864e5 > lim;
}
function alive(pid) { try { process.kill(pid, 0); return true; } catch (e) { return e && e.code === 'EPERM'; } }
function readLock(lock) { try { const x = JSON.parse(fs.readFileSync(lock, 'utf8')); return x && Number.isInteger(x.pid) ? x : null; } catch (_) { return null; } }
function writeLock(lock, flag) {
  const fd = fs.openSync(lock, flag);
  try { fs.writeSync(fd, JSON.stringify({ pid: process.pid, ts: new Date().toISOString() }) + '\n'); } finally { fs.closeSync(fd); }
}
function takeLock(lock, now = Date.now()) {
  for (let i = 0; i < 2; i++) {
    try { writeLock(lock, 'wx'); return true; } catch (e) { if (!e || e.code !== 'EEXIST') return false; }
    const held = readLock(lock), m = mtime(lock), age = m == null ? 0 : now - m;
    const free = age > LOCK_MAX_MS || (age > LOCK_GRACE_MS && !(held && alive(held.pid)));
    if (!free) return false;
    try { fs.unlinkSync(lock); } catch (_) {}
  }
  return false;
}
function releaseLock(lock) { const h = readLock(lock); if (!h || h.pid === process.pid) { try { fs.unlinkSync(lock); } catch (_) {} } }

function start() { // the hook: one stat in the common case
  const f = files();
  if (!isStale(f)) return;
  if (!takeLock(f.lock)) return;
  if (!isStale(f)) { releaseLock(f.lock); return; } // a sync finished between the first check and the lock (it touches the marker first)
  const child = cp.spawn(process.execPath, [__filename, '--run-sync'], { detached: true, stdio: 'ignore', env: process.env });
  child.on('error', () => releaseLock(f.lock));
  child.unref();
}
function runSync() { // the detached child: owns the lock for the whole sync
  const f = files(), t0 = Date.now();
  try { writeLock(f.lock, 'w'); } catch (_) {}
  try { os.setPriority(19); } catch (_) {}
  let r;
  try {
    r = cp.spawnSync(process.execPath, [path.join(__dirname, '..', 'lib', 'ledger.js'), 'sync'], { encoding: 'utf8', env: process.env, timeout: SYNC_TIMEOUT_MS, maxBuffer: 64 << 20, stdio: ['ignore', 'pipe', 'pipe'] });
  } catch (e) { r = { status: null, error: e, stdout: '', stderr: '' }; }
  try {
    const tail = s => String(s || '').trim().split('\n').slice(-12).join('\n');
    fs.appendFileSync(f.log, `${new Date().toISOString()} stale-sync pid ${process.pid}: exit ${r.status}${r.error ? ` (${r.error.code || r.error.message})` : ''}, ${Date.now() - t0} ms\n`
      + [tail(r.stdout), tail(r.stderr)].filter(Boolean).join('\n') + '\n');
  } catch (_) {}
  releaseLock(f.lock);
}

if (require.main === module) {
  try { if (process.argv[2] === '--run-sync') runSync(); else start(); } catch (_) {}
  process.exitCode = 0;
}
module.exports = { thresholdDays, files, isStale, takeLock, releaseLock };
