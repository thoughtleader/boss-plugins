#!/usr/bin/env bash
# ledger.js tests (orchestrator v2 fixes, row 4): F-10 unknown/mode/tools/ms, F-30 safe tokens, D6 Codex section,
# D14/F-23 role grouping + tok/accepted, A-07 stats over every matching row, stable line ids, --role filter.
# Row 13 (D4 design §4, §8, §9 row 13): F-18 update targeting (--attempt, --ts, the multi-attempt refusal), add --attempt,
# --tokens unknown, notice ingest (golden notice, dedupe, no-ID, A-14, fake tags, waiting/resume/continuation/killed numbers,
# --tail-bytes all, reconcile fixture 9 with this ledger.js, G4, G5, G7, G8, Q2 help sentence, no sqlite on require).
# Runs on scratch ORCH_DIR / ORCH_RUNS_DIR / ORCH_PROJECTS_DIR; never the live ACTIVE, ACTIVE.lease or run ledgers.
# Usage: bash ~/.claude/orchestrator/test/ledger-tests.sh   (exit 0 = all pass)
set -u
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"   # the checkout this suite tests (a worktree runs its own code)
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
L="$CL/lib/ledger.js"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export ORCH_WFP_STATE="$T/wfp" ORCH_DIR="$T/orch" ORCH_RUNS_DIR="$T/orch/runs" ORCH_PROJECTS_DIR="$T/projects" ORCH_METRICS_DB="$T/metrics.db" ORCH_SETTINGS="$T/settings.json" ORCH_COMMIT_HOME="$T/nohome"
unset WFP_SID BOSS_PROGRESS_DIR; mkdir -p "$ORCH_RUNS_DIR" "$ORCH_PROJECTS_DIR"
LIVE="$HOME/.claude/orchestrator"; sum(){ if [ -e "$1" ]; then md5sum < "$1"; else echo absent; fi; }
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
F(){ echo "$ORCH_RUNS_DIR/$1/ledger.jsonl"; }
A(){ node "$L" add --repo r --area a "$@"; }   # add with the shared repo/area
rc(){ "$@" >/dev/null 2>&1; echo $?; }

echo "== F-10 unknown effort, mode normalisation, unknown tools/ms"
R=t10
A --run $R --row 1 --role discovery --agent x --model haiku --effort unknown --mode "Agent Team (only when argued)" --tokens 1000 --tools unknown --ms unknown >/dev/null; ok "effort unknown + mode 'Agent Team (note)' accepted" "$?" "0"
ok "mode normalised to agent-team" "$(jq -r .mode "$(F $R)")" "agent-team"
ok "tools/ms unknown stored as null" "$(jq -r '[.tools, .ms] | map(tostring) | join(" ")' "$(F $R)")" "null null"
ok "effort unknown stored" "$(jq -r .effort "$(F $R)")" "unknown"
A --run $R --row 2 --role discovery --agent x --model haiku --effort low --mode WAVE --tokens 2000 --tools 3 --ms 4000 >/dev/null; ok "mode WAVE lowercased" "$(jq -r .mode "$(F $R)" | tail -1)" "wave"
ok "bogus mode refused (exit 2)" "$(rc node "$L" add --repo r --area a --run $R --row 3 --role discovery --agent x --model haiku --effort low --mode bogus --tokens 1 --tools 1 --ms 1)" "2"
s=$(node "$L" show --run $R)
ok "show reports 'unknown effort: 1'" "$(echo "$s" | grep -c '^unknown effort: 1 ')" "1"
ok "unknown effort left out of the model/effort groups" "$(echo "$s" | grep -cE '^haiku/unknown ')" "0"
ok "known effort row stays in the groups" "$(echo "$s" | grep -cE '^haiku/low +0 +- +- +no outcomes ')" "1"
ok "show lists the unknown-effort row in the table" "$(echo "$s" | grep -cE '^t10 +1 +discovery')" "1"
A --run $R --row 1 --role discovery --agent x --model haiku --effort unknown --mode wave --tokens 1000 --tools unknown --ms unknown --retries 1 >/dev/null; ok "retry on unknown effort: no auto escalation" "$(jq -r '.escalated' "$(F $R)" | tail -1)" "false"
node "$L" update --run $R --row 2 --tools unknown >/dev/null; ok "update --tools unknown sets null" "$(jq -r '.tools | tostring' "$(F $R)" | sed -n 2p)" "null"

echo "== F-30 safe token parsing"
R=t30
ok "1e400k refused (exit 2)" "$(rc node "$L" add --repo r --area a --run $R --row 1 --role review --agent x --model opus --effort high --mode wave --tokens 1e400k --tools 1 --ms 1)" "2"
ok "99999999999999999999k refused (exit 2)" "$(rc node "$L" add --repo r --area a --run $R --row 1 --role review --agent x --model opus --effort high --mode wave --tokens 99999999999999999999k --tools 1 --ms 1)" "2"
ok "refused add wrote no ledger" "$([ -e "$(F $R)" ] && echo present || echo none)" "none"
A --run $R --row 1 --role review --agent x --model opus --effort high --mode wave --tokens 1.2M --tools 1 --ms 1 >/dev/null; ok "1.2M accepted" "$(jq -r .tokens "$(F $R)")" "1200000"
ok "update --tokens 1e400k refused (exit 2)" "$(rc node "$L" update --run $R --row 1 --tokens 1e400k)" "2"

echo "== D6 Codex section, no escalation"
R=t6
A --run $R --row 1 --role review --agent codex --model gpt-6-astra --effort ultra --mode wave --tokens 120564 --tools unknown --ms unknown >/dev/null; ok "gpt-6-astra/ultra accepted" "$?" "0"
A --run $R --row 2 --role review --agent o --model opus --effort high --mode subagent --tokens 100000 --tools 10 --ms 10000 --first-pass y >/dev/null
A --run $R --row 3 --role review --agent c --model codex --effort ultra --mode wave --tokens 5000 --tools 1 --ms 1000 >/dev/null; ok "model codex accepted" "$?" "0"
A --run $R --row 3 --role review --agent o --model opus --effort max --mode wave --tokens 7000 --tools 1 --ms 1000 >/dev/null
ok "Claude retry after a Codex attempt: no auto escalation" "$(jq -r '.escalated' "$(F $R)" | tail -1)" "false"
A --run $R --row 1 --role review --agent codex --model gpt-6-astra --effort max --mode wave --tokens 1000 --tools 1 --ms 1 >/dev/null; ok "Codex retry (new effort) accepted" "$?" "0"
ok "Codex retry gets no auto escalation" "$(jq -r '[.retries, .escalated] | map(tostring) | join(" ")' "$(F $R)" | tail -1)" "1 false"
node "$L" update --attempt "$R#1.2" --first-pass y >/dev/null; ok "update --attempt on a 2-attempt row (F-18)" "$?" "0"
s=$(node "$L" show --run $R)
ok "codex section header present" "$(echo "$s" | grep -c '^== codex (own section')" "1"
ok "Claude block lists opus/high before the codex header" "$(echo "$s" | awk '/^== codex/{exit} /^opus\/high /{n++} END{print n+0}')" "1"
ok "no gpt row in the Claude block" "$(echo "$s" | awk '/^== codex/{exit} /^gpt-/{n++} END{print n+0}')" "0"
ok "gpt-6-astra/max listed in the codex section" "$(echo "$s" | awk '/^== codex/{f=1} f && /^gpt-6-astra\/max /{n++} END{print n+0}')" "1"
ok "ultra accepted for a Claude model too" "$(rc node "$L" add --repo r --area a --run t6b --row 1 --role review --agent o --model opus --effort ultra --mode wave --tokens 1 --tools 1 --ms 1)" "0"

echo "== D14 + F-23 role grouping, tok/accepted"
R=t14
A --run $R --row 1 --role implementation --agent b --model sonnet --effort high --mode subagent --tokens 100000 --tools 10 --ms 60000 >/dev/null
node "$L" update --run $R --row 1 --first-pass n >/dev/null
A --run $R --row 1 --role implementation --agent b --model opus --effort high --mode subagent --tokens 200000 --tools 20 --ms 120000 >/dev/null
node "$L" update --attempt "$R#1.2" --first-pass y >/dev/null
ok "retry after escalation: auto escalated flag" "$(jq -r '.escalated' "$(F $R)" | tail -1)" "true"
A --run $R --row 2 --role review --agent r --model opus --effort high --mode subagent --tokens 50000 --tools 5 --ms 30000 >/dev/null
A --run $R --row 3 --role implementation --agent b --model sonnet --effort high --mode subagent --tokens 30000 --tools 3 --ms 20000 >/dev/null
s=$(node "$L" show --run $R)
ok "implementation role line: 2 rows, tokens counted once, accepted 1, tok/accepted 330.0k" "$(echo "$s" | grep -c '^role implementation: 2 rows, 3 attempts, tokens 330.0k, accepted 1, tok/accepted 330.0k$')" "1"
ok "review role line: no accepted row prints '-'" "$(echo "$s" | grep -c '^role review: 1 rows, 1 attempts, tokens 50.0k, accepted 0, tok/accepted -$')" "1"
ok "accepted row sits under opus/high with both attempts' tokens (300.0k)" "$(echo "$s" | grep -cE '^opus/high +1 +300\.0k +300\.0k +1/1 100%\*')" "1"
ok "sonnet/high keeps no folded tokens: 0 accepted rows, tok/accepted -" "$(echo "$s" | grep -cE '^sonnet/high +0 +- +- +0/2 0%\*|^sonnet/high +0 +- +- +0/1 0%\*')" "1"
ok "no-outcome bucket lists the unreviewed implementation row" "$(echo "$s" | grep -cE '^no outcome +1 +30\.0k +- ')" "1"
ok "no-outcome bucket in the review role" "$(echo "$s" | grep -cE '^no outcome +1 +50\.0k +- ')" "1"
ok "no token counted in two groups: sum of group token cells equals role totals" "$(echo "$s" | awk '/^role implementation/{f=1;next} /^role review/{f=0} f && /^(opus|no outcome)/{print}' | grep -cE '300\.0k|30\.0k')" "2"

echo "== A-07 stats over every matching row, --last limits only the list"
R=t07
for i in $(seq 1 25); do A --run $R --row $i --role implementation --agent b --model sonnet --effort high --mode subagent --tokens 1000 --tools 1 --ms 1000 --first-pass y >/dev/null; done
s=$(node "$L" show --run $R --last 5)
ok "list shows 5 of 25" "$(echo "$s" | head -1)" "ledger: 5 of 25 attempts (run=t07; newest last)"
ok "table lists 5 rows" "$(echo "$s" | grep -c '^t07 ')" "5"
ok "stats header prints n=25" "$(echo "$s" | grep -c '^stats: n=25 attempts')" "1"
ok "role line counts 25 rows" "$(echo "$s" | grep -c '^role implementation: 25 rows, 25 attempts, tokens 25.0k, accepted 25, tok/accepted 1.0k$')" "1"
ok "model/effort group over 25 (first-pass 25/25 100%, no anecdotal star)" "$(echo "$s" | grep -cE '^sonnet/high +25 +25\.0k +1\.0k +25/25 100% ')" "1"

echo "== stable line ids, legacy lines untouched"
R=t-id
mkdir -p "$ORCH_RUNS_DIR/$R"; legacy='{"ts":"2026-01-01T00:00:00.000Z","run":"t-id","row":1,"role":"review","agent":"x","model":"opus","effort":"high","mode":"wave","tokens":10,"tools":1,"ms":1,"repo":"r","area":"a","firstPass":null,"retries":0,"escalated":false}'
echo "$legacy" > "$(F $R)"
A --run $R --row 1 --role review --agent x --model opus --effort max --mode wave --tokens 10 --tools 1 --ms 1 >/dev/null
A --run $R --row 2 --role review --agent x --model opus --effort high --mode wave --tokens 10 --tools 1 --ms 1 >/dev/null
A --run $R --row 2 --role review --agent x --model opus --effort high --mode wave --tokens 10 --tools 1 --ms 1 --retries 1 >/dev/null
A --run $R --row 7b --role review --agent x --model opus --effort high --mode wave --tokens 10 --tools 1 --ms 1 >/dev/null
ok "legacy line unchanged (no id added)" "$(sed -n 1p "$(F $R)")" "$legacy"
ok "attempt ordinal continues after a legacy line (row 1 -> .2)" "$(sed -n 2p "$(F $R)" | jq -r .id)" "t-id#1.2"
ok "first attempt of row 2 -> .1" "$(sed -n 3p "$(F $R)" | jq -r .id)" "t-id#2.1"
ok "same-step retry -> .2" "$(sed -n 4p "$(F $R)" | jq -r .id)" "t-id#2.2"
ok "letter row id" "$(sed -n 5p "$(F $R)" | jq -r .id)" "t-id#7b.1"
ok "id is the only new field" "$(sed -n 3p "$(F $R)" | jq -r 'keys | join(",")')" "agent,area,effort,escalated,firstPass,id,mode,model,ms,repo,retries,role,row,run,tokens,tools,ts"

echo "== --role filter"
s=$(node "$L" show --run t14 --role review)
ok "--role review lists only review attempts" "$(echo "$s" | head -1)" "ledger: 1 of 1 attempts (run=t14 role=review; newest last)"
ok "--role review: no implementation section" "$(echo "$s" | grep -c '^role implementation')" "0"
ok "--role bogus refused (exit 2)" "$(rc node "$L" show --role bogus)" "2"

echo "== F-18 update targeting, add --attempt, --tokens unknown"
R=t-f18
A --run $R --row 1 --role implementation --agent b --model sonnet --effort high --mode subagent --tokens 10 --tools 1 --ms 1 >/dev/null
A --run $R --row 1 --role implementation --agent b --model opus --effort high --mode subagent --tokens 20 --tools 2 --ms 2 >/dev/null
o=$(node "$L" update --run $R --row 1 --first-pass y 2>&1); st=$?
ok "update --run --row on a 2-attempt row refused (exit 2)" "$st" "2"
ok "the refusal names both attempts and the fix" "$(echo "$o" | grep -c "row 1 has 2 attempts: $R#1.1, $R#1.2; name one with --attempt <id> (or --ts <iso> for a line without an id)")" "1"
ok "refused update changed nothing" "$(jq -r '.firstPass | tostring' "$(F $R)" | tr '\n' ' ')" "null null "
node "$L" update --attempt "$R#1.1" --first-pass n >/dev/null; ok "update --attempt <older attempt> exits 0" "$?" "0"
ok "update --attempt edits exactly that line" "$(jq -r '.firstPass | tostring' "$(F $R)" | tr '\n' ' ')" "false null "
ok "update --attempt with a disagreeing --run refused (exit 2)" "$(rc node "$L" update --attempt "$R#1.1" --run other --first-pass y)" "2"
ok "update --attempt with a disagreeing --row refused (exit 2)" "$(rc node "$L" update --attempt "$R#1.1" --row 2 --first-pass y)" "2"
o=$(node "$L" update --attempt "$R#1.9" --first-pass y 2>&1); st=$?
ok "update --attempt with no such line: exit 2 + hint" "$st|$(echo "$o" | grep -c "no ledger line $R#1.9 (notice not ingested yet? manifest.js show --run $R)")" "2|1"
ok "update --attempt with a bad id refused (exit 2)" "$(rc node "$L" update --attempt "nope" --first-pass y)" "2"
ok "update --attempt and --ts together refused (exit 2)" "$(rc node "$L" update --attempt "$R#1.1" --run $R --ts 2026-01-01T00:00:00.000Z --first-pass y)" "2"
ok "no manifest.jsonl: update --attempt writes no outcome event" "$([ -e "$ORCH_RUNS_DIR/$R/manifest.jsonl" ] && echo present || echo none)" "none"
leg1='{"ts":"2026-01-01T00:00:00.000Z","run":"t-f18","row":2,"role":"review","agent":"x","model":"opus","effort":"high","mode":"wave","tokens":10,"tools":1,"ms":1,"repo":"r","area":"a","firstPass":null,"retries":0,"escalated":false}'
leg2='{"ts":"2026-01-02T00:00:00.000Z","run":"t-f18","row":2,"role":"review","agent":"x","model":"opus","effort":"max","mode":"wave","tokens":10,"tools":1,"ms":1,"repo":"r","area":"a","firstPass":null,"retries":1,"escalated":true}'
echo "$leg1" >> "$(F $R)"; echo "$leg2" >> "$(F $R)"
ok "two legacy lines (no id) in row 2: --row refused (exit 2)" "$(rc node "$L" update --run $R --row 2 --first-pass y)" "2"
o=$(node "$L" update --run $R --row 2 --first-pass y 2>&1); ok "the refusal lists legacy lines by ts" "$(echo "$o" | grep -c 'ts 2026-01-01T00:00:00.000Z, ts 2026-01-02T00:00:00.000Z')" "1"
node "$L" update --run $R --ts 2026-01-02T00:00:00.000Z --first-pass y >/dev/null; ok "update --run --ts reaches a legacy line" "$?" "0"
ok "--ts edited only that line" "$(jq -r 'select(.row==2) | .firstPass | tostring' "$(F $R)" | tr '\n' ' ')" "null true "
ok "--ts with no match refused (exit 2)" "$(rc node "$L" update --run $R --ts 2026-01-03T00:00:00.000Z --first-pass y)" "2"
ok "--ts not an ISO time refused (exit 2)" "$(rc node "$L" update --run $R --ts yesterday --first-pass y)" "2"
ok "a legacy line still parses after the edit (show lists 4 attempts)" "$(node "$L" show --run $R | head -1)" "ledger: 4 of 4 attempts (run=t-f18; newest last)"
A --run $R --row 3 --role review --agent x --model opus --effort high --mode wave --tokens unknown --tools 1 --ms 1 --attempt "$R#3.5" >/dev/null; ok "add --attempt accepted" "$?" "0"
ok "add --attempt: the id is the line id; --tokens unknown stores null" "$(tail -1 "$(F $R)" | jq -c '[.id, .tokens]')" "[\"$R#3.5\",null]"
ok "add --attempt with an existing id refused (exit 2)" "$(rc node "$L" add --repo r --area a --run $R --row 3 --role review --agent x --model opus --effort max --mode wave --tokens 1 --tools 1 --ms 1 --attempt "$R#3.5")" "2"
ok "add --attempt of another row refused (exit 2)" "$(rc node "$L" add --repo r --area a --run $R --row 4 --role review --agent x --model opus --effort max --mode wave --tokens 1 --tools 1 --ms 1 --attempt "$R#3.6")" "2"
A --run $R --row 3 --role review --agent x --model opus --effort max --mode wave --tokens 1 --tools 1 --ms 1 >/dev/null
ok "ordinal fallback never collides with an explicit id (row 3 -> .6)" "$(tail -1 "$(F $R)" | jq -r .id)" "$R#3.6"

echo "== ingest: golden notice (design §9.1), dedupe, no-ID"
M="$CL/lib/manifest.js"
LA(){ node "$M" launch "$@" 2>/dev/null | head -1; }
LB(){ LA --role implementation --agent orch-builder --mode subagent --repo r --area a "$@"; }
E(){ node "$M" event "$@" >/dev/null; }
MF(){ echo "$ORCH_RUNS_DIR/$1/manifest.jsonl"; }
I(){ node "$L" ingest "$@"; }
notice(){ # task tool_use_id description tokens tools ms [status] [summary tail] [result body]; empty tokens = no <usage>
  local u=""; [ -n "$4" ] && u="<usage><subagent_tokens>$4</subagent_tokens><tool_uses>$5</tool_uses><duration_ms>$6</duration_ms></usage>"
  printf '<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<output-file>/tmp/fixture/%s.output</output-file>\n<status>%s</status>\n<summary>Agent "%s" %s</summary>\n<result>%s</result>\n%s\n</task-notification>' \
    "$1" "$2" "$1" "${7:-completed}" "$3" "${8:-finished}" "${9:-FINDINGS fixture}" "$u"; }
enq(){ jq -cn --arg ts "$1" --arg c "$2" '{type:"queue-operation",operation:"enqueue",timestamp:$ts,content:$c}'; }
now(){ date -u +%Y-%m-%dT%H:%M:%S.000Z; }
R=t-gold; GD="$T/gold"; mkdir -p "$GD" "$ORCH_RUNS_DIR/$R"
ok "golden: launch allocates t-gold#6.1" "$(LA --run $R --row 6 --role discovery --agent orch-scout --model haiku --effort low --mode wave --repo .claude --area orchestrator)" "$R#6.1"
notice a12f7f52584ff3573 toolu_013h1qc5HxCwQtg6NkVww9AH "[haiku/low] t-gold#6.1 Smoke test orch-scout" 43418 7 21708 > "$GD/notice.txt"
echo '{"agentType":"orch-scout","description":"[haiku/low] t-gold#6.1 Smoke test orch-scout","toolUseId":"toolu_013h1qc5HxCwQtg6NkVww9AH","model":"haiku","effort":"low","finished":true}' > "$GD/agent-a12f7f52584ff3573.meta.json"
o=$(I --file "$GD/notice.txt" --meta "$GD/agent-a12f7f52584ff3573.meta.json"); st=$?
ok "golden: ingest exits 0 and reports the write" "$st|$o" "0|$R#6.1: stop new, ledger written"
ok "golden: tokens 43418, tools 7, ms 21708, haiku/low, orch-scout, retries 0" "$(jq -c '[.id,.tokens,.tools,.ms,.model,.effort,.agent,.retries,.firstPass,.escalated]' "$(F $R)")" "[\"$R#6.1\",43418,7,21708,\"haiku\",\"low\",\"orch-scout\",0,null,false]"
ok "golden: role, mode, repo, area from the launch; row is a number" "$(jq -c '[.role,.mode,.repo,.area,.row]' "$(F $R)")" '["discovery","wave",".claude","orchestrator",6]'
ok "golden: the line has add's fields (row 4's metric reads it)" "$(jq -r 'keys | join(",")' "$(F $R)")" "agent,area,effort,escalated,firstPass,id,mode,model,ms,repo,retries,role,row,run,tokens,tools,ts"
ok "golden: the stop event (via cli, final, model/effort from meta)" "$(jq -c 'select(.type=="stop")|[.attempt,.via,.phase,.final,.tokens,.tools,.ms,.numbers_from,.model_from,.effort_from,.key]' "$(MF $R)")" "[\"$R#6.1\",\"cli\",\"finished\",true,43418,7,21708,\"notice\",\"meta\",\"meta\",\"a12f7f52584ff3573:finished:7:21708\"]"
ok "golden: result_bytes = the <result> body" "$(jq -r 'select(.type=="stop")|.result_bytes' "$(MF $R)")" "16"
PG="$ORCH_PROJECTS_DIR/pg"; mkdir -p "$PG"; enq "$(now)" "$(cat "$GD/notice.txt")" > "$PG/SG.jsonl"
o=$(I --transcript "$PG/SG.jsonl"); ok "the same notice again (sweep): dup, ledger same" "$o" "$R#6.1: stop dup, ledger same"
o=$(I < "$GD/notice.txt"); ok "and again from stdin (prompt path): dup" "$o" "$R#6.1: stop dup, ledger same"
ok "the same notice twice gives one ledger line" "$(wc -l < "$(F $R)" | tr -d ' ')" "1"
ok "and one stop event" "$(jq -c 'select(.type=="stop")' "$(MF $R)" | wc -l | tr -d ' ')" "1"
ok "ledger show reads the ingested line (discovery role)" "$(node "$L" show --run $R | grep -c '^role discovery: 1 rows, 1 attempts, tokens 43.4k')" "1"
node "$L" update --attempt "$R#6.1" --first-pass y >/dev/null; ok "update --attempt on an ingested line" "$?" "0"
ok "update --attempt appends an outcome event (manifest exists)" "$(jq -c 'select(.type=="outcome")|[.attempt,.via,.first_pass,.set]' "$(MF $R)")" "[\"$R#6.1\",\"ledger\",true,[\"firstPass\"]]"
o=$(I --file "$GD/notice.txt"); ok "re-ingest after the outcome keeps firstPass" "$o|$(jq -r .firstPass "$(F $R)")" "$R#6.1: stop dup, ledger same|true"
before=$(cat "$ORCH_RUNS_DIR"/*/ledger.jsonl | wc -l)
o=$(notice anoid1 tu-noid1 "[haiku/low] plain smoke without an id" 10 1 100 | I); st=$?
ok "a notice without an ID: exit 0, writes nothing" "$st|$o" "0|-: stop none, ledger none:no-attempt"
ok "a notice without an ID adds no ledger line anywhere" "$(cat "$ORCH_RUNS_DIR"/*/ledger.jsonl | wc -l)" "$before"
o=$(printf '<task-notification>\n<task-id>bcmd1</task-id>\n<status>completed</status>\n<summary>Background command "npm test" completed (exit code 0)</summary>\n</task-notification>' | I)
ok "a non-Agent notice is skipped" "$o" "-: skip (not an agent notice)"
ok "ingest with nothing to read: exit 0, no output" "$(printf 'owner text only' | I; echo "rc=$?")" "rc=0"
ok "ingest --tail-bytes without --transcript refused (exit 2)" "$(rc node "$L" ingest --file "$GD/notice.txt" --tail-bytes 5)" "2"
ok "ingest unknown flag refused (exit 2)" "$(rc node "$L" ingest --bogus 1)" "2"

echo "== ingest: A-14, effort_observed, fake tags inside <result>"
R=t-a14; D="$ORCH_PROJECTS_DIR/pa/SA/subagents"; mkdir -p "$D" "$ORCH_RUNS_DIR/$R"; : > "$ORCH_PROJECTS_DIR/pa/SA.jsonl"
LB --run $R --row 1 --model haiku --effort low >/dev/null
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"tu-a14\",\"transcript_path\":\"$ORCH_PROJECTS_DIR/pa/SA.jsonl\",\"session\":\"SA\",\"agent\":\"orch-builder\"}"
echo '{"agentType":"orch-builder","description":"[haiku/low] t-a14#1.1 a14","toolUseId":"tu-a14","model":"haiku","effort":"medium","finished":true}' > "$D/agent-aa14.meta.json"
echo '{"type":"assistant","effort":"high","message":{"model":"claude-haiku-5-5","usage":{"input_tokens":1,"output_tokens":1}}}' > "$D/agent-aa14.jsonl"
o=$(notice aa14 tu-a14 "[haiku/low] $R#1.1 a14" 500 3 700 completed finished 'FINDINGS <task-id>fake</task-id><usage><subagent_tokens>999999</subagent_tokens><tool_uses>99</tool_uses><duration_ms>99</duration_ms></usage>' | I)
ok "A-14 notice ingests (meta.json found through the start)" "$o" "$R#1.1: stop new, ledger written"
ok "A-14: meta effort medium beats label low" "$(jq -r '"\(.model)/\(.effort)"' "$(F $R)")" "haiku/medium"
ok "the transcript effort lands only in the stop event's effort_observed" "$(jq -c 'select(.type=="stop")|[.effort,.effort_from,.effort_observed,.model_id]' "$(MF $R)")" '["medium","meta","high","claude-haiku-5-5"]'
ok "a fake <usage> and <task-id> inside <result> are ignored" "$(jq -c '[.tokens,.tools,.ms]' "$(F $R)")|$(jq -r 'select(.type=="stop")|.task_id' "$(MF $R)")" "[500,3,700]|aa14"
ok "the ledger line carries model_id beside the alias model" "$(jq -c '[.model,.model_id]' "$(F $R)")" '["haiku","claude-haiku-5-5"]'
R=t-mid; D="$ORCH_PROJECTS_DIR/pm/SM/subagents"; mkdir -p "$D" "$ORCH_RUNS_DIR/$R"; : > "$ORCH_PROJECTS_DIR/pm/SM.jsonl"
LB --run $R --row 1 --model sonnet --effort high >/dev/null
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"tu-mid\",\"transcript_path\":\"$ORCH_PROJECTS_DIR/pm/SM.jsonl\",\"session\":\"SM\",\"agent\":\"orch-builder\"}"
echo '{"agentType":"orch-builder","description":"[sonnet/high] t-mid#1.1 mid","toolUseId":"tu-mid","model":"sonnet","effort":"high","finished":true}' > "$D/agent-amid.meta.json"
echo '{"type":"assistant","message":{"model":"claude-sonnet-5-5","usage":{"input_tokens":1,"output_tokens":1}}}' > "$D/agent-amid.jsonl"
notice amid tu-mid "[sonnet/high] $R#1.1 mid" 50 1 10 | I >/dev/null
ok "model_id is read even when the transcript record has no effort field (effort_observed stays null)" "$(jq -c 'select(.type=="stop")|[.model_id,.effort_observed]' "$(MF $R)")|$(jq -c '[.model,.model_id]' "$(F $R)")" '["claude-sonnet-5-5",null]|["sonnet","claude-sonnet-5-5"]'
R=t-nomid; mkdir -p "$ORCH_RUNS_DIR/$R"; LB --run $R --row 1 --model opus --effort high >/dev/null
notice anomid tu-nomid "[opus/high] $R#1.1 nomid" 50 1 10 | I >/dev/null
ok "no transcript: model_id is null on the stop and the ledger line (never guessed from the alias)" "$(jq -c '[.model,.model_id]' "$(F $R)")" '["opus",null]'
R=t-lbl; mkdir -p "$ORCH_RUNS_DIR/$R"; LB --run $R --row 1 --model opus --effort high >/dev/null
notice albl tu-lbl "[sonnet/low] $R#1.1 label only" 50 1 10 | I >/dev/null
ok "no meta.json: the label wins over the launch" "$(jq -r '"\(.model)/\(.effort)"' "$(F $R)")" "sonnet/low"
R=t-lch; mkdir -p "$ORCH_RUNS_DIR/$R"; LB --run $R --row 1 --model opus --effort high >/dev/null
notice alch tu-lch "[auto] $R#1.1 no label" 50 1 10 | I >/dev/null
ok "no meta.json, no label: the launch's model/effort" "$(jq -r '"\(.model)/\(.effort)"' "$(F $R)")" "opus/high"

echo "== ingest: waiting -> finished, resume pair, killed"
R=t-wait; mkdir -p "$ORCH_RUNS_DIR/$R"; LB --run $R --row 1 --model sonnet --effort high >/dev/null
o=$(notice awt tu-wt "[sonnet/high] $R#1.1 w" 100 3 100 completed "is waiting on its own background work" | I)
ok "waiting-only notice (completed): the idle stop is final enough for a ledger line" "$o|$(wc -l < "$(F $R)" | tr -d ' ')|$(jq -c '[.tokens,.tools,.ms]' "$(F $R)")" "$R#1.1: stop new, ledger written|1|[100,3,100]"
ok "the stored waiting stop keeps final:false" "$(jq -r 'select(.type=="stop")|"\(.phase) \(.final)"' "$(MF $R)")" "waiting false"
o=$(notice awt tu-wt "[sonnet/high] $R#1.1 w" 200 7 500 | I)
ok "waiting -> finished: the same line is updated, not duplicated" "$o|$(wc -l < "$(F $R)" | tr -d ' ')|$(jq -c '[.tokens,.tools,.ms]' "$(F $R)")" "$R#1.1: stop new, ledger updated|1|[200,7,500]"
R=t-waitrun; mkdir -p "$ORCH_RUNS_DIR/$R"; LB --run $R --row 1 --model sonnet --effort high >/dev/null
notice awr tu-wr "[sonnet/high] $R#1.1 w" 100 3 100 failed "is waiting on its own background work" | I >/dev/null
ok "a waiting notice whose status is not completed writes no line" "$([ -e "$(F $R)" ] && echo present || echo none)" "none"
R=t-res; mkdir -p "$ORCH_RUNS_DIR/$R"; LB --run $R --row 1 --model sonnet --effort high >/dev/null
notice ars tu-rs "[sonnet/high] $R#1.1 r" 100 5 1000 | I >/dev/null
o=$(notice ars tu-rs "[sonnet/high] $R#1.1 r" 150 2 300 | I)
ok "resume pair (tools/ms drop): the line is updated" "$o" "$R#1.1: stop new, ledger updated"
ok "resume pair adds tools/ms and takes the latest tokens" "$(jq -c '[.tokens,.tools,.ms]' "$(F $R)")" "[150,7,1300]"
R=t-kill; KD="$T/kill"; mkdir -p "$KD" "$ORCH_RUNS_DIR/$R"; LB --run $R --row 1 --model sonnet --effort high >/dev/null
echo '{"agentType":"orch-builder","description":"[sonnet/high] t-kill#1.1 k","toolUseId":"tu-kill","model":"sonnet","effort":"high"}' > "$KD/agent-akill.meta.json"
{ echo '{"type":"assistant","effort":"high","message":{"model":"claude-sonnet-5-5","usage":{"input_tokens":10,"cache_creation_input_tokens":20,"cache_read_input_tokens":30,"output_tokens":5},"content":[{"type":"tool_use","id":"tu1"},{"type":"tool_use","id":"tu2"}]}}'
  echo '{"type":"user","message":{"content":"x"}}'
  echo '{"type":"assistant","effort":"high","message":{"model":"claude-sonnet-5-5","usage":{"input_tokens":12,"cache_creation_input_tokens":0,"cache_read_input_tokens":100,"output_tokens":8},"content":[{"type":"tool_use","id":"tu2"},{"type":"tool_use","id":"tu3"}]}}'; } > "$KD/agent-akill.jsonl"
o=$(notice akill tu-kill "[sonnet/high] $R#1.1 k" "" "" "" killed "was stopped by Claude" | I --meta "$KD/agent-akill.meta.json")
ok "killed notice with no usage: line written" "$o" "$R#1.1: stop new, ledger written"
ok "killed: tokens/tools from the subagent transcript, ms null" "$(jq -c '[.tokens,.tools,.ms]' "$(F $R)")" "[120,3,null]"
ok "killed: stop phase killed, numbers_from transcript" "$(jq -c 'select(.type=="stop")|[.phase,.final,.numbers_from,.key]' "$(MF $R)")" '["killed",true,"transcript","akill:killed:-:-"]'

echo "== ingest: --tail-bytes all"
R=t-tail; mkdir -p "$ORCH_RUNS_DIR/$R" "$ORCH_PROJECTS_DIR/pt"; LB --run $R --row 1 --model haiku --effort low >/dev/null; TT="$ORCH_PROJECTS_DIR/pt/ST.jsonl"
{ enq "$(now)" "$(notice atl tu-tl "[haiku/low] $R#1.1 t" 10 1 10)"; printf '{"type":"user","pad":"%s"}\n' "$(head -c 6000 /dev/zero | tr '\0' x)"; } > "$TT"
ok "--tail-bytes 100 misses a notice at the head" "$(I --transcript "$TT" --tail-bytes 100 | wc -l | tr -d ' ')|$([ -e "$(F $R)" ] && echo present || echo none)" "0|none"
ok "--tail-bytes all reads the whole file" "$(I --transcript "$TT" --tail-bytes all)" "$R#1.1: stop new, ledger written"
ok "--tail-bytes all: the stop came from the sweep, with the record's timestamp" "$(jq -r 'select(.type=="stop")|.via' "$(MF $R)")" "sweep"
ok "--tail-bytes bogus refused (exit 2)" "$(rc node "$L" ingest --transcript "$TT" --tail-bytes lots)" "2"

echo "== ingest: continuation by task-id, G4, G5"
R=t-cont; PC="$ORCH_PROJECTS_DIR/pc"; mkdir -p "$PC" "$ORCH_RUNS_DIR/$R"; : > "$PC/SC.jsonl"
P=$(LB --run $R --row 1 --model sonnet --effort high)
E --run $R --type start --json "{\"attempt\":\"$P\",\"tool_use_id\":\"tu-c\",\"transcript_path\":\"$PC/SC.jsonl\",\"session\":\"SC\"}"
notice acont tu-c "[sonnet/high] $P cont" 100 4 400 | I >/dev/null
C=$(LB --run $R --row 1 --model sonnet --effort high --continue "$P")
ok "continuation launched as .2" "$C" "$R#1.2"
enq "2000-01-01T00:00:00.000Z" "$(notice acont tu-c "[sonnet/high] $P cont" 110 4 450)" > "$PC/SC.jsonl"
o=$(I --transcript "$PC/SC.jsonl")
ok "G4: a predecessor notice swept after the continuation's launch (older record) maps to the predecessor" "$o" "$P: stop new, ledger updated"
ok "G4: the continuation stays open" "$(node "$M" show --run $R --json | jq -r --arg c "$C" '.attempts[]|select(.id==$c)|.state')" "allocated"
o=$(notice acont tu-c "[sonnet/high] $P cont" 300 6 900 | I)
ok "a continuation's notice maps by task-id to the continuation" "$o" "$C: stop new, ledger written"
ok "the continuation's line keeps its notice's tokens (Q2, no delta); retries n-1" "$(jq -c --arg c "$C" 'select(.id==$c)|[.tokens,.tools,.ms,.retries]' "$(F $R)")" "[300,6,900,1]"
ok "the predecessor's line is untouched by the continuation" "$(jq -c --arg p "$P" 'select(.id==$p)|[.tokens,.tools,.ms]' "$(F $R)")" "[110,4,450]"
R=t-g5; mkdir -p "$ORCH_RUNS_DIR/$R"; : > "$PC/S5.jsonl"
ok "G5: first launch" "$(LB --run $R --row 1 --model opus --effort high)" "$R#1.1"
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"tu-g5a\",\"transcript_path\":\"$PC/S5.jsonl\",\"session\":\"S5\"}"
ok "G5: a phantom start, relaunch reuses the ID" "$(node "$M" launch --run $R --row 1 --role implementation --agent orch-builder --mode subagent --repo r --area a --model opus --effort high 2>/dev/null | tr '\n' ' ')" "$R#1.1 reused "
notice ag5 tu-g5b "[opus/high] $R#1.1 g5" 70 2 20 | I >/dev/null
ok "G5: the reused attempt's ledger line has retries 0" "$(jq -c '[.id,.retries,.escalated]' "$(F $R)")" "[\"$R#1.1\",0,false]"

echo "== retry_kind: continue / fresh / none, update validation, add default, heal"
ok "retry_kind: the continuation's line (launch --continue) has retry_kind continue; the first attempt has none" "$(jq -c --arg c "t-cont#1.2" --arg p "t-cont#1.1" 'select(.id==$c or .id==$p)|[.id,(.retry_kind // "none")]' "$(F t-cont)" | tr '\n' ' ')" '["t-cont#1.1","none"] ["t-cont#1.2","continue"] '
R=t-rk; PK="$ORCH_PROJECTS_DIR/prk"; mkdir -p "$PK" "$ORCH_RUNS_DIR/$R"; : > "$PK/SK.jsonl"
P=$(LB --run $R --row 1 --model sonnet --effort high)
E --run $R --type start --json "{\"attempt\":\"$P\",\"tool_use_id\":\"tu-rk1\",\"transcript_path\":\"$PK/SK.jsonl\",\"session\":\"SK\"}"
notice ark1 tu-rk1 "[sonnet/high] $P rk" 100 4 400 | I >/dev/null
F2=$(LB --run $R --row 1 --model opus --effort high)
ok "retry_kind: a fresh retry is launched as .2" "$F2" "$R#1.2"
E --run $R --type start --json "{\"attempt\":\"$F2\",\"tool_use_id\":\"tu-rk2\",\"transcript_path\":\"$PK/SK.jsonl\",\"session\":\"SK\"}"
notice ark2 tu-rk2 "[opus/high] $F2 rk" 120 5 500 | I >/dev/null
ok "retry_kind: attempt 1 has no key; attempt 2 launched without --continue is fresh" "$(jq -c '[.id,(.retry_kind // "none")]' "$(F $R)" | tr '\n' ' ')" "[\"$R#1.1\",\"none\"] [\"$R#1.2\",\"fresh\"] "
ok "retry_kind: attempt 1 line has no retry_kind key at all" "$(jq -r --arg p "$P" 'select(.id==$p)|has("retry_kind")' "$(F $R)")" "false"
ok "update --retry-kind bogus refused (exit 2)" "$(rc node "$L" update --attempt "$F2" --retry-kind bogus)" "2"
ok "update --retry-kind (empty) refused (exit 2)" "$(rc node "$L" update --attempt "$F2" --retry-kind "")" "2"
ok "update --retry-kind continue sets it" "$(node "$L" update --attempt "$F2" --retry-kind continue >/dev/null; jq -r --arg a "$F2" 'select(.id==$a)|.retry_kind' "$(F $R)")" "continue"
ok "update --retry-kind Fresh (case-insensitive) sets fresh" "$(node "$L" update --attempt "$F2" --retry-kind Fresh >/dev/null; jq -r --arg a "$F2" 'select(.id==$a)|.retry_kind' "$(F $R)")" "fresh"
ok "a re-ingest never overrides a set retry_kind" "$(node "$L" update --attempt "$F2" --retry-kind continue >/dev/null; notice ark2 tu-rk2 "[opus/high] $F2 rk" 130 5 500 | I >/dev/null; jq -r --arg a "$F2" 'select(.id==$a)|.retry_kind' "$(F $R)")" "continue"
jq -c --arg a "$F2" 'if .id==$a then del(.retry_kind) else . end' "$(F $R)" > "$T/rk.tmp" && mv "$T/rk.tmp" "$(F $R)"
ok "heal: a retry line without the field gets it from the launch event on the next ingest (here fresh: launched without --continue)" "$(notice ark2 tu-rk2 "[opus/high] $F2 rk" 130 5 500 | I >/dev/null; jq -r --arg a "$F2" 'select(.id==$a)|.retry_kind' "$(F $R)")" "fresh"
echo '{"ts":"2026-01-01T00:00:00.000Z","id":"t-rk#2.1","run":"t-rk","row":2,"role":"implementation","agent":"x","model":"sonnet","effort":"high","mode":"subagent","tokens":1,"tools":1,"ms":1,"repo":"r","area":"a","firstPass":null,"retries":0,"escalated":false}' >> "$(F $R)"
ok "an old line without the field still shows and updates" "$(node "$L" show --run $R >/dev/null 2>&1; echo $?)|$(node "$L" update --attempt t-rk#2.1 --first-pass y >/dev/null 2>&1; echo $?)" "0|0"
R=t-rka; mkdir -p "$ORCH_RUNS_DIR/$R"
A --run $R --row 1 --role implementation --agent x --model sonnet --effort high --mode subagent --tokens 1 --tools 1 --ms 1 >/dev/null
A --run $R --row 1 --role implementation --agent x --model opus --effort high --mode subagent --tokens 1 --tools 1 --ms 1 >/dev/null
A --run $R --row 1 --role implementation --agent x --model opus --effort high --mode subagent --tokens 1 --tools 1 --ms 1 --retries 2 --retry-kind continue >/dev/null
ok "add: attempt 1 none, attempt n>1 defaults to fresh, --retry-kind continue honoured" "$(jq -r '(.retry_kind // "none")' "$(F $R)" | tr '\n' ' ')" "none fresh continue "
ok "add --retry-kind bogus refused (exit 2)" "$(rc node "$L" add --repo r --area a --run $R --row 2 --role review --agent x --model opus --effort max --mode wave --tokens 1 --tools 1 --ms 1 --retry-kind bogus)" "2"
ok "help documents --retry-kind" "$(node "$L" help | grep -c -- '--retry-kind continue|fresh')" "3"

echo "== launch --continue through a real ingest (G7, G8)"
R=t-g8; PG8="$ORCH_PROJECTS_DIR/p8"; mkdir -p "$PG8" "$ORCH_RUNS_DIR/$R"; : > "$PG8/S8.jsonl"
P=$(LB --run $R --row 1 --model sonnet --effort high)
E --run $R --type start --json "{\"attempt\":\"$P\",\"tool_use_id\":\"tu-g8\",\"transcript_path\":\"$PG8/S8.jsonl\",\"session\":\"S8\",\"ts\":\"$(now)\"}"
enq "2099-01-01T00:00:00.000Z" "$(notice ag8 tu-g8 "[sonnet/high] $P g8" 100 4 400)" > "$PG8/S8.jsonl"
o=$(node "$M" launch --run $R --row 1 --role implementation --agent orch-builder --mode subagent --repo r --area a --model sonnet --effort high --continue "$P" 2>/dev/null); st=$?
ok "G8: launch --continue P exits 0 and prints new" "$st|$(echo "$o" | sed -n 2p)" "0|new"
ok "G8: P's final stop came from the sweep" "$(jq -c --arg p "$P" 'select(.type=="stop" and .attempt==$p)|[.via,.final]' "$(MF $R)")" '["sweep",true]'
ok "G8: the launch has continues P and P's task_id" "$(jq -c --arg p "$P" 'select(.type=="launch" and .continues==$p)|[.attempt,.task_id]' "$(MF $R)")" "[\"$R#1.2\",\"ag8\"]"
R=t-g7; PG7="$ORCH_PROJECTS_DIR/p7"; mkdir -p "$PG7" "$ORCH_RUNS_DIR/$R"; : > "$PG7/S7.jsonl"
P=$(LB --run $R --row 1 --model sonnet --effort high)
E --run $R --type start --json "{\"attempt\":\"$P\",\"tool_use_id\":\"tu-g7\",\"transcript_path\":\"$PG7/S7.jsonl\",\"session\":\"S7\"}"
notice ag7 tu-g7 "[sonnet/high] $P g7" 100 4 400 | I >/dev/null
C=$(LB --run $R --row 1 --model sonnet --effort high --continue "$P")
enq "2099-01-01T00:00:00.000Z" "$(notice ag7 tu-g7 "[sonnet/high] $P g7" 250 9 1200)" > "$PG7/S7.jsonl"
o=$(node "$M" launch --run $R --row 1 --role implementation --agent orch-builder --mode subagent --repo r --area a --model sonnet --effort high --continue "$C" 2>/dev/null); st=$?
ok "G7: launch --continue C (C a continuation of P) exits 0 and prints new" "$st|$(echo "$o" | sed -n 2p)" "0|new"
ok "G7: C's final stop came from the sweep" "$(jq -c --arg c "$C" 'select(.type=="stop" and .attempt==$c)|[.via,.final,.tools]' "$(MF $R)")" '["sweep",true,9]'
ok "G7: a launch with continues C" "$(jq -c --arg c "$C" 'select(.type=="launch" and .continues==$c)|[.attempt,.task_id]' "$(MF $R)")" "[\"$R#1.3\",\"ag7\"]"

echo "== reconcile fixture 9 with this ledger.js (design §7, §9 row 13)"
G(){ git -C "$1" -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false "${@:2}"; }
R=f9; RP="$T/repo9"; P9="$ORCH_PROJECTS_DIR/p9"; mkdir -p "$ORCH_RUNS_DIR/$R" "$RP" "$P9/SF9/subagents"
git -C "$RP" init -q -b main; G "$RP" commit -q --allow-empty -m "chore: init"
node "$M" launch --run $R --row 1 --role implementation --agent orch-builder --mode subagent --repo r --area a --model opus --effort high --git "$RP" --sid SF9 >/dev/null 2>&1
enq "$(now)" "$(notice a9 tu-f9 "[opus/high] $R#1.1 fixture nine" 100 2 300)" > "$P9/SF9.jsonl"
echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $R#1.1 fixture nine\",\"toolUseId\":\"tu-f9\",\"model\":\"opus\",\"effort\":\"high\",\"finished\":true}" > "$P9/SF9/subagents/agent-a9.meta.json"
G "$RP" commit -q --allow-empty -m "feat(n): nine" -m "Orch-Attempt: $R#1.1"; SHA9=$(git -C "$RP" rev-parse HEAD)
printf -- '---\nrow: 1\nstatus: done\nsha: %s\nattempt: %s\n---\nbody\n' "${SHA9:0:7}" "$R#1.1" > "$ORCH_RUNS_DIR/$R/1-nine.md"
OUT=$(node "$M" reconcile --run $R --sid SF9); st=$?
ok "fixture 9: reconcile exits 0" "$st" "0"
ok "fixture 9: start rebuilt" "$(echo "$OUT" | grep -c "$R#1.1: start rebuilt from $P9/SF9/subagents/agent-a9.meta.json")" "1"
ok "fixture 9: the sweep ingested the missed enqueue record" "$(echo "$OUT" | grep -c "$R#1.1: stop new, ledger written")" "1"
ok "fixture 9: done, next advance" "$(echo "$OUT" | grep "^1 | " | grep -c "| $R#1.1 | done .*| advance")" "1"
ok "fixture 9: one ledger line from the notice" "$(jq -c '[.id,.tokens,.tools,.ms]' "$(F $R)")" "[\"$R#1.1\",100,2,300]"

echo "== ingest gate (OD-3): no ACTIVE fallback, --sid / --run, held runs only"
# Every gated case launches its target attempt first, so an ungated ingest would write; snapshots are taken after all lease setup.
snap(){ echo "$(sum "$(MF $1)") $(sum "$(F $1)")"; }
LQ(){ node "$M" lease acquire --run "$1" --sid="$2" 2>/dev/null >/dev/null; echo $?; }
SA=zz-gate-sid-A-0001; S2=zz-gate-sid-S2-0002; SN=zz-gate-sid-N-0003
GX=t-gate-x; GA=t-gate-a; GA2=t-gate-a2; GB=t-gate-b; GQ=t-gate-q; GR=t-gate-r; GE=t-gate-e; GP=t-gate-p; GM=t-gate-m
for r in $GX $GA $GA2 $GB $GQ $GR $GE $GP $GM; do mkdir -p "$ORCH_RUNS_DIR/$r"; done
for r in $GX $GB $GQ $GR $GE $GM; do LB --run $r --row 1 --model haiku --effort low >/dev/null; done
for x in "$GA 1" "$GA 2" "$GA 3" "$GA2 1" "$GP 1" "$GP 2" "$GM 2" "$GM 3"; do set -- $x; LB --run $1 --row $2 --model haiku --effort low >/dev/null; done
E --run $GX --type start --json "{\"attempt\":\"$GX#1.1\",\"tool_use_id\":\"tu-gx\",\"session\":\"$S2\"}"
E --run $GA --type start --json "{\"attempt\":\"$GA#1.1\",\"tool_use_id\":\"tu-ga\",\"session\":\"$SA\"}"
E --run $GA --type start --json "{\"attempt\":\"$GA#2.1\",\"tool_use_id\":\"tu-amb\",\"session\":\"$SA\"}"
E --run $GA2 --type start --json "{\"attempt\":\"$GA2#1.1\",\"tool_use_id\":\"tu-amb\",\"session\":\"$SA\"}"
printf '%s\n' "$ORCH_RUNS_DIR/$GX" > "$ORCH_DIR/ACTIVE"   # scratch ACTIVE names X for the rest of this section
ok "gate setup: SA acquires A, A2 and P; S2 acquires Q" "$(LQ $GA $SA)$(LQ $GA2 $SA)$(LQ $GP $SA)$(LQ $GQ $S2)" "0000"
# ACTIVE is never read: an ID-less notice whose start is in ACTIVE's run X finds nothing
s0=$(snap $GX); o=$(notice agx tu-gx "[haiku/low] gate x no id" 10 1 10 | I); st=$?
ok "ACTIVE names X, ID-less notice with its start in X, ungated: none:no-attempt, exit 0" "$st|$o" "0|-: stop none, ledger none:no-attempt"
ok "and X's manifest.jsonl and ledger.jsonl are byte-identical" "$(snap $GX)" "$s0"
o=$(notice agx2 tu-gx "[haiku/low] gate x no id" 10 1 10 | I --sid $SA)
ok "ACTIVE names X, ID-less notice, --sid holding A (not X): none:no-attempt" "$o|$(snap $GX)" "-: stop none, ledger none:no-attempt|$s0"
# --sid holding A
s0=$(snap $GB); o=$(notice agb tu-gb "[haiku/low] $GB#1.1 gate b" 20 2 20 | I --sid $SA)
ok "--sid holding A, notice with B's ID: none:not-held" "$o" "$GB#1.1: stop none, ledger none:not-held"
ok "B's manifest.jsonl and ledger.jsonl are byte-identical" "$(snap $GB)" "$s0"
o=$(notice agb tu-gb "[haiku/low] $GB#1.1 gate b" 20 2 20 | I)
ok "control: the same B notice ungated (no sid) is written" "$o" "$GB#1.1: stop new, ledger written"
o=$(notice aga1 tu-ga1 "[haiku/low] $GA#1.1 gate a" 30 3 30 | I --sid $SA)
ok "--sid holding A, A's ID: written" "$o" "$GA#1.1: stop new, ledger written"
s0=$(snap $GX); o=$(notice aga tu-ga "[haiku/low] gate a no id" 31 3 31 | I --sid $SA)
ok "--sid holding A, ID-less notice with its start in A: linked to A's start" "$o|$(jq -r 'select(.type=="stop" and .task_id=="aga")|.attempt' "$(MF $GA)")" "$GA#1.1: stop new, ledger updated|$GA#1.1"
ok "and not to ACTIVE's run X" "$(snap $GX)" "$s0"
o=$(notice aga3 tu-ga "[haiku/low] gate a no id" 32 3 32 | I --sid $SN)
ok "--sid holding nothing (gated, empty set), ID-less notice with its start in A: none:no-attempt" "$o" "-: stop none, ledger none:no-attempt"
sa=$(snap $GA); sa2=$(snap $GA2); o=$(notice aamb tu-amb "[haiku/low] gate amb no id" 40 4 40 | I --sid $SA); st=$?
ok "starts in A and A2, both held, ID-less notice: none:ambiguous, exit 0" "$st|$o" "0|-: stop none, ledger none:ambiguous"
ok "ambiguous: A and A2 are byte-identical" "$(snap $GA)|$(snap $GA2)" "$sa|$sa2"
# env WFP_SID fallback; an explicit empty --sid= is no session id
s0=$(snap $GE); o=$(notice age tu-ge "[haiku/low] $GE#1.1 gate e" 50 5 50 | WFP_SID=$SA I)
ok "env WFP_SID (holding A) gates like --sid: E's ID is none:not-held, E byte-identical" "$o|$(snap $GE)" "$GE#1.1: stop none, ledger none:not-held|$s0"
o=$(notice age tu-ge "[haiku/low] $GE#1.1 gate e" 50 5 50 | WFP_SID=$SA I --sid=)
ok "--sid= with WFP_SID set: no session id, no env fallback (ungated): written" "$o" "$GE#1.1: stop new, ledger written"
ok "a bad --sid is a usage error (exit 2)" "$(rc node "$L" ingest --file "$GD/notice.txt" --sid=a/b)" "2"
# --run R
o=$(notice agr tu-gr "[haiku/low] $GR#1.1 gate r" 60 6 60 | I --sid $SN)
ok "--sid S holding nothing, R's ID, no --run: none:not-held" "$o" "$GR#1.1: stop none, ledger none:not-held"
o=$(notice agr tu-gr "[haiku/low] $GR#1.1 gate r" 60 6 60 | I --sid $SN --run $GR)
ok "--sid S holding nothing --run R (no lease on R): R is written" "$o" "$GR#1.1: stop new, ledger written"
s0=$(snap $GQ); o=$(notice agq tu-gq "[haiku/low] $GQ#1.1 gate q" 70 7 70 | I --sid $SA --run $GQ 2>"$T/gq.err")
ok "--sid S --run Q, Q held live by S2: none:not-held" "$o" "$GQ#1.1: stop none, ledger none:not-held"
ok "and Q's manifest.jsonl and ledger.jsonl are byte-identical" "$(snap $GQ)" "$s0"
ok "and stderr says --run Q was not added" "$(grep -c "^ledger: note: --run $GQ is held live by ${S2:0:8}; not added" "$T/gq.err")" "1"
o=$(notice agq tu-gq "[haiku/low] $GQ#1.1 gate q" 70 7 70 | I --sid $S2)
ok "control: Q's holder S2 writes the same notice" "$o" "$GQ#1.1: stop new, ledger written"
ok "--run without a sid: exit 2" "$(notice agr2 tu-gr2 "[haiku/low] $GR#1.1 r" 1 1 1 | rc node "$L" ingest --run $GR)" "2"
ok "--run with --sid= (no sid) and WFP_SID set: exit 2" "$(notice agr2 tu-gr2 "[haiku/low] $GR#1.1 r" 1 1 1 | WFP_SID=$SA rc node "$L" ingest --sid= --run $GR)" "2"
ok "--run with a missing run folder: exit 2" "$(notice agr2 tu-gr2 "[haiku/low] $GR#1.1 r" 1 1 1 | rc node "$L" ingest --sid $SN --run t-gate-none)" "2"
# expired own lease, then taken over
jq -c '.expires="2000-01-01T00:00:00.000Z"' "$ORCH_DIR/leases/$GP.json" > "$T/gp.json" && mv "$T/gp.json" "$ORCH_DIR/leases/$GP.json"
o=$(notice agp1 tu-gp1 "[haiku/low] $GP#1.1 gate p" 80 8 80 | I --sid $SA)
ok "S's own lease on P expired, nobody took it: P is still written" "$o" "$GP#1.1: stop new, ledger written"
ok "S2 acquires the expired P (no --force)" "$(LQ $GP $S2)" "0"
s0=$(snap $GP); o=$(notice agp2 tu-gp2 "[haiku/low] $GP#2.1 gate p" 81 8 81 | I --sid $SA)
ok "after S2 takes P over, S's ingest into P: none:not-held, P byte-identical" "$o|$(snap $GP)" "$GP#2.1: stop none, ledger none:not-held|$s0"
# the module API and --transcript give the same results as the CLI
MOD(){ node -e "const L=require('$L'); console.log(L.ingestText(require('fs').readFileSync(0,'utf8'), $1).join('|'))"; }
o=$(notice agm2 tu-gm2 "[haiku/low] $GM#2.1 gate m" 90 9 90 | MOD "{held:[]}")
ok "module ingestText(text, {held:[]}): none:not-held" "$o" "$GM#2.1: stop none, ledger none:not-held"
o=$(notice agm2 tu-gm2 "[haiku/low] $GM#2.1 gate m" 90 9 90 | MOD "{held:['$GM']}")
ok "module ingestText(text, {held:[M]}): written" "$o" "$GM#2.1: stop new, ledger written"
o=$(notice agma tu-ga "[haiku/low] gate a no id" 33 3 33 | MOD "{held:['$GA','$GM']}")
ok "module ingestText, ID-less notice, held [A, M]: linked to A's start" "$o" "$GA#1.1: stop new, ledger updated"
PGT="$ORCH_PROJECTS_DIR/pgate"; mkdir -p "$PGT"; enq "$(now)" "$(notice agm3 tu-gm3 "[haiku/low] $GM#3.1 gate m3" 95 9 95)" > "$PGT/SGT.jsonl"
TRX(){ node -e "const L=require('$L'); console.log(L.ingestTranscript('$PGT/SGT.jsonl', $1).join('|'))"; }
s0=$(snap $GM)
ok "module ingestTranscript(file, {held:[]}): none:not-held, M byte-identical" "$(TRX "{held:[]}")|$(snap $GM)" "$GM#3.1: stop none, ledger none:not-held|$s0"
ok "ingest --transcript F --sid S (S holds A, not M): none:not-held" "$(I --transcript "$PGT/SGT.jsonl" --sid $SA)|$(snap $GM)" "$GM#3.1: stop none, ledger none:not-held|$s0"
ok "ingest --transcript F --session-id S --sid=: --session-id never gates (written by the sweep)" "$(I --transcript "$PGT/SGT.jsonl" --session-id $SA --sid=)|$(jq -r 'select(.type=="stop" and .task_id=="agm3")|.via' "$(MF $GM)")" "$GM#3.1: stop new, ledger written|sweep"
ok "module ingestTranscript(file, {held:[M]}): the same notice again is a dup" "$(TRX "{held:['$GM']}")" "$GM#3.1: stop dup, ledger same"
# B1 (row 4): with no session id, only the notice's own ID run, and only while no session holds it live (lease none or expired)
GQ2=t-gate-q2; mkdir -p "$ORCH_RUNS_DIR/$GQ2"; LB --run $GQ2 --row 1 --model haiku --effort low >/dev/null
ok "B1 setup: S2 acquires Q2" "$(LQ $GQ2 $S2)" "0"
B1N(){ notice ab1 tu-b1 "[haiku/low] $GQ2#1.1 gate b1" 11 1 11; }
s0=$(snap $GQ2); o=$(B1N | I)
ok "B1: no sid, Q2's ID, Q2 held live by S2: none:not-held" "$o" "$GQ2#1.1: stop none, ledger none:not-held"
ok "B1: Q2's manifest.jsonl and ledger.jsonl are byte-identical" "$(snap $GQ2)" "$s0"
ok "B1: --sid= (no sid, WFP_SID unset): none:not-held, Q2 byte-identical" "$(B1N | I --sid=)|$(snap $GQ2)" "$GQ2#1.1: stop none, ledger none:not-held|$s0"
ok "B1: WFP_SID empty: none:not-held, Q2 byte-identical" "$(B1N | WFP_SID= I)|$(snap $GQ2)" "$GQ2#1.1: stop none, ledger none:not-held|$s0"
PB1="$ORCH_PROJECTS_DIR/pb1"; mkdir -p "$PB1"; enq "$(now)" "$(B1N)" > "$PB1/SB1.jsonl"
ok "B1: no-sid --transcript sweep: none:not-held, Q2 byte-identical" "$(I --transcript "$PB1/SB1.jsonl")|$(snap $GQ2)" "$GQ2#1.1: stop none, ledger none:not-held|$s0"
ok "B1: module ingestText(text, {}) (no held): none:not-held, Q2 byte-identical" "$(B1N | MOD "{}")|$(snap $GQ2)" "$GQ2#1.1: stop none, ledger none:not-held|$s0"
ok "B1: module ingestTranscript(file, {}) (no held): none:not-held, Q2 byte-identical" "$(node -e "const L=require('$L'); console.log(L.ingestTranscript('$PB1/SB1.jsonl', {}).join('|'))")|$(snap $GQ2)" "$GQ2#1.1: stop none, ledger none:not-held|$s0"
ok "B1: no stop event reached Q2 from the no-sid paths above" "$(jq -c 'select(.type=="stop")' "$(MF $GQ2)" | wc -l | tr -d ' ')" "0"
jq -c '.expires="2000-01-01T00:00:00.000Z"' "$ORCH_DIR/leases/$GQ2.json" > "$T/gq2.json" && mv "$T/gq2.json" "$ORCH_DIR/leases/$GQ2.json"
ok "B1: Q2's lease expired (nobody took it over): the no-sid ingest writes Q2" "$(B1N | I)" "$GQ2#1.1: stop new, ledger written"
ok "help documents --sid / --run and that ACTIVE is never read" "$(node "$L" help | grep -c -e '\[--sid S \[--run R\]\]' -e 'ACTIVE is never read')" "2"

echo "== cost (read-only token, tool and time totals per row and per run)"
CA(){ A --role implementation --agent b --mode subagent "$@" >/dev/null; }
CA --run tc1 --row 1 --model opus --effort high --tokens 1000 --tools 2 --ms 60000 --first-pass y
CA --run tc1 --row 2 --model haiku --effort low --tokens 2000 --tools 3 --ms 2000 --first-pass n
CA --run tc1 --row 2 --model sonnet --effort high --tokens 3000 --tools 4 --ms 1000 --first-pass y
CA --run tc1 --row 10 --model opus --effort high --tokens unknown --tools 1 --ms 1000
CA --run tc2 --row 1 --model haiku --effort low --tokens 500 --tools 1 --ms 1000
tree(){ find "$ORCH_DIR" -type f -print0 | sort -z | xargs -0 md5sum | md5sum; }
t0=$(tree); c=$(node "$L" cost --run tc1)
ok "cost --run: line 1 totals, '?' on a sum with an unknown attempt" "$(echo "$c" | head -1)" "cost: run tc1: 3 rows, 4 attempts, tokens 6.0k?, tools 10, time 1.1m"
ok "cost --run: rows by number (1, 2, 10)" "$(echo "$c" | sed -n '3,$p' | awk '/^[0-9]/{print $1}' | paste -sd' ')" "1 2 10"
ok "cost --run: retry row = accepted model/effort, summed numbers, retries 1, escalated y, first-pass of the first attempt" "$(echo "$c" | grep -cE '^2 +2 +sonnet/high +5\.0k +7 +3s +1 +y +n$')" "1"
ok "cost --run: null tokens row shows '?' and the unknown line counts it" "$(echo "$c" | grep -cE '^10 +1 +opus/high +\? ')|$(echo "$c" | tail -1)" "1|unknown: tokens on 1 attempt"
l=$(node "$L" cost --limit 50)
ok "cost (run list): newest first (tc2 above tc1), per-run totals" "$(echo "$l" | awk '$1=="tc1"||$1=="tc2"{print $1}' | paste -sd' ')|$(echo "$l" | grep -cE '^tc1 +3 +4 +6\.0k\? +1\.1m +1 +1$')" "tc2 tc1|1"
ok "cost --json: raw sums with unknown counts" "$(node "$L" cost --run tc1 --json | jq -c '[.tokens, .table[1].escalated, .table[1].retries]')" '[{"sum":6000,"unknown":1},true,1]'
ok "cost: a run with no ledger says so (exit 0)" "$(node "$L" cost --run tc-none; echo "exit $?")" "$(printf 'cost: run tc-none has no ledger yet\nexit 0')"
ok "cost: usage errors exit 2 (--limit 0, unknown flag)" "$(rc node "$L" cost --limit 0)$(rc node "$L" cost --bogus x)" "22"
node "$L" cost --json >/dev/null; ok "cost is read-only: scratch tree byte-identical, no lock, no metrics.db" "$(tree)|$([ -e "$ORCH_METRICS_DB" ] && echo db)" "$t0|"

echo "== row 19 (P29): model_id from the transcript head past 262,144 B"
# bigtx <file> <attachment model or ''> <assistant model> <assistant effort or ''>: about 360 KB of user records before the first assistant record
bigtx(){ node -e '
  const [f, att, mm, eff] = process.argv.slice(1), L = [];
  L.push(JSON.stringify({ type: "user", message: { role: "user", content: "brief" } }));
  if (att) L.push(JSON.stringify({ type: "attachment", attachment: { type: "model", identity: { modelId: att } } }));
  for (let i = 0; i < 36; i++) L.push(JSON.stringify({ type: "user", message: { role: "user", content: "x".repeat(10000) } }));
  const a = { type: "assistant", message: { model: mm, usage: { input_tokens: 1, output_tokens: 1 } } }; if (eff) a.effort = eff;
  L.push(JSON.stringify(a)); require("fs").writeFileSync(f, L.join("\n") + "\n");' "$@"; }
R=t-p29; D="$ORCH_PROJECTS_DIR/p29/S29/subagents"; mkdir -p "$D" "$ORCH_RUNS_DIR/$R"; : > "$ORCH_PROJECTS_DIR/p29/S29.jsonl"
bigtx "$D/agent-ap291.jsonl" claude-opus-5-5 claude-opus-5-5 high
bigtx "$D/agent-ap292.jsonl" "" claude-sonnet-5-5 medium
ok "fixtures: the first assistant record sits past byte 262,144" "$(node -e 'const s=require("fs").readFileSync(process.argv[1],"utf8"); console.log(s.indexOf("\"type\":\"assistant\"") > 262144)' "$D/agent-ap291.jsonl")|$(node -e 'const s=require("fs").readFileSync(process.argv[1],"utf8"); console.log(s.indexOf("\"type\":\"assistant\"") > 262144)' "$D/agent-ap292.jsonl")" "true|true"
ok "transcriptFacts: attachment model and the assistant record's effort past 256 KB" "$(node -e 'const t=require(process.argv[1]).transcriptFacts(process.argv[2], false); console.log(t.model_id, t.effort_observed)' "$L" "$D/agent-ap291.jsonl")" "claude-opus-5-5 high"
ok "transcriptFacts: no attachment, the assistant record's model past 256 KB" "$(node -e 'const t=require(process.argv[1]).transcriptFacts(process.argv[2], false); console.log(t.model_id, t.effort_observed)' "$L" "$D/agent-ap292.jsonl")" "claude-sonnet-5-5 medium"
ok "transcriptFacts with a 256 KB cap (the substop hook's read): the attachment alone gives the model" "$(node -e 'const t=require(process.argv[1]).transcriptFacts(process.argv[2], false, 262144); console.log(t.model_id, t.effort_observed)' "$L" "$D/agent-ap291.jsonl")" "claude-opus-5-5 null"
ok "teeth: with no attachment a 256 KB cap (the old read) misses the model" "$(node -e 'const t=require(process.argv[1]).transcriptFacts(process.argv[2], false, 262144); console.log(t.model_id)' "$L" "$D/agent-ap292.jsonl")" "null"
for k in 1 2; do
  LB --run $R --row $k --model opus --effort high >/dev/null
  E --run $R --type start --json "{\"attempt\":\"$R#$k.1\",\"tool_use_id\":\"tu-p29$k\",\"transcript_path\":\"$ORCH_PROJECTS_DIR/p29/S29.jsonl\",\"session\":\"S29\",\"agent\":\"orch-builder\"}"
  echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $R#$k.1 p29\",\"toolUseId\":\"tu-p29$k\",\"model\":\"opus\",\"effort\":\"high\",\"finished\":true}" > "$D/agent-ap29$k.meta.json"
  notice ap29$k tu-p29$k "[opus/high] $R#$k.1 p29" 50 1 10 | I >/dev/null
done
ok "ingest: the ledger lines get model_id from past 256 KB (attachment, then assistant record)" "$(jq -r '"\(.id) \(.model_id)"' "$(F $R)" | paste -sd'|')" "$R#1.1 claude-opus-5-5|$R#2.1 claude-sonnet-5-5"
ok "ingest: the stop's effort_observed is read past 256 KB" "$(jq -r 'select(.type=="stop")|.effort_observed' "$(MF $R)" | paste -sd' ')" "high medium"

echo "== row 19 (P31): cause on update, add and close"
R=t-cause; mkdir -p "$ORCH_RUNS_DIR/$R"
CA --run $R --row 1 --model sonnet --effort high --tokens 100 --tools 1 --ms 1
e=$(node "$L" update --run $R --row 1 --first-pass n 2>&1 >/dev/null); st=$?
ok "update --first-pass n without --cause: exit 0, cause unstated, one warning line naming the flag and its values" "$st|$(jq -r .cause "$(F $R)")|$e" "0|unstated|ledger: warning: update --first-pass n without --cause; recorded as unstated (--cause owner-tune|review-findings|failed|tooling)"
node "$L" update --run $R --row 1 --cause review-findings >/dev/null 2>&1; ok "update --cause sets the stated cause over unstated" "$(jq -r .cause "$(F $R)")" "review-findings"
ok "update --cause bogus refused (exit 2)" "$(rc node "$L" update --run $R --row 1 --cause bogus)" "2"
CA --run $R --row 2 --model sonnet --effort high --tokens 100 --tools 1 --ms 1
e=$(node "$L" update --run $R --row 2 --first-pass n --cause failed 2>&1 >/dev/null); ok "update --first-pass n --cause failed: no warning, cause failed" "$e|$(jq -r 'select(.row==2)|.cause' "$(F $R)")" "|failed"
node "$L" update --run $R --row 2 --first-pass n 2>"$T/e.c2" >/dev/null; ok "a later --first-pass n keeps the stated cause (no warning)" "$(jq -r 'select(.row==2)|.cause' "$(F $R)")|$(wc -c < "$T/e.c2" | tr -d ' ')" "failed|0"
e=$(A --run $R --row 3 --role implementation --agent b --mode subagent --model sonnet --effort high --tokens 1 --tools 1 --ms 1 --first-pass n 2>&1 >/dev/null)
ok "add --first-pass n without --cause: cause unstated plus the warning" "$(jq -r 'select(.row==3)|.cause' "$(F $R)")|$e" "unstated|ledger: warning: add --first-pass n without --cause; recorded as unstated (--cause owner-tune|review-findings|failed|tooling)"
A --run $R --row 4 --role implementation --agent b --mode subagent --model sonnet --effort high --tokens 1 --tools 1 --ms 1 --first-pass n --cause tooling >/dev/null 2>&1
ok "add --first-pass n --cause tooling" "$(jq -r 'select(.row==4)|.cause' "$(F $R)")" "tooling"
ok "add --cause on a first line without --first-pass n refused (exit 2), nothing written" "$(rc A --run $R --row 5 --role implementation --agent b --mode subagent --model sonnet --effort high --tokens 1 --tools 1 --ms 1 --cause failed)|$(jq -c 'select(.row==5)' "$(F $R)" | wc -l | tr -d ' ')" "2|0"
p1=$(sed -n 1p "$(F $R)")
e=$(A --run $R --row 1 --role implementation --agent b --mode subagent --model opus --effort high --tokens 200 --tools 2 --ms 2 2>&1 >/dev/null)
ok "add of a retry without --cause: retry_cause unstated on the new line plus the warning" "$(jq -r 'select(.id=="'$R'#1.2")|.retry_cause' "$(F $R)")|$e" "unstated|ledger: warning: add of a retry without --cause; recorded as unstated (--cause owner-tune|review-findings|failed|tooling)"
ok "add of a retry leaves the earlier line byte-identical (append-only)" "$(sed -n 1p "$(F $R)")" "$p1"
A --run $R --row 2 --role implementation --agent b --mode subagent --model opus --effort high --tokens 200 --tools 2 --ms 2 --cause failed >/dev/null 2>&1
ok "add of a retry --cause failed: retry_cause failed" "$(jq -r 'select(.id=="'$R'#2.2")|.retry_cause' "$(F $R)")" "failed"
ok "a first line carries no retry_cause" "$(jq -r 'select(.id=="'$R'#1.1")|has("retry_cause")' "$(F $R)")" "false"
# close: the run needs a manifest for the outcome event
LB --run $R --row 9 --model sonnet --effort high >/dev/null; CA --run $R --row 9 --attempt "$R#9.1" --model sonnet --effort high --tokens 1 --tools 1 --ms 1
ok "close without --cause on --first-pass n refused (exit 2)" "$(rc node "$L" close --attempt "$R#9.1" --first-pass n --outcome retry)" "2"
ok "close without --cause on --outcome retry refused, even with --first-pass y (exit 2)" "$(rc node "$L" close --attempt "$R#9.1" --first-pass y --outcome retry)" "2"
ok "close --cause unstated refused (exit 2)" "$(rc node "$L" close --attempt "$R#9.1" --first-pass n --outcome retry --cause unstated)" "2"
ok "close --first-pass y --cause failed refused (exit 2)" "$(rc node "$L" close --attempt "$R#9.1" --first-pass y --outcome accepted --cause failed)" "2"
ok "close missing --outcome refused (exit 2); close --bogus refused (exit 2)" "$(rc node "$L" close --attempt "$R#9.1" --first-pass y)$(rc node "$L" close --attempt "$R#9.1" --first-pass y --outcome accepted --bogus 1)" "22"
ok "close --outcome bogus refused (exit 2)" "$(rc node "$L" close --attempt "$R#9.1" --first-pass y --outcome bogus)" "2"
ok "the refused closes left the line unset" "$(jq -c 'select(.id=="'$R'#9.1")|[.firstPass,.cause,.outcome]' "$(F $R)")" "[null,null,null]"
o=$(node "$L" close --attempt "$R#9.1" --first-pass n --outcome retry --cause review-findings 2>&1); st=$?
ok "close: exit 0 and one 'closed' line" "$st|$(echo "$o" | grep -c "^ledger: closed $R#9.1: firstPass=false cause=review-findings outcome=retry closed=")" "0|1"
ok "close: first-pass, cause, outcome and closed on the line" "$(jq -c 'select(.id=="'$R'#9.1")|[.firstPass,.cause,.outcome,(.closed|type)]' "$(F $R)")" '[false,"review-findings","retry","string"]'
ok "close: one outcome event via close with cause and outcome" "$(jq -c 'select(.type=="outcome" and .attempt=="'$R'#9.1")|[.via,.first_pass,.cause,.outcome,.set]' "$(MF $R)")" '["close",false,"review-findings","retry",["firstPass","cause","outcome","closed"]]'
node "$L" close --attempt "$R#9.1" --first-pass y --outcome accepted >/dev/null 2>&1; ok "close --first-pass y --outcome accepted without --cause (exit 0)" "$(jq -c 'select(.id=="'$R'#9.1")|[.firstPass,.outcome]' "$(F $R)")" '[true,"accepted"]'
ok "close --first-pass y --cause owner-tune accepted (exit 0)" "$(rc node "$L" close --attempt "$R#9.1" --first-pass y --outcome retry --cause owner-tune)|$(jq -r 'select(.id=="'$R'#9.1")|.cause' "$(F $R)")" "0|owner-tune"
ok "update --attempt still writes via ledger (no outcome field)" "$(node "$L" update --attempt "$R#9.1" --retries 1 >/dev/null 2>&1; jq -c 'select(.type=="outcome")' "$(MF $R)" | tail -1 | jq -c '[.via,has("outcome")]')" '["ledger",false]'

echo "== row 19: ingest carries the launch's cause and the substop's model_id"
R=t-rci; mkdir -p "$ORCH_RUNS_DIR/$R"
LB --run $R --row 1 --model sonnet --effort high >/dev/null
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"arci1\",\"phase\":\"agent-stopped\",\"final\":false,\"via\":\"substop\",\"model_id\":\"claude-sonnet-5-5\"}"
notice arci1 tu-rci1 "[sonnet/high] $R#1.1 rci" 100 1 10 | I >/dev/null
ok "no transcript, but the substop stop has model_id: the ledger line gets it" "$(jq -r 'select(.id=="'$R'#1.1")|.model_id' "$(F $R)")" "claude-sonnet-5-5"
ok "a first attempt's line has no retry_cause" "$(jq -r 'select(.id=="'$R'#1.1")|has("retry_cause")' "$(F $R)")" "false"
node "$M" launch --run $R --row 1 --role implementation --agent orch-builder --mode subagent --repo r --area a --model opus --effort high --cause failed >/dev/null 2>&1
ok "the fresh retry launch flipped the ingested predecessor" "$(jq -c 'select(.id=="'$R'#1.1")|[.firstPass,.cause]' "$(F $R)")" '[false,"failed"]'
notice arci2 tu-rci2 "[opus/high] $R#1.2 rci" 200 1 10 | I >/dev/null
ok "the retry's ingested line carries retry_cause from its launch event" "$(jq -c 'select(.id=="'$R'#1.2")|[.retries,.retry_kind,.retry_cause]' "$(F $R)")" '[1,"fresh","failed"]'

echo "== row 19: a line with no cause still reads"
R=t-old; mkdir -p "$ORCH_RUNS_DIR/$R"
printf '%s\n' "{\"ts\":\"2026-11-01T00:00:00.000Z\",\"id\":\"$R#1.1\",\"run\":\"$R\",\"row\":1,\"role\":\"implementation\",\"agent\":\"b\",\"model\":\"sonnet\",\"effort\":\"high\",\"mode\":\"subagent\",\"repo\":\"r\",\"area\":\"a\",\"tokens\":1000,\"tools\":1,\"ms\":1000,\"retries\":0,\"escalated\":false,\"firstPass\":false}" \
  "{\"ts\":\"2026-11-01T01:00:00.000Z\",\"id\":\"$R#1.2\",\"run\":\"$R\",\"row\":1,\"role\":\"implementation\",\"agent\":\"b\",\"model\":\"opus\",\"effort\":\"high\",\"mode\":\"subagent\",\"repo\":\"r\",\"area\":\"a\",\"tokens\":2000,\"tools\":1,\"ms\":1000,\"retries\":1,\"escalated\":true,\"firstPass\":true}" > "$(F $R)"
ok "show reads a ledger with no cause fields (exit 0, role line)" "$(node "$L" show --run $R 2>&1 | grep -c '^role implementation: 1 rows, 2 attempts, tokens 3.0k, accepted 1')" "1"
ok "cost reads it (exit 0)" "$(rc node "$L" cost --run $R)" "0"
ok "update on an old line adds no cause unless asked" "$(node "$L" update --attempt "$R#1.2" --retries 1 >/dev/null 2>&1; jq -r 'select(.id=="'$R'#1.2")|has("cause")' "$(F $R)")" "false"

echo "== row 19: report first-pass split by cause"
R=t-uns; mkdir -p "$ORCH_RUNS_DIR/$R"; CA --run $R --row 1 --model sonnet --effort high --tokens 10 --tools 1 --ms 1; CA --run $R --row 1 --model opus --effort high --tokens 30 --tools 1 --ms 1 2>/dev/null
RP=$(node "$L" report first-pass --run t-cause 2>/dev/null); st=$?
ok "report first-pass exit 0" "$st" "0"
ok "report: one 'by cause' line right after 'retry kind (claude):'" "$(echo "$RP" | grep -A1 '^retry kind (claude):' | sed -n 2p | grep -c '^retry kind (claude) by cause: ')" "1"
cz(){ grep '^retry kind (claude) by cause: ' | grep -oE '(owner-tune|review-findings|failed|tooling|unstated|unlabelled) [0-9]+|not stated [0-9]+ of [0-9]+' | paste -sd' '; }
ok "report: the retried line's cause (update --cause) and the retry line's cause (add --cause)" "$(echo "$RP" | cz)" "owner-tune 0 review-findings 1 failed 1 tooling 0 unstated 0 unlabelled 0 not stated 0 of 2"
ok "report: a retry added without --cause counts as unstated" "$(node "$L" report first-pass --run t-uns 2>/dev/null | cz)" "owner-tune 0 review-findings 0 failed 0 tooling 0 unstated 1 unlabelled 0 not stated 1 of 1"
ok "report: lines from before the fields count as unlabelled" "$(node "$L" report first-pass --run t-old 2>/dev/null | cz)" "owner-tune 0 review-findings 0 failed 0 tooling 0 unstated 0 unlabelled 1 not stated 1 of 1"
ok "report: the share line names the unstated percentage" "$(node "$L" report first-pass --run t-uns 2>/dev/null | grep -c 'not stated 1 of 1 retries (100%)$')" "1"
J=$(node "$L" report first-pass --run t-cause --json 2>/dev/null)
ok "report --json: retry_causes.claude (retries, not_stated, failed retries and next-attempt tokens)" "$(echo "$J" | jq -c '.retry_causes.claude | [.retries, .not_stated, .causes.failed.retries, .causes.failed.tokens, .causes["review-findings"].tokens]')" "[2,0,1,200,200]"
ok "report on a run with no retries: no 'by cause' line, no retry_causes" "$(node "$L" report first-pass --run t-p29 2>/dev/null | grep -c 'by cause')|$(node "$L" report first-pass --run t-p29 --json 2>/dev/null | jq 'has("retry_causes")')" "0|false"

echo "== win 4: report retries (by cause, role, model/effort; read-only, no db)"
R=t-rt; mkdir -p "$ORCH_RUNS_DIR/$R"; rm -f "$ORCH_METRICS_DB"
ln(){ printf '{"ts":"%s","id":"%s#%s.%s","run":"%s","row":%s,"role":"%s","agent":"b","model":"%s","effort":"%s","mode":"subagent","repo":"rr","area":"aa","tokens":%s,"tools":1,"ms":1000,"firstPass":%s%s}\n' "$1" "$R" "$2" "$3" "$R" "$2" "$4" "$5" "$6" "$7" "$8" "$9"; }
{ ln 2026-11-01T00:00:00.000Z 1 1 implementation sonnet high 1000 false ',"cause":"review-findings"'
  ln 2026-11-01T01:00:00.000Z 1 2 implementation sonnet high 500 true ',"retry_cause":"review-findings"'
  ln 2026-11-01T02:00:00.000Z 2 1 implementation sonnet high 2000 false ''
  ln 2026-11-01T03:00:00.000Z 2 2 implementation opus high 700 true ',"retry_cause":"failed"'
  ln 2026-11-01T04:00:00.000Z 3 1 review opus high 3000 false ',"cause":"unstated"'
  ln 2026-11-01T05:00:00.000Z 4 1 review opus high 100 true ''
  ln 2026-11-01T06:00:00.000Z 5 1 implementation sonnet high 4000 false ''
  ln 2026-11-01T07:00:00.000Z 5 2 implementation sonnet high 800 true ',"retry_cause":"tooling"'
  ln 2026-11-01T08:00:00.000Z 6 1 planning opus xhigh 50 false ''; } > "$ORCH_RUNS_DIR/$R/ledger.jsonl"
RJ=$(node "$L" report retries --run $R --json); st=$?
ok "report retries: exit 0" "$st" "0"
ok "report retries --json: 5 retried of 9 attempts, tokens 10050 of 12150" "$(echo "$RJ" | jq -c '[.lines, .retried, .retried_tokens, .tokens]')" "[9,5,10050,12150]"
ok "report retries: the line's own cause wins, else the next attempt's retry_cause, else unstated" "$(echo "$RJ" | jq -cS '.causes | to_entries | map({(.key): [.value.attempts, .value.tokens]}) | add')" '{"failed":[1,2000],"review-findings":[1,1000],"tooling":[1,4000],"unstated":[2,3050]}'
ok "report retries: a literal 'unstated' and a missing cause with no retry_cause both count as unstated" "$(echo "$RJ" | jq -r '.unstated')" "2"
ok "report retries: groups carry cause, role, model/effort" "$(echo "$RJ" | jq -r '.groups[] | select(.cause=="failed") | [.role,.model,.effort,.attempts] | join(" ")')" "implementation sonnet high 1"
RT=$(node "$L" report retries --run $R)
ok "report retries (text): header counts and the unstated share" "$(echo "$RT" | head -1)" "retries by cause: 5 of 9 attempts needed a retry (firstPass n), tokens 10.1k of 12.2k (83%); cause not stated on 2 of 5 (40%)"
ok "report retries (text): a table row per cause/role/model" "$(echo "$RT" | grep -c '^tooling  *implementation  *sonnet/high  *1 ')" "1"
ok "report retries --role review keeps only that role" "$(node "$L" report retries --run $R --role review --json | jq -c '[.lines, .retried, .retried_tokens]')" "[2,1,3000]"
ok "report retries: no matching attempts says so (exit 0)" "$(node "$L" report retries --run nosuch 2>&1)|$(node "$L" report retries --run nosuch >/dev/null 2>&1; echo $?)" "ledger: no attempts match|0"
ok "report retries: --repo/--area filters apply (none match)" "$(node "$L" report retries --repo rr --area zz 2>&1 | head -1)" "ledger: no attempts match"
ok "report retries is read-only: no metrics.db is created" "$([ -e "$ORCH_METRICS_DB" ] && echo exists || echo absent)" "absent"
ok "report retries: help documents it" "$(node "$L" help | grep -c '^  report retries ')" "1"
ok "report bogus name lists retries (exit 2)" "$(node "$L" report bogus 2>&1 | grep -c 'tokens-per-accepted | first-pass | retries')" "1"

echo "== row 19: backfill-models"
R=t-bf; D="$ORCH_PROJECTS_DIR/pbf/SBF/subagents"; mkdir -p "$D" "$ORCH_RUNS_DIR/$R"
LB --run $R --row 1 --model opus --effort high >/dev/null; LB --run $R --row 2 --model opus --effort high >/dev/null; LB --run $R --row 3 --model opus --effort high >/dev/null
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"abf1\",\"phase\":\"finished\",\"final\":true,\"model_id\":\"claude-opus-5-5\"}"
E --run $R --type start --json "{\"attempt\":\"$R#2.1\",\"tool_use_id\":\"tu-bf2\",\"transcript_path\":\"$ORCH_PROJECTS_DIR/pbf/SBF.jsonl\",\"session\":\"SBF\",\"agent\":\"orch-builder\"}"
E --run $R --type stop --json "{\"attempt\":\"$R#2.1\",\"task_id\":\"abf2\",\"phase\":\"finished\",\"final\":true,\"session\":\"SBF\"}"
bigtx "$D/agent-abf2.jsonl" claude-opus-5-5 claude-opus-5-5 high
E --run $R --type stop --json "{\"attempt\":\"$R#3.1\",\"task_id\":\"abf3\",\"phase\":\"finished\",\"final\":true}"
for k in 1 2 3; do CA --run $R --row $k --attempt "$R#$k.1" --model opus --effort high --tokens 1 --tools 1 --ms 1; done
CA --run $R --row 4 --model sonnet --effort high --tokens 1 --tools 1 --ms 1
A --run $R --row 5 --role review --agent codex --mode wave --model gpt-6-astra --effort ultra --tokens 1 --tools 1 --ms 1 >/dev/null
CA --run $R --row 6 --attempt "$R#6.1" --model sonnet --effort high --tokens 1 --tools 1 --ms 1
node -e 'const f=process.argv[1],fs=require("fs");fs.writeFileSync(f,fs.readFileSync(f,"utf8").split("\n").map(l=>{if(!l)return l;const x=JSON.parse(l);if(x.id===process.argv[2])x.model_id="claude-sonnet-5-5";return JSON.stringify(x)}).join("\n"))' "$(F $R)" "$R#6.1"
b0=$(md5sum < "$(F $R)"); m0=$(md5sum < "$(MF $R)")
o=$(node "$L" backfill-models --run $R --dry-run); st=$?
ok "backfill dry run: exit 0 and the summary line" "$st|$(echo "$o" | head -1)" "0|backfill-models (dry run: nothing written): 1 runs, 6 lines: already 1, resolved 2 (stop 1, transcript 1), unresolved 3"
ok "backfill dry run: resolved models and every unresolved line with its reason" "$(echo "$o" | sed -n '2,$p' | paste -sd'|')" "resolved: claude-opus-5-5 2|unresolved: codex 1, no-launch 1, no-transcript 1|  $R#3.1: no-transcript|  $R#4.1: no-launch|  $R#5.1: codex"
ok "backfill dry run writes nothing (ledger and manifest byte-identical)" "$(md5sum < "$(F $R)")|$(md5sum < "$(MF $R)")" "$b0|$m0"
ok "backfill --json: counts and reasons" "$(node "$L" backfill-models --run $R --dry-run --json | jq -c '[.dry_run,.lines,.already,.resolved,.from_stop,.from_transcript,.unresolved,.reasons.codex,.reasons["no-launch"],.reasons["no-transcript"]]')" "[true,6,1,2,1,1,3,1,1,1]"
o=$(node "$L" backfill-models --run $R)
ok "backfill: writes the two resolved lines" "$(echo "$o" | head -1)" "backfill-models: 1 runs, 6 lines: already 1, resolved 2 (stop 1, transcript 1), written 2, unresolved 3"
ok "backfill: model_id and model_id_from on the filled lines only" "$(jq -r '"\(.id) \(.model_id) \(.model_id_from)"' "$(F $R)" | paste -sd'|')" "$R#1.1 claude-opus-5-5 backfill|$R#2.1 claude-opus-5-5 backfill|$R#3.1 null null|$R#4.1 null null|$R#5.1 null null|$R#6.1 claude-sonnet-5-5 null"
ok "backfill never edits the manifest" "$(md5sum < "$(MF $R)")" "$m0"
b1=$(md5sum < "$(F $R)"); o=$(node "$L" backfill-models --run $R)
ok "backfill again: idempotent (already 3, written 0, ledger byte-identical)" "$(echo "$o" | head -1)|$(md5sum < "$(F $R)")" "backfill-models: 1 runs, 6 lines: already 3, resolved 0 (stop 0, transcript 0), written 0, unresolved 3|$b1"
ok "backfill --since after every line: 0 lines" "$(node "$L" backfill-models --run $R --since 2999-01-01 --dry-run | head -1)" "backfill-models (dry run: nothing written): 1 runs, 0 lines since 2999-01-01: already 0, resolved 0 (stop 0, transcript 0), unresolved 0"
ok "backfill usage errors exit 2 (bad --since, unknown flag, unknown run)" "$(rc node "$L" backfill-models --since nope)$(rc node "$L" backfill-models --bogus 1)$(rc node "$L" backfill-models --run t-none-bf)" "222"

echo "== help and module"
ok "Q2: help has the continuation sentence" "$(node "$L" help | grep -c "A SendMessage continuation gets its own line with its notice's tokens as reported; that context includes the predecessor's, so the row's token sum counts the shared context twice (no delta is computed).")" "1"
ok "require() prints nothing and loads no sqlite" "$(node -e "require('$L'); console.log(process.moduleLoadList.some(m=>/sqlite/.test(m)))" 2>&1)" "false"
ok "exports" "$(node -e "console.log(Object.keys(require('$L')).join(','))")" "parseNotices,ingestText,ingestTranscript,ensureLedgerLine,transcriptFacts,main"
ok "unknown command lists ingest (exit 2)" "$(node "$L" bogus 2>&1 | grep -c "(add | update | show | ingest | help)")" "1"
ok "live ACTIVE kept and free of this suite's paths (content check; peer-written, so no md5)" "$(activecheck)" "ok"
ok "no live lease, run manifest or ledger line, run dir or errors.log line names a sid or run of this suite" "$(liveleak "$LIVE" "$T")" "0"
liveleak "$LIVE" "$T" >/dev/null   # a plain call, so SUITE_SIDS / SUITE_RUNS are set in this shell
ok "derived lists hold this suite's own ids (the live check is not vacuous)" "$(jq -rn --argjson s "$SUITE_SIDS" --argjson r "$SUITE_RUNS" '["SF9","t-gold","t-f18"] - ($s + $r) | join(",")')" ""

echo "== hardening: council/background-bash rows, flag-naming errors, run paths, turn-limit stop"
R=t-cn
ok "add --model council-5seat --mode background-bash accepted" "$(rc node "$L" add --repo r --area a --run $R --row 1 --role review --agent council --model council-5seat --effort high --mode background-bash --tokens 9k --tools 1 --ms 120s)" "0"
ok "council row stored with its model and mode" "$(jq -c '[.model,.mode]' "$(F $R)")" '["council-5seat","background-bash"]'
ok "mode 'Background Bash (note)' normalises" "$(node "$L" add --repo r --area a --run $R --row 2 --role review --agent council --model council --effort high --mode "Background Bash (council)" --tokens 1 --tools 1 --ms 1 >/dev/null; jq -r .mode "$(F $R)" | tail -1)" "background-bash"
s=$(node "$L" show --run $R)
ok "council rows sit in the non-Claude section, never the Claude block" "$(echo "$s" | awk '/^== codex/{f=1} f && /^council/{n++} END{print n+0}')|$(echo "$s" | awk '/^== codex/{exit} /^council/{n++} END{print n+0}')" "2|0"
ok "council-x refused (exit 2)" "$(rc node "$L" add --repo r --area a --run $R --row 3 --role review --agent x --model council-x --effort high --mode wave --tokens 1 --tools 1 --ms 1)" "2"
err=$(node "$L" add --repo r --area a --run $R --row 3 --role review --agent x --model council-x --effort high --mode wave --tokens 1 --tools 1 --ms 1 2>&1)
ok "...and the error names the flag and its value" "$(echo "$err" | grep -c "ledger: error: --model 'council-x'")" "1"
ok "help mentions both values" "$(node "$L" help | grep -c 'modes: .*background-bash')|$(node "$L" help | grep -c 'council-<N>seat')" "1|3"
err=$(node "$L" add --run $R --row 4 --tokens --tools 5 2>&1)
ok "a flag followed by a flag: names it and what followed" "$(echo "$err" | grep -c "ledger: error: --tokens needs a value (the next argument is the flag '--tools')")" "1"
err=$(node "$L" add --run $R --row 4 --bogus 1 2>&1)
ok "add unknown flag lists the valid flags" "$(echo "$err" | grep -c "add: unknown flag --bogus (flags: --run --row")" "1"
err=$(node "$L" add --run $R --run b 2>&1)
ok "a flag given twice shows both values" "$(echo "$err" | grep -cF -- "--run is given twice ('t-cn' and 'b')")" "1"
ok "run path under the runs dir works for show" "$(node "$L" show --run "$ORCH_RUNS_DIR/$R" | grep -c "ledger: .* attempts (run=$R")" "1"
ok "run path with a trailing slash works for add" "$(rc node "$L" add --repo r --area a --run "$ORCH_RUNS_DIR/$R/" --row 5 --role review --agent x --model opus --effort high --mode wave --tokens 1 --tools 1 --ms 1)" "0"
ok "...and the line is stored under the folder name" "$(jq -r .run "$(F $R)" | tail -1)" "$R"
err=$(node "$L" show --run "$T/elsewhere/$R" 2>&1)
ok "a path outside the runs dir is refused, naming flag and value" "$(echo "$err" | grep -c "ledger: error: --run '$T/elsewhere/$R'")" "1"
# seed 9: a turn-limit notice is a final stop
R=t-tl; TD="$T/tl"; mkdir -p "$TD" "$ORCH_RUNS_DIR/$R"
ok "turn-limit: launch allocates t-tl#1.1" "$(LB --run $R --row 1 --model sonnet --effort high)" "$R#1.1"
TLS='stopped at its 80-turn limit (partial result; SendMessage to task-id to continue)'
notice tlaaa111 toolu_tl1 "[sonnet/high] $R#1.1 Audit" 5000 9 40000 completed "$TLS" "PARTIAL" > "$TD/n.txt"
echo '{"agentType":"orch-builder","description":"[sonnet/high] t-tl#1.1 Audit","toolUseId":"toolu_tl1","model":"sonnet","effort":"high","finished":true}' > "$TD/agent-tlaaa111.meta.json"
o=$(I --file "$TD/n.txt" --meta "$TD/agent-tlaaa111.meta.json")
ok "turn-limit notice is recorded (not 'not an agent notice')" "$o" "$R#1.1: stop new, ledger written"
ok "its stop event: phase stopped-turn-limit, final, status turn-limit" "$(jq -c 'select(.type=="stop")|[.phase,.final,.status]' "$(MF $R)")" '["stopped-turn-limit",true,"turn-limit"]'
ok "its ledger line carries the tokens" "$(jq -r .tokens "$(F $R)")" "5000"
ok "launch --continue accepts it (exit 0, new ID)" "$(node "$M" launch --run $R --row 1 --role implementation --agent orch-builder --mode subagent --repo r --area a --model sonnet --effort high --continue "$R#1.1" --cause failed 2>/dev/null | head -1)" "$R#1.2"
# review gap 1: the final stop is chosen by its own ts, never by ingest order; a turn-limit notice never overwrites a ledger line from a real notice
TLP="$ORCH_PROJECTS_DIR/tl/STL.jsonl"; mkdir -p "$(dirname "$TLP")"
tlstate(){ node -e "const M=require('$CL/lib/manifest.js'); const a=M.fold('$1').attempts.find(x=>x.id==='$2'); console.log(a?a.state:'none')"; }
tlrun(){ # run: the launch, then the notices in the order given as 'KIND:TS' (KIND fin|tl) swept from one transcript each, in that order
  local r=$1; shift; mkdir -p "$ORCH_RUNS_DIR/$r"; LB --run $r --row 1 --model sonnet --effort high >/dev/null
  local k; for k in "$@"; do
    case "${k%%:*}" in
      fin) enq "${k#*:}" "$(notice tlfff111 toolu_tl9 "[sonnet/high] $r#1.1 Audit" 51248 87 924509 completed finished FINAL)" > "$TLP" ;;
      tl) enq "${k#*:}" "$(notice tlfff111 toolu_tl9 "[sonnet/high] $r#1.1 Audit" 212215 172 1675465 completed "$TLS" PARTIAL)" > "$TLP" ;;
    esac
    TLOUT=$(I --transcript "$TLP")
  done; }
tlrun t-tl2 fin:2026-11-18T00:52:27.000Z tl:2026-11-18T00:49:34.000Z
ok "late turn-limit (older ts): its notice is recorded, the line is left alone" "$TLOUT" "t-tl2#1.1: stop new, ledger same"
ok "...the ledger line keeps the finished notice's numbers" "$(jq -c '[.tokens,.tools,.ms]' "$(F t-tl2)")" "[51248,87,924509]"
ok "...and there is still one line" "$(wc -l < "$(F t-tl2)" | tr -d ' ')" "1"
ok "...the attempt reads finished (ts decides, not seq)" "$(tlstate t-tl2 t-tl2#1.1)" "finished"
tlrun t-tl3 tl:2026-11-18T00:49:34.000Z fin:2026-11-18T00:52:27.000Z
ok "turn-limit then a real continuation finish: the finished notice heals the line" "$TLOUT" "t-tl3#1.1: stop new, ledger updated"
ok "...the line holds the finished numbers, not summed with the partial ones" "$(jq -c '[.tokens,.tools,.ms]' "$(F t-tl3)")" "[51248,87,924509]"
ok "...the attempt reads finished" "$(tlstate t-tl3 t-tl3#1.1)" "finished"
ok "...and it is not flagged renotice (a turn-limit notice is not a repeat of the finish)" "$(node "$M" show --run t-tl3 | grep -c renotice)" "0"
ok "two real final notices still are" "$(E "--run" t-tl3 --type stop --json "{\"attempt\":\"t-tl3#1.1\",\"task_id\":\"tlfff111\",\"phase\":\"finished\",\"status\":\"completed\",\"key\":\"again\"}"; node "$M" show --run t-tl3 | grep -c renotice)" "1"
tlrun t-tl4 fin:2026-11-18T00:52:27.000Z tl:2026-11-18T00:59:00.000Z
ok "a turn-limit stop that is really later: the attempt reads stopped-turn-limit" "$(tlstate t-tl4 t-tl4#1.1)" "stopped-turn-limit"
ok "...but the ledger line written from the real notice is not overwritten" "$(jq -c '[.tokens,.tools,.ms]' "$(F t-tl4)")" "[51248,87,924509]"
tlrun t-tl5 tl:2026-11-18T00:49:34.000Z
ok "a turn-limit notice alone still writes the line and reads stopped-turn-limit" "$(jq -c '[.tokens,.tools]' "$(F t-tl5)")|$(tlstate t-tl5 t-tl5#1.1)" "[212215,172]|stopped-turn-limit"
tlrun t-tl6 fin:2026-11-18T00:49:34.000Z tl:2026-11-18T00:49:34.000Z
ok "equal ts: the real stop wins over the turn-limit one" "$(tlstate t-tl6 t-tl6#1.1)" "finished"
# launch --continue replay: only the predecessor's lines unless --verbose
R=t-rp; mkdir -p "$ORCH_RUNS_DIR/$R"; RPT="$ORCH_PROJECTS_DIR/rp/SRP.jsonl"; mkdir -p "$(dirname "$RPT")"
ok "replay: launch allocates t-rp#1.1" "$(LB --run $R --row 1 --model sonnet --effort high)" "$R#1.1"
{ enq "$(now)" "$(notice rpaaa111 toolu_rp1 "[sonnet/high] $R#1.1 One" 100 1 10)"
  enq "$(now)" "$(notice rpbbb222 toolu_rp2 "[sonnet/high] other-run#7.1 Two" 100 1 10)"
  enq "$(now)" "$(notice rpccc333 toolu_rp3 "[sonnet/high] other-run#8.1 Three" 100 1 10)"; } > "$RPT"
E "--run" $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"toolu_rp1\",\"transcript_path\":\"$RPT\",\"session\":\"sess-rp\"}"
node "$M" launch --run $R --row 1 --role implementation --agent orch-builder --mode subagent --repo r --area a --model sonnet --effort high --continue "$R#1.1" --cause failed >"$T/rp.out" 2>"$T/rp.err"
ok "replay: stdout is the ID and kind only" "$(tr '\n' ' ' < "$T/rp.out")" "$R#1.2 new "
ok "replay: the predecessor's own line is shown" "$(grep -c "manifest: $R#1.1: stop" "$T/rp.err")" "1"
ok "replay: other runs' lines are not shown, one count says so" "$(grep -c 'other-run' "$T/rp.err")|$(grep -c 'other notice line(s) not shown (--verbose lists them)' "$T/rp.err")" "0|1"
node "$M" launch --run $R --row 1 --role implementation --agent orch-builder --mode subagent --repo r --area a --model sonnet --effort high --continue "$R#1.1" --cause failed --verbose >"$T/rp.out" 2>"$T/rp.err"
ok "replay: --verbose lists every line" "$(grep -c 'other-run#7.1\|other-run#8.1' "$T/rp.err")" "2"

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
