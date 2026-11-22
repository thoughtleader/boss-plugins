#!/usr/bin/env bash
# Row 4 tests (orchestrator v2): wfp fixtures, ledger round trip, orchestrator-guard exemption. Re-runnable; cleans up.
# Usage: bash test/presets-tests.sh     (exit 0 = all pass)
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib/compat.sh"   # on macOS: GNU date/stat/touch/md5sum/wc shims (test/lib)
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"   # the checkout this suite tests (a worktree runs its own code)
# A scratch environment ALWAYS (a direct run included; under scripts/test-all.sh it nests inside the suite's own scratch): HOME, the
# config dir and the data dir are throwaway folders, so the ledger round trip below and the guard probes never touch the real
# ~/.claude or the real runs folder, whatever the caller exported. ORCH_DIR, ORCH_RUNS_DIR and BOSS_PROGRESS_DIR stay unset: the guard and
# ledger DEFAULTS are under test (they resolve to <data dir>/runs, which is scratch).
SCR=$(mktemp -d "${TMPDIR:-/tmp}/boss-presets-env.XXXXXX") || exit 1
SCR=$(cd "$SCR" && pwd -P)
mkdir -p "$SCR/home/.claude" "$SCR/data"
unset ORCH_DIR ORCH_RUNS_DIR BOSS_PROGRESS_DIR CLAUDE_CONFIG_DIR WFP_STATE_DIR
export HOME="$SCR/home" BOSS_DATA="$SCR/data" TMPDIR="$SCR"
trap 'rm -rf "$SCR"' EXIT
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }

echo "== 1. wfp fixtures (this tree's wfp.js + config.default.json, scratch data dir, defaultProfile=null)"
fx=$(WFP_SRC="$CL" bash "$CL/wfp/fixtures/run.sh" 2>&1); echo "$fx" | grep '^FAIL'
fp=$(echo "$fx" | tail -1 | sed -n 's/^pass=\([0-9]*\) fail=\([0-9]*\)$/\1 \2/p'); set -- $fp
pass=$((pass + ${1:-0})); fail=$((fail + ${2:-1})); echo "wfp fixtures: pass=${1:-?} fail=${2:-?}"

echo "== 2. ledger round trip in a temp run folder under the scratch runs dir (default path <data dir>/runs), then deleted"
L="$CL/lib/ledger.js"; RUN="zz-ledger-selftest-$$"; RD="$(node "$CL/lib/paths.js" --json | jq -r .runsDir)/$RUN"; F="$RD/ledger.jsonl"
base="--run $RUN --mode subagent --repo zz-selftest-repo --area selftest"
o=$(node "$L" add $base --row 1 --role planning --agent orch-planner --model opus --effort high --tokens 88,332 --tools 11 --ms 150000 --first-pass y); rc=$?
ok "ledger add creates the run folder" "$rc $(echo "$o" | grep -c 'new run folder')" "0 1"
node "$L" add $base --row 2 --role implementation --agent orch-builder --model sonnet --effort high --tokens 120000 --tools 30 --ms 300000 >/dev/null; ok "ledger add row 2" "$?" "0"
node "$L" add $base --row 2 --role implementation --agent orch-builder --model sonnet --effort high --tokens 1 --tools 1 --ms 1 >/dev/null 2>&1; ok "ledger duplicate add refused (exit 2)" "$?" "2"
node "$L" update --run $RUN --row 2 --first-pass n >/dev/null; ok "ledger update first-pass n" "$?" "0"
o=$(node "$L" add $base --row 2 --role implementation --agent orch-builder --model opus --effort high --tokens 140000 --tools 25 --ms 280000 --retries 1 --escalated y); ok "ledger retry attempt after escalate" "$?|$(echo "$o" | grep -c 'attempt 2')" "0|1"
node "$L" update --run $RUN --row 2 --first-pass y --tokens 150000 >/dev/null 2>&1; ok "ledger update with no --attempt on a 2-attempt row refused (exit 2, F-18)" "$?" "2"
node "$L" update --attempt "$RUN#2.2" --first-pass y --tokens 150000 >/dev/null; ok "ledger update --attempt names the attempt" "$?" "0"
ts1=$(sed -n 1p "$F" | jq -r .ts); node "$L" update --run $RUN --ts "$ts1" --first-pass y >/dev/null; ok "ledger update --run --ts reaches one line" "$?|$(sed -n 1p "$F" | jq -r 'has("updated")')" "0|true"
node "$L" update --run $RUN --ts 2000-01-01T00:00:00.000Z --first-pass y >/dev/null 2>&1; ok "ledger update --ts with no match refused (exit 2)" "$?" "2"
ok "ledger file: 3 lines" "$(wc -l < "$F" | tr -d ' ')" "3"
ok "ledger: attempt 1 kept its own outcome" "$(sed -n 2p "$F" | jq -r '[.model, .firstPass, .retries, .escalated] | map(tostring) | join(" ")')" "sonnet false 0 false"
ok "ledger: attempt 2 outcome + update" "$(sed -n 3p "$F" | jq -r '[.model, .firstPass, .retries, .escalated, .tokens] | map(tostring) | join(" ")')" "opus true 1 true 150000"
s=$(node "$L" show --run $RUN)
ok "ledger show: table lists 3 attempts" "$(echo "$s" | grep -c "^$RUN ")" "3"
ok "ledger show: sonnet/high 0 accepted rows, first-pass 0/1 (anecdotal *)" "$(echo "$s" | grep -cE '^sonnet/high +0 +- +- +0/1 0%\*')" "1"
ok "ledger show: opus/high (implementation) 1 accepted row, retry tokens folded, 270.0k" "$(echo "$s" | grep -cE '^opus/high +1 +270\.0k +270\.0k +1/1 100%\*')" "1"
ok "ledger show --repo (path form) across runs" "$(node "$L" show --repo /x/zz-selftest-repo/ --last 2 | head -1)" "ledger: 2 of 3 attempts (repo=zz-selftest-repo; newest last)"
o=$(node "$L" add $base --row 3 --role implementation --agent orch-builder --model sonnet --effort high --tokens 88.3k --tools 9 --ms "2m 30s"); ok "ledger takes notice-style numbers (88.3k, '2m 30s')" "$?|$(tail -1 "$F" | jq -r '"\(.tokens) \(.ms)"')" "0|88300 150000"
o=$(node "$L" add $base --row 3 --role implementation --agent orch-builder --model opus --effort high --tokens 99000 --tools 12 --ms 200000); ok "CLAUDE.md flow: plain add for a retry on a new model -> attempt 2, auto retries/escalated" "$?|$(echo "$o" | grep -c 'attempt 2 (retry 1, escalated sonnet/high -> opus/high)')|$(tail -1 "$F" | jq -r '"\(.retries) \(.escalated)"')" "0|1|1 true"
ok "literal 'ledger.js show' (PLAN-TEMPLATE / orch-planner form) exits 0" "$(node "$L" show >/dev/null 2>&1; echo $?)" "0"
node "$L" add --run ../escape --row 1 >/dev/null 2>&1; ok "ledger rejects a path-like --run (exit 2)" "$?" "2"
node "$L" update --run $RUN --row 2 --model haiku >/dev/null 2>&1; ok "ledger update refuses identity fields (exit 2)" "$?" "2"
rm -rf "$RD"; ok "ledger temp run folder deleted" "$([ -e "$RD" ] && echo present || echo gone)" "gone"

echo "== 3. orchestrator-guard exemption (guard runs in place; warning slots go to a scratch ORCH_GUARD_STATE)"
GT=$(mktemp -d); GS="fixture-guard-$$"
g(){ jq -cn --arg s "$1" --arg c "$2" --arg a "${3:-}" '{session_id:$s, hook_event_name:"PreToolUse", tool_name:"Bash", tool_input:{command:$c}} + (if $a == "" then {} else {agent_id:$a} end)' | ORCH_GUARD_STATE="$GT" ORCH_GUARD_AFTER=1 node "$CL/hooks/orchestrator-guard.js"; }
warned(){ jq -r '.hookSpecificOutput.additionalContext // ""' | grep -c '^\[orchestrator\]'; }
ok "ledger add exempt (CLAUDE.md form)" "$(g ${GS}a 'node ~/.claude/orchestrator/ledger.js add --run r --row 1 --role review --agent orch-reviewer --model opus --effort high --mode subagent --tokens 1 --tools 1 --ms 1 --repo x --area y')" ""
ok "ledger show with quoted args exempt" "$(g ${GS}a 'node ~/.claude/orchestrator/ledger.js show --area "header rework" --last 5')" ""
ok "ledger via \$HOME exempt" "$(g ${GS}a 'node $HOME/.claude/orchestrator/ledger.js update --run r --row 1 --first-pass y')" ""
ok "ledger via quoted absolute path exempt" "$(g ${GS}a "node \"$HOME/.claude/orchestrator/ledger.js\" show")" ""
ok "wfp profile show exempt (pre-existing)" "$(g ${GS}a 'node ~/.claude/wfp/wfp.js profile show')" ""
ok "wfp escalate exempt (pre-existing)" "$(g ${GS}a 'node ~/.claude/wfp/wfp.js escalate review --json')" ""
ok "another orchestrator script still warns" "$(g ${GS}b 'node ~/.claude/orchestrator/other.js' | warned)" "1"
ok "ordinary work still warns" "$(g ${GS}c 'npm test' | warned)" "1"
ok "subagent calls stay silent" "$(g ${GS}d 'npm test' agent-123)" ""
ok "a command chained after ledger.js is re-checked and warns (F-08)" "$(g ${GS}e 'node ~/.claude/orchestrator/ledger.js show; npm test' | warned)" "1"
rm -rf "$GT"

echo "pass=$pass fail=$fail"; [ "$fail" -eq 0 ]
