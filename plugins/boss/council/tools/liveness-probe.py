#!/bin/sh
''''exec python3 -Es "$0" "$@" # '''
# ^ sh runs line 2 as `exec python3 -Es "$0" "$@"` (-E ignores PYTHON* variables, -s skips the user site); Python reads it as a string and
#   skips it. No `/usr/bin/env -S` here: GNU env older than 8.30 rejects -S. It works wherever /bin/sh and python3 exist.
__doc__ = """Liveness probe: ONE real seat call (codex or agy) observed, never supervised.

  liveness-probe.py run --seat ID --question-file F --out DIR --label L
                    [--config P]... [--extra-arg A]... [--codex-c KEY=VAL]... [--env K=V]...
                    [--otlp [--otlp-protocol json|binary]] [--agy-log] [--no-print-timeout]
                    [--observer-kill-at S] [--sigstop-at S --sigstop-for D]
                    [--concurrent-with LABEL] [--end-at S] [--horizon 3600] [--drain-s 3] [--dry-run]

What it is for (row 20 of example-run): measure which progress signals fire during a seat's
silent phase (request start to first answer byte) on the REAL adapter build, with nothing cutting the call.
There is no Supervisor, no deadline, no idle clock and no ceiling: `_limits` is read ONLY to give
`Adapter.build` the number it needs (agy `--print-timeout` = ceiling + 60). The call ends by itself (exit),
or by one of the probe's own labelled controls: --end-at S, --horizon S (default 3600; D34: a free labelled
probe must not hang a row forever) or a signal to this tool. Each of those sends SIGTERM (after a SIGCONT)
to the probe's OWN tagged processes only and records `ended_by`; nothing is ever SIGKILLed. Survivors are
listed in the record and make the exit code 3.

Per call, in --out (dir 700, files 600; nothing is overwritten, an existing label is an error):
  <label>.stdout, <label>.stderr   in full (the 4 KiB stderr tail of the supervisor is not used here)
  <label>.samples.jsonl            one line per second: netwatch (tagged tree, ss tcp/unix), stdout/stderr/log
                                   bytes, OTLP event count, open tool steps (codex item.started without its
                                   item.completed; agy step_update not DONE), answer seen
  <label>.otlp.jsonl               one line per OTLP request (t, s, path, bytes, names; no body)
  <label>.agy.log                  agy --v=3 --log-file (--agy-log; created by agy)
  <label>.answer.md                the answer (codex: the -o file, else the extractor's text)
  <label>.started.json             written at launch (pid, tag): what --concurrent-with waits for
  <label>.json                     the record: exit, ended_by, total_s, first_stdout_byte_s,
                                   first_answer_byte_s, per-signal first event and max gap inside the silent
                                   phase, ss ESTAB evidence, daemon peers, observer / sigstop / horizon /
                                   concurrent results, answer_head <= 400, env as key names only.

Attribution is by a per-launch COUNCIL_SEAT_TAG in /proc/<pid>/environ plus ppid descendants (netwatch), not by
pgid. The shared codex app-server daemon (daemon.pid under $CODEX_HOME/app-server-daemon) is never signalled
and is recorded: `daemon_tagged` is true when a tagged launch started it (it inherits the tag).

--dry-run prints the exact argv (prompt redacted), the per-launch env additions and removals, the stdin file and
the OTLP sink placeholder, and launches nothing. The per-launch `-c` OTel flags it prints are the ones a live
run uses, with the sink port replaced by <sink-port>. The key shape of the OTel flags comes from the codex
0.163 binary's config schema (OtelConfigToml: exporter = {otlp-http = {endpoint, protocol}}, log_user_prompt)
and is UNVERIFIED until row 21's positive control sees an event.

--codex-c KEY=VAL appends `-c KEY=VAL` (codex only). --extra-arg A appends one raw argv element (use
--extra-arg=-c --extra-arg=k=v for pairs). Extra flags go into the seat's extra_args so the real build() puts
them where it puts the seat's own (before codex's trailing `-`, before agy's --print).

Exit: 0 the call ended by itself with an answer; 1 it ended with no answer; 2 usage or config error;
3 a tagged process survived the SIGTERM of a labelled control.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

# C-bytecode: never write __pycache__ into the plugin dir (the entry shebangs run -E, which ignores PYTHONDONTWRITEBYTECODE)
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from councillib import config, council_run, netwatch, providers  # noqa: E402
from councillib.otlp_sink import Sink  # noqa: E402
from councillib.supervise import Launch  # noqa: E402

WORK_PREFIX = "council-liveprobe-"     # under $TMPDIR, so secret-scan.sh --leftover (council-*) sees it
HEAD_MAX = 400
LONG_ARG = 512                         # an argv element this long is prompt-sized: never printed
DEFAULT_QUESTION = "Reply with exactly: OK"
SAMPLE_S = 1.0                         # netwatch sample period
LOOP_S = 0.1                           # byte / sink poll period
TERM_GRACE_S = 15.0
DRAIN_S = 3.0                          # keep reading the sink / files this long after the CLI exits
STEP_DONE = frozenset(("DONE", "COMPLETED", "ERROR", "FAILED", "CANCELLED", "CANCELED"))
SECRETISH = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|AUTH|CRED)", re.I)

RELAY_PY = ("import os,sys\n"
            "f=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)\n"
            "while True:\n"
            " b=os.read(0,65536)\n"
            " if not b: break\n"
            " os.write(f,b)\n")


# ---------------------------------------------------------------- pure helpers
def gap_stats(times, end):
    """Signal times (seconds since launch) -> first event, last event and max gap inside [0, end].

    The gaps include start -> first event and last event -> end, so a signal that never fires reports the
    whole phase as its gap. `count_total` also counts events after `end`."""
    ts = sorted(float(t) for t in times)
    pts = [t for t in ts if 0 <= t <= end]
    edges = [0.0] + pts + [float(end)]
    gap = max((b - a for a, b in zip(edges, edges[1:])), default=0.0)
    return {"count": len(pts), "count_total": len(ts), "first_s": round(pts[0], 3) if pts else None,
            "last_s": round(pts[-1], 3) if pts else None, "max_gap_s": round(max(gap, 0.0), 3)}


def otel_flags(port, protocol="json"):
    """The per-launch codex flags that point the OTel log exporter at the local sink. UNVERIFIED key shape."""
    exporter = ('otel.exporter={otlp-http={endpoint="http://127.0.0.1:%s/v1/logs",protocol="%s"}}'
                % (port, protocol))
    return ["-c", exporter, "-c", "otel.log_user_prompt=false"]


def agy_log_flags(path):
    return ["--v=3", "--log-file", path]


def strip_print_timeout(argv):
    """argv without `--print-timeout <value>` (also the `--print-timeout=<value>` form)."""
    out, i = [], 0
    while i < len(argv):
        a = str(argv[i])
        if a == "--print-timeout":
            i += 2
            continue
        if a.startswith("--print-timeout="):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def redact_argv(argv, prompt):
    p = (prompt or "").strip()
    out = []
    for a in argv:
        a = str(a)
        if (p and p in a) or len(a) > LONG_ARG:
            out.append("<prompt %d bytes>" % len(a.encode("utf-8")))
        else:
            out.append(providers._scrub(a))
    return out


def read_daemon_pid(path):
    """The codex app-server daemon pid from daemon.pid (JSON {"pid": N} or a bare integer), else None."""
    try:
        with open(path, "rb") as f:
            data = f.read(4096).decode("utf-8", "replace")
    except OSError:
        return None
    try:
        v = json.loads(data)
        if isinstance(v, dict) and isinstance(v.get("pid"), int):
            return v["pid"]
        if isinstance(v, int):
            return v
    except ValueError:
        pass
    m = re.search(r"\d+", data)
    return int(m.group(0)) if m else None


def default_daemon_pidfile():
    return os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
                        "app-server-daemon", "daemon.pid")


def parse_kv(items, what):
    out = []
    for it in items or ():
        if "=" not in it or it.startswith("="):
            raise ValueError("%s %r is not KEY=VAL" % (what, it))
        k, v = it.split("=", 1)
        out.append((k, v))
    return out


class StepTracker:
    """Open tool steps from stdout JSON lines. codex: item.started without item.completed (by item id).
    agy: step_update whose latest state is not terminal (by step_index)."""

    def __init__(self):
        self._buf = bytearray()
        self.codex = {}          # item id -> item type
        self.agy = {}            # step_index -> [step_type, state]
        self.max_open = 0

    def feed(self, chunk):
        for ev in providers._json_lines(self._buf, chunk):
            t = ev.get("type")
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if t == "item.started" and item.get("id") is not None:
                self.codex[item["id"]] = str(item.get("type") or "?")
            elif t in ("item.completed", "item.failed") and item.get("id") is not None:
                self.codex.pop(item["id"], None)
            elif ev.get("event") == "step_update" and isinstance(ev.get("step_update"), dict):
                su = ev["step_update"]
                if su.get("step_index") is not None:
                    self.agy[su["step_index"]] = [str(su.get("step_type") or "?"), str(su.get("state") or "")]
        self.max_open = max(self.max_open, self.open_count())

    def open_types(self):
        return sorted(list(self.codex.values())
                      + [v[0] for v in self.agy.values() if v[1].upper() not in STEP_DONE and v[1]])

    def open_count(self):
        return len(self.open_types())


# ---------------------------------------------------------------- file helpers
def _out_dir(path):
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_private(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _open_private(path, flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND):
    return os.open(path, flags, 0o600)


class JsonlOut:
    def __init__(self, path):
        self.path = path
        self.fd = _open_private(path)

    def write(self, rec):
        os.write(self.fd, (json.dumps(rec, default=str) + "\n").encode("utf-8"))

    def close(self):
        os.close(self.fd)


def _size(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _send(pids, sig):
    sent = []
    for p in pids:
        try:
            os.kill(p, sig)
            sent.append(p)
        except (ProcessLookupError, PermissionError):
            pass
    return sent


# ---------------------------------------------------------------- the observer
class Options:
    def __init__(self, **kw):
        self.observer_kill_at = None
        self.sigstop_at = None
        self.sigstop_for = 0.0
        self.end_at = None
        self.horizon = 3600.0
        self.drain_s = DRAIN_S
        self.term_grace_s = TERM_GRACE_S
        self.concurrent_with = None
        self.concurrent_wait_s = 600.0
        self.agy_log_path = None
        self.daemon_pidfile = None
        self.ss_runner = None
        self.proc = "/proc"
        self.sample_s = SAMPLE_S
        self.__dict__.update(kw)


def observe(launch, extractor, out, label, tag, opts, sink=None, meta=None):
    """Launch `launch` and watch it to the end. Returns the record dict (also written to <label>.json).

    Nothing here enforces a clock on the call: only the labelled controls in `opts` (end_at, horizon, a signal
    to this tool) ever signal it, and only its own tagged processes."""
    p = lambda ext: os.path.join(out, label + ext)          # noqa: E731
    rec = dict(meta or {})
    rec.update(label=label, tag=tag, started_wall=None)
    other = None
    if opts.concurrent_with:
        other = _wait_for_other(out, opts.concurrent_with, opts.concurrent_wait_s)
    daemon_pid = read_daemon_pid(opts.daemon_pidfile) if opts.daemon_pidfile else None
    stdout_path, stderr_path = p(".stdout"), p(".stderr")
    for path in (stdout_path, stderr_path):
        _write_private(path, b"")
    samples, otlp_out = JsonlOut(p(".samples.jsonl")), JsonlOut(p(".otlp.jsonl")) if sink else None
    env = dict(launch.env)
    env[netwatch.TAG_ENV] = tag

    # ---- launch (stdout straight to a file, or through an untagged relay for the observer-loss control)
    relay = None
    stdin = open(launch.stdin_path, "rb") if launch.stdin_path else subprocess.DEVNULL
    err_fd = _open_private(stderr_path)
    out_fd = None
    try:
        if opts.observer_kill_at is not None:
            r_fd, w_fd = os.pipe()
            renv = {k: v for k, v in os.environ.items() if k != netwatch.TAG_ENV}
            try:
                relay = subprocess.Popen([sys.executable, "-Es", "-c", RELAY_PY, stdout_path], stdin=r_fd,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=renv,
                                         close_fds=True)
            finally:
                os.close(r_fd)
            out_fd = w_fd
        else:
            out_fd = _open_private(stdout_path)
        wall0, t0 = time.time(), time.monotonic()
        proc = subprocess.Popen(list(launch.argv), env=env, cwd=launch.cwd, stdin=stdin, stdout=out_fd,
                                stderr=err_fd, close_fds=True, start_new_session=True)
    except OSError as e:
        if relay:
            relay.kill()
            relay.wait()
        samples.close()
        if otlp_out:
            otlp_out.close()
        rec.update(status="spawn_error", detail=providers._scrub("%s: %s" % (type(e).__name__, e), 200),
                   ended_by="spawn_error", exit_code=None)
        _write_private(p(".json"), json.dumps(rec, indent=1, default=str))
        return rec
    finally:
        # the parent must not hold the pipe's write end: killing the relay must break the CLI's stdout
        if out_fd is not None:
            os.close(out_fd)
        os.close(err_fd)
        if stdin is not subprocess.DEVNULL:
            stdin.close()
    rec["started_wall"] = wall0
    rec["pid"] = proc.pid
    _write_private(p(".started.json"), json.dumps({"pid": proc.pid, "tag": tag, "started_wall": wall0,
                                                   "label": label}))
    if other:
        rec["concurrent"] = {"with": opts.concurrent_with, "other_pid": other.get("pid"),
                             "other_tag": other.get("tag"),
                             "other_started_rel_s": round(other.get("started_wall", wall0) - wall0, 3),
                             "other_alive_at_start": _pid_alive(other["pid"])}

    tracker = StepTracker()
    arrivals = {}                  # signal name -> [seconds since launch]
    state = {"stdout": 0, "stderr": 0, "log": 0, "otlp_seen": 0, "first_stdout": None, "first_answer": None,
             "tcp_prev": {}, "next_sample": t0, "stopped_at": None, "cont_at": None, "relay_killed_at": None,
             "ended_by": None, "term_at": None, "stopped_pids": [], "sigstop_done": False,
             "daemon_tagged": False}
    est = {"samples": 0, "with_estab": 0, "ss_fail": 0, "run": 0.0, "max_run": 0.0, "last": None}
    owners_seen, peers_outside, unresolved = {}, {}, [0]
    tcp_peers, tree_max, children_max, open_steps = set(), [0], [0], []
    sample_rows = []
    stdout_f = open(stdout_path, "rb")

    def mark(name, at):
        arrivals.setdefault(name, []).append(at)

    def read_new():
        now = time.monotonic() - t0
        chunk = stdout_f.read(1 << 20)
        while chunk:
            state["stdout"] += len(chunk)
            if state["first_stdout"] is None:
                state["first_stdout"] = now
            mark("stdout", now)
            tracker.feed(chunk)
            try:
                extractor.feed(chunk)
                if state["first_answer"] is None and (extractor.partial_text() or "").strip():
                    state["first_answer"] = now
            except Exception:                        # noqa: BLE001 - an extractor bug must not stop the watch
                pass
            chunk = stdout_f.read(1 << 20)
        n = _size(stderr_path)
        if n > state["stderr"]:
            state["stderr"] = n
            mark("stderr", now)
        if opts.agy_log_path:
            n = _size(opts.agy_log_path)
            if n > state["log"]:
                state["log"] = n
                mark("agylog", now)
        if sink is not None:
            evs = sink.events()
            for e in evs[state["otlp_seen"]:]:
                at = e["t"] - wall0
                otlp_out.write({"t": e["t"], "s": round(at, 3), "path": e["path"], "bytes": e["bytes"],
                                "names": e["names"]})
                mark("otlp", at)
                for nm in e["names"]:
                    mark("otlp:" + nm, at)
            state["otlp_seen"] = len(evs)

    def tree():
        pids = set(netwatch.tagged_pids(tag, proc.pid, proc=opts.proc))
        if daemon_pid in pids:
            state["daemon_tagged"] = True
        return sorted(pids - {daemon_pid, os.getpid()})

    def owner_of(pid, own, oth):
        if pid in own:
            return "self"
        if pid in oth:
            return "other"
        if daemon_pid is not None and pid == daemon_pid:
            return "daemon"
        return "outside"

    def take_sample(now):
        own = tree()
        oth = []
        if other:
            oth = sorted(set(netwatch.tagged_pids(other["tag"], other.get("pid"), proc=opts.proc)))
        watch = set(own) | set(oth) | ({daemon_pid} if daemon_pid else set())
        ns = netwatch.sample(sorted(watch), ss_runner=opts.ss_runner, proc=opts.proc)
        own_s, oth_s = set(own), set(oth)
        tcp = []
        for s in ns.tcp:
            ow = owner_of(s["pid"], own_s, oth_s)
            owners_seen[ow] = owners_seen.get(ow, 0) + 1
            tcp.append(dict(s, owner=ow))
            tcp_peers.add("%s:%s" % (s.get("peer"), s.get("port")))
        unix = []
        for u in ns.unix:
            if u["pid"] not in own_s and u["pid"] not in oth_s:
                continue
            pp = u.get("peer_pid")
            if pp is None:
                unresolved[0] += 1
                pw = None
            else:
                pw = owner_of(pp, own_s, oth_s)
                if pw != "self" or u["pid"] in oth_s:
                    peers_outside["%s:%s" % (pw, pp)] = peers_outside.get("%s:%s" % (pw, pp), 0) + 1
            unix.append(dict(u, peer_owner=pw, side="other" if u["pid"] in oth_s else "self"))
        # progress: any own tcp socket whose received bytes grew since the last sample
        grew = False
        cur = {}
        for s in tcp:
            if s["owner"] != "self":
                continue
            k = (s["pid"], s.get("fd"), s.get("local"), s.get("peer"), s.get("port"))
            cur[k] = s.get("bytes_received") or 0
            if cur[k] > state["tcp_prev"].get(k, 0):
                grew = True
        state["tcp_prev"] = cur
        if grew:
            mark("tcp_rx", now)
        # ESTAB evidence (only samples where ss worked count; a failed ss is never "no socket")
        if ns.ss_ok:
            has = any(s["state"] == "ESTAB" and s["owner"] == "self" for s in tcp)
            if state["first_answer"] is None:
                est["samples"] += 1
                est["with_estab"] += 1 if has else 0
                if has:
                    est["run"] = 0.0
                else:
                    est["run"] += opts.sample_s
                    est["max_run"] = max(est["max_run"], est["run"])
        else:
            est["ss_fail"] += 1
        tree_max[0] = max(tree_max[0], len(own))
        children_max[0] = max(children_max[0], sum(len(v) for v in ns.children.values()))
        n_open = tracker.open_count()
        open_steps.append(n_open)
        row = {"t": ns.t, "s": round(now, 3), "pids": own, "other_pids": oth, "states": ns.states,
               "children": ns.children, "ss_ok": ns.ss_ok, "errors": ns.errors, "tcp": tcp, "unix": unix,
               "stdout_bytes": state["stdout"], "stderr_bytes": state["stderr"], "log_bytes": state["log"],
               "otlp_events": state["otlp_seen"], "open_steps": n_open, "open_step_types": tracker.open_types(),
               "answer": state["first_answer"] is not None, "stopped": state["stopped_at"] is not None
               and state["cont_at"] is None}
        samples.write(row)

    def maybe_sample():
        if time.monotonic() >= state["next_sample"]:
            take_sample(time.monotonic() - t0)
            state["next_sample"] = max(state["next_sample"] + opts.sample_s, time.monotonic())

    def terminate(reason):
        """SIGCONT then SIGTERM the probe's own tagged tree (never the daemon); no SIGKILL."""
        state["ended_by"] = state["ended_by"] or reason
        state["term_at"] = time.monotonic() - t0
        pids = tree()
        _send(pids, signal.SIGCONT)
        _send(pids, signal.SIGTERM)
        start = time.monotonic()
        resent = False
        while time.monotonic() - start < opts.term_grace_s:
            if proc.poll() is not None and not [x for x in tree() if _pid_alive(x)]:
                break
            if not resent and time.monotonic() - start > opts.term_grace_s / 2:
                _send(tree(), signal.SIGTERM)
                resent = True
            read_new()
            maybe_sample()
            time.sleep(LOOP_S)

    stop_flag = {"sig": None}
    old = {}

    def on_sig(signum, _frame):
        stop_flag["sig"] = signum
    try:
        for sg in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old[sg] = signal.signal(sg, on_sig)
    except ValueError:
        old = {}

    survivors = []
    try:
        exited_at = None
        while True:
            now = time.monotonic() - t0
            read_new()
            rc = proc.poll()
            if rc is not None and exited_at is None:
                exited_at = now
                state["ended_by"] = state["ended_by"] or "exit"
            if exited_at is not None and now - exited_at >= opts.drain_s:
                break
            if exited_at is None:
                if stop_flag["sig"] is not None:
                    terminate("tool_signal")
                    if proc.poll() is None:
                        break          # ignored SIGTERM: report the survivors, never SIGKILL
                    continue
                if (opts.observer_kill_at is not None and relay is not None
                        and state["relay_killed_at"] is None and now >= opts.observer_kill_at):
                    state["relay_killed_at"] = now
                    relay.kill()
                    relay.wait()
                if (opts.sigstop_at is not None and not state["sigstop_done"] and now >= opts.sigstop_at):
                    pids = tree()
                    state["stopped_pids"] = _send(pids, signal.SIGSTOP)
                    state["stopped_at"], state["sigstop_done"] = now, True
                if (state["stopped_at"] is not None and state["cont_at"] is None
                        and now >= state["stopped_at"] + (opts.sigstop_for or 0.0)):
                    _send(sorted(set(tree()) | set(state["stopped_pids"])), signal.SIGCONT)
                    state["cont_at"] = now
                if opts.end_at is not None and now >= opts.end_at:
                    terminate("end_at")
                    if proc.poll() is None:
                        break
                    continue
                if now >= opts.horizon:
                    state["horizon_reached"] = True
                    terminate("horizon")
                    if proc.poll() is None:
                        break
                    continue
            maybe_sample()
            time.sleep(LOOP_S)
        read_new()
        take_sample(time.monotonic() - t0)
    finally:
        # a stopped tree holds SIGTERM pending: never leave it stopped
        try:
            if state["stopped_at"] is not None and state["cont_at"] is None:
                _send(sorted(set(netwatch.tagged_pids(tag, proc.pid, proc=opts.proc))
                             | set(state["stopped_pids"])), signal.SIGCONT)
        except Exception:                                    # noqa: BLE001
            pass
        for sg, h in old.items():
            try:
                signal.signal(sg, h if h is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                pass
    total_s = round((exited_at if exited_at is not None else time.monotonic() - t0), 3)
    alive = [x for x in tree() if _pid_alive(x)]
    survivors = alive if state["ended_by"] != "exit" else []         # still there after a labelled SIGTERM
    leftover_after_exit = alive if state["ended_by"] == "exit" else []   # informational: never signalled
    if relay is not None and relay.poll() is None:
        relay.kill()
        relay.wait()
    stdout_f.close()
    samples.close()
    if otlp_out:
        otlp_out.close()

    # ---- the record
    silent_end = state["first_answer"] if state["first_answer"] is not None else total_s
    signals = {name: gap_stats(ts, silent_end) for name, ts in sorted(arrivals.items())}
    for name in ("stdout", "stderr", "tcp_rx") + (("agylog",) if opts.agy_log_path else ()) \
            + (("otlp",) if sink is not None else ()):
        signals.setdefault(name, gap_stats([], silent_end))
    answer, answer_src = "", None
    last = getattr(launch, "final_text_path", None)
    if last:
        try:
            with open(last, "rb") as f:
                answer = f.read().decode("utf-8", "replace")
            answer_src = "last_file" if answer.strip() else None
        except OSError:
            answer = ""
    from_stream = ""
    try:
        from_stream = extractor.final_text() or extractor.partial_text() or ""
    except Exception:                                        # noqa: BLE001
        pass
    if not answer.strip() and from_stream.strip():
        answer, answer_src = from_stream, "stdout"
    if answer.strip():
        _write_private(p(".answer.md"), answer)
    try:
        ex_error = extractor.error()
    except Exception:                                        # noqa: BLE001
        ex_error = None
    try:
        usage = extractor.usage()
    except Exception:                                        # noqa: BLE001
        usage = None
    ended_by = state["ended_by"] or "exit"
    exit_code = proc.returncode
    rec.update(
        pid=proc.pid, exit_code=exit_code, exit_signal=(-exit_code if exit_code is not None and exit_code < 0
                                                        else None),
        ended_by=ended_by, total_s=total_s, horizon_s=opts.horizon,
        horizon_reached=bool(state.get("horizon_reached")),
        still_running_at_horizon=bool(state.get("horizon_reached")),
        term_at_s=None if state["term_at"] is None else round(state["term_at"], 3),
        stdout_bytes=state["stdout"], stderr_bytes=_size(stderr_path), log_bytes=state["log"],
        first_stdout_byte_s=None if state["first_stdout"] is None else round(state["first_stdout"], 3),
        first_answer_byte_s=None if state["first_answer"] is None else round(state["first_answer"], 3),
        silent_end_s=round(silent_end, 3), signals=signals,
        estab={"samples_in_silent_phase": est["samples"], "samples_with_estab": est["with_estab"],
               "max_run_without_estab_s": round(est["max_run"], 3), "ss_failed_samples": est["ss_fail"],
               "always_estab": est["samples"] > 0 and est["with_estab"] == est["samples"]},
        tcp_owners_seen=owners_seen, tcp_peers=sorted(tcp_peers),
        daemon={"pid": daemon_pid, "tagged": state["daemon_tagged"], "peers_outside": peers_outside,
                "unresolved_unix_peer_samples": unresolved[0]},
        tagged_tree_max=tree_max[0], children_max=children_max[0], open_steps_max=tracker.max_open,
        open_steps_samples=sum(1 for n in open_steps if n > 0),
        observer=None if opts.observer_kill_at is None else {
            "kill_at_s": opts.observer_kill_at, "killed_at_s": None if state["relay_killed_at"] is None
            else round(state["relay_killed_at"], 3), "stdout_bytes_at_end": state["stdout"],
            "cli_exit_code": exit_code, "cli_outlived_relay": state["relay_killed_at"] is not None
            and (exited_at is None or exited_at > state["relay_killed_at"] + 0.5),
            "answer_source": answer_src, "answer_in_last_file": answer_src == "last_file"},
        sigstop=None if opts.sigstop_at is None else {
            "at_s": None if state["stopped_at"] is None else round(state["stopped_at"], 3),
            "cont_at_s": None if state["cont_at"] is None else round(state["cont_at"], 3),
            "for_s": opts.sigstop_for, "pids_stopped": state["stopped_pids"]},
        survivors=survivors, leftover_after_exit=leftover_after_exit, answer_source=answer_src, answer_bytes=len(answer.encode("utf-8")),
        answer_head=providers._scrub(answer, 10 ** 6)[:HEAD_MAX], extractor_error=ex_error, usage=usage,
        model_reported=_safe_call(extractor.model_reported))
    if rec.get("concurrent"):
        c = rec["concurrent"]
        c["other_alive_at_end"] = _pid_alive(other["pid"])
        c["other_record_present_at_end"] = os.path.exists(os.path.join(out, opts.concurrent_with + ".json"))
        c["shared_peers"] = {k: v for k, v in peers_outside.items() if not k.startswith("outside:")}
    _write_private(p(".json"), json.dumps(rec, indent=1, default=str))
    return rec


def _safe_call(fn):
    try:
        return fn()
    except Exception:                                        # noqa: BLE001
        return None


def _wait_for_other(out, label, wait_s):
    path = os.path.join(out, label + ".started.json")
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        try:
            with open(path, "rb") as f:
                d = json.loads(f.read())
            if isinstance(d, dict) and d.get("pid") and d.get("tag"):
                return d
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise ValueError("--concurrent-with %s: no %s.started.json after %gs" % (label, label, wait_s))


# ---------------------------------------------------------------- building the real launch
def resolve_seat(cfg, sid):
    roster = cfg.get("seat") or []
    for s in roster:
        if s.get("id") == sid:
            return dict(s)
    for s in roster:
        if s.get("provider") == sid:
            return dict(s)
    raise ValueError("--seat %r is not a roster id or provider (%s)"
                     % (sid, ", ".join(str(s.get("id")) for s in roster) or "empty roster"))


def check_flags(seat, args):
    prov = seat.get("provider")
    if args.otlp and prov != "codex":
        raise ValueError("--otlp is a codex flag (seat provider is %r)" % prov)
    if args.codex_c and prov != "codex":
        raise ValueError("--codex-c is a codex flag (seat provider is %r)" % prov)
    if args.otlp_protocol != "json" and not args.otlp:
        raise ValueError("--otlp-protocol needs --otlp")
    for flag, on in (("--agy-log", args.agy_log), ("--no-print-timeout", args.no_print_timeout)):
        if on and prov != "agy":
            raise ValueError("%s is an agy flag (seat provider is %r)" % (flag, prov))
    if (args.sigstop_at is None) != (args.sigstop_for is None):
        raise ValueError("--sigstop-at and --sigstop-for go together")
    for name in ("observer_kill_at", "sigstop_at", "sigstop_for", "end_at"):
        v = getattr(args, name)
        if v is not None and v < 0:
            raise ValueError("--%s must be >= 0" % name.replace("_", "-"))
    if args.horizon <= 0:
        raise ValueError("--horizon must be > 0")


def plan(cfg, args, label, out_for_paths, workdir, otlp_port):
    """-> (seat, Launch, prompt_text, prompt_bytes, extra_flags, env_adds). Uses the real adapter build()."""
    seat = resolve_seat(cfg, args.seat)
    check_flags(seat, args)
    adapter = council_run._adapter(seat.get("provider"))
    if adapter is None:
        raise ValueError("no adapter for provider %r" % seat.get("provider"))
    question = providers_read(args.question_file) if args.question_file else DEFAULT_QUESTION
    prompt = council_run.advisor_prompt(council_run._load_template(cfg, "advisor.md"), question,
                                        council_run._context_block([]), seat["id"], len(cfg.get("seat") or []))
    pdata = prompt.encode("utf-8")
    ppath = os.path.join(workdir, "prompt.md")
    _write_private(ppath, pdata)
    extra = []
    if args.otlp:
        extra += otel_flags(otlp_port, args.otlp_protocol)
    for kv in args.codex_c or ():
        extra += ["-c", kv]
    if args.agy_log:
        extra += agy_log_flags(os.path.join(out_for_paths, label + ".agy.log"))
    extra += list(args.extra_arg or ())
    if extra:
        seat["extra_args"] = [str(a) for a in (seat.get("extra_args") or [])] + extra
    state_dir = cfg.get("state_dir") or os.path.expanduser("~/.claude-council")
    wd = os.path.join(workdir, "seat")
    os.makedirs(wd, mode=0o700, exist_ok=True)
    # `_limits` only supplies the Limits object build() reads (agy: --print-timeout = ceiling + 60).
    limits, _mode, _dl, _src = council_run._limits(cfg, seat, "seat", os.path.join(state_dir, "latency.jsonl"),
                                                   len(pdata))
    launch = adapter.build(seat, ppath, wd, limits, cfg)
    if args.no_print_timeout:
        launch = Launch(argv=strip_print_timeout(launch.argv), env=launch.env, cwd=launch.cwd,
                        stdin_path=launch.stdin_path, final_text_path=launch.final_text_path)
    env_adds = parse_kv(args.env, "--env")
    return seat, adapter, launch, prompt, len(pdata), limits, env_adds


def providers_read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


def print_dry(seat, launch, prompt, pbytes, limits, env_adds, args, label, out_dir):
    w = sys.stdout.write
    w("== liveness-probe DRY RUN: nothing launched\n")
    w("seat: id=%s provider=%s model=%s\n" % (seat.get("id"), seat.get("provider"), seat.get("model")))
    w("limits: build input only, NOTHING enforced (ceiling_s=%s feeds the adapter; horizon=%gs is the only stop)\n"
      % ("None" if limits.ceiling_s is None else "%g" % limits.ceiling_s, args.horizon))      # None: no ceiling (row 35)
    w("argv: %s\n" % shlex.join(redact_argv(launch.argv, prompt)))
    shown = ["%s=<uuid per launch>" % netwatch.TAG_ENV]
    for k, v in env_adds:
        shown.append("%s=%s" % (k, "<hidden>" if SECRETISH.search(k) else providers._scrub(v)))
    w("env added: %s\n" % " ".join(shown))
    w("env removed (SCRUB_ENV, when set): %s\n" % " ".join(providers.SCRUB_ENV))
    w("env inherited: %d variables from the caller (values not printed)\n" % len(launch.env))
    w("stdin: %s\n" % (("<prompt %d bytes> from file" % pbytes) if launch.stdin_path else "(none)"))
    if args.otlp:
        w("otlp sink: 127.0.0.1:<sink-port> (ephemeral, one per launch), protocol %s\n" % args.otlp_protocol)
    if args.agy_log:
        w("agy log: %s\n" % os.path.join(out_dir, label + ".agy.log"))
    ctl = []
    for name in ("observer_kill_at", "sigstop_at", "sigstop_for", "end_at", "concurrent_with"):
        if getattr(args, name) is not None:
            ctl.append("%s=%s" % (name, getattr(args, name)))
    w("controls: %s\n" % (" ".join(ctl) or "(none)"))
    w("horizon: %g s (then SIGTERM to the probe's own tagged tree; never SIGKILL)\n" % args.horizon)


# ---------------------------------------------------------------- CLI
def _parser():
    ap = argparse.ArgumentParser(prog="liveness-probe.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--seat", required=True, metavar="ID", help="roster seat id, or a provider name (codex, agy)")
    p.add_argument("--question-file", metavar="F", help='default question: "%s"' % DEFAULT_QUESTION)
    p.add_argument("--out", metavar="DIR", help="result dir (700); files 600")
    p.add_argument("--label", metavar="L", help="file name stem (an existing label is an error)")
    p.add_argument("--config", action="append", default=[], metavar="P", help="overlay TOML, repeatable")
    p.add_argument("--extra-arg", action="append", default=[], metavar="A", help="one raw argv element, repeatable")
    p.add_argument("--codex-c", action="append", default=[], metavar="KEY=VAL", help="codex: append -c KEY=VAL")
    p.add_argument("--env", action="append", default=[], metavar="K=V", help="extra env for the CLI, repeatable")
    p.add_argument("--otlp", action="store_true", help="codex: per-launch OTel exporter to a local sink")
    p.add_argument("--otlp-protocol", choices=("json", "binary"), default="json")
    p.add_argument("--agy-log", action="store_true", help="agy: --v=3 --log-file <out>/<label>.agy.log")
    p.add_argument("--no-print-timeout", action="store_true", help="agy: drop --print-timeout from the argv")
    p.add_argument("--observer-kill-at", type=float, metavar="S", help="kill the stdout relay at S seconds")
    p.add_argument("--sigstop-at", type=float, metavar="S")
    p.add_argument("--sigstop-for", type=float, metavar="D")
    p.add_argument("--concurrent-with", metavar="LABEL", help="wait for <LABEL>.started.json, record overlap")
    p.add_argument("--concurrent-wait", type=float, default=600.0, metavar="S")
    p.add_argument("--end-at", type=float, metavar="S", help="SIGTERM this run's own tagged tree at S seconds")
    p.add_argument("--horizon", type=float, default=3600.0, metavar="S")
    p.add_argument("--drain-s", type=float, default=DRAIN_S, metavar="S",
                   help="keep reading the sink and files this long after the CLI exits (OTel may flush at shutdown)")
    p.add_argument("--daemon-pidfile", metavar="F", help="codex daemon.pid (default $CODEX_HOME/app-server-daemon)")
    p.add_argument("--dry-run", action="store_true")
    return ap


def main(argv=None, ss_runner=None):
    args = _parser().parse_args(argv)
    try:
        cfg = config.load(overlays=args.config)
    except (OSError, ValueError) as e:
        sys.stderr.write("config: %s\n" % providers._scrub("%s: %s" % (type(e).__name__, e), 200))
        return 2
    if not args.dry_run and (not args.out or not args.label):
        sys.stderr.write("--out DIR and --label L are required (or --dry-run)\n")
        return 2
    label = council_run._safe(args.label or "LABEL")
    work = tempfile.mkdtemp(prefix=WORK_PREFIX)
    sink = None
    try:
        if args.dry_run:
            out_dir = os.path.abspath(os.path.expanduser(args.out)) if args.out else "<OUT>"
            seat, _ad, launch, prompt, pbytes, limits, env_adds = plan(cfg, args, label, out_dir, work,
                                                                       "<sink-port>")
            print_dry(seat, launch, prompt, pbytes, limits, env_adds, args, label, out_dir)
            return 0
        out_dir = _out_dir(args.out)
        if any(os.path.exists(os.path.join(out_dir, label + ext))
               for ext in (".json", ".stdout", ".started.json", ".samples.jsonl")):
            raise ValueError("label %r already exists in %s (a probe never overwrites)" % (label, out_dir))
        if args.otlp:
            sink = Sink()
        seat, adapter, launch, prompt, pbytes, limits, env_adds = plan(
            cfg, args, label, out_dir, work, sink.port if sink else None)
        env = dict(launch.env)
        env.update(env_adds)
        launch = Launch(argv=launch.argv, env=env, cwd=launch.cwd, stdin_path=launch.stdin_path,
                        final_text_path=launch.final_text_path)
        tag = uuid.uuid4().hex
        opts = Options(observer_kill_at=args.observer_kill_at, sigstop_at=args.sigstop_at,
                       sigstop_for=args.sigstop_for or 0.0, end_at=args.end_at, horizon=args.horizon,
                       concurrent_with=args.concurrent_with, concurrent_wait_s=args.concurrent_wait, drain_s=args.drain_s,
                       agy_log_path=os.path.join(out_dir, label + ".agy.log") if args.agy_log else None,
                       daemon_pidfile=args.daemon_pidfile
                       or (default_daemon_pidfile() if seat.get("provider") == "codex" else None),
                       ss_runner=ss_runner)
        if opts.agy_log_path:
            _write_private(opts.agy_log_path, b"")
        meta = {"seat": seat.get("id"), "provider": seat.get("provider"), "model": seat.get("model"),
                "argv": redact_argv(launch.argv, prompt),
                "env_keys": sorted([netwatch.TAG_ENV] + [k for k, _ in env_adds]),
                "env_inherited_count": len(launch.env), "stdin": ("<prompt %d bytes>" % pbytes)
                if launch.stdin_path else None, "prompt_bytes": pbytes, "otlp_port": sink.port if sink else None,
                "flags": {"otlp": args.otlp, "agy_log": args.agy_log, "no_print_timeout": args.no_print_timeout}}
        rec = observe(launch, _extractor(adapter, seat), out_dir, label, tag, opts, sink=sink, meta=meta)
        sys.stdout.write("%s: ended_by=%s exit=%s total_s=%s first_answer_byte_s=%s -> %s\n" % (
            label, rec.get("ended_by"), rec.get("exit_code"), rec.get("total_s"), rec.get("first_answer_byte_s"),
            os.path.join(out_dir, label + ".json")))
        if rec.get("survivors"):
            return 3
        return 0 if rec.get("answer_bytes") and not rec.get("extractor_error") else 1
    except (ValueError, KeyError, OSError) as e:
        sys.stderr.write("probe: %s\n" % providers._scrub("%s: %s" % (type(e).__name__, e), 300))
        return 2
    finally:
        if sink is not None:
            sink.close()
        shutil.rmtree(work, ignore_errors=True)


def _extractor(adapter, seat):
    return council_run._extractor(adapter, seat)


if __name__ == "__main__":
    sys.exit(main())
