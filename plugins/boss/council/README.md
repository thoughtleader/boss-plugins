# council

A multi-model council runner. One question goes to several seats (models) in parallel, each answers independently, and a chair model writes one synthesis with the majority view, the minority view, risks and a next step. It is stdlib-only Python (3.11+, for `tomllib`) and drives each model through its own CLI or API.

Use it through the plugin: `/boss:council <question>` (the skill runs the preflight gate, then the run). This file covers running it by hand.

## Layout

| Path | What |
|---|---|
| `council.py` | the CLI: `preflight`, `run`, `status`, `drift`, `reap` |
| `councillib/` | the engine: config, providers, supervision, preflight, status, drift, grounded mode |
| `prompts/` | the advisor, critique and chair prompt templates |
| `config.default.toml` | the shipped default config (one `claude` seat) |
| `progress.py` | publishes the live `boss.progress/1` snapshot the chat meter reads |
| `tools/` | `council-probe.py` and `liveness-probe.py`: probe scripts that characterise a CLI's behaviour; they can make paid calls and the tests do not run them |
| `tests/` | the unit suite (`run_suite.py`) and its fixtures; a stub CLI stands in for every model |

## Configure

The base config is the first of these that exists:

1. `$COUNCIL_CONFIG`, an explicit path (a missing file is an error);
2. `<config dir>/council/config.toml`, where `<config dir>` is `CLAUDE_CONFIG_DIR`, or `~/.claude`;
3. `config.default.toml` next to `council.py`.

A user config replaces the default entirely (it is the base, not an overlay), so start from a copy of `config.default.toml`. `--config P` (repeatable) deep-merges an overlay over the base for one run; a `[[seat]]` list in an overlay replaces the whole roster.

What to edit:

- `[chair]` and `[[seat]]`: provider (`claude`, `codex`, `agy`, `openrouter`, `qwen`) and model. The file has commented examples for each provider. A `claude` model must be a full model id; at most one Anthropic seat is allowed (`[policy]`).
- `[quorum] min_ok`: how many seats must answer. `"n-1"` lets one seat fail; the default is `"n"` because the default has one seat.
- `[pins]`: CLI versions preflight checks. Add exact pins for the CLIs you use.
- `state_dir` (default `~/.claude-council`): where run directories, the drift baseline and latency history go. `prompts_dir` is optional; unset, the prompts next to `council.py` are used.
- OpenRouter seats read the key from `OPENROUTER_API_KEY` (`[providers.openrouter] key_env`).

## Run

```bash
python3 council.py preflight --free-only            # free: CLI versions, budgets, drift, roster policy; first line names the config used
echo "your question" > q.txt
python3 council.py preflight --free-only --question-file q.txt
python3 council.py run --question-file q.txt        # PAID: sends the question to every seat, then the chair
python3 council.py status [--watch]                 # live status of the current run
```

`run` repeats the preflight (with a small paid canary per seat) before any spend, starts a detached runner, and prints `synthesis ready: <path>`, `run dir: <path>` and `exit: N`. The synthesis is `<run dir>/chair/synthesis.md`; every seat's answer is in `<run dir>/seats/`. Exit codes: 0 done, 1 preflight failed, 2 usage or policy, 3 quorum not met, 4 chair failed or no answer yet, 5 runner crashed.

A seat that passed its canary within `[canary] cache_s` seconds (default 600, `0` = always call) for the same model, prompt, launch flags and CLI binary is not called again; the preflight table shows it as `cached`. Codex seats do not load your own MCP servers: for each server in your codex `config.toml` whose name is a plain key (`[A-Za-z0-9_-]`) the seat gets `-c mcp_servers.<name>.enabled=false` (`[providers.codex] disable_user_mcp = false` turns that off; a server with another name cannot be disabled this way and the preflight warns). `drift` and the preflight never print URLs, commands or header values from your codex config, only `<set>` and "value changed, not shown".

Seats run in a temporary directory with the prompt on stdin, so they cannot read your disk: attach every file the question refers to with `--context-file`, or use `--grounded [PATH]` to give the tool-capable seats read-only access and have their `path:line` citations checked.

## Test

```bash
bash ../test/council-tests.sh                         # the fast half, about 2 minutes (from plugins/boss/council)
python3 -Es tests/run_suite.py --slow                 # the stub-process classes: supervisor, hedge, fallback, replay
python3 -Es tests/run_suite.py                        # everything, 600 s limit
```

The suite never starts a paid call and never reads your own config: `tests/__init__.py` pins `COUNCIL_CONFIG` to `tests/fixtures/config.test.toml`. `scripts/test-all.sh` in the repo root runs the fast half with the other suites.
