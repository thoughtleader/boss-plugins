'use strict';
// agent-tools-patch is opt-in: nothing in the plugin dir changes unless config.json sets patchPluginAgents: true.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { spawnSync } = require('child_process');

const HOOK = path.join(__dirname, '..', 'hooks', 'agent-tools-patch.js');
const AGENTS = path.join(__dirname, '..', 'agents');
const tools = require(HOOK);

const mk = () => fs.mkdtempSync(path.join(os.tmpdir(), 'atp-'));
const agent = (t) => `---\nname: orch-x\ntools: ${t}\nmodel: sonnet\n---\n\nbody\n`;
function setup(cfg) {
  const data = mk(), dir = mk();
  fs.writeFileSync(path.join(dir, 'orch-scout.md'), agent('Read, Grep'));
  fs.writeFileSync(path.join(dir, 'orch-builder.md'), agent('Read, Bash'));
  if (cfg) fs.writeFileSync(path.join(data, 'config.json'), JSON.stringify(cfg));
  return { env: { BOSS_DATA: data }, dir, data };
}
const read = (dir, f) => fs.readFileSync(path.join(dir, f), 'utf8');
const EXTRA = { '*': ['mcp__all__*'], 'orch-scout': ['mcp__example__*'] };

test('off by default: extraAgentTools alone changes nothing', () => {
  const { env, dir } = setup({ extraAgentTools: EXTRA });
  assert.strictEqual(tools.run({ env, dir }), 0);
  assert.strictEqual(read(dir, 'orch-scout.md'), agent('Read, Grep'));
  assert.strictEqual(read(dir, 'orch-builder.md'), agent('Read, Bash'));
});

test('off when patchPluginAgents is not exactly true, or no config at all', () => {
  for (const v of [false, 'true', 1, null]) {
    const { env, dir } = setup({ patchPluginAgents: v, extraAgentTools: EXTRA });
    assert.strictEqual(tools.run({ env, dir }), 0, String(v));
    assert.strictEqual(read(dir, 'orch-scout.md'), agent('Read, Grep'));
  }
  const { env, dir } = setup(null);
  assert.strictEqual(tools.run({ env, dir }), 0);
});

test('opt-in: adds the missing tools, per agent and for "*", and is idempotent', () => {
  const { env, dir } = setup({ patchPluginAgents: true, extraAgentTools: EXTRA });
  assert.strictEqual(tools.run({ env, dir }), 2);
  assert.ok(read(dir, 'orch-scout.md').includes('tools: Read, Grep, mcp__all__*, mcp__example__*\n'));
  assert.ok(read(dir, 'orch-builder.md').includes('tools: Read, Bash, mcp__all__*\n'));
  assert.strictEqual(tools.run({ env, dir }), 0);
});

test('opt-in without extraAgentTools does nothing', () => {
  const { env, dir } = setup({ patchPluginAgents: true });
  assert.strictEqual(tools.run({ env, dir }), 0);
  assert.strictEqual(read(dir, 'orch-scout.md'), agent('Read, Grep'));
});

test('hook CLI without opt-in leaves the real plugin agents byte-identical and exits 0', () => {
  const sum = () => fs.readdirSync(AGENTS).sort().map((f) => f + crypto.createHash('sha1').update(fs.readFileSync(path.join(AGENTS, f))).digest('hex')).join();
  const before = sum();
  const { data } = setup({ extraAgentTools: { '*': ['mcp__zzz__*'] } });
  const r = spawnSync(process.execPath, [HOOK], { env: { ...process.env, BOSS_DATA: data, ORCH_DIR: '' }, encoding: 'utf8' });
  assert.strictEqual(r.status, 0);
  assert.strictEqual(r.stdout + r.stderr, '');
  assert.strictEqual(sum(), before);
});
