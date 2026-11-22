import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const NO_PROC = fs.existsSync('/proc/self/stat') ? false : 'no /proc here (macOS): this reads /proc, which is Linux only (council README, Platform)';
import { buildBoard, loadSources, defaultPaths, plain, stripFor, glyph, SCHEMA } from '../board.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIX = (n) => JSON.parse(fs.readFileSync(path.join(HERE, 'fixtures', 'board', n), 'utf8'));
const NOW = Date.parse('2026-10-31T12:00:00.000Z');
const iso = (offS) => new Date(NOW + offS * 1000).toISOString();

// Probe: pids -> start ticks. Nothing here touches /proc.
const probe = (alive = {}, boot = 'boot-1') => ({ bootId: () => boot, startTicks: (pid) => alive[pid] ?? null, startEpoch: () => null });
const sess = (pid, o = {}) => ({ pid, sessionId: `sid-${pid}`, cwd: `/home/u/proj-${pid}`, name: `name-${pid}`, status: 'busy', statusUpdatedAt: NOW - 600000, procStart: String(pid * 10), startedAt: NOW - 3600000, ...o });
const live = (...pids) => Object.fromEntries(pids.map((p) => [p, p * 10]));
const item = (id, kind, state, o = {}) => ({ id, parent: null, kind, label: id, state, ...o });
const orch = (run, items, o = {}) => ({
  schema: 'boss.progress/1', tool: 'orchestrator', run_id: run, title: `goal of ${run}`, state: 'running', phase: 'wave 1', cwd: '/home/u/proj-1', source_dir: `/runs/${run}`, events_path: `/runs/${run}/manifest.jsonl`,
  created_at: iso(-7200), updated_at: iso(-300), last_progress_at: iso(-300), seq: 1, writer: { pid: 1, start_ticks: null, boot_id: 'boot-1', kind: 'oneshot' },
  owner: { session_id: 'sid-100', pid: null, start_ticks: null, inbox: null }, counts: [], items, attention: [], ...o,
});
const lease = (run, sid, o = {}) => ({ v: 1, run, session: sid, acquired: iso(-7000), renewed: iso(-600), expires: iso(6000), ttl_sec: 7200, ...o });
const board = (s, now = NOW) => buildBoard({ probe: probe(), ...s }, now);
const kinds = (b) => b.needs_you.map((n) => n.kind);

test('schema, empty sources: empty lists and unknown (null) today, never 0', () => {
  const b = board({});
  assert.equal(b.schema, SCHEMA);
  assert.deepEqual([b.needs_you, b.sessions, b.runs, b.councils], [[], [], [], []]);
  assert.equal(b.today.tokens, null); assert.equal(b.today.attempts, null); assert.equal(b.today.launches, null);
});

test('strip: the chat progress-view example renders literally', () => {
  const items = [
    item('wave:1', 'wave', 'done'), ...[1, 2, 3, 4].map((n) => item(`row:${n}`, 'row', 'done', { parent: 'wave:1' })),
    item('wave:2', 'wave', 'running'), item('row:5', 'row', 'running', { parent: 'wave:2' }), item('row:6', 'row', 'queued', { parent: 'wave:2' }), item('row:7', 'row', 'queued', { parent: 'wave:2' }),
    item('wave:3', 'wave', 'queued'), item('row:8', 'gate', 'queued', { parent: 'wave:3' }), item('row:9', 'row', 'queued', { parent: 'wave:3' }), item('row:10', 'row', 'queued', { parent: 'wave:3' }),
  ];
  const b = board({ snapshots: [{ snap: orch('r1', items) }] });
  assert.equal(b.runs[0].strip, '●1 ●2 ●3 ●4 ┃ ◐5 ○6 ○7 ┃ ◆8 ○9 ○10');
  assert.equal(b.runs[0].waves.length, 3);
  assert.deepEqual(b.runs[0].next_pending.map((r) => r.short), ['6', '7', '8']);
});

test('strip: failed/blocked ✕, retry ↻, gate meta ◆, rows without a wave share one group', () => {
  const items = [
    item('row:1', 'row', 'failed'), item('row:2', 'row', 'blocked'),
    item('row:3', 'row', 'running'), item('row:3/a1', 'attempt', 'failed', { parent: 'row:3', attempt: 1 }), item('row:3/a2', 'attempt', 'running', { parent: 'row:3', attempt: 2 }),
    item('row:4', 'row', 'waiting_input', { meta: { gate: true } }), item('row:5', 'row', 'skipped'),
  ];
  const b = board({ snapshots: [{ snap: orch('r1', items) }] });
  assert.equal(b.runs[0].strip, '✕1 ✕2 ↻3 ◆4 –5');
  assert.equal(stripFor([{ rows: [] }, { rows: [{ glyph: '●', short: '1' }] }]), '●1');
  assert.equal(glyph('queued'), '○');
});

test('idle session with an unfinished leased run and nothing running -> idle_run_unfinished', () => {
  const snap = orch('r1', [item('row:1', 'row', 'done'), item('row:2', 'row', 'queued')]);
  const b = board({ sessions: [sess(100, { status: 'idle' })], leases: [lease('r1', 'sid-100')], snapshots: [{ snap }], probe: probe(live(100)) });
  const n = b.needs_you.find((x) => x.kind === 'idle_run_unfinished');
  assert.ok(n); assert.equal(n.run_id, 'r1'); assert.match(n.title, /idle, run unfinished/); assert.equal(n.since, iso(-600));
  assert.equal(n.action.type, 'focus'); assert.equal(n.action.pid, 100);
  assert.equal(b.sessions[0].lease_runs[0].run, 'r1');
});

test('idle main chat while a subagent row runs is NOT an item', () => {
  const snap = orch('r1', [item('row:1', 'row', 'running'), item('row:1/a1', 'attempt', 'running', { parent: 'row:1', attempt: 1, model: 'sonnet', effort: 'high' })]);
  const b = board({ sessions: [sess(100, { status: 'idle' })], leases: [lease('r1', 'sid-100')], snapshots: [{ snap }], probe: probe(live(100)) });
  assert.ok(!kinds(b).includes('idle_run_unfinished'));
  assert.equal(b.sessions[0].current.row, '1'); assert.equal(b.sessions[0].current.model, 'sonnet'); assert.equal(b.sessions[0].current.effort, 'high');
});

test('idle main chat while an attempt runs under a still-queued row is NOT an item, and the run counts it as running', () => {
  const snap = orch('r1', [item('row:1', 'row', 'queued'), item('row:1/a1', 'attempt', 'running', { parent: 'row:1', attempt: 1, model: 'sonnet', effort: 'high' })]);
  const b = board({ sessions: [sess(100, { status: 'idle' })], leases: [lease('r1', 'sid-100')], snapshots: [{ snap }], probe: probe(live(100)) });
  assert.ok(!kinds(b).includes('idle_run_unfinished'));
  assert.equal(b.runs[0].running, 1);
  // a row and its own attempt both running is still one running thing
  const both = orch('r2', [item('row:1', 'row', 'running'), item('row:1/a1', 'attempt', 'running', { parent: 'row:1', attempt: 1 })]);
  assert.equal(board({ snapshots: [{ snap: both }] }).runs[0].running, 1);
});

test('a failed item with no path to open falls back to copying the resume command (or the title with no run)', () => {
  const snap = orch('r9', [item('row:1', 'row', 'failed', { error: 'boom' })], { source_dir: null, events_path: null });
  const b = board({ snapshots: [{ snap }] });
  const n = b.needs_you.find((x) => x.kind === 'failed');
  assert.ok(n); assert.deepEqual(n.action, { type: 'copy', text: '/orch-resume r9' });
  for (const x of b.needs_you) assert.ok(x.action.type !== 'open_file' || x.action.path, `no empty open_file action on ${x.id}`);
});

test('idle leaseholder with no unfinished run (done, paused) is NOT an item; busy session is not an item', () => {
  const done = orch('d1', [item('row:1', 'row', 'done')], { state: 'done', ended_at: iso(-100) });
  const paused = orch('p1', [item('row:1', 'row', 'queued')], { state: 'waiting_input', phase: 'paused (lease released)', paused: true, owner: { session_id: 'sid-101' } });
  const b = board({
    sessions: [sess(100, { status: 'idle' }), sess(101, { status: 'idle' }), sess(102, { status: 'busy' })],
    leases: [lease('d1', 'sid-100'), lease('p1', 'sid-101'), lease('r3', 'sid-102')],
    snapshots: [{ snap: done }, { snap: paused }, { snap: orch('r3', [item('row:1', 'row', 'queued')], { owner: { session_id: 'sid-102' } }) }], probe: probe(live(100, 101, 102)),
  });
  assert.ok(!kinds(b).includes('idle_run_unfinished'));
});

test('gate via attention[], blocked row, failed row, waiting_input row', () => {
  const items = [item('row:1', 'row', 'blocked', { error: 'needs a token' }), item('row:2', 'row', 'failed', { error: 'tests red', ended_at: iso(-50) }),
    item('row:3', 'gate', 'queued', { label: 'Review gate' }), item('row:4', 'row', 'waiting_input', { meta: { plan_status: 'revise' } })];
  const snap = orch('r1', items, { attention: [{ id: 'a1', kind: 'approval', item: 'row:3', text: 'gaps_found: 2 items', since: iso(-900) }] });
  const b = board({ snapshots: [{ snap }] });
  const by = (k) => b.needs_you.filter((n) => n.kind === k);
  assert.equal(by('gate').length, 1 + 0); // attention gate only; row 4 is waiting_input but not a gate row -> blocked
  assert.equal(by('gate')[0].title, 'Review gate'); assert.equal(by('gate')[0].detail, 'gaps_found: 2 items');
  assert.equal(by('blocked').length, 2); assert.match(by('blocked').map((n) => n.detail).join('|'), /needs a token/);
  assert.equal(by('failed').length, 1); assert.equal(by('failed')[0].detail, 'tests red');
  // oldest first
  const sinces = b.needs_you.map((n) => Date.parse(n.since));
  assert.deepEqual([...sinces], [...sinces].sort((a, c) => a - c));
});

test('paused run -> paused_run with copy /orch-resume; running rows in a paused run are not counted running', () => {
  const snap = orch('p1', [item('row:1', 'row', 'running')], { state: 'waiting_input', phase: 'paused (lease released)', paused: true });
  const b = board({ snapshots: [{ snap }] });
  const n = b.needs_you[0];
  assert.equal(n.kind, 'paused_run'); assert.deepEqual(n.action, { type: 'copy', text: '/orch-resume p1' });
  assert.equal(b.runs[0].running, 0); assert.equal(b.runs[0].paused, true);
  // old snapshots without the field: waiting_input + phase "paused ..."
  const old = orch('p2', [], { state: 'waiting_input', phase: 'paused (lease released)' });
  assert.equal(board({ snapshots: [{ snap: old }] }).needs_you[0].kind, 'paused_run');
});

test('orphaned: owner session gone / process writer died are "orphaned", never "stalled", and never say 0s', () => {
  const o = orch('r1', [item('row:1', 'row', 'running')], { owner: { session_id: 'sid-gone' } });
  const c = FIX('council.json'); c.writer = { ...c.writer, pid: 555, start_ticks: 5550 };
  const b = board({ snapshots: [{ snap: o }, { snap: c }], probe: probe({}), }, Date.parse(c.updated_at) + 5000);
  assert.equal(b.needs_you.filter((n) => n.kind === 'stalled').length, 0);
  const st = b.needs_you.filter((n) => n.kind === 'orphaned');
  assert.equal(st.length, 2);
  assert.match(st.find((n) => n.run_id === 'r1').detail, /session that owns it is gone/);
  assert.match(st.find((n) => n.run_id === c.run_id).detail, /writer.*(no longer running|before the last reboot)/);
  for (const n of st) { assert.match(n.detail, /^Orphaned: /); assert.doesNotMatch(n.detail, /\b0s\b|stalled|no progress for/); }
});

test('launch_cap per session from wfp state; Missing wfp state is not an item', () => {
  const b = board({ sessions: [sess(100), sess(101)], probe: probe(live(100, 101)), wfp: { 'sid-100': { launches: 22, policy: { maxLaunches: 22 } }, 'sid-101': { launches: 3, policy: { maxLaunches: 22 } } } });
  const caps = b.needs_you.filter((n) => n.kind === 'launch_cap');
  assert.equal(caps.length, 1); assert.equal(caps[0].session, 'sid-100'); assert.match(caps[0].detail, /22\/22/);
  assert.equal(b.today.sessions.find((s) => s.session_id === 'sid-101').max_launches, 22);
  const none = board({ sessions: [sess(100)], probe: probe(live(100)) });
  assert.equal(none.sessions[0].launches_total, null); assert.equal(none.sessions[0].max_launches, null);
});

test('lease_conflict: dead holder on a live run; two live claimants; stale expired lease is quiet', () => {
  const live1 = orch('r1', [item('row:1', 'row', 'queued')], { owner: { session_id: 'sid-100' } });
  const two = orch('r2', [item('row:1', 'row', 'queued')], { owner: { session_id: 'sid-101' } });
  const b = board({
    sessions: [sess(100), sess(101)], probe: probe(live(100, 101)),
    snapshots: [{ snap: live1 }, { snap: two }],
    leases: [lease('r1', 'sid-dead'), lease('r2', 'sid-100'),
      lease('old-1', 'sid-dead', { expires: iso(-86400 * 3), renewed: iso(-86400 * 3) }), // expired, no snapshot
      lease('old-2', 'sid-dead', { expires: iso(-100) }) /* expired, run has no snapshot */],
  });
  const lc = b.needs_you.filter((n) => n.kind === 'lease_conflict');
  assert.deepEqual(lc.map((n) => n.run_id).sort(), ['r1', 'r2']);
  assert.match(lc.find((n) => n.run_id === 'r1').title, /dead session/);
  assert.match(lc.find((n) => n.run_id === 'r2').title, /two sessions/);
  assert.equal(lc[0].action.type, 'copy');
});

test('unread_inbox: counts replies after the read mark; falls back to last 24h and says so', () => {
  const replies = [{ msg_id: 'm1', text: 'old \x1b[31mred\x1b[0m', at: iso(-86400 * 2) }, { msg_id: 'm2', text: 'recent one', at: iso(-3600) }, { msg_id: 'm3', text: 'latest', at: iso(-60) }];
  const base = { sessions: [sess(100)], probe: probe(live(100)), inbox: { 100: { replies, pending: 1 } } };
  const a = board(base);
  assert.equal(a.sessions[0].unread, 2); assert.equal(a.sessions[0].unread_basis, 'last24h'); assert.equal(a.sessions[0].inbox_pending, 1);
  const n = a.needs_you.find((x) => x.kind === 'unread_inbox');
  assert.ok(n); assert.equal(n.action.type, 'message'); assert.equal(n.since, iso(-60));
  const m = board({ ...base, readMarks: { 100: NOW - 30000 } });
  assert.equal(m.sessions[0].unread, 0); assert.equal(m.sessions[0].unread_basis, 'marks'); assert.ok(!kinds(m).includes('unread_inbox'));
  const none = board({ sessions: [sess(100)], probe: probe(live(100)) });
  assert.equal(none.sessions[0].unread, null); // no inbox at all: unknown, not 0
});

test('sessions: dead pid skipped, reused pid skipped, observers skipped; only allowed fields leave', () => {
  const b = board({
    sessions: [sess(100), sess(200), sess(300, { procStart: '999' }), sess(400, { cwd: '/home/u/.claude-mem/observer-sessions/12' }), { ...sess(500), messagingSocketPath: '/tmp/cc-socks/500.sock', key: 'secret' }],
    probe: probe({ 100: 1000, 300: 3000, 400: 4000, 500: 5000 }),
  });
  assert.deepEqual(b.sessions.map((s) => s.pid), [100, 500]);
  const s = b.sessions.find((x) => x.pid === 500);
  assert.ok(!JSON.stringify(b).includes('cc-socks') && !JSON.stringify(b).includes('secret'));
  assert.equal(s.project, 'proj-500'); assert.equal(s.status, 'busy'); assert.equal(s.since, iso(-600));
  // old-format file without status: null, not idle
  assert.equal(board({ sessions: [{ pid: 7, sessionId: 'x', cwd: '/a/b', procStart: '70' }], probe: probe({ 7: 70 }) }).sessions[0].status, null);
});

test('council: seat grid, stand-in, chair; failed seat without stand-in is a needs_you item', () => {
  const c = FIX('council.json');
  const t = Date.parse(c.updated_at) + 2000;
  const b = board({ snapshots: [{ snap: c }], probe: { bootId: () => c.writer.boot_id, startTicks: () => c.writer.start_ticks, startEpoch: () => null } }, t);
  assert.equal(b.runs.length, 0); assert.equal(b.councils.length, 1);
  const k = b.councils[0];
  assert.deepEqual(k.seats.map((s) => [s.label, s.state]), [['claude', 'done'], ['codex', 'running'], ['glm', 'done']]);
  assert.equal(k.seats[2].stand_in.model, 'stand/in-model'); assert.match(k.seats[2].stand_in.reason, /openrouter http 503/);
  assert.equal(k.seats[0].stand_in, null); assert.equal(k.chair.state, 'queued'); assert.equal(k.status, 'running');
  assert.equal(b.needs_you.length, 0);
  const f = FIX('council.json'); f.items.find((i) => i.id === 'seat:codex').state = 'failed'; f.items.find((i) => i.id === 'seat:codex').error = 'exit_1';
  const b2 = board({ snapshots: [{ snap: f }], probe: { bootId: () => f.writer.boot_id, startTicks: () => f.writer.start_ticks, startEpoch: () => null } }, t);
  const fl = b2.needs_you.find((n) => n.kind === 'failed'); assert.ok(fl); assert.match(fl.title, /seat codex failed/); assert.equal(fl.detail, 'exit_1');
});

test('council: carries the writer identity (pid, start ticks, boot id) so the host can prove the runner before it signals it; junk becomes null fields', () => {
  const c = FIX('council.json');
  const t = Date.parse(c.updated_at) + 2000;
  const pr = { bootId: () => c.writer.boot_id, startTicks: () => c.writer.start_ticks, startEpoch: () => null };
  const k = board({ snapshots: [{ snap: c }], probe: pr }, t).councils[0];
  assert.deepEqual(k.writer, { kind: 'process', pid: c.writer.pid, start_ticks: c.writer.start_ticks, boot_id: c.writer.boot_id });
  const j = FIX('council.json'); j.writer = { ...j.writer, pid: '12; rm', start_ticks: 1.5, boot_id: 7 };
  const w = board({ snapshots: [{ snap: j }], probe: pr }, t).councils[0].writer;
  assert.deepEqual([w.pid, w.start_ticks, w.boot_id], [null, null, null]);
});

test('terminal runs older than 24h are dropped; recent failed run is a failed item', () => {
  const old = orch('old', [], { state: 'done', ended_at: iso(-86400 * 3), updated_at: iso(-86400 * 3) });
  const fail = orch('bad', [], { state: 'failed', ended_at: iso(-600), summary: 'gave up' });
  const b = board({ snapshots: [{ snap: old }, { snap: fail }] });
  assert.deepEqual(b.runs.map((r) => r.id), ['bad']);
  assert.equal(b.needs_you[0].kind, 'failed'); assert.equal(b.needs_you[0].detail, 'gave up');
});

test('text from sources is stripped of ANSI/control characters and capped', () => {
  assert.equal(plain('a\x1b[31mb\x00c\n d', 50), 'ab c d');
  assert.equal(plain('x'.repeat(500), 20).length, 20);
  const snap = orch('r1', [item('row:1', 'row', 'blocked', { label: 'evil \x1b[2J\x07label ' + 'z'.repeat(400), error: '\x1b]0;title\x07boom' })], { title: 'T\x1b[1mitle' });
  const b = board({ snapshots: [{ snap }] });
  const j = JSON.stringify(b);
  assert.ok(!/[\x00-\x08\x0b-\x1f\x7f]/.test(j) && !j.includes('\\u001b'));
  assert.ok(b.runs[0].waves[0].rows[0].label.length <= 200);
  assert.equal(b.runs[0].title, 'Title');
});

test('ledger: tokens and first-pass per attempt; today per run and per session; unknown stays null', () => {
  const day = (h) => new Date(NOW).setHours(h, 0, 0, 0);
  const items = [item('row:1', 'row', 'done'), item('row:1/a1', 'attempt', 'done', { parent: 'row:1', attempt: 1, label: 'r1#1.1', model: 'sonnet', effort: 'high', artifacts: [{ label: 'commit', sha: 'abc1234' }, { label: 'result', path: '/runs/r1/1-x.md' }] }),
    item('row:2', 'row', 'done'), item('row:2/a1', 'attempt', 'done', { parent: 'row:2', attempt: 1, label: 'r1#2.1' })];
  const ledger = { r1: [{ ts: new Date(day(0) + 3600e3).toISOString(), id: 'r1#1.1', tokens: 1000, firstPass: true, ms: 60000, escalated: false, retries: 0 }, { ts: new Date(day(0) - 3600e3).toISOString(), id: 'r1#2.1', tokens: 500, firstPass: false }] };
  const launches = [{ run: 'r1', attempt: 'r1#1.1', session: 'sid-100', ts: new Date(day(0) + 3000e3).toISOString() }, { run: 'r1', attempt: 'r1#2.1', session: 'sid-100', ts: new Date(day(0) - 4000e3).toISOString() }];
  const b = board({ snapshots: [{ snap: orch('r1', items) }], ledger, launches, sessions: [sess(100), sess(101)], probe: probe(live(100, 101)) });
  const r = b.runs[0].waves[0].rows[0];
  assert.equal(r.tokens, 1000); assert.equal(r.sha, 'abc1234'); assert.equal(r.result, '/runs/r1/1-x.md'); assert.equal(r.first_pass, true); assert.equal(r.attempts[0].duration_s, 60);
  assert.equal(b.runs[0].waves[0].rows[1].tokens, 500); assert.equal(b.runs[0].waves[0].rows[1].first_pass, false);
  assert.equal(b.runs[0].totals.tokens, 1500);
  assert.equal(b.today.tokens, 1000); assert.equal(b.today.attempts, 1); assert.equal(b.today.launches, 1);
  assert.equal(b.today.runs[0].tokens, 1000); assert.equal(b.today.runs[0].launches, 1);
  const s100 = b.today.sessions.find((s) => s.session_id === 'sid-100'), s101 = b.today.sessions.find((s) => s.session_id === 'sid-101');
  assert.equal(s100.launches_today, 1); assert.equal(s100.tokens, 1000);
  assert.equal(s101.launches_today, null); assert.equal(s101.tokens, null);
  // an attempt without a ledger line has unknown tokens, not 0
  const none = board({ snapshots: [{ snap: orch('r9', [item('row:1', 'row', 'done'), item('row:1/a1', 'attempt', 'done', { parent: 'row:1' })]) }] });
  assert.equal(none.runs[0].waves[0].rows[0].tokens, null); assert.equal(none.runs[0].totals.tokens, null);
});

test('loader: reads a fixture tree read-only (dead pid skipped, .key untouched), builds a board', { skip: NO_PROC }, () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'board-'));
  const P = { boss: path.join(tmp, 'boss'), sessions: path.join(tmp, 'sessions'), leases: path.join(tmp, 'orch', 'leases'), runs: path.join(tmp, 'orch', 'runs'), wfp: path.join(tmp, 'wfp'), inbox: path.join(tmp, 'boss', 'inbox') };
  for (const d of [P.sessions, P.leases, P.wfp, path.join(P.runs, 'r1'), path.join(P.boss, 'runs', 'orchestrator', 'r1'), path.join(P.inbox, String(process.pid))]) fs.mkdirSync(d, { recursive: true });
  const ticks = Number(fs.readFileSync(`/proc/${process.pid}/stat`, 'utf8').split(') ')[1].split(' ')[19]);
  const sid = '11111111-2222-3333-4444-555555555555';
  fs.writeFileSync(path.join(P.sessions, `${process.pid}.json`), JSON.stringify({ pid: process.pid, sessionId: sid, cwd: '/home/u/myproj', name: 'myproj-aa', status: 'idle', statusUpdatedAt: NOW, procStart: String(ticks), messagingSocketPath: '/tmp/cc-socks/x.sock' }));
  fs.writeFileSync(path.join(P.sessions, `${process.pid}.deadbeef.key`), 'SECRET', { mode: 0 });
  fs.writeFileSync(path.join(P.sessions, '999999.json'), JSON.stringify({ pid: 999999, sessionId: 'dead', cwd: '/x', procStart: '1' }));
  fs.writeFileSync(path.join(P.leases, 'r1.json'), JSON.stringify(lease('r1', sid)));
  fs.writeFileSync(path.join(P.wfp, `${sid}.json`), JSON.stringify({ policy: { maxLaunches: 5 }, launches: 5, pending: [{ desc: 'big' }] }));
  fs.writeFileSync(path.join(P.boss, 'runs', 'orchestrator', 'r1', 'progress.json'), JSON.stringify(orch('r1', [item('row:1', 'row', 'queued')], { owner: { session_id: sid } })));
  fs.writeFileSync(path.join(P.inbox, String(process.pid), 'replies.jsonl'), JSON.stringify({ msg_id: 'm1', text: 'hi', at: new Date(Date.now() - 1000).toISOString() }) + '\n');
  fs.writeFileSync(path.join(P.runs, 'r1', 'ledger.jsonl'), JSON.stringify({ ts: new Date().toISOString(), id: 'r1#1.1', tokens: 42, firstPass: true }) + '\n');
  fs.writeFileSync(path.join(P.runs, 'r1', 'manifest.jsonl'), JSON.stringify({ type: 'launch', ts: new Date().toISOString(), attempt: 'r1#1.1', session: sid }) + '\n'
    + JSON.stringify({ type: 'launch', ts: new Date().toISOString(), attempt: 'r1#2.1', session: sid }) + '\n' + JSON.stringify({ type: 'void', ts: new Date().toISOString(), attempt: 'r1#2.1' }) + '\n'); // a voided (never launched) attempt is not a launch
  const src = loadSources(P, Date.now());
  assert.deepEqual(src.launches.map((l) => l.attempt), ['r1#1.1']);
  assert.equal(src.sessions.length, 1);
  assert.deepEqual(Object.keys(src.sessions[0]).sort(), ['cwd', 'name', 'pid', 'procStart', 'sessionId', 'startedAt', 'status', 'statusUpdatedAt']);
  const b = buildBoard(src, Date.now());
  assert.equal(b.sessions.length, 1); assert.equal(b.sessions[0].project, 'myproj'); assert.equal(b.sessions[0].lease_runs[0].run, 'r1');
  assert.deepEqual(kinds(b).sort(), ['idle_run_unfinished', 'launch_cap', 'unread_inbox']);
  assert.equal(b.today.tokens, 42); assert.equal(b.today.sessions[0].launches_today, 1);
  fs.rmSync(tmp, { recursive: true, force: true });
});

test('resolved gate leaves Needs you; an unresolved gate stays; a real blocked row stays ✕', () => {
  const gate = (state = 'blocked') => item('row:10d', 'gate', state, { label: 'Review gate', meta: { gate: true, plan_status: 'gaps_found 13/16' } });
  const att = { attention: [{ id: 'g', kind: 'approval', item: 'row:10d', text: 'Row 10d is gaps_found', since: iso(-900) }] };
  // nothing done after it: still listed, glyph is the gate diamond
  const open = board({ snapshots: [{ snap: orch('r1', [item('row:10c', 'row', 'done'), gate(), item('row:10e', 'row', 'queued')], att) }] });
  assert.equal(open.needs_you.filter((n) => n.kind === 'gate').length, 1);
  assert.equal(open.runs[0].strip, '●10c ◆10d ○10e');
  // a later row finished: resolved, gone from Needs you (attention and row paths), glyph unchanged
  const items = [item('row:10c', 'row', 'done'), gate(), item('row:10e', 'row', 'done'), item('row:11', 'row', 'running')];
  const done = board({ snapshots: [{ snap: orch('r1', items, att) }] });
  assert.equal(done.needs_you.filter((n) => n.kind === 'gate' || n.kind === 'blocked').length, 0);
  assert.equal(done.runs[0].strip, '●10c ◆10d ●10e ◐11');
  const noAtt = board({ snapshots: [{ snap: orch('r1', items) }] });
  assert.equal(noAtt.needs_you.filter((n) => n.kind === 'gate' || n.kind === 'blocked').length, 0);
  // a done row BEFORE the gate does not resolve it
  const before = board({ snapshots: [{ snap: orch('r1', [item('row:10c', 'row', 'done'), gate()], att) }] });
  assert.equal(before.needs_you.filter((n) => n.kind === 'gate').length, 1);
  // real blocked rows keep ✕ and stay listed even with later rows done
  const blocked = board({ snapshots: [{ snap: orch('r1', [item('row:1', 'row', 'blocked'), item('row:2', 'row', 'done')]) }] });
  assert.equal(blocked.runs[0].strip, '✕1 ●2'); assert.equal(blocked.needs_you.filter((n) => n.kind === 'blocked').length, 1);
  assert.equal(glyph('blocked', { gate: true }), '◆'); assert.equal(glyph('failed', { gate: true }), '✕'); assert.equal(glyph('blocked'), '✕');
});

test('one session listed twice (resumed, old process alive) gives one launch-cap item', () => {
  const dup = (pid) => sess(pid, { sessionId: 'sid-same', status: 'busy' });
  const b = board({
    probe: probe({ ...live(10, 11) }), sessions: [dup(10), dup(11)],
    wfp: { 'sid-same': { launches: 22, policy: { maxLaunches: 22 } } },
  });
  assert.equal(b.needs_you.filter((n) => n.kind === 'launch_cap').length, 1);
});

test('rich rows: human names, context fields, wall/active inputs, no ticking numbers', () => {
  const att = (n, state, o = {}) => item(`row:3/a${n}`, 'attempt', state, { parent: 'row:3', attempt: n, label: 'Design the thing', model: 'opus', effort: 'high', meta: { attempt_id: `r1#3.${n}`, agent: 'orch-builder', launched_at: iso(-120), ...(o.meta || {}) }, ...o, });
  const items = [
    item('row:3', 'row', 'running', { label: 'Design the thing' }),
    att(1, 'done', { started_at: iso(-3000), ended_at: iso(-1800), meta: { attempt_id: 'r1#3.1', launched_at: iso(-3100) } }),
    att(2, 'running', { started_at: iso(-90) }),
  ];
  const ledger = { r1: [{ id: 'r1#3.1', tokens: 120000, tools: 31, ms: 1200000, firstPass: true }] };
  const b = board({ snapshots: [{ snap: orch('r1', items) }], ledger });
  const run = b.runs[0], row = run.waves[0].rows[0];
  assert.equal(row.name, 'Design the thing');
  assert.equal(row.attempts[0].id, 'r1#3.1', 'attempt id comes from meta.attempt_id, the label is the name');
  assert.equal(row.attempts[0].name, 'Design the thing');
  assert.equal(row.tokens, 120000, 'ledger tokens found by the attempt id even though the label is a name');
  assert.equal(row.tools, 31); assert.equal(row.duration_s, 1200); assert.equal(row.live_since, iso(-90));
  assert.equal(row.added_at, iso(-3100), 'earliest launch');
  assert.equal(run.time.active_done_s, 1200); assert.deepEqual(run.time.live.map((l) => [l.id, l.since]), [['row:3', iso(-90)]]);
  assert.equal(run.totals.attempts, 2);
  // the board has no wall/active/elapsed number that moves between two reads
  const again = board({ snapshots: [{ snap: orch('r1', items) }], ledger }, NOW + 60000);
  const strip = (x) => JSON.stringify(x, (k, v) => (k === 'generated_at' || k === 'no_progress_s' ? undefined : v));
  assert.equal(strip(again.runs[0]), strip(b.runs[0]));
});

test('rich rows: a row with no wave sits in the wave of the nearest earlier row, in plan order', () => {
  const items = [
    item('wave:1', 'wave', 'done'), item('row:3a', 'row', 'done', { parent: 'wave:1' }), item('row:3b', 'row', 'done', { parent: 'wave:1' }),
    item('wave:2', 'wave', 'running'), item('row:4', 'row', 'queued', { parent: 'wave:2' }),
    item('row:3c', 'row', 'running'),
  ];
  const b = board({ snapshots: [{ snap: orch('r1', items) }] });
  assert.deepEqual(b.runs[0].waves.map((w) => w.rows.map((r) => r.short)), [['3a', '3b', '3c'], ['4']]);
});

test('rich rows: council topic, round, agent count', () => {
  const snap = { ...FIX('council.json'), run_id: '20261103T051602Z-round-3-review-of-an-interaction', title: 'Round 3 review of an interaction design for a tool' };
  const b = board({ snapshots: [{ snap }] });
  const c = b.councils[0];
  assert.equal(c.round, 3); assert.equal(c.topic, 'review of an interaction design for a tool'); assert.equal(c.agents, c.seats.length + (c.chair ? 1 : 0));
});

test('controls: attention options reach the need (capped, plain); owner carries pid and idle/busy status', () => {
  const items = [item('row:3', 'gate', 'queued', { label: 'Review gate' })];
  const opts = ['Approve', 'Revise\u001b[31m', ...Array.from({ length: 12 }, (_, i) => `choice ${i}`), 42];
  const snap = orch('r1', items, { attention: [{ id: 'a1', kind: 'approval', item: 'row:3', text: 'pick one', since: iso(-900), options: opts }] });
  const b = board({ snapshots: [{ snap }], sessions: [sess(100, { status: 'idle' })], probe: probe(live(100)) });
  const g = b.needs_you.find((n) => n.kind === 'gate');
  assert.equal(g.options.length, 8);
  assert.deepEqual(g.options.slice(0, 2), ['Approve', 'Revise']);
  assert.ok(g.options.every((o) => typeof o === 'string' && !/\u001b/.test(o)));
  assert.deepEqual(b.runs[0].owner, { session_id: 'sid-100', name: 'name-100', alive: true, pid: 100, status: 'idle' });
  const gone = board({ snapshots: [{ snap }], probe: probe() });
  assert.deepEqual(gone.runs[0].owner, { session_id: 'sid-100', name: null, alive: false, pid: null, status: null });
  const none = board({ snapshots: [{ snap: orch('r2', items, { attention: [{ id: 'a', kind: 'approval', item: 'row:3', text: 'x' }] }) }] });
  assert.equal(none.needs_you.find((n) => n.kind === 'gate').options, undefined);
});

// ---------- owner Stop (control/cancel) ----------
const CANCEL = { requested_at: iso(-120), by: 'boss-vscode', reason: 'stopped by owner', nonce: 'ab12cd34' };
const stopBoard = (snapOpts, cancel = CANCEL, extra = {}) => board({ snapshots: [{ snap: orch('rs', snapOpts.items, snapOpts.o), cancel }], sessions: [sess(100)], probe: probe(live(100)), ...extra });   // owner session alive: a run is dead only when the test says so

test('stop: no marker -> run.stop is null; terminal runs and councils ignore a marker', () => {
  const running = [item('row:1', 'row', 'running')];
  assert.equal(stopBoard({ items: running }, null).runs[0].stop, null);
  assert.equal(stopBoard({ items: [item('row:1', 'row', 'done')], o: { state: 'done', ended_at: iso(-60) } }).runs[0].stop, null);
  const council = { ...FIX('council.json'), updated_at: iso(-30), last_progress_at: iso(-30), heartbeat_at: iso(-30) };
  const c = board({ snapshots: [{ snap: council, cancel: CANCEL }] });
  assert.equal(c.councils[0].stop, null);
});

test('stop: marker + a running row/attempt = stopping; marker + nothing running = stopped; carries since/by/reason/nonce', () => {
  const stopping = stopBoard({ items: [item('row:1', 'row', 'running'), item('row:2', 'row', 'queued')] }).runs[0];
  assert.deepEqual(stopping.stop, { state: 'stopping', since: CANCEL.requested_at, by: 'boss-vscode', reason: 'stopped by owner', nonce: 'ab12cd34' });
  const attemptOnly = stopBoard({ items: [item('row:1', 'row', 'queued'), item('row:1/a1', 'attempt', 'running', { parent: 'row:1' })] }).runs[0];
  assert.equal(attemptOnly.stop.state, 'stopping');
  const stopped = stopBoard({ items: [item('row:1', 'row', 'done'), item('row:2', 'row', 'queued')] }).runs[0];
  assert.equal(stopped.stop.state, 'stopped');
  assert.equal(stopped.state, 'running'); // the snapshot state is untouched: the marker is only an overlay
});

test('stop: a paused run, or one whose writer is gone, has nothing left to finish -> stopped', () => {
  const paused = stopBoard({ items: [item('row:1', 'row', 'running')], o: { state: 'waiting_input', paused: true, phase: 'paused (lease released)' } }).runs[0];
  assert.equal(paused.stop.state, 'stopped');
  const dead = stopBoard({ items: [item('row:1', 'row', 'running')], o: { owner: { session_id: 'sid-gone', pid: null, start_ticks: null, inbox: null } } }).runs[0];
  assert.equal(dead.status, 'dead'); assert.equal(dead.stop.state, 'stopped');
});

test('stop: a marker with no readable fields still stops (since falls back to the marker file time handed in)', () => {
  const r = stopBoard({ items: [item('row:1', 'row', 'queued')] }, { requested_at: iso(-5), by: null, reason: null, nonce: null }).runs[0];
  assert.deepEqual([r.stop.state, r.stop.since, r.stop.by, r.stop.nonce], ['stopped', iso(-5), null, null]);
});

test('stop: a Stopped run raises no needs (paused, stalled, orphaned, idle, gate, blocked); a Stopping run keeps its gate', () => {
  const gate = { attention: [{ id: 'a1', kind: 'approval', item: 'row:2', text: 'pick', since: iso(-900) }] };
  const items = [item('row:1', 'row', 'done'), item('row:2', 'gate', 'queued'), item('row:3', 'row', 'blocked')];
  assert.ok(kinds(stopBoard({ items: [...items, item('row:4', 'row', 'running')], o: gate })).includes('gate'));   // stopping: the gate still needs the owner
  const stoppedGate = stopBoard({ items, o: gate });
  assert.equal(stoppedGate.runs[0].stop.state, 'stopped'); assert.deepEqual(kinds(stoppedGate), []);
  const idle = stopBoard({ items: [item('row:1', 'row', 'done'), item('row:2', 'row', 'queued')] }, CANCEL, { sessions: [sess(100, { status: 'idle' })], leases: [lease('rs', 'sid-100')], probe: probe(live(100)) });
  assert.deepEqual(kinds(idle), []);   // without the marker this is idle_run_unfinished
  assert.deepEqual(kinds(board({ snapshots: [{ snap: orch('rs', [item('row:1', 'row', 'done'), item('row:2', 'row', 'queued')]) }], sessions: [sess(100, { status: 'idle' })], leases: [lease('rs', 'sid-100')], probe: probe(live(100)) })), ['idle_run_unfinished']);
  const pausedStopped = stopBoard({ items: [item('row:1', 'row', 'queued')], o: { state: 'waiting_input', paused: true, phase: 'paused (lease released)' } });
  assert.deepEqual(kinds(pausedStopped), []);
  const other = board({ snapshots: [{ snap: orch('rs', [item('row:1', 'row', 'done')]), cancel: CANCEL }, { snap: orch('ro', [item('row:1', 'row', 'blocked')]) }], sessions: [sess(100)], probe: probe(live(100)) });
  assert.deepEqual(other.needs_you.map((n) => n.run_id), ['ro']);   // another run's items stay
});

test('stop loader: control/cancel next to progress.json is read (malformed and odd files still count), nothing is written', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'boss-stop-'));
  try {
    const mk = (run, cancel) => {
      const d = path.join(tmp, 'boss', 'runs', 'orchestrator', run); fs.mkdirSync(d, { recursive: true });
      fs.writeFileSync(path.join(d, 'progress.json'), JSON.stringify(orch(run, [item('row:1', 'row', 'queued')], { updated_at: new Date().toISOString() })));
      if (cancel !== undefined) { fs.mkdirSync(path.join(d, 'control')); if (cancel !== null) fs.writeFileSync(path.join(d, 'control', 'cancel'), cancel); else fs.symlinkSync('/etc/passwd', path.join(d, 'control', 'cancel')); }
    };
    mk('l-none'); mk('l-good', JSON.stringify({ schema: 'boss.cancel/1', requested_at: '2026-11-03T10:00:00.000Z', by: 'boss-vscode', reason: 'stopped by owner', nonce: 'n1' }));
    mk('l-bad', 'not json {{'); mk('l-link', null);
    const before = fs.readdirSync(path.join(tmp, 'boss', 'runs', 'orchestrator', 'l-good', 'control'));
    const src = loadSources({ boss: path.join(tmp, 'boss'), sessions: path.join(tmp, 'none'), leases: path.join(tmp, 'none'), runs: path.join(tmp, 'none'), wfp: path.join(tmp, 'none'), inbox: path.join(tmp, 'none') });
    const by = Object.fromEntries(src.snapshots.map((e) => [e.snap.run_id, e.cancel]));
    assert.equal(by['l-none'], null);
    assert.deepEqual([by['l-good'].requested_at, by['l-good'].by, by['l-good'].nonce], ['2026-11-03T10:00:00.000Z', 'boss-vscode', 'n1']);
    assert.ok(by['l-bad'] && Number.isFinite(Date.parse(by['l-bad'].requested_at)) && by['l-bad'].nonce === null);
    assert.ok(by['l-link'] && by['l-link'].by === null);   // a symlink is not followed, but it is a stop
    assert.deepEqual(fs.readdirSync(path.join(tmp, 'boss', 'runs', 'orchestrator', 'l-good', 'control')), before);
  } finally { fs.rmSync(tmp, { recursive: true, force: true }); }
});

// ---------- rows of runs that launch nothing (0.4.1): never running / queued ----------
const rowStates = (r) => r.waves.flatMap((w) => w.rows).map((x) => `${x.short}:${x.state}${x.glyph}`).join(' ');
const mixed = () => [item('row:1', 'row', 'done'), item('row:2', 'row', 'running'), item('row:2/a1', 'attempt', 'running', { parent: 'row:2', started_at: iso(-600) }), item('row:3', 'row', 'queued')];

test('rows: an active run is untouched; a Stopped run reads interrupted / not run, with no live clocks and no Next up', () => {
  const live1 = board({ snapshots: [{ snap: orch('rs', mixed()) }], sessions: [sess(100)], probe: probe(live(100)) }).runs[0];
  assert.equal(rowStates(live1), '1:done● 2:running◐ 3:queued○'); assert.equal(live1.next_pending.length, 1);
  const stopped = board({ snapshots: [{ snap: orch('rs', mixed(), { state: 'waiting_input', paused: true, phase: 'paused (lease released)' }), cancel: CANCEL }], sessions: [sess(100)], probe: probe(live(100)) }).runs[0];
  assert.equal(stopped.stop.state, 'stopped');
  assert.equal(rowStates(stopped), '1:done● 2:interrupted⊘ 3:not_run–');
  assert.deepEqual(stopped.waves[0].rows[1].attempts.map((a) => a.state), ['interrupted']); assert.equal(stopped.waves[0].rows[1].live_since, null);
  assert.deepEqual(stopped.next_pending, []);
});

test('rows: a Stopping run keeps its running row running and holds the queued rows', () => {
  const r = board({ snapshots: [{ snap: orch('rs', mixed()), cancel: CANCEL }], sessions: [sess(100)], probe: probe(live(100)) }).runs[0];
  assert.equal(r.stop.state, 'stopping'); assert.equal(rowStates(r), '1:done● 2:running◐ 3:held○'); assert.deepEqual(r.next_pending, []);
});

test('rows: a finished run never shows running or queued rows; a paused run shows its stale running rows as interrupted but keeps queued ones', () => {
  const done = board({ snapshots: [{ snap: orch('rs', mixed(), { state: 'failed', ended_at: iso(-60) }) }] }).runs[0];
  assert.equal(rowStates(done), '1:done● 2:interrupted⊘ 3:not_run–');
  const paused = board({ snapshots: [{ snap: orch('rs', mixed(), { state: 'waiting_input', paused: true, phase: 'paused (lease released)' }) }] }).runs[0];
  assert.equal(rowStates(paused), '1:done● 2:interrupted⊘ 3:queued○');
});

// review gap 1 (seed 9): a turn-limit stop is a partial result. The real fold (lib/progress-mirror.js) gives its attempt and row `waiting_input`, so the
// board shows the unfinished mark, no failed strip mark and no `failed` needs-you item; a real finish that is not older still wins over it.
test('rows: a turn-limit-only attempt reads unfinished (waiting_input, ◆, a needs-you item), not failed; a real finish stays done', () => {
  const PM = createRequire(import.meta.url)('../../lib/progress-mirror.js');
  const E = (seq, type, f) => ({ id: `r#e${seq}`, seq, type, ts: `2026-10-31T10:00:${String(seq).padStart(2, '0')}.000Z`, run: 'tl', attempt: null, row: null, key: `k${seq}`, session: 'S1', ...f });
  const parseAttemptId = (id) => { const m = /^(.+)#(\d+[a-z]?)\.(\d+)$/.exec(id); return m ? { id, run: m[1], row: m[2], n: +m[3] } : null; };
  const fold = (events) => PM.buildSnapshot('tl', events, { runDir: '/x/runs/tl', plan: null, cfg: {}, parseAttemptId, ownerFor: () => null, now: NOW, writer: { pid: 1, start_ticks: null, boot_id: null, kind: 'oneshot' } });
  const launch = E(1, 'launch', { attempt: 'tl#1.1', row: '1', n: 1, model: 'sonnet', effort: 'high', agent: 'orch-builder' });
  const start = E(2, 'start', { attempt: 'tl#1.1', row: '1', tool_use_id: 't1', agent: 'orch-builder' });
  const tl = (seq, ts) => E(seq, 'stop', { attempt: 'tl#1.1', row: '1', task_id: 'k', phase: 'stopped-turn-limit', status: 'turn-limit', final: true, ts });
  const fin = (seq, ts) => E(seq, 'stop', { attempt: 'tl#1.1', row: '1', task_id: 'k', phase: 'finished', status: 'completed', final: true, ts });
  const only = fold([launch, start, tl(3, '2026-10-31T10:00:03.000Z')]);
  assert.deepEqual(only.items.filter((i) => i.id.startsWith('row:1')).map((i) => `${i.id}:${i.state}`), ['row:1:waiting_input', 'row:1/a1:waiting_input']);
  const b = board({ snapshots: [{ snap: only }] }).runs[0];
  assert.equal(rowStates(b), '1:waiting_input◆'); assert.ok(!b.strip.includes('✕')); assert.equal(b.live_since ?? null, null);
  const need = board({ snapshots: [{ snap: only }] }).needs_you;
  assert.ok(!need.some((n) => n.kind === 'failed'), 'no failed needs-you item for a turn-limit stop');
  assert.ok(need.some((n) => n.kind === 'blocked' && /turn limit/.test(n.detail)), 'a visible needs-you item says it stopped at its turn limit');
  assert.equal(only.items.find((i) => i.id === 'row:1/a1').meta.stop_status, 'turn-limit');
  // the finished notice was ingested first (lower seq) but is the later stop by ts: the late turn-limit notice must not override it
  const late = fold([launch, start, fin(3, '2026-10-31T10:00:09.000Z'), tl(4, '2026-10-31T10:00:04.000Z')]);
  assert.deepEqual(late.items.filter((i) => i.id.startsWith('row:1')).map((i) => `${i.id}:${i.state}`), ['row:1:done', 'row:1/a1:done']);
  assert.equal(rowStates(board({ snapshots: [{ snap: late }] }).runs[0]), '1:done●');
});

// a turn-limit stop carries a detail (the fold sets it only then); with a real PLAN the row also has plan_status, which must not hide it
test('needs-you: a turn-limit row with a plan status shows the turn-limit detail; a plain waiting row still shows its plan status', () => {
  const tlDetail = 'stopped at its turn limit (partial result): continue it with launch --continue, or accept the partial';
  const items = [item('row:1', 'row', 'waiting_input', { detail: tlDetail, meta: { plan_status: 'running' } }), item('row:2', 'row', 'waiting_input', { meta: { plan_status: 'revise' } })];
  const need = board({ snapshots: [{ snap: orch('tlp', items) }] }).needs_you;
  assert.equal(need.find((n) => n.title === 'row:1').detail, tlDetail);
  assert.equal(need.find((n) => n.title === 'row:2').detail, 'revise');
});

test('glyph: interrupted and not run have their own marks', () => { assert.equal(glyph('interrupted'), '⊘'); assert.equal(glyph('not_run'), '–'); });

test('defaultPaths: resolved by lib/paths.js like the hooks (ORCH_DIR, BOSS_DATA, <config>/boss-data), not hard-coded to ~/.claude/orchestrator', () => {
  const cfg = path.join(os.tmpdir(), 'boss-cfg-x'), data = path.join(os.tmpdir(), 'boss-data-x');
  // a stranger's install: no legacy orchestrator dir, so the data dir is <config>/boss-data
  const a = defaultPaths({ HOME: '/nonexistent-home', CLAUDE_CONFIG_DIR: cfg });
  assert.equal(a.runs, path.join(cfg, 'boss-data', 'runs'));
  assert.equal(a.leases, path.join(cfg, 'boss-data', 'leases'));
  assert.equal(a.wfp, path.join(cfg, 'boss-data', 'wfp', 'state'));
  assert.equal(a.sessions, path.join(cfg, 'sessions'));
  assert.equal(a.boss, path.join(cfg, 'boss'));
  assert.equal(a.inbox, path.join(cfg, 'boss', 'inbox'));
  // BOSS_DATA relocates runs, leases and wfp state together
  const b = defaultPaths({ HOME: '/nonexistent-home', BOSS_DATA: data });
  assert.deepEqual([b.runs, b.leases, b.wfp], [path.join(data, 'runs'), path.join(data, 'leases'), path.join(data, 'wfp', 'state')]);
  // the hooks' own overrides still win, and the old BOSS_* names stay per-source overrides
  const c = defaultPaths({ HOME: '/nonexistent-home', BOSS_DATA: data, ORCH_RUNS_DIR: '/r', WFP_STATE_DIR: '/w' });
  assert.deepEqual([c.runs, c.wfp], ['/r', '/w']);
  const d = defaultPaths({ HOME: '/nonexistent-home', BOSS_DATA: data, BOSS_WFP_STATE: '/ws', BOSS_SESSIONS_DIR: '/ss', BOSS_INBOX_DIR: '/ib', BOSS_PROGRESS_DIR: '/pg' });
  assert.deepEqual([d.wfp, d.sessions, d.inbox, d.boss], ['/ws', '/ss', '/ib', '/pg']);
});

test('no /proc (macOS, Git Bash): a session shows when its pid answers and its start time matches; a stale, reused or unreadable-start pid does not', () => {
  const np = (alive, epoch = {}) => ({ noProc: true, bootId: () => null, startTicks: () => null, startEpoch: (pid) => epoch[pid] ?? null, alive: (pid) => alive.includes(pid) });
  // procStart (a /proc tick count) cannot be compared here; startedAt against the ps start time decides
  const b = board({
    sessions: [sess(100), sess(101), sess(102), sess(103)],
    probe: np([100, 101, 103], { 100: (NOW - 3600000) / 1000, 101: (NOW - 1000) / 1000 }),
  });
  assert.deepEqual(b.sessions.map((s) => s.pid).sort(), [100]); // 101 reused (started an hour late), 102 dead, 103 start time unreadable: hidden, not shown on a bare signal 0
  const bare = board({ sessions: [sess(103, { startedAt: undefined })], probe: np([103]) }); // no startedAt to compare: pid answering is enough
  assert.deepEqual(bare.sessions.map((s) => s.pid), [103]);
});
