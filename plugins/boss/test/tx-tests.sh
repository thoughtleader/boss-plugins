#!/usr/bin/env bash
# Transcript loader tests (metrics-dashboard run, row 2): dedupe (session_id, message.id) keeping max output_tokens, cross-session dup guard,
# incremental resume by byte offset (append, partial tail, shrink), report never loads transcripts, sync --no-transcripts, per-file counters,
# run attribution of main-chat calls, subagent -> attempt (task_id / toolUseId / description), weights, wfp snapshots.
# Scratch ORCH_* dirs only. Usage: bash ~/.claude/orchestrator/test/tx-tests.sh   (exit 0 = all pass)
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib/compat.sh"   # on macOS: GNU date/stat/touch/md5sum/wc shims (test/lib)
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
L="$CL/lib/ledger.js"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export ORCH_WFP_STATE="$T/wfp" ORCH_DIR="$T/orch" ORCH_RUNS_DIR="$T/orch/runs" ORCH_PROJECTS_DIR="$T/projects" ORCH_METRICS_DB="$T/metrics.db" ORCH_SETTINGS="$T/settings.json" ORCH_COMMIT_HOME="$T/nohome"
unset WFP_SID BOSS_PROGRESS_DIR; mkdir -p "$ORCH_RUNS_DIR/r1" "$ORCH_RUNS_DIR/r2" "$ORCH_WFP_STATE"
DB="$ORCH_METRICS_DB"; q(){ sqlite3 "$DB" "$1"; }
P="$ORCH_PROJECTS_DIR/-slug"; LATTE="$ORCH_PROJECTS_DIR/-home-user--claude-double-shot-latte" OBS="$ORCH_PROJECTS_DIR/-home-user--claude-mem-observer-sessions" OCTO="$ORCH_PROJECTS_DIR/-tmp-octopus-consultative-1-workspace"
mkdir -p "$P/S1/subagents/workflows/wf_1" "$P/S2" "$LATTE" "$OBS" "$OCTO"
MAIN="$P/S1.jsonl"

# a: assistant line  ts id out [model] [cw] [cw1h] [content-json] [sid] [agentId]
a(){ jq -cn --arg ts "$1" --arg id "$2" --argjson out "$3" --arg model "${4:-claude-opus-5-5}" --argjson cw "${5:-0}" --argjson h "${6:-0}" \
  --argjson content "${7:-[]}" --arg sid "${8:-S1}" --arg ag "${9:-}" \
  '{type:"assistant",sessionId:$sid,timestamp:$ts,effort:"high",perTurnEffort:"medium",requestId:("req_"+$id)} + (if $ag != "" then {agentId:$ag,isSidechain:true} else {} end)
   + {message:{id:$id,model:$model,content:$content,usage:{input_tokens:1000,output_tokens:$out,cache_read_input_tokens:2000,cache_creation_input_tokens:$cw,
     cache_creation:{ephemeral_1h_input_tokens:$h,ephemeral_5m_input_tokens:($cw-$h)}}}}'; }
u(){ jq -cn --arg ts "$1" --arg t "$2" '{type:"user",timestamp:$ts,message:{role:"user",content:$t}}'; }
AGENT='[{"type":"tool_use","id":"tu1","name":"Agent","input":{"description":"[sonnet/high] r1#1.1 do it"}},{"type":"tool_use","id":"tu2","name":"Agent","input":{"description":"look around"}},{"type":"tool_use","id":"tu3","name":"Edit","input":{"file_path":"/x/PLAN.md"}}]'
{ u 2026-10-01T00:00:00.000Z "build the thing"
  a 2026-10-01T00:00:10.000Z msg_A 10 claude-opus-5-5 500 200 "$AGENT"
  a 2026-10-01T00:00:10.500Z msg_A 50 claude-opus-5-5 500 200
  a 2026-10-01T00:00:11.000Z msg_A 30 claude-opus-5-5 500 200
  a 2026-10-01T00:00:12.000Z msg_syn 5 "<synthetic>"
  echo '{"type":"user","timestamp":"2026-10-01T00:00:13.000Z","message":{"content":[{"type":"tool_result","tool_use_id":"x","content":"[main abc1234] feat: x\n 1 file changed, 2 insertions(+)"}]}}'
  echo '{"type":"system","subtype":"compact_boundary","timestamp":"2026-10-01T00:00:14.000Z"}'
  u 2026-10-01T00:30:00.000Z "now the second thing"          # gap > 15 min: not active
  a 2026-10-01T00:30:05.000Z msg_B 20 claude-sonnet-5-5 ; } > "$MAIN"
# run events: S1 drives r1 from 00:00:05, then r2 from 00:30:01. Subagents: agentX (stop task_id), agentY (start tool_use_id only), agentZ (description only), agentW (none)
mkdir -p "$ORCH_RUNS_DIR/r1" "$ORCH_RUNS_DIR/r2"
jq -cn '{id:"r1#e1",seq:1,type:"launch",ts:"2026-10-01T00:00:05.000Z",run:"r1",attempt:"r1#1.1",row:"1",session:"S1"}' > "$ORCH_RUNS_DIR/r1/manifest.jsonl"
jq -cn '{id:"r1#e2",seq:2,type:"start",ts:"2026-10-01T00:00:06.000Z",run:"r1",attempt:"r1#1.1",row:"1",session:"S1",tool_use_id:"toolu_Y"}' >> "$ORCH_RUNS_DIR/r1/manifest.jsonl"
jq -cn '{id:"r1#e3",seq:3,type:"stop",ts:"2026-10-01T00:00:20.000Z",run:"r1",attempt:"r1#2.1",row:"2",session:"S1",task_id:"agentX"}' >> "$ORCH_RUNS_DIR/r1/manifest.jsonl"
jq -cn '{id:"r2#e1",seq:1,type:"launch",ts:"2026-10-01T00:30:01.000Z",run:"r2",attempt:"r2#1.1",row:"1",session:"S1"}' > "$ORCH_RUNS_DIR/r2/manifest.jsonl"
at(){ jq -cn --arg id "$1" --arg run "$2" --arg row "$3" --arg role "$4" '{ts:"2026-10-01T00:00:01.000Z",id:$id,run:$run,row:($row|tonumber),role:$role,agent:"orch-builder",model:"sonnet",effort:"high",mode:"subagent",tokens:1000,tools:1,ms:1,repo:"r",area:"a"}'; }
{ at r1#1.1 r1 1 implementation; at r1#2.1 r1 2 review; at r1#3.1 r1 3 planning; at r2#1.1 r2 1 implementation; } > /dev/null
{ at r1#1.1 r1 1 implementation; at r1#2.1 r1 2 review; at r1#3.1 r1 3 planning; } > "$ORCH_RUNS_DIR/r1/ledger.jsonl"; at r2#1.1 r2 1 implementation > "$ORCH_RUNS_DIR/r2/ledger.jsonl"
SUB="$P/S1/subagents"
{ a 2026-10-01T00:00:21.000Z msg_X1 100 claude-sonnet-5-5 0 0 '[]' S1 agentX; a 2026-10-01T00:00:22.000Z msg_X2 200 claude-sonnet-5-5 0 0 '[]' S1 agentX; } > "$SUB/agent-agentX.jsonl"
echo '{"agentType":"orch-builder","description":"[sonnet/high] r1#2.1 review it","toolUseId":"toolu_X"}' > "$SUB/agent-agentX.meta.json"
a 2026-10-01T00:00:23.000Z msg_Y1 40 claude-haiku-4-5 0 0 '[]' S1 agentY > "$SUB/agent-agentY.jsonl"
echo '{"agentType":"orch-builder","description":"whatever","toolUseId":"toolu_Y"}' > "$SUB/agent-agentY.meta.json"
a 2026-10-01T00:00:24.000Z msg_Z1 60 claude-sonnet-5-5 0 0 '[]' S1 agentZ > "$SUB/agent-agentZ.jsonl"
echo '{"agentType":"orch-builder","description":"[sonnet/high] r1#3.1 plan it","toolUseId":"toolu_Z"}' > "$SUB/agent-agentZ.meta.json"
a 2026-10-01T00:00:25.000Z msg_W1 70 claude-sonnet-5-5 0 0 '[]' S1 agentW > "$SUB/workflows/wf_1/agent-agentW.jsonl"
a 2026-10-01T00:00:26.000Z msg_B 20 claude-sonnet-5-5 0 0 '[]' S2 > "$P/S2.jsonl"                  # a resumed copy of S1's msg_B under another session
a 2026-10-01T00:00:27.000Z msg_L 999 claude-haiku-4-5 0 0 '[{"type":"tool_use","id":"tuL","name":"Agent","input":{"description":"look"}}]' L > "$LATTE/L.jsonl"                    # helper dir (schema 6): loaded, tagged helper
a 2026-10-01T00:00:28.000Z msg_O 30 claude-sonnet-5-5 900 600 '[]' O > "$OBS/O.jsonl"                       # helper dir: cache writes 600 1h + 300 5m
a 2026-10-01T00:00:29.000Z msg_T 5 claude-sonnet-5-5 0 0 '[]' T > "$OCTO/T.jsonl"                          # tmp-octopus: still skipped
jq -cn '{ts:1792611510,source:"transcript",cwd:"/w",ctxTokens:5000,window:null,usedPct:null,model:"claude-opus-5-5",effort:null,apiEquivUsd:null,fiveHourPct:null,sevenDayPct:null}' > "$ORCH_WFP_STATE/SID1.tel.json"

echo "== report does not load transcripts; sync --no-transcripts neither"
o=$(node "$L" report first-pass 2>&1); st=$?
ok "report exits 0" "$st" "0"
ok "report left tx_files and tx_calls empty" "$(q "SELECT (SELECT COUNT(*) FROM tx_files)||' '||(SELECT COUNT(*) FROM tx_calls)||' '||(SELECT COUNT(*) FROM wfp_telemetry)")" "0 0 0"
o=$(node "$L" sync --no-transcripts 2>&1); ok "sync --no-transcripts loads nothing, prints no transcripts line" "$(q "SELECT COUNT(*) FROM tx_calls")|$(echo "$o" | grep -c 'transcripts:')" "0|0"

echo "== first sync: dedupe, skip dirs, counters"
o=$(node "$L" sync 2>"$T/err"); st=$?
ok "sync exits 0, stderr empty" "$st|$(cat "$T/err")" "0|"
ok "files: 1 main + 4 sub/wf + S2 main read (helper files apart); tmp-octopus skipped" "$(q "SELECT kind||'='||COUNT(*) FROM tx_files WHERE kind <> 'wfp_tel' AND helper = 0 GROUP BY kind ORDER BY kind" | tr '\n' ' ')|$(q "SELECT COUNT(*) FROM tx_files WHERE dir LIKE '%octopus%'")" "main=2 sub=3 wf=1 |0"
ok "helper folders loaded and tagged: latte + observer files and calls helper 1, nothing else tagged" "$(q "SELECT group_concat(session_id||':'||helper) FROM (SELECT * FROM tx_files WHERE helper = 1 ORDER BY session_id)")|$(q "SELECT group_concat(message_id) FROM (SELECT message_id FROM tx_calls WHERE helper = 1 ORDER BY message_id)")|$(q "SELECT COUNT(*) FROM tx_calls WHERE helper = 1 AND message_id NOT IN ('msg_L','msg_O')")" "L:1,O:1|msg_L,msg_O|0"
ok "helper calls carry no inline_n / agent_n (latte's Agent tool_use not counted) and stay out of v_tx_call_w, v_tx_call_run, v_tx_call_attr, v_tx_sessions" "$(q "SELECT coalesce(inline_n,'null')||'/'||coalesce(agent_n,'null') FROM tx_calls WHERE message_id = 'msg_L'")|$(q "SELECT (SELECT COUNT(*) FROM v_tx_call_w WHERE message_id IN ('msg_L','msg_O'))+(SELECT COUNT(*) FROM v_tx_call_run WHERE message_id IN ('msg_L','msg_O'))+(SELECT COUNT(*) FROM v_tx_call_attr WHERE message_id IN ('msg_L','msg_O'))+(SELECT COUNT(*) FROM v_tx_sessions WHERE session_id IN ('L','O'))")" "null/null|0"
ok "v_tx_helper: per folder, cache writes split 5m / 1h" "$(q "SELECT group_concat(dir||' '||model||' '||calls||' '||cw_5m||' '||cw_1h, ',') FROM (SELECT * FROM v_tx_helper ORDER BY dir)")" "-home-user--claude-double-shot-latte claude-haiku-4-5 1 0 0,-home-user--claude-mem-observer-sessions claude-sonnet-5-5 1 300 600"
ok "msg_A written 3 times: one row, max output_tokens (50, not the last 30)" "$(q "SELECT COUNT(*)||' '||max(output_tokens) FROM tx_calls WHERE message_id='msg_A'")" "1 50"
ok "synthetic skipped, skipped dir skipped" "$(q "SELECT COUNT(*) FROM tx_calls WHERE message_id IN ('msg_syn','msg_T')")" "0"
ok "cross-session copy of msg_B not double-counted (dup_ids 1, session S1 keeps it)" "$(q "SELECT COUNT(*)||' '||max(session_id) FROM tx_calls WHERE message_id='msg_B'")|$(q "SELECT dup_ids FROM tx_sync_log ORDER BY id DESC LIMIT 1")" "1 S1|1"
ok "call columns: cw_1h, effort (perTurnEffort), is_sidechain, agent_id from line" "$(q "SELECT cw_1h||' '||cache_creation_input_tokens||' '||effort||' '||is_sidechain||' '||ifnull(agent_id,'-') FROM tx_calls WHERE message_id='msg_A'")|$(q "SELECT agent_id||' '||is_sidechain FROM tx_calls WHERE message_id='msg_W1'")" "200 500 medium 0 -|agentW 1"
ok "S1 counters: 2 owner turns, 2 Agent calls (1 orch), 1 compaction, 4 subs; kind orch" "$(q "SELECT human_turns||' '||agent_calls||' '||orch_calls||' '||compactions||' '||n_subagents||' '||kind FROM v_tx_sessions WHERE session_id='S1'")" "2 2 1 1 4 orch"
ok "active_ms: gaps under 15 min only (timestamps of parsed lines 0:10..0:14 plus the 5 s after the 30 min gap)" "$(q "SELECT active_ms FROM v_tx_sessions WHERE session_id='S1'")" "19000"
ok "SHA seen in a commit tool_result; edit counted" "$(q "SELECT group_concat(sha) FROM tx_shas")|$(q "SELECT file_path||' '||n FROM tx_edits")" "abc1234|/x/PLAN.md 1"
ok "tx_agents from meta.json (attempt parsed from the description)" "$(q "SELECT agent_type||' '||tool_use_id||' '||attempt FROM tx_agents WHERE agent_id='agentX'")" "orch-builder toolu_X r1#2.1"
ok "wfp snapshot loaded as is" "$(q "SELECT session_id||' '||ts||' '||ctx_tokens||' '||tel_source FROM wfp_telemetry")" "SID1 1792611510 5000 transcript"

echo "== attribution"
ok "(a) main calls by latest run event at or before ts: msg_A (00:10, after r1's launch) -> r1, msg_B (00:30:05, after r2's launch) -> r2" "$(q "SELECT group_concat(message_id||'>'||run) FROM (SELECT * FROM v_tx_call_run ORDER BY message_id)")" "msg_A>r1,msg_B>r2"
ok "(b) agent -> attempt: task_id (X), toolUseId (Y), description (Z); W unattributed" "$(q "SELECT group_concat(agent_id||'>'||attempt||'/'||via) FROM (SELECT * FROM v_tx_agent_attempt ORDER BY agent_id)")" "agentX>r1#2.1/1,agentY>r1#1.1/2,agentZ>r1#3.1/3"
ok "(b) per-attempt subagent tokens" "$(q "SELECT group_concat(attempt||'='||tokens) FROM (SELECT * FROM v_tx_attempt_totals ORDER BY attempt)")" "r1#1.1=3040,r1#2.1=6300,r1#3.1=3060"
ok "(c) per-run totals = main + attributed subagents (msg_W1 is unattributed)" "$(q "SELECT run||' main '||main_calls||'/'||main_tokens||' sub '||sub_calls||'/'||sub_tokens FROM v_tx_run_totals WHERE run='r1'")" "r1 main 1/3550 sub 4/12400"
ok "(c) r2 has the main call only" "$(q "SELECT run||' '||calls||' '||tokens FROM v_tx_run_totals WHERE run='r2'")" "r2 1 3020"
ok "(d) by role: main, implementation, review, planning, unattributed" "$(q "SELECT group_concat(role||'='||calls) FROM (SELECT * FROM v_tx_by_role ORDER BY role)")" "implementation=1,main=2,planning=1,review=2,unattributed=1"
ok "(d) by model/effort uses the call's own effort" "$(q "SELECT group_concat(model||'/'||effort||'='||calls) FROM (SELECT * FROM v_tx_by_model_effort ORDER BY model)")" "claude-haiku-4-5/medium=1,claude-opus-5-5/medium=1,claude-sonnet-5-5/medium=5"
ok "unattributed by agent type: the Workflow agent has no meta" "$(q "SELECT agent_type||' '||calls FROM v_tx_unattributed")" "? 1"
# weight: msg_A = in 1000, out 50, cr 2000, cw 500 (1h 200): (4000 + 1000 + 400 + 300*5 + 200*8)/4 = 2125
ok "weight in Opus-input units (cw5 = cw - cw1h)" "$(q "SELECT units FROM v_tx_call_w WHERE message_id='msg_A'")" "2125.0"

echo "== prices (schema 6): Sonnet cache read 0.05x input, Fable + Mythos on the fable row, advisor tokens from usage.iterations"
# msg_B (sonnet) = in 1000, out 20, cr 2000: (2000 + 200 + 2000*0.10)/4 = 600 (650 at the old cr_p 0.20)
ok "Sonnet cache read priced at 0.05x input (cr_p 0.10 of in_p 2)" "$(q "SELECT cr_p/in_p FROM prices WHERE family='sonnet'")|$(q "SELECT units FROM v_tx_call_w WHERE message_id='msg_B'")" "0.05|600.0"
S5="$P/S5.jsonl"
ADV='[{"type":"message","input_tokens":1000,"output_tokens":40},{"type":"advisor_message","model":"claude-opus-5-5","input_tokens":100,"output_tokens":20,"cache_read_input_tokens":300,"cache_creation_input_tokens":50,"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":50}}]'
{ a 2026-10-01T01:00:00.000Z msg_F 10 claude-fable-5-2 0 0 '[]' S5
  a 2026-10-01T01:00:01.000Z msg_M 10 claude-mythos-5-2 0 0 '[]' S5
  a 2026-10-01T01:00:02.000Z msg_V 5 claude-sonnet-5-5 0 0 '[]' S5
  a 2026-10-01T01:00:03.000Z msg_V 40 claude-sonnet-5-5 0 0 '[]' S5 | jq -c --argjson it "$ADV" '.message.usage.iterations = $it'
  a 2026-10-01T01:00:04.000Z msg_V 40 claude-sonnet-5-5 0 0 '[]' S5; } > "$S5"
node "$L" sync >/dev/null 2>&1
# fable = in 1000*8 + out 10*40 + cr 2000*0.20 = 8800 / 4 = 2200 (1150 as opus)
ok "Fable and Mythos on the fable row (8, 40, 0.20, 10, 16)" "$(q "SELECT group_concat(message_id||'='||family||'/'||units) FROM (SELECT * FROM v_tx_call_w WHERE message_id IN ('msg_F','msg_M') ORDER BY message_id)")|$(q "SELECT in_p||','||out_p||','||cr_p||','||cw5_p||','||cw1h_p FROM prices WHERE family='fable'")" "msg_F=fable/2200.0,msg_M=fable/2200.0|8.0,40.0,0.2,10.0,16.0"
# advisor = in 100*4 + out 20*20 + cr 300*0.20 + cw1h 50*8 = 1260 / 4 = 315; the executor's own top-level usage stays (2000 + 400 + 200)/4 = 650
ok "usage.iterations counted: advisor sub-call stored apart (one row, max over the repeated lines), top-level usage untouched" "$(q "SELECT adv_n||' '||adv_model||' '||adv_in||'/'||adv_out||'/'||adv_cr||'/'||adv_cw||'/'||adv_cw1h||' out '||output_tokens FROM tx_calls WHERE message_id='msg_V'")|$(q "SELECT units||' '||adv_family||' '||adv_tokens||' '||adv_units FROM v_tx_call_w WHERE message_id='msg_V'")" "1 claude-opus-5-5 100/20/300/50/50 out 40|650.0 opus 470 315.0"
ok "v_tx_advisor groups by executor family and advisor model; no adv on other calls" "$(q "SELECT family||' '||adv_model||' '||calls||' '||adv_calls||' '||adv_units FROM v_tx_advisor")|$(q "SELECT COUNT(*) FROM tx_calls WHERE adv_n IS NOT NULL")" "sonnet claude-opus-5-5 1 1 315.0|1"
q "UPDATE tx_calls SET adv_n = NULL, adv_model = NULL, adv_in = NULL, adv_out = NULL, adv_cr = NULL, adv_cw = NULL, adv_cw1h = NULL; UPDATE tx_files SET adv_v = 0"
o=$(node "$L" sync 2>&1)
ok "advisor backfill: a file loaded before schema 6 (adv_v 0) is re-read for adv_* only, and flagged" "$(q "SELECT adv_n||' '||adv_in||'/'||adv_out||'/'||adv_cr||'/'||adv_cw||'/'||adv_cw1h FROM tx_calls WHERE message_id='msg_V'")|$(q "SELECT COUNT(*) FROM tx_files WHERE kind <> 'wfp_tel' AND adv_v = 0")|$(q "SELECT COUNT(*) FROM tx_calls")" "1 100/20/300/50/50|0|$(q "SELECT COUNT(*) FROM tx_calls")"

echo "== reseed-prices: own command, --dry-run, idempotent (a copy of the db with the schema-5 prices)"
RS="$T/reseed.db"; cp "$DB" "$RS"; sqlite3 "$RS" "UPDATE prices SET cr_p = 0.20 WHERE family = 'sonnet'; DELETE FROM prices WHERE family = 'fable'"
RS0=$(md5sum < "$RS")
o=$(node "$CL/lib/metrics.js" reseed-prices --dry-run --db "$RS" 2>&1); st=$?
ok "dry run: exit 0, old -> new per family, db unchanged" "$st|$(echo "$o" | grep -c '(dry run).*2 families would change')|$(echo "$o" | grep -c '^  sonnet: cr_p 0.2 -> 0.1$')|$(echo "$o" | grep -c '^  fable (new): in_p - -> 8, out_p - -> 40, cr_p - -> 0.2, cw5_p - -> 10, cw1h_p - -> 16$')|$(md5sum < "$RS")" "0|1|1|1|$RS0"
o=$(node "$CL/lib/metrics.js" reseed-prices --db "$RS" 2>&1); st=$?
ok "real run: only the differing rows change (opus and haiku untouched)" "$st|$(echo "$o" | grep -c '2 families changed')|$(sqlite3 "$RS" "SELECT group_concat(family||'='||in_p||','||out_p||','||cr_p||','||cw5_p||','||cw1h_p, ' ') FROM (SELECT * FROM prices ORDER BY family)")" "0|1|fable=8.0,40.0,0.2,10.0,16.0 haiku=0.1,0.5,0.01,0.125,0.2 opus=4.0,20.0,0.2,5.0,8.0 sonnet=2.0,10.0,0.1,2.5,4.0"
RS1=$(md5sum < "$RS"); o=$(node "$CL/lib/metrics.js" reseed-prices --db "$RS" 2>&1); st=$?
ok "second run changes nothing (says so, file unchanged)" "$st|$(echo "$o" | grep -c 'no change, every family matches')|$(md5sum < "$RS")" "0|1|$RS1"
ok "default db is \$ORCH_METRICS_DB; --json; bad verb exit 2; missing db exit 1" "$(node "$CL/lib/metrics.js" reseed-prices --dry-run --json | jq -c '[.db == env.ORCH_METRICS_DB, (.changes|length)]')|$(node "$CL/lib/metrics.js" bogus >/dev/null 2>&1; echo $?)|$(node "$CL/lib/metrics.js" reseed-prices --db "$T/none.db" >/dev/null 2>&1; echo $?)" "[true,0]|2|1"

echo "== idempotent, incremental"
H1=$(sqlite3 "$DB" '.dump tx_calls tx_files tx_agents tx_edits tx_shas wfp_telemetry' | md5sum)
o=$(node "$L" sync 2>&1)
ok "second sync reads 0 files, all unchanged, 0 new calls" "$(echo "$o" | grep -o '[0-9]* files ([0-9]* read [0-9.]* MB, [0-9]* unchanged, [0-9]* reset), calls +[0-9]*')" "9 files (0 read 0.0 MB, 9 unchanged, 0 reset), calls +0"
ok "second sync: same dump hash" "$(sqlite3 "$DB" '.dump tx_calls tx_files tx_agents tx_edits tx_shas wfp_telemetry' | md5sum)" "$H1"
SZ1=$(stat -c %s "$MAIN")
{ a 2026-10-01T00:30:06.000Z msg_A 90 claude-opus-5-5 500 200; a 2026-10-01T00:30:07.000Z msg_C 7 claude-sonnet-5-5; u 2026-10-01T00:30:08.000Z "third"; } >> "$MAIN"
printf '%s' "$(a 2026-10-01T00:30:09.000Z msg_D 8 claude-sonnet-5-5 | head -c 60)" >> "$MAIN"   # a torn last line
o=$(node "$L" sync 2>&1)
ok "append: only the new bytes read; msg_A raised to 90 (a message straddling two reads); msg_C added; counters continue" "$(q "SELECT (SELECT output_tokens FROM tx_calls WHERE message_id='msg_A')||' '||(SELECT COUNT(*) FROM tx_calls WHERE message_id='msg_C')||' '||(SELECT human_turns FROM tx_files WHERE path='$MAIN')")|$(echo "$o" | grep -c '(1 read ')" "90 1 3|1"
ok "torn tail not consumed: offset stops at the last newline, before the torn line" "$(q "SELECT offset FROM tx_files WHERE path='$MAIN'")|$(q "SELECT COUNT(*) FROM tx_calls WHERE message_id='msg_D'")" "$(( $(stat -c %s "$MAIN") - 60 ))|0"
# finish the torn line
head -c $(( $(stat -c %s "$MAIN") - 60 )) "$MAIN" > "$MAIN.tmp" && cat "$MAIN.tmp" > "$MAIN"; rm "$MAIN.tmp"; a 2026-10-01T00:30:09.000Z msg_D 8 claude-sonnet-5-5 >> "$MAIN"
node "$L" sync >/dev/null 2>&1
ok "completed line is picked up from the stored offset" "$(q "SELECT COUNT(*) FROM tx_calls WHERE message_id='msg_D'")|$(q "SELECT offset=size FROM tx_files WHERE path='$MAIN'")" "1|1"
ok "no double count after the resumes: S1 main calls = A,B,C,D" "$(q "SELECT group_concat(message_id) FROM (SELECT message_id FROM tx_calls WHERE session_id='S1' AND is_sidechain=0 ORDER BY message_id)")" "msg_A,msg_B,msg_C,msg_D"
ok "inline counters (schema 4): msg_A has 1 inline tool_use (Edit) and 2 Agent calls; every main file is counted (inline_v 1)" "$(q "SELECT inline_n||'/'||agent_n FROM tx_calls WHERE message_id='msg_A'")|$(q "SELECT COUNT(*) FROM tx_files WHERE kind='main' AND inline_v=0")" "1/2|0"
q "UPDATE tx_calls SET inline_n = NULL, agent_n = NULL; UPDATE tx_files SET inline_v = 0 WHERE kind = 'main'"
node "$L" sync >/dev/null 2>&1
ok "inline backfill recomputes the same counts from the transcript and flags the file" "$(q "SELECT inline_n||'/'||agent_n FROM tx_calls WHERE message_id='msg_A'")|$(q "SELECT COUNT(*) FROM tx_files WHERE kind='main' AND inline_v=0")" "1/2|0"
# shrink: rewritten file is dropped and reloaded whole
a 2026-10-01T00:00:10.000Z msg_A 11 claude-opus-5-5 500 200 > "$MAIN"
o=$(node "$L" sync 2>&1)
ok "a shrunk file is reset and reloaded (msg_A back to 11, msg_B/C/D gone, counters reset)" "$(q "SELECT group_concat(message_id||'='||output_tokens) FROM (SELECT * FROM tx_calls WHERE session_id='S1' AND is_sidechain=0 ORDER BY message_id)")|$(echo "$o" | grep -c '1 reset')|$(q "SELECT human_turns FROM tx_files WHERE path='$MAIN'")" "msg_A=11|1|0"

echo "== rebuild-safe: tx tables survive --rebuild, a schema bump and a deleted transcript (row 4b)"
SNAP="$(q "SELECT COUNT(*)||'/'||SUM(output_tokens)||'/'||(SELECT COUNT(*) FROM tx_files)||'/'||(SELECT COUNT(*) FROM tx_agents)||'/'||(SELECT COUNT(*) FROM tx_shas)||'/'||(SELECT COUNT(*) FROM wfp_telemetry) FROM tx_calls")"
q "UPDATE prices SET in_p = 9.99 WHERE family = (SELECT family FROM prices LIMIT 1)"; PR="$(q "SELECT group_concat(in_p) FROM prices")"
o=$(node "$L" sync --rebuild 2>&1)
ok "--rebuild keeps every tx table (nothing reread: 0 files read) and the edited prices" "$(echo "$o" | grep -c '(rebuild)')|$(echo "$o" | grep -c ', 0 read')|$(q "SELECT COUNT(*)||'/'||SUM(output_tokens)||'/'||(SELECT COUNT(*) FROM tx_files)||'/'||(SELECT COUNT(*) FROM tx_agents)||'/'||(SELECT COUNT(*) FROM tx_shas)||'/'||(SELECT COUNT(*) FROM wfp_telemetry) FROM tx_calls")|$(q "SELECT group_concat(in_p) FROM prices")" "1|0|$SNAP|$PR"
rm -f "$MAIN" "$SUB/agent-agentX.jsonl"
o=$(node "$L" sync --rebuild 2>&1)
ok "a transcript deleted from disk, then --rebuild: its calls, file row, counters and the attempt join survive" "$(q "SELECT COUNT(*)||'/'||SUM(output_tokens) FROM tx_calls")|$(q "SELECT COUNT(*) FROM tx_files WHERE path IN ('$MAIN','$SUB/agent-agentX.jsonl')")|$(q "SELECT COUNT(*) FROM tx_calls WHERE message_id IN ('msg_A','msg_X1')")|$(q "SELECT COUNT(*) FROM v_tx_call_attr WHERE attempt = 'r1#2.1'")" "${SNAP%%/*}/$(echo "$SNAP" | cut -d/ -f2)|2|2|2"
# a schema-5 database (the live db before go-live: no helper / adv_* columns, no fable price): rebuild-auto adds the columns in place,
# helper NOT NULL DEFAULT 0 on every kept row, fable seeded, sonnet's stored price kept (only reseed-prices changes it)
q "$(q "SELECT 'DROP VIEW '||name||';' FROM sqlite_master WHERE type='view'") ALTER TABLE tx_calls DROP COLUMN helper; ALTER TABLE tx_calls DROP COLUMN adv_n; ALTER TABLE tx_calls DROP COLUMN adv_cw1h; ALTER TABLE tx_files DROP COLUMN helper; ALTER TABLE tx_files DROP COLUMN adv_v; DELETE FROM prices WHERE family = 'fable'; UPDATE prices SET cr_p = 0.20 WHERE family = 'sonnet'; DELETE FROM schema_version; INSERT INTO schema_version VALUES (5, 'old');"
o=$(node "$L" sync 2>&1)
ok "schema 5 -> 6: rebuild-auto, rows kept, helper/adv columns added in place (helper 0), fable seeded, sonnet cr_p kept until reseed" "$(echo "$o" | grep -c '(rebuild-auto)')|$(q "SELECT COUNT(*) FROM tx_calls")|$(q "SELECT group_concat(name) FROM (SELECT name FROM pragma_table_info('tx_calls') WHERE name IN ('helper','adv_n','adv_cw1h') ORDER BY name)")|$(q "SELECT group_concat(DISTINCT helper) FROM tx_calls")|$(q "SELECT COUNT(*) FROM pragma_table_info('tx_files') WHERE name IN ('helper','adv_v')")|$(q "SELECT group_concat(family||'='||cr_p) FROM (SELECT * FROM prices WHERE family IN ('fable','sonnet') ORDER BY family)")" "1|${SNAP%%/*}|adv_cw1h,adv_n,helper|0|2|fable=0.2,sonnet=0.2"
q "UPDATE tx_calls SET helper = 1 WHERE message_id IN ('msg_L','msg_O'); UPDATE tx_files SET helper = 1 WHERE session_id IN ('L','O'); UPDATE prices SET cr_p = 0.10 WHERE family = 'sonnet'"   # put the tags back (a real migration finds no helper rows: they were skipped before schema 6)
# a v2 database (no pending_tu / sha_v, schema_version 2): the version change is rebuild-auto and the columns are added in place, rows kept
q "ALTER TABLE tx_files DROP COLUMN pending_tu; ALTER TABLE tx_files DROP COLUMN sha_v; DROP TABLE tx_git_commits; DROP TABLE tx_meta; DELETE FROM schema_version; INSERT INTO schema_version VALUES (2, 'old');"
o=$(node "$L" sync 2>&1)
ok "schema 2 -> 6: rebuild-auto, tx rows kept, columns added in place, new tables created" "$(echo "$o" | grep -c '(rebuild-auto)')|$(q "SELECT COUNT(*) FROM tx_calls")|$(q "SELECT group_concat(name) FROM pragma_table_info('tx_files') WHERE name IN ('pending_tu','sha_v')")|$(q "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('tx_git_commits','tx_meta')")|$(q "SELECT group_concat(version) FROM schema_version")" "1|${SNAP%%/*}|pending_tu,sha_v|2|6"
ok "backfill flagged the surviving files (gone from disk) as not scanned: sha_v 0 stays" "$(q "SELECT COUNT(*) FROM tx_files WHERE path = '$MAIN' AND sha_v = 0")" "1"

echo "== commit SHAs: git commit -q results (hex after a Bash commit), pending id kept across syncs, git log table, attribution (row 4b)"
S3="$P/S3.jsonl"; GH="$T/home"; mkdir -p "$GH/sample-repo"
git -C "$GH/sample-repo" init -q && echo x > "$GH/sample-repo/f" && git -C "$GH/sample-repo" add f
GIT_AUTHOR_DATE=2026-10-01T00:40:30Z GIT_COMMITTER_DATE=2026-10-01T00:40:30Z git -C "$GH/sample-repo" -c user.name=testauthor -c user.email=a@b.c commit -q -m "feat: x"
FULL=$(git -C "$GH/sample-repo" rev-parse HEAD); SH=${FULL:0:7}
ORCH_COMMIT_HOME="$GH"; export ORCH_COMMIT_REPOS=sample-repo ORCH_COMMIT_AUTHOR=testauthor   # neutral fixture repo and author
export TZ=UTC   # git reads a date-only --since in local time at the current time of day; metrics.js passes one (fixture commits sit near the day boundary)
CM='[{"type":"tool_use","id":"tuC","name":"Bash","input":{"command":"git add f && git commit -q -m x && git log -1 --format=%h"}},{"type":"tool_use","id":"tuN","name":"Bash","input":{"command":"git log -3 --oneline"}}]'
{ u 2026-10-01T00:40:00.000Z "commit it"; a 2026-10-01T00:40:10.000Z msg_S3a 5 claude-opus-5-5 0 0 "$CM" S3; } > "$S3"
node "$L" sync >/dev/null 2>&1
ok "the commit tool_use id is pending after the first sync (not yet answered)" "$(q "SELECT pending_tu FROM tx_files WHERE path = '$S3'")" '["tuC"]'
jq -cn --arg h "$SH" '{type:"user",timestamp:"2026-10-01T00:40:20.000Z",message:{content:[{type:"tool_result",tool_use_id:"tuN",content:"0badf00 older commit"}]}}' >> "$S3"
jq -cn --arg h "$SH" '{type:"user",timestamp:"2026-10-01T00:40:21.000Z",message:{content:[{type:"tool_result",tool_use_id:"tuC",content:($h+"\n")}]}}' >> "$S3"
a 2026-10-01T00:41:00.000Z msg_S3b 5 claude-opus-5-5 0 0 '[]' S3 >> "$S3"
o=$(node "$L" sync --full-git 2>&1)
ok "result arrives in a later sync: its hex is stored, a non-commit Bash result is not, pending cleared" "$(q "SELECT group_concat(sha) FROM tx_shas WHERE src_file = '$S3'")|$(q "SELECT coalesce(pending_tu,'none') FROM tx_files WHERE path = '$S3'")" "$SH|none"
ok "git log table: the owner commit stored (sha7, repo), sync reports it" "$(q "SELECT sha||' '||repo FROM tx_git_commits")|$(echo "$o" | grep -c 'git commits: 1 repos scanned')" "$SH sample-repo|1"
ok "a second sync rescans incrementally and adds nothing" "$(node "$L" sync 2>&1 | grep -c '+0 new (1 stored)')" "1"
# dashboard attribution cases live with the dashboard in the boss-analytics plugin (ledger.js dashboard is its seam)
# a pre-schema-3 file (sha_v 0) still on disk is re-read for SHAs only
q "UPDATE tx_files SET sha_v = 0, pending_tu = NULL WHERE path = '$S3'; DELETE FROM tx_shas WHERE src_file = '$S3'"
o=$(node "$L" sync 2>&1)
ok "backfill: sha_v 0 file re-read, SHA recovered, tx_calls unchanged" "$(q "SELECT group_concat(sha) FROM tx_shas WHERE src_file = '$S3'")|$(q "SELECT sha_v FROM tx_files WHERE path = '$S3'")|$(q "SELECT COUNT(*) FROM tx_calls WHERE session_id = 'S3'")|$(echo "$o" | grep -c 'sha backfill: 1 older')" "$SH|1|2|1"

echo "== catch-up note (row 4b; the retention warning cases moved with the dashboard)"
echo '{"cleanupPeriodDays":30}' > "$ORCH_SETTINGS"
OLD="$P/S9.jsonl"; a 2026-09-30T00:00:00.000Z msg_old 5 claude-opus-5-5 0 0 '[]' S9 > "$OLD"; touch -d '27 days ago' "$OLD" "$S3"
node "$L" sync >/dev/null 2>&1
q "UPDATE tx_sync_log SET finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now','-10 days') WHERE id = (SELECT max(id) FROM tx_sync_log)"
o=$(node "$L" sync 2>&1)
ok "catch-up: sync prints a note when the previous sync is over 7 days old" "$(echo "$o" | grep -c 'note: the previous sync was 10')" "1"
ok "no note when the previous sync is recent" "$(node "$L" sync 2>&1 | grep -c 'previous sync was')" "0"


echo "== result"
echo "pass=$pass fail=$fail"; [ "$fail" = 0 ]
