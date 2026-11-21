// lease-store: the read side of orchestrator leases, one lease per run (per-run lease design D-1, D-2 read side).
// Plain node, no dependencies, no writes, never throws, silent on require: the hooks load it on every tool call.
// Layout (D-1): $ORCH_DIR/leases/<run>.json holds {v:1,run,path,session,acquired,renewed,expires,ttl_sec}. A file counts only
// when its name is `<lease.run>.json`, the run passes RUN_OK and the session passes SID_OK; anything else reads as no lease.
// Legacy (D-2): $ORCH_DIR/ACTIVE.lease (the old global slot) is read as the lease of its own run only, and only while
// leases/<that run>.json does not exist, whatever that file holds. It never covers another run. A malformed legacy file is none.
// "Does not exist" means ENOENT or ENOTDIR. Any other failure (EACCES, EISDIR, bad JSON, name != run) reads as no lease for
// that run and never falls through to the legacy file. Writers (manifest.js) migrate and lock; this module only reads.
'use strict';
const fs = require('fs'), path = require('path');

const SID_OK = s => typeof s === 'string' && /^[A-Za-z0-9._-]{1,128}$/.test(s) && !s.includes('..'); // same as manifest.js
const RUN_OK = s => typeof s === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(s) && !s.includes('..');
const LEGACY_NAME = 'ACTIVE.lease';
const dirOk = d => typeof d === 'string' && d.length > 0;

function leasesDir(orchDir) { try { return dirOk(orchDir) ? path.join(orchDir, 'leases') : null; } catch { return null; } }
function leaseFile(orchDir, run) { try { const d = leasesDir(orchDir); return d && RUN_OK(run) ? path.join(d, `${run}.json`) : null; } catch { return null; } }

// The lease object when the text is one, for the given run, else null.
function parseLease(txt, run) {
  let x; try { x = JSON.parse(txt); } catch { return null; }
  if (!x || typeof x !== 'object' || Array.isArray(x)) return null;
  if (!RUN_OK(x.run) || !SID_OK(x.session)) return null;
  return run === undefined || x.run === run ? x : null;
}
// 'absent' (ENOENT/ENOTDIR), or {txt}, or 'unreadable' (exists or cannot tell: EACCES, EISDIR, ...).
function readText(file) {
  try { return { txt: fs.readFileSync(file, 'utf8') }; } catch (e) { return e && (e.code === 'ENOENT' || e.code === 'ENOTDIR') ? 'absent' : 'unreadable'; }
}
function legacyLease(orchDir) { // the parsed legacy lease (any run), or null
  try { const r = readText(path.join(orchDir, LEGACY_NAME)); return typeof r === 'object' ? parseLease(r.txt) : null; } catch { return null; }
}

// readRunLease -> {lease, legacy:false, file} | {lease, legacy:true, file:<orchDir>/ACTIVE.lease} | null
function readRunLease(orchDir, run) {
  try {
    const file = leaseFile(orchDir, run);
    if (!file) return null;
    const r = readText(file);
    if (r !== 'absent') { // the per-run file exists (or cannot be told apart from existing): it alone decides
      const lease = typeof r === 'object' ? parseLease(r.txt, run) : null;
      return lease ? { lease, legacy: false, file } : null;
    }
    const lease = legacyLease(orchDir);
    return lease && lease.run === run ? { lease, legacy: true, file: path.join(orchDir, LEGACY_NAME) } : null;
  } catch { return null; }
}

// listLeases -> [{run, lease, legacy, file}], sorted by run: valid per-run files, plus the legacy lease when its run has no per-run file.
function listLeases(orchDir) {
  try {
    const d = leasesDir(orchDir);
    if (!d) return [];
    const out = [];
    let names = []; try { names = fs.readdirSync(d); } catch {}
    for (const name of names) {
      if (typeof name !== 'string' || !name.endsWith('.json')) continue; // tmp files and <run>.json.lock/ dirs end otherwise
      const run = name.slice(0, -5);
      if (!RUN_OK(run)) continue;
      const r = readRunLease(orchDir, run);
      if (r && !r.legacy) out.push({ run, lease: r.lease, legacy: false, file: r.file });
    }
    const L = legacyLease(orchDir);
    if (L && !out.some(x => x.run === L.run)) {
      const r = readRunLease(orchDir, L.run);
      if (r && r.legacy) out.push({ run: L.run, lease: r.lease, legacy: true, file: r.file });
    }
    return out.sort((a, b) => (a.run < b.run ? -1 : a.run > b.run ? 1 : 0));
  } catch { return []; }
}

// heldBy -> the leases whose session is sid (live or expired), newest `renewed` first (ties: by run).
function heldBy(orchDir, sid) {
  try {
    if (!SID_OK(sid)) return [];
    const t = x => { const v = Date.parse(x.lease.renewed); return Number.isFinite(v) ? v : -Infinity; };
    return listLeases(orchDir).filter(x => x.lease.session === sid)
      .sort((a, b) => (t(b) - t(a)) || (a.run < b.run ? -1 : a.run > b.run ? 1 : 0));
  } catch { return []; }
}

function isLive(lease, now = Date.now()) {
  try { if (!lease || typeof lease !== 'object') return false; const e = Date.parse(lease.expires); return Number.isFinite(e) && e > now; } catch { return false; }
}

module.exports = { leasesDir, leaseFile, readRunLease, listLeases, heldBy, isLive, RUN_OK, SID_OK };
