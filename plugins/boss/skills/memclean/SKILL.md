---
name: memclean
description: "User-invoked only: free memory on a WSL2 or Linux dev box without touching live chats: report, reap orphaned helpers, optionally stop idle dev servers, and print the root cache-reclaim command."
disable-model-invocation: true
argument-hint: "[--kill-dev] [--kill-chrome]"
---
Main chat, user-invoked. Follow `${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md` (orchestrator protocol); this is one small inline job.

Argument: `$ARGUMENTS`

## 1. Report (dry run, kills nothing)

Run `bash "${CLAUDE_PLUGIN_ROOT}/bin/boss-memclean"` (Bash, one call, exactly this line; add `$ARGUMENTS` only when the user gave `--kill-dev` or `--kill-chrome`).

- Lines: `RUN-START` (used, cached, anon memory), `SPARE` (checked, kept, with the reason), `IDLE`/`CANDIDATE` (dev server or Chrome that would go), `WOULD-KILL` (orphan MCP helper), `RUN-END` (counts, estimated MB freed).
- A live chat's MCP servers are never killed: they show `SPARE ... live-claude-parent`.
- Summarize in a table: memory now (used, cached, anon), what would be killed with its MB, what was spared and why in one phrase.

## 2. Confirm

One AskUserQuestion, the recommended option first and labelled (Recommended). The full dry-run output goes in the `preview` of the first option; never print it as text before the call.

| Option | What it does |
|---|---|
| Reap orphans (Recommended) | step 3 with `--apply` and no kill flags: only MCP and helper processes whose Claude session is gone |
| Reap orphans and idle dev servers | step 3 with `--apply --kill-dev`: also dev servers (next, vite) idle 2 h+ with no open connection for 3 runs; they must be restarted by hand |
| Report only | end here |

If `WOULD-KILL` is empty and there are no `CANDIDATE` or `IDLE` lines, skip the question and say there is nothing to reap.

## 3. Apply

Run `bash "${CLAUDE_PLUGIN_ROOT}/bin/boss-memclean" --apply` plus the chosen flag, one call. Report the `KILL` lines and the `RUN-END` line (killed count, MB freed, memory used after). The log is `memclean/memclean.log` in the boss data directory.

## 4. Page cache (root, optional)

Dropping page cache needs root and frees the most on WSL2 (the cache is held by Windows until released). Never run sudo yourself. Print this paste-ready block in a fenced code block as the last thing in your reply, with no question after it:

```bash
sudo sh -c 'sync; echo 1 > /proc/sys/vm/drop_caches; echo 1 > /proc/sys/vm/compact_memory'
```

When `Cached` in the report was 6 GB or more, also print the one-time install of the automatic version (root-owned script, root cron every 15 minutes, acts only at `Cached` of 6 GB or more and at most once an hour):

```bash
sudo install -o root -g root -m 0755 "${CLAUDE_PLUGIN_ROOT}/bin/boss-wsl-reclaim" /usr/local/sbin/wsl-reclaim && (sudo crontab -l 2>/dev/null; echo '*/15 * * * * /usr/local/sbin/wsl-reclaim # wsl-reclaim') | sudo crontab -
```
