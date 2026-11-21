"""Seat supervisor: one selectors loop over every seat, smart timeouts, no orphans.

Each seat runs as  python3 -Es reap.py <grace_s> -- <argv...>  in its own
process group (pgid = the reaper's pid). See reap.py for the reaper contract.

Windows (Limits):
- stream:   idle_s since the last output byte (stdout or stderr).
- buffered: deadline_s since start (None = ceiling only).
- watch:    no idle or deadline window and no CPU/io extension: nothing is killed
            on elapsed time except a finite ceiling. A seat ends when it exits, at
            its ceiling, or through an explicit terminate(key, reason) / cut(key).
- ceiling_s since start holds in every mode and is never extended
  (None = no ceiling timer).

Watch mode output (rc 101 rule, row 25): the CLI's stdout and stderr are files on
disk, never pipes (Launch.stdout_path / stderr_path, else a private mode-600 temp
file removed after the result). The Supervisor tails them each poll, so no
supervisor-side failure (a raising callback or sampler, a stalled poll loop, a
crashed reader) can make the CLI hit EPIPE: it keeps writing to disk until it
exits. on_output and sampler exceptions are caught in watch mode.

Soft notice: WatchLimits.soft_at_s makes poll() return Notice("soft", key, t) once,
t = seconds since start. It only informs the caller and never kills. Seats
without soft_at_s never produce one, so legacy callers see only (key, Result).

Sampler seam: Supervisor(sampler=f). For a seat with WatchLimits.sample_s, f(SeatInfo)
is called at most once every sample_s (first at start + sample_s) while the seat
runs and no kill is in progress. Its return value (or a SamplerError if it
raised) is exposed as sup.liveness(key). The verdict never changes a status.

Explicit reap: terminate(key, reason) kills the seat with status = reason and
keeps the partial output (truncated=True). A seat that already exited on its own
keeps its real status.

Check-in: 1 s before an idle or deadline expiry the Supervisor samples CPU
(utime+stime+cutime+cstime from /proc/<pid>/stat) and I/O (rchar+wchar from
/proc/<pid>/io) summed over the seat's group, reaper excluded. At expiry, if
either rose and extensions < max_extensions, the window grows by
window * extension_factor (capped at the ceiling); otherwise the seat is killed.

Completion: a seat is done when its reaper exits. Any survivor left in its
group (a grandchild still holding the pipes) is SIGKILLed while the reaper is
still an unreaped zombie (so the pgid can't be reused), the pipes are drained
for at most DRAIN_S, and the status comes from the reaper's exit code.

Kill: SIGTERM to the reaper (it TERMs the group, waits grace_s, then KILLs),
SIGKILL to the group as a backstop after grace_s + 1. Every kill or cut sets
truncated=True and keeps the captured output.

Callbacks:
- start(..., on_output=f): f(chunk: bytes) for every stdout chunk of that seat.
- install_signal_handlers(sup, on_signal): on_signal() with no arguments; the
  signal number is on sup.last_signal.

Single-threaded: call every method from the main thread.
"""
from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import NamedTuple

REAP_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reap.py")
STDERR_TAIL = 4096
DRAIN_S = 0.5
MAX_WAIT_S = 0.25          # cap on one select step, so reaper exits are seen fast
SAMPLE_LEAD_S = 1.0
READ_CHUNK = 65536
MODES = ("stream", "buffered", "watch")


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str]
    cwd: str
    stdin_path: str | None = None        # file-backed stdin, never a pipe the loop feeds
    final_text_path: str | None = None   # e.g. codex -o file (read by the adapter)
    # watch mode only: capture files (created O_EXCL, mode 600, kept). None = a
    # private temp file, removed once the Result is built.
    stdout_path: str | None = None
    stderr_path: str | None = None


@dataclass
class Limits:
    mode: str                 # "stream" | "buffered" | "watch"
    idle_s: float             # stream only
    ceiling_s: float | None   # None = no ceiling timer
    deadline_s: float | None  # buffered only
    grace_s: float = 5
    max_extensions: int = 1
    extension_factor: float = 1.5


@dataclass
class WatchLimits(Limits):
    """Limits plus the row-27 knobs (any mode). Kept off Limits on purpose: drift.py
    snapshots dataclasses.asdict(limits), so plain Limits must keep its seven fields;
    these keys reach a drift snapshot only once a caller actually uses them."""
    soft_at_s: float | None = None   # one Notice("soft", key, t) from poll(); never kills
    sample_s: float | None = None    # sampler period for this seat (None = never sampled)


@dataclass
class Result:
    status: str   # ok|exit_<n>|idle_kill|deadline_kill|ceiling_kill|cut|signal_kill|spawn_error
    exit_code: int | None
    stdout: bytes
    stderr_tail: bytes        # last 4 KiB
    first_byte_s: float | None
    max_gap_s: float
    total_s: float
    out_bytes: int
    extensions: int
    truncated: bool           # True on every kill/cut
    stdout_path: str | None = None   # watch mode, caller-supplied capture files only
    stderr_path: str | None = None


class Notice(NamedTuple):
    """A poll() item that is not a Result: ("soft", key, t), t = seconds since start."""
    kind: str
    key: str
    t: float


@dataclass(frozen=True)
class SeatInfo:
    """What the sampler sees for one running seat (times in seconds since start)."""
    key: str
    mode: str
    pid: int                  # reaper pid = the seat's pgid; the CLI is its child
    tag: str | None           # launch.env["COUNCIL_SEAT_TAG"], if set
    started: float            # time.monotonic() at start
    elapsed_s: float
    out_bytes: int
    err_bytes: int            # every stderr byte seen (not just the tail)
    first_byte_s: float | None
    last_out_s: float | None  # at the last stdout byte
    last_err_s: float | None  # at the last stderr byte
    stdout_path: str | None   # watch mode capture files (temp ones included)
    stderr_path: str | None


@dataclass(frozen=True)
class SamplerError:
    """liveness(key) value when the sampler raised; the seat is unaffected."""
    error: str


@dataclass
class _Seat:
    key: str
    limits: Limits
    proc: subprocess.Popen | None
    start: float
    on_output: object = None
    out: bytearray = field(default_factory=bytearray)
    err: bytearray = field(default_factory=bytearray)
    fds: dict = field(default_factory=dict)       # fd -> "out" | "err"
    first_byte_s: float | None = None
    last_arrival: float = 0.0
    max_gap_s: float = 0.0
    window: float | None = None                   # current window length
    window_end: float | None = None               # idle/deadline expiry (monotonic)
    ceiling_at: float | None = None
    sample: tuple | None = None                   # (window_end, cpu, io)
    extensions: int = 0
    kill_reason: str | None = None
    kill_backstop: float | None = None
    backstop_sent: bool = False
    exit_code: int | None = None
    reaper_signaled: bool = False                 # reaper itself died by a signal
    reaped: bool = False
    drain_until: float | None = None
    watch: bool = False
    tag: str | None = None
    err_bytes: int = 0
    last_out: float | None = None
    last_err: float | None = None
    soft_at: float | None = None
    soft_sent: bool = False
    next_sample: float | None = None
    out_path: str | None = None
    err_path: str | None = None
    temp_dir: str | None = None                   # private capture dir, removed at finalize
    callback_errors: int = 0


def _stat_fields(pid):
    with open("/proc/%d/stat" % pid, "rb") as f:
        data = f.read()
    return data[data.rfind(b")") + 2:].split()


def _have_proc():
    return os.path.isdir("/proc/self")


def _cpu_seconds(text):
    """`ps -o time=` ([[dd-]hh:]mm:ss[.ff]) -> seconds, or 0.0 when it does not parse."""
    try:
        days, _, rest = text.rpartition("-")
        secs = 0.0
        for part in rest.split(":"):
            secs = secs * 60 + float(part)
        return secs + (int(days) * 86400 if days else 0)
    except ValueError:
        return 0.0


def _ps_group(pgid, exclude=()):
    """No /proc (macOS, BSD): [(pid, cpu_seconds)] of the live members of pgid from one `ps -A`, or [] when ps cannot run.
    CPU only, no IO: the activity extension there rests on CPU time alone, at ps's 1-second resolution."""
    try:
        r = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat=,time="], capture_output=True, text=True, timeout=5,
                           env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in r.stdout.splitlines() if r.returncode == 0 else ():
        f = line.split()
        if len(f) < 4 or not f[0].isdigit() or not f[1].isdigit() or f[2].startswith("Z"):
            continue
        if int(f[1]) == pgid and int(f[0]) not in exclude:
            out.append((int(f[0]), _cpu_seconds(f[3])))
    return out


def _group_pids(pgid, exclude=()):
    if not _have_proc():
        return [pid for pid, _ in _ps_group(pgid, exclude)]
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
            fields = _stat_fields(pid)
        except OSError:
            continue
        if len(fields) < 3 or fields[0] == b"Z":
            continue
        if int(fields[2]) == pgid:
            out.append(pid)
    return out


def _activity(pgid, reaper_pid):
    """(cpu_ticks, io_bytes) summed over the seat group, reaper excluded. Without /proc: (cpu_seconds * 100, 0) from ps."""
    if not _have_proc():
        return int(sum(c for _, c in _ps_group(pgid, (reaper_pid,))) * 100), 0
    cpu = io = 0
    for pid in _group_pids(pgid, exclude=(reaper_pid,)):
        try:
            f = _stat_fields(pid)
            # after "comm)": state=0 ... utime=11 stime=12 cutime=13 cstime=14
            cpu += int(f[11]) + int(f[12]) + int(f[13]) + int(f[14])
        except (OSError, IndexError, ValueError):
            continue
        try:
            with open("/proc/%d/io" % pid, "rb") as fh:
                for line in fh:
                    if line.startswith((b"rchar:", b"wchar:")):
                        io += int(line.split()[1])
        except (OSError, ValueError):
            pass
    return cpu, io


def _killpg(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _open_capture(key, launch):
    """Watch mode: (out_path, err_path, temp_dir, {"out": wfd, "err": wfd}, {rfd: tag}).

    Write fds go to the child; read fds stay with the Supervisor. On failure
    everything opened here is closed and the temp dir removed, then it re-raises.
    """
    temp_dir = None
    wfds, rfds = {}, {}
    try:
        if launch.stdout_path is None or launch.stderr_path is None:
            temp_dir = tempfile.mkdtemp(prefix="council-watch-")   # mode 700
        safe = re.sub(r"[^A-Za-z0-9._~-]", "_", key) or "seat"
        paths = {"out": launch.stdout_path or os.path.join(temp_dir, safe + ".stdout"),
                 "err": launch.stderr_path or os.path.join(temp_dir, safe + ".stderr")}
        for tag in ("out", "err"):
            # O_EXCL: a capture is a disk record and is never overwritten
            wfds[tag] = os.open(paths[tag], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
                                | os.O_CLOEXEC, 0o600)
            rfds[os.open(paths[tag], os.O_RDONLY | os.O_CLOEXEC)] = tag
    except BaseException:
        for fd in list(wfds.values()) + list(rfds):
            os.close(fd)
        _remove_temp(temp_dir)
        raise
    return paths["out"], paths["err"], temp_dir, wfds, rfds


def _remove_temp(temp_dir):
    if temp_dir is None:
        return
    try:
        for name in os.listdir(temp_dir):
            os.unlink(os.path.join(temp_dir, name))
        os.rmdir(temp_dir)
    except OSError:
        pass


class Supervisor:
    """Single-threaded; spawns only from the calling (main) thread."""

    def __init__(self, sampler=None):
        self._sel = selectors.DefaultSelector()
        self._seats: dict[str, _Seat] = {}
        self._pending: list = []          # (key, Result) and Notice items
        self._liveness: dict[str, object] = {}
        self.sampler = sampler            # f(SeatInfo) -> verdict, see the module doc
        self.last_signal: int | None = None

    # ---- public -----------------------------------------------------------
    def start(self, key: str, launch: Launch, limits: Limits, on_output=None) -> None:
        if key in self._seats:
            raise ValueError("seat already running: %s" % key)
        if limits.mode not in MODES:
            raise ValueError("Limits.mode must be 'stream', 'buffered' or 'watch'")
        soft_at_s = getattr(limits, "soft_at_s", None)
        sample_s = getattr(limits, "sample_s", None)
        if sample_s is not None and not sample_s > 0:
            raise ValueError("sample_s must be > 0 or None")
        now = time.monotonic()
        watch = limits.mode == "watch"
        argv = [sys.executable or "/usr/bin/python3", "-Es", REAP_PY,
                str(limits.grace_s), "--", *launch.argv]
        stdin = None
        cap = None
        try:
            if watch:
                cap = _open_capture(key, launch)
            stdin = open(launch.stdin_path, "rb") if launch.stdin_path else subprocess.DEVNULL
            out_to = cap[3]["out"] if cap else subprocess.PIPE
            err_to = cap[3]["err"] if cap else subprocess.PIPE
            proc = subprocess.Popen(
                argv, stdin=stdin, stdout=out_to, stderr=err_to,
                process_group=0, env=launch.env, cwd=launch.cwd, close_fds=True)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            if cap is not None:
                for fd in cap[4]:
                    os.close(fd)
                _remove_temp(cap[2])
            self._pending.append((key, Result(
                status="spawn_error", exit_code=None, stdout=b"",
                stderr_tail=("spawn_error: %s" % type(e).__name__).encode(),
                first_byte_s=None, max_gap_s=0.0, total_s=0.0, out_bytes=0,
                extensions=0, truncated=False)))
            return
        finally:
            if stdin not in (None, subprocess.DEVNULL):
                stdin.close()
            if cap is not None:
                for fd in cap[3].values():
                    os.close(fd)          # the child holds its own copies
        seat = _Seat(key=key, limits=limits, proc=proc, start=now, on_output=on_output,
                     last_arrival=now,
                     ceiling_at=None if limits.ceiling_s is None else now + limits.ceiling_s)
        seat.tag = (launch.env or {}).get("COUNCIL_SEAT_TAG")
        if soft_at_s is not None:
            seat.soft_at = now + soft_at_s
        if sample_s is not None:
            seat.next_sample = now + sample_s
        self._liveness.pop(key, None)
        if watch:
            seat.watch = True
            seat.out_path, seat.err_path, seat.temp_dir = cap[0], cap[1], cap[2]
            seat.fds.update(cap[4])      # regular files: tailed each poll, never selected
        else:
            if limits.mode == "stream":
                seat.window = limits.idle_s
            else:
                seat.window = limits.deadline_s
            seat.window_end = None if seat.window is None else now + seat.window
            for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
                fd = stream.fileno()
                os.set_blocking(fd, False)
                seat.fds[fd] = tag
                self._sel.register(fd, selectors.EVENT_READ, key)
        self._seats[key] = seat

    def terminate(self, key: str, reason: str) -> bool:
        """Kill a running seat with status = reason, keeping its partial output.

        For a later row to call on a proven terminal state. Returns False (and
        does nothing) for an unknown or finished key, a seat already being
        killed, or a seat whose process already exited (its real status stands).
        """
        if not isinstance(reason, str) or not reason or reason == "ok" \
                or reason.startswith("exit_"):
            raise ValueError("terminate reason must be a status name other than ok/exit_*")
        seat = self._seats.get(key)
        if seat is None or seat.reaped or seat.drain_until is not None \
                or seat.kill_reason is not None:
            return False
        if self._reaper_exited(seat):
            return False
        self._begin_kill(seat, reason, time.monotonic())
        return True

    def liveness(self, key: str):
        """The sampler's last verdict for key (kept after the seat ends), or None."""
        return self._liveness.get(key)

    def info(self, key: str) -> SeatInfo | None:
        """A SeatInfo snapshot of a running seat, or None."""
        seat = self._seats.get(key)
        return self._info(seat, time.monotonic()) if seat is not None else None

    def stderr_tail(self, key: str) -> bytes:
        """The last STDERR_TAIL bytes of a running seat's stderr (b"" when it is not running). Read-only: the live
        status (status.json) counts an OpenRouter seat child's `openrouter retry N` lines from it."""
        seat = self._seats.get(key)
        return bytes(seat.err) if seat is not None else b""

    def cut(self, key: str) -> None:
        seat = self._seats.get(key)
        if seat is not None:
            self._begin_kill(seat, "cut", time.monotonic())

    def poll(self, timeout: float) -> list[tuple[str, Result]]:
        """One selectors step; returns the seats that finished in it."""
        now = time.monotonic()
        wait = min(max(timeout, 0.0), MAX_WAIT_S)
        nxt = self._next_timer()
        if nxt is not None:
            wait = min(wait, max(nxt - now, 0.0))
        if self._pending:
            wait = 0.0
        if self._sel.get_map():
            events = self._sel.select(wait)
        else:
            if wait > 0:
                time.sleep(wait)
            events = []
        now = time.monotonic()
        for skey, _mask in events:
            seat = self._seats.get(skey.data)
            if seat is not None:
                self._read(seat, skey.fd, now)
        now = time.monotonic()
        for seat in list(self._seats.values()):
            self._tick(seat, now)
        out, self._pending = self._pending, []
        return out

    def running(self) -> list[str]:
        return list(self._seats)

    def pgid(self, key: str) -> int | None:
        """The seat's process group id (= its reaper's pid), or None."""
        seat = self._seats.get(key)
        return seat.proc.pid if seat and seat.proc else None

    def kill_all(self) -> None:
        """Kill every running seat (status signal_kill) and reap it.

        Signal-handler safe: only kill/waitid/waitpid, no selector work, no
        Popen.wait. Results are returned by the next poll().
        """
        now = time.monotonic()
        seats = list(self._seats.values())
        for seat in seats:
            if seat.kill_reason is None:
                seat.kill_reason = "signal_kill"
            if not seat.reaped:
                try:
                    os.kill(seat.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        grace = max((s.limits.grace_s for s in seats), default=0)
        end = now + grace + 1
        while time.monotonic() < end:
            if all(self._reaper_exited(s) for s in seats):
                break
            time.sleep(0.05)
        for seat in seats:
            if not seat.reaped:
                self._reap(seat)   # killpg KILL survivors + reaper backstop, then wait
        for seat in seats:
            self._finalize(seat, time.monotonic())

    # ---- internals ---------------------------------------------------------
    def _next_timer(self):
        times = []
        for s in self._seats.values():
            if s.drain_until is not None:
                times.append(s.drain_until)
            elif s.kill_reason is not None:
                if s.kill_backstop is not None and not s.backstop_sent:
                    times.append(s.kill_backstop)
            else:
                if s.ceiling_at is not None:
                    times.append(s.ceiling_at)
                if s.soft_at is not None and not s.soft_sent:
                    times.append(s.soft_at)
                if s.next_sample is not None and self.sampler is not None:
                    times.append(s.next_sample)
                if s.window_end is not None:
                    times.append(s.window_end)
                    if s.sample is None or s.sample[0] != s.window_end:
                        times.append(s.window_end - SAMPLE_LEAD_S)
        return min(times) if times else None

    def _read(self, seat, fd, now):
        try:
            data = os.read(fd, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._close_fd(seat, fd)
            return
        gap = now - seat.last_arrival
        if gap > seat.max_gap_s:
            seat.max_gap_s = gap
        seat.last_arrival = now
        if seat.fds.get(fd) == "out":
            if seat.first_byte_s is None:
                seat.first_byte_s = now - seat.start
            seat.out += data
            seat.last_out = now
            if seat.on_output is not None:
                seat.on_output(data)
        else:
            seat.err += data
            seat.err_bytes += len(data)
            seat.last_err = now
            if len(seat.err) > STDERR_TAIL:
                del seat.err[:-STDERR_TAIL]
        if (seat.limits.mode == "stream" and seat.kill_reason is None
                and seat.drain_until is None):
            seat.window = seat.limits.idle_s
            seat.window_end = now + seat.window

    def _close_fd(self, seat, fd):
        if fd in seat.fds:
            if seat.watch:
                try:
                    os.close(fd)      # our read fd of a capture file
                except OSError:
                    pass
            else:
                try:
                    self._sel.unregister(fd)
                except (KeyError, ValueError):
                    pass
            del seat.fds[fd]

    def _ingest(self, seat, tag, data, now, gap=True):
        """Watch mode: account one chunk read from a capture file."""
        if gap:
            g = now - seat.last_arrival
            if g > seat.max_gap_s:
                seat.max_gap_s = g
            seat.last_arrival = now
        if tag == "out":
            if seat.first_byte_s is None:
                seat.first_byte_s = now - seat.start
            seat.out += data
            seat.last_out = now
            if seat.on_output is not None:
                try:
                    seat.on_output(data)
                except Exception:
                    seat.callback_errors += 1   # the drain never stops for a callback
        else:
            seat.err += data
            seat.err_bytes += len(data)
            seat.last_err = now
            if len(seat.err) > STDERR_TAIL:
                del seat.err[:-STDERR_TAIL]

    def _tail(self, seat, now, gap=True):
        """Watch mode: read whatever the CLI appended to its capture files.

        b"" from a regular file means "nothing new yet", not EOF.
        """
        for fd, tag in list(seat.fds.items()):
            while True:
                try:
                    data = os.read(fd, READ_CHUNK)
                except OSError:
                    break
                if not data:
                    break
                self._ingest(seat, tag, data, now, gap)

    def _info(self, seat, now):
        rel = (lambda t: None if t is None else t - seat.start)
        return SeatInfo(
            key=seat.key, mode=seat.limits.mode, pid=seat.proc.pid, tag=seat.tag,
            started=seat.start, elapsed_s=now - seat.start, out_bytes=len(seat.out),
            err_bytes=seat.err_bytes, first_byte_s=seat.first_byte_s,
            last_out_s=rel(seat.last_out), last_err_s=rel(seat.last_err),
            stdout_path=seat.out_path, stderr_path=seat.err_path)

    def _soft_and_sample(self, seat, now):
        if seat.soft_at is not None and not seat.soft_sent and now >= seat.soft_at:
            seat.soft_sent = True
            self._pending.append(Notice("soft", seat.key, now - seat.start))
        if self.sampler is not None and seat.next_sample is not None \
                and now >= seat.next_sample:
            seat.next_sample = now + seat.limits.sample_s     # set only on WatchLimits
            try:
                verdict = self.sampler(self._info(seat, now))
            except Exception as e:      # a sampler failure never touches the seat
                verdict = SamplerError("%s: %s" % (type(e).__name__, str(e)[:200]))
            self._liveness[seat.key] = verdict

    def _reaper_exited(self, seat):
        """True once the reaper has exited; does not reap it (WNOWAIT)."""
        if seat.reaped:
            return True
        try:
            info = os.waitid(os.P_PID, seat.proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return True
        return info is not None and info.si_pid != 0

    def _reap(self, seat):
        """Kill any survivors in the group, then reap the reaper."""
        pgid = seat.proc.pid
        _killpg(pgid, signal.SIGKILL)  # reaper still unreaped: pgid can't be reused
        try:
            _, status = os.waitpid(pgid, 0)
            code = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            code = -signal.SIGKILL
        seat.proc.returncode = code    # keep Popen.__del__/_cleanup off this pid
        seat.reaped = True
        if code < 0:
            seat.reaper_signaled = True
            seat.exit_code = None
        else:
            seat.exit_code = code

    def _begin_kill(self, seat, reason, now):
        if seat.kill_reason is not None or seat.reaped:
            return
        seat.kill_reason = reason
        try:
            os.kill(seat.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        seat.kill_backstop = now + seat.limits.grace_s + 1

    def _tick(self, seat, now):
        if seat.watch and seat.drain_until is None:
            self._tail(seat, now)
        if seat.drain_until is None and self._reaper_exited(seat):
            self._reap(seat)
            seat.drain_until = now + DRAIN_S
            if seat.watch:
                # files: every writer is gone, so one read to the end is complete
                self._finalize(seat, now)
                return
        if seat.drain_until is not None:
            if not seat.fds or now >= seat.drain_until:
                self._finalize(seat, now)
            return
        if seat.kill_reason is not None:
            if seat.kill_backstop is not None and now >= seat.kill_backstop \
                    and not seat.backstop_sent:
                _killpg(seat.proc.pid, signal.SIGKILL)
                seat.backstop_sent = True
            return
        if seat.ceiling_at is not None and now >= seat.ceiling_at:
            self._begin_kill(seat, "ceiling_kill", now)
            return
        self._soft_and_sample(seat, now)
        if seat.window_end is None:
            return
        if now >= seat.window_end:
            active = False
            if seat.sample is not None and seat.sample[0] == seat.window_end:
                cpu, io = _activity(seat.proc.pid, seat.proc.pid)
                active = cpu > seat.sample[1] or io > seat.sample[2]
            if active and seat.extensions < seat.limits.max_extensions:
                seat.extensions += 1
                end = seat.window_end + seat.window * seat.limits.extension_factor
                if seat.ceiling_at is not None:
                    end = min(end, seat.ceiling_at)
                seat.window_end = end
                seat.window = seat.window * seat.limits.extension_factor
                return
            reason = "idle_kill" if seat.limits.mode == "stream" else "deadline_kill"
            self._begin_kill(seat, reason, now)
            return
        if now >= seat.window_end - SAMPLE_LEAD_S and \
                (seat.sample is None or seat.sample[0] != seat.window_end):
            cpu, io = _activity(seat.proc.pid, seat.proc.pid)
            seat.sample = (seat.window_end, cpu, io)

    def _finalize(self, seat, now):
        if self._seats.get(seat.key) is not seat:
            return
        if seat.watch:
            self._tail(seat, now, gap=False)     # read the capture files to their end
        for fd in [] if seat.watch else list(seat.fds):
            # last non-blocking drain
            while True:
                try:
                    data = os.read(fd, READ_CHUNK)
                except (BlockingIOError, OSError):
                    break
                if not data:
                    break
                if seat.fds[fd] == "out":
                    if seat.first_byte_s is None:
                        seat.first_byte_s = now - seat.start
                    seat.out += data
                    if seat.on_output is not None:
                        seat.on_output(data)
                else:
                    seat.err += data
            self._close_fd(seat, fd)
        for fd in list(seat.fds):                # watch: our read fds of the files
            self._close_fd(seat, fd)
        for stream in (seat.proc.stdout, seat.proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass
        if seat.kill_reason is not None:
            status = seat.kill_reason
        elif seat.reaper_signaled:
            status = "signal_kill"
        elif seat.exit_code == 0:
            status = "ok"
        elif seat.exit_code == 127 and b"reap: spawn_error:" in seat.err:
            status = "spawn_error"
        else:
            status = "exit_%d" % seat.exit_code
        del self._seats[seat.key]
        kept = seat.temp_dir is None
        self._pending.append((seat.key, Result(
            status=status, exit_code=seat.exit_code, stdout=bytes(seat.out),
            stderr_tail=bytes(seat.err[-STDERR_TAIL:]), first_byte_s=seat.first_byte_s,
            max_gap_s=seat.max_gap_s, total_s=now - seat.start, out_bytes=len(seat.out),
            extensions=seat.extensions, truncated=seat.kill_reason is not None,
            stdout_path=seat.out_path if kept else None,
            stderr_path=seat.err_path if kept else None)))
        _remove_temp(seat.temp_dir)


def run_one(launch: Launch, limits: Limits) -> Result:
    sup = Supervisor()
    sup.start("one", launch, limits)
    while True:
        for item in sup.poll(1.0):
            if not isinstance(item, Notice):
                return item[1]


def install_signal_handlers(sup: Supervisor, on_signal) -> None:
    """Explicit TERM/INT/HUP handlers (override an inherited SIG_IGN).

    handler: on_signal() -> sup.kill_all() -> exit 128+sig. The signal number
    is on sup.last_signal while on_signal runs. A second signal while the
    first is handled is ignored.
    """
    state = {"busy": False}

    def handler(signum, _frame):
        if state["busy"]:
            return
        state["busy"] = True
        sup.last_signal = signum
        try:
            if on_signal is not None:
                on_signal()
        finally:
            try:
                sup.kill_all()
            finally:
                for stream in (sys.stdout, sys.stderr):
                    try:
                        stream.flush()
                    except Exception:
                        pass
                os._exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, handler)
