#!/usr/bin/env bash
# doctor tests: `manifest.js doctor` read-only checks leases, wfp, config, runs, active, worktrees, backups, hooks (the plugin's
# hooks.json, plus the double-fire warning for settings.json), runtime (Node, data dir, skills) and retention (analytics lib);
# --fix (own leases only, worktree prune/remove, backups to .trash); --check limiting, --json, usage errors, exit 0 on findings,
# and a tree md5 of root, the data dir and settings unchanged after a full run.
# Runs on a scratch ORCH_DIR / ORCH_RUNS_DIR / ORCH_PROJECTS_DIR / ORCH_SETTINGS / ORCH_CLAUDE_ROOT / ORCH_PLUGIN_ROOT; never the live dirs.
# Usage: bash test/doctor-tests.sh   (exit 0 = all pass)
set -u
CL="${ORCH_TEST_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"; echo "code under test: $CL"   # the checkout this suite tests
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }
has(){ if printf '%s\n' "$2" | grep -qF -- "$3"; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (missing: $3)"; fi; }
M="$CL/lib/manifest.js"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export ORCH_WFP_STATE="$T/wfp" ORCH_DIR="$T/orch" ORCH_RUNS_DIR="$T/orch/runs" ORCH_PROJECTS_DIR="$T/projects" ORCH_SETTINGS="$T/settings.json" ORCH_CLAUDE_ROOT="$T/root" ORCH_PLUGIN_ROOT="$T/plugin" ORCH_METRICS_DB="$T/metrics.db"
unset WFP_SID BOSS_ANALYTICS_LIB BOSS_DATA CLAUDE_CONFIG_DIR BOSS_PROGRESS_DIR; mkdir -p "$ORCH_RUNS_DIR" "$ORCH_PROJECTS_DIR" "$ORCH_DIR/leases" "$ORCH_CLAUDE_ROOT/wfp"
# the data dir a session without scratch env would use (resolved by lib/paths.js, not a hard-coded ~/.claude path): this suite must leave it alone
LIVE=$(env -u ORCH_DIR -u ORCH_RUNS_DIR -u BOSS_DATA node "$CL/lib/paths.js" --json | jq -r .dataDir)
LIVE_ERR_WAS=$([ -f "$LIVE/errors.log" ] && grep -cF -- "$T" "$LIVE/errors.log" || echo 0)
rc(){ "$@" >/dev/null 2>&1; echo $?; }
treesum(){ (cd "$1" && { find . -printf '%p %y %s %T@\n' | sort; find . -type f -exec md5sum {} + | sort; }) | md5sum | cut -c1-32; }
iso(){ date -u -d "$1" +%Y-%m-%dT%H:%M:%S.000Z; }
ME=zzdoc-me-sid-0001 OTHER=zzdoc-other-sid-02
lease(){ printf '{"v":1,"run":"%s","path":"x","session":"%s","acquired":"%s","renewed":"%s","expires":"%s","ttl_sec":7200}\n' "$2" "$3" "$(iso '-3 hours')" "$(iso '-1 hour')" "$4" > "$ORCH_DIR/leases/$1"; }
plan(){ mkdir -p "$ORCH_RUNS_DIR/$1"; printf -- '---\nrun: %s\n---\n# p\n\n## Status\n\n| # | Subtask | Status |\n|---|---|---|\n| 1 | a | %s |\n' "$1" "$2" > "$ORCH_RUNS_DIR/$1/PLAN.md"; }

# ---- fixtures ----
WFPBASE="$CL/wfp/config.default.json"
echo '{"defaultProfile":"quality"}' > "$ORCH_DIR/wfp.json"   # user overlay on the plugin's wfp defaults
cp "$CL/lib/defaults.json" "$ORCH_DIR/config.json"
# fixture plugin root: hooks.json in the {"hooks": {...}} wrapper, with every command naming an existing script, and the ten skills
PL="$ORCH_PLUGIN_ROOT"; mkdir -p "$PL/hooks" "$PL/bin" "$PL/wfp"
for f in bin/boss-node wfp/wfp.js hooks/orch-attempts.js hooks/orch-return-cap.js hooks/orch-stop-gate.js hooks/orchestrator-guard.js; do : > "$PL/$f"; done
for n in start approve away cost doctor runs status stop resume memclean council; do mkdir -p "$PL/skills/$n"; : > "$PL/skills/$n/SKILL.md"; done
hk(){ printf '{"type":"command","command":"\\"${CLAUDE_PLUGIN_ROOT}/bin/boss-node\\" \\"${CLAUDE_PLUGIN_ROOT}/%s\\" %s"}' "$1" "$2"; }
hkgrp(){ printf '[{%s"hooks":[%s]}]' "${2:+\"matcher\":\"$2\",}" "$1"; }
printf '{"hooks":{"SessionStart":%s,"SubagentStart":%s,"SubagentStop":%s,"PreToolUse":%s,"UserPromptSubmit":%s,"Stop":%s}}\n' \
  "$(hkgrp "$(hk wfp/wfp.js session)")" "$(hkgrp "$(hk wfp/wfp.js substart)")" "$(hkgrp "$(hk wfp/wfp.js substop),$(hk hooks/orch-attempts.js substop),$(hk hooks/orch-return-cap.js substop)")" \
  "$(hkgrp "$(hk hooks/orch-attempts.js pretool),$(hk hooks/orch-stop-gate.js '')" 'Agent|Task')" "$(hkgrp "$(hk wfp/wfp.js prompt)")" "$(hkgrp "$(hk hooks/orch-attempts.js stop)")" > "$PL/hooks/hooks.json"
jq -e . "$PL/hooks/hooks.json" >/dev/null || echo "FIXTURE hooks.json does not parse"
cp "$PL/hooks/hooks.json" "$T/hooks.good"
# settings fixture: unrelated hooks only (no orchestrator entries), so nothing double-fires
printf '{"env":{},"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"rtk hook claude"}]}]}}\n' > "$ORCH_SETTINGS"
cp "$ORCH_SETTINGS" "$T/settings.good"
echo "$T/orch/runs/zzdoc-own-live" > "$ORCH_DIR/ACTIVE"
plan zzdoc-own-done done; plan zzdoc-own-live pending; plan zzdoc-own-exp running; plan zzdoc-stale running
lease zzdoc-own-exp.json  zzdoc-own-exp  "$ME"    "$(iso '-10 minutes')"
lease zzdoc-own-done.json zzdoc-own-done "$ME"    "$(iso '+1 hour')"
lease zzdoc-own-live.json zzdoc-own-live "$ME"    "$(iso '+1 hour')"
lease zzdoc-for-live.json zzdoc-for-live "$OTHER" "$(iso '+1 hour')"
lease zzdoc-for-exp.json  zzdoc-for-exp  "$OTHER" "$(iso '-2 hours')"
lease zzdoc-wrong.json    zzdoc-elsewhere "$OTHER" "$(iso '+1 hour')"   # file name != its run
echo 'not json' > "$ORCH_DIR/leases/zzdoc-bad.json"
echo '{}' > "$ORCH_DIR/leases/Bad Name.json"
mkdir "$ORCH_DIR/leases/zzdoc-for-live.json.lock"; touch -d '10 minutes ago' "$ORCH_DIR/leases/zzdoc-for-live.json.lock"
mkdir "$ORCH_DIR/leases/zzdoc-own-live.json.lock"   # fresh: not reported
mkdir -p "$ORCH_RUNS_DIR/zzdoc-brief" "$ORCH_RUNS_DIR/zzdoc-mf" "$ORCH_RUNS_DIR/zzdoc-empty"
echo '# brief' > "$ORCH_RUNS_DIR/zzdoc-brief/BRIEF.md"
echo '{"id":"zzdoc-mf#e1","seq":1,"type":"launch","ts":"'"$(iso '-1 hour')"'","run":"zzdoc-mf","attempt":"zzdoc-mf#1.1","row":"1","n":1}' > "$ORCH_RUNS_DIR/zzdoc-mf/manifest.jsonl"
echo '{"id":"zzdoc-stale#e1","seq":1,"type":"launch","ts":"'"$(iso '-30 hours')"'","run":"zzdoc-stale","attempt":"zzdoc-stale#2.1","row":"2","n":1}' > "$ORCH_RUNS_DIR/zzdoc-stale/manifest.jsonl"

S0="$(treesum "$ORCH_CLAUDE_ROOT") $(treesum "$ORCH_DIR") $(md5sum < "$ORCH_SETTINGS")"

echo "== full run with --sid (text)"
out=$(node "$M" doctor --sid "$ME" 2>&1); r=$?
ok "doctor exits 0 with findings" "$r" "0"
has "own expired lease: warn, fixable" "$out" "warn leases: zzdoc-own-exp: own lease expired"
has "own live lease on a done run: warn, fixable" "$out" "warn leases: zzdoc-own-done: own live lease on a done run · fix: --fix leases"
has "own live lease on an open run: ok" "$out" "ok leases: zzdoc-own-live: own lease, live until"
has "foreign live lease: info, not fixable" "$out" "info leases: zzdoc-for-live: foreign lease, holder zzdoc-ot, live until"
has "foreign expired lease: info" "$out" "info leases: zzdoc-for-exp: foreign lease, holder zzdoc-ot, expired"
ok "foreign leases carry no fix hint" "$(printf '%s\n' "$out" | grep 'zzdoc-for-' | grep -c 'fix:')" "0"
has "bad JSON lease file: warn" "$out" "warn leases: unreadable lease file leases/zzdoc-bad.json"
has "name != run lease file: warn" "$out" "warn leases: unreadable lease file leases/zzdoc-wrong.json"
has "misnamed lease file: warn" "$out" "warn leases: misnamed lease file leases/Bad Name.json"
has "old lock dir: info" "$out" "info leases: lock dir leases/zzdoc-for-live.json.lock is 10m old"
ok "fresh lock dir not reported" "$(printf '%s\n' "$out" | grep -c 'zzdoc-own-live.json.lock')" "0"
has "runs: brief-only" "$out" "info runs: zzdoc-brief: no PLAN.md: brief-only"
has "runs: manifest only" "$out" "info runs: zzdoc-mf: no PLAN.md: ledger/manifest only (tiny/small)"
has "runs: empty" "$out" "info runs: zzdoc-empty: no PLAN.md: empty"
has "runs: open attempt >24 h, lease none" "$out" "info runs: zzdoc-stale: 1 open attempt(s) older than 24 h with no stop event (oldest zzdoc-stale#2.1, 30h), lease none · fix: /boss:resume zzdoc-stale"
ok "runs: a 1 h open attempt is not reported" "$(printf '%s\n' "$out" | grep -c 'zzdoc-mf: 1 open')" "0"
has "active: a legacy ACTIVE file is inert" "$out" "info active: a legacy ACTIVE file exists in the data dir: nothing reads it any more, safe to remove"
has "config ok" "$out" "ok config: config.json parses"
has "wfp ok: plugin defaults overlaid by <data>/wfp.json" "$out" "ok wfp: wfp config: 4 profiles, default quality"
has "hooks ok: hooks.json wrapper, 6 events, every command names an existing script" "$out" "ok hooks: plugin hooks registered for 6 events; every command names an existing script"
ok "settings.json with no orchestrator entries raises no double-fire warning" "$(printf '%s\n' "$out" | grep -c 'still registers')" "0"
has "runtime: node" "$out" "ok runtime: Node "
has "runtime: data dir writable" "$out" "ok runtime: data dir is writable"
has "runtime: skill count is computed (11 on disk)" "$out" "ok runtime: all 11 plugin skills present"
has "retention: no analytics lib configured is info, not an error" "$out" "info retention: weekly sync scheduler not checked: no boss-analytics lib configured"
has "worktrees ok: no git repo" "$out" "ok worktrees: no linked worktrees"
has "backups ok: none" "$out" "ok backups: no *.bak-* files"
has "summary line" "$out" "doctor: 5 warn, 0 error; fixable: leases"
ok "no orch in new output lines (OC-6)" "$(printf '%s\n' "$out" | grep -v '/boss:resume' | grep -ci 'orch')" "0"

echo "== no sid: no lease reads as own"
out=$(node "$M" doctor --check leases 2>&1)
ok "no sid: nothing fixable" "$(printf '%s\n' "$out" | tail -1)" "doctor: 3 warn, 0 error; fixable: none"
has "no sid: note line" "$out" "note: no session id, so no lease reads as own (pass --sid)"

echo "== --check and --json"
out=$(node "$M" doctor --check config,active --sid "$ME" 2>&1)
ok "--check limits to the named checks" "$(printf '%s\n' "$out" | grep -v '^doctor:' | cut -d' ' -f2 | sort -u | tr '\n' ' ')" "active: config: "
j=$(node "$M" doctor --sid "$ME" --json)
ok "--json keys" "$(jq -c 'keys' <<<"$j")" '["checks","error","findings","fixable","orch_dir","root","settings","sid","warn"]'
ok "--json counts match the text" "$(jq -c '[.warn,.error,.fixable]' <<<"$j")" '[5,0,["leases"]]'
ok "--json finding shape" "$(jq -c '.findings[0] | keys' <<<"$j")" '["check","fixable","level","msg"]'

echo "== usage errors exit 2"
ok "unknown check id" "$(rc node "$M" doctor --check leases,bogus)" "2"
ok "--fix on a check with no fix" "$(rc node "$M" doctor --fix wfp)" "2"
ok "--fix hooks: the plugin registers its hooks, nothing to fix" "$(rc node "$M" doctor --fix hooks)" "2"
ok "--fix with an unknown id" "$(rc node "$M" doctor --fix bogus)" "2"
ok "--fix with no ids" "$(rc node "$M" doctor --fix ,)" "2"
ok "--check with no ids" "$(rc node "$M" doctor --check ,)" "2"
has "HELP lists doctor" "$(node "$M" help)" "doctor [--check id[,id...]] [--sid S] [--json]"
has "unknown command lists doctor" "$(node "$M" bogus 2>&1)" "read-only: status | runs | doctor"

echo "== read-only: tree md5 unchanged after every call above"
ok "root, ORCH_DIR and settings unchanged" "$(treesum "$ORCH_CLAUDE_ROOT") $(treesum "$ORCH_DIR") $(md5sum < "$ORCH_SETTINGS")" "$S0"
ok "no errors.log written" "$([ -e "$ORCH_DIR/errors.log" ] && echo yes || echo no)" "no"

echo "== failure modes: bad config, bad wfp overlay, ACTIVE, retention, runtime"
echo '{"leaseTtlSec": -5, "passStatuses": "done"' > "$ORCH_DIR/config.json"
has "config that does not parse: error" "$(node "$M" doctor --check config 2>&1)" "error config: config.json does not parse"
echo '{"leaseTtlSec": -5, "passStatuses": "done"}' > "$ORCH_DIR/config.json"
out=$(node "$M" doctor --check config 2>&1)
has "non-positive number: error" "$out" "error config: leaseTtlSec must be a positive number"
has "non-array: error" "$out" "error config: passStatuses must be an array"
jq '.profiles.quality.roles.review.effort = "huge" | .ultracodeRaise.max = "nope" | .typeRoles["orch-builder"] = "builder"' "$WFPBASE" > "$ORCH_DIR/wfp.json"
out=$(node "$M" doctor --check wfp 2>&1); r=$?
ok "wfp errors still exit 0" "$r" "0"
has "wfp: bad effort" "$out" "error wfp: profile quality: role review effort 'huge' is not one of low, medium, high, xhigh, max"
has "wfp: ultracodeRaise gap" "$out" "error wfp: ultracodeRaise has no profile for 'max'"
has "wfp: bad typeRoles role" "$out" "error wfp: typeRoles orch-builder: role 'builder'"
echo '{"defaultProfile": "nosuch"}' > "$ORCH_DIR/wfp.json"
has "wfp: defaultProfile naming no profile" "$(node "$M" doctor --check wfp 2>&1)" "error wfp: defaultProfile 'nosuch' names no profile"
echo '{"defaultProfile": ' > "$ORCH_DIR/wfp.json"
has "wfp: a broken overlay is reported and ignored" "$(node "$M" doctor --check wfp 2>&1)" "error wfp: wfp config: wfp.json ignored"
rm "$ORCH_DIR/wfp.json"
has "wfp: no overlay at all is fine (defaults only)" "$(node "$M" doctor --check wfp 2>&1)" "ok wfp: wfp config: 4 profiles, default "
rm "$ORCH_DIR/ACTIVE"
has "active: absent is ok" "$(node "$M" doctor --check active 2>&1)" "ok active: no ACTIVE file"
cp "$CL/lib/defaults.json" "$ORCH_DIR/config.json"

echo "== retention: the analytics lib comes from config.json analytics.lib, else BOSS_ANALYTICS_LIB"
mkdir -p "$T/an-ok" "$T/an-no" "$T/an-bad"
printf 'exports.status = () => ({ crontab: true, installed: true, daemon: { running: true }, fix: [] });\n' > "$T/an-ok/cron.js"
printf 'exports.status = () => ({ crontab: true, installed: false, daemon: { running: false }, fix: [] });\n' > "$T/an-no/cron.js"
printf 'throw new Error("boom");\n' > "$T/an-bad/cron.js"
out=$(BOSS_ANALYTICS_LIB="$T/an-ok" node "$M" doctor --check retention 2>&1)
has "env lib: scheduled is ok" "$out" "ok retention: weekly metrics sync scheduled (Sunday 03:00)"
out=$(BOSS_ANALYTICS_LIB="$T/an-no" node "$M" doctor --check retention 2>&1)
has "env lib: not scheduled warns with the CLI hint" "$out" 'warn retention: weekly metrics sync not scheduled: node "$BOSS_LIB/ledger.js" cron install'
out=$(BOSS_ANALYTICS_LIB="$T/an-bad" node "$M" doctor --check retention 2>&1)
has "env lib: a cron.js that throws is an error naming the lib" "$out" "error retention: cron.js failed to load from $T/an-bad: boom"
jq --arg l "$T/an-ok" '.analytics.lib = $l' "$CL/lib/defaults.json" > "$ORCH_DIR/config.json"
out=$(BOSS_ANALYTICS_LIB="$T/an-no" node "$M" doctor --check retention 2>&1)
has "config analytics.lib wins over the env var" "$out" "ok retention: weekly metrics sync scheduled (Sunday 03:00)"
# H9: a cron line removed on purpose is not a fault; a setup that wants the sync still gets the warning
jq '.analytics.weeklySync = false' "$CL/lib/defaults.json" > "$ORCH_DIR/config.json"
out=$(BOSS_ANALYTICS_LIB="$T/an-no" node "$M" doctor --check retention 2>&1)
has "weeklySync false: info, not a warning" "$out" 'info retention: weekly metrics sync not scheduled on purpose (config analytics.weeklySync is false)'
lacks "weeklySync false: no warn line for the sync" "$out" "warn retention: weekly metrics sync not scheduled"
cp "$CL/lib/defaults.json" "$ORCH_DIR/config.json"
out=$(BOSS_ANALYTICS_LIB="$T/an-no" node "$M" doctor --check retention 2>&1)
has "default config still warns, and says how to silence it" "$out" 'warn retention: weekly metrics sync not scheduled: node "$BOSS_LIB/ledger.js" cron install (or set analytics.weeklySync false'
echo "x" > "$ORCH_DIR/cron-removed"
out=$(BOSS_ANALYTICS_LIB="$T/an-no" node "$M" doctor --check retention 2>&1)
has "the cron-removed marker (ledger.js cron remove) makes it info" "$out" 'info retention: weekly metrics sync not scheduled on purpose (removed with ledger.js cron remove)'
rm -f "$ORCH_DIR/cron-removed"
printf 'exports.status = () => ({ crontab: true, installed: true, daemon: { running: true }, fix: [] }); exports.remove = () => ({ changed: true }); exports.install = () => ({ changed: true, line: "x" }); exports.printStatus = () => {};\n' > "$T/an-ok/cron.js"
BOSS_ANALYTICS_LIB="$T/an-ok" node "$CL/lib/ledger.js" cron remove >/dev/null 2>&1
ok "ledger.js cron remove leaves the marker" "$([ -f "$ORCH_DIR/cron-removed" ] && echo marker)" "marker"
BOSS_ANALYTICS_LIB="$T/an-ok" node "$CL/lib/ledger.js" cron install >/dev/null 2>&1
ok "ledger.js cron install removes it again" "$([ -f "$ORCH_DIR/cron-removed" ] && echo marker || echo none)" "none"
ok "the plugin lib dir has no cron.js (never loaded from __dirname)" "$(ls "$CL/lib/cron.js" 2>/dev/null | wc -l | tr -d ' ')" "0"
cp "$CL/lib/defaults.json" "$ORCH_DIR/config.json"

echo "== hooks and runtime failure modes (copies of the good fixtures)"
HJ="$ORCH_PLUGIN_ROOT/hooks/hooks.json"
jq '.hooks' "$T/hooks.good" > "$HJ"
out=$(node "$M" doctor --check hooks 2>&1)
has "hooks.json without the wrapper: error" "$out" 'error hooks: hooks.json has no {"hooks": {...}} wrapper'
jq 'del(.hooks.Stop, .hooks.UserPromptSubmit)' "$T/hooks.good" > "$HJ"
has "hooks.json missing events: warn" "$(node "$M" doctor --check hooks 2>&1)" "warn hooks: hooks.json registers nothing for UserPromptSubmit, Stop"
echo '{"hooks": ' > "$HJ"
has "hooks.json that does not parse: error" "$(node "$M" doctor --check hooks 2>&1)" "error hooks: plugin hooks file $HJ does not parse"
rm "$HJ"
has "hooks.json missing: error" "$(node "$M" doctor --check hooks 2>&1)" "error hooks: plugin hooks file $HJ is missing or unreadable"
cp "$T/hooks.good" "$HJ"; mv "$ORCH_PLUGIN_ROOT/hooks/orch-stop-gate.js" "$T/stop-gate.moved"
has "hooks.json names a script that is gone: error" "$(node "$M" doctor --check hooks 2>&1)" "error hooks: hooks.json names files that do not exist: hooks/orch-stop-gate.js"
mv "$T/stop-gate.moved" "$ORCH_PLUGIN_ROOT/hooks/orch-stop-gate.js"
jq '.hooks.Stop[0].hooks[0].command = "node /opt/other.js"' "$T/hooks.good" > "$HJ"
has "a command that does not go through CLAUDE_PLUGIN_ROOT: error" "$(node "$M" doctor --check hooks 2>&1)" 'error hooks: hooks.json names files that do not exist: (command without ${CLAUDE_PLUGIN_ROOT}: node /opt/other.js)'
cp "$T/hooks.good" "$HJ"
jq '.hooks.SessionStart = [{"hooks":[{"type":"command","command":"node \"/h/wfp/wfp.js\" session"}]}] | .hooks.PreToolUse += [{"matcher":"Bash","hooks":[{"type":"command","command":"node \"/h/hooks/orchestrator-guard.js\""}]}]' "$T/settings.good" > "$ORCH_SETTINGS"
out=$(node "$M" doctor --check hooks 2>&1)
has "settings.json still registers the orchestrator hooks: warn (double fire)" "$out" "warn hooks: settings.json still registers 2 orchestrator hook(s) (PreToolUse: hooks/orchestrator-guard.js; SessionStart: wfp/wfp.js); with the plugin enabled every hook fires twice"
ok "double-fire warning is report-only" "$(printf '%s\n' "$out" | tail -1)" "doctor: 1 warn, 0 error; fixable: none"
echo 'not json' > "$ORCH_SETTINGS"
has "unparseable settings.json: warn, not a crash" "$(node "$M" doctor --check hooks 2>&1)" "warn hooks: settings file $ORCH_SETTINGS does not parse"
rm "$ORCH_SETTINGS"
ok "no settings.json: nothing to compare, hooks still ok" "$(node "$M" doctor --check hooks 2>&1 | head -1)" "ok hooks: plugin hooks registered for 6 events; every command names an existing script"
cp "$T/settings.good" "$ORCH_SETTINGS"
rm -r "$ORCH_PLUGIN_ROOT/skills/status"
has "a missing skill: warn" "$(node "$M" doctor --check runtime 2>&1)" "warn runtime: plugin skills missing: status"
mkdir "$ORCH_PLUGIN_ROOT/skills/status"; : > "$ORCH_PLUGIN_ROOT/skills/status/SKILL.md"
mkdir "$ORCH_PLUGIN_ROOT/skills/zzextra"; : > "$ORCH_PLUGIN_ROOT/skills/zzextra/SKILL.md"
has "a new skills/<name>/ is counted without a code edit (12)" "$(node "$M" doctor --check runtime 2>&1)" "ok runtime: all 12 plugin skills present"
rm -r "$ORCH_PLUGIN_ROOT/skills/zzextra"
if [ "$(id -u)" != 0 ]; then
  chmod 500 "$ORCH_DIR"; out=$(node "$M" doctor --check runtime 2>&1); chmod 700 "$ORCH_DIR"
  has "data dir not writable: error" "$out" "error runtime: data dir $ORCH_DIR is not writable (EACCES)"
fi
has "data dir not created yet: info" "$(ORCH_DIR="$T/nodata" ORCH_RUNS_DIR="$T/nodata/runs" node "$M" doctor --check runtime 2>&1)" "info runtime: data dir $T/nodata does not exist yet; the first run creates it"

echo "== backups and worktrees fixtures"
mkdir -p "$ORCH_CLAUDE_ROOT/wfp/fixtures" "$ORCH_CLAUDE_ROOT/hooks"
echo a > "$ORCH_CLAUDE_ROOT/hooks/manifest.js.bak-fix"; echo b > "$ORCH_CLAUDE_ROOT/wfp/fixtures/run.sh.bak-fix10"
echo c > "$ORCH_CLAUDE_ROOT/wfp/config.json.bak-default"
for i in 1 2 3; do echo "$i" > "$ORCH_CLAUDE_ROOT/settings.json.bak-orch-2026010$i-000000"; touch -d "$((4-i)) days ago" "$ORCH_CLAUDE_ROOT/settings.json.bak-orch-2026010$i-000000"; done
echo w > "$ORCH_CLAUDE_ROOT/settings.json.bak-wfp-20260101-000000"
R="$T/repo"; G(){ git -C "$R" -c user.name=t -c user.email=t@t "$@" >/dev/null 2>&1; }
git init -q -b main "$R"; echo x > "$R/f"; G add f; G commit -qm base
mkdir -p "$ORCH_RUNS_DIR/zzdoc-wt-done" "$ORCH_RUNS_DIR/zzdoc-wt-dirty"
echo '{"id":"zzdoc-wt-done#e1","seq":1,"type":"launch","ts":"'"$(iso '-2 hours')"'","run":"zzdoc-wt-done","attempt":"zzdoc-wt-done#1.1","row":"1","n":1,"git":"'"$R"'"}' > "$ORCH_RUNS_DIR/zzdoc-wt-done/manifest.jsonl"
G worktree add -q -b orch/zzdoc-wt-done "$T/wt-done"; G worktree add -q -b x/zzdoc-wt-dirty "$T/wt-dirty"; echo d > "$T/wt-dirty/dirty"
G worktree add -q -b feature "$T/wt-other"; G worktree add -q -b gone "$T/wt-gone"; rm -rf "$T/wt-gone"
mkdir -p "$R/sub/dir"; for g in "$T/wt-other" "$R/sub/dir" "$R" "$R"; do echo '{"id":"zzdoc-wt-done#e2","seq":2,"type":"launch","ts":"'"$(iso '-2 hours')"'","run":"zzdoc-wt-done","attempt":"zzdoc-wt-done#2.1","row":"2","n":1,"git":"'"$g"'"}' >> "$ORCH_RUNS_DIR/zzdoc-wt-done/manifest.jsonl"; done
W0="$(treesum "$R") $(treesum "$T/wt-dirty")"
out=$(node "$M" doctor --check worktrees,backups 2>&1)
has "worktree: prunable warn, fixable" "$out" "warn worktrees: $T/wt-gone [gone]: prunable"
has "worktree: merged, clean, quiet run worktree removable" "$out" "info worktrees: $T/wt-done [orch/zzdoc-wt-done]: run zzdoc-wt-done: merged into main, clean, lease none, quiet; removable (branch kept) · fix: --fix worktrees"
has "worktree: dirty one kept" "$out" "info worktrees: $T/wt-dirty [x/zzdoc-wt-dirty]: run zzdoc-wt-dirty: kept (uncommitted changes)"
has "worktree: other branches report only" "$out" "info worktrees: $R: 1 other worktree(s)"
ok "worktree: a linked worktree, a subdirectory and a repeated path as launch git dirs resolve to one repo (deduped without a git call per event)" "$(printf '%s\n' "$out" | grep -c 'other worktree(s)')" "1"
has "backups: summary" "$out" "info backups: 7 backup file(s)"
has "backups: *.bak-fix fixable" "$out" "warn backups: hooks/manifest.js.bak-fix: leftover *.bak-fix copy · fix: --fix backups"
has "backups: settings.json.bak-orch beyond the newest 2 fixable" "$out" "warn backups: settings.json.bak-orch-20260101-000000: older than the newest 2 of its kind"
ok "backups: other *.bak-* info only" "$(printf '%s\n' "$out" | grep -c 'bak-default\|bak-orch-2026010[23]\|bak-wfp')" "0"
ok "worktree and backups checks write nothing" "$(treesum "$R") $(treesum "$T/wt-dirty")" "$W0"

echo "== --fix all: foreign leases byte-identical, own leases released, backups moved, worktrees removed or pruned"
F0=$(md5sum "$ORCH_DIR/leases/zzdoc-for-live.json" "$ORCH_DIR/leases/zzdoc-for-exp.json" "$ORCH_DIR/leases/zzdoc-wrong.json")
out=$(node "$M" doctor --fix leases 2>&1)
has "--fix leases without a sid: skipped" "$out" "skipped leases: needs --sid"
out=$(node "$M" doctor --fix all --sid "$ME" 2>&1); r=$?
ok "--fix all exits 0" "$r" "0"
ok "foreign live/expired and other-run lease files byte-identical" "$(md5sum "$ORCH_DIR/leases/zzdoc-for-live.json" "$ORCH_DIR/leases/zzdoc-for-exp.json" "$ORCH_DIR/leases/zzdoc-wrong.json")" "$F0"
has "own expired lease released" "$out" "fixed leases: released own lease on zzdoc-own-exp"
has "own live lease on a done run released" "$out" "fixed leases: released own lease on zzdoc-own-done"
ok "own released lease files gone, own open-run lease kept" "$(ls "$ORCH_DIR/leases" | grep -c '^zzdoc-own-\(exp\|done\)\.json$') $(ls "$ORCH_DIR/leases/zzdoc-own-live.json" >/dev/null 2>&1 && echo kept)" "0 kept"
has "backups moved, not deleted" "$out" "fixed backups: hooks/manifest.js.bak-fix -> .trash/doctor-"
ok "moved backups exist under .trash" "$(find "$ORCH_CLAUDE_ROOT/.trash" -type f | sed 's#.*/doctor-[^/]*/##' | sort | tr '\n' ' ')" "hooks/manifest.js.bak-fix settings.json.bak-orch-20260101-000000 wfp/fixtures/run.sh.bak-fix10 "
ok "info-only backups stay" "$(ls "$ORCH_CLAUDE_ROOT/wfp/config.json.bak-default" "$ORCH_CLAUDE_ROOT"/settings.json.bak-* | wc -l | tr -d ' ')" "4"
has "worktree pruned" "$out" "fixed worktrees: git -C $R worktree prune"
has "worktree removed, branch kept" "$out" "fixed worktrees: git -C $R worktree remove $T/wt-done (branch kept)"
ok "removed worktree dir gone, its branch kept" "$([ -e "$T/wt-done" ] && echo there || echo gone) $(git -C "$R" rev-parse --verify --quiet refs/heads/orch/zzdoc-wt-done >/dev/null && echo branch)" "gone branch"
ok "dirty worktree untouched" "$(treesum "$T/wt-dirty")" "$(echo "$W0" | cut -d' ' -f2)"
ok "after --fix all nothing fixable is left" "$(printf '%s\n' "$out" | tail -1 | sed 's/.*fixable: //')" "none"
ok "no rm of foreign lease: lease dir count" "$(ls "$ORCH_DIR/leases" | grep -c '^zzdoc-for-.*\.json$')" "2"

echo "== live dirs untouched"
ok "no live lease or run folder from this suite" "$(ls -d "$LIVE"/leases/zzdoc-* "$LIVE"/runs/zzdoc-* 2>/dev/null | wc -l | tr -d ' ')" "0"
ok "live errors.log names no scratch path" "$([ -f "$LIVE/errors.log" ] && grep -cF -- "$T" "$LIVE/errors.log" || echo 0)" "$LIVE_ERR_WAS"

echo "pass=$pass fail=$fail"; [ "$fail" -eq 0 ]
