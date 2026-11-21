"""Seat process tree and socket sampling (row 19; D15, D16).

Attribution is by a per-launch tag, not by process group: a launcher exports
COUNCIL_SEAT_TAG=<tag>, and every process whose /proc/<pid>/environ holds that exact
entry belongs to the seat, plus the ppid descendants of the seat's root pid. A child
that reparented or started its own pgid (codex's depth-4 child) is still found.

`sample(pids)` reads /proc (process state, children) and runs `ss` twice (TCP with
-i for byte counters, unix for peer pids). Everything is read-only. The parsers are
pure so tests feed them scrubbed `ss` text.

Callers must check `NetSample.ss_ok`: a failed `ss` run yields empty socket lists,
and that must never be read as "no socket" (negative evidence, D15).
"""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import time
from dataclasses import dataclass, field

TAG_ENV = "COUNCIL_SEAT_TAG"
SS_TIMEOUT_S = 10

_USERS_RE = re.compile(r'\("((?:[^"\\]|\\.)*)",pid=(\d+),fd=(\d+)\)')
_UNIX_RE = re.compile(
    r"^(?P<netid>\S+)\s+(?P<state>\S+)\s+(?P<recvq>\d+)\s+(?P<sendq>\d+)\s+"
    r"(?P<local>.*?)\s+(?P<inode>\d+)\s+(?P<peer>\S+)\s+(?P<peer_inode>\d+)"
    r"(?:\s+(?P<users>users:.*))?\s*$")
_INT_KEYS = ("bytes_received", "bytes_acked", "segs_in")


@dataclass
class NetSample:
    t: float
    states: dict = field(default_factory=dict)      # {pid: proc state letter, e.g. "S", "T"}
    tcp: list = field(default_factory=list)         # non-loopback sockets of the pids
    unix: list = field(default_factory=list)        # [{pid, peer_pid, ...}]
    children: dict = field(default_factory=dict)    # {pid: [child pids]} within the set
    ss_ok: bool = True                              # False: socket lists are NOT evidence
    errors: list = field(default_factory=list)


# ---- /proc -----------------------------------------------------------------

def _stat(proc, pid):
    """(state, ppid) from /proc/<pid>/stat, or None when unreadable."""
    try:
        with open(os.path.join(proc, str(pid), "stat"), "rb") as f:
            data = f.read()
    except OSError:
        return None
    fields = data[data.rfind(b")") + 2:].split()
    if len(fields) < 2:
        return None
    try:
        return fields[0].decode("ascii", "replace"), int(fields[1])
    except ValueError:
        return None


def _all_pids(proc):
    try:
        names = os.listdir(proc)
    except OSError:
        return []
    return [int(n) for n in names if n.isdigit()]


def _has_tag(proc, pid, tag):
    try:
        with open(os.path.join(proc, str(pid), "environ"), "rb") as f:
            data = f.read()
    except OSError:
        return False
    return ("%s=%s" % (TAG_ENV, tag)).encode() in data.split(b"\0")


def tagged_pids(tag, root_pid=None, proc="/proc"):
    """Live pids (zombies excluded) whose environ holds COUNCIL_SEAT_TAG=<tag>, plus
    root_pid and its ppid-descendants. Sorted."""
    if not tag:
        return []
    states, ppids = {}, {}
    for pid in _all_pids(proc):
        st = _stat(proc, pid)
        if st is None:
            continue
        states[pid], ppids[pid] = st
    found = set()
    for pid, state in states.items():
        if state != "Z" and _has_tag(proc, pid, tag):
            found.add(pid)
    if root_pid is not None and root_pid in states:
        kids = {}
        for pid, pp in ppids.items():
            kids.setdefault(pp, []).append(pid)
        stack = [root_pid]
        while stack:
            cur = stack.pop()
            if states.get(cur) != "Z":
                found.add(cur)
            stack.extend(kids.get(cur, ()))
    return sorted(found)


# ---- ss parsers (pure) ------------------------------------------------------

def _users(text):
    return [(m.group(1), int(m.group(2)), int(m.group(3))) for m in _USERS_RE.finditer(text or "")]


def _split_addr(addr):
    """'1.2.3.4:443' / '[::1]:443' / '*:80' -> (ip or '*', port str)."""
    host, _, port = addr.rpartition(":")
    host = host.strip("[]").split("%", 1)[0]
    return host, port


def _is_loopback(host):
    if host in ("", "*"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback


def _tcp_blocks(text):
    """Group lines: a line starting in column 0 opens a socket, indented lines continue it."""
    blocks = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t":
            if blocks:
                blocks[-1].append(line)
        else:
            blocks.append([line])
    return blocks


def _tcp_info(lines):
    info = {}
    for line in lines:
        for tok in line.split():
            key, sep, val = tok.partition(":")
            if not sep:
                continue
            if key in _INT_KEYS:
                try:
                    info[key] = int(val)
                except ValueError:
                    pass
            elif key == "lastrcv":
                try:
                    info["lastrcv_ms"] = int(val)
                except ValueError:
                    pass
    return info


def parse_ss_tcp(text, include_loopback=False):
    """Parse `ss -tinpH` into one dict per (socket, owning pid):
    {pid, fd, comm, state, local, peer, port, loopback, bytes_received, bytes_acked,
     segs_in, lastrcv_ms}. Counters absent from the text are None. Sockets with no
    `users:` (kernel-owned, e.g. TIME-WAIT) have no pid and are skipped.
    Loopback peers are dropped unless include_loopback."""
    out = []
    for lines in _tcp_blocks(text):
        parts = lines[0].split(None, 5)
        if len(parts) < 5:
            continue
        state, local, peer = parts[0], parts[3], parts[4]
        users = _users(parts[5] if len(parts) > 5 else "")
        if not users:
            continue
        host, port = _split_addr(peer)
        loop = _is_loopback(host)
        if loop and not include_loopback:
            continue
        info = _tcp_info(lines[1:])
        for comm, pid, fd in users:
            out.append({
                "pid": pid, "fd": fd, "comm": comm, "state": state,
                "local": local, "peer": host, "port": int(port) if port.isdigit() else None,
                "loopback": loop,
                "bytes_received": info.get("bytes_received"),
                "bytes_acked": info.get("bytes_acked"),
                "segs_in": info.get("segs_in"),
                "lastrcv_ms": info.get("lastrcv_ms"),
            })
    return out


def parse_ss_unix(text):
    """Parse `ss -xpH` into one dict per (socket, owning pid):
    {pid, fd, comm, state, inode, peer_inode, local, peer}. Lines without users are
    kept with pid None so peer inodes can still be resolved by the caller."""
    out = []
    for line in text.splitlines():
        m = _UNIX_RE.match(line.strip())
        if not m:
            continue
        base = {"state": m["state"], "inode": int(m["inode"]),
                "peer_inode": int(m["peer_inode"]), "local": m["local"], "peer": m["peer"]}
        users = _users(m["users"])
        if not users:
            out.append(dict(base, pid=None, fd=None, comm=None))
        for comm, pid, fd in users:
            out.append(dict(base, pid=pid, fd=fd, comm=comm))
    return out


# ---- sampling ---------------------------------------------------------------

def _run_ss(args):
    r = subprocess.run(["ss"] + list(args), capture_output=True, text=True, timeout=SS_TIMEOUT_S,
                       env=dict(os.environ, LC_ALL="C"), check=False)
    if r.returncode != 0:
        raise RuntimeError("ss %s rc=%d" % (" ".join(args), r.returncode))
    return r.stdout


def sample(pids, ss_runner=None, proc="/proc", now=None):
    """One NetSample for `pids`. ss_runner(args) -> text is injectable for tests."""
    pids = set(pids)
    runner = ss_runner or _run_ss
    ns = NetSample(t=time.time() if now is None else now)
    ppids = {}
    for pid in _all_pids(proc):
        st = _stat(proc, pid)
        if st is None:
            continue
        if pid in pids:
            ns.states[pid] = st[0]
        if st[1] in pids:
            ppids.setdefault(st[1], []).append(pid)
    ns.children = {p: sorted(ppids[p]) for p in sorted(ppids)}
    try:
        tcp = parse_ss_tcp(runner(["-tinpH"]))
        ns.tcp = [s for s in tcp if s["pid"] in pids]
    except Exception as e:  # noqa: BLE001 - any ss failure means "no evidence", never "no socket"
        ns.ss_ok = False
        ns.errors.append("tcp: %s" % e)
    try:
        entries = parse_ss_unix(runner(["-xpH"]))
        owners = {}
        for e in entries:
            if e["pid"] is not None:
                owners.setdefault(e["inode"], set()).add(e["pid"])
        for e in entries:
            if e["pid"] not in pids:
                continue
            peers = sorted(owners.get(e["peer_inode"], ())) or [None]
            for peer_pid in peers:
                ns.unix.append({"pid": e["pid"], "peer_pid": peer_pid, "state": e["state"],
                                "inode": e["inode"], "peer_inode": e["peer_inode"]})
    except Exception as e:  # noqa: BLE001
        ns.ss_ok = False
        ns.errors.append("unix: %s" % e)
    return ns
