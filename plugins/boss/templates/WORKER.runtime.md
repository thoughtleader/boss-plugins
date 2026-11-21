# Runtime rules (UI checks, servers, background jobs; read after WORKER.md)

- UI checks: use the browser tooling your brief or the local house rules name. Act only on pages you opened yourself. Prove UI live with real input (wheel, click, Esc, hard load) at each viewport the brief names; use a production build when the bug is in load or paint. After a runtime change: reload, read console errors and warnings, and read network requests twice at least 3 s apart (same count = no loop).
- Stop only the dev server you started: kill its PID from `ss -ltnp | grep :<port>`. Never `pkill -f next-server` (or any shared pattern); it kills other agents' servers.
- Build copies (e.g. `.next` copies) go in the session scratchpad, not in the repo.

## Background process ownership (reporting only)

- When starting a background job or dev server for an Orch-Attempt, register its PID immediately:
  `node "$BOSS_LIB/processes.js" register --pid "$job_pid" --attempt '<Orch-Attempt ID>' --session "$WFP_SID" --purpose 'why this job exists' --keep no`
- Use `--keep yes` only when the server/job is intentionally needed after the task, and explain that purpose. Register the actual long-lived process where possible; a short-lived wrapper cannot identify children after reparenting. Never guess ownership of an existing shared process.
- Before returning, run `node "$BOSS_LIB/processes.js" report --attempt '<Orch-Attempt ID>'` and mention surviving jobs in the result file. Main-turn Stop and mapped SubagentStop hooks also save reports under `<data>/process-ownership/reports/`.
- Reports never stop processes and never treat an expired lease or a stopped agent as permission to terminate anything. PID identity includes boot ID and process start time. Existing unregistered jobs are outside coverage.
