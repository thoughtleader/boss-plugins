"""Live status of a running council (run example-run, row 1).

While a council runs, the runner rewrites `<run dir>/status.json` (atomically: temp file + os.replace) on every
state change and at least every `[status] interval_s` (default 3 s); `<state_dir>/current.json` points at the live run
({out_dir, pid, started_at}) so a status line finds it without scanning. `council.py status` and any other reader
use `load` / `current` / `view` / `render` here; nothing in this module touches the network or a model.

Rules this module owns:
- Never block or slow the event loop: one small JSON file per write, no fsync (it can stall on WSL), every error
  swallowed and noted ONCE on stderr (the runner's stderr is runner.log).
- status.json is a snapshot, never a log: a reader that sees `updated_epoch` older than a few intervals while the
  runner `pid` is gone knows the run died (`view` computes it). `ended` is true only on the final write.
- schema_version 1 (documented in README.md "Live status"): consumers must ignore keys they do not know.

Phases: preflight (the launcher, before the runner exists), seats, critique (only a `--depth standard` run: round 2,
between the answers and the chair), chair, done, failed. `supervising` is NOT a phase:
after the synthesis, calls that are still running (pending seats, a resolved seat's other call) keep the file at
done/failed with `ended` false and `late_pending` listing them, until the runner really ends.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import progress_mirror as _pm

SCHEMA_VERSION = 1
FILE = "status.json"
POINTER = "current.json"
DEFAULT_INTERVAL_S = 3.0
MIN_GAP_S = 0.25            # a burst of state changes writes at most this often (the loop ticks every 0.2 s)
FINAL_PHASES = ("done", "failed")
SEAT_PHASES = ("waiting", "streaming", "done", "failed", "skipped")


# ---------------------------------------------------------------- config + files
def cfg_of(cfg):
    """`[status]` -> {enabled, interval_s}. A bad value falls back to the default (a status file must never stop a run)."""
    sc = (cfg or {}).get("status") or {}
    try:
        iv = float(sc.get("interval_s", DEFAULT_INTERVAL_S))
        if not iv > 0:
            iv = DEFAULT_INTERVAL_S
    except (TypeError, ValueError):
        iv = DEFAULT_INTERVAL_S
    return {"enabled": sc.get("enabled", True) is not False, "interval_s": iv,
            "progress": sc.get("progress", True) is not False}


def write_atomic(path, data):
    """Write `data` (a dict) to `path` through a pid-suffixed temp file in the same dir and os.replace (mode 600)."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=1))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def iso(epoch=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if epoch is None else epoch))


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def state_dir_of(cfg):
    return (cfg or {}).get("state_dir") or os.path.expanduser("~/.claude-council")


# ---------------------------------------------------------------- the pointer
def write_current(state_dir, out_dir, pid=None, started_at=None):
    """<state_dir>/current.json -> the live run. Never raises."""
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        write_atomic(os.path.join(state_dir, POINTER),
                     {"out_dir": out_dir, "pid": os.getpid() if pid is None else pid,
                      "started_at": started_at or iso()})
    except Exception:
        pass


def clear_current(state_dir, out_dir, pid=None):
    """Remove the pointer, only when it still names this run (another council may have overwritten it)."""
    path = os.path.join(state_dir, POINTER)
    try:
        with open(path, encoding="utf-8") as f:
            cur = json.load(f)
        if cur.get("out_dir") != out_dir or (pid is not None and cur.get("pid") != pid):
            return False
        os.unlink(path)
        return True
    except (OSError, ValueError, AttributeError):
        return False


def current(state_dir):
    """The live run the pointer names -> its dict, or None when there is none or it is stale (pid gone, dir gone)."""
    try:
        with open(os.path.join(state_dir, POINTER), encoding="utf-8") as f:
            cur = json.load(f)
        if not isinstance(cur, dict) or not os.path.isdir(cur.get("out_dir") or ""):
            return None
        return cur if pid_alive(cur.get("pid")) else None
    except (OSError, ValueError):
        return None


def newest_run(state_dir):
    """The newest run dir under <state_dir>/runs (names start with a UTC timestamp), or None."""
    parent = os.path.join(state_dir, "runs")
    try:
        names = sorted(n for n in os.listdir(parent) if os.path.isdir(os.path.join(parent, n)))
    except OSError:
        return None
    return os.path.join(parent, names[-1]) if names else None


def resolve_run(state_dir, run=None):
    """--run DIR (a path, or a name under <state_dir>/runs) | the current run | the newest run dir -> a dir or None."""
    if run:
        for cand in (os.path.expanduser(run), os.path.join(state_dir, "runs", run)):
            if os.path.isdir(cand):
                return os.path.abspath(cand)
        return None
    cur = current(state_dir)
    return cur["out_dir"] if cur else newest_run(state_dir)


def load(run_dir):
    """<run dir>/status.json -> dict, or None (absent, half-written, not a dict)."""
    try:
        with open(os.path.join(run_dir, FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- the writer
def _sig(snap):
    """What counts as a state change: everything but the clocks and counters that move continuously."""
    def seat_sig(s):
        c = s.get("critique") or {}               # --depth standard: round 2 of the seat
        return (s.get("id"), s.get("phase"), s.get("call"), s.get("retry"), s.get("status"), s.get("model"),
                bool(s.get("substituted")), s.get("detail"), len(s.get("calls") or ()), s.get("verdict"), s.get("grounding"),
                c.get("phase"), c.get("call"), c.get("status"), c.get("verdict"), c.get("changed_view"))
    chair = snap.get("chair") or {}
    return (snap.get("phase"), (snap.get("quorum") or {}).get("ok"), (snap.get("quorum") or {}).get("reached_at_s"),
            tuple(seat_sig(s) for s in snap.get("seats") or ()), chair.get("phase"), chair.get("call"),
            chair.get("status"), tuple(snap.get("late_pending") or ()), snap.get("ended"))


class Writer:
    """Throttled, failure-proof status.json writer. `tick(build)` is called from the event loop; `build()` returns
    the snapshot dict. A write happens when the signature changed (at most every MIN_GAP_S) or interval_s passed."""

    def __init__(self, out_dir, scfg, clock=time.monotonic):
        self.path = os.path.join(out_dir, FILE)
        self.enabled = bool(scfg.get("enabled", True))
        self.out_dir = out_dir
        self.progress = bool(scfg.get("progress", False))    # cfg_of() sets it; also publish boss.progress/1 (progress_mirror.py)
        self.interval = float(scfg.get("interval_s", DEFAULT_INTERVAL_S))
        self.clock = clock
        self.last_at = None
        self.last_sig = None
        self.noted = False
        self.writes = 0

    def _note(self, err):
        if self.noted:
            return
        self.noted = True
        try:
            sys.stderr.write("status: cannot write %s (%s); the run is unaffected\n" % (self.path, type(err).__name__))
            sys.stderr.flush()
        except (OSError, ValueError):
            pass

    def tick(self, build, force=False):
        if not self.enabled:
            return False
        try:
            now = self.clock()
            if not force and self.last_at is not None and now - self.last_at < MIN_GAP_S:
                return False
            snap = build()
            sig = _sig(snap)
            if not force and self.last_at is not None and sig == self.last_sig and now - self.last_at < self.interval:
                return False
            write_atomic(self.path, snap)
            self.last_at, self.last_sig = now, sig
            self.writes += 1
            if self.progress:
                _pm.publish(self.out_dir, snap)      # never raises
            return True
        except Exception as e:           # a status file must never reach the run
            self._note(e)
            return False


def write_final(out_dir, snap, progress=True):
    """One-off final write for code paths with no Writer (an early exit, the crash guard). Never raises.
    `progress` also mirrors it into the boss.progress/1 registry, so every exit path leaves a terminal state there."""
    ok = False
    try:
        write_atomic(os.path.join(out_dir, FILE), snap)
        ok = True
    except Exception:
        pass
    if progress:
        _pm.publish(out_dir, snap)
    return ok


def close_open(out_dir, code=None, reason="runner ended without a final status", only_if_dead=False, progress=True):
    """Crash guard: a status.json still open (`ended` false) is rewritten failed + ended. only_if_dead: the launcher's
    use, when the runner may still be alive and supervising late calls (its pid is in the file)."""
    snap = load(out_dir)
    if snap is None or snap.get("ended") or (only_if_dead and pid_alive(snap.get("pid"))):
        return False
    now = time.time()
    snap.update(phase="failed", ended=True, exit_code=code, supervising=False, updated_at=iso(now),
                updated_epoch=round(now, 3), detail=reason)
    for s in snap.get("seats") or ():
        if s.get("phase") in ("waiting", "streaming"):
            s["phase"], s["status"] = "failed", s.get("status") or "runner_ended"
    return write_final(out_dir, snap, progress)


def early_snapshot(run_id, title, phase, exit_code=None, seats=(), quorum=None, started_epoch=None, pid=None,
                   ended=False, detail=None):
    """A snapshot for the launcher's preflight phase and for runs that end before any launch (exit 2/3)."""
    now = time.time()
    started = started_epoch or now
    return {"schema_version": SCHEMA_VERSION, "run_id": run_id, "title": title, "pid": pid or os.getpid(),
            "started_at": iso(started), "started_epoch": round(started, 3), "updated_at": iso(now),
            "updated_epoch": round(now, 3), "elapsed_s": round(now - started, 1), "phase": phase,
            "ended": ended, "supervising": False, "exit_code": exit_code, "detail": detail,
            "quorum": quorum or {"ok": 0, "min_ok": None, "need": None, "reached_at_s": None},
            "seats": list(seats), "chair": None, "late_pending": []}


def title_of(question, limit=80):
    """First non-empty line of the question, markdown marks stripped, cut to `limit` characters."""
    for ln in str(question or "").splitlines():
        ln = ln.strip().lstrip("#>*- ").strip()
        if ln:
            return ln if len(ln) <= limit else ln[:limit - 1].rstrip() + "…"
    return ""


# ---------------------------------------------------------------- readers: view + render
def view(snap, now=None):
    """`snap` plus the derived fields a reader needs: `age_s` (since the last write), `runner_alive`, and `stale`
    (the run is not final and its runner is gone, or nothing was written for max(10 s, 4 x interval))."""
    now = time.time() if now is None else now
    out = dict(snap)
    age = max(0.0, now - float(snap.get("updated_epoch") or now))
    alive = pid_alive(snap.get("pid"))
    out["age_s"] = round(age, 1)
    out["runner_alive"] = alive
    out["stale"] = bool(not snap.get("ended") and (not alive or age > max(10.0, 4 * float(snap.get("interval_s") or 3))))
    return out


def fmt_secs(s):
    if s is None:
        return "-"
    s = float(s)
    if s < 60:
        return "%.0fs" % s
    m, sec = divmod(int(s + 0.5), 60)
    return "%dm%02ds" % (m, sec) if m < 60 else "%dh%02dm" % divmod(m, 60)


def fmt_bytes(n):
    if n is None:
        return "-"
    n = float(n)
    return "%d" % n if n < 1000 else ("%.1fk" % (n / 1000) if n < 1e6 else "%.1fM" % (n / 1e6))


GLYPH = {"waiting": "○", "streaming": "◐", "done": "●", "failed": "✕", "skipped": "–"}


def render(snap, now=None):
    """The compact table `council.py status` prints: a head line, one line per seat, a quorum + chair line."""
    v = view(snap, now)
    head = "%s  %s  %s  elapsed %s" % (v.get("run_id"), v.get("phase"), v.get("title") or "", fmt_secs(v.get("elapsed_s")))
    if v["stale"]:
        head += "  [STALE: %s, last write %s ago]" % ("runner gone" if not v["runner_alive"] else "no recent write",
                                                       fmt_secs(v["age_s"]))
    rows = [("seat", "", "call", "elapsed", "bytes", "last byte", "detail")]
    for s in v.get("seats") or ():
        call = s.get("call") or "-"
        if s.get("retry"):
            call += " (retry %d)" % s["retry"]
        detail = s.get("detail") or (s.get("status") if s.get("phase") in ("done", "failed") else "") or ""
        if s.get("substituted"):
            detail = ("stand-in %s: " % (s["substituted"] or {}).get("model", "?")) + detail if detail else \
                "stand-in %s" % (s["substituted"] or {}).get("model", "?")
        if s.get("verdict"):                  # display only (councillib/verdict.py): the seat's own closing verdict line
            detail = ("verdict %s" % s["verdict"]) + (", " + detail if detail else "")
        c = s.get("critique")
        if c:                                 # --depth standard: round 2 of the seat, after its round 1 verdict
            r2 = "critique %s" % (c.get("status") if c.get("phase") in ("done", "failed") else c.get("phase") or "waiting")
            if c.get("phase") == "done" and c.get("verdict"):
                r2 += " verdict %s" % c["verdict"]
                if c.get("changed_view"):
                    r2 += " (changed view: %s)" % c["changed_view"]
            elif c.get("phase") in ("streaming", "waiting"):
                r2 += " %s" % fmt_secs(c.get("elapsed_s"))
            detail = (detail + "; " if detail else "") + r2
        if s.get("grounding"):                # grounded run: "ungrounded", or the quote check of the seat's citations
            detail = s["grounding"] + (", " + detail if detail else "")
        age = s.get("last_byte_age_s")
        rows.append((s.get("id") or "?", GLYPH.get(s.get("phase"), "?"), call, fmt_secs(s.get("elapsed_s")),
                     fmt_bytes(s.get("out_bytes")), fmt_secs(age) if age is not None else "-", detail))
    widths = [max(len(str(r[i])) for r in rows) for i in range(6)]
    lines = [head]
    for r in rows:
        lines.append("  " + "  ".join(str(r[i]).ljust(widths[i]) for i in range(6)) + ("  " + r[6] if r[6] else ""))
    q = v.get("quorum") or {}
    line = "quorum %s/%s%s" % (q.get("ok"), q.get("min_ok"),
                               " (reached at %s)" % fmt_secs(q["reached_at_s"]) if q.get("reached_at_s") is not None else "")
    ch = v.get("chair")
    if ch:
        line += "  chair %s %s %s %s" % (GLYPH.get(ch.get("phase"), "?"), ch.get("phase"), fmt_secs(ch.get("elapsed_s")),
                                          fmt_bytes(ch.get("out_bytes")) + " bytes")
    else:
        line += "  chair not started"
    if v.get("late_pending"):
        line += "  still running: %s" % ",".join(v["late_pending"])
    if v.get("ended"):
        line += "  exit %s" % v.get("exit_code")
    lines.append(line)
    return "\n".join(lines) + "\n"
