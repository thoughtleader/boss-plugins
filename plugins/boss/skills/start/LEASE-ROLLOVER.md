# Boss lease, resume and rollover

Moved out of PROTOCOL.md; read it before a rollover or when a lease command refuses. `$BOSS_LIB` is the engine path `/boss:start` printed.

## Lease, resume and rollover

Each run has its own lease at `<data>/leases/<run>.json`, so two sessions never drive one run at once.

- `node "$BOSS_LIB/manifest.js" lease acquire --run <run> --sid=$WFP_SID` when a run starts or resumes (`launch` also takes it when nobody holds it).
- `lease renew --sid=$WFP_SID` (launches and prompts also renew it), `lease check --run <run> --sid=$WFP_SID`, `lease release`.
- Lease commands take the run folder name, never a path.
- If another live session holds the run, `acquire` and `launch` refuse (exit 3). Ask the user before `--force`.

When context runs long and nothing is running (`/boss:stop` does these steps):

1. Run `node "$BOSS_LIB/manifest.js" reconcile --run <run> --sid=$WFP_SID`. Every attempt that is not `done` with `next: advance` goes in HANDOFF's running table. A `next: gate` row goes there with its status (for example `gaps_found`) in Notes, and its dependents stay blocked.
2. Write `<run>/HANDOFF.md` from the handoff template (5 KB or less).
3. Run `node "$BOSS_LIB/manifest.js" lease release --sid=$WFP_SID`; it frees every lease this session holds.
4. Tell the user to clear the chat and run `/boss:resume <run name>` (HANDOFF's `run:`, never a path). The command loads HANDOFF.md, the plan's Status table and any work-state list, re-acquires the lease, reconciles running rows and continues. Without a name it lists resumable runs and asks; it never picks one itself (`/boss:runs` shows the same list).

When the run ends: `node "$BOSS_LIB/manifest.js" lease release --run <run> --sid=$WFP_SID`.

A rollover is never a reason to stop on its own. Mention it only as one extra option inside a question already being asked at a real decision point.
