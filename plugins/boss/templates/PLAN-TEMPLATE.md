---
run: <YYYY-MM-DD>-<job-slug>     # this file lives at <runs>/<run>/PLAN.md
goal: <one sentence, what is true when the run is done>
preset: <economy|balanced|quality|max>     # `node "$BOSS_LIB/../wfp/wfp.js" profile show`
ultracode: off     # on only when the owner turned it on: bar one step up, Workflow rows allowed
tier: large     # tiny | small | large
mode: <B|C>     # planning mode (PROTOCOL.md "Planning modes"): A try first (no plan) | B stage by stage | C full design; recorded for ledger tuning
mode_reason: <one line: how much is unknown, and why this depth>
repo: <repo name or path>     # ledger filter
area: <area within the repo>     # ledger filter
launch_total: <2 x rows + 2>     # D9: approval sets wfp maxLaunches = launches so far + 2 x rows still pending + 2
---

<!-- orch-planner: copy to <run>/PLAN.md and fill every <placeholder>. Comments are instructions; delete them once filled. -->
<!-- Paths: `$BOSS_LIB` is the plugin's lib dir (`/boss:start` prints `BOSS_LIB=<path>`; the brief carries it) and `<runs>` is the runs dir (`node "$BOSS_LIB/paths.js"` lists it). Substitute both before running a command. -->

# <Run title>

## Goal

<2–3 sentences: the outcome, who sees it, and the source of truth (spec, ADR, decisions file) by path.>

## must_haves

- **Source of truth:** `<decisions file, e.g. <run>/0-decisions.md or a gate-answers file>`: the owner's original decisions. They outrank this plan's wording, and the final review reads them first.
- **Truths** (observable when done; the final review checks each one):
  1. <user-visible behavior or checkable fact>
- **Artifacts** (exist and are substantive): `<path>`: <what it provides>
- **Key links** (wired, not just present): `<from>` → `<to>` via <import / call / route / config>

## Design

<!-- Mode C only (delete for B). The parts that must last: architecture (components and their boundaries), extension
points (where later work plugs in, by path), contracts (types, events, file formats, CLI verbs that other code relies
on), and what later work is expected to plug into each. Rows below build to this section. -->

- **Architecture:** <components and boundaries>
- **Extension points:** `<path>`: <what plugs in here, and how>
- **Contracts:** <signature / format / event>: <who relies on it>
- **Later work:** <what is expected to plug in, and where>

## Plan

Preset: <name> · <N> rows above bar, <M> below
Launch total: <rows> rows x 2 + 2 spare = <launch_total>

| # | Subtask | Agent type | Model | Effort | Mode | Isolation | Files/inputs | Done when | Wave | Why |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | <verb + object> | orch-builder | <bar> | <bar> | subagent | none | `<path>` | <checkable outcome> | 1 | |
| 2 | <same change over 5+ similar items> | orch-builder | <model> | <effort> | Workflow (invisible in VS Code) | none | `<glob>` | <checkable outcome> | 1 | <why Workflow; why off the bar, with ledger numbers> |
| 3 | needs a try: <question the planner can't answer without building> | orch-builder | opus | medium | subagent | worktree | throwaway branch | screenshot or result plus what it learned, in the result file; its answer patches the rows after it | 2 | creation (try row) |
| 4 | Goal-backward review | orch-reviewer | <bar> | <bar> | subagent | none | this plan, `<decisions file>`, tested SHA (the integration row's merged SHA, else the target branch HEAD), row 1–3 results | verdict written: passed / gaps_found / human_needed | 3 | |

Later stages (mode B only; delete for C):
- Stage 2: <outcome> · <rough row count> · detailed when reached

<!--
Mode B (stage by stage): detail only the next stage's rows (full rows and packs). List later stages under the table as
one line each, `Stage <k>: <outcome> · <rough row count>`; detail a stage (rows, packs) when it is reached, by patching
this file. Mode C: every row in full, built to ## Design.
Unknowns: when a row depends on something only a build can answer, write a `needs a try: <question>` row (an
orch-builder on a throwaway branch or worktree, like mode A) and keep planning; don't research it. Rows after it name it
in their Inputs, and its result patches them.
<bar> = the preset's model/effort for the row's role. Why is required whenever a row departs from it; cite the
your own numbers from `node "$BOSS_LIB/ledger.js" show` (same repo/area) when they exist.
- Above the bar: any row, with a reason. Below: clearly mechanical rows only; never planning or review under max.
- Mode: subagent (default) / wave / Workflow / agent team (only when workers must argue). Workflow only for 5+ similar
  items, unknown-count sweeps, 3-vote release checks or judge panels; it needs approval.
- Ambiguous or unknown work, or a first time in an area: propose mode A or a `needs a try` row, not deeper planning
  or max effort. opus/high only for mode C planning (Why `mode C`) and the final review before
  publish/deploy/migration/data (Why `pre-ship review`). A failed or weak row (diagnosis included) steps up per the
  ladder (`wfp.js escalate`), not straight to max. Max stays the owner's call.
- Append "· ultracode-shaped" to the header line when most rows are Workflow-shaped (only the owner turns it on).
- Isolation: none / worktree. Rows in one wave never edit the same files; parallel rows in one repo use worktree.
- After any wave whose rows ran in worktrees, a serial `orch-builder` integration row merges the wave's branches into
  the target branch, runs the combined tests and records the merged SHA. The final review checks that SHA. Each
  worktree row's pack names its base SHA and worktree path.
- Launch total: `2 x rows + 2`, the first try and the one step-up the retry rules allow on every row, plus 2 spare.
  Approving the plan approves its waves, including any wave of more than 2 agents. On approval, run
  `node "$BOSS_LIB/../wfp/wfp.js" set maxLaunches=<launches so far + 2 x rows still pending + 2> --sid=$WFP_SID` (read
  launches so far from `wfp.js status --sid=$WFP_SID`). An approved plan patch adds 2 per new row in the same answer.
  The launch cap and main-only never give way on retries; only the owner lifts them, apart from the cap widenings in
  PROTOCOL.md "Dispatch loop" steps 1 and 9.
- One commit per mutation row in a git repo: path-scoped, with the `Orch-Attempt:` trailer. Otherwise the result says
  `sha: none` with the reason, plus the artifact paths. The last row is always one goal-backward orch-reviewer against
  Goal, must_haves and the source of truth.
-->

## Suggestions

<!-- Plan-level suggestions for the owner (judge panel for a mode C design, agent team), one line each with its reason; "none" if none. Row-level ones go in the table's Why. -->

- <suggestion · reason>

## Context packs

One block per row. The five-part brief carries it (pasted, or this file's path plus the row number).

### Row 1 · <subtask>

- **Files:** `<path>` (edit) · `<path>` (read only)
- **Base:** <worktree rows only: base SHA `<sha>` · worktree `<path>` · setup `git switch -c orch/<run-slug>-r<#> <base>`; code paths repo-relative>
- **Key lines:** `<path>:<start>-<end>`: <what is there and why it matters>
- **Inputs:** <earlier rows' result paths and SHAs, or none>
- **Constraints:** <rules the worker can't see (CLAUDE.md, memory, repo rules); the scope fence>
- **Done when:** <checkable outcome, plus the command or evidence that proves it>
- **must_haves:** <the truths, artifacts and key links above that this row delivers>
- **Respect:** <memory files, decisions, ADRs, spike-findings skills, by path or name>
- **Result:** `<run>/<row#>-<slug>.md` when longer than the 5-line summary

<!-- Check per mode: B, one round where the checker patches rows itself, skipped on plans of ≤6 rows; C, the full
check with at most one revise round to the planner, later findings patched in place. Owner changes and checker findings
patch only the affected rows (main chat or checker edits this file); full re-plan only if the goal changes. -->

## Status

Plan check: <pending | passed, round N | patched, round 1 (mode B) | skipped (mode B, ≤6 rows) | issues, path> · Approved: <no | YYYY-MM-DD; covers all waves; maxLaunches set to N>

| # | Status | Ran on (model/effort) | Attempt | SHA / result | First-pass review | Retries | Escalated |
|---|---|---|---|---|---|---|---|
| 1 | pending | | | | | 0 | n |

<!-- Status: pending / running / done / blocked. Attempt: the ID from `node "$BOSS_LIB/manifest.js" launch`
(`<run>#<row>.<n>`), or `legacy`. Launch every Agent row through `manifest.js launch` so it carries an attempt ID
(description `[<model>/<effort>] <attempt-id> <subtask>`, brief line `Orch-Attempt: <attempt-id>`); never launch one
without it. Its ledger line comes from its completion notice; close each attempt with
`node "$BOSS_LIB/ledger.js" close --attempt <attempt-id> --first-pass y|n --outcome accepted|retry|blocked|dropped [--cause …]`
(`--cause` required with n or retry; the old `ledger.js update` form is still accepted). Only Codex, Workflow and
background-Bash rows keep a manual `ledger.js add`; reconcile reports them `legacy`. Update this table after each
completion notice. -->

## Resume

After /clear: `/boss:resume <run name>` (reads `<run>/HANDOFF.md` first; pass the run name); these steps are its fallback.
Here `<run name>` is the frontmatter `run:`, the bare folder name; every `--run` takes it, never the folder path.
1. Read the frontmatter and Status. `done` rows stay done. If the session's preset differs from `preset`, run
   `node "$BOSS_LIB/../wfp/wfp.js" profile <preset> --sid=$WFP_SID`. Re-check `maxLaunches` against launches so far +
   2 x rows still pending + 2.
2. Run `node "$BOSS_LIB/manifest.js" activity --run <run name> --sid=$WFP_SID`; on `state: recent` ask the owner
   (wait and recheck, or take over) before going on. Then run `node "$BOSS_LIB/manifest.js" lease check --run <run name> --sid=$WFP_SID`;
   on `mine`, `none` or `expired`, run `manifest.js lease acquire --run <run name> --sid=$WFP_SID` (`other`: another live
   session holds this run; ask the owner before `--force`, which a `legacy: yes` lease refuses with exit 3).
3. A `running` row: run `node "$BOSS_LIB/manifest.js" reconcile --run <run name> --row <n> --sid=$WFP_SID` and follow its verdict (done: `next: advance` marks it done, `next: gate` follows the plan's gate / unknown with its `next`: wait, redispatch or ask / legacy by the result file, else HANDOFF's commit check). Ask the owner about every `next: ask`.
   Continue at the lowest wave that still has `pending` rows. Reprint Status before launching.
