# Recommended main-chat settings

The boss plugin ships its protocol in `plugins/boss/skills/start/PROTOCOL.md`; `/boss:start` loads it. What follows is optional text for your own `CLAUDE.md` (user-level or project-level) that makes the main chat behave well as an orchestrator. Paste what you want and edit freely: these are preferences, not requirements of the plugin.

```markdown
## Orchestration

- Run multi-step jobs with `/boss:start <job>`. The main chat plans and dispatches; workers do the building.
- Do small, contained work (about 5 tool calls or fewer, known files) inline. Delegate reading across many files, test or build output, browser sessions, large diffs and independent review.
- Keep run state in the run folder (plan status, decisions, result paths and SHAs), so a context compaction loses nothing.

## Asking questions

- Ask for decisions with an interactive question tool when one is available, instead of burying a question in prose.
- Give 2 to 4 options with the recommended one first and labelled "(Recommended)".
- Put everything a question refers to (a plan table, a draft, a diff) inside the question or its options, so it can be answered without scrolling back.
- Close a response with a concrete next step to pick, unless a run is still in progress and no decision is due.
```

Notes:

- The protocol itself does not depend on this snippet. Without it, `/boss:start` and the other `/boss:*` commands still work.
- Model and effort per role come from the usage presets (`node "$BOSS_LIB/../wfp/wfp.js" profile show`), not from `CLAUDE.md`.
- Personal rules for workers (git identity, ports, preferred tools) belong in `WORKER.local.md` in your data dir; see `docs/WORKER.local.example.md`.
