#!/usr/bin/env bash
# The leaked-key scan ships inside the plugin (plugins/boss/council/tools/secret-scan.sh, the one copy, used by
# `council.py ask`); this root path keeps working for CI and the README. Same arguments, output and exit codes.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/plugins/boss/council/tools/secret-scan.sh" "$@"
