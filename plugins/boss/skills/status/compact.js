#!/usr/bin/env node
'use strict';
// Read-only chat adapter for the boss-meter formatter (meter.mjs). No lease, launch, run-state write or LLM call.
// It saves an immutable report (Markdown + companion snapshot) under <progress root>/reports and, once the report is
// saved, advances the report-history cache under <progress root>/report-state; plans, snapshots, ledgers, leases and
// active tasks are never changed. Paths come from lib/paths.js. The meter files (meter.mjs, reporting.mjs, reports.mjs)
// are looked up in BOSS_METER_DIR, else <plugin>/meter; when any is missing the task report (progress.js --details)
// is printed instead.
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const paths = require(path.resolve(__dirname, '../../lib/paths.js'));
const { pathToFileURL } = require('node:url');

const text = file => { try { return fs.readFileSync(file, 'utf8'); } catch (e) { if (e.code === 'ENOENT') return null; throw e; } };
const json = file => { const t = text(file); return t == null ? null : JSON.parse(t); };
// A writer may be appending the final line. Ignore only an incomplete trailing event.
function jsonl(file) {
  const raw = text(file);
  if (raw == null) return [];
  const lines = raw.split('\n');
  return lines.flatMap((line, i) => {
    if (!line.trim()) return [];
    try { return [JSON.parse(line)]; }
    catch (e) { if (i === lines.length - 1 && !raw.endsWith('\n')) return []; throw new Error(`Invalid record in ${path.basename(file)}:${i + 1}`); }
  });
}

// Read-only counterpart of manifest.js's result lookup. Keep exact-attempt and
// single-attempt legacy matching: a retry must never inherit an unscoped file.
function resultReader(dir) {
  let names;
  const frontmatter = file => {
    let raw; try { raw = fs.readFileSync(file, 'utf8'); } catch { return null; }
    const lines = raw.split(/\r?\n/), first = lines.findIndex(line => line.trim() === '---');
    const last = first < 0 ? -1 : lines.findIndex((line, index) => index > first && line.trim() === '---');
    if (last < 0) return null;
    const fm = {};
    for (const line of lines.slice(first + 1, last)) {
      const match = /^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$/.exec(line);
      if (!match) continue;
      let value = match[2].trim();
      if (!/^["']/.test(value)) value = value.replace(/\s#.*$/, '').trim();
      fm[match[1]] = value.replace(/^"(.*)"$/, '$1').replace(/^'(.*)'$/, '$1');
    }
    return fm;
  };
  return (row, launch, attemptId, attemptCount) => {
    if (names === undefined) { try { names = fs.readdirSync(dir); } catch { names = []; } }
    const files = launch?.result ? [String(launch.result)]
      : names.filter(name => name.toLowerCase().startsWith(row + '-') && name.endsWith('.md')).map(name => path.join(dir, name));
    const candidates = files.flatMap(file => {
      const fm = frontmatter(file);
      if (!fm) return [];
      let mtime = 0; try { mtime = fs.statSync(file).mtimeMs; } catch {}
      return [{ path: file, fm, mtime }];
    }).sort((a, b) => b.mtime - a.mtime);
    const exact = attemptId ? candidates.find(item => item.fm.attempt === attemptId) : null;
    if (exact) return { ...exact, match: true };
    const bare = candidates.find(item => !item.fm.attempt);
    return bare ? { ...bare, match: !attemptId || attemptCount === 1 } : null;
  };
}

function retainCachedArtifacts(previous, rebuilt) {
  const attempts = Array.isArray(previous?.items) ? previous.items.filter(item => item?.kind === 'attempt') : [];
  const byAttempt = new Map(attempts.filter(item => item.meta?.attempt_id).map(item => [item.meta.attempt_id, item]));
  const byItem = new Map(attempts.map(item => [item.id, item]));
  for (const item of rebuilt.items || []) {
    if (item.kind !== 'attempt' || item.artifacts?.length) continue;
    const id = item.meta?.attempt_id;
    const sameItem = byItem.get(item.id);
    const cached = (id ? byAttempt.get(id) : null)
      || (sameItem?.parent === item.parent && (!id || !sameItem.meta?.attempt_id || sameItem.meta.attempt_id === id) ? sameItem : null);
    if (Array.isArray(cached?.artifacts)) item.artifacts = cached.artifacts.map(artifact => ({ ...artifact }));
  }
}

async function main() {
  const args = process.argv.slice(2), runArg = args.find(a => !a.startsWith('--')), run = runArg ? paths.runName(runArg) : runArg; // a run folder path works as well as the name
  if (!run || args.filter(a => !a.startsWith('--')).length !== 1 || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(run) || run.length > 128 || args.some(a => a.startsWith('--') && a !== '--json')) {
    throw new Error(`usage: progress.js <run folder name or path> [--details|--json] (got ${args.map(a => `'${a}'`).join(' ') || 'no arguments'})`);
  }
  const meterDir = process.env.BOSS_METER_DIR || path.join(paths.pluginRoot, 'meter');
  const meterFiles = ['meter.mjs', 'reporting.mjs', 'reports.mjs'].map(f => path.join(meterDir, f));
  if (!meterFiles.every(f => fs.existsSync(f))) {
    process.stdout.write(execFileSync(process.execPath, [path.join(__dirname, 'progress.js'), run, '--details'], { encoding: 'utf8' }));
    return;
  }
  const [meterFile, reportingFile, reportsFile] = meterFiles;
  const runs = process.env.BOSS_ORCH_RUNS || paths.runsDir();
  const registry = paths.progressRoot();
  const dir = path.join(runs, run);
  const snapshotPath = path.join(registry, 'runs/orchestrator', run, 'progress.json');
  let snap = json(snapshotPath);
  if (snap && (snap.schema !== 'boss.progress/1' || snap.run_id !== run || snap.tool !== 'orchestrator')) {
    throw new Error('Unsupported or mismatched progress snapshot');
  }
  const events = jsonl(path.join(dir, 'manifest.jsonl'));
  const planPath = path.join(dir, 'PLAN.md'), planText = text(planPath);
  const planMtime = planText == null ? 0 : fs.statSync(planPath).mtimeMs;
  const lastSeq = events.reduce((n, e) => Math.max(n, Number.isSafeInteger(e.seq) ? e.seq : 0), 0);
  // Rebuild stale/missing caches in memory using the publisher's own fold rules.
  // This reads the maintained PLAN status too; it never repairs a cache on disk.
  if (!snap || lastSeq > (snap.seq ?? 0) || (planText != null && planMtime > Date.parse(snap.updated_at || ''))) {
    if (!events.length && planText == null) {
      if (!snap) throw new Error(`No progress recorded for ${run}`);
    } else {
      const { buildSnapshot } = require(path.join(paths.pluginRoot, 'lib/progress-mirror.js'));
      const cfg = paths.loadConfig('engine').cfg;
      const parseAttemptId = id => {
        const m = /^(.*)#(\d{1,4}[a-z]?)\.(\d+)$/.exec(String(id));
        return m ? { run: m[1], row: m[2], n: Number(m[3]) } : null;
      };
      const previous = snap;
      snap = buildSnapshot(run, events, {
        runDir: dir, plan: planText == null ? null : { txt: planText, fm: {}, mtimeMs: planMtime },
        cfg, parseAttemptId, now: Date.now(), attentionFile: json(path.join(dir, 'attention.json')),
        resultFor: resultReader(dir),
      });
      retainCachedArtifacts(previous, snap);
    }
  }
  const { meterFromSnapshot } = await import(pathToFileURL(meterFile).href);
  const { reportingSnapshot, reportingState } = await import(pathToFileURL(reportingFile).href);
  const statePath = path.join(registry, 'report-state/orchestrator', `${run}.json`);
  let previous = null;
  try { previous = json(statePath); } catch { /* Corrupt presentation history cannot block reporting. */ }
  const nowMs = Date.now();
  snap = reportingSnapshot(snap, previous, { events, nowMs });
  const meter = meterFromSnapshot(snap, { nowMs, ledger: jsonl(path.join(dir, 'ledger.jsonl')) });
  const nextState = reportingState(snap, meter, previous, nowMs);
  if (meter.forecast) meter.forecast.delayHistory = nextState.delayHistory;
  if (nextState.delayAdded) {
    meter.attention.push(`${meter.forecast.reason || 'Work is taking longer than estimated'}; ${meter.forecast.finish.text}`);
  }
  if (args.includes('--json')) console.log(JSON.stringify(meter, null, 2));
  else {
    const { writeReportArtifact, reportText } = await import(pathToFileURL(reportsFile).href);
    const columns = Math.max(20, Math.min(240, Math.floor(Number(process.env.COLUMNS) || 72)));
    // The normal file link opens readable task details even without a native plugin.
    // A companion snapshot lets the plugin enhance this exact report, including mixed prose.
    // Artifact failures never suppress status or leak internal payloads into chat.
    let output;
    try {
      output = (await writeReportArtifact(meter, { root: registry, run, columns })).output;
      // This is presentation history, never a run/plan/lease mutation. Advance
      // the DONE boundary only once a readable report has been saved.
      try {
        fs.mkdirSync(path.dirname(statePath), { recursive: true, mode: 0o700 });
        const tmp = `${statePath}.${process.pid}.tmp`;
        fs.writeFileSync(tmp, JSON.stringify(nextState), { mode: 0o600 });
        fs.renameSync(tmp, statePath);
      } catch { /* The immutable report is still usable without a history cache. */ }
    }
    catch { output = `${reportText(meter, columns)}\n\nDetails: /boss:status ${run} --details`; }
    console.log(output);
  }
}

main().catch(e => { console.error(`progress: ${e.message}`); process.exitCode = 2; });
