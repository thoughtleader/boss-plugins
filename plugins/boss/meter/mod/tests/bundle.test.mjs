import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const METER = path.resolve(HERE, '../..');
const PLUGIN = path.resolve(METER, '..');
const committed = path.join(PLUGIN, 'hooks/meter-register.js');
const haveEsbuild = (() => { try { createRequire(path.join(METER, 'package.json'))('esbuild'); return true; } catch { return false; } })();

test('hooks.json names the committed meter bundle under modules and keeps the orchestrator hooks', () => {
  const hooks = JSON.parse(fs.readFileSync(path.join(PLUGIN, 'hooks/hooks.json'), 'utf8'));
  assert.deepEqual(hooks.modules, ['./meter-register.js']);
  assert.ok(hooks.hooks && Object.keys(hooks.hooks).length > 0);
  assert.ok(fs.existsSync(path.resolve(PLUGIN, 'hooks', hooks.modules[0])));
});

test('the bundle is self-contained: no relative or node imports left in it', () => {
  const text = fs.readFileSync(committed, 'utf8');
  assert.doesNotMatch(text, /^\s*import\s[^;]*from\s+['"](\.|node:)/m);
  assert.doesNotMatch(text, /require\(['"]\./);
});

test('the committed bundle equals a fresh build of meter/mod/src', { skip: !haveEsbuild && 'esbuild is not installed (run npm ci in plugins/boss/meter)' }, () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'meter-bundle-'));
  try {
    const out = path.join(tmp, 'meter-register.js');
    execFileSync(process.execPath, [path.join(HERE, '../build.mjs'), '--out', out], { encoding: 'utf8' });
    assert.equal(fs.readFileSync(out, 'utf8'), fs.readFileSync(committed, 'utf8'),
      'hooks/meter-register.js is stale: run `node plugins/boss/meter/mod/build.mjs` and commit it');
  } finally { fs.rmSync(tmp, { recursive: true, force: true }); }
});
