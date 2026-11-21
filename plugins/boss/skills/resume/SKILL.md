---
name: resume
description: "User-invoked only: resume an orchestrator run in a fresh chat after /clear, from <run>/HANDOFF.md (or PLAN.md when no HANDOFF exists)."
disable-model-invocation: true
argument-hint: "[run folder name]"
---
Resume an orchestrator run after `/clear`. Keep this chat small: load about 10k tokens, no more. Follow `${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md` (orchestrator protocol; read it now): no direct work here, set `model` and `effort` on every Agent call, never pass `name`, one brief per row.

Argument: `$ARGUMENTS`

Every question to the user, approvals included, is an AskUserQuestion call with 2-4 options, the recommended one first and labelled (Recommended). Anything the question refers to (a table, list, plan, handoff, prompt, draft or diff) goes inside the call: in the option `preview` (the full status table in the approve option's preview), in the option descriptions, or in the question text. Never print it as text before the call, because the VS Code panel collapses text that precedes a tool call. Never ask the user in plain text. Paste-ready text (a handoff prompt, a command to run) is the exception: the preview carries a one-line summary, and the full text goes in a fenced block in the message after the user answers, which ends on that text (preview text can't be copied).

## 1. Find the run

`<runs>` is `runsDir` from `node "${CLAUDE_PLUGIN_ROOT}/lib/paths.js" --json` (run it once); `<run>` = `<runs>/<name>`. Every manifest.js `--run` takes `<name>`, the bare folder name; a path exits 2. ACTIVE is retired: nothing reads or writes it.
1. Argument given: a folder name under `runs/`, or a path. A path is reduced to its run folder's name. Use it.
   - its folder has `HANDOFF.md`: use it;
   - it has `PLAN.md` but no `HANDOFF.md`: use it anyway. Resume from PLAN Status, the work-state list and reconcile (§2 PLAN-only);
   - the folder is missing, or it has neither file: say so in one line and take the no-argument path (step 2). Never fall through to another run on your own.
2. No argument: run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" resumable --sid=$WFP_SID`. It is read-only and picks nothing. Each `resumable:` line is a candidate (run, `handoff` or `plan` kind, mtime and age, preset, lease); each `excluded:` line is a run left out, with its reason (`done`, `lease other …`, `activity recent …`). Ask (step 4).
3. No candidate (no `resumable:` line, and no `lease other … activity quiet` run to offer): say so, tell the user to type `/boss:resume <run name>`, and stop.
4. Asking for the run: one AskUserQuestion, each candidate run its own option (up to 3 `resumable:` runs: the newest `handoff` kind first as (Recommended), else the newest `plan` kind), with kind, age and preset in that option's description; plus "None of these". Put the excluded runs and their reasons in the question text. An excluded `lease other … activity quiet` run (probably this user's previous session) may also be an option; its description says that step 5 will ask before `--force`. Never pick on your own, even with one candidate. "None of these": tell the user to type `/boss:resume <run name>`, and stop. (Coming back from step 6 with no `resumable` output yet: run step 2 first.)
5. Lease: run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease check --run <name> --sid=$WFP_SID` (each run has its own lease; a bare `lease check` shows only this session's own leases and never prints `other`). `state: other` means another session holds this run's lease live and may still be driving it: ask (AskUserQuestion; `holder`, run and `expires` in the question text) whether to wait for expiry or take over with `--force` in §4. With `legacy: yes` (held through the old `ACTIVE.lease`) `--force` exits 3: only expiry, or the holder's own renew or release, ends it, so offer waiting, not take over. A lease on a different run is never a conflict. `state: none` does NOT mean the run is idle: a driver that never took the lease leaves it `none`, so step 6 always runs.
6. Live-driver check, before anything is loaded or acquired. It gates §4 step 1 (the re-acquire). Run:
   `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" activity --run <name> --sid=$WFP_SID`
   It is read-only. `state: quiet` (nothing recent, or a clean handoff): go on. `state: recent` means another session may still be driving this run (a result file, PLAN.md, the ledger or a manifest event changed within 15 min, or another session's agent with this run in its brief was active within 3 min): ask before going on, one AskUserQuestion whose question text gives the `newest:` path, its age and the `agent-live:` tier (`strong` = an attempt ID or the run's `runs/` path in a live agent's brief, `weak` = only the bare run name). Options:
   - "Another session may still be driving it: wait and recheck (Recommended)": stop here; when the user says go, rerun `activity` and ask again if it is still `recent`.
   - "Pick another run": go back to step 4.
   - "Take over anyway": go on; §4's lease step then runs as written (with a live lease held by another session it still asks before `--force`; a live `legacy: yes` lease can't be forced, so then wait for its expiry).

## 2. Load only this

- `<run>/HANDOFF.md`, whole (it is ≤5 KB).
- `<run>/PLAN.md` if it exists: the frontmatter, and the `## Status` section to the end of the file. Find lines with `grep -n '^## ' <run>/PLAN.md`, then Read with offset/limit. Never read `## Context packs`. If HANDOFF's table lacks a row's agent/model/effort, read the `## Plan` table too (it ends before `## Context packs`).
- If `work_state_read` is available: read the list named in HANDOFF frontmatter `work_state_list` (claude-mem MCP; if the tool isn't loaded, ToolSearch `select:` it first). Without it, skip the list and use HANDOFF and the PLAN Status table. Don't dump big output: you need item status only.
- Nothing else unless a step below needs it. HANDOFF is newer than PLAN Status and the work-state list, so it wins when they disagree (a work-state item still `doing` for a row HANDOFF calls done is done; fix the item in §4).
- In HANDOFF, `<run>` stands for the run folder §1 found; substitute it before using a path.
- **Work-state recency.** `work_state_read` prints relative times per list and per item ("updated N minutes ago"). If the run's list, or any of its items, was updated within 15 minutes and step 6 did not already ask, ask the same §1 step 6 question (with the list name and its age in the question text) before §3. A recent update means another chat is likely still driving the run. Skip the ask when HANDOFF.md exists and the list's and items' latest update is no newer than HANDOFF.md's mtime + 120 s (a clean handoff, the same rule as `manifest.js activity`: a driver writes work-state seconds before HANDOFF.md). Compare by age: HANDOFF.md's age is `date +%s` minus `stat -c %Y <run>/HANDOFF.md`; the update is no newer than the handoff + 120 s when the work-state age in seconds is at least the handoff age minus 120. Any update after that window, or no HANDOFF.md, keeps the ask.
- **PLAN-only** (no HANDOFF): `preset` and `ultracode` come from PLAN frontmatter; treat `approved_next` as `none`; PLAN Status rows `running` are the rows to reconcile. The work-state list is not always named for the run (drivers often name it for the job, e.g. `council-runner`), so find it by its `run_dir=` field: `work_state_read` with no list name to see the project's lists, then take the list whose `run_dir=` names this run folder (else a list named for the run). None found: say so in one line, skip it, and rely on §1 step 6.
- If `<run>/DECISIONS.md` says `Mode: away`, stay in away mode: follow `${CLAUDE_PLUGIN_ROOT}/skills/away/SKILL.md` driver rules and log decisions there.

## 3. Restore settings

- `node "${CLAUDE_PLUGIN_ROOT}/wfp/wfp.js" profile show --sid=$WFP_SID` (line 1 shows the preset and the ultracode mirror). If its preset differs from HANDOFF `preset`, run `node "${CLAUDE_PLUGIN_ROOT}/wfp/wfp.js" profile <preset> --sid=$WFP_SID`.
- Ultracode: if HANDOFF `ultracode` differs from the mirror, restore it both ways: `node "${CLAUDE_PLUGIN_ROOT}/wfp/wfp.js" ultracode on --sid=$WFP_SID` or `node "${CLAUDE_PLUGIN_ROOT}/wfp/wfp.js" ultracode off --sid=$WFP_SID`. This restores the user's choice recorded at handoff. wfp only mirrors ultracode; the native ultracode state is unknown to it, so say so in one line and let the user correct it.
- Re-check wfp `maxLaunches` (`wfp.js status --sid=$WFP_SID`) against launches so far + 2 x rows still pending + 2 (PROTOCOL.md "Dispatch loop" step 1). Widen it only as that step allows: 2 per new row of an approved plan patch, or in away mode for rows the main chat adds (within the stated limit, logged in DECISIONS.md). Beyond that only the user lifts it.

## 4. Reconcile rows that were running

Take every row HANDOFF lists as running (and any row the PLAN Status shows `running`). Agents from before the clear can't report back, so decide from files:
1. Lease (re-acquire), only after §1 step 6 said `state: quiet` or the user picked "Take over anyway": run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease check --run <name> --sid=$WFP_SID`. If it says `mine`, `none` or `expired`, run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease acquire --run <name> --sid=$WFP_SID` (`mine` renews it). If it says `other` (another live session holds this run), ask the user (AskUserQuestion) before `lease acquire --run <name> --sid=$WFP_SID --force`; with `legacy: yes` that exits 3 too, so wait for expiry. Leases on other runs, this session's or anyone's, never block it.
2. Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" reconcile --run <name> --sid=$WFP_SID` and act on each row's verdict:
   - `done` with `next: advance`: mark it done with the SHA@branch it names (or `sha: none`) and the result path.
   - `done` with `next: gate`: the attempt finished but its check did not pass. Follow the plan's gate for that row (send the gaps back as a retry, or ask the user), and never mark its dependents ready.
   - `unknown` with `next: wait`: the agent is still running in this session; leave the row running.
   - `unknown` with `next: redispatch`: re-dispatch it as a new attempt (`manifest.js launch`; if it answers that the attempt is still running, rerun it with `--new`, since reconcile has already found it not live), naming any dirty files reconcile printed in the brief.
   - `unknown` with `next: ask`: ask the user, with reconcile's evidence in the question.
   - `legacy`: decide by the old rule (the result file's status and sha; else HANDOFF's commit check, and if neither, mark it `orphaned` and re-dispatch fresh). Never `reset --hard`.
3. Never mark a row done from a commit subject alone.

Notes: `launch --sid` also takes this run's lease when it is free (stdout unchanged; stderr `note: lease acquired by …`), and exits 3 only when another session holds this run live. Exit 3 is a live conflict on this run: ask, never `--force` on your own. Row ids are digits plus an optional letter (`3`, `3b`); manifest.js and ledger.js reject a letter-only id (such as `P`), so such a row never has a launch event and is decided by the legacy rule.

Write the outcomes back: `work_state_write` for the list, and the PLAN.md Status table if it exists (`orphaned` goes in as `pending`, with a note). Add `node "${CLAUDE_PLUGIN_ROOT}/lib/ledger.js" add …` only for legacy rows that finished while you were away and have no ledger line yet, when you have the numbers; rows with attempt IDs get their ledger line from reconcile's sweep.

## 5. Report and go on

The status table: `# | Subtask | Agent/model/effort | Attempt | Status | Result path/SHA`. Status is one of done / pending / running / orphaned / blocked / gate. Then the next step:
- HANDOFF frontmatter `approved_next:` names rows (not `none`): the user already approved that wave. Reprint the table as a progress table (no question needed) and launch exactly those rows by HANDOFF "Next steps", but only if every row they take input from is done after §4. Launch each through `manifest.js launch --run <name> --row <n> … --sid=$WFP_SID` (line 1 is the attempt ID), with the description `[<model>/<effort>] <attempt-id> <subtask>`, the brief line `Orch-Attempt: <attempt-id>`, the protocol's five-part brief and the wave rules. A launch that exits 3 means another session holds this run's lease live: ask. A row whose input is orphaned, blocked or at a gate waits and goes in the question below, as do rows outside `approved_next` (including other orphans).
- `approved_next: none`, PLAN-only, or anything unclear: one AskUserQuestion with the status table in the recommended option's `preview`. Options: the HANDOFF next step (Recommended); re-dispatch orphaned rows; change the plan.
- Everything done: dispatch the final `orch-reviewer` row if it's pending; otherwise report the run complete. At run end: `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease release --run <name> --sid=$WFP_SID`.

After each completion notice follow the dispatch loop (ledger, work-state, reprint the changed rows). To roll over again later, when nothing is running: reconcile, write a new `<run>/HANDOFF.md` from `${CLAUDE_PLUGIN_ROOT}/templates/HANDOFF-TEMPLATE.md`, then the **Release point**: `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease release --sid=$WFP_SID` with no `--run` frees every lease this session holds, never another session's (`lease: none (nothing to release)` is fine). Then tell the user to type `/clear` and then `/boss:resume <run name>`, always with the name.
