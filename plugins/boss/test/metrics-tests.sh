#!/usr/bin/env bash
# metrics.js + ledger.js sync/report tests (orchestrator v2 fixes, row 13b; design 6-d4-design.md §12).
# The §12.6 fixture (2 runs, ledger lines with and without id, manifest events incl. row 12's launch key form, one bad ledger
# line, one torn manifest line); sync idempotence (counts, .dump hash, upserted 0), --rebuild, rebuild-auto, deletes, rollback;
# report parity with show (role lines, step columns, first-pass columns, filtered); the Codex group; no node:sqlite on the
# write paths (preload probe with a positive control, hook/manifest/wfp grep, ingest with metrics.js removed in a temp copy).
# Runs on scratch ORCH_DIR / ORCH_RUNS_DIR / ORCH_METRICS_DB; never the live ACTIVE, ACTIVE.lease, run ledgers or metrics.db.
# Usage: bash ~/.claude/orchestrator/test/metrics-tests.sh   (exit 0 = all pass)
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib/compat.sh"   # on macOS: GNU date/stat/touch/md5sum/wc shims (test/lib)
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"   # the checkout this suite tests (a worktree runs its own code)
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
O="$CL/lib"; L="$O/ledger.js"; M="$O/manifest.js"; LIVE="$HOME/.claude/orchestrator"   # O = code under test; LIVE = live files, checked last
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export ORCH_WFP_STATE="$T/wfp" ORCH_DIR="$T/orch" ORCH_RUNS_DIR="$T/orch/runs" ORCH_PROJECTS_DIR="$T/projects" ORCH_METRICS_DB="$T/metrics.db" ORCH_SETTINGS="$T/settings.json" ORCH_COMMIT_HOME="$T/nohome"
unset WFP_SID BOSS_PROGRESS_DIR; mkdir -p "$ORCH_RUNS_DIR" "$ORCH_PROJECTS_DIR"
DB="$ORCH_METRICS_DB"
sum(){ if [ -e "$1" ]; then md5sum < "$1"; else echo absent; fi; }
# Live ACTIVE is peer-written (any chat may write or empty it), so it is checked by content, never md5: if it existed at the start it still exists, and it holds
# neither this suite's scratch root nor a runs/<run> path component of a run this suite made (a substring test would trip on short run names).
ACTIVE_WAS=$([ -e "$LIVE/ACTIVE" ] && echo 1 || echo 0)
activecheck(){
  local a="$LIVE/ACTIVE" n=0 r d e
  if [ ! -e "$a" ]; then [ "$ACTIVE_WAS" = 1 ] && echo gone || echo ok; return; fi
  n=$(grep -cF -- "$T" "$a")
  while IFS= read -r r; do
    [ -n "$r" ] || continue
    e=$(printf '%s' "$r" | sed 's/[][\.*^$+?(){}|/]/\\&/g')
    n=$((n + $(grep -cE -- "(^|/)runs/$e(/|[[:space:]]|\$)" "$a")))
  done < <(find "$T" -type d -name runs 2>/dev/null | while IFS= read -r d; do ls -1 "$d"; done | sort -u)
  [ "$n" = 0 ] && echo ok || echo "leak:$n"
}
# Peer-written live files (leases, run manifests and ledgers, errors.log) change under live sessions, so md5 would flake: they are checked by content.
# liveleak <live dir> <scratch root>: counts live records (lease, manifest line, ledger line), live run dirs and distinctive errors.log lines that
# name a sid or run this suite made (read back from <scratch root>). SUITE_SIDS / SUITE_RUNS keep the derived lists (JSON arrays) for the caller.
liveleak(){
  local SEL='objects | select(([.session?,.sid?,.holder?,.prev_holder?] | map(strings) | any(IN($s[]))) or ([.run?] | map(strings) | any(IN($r[]))))' hit r pats
  SUITE_SIDS=$({ find "$2" -type f \( -name ACTIVE.lease -o -path '*/leases/*.json' \) -exec cat {} + 2>/dev/null | jq -r 'objects | [.session?,.sid?,.holder?,.prev_holder?] | map(strings) | .[]' 2>/dev/null
    find "$2" -type f \( -name manifest.jsonl -o -name ledger.jsonl \) -exec cat {} + 2>/dev/null | jq -R -r 'fromjson? | objects | [.session?,.sid?,.holder?,.prev_holder?] | map(strings) | .[]' 2>/dev/null; } | jq -R . | jq -s 'unique')
  SUITE_RUNS=$(find "$2" -type d -name runs 2>/dev/null | while IFS= read -r r; do ls -1 "$r"; done | jq -R . | jq -s 'unique')
  hit=$({ cat "$1"/ACTIVE.lease "$1"/leases/*.json 2>/dev/null | jq -c --argjson s "$SUITE_SIDS" --argjson r "$SUITE_RUNS" "$SEL" 2>/dev/null
    cat "$1"/runs/*/manifest.jsonl "$1"/runs/*/ledger.jsonl 2>/dev/null | jq -R -c --argjson s "$SUITE_SIDS" --argjson r "$SUITE_RUNS" "fromjson? | $SEL" 2>/dev/null; } | wc -l | tr -d ' ')
  for r in $(jq -r '.[]' <<<"$SUITE_RUNS"); do [ -e "$1/runs/$r" ] && hit=$((hit+1)); done
  pats=$({ jq -r '.[] | select(length>=8)' <<<"$SUITE_SIDS"; jq -r '.[] | select(length>=5)' <<<"$SUITE_RUNS"; echo "$2"; })
  [ -f "$1/errors.log" ] && hit=$((hit + $(grep -cF -f <(printf '%s\n' "$pats" | grep -v '^$') "$1/errors.log")))
  echo "$hit"
}
q(){ sqlite3 "$DB" "$1"; }
dumphash(){ sqlite3 "$DB" '.dump runs attempts events sources schema_version' | md5sum; }
counts(){ q "SELECT (SELECT COUNT(*) FROM attempts)||' '||(SELECT COUNT(*) FROM events)||' '||(SELECT COUNT(*) FROM runs)"; }
lastlog(){ q "SELECT $1 FROM sync_log ORDER BY id DESC LIMIT 1"; }

echo "== fixture (§12.6)"
mkdir -p "$ORCH_RUNS_DIR/m-a" "$ORCH_RUNS_DIR/m-b"
ln(){ # ts id run row role agent model effort tokens tools ms firstPass retries escalated; "-" id = no id key
  jq -cn --arg ts "$1" --arg id "$2" --arg run "$3" --arg row "$4" --arg role "$5" --arg agent "$6" --arg model "$7" --arg effort "$8" \
    --argjson tokens "$9" --argjson tools "${10}" --argjson ms "${11}" --argjson fp "${12}" --argjson retries "${13}" --argjson esc "${14}" \
    '{ts:$ts} + (if $id == "-" then {} else {id:$id} end) + (if env.MID then {model_id:env.MID} else {} end) + {run:$run, row:($row | if test("^[0-9]+$") then tonumber else . end), role:$role,
      agent:$agent, model:$model, effort:$effort, mode:"subagent", tokens:$tokens, tools:$tools, ms:$ms, repo:"r", area:"a",
      firstPass:$fp, retries:$retries, escalated:$esc}'; }
LA="$ORCH_RUNS_DIR/m-a/ledger.jsonl"; LB="$ORCH_RUNS_DIR/m-b/ledger.jsonl"
{ ln 2026-10-01T00:00:01.000Z - m-a 1 implementation orch-builder sonnet high 100000 10 60000 false 0 false
  ln 2026-10-01T00:00:02.000Z m-a#1.2 m-a 1 implementation orch-builder opus high 50000 5 30000 true 1 true
  MID=claude-sonnet-5-5 ln 2026-10-01T00:00:03.000Z m-a#2.1 m-a 2 implementation orch-builder sonnet high 40000 4 20000 true 0 false
  echo '{broken'
  ln 2026-10-01T00:00:05.000Z m-a#4.1 m-a 4 implementation orch-builder opus unknown 70000 null null null 0 false
  ln 2026-10-01T00:00:06.000Z m-a#4.2 m-a 4 implementation orch-builder opus high 30000 3 10000 true 1 false; } > "$LA"
{ ln 2026-10-01T00:00:07.000Z m-b#1.1 m-b 1 review orch-reviewer opus high 80000 8 50000 null 0 false
  ln 2026-10-01T00:00:08.000Z m-b#7b.1 m-b 7b review codex gpt-6-astra ultra 120564 null null null 0 false; } > "$LB"
MA="$ORCH_RUNS_DIR/m-a/manifest.jsonl"
{ echo '{"id":"m-a#e1","seq":1,"type":"launch","ts":"2026-10-01T00:00:00.500Z","run":"m-a","attempt":"m-a#1.2","row":"1","key":"m-a#1.2:opus/high#2","via":"cli","session":"S1","n":2,"role":"implementation","model":"opus","effort":"high","continues":null}'
  echo '{"id":"m-a#e2","seq":2,"type":"start","ts":"2026-10-01T00:00:00.600Z","run":"m-a","attempt":"m-a#1.2","row":"1","key":"tu-1","via":"pretool","session":"S1","tool_use_id":"tu-1","agent":"orch-builder","model":"opus","effort":"high"}'
  echo '{"id":"m-a#e3","seq":3,"type":"stop","ts":"2026-10-01T00:00:02.000Z","run":"m-a","attempt":"m-a#1.2","row":"1","key":"t1:finished:5:30000","via":"cli","session":"S1","task_id":"t1","tool_use_id":"tu-1","status":"completed","phase":"finished","final":true,"tokens":50000,"tools":5,"ms":30000,"effort_observed":"high","result_bytes":700}'
  printf '%s' '{"id":"m-a#e4","seq":4,"type":"st'; } > "$MA"

echo "== sync: counts, bad lines, mapping"
o=$(node "$L" sync 2>"$T/sync.err"); st=$?
ok "sync exits 0" "$st" "0"
ok "sync stderr is empty (ExperimentalWarning silenced)" "$(cat "$T/sync.err")" ""
ok "sync names the scratch db, mode sync" "$(echo "$o" | grep -c "^ledger: synced $DB (sync): attempts 7, events 3, runs 2;")" "1"
ok "attempts 7, events 3, runs 2" "$(counts)" "7 3 2"
ok "bad_lines 2 (ledger {broken + torn manifest line), skipped 0, dup 0" "$(lastlog "bad_lines||' '||skipped_lines||' '||dup_ids")" "2 0 0"
ok "bad lines counted per file in detail" "$(lastlog detail | jq -c '[.["m-a/ledger.jsonl"].bad, .["m-a/manifest.jsonl"].bad, .["m-b/ledger.jsonl"].bad]')" "[1,1,0]"
ok "line without id gets the derived id m-a#1.1 (id_derived 1, n 1)" "$(q "SELECT id_derived||' '||n||' '||src_line FROM attempts WHERE id='m-a#1.1'")" "1 1 1"
ok "explicit ids kept (id_derived 0)" "$(q "SELECT COUNT(*) FROM attempts WHERE id_derived=0")" "6"
ok "row stored as text (7b and 1)" "$(q "SELECT group_concat(row, ',') FROM (SELECT row FROM attempts WHERE run='m-b' ORDER BY src_line)")" "1,7b"
ok "firstPass/escalated -> 1/0/NULL" "$(q "SELECT coalesce(first_pass,'N')||coalesce(escalated,'N') FROM attempts WHERE id IN ('m-a#1.1','m-a#1.2','m-b#1.1') ORDER BY id")" "$(printf '00\n11\nN0')"
ok "is_codex only for gpt-6-astra; effort_known 0 only for opus/unknown" "$(q "SELECT group_concat(id, ',') FROM attempts WHERE is_codex=1")|$(q "SELECT group_concat(id, ',') FROM attempts WHERE effort_known=0")" "m-b#7b.1|m-a#4.1"
ok "source_ref <run>/ledger.jsonl:<line> (line 6 after the bad line 4)" "$(q "SELECT source||' '||source_ref FROM attempts WHERE id='m-a#4.2'")" "ledger m-a/ledger.jsonl:6"
ok "raw holds the whole line" "$(q "SELECT json_extract(raw,'\$.agent') FROM attempts WHERE id='m-b#7b.1'")" "codex"
ok "row 12 launch key <attempt>:<model>/<effort>#<ordinal> stored as is" "$(q "SELECT key FROM events WHERE type='launch'")" "m-a#1.2:opus/high#2"
ok "event columns: session->session_id, result_bytes->bytes, final 1" "$(q "SELECT session_id||' '||bytes||' '||final||' '||effort_observed FROM events WHERE id='m-a#e3'")" "S1 700 1 high"
ok "events source_ref and seq" "$(q "SELECT group_concat(seq||'@'||source_ref, ',') FROM events")" "1@m-a/manifest.jsonl:1,2@m-a/manifest.jsonl:2,3@m-a/manifest.jsonl:3"
ok "runs: m-a has ledger+manifest, 5 attempts, 3 events, ts span" "$(q "SELECT has_ledger||has_manifest||' '||n_attempts||' '||n_events||' '||first_ts||' '||last_ts||' '||source_ref FROM runs WHERE run='m-a'")" "11 5 3 2026-10-01T00:00:00.500Z 2026-10-01T00:00:06.000Z m-a/"
ok "schema_version 6" "$(q "SELECT group_concat(version) FROM schema_version")" "6"
ok "model_id column carries the ledger line's resolved model; null when the line has none" "$(q "SELECT group_concat(id||'='||coalesce(model_id,'null'), ',') FROM attempts WHERE id IN ('m-a#2.1','m-a#1.2') ORDER BY id")" "m-a#1.2=null,m-a#2.1=claude-sonnet-5-5"
ok "sources: ledger, manifest, fs, transcript, wfp_tel active; 3 reserved" "$(q "SELECT group_concat(source, ',') FROM (SELECT source FROM sources WHERE status='active' ORDER BY source)")|$(q "SELECT COUNT(*) FROM sources WHERE status='reserved'")" "fs,ledger,manifest,transcript,wfp_tel|3"
ok "tx tables are empty on an empty projects dir; prices seeded; reserved tables empty" "$(q "SELECT (SELECT COUNT(*) FROM tx_calls)+(SELECT COUNT(*) FROM wfp_telemetry)+(SELECT COUNT(*) FROM gsd_metrics)+(SELECT COUNT(*) FROM codex_runs)+(SELECT COUNT(*) FROM tx_files)")|$(q "SELECT group_concat(family) FROM prices")" "0|fable,haiku,opus,sonnet"
ok "views: the 4 report views + the v_tx_* views" "$(q "SELECT group_concat(name, ',') FROM (SELECT name FROM sqlite_master WHERE type='view' ORDER BY name)")" "v_attempt_keys,v_first_pass,v_rows,v_tokens_per_accepted,v_tx_advisor,v_tx_agent_attempt,v_tx_attempt_totals,v_tx_by_model_effort,v_tx_by_role,v_tx_by_step,v_tx_call_attr,v_tx_call_run,v_tx_call_w,v_tx_call_w_all,v_tx_helper,v_tx_run_totals,v_tx_sessions,v_tx_unattributed"
ok "v_attempt_keys joins the attempt to its tool_use_id" "$(q "SELECT group_concat(DISTINCT tool_use_id) FROM v_attempt_keys WHERE attempt='m-a#1.2'")" "tu-1"

echo "== idempotence, --rebuild, rebuild-auto"
H1=$(dumphash); C1=$(counts)
o=$(node "$L" sync 2>&1); st=$?
ok "second sync exits 0, upserted 0, deleted 0" "$st|$(echo "$o" | grep -o 'upserted [0-9]*, unchanged [0-9]*, deleted [0-9]*')" "0|upserted 0, unchanged 12, deleted 0"
ok "second sync: same row counts" "$(counts)" "$C1"
ok "second sync: same .dump hash (sync_log excluded)" "$(dumphash)" "$H1"
ok "sync_log gained exactly one row" "$(q "SELECT COUNT(*) FROM sync_log")" "2"
o=$(node "$L" sync --rebuild 2>&1); st=$?
ok "--rebuild exits 0, mode rebuild" "$st|$(echo "$o" | grep -c '(rebuild): attempts 7, events 3, runs 2;')" "0|1"
ok "--rebuild: same .dump hash" "$(dumphash)" "$H1"
ok "--rebuild: sync_log recreated with one row" "$(q "SELECT COUNT(*)||' '||max(mode) FROM sync_log")" "1 rebuild"
q "UPDATE schema_version SET version = 0"
o=$(node "$L" sync 2>&1); st=$?
ok "schema_version mismatch -> rebuild-auto, same data" "$st|$(echo "$o" | grep -c '(rebuild-auto): attempts 7')|$(dumphash)" "0|1|$H1"
ok "sync --bogus refused (exit 2)" "$(node "$L" sync --bogus >/dev/null 2>&1; echo $?)" "2"

echo "== report vs show (parity on the same fixture)"
S=$(node "$L" show --last 1000 2>/dev/null)
RT=$(node "$L" report tokens-per-accepted 2>&1); st=$?
RF=$(node "$L" report first-pass 2>&1); st2=$?
ok "report tokens-per-accepted / first-pass exit 0" "$st|$st2" "0|0"
ok "role lines: report tokens-per-accepted == show (claude then codex)" "$(diff <(echo "$S" | grep '^role ') <(echo "$RT" | grep '^role ') && echo same)" "same"
ok "role lines: report first-pass == show" "$(diff <(echo "$S" | grep '^role ') <(echo "$RF" | grep '^role ') && echo same)" "same"
ok "implementation role line (escalated retry counted once, unknown-effort retry excluded)" "$(echo "$RT" | grep -c '^role implementation: 3 rows, 4 attempts, tokens 220.0k, accepted 3, tok/accepted 73.3k$')" "1"
ok "review role lines: claude and codex" "$(echo "$RT" | grep -c '^role review: 1 rows, 1 attempts, tokens 80.0k, accepted 0, tok/accepted -$')|$(echo "$RT" | grep -c '^role review: 1 rows, 1 attempts, tokens 120.6k, accepted 0, tok/accepted -$')" "1|1"
# step tables: show's columns 1-4 (model/effort, rows, tokens, tok/accepted) and 1,5-8 (first-pass, avg tools, avg time)
stepsShow(){ echo "$S" | sed -n '/^stats: /,$p' | sed 's/^no outcomes/no_outcomes/; s/^no outcome /no_outcome /; s/ no outcomes / no_outcomes /' | grep -vE '^(role |== |unknown effort|stats: |\* |model/effort|$)'; }
stepsRep(){ echo "$1" | sed -n '2,$p' | sed 's/^no outcome /no_outcome /; s/ no outcomes / no_outcomes /' | grep -vE '^(role |== |unknown effort|dup ids|retry kind |\* |model/effort|$)'; }
ok "step columns (rows, tokens, tok/accepted) == show" "$(diff <(stepsShow | awk '{print $1,$2,$3,$4}') <(stepsRep "$RT" | awk '{print $1,$2,$3,$4}') && echo same)" "same"
ok "first-pass columns (first-pass, avg tools, avg time) == show" "$(diff <(stepsShow | grep -v '^no_outcome ' | awk '{print $1,$5,$6,$7,$8}') <(stepsRep "$RF" | awk '{print $1,$3,$4,$5,$6}') && echo same)" "same"
ok "opus/high: rows 2, tokens 180.0k, tok/accepted 90.0k" "$(echo "$RT" | grep -cE '^opus/high +2 +180\.0k +90\.0k$')" "1"
ok "sonnet/high: rows 1, tokens 40.0k (the escalated row counts under opus, once)" "$(echo "$RT" | grep -cE '^sonnet/high +1 +40\.0k +40\.0k$')" "1"
ok "first-pass: opus/high 2/2 100%*, sonnet/high 1/2 50%*" "$(echo "$RF" | grep -cE '^opus/high +2 +2/2 100%\*')|$(echo "$RF" | grep -cE '^sonnet/high +2 +1/2 50%\*')" "1|1"
ok "opus/high listed before sonnet/high (rank order)" "$(echo "$RT" | grep -E '^(opus|sonnet)/high ' | head -1 | cut -d' ' -f1)" "opus/high"
ok "unknown effort: 1" "$(echo "$RT" | grep -c '^unknown effort: 1 ')" "1"
ok "Codex row in its own group, never in the claude section" "$(echo "$RT" | awk '/^== codex ==/{c=1} c&&/^gpt-6-astra\/ultra /{n++} END{print n+0}')|$(echo "$RT" | awk '/^== codex ==/{exit} /^gpt-/{n++} END{print n+0}')" "1|0"
for f in "--role implementation" "--role review" "--run m-a" "--run m-b" "--repo R" "--area a" "--repo r --area A --role review"; do
  ok "filtered parity ($f): role lines + step columns == show" "$(diff <(node "$L" show --last 1000 $f 2>/dev/null | sed -n '/^stats: /,$p' | grep -E '^(role |[a-z0-9.-]+/[a-z]+ )' | grep -v '^model/effort ' | awk '/^role /{print; next} {print $1,$2,$3,$4}') \
    <(node "$L" report tokens-per-accepted $f 2>/dev/null | grep -E '^(role |[a-z0-9.-]+/[a-z]+ )' | grep -v '^model/effort ' | awk '/^role /{print; next} {print $1,$2,$3,$4}') && echo same)" "same"
done
ok "report with no match says so (exit 0)" "$(node "$L" report first-pass --run nosuch 2>&1 | grep -c '^ledger: no attempts match (run=nosuch)')" "1"
J=$(node "$L" report tokens-per-accepted --json 2>/dev/null)
ok "--json: codex family role review, claude implementation tok/accepted 73333" "$(echo "$J" | jq -c '[.families.codex[0].role, (.families.claude[] | select(.role=="implementation") | .tok_per_accepted), .unknown_effort]')" '["review",73333,1]'
ok "report bogus name refused (exit 2)" "$(node "$L" report bogus >/dev/null 2>&1; echo $?)" "2"
ok "report --role bogus refused (exit 2)" "$(node "$L" report first-pass --role bogus >/dev/null 2>&1; echo $?)" "2"
ok "report leaves the data unchanged (its sync upserts nothing)" "$(dumphash)" "$H1"
echo "== win 6: report --no-sync reads and never writes metrics.db"
H2=$(md5sum < "$DB"); ST0=$(stat -c %Y "$DB")
RN=$(node "$L" report first-pass --no-sync 2>&1); st=$?
ok "report --no-sync: exit 0 with the same role lines as the syncing report" "$st|$(diff <(echo "$RF" | grep '^role ') <(echo "$RN" | grep '^role ') && echo same)" "0|same"
ok "report --no-sync leaves metrics.db byte-identical" "$(md5sum < "$DB")" "$H2"
mv "$DB" "$DB.keep"
ok "report --no-sync with no db: exit 2, says to sync, creates nothing" "$(node "$L" report first-pass --no-sync 2>&1 | grep -c "no metrics.db at .* yet; run 'ledger.js sync'")|$(node "$L" report first-pass --no-sync >/dev/null 2>&1; echo $?)|$([ -e "$DB" ] && echo created || echo absent)" "1|2|absent"
ok "a syncing report on a fresh db says it created the cache (stderr)" "$(node "$L" report first-pass 2>&1 >/dev/null | grep -c '^ledger: created .*metrics.db from the run ledgers')" "1"
ok "the second syncing report stays quiet about creating it" "$(node "$L" report first-pass 2>&1 >/dev/null | grep -c 'ledger: created')" "0"
rm -f "$DB" "$DB".* ; mv "$DB.keep" "$DB" 2>/dev/null; [ -e "$DB" ] || node "$L" sync >/dev/null 2>&1

echo "== staleness catch-up (last transcript sync older than 2 days: report syncs transcripts first; the dashboard's own warning moved to boss-analytics)"
txn(){ q "SELECT COUNT(*) FROM tx_sync_log"; }
staleize(){ q "UPDATE tx_sync_log SET finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now','-$1 days')"; }
n0=$(txn)
node "$L" report first-pass >/dev/null 2>"$T/e.fresh"
ok "fresh db (last transcript sync just now): report adds no tx_sync_log row, no note" "$(txn) $(wc -c < "$T/e.fresh" | tr -d ' ')" "$n0 0"
staleize 3
node "$L" report first-pass >"$T/o.stale" 2>"$T/e.stale"
ok "3 days stale: report ran a transcript sync (one new tx_sync_log row)" "$(txn)" "$((n0 + 1))"
ok "the note is on stderr, with the age" "$(grep -c '^ledger: note: the last transcript sync was 3\.0 days ago' "$T/e.stale")" "1"
ok "stdout stays the report (no note in it)" "$(grep -c 'transcript sync' "$T/o.stale")|$(grep -c '^ledger: report first-pass' "$T/o.stale")" "0|1"
staleize 3
J2=$(node "$L" report first-pass --json 2>/dev/null)
ok "--json: stdout is clean JSON, catch_up says 3 days" "$(echo "$J2" | jq -c '.catch_up.age_days|floor')" "3"
node "$L" report first-pass >/dev/null 2>"$T/e.again"
ok "after the catch-up the next report is fresh again (no note)" "$(wc -c < "$T/e.again" | tr -d ' ')" "0"
staleize 3; n1=$(txn)
ORCH_SYNC_STALE_DAYS=off node "$L" report first-pass >/dev/null 2>"$T/e.off"
ok "ORCH_SYNC_STALE_DAYS=off: no catch-up, no note" "$(txn) $(wc -c < "$T/e.off" | tr -d ' ')" "$n1 0"
ORCH_SYNC_STALE_DAYS=5 node "$L" report first-pass >/dev/null 2>"$T/e.five"
ok "ORCH_SYNC_STALE_DAYS=5: a 3-day-old sync is still fresh enough" "$(txn) $(wc -c < "$T/e.five" | tr -d ' ')" "$n1 0"
node "$L" report first-pass >/dev/null 2>&1

echo "== SessionStart stale sync (hooks/stale-sync.js, P30): reads <db>.txsync's age only, a detached background sync under a lock"
SS="$CL/hooks/stale-sync.js"; MK="$DB.txsync"; LK="$DB.sync.lock"; LG="$DB.sync.log"
waitlock(){ local i=0; while [ -e "$LK" ] && [ $i -lt 600 ]; do sleep 0.1; i=$((i+1)); done; [ -e "$LK" ] && echo held || echo free; }   # <= 60 s, so no child outlives the suite
ms(){ local a b; a=$(date +%s%N); "$@" >"$T/ss.out" 2>&1; b=$(date +%s%N); echo $(( (b - a) / 1000000 )); }
par=""; for v in unset 3 off "" abc 0.5; do if [ "$v" = unset ]; then e="env -u ORCH_SYNC_STALE_DAYS"; else e="env ORCH_SYNC_STALE_DAYS=$v"; fi
  par="$par$($e node -e "const a=require('$SS').thresholdDays(), b=require('$O/metrics.js').staleDays(); process.stdout.write(a===b?'=':'('+a+'!='+b+')')" 2>/dev/null)"; done
ok "threshold parity with the lib's staleDays() (unset, 3, off, empty, abc, 0.5)" "$par" "======"
node "$L" sync >/dev/null 2>&1
ok "a transcript sync touches <db>.txsync" "$([ -e "$MK" ] && echo marker)" "marker"
n0=$(txn); FRESH_MS=$(ms node "$SS"); st=$?
ok "fresh db: exit 0, no output, no lock, no log, no sync" "$st|$(wc -c < "$T/ss.out" | tr -d ' ')|$([ -e "$LK" ] && echo lock)|$([ -e "$LG" ] && echo log)|$(txn)" "0|0|||$n0"
touch -d '3 days ago' "$MK"
STALE_MS=$(ms bash -c "node '$SS' & node '$SS' & wait")
ok "stale db, two starts at once: both return at once with no output, the lock is released after the sync" "$(wc -c < "$T/ss.out" | tr -d ' ')|$(waitlock)" "0|free"
ok "two starts at once ran one sync (one new tx_sync_log row, one log record, exit 0), marker fresh again" "$(txn)|$(grep -c ' stale-sync pid ' "$LG")|$(grep -c ': exit 0, ' "$LG")|$(find "$MK" -mmin -1 | wc -l)" "$((n0 + 1))|1|1|1"
echo "NOTE stale-sync hook wall time: fresh db ${FRESH_MS} ms; stale db, two concurrent starts ${STALE_MS} ms (the sync itself runs detached)"
ok "hook wall time under 1 s in both cases (timeout 5)" "$([ "$FRESH_MS" -lt 1000 ] && [ "$STALE_MS" -lt 1000 ] && echo fast)" "fast"
rm -f "$MK"; n1=$(txn); node "$SS"; waitlock >/dev/null
ok "a missing marker next to an existing db reads as stale: one sync" "$(txn)" "$((n1 + 1))"
touch -d '3 days ago' "$MK"; n1=$(txn)
ORCH_SYNC_STALE_DAYS=off node "$SS"; sleep 0.3
ok "ORCH_SYNC_STALE_DAYS=off: no sync, no lock" "$(txn)|$([ -e "$LK" ] && echo lock)" "$n1|"
echo "{\"pid\":$$,\"ts\":\"x\"}" > "$LK"; node "$SS"; sleep 0.3
ok "a held lock (live pid) blocks the start" "$(txn)|$(jq -r .pid "$LK")" "$n1|$$"
touch -d '3 hours ago' "$LK"; node "$SS"; waitlock >/dev/null
ok "a lock older than 2 h is taken over: one sync, lock released" "$(txn)|$([ -e "$LK" ] && echo lock)" "$((n1 + 1))|"
touch -d '3 days ago' "$MK"; n1=$(txn); echo '{"pid":999999999,"ts":"x"}' > "$LK"; node "$SS"; sleep 0.3
ok "a dead pid's lock younger than 30 s still holds (the hook-to-child hand-over)" "$(txn)|$([ -e "$LK" ] && echo lock)" "$n1|lock"
touch -d '1 minute ago' "$LK"; node "$SS"; waitlock >/dev/null
ok "a dead pid's lock older than 30 s is taken over" "$(txn)" "$((n1 + 1))"
ok "no db: nothing to catch up on (no db, lock or log created)" "$(ORCH_METRICS_DB="$T/ss-none.db" node "$SS"; sleep 0.3; ls "$T" | grep -c '^ss-none')" "0"

echo "== commit scan: repos and author come from config.json metrics.commitRepos / metrics.commitAuthor (empty = none, null author = no filter)"
GH="$T/gh"; mkdir -p "$GH/r1"; git init -q -b main "$GH/r1"
git -C "$GH/r1" -c user.name=Alice -c user.email=a@x.invalid commit -q --allow-empty -m "by alice"
git -C "$GH/r1" -c user.name=Bob -c user.email=b@x.invalid commit -q --allow-empty -m "by bob"
gitsync(){ rm -f "$T/git.db"; ORCH_METRICS_DB="$T/git.db" ORCH_COMMIT_HOME="$GH" "$@" node "$L" sync --full-git >/dev/null 2>&1; sqlite3 "$T/git.db" "SELECT COUNT(*) FROM tx_git_commits" 2>/dev/null; }
rm -f "$ORCH_DIR/config.json"
ok "no config: no repos are scanned, no commits recorded" "$(gitsync env)" "0"
echo '{"metrics":{"commitRepos":["r1"],"commitAuthor":null}}' > "$ORCH_DIR/config.json"
ok "commitRepos [r1], commitAuthor null: every author counts" "$(gitsync env)" "2"
echo '{"metrics":{"commitRepos":["r1"],"commitAuthor":"Alice"}}' > "$ORCH_DIR/config.json"
ok "commitAuthor Alice: only her commit" "$(gitsync env)" "1"
ok "ORCH_COMMIT_AUTHOR overrides the config author" "$(gitsync env ORCH_COMMIT_AUTHOR=Bob)" "1"
echo '{"metrics":{"commitRepos":["nosuch","r1"],"commitAuthor":null}}' > "$ORCH_DIR/config.json"
ok "a listed repo that does not exist is skipped" "$(gitsync env)" "2"
echo '{"metrics":{"commitRepos":["'"$GH"'/r1"],"commitAuthor":null}}' > "$ORCH_DIR/config.json"
ok "an absolute repo path works" "$(gitsync env ORCH_COMMIT_HOME="$T/nohome")" "2"
rm -f "$ORCH_DIR/config.json" "$T/git.db"
ok "report leaves the ledger data unchanged after the catch-up" "$(dumphash)" "$H1"

echo "== deletes and rollback"
rm -rf "$ORCH_RUNS_DIR/m-b"
o=$(node "$L" sync 2>&1)
ok "deleted run folder: its rows are deleted (2 attempts + 1 run)" "$(echo "$o" | grep -o 'deleted [0-9]*')|$(counts)" "deleted 3|5 3 1"
cp "$DB" "$T/before.db"; printf 'not a database, just text that is long enough to fail the header check......................................................' > "$T/bad.db"; B0=$(sum "$T/bad.db")
o=$(ORCH_METRICS_DB="$T/bad.db" node "$L" sync 2>&1); st=$?
ok "sync on a corrupt db: exit 1, file unchanged" "$st|$(sum "$T/bad.db")" "1|$B0"
chmod 000 "$ORCH_RUNS_DIR/m-a/ledger.jsonl" 2>/dev/null
if [ -r "$ORCH_RUNS_DIR/m-a/ledger.jsonl" ]; then ok "unreadable file test skipped (running as root)" "skip" "skip"; else
  o=$(node "$L" sync 2>&1); ok "unreadable ledger.jsonl reads as missing: its attempts are deleted, the db stays valid" "$(counts)" "0 3 1"; fi
chmod 644 "$ORCH_RUNS_DIR/m-a/ledger.jsonl"

echo "== no node:sqlite on the write and hook paths"
P="$T/probe.js"; echo "process.on('exit', () => process.stderr.write('SQLITE=' + process.moduleLoadList.some(m => /sqlite/i.test(m)) + '\n'));" > "$P"
export ORCH_RUNS_DIR="$T/orch2/runs" ORCH_DIR="$T/orch2"; mkdir -p "$ORCH_RUNS_DIR"
probe(){ node -r "$P" "$@" 2>&1 >/dev/null | grep '^SQLITE=' ; }
ok "add loads no sqlite" "$(probe "$L" add --run p --row 1 --role review --agent x --model opus --effort high --mode wave --tokens 1 --tools 1 --ms 1 --repo r --area a)" "SQLITE=false"
ok "update loads no sqlite" "$(probe "$L" update --run p --row 1 --first-pass y)" "SQLITE=false"
ok "show loads no sqlite" "$(probe "$L" show)" "SQLITE=false"
R=p2; mkdir -p "$ORCH_RUNS_DIR/$R"
node "$M" launch --run $R --row 3 --role discovery --agent orch-scout --model haiku --effort low --mode wave --repo r --area a >/dev/null 2>&1
printf '<task-notification>\n<task-id>aprobe1</task-id>\n<tool-use-id>tu-probe</tool-use-id>\n<output-file>/tmp/fixture/aprobe1.output</output-file>\n<status>completed</status>\n<summary>Agent "[haiku/low] %s#3.1 probe" finished</summary>\n<result>ok</result>\n<usage><subagent_tokens>1234</subagent_tokens><tool_uses>2</tool_uses><duration_ms>3000</duration_ms></usage>\n</task-notification>' "$R" > "$T/notice.txt"
ok "ingest loads no sqlite (and writes the line)" "$(probe "$L" ingest --file "$T/notice.txt")|$(jq -r .id "$ORCH_RUNS_DIR/$R/ledger.jsonl")" "SQLITE=false|$R#3.1"
ok "manifest.js show loads no sqlite" "$(probe "$M" show --run $R)" "SQLITE=false"
ok "positive control: sync loads sqlite" "$(ORCH_METRICS_DB="$T/probe.db" probe "$L" sync)" "SQLITE=true"
ok "require(ledger.js) loads no sqlite" "$(node -e "require('$L'); console.log(process.moduleLoadList.some(m=>/sqlite/.test(m)))" 2>&1)" "false"
CP="$T/copy/lib"; mkdir -p "$CP"; cp "$L" "$M" "$(dirname "$L")/paths.js" "$(dirname "$L")/plan-status.js" "$(dirname "$L")/defaults.json" "$CP/"
R=p3; mkdir -p "$ORCH_RUNS_DIR/$R"
node "$CP/manifest.js" launch --run $R --row 3 --role discovery --agent orch-scout --model haiku --effort low --mode wave --repo r --area a >/dev/null 2>&1
sed "s/p2#3.1/$R#3.1/; s/aprobe1/aprobe3/g; s/tu-probe/tu-probe3/" "$T/notice.txt" > "$T/notice3.txt"
o=$(node "$CP/ledger.js" ingest --file "$T/notice3.txt" 2>&1); st=$?
ok "ingest works in a temp copy without metrics.js" "$([ -e "$CP/metrics.js" ] && echo has-metrics || echo no-metrics)|$st|$o" "no-metrics|0|$R#3.1: stop new, ledger written"
ok "control: sync in that copy fails (exit 1: metrics.js missing)" "$(ORCH_METRICS_DB="$T/copy.db" node "$CP/ledger.js" sync >/dev/null 2>&1; echo $?)|$([ -e "$T/copy.db" ] && echo db || echo no-db)" "1|no-db"
H="$CL/hooks"; hk=""
for f in orch-attempts.js orch-return-cap.js; do if [ -e "$H/$f" ]; then hk="$hk$(rg -n "metrics|node:sqlite" "$H/$f")"; else echo "NOTE $H/$f absent (built by a later row): grep vacuous"; fi; done
ok "R13b hook grep (orch-attempts.js, orch-return-cap.js) empty" "$hk" ""
ok "no hook, manifest.js or wfp.js requires metrics.js or node:sqlite" "$(rg -n "metrics\.js|node:sqlite|require\([^)]*metrics['\"]" "$H"/*.js "$M" "$CL"/wfp/*.js)" ""
ok "ledger.js mentions metrics.js only in the lazy loader" "$(rg -n "require\(.*metrics" "$L" | wc -l)|$(rg -n "require\(.*metrics" "$L" | grep -c 'const metricsMod = ()')" "1|1"
ok "metrics.js is the only node:sqlite require in orchestrator/" "$(rg -l "require\('node:sqlite'\)" "$O"/*.js)" "$O/metrics.js"

echo "== report first-pass: retry kind split (own scratch runs dir and db; old lines without the field)"
RK="$T/rkruns"; mkdir -p "$RK/k-a"
rl(){ jq -cn --arg id "$1" --argjson row "$2" --arg model "$3" --argjson fp "$4" --argjson retries "$5" --arg kind "$6" \
  '{ts:("2026-10-02T00:00:0"+($id|split(".")[1])+".000Z"), id:$id, run:"k-a", row:$row, role:"implementation", agent:"orch-builder", model:$model, effort:"high", mode:"subagent",
    tokens:10, tools:1, ms:1, repo:"r", area:"a", firstPass:$fp, retries:$retries, escalated:false} + (if $kind == "" then {} else {retry_kind:$kind} end)'; }
{ rl k-a#1.1 1 sonnet false 0 ""; rl k-a#1.2 1 sonnet true 1 continue
  rl k-a#2.1 2 sonnet false 0 ""; rl k-a#2.2 2 opus true 1 fresh
  rl k-a#3.1 3 sonnet true 0 ""
  rl k-a#4.1 4 sonnet false 0 ""; rl k-a#4.2 4 opus null 1 ""; } > "$RK/k-a/ledger.jsonl"
KT=$(ORCH_METRICS_DB="$T/rk.db" ORCH_RUNS_DIR="$RK" node "$L" report first-pass 2>&1); kst=$?
ok "retry kind report exits 0" "$kst" "0"
ok "retry kind line: first 4 (1/4), continue 1 (1/1), fresh 1 (1/1), unlabelled 1 (no outcome)" "$(echo "$KT" | grep '^retry kind (claude):')" "retry kind (claude): first 4 (1/4 first-pass), continue 1 (1/1 first-pass), fresh 1 (1/1 first-pass), unlabelled 1"
KJ=$(ORCH_METRICS_DB="$T/rk.db" ORCH_RUNS_DIR="$RK" node "$L" report first-pass --json 2>/dev/null)
ok "retry kind json: counts per kind" "$(echo "$KJ" | jq -c '.retry_kinds.claude | [.first.attempts, .continue.attempts, .fresh.attempts, .unlabelled.attempts, .continue.first_pass, .fresh.outcomes]')" "[4,1,1,1,1,1]"
ok "retry kind: the model/effort rows are unchanged by the split (sonnet/high 5 attempts, opus/high 2)" "$(echo "$KT" | grep -cE '^(sonnet/high +5 |opus/high +2 )')" "2"
ok "retry kind: tokens-per-accepted report has no retry kind line" "$(ORCH_METRICS_DB="$T/rk.db" ORCH_RUNS_DIR="$RK" node "$L" report tokens-per-accepted 2>&1 | grep -c '^retry kind')" "0"
ok "retry kind: old fixture lines (no field, retries>0) count as unlabelled" "$(echo "$RF" | grep '^retry kind (claude):' | grep -c 'unlabelled 2')|$(echo "$RF" | grep -c '^retry kind (codex):')" "1|1"

echo "== provisional waiting notice (item 14): interim, promoted and final stops of one attempt count once in sqlite"
PW="$T/pw"; mkdir -p "$PW/runs"
pwn(){ printf '<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<output-file>/tmp/fixture/%s.output</output-file>\n<status>completed</status>\n<summary>Agent "[sonnet/high] %s#1.1 w" %s</summary>\n<result>ok</result>\n<usage><subagent_tokens>%s</subagent_tokens><tool_uses>%s</tool_uses><duration_ms>%s</duration_ms></usage>\n</task-notification>' "$1" "$2" "$1" "$3" "$4" "$5" "$6" "$7"; }
pwe(){ ORCH_RUNS_DIR="$PW/runs" ORCH_DIR="$PW" "$@"; }   # own scratch runs dir and db
for RR in pw-fin pw-close; do
  mkdir -p "$PW/runs/$RR"; pwe node "$M" launch --run $RR --row 1 --role implementation --agent orch-builder --model sonnet --effort high --mode wave --repo r --area a >/dev/null 2>&1
  pwn apw-$RR tu-$RR $RR "is waiting on its own background work" 100 3 1000 > "$PW/$RR.i.txt"; pwe node "$L" ingest --file "$PW/$RR.i.txt" >/dev/null 2>&1
done
pwn apw-pw-fin tu-pw-fin pw-fin finished 700 9 5000 > "$PW/pw-fin.f.txt"; pwe node "$L" ingest --file "$PW/pw-fin.f.txt" >/dev/null 2>&1
pwe node "$L" close --attempt pw-close#1.1 --first-pass y --outcome accepted >/dev/null 2>&1
pws(){ ORCH_METRICS_DB="$PW/m.db" pwe node "$L" "$@" 2>&1; }
pws sync >/dev/null
ok "sqlite: interim then final, and interim then close, make one attempt each (not one per stop event)" "$(sqlite3 -readonly "$PW/m.db" "SELECT run||' '||COUNT(*) FROM attempts GROUP BY run ORDER BY run" | tr '\n' ';')" "pw-close 1;pw-fin 1;"
ok "sqlite: the final attempt carries the final tokens (700), the closed one the promoted interim figures (100)" "$(sqlite3 -readonly "$PW/m.db" "SELECT run||' '||tokens FROM attempts ORDER BY run" | tr '\n' ';')" "pw-close 100;pw-fin 700;"
ok "report tokens-per-accepted counts each attempt once (tokens 800 over 2 attempts)" "$(pws report tokens-per-accepted | grep -c '^role implementation: 2 rows, 2 attempts, tokens 800,')" "1"
ok "a second sync changes nothing (one attempt per run)" "$(pws sync >/dev/null; sqlite3 -readonly "$PW/m.db" 'SELECT COUNT(*) FROM attempts')" "2"

echo "== live files"
ok "live ACTIVE kept and free of this suite's paths (content check; peer-written, so no md5)" "$(activecheck)" "ok"
ok "no live lease, run manifest or ledger line, run dir or errors.log line names a sid or run of this suite" "$(liveleak "$LIVE" "$T")" "0"
liveleak "$LIVE" "$T" >/dev/null   # a plain call, so SUITE_SIDS / SUITE_RUNS are set in this shell
ok "derived lists hold this suite's own ids (the live check is not vacuous)" "$(jq -rn --argjson s "$SUITE_SIDS" --argjson r "$SUITE_RUNS" '["m-a","p2"] - ($s + $r) | join(",")')" ""
ok "live metrics.db (written by live syncs) has no run of this suite" "$([ -e "$LIVE/metrics.db" ] && sqlite3 -readonly -cmd '.timeout 2000' "$LIVE/metrics.db" "SELECT COUNT(*) FROM runs WHERE run IN (SELECT value FROM json_each('$(jq -c . <<<"$SUITE_RUNS")'))" || echo 0)" "0"

echo "== live-leak check has teeth (fixtures planted last, outside the suite's scratch trees)"
X="$T/teeth"; mkdir -p "$X/s/orch/runs/zz-teeth" "$X/clean" "$X/l1" "$X/l2/runs/zz-teeth" "$X/l3/runs/zz-other"
echo '{"session":"zz-teeth-sid-1234","run":"zz-teeth"}' > "$X/s/orch/runs/zz-teeth/manifest.jsonl"
echo '{"v":1,"run":"zz-ele","session":"zz-teeth-sid-1234"}' > "$X/l1/ACTIVE.lease"
echo '{"type":"launch","run":"zz-teeth"}' > "$X/l2/runs/zz-teeth/manifest.jsonl"
echo '{"type":"event","sid":"zz-teeth-sid-1234","run":"other"}' > "$X/l3/runs/zz-other/ledger.jsonl"
ok "teeth: a clean live dir counts 0" "$(liveleak "$X/clean" "$X/s")" "0"
ok "teeth: a live lease with a suite sid counts 1" "$(liveleak "$X/l1" "$X/s")" "1"
ok "teeth: a live run dir and its manifest line count 2" "$(liveleak "$X/l2" "$X/s")" "2"
ok "teeth: a live ledger line with a suite sid counts 1" "$(liveleak "$X/l3" "$X/s")" "1"


echo "pass=$pass fail=$fail"; [ "$fail" -eq 0 ]
