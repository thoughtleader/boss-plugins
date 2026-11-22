# Sourced by the shims in this directory. shim_find NAME prints the real tool's absolute path (PATH without this directory);
# shim_js TOOL ARGS... runs the node fallback in ../gnu-compat.js.
shim_dir=$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)
IFS=: read -r -a _shim_parts <<< "$PATH"
shim_find(){ local d; for d in "${_shim_parts[@]}"; do [ "$d" = "$shim_dir" ] && continue; [ -f "$d/$1" ] && [ -x "$d/$1" ] && { printf '%s' "$d/$1"; return 0; }; done; return 1; }
shim_js(){ exec node "$shim_dir/../gnu-compat.js" "$@"; }
