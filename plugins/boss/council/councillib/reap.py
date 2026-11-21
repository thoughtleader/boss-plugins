#!/usr/bin/env python3
"""Seat reaper: the direct child of the council Supervisor for every seat.

Usage: python3 -Es reap.py <grace_s> -- <argv...>

- Runs as the leader of its own process group (the Supervisor spawns it with
  process_group=0) and starts argv in that same group with inherited stdio.
- Exits with the child's exit code; a child killed by signal N exits 128+N.
- If its parent (the runner) dies, even by SIGKILL, it notices within
  POLL_S and SIGKILLs the whole group, itself included. This is how a
  grandchild (codex.js -> native codex, qwen relaunch) can't outlive the runner.
- On TERM, INT or HUP: ignores further signals, sends TERM to the group
  (itself excluded), waits up to grace_s for the group to empty, then SIGKILLs
  the group.

Detached (watch) seats, row 31 (D18): with COUNCIL_REAP_DETACH=1 in its env (the
variable is removed before argv starts, so the CLI never sees it):
- the death of its parent is NOT a kill: the seat keeps running (a watch call is
  reaped only on a proven terminal state, never because the runner went away);
- when argv exits (the seat's terminal state), every survivor is SIGKILLed: its
  group and every process tagged with its COUNCIL_SEAT_TAG (codex's native child
  has its own pgid), so nothing outlives a finished seat;
- on TERM, INT or HUP the tagged processes get TERM with the group, and a final
  SIGKILL with it;
- with COUNCIL_REAP_STATUS=<path> (also removed before argv starts) the reaper writes
  {"exit_code", "ended_at", "via"} to that file (tmp + fsync + rename) BEFORE it sweeps or
  exits, on every exit path. Once the runner is dead nobody waits on the reaper, so this file
  is the only record of how the call ended (row 40: the orphan sweep reads it).

Standalone and stdlib only: no ctypes, no PDEATHSIG.
"""
import errno
import os
import signal
import subprocess
import sys
import time

POLL_S = 0.1
SIGS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
DETACH_ENV = "COUNCIL_REAP_DETACH"
STATUS_ENV = "COUNCIL_REAP_STATUS"
TAG_ENV = "COUNCIL_SEAT_TAG"


def _have_proc():
    return os.path.isdir("/proc/self")


def _ps_members(pgid, exclude=()):
    """No /proc (macOS, BSD): live pids of pgid from one `ps -A`, or None when ps cannot run (the group is then unknown).
    ps runs in its own session, so it never counts itself as a member of the group."""
    try:
        r = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=5,
                           env=dict(os.environ, LC_ALL="C"), start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out = []
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) < 3 or not f[0].isdigit() or not f[1].isdigit() or f[2].startswith("Z"):
            continue
        if int(f[1]) == pgid and int(f[0]) not in exclude:
            out.append(int(f[0]))
    return out


def group_alive(pgid, exclude=()):
    """True while pgid has a live member besides exclude. True too when the group cannot be listed (no /proc and no
    ps): an unknown group is never read as an empty one, so the grace wait runs out and the SIGKILL still follows."""
    if _have_proc():
        return bool(group_members(pgid, exclude))
    members = _ps_members(pgid, exclude)
    return True if members is None else bool(members)


def group_unknown(pgid, exclude=()):
    """True when the group cannot be listed at all (no /proc and no working ps)."""
    return not _have_proc() and _ps_members(pgid, exclude) is None


def group_members(pgid, exclude=()):
    """Live (non-zombie) pids whose process group is pgid, minus exclude."""
    if not _have_proc():
        return _ps_members(pgid, exclude) or []
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid in exclude:
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as f:
                data = f.read()
        except OSError:
            continue
        fields = data[data.rfind(b")") + 2:].split()
        if len(fields) < 3 or fields[0] == b"Z":
            continue
        if int(fields[2]) == pgid:
            out.append(pid)
    return out


def tagged(tag, exclude=()):
    """Live pids whose /proc/<pid>/environ holds COUNCIL_SEAT_TAG=<tag>, minus exclude. Linux only: without /proc there is
    no portable way to read another process's environment, so it returns [] (the seat's own process group is still killed)."""
    if not tag:
        return []
    want = ("%s=%s" % (TAG_ENV, tag)).encode()
    out = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit() or int(name) in exclude:
            continue
        try:
            with open("/proc/%s/environ" % name, "rb") as f:
                env = f.read().split(b"\0")
            with open("/proc/%s/stat" % name, "rb") as f:
                data = f.read()
        except OSError:
            continue
        fields = data[data.rfind(b")") + 2:].split()
        if fields and fields[0] != b"Z" and want in env:
            out.append(int(name))
    return out


def _signal_pids(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def write_status(path, exit_code, via):
    """Atomic, durable end record: tmp file, fsync, rename. Never raises (a failed write must not change how
    the seat ends)."""
    if not path:
        return
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        try:
            data = ('{"exit_code": %d, "ended_at": %.3f, "via": "%s"}\n' % (exit_code, time.time(), via)).encode()
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _exit_code(status):
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def main(args):
    if len(args) < 3 or args[1] != "--":
        sys.stderr.write("reap: usage: reap.py <grace_s> -- <argv...>\n")
        return 2
    grace_s = float(args[0])
    cmd = args[2:]
    detach = os.environ.pop(DETACH_ENV, None) == "1"
    status_path = os.environ.pop(STATUS_ENV, None)      # the CLI never sees either variable
    tag = os.environ.get(TAG_ENV) if detach else None
    start_ppid = os.getppid()
    pgid = os.getpgrp()
    me = os.getpid()
    child = {"pid": None}

    def reap_child_nowait():
        pid = child["pid"]
        if pid is None:
            return
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            done = pid
        if done:
            child["pid"] = None

    def shutdown(signum, _frame):
        for s in SIGS:
            signal.signal(s, signal.SIG_IGN)
        write_status(status_path, 128 + signum, "signal")
        try:
            os.killpg(pgid, signal.SIGTERM)  # we ignore TERM now, so this skips us
        except ProcessLookupError:
            pass
        _signal_pids(tagged(tag, exclude=(me,)), signal.SIGTERM)
        end = time.monotonic() + grace_s
        while time.monotonic() < end:
            reap_child_nowait()
            if not group_alive(pgid, exclude=(me,)) and not tagged(tag, exclude=(me,)):
                os._exit(128 + signum)
            time.sleep(0.05)
        _signal_pids(tagged(tag, exclude=(me,)), signal.SIGKILL)
        os.killpg(pgid, signal.SIGKILL)  # includes us
        os._exit(128 + signum)

    def sweep():
        """A detached seat ended: no process of it may outlive it (its group + its tagged pids)."""
        _signal_pids(tagged(tag, exclude=(me,)), signal.SIGKILL)
        if group_unknown(pgid, exclude=(me,)):
            # no /proc and no ps: the members cannot be named, so kill the group whole. This reaper goes with it; its
            # end record was written before the sweep, and the process exits right after it anyway.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _signal_pids(group_members(pgid, exclude=(me,)), signal.SIGKILL)

    for s in SIGS:
        signal.signal(s, shutdown)

    try:
        proc = subprocess.Popen(cmd)  # same group, inherited stdio
    except OSError as e:
        name = errno.errorcode.get(e.errno, str(e.errno))
        sys.stderr.write("reap: spawn_error: %s\n" % name)
        sys.stderr.flush()
        write_status(status_path, 127, "spawn_error")
        os._exit(127)
    child["pid"] = proc.pid

    while True:
        try:
            done, status = os.waitpid(proc.pid, os.WNOHANG)
        except ChildProcessError:
            write_status(status_path, -1, "lost_child")   # unknown: the sweep reads it as no verdict
            os._exit(0)
        if done:
            proc.returncode = 0  # reaped here; keep Popen from re-waiting
            child["pid"] = None
            code = _exit_code(status)
            write_status(status_path, code, "exit")      # before the sweep: a record exists once nothing is alive
            if detach:
                sweep()
            os._exit(code)
        if os.getppid() != start_ppid and not detach:
            os.killpg(pgid, signal.SIGKILL)  # runner died: take the group down
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
