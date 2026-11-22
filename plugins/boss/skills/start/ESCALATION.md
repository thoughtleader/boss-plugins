# Boss escalation by cause

Moved out of PROTOCOL.md; read it when a row fails, is weak or needs a follow-up. `$BOSS_LIB` is the engine path `/boss:start` printed.

## Escalation by cause

The ladder is medium, then high, then xhigh, one step per row. Get the next step from `node "$BOSS_LIB/../wfp/wfp.js" escalate <role> [<model>/<effort>]`, passing the row's role and the step that ran, for example `escalate implementation sonnet/high`.

- **Findings on a sound approach** (a review found fixable problems, the direction is right): continue the same builder with `manifest.js launch … --continue <id> --cause review-findings`, then SendMessage. Same model and effort.
- **Failed, weak or wrong-approach output** (it missed its done-when, was shallow, needed heavy patching, or took the wrong approach): a fresh agent one step up, launched with `--cause failed`. A continuation keeps its model and effort and carries the failed attempt's assumptions, so it is the wrong tool here.
- **A user taste round** (the user asks to change output that met its done-when): `--continue <id> --tune` records `owner-tune` and keeps the predecessor's first-pass. **A tooling fault** (a tool, hook or harness failed, not the worker): relaunch with `--cause tooling`.
- The retry's launch sets the failed attempt's first-pass to n with its cause, and the retry's ledger line gets `escalated` from its step-up, so neither needs a hand-made record; `ledger.js close --attempt <id> --first-pass n --outcome retry --cause <same>` adds the outcome. A failure at the stepped-up level is blocked and goes to the user.
- Never re-run a repeated failure without new evidence: rescope or use a stronger model.
