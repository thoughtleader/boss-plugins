import { test } from 'node:test';
import assert from 'node:assert/strict';
import { windowedBar } from '../meter.mjs';

test('extra space is recovered from old completions, not unfinished ticks', () => {
  const source = { done: 12, total: 13 };
  assert.deepEqual(windowedBar(source, 12), { text: '▰'.repeat(11) + '▱', hiddenDone: 1, hiddenUpcoming: 0 });
  assert.deepEqual(windowedBar({ done: 10, total: 12 }, 11), { text: '▰'.repeat(9) + '▱▱', hiddenDone: 1, hiddenUpcoming: 0 });
  assert.deepEqual(windowedBar({ done: 15, total: 24 }, 12), { text: '▰▰▰' + '▱'.repeat(9), hiddenDone: 12, hiddenUpcoming: 0 });
  assert.deepEqual(source, { done: 12, total: 13 });
});

test('larger capacity reveals actual older completions without inventing ticks', () => {
  const source = { done: 12, total: 13 };
  assert.equal(windowedBar(source, 13).text, '▰'.repeat(12) + '▱');
  assert.equal(windowedBar(source, 40).text, '▰'.repeat(12) + '▱');
  assert.equal(windowedBar({ done: 3, total: 3 }, 12).text, '▰▰▰');
  assert.equal(windowedBar({ done: 20, total: 20 }, 12).text, '▰'.repeat(12));
});

test('an oversized future queue explicitly indicates undisplayed upcoming tasks', () => {
  assert.deepEqual(windowedBar({ done: 10, total: 40 }, 12), { text: '▱'.repeat(11) + '…', hiddenDone: 10, hiddenUpcoming: 19 });
  assert.deepEqual(windowedBar({ done: 0, total: 2 }, 1), { text: '…', hiddenDone: 0, hiddenUpcoming: 2 });
  assert.equal(windowedBar({ done: 0, total: 12 }, 12).text, '▱'.repeat(12));
  assert.equal(windowedBar(null, 12).text, '');
});
