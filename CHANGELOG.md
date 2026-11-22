# Changelog

## 0.4.4 (2026-11-22)

### Added
- `ledger.js close` can also update the plan: `--plan-status` writes the row's Status cell (with the model, attempt and first-pass cells), `--plan-result` adds the result cell, and `--decision` appends a line to `DECISIONS.md`; all are opt-in, and every check runs first, so a failed check closes nothing and changes no file.
- `ledger.js promote --attempt <id> --by close|waiting-timeout` turns a provisional waiting notice into a final one.
- Council: `--allow-grounded-root` (on `ask`, `run` and `preflight`, only with `--grounded`) turns the refusal of a risky `--grounded` root into a warning that names what was found.
- Council preflight shows a `routing_pref` row when an OpenRouter seat sets `sort`, `preferred_min_throughput` or `preferred_max_latency`.
- `plugins/boss/test/council-tests.sh --full` runs the whole council suite behind a guard that refuses to run outside a scratch home, with shims in place of the provider tools; the council test runner also blocks connections to anything but the local machine.
- `node <plugin dir>/hooks/agent-tools-patch.js --lift [<file>...]` undoes the opt-in agent-tools patch before a merge: it restores the committed text of each patched agent file and clears its skip-worktree flag, prints one line per file, and refuses (exit 1) any file that has another local change. Only skip-worktree files are lifted (an assume-unchanged file is reported "not patched"). `--lift` exists from 0.4.4 on: an older copy of the script ignores the flag and patches instead. With no file argument it lifts the agents folder next to the script, so to lift another checkout pass that checkout's agent files as full paths. Run the patcher again with no argument after the merge; it puts back only the extra tools the config lists, so `tools:` entries added by hand that the config does not list are not restored.

### Changed
- A notice that says the agent is "waiting on its own background work" is now provisional: the row stays open (reconcile shows `waiting`) until `ledger.js close`, `ledger.js promote --attempt <id>`, or reconcile after `waitingGraceSec` (default 1800 seconds) makes it final.
- `--grounded` refuses a root that is, is inside or is above the run folders or a memory folder, or that holds a link into them, a run-folder-shaped folder or a memory index within 3 levels; a clean root passes with that bound stated, and a link to an outside folder gives an "only partly checked" warning that says either that only the top level of that folder was read or that the folder could not be read.
- The launch cap at a re-approval is the launches so far, plus 2 per pending row, plus 1 per running row (its first launch is already in the launches so far; the 1 is room for its one retry), plus 2 spare.
- Reconcile finds the result file of a lettered row (for example `5a`) and prefers the file whose frontmatter names the attempt.
- The presets tests make their own scratch home when run directly, instead of using the real one.
- Merge and integration guidance (builder agent, orchestrator protocol, README) now says that a clean `git status` does not show skip-worktree or assume-unchanged files: list them with `git ls-files -v | grep '^[Sh]'`, and lift the agent-tools patch first when the branch changes one of them.

### Fixed
- The council's rulings check no longer refuses harmless text such as `risk-checks`, `motion.dev` or `socket.io`; a key at the start of a word or glued to an underscore (`token_sk-...`), and `.dev` or `.io` with a path or a subdomain, are still refused.
- A Codex seat's error items are kept in that seat's detail line instead of being dropped.
- The council preflight replaces line and paragraph separators and bidirectional control characters (including U+200E, U+200F and U+061C) with `?` in the names it prints, so a folder name cannot reorder or hide the text around it.
- The council full-mode test guard now refuses an empty scratch folder instead of accepting any absolute path.
- The progress report shows a row that was sent back to the same agent (`launch --continue`) as running, with its short name and activity, instead of under upcoming with "No active tasks".
- The progress report no longer says a review row "needs your decision" while the fix round for its findings (`gaps_found` or `revise`) is running or has finished; that row waits as queued until it is re-reviewed. When the plan has no `Depends on` column the fix round may be for any row, and a fix that was closed as blocked or dropped does not count. A review that needs a person (`human_needed`) always keeps its line.
- The council's check of a `--grounded` root against the run and memory folders also compares a spelling that differs only in letter case by asking the file system whether it is the same folder.
- The scout agent's description no longer claims it reads memory first.

## 0.4.3 (2026-11-22)

### Fixed
- The council status test no longer flakes on slow macOS runners: it reads the phase order (seats, chair, done) from every status write instead of a 50 ms file poll that could miss the short chair phase.

## 0.4.2 (2026-11-22)

### Changed
- `/boss:start` and `/boss:resume` load about 5 KB less: the Progress view, Escalation and Lease/rollover sections of `skills/start/PROTOCOL.md` moved verbatim into `PROGRESS-VIEW.md`, `ESCALATION.md` and `LEASE-ROLLOVER.md`, which the protocol reads only when it needs them (a progress report, a failed or retried row, a rollover or run end).

## 0.4.1 (2026-11-22)

### Fixed
- CI passes on Ubuntu and macOS (Node 22.x and 24.x): portable test helpers for BSD tools and macOS temp paths, a Python 3.14 argparse test fix, and `/proc`-only tests skipped on macOS with a printed reason.
- `doctor-tests.sh` defines `lacks()`, so six assertions that never ran now run.

### Changed
- README states that process-ownership reporting is Linux only, and that the meter board's macOS `ps` path has no CI coverage yet.
- The macOS CI step that runs `test-all.sh` under `/bin/bash` 3.2 is named for what it checks.

## 0.4.0 (2026-11-21)

### Added
- `manifest.js void` marks an allocated-but-never-launched attempt so status views and `reconcile` skip it.
- Ledger and manifest modes `background-bash` and council rows; a worker's turn-limit stop is recorded as unfinished and `launch --continue` accepts it.
- Council: `[canary] cache_s` skips a repeat canary that already passed (default 600 s); `[providers.codex] disable_user_mcp` turns off the user's codex MCP servers for council seats (default on).
- Config keys `analytics.weeklySync` and `patchPluginAgents`; worker rules split by role (`paths.js worker <role>`).
- `ledger.js report retries`: read-only report of retried attempts grouped by cause, role and model/effort (`--json`; needs no metrics db).
- Refused launches are logged to `wfp/denials.jsonl` in the data dir (fixed fields only, 256 KB cap).
- `scripts/build-export.sh <sha> <out-dir>` builds the public one-commit export of a commit (git archive, scrub, a fail-closed scan of keys, private names, emails, URLs, private IPs and home paths against exact allow-lists, noreply author); `plugins/boss/test/export-tests.sh` tests it; README "Building the public export".
- README sections "Turning parts off" and the data-dir files `sweep-state/` and `wfp/denials.jsonl`.
- Plugin and marketplace manifests carry author, homepage and repository (https://github.com/thoughtleader/boss-plugins); README has GitHub install steps, requirements and platform notes.

### Changed
- `scripts/secret-scan.sh` uses the `grep` on PATH and exits 2 (never 0) when grep is missing or fails and nothing was found; its key-shape pass no longer needs `grep -P`, so it works with BSD grep. `scripts/build-export.sh` also sweeps file and directory names, and a missing or failing tool anywhere in its gate refuses the export.
- Needs-you items for a row that stopped at its turn limit show the turn-limit message instead of the plan status; `manifest.js` help and ledger comments name the real progress state (`waiting_input`).
- Council drift redacts codex `query_params` values and scheme-less hosts (`host:port`, and dotted hosts under address-like keys) the same way as URLs: stored as sha256-8, printed as `<set>`.
- LICENSE holder is Michael Evan.
- Council drift output and stored snapshots no longer show URLs or MCP server settings from the user's codex config.
- `agent-tools-patch` only rewrites the orch agent files when `patchPluginAgents` is `true` in `config.json`.
- The Boss board resolves its paths through `lib/paths.js`, so a fresh install's runs and leases show up.
- Hook startup is faster for subagent guard calls.
- Hook startup: `bin/boss-skip` (portable sh) lets three more hooks skip node when it provably has nothing to do, falling back to node on any doubt or error (a missing helper included): the orchestrator guard once the session's reminder is spent or the guard is off, role-write-guard unless a read-only role name is in the call, and Stop/SubagentStop attempts hooks for a session that holds no lease. About 20 ms less per main-chat Bash/Read/Edit call after the reminder. The Stop/SubagentStop skip saves about 17 ms for a session without a lease (only when `<data>/leases/` exists and is readable; a missing one runs node) and adds about 2-5 ms per Stop/SubagentStop for a lease holder.
- wfp keeps the `[model/effort]` label at the start of an Agent description when the call has no `effort` and the label's effort is above the role bar's (same model only; an explicit `effort` or `set effort.<role>` still wins; never below the bar). Before, a launch labelled `[sonnet/high]` without `effort` was rewritten to the bar's effort.
- The finished-attempt notice and the SessionStart run line are shorter: the close line no longer repeats the flag values, and a run in the runs dir shows its name instead of its full path.
- `/boss:doctor` runs about 7x faster (about 1.8 s to 0.26 s): git lookups are cached per directory, and its skill count is computed.
- The Stop/prompt notice sweep reads only the transcript bytes added since the last sweep (per-session offset in `sweep-state/`, 14-day cleanup); the offset never moves past a notice whose ingest failed.
- `ledger.js report ... --no-sync` writes nothing; a syncing report says when it creates `metrics.db`.
- The compaction-gap hint no longer fires for attempts that already have an outcome.
- SessionStart no longer appends duplicate exports to `CLAUDE_ENV_FILE`.
- Test fixtures and examples use neutral names and synthetic content (a made-up todo app); no personal paths, hosts, repo names or private-project text remain in the tree.
- Portability: `boss-memclean` exits 2 with "Linux and WSL only" on any other OS (it needs bash 4, `/proc`, `free`, `ss` and `ps etimes`); `secret-scan.sh --leftover` exits 2 off Linux/WSL instead of reporting "nothing found", and a `realpath` failure is an error, not a silently dropped directory; the council supervisor and reaper list a process group through `ps` when there is no `/proc`, and the reaper never reads an unlistable group as empty; the meter board shows live sessions without `/proc` (pid answers, `ps` start time).
- The council entry scripts (`council.py`, `tools/council-probe.py`, `tools/liveness-probe.py`) start with a `#!/bin/sh` line that execs `python3 -Es`, so they run on GNU env older than 8.30 (no `env -S`); the Python 3.11 message is unchanged.
- README: the council needs a POSIX system (Linux, macOS, WSL) and Python 3.11+; native Windows Python is unsupported.
- The compaction-gap hint also skips an attempt a later attempt on its row retried or continued, and one this session started less than `gapInflightSec` (default 2 h) ago.

- `scripts/build-export.sh` reads its list of private names from a file outside the repo (default `${XDG_CONFIG_HOME:-$HOME/.config}/boss-plugins/export-private-names.txt`, or `EXPORT_PRIVATE_NAMES_FILE`) and refuses to run when the file is missing or empty; the tests write their own file with invented names.
- The `council` skill sets `disable-model-invocation: true`, so only the user can start a council run.
- The Node floor is 22.13: `node:sqlite` needed `--experimental-sqlite` before it. `bin/boss-node` and `/boss:doctor` check for 22.13.

### Removed
- `IMPORT-SOURCE` and `scripts/import-from-claude.sh` (a one-off import helper).

### Fixed
- Council entry points no longer write `__pycache__` into the plugin directory.
- Council: an unwritable `--out` is refused before any paid canary (`ask`) or answered with one line (`run`), exit 2, no traceback.
- Council: Python older than 3.11 gets "council needs Python 3.11+ (found X)" before the `tomllib` import; the entry scripts use `#!/usr/bin/env -S python3 -Es` instead of a hard-coded `/usr/bin/python3`.
- Council: a missing `claude`, `codex`, `agy` or `qwen` CLI is reported as "<name> CLI not found on PATH" and skips the 2 s retry (a CLI that exists but fails is still retried).
- Council: `tools/council-probe.py` prints the Python 3.11+ requirement before importing `tomllib`.
- Council: invalid TOML in a config names the file, line and column.
- Council: the session mirror reads `$CLAUDE_CONFIG_DIR/sessions` when set (default `~/.claude/sessions`).
- Council, hook and meter comments, example JSON and test fixtures no longer name internal run slugs.
- `claude plugin validate --strict` passes.
