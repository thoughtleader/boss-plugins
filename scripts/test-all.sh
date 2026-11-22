#!/usr/bin/env bash
# Run every test suite of the boss plugin, each in its own scratch environment.
#
#   scripts/test-all.sh                 run all suites, print one PASS/FAIL line each, exit 1 if any failed
#   scripts/test-all.sh --list          print the suite paths (relative to plugins/boss), one per line, and exit 0
#   scripts/test-all.sh <text>...       run only the suites whose path contains one of the given texts
#   scripts/test-all.sh -v | --verbose  accepted, no effect (a failing suite always prints all its FAIL lines and the last 80 lines of its output)
#
# Suites: plugins/boss/test/*.sh, plugins/boss/test/*.test.js (run with `node --test`) and plugins/boss/wfp/fixtures/run.sh.
# Each suite gets a fresh scratch HOME, ORCH_DIR and BOSS_DATA, so nothing touches the real config or data dir.
# CLAUDE_CONFIG_DIR and BOSS_PROGRESS_DIR are cleared, not set: the config dir and progress root then default to
# <scratch HOME>/.claude and <scratch HOME>/.claude/boss (still scratch), and the gate and hook suites assert those defaults. Session and test-override variables from the calling
# shell are cleared first.
# Env: TEST_ALL_TIMEOUT=<seconds per suite> (default 600; ignored when no timeout command exists).
#      TEST_ALL_TAIL=<lines> how much of a failing suite's output to print after its failure lines (default 80).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN="$ROOT/plugins/boss"
LIST=0; VERBOSE=0; FILTERS=()
for a in "$@"; do
  case "$a" in
    --list) LIST=1;;
    -v|--verbose) VERBOSE=1;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0;;
    -*) echo "test-all: unknown flag $a" >&2; exit 2;;
    *) FILTERS+=("$a");;
  esac
done

# discover: test/*.test.js and test/*.sh (sorted), then the wfp fixture runner; paths relative to plugins/boss
suites(){
  local f
  for f in "$PLUGIN"/test/*.sh "$PLUGIN"/test/*.test.js "$PLUGIN"/wfp/fixtures/run.sh; do
    [ -f "$f" ] && echo "${f#"$PLUGIN"/}"
  done | sort
}
selected(){
  local s f
  while IFS= read -r s; do
    if [ "${#FILTERS[@]}" -eq 0 ]; then echo "$s"; continue; fi
    for f in "${FILTERS[@]}"; do case "$s" in *"$f"*) echo "$s"; break;; esac; done
  done < <(suites)
}

if [ "$LIST" = 1 ]; then selected; exit 0; fi

# No provider key reaches a suite: every *_API_KEY* variable (also *_API_KEY_PERSONAL) is unset by name (a value is never read or printed).
while IFS= read -r v; do unset "$v"; done < <(compgen -v | grep '_API_KEY')

TMO=""
if command -v timeout >/dev/null 2>&1; then TMO="timeout ${TEST_ALL_TIMEOUT:-600}"
elif command -v gtimeout >/dev/null 2>&1; then TMO="gtimeout ${TEST_ALL_TIMEOUT:-600}"; fi

total=0; failed=0; FAILED=()
START=$(date +%s)
while IFS= read -r s; do
  [ -n "$s" ] || continue
  total=$((total+1))
  S=$(mktemp -d "${TMPDIR:-/tmp}/boss-test-all.XXXXXX")
  # the canonical path: macOS TMPDIR is /var/folders/../T/ (a symlink into /private, and a trailing slash that mktemp doubles), and the
  # suites compare paths they built with paths git, node and the plugin print after resolving them
  S=$(cd "$S" && pwd -P)
  mkdir -p "$S/home/.claude" "$S/orch" "$S/data"
  # a neutral git identity in the scratch HOME's global config (like a CI runner would need); suites' own -c settings still win
  printf '[user]\n\tname = boss-test\n\temail = boss-test@example.invalid\n' > "$S/home/.gitconfig"
  case "$s" in *.test.js) cmd=(node --test "$PLUGIN/$s");; *) cmd=(bash "$PLUGIN/$s");; esac
  t0=$(date +%s)
  # env -u: no session, runtime or test-override variable from the caller leaks in
  env -u WFP_SID -u WFP_STATE_DIR -u CLAUDE_ENV_FILE -u CLAUDE_PLUGIN_ROOT -u CLAUDE_PLUGIN_DATA -u BOSS_NODE -u BOSS_LIB \
      -u CLAUDE_CONFIG_DIR -u BOSS_PROGRESS_DIR -u BOSS_ANALYTICS_LIB -u ORCHESTRATOR_GUARD -u ORCH_TEST_ROOT -u ORCH_RUNS_DIR -u ORCH_PROJECTS_DIR -u ORCH_SESSIONS_DIR \
      -u ORCH_METRICS_DB -u ORCH_SETTINGS -u ORCH_CLAUDE_ROOT -u ORCH_PLUGIN_ROOT -u ORCH_WFP_STATE -u ORCH_WFP_JS -u WFP_SRC \
      HOME="$S/home" ORCH_DIR="$S/orch" BOSS_DATA="$S/data" \
      TMPDIR="$S" \
      $TMO "${cmd[@]}" > "$S/out.log" 2>&1 < /dev/null
  rc=$?
  dt=$(( $(date +%s) - t0 ))
  # counts: shell suites print "pass=N fail=M"; node --test prints "ℹ pass N" / "ℹ fail M" (or "# pass N" in TAP)
  counts=$(grep -E '^pass=[0-9]+ fail=[0-9]+' "$S/out.log" | tail -1 | sed -E 's/^pass=([0-9]+) fail=([0-9]+).*/\1 pass, \2 fail/')
  if [ -z "$counts" ]; then
    p=$(sed -n 's/^[# ℹ]*pass \([0-9][0-9]*\).*/\1/p' "$S/out.log" | tail -1); f=$(sed -n 's/^[# ℹ]*fail \([0-9][0-9]*\).*/\1/p' "$S/out.log" | tail -1)
    [ -n "$p" ] && counts="$p pass, ${f:-0} fail"
  fi
  [ -n "$counts" ] || counts="no counts printed"
  # skips are never silent: node prints "﹣ name # reason" (and "ℹ skipped N"), shell suites print "SKIP reason"
  skips=$(grep -E '^[ ]*(﹣|SKIP )' "$S/out.log" | head -20)
  if [ -n "$skips" ]; then nskip=$(printf '%s\n' "$skips" | grep -c .); counts="$counts, $nskip skipped"; fi
  if [ "$rc" = 0 ]; then
    printf 'PASS  %-34s %s (%ss)\n' "$s" "$counts" "$dt"
    [ -n "$skips" ] && printf '%s\n' "$skips" | sed 's/^ */        /'
  else
    failed=$((failed+1)); FAILED+=("$s")
    why="exit $rc"; [ "$rc" = 124 ] && why="timeout"
    printf 'FAIL  %-34s %s, %s (%ss)\n' "$s" "$counts" "$why" "$dt"
    # every failure line (a cap of 1000), then the last 80 lines of the suite's output, so a CI log alone says what broke
    # (-v / --verbose is kept for compatibility; the full list is now the default)
    nfail=$(grep -cE '^FAIL[: ]|^ERROR: |^not ok |^# Subtest.*fail|^ +not ok ' "$S/out.log")
    { grep -E '^FAIL[: ]|^ERROR: |^not ok |^# Subtest.*fail|^ +not ok ' "$S/out.log" | head -1000
      echo "--- last ${TEST_ALL_TAIL:-80} lines of $s ($nfail failure line(s) above) ---"; tail -n "${TEST_ALL_TAIL:-80}" "$S/out.log"; } | sed 's/^/        /'
  fi
  rm -rf "$S"
done < <(selected)

dur=$(( $(date +%s) - START ))
echo "suites: $total, passed: $((total-failed)), failed: $failed (${dur}s)"
if [ "$total" -eq 0 ]; then echo "test-all: no suite matched"; exit 2; fi
if [ "$failed" -gt 0 ]; then echo "failed: ${FAILED[*]}"; exit 1; fi
exit 0
