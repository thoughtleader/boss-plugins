#!/usr/bin/env bash
# Council tests, fast half: python3 -Es council/tests/run_suite.py --fast, from the plugin copy (about 2 minutes, stub CLI,
# no network and no paid calls). The slow half (stub processes through the supervisor, hedge, fallback and replay) is
# `python3 -Es council/tests/run_suite.py --slow`; run it by hand when you change the runner.
# Prints "pass=N fail=M" last, which scripts/test-all.sh reads. Needs python3 3.11+ (tomllib).
# Usage: bash test/council-tests.sh     (exit 0 = all pass; always runs in a scratch HOME, progress dir and state dirs, see below)
set -u
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"   # the plugin root this suite tests (a worktree runs its own code)
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
python3 -EsB "$CL/council/tests/run_suite.py" --fast > "$LOG" 2>&1; rc=$?
if [ "$rc" = 0 ]; then tail -n 40 "$LOG"; else tail -n 600 "$LOG"; fi    # a failing run prints its tracebacks, not just the verdict line
ran=$(sed -n 's/^Ran \([0-9][0-9]*\) tests.*/\1/p' "$LOG" | tail -1); ran=${ran:-0}
verdict=$(sed -n 's/^FAILED (\(.*\))/\1/p;s/^OK (\(.*\))/\1/p' "$LOG" | tail -1)
bad=$(printf '%s\n' "$verdict" | grep -oE '(failures|errors|unexpected successes)=[0-9]+' | grep -oE '[0-9]+' | awk '{s+=$1} END {print s+0}'); bad=${bad:-0}    # skipped tests are not failures
skipped=$(printf '%s\n' "$verdict" | sed -n 's/.*skipped=\([0-9][0-9]*\).*/\1/p'); skipped=${skipped:-0}
if [ "$rc" != 0 ] && [ "$bad" = 0 ]; then bad=1; fi            # a timeout or a crash with no unittest verdict still fails
[ "$ran" -gt 0 ] || { [ "$bad" -gt 0 ] || bad=1; }
echo "pass=$((ran - bad - skipped)) fail=$bad"; [ "$skipped" = 0 ] || echo "council: $skipped test(s) skipped, each with its reason in the SKIP lines above"
[ "$rc" = 0 ] && [ "$bad" = 0 ]
