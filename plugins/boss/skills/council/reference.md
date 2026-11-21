# Council: reference (read on demand)

The `council` skill runs `council.py ask`, which prints the preflight rows to name, `run`'s lines and a report block. This file holds what the chat needs only for a flag, a hand re-read or a follow-up. `council.py ask --help` has the flags.

## The separate commands (still supported)

`council.py preflight --free-only [flags] --question-file "$Q"` (free checks only), then `council.py run [flags] --question-file "$Q"` in the background: the old two-call flow. `run` repeats the preflight gate itself, canaries included, so its table may differ; exit 1 again means stop. `ask` = that `run`, with the gate before the run dir, a short table and the report block. Both take `--rulings-file`. Every form refuses a whole rulings or memory file: `ask` and `run` exit 2 before anything is sent, `preflight` shows a `private_file` FAIL row. By name always; a renamed copy only in part, when its content still has that file's shape (a copy with a line put in front, a short one, or a memory note without frontmatter passes): never pass a copy, write an excerpt.

## Re-read a run by hand

The report block's seat table is this script's table (tests check they agree). Use it to re-read a later `summary.json`, e.g. for late answers (set `RUN` to the run dir):

```
CAPPED=$(/usr/bin/grep -l 'hit its output-token cap' "$RUN"/seats/*.md 2>/dev/null | xargs -r -n1 basename -s .md | jq -R . | jq -sc .)
jq -r --argjson capped "${CAPPED:-[]}" '
  def row(r; kind): [kind, r.id, r.provider, (r.substituted.model // r.model), r.status, ((r.verdict // "-") | tostring),
      (if r.grounding then (if r.grounding.mode == "ungrounded" then "ungrounded" elif r.grounding.check
         then "grounded " + ((r.grounding.check.n_citations - r.grounding.check.n_bad) | tostring) + "/" + (r.grounding.check.n_citations | tostring)
         else "grounded" end) else "-" end),
      (r.total_s | if . == null then "-" else (. * 10 | round / 10 | tostring) end),
      ((r.truncated // false) | tostring),
      ((r.usage.output_tokens // "-") | tostring),
      ((r.usage.reasoning_tokens // "-") | tostring),
      ((r.usage.cost_usd // "-") | tostring),
      ([ (if r.status == "pending" then "PENDING" elif r.status != "ok" then "FAILED" else empty end),
         (if r.status == "unresolved" then "UNRESOLVED" else empty end),
         (if (r.answered_by // "original") != "original" then "BY_" + (r.answered_by | ascii_upcase) else empty end),
         (if (r.late // []) | length > 0 then "LATE" else empty end),
         (if r.truncated then "TRUNCATED" else empty end),
         (if r.grounded_ok == false then "UNVERIFIED_CITES" else empty end),
         (if r.salvaged then "SALVAGED" else empty end),
         (if r.substituted then "SUBSTITUTED" else empty end),
         (if (r.detail == "output_cap") or ((r.id // "") as $i | $capped | index($i)) then "CAPPED" else empty end)
       ] | join(",") | if . == "" then "-" else . end)];
  def sv(r): row({id: r.id, provider: r.provider, model: r.salvage.model, status: r.salvage.status,
      total_s: r.salvage.total_s, truncated: r.salvage.truncated, usage: r.salvage.usage,
      detail: r.salvage.detail}; "salvage");
  (["kind","seat","provider","model","status","verdict","grounding","total_s","truncated","out_tok","reasoning_tok","cost_usd","flags"],
   (.seats[] | row(.; "advisor"), (select(.salvage) | sv(.))),
   (if .chair then (.chair | .id = "chair" | row(.; "chair"), (select(.salvage) | sv(.))) else empty end)) | @tsv' "$RUN/summary.json" | column -t -s "$(printf '\t')"
jq -c '{cost_reported_usd, cost_unreported, verdicts, grounded} | with_entries(select(.value != null))' "$RUN/summary.json"
jq -r '.seats[] | select(.calls or .progress) | "\(.id): outcome \(.outcome // .status), answered_by \(.answered_by // "-"), calls " + ([(.calls // [])[] | "\(.call)=\(.status)/\(.role)"] | join(" ")) + (if .progress then ", progress \(.progress.signal) last \(.progress.last_progress_s // "-") s, \(.progress.events // "-") events" else "" end)' "$RUN/summary.json"
jq -c '{phase, runner_pid, below_quorum, pending, late, late_pending, late_synthesis, unavailable} | with_entries(select(.value != null))' "$RUN/summary.json"
```

Then `jq -c '.critique' "$RUN/summary.json"`, `jq -c .quorum "$RUN/summary.json"` and the first ~15 lines of `$RUN/chair/synthesis.md` (a `> BELOW QUORUM` header comes first when it applies).

## The columns

- `verdict` is the seat's own closing line (`APPROVE`, `REVISE`, `BLOCK`, `n/a`; `-` = none given or a non-answering row); `verdicts` is the tally. Display only: never stop, retry or reweigh because of it, never add a verdict a seat did not write.
- `grounding` (grounded runs only; `-` otherwise): `ungrounded` (text only), `grounded` (no citation to check) or `grounded N/M` = N of M of the seat's `path:line` citations and quotes held against the files. `summary.json` `seats[].grounding.check.bad` lists the ones that failed; the `grounded` object lists the grounded and ungrounded seats and the totals.
- `out_tok` is the call's output tokens with `reasoning_tok` already inside it (do not add them); `cost_usd` is what the provider billed. `-` = nothing reported (unknown, not zero): codex and agy are subscription CLIs and never report a cost. A `salvage` row is the one follow-up call of the seat or chair above it (its own tokens and cost; the seat row keeps the first run's). `cost_reported_usd` sums the reported costs only; `cost_unreported` lists the calls (`<id>`, `<id>.salvage`) that ran without one.
- The calls line (hedged seats only, `[hedge]`/`[fallback]` on): each call as `<call>=<status>/<role>`, role `used` (the chair got it), `alternate` (an answer the chair did not need: one answer per seat counts), `late`, `failed`, `running` or `observer_lost`; `progress` is the call's liveness signal, its last progress in seconds since start and the events seen.
- The phase line: `phase` (`supervising` while the runner still has calls running, `done` once every call ended; `runner_lost` while a call of a run whose runner died is still running, then `done` with `runner_lost: true` once the orphan sweep has settled the last one; `orphans[]` lists each swept call), `pending`, `late` (with `at_s`), `late_pending` (calls still running after the chair started), `late_synthesis` (the exit-4 pending case: `waiting`, then `ok`, `chair_failed`, `no_answer`, or `runner_lost` when the runner died before the first answer: no chair ran and none will) and `unavailable` (out-of-quota seats skipped).
- Flags: PENDING, UNRESOLVED, BY_HEDGE / BY_FALLBACK / BY_STANDIN, SUBSTITUTED (`summary.json` `substituted.reason`; `preflight:...` when the preflight found the model dead), LATE (`seats/<id>.<call>.late.md`), FAILED, TRUNCATED, CAPPED (`[TRUNCATED: ...]` in `seats/<id>.md`), SALVAGED (the chair row too: its synthesis is marked `> SALVAGED`), UNVERIFIED_CITES. The report block prints the meaning of each flag present. FAILED statuses: `idle_kill`, `ceiling_kill`, `deadline_kill`, `exit_<n>`, `adapter_error`, `model_mismatch`, `signal_kill`, `cli_timeout`, `invalid_answer` (tool-call markup or near-empty text; salvaged if it could be), `quota_exhausted` (no retry; preflight marks it unavailable until its reset), `unresolved`.

## Critique round (`--depth standard`)

`summary.json` has `depth` and `critique` (absent on a quick run). Say whether the round ran (`ran: false` + `reason`, e.g. fewer than 2 answers), who critiqued (`critics`), whose critique failed (`failed`; a failed critique is not a failed run), who changed their view (`changed_view`) and how verdicts moved (`verdict_changes`; `revised_verdicts` is the tally after). A seat's critique is `seats[].critique` and `$RUN/seats/<id>.critique.md`. Critics saw only letters (`letters` maps them to seat ids); the chair was told the key. It is never a vote or a gate: the exit codes are round 1's and the chair's.

## Drift rows

Provider `drift`, free, never blocking. The preflight compares the effective settings of every seat (resolved argv and env, every config value, the codex config and model defaults, CLI versions and binary hashes, `agy models` labels, OpenRouter fields, pins) with the accepted baseline (`<state_dir>/drift/last.json`, state dir `~/.claude-council` unless the config sets `state_dir`). Alarms are sticky: a preflight never moves the baseline, so a `drift:<item>` WARN (`CHANGED old -> new`, `NEW`, `GONE`, `NOT COLLECTED`) repeats until it is accepted. `drift PASS` = first pass (seeds the baseline) or no change. `council.py drift` shows the diff (free; exit 0 no change, 1 changed). `council.py drift --accept` accepts all pending changes, `council.py drift --accept <key> [<key>...]` just those (`*` matches any text); exit 0 once nothing is left raised. Accept only changes you have named to the user: a CLI self-update or an unexpected config change must not be accepted unseen. `--record` is an alias of a bare `--accept`.

## Orphan rows

Provider `orphans`, free, WARN, never blocking: every `council.py` start first sweeps the watch calls whose runner is gone (killed or crashed). `orphan:<seat>.<call>` says either `LIVE ... age ...: still running after its runner was lost; left running, never killed` (a paid call with no supervisor; only the user may stop it, with `kill -TERM <pid>`), or `ENDED ... answer appended as late` / `no answer, recorded unresolved` (the run's `summary.json` lists it under `orphans`, and `late[]` when an answer was appended). The sweep never kills or relaunches anything. `council.py reap --orphans` runs just the sweep.

## Launcher and runner

`run` (and `ask`) is a launcher: it returns as soon as the synthesis is ready (or with exit 4 `pending: no answers yet`). A detached runner (`runner_pid` in `summary.json`, log `$RUN/runner.log`) keeps supervising the calls still running. They are never killed; each ends on its own, and a late answer is appended to `chair/synthesis.md` as a `> LATE: seat <id> (<call>) answered at <s> s after synthesis; not used by the chair` footer (the chair never re-runs). Do not wait for the runner and do not kill it. The manual stop is `kill -TERM <runner_pid>` (stops every call, keeps the partials): only when the user asks. Closing the chat or terminal, or a signal to the launcher, never stops the runner (the launcher prints `launcher stopped by signal N before the synthesis; runner <pid> keeps running ...` and exits 128+N). A watch call running longer than 2x its soft deadline is listed in `runner.log` (`long_watch:`) and `summary.json` `long_watch[]`; nothing is killed.

## Grounded runs

`--grounded [<dir>]` (`<dir>` defaults to the cwd's git root): claude seats (Read/Grep/Glob only) and codex seats (`-s read-only`) read the repository and cite `path:line` with a quoted snippet, each checked against the files. `--attach <image>` (png, jpg, gif, webp; repeatable; needs `--grounded`) lets those seats open the image. OpenRouter and agy seats answer from the text only and are labelled `ungrounded`. The preflight shows `grounded_root` and one `grounded:<seat>` row per seat (PASS = read-only file tools, WARN `ungrounded: <reason>`). A path inside the root is a `question_path` WARN; outside it still FAILs. A grounded seat takes longer; nothing is cut short for that.
