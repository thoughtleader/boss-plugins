#!/usr/bin/env bash
# Meter suites: the model, board and CLI tests (meter/test) and the mod tests (meter/mod/tests), including the check
# that the committed hooks/meter-register.js equals a fresh build. node --test prints the "ℹ pass N / ℹ fail M" counts
# scripts/test-all.sh reads. meter/mod/tests/native.test.ts runs only under `claude plugin test`; it is not run here.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib/compat.sh"   # on macOS: GNU date/stat/touch/md5sum/wc shims (test/lib)
PLUGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PLUGIN/meter" || exit 1
# esbuild (devDependency in meter/package.json) is needed to rebuild the bundle for the staleness test
if [ ! -d node_modules/esbuild ] && command -v npm >/dev/null 2>&1; then
  npm ci --no-audit --no-fund --loglevel=error >&2 || echo "meter-tests: npm ci failed; the bundle staleness test will be skipped" >&2
fi
# esbuild installed (above, in the caller's environment); from here on everything runs in the scratch environment
# Scratch environment, ALWAYS: values are overridden, never defaulted, so nothing the caller exported reaches the tests. A Claude
# session exports BOSS_PROGRESS_DIR (the owner's real progress dir) and HOME is real, which let a run write fake runs into
# ~/.claude/boss/runs/council and ~/.claude-council. Removed on exit (the trap below).
SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/boss-meter-env.XXXXXX") || exit 1
mkdir -p "$SCRATCH/home/.claude" "$SCRATCH/progress" "$SCRATCH/orch" "$SCRATCH/data" "$SCRATCH/wfp" "$SCRATCH/tmp"
unset BOSS_LIB BOSS_PROGRESS_LIB BOSS_ANALYTICS_LIB BOSS_NODE BOSS_ORCH_RUNS BOSS_COUNCIL_RUNS BOSS_SESSIONS_DIR BOSS_WFP_STATE \
      BOSS_INBOX_DIR BOSS_PROJECTS_DIR BOSS_METER_DIR WFP_SID CLAUDE_ENV_FILE CLAUDE_PLUGIN_ROOT CLAUDE_PLUGIN_DATA \
      ORCHESTRATOR_GUARD ORCH_RUNS_DIR ORCH_PROJECTS_DIR ORCH_SESSIONS_DIR ORCH_METRICS_DB ORCH_SETTINGS ORCH_CLAUDE_ROOT \
      ORCH_PLUGIN_ROOT ORCH_WFP_STATE ORCH_WFP_JS WFP_SRC COUNCIL_CONFIG
export HOME="$SCRATCH/home" CLAUDE_CONFIG_DIR="$SCRATCH/home/.claude" BOSS_PROGRESS_DIR="$SCRATCH/progress" \
       BOSS_DATA="$SCRATCH/data" ORCH_DIR="$SCRATCH/orch" WFP_STATE_DIR="$SCRATCH/wfp" TMPDIR="$SCRATCH/tmp"
trap 'rm -rf "$SCRATCH"' EXIT
node --test test/*.test.mjs mod/tests/*.test.mjs
