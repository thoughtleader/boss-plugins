# boss.progress/1: shared live-progress contract

One format that council, the orchestrator, the Claude mod (boss-progress) and the Boss VS Code extension all use. It comes from a council synthesis of 2026-10-31.

| File | What |
|---|---|
| `PROGRESS.md` | this spec |
| `progress.schema.json` | JSON Schema (draft 2020-12) for one snapshot (plugin `lib/progress.schema.json`) |
| `progress.py` | (plugin `council/progress.py`) stdlib helpers: writer, reader, `liveness`, `session_state`, `claim_notification`, `prune`, `validate` |
| `progress.mjs` | (plugin `lib/progress.mjs`) the same helpers for Node (no deps) |
| `examples/` | `council.json` (mid-run, with a stand-in seat), `orchestrator.json` (waves, attempts, a gate, an attention item) |
| `tests/` | `python3 -B -m unittest test_progress.py` (council) and `node --test meter/test/progress.test.mjs` (meter). Both suites load the same `liveness-cases.json`. |

## 1. Layout

```
$BOSS_PROGRESS_DIR (default ~/.claude/boss/)      0700
  runs/<tool>/<run_id>/                           0700  registry entry = this directory
    run.json        write-once identity           0600  {schema, tool, run_id, created_at, source_dir, title}
    progress.json   the snapshot                  0600  replaced atomically, never edited in place
    claims/<key>    notification claims           0600  O_CREAT|O_EXCL, written by readers
    control/        cancel: owner Stop from the Boss board (orchestrator runs). JSON {schema:"boss.cancel/1",requested_at,by,reason,tool,run_id,nonce}; presence is the contract. Read by manifest.js launch (exit 4), hooks/orch-stop-gate.js and board.mjs (Stopping/Stopped)
  inbox/<claude_pid>/                             0700  messaging (section 10)
```

- **Colocated, not a pointer.** The snapshot lives in the registry tree. Tool artifacts (council's `status.json` and seat outputs, the orchestrator's `PLAN.md`, `manifest.jsonl` and result files) stay where they are, and the snapshot points at them through `source_dir`, `events_path` and `items[].artifacts`. Why:
  - a pointer and its target can never disagree;
  - readers watch a single root (`runs/`), which also answers the inotify-limit concern;
  - retention is one rule over one tree;
  - the tools' own files keep their current shape, so no existing reader breaks.
- **Write-once:** the writer creates `run.json` with `O_EXCL` and never rewrites or deletes it. Only `prune` removes a whole run directory (section 9).
- **`tool`** matches `^[a-z][a-z0-9-]{0,31}$`. **`run_id`** matches `[A-Za-z0-9._-]{1,128}` and may not be `.` or `..`. Helpers reject anything else before building a path.
- **Root override:** `BOSS_PROGRESS_DIR`. Tests always set it, so they never touch the real tree.

## 2. Writing

- **Exactly one publisher per run.**
  - Council: the runner process (`council_run.py`) writes `progress.json` from the same code path that writes `status.json`.
  - Orchestrator: `manifest.js`, inside its existing `withLock(manifestFile(run))`, the mkdir lock that `appendLocked` already runs under. It appends the event and folds it into the snapshot under that lock. Hooks never read, modify and write the snapshot on their own: they call `manifest.js`.
- **Atomic replace:** write to `.progress.json.<pid>.tmp` in the same directory (0600), then `os.replace` / `fs.renameSync`. No fsync is needed, because a torn file can only follow a power loss, and the next write repairs it. The helpers are `write_snapshot` / `writeSnapshot`.
- **`seq`:** a non-negative integer that never goes down within a run.
  - Orchestrator: the `seq` of the last manifest event folded in.
  - Council: its `events.jsonl` line count, or the status write counter until events exist.
  - A reader that finds `seq` behind the event log may say "snapshot behind log". Rebuilding from events belongs to the tool, never the reader.
- **Writer identity:** `writer_identity(kind)` records the publishing process itself: `{pid, start_ticks (/proc/<pid>/stat field 22), boot_id, kind}`. Council must record the Python runner, not the launcher shell.
  - `kind: "process"` is a long-lived writer that heartbeats.
  - `kind: "oneshot"` is a short-lived writer such as `manifest.js`, which promises no heartbeat.
- **Write cadence:**
  - Process writers write on every state change (coalesced, ≥0.25 s apart) and at least every `heartbeat_interval_s`.
  - `last_progress_at` moves only on real progress: bytes, phase or item change. A pure heartbeat does not move it.
- **Size and hygiene:** `write_snapshot` strips control and ANSI characters and truncates text to these caps:
  - title 200, summary 500, label 200, error 2000, detail 2000, attention text 1000.
  - Whole snapshot ≤ 512 KiB (the writer raises above that). Items ≤ 1000, attention ≤ 32.
  - Never copy seat output or model text into a snapshot. Point at the file instead (`artifacts[].path`).

## 3. Snapshot envelope

| Field | Type | Meaning |
|---|---|---|
| `schema` | `"boss.progress/1"` | major version in the string |
| `tool` | string | `council`, `orchestrator`, … |
| `run_id` | safe id | the tool's own run name |
| `title`, `summary` | string? | one line each. Summary is the writer's human line. |
| `cwd` | path? | the session/project folder the run belongs to (jump-to target) |
| `source_dir`, `events_path` | path? | the tool's run folder and its append-only log |
| `state` | `queued·running·waiting_input·done·failed·cancelled` | the writer's claim. Liveness is separate (section 6). |
| `phase` | string? | tool vocabulary (`seats`, `chair`, `wave 2`, …) |
| `created_at`, `started_at`, `updated_at`, `ended_at` | ISO-8601 UTC `…Z` | `updated_at` is required and set on every write |
| `heartbeat_at`, `heartbeat_interval_s` | ts?, number? | null for oneshot writers |
| `last_progress_at` | ts? | see section 2 |
| `seq` | int ≥ 0 | section 2 |
| `writer` | `{pid, start_ticks, boot_id, kind}` | section 2 |
| `owner` | `{session_id, pid, start_ticks, inbox}`? | the Claude session that owns the run. `pid` may be null (resolved by the session adapter, section 7). `inbox` is reserved (section 10). |
| `counts[]` | `{unit, label, done, total, running?, failed?}` | **always scoped to a unit** (`seats`, `quorum`, `rows`, `wave`). There is no global percent: "5/5 seats" is not "done" while the chair runs. |
| `items[]` | section 4 | |
| `attention[]` | section 5 | |
| `paused` | boolean? | **additive in /1 (row 4b).** `true` only while a non-terminal run is paused on purpose (orchestrator: lease released before the plan finished; the state stays `waiting_input`, phase `paused (lease released)`). Absent otherwise. Readers show "paused": not dead, not "need you", no toast. For snapshots written before this field, a reader may treat `state: waiting_input` with a phase starting `paused` the same way. |
| `ext` | `{ "<tool>": {...} }` | tool-only data. Shared views never read it. Anything two tools need is promoted to a top-level field in a minor revision. |

## 4. Items

`{id, parent, kind, label, state, attempt, replaces, model, effort, started_at, updated_at, ended_at, error, detail, artifacts[], meta}`

- `state`: `queued · running · waiting_input · done · failed · cancelled · skipped · blocked`.
- **A tree through `parent`.** Ids are unique within the snapshot and stable across writes, so readers can diff.
- **Retries, hedges, fallbacks and stand-ins are separate items**, linked by `replaces` (the id of the item they follow) and `attempt` (1-based). A stand-in seat runs a different model, so it needs its own row.
- `artifacts[]`: `{label, path?, sha?}`. Use it for result files and commits.
- `kind` vocabulary:
  - council: `seat`, `call`, `stand-in`, `chair`;
  - orchestrator: `wave`, `row`, `gate`, `attempt`.
  - Readers render unknown kinds generically.

## 5. Attention ("needs you")

`{id, kind: question|approval|error, item?, text, since, options?, answered_by {via, message_kind?, gate_hash?, command?, path?}}`

- `via` says what answers it:
  - `session`: answer in the owning Claude session;
  - `message`: a row-4b inbox message;
  - `control_file`: write `<run dir>/control/<name>`;
  - `command`: run this shell command;
  - `file`: edit or open this file.
- `message_kind` uses row 5's message kinds: `approve_gate · answer · resume · cancel`. `gate_hash` ties an approval to one plan hash, so a stale message cannot approve a different plan.
- Status bar count = runs with a non-empty `attention[]`, plus runs whose liveness is `waiting_input`.

## 6. Liveness: worked out by the reader, never stored

`liveness(snap, now, probe, resumed_at)` → `{status, reason, heartbeat_age_s, no_progress_s, quiet}`. The checks run in this order:

1. **Major version:** a schema whose major isn't 1, or that can't be parsed, → `unsupported`. Show "unsupported version", never guess.
2. **Terminal state:** `done`, `failed` or `cancelled` → `ended`. Stop here.
3. **Is the writer or owner there?**
   - **process** writer:
     - `writer.boot_id` ≠ the current boot id → `dead`;
     - the pid is missing, or `start_ticks` differs (pid reuse) → `dead`.
     - A dead writer on a non-terminal run is "orphaned".
   - **oneshot** writer: never check `writer.pid`, because it is gone by design. Ask the session adapter (section 7):
     - owner session gone → `dead`;
     - `busy` → `running`;
     - `idle` → `waiting_input`;
     - no status → `unknown`.
4. **Stalled heartbeat** (process only): if the heartbeat is older than `max(60 s, 5 × heartbeat_interval_s)`, the result is `stalled`.
   - **Resume grace:** if the reader noticed a clock jump (its own poll gap was over 3× its poll interval, e.g. a WSL suspend) and passes `resumed_at`, report `running` until the same threshold has passed since `resumed_at`.
   - A snapshot `state: waiting_input` from a live process writer → `waiting_input`.
5. **"No progress for X":** if `last_progress_at` is more than 300 s old while the status is `running`, the result gets `quiet: true` with `no_progress_s`. Word it as a fact, not an alarm: "no new output for 6m". Long model calls are normal.

**Thresholds** are at codex/deepseek scale, not 6–10 s: 60 s minimum stall, 5× interval, 300 s quiet. Readers poll every 1–3 s and use `fs.watch` on `runs/` only as a hint to rescan sooner. Readers never repair writer state.

Without `/proc` (not Linux), the probe returns null and step 3 would report dead. Hosts like that must pass a probe that returns "unknown". All tools run in one WSL distro today.

## 7. Orchestrator session adapter (the only reader of `~/.claude/sessions/`)

`session_state(owner, sessions_dir)` / `sessionState(owner, sessionsDir)`:

- It reads `<pid>.json` files only. It **never opens `*.key`, never touches `messagingSocketPath` or `/tmp/cc-socks`.** The tests put an unreadable `.key` file next to the session files to prove this.
- Lookup:
  - `owner.pid` known → `<pid>.json`;
  - otherwise it scans `^\d+\.json$` for `sessionId == owner.session_id`.
- **Before trusting `status`**, it checks that the pid is alive and is the same process:
  - with `owner.start_ticks`, it compares those;
  - otherwise, `|process start (btime + ticks/CLK_TCK) − startedAt| ≤ 30 s`.
  - A crashed session can leave its file behind. In that case the adapter returns null (= gone).
- It returns `{pid, status: busy|idle|unknown}`.
- The file format is undocumented Claude Code state. If it changes, only this one function changes.

## 8. Notifications: dedupe across windows

- Before showing a toast, a reader calls `claim_notification(tool, run_id, key)`. That creates `claims/<key>` with `O_CREAT|O_EXCL`, and only the window that gets `True` shows the toast.
- Keys:
  - `<state>` for run-level events (`done`, `failed`, `dead`, `waiting_input`);
  - `<item-id-sanitised>-<state>` for item events (for example `row-2-done`).
- The key alphabet is the same as for `run_id`.
- **First load:** a reader that starts up records the runs that are already terminal (in memory) and toasts nothing for them. It does not claim them either. Each reader works this out on its own.

## 9. Retention

- `prune(days=7)` deletes `runs/<tool>/<run_id>/` when its `progress.json` (or `run.json`) mtime is older than 7 days. That covers both finished runs and abandoned orphans.
- Writers call it (council at run start, `manifest.js` on `lease acquire`). Readers never delete anything.
- Tool-native artifacts (`~/.claude-council/runs/…`, `orchestrator/runs/…`) are untouched. Their own retention stays as it is.
- Views show "active + last 24 h"; the disk keeps 7 days.

## 10. Messaging (VS Code extension add-on, not shipped in this repo)

This section documents the messaging add-on that a Boss VS Code extension can install; none of its code (`msg/`, `vscode/`) is part of this plugin. It is kept here because it reads and writes the same progress dir. The Boss extension can message a live Claude session; the agent answers with a tool. Code: `msg/boss-msg.mjs` (stdio MCP server, one per Claude process, no dependencies), `msg/stop-hook.sh` + `msg/stop-hook-drain.mjs` (Stop hook), `vscode/src/inbox.mts` (extension side). Tests: `tests/msg.test.mjs`, `vscode/test/inbox.test.mts`, `vscode/test/activate.test.mts`.

```
$BOSS_PROGRESS_DIR/inbox/<claude_pid>/        0700, made by boss-msg when the session starts
  meta.json          {pid, start_ticks, cwd, session_id, name, channel, registered, server_pid, started_at}
  <msg_id>.json      pending message {msg_id, from, text, run?, at}   0600, written by the extension (tmp + rename)
  done/<msg_id>.json delivered message (claimed by rename: exactly one winner, so never delivered twice)
  replies.jsonl      {msg_id, text, at, session_id, pid}, appended by the reply tool (rotates to .1 above 1 MiB)
```

- **Pid:** boss-msg walks its parent chain to the first pid that has `~/.claude/sessions/<pid>.json` (reads only pid, sessionId, name, status, cwd). Without one it falls back to its parent pid (`registered: false`).
- **Three delivery routes, one inbox:** (1) channel push (`notifications/claude/channel`) when the Claude process was started with `--channels` or `--dangerously-load-development-channels` (read from `/proc/<pid>/cmdline`; `meta.channel`); wakes an idle CLI session; (2) pull tool `get_pending_messages`; (3) the Stop hook, which blocks once with the message quoted in `reason` and tells Claude to answer with `mcp__boss__reply`. Without channels (the VS Code panel) a message waits until the session ends a turn: **an idle panel session cannot be woken**. The extension says so when it sends.
- **Limits:** text 4000 chars from the extension (8 KiB in the file, 16 KiB file cap), 50 pending, replies shown as untrusted plain text. Files not owned by the user, not regular, oversized or malformed are ignored. Message text is a request from the owner, never a system instruction.
- **Install (done once, global):** `claude mcp add --scope user boss -- <node> ~/.claude/boss/msg/boss-msg.mjs` (entry `mcpServers.boss` in `~/.claude.json`) and one extra entry at the end of `hooks.Stop` in `~/.claude/settings.json`: `/bin/bash ~/.claude/boss/msg/stop-hook.sh` (timeout 10). Sessions started before that do not have the server: start a new one. First use of `mcp__boss__reply` asks for permission unless allowed (`--allowedTools mcp__boss__reply`, or a `permissions.allow` entry).
- **Live push for a CLI session:** `claude --dangerously-load-development-channels server:boss` (a confirmation dialog on start; research preview; ignored with `-p`; not available in the VS Code panel). Suggested shell alias, not installed: `alias claude-boss='claude --dangerously-load-development-channels server:boss'`.
- **Undo:** `claude mcp remove boss --scope user`; delete the last `hooks.Stop` entry in `settings.json` (the one running `stop-hook.sh`); `rm -r ~/.claude/boss/inbox`.
- **Not messaging:** cancel stays a tool-level control file (`<run dir>/control/cancel`, phase 2), because a busy session cannot take a message.
- **Paused runs (additive field `paused`, section 3):** when the orchestrator lease is released before the plan finishes, the snapshot keeps `state: waiting_input`, phase `paused (lease released)` and now also `paused: true`. The extension shows such a run as `paused` (pause icon, not dead, not "need you", no toast, clock stops).

## 11. Versioning

- The major version is in the `schema` string. Within `/1`, changes are **additive only**: new optional fields, new enum values on `kind`, and new `ext` keys. Readers ignore unknown fields.
- Changing the meaning of a field, removing one, or adding a run `state` value is a new major. Readers then show "unsupported version" (liveness step 1).
- The schema deliberately has no `additionalProperties: false` anywhere.
- **Validator subset:** `validate()` in both helpers implements exactly these keywords: `type` (also as an array), `required`, `enum`, `properties`, `items`, `maxLength`, `maxItems`, `pattern` (strings only), `minimum`, and local `$ref` to `#/$defs/*`. A schema change has to stay inside this subset, or the helper needs a new keyword first.

## 12. Rendering

Treat every string as untrusted:
- escape it in webviews and never set innerHTML;
- truncate it again to the view's width;
- never execute `command` without a click;
- open only paths under the run's `source_dir`, `cwd` or the boss root.
