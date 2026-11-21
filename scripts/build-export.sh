#!/usr/bin/env bash
# Build the public export of this repo: a fresh one-commit repository holding the tree of one commit, no history.
#
#   scripts/build-export.sh [--name NAME] [--email EMAIL] [--message TEXT] <sha> <out-dir>
#
#   <sha>      the commit (any rev git resolves) whose tree is exported
#   <out-dir>  must not exist or must be empty; the export is built there and nothing is pushed anywhere
#
# Steps: git archive <sha> into out-dir, scrub, scan (the gate), git init, one commit, print the SHA and file count.
#   scrub  deletes what the public repo must not carry (IMPORT-SOURCE, scripts/import-from-claude.sh) and anything
#          archive could not carry anyway but a copy might (__pycache__, *.pyc, .DS_Store). Idempotent.
#   scan   the gate, fail closed. Every file is swept (binary files too) for: scripts/secret-scan.sh key shapes; key-shaped
#          strings; private names (see "Private names" below); any email, URL, private IPv4 address or /home/<user>, /Users/<user>,
#          C:\Users\<user> path that is not on an exact allow-list. The only public identity tokens are the exact author
#          URL, the exact repo path and the exact noreply address; a longer token (a sub-path, a suffix, a look-alike
#          host) is a violation. The commit author name and message are swept too. A missing grep, a tool error, a
#          sweep that cannot find its own planted canary, a symlink in the tree or any hit prints and exits 1; no .git
#          is created, so there is no commit to push by mistake.
#   commit one commit, author and committer from --name/--email, else EXPORT_AUTHOR_NAME/EXPORT_AUTHOR_EMAIL, else the
#          documented default (README "Building the public export"). Message: --message, EXPORT_COMMIT_MESSAGE, else
#          "boss <plugin version>: orchestrator, council and usage-workflow plugins". The email must be a GitHub noreply
#          address (*@users.noreply.github.com) unless EXPORT_ALLOW_ANY_EMAIL=1. No remote is added, nothing is pushed.
# Private names: the list of names the export must never carry is NOT in this repository (a list in the tree would publish
#   what it guards). It is read at run time from a plain-text file outside the repo, one extended regex per line, matched
#   case-insensitively; blank lines and lines starting with # are ignored. Default location:
#   ${XDG_CONFIG_HOME:-$HOME/.config}/boss-plugins/export-private-names.txt, overridden by EXPORT_PRIVATE_NAMES_FILE.
#   A missing, unreadable or empty file refuses the export (exit 2): the gate does not run without its list.
# Env: BUILD_EXPORT_REPO=<path> exports another repo than the one holding this script (the tests use it).
#      EXPORT_PRIVATE_NAMES_FILE=<path> the private-names file (see above).
# Exit: 0 built, 1 gate failed, 2 usage or refused input.
set -u
export LC_ALL=C

HERE="${BASH_SOURCE[0]%/*}"; [ "$HERE" = "${BASH_SOURCE[0]}" ] && HERE=.
ROOT="$(cd "$HERE/.." && pwd)" || { echo "build-export: cannot resolve the repo root" >&2; exit 2; }
SCAN="$ROOT/scripts/secret-scan.sh"
REPO="${BUILD_EXPORT_REPO:-$ROOT}"

DEFAULT_EMAIL="thoughtleader@users.noreply.github.com"
NAME="${EXPORT_AUTHOR_NAME:-Michael Evan}"
EMAIL="${EXPORT_AUTHOR_EMAIL:-$DEFAULT_EMAIL}"
MSG="${EXPORT_COMMIT_MESSAGE:-}"
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --name) [ "$#" -ge 2 ] || { echo "build-export: --name needs a value" >&2; exit 2; }; NAME="$2"; shift 2;;
    --email) [ "$#" -ge 2 ] || { echo "build-export: --email needs a value" >&2; exit 2; }; EMAIL="$2"; shift 2;;
    --message) [ "$#" -ge 2 ] || { echo "build-export: --message needs a value" >&2; exit 2; }; MSG="$2"; shift 2;;
    -h|--help) sed -n '2,/^set -u/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0;;
    -*) echo "build-export: unknown flag $1" >&2; exit 2;;
    *) args+=("$1"); shift;;
  esac
done
[ "${#args[@]}" -eq 2 ] || { echo "usage: build-export.sh [--name N] [--email E] [--message M] <sha> <out-dir>" >&2; exit 2; }
SHA_IN="${args[0]}"; OUT="${args[1]}"

case "$EMAIL" in
  *@users.noreply.github.com) ;;
  *) if [ "${EXPORT_ALLOW_ANY_EMAIL:-0}" != 1 ]; then
       echo "build-export: refusing author email '$EMAIL': not a GitHub noreply address (set EXPORT_ALLOW_ANY_EMAIL=1 to override)" >&2; exit 2
     fi;;
esac

# the private-names list: outside the repo, required (fail closed). Bash builtins only, so a stripped PATH cannot skip it.
NAMES_FILE="${EXPORT_PRIVATE_NAMES_FILE:-${XDG_CONFIG_HOME:-${HOME:-}/.config}/boss-plugins/export-private-names.txt}"
NAMES=""
if [ -f "$NAMES_FILE" ] && [ -r "$NAMES_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    case "$line" in ''|'#'*) continue;; esac
    [[ $line =~ ^[[:space:]]*$ ]] && continue
    NAMES+="${NAMES:+|}$line"
  done < "$NAMES_FILE"
fi
if [ -z "$NAMES" ]; then
  echo "build-export: refusing to export: the private-names file is missing or has no patterns: $NAMES_FILE" >&2
  echo "build-export: create it (one regex per line, kept outside the repo) or point EXPORT_PRIVATE_NAMES_FILE at it" >&2
  exit 2
fi

SHA=$(git -C "$REPO" rev-parse --verify --quiet "${SHA_IN}^{commit}") || { echo "build-export: '$SHA_IN' is not a commit in $REPO" >&2; exit 2; }

if [ -e "$OUT" ]; then
  [ -d "$OUT" ] || { echo "build-export: $OUT exists and is not a directory" >&2; exit 2; }
  shopt -s nullglob dotglob; existing=("$OUT"/*); shopt -u nullglob dotglob
  if [ "${#existing[@]}" -gt 0 ]; then echo "build-export: $OUT is not empty; refusing" >&2; exit 2; fi
fi
mkdir -p -- "$OUT" || exit 2
OUT="$(cd "$OUT" && pwd)"

# 1. the tree of the commit, tracked files only
git -C "$REPO" archive --format=tar "$SHA" | tar -x -C "$OUT"
st=("${PIPESTATUS[@]}")
[ "${st[0]}" = 0 ] && [ "${st[1]}" = 0 ] || { echo "build-export: git archive | tar failed (${st[*]})" >&2; exit 2; }

# 2. scrub
rm -f -- "$OUT/IMPORT-SOURCE" "$OUT/scripts/import-from-claude.sh" || { echo "build-export: scrub failed" >&2; exit 2; }
find "$OUT" \( -name __pycache__ -type d -prune -exec rm -rf {} + \) -o \( \( -name '*.pyc' -o -name .DS_Store \) -type f -exec rm -f {} + \) || { echo "build-export: scrub failed" >&2; exit 2; }

if [ -z "$MSG" ]; then
  ver=""
  if [ -f "$OUT/plugins/boss/.claude-plugin/plugin.json" ]; then
    pj=$(<"$OUT/plugins/boss/.claude-plugin/plugin.json")
    [[ $pj =~ \"version\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]] && ver=${BASH_REMATCH[1]}
  fi
  MSG="boss ${ver:-export}: orchestrator, council and usage-workflow plugins"
fi

# 3. the gate. It fails closed: a missing tool, a tool error (grep exit 2 or more, or anything on its stderr), a sweep that
# cannot find its own planted canary, a symlink, or any hit stops the export with exit 1 and no repository. Sweeps extract the
# matched TOKENS (grep -o) and judge each against the exact allow-lists below; nothing is allowed by substring. Planted
# strings carry a split so this file never matches itself.
GATE_ERR=""
gate_fail(){ echo "build-export: GATE $*" >&2; echo "build-export: gate failed; $OUT left for inspection, no repository created" >&2; exit 1; }
GREP=$(command -v grep) || gate_fail "grep not found: the sweep cannot run"
GT=$(mktemp -d "${TMPDIR:-/tmp}/build-export.XXXXXX") || gate_fail "mktemp failed"
trap 'rm -rf "$GT"' EXIT
shopt -s nocasematch

PUB_URL="https://michaelevan.com"                    # the public author URL, exact
PUB_REPO="github.com/thoughtleader/boss-plugins"     # the public repo path, exact (also with https://)
PUB_MAIL="$DEFAULT_EMAIL"                            # the public noreply address, exact
PUB_HOST="${PUB_URL#*//}"                            # its host
PUB_REPO_URL="https:/""/$PUB_REPO"                   # the repo path as a URL
OWNER_PATH="${PUB_REPO#github.com}"; OWNER_PATH="${OWNER_PATH%/*}"   # "/<owner>" on github.com: any path under it but the repo fails
# never allowed anywhere: NAMES (the private-names file, loaded above) plus any /mnt/<drive>/Users/ path (the handle and the domain have their own exact-token rule below)
NAMES="/mnt/[a-z]/Users/|$NAMES"
HANDLE='[A-Za-z0-9._/:@%+~-]*(micha[e]levan|though[t]leader)[A-Za-z0-9._/:@%+~-]*'
EMAILRE='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
URLRE='[A-Za-z][A-Za-z0-9+.-]*://[^][:space:]"'"'"'<>)`}\\,;]+'
IPRE='(^|[^0-9.])(10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|169\.254\.[0-9]+\.[0-9]+)([^0-9]|$)'
PATHRE='(/home/|/Users/)[^/[:space:]"'"'"'`\\|,)]*|[A-Za-z]:\\+Users\\+[^\\[:space:]"'"'"'`]*'
KEYRE='(^|[^A-Za-z0-9])sk-(or-v1-)?[A-Za-z0-9_-]{16,}'
WINUSER='[Uu]sers\\+(.*)$'
# placeholder user names in /home/<user> and C:\Users\<user> (fixtures, docs); every other name fails
PLACEHOLDER_USERS='example|someone|somebody|user|username|nobody|you|me|name|u|x|a|b|tmp|test'
# private IPs that exist only as fixtures: exact "file|ip"
IP_OK='plugins/boss/council/tests/test_liveness_probe.py|10.0.0.''5'$'\n''plugins/boss/council/councillib/drift.py|10.0.0.''5'

# grep_tokens <dir> <extra flag letters> <ere>: print "path:line:token" per match (paths relative to <dir>); 2 = the tool failed
# (cd failure, grep exit 2 or more, or anything on grep's stderr). Exit 1 (no match) is the only quiet outcome.
grep_tokens(){
  local rc out here=$PWD
  cd "$1" || { GATE_ERR="cannot enter $1"; return 2; }
  out=$("$GREP" -raoHnE$2 -e "$3" . 2>"$GT/grep.err"); rc=$?
  cd "$here" || { GATE_ERR="cannot leave $1"; return 2; }
  if [ "$rc" -ge 2 ] || [ -s "$GT/grep.err" ]; then
    GATE_ERR="grep exited $rc: $(<"$GT/grep.err")"; GATE_ERR=${GATE_ERR:0:200}; return 2
  fi
  [ -n "$out" ] && printf '%s\n' "$out"
  return 0
}
trim(){ local t=$1; while [[ $t =~ [.,\;:]$ ]]; do t=${t%?}; done; printf '%s' "$t"; }
tokof(){ local t=${1#*:}; printf '%s' "${t#*:}"; }          # path:line:token -> token
# path:line:token -> the file; in file-name mode (NAMELIST set) the line number picks the tracked path out of the name list
fileof(){
  local f=${1%%:*} n=${1#*:} i=0 l
  if [ -n "${NAMELIST:-}" ]; then
    n=${n%%:*}
    while IFS= read -r l; do i=$((i+1)); if [ "$i" = "$n" ]; then printf '[file name] %s' "$l"; return 0; fi; done < "$NAMELIST"
    printf '[file name] ?'; return 0
  fi
  printf '%s' "${f#./}"
}

# judge_*: 0 = allowed
judge_email(){
  local e=$1 d=${1#*@}
  [[ $e == "$PUB_MAIL" ]] && return 0
  case "$d" in
    example.com|example.org|example.net|example.invalid|*.example.com|*.invalid|*.example|host.tld|*.test) return 0;;
  esac
  return 1
}
judge_url(){
  local u=$1 rest host path
  rest=${u#*://}
  [[ ${u%%://*} == file ]] && return 0
  host=${rest%%/*}; path=${rest#"$host"}
  case "$host" in *@*) return 1;; esac
  host=${host%%:*}
  case "$host" in
    "$PUB_HOST") [[ $u == "$PUB_URL" ]] && return 0; return 1;;
    boss.local) [[ $u == "https://boss.local/progress.schema.json" ]] && return 0; return 1;;
    github.com)
      case "$path" in "$OWNER_PATH"|"$OWNER_PATH"/*) [[ "$host$path" == "$PUB_REPO" ]] && return 0; return 1;; esac
      return 0;;
    registry.npmjs.org|openrouter.ai|code.claude.com|json-schema.org|localhost|127.0.0.1|x.com|x|example.com|*.example.com|example|*.example|*.invalid|*.test) return 0;;
  esac
  return 1
}
judge_ip(){
  local f=$1 ip line
  [[ $2 =~ ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) ]] || return 1
  ip=${BASH_REMATCH[1]}
  while IFS= read -r line; do [ "$line" = "$f|$ip" ] && return 0; done <<< "$IP_OK"
  return 1
}
judge_path(){
  local m=$1 user
  case "$m" in
    /home/*) user=${m#/home/};;
    /Users/*) user=${m#/Users/};;
    *) [[ $m =~ $WINUSER ]] || return 1; user=${BASH_REMATCH[1]};;
  esac
  [[ -z $user || $user =~ ^[^A-Za-z0-9] ]] && return 0      # empty, a regex fragment or a dotfile: not a name
  [[ $user =~ ^($PLACEHOLDER_USERS)$ ]] && return 0
  return 1
}

# sweep_dir <dir>: the content sweeps over every file under <dir>; one "<category>|<where> <detail>" line per violation on stdout
# (in file-name mode, NAMELIST names the list being swept and <where> is the tracked path). 2 = a tool failed.
sweep_dir(){
  local d=$1 line tok list="$GT/hits.$$"
  : > "$list" || return 2
  grep_tokens "$d" i "$NAMES" > "$list" || return 2
  while IFS= read -r line; do [ -n "$line" ] && echo "name|$(fileof "$line") $(tokof "$line")"; done < "$list"
  grep_tokens "$d" i "$HANDLE" > "$list" || return 2
  while IFS= read -r line; do [ -n "$line" ] || continue; tok=$(trim "$(tokof "$line")")
    case "$tok" in "$PUB_URL"|"$PUB_REPO_URL"|"$PUB_REPO"|"${PUB_REPO#github.com/}"|"$PUB_MAIL") ;; *) echo "handle|$(fileof "$line") $tok";; esac
  done < "$list"
  grep_tokens "$d" i "$EMAILRE" > "$list" || return 2
  while IFS= read -r line; do [ -n "$line" ] || continue; tok=$(trim "$(tokof "$line")"); judge_email "$tok" || echo "email|$(fileof "$line") $tok"; done < "$list"
  grep_tokens "$d" i "$URLRE" > "$list" || return 2
  while IFS= read -r line; do [ -n "$line" ] || continue; tok=$(trim "$(tokof "$line")"); judge_url "$tok" || echo "url|$(fileof "$line") $tok"; done < "$list"
  grep_tokens "$d" "" "$IPRE" > "$list" || return 2
  while IFS= read -r line; do [ -n "$line" ] || continue; judge_ip "$(fileof "$line")" "$(tokof "$line")" || echo "ip|$(fileof "$line") $(tokof "$line")"; done < "$list"
  grep_tokens "$d" i "$PATHRE" > "$list" || return 2
  while IFS= read -r line; do [ -n "$line" ] || continue; tok=$(tokof "$line"); judge_path "$tok" || echo "path|$(fileof "$line") $tok"; done < "$list"
  # key-shaped strings: file names only, never a value; TESTSECRET marks the suites' fixtures
  grep_tokens "$d" "" "$KEYRE" > "$list" || return 2
  while IFS= read -r line; do [ -n "$line" ] || continue; tok=$(tokof "$line")
    [[ $tok =~ sk-(or-v1-)?TESTSECRET ]] || echo "key|$(fileof "$line")"
  done < "$list"
  return 0
}

# scan_tree <dir>: the symlink check, the content sweeps, then the same sweeps over the list of file and directory names.
# Only bash builtins and the tools below are used, and each is checked: find (stderr and exit), grep (see grep_tokens).
scan_tree(){
  local d=$1 line links rel names="$GT/names.$$"
  links=$(find "$d" -type l 2>"$GT/find.err"); [ "$?" = 0 ] && [ ! -s "$GT/find.err" ] || { GATE_ERR="find failed: $(<"$GT/find.err")"; return 2; }
  while IFS= read -r line; do [ -n "$line" ] && echo "symlink|${line#"$d"/}"; done <<< "$links"
  NAMELIST="" sweep_dir "$d" || return 2
  # file and directory names get the same rules as contents
  mkdir -p "$names.d" || return 2
  find "$d" -mindepth 1 > "$names.raw" 2>"$GT/find.err"; [ "$?" = 0 ] && [ ! -s "$GT/find.err" ] || { GATE_ERR="find failed: $(<"$GT/find.err")"; return 2; }
  : > "$names.d/names.txt" || return 2
  while IFS= read -r line; do [ -n "$line" ] && printf '%s\n' "${line#"$d"/}" >> "$names.d/names.txt"; done < "$names.raw"
  NAMELIST="$names.d/names.txt" sweep_dir "$names.d" || return 2
  return 0
}

# canary: the same sweep over a planted scratch tree must report every category, or the tools are not doing their job
CAN="$GT/canary"; mkdir -p "$CAN" || gate_fail "mkdir failed"
printf '%s\n' 'x@gma''il.com' 'https:/''/evil.exam''ple.org/x' 'http:/''/192.168.''7.7:80' '/ho''me/zedzed/x' \
  "sk-$(printf 'q%.0s' 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18)" "https:/""/micha""elevan.com/admin" 'canary-pri''vate-zq notes' > "$CAN/c.txt" || gate_fail "cannot write the canary"
: > "$CAN/a-file-named-micha""elevan.com" || gate_fail "cannot write the canary"
ln -s c.txt "$CAN/link" 2>/dev/null
REAL_NAMES=$NAMES; NAMES="$NAMES|canary-priv""ate-zq"   # the canary sweeps with the real list plus its own invented name
canary_out=$(scan_tree "$CAN") || gate_fail "sweep tool failed on its self-check: $GATE_ERR"
for cat in symlink name handle email url ip path key '[file name]'; do
  case "$canary_out" in *"$cat"*) ;; *) gate_fail "sweep self-check did not report '$cat': the scan tools are not working";; esac
done

NAMES=$REAL_NAMES

# the verdict: shell substitution only, no tool whose silent failure could turn a dirty tree into "clean"
hits=$(scan_tree "$OUT") || gate_fail "sweep tool failed: $GATE_ERR"
keyscan=$("$BASH" "$SCAN" "$OUT" 2>/dev/null); rc=$?
case "$rc" in
  0) ;;
  1) hits+=$'\n'"key|${keyscan//$'\n'/ } (scripts/secret-scan.sh)";;
  *) gate_fail "scripts/secret-scan.sh failed (exit $rc)";;
esac
# the author name and commit message are published too
mkdir -p "$GT/meta" || gate_fail "mkdir failed"
printf '%s\n%s\n' "$NAME" "$MSG" > "$GT/meta/commit-text.txt" || gate_fail "cannot write the commit text"
metahits=$(scan_tree "$GT/meta") || gate_fail "sweep tool failed: $GATE_ERR"
while IFS= read -r line; do [ -n "$line" ] && hits+=$'\n'"commit-text: $line"; done <<< "$metahits"
nhits=0; report=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  nhits=$((nhits+1)); report+="  ${line:0:200}"$'\n'
done <<< "$hits"
if [ "$nhits" -gt 0 ]; then
  printf 'build-export: GATE hits (category|file token); only the exact public author URL, repo path and noreply address pass:\n%s' "$report" >&2
  gate_fail "$nhits violation(s)"
fi

# 4. one commit. No global or system git config, no hooks, no template: the commit carries only what is set here.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL"
git -C "$OUT" init -q -b main --template= || { echo "build-export: git init failed" >&2; exit 2; }
git -C "$OUT" add -A || exit 2
TZ=UTC git -C "$OUT" -c commit.gpgsign=false -c core.hooksPath=/dev/null commit -q -m "$MSG" || { echo "build-export: git commit failed" >&2; exit 2; }

n=$(git -C "$OUT" rev-list --count HEAD)
[ "$n" = 1 ] || { echo "build-export: expected 1 commit, found $n" >&2; exit 2; }
echo "source:  $SHA"
echo "export:  $(git -C "$OUT" rev-parse HEAD)"
echo "tree:    $(git -C "$OUT" rev-parse 'HEAD^{tree}')"
echo "author:  $(git -C "$OUT" log -1 --format='%an <%ae> | %cn <%ce>')"
nfiles=0; while IFS= read -r _; do nfiles=$((nfiles+1)); done < <(git -C "$OUT" ls-files)
echo "files:   $nfiles"
echo "gate:    clean (key shapes, private names, emails, URLs, private IPs, home paths)"
exit 0
