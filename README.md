# boss-plugins

A Claude Code plugin marketplace (git or local directory) with one plugin, `boss`: plan-driven orchestration for Claude Code. The dispatch loop is inspired by Kilo Code's Orchestrator mode.

## What boss is

You describe a job. The main chat plans it, you approve the plan, and it runs the plan in waves of subagent rows while keeping its own context small. Everything the run needs lives in files, so a context compaction or a new chat loses nothing.

The plugin ships:

| Part | What it does |
|---|---|
| Skills `/boss:*` | `start`, `approve`, `away`, `cost`, `doctor`, `runs`, `status`, `stop`, `resume`, `memclean`, `council` |
| Council (`council/`) | a multi-model council runner: several models answer one question, a chair synthesises. `/boss:council`; see [Council](#council) |
| Agents `boss:orch-*` | `orch-scout` (read-only search), `orch-builder` (edits and commits), `orch-planner`, `orch-plan-checker`, `orch-reviewer` |
| Engine (`lib/`) | run manifest, attempt ledger, leases, doctor, metrics. Commands print their own help: `node <plugin dir>/lib/ledger.js help` |
| Usage policy (`wfp/`) | model and effort presets per role (economy, balanced, quality, max), launch caps, context checkpoints |
| Hooks (`hooks/hooks.json`) | record each launch and completion in the ledger, flag overlong worker replies, keep read-only roles out of the repo, remind the main chat to delegate |
| Templates | the worker contract, plan and handoff templates |

The hooks are active in every session while the plugin is enabled, not only inside `/boss:start` runs.

## Requirements

- Claude Code 2.1.321 or newer (the release this plugin was built and checked against; older ones are untested).
- Node 22.13 or newer. The metrics store uses `node:sqlite`, which needs `--experimental-sqlite` before 22.13. Hooks find Node through `bin/boss-node`, which tries `$BOSS_NODE`, then `node` on the PATH, then the newest `~/.nvm` install.
- `git` for runs that commit, and `jq` for the commands in these docs.
- Python 3.11 or newer, only for the council (`tomllib`). Council seats need the `claude` CLI; the optional ones are the `codex` and `agy` CLIs or an `OPENROUTER_API_KEY`. Without them the rest of the plugin works and `/boss:council` reports what is missing.
- To run the tests: also the `sqlite3` CLI and `rg` (see [Development](#development)).
- Platform: developed and used on Linux and WSL2. The engine and hooks are plain Node and should run on macOS too; the CI workflow runs the suites there and they pass on macOS (Node 22.x and 24.x), apart from the tests that read `/proc`, which are skipped on macOS and print why. The council needs a POSIX system (Linux, macOS or WSL) and Python 3.11 or newer; native Windows Python is not supported (the council uses `fcntl`, `fork` and process groups), so run it under WSL. On macOS the council works but is lightly tested: its process-group listing and CPU sampling fall back to `ps`, while the per-seat environment scan, the IO sampling and the `/proc` readers in the progress mirror and network watch are Linux only and degrade to "unknown". `/boss:memclean` and `bin/boss-wsl-reclaim` are Linux and WSL2 only (memclean exits 2 elsewhere), and `secret-scan.sh --leftover` exits 2 ("cannot check here") off Linux rather than report a clean result. The meter board lists sessions on macOS by pid and `ps` start time instead of `/proc` start ticks (that path has no CI coverage yet). Process-ownership reporting (`lib/processes.js`, used by the Stop hook) reads `/proc` and is Linux only: on macOS it logs one line to `errors.log` and the hook carries on. Windows without WSL is untested.

## Install

From GitHub:

```bash
claude plugin marketplace add thoughtleader/boss-plugins
claude plugin install boss@boss-plugins
```

Or inside Claude Code:

```
/plugin marketplace add thoughtleader/boss-plugins
/plugin install boss@boss-plugins
```

From a local clone, for development:

```bash
claude plugin marketplace add /path/to/boss-plugins
claude plugin install boss@boss-plugins
```

(or `/plugin marketplace add /path/to/boss-plugins` inside Claude Code). A directory marketplace is read in place, so a `git pull` in this repo followed by `claude plugin update boss@boss-plugins` is how you update. A Git marketplace updates with `claude plugin marketplace update boss-plugins`, then `claude plugin update boss@boss-plugins`. To try the plugin without installing it, run `claude --plugin-dir /path/to/boss-plugins/plugins/boss`.

If you already run a hand-installed copy, read [docs/MIGRATION.md](docs/MIGRATION.md) first. Two copies of the hooks make every hook fire twice.

## First run

1. Run `node <plugin dir>/lib/paths.js` to see where the plugin keeps its state. Inside a session, `/boss:start` prints `BOSS_LIB=<path>` at the top of its output. From a shell, the install path is `jq -r '.plugins["boss@boss-plugins"][0].installPath' ~/.claude/plugins/installed_plugins.json`.
2. In a Claude Code session in the repo you want to work on, run `/boss:start <job description>`. It names the run, writes `BRIEF.md` in the run folder, takes the run's lease, launches the planner and the plan check, then asks you to approve the plan.
3. Approve with the question it asks, or with `/boss:approve`. The first wave launches.
4. Watch with `/boss:status` (add `--details` or `--full`), and see what it cost with `/boss:cost`.
5. To pause, run `/boss:stop`, then `/clear`, then `/boss:resume <run name>`. `/boss:runs` lists runs you can resume. `/boss:doctor` checks the setup.

Run `/boss:doctor` once after installing. It checks the data dir, the hook registration and the Node version.

Run `/boss:memclean` to free memory on a WSL2 or Linux dev box: it reports memory, reaps helper processes whose Claude session is gone (live chats' MCP servers are never killed), optionally stops idle dev servers, and prints the sudo command that drops page cache (`bin/boss-wsl-reclaim` is the same job as a root cron).

## Council

`/boss:council <question>` runs the plugin's own multi-model council (`council/council.py`): a preflight gate, then several seats (models) answer the same question in parallel and a chair model writes one synthesis. Seats can be the `claude`, `codex` and `agy` CLIs or any OpenRouter model. Runs are detached, keep every seat's answer on disk and survive a closed chat. It is separate from any other plugin's council command.

- **Needs** a POSIX system (Linux, macOS or WSL), python3 3.11+ (an older Python exits 2 with "council needs Python 3.11+"; there is no `tomli` fallback) and the `claude` CLI. Other seats need their own CLI (`codex`, `agy`) or `OPENROUTER_API_KEY`. A council run is paid, because each seat is a real model call; `council.py preflight --free-only` is free.
- **Config.** `council/config.default.toml` ships a one-seat roster that works with the `claude` CLI alone. For your own roster, copy it to `<config>/council/config.toml` (`<config>` is `CLAUDE_CONFIG_DIR`, or `~/.claude`) and edit it, or point `COUNCIL_CONFIG` at a file. The first of those that exists is the base config; the skill's preflight prints which one it used (`config: <path> (<source>)`). Run state goes to `~/.claude-council` (`state_dir`). Two keys you may want: `[canary] cache_s` (built in 600; a preflight canary that already passed for the same provider, model, prompt, launch flags and CLI binary within that many seconds is not paid for again; `0` always calls) and `[providers.codex] disable_user_mcp` (built in `true`: codex seats start with the MCP servers from your own `~/.codex/config.toml` turned off, one `-c mcp_servers.<name>.enabled=false` each; `false` leaves them on). The drift report hides URLs and MCP settings from your codex config (it shows `<set>`, stores a hash).
- **Check it.** `python3 <plugin dir>/council/council.py preflight --free-only`.
- **Test it.** `bash plugins/boss/test/council-tests.sh` runs the fast suite (about 2 minutes, a stub CLI, no network). `scripts/test-all.sh` includes it. Details: [plugins/boss/council/README.md](plugins/boss/council/README.md).

## Data directory

The plugin never writes state under its own directory, which is replaced on update. State goes in the data dir:

| Resolved in this order | Used for |
|---|---|
| `ORCH_DIR` | tests; also turns off progress snapshots |
| `BOSS_DATA` | your chosen location |
| `<config>/orchestrator`, when it already has a `runs/` folder | an existing hand-installed setup |
| `<config>/boss-data` | the default for a new install |

`<config>` is `CLAUDE_CONFIG_DIR`, or `~/.claude` when that is unset. Inside the data dir:

```
runs/<date>-<slug>/   one folder per run: BRIEF.md, PLAN.md, DECISIONS.md, results, manifest
leases/               which session drives which run
wfp/state/            per-session usage-policy state (short-lived)
wfp/handoffs/         handoff notes
wfp/denials.jsonl     one line per refused launch (rule, agent type, reason); capped at 256 KB, oldest half dropped
sweep-state/          one small <session id>.json per lease-holding session: how far the notice sweep has read its transcript
metrics.db            derived, rebuildable metrics (SQLite)
errors.log            hook errors
config.json           your engine settings (optional)
wfp.json              your usage policy (optional)
WORKER.local.md       your worker house rules (optional)
```

`wfp/denials.jsonl` and `sweep-state/` are derived and safe to delete. A missing denials log starts a new one. Without a sweep-state file the next sweep re-reads the last 4 MB of the transcript once and records nothing twice. Files in `sweep-state/` that have been idle for 14 days are removed when a new one is written. Neither file holds prompt text: they hold session ids, a transcript path, agent types and the deny reasons.

Finer overrides, each optional:

| Variable | Moves |
|---|---|
| `CLAUDE_CONFIG_DIR` | the config dir that the defaults below hang off |
| `ORCH_RUNS_DIR` | the `runs/` folder only |
| `WFP_STATE_DIR` | `wfp/state` |
| `ORCH_METRICS_DB` | `metrics.db` |
| `BOSS_PROGRESS_DIR` | progress snapshots (default `<config>/boss`; the `boss.progress/1` files a status line or editor extension can read) |
| `ORCH_PROJECTS_DIR`, `ORCH_SESSIONS_DIR` | where metrics read Claude Code transcripts and session files (defaults `<config>/projects`, `<config>/sessions`) |
| `BOSS_NODE` | the Node binary the hooks use |
| `BOSS_ANALYTICS_LIB` | the dir of an optional analytics add-on (see `analytics.lib` below) |

`node <plugin dir>/lib/paths.js` prints every path as resolved in your environment.

## Your policy

The plugin ships neutral defaults and replaces them on update. Your choices live in three files in the data dir, which the plugin never creates or overwrites.

**`config.json`** overlays `lib/defaults.json` one level deep. Keys:

| Key | Default | Meaning |
|---|---|---|
| `returnCapBytes`, `returnCapMode` | `2048`, `flag` | longest worker reply the hook accepts, and whether it only flags a longer one |
| `leaseTtlSec` | `7200` | how long a session's lease on a run lasts |
| `gapInflightSec` | `7200` | how long an attempt this session started counts as still running, so the compaction-gap hint skips it |
| `waitingGraceSec` | `1800` | how long an interim "waiting on its own background work" notice may stand with no later notice before `manifest.js reconcile` promotes it to final and prints `waiting-timeout` |
| `guard.exemptPaths` | `[]` | extra path prefixes the delegation reminder ignores |
| `metrics.commitRepos` | `[]` | repos, relative to `$HOME`, that the metrics scan for commits. Empty means none |
| `metrics.commitAuthor` | `null` | only count commits by this author. `null` means no filter |
| `analytics.lib` | `null` | where an optional analytics add-on lives; `ledger.js dashboard` and `ledger.js cron` need it |
| `analytics.weeklySync` | `true` | set `false` if you do not want the weekly metrics cron job; `/boss:doctor` then reports its absence as a choice, not a warning. `ledger.js cron remove` does the same (it leaves a `cron-removed` marker in the data dir; `cron install` deletes it) |
| `patchPluginAgents`, `extraAgentTools` | off | opt-in edits to the orch agents' `tools:` line, see below |

**`wfp.json`** overlays `wfp/config.default.json`. Top-level keys replace the defaults. `profiles`, `typeRoles` and `ultracodeRaise` merge one level deep, so a profile you define replaces the whole shipped profile of that name. Useful commands (run from `<plugin dir>`):

```bash
node wfp/wfp.js profile show quality   # one preset: role -> model + effort
node wfp/wfp.js default quality    # store a lasting default preset in wfp.json
```

With no `defaultProfile`, the policy hook asks once per session which preset to use. `wfp/REFERENCE.md` documents the rest.

**`WORKER.local.md`** holds rules about you or your machine that every worker should follow: git identity, package manager, ports, off-limits directories. Workers read the plugin's `templates/WORKER.md` first, then the role files for their role, then this file, and the local file wins on conflict. The worker rules are split by role: `node "$BOSS_LIB/paths.js" worker <role>` lists `WORKER.md` plus `WORKER.builder.md` (commit and git rules, builders only) and `WORKER.runtime.md` (UI checks and background processes, builders and reviewers), so scouts and planners load less. With no role it lists every file. [docs/WORKER.local.example.md](docs/WORKER.local.example.md) shows the format. [docs/CLAUDE-snippet.md](docs/CLAUDE-snippet.md) has optional text for your own `CLAUDE.md`.

**Engine commands worth knowing.** `node "$BOSS_LIB/manifest.js" void --attempt <run>#<row>.<n> [--reason <text>]` marks an attempt ID that was allocated but never launched (for example after an ID collision), so `show`, `reconcile` and the status views stop counting it; it refuses once the attempt has a start, a stop or a ledger line. `ledger.js` and `manifest.js` accept the modes `subagent`, `wave`, `workflow`, `agent-team` and `background-bash` (a council run or other shell job); a council row is recorded with model `council` or `council-<n>seat` and is listed apart from the Claude rows. A worker that hits its turn limit is recorded as unfinished (`stopped-turn-limit`) and can be continued with `launch --continue`.

**Extra tools for the orch agents (opt-in, off by default).** Claude Code has no setting that adds tools to a plugin agent: a user or project agent with the same file name is a different agent (`orch-builder`, not `boss:orch-builder`), and the plan rows only call the `boss:` form. If you need extra MCP tools on the orch agents, set `"patchPluginAgents": true` and `"extraAgentTools": {"*": ["mcp__x__*"], "orch-scout": ["mcp__y__*"]}` in `<data>/config.json`. A session-start hook then edits the `tools:` line of the installed agent files (and marks them skip-worktree in a git checkout); a plugin update replaces them and the next session patches again. Without `patchPluginAgents`, the plugin never modifies its own files.

**Merging into a checkout that has the patch.** The patched files are marked skip-worktree, so `git status` prints nothing for them, but `git merge` still refuses a branch that changes one of them ("Your local changes would be overwritten"). The order is: lift, merge, run the patcher again. `node <plugin dir>/hooks/agent-tools-patch.js --lift [<file>...]` restores the committed text and clears the flag of each skip-worktree file whose only difference is extra entries on its `tools:` line, and prints one line per file (lifted, not patched, or refused with the reason; a file with any other local change is refused, left as it is, and the command exits 1; an assume-unchanged file is reported as not patched). With no file it lifts the agent files in the folder next to the script, so whenever the script you run is not the target checkout's own copy, pass that checkout's agent files as full paths (`<checkout>/plugins/boss/agents/orch-builder.md` and the others). A copy of the script older than 0.4.4 does not know `--lift`: it ignores the flag and patches instead, so run the 0.4.4 copy. After the merge, `node <plugin dir>/hooks/agent-tools-patch.js` with no argument (what the session-start hook runs) puts back the extra tools the config lists and the flag; `tools:` entries you added by hand that the config does not list are lost by the lift and not put back, so add them to `extraAgentTools`. `git ls-files -v | grep '^[Sh]'` lists every skip-worktree or assume-unchanged file of a checkout; a clean `git status` does not.

## Turning parts off

The always-on hooks (the delegation reminder and the policy question before the first agent launch) are meant for orchestrated work. If you only want one part of the plugin, such as the council, you can quiet them:

- **Delegation reminder:** set `ORCHESTRATOR_GUARD=off` in your environment to disable it everywhere, or run `node <plugin dir>/hooks/orchestrator-guard.js off` to disable it for the current session (`on` re-enables it, `status` shows the state).
- **The "which preset?" question before the first agent launch:** store a lasting default with `node <plugin dir>/wfp/wfp.js default balanced` (any of `economy`, `balanced`, `quality`, `max`), and new sessions stop asking. For one session, `node <plugin dir>/wfp/wfp.js set override=true --sid=$WFP_SID`, which skips the question and lifts the launch cap and the model warnings.
- **Everything:** disable the plugin with `/plugin`. Nothing in the data dir needs removing.

The reports only read, with one exception: `ledger.js report tokens-per-accepted|first-pass` first syncs the derived metrics cache (`metrics.db`) so the numbers are current and prints a line on stderr when it creates it. Add `--no-sync` to read the cache as it stands and write nothing. `ledger.js report retries` never touches the cache: it reads the run ledgers and shows what retried attempts cost, grouped by cause, role and model/effort.

## Uninstall

```bash
claude plugin uninstall boss@boss-plugins
claude plugin marketplace remove boss-plugins
```

Claude Code leaves the cached copy under `<config>/plugins/cache/boss-plugins/` on disk; delete it if you want it gone. The data dir stays too, so your runs and ledger survive. Delete it yourself when you no longer want them.

## Privacy

- Runs hold your prompts. `BRIEF.md`, plans, decisions and worker results are plain files in the data dir, and may quote your code, paths and task text.
- Metrics read your local Claude Code transcripts (`<config>/projects`) to count tokens and tool use, and write derived numbers to `metrics.db`. If you set `metrics.commitRepos`, they also read those repos' git logs.
- The plugin's own code makes no network calls and sends nothing anywhere, except a council run, which sends the question and any `--context-file` contents to the seats' providers. Your prompts go where Claude Code already sends them.
- Before you publish your data dir, a run folder or a clone with local edits, run `bash scripts/secret-scan.sh <path>`. It lists files that hold key-shaped strings and prints file names only.

## Development

```bash
bash scripts/test-all.sh      # every suite, each in a scratch HOME and data dir
bash scripts/secret-scan.sh . # leaked-key scan of the repo
bash scripts/build-export.sh HEAD <out-dir>   # one-commit public export, see below
```

The tests need `bash`, `node` 22.13 or newer, `python3` 3.11 or newer (the council suite), `jq`, `git`, the `sqlite3` CLI and `rg`. They run in a scratch HOME and write nothing to your config or data dir. The CI workflow is configured to run both commands on Ubuntu and macOS with Node 22.x and 24.x; all of it passes there; on macOS the tests that read `/proc` (Linux only) are skipped and each prints its reason.

## Building the public export

Maintainers publish a one-commit copy of the tree, not the development history. `scripts/build-export.sh` builds it:

```bash
bash scripts/build-export.sh <sha> <out-dir>     # out-dir must be missing or empty
```

It runs `git archive <sha>` into `<out-dir>`, deletes what the public repo must not carry, sweeps the result, and only then runs `git init` and one commit. The sweep fails closed: a missing `grep`, a tool error, a sweep that cannot find its own planted test strings, a symlink in the tree or any hit prints the hit and exits 1 with no repository created. It checks every file (binary too) and every file and directory name for key-shaped strings (`scripts/secret-scan.sh` plus its own pass), private project names, and any email, URL, private IPv4 address (10.x, 172.16-31.x, 192.168.x, 169.254.x) or `/home/<user>`, `/Users/<user>`, `C:\Users\<user>` path that is not on an exact allow-list. The only public identity tokens it lets through are the exact author URL, the exact repo path (`thoughtleader/boss-plugins`) and the exact noreply address; a sub-path, a suffix or a look-alike host (`.../admin`, `...-private`) fails. The author name and commit message are swept too. It prints the commit SHA, the tree hash and the file count, adds no remote and pushes nothing.

The commit author and committer default to `Michael Evan <thoughtleader@users.noreply.github.com>`. Override them with `--name`/`--email` or `EXPORT_AUTHOR_NAME`/`EXPORT_AUTHOR_EMAIL`; the email must be a GitHub noreply address (`*@users.noreply.github.com`) unless `EXPORT_ALLOW_ANY_EMAIL=1`. `--message` or `EXPORT_COMMIT_MESSAGE` sets the commit message. After building, run `bash scripts/test-all.sh` and `claude plugin validate --strict` inside the export before you push it.

## License

MIT, see [LICENSE](LICENSE).
