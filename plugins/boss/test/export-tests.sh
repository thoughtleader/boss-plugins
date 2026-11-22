#!/usr/bin/env bash
# scripts/build-export.sh: offline tests in temp dirs, against a throwaway fixture repo (BUILD_EXPORT_REPO) and, last,
# this repo's own HEAD. Planted strings are assembled at run time so this file never trips the scan it tests.
# Usage: bash test/export-tests.sh     (exit 0 = all pass; prints "pass=N fail=M")
set -u
PLUGIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$PLUGIN/../.." && pwd)"
BX="$ROOT/scripts/build-export.sh"
T=$(mktemp -d "${TMPDIR:-/tmp}/boss-export-test.XXXXXX") || exit 1
trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok(){ if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1 (got: $2 | want: $3)"; fi; }

# a neutral identity for the fixture repo's own commits; the export must not carry it
FX_ENV=(GIT_AUTHOR_NAME=fixture GIT_AUTHOR_EMAIL=fixture@example.invalid GIT_COMMITTER_NAME=fixture GIT_COMMITTER_EMAIL=fixture@example.invalid)
NOREPLY="tester@users.""noreply.github.com"
# the private-names list lives outside the repo in real use; the tests write their own with invented names (split here so this file never matches itself)
NAMES_TXT="$T/private-names.txt"
printf '%s\n' '# invented names for the tests' '' 'acme-pri''vate' 'zeta[ -]corp' > "$NAMES_TXT"
export EXPORT_PRIVATE_NAMES_FILE="$NAMES_TXT"

# mkfixture <dir>: two commits (history the export must drop), a scrub target, a pycache, an untracked file
mkfixture(){
  local d=$1
  mkdir -p "$d/plugins/boss/.claude-plugin" "$d/scripts" "$d/src/__pycache__"
  git -C "$d" init -q -b main
  printf '{"name":"boss","version":"9.9.9","author":{"url":"%s"}}\n' "https:/""/micha""elevan.com" > "$d/plugins/boss/.claude-plugin/plugin.json"
  echo "hello" > "$d/src/a.txt"
  echo "old import" > "$d/IMPORT-SOURCE"
  echo "#!/bin/sh" > "$d/scripts/import-from-claude.sh"
  echo "junk" > "$d/src/__pycache__/x.cpython-311.pyc"
  git -C "$d" add -A -f
  env "${FX_ENV[@]}" git -C "$d" commit -q -m "first"
  echo "more" >> "$d/src/a.txt"
  env "${FX_ENV[@]}" git -C "$d" commit -q -am "second"
  echo "never committed" > "$d/untracked.txt"
}
FX="$T/fx"; mkdir -p "$FX"; mkfixture "$FX"
export BUILD_EXPORT_REPO="$FX"

echo "== 1. refuses a non-empty out dir"
mkdir -p "$T/full"; echo keep > "$T/full/keep.txt"
o=$(bash "$BX" HEAD "$T/full" --email "$NOREPLY" 2>&1); rc=$?
ok "non-empty out dir: exit 2" "$rc" "2"
ok "non-empty out dir: message names it" "$(echo "$o" | grep -c 'not empty')" "1"
ok "non-empty out dir: left untouched" "$(ls -A "$T/full" | tr '\n' ' ')" "keep.txt "

echo "== 2. a clean build: one commit, noreply identity, scrubbed, no history, no remote"
o=$(bash "$BX" HEAD "$T/out" --name "Test Person" --email "$NOREPLY" 2>&1); rc=$?
ok "build exits 0" "$rc" "0"
ok "exactly one commit" "$(git -C "$T/out" rev-list --count HEAD)" "1"
ok "author and committer" "$(git -C "$T/out" log -1 --format='%an <%ae> | %cn <%ce>')" "Test Person <$NOREPLY> | Test Person <$NOREPLY>"
ok "fixture identity not carried" "$(git -C "$T/out" log --all --format='%ae %ce' | grep -c fixture)" "0"
ok "default message uses the plugin version" "$(git -C "$T/out" log -1 --format=%s)" "boss 9.9.9: orchestrator, council and usage-workflow plugins"
ok "no remote" "$(git -C "$T/out" remote | wc -l | tr -d ' ')" "0"
ok "IMPORT-SOURCE scrubbed" "$([ -e "$T/out/IMPORT-SOURCE" ] && echo present || echo gone)" "gone"
ok "import-from-claude.sh scrubbed" "$([ -e "$T/out/scripts/import-from-claude.sh" ] && echo present || echo gone)" "gone"
ok "__pycache__ scrubbed" "$([ -e "$T/out/src/__pycache__" ] && echo present || echo gone)" "gone"
ok "untracked file not exported" "$([ -e "$T/out/untracked.txt" ] && echo present || echo gone)" "gone"
ok "tracked content is HEAD's" "$(tail -1 "$T/out/src/a.txt")" "more"
ok "prints the SHA" "$(echo "$o" | sed -n 's/^export:  //p')" "$(git -C "$T/out" rev-parse HEAD)"
ok "prints the file count" "$(echo "$o" | sed -n 's/^files:   //p')" "$(git -C "$T/out" ls-files | wc -l | tr -d ' ')"
ok "public author URL alone passes the gate" "$(echo "$o" | grep -c '^gate:')" "1"
ok "an empty existing out dir is accepted" "$(mkdir "$T/empty"; bash "$BX" HEAD~1 "$T/empty" --email "$NOREPLY" >/dev/null 2>&1; echo $?)" "0"
ok "an earlier sha exports that tree" "$(tail -c 5 "$T/empty/src/a.txt" | tr -d '\n')" "ello"

echo "== 3. the scan gate fails on a planted secret"
F2="$T/fx2"; mkdir -p "$F2"; mkfixture "$F2"
planted="sk-""or-v1-$(printf 'abcdef0123456789%.0s' 1 2)"   # key-shaped, not a TESTSECRET fixture
printf 'API=%s\n' "$planted" > "$F2/src/notes.txt"
git -C "$F2" add -A; env "${FX_ENV[@]}" git -C "$F2" commit -q -m "plant"
o=$(BUILD_EXPORT_REPO="$F2" bash "$BX" HEAD "$T/out2" --email "$NOREPLY" 2>&1); rc=$?
ok "planted key: exit 1" "$rc" "1"
ok "planted key: file named" "$(echo "$o" | grep -q 'src/notes.txt' && echo yes || echo no)" "yes"
ok "planted key: value never printed" "$(echo "$o" | grep -c "$planted")" "0"
ok "planted key: no repository created" "$([ -e "$T/out2/.git" ] && echo repo || echo none)" "none"

echo "== 4. the private-name gate"
F3="$T/fx3"; mkdir -p "$F3"; mkfixture "$F3"
printf 'see %s%s/notes\n' "/home/" "jdoe" > "$F3/src/path.txt"
git -C "$F3" add -A; env "${FX_ENV[@]}" git -C "$F3" commit -q -m "path"
o=$(BUILD_EXPORT_REPO="$F3" bash "$BX" HEAD "$T/out3" --email "$NOREPLY" 2>&1); rc=$?
ok "private home path: exit 1" "$rc" "1"
ok "private home path: line reported" "$(echo "$o" | grep -c 'src/path.txt')" "1"
ok "private home path: no repository created" "$([ -e "$T/out3/.git" ] && echo repo || echo none)" "none"

echo "== 4b. the gate rejects each planted string a reviewer got through (every case: exit 1, no repository)"
# plant <label> <content>: a fixture repo whose HEAD adds one file holding <content>; the gate must refuse it
plant(){
  local d="$T/p-$1"; mkdir -p "$d"; mkfixture "$d"
  printf '%s\n' "$2" > "$d/src/planted.txt"
  git -C "$d" add -A; env "${FX_ENV[@]}" git -C "$d" commit -q -m "plant"
  local o; o=$(BUILD_EXPORT_REPO="$d" bash "$BX" HEAD "$T/po-$1" --email "$NOREPLY" 2>&1); local rc=$?
  ok "plant $1: exit 1" "$rc" "1"
  ok "plant $1: named in the output" "$(echo "$o" | grep -q 'src/planted.txt' && echo yes || echo no)" "yes"
  ok "plant $1: no repository created" "$([ -e "$T/po-$1/.git" ] && echo repo || echo none)" "none"
}
plant gmail "contact someone@gma""il.com"
plant mail-beside-noreply "Michael Evan <thoughtleader@users.noreply.github.com> and a.person@gma""il.com"
plant author-admin "see https:/""/micha""elevan.com/admin/internal"
plant author-lookalike "see https:/""/micha""elevan.com.evil.exam""ple/x"
plant repo-private "clone https://github.com/though""tleader/boss-plugins-private"
plant repo-shorthand-private "marketplace add though""tleader/boss-plugins-private"
plant ip-10 "proxy at http:/""/10.20.""30.40:3000"
plant ip-192 "host 192.168.""1.77"
plant ip-172 "host 172.20.""1.1"
plant ip-fixture-elsewhere "ss 10.0.""0.5:5000"
plant home-path "/ho""me/jdoe/.ssh/id"
plant users-path "/Us""ers/jdoe/Documents"
plant windows-path "C:\\Us""ers\\jdoe\\x"
plant userinfo-url "https:/""/user:pw@gith""ub.com/x"
plant commit-text-via-file "ops@private-""corp.io"
plant private-name "client acme-pri""vate has a contract"
plant private-name-case "see ACME-PRI""VATE for details"
plant private-name-regex-line "notes on Zeta C""orp"
plant mnt-users-path "cp /mnt/d/Us""ers/jdoe/x ."
# a name on the list is judged only by the list: the same text passes when the list does not name it
F5="$T/fx5"; mkdir -p "$F5"; mkfixture "$F5"
printf '%s\n' "client acme-pri""vate has a contract" > "$F5/src/n.txt"
git -C "$F5" add -A; env "${FX_ENV[@]}" git -C "$F5" commit -q -m "n"
printf '%s\n' 'some-other-name' > "$T/other-names.txt"
BUILD_EXPORT_REPO="$F5" EXPORT_PRIVATE_NAMES_FILE="$T/other-names.txt" bash "$BX" HEAD "$T/n5" --email "$NOREPLY" >/dev/null 2>&1; ok "a name not on the list passes (the list is the only source)" "$?" "0"
# the exact public tokens and fixture placeholders pass
F4="$T/fx4"; mkdir -p "$F4"; mkfixture "$F4"
printf '%s\n' "repo https://github.com/thoughtleader/boss-plugins and thoughtleader/boss-plugins and a@example.com /home/example/x https://example.invalid/y" > "$F4/src/ok.txt"
git -C "$F4" add -A; env "${FX_ENV[@]}" git -C "$F4" commit -q -m "ok"
BUILD_EXPORT_REPO="$F4" bash "$BX" HEAD "$T/ok4" --email "$NOREPLY" >/dev/null 2>&1; ok "exact public tokens and placeholders pass" "$?" "0"
BUILD_EXPORT_REPO="$FX" bash "$BX" HEAD "$T/msg1" --email "$NOREPLY" --message "see /ho""me/jdoe" >/dev/null 2>&1; ok "a private path in --message fails the gate" "$?" "1"

echo "== 4b2. the private-names file: required, outside the repo, fail closed"
nf(){ local rc; rm -rf "$T/nfo"; "$@" >"$T/nf.log" 2>&1; rc=$?; echo "$rc"; }
ok "missing names file: exit 2" "$(nf env EXPORT_PRIVATE_NAMES_FILE="$T/does-not-exist.txt" BUILD_EXPORT_REPO="$FX" bash "$BX" HEAD "$T/nfo" --email "$NOREPLY")" "2"
ok "missing names file: says so and names the path" "$(grep -c "private-names file is missing.*does-not-exist.txt" "$T/nf.log")" "1"
ok "missing names file: nothing built" "$([ -e "$T/nfo" ] && echo made || echo none)" "none"
: > "$T/empty-names.txt"
ok "empty names file: exit 2" "$(nf env EXPORT_PRIVATE_NAMES_FILE="$T/empty-names.txt" BUILD_EXPORT_REPO="$FX" bash "$BX" HEAD "$T/nfo" --email "$NOREPLY")" "2"
printf '# only a comment\n\n   \n' > "$T/comment-names.txt"
ok "comments and blanks only: exit 2" "$(nf env EXPORT_PRIVATE_NAMES_FILE="$T/comment-names.txt" BUILD_EXPORT_REPO="$FX" bash "$BX" HEAD "$T/nfo" --email "$NOREPLY")" "2"
mkdir -p "$T/xdg/boss-plugins" "$T/xdg-empty"
cp "$NAMES_TXT" "$T/xdg/boss-plugins/export-private-names.txt"
ok "default location (XDG_CONFIG_HOME) is read when the env var is unset" "$(env -u EXPORT_PRIVATE_NAMES_FILE XDG_CONFIG_HOME="$T/xdg" BUILD_EXPORT_REPO="$FX" bash "$BX" HEAD "$T/nfx" --email "$NOREPLY" >/dev/null 2>&1; echo $?)" "0"
ok "default location missing: exit 2" "$(env -u EXPORT_PRIVATE_NAMES_FILE XDG_CONFIG_HOME="$T/xdg-empty" BUILD_EXPORT_REPO="$FX" bash "$BX" HEAD "$T/nfy" --email "$NOREPLY" >/dev/null 2>&1; echo $?)" "2"
ok "--help works without a names file" "$(env EXPORT_PRIVATE_NAMES_FILE="$T/does-not-exist.txt" bash "$BX" --help >/dev/null 2>&1; echo $?)" "0"

echo "== 4c. the gate fails closed when its tools break"
GOODBIN="$T/bin-nogrep"; mkdir -p "$GOODBIN"
for c in bash git tar find sed rm mkdir ls wc tr head cut sort mktemp cat dirname env awk chmod printenv realpath; do
  p=$(command -v "$c" 2>/dev/null) && ln -sf "$p" "$GOODBIN/$c"
done
BUILD_EXPORT_REPO="$FX" PATH="$GOODBIN" "$(command -v bash)" "$BX" HEAD "$T/ng" --email "$NOREPLY" >"$T/ng.log" 2>&1; ok "no grep on PATH: exit 1" "$?" "1"
ok "no grep on PATH: says so" "$(grep -c 'grep not found' "$T/ng.log")" "1"
ok "no grep on PATH: no repository" "$([ -e "$T/ng/.git" ] && echo repo || echo none)" "none"
for mode in "exit 2" "exit 1" "exit 0" 'echo oops >&2; exit 0'; do
  FB="$T/bin-fake"; rm -rf "$FB"; mkdir -p "$FB"; for c in bash git tar find sed rm mkdir ls wc tr head cut sort mktemp cat dirname env awk chmod printenv realpath; do
    p=$(command -v "$c" 2>/dev/null) && ln -sf "$p" "$FB/$c"; done
  printf '#!/bin/sh\n%s\n' "$mode" > "$FB/grep"; chmod +x "$FB/grep"
  rm -rf "$T/fg"
  BUILD_EXPORT_REPO="$FX" PATH="$FB" "$(command -v bash)" "$BX" HEAD "$T/fg" --email "$NOREPLY" >/dev/null 2>&1; rc=$?
  ok "grep that does '$mode': exit 1 (never a clean pass)" "$rc" "1"
  ok "grep that does '$mode': no repository" "$([ -e "$T/fg/.git" ] && echo repo || echo none)" "none"
done

echo "== 4d. file and directory names are swept like contents"
plantname(){
  local d="$T/n-$1"; mkdir -p "$d"; mkfixture "$d"
  mkdir -p "$d/$(dirname "$2")"; echo "harmless" > "$d/$2"
  git -C "$d" add -A; env "${FX_ENV[@]}" git -C "$d" commit -q -m "name"
  local o; o=$(BUILD_EXPORT_REPO="$d" bash "$BX" HEAD "$T/no-$1" --email "$NOREPLY" 2>&1); local rc=$?
  ok "file name $1: exit 1" "$rc" "1"
  ok "file name $1: reported as a file name" "$(echo "$o" | grep -q '\[file name\]' && echo yes || echo no)" "yes"
  ok "file name $1: no repository created" "$([ -e "$T/no-$1/.git" ] && echo repo || echo none)" "none"
}
plantname private-address "docs/someone@gma""il.com.md"
plantname private-project "docs/acme-pri""vate-notes.md"
plantname private-dir-ip "host-192.168.""1.77/readme.md"
plantname home-dir "notes/ho""me/jdoe-copy/x.md"
plantname private-url-name "notes/https:/""/micha""elevan.com/admin.md"

echo "== 4e. a dirty tree never exports, whichever tool is missing (no silent fail-open)"
FD="$T/fxd"; mkdir -p "$FD"; mkfixture "$FD"
printf '%s\n' "/ho""me/jdoe/x and a.person@gma""il.com" > "$FD/src/dirty.txt"
git -C "$FD" add -A; env "${FX_ENV[@]}" git -C "$FD" commit -q -m "dirty"
TOOLS="bash git tar find sed rm mkdir ls wc tr head cut sort mktemp cat dirname env awk chmod printenv realpath ln grep"
REALBASH=$(command -v bash)
for missing in $TOOLS; do
  B="$T/bin-no-$missing"; rm -rf "$B"; mkdir -p "$B"
  for c in $TOOLS; do [ "$c" = "$missing" ] && continue; p=$(command -v "$c" 2>/dev/null) && ln -sf "$p" "$B/$c"; done
  rm -rf "$T/dm"
  BUILD_EXPORT_REPO="$FD" PATH="$B" "$REALBASH" "$BX" HEAD "$T/dm" --email "$NOREPLY" >/dev/null 2>&1; rc=$?
  ok "dirty tree, no $missing on PATH: refused (exit $rc)" "$([ "$rc" -ne 0 ] && echo refused || echo EXPORTED)" "refused"
  ok "dirty tree, no $missing on PATH: no repository" "$([ -e "$T/dm/.git" ] && echo repo || echo none)" "none"
done
rm -rf "$T/dm"
BUILD_EXPORT_REPO="$FD" PATH="$T/bin-no-sed" "$REALBASH" "$BX" HEAD "$T/dm" --email "$NOREPLY" >/dev/null 2>&1; ok "dirty tree, no sed: exit 1 exactly (the gate, not luck)" "$?" "1"
rm -rf "$T/dm"
# a clean tree still builds with the same stripped-down PATH minus nothing it needs (control for the loop above)
BUILD_EXPORT_REPO="$FX" PATH="$T/bin-no-awk" "$REALBASH" "$BX" HEAD "$T/dm" --email "$NOREPLY" >/dev/null 2>&1; ok "clean tree builds without tools the gate does not use (control)" "$?" "0"

echo "== 4f. secret-scan.sh fails closed when grep is missing or broken"
SS="$PLUGIN/council/tools/secret-scan.sh"
mkdir -p "$T/home" "$T/ss-clean" "$T/ss-key" "$T/ss-fixture"
echo "nothing here" > "$T/ss-clean/a.txt"
printf 'oops %s\n' "sk-""or-v1-$(printf 'abcdef0123456789%.0s' 1 2)" > "$T/ss-key/b.txt"
printf 'fixture %s\n' "sk-or-v1-TESTSECRET0123456789abcdef" > "$T/ss-fixture/c.txt"
ssrun(){ env -i PATH="${SSPATH:-$PATH}" HOME="$T/home" TMPDIR="$T" "$REALBASH" "$SS" "$@"; }
ssrun "$T/ss-clean" >/dev/null 2>&1; ok "secret-scan: clean dir exits 0" "$?" "0"
o=$(ssrun "$T/ss-key" 2>/dev/null); ok "secret-scan: key-shaped string exits 1" "$?" "1"
ok "secret-scan: names the file, not the key" "$(echo "$o" | grep -c 'ss-key/b.txt')|$(echo "$o" | grep -c 'abcdef0123')" "1|0"
ssrun "$T/ss-fixture" >/dev/null 2>&1; ok "secret-scan: TESTSECRET fixture exits 0" "$?" "0"
SSPATH="$T/bin-no-grep" ssrun "$T/ss-key" >/dev/null 2>"$T/ss.err"; rc=$?
ok "secret-scan: no grep on PATH exits 2, never 0" "$rc" "2"
ok "secret-scan: says grep is missing" "$(grep -c 'grep not found' "$T/ss.err")" "1"
FB2="$T/bin-fake2"; rm -rf "$FB2"; mkdir -p "$FB2"; for c in bash mktemp rm sort awk printenv jq cat; do p=$(command -v "$c" 2>/dev/null) && ln -sf "$p" "$FB2/$c"; done
printf '#!/bin/sh\nexit 2\n' > "$FB2/grep"; chmod +x "$FB2/grep"
SSPATH="$FB2" ssrun "$T/ss-clean" >/dev/null 2>&1; ok "secret-scan: a grep that errors (exit 2) on a clean dir exits 2, not 0" "$?" "2"
SSPATH="$FB2" ssrun "$T/ss-key" >/dev/null 2>&1; ok "secret-scan: a grep that errors on a dir with a key exits 2, not 0" "$?" "2"

echo "== 4g. secret-scan.sh --leftover exits 1 when a process sits in the dir, 0 when none"
if [ -d /proc/self ]; then
  mkdir -p "$T/lo-busy" "$T/lo-idle"
  (cd "$T/lo-busy" && exec sleep 30) & lopid=$!
  sleep 0.2
  o=$(ssrun --leftover "$T/lo-busy" 2>/dev/null); rc=$?
  ok "secret-scan --leftover: process present exits 1" "$rc" "1"
  ok "secret-scan --leftover: names the pid" "$(echo "$o" | grep -c "^$lopid")" "1"
  ssrun --leftover "$T/lo-idle" >/dev/null 2>&1; ok "secret-scan --leftover: none exits 0" "$?" "0"
  kill "$lopid" 2>/dev/null; wait "$lopid" 2>/dev/null
else
  echo "SKIP no /proc here (macOS): the Linux leftover scan of /proc/<pid>/cwd cannot run; the fail-closed check below covers this host"
fi
# off Linux there is no /proc/<pid>/cwd to scan: the check must fail closed (2), never report "nothing found" (0). A uname stub first on PATH stands in for macOS.
UND="$T/uname-darwin"; mkdir -p "$UND"; printf '#!/bin/sh\necho Darwin\n' > "$UND/uname"; chmod +x "$UND/uname"
mkdir -p "$T/lo-idle2"
SSPATH="$UND:$PATH" ssrun --leftover "$T/lo-idle2" >/dev/null 2>"$T/lo.err"; rc=$?
ok "secret-scan --leftover: a non-Linux uname exits 2, never 0" "$rc" "2"
ok "secret-scan --leftover: says it cannot check here" "$(grep -c 'needs Linux or WSL' "$T/lo.err")" "1"
SSPATH="$UND:$PATH" ssrun --leftover >/dev/null 2>&1; ok "secret-scan --leftover: no dirs given, non-Linux uname still exits 2" "$?" "2"
ssrun --leftover "$T/lo-idle2" >/dev/null 2>&1; rc=$?
if [ "$(uname -s)" = Linux ] && [ -d /proc/self ]; then ok "secret-scan --leftover: Linux with /proc is unchanged (none exits 0)" "$rc" "0"; fi

echo "== 5. refused input"
bash "$BX" HEAD "$T/out4" --email "someone@example.com" >/dev/null 2>&1; ok "non-noreply email refused (exit 2)" "$?" "2"
ok "non-noreply email: no out dir made" "$([ -e "$T/out4" ] && echo made || echo none)" "none"
EXPORT_ALLOW_ANY_EMAIL=1 bash "$BX" HEAD "$T/out5" --email "someone@example.com" >/dev/null 2>&1; ok "EXPORT_ALLOW_ANY_EMAIL=1 allows it" "$?" "0"
bash "$BX" no-such-rev "$T/out6" --email "$NOREPLY" >/dev/null 2>&1; ok "unknown sha refused (exit 2)" "$?" "2"
bash "$BX" HEAD >/dev/null 2>&1; ok "missing out dir argument (exit 2)" "$?" "2"
ok "env identity is read" "$(EXPORT_AUTHOR_NAME=Env EXPORT_AUTHOR_EMAIL=env@users."noreply.github.com" bash "$BX" HEAD "$T/out7" >/dev/null 2>&1; git -C "$T/out7" log -1 --format='%an <%ae>')" "Env <env@users.""noreply.github.com>"

echo "== 6. this repo's own HEAD passes its own gate"
unset BUILD_EXPORT_REPO
if git -C "$ROOT" rev-parse --verify -q HEAD >/dev/null; then
  o=$(bash "$BX" HEAD "$T/self" 2>&1); rc=$?
  ok "own HEAD: gate clean, exit 0" "$rc" "0"
  [ "$rc" = 0 ] || echo "$o" | head -12
  ok "own HEAD: one commit" "$(git -C "$T/self" rev-list --count HEAD 2>/dev/null)" "1"
else
  echo "SKIP no HEAD"
fi

echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
