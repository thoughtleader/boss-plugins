#!/usr/bin/env bash
# Dry-run fixtures for wfp. Uses an isolated copy of state (WFP uses its own dir; we use a throwaway sid and clean up).
# Runs against a throwaway copy (own state dir, defaultProfile=null, correctionMode pinned to deny) so the live config/default profile and shipped correctionMode never affect results; updatedInput cases set it themselves.
# WFP_SRC=<plugin root> tests a candidate plugin tree (wfp/wfp.js, wfp/config.default.json, lib/paths.js) instead of this one. Exit code = 0 only when all pass.
# wfp.js runs in place and writes only under the scratch data dir $T (BOSS_DATA): state in $T/state (WFP_STATE_DIR), errors.log, and the user overlay $T/wfp.json.
SRC="${WFP_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/test/lib/compat.sh"   # on macOS: GNU date/stat/touch/md5sum/wc shims (test/lib)
unset ORCH_DIR ORCH_RUNS_DIR CLAUDE_ENV_FILE WFP_SID WFP_STATE_DIR CLAUDE_CONFIG_DIR BOSS_DATA
CFG="$SRC/wfp/config.default.json"
# Preset values are pinned for the legacy bar-logic cases. Section 21 at the end reads the shipped quality preset and asserts its bars and escalate steps (opus/medium planning, plan-check, review, default; diagnosis sonnet/medium; implementation sonnet/high; discovery haiku/medium), so tuning the shipped defaults fails loudly.
FXQ='{"planning":{"model":"opus","effort":"high"},"plan-check":{"model":"opus","effort":"high"},"discovery":{"model":"haiku","effort":"medium"},"implementation":{"model":"sonnet","effort":"high"},"diagnosis":{"model":"sonnet","effort":"high"},"review":{"model":"opus","effort":"high"},"default":{"model":"opus","effort":"high"}}'
T=$(mktemp -d); export BOSS_DATA="$T" WFP_STATE_DIR="$T/state"; BEFORE=$(find "$SRC" -type f | sort | xargs cksum | cksum)
# the pinned quality preset goes in the user overlay (wfp.json); profiles merge one level deep, so the overlay carries the whole quality profile
jq --argjson FXQ "$FXQ" '{defaultProfile: null, correctionMode: "deny", profiles: {quality: (.profiles.quality | .roles = $FXQ)}}' "$CFG" > "$T/wfp.json"
W="$SRC/wfp/wfp.js"; N=node; S="fixture-$$-aaaa"; pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
run(){ printf '%s' "$2" | $N $W "$1"; }
dec(){ local x; x=$(cat); [ -z "$x" ] && { echo none; return; }; echo "$x" | jq -r '.hookSpecificOutput.permissionDecision // "none"'; }
mdl(){ local x; x=$(cat); [ -z "$x" ] && { echo none; return; }; echo "$x" | jq -r '.hookSpecificOutput.permissionDecisionReason | capture("model=(?<m>[a-z]+)").m // "none"' 2>/dev/null || echo none; }
tel='{"session_id":"'$S'","workspace":{"current_dir":"/x"},"model":{"id":"claude-sonnet-5-5"},"effort":{"level":"xhigh"},"context_window":{"context_window_size":1000000,"used_percentage":5,"current_usage":{"input_tokens":1000,"cache_read_input_tokens":40000}},"cost":{"total_cost_usd":1.5}}'
fx(){ case "$2" in -) ;; '') [ -n "$1" ] && echo ',"effort":"max"';; *) echo ",\"effort\":\"$2\"";; esac; } # F-09: a launch with a model gets effort max (above every bar) unless the effort arg is given, or '-' for none
ag(){ echo '{"session_id":"'$S'","hook_event_name":"PreToolUse","tool_name":"Agent","tool_input":{"subagent_type":"'$1'","description":"'$2'","prompt":"p"'${3:+,\"model\":\"$3\"}$(fx "$3" "$4")'}}'; }
why(){ jq -r '.hookSpecificOutput.permissionDecisionReason // "none"'; }
pr(){ echo '{"session_id":"'$S'","hook_event_name":"UserPromptSubmit","prompt":"'$1'"}'; }
# 1 below threshold: silent
run telemetry "$tel"; ok "below threshold silent" "$(run prompt "$(pr 'hello')")" ""
# 2 preflight deny once, then allow
ok "preflight deny" "$(run pretool "$(ag backend-developer implement | tee /dev/null)" | dec)" "deny"
ok "preflight only once" "$(run pretool "$(ag backend-developer implement)" | dec)" "none"
# 3 profile + model rewrite (askBeforeOpus is a session opt-in since presets v2: no preset sets it)
node $W profile economy --sid=$S >/dev/null; node $W set maxLaunches=50 --sid=$S >/dev/null; node $W set askBeforeOpus=true --sid=$S >/dev/null
ok "discovery -> haiku" "$(run pretool "$(ag Explore search)" | mdl)" "haiku"
ok "impl no model -> sonnet" "$(run pretool "$(ag backend-developer implement)" | mdl)" "sonnet"
run pretool "$(ag backend-developer implement sonnet)" >/dev/null
ok "opus asks first" "$(run pretool "$(ag code-reviewer review opus)" | dec)" "deny"
ok "opus then capped" "$(run pretool "$(ag code-reviewer review opus)" | mdl)" "sonnet"
ok "corrected relaunch passes" "$(run pretool "$(ag code-reviewer review sonnet)" | dec)" "none"
ok "cheaper explicit kept" "$(run pretool "$(ag backend-developer implement haiku)" | mdl)" "none"
# 4 launch cap + streak guard (fresh session, cap 2)
S5="$S-e"; run telemetry "${tel//$S/$S5}"; node $W profile economy --sid=$S5 >/dev/null; node $W set maxLaunches=2 maxConcurrent=99 --sid=$S5 >/dev/null # admitted launches count as active (F-16) and these fixtures never send SubagentStart
a5(){ ag Explore search haiku | sed "s/$S/$S5/"; }
run pretool "$(a5)" >/dev/null; run pretool "$(a5)" >/dev/null
ok "launch cap deny" "$(run pretool "$(a5)" | dec)" "deny"
# D8: the launch cap never gives way on retries (was: allowed after the streak); only the owner lifts it
ok "launch cap: 2nd retry denied" "$(run pretool "$(a5)" | dec)" "deny"
ok "launch cap: 3rd retry still denied" "$(run pretool "$(a5)" | dec)" "deny"
ok "launch cap: 4th retry still denied" "$(run pretool "$(a5)" | dec)" "deny"
ok "launch cap text: owner lifts, names set maxLaunches=N + override" "$(run pretool "$(a5)" | why | grep -c 'Only the owner lifts it.*set maxLaunches=N.*set override=true')" "1"
node $W set maxLaunches=10 --sid=$S5 >/dev/null
ok "launch cap lifted by set maxLaunches=N" "$(run pretool "$(a5)" | dec)" "none"
S6="$S-f"; node $W profile economy --sid=$S6 >/dev/null; node $W set maxLaunches=1 --sid=$S6 >/dev/null
a6(){ ag Explore search haiku | sed "s/$S/$S6/"; }; run pretool "$(a6)" >/dev/null
for n in 1 2 3 4; do run pretool "$(a6)" >/dev/null; done
node $W set override=true --sid=$S6 >/dev/null
ok "launch cap lifted by set override=true" "$(run pretool "$(a6)" | dec)" "none"
# 5 override honored via NL
jp(){ jq -cn --arg s "$1" --arg p "$2" '{session_id:$s,hook_event_name:"UserPromptSubmit",prompt:$p}'; }
run prompt "$(jp $S 'just finish this approved workflow')" >/dev/null
ok "override lifts caps" "$(run pretool "$(ag Explore search haiku)" | dec)" "none"
# 6 main-only
S2="$S-b"; sed_tel=${tel//$S/$S2}; run telemetry "$sed_tel"
run prompt '{"session_id":"'$S2'","prompt":"use the main agent only"}' >/dev/null
a2(){ ag Explore s haiku | sed "s/$S/$S2/"; }
ok "main-only deny" "$(run pretool "$(a2)" | dec)" "deny"
ok "main-only: 2nd retry denied" "$(run pretool "$(a2)" | dec)" "deny"
ok "main-only: 3rd retry still denied (D8)" "$(run pretool "$(a2)" | dec)" "deny"
ok "main-only: 4th retry still denied (D8)" "$(run pretool "$(a2)" | dec)" "deny"
ok "main-only text: owner lifts, names set mode=delegate + override" "$(run pretool "$(a2)" | jq -r '.hookSpecificOutput.permissionDecisionReason' | grep -c 'Only the owner lifts it.*set mode=delegate.*set override=true')" "1"
node $W set mode=delegate --sid=$S2 >/dev/null
ok "main-only lifted by set mode=delegate" "$(run pretool "$(a2)" | dec)" "none"
S7="$S-g"; run prompt "$(jp $S7 'use the main agent only')" >/dev/null
a7(){ ag Explore s haiku | sed "s/$S/$S7/"; }; for n in 1 2 3 4; do run pretool "$(a7)" >/dev/null; done
node $W set override=true --sid=$S7 >/dev/null
ok "main-only lifted by set override=true" "$(run pretool "$(a7)" | dec)" "none"
# 7 other session unaffected
S3="$S-c"; ok "no leak to other session" "$(run pretool "$(ag Explore s | sed "s/$S/$S3/")" | dec)" "none"
# 8 checkpoint warn dedupe + stale
big=${tel//40000/200000}; run telemetry "$big"
S4="$S-d"; run telemetry "${big//$S/$S4}"
w1="$(run prompt "$(pr 'continue' | sed "s/$S/$S4/")")"; w2="$(run prompt "$(pr 'continue' | sed "s/$S/$S4/")")"
ok "checkpoint warns once" "$([ -n "$w1" ] && [ -z "$w2" ] && echo y)" "y"
# stale telemetry -> no warn, status says STALE/unknown
node -e "const f='$T/state/$S4.tel.json',fs=require('fs'),t=JSON.parse(fs.readFileSync(f));t.ts-=9999;fs.writeFileSync(f,JSON.stringify(t))"
rm -f $T/state/$S4.json
ok "stale telemetry silent" "$(run prompt "$(pr 'continue' | sed "s/$S/$S4/")")" ""
ok "status marks STALE" "$(node $W status --sid=$S4 | grep -c STALE)" "1"
ok "no telemetry -> unknown" "$(node $W status --sid=$S-none | grep -c 'unknown')" "3"
# 9 compaction survival
ok "restored after compact" "$(run session '{"session_id":"'$S'","source":"compact"}' | grep -c 'restored after compact')" "1"
ok "silent on startup" "$(run session '{"session_id":"'$S'","source":"startup"}')" ""
# SessionStart exports WFP_SID and BOSS_LIB (the plugin's lib dir) through CLAUDE_ENV_FILE, on startup and on compact
EF="$T/envfile"; : > "$EF"; CLAUDE_ENV_FILE="$EF" run session '{"session_id":"'$S'","source":"startup"}' >/dev/null
ok "session exports WFP_SID" "$(grep -c "^export WFP_SID=$S\$" "$EF")" "1"
ok "session exports BOSS_LIB = <plugin root>/lib" "$(grep -c "^export BOSS_LIB='$SRC/lib'\$" "$EF")" "1"
ok "exported BOSS_LIB resolves paths.js" "$(bash -c "$(grep '^export BOSS_LIB' "$EF"); test -f \"\$BOSS_LIB/paths.js\" && echo y")" "y"
# CLAUDE_ENV_FILE is idempotent: compact and resume do not append the lines again; a changed sid appends once and, last line winning, a return to the old sid appends again
CLAUDE_ENV_FILE="$EF" run session '{"session_id":"'$S'","source":"compact"}' >/dev/null; CLAUDE_ENV_FILE="$EF" run session '{"session_id":"'$S'","source":"resume"}' >/dev/null
ok "env file: 3 more session starts add no lines (2 lines in all)" "$(wc -l < "$EF" | tr -d ' ')|$(grep -c "^export WFP_SID=$S\$" "$EF")|$(grep -c '^export BOSS_LIB=' "$EF")" "2|1|1"
CLAUDE_ENV_FILE="$EF" run session '{"session_id":"'$S-envb'","source":"resume"}' >/dev/null
ok "env file: a different session id appends only its WFP_SID line" "$(wc -l < "$EF" | tr -d ' ')|$(tail -1 "$EF")" "3|export WFP_SID=$S-envb"
CLAUDE_ENV_FILE="$EF" run session '{"session_id":"'$S'","source":"resume"}' >/dev/null
ok "env file: back to the first id appends it again (the last line wins when sourced)" "$(wc -l < "$EF" | tr -d ' ')|$(bash -c "source '$EF'; echo \$WFP_SID")" "4|$S"
: > "$EF"; CLAUDE_ENV_FILE="$EF" run session '{"session_id":"'$S'","source":"startup"}' >/dev/null; CLAUDE_ENV_FILE="/nonexistent-dir/env" run session '{"session_id":"'$S'","source":"startup"}'; ok "env file: an unwritable path fails open (rc 0)" "$?" "0"
# 10 malformed input fails open
ok "garbage fails open" "$(echo 'not json' | $N $W pretool; echo rc=$?)" "rc=0"

# ---- presets: bar = default AND minimum; above passes silently; below = advisory only ----
ag2(){ echo '{"session_id":"'$1'","hook_event_name":"PreToolUse","tool_name":"Agent","tool_input":{"subagent_type":"'$2'","description":"'$3'","prompt":"p"'${4:+,\"model\":\"$4\"}$(fx "$4" "$5")'}}'; }
ctx(){ local x; x=$(cat); [ -z "$x" ] && { echo none; return; }; echo "$x" | jq -r '.hookSpecificOutput.additionalContext // "none"'; }
# 11 quality: below the bar -> advisory, no decision; at/above silent; dedupe; skips; override
SQ="$S-q"; node $W profile quality --sid=$SQ >/dev/null; node $W set maxLaunches=99 maxConcurrent=99 --sid=$SQ >/dev/null
o=$(run pretool "$(ag2 $SQ orch-builder 'wire it' haiku low)")
ok "below bar: no permission decision" "$(echo "$o" | dec)" "none"
ok "below bar: advisory names role + bar" "$(echo "$o" | ctx | grep -c 'orch-builder (implementation) launched at haiku/low, below the quality bar sonnet/high')" "1"
ok "below bar: note deduped (cooldown)" "$(run pretool "$(ag2 $SQ orch-builder 'wire it' haiku low)")" ""
ok "at bar: silent" "$(run pretool "$(ag2 $SQ orch-builder 'wire it' sonnet high)")" ""
ok "above bar (opus/max): silent" "$(run pretool "$(ag2 $SQ orch-builder 'wire it' opus max)")" ""
ok "same model, lower effort: advisory" "$(run pretool "$(ag2 $SQ orch-planner 'plan it' opus low)" | ctx | grep -c 'below the quality bar opus/high')" "1"
ok "bar model without effort: corrected for effort alone (F-09)" "$(run pretool "$(ag2 $SQ orch-reviewer 'check it' opus -)" | why)" "[wfp] Session policy for review agents: relaunch this same Agent call with effort=high. No need to ask the user."
ok "typeRoles beats description words" "$(run pretool "$(ag2 $SQ orch-builder 'review fixes' haiku medium)" | ctx | grep -c 'orch-builder (implementation)')" "1"
ok "plan-check classified before review" "$(run pretool "$(ag2 $SQ gsd-plan-checker 'check plans' haiku)" | ctx | grep -c '(plan-check)')" "1"
ok "default role not judged" "$(run pretool "$(ag2 $SQ general-purpose 'find files' haiku low)")" ""
ok "fork not judged" "$(run pretool "$(ag2 $SQ fork 'review it' haiku)")" ""
ok "unranked model not judged" "$(run pretool "$(ag2 $SQ orch-reviewer 'check it' fable)")" ""
ok "missing model: filled with bar model + effort" "$(run pretool "$(ag2 $SQ orch-reviewer 'check it')" | why | grep -c 'model=opus effort=high')" "1"
node $W set override=true --sid=$SQ >/dev/null
ok "override: below bar silent" "$(run pretool "$(ag2 $SQ orch-scout 'find it' haiku low)")" ""
# 12 above-bar Opus is never denied or warned (economy + balanced presets), including an escalated retry
for pf in economy balanced; do
  SX="$S-$pf"; node $W profile $pf --sid=$SX >/dev/null; node $W set maxConcurrent=99 --sid=$SX >/dev/null
  ok "$pf: above-bar opus builder silent" "$(run pretool "$(ag2 $SX orch-builder 'build it' opus high)")" ""
  ok "$pf: above-bar opus reviewer silent" "$(run pretool "$(ag2 $SX orch-reviewer 'review it' opus xhigh)")" ""
  up=$(node $W escalate review --json --sid=$SX | jq -r '.to.model + " " + .to.effort')
  ok "$pf: escalated retry ($up) silent" "$(run pretool "$(ag2 $SX orch-reviewer 'review again' $up)")" ""
done
# 13 max: below the bar for review says "never"; implementation below says mechanical
SM="$S-mx"; node $W profile max --sid=$SM >/dev/null
ok "max: review below bar -> never" "$(run pretool "$(ag2 $SM orch-reviewer 'review it' sonnet high)" | ctx | grep -c 'max never allows going below the bar for review')" "1"
ok "max: implementation below bar -> mechanical" "$(run pretool "$(ag2 $SM orch-builder 'build it' sonnet high)" | ctx | grep -c 'clearly mechanical')" "1"
ok "max: planning at xhigh bar silent" "$(run pretool "$(ag2 $SM orch-planner 'plan it' opus xhigh)")" ""
# 14 ultracode: raises the bar one step in the hook; survives a profile switch; off restores
SU="$S-uc"; node $W profile quality --sid=$SU >/dev/null; node $W ultracode on --sid=$SU >/dev/null
ok "ultracode: quality impl sonnet/high is below the max bar" "$(run pretool "$(ag2 $SU orch-builder 'build it' sonnet high)" | ctx | grep -c 'below the max bar opus/high')" "1"
ok "ultracode: missing model filled from max" "$(run pretool "$(ag2 $SU orch-planner 'plan it')" | why | grep -c 'model=opus effort=xhigh')" "1"
node $W profile balanced --sid=$SU >/dev/null
ok "ultracode survives a profile switch" "$(node $W profile show --sid=$SU | head -1 | grep -c 'preset balanced (this session) · ultracode on')" "1"
node $W ultracode off --sid=$SU >/dev/null
ok "ultracode off: back to the balanced bar" "$(node $W profile show --sid=$SU | sed -n 2p | grep -c '^role  *balanced \*')" "1"
# 15 legacy policies (old roleModels shape) migrate at read time; state files untouched
lg(){ printf '{"policy":{"set":true,"mode":"delegate","override":false,"roleModels":{"discovery":"sonnet","implementation":"%s","review":"opus","planning":"opus","default":"opus"},"maxConcurrent":4,"maxLaunches":12,"askBeforeOpus":false,"profile":"%s"},"preflightAsked":1}' "$1" "$2"; }
mkdir -p "$T/state"; SL="$S-lg"; lg sonnet quality > "$T/state/$SL.json"
ok "legacy preset policy: shipped quality bar (haiku scout silent)" "$(run pretool "$(ag2 $SL orch-scout 'find it' haiku medium)")" ""
ok "legacy preset policy: plan-check gets the live bar" "$(node $W escalate plan-check --json --sid=$SL | jq -r '.bar.model + "/" + .bar.effort')" "opus/high"
SLC="$S-lc"; lg haiku 'quality+custom' > "$T/state/$SLC.json"
ok "legacy custom policy: its model map stays explicit" "$(run pretool "$(ag2 $SLC orch-builder 'build it')" | why | grep -c 'model=haiku effort=high')" "1"
# 16 set role.<hyphenated role> parses (was silently dropped before v2)
SR="$S-sr"; node $W profile quality --sid=$SR >/dev/null; node $W set role.plan-check=sonnet --sid=$SR >/dev/null
ok "set role.plan-check applies" "$(node $W profile show --sid=$SR | grep -c '^plan-check  *sonnet/high')" "1"
# 17 profile show: 4 presets x ultracode off/on; the active column and the below-bar rule follow the raised preset
for pf in economy balanced quality max; do
  up=$(jq -r ".ultracodeRaise.$pf" "$CFG")
  ok "show $pf off: active = $pf" "$(node $W profile show $pf --ultracode=off | sed -n 2p | grep -c "^role  *$pf \*")" "1"
  ok "show $pf on: active = $up" "$(node $W profile show $pf --ultracode=on | sed -n 2p | grep -c "ultracode: $up \*")" "1"
done
ok "show quality on: review below bar = never (max rule)" "$(node $W profile show quality --ultracode=on | grep -c '^review .*never$')" "1"
ok "show quality off: review below bar = mechanical only" "$(node $W profile show quality --ultracode=off | grep -c '^review .*mechanical rows only$')" "1"
ok "show without policy or default: says so" "$(node $W profile show --sid=$S-nopolicy | grep -c 'no session policy and no lasting default')" "1"
# 18 escalate: every role under quality, then top / below-bar / agent type / ultracode / bad input
SE="$S-es"; node $W profile quality --sid=$SE >/dev/null
for pair in planning:opus/xhigh plan-check:opus/xhigh discovery:sonnet/medium implementation:opus/high diagnosis:opus/high review:opus/xhigh default:opus/xhigh; do
  r=${pair%%:*}; want=${pair#*:}
  ok "escalate $r -> $want" "$(node $W escalate $r --json --sid=$SE | jq -r '.to.model + "/" + .to.effort')" "$want"
done
ok "escalate at the top -> blocked" "$(node $W escalate review opus/max --sid=$SE | grep -c 'no step above opus/max. Mark the row blocked')" "1"
ok "escalate a below-bar row -> back to the bar" "$(node $W escalate implementation haiku/low --json --sid=$SE | jq -r '.to.model + "/" + .to.effort')" "sonnet/high"
ok "escalate by agent type" "$(node $W escalate orch-scout --json --sid=$SE | jq -r .role)" "discovery"
ok "escalate under ultracode (max bar)" "$(node $W escalate implementation --ultracode=on --json --sid=$SE | jq -r '.to.model + "/" + .to.effort')" "opus/xhigh"
ok "escalate bad role -> usage" "$(node $W escalate nope --sid=$SE | grep -c 'usage: escalate')" "1"
ok "escalate bad step -> message" "$(node $W escalate review gpt/high --sid=$SE | grep -c 'cannot read')" "1"
# 19 orch-* agent types map by exact type before any regex (one session each: a missing model gets a fill-deny naming the role)
i=0
for c in 'orch-planner|Plan header rework|planning' 'orch-plan-checker|Check the plan|plan-check' 'orch-scout|Find drag handlers|discovery' \
         'orch-scout|Diagnose header flicker|diagnosis' 'orch-scout|Root cause of the OOM|diagnosis' 'orch-scout|Debug the drop target|diagnosis' \
         'orch-builder|Plan row 3: review fixes|implementation' 'orch-reviewer|Build check after row 2|review'; do
  IFS='|' read -r ty de want <<< "$c"; i=$((i+1)); SX="$S-ty$i"; node $W profile quality --sid=$SX >/dev/null
  ok "map $ty '$de' -> $want" "$(run pretool "$(ag2 $SX $ty "$de")" | why | grep -c "policy for $want agents")" "1"
done
ok "escalate by agent type uses the base role (orch-scout -> discovery)" "$(node $W escalate orch-scout --json --sid=$SE | jq -r '.role + " " + .bar.model')" "discovery haiku"
# 21 F-04 prompt parser: only the owner's own words set policy. Notices stripped; quoted/code, negated, interrogative,
#    recap (count setters) and topic-scoped (override) matches change nothing. Each case runs in a fresh session; asserts read state.
pc=0; pol(){ node $W json --sid=$1 | jq -r ".state.policy.$2 // \"unset\""; }
cs(){ pc=$((pc+1)); local x="$S-p$pc"; run prompt "$(jp $x "$2")" >/dev/null; ok "$1" "$(pol $x $3)" "$4"; LAST=$x; }
NB='<task-notification>
<task-id>a1b2</task-id>
<status>completed</status>
<result>'; NE='</result>
</task-notification>'
# main-only family
cs "main setter: plain applies" 'Use the main agent only.' mode main-only
cs "main setter: alt phrasing applies" 'Do this with no subagents, thanks.' mode main-only
cs "main setter: negated" 'Never use the main agent only.' mode unset
cs "main setter: negated (3 words before)" 'Please do not go without any subagents.' mode unset
cs "main setter: double-quoted" 'The doc says "use the main agent only" for drafts.' mode unset
cs "main setter: backticked" 'Ignore the `no subagents` label.' mode unset
cs "main setter: interrogative" 'Should we use the main agent only?' mode unset
cs "main setter: notice only" "$NB Row 3 says: use the main agent only. $NE" mode unset
cs "main setter: notice + owner text (owner count applies)" "$NB Row 3 says: use the main agent only. $NE
Run max 3 agents at once." mode delegate
ok "main setter: notice + owner text, owner value set" "$(pol $LAST maxConcurrent)" "3"
# maxConcurrent family
cs "maxConcurrent setter: plain applies" 'Run max 3 agents at once.' maxConcurrent 3
cs "maxConcurrent setter: negated" 'Do not run max 3 agents at once.' maxConcurrent unset
cs "maxConcurrent setter: single-quoted" "Rename the field 'max 5 agents' please." maxConcurrent unset
cs "maxConcurrent setter: curly-quoted" 'The label “maximum of 5 agents” is old.' maxConcurrent unset
cs "maxConcurrent setter: interrogative" 'Can we do max 4 agents at once?' maxConcurrent unset
cs "maxConcurrent setter: past-tense recap" 'Last week we ran max 6 agents at once.' maxConcurrent unset
cs "maxConcurrent setter: notice only" "$NB Plan: max 7 agents at once. $NE" maxConcurrent unset
cs "maxConcurrent setter: notice + owner text (owner value wins)" "$NB Plan: max 7 agents at once. $NE
OK, max 3 agents at once." maxConcurrent 3
cs "maxConcurrent setter: owner text before the notice" "OK, max 2 agents at once.
$NB Plan: max 7 agents at once. $NE" maxConcurrent 2
# maxLaunches family
cs "maxLaunches setter: plain applies" 'Budget 12 launches for this.' maxLaunches 12
cs "maxLaunches setter: contractions are not quotes (applies)" "Don't worry, it's fine: allow 10 launches, that's it." maxLaunches 10
cs "maxLaunches setter: negated" 'We do not need 20 launches.' maxLaunches unset
cs "maxLaunches setter: single-quoted" "Rename the field 'cap 9 launches' please." maxLaunches unset
cs "maxLaunches setter: code fence" 'See:
```
set 9 total launches
```' maxLaunches unset
cs "maxLaunches setter: interrogative" 'Is 15 launches enough?' maxLaunches unset
cs "maxLaunches setter: past-tense recap" 'In the old plan we set 30 launches.' maxLaunches unset
cs "maxLaunches setter: notice only" "$NB launch total is 7 agent launches $NE" maxLaunches unset
cs "maxLaunches setter: notice + owner text (owner value wins)" "$NB launch total is 7 agent launches $NE
Allow 3 launches." maxLaunches 3
cs "maxLaunches setter: unterminated notice stripped" "Allow 4 launches.
<task-notification><result>then 9 launches" maxLaunches 4
# override family
cs "override setter: plain applies" 'Just finish this approved workflow.' override true
cs "override setter: alt phrasing applies" 'Stop repeating that warning.' override true
cs "override setter: negated (live F-04 shape)" 'Do not stop warning me.' override unset
cs "override setter: negated contraction" "Please don't stop warning me." override unset
cs "override setter: curly-quoted" '“stop warning me” is a bad label.' override unset
cs "override setter: code fence" 'Fixture:
```
stop warning me
```' override unset
cs "override setter: interrogative" 'Can you stop warning me?' override unset
cs "override setter: scoped to a topic" 'Stop warning me about the context size.' override unset
cs "override setter: notice only" "$NB Kilo: just finish this approved workflow. $NE" override unset
cs "override setter: notice + owner text (owner count applies)" "$NB Kilo: just finish this approved workflow. $NE
Max 2 agents." override unset
ok "override setter: notice + owner text, owner value set" "$(pol $LAST maxConcurrent)" "2"
ok "reassess: notice-only text is not read" "$(run prompt "$(jp $S-p0 "$NB would a fresh chat help $NE")")" ""
ok "reassess: owner text still read" "$(run prompt "$(jp $S-p0 'would a fresh chat help')" | jq -r '.hookSpecificOutput.additionalContext' | grep -c 'reassess requested')" "1"
# 22 loop guard (A-15): model/conc/opus still give way after the streak, with a note + one errors.log line
SG="$S-lg2"; node $W profile quality --sid=$SG >/dev/null
ok "model deny: 1st model-less launch denied" "$(run pretool "$(ag2 $SG orch-builder 'build it')" | dec)" "deny"
ok "model deny: 2nd denied" "$(run pretool "$(ag2 $SG orch-builder 'build it')" | dec)" "deny"
o=$(run pretool "$(ag2 $SG orch-builder 'build it')")
ok "model deny gives way after the streak" "$(echo "$o" | dec)" "none"
ok "give-way note names the key" "$(echo "$o" | ctx | grep -c "Loop guard: the 'model' deny")" "1"
ok "give-way writes one errors.log line" "$(grep -c "loop-guard let the 'model' deny pass" "$T/errors.log")" "1"
# denials.jsonl: every deny (and the loop-guard pass) is one JSON line {ts, sid, tool, hook, key, agent, decision, reason}; an allowed launch writes none
DL="$T/wfp/denials.jsonl"; dl(){ jq -rs --arg s "$1" "[.[] | select(.sid == \$s)] | $2" "$DL"; }
ok "denials log: 3 lines for the loop-guard session (2 denies + 1 loop-guard pass), none for the launches that were allowed" "$(dl $SG 'length')" "3"
ok "denials log: decisions in order" "$(dl $SG 'map(.decision) | join(",")')" "deny,deny,loop-guard-pass"
ok "denials log: line shape (ts, sid, tool, hook, key, agent, reason)" "$(dl $SG '.[0] | [(.ts | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T")), .tool, .hook, .key, .agent, (.reason | startswith("[wfp] Session policy for"))] | map(tostring) | join(" ")')" "true Agent pretool model orch-builder true"
ok "denials log: every line is valid JSON" "$(jq -s 'length' "$DL" >/dev/null 2>&1 && echo yes)" "yes"
# 23 F-03: preflight text puts the table in the AskUserQuestion preview, never as text or a plain-text question
SP="$S-pf"; o=$(run pretool "$(ag2 $SP backend-developer implement)" | why)
ok "preflight text: preview, not text before the call" "$(echo "$o" | grep -c "recommended option's preview.*Never print it as text before the call.*never ask as a plain-text question")" "1"
ok "denials log: the preflight deny is logged (key preflight, decision deny)" "$(dl $SP 'map(.key + ":" + .decision) | join(",")')" "preflight:deny"
ok "preflight text: no absolute path (the command goes through BOSS_LIB)" "$(echo "$o" | grep -c '/home/\|/Users/\|'"$W")|$(echo "$o" | grep -cF 'node "$BOSS_LIB/../wfp/wfp.js" profile <')" "0|1"
ok "preflight text: shorter than 800 chars" "$([ "${#o}" -lt 800 ] && echo short || echo "long ${#o}")" "short"
# 24 F-17: --sid is validated before any path is built; mutating commands refuse to guess among live sessions for a cwd
ok "F-17: --sid=../x refused" "$(node $W set maxLaunches=5 --sid=../x | grep -c "invalid --sid")" "1"
ok "F-17: --sid=../x wrote nothing outside state/" "$([ -e "$T/x.json" ] || [ -e "$T/x.lock" ] && echo wrote || echo clean)" "clean"
ok "F-17: --sid= (empty) refused" "$(node $W reset --sid= | grep -c "invalid --sid")" "1"
ok "F-17: --sid with a slash refused (status too)" "$(node $W status --sid=aaaaaaaa/../b | grep -c "invalid --sid")" "1"
CW="$T/cw"; mkdir -p "$CW"; CW=$(cd "$CW" && pwd -P); SC1="$S-cw1"; SC2="$S-cw2"
cwt(){ jq -cn --arg s "$1" --arg c "$CW" '{session_id:$s,workspace:{current_dir:$c},model:{id:"claude-sonnet-5-5"},context_window:{context_window_size:1000000,used_percentage:5,current_usage:{input_tokens:1000}}}'; }
run telemetry "$(cwt $SC1)"; run telemetry "$(cwt $SC2)"
cw(){ (cd "$CW" && env -u WFP_SID node $W "$@"); }
ok "F-17: two fresh cwd sessions: set refuses" "$(cw set maxLaunches=40 | grep -c "refusing to guess the session")" "1"
ok "F-17: refused set changed no session" "$(ls "$T/state/$SC1.json" "$T/state/$SC2.json" 2>/dev/null | wc -l)" "0"
ok "F-17: profile / ultracode / reset / handoff refuse too" "$(for c in 'profile quality' 'ultracode on' reset handoff; do cw $c | grep -c "refusing to guess"; done | tr -d '\n')" "1111"
ok "F-17: profile show still falls back (read-only)" "$(cw profile show quality | head -1 | grep -c "wfp preset quality")" "1"
ok "F-17: status still falls back" "$(cw status | grep -c "^session ")" "1"
ok "F-17: set with --sid=<id> works" "$(cw set maxLaunches=40 --sid=$SC1 | grep -c "maxLaunches=40")" "1"
ok "F-17: WFP_SID still works for set" "$(cd "$CW" && WFP_SID=$SC2 node $W set maxLaunches=41 | grep -c "maxLaunches=41")" "1"
node -e "const f='$T/state/$SC2.tel.json',fs=require('fs'),t=JSON.parse(fs.readFileSync(f));t.ts-=3600;fs.writeFileSync(f,JSON.stringify(t))"
ok "F-17: one fresh + one old session: set falls back to the fresh one" "$(cw set maxLaunches=42 | grep -c "maxLaunches=42")" "1"
# 25 F-16: an exhausted lock still persists (atomic) and logs one line; admitted-pending launches count as active
SK="$S-lk"; node $W profile economy --sid=$SK >/dev/null; node $W set maxLaunches=50 --sid=$SK >/dev/null
mkdir "$T/state/$SK.lock.json"; : > "$T/errors.log"
run pretool "$(ag2 $SK Explore search haiku)" >/dev/null; rmdir "$T/state/$SK.lock.json"
ok "F-16: exhausted lock still persists the launch" "$(jq -r .launches "$T/state/$SK.json")" "1"
ok "F-16: exhausted lock logs one errors.log line" "$(grep -c "update lock exhausted" "$T/errors.log")" "1"
ok "F-16: exhausted lock leaves no tmp file" "$(ls "$T/state" | grep -c "$SK.json.*tmp")" "0"
SA="$S-ac"; node $W profile economy --sid=$SA >/dev/null; node $W set maxLaunches=50 maxConcurrent=1 --sid=$SA >/dev/null
ss(){ echo '{"session_id":"'$SA'","agent_id":"'$1'","agent_type":"Explore"}'; }
ok "pending: first launch admitted" "$(run pretool "$(ag2 $SA Explore search haiku)" | dec)" "none"
o=$(run pretool "$(ag2 $SA Explore search haiku)")
ok "pending: admitted slot counts as active (before SubagentStart)" "$(echo "$o" | dec)" "deny"
ok "pending: deny text names 1 active" "$(echo "$o" | why | grep -c "^\[wfp\] 1 agents already active (max 1)")" "1"
run substart "$(ss a1)"
ok "pending: started agent counted once (active + its pending entry)" "$(run pretool "$(ag2 $SA Explore search haiku)" | why | grep -c "^\[wfp\] 1 agents already active (max 1)")" "1"
run substop "$(ss a1)"
ok "pending: stopped agent frees the slot" "$(run pretool "$(ag2 $SA Explore search haiku)" | dec)" "none"
SB="$S-ac2"; node $W profile economy --sid=$SB >/dev/null; node $W set maxLaunches=50 maxConcurrent=1 --sid=$SB >/dev/null
run pretool "$(ag2 $SB Explore search haiku)" >/dev/null
node -e "const f='$T/state/$SB.json',fs=require('fs'),t=JSON.parse(fs.readFileSync(f));t.pending.forEach(p=>p.t-=100);fs.writeFileSync(f,JSON.stringify(t))"
ok "pending: an entry over 60 s old is not counted" "$(run pretool "$(ag2 $SB Explore search haiku)" | dec)" "none"
ok "pending: entry shape on push (F-15 adds desc, tuid)" "$(jq -c '.pending[0] | keys' "$T/state/$SB.json")" '["desc","final","req","role","t","tuid","type"]'
# F-15: SubagentStop matches its own pending entry by tool_use_id, then description, then a single same-type entry, and consumes it
SS="$S-f15"; node $W profile economy --sid=$SS >/dev/null; node $W set maxLaunches=50 maxConcurrent=99 --sid=$SS >/dev/null
MD="$T/f15"; mkdir -p "$MD"
agt(){ echo '{"session_id":"'$SS'","hook_event_name":"PreToolUse","tool_name":"Agent","tool_use_id":"'$1'","tool_input":{"subagent_type":"Explore","description":"'$2'","prompt":"p","model":"haiku","effort":"'$3'"}}'; }
stp(){ echo '{"session_id":"'$SS'","agent_id":"'$1'","agent_type":"Explore","agent_transcript_path":"'$MD'/agent-'$1'.jsonl"}'; }
mt(){ echo "$2" > "$MD/agent-$1.meta.json"; }
run pretool "$(agt toolu_A 'scan alpha' low)" >/dev/null; run pretool "$(agt toolu_B 'scan beta' medium)" >/dev/null
ok "F-15: both launches recorded with tuid and desc" "$(jq -c '[.pending[] | [.tuid, .desc]]' "$T/state/$SS.json")" '[["toolu_A","scan alpha"],["toolu_B","scan beta"]]'
mt s1 '{"agentType":"Explore","description":"scan beta","toolUseId":"toolu_B"}'
run substop "$(stp s1)"
ok "F-15: the later launch's stop matches its own entry by tuid (effort medium, not the first entry's low)" "$(jq -r '.agents[-1].requested' "$T/state/$SS.json")" "haiku"
ok "F-15: the matched entry is consumed; the other stays" "$(jq -c '[.pending[].tuid]' "$T/state/$SS.json")" '["toolu_A"]'
mt s2 '{"agentType":"Explore","description":"scan alpha","toolUseId":"toolu_A"}'
run substop "$(stp s2)"
ok "F-15: the second stop consumes the first entry; pending empty" "$(jq -c '.pending' "$T/state/$SS.json")" '[]'
# same-type launches that differ only by tuid: each stop takes its own entry even when the order is reversed
run pretool "$(agt toolu_C 'same text' low)" >/dev/null; run pretool "$(agt toolu_D 'same text' low)" >/dev/null
mt s3 '{"agentType":"Explore","description":"same text","toolUseId":"toolu_D"}'
run substop "$(stp s3)"
ok "F-15: identical descriptions, different tuids: the stop with tuid D consumes D, not C" "$(jq -c '[.pending[].tuid]' "$T/state/$SS.json")" '["toolu_C"]'
mt s4 '{"agentType":"Explore","description":"same text","toolUseId":"toolu_C"}'; run substop "$(stp s4)"
# no tuid on the entry (older hook payload): a matching description still matches
SD="$S-f15d"; node $W profile economy --sid=$SD >/dev/null; node $W set maxLaunches=50 maxConcurrent=99 --sid=$SD >/dev/null
agd(){ echo '{"session_id":"'$SD'","hook_event_name":"PreToolUse","tool_name":"Agent","tool_input":{"subagent_type":"Explore","description":"'$1'","prompt":"p","model":"haiku","effort":"'$2'"}}'; }
sd(){ echo '{"session_id":"'$SD'","agent_id":"'$1'","agent_type":"Explore","agent_transcript_path":"'$MD'/agent-'$1'.jsonl"}'; }
run pretool "$(agd 'find one' low)" >/dev/null; run pretool "$(agd 'find two' medium)" >/dev/null
ok "F-15: no tool_use_id on the hook payload stores tuid null" "$(jq -c '[.pending[].tuid]' "$T/state/$SD.json")" '[null,null]'
mt d1 '{"agentType":"Explore","description":"find two","toolUseId":"toolu_none"}'
run substop "$(sd d1)"
ok "F-15: no tuid, matching description still matches (and consumes) the right entry" "$(jq -c '[.pending[].desc]' "$T/state/$SD.json")" '["find one"]'
ok "F-15: matched entry's effort path recorded" "$(jq -r '.agents[-1].policyModel' "$T/state/$SD.json")" "haiku"
# 200-character cut applies to both sides
LD=$(printf 'x%.0s' $(seq 1 230))
run pretool "$(agd "$LD" low)" >/dev/null; run pretool "$(agd 'other' low)" >/dev/null
mt d2 '{"agentType":"Explore","description":"'$LD'"}'
run substop "$(sd d2)"
ok "F-15: a 230-character description matches its 200-character stored cut" "$(jq -c '[.pending[].desc | length]' "$T/state/$SD.json")" '[8,5]'
# missing meta.json: falls back to type when exactly one same-type entry remains, else requested is unknown and nothing is consumed
rm -f "$MD/agent-d3.meta.json"; SE="$S-f15e"; node $W profile economy --sid=$SE >/dev/null; node $W set maxLaunches=50 maxConcurrent=99 --sid=$SE >/dev/null
age(){ echo '{"session_id":"'$SE'","hook_event_name":"PreToolUse","tool_name":"Agent","tool_use_id":"'$1'","tool_input":{"subagent_type":"Explore","description":"'$2'","prompt":"p","model":"haiku","effort":"low"}}'; }
se(){ echo '{"session_id":"'$SE'","agent_id":"'$1'","agent_type":"Explore","agent_transcript_path":"'$MD'/agent-'$1'.jsonl"}'; }
run pretool "$(age toolu_E 'only one')" >/dev/null
run substop "$(se e1)"
ok "F-15: missing meta.json, one same-type entry: falls back to type and consumes it" "$(jq -r '(.agents[-1].requested) + "|" + (.pending | length | tostring)' "$T/state/$SE.json")" "haiku|0"
run pretool "$(age toolu_F 'first')" >/dev/null; run pretool "$(age toolu_G 'second')" >/dev/null
run substop "$(se e2)"
ok "F-15: missing meta.json, two same-type entries: requested unknown, none consumed" "$(jq -r '(.agents[-1].requested) + "|" + (.pending | length | tostring)' "$T/state/$SE.json")" "unknown|2"
echo '{not json' > "$MD/agent-e3.meta.json"; run substop "$(se e3)"
ok "F-15: unreadable meta.json fails open (still unknown, state intact)" "$(jq -r '(.agents[-1].requested) + "|" + (.pending | length | tostring)' "$T/state/$SE.json")" "unknown|2"
ok "F-15: a stop with no pending entry at all records requested none" "$(printf '%s' '{"session_id":"'$SE'-z","agent_id":"z1","agent_type":"Explore"}' | $N $W substop; jq -r '.agents[-1].requested // "none"' "$T/state/$SE-z.json" 2>/dev/null || echo none)" "none"
# 26 F-29: set on a legacy un-customized policy drops its stale roleModels copy; a customized one keeps it
SL2="$S-lg3"; lg sonnet quality > "$T/state/$SL2.json"; node $W set maxLaunches=40 --sid=$SL2 >/dev/null
ok "F-29: legacy policy + set maxLaunches=40: discovery bar stays haiku/medium" "$(node $W escalate discovery --json --sid=$SL2 | jq -r '.bar.model + "/" + .bar.effort')" "haiku/medium"
ok "F-29: scout at haiku/medium stays silent after set" "$(run pretool "$(ag2 $SL2 orch-scout 'find it' haiku medium)")" ""
ok "F-29: stale roleModels removed, +custom appended" "$(jq -r '(.policy.roleModels // "none") + "|" + .policy.profile' "$T/state/$SL2.json" 2>/dev/null || jq -r '"obj|" + .policy.profile' "$T/state/$SL2.json")" "none|quality+custom"
SL3="$S-lg4"; lg haiku 'quality+custom' > "$T/state/$SL3.json"; node $W set maxLaunches=40 --sid=$SL3 >/dev/null
ok "F-29: customized legacy policy keeps its explicit model map" "$(node $W escalate implementation --json --sid=$SL3 | jq -r '.bar.model')" "haiku"
SL4="$S-lg5"; lg sonnet quality > "$T/state/$SL4.json"; node $W set role.review=sonnet --sid=$SL4 >/dev/null
ok "F-29: set role.X on a legacy policy overrides only X" "$(node $W escalate discovery --json --sid=$SL4 | jq -r '.bar.model') $(node $W escalate review --json --sid=$SL4 | jq -r '.bar.model')" "haiku sonnet"
# 27 A-18: a user overlay (wfp.json) that fails to parse fails open on the shipped defaults, with one errors.log line and one systemMessage per session
T2=$(mktemp -d); echo '{ "profiles": ' > "$T2/wfp.json"; W2="$W"; SB1="$S-bc"
w2(){ BOSS_DATA="$T2" WFP_STATE_DIR="$T2/state" node $W2 "$@"; }
o1=$(printf '%s' "$(ag2 $SB1 Explore search haiku)" | w2 pretool); o2=$(printf '%s' "$(ag2 $SB1 Explore search haiku)" | w2 pretool)
ok "A-18: broken config: first call carries one systemMessage naming the file" "$(echo "$o1" | jq -r '.systemMessage // "none"' | grep -c "failed to parse")" "1"
ok "A-18: broken config: message not repeated in the session" "$(echo "${o2:-{\}}" | jq -r '.systemMessage // "none"')" "none"
ok "A-18: broken config: still fails open (second launch is allowed)" "$(echo "${o2:-{\}}" | jq -r '.hookSpecificOutput.permissionDecision // "none"')" "none"
ok "A-18: broken config: one errors.log line" "$(grep -c "config failed to parse" "$T2/errors.log")" "1"
ok "A-18: broken config: SessionStart startup emits the message" "$(printf '%s' '{"session_id":"'$S-bs'","source":"startup"}' | w2 session | jq -r '.systemMessage // "none"' | grep -c "failed to parse")" "1"
og=$(run pretool "$(ag2 $S-gc Explore search haiku)"); ok "A-18: good config: no systemMessage" "$(echo "${og:-{\}}" | jq -r '.systemMessage // "none"')" "none"
rm -rf "$T2"
# 28 F-09 effort fill, F-28 correctionMode, A-06 named launches, A-08 role from subagent_type only
tojson(){ jq -c "$1" "$T/wfp.json" > "$T/c.tmp" && mv "$T/c.tmp" "$T/wfp.json"; } # edits the user overlay, as a user would
ui(){ jq -c '.hookSpecificOutput | {d: (.permissionDecision // "none"), m: (.updatedInput.model // "-"), e: (.updatedInput.effort // "-"), p: (.updatedInput.prompt // "-"), n: (.updatedInput.description // "-"), t: (.updatedInput.subagent_type // "-")}'; }
SF="$S-f9"; node $W profile quality --sid=$SF >/dev/null; node $W set maxLaunches=99 maxConcurrent=99 --sid=$SF >/dev/null
ok "F-09: haiku with no effort (orch-scout, quality) is corrected to effort=medium only" "$(run pretool "$(ag2 $SF orch-scout 'find it' haiku -)" | why)" "[wfp] Session policy for discovery agents: relaunch this same Agent call with effort=medium. No need to ask the user."
ok "F-09: the corrected relaunch (haiku/medium) passes" "$(run pretool "$(ag2 $SF orch-scout 'find it' haiku medium)")" ""
ok "F-09: below-bar explicit model keeps its model (sonnet no effort, quality review: effort=high, no model=)" "$(run pretool "$(ag2 $SF orch-reviewer 'check it' sonnet -)" | why | grep -c 'with effort=high\.')" "1"
ok "F-09: above-bar launch with no effort passes untouched (opus builder, quality bar sonnet/high)" "$(run pretool "$(ag2 $SF orch-builder 'build it' opus -)")" ""
ok "F-09: above-bar launch with effort passes untouched" "$(run pretool "$(ag2 $SF orch-builder 'build it' opus max)")" ""
ok "F-09: unranked model with no effort is never corrected" "$(run pretool "$(ag2 $SF orch-reviewer 'check it' fable -)")" ""
ok "F-09: missing model filled (deny mode)" "$(run pretool "$(ag2 $SF orch-reviewer 'check it')" | why | grep -c 'model=opus effort=high')" "1"
tojson '.correctionMode="updatedInput"'
SG="$S-ui"; node $W profile quality --sid=$SG >/dev/null; node $W set maxLaunches=99 maxConcurrent=99 --sid=$SG >/dev/null
o=$(run pretool "$(ag2 $SG orch-reviewer 'check it')" | ui)
ok "F-28: missing model filled by updatedInput, no permissionDecision" "$(echo "$o" | jq -r '.d + " " + .m + " " + .e')" "none opus high"
ok "F-28: updatedInput keeps every other field" "$(echo "$o" | jq -r '.p + " " + .n + " " + .t')" "p check it orch-reviewer"
o=$(run pretool "$(ag2 $SG orch-scout 'find it' haiku -)" | ui)
ok "F-28: haiku with no effort rewritten to medium, model kept" "$(echo "$o" | jq -r '.d + " " + .m + " " + .e')" "none haiku medium"
ok "F-28: the same call is not denied twice (rewrite never loops)" "$(run pretool "$(ag2 $SG orch-scout 'find it' haiku -)" | ui | jq -r .d)" "none"
ok "F-28: above-bar launch passes untouched" "$(run pretool "$(ag2 $SG orch-builder 'build it' opus max)")" ""
ok "F-28: a rewrite counts as a launch" "$(jq -r .launches "$T/state/$SG.json")" "4"
ok "F-28: pending records the policy model, not the request" "$(jq -r '.pending[0].req + "|" + .pending[0].final' "$T/state/$SG.json")" "|opus"
tojson '.correctionMode="deny"'
ok "F-28: correctionMode deny: missing model is denied with the correction again" "$(run pretool "$(ag2 $SG orch-reviewer 'check it')" | why | grep -c 'model=opus effort=high')" "1"
# 29 win 4: a `[model/effort]` label at the start of the description is kept when the launch has no `effort` (never below the bar, never for another model)
LBL=0; lsess(){ LBL=$((LBL+1)); SL="$S-lbl$LBL"; node $W profile quality --sid=$SL >/dev/null; node $W set maxLaunches=99 maxConcurrent=99 --sid=$SL >/dev/null; }   # fresh session per case: the deny streak guard never interferes
GPR='[wfp] Session policy for default agents: relaunch this same Agent call with effort='   # the pinned quality default bar is opus/high
lsess; ok "label: general-purpose + sonnet + [sonnet/high], no effort -> effort=xhigh from the label (the default bar is opus/high)" "$(run pretool "$(ag2 $SL general-purpose '[sonnet/xhigh] do it' sonnet -)" | why)" "${GPR}xhigh (effort kept from the description label). No need to ask the user."
lsess; ok "label: [sonnet/low] stays below the bar: the bar's effort=high, no label text" "$(run pretool "$(ag2 $SL general-purpose '[sonnet/low] do it' sonnet -)" | why)" "${GPR}high. No need to ask the user."
lsess; ok "label: [sonnet/high] equals the bar: the bar's effort=high, no label text" "$(run pretool "$(ag2 $SL general-purpose '[sonnet/high] do it' sonnet -)" | why)" "${GPR}high. No need to ask the user."
lsess; ok "label: a label naming another model (opus) is ignored: the bar's effort=high" "$(run pretool "$(ag2 $SL general-purpose '[opus/max] do it' sonnet -)" | why)" "${GPR}high. No need to ask the user."
lsess; ok "label: an unknown label model or effort is ignored" "$(run pretool "$(ag2 $SL general-purpose '[fable/max] do it' sonnet -)" | why | grep -c 'effort=high\.')|$(run pretool "$(ag2 $SL general-purpose '[sonnet/ultra] do it' sonnet -)" | why | grep -c 'effort=high\.')" "1|1"
lsess; ok "label: a full model id (claude-sonnet-5-5) matches the label model by rank" "$(run pretool "$(ag2 $SL general-purpose '[sonnet/xhigh] do it' claude-sonnet-5-5 -)" | why | grep -c 'effort=xhigh (effort kept')" "1"
lsess; ok "label: only a label at the START of the description counts" "$(run pretool "$(ag2 $SL general-purpose 'do it [sonnet/xhigh]' sonnet -)" | why | grep -c 'effort=high\.')" "1"
lsess; ok "label: an explicit effort wins over the label (launch passes untouched)" "$(run pretool "$(ag2 $SL general-purpose '[sonnet/xhigh] do it' sonnet low)")" ""
lsess; ok "label: no label, no effort: unchanged (the bar's effort=high)" "$(run pretool "$(ag2 $SL general-purpose 'do it' sonnet -)" | why)" "${GPR}high. No need to ask the user."
lsess; ok "label: above-bar model with no effort stays untouched even with a label (opus builder, bar sonnet/high)" "$(run pretool "$(ag2 $SL orch-builder '[opus/max] build it' opus -)")" ""
lsess; ok "label: a missing model is filled and the label effort above the bar is kept (orch-reviewer bar opus/high, [opus/max])" "$(run pretool "$(ag2 $SL orch-reviewer '[opus/max] check it')" | why | grep -c 'model=opus effort=max (effort kept')" "1"
lsess; ok "label: a missing model filled with a different model than the label: the label is ignored (bar's effort=high)" "$(run pretool "$(ag2 $SL orch-reviewer '[sonnet/max] check it')" | why | grep -c 'model=opus effort=high\.')" "1"
lsess; node $W set effort.default=low --sid=$SL >/dev/null; ok "label: a session 'set effort.default=low' stays ahead of the label" "$(run pretool "$(ag2 $SL general-purpose '[sonnet/xhigh] do it' sonnet -)" | why)" "${GPR}low. No need to ask the user."
lsess; ok "label: the corrected relaunch (sonnet, effort=xhigh) passes" "$(run pretool "$(ag2 $SL general-purpose '[sonnet/xhigh] do it' sonnet -)" >/dev/null; run pretool "$(ag2 $SL general-purpose '[sonnet/xhigh] do it' sonnet xhigh)")" ""
tojson '.correctionMode="updatedInput"'
lsess; o=$(run pretool "$(ag2 $SL general-purpose '[sonnet/xhigh] do it' sonnet -)")
ok "label (updatedInput): effort=xhigh rewritten, model kept, note names the label" "$(echo "$o" | jq -r '(.hookSpecificOutput.updatedInput.model + " " + .hookSpecificOutput.updatedInput.effort + " | " + .hookSpecificOutput.additionalContext)')" "sonnet xhigh | [wfp] Session policy for default agents: this launch was rewritten to effort=xhigh (effort kept from the description label)."
lsess; ok "label (updatedInput): [sonnet/low] gives the bar's effort=high" "$(run pretool "$(ag2 $SL general-purpose '[sonnet/low] do it' sonnet -)" | jq -r '.hookSpecificOutput.updatedInput.effort')" "high"
tojson '.correctionMode="deny"'
ok "F-28: config ships correctionMode updatedInput (reads the shipped file so a scratch overlay still tests behaviour)" "$(jq -r .correctionMode "$CFG")" "updatedInput"
# A-06: a named Agent call is denied (it would spawn a teammate) unless the session has agentTeam=true
nm(){ echo '{"session_id":"'$1'","hook_event_name":"PreToolUse","tool_name":"Agent","tool_input":{"subagent_type":"orch-builder","description":"build it","prompt":"p","name":"w1","model":"sonnet","effort":"high"}}'; }
SN="$S-nm"; node $W profile quality --sid=$SN >/dev/null; node $W set maxLaunches=99 maxConcurrent=99 --sid=$SN >/dev/null
ok "A-06: named launch denied" "$(run pretool "$(nm $SN)" | dec)" "deny"
for n in 1 2 3 4; do run pretool "$(nm $SN)" >/dev/null; done
ok "A-06: named launch still denied on the 5th try (ignores the streak)" "$(run pretool "$(nm $SN)" | dec)" "deny"
ok "A-06: deny text says to remove name or get an agent-team row approved" "$(run pretool "$(nm $SN)" | why | grep -c 'without `name`.*agent-team row.*set agentTeam=true')" "1"
ok "A-06: an unnamed launch is unaffected" "$(run pretool "$(ag2 $SN orch-builder 'build it' sonnet high)")" ""
ok "A-06: a blank name is treated as unnamed" "$(run pretool "$(nm $SN | sed 's/"name":"w1"/"name":""/')")" ""
node $W set agentTeam=true --sid=$SN >/dev/null
ok "A-06: named launch passes with agentTeam=true" "$(run pretool "$(nm $SN)")" ""
ok "A-06: a named launch does not count as a launch" "$(jq -r .launches "$T/state/$SN.json")" "3"
SN2="$S-nm2"; ok "A-06: denied even before a policy exists" "$(run pretool "$(nm $SN2)" | dec)" "deny"
# A-08: an unmapped type is judged on subagent_type alone; byDescription stays the one description route
SR="$S-rl"; node $W profile quality --sid=$SR >/dev/null; node $W set maxLaunches=99 maxConcurrent=99 --sid=$SR >/dev/null
ok "A-08: Explore 'Find the plan file' is discovery (haiku), not planning" "$(run pretool "$(ag2 $SR Explore 'Find the plan file')" | mdl)" "haiku"
SR2="$S-rl2"; node $W profile quality --sid=$SR2 >/dev/null; node $W set maxLaunches=99 maxConcurrent=99 --sid=$SR2 >/dev/null
ok "A-08: general-purpose 'implement the plan' stays default (opus), not implementation" "$(run pretool "$(ag2 $SR2 general-purpose 'implement the plan')" | mdl)" "opus"
SR3="$S-rl3"; node $W profile quality --sid=$SR3 >/dev/null; node $W set maxLaunches=99 maxConcurrent=99 --sid=$SR3 >/dev/null
ok "A-08: an orch-scout described 'debug the hook' is still diagnosis via byDescription" "$(run pretool "$(ag2 $SR3 orch-scout 'debug the hook')" | why | grep -c 'model=sonnet effort=high')" "1"
# 20 config sanity: every role is a known model/effort; ultracode never lowers a bar
inv='const c=require(process.argv[1]),E=["low","medium","high","xhigh","max"],M=["haiku","sonnet","opus"],bad=[],r=s=>[M.indexOf(s.model),E.indexOf(s.effort)];
for(const[p,pr]of Object.entries(c.profiles))for(const[k,v]of Object.entries(pr.roles||{})){const x=r(v);if(x[0]<0||x[1]<0)bad.push(p+"."+k+"=?")}
for(const[p,u]of Object.entries(c.ultracodeRaise))for(const[k,v]of Object.entries(c.profiles[p].roles)){const a=r(v),h=r(c.profiles[u].roles[k]);if(h[0]<a[0]||(h[0]===a[0]&&h[1]<a[1]))bad.push(p+"->"+u+"."+k)}
console.log(bad.length?bad.join(","):"ok")'
node -e "console.log(JSON.stringify(require('$SRC/lib/paths.js').loadConfig('wfp').cfg))" > "$T/merged.json"
ok "config: valid steps, raise never lowers a bar" "$(node -e "$inv" "$T/merged.json")" "ok"
# 21 shipped quality preset (unpinned): its bars and escalate steps; tuning the shipped defaults must fail loudly here
TL=$(mktemp -d); echo '{"defaultProfile": null, "correctionMode": "deny"}' > "$TL/wfp.json"; mkdir -p "$TL/state"
WL="$W"; SLV="$S-live"; tl(){ BOSS_DATA="$TL" WFP_STATE_DIR="$TL/state" node $WL "$@"; }
tl profile quality --sid=$SLV >/dev/null
lbar(){ tl escalate "$1" --json --sid=$SLV | jq -r '.bar.model + "/" + .bar.effort'; }
lstep(){ tl escalate "$1" "$2" --json --sid=$SLV | jq -r '.to.model + "/" + .to.effort'; }
for pair in planning:opus/medium plan-check:opus/medium review:opus/medium default:opus/medium diagnosis:sonnet/medium implementation:sonnet/high discovery:haiku/medium; do
  ok "shipped quality bar ${pair%%:*} = ${pair#*:}" "$(lbar ${pair%%:*})" "${pair#*:}"
done
ok "shipped escalate implementation sonnet/high -> opus/high" "$(lstep implementation sonnet/high)" "opus/high"
ok "shipped escalate diagnosis sonnet/medium -> opus/medium" "$(lstep diagnosis sonnet/medium)" "opus/medium"
ok "shipped escalate review opus/medium -> opus/high" "$(lstep review opus/medium)" "opus/high"
ok "shipped escalate planning opus/medium -> opus/high" "$(lstep planning opus/medium)" "opus/high"
BOSS_DATA="$TL" node -e "console.log(JSON.stringify(require('$SRC/lib/paths.js').loadConfig('wfp').cfg))" > "$TL/merged.json"
ok "shipped config: valid steps, raise never lowers a bar" "$(node -e "$inv" "$TL/merged.json")" "ok"
rm -f $TL/state/$S*; rm -rf "$TL"
# 29 plugin-root contract: state under the data dir, user default in wfp.json, plugin dir untouched, plugin-prefixed agents
SPX="$S-px"; ST="$T/state"
run telemetry "${tel//$S/$SPX}"
ok "state lands under WFP_STATE_DIR" "$([ -e "$ST/$SPX.tel.json" ] && echo yes || echo no)" "yes"
ok "no wfp state in the plugin dir" "$(find "$SRC/wfp" -name '*.tel.json' -o -name 'errors.log' -o -type d -name state -o -type d -name handoffs | wc -l | tr -d ' ')" "0"
rm -f "$T/wfp.json"
ok "default: none shipped" "$(node $W default | grep -c 'default profile none')" "1"
node $W default balanced >/dev/null
ok "default balanced writes <data>/wfp.json" "$(jq -r .defaultProfile "$T/wfp.json")" "balanced"
echo '{"keep": 1, "defaultProfile": null}' > "$T/wfp.json"; node $W default quality >/dev/null
ok "default keeps other overlay keys" "$(jq -c '[.keep, .defaultProfile]' "$T/wfp.json")" '[1,"quality"]'
echo '{ nope' > "$T/wfp.json"; dout=$(node $W default max 2>&1)
ok "default refuses an overlay that does not parse" "$(echo "$dout" | grep -c 'does not parse')" "1"
ok "default left the broken overlay as it was" "$(cat "$T/wfp.json")" "{ nope"
rm -f "$T/wfp.json"; printf '{"defaultProfile": null, "correctionMode": "deny"}' > "$T/wfp.json"
ho=$(node $W handoff --sid=$SPX); ok "handoff written under <data>/wfp/handoffs" "$(case "$ho" in "$T"/wfp/handoffs/*.md) [ -f "$ho" ] && echo yes;; esac)" "yes"
SB="$S-bx"; node $W profile quality --sid=$SB >/dev/null; node $W set maxLaunches=99 maxConcurrent=99 --sid=$SB >/dev/null
ok "boss:orch-builder routes to implementation (sonnet/high)" "$(run pretool "$(ag2 $SB boss:orch-builder 'build row 3')" | why | grep -c 'model=sonnet effort=high')" "1"
ok "boss:orch-scout debug description is diagnosis via byDescription" "$(run pretool "$(ag2 $SB boss:orch-scout 'debug the hook')" | why | grep -c 'model=sonnet effort=medium')" "1"
ok "escalate boss:orch-scout uses the base role" "$(node $W escalate boss:orch-scout --json --sid=$SB | jq -r .role)" "discovery"
SBP="$S-bp"; node $W profile economy --sid=$SBP >/dev/null; node $W set maxLaunches=50 maxConcurrent=99 --sid=$SBP >/dev/null
run pretool "$(ag2 $SBP boss:orch-scout 'scan' haiku)" >/dev/null
ok "prefixed launch records the base type in pending" "$(jq -r '.pending[0].type' "$ST/$SBP.json")" "orch-scout"
printf '%s' '{"session_id":"'$SBP'","agent_id":"pfx1","agent_type":"boss:orch-scout"}' | $N $W substart
ok "prefixed substart marks the pending launch started" "$(jq -r '(.pending[0].started != null | tostring) + "|" + .active.pfx1.type' "$ST/$SBP.json")" "true|orch-scout"
printf '%s' '{"session_id":"'$SBP'","agent_id":"pfx1","agent_type":"boss:orch-scout"}' | $N $W substop
ok "prefixed substop consumes the pending entry by type" "$(jq -r '(.pending | length | tostring) + "|" + .agents[-1].type' "$ST/$SBP.json")" "0|orch-scout"
AFTER=$(find "$SRC" -type f | sort | xargs cksum | cksum)
ok "the plugin tree is byte-identical after the whole run" "$AFTER" "$BEFORE"
rm -f $T/state/$S*; rm -rf "$T"; echo "pass=$pass fail=$fail"; [ "$fail" -eq 0 ]
