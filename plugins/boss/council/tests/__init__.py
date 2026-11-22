"""Council tests. Always run in a scratch environment, whatever the caller's holds.

A Claude session exports BOSS_PROGRESS_DIR (the owner's real progress dir) and the tests run under the owner's real HOME, so
anything the suite resolved from the environment or from `~` used to land in the real ~/.claude/boss/runs/council and
~/.claude-council. This module runs first (before any test module is imported) and OVERRIDES, never defaults:

  HOME, CLAUDE_CONFIG_DIR       a throwaway home (so `~/.claude-council`, `~/.claude/boss`, `~/.claude/council` are scratch)
  BOSS_PROGRESS_DIR             where councillib/progress_mirror.py publishes boss.progress/1 snapshots
  BOSS_DATA, ORCH_DIR, WFP_STATE_DIR   the orchestrator's data/state dirs, in case a test or a child process reads them
  BOSS_PROGRESS_LIB             unset (the default is council/progress.py in this tree)
  socket.socket.connect, connect_ex   refuse any address that is not loopback or a unix socket (see the end of this file)

`config.load()` reads the base config from $COUNCIL_CONFIG first: the suite pins it to tests/fixtures/config.test.toml, so
results never depend on whether the machine has a user config at <Claude config dir>/council/config.toml.
Test files may still set their own values with mock.patch.dict; those are restored afterwards."""
import atexit
import os
import shutil
import sys
import tempfile

# C-bytecode: never write __pycache__ into the plugin dir (the entry shebangs run -E, which ignores PYTHONDONTWRITEBYTECODE)
sys.dont_write_bytecode = True

# A caller's very long TMPDIR (a CI runner, a deep scratch folder) makes the temp paths long enough that product output which clips
# long lines (the dry-run argv line, the preflight table) no longer holds the whole path the test compares it with. Normal TMPDIRs
# (macOS /var/folders/.../T included, nested in a wrapper's scratch folder) stay far below the limit and are used as they are.
if len(tempfile.gettempdir()) > 120 and os.path.isdir("/tmp") and os.access("/tmp", os.W_OK):
    tempfile.tempdir = "/tmp"
    os.environ["TMPDIR"] = "/tmp"
_root = tempfile.mkdtemp(prefix="council-test-env-")
atexit.register(shutil.rmtree, _root, True)
_home = os.path.join(_root, "home")
os.makedirs(os.path.join(_home, ".claude"))
os.environ.update({
    "HOME": _home,
    "CLAUDE_CONFIG_DIR": os.path.join(_home, ".claude"),
    "BOSS_PROGRESS_DIR": os.path.join(_root, "progress"),
    "BOSS_DATA": os.path.join(_root, "data"),
    "ORCH_DIR": os.path.join(_root, "orch"),
    "WFP_STATE_DIR": os.path.join(_root, "wfp"),
})
os.environ.pop("BOSS_PROGRESS_LIB", None)
os.environ["COUNCIL_CONFIG"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "config.test.toml")
SCRATCH_ROOT = _root

# Tests that read /proc (the per-seat environment scan, process start ticks, the parent walk). The council works without /proc on
# macOS, but those readers degrade to "unknown" there (README, Platform), so these tests run on Linux only; run_suite prints each skip.
import unittest  # noqa: E402
NO_PROC = not os.path.isdir("/proc/self")
needs_proc = unittest.skipIf(NO_PROC, "no /proc here (macOS): this reads /proc, which is Linux only (council README, Platform)")


# Network block. No test may reach a real host: connect and connect_ex raise for any address that is not loopback or a unix
# socket. This lives in the package init so it covers `run_suite.py`, `unittest discover` and a direct `python3 -m unittest
# tests.<name>` alike. It guards this Python process only (a child process is kept off the network by the shims and the unset
# keys that test/council-tests.sh --full sets up). RuntimeError, not OSError: code that catches OSError around a connect must not
# swallow the block and let a test pass by accident.
import ipaddress  # noqa: E402
import socket  # noqa: E402


class NetworkBlocked(RuntimeError):
    pass


def _allowed_address(sock, address):
    if sock.family == getattr(socket, "AF_UNIX", None):
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except (ValueError, AttributeError):
        return False                      # a host name other than localhost: refused, never resolved
    return ip.is_loopback or (ip.version == 6 and ip.ipv4_mapped is not None and ip.ipv4_mapped.is_loopback)


_real_connect, _real_connect_ex = socket.socket.connect, socket.socket.connect_ex


def _guarded_connect(self, address):
    if not _allowed_address(self, address):
        self.close()                      # create_connection closes only on OSError: do not leak the socket
        raise NetworkBlocked("test network block: connect to %r refused (only loopback and unix sockets)" % (address,))
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _allowed_address(self, address):
        self.close()
        raise NetworkBlocked("test network block: connect_ex to %r refused (only loopback and unix sockets)" % (address,))
    return _real_connect_ex(self, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
