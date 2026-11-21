---
name: away
description: "User-invoked only: turn away mode on or off for a run: record it in the run's DECISIONS.md, where the driver decides technical calls itself within hard limits and logs each one."
disable-model-invocation: true
argument-hint: "[run folder name] [off] [user's words]"
---
Main chat, user-invoked. Follow `${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md` (orchestrator protocol); no direct work here.

Argument: `$ARGUMENTS`

`<name>` is the run folder name (bare, never a path). `<runs>` is `runsDir` from `node "${CLAUDE_PLUGIN_ROOT}/lib/paths.js" --json` (run it once); `<run>` is `<runs>/<name>`. Away mode lives only in `<run>/DECISIONS.md`: its `Mode:` line and its decision table. `/boss:resume` reads that line and keeps the mode. Typing this command is the user's delegation.

## 1. Find the run

- An argument naming a folder under `runs/` names the run. `off` turns away mode off. Any other words are the user's words, kept verbatim.
- Otherwise run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease check` (no run flag: it lists the runs this session holds). One held run: use it. None or several: one AskUserQuestion, one option per candidate, the newest first (Recommended).
- Time: one `date -u '+%F %H:%M'` call gives `<UTC>`.

## 2. On

- No `<run>/DECISIONS.md`: create it with the Write tool:
  - `# Driver decisions (user away, autonomy delegated <UTC date>)`
  - `User, verbatim: "<user's words>"` (no words given: "none given").
  - `Mode: away since <UTC>`
  - One paragraph: the driver makes the user's calls and logs each one here with its evidence; the hard limits below still apply.
  - The table header `| # | Time (UTC) | Decision | Evidence |` and row 1: "Away mode on".
- DECISIONS.md exists: set its `Mode:` line to `Mode: away since <UTC>` (add the line under the user's words if missing) and append a row "Away mode on", with the user's words if given.

## 3. Off

Set the `Mode:` line to `Mode: attended since <UTC>` and append a row "User back; away mode off". From now on, ask the user as usual.

## Driver rules while away

- Before treating anything as open, read `<run>/0-user-decisions.md` (if present) and DECISIONS.md. Then decide technical calls yourself and log each one as a table row with its evidence (a run file path, a command's output line). Don't wait for the user.
- **Allowed:**
  - Approving a plan after a passing plan check (`/boss:approve` steps 2-5).
  - Landing after a passing pre-land review.
  - Setting wfp `maxLaunches` at approval (`/boss:approve` step 3), and widening it for rows you add (PROTOCOL.md "Dispatch loop" step 1): the main chat may widen the cap itself for rows it adds: 2 launches each, for at most half as many added rows as the plan had at approval (rounded down), each widening logged in DECISIONS.md. Beyond that: stop launching and log it as pending for the owner.
- **Hard limits:**
  - No take, force, renew or release of another session's lease.
  - No CLAUDE.md edits.
  - No amend, rebase, hard reset or force-push.
  - No retry of anything a permission layer blocked; log it as pending.
  - Push, deploy or publish only where the user's own rules (CLAUDE.md or memory) already allow it for that repo; otherwise log it as pending.
  - Paid calls run to completion: never stop a running agent (no TaskStop, no `/boss:stop` "Stop them now"; wait for the notice).
- A row that fails twice (the first try and its escalated retry) is marked blocked and logged; go on with rows that don't depend on it.
- Questions the protocol would ask the user become logged decisions, except those the hard limits leave pending. Pending items go in one AskUserQuestion when the user is back.
