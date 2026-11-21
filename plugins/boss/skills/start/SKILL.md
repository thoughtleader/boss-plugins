---
name: start
description: "User-invoked only: start a new orchestrated run from a job description: name the run, write its BRIEF.md, take its lease, launch the planner and the plan check, then ask for approval."
disable-model-invocation: true
argument-hint: "<job description>"
---
BOSS_LIB=${CLAUDE_PLUGIN_ROOT}/lib

Print the line above as your first output line, exactly as shown (the path is already filled in). It gives this chat the absolute engine path. Then read `${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md` and follow it: it holds the tiers, planning modes, dispatch loop, ledger and lease rules. This chat orchestrates and does no direct work. When PROTOCOL.md writes an engine call as `node "$BOSS_LIB/<x>.js"`, run it with `$BOSS_LIB` replaced by the path printed above (`$BOSS_LIB` is not an environment variable).

Job: `$ARGUMENTS`

`<name>` below is the run folder name (bare, never a path). `<runs>` is `runsDir` from `node "${CLAUDE_PLUGIN_ROOT}/lib/paths.js" --json` (run it once); `<run>` is `<runs>/<name>`. The CLIs read this session from env `WFP_SID`: run every CLI line below exactly as written (placeholders filled in), with no session flag, pipe or redirect, so the guard treats it as housekeeping.

## 1. Name the run

- Run `date -u +%F` once (one guard warning is accepted). Slug: the job in kebab case, at most 40 characters, no "orch".
- `<name>` = `<date>-<slug>`. List `<runs>` with `ls` (no glob); if `<name>` exists, add `-2`, then `-3` and so on.

## 2. Write the brief

Run `node "${CLAUDE_PLUGIN_ROOT}/wfp/wfp.js" profile show` and `node "${CLAUDE_PLUGIN_ROOT}/wfp/wfp.js" status` (line 1 of each gives the preset and ultracode, and the session). Then write `<run>/BRIEF.md` with the Write tool; it creates the folder (no `mkdir`). Contents:

- the job, verbatim;
- created: the UTC date and time;
- driver session (the 8-character sid from `wfp.js status`);
- preset and ultracode;
- source of truth, only if the job names one;
- repo and area, if the job names them; otherwise `unknown` for each;
- planning mode, `A`, `B` or `C` (PROTOCOL.md "Planning modes"), picked from how much of the job is unknown, with a one-line reason.

## 3. Take the lease

Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease acquire --run <name>`.
Exit 3 means another session holds this run live. Ask the user (AskUserQuestion); never force it and never take it over on your own.

## 4. Plan (row 0)

Mode A (try first): no planner and no plan check. One AskUserQuestion proposes the mode with its reason. Options: "Try first (Recommended)", "Plan stage by stage (B)", "Full design (C)". On "Try first", launch one try-it `boss:orch-builder` row (`--row 1 --role implementation --model opus --effort medium`, Why `creation`, subtask `Try: <question>`): the smallest working version on a throwaway branch or worktree, returning a screenshot or result plus what it learned. Show the user the result in the next question, and repeat until the direction is settled; then set the mode to B or C in BRIEF.md and continue here. B or C: go on below, passing the mode and reason in the brief.

Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" launch --run <name> --row 0 --role planning --agent boss:orch-planner --model <bar> --effort <bar> --mode subagent --repo unknown --area unknown --subtask Plan`

- The two `<bar>` values are the planning role's model and effort from `profile show` (the column marked *).
- Replace `unknown` with the job's repo and area when it names them. Any text there is at most 80 characters.
- Line 1 of the output is the attempt ID. Launch one unnamed Agent: `subagent_type: "boss:orch-planner"` (the bare name `orch-planner` is not callable), that model and effort, description `[<model>/<effort>] <attempt-id> Plan`, and the five-part brief (PROTOCOL.md "Dispatch loop", step 3) carrying `<run>/BRIEF.md`, the line `Orch-Attempt: <attempt-id>`, the output `<run>/PLAN.md` from `${CLAUDE_PLUGIN_ROOT}/templates/PLAN-TEMPLATE.md`, and the commit-trailer line from "Row briefs" below. No time box and no lean-testing rule for planning: depth follows the unknowns. Stop rule: on an unknown, write a `needs a try: <question>` row and move on.
- When it returns, read its first line:
  - `LIGHT PATH: tiny` or `LIGHT PATH: small`: follow PROTOCOL.md "Size tiers" for that tier. No plan check and no approval question.
  - `SPLIT RECOMMENDED` or `BLOCKED`: one AskUserQuestion with its reasons in the recommended option's preview.
  - Otherwise go on to step 5.

## 5. Plan check (row 0c)

Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" launch --run <name> --row 0c --role plan-check --agent boss:orch-plan-checker --model <bar> --effort <bar> --mode subagent --repo unknown --area unknown --subtask Check`

- Use the plan-check role's bar and the same repo and area as row 0. The Agent call uses `subagent_type: "boss:orch-plan-checker"`.
- No time box on this row: the checker stops when the check is done, and an unknown becomes a `needs a try` row, not more checking.
- The brief names the output file `<run>/0c-plan-check.md` (round 2: `<run>/0c-plan-check-r2.md`). That overrides the agent file's `plan-check-r<round>.md`.
- Mode B: skip the check on plans of 6 rows or fewer (write `Plan check: skipped (mode B, ≤6 rows)` in PLAN.md's Status). Otherwise one round: the checker patches rows itself and never returns `revise`.
- Mode C, `revise`: launch row 0 again with the same line (it allocates the next attempt), with the check file as input, then row 0c again for round 2, which patches what is left in place. At most 1 revise round; on `blocked`, ask the user.

## 6. Approval

One AskUserQuestion: "Approve plan `<name>` (mode <B|C>: <reason>)?". The recommended option's `preview` holds the plan table (header `Preset: … · N rows above bar, M below`), the plan's launch total (`2 x rows + 2`) and the check's verdict. Never print them as text before the call, because the panel collapses it. Options:

| Option | What it does |
|---|---|
| Approve (Recommended) | follow `${CLAUDE_PLUGIN_ROOT}/skills/approve/SKILL.md` steps 2-5 for `<name>` |
| Adjust | patch only the affected rows in PLAN.md yourself (no planner run), then ask again |
| Re-plan at max | row 0 again at the top model and effort, then the check; only when the goal changed or the user asks |

- The user may override the mode (free-text answer): to A, park the plan and run step 4's try loop; between B and C, run row 0 again with the new mode, then the check.
- Away mode: if `<run>/DECISIONS.md` says `Mode: away`, don't ask. Approve on a passing check, log the decision there with the check file as evidence, and go on.
- Never write an `ACTIVE` file; the run's lease replaces it.

## Row briefs (default for every row of every run except planning)

Each row brief, and each brief `/boss:approve` composes, carries the items below. Planning rows (row 0 and any re-run of it, row 0c) are exempt from the time box and lean testing, because planning depth depends on unknowns, never on a clock; they carry only the commit-trailer line. Their stop rule: on an unknown, write a `needs a try: <question>` row and move on. The three items are defaults: a user who wants other limits edits this section.

- **Time box:** "~30 min target; at 45 min stop, commit what is green, and list unproven items in the result file." Never TaskStop or kill a running worker over the time box: paid calls run to completion.
- **Commit trailers:** "In the commit message, put the `Orch-Attempt: <attempt-id>` line and any `Co-Authored-By:` line in one final trailer block, with no blank line between them."
- **Lean testing:** "Follow the lean-testing line in `${CLAUDE_PLUGIN_ROOT}/templates/WORKER.md`."

## After approval

Follow PROTOCOL.md "Dispatch loop" steps 4-10 (ledger, work-state, waves, progress table, re-plan, rollover). Launch every row through `manifest.js launch` with description `[<model>/<effort>] <attempt-id> <subtask>` and the brief line `Orch-Attempt: <attempt-id>`; the Agent's `subagent_type` is always `boss:orch-<role>`. To stop or hand off, use `/boss:stop <name>`.
