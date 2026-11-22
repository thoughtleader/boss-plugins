#!/usr/bin/env bash
# orch-attempts.js + hooks/hooks.json tests (orchestrator v2 fixes, row 14; D4 design section 5 and section 9 row 14).
# Fake hook JSON on scratch ORCH_DIR / ORCH_RUNS_DIR / ORCH_PROJECTS_DIR; never the live files.
# Cases: pretool with an ID, without one, with no brief line, unknown attempt, from a subagent (ignored), dedupe; prompt with
# two notices mixed with owner text, again (dedupe), a notice with no ID; Stop sweep over a fake transcript; substop by ID and by
# start, unmapped; session with the lease mine/other/expired/none (only a held lease is ever described); the lease rule (a
# non-holder session writes and prints nothing in all five modes; ACTIVE is never read, so it never picks or names a run); failures write nothing;
# errors.log cap; no sqlite and no store module on any hook path; hooks.json lint (boss-node by full path, scripts exist, entries, guard matcher A-02);
# the Stage A pretool nudge (no live lease, orch-* launch: one context line, nothing written, silent otherwise).
# Per-run leases (row 4 of the per-run lease run): lease files are read through lease-store.js; D1 picks the run (the ID ends
# the chain; else the one held run the brief names; else the only lease; else nothing + one errors.log line); the nudge reads
# only the target run's lease; session text for one, several or no held leases; the fail-open matrix over junk lease files.
# Retire-ACTIVE run (row 3): a brief that names no run gets no nudge; session without a held lease is silent; notice ingest
# (prompt, sweep) is gated to the held runs (OD-3): not-held, ID-less linking, ambiguous, expired-own and taken-over cases.
# Compaction-gap hint (prompt, holder): a 2+ min old attempt with no notice or a lease expired within the hour prints one line, at most once per run per 10 min; silent otherwise.
# Usage: bash test/attempt-hooks-tests.sh   (exit 0 = all pass)
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib/compat.sh"   # on macOS: GNU date/stat/touch/md5sum/wc shims (test/lib)
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"   # the checkout this suite tests (a worktree runs its own code)
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
HOOK="$CL/hooks/orch-attempts.js"; MAN="$CL/lib/manifest.js"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export ORCH_DIR="$T/orch" ORCH_RUNS_DIR="$T/orch/runs" ORCH_PROJECTS_DIR="$T/projects" ORCH_METRICS_DB="$T/m.db"
unset WFP_SID NODE_OPTIONS BOSS_PROGRESS_DIR   # a direct run behaves like scripts/test-all.sh: no snapshot reaches the live registry (gate cases set it per command)
LIVE="$HOME/.claude/orchestrator"; sum(){ if [ -e "$1" ]; then md5sum < "$1"; else echo absent; fi; }   # LIVE is only for the live-file checks
livesums(){ for f in "$HOME/.claude/settings.json"; do echo "$f $(sum "$f")"; done; }   # not peer-written: md5
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
LIVE0=$(livesums)
R=t-run; O=t-other; RD="$ORCH_RUNS_DIR/$R"; MFILE="$RD/manifest.jsonl"; LFILE="$RD/ledger.jsonl"
S1=sess-holder-1111; S2=sess-other-2222
P="$ORCH_PROJECTS_DIR/p"; TR="$P/$S1.jsonl"; SUB="$P/$S1/subagents"
mkdir -p "$RD" "$ORCH_RUNS_DIR/$O" "$SUB"; : > "$TR"
echo "$ORCH_RUNS_DIR/$O" > "$ORCH_DIR/ACTIVE"             # ACTIVE names ANOTHER run than the lease (the live situation)
treesum(){ (cd "$ORCH_DIR" && find . -type f ! -name errors.log -print0 | sort -z | xargs -0 md5sum 2>/dev/null; find . | sort) | md5sum; }
errs(){ if [ -f "$ORCH_DIR/errors.log" ]; then wc -l < "$ORCH_DIR/errors.log" | tr -d ' '; else echo 0; fi; }
# lf <run>: the file holding that run's lease: leases/<run>.json when it exists, else the legacy ACTIVE.lease (so a manifest.js
# that writes per-run files needs no edit here)
lf(){ if [ -e "$ORCH_DIR/leases/$1.json" ]; then echo "$ORCH_DIR/leases/$1.json"; else echo "$ORCH_DIR/ACTIVE.lease"; fi; }
# hook <mode> <json>: stdout to $T/out, exit code to $T/rc
hook(){ printf '%s' "$2" | node "$HOOK" "$1" > "$T/out" 2> "$T/err"; echo $? > "$T/rc"; }
outctx(){ jq -r '.hookSpecificOutput.additionalContext // empty' "$T/out" 2>/dev/null; }
nodecision(){ if [ ! -s "$T/out" ]; then echo none; else jq -r 'if (.hookSpecificOutput.permissionDecision // .decision // .hookSpecificOutput.updatedInput // null) == null then "none" else "DECISION" end' "$T/out"; fi; }
ev(){ jq -c "select($1)" "$MFILE" 2>/dev/null; }
evn(){ ev "$1" | wc -l | tr -d ' '; }
launch(){ node "$MAN" launch --run $R --row "$1" --role discovery --agent orch-scout --model "$2" --effort "$3" --mode wave --repo .claude --area orchestrator --sid $S1 | head -1; }
pre(){ jq -cn --arg sid "$1" --arg tu "$2" --arg d "$3" --arg p "$4" --arg tp "$TR" --arg st "${5:-orch-scout}" \
  '{session_id:$sid, transcript_path:$tp, hook_event_name:"PreToolUse", tool_name:"Agent", tool_use_id:$tu,
    tool_input:{description:$d, prompt:$p, subagent_type:$st, model:"haiku", effort:"low", run_in_background:true}}'; }
notice(){ # task tuid desc tokens tools ms
  printf '<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<output-file>/tmp/fixture/%s.output</output-file>\n<status>completed</status>\n<summary>Agent "%s" finished</summary>\n<result>FINDINGS fixture</result>\n<usage><subagent_tokens>%s</subagent_tokens><tool_uses>%s</tool_uses><duration_ms>%s</duration_ms></usage>\n</task-notification>' "$1" "$2" "$1" "$3" "$4" "$5" "$6"; }
meta(){ jq -cn --arg d "$2" --arg tu "$3" --arg m "$4" --arg e "$5" '{agentType:"orch-scout", description:$d, toolUseId:$tu, spawnDepth:1, model:$m, effort:$e, finished:true}' > "$SUB/agent-$1.meta.json"; }

echo "== setup: lease for $R held by $S1, ACTIVE names $O, launches for rows 6, 7, 8"
node "$MAN" lease acquire --run $R --sid $S1 >/dev/null 2>&1; ok "lease acquired by the holder" "$(jq -r '.session + " " + .run' "$(lf $R)")" "$S1 $R"
ID6=$(launch 6 haiku low); ID7=$(launch 7 sonnet medium); ID8=$(launch 8 haiku low)
ok "attempt IDs allocated" "$ID6 $ID7 $ID8" "$R#6.1 $R#7.1 $R#8.1"

echo "== hygiene and syntax"
ok "hook passes node --check" "$(node --check "$HOOK" >/dev/null 2>&1; echo $?)" "0"
ok "hook file never names the sqlite store (rg metrics|node:sqlite empty)" "$(rg -n 'metrics|node:sqlite' "$HOOK" | wc -l | tr -d ' ')" "0"

echo "== pretool (holder)"
D6="[haiku/low] $ID6 Smoke test orch-scout"; TU6=toolu_013h1qc5HxCwQtg6NkVww9AH
hook pretool "$(pre $S1 $TU6 "$D6" "Do the thing.
Orch-Attempt: $ID6
More.")"
ok "pretool with an ID: exit 0, silent" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
ok "start written to the ID's run" "$(ev ".type==\"start\" and .key==\"$TU6\"" | jq -r '[.attempt, .via, .session, .agent, .model, .effort, .id_in_brief, .unknown_attempt, .unplanned, .foreign_lease, .background, .named] | map(tostring) | join(" ")')" "$ID6 pretool $S1 orch-scout haiku low true false false false true false"
ok "start keeps transcript_path and description" "$(ev ".key==\"$TU6\"" | jq -r '(.transcript_path == "'"$TR"'") and (.description == "'"$D6"'")')" "true"
hook pretool "$(pre $S1 $TU6 "$D6" "Orch-Attempt: $ID6")"; ok "same tool_use_id again: one start (dedupe)" "$(evn ".type==\"start\" and .key==\"$TU6\"")" "1"
TU7=toolu_7777; hook pretool "$(pre $S1 $TU7 "[sonnet/medium] $ID7 second" "no attempt line here" "Explore")"
ok "pretool with no brief line: start id_in_brief false, raw subagent_type kept" "$(ev ".key==\"$TU7\"" | jq -r '"\(.id_in_brief) \(.agent)"')" "false Explore"
ok "pretool with no brief line: one context note, no decision" "$(outctx | grep -c "the brief lacks the line Orch-Attempt: $ID7; the commit trailer depends on it.")|$(nodecision)" "1|none"
TUP=toolu_prefixed1; hook pretool "$(pre $S1 $TUP "[haiku/low] prefixed launch, no id" "brief" "boss:orch-builder")"
ok "pretool with subagent_type boss:orch-builder: the start records orch-builder (plugin prefix stripped)" "$(ev ".key==\"$TUP\"" | jq -r .agent)" "orch-builder"
hook pretool "$(pre $S1 toolu_9999 "[haiku/low] $R#9.1 never launched" "Orch-Attempt: $R#9.1")"
ok "pretool with an unallocated ID: unknown_attempt + note" "$(ev '.key=="toolu_9999"' | jq -r .unknown_attempt)|$(outctx | grep -c 'no launch event for this ID')" "true|1"
TUU=toolu_unplanned1; hook pretool "$(pre $S1 $TUU "[haiku/low] plain launch, no id" "brief")"
ok "pretool without an ID: unplanned start in the LEASE run" "$(ev ".key==\"$TUU\"" | jq -r '"\(.attempt) \(.unplanned) \(.run)"')|$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "null true $R|0|0"
ok "ACTIVE's run ($O) got nothing" "$(ls -A "$ORCH_RUNS_DIR/$O" | wc -l | tr -d ' ')" "0"
n0=$(wc -l < "$MFILE"); hook pretool "$(pre $S1 toolu_sub1 "[haiku/low] $ID6 nested" "Orch-Attempt: $ID6" | jq -c '. + {agent_id:"a1234", agent_type:"orch-scout"}')"
ok "pretool from a subagent (agent_id, holder's session_id): ignored" "$(wc -l < "$MFILE" | tr -d ' ')|$(wc -c < "$T/out" | tr -d ' ')|$(cat "$T/rc")" "$n0|0|0"

echo "== the lease rule: a non-holder session passes through (nothing written, nothing printed) in all five modes"
cat > "$T/probe.js" <<'EOF'
process.on('exit', () => { try { require('fs').appendFileSync(process.env.PROBE_OUT, JSON.stringify({
  sqlite: process.moduleLoadList.some(m => /sqlite/.test(m)),
  mods: Object.keys(require.cache).filter(k => /\/lib\/(?!paths\.js)[a-z-]+\.js$/.test(k)).map(k => require('path').basename(k)).sort() }) + '\n'); } catch {} });
EOF
export PROBE_OUT="$T/probe.out"
meta a00other1 "[haiku/low] $ID6 by another chat" toolu_other1 haiku low
GN=$(notice a00other1 toolu_other1 "[haiku/low] $ID6 by another chat" 111 2 333)
jq -cn --arg c "$GN" --arg ts "2026-10-21T09:00:00.000Z" '{type:"queue-operation",operation:"enqueue",timestamp:$ts,content:$c}' > "$T/tr-other.jsonl"
NUDGE(){ echo "[orch] $1, and this session is launching $2 for run $3. If this session drives that run, take the lease now: node \"$CL/lib/manifest.js\" lease acquire --run $3 --sid=\$WFP_SID, and launch rows through manifest.js launch. If it doesn't drive the run, ignore this."; }
CT12(){ echo "[orch] Active run: $(basename "$1") (lease: $2). Run /boss:resume $(basename "$1") before launching anything (it reconciles running rows)."; }   # a run in the runs dir shows as its name (/boss:resume resolves it)
s0=$(treesum); e0=$(errs); : > "$PROBE_OUT"
# per case: mode|want additionalContext|hook JSON. ACTIVE names $O (unleased), but ACTIVE is never read: the ID-less launch
# (its brief names no run) gets no nudge, and session (S2 holds no lease) is silent.
for c in "pretool||$(pre $S2 toolu_other1 "[haiku/low] $ID6 by another chat" "Orch-Attempt: $ID6")" \
         "pretool||$(pre $S2 toolu_other2 "[haiku/low] no id" "x")" \
         "prompt||$(jq -cn --arg s $S2 --arg p "$GN" --arg tp "$T/tr-other.jsonl" '{session_id:$s, transcript_path:$tp, hook_event_name:"UserPromptSubmit", prompt:$p}')" \
         "stop||$(jq -cn --arg s $S2 --arg tp "$T/tr-other.jsonl" '{session_id:$s, transcript_path:$tp, hook_event_name:"Stop", stop_hook_active:false}')" \
         "substop||$(jq -cn --arg s $S2 --arg tp "$SUB/agent-a00other1.jsonl" '{session_id:$s, agent_id:"a00other1", agent_type:"orch-scout", agent_transcript_path:$tp, last_assistant_message:"hi", stop_hook_active:false}')" \
         "session||$(jq -cn --arg s $S2 '{session_id:$s, hook_event_name:"SessionStart", source:"compact"}')"; do
  m=${c%%|*}; r=${c#*|}; w=${r%%|*}; NODE_OPTIONS="--require $T/probe.js" hook "$m" "${r#*|}"
  ok "non-holder $m: exit 0, no decision, empty stderr, context as wanted" "$(cat "$T/rc")|$(nodecision)|$(wc -c < "$T/err" | tr -d ' ')|$(outctx)" "0|none|0|$w"
done
ok "non-holder: no file under ORCH_DIR changed, no errors.log line" "$(treesum)|$(errs)" "$s0|$e0"
ok "non-holder: only lease-store.js loaded (never manifest.js / ledger.js), no sqlite (6 calls)" "$(jq -s -c '[.[] | select(.sqlite or ((.mods - ["lease-store.js"]) | length > 0))] | length' "$PROBE_OUT")|$(jq -s -c '[.[] | select(.mods == ["lease-store.js"])] | length' "$PROBE_OUT")|$(wc -l < "$PROBE_OUT" | tr -d ' ')" "0|6|6"
echo "== pretool nudge (Stage A 1b): no live lease at all, orch-* launch by the main session; never writes, never denies"
rm "$(lf $R)"; s0=$(treesum); e0=$(errs); : > "$PROBE_OUT"
NODE_OPTIONS="--require $T/probe.js" hook pretool "$(pre $S1 toolu_nolease "[haiku/low] $ID6 x" "Orch-Attempt: $ID6")"
ok "no lease at all: pretool writes nothing (tree, errors.log), exit 0, no decision" "$(treesum)|$(errs)|$(cat "$T/rc")|$(nodecision)" "$s0|$e0|0|none"
ok "no lease, orch-* launch with an ID: the nudge names the ID's run" "$(outctx)" "$(NUDGE 'No session holds the run lease' orch-scout $R)"
hook pretool "$(pre $S1 toolu_nolease_p "[haiku/low] $ID6 x" "Orch-Attempt: $ID6" boss:orch-scout)"
ok "plugin-prefixed subagent_type (boss:orch-scout) still counts as orch-*: the nudge fires" "$(outctx)" "$(NUDGE 'No session holds the run lease' boss:orch-scout $R)"
ok "the nudge is PreToolUse additionalContext" "$(jq -r .hookSpecificOutput.hookEventName "$T/out")" "PreToolUse"
ok "the nudge never loads manifest.js / ledger.js or sqlite" "$(jq -s -c '[.[] | select(.sqlite or ((.mods - ["lease-store.js"]) | length > 0))] | length' "$PROBE_OUT")|$(wc -l < "$PROBE_OUT" | tr -d ' ')" "0|1"
hook pretool "$(pre $S2 toolu_nl2 "[haiku/low] plain launch, no id" "x")"
ok "no run in the brief (ACTIVE names unleased $O): silent, ACTIVE is never read" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
hook pretool "$(pre $S2 toolu_nl3 "[haiku/low] zz-missing#1.1 x" "x" orch-builder)"
ok "an ID whose run folder is missing, no other run named: silent (no fallback to ACTIVE's run)" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
s0=$(treesum); e0=$(errs)
hook pretool "$(pre $S2 toolu_nb1 "[haiku/low] plan check" "Check the plan at ~/.claude/orchestrator/runs/$R/PLAN.md against its must_haves." orch-plan-checker)"
ok "brief names run X by its runs/ path while ACTIVE is Y: the nudge names X" "$(outctx)" "$(NUDGE 'No session holds the run lease' orch-plan-checker $R)"
hook pretool "$(pre $S2 toolu_nb2 "[haiku/low] $R#6.1 x" "Orch-Attempt: $O#2.1
See runs/$R/ too.")"
ok "the brief's Orch-Attempt line wins over the description ID and a path" "$(outctx)" "$(NUDGE 'No session holds the run lease' orch-scout $O)"
hook pretool "$(pre $S2 toolu_nb3 "[haiku/low] plain" "Read runs/zz-missing/PLAN.md and runs/$R/1-x.md")"
ok "a path to a missing run folder is skipped for the next path" "$(outctx)" "$(NUDGE 'No session holds the run lease' orch-scout $R)"
ok "brief-run nudges and the silent no-run launches wrote nothing (tree, errors.log, no decision)" "$(treesum)|$(errs)|$(nodecision)" "$s0|$e0|none"
hook pretool "$(pre $S2 toolu_nl4 "[haiku/low] $ID6 x" "x" Explore)"
ok "a non-orch agent type: silent" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
hook pretool "$(pre $S2 toolu_nl5 "[haiku/low] $ID6 x" "x" | jq -c '. + {agent_id:"a77", agent_type:"orch-scout"}')"
ok "from a subagent (agent_id): silent" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
: > "$ORCH_DIR/ACTIVE"; hook pretool "$(pre $S2 toolu_nl6 "[haiku/low] plain, no id" "x")"
ok "ACTIVE empty and no ID: silent" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
echo "$ORCH_RUNS_DIR/gone-run" > "$ORCH_DIR/ACTIVE"; hook pretool "$(pre $S2 toolu_nl7 "[haiku/low] plain, no id" "x")"
ok "ACTIVE names a missing folder and no ID: silent" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
echo "$ORCH_RUNS_DIR/$O" > "$ORCH_DIR/ACTIVE"
jq -cn --arg s $S1 --arg r $R --arg p "$RD" '{v:1, run:$r, path:$p, session:$s, acquired:"2026-01-01T01:00:00.000Z", renewed:"2026-01-01T01:00:00.000Z", expires:"2026-01-01T03:04:00.000Z", ttl_sec:7200}' > "$ORCH_DIR/ACTIVE.lease"
s0=$(treesum); e0=$(errs); hook pretool "$(pre $S2 toolu_nl8 "[haiku/low] $ID6 x" "x")"
ok "another session's expired lease: the nudge says so" "$(outctx)" "$(NUDGE "The run lease expired (last held by ${S1:0:8} for run $R)" orch-scout $R)"
ok "expired-lease nudge writes nothing" "$(treesum)|$(errs)|$(nodecision)" "$s0|$e0|none"
jq -c '.expires = "2099-01-01T00:00:00.000Z"' "$ORCH_DIR/ACTIVE.lease" > "$T/l.t" && mv "$T/l.t" "$ORCH_DIR/ACTIVE.lease"
hook pretool "$(pre $S2 toolu_nl9 "[haiku/low] $ID6 x" "x")"
ok "a live lease held by another session: silent (no nudge)" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
rm "$ORCH_DIR/ACTIVE.lease"
node "$MAN" lease acquire --run $R --sid $S1 >/dev/null 2>&1

echo "== prompt (holder): two notices mixed with owner text"
meta a12f7f52584ff3573 "$D6" $TU6 haiku low
meta a7777 "[sonnet/medium] $ID7 second" $TU7 sonnet medium
PR="owner text before
$(notice a12f7f52584ff3573 $TU6 "$D6" 43418 7 21708)
owner text between
$(notice a7777 $TU7 "[sonnet/medium] $ID7 second" 50000 12 90000)
owner text after"
PJ=$(jq -cn --arg s $S1 --arg p "$PR" --arg tp "$TR" '{session_id:$s, transcript_path:$tp, hook_event_name:"UserPromptSubmit", prompt:$p}')
: > "$PROBE_OUT"; NODE_OPTIONS="--require $T/probe.js" hook prompt "$PJ"
ok "prompt: exit 0, no decision" "$(cat "$T/rc")|$(nodecision)" "0|none"
ok "prompt: two final stops via prompt" "$(evn '.type=="stop" and .via=="prompt" and .final==true')" "2"
ok "prompt: golden ledger line (tokens 43418, tools 7, ms 21708, haiku/low, orch-scout, retries 0)" "$(jq -c --arg id "$ID6" 'select(.id==$id) | [.tokens, .tools, .ms, .model, .effort, .agent, .retries] | map(tostring) | join(" ")' -r "$LFILE")" "43418 7 21708 haiku low orch-scout 0"
ok "prompt: second ledger line (sonnet/medium)" "$(jq -r --arg id "$ID7" 'select(.id==$id) | "\(.tokens) \(.model)/\(.effort)"' "$LFILE")" "50000 sonnet/medium"
ok "prompt: one context line per new ledger line" "$(outctx | grep -c '^\[orch\] ledger: .* tokens .*, tools .*, ms .*\. Close: ledger\.js close --attempt ')|$(outctx | grep -cF "[orch] ledger: $ID6 tokens 43418, tools 7, ms 21708. Close: ledger.js close --attempt $ID6 (+ --first-pass --outcome)")" "2|1"
ok "prompt (holder): ledger and manifest loaded, sqlite never" "$(jq -r '"\(.sqlite) \(.mods | join(","))"' "$PROBE_OUT")" "false lease-store.js,ledger.js,manifest.js"
l0=$(wc -l < "$LFILE"); m0=$(wc -l < "$MFILE"); hook prompt "$PJ"
ok "same prompt again: no new stop, no new ledger line, silent" "$(wc -l < "$MFILE" | tr -d ' ')|$(wc -l < "$LFILE" | tr -d ' ')|$(wc -c < "$T/out" | tr -d ' ')" "$m0|$l0|0"
hook prompt "$(jq -cn --arg s $S1 --arg p "$(notice anoid1 toolu_noid "[haiku/low] Smoke test orch-scout" 43418 7 21708)" --arg tp "$TR" '{session_id:$s, transcript_path:$tp, prompt:$p}')"
ok "a notice with no ID and no start: no stop, no ledger line, silent" "$(wc -l < "$MFILE" | tr -d ' ')|$(wc -l < "$LFILE" | tr -d ' ')|$(wc -c < "$T/out" | tr -d ' ')" "$m0|$l0|0"
hook prompt "$(jq -cn --arg s $S1 --arg tp "$TR" '{session_id:$s, transcript_path:$tp, prompt:"just the owner typing"}')"
ok "plain owner prompt: silent, exit 0" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"

echo "== stop (holder): sweep over a fake transcript with an enqueue record"
TU8=toolu_8888; hook pretool "$(pre $S1 $TU8 "[haiku/low] $ID8 third" "Orch-Attempt: $ID8")"
meta a8888 "[haiku/low] $ID8 third" $TU8 haiku low
jq -cn --arg c "$(notice a8888 $TU8 "[haiku/low] $ID8 third" 30000 4 15000)" --arg ts "$(date -u -d '+1 min' +%Y-%m-%dT%H:%M:%S.000Z)" \
  '{type:"queue-operation",operation:"enqueue",timestamp:$ts,content:$c}' >> "$TR"
: > "$PROBE_OUT"; NODE_OPTIONS="--require $T/probe.js" hook stop "$(jq -cn --arg s $S1 --arg tp "$TR" '{session_id:$s, transcript_path:$tp, hook_event_name:"Stop", stop_hook_active:false}')"
ok "stop: exit 0, silent" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
ok "stop: final stop via sweep for $ID8" "$(evn ".type==\"stop\" and .attempt==\"$ID8\" and .via==\"sweep\" and .final==true")" "1"
ok "stop: ledger line for $ID8 (30000 4 15000)" "$(jq -r --arg id "$ID8" 'select(.id==$id) | "\(.tokens) \(.tools) \(.ms)"' "$LFILE")" "30000 4 15000"
ok "stop (holder): sqlite never loaded" "$(jq -r .sqlite "$PROBE_OUT")" "false"
m0=$(wc -l < "$MFILE"); hook stop "$(jq -cn --arg s $S1 --arg tp "$TR" '{session_id:$s, transcript_path:$tp}')"
ok "stop again: no new events (dedupe)" "$(wc -l < "$MFILE" | tr -d ' ')" "$m0"

echo "== stop sweep offset (per session, in <data>/sweep-state): only the bytes added since the last sweep are read"
cat > "$T/bytes.js" <<'BYTESEOF'
// counts the bytes read from the file named by SWEEP_TR (the transcript); written on exit
const fs = require('fs'), fds = new Set(); let n = 0;
const open = fs.openSync, read = fs.readSync;
fs.openSync = function (p, ...a) { const fd = open.call(this, p, ...a); if (p === process.env.SWEEP_TR) fds.add(fd); return fd; };
fs.readSync = function (fd, ...a) { const r = read.call(this, fd, ...a); if (fds.has(fd)) n += r; return r; };
process.on('exit', () => { try { fs.appendFileSync(process.env.SWEEP_OUT, n + '\n'); } catch {} });
BYTESEOF
export SWEEP_TR="$TR" SWEEP_OUT="$T/bytes.out"
stopread(){ : > "$SWEEP_OUT"; NODE_OPTIONS="--require $T/bytes.js" hook stop "$(jq -cn --arg s $S1 --arg tp "$TR" '{session_id:$s, transcript_path:$tp, hook_event_name:"Stop"}')"; tail -1 "$SWEEP_OUT"; }
fullread(){ awk -v t="$(stat -c%s "$TR")" '{print ($1==t)?"full":"read "$1" of "t}'; }
SSF="$ORCH_DIR/sweep-state/$S1.json"
ok "the hook saved the sweep state: this transcript, offset = its size, the held run" "$(jq -r --arg f "$TR" --argjson sz "$(stat -c%s "$TR")" '"\(.file == $f) \(.offset == $sz) \(.held)"' "$SSF")" "true true $R"
ok "a second stop over the unchanged transcript reads 0 bytes of it" "$(stopread)" "0"
TU9=toolu_9999; ID9=$(launch 9 haiku low); meta a9999 "[haiku/low] $ID9 fourth" $TU9 haiku low
SZ0=$(stat -c%s "$TR")
jq -cn --arg c "$(notice a9999 $TU9 "[haiku/low] $ID9 fourth" 20000 3 12000)" --arg ts "$(date -u -d '+2 min' +%Y-%m-%dT%H:%M:%S.000Z)" '{type:"queue-operation",operation:"enqueue",timestamp:$ts,content:$c}' >> "$TR"
SZ1=$(stat -c%s "$TR"); ok "after a notice is appended, the next stop reads exactly the appended bytes (not the whole transcript)" "$(stopread | awk -v a="$((SZ1 - SZ0))" -v t="$SZ1" '{print ($1==a && a<t)?"appended bytes only":"read "$1" of "t" (appended "a")"}')" "appended bytes only"
ok "the appended notice was ingested once (one sweep stop + its ledger line)" "$(evn ".type==\"stop\" and .attempt==\"$ID9\" and .via==\"sweep\"")|$(jq -r --arg id "$ID9" 'select(.id==$id) | "\(.tokens) \(.tools) \(.ms)"' "$LFILE")" "1|20000 3 12000"
ok "and the stop after that reads 0 bytes again, still one stop for $ID9" "$(stopread)|$(evn ".type==\"stop\" and .attempt==\"$ID9\"")" "0|1"
echo "-- a record still being written (no newline yet) is not lost: it is ingested once when its newline lands"
TU10=toolu_1010; ID10=$(launch 10 haiku low); meta a1010 "[haiku/low] $ID10 fifth" $TU10 haiku low
REC=$(jq -cn --arg c "$(notice a1010 $TU10 "[haiku/low] $ID10 fifth" 10000 2 8000)" --arg ts "$(date -u -d '+3 min' +%Y-%m-%dT%H:%M:%S.000Z)" '{type:"queue-operation",operation:"enqueue",timestamp:$ts,content:$c}')
printf '%s' "${REC:0:200}" >> "$TR"; hook stop "$(jq -cn --arg s $S1 --arg tp "$TR" '{session_id:$s, transcript_path:$tp}')"
ok "half a record: nothing ingested yet" "$(evn ".type==\"stop\" and .attempt==\"$ID10\"")" "0"
printf '%s\n' "${REC:200}" >> "$TR"; hook stop "$(jq -cn --arg s $S1 --arg tp "$TR" '{session_id:$s, transcript_path:$tp}')"
ok "the rest of the record arrives: it is ingested once" "$(evn ".type==\"stop\" and .attempt==\"$ID10\" and .via==\"sweep\"")" "1"
echo "-- a notice whose ingest failed (manifest locked) is not skipped: the offset stays in front of it and the next stop retries"
TU11=toolu_1111; ID11=$(launch 11 haiku low); meta a1111 "[haiku/low] $ID11 sixth" $TU11 haiku low
SZ0=$(stat -c%s "$TR")
jq -cn --arg c "$(notice a1111 $TU11 "[haiku/low] $ID11 sixth" 9000 1 7000)" --arg ts "$(date -u -d '+4 min' +%Y-%m-%dT%H:%M:%S.000Z)" '{type:"queue-operation",operation:"enqueue",timestamp:$ts,content:$c}' >> "$TR"
mkdir "$MFILE.lock"; hook stop "$(jq -cn --arg s $S1 --arg tp "$TR" '{session_id:$s, transcript_path:$tp}')"; rmdir "$MFILE.lock"
ok "manifest locked during the sweep: nothing ingested, exit 0, and the saved offset is still where the notice starts" "$(evn ".type==\"stop\" and .attempt==\"$ID11\"")|$(cat "$T/rc")|$(jq -r --argjson o "$SZ0" '.offset == $o' "$SSF")" "0|0|true"
SZ1=$(stat -c%s "$TR"); ok "the next stop (lock gone) re-reads from the failed notice and ingests it once" "$(stopread | awk -v a="$((SZ1 - SZ0))" '{print ($1==a)?"retried":"read "$1" want "a}')|$(evn ".type==\"stop\" and .attempt==\"$ID11\" and .via==\"sweep\"")|$(jq -r --arg id "$ID11" 'select(.id==$id) | "\(.tokens) \(.tools) \(.ms)"' "$LFILE")" "retried|1|9000 1 7000"
ok "and then the offset moved on: the stop after reads 0 bytes" "$(stopread)" "0"
echo "-- a transcript that shrank (rotated) or is another file is swept from the tail again; so is a changed held-run set"
jq -c '.offset = .offset + 1000000' "$SSF" > "$T/ss.x" && mv "$T/ss.x" "$SSF"
ok "a saved offset beyond the file size (it shrank): full sweep again" "$(stopread | fullread)" "full"
ok "the state was reset to the file size" "$(jq -r --argjson sz "$(stat -c%s "$TR")" '.offset == $sz' "$SSF")" "true"
jq -c '.file = "/somewhere/else.jsonl"' "$SSF" > "$T/ss.x" && mv "$T/ss.x" "$SSF"
ok "state kept for another transcript path: full sweep again" "$(stopread | fullread)" "full"
jq -c '.held = "some-other-run"' "$SSF" > "$T/ss.x" && mv "$T/ss.x" "$SSF"
ok "state kept for a different held-run set: full sweep again" "$(stopread | fullread)" "full"
printf 'not json' > "$SSF"; ok "a junk state file: full sweep, exit 0" "$(stopread | fullread)|$(cat "$T/rc")" "full|0"
ok "the full re-sweeps added no events (dedupe): one sweep stop each for $ID8 $ID9 $ID10 $ID11" "$(evn '.type=="stop" and .via=="sweep"')" "4"
unset SWEEP_TR SWEEP_OUT

echo "== substop (holder)"
SJ(){ jq -cn --arg s $S1 --arg a "$1" --arg tp "$SUB/agent-$1.jsonl" --arg at "$2" --arg msg "$3" '{session_id:$s, hook_event_name:"SubagentStop", agent_id:$a, agent_type:$at, agent_transcript_path:$tp, last_assistant_message:$msg, stop_hook_active:false}'; }
hook substop "$(SJ a12f7f52584ff3573 orch-scout "hello")"
ok "substop by description ID: agent-stopped, final false, result_bytes 5, raw agent_type" "$(ev '.type=="stop" and .via=="substop"' | jq -r '[.attempt, .phase, .final, .result_bytes, .agent, .tool_use_id, .model, .effort, .key] | map(tostring) | join(" ")')" "$ID6 agent-stopped false 5 orch-scout $TU6 haiku low a12f7f52584ff3573:substop:0:5"
ok "substop: exit 0, silent" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
hook substop "$(SJ a12f7f52584ff3573 boss:orch-scout "hello!")"
ok "substop with agent_type boss:orch-scout: the event records orch-scout (plugin prefix stripped)" "$(ev '.type=="stop" and .via=="substop" and .result_bytes==6' | jq -r .agent)" "orch-scout"
meta aunpl1 "[haiku/low] plain launch, no id" $TUU haiku low
hook substop "$(SJ aunpl1 general-purpose "x")"
ok "substop mapped by the start's toolUseId (unplanned): attempt null in the lease run" "$(ev '.type=="stop" and .task_id=="aunpl1"' | jq -r '"\(.attempt) \(.run) \(.agent)"')" "null $R general-purpose"
meta anomap1 "[haiku/low] unrelated" toolu_nomatch haiku low; m0=$(wc -l < "$MFILE")
hook substop "$(SJ anomap1 orch-scout "x")"; ok "substop unmapped: nothing" "$(wc -l < "$MFILE" | tr -d ' ')|$(cat "$T/rc")" "$m0|0"
hook substop "$(SJ amissing orch-scout "x")"; ok "substop with no meta.json: nothing" "$(wc -l < "$MFILE" | tr -d ' ')|$(cat "$T/rc")" "$m0|0"
ok "substop with no transcript file: model_id null on the stop (never guessed)" "$(ev '.type=="stop" and .via=="substop" and .result_bytes==5' | jq -r .model_id)" "null"
{ echo '{"type":"user","message":{"role":"user","content":"brief"}}'; echo '{"type":"attachment","attachment":{"type":"model","identity":{"modelId":"claude-haiku-5-5"}}}'; } > "$SUB/agent-a12f7f52584ff3573.jsonl"
hook substop "$(SJ a12f7f52584ff3573 orch-scout "hello!!")"
ok "row 19: substop reads model_id from the transcript's model attachment (no assistant record yet)" "$(ev '.type=="stop" and .via=="substop" and .result_bytes==7' | jq -r '"\(.model_id) \(.final)"')|$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "claude-haiku-5-5 false|0|0"
echo 'not json' > "$SUB/agent-a12f7f52584ff3573.jsonl"; hook substop "$(SJ a12f7f52584ff3573 orch-scout "hello!!!")"
ok "row 19: substop with an unreadable transcript still writes its stop (model_id null, exit 0)" "$(ev '.type=="stop" and .via=="substop" and .result_bytes==8' | jq -r .model_id)|$(cat "$T/rc")" "null|0"
rm -f "$SUB/agent-a12f7f52584ff3573.jsonl"

echo "== session (SessionStart compact|resume): CT-12, no file writes"
SS(){ jq -cn --arg s "$1" '{session_id:$s, hook_event_name:"SessionStart", source:"compact"}'; }
LEASE=$(lf $R); cp "$LEASE" "$T/lease.keep"; s0=$(treesum)
hook session "$(SS $S1)"
ok "lease mine, ACTIVE names another run: reports the lease's run" "$(outctx)" "[orch] Active run: $R (lease: this session). Run /boss:resume $R before launching anything (it reconciles running rows)."
jq -c '.path="/elsewhere/runs-x/t-run"' "$LEASE" > "$T/lease.far" && cp "$T/lease.far" "$LEASE"; hook session "$(SS $S1)"
ok "session: a lease path outside the runs dir keeps the full path (the name alone would not resolve)" "$(outctx)" "[orch] Active run: /elsewhere/runs-x/t-run (lease: this session). Run /boss:resume t-run before launching anything (it reconciles running rows)."
cp "$T/lease.keep" "$LEASE"; hook session "$(SS $S1)"
ok "session: a lease path of RUNS/<name> shows the name: the line stays short (under 150 bytes)" "$(outctx | wc -c | tr -d ' ' | awk '{print ($1 < 150) ? "short" : "long"}')" "short"
ok "session output is SessionStart additionalContext" "$(jq -r .hookSpecificOutput.hookEventName "$T/out")" "SessionStart"
hook session "$(SS $S2)"; ok "S2 holds no lease, ACTIVE names unleased $O: silent" "$(wc -c < "$T/out" | tr -d ' ')|$(cat "$T/rc")" "0|0"
echo "$RD" > "$ORCH_DIR/ACTIVE"; hook session "$(SS $S2)"
ok "S2 holds no lease, ACTIVE names $R held live by S1: silent (no warning text either)" "$(wc -c < "$T/out" | tr -d ' ')|$(cat "$T/rc")" "0|0"
jq -c '.expires = "2026-01-01T03:04:00.000Z"' "$T/lease.keep" > "$LEASE"
hook session "$(SS $S2)"; ok "S2 holds no lease, ACTIVE's run lease expired (S1's): silent" "$(wc -c < "$T/out" | tr -d ' ')" "0"
hook session "$(SS $S1)"; ok "own lease expired: expired since (lease run)" "$(outctx | grep -o '(lease: [^)]*)')" "(lease: expired since 03:04 UTC)"
rm "$LEASE"; hook session "$(SS $S2)"; ok "no lease at all, ACTIVE names $R: silent" "$(wc -c < "$T/out" | tr -d ' ')" "0"
: > "$ORCH_DIR/ACTIVE"; hook session "$(SS $S2)"; ok "ACTIVE empty, no lease: silent" "$(wc -c < "$T/out" | tr -d ' ')" "0"
hook session "$(SS $S1 | jq -c '. + {agent_id:"a1"}')"; ok "session with agent_id: silent" "$(wc -c < "$T/out" | tr -d ' ')" "0"
echo "$ORCH_RUNS_DIR/$O" > "$ORCH_DIR/ACTIVE"; cp "$T/lease.keep" "$LEASE"
ok "session mode wrote nothing (tree restored identical)" "$(treesum)" "$s0"

echo "== failures: exit 0, nothing written, one errors.log line"
for m in pretool prompt stop substop session; do
  m0=$(wc -l < "$MFILE"); e0=$(errs); hook $m '{not json'
  ok "bad JSON ($m): exit 0, silent, manifest unchanged, logged" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')|$(wc -l < "$MFILE" | tr -d ' ')|$(( $(errs) - e0 ))" "0|0|$m0|1"
done
e0=$(errs); hook bogus '{}'; ok "unknown mode: exit 0, logged" "$(cat "$T/rc")|$(( $(errs) - e0 ))" "0|1"
e0=$(errs); m0=$(wc -l < "$MFILE"); hook pretool "$(pre $S1 toolu_zz "[haiku/low] zz-missing#1.1 x" "Orch-Attempt: zz-missing#1.1")"
ok "ID for a run this session doesn't hold (folder missing): D1 (1) writes nothing anywhere, no log, no nudge (no ACTIVE fallback)" "$([ -e "$ORCH_RUNS_DIR/zz-missing" ] && echo made || echo none)|$(cat "$T/rc")|$(wc -l < "$MFILE" | tr -d ' ')|$(( $(errs) - e0 ))|$(nodecision)|$(wc -c < "$T/out" | tr -d ' ')" "none|0|$m0|0|none|0"
mkdir "$MFILE.lock"; m0=$(wc -l < "$MFILE"); e0=$(errs)
hook pretool "$(pre $S1 toolu_locked "[haiku/low] $ID6 again" "Orch-Attempt: $ID6")"
ok "manifest lock busy: start skipped, exit 0, logged, no decision" "$(wc -l < "$MFILE" | tr -d ' ')|$(cat "$T/rc")|$(grep -c 'lock busy' "$ORCH_DIR/errors.log")|$(nodecision)" "$m0|0|1|none"
rmdir "$MFILE.lock"
mkdir -p "$T/adir"; e0=$(errs); hook stop "$(jq -cn --arg s $S1 --arg tp "$T/adir" '{session_id:$s, transcript_path:$tp}')"
ok "transcript path is a directory: exit 0, logged" "$(cat "$T/rc")|$(( $(errs) - e0 ))" "0|1"
head -c 300000 /dev/zero | tr '\0' 'x' | fold -w 99 >> "$ORCH_DIR/errors.log"; hook bogus '{}'
ok "errors.log capped (dropped the older half)" "$([ "$(wc -c < "$ORCH_DIR/errors.log")" -le 262144 ] && echo capped || echo big)|$(tail -1 "$ORCH_DIR/errors.log" | grep -c "unknown mode")" "capped|1"

echo "== D1 (per-run leases): which run a hook writes to; this tip's manifest.js writes only ACTIVE.lease, so leases/<run>.json are hand-written"
FAR=2099-01-01T00:00:00.000Z; PAST=2026-01-01T03:04:00.000Z
# plf <orch dir> <run> <sid> <expires> [renewed]: hand-write a per-run lease file (D-1 shape)
plf(){ mkdir -p "$1/leases"; jq -cn --arg r "$2" --arg s "$3" --arg e "$4" --arg n "${5:-2026-10-21T08:00:00.000Z}" --arg p "$1/runs/$2" '{v:1, run:$r, path:$p, session:$s, acquired:$n, renewed:$n, expires:$e, ttl_sec:7200}' > "$1/leases/$2.json"; }
# legacy <orch dir> <run> <sid> <expires>: hand-write the legacy global ACTIVE.lease (live shape)
legacy(){ jq -cn --arg r "$2" --arg s "$3" --arg e "$4" --arg p "$1/runs/$2" '{v:1, run:$r, path:$p, session:$s, acquired:"2026-10-21T07:00:00.000Z", renewed:"2026-10-21T07:00:00.000Z", expires:$e, ttl_sec:7200}' > "$1/ACTIVE.lease"; }
hk(){ local d=$1; shift; ORCH_DIR="$d" ORCH_RUNS_DIR="$d/runs" hook "$@"; }        # hook against another scratch orch dir
errsd(){ if [ -f "$1/errors.log" ]; then wc -l < "$1/errors.log" | tr -d ' '; else echo 0; fi; }
mlines(){ if [ -f "$1/runs/$2/manifest.jsonl" ]; then wc -l < "$1/runs/$2/manifest.jsonl" | tr -d ' '; else echo 0; fi; }
mev(){ jq -c "select($3)" "$1/runs/$2/manifest.jsonl" 2>/dev/null; }
DB="$T/d1/orch"; mkdir -p "$DB/runs/t-run" "$DB/runs/t-run2" "$DB/runs/t-other" "$T/d1/sub"; echo "$DB/runs/t-other" > "$DB/ACTIVE"
plf "$DB" t-run $S1 $FAR; plf "$DB" t-other $S2 $FAR
dmeta(){ jq -cn --arg d "$2" --arg tu "$3" '{agentType:"orch-scout", description:$d, toolUseId:$tu, spawnDepth:1, model:"haiku", effort:"low", finished:true}' > "$T/d1/sub/agent-$1.meta.json"; }
DSJ(){ jq -cn --arg s "$1" --arg a "$2" --arg tp "$T/d1/sub/agent-$2.jsonl" '{session_id:$s, hook_event_name:"SubagentStop", agent_id:$a, agent_type:"orch-scout", agent_transcript_path:$tp, last_assistant_message:"hi", stop_hook_active:false}'; }
three(){ echo "$(mlines "$DB" t-run) $(mlines "$DB" t-run2) $(mlines "$DB" t-other)"; }
c0=$(three); e0=$(errsd "$DB")
hk "$DB" pretool "$(pre $S2 toolu_d1a "[haiku/low] t-run#1.1 x" "Orch-Attempt: t-run#1.1")"
ok "S2 holds t-other, launches an ID for t-run (S1 holds it live): nothing written to either run, silent, no log" "$(three)|$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')|$(( $(errsd "$DB") - e0 ))" "$c0|0|0|0"
dmeta ad1a "[haiku/low] t-run#1.1 x" toolu_d1a; hk "$DB" substop "$(DSJ $S2 ad1a)"
ok "S2 substop for that t-run ID: nothing written, silent" "$(three)|$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')|$(( $(errsd "$DB") - e0 ))" "$c0|0|0|0"
mv "$DB/leases/t-run.json" "$T/d1/t-run.keep"
hk "$DB" pretool "$(pre $S2 toolu_d1a2 "[haiku/low] t-run#1.1 x" "Orch-Attempt: t-run#1.1")"
ok "same launch while t-run is unleased: one t-run nudge, still nothing written (pretool may still nudge)" "$(three)|$(outctx)|$(nodecision)" "$c0|$(NUDGE 'No session holds the run lease' orch-scout t-run)|none"
mv "$T/d1/t-run.keep" "$DB/leases/t-run.json"
hk "$DB" pretool "$(pre $S1 toolu_d1n1 "[haiku/low] plan check" "Check runs/t-other/PLAN.md" orch-plan-checker)"
ok "S1 (holds t-run only) cites t-other, held live by S2: no nudge, D1 (3) writes the unplanned start to t-run" "$(wc -c < "$T/out" | tr -d ' ')|$(mev "$DB" t-run '.key=="toolu_d1n1"' | jq -r '"\(.unplanned) \(.attempt)"')|$(mlines "$DB" t-other)" "0|true null|0"
plf "$DB" t-run2 $S1 $FAR 2026-10-21T09:00:00.000Z
echo "-- S1 now holds t-run and t-run2"
c0=$(three); e0=$(errsd "$DB")
hk "$DB" pretool "$(pre $S1 toolu_d1b1 "[haiku/low] plain launch" "Read runs/t-run2/PLAN.md and do row 3.")"
ok "brief citing only runs/t-run2/: unplanned start in t-run2, none in t-run, silent" "$(mev "$DB" t-run2 '.key=="toolu_d1b1"' | jq -r '"\(.unplanned) \(.attempt) \(.session)"')|$(mev "$DB" t-run '.key=="toolu_d1b1"' | wc -l | tr -d ' ')|$(wc -c < "$T/out" | tr -d ' ')|$(( $(errsd "$DB") - e0 ))" "true null $S1|0|0|0"
hk "$DB" pretool "$(pre $S1 toolu_d1b4 "[haiku/low] plain launch" "Spec: runs/t-other/PLAN.md. Work: runs/t-run2/3-x.md")"
ok "brief citing t-other (not held) and t-run2: exactly one held, so t-run2" "$(mev "$DB" t-run2 '.key=="toolu_d1b4"' | wc -l | tr -d ' ')|$(mlines "$DB" t-other)" "1|0"
c1=$(three); e1=$(errsd "$DB")
hk "$DB" pretool "$(pre $S1 toolu_d1b2 "[haiku/low] plain launch" "Compare runs/t-run/PLAN.md with runs/t-run2/PLAN.md")"
ok "brief citing both held runs: nothing written, one errors.log line, silent, exit 0" "$(three)|$(( $(errsd "$DB") - e1 ))|$(tail -1 "$DB/errors.log" | grep -c 'unplanned start toolu_d1b2 not written: this session holds 2 leases (t-run, t-run2)')|$(wc -c < "$T/out" | tr -d ' ')|$(cat "$T/rc")" "$c1|1|1|0|0"
hk "$DB" pretool "$(pre $S1 toolu_d1b3 "[haiku/low] plain launch" "no run named here")"
ok "brief naming no run, two leases held: nothing written, one errors.log line (D1 4)" "$(three)|$(( $(errsd "$DB") - e1 ))|$(wc -c < "$T/out" | tr -d ' ')" "$c1|2|0"
hk "$DB" pretool "$(pre $S1 toolu_d1b5 "[haiku/low] t-run#2.1 x" "Orch-Attempt: t-run#2.1
Spec: runs/t-run2/PLAN.md")"
ok "an ID for t-run with a brief citing t-run2: the ID ends the chain (t-run)" "$(mev "$DB" t-run '.key=="toolu_d1b5"' | jq -r .attempt)|$(mev "$DB" t-run2 '.key=="toolu_d1b5"' | wc -l | tr -d ' ')" "t-run#2.1|0"
c1=$(three); e1=$(errsd "$DB")
hk "$DB" pretool "$(pre $S1 toolu_d1b6 "[haiku/low] t-other#1.1 x" "Orch-Attempt: t-other#1.1
See runs/t-run2/")"
ok "an ID for t-other (held live by S2) from S1: nothing anywhere, no nudge, no log" "$(three)|$(wc -c < "$T/out" | tr -d ' ')|$(( $(errsd "$DB") - e1 ))" "$c1|0|0"
dmeta ad1b1 "[haiku/low] plain launch" toolu_d1b1; hk "$DB" substop "$(DSJ $S1 ad1b1)"
ok "substop without an ID finds t-run2 by toolUseId" "$(mev "$DB" t-run2 '.type=="stop" and .task_id=="ad1b1"' | jq -r '"\(.attempt) \(.tool_use_id) \(.phase)"')|$(mev "$DB" t-run '.task_id=="ad1b1"' | wc -l | tr -d ' ')" "null toolu_d1b1 agent-stopped|0"
dmeta ad1b5 "[haiku/low] t-run#2.1 x" toolu_d1b5; hk "$DB" substop "$(DSJ $S1 ad1b5)"
ok "substop with a held ID: that run" "$(mev "$DB" t-run '.type=="stop" and .task_id=="ad1b5"' | jq -r .attempt)" "t-run#2.1"
c1=$(three); dmeta ad1b6 "[haiku/low] t-other#1.1 x" toolu_d1b6; hk "$DB" substop "$(DSJ $S1 ad1b6)"
dmeta ad1b9 "[haiku/low] plain" toolu_nowhere; hk "$DB" substop "$(DSJ $S1 ad1b9)"
ok "substop with an ID S1 doesn't hold, and one whose start is in no held run: nothing" "$(three)|$(cat "$T/rc")" "$c1|0"
mev "$DB" t-run2 '.key=="toolu_d1b1"' >> "$DB/runs/t-run/manifest.jsonl"; e1=$(errsd "$DB")
dmeta ad1b1x "[haiku/low] plain launch" toolu_d1b1; hk "$DB" substop "$(DSJ $S1 ad1b1x)"
ok "substop whose start is in both held runs: nothing written, one errors.log line" "$(mev "$DB" t-run '.task_id=="ad1b1x"' | wc -l | tr -d ' ')$(mev "$DB" t-run2 '.task_id=="ad1b1x"' | wc -l | tr -d ' ')|$(( $(errsd "$DB") - e1 ))" "00|1"
s1=$( (cd "$DB" && find . -type f ! -name errors.log | sort | xargs md5sum) | md5sum)
hk "$DB" session "$(SS $S1)"
ok "session, two leases held: one line naming both runs" "$(outctx)" "[orch] This session holds leases for runs t-run, t-run2. Run /boss:resume <run> for the run you drive before launching anything."
jq -c --arg e $PAST '.expires=$e' "$DB/leases/t-run2.json" > "$T/d1/x" && mv "$T/d1/x" "$DB/leases/t-run2.json"
hk "$DB" session "$(SS $S1)"; ok "session: an expired own lease still counts as held" "$(outctx | grep -c 'holds leases for runs t-run, t-run2')" "1"
rm "$DB/leases/t-run2.json"; hk "$DB" session "$(SS $S1)"; ok "session, one per-run lease held: CT-12 for it" "$(outctx)" "$(CT12 "$DB/runs/t-run" 'this session')"
hk "$DB" session "$(SS $S2)"; ok "session, S2 holds t-other (ACTIVE's run): CT-12 for it" "$(outctx)" "$(CT12 "$DB/runs/t-other" 'this session')"
hk "$DB" session "$(SS sess-none-9191)"; ok "session, no lease held, ACTIVE's run held live by S2: silent" "$(wc -c < "$T/out" | tr -d ' ')|$(cat "$T/rc")" "0|0"
for m in prompt stop; do hk "$DB" $m "$(jq -cn --arg s $S1 --arg tp "$T/d1/none.jsonl" '{session_id:$s, transcript_path:$tp, prompt:"owner text"}')"; done
ok "prompt and stop for the per-run holder: exit 0, silent" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')|$(wc -c < "$T/err" | tr -d ' ')" "0|0|0"

echo "== legacy fixture (the 08:20 case at hook level): S16 holds t-run through the live-shape ACTIVE.lease, S35 holds nothing"
S16=sess-s16-legacy-1616; S35=sess-s35-driver-3535; LG="$T/lg/orch"; mkdir -p "$LG/runs/t-run" "$LG/runs/t-other"; echo "$LG/runs/t-run" > "$LG/ACTIVE"
legacy "$LG" t-run $S16 $FAR; cp "$LG/ACTIVE.lease" "$T/lg/lease.0"
hk "$LG" pretool "$(pre $S16 toolu_lg1 "[haiku/low] t-run#1.1 x" "Orch-Attempt: t-run#1.1")"
ok "S16's ID writes its start to t-run" "$(mev "$LG" t-run '.key=="toolu_lg1"' | jq -r '"\(.attempt) \(.session)"')" "t-run#1.1 $S16"
hk "$LG" pretool "$(pre $S16 toolu_lg2 "[haiku/low] plain" "no run named")"
ok "S16 unplanned launch: the only lease (D1 3), t-run" "$(mev "$LG" t-run '.key=="toolu_lg2"' | jq -r .unplanned)" "true"
dmeta alg1 "[haiku/low] t-run#1.1 x" toolu_lg1; hk "$LG" substop "$(DSJ $S16 alg1)"
ok "S16 substop by ID: t-run" "$(mev "$LG" t-run '.type=="stop" and .task_id=="alg1"' | jq -r .attempt)" "t-run#1.1"
lgs=$( (cd "$LG" && find . -type f ! -name errors.log | sort | xargs md5sum) | md5sum); : > "$PROBE_OUT"
NODE_OPTIONS="--require $T/probe.js" hk "$LG" pretool "$(pre $S35 toolu_lg3 "[sonnet/high] plan check" "Check runs/t-other/PLAN.md against its must_haves." orch-plan-checker)"
ok "S35 (no lease, brief names t-other) gets the t-other nudge; S16's live legacy lease on t-run doesn't silence it" "$(outctx)|$(nodecision)" "$(NUDGE 'No session holds the run lease' orch-plan-checker t-other)|none"
ok "S35's nudge loads only lease-store.js" "$(jq -r '"\(.sqlite) \(.mods | join(","))"' "$PROBE_OUT")" "false lease-store.js"
hk "$LG" pretool "$(pre $S35 toolu_lg4 "[sonnet/high] t-run#1.1 x" "Orch-Attempt: t-run#1.1")"
ok "S35 launching for t-run (S16's live legacy lease): silent" "$(wc -c < "$T/out" | tr -d ' ')|$(cat "$T/rc")" "0|0"
hk "$LG" session "$(SS $S35)"
ok "S35 session (no lease; ACTIVE's run t-run held by S16, legacy): silent" "$(wc -c < "$T/out" | tr -d ' ')|$(cat "$T/rc")" "0|0"
hk "$LG" session "$(SS $S16)"; ok "S16 session: CT-12 this session" "$(outctx)" "$(CT12 "$LG/runs/t-run" 'this session')"
ok "S35's calls wrote nothing; the legacy file is byte-identical (reads never write)" "$( (cd "$LG" && find . -type f ! -name errors.log | sort | xargs md5sum) | md5sum)|$(cmp -s "$LG/ACTIVE.lease" "$T/lg/lease.0" && echo same)|$(errsd "$LG")" "$lgs|same|0"
mkdir -p "$LG/leases"; plf "$LG" t-run $S35 $FAR
hk "$LG" pretool "$(pre $S16 toolu_lg5 "[haiku/low] t-run#1.1 x" "Orch-Attempt: t-run#1.1")"
ok "a per-run file for t-run beats the legacy lease: S16 no longer holds t-run, writes nothing" "$(mev "$LG" t-run '.key=="toolu_lg5"' | wc -l | tr -d ' ')|$(cat "$T/rc")" "0|0"
hk "$LG" pretool "$(pre $S35 toolu_lg6 "[haiku/low] t-run#1.1 x" "Orch-Attempt: t-run#1.1")"
ok "...and S35, holding the per-run file, writes there" "$(mev "$LG" t-run '.key=="toolu_lg6"' | jq -r .session)" "$S35"

echo "== retire ACTIVE (row 3): the 12:40 fixture and the notice ingest gate (OD-3) on prompt and stop, in a scratch dir of their own"
GT="$T/gt/orch"; GTP="$T/gt"; mkdir -p "$GT/runs"; for r in g-x g-r g-r2 g-o g-p; do mkdir -p "$GT/runs/$r"; done; : > "$GTP/empty.jsonl"
GA=sess-gate-a-5151; GB=sess-gate-b-5252; G1=sess-gate-1-5353; G2=sess-gate-2-5454
gm(){ ORCH_DIR="$GT" ORCH_RUNS_DIR="$GT/runs" node "$MAN" "$@"; }
gla(){ gm lease acquire --run "$1" --sid="$2" >/dev/null 2>&1; echo $?; }
gl(){ gm launch --run "$1" --row "$2" --role discovery --agent orch-scout --model haiku --effort low --mode wave --repo .claude --area orchestrator | head -1; }
gst(){ gm event --run "$1" --type start --json "{\"attempt\":$2,\"tool_use_id\":\"$3\",\"session\":\"$G1\"}" >/dev/null; }
gsnap(){ echo "$(sum "$GT/runs/$1/manifest.jsonl") $(sum "$GT/runs/$1/ledger.jsonl")"; }
gstops(){ jq -c --arg t "$2" 'select(.type=="stop" and .task_id==$t)' "$GT/runs/$1/manifest.jsonl" 2>/dev/null | wc -l | tr -d ' '; }
gprompt(){ hk "$GT" prompt "$(jq -cn --arg s "$1" --arg p "$2" --arg tp "${3:-$GTP/empty.jsonl}" '{session_id:$s, transcript_path:$tp, hook_event_name:"UserPromptSubmit", prompt:$p}')"; }
gstop(){ hk "$GT" stop "$(jq -cn --arg s "$1" --arg tp "$2" '{session_id:$s, transcript_path:$tp, hook_event_name:"Stop", stop_hook_active:false}')"; }
genq(){ jq -cn --arg c "$2" --arg ts "$(date -u -d '+1 min' +%Y-%m-%dT%H:%M:%S.000Z)" '{type:"queue-operation",operation:"enqueue",timestamp:$ts,content:$c}' >> "$1"; }
SSR(){ jq -cn --arg s "$1" '{session_id:$s, hook_event_name:"SessionStart", source:"resume"}'; }
echo "$GT/runs/g-x" > "$GT/ACTIVE"
ok "gate setup: A takes g-x; S1 takes g-r, g-r2, g-p; launches allocated" "$(gla g-x $GA)$(gla g-r $G1)$(gla g-r2 $G1)$(gla g-p $G1)|$(gl g-o 1) $(gl g-o 2) $(gl g-r 1) $(gl g-r 2) $(gl g-r 3) $(gl g-r2 1) $(gl g-p 1) $(gl g-p 2) $(gl g-p 3)" "0000|g-o#1.1 g-o#2.1 g-r#1.1 g-r#2.1 g-r#3.1 g-r2#1.1 g-p#1.1 g-p#2.1 g-p#3.1"
echo "-- the 12:40 case: ACTIVE names g-x, held live by A; B holds no lease"
s0=$( (cd "$GT" && find . -type f ! -name errors.log | sort | xargs md5sum) | md5sum); e0=$(errsd "$GT")
hk "$GT" session "$(SS $GB)"; ok "12:40: B's SessionStart (compact) prints nothing" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
hk "$GT" session "$(SSR $GB)"; ok "12:40: B's SessionStart (resume) prints nothing" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
hk "$GT" pretool "$(pre $GB toolu_g1240 "[sonnet/high] plan the next thing" "Read the plan and build it." orch-builder)"
ok "12:40: B's orch-builder launch, brief naming no run: prints nothing, no decision" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')|$(nodecision)" "0|0|none"
ok "12:40: B's calls wrote nothing (tree, errors.log)" "$( (cd "$GT" && find . -type f ! -name errors.log | sort | xargs md5sum) | md5sum)|$(( $(errsd "$GT") - e0 ))" "$s0|0"
hk "$GT" session "$(SS $GA)"; ok "12:40: holder A still gets CT-12 for g-x (text unchanged)" "$(outctx)" "$(CT12 "$GT/runs/g-x" 'this session')"
echo "$GT/runs/g-o" > "$GT/ACTIVE"; hk "$GT" session "$(SS $GA)"
ok "A's CT-12 names its held run whatever ACTIVE says" "$(outctx)" "$(CT12 "$GT/runs/g-x" 'this session')"
hk "$GT" session "$(SSR $G1)"; ok "S1 holding three runs gets the several-leases line, whatever ACTIVE says" "$(outctx)" "[orch] This session holds leases for runs g-p, g-r, g-r2. Run /boss:resume <run> for the run you drive before launching anything."
echo "-- prompt: a notice whose ID names a run S1 doesn't hold writes nothing (none:not-held)"
NO=$(notice ag-o1 toolu_g_o1 "[haiku/low] g-o#1.1 other run" 101 1 1001)
so=$(gsnap g-o); lr=$(sum "$GT/runs/g-r/ledger.jsonl"); e0=$(errsd "$GT")
gprompt $G1 "$NO"
ok "S1 prompt, g-o#1.1 notice, g-o unheld: g-o byte-identical, exit 0, silent, no errors.log line" "$(gsnap g-o)|$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')|$(( $(errsd "$GT") - e0 ))" "$so|0|0|0"
ok "...and S1's own g-r is unaffected (no stop for that task, ledger byte-identical)" "$(gstops g-r ag-o1)|$(sum "$GT/runs/g-r/ledger.jsonl")" "0|$lr"
ok "S2 takes g-o" "$(gla g-o $G2)" "0"
NO2=$(notice ag-o3 toolu_g_o3 "[haiku/low] g-o#2.1 other run, held" 102 1 1002)   # its own ID, so an ungated hook could not hide behind a dup
so=$(gsnap g-o); gprompt $G1 "$NO2"
ok "S1 prompt, g-o#2.1 notice, g-o held live by S2: g-o byte-identical, silent" "$(gsnap g-o)|$(wc -c < "$T/out" | tr -d ' ')" "$so|0"
gprompt $G2 "$NO2"
ok "control: S2 (the holder) gets the same notice written to g-o, one context line" "$(gstops g-o ag-o3)|$(jq -r 'select(.id=="g-o#2.1") | .tokens' "$GT/runs/g-o/ledger.jsonl")|$(outctx | grep -c '^\[orch\] ledger: g-o#2.1 tokens ')" "1|102|1"
echo "-- prompt: an ID-less notice links to the held run whose start has its tool_use_id; two such runs write nothing"
gst g-r '"g-r#1.1"' toolu_g_link; sr2=$(gsnap g-r2)
gprompt $G1 "$(notice ag-link toolu_g_link "[haiku/low] no id here" 202 2 2002)"
ok "ID-less notice, start in held g-r: final stop in g-r for g-r#1.1, ledger line, g-r2 untouched" "$(jq -r 'select(.type=="stop" and .task_id=="ag-link") | "\(.attempt) \(.via) \(.final)"' "$GT/runs/g-r/manifest.jsonl")|$(jq -r 'select(.id=="g-r#1.1") | .tokens' "$GT/runs/g-r/ledger.jsonl")|$(gsnap g-r2)" "g-r#1.1 prompt true|202|$sr2"
gst g-r '"g-r#2.1"' toolu_g_amb; gst g-r2 '"g-r2#1.1"' toolu_g_amb
lr=$(sum "$GT/runs/g-r/ledger.jsonl"); lr2=$(sum "$GT/runs/g-r2/ledger.jsonl"); e0=$(errsd "$GT")
gprompt $G1 "$(notice ag-amb toolu_g_amb "[haiku/low] no id, two starts" 303 3 3003)"
ok "ID-less notice, start in g-r and g-r2 (both held): no stop in either, ledgers byte-identical, exit 0, silent" "$(gstops g-r ag-amb)$(gstops g-r2 ag-amb)|$(sum "$GT/runs/g-r/ledger.jsonl") $(sum "$GT/runs/g-r2/ledger.jsonl")|$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "00|$lr $lr2|0|0"
ok "...and exactly one errors.log line, naming none:ambiguous" "$(( $(errsd "$GT") - e0 ))|$(tail -1 "$GT/errors.log" | grep -c 'orch-attempts prompt: ingest: -: stop none, ledger none:ambiguous')" "1|1"
echo "-- #3.2: the same ambiguous notice in both the prompt and the transcript (live shape): still one errors.log line for the call"
TB="$GTP/tr-both.jsonl"; : > "$TB"; NB=$(notice ag-amb3 toolu_g_amb "[haiku/low] no id, two starts, prompt and transcript" 304 3 3004); genq "$TB" "$NB"
lr=$(sum "$GT/runs/g-r/ledger.jsonl"); lr2=$(sum "$GT/runs/g-r2/ledger.jsonl"); e0=$(errsd "$GT")
gprompt $G1 "$NB" "$TB"
ok "prompt + sweep both see the two-start notice: nothing written, exit 0, silent" "$(gstops g-r ag-amb3)$(gstops g-r2 ag-amb3)|$(sum "$GT/runs/g-r/ledger.jsonl") $(sum "$GT/runs/g-r2/ledger.jsonl")|$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "00|$lr $lr2|0|0"
ok "...and exactly one errors.log line for the call, naming none:ambiguous (x2)" "$(( $(errsd "$GT") - e0 ))|$(tail -1 "$GT/errors.log" | grep -c 'orch-attempts prompt: ingest: -: stop none, ledger none:ambiguous (x2)$')" "1|1"
echo "-- stop: the sweep is gated the same way"
TG="$GTP/tr-g1.jsonl"; : > "$TG"
gst g-r '"g-r#3.1"' toolu_g_link2
genq "$TG" "$(notice ag-o2 toolu_g_o2 "[haiku/low] g-o#1.1 other run, swept" 111 1 1111)"
genq "$TG" "$(notice ag-link2 toolu_g_link2 "[haiku/low] no id, swept" 222 2 2222)"
genq "$TG" "$(notice ag-amb2 toolu_g_amb "[haiku/low] no id, two starts, swept" 333 3 3333)"
so=$(gsnap g-o); lr2=$(sum "$GT/runs/g-r2/ledger.jsonl"); e0=$(errsd "$GT")
gstop $G1 "$TG"
ok "S1 stop sweep: exit 0, silent" "$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|0"
ok "sweep: the g-o notice (S2's run) leaves g-o byte-identical" "$(gsnap g-o)" "$so"
ok "sweep: the ID-less notice links in held g-r (g-r#3.1, via sweep)" "$(jq -r 'select(.type=="stop" and .task_id=="ag-link2") | "\(.attempt) \(.via)"' "$GT/runs/g-r/manifest.jsonl")" "g-r#3.1 sweep"
ok "sweep: the two-start notice writes nothing, one errors.log line" "$(gstops g-r ag-amb2)$(gstops g-r2 ag-amb2)|$(sum "$GT/runs/g-r2/ledger.jsonl")|$(( $(errsd "$GT") - e0 ))|$(tail -1 "$GT/errors.log" | grep -c 'orch-attempts stop: ingest: -: stop none, ledger none:ambiguous')" "00|$lr2|1|1"
echo "-- S1's own lease on g-p expired and nobody took it: still ingests; after S2 takes g-p, S1 writes nothing there"
expire_gp(){ jq -c --arg e $PAST '.expires=$e' "$GT/leases/g-p.json" > "$GTP/x" && mv "$GTP/x" "$GT/leases/g-p.json"; }
expire_gp
gprompt $G1 "$(notice ag-p1 toolu_g_p1 "[haiku/low] g-p#1.1 expired own" 401 4 4001)"
ok "expired own g-p, prompt: g-p#1.1 written" "$(gstops g-p ag-p1)|$(jq -r 'select(.id=="g-p#1.1") | .tokens' "$GT/runs/g-p/ledger.jsonl")" "1|401"
expire_gp; TP2="$GTP/tr-gp.jsonl"; : > "$TP2"; genq "$TP2" "$(notice ag-p2 toolu_g_p2 "[haiku/low] g-p#2.1 expired own, swept" 402 4 4002)"
gstop $G1 "$TP2"
ok "expired own g-p, stop sweep: g-p#2.1 written" "$(jq -r 'select(.type=="stop" and .task_id=="ag-p2") | .via' "$GT/runs/g-p/manifest.jsonl")|$(jq -r 'select(.id=="g-p#2.1") | .tokens' "$GT/runs/g-p/ledger.jsonl")" "sweep|402"
expire_gp; ok "S2 takes the expired g-p without --force" "$(gla g-p $G2)|$(jq -r .session "$GT/leases/g-p.json")" "0|$G2"
sp=$(gsnap g-p); : > "$TP2"; genq "$TP2" "$(notice ag-p3s toolu_g_p3s "[haiku/low] g-p#3.1 after takeover, swept" 404 4 4004)"
gprompt $G1 "$(notice ag-p3 toolu_g_p3 "[haiku/low] g-p#3.1 after takeover" 403 4 4003)"
ok "after the takeover, S1 (still holding g-r) prompt with g-p#3.1: g-p byte-identical, silent" "$(gsnap g-p)|$(wc -c < "$T/out" | tr -d ' ')" "$sp|0"
gstop $G1 "$TP2"; ok "after the takeover, S1's stop sweep with g-p#3.1: g-p byte-identical" "$(gsnap g-p)" "$sp"
ok "the gate section never wrote ACTIVE" "$(cat "$GT/ACTIVE")" "$GT/runs/g-o"

echo "== fail-open matrix: junk lease state never blocks or throws (5 modes, holder and non-holder: exit 0, no decision, empty stderr)"
HM=sess-mx-holder-4141; NM=sess-mx-other-4242; MX="$T/mx"; mkdir -p "$MX/meta" "$MX/nolib/hooks" "$MX/nolib/lib"; cp "$HOOK" "$MX/nolib/hooks/"; cp "$CL/lib/paths.js" "$MX/nolib/lib/"   # a hook copy with paths.js but no lease-store.js (nor anything else) beside it
jq -cn '{agentType:"orch-scout", description:"[haiku/low] mx-run#1.1 x", toolUseId:"toolu_mx1", spawnDepth:1, model:"haiku", effort:"low", finished:true}' > "$MX/meta/agent-amx1.meta.json"
MXP="$T/mxprobe.out"; : > "$MXP"
mxcalls(){ # mxcalls <sid> <tag> [probe]: the 5 modes; prints <rc><decision n|D><stderr bytes> per call
  local out="" m j
  for m in pretool prompt stop substop session; do
    case $m in
      pretool) j=$(pre "$1" "toolu_mx_$2" "[haiku/low] mx-run#1.1 x" "Orch-Attempt: mx-run#1.1" Explore);;
      prompt)  j=$(jq -cn --arg s "$1" --arg tp "$MX/none.jsonl" '{session_id:$s, transcript_path:$tp, prompt:"owner text"}');;
      stop)    j=$(jq -cn --arg s "$1" --arg tp "$MX/none.jsonl" '{session_id:$s, transcript_path:$tp, stop_hook_active:false}');;
      substop) j=$(jq -cn --arg s "$1" --arg tp "$MX/meta/agent-amx1.jsonl" '{session_id:$s, agent_id:"amx1", agent_type:"orch-scout", agent_transcript_path:$tp, last_assistant_message:"hi", stop_hook_active:false}');;
      session) j=$(jq -cn --arg s "$1" '{session_id:$s, hook_event_name:"SessionStart", source:"compact"}');;
    esac
    if [ -n "${3:-}" ]; then PROBE_OUT="$MXP" NODE_OPTIONS="--require $T/probe.js" hook $m "$j"; else hook $m "$j"; fi
    out="$out$(cat "$T/rc")$(nodecision | cut -c1)$(wc -c < "$T/err" | tr -d ' ') "
  done; echo "$out"; }
# mxsum [exclude ERE]: paths + contents under ORCH_DIR (errors.log aside), minus what the ERE matches
mxsum(){ (cd "$ORCH_DIR" && { find . -type f ! -name errors.log | grep -Ev "${1:-^$}" | sort | xargs -r md5sum 2>/dev/null; find . ! -name errors.log | grep -Ev "${1:-^$}" | sort; }) | md5sum; }
W5="0n0 0n0 0n0 0n0 0n0 "
# mxcase <name> <holder writes: 2 (start+stop) or 0> <holder errors.log lines> <non-holder errors.log lines> <setup>
mxcase(){
  local C="$MX/$1/orch" OD=$ORCH_DIR ORD=$ORCH_RUNS_DIR HK=$HOOK s e x="manifest\.jsonl|\.lock"
  mkdir -p "$C/runs/mx-run"; echo "$C/runs/mx-run" > "$C/ACTIVE"; export ORCH_DIR="$C" ORCH_RUNS_DIR="$C/runs"
  eval "$5"
  [ "$1" = nolib ] && HOOK="$MX/nolib/hooks/orch-attempts.js"
  [ "$1" = expired-own ] && x="$x|leases/mx-run\.json$"   # its holder may renew it (row 6)
  s=$(mxsum); e=$(errs)
  ok "matrix $1, non-holder: 5 modes exit 0, no decision, empty stderr" "$(mxcalls $NM "$1-n" probe)" "$W5"
  ok "matrix $1, non-holder: nothing written (tree, errors.log +$4)" "$(mxsum)|$(( $(errs) - e ))" "$s|$4"
  s=$(mxsum "$x"); e=$(errs)
  ok "matrix $1, holder: 5 modes exit 0, no decision, empty stderr" "$(mxcalls $HM "$1-h")" "$W5"
  ok "matrix $1, holder: writes $2 (start+stop in mx-run), nothing else (errors.log +$3)" "$(( $(mev "$C" mx-run ".key==\"toolu_mx_$1-h\"" | wc -l) + $(mev "$C" mx-run '.type=="stop" and .task_id=="amx1"' | wc -l) ))|$(mxsum "$x")|$(( $(errs) - e ))" "$2|$s|$3"
  HOOK=$HK; export ORCH_DIR="$OD" ORCH_RUNS_DIR="$ORD"
}
OK0='plf "$C" mx-run $HM $FAR'
mxcase no-leases-dir 2 0 0 'legacy "$C" mx-run $HM $FAR'
mxcase leases-is-a-file 2 0 0 'legacy "$C" mx-run $HM $FAR; echo x > "$C/leases"'
mxcase empty-file 2 0 0 "$OK0"'; : > "$C/leases/mx-junk.json"'
mxcase truncated-json 2 0 0 "$OK0"'; printf "{\"v\":1,\"run\":\"mx-junk\",\"sess" > "$C/leases/mx-junk.json"'
mxcase json-array 2 0 0 "$OK0"'; echo "[1,2]" > "$C/leases/mx-junk.json"'
mxcase name-not-run 2 0 0 "$OK0"'; jq -c --arg s $NM ".session=\$s" "$C/leases/mx-run.json" > "$C/leases/mx-junk.json"'
mxcase tmp-file 2 0 0 "$OK0"'; jq -c --arg s $NM ".session=\$s" "$C/leases/mx-run.json" > "$C/leases/mx-run.json.tmp-123"'
mxcase lock-dir 2 0 0 "$OK0"'; mkdir "$C/leases/mx-run.json.lock"'
mxcase expired-own 2 0 0 'plf "$C" mx-run $HM $PAST'
mxcase deleted-run 0 2 0 "$OK0"'; rm -rf "$C/runs/mx-run"'
mxcase fifty-expired 2 0 0 "$OK0"'; for k in $(seq -w 1 50); do plf "$C" mx-z$k sess-mx-gone-4343 $PAST; done'
mxcase chmod-000 2 0 0 "$OK0"'; plf "$C" mx-junk $NM $FAR; chmod 000 "$C/leases/mx-junk.json"'
ok "matrix chmod-000: the fixture really is unreadable" "$([ -r "$MX/chmod-000/orch/leases/mx-junk.json" ] && echo readable || echo unreadable)" "unreadable"
mxcase nolib 0 5 5 "$OK0"
echo "-- junk in the holder's own per-run file blocks the legacy fallback for that run: the holder holds nothing, writes nothing"
mxcase own-empty 0 0 0 'legacy "$C" mx-run $HM $FAR; mkdir -p "$C/leases"; : > "$C/leases/mx-run.json"'
mxcase own-truncated 0 0 0 'legacy "$C" mx-run $HM $FAR; mkdir -p "$C/leases"; printf "{\"v\":1,\"run\":\"mx-r" > "$C/leases/mx-run.json"'
mxcase own-array 0 0 0 'legacy "$C" mx-run $HM $FAR; mkdir -p "$C/leases"; echo "[]" > "$C/leases/mx-run.json"'
mxcase own-name-not-run 0 0 0 'legacy "$C" mx-run $HM $FAR; plf "$C" mx-run $HM $FAR; jq -c ".run=\"mx-other\"" "$C/leases/mx-run.json" > "$C/x" && mv "$C/x" "$C/leases/mx-run.json"'
mxcase own-chmod-000 0 0 0 'legacy "$C" mx-run $HM $FAR; plf "$C" mx-run $HM $FAR; chmod 000 "$C/leases/mx-run.json"'
ok "matrix own-chmod-000: the fixture really is unreadable" "$([ -r "$MX/own-chmod-000/orch/leases/mx-run.json" ] && echo readable || echo unreadable)" "unreadable"
ok "matrix: every non-holder call loaded lease-store.js at most (never manifest.js / ledger.js / sqlite), 90 calls" "$(jq -s -c '[.[] | select(.sqlite or ((.mods - ["lease-store.js"]) | length > 0))] | length' "$MXP")|$(wc -l < "$MXP" | tr -d ' ')" "0|90"
chmod -R u+rwX "$MX"; find "$MX" -name leases -prune -exec rm -rf {} +; find "$MX" -name ACTIVE.lease -delete   # junk lease files would stall liveleak's jq read of $T

echo "== compaction-gap hint (prompt, holder): an attempt with no notice for 2+ min (older than gapInflightSec when this session started it), or a lease expired within the hour; once per run per 10 min"
HD="$T/gh"; HO="$HD/orch"; HH1=sess-gaph-1-6161; HH2=sess-gaph-2-6262; mkdir -p "$HO/runs/h-run" "$HO/runs/h-run2"; : > "$HD/empty.jsonl"
hm(){ ORCH_DIR="$HO" ORCH_RUNS_DIR="$HO/runs" node "$MAN" "$@"; }
hprompt(){ hk "$HO" prompt "$(jq -cn --arg s "$1" --arg tp "${2:-$HD/empty.jsonl}" '{session_id:$s, transcript_path:$tp, hook_event_name:"UserPromptSubmit", prompt:"owner text"}')"; }
hage(){ local f="$HO/runs/$1/manifest.jsonl" ts; ts=$(date -u -d "-$2 min" +%Y-%m-%dT%H:%M:%S.000Z); jq -c --arg t "$ts" '.ts=$t' "$f" > "$f.x" && mv "$f.x" "$f"; }   # date every event of the run $2 min ago
hstate(){ jq -c --arg r "$1" --argjson t "$2" '.[$r]=$t' "$HO/gap-hint-state.json" > "$HD/st.x" && mv "$HD/st.x" "$HO/gap-hint-state.json"; }
hm lease acquire --run h-run --sid=$HH1 >/dev/null 2>&1
HID=$(hm launch --run h-run --row 1 --role discovery --agent orch-scout --model haiku --effort low --mode wave --repo .claude --area orchestrator --sid $HH1 | head -1)
hprompt $HH1
ok "gap: a launch 0 min old, live lease: silent, no state file" "$(wc -c < "$T/out" | tr -d ' ')|$([ -e "$HO/gap-hint-state.json" ] && echo state || echo none)" "0|none"
hage h-run 150; hprompt $HH1
ok "gap: a 150-min-old launch (past the in-flight window of this session) with no notice: one hint naming the run, the count, the id and the reconcile command" "$(outctx | grep -c "^\[orch\] possible compaction gap: run h-run has 1 attempt(s) with no recorded notice ($HID)\. Run: node \"$CL/lib/manifest.js\" reconcile --run h-run --sid=\$WFP_SID, then ListAgents\.\$")" "1"
ok "gap: exit 0, no decision, state file names the run" "$(cat "$T/rc")|$(nodecision)|$(jq -r 'keys | join(",")' "$HO/gap-hint-state.json")" "0|none|h-run"
hprompt $HH1
ok "gap: a repeat inside 10 minutes: silent" "$(wc -c < "$T/out" | tr -d ' ')" "0"
hstate h-run $(( $(date +%s) * 1000 - 11 * 60000 )); hprompt $HH1
ok "gap: 11 minutes after the last hint: the hint comes again" "$(outctx | grep -c 'possible compaction gap: run h-run has 1 attempt')" "1"
hstate h-run $(( $(date +%s) * 1000 - 11 * 60000 ))
hm event --run h-run --type stop --json "{\"attempt\":\"$HID\",\"key\":\"hk1\",\"final\":true,\"phase\":\"finished\"}" >/dev/null; hprompt $HH1
ok "no gap: the attempt has its final notice: silent" "$(wc -c < "$T/out" | tr -d ' ')" "0"
HID2=$(hm launch --run h-run --row 2 --role discovery --agent orch-scout --model haiku --effort low --mode wave --repo .claude --area orchestrator --sid $HH1 | head -1)
hm event --run h-run --type stop --json "{\"attempt\":\"$HID2\",\"key\":\"hk2\",\"final\":false,\"phase\":\"waiting\",\"status\":\"completed\"}" >/dev/null; hage h-run 150
hstate h-run $(( $(date +%s) * 1000 - 11 * 60000 )); hprompt $HH1
ok "gap: a waiting attempt (final:false notice) the owner has not closed still gets the hint" "$(outctx | grep -c "possible compaction gap: run h-run has 1 attempt(s) with no recorded notice ($HID2)")" "1"
hm event --run h-run --type outcome --json "{\"attempt\":\"$HID2\",\"outcome\":\"accepted\"}" >/dev/null
hstate h-run $(( $(date +%s) * 1000 - 11 * 60000 )); hprompt $HH1
ok "no gap: the same waiting attempt after the owner closed it (outcome event): silent (was a false alarm for 24 h)" "$(wc -c < "$T/out" | tr -d ' ')" "0"
GL="--role discovery --agent orch-scout --model haiku --effort low --mode wave --repo .claude --area orchestrator --sid $HH1"
echo "-- a retried row is not a gap: only the retry is named"
hm lease acquire --run h-run3 --sid=$HH1 >/dev/null 2>&1
RA=$(hm launch --run h-run3 --row 4 $GL | head -1)
hm event --run h-run3 --type stop --json "{\"attempt\":\"$RA\",\"key\":\"rw\",\"final\":false,\"phase\":\"waiting\",\"status\":\"completed\"}" >/dev/null
RB=$(hm launch --run h-run3 --row 4 --cause failed $GL 2>/dev/null | head -1); hage h-run3 150; rm -f "$HO/gap-hint-state.json"; hprompt $HH1
ok "no gap: row 4's first attempt (waiting, 150 min) was retried: the hint names the retry only" "$(outctx | grep -c "run h-run3 has 1 attempt(s) with no recorded notice ($RB)\.")|$(outctx | grep -c "$RA")" "1|0"
hm event --run h-run3 --type stop --json "{\"attempt\":\"$RB\",\"key\":\"rb\",\"final\":true,\"phase\":\"finished\"}" >/dev/null; rm -f "$HO/gap-hint-state.json"; hprompt $HH1
ok "no gap: the retry finished and the first attempt was superseded: silent" "$(wc -c < "$T/out" | tr -d ' ')" "0"
rm -f "$HO/leases/h-run3.json"
echo "-- an attempt this session started minutes ago is in flight; one older than the window is the gap"
hm lease acquire --run h-run4 --sid=$HH1 >/dev/null 2>&1
HID4=$(hm launch --run h-run4 --row 1 $GL | head -1); hage h-run4 10; rm -f "$HO/gap-hint-state.json"; hprompt $HH1
ok "no gap: launched by this session 10 min ago, no notice yet (still running): silent, no state file" "$(wc -c < "$T/out" | tr -d ' ')|$([ -e "$HO/gap-hint-state.json" ] && echo state || echo none)" "0|none"
hage h-run4 150; rm -f "$HO/gap-hint-state.json"; hprompt $HH1
ok "gap: the same attempt 150 min old (an orphan): the hint" "$(outctx | grep -c "possible compaction gap: run h-run4 has 1 attempt(s) with no recorded notice ($HID4)")" "1"
hm event --run h-run4 --type outcome --json "{\"attempt\":\"$HID4\",\"outcome\":\"accepted\"}" >/dev/null; rm -f "$HO/gap-hint-state.json"; hprompt $HH1
ok "no gap: that orphan closed with ledger.js close (outcome event): silent" "$(wc -c < "$T/out" | tr -d ' ')" "0"
echo "-- another session's young attempt is not this session's in-flight one; a parallel subtask is not a retry"
hm lease acquire --run h-run5 --sid=$HH1 >/dev/null 2>&1
HID5=$(hm launch --run h-run5 --row 1 $GL | head -1)
hm event --run h-run5 --type start --json "{\"attempt\":\"$HID5\",\"key\":\"os\",\"session\":\"sess-other-6363\"}" >/dev/null; hage h-run5 10; rm -f "$HO/gap-hint-state.json"; hprompt $HH1
ok "gap: an attempt started 10 min ago by another session: the hint (only this session's young attempts are in flight)" "$(outctx | grep -c "run h-run5 has 1 attempt(s) with no recorded notice ($HID5)")" "1"
rm -f "$HO/leases/h-run5.json"; hm lease acquire --run h-run6 --sid=$HH1 >/dev/null 2>&1
SA=$(hm launch --run h-run6 --row 2 --subtask a $GL | head -1)
hm event --run h-run6 --type stop --json "{\"attempt\":\"$SA\",\"key\":\"sw\",\"final\":false,\"phase\":\"waiting\",\"status\":\"completed\"}" >/dev/null
SB=$(hm launch --run h-run6 --row 2 --subtask b $GL | head -1); hage h-run6 150; rm -f "$HO/gap-hint-state.json"; hprompt $HH1
ok "gap: row 2 subtask a (150 min, waiting) and subtask b (150 min) are parallel work: both named (a later n on another subtask is no retry)" "$(outctx | grep -c "run h-run6 has 2 attempt(s) with no recorded notice ($SA, $SB)")" "1"
rm -f "$HO/leases/h-run6.json"
rm -f "$HO/leases/h-run4.json"
echo "{}" > "$HO/gap-hint-state.json"
hstate h-run $(( $(date +%s) * 1000 - 11 * 60000 )); hprompt $HH2
ok "no gap: a session holding no lease: silent" "$(wc -c < "$T/out" | tr -d ' ')" "0"
echo "-- a lease that expired within the hour (read before the prompt renews it); one that lapsed long ago stays quiet"
plf "$HO" h-run2 $HH1 "$(date -u -d '-10 min' +%Y-%m-%dT%H:%M:%S.000Z)"; hprompt $HH1
ok "lease expired 10 min ago, nothing pending: hint says an expired lease" "$(outctx | grep -c '^\[orch\] possible compaction gap: run h-run2 has an expired lease\. Run: ')" "1"
ok "that prompt renewed the lease (the hint came first)" "$(jq -r '.expires > (now | todate)' "$HO/leases/h-run2.json")" "true"
plf "$HO" h-run2 $HH1 "$PAST"; rm -f "$HO/gap-hint-state.json"; hprompt $HH1
ok "lease expired months ago, nothing pending: silent" "$(wc -c < "$T/out" | tr -d ' ')" "0"
echo "-- compaction detected: a compact_boundary record in the transcript tail"
CT="$HD/tr-compact.jsonl"; plf "$HO" h-run2 $HH1 "$(date -u -d '-10 min' +%Y-%m-%dT%H:%M:%S.000Z)"; rm -f "$HO/gap-hint-state.json"
jq -cn --arg t "$(date -u -d '-3 min' +%Y-%m-%dT%H:%M:%S.000Z)" '{type:"system",subtype:"compact_boundary",content:"Conversation compacted",timestamp:$t}' > "$CT"; hprompt $HH1 "$CT"
ok "compact_boundary 3 min ago: the hint says compaction detected" "$(outctx | grep -c 'possible compaction gap (compaction detected): run h-run2 has an expired lease')" "1"
plf "$HO" h-run2 $HH1 "$(date -u -d '-10 min' +%Y-%m-%dT%H:%M:%S.000Z)"; rm -f "$HO/gap-hint-state.json"
jq -cn --arg t "$(date -u -d '-3 hours' +%Y-%m-%dT%H:%M:%S.000Z)" '{type:"system",subtype:"compact_boundary",content:"Conversation compacted",timestamp:$t}' > "$CT"; hprompt $HH1 "$CT"
ok "compact_boundary 3 hours ago: the hint stays without the words" "$(outctx | grep -c 'possible compaction gap: run h-run2 has an expired lease')" "1"

echo "== hooks/hooks.json (install-free): wrapper, boss-node by full path, every script exists"
HJ="$CL/hooks/hooks.json"
ok "hooks.json is valid JSON with the {hooks:{...}} wrapper" "$(jq -e '.hooks | type == "object" and length > 0' "$HJ" 2>/dev/null)" "true"
lint_bad=0; lint_n=0
while IFS= read -r c; do
  lint_n=$((lint_n+1))
  case "$c" in "sh -c '"*"' \"\${CLAUDE_PLUGIN_ROOT}/bin/boss-node\" "*) c=${c#"sh -c '"*"' "};; esac   # the orchestrator-guard prefilter (H1, tested in guard-tests.sh section 9): lint the boss-node call it wraps
  if [[ "$c" =~ ^\"\$\{CLAUDE_PLUGIN_ROOT\}/bin/boss-node\"\ \"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"]+)\"(\ [a-z]+)?$ ]]; then
    [ -f "$CL/${BASH_REMATCH[1]}" ] || { lint_bad=$((lint_bad+1)); echo "  missing script: ${BASH_REMATCH[1]}"; }
  else lint_bad=$((lint_bad+1)); echo "  bad command form: $c"; fi
done < <(jq -r '.hooks[][] | .hooks[] | .command' "$HJ")
ok "every command is boss-node by full path plus an existing script (${lint_n} commands)" "$lint_bad" "0"
ok "every command is type command with a numeric timeout" "$(jq '[.hooks[][] | .hooks[] | select(.type != "command" or (.timeout | type) != "number")] | length' "$HJ")" "0"
ok "no node path pin, no \$HOME or user path in hooks.json" "$(grep -cE 'nvm|/home/|\$HOME|\.claude' "$HJ")" "0"
orch(){ jq -c '[.hooks | to_entries[] | .key as $e | .value[] | select(.hooks | map(.command) | any(contains("hooks/orch-attempts.js"))) | [$e, (.matcher // ""), (.hooks[0].command | split(" ") | last), .hooks[0].timeout]]' "$1"; }
ok "five orch-attempts entries with matchers and timeouts" "$(orch "$HJ")" '[["SessionStart","compact|resume","session",5],["SubagentStop","","substop",5],["PreToolUse","Agent|Task","pretool",5],["UserPromptSubmit","","prompt",5],["Stop","","stop",10]]'
ok "command form: the lease prefilter (sh -c) ends in boss-node plus the plugin-root hook path" "$(jq -r '.hooks.Stop[0].hooks[0].command | sub("^sh -c .* (?<t>\"[$][{]CLAUDE_PLUGIN_ROOT[}]/bin/boss-node\" )"; "SH \(.t)")' "$HJ")" 'SH "${CLAUDE_PLUGIN_ROOT}/bin/boss-node" "${CLAUDE_PLUGIN_ROOT}/hooks/orch-attempts.js" stop'
ok "orch-stop-gate.js shares the PreToolUse Agent|Task group as the second command (attempts pretool stays first)" "$(jq -c '[.hooks.PreToolUse[] | select(.hooks | map(.command) | any(contains("orch-stop-gate.js"))) | [.matcher, (.hooks | map(.command | split("/") | last))]]' "$HJ")" '[["Agent|Task",["orch-attempts.js\" pretool","orch-stop-gate.js\""]]]'
ok "guard matcher covers Read|Grep|Glob (A-02)" "$(jq -r '.hooks.PreToolUse[] | select(.hooks[0].command | contains("orchestrator-guard.js")) | .matcher' "$HJ")" "Edit|Write|MultiEdit|NotebookEdit|Bash|Read|Grep|Glob"
ok "SubagentStop: wfp group then one orch group (attempts, then return-cap)" "$(jq -c '[.hooks.SubagentStop[] | .hooks | map(.command | split("/") | last)]' "$HJ")" '[["wfp.js\" substop"],["orch-attempts.js\" substop","orch-return-cap.js\" substop"]]'
ok "wfp hooks registered for session, substart, substop, pretool, prompt" "$(jq -c '[.hooks[][] | .hooks[] | .command | select(contains("wfp/wfp.js")) | split(" ") | last] | sort' "$HJ")" '["pretool","prompt","session","substart","substop"]'
echo "== the hooks.json command strings run under bash with fake stdin (exit 0)"
while IFS= read -r c; do
  printf '%s' "$(jq -cn --arg s $S2 '{session_id:$s}')" | CLAUDE_PLUGIN_ROOT="$CL" bash -c "$c" >/dev/null 2>&1; ok "bash -c '...${c##*/}'" "$?" "0"
done < <(jq -r '.hooks[][] | .hooks[] | .command | select(contains("orch-attempts.js"))' "$HJ")

ok "live ACTIVE kept and free of this suite's paths (content), settings.json byte-identical (md5), before and after the suite" "$(livesums) active:$(activecheck)" "$LIVE0 active:ok"
ok "no live lease, run manifest or ledger line, run dir or errors.log line names a sid or run of this suite" "$(liveleak "$LIVE" "$T")" "0"
liveleak "$LIVE" "$T" >/dev/null   # a plain call, so SUITE_SIDS / SUITE_RUNS are set in this shell
ok "derived lists hold this suite's own ids (the live check is not vacuous)" "$(jq -rn --argjson s "$SUITE_SIDS" --argjson r "$SUITE_RUNS" '["sess-holder-1111","t-run","t-other"] - ($s + $r) | join(",")')" ""

echo "== orch-stop-gate.js (Boss Stop): deny an Agent call naming a stopped run; fail open on everything else"
GATE="$CL/hooks/orch-stop-gate.js"; GB=$(mktemp -d); trap 'rm -rf "$T" "$GB"' EXIT   # scratch boss root outside $T (its runs/orchestrator/ would read as a run to the live-leak check)
gk(){ BOSS_PROGRESS_DIR="$GB/boss" node "$GATE" 2>/dev/null; }   # stdin -> stdout
gin(){ jq -cn --arg d "$1" --arg p "$2" --arg a "${3:-}" '{session_id:"gate-sid-0001", hook_event_name:"PreToolUse", tool_name:"Agent", tool_input:{description:$d, prompt:$p, subagent_type:"orch-builder"}} + (if $a == "" then {} else {agent_id:$a} end)'; }
gdir(){ echo "$GB/boss/runs/orchestrator/$1/control"; }
mkdir -p "$(gdir sg-run)" "$(gdir sg-bad)"; echo '{"schema":"boss.cancel/1","requested_at":"2026-11-03T10:00:00.000Z"}' > "$(gdir sg-run)/cancel"; printf 'garbage \xff' > "$(gdir sg-bad)/cancel"
ok "stopped run, ID in the description: deny" "$(gin '[sonnet/high] sg-run#2.1 build it' 'do it' | gk | jq -r '.hookSpecificOutput | "\(.hookEventName) \(.permissionDecision)"')" "PreToolUse deny"
ok "...exit 0 and one JSON line" "$(gin 'sg-run#2.1 x' 'p' | BOSS_PROGRESS_DIR="$GB/boss" node "$GATE" >/dev/null; echo $?)|$(gin 'sg-run#2.1 x' 'p' | gk | wc -l | tr -d ' ')" "0|1"
ok "...the reason names the run and the undo route" "$(gin 'sg-run#2.1 x' 'p' | gk | jq -r '.hookSpecificOutput.permissionDecisionReason | (contains("sg-run") and contains("Undo stop") and contains("stopped by the owner"))')" "true"
ok "stopped run, only an Orch-Attempt line in the brief: deny" "$(gin 'subtask' $'Read WORKER.md\nOrch-Attempt: sg-run#4r.2\nmore' | gk | jq -r .hookSpecificOutput.permissionDecision)" "deny"
ok "a run that is not stopped: no output" "$(gin 'sg-other#1.1 x' $'Orch-Attempt: sg-other#1.1' | gk | wc -c | tr -d ' ')" "0"
ok "no attempt ID, the brief only mentions the stopped run's path: no output" "$(gin 'plain task' 'see runs/sg-run/1-x.md' | gk | wc -c | tr -d ' ')" "0"
ok "a subagent's own call (agent_id): no output" "$(gin 'sg-run#2.1 x' 'p' agent-7 | gk | wc -c | tr -d ' ')" "0"
ok "a malformed cancel file still denies" "$(gin 'sg-bad#1.1 x' 'p' | gk | jq -r .hookSpecificOutput.permissionDecision)" "deny"
mkdir -p "$(gdir sg-dir)/cancel"
ok "a directory named cancel still denies (presence is the contract)" "$(gin 'sg-dir#1.1 x' 'p' | gk | jq -r .hookSpecificOutput.permissionDecision)" "deny"
chmod 000 "$(gdir sg-run)"
ok "unreadable control dir: fails open (no output, exit 0)" "$(gin 'sg-run#2.1 x' 'p' | BOSS_PROGRESS_DIR="$GB/boss" node "$GATE" | wc -c | tr -d ' ')|$(gin 'sg-run#2.1 x' 'p' | BOSS_PROGRESS_DIR="$GB/boss" node "$GATE" >/dev/null; echo $?)" "0|0"
chmod 755 "$(gdir sg-run)"
ok "input that is not JSON / empty / a bare array: no output, exit 0" "$(for x in 'not json' '' '[]' 'null'; do printf '%s' "$x" | BOSS_PROGRESS_DIR="$GB/boss" node "$GATE"; echo -n "$?"; done)" "0000"
ok "tool_input missing or odd: no output" "$(for x in '{}' '{"tool_input":null}' '{"tool_input":"x"}' '{"tool_input":{"description":7,"prompt":{}}}'; do printf '%s' "$x" | gk; done | wc -c | tr -d ' ')" "0"
ok "a run id that tries to escape (..#1.1, a/b#1.1): no output" "$(gin '../sg-run#1.1 x' $'Orch-Attempt: ../x#1.1' | gk | wc -c | tr -d ' ')" "0"
GSNAP=$(find "$GB" | sort | md5sum)
gin 'sg-run#2.1 x' 'p' | gk >/dev/null; ok "the hook writes nothing (tree unchanged)" "$(find "$GB" | sort | md5sum)" "$GSNAP"
mv "$(gdir sg-run)/cancel" "$(gdir sg-run)/cancel.undone-1"
ok "Undo (cancel moved away): the same call passes" "$(gin 'sg-run#2.1 x' 'p' | gk | wc -c | tr -d ' ')" "0"
mkdir -p "$GB/home/.claude/boss/runs/orchestrator/sg-home/control"; echo '{}' > "$GB/home/.claude/boss/runs/orchestrator/sg-home/control/cancel"
ok "scratch ORCH_DIR without BOSS_PROGRESS_DIR: skipped (no output)" "$(gin 'sg-home#1.1 x' 'p' | HOME="$GB/home" node "$GATE" | wc -c | tr -d ' ')" "0"
ok "no scratch env, no BOSS_PROGRESS_DIR: the default ~/.claude/boss root is read (deny)" "$(gin 'sg-home#1.1 x' 'p' | env -u ORCH_DIR -u ORCH_RUNS_DIR -u BOSS_PROGRESS_DIR HOME="$GB/home" node "$GATE" | jq -r .hookSpecificOutput.permissionDecision)" "deny"
ok "the hook loads no manifest.js, ledger or sqlite (regex, fs and the read-only lease-store only)" "$(grep -cE "require\(.*(manifest|ledger|sqlite)" "$GATE")" "0"
# ID-less calls: the calling session's only lease decides (gap 1 of the 0.4.1 review). Scratch ORCH_DIR holds the leases.
gkl(){ ORCH_DIR="$GB/orch" BOSS_PROGRESS_DIR="$GB/boss" node "$GATE" 2>/dev/null; }
gsin(){ jq -cn --arg s "$1" --arg d "$2" --arg p "${3:-plain brief}" '{session_id:$s, hook_event_name:"PreToolUse", tool_name:"Agent", tool_input:{description:$d, prompt:$p, subagent_type:"orch-builder"}}'; }
plf "$GB/orch" sg-run gate-owner-1 $FAR; plf "$GB/orch" sg-live gate-owner-2 $FAR; plf "$GB/orch" sg-two-a gate-owner-3 $FAR; plf "$GB/orch" sg-two-b gate-owner-3 $FAR
mkdir -p "$(gdir sg-two-a)"; echo '{}' > "$(gdir sg-two-a)/cancel"; mkdir -p "$(gdir sg-live)" ; plf "$GB/orch" sg-exp gate-owner-4 $PAST; mkdir -p "$(gdir sg-exp)"; echo '{}' > "$(gdir sg-exp)/cancel"
echo '{}' > "$(gdir sg-run)/cancel"
ok "no ID, the session's only lease is a stopped run: deny (names the run)" "$(gsin gate-owner-1 'build a thing' | gkl | jq -r '.hookSpecificOutput | "\(.permissionDecision) \(.permissionDecisionReason | contains("sg-run"))"')" "deny true"
ok "no ID, only lease is a run that is not stopped: no output" "$(gsin gate-owner-2 'build a thing' | gkl | wc -c | tr -d ' ')" "0"
ok "no ID, the session holds two leases (one stopped): no output (cannot tell which run)" "$(gsin gate-owner-3 'build a thing' | gkl | wc -c | tr -d ' ')" "0"
ok "no ID, an expired lease still its own, stopped run: deny (same chain as orch-attempts D1)" "$(gsin gate-owner-4 'build a thing' | gkl | jq -r .hookSpecificOutput.permissionDecision)" "deny"
ok "no ID, a session that holds no lease: no output" "$(gsin gate-nobody 'build a thing' | gkl | wc -c | tr -d ' ')" "0"
ok "an ID for another, unstopped run beats the lease (the ID ends the chain): no output" "$(gsin gate-owner-1 'sg-live#1.1 x' $'Orch-Attempt: sg-live#1.1' | gkl | wc -c | tr -d ' ')" "0"
ok "subagent call (agent_id) with a stopped lease: no output" "$(gsin gate-owner-1 'build a thing' | jq -c '. + {agent_id:"a-1"}' | gkl | wc -c | tr -d ' ')" "0"
ok "no session_id / unsafe session_id: no output, exit 0" "$(for x in '{"tool_input":{"description":"x"}}' '{"session_id":"../x","tool_input":{"description":"x"}}'; do printf '%s' "$x" | gkl; echo -n "$?"; done)" "00"
ok "lease-store missing: fails open (no output)" "$(mkdir -p "$GB/nolib/hooks" "$GB/nolib/lib"; cp "$GATE" "$GB/nolib/hooks/"; cp "$CL/lib/paths.js" "$GB/nolib/lib/"; gsin gate-owner-1 'build a thing' | ORCH_DIR="$GB/orch" BOSS_PROGRESS_DIR="$GB/boss" node "$GB/nolib/hooks/orch-stop-gate.js" | wc -c | tr -d ' ')" "0"
ok "scratch ORCH_DIR without BOSS_PROGRESS_DIR still skips the lease path" "$(gsin gate-owner-1 'build a thing' | ORCH_DIR="$GB/orch" node "$GATE" | wc -c | tr -d ' ')" "0"
mv "$(gdir sg-run)/cancel" "$(gdir sg-run)/cancel.undone-2"
ok "Undo (marker gone): the ID-less call passes" "$(gsin gate-owner-1 'build a thing' | gkl | wc -c | tr -d ' ')" "0"

echo "== orch-attempts pretool: a call the stop gate denies leaves no start event (Boss Stop leftover A)"
IDS=$(launch 10 haiku low); TUS=toolu_stopped_10; mkdir -p "$(gdir $R)"; echo '{"schema":"boss.cancel/1"}' > "$(gdir $R)/cancel"
nS=$(wc -l < "$MFILE")
printf '%s' "$(pre $S1 $TUS "[haiku/low] $IDS stopped" "Orch-Attempt: $IDS")" | BOSS_PROGRESS_DIR="$GB/boss" node "$HOOK" pretool > "$T/out" 2> "$T/err"; echo $? > "$T/rc"
ok "stopped run + ID in the description: no start written, exit 0, silent" "$(evn ".key==\"$TUS\"")|$(wc -l < "$MFILE" | tr -d ' ')|$(cat "$T/rc")|$(wc -c < "$T/out" | tr -d ' ')" "0|$nS|0|0"
ok "...one errors.log line says why" "$(grep -c "$TUS not written: the stop gate denies" "$ORCH_DIR/errors.log")" "1"
printf '%s' "$(pre $S1 toolu_stopped_brief "plain subtask" "Orch-Attempt: $IDS")" | BOSS_PROGRESS_DIR="$GB/boss" node "$HOOK" pretool > /dev/null 2>&1
ok "stopped run, ID only in the brief line (the gate denies that too): no start" "$(evn '.key=="toolu_stopped_brief"')" "0"
printf '%s' "$(pre $S1 $TUS "[haiku/low] $IDS stopped" "Orch-Attempt: $IDS")" | node "$HOOK" pretool > /dev/null 2>&1
ok "scratch env without BOSS_PROGRESS_DIR (the gate is skipped there): the start is written as before" "$(evn ".key==\"$TUS\"")" "1"
rm -f "$(gdir $R)/cancel"; printf '%s' "$(pre $S1 toolu_after_undo "[haiku/low] $IDS again" "Orch-Attempt: $IDS")" | BOSS_PROGRESS_DIR="$GB/boss" node "$HOOK" pretool > /dev/null 2>&1
ok "Undo (marker gone): the start is written" "$(evn '.key=="toolu_after_undo"')" "1"

echo "== hooks.json lease prefilter (Stop, SubagentStop attempts): no lease for this sid = node never starts; a holder still gets the full hook"
# The real command strings from hooks.json run under /bin/sh with a stub BOSS_NODE: `-v` passes boss-node's version gate, every other call
# logs a line and then runs the real node. Data dir: a scratch ORCH_DIR (the env the suites use) plus, last, the owner's layout without it.
PFD="$T/pf"; mkdir -p "$PFD/orch/runs/pf-run" "$PFD/orch/leases" "$PFD/bin"
REALNODE=$(command -v node); PFLAG="$PFD/flag"
printf '#!/bin/sh\n[ "$1" = "-v" ] && { echo v99.0.0; exit 0; }\necho call >> "%s"\nexec "%s" "$@"\n' "$PFLAG" "$REALNODE" > "$PFD/bin/stubnode"; chmod +x "$PFD/bin/stubnode"
pcmd(){ jq -r --arg e "$1" --arg m "$2" '.hooks[$e][] | .hooks[] | .command | select(contains("orch-attempts.js\" " + $m))' "$HJ"; }
PF_STOP=$(pcmd Stop stop); PF_SUBSTOP=$(pcmd SubagentStop substop)
ok "both commands are the sh -c lease prefilter" "$(printf '%s\n%s\n' "$PF_STOP" "$PF_SUBSTOP" | grep -c "^sh -c .*boss-skip\" lease ")" "2"
pfrun(){ # pfrun <command> <stdin file> [env assignments...]; sets PF_RC PF_OUT PF_CALLS
  local c=$1 in=$2; shift 2; rm -f "$PFLAG"
  PF_OUT=$(env CLAUDE_PLUGIN_ROOT="${PF_ROOT:-$CL}" BOSS_NODE="$PFD/bin/stubnode" ORCH_DIR="$PFD/orch" ORCH_RUNS_DIR="$PFD/orch/runs" "$@" /bin/sh -c "$c" < "$in" 2>"$PFD/err"); PF_RC=$?
  PF_CALLS=$([ -f "$PFLAG" ] && wc -l < "$PFLAG" | tr -d ' ' || echo 0)
}
pfl(){ jq -cn --arg r "$1" --arg s "$2" --arg e "$3" --arg rn "$4" --arg path "$PFD/orch/runs/$1" '{v:1,run:$r,path:$path,session:$s,acquired:$rn,renewed:$rn,expires:$e,ttl_sec:7200}' > "$PFD/orch/leases/$1.json"; }
PAST_TS=$(date -u -d '-2 hours' +%Y-%m-%dT%H:%M:%S.000Z); SOON_TS=$(date -u -d '+10 minutes' +%Y-%m-%dT%H:%M:%S.000Z); LONGAGO_TS=$(date -u -d '-3 hours' +%Y-%m-%dT%H:%M:%S.000Z)
SPH=sess-pf-holder-1111; SPO=sess-pf-other-2222; SPN=sess-pf-none-3333
pfin(){ jq -cn --arg s "$1" '{session_id:$s,hook_event_name:"Stop",stop_hook_active:false,transcript_path:"/nonexistent/t.jsonl"}' > "$PFD/in.json"; }
for who in "Stop:$PF_STOP" "SubagentStop:$PF_SUBSTOP"; do
  nm=${who%%:*}; cm=${who#*:}
  pfin $SPN; rm -rf "$PFD/orch/leases"; pfrun "$cm" "$PFD/in.json"
  ok "$nm: no leases dir: fail open, node starts, exit 0, silent" "$PF_CALLS|$PF_RC|${#PF_OUT}|$(wc -c < "$PFD/err" | tr -d ' ')" "1|0|0|0"
  mkdir -p "$PFD/orch/leases"; pfrun "$cm" "$PFD/in.json"
  ok "$nm: empty, readable leases dir: node never starts" "$PF_CALLS|$PF_RC" "0|0"
  if [ "$(id -u)" != 0 ]; then chmod 000 "$PFD/orch/leases"; pfrun "$cm" "$PFD/in.json"; chmod 755 "$PFD/orch/leases"
    ok "$nm: unreadable leases dir: fail open, node starts, exit 0" "$PF_CALLS|$PF_RC" "1|0"; fi
  mv "$PFD/orch/leases" "$PFD/orch/leases.gone"; pfrun "$cm" "$PFD/in.json"; mv "$PFD/orch/leases.gone" "$PFD/orch/leases"
  ok "$nm: leases dir missing but the data dir exists: node starts (fail open)" "$PF_CALLS|$PF_RC" "1|0"
  pfl pf-run $SPO "$SOON_TS" "$PAST_TS"; pfrun "$cm" "$PFD/in.json"
  ok "$nm: another session's live lease: node never starts" "$PF_CALLS|$PF_RC|${#PF_OUT}" "0|0|0"
  pfl pf-run $SPO "$PAST_TS" "$LONGAGO_TS"; pfrun "$cm" "$PFD/in.json"
  ok "$nm: another session's expired lease: node never starts" "$PF_CALLS|$PF_RC" "0|0"
  pfin $SPH; pfl pf-run $SPH "$SOON_TS" "$LONGAGO_TS"; pfrun "$cm" "$PFD/in.json"
  ok "$nm: this session's live lease: node starts once, exit 0" "$PF_CALLS|$PF_RC" "1|0"
  pfl pf-run $SPH "$PAST_TS" "$LONGAGO_TS"; pfrun "$cm" "$PFD/in.json"
  ok "$nm: this session's own EXPIRED lease (heldBy still returns it): node starts" "$PF_CALLS|$PF_RC" "1|0"
  rm -f "$PFD/orch/leases/pf-run.json"; pfl pf-run $SPO "$SOON_TS" "$PAST_TS"
  jq -cn --arg s $SPH --arg p "$PFD/orch/runs/pf-legacy" '{v:1,run:"pf-legacy",path:$p,session:$s,acquired:"2026-01-01T01:00:00.000Z",renewed:"2026-01-01T01:00:00.000Z",expires:"2026-01-01T03:00:00.000Z",ttl_sec:7200}' > "$PFD/orch/ACTIVE.lease"
  pfrun "$cm" "$PFD/in.json"
  ok "$nm: the legacy ACTIVE.lease names this session: node starts" "$PF_CALLS|$PF_RC" "1|0"
  pfin $SPN; pfrun "$cm" "$PFD/in.json"
  ok "$nm: the legacy ACTIVE.lease names another session: node never starts" "$PF_CALLS|$PF_RC" "0|0"
  rm -f "$PFD/orch/ACTIVE.lease"
  pfin $SPH; pfl pf-run $SPH "$SOON_TS" "$LONGAGO_TS"; jq -n --arg s $SPH '{session_id:$s,hook_event_name:"Stop",stop_hook_active:false}' > "$PFD/in.json"
  pfrun "$cm" "$PFD/in.json"
  ok "$nm: pretty-printed input (no compact session_id key): node starts" "$PF_CALLS|$PF_RC" "1|0"
  : > "$PFD/in.json"; pfrun "$cm" "$PFD/in.json"
  ok "$nm: empty stdin: node starts and fails open" "$PF_CALLS|$PF_RC|${#PF_OUT}" "1|0|0"
  printf 'not json' > "$PFD/in.json"; pfrun "$cm" "$PFD/in.json"
  ok "$nm: malformed stdin: node starts, exit 0, silent" "$PF_CALLS|$PF_RC|${#PF_OUT}" "1|0|0"
  pfl pf-run $SPH "$SOON_TS" "$LONGAGO_TS"; jq -cn --arg s "x\"session_id\":\"$SPN" '{tool_input:$s,session_id:"sess-pf-holder-1111"}' > "$PFD/in.json"
  pfrun "$cm" "$PFD/in.json"
  ok "$nm: a session_id key inside quoted text never mis-skips (escaped quotes), the real sid wins: node starts" "$PF_CALLS|$PF_RC" "1|0"
done
# the holder's full hook really runs through the prefilter: Stop renews a lease with under an hour left (writes the lease file)
pfin $SPH; pfl pf-run $SPH "$SOON_TS" "$LONGAGO_TS"; before=$(jq -r .renewed "$PFD/orch/leases/pf-run.json")
pfrun "$PF_STOP" "$PFD/in.json"; after=$(jq -r .renewed "$PFD/orch/leases/pf-run.json")
ok "Stop for a lease holder through the prefilter renews the lease (the full hook ran)" "$PF_CALLS|$([ "$before" != "$after" ] && echo renewed || echo unchanged)" "1|renewed"
pfl pf-run $SPO "$SOON_TS" "$LONGAGO_TS"; pfin $SPN; before=$(jq -r .renewed "$PFD/orch/leases/pf-run.json"); pfrun "$PF_STOP" "$PFD/in.json"
ok "Stop for a session without a lease leaves another session's lease byte for byte" "$PF_CALLS|$(jq -r .renewed "$PFD/orch/leases/pf-run.json")" "0|$before"
# the data dir resolves the way lib/paths.js does without ORCH_DIR: BOSS_DATA, then <config dir>/orchestrator (with runs/), then <config dir>/boss-data
pfin $SPN
PFH="$PFD/home"; mkdir -p "$PFH/.claude/orchestrator/runs" "$PFH/.claude/orchestrator/leases"
pfl2(){ jq -cn --arg s "$1" --arg p "$2/runs/pf-run" --arg e "$SOON_TS" --arg rn "$LONGAGO_TS" '{v:1,run:"pf-run",path:$p,session:$s,acquired:$rn,renewed:$rn,expires:$e,ttl_sec:7200}' > "$2/leases/pf-run.json"; }
pfrun2(){ local c=$1; shift; rm -f "$PFLAG"; PF_OUT=$(env -u ORCH_DIR -u BOSS_DATA -u CLAUDE_CONFIG_DIR -u ORCH_RUNS_DIR CLAUDE_PLUGIN_ROOT="$CL" BOSS_NODE="$PFD/bin/stubnode" HOME="$PFH" "$@" /bin/sh -c "$c" < "$PFD/in.json" 2>/dev/null); PF_RC=$?; PF_CALLS=$([ -f "$PFLAG" ] && wc -l < "$PFLAG" | tr -d ' ' || echo 0); }
pfl2 $SPN "$PFH/.claude/orchestrator"; pfrun2 "$PF_STOP"
ok "owner layout (no ORCH_DIR/BOSS_DATA): <HOME>/.claude/orchestrator holds this sid: node starts" "$PF_CALLS|$PF_RC" "1|0"
pfl2 $SPO "$PFH/.claude/orchestrator"; pfrun2 "$PF_STOP"
ok "owner layout: only another session's lease: node never starts" "$PF_CALLS|$PF_RC" "0|0"
mkdir -p "$PFD/bd/leases"; pfl2 $SPN "$PFD/bd"; pfrun2 "$PF_STOP" BOSS_DATA="$PFD/bd"
ok "BOSS_DATA wins over the owner layout: its lease names this sid, node starts" "$PF_CALLS|$PF_RC" "1|0"
rm -rf "$PFH/.claude/orchestrator"; mkdir -p "$PFH/.claude/boss-data/leases"; pfl2 $SPN "$PFH/.claude/boss-data"; pfrun2 "$PF_STOP"
ok "fresh install layout: <HOME>/.claude/boss-data/leases holds this sid: node starts" "$PF_CALLS|$PF_RC" "1|0"
pfl2 $SPO "$PFH/.claude/boss-data"; pfrun2 "$PF_STOP"
ok "fresh install layout: only another session's lease: node never starts" "$PF_CALLS|$PF_RC" "0|0"
pfrun2 "$PF_STOP" CLAUDE_CONFIG_DIR="$PFD/cfg"
ok "CLAUDE_CONFIG_DIR is the config dir (no data or leases dir there): fail open, node starts" "$PF_CALLS|$PF_RC" "1|0"
mkdir -p "$PFD/cfg/boss-data/leases"; pfrun2 "$PF_STOP" CLAUDE_CONFIG_DIR="$PFD/cfg"
ok "CLAUDE_CONFIG_DIR with an empty readable leases dir: node never starts" "$PF_CALLS|$PF_RC" "0|0"
rm -rf "$PFD/cfg"; pfrun2 "$PF_STOP" BOSS_DATA="$PFD/nodata"
ok "BOSS_DATA pointing at a missing dir: fail open, node starts" "$PF_CALLS|$PF_RC" "1|0"
# fail open: a helper that is missing, or unreadable, runs node and never makes the hook exit 2
PF_ROOT="$PFD/root-nohelper"; mkdir -p "$PF_ROOT/bin"; cp "$CL/bin/boss-node" "$PF_ROOT/bin/"; for d in hooks lib wfp; do ln -s "$CL/$d" "$PF_ROOT/$d"; done
pfrun "$PF_STOP" "$PFD/in.json"
ok "boss-skip missing: node starts (fail open), exit 0, not 2" "$PF_CALLS|$PF_RC" "1|0"
printf 'if then fi ((( ;;\n' > "$PF_ROOT/bin/boss-skip"; pfrun "$PF_STOP" "$PFD/in.json"
ok "boss-skip with a syntax error: node starts (fail open), exit 0, not 2" "$PF_CALLS|$PF_RC" "1|0"
printf '#!/bin/sh\nexit 2\n' > "$PF_ROOT/bin/boss-skip"; pfrun "$PF_STOP" "$PFD/in.json"
ok "boss-skip exiting 2: node starts, the hook exits 0, not 2" "$PF_CALLS|$PF_RC" "1|0"
PF_ROOT=

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

echo "pass=$pass fail=$fail"
[ "$fail" = 0 ]
