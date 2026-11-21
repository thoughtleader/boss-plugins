# Boss meter

Part of the `boss` plugin (`plugins/boss/meter/`). Its mod entry point is the committed bundle
`hooks/meter-register.js`, named under `modules` in `hooks/hooks.json`.

Native Claude Code rendering for compact progress **inside chat messages**.
The bar, elapsed time and reported-token totals use the same `meter/meter.mjs`
model as the plain-text and Board views. `/boss-meter` also opens an optional
live pane; `/boss-meter orchestrator/<run-id>` opens a specific reported run.
The plugin is `boss`; the command and pane ID are `boss-meter`. Claude Code adds the plugin's registry name and the word
"plugin" to native status attribution, so this version shortens that label
to "boss plugin". The status API cannot remove the host's attribution.

Progress reports can appear anywhere **inside an assistant reply**, with prose
before and after them. The CLI writes an immutable pair under the configured
Boss `reports/` directory: a human-readable Markdown report and a companion
JSON snapshot. Chat shows only a readable fenced meter, ☑/➜/⮡ summaries, and
an ordinary `[DETAILS](file:///.../reports/<hash>.md)` link. Without the mod,
the report remains readable and the link opens the complete Markdown report.
There are no hidden HTML comments or encoded payloads in new output.

The mod reads only the matching JSON companion under that configured report
root, limited to 512 KiB. It validates `boss.report/1`, the normalized meter and
tasks, and an exact match between the stored report text and the text directly
before its link. Bars always lead, then completed/current/upcoming status rows.
Surrounding prose appears as bullets inside expanded DETAILS through native
Markdown using the original strings; existing lists receive no extra bullet.
Presentation checks recognize the report structure without interpreting tick
counts, clock formats, usage columns, or ETA wording. Unrecognized, malformed,
missing, or mismatched reports
remain ordinary Markdown. Valid old v3 payload comments are removed, including
line-wrapped payloads, while their readable report and surrounding prose remain.

The report's bars, clocks, ☑/➜/⮡, task outcomes and provenance are frozen at
publication. DETAILS expands current tasks, queued work, completed outcomes,
model/effort, result references and usage provenance from that same snapshot.
It never substitutes a live run's newer figures. This does not open a pane.
`encodeInlineMessage` remains a compatibility export for plain reports with a
`/boss:status <run> --details` footer; it never emits encoded comments.

The default is the unique active run owned by the current session. With no
exact owner match, or several matching runs, the pane asks you to pick a run.
Opening another session's run does not make it the current session's status.
The reader never changes the registry, launches work, sends messages, approves
gates, or reloads another session.

The matching W and T badges label overall waves and current-wave tasks.
W shows completed waves with cumulative elapsed time and usage; T
shows completion within the current wave with that wave's elapsed time and
usage. Both use shared-width Box columns, paired `MM:SS` clocks that switch to
`HH:MM:SS` when either reaches one hour, and compact `K`
usage figures. Runs without wave groups show one Tasks row. Narrow panes
place each bar above its clock and usage pair.
The Details button shows the task states and usage explanation. Token values
are reported ledger/snapshot figures; Details explains any missing active or
unknown attempt usage. They are not a billing estimate.

VS Code maps numeric Box widths and horizontal gaps to `ch`, vertical gaps to
`lh`, and inherits the chat font (Text `mono`/`tabular` are ignored there).
Metric rows explicitly disable the host's default flex wrapping. The layout
reserves a five-`ch` badge column and two `ch` per bar segment so proportional
chat fonts do not cut off labels or progress glyphs. Graphical badges use
matching three-`ch` widths and 1.2-`lh` heights; terminals use `[W]` and `[T]`. Both bars use twelve
proportional segments, or eight on narrow surfaces. Bars and labels never
ellipsize. Only unfilled ticks and the leading zero prefix of clocks are dimmed.
The uppercase DETAILS control sits half a line below the meter. VS Code fixes
native buttons to the inherited chat font; its mod API does not support a
smaller button font, so the preview preserves that actual limitation.
Inline layout reserves four columns because the reported transcript viewport is shared with
the above-prompt band, rather than measured at each message. These semantics
were checked against the installed VS Code extension 2.1.322; the preview
uses the actual element tree and those styles, not Board CSS.

## Build and verify

The model is `meter.mjs`, `reporting.mjs`, `reports.mjs` and `board.mjs`/`ack.mjs`/`artifacts.mjs` in this
folder; the mod source is `mod/src/`, which imports the model by relative path. `mod/build.mjs` bundles it
into `hooks/meter-register.js`. esbuild is this folder's own devDependency:

```sh
cd plugins/boss/meter && npm ci          # once
node mod/build.mjs                       # writes ../hooks/meter-register.js
bash ../test/meter-tests.sh              # meter and mod node --test suites
claude plugin validate ../              # the whole boss plugin
claude plugin test ../                   # runs mod/tests/native.test.ts (needs the engine)
```

`hooks/meter-register.js` is generated and committed (marketplace installs run no build step).
Build after changing the model or any file under `mod/src/`; `mod/tests/bundle.test.mjs` fails when the
committed bundle differs from a fresh build. The engine tests exercise terminal and VS Code element
trees, mixed prose around an artifact report, frozen task details, and the run picker. They validate
the native API, not the host's pixels.

`skills/status/compact.js` (`/boss:status`) imports the model from this folder; set `BOSS_METER_DIR` to
point it elsewhere.

## Load

The mod loads with the `boss` plugin; there is nothing separate to install. To try a working tree in a
new session without disturbing active work:

```sh
claude --plugin-dir /absolute/path/to/boss-plugins/plugins/boss
```

Then run `/boss-meter`. An already open session can pick up an installed plugin with `/reload-plugins`
at a suitable stopping point. Do not enable a second plugin that also registers `/boss-meter`.
The installed Claude Code must support mods. The VS Code drawing surface
requires extension 2.1.299 or later (2.1.304 without feature flags).

Options: `progress_dir` defaults to `$BOSS_PROGRESS_DIR` or `~/.claude/boss`;
`poll_seconds` defaults to 3. There is no application server or background
process: the session's own mod timer reads the reports.

References: [mod UI](https://code.claude.com/docs/en/plugins/mods/interface),
[elements](https://code.claude.com/docs/en/plugins/mods/reference#elements).
