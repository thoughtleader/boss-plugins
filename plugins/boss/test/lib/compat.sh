# Sourced by the shell suites (first lines, after `set -u`): on macOS, or a host whose date, touch, stat, md5sum or wc (or paste without a file operand) lack the GNU forms the
# suites call (macOS), put test/lib/compat-bin first on PATH. The shims run the real tool whenever it is GNU, so on Linux this
# changes nothing. Test-side only: the shipped plugin calls none of these (BSD-safe), and the /bin/bash 3.2 CI step checks that.
_compat_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# BOSS_COMPAT_FORCE_JS=1 forces the shims and their node fallbacks on any host (how they are tested on Linux)
if [ -n "${BOSS_COMPAT_FORCE_JS:-}" ] || [ "$(uname -s)" = Darwin ] || ! date -d @0 +%s%N >/dev/null 2>&1 || ! stat -c%s / >/dev/null 2>&1 || ! touch --version >/dev/null 2>&1 \
   || ! command -v md5sum >/dev/null 2>&1 || [ "$(wc -l </dev/null)" != 0 ] || ! paste -sd, </dev/null >/dev/null 2>&1; then
  case ":$PATH:" in *":$_compat_dir/compat-bin:"*) ;; *) export PATH="$_compat_dir/compat-bin:$PATH";; esac
fi
unset _compat_dir
