---
name: cost
description: "User-invoked only: show what a run used, per row (attempts, tokens, tool uses, time, retries, escalations, first-pass), or one line per run with no argument."
disable-model-invocation: true
argument-hint: "[run folder name]"
---
Main chat, user-invoked. Follow `${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md` (orchestrator protocol); no direct work here.

Argument: `$ARGUMENTS`

Read-only: this command changes nothing. "Cost" here means tokens, tool uses and time from the run ledger. There are no prices and no dollar figures; never estimate or convert any.

## 1. Read the ledger

- With a run name, run `node "${CLAUDE_PLUGIN_ROOT}/lib/ledger.js" cost --run <name>` (Bash, one call, exactly this line).
  - Line 1: `cost: run <name>: <rows> rows, <attempts> attempts, tokens <sum>, tools <sum>, time <sum>`.
  - Then one line per row: attempts, model/effort, tokens, tools, time, retries, escalated, first-pass.
  - An `unknown:` line counts attempts whose numbers were never recorded; `?` marks them in the table. Report them as unknown, never as zero.
  - `cost: run <name> has no ledger yet` means no row has finished; say so.
- With no argument, run `node "${CLAUDE_PLUGIN_ROOT}/lib/ledger.js" cost`. It prints one line per run that has a ledger, newest first: run, rows, attempts, tokens, time, retries, escalations.
- The line must stay exactly as written (no pipe, no redirect, no session flag) so the guard treats it as housekeeping. Exit 2 is a usage error: say so in one line and stop.

## 2. Ask once

One AskUserQuestion, 2-4 options, the recommended one first and labelled (Recommended). The full `cost` output, as printed, goes in the `preview` of the first option. Never print it as text before the call: the VS Code panel collapses text that precedes a tool call.

| Option | What it does |
|---|---|
| Done (Recommended) | stop here |
| Status of `<name>` | run `/boss:status <name>` (with no argument: the user names the run) |
| Cost of another run | the user names the run in the answer; run this command's step 1 for it |

## 3. Act on the answer

- Status: tell the user the exact line to type. Don't run it yourself.
- Another run: run its `cost` line, then ask once more the same way.
- Done: end the turn without another question.
