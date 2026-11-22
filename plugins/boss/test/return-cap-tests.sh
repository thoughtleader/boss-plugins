#!/usr/bin/env bash
# orch-return-cap.js + its hooks.json entry (orchestrator v2 fixes, row 16; D4 design section 6).
# Fake SubagentStop / PreToolUse SubagentHandback JSON on scratch ORCH_DIR / ORCH_RUNS_DIR; never the live files.
# Cases: under the cap, exactly at the cap, over it (byteLength, multibyte), a non-orch type; flag mode (flagged, once per agent);
# block-once (block JSON + blocked, second stop by stop_hook_active and by the recorded event passes with passed-after-block);
# attempt mapping (ID run, start in the lease run, unmapped -> lease run); the lease rule (a non-holder session writes, prints and
# logs nothing; no lease: nothing); fail-open (bad JSON, non-object, unknown mode, bad config values, lock busy never blocks,
# missing run folder); handback mode (deny once, flag, other tool); hooks.json (shared SubagentStop group); live files untouched.
# Usage: bash test/return-cap-tests.sh   (exit 0 = all pass)
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib/compat.sh"   # on macOS: GNU date/stat/touch/md5sum/wc shims (test/lib)
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"   # the checkout this suite tests (a worktree runs its own code)
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
HOOK="$CL/hooks/orch-return-cap.js"; MAN="$CL/lib/manifest.js"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export ORCH_DIR="$T/orch" ORCH_RUNS_DIR="$T/orch/runs" ORCH_PROJECTS_DIR="$T/projects" ORCH_METRICS_DB="$T/m.db"
unset WFP_SID NODE_OPTIONS BOSS_PROGRESS_DIR
LIVE="$HOME/.claude/orchestrator"; sum(){ if [ -e "$1" ]; then md5sum < "$1"; else echo absent; fi; }
livesums(){ for f in "$LIVE/config.json" "$HOME/.claude/settings.json"; do echo "$f $(sum "$f")"; done; }   # not peer-written: md5
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
R=t-run; O=t-other; RD="$ORCH_RUNS_DIR/$R"; MFILE="$RD/manifest.jsonl"
S1=sess-holder-1111; S2=sess-other-2222
P="$ORCH_PROJECTS_DIR/p"; TR="$P/$S1.jsonl"; SUB="$P/$S1/subagents"
mkdir -p "$RD" "$ORCH_RUNS_DIR/$O" "$SUB"; : > "$TR"
echo "$ORCH_RUNS_DIR/$O" > "$ORCH_DIR/ACTIVE"             # ACTIVE names another run than the lease (the live situation)
cfg(){ printf '{"returnCapBytes":%s,"returnCapMode":"%s","returnCapAgentTypes":"%s","lockWaitMsHook":200}\n' "${2:-100}" "$1" "${3:-^orch-}" > "$ORCH_DIR/config.json"; }
errs(){ if [ -f "$ORCH_DIR/errors.log" ]; then wc -l < "$ORCH_DIR/errors.log" | tr -d ' '; else echo 0; fi; }
# lf <run>: the file holding that run's lease: leases/<run>.json when it exists, else the legacy ACTIVE.lease (so a manifest.js
# that writes per-run files needs no edit here)
lf(){ if [ -e "$ORCH_DIR/leases/$1.json" ]; then echo "$ORCH_DIR/leases/$1.json"; else echo "$ORCH_DIR/ACTIVE.lease"; fi; }
treesum(){ (cd "$ORCH_DIR" && find . -type f ! -name errors.log -print0 | sort -z | xargs -0 md5sum 2>/dev/null; find . | sort) | md5sum; }
# hook <mode> <json>: stdout to $T/out, exit code to $T/rc
hook(){ printf '%s' "$2" | node "$HOOK" "$1" > "$T/out" 2> "$T/err"; echo $? > "$T/rc"; }
outlen(){ wc -c < "$T/out" | tr -d ' '; }
ev(){ jq -c "select($1)" "$MFILE" 2>/dev/null; }
evn(){ ev "$1" | wc -l | tr -d ' '; }
caps(){ ev ".type==\"return_over_cap\" and .task_id==\"$1\"" | jq -r '.action' | paste -sd, -; }
mlines(){ if [ -f "$MFILE" ]; then wc -l < "$MFILE" | tr -d ' '; else echo 0; fi; }
str(){ head -c "$1" /dev/zero | tr '\0' "${2:-x}"; }
meta(){ jq -cn --arg d "$2" --arg tu "$3" '{agentType:"orch-builder", description:$d, toolUseId:$tu, spawnDepth:1, model:"opus", effort:"high"}' > "$SUB/agent-$1.meta.json"; }
# SJ <sid> <agent_id> <agent_type> <message> [stop_hook_active]
SJ(){ jq -cn --arg s "$1" --arg a "$2" --arg at "$3" --arg msg "$4" --arg tp "$SUB/agent-$2.jsonl" --arg mt "$TR" --argjson sha "${5:-false}" \
  '{session_id:$s, transcript_path:$mt, hook_event_name:"SubagentStop", agent_id:$a, agent_type:$at, agent_transcript_path:$tp, last_assistant_message:$msg, stop_hook_active:$sha}'; }
# HB <sid> <agent_id> <agent_type> <message> [transcript_path]: PreToolUse SubagentHandback inside the subagent
HB(){ jq -cn --arg s "$1" --arg a "$2" --arg at "$3" --arg msg "$4" --arg tp "${5:-$SUB/agent-$2.jsonl}" \
  '{session_id:$s, transcript_path:$tp, hook_event_name:"PreToolUse", tool_name:"SubagentHandback", tool_use_id:"toolu_hb", agent_id:$a, agent_type:$at, tool_input:{message:$msg}}'; }
decision(){ if [ ! -s "$T/out" ]; then echo none; else jq -r '.decision // .hookSpecificOutput.permissionDecision // "other"' "$T/out"; fi; }

echo "== setup: lease for $R held by $S1, ACTIVE names $O, launches for rows 1-3"
node "$MAN" lease acquire --run $R --sid $S1 >/dev/null 2>&1; ok "lease acquired by the holder" "$(jq -r '.session + " " + .run' "$(lf $R)")" "$S1 $R"
launch(){ node "$MAN" launch --run $R --row "$1" --role implementation --agent orch-builder --model opus --effort high --mode subagent --repo .claude --area orchestrator --sid $S1 | head -1; }
ID1=$(launch 1); ID2=$(launch 2); ID3=$(launch 3)
ok "attempt IDs allocated" "$ID1 $ID2 $ID3" "$R#1.1 $R#2.1 $R#3.1"
meta aflag1 "[opus/high] $ID1 Row 1 builder" toolu_1
meta ablock1 "[opus/high] $ID2 Row 2 builder" toolu_2
meta ablock2 "[opus/high] $ID3 Row 3 builder" toolu_3

echo "== hygiene and syntax"
ok "hook passes node --check" "$(node --check "$HOOK" >/dev/null 2>&1; echo $?)" "0"
ok "no sqlite / metrics store in the hook" "$(grep -c 'sqlite\|metrics' "$HOOK")" "0"

echo "== fast path (flag mode, cap 100)"
cfg flag 100
m0=$(mlines); hook substop "$(SJ $S1 aflag1 orch-builder "$(str 50)")"
ok "under the cap: exit 0, silent, nothing written" "$(cat "$T/rc")|$(outlen)|$(mlines)" "0|0|$m0"
hook substop "$(SJ $S1 aflag1 orch-builder "$(str 100)")"
ok "exactly at the cap passes" "$(cat "$T/rc")|$(outlen)|$(mlines)" "0|0|$m0"
hook substop "$(SJ $S1 aflag1 orch-builder "$(printf 'é%.0s' $(seq 1 60))")"   # 60 chars, 120 bytes
ok "multibyte: 60 chars but 120 bytes is over (byteLength)" "$(caps aflag1)|$(ev '.task_id=="aflag1"' | jq -r '[.bytes, .cap] | map(tostring) | join(" ")')" "flagged|120 100"
m0=$(mlines); hook substop "$(SJ $S1 aother1 general-purpose "$(str 5000)")"
ok "non-orch agent type over the cap: nothing" "$(cat "$T/rc")|$(outlen)|$(mlines)" "0|0|$m0"
hook substop "$(SJ $S1 aother2 Explore "$(str 5000)")"
ok "Explore over the cap: nothing" "$(outlen)|$(mlines)" "0|$m0"
hook substop "$(SJ $S1 aprefix1 boss:orch-builder "$(str 500)")"
ok "plugin-prefixed type boss:orch-builder over the cap: flagged, event records orch-builder (agentBase)" "$(caps aprefix1)|$(ev '.task_id=="aprefix1"' | jq -r .agent_type)" "flagged|orch-builder"
hook substop "$(SJ $S1 aprefix2 boss:Explore "$(str 5000)")"
ok "boss:Explore (not orch-*) over the cap: nothing" "$(caps aprefix2)" ""

echo "== flag mode"
hook substop "$(SJ $S1 aflag1 orch-builder "$(str 500)")"
ok "flag: second over-cap stop of the same agent dedupes (one flagged)" "$(cat "$T/rc")|$(outlen)|$(caps aflag1)" "0|0|flagged"
ok "flag event fields" "$(ev '.task_id=="aflag1"' | jq -r '[.attempt, .row, .via, .session, .agent_type, .action, .mode, .cap_mode, .tool_use_id, .key] | map(tostring) | join(" ")')" "$ID1 1 cap $S1 orch-builder flagged subagent-stop flag toolu_1 aflag1:flagged"
hook substop "$(SJ $S1 aflag1 orch-builder "$(str 500)" true)"
ok "flag never blocks, even with stop_hook_active" "$(decision)|$(caps aflag1)" "none|flagged"
ok "manifest.js show folds the cap (over-cap:flagged)" "$(node "$MAN" show --run $R --json | jq -r --arg id "$ID1" '.attempts[] | select(.id==$id) | .flags | map(select(startswith("over-cap"))) | join(",")')" "over-cap:flagged"

echo "== block-once mode"
cfg block-once 100
hook substop "$(SJ $S1 ablock1 orch-builder "$(str 300)")"
ok "first over-cap stop: block decision" "$(cat "$T/rc")|$(decision)" "0|block"
ok "block reason names bytes, cap, result file, 5-line summary" "$(jq -r '.reason' "$T/out")" "[orch] Your final message is 300 bytes; the cap is 100. Move the detail into your result file and end with the WORKER.md summary (at most 5 lines)."
ok "blocked event recorded" "$(caps ablock1)|$(ev '.task_id=="ablock1"' | jq -r '[.attempt, .cap_mode, .mode] | join(" ")')" "blocked|$ID2 block-once subagent-stop"
hook substop "$(SJ $S1 ablock1 orch-builder "$(str 250)" true)"
ok "second stop (stop_hook_active): passes, passed-after-block" "$(cat "$T/rc")|$(decision)|$(caps ablock1)" "0|none|blocked,passed-after-block"
hook substop "$(SJ $S1 ablock2 orch-builder "$(str 300)")"; d1=$(decision)
hook substop "$(SJ $S1 ablock2 orch-builder "$(str 300)")"
ok "second stop with stop_hook_active false but a blocked event recorded: passes" "$d1|$(decision)|$(caps ablock2)" "block|none|blocked,passed-after-block"
hook substop "$(SJ $S1 ablock2 orch-builder "$(str 300)")"
ok "third stop: still passes, no extra events" "$(decision)|$(caps ablock2)" "none|blocked,passed-after-block"
hook substop "$(SJ $S1 ablock3 orch-scout "$(str 40)")"
ok "block-once under the cap: nothing" "$(decision)|$(caps ablock3)" "none|"
hook substop "$(SJ $S1 ablock4 orch-scout "$(str 300)" true)"
ok "first stop already stop_hook_active (another hook blocked): pass, no block" "$(decision)|$(caps ablock4)" "none|passed-after-block"

echo "== attempt mapping"
meta astart1 "[opus/high] Row x unplanned" toolu_s1
node "$MAN" event --run $R --type start --json "$(jq -cn --arg s $S1 '{tool_use_id:"toolu_s1", attempt:null, via:"pretool", session:$s}')" >/dev/null
cfg flag 100
hook substop "$(SJ $S1 astart1 orch-builder "$(str 200)")"
ok "no ID in meta, start found by toolUseId: lease run, attempt null" "$(ev '.task_id=="astart1"' | jq -r '[.action, .attempt, .tool_use_id] | map(tostring) | join(" ")')" "flagged null toolu_s1"
hook substop "$(SJ $S1 anometa1 orch-builder "$(str 200)")"
ok "no meta.json: recorded in the lease's run, attempt null" "$(ev '.task_id=="anometa1"' | jq -r '[.action, .attempt, .run] | map(tostring) | join(" ")')" "flagged null $R"
ok "ACTIVE's run never written" "$([ -e "$ORCH_RUNS_DIR/$O/manifest.jsonl" ] && echo written || echo none)" "none"
meta amiss1 "[opus/high] zz-missing#4.1 Row 4" toolu_m1
e0=$(errs); hook substop "$(SJ $S1 amiss1 orch-builder "$(str 200)")"
ok "ID for a run the session does not hold (D1: the ID ends the chain): no folder made, not moved to the lease's run, nothing logged" "$([ -e "$ORCH_RUNS_DIR/zz-missing" ] && echo made || echo none)|$(caps amiss1)|$(( $(errs) - e0 ))" "none||0"
meta aderive1 "[opus/high] $ID3 Row 3" toolu_d
hook substop "$(SJ $S1 aderive1 orch-scout "$(str 200)" | jq -c 'del(.agent_transcript_path)')"
ok "no agent_transcript_path: meta.json derived from transcript_path + session_id" "$(ev '.task_id=="aderive1"' | jq -r '[.action, .attempt] | join(" ")')" "flagged $ID3"

echo "== the lease rule"
cfg block-once 100
s0=$(treesum); e0=$(errs)
hook substop "$(SJ $S2 a00other1 orch-builder "$(str 5000)")"
ok "non-holder session, over the cap, block-once: exit 0, silent, nothing written or logged" "$(cat "$T/rc")|$(outlen)|$(treesum)|$(( $(errs) - e0 ))" "0|0|$s0|0"
hook handback "$(HB $S2 a00other2 orch-builder "$(str 5000)")"
ok "non-holder handback: silent, nothing written" "$(outlen)|$(treesum)" "0|$s0"
LEASE="$(lf $R)"; mv "$LEASE" "$T/lease.keep"; s0=$(treesum)
hook substop "$(SJ $S1 anolease1 orch-builder "$(str 5000)")"
ok "no lease: silent, nothing written" "$(outlen)|$(treesum)|$(( $(errs) - e0 ))" "0|$s0|0"
mv "$T/lease.keep" "$LEASE"
jq -c '.expires = "2020-01-01T00:00:00.000Z"' "$LEASE" > "$T/l2" && cp "$LEASE" "$T/lease.keep" && cp "$T/l2" "$LEASE"
hook substop "$(SJ $S1 aexp1 orch-builder "$(str 300)")"
ok "own expired lease still counts (block)" "$(decision)|$(caps aexp1)" "block|blocked"
cp "$T/lease.keep" "$LEASE"

echo "== fail-open"
for m in substop handback; do
  m0=$(mlines); e0=$(errs); hook $m '{not json'
  ok "bad JSON ($m): exit 0, silent, logged" "$(cat "$T/rc")|$(outlen)|$(mlines)|$(( $(errs) - e0 ))" "0|0|$m0|1"
  e0=$(errs); hook $m '[1,2]'; ok "non-object JSON ($m): exit 0, silent, logged" "$(cat "$T/rc")|$(outlen)|$(( $(errs) - e0 ))" "0|0|1"
  hook $m '{}'; ok "empty object ($m): exit 0, silent" "$(cat "$T/rc")|$(outlen)" "0|0"
done
e0=$(errs); hook bogus '{}'; ok "unknown mode: exit 0, logged" "$(cat "$T/rc")|$(( $(errs) - e0 ))" "0|1"
printf '' | node "$HOOK" substop > "$T/out"; ok "empty stdin: exit 0, silent" "$?|$(outlen)" "0|0"
hook substop "$(SJ $S1 'bad id/..' orch-builder "$(str 300)")"; ok "unsafe agent_id: nothing" "$(cat "$T/rc")|$(outlen)" "0|0"
mkdir "$MFILE.lock"; m0=$(mlines); e0=$(errs)
hook substop "$(SJ $S1 alock1 orch-builder "$(str 300)")"
ok "manifest lock busy: no block (once can't be recorded), exit 0, logged" "$(cat "$T/rc")|$(decision)|$(mlines)|$(grep -c 'lock busy' "$ORCH_DIR/errors.log")" "0|none|$m0|1"
rmdir "$MFILE.lock"
printf '{"returnCapBytes":"lots","returnCapMode":"slam","returnCapAgentTypes":"(["}\n' > "$ORCH_DIR/config.json"; e0=$(errs)
hook substop "$(SJ $S1 abadcfg1 orch-builder "$(str 3000)")"
ok "bad config values: defaults (2048, ^orch-, flag), logged" "$(cat "$T/rc")|$(decision)|$(caps abadcfg1)|$(ev '.task_id=="abadcfg1"' | jq -r '.cap')|$(( $(errs) - e0 ))" "0|none|flagged|2048|3"
echo '{broken' > "$ORCH_DIR/config.json"
hook substop "$(SJ $S1 abadcfg2 orch-builder "$(str 3000)")"
ok "unparsable config.json: defaults, exit 0" "$(cat "$T/rc")|$(caps abadcfg2)" "0|flagged"
cfg block-once 100
mv "$RD" "$T/rd.keep"; e0=$(errs)
hook substop "$(SJ $S1 anorun1 orch-builder "$(str 300)")"
ok "lease run folder missing: no block, no folder made, logged" "$(cat "$T/rc")|$(decision)|$([ -e "$RD" ] && echo made || echo none)|$(( $(errs) - e0 ))" "0|none|none|1"
mv "$T/rd.keep" "$RD"
head -c 300000 /dev/zero | tr '\0' 'x' | fold -w 99 >> "$ORCH_DIR/errors.log"; hook bogus '{}'
ok "errors.log capped (dropped the older half)" "$([ "$(wc -c < "$ORCH_DIR/errors.log")" -le 262144 ] && echo capped || echo big)|$(tail -1 "$ORCH_DIR/errors.log" | grep -c "orch-return-cap bogus: unknown mode")" "capped|1"

echo "== handback mode (PreToolUse SubagentHandback; fake JSON only)"
meta ahb1 "[opus/high] $ID1 Row 1 builder" toolu_1
cfg block-once 100
hook handback "$(HB $S1 ahb1 orch-builder "$(str 400)")"
ok "handback over the cap: deny once" "$(cat "$T/rc")|$(decision)|$(jq -r '.hookSpecificOutput.hookEventName' "$T/out")" "0|deny|PreToolUse"
ok "deny reason" "$(jq -r '.hookSpecificOutput.permissionDecisionReason' "$T/out")" "[orch] Your handback message is 400 bytes; the cap is 100. Move the detail into your result file and hand back the WORKER.md summary (at most 5 lines)."
ok "handback event: via handback, mode handback, attempt from meta" "$(ev '.task_id=="ahb1"' | jq -r '[.action, .via, .mode, .attempt] | join(" ")')" "blocked handback handback $ID1"
hook handback "$(HB $S1 ahb1 orch-builder "$(str 400)")"
ok "second handback: allowed, passed-after-block" "$(decision)|$(caps ahb1)" "none|blocked,passed-after-block"
hook substop "$(SJ $S1 ahb1 orch-builder "$(str 400)")"
ok "SubagentStop after a handback block: no second block (once per agent)" "$(decision)|$(caps ahb1)" "none|blocked,passed-after-block"
meta ahb2 "[opus/high] $ID3 Row 3 builder" toolu_3
hook handback "$(HB $S1 ahb2 orch-builder "$(str 400)" "$SUB/agent-ahb2.jsonl" | jq -c 'del(.agent_transcript_path)')"
ok "handback: meta.json from transcript_path agent-<id>.jsonl (unverified live)" "$(ev '.task_id=="ahb2"' | jq -r '.attempt')" "$ID3"
meta ahb3 "[opus/high] $ID2 Row 2 builder" toolu_2
hook handback "$(HB $S1 ahb3 orch-builder "$(str 400)" "$TR")"
ok "handback: transcript_path is the main one, meta.json derived from session" "$(ev '.task_id=="ahb3"' | jq -r '.attempt')" "$ID2"
hook handback "$(HB $S1 ahb4 orch-builder "$(str 400)" | jq -c '.tool_name = "Bash"')"
ok "another tool name: nothing" "$(decision)|$(caps ahb4)" "none|"
hook handback "$(HB $S1 ahb5 orch-builder "$(str 50)")"; ok "handback under the cap: nothing" "$(decision)|$(caps ahb5)" "none|"
cfg flag 100
hook handback "$(HB $S1 ahb6 orch-builder "$(str 400)")"
ok "handback flag mode: no deny, flagged" "$(decision)|$(caps ahb6)" "none|flagged"

echo "== timing"
cfg flag 2048
t0=$(date +%s%N); for k in 1 2 3 4 5; do hook substop "$(SJ $S1 afast$k orch-builder "short")"; done; t1=$(date +%s%N)
ms=$(( (t1 - t0) / 1000000 )); echo "  5 under-cap calls: ${ms} ms"
ok "fast path: 5 under-cap calls in under 2 s" "$([ $ms -lt 2000 ] && echo fast || echo slow)" "fast"

echo "== D1 (per-run leases): which run a cap event goes to; this tip's manifest.js writes only ACTIVE.lease, so leases/<run>.json are hand-written"
FAR=2099-01-01T00:00:00.000Z; PAST=2026-01-01T03:04:00.000Z
# plf <orch dir> <run> <sid> <expires> [renewed]: hand-write a per-run lease file (D-1 shape)
plf(){ mkdir -p "$1/leases"; jq -cn --arg r "$2" --arg s "$3" --arg e "$4" --arg n "${5:-2026-10-21T08:00:00.000Z}" --arg p "$1/runs/$2" '{v:1, run:$r, path:$p, session:$s, acquired:$n, renewed:$n, expires:$e, ttl_sec:7200}' > "$1/leases/$2.json"; }
# legacy <orch dir> <run> <sid> <expires>: hand-write the legacy global ACTIVE.lease (live shape)
legacy(){ jq -cn --arg r "$2" --arg s "$3" --arg e "$4" --arg p "$1/runs/$2" '{v:1, run:$r, path:$p, session:$s, acquired:"2026-10-21T07:00:00.000Z", renewed:"2026-10-21T07:00:00.000Z", expires:$e, ttl_sec:7200}' > "$1/ACTIVE.lease"; }
errsd(){ if [ -f "$1/errors.log" ]; then wc -l < "$1/errors.log" | tr -d ' '; else echo 0; fi; }
mev(){ jq -c "select($3)" "$1/runs/$2/manifest.jsonl" 2>/dev/null; }                       # mev <dir> <run> <jq filter>
mcaps(){ mev "$1" "$2" ".type==\"return_over_cap\" and .task_id==\"$3\"" | jq -r '.action' | paste -sd, -; }   # mcaps <dir> <run> <agent>
mtotal(){ cat "$1"/runs/*/manifest.jsonl 2>/dev/null | wc -l | tr -d ' '; }
DS1=sess-d1-holder-5151; DS2=sess-d1-other-5252; DR=t-run; DR2=t-run2
OD0=$ORCH_DIR; ORD0=$ORCH_RUNS_DIR
D="$T/d1/orch"; mkdir -p "$D/runs/$DR" "$D/runs/$DR2" "$D/runs/$O"; echo "$D/runs/$O" > "$D/ACTIVE"
export ORCH_DIR="$D" ORCH_RUNS_DIR="$D/runs"
plf "$D" $DR $DS1 $FAR; plf "$D" $DR2 $DS1 $FAR; plf "$D" $O $DS2 $FAR
cfg flag 100
startev(){ node "$MAN" event --run "$1" --type start --json "$(jq -cn --arg tu "$2" --arg a "${3:-}" --arg s $DS1 '{tool_use_id:$tu, attempt:(if $a=="" then null else $a end), via:"pretool", session:$s}')" >/dev/null; }
echo "-- no ID: the held run whose manifest has a start with the agent's toolUseId"
meta ad1a "[opus/high] Row x unplanned" toolu_d1a; startev $DR2 toolu_d1a "$DR2#7.1"
e0=$(errsd "$D"); hook substop "$(SJ $DS1 ad1a orch-builder "$(str 300)")"
ok "two leases held, start in t-run2 only: flagged in t-run2 with the start's attempt, nothing in t-run" "$(mcaps "$D" $DR2 ad1a)|$(mev "$D" $DR2 '.task_id=="ad1a"' | jq -r '[.attempt, .row, .tool_use_id] | map(tostring) | join(" ")')|$(mev "$D" $DR '.task_id=="ad1a"' | wc -l | tr -d ' ')|$(( $(errsd "$D") - e0 ))" "flagged|$DR2#7.1 7 toolu_d1a|0|0"
meta ad1a2 "[opus/high] Row x unplanned" toolu_d1a
hook handback "$(HB $DS1 ad1a2 orch-builder "$(str 300)")"
ok "same for handback" "$(mcaps "$D" $DR2 ad1a2)|$(mev "$D" $DR '.task_id=="ad1a2"' | wc -l | tr -d ' ')" "flagged|0"
meta ad1b "[opus/high] Row x unplanned" toolu_d1b; startev $DR toolu_d1b; startev $DR2 toolu_d1b
m0=$(mtotal "$D"); e0=$(errsd "$D"); hook substop "$(SJ $DS1 ad1b orch-builder "$(str 300)")"
ok "start in both held runs: nothing written, one log line naming both runs" "$(( $(mtotal "$D") - m0 ))|$(( $(errsd "$D") - e0 ))|$(tail -1 "$D/errors.log" | grep -c 'start toolu_d1b is in 2 held runs (t-run, t-run2)')|$(cat "$T/rc")|$(outlen)" "0|1|1|0|0"
meta ad1c "[opus/high] Row x unplanned" toolu_d1c
m0=$(mtotal "$D"); e0=$(errsd "$D"); hook substop "$(SJ $DS1 ad1c orch-builder "$(str 300)")"
ok "no ID, no start anywhere, two leases: nothing written, one log line" "$(( $(mtotal "$D") - m0 ))|$(( $(errsd "$D") - e0 ))|$(tail -1 "$D/errors.log" | grep -c 'holds 2 leases (t-run, t-run2)')|$(cat "$T/rc")|$(outlen)" "0|1|1|0|0"
m0=$(mtotal "$D"); e0=$(errsd "$D"); hook substop "$(SJ $DS1 ad1d orch-builder "$(str 300)")"
ok "no meta.json, two leases: nothing written, one log line" "$(( $(mtotal "$D") - m0 ))|$(( $(errsd "$D") - e0 ))" "0|1"
echo "-- an attempt ID ends the chain"
meta ad1e "[opus/high] $DR#1.1 Row 1" toolu_d1b
m2=$(mev "$D" $DR2 '.type=="return_over_cap"' | wc -l | tr -d ' '); e0=$(errsd "$D"); hook substop "$(SJ $DS1 ad1e orch-builder "$(str 300)")"
ok "ID for held t-run wins over a start in the other held run: t-run, with the ID's attempt, nothing new in t-run2" "$(mcaps "$D" $DR ad1e)|$(mev "$D" $DR '.task_id=="ad1e"' | jq -r '.attempt')|$(mev "$D" $DR2 '.type=="return_over_cap"' | wc -l | tr -d ' ')|$(( $(errsd "$D") - e0 ))" "flagged|$DR#1.1|$m2|0"
meta ad1f "[opus/high] $O#1.1 Row 1" toolu_d1f
m0=$(mtotal "$D"); e0=$(errsd "$D"); hook substop "$(SJ $DS1 ad1f orch-builder "$(str 300)")"
ok "ID for t-other, held live by another session, while this one holds two other runs: nothing written anywhere, nothing logged" "$(( $(mtotal "$D") - m0 ))|$(( $(errsd "$D") - e0 ))|$(cat "$T/rc")|$(outlen)" "0|0|0|0"
hook handback "$(HB $DS1 ad1f orch-builder "$(str 300)")"
ok "same for handback" "$(( $(mtotal "$D") - m0 ))|$(( $(errsd "$D") - e0 ))|$(outlen)" "0|0|0"
meta ad1g "[opus/high] $O#1.1 Row 1" toolu_d1g
cfg block-once 100; m0=$(mtotal "$D"); hook substop "$(SJ $DS1 ad1g orch-builder "$(str 300)")"
ok "block-once: an ID for a run this session does not hold never blocks, nothing written" "$(decision)|$(( $(mtotal "$D") - m0 ))" "none|0"
cfg flag 100
echo "-- block-once with several leases"
cfg block-once 100
meta ad1h "[opus/high] Row x unplanned" toolu_d1h; startev $DR2 toolu_d1h
hook substop "$(SJ $DS1 ad1h orch-builder "$(str 300)")"; d1=$(decision)
hook substop "$(SJ $DS1 ad1h orch-builder "$(str 300)")"
ok "start in t-run2: blocks once, then passes; both events in t-run2, none in t-run" "$d1|$(decision)|$(mcaps "$D" $DR2 ad1h)|$(mev "$D" $DR '.task_id=="ad1h"' | wc -l | tr -d ' ')" "block|none|blocked,passed-after-block|0"
meta ad1i "[opus/high] Row x unplanned" toolu_d1i
m0=$(mtotal "$D"); hook substop "$(SJ $DS1 ad1i orch-builder "$(str 300)")"
ok "block-once, nothing maps the agent to one of two leases: no record, no block" "$(decision)|$(( $(mtotal "$D") - m0 ))" "none|0"
cfg flag 100
echo "-- expired own lease still counts; one lease falls back to it"
plf "$D" $DR2 $DS1 $PAST; meta ad1j "[opus/high] Row x unplanned" toolu_d1j; startev $DR2 toolu_d1j
hook substop "$(SJ $DS1 ad1j orch-builder "$(str 300)")"
ok "expired own lease in t-run2 (held, not taken): start found there, flagged" "$(mcaps "$D" $DR2 ad1j)" "flagged"
plf "$D" $DR2 $DS2 $FAR; meta ad1k "[opus/high] Row x unplanned" toolu_d1k; startev $DR2 toolu_d1k
m0=$(mtotal "$D"); e0=$(errsd "$D"); hook substop "$(SJ $DS1 ad1k orch-builder "$(str 300)")"
ok "t-run2 now held live by another session: its start does not pull this session in; the only lease (t-run) takes the event" "$(mcaps "$D" $DR2 ad1k)|$(mcaps "$D" $DR ad1k)|$(( $(errsd "$D") - e0 ))" "|flagged|0"
meta ad1l "[opus/high] $DR2#1.1 Row 1" toolu_d1l; m0=$(mtotal "$D"); e0=$(errsd "$D"); hook substop "$(SJ $DS1 ad1l orch-builder "$(str 300)")"
ok "one lease (t-run): an ID for the other, unheld run t-run2 writes nothing and logs nothing" "$(( $(mtotal "$D") - m0 ))|$(( $(errsd "$D") - e0 ))" "0|0"
hook substop "$(SJ $DS1 ad1m orch-builder "$(str 300)")"
ok "one lease, no meta: recorded in that run, attempt null" "$(mcaps "$D" $DR ad1m)|$(mev "$D" $DR '.task_id=="ad1m"' | jq -r '.attempt')" "flagged|null"
hook substop "$(SJ $DS2 ad1n orch-builder "$(str 300)")"
ok "the other session holds t-run2 and t-other: no ID, no start, two leases: nothing" "$(mcaps "$D" $DR2 ad1n)$(mcaps "$D" $O ad1n)" ""
meta ad1o "[opus/high] $O#1.1 Row 1" toolu_d1o
hook substop "$(SJ $DS2 ad1o orch-builder "$(str 300)")"
ok "the other session's own ID for t-other: written there" "$(mcaps "$D" $O ad1o)" "flagged"

echo "-- legacy ACTIVE.lease (the live shape): S16 holds t-run through it, S35 holds nothing"
L16=sess-legacy-16-aaaa; L35=sess-legacy-35-bbbb
LG="$T/lg/orch"; mkdir -p "$LG/runs/$DR" "$LG/runs/$O"; echo "$LG/runs/$O" > "$LG/ACTIVE"; legacy "$LG" $DR $L16 $FAR
export ORCH_DIR="$LG" ORCH_RUNS_DIR="$LG/runs"; cfg flag 100; lsum=$(md5sum < "$LG/ACTIVE.lease")
meta alg1 "[opus/high] $DR#1.1 Row 1" toolu_lg1; meta alg2 "[opus/high] Row x unplanned" toolu_lg2; meta alg3 "[opus/high] $O#1.1 Row 1" toolu_lg3
hook substop "$(SJ $L16 alg1 orch-builder "$(str 300)")"; hook handback "$(HB $L16 alg2 orch-builder "$(str 300)")"
ok "S16 (legacy holder): its ID and its ID-less agent both land in t-run" "$(mcaps "$LG" $DR alg1)|$(mev "$LG" $DR '.task_id=="alg1"' | jq -r '.attempt')|$(mcaps "$LG" $DR alg2)" "flagged|$DR#1.1|flagged"
hook substop "$(SJ $L16 alg3 orch-builder "$(str 300)")"
ok "S16: an ID for t-other, which it does not hold: nothing" "$(mcaps "$LG" $O alg3)$(mcaps "$LG" $DR alg3)" ""
m0=$(mtotal "$LG"); s0=$(treesum); e0=$(errsd "$LG")
for a in alg1 alg2 alg3; do hook substop "$(SJ $L35 $a orch-builder "$(str 5000)")"; hook handback "$(HB $L35 $a orch-builder "$(str 5000)")"; done
ok "S35 (no lease): 6 over-cap calls write nothing, print nothing, log nothing; the legacy file is byte-identical" "$(( $(mtotal "$LG") - m0 ))|$(outlen)|$(treesum)|$(( $(errsd "$LG") - e0 ))|$(md5sum < "$LG/ACTIVE.lease")" "0|0|$s0|0|$lsum"
plf "$LG" $DR $L35 $FAR; meta alg4 "[opus/high] $DR#1.1 Row 1" toolu_lg4
hook substop "$(SJ $L16 alg4 orch-builder "$(str 300)")"; hook substop "$(SJ $L35 alg4 orch-builder "$(str 300)")"
ok "a per-run file for t-run beats the legacy lease: S16 stops writing, S35 (the per-run holder) writes" "$(mev "$LG" $DR '.task_id=="alg4"' | jq -r '.session')" "$L35"
export ORCH_DIR="$OD0" ORCH_RUNS_DIR="$ORD0"

echo "== fail-open matrix: junk lease state never blocks or throws (substop and handback; under the cap, over it in flag mode, over it in block-once)"
HM=sess-mx-holder-4141; NM=sess-mx-other-4242; MX="$T/mx"; mkdir -p "$MX/nolib/hooks" "$MX/nolib/lib"; cp "$HOOK" "$MX/nolib/hooks/"; cp "$CL/lib/paths.js" "$CL/lib/defaults.json" "$MX/nolib/lib/"   # a hook copy with paths.js and defaults.json but no lease-store.js (nor anything else) beside it
cat > "$T/probe.js" <<'EOP'
process.on('exit', () => { try { require('fs').appendFileSync(process.env.PROBE_OUT, JSON.stringify({
  sqlite: process.moduleLoadList.some(m => /sqlite/.test(m)),
  mods: Object.keys(require.cache).filter(k => /\/lib\/(?!paths\.js)[a-z-]+\.js$/.test(k)).map(k => require('path').basename(k)).sort() }) + '\n'); } catch {} });
EOP
MXP="$T/mxprobe.out"; : > "$MXP"
# mxone <mode> <sid> <agent> <bytes> [stop_hook_active]: one call, prints <rc>/<decision>/<stderr bytes>
mxone(){
  local j; if [ "$1" = substop ]; then j=$(SJ "$2" "$3" orch-builder "$(str "$4")" "${5:-false}"); else j=$(HB "$2" "$3" orch-builder "$(str "$4")"); fi
  if [ -n "${MXPROBE:-}" ]; then PROBE_OUT="$MXP" NODE_OPTIONS="--require $T/probe.js" hook "$1" "$j"; else hook "$1" "$j"; fi
  printf '%s/%s/%s ' "$(cat "$T/rc")" "$(decision)" "$(wc -c < "$T/err" | tr -d ' ')"; }
# mxcalls <tag> <sid>: 2 under-cap calls, 2 over-cap calls in flag mode, 2 over-cap calls in block-once (the second of one agent)
mxcalls(){
  local t=$1 s=$2 a out=""
  for a in u1 u2 f1 f2 b1; do meta "amx$t$a" "[opus/high] mx-run#1.1 x" "toolu_mx_$t$a"; done
  cfg flag 100; out="$(mxone substop $s amx${t}u1 50)$(mxone handback $s amx${t}u2 50)$(mxone substop $s amx${t}f1 300)$(mxone handback $s amx${t}f2 300)"
  cfg block-once 100; out="$out$(mxone substop $s amx${t}b1 300)$(mxone substop $s amx${t}b1 300)"
  echo "$out"; }
# mxsum [exclude ERE]: paths + contents under ORCH_DIR (errors.log aside), minus what the ERE matches
mxsum(){ (cd "$ORCH_DIR" && { find . -type f ! -name errors.log | grep -Ev "${1:-^$}" | sort | xargs -r md5sum 2>/dev/null; find . ! -name errors.log | grep -Ev "${1:-^$}" | sort; }) | md5sum; }
NONE6="0/none/0 0/none/0 0/none/0 0/none/0 0/none/0 0/none/0 "; HOLD6="0/none/0 0/none/0 0/none/0 0/none/0 0/block/0 0/none/0 "
# mxcase <name> <holder holds: 1|0> <holder errors.log lines> <non-holder errors.log lines> <setup>
mxcase(){
  local C="$MX/$1/orch" OD=$ORCH_DIR ORD=$ORCH_RUNS_DIR HK=$HOOK s e x="manifest\.jsonl|\.lock|config\.json" wantp wantr acts
  mkdir -p "$C/runs/mx-run"; echo "$C/runs/mx-run" > "$C/ACTIVE"; export ORCH_DIR="$C" ORCH_RUNS_DIR="$C/runs"
  eval "$5"
  [ "$1" = nolib ] && HOOK="$MX/nolib/hooks/orch-return-cap.js"
  [ "$1" = expired-own ] && x="$x|leases/mx-run\.json$"   # its holder may renew it (row 6)
  if [ "$2" = 1 ]; then wantp=$HOLD6; wantr=4; acts="flagged,flagged,blocked,passed-after-block"; else wantp=$NONE6; wantr=0; acts=""; fi
  cfg flag 100; s=$(mxsum "config\.json"); e=$(errs)
  MXPROBE=1 ok "matrix $1, non-holder: 6 calls exit 0, no decision, empty stderr" "$(MXPROBE=1 mxcalls "$1n" $NM)" "$NONE6"
  ok "matrix $1, non-holder: nothing written (tree, errors.log +$4)" "$(mxsum "config\.json")|$(( $(errs) - e ))" "$s|$4"
  s=$(mxsum "$x"); e=$(errs)
  ok "matrix $1, holder: 6 calls exit 0, stderr empty; ${acts:-no decision at all}" "$(mxcalls "$1h" $HM)" "$wantp"
  ok "matrix $1, holder: $wantr cap events in mx-run ($acts), nothing else (errors.log +$3)" "$(mev "$C" mx-run '.type=="return_over_cap"' | jq -r '.action' | paste -sd, -)|$(mxsum "$x")|$(( $(errs) - e ))" "$acts|$s|$3"
  HOOK=$HK; export ORCH_DIR="$OD" ORCH_RUNS_DIR="$ORD"
}
OK0='plf "$C" mx-run $HM $FAR'
mxcase no-leases-dir 1 0 0 'legacy "$C" mx-run $HM $FAR'
mxcase leases-is-a-file 1 0 0 'legacy "$C" mx-run $HM $FAR; echo x > "$C/leases"'
mxcase empty-file 1 0 0 "$OK0"'; : > "$C/leases/mx-junk.json"'
mxcase truncated-json 1 0 0 "$OK0"'; printf "{\"v\":1,\"run\":\"mx-junk\",\"sess" > "$C/leases/mx-junk.json"'
mxcase json-array 1 0 0 "$OK0"'; echo "[1,2]" > "$C/leases/mx-junk.json"'
mxcase name-not-run 1 0 0 "$OK0"'; jq -c --arg s $NM ".session=\$s" "$C/leases/mx-run.json" > "$C/leases/mx-junk.json"'
mxcase tmp-file 1 0 0 "$OK0"'; jq -c --arg s $NM ".session=\$s" "$C/leases/mx-run.json" > "$C/leases/mx-run.json.tmp-123"'
mxcase lock-dir 1 0 0 "$OK0"'; mkdir "$C/leases/mx-run.json.lock"'
mxcase expired-own 1 0 0 'plf "$C" mx-run $HM $PAST'
mxcase fifty-expired 1 0 0 "$OK0"'; for k in $(seq -w 1 50); do plf "$C" mx-z$k sess-mx-gone-4343 $PAST; done'
mxcase chmod-000 1 0 0 "$OK0"'; plf "$C" mx-junk $NM $FAR; chmod 000 "$C/leases/mx-junk.json"'
ok "matrix chmod-000: the fixture really is unreadable" "$([ -r "$MX/chmod-000/orch/leases/mx-junk.json" ] && echo readable || echo unreadable)" "unreadable"
mxcase deleted-run 0 4 0 "$OK0"'; rm -rf "$C/runs/mx-run"'
mxcase nolib 0 4 4 "$OK0"
echo "-- junk in the holder's own per-run file blocks the legacy fallback for that run: the holder holds nothing, writes nothing, logs nothing"
mxcase own-empty 0 0 0 'legacy "$C" mx-run $HM $FAR; mkdir -p "$C/leases"; : > "$C/leases/mx-run.json"'
mxcase own-truncated 0 0 0 'legacy "$C" mx-run $HM $FAR; mkdir -p "$C/leases"; printf "{\"v\":1,\"run\":\"mx-r" > "$C/leases/mx-run.json"'
mxcase own-array 0 0 0 'legacy "$C" mx-run $HM $FAR; mkdir -p "$C/leases"; echo "[]" > "$C/leases/mx-run.json"'
mxcase own-name-not-run 0 0 0 'legacy "$C" mx-run $HM $FAR; plf "$C" mx-run $HM $FAR; jq -c ".run=\"mx-other\"" "$C/leases/mx-run.json" > "$C/x" && mv "$C/x" "$C/leases/mx-run.json"'
mxcase own-chmod-000 0 0 0 'legacy "$C" mx-run $HM $FAR; plf "$C" mx-run $HM $FAR; chmod 000 "$C/leases/mx-run.json"'
ok "matrix own-chmod-000: the fixture really is unreadable" "$([ -r "$MX/own-chmod-000/orch/leases/mx-run.json" ] && echo readable || echo unreadable)" "unreadable"
ok "matrix: no non-holder call (108 of them) loaded manifest.js / ledger.js or sqlite; the over-cap ones loaded lease-store.js; nothing loaded for the 36 under-cap calls and the 4 nolib calls" "$(jq -s -c '[.[] | select(.sqlite or ((.mods - ["lease-store.js"]) | length > 0))] | length' "$MXP")|$(wc -l < "$MXP" | tr -d ' ')|$(jq -s -c '[.[] | select(.mods == [])] | length' "$MXP")" "0|108|40"
chmod -R u+rwX "$MX"; find "$MX" -name leases -prune -exec rm -rf {} +; find "$MX" -name ACTIVE.lease -delete   # junk lease files would stall liveleak's jq read of $T

echo "== hooks/hooks.json (install-free): the return-cap entry"
HJ="$CL/hooks/hooks.json"
ok "hooks.json is valid JSON with the {hooks:{...}} wrapper" "$(jq -e '.hooks | type == "object" and length > 0' "$HJ" 2>/dev/null)" "true"
ok "SubagentStop: one orch group, orch-attempts then orch-return-cap, 5 s each" "$(jq -c '[.hooks.SubagentStop[] | select(.hooks | map(.command) | any(test("hooks/orch-"))) | [.matcher // "", (.hooks | map((.command | split("/") | last) + " " + (.timeout | tostring)))]]' "$HJ")" '[["",["orch-attempts.js\" substop 5","orch-return-cap.js\" substop 5"]]]'
ok "return-cap command form: boss-node plus the plugin-root hook path" "$(jq -r '.hooks.SubagentStop[] | .hooks[] | .command | select(contains("orch-return-cap"))' "$HJ")" '"${CLAUDE_PLUGIN_ROOT}/bin/boss-node" "${CLAUDE_PLUGIN_ROOT}/hooks/orch-return-cap.js" substop'
ok "exactly one return-cap command in all hooks" "$(jq '[.hooks[][] | .hooks[] | select(.command | contains("orch-return-cap"))] | length' "$HJ")" "1"
ok "no SubagentHandback entry registered" "$(jq '[(.hooks.PreToolUse // [])[] | select(.matcher == "SubagentHandback")] | length' "$HJ")" "0"
ok "no node path pin in hooks.json" "$(grep -cE 'nvm|/home/|\$HOME' "$HJ")" "0"
echo "== the hooks.json return-cap command runs under bash with fake stdin (exit 0)"
c=$(jq -r '.hooks.SubagentStop[] | .hooks[] | .command | select(contains("orch-return-cap"))' "$HJ")
printf '%s' "$(SJ $S2 abash1 orch-builder "$(str 5000)")" | CLAUDE_PLUGIN_ROOT="$CL" bash -c "$c" > "$T/out" 2>&1; ok "bash -c '...orch-return-cap.js\" substop' (non-holder): exit 0, silent" "$?|$(outlen)" "0|0"

ok "live ACTIVE kept and free of this suite's paths (content), config.json and settings.json byte-identical (md5), before and after the suite" "$(livesums) active:$(activecheck)" "$LIVE0 active:ok"
ok "no live lease, run manifest or ledger line, run dir or errors.log line names a sid or run of this suite" "$(liveleak "$LIVE" "$T")" "0"
liveleak "$LIVE" "$T" >/dev/null   # a plain call, so SUITE_SIDS / SUITE_RUNS are set in this shell
ok "derived lists hold this suite's own ids (the live check is not vacuous)" "$(jq -rn --argjson s "$SUITE_SIDS" --argjson r "$SUITE_RUNS" '["sess-holder-1111","t-run","t-other"] - ($s + $r) | join(",")')" ""

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
