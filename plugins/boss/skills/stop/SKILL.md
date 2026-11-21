---
name: stop
description: "User-invoked only: stop driving a run cleanly: reconcile its rows, write HANDOFF.md, release this session's lease on that run, then hand the user the /clear and `/boss:resume` line."
disable-model-invocation: true
argument-hint: "[run folder name]"
---
Main chat, user-invoked. Follow `${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md` (orchestrator protocol); no direct work here.

Argument: `$ARGUMENTS`

`<name>` is the run folder name (bare, never a path). `<runs>` is `runsDir` from `node "${CLAUDE_PLUGIN_ROOT}/lib/paths.js" --json` (run it once); `<run>` is `<runs>/<name>`. The CLIs read this session from env `WFP_SID`: run every CLI line below exactly as written, with no session flag, pipe or redirect. Every question is one AskUserQuestion, recommended option first, with the table or text it refers to in that option's `preview`.

## 1. Find the run

- An argument names the run.
- Otherwise run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease check` (no run flag: it lists the runs this session holds). One held run: use it. None: ask which run. Several: one option per held run, the newest first (Recommended).

## 2. Reconcile

Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" reconcile --run <name>` and keep its verdict per row.

- No row says `next: wait`: go on to step 3.
- Any `next: wait` (an agent of this session is still running): one AskUserQuestion, those rows in the preview.
  - "Wait for them (Recommended)": stop here; run `/boss:stop` again once their completion notices arrive.
  - "Stop them now": TaskStop each running agent, then run the reconcile line again. Rows still not `done` go into HANDOFF.md as running.
  - "Cancel": change nothing.
- Away mode (`<run>/DECISIONS.md` says `Mode: away`): never stop a running agent; paid calls run to completion. Wait for the notices, then go on.

## 3. Write the handoff

- Write `<run>/HANDOFF.md` from `${CLAUDE_PLUGIN_ROOT}/templates/HANDOFF-TEMPLATE.md` with the Write tool, at most 5 KB, pointers over copies (paths and SHAs).
  - Status table from the reconcile verdicts and PLAN.md Status; `next: gate` rows stay blocked with the reason in Notes.
  - "Running or orphaned at handoff": every row not `done` with `next: advance`, with its attempt ID.
  - Next steps: the exact launch line, description and result path per row, so the fresh chat needs no re-planning. `approved_next` names only rows the user already approved.
  - `preset` and `ultracode` from `node "${CLAUDE_PLUGIN_ROOT}/wfp/wfp.js" profile show`.
- If `work_state_write` is available: bring the run's items up to date (done, running, blocked); else the PLAN.md Status table is the record.
- Do not write ACTIVE; nothing reads it any more.

## 4. Release this run's lease

- Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease release --run <name>`. Always with the run name: it frees only this session's lease on that run, never another session's, and never with a force flag.
- Then `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease check --run <name>` must show it released (`state: none`, or not `mine`). If it still says `mine`, report that in one line.

## 5. Hand over

One AskUserQuestion: "Handoff written; /clear, then resume?"

- "/clear, then `/boss:resume <name>` (Recommended)": the preview is a one-line summary (run name and the HANDOFF.md path). Once the user picks it, print the paste line `/boss:resume <name>` in a fenced block in the next chat message and end the turn on it: text in a preview can't be copied.
- "Keep this chat": nothing more.
