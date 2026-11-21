---
name: status
description: "User-invoked only: show aligned progress with current/upcoming tasks and linked full details; --details for the former task report, --full for the ledger table, or one line per run with no argument."
disable-model-invocation: true
argument-hint: "[run folder name] [--details|--full]"
---
Main chat, user-invoked. Follow `${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md` (orchestrator protocol); no direct work here.

Argument: `$ARGUMENTS`

Run state is read-only: this command takes no lease and launches nothing. A named compact report saves an immutable Markdown report and companion snapshot under the Boss reports folder; plans, progress caches, ledgers and active tasks are never changed.

## 1. Read the status

- With a run name (no detail flag), run `node "${CLAUDE_PLUGIN_ROOT}/skills/status/progress.js" <name>` (Bash, one call, exactly this line). This reads the shared `boss.progress/1` snapshot and run ledger. A missing or stale snapshot is folded from manifest events and the maintained plan in memory only. It never writes a cache, takes a lease, or launches work.
  - Reproduce the report and its normal DETAILS Markdown link intact. The link opens a readable full task report even without the native plugin. With the plugin loaded, the same snapshot draws as an inline component; any surrounding explanatory prose is preserved as bullets inside expanded Details, so the bars remain first. Never print HTML markers or encoded payloads. The formatter owns the spacing; never retype it, split it into inline code, or turn it into a table.
  - Equal-size W/T badges identify wave and task progress. The upper W bar carries cumulative run time/usage; the lower T bar carries the current wave's figures. A run without waves has one T row. Numeric columns align.
  - Do not add speaker/status headings, fractions, percentages, token labels, time-unit suffixes, column-divider glyphs, or usage asterisks. The formatter uses MM:SS clocks; both switch to HH:MM:SS when either reaches an hour. Chat reports are snapshots; the live pane and Board update.
  - Ticks are visual resolution, not individual waves/tasks. Unknown usage is not zero; reporting completeness is in Details. Tokens are reported agent tokens, not billing or context-window occupancy.
  - Keep the formatter's three visible ☑/➜/⮡ rows for completed/current/upcoming tasks, at most two each with a truthful remaining count, and any attention text. ☐ means nothing has completed yet. Never prepend completion prose above the bars; supporting findings belong in concise bullets after the DETAILS link. DETAILS expands all current/queued tasks, recent results and reporting context. Do not append duplicate task tables or next-up lists.
  - The meter needs the boss-meter files (`meter.mjs`, `reporting.mjs`, `reports.mjs`) in `BOSS_METER_DIR` or `<config dir>/boss`. Without them the script prints the task report instead (the `--details` view); reproduce that verbatim too.
- With `--details`, run `node "${CLAUDE_PLUGIN_ROOT}/skills/status/progress.js" <name> --details`. This retains the task strip, running/blocked/retry and recently completed task table, result paths, and next-up list for inspection on demand.
- With a run name and `--full` (either order), run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" status --run <name>` (Bash, one call, exactly this line), the whole plan table:
  - Line 1: `status: run <name>  preset …  plan yes|no  rows <done>/<total>`.
  - Then one line per row: row, subtask, status, attempts, latest attempt ID, model/effort, sha, tokens, time.
  - The last line is the lease line (none, mine, other or expired).
- With no argument, run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" status`. It prints one line per run folder (rows done/total, open rows, tokens, lease), runs that are not done first.
- The CLI reads this session from env `WFP_SID`. Never add a session flag, a pipe or a redirect: the line must stay exactly as written.
- Exit 2 is a usage error (for example an unknown run) from either command. Say so in one line and stop.

## 2. Present the result

For a named run with no detail flag, show the compact output and stop. Do not put it in an AskUserQuestion preview or add a routine follow-up question. Preserve its DETAILS link; if report storage is unavailable, preserve the printed detail command instead. This status request does not authorize resuming work.

For `--details`, `--full`, or the no-argument run list, retain the following inspection flow (these outputs include lease information):

One AskUserQuestion, 2-4 options, the recommended one first and labelled (Recommended). Put the whole output, as printed, in the `preview` of the first option. Never print it as text before the call: the VS Code panel collapses text that precedes a tool call.

With a run name:

| Option | When | What it does |
|---|---|---|
| Continue `<name>` (Recommended) | rows still open and the lease line says `mine` | carry on with the plan's dispatch loop in this chat |
| `/boss:resume <name>` (Recommended) | rows still open and the lease is none, expired or other | the user types it (after `/clear` if this chat is long); never take another session's lease |
| Full plan table | progress view shown | the user types `/boss:status <name> --full` |
| Cost of `<name>` | always | run `/boss:cost <name>` |
| Nothing | always | stop here |

When every row is done, "Nothing" is the recommended option and the continue or resume option is dropped.

With no argument: the first option names the first run in the list that is not done (`/boss:resume <that run>`, Recommended), then "Status of another run" (the user names it in the answer), "Cost of all runs" (`/boss:cost`), "Nothing". Never pick a run on the user's behalf beyond recommending it.

## 3. Act on the answer

- Continue: follow the PROTOCOL.md dispatch loop from the lowest open row; launches go through `manifest.js launch` as usual.
- `/boss:resume`, `/boss:status <name> --full` or `/boss:cost`: tell the user the exact line to type. Don't run it yourself.
- Nothing: end the turn without another question.
