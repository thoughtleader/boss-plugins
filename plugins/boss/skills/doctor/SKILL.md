---
name: doctor
description: "User-invoked only: health-check the run setup (leases, worktrees, backups, hooks, wfp, config, runs) and fix the fixable findings once the user confirms."
disable-model-invocation: true
argument-hint: "[--fix <ids>|all]"
---
Main chat, user-invoked. Follow `${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md` (orchestrator protocol); no direct work here.

Argument: `$ARGUMENTS`

## 1. Check (read-only)

Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" doctor` (Bash, one call, exactly this line). It writes nothing. The CLI reads this session from env `WFP_SID`; never add a session flag, a pipe or a redirect.

- One line per finding: `<level> <check>: <message>[ · fix: <hint>]`, level `ok`, `info`, `warn` or `error`.
- Last line: `doctor: <w> warn, <e> error; fixable: <ids|none>`. The fixable ids are check names among `leases`, `worktrees` and `backups`.
- Exit 2 is a usage error: say so in one line and stop.

## 2. Confirm

- If the argument holds `--fix` with ids or `all`, that is the user's confirmation: skip the question and go to step 3 with those ids.
- If `fixable: none`, there is nothing to fix. Report the counts in one line and end; findings that are report-only (no fixable id) only get their `fix:` hint printed for the user to run by hand.
- Otherwise one AskUserQuestion, the recommended option first and labelled (Recommended). The full doctor output, as printed, goes in the `preview` of the first option. Never print it as text before the call: the VS Code panel collapses text that precedes a tool call.

| Option | What it does |
|---|---|
| Fix `<fixable ids>` (Recommended) | step 3 with every id on the `fixable:` line |
| Pick which | a second AskUserQuestion, multiSelect, one option per fixable id, each described by its findings |
| Leave as is | end here; print the `fix:` hints of the report-only findings |

## 3. Fix

Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" doctor --fix <ids>`, with `<ids>` the confirmed check names joined by commas (or `all`).

- It prints `fixed|skipped|failed <check>: …` per fix, then runs that check again.
- What a fix may do: release this session's own expired leases and its own leases on finished runs; `git worktree prune` and `git worktree remove` of merged, clean, quiet run worktrees (the branch is kept); move stale `*.bak-*` copies to `.trash/doctor-<UTC ts>/`.
- What it never does: delete files, force anything, or take, renew or release another session's lease. Never work around a `skipped` or `failed` line with your own commands; report it.
- The `hooks`, `runtime` and legacy `active` findings are report-only: never edit hook registrations, `settings.json` or an ACTIVE file by hand to clear them. Print the `fix:` hint for the user to act on. `/boss:doctor` offers no `--fix hooks`; the plugin registers its own hooks from its `hooks.json`.

## 4. Report

One short summary line per fixed check from the `fixed` lines, plus any `skipped` or `failed` lines verbatim. Don't ask another question unless something failed; then offer "Leave as is (Recommended)" or "Run `/boss:doctor` again".
