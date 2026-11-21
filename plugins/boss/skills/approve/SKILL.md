---
name: approve
description: "User-invoked only: approve a checked plan: confirm the plan check passed, set the wfp launch cap (2 per pending row plus 2 spare), record the approval and launch the first pending wave."
disable-model-invocation: true
argument-hint: "[run folder name]"
---
Main chat, user-invoked. Follow `${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md` (orchestrator protocol); no direct work here.

Argument: `$ARGUMENTS`

`<name>` is the run folder name (bare, never a path). `<runs>` is `runsDir` from `node "${CLAUDE_PLUGIN_ROOT}/lib/paths.js" --json` (run it once); `<run>` is `<runs>/<name>`. The CLIs read this session from env `WFP_SID`: run every CLI line below exactly as written, with no session flag, pipe or redirect. Typing this command is the user's approval; `/boss:start` also comes here (steps 2-5) after an Approve answer.

## 1. Find the run

- An argument names the run.
- Otherwise run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease check` (with no run flag: it lists the runs this session holds). One held run: use it. None: ask the user which run. Several: one AskUserQuestion, one option per held run, the newest first (Recommended).

## 2. Check the plan and its check

- `<run>/PLAN.md` must exist. If it doesn't, say so in one line and stop.
- The check file: list `<run>` with `ls -t` (newest first, no glob). The first entry whose name matches `0c-plan-check*.md` or `plan-check-r*.md` is the check file. Read its frontmatter `status:`.
- The check passes when that status is in the engine config's `passStatuses` (defaults `done`, `passed`, `pass`; the data dir's `config.json` overlays them; `node "${CLAUDE_PLUGIN_ROOT}/lib/paths.js"` lists the data dir).
- A mode B plan whose Status says `Plan check: skipped (mode B, ≤6 rows)` passes with no check file.
- No check file, or a status outside that list: one AskUserQuestion, with the check's verdict and its blockers in the recommended option's preview. Options: "Patch the affected rows (Recommended)" (you edit PLAN.md in place, no planner run; a mode C plan still in round 1 goes back to `/boss:start` step 4 with the check file as input, then step 5), "Approve anyway", "Stop".

## 3. Set the launch cap

- Count the rows still pending in PLAN.md's Status table.
- Run `node "${CLAUDE_PLUGIN_ROOT}/wfp/wfp.js" status`. The `agents` line gives `launches` so far.
- N = launches so far + 2 x rows still pending + 2: the first try and the one step-up per row, plus 2 spare. Compute it from the rows, never from `launch_total`: a plan written before this rule (`launch_total` = rows plus an allowance) is approved with the same formula. Run `node "${CLAUDE_PLUGIN_ROOT}/wfp/wfp.js" set maxLaunches=N` with N written as the number.

## 4. Record the approval

- PLAN.md Status: set the line to `Approved: <UTC date>; covers all waves; maxLaunches set to N`. A re-plan that was already approved once writes `Re-approved: …` instead. Fill in `Plan check: passed, round <k>` from the check file (or keep `skipped (mode B, ≤6 rows)`).
- Away mode: if `<run>/DECISIONS.md` says `Mode: away`, append a table row: time (UTC), "Plan approved", and the check file path as evidence.
- Work-state list: if `work_state_write` tools are available (the optional claude-mem plugin), write one item per plan row, each starting `[<model>/<effort>]`. Otherwise keep PLAN.md's Status table current; nothing else is needed.

## 5. Launch the first wave

Take the lowest wave with pending rows. For each row in it:

- Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" launch --run <name> --row … --subtask "…"` with the row's subtask (quoted) and its role, agent type (`boss:orch-<role>`), model, effort, mode, repo and area from the plan (line 1 is the attempt ID; exit 3: another session holds the run live, so ask).
- Launch one unnamed Agent with `subagent_type: "boss:orch-<role>"`, description `[<model>/<effort>] <attempt-id> <subtask>` and the five-part brief: the row's context pack, the line `Orch-Attempt: <attempt-id>`, and the time box and lean-testing lines from `${CLAUDE_PLUGIN_ROOT}/skills/start/SKILL.md` "Row briefs".

Rows in one wave launch in parallel. The approval covers waves of more than 2 agents too. Then reprint the plan as a progress table and continue with PROTOCOL.md "Dispatch loop" steps 4-10.
