#!/usr/bin/env bash
# manifest.js tests (orchestrator v2 fixes, rows 11-12; D4 design §1, §2, §3, §7, §9 rows 11-12): attempt IDs, per-row n (ledger-aware),
# atomic appends under the lock, dedupe, torn lines, launch reuse/refuse/--new/--continue (G4, G5), show folding (A-16), config keys,
# lease (acquire/renew/release/check, launch gate, implicit renewal), reconcile (§7 fixtures 1-10, the G1 pinned-copy case, A-19),
# activity (Stage A live-driver check: file/event/agent signals, clean handoff, --sid exclusion, --json, writes nothing), resumable (row 5: candidates, done, lease, --limit, read-only),
# status (per-row view, run list, --json, usage, read-only), runs (live leases, resumable lines byte-identical, finished, other, read-only).
# Scratch ORCH_DIR / ORCH_RUNS_DIR / ORCH_PROJECTS_DIR; never the live ACTIVE. Usage: bash ~/.claude/orchestrator/test/manifest-tests.sh   (exit 0 = all pass)
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib/compat.sh"   # on macOS: GNU date/stat/touch/md5sum/wc shims (test/lib)
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"   # the checkout this suite tests (a worktree runs its own code)
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
has(){ case "$2" in *"$3"*) ok "$1" yes yes;; *) ok "$1" "no: $2" "contains: $3";; esac; }
lacks(){ case "$2" in *"$3"*) ok "$1" "contains: $3" "lacks it";; *) ok "$1" yes yes;; esac; }
M="$CL/lib/manifest.js"
REAL_CFG="$CL/lib/defaults.json"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export ORCH_DIR="$T/orch" ORCH_RUNS_DIR="$T/orch/runs" ORCH_PROJECTS_DIR="$T/projects" ORCH_METRICS_DB="$T/metrics.db" ORCH_SETTINGS="$T/settings.json"
unset WFP_SID BOSS_PROGRESS_DIR   # a direct run behaves like scripts/test-all.sh: no snapshot reaches the live registry (the stop cases set it per command)
FAKEWFP="$T/fake-wfp.js"; printf "%s\n" "const b=process.env.FAKE_BAR; if(!b){console.log(\"wfp: no session policy and no lasting default; set a profile first\");process.exit(0)} const [m,e]=b.split(\"/\"); console.log(JSON.stringify({role:process.argv[3],bar:{model:m,effort:e||null}}));" > "$FAKEWFP"
export ORCH_WFP_JS="$FAKEWFP"   # hermetic: launches read the bar from this fake (FAKE_BAR=model/effort), never the live wfp policy
LIVE="$HOME/.claude/orchestrator"; sum(){ if [ -e "$1" ]; then md5sum < "$1"; else echo absent; fi; }   # LIVE is only for the live-file checks
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
mkdir -p "$ORCH_RUNS_DIR" "$ORCH_PROJECTS_DIR"; cp "$REAL_CFG" "$ORCH_DIR/config.json"
L(){ node "$M" launch --role implementation --agent orch-builder --mode subagent --repo r --area a "$@"; }
E(){ node "$M" event "$@" >/dev/null; }
S(){ node "$M" show "$@"; }
MF(){ echo "$ORCH_RUNS_DIR/$1/manifest.jsonl"; }
nlaunch(){ jq -c 'select(.type=="launch")' "$(MF "$1")" | wc -l | tr -d ' '; }
rc(){ "$@" >/dev/null 2>&1; echo $?; }
ago(){ date -u -d "-$1 min" +%Y-%m-%dT%H:%M:%S.000Z; }

echo "== config.json (§10.0) and module"
want='{"returnCapBytes":2048,"returnCapMode":"flag","returnCapAgentTypes":"^orch-","leaseTtlSec":7200,"leaseRenewWhenLeftSec":3600,"gapInflightSec":7200,"waitingGraceSec":1800,"sweepTailBytes":4194304,"passStatuses":["done","passed","pass"],"gateStatuses":["gaps_found","revise","human_needed"],"readOnlyRoles":["planning","plan-check","discovery","diagnosis","review"],"lockWaitMsHook":1000,"lockWaitMsCli":3000}'
ok "defaults.json holds the §10.0 keys and values incl. gapInflightSec (plus progress and the guard/metrics/analytics keys)" "$(jq -cS 'del(.progress,.guard,.metrics,.analytics)' "$REAL_CFG")" "$(echo "$want" | jq -cS .)"
ok "require() prints nothing" "$(node -e "require('$M')" 2>&1)" ""
ok "require() of manifest.js prints nothing" "$(env -u ORCH_DIR -u ORCH_RUNS_DIR node -e "require('$M')" 2>&1)" ""
ok "exports" "$(node -e "const m=require('$M'); console.log(['parseAttemptId','parseDescription','appendEvent','readEvents','fold','runIngest','leaseState','renewIfMine'].map(k=>typeof m[k]).join(','), typeof m.ORCH_DIR, typeof m.RUNS, m.cfg.returnCapBytes, m.cfg.leaseTtlSec)")" "function,function,function,function,function,function,function,function string string 2048 7200"
ok "ORCH_RUNS_DIR override honoured" "$(node -e "console.log(require('$M').RUNS)")" "$ORCH_RUNS_DIR"
ok "bad config.json: defaults, require still silent" "$(d=$T/badcfg; mkdir -p $d; echo '{nope' > $d/config.json; ORCH_DIR=$d node -e "console.log(require('$M').cfg.leaseTtlSec)" 2>&1)" "7200"

echo "== attempt ID format and description parsing (§1)"
P(){ node -e "const m=require('$M'); console.log(JSON.stringify(m.$1))"; }
ok "parse run-a#3B.2 (row lower-cased)" "$(P "parseAttemptId('run-a#3B.2')")" '{"id":"run-a#3b.2","run":"run-a","row":"3b","n":2}'
ok "'..' in the run refused" "$(P "parseAttemptId('a..b#3.1')")" "null"
ok "n 0 refused" "$(P "parseAttemptId('r#3.0')")" "null"
ok "row of 5 digits refused" "$(P "parseAttemptId('r#12345.1')")" "null"
ok "n of 4 digits refused" "$(P "parseAttemptId('r#3.1000')")" "null"
ok "description [m/e] <id> <subtask>" "$(P "parseDescription('[opus/high] r1#11.1 manifest events')")" '{"attempt":"r1#11.1","model":"opus","effort":"high"}'
ok "description [auto] <id>: no label values" "$(P "parseDescription('[auto] r1#2.3 probe')")" '{"attempt":"r1#2.3","model":null,"effort":null}'
ok "an ID quoted later in the text is ignored" "$(P "parseDescription('[haiku/low] retry of r1#2.3')")" '{"attempt":null,"model":"haiku","effort":"low"}'
ok "a bare ID with no label parses" "$(P "parseDescription('r1#2.3 x')")" '{"attempt":"r1#2.3","model":null,"effort":null}'

echo "== launch: ID, new, per-row n"
out=$(L --run t1 --row 3 --model opus --effort high 2>"$T/err")
ok "first launch prints the ID on line 1" "$(echo "$out" | sed -n 1p)" "t1#3.1"
ok "line 2 is new" "$(echo "$out" | sed -n 2p)" "new"
ok "stdout is exactly 2 lines" "$(echo "$out" | wc -l | tr -d ' ')" "2"
ok "no sid: note on stderr" "$(cat "$T/err")" "note: no session id, lease not checked; hooks will not record this run"
ok "ID matches the §1 regex" "$(echo "$out" | head -1 | grep -cE '^([A-Za-z0-9][A-Za-z0-9._-]{0,99})#([0-9]{1,4}[a-z]?)\.([1-9][0-9]{0,2})$')" "1"
ok "launch --new on the same row gives .2" "$(L --run t1 --row 3 --model opus --effort high --new 2>/dev/null | head -1)" "t1#3.2"
ok "another row starts at .1" "$(L --run t1 --row 4 --model opus --effort high 2>/dev/null | head -1)" "t1#4.1"
ok "row 3B is lower-cased" "$(L --run t1 --row 3B --model opus --effort high 2>/dev/null | head -1)" "t1#3b.1"
out=$(L --run t1 --row 7 --model sonnet --effort medium --sid S-1 --subtask "do x" --git "$T" --base abc1234 2>"$T/err")
ok "with --sid on a free run: the acquire note on stderr (D-6 row 7), stdout still 2 lines" "$(sed 's/until .* (run/until _ (run/' "$T/err") $(echo "$out" | wc -l | tr -d ' ')" "note: lease acquired by S-1 until _ (run t1) 2"
ok "launch event fields" "$(jq -c 'select(.attempt=="t1#7.1") | [.type,.id,.seq,.run,.row,.key,.via,.session,.n,.role,.agent,.model,.effort,.mode,.repo,.area,.git,.base,.subtask,.continues,.task_id]' "$(MF t1)")" \
  "[\"launch\",\"t1#e6\",6,\"t1\",\"7\",\"t1#7.1:sonnet/medium\",\"cli\",\"S-1\",1,\"implementation\",\"orch-builder\",\"sonnet\",\"medium\",\"subagent\",\"r\",\"a\",\"$T\",\"abc1234\",\"do x\",null,null]"
ok "WFP_SID env stands in for --sid (its own run: S-1 now holds t1)" "$(WFP_SID=S-env L --run t1w --row 8 --model opus --effort high >/dev/null 2>&1; jq -r 'select(.attempt=="t1w#8.1") | .session' "$(MF t1w)")" "S-env"

echo "== n skips manual ledger lines"
mkdir -p "$ORCH_RUNS_DIR/t2"
printf '%s\n' '{"ts":"2026-10-20T00:00:00.000Z","id":"t2#5.1","run":"t2","row":5,"model":"opus","effort":"high"}' \
  '{"ts":"2026-10-20T00:00:01.000Z","run":"t2","row":6,"model":"opus","effort":"high"}' '{"ts":"2026-10-20T00:00:02.000Z","run":"t2","row":6,"model":"opus","effort":"max"}' > "$ORCH_RUNS_DIR/t2/ledger.jsonl"
ok "a manual ledger #5.1 makes the launch .2" "$(L --run t2 --row 5 --model opus --effort high 2>/dev/null | head -1)" "t2#5.2"
ok "two id-less ledger lines (ordinals 1, 2) make the launch .3" "$(L --run t2 --row 6 --model opus --effort high 2>/dev/null | head -1)" "t2#6.3"

echo "== concurrent appends: 2 processes x 20 event calls"
( for i in $(seq 1 20); do E --run t3 --type start --json "{\"tool_use_id\":\"a$i\"}"; done ) &
( for i in $(seq 1 20); do E --run t3 --type start --json "{\"tool_use_id\":\"b$i\"}"; done ) &
wait
ok "40 readable lines" "$(jq -c . "$(MF t3)" | wc -l | tr -d ' ')" "40"
ok "40 unique ids" "$(jq -r .id "$(MF t3)" | sort -u | wc -l | tr -d ' ')" "40"
ok "seq 1..40, no gap" "$(jq -s -c 'map(.seq)|sort == [range(1;41)]' "$(MF t3)")" "true"
ok "no unreadable line" "$(S --run t3 --json | jq .bad_lines)" "0"

echo "== concurrent launch x2 for one row"
L --run t4 --row 1 --model opus --effort high --new >"$T/l1" 2>/dev/null & L --run t4 --row 1 --model opus --effort high --new >"$T/l2" 2>/dev/null & wait
ok "with --new: .1 and .2" "$(cat "$T/l1" "$T/l2" | grep '#' | sort | tr '\n' ' ')" "t4#1.1 t4#1.2 "
L --run t5 --row 1 --model opus --effort high >"$T/l1" 2>/dev/null & L --run t5 --row 1 --model opus --effort high >"$T/l2" 2>/dev/null & wait
ok "without --new (G5): one ID twice" "$(cat "$T/l1" "$T/l2" | grep '#' | sort | tr '\n' ' ')" "t5#1.1 t5#1.1 "
ok "without --new: one new, one reused" "$(cat "$T/l1" "$T/l2" | grep -v '#' | sort | tr '\n' ' ')" "new reused "
ok "without --new: one launch event" "$(nlaunch t5)" "1"

echo "== dedupe by type + key"
ok "first start" "$(node "$M" event --run t6 --type start --json '{"tool_use_id":"x1"}')" "t6#e1 new"
ok "same type+key is a dup with the existing id" "$(node "$M" event --run t6 --type start --json '{"tool_use_id":"x1","agent":"other"}')" "t6#e1 dup"
ok "dup wrote nothing" "$(wc -l < "$(MF t6)" | tr -d ' ')" "1"
ok "same key, other type is new" "$(node "$M" event --run t6 --type return_over_cap --json '{"key":"x1","task_id":"k","action":"flagged"}')" "t6#e2 new"
ok "stop key derived: <task>:<phase>:<tools>:<ms>" "$(E --run t6 --type stop --json '{"task_id":"k9","phase":"finished","tools":3,"ms":40}'; jq -r 'select(.type=="stop")|.key+" "+(.final|tostring)' "$(MF t6)")" "k9:finished:3:40 true"
ok "substop key derived" "$(E --run t6 --type stop --json '{"task_id":"k9","phase":"agent-stopped","via":"substop","stop_hook_active":false,"result_bytes":512}'; jq -r 'select(.via=="substop")|.key+" "+(.final|tostring)' "$(MF t6)")" "k9:substop:0:512 false"

echo "== torn line"
printf '{"id":"t6#e5","type":"st' >> "$(MF t6)"
ok "append after a torn line" "$(node "$M" event --run t6 --type start --json '{"tool_use_id":"x2"}')" "t6#e6 new"
ok "earlier ids unchanged" "$(jq -R -r 'fromjson? | .id' "$(MF t6)" | tr '\n' ' ')" "t6#e1 t6#e2 t6#e3 t6#e4 t6#e6 "
ok "the torn line is counted as unreadable" "$(S --run t6 --json | jq .bad_lines)" "1"
ok "the new line stands on its own line" "$(tail -1 "$(MF t6)" | jq -r .tool_use_id)" "x2"
ok "show (text) reports the unreadable line" "$(S --run t6 | head -1)" "manifest: run t6: 0 attempt(s), 0 open, 1 unreadable line(s)"

echo "== show: order (open first, oldest first, with age) and flags (A-16)"
R=t7
for r in 1 2 3 4 4b 5 6 7 10; do L --run $R --row $r --model opus --effort high >/dev/null 2>&1; done
E --run $R --type start --json "{\"attempt\":\"$R#2.1\",\"tool_use_id\":\"u2\",\"ts\":\"$(ago 60)\"}"
E --run $R --type start --json "{\"attempt\":\"$R#7.1\",\"tool_use_id\":\"u7\",\"ts\":\"$(ago 45)\"}"
E --run $R --type stop --json "{\"attempt\":\"$R#7.1\",\"task_id\":\"k7\",\"phase\":\"waiting\",\"tools\":2,\"ms\":10}"
E --run $R --type start --json "{\"attempt\":\"$R#6.1\",\"tool_use_id\":\"u6\",\"ts\":\"$(ago 30)\"}"
E --run $R --type stop --json "{\"attempt\":\"$R#6.1\",\"task_id\":\"k6\",\"phase\":\"agent-stopped\",\"via\":\"substop\",\"result_bytes\":9}"
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"u1\",\"ts\":\"$(ago 10)\",\"id_in_brief\":false,\"foreign_lease\":true}"
E --run $R --type start --json "{\"attempt\":\"$R#9.1\",\"tool_use_id\":\"u9\",\"ts\":\"$(ago 5)\",\"unknown_attempt\":true}"
for r in 4 4b 10; do E --run $R --type stop --json "{\"attempt\":\"$R#$r.1\",\"task_id\":\"k$r\",\"phase\":\"finished\",\"tools\":5,\"ms\":100}"; done
E --run $R --type stop --json "{\"attempt\":\"$R#4.1\",\"task_id\":\"k4\",\"phase\":\"finished\",\"tools\":6,\"ms\":200}"
E --run $R --type stop --json "{\"attempt\":\"$R#5.1\",\"task_id\":\"k5\",\"phase\":\"failed\"}"
E --run $R --type return_over_cap --json "{\"attempt\":\"$R#5.1\",\"task_id\":\"k5\",\"action\":\"flagged\",\"bytes\":3000,\"cap\":2048}"
echo "{\"ts\":\"x\",\"id\":\"$R#5.1\",\"run\":\"$R\",\"row\":5}" > "$ORCH_RUNS_DIR/$R/ledger.jsonl"
E --run $R --type outcome --json "{\"attempt\":\"$R#5.1\",\"first_pass\":false,\"set\":[\"first_pass\"]}"
E --run $R --type start --json '{"tool_use_id":"plain1","agent":"Explore","description":"look around"}'
J=$(S --run $R --json)
ok "order: open oldest first, then by row (number, letter) and n" "$(echo "$J" | jq -r '[.attempts[].id]|join(" ")')" "$R#2.1 $R#7.1 $R#6.1 $R#1.1 $R#9.1 $R#3.1 $R#4.1 $R#4b.1 $R#5.1 $R#10.1"
ok "states" "$(echo "$J" | jq -r '[.attempts[].state]|join(" ")')" "started waiting stopped-unreported started started allocated finished finished failed finished"
ok "open ages in minutes; closed have none" "$(echo "$J" | jq -c '[.attempts[].age_min]')" "[60,45,30,10,5,0,null,null,null,null]"
ok "flags: no-brief-line, foreign-lease" "$(echo "$J" | jq -c --arg id "$R#1.1" '.attempts[]|select(.id==$id)|.flags')" '["no-brief-line","foreign-lease"]'
ok "flags: unknown-attempt (no launch event)" "$(echo "$J" | jq -c --arg id "$R#9.1" '.attempts[]|select(.id==$id)|.flags')" '["unknown-attempt"]'
ok "flags: renotice + no-ledger-line" "$(echo "$J" | jq -c --arg id "$R#4.1" '.attempts[]|select(.id==$id)|.flags')" '["renotice","no-ledger-line"]'
ok "flags: over-cap, ledger line found" "$(echo "$J" | jq -c --arg id "$R#5.1" '.attempts[]|select(.id==$id)|[.flags,.ledger,.outcome.first_pass,(.caps|length)]')" '[["over-cap:flagged"],true,false,1]'
ok "json shape" "$(echo "$J" | jq -c '[keys, (.attempts[0]|keys)]')" '[["attempts","bad_lines","lease","run","unplanned"],["age_min","caps","flags","id","launch","ledger","n","outcome","row","starts","state","stops"]]'
ok "unplanned section holds the start with no attempt" "$(echo "$J" | jq -c '[.unplanned[]|[.tool_use_id,.state,.agent]]')" '[["plain1","started","Explore"]]'
TX=$(S --run $R)
ok "text: header" "$(echo "$TX" | sed -n 1p)" "manifest: run $R: 10 attempt(s), 6 open"
ok "text: open section first" "$(echo "$TX" | sed -n 2p)" "open:"
has "text: oldest open attempt with its age" "$(echo "$TX" | sed -n 3p)" "$R#2.1  started  age 60m  opus/high"
has "text: unplanned section" "$TX" "unplanned:"
ok "text: lease line last" "$(echo "$TX" | tail -1 | cut -c1-6)" "lease:"
ok "show --row 4 keeps only row 4, no unplanned" "$(S --run $R --row 4 --json | jq -c '[[.attempts[].id], (.unplanned|length)]')" "[[\"$R#4.1\"],0]"
ok "show of a run with no manifest" "$(S --run none-yet)" "manifest: run none-yet has no manifest yet ($ORCH_RUNS_DIR/none-yet/manifest.jsonl)"

echo "== G5: reuse a phantom, refuse a running attempt, --new"
R=t8; PJ="$ORCH_PROJECTS_DIR/p"; mkdir -p "$PJ"; : > "$PJ/S1.jsonl"
L --run $R --row 1 --model opus --effort high >/dev/null 2>&1
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"tu1\",\"transcript_path\":\"$PJ/S1.jsonl\",\"session\":\"S1\"}"
out=$(L --run $R --row 1 --model opus --effort high 2>/dev/null)
ok "a start with no meta.json (phantom): same ID, reused" "$(echo "$out" | tr '\n' ' ')" "$R#1.1 reused "
ok "reuse on the same model/effort appends no launch" "$(nlaunch $R)" "1"
ok "reuse with a new effort appends a launch" "$(L --run $R --row 1 --model opus --effort max 2>/dev/null | tr '\n' ' '; nlaunch $R)" "$R#1.1 reused 2"
ok "show takes the latest launch event" "$(S --run $R --json | jq -r --arg id "$R#1.1" '.attempts[]|select(.id==$id)|.launch.effort')" "max"
mkdir -p "$PJ/S1/subagents"; echo '{"agentType":"orch-builder","toolUseId":"tu1","description":"x"}' > "$PJ/S1/subagents/agent-a1.meta.json"
ok "meta.json-backed start, no final stop: refused, exit 2" "$(rc L --run $R --row 1 --model opus --effort high)" "2"
ok "refusal message" "$(L --run $R --row 1 --model opus --effort high 2>&1 >/dev/null)" "manifest: $R#1.1 is still running; wait for its notice, or pass --new"
ok "--new gives a fresh n" "$(L --run $R --row 1 --model opus --effort high --new 2>/dev/null | tr '\n' ' ')" "$R#1.2 new "
L --run $R --row 2 --model opus --effort high >/dev/null 2>&1
E --run $R --type stop --json "{\"attempt\":\"$R#2.1\",\"task_id\":\"w2\",\"phase\":\"waiting\"}"
ok "a waiting stop (no meta.json) is refused too" "$(rc L --run $R --row 2 --model opus --effort high)" "2"
E --run $R --type stop --json "{\"attempt\":\"$R#2.1\",\"task_id\":\"w2\",\"phase\":\"finished\",\"tools\":1,\"ms\":1}"
ok "after a final stop the next launch is new" "$(L --run $R --row 2 --model opus --effort high 2>/dev/null | tr '\n' ' ')" "$R#2.2 new "

echo "== G4: launch --continue"
R=t9; : > "$PJ/S2.jsonl"
L --run $R --row 1 --model opus --effort high >/dev/null 2>&1
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"tc1\",\"transcript_path\":\"$PJ/S2.jsonl\",\"session\":\"S2\"}"
ok "--continue P while P has no final stop: exit 2" "$(rc L --run $R --row 1 --model opus --effort high --continue "$R#1.1")" "2"
err=$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>&1 >/dev/null)
has "refusal names P" "$err" "manifest: $R#1.1 has no final stop yet; wait for its notice"
lacks "the ingest child ran (no failure)" "$err" "ingest failed"
lacks "P's start transcript was found" "$err" "no transcript for"
ok "a refused launch writes nothing" "$(nlaunch $R)" "1"
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"tk1\",\"phase\":\"finished\",\"tools\":4,\"ms\":9}"
ok "--continue after P's final stop: new" "$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>/dev/null | tr '\n' ' ')" "$R#1.2 new "
ok "the launch records continues and P's task_id" "$(jq -c --arg id "$R#1.2" 'select(.attempt==$id)|[.continues,.task_id]' "$(MF $R)")" "[\"$R#1.1\",\"tk1\"]"
ok "show flags the continuation" "$(S --run $R --json | jq -c --arg id "$R#1.2" '.attempts[]|select(.id==$id)|[.state,.flags]')" "[\"allocated\",[\"continuation-of $R#1.1\"]]"
ok "a continuation with no final stop: plain launch refused, exit 2" "$(rc L --run $R --row 1 --model opus --effort high)" "2"
has "same refusal message" "$(L --run $R --row 1 --model opus --effort high 2>&1 >/dev/null)" "$R#1.2 is still running; wait for its notice, or pass --new"
lacks "continuation of a continuation finds the transcript up the chain" "$(L --run $R --row 1 --model opus --effort high --continue "$R#1.2" 2>&1 >/dev/null)" "no transcript for"
E --run $R --type stop --json "{\"attempt\":\"$R#1.2\",\"task_id\":\"tk1\",\"phase\":\"finished\",\"tools\":2,\"ms\":3}"
ok "continuation finished: next launch new" "$(L --run $R --row 1 --model opus --effort high 2>/dev/null | head -1)" "$R#1.3"
ok "--continue of an attempt with no launch: exit 2" "$(rc L --run $R --row 1 --model opus --effort high --continue "$R#5.1")" "2"
ok "--continue of another run's attempt: exit 2" "$(rc L --run $R --row 1 --model opus --effort high --continue "t8#1.1")" "2"
mkdir -p "$ORCH_PROJECTS_DIR/q"; : > "$ORCH_PROJECTS_DIR/q/S3.jsonl"
L --run $R --row 2 --model opus --effort high --sid S3 >/dev/null 2>&1
lacks "no start: falls back to \$ORCH_PROJECTS_DIR/*/<launch session>.jsonl" "$(L --run $R --row 2 --model opus --effort high --continue "$R#2.1" --sid S3 2>&1 >/dev/null)" "no transcript for"
L --run $R --row 3 --model opus --effort high --sid S-none >/dev/null 2>&1
has "no transcript anywhere: checks the manifest as it stands" "$(L --run $R --row 3 --model opus --effort high --continue "$R#3.1" 2>&1 >/dev/null)" "no transcript for $R#3.1; checking the manifest as it stands"

echo "== idle stop: a completed 'waiting' stop counts as final (launch --continue, plain relaunch, reconcile)"
R=t9w; : > "$PJ/S9w.jsonl"
L --run $R --row 1 --model opus --effort high >/dev/null 2>&1
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"tw1\",\"transcript_path\":\"$PJ/S9w.jsonl\",\"session\":\"S9w\"}"
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"tkw1\",\"phase\":\"waiting\",\"status\":\"failed\",\"tools\":4,\"ms\":9}"
ok "waiting with status not completed: --continue still refused" "$(rc L --run $R --row 1 --model opus --effort high --continue "$R#1.1")" "2"
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"tkw1\",\"phase\":\"waiting\",\"status\":\"completed\",\"tools\":5,\"ms\":10}"
ok "waiting+completed only: --continue allowed" "$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>/dev/null | tr '\n' ' ')" "$R#1.2 new "
ok "the continuation records the waiting stop's task_id" "$(jq -c --arg id "$R#1.2" 'select(.attempt==$id)|[.continues,.task_id]' "$(MF $R)")" "[\"$R#1.1\",\"tkw1\"]"
R=t9x; : > "$PJ/S9x.jsonl"
L --run $R --row 1 --model opus --effort high >/dev/null 2>&1
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"tx1\",\"transcript_path\":\"$PJ/S9x.jsonl\",\"session\":\"S9x\"}"
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"tkx1\",\"phase\":\"waiting\",\"status\":\"completed\",\"tools\":5,\"ms\":10}"
ok "waiting+completed only: a plain relaunch allocates the next attempt" "$(L --run $R --row 1 --model opus --effort high 2>/dev/null | tr '\n' ' ')" "$R#1.2 new "
R=t9y; : > "$PJ/S9y.jsonl"
L --run $R --row 1 --model opus --effort high >/dev/null 2>&1
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"ty1\",\"transcript_path\":\"$PJ/S9y.jsonl\",\"session\":\"S9y\"}"
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"tky1\",\"phase\":\"waiting\",\"status\":\"completed\",\"tools\":5,\"ms\":10}"
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"ty2\",\"transcript_path\":\"$PJ/S9y.jsonl\",\"session\":\"S9y\"}"
ok "a start newer than the waiting stop (resumed): --continue refused again" "$(rc L --run $R --row 1 --model opus --effort high --continue "$R#1.1")" "2"

echo "== launch --continue: ID first, idempotent repeat, auto first-pass n, --tune"
R=t9c; : > "$PJ/S9.jsonl"
L --run $R --row 1 --model opus --effort high >/dev/null 2>&1
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"tcc1\",\"transcript_path\":\"$PJ/S9.jsonl\",\"session\":\"S9\"}"
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"tkc1\",\"phase\":\"finished\",\"tools\":4,\"ms\":9}"
echo "{\"ts\":\"2026-11-11T00:00:00.000Z\",\"id\":\"$R#1.1\",\"run\":\"$R\",\"row\":1,\"model\":\"opus\",\"effort\":\"high\",\"firstPass\":true}" > "$ORCH_RUNS_DIR/$R/ledger.jsonl"
fp(){ jq -c --arg id "$1" 'select(.id==$id)|.firstPass' "$ORCH_RUNS_DIR/$R/ledger.jsonl"; }
L --run $R --row 3 --model opus --effort high --sid S-none >/dev/null 2>&1
E --run $R --type stop --json "{\"attempt\":\"$R#3.1\",\"task_id\":\"tkc3\",\"phase\":\"finished\",\"tools\":1,\"ms\":1}"
merged=$(L --run $R --row 3 --model opus --effort high --continue "$R#3.1" 2>&1 | head -1)
ok "merged stdout+stderr (2>&1 | head -1): line 1 is the ID although the sweep printed a note" "$merged" "$R#3.2"
n0=$(nlaunch $R)
out=$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>$T/cerr)
ok "--continue: stdout is ID, new" "$(echo "$out" | tr '\n' ' ')" "$R#1.2 new "
ok "stderr carries the first-pass note" "$(grep -c 'first-pass set to n' $T/cerr)" "1"
ok "the predecessor's ledger first_pass was y, now n (auto, retry-kind continue)" "$(fp $R#1.1)" "false"
ok "repeat --continue (continuation not started): same ID, reused" "$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>/dev/null | tr '\n' ' ')" "$R#1.2 reused "
ok "the repeat wrote no launch event" "$(nlaunch $R)" "$((n0 + 1))"
ok "repeat with merged streams: line 1 still the ID" "$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>&1 | head -1)" "$R#1.2"
ok "an explicit later ledger update (y) survives the repeat" "$(node "$CL/lib/ledger.js" update --attempt $R#1.1 --first-pass y >/dev/null 2>&1; L --run $R --row 1 --model opus --effort high --continue "$R#1.1" >/dev/null 2>&1; fp $R#1.1)" "true"
ok "repeat with a new effort appends a launch for the same ID" "$(L --run $R --row 1 --model opus --effort max --continue "$R#1.1" 2>/dev/null | tr '\n' ' '; nlaunch $R)" "$R#1.2 reused $((n0 + 2))"
ok "the flip's launch still records continues" "$(jq -r --arg id "$R#1.2" 'select(.type=="launch" and .attempt==$id)|.continues' "$(MF $R)" | sort -u)" "$R#1.1"
E --run $R --type start --json "{\"attempt\":\"$R#1.2\",\"tool_use_id\":\"tcc2\",\"transcript_path\":\"$PJ/S9.jsonl\",\"session\":\"S9\"}"
mkdir -p "$PJ/S9/subagents"; echo '{"agentType":"orch-builder","toolUseId":"tcc2","description":"x"}' > "$PJ/S9/subagents/agent-a2.meta.json"
ok "repeat --continue once the continuation has started: refused, exit 2" "$(rc L --run $R --row 1 --model opus --effort high --continue "$R#1.1")" "2"
ok "refusal: notes first, then the message (stderr order)" "$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>&1 >/dev/null | tail -1)" "manifest: $R#1.2 is still running; wait for its notice, or pass --new"
# --tune: label untouched, flag recorded
R=t9d; : > "$PJ/S10.jsonl"
L --run $R --row 1 --model opus --effort high >/dev/null 2>&1
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"tkd1\",\"phase\":\"finished\",\"tools\":4,\"ms\":9}"
echo "{\"ts\":\"2026-11-11T00:00:00.000Z\",\"id\":\"$R#1.1\",\"run\":\"$R\",\"row\":1,\"model\":\"opus\",\"effort\":\"high\",\"firstPass\":true}" > "$ORCH_RUNS_DIR/$R/ledger.jsonl"
ok "--tune continue: new ID" "$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" --tune 2>/dev/null | tr '\n' ' ')" "$R#1.2 new "
ok "--tune leaves the predecessor's first_pass label" "$(fp $R#1.1)" "true"
ok "--tune is recorded on the launch event" "$(jq -r --arg id "$R#1.2" 'select(.type=="launch" and .attempt==$id)|.tune' "$(MF $R)")" "true"
ok "--tune without --continue: usage error, exit 2" "$(rc L --run $R --row 2 --model opus --effort high --tune)" "2"
R=t9e; L --run $R --row 1 --model opus --effort high >/dev/null 2>&1
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"tke1\",\"phase\":\"finished\",\"tools\":4,\"ms\":9}"
ok "no ledger line for the predecessor: the launch still succeeds, with a note" "$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>$T/cerr2 | tr '\n' ' ')|$(grep -c 'not set to n' $T/cerr2)" "$R#1.2 new |1"

echo "== row 19 (P31): launch --cause on continuations and fresh retries"
LE(){ jq -c --arg id "$2" 'select(.type=="launch" and .attempt==$id)|.cause' "$(MF "$1")" | tail -1; }
LC(){ jq -c --arg id "$2" 'select(.id==$id)|[.firstPass,.cause]' "$ORCH_RUNS_DIR/$1/ledger.jsonl"; }
seed(){ L --run $1 --row $2 --model opus --effort high >/dev/null 2>&1; E --run $1 --type stop --json "{\"attempt\":\"$1#$2.1\",\"task_id\":\"tk$1$2\",\"phase\":\"finished\",\"tools\":1,\"ms\":1}"
  echo "{\"ts\":\"2026-11-11T00:00:00.000Z\",\"id\":\"$1#$2.1\",\"run\":\"$1\",\"row\":$2,\"model\":\"opus\",\"effort\":\"high\",\"firstPass\":${3:-null}}" >> "$ORCH_RUNS_DIR/$1/ledger.jsonl"; }
R=t19c; seed $R 1 true
out=$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" --cause review-findings 2>$T/c19a | tr '\n' ' ')
ok "--continue --cause review-findings: new ID, no warning" "$out|$(grep -c 'warning' $T/c19a)" "$R#1.2 new |0"
ok "--continue --cause: the launch event records the cause" "$(LE $R "$R#1.2")" '"review-findings"'
ok "--continue --cause: the predecessor gets first-pass n and the cause" "$(LC $R "$R#1.1")" '[false,"review-findings"]'
ok "the note names the cause" "$(grep -c "first-pass set to n (continued by $R#1.2; cause review-findings;" $T/c19a)" "1"
R=t19d; seed $R 1 true
out=$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>$T/c19b | tr '\n' ' ')
ok "--continue without --cause: still new (never refused), cause unstated on the event and the predecessor" "$out|$(LE $R "$R#1.2")|$(LC $R "$R#1.1")" "$R#1.2 new |\"unstated\"|[false,\"unstated\"]"
ok "--continue without --cause: exactly one warning line naming the flag and its values" "$(grep -c '' <(grep warning $T/c19b))|$(grep warning $T/c19b)" "1|manifest: warning: retry launch without --cause; recorded as unstated (--cause owner-tune|review-findings|failed|tooling)"
ok "a repeat (reused) prints no second warning" "$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>&1 >/dev/null | grep -c warning)" "0"
ok "--cause bogus: usage error, exit 2" "$(rc L --run $R --row 2 --model opus --effort high --cause bogus)" "2"
R=t19t; seed $R 1 true
out=$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" --tune 2>$T/c19t | tr '\n' ' ')
ok "--tune: new, no warning, launch cause owner-tune, predecessor keeps first-pass y and gets cause owner-tune" "$out|$(grep -c warning $T/c19t)|$(LE $R "$R#1.2")|$(LC $R "$R#1.1")" "$R#1.2 new |0|\"owner-tune\"|[true,\"owner-tune\"]"
seed $R 4 true; seed $R 6 true
ok "--continue --tune --cause failed refused (exit 2); --continue --tune --cause owner-tune accepted (exit 0)" "$(rc L --run $R --row 4 --model opus --effort high --continue "$R#4.1" --tune --cause failed)|$(rc L --run $R --row 6 --model opus --effort high --continue "$R#6.1" --tune --cause owner-tune)|$(LE $R "$R#6.2")" '2|0|"owner-tune"'
ok "--tune without --continue stays a usage error with --cause owner-tune (exit 2)" "$(rc L --run $R --row 3 --model opus --effort high --tune --cause owner-tune)" "2"
R=t19f; seed $R 1; seed $R 2 true; seed $R 3
out=$(L --run $R --row 1 --model sonnet --effort high --cause failed 2>$T/c19f | tr '\n' ' ')
ok "fresh retry (n>1, predecessor ran): new ID, launch cause failed" "$out|$(LE $R "$R#1.2")" "$R#1.2 new |\"failed\""
ok "fresh retry flips the unset predecessor to n with the cause" "$(LC $R "$R#1.1")|$(grep -c "note: $R#1.1 first-pass set to n (fresh retry $R#1.2; cause failed)" $T/c19f)" "[false,\"failed\"]|1"
L --run $R --row 2 --model sonnet --effort high --cause owner-tune >/dev/null 2>$T/c19g
ok "fresh retry of a passed line: first-pass y kept, cause recorded" "$(LC $R "$R#2.1")|$(grep -c 'first-pass kept' $T/c19g)" "[true,\"owner-tune\"]|1"
e=$(L --run $R --row 3 --model sonnet --effort high 2>&1 >/dev/null)
ok "fresh retry without --cause: unstated, predecessor flipped, one warning" "$(LE $R "$R#3.2")|$(LC $R "$R#3.1")|$(echo "$e" | grep -c '^manifest: warning: retry launch without --cause')" "\"unstated\"|[false,\"unstated\"]|1"
L --run $R --row 5 --model opus --effort high >/dev/null 2>&1
e=$(L --run $R --row 5 --model sonnet --effort high --new 2>&1 >/dev/null)
ok "--new past a phantom (no stop, no ledger line): no cause on the launch, no warning" "$(jq -c --arg id "$R#5.2" 'select(.type=="launch" and .attempt==$id)|has("cause")' "$(MF $R)")|$(echo "$e" | grep -c warning)" "false|0"
ok "a first launch carries no cause" "$(jq -c --arg id "$R#5.1" 'select(.type=="launch" and .attempt==$id)|has("cause")' "$(MF $R)")" "false"
R=t19n; L --run $R --row 1 --model opus --effort high >/dev/null 2>&1; E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"tkn1\",\"phase\":\"finished\",\"tools\":1,\"ms\":1}"
ok "fresh retry with no predecessor ledger line yet: still new, with a note" "$(L --run $R --row 1 --model sonnet --effort high --cause failed 2>$T/c19n | tr '\n' ' ')|$(grep -c "first-pass of $R#1.1 not set to n: no ledger line yet" $T/c19n)" "$R#1.2 new |1"

echo "== usage errors (exit 2) and hook-mode appends"
ok "launch missing flags" "$(rc node "$M" launch --run t1 --row 1)" "2"
ok "launch bad model" "$(rc L --run t1 --row 1 --model gpt --effort high)" "2"
ok "launch bad run" "$(rc L --run ../x --row 1 --model opus --effort high)" "2"
ok "launch --new takes no value" "$(rc L --run t1 --row 1 --model opus --effort high --new=yes)" "2"
ok "event --type launch refused" "$(rc node "$M" event --run t1 --type launch --json '{}')" "2"
ok "event bad json" "$(rc node "$M" event --run t1 --type start --json '{x')" "2"
ok "event start without tool_use_id" "$(rc node "$M" event --run t1 --type start --json '{}')" "2"
ok "event attempt of another run" "$(rc node "$M" event --run t1 --type start --json '{"tool_use_id":"z","attempt":"t2#1.1"}')" "2"
ok "show without --run" "$(rc node "$M" show)" "2"
ok "unknown command" "$(rc node "$M" lease-x)" "2"
ok "hook append to a missing run folder: skipped" "$(node -e "console.log(JSON.stringify(require('$M').appendEvent('gone',{type:'start',key:'k'},{hook:true})))")" '{"skipped":true,"reason":"no-run-folder"}'
ok "skip logged one errors.log line" "$(wc -l < "$ORCH_DIR/errors.log" | tr -d ' ')" "1"
mkdir "$(MF t6).lock"
ok "hook append with the lock busy: skipped, not written" "$(node -e "console.log(JSON.stringify(require('$M').appendEvent('t6',{type:'start',key:'k2'},{hook:true,waitMs:100})))"; jq -R -r 'fromjson? | select(.key=="k2") | .id' "$(MF t6)")" '{"skipped":true,"reason":"lock-busy"}'
rmdir "$(MF t6).lock"
echo "== carry-over 1: an empty --sid= takes the no-session path (design §3)"
out=$(L --run c1 --row 1 --model opus --effort high --sid= 2>"$T/err"); st=$?
ok "launch --sid=: exit 0" "$st" "0"
ok "launch --sid=: the ID and new" "$(echo "$out" | tr '\n' ' ')" "c1#1.1 new "
ok "launch --sid=: the no-session note" "$(cat "$T/err")" "note: no session id, lease not checked; hooks will not record this run"
ok "launch --sid=: session null" "$(jq -r 'select(.type=="launch")|.session' "$(MF c1)")" "null"
ok "event --sid=: exit 0" "$(rc node "$M" event --run c1 --type start --json '{"tool_use_id":"cs1"}' --sid=)" "0"
ok "event --sid=: session null" "$(jq -r 'select(.type=="start")|.session' "$(MF c1)")" "null"
ok "launch --sid= with WFP_SID set: the env value" "$(WFP_SID=S-env2 L --run c1 --row 2 --model opus --effort high --sid= >/dev/null 2>&1; jq -r 'select(.attempt=="c1#2.1")|.session' "$(MF c1)")" "S-env2"
ok "a bad --sid is still a usage error" "$(rc L --run c1 --row 3 --model opus --effort high --sid=a/b)" "2"

echo "== carry-over 2: an A->B->A model/effort flip on reuse launches"
R=c2
L --run $R --row 1 --model opus --effort high >/dev/null 2>&1
L --run $R --row 1 --model sonnet --effort medium >/dev/null 2>&1
ok "third launch (back to A) reuses the ID" "$(L --run $R --row 1 --model opus --effort high 2>/dev/null | tr '\n' ' ')" "$R#1.1 reused "
ok "A->B->A gives three launch events" "$(nlaunch $R)" "3"
ok "show's latest launch is A again" "$(S --run $R --json | jq -r --arg id "$R#1.1" '.attempts[]|select(.id==$id)|.launch.model+"/"+.launch.effort')" "opus/high"
ok "keys: base, then #<launch ordinal>" "$(jq -r 'select(.type=="launch")|.key' "$(MF $R)" | tr '\n' ' ')" "$R#1.1:opus/high $R#1.1:sonnet/medium#2 $R#1.1:opus/high#3 "
L --run $R --row 1 --model opus --effort high >/dev/null 2>&1
ok "reuse on the latest model/effort writes nothing" "$(nlaunch $R)" "3"

echo "== opus holdout ended 2026-11-11 (no roll, no line; --holdout-seed accepted and ignored; config holdoutRate ignored)"
HD1="$T/orchH1"; mkdir -p "$HD1"; echo '{"holdoutRate":1}' > "$HD1/config.json"
HL(){ local d="$1"; shift; ORCH_DIR="$d" node "$M" launch --role implementation --agent orch-builder --mode subagent --repo r --area a "$@"; }
R=ho
ok "holdoutRate 1 in config: creation sonnet/high prints only ID and kind" "$(HL "$HD1" --run $R --row 1 --model sonnet --effort high --why creation 2>/dev/null | tr '\n' '|')" "$R#1.1|new|"
ok "the launch event carries no holdout field" "$(jq -r 'select(.type=="launch" and .row=="1")|has("holdout")' "$(MF $R)")" "false"
ok "default config: seed 7 (once a hit) prints no holdout line" "$(HL "$ORCH_DIR" --run $R --row 2 --model sonnet --effort high --why creation --holdout-seed 7 2>/dev/null | tr '\n' '|')" "$R#2.1|new|"
ok "a non-integer --holdout-seed is no longer an error (ignored)" "$(rc HL "$ORCH_DIR" --run $R --row 3 --model sonnet --effort high --why creation --holdout-seed x)" "0"
ok "an old launch event with holdout:true still folds in show" "$(E --run $R --type start --json '{"tool_use_id":"hx"}'; S --run $R --json | jq -r '.attempts|length')" "3"

echo "== above-bar warning (launch vs the active wfp preset bar; fake wfp via ORCH_WFP_JS, FAKE_BAR=model/effort)"
R=ab
AB(){ FAKE_BAR="${FAKE_BAR-sonnet/high}" L --run $R "$@" 2>/dev/null; }
ABW='above bar: implementation opus/medium (bar sonnet/high), no --why'
ok "above bar, no why: exit 0" "$(rc env FAKE_BAR=sonnet/high node "$M" launch --role implementation --agent orch-builder --mode subagent --repo r --area a --run $R --row 90 --model opus --effort medium)" "0"
ok "above bar, no why: warning printed after the id and kind lines" "$(AB --row 91 --model opus --effort medium | tr '\n' '|')" "$R#91.1|new|$ABW|"
ok "above bar, no why: event records above_bar true" "$(jq -r 'select(.type=="launch" and .row=="91")|.above_bar' "$(MF $R)")" "true"
ok "above bar, same model higher effort: warns" "$(AB --row 92 --model sonnet --effort xhigh | sed -n 3p)" "above bar: implementation sonnet/xhigh (bar sonnet/high), no --why"
ok "above bar with --why: no warning line" "$(AB --row 93 --model opus --effort medium --why creation | tr '\n' '|')" "$R#93.1|new|"
ok "above bar with --why: no above_bar field" "$(jq -r 'select(.type=="launch" and .row=="93")|has("above_bar")' "$(MF $R)")" "false"
ok "at the bar: no warning, no field" "$(AB --row 94 --model sonnet --effort high | wc -l | tr -d ' ') $(jq -r 'select(.type=="launch" and .row=="94")|has("above_bar")' "$(MF $R)")" "2 false"
ok "below the bar (sonnet/medium): no warning, no field" "$(AB --row 95 --model sonnet --effort medium | wc -l | tr -d ' ') $(jq -r 'select(.type=="launch" and .row=="95")|has("above_bar")' "$(MF $R)")" "2 false"
ok "below the bar (haiku/max vs sonnet/high): no warning" "$(AB --row 96 --model haiku --effort max | wc -l | tr -d ' ')" "2"
ok "no readable bar (wfp prints text): no warning, exit 0" "$(env -u FAKE_BAR node "$M" launch --role implementation --agent orch-builder --mode subagent --repo r --area a --run $R --row 97 --model opus --effort high 2>/dev/null | wc -l | tr -d ' ')" "2"
ok "wfp script missing: no warning, exit 0" "$(ORCH_WFP_JS="$T/nope.js" node "$M" launch --role implementation --agent orch-builder --mode subagent --repo r --area a --run $R --row 98 --model opus --effort high 2>/dev/null | wc -l | tr -d ' ')" "2"
ok "the role is passed to wfp and the bar compared per launch" "$(AB --row 99 --model opus --effort high | sed -n 3p)" "above bar: implementation opus/high (bar sonnet/high), no --why"
echo "-- idempotent repeat"
n0=$(nlaunch $R)
ok "repeat of an above-bar launch reuses the ID and prints the same warning" "$(AB --row 91 --model opus --effort medium | tr '\n' '|')" "$R#91.1|reused|$ABW|"
ok "repeat wrote no launch event" "$(nlaunch $R)" "$n0"
ok "the event still carries one above_bar field" "$(jq -c 'select(.type=="launch" and .row=="91")|.above_bar' "$(MF $R)" | tr '\n' ' ')" "true "

echo "== lease (per-run lease D-1 to D-5; R12 fixture 5): separate scratch ORCH_DIR, leases/<run>.json"
LD="$T/orchL"; mkdir -p "$LD/runs/lr" "$LD/runs/lr2"; cp "$REAL_CFG" "$LD/config.json"
printf '%s\n%s\n' "$LD/runs/lr" "second line kept" > "$LD/ACTIVE"; cp "$LD/ACTIVE" "$T/active.before"
LZ(){ ORCH_DIR="$LD" ORCH_RUNS_DIR="$LD/runs" node "$M" "$@"; }
LF="$LD/leases/lr.json"; LF2="$LD/leases/lr2.json"; LM="$LD/runs/lr/manifest.jsonl"
LIVEKEYS='["v","run","path","session","acquired","renewed","expires","ttl_sec"]'
# setexp <date offset> [file]: move a lease file's expires (default: lr's per-run file)
setexp(){ local f="${2:-$LF}"; jq -c --arg e "$(date -u -d "$1" +%Y-%m-%dT%H:%M:%S.000Z)" '.expires=$e' "$f" > "$f.t" && mv "$f.t" "$f"; }
lev(){ jq -r 'select(.type=="lease")|[.action,.session,(.holder//"-"),(.prev_holder//"-")]|join(" ")' "$1" | tail -1; }
gone(){ for f in "$@"; do test -e "$f" && echo present || echo gone; done | tr '\n' ' '; }
ok "check, no lease: state none, leases 0" "$(LZ lease check --sid A1)" "state: none
leases: 0"
ok "check --run, no lease: none" "$(LZ lease check --run lr --sid A1)" "state: none"
ok "acquire by A1: exit 0" "$(rc LZ lease acquire --run lr --sid A1)" "0"
ok "acquire writes leases/lr.json and never ACTIVE.lease" "$(gone "$LF" "$LD/ACTIVE.lease")" "present gone "
ok "ACTIVE byte-identical after acquire (cmp)" "$(cmp "$T/active.before" "$LD/ACTIVE" && echo same)" "same"
ok "lease file fields" "$(jq -c '[.v,.run,.path,.session,.ttl_sec,(.acquired|length>0)]' "$LF")" "[1,\"lr\",\"$LD/runs/lr\",\"A1\",7200,true]"
ok "lease file keys in the live ACTIVE.lease order" "$(jq -c keys_unsorted "$LF")" "$LIVEKEYS"
ok "expires = renewed + ttl_sec" "$(jq '(.expires|sub("\\.[0-9]+Z$";"Z")|fromdate) - (.renewed|sub("\\.[0-9]+Z$";"Z")|fromdate)' "$LF")" "7200"
ok "no tmp file or lock dir left in leases/" "$(ls -A "$LD/leases")" "lr.json"
ok "acquire event in the run manifest" "$(lev "$LM")" "acquire A1 A1 -"
ok "lease event key <action>:<ts>, ttl_sec" "$(jq -r 'select(.type=="lease")|[(.key|test("^acquire:20")),.ttl_sec,.attempt]|@csv' "$LM")" "true,7200,"
ok "check by the holder (no --run): mine" "$(LZ lease check --sid A1 | head -1)" "state: mine"
ok "check --run lr by another: other" "$(LZ lease check --run lr --sid B2 | head -1)" "state: other"
ok "check without --run by another: own view none (never other), then leases: 1" "$(LZ lease check --sid B2 | head -2)" "state: none
leases: 1"
ok "check --run --json" "$(LZ lease check --run lr --sid B2 --json | jq -c '[.state,.holder,.run,.legacy]')" '["other","A1","lr",false]'
ok "acquire by B2 while A1 holds it live: exit 3" "$(rc LZ lease acquire --run lr --sid B2)" "3"
has "refusal message" "$(LZ lease acquire --run lr --sid B2 2>&1)" "manifest: lease: held by A1 until "
has "refusal names the run and --force" "$(LZ lease acquire --run lr --sid B2 2>&1)" "(run lr); wait for expiry, or ask the owner and rerun with --force"
ok "refused event appended" "$(lev "$LM")" "refused B2 A1 A1"
ok "the refused acquire left the lease alone" "$(jq -r .session "$LF")" "A1"
acq0=$(jq -r .acquired "$LF")
ok "acquire again by A1: renew" "$(LZ lease acquire --run lr --sid A1 2>&1 | cut -d' ' -f1-4)" "lease: renew by A1"
ok "renew keeps acquired" "$(jq -r .acquired "$LF")" "$acq0"
ok "lease renew by B2 (holds none): exit 3" "$(rc LZ lease renew --sid B2)" "3"
ok "lease renew --run lr by B2 (A1's): exit 3" "$(rc LZ lease renew --run lr --sid B2)" "3"
ok "lease renew by A1: exit 0, renew event" "$(rc LZ lease renew --sid A1; lev "$LM")" "0
renew A1 A1 -"
ok "lease release --run lr by B2 (live, A1's): exit 3" "$(rc LZ lease release --run lr --sid B2)" "3"
ok "bare release by B2 (holds none): lease none, exit 0, A1's lease kept" "$(LZ lease release --sid B2; echo $?; jq -r .session "$LF")" "lease: none (nothing to release)
0
A1"
ok "lease commands with no session id: exit 2" "$(rc LZ lease acquire --run lr) $(rc LZ lease renew) $(rc LZ lease release) $(rc LZ lease acquire --run lr --sid=) $(rc LZ lease renew --run lr)" "2 2 2 2 2"
has "no-session message" "$(LZ lease renew --sid= 2>&1)" "lease renew: needs a session id (--sid S, or env WFP_SID)"
ok "acquire on a missing run folder: exit 2" "$(rc LZ lease acquire --run nope --sid A1)" "2"
ok "unknown lease subcommand: exit 2" "$(rc LZ lease grab --sid A1)" "2"
ok "bad --run on renew/release/check: exit 2" "$(rc LZ lease renew --sid A1 --run ../x) $(rc LZ lease release --sid A1 --run a/b) $(rc LZ lease check --run ..)" "2 2 2"
ok "show's lease line (A1)" "$(LZ show --run lr --sid A1 | tail -1 | cut -d' ' -f1-4)" "lease: mine  holder"
ok "show --json lease" "$(LZ show --run lr --sid B2 --json | jq -c '[.lease.state,.lease.holder,.lease.legacy]')" '["other","A1",false]'
LZ event --run lr2 --type start --json '{"tool_use_id":"lr2-s1"}' >/dev/null
ok "show of another run prints that run's own lease (none)" "$(LZ show --run lr2 --sid A1 | tail -1)" "lease: none"

echo "== lease: the per-run launch gate and implicit renewal (D-6; acquire-when-free is row 7's, asserted on lr3 and below)"
LL(){ LZ launch --role implementation --agent orch-builder --mode subagent --repo r --area a --model opus --effort high "$@"; }
ok "launch under another session's live lease on this run: exit 3" "$(rc LL --run lr --row 1 --sid B2)" "3"
has "launch refusal message" "$(LL --run lr --row 1 --sid B2 2>&1)" "manifest: lease: held by A1 until "
ok "a refused launch writes no launch event" "$(jq -c 'select(.type=="launch")' "$LM" | wc -l | tr -d ' ')" "0"
mkdir -p "$LD/runs/lr3"; LF3="$LD/leases/lr3.json"
ok "launch by B2 on another run (lr3, no lease): exit 0; A1's lease on lr does not block it" "$(rc LL --run lr3 --row 1 --sid B2)" "0"
ok "that launch took lr3's free lease (row 7), and lr2 stays unleased" "$(jq -r .session "$LF3") $(jq -r 'select(.type=="lease")|[.action,.session,.holder,(.prev_holder//"-"),.by]|join(" ")' "$LD/runs/lr3/manifest.jsonl") $(gone "$LF2")" "B2 acquire B2 B2 - launch gone "
ok "B2 releases lr3 again (the checks below keep row 6's state)" "$(rc LZ lease release --run lr3 --sid B2) $(gone "$LF3")" "0 gone "
ok "launch by the holder: exit 0" "$(rc LL --run lr --row 1 --sid A1)" "0"
ok "launch with no session id: not checked, exit 0" "$(LL --run lr --row 2 2>&1 >/dev/null; echo $?)" "note: no session id, lease not checked; hooks will not record this run
0"
r0=$(jq -r .renewed "$LF")
LL --run lr --row 3 --sid A1 >/dev/null 2>&1
ok "launch with > leaseRenewWhenLeftSec left: lease not rewritten" "$(jq -r .renewed "$LF")" "$r0"
setexp '+10 min'
LL --run lr --row 4 --sid A1 >/dev/null 2>&1
ok "launch with < leaseRenewWhenLeftSec left: renewed to the full TTL" "$(jq '((.expires|sub("\\.[0-9]+Z$";"Z")|fromdate) - now) > 7000' "$LF")" "true"
ok "implicit renewal appends renew" "$(lev "$LM")" "renew A1 A1 -"
ok "renewIfMine for another sid: no change, prints nothing" "$(ORCH_DIR="$LD" ORCH_RUNS_DIR="$LD/runs" node -e "console.log(JSON.stringify(require('$M').renewIfMine('B2')))" 2>&1)" '{"renewed":false}'
ok "renewIfMine on garbage: false, prints nothing" "$(ORCH_DIR="$LD" node -e "console.log(require('$M').renewIfMine('../x').renewed)" 2>&1)" "false"

echo "== lease: --force steal, expiry, release"
ok "acquire --force by B2: exit 0" "$(rc LZ lease acquire --run lr --sid B2 --force)" "0"
ok "steal event with prev_holder" "$(lev "$LM")" "steal B2 B2 A1"
ok "the lease is B2's" "$(jq -r .session "$LF")" "B2"
setexp '-5 min'
ok "an expired lease: check --run says expired" "$(LZ lease check --run lr --sid A1 | head -1)" "state: expired"
ok "check (no --run) by its expired holder: expired" "$(LZ lease check --sid B2 | head -1)" "state: expired"
ok "show's lease line says expired" "$(LZ show --run lr --sid A1 | tail -1 | cut -d' ' -f1-2)" "lease: expired"
ok "expiry allows acquire by A1: exit 0" "$(rc LZ lease acquire --run lr --sid A1)" "0"
ok "acquire over an expired lease: acquire, prev_holder B2" "$(lev "$LM")" "acquire A1 A1 B2"
setexp '-5 min'
ok "renew of its own expired lease (not since taken): exit 0" "$(rc LZ lease renew --sid A1)" "0"
ok "ACTIVE line 1 names another run: acquire prints nothing on stderr (no ACTIVE text), exit 0" "$(LZ lease acquire --run lr2 --sid A1 2>&1 >/dev/null; echo $?)" "0"
ok "ACTIVE still byte-identical (cmp)" "$(cmp "$T/active.before" "$LD/ACTIVE" && echo same)" "same"
ok "bare release by the holder of lr and lr2: exit 0, both released" "$(LZ lease release --sid A1 | sort; echo $?)" "lease: released by A1 (run lr)
lease: released by A1 (run lr2)
0"
ok "release deletes both per-run files" "$(gone "$LF" "$LF2")" "gone gone "
ok "release events in each lease's run" "$(lev "$LD/runs/lr2/manifest.jsonl") / $(lev "$LM")" "release A1 - - / release A1 - -"
ok "release with no lease: exit 0" "$(LZ lease release --sid A1; echo $?)" "lease: none (nothing to release)
0"
LZ lease acquire --run lr --sid B2 >/dev/null 2>&1; setexp '-1 min'
ok "release --run of another session's expired lease: exit 0" "$(rc LZ lease release --run lr --sid A1)" "0"
ok "that release records prev_holder" "$(lev "$LM")" "release A1 - B2"
LZ lease acquire --run lr --sid B2 >/dev/null 2>&1
ok "release --force without --run: exit 2, nothing released" "$(rc LZ lease release --sid A1 --force; jq -r .session "$LF")" "2
B2"
ok "release --force --run of another's live lease: exit 0, file gone" "$(rc LZ lease release --run lr --sid A1 --force; gone "$LF")" "0
gone "
ok "the ACTIVE file was never rewritten" "$(cmp "$T/active.before" "$LD/ACTIVE" && echo same)" "same"

echo "== lease: several leases (two sids on two runs; one sid on two runs; D-5 verbs, D-7, D-8)"
XA=lsx-sid-alpha-0001; XB=lsx-sid-beta-0002; mkdir -p "$LD/runs/lsx-run-a" "$LD/runs/lsx-run-b" "$LD/runs/lsx-run-c"
XF(){ echo "$LD/leases/lsx-run-$1.json"; }; XM(){ echo "$LD/runs/lsx-run-$1/manifest.jsonl"; }
ok "two sids on two runs: both acquires exit 0" "$(rc LZ lease acquire --run lsx-run-a --sid $XA) $(rc LZ lease acquire --run lsx-run-b --sid $XB)" "0 0"
ok "each run's file names its own holder" "$(jq -r .session "$(XF a)") $(jq -r .session "$(XF b)")" "$XA $XB"
ok "each refuses the other's sid: exit 3 3" "$(rc LZ lease acquire --run lsx-run-a --sid $XB) $(rc LZ lease acquire --run lsx-run-b --sid $XA)" "3 3"
mkdir -p "$LD/runs/lsx-run-x"
ok "launch gate per run: XA on b 3, XA on a 0, XB on x (unleased) 0" "$(rc LL --run lsx-run-b --row 1 --sid $XA) $(rc LL --run lsx-run-a --row 1 --sid $XA) $(rc LL --run lsx-run-x --row 1 --sid $XB)" "3 0 0"
ok "  XB's launch took x's free lease (row 7); c is still unleased" "$(jq -r .session "$(XF x)") $(jq -r 'select(.type=="lease")|[.action,.session,.by]|join(" ")' "$(XM x)") $(gone "$(XF c)")" "$XB acquire $XB launch gone "
ok "  XB releases x again (the checks below keep row 6's state)" "$(rc LZ lease release --run lsx-run-x --sid $XB) $(gone "$(XF x)")" "0 gone "
LZ event --run lsx-run-c --type start --json '{"tool_use_id":"lsx-c-s1"}' >/dev/null   # c has a manifest (it used to come from XB's launch)
ok "show per run: a mine, b other, c none (seen by XA)" "$(LZ show --run lsx-run-a --sid $XA | tail -1 | cut -d' ' -f1-2) | $(LZ show --run lsx-run-b --sid $XA | tail -1 | cut -d' ' -f1-2) | $(LZ show --run lsx-run-c --sid $XA | tail -1)" "lease: mine | lease: other | lease: none"
has "show names the run's own holder" "$(LZ show --run lsx-run-b --sid $XA | tail -1)" "holder ${XB:0:8}  run lsx-run-b"
LZ lease acquire --run lsx-run-c --sid $XA >/dev/null 2>&1
CK=$(LZ lease check --sid $XA)
ok "check (no --run): state line kept, then leases: 3" "$(echo "$CK" | head -2)" "state: mine
leases: 3"
ok "check (no --run): one line per lease, sorted by run" "$(echo "$CK" | sed -n '3,$p' | cut -d' ' -f1-2,7-8 | tr '\n' '|')" "lease: mine run lsx-run-a|lease: other run lsx-run-b|lease: mine run lsx-run-c|"
ok "check --json (no --run): {state, sid, leases}" "$(LZ lease check --sid $XA --json | jq -c '[keys, .state, .sid, (.leases|map(.run+":"+.state)|join(","))]')" "[[\"leases\",\"sid\",\"state\"],\"mine\",\"$XA\",\"lsx-run-a:mine,lsx-run-b:other,lsx-run-c:mine\"]"
ok "check --run lsx-run-b --json: other, legacy false" "$(LZ lease check --run lsx-run-b --sid $XA --json | jq -c '[.state,.holder,.legacy]')" "[\"other\",\"$XB\",false]"
ok "heldLeases export: XA holds a and c, XB holds b" "$(ORCH_DIR="$LD" ORCH_RUNS_DIR="$LD/runs" node -e "const m=require('$M'); console.log(m.heldLeases('$XA').map(x=>x.run).sort().join(','), m.heldLeases('$XB').map(x=>x.run+':'+x.state).join(','), JSON.stringify(m.heldLeases('../x')))")" "lsx-run-a,lsx-run-c lsx-run-b:mine []"
setexp '+10 min' "$(XF a)"; setexp '+10 min' "$(XF c)"; setexp '+10 min' "$(XF b)"; bsum=$(md5sum < "$(XF b)")
RI=$(ORCH_DIR="$LD" ORCH_RUNS_DIR="$LD/runs" node -e "console.log(JSON.stringify(require('$M').renewIfMine('$XA',{run:'lsx-run-a'})))" 2>&1)
ok "renewIfMine(sid, {run}): that run only" "$(echo "$RI" | jq -c '[.renewed,.runs]')" '[true,["lsx-run-a"]]'
ok "  and c is still low" "$(jq '((.expires|sub("\\.[0-9]+Z$";"Z")|fromdate) - now) < 3600' "$(XF c)")" "true"
setexp '+10 min' "$(XF a)"
RI=$(ORCH_DIR="$LD" ORCH_RUNS_DIR="$LD/runs" node -e "console.log(JSON.stringify(require('$M').renewIfMine('$XA')))" 2>&1)
ok "require(manifest).renewIfMine(sid) renews both of its low leases, prints nothing else" "$(echo "$RI" | jq -c '[.renewed,(.runs|sort)]')" '[true,["lsx-run-a","lsx-run-c"]]'
ok "  both now have the full TTL" "$(jq -s 'map(((.expires|sub("\\.[0-9]+Z$";"Z")|fromdate) - now) > 7000) | all' "$(XF a)" "$(XF c)")" "true"
ok "  a renew event in each run" "$(lev "$(XM a)") / $(lev "$(XM c)")" "renew $XA $XA - / renew $XA $XA -"
ok "  the other sid's low lease is untouched (md5)" "$(md5sum < "$(XF b)")" "$bsum"
ok "bare renew acts on both of XA's leases, not b" "$(LZ lease renew --sid $XA | sort | cut -d' ' -f1-4,8- ; md5sum < "$(XF b)")" "lease: renew by ${XA:0:8} (run lsx-run-a)
lease: renew by ${XA:0:8} (run lsx-run-c)
$bsum"
ok "renew --run of the other sid's run: exit 3" "$(rc LZ lease renew --run lsx-run-b --sid $XA)" "3"
ok "release --force without --run: exit 2, nothing released" "$(rc LZ lease release --sid $XA --force; gone "$(XF a)" "$(XF c)")" "2
present present "
ok "bare release frees both of XA's leases and never XB's" "$(LZ lease release --sid $XA | sort | cut -d' ' -f1-3,5-; gone "$(XF a)" "$(XF b)" "$(XF c)")" "lease: released by (run lsx-run-a)
lease: released by (run lsx-run-c)
gone present gone "
ok "check by XA afterwards: none, one lease left (b)" "$(LZ lease check --sid $XA | head -2)" "state: none
leases: 1"

echo "== lease: strict read under the lock (a per-run file that is not a readable lease is an error, never none)"
mkdir -p "$LD/runs/lsx-run-bad"; BF="$LD/leases/lsx-run-bad.json"; echo '{nope' > "$BF"; bsum=$(md5sum < "$BF")
ok "acquire over a malformed per-run file: exit 1, file unchanged" "$(rc LZ lease acquire --run lsx-run-bad --sid A1; md5sum < "$BF")" "1
$bsum"
has "  the error names the file" "$(LZ lease acquire --run lsx-run-bad --sid A1 2>&1)" "lease file $BF exists but is not a readable lease for run lsx-run-bad"
ok "release --run and renew --run over it: exit 1 1" "$(rc LZ lease release --run lsx-run-bad --sid A1) $(rc LZ lease renew --run lsx-run-bad --sid A1)" "1 1"
ok "read side: check --run reads it as none (no write)" "$(LZ lease check --run lsx-run-bad --sid A1; md5sum < "$BF")" "state: none
$bsum"
lfixL(){ printf '{"v":1,"run":"%s","path":"%s","session":"%s","acquired":"%s","renewed":"%s","expires":"%s","ttl_sec":7200}\n' "$2" "$LD/runs/$2" "$3" "$(ago 60)" "$(ago 10)" "$(date -u -d "+100 min" +%Y-%m-%dT%H:%M:%S.000Z)" > "$1"; }
lfixL "$BF" lsx-run-bad A1; chmod 000 "$BF"
ok "an unreadable (chmod 000) per-run file: fixture unreadable, acquire exit 1" "$([ -r "$BF" ] && echo readable || echo unreadable) $(rc LZ lease acquire --run lsx-run-bad --sid B2)" "unreadable 1"
ok "renewIfMine over it: fails open, false, prints nothing" "$(ORCH_DIR="$LD" ORCH_RUNS_DIR="$LD/runs" node -e "console.log(JSON.stringify(require('$M').renewIfMine('A1',{run:'lsx-run-bad'})))" 2>&1)" '{"renewed":false}'
chmod 644 "$BF"; rm -f "$BF"

echo "== lease: atomic acquire (D-4): 20 rounds, a fresh run each, 2 parallel acquires with different sids"
bad=0; badl=""
for i in $(seq -w 1 20); do
  r=lsrace-run-$i; mkdir -p "$LD/runs/$r"
  LZ lease acquire --run $r --sid lsrace-sid-a-$i >/dev/null 2>&1 & p1=$!
  LZ lease acquire --run $r --sid lsrace-sid-b-$i >/dev/null 2>&1 & p2=$!
  wait $p1; e1=$?; wait $p2; e2=$?
  w=$([ $e1 = 0 ] && echo a || echo b)
  got="$(printf '%s\n' $e1 $e2 | sort | tr '\n' ' ')|$(jq -r .session "$LD/leases/$r.json")|$(jq -c 'select(.type=="lease")|.action' "$LD/runs/$r/manifest.jsonl" | sort | tr '\n' ' ')"
  [ "$got" = "0 3 |lsrace-sid-$w-$i|\"acquire\" \"refused\" " ] || { bad=$((bad+1)); badl="$badl [$i: $got]"; }
done
ok "20 rounds: each one 0 and one 3, the winner holds the lease, one acquire and one refused event$badl" "$bad" "0"
bad=0
for i in $(seq -w 1 10); do
  mkdir -p "$LD/runs/lsdiff-run-a$i" "$LD/runs/lsdiff-run-b$i"
  LZ lease acquire --run lsdiff-run-a$i --sid lsrace-sid-c-$i >/dev/null 2>&1 & p1=$!
  LZ lease acquire --run lsdiff-run-b$i --sid lsrace-sid-d-$i >/dev/null 2>&1 & p2=$!
  wait $p1; e1=$?; wait $p2; e2=$?; [ "$e1 $e2" = "0 0" ] || bad=$((bad+1))
done
ok "different runs in parallel: 10 rounds, 0/0 each" "$bad" "0"
ok "no tmp file or lock dir left in leases/" "$(ls -A "$LD/leases" | grep -cv '\.json$')" "0"

echo "== launch acquire-when-free (D-6 row 7): launch --sid takes the run's lease when none or expired, under the run's lock"
LA="$T/orchA"; mkdir -p "$LA/runs"; cp "$REAL_CFG" "$LA/config.json"
AZ(){ ORCH_DIR="$LA" ORCH_RUNS_DIR="$LA/runs" node "$M" "$@"; }
AL(){ AZ launch --role implementation --agent orch-builder --mode subagent --repo r --area a --model opus --effort high "$@"; }
AF(){ echo "$LA/leases/$1.json"; }; AM(){ echo "$LA/runs/$1/manifest.jsonl"; }
aev(){ jq -r 'select(.type=="lease")|[.action,.session,(.holder//"-"),(.prev_holder//"-"),(.by//"-")]|join(" ")' "$(AM "$1")" 2>/dev/null; }
nacq(){ jq -c 'select(.type=="lease" and .action=="acquire")' "$(AM "$1")" 2>/dev/null | wc -l | tr -d ' '; }
nlA(){ jq -c 'select(.type=="launch")' "$(AM "$1")" 2>/dev/null | wc -l | tr -d ' '; }
SA=lacq-sid-a-0001; SB=lacq-sid-b-0002
AL --run la-run-1 --row 1 --sid $SA > "$T/al.out" 2> "$T/al.err"; ec=$?
ok "no lease: launch exit 0, stdout exactly 2 lines (ID, new)" "$ec $(wc -l < "$T/al.out" | tr -d ' ') $(tr '\n' ' ' < "$T/al.out")" "0 2 la-run-1#1.1 new "
ok "  lev: acquire <sid> <sid> - by=launch" "$(aev la-run-1)" "acquire $SA $SA - launch"
ok "  the note is on stderr" "$(sed 's/until .* (run/until _ (run/' "$T/al.err")" "note: lease acquired by ${SA:0:8} until _ (run la-run-1)"
ok "  leases/la-run-1.json: holder, run, path, full TTL, live key order" "$(jq -c '[.session,.run,.path,.ttl_sec]' "$(AF la-run-1)") $(jq -c keys_unsorted "$(AF la-run-1)")" "[\"$SA\",\"la-run-1\",\"$LA/runs/la-run-1\",7200] $LIVEKEYS"
ok "  the acquire precedes the launch event; no ACTIVE or ACTIVE.lease written" "$(jq -r '.type' "$(AM la-run-1)" | tr '\n' ' ')$(gone "$LA/ACTIVE" "$LA/ACTIVE.lease")" "lease launch gone gone "
ok "  no tmp file or lock dir left in leases/" "$(ls -A "$LA/leases")" "la-run-1.json"
r0=$(jq -r .renewed "$(AF la-run-1)")
AL --run la-run-1 --row 2 --sid $SA > "$T/al.out" 2> "$T/al.err"
ok "a second launch by the same sid: no new acquire, lease not rewritten, stderr empty" "$(nacq la-run-1) $(jq -r .renewed "$(AF la-run-1)") $(wc -c < "$T/al.err" | tr -d ' ')" "1 $r0 0"
ok "another session's live lease: launch exit 3" "$(rc AL --run la-run-1 --row 3 --sid $SB)" "3"
ok "  no launch event and no lease event for it" "$(nlA la-run-1) $(aev la-run-1 | wc -l | tr -d ' ')" "2 1"
setexp '-5 min' "$(AF la-run-1)"
AL --run la-run-1 --row 3 --sid $SB > "$T/al.out" 2> "$T/al.err"; ec=$?
ok "another session's expired lease: launch exit 0, acquired with prev_holder" "$ec $(aev la-run-1 | tail -1) $(jq -r .session "$(AF la-run-1)")" "0 acquire $SB $SB $SA launch $SB"
has "  the note names the previous holder" "$(cat "$T/al.err")" "; previous holder ${SA:0:8}"
setexp '-5 min' "$(AF la-run-1)"
AL --run la-run-1 --row 4 --sid $SB >/dev/null 2>&1
ok "its own expired lease: launch re-acquires (prev_holder none)" "$(aev la-run-1 | tail -1) $(jq '((.expires|sub("\\.[0-9]+Z$";"Z")|fromdate) - now) > 7000' "$(AF la-run-1)")" "acquire $SB $SB - launch true"
mkdir -p "$LA/runs/la-run-n"
ok "no sid: launch exit 0, no lease file written, no lease event" "$(rc AL --run la-run-n --row 1) $(gone "$(AF la-run-n)") $(aev la-run-n | wc -l | tr -d ' ')" "0 gone  0"
ok "an empty --sid= is no sid: no lease file" "$(rc AL --run la-run-n --row 2 --sid=) $(gone "$(AF la-run-n)")" "0 gone "
echo "-- 08:20 evidence case, launch only (no lease acquire first)"
printf '{"v":1,"run":"la-run-x","path":"%s","session":"%s","acquired":"%s","renewed":"%s","expires":"%s","ttl_sec":7200}\n' "$LA/runs/la-run-x" "$SA" "$(ago 180)" "$(ago 60)" "$(date -u -d '100 min' +%Y-%m-%dT%H:%M:%S.000Z)" > "$LA/ACTIVE.lease"; mkdir -p "$LA/runs/la-run-x"; xsum=$(md5sum < "$LA/ACTIVE.lease")
ok "08:20 (legacy): A holds run X through ACTIVE.lease; B launches run Y: exit 0" "$(rc AL --run la-run-y --row 1 --sid $SB)" "0"
ok "  B holds Y; acquire by launch; A's legacy file byte-identical; X has no per-run file" "$(jq -r .session "$(AF la-run-y)") $(aev la-run-y) $(md5sum < "$LA/ACTIVE.lease") $(gone "$(AF la-run-x)")" "$SB acquire $SB $SB - launch $xsum gone "
ok "  B's launch on X itself: exit 3, legacy md5 unchanged, no launch event" "$(rc AL --run la-run-x --row 1 --sid $SB) $(md5sum < "$LA/ACTIVE.lease") $(nlA la-run-x)" "3 $xsum 0"
rm -f "$LA/ACTIVE.lease"
AZ lease acquire --run la-run-x --sid $SA >/dev/null 2>&1; psum=$(md5sum < "$(AF la-run-x)")
ok "08:20 (per-run): A holds X in leases/; B launches run Z: exit 0, B holds Z, X's file unchanged" "$(rc AL --run la-run-z --row 1 --sid $SB) $(jq -r .session "$(AF la-run-z)") $(md5sum < "$(AF la-run-x)")" "0 $SB $psum"
echo "-- launch race: 20 rounds, a fresh run each, 2 parallel launches with different sids"
bad=0; badl=""
for i in $(seq -w 1 20); do
  r=larace-run-$i; X=larace-sid-x-$i; Y=larace-sid-y-$i
  AL --run $r --row 1 --sid $X >/dev/null 2>&1 & p1=$!
  AL --run $r --row 1 --sid $Y >/dev/null 2>&1 & p2=$!
  wait $p1; e1=$?; wait $p2; e2=$?
  w=$([ $e1 = 0 ] && echo $X || echo $Y)
  got="$(printf '%s\n' $e1 $e2 | sort | tr '\n' ' ')|$(jq -r .session "$(AF $r)" 2>/dev/null)|$(nacq $r)|$(nlA $r)|$(jq -r 'select(.type=="launch")|.session' "$(AM $r)" 2>/dev/null)|$(jq -c 'select(.type=="lease" and .action!="acquire")' "$(AM $r)" 2>/dev/null | wc -l | tr -d ' ')"
  [ "$got" = "0 3 |$w|1|1|$w|0" ] || { bad=$((bad+1)); badl="$badl [$i: $got]"; }
done
ok "20 rounds: one 0 and one 3, the winner holds the lease, one acquire and one launch event (the winner's), nothing else$badl" "$bad" "0"
bad=0; badl=""
for i in $(seq -w 1 10); do
  r=lasame-run-$i; X=lasame-sid-$i
  AL --run $r --row 1 --sid $X >/dev/null 2>&1 & p1=$!
  AL --run $r --row 2 --sid $X >/dev/null 2>&1 & p2=$!
  wait $p1; e1=$?; wait $p2; e2=$?
  got="$e1 $e2|$(nacq $r)|$(nlA $r)|$(jq -r .session "$(AF $r)" 2>/dev/null)"
  [ "$got" = "0 0|1|2|$X" ] || { bad=$((bad+1)); badl="$badl [$i: $got]"; }
done
ok "same sid, different --row, in parallel (10 rounds): 0/0, one acquire, two launches$badl" "$bad" "0"
bad=0; badl=""
for i in $(seq -w 1 10); do
  X=ladiff-sid-x-$i; Y=ladiff-sid-y-$i
  AL --run ladiff-run-x-$i --row 1 --sid $X >/dev/null 2>&1 & p1=$!
  AL --run ladiff-run-y-$i --row 1 --sid $Y >/dev/null 2>&1 & p2=$!
  wait $p1; e1=$?; wait $p2; e2=$?
  got="$e1 $e2|$(jq -r .session "$(AF ladiff-run-x-$i)" 2>/dev/null) $(jq -r .session "$(AF ladiff-run-y-$i)" 2>/dev/null)"
  [ "$got" = "0 0|$X $Y" ] || { bad=$((bad+1)); badl="$badl [$i: $got]"; }
done
ok "different runs in parallel (10 rounds): 0/0, each holds its own run$badl" "$bad" "0"
ok "no tmp file or lock dir left in leases/ after the races" "$(ls -A "$LA/leases" | grep -cv '\.json$')" "0"
ok "help: launch takes the run's lease when it is free" "$(node "$M" help | grep -c "it takes the run's lease when it is free")" "1"

echo "== legacy ACTIVE.lease (D-2): fixture in the live key order, synthetic values; the holder's first write migrates it"
LG="$T/orchG"; mkdir -p "$LG/runs"; cp "$REAL_CFG" "$LG/config.json"; printf '%s\n' "$LG/runs/lgfix-run-a" > "$LG/ACTIVE"
GZ(){ ORCH_DIR="$LG" ORCH_RUNS_DIR="$LG/runs" node "$M" "$@"; }
GL(){ GZ launch --role implementation --agent orch-builder --mode subagent --repo r --area a --model opus --effort high "$@"; }
GA="$LG/ACTIVE.lease"; GF(){ echo "$LG/leases/$1.json"; }; GM(){ echo "$LG/runs/$1/manifest.jsonl"; }
S16=lgfix-sid-holder-0016; S35=lgfix-sid-other-0035
inm(){ date -u -d "$1 min" +%Y-%m-%dT%H:%M:%S.000Z; }
# legacy <run> <sid> <expires: minutes from now>: write the legacy file, keys in the live order, acquired 3 h ago, renewed 1 h ago
legacy(){ mkdir -p "$LG/runs/$1"; printf '{"v":1,"run":"%s","path":"%s","session":"%s","acquired":"%s","renewed":"%s","expires":"%s","ttl_sec":7200}\n' "$1" "$LG/runs/$1" "$2" "$(ago 180)" "$(ago 60)" "$(inm "$3")" > "$GA"; }
gtree(){ (cd "$LG" && find . -exec stat -c '%n %.9Y %s %a' {} + | sort) | md5sum; }
LIVEK=$(jq -c keys_unsorted "$LIVE/ACTIVE.lease" 2>/dev/null || echo "$LIVEKEYS")
legacy lgfix-run-a $S16 100; mkdir -p "$LG/runs/lgfix-run-b"; asum=$(md5sum < "$GA")
for r in lgfix-run-a lgfix-run-b; do GZ event --run $r --type start --json '{"tool_use_id":"lg-s1"}' >/dev/null; done
ok "fixture keys match the live ACTIVE.lease order (jq keys_unsorted, read only)" "$(jq -c keys_unsorted "$GA")" "$LIVEK"
G0=$(gtree)
ok "check --run on the legacy run: other, legacy: yes" "$(GZ lease check --run lgfix-run-a --sid $S35 | sed -n '1p;5p')" "state: other
legacy: yes"
ok "check (no --run) by the holder: mine, leases 1, tagged legacy" "$(GZ lease check --sid $S16 | head -2) $(GZ lease check --sid $S16 | sed -n 3p | grep -c '(legacy ACTIVE.lease)')" "state: mine
leases: 1 1"
has "show --run tags the legacy file" "$(GZ show --run lgfix-run-a --sid $S16 | tail -1)" "lease: mine  holder ${S16:0:8}  run lgfix-run-a"
has "  (legacy ACTIVE.lease)" "$(GZ show --run lgfix-run-a --sid $S16 | tail -1)" "(legacy ACTIVE.lease)"
ok "show of another run: none (the legacy lease covers only its own run)" "$(GZ show --run lgfix-run-b --sid $S16 | tail -1)" "lease: none"
GZ lease check --sid $S35 --json >/dev/null; GZ lease check --run lgfix-run-b >/dev/null; GZ show --run lgfix-run-a --json >/dev/null
ok "check and show wrote nothing (tree: names, mtimes, sizes, modes)" "$(gtree)" "$G0"
echo "-- a live legacy lease blocks only its own run"
ok "another sid's acquire on the legacy run: exit 3, legacy md5 unchanged, no per-run file" "$(rc GZ lease acquire --run lgfix-run-a --sid $S35) $(md5sum < "$GA") $(gone "$(GF lgfix-run-a)")" "3 $asum gone "
has "  the refusal says it is the legacy lease" "$(GZ lease acquire --run lgfix-run-a --sid $S35 2>&1)" "through the legacy ACTIVE.lease; --force does not take a live legacy lease"
ok "  --force does not take it either (D-2 c, d): exit 3, md5 unchanged" "$(rc GZ lease acquire --run lgfix-run-a --sid $S35 --force) $(md5sum < "$GA")" "3 $asum"
ok "  release --force --run by another sid: exit 3, md5 unchanged" "$(rc GZ lease release --run lgfix-run-a --sid $S35 --force) $(md5sum < "$GA")" "3 $asum"
ok "  another sid's launch on the legacy run: exit 3, no launch event" "$(rc GL --run lgfix-run-a --row 1 --sid $S35) $(jq -c 'select(.type=="launch")' "$(GM lgfix-run-a)" 2>/dev/null | wc -l | tr -d ' ')" "3 0"
ok "08:20 case (legacy): S35 acquires and launches run B while S16 holds A through ACTIVE.lease: 0 0" "$(rc GZ lease acquire --run lgfix-run-b --sid $S35) $(rc GL --run lgfix-run-b --row 1 --sid $S35)" "0 0"
ok "  S35 holds B; S16's legacy file byte-identical" "$(jq -r .session "$(GF lgfix-run-b)") $(md5sum < "$GA")" "$S35 $asum"
echo "-- -16 after the land: its launch and renew against the legacy lease still work"
# (row 7) the holder's launch with > 3600 s left now migrates: that case runs on its own legacy run, lgfix-run-l, below
ok "holder renewIfMine with > 3600 s left: false, no write" "$(ORCH_DIR="$LG" ORCH_RUNS_DIR="$LG/runs" node -e "console.log(JSON.stringify(require('$M').renewIfMine('$S16',{hook:true})))" 2>&1) $(md5sum < "$GA")" "{\"renewed\":false} $asum"
jq -c '.expires="'"$(inm 10)"'"' "$GA" > "$GA.t" && mv "$GA.t" "$GA"; A0=$(jq -r .acquired "$GA"); E0=$(jq -r .expires "$GA")
ok "holder renew of a low legacy lease: exit 0" "$(rc GZ lease renew --sid $S16)" "0"
ok "  the legacy file is gone; the per-run file holds S16" "$(gone "$GA") $(jq -r .session "$(GF lgfix-run-a)")" "gone  $S16"
ok "  per-run keeps acquired and the live key order" "$(jq -r .acquired "$(GF lgfix-run-a)") $(jq -c keys_unsorted "$(GF lgfix-run-a)")" "$A0 $LIVEK"
ok "  expires moved forward (full TTL)" "$(jq --arg e0 "$E0" '((.expires|sub("\\.[0-9]+Z$";"Z")|fromdate) - now) > 7000 and .expires > $e0' "$(GF lgfix-run-a)")" "true"
ok "  one migrate event with the legacy acquired and expires, by renew; then renew" "$(jq -c 'select(.type=="lease" and .action!="refused")|[.action,.session,.holder,.acquired,.expires,.by]' "$(GM lgfix-run-a)" | tr '\n' ' ')" "[\"migrate\",\"$S16\",\"$S16\",\"$A0\",\"$E0\",\"renew\"] [\"renew\",\"$S16\",\"$S16\",null,$(jq -c .expires "$(GF lgfix-run-a)"),null] "
ok "  S35's lease on B untouched" "$(jq -r .session "$(GF lgfix-run-b)")" "$S35"
ok "08:20 case (per-run): S16 holds A per-run; S35 acquires and launches run C: 0 0, A's file unchanged" "$(asum2=$(md5sum < "$(GF lgfix-run-a)"); mkdir -p "$LG/runs/lgfix-run-c"; echo "$(rc GZ lease acquire --run lgfix-run-c --sid $S35) $(rc GL --run lgfix-run-c --row 1 --sid $S35) $([ "$(md5sum < "$(GF lgfix-run-a)")" = "$asum2" ] && echo same)")" "0 0 same"
echo "-- (row 7) the holder's launch with > 3600 s left migrates its legacy lease, expires unchanged; the hook then records a start"
legacy lgfix-run-l $S16 100; A0=$(jq -r .acquired "$GA"); E0=$(jq -r .expires "$GA"); R0=$(jq -r .renewed "$GA")
GL --run lgfix-run-l --row 1 --sid $S16 > "$T/gl.out" 2> "$T/gl.err"; ec=$?; GID=$(head -1 "$T/gl.out")
ok "holder launch with > 3600 s left: exit 0, stdout ID + new, stderr empty (no acquire)" "$ec $(tr '\n' ' ' < "$T/gl.out")$(wc -c < "$T/gl.err" | tr -d ' ')" "0 lgfix-run-l#1.1 new 0"
ok "  migrated: legacy gone, per-run S16 with acquired, renewed and expires unchanged, live key order" "$(gone "$GA") $(jq -r '[.session,.acquired,.renewed,.expires]|join(" ")' "$(GF lgfix-run-l)") $(jq -c keys_unsorted "$(GF lgfix-run-l)")" "gone  $S16 $A0 $R0 $E0 $LIVEK"
ok "  lease events: one migrate by launch with the legacy acquired and expires; no acquire, no renew" "$(jq -c 'select(.type=="lease")|[.action,.session,.holder,.acquired,.expires,.by]' "$(GM lgfix-run-l)" | tr '\n' ' ')" "[\"migrate\",\"$S16\",\"$S16\",\"$A0\",\"$E0\",\"launch\"] "
ok "  one launch event, session S16" "$(jq -r 'select(.type=="launch")|.attempt+" "+.session' "$(GM lgfix-run-l)")" "$GID $S16"
GTR="$T/gl-transcript.jsonl"; : > "$GTR"
GPRE=$(jq -cn --arg sid "$S16" --arg tp "$GTR" --arg d "[opus/high] $GID legacy end to end" --arg p "Orch-Attempt: $GID" \
  '{session_id:$sid, transcript_path:$tp, hook_event_name:"PreToolUse", tool_name:"Agent", tool_use_id:"toolu_lgl1", tool_input:{description:$d, prompt:$p, subagent_type:"orch-builder", model:"opus", effort:"high", run_in_background:true}}')
ok "  orch-attempts.js pretool with that ID: exit 0" "$(printf '%s' "$GPRE" | ORCH_DIR="$LG" ORCH_RUNS_DIR="$LG/runs" ORCH_PROJECTS_DIR="$T/projects" node "$CL/hooks/orch-attempts.js" pretool >/dev/null 2>&1; echo $?)" "0"
ok "  the hook recorded a start for that attempt, session S16, in the run's manifest" "$(jq -r 'select(.type=="start" and .key=="toolu_lgl1")|[.attempt,.session,.via]|join(" ")' "$(GM lgfix-run-l)")" "$GID $S16 pretool"
echo "-- the prompt/stop hooks' renewIfMine on a low legacy lease migrates it"
legacy lgfix-run-d $S16 10; A0=$(jq -r .acquired "$GA")
ok "renewIfMine(sid, {hook:true}): renewed, only the low runs" "$(ORCH_DIR="$LG" ORCH_RUNS_DIR="$LG/runs" node -e "console.log(JSON.stringify(require('$M').renewIfMine('$S16',{hook:true}).runs))" 2>&1)" '["lgfix-run-d"]'
ok "  migrated: legacy gone, per-run S16, acquired kept, migrate by hook" "$(gone "$GA") $(jq -r '.session+" "+.acquired' "$(GF lgfix-run-d)") $(jq -r 'select(.action=="migrate")|.by' "$(GM lgfix-run-d)")" "gone  $S16 $A0 hook"
echo "-- after expiry, another session's acquire takes the legacy run over"
legacy lgfix-run-e $S16 -5
ok "S35 acquires the expired legacy run: exit 0, per-run S35, legacy gone" "$(rc GZ lease acquire --run lgfix-run-e --sid $S35) $(jq -r .session "$(GF lgfix-run-e)") $(gone "$GA")" "0 $S35 gone "
ok "  events: migrate (holder S16, by acquire), then acquire with prev_holder S16" "$(jq -r 'select(.type=="lease")|[.action,.session,.holder,(.prev_holder//"-"),(.by//"-")]|join(" ")' "$(GM lgfix-run-e)" | tr '\n' '|')" "migrate $S35 $S16 - acquire|acquire $S35 $S35 $S16 -|"
echo "-- release removes the legacy file; a malformed legacy file is none and stays"
legacy lgfix-run-f $S16 100
ok "holder release --run of the legacy run: exit 0, legacy gone, no per-run file" "$(rc GZ lease release --run lgfix-run-f --sid $S16) $(gone "$GA" "$(GF lgfix-run-f)")" "0 gone gone "
ok "  events: migrate, release" "$(jq -r 'select(.type=="lease")|.action' "$(GM lgfix-run-f)" | tr '\n' ' ')" "migrate release "
legacy lgfix-run-g $S16 100
ok "bare release by the holder frees the legacy lease and its per-run ones" "$(GZ lease release --sid $S16 | sort | sed 's/ by [^ ]* / /'; gone "$GA" "$(GF lgfix-run-a)" "$(GF lgfix-run-d)" "$(GF lgfix-run-g)" "$(GF lgfix-run-l)")" "lease: released (run lgfix-run-a)
lease: released (run lgfix-run-d)
lease: released (run lgfix-run-g)
lease: released (run lgfix-run-l)
gone gone gone gone gone "
mkdir -p "$LG/runs/lgfix-run-h"; echo '{"v":1,"run":"lgfix-run-h"' > "$GA"; msum=$(md5sum < "$GA")
ok "malformed legacy: reads as none, acquire exit 0, legacy left in place" "$(GZ lease check --run lgfix-run-h --sid $S35 | head -1) $(rc GZ lease acquire --run lgfix-run-h --sid $S35) $(md5sum < "$GA")" "state: none 0 $msum"
rm -f "$GA"

echo "== legacy migration race (D-2 a/b, W4): 20 rounds, holder renew vs another sid's acquire on an expired legacy lease"
bad=0; badl=""
for i in $(seq -w 1 20); do
  r=lgrace-run-$i; H=lgrace-sid-h-$i; O=lgrace-sid-o-$i; legacy $r $H -5
  GZ lease renew --sid $H >/dev/null 2>&1 & p1=$!
  GZ lease acquire --run $r --sid $O >/dev/null 2>&1 & p2=$!
  wait $p1; e1=$?; wait $p2; e2=$?
  w=$([ $e1 = 0 ] && echo $H || echo $O)
  got="$(printf '%s\n' $e1 $e2 | sort | tr '\n' ' ')|$(jq -r .session "$(GF $r)" 2>/dev/null)|$(gone "$GA")|$(jq -c 'select(.action=="migrate")' "$(GM $r)" | wc -l | tr -d ' ')"
  [ "$got" = "0 3 |$w|gone |1" ] || { bad=$((bad+1)); badl="$badl [$i: $got]"; }
  rm -f "$GA"
done
ok "20 rounds: one 0 and one 3, the winner holds the per-run lease, no legacy file, one migrate event$badl" "$bad" "0"
ok "no tmp file or lock dir left (leases/, ACTIVE.lease.lock)" "$(ls -A "$LG/leases" | grep -cv '\.json$') $(gone "$LG/ACTIVE.lease.lock")" "0 gone "
ok "new code never wrote ACTIVE.lease: no legacy file is left in either scratch dir" "$(gone "$LD/ACTIVE.lease" "$GA")" "gone gone "
ok "help: one lease per run, no 'one global lease'" "$(node "$M" help | grep -c 'one global lease') $(node "$M" help | grep -c 'one lease per run in leases/<run>.json')" "0 1"

echo "== reconcile (design §7): fixtures 1-10, G1, A-19"
G(){ git -C "$1" -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false "${@:2}"; }
mkrepo(){ mkdir -p "$1"; git -C "$1" init -q -b main; G "$1" commit -q --allow-empty -m "${2:-chore: init}"; }
LR(){ node "$M" launch --agent orch-builder --mode subagent --repo r --area a --model opus --effort high "$@"; }
RC(){ node "$M" reconcile "$@"; }
rowline(){ echo "$1" | grep "^$2 | "; }
result(){ # file status sha [attempt] [sha_reason]
  { echo '---'; echo "row: x"; echo "status: $2"; [ -n "$3" ] && echo "sha: $3"; [ -n "${4:-}" ] && echo "attempt: $4"; [ -n "${5:-}" ] && echo "sha_reason: $5"; echo '---'; echo 'body'; } > "$1"; }
start(){ # run attempt tool_use_id session
  : >> "$PJ/$4.jsonl"; E --run "$1" --type start --json "{\"attempt\":\"$2\",\"tool_use_id\":\"$3\",\"transcript_path\":\"$PJ/$4.jsonl\",\"session\":\"$4\"}"; }
fstop(){ E --run "$1" --type stop --json "{\"attempt\":\"$2\",\"task_id\":\"$3\",\"phase\":\"${4:-finished}\",\"tools\":1,\"ms\":1}"; }
RD(){ echo "$ORCH_RUNS_DIR/$1"; }

# fixture 1: done (trailer commit, matching result status done, a stop)
R=f1; RP="$T/repo1"; mkrepo "$RP"
LR --run $R --row 1 --role implementation --git "$RP" --sid SF1 >/dev/null 2>&1
start $R "$R#1.1" tu-f1 SF1; fstop $R "$R#1.1" k-f1
G "$RP" commit -q --allow-empty -m "feat(x): thing" -m "Orch-Attempt: $R#1.1"; SHA1=$(git -C "$RP" rev-parse HEAD)
G "$RP" commit -q --allow-empty -m "feat(x): decoy" -m "Orch-Attempt: $R#1.12"
G "$RP" commit -q --allow-empty -m "feat(x): decoy 2" -m "Orch-Attempt: $R#1x1"
result "$(RD $R)/1-thing.md" done "${SHA1:0:7}" "$R#1.1"
OUT=$(RC --run $R --sid SF1); st=$?
ok "reconcile exits 0" "$st" "0"
has "fixture 1: done" "$(rowline "$OUT" 1)" "1 | $R#1.1 | done done | stop finished"
has "fixture 1: commit sha@branch" "$(rowline "$OUT" 1)" "commit ${SHA1:0:7}@main"
has "fixture 1: next advance" "$(rowline "$OUT" 1)" "| advance"
lacks "fixture 1: the trailer grep is anchored and escaped (no multiple-commits)" "$(rowline "$OUT" 1)" "multiple-commits"
ok "fixture 1: --json" "$(RC --run $R --sid SF1 --json | jq -c '.rows[0]|[.verdict,.next,.status,.evidence.commit.sha==$s,.evidence.commit.branches]' --arg s "$SHA1")" '["done","advance","done",true,["main"]]'
ok "the header row" "$(echo "$OUT" | grep -c '^row | attempt | verdict | evidence | next$')" "1"

# fixture 2: done without a commit (sha: none with a reason, role review); plus the inline-comment and read-only forms
R=f2; mkdir -p "$(RD $R)"
LR --run $R --row 1 --role review >/dev/null 2>&1; start $R "$R#1.1" tu-f2a SF2; fstop $R "$R#1.1" k-f2a
result "$(RD $R)/1-review.md" done none "$R#1.1" "read-only review"
LR --run $R --row 2 --role implementation >/dev/null 2>&1; start $R "$R#2.1" tu-f2b SF2; fstop $R "$R#2.1" k-f2b
result "$(RD $R)/2-cfg.md" done "none # settings.json is ignored"
LR --run $R --row 3 --role discovery >/dev/null 2>&1; start $R "$R#3.1" tu-f2c SF2; fstop $R "$R#3.1" k-f2c
result "$(RD $R)/3-scout.md" done none
LR --run $R --row 4 --role implementation >/dev/null 2>&1; start $R "$R#4.1" tu-f2d SF2; fstop $R "$R#4.1" k-f2d
result "$(RD $R)/4-x.md" done none
OUT=$(RC --run $R)
has "fixture 2: done without a commit" "$(rowline "$OUT" 1)" "1 | $R#1.1 | done done | stop finished"
has "fixture 2: sha none and its reason shown" "$(rowline "$OUT" 1)" "sha: none (read-only review) | advance"
has "inline '# reason' after sha: none counts as the reason" "$(rowline "$OUT" 2)" "| done done |"
has "read-only role: sha none needs no reason" "$(rowline "$OUT" 3)" "| done done |"
has "mutation role, sha none, no reason: unknown" "$(rowline "$OUT" 4)" "| unknown sha-none-without-reason |"

# fixture 3: unknown, F01 negative (the repo's only commit has the expected subject prefix; no trailer, no result)
R=f3; RP="$T/repo3"; mkdir -p "$(RD $R)"; mkrepo "$RP" "fix(ui): unrelated old work"; echo x > "$RP/stray.txt"
LR --run $R --row 1 --role implementation --git "$RP" --subtask "fix(ui): the change" >/dev/null 2>&1
OUT=$(RC --run $R)
has "fixture 3 (F01): unknown, not done" "$(rowline "$OUT" 1)" "1 | $R#1.1 | unknown no-stop,no-result,no-trailer-commit |"
has "fixture 3: next redispatch" "$(rowline "$OUT" 1)" "| redispatch"
has "fixture 3: git status --short printed before re-dispatch" "$OUT" "git -C $RP status --short:"
has "fixture 3: the dirty file is listed" "$OUT" "?? stray.txt"

# fixture 4: legacy (no launch event; decided by the result file)
R=f4; mkdir -p "$(RD $R)"; result "$(RD $R)/4-old.md" done abc1234
OUT=$(RC --run $R --row 4,5)
has "fixture 4: legacy with status done" "$(rowline "$OUT" 4)" "4 | - | legacy done | stop none · result done@$(RD $R)/4-old.md · sha abc1234 | old-rule (result file)"
has "legacy with no result: HANDOFF's commit check" "$(rowline "$OUT" 5)" "5 | - | legacy no-result | stop none · result none | old-rule (HANDOFF's commit check)"
ok "no --row and no launch events: no rows" "$(RC --run $R | grep -c ' | ')" "1"

# fixture 5: live (meta.json without finished, --sid equals the start's session); fixture 10: the same agent from another session
R=f5; mkdir -p "$(RD $R)"
LR --run $R --row 1 --role implementation --sid SF5 >/dev/null 2>&1; start $R "$R#1.1" tu-f5 SF5
mkdir -p "$PJ/SF5/subagents"; echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $R#1.1 x\",\"toolUseId\":\"tu-f5\"}" > "$PJ/SF5/subagents/agent-f5.meta.json"
has "fixture 5: unknown live" "$(rowline "$(RC --run $R --sid SF5)" 1)" "| unknown live,no-result |"
has "fixture 5: next wait" "$(rowline "$(RC --run $R --sid SF5)" 1)" "| wait"
has "fixture 10 (G10): unknown unfinished-other-session" "$(rowline "$(RC --run $R --sid OTHER)" 1)" "| unknown unfinished-other-session,no-result |"
has "fixture 10: next ask" "$(rowline "$(RC --run $R --sid OTHER)" 1)" "| ask"
has "no --sid: never live" "$(rowline "$(RC --run $R)" 1)" "| unknown unfinished-other-session,no-result |"

# idle stop: a completed 'waiting' stop is the final stop for reconcile (no stop none / no-stop), a started meta.json-backed attempt included
R=f5w; mkdir -p "$(RD $R)"
LR --run $R --row 1 --role implementation --sid SF5W >/dev/null 2>&1; start $R "$R#1.1" tu-f5w SF5W
mkdir -p "$PJ/SF5W/subagents"; echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $R#1.1 x\",\"toolUseId\":\"tu-f5w\"}" > "$PJ/SF5W/subagents/agent-f5w.meta.json"
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"f5w\",\"phase\":\"waiting\",\"status\":\"completed\",\"tools\":1,\"ms\":1}"
has "idle stop: reconcile sees the waiting stop, not 'live'" "$(rowline "$(RC --run $R --sid SF5W)" 1)" "| unknown no-result | stop waiting"

# item 14: an interim waiting notice recorded by ingest is provisional ("provisional": true): the row waits, it is not done; the final notice replaces it;
# close / waiting-timeout (config waitingGraceSec, default 1800 s) promote it. The block above is the OLD format (no marker): unchanged.
echo "== provisional waiting notice (item 14): waits, final replaces it, waiting-timeout fallback"
PW(){ # run task age_min [tokens] : a provisional waiting stop for <run>#1.1, <age_min> minutes old
  E --run "$1" --type stop --json "{\"attempt\":\"$1#1.1\",\"task_id\":\"$2\",\"tool_use_id\":\"tu-$1\",\"phase\":\"waiting\",\"status\":\"completed\",\"provisional\":true,\"ts\":\"$(ago $3)\",\"tokens\":${4:-80},\"tools\":2,\"ms\":50,\"model\":\"opus\",\"effort\":\"high\",\"agent\":\"orch-builder\"}"; }
R=f5p; mkdir -p "$(RD $R)"
LR --run $R --row 1 --role implementation --sid SF5P >/dev/null 2>&1; start $R "$R#1.1" tu-$R SF5P
mkdir -p "$PJ/SF5P/subagents"; echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $R#1.1 x\",\"toolUseId\":\"tu-$R\"}" > "$PJ/SF5P/subagents/agent-f5p.meta.json"
result "$(RD $R)/1-x.md" done none "$R#1.1" "no commit by design"
PW $R f5p 5
O=$(RC --run $R --sid SF5P); LN=$(rowline "$O" 1)
has "provisional: reconcile says waiting, with a matching result file present" "$LN" "| unknown waiting |"
has "provisional: reconcile names it" "$LN" "stop none (waiting on its own background work, interim notice 5m old)"
ok "provisional: next is wait, not advance" "$(echo "$LN" | awk -F' [|] ' '{print $NF}')" "wait"
ok "provisional: reconcile --json: no stop, waiting with its figures" "$(RC --run $R --sid SF5P --json | jq -c '.rows[0]|[.verdict,.next,.evidence.stop,.evidence.waiting.tokens,.evidence.waiting.tools,.evidence.waiting.ms]')" '["unknown","wait",null,80,2,50]'
ok "provisional: young notice, nothing promoted, no waiting-timeout word" "$(echo "$O" | grep -c 'waiting-timeout')|$(jq -c 'select(.type=="stop" and .final==true)' "$(MF $R)" | wc -l | tr -d ' ')|$([ -e "$(RD $R)/ledger.jsonl" ] && echo line || echo noline)" "0|0|noline"
ok "provisional: the stored stop keeps its figures and is marked" "$(jq -c 'select(.type=="stop")|[.provisional,.final,.tokens,.tools,.ms]' "$(MF $R)")" "[true,false,80,2,50]"
SO=$(node "$M" show --run $R)
has "provisional: show lists it open, state waiting, flagged waiting-own-work" "$SO" "$R#1.1  waiting"
has "provisional: show flag" "$SO" "[waiting-own-work]"
has "provisional: status says waiting on its own background work" "$(node "$M" status --run $R)" "waiting (own background work)"
ok "provisional: status --json: row open, waiting, not done" "$(node "$M" status --run $R --json | jq -c '.rows[0]|[.open,.waiting,.done]')" "[true,true,false]"
ok "provisional: launch --continue is refused (exit 2) and writes no launch" "$(rc L --run $R --row 1 --model opus --effort high --continue "$R#1.1")|$(nlaunch $R)" "2|1"
has "provisional: the refusal says why and names the way out" "$(L --run $R --row 1 --model opus --effort high --continue "$R#1.1" 2>&1 >/dev/null)" "waiting on its own background work (interim notice, not final)"
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"f5p\",\"tool_use_id\":\"tu-$R\",\"phase\":\"finished\",\"status\":\"completed\",\"tokens\":200,\"tools\":7,\"ms\":500,\"model\":\"opus\",\"effort\":\"high\"}"
O=$(RC --run $R --sid SF5P)
has "interim then final: the row is done, next advance, via the final stop" "$(rowline "$O" 1)" "| done done | stop finished · result done@"
ok "interim then final: the final stop speaks (figures 200/7/500), the interim one stays on record" "$(node "$M" show --run $R --json | jq -c '.attempts[0]|[.state,(.stops|length),.stops[-1].tokens]')" '["finished",2,200]'
ok "interim then final: launch --continue is allowed now" "$(rc L --run $R --row 1 --model opus --effort high --continue "$R#1.1")" "0"
# the fallback: reconcile promotes a provisional notice older than the grace period, with no later notice
R=f5q; mkdir -p "$(RD $R)"
LR --run $R --row 1 --role implementation --sid SF5Q >/dev/null 2>&1; start $R "$R#1.1" tu-$R SF5Q
mkdir -p "$PJ/SF5Q/subagents"; echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $R#1.1 x\",\"toolUseId\":\"tu-$R\",\"finished\":true}" > "$PJ/SF5Q/subagents/agent-f5q.meta.json"
result "$(RD $R)/1-x.md" done none "$R#1.1" "no commit by design"
PW $R f5q 40 90
O=$(RC --run $R --sid SF5Q); LN=$(rowline "$O" 1)
has "waiting-timeout: reconcile says so, in one word" "$O" "$R#1.1: waiting-timeout: interim waiting notice older than 30 min with no later notice; promoted (waiting-timeout), ledger line written"
has "waiting-timeout: the row is done, via the promoted stop" "$LN" "| done done | stop finished (promoted: waiting-timeout)"
ok "waiting-timeout: the promoted stop copies the figures; the ledger line carries them and the marker" "$(jq -c 'select(.type=="stop" and .final==true)|[.phase,.promoted,.tokens,.tools,.ms]' "$(MF $R)")|$(jq -c '[.tokens,.tools,.ms,.promoted]' "$(RD $R)/ledger.jsonl")" '["finished","waiting-timeout",90,2,50]|[90,2,50,"waiting-timeout"]'
O=$(RC --run $R --sid SF5Q)
ok "waiting-timeout: a second reconcile promotes nothing more (idempotent)" "$(echo "$O" | grep -c 'waiting-timeout: interim')|$(jq -c 'select(.type=="stop" and .final==true)' "$(MF $R)" | wc -l | tr -d ' ')|$(wc -l < "$(RD $R)/ledger.jsonl" | tr -d ' ')" "0|1|1"
# the promoted stop's end time is the waiting notice's (ended_at); its ts stays the write time, so ordering and the duplicate key are untouched
W=$(jq -r 'select(.type=="stop" and .provisional==true)|.ts' "$(MF $R)")
ok "waiting-timeout: ended_at on the promoted stop is the waiting notice's ts; its own ts is the later promotion time; ms and tokens are the notice's" "$(jq -r 'select(.type=="stop" and .final==true)|[(.ended_at=="'$W'"),(.ts>"'$W'"),.ms,.tokens,.key]|tostring' "$(MF $R)")" '[true,true,50,90,"f5q:promoted:waiting-timeout"]'
ok "waiting-timeout: reconcile --json shows the waiting notice's time as the stop's end, and the ledger line's ts is the same" "$(RC --run $R --sid SF5Q --json | jq -r '.rows[0].evidence.stop.ts')|$(jq -r .ts "$(RD $R)/ledger.jsonl")" "$W|$W"
# a promoted stop written before the field existed reads as before: its own ts
R=f5u; mkdir -p "$(RD $R)"
LR --run $R --row 1 --role implementation --sid SF5U >/dev/null 2>&1; start $R "$R#1.1" tu-$R SF5U
mkdir -p "$PJ/SF5U/subagents"; echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $R#1.1 x\",\"toolUseId\":\"tu-$R\",\"finished\":true}" > "$PJ/SF5U/subagents/agent-f5u.meta.json"
result "$(RD $R)/1-x.md" done none "$R#1.1" "no commit by design"
OLD=$(ago 100)
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"f5u\",\"tool_use_id\":\"tu-$R\",\"phase\":\"finished\",\"status\":\"completed\",\"final\":true,\"promoted\":\"close\",\"ts\":\"$OLD\",\"tokens\":70,\"tools\":2,\"ms\":50,\"model\":\"opus\",\"effort\":\"high\",\"agent\":\"orch-builder\"}"
ok "an old promoted stop (no ended_at): reconcile --json shows its own ts, as before" "$(RC --run $R --sid SF5U --json | jq -r '.rows[0].evidence.stop|[.ts,.promoted]|tostring')" "[\"$OLD\",\"close\"]"
# a notice that is old but has a newer start (the agent resumed): not promoted
R=f5r; mkdir -p "$(RD $R)"
LR --run $R --row 1 --role implementation --sid SF5R >/dev/null 2>&1; start $R "$R#1.1" tu-$R SF5R
PW $R f5r 40
E --run $R --type start --json "{\"attempt\":\"$R#1.1\",\"tool_use_id\":\"tu2-$R\",\"transcript_path\":\"$PJ/SF5R.jsonl\",\"session\":\"SF5R\"}"
ok "waiting-timeout: an old interim notice with a newer start is not promoted" "$(RC --run $R --sid SF5R | grep -c 'waiting-timeout: interim')|$(jq -c 'select(.type=="stop" and .final==true)' "$(MF $R)" | wc -l | tr -d ' ')" "0|0"
# the grace period is config waitingGraceSec (seconds), read like the other keys
R=f5s; mkdir -p "$(RD $R)"; CD=$T/gracecfg; mkdir -p "$CD"; echo '{"waitingGraceSec":120}' > "$CD/config.json"
LR --run $R --row 1 --role implementation --sid SF5S >/dev/null 2>&1; start $R "$R#1.1" tu-$R SF5S
PW $R f5s 5
ok "config waitingGraceSec 120: a 5-minute-old notice is promoted (default 1800 would keep it)" "$(ORCH_DIR=$CD RC --run $R --sid SF5S | grep -c 'older than 2 min with no later notice; promoted (waiting-timeout)')" "1"
ok "config default: waitingGraceSec 1800 in a scratch module load" "$(node -e "console.log(require('$M').cfg.waitingGraceSec)")" "1800"

# fixture 6 (A-19): the trailer commit only on side branch orch/x
R=f6; RP="$T/repo6"; mkdir -p "$(RD $R)"; mkrepo "$RP"
LR --run $R --row 1 --role implementation --git "$RP" >/dev/null 2>&1; start $R "$R#1.1" tu-f6 SF6; fstop $R "$R#1.1" k-f6
git -C "$RP" checkout -q -b orch/x; G "$RP" commit -q --allow-empty -m "feat(y): side" -m "Orch-Attempt: $R#1.1"; SHA6=$(git -C "$RP" rev-parse HEAD); git -C "$RP" checkout -q main
result "$(RD $R)/1-side.md" done "$SHA6" "$R#1.1"
OUT=$(RC --run $R)
has "fixture 6 (A-19): done" "$(rowline "$OUT" 1)" "| done done |"
has "fixture 6: names the side branch" "$(rowline "$OUT" 1)" "commit ${SHA6:0:7}@orch/x | advance"
ok "fixture 6: main does not contain it" "$(RC --run $R --json | jq -c '.rows[0].evidence.commit.branches')" '["orch/x"]'

# item 33: a lettered result name. Row 26 takes 26b-fix.md only when the run has no row 26b of its own; 5a-x.md is row 5a's, never row 5's.
R=f6l; mkdir -p "$(RD $R)"
LR --run $R --row 26 --role implementation >/dev/null 2>&1; start $R "$R#26.1" tu-f6l SF6L; fstop $R "$R#26.1" k-f6l
result "$(RD $R)/26b-fix.md" done none "$R#26.1" "fix round, no commit"
OUT=$(RC --run $R)
has "item 33 (1): row 26 finds 26b-fix.md when the run has no row 26b" "$(rowline "$OUT" 26)" "result done@$(RD $R)/26b-fix.md"
lacks "item 33 (1): no result-attempt-mismatch" "$(rowline "$OUT" 26)" "result-attempt-mismatch"
result "$(RD $R)/26-x.md" done none "$R#26.1" "exact file"
OUT=$(RC --run $R)
has "item 33 (4): an exact 26-x.md still wins over 26b-fix.md" "$(rowline "$OUT" 26)" "result done@$(RD $R)/26-x.md"
R=f6m; mkdir -p "$(RD $R)"
LR --run $R --row 5 --role implementation >/dev/null 2>&1; start $R "$R#5.1" tu-f6m SF6M; fstop $R "$R#5.1" k-f6m
LR --run $R --row 5a --role implementation >/dev/null 2>&1; start $R "$R#5a.1" tu-f6n SF6M; fstop $R "$R#5a.1" k-f6n
result "$(RD $R)/5a-x.md" done none "$R#5a.1" "row 5a"
OUT=$(RC --run $R)
has "item 33 (2): row 5 does not take row 5a's file" "$(rowline "$OUT" 5)" "result none"
has "item 33 (3): row 5a still finds 5a-x.md" "$(rowline "$OUT" 5a)" "| done done | stop finished · result done@$(RD $R)/5a-x.md"
printf '%s\n' '---' 'run: f6p' '---' '' '## Status' '' '| # | Status |' '|---|---|' '| 7 | done |' '| 7b | pending |' > "$(RD f6p)-plan.tmp"; mkdir -p "$(RD f6p)"; mv "$(RD f6p)-plan.tmp" "$(RD f6p)/PLAN.md"
result "$(RD f6p)/7b-x.md" done none
ok "item 33: a plan row 7b (no launch yet) keeps 7b-x.md away from row 7" "$(RC --run f6p --row 7 | grep -c 'result none')" "1"

# item 33 fix round (#4.2): among the safe candidates a file naming the reconciled attempt wins; none naming it: exact name first, then the lettered fallback
R=f6q; mkdir -p "$(RD $R)"
LR --run $R --row 26 --role implementation >/dev/null 2>&1; start $R "$R#26.1" tu-f6q1 SF6Q; fstop $R "$R#26.1" k-f6q1
result "$(RD $R)/26-x.md" done none "$R#26.1" "round 1"
result "$(RD $R)/26b-fix.md" done none "$R#26.2" "fix round"
has "item 33 fix (1): reconciling .1 still finds 26-x.md" "$(rowline "$(RC --run $R)" 26)" "result done@$(RD $R)/26-x.md"
LR --run $R --row 26 --role implementation --new >/dev/null 2>&1; start $R "$R#26.2" tu-f6q2 SF6Q; fstop $R "$R#26.2" k-f6q2
OUT=$(RC --run $R)
has "item 33 fix (1): reconciling .2 finds 26b-fix.md beside a stale 26-x.md" "$(rowline "$OUT" 26)" "26 | $R#26.2 | done done | stop finished · result done@$(RD $R)/26b-fix.md"
lacks "item 33 fix (1): no result-attempt-mismatch" "$(rowline "$OUT" 26)" "result-attempt-mismatch"
R=f6r; mkdir -p "$(RD $R)"
LR --run $R --row 26 --role implementation >/dev/null 2>&1; start $R "$R#26.1" tu-f6r1 SF6R; fstop $R "$R#26.1" k-f6r1
result "$(RD $R)/26-a.md" done none "$R#26.1" "a"; result "$(RD $R)/26-b.md" done none "$R#26.2" "b"
has "item 33 fix (2): two exact files, attempt .1 finds 26-a.md" "$(rowline "$(RC --run $R)" 26)" "result done@$(RD $R)/26-a.md"
LR --run $R --row 26 --role implementation --new >/dev/null 2>&1; start $R "$R#26.2" tu-f6r2 SF6R; fstop $R "$R#26.2" k-f6r2
has "item 33 fix (2): two exact files, attempt .2 finds 26-b.md" "$(rowline "$(RC --run $R)" 26)" "result done@$(RD $R)/26-b.md"
R=f6s; mkdir -p "$(RD $R)"
LR --run $R --row 26 --role implementation >/dev/null 2>&1; start $R "$R#26.1" tu-f6s1 SF6S; fstop $R "$R#26.1" k-f6s1
result "$(RD $R)/26-x.md" done none "$R#26.1" "round 1"; printf '%s\n' '---' 'status: done' 'sha: none' 'sha_reason: fix round' '---' > "$(RD $R)/26b-fix.md"
LR --run $R --row 26 --role implementation --new >/dev/null 2>&1; start $R "$R#26.2" tu-f6s2 SF6S; fstop $R "$R#26.2" k-f6s2
has "item 33 fix (3): none names .2: the exact 26-x.md is used, as in round 1 (other attempt)" "$(rowline "$(RC --run $R)" 26)" "result done@$(RD $R)/26-x.md (other attempt)"
rm "$(RD $R)/26-x.md"
has "item 33 fix (3): no exact file, an attempt-less lettered one: lettered fallback as in round 1" "$(rowline "$(RC --run $R)" 26)" "result done@$(RD $R)/26b-fix.md (other attempt)"
R=f6t; mkdir -p "$(RD $R)"
LR --run $R --row 5 --role implementation >/dev/null 2>&1; start $R "$R#5.1" tu-f6t1 SF6T; fstop $R "$R#5.1" k-f6t1
LR --run $R --row 5a --role implementation >/dev/null 2>&1; start $R "$R#5a.1" tu-f6t2 SF6T; fstop $R "$R#5a.1" k-f6t2
result "$(RD $R)/5a-x.md" done none "$R#5.1" "names a row-5 attempt by mistake"
OUT=$(RC --run $R)
has "item 33 fix (4): row 5 still does not take 5a-x.md, whatever its attempt says" "$(rowline "$OUT" 5)" "result none"
has "item 33 fix (4): row 5a still finds it" "$(rowline "$OUT" 5a)" "result done@$(RD $R)/5a-x.md"

# fixture 7: sha mismatch
R=f7; RP="$T/repo7"; mkdir -p "$(RD $R)"; mkrepo "$RP"
LR --run $R --row 1 --role implementation --git "$RP" >/dev/null 2>&1; start $R "$R#1.1" tu-f7 SF7; fstop $R "$R#1.1" k-f7
G "$RP" commit -q --allow-empty -m "feat(z): z" -m "Orch-Attempt: $R#1.1"
result "$(RD $R)/1-z.md" done deadbeef "$R#1.1"
has "fixture 7: unknown sha-mismatch" "$(rowline "$(RC --run $R)" 1)" "| unknown sha-mismatch |"
has "fixture 7: next ask" "$(rowline "$(RC --run $R)" 1)" "| ask"

# fixture 8: gate (G3): role review, a stop, status gaps_found, sha none with a reason
R=f8; mkdir -p "$(RD $R)"
LR --run $R --row 1 --role review >/dev/null 2>&1; start $R "$R#1.1" tu-f8 SF8; fstop $R "$R#1.1" k-f8
result "$(RD $R)/1-review.md" gaps_found none "$R#1.1" "read-only"
has "fixture 8: done with gaps_found" "$(rowline "$(RC --run $R)" 1)" "| done gaps_found |"
has "fixture 8: next gate, status printed" "$(rowline "$(RC --run $R)" 1)" "| gate (gaps_found)"
ok "fixture 8: --json next gate" "$(RC --run $R --json | jq -c '.rows[0]|[.verdict,.next,.status]')" '["done","gate","gaps_found"]'

# other unknown reasons
R=fx; RP="$T/repox"; mkdir -p "$(RD $R)"; mkrepo "$RP"
LR --run $R --row 1 --role implementation >/dev/null 2>&1; start $R "$R#1.1" tu-x1 SX
E --run $R --type stop --json "{\"attempt\":\"$R#1.1\",\"task_id\":\"kx1\",\"phase\":\"agent-stopped\",\"via\":\"substop\",\"result_bytes\":5}"
result "$(RD $R)/1-a.md" done none "$R#1.1" "r"
LR --run $R --row 2 --role implementation >/dev/null 2>&1; start $R "$R#2.1" tu-x2 SX; fstop $R "$R#2.1" kx2
result "$(RD $R)/2-b.md" done none "$R#9.1" "r"
LR --run $R --row 3 --role implementation >/dev/null 2>&1; start $R "$R#3.1" tu-x3 SX; fstop $R "$R#3.1" kx3
result "$(RD $R)/3-c.md" blocked none "$R#3.1" "r"
LR --run $R --row 4 --role implementation --git "$RP" >/dev/null 2>&1; start $R "$R#4.1" tu-x4 SX; fstop $R "$R#4.1" kx4
result "$(RD $R)/4-d.md" done abc1234 "$R#4.1"
LR --run $R --row 5 --role implementation --git "$RP" >/dev/null 2>&1; start $R "$R#5.1" tu-x5 SX; fstop $R "$R#5.1" kx5
G "$RP" commit -q --allow-empty -m "feat: one" -m "Orch-Attempt: $R#5.1"; G "$RP" commit -q --allow-empty -m "feat: two" -m "Orch-Attempt: $R#5.1"; SHA5=$(git -C "$RP" rev-parse HEAD)
result "$(RD $R)/5-e.md" done "" "$R#5.1"
LR --run $R --row 6 --role implementation >/dev/null 2>&1; LR --run $R --row 6 --role implementation --new >/dev/null 2>&1
start $R "$R#6.2" tu-x6 SX; fstop $R "$R#6.2" kx6; result "$(RD $R)/6-f.md" done none "" "r"
LR --run $R --row 7 --role implementation --git "$T/no-such-repo" >/dev/null 2>&1; start $R "$R#7.1" tu-x7 SX; fstop $R "$R#7.1" kx7
result "$(RD $R)/7-g.md" done none "$R#7.1" "r"
OUT=$(RC --run $R)
has "agent-stopped with no notice stop: notice-pending" "$(rowline "$OUT" 1)" "| unknown notice-pending |"
has "result for another attempt: result-attempt-mismatch" "$(rowline "$OUT" 2)" "| unknown result-attempt-mismatch |"
has "status outside pass and gate: result-status:<s>" "$(rowline "$OUT" 3)" "| unknown result-status:blocked |"
has "a sha with no trailer commit: sha-without-trailer" "$(rowline "$OUT" 4)" "| unknown sha-without-trailer |"
has "two trailer commits: the newest wins, flagged" "$(rowline "$OUT" 5)" "commit ${SHA5:0:7}@main · [multiple-commits] | advance"
has "the row's latest attempt is judged" "$(rowline "$OUT" 6)" "6 | $R#6.2 |"
has "an attempt-less result with two attempts on the row: mismatch" "$(rowline "$OUT" 6)" "| unknown result-attempt-mismatch |"
has "a git error counts as no commit (sha none + reason: done)" "$(rowline "$OUT" 7)" "| done done |"
ok "--row 1,3 limits the rows" "$(RC --run $R --row 1,3 | grep -c "^[0-9]* | ")" "2"
ok "reconcile without --run: exit 2" "$(rc RC)" "2"
ok "reconcile with a bad --row: exit 2" "$(rc RC --run $R --row x!)" "2"
ok "reconcile of a missing run folder: exit 2" "$(rc RC --run nope-run)" "2"

echo "== reconcile step 0: G1 pinned copy (row 4 ledger.js, no ingest), fixture 9 (G6 start rebuild)"
ok "manifest.js never requires ledger.js" "$(grep -cE "require\([^)]*ledger" "$M")" "0"
PIN="$T/pin/lib"; mkdir -p "$PIN"; cp "$M" "$CL/lib/lease-store.js" "$CL/lib/paths.js" "$CL/lib/plan-status.js" "$CL/lib/defaults.json" "$PIN/";   # a plugin-shaped copy: paths.js finds lib/defaults.json next to it
 printf '%s\n' "console.error('unknown command: ' + process.argv[2]); process.exit(2);" > "$PIN/ledger.js"   # a ledger.js without ingest (exit 2, unknown command)
R=f9; RP="$T/repo9"; mkdir -p "$(RD $R)"; mkrepo "$RP"; P9="$ORCH_PROJECTS_DIR/p9"; mkdir -p "$P9/SF9/subagents"
LR --run $R --row 1 --role implementation --git "$RP" --sid SF9 >/dev/null 2>&1
printf '%s\n' "{\"type\":\"queue-operation\",\"operation\":\"enqueue\",\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)\",\"content\":\"<task-notification><task-id>a9</task-id><tool-use-id>tu-f9</tool-use-id><status>completed</status><summary>Agent \\\"[opus/high] $R#1.1 fixture nine\\\" finished</summary><result>FINDINGS fixture</result><usage><subagent_tokens>100</subagent_tokens><tool_uses>2</tool_uses><duration_ms>300</duration_ms></usage></task-notification>\"}" > "$P9/SF9.jsonl"
echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $R#1.1 fixture nine\",\"toolUseId\":\"tu-f9\",\"model\":\"opus\",\"effort\":\"high\",\"finished\":true}" > "$P9/SF9/subagents/agent-a9.meta.json"
echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $R#2.1 other\",\"toolUseId\":\"tu-f9b\",\"finished\":true}" > "$P9/SF9/subagents/agent-a9b.meta.json"
G "$RP" commit -q --allow-empty -m "feat(n): nine" -m "Orch-Attempt: $R#1.1"; SHA9=$(git -C "$RP" rev-parse HEAD)
result "$(RD $R)/1-nine.md" done "${SHA9:0:7}" "$R#1.1"
LR --run $R --row 2 --role implementation >/dev/null 2>&1
OUT=$(node "$PIN/manifest.js" reconcile --run $R --sid SF9); st=$?
ok "G1: the pinned copy's reconcile exits 0" "$st" "0"
has "G1: ingest unavailable printed" "$OUT" "  ingest unavailable"
has "fixture 9: start rebuilt" "$OUT" "$R#1.1: start rebuilt from $P9/SF9/subagents/agent-a9.meta.json"
ok "fixture 9: the rebuilt start event" "$(jq -c 'select(.type=="start")|[.attempt,.via,.tool_use_id,.transcript_path,.session,.agent,.model,.effort]' "$(MF $R)")" "[\"$R#1.1\",\"reconcile\",\"tu-f9\",\"$P9/SF9.jsonl\",\"SF9\",\"orch-builder\",\"opus\",\"high\"]"
has "fixture 9 (row-12 form): unknown no-stop" "$(rowline "$OUT" 1)" "| unknown no-stop |"
has "fixture 9: next ask" "$(rowline "$OUT" 1)" "| ask"
has "a launch with no session: not rebuildable" "$OUT" "$R#2.1: no session on launch; start not rebuildable"
node "$PIN/manifest.js" reconcile --run $R --sid SF9 >/dev/null
ok "a second reconcile rebuilds nothing new" "$(jq -c 'select(.type=="start")' "$(MF $R)" | wc -l | tr -d ' ')" "1"
ok "a continuation is never rebuilt" "$(fstop $R "$R#1.1" k9; LR --run $R --row 1 --role implementation --sid SF9 --continue "$R#1.1" >/dev/null 2>&1; node "$PIN/manifest.js" reconcile --run $R --sid SF9 | grep -c "$R#1.2: ")" "0"
STUB="$T/stub/lib"; mkdir -p "$STUB"; cp "$M" "$CL/lib/lease-store.js" "$CL/lib/paths.js" "$CL/lib/plan-status.js" "$CL/lib/defaults.json" "$STUB/"; printf '%s\n' "process.stderr.write('boom from stub\\n'); process.exit(1);" > "$STUB/ledger.js"
OUT=$(node "$STUB/manifest.js" reconcile --run $R --sid SF9); st=$?
ok "a failing ingest: exit 0" "$st" "0"
has "a failing ingest: ingest failed: <first stderr line>" "$OUT" "ingest failed: boom from stub"

echo "== reconcile and launch --continue: the OD-3 ingest gate (row 4: runIngest passes --sid=<sid or ''> and --run R; B1 no-sid rule)"
# Every target is launched first, so an ungated ingest would write. A WFP_SID of another session in env (where set) proves the child
# takes the caller's sid from the explicit --sid=, never from the inherited env.
OP="$ORCH_PROJECTS_DIR/og"; mkdir -p "$OP"
OLJ(){ echo "$ORCH_RUNS_DIR/$1/ledger.jsonl"; }
osnap(){ echo "$(sum "$(MF $1)") $(sum "$(OLJ $1)")"; }
onote(){ printf '<task-notification><task-id>%s</task-id><tool-use-id>%s</tool-use-id><status>completed</status><summary>Agent "%s" finished</summary><result>R</result><usage><subagent_tokens>10</subagent_tokens><tool_uses>1</tool_uses><duration_ms>10</duration_ms></usage></task-notification>' "$1" "$2" "$3"; }
oenq(){ jq -cn --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" --arg c "$1" '{type:"queue-operation",operation:"enqueue",timestamp:$ts,content:$c}'; }
OL(){ node "$M" launch --role implementation --agent orch-builder --mode subagent --repo r --area a --model opus --effort high "$@" 2>/dev/null | head -1; }
ostart(){ E --run "$1" --type start --json "{\"attempt\":\"$1#1.1\",\"tool_use_id\":\"$2\",\"transcript_path\":\"$3\",\"session\":\"$4\"}"; }
nstop(){ jq -c 'select(.type=="stop")' "$(MF $1)" | wc -l | tr -d ' '; }
swl(){ echo "$1" | grep -c "^  $2$"; }   # an exact sweep line in reconcile's output
SR=zz-og-sid-R-0001; SO=zz-og-sid-O-0002; SF=zz-og-sid-F-0003; SC=zz-og-sid-C-0004
OR=t-og-r; OQ=t-og-q; OR2=t-og-r2; OR3=t-og-r3; OR4=t-og-r4; OR5=t-og-r5; OQ5=t-og-q5; OR6=t-og-r6
for r in $OR $OQ $OR2 $OR3 $OR4 $OR5 $OQ5 $OR6; do mkdir -p "$ORCH_RUNS_DIR/$r"; done
OL --run $OR --row 1 --sid $SR >/dev/null; OL --run $OQ --row 1 --sid $SO >/dev/null; OL --run $OR3 --row 1 --sid $SO >/dev/null; OL --run $OQ5 --row 1 --sid $SO >/dev/null
for r in $OR2 $OR4 $OR5 $OR6; do OL --run $r --row 1 >/dev/null; done
ok "gate setup: SR holds R; SO holds Q, R3 and Q5; R2, R4, R5 and R6 have no lease" "$(jq -r .session "$ORCH_DIR/leases/$OR.json") $(jq -r .session "$ORCH_DIR/leases/$OQ.json" "$ORCH_DIR/leases/$OR3.json" "$ORCH_DIR/leases/$OQ5.json" | tr '\n' ' ')$(ls "$ORCH_DIR/leases/" | grep -cE "^($OR2|$OR4|$OR5|$OR6)\.json$")" "$SR $SO $SO $SO 0"
# (a) reconcile --sid holding R: R's notice is written, Q's (held live by SO) is not
TA="$OP/$SR.jsonl"; { oenq "$(onote oga tu-oga "[opus/high] $OR#1.1 og r")"; oenq "$(onote ogq tu-ogq "[opus/high] $OQ#1.1 og q")"; } > "$TA"
ostart $OR tu-oga "$TA" $SR
sq=$(osnap $OQ); OUT=$(WFP_SID=$SO RC --run $OR --sid $SR); st=$?
ok "reconcile --sid SR holding R (WFP_SID=SO in env): exit 0, R's notice written" "$st $(swl "$OUT" "$OR#1.1: stop new, ledger written")" "0 1"
ok "  Q's notice (Q held live by SO): none:not-held" "$(swl "$OUT" "$OQ#1.1: stop none, ledger none:not-held")" "1"
ok "  Q's manifest.jsonl and ledger.jsonl are byte-identical" "$(osnap $OQ)" "$sq"
ok "  R's stop came from the sweep and R has one ledger line" "$(jq -r 'select(.type=="stop")|.via' "$(MF $OR)") $(wc -l < "$(OLJ $OR)" | tr -d ' ')" "sweep 1"
# (b) the post-/clear case: a fresh sid holding nothing reconciles R2 (lease none); R2's missed notice is written
TB="$OP/zz-og-sid-old-0005.jsonl"; oenq "$(onote ogr2 tu-ogr2 "[opus/high] $OR2#1.1 og r2")" > "$TB"; ostart $OR2 tu-ogr2 "$TB" zz-og-sid-old-0005
ok "  R2's lease is none and the fresh sid holds nothing" "$(node "$M" lease check --run $OR2 --sid $SF | head -1) / $(node "$M" lease check --sid $SF | head -1)" "state: none / state: none"
OUT=$(WFP_SID=$SR RC --run $OR2 --sid $SF); st=$?
ok "reconcile --sid of a fresh session, R2's lease none (WFP_SID=SR in env): exit 0, R2 written via --run" "$st $(swl "$OUT" "$OR2#1.1: stop new, ledger written")" "0 1"
ok "  the reconcile took no lease" "$(test -e "$ORCH_DIR/leases/$OR2.json" && echo present || echo gone)" "gone"
# (c) another sid holds R3 live: the start rebuild (0(a)) still runs, the sweep writes no stop and no ledger line, verdicts print
mkdir -p "$OP/$SO/subagents"; echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $OR3#1.1 og r3\",\"toolUseId\":\"tu-ogr3\",\"model\":\"opus\",\"effort\":\"high\",\"finished\":true}" > "$OP/$SO/subagents/agent-ogr3.meta.json"
oenq "$(onote ogr3 tu-ogr3 "[opus/high] $OR3#1.1 og r3")" > "$OP/$SO.jsonl"
OUT=$(RC --run $OR3 --sid $SR); st=$?
ok "reconcile --sid SR while SO holds R3 live: exit 0, start rebuilt (step 0(a) is not gated)" "$st $(echo "$OUT" | grep -c "^  $OR3#1.1: start rebuilt from $OP/$SO/subagents/agent-ogr3.meta.json$")" "0 1"
ok "  the sweep: none:not-held" "$(swl "$OUT" "$OR3#1.1: stop none, ledger none:not-held")" "1"
ok "  no stop event and no ledger.jsonl in R3" "$(nstop $OR3) $(sum "$(OLJ $OR3)")" "0 absent"
has "  the verdict still prints" "$(rowline "$OUT" 1)" "1 | $OR3#1.1 | unknown "
ok "  control: SO (R3's holder) reconciles and the notice is written" "$(swl "$(RC --run $OR3 --sid $SO)" "$OR3#1.1: stop new, ledger written") $(nstop $OR3)" "1 1"
# (d) launch --continue by a fresh sid on a free run: the ingest writes R4 first, then the acquire
TD="$OP/zz-og-sid-old-0006.jsonl"; oenq "$(onote ogr4 tu-ogr4 "[opus/high] $OR4#1.1 og r4")" > "$TD"; ostart $OR4 tu-ogr4 "$TD" zz-og-sid-old-0006
WFP_SID=$SR node "$M" launch --role implementation --agent orch-builder --mode subagent --repo r --area a --model opus --effort high --run $OR4 --row 1 --continue "$OR4#1.1" --sid $SC > "$T/og4.out" 2> "$T/og4.err"; ec=$?
ok "launch --continue R4#1.1 by fresh sid SC (WFP_SID=SR in env): exit 0, R4#1.2 new" "$ec $(tr '\n' ' ' < "$T/og4.out")" "0 $OR4#1.2 new "
ok "  stderr: the ingest wrote R4's notice" "$(grep -c "^manifest: $OR4#1.1: stop new, ledger written$" "$T/og4.err")" "1"
ok "  the stop (via sweep, final) precedes SC's acquire (by launch), then the launch, then the predecessor's auto first-pass n outcome" "$(jq -r 'if .type=="stop" then "stop:\(.via):\(.final)" elif .type=="lease" then "lease:\(.action):\(.session):\(.by)" else .type end' "$(MF $OR4)" | tr '\n' ' ')" "launch start stop:sweep:true lease:acquire:$SC:launch launch outcome "
ok "  R4's lease is SC's" "$(jq -r .session "$ORCH_DIR/leases/$OR4.json")" "$SC"
# (e) B1: a no-sid reconcile of a transcript holding Q5's ID (Q5 held live by SO) leaves Q5 byte-identical; lease none or expired is written
TE="$OP/zz-og-sid-old-0007.jsonl"; { oenq "$(onote ogr5 tu-ogr5 "[opus/high] $OR5#1.1 og r5")"; oenq "$(onote ogq5 tu-ogq5 "[opus/high] $OQ5#1.1 og q5")"; } > "$TE"; ostart $OR5 tu-ogr5 "$TE" zz-og-sid-old-0007
sq=$(osnap $OQ5); OUT=$(RC --run $OR5); st=$?
ok "B1: no-sid reconcile of R5: exit 0, R5 (lease none) written" "$st $(swl "$OUT" "$OR5#1.1: stop new, ledger written")" "0 1"
ok "B1:   Q5's notice (Q5 held live by SO): none:not-held" "$(swl "$OUT" "$OQ5#1.1: stop none, ledger none:not-held")" "1"
ok "B1:   Q5's manifest.jsonl and ledger.jsonl are byte-identical" "$(osnap $OQ5)" "$sq"
ok "B1: reconcile --sid= (no sid, WFP_SID unset): Q5 still byte-identical" "$(swl "$(RC --run $OR5 --sid=)" "$OQ5#1.1: stop none, ledger none:not-held") $(osnap $OQ5)" "1 $sq"
jq -c '.expires="2000-01-01T00:00:00.000Z"' "$ORCH_DIR/leases/$OQ5.json" > "$T/oq5.json" && mv "$T/oq5.json" "$ORCH_DIR/leases/$OQ5.json"
ok "B1: Q5's lease expired (nobody took it over): the no-sid reconcile writes Q5" "$(swl "$(RC --run $OR5)" "$OQ5#1.1: stop new, ledger written") $(nstop $OQ5)" "1 1"
# runIngest(T) with no sid ignores an inherited WFP_SID: the child gets --sid= (no env fallback in ledger.js)
TG="$OP/zz-og-sid-old-0008.jsonl"; oenq "$(onote ogr6 tu-ogr6 "[opus/high] $OR6#1.1 og r6")" > "$TG"
ok "module runIngest(T) with no sid and WFP_SID=SR in env (SR does not hold R6): ok, R6 written" "$(WFP_SID=$SR node -e "const r=require('$M').runIngest('$TG'); console.log(r.status+'|'+r.lines.join('|'))")" "ok|$OR6#1.1: stop new, ledger written"
ok "module runIngest(T, sid, run) passes --run when the sid's run is free: a fresh sid writes R6's dup" "$(node -e "const r=require('$M').runIngest('$TG','$SF','$OR6'); console.log(r.status+'|'+r.lines.join('|'))")" "ok|$OR6#1.1: stop dup, ledger same"
ok "module runIngest(T, sid, run) with the run held live by another sid: no --run, none:not-held" "$(node -e "const r=require('$M').runIngest('$TA','$SF','$OQ'); console.log(r.status+'|'+r.lines.join('|'))")" "ok|$OR#1.1: stop none, ledger none:not-held|$OQ#1.1: stop none, ledger none:not-held"
ok "help: reconcile's sweep writes only into runs the --sid session holds, plus R" "$(node "$M" help | grep -c 'The sweep writes only into runs the --sid session holds, plus R unless another session holds R live')" "1"

echo "== activity (Stage A: read-only live-driver check): separate scratch ORCH_DIR and projects dir"
AD="$T/orchA"; AP="$T/projA"; AR=act-run; ARD="$AD/runs/$AR"; mkdir -p "$ARD/packs" "$AP/pj"; cp "$REAL_CFG" "$AD/config.json"
AZ(){ ORCH_DIR="$AD" ORCH_RUNS_DIR="$AD/runs" ORCH_PROJECTS_DIR="$AP" node "$M" activity "$@"; }
astate(){ AZ "$@" | head -1; }
atree(){ (cd "$T" && find orchA projA -print0 | sort -z | xargs -0 stat -c '%n %s %Y' ; find orchA projA -type f -print0 | sort -z | xargs -0 md5sum) | md5sum; }
old(){ touch -d '40 min ago' "$@"; }
echo '# r1' > "$ARD/1-scout.md"; echo '# plan' > "$ARD/PLAN.md"; echo '{}' > "$ARD/ledger.jsonl"; echo pack > "$ARD/packs/R1.md"
old "$ARD/1-scout.md" "$ARD/PLAN.md" "$ARD/ledger.jsonl" "$ARD/packs/R1.md"
ok "(b) everything 40 min old: quiet" "$(astate --run $AR --sid ME)" "state: quiet"
has "(b) reason names the window" "$(AZ --run $AR --sid ME)" "reason: nothing within 15m and no live agent within 3m"
has "newest line with path and age" "$(AZ --run $AR --sid ME)" "newest: "
has "handoff: none" "$(AZ --run $AR --sid ME)" "handoff: none"
has "says the lease is not consulted" "$(AZ --run $AR --sid ME)" "lease: not consulted (lease check none does not mean idle)"
touch -d '1 min ago' "$ARD/packs/R1.md"; ok "a fresh packs/ file is not a signal" "$(astate --run $AR --sid ME)" "state: quiet"; old "$ARD/packs/R1.md"
touch -d '5 min ago' "$ARD/1-scout.md"
ok "(a) a result file 5 min old: recent" "$(astate --run $AR --sid ME)" "state: recent"
has "(a) newest is that result file" "$(AZ --run $AR --sid ME | sed -n 3p)" "newest: result $ARD/1-scout.md "
has "(a) its age" "$(AZ --run $AR --sid ME | sed -n 3p)" "(5m ago)"
ok "(f) --minutes 1: the same file is outside the window" "$(astate --run $AR --sid ME --minutes 1)" "state: quiet"
ok "(f) --minutes 10: inside" "$(astate --run $AR --sid ME --minutes 10)" "state: recent"
ok "(f) a bad --minutes: exit 2" "$(rc AZ --run $AR --minutes 0) $(rc AZ --run $AR --minutes x) $(rc AZ --run $AR --minutes -5)" "2 2 2"
old "$ARD/1-scout.md"; touch -d '2 min ago' "$ARD/PLAN.md"; ok "PLAN.md 2 min old: recent" "$(astate --run $AR --sid ME)" "state: recent"; old "$ARD/PLAN.md"
touch -d '2 min ago' "$ARD/ledger.jsonl"; ok "ledger.jsonl 2 min old: recent" "$(astate --run $AR --sid ME)" "state: recent"
echo '# h' > "$ARD/HANDOFF.md"; touch -d '1 min ago' "$ARD/HANDOFF.md"
ok "(c) HANDOFF.md newest, ledger 1 min older: quiet (clean handoff)" "$(astate --run $AR --sid ME)" "state: quiet"
has "(c) reason: clean handoff" "$(AZ --run $AR --sid ME)" "reason: clean handoff: nothing newer than HANDOFF.md + 120s"
has "(c) handoff line has its time" "$(AZ --run $AR --sid ME)" "handoff: 20"
old "$ARD/ledger.jsonl"; touch -d '6 min ago' "$ARD/HANDOFF.md"; touch -d '3 min ago' "$ARD/2-build.md"
ok "(d) a result file 3 min newer than HANDOFF.md: recent" "$(astate --run $AR --sid ME)" "state: recent"
touch -d '5 min ago' "$ARD/2-build.md"; ok "a result file 1 min newer than HANDOFF.md (inside the 120 s slack): quiet" "$(astate --run $AR --sid ME)" "state: quiet"
old "$ARD/2-build.md" "$ARD/HANDOFF.md"; touch -d '1 min ago' "$ARD/HANDOFF-old.md"; ok "HANDOFF*.md is never a signal" "$(astate --run $AR --sid ME)" "state: quiet"; rm "$ARD/HANDOFF-old.md"
printf '%s\n' "{\"id\":\"$AR#e1\",\"seq\":1,\"type\":\"launch\",\"ts\":\"$(ago 40)\",\"run\":\"$AR\",\"attempt\":\"$AR#1.1\",\"row\":\"1\",\"key\":\"k1\",\"via\":\"cli\",\"session\":\"X\"}" \
  "{\"id\":\"$AR#e2\",\"seq\":2,\"type\":\"lease\",\"ts\":\"$(ago 0)\",\"run\":\"$AR\",\"attempt\":null,\"row\":null,\"key\":\"release:x\",\"via\":\"cli\",\"session\":\"X\",\"action\":\"release\"}" > "$ARD/manifest.jsonl"
old "$ARD/HANDOFF.md"; rm "$ARD/HANDOFF.md"
ok "(e) only a lease release event just now: quiet" "$(astate --run $AR --sid ME)" "state: quiet"
has "(e) the event signal is the newest non-lease event" "$(AZ --run $AR --sid ME)" "[launch $AR#e1]"
printf '%s\n' "{\"id\":\"$AR#e3\",\"seq\":3,\"type\":\"stop\",\"ts\":\"$(ago 2)\",\"run\":\"$AR\",\"attempt\":\"$AR#1.1\",\"row\":\"1\",\"key\":\"k3\",\"via\":\"prompt\",\"session\":\"X\"}" >> "$ARD/manifest.jsonl"
touch -d '40 min ago' "$ARD/manifest.jsonl"
ok "a stop event 2 min ago (event ts, not file mtime): recent" "$(astate --run $AR --sid ME)" "state: recent"
has "the event line names it" "$(AZ --run $AR --sid ME)" "event $ARD/manifest.jsonl"
printf '%s\n' "$(head -2 "$ARD/manifest.jsonl")" > "$ARD/manifest.jsonl"; old "$ARD/manifest.jsonl"
ok "back to quiet" "$(astate --run $AR --sid ME)" "state: quiet"
echo "== activity (g): live agents of other sessions"
brief(){ jq -cn --arg c "$1" '{parentUuid:null,isSidechain:true,type:"user",message:{role:"user",content:$c}}'; }
later(){ jq -cn --arg c "$1" '{type:"assistant",message:{role:"assistant",content:[{type:"text",text:$c}]}}'; }
mkdir -p "$AP/pj/OTHER/subagents" "$AP/pj/ME/subagents" "$AP/pj/THIRD/subagents"
A1="$AP/pj/OTHER/subagents/agent-a1.jsonl"; brief "Read the result at ~/.claude/orchestrator/runs/$AR/1-scout.md and build row 2." > "$A1"; touch -d '1 min ago' "$A1"
ok "(g) a brief naming runs/<run>/ with mtime 1 min: recent" "$(astate --run $AR --sid ME)" "state: recent"
has "(g) agent-live strong" "$(AZ --run $AR --sid ME)" "agent-live: strong (1 agent transcript(s) of other sessions touched within 3m)"
has "(g) the agent line names its session and why" "$(AZ --run $AR --sid ME)" "[strong, session OTHER: brief names runs/$AR]"
ok "(g) the caller's own sid: ignored" "$(astate --run $AR --sid OTHER)" "state: quiet"
ok "(j) WFP_SID stands in for --sid" "$(WFP_SID=OTHER AZ --run $AR | head -1)" "state: quiet"
has "(j) no --sid and no WFP_SID: the probe still runs" "$(AZ --run $AR)" "agent-live: strong"
has "(j) and says the caller is not excluded" "$(AZ --run $AR)" "note: no session id, so the caller's own agents are not excluded (pass --sid)"
lacks "(j) with --sid: no such note" "$(AZ --run $AR --sid ME)" "note: no session id"
touch -d '20 min ago' "$A1"; ok "(g) the same agent at 20 min: not counted" "$(astate --run $AR --sid ME)" "state: quiet"
has "(g) agent-live none" "$(AZ --run $AR --sid ME)" "agent-live: none"
A2="$AP/pj/THIRD/subagents/agent-a2.jsonl"; { brief "Look at the dashboard grid."; later "I also read runs/$AR/PLAN.md and Orch-Attempt: $AR#1.1"; } > "$A2"; touch -d '1 min ago' "$A2"
ok "(g) a run mentioned only in a later message: ignored" "$(astate --run $AR --sid ME)" "state: quiet"
A3="$AP/pj/ME/subagents/agent-a3.jsonl"; brief "Orch-Attempt: $AR#2.1
build it" > "$A3"; touch -d '1 min ago' "$A3"
ok "(g) the caller's own agent with an Orch-Attempt brief: ignored" "$(astate --run $AR --sid ME)" "state: quiet"
ok "(g) the same agent seen from another sid: recent" "$(astate --run $AR --sid OTHER)" "state: recent"
has "(g) Orch-Attempt in the brief: strong" "$(AZ --run $AR --sid OTHER)" "[strong, session ME: brief has Orch-Attempt for the run]"
touch -d '20 min ago' "$A3"
A4="$AP/pj/THIRD/subagents/agent-a4.jsonl"; brief "Something about $AR in passing." > "$A4"; touch -d '1 min ago' "$A4"
ok "(g) a bare run name in the brief: recent" "$(astate --run $AR --sid ME)" "state: recent"
has "(g) bare name: weak" "$(AZ --run $AR --sid ME)" "agent-live: weak"
brief "Something about $AR-other in passing." > "$A4"; touch -d '1 min ago' "$A4"
ok "(g) a longer run name that starts with this one: not a match" "$(astate --run $AR --sid ME)" "state: quiet"
brief "no run here" > "$A4"; echo "{\"agentType\":\"orch-builder\",\"description\":\"[opus/high] $AR#3.1 build\",\"toolUseId\":\"tu4\"}" > "$AP/pj/THIRD/subagents/agent-a4.meta.json"; touch -d '1 min ago' "$A4"
ok "(g) a meta.json description with the run's attempt ID: recent" "$(astate --run $AR --sid ME)" "state: recent"
has "(g) meta ID: strong, agent type shown" "$(AZ --run $AR --sid ME)" "[strong, session THIRD, orch-builder: meta description $AR#3.1]"
echo "== activity (i) --json, (h) usage"
J=$(AZ --run $AR --sid ME --json)
ok "(i) --json keys" "$(echo "$J" | jq -c 'keys')" '["agent_minutes","agents_live","handoff","minutes","newest","now","reason","run","sid","sid_excluded","signals","state"]'
ok "(i) --json values" "$(echo "$J" | jq -c '[.run,.state,.minutes,.agent_minutes,.agents_live,.handoff,.sid,.sid_excluded,.newest.kind,.newest.tier,.newest.session,(.signals|length<=6)]')" "[\"$AR\",\"recent\",15,3,\"strong\",null,\"ME\",true,\"agent\",\"strong\",\"THIRD\",true]"
ok "(i) --json signal fields" "$(echo "$J" | jq -c '.signals[] | select(.kind=="ledger") | [(.path|endswith("ledger.jsonl")), (.ts|test("^20")), (.age_min>=39)]')" "[true,true,true]"
ok "(h) unknown run folder: exit 2" "$(rc AZ --run no-such-run --sid ME)" "2"
has "(h) message" "$(AZ --run no-such-run 2>&1)" "activity: run folder $AD/runs/no-such-run not found"
ok "(h) no --run: exit 2; unknown flag: exit 2" "$(rc AZ --sid ME) $(rc AZ --run $AR --bogus 1)" "2 2"
ok "activity exits 0" "$(rc AZ --run $AR --sid ME)" "0"
rm -f "$AP/pj/THIRD/subagents/agent-a4.meta.json"; old "$A4" "$A2"; touch -d '20 min ago' "$A1" "$A3"
TREE1=$(atree)
echo "== activity writes nothing"
for a in "--run $AR --sid ME" "--run $AR" "--run $AR --json" "--run $AR --minutes 1" "--run no-such-run"; do AZ $a >/dev/null 2>&1; done
ok "2. no file, mtime or listing under the scratch dirs changed by activity" "$(atree)" "$TREE1"
ok "2. the run's manifest.jsonl unchanged (md5)" "$(md5sum < "$ARD/manifest.jsonl")" "$(printf '%s\n' "$(head -2 "$ARD/manifest.jsonl")" | md5sum)"
ok "2. no errors.log in the activity scratch dir" "$([ -e "$AD/errors.log" ] && echo yes || echo no)" "no"

echo "== resumable (row 5, OD-1 A, truth 6): read-only candidate list; separate scratch ORCH_DIR and projects dir"
RD="$T/orchR"; RP="$T/projR"; mkdir -p "$RD/runs" "$RD/leases" "$RP/pj"; cp "$REAL_CFG" "$RD/config.json"
RZ(){ ORCH_DIR="$RD" ORCH_RUNS_DIR="$RD/runs" ORCH_PROJECTS_DIR="$RP" node "$M" resumable "$@"; }
rtree(){ (cd "$T" && find orchR projR orchR2 -print0 2>/dev/null | sort -z | xargs -0 stat -c '%n %s %Y' ; find orchR projR orchR2 -type f -print0 2>/dev/null | sort -z | xargs -0 md5sum) | md5sum; }
at(){ touch -d "$1" "${@:2}"; }
rlease(){ printf '{"v":1,"run":"%s","path":"%s","session":"%s","acquired":"%s","renewed":"%s","expires":"%s","ttl_sec":7200}\n' "$1" "$RD/runs/$1" "$2" "$(ago 60)" "$(ago 60)" "$3" > "$RD/leases/$1.json"; }
plan(){ printf -- '---\nrun: %s\npreset: %s\n---\n# plan\n\n## Status\n\nPlan check: passed, round 1 · Approved: yes\n\n| # | Status | Ran on | Attempt |\n|---|---|---|---|\n' "$1" "$2"; shift 2; for s in "$@"; do printf '| 1 | %s | | |\n' "$s"; done; printf '\n## Resume\n\nAfter /clear: resume.\n'; }
RSO=OTHER-sid-1234; RUNTIL=$(date -u -d '+60 min' +%Y-%m-%dT%H:%M:%S.000Z); RHH=$(date -u -d "$RUNTIL" +%H:%M)
mkdir -p "$RD/runs/"{rsx-a,rsx-b,rsx-c,rsx-d,rsx-e,rsx-f}; echo stray > "$RD/runs/not-a-dir.md"
# A: clean HANDOFF, no lease (HANDOFF's preset wins over PLAN's)
plan rsx-a balanced pending pending > "$RD/runs/rsx-a/PLAN.md"; printf -- '---\nrun: rsx-a\npreset: quality\n---\n# handoff\n' > "$RD/runs/rsx-a/HANDOFF.md"
T40=$(ago 40); T30=$(ago 30); T20=$(ago 20); T50=$(ago 50); T10=$(ago 10)
at "$T40" "$RD/runs/rsx-a/PLAN.md"; at "$T30" "$RD/runs/rsx-a/HANDOFF.md"
# B: HANDOFF, another sid's live lease
plan rsx-b quality done running > "$RD/runs/rsx-b/PLAN.md"; echo '# handoff' > "$RD/runs/rsx-b/HANDOFF.md"; at "$T40" "$RD/runs/rsx-b/PLAN.md"; at "$T20" "$RD/runs/rsx-b/HANDOFF.md"; rlease rsx-b $RSO "$RUNTIL"
# C: PLAN-only, a result file from 1 minute ago
plan rsx-c economy running > "$RD/runs/rsx-c/PLAN.md"; at "$T40" "$RD/runs/rsx-c/PLAN.md"; echo '# r' > "$RD/runs/rsx-c/1-scout.md"; at "$(ago 1)" "$RD/runs/rsx-c/1-scout.md"
# D: PLAN-only (no Status section: not done), quiet, own lease
printf -- '---\nrun: rsx-d\npreset: max\n---\n# plan, no status yet\n' > "$RD/runs/rsx-d/PLAN.md"; at "$T50" "$RD/runs/rsx-d/PLAN.md"; rlease rsx-d ME "$RUNTIL"
# E: HANDOFF (the newest), every Status row done
plan rsx-e quality done "done (abc1234)" done > "$RD/runs/rsx-e/PLAN.md"; echo '# stale handoff' > "$RD/runs/rsx-e/HANDOFF.md"; at "$T40" "$RD/runs/rsx-e/PLAN.md"; at "$T10" "$RD/runs/rsx-e/HANDOFF.md"
# F: neither HANDOFF.md nor PLAN.md: not a candidate
echo x > "$RD/runs/rsx-f/notes.md"
WANT_R="resumable: rsx-a  handoff $T30 (30m)  preset quality  lease none
resumable: rsx-d  plan $T50 (50m)  preset max  lease mine
excluded: rsx-b  lease other: holder OTHER-si until $RHH UTC, activity quiet
excluded: rsx-c  activity recent: result 1m
excluded: rsx-e  done
candidates: 2 of 5 checked"
R0=$(RZ --sid ME)
ok "A resumable, D resumable (lease mine), B lease other + activity quiet, C recent, E done last; F not a candidate" "$R0" "$WANT_R"
ok "resumable exits 0" "$(rc RZ --sid ME)" "0"
ok "WFP_SID stands in for --sid" "$(WFP_SID=ME RZ)" "$WANT_R"
ok "--limit 2: E (done, not counted), B, A; stops before C and D" "$(RZ --sid ME --limit 2)" "resumable: rsx-a  handoff $T30 (30m)  preset quality  lease none
excluded: rsx-b  lease other: holder OTHER-si until $RHH UTC, activity quiet
excluded: rsx-e  done
candidates: 1 of 3 checked"
ok "--limit 1: E and B only" "$(RZ --sid ME --limit 1 | tail -1)" "candidates: 0 of 2 checked"
ok "--limit 50: all 5 checked" "$(RZ --sid ME --limit 50 | tail -1)" "candidates: 2 of 5 checked"
printf '%s\n' "$RD/runs/rsx-b" > "$RD/ACTIVE"; ok "ACTIVE naming B changes nothing (full output)" "$(RZ --sid ME)" "$R0"
printf 'rsx-e\n' > "$RD/ACTIVE"; ok "ACTIVE naming done run E changes nothing" "$(RZ --sid ME --limit 2)" "$(rm -f "$RD/ACTIVE"; RZ --sid ME --limit 2)"
RJ=$(RZ --sid ME --json)
ok "--json keys" "$(echo "$RJ" | jq -c 'keys')" '["candidates","checked","excluded"]'
ok "--json candidates" "$(echo "$RJ" | jq -c '[.candidates[] | [.run,.kind,.mtime,.preset,.lease,.activity]]')" "[[\"rsx-a\",\"handoff\",\"$T30\",\"quality\",\"none\",\"quiet\"],[\"rsx-d\",\"plan\",\"$T50\",\"max\",\"mine\",\"quiet\"]]"
ok "--json excluded (done last) and checked" "$(echo "$RJ" | jq -c '[[.excluded[] | [.run,.reason]], .checked, .excluded[0].holder, .excluded[0].until, .excluded[0].activity, .excluded[1].newest.kind]')" "[[[\"rsx-b\",\"lease-other\"],[\"rsx-c\",\"activity-recent\"],[\"rsx-e\",\"done\"]],5,\"$RSO\",\"$RUNTIL\",\"quiet\",\"result\"]"
echo "-- live agents: the caller's own are excluded with --sid; a lease-other run reports its activity (W2)"
mkdir -p "$RP/pj/ME/subagents" "$RP/pj/$RSO/subagents"
RA1="$RP/pj/ME/subagents/agent-r1.jsonl"; brief "Orch-Attempt: rsx-a#2.1
read runs/rsx-a/PLAN.md" > "$RA1"; at "$(ago 1)" "$RA1"
ok "the caller's own live agent naming A: A still resumable with --sid ME" "$(RZ --sid ME | head -1)" "resumable: rsx-a  handoff $T30 (30m)  preset quality  lease none"
has "the same agent seen from another sid: A excluded, recent agent" "$(RZ --sid ZZ)" "excluded: rsx-a  activity recent: agent 1m"
has "  and D (held by ME) reads lease other from that sid" "$(RZ --sid ZZ)" "excluded: rsx-d  lease other: holder ME until $RHH UTC, activity quiet"
RA2="$RP/pj/$RSO/subagents/agent-r2.jsonl"; brief "build runs/rsx-b/PLAN.md row 3" > "$RA2"; at "$(ago 1)" "$RA2"
ok "B's holder has a live agent: still lease other (it wins), activity recent" "$(RZ --sid ME | /usr/bin/grep rsx-b)" "excluded: rsx-b  lease other: holder OTHER-si until $RHH UTC, activity recent"
at "$(ago 30)" "$RA1" "$RA2"
has "no session id: a note, and D's live lease reads other" "$(RZ)" "note: no session id, so every live lease reads as other"
has "  (D excluded without a sid)" "$(RZ)" "excluded: rsx-d  lease other: holder ME until"
lacks "with --sid: no note" "$(RZ --sid ME)" "note: no session id"
rlease rsx-a $RSO "$(ago 5)"; ok "an expired lease on A: resumable, lease expired" "$(RZ --sid ME | head -1)" "resumable: rsx-a  handoff $T30 (30m)  preset quality  lease expired"; rm -f "$RD/leases/rsx-a.json"
echo "-- done (W1): the Status table's Status column"
R2="$T/orchR2"; mkdir -p "$R2/runs/"{g1,g2,g3,g4,g5,g6}
RZ2(){ ORCH_DIR="$R2" ORCH_RUNS_DIR="$R2/runs" ORCH_PROJECTS_DIR="$RP" node "$M" resumable --sid ME --limit 50 "$@"; }
plan g1 x > "$R2/runs/g1/PLAN.md"                                  # header and separator only: no rows
plan g2 x done blocked > "$R2/runs/g2/PLAN.md"
plan g3 x done "Done, sha abc" passed > "$R2/runs/g3/PLAN.md"     # no pending/running/blocked/orphaned
plan g4 x done orphaned > "$R2/runs/g4/PLAN.md"
printf '# p\n\n## Status\n\nno table yet\n\n## Resume\n| x | Status |\n|---|---|\n| 1 | done |\n' > "$R2/runs/g5/PLAN.md"   # a table after the next ## is not the Status table
printf '# p\n\n## Status\n\n| # | State |\n|---|---|\n| 1 | done |\n' > "$R2/runs/g6/PLAN.md"                              # no Status column
for i in 1 2 3 4 5 6; do at "$(ago $((60+i)))" "$R2/runs/g$i/PLAN.md"; done
RG=$(RZ2 --json)
ok "done only for g3; header-only, done+blocked, orphaned, table outside the section, no Status column: not done" "$(echo "$RG" | jq -c '[[.candidates[].run], [.excluded[] | [.run,.reason]], .checked]')" '[["g1","g2","g4","g5","g6"],[["g3","done"]],6]'
ok "plan-only preset from PLAN.md, '?' without one" "$(RZ2 | /usr/bin/grep -E '^resumable: (g1|g5) ' | sed -E 's/.*preset ([^ ]+).*/\1/' | tr '\n' ' ')" "x ? "
echo "-- usage and empty"
ok "usage errors exit 2: --limit 0, 51, x, 2.5, --json=x, unknown flag, positional, --minutes 0, --run" "$(for a in '--limit 0' '--limit 51' '--limit x' '--limit 2.5' '--json=x' '--bogus 1' 'foo' '--minutes 0' '--run rsx-a'; do printf '%s ' "$(rc RZ $a)"; done)" "2 2 2 2 2 2 2 2 2 "
has "--limit message" "$(RZ --limit 0 2>&1)" "--limit '0': a whole number of runs from 1 to 50"
ok "no runs folder: exit 0, 0 of 0" "$(ORCH_DIR="$T/orchR-none" ORCH_RUNS_DIR="$T/orchR-none/runs" ORCH_PROJECTS_DIR="$RP" node "$M" resumable --sid ME; echo "rc=$?")" "candidates: 0 of 0 checked
rc=0"
ok "  and it created nothing" "$([ -e "$T/orchR-none" ] && echo made || echo none)" "none"
has "HELP lists resumable" "$(node "$M" help)" "resumable [--sid S] [--limit N (1-50, default 6)] [--minutes 15] [--json]"
has "unknown command lists resumable" "$(node "$M" bogus 2>&1)" "(launch | void | event | show | lease | reconcile | activity | resumable | help)"
echo "-- resumable writes nothing (truth 6)"
printf '%s\n' "{\"id\":\"rsx-b#e1\",\"seq\":1,\"type\":\"lease\",\"ts\":\"$(ago 40)\",\"run\":\"rsx-b\",\"attempt\":null,\"row\":null,\"key\":\"acquire:x\",\"via\":\"cli\",\"session\":\"$RSO\",\"action\":\"acquire\"}" > "$RD/runs/rsx-b/manifest.jsonl"; at "$T40" "$RD/runs/rsx-b/manifest.jsonl"
printf '%s\n' "$RD/runs/rsx-b" > "$RD/ACTIVE"; at "$T40" "$RD/ACTIVE"
RTREE=$(rtree); RMSUM=$(md5sum < "$RD/runs/rsx-b/manifest.jsonl")
for a in "--sid ME" "" "--json --sid ME" "--sid ZZ --limit 2" "--sid ME --minutes 1" "--limit 0" "--sid ME --limit 50"; do RZ $a >/dev/null 2>&1; RZ2 >/dev/null 2>&1; done
ok "tree md5 (names, sizes, mtimes, contents) of orchR, projR, orchR2 unchanged" "$(rtree)" "$RTREE"
ok "B's manifest.jsonl byte-identical; no manifest.jsonl created elsewhere" "$(md5sum < "$RD/runs/rsx-b/manifest.jsonl") $(find "$RD/runs" "$R2/runs" -name manifest.jsonl | wc -l | tr -d ' ')" "$RMSUM 1"
ok "leases/ holds only the B and D fixtures; no errors.log; no lock dirs" "$(ls "$RD/leases" | tr '\n' ' ')$([ -e "$RD/errors.log" ] && echo errlog)$(find "$RD" "$R2" -name '*.lock' | wc -l | tr -d ' ')" "rsx-b.json rsx-d.json 0"

echo "== runs (row 3, truth 5): live leases, resumable's lines byte-identical, finished, other; reuses the resumable fixtures"
RN(){ ORCH_DIR="$RD" ORCH_RUNS_DIR="$RD/runs" ORCH_PROJECTS_DIR="$RP" node "$M" runs "$@"; }
rm -f "$RD/ACTIVE"; rlease rsx-c ZZ "$(ago 5)"; rlease rsx-gone $RSO "$RUNTIL"   # C: an expired lease (not live); a live lease on a missing folder
RNA=$(RN --sid ME)
ok "full output: live (other, this session, missing folder), resumable's lines, finished E, other F" "$RNA" "live: rsx-b  holder OTHER-si  until $RHH UTC  activity quiet
live: rsx-d  holder ME (this session)  until $RHH UTC  activity quiet
live: rsx-gone  holder OTHER-si  until $RHH UTC  activity no-folder
$(RZ --sid ME)
finished: rsx-e  $T10
other: 1 folders without PLAN.md or HANDOFF.md: rsx-f"
ok "runs exits 0" "$(rc RN --sid ME)" "0"
mid(){ /usr/bin/grep -vE '^(live|finished|other): '; }
ok "middle section byte-identical to resumable: --sid ME, no sid, --limit 2, --minutes 1" "$(for a in '--sid ME' '' '--sid ME --limit 2' '--sid ME --minutes 1'; do [ "$(RN $a | mid)" = "$(RZ $a)" ] && printf 'same ' || printf 'DIFF '; done)" "same same same same "
lacks "no sid: no '(this session)'" "$(RN)" "(this session)"
RNJ=$(RN --sid ME --json)
ok "--json keys; resumable equals resumable --json; live runs; finished; other" "$(echo "$RNJ" | jq -c '[keys, [.live[] | [.run,.mine,.activity]], [.finished[].run], .finished_total, .other]') $([ "$(echo "$RNJ" | jq -cS .resumable)" = "$(RZ --sid ME --json | jq -cS .)" ] && echo same)" '[["finished","finished_total","live","other","resumable"],[["rsx-b",false,"quiet"],["rsx-d",true,"quiet"],["rsx-gone",false,"no-folder"]],["rsx-e"],1,{"count":1,"names":["rsx-f"]}] same'
ok "usage errors exit 2: --limit 0, --run, unknown flag, positional, --minutes 0" "$(for a in '--limit 0' '--run rsx-a' '--bogus 1' 'foo' '--minutes 0'; do printf '%s ' "$(rc RN $a)"; done)" "2 2 2 2 2 "
has "HELP lists runs" "$(node "$M" help)" "runs [--sid S] [--limit N (1-50, default 6)] [--minutes 15] [--json]"
has "unknown command lists runs" "$(node "$M" bogus 2>&1)" "read-only: status | runs"
echo "-- runs writes nothing (truth 5)"
RTREE2=$(rtree)
for a in "--sid ME" "" "--json --sid ME" "--sid ZZ --limit 2" "--limit 0"; do RN $a >/dev/null 2>&1; done
ok "tree md5 of orchR, projR, orchR2 unchanged; no lock dirs, no errors.log" "$(rtree) $(find "$RD" -name '*.lock' | wc -l | tr -d ' ')$([ -e "$RD/errors.log" ] && echo errlog)" "$RTREE2 0"
echo "-- a done plan with an attempt that never stopped (old data): still finished, the open count shown"
ORCH_DIR="$RD" ORCH_RUNS_DIR="$RD/runs" ORCH_PROJECTS_DIR="$RP" node "$M" launch --role implementation --agent orch-builder --mode subagent --repo r --area a --run rsx-e --row 1 --model opus --effort high >/dev/null
ok "E stays finished, with its open attempt counted" "$(RN --sid ME | /usr/bin/grep '^finished: ')" "finished: rsx-e  $T10  (1 open attempt(s) with no stop event)"

echo "== status (row 2, truth 4): read-only per-row view and run list; separate scratch ORCH_DIR"
SD="$T/orchS"; mkdir -p "$SD/runs/"{stx-a,stx-b,stx-c} "$SD/leases"; cp "$REAL_CFG" "$SD/config.json"
SX(){ ORCH_DIR="$SD" ORCH_RUNS_DIR="$SD/runs" ORCH_PROJECTS_DIR="$T/projS" node "$M" "$@"; }
SL(){ SX launch --role implementation --agent orch-builder --mode subagent --repo r --area a "$@" | head -1; }
SE(){ SX event "$@" >/dev/null; }
sq(){ tr -s ' '; }
ok "stx-a#1.1 launched" "$(SL --run stx-a --row 1 --model opus --effort high --subtask 'Build the thing')" "stx-a#1.1"
SE --run stx-a --type start --json '{"attempt":"stx-a#1.1","tool_use_id":"s1"}'; SE --run stx-a --type stop --json '{"attempt":"stx-a#1.1","task_id":"k1","phase":"finished"}'
SL --run stx-a --row 2 --model sonnet --effort high --subtask 'Second step' >/dev/null
SE --run stx-a --type start --json '{"attempt":"stx-a#2.1","tool_use_id":"s2"}'; SE --run stx-a --type stop --json '{"attempt":"stx-a#2.1","task_id":"k2","phase":"failed"}'
ok "retry stx-a#2.2 launched" "$(SL --run stx-a --row 2 --model opus --effort xhigh)" "stx-a#2.2"
SE --run stx-a --type start --json '{"attempt":"stx-a#2.2","tool_use_id":"s3"}'
printf -- '---\nrun: stx-a\npreset: quality\n---\n# plan\n\n## Status\n\n| # | Status | SHA |\n|---|---|---|\n| 1 | done (abc1234) | |\n| 2 | running | |\n| 3 | pending | |\n' > "$SD/runs/stx-a/PLAN.md"
printf -- '---\nrow: 1\nattempt: stx-a#1.1\nsha: abc1234def0\n---\n' > "$SD/runs/stx-a/1-build.md"
printf '%s\n' '{"id":"stx-a#1.1","run":"stx-a","row":1,"model":"opus","effort":"high","tokens":1500,"ms":120000}' '{"id":"stx-a#2.1","run":"stx-a","row":2,"model":"sonnet","effort":"high","tokens":null,"ms":60000}' > "$SD/runs/stx-a/ledger.jsonl"
SUNTIL=$(date -u -d '+60 min' +%Y-%m-%dT%H:%M:%S.000Z); SHH=$(date -u -d "$SUNTIL" +%H:%M)
printf '{"v":1,"run":"stx-a","path":"%s","session":"OTHER-stx-sid","acquired":"%s","renewed":"%s","expires":"%s","ttl_sec":7200}\n' "$SD/runs/stx-a" "$(ago 5)" "$(ago 5)" "$SUNTIL" > "$SD/leases/stx-a.json"
printf -- '---\nrun: stx-b\npreset: economy\n---\n## Status\n\n| # | Status |\n|---|---|\n| 1 | done |\n| 2 | passed |\n' > "$SD/runs/stx-b/PLAN.md"
printf '%s\n' '{"id":"stx-b#1.1","run":"stx-b","row":1,"tokens":2000000,"ms":1000}' > "$SD/runs/stx-b/ledger.jsonl"
touch -d "$(ago 1)" "$SD/runs/stx-b/PLAN.md"
SA=$(SX status --run stx-a --sid ME)
ok "--run: header, rows 1-3 (plan cell wins; retry; sha from the result; '?' tokens; plan-only row), lease line" "$(echo "$SA" | sq)" "status: run stx-a preset quality plan yes rows 1/3
row subtask status attempts latest model/effort sha tokens time
1 Build the thing done (abc1234) 1 stx-a#1.1 opus/high abc1234 1.5k 2.0m
2 Second step running 2 stx-a#2.2 opus/xhigh - ? 1.0m
3 - pending 0 - - - - -
lease: other holder OTHER-st run stx-a until $SHH UTC"
ok "status exits 0" "$(rc SX status --run stx-a)" "0"
SJ=$(SX status --run stx-a --sid ME --json)
ok "--json: keys, counts, row 2 latest/attempts/unknown tokens, row 1 full sha, lease state" "$(echo "$SJ" | jq -c '[keys, .rows_done, .rows_total, .open, .rows[1].latest, .rows[1].attempts, .rows[1].tokens, .rows[0].sha, .lease.state]')" '[["lease","ms","open","plan","preset","rows","rows_done","rows_total","run","tokens"],1,3,1,"stx-a#2.2",2,{"n":1,"sum":0,"unknown":1},"abc1234def0","other"]'
ok "run list: not-done stx-a before the empty stx-c, done stx-b last" "$(SX status --sid ME | sq)" "stx-a rows 1/3 open 1 tokens 1.5k? lease other OTHER-st
stx-c rows 0/0 open 0 tokens - lease none
stx-b rows 2/2 open 0 tokens 2.0M lease none"
ok "run list --limit 1 and --json" "$(SX status --sid ME --limit 1 | sq) $(SX status --json | jq -c '[.total, [.runs[].run], .runs[0].rows_done]')" "stx-a rows 1/3 open 1 tokens 1.5k? lease other OTHER-st
(2 more; --limit up to 50) [3,[\"stx-a\",\"stx-c\",\"stx-b\"],1]"
ok "usage errors exit 2: --limit 0, --limit 51, unknown flag, --run with --limit, missing run folder, bad run name" "$(for a in '--limit 0' '--limit 51' '--bogus 1' '--run stx-a --limit 2' '--run stx-zz' '--run a/b'; do printf '%s ' "$(rc SX status $a)"; done)" "2 2 2 2 2 2 "
has "HELP lists status" "$(node "$M" help)" "status [--run R] [--limit N (1-50, default 20)] [--sid S] [--json]"
has "unknown command lists status" "$(node "$M" bogus 2>&1)" "read-only: status"
echo "-- status writes nothing (truth 4)"
stree(){ (cd "$T" && find orchS -print0 | sort -z | xargs -0 stat -c '%n %s %Y'; find orchS -type f -print0 | sort -z | xargs -0 md5sum) | md5sum; }
STREE=$(stree)
for a in "--run stx-a --sid ME" "--run stx-a" "--run stx-a --json" "--run stx-b --sid OTHER-stx-sid" "" "--json" "--limit 1" "--limit 0" "--run stx-zz"; do SX status $a >/dev/null 2>&1; done
ok "tree md5 (names, sizes, mtimes, contents; leases/ included) of orchS unchanged" "$(stree)" "$STREE"
ok "no lock dirs, no errors.log, no projects dir" "$(find "$SD" -name '*.lock' | wc -l | tr -d ' ')$([ -e "$SD/errors.log" ] && echo errlog)$([ -e "$T/projS" ] && echo proj)" "0"

echo "-- status and runs agree on done: closed/skipped/dropped plan cells count as done"
SD2="$T/orchS2"; mkdir -p "$SD2/runs/stx-d" "$SD2/leases"; cp "$REAL_CFG" "$SD2/config.json"
printf -- '---\nrun: stx-d\n---\n## Status\n\n| # | Status |\n|---|---|\n| 1 | done |\n| 2 | closed |\n| 3 | skipped |\n| 4 | dropped |\n| 5 | running |\n' > "$SD2/runs/stx-d/PLAN.md"
ok "status: closed/skipped/dropped are done, running is not (4/5)" "$(ORCH_DIR="$SD2" ORCH_RUNS_DIR="$SD2/runs" ORCH_PROJECTS_DIR="$T/projS2" node "$M" status --run stx-d --json | jq -r '"\(.rows_done)/\(.rows_total)"')" "4/5"
printf -- '---\nrun: stx-d\n---\n## Status\n\n| # | Status |\n|---|---|\n| 1 | done |\n| 2 | closed |\n| 3 | skipped |\n| 4 | dropped |\n' > "$SD2/runs/stx-d/PLAN.md"
SD2O=$(ORCH_DIR="$SD2" ORCH_RUNS_DIR="$SD2/runs" ORCH_PROJECTS_DIR="$T/projS2" node "$M" status --run stx-d | head -1 | /usr/bin/grep -o 'rows [0-9]*/[0-9]*')
SD2R=$(ORCH_DIR="$SD2" ORCH_RUNS_DIR="$SD2/runs" ORCH_PROJECTS_DIR="$T/projS2" node "$M" runs | /usr/bin/grep -c '^finished: stx-d')
ok "status shows rows 4/4 and runs lists the same run as finished" "$SD2O $SD2R" "rows 4/4 1"

echo "-- attempt-derived status: a closed attempt counts as done, a running one does not"
SD3="$T/orchS3"; mkdir -p "$SD3/runs/stx-e" "$SD3/leases"; cp "$REAL_CFG" "$SD3/config.json"
SX3(){ ORCH_DIR="$SD3" ORCH_RUNS_DIR="$SD3/runs" ORCH_PROJECTS_DIR="$T/projS3" node "$M" "$@"; }
SX3 launch --role implementation --agent orch-builder --mode subagent --repo r --area a --run stx-e --row 1 --model opus --effort high >/dev/null
SX3 event --run stx-e --type start --json '{"attempt":"stx-e#1.1","tool_use_id":"e1"}' >/dev/null; SX3 event --run stx-e --type stop --json '{"attempt":"stx-e#1.1","task_id":"ke1","phase":"closed"}' >/dev/null
ok "status: a row whose attempt is closed counts as done (1/1, no PLAN.md)" "$(SX3 status --run stx-e --json | jq -r '"\(.rows_done)/\(.rows_total) \(.rows[0].status) \(.rows[0].status_from)"')" "1/1 closed attempt"
SX3 launch --role implementation --agent orch-builder --mode subagent --repo r --area a --run stx-e --row 2 --model opus --effort high >/dev/null
SX3 event --run stx-e --type start --json '{"attempt":"stx-e#2.1","tool_use_id":"e2"}' >/dev/null
ok "status: a row whose attempt is started (open) is not done (1/2)" "$(SX3 status --run stx-e --json | jq -r '"\(.rows_done)/\(.rows_total) \(.rows[1].status) \(.rows[1].done)"')" "1/2 started false"

echo "== lease-store.js (per-run lease: D-1 layout, D-2 legacy read side, D-11 tracked)"
# Fixtures live in $T/lstore (not under $ORCH_DIR) and use invented sids in the live ACTIVE.lease shape; no dir named runs.
LS="$CL/lib/lease-store.js"; LD="$T/lstore"; mkdir -p "$LD"
SA=lsfix-sid-alpha-0001; SB=lsfix-sid-beta-0002; RA=lsfix-run-a; RB=lsfix-run-b
LQ(){ node -e "const s=require(process.argv[1]), D=process.argv[2]; const r=($2); console.log(typeof r==='string'?r:JSON.stringify(r))" "$LS" "$1"; }
inm(){ date -u -d "$1 min" +%Y-%m-%dT%H:%M:%S.000Z; }
# lfix <file> <run> <sid> [renewed: minutes ago] [expires: minutes from now]   (the live ACTIVE.lease shape: v,run,path,session,acquired,renewed,expires,ttl_sec)
lfix(){ mkdir -p "$(dirname "$1")"; printf '{"v":1,"run":"%s","path":"/x/orchestrator/%s","session":"%s","acquired":"%s","renewed":"%s","expires":"%s","ttl_sec":7200}\n' "$2" "$2" "$3" "$(ago 120)" "$(ago "${4:-10}")" "$(inm "${5:-110}")" > "$1"; }
LL(){ LQ "$1" "s.listLeases(D).map(x=>x.run+':'+(x.legacy?'L':'P')+':'+x.lease.session.slice(10,14)).join(',')"; }
# d1 legacy only, no leases/ dir | d2 per-run beats legacy | d3 malformed / name!=run per-run file plus legacy | d3e EISDIR plus legacy
lfix "$LD/d1/ACTIVE.lease" $RA $SA
lfix "$LD/d2/ACTIVE.lease" $RA $SA; lfix "$LD/d2/leases/$RA.json" $RA $SB
lfix "$LD/d3/ACTIVE.lease" $RA $SA; mkdir -p "$LD/d3/leases"; echo '{nope' > "$LD/d3/leases/$RA.json"
lfix "$LD/d3n/ACTIVE.lease" $RA $SA; lfix "$LD/d3n/leases/$RA.json" $RB $SA
lfix "$LD/d3z/ACTIVE.lease" $RA $SA; mkdir -p "$LD/d3z/leases"; : > "$LD/d3z/leases/$RA.json"
lfix "$LD/d3e/ACTIVE.lease" $RA $SA; mkdir -p "$LD/d3e/leases/$RA.json"
# d4 ignored names and contents
lfix "$LD/d4/leases/$RA.json" $RA $SA
lfix "$LD/d4/leases/$RB.json.tmp-1" $RB $SA; mkdir -p "$LD/d4/leases/$RB.json.lock"
lfix "$LD/d4/leases/lsfix-run-c.json" lsfix-run-x $SA; lfix "$LD/d4/leases/.json" "" $SA; lfix "$LD/d4/leases/bad..run.json" bad..run $SA
lfix "$LD/d4/leases/lsfix-run-e.json" lsfix-run-e "lsfix bad sid 0003"; echo '[1]' > "$LD/d4/leases/lsfix-run-f.json"; lfix "$LD/d4/leases/lsfix-run-g.json" lsfix-run-g "../x"
lfix "$LD/d4/leases/lsfix-run-h.txt" lsfix-run-h $SA
# d5 empty | d6 leases is a file | d7 leases is a file, plus legacy | d8 legacy malformed | d9 legacy is an array
mkdir -p "$LD/d5"; mkdir -p "$LD/d6"; echo x > "$LD/d6/leases"; mkdir -p "$LD/d7"; echo x > "$LD/d7/leases"; lfix "$LD/d7/ACTIVE.lease" $RA $SA
mkdir -p "$LD/d8"; echo '{"v":1,"run":"lsfix-run-a"' > "$LD/d8/ACTIVE.lease"; mkdir -p "$LD/d9"; echo '[]' > "$LD/d9/ACTIVE.lease"
# d10 chmod 000 per-run file (legacy for the same run must not show through) | d11 chmod 000 leases/ dir
lfix "$LD/d10/ACTIVE.lease" $RA $SA; lfix "$LD/d10/leases/$RA.json" $RA $SB; lfix "$LD/d10/leases/$RB.json" $RB $SB; chmod 000 "$LD/d10/leases/$RA.json"
lfix "$LD/d11/ACTIVE.lease" $RA $SA; lfix "$LD/d11/leases/$RB.json" $RB $SB; chmod 000 "$LD/d11/leases"
# d12 heldBy order: b renewed 5 min ago, a 30, legacy d 60, f 200 (expired), e unparsable renewed; c held by the other sid
lfix "$LD/d12/leases/lsfix-run-a.json" lsfix-run-a $SA 30; lfix "$LD/d12/leases/lsfix-run-b.json" lsfix-run-b $SA 5; lfix "$LD/d12/leases/lsfix-run-c.json" lsfix-run-c $SB 1
lfix "$LD/d12/ACTIVE.lease" lsfix-run-d $SA 60; lfix "$LD/d12/leases/lsfix-run-f.json" lsfix-run-f $SA 200 -5
lfix "$LD/d12/leases/lsfix-run-e.json" lsfix-run-e $SA; jq -c '.renewed="garbage"' "$LD/d12/leases/lsfix-run-e.json" > "$LD/x.tmp" && mv "$LD/x.tmp" "$LD/d12/leases/lsfix-run-e.json"
lsnap(){ find "$LD" -exec stat -c '%n %.9Y %s %a' {} + 2>/dev/null | sort; }
LTREE0=$(lsnap)

ok "require() of lease-store.js prints nothing (scratch env and unset env)" "$(node -e "require('$LS')" 2>&1)$(env -u ORCH_DIR -u ORCH_RUNS_DIR node -e "require('$LS')" 2>&1)" ""
ok "requires only fs and path" "$(grep -oE "require\('[^']+'\)" "$LS" | sort -u | tr '\n' ' ')" "require('fs') require('path') "
ok "exports" "$(LQ "$LD" "Object.keys(s).sort().join(',')")" "RUN_OK,SID_OK,heldBy,isLive,leaseFile,leasesDir,listLeases,readRunLease"
ok "leasesDir / leaseFile paths" "$(LQ "$LD/d1" "[s.leasesDir(D)===D+'/leases', s.leaseFile(D,'$RA')===D+'/leases/$RA.json']")" "[true,true]"
ok "leaseFile: bad runs give null" "$(LQ "$LD/d1" "['../x','a..b','.hid','',null,'a/b','x'.repeat(101)].map(r=>s.leaseFile(D,r))")" "[null,null,null,null,null,null,null]"
ok "RUN_OK / SID_OK match manifest.js" "$(LQ "$LD" "[s.RUN_OK('$RA'),s.RUN_OK('a..b'),s.RUN_OK('-x'),s.SID_OK('$SA'),s.SID_OK('a b'),s.SID_OK('..'),s.SID_OK('x'.repeat(129))]")" "[true,false,false,true,false,false,false]"
echo "-- legacy covers only its own run (D-2)"
ok "d1 legacy: its run reads legacy:true with the ACTIVE.lease path" "$(LQ "$LD/d1" "(r=>[r.legacy,r.file===D+'/ACTIVE.lease',r.lease.session,r.lease.ttl_sec])(s.readRunLease(D,'$RA'))")" "[true,true,\"$SA\",7200]"
ok "d1 legacy: another run reads null" "$(LQ "$LD/d1" "s.readRunLease(D,'$RB')")" "null"
ok "d1 legacy: listLeases with no leases/ dir lists the legacy lease" "$(LL "$LD/d1")" "lsfix-run-a:L:alph"
echo "-- per-run beats legacy; a malformed per-run file shadows legacy (null)"
ok "d2 per-run file wins over legacy for the same run" "$(LQ "$LD/d2" "(r=>[r.legacy,r.file===D+'/leases/$RA.json',r.lease.session])(s.readRunLease(D,'$RA'))")" "[false,true,\"$SB\"]"
ok "d2 listLeases: one entry, the per-run one" "$(LL "$LD/d2")" "lsfix-run-a:P:beta"
ok "d3 malformed per-run plus legacy: null and []" "$(LQ "$LD/d3" "[s.readRunLease(D,'$RA'),s.listLeases(D).length]")" "[null,0]"
ok "d3n per-run file whose run is another (name != run) plus legacy: null, [] and run b null" "$(LQ "$LD/d3n" "[s.readRunLease(D,'$RA'),s.readRunLease(D,'$RB'),s.listLeases(D).length]")" "[null,null,0]"
ok "d3z empty per-run file plus legacy: null" "$(LQ "$LD/d3z" "[s.readRunLease(D,'$RA'),s.listLeases(D).length]")" "[null,0]"
ok "d3e per-run path is a dir (EISDIR) plus legacy: null" "$(LQ "$LD/d3e" "[s.readRunLease(D,'$RA'),s.listLeases(D).length]")" "[null,0]"
echo "-- ignored names and contents"
ok "d4 only the valid <run>.json counts (tmp, .lock/, name!=run, .json, '..', bad sid, array, .txt ignored)" "$(LL "$LD/d4")" "lsfix-run-a:P:alph"
ok "d4 readRunLease of the tmp/lock run and of name!=run run: null" "$(LQ "$LD/d4" "['$RB','lsfix-run-x','lsfix-run-c','lsfix-run-e','lsfix-run-f','lsfix-run-g','lsfix-run-h'].map(r=>s.readRunLease(D,r))")" "[null,null,null,null,null,null,null]"
echo "-- no dir, leases as a file, malformed legacy"
ok "d5 empty dir and a missing orchDir: []" "$(LQ "$LD/d5" "[s.listLeases(D),s.listLeases(D+'/nope'),s.readRunLease(D,'$RA'),s.heldBy(D,'$SA')]")" "[[],[],null,[]]"
ok "d6 leases is a file: []" "$(LQ "$LD/d6" "[s.listLeases(D),s.readRunLease(D,'$RA')]")" "[[],null]"
ok "d7 leases is a file (ENOTDIR = absent) plus legacy: the legacy lease" "$(LL "$LD/d7")|$(LQ "$LD/d7" "s.readRunLease(D,'$RA').legacy")" "lsfix-run-a:L:alph|true"
ok "d8 truncated legacy reads as none" "$(LQ "$LD/d8" "[s.listLeases(D),s.readRunLease(D,'$RA')]")" "[[],null]"
ok "d9 array legacy reads as none" "$(LQ "$LD/d9" "[s.listLeases(D),s.readRunLease(D,'$RA')]")" "[[],null]"
echo "-- chmod 000 is skipped"
ok "d10 fixture really unreadable" "$([ -r "$LD/d10/leases/$RA.json" ] && echo readable || echo unreadable)" "unreadable"
ok "d10 unreadable per-run file skipped, legacy for its run not used" "$(LL "$LD/d10")|$(LQ "$LD/d10" "s.readRunLease(D,'$RA')")" "lsfix-run-b:P:beta|null"
ok "d11 fixture: leases/ dir really unreadable" "$(ls "$LD/d11/leases" >/dev/null 2>&1 && echo readable || echo unreadable)" "unreadable"
ok "d11 unreadable leases/ dir: [], and legacy not used (cannot tell the per-run file is absent)" "$(LQ "$LD/d11" "[s.listLeases(D),s.readRunLease(D,'$RA'),s.readRunLease(D,'$RB')]")" "[[],null,null]"
echo "-- heldBy order, isLive"
ok "d12 heldBy: own leases only, newest renewed first, legacy included, unparsable renewed last" "$(LQ "$LD/d12" "s.heldBy(D,'$SA').map(x=>x.run.slice(-1)+(x.legacy?'L':'')).join(',')")" "b,a,dL,f,e"
ok "d12 heldBy other sid" "$(LQ "$LD/d12" "s.heldBy(D,'$SB').map(x=>x.run).join(',')")" "lsfix-run-c"
ok "d12 heldBy bad or unknown sid: []" "$(LQ "$LD/d12" "[s.heldBy(D,'a b'),s.heldBy(D,undefined),s.heldBy(D,'lsfix-sid-none-0000')]")" "[[],[],[]]"
ok "d12 listLeases sorted by run" "$(LQ "$LD/d12" "s.listLeases(D).map(x=>x.run.slice(-1)).join('')")" "abcdef"
ok "isLive: live, expired (f), garbage, null, explicit now" "$(LQ "$LD/d12" "[s.isLive(s.readRunLease(D,'lsfix-run-a').lease),s.isLive(s.readRunLease(D,'lsfix-run-f').lease),s.isLive({expires:'x'}),s.isLive(null),s.isLive({expires:'2026-01-01T00:00:00Z'},Date.parse('2025-12-31T00:00:00Z'))]")" "[true,false,false,false,true]"
echo "-- never throws, writes nothing"
ok "every export on junk arguments: no throw, safe defaults" "$(LQ "$LD" "(()=>{let th=0;const out=[];const bad=[undefined,null,{},42,[],'',true,Symbol('x')];for(const k of ['leasesDir','leaseFile','readRunLease','listLeases','heldBy','isLive','RUN_OK','SID_OK'])for(const a of bad)for(const b of bad){try{const r=s[k](a,b);out.push(r===null||r===false||(Array.isArray(r)&&r.length===0)?0:1)}catch{th++}}return 'threw='+th+' nondefault='+out.reduce((x,y)=>x+y,0)})()")" "threw=0 nondefault=0"
for d in "$LD"/d*; do LQ "$d" "[s.listLeases(D),s.heldBy(D,'$SA'),s.readRunLease(D,'$RA'),s.readRunLease(D,'lsfix-run-d')]" >/dev/null; done
ok "no file, mtime, size or mode under the fixtures changed by any read" "$(lsnap)" "$LTREE0"
ok "no leases/ dir created where there was none (d1, d5)" "$([ -e "$LD/d1/leases" ] || [ -e "$LD/d5/leases" ] && echo created || echo none)" "none"
chmod 644 "$LD/d10/leases/$RA.json"; chmod 755 "$LD/d11/leases"
# the config-repo .gitignore allowlist checks (lease-store.js tracked, leases/ ignored) belong to the cutover scripts, not this repo

echo "== Boss Stop: <boss root>/runs/orchestrator/<run>/control/cancel refuses launch, exit 4 (scratch boss root; per-command env, never exported)"
BT=$(mktemp -d); trap 'rm -rf "$T" "$BT"' EXIT   # the boss root lives outside $T: its runs/orchestrator/ would read as a run named "orchestrator" to the live-leak check below
BS="$BT/boss"; BSR=bs-stop; BSF="$BS/runs/orchestrator/$BSR/control/cancel"
BL(){ BOSS_PROGRESS_DIR="$BS" L "$@"; }
LEASES0="$(ls "$ORCH_DIR/leases" 2>/dev/null | sort | tr '\n' ' ')"
ok "no cancel file: a launch works (exit 0)" "$(rc BL --run $BSR --row 1 --model opus --effort high)" "0"
BSID1=$(BL --run $BSR --row 1 --model opus --effort high | head -1)
mkdir -p "$(dirname "$BSF")"; echo '{"schema":"boss.cancel/1","requested_at":"2026-11-03T10:00:00.000Z","by":"boss-vscode","reason":"stopped by owner","run_id":"bs-stop","nonce":"ab12cd34"}' > "$BSF"
N0=$(nlaunch $BSR)
BSE=$(BL --run $BSR --row 2 --model opus --effort high 2>&1 >/dev/null); st=$?
ok "cancel present: a new row exits 4" "$st" "4"
has "...the message names the run, 'stopped by owner' and the stop time" "$BSE" "run $BSR stopped by owner (Boss Stop at 2026-11-03T10:00:00.000Z)"
has "...and the undo route" "$BSE" "Undo: Boss board > Undo stop, or rm $BSF"
ok "...no launch event, no stdout" "$(nlaunch $BSR)|$(BL --run $BSR --row 2 --model opus --effort high 2>/dev/null | wc -c | tr -d ' ')" "$N0|0"
ok "the same row again (an idempotent repeat) is refused too, no ID handed out" "$(rc BL --run $BSR --row 1 --model opus --effort high)" "4"
ok "--new is refused" "$(rc BL --run $BSR --row 1 --model opus --effort high --new)" "4"
ok "--continue of an attempt is refused (before the ingest)" "$(rc BL --run $BSR --row 1 --model opus --effort high --continue "$BSR#1.1")" "4"
BSSID=bs-sid-0001-aaaa-0001
ok "with --sid the stop comes before the lease gate: exit 4 and no lease taken" "$(rc BL --run $BSR --row 3 --model opus --effort high --sid=$BSSID)|$(ls "$ORCH_DIR/leases" 2>/dev/null | sort | tr '\n' ' ')" "4|$LEASES0"
ok "there is no bypass flag: --force is a usage error and launches nothing" "$(rc BL --run $BSR --row 2 --model opus --effort high --force)|$(nlaunch $BSR)" "2|$N0"
ok "publishing off (ORCH_PROGRESS=off) does not lift the stop" "$(ORCH_PROGRESS=off BOSS_PROGRESS_DIR="$BS" node "$M" launch --role implementation --agent orch-builder --mode subagent --repo r --area a --run $BSR --row 2 --model opus --effort high >/dev/null 2>&1; echo $?)" "4"
ok "another run is unaffected (exit 0)" "$(rc BL --run bs-other --row 1 --model opus --effort high)" "0"
printf 'not json \xff\x00 garbage' > "$BSF"
BSE=$(BL --run $BSR --row 2 --model opus --effort high 2>&1 >/dev/null); st=$?
ok "a malformed cancel file still stops (exit 4), stamped with its mtime" "$st|$(echo "$BSE" | grep -cE 'Boss Stop at 20[0-9]{2}-[0-9]{2}-[0-9]{2}T')" "4|1"
rm -f "$BSF"; mkdir "$BSF"
ok "a directory named cancel counts as stopped (presence is the contract)" "$(rc BL --run $BSR --row 2 --model opus --effort high)" "4"
rmdir "$BSF"
ok "Undo (file removed): the launch goes through again (exit 0)" "$(rc BL --run $BSR --row 2 --model opus --effort high)" "0"
ok "...and a repeat of row 1 returns its original ID" "$(BL --run $BSR --row 1 --model opus --effort high | head -1)" "$BSID1"
mkdir -p "$BT/home/.claude/boss/runs/orchestrator/bs-scratch/control"; echo '{}' > "$BT/home/.claude/boss/runs/orchestrator/bs-scratch/control/cancel"
ok "scratch ORCH_DIR without BOSS_PROGRESS_DIR ignores any cancel file (hermetic, like progressOn)" "$(HOME="$BT/home" rc L --run bs-scratch --row 1 --model opus --effort high)" "0"
ok "...a stop under the real-root path is not read in scratch mode" "$([ -f "$BT/home/.claude/boss/runs/orchestrator/bs-scratch/control/cancel" ] && echo present)" "present"
ok "the help text documents exit 4" "$(node "$M" help | grep -c 'exit 4')" "1"

ok "live ACTIVE untouched (tests ran on scratch dirs; leases/ from row 7's launches with a sid)" "$(ls "$ORCH_DIR")" "$(printf 'config.json\nerrors.log\nleases\nruns')"

ok "live ACTIVE kept and free of this suite's paths (content check; peer-written, so no md5)" "$(activecheck)" "ok"
ok "no live lease, run manifest or ledger line, run dir or errors.log line names a sid or run of this suite" "$(liveleak "$LIVE" "$T")" "0"
liveleak "$LIVE" "$T" >/dev/null   # a plain call, so SUITE_SIDS / SUITE_RUNS are set in this shell
ok "derived lists hold this suite's own ids (the live check is not vacuous)" "$(jq -rn --argjson s "$SUITE_SIDS" --argjson r "$SUITE_RUNS" '["A1","lr","t1"] - ($s + $r) | join(",")')" ""

echo "== hook hot-path helpers (H5): openAttemptIds and hasLaunch give exactly the fold() / readEvents() answers"
HP="$ORCH_RUNS_DIR/hp-run"; mkdir -p "$HP"
node - "$M" "$HP/manifest.jsonl" <<'EOF' > "$T/hp.out" 2>&1
const [M, file] = process.argv.slice(2), fs = require('fs');
const m = require(M);
const NOW = Date.parse('2026-11-18T12:00:00Z'), iso = min => new Date(NOW - min * 60000).toISOString();
let seq = 0; const L = [];
const ev = (type, attempt, minAgo, extra = {}) => { seq++; L.push(JSON.stringify(Object.assign({ id: `hp-run#e${seq}`, seq, type, ts: iso(minAgo), run: 'hp-run', attempt, row: attempt ? attempt.split('#')[1].split('.')[0] : null, key: `k${seq}`, via: 'cli', session: 's1' }, extra))); };
ev('launch', 'hp-run#1.1', 600);                                    // launched only, 10 h old: a candidate
ev('launch', 'hp-run#2.1', 1); ev('start', 'hp-run#2.1', 1);        // too young
ev('launch', 'hp-run#3.1', 30); ev('start', 'hp-run#3.1', 29);      // started, 29 min: a candidate
ev('launch', 'hp-run#4.1', 50); ev('start', 'hp-run#4.1', 49); ev('stop', 'hp-run#4.1', 40, { final: true, phase: 'finished' });   // finished: not open
ev('launch', 'hp-run#5.1', 90); ev('start', 'hp-run#5.1', 89); ev('stop', 'hp-run#5.1', 80, { final: false, phase: 'agent-stopped' }); // stopped-unreported: open
ev('launch', 'hp-run#6.1', 70); ev('stop', 'hp-run#6.1', 60, { final: false, phase: 'waiting', status: 'completed' });                  // waiting: open
ev('launch', 'hp-run#7.1', 3000);                                   // older than 24 h
ev('launch', 'hp-run#8.1', 30);                                     // same age as 3.1's launch, but 3.1 started later: ordering by since
ev('start', 'hp-run#9.1', 40);                                      // start without a launch: never a candidate
ev('launch', 'hp-run#10.1', 45, { ts: 'not a date' });              // unparseable ts: no age
ev('launch', 'hp-run#12.1', 30); ev('launch', 'hp-run#13.1', 30);   // exact ties on since: first-event order decides
ev('start', null, 20);                                              // unplanned start (no attempt)
ev('launch', 'hp-run#14.1', 200); ev('void', 'hp-run#14.1', 199);   // voided (never launched): fold() leaves it out, so must openAttemptIds
ev('launch', 'hp-run#15.1', 70); ev('stop', 'hp-run#15.1', 60, { final: false, phase: 'waiting', status: 'completed' }); ev('outcome', 'hp-run#15.1', 5, { outcome: 'accepted' });   // waiting, then closed by the owner: not open (the gap hint's false alarm)
ev('launch', 'hp-run#16.1', 80); ev('start', 'hp-run#16.1', 79); ev('outcome', 'hp-run#16.1', 5, { outcome: 'dropped' });   // dropped while still running: closed too
const raw = L.slice(); raw.splice(3, 0, '{"torn":'); raw.push('[1,2]', '{"type":"launch","attempt":"hp-run#11.1"}', '', '   ');   // bad lines: torn, array, no id, blank
fs.writeFileSync(file, raw.join('\n') + '\n');
let bad = 0; const say = (k, a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) { bad++; console.log('MISMATCH', k, JSON.stringify(a), JSON.stringify(b)); } };
const cand = (now, min, max) => m.fold('hp-run', { now }).attempts.filter(a => a.launch && a.age_min != null && a.age_min * 60000 >= min && a.age_min * 60000 < max && !a.outcome).map(a => a.id);
let nonEmpty = 0;
for (const dm of [0, 1, 2, 5, 31, 100, 700, 1500, 1500.5, 5000]) for (const [min, max] of [[120000, 86400000], [0, 1e12], [60000, 3600000]]) {
  const now = NOW + dm * 60000, want = cand(now, min, max), got = m.openAttemptIds('hp-run', { now, minAgeMs: min, maxAgeMs: max });
  say(`open ${dm}/${min}`, got, want); if (want.length) nonEmpty++;
}
{ const F = m.fold('hp-run', { now: NOW }).attempts, st = id => F.find(a => a.id === id).state, open = m.openAttemptIds('hp-run', { now: NOW + 30 * 60000, minAgeMs: 0, maxAgeMs: 1e12 });
  say('closed attempt 15.1 still reads waiting in fold (the false-alarm shape)', st('hp-run#15.1'), 'waiting');
  say('closed attempts 15.1 and 16.1 are not open', open.filter(id => /#1[56]\.1$/.test(id)), []);
  say('an unclosed waiting attempt (6.1) is still open', open.includes('hp-run#6.1'), true); }
const ids = new Set(m.readEvents('hp-run').events.map(e => e.attempt).filter(Boolean)); ids.add('hp-run#99.1'); ids.add('hp-run#11.1'); ids.add('other#1.1'); ids.add('weird "id\\');
for (const id of ids) say(`hasLaunch ${id}`, m.hasLaunch('hp-run', id), m.readEvents('hp-run').events.some(e => e.type === 'launch' && e.attempt === id));
say('hasLaunch missing run', m.hasLaunch('no-such-run', 'hp-run#1.1'), false);
say('openAttemptIds missing run', m.openAttemptIds('no-such-run', { now: NOW, minAgeMs: 0, maxAgeMs: 1e12 }), []);
console.log(`checked bad=${bad} nonEmptyCases=${nonEmpty} ids=${ids.size}`);
EOF
ok "openAttemptIds / hasLaunch equal the fold()/readEvents() expressions on a manifest with every state, ties, torn and non-event lines" "$(tail -1 "$T/hp.out" | sed -E 's/nonEmptyCases=[0-9]+/nonEmptyCases=N/; s/ids=[0-9]+/ids=N/')" "checked bad=0 nonEmptyCases=N ids=N"
ok "the equivalence check is not vacuous (many cases with a non-empty candidate list)" "$(tail -1 "$T/hp.out" | sed -E 's/.*nonEmptyCases=([0-9]+).*/\1/' | awk '{print ($1>=10)?"yes":"no"}')" "yes"

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
echo "== hardening: void, run paths on lease/progress verbs, flag-naming errors, council/background-bash launch"
VR=vd-run
ok "void: launch allocates vd-run#5.1" "$(L --run $VR --row 5 --model opus --effort high 2>/dev/null | head -1)" "$VR#5.1"
ok "void: a second --new launch makes .2" "$(L --run $VR --row 5 --model opus --effort high --new 2>/dev/null | head -1)" "$VR#5.2"
ok "void --attempt .1 exits 0 and says voided" "$(node "$M" void --attempt "$VR#5.1" --reason 'row collision' 2>&1)" "$VR#5.1 voided"
ok "void: the event is on the manifest with its reason" "$(jq -c 'select(.type=="void")|[.attempt,.reason,.via]' "$(MF $VR)")" "[\"$VR#5.1\",\"row collision\",\"cli\"]"
ok "void again: no second event, says already voided" "$(node "$M" void --attempt "$VR#5.1")|$(jq -c 'select(.type=="void")' "$(MF $VR)" | wc -l | tr -d ' ')" "$VR#5.1 already voided|1"
ok "show --json lists only the live attempt, and the voided ID separately" "$(node "$M" show --run $VR --json | jq -c '[[.attempts[].id], .voided]')" "[[\"$VR#5.2\"],[\"$VR#5.1\"]]"
ok "show text names the voided attempt" "$(node "$M" show --run $VR | grep -c "^voided: $VR#5.1")" "1"
ok "reconcile does not judge the voided attempt" "$(node "$M" reconcile --run $VR --json | jq -c '[.rows[].attempt]')" "[\"$VR#5.2\"]"
ok "a plain launch reuses the live attempt, never the voided one" "$(L --run $VR --row 5 --model opus --effort high 2>/dev/null | head -1)" "$VR#5.2"
node "$M" void --attempt "$VR#5.2" >/dev/null
ok "with every attempt voided a plain launch allocates .3 (an ID is never reused)" "$(L --run $VR --row 5 --model opus --effort high 2>/dev/null | head -1)" "$VR#5.3"
ok "void of an attempt that ran (a stop event) is refused (exit 2)" "$(L --run $VR --row 6 --model opus --effort high >/dev/null 2>&1; node "$M" event --run $VR --type stop --json "{\"attempt\":\"$VR#6.1\",\"task_id\":\"tv1\",\"phase\":\"finished\"}" >/dev/null; rc node "$M" void --attempt "$VR#6.1")" "2"
ok "void of an unknown attempt is refused (exit 2)" "$(rc node "$M" void --attempt "$VR#9.1")" "2"
ok "void with a bad ID names the flag and value" "$(node "$M" void --attempt nope 2>&1 | grep -c "manifest: error: --attempt 'nope'")" "1"
ok "void without --attempt names it" "$(node "$M" void 2>&1 | grep -c "void: needs --attempt")" "1"
ok "void: help documents it" "$(node "$M" help | grep -c '^  void ')" "1"
# run path on the lease verbs and the other --run verbs
LS="aaaaaaaa-0000-0000-0000-000000000001"
ok "lease acquire accepts a run folder path" "$(node "$M" lease acquire --run "$ORCH_RUNS_DIR/$VR" --sid $LS 2>&1 | grep -c "(run $VR)")" "1"
ok "lease check accepts a run folder path" "$(node "$M" lease check --run "$ORCH_RUNS_DIR/$VR/" --sid $LS | head -1)" "state: mine"
ok "lease release accepts a run folder path" "$(rc node "$M" lease release --run "$ORCH_RUNS_DIR/$VR" --sid $LS)" "0"
ok "status --run accepts a path" "$(node "$M" status --run "$ORCH_RUNS_DIR/$VR" --json | jq -r .run)" "$VR"
ok "a path outside the runs dir is refused, naming --run and the value" "$(node "$M" show --run "$T/elsewhere/$VR" 2>&1 | grep -c "manifest: error: --run '$T/elsewhere/$VR'")" "1"
# flag-naming errors
ok "a flag followed by a flag names both" "$(node "$M" launch --run $VR --row --model opus 2>&1 | grep -cF -- "--row needs a value (the next argument is the flag '--model')")" "1"
ok "a flag given twice shows both values" "$(node "$M" show --run a --run b 2>&1 | grep -cF -- "--run is given twice ('a' and 'b')")" "1"
# council / background-bash on launch
ok "launch accepts --model council-5seat --mode background-bash" "$(node "$M" launch --run $VR --row 7 --role review --agent council --model council-5seat --effort high --mode background-bash --repo r --area a 2>/dev/null | head -1)" "$VR#7.1"
ok "launch rejects --model council-x, naming flag and value" "$(node "$M" launch --run $VR --row 8 --role review --agent council --model council-x --effort high --mode background-bash --repo r --area a 2>&1 | grep -cF -- "--model 'council-x'")" "1"
ok "a turn-limit stop event is final by default" "$(node "$M" event --run $VR --type stop --json "{\"attempt\":\"$VR#7.1\",\"task_id\":\"tv7\",\"phase\":\"stopped-turn-limit\"}" >/dev/null; jq -r 'select(.type=="stop" and .task_id=="tv7")|.final' "$(MF $VR)")" "true"

echo "pass=$pass fail=$fail"; [ "$fail" -eq 0 ]
