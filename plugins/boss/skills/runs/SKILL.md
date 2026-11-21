---
name: runs
description: "User-invoked only: list every run folder (live leases, resumable runs, finished runs, other folders) and offer which one to resume."
disable-model-invocation: true
argument-hint: "(no arguments)"
---
Main chat, user-invoked. Follow `${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md` (orchestrator protocol); no direct work here.

Read-only: this command changes nothing, takes no lease and launches nothing. It lists; the user picks. Never pick a run on your own.

## 1. Read the list

Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" runs` (Bash, one call, exactly this line). The CLI reads this session from env `WFP_SID`; never add a session flag, a pipe or a redirect.

It prints these sections, in order:

- `live: <run>  holder <sid8>[ (this session)]  until HH:MM UTC  activity quiet|recent|no-folder` per live lease, or `live: none`.
- `resumable: <run>  handoff|plan <iso> (<age>m)  preset <p>  lease none|expired|mine` per candidate, newest first, and `excluded: <run>  <reason>` for runs another session holds or that are done.
- `candidates: <k> of <n> checked`.
- `finished: <run>  <iso>` per done run, newest first.
- `other: <k> folders without PLAN.md or HANDOFF.md`.

## 2. Ask once

One AskUserQuestion, 2-4 options, the recommended one first and labelled (Recommended). The full `runs` output, as printed, goes in the `preview` of the first option. Never print it as text before the call: the VS Code panel collapses text that precedes a tool call.

| Option | What it does |
|---|---|
| `/boss:resume <newest candidate>` (Recommended) | the first `resumable:` line; the user types it, after `/clear` if this chat is long |
| Status of a listed run | run `/boss:status <run>`; the user names the run in the answer (default: the newest candidate) |
| Nothing | stop here |

- With no `resumable:` line, drop the first option and recommend "Nothing"; a run listed under `live:` with `(this session)` is already being driven here.
- A run listed under `live:` for another holder, or under `excluded:` with `lease other`, belongs to another session: never offer to resume it, and never take, renew or release its lease.

## 3. Act on the answer

- `/boss:resume <run>` or `/boss:status <run>`: tell the user the exact line to type. Don't run it yourself.
- Nothing: end the turn without another question.
