#!/usr/bin/env bash
# Lint for the plugin's ten user-invoked skills: skills/{start,approve,away,stop,resume,runs,status,cost,doctor,memclean}/SKILL.md.
# The skill set is exactly those ten directories (no boss-dashboard, no boss-optimize: they are not part of this plugin).
# Per skill: frontmatter (name = dir, disable-model-invocation: true, argument-hint, description "<...>-invoked only"), the
# first body line (start: BOSS_LIB=${CLAUDE_PLUGIN_ROOT}/lib; the others: a pointer to skills/start/PROTOCOL.md), a size cap,
# no ~/.claude path, no bare $CLAUDE_PLUGIN_ROOT, no pre-plugin command name (/boss-status, /orch-resume, bare /boss),
# every /boss:<name> it mentions is one of the ten, every ${CLAUDE_PLUGIN_ROOT}/<path> it mentions exists in the plugin tree,
# no --force (outside a never/not sentence), no lease release without --run, every --run followed by <name> or <run name>, ACTIVE only on a line saying it is
# retired or never/not used, and every `node "${CLAUDE_PLUGIN_ROOT}/..."` CLI line: placeholders swapped for sample values and
# the plugin root expanded (that is what the main chat sees), then fed to the guard as a main-session Bash call (one fresh
# sid per line, expect no warning; the same line plus --sid=$WFP_SID must warn), its verb listed in that script's HELP.
# The guard probe covers manifest.js, ledger.js and wfp.js lines (the guard's housekeeping list); lib/paths.js and
# skills/status/progress.js lines are only checked for existing. --force may appear only on a line saying never/not.
# resume is the one skill that passes --sid=$WFP_SID on purpose and may take over a lease after asking the user (--force), so it
# skips the guard probe and the --force rule.
# A bad fixture proves the lint fires.
# The guard runs in place from $CL/hooks (it needs ../lib/paths.js beside it), with its warning slots in scratch.
# Usage: bash plugins/boss/test/skills-tests.sh     (exit 0 = all pass; scripts/test-all.sh runs it in a scratch HOME)
set -u
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"   # the plugin root this suite tests (a worktree runs its own code)
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
PFX="skillstest-$$-"
T=$(mktemp -d)
# a guard older than ORCH_GUARD_STATE keeps its slots next to the script: remove this run's probe sids from there too
cleanup(){ rm -f "$CL"/hooks/state/orchestrator-guard/"$PFX"* 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
export ORCH_DIR="$T/orch" ORCH_RUNS_DIR="$T/orch/runs" ORCH_PROJECTS_DIR="$T/projects" ORCH_SETTINGS="$T/settings.json" ORCH_CLAUDE_ROOT="$T/root" ORCH_GUARD_STATE="$T/guard-state"
unset WFP_SID ORCHESTRATOR_GUARD BOSS_DATA CLAUDE_CONFIG_DIR BOSS_PROGRESS_DIR
mkdir -p "$ORCH_GUARD_STATE"
GUARD="$CL/hooks/orchestrator-guard.js"
NAMES="start approve away stop resume runs status cost doctor memclean"
ssid(){ echo "$PFX$(cat /proc/sys/kernel/random/uuid 2>/dev/null || node -e 'process.stdout.write(require("crypto").randomUUID())')"; }
gwarn(){ jq -cn --arg s "$(ssid)" --arg c "$1" '{session_id:$s, hook_event_name:"PreToolUse", tool_name:"Bash", tool_input:{command:$c}}' \
  | ORCH_GUARD_AFTER=1 node "$GUARD" | jq -r '.hookSpecificOutput.additionalContext // ""' | grep -c '^\[orchestrator\]'; }
HELP_M=$(node "$CL/lib/manifest.js" help 2>&1); HELP_L=$(node "$CL/lib/ledger.js" help 2>&1)
START_FIRST='BOSS_LIB=${CLAUDE_PLUGIN_ROOT}/lib'

fm(){ awk 'NR==1 && $0!="---" {exit} NR>1 && $0=="---" {exit} NR>1' "$1"; }        # frontmatter body
body1(){ awk 'NR==1 {next} f {print; exit} $0=="---" {f=1}' "$1"; }                    # first line after the frontmatter
fmval(){ fm "$1" | sed -n "s/^$2: *//p" | head -1; }
# CLI lines: inline spans and fenced lines that start with node "${CLAUDE_PLUGIN_ROOT}/
clilines(){ { grep -o '`node "${CLAUDE_PLUGIN_ROOT}/[^`]*`' "$1" | sed 's/^`//; s/`$//'
  awk '/^```/ {f=!f; next} f && /^node "\$\{CLAUDE_PLUGIN_ROOT\}\//' "$1"; } | sort -u; }
# sample values for placeholders, then the plugin root expanded the way the skill loader does
subst(){ sed -e 's/<run name>/2026-01-01-x/g; s/<name>/2026-01-01-x/g; s/<bar>/opus/g; s/<ids>/leases/g; s/<preset>/quality/g' \
  -e 's/ N\b/ 5/g; s/=N\b/=5/g; s/…/x/g' -e "s#\${CLAUDE_PLUGIN_ROOT}#$CL#g"; }
# lint <file> <dir name> <cap bytes> <guard probe: 1|0>: one 'problem: ...' line per broken rule, nothing when clean
lint(){
  local f="$1" d="$2" cap="$3" probe="$4" l s v n p
  [ "$(fmval "$f" name)" = "$d" ] || echo "problem: name is not $d"
  [ "$(fmval "$f" disable-model-invocation)" = "true" ] || echo "problem: disable-model-invocation is not true"
  [ -n "$(fmval "$f" argument-hint)" ] || echo "problem: no argument-hint"
  fmval "$f" description | grep -q '^"\{0,1\}[A-Za-z]*-invoked only' || echo "problem: description does not begin <who>-invoked only"
  if [ "$d" = start ]; then [ "$(body1 "$f")" = "$START_FIRST" ] || echo "problem: first body line is not $START_FIRST"
    grep -q 'skills/start/PROTOCOL.md' "$f" || echo "problem: start does not point at PROTOCOL.md"
  else body1 "$f" | grep -qF '${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md' || echo "problem: first body line does not point at PROTOCOL.md"; fi
  s=$(wc -c < "$f"); [ "$s" -le "$cap" ] || echo "problem: size $s > $cap"
  grep -q '~/\.claude\|\$HOME/\.claude' "$f" && echo "problem: ~/.claude path"
  grep -qE '\$CLAUDE_PLUGIN_ROOT|\$\{?CLAUDE_PLUGIN_DATA' "$f" && echo "problem: bare \$CLAUDE_PLUGIN_ROOT"
  grep -qE '/boss-(approve|away|cost|doctor|runs|status|stop|dashboard|optimize)|/orch-resume|`/boss`' "$f" && echo "problem: pre-plugin command name"
  for n in $(grep -oE '/boss:[a-z]+' "$f" | sort -u); do case " $NAMES " in *" ${n#/boss:} "*) ;; *) echo "problem: unknown command $n";; esac; done
  if [ "$probe" = 1 ]; then grep -- '--force' "$f" | grep -viqE '\b(never|not)\b' && echo "problem: --force"; fi   # resume may take over a lease after asking
  grep -E 'lease release' "$f" | grep -vq -- '--run' && echo "problem: lease release without --run"
  grep -E 'ACTIVE' "$f" | grep -viqE '\b(never|not|retired)\b' && echo "problem: ACTIVE without never/not/retired"
  n=$(grep -oE -- '--run( <run name>| <name>| [^< ][^ ]*|[^ `]+)' "$f" | grep -cvE -- '^--run (<run name>|<name>)$'); [ "$n" = 0 ] || echo "problem: $n --run without <name>"
  for p in $(grep -o '\${CLAUDE_PLUGIN_ROOT}/[A-Za-z0-9_./-]*[A-Za-z0-9_]' "$f" | sed 's#^\${CLAUDE_PLUGIN_ROOT}/##' | sort -u); do
    [ -e "$CL/$p" ] || echo "problem: no such path in the plugin: $p"
  done
  n=0
  while IFS= read -r l; do
    [ -n "$l" ] || continue; n=$((n+1)); s=$(printf '%s' "$l" | subst)
    case "$s" in *'<'*|*'>'*) echo "problem: unknown placeholder: $l"; continue;; esac
    # the guard vets only the engine CLIs it knows (manifest, ledger, wfp); lib/paths.js and skills/status/progress.js are
    # read-only helpers that run once per skill, so they get the existence check above and no probe
    case "$s" in "node \"$CL/lib/manifest.js\""*|"node \"$CL/lib/ledger.js\""*|"node \"$CL/wfp/wfp.js\""*) pr=1;; *) pr=0;; esac
    if [ "$probe" = 1 ] && [ "$pr" = 1 ]; then
      [ "$(gwarn "$s")" = 0 ] || echo "problem: guard warns: $l"
      [ "$(gwarn "$s --sid=\$WFP_SID")" = 1 ] || echo "problem: negative control silent: $l"
    fi
    v=$(printf '%s' "$s" | sed -nE "s#^node \"$CL/lib/(manifest|ledger)\.js\" ([a-z]+).*#\1 \2#p")
    if [ -n "$v" ]; then
      if [ "${v% *}" = manifest ]; then printf '%s\n' "$HELP_M"; else printf '%s\n' "$HELP_L"; fi | grep -qE "^  ${v#* }( |$)" || echo "problem: verb not in HELP: $v"
    fi
  done < <(clilines "$f")
  [ "$n" -gt 0 ] || [ "$d" = away ] || [ "$d" = memclean ] || echo "problem: no CLI line"
}

echo "== 1. the skill set is the ten orchestrator skills plus council (council has its own gate and is not linted here)"
ok "skills/ holds exactly the ten names plus council" "$(ls "$CL/skills" | tr '\n' ' ')" "$(echo $NAMES council | tr ' ' '\n' | sort | tr '\n' ' ')"
for d in boss-dashboard boss-optimize boss; do ok "no skills/$d" "$([ -e "$CL/skills/$d" ] && echo present || echo absent)" "absent"; done

echo "== 2. lint each skill"
k=0
for d in $NAMES; do
  f="$CL/skills/$d/SKILL.md"
  ok "$d: SKILL.md present" "$([ -f "$f" ] && echo y)" "y"
  [ -f "$f" ] || continue; k=$((k+1))
  cap=6144; [ "$d" = start ] && cap=10240; [ "$d" = resume ] && cap=16384   # start and resume carry the longest procedures
  probe=1; [ "$d" = resume ] && probe=0
  ok "$d: lint clean ($(clilines "$f" | grep -c .) CLI lines, $(wc -c < "$f") bytes, cap $cap)" "$(lint "$f" "$d" "$cap" "$probe")" ""
done
echo "skills checked: $k"

echo "== 3. resume re-attaches with --sid; its away bullet points at the away skill"
R="$CL/skills/resume/SKILL.md"
ok "resume name" "$(fmval "$R" name)" "resume"
ok "resume disable-model-invocation" "$(fmval "$R" disable-model-invocation)" "true"
if grep -q 'Mode: away' "$R"; then
  ok "resume away bullet points at skills/away/SKILL.md" "$(grep 'Mode: away' "$R" | grep -c 'skills/away/SKILL.md')" "1"
else echo "SKIP resume away bullet (not present)"; fi

echo "== 4. lint fires on a bad fixture"
B="$T/skills/bad"; mkdir -p "$B"
cat > "$B/SKILL.md" <<'EOF'
---
name: boss-bad
description: "Shows things."
disable-model-invocation: true
argument-hint: "[run]"
---
Main chat, user-invoked. Follow the protocol.
Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" lease release --sid=$WFP_SID --force`.
Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" status --run R`.
Run `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" frobnicate`.
Run `node "${CLAUDE_PLUGIN_ROOT}/lib/ledger.js" sync <what>`.
Read ACTIVE first, from ~/.claude/orchestrator, via $CLAUDE_PLUGIN_ROOT, then `/boss-status`, `/orch-resume` and `/boss:frob`.
See ${CLAUDE_PLUGIN_ROOT}/skills/start/NOPE.md.
EOF
P=$(lint "$B/SKILL.md" bad 6144 1)
for w in 'name is not bad' 'description does not begin' 'first body line does not point' '^problem: --force$' 'lease release without --run' 'ACTIVE without' '--run without' \
  '~/.claude path' 'bare \$CLAUDE_PLUGIN_ROOT' 'pre-plugin command name' 'unknown command /boss:frob' 'no such path in the plugin: skills/start/NOPE.md' \
  'unknown placeholder: node .*ledger.js. sync <what>' 'guard warns: node .*lease release' 'verb not in HELP: manifest frobnicate'; do
  ok "bad fixture flags: $w" "$(printf '%s\n' "$P" | grep -cE -- "$w")" "1"
done
ok "bad fixture over a 100-byte cap flags size" "$(lint "$B/SKILL.md" bad 100 0 | grep -c 'size')" "1"
mkdir -p "$T/skills/start"; printf -- '---\nname: start\ndescription: "User-invoked only: x"\ndisable-model-invocation: true\nargument-hint: "x"\n---\nHello\nRun `node "${CLAUDE_PLUGIN_ROOT}/lib/manifest.js" status`\n' > "$T/skills/start/SKILL.md"
ok "start without BOSS_LIB first line is flagged" "$(lint "$T/skills/start/SKILL.md" start 6144 0 | grep -c 'first body line is not BOSS_LIB')" "1"
ok "a skill with no CLI line is flagged (except away)" "$(printf -- '---\nname: x\ndescription: "User-invoked only: x"\ndisable-model-invocation: true\nargument-hint: "x"\n---\n%s\n' 'Main chat. ${CLAUDE_PLUGIN_ROOT}/skills/start/PROTOCOL.md' > "$T/skills/x.md"; lint "$T/skills/x.md" x 6144 0 | grep -c 'no CLI line')" "1"

echo "== 5. no probe state outside scratch"
ok "no probe sid in the plugin tree, the guard's scratch slots aside" "$(ls "$CL/hooks/state/orchestrator-guard" 2>/dev/null | grep -c "^$PFX")" "0"

echo "== 6. boss-memclean takes its data dir from lib/paths.js (Linux and WSL only; elsewhere it exits 2 with a message)"
if [ "$(uname -s)" = Linux ]; then
MC="$T/memclean-data"; mkdir -p "$MC"
env -u ORCH_DIR -u ORCH_RUNS_DIR BOSS_DATA="$MC" bash "$CL/bin/boss-memclean" > /dev/null 2>&1
ok "memclean state lands under BOSS_DATA/memclean" "$([ -d "$MC/memclean" ] && echo y)" "y"
MC2="$T/memclean-cfg"; mkdir -p "$MC2"
env -u ORCH_DIR -u ORCH_RUNS_DIR CLAUDE_CONFIG_DIR="$MC2" bash "$CL/bin/boss-memclean" > /dev/null 2>&1
ok "memclean under a fresh config dir uses <config>/boss-data/memclean" "$([ -d "$MC2/boss-data/memclean" ] && echo y)" "y"
else
  echo "SKIP boss-memclean data-dir checks: not Linux"
fi
# a uname that says Darwin (first on PATH): exits 2 with the message, before touching anything
UN="$T/uname-darwin"; mkdir -p "$UN"; printf '#!/bin/sh\necho Darwin\n' > "$UN/uname"; chmod +x "$UN/uname"
MC3="$T/memclean-darwin"; mkdir -p "$MC3"
PATH="$UN:$PATH" BOSS_DATA="$MC3" bash "$CL/bin/boss-memclean" > "$T/mc3.out" 2> "$T/mc3.err"; rc=$?
ok "memclean on a non-Linux uname exits 2" "$rc" "2"
ok "memclean on a non-Linux uname says Linux and WSL only" "$(grep -c 'Linux and WSL only' "$T/mc3.err")" "1"
ok "memclean on a non-Linux uname creates no state" "$([ -e "$MC3/memclean" ] && echo made || echo none)" "none"

echo "== 7. PROTOCOL.md keeps a read-on-demand pointer for each section moved to its own file; the text lives in the new file"
PR="$CL/skills/start/PROTOCOL.md"
for pair in "Escalation by cause:ESCALATION.md" "Lease, resume and rollover:LEASE-ROLLOVER.md" "Progress view:PROGRESS-VIEW.md"; do
  h="${pair%%:*}"; f="${pair##*:}"
  ok "PROTOCOL.md has the '## $h' heading" "$(grep -cxF "## $h" "$PR")" "1"
  ok "'## $h' pointer names skills/start/$f" "$(awk -v h="## $h" '$0==h {f=1; next} f && /^## / {exit} f' "$PR" | grep -cF "\$BOSS_LIB/../skills/start/$f")" "1"
  ok "'## $h' pointer does not use the unexpanded CLAUDE_PLUGIN_ROOT form" "$(awk -v h="## $h" '$0==h {f=1; next} f && /^## / {exit} f' "$PR" | grep -c 'CLAUDE_PLUGIN_ROOT')" "0"
  ok "skills/start/$f exists" "$([ -s "$CL/skills/start/$f" ] && echo y)" "y"
done
for pair in 'ESCALATION.md|wfp.js" escalate <role>' 'ESCALATION.md|--cause review-findings' 'ESCALATION.md|--cause failed' 'ESCALATION.md|--cause tooling' 'ESCALATION.md|owner-tune' \
  'LEASE-ROLLOVER.md|lease acquire --run' 'LEASE-ROLLOVER.md|lease release --sid' 'LEASE-ROLLOVER.md|manifest.js" reconcile --run' 'LEASE-ROLLOVER.md|<run>/HANDOFF.md' 'LEASE-ROLLOVER.md|A rollover is never a reason' \
  'PROGRESS-VIEW.md|progress.js" <run>' 'PROGRESS-VIEW.md|reproduce its output verbatim' 'PROGRESS-VIEW.md|Never invent an activity'; do
  ok "${pair%%|*} holds: ${pair#*|}" "$(grep -cF -- "${pair#*|}" "$CL/skills/start/${pair%%|*}" | sed 's/^[1-9][0-9]*$/found/')" "found"
done
for t in 'wfp.js" escalate <role>' 'Findings on a sound approach' 'lease acquire --run <run> --sid' 'reproduce its output verbatim' 'Keep the formatter'; do
  ok "PROTOCOL.md no longer carries moved text: $t" "$(grep -cF -- "$t" "$PR")" "0"
done
ok "lease pointer triggers on the run ending" "$(awk '$0=="## Lease, resume and rollover" {f=1; next} f && /^## / {exit} f' "$PR" | grep -c 'when the run ends')" "1"
ok "PROTOCOL.md still carries Review sizing" "$(grep -cxF '### Review sizing (configurable default)' "$PR")" "1"

echo "pass=$pass fail=$fail"
[ "$fail" = 0 ]
