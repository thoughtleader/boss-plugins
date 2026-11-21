#!/usr/bin/env python3
"""Stub CLI for Supervisor tests. Usage: stub_cli.py <mode> [args...]

If STUB_PIDFILE is set, every stub process appends "<role> <pid>" to it on start.

Modes:
  quick                          print QUICK-OK, exit 0
  echo-stdin                     copy stdin to stdout
  slow-stream N                  a line every 0.5 s for N s, then DONE
  hung                           sleep forever, no output
  partial-then-hang              print PARTIAL-123, then hang
  busy-silent N                  spin the CPU N s with no output, then DONE
  forever-busy                   spin the CPU forever, no output
  drip-forever                   one byte every 0.5 s forever
  grandchild-hang                spawn a sleeping grandchild, then hang
  wrapper-grandchild-hang        like codex.js: spawn a child with inherited stdio
                                 (it hangs), forward INT/TERM/HUP, wait for it
  wrapper-exit-grandchild-hang   spawn a hanging grandchild that holds stdout,
                                 print WRAPPER-DONE, exit 0 (R2-W2)
  env-cwd NAME                   print ENV=<$NAME> and CWD=<cwd>
  exit N                         exit with code N
  replay FILE DELAY_S            write FILE line by line, DELAY_S between lines
  timeline FILE CASE CALL        row 32: replay the events of one call from a JSON-lines timeline
                                 (tests/fixtures/replay.jsonl). A line is {case, call, t, <kind>[, every]}:
                                 t = seconds after start; kind is one of
                                   stdout  a line (a string, or an object written as JSON)
                                   stderr  a line
                                   otlp    OTLP/JSON POST to 127.0.0.1:$STUB_OTLP_PORT/v1/logs holding those
                                           names ("codex.sse_event", "event.kind=keepalive", ...); skipped
                                           without the variable (no sink, signal off)
                                   exit    exit with that code
                                 `every` repeats the event every N s until the call ends. After the last
                                 event the stub hangs (silent, alive) unless an exit event was given.
"""
import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.abspath(__file__)


def note(role):
    path = os.environ.get("STUB_PIDFILE")
    if path:
        with open(path, "a") as f:
            f.write("%s %d\n" % (role, os.getpid()))


def out(s):
    sys.stdout.write(s)
    sys.stdout.flush()


def hang():
    while True:
        time.sleep(3600)


def spin(seconds=None):
    end = None if seconds is None else time.monotonic() + seconds
    x = 0
    while end is None or time.monotonic() < end:
        x = (x + 1) % 1000003


def wait_noted(role, pid, timeout=3.0):
    """Wait until a spawned process has recorded itself in STUB_PIDFILE."""
    path = os.environ.get("STUB_PIDFILE")
    if not path:
        return
    want = "%s %d" % (role, pid)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with open(path) as f:
                if any(line.strip() == want for line in f):
                    return
        except OSError:
            pass
        time.sleep(0.01)


def spawn_self(mode):
    return subprocess.Popen([sys.executable, "-Es", HERE, mode])


def otlp_post(names):
    """POST an OTLP/JSON log export that holds `names` (codex.* event names + event.kind=<kind> entries)."""
    port = os.environ.get("STUB_OTLP_PORT")
    if not port:
        return
    plain = [n for n in names if not n.startswith("event.kind=")]
    kinds = [n.split("=", 1)[1] for n in names if n.startswith("event.kind=")]
    recs = [{"attributes": [{"key": "event.name", "value": {"stringValue": n}}]} for n in plain]
    recs += [{"attributes": [{"key": "event.kind", "value": {"stringValue": k}}]} for k in kinds]
    body = json.dumps({"resourceLogs": [{"scopeLogs": [{"logRecords": recs}]}]}).encode()
    try:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=2)
        conn.request("POST", "/v1/logs", body, {"Content-Type": "application/json"})
        conn.getresponse().read()
        conn.close()
    except (OSError, ValueError):
        pass


def timeline(path, case, call):
    """Row 32 fake-event replay. See the module docstring. -> exit code (never returns while hanging)."""
    events = []
    with open(path) as f:
        for n, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("case") == case and ev.get("call") == call:
                events.append((float(ev["t"]), n, ev))
    if not events:
        sys.stderr.write("stub_cli: no timeline %s/%s in %s\n" % (case, call, path))
        return 2
    t0 = time.monotonic()
    pending = sorted(events, key=lambda e: (e[0], e[1]))
    while pending:
        t, n, ev = pending.pop(0)
        wait = t0 + t - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        if "stdout" in ev:
            v = ev["stdout"]
            out((v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))) + "\n")
        if "stderr" in ev:
            sys.stderr.write(ev["stderr"] + "\n")
            sys.stderr.flush()
        if "otlp" in ev:
            otlp_post(ev["otlp"])
        if "exit" in ev:
            return int(ev["exit"])
        if ev.get("every"):
            pending.append((t + float(ev["every"]), n, ev))
            pending.sort(key=lambda e: (e[0], e[1]))
    hang()


def main(argv):
    mode = argv[0] if argv else "quick"
    args = argv[1:]
    note(os.environ.get("STUB_ROLE", "main"))
    if mode == "quick":
        out("QUICK-OK\n")
    elif mode == "echo-stdin":
        sys.stdout.buffer.write(sys.stdin.buffer.read())
        sys.stdout.flush()
    elif mode == "slow-stream":
        end = time.monotonic() + float(args[0])
        i = 0
        while time.monotonic() < end:
            out("tick %d\n" % i)
            i += 1
            time.sleep(0.5)
        out("DONE\n")
    elif mode == "hung":
        hang()
    elif mode == "partial-then-hang":
        out("PARTIAL-123\n")
        hang()
    elif mode == "busy-silent":
        spin(float(args[0]))
        out("DONE\n")
    elif mode == "forever-busy":
        spin()
    elif mode == "drip-forever":
        while True:
            out(".")
            time.sleep(0.5)
    elif mode == "grandchild-hang":
        os.environ["STUB_ROLE"] = "grandchild"
        spawn_self("hung")
        hang()
    elif mode == "wrapper-grandchild-hang":
        os.environ["STUB_ROLE"] = "grandchild"
        child = spawn_self("hung")

        def forward(signum, _frame):
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, forward)
        rc = child.wait()
        return 128 - rc if rc < 0 else rc
    elif mode == "wrapper-exit-grandchild-hang":
        os.environ["STUB_ROLE"] = "grandchild"
        gc = spawn_self("hung")
        wait_noted("grandchild", gc.pid)
        out("WRAPPER-DONE\n")
        return 0
    elif mode == "env-cwd":
        out("ENV=%s\n" % os.environ.get(args[0], ""))
        out("CWD=%s\n" % os.getcwd())
    elif mode == "exit":
        return int(args[0])
    elif mode == "replay":
        delay = float(args[1])
        with open(args[0], "rb") as f:
            for line in f:
                sys.stdout.buffer.write(line)
                sys.stdout.flush()
                time.sleep(delay)
    elif mode == "timeline":
        return timeline(args[0], args[1], args[2])
    else:
        sys.stderr.write("stub_cli: unknown mode %s\n" % mode)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
