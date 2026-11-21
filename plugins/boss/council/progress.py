"""boss.progress/1 helpers (stdlib only). Spec: meter/docs/PROGRESS.md (plugins/boss).

Writer:  root(), run_dir(), register(), writer_identity(), write_snapshot(), prune()
Reader:  list_runs(), read_snapshot(), liveness(), session_state(), claim_notification()
Check:   validate(doc, schema) -- the keyword subset PROGRESS.md names.
"""
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone

SCHEMA = "boss.progress/1"
MAJOR = 1
TERMINAL = ("done", "failed", "cancelled")
SAFE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
TOOL = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
MAX_BYTES = 512 * 1024
CAPS = {"title": 200, "summary": 500, "label": 200, "error": 2000, "detail": 2000, "text": 1000}
STALL_MIN_S = 60.0       # stalled = heartbeat older than max(60 s, 5 x interval)
STALL_FACTOR = 5.0
NO_PROGRESS_S = 300.0    # "no progress for X" fact after 5 min without last_progress_at moving
RETAIN_DAYS = 7
_CTRL = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")


# ---------- paths ----------
def root():
    return os.environ.get("BOSS_PROGRESS_DIR") or os.path.expanduser("~/.claude/boss")


def default_sessions_dir():
    """<Claude config dir>/sessions: CLAUDE_CONFIG_DIR, else ~/.claude (the same rule as council's config.user_config_path)."""
    root = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return os.path.join(os.path.expanduser(root), "sessions")


def _mkdir(path):
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)  # makedirs mode= is umask-reduced and leaf-only


def run_dir(tool, run_id, base=None):
    if not TOOL.match(tool or "") or not SAFE.match(run_id or "") or run_id in (".", ".."):
        raise ValueError("unsafe tool/run_id: %r/%r" % (tool, run_id))
    return os.path.join(base or root(), "runs", tool, run_id)


def iso(t=None):
    t = time.time() if t is None else t
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------- /proc ----------
def proc_start_ticks(pid):
    """Field 22 of /proc/<pid>/stat, or None when the pid is gone."""
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    rest = data[data.rindex(")") + 2:].split()  # comm may hold spaces and ')'
    return int(rest[19])


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def _proc_start_epoch(pid):
    ticks = proc_start_ticks(pid)
    if ticks is None:
        return None
    try:
        with open("/proc/stat") as f:
            btime = next(int(l.split()[1]) for l in f if l.startswith("btime "))
    except (OSError, StopIteration):
        return None
    return btime + ticks / os.sysconf("SC_CLK_TCK")


def writer_identity(kind="process", pid=None):
    """Record the publishing process itself (the Python runner, never its launcher shell)."""
    pid = pid or os.getpid()
    return {"pid": pid, "start_ticks": proc_start_ticks(pid), "boot_id": boot_id(), "kind": kind}


# ---------- writer ----------
def _clip(s, n):
    if not isinstance(s, str):
        return s
    s = _CTRL.sub("", s)
    return s if len(s) <= n else s[: n - 1] + "…"


def clean(snap):
    """Strip control/ANSI chars and truncate the free-text fields to their caps (in place)."""
    for k in ("title", "summary"):
        snap[k] = _clip(snap.get(k), CAPS[k])
    for it in snap.get("items", []):
        for k in ("label", "error", "detail"):
            if k in it:
                it[k] = _clip(it[k], CAPS[k])
    for a in snap.get("attention", []):
        a["text"] = _clip(a.get("text"), CAPS["text"])
    return snap


def register(tool, run_id, source_dir=None, title=None, base=None):
    """Create the registry entry once. Returns the run dir; never rewrites run.json."""
    d = run_dir(tool, run_id, base)
    for p in (base or root(), os.path.dirname(os.path.dirname(d)), os.path.dirname(d)):
        _mkdir(p)
    _mkdir(d)
    path = os.path.join(d, "run.json")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return d
    with os.fdopen(fd, "w") as f:
        json.dump({"schema": SCHEMA, "tool": tool, "run_id": run_id, "created_at": iso(),
                   "source_dir": source_dir, "title": _clip(title, CAPS["title"])}, f)
    return d


def write_snapshot(snap, base=None):
    """Atomically replace <run dir>/progress.json (temp in the same dir + os.replace, 0600)."""
    snap.setdefault("schema", SCHEMA)
    snap.setdefault("updated_at", iso())
    d = register(snap["tool"], snap["run_id"], snap.get("source_dir"), snap.get("title"), base)
    data = json.dumps(clean(snap), ensure_ascii=False, separators=(",", ":")).encode()
    if len(data) > MAX_BYTES:
        raise ValueError("snapshot %d bytes > %d cap; drop item detail" % (len(data), MAX_BYTES))
    tmp = os.path.join(d, ".progress.json.%d.tmp" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, os.path.join(d, "progress.json"))
    return os.path.join(d, "progress.json")


def prune(base=None, days=RETAIN_DAYS, now=None):
    """Remove run dirs whose progress.json (or run.json) is older than `days`. Writers call it; readers never."""
    now = now or time.time()
    removed = []
    runs = os.path.join(base or root(), "runs")
    for tool in (os.listdir(runs) if os.path.isdir(runs) else []):
        for rid in os.listdir(os.path.join(runs, tool)):
            d = os.path.join(runs, tool, rid)
            ref = next((p for p in (os.path.join(d, "progress.json"), os.path.join(d, "run.json"))
                        if os.path.exists(p)), d)
            if now - os.stat(ref).st_mtime > days * 86400:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d)
    return removed


# ---------- reader ----------
def list_runs(base=None):
    runs = os.path.join(base or root(), "runs")
    out = []
    for tool in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        for rid in sorted(os.listdir(os.path.join(runs, tool))):
            out.append(os.path.join(runs, tool, rid))
    return out


def read_snapshot(d):
    try:
        with open(os.path.join(d, "progress.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def major(snap):
    m = re.match(r"^boss\.progress/(\d+)$", str((snap or {}).get("schema", "")))
    return int(m.group(1)) if m else None


def session_state(owner, sessions_dir=None):
    """THE ONE adapter onto <Claude config dir>/sessions/ (default
    ~/.claude/sessions/). Reads <pid>.json only: never *.key, never a socket.
    Returns {"pid", "status"} (status busy|idle|unknown) or None when the session is gone."""
    sessions_dir = sessions_dir or default_sessions_dir()
    owner = owner or {}
    sid, pid = owner.get("session_id"), owner.get("pid")
    doc = None
    names = ["%d.json" % pid] if pid else sorted(
        n for n in (os.listdir(sessions_dir) if os.path.isdir(sessions_dir) else [])
        if re.match(r"^\d+\.json$", n))
    for n in names:
        try:
            with open(os.path.join(sessions_dir, n)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not sid or d.get("sessionId") == sid:
            doc = d
            break
    if not doc or not isinstance(doc.get("pid"), int):
        return None
    if owner.get("start_ticks") is not None:
        if proc_start_ticks(doc["pid"]) != owner["start_ticks"]:
            return None
    else:
        start = _proc_start_epoch(doc["pid"])
        if start is None:
            return None
        if doc.get("startedAt") and abs(start - doc["startedAt"] / 1000.0) > 30:
            return None  # pid reused by another process; the session file is left over
    status = doc.get("status")
    return {"pid": doc["pid"], "status": status if status in ("busy", "idle") else "unknown"}


class Probe:
    """Default facts source for liveness(); tests pass their own object with the same three methods."""

    def __init__(self, sessions_dir=None):
        self.sessions_dir = sessions_dir

    def boot_id(self):
        return boot_id()

    def start_ticks(self, pid):
        return proc_start_ticks(pid)

    def session(self, owner):
        return session_state(owner, self.sessions_dir)


def liveness(snap, now=None, probe=None, resumed_at=None):
    """Reader-derived liveness. Pure given (now, probe). Returns
    {status, reason, heartbeat_age_s, no_progress_s, quiet}; status in
    unsupported|ended|dead|stalled|running|waiting_input|unknown."""
    now = time.time() if now is None else now
    probe = probe or Probe()
    out = {"status": "unknown", "reason": None, "heartbeat_age_s": None, "no_progress_s": None, "quiet": False}
    if major(snap) != MAJOR:
        out.update(status="unsupported", reason="schema %s" % (snap or {}).get("schema"))
        return out
    if snap.get("state") in TERMINAL:
        out.update(status="ended", reason=snap["state"])
        return out
    lp = parse_ts(snap.get("last_progress_at"))
    if lp is not None:
        out["no_progress_s"] = round(now - lp, 1)
    # Paused on purpose (orchestrator lease released before the plan finished): not live, not dead, nothing to watch.
    if snap.get("paused") is True:
        out.update(status="waiting_input", reason="paused by owner")
        return out
    w = snap.get("writer") or {}
    if w.get("kind") == "oneshot":
        if not snap.get("owner"):
            out["reason"] = "no owner session recorded"
            return out
        s = probe.session(snap["owner"])
        if s is None:
            out.update(status="dead", reason="owner session gone (orphaned)")
            return out
        st = {"busy": "running", "idle": "waiting_input"}.get(s["status"], "unknown")
        out.update(status=st, reason="owner session %s" % s["status"])
    else:
        b = probe.boot_id()
        if w.get("boot_id") and b and w["boot_id"] != b:
            out.update(status="dead", reason="writer from an earlier boot")
            return out
        ticks = probe.start_ticks(w.get("pid"))
        if ticks is None or (w.get("start_ticks") is not None and ticks != w["start_ticks"]):
            out.update(status="dead", reason="writer pid %s not running" % w.get("pid")
                       if ticks is None else "writer pid %s reused" % w.get("pid"))
            return out
        hb = parse_ts(snap.get("heartbeat_at") or snap.get("updated_at"))
        out["heartbeat_age_s"] = None if hb is None else round(now - hb, 1)
        stall = max(STALL_MIN_S, STALL_FACTOR * float(snap.get("heartbeat_interval_s") or 0))
        if hb is not None and now - hb > stall:
            if resumed_at is not None and now - resumed_at < stall:
                out.update(status="running", reason="resume grace")
            else:
                out.update(status="stalled", reason="no heartbeat for %ds" % (now - hb))
                return out
        else:
            out.update(status="waiting_input" if snap.get("state") == "waiting_input" else "running")
    if out["status"] == "running" and out["no_progress_s"] is not None and out["no_progress_s"] > NO_PROGRESS_S:
        out["quiet"] = True
    return out


def claim_notification(tool, run_id, key, base=None):
    """O_CREAT|O_EXCL claim on <run dir>/claims/<key>. True = this window shows the toast."""
    if not SAFE.match(key or ""):
        raise ValueError("unsafe claim key %r" % key)
    d = os.path.join(run_dir(tool, run_id, base), "claims")
    _mkdir(d)
    try:
        fd = os.open(os.path.join(d, key), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"pid": os.getpid(), "at": iso()}))
    return True


# ---------- validation (keyword subset; see PROGRESS.md) ----------
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(v, t):
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def validate(doc, schema, node=None, path="$"):
    """Return a list of error strings ([] = valid)."""
    node = schema if node is None else node
    if "$ref" in node:
        node = schema["$defs"][node["$ref"].split("/")[-1]]
    errs = []
    types = node.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        if not any(_is(doc, t) for t in types):
            return ["%s: expected %s" % (path, "|".join(types))]
    if "enum" in node and doc not in node["enum"]:
        errs.append("%s: %r not in enum" % (path, doc))
    if isinstance(doc, str):
        if "maxLength" in node and len(doc) > node["maxLength"]:
            errs.append("%s: longer than %d" % (path, node["maxLength"]))
        if "pattern" in node and not re.search(node["pattern"], doc):
            errs.append("%s: does not match %s" % (path, node["pattern"]))
    if _is(doc, "number") and "minimum" in node and doc < node["minimum"]:
        errs.append("%s: below %s" % (path, node["minimum"]))
    if isinstance(doc, dict):
        for k in node.get("required", []):
            if k not in doc:
                errs.append("%s: missing %s" % (path, k))
        for k, sub in node.get("properties", {}).items():
            if k in doc:
                errs += validate(doc[k], schema, sub, "%s.%s" % (path, k))
    if isinstance(doc, list):
        if "maxItems" in node and len(doc) > node["maxItems"]:
            errs.append("%s: more than %d items" % (path, node["maxItems"]))
        if "items" in node:
            for i, v in enumerate(doc):
                errs += validate(v, schema, node["items"], "%s[%d]" % (path, i))
    return errs
