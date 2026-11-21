"""Liveness evidence for a supervised seat (row 28; D10, D15, D16, D30, D44, D47).

This layer only INFORMS. A verdict never kills, signals or reaps anything: it is a frozen
snapshot that the scheduler reads (`sup.liveness(key)`) to time a hedge and to report. The
scheduler decides, and only a proven terminal state may lead to `Supervisor.terminate`.

Shape
    policy   = LivenessPolicy.from_config(provider_cfg, liveness_cfg)   # gate lines, row 25
    seat     = SeatLiveness(provider, tag, policy, sources)             # one per launched call
    board    = LivenessBoard(); board.add(key, seat)
    sup      = Supervisor(sampler=board)                                # f(SeatInfo) -> Verdict
    sup.liveness(key) -> Verdict                                        # last snapshot

Clock domain: every time in a Verdict is SECONDS SINCE THE SEAT STARTED (the same domain as
`Notice.t` and `SeatInfo.elapsed_s`). Wall-clock inputs (OTLP sink `t`, extractor timestamps)
are converted with `started_wall`, taken when the SeatLiveness is created (= at launch).

progress_at (adopted signals only; None when the policy signal is "none", i.e. hedge-only)
    otel    the last OTLP request that holds `progress_event` (codex.sse_event) and at least one
            `event.kind=X` whose name is not in `progress_exclude` (keepalive). A request that
            holds only keepalive kinds is counted as a keepalive, never as progress. A request
            with the event name and NO kind cannot be classified and is not progress (counted in
            `unclassified`): the hedge then starts on time, which is the safe side.
    applog  the last new log line holding `log_match`.
    stream  the extractor's last `data:` line (OpenRouter). Comment keepalives are a separate
            label (D44) and are never content progress or proof of a hang.

terminal (None, or a reason). Never elapsed time.
    cli_idle_timeout / stream_error / cli_timeout / quota_exhausted
        a line from the CLI that matches a gate `terminal_lines` entry. Only entries listed
        there count (the probe showed the CLI does not retry after them); an unlisted idle form
        (e.g. websocket) is not terminal. A list entry needs every substring on ONE line.
        stderr lines are matched raw; stdout is JSON, so only error-shaped events are matched
        (type error / turn.failed, result.error), never answer text. The raw line is never kept:
        the verdict holds only the reason and the gate substring that matched.
    no_connection
        only when `negative_evidence`, after `grace_s`, for `no_conn_samples` consecutive samples
        spaced >= 0.9 x sample_s that each show: ss worked, tagged pids exist, none has an ESTAB
        non-loopback TCP socket, none is stopped (T), the tagged tree has no pid beyond its
        baseline, no tool step is open, and no new stdout/stderr/log byte or OTLP event arrived.
        Any other sample resets the count. Not sticky: it clears when activity returns.
        Line terminals are sticky.

Sources (all optional, injectable for tests): see SeatLiveness.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from . import netwatch

SIGNALS = ("otel", "applog", "stream", "none")
REASONS = ("cli_idle_timeout", "stream_error", "cli_timeout", "quota_exhausted", "no_connection")
_CHAT_STEPS = ("user_input", "agent_response", "error_message")
_READ_CAP = 8 << 20


# ---- policy ------------------------------------------------------------------

def _reason_for(substrings):
    text = " ".join(substrings).lower()
    if "idle timeout" in text:
        return "cli_idle_timeout"
    if "print timeout" in text:
        return "cli_timeout"
    if "resource_exhausted" in text or "quota" in text:
        return "quota_exhausted"
    return "stream_error"


def normalize_terminal_lines(entries):
    """Gate `terminal_lines` -> tuple of (reason, (substring, ...)). An entry is a string, a list of
    substrings that must all be on one line, or {"reason": r, "match": str|list}."""
    out = []
    for e in entries or ():
        reason = None
        if isinstance(e, dict):
            reason, e = e.get("reason"), e.get("match")
        subs = [e] if isinstance(e, str) else list(e or ())
        subs = [str(s) for s in subs]
        if not subs or any(not s for s in subs):
            raise ValueError("terminal_lines entry needs non-empty substrings: %r" % (e,))
        reason = reason or _reason_for(subs)
        if reason not in REASONS:
            raise ValueError("unknown terminal reason %r" % reason)
        out.append((reason, tuple(subs)))
    return tuple(out)


@dataclass(frozen=True)
class LivenessPolicy:
    progress_signal: str = "none"
    progress_event: str = "codex.sse_event"
    progress_exclude: tuple = ("event.kind=keepalive",)
    negative_evidence: bool = False
    terminal_lines: tuple = ()           # normalized: ((reason, (substr, ...)), ...)
    sample_s: float = 10.0
    grace_s: float = 120.0
    no_conn_samples: int = 18
    baseline_s: float = 3.0
    log_match: str = "stream_helpers.go"

    def __post_init__(self):
        if self.progress_signal not in SIGNALS:
            raise ValueError("progress_signal must be one of %s" % ", ".join(SIGNALS))
        if not self.sample_s > 0:
            raise ValueError("sample_s must be > 0")
        if int(self.no_conn_samples) < 1:
            raise ValueError("no_conn_samples must be >= 1")
        if self.grace_s < 0:
            raise ValueError("grace_s must be >= 0")
        object.__setattr__(self, "terminal_lines", normalize_terminal_lines(self.terminal_lines))
        object.__setattr__(self, "progress_exclude", tuple(self.progress_exclude or ()))

    @classmethod
    def from_config(cls, provider_cfg=None, liveness_cfg=None):
        """`[providers.<p>]` keys progress_signal / progress_event / progress_exclude /
        negative_evidence / terminal_lines, plus the `[liveness]` keys sample_s / grace_s /
        no_conn_samples (row 29 tables). Missing keys keep the safe defaults (hedge-only)."""
        pc, lc = provider_cfg or {}, liveness_cfg or {}
        kw = {}
        for k in ("progress_signal", "progress_event", "progress_exclude", "negative_evidence",
                  "terminal_lines", "log_match"):
            if k in pc:
                kw[k] = pc[k]
        for k in ("sample_s", "grace_s", "no_conn_samples", "baseline_s"):
            if k in lc:
                kw[k] = lc[k]
        return cls(**kw)


# ---- verdict -----------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    """Immutable snapshot. Times are seconds since the seat started."""
    provider: str
    tag: str | None
    elapsed_s: float
    samples: int                     # counted samples so far
    progress_signal: str
    hedge_only: bool                 # True: no adopted signal, progress_at is always None
    progress_at: float | None        # last adopted progress; keepalives excluded
    keepalive_at: float | None       # last keepalive (labelled, never progress)
    keepalives: int
    unclassified: int                # sse_event requests with no event.kind
    terminal: str | None
    terminal_at: float | None
    terminal_source: str | None      # "line" | "no_connection"
    terminal_match: str | None       # the gate substring that matched (never the raw line)
    no_conn_streak: int
    no_conn_needed: int
    no_conn_blocked: str | None      # why the last counted sample did not count
    tool_open: bool
    extra_children: bool
    stopped: bool
    estab: int
    ss_ok: bool | None
    last_activity_at: float | None

    def progress_recent(self, quiet_s):
        """True if an adopted signal showed progress within `quiet_s` of the sample (D12 deferral)."""
        return self.progress_at is not None and (self.elapsed_s - self.progress_at) <= quiet_s


# ---- file tail ---------------------------------------------------------------

class LineTail:
    """Incremental line reader of a growing file: keeps the offset and a partial last line."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self._partial = b""

    def poll(self):
        """New complete lines (bytes, no newline) since the last call."""
        if not self.path:
            return []
        try:
            size = os.path.getsize(self.path)
            if size < self.offset:
                self.offset, self._partial = 0, b""
            if size == self.offset:
                return []
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(min(size - self.offset, _READ_CAP))
        except OSError:
            return []
        self.offset += len(data)
        parts = (self._partial + data).split(b"\n")
        self._partial = parts.pop()
        return [p.rstrip(b"\r") for p in parts if p.strip()]


def _json(line):
    try:
        ev = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return ev if isinstance(ev, dict) else None


def _text(line):
    return line.decode("utf-8", "replace") if isinstance(line, (bytes, bytearray)) else str(line)


def _error_texts(ev):
    """Strings of an error-shaped stdout event; [] for any other event (answer text is never matched)."""
    out = []
    t = ev.get("type")
    if t in ("error", "turn.failed"):
        if isinstance(ev.get("message"), str):
            out.append(ev["message"])
        err = ev.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            out.append(err["message"])
        elif isinstance(err, str):
            out.append(err)
    if ev.get("event") == "result":
        res = ev.get("result")
        if isinstance(res, dict) and res.get("status") not in (None, "SUCCESS") and isinstance(res.get("error"), str):
            out.append(res["error"])
    return out


# ---- per-seat liveness -------------------------------------------------------

class SeatLiveness:
    """Evidence for one launched call.

    sources (all optional; every callable is injectable):
      otlp_events()        -> [{t, path, bytes, names}] wall-clock events of this call's sink
      pids(info)           -> tagged pids (default netwatch.tagged_pids(tag, root_pid=info.pid))
      net(pids)            -> NetSample (default netwatch.sample)
      stdout_lines()/stderr_lines()/log_lines() -> new complete lines (default: tail the
                              info.stdout_path / info.stderr_path / `log_path`)
      tool_open()          -> bool, overrides the stdout-derived open-step tracking
      log_bytes()          -> int (default: size of `log_path`)
      out_bytes()/err_bytes() -> int (default: SeatInfo.out_bytes / err_bytes)
      stream_stats()       -> {last_data_at, last_keepalive_at, keepalives} wall clock (OpenRouterExtractor)
    clock: wall clock (time.time). started_wall: wall time of launch (default clock() now).
    """

    def __init__(self, provider, tag, policy, sources=None, clock=time.time, started_wall=None, log_path=None):
        self.provider = provider
        self.tag = tag
        self.policy = policy
        self.src = dict(sources or {})
        self.clock = clock
        self.t0 = clock() if started_wall is None else started_wall
        self.log_path = log_path
        self._tails = {}
        self._ev_seen = 0
        self._progress = None
        self._keepalive = None
        self._keepalives = 0
        self._unclassified = 0
        self._open_steps = set()
        self._term = None               # (reason, at, match)
        self._baseline = None
        self._extra = False
        self._stopped = False
        self._estab = 0
        self._ss_ok = None
        self._streak = 0
        self._streak_since = None
        self._blocked = None
        self._samples = 0
        self._prev_elapsed = None
        self._seen = None               # (out, err, log, otlp events) at the last counted sample
        self._activity_at = None
        self._stdout_seen = False
        self._line_count = 0
        self._last = None

    # ---- public ----
    def __call__(self, info=None):
        return self.sample(info)

    def progress_at(self):
        return self._progress if self.policy.progress_signal != "none" else None

    def terminal(self):
        return self.verdict().terminal

    def verdict(self):
        return self._last if self._last is not None else self._make(self._elapsed(None))

    def sample(self, info=None):
        elapsed = self._elapsed(info)
        self._ingest_events()
        self._ingest_streams(info)
        self._ingest_stream_stats()
        spaced = (self._prev_elapsed is None
                  or elapsed - self._prev_elapsed >= 0.9 * self.policy.sample_s)
        if spaced:
            self._count_sample(info, elapsed)
            self._prev_elapsed = elapsed
        self._last = self._make(elapsed)
        return self._last

    # ---- helpers ----
    def _elapsed(self, info):
        if info is not None and getattr(info, "elapsed_s", None) is not None:
            return float(info.elapsed_s)
        return self.clock() - self.t0

    def _rel(self, wall):
        return wall - self.t0

    def _tail(self, name, path):
        tail = self._tails.get(name)
        if tail is None and path:
            tail = self._tails[name] = LineTail(path)
        return tail

    def _new_lines(self, name, info):
        fn = self.src.get(name + "_lines")
        if fn is not None:
            return list(fn() or ())
        path = {"stdout": getattr(info, "stdout_path", None), "stderr": getattr(info, "stderr_path", None),
                "log": self.log_path}.get(name)
        tail = self._tail(name, path)
        return tail.poll() if tail else []

    def _stdout_observable(self, info):
        return ("stdout_lines" in self.src or "tool_open" in self.src
                or getattr(info, "stdout_path", None) is not None)

    def _ingest_events(self):
        fn = self.src.get("otlp_events")
        if fn is None:
            return
        events = fn() or []
        if len(events) < self._ev_seen:
            self._ev_seen = 0
        pol = self.policy
        for ev in events[self._ev_seen:]:
            names = ev.get("names") or ()
            at = self._rel(ev.get("t", self.clock()))
            self._activity_at = at if self._activity_at is None else max(self._activity_at, at)
            if pol.progress_signal != "otel" or pol.progress_event not in names:
                continue
            kinds = [n for n in names if n.startswith("event.kind=")]
            if not kinds:
                self._unclassified += 1
            elif any(k not in pol.progress_exclude for k in kinds):
                self._progress = at if self._progress is None else max(self._progress, at)
            else:
                self._keepalives += 1
                self._keepalive = at if self._keepalive is None else max(self._keepalive, at)
        self._ev_seen = len(events)

    def _ingest_stream_stats(self):
        fn = self.src.get("stream_stats")
        if fn is None:
            return
        st = fn() or {}
        if self.policy.progress_signal == "stream" and st.get("last_data_at") is not None:
            self._progress = self._rel(st["last_data_at"])
        if st.get("last_keepalive_at") is not None:
            self._keepalive = self._rel(st["last_keepalive_at"])
        self._keepalives = int(st.get("keepalives") or 0)

    def _ingest_streams(self, info):
        out_lines = self._new_lines("stdout", info)
        err_lines = self._new_lines("stderr", info)
        self._line_count += len(out_lines) + len(err_lines)
        for raw in out_lines:
            self._stdout_seen = True
            ev = _json(raw)
            if ev is None:
                self._match(_text(raw))
                continue
            for text in _error_texts(ev):
                self._match(text)
            self._track_steps(ev)
        for raw in err_lines:
            self._match(_text(raw))
        if self.policy.progress_signal == "applog":
            for raw in self._new_lines("log", info):
                if self.policy.log_match in _text(raw):
                    self._progress = self._elapsed(info)

    def _match(self, text):
        if self._term is not None:
            return
        for reason, subs in self.policy.terminal_lines:
            if all(s in text for s in subs):
                self._term = (reason, self._elapsed(None), subs[0])
                return

    def _track_steps(self, ev):
        t = ev.get("type")
        if t in ("turn.completed", "turn.failed") or ev.get("event") == "result":
            self._open_steps.clear()
            return
        item = ev.get("item")
        if t == "item.started" and isinstance(item, dict):
            self._open_steps.add(("item", item.get("id")))
        elif t == "item.completed" and isinstance(item, dict):
            self._open_steps.discard(("item", item.get("id")))
        elif ev.get("event") == "step_update" and isinstance(ev.get("step_update"), dict):
            su = ev["step_update"]
            if su.get("step_type") in _CHAT_STEPS:
                return
            key = ("step", su.get("step_index"))
            if su.get("state") == "ACTIVE":
                self._open_steps.add(key)
            else:
                self._open_steps.discard(key)

    def _tool_open(self):
        fn = self.src.get("tool_open")
        return bool(fn()) if fn is not None else bool(self._open_steps)

    def _totals(self, info):
        ofn, efn = self.src.get("out_bytes"), self.src.get("err_bytes")
        out = int(ofn() or 0) if ofn is not None else (getattr(info, "out_bytes", 0) if info is not None else 0)
        err = int(efn() or 0) if efn is not None else (getattr(info, "err_bytes", 0) if info is not None else 0)
        fn = self.src.get("log_bytes")
        if fn is not None:
            log = int(fn() or 0)
        elif self.log_path:
            try:
                log = os.path.getsize(self.log_path)
            except OSError:
                log = 0
        else:
            log = 0
        return (out, err, log, self._ev_seen, self._line_count)

    def _count_sample(self, info, elapsed):
        pol = self.policy
        self._samples += 1
        totals = self._totals(info)
        activity = self._seen is not None and totals != self._seen
        if activity:
            self._activity_at = elapsed
        self._seen = totals
        pfn = self.src.get("pids")
        pids = list(pfn(info) if pfn is not None else
                    netwatch.tagged_pids(self.tag, root_pid=getattr(info, "pid", None)))
        if self._baseline is None and pids and elapsed >= pol.baseline_s:
            self._baseline = frozenset(pids)
        self._extra = bool(self._baseline) and bool(set(pids) - self._baseline)
        net = None
        if pids:
            nfn = self.src.get("net")
            try:
                net = nfn(pids) if nfn is not None else netwatch.sample(pids)
            except Exception:                       # noqa: BLE001 - a failed sample is no evidence
                net = None
        self._ss_ok = None if net is None else bool(net.ss_ok)
        self._stopped = bool(net) and any(s in ("T", "t") for s in net.states.values())
        self._estab = 0 if net is None else sum(1 for s in net.tcp if s.get("state") == "ESTAB")
        blocked = None
        if not pol.negative_evidence:
            blocked = "disabled"
        elif elapsed < pol.grace_s:
            blocked = "grace"
        elif not pids:
            blocked = "no_pids"
        elif net is None or not net.ss_ok:
            blocked = "ss_failed"
        elif self._estab:
            blocked = "socket"
        elif self._stopped:
            blocked = "stopped"
        elif self._extra:
            blocked = "children"
        elif self._tool_open():
            blocked = "tool_open"
        elif not (self._stdout_observable(info) or self._stdout_seen):
            blocked = "stdout_unobservable"
        elif activity:
            blocked = "activity"
        self._blocked = blocked
        if blocked is not None:
            self._streak, self._streak_since = 0, None
            return
        if self._streak == 0:
            prev = self._prev_elapsed if self._prev_elapsed is not None else 0.0
            self._streak_since = max(prev, pol.grace_s)
        self._streak += 1

    def _no_conn(self, elapsed):
        pol = self.policy
        return (pol.negative_evidence and self._streak >= int(pol.no_conn_samples)
                and self._streak_since is not None
                and elapsed - self._streak_since >= 0.9 * pol.no_conn_samples * pol.sample_s)

    def _make(self, elapsed):
        pol = self.policy
        terminal = at = source = match = None
        if self._term is not None:
            terminal, at, match = self._term
            source = "line"
        elif self._no_conn(elapsed):
            terminal, at, source = "no_connection", elapsed, "no_connection"
        return Verdict(
            provider=self.provider, tag=self.tag, elapsed_s=elapsed, samples=self._samples,
            progress_signal=pol.progress_signal, hedge_only=pol.progress_signal == "none",
            progress_at=self.progress_at(), keepalive_at=self._keepalive, keepalives=self._keepalives,
            unclassified=self._unclassified, terminal=terminal, terminal_at=at, terminal_source=source,
            terminal_match=match, no_conn_streak=self._streak, no_conn_needed=int(pol.no_conn_samples),
            no_conn_blocked=self._blocked, tool_open=self._tool_open() if self._samples else False,
            extra_children=self._extra, stopped=self._stopped, estab=self._estab, ss_ok=self._ss_ok,
            last_activity_at=self._activity_at)


class LivenessBoard:
    """The Supervisor sampler: `Supervisor(sampler=board)`. Maps SeatInfo.key to that seat's
    SeatLiveness; an unregistered key gets None. Returns the frozen Verdict."""

    def __init__(self):
        self._seats = {}

    def add(self, key, seat):
        self._seats[key] = seat
        return seat

    def get(self, key):
        return self._seats.get(key)

    def remove(self, key):
        self._seats.pop(key, None)

    def __call__(self, info):
        seat = self._seats.get(info.key)
        return seat.sample(info) if seat is not None else None


# ---- exit classification -----------------------------------------------------

def exit_class(provider, rc, stderr="", terminated=False, saw_turn_completed=False):
    """Why an exited call is NOT an answer, or None when it may be one (the extractor still decides).
    Row 25: codex rc 101 after the stdout reader died (Broken pipe) and codex rc 0 after SIGTERM
    are not answers. Row 23/D47: agy rc 0 with the print-timeout warning is a cli_timeout."""
    err = stderr or ""
    if provider == "agy" and "[agy] print timeout after" in err:
        return "cli_timeout"
    if rc == 101 or "Broken pipe" in err:
        return "observer_lost"
    if terminated and rc in (0, None):
        return "terminated_not_answer"
    if provider == "codex" and rc == 0 and not saw_turn_completed:
        return "no_turn_completed"
    return None
