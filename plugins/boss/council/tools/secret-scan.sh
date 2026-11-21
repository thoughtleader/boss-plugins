#!/usr/bin/env bash
# secret-scan.sh - leaked-key scan and leftover-process check for the council runner.
#
#   secret-scan.sh [PATH...]            scan PATHs (default: ~/.claude/council ~/.claude-council)
#   secret-scan.sh --leftover [DIR...]  list processes whose cwd is under DIR (default: any cwd under
#                                       ${TMPDIR:-/tmp}/council-*, deleted or not)
#
# Key scan, two passes, output = FILE NAMES ONLY (never a key value, never a matching line):
#   1. the live key values (OpenRouter/qwen/dashscope env vars and the env block of
#      ~/.qwen/settings.json) as fixed strings, fed to grep through a pipe, never argv;
#   2. an `sk-or-v1-` / `sk-` regex for key-shaped strings no live value explains (a rotated key
#      left in a backup, a new key pasted into a note). Strings starting TESTSECRET are fixtures.
# Leftover check (plan-check R2-W3): ANY process, whatever its exe, whose cwd is under a seat
#   workdir. codex runs as node + a musl binary and qwen as node, so an exe filter misses them.
#   Prints "pid<TAB>comm<TAB>cwd" (never argv or environ).
# Exit 0 = nothing found, 1 = something found, 2 = usage or the scan could not run (grep missing or failing and nothing
# found; --leftover off Linux/WSL, where there is no /proc/<pid>/cwd): never 0 when the tools did not work. Hits win over a tool error.
set -u
export LC_ALL=C

if [ "${1:-}" = "--leftover" ]; then
  shift
  # Fail closed: the scan reads /proc/<pid>/cwd, which only Linux (and WSL) serves in full. macOS has no /proc, and
  # Git Bash or Cygwin list only their own pids, so an empty result there would mean "not checked", never "clean".
  if [ "$(uname -s 2>/dev/null)" != Linux ] || ! readlink -- "/proc/$$/cwd" >/dev/null 2>&1; then
    echo "secret-scan: --leftover needs Linux or WSL (/proc/<pid>/cwd) and cannot check for leftover processes here; not clean, not run" >&2
    exit 2
  fi
  # A finished council deletes its workdir (shutil.rmtree), so an orphan's cwd reads
  # "<dir> (deleted)". Match cwd strings, never the directory itself, and strip that suffix.
  dirs=()
  bases=()
  if [ "$#" -gt 0 ]; then
    for d in "$@"; do
      r=$(realpath -m -- "$d" 2>/dev/null) && [ -n "$r" ] || { echo "secret-scan: cannot resolve '$d' (realpath -m); not run" >&2; exit 2; }
      dirs+=("$r")
    done
  else
    for b in "${TMPDIR:-/tmp}" /tmp; do
      r=$(realpath -m -- "$b" 2>/dev/null) && [ -n "$r" ] || { echo "secret-scan: cannot resolve '$b' (realpath -m); not run" >&2; exit 2; }
      bases+=("$r")
    done
  fi
  found=0
  for p in /proc/[0-9]*; do
    pid=${p#/proc/}
    [ "$pid" = "$$" ] && continue
    cwd=$(readlink -- "$p/cwd" 2>/dev/null) || continue
    c=${cwd% (deleted)}
    hit=0
    for d in ${dirs[@]+"${dirs[@]}"}; do
      case "$c/" in "$d"/*) hit=1; break ;; esac
    done
    if [ "$hit" = 0 ]; then
      for b in ${bases[@]+"${bases[@]}"}; do
        case "$c" in "$b"/council-*) hit=1; break ;; esac
      done
    fi
    if [ "$hit" = 1 ]; then
      comm=$(cat -- "$p/comm" 2>/dev/null || echo '?')
      printf '%s\t%s\t%s\n' "$pid" "$comm" "$cwd"
      found=1
    fi
  done
  exit "$found"
fi

# the grep on PATH, not a fixed path: a missing grep is an error (exit 2), never a clean result
GREP=$(command -v grep) || { echo "secret-scan: grep not found; scan not run" >&2; exit 2; }
if [ "$#" -gt 0 ]; then
  paths=("$@")
else
  paths=("$HOME/.claude/council" "$HOME/.claude-council")
fi
existing=()
for p in "${paths[@]}"; do
  [ -e "$p" ] && existing+=("$p")
done
[ "${#existing[@]}" -gt 0 ] || exit 0

toolerr=0
tmp=$(mktemp -d) || exit 2
trap 'rm -rf "$tmp"' EXIT
chmod 700 "$tmp"

# Pass 1: live values as fixed strings. The values stay in a shell variable and reach grep through a
# pipe (builtin printf, so never argv). An empty pattern list would match every file, so skip then.
live=$({
  printenv OPENROUTER_API_KEY OPENROUTER_API_KEY_PERSONAL QWEN_API_KEY DASHSCOPE_API_KEY
  jq -r '.env // {} | .[] | select(type == "string")' "$HOME/.qwen/settings.json" 2>/dev/null
} | awk 'length >= 16')
if [ -n "$live" ]; then
  "$GREP" -rlaF -f <(printf '%s\n' "$live") -- "${existing[@]}" 2>/dev/null >> "$tmp/hits"
  [ "$?" -ge 2 ] && toolerr=1
fi
live=

# Pass 2: key-shaped strings (ERE, no PCRE: BSD grep has no -P), minus the TESTSECRET fixtures, which the loop drops
# by token. -o prints "file:token"; only the file name is kept.
"$GREP" -raoHE '(^|[^A-Za-z0-9])sk-(or-v1-)?[A-Za-z0-9_-]{16,}' -- "${existing[@]}" 2>/dev/null > "$tmp/tokens"
[ "$?" -ge 2 ] && toolerr=1
while IFS= read -r line; do
  [ -n "$line" ] || continue
  case "${line#*:}" in *sk-TESTSECRET*|*sk-or-v1-TESTSECRET*) continue;; esac
  printf '%s\n' "${line%%:*}" >> "$tmp/hits"
done < "$tmp/tokens"

if [ -s "$tmp/hits" ]; then
  sort -u "$tmp/hits" || exit 2
  exit 1
fi
[ "$toolerr" = 1 ] && { echo "secret-scan: grep failed; scan incomplete" >&2; exit 2; }
exit 0
