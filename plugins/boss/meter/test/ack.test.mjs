// boss.ack/1: the shared "done / snoozed / moved on" store and the one filter every Board consumer reads.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { emptyAck, readAck, writeAck, updateAck, ackNeed, snoozeNeed, abandonRun, restoreEntry, snoozeUntil, hiddenState, filterBoard, heldRunIds, abandonedFor, prune, stampOf, ackPath, SNOOZE_HOUR_MS } from '../ack.mjs';
import { buildBoard, loadSources } from '../board.mjs';

const NOW = Date.parse('2026-11-02T12:00:00.000Z');
const need = (o = {}) => ({ id: 'blocked:r1:row 3', kind: 'blocked', run_id: 'r1', title: 'row 3', detail: 'row blocked', since: '2026-11-02T10:00:00.000Z', action: { type: 'open_file', path: null }, ...o });
const run = (o = {}) => ({ id: 'r1', tool: 'orchestrator', title: 'Run one', paused: true, updated_at: '2026-11-01T08:00:00.000Z', state: 'waiting_input', ...o });
const bd = (o = {}) => ({ schema: 'boss.board/1', needs_you: [need()], runs: [run({ paused: false, state: 'running' })], councils: [], ...o });
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'boss-ack-'));

test('store: missing and corrupt files read as empty; write is atomic and round-trips', () => {
  const d = tmp(); const f = path.join(d, 'ack.json');
  assert.deepEqual(readAck(f), emptyAck());
  fs.writeFileSync(f, '{ not json'); assert.deepEqual(readAck(f), emptyAck());
  const a = ackNeed(emptyAck(), need(), NOW);
  assert.equal(writeAck(f, a), true);
  assert.deepEqual(readAck(f), a);
  assert.deepEqual(fs.readdirSync(d), ['ack.json'], 'no temp file left');
  assert.equal(fs.statSync(f).mode & 0o777, 0o600);
  assert.equal(ackPath('/x/boss'), '/x/boss/ack.json');
});

test('store: updateAck re-reads before it writes, so two windows do not overwrite each other; a corrupt file is kept as .bad', () => {
  const d = tmp(); const f = path.join(d, 'ack.json');
  updateAck(f, (a) => ackNeed(a, need({ id: 'a' }), NOW), NOW);
  updateAck(f, (a) => snoozeNeed(a, need({ id: 'b' }), NOW + SNOOZE_HOUR_MS, NOW), NOW);   // a second window's edit sees the first one's
  const r = readAck(f); assert.deepEqual([Object.keys(r.acks), Object.keys(r.snoozes)], [['a'], ['b']]);
  fs.writeFileSync(f, 'garbage');
  updateAck(f, (a) => ackNeed(a, need({ id: 'c' }), NOW), NOW);
  assert.equal(fs.readFileSync(f + '.bad', 'utf8'), 'garbage'); assert.deepEqual(Object.keys(readAck(f).acks), ['c']);
  assert.equal(updateAck(path.join(d, 'no', 'such', 'x'.repeat(5000)), (a) => a, NOW), null, 'an unwritable path reports failure and throws nothing');
});

test('done: hides this occurrence; a NEW since (or a different detail when there is no since) shows it again', () => {
  const a = ackNeed(emptyAck(), need(), NOW);
  assert.equal(hiddenState(a, need(), NOW).kind, 'done');
  assert.equal(hiddenState(a, need({ since: '2026-11-02T11:00:00.000Z' }), NOW), null, 'new event re-surfaces');
  const cap = need({ id: 'launch_cap::s', kind: 'launch_cap', run_id: null, since: null, detail: '5/5 launches' });
  const ac = ackNeed(emptyAck(), cap, NOW);
  assert.equal(stampOf(cap), '5/5 launches');
  assert.equal(hiddenState(ac, cap, NOW).kind, 'done'); assert.equal(hiddenState(ac, { ...cap, detail: '6/6 launches' }, NOW), null);
});

test('snooze: hidden until the end, then back; 1h and tomorrow 08:00 local', () => {
  assert.equal(snoozeUntil('1h', NOW), NOW + 3_600_000);
  const t = new Date(snoozeUntil('tomorrow', NOW)); assert.equal(t.getHours(), 8); assert.equal(t.getMinutes(), 0);
  assert.ok(t.getTime() > NOW && t.getTime() - NOW <= 32 * 3_600_000);
  assert.equal(snoozeUntil('bogus', NOW), null);
  const a = snoozeNeed(emptyAck(), need(), NOW + 3_600_000, NOW);
  const h = hiddenState(a, need(), NOW + 60_000); assert.equal(h.kind, 'snoozed'); assert.equal(h.until, new Date(NOW + 3_600_000).toISOString());
  assert.equal(hiddenState(a, need(), NOW + 3_600_001), null, 'expired snooze shows again');
  assert.equal(hiddenState(a, need({ since: 'newer' }), NOW + 60_000), null, 'a new event breaks through a snooze');
  assert.equal(Object.keys(ackNeed(a, need(), NOW).snoozes).length, 0, 'done replaces a snooze');
});

test('filterBoard: splits visible / hidden, keeps counts honest, and restore brings an item back', () => {
  const b = bd({ needs_you: [need(), need({ id: 'failed:r2:x', kind: 'failed', run_id: 'r2' }), need({ id: 'gate:r3:g', kind: 'gate', run_id: 'r3' })] });
  let a = ackNeed(emptyAck(), b.needs_you[0], NOW); a = snoozeNeed(a, b.needs_you[1], NOW + 1000 * 3600, NOW);
  const f = filterBoard(b, a, NOW);
  assert.deepEqual(f.needs_you.map((n) => n.id), ['gate:r3:g']);
  assert.deepEqual(f.hidden_needs.map((n) => [n.id, n.hidden.kind]), [['blocked:r1:row 3', 'done'], ['failed:r2:x', 'snoozed']]);
  assert.deepEqual(filterBoard(b, restoreEntry(a, 'blocked:r1:row 3'), NOW).needs_you.map((n) => n.id), ['blocked:r1:row 3', 'gate:r3:g']);
  assert.equal(filterBoard(b, null, NOW).needs_you.length, 3); assert.deepEqual(filterBoard(b, null, NOW).hidden_needs, []);
});

test('moved on: a paused run leaves Needs-you and Runs into abandoned; restorable; a resumed or changed run comes back', () => {
  const paused = run();
  const pn = need({ id: 'paused_run:r1:Run one', kind: 'paused_run', since: paused.updated_at });
  const b = { schema: 'boss.board/1', needs_you: [pn, need({ id: 'blocked:r1:other', run_id: 'r1' }), need({ id: 'failed:r9:z', kind: 'failed', run_id: 'r9' })], runs: [paused, run({ id: 'r9', paused: false })], councils: [] };
  const a = abandonRun(emptyAck(), paused, 'finished elsewhere', NOW);
  assert.equal(a.abandoned.r1.reason, 'finished elsewhere'); assert.equal(abandonRun(emptyAck(), paused, '<script>', NOW).abandoned.r1.reason, 'moved on', 'reasons are a fixed menu');
  const f = filterBoard(b, a, NOW);
  assert.deepEqual(f.runs.map((r) => r.id), ['r9']); assert.deepEqual(f.needs_you.map((n) => n.id), ['failed:r9:z']);
  assert.deepEqual(f.hidden_needs, [], 'the Abandoned group is its home, not Hidden');
  assert.deepEqual(f.abandoned.map((x) => [x.id, x.title, x.reason]), [['r1', 'Run one', 'finished elsewhere']]);
  assert.equal(filterBoard(b, restoreEntry(a, 'r1'), NOW).runs.length, 2, 'restore');
  assert.equal(abandonedFor(a, { ...paused, paused: false }), null, 'resumed run is not abandoned');
  assert.equal(abandonedFor(a, { ...paused, updated_at: '2026-11-02T01:00:00.000Z' }), null, 'a run that moved since comes back');
});

test('heldRunIds: abandoned runs and runs with a snoozed need hold notifications back; done-only and expired do not', () => {
  const r = run(); const n1 = need();
  const b = { needs_you: [n1], runs: [r], councils: [] };
  assert.deepEqual([...heldRunIds(filterBoard(b, abandonRun(emptyAck(), r, 'moved on', NOW), NOW))], ['r1']);
  assert.deepEqual([...heldRunIds(filterBoard(b, snoozeNeed(emptyAck(), n1, NOW + 1000, NOW), NOW))], ['r1']);
  assert.deepEqual([...heldRunIds(filterBoard(b, snoozeNeed(emptyAck(), n1, NOW + 1000, NOW), NOW + 2000))], [], 'snooze over: events flow again');
  assert.deepEqual([...heldRunIds(filterBoard(b, ackNeed(emptyAck(), n1, NOW), NOW))], []);
  assert.deepEqual([...heldRunIds(null)], []);
});

test('prune drops old done acks, long-expired snoozes and very old abandonments', () => {
  const old = NOW - 40 * 86_400_000;
  const a = { ...emptyAck(), acks: { x: { since: 's', at: new Date(old).toISOString() }, y: { since: 's', at: new Date(NOW).toISOString() } },
    snoozes: { z: { since: 's', until: new Date(NOW - 2 * 86_400_000).toISOString() } }, abandoned: { r: { since: '', at: new Date(NOW - 100 * 86_400_000).toISOString(), reason: 'moved on', title: 't', tool: 'x' } } };
  const p = prune(a, NOW); assert.deepEqual([Object.keys(p.acks), Object.keys(p.snoozes), Object.keys(p.abandoned)], [['y'], [], []]);
});

test('board: buildBoard applies src.ack; loadSources reads <boss>/ack.json', () => {
  const snap = (o) => ({ schema: 'boss.progress/1', tool: 'orchestrator', run_id: 'r1', title: 'Run one', state: 'waiting_input', paused: true, phase: 'paused', cwd: '/x', source_dir: '/runs/r1', events_path: '/runs/r1/m.jsonl',
    created_at: '2026-11-01T07:00:00.000Z', updated_at: '2026-11-01T08:00:00.000Z', last_progress_at: '2026-11-01T08:00:00.000Z', seq: 1, writer: { pid: 1, start_ticks: null, boot_id: 'b', kind: 'oneshot' }, owner: null, counts: [], items: [], attention: [], ...o });
  const probe = { bootId: () => 'b', startTicks: () => null, startEpoch: () => null };
  const src = { probe, snapshots: [{ dir: '/x', snap: snap(), mtimeMs: NOW }] };
  const plain = buildBoard(src, NOW);
  assert.deepEqual(plain.needs_you.map((n) => n.kind), ['paused_run']); assert.deepEqual([plain.hidden_needs, plain.abandoned], [[], []]);
  const n = plain.needs_you[0];
  const hid = buildBoard({ ...src, ack: ackNeed(emptyAck(), n, NOW) }, NOW);
  assert.equal(hid.needs_you.length, 0); assert.equal(hid.hidden_needs[0].hidden.kind, 'done'); assert.equal(hid.runs.length, 1);
  const ab = buildBoard({ ...src, ack: abandonRun(emptyAck(), plain.runs[0], 'moved on', NOW) }, NOW);
  assert.deepEqual([ab.needs_you.length, ab.runs.length, ab.abandoned.length], [0, 0, 1]);
  const again = buildBoard({ ...src, snapshots: [{ dir: '/x', snap: snap({ updated_at: '2026-11-02T09:00:00.000Z' }), mtimeMs: NOW }], ack: ackNeed(emptyAck(), n, NOW) }, NOW);
  assert.equal(again.needs_you.length, 1, 'a new since re-surfaces');
  // loader
  const d = tmp(); const paths = { boss: d, sessions: path.join(d, 's'), leases: path.join(d, 'l'), runs: path.join(d, 'r'), wfp: path.join(d, 'w'), inbox: path.join(d, 'i') };
  writeAck(path.join(d, 'ack.json'), ackNeed(emptyAck(), n, NOW));
  assert.deepEqual(Object.keys(loadSources(paths, NOW).ack.acks), [n.id]);
});
