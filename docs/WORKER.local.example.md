# WORKER.local.md: example

Copy this file to `<data>/WORKER.local.md` and edit it. `<data>` is the boss data dir; `node "$BOSS_LIB/paths.js"` prints it.

## How it works

- `templates/WORKER.md` (with `WORKER.builder.md` and `WORKER.runtime.md` beside it) is the base worker contract. It ships with the plugin and is replaced on every update, so do not edit it.
- `<data>/WORKER.local.md` is yours. It is optional, and nothing in the plugin creates or overwrites it.
- `node "$BOSS_LIB/paths.js" worker <role>` prints the files a worker must read, one per line: the base file, the role files for that role (`WORKER.builder.md`, `WORKER.runtime.md`; with no role, all of them), then the local file when it exists. Briefs tell workers to read every file it lists, in that order.
- The local file adds to the base file and wins on conflict. Keep it short: a worker reads it on every row.

## What belongs here

Rules that are about you or your machine, not about the mechanism:

- Git identity and commit style.
- Package manager.
- Tool preferences for command output and search.
- Repos or directories that are off limits.
- Browser, dev-server ports and what a worker may start or stop.
- Where build copies and scratch files go.

## Example (neutral; replace with your own)

```markdown
# Local house rules

- Git identity: commit as `Example Dev <dev@example.com>`; check `git config user.email` in a new clone or worktree.
- Use `pnpm`, never `npm` or `yarn`.
- `legacy-service/` is frozen: no new work, no dependency bumps, no live checks.
- The docs site dev server runs on :4000. A worker may start it and must stop only the process it started.
- UI checks: use the browser on debug port :9333 and only the pages you opened yourself.
- Build output and scratch copies go in the session scratch directory, never in the repo.
- Prefer `rg` for text search and `jq` for JSON.
```
