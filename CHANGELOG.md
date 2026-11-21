# Changelog

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
