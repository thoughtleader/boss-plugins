#!/usr/bin/env bash
# Guard tests: orchestrator-guard.js exemptions (F-08, A-02, A-09, A-10) and the one-reminder-per-session bloat signal (section 8).
# Runs the checkout's guard with its warning slots and data dir in scratch (BOSS_DATA, ORCH_GUARD_STATE). Re-runnable; cleans up.
# Usage: bash plugins/boss/test/guard-tests.sh     (exit 0 = all pass)
set -u
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"   # the checkout this suite tests (a worktree runs its own code)
export ORCH_GUARD_AFTER=1   # sections 1-7 test the exemption rules: the first non-exempt call reminds (section 8 tests the real threshold)
unset ORCH_DIR ORCH_RUNS_DIR ORCH_PROJECTS_DIR CLAUDE_CONFIG_DIR BOSS_DATA BOSS_PROGRESS_DIR   # section 7 sets ORCH_DIR per call
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
GT=$(mktemp -d); N=0
trap 'rm -rf "$GT"' EXIT
GUARD="$CL/hooks/orchestrator-guard.js"
export BOSS_DATA="$GT/data"                                  # the default data dir under test (what the guard sees with no ORCH_DIR)
export ORCH_GUARD_STATE="$GT/state/orchestrator-guard"       # warning slots stay in scratch, never in the checkout
ssid(){ echo "guardtest-$(cat /proc/sys/kernel/random/uuid)"; }
# t <sid> <tool> <json tool_input> [agent_id]
t(){ jq -cn --arg s "$1" --arg t "$2" --argjson i "$3" --arg a "${4:-}" '{session_id:$s, hook_event_name:"PreToolUse", tool_name:$t, tool_input:$i} + (if $a == "" then {} else {agent_id:$a} end)' | node "$GUARD"; }
b(){ t "$1" Bash "$(jq -cn --arg c "$2" '{command:$c}')"; }       # b <sid> <command>
p(){ t "$1" "$2" "$(jq -cn --arg p "$3" '{file_path:$p, path:$p}')"; } # p <sid> <tool> <path>
warned(){ jq -r '.hookSpecificOutput.additionalContext // ""' | grep -c '^\[orchestrator\]'; }
silent(){ ok "$1" "$2" ""; }
warns(){ ok "$1" "$(printf %s "$2" | warned)" "1"; }
R="$BOSS_DATA"; RUN="$R/runs/2026-10-20-x"   # R = the data dir; the legacy ~/.claude/orchestrator/... command paths are tested in section 5

echo "== 1. Write/Edit exemptions (F-08 item 1)"
S=$(ssid)
silent "Write into a run folder is silent" "$(p $S Write "$RUN/PLAN.md")"
S=$(ssid)
warns "Edit of ACTIVE warns (exemption retired, OD-2)" "$(p $S Edit "$R/ACTIVE")"
S=$(ssid)
silent "Write of ACTIVE.lease is silent" "$(p $S Write "$R/ACTIVE.lease")"
silent "Write to a memory dir is silent" "$(p $S Write "$HOME/.claude/projects/-x/memory/MEMORY.md")"
S=$(ssid)
warns "run-folder path with .. warns" "$(p $S Write "$R/runs/../../settings.json")"
S=$(ssid)
warns "other orchestrator file (not runs/ACTIVE) warns on Write" "$(p $S Write "$R/ledger.js")"
S=$(ssid)
warns "relative path warns" "$(p $S Write "orchestrator/runs/x/y.md")"
S=$(ssid)
warns "memory-looking path outside HOME/.claude warns" "$(p $S Write "/tmp/.claude/projects/x/memory/y.md")"

echo "== 2. Read/Grep/Glob count like any other non-exempt call"
S=$(ssid)
warns "Read of a repo file reminds (AFTER=1)" "$(p $S Read "$HOME/some/repo/src/x.ts")"
silent "second Read is silent (one reminder per session)" "$(p $S Read "$HOME/some/repo/src/y.ts")"
silent "Bash is silent too after the reminder" "$(b $S 'npm test')"
ok "one reminder file, no w/r slots" "$(ls "$GT/state/orchestrator-guard" | grep "^$S" | grep -c '\.[wr][0-9]$')" "0"
S=$(ssid)
silent "Read of a run-folder result is silent" "$(p $S Read "$RUN/PLAN.md")"
silent "Grep under ~/.claude/orchestrator is silent" "$(p $S Grep "$R")"
silent "Read of a memory file is silent" "$(p $S Read "$HOME/.claude/projects/-x/memory/MEMORY.md")"
S=$(ssid)
warns "Glob with absolute pattern outside warns" "$(t $S Glob '{"pattern":"/etc/**/*.conf"}')"
S=$(ssid)
warns "Grep with no path warns" "$(t $S Grep '{"pattern":"foo"}')"
S=$(ssid)
silent "Read from a subagent is silent" "$(t $S Read "{\"file_path\":\"$HOME/x/y\"}" agent-1)"

echo "== 3. Bash TRIVIAL additions (F-08 item 2)"
S=$(ssid)
silent "git rev-parse is silent" "$(b $S 'git rev-parse --show-toplevel')"
silent "git -C path rev-parse HEAD is silent" "$(b $S 'git -C ~/.claude rev-parse HEAD')"
silent "git show --stat is silent" "$(b $S 'git show --stat HEAD')"
silent "git branch --show-current is silent" "$(b $S 'git branch --show-current')"
silent "git log with flags is silent" "$(b $S 'git log --oneline -5')"
silent "git log | head -5 is silent" "$(b $S 'git log --oneline | head -5')"
silent "git status | wc -l is silent" "$(b $S 'git status --short | wc -l')"
silent "git log | jq filter is silent" "$(b $S "git log --oneline | jq -R 'length'")"
silent "trailing 2>/dev/null is silent" "$(b $S 'git rev-parse HEAD 2>/dev/null')"
silent "ls with 2>/dev/null is silent" "$(b $S 'ls /tmp 2>/dev/null')"
S=$(ssid); warns "git branch -D warns" "$(b $S 'git branch -D x')"
S=$(ssid); warns "two pipes warn" "$(b $S 'git log | head -5 | wc -l')"
S=$(ssid); warns "pipe to another command warns" "$(b $S 'git log | sh')"
S=$(ssid); warns "pipe to head with a file operand warns" "$(b $S 'git log | head -5 /etc/passwd')"
S=$(ssid); warns "pipe to jq -f warns" "$(b $S 'git log | jq -f prog.jq')"
S=$(ssid); warns "other redirect (2>&1) warns" "$(b $S 'git rev-parse HEAD 2>&1')"
S=$(ssid); warns "git rev-parse; npm test warns" "$(b $S 'git rev-parse HEAD; npm test')"

echo "== 4. A-09: --output and .env"
S=$(ssid); warns "git log --output=file warns" "$(b $S 'git log --output=/tmp/x')"
S=$(ssid); warns "git diff --stat -o file warns" "$(b $S 'git diff --stat -o /tmp/x')"
S=$(ssid); warns "git log --output file warns" "$(b $S 'git log --output /tmp/x')"
S=$(ssid); warns "cat .env warns" "$(b $S 'cat .env')"
S=$(ssid); warns "head -5 app/.env.local warns" "$(b $S 'head -5 app/.env.local')"
S=$(ssid); warns "wc ./.env.production warns" "$(b $S 'wc ./.env.production')"
S=$(ssid); warns "cat .env piped to head warns" "$(b $S 'cat .env | head -3')"
S=$(ssid); silent "cat of an ordinary file is silent" "$(b $S 'cat README.md')"
S=$(ssid); silent "cat of environment.ts is silent" "$(b $S 'cat src/environment.ts')"

echo "== 5. A-10 and HOUSEKEEPING anchoring (F-08 item 5)"
S=$(ssid)
silent "ledger.js show (installed) is silent" "$(b $S 'node ~/.claude/orchestrator/ledger.js show')"
silent "ledger add with quoted args is silent" "$(b $S 'node $HOME/.claude/orchestrator/ledger.js add --run r --row 1 --role review --area "header rework"')"
silent "quoted absolute ledger path is silent" "$(b $S "node \"$HOME/.claude/orchestrator/ledger.js\" show")"
silent "wfp.js status is silent" "$(b $S 'node ~/.claude/wfp/wfp.js status')"
silent "guard CLI status is silent" "$(b $S 'node ~/.claude/hooks/orchestrator-guard.js status')"
silent "manifest.js show is silent" "$(b $S 'node ~/.claude/orchestrator/manifest.js show --run x')"
silent "rtk-prefixed ledger.js is silent" "$(b $S 'rtk node ~/.claude/orchestrator/ledger.js show')"
S=$(ssid); warns "node /tmp/x/orchestrator/ledger.js show warns (A-10)" "$(b $S 'node /tmp/x/orchestrator/ledger.js show')"
S=$(ssid); warns "relative ./orchestrator/ledger.js warns" "$(b $S 'node ./orchestrator/ledger.js show')"
S=$(ssid); warns "another orchestrator script warns" "$(b $S 'node ~/.claude/orchestrator/other.js')"
S=$(ssid); warns "ledger.js show; npm test warns (was a known gap)" "$(b $S 'node ~/.claude/orchestrator/ledger.js show; npm test')"
S=$(ssid); warns "ledger.js show && npm test warns" "$(b $S 'node ~/.claude/orchestrator/ledger.js show && npm test')"
S=$(ssid); warns "manifest.js show; rm -rf x warns" "$(b $S 'node ~/.claude/orchestrator/manifest.js show; rm -rf x')"
S=$(ssid)
silent "cat of a memory file is silent" "$(b $S 'cat ~/.claude/projects/-x/memory/MEMORY.md')"
silent "ls of a memory dir is silent" "$(b $S 'ls ~/.claude/projects/-x/memory/')"
S=$(ssid); warns "memory cat then a chain warns" "$(b $S 'cat ~/.claude/projects/-x/memory/a.md; rm -rf /tmp/zz')"
S=$(ssid); warns "memory path as a later arg to an interpreter warns" "$(b $S 'node -e foo ~/.claude/projects/-x/memory/a.md')"
S=$(ssid); warns "foreign dir named .claude/projects/x/memory warns" "$(b $S "touch /tmp/.claude/projects/x/memory/a.md")"

echo "== 6. Plain behaviour, label, on/off"
S=$(ssid)
out=$(b $S 'npm test')
ok "ordinary work reminds" "$(printf %s "$out" | warned)" "1"
ok "label reads [model/effort]" "$(printf %s "$out" | jq -r '.hookSpecificOutput.additionalContext' | grep -c 'subagent with a \[model/effort\] label')" "1"
ok "subagent stays silent" "$(t $S Bash '{"command":"npm test"}' agent-9)" ""
S=$(ssid)
b $S 'npm a' >/dev/null
silent "second call is capped (one reminder per session)" "$(b $S 'npm d')"
sidv=$S
node "$GUARD" on --sid=$sidv >/dev/null
ok "'on' deletes the reminder slot" "$(ls "$GT/state/orchestrator-guard" | grep -c "^$sidv\.b1")" "0"
warns "'on' re-arms: next call reminds again" "$(b $sidv 'npm e')"
ok "status shows reminders 1/1" "$(node "$GUARD" status --sid=$sidv | grep -c 'reminders 1/1')" "1"
S=$(ssid)
node "$GUARD" off --sid=$S >/dev/null
silent "'off' silences" "$(b $S 'npm test')"
node "$GUARD" on --sid=$S >/dev/null
warns "'on' removes the off file" "$(b $S 'npm test')"
ok "global kill env is silent" "$(S=$(ssid); ORCHESTRATOR_GUARD=off b $S 'npm test')" ""

echo "== 7. ORCH_DIR from env, leases/ exemption, fail-open (per-run lease row 2)"
T=$(mktemp -d); trap 'rm -rf "$GT" "$T"' EXIT
S=$(ssid)
silent "default: Write to \$R/leases/x.json is silent" "$(p $S Write "$R/leases/x.json")"
silent "default: Edit of \$R/leases/x.json.lock/y is silent" "$(p $S Edit "$R/leases/x.json.lock/y")"
S=$(ssid)
warns "default: \$R/leases/../ledger.js warns" "$(p $S Write "$R/leases/../ledger.js")"
S=$(ssid)
warns "default: a sibling named leases2 warns" "$(p $S Write "$R/leases2/x.json")"
S=$(ssid)
warns "default: ledger.js in a leases-named file warns" "$(p $S Write "$R/leases-x.json")"
# ORCH_DIR set: the override moves every orchestrator path match
O="$T/o"
S=$(ssid); silent "ORCH_DIR: Write to \$O/leases/x.json is silent" "$(ORCH_DIR=$O p $S Write "$O/leases/x.json")"
S=$(ssid); silent "ORCH_DIR: Write to \$O/runs/x/PLAN.md is silent" "$(ORCH_DIR=$O p $S Write "$O/runs/x/PLAN.md")"
S=$(ssid); warns "ORCH_DIR: Write of \$O/ACTIVE warns (exemption retired, OD-2)" "$(ORCH_DIR=$O p $S Write "$O/ACTIVE")"
S=$(ssid); silent "ORCH_DIR: Write of \$O/ACTIVE.lease is silent" "$(ORCH_DIR=$O p $S Write "$O/ACTIVE.lease")"
S=$(ssid); silent "ORCH_DIR: Write of \$O/leases itself is silent" "$(ORCH_DIR=$O p $S Write "$O/leases")"
S=$(ssid); silent "ORCH_DIR with a trailing slash is the same dir" "$(ORCH_DIR=$O/ p $S Write "$O/leases/x.json")"
S=$(ssid); silent "ORCH_DIR with .. segments resolves" "$(ORCH_DIR=$O/../o p $S Write "$O/leases/x.json")"
S=$(ssid); silent "ORCH_DIR relative resolves against cwd" "$(cd "$T" && ORCH_DIR=o p $S Write "$O/leases/x.json")"
S=$(ssid); silent "ORCH_DIR: Read under \$O is silent" "$(ORCH_DIR=$O p $S Read "$O/leases/x.json")"
S=$(ssid); warns "ORCH_DIR: \$O/leases/../ledger.js warns" "$(ORCH_DIR=$O p $S Write "$O/leases/../ledger.js")"
S=$(ssid); warns "ORCH_DIR: \$O/ledger.js warns" "$(ORCH_DIR=$O p $S Write "$O/ledger.js")"
S=$(ssid); warns "ORCH_DIR: another dir's leases/x.json warns" "$(ORCH_DIR=$O p $S Write "$T/other/leases/x.json")"
S=$(ssid); warns "ORCH_DIR: the default \$R/leases/x.json no longer matches" "$(ORCH_DIR=$O p $S Write "$R/leases/x.json")"
S=$(ssid); silent "ORCH_DIR set: housekeeping commands stay exempt (ROOT-anchored)" "$(ORCH_DIR=$O b $S 'node ~/.claude/orchestrator/ledger.js show')"
S=$(ssid); warns "ORCH_DIR set: ordinary Bash still warns" "$(ORCH_DIR=$O b $S 'npm test')"
S=$(ssid); silent "ORCH_DIR='' falls back to the default (leases exempt)" "$(ORCH_DIR= p $S Write "$R/leases/x.json")"
S=$(ssid); silent "memory write stays exempt with ORCH_DIR set" "$(ORCH_DIR=$O p $S Write "$HOME/.claude/projects/-x/memory/MEMORY.md")"
# output is unchanged for everything that is not a lease/orchestrator path
ok "warning text is identical with ORCH_DIR set (Bash)" "$(ORCH_DIR=$O b $(ssid) 'npm test')" "$(b $(ssid) 'npm test')"
ok "warning text is identical with ORCH_DIR set (Write)" "$(ORCH_DIR=$O p $(ssid) Write /etc/x)" "$(p $(ssid) Write /etc/x)"
ok "warning text is identical with ORCH_DIR set (Read)" "$(ORCH_DIR=$O p $(ssid) Read "$HOME/z/a")" "$(p $(ssid) Read "$HOME/z/a")"
ok "...and the Bash warning is a real warning" "$(ORCH_DIR=$O b $(ssid) 'npm test' | warned)" "1"
# fail-open: every input shape and ORCH_DIR shape exits 0 with empty stderr, and prints nothing for junk
mkdir -p "$T/f"; : > "$T/f/leases"; : > "$T/afile"
rawrun(){ # rawrun <stdin text> [ENV=val ...]; prints "rc|stdout bytes|stderr bytes"
  local in="$1"; shift
  printf %s "$in" | env "$@" node "$GUARD" >"$T/out" 2>"$T/err"; echo "$?|$(wc -c <"$T/out" | tr -d ' ')|$(wc -c <"$T/err" | tr -d ' ')"; }
G='{"session_id":"guardtest-failopen-0001","hook_event_name":"PreToolUse"'
for inp in '' ' ' 'garbage' '[]' 'null' '0' '"str"' '{}' '{"session_id":5}' '{"session_id":"guardtest-failopen-0001"}' \
  "$G"',"tool_name":"Write"}' "$G"',"tool_name":"Write","tool_input":null}' "$G"',"tool_name":"Write","tool_input":"x"}' \
  "$G"',"tool_name":"Write","tool_input":{"file_path":5}}' "$G"',"tool_name":"Write","tool_input":{"file_path":{"a":1}}}' \
  "$G"',"tool_name":"Write","tool_input":{"file_path":["x"]}}' "$G"',"tool_name":"Edit","tool_input":{"notebook_path":null}}' \
  "$G"',"tool_name":"Bash","tool_input":{"command":5}}' "$G"',"tool_name":"Bash","tool_input":{"command":{"a":1}}}' "$G"',"tool_name":"Bash","tool_input":{}}' \
  "$G"',"tool_name":"Read","tool_input":{"pattern":5}}' "$G"',"tool_name":"Grep","tool_input":{"path":{}}}' "$G"',"agent_id":5,"tool_name":"Bash"}'; do
  for envs in "X=1" "ORCH_DIR=$T/o" "ORCH_DIR=$T/f" "ORCH_DIR=$T/afile" "ORCH_DIR=$T/nonexistent/deep" "ORCH_DIR=" "ORCH_DIR=rel/dir" "ORCH_DIR=/" "ORCH_DIR=$T/o/../.."; do
    r=$(rawrun "$inp" "$envs")
    case "$r" in 0\|*\|0) ok "fail-open rc=0, no stderr: ${inp:0:60} [$envs]" "ok" "ok" ;; *) ok "fail-open rc=0, no stderr: ${inp:0:60} [$envs]" "$r" "0|*|0" ;; esac
  done
done
ok "junk stdin prints nothing" "$(rawrun 'garbage' ORCH_DIR=$T/o)" "0|0|0"
ok "empty stdin prints nothing" "$(rawrun '' ORCH_DIR=$T/o)" "0|0|0"
ok "a lease write with ORCH_DIR=<file>/leases layout prints nothing" "$(rawrun "$G"',"tool_name":"Write","tool_input":{"file_path":"'"$T"'/f/leases/x.json"}}' ORCH_DIR=$T/f)" "0|0|0"
G2='{"session_id":"guardtest-failopen-0002","hook_event_name":"PreToolUse"'
ok "an unexempt write exits 0, empty stderr, with ORCH_DIR=<file>" "$(rawrun "$G2"',"tool_name":"Write","tool_input":{"file_path":"/etc/zz"}}' ORCH_DIR=$T/afile | cut -d'|' -f1,3)" "0|0"
ok "...and it still warned (stdout non-empty)" "$(grep -c '^\[orchestrator\]' <(jq -r '.hookSpecificOutput.additionalContext // ""' "$T/out"))" "1"
ok "the real leases dir is not touched by the suite" "$([ -e "$T/o/leases" ] && echo present || echo none)" "none"

echo "== 8. Real threshold (default 6), exempt calls do not count, one reminder, idle gap resets"
S=$(ssid); got=""
for k in 1 2 3 4 5; do got="$got$(ORCH_GUARD_AFTER= b $S "npm run s$k")"; done
silent "five inline calls stay silent (default threshold)" "$got"
warns "sixth inline call reminds" "$(ORCH_GUARD_AFTER= b $S 'npm run s6')"
ok "the reminder names the count" "$(S2=$(ssid); for k in 1 2 3 4 5; do ORCH_GUARD_AFTER= b $S2 "npm x$k" >/dev/null; done; ORCH_GUARD_AFTER= b $S2 'npm x6' | jq -r '.hookSpecificOutput.additionalContext' | grep -c 'run 6 tool calls')" "1"
silent "seventh call is silent (one per session)" "$(ORCH_GUARD_AFTER= b $S 'npm run s7')"
S=$(ssid); got=""
for k in 1 2 3 4 5 6 7 8; do got="$got$(ORCH_GUARD_AFTER= b $S 'git status --short')$(ORCH_GUARD_AFTER= p $S Write "$RUN/x$k.md")"; done
silent "exempt calls never count" "$got"
S=$(ssid); mkdir -p "$GT/state/orchestrator-guard"
old=$(( $(date +%s) * 1000 - 3600000 )); for k in 1 2 3 4 5; do echo $((old + k)) >> "$GT/state/orchestrator-guard/$S.calls"; done
silent "five old calls (an hour ago) plus one new: the idle gap resets the run" "$(ORCH_GUARD_AFTER= b $S 'npm now')"

echo "== 9. Plugin layout: plugin-root housekeeping, guard.exemptPaths, state stays out of the plugin dir"
S=$(ssid)
silent "plugin lib/ledger.js show is silent" "$(b $S "node \"$CL/lib/ledger.js\" show")"
silent "plugin lib/manifest.js launch with args is silent" "$(b $S "node $CL/lib/manifest.js launch --run r --row 1")"
silent "plugin wfp/wfp.js status is silent" "$(b $S "node $CL/wfp/wfp.js status")"
silent "\$BOSS_LIB/ledger.js is silent" "$(b $S 'node "$BOSS_LIB/ledger.js" show')"
S=$(ssid); warns "another plugin lib script warns" "$(b $S "node $CL/lib/doctor.js")"
S=$(ssid); warns "ledger.js from a foreign dir warns" "$(b $S "node /tmp/x/lib/ledger.js show")"
S=$(ssid); warns "plugin ledger.js chained with npm warns" "$(b $S "node $CL/lib/ledger.js show; npm test")"
S=$(ssid)
silent "Read of a plugin file is silent" "$(p $S Read "$CL/lib/paths.js")"
silent "Grep under the data dir is silent" "$(p $S Grep "$R/leases")"
S=$(ssid); warns "Write to a plugin file still warns" "$(p $S Write "$CL/lib/paths.js")"
S=$(ssid); warns "Write to the data dir root (not runs/leases) warns" "$(p $S Write "$R/config.json")"
# guard.exemptPaths (config.json in the data dir)
X="$T/extra"; mkdir -p "$R" "$X"
S=$(ssid); warns "before config: a path under an extra dir warns" "$(p $S Write "$X/notes.md")"
printf '{"guard":{"exemptPaths":["%s","relative/ignored"]}}' "$X" > "$R/config.json"
S=$(ssid); silent "exemptPaths: Write under the listed dir is silent" "$(p $S Write "$X/notes.md")"
S=$(ssid); silent "exemptPaths: Read under the listed dir is silent" "$(p $S Read "$X/deep/a.md")"
S=$(ssid); warns "exemptPaths: a sibling dir with the same prefix warns" "$(p $S Write "${X}2/notes.md")"
S=$(ssid); warns "exemptPaths: .. out of the listed dir warns" "$(p $S Write "$X/../other.md")"
printf 'not json' > "$R/config.json"
S=$(ssid); warns "a broken config.json is ignored (still warns, no crash)" "$(p $S Write "$X/notes.md")"
rm -f "$R/config.json"
# state: warning slots follow ORCH_GUARD_STATE, else <data dir>/guard-state, never the plugin dir
S=$(ssid); b $S 'npm t' >/dev/null
ok "state slot is in ORCH_GUARD_STATE" "$(ls "$GT/state/orchestrator-guard" | grep -c "^$S")" "2"
S=$(ssid); ORCH_GUARD_STATE= b $S 'npm t' >/dev/null
ok "default state dir is <data dir>/guard-state" "$(ls "$BOSS_DATA/guard-state" | grep -c "^$S")" "2"
ok "nothing is written under the plugin dir" "$(find "$CL" \( -name '*.calls' -o -name '*.b1' -o -name guard-state -o -path '*/hooks/state' \) | wc -l | tr -d ' ')" "0"


echo "== 9. hooks.json prefilter (H1): a subagent call never launches node; every other input reaches the guard byte for byte"
# The command string is taken from hooks.json itself (the guard's PreToolUse entry), run under sh -c with CLAUDE_PLUGIN_ROOT set.
# A shim `node` first on PATH logs each launch and tees stdin to a file, then runs the real node.
HJ="$CL/hooks/hooks.json"
PCMD=$(jq -r '.hooks.PreToolUse[] | select(.hooks[0].command | contains("orchestrator-guard.js")) | .hooks[0].command' "$HJ")
ok "prefilter: exactly one guard entry in hooks.json, run through sh -c with boss-node as its target" "$(printf %s "$PCMD" | grep -c '^sh -c .*"${CLAUDE_PLUGIN_ROOT}/bin/boss-node" "${CLAUDE_PLUGIN_ROOT}/hooks/orchestrator-guard.js"$')" "1"
ok "prefilter: the role-write-guard entry is prefiltered on role names, never on agent_id (it must see subagent calls; tested in role-write-guard.test.js)" "$(jq -r '.hooks.PreToolUse[] | select(.hooks[0].command | contains("role-write-guard.js")) | .hooks[0].command | (startswith("sh -c ") and contains("orch-scout") and (contains("agent_id") | not))' "$HJ")" "true"
SHIM="$GT/shim"; mkdir -p "$SHIM"; REALNODE=$(command -v node)
printf '#!/bin/sh\necho call >> "%s/shim.calls"\ntee "%s/shim.stdin" | "%s" "$@"\n' "$GT" "$GT" "$REALNODE" > "$SHIM/node"; chmod +x "$SHIM/node"
pf(){ rm -f "$GT/shim.calls" "$GT/shim.stdin"; PF_OUT=$(CLAUDE_PLUGIN_ROOT="$CL" PATH="$SHIM:$PATH" sh -c "$PCMD" < "$1" 2>"$GT/pf.err"); PF_RC=$?; }   # pf <stdin file>; sets PF_OUT, PF_RC
shim_calls(){ [ -f "$GT/shim.calls" ] && wc -l < "$GT/shim.calls" | tr -d ' ' || echo 0; }
mk_in(){ printf '%s' "$1" > "$GT/in.json"; }   # raw bytes, no trailing newline
SA=$(ssid)
mk_in "$(jq -cn --arg s "$SA" '{session_id:$s,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{command:"npm test"},agent_id:"agent-9f3",agent_type:"boss:orch-builder"}')"
pf "$GT/in.json"
ok "subagent call (agent_id): exit 0, no output, node never launched" "$PF_RC|${#PF_OUT}|$(shim_calls)|$(wc -c < "$GT/pf.err" | tr -d ' ')" "0|0|0|0"
ok "subagent call: the guard's own state is untouched (no .calls file for that session)" "$(ls "$ORCH_GUARD_STATE" 2>/dev/null | grep -c "^$SA")" "0"
# main chat, awkward bytes: newline escape, backslash, percent, multibyte, a quote, and TWO trailing newlines
SB=$(ssid)
jq -cn --arg s "$SB" '{session_id:$s,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{command:"printf \"50%% \\n é✓ \\\\ done\" | wc -c"}}' > "$GT/in.json"; printf '\n\n' >> "$GT/in.json"
pf "$GT/in.json"
cmp -s "$GT/in.json" "$GT/shim.stdin"; same=$?
ok "main-chat call: node launched once, stdin reached it byte for byte (cmp, trailing newlines kept)" "$(shim_calls)|$same|$PF_RC" "1|0|0"
SC=$(ssid)
jq -cn --arg s "$SC" '{session_id:$s,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{command:"printf \"50%% \\n é✓ \\\\ done\" | wc -c"}}' > "$GT/in2.json"; printf '\n\n' >> "$GT/in2.json"
direct=$(node "$GUARD" < "$GT/in2.json")
ok "main-chat call: the prefiltered output equals running the guard directly (the reminder)" "$([ "$PF_OUT" = "$direct" ] && [ -n "$PF_OUT" ] && echo same || echo "differs: [$PF_OUT] vs [$direct]")" "same"
# a miss must land on "run node", never on "skip a main-chat call"
SD=$(ssid); mk_in "$(jq -cn --arg s "$SD" '{session_id:$s,tool_name:"Bash",tool_input:{command:"npm test"},agent_id:""}')"; pf "$GT/in.json"
ok "agent_id empty string is a main-chat call: node runs and the guard reminds" "$(shim_calls)|$(printf %s "$PF_OUT" | warned)" "1|1"
SD=$(ssid); mk_in "$(jq -cn --arg s "$SD" '{session_id:$s,tool_name:"Bash",tool_input:{command:"npm test"},agent_id:null}')"; pf "$GT/in.json"
ok "agent_id null is a main-chat call: node runs and the guard reminds" "$(shim_calls)|$(printf %s "$PF_OUT" | warned)" "1|1"
SD=$(ssid); mk_in "$(jq -n --arg s "$SD" '{session_id:$s,tool_name:"Bash",tool_input:{command:"npm test"},agent_id:"agent-1"}')"; pf "$GT/in.json"   # pretty-printed: "agent_id": "agent-1" (space)
ok "pretty-printed subagent input misses the key form: node runs, the guard itself stays silent" "$(shim_calls)|${#PF_OUT}|$PF_RC" "1|0|0"
SD=$(ssid); mk_in "$(jq -cn --arg s "$SD" '{session_id:$s,tool_name:"Grep",tool_input:{pattern:"agent_id",path:"/home/x/repo"}}')"; pf "$GT/in.json"
ok "main-chat Grep for the text agent_id is not skipped: node runs and the guard reminds" "$(shim_calls)|$(printf %s "$PF_OUT" | warned)" "1|1"
SD=$(ssid); mk_in "$(jq -cn --arg s "$SD" '{session_id:$s,tool_name:"Bash",tool_input:{command:"cat x | grep \"\\\"agent_id\\\":\\\"abc\\\"\""}}')"; pf "$GT/in.json"
ok "main-chat Bash whose command text holds \"agent_id\":\"abc\" is not skipped (the quotes are escaped in the JSON)" "$(shim_calls)|$(printf %s "$PF_OUT" | warned)" "1|1"
: > "$GT/in.json"; pf "$GT/in.json"
ok "empty stdin: exit 0, silent (node runs and fails open)" "$PF_RC|${#PF_OUT}" "0|0"
mk_in 'not json'; pf "$GT/in.json"
ok "malformed stdin: exit 0, silent" "$PF_RC|${#PF_OUT}|$(wc -c < "$GT/pf.err" | tr -d ' ')" "0|0|0"

echo "== 10. state prefilter (win 1): once the session's warning slot is spent (.b1) or the guard is off (.off), node never starts"
# Same hooks.json command, run under /bin/sh with a stub BOSS_NODE: `-v` passes boss-node's version gate, any other call logs a line, then runs the real node.
BN="$GT/bn"; mkdir -p "$BN"; BFLAG="$GT/bn.flag"
printf '#!/bin/sh\n[ "$1" = "-v" ] && { echo v99.0.0; exit 0; }\necho call >> "%s"\nexec "%s" "$@"\n' "$BFLAG" "$REALNODE" > "$BN/stubnode"; chmod +x "$BN/stubnode"
bf(){ rm -f "$BFLAG"; PF_OUT=$(env CLAUDE_PLUGIN_ROOT="${BF_ROOT:-$CL}" BOSS_NODE="$BN/stubnode" "$@" /bin/sh -c "$PCMD" < "$GT/in.json" 2>"$GT/pf.err"); PF_RC=$?; BF_CALLS=$([ -f "$BFLAG" ] && wc -l < "$BFLAG" | tr -d ' ' || echo 0); }   # bf [env assignments...]
bmk(){ jq -cn --arg s "$1" '{session_id:$s,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{command:"npm test"}}' > "$GT/in.json"; }
ST="$ORCH_GUARD_STATE"; mkdir -p "$ST"
SE=$(ssid); bmk "$SE"; : > "$ST/$SE.b1"; bf
ok "slot spent (.b1): node never starts, exit 0, silent" "$BF_CALLS|$PF_RC|${#PF_OUT}|$(wc -c < "$GT/pf.err" | tr -d ' ')" "0|0|0|0"
SE=$(ssid); bmk "$SE"; : > "$ST/$SE.off"; bf
ok "guard off for the session (.off): node never starts" "$BF_CALLS|$PF_RC|${#PF_OUT}" "0|0|0"
SE=$(ssid); bmk "$SE"; bf
ok "no slot yet: node starts and the guard reminds (ORCH_GUARD_AFTER=1)" "$BF_CALLS|$(printf %s "$PF_OUT" | warned)" "1|1"
ok "...and that run spent the slot, so the next call skips node" "$(bf; echo "$BF_CALLS|${#PF_OUT}")" "0|0"
SE=$(ssid); : > "$ST/$SE.b1"; SF=$(ssid)
jq -cn --arg s "$SF" --arg t "$SE" '{tool_input:{command:("echo \"session_id\":\"" + $t + "\"")},session_id:$s,tool_name:"Bash"}' > "$GT/in.json"; bf
ok "a session_id key inside the command text names a spent sid, the real sid has no slot: node starts" "$BF_CALLS|$(printf %s "$PF_OUT" | warned)" "1|1"
SE=$(ssid); : > "$ST/$SE.b1"; jq -n --arg s "$SE" '{session_id:$s,tool_name:"Bash",tool_input:{command:"npm test"}}' > "$GT/in.json"; bf
ok "pretty-printed input (\"session_id\": with a space) is not parsed by sh: node starts, the guard itself stays silent" "$BF_CALLS|${#PF_OUT}|$PF_RC" "1|0|0"
SE=$(ssid); : > "$ST/$SE.b1"; jq -cn --arg s "${SE}x" '{session_id:$s,tool_name:"Bash",tool_input:{command:"npm test"}}' > "$GT/in.json"; bf
ok "a different sid that only shares a prefix with the spent one: node starts" "$BF_CALLS" "1"
bmk 'bad sid;rm' ; bf
ok "a sid with characters outside [A-Za-z0-9._-]: node starts (the guard rejects it itself)" "$BF_CALLS|$PF_RC|${#PF_OUT}" "1|0|0"
bmk 'abc'; : > "$ST/abc.b1"; bf
ok "a sid shorter than 8 (the guard's own limit): node starts" "$BF_CALLS" "1"
: > "$GT/in.json"; bf
ok "empty stdin: node starts and fails open" "$BF_CALLS|$PF_RC|${#PF_OUT}" "1|0|0"
SE=$(ssid); bmk "$SE"; mkdir -p "$GT/st2"; : > "$GT/st2/$SE.b1"; bf ORCH_GUARD_STATE="$GT/st2"
ok "ORCH_GUARD_STATE is honoured: the slot there skips node" "$BF_CALLS" "0"
SE=$(ssid); bmk "$SE"; : > "$GT/st2/$SE.b1"; bf ORCH_GUARD_STATE=
ok "ORCH_GUARD_STATE empty means unset: the slot only in st2 does not skip node (state under <BOSS_DATA>/guard-state)" "$BF_CALLS" "1"
# data dir without ORCH_GUARD_STATE: ORCH_DIR, BOSS_DATA, <config dir>/orchestrator (with runs/), <config dir>/boss-data (lib/paths.js order)
GH="$GT/gh"; mkdir -p "$GH/.claude/orchestrator/runs/x" "$GH/.claude/orchestrator/guard-state" "$GH/.claude/boss-data/guard-state"
nogs(){ env -u ORCH_GUARD_STATE -u ORCH_DIR -u BOSS_DATA -u CLAUDE_CONFIG_DIR HOME="$GH" "$@"; }
SE=$(ssid); bmk "$SE"; : > "$GH/.claude/orchestrator/guard-state/$SE.b1"; rm -f "$BFLAG"
PF_OUT=$(nogs env CLAUDE_PLUGIN_ROOT="$CL" BOSS_NODE="$BN/stubnode" /bin/sh -c "$PCMD" < "$GT/in.json" 2>/dev/null); BF_CALLS=$([ -f "$BFLAG" ] && wc -l < "$BFLAG" | tr -d ' ' || echo 0)
ok "owner layout (no ORCH_DIR, BOSS_DATA or CLAUDE_CONFIG_DIR; <HOME>/.claude/orchestrator/runs exists): its guard-state slot skips node" "$BF_CALLS" "0"
SE=$(ssid); bmk "$SE"; : > "$GH/.claude/boss-data/guard-state/$SE.b1"; rm -f "$BFLAG"
PF_OUT=$(nogs env CLAUDE_PLUGIN_ROOT="$CL" BOSS_NODE="$BN/stubnode" /bin/sh -c "$PCMD" < "$GT/in.json" 2>/dev/null); BF_CALLS=$([ -f "$BFLAG" ] && wc -l < "$BFLAG" | tr -d ' ' || echo 0)
ok "owner layout: a slot only under boss-data is not the data dir (runs/ exists in orchestrator): node starts" "$BF_CALLS" "1"
rm -rf "$GH/.claude/orchestrator"; rm -f "$BFLAG"
PF_OUT=$(nogs env CLAUDE_PLUGIN_ROOT="$CL" BOSS_NODE="$BN/stubnode" /bin/sh -c "$PCMD" < "$GT/in.json" 2>/dev/null); BF_CALLS=$([ -f "$BFLAG" ] && wc -l < "$BFLAG" | tr -d ' ' || echo 0)
ok "fresh install layout (no orchestrator/runs): <HOME>/.claude/boss-data/guard-state slot skips node" "$BF_CALLS" "0"
# a subagent still never launches node, and the helper is the only new moving part: gone, broken, or exiting 2, node runs and the hook exits 0
SE=$(ssid); jq -cn --arg s "$SE" '{session_id:$s,tool_name:"Bash",tool_input:{command:"npm test"},agent_id:"agent-9f3"}' > "$GT/in.json"; bf
ok "subagent call (agent_id) still never launches node" "$BF_CALLS|$PF_RC|${#PF_OUT}" "0|0|0"
BF_ROOT="$GT/root-nohelper"; mkdir -p "$BF_ROOT/bin"; cp "$CL/bin/boss-node" "$BF_ROOT/bin/"; for d in hooks lib wfp; do ln -s "$CL/$d" "$BF_ROOT/$d"; done
SE=$(ssid); bmk "$SE"; : > "$ST/$SE.b1"; bf
ok "boss-skip missing even though the slot is spent: node starts (fail open), exit 0, not 2" "$BF_CALLS|$PF_RC" "1|0"
printf 'if then fi ((( ;;\n' > "$BF_ROOT/bin/boss-skip"; bf
ok "boss-skip with a syntax error: node starts, exit 0, not 2" "$BF_CALLS|$PF_RC" "1|0"
printf '#!/bin/sh\nexit 2\n' > "$BF_ROOT/bin/boss-skip"; bf
ok "boss-skip exiting 2: node starts, the hook exits 0, not 2" "$BF_CALLS|$PF_RC" "1|0"
BF_ROOT=

echo "pass=$pass fail=$fail"; [ "$fail" -eq 0 ]
