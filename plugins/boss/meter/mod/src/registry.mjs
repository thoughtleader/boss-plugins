// Registry adapter. All I/O is injected; this never repairs or writes run data.
export const MAX_RUNS = 120;
const SAFE_TOOL = /^[a-z][a-z0-9-]{0,31}$/;
const SAFE_RUN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const terminal = s => ['done', 'failed', 'cancelled'].includes(s);
export const clean = (value, max = 200) => String(value ?? '')
  .replace(/\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|$)/g, '')
  .replace(/[\x00-\x1f\x7f-\x9f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, max);
const list = async (io, dir) => { try { return await io.list(dir); } catch { return []; } };

export async function readRegistry(io, root) {
  const base = String(root).replace(/\/+$/, '');
  const entries = [];
  const errors = [];
  let scanned = 0;
  const tools = (await list(io, `${base}/runs`)).filter(e => e.kind === 'dir' && !e.isLink && SAFE_TOOL.test(e.name)).slice(0, 20);
  for (const tool of tools) {
    const dirs = (await list(io, `${base}/runs/${tool.name}`))
      .filter(e => e.kind === 'dir' && !e.isLink && SAFE_RUN.test(e.name))
      .sort((a, b) => (b.mtimeMs || 0) - (a.mtimeMs || 0));
    for (const run of dirs) {
      if (scanned++ >= MAX_RUNS) break;
      const key = `${tool.name}/${run.name}`;
      try {
        const raw = await io.read(`${base}/runs/${key}/progress.json`);
        if (raw.length > 512 * 1024) throw new Error('snapshot is too large');
        const snap = JSON.parse(raw);
        if (snap.schema !== 'boss.progress/1' || snap.tool !== tool.name || snap.run_id !== run.name || !Array.isArray(snap.items)) throw new Error('unsupported snapshot');
        let reporting = null;
        try {
          const history = await io.read(`${base}/report-state/${key}.json`);
          if (history.length <= 512 * 1024) {
            const candidate = JSON.parse(history);
            if (candidate.schema === 'boss.reporting/1' && candidate.key === key && candidate.startedAt === (snap.started_at || snap.created_at || null)) reporting = candidate;
          }
        } catch { /* Optional reporting history is not a damaged run. */ }
        entries.push({ key, reporting, snapshot: snap, title: clean(snap.title || snap.run_id), updatedAt: snap.updated_at || null });
      } catch { errors.push(key); }
    }
  }
  entries.sort((a, b) => Number(terminal(a.snapshot.state)) - Number(terminal(b.snapshot.state)) || (Date.parse(b.updatedAt) || 0) - (Date.parse(a.updatedAt) || 0));
  return { entries, errors };
}

// A selection made by the owner remains selected or missing. Never silently
// replace a vanished chosen run with another project's work.
export function chooseRun(entries, sessionId, selectedKey) {
  if (selectedKey) return entries.find(e => e.key === selectedKey) || null;
  const mine = entries.filter(e => sessionId && e.snapshot.owner?.session_id === sessionId && !terminal(e.snapshot.state));
  return mine.length === 1 ? mine[0] : null;
}

export function ownedRun(entries, sessionId) {
  return chooseRun(entries, sessionId, null);
}
