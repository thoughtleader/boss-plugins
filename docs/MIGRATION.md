# Migrating from a hand-installed copy

This guide is for you if you already run the orchestrator by hand: scripts under `~/.claude/orchestrator`, `~/.claude/wfp` and `~/.claude/hooks`, hook entries in `settings.json`, and `/boss*` skills or `orch-*` agents under `~/.claude`. Moving to the plugin takes five steps. Do them in order.

## The one rule

**Remove the old hook entries from `settings.json` before you enable the plugin.** The plugin registers the same hooks through its own `hooks/hooks.json`. If both exist, every hook fires twice: two ledger lines per launch, and the usage policy counts each launch twice.

## 1. Back up and list the old hooks

```bash
cp ~/.claude/settings.json ~/.claude/settings.json.bak-pre-boss

RE='hooks/orch-(attempts|return-cap|stop-gate)\.js|hooks/orchestrator-guard\.js|hooks/role-write-guard\.js|wfp/wfp\.js'
jq -r --arg re "$RE" '.hooks // {} | to_entries[] | .key as $e | .value[] | .hooks[]?
  | select((.command // "") | test($re)) | "\($e)\t\(.command)"' ~/.claude/settings.json
```

The output lists each hook command the plugin replaces, with its event. Entries for other tools do not match and stay.

## 2. Remove them

```bash
jq --arg re "$RE" 'if .hooks then .hooks |= (
    map_values(map(.hooks |= map(select(((.command // "") | test($re)) | not)))
               | map(select((.hooks | length) > 0)))
    | with_entries(select(.value | length > 0))) else . end' \
  ~/.claude/settings.json > ~/.claude/settings.json.new \
  && mv ~/.claude/settings.json.new ~/.claude/settings.json
```

Run the step 1 listing again: it must print nothing. If your hooks use a command layout the pattern does not match, edit the entries by hand and keep the backup until step 5 passes.

Restart any running Claude Code session afterwards. Hooks are read at session start.

## 3. Install the plugin

```bash
claude plugin marketplace add <path to this repo>
claude plugin install boss@boss-plugins
```

Or inside Claude Code: `/plugin marketplace add <path>`, then `/plugin install boss@boss-plugins`.

## 4. Keep your data and policy

- **Runs, leases and the ledger stay where they are.** When `<config>/orchestrator/runs` exists, the plugin uses `<config>/orchestrator` as its data dir (see "Data directory" in the README). You do not copy anything. To use a different location, set `BOSS_DATA`.
- **`config.json` carries over.** The engine reads `<data>/config.json` as an overlay on its defaults, and the old file uses the same keys.
- **Usage policy does not carry over by itself.** The plugin reads `<data>/wfp.json`, not the old `wfp/config.json`. Copy the settings you changed (for example `defaultProfile` and any edited profiles) into `<data>/wfp.json`. Top-level keys replace the shipped defaults; objects such as `profiles` merge one level deep. The old `wfp/state` is short-lived session state and is not needed.
- **Your own worker rules go in `<data>/WORKER.local.md`.** The plugin's base worker contract is `templates/WORKER.md` and is replaced on every update. If your old `WORKER.md` held personal rules (identity, tools, ports), move those lines into the local file. See `docs/WORKER.local.example.md`.
- **Commit-scan settings** (`metrics.commitRepos`, `metrics.commitAuthor`) belong in `<data>/config.json`. The plugin ships them empty.

## 5. Check, then clean up

1. Start a new session and run `/boss:doctor`. Its `hooks` check warns if `settings.json` still registers any hook the plugin owns.
2. Launch one planned row and confirm the run's ledger gets exactly one line for it.
3. Once it works, remove what the plugin replaces:
   - the old skill directories (`boss`, `boss-approve`, `boss-away`, `boss-cost`, `boss-doctor`, `boss-runs`, `boss-status`, `boss-stop`, `orch-resume`) from `~/.claude/skills`;
   - the old `orch-*.md` files from `~/.claude/agents`. The plugin's agents are named `boss:orch-scout`, `boss:orch-builder`, and so on, so they do not clash, but two sets are confusing.
4. Update any note of yours that names the old commands.

| Old | New |
|---|---|
| `/boss` | `/boss:start` |
| `/boss-approve` | `/boss:approve` |
| `/boss-away` | `/boss:away` |
| `/boss-cost` | `/boss:cost` |
| `/boss-doctor` | `/boss:doctor` |
| `/boss-runs` | `/boss:runs` |
| `/boss-status` | `/boss:status` |
| `/boss-stop` | `/boss:stop` |
| `/orch-resume` | `/boss:resume` |
| `subagent_type: "orch-builder"` | `subagent_type: "boss:orch-builder"` |

Scripts that call `~/.claude/orchestrator/<x>.js` directly (a cron job, for example) keep working only while those files exist. Point them at the plugin's `lib/<x>.js` before you delete the old copies. `node <plugin dir>/lib/paths.js` prints where the plugin is installed and what it resolves.

## Rolling back

1. Disable or uninstall the plugin: `claude plugin uninstall boss@boss-plugins`.
2. Restore `settings.json` from the backup you made in step 1.

Your data dir is untouched by either step.
