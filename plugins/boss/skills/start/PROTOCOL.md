# Boss orchestration protocol

How the main chat runs a job as an orchestrator: it plans, dispatches small rows to fresh subagents, tracks results in files and does little of the work itself. `/boss:start` loads this file. Every number below (row limits, model bars, time-boxes) is a default you can change in your data-dir config or your own CLAUDE.md; the mechanisms are what the engine and hooks rely on.

The dispatch idea is adapted from Kilo Code's Orchestrator mode.

## Setup: `$BOSS_LIB`

`/boss:start` prints `BOSS_LIB=<absolute path>` on its first line. That is the plugin's `lib/` directory. In every command below, replace `$BOSS_LIB` with that printed path. The shell has no such variable, so write the literal path.

- Engine: `node "$BOSS_LIB/manifest.js"`, `ledger.js`, `lease-store.js`, `doctor.js`, `metrics.js`, `processes.js`.
- Usage policy (wfp): `node "$BOSS_LIB/../wfp/wfp.js"`.
- Progress meter: `node "$BOSS_LIB/../skills/status/progress.js" <run>`.
- Templates: `$BOSS_LIB/../templates/{PLAN-TEMPLATE,HANDOFF-TEMPLATE,WORKER}.md`.
- Paths: `node "$BOSS_LIB/paths.js"` prints the data dir, the runs dir and the other resolved paths. `<run>` below means `<runs>/<YYYY-MM-DD>-<job-slug>`, the folder holding one job's plan, results, manifest and ledger.
- Pass the sid on engine calls as `--sid=$WFP_SID` where a command takes one. Run each CLI line exactly as written, with no pipe or redirect, so the guard counts it as housekeeping.
- Commands: `/boss:start`, `/boss:approve`, `/boss:away`, `/boss:stop`, `/boss:resume`, `/boss:status`, `/boss:cost`, `/boss:runs`, `/boss:doctor`.
- Agents: `subagent_type: "boss:orch-<role>"` with role `scout`, `builder`, `planner`, `plan-checker` or `reviewer`. Pass the agent type to `manifest.js launch --agent boss:orch-<role>`.

## The main chat's job

The main chat orchestrates. It keeps its own context small so a long job stays workable.

- **Do inline** when the work is small and contained: about 5 tool calls or fewer, known files, about 10k tokens of output or less. A one-file edit, a quick query, a git check, reading a short result or a screenshot.
- **Delegate** when the work would bloat context or run long: reading more than 3 files or unknown code, test or build output, browser sessions, large diffs, parallel work, anything over about 15 minutes, independent review. If inline work grows past these limits, stop and delegate the rest.
- **Allowed in the main chat:** asking the user questions, memory and work-state writes, SendMessage and ListAgents, `wfp.js`, `ledger.js` and `manifest.js` calls, writing plan and decision files into the run folder, reading a subagent's result file when its summary isn't enough, and one-line checks (`git status`, one `cat` of a short file).
- **Keep state in files.** Context size never pauses work. Write each user ruling and each session fact (what the user asked for, a parameter the user added, the text of a pending question) to `<run>/DECISIONS.md` and each result path and SHA to the run folder as it arrives, keep the PLAN.md Status table current at each wave boundary, and never rely on chat memory for attempt IDs, running agents or pending questions. After a context compaction, run `node "$BOSS_LIB/manifest.js" reconcile --run <run> --sid=$WFP_SID` and `/boss:status <run>`, read `<run>/DECISIONS.md`, then check running agents before launching anything.
- **Verify claims.** Read a result's frontmatter and spot-check it. If a summary claims something that matters (tests green, deployed, fixed), ask for the evidence (command output, screenshot path) or dispatch a cheap verifier before reporting it as done.

## Size tiers

Every tier gets a run folder (`<run>`) holding results and the ledger.

| Tier | Size | How it runs |
|---|---|---|
| Tiny | One known file, a few lines | Inline in the main chat. No subagent, no plan. |
| Small | 2 to 4 steps | `orch-scout`, then `orch-builder`, then `orch-reviewer` as the test check. No approval step. |
| Large | Anything bigger | `orch-planner` writes `<run>/PLAN.md` from the plan template; `orch-plan-checker` checks it; the user approves; waves run; one goal-backward `orch-reviewer` checks the result against the plan's goal and must-haves. |

### Planning gate (configurable default)

A plan of 8 rows or fewer needs no `orch-planner` and no `orch-plan-checker`: the main chat writes the plan table in the approval question and runs it as the Small tier plus waves. Use `orch-planner` for mode C (big and meant to last) or when the user asks. Keep planner and checker rows short, with no revise round on small plans. The main chat patches PLAN.md for surprises instead of re-planning. Raise or lower the 8-row line to fit how much planning overhead you want.

## Planning modes

How much is unknown sets the planning depth, never a clock. Propose one mode, with a one-line reason, in the approval question; the user can override it. Record it in PLAN.md frontmatter (`mode:`, `mode_reason:`).

- **A, Try first** (mostly unknown: a proof of concept, design or art, a new library, an unclear approach). No plan. One small try-it `orch-builder` builds the smallest working version on a throwaway branch or worktree and returns a screenshot or result plus what it learned. The user reacts; repeat until the direction is settled, then move to B or C.
- **B, Stage by stage** (partly known). The next stage in full detail, later stages one line each, detailed when reached. Light check: one round, the checker fixes rows itself; skip the check on plans of 6 rows or fewer.
- **C, Full design** (big, well understood, meant to last). A full plan with architecture, extension points, contracts and what later work plugs into. Full check, at most one revise round; later findings are patched in place.

**All modes:** an unknown stops the planner instead of sending it off to research. It writes a `needs a try: <question>` row, which works like mode A and whose result patches the rows after it. User changes and checker findings patch only the affected rows; there is no new planner run. A full re-plan happens only if the goal changes.

## Dispatch loop

1. **Plan table** (Large tier). A header line `Preset: <name> · N rows above bar, M below`, then rows in execution order:

   | # | Subtask | Agent type | Model | Effort | Mode | Isolation | Files/inputs | Done when | Wave | Why |
   |---|---|---|---|---|---|---|---|---|---|---|

   Why is required whenever a row departs from the preset. Mode is subagent (default), wave, Workflow, background-bash (a council run or other shell job with no Agent notice), or agent team (only when workers must argue with each other). Use Workflow only for 5 or more similar items, unknown-count sweeps, multi-vote release checks and judge panels, and only when the user has approved it. The plan states its launch total: `2 x rows + 2`, room for the first try and the one step-up the retry rules allow on every row, plus 2 spare.

   Ask once, naming the planning mode and its reason, whether to approve or adjust; put the full plan table in the approve option so the user sees what they approve. Approving the plan approves its waves, including a wave of more than 2 agents. On approval, set the launch cap: read launches so far from `node "$BOSS_LIB/../wfp/wfp.js" status --sid=$WFP_SID`, then run `node "$BOSS_LIB/../wfp/wfp.js" set maxLaunches=<launches so far + 2 x rows still pending + 2> --sid=$WFP_SID`. Count the rows, never an old plan's number: a plan written before this rule (`launch_total` = rows plus an allowance) is approved with the same formula. The cap counts fresh Agent launches only; a continuation of the same agent does not count. An approved plan patch adds 2 per new row in the same answer (step 9). **Away mode:** the main chat may widen the cap itself for rows it adds: 2 launches each, for at most half as many added rows as the plan had at approval (rounded down), each widening logged in DECISIONS.md. Beyond that: stop launching and log it as pending for the owner. The cap stays as a backstop against a loop: it never gives way on retries, and only the user lifts it beyond these two widenings. `/boss:approve` does this approval step and launches the first wave.

2. **Fresh agent per row.** Each row gets a new subagent whose only context is its brief. Use SendMessage (by agent ID) only to fix or follow up on the same row, never to start the next row or to escalate. Never pass `name` to the Agent call unless the row's approved mode is agent team; a named spawn becomes a teammate.

3. **Brief.** See "The five-part brief" below.

4. **Results live in files.** Workers return a 5-line summary with paths and the commit SHA. Anything longer goes to `<run>/<row#>-<slug>.md`. Rows pass results to each other as paths and SHAs, never as file dumps or full diffs. One commit per mutation row in a git repo: path-scoped, with the `Orch-Attempt:` trailer. Otherwise the result says `sha: none` with the reason, plus the artifact paths. Worker final messages over `returnCapBytes` (engine config) are recorded as `return_over_cap` in `<run>/manifest.jsonl` and flagged by `manifest.js show`; with `returnCapMode: block-once` the worker is sent back once to shorten it.

5. **Ledger.** See "Attempt IDs and the ledger" below.

6. **Track the plan.** Keep the PLAN.md Status table current, one line per row prefixed `[model/effort]`. If a work-state tool is available, mirror the rows there.

7. **Waves.** See "Waves and integration" below.

8. **Progress view.** Whenever the active task or its activity changes, including row completion and wave launch, see "Progress view" below.

9. **Patch on surprises.** If a result changes the plan, patch only the affected rows in PLAN.md and ask before continuing, with the patched rows in the recommended option. While `<run>/DECISIONS.md` says `Mode: away` (set by `/boss:away`), decide within that skill's limits and log the decision there. The answer that approves a patch with new rows also widens the cap, with no second question: `node "$BOSS_LIB/../wfp/wfp.js" set maxLaunches=<current cap + 2 x new rows> --sid=$WFP_SID` (the current cap is `maxLaunches` in `wfp.js status`). In away mode the away rule in step 1 applies instead.

10. **Advisor checkpoint (optional).** When an advisor tool is available, consult it before the plan-approval question, especially on plans of 8 rows or fewer that get no plan check. It is not a row and gets no launch or ledger line. Log advice that changes the plan in `<run>/DECISIONS.md`.

### The five-part brief

Every worker brief has five parts:

- **(a)** "Read `WORKER.md` first", with its absolute path and the absolute path of every file `node "$BOSS_LIB/paths.js" worker <role>` lists for the row's agent (`builder`, `reviewer`, `scout`, `planner` or `plan-checker`: the shared file, that role's rule files, and any local house-rules file). Then the row's context pack from the plan, earlier rows' result paths and SHAs, and any project rules the worker file doesn't cover. Builder and scout rows load no CLAUDE.md and no memory index (`omitClaudeMd: true`), so their brief names the target repo's CLAUDE.md, names the memory index (`MEMORY.md`, absolute path) on UI, site and dashboard rows, and carries the task rules those files hold that the worker files don't. Also pass the absolute `$BOSS_LIB` path and the runs dir, because workers do not inherit them. A design round asks for several variants in one round, each a full layout, shown together in one preview; a design brief carries the owner's prior rulings on that design.
- **(b)** The exact scope.
- **(c)** "Do only this; nothing outside scope."
- **(d)** The return format from `WORKER.md`.
- **(e)** "These instructions override your agent type's defaults."

### Attempt IDs and the ledger

An attempt ID is `<run>#<row>.<n>`: the run folder name, `#`, the plan row (digits plus an optional letter), `.`, and that row's attempt number from 1. Only `manifest.js launch` allocates it.

1. Before each Agent call, run `node "$BOSS_LIB/manifest.js" launch --run <run> --row <n> --role <role> --agent boss:orch-<type> --model <m> --effort <e> --mode <mode> --repo <repo> --area <area> [--git <repo path>] --sid=$WFP_SID`. Line 1 of its output is the attempt ID.
2. Start the Agent description with `[<model>/<effort>] <attempt-id> <subtask>`, for example `[opus/medium] <run>#3.1 Build the form`. Put the line `Orch-Attempt: <attempt-id>` in the brief. If a hook denies the Agent call, fix the call and send it again with the same attempt ID; running `launch` again returns the same ID.
3. The hooks write the attempt's ledger line from its completion notice. Never run `ledger.js add` for it.
4. Close each attempt once its outcome is known: `node "$BOSS_LIB/ledger.js" close --attempt <attempt-id> --first-pass y|n --outcome accepted|retry|blocked|dropped [--cause owner-tune|review-findings|failed|tooling]` (first-pass, cause and outcome in one call; add `--retries N --escalated y|n` when known). `close` requires `--cause` with `--first-pass n` or `--outcome retry`, and refuses it with `--first-pass y` unless it is `owner-tune`. The old form `ledger.js update --attempt <attempt-id> --first-pass y|n [--retries N --escalated y|n]` is still accepted. Without `--cause`, a retry launch, `update --first-pass n` and `add --first-pass n` (or of a retry line) record `unstated` and print one warning; nothing is refused for it.
5. Before a SendMessage follow-up on a finished attempt, run `manifest.js launch … --continue <attempt-id> --cause review-findings` (or `--tune` for a user taste round; see "Escalation by cause") and start the message with the new `Orch-Attempt:` line. A fresh retry (attempt n>1) passes its `--cause` too; it sets an unset predecessor first-pass to n by itself, so that needs no separate record. `launch --continue` refuses until the predecessor's notice is recorded. The continuation's ledger line keeps the tokens its notice reported; they include the predecessor's context, so the row's token sum counts that shared context twice.
6. Workflow and background-shell rows send no Agent completion notice, so they keep a manual `ledger.js add`. Values `add` accepts for them: `--mode workflow` or `--mode background-bash`; `--model council` or `--model council-<N>seat` for a council run (with `--mode background-bash`; effort `unknown` or the chair's), `codex` / `gpt-*` for Codex. Council and Codex rows get their own section in `ledger.js show`, never ranked against Claude. A launch that allocated an attempt ID but never ran (a row-number collision) is closed out with `manifest.js void --attempt <id>`; reconcile and the first-pass reports then skip it. An Agent call made without an attempt ID is recorded `unplanned` with no ledger line; add its line by hand.

`node "$BOSS_LIB/ledger.js" sync` rebuilds the metrics database from the run folders, and `ledger.js report` gives first-pass and retry rates. `/boss:cost <run>` shows a run's usage per row.

A diagnosis row's subtask starts with `Diagnose …`; its ledger role is `diagnosis`.

### Waves and integration

- Rows in the same wave may run in parallel. Never let two agents edit the same files in one wave; use `isolation: "worktree"` when parallel rows touch the same repo.
- Approving the plan approves its waves, so a wave needs no question of its own.
- After any wave whose rows ran in worktrees, a serial `orch-builder` integration row merges the wave's branches into the target branch with `git merge --no-ff` (never rebase), runs the combined tests and records the merged SHA. The final review checks that SHA. When the repo has a remote, an integration or release brief starts with `git fetch` and a merge check of the target branch against origin.
- Each worktree row's pack names its base SHA and worktree path. A worker whose HEAD is not on that base stops and says so.
- One subagent at a time unless the approved plan puts rows in the same wave. Keep each unit small enough to finish in a few minutes.

## Presets and roles

Model and effort come from the active usage preset, not from this file. Print the role table with `node "$BOSS_LIB/../wfp/wfp.js" profile show`. Presets are `economy`, `balanced`, `quality` and `max`; `wfp.js profile <name>` sets one for this session and `wfp.js default <name>` sets a lasting default in your data-dir `wfp.json`.

Roles: planning, plan-check, discovery (`orch-scout`), implementation (`orch-builder`), diagnosis, review. On every Agent call pass both `model` and `effort` from that table or from the approved plan row. Each value is the default and the minimum (the bar): go above it with a reason in Why; go below only on clearly mechanical rows, and never where the preset forbids it. `manifest.js launch` warns when a row goes above the bar with no `--why`.

An agent type maps to its base role. A diagnosis `orch-scout` row passes `diagnosis`, not `orch-scout`.

Make model and effort visible: start every Agent description with `[model/effort]`.

## Escalation by cause

The ladder is medium, then high, then xhigh, one step per row. Get the next step from `node "$BOSS_LIB/../wfp/wfp.js" escalate <role> [<model>/<effort>]`, passing the row's role and the step that ran, for example `escalate implementation sonnet/high`.

- **Findings on a sound approach** (a review found fixable problems, the direction is right): continue the same builder with `manifest.js launch … --continue <id> --cause review-findings`, then SendMessage. Same model and effort.
- **Failed, weak or wrong-approach output** (it missed its done-when, was shallow, needed heavy patching, or took the wrong approach): a fresh agent one step up, launched with `--cause failed`. A continuation keeps its model and effort and carries the failed attempt's assumptions, so it is the wrong tool here.
- **A user taste round** (the user asks to change output that met its done-when): `--continue <id> --tune` records `owner-tune` and keeps the predecessor's first-pass. **A tooling fault** (a tool, hook or harness failed, not the worker): relaunch with `--cause tooling`.
- The retry's launch sets the failed attempt's first-pass to n with its cause, and the retry's ledger line gets `escalated` from its step-up, so neither needs a hand-made record; `ledger.js close --attempt <id> --first-pass n --outcome retry --cause <same>` adds the outcome. A failure at the stepped-up level is blocked and goes to the user.
- Never re-run a repeated failure without new evidence: rescope or use a stronger model.

### Review sizing (configurable default)

One independent review at the end of a run, at the preset's review bar. A run of 8 rows or fewer gets one review at a mid-tier bar; a larger run, or work that ships, deploys, migrates or touches data, gets the stronger bar. Before a review of work that ships starts, the reviewer runs `git fetch` and a merge check of the tested SHA against origin (when the repo has a remote). A re-review after fixes, and any loop-pass review, is scoped to the prior findings and the fix diff only, at the cheaper bar.

## Lease, resume and rollover

Each run has its own lease at `<data>/leases/<run>.json`, so two sessions never drive one run at once.

- `node "$BOSS_LIB/manifest.js" lease acquire --run <run> --sid=$WFP_SID` when a run starts or resumes (`launch` also takes it when nobody holds it).
- `lease renew --sid=$WFP_SID` (launches and prompts also renew it), `lease check --run <run> --sid=$WFP_SID`, `lease release`.
- Lease commands take the run folder name, never a path.
- If another live session holds the run, `acquire` and `launch` refuse (exit 3). Ask the user before `--force`.

When context runs long and nothing is running (`/boss:stop` does these steps):

1. Run `node "$BOSS_LIB/manifest.js" reconcile --run <run> --sid=$WFP_SID`. Every attempt that is not `done` with `next: advance` goes in HANDOFF's running table. A `next: gate` row goes there with its status (for example `gaps_found`) in Notes, and its dependents stay blocked.
2. Write `<run>/HANDOFF.md` from the handoff template (5 KB or less).
3. Run `node "$BOSS_LIB/manifest.js" lease release --sid=$WFP_SID`; it frees every lease this session holds.
4. Tell the user to clear the chat and run `/boss:resume <run name>` (HANDOFF's `run:`, never a path). The command loads HANDOFF.md, the plan's Status table and any work-state list, re-acquires the lease, reconciles running rows and continues. Without a name it lists resumable runs and asks; it never picks one itself (`/boss:runs` shows the same list).

When the run ends: `node "$BOSS_LIB/manifest.js" lease release --run <run> --sid=$WFP_SID`.

A rollover is never a reason to stop on its own. Mention it only as one extra option inside a question already being asked at a real decision point.

## Progress view

Whenever the active task or its activity changes, including row completion and wave launch, run `node "$BOSS_LIB/../skills/status/progress.js" <run>` and reproduce its output verbatim. The shared formatter prints a compact meter: matching W/T badges, cumulative wave progress above current-wave task progress, and three visible ☑/➜/⮡ summaries for completed, current and upcoming tasks. The bars are always first; do not prepend a completion paragraph. A run without waves has one T row. Preserve the normal DETAILS file link, which opens the full readable task report and lets a native chat plugin enhance it. Never print hidden payloads or HTML markers. Do not hand-space or reformat it as inline-code fragments or a Markdown table. Missing tokens stay unknown and reporting completeness belongs in Details; reported tokens are not billing figures. Do not add speaker/status headings, numeric fractions or percentages, token-unit labels, time-unit suffixes, column-divider glyphs, or usage asterisks. The meter uses equal-width tracks with full-size item ticks and blank unused positions, compact uppercase usage figures, and aligned clocks with dim leading zeros. W is hidden for a single wave. Approximate current-wave and job finish times show beside T/W (for example ~3:45PM); the next-reviewable-result forecast belongs in Details. Recorded owner waits are excluded from elapsed time. Both clocks use MM:SS until either reaches an hour, then both use HH:MM:SS. Live views update; posted chat reports keep their recorded values.

Keep the formatter's ☑/➜/⮡ summaries and attention text. DONE shows work completed since the previous report; NOW shows every active task with short name and activity; NEXT includes recorded waiting reasons. Use the optional Short name, Activity, Estimate, Depends on, Reviewable and Instructions plan columns to supply those facts, and update Activity at each task change. Short name is brief plain language (at most 64 characters and 11 words); keep routes, code identifiers and full instructions in Details. Rows without a readable short name are omitted from the compact report, never clipped. Never invent an activity or a reviewable checkpoint. The completed row uses recorded successes only; empty completion groups are omitted. Add findings as concise bullets after the DETAILS link; the plugin places them inside expanded Details. Critical decisions and blockers remain explicit attention items. Do not add duplicate task strips, tables or next-up lists. `/boss:status <run>` shows this report; `--details` shows the task strip, table, results and next up; `--full` shows the per-row ledger table. Plan patches and decision points still show the relevant detail.

## Orchestrator guard

A soft PreToolUse hook counts the main chat's own file and shell calls. When 6 land in a row (a pause of 3 minutes or more starts a new count), it adds one reminder to consider delegating the rest. It never blocks. Never counted: subagent calls, read-only git one-liners, one-file `cat`/`head`/`wc`, `ls`, memory paths, writes into run folders and the data dir, reads under the plugin, and calls of `wfp.js`, `ledger.js` and `manifest.js`. Add path prefixes to `guard.exemptPaths` in your data-dir `config.json` to exempt more. Inline workflows that legitimately do many edits can silence it with `node "$BOSS_LIB/../hooks/orchestrator-guard.js" off` and re-arm it with `on`; `ORCHESTRATOR_GUARD=off` in the environment disables it. `/boss:doctor` checks the hook install.

## Configuration summary

| Default in this file | Where to change it |
|---|---|
| Planning gate (8 rows) and review sizing | Your CLAUDE.md, or the plan the main chat proposes |
| Model and effort bars, escalation ladder | wfp presets: `<data>/wfp.json` overlays the plugin's default |
| Return cap, lease TTL, pass and gate statuses | `<data>/config.json` overlays `lib/defaults.json` |
| Guard exemptions | `guard.exemptPaths` in `<data>/config.json` |
| Worker house rules (git identity, ports, tools) | `<data>/WORKER.local.md`, appended after the base worker file |
