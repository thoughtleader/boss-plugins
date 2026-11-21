"""boss.progress/1 mirror of status.json (run example-run, row 1b).

Every time `status.py` writes `<run dir>/status.json` (the same call, so the same throttle: state change at most every
0.25 s, heartbeat at least every `[status] interval_s`), it also hands that snapshot here, and this module publishes
the shared `boss.progress/1` snapshot to `~/.claude/boss/runs/council/<run_id>/progress.json` (spec: boss.progress/1, plugins/boss/lib/progress.schema.json).
status.json keeps its shape; this module only READS the status snapshot, so there is exactly one source of truth.

- stdlib only: the helper library `progress.py` is imported by path (`$BOSS_PROGRESS_LIB`, else council/progress.py
  next to council.py). If it cannot be loaded the mirror is off and the run is unaffected.
- Never raises and never blocks the event loop: one small JSON file per write, no fsync, every error swallowed and noted
  ONCE on stderr (the runner's stderr is runner.log).
- Writer identity is the process that writes (the runner during seats/chair, the launcher during preflight and for
  crash-guard writes); a terminal state is written by whichever process ends the run (done, failed, cancelled by signal,
  early exits, preflight failure), so the reader sees `ended` instead of a dead writer.
- `<run dir>/events.jsonl` gets one line per run/item state change (never per byte); `seq` in the snapshot = its line
  count, so a reader can tell "snapshot behind log".
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time

TOOL = "council"
EVENTS = "events.jsonl"
PREFLIGHT_INTERVAL_S = 60.0     # the launcher does not heartbeat while the preflight canaries run
DEFAULT_INTERVAL_S = 3.0
ITEM_STATE = {"waiting": "running", "streaming": "running", "done": "done", "failed": "failed", "skipped": "skipped"}

_LIB = {"loaded": False, "mod": None}
_MIRRORS = {}
_OWNER = {"done": False, "value": None}
_NOTED = {"done": False}


def lib():
    """The progress.py module (council/progress.py, next to council.py) loaded by path, or None.
    $BOSS_PROGRESS_LIB overrides the path."""
    if _LIB["loaded"]:
        return _LIB["mod"]
    _LIB["loaded"] = True
    path = os.environ.get("BOSS_PROGRESS_LIB") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "progress.py")
    try:
        spec = importlib.util.spec_from_file_location("boss_progress", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LIB["mod"] = mod
    except Exception as e:
        _note("cannot load %s (%s)" % (path, type(e).__name__))
    return _LIB["mod"]


def _note(msg):
    if _NOTED["done"]:
        return
    _NOTED["done"] = True
    try:
        sys.stderr.write("progress: %s; the run is unaffected\n" % msg)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------- owner session
def _ppid(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            data = f.read()
        return int(data[data.rindex(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def find_owner(P, sessions_dir=None, env=None, pid=None):
    """The Claude session that launched this run, or None. Walk the parent chain to a pid that has
    <sessions_dir>/<pid>.json and read only its pid and sessionId fields (never the .key files, never a socket).
    A detached runner has lost its parents, so fall back to the session id in the environment."""
    sdir = sessions_dir or os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"), "sessions")
    cur = pid or os.getpid()
    for _ in range(24):
        cur = _ppid(cur)
        if cur <= 1:
            break
        try:
            with open(os.path.join(sdir, "%d.json" % cur), encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        sid, spid = doc.get("sessionId"), doc.get("pid")
        if isinstance(doc, dict) and spid == cur and isinstance(sid, str) and sid:
            return {"session_id": sid, "pid": spid, "start_ticks": P.proc_start_ticks(spid), "inbox": None}
    sid = (env if env is not None else os.environ).get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return {"session_id": sid[:64], "pid": None, "start_ticks": None, "inbox": None}
    return None


def _owner(P):
    if not _OWNER["done"]:
        _OWNER["done"] = True
        try:
            _OWNER["value"] = find_owner(P)
        except Exception:
            _OWNER["value"] = None
    return _OWNER["value"]


# ---------------------------------------------------------------- status snapshot -> progress items
def _safe_id(run_id):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(run_id or "run"))[:128] or "run"


def _compact(d):
    return {k: v for k, v in d.items() if v is not None and v != "" and v != {}}


def _item_state(phase, has_call=True):
    st = ITEM_STATE.get(phase, "running")
    return "queued" if (phase == "waiting" and not has_call) else st


def _signal_code(code):
    return isinstance(code, int) and 129 <= code <= 159


def run_state(s):
    """status phase + exit code -> (run state, progress phase). Synthesis out but late calls still running
    (phase done/failed, ended false) is the terminal state with phase `supervising`."""
    phase, code = s.get("phase"), s.get("exit_code")
    if phase in ("done", "failed"):
        state = "cancelled" if _signal_code(code) else phase
        return state, ("supervising" if not s.get("ended") else phase)
    return "running", phase or "seats"


def seat_items(sd):
    sid = str(sd.get("id"))
    iid = "seat:" + sid
    calls = sd.get("calls") or []
    sub = sd.get("substituted") or None
    phase = sd.get("phase")
    meta = _compact({"provider": sd.get("provider"), "call": sd.get("call"), "phase": phase, "status": sd.get("status"),
                     "elapsed_s": sd.get("elapsed_s"), "call_elapsed_s": sd.get("call_elapsed_s"),
                     "first_byte_s": sd.get("first_byte_s"), "out_bytes": sd.get("out_bytes"),
                     "last_byte_age_s": sd.get("last_byte_age_s"), "retry": sd.get("retry") or None,
                     "substituted": sub})
    state = _item_state(phase, bool(sd.get("call")))
    items = [_compact({"id": iid, "parent": None, "kind": "seat", "label": sid, "state": state, "model": sd.get("model"),
                       "error": (sd.get("status") if state == "failed" else None), "detail": sd.get("detail"),
                       "meta": meta})]
    used, prev = set(), None
    for i, c in enumerate(calls):
        cid = "%s/%s" % (iid, c.get("call"))
        if cid in used:
            cid = "%s#%d" % (cid, i + 1)
        used.add(cid)
        stand = c.get("call") == "stand-in"
        cstate = _item_state(c.get("phase"))
        item = {"id": cid, "parent": iid, "kind": "stand-in" if stand else "call",
                "label": ("stand-in %s" % c["model"]) if (stand and c.get("model")) else str(c.get("call")),
                "state": cstate, "attempt": i + 1, "replaces": prev, "model": c.get("model"),
                "error": (c.get("status") if cstate == "failed" else None),
                "meta": _compact({"elapsed_s": c.get("elapsed_s"), "out_bytes": c.get("out_bytes"),
                                  "status": c.get("status"), "call": c.get("call")})}
        if stand and sub:
            item["detail"] = "stand-in for %s: %s" % (sid, (sub or {}).get("reason") or "?")
        items.append(_compact(item))
        prev = cid
    crit = sd.get("critique")
    if crit:                                  # --depth standard: round 2 of this seat, an item of its own (not a retry)
        cstate = _item_state(crit.get("phase"))
        items.append(_compact({
            "id": iid + "/critique", "parent": iid, "kind": "critique", "label": "critique", "state": cstate,
            "model": sd.get("model"), "error": (crit.get("status") if cstate == "failed" else None),
            "detail": crit.get("detail"),
            "meta": _compact({"letter": crit.get("letter"), "call": crit.get("call"), "phase": crit.get("phase"),
                              "status": crit.get("status"), "elapsed_s": crit.get("elapsed_s"),
                              "out_bytes": crit.get("out_bytes"), "last_byte_age_s": crit.get("last_byte_age_s"),
                              "round1_verdict": crit.get("round1_verdict"), "verdict": crit.get("verdict"),
                              "changed_view": crit.get("changed_view")})}))
    return items


def chair_item(ch):
    if not ch:
        return {"id": "chair", "parent": None, "kind": "chair", "label": "chair", "state": "queued"}
    state = _item_state(ch.get("phase"))
    return _compact({"id": "chair", "parent": None, "kind": "chair", "label": "chair", "state": state,
                     "model": ch.get("model"), "error": (ch.get("status") if state == "failed" else None),
                     "detail": ch.get("detail"),
                     "meta": _compact({"provider": ch.get("provider"), "call": ch.get("call"), "phase": ch.get("phase"),
                                       "status": ch.get("status"), "elapsed_s": ch.get("elapsed_s"),
                                       "first_byte_s": ch.get("first_byte_s"), "out_bytes": ch.get("out_bytes"),
                                       "last_byte_age_s": ch.get("last_byte_age_s"), "retry": ch.get("retry") or None})})


def build_items(s):
    items = []
    for sd in s.get("seats") or []:
        items.extend(seat_items(sd))
    items.append(chair_item(s.get("chair")))
    if s.get("ended"):                     # nothing runs any more: what never started was skipped, what was cut is cancelled
        for it in items:
            if it["state"] == "queued":
                it["state"] = "skipped"
            elif it["state"] in ("running", "waiting_input"):
                it["state"] = "cancelled"
    return items


def build_counts(s, items):
    seats = [it for it in items if it["kind"] == "seat"]
    counts = [{"unit": "seats", "label": "seats ok", "total": len(seats),
               "done": sum(1 for it in seats if it["state"] == "done"),
               "running": sum(1 for it in seats if it["state"] == "running"),
               "failed": sum(1 for it in seats if it["state"] == "failed")}]
    q = s.get("quorum") or {}
    need = q.get("need") if q.get("need") is not None else q.get("min_ok")
    if isinstance(need, int):
        counts.append({"unit": "quorum", "label": "quorum", "done": min(int(q.get("ok") or 0), need), "total": need})
    return counts


def build_summary(s, state, counts, items):
    seats, q = counts[0], next((c for c in counts if c["unit"] == "quorum"), None)
    parts = []
    if state in ("failed", "cancelled"):
        parts.append("%s (exit %s)" % (state, s.get("exit_code")) if s.get("exit_code") is not None else state)
    elif state == "done":
        parts.append("done")
    if s.get("phase") == "preflight":
        parts.append("preflight running")
    else:
        parts.append("%d/%d seats ok" % (seats["done"], seats["total"]))
        if q:
            parts.append("quorum %d/%d" % (q["done"], q["total"]))
        chair = next((it for it in items if it["id"] == "chair"), None)
        parts.append("chair " + {"queued": "pending", "skipped": "not run"}.get(chair["state"], chair["state"]))
    if s.get("late_pending"):
        parts.append("%d late" % len(s["late_pending"]))
    if s.get("detail"):
        parts.append(str(s["detail"]))
    return " · ".join(parts)


def build_attention(s, items, out_dir, since):
    """Run ended its synthesis step badly: chair failed, or fewer seats answered than the quorum needs."""
    out = []
    if s.get("exit_code") is None:
        return out
    where = {"via": "file", "path": os.path.join(out_dir, "summary.json")}
    chair = next((it for it in items if it["id"] == "chair"), None)
    if chair and chair["state"] == "failed":
        out.append({"id": "chair-failed", "kind": "error", "item": "chair", "answered_by": where, "since": since,
                    "text": "Chair failed (%s%s). Read the run's summary.json and chair/ files." % (
                        chair.get("error") or "unknown", (": " + chair["detail"]) if chair.get("detail") else "")})
    q = s.get("quorum") or {}
    need = q.get("need") if q.get("need") is not None else q.get("min_ok")
    if isinstance(need, int) and int(q.get("ok") or 0) < need and not _signal_code(s.get("exit_code")):
        out.append({"id": "quorum-not-met", "kind": "error", "answered_by": where, "since": since,
                    "text": "Quorum not met: %d of %d needed seats answered (exit %s)." % (
                        int(q.get("ok") or 0), need, s.get("exit_code"))})
    return out


# ---------------------------------------------------------------- events
def _count_lines(path):
    try:
        with open(path, "rb") as f:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 16), b""))
    except OSError:
        return 0


def _progress_sig(doc):
    return (doc.get("phase"), doc.get("state"),
            tuple((it["id"], it["state"], (it.get("meta") or {}).get("out_bytes")) for it in doc.get("items") or []))


class Mirror:
    """Per-run publisher. Holds the previous published document so item timestamps stay stable and events are diffs."""

    def __init__(self, out_dir, P=None):
        self.P = P or lib()
        self.out_dir = os.path.abspath(out_dir)
        self.run_id = _safe_id(os.path.basename(self.out_dir.rstrip("/")))
        self.cwd = os.getcwd()
        self.prev = None
        self.seq = _count_lines(os.path.join(self.out_dir, EVENTS))
        self.last_progress_at = None
        try:
            self.prev = self.P.read_snapshot(self.P.run_dir(TOOL, self.run_id))
        except Exception:
            self.prev = None
        if self.prev:
            self.seq = max(self.seq, int(self.prev.get("seq") or 0))
            self.last_progress_at = self.prev.get("last_progress_at")
        self.seen_progress_sig = _progress_sig(self.prev) if self.prev else None
        self.stamp = self._file_stamp()

    def _file_stamp(self):
        """(inode, mtime_ns) of progress.json: another process (the runner after the launcher's preflight) replaced it."""
        try:
            st = os.stat(os.path.join(self.P.run_dir(TOOL, self.run_id), "progress.json"))
            return (st.st_ino, st.st_mtime_ns)
        except OSError:
            return None

    def stale(self):
        return self._file_stamp() != self.stamp

    def _stamp_items(self, items, now_iso, started_epoch, now_epoch):
        prev = {it["id"]: it for it in (self.prev or {}).get("items") or []}
        for it in items:
            p = prev.get(it["id"], {})
            elapsed = (it.get("meta") or {}).get("elapsed_s")
            if it["state"] != "queued":
                it["started_at"] = p.get("started_at") or self.P.iso(now_epoch - float(elapsed or 0))
            if it["state"] in ("done", "failed", "cancelled", "skipped"):
                it["ended_at"] = p.get("ended_at") or now_iso
            changed = (p.get("state"), (p.get("meta") or {}).get("out_bytes"), p.get("model")) != \
                      (it["state"], (it.get("meta") or {}).get("out_bytes"), it.get("model"))
            it["updated_at"] = now_iso if changed or not p.get("updated_at") else p["updated_at"]

    def _events(self, doc, now_iso):
        prev = self.prev or {}
        pitems = {it["id"]: it for it in prev.get("items") or []}
        out = []
        if (prev.get("state"), prev.get("phase")) != (doc["state"], doc["phase"]):
            out.append({"kind": "run", "state": doc["state"], "phase": doc["phase"]})
        for it in doc["items"]:
            if pitems.get(it["id"], {}).get("state") != it["state"]:
                ev = {"kind": "item", "id": it["id"], "item_kind": it["kind"], "state": it["state"]}
                if it.get("model"):
                    ev["model"] = it["model"]
                if it.get("error"):
                    ev["error"] = it["error"]
                out.append(ev)
        pids = {a["id"] for a in prev.get("attention") or []}
        for a in doc["attention"]:
            if a["id"] not in pids:
                out.append({"kind": "attention", "id": a["id"], "text": a["text"]})
        return out

    def _append(self, events, now_iso):
        lines = []
        for ev in events:
            self.seq += 1
            lines.append(json.dumps(dict(seq=self.seq, ts=now_iso, tool=TOOL, run_id=self.run_id, **ev),
                                    ensure_ascii=False, separators=(",", ":")))
        if not lines or not os.path.isdir(self.out_dir):
            return
        fd = os.open(os.path.join(self.out_dir, EVENTS), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
        finally:
            os.close(fd)

    def build(self, s):
        P = self.P
        now_epoch = time.time()
        now_iso = P.iso(now_epoch)
        started_epoch = s.get("started_epoch") or now_epoch
        state, phase = run_state(s)
        items = build_items(s)
        self._stamp_items(items, now_iso, started_epoch, now_epoch)
        counts = build_counts(s, items)
        pa = {a["id"]: a["since"] for a in (self.prev or {}).get("attention") or []}
        attention = []
        for a in build_attention(s, items, self.out_dir, now_iso):
            a["since"] = pa.get(a["id"], a["since"])
            attention.append(a)
        interval = PREFLIGHT_INTERVAL_S if s.get("phase") == "preflight" else float(s.get("interval_s") or DEFAULT_INTERVAL_S)
        doc = {"schema": P.SCHEMA, "tool": TOOL, "run_id": self.run_id, "title": s.get("title") or None,
               "summary": build_summary(s, state, counts, items), "cwd": self.cwd, "source_dir": self.out_dir,
               "events_path": os.path.join(self.out_dir, EVENTS) if os.path.isdir(self.out_dir) else None,
               "state": state, "phase": phase,
               "created_at": (self.prev or {}).get("created_at") or P.iso(started_epoch),
               "started_at": P.iso(started_epoch), "updated_at": now_iso,
               "ended_at": ((self.prev or {}).get("ended_at") or now_iso) if s.get("ended") else None,
               "heartbeat_at": now_iso, "heartbeat_interval_s": interval,
               "writer": P.writer_identity("process"), "owner": _owner(P),
               "counts": counts, "items": items, "attention": attention,
               "ext": {"council": _compact({
                   "schema_version": s.get("schema_version"), "interval_s": s.get("interval_s"),
                   "elapsed_s": s.get("elapsed_s"), "supervising": bool(not s.get("ended") and s.get("exit_code") is not None),
                   "exit_code": s.get("exit_code"), "quorum": s.get("quorum"), "late_pending": s.get("late_pending") or [],
                   "detail": s.get("detail"), "status_json": os.path.join(self.out_dir, "status.json")})}}
        return doc, now_iso

    def publish(self, s):
        doc, now_iso = self.build(s)
        self._append(self._events(doc, now_iso), now_iso)
        doc["seq"] = self.seq
        sig = _progress_sig(doc)
        if sig != self.seen_progress_sig or not self.last_progress_at:
            self.last_progress_at = now_iso
            self.seen_progress_sig = sig
        doc["last_progress_at"] = self.last_progress_at
        self.P.write_snapshot(doc)
        self.prev = doc
        self.stamp = self._file_stamp()
        return doc


def publish(out_dir, snap):
    """Mirror one status snapshot into the boss.progress registry. Never raises. True when written."""
    try:
        P = lib()
        if P is None:
            return False
        key = os.path.abspath(out_dir)
        m = _MIRRORS.get(key)
        if m is not None and m.stale():          # a forked/other process wrote since: rebuild seq and prev from disk
            m = None
        if m is None:
            m = _MIRRORS[key] = Mirror(key, P)
        m.publish(snap)
        return True
    except Exception as e:
        _note("cannot write the snapshot for %s (%s: %s)" % (out_dir, type(e).__name__, e))
        return False


def prune_old():
    """Writers prune the registry at run start (spec section 9). Never raises."""
    try:
        P = lib()
        if P is not None:
            P.prune()
    except Exception:
        pass
