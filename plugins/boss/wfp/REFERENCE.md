# wfp reference (loaded on demand)

## Parts
| File | Role |
|---|---|
| wfp/wfp.js | hooks + CLI (`status`, `profile`, `profile show`, `default`, `ultracode`, `escalate`, `set`, `reset`, `handoff`, `json`) |
| wfp/config.default.json | shipped defaults (replaced on update, do not edit): thresholds, cooldowns, caps, `typeRoles` + role regexes, presets (`profiles.<name>.roles.<role>` = `{model, effort}`, `belowBarAllowed`), `ultracodeRaise`, `defaultProfile` (null) |
| `<data>/wfp.json` | your overrides, read on top of the defaults (top-level keys replace, object values such as `profiles` and `typeRoles` merge one level deep: a profile you define replaces the shipped profile of that name). `wfp.js default` writes `defaultProfile` here |
| lib/ledger.js | per-run token/outcome ledger (not part of wfp; `node "$BOSS_LIB/ledger.js" help`) |
| `<data>/wfp/state/<sid>.json` | session policy + counters (atomic, locked); `<sid>.tel.json` telemetry; purged after `stateTtlDays`. `WFP_STATE_DIR` overrides the directory. Handoffs go to `<data>/wfp/handoffs`, log lines to `<data>/errors.log` |
| `<data>/wfp/denials.jsonl` | one JSON line per launch deny and per loop-guard pass: `{ts, sid, tool, hook, key, agent, decision, reason}` (`key` = main, launch, name, conc, opus, model, preflight; capped at 256 KB). `jq -r .key <data>/wfp/denials.jsonl \| sort \| uniq -c` shows which rule fires most |
| hooks/hooks.json | PreToolUse(Agent), SubagentStart/Stop, UserPromptSubmit, SessionStart entries that call `wfp.js` |
| statusline (optional) | pipe the statusline JSON to `wfp.js telemetry` (detached) for fresher context numbers; without it wfp falls back to a bounded transcript read |

## Enforced (deterministic, local)
- Agent launch: a launch with no model, or with an explicit model at or below the bar and no effort, is corrected to the role's bar (`model=… effort=…`; F-09). When the effort is missing and the description starts with a `[model/effort]` label naming the model that will run, a label effort above the bar's effort is kept instead of the bar's (never below the bar; an explicit `effort` and a session `set effort.<role>` win over it; an above-bar model is left alone). config `correctionMode` picks how: `"updatedInput"` (shipped default; F-28; the call is rewritten in place with every field kept and no `permissionDecision`, so no round trip) or `"deny"` (fallback; deny-with-correction, relaunch once, no allow-decision is ever returned). A named call (`tool_input.name`, which spawns a teammate; A-06) is denied on every retry unless the session has `set agentTeam=true`. Launch cap, concurrency cap, main-only deny, preflight-required (once; skipped when a lasting default applies; its text puts the preflight table in the AskUserQuestion option `preview` or descriptions, never as text before the call or as a plain-text question, F-03). Session-only opt-ins: `set effort.<role>=<lvl>` (a launch without effort is corrected; so is any launch without effort whose model is at or below the bar), `set askBeforeOpus=true` (Opus above the bar: ask once, then capped). No preset sets either.
- Never denied for its model: a launch at or above the bar (any higher model or effort, escalations, justified `max` rows).
- Hard denies (D8): main-only and the launch cap deny on every retry; the loop guard never lets them through. Only the owner lifts them: `set mode=delegate` (main-only), `set maxLaunches=N` (cap), or `set override=true` (both), each with `--sid=$WFP_SID`.
- Loop guard: the model-fill, concurrency and ask-before-Opus denies (keys `model`, `conc`, `opus`) give way after the same deny repeats more than `denyStreakLimit` times in a row. The launch that goes through carries an `additionalContext` note naming the key, and `errors.log` gets one line (A-15). `override` lifts caps/warnings for the session.
- In-chat setters (UserPromptSubmit; main-only, max concurrent, launch cap, override): only the owner's own words apply (F-04). `<task-notification>` blocks are stripped first (anywhere in the prompt; an unterminated one is stripped to the end), and the reassess check reads the same stripped text. A setter match is skipped when it sits in quotes (`"…"`, `'…'`, `“…”`), backticks or a code fence; when not / never / cannot / n't / no longer falls within the 3 words before it; when its sentence ends with `?`; for the two count setters, when the sentence is a past-tense recap (last week, in the old plan, we had …); for override, when scoped to a topic (`about …`). The first surviving match applies. Phrases and cases: `fixtures/run.sh` §21.
- Effective model is *observed* at SubagentStop from the agent transcript (`/wfp status` shows requested → policy → effective).

## Lasting default
- `defaultProfile` (shipped as `null`; set it in `<data>/wfp.json`): a session with no policy gets that profile on its first Agent launch (also as the base for `set` / in-chat changes), no preflight. `null`/missing = per-session preflight as before.
- View/change: `node "$BOSS_LIB/../wfp/wfp.js" default [<profile>|none]` (read-modify-write of `<data>/wfp.json`; it refuses to touch a file that does not parse). A session-level `profile`/`set` still overrides it.
- Fixtures (`fixtures/run.sh`) run against a scratch data dir with `defaultProfile=null`, so a user's default never changes test results.

## Presets = quality bar
- `profiles.<name>.roles.<role>` = `{model, effort}` for planning, plan-check, discovery, implementation, diagnosis, review, default: the default AND the minimum. Presets: economy, balanced, quality, max. Show: `wfp.js profile show [<name>] [--ultracode=on|off]` (works without a session: falls back to `defaultProfile`).
- Above the bar: always allowed, silent (the plan gives the Why). Below the bar: one advisory note (`additionalContext`, never blocks; deduped per role+step by `warnCooldownSec`, at most `barNoteMaxPerSession`, silent with `override`). It says "mechanical rows only" when the role is in the preset's `belowBarAllowed`, "never" otherwise (`max`: planning, review). Not judged: role `default` (unclassified), `fork`, unknown model names, a launch without effort is judged on its model only.
- Role of a launch: `typeRoles` exact base agent type first (a plugin prefix such as `boss:` is stripped; orch-planner/plan-checker/scout/builder/reviewer; orch-scout → diagnosis when the description says diagnose/debug/root cause), then the `roles` regexes over type + description (first match), else `default`.
- `wfp.js ultracode on|off`: session flag kept outside the policy (survives `profile`, cleared by `reset`); raises the bar one step via `ultracodeRaise` (economy→balanced→quality→max, max stays). It only mirrors the user's ultracode switch; the user turns ultracode on themselves.
- `wfp.js escalate <role|agent-type> [<model>/<effort>] [--json]`: the one retry step for a failed row: a stronger model at the same effort; at opus, one effort level up; a below-bar row goes back to the bar; above opus/max = blocked → owner.
- Default bars: the quality preset runs planning, plan-check, review and default at opus/medium, diagnosis at sonnet/medium, implementation at sonnet/high and discovery at haiku/medium. Creation rows and above-bar rows (a mode C plan, a pre-ship review) are the planner's call and carry a Why. At opus the ladder is medium → high → xhigh (below opus: a stronger model at the same effort, e.g. sonnet/high → opus/high). The orchestrator may also step up a weak-but-not-failed row (one step-up per row either way), recorded `ledger.js update --first-pass n --escalated y`.
- Fixtures also pin `profiles.quality.roles` in their scratch overlay, so tuning the shipped presets never changes the legacy bar-logic results.
- Presets are read from config by name at use time, so edits to `<data>/wfp.json` reach the next hook call of an open session. Legacy policies (old `roleModels` shape): an un-customized copy of a preset is replaced by the live preset; a `+custom` one keeps its model map as explicit overrides. `set role.<role>=` / `set effort.<role>=` overlay the preset (hyphenated roles parse).

## Advisory
- Context checkpoints (UserPromptSubmit one-liner; level + cooldown + growth deduped), continue/compact/clear judgments, preflight content, failure-pattern advice.
- Telemetry is "unknown" if absent, "STALE" if older than `telemetryFreshSec`. Subscription % only if the statusline JSON reports it. Cost is API-equivalent, not the subscription bill.

## Compact vs clear vs separate chat
- /compact: same session; history replaced by a summary (costs one summarization call; keeps policy, plans, decisions roughly). Use when much work remains and old detail is reducible.
- /clear: same terminal, new empty session; nothing carries but files/handoff. Use when the next step is independent or context is mostly noise.
- Separate chat: new session, old one stays open for reference; use for an independent task or a parallel line.
- None restores the account allowance; they only change what each next turn re-reads (cache reads are cheap, a cold rebuild is not). Near completion, continuing is usually cheaper than rebuilding.

## Version limits
- SubagentStart/Stop carry no model; effective model comes from the transcript.
- Correction cost: in `deny` mode a corrected launch costs one extra round trip; `updatedInput` removes it. The docs do not say whether `updatedInput` without a `permissionDecision` is applied so it was probed live: a launch with neither model nor effort was rewritten in place and the agent's meta.json showed haiku/medium. The config therefore ships `correctionMode: "updatedInput"`; set it to `deny` in `<data>/wfp.json` if a future Claude Code version stops applying the rewrite. The fixtures pin `deny` in their own overlay and set `updatedInput` per case, so they pass whichever mode ships. A launch above the bar, or with an unranked model name, is never corrected for effort. Roles for types without a `typeRoles` entry come from `subagent_type` alone (A-08); only `byDescription` reads the description.
- PreToolUse `additionalContext` (the below-bar note) reaches Claude next to the tool result (hooks docs, PreToolUse decision control).
- Workflow-plugin agents that bypass the Agent tool are not seen by the hook.
- Hooks load at session start; sessions already open need a restart/`/hooks` reload to pick up wfp.

## Disable
- Disable the hooks: disable the plugin (`/plugin`), or set `override=true` for one session. State under `<data>/wfp/state` is plain JSON and safe to delete.
- Tests: `bash wfp/fixtures/run.sh` (`WFP_SRC=<plugin root>` tests a candidate copy; exit 0 = all pass).
