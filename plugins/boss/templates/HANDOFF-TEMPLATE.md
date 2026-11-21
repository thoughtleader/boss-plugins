---
run: <YYYY-MM-DD>-<job-slug>     # the run name; this file lives at <runs>/<run name>/HANDOFF.md
work_state_list: <work-state list name for this run, if work_state tools are available; else "none">
preset: <economy|balanced|quality|max>     # the session's preset when this was written
ultracode: off     # off | on, as the owner has it now
created: <YYYY-MM-DD HH:MM>
approved_next: none     # none | rows <n,n>: the owner already approved launching these on resume. Name only rows that can run once the running rows are reconciled; they must appear under Next steps
---

<!-- `$BOSS_LIB` is the plugin lib dir (printed by `/boss:start`) and `<runs>` the runs dir (`node "$BOSS_LIB/paths.js"`). Write this only when NOTHING is running. Keep it ≤5 KB. Pointers over copies: paths and SHAs, no pasted results. Delete comments once filled. First run `node "$BOSS_LIB/manifest.js" reconcile --run <run name> --sid=$WFP_SID` (`--run` takes the frontmatter `run:` name, never a path): every attempt that is not `done` with `next: advance` goes in the running table below (a `next: gate` row with its status in Notes; its dependents stay blocked). Release point: after this file is written, run `node "$BOSS_LIB/manifest.js" lease release --sid=$WFP_SID` with no `--run`; it frees every lease this session holds and never another session's. `<run>` below means the run folder's path, and `<run name>` (every `--run` takes it) means the frontmatter `run:`. Then tell the owner: /clear, then `/boss:resume <run name>`, always with the name: without it, resume lists candidate runs and asks. -->

# Handoff: <run title>

Goal: <one sentence>. Plan: `<run>/PLAN.md` (or none: Small tier). Decisions: `<run>/0-decisions.md` or the list below.

## Status

| # | Subtask | Agent / model / effort | Status | Result / SHA |
|---|---|---|---|---|
| 1 | <verb + object> | orch-builder / sonnet / high | done | `<run>/1-slug.md` · `<sha>` |
| 2 | <verb + object> | orch-builder / sonnet / high | pending | |

<!-- Status: done / pending / blocked / orphaned. Use `running` only for rows listed in the next section. -->

## Decisions since the plan

- <owner decision or plan change, with its date; "none" if none>

## Running or orphaned at handoff

<!-- Should be empty when nothing is running; list any row that may still be going or was cut off. Attempt rows are checked only by their attempt ID and reconcile, never by a commit subject; only legacy rows keep the subject-prefix check. -->

| # | Attempt | Result file expected | Repo / branch | Check | Notes |
|---|---|---|---|---|---|
| <n> | `<run>#<n>.<k>` or `legacy` | `<run>/<n>-slug.md` | `<repo path>` @ `<branch>` | attempt rows: `node "$BOSS_LIB/manifest.js" reconcile --run <run name> --row <n> --sid=$WFP_SID`; legacy rows: `git -C <repo> log --oneline -20`, subject starts `<type>(<scope>):` | <dirty files? worktree path?> |

## Next steps

<!-- Exact dispatch details so the fresh chat needs no re-planning. Rows named in approved_next must be here. Every Agent row launches through `manifest.js launch` and carries its attempt ID; never launch one without it. -->

1. Row <n>: `orch-builder` · model <m> · effort <e> · mode subagent · wave <w> · isolation <none|worktree>. Launch: `manifest.js launch --run <run name> --row <n> …`; description `[<m>/<e>] <attempt-id> <subtask>`; brief line `Orch-Attempt: <attempt-id>`. Brief: five-part, context pack = `<run>/PLAN.md` row <n> (inputs: `<run>/<k>-slug.md`, `<sha>`). Result: `<run>/<n>-slug.md`.
2. Codex row (only if the owner asked): `<exact command, e.g. codex exec --cd <repo> "$(cat <prompt file>)">` · output to `<path>` · manual `ledger.js add` (agent `codex`).
3. Final: `orch-reviewer` against the plan's Goal, must_haves and source-of-truth decisions, on the tested SHA, after all rows are done.

## Cautions

- <things a cold reader would trip on: shared checkout, files another agent owns, push/deploy not approved, a flaky check, a row that already failed once (escalate to <m>/<e>)>
