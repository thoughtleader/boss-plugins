# Compact progress reporting

The same meter model drives the Boss Board, `/boss:status <run>`, and the
`boss` Claude Code plugin. Matching square W/T badges identify completed waves and tasks in the current
wave. W carries cumulative run time/usage; T carries the current wave's figures. Both numeric columns align, with
paired clocks and compact values such as 64.4K. Speaker/status headings, numeric
counts, percentages, unit labels, divider glyphs and usage asterisks are omitted. A run without waves has one
T row. ☑ shows tasks completed since the previous published report; ➜ lists every active task with short name and recorded activity; ⮡ lists upcoming tasks and recorded dependency reasons. Only successful tasks receive ☑; empty completion groups are omitted. ➜ identifies current work; ⮡ identifies upcoming work. Both
arrows are blue in native views. All three status markers share the W/T badge
footprint and column width so both symbols and task text stay aligned. Plain-text reports use the same glyphs without color. DETAILS
expands short task summaries with timing, usage and model. Individual task controls reveal instructions, notes and outcomes.

## Use

- `/boss:status <run>` posts an inline snapshot in chat. Without the plugin,
  the report remains a readable aligned code block with ☑/➜/⮡ and a normal
  DETAILS file link to a full Markdown task report.
- `/boss:status <run> --details` shows the former task report and results.
- `/boss:status <run> --full` shows the full ledger table.
- `/boss-meter` opens a live native pane. An exact, unique owner-session match
  selects the active run; otherwise choose one. Inspecting another run does
  not replace this session's status.

Existing Claude sessions can load the installed plugin with `/reload-plugins`
when convenient; new sessions load it normally. The Board extension update
may require a normal VS Code reload. Installation does not force either reload.

## What the figures mean

- One visible tick represents each actual task or wave. Unused positions are blank;
  the bar track remains allocated. The track uses available width; when it fills, earlier completed ticks are omitted before upcoming ticks. Tick size stays fixed and the bar stays on one line.
  Single-wave jobs omit W. Until a second wave begins, W's timer is invisible but
  its full column stays allocated. Filled-square 🆆/🆃 are used in native and text views.
- Time is elapsed wall time, not summed agent time. When all work waits for owner
  input, clocks freeze and recorded waiting intervals are excluded after resume.
  Waiting time is shown separately in Details. An active task keeps clocks running.
  Both clocks switch to HH:MM:SS together at one hour.
- Current-wave and job forecasts sit beside their respective T and W figures when space allows; otherwise
  only the ETA moves to the next line. While waiting, they show remaining work time
  such as “~12 min after you respond.” Original forecasts remain in Details.
- Estimates are explicitly approximate: recorded task estimates take precedence,
  then the median of completed task durations, then a disclosed five-minute/task
  initial heuristic. Independent tasks within a wave are assumed parallel; waves
  run sequentially, and recorded dependencies extend the critical path. Explicit
  reviewable tasks and named review/result tasks identify the next checkpoint;
  without those, the next-wave result is labeled estimated. No calibration claim.
- A changed task/activity report records a slipped forecast, reason and revised ETA.
  The original job forecast never moves. Historical chat reports remain immutable.
- Tokens are reported attempt/ledger usage, including retries, deduplicated by
  attempt identity. They are not a bill or context-window occupancy. Unknown
  usage is distinct from zero; reporting completeness is explained in Details.
- Chat reports preserve their posted values. They do not update historical
  messages to current values. Details exposes the reporting basis.
- Attention messages remain visible. Routine rows and provenance stay behind
  Details or the explicit detail commands.
- The native DETAILS control is uppercase and spaced below the meter. Claude's
  current VS Code mod renderer fixes Button font size; the plugin cannot shrink it.

## Implementation

`meter.mjs` owns calculations and the fixed-width fallback. `mod/src/layout.mjs`
uses native Box columns for chat and the optional pane. The Boss VS Code extension's board view
uses a shared CSS grid for the Board. `skills/status/compact.js` keeps run state read-only:
it folds a stale or missing snapshot in memory with the existing publisher,
without repairing caches, taking leases, or launching work. It writes immutable artifacts in `boss/reports` plus a presentation-only cursor in `boss/report-state/<tool>/<run>.json` for original forecasts, completion boundaries and wait history. JSON mode remains read-only. The native pane reads that history and maintains its own live view in memory; it never writes run data.

The chat transport uses a normal `[DETAILS](file:///.../boss/reports/<hash>.md)`
link. There are no encoded payloads or HTML markers. The native plugin reads
only the companion JSON under the configured reports folder, validates its
schema and exact visible report text, and preserves surrounding Markdown prose.
The bars always lead, followed by completed/current/upcoming status rows.
Surrounding assistant prose is preserved as bullets only inside expanded Details;
existing Markdown lists, code and links retain their contents. Critical attention
remains visible after the status rows. Collapsed summaries and expanded details retain their posted values. The
plain link opens the readable Markdown report if native rendering is absent.
If artifact storage fails, the complete visible report and a detail command
remain available.

The integration regression exercises actual CLI output surrounded by ordinary
prose, the saved artifact, native lookup, and the readable fallback. Native
engine tests exercise task expansion; previews alone are not host verification.

## Verification and previews

```sh
bash plugins/boss/test/meter-tests.sh
node plugins/boss/meter/mod/build.mjs
claude plugin validate plugins/boss
claude plugin test plugins/boss
```

The Boss VS Code extension (not part of this plugin) has its own build, tests and a preview harness that
serves isolated fixtures only.
The Board preview is at `/`; the inline chat preview is at
`/harness/inline-meter.html`. The latter renders the production native element
tree through a browser adapter. These are fixture previews, not captures of a
live Claude conversation. Native engine tests check terminal and VS Code trees
and interactions; browser checks cover alignment, narrow widths and clocks.

## Optional source fields

PLAN tables may supply `Short name`, `Activity`, `Estimate` (minutes unless a unit is given),
`Depends on` (row IDs), `Reviewable` (yes/no), and `Instructions`. They affect presentation
only. Update Activity when the active work changes; do not invent activity from a ticking
clock. Full Subtask text remains available as instructions when no separate text is given.
Snapshots can provide the corresponding `short_label`, `activity`, `estimate_ms`,
`depends_on`, `reviewable`, `instructions`, and `reporting.waits` fields directly.

Phone-specific layout changes are deferred by the owner.

## Compact task copy

Visible rows require a brief plain-language label (at most 64 characters and 11 words).
Use `Short name` for an authored summary. Long instruction prose, route templates,
code identifiers and clipped/ellipsis labels remain in Details and the saved Markdown
report instead of appearing as compact rows. DONE shows only the name; current activity
and waiting reasons appear only when they keep the complete row concise (96 characters).
Text wraps in a constrained flexible column. Empty DONE groups are omitted.

When no previous report cursor exists, existing completions establish the initial
boundary; they are not replayed as new. Subsequent reports show new completions once.

Compact forecasts show only the approximation mark and time (`~3:45PM`), with no Job, Result, or ETA prefix. Details keeps the forecast descriptions.

## Forecast scope

W forecasts the whole job. T forecasts completion of the tasks in the displayed current
wave, including recorded dependencies within that wave. The next-reviewable-result
forecast lives in Details alongside its preserved original forecast. A single-wave
job has one visible forecast. Older immutable snapshots lacking a current-wave forecast
do not display their checkpoint forecast next to T; their reviewable forecast stays in Details.

## Tick window

Native inline reports fill their available container, without the old 100-column cap.
Both meter rows share a track width calculated from available columns after reserving
clocks, usage and forecast space. When a track fills, oldest completed ticks disappear
first. Visible ticks retain their size and meaning; full counts and tasks remain in
Details. An oversized unfinished queue uses a trailing ellipsis to indicate further
upcoming tasks. Text fallback and pinned status use the same windowing rule.
