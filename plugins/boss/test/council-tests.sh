#!/usr/bin/env bash
# Council tests. Default: the fast half, python3 -Es council/tests/run_suite.py --fast, from the plugin copy (about 2 minutes,
# stub CLI, no network and no paid calls). This is what scripts/test-all.sh and CI run.
#
#   bash test/council-tests.sh              fast half (as before)
#   bash test/council-tests.sh --full       the WHOLE suite (fast + slow: stub processes through the supervisor, hedge, fallback, replay),
#                                           or COUNCIL_TESTS_FULL=1. Not part of test-all: run it by hand when you change the runner.
#   bash test/council-tests.sh --full --guard-only    set the full-mode environment up, run the guard, run no test (exit 0 = guard holds)
#
# Full mode makes a real provider call impossible, not only unlikely, and checks that BEFORE any test runs:
#   - HOME, config, progress and data dirs are scratch (always, in both modes), so no CLI finds a login there;
#   - every provider API key / token variable is unset (by name), and the guard refuses if one is still set;
#   - CODEX_HOME and XDG_CONFIG_HOME are unset, and the guard refuses if either points outside the scratch folder (or climbs out with ..);
#   - a shim stands in for every provider CLI (claude codex agy qwen) first on PATH, inside the scratch folder; each shim only
#     records the call and exits 97. The guard refuses unless `command -v <cli>` is that shim for every name, and unless every
#     CLI name the engine defaults to (the `pc.get("bin", "<cli>")` calls in councillib) is on the list;
#   - the council tests' own network block (council/tests/__init__.py) refuses non-loopback connects in the Python process.
# A refusal prints which check failed and exits 1 without running a test.
# Prints "pass=N fail=M" last, which scripts/test-all.sh reads. Needs python3 3.11+ (tomllib). Full mode first runs the guard's own small
# self-test (a missing shim, a real CLI ahead of the shim, a set key, a CODEX_HOME or XDG_CONFIG_HOME outside the scratch folder, an uncovered engine CLI each make it refuse); its cases count in
# pass=/fail=, and a failing self-test stops the run before any test. The default (fast) mode runs none of this.
# Usage: bash test/council-tests.sh     (exit 0 = all pass; always runs in a scratch HOME, progress dir and state dirs, see below)
set -u

# ---- the guard (functions first, so the self-test can source this file and call them without running anything) ----
COUNCIL_PROVIDER_CLIS="claude codex agy qwen"
# Names checked in addition to every variable whose name contains _API_KEY.
COUNCIL_KEY_VARS="ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY GOOGLE_API_KEY OPENROUTER_API_KEY QWEN_API_KEY DASHSCOPE_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN"

council_key_var_names(){ { printf '%s\n' $COUNCIL_KEY_VARS; compgen -v | grep '_API_KEY'; } | sort -u; }

# council_make_shims <dir>: one executable shim per provider CLI. A shim never runs anything: it logs the call and exits 97.
council_make_shims(){
  local d="$1" n
  mkdir -p "$d" || return 1
  for n in $COUNCIL_PROVIDER_CLIS; do
    printf '#!/bin/sh\necho "council-tests shim: provider CLI %s was called; a real provider call is impossible here" >&2\necho "%s $*" >> "%s/shim-calls.log"\nexit 97\n' "$n" "$n" "$d" > "$d/$n" || return 1
    chmod +x "$d/$n" || return 1
  done
}

# council_full_guard <shim dir> <scratch home> <plugin root> [<scratch folder>]: 0 = every check holds; otherwise prints each failed
# check and returns 1. The scratch folder defaults to the parent of the scratch home.
council_full_guard(){
  local shimdir="$1" home="$2" root="$3" scratch="${4:-${2%/*}}" bad=0 n p v bins
  for n in $COUNCIL_PROVIDER_CLIS; do
    p=$(command -v "$n" 2>/dev/null) || p=""
    if [ -z "$p" ]; then echo "council-tests guard: provider CLI '$n' is not shimmed (command -v finds nothing)" >&2; bad=1
    elif [ "$p" != "$shimdir/$n" ] || [ ! -x "$shimdir/$n" ]; then echo "council-tests guard: provider CLI '$n' resolves to $p, not to the shim $shimdir/$n" >&2; bad=1; fi
  done
  while IFS= read -r v; do
    [ -n "$v" ] || continue
    if [ -n "${!v+x}" ]; then echo "council-tests guard: provider key variable $v is set (value not shown)" >&2; bad=1; fi
  done < <(council_key_var_names)
  # an empty scratch folder would turn the "$scratch"/* pattern below into /*, which accepts any absolute path: refuse it up front
  [ -n "$scratch" ] || { echo "council-tests guard: the scratch folder is empty (no scratch argument, and the scratch home $home has no parent folder), so no path can be checked against it" >&2; bad=1; }
  # a real Codex or XDG config must not be readable: unset, or inside the scratch folder (a path with ".." in it does not count)
  for v in CODEX_HOME XDG_CONFIG_HOME; do
    [ -n "${!v+x}" ] || continue
    case "${!v}" in
      */..|*/../*|..|../*) echo "council-tests guard: $v has a '..' in it, so it may point outside the scratch folder (value not shown)" >&2; bad=1;;
      "$scratch"|"$scratch"/*) ;;
      *) echo "council-tests guard: $v points outside the scratch folder $scratch (value not shown)" >&2; bad=1;;
    esac
  done
  [ "${HOME:-}" = "$home" ] || { echo "council-tests guard: HOME is ${HOME:-unset}, not the scratch home $home" >&2; bad=1; }
  # the engine's own list of CLIs: a provider added later without a shim must stop the run, not slip through
  bins=$(cat "$root"/council/councillib/*.py 2>/dev/null | grep -oE 'pc\.get\("bin", "[A-Za-z0-9_.-]+"\)' | sed -E 's/^pc\.get\("bin", "(.*)"\)$/\1/' | sort -u)
  [ -n "$bins" ] || { echo "council-tests guard: found no provider CLI names in $root/council/councillib (cannot check the shim list against the engine)" >&2; bad=1; }
  for n in $bins; do
    case " $COUNCIL_PROVIDER_CLIS " in *" $n "*) ;; *) echo "council-tests guard: the engine can run provider CLI '$n', which has no shim (add it to COUNCIL_PROVIDER_CLIS)" >&2; bad=1;; esac
  done
  [ "$bad" = 0 ]
}

# council_guard_selftest: runs the guard against a fake PATH / env / engine tree; sets gpass and gfail (needs SCRATCH, CL, SELF, HOME).
council_guard_selftest(){
  gpass=0; gfail=0
  gcheck(){ if [ "$2" = "$3" ]; then gpass=$((gpass+1)); echo "PASS $1"; else gfail=$((gfail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
  GT="$SCRATCH/guard-selftest"; mkdir -p "$GT/real" "$GT/fakeroot/council/councillib"
  printf '#!/bin/sh\nexit 0\n' > "$GT/real/claude"; chmod +x "$GT/real/claude"
  printf 'x = pc.get("bin", "claude"); y = pc.get("bin", "codex"); z = pc.get("bin", "agy"); w = pc.get("bin", "qwen")\n' > "$GT/fakeroot/council/councillib/p.py"
  council_make_shims "$GT/shims"
  gsrc="$(cd "$(dirname "$SELF")" && pwd)/$(basename "$SELF")"
  gcase(){   # gcase <extra PATH dir or -> <env assignments, space separated or -> <engine root> ; prints "rc|stderr"
    local pre="$1" envs="$2" eroot="$3" out rc
    out=$( ( COUNCIL_TESTS_SOURCE_ONLY=1 . "$gsrc"
             PATH="$GT/shims:/usr/bin:/bin"; [ "$pre" = - ] || PATH="$pre:$PATH"
             for kv in $envs; do [ "$kv" = - ] || export "$kv"; done
             council_full_guard "$GT/shims" "$HOME" "$eroot" ) 2>&1 ); rc=$?
    echo "$rc|$out"
  }
  r=$(gcase - - "$GT/fakeroot"); gcheck "guard: all shims first, no key set, engine list covered -> holds" "${r%%|*}" "0"
  rm "$GT/shims/qwen"; r=$(gcase - - "$GT/fakeroot"); gcheck "guard: a missing shim refuses and names the CLI" "${r%%|*}|$(echo "$r" | grep -c "'qwen'")" "1|1"
  council_make_shims "$GT/shims"
  r=$(gcase "$GT/real" - "$GT/fakeroot"); gcheck "guard: a real CLI ahead of the shim refuses and names the CLI" "${r%%|*}|$(echo "$r" | grep -c "'claude' resolves to $GT/real/claude")" "1|1"
  r=$(gcase - OPENROUTER_API_KEY=hush-value-1 "$GT/fakeroot"); gcheck "guard: a set provider key refuses, names the variable and never shows the value" "${r%%|*}|$(echo "$r" | grep -c 'OPENROUTER_API_KEY is set')|$(echo "$r" | grep -c 'hush-value-1')" "1|1|0"
  r=$(gcase - ZZ_API_KEY_PERSONAL=hush-value-2 "$GT/fakeroot"); gcheck "guard: any *_API_KEY* variable counts, not only the known names" "${r%%|*}|$(echo "$r" | grep -c 'ZZ_API_KEY_PERSONAL is set')" "1|1"
  printf 'q = pc.get("bin", "newcli")\n' > "$GT/fakeroot/council/councillib/q.py"
  r=$(gcase - - "$GT/fakeroot"); gcheck "guard: an engine CLI with no shim refuses" "${r%%|*}|$(echo "$r" | grep -c "'newcli', which has no shim")" "1|1"
  rm "$GT/fakeroot/council/councillib/q.py"
  r=$(gcase - CODEX_HOME=/somewhere/real-codex-cfg "$GT/fakeroot"); gcheck "guard: a CODEX_HOME outside the scratch folder refuses, names it and never shows the value" "${r%%|*}|$(echo "$r" | grep -c 'CODEX_HOME points outside')|$(echo "$r" | grep -c 'real-codex-cfg')" "1|1|0"
  r=$(gcase - XDG_CONFIG_HOME=/somewhere/real-xdg-cfg "$GT/fakeroot"); gcheck "guard: an XDG_CONFIG_HOME outside the scratch folder refuses, names it and never shows the value" "${r%%|*}|$(echo "$r" | grep -c 'XDG_CONFIG_HOME points outside')|$(echo "$r" | grep -c 'real-xdg-cfg')" "1|1|0"
  r=$(gcase - "CODEX_HOME=$SCRATCH/../escape" "$GT/fakeroot"); gcheck "guard: a CODEX_HOME that climbs out of the scratch folder with .. refuses" "${r%%|*}|$(echo "$r" | grep -c 'CODEX_HOME has a')" "1|1"
  r=$(gcase - "CODEX_HOME=$SCRATCH/home/.codex XDG_CONFIG_HOME=$SCRATCH/home/.config" "$GT/fakeroot"); gcheck "guard: both inside the scratch folder hold" "${r%%|*}" "0"
  r=$(gcase - CODEX_HOME= "$GT/fakeroot"); gcheck "guard: a set but empty CODEX_HOME refuses" "${r%%|*}" "1"
  r=$(gcase - "HOME=/scratchhome CODEX_HOME=/etc" "$GT/fakeroot"); gcheck "guard: an empty scratch folder (home with no parent) refuses, names the cause, and does not let CODEX_HOME=/etc through" "${r%%|*}|$(echo "$r" | grep -c 'the scratch folder is empty')" "1|1"
  r=$(gcase - "HOME=/scratchhome" "$GT/fakeroot"); gcheck "guard: an empty scratch folder refuses even with no CODEX_HOME set" "${r%%|*}" "1"
  r=$(gcase - - "$CL"); gcheck "guard: this tree's engine names no provider CLI outside the shim list" "${r%%|*}" "0"
}

if [ -n "${COUNCIL_TESTS_SOURCE_ONLY:-}" ]; then return 0; fi    # sourced by the self-test (it sets this): functions only

# ---- main ----
SELF="${BASH_SOURCE[0]}"
FULL=0; GUARD_ONLY=0
case "${COUNCIL_TESTS_FULL:-}" in ''|0) ;; *) FULL=1;; esac
for a in "$@"; do
  case "$a" in
    --full) FULL=1;;
    --guard-only) GUARD_ONLY=1;;
    -h|--help) sed -n '2,22p' "$SELF" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "council-tests: unknown argument $a" >&2; exit 2;;
  esac
done
[ "$GUARD_ONLY" = 0 ] || [ "$FULL" = 1 ] || { echo "council-tests: --guard-only needs --full" >&2; exit 2; }
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "$SELF")/.." && pwd)}"; echo "code under test: $CL"   # the plugin root this suite tests (a worktree runs its own code)
if ! python3 -c 'import sys, tomllib; sys.exit(0)' 2>/dev/null; then
  echo "FAIL council needs python3 3.11+ (tomllib)"; echo "pass=0 fail=1"; exit 1
fi
# Scratch environment, ALWAYS: values are overridden, never defaulted, so nothing the caller exported reaches the tests. A Claude
# session exports BOSS_PROGRESS_DIR (the owner's real progress dir) and HOME is real, which let a run write fake runs into
# ~/.claude/boss/runs/council and ~/.claude-council. Removed on exit (the trap below).
SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/boss-council-env.XXXXXX") || exit 1
SCRATCH=$(cd "$SCRATCH" && pwd -P)          # canonical (macOS TMPDIR is a symlinked path with a trailing slash)
mkdir -p "$SCRATCH/home/.claude" "$SCRATCH/progress" "$SCRATCH/orch" "$SCRATCH/data" "$SCRATCH/wfp" "$SCRATCH/tmp"
unset BOSS_LIB BOSS_PROGRESS_LIB BOSS_ANALYTICS_LIB BOSS_NODE BOSS_ORCH_RUNS BOSS_COUNCIL_RUNS BOSS_SESSIONS_DIR BOSS_WFP_STATE \
      BOSS_INBOX_DIR BOSS_PROJECTS_DIR BOSS_METER_DIR WFP_SID CLAUDE_ENV_FILE CLAUDE_PLUGIN_ROOT CLAUDE_PLUGIN_DATA \
      ORCHESTRATOR_GUARD ORCH_RUNS_DIR ORCH_PROJECTS_DIR ORCH_SESSIONS_DIR ORCH_METRICS_DB ORCH_SETTINGS ORCH_CLAUDE_ROOT \
      ORCH_PLUGIN_ROOT ORCH_WFP_STATE ORCH_WFP_JS WFP_SRC COUNCIL_CONFIG CLAUDE_CODE_SESSION_ID
while IFS= read -r v; do unset "$v"; done < <(compgen -v | grep '_API_KEY')    # no provider key reaches a test (by name; no value read)
export HOME="$SCRATCH/home" CLAUDE_CONFIG_DIR="$SCRATCH/home/.claude" BOSS_PROGRESS_DIR="$SCRATCH/progress" \
       BOSS_DATA="$SCRATCH/data" ORCH_DIR="$SCRATCH/orch" WFP_STATE_DIR="$SCRATCH/wfp" TMPDIR="$SCRATCH/tmp"
LOG="$SCRATCH/council-tests.log"
trap 'rm -rf "$SCRATCH"; find "$CL/council" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null' EXIT    # leave no bytecode in the plugin tree

gpass=0; gfail=0
if [ "$FULL" = 1 ]; then
  while IFS= read -r v; do unset "$v"; done < <(council_key_var_names)    # full mode: also the named tokens, by name
  unset CODEX_HOME XDG_CONFIG_HOME     # full mode: no real Codex or XDG config is read (HOME is scratch too); the guard checks it
  council_guard_selftest          # the guard must refuse what it should, before it is trusted with the real checks
  if [ "$gfail" != 0 ]; then echo "council-tests: full mode REFUSED: the guard's own self-test failed, no test was run"; echo "pass=$gpass fail=$gfail"; exit 1; fi
  SHIMS="$SCRATCH/shims"
  council_make_shims "$SHIMS" || { echo "council-tests: cannot make the provider CLI shims"; echo "pass=0 fail=1"; exit 1; }
  export PATH="$SHIMS:$PATH"; hash -r
  if ! council_full_guard "$SHIMS" "$SCRATCH/home" "$CL" "$SCRATCH"; then
    echo "council-tests: full mode REFUSED: a check above failed, no test was run"; echo "pass=0 fail=1"; exit 1
  fi
  echo "council-tests: full mode guard holds (${COUNCIL_PROVIDER_CLIS// /, } shimmed in $SHIMS; no provider key variable set; no CODEX_HOME or XDG_CONFIG_HOME outside the scratch folder; scratch HOME)"
  [ "$GUARD_ONLY" = 0 ] || { echo "pass=$gpass fail=0"; exit 0; }
  SUITE_ARGS=(--timeout "${COUNCIL_TESTS_TIMEOUT:-1800}")
else
  SUITE_ARGS=(--fast)
fi
python3 -EsB "$CL/council/tests/run_suite.py" "${SUITE_ARGS[@]}" > "$LOG" 2>&1; rc=$?
if [ "$rc" = 0 ]; then tail -n 40 "$LOG"; else tail -n 600 "$LOG"; fi    # a failing run prints its tracebacks, not just the verdict line
if [ "$FULL" = 1 ]; then
  calls=0; [ -f "$SHIMS/shim-calls.log" ] && calls=$(wc -l < "$SHIMS/shim-calls.log" | tr -d ' ')
  echo "council-tests: full mode: $calls provider CLI shim call(s) during the run$([ "$calls" = 0 ] || echo ": $(cut -d' ' -f1 "$SHIMS/shim-calls.log" | sort | uniq -c | tr -s ' ' | tr '\n' ';')")"
fi
ran=$(sed -n 's/^Ran \([0-9][0-9]*\) tests.*/\1/p' "$LOG" | tail -1); ran=${ran:-0}
verdict=$(sed -n 's/^FAILED (\(.*\))/\1/p;s/^OK (\(.*\))/\1/p' "$LOG" | tail -1)
bad=$(printf '%s\n' "$verdict" | grep -oE '(failures|errors|unexpected successes)=[0-9]+' | grep -oE '[0-9]+' | awk '{s+=$1} END {print s+0}'); bad=${bad:-0}    # skipped tests are not failures
skipped=$(printf '%s\n' "$verdict" | sed -n 's/.*skipped=\([0-9][0-9]*\).*/\1/p'); skipped=${skipped:-0}
if [ "$rc" != 0 ] && [ "$bad" = 0 ]; then bad=1; fi            # a timeout or a crash with no unittest verdict still fails
[ "$ran" -gt 0 ] || { [ "$bad" -gt 0 ] || bad=1; }

pass_py=$((ran - bad - skipped)); [ "$ran" -gt 0 ] || pass_py=0
echo "pass=$((pass_py + gpass)) fail=$((bad + gfail))"; [ "$skipped" = 0 ] || echo "council: $skipped test(s) skipped, each with its reason in the SKIP lines above"
[ "$rc" = 0 ] && [ "$bad" = 0 ] && [ "$gfail" = 0 ]
