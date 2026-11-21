"""netwatch tests (row 19): tag/ppid attribution, `ss` parsers, sample(). Stubs only.

Run: python3 -Es -m unittest discover -s ~/.claude/council/tests -t ~/.claude/council
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import netwatch  # noqa: E402

PY = sys.executable or "/usr/bin/python3"

# Scrubbed `ss -tinpH`: addresses are documentation ranges; pids are invented.
SS_TCP = (
    "ESTAB 0      0           192.0.2.10:58584      198.51.100.7:443   users:((\"codex\",pid=4001,fd=18))\n"
    "\t cubic wscale:13,10 rto:208 rtt:7.64/2.566 ato:40 mss:1388 pmtu:1500 rcvmss:1448 advmss:1448 cwnd:10 "
    "bytes_sent:25893 bytes_acked:25894 bytes_received:8318 segs_out:148 segs_in:147 data_segs_out:74 "
    "data_segs_in:74 send 14534031bps lastsnd:4288 lastrcv:3992 lastack:3992 pacing_rate 29065680bps\n"
    "ESTAB 0      0           192.0.2.10:41000      203.0.113.9:443    users:((\"codex\",pid=4002,fd=9),(\"node\",pid=4001,fd=9))\n"
    "\t cubic rto:216 bytes_acked:119 bytes_received:4306 segs_out:36 segs_in:32 lastsnd:14752 lastrcv:14584\n"
    "ESTAB 0      0                127.0.0.1:39000       127.0.0.1:3000   users:((\"codex\",pid=4001,fd=20))\n"
    "\t cubic bytes_acked:1 bytes_received:2 segs_in:3 lastrcv:5\n"
    "ESTAB 0      0     [::ffff:127.0.0.1]:39002 [::ffff:127.0.0.1]:3000   users:((\"codex\",pid=4001,fd=21))\n"
    "\t cubic bytes_received:2\n"
    "ESTAB 0      0     [2001:db8::10]:50000     [2001:db8::99]:443       users:((\"codex\",pid=4002,fd=11))\n"
    "\t cubic bytes_acked:77 bytes_received:88 segs_in:9 lastrcv:120\n"
    "ESTAB 0      0     [::1]:50001              [::1]:3000               users:((\"codex\",pid=4002,fd=12))\n"
    "\t cubic bytes_received:2\n"
    "ESTAB 0      0           192.0.2.10:55936      34.149.66.165:443  users:((\"other\",pid=9999,fd=15))\n"
    "\t cubic bytes_received:4669 segs_in:84 lastrcv:10000\n"
    "TIME-WAIT 0    0           192.0.2.10:55937      34.149.66.165:443\n"
    "ESTAB 0      0           192.0.2.10:44000      198.51.100.8:443   users:((\"npm exec @model\",pid=4003,fd=1))\n"
    "\t cubic bytes_received:10 segs_in:2 lastrcv:7\n"
)

# Scrubbed `ss -xpH`: 4001 <-> 4003 is a connected pair; 4002's peer has an unknown owner.
SS_UNIX = (
    "u_str ESTAB 0      0                                      * 5001 * 5002 users:((\"codex\",pid=4001,fd=0))\n"
    "u_str ESTAB 0      0                                      * 5002 * 5001 users:((\"npm exec @model\",pid=4003,fd=1),(\"sh\",pid=4004,fd=1))\n"
    "u_str ESTAB 0      0                    /tmp/obs.sock 5003 * 5004 users:((\"codex\",pid=4002,fd=3))\n"
    "u_str ESTAB 0      0                                      * 5009 * 5010\n"
    "u_str ESTAB 0      0                                      * 5100 * 5101 users:((\"other\",pid=9999,fd=1))\n"
)


class ParseSsTcp(unittest.TestCase):
    def test_counters_and_peer(self):
        rows = netwatch.parse_ss_tcp(SS_TCP)
        first = [r for r in rows if r["pid"] == 4001 and r["fd"] == 18][0]
        self.assertEqual(first["state"], "ESTAB")
        self.assertEqual(first["peer"], "198.51.100.7")
        self.assertEqual(first["port"], 443)
        self.assertEqual(first["bytes_received"], 8318)
        self.assertEqual(first["bytes_acked"], 25894)
        self.assertEqual(first["segs_in"], 147)
        self.assertEqual(first["lastrcv_ms"], 3992)
        self.assertEqual(first["comm"], "codex")

    def test_loopback_excluded_by_default(self):
        rows = netwatch.parse_ss_tcp(SS_TCP)
        self.assertFalse(any(r["loopback"] for r in rows))
        self.assertFalse(any(r["peer"] in ("127.0.0.1", "::1") for r in rows))
        self.assertNotIn(20, [r["fd"] for r in rows if r["pid"] == 4001])
        self.assertNotIn(21, [r["fd"] for r in rows if r["pid"] == 4001])
        self.assertNotIn(12, [r["fd"] for r in rows if r["pid"] == 4002])
        full = netwatch.parse_ss_tcp(SS_TCP, include_loopback=True)
        self.assertEqual(sum(1 for r in full if r["loopback"]), 3)

    def test_shared_socket_one_row_per_owner_and_ipv6(self):
        rows = netwatch.parse_ss_tcp(SS_TCP)
        shared = [r for r in rows if r["fd"] == 9]
        self.assertEqual(sorted(r["pid"] for r in shared), [4001, 4002])
        v6 = [r for r in rows if r["peer"] == "2001:db8::99"]
        self.assertEqual(len(v6), 1)
        self.assertEqual((v6[0]["pid"], v6[0]["port"], v6[0]["lastrcv_ms"]), (4002, 443, 120))

    def test_ownerless_and_spaced_names(self):
        rows = netwatch.parse_ss_tcp(SS_TCP)
        self.assertFalse(any(r["state"] == "TIME-WAIT" for r in rows))
        self.assertEqual([r["comm"] for r in rows if r["pid"] == 4003], ["npm exec @model"])

    def test_missing_counters_are_none_and_garbage_is_skipped(self):
        rows = netwatch.parse_ss_tcp("ESTAB 0 0 192.0.2.1:1 198.51.100.1:443 users:((\"x\",pid=1,fd=3))\nnot a socket\n")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["bytes_received"])
        self.assertEqual(netwatch.parse_ss_tcp(""), [])


class ParseSsUnix(unittest.TestCase):
    def test_rows(self):
        rows = netwatch.parse_ss_unix(SS_UNIX)
        r = [x for x in rows if x["pid"] == 4001][0]
        self.assertEqual((r["inode"], r["peer_inode"], r["fd"], r["state"]), (5001, 5002, 0, "ESTAB"))
        shared = [x for x in rows if x["inode"] == 5002]
        self.assertEqual(sorted(x["pid"] for x in shared), [4003, 4004])
        named = [x for x in rows if x["pid"] == 4002][0]
        self.assertEqual(named["local"], "/tmp/obs.sock")
        self.assertTrue(any(x["pid"] is None and x["inode"] == 5009 for x in rows))


def _write_proc(proc, pid, ppid, state="S", env=(), comm="x"):
    d = os.path.join(proc, str(pid))
    os.makedirs(d)
    with open(os.path.join(d, "stat"), "w") as f:
        f.write("%d (%s) %s %d %d 0 0 0\n" % (pid, comm, state, ppid, pid))
    with open(os.path.join(d, "environ"), "wb") as f:
        f.write(b"\0".join(e.encode() for e in env) + b"\0")


class FakeProc(unittest.TestCase):
    def setUp(self):
        self.proc = tempfile.mkdtemp(prefix="nw-proc-")
        self.addCleanup(shutil.rmtree, self.proc, ignore_errors=True)
        _write_proc(self.proc, 1, 0, comm="init")
        _write_proc(self.proc, 4000, 1, comm="reaper (x) y", env=["COUNCIL_SEAT_TAG=tagA", "PATH=/bin"])
        _write_proc(self.proc, 4001, 4000, env=["COUNCIL_SEAT_TAG=tagA"])
        _write_proc(self.proc, 4002, 4001, env=[])                          # scrubbed env, descendant
        _write_proc(self.proc, 4003, 1, env=["COUNCIL_SEAT_TAG=tagA"])      # reparented to init
        _write_proc(self.proc, 4004, 4003, state="T", env=[])               # child of a stopped one
        _write_proc(self.proc, 4005, 1, state="Z", env=["COUNCIL_SEAT_TAG=tagA"])
        _write_proc(self.proc, 5000, 1, env=["COUNCIL_SEAT_TAG=tagB"])
        _write_proc(self.proc, 5001, 1, env=["COUNCIL_SEAT_TAG=tagAB", "X=COUNCIL_SEAT_TAG=tagA"])
        os.makedirs(os.path.join(self.proc, "999"))                         # vanished: no stat
        os.makedirs(os.path.join(self.proc, "self"))

    def test_tag_only_exact_and_zombies_dropped(self):
        self.assertEqual(netwatch.tagged_pids("tagA", proc=self.proc), [4000, 4001, 4003])
        self.assertEqual(netwatch.tagged_pids("tagB", proc=self.proc), [5000])
        self.assertEqual(netwatch.tagged_pids("", proc=self.proc), [])
        self.assertEqual(netwatch.tagged_pids("nope", proc=self.proc), [])

    def test_root_pid_descendants_added(self):
        self.assertEqual(netwatch.tagged_pids("tagA", root_pid=4000, proc=self.proc),
                         [4000, 4001, 4002, 4003])
        self.assertEqual(netwatch.tagged_pids("nope", root_pid=4003, proc=self.proc), [4003, 4004])
        self.assertEqual(netwatch.tagged_pids("nope", root_pid=123456, proc=self.proc), [])

    def test_sample_states_children_and_stopped_state(self):
        ns = netwatch.sample([4003, 4004, 4001], ss_runner=lambda a: "", proc=self.proc, now=5.0)
        self.assertEqual(ns.t, 5.0)
        self.assertEqual(ns.states, {4001: "S", 4003: "S", 4004: "T"})
        self.assertEqual(ns.children, {4001: [4002], 4003: [4004]})
        self.assertTrue(ns.ss_ok)
        self.assertEqual((ns.tcp, ns.unix), ([], []))


class Sample(unittest.TestCase):
    def setUp(self):
        self.proc = tempfile.mkdtemp(prefix="nw-proc-")
        self.addCleanup(shutil.rmtree, self.proc, ignore_errors=True)
        for pid in (4001, 4002, 4003, 4004):
            _write_proc(self.proc, pid, 1)

    @staticmethod
    def runner(args):
        return {"-tinpH": SS_TCP, "-xpH": SS_UNIX}[args[0]]

    def test_filters_to_the_pid_set_and_resolves_unix_peers(self):
        ns = netwatch.sample([4001, 4002], ss_runner=self.runner, proc=self.proc, now=1.0)
        self.assertTrue(ns.ss_ok)
        self.assertEqual(sorted((s["pid"], s["fd"]) for s in ns.tcp),
                         [(4001, 9), (4001, 18), (4002, 9), (4002, 11)])
        self.assertTrue(all(not s["loopback"] and s["pid"] in (4001, 4002) for s in ns.tcp))
        self.assertEqual({(u["pid"], u["peer_pid"]) for u in ns.unix},
                         {(4001, 4003), (4001, 4004), (4002, None)})   # unknown owner stays None

    def test_ss_failure_is_not_evidence(self):
        def boom(args):
            raise RuntimeError("ss rc=1")
        ns = netwatch.sample([4001], ss_runner=boom, proc=self.proc)
        self.assertFalse(ns.ss_ok)
        self.assertEqual(ns.tcp, [])
        self.assertEqual(len(ns.errors), 2)

    def test_partial_failure_still_marks_not_ok(self):
        def half(args):
            if args[0] == "-xpH":
                raise RuntimeError("unix failed")
            return SS_TCP
        ns = netwatch.sample([4001], ss_runner=half, proc=self.proc)
        self.assertFalse(ns.ss_ok)
        self.assertEqual(len(ns.tcp), 2)


class RealProc(unittest.TestCase):
    """A real reparented child, found by its tag in the live /proc."""

    CODE = ("import os, time\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            "    time.sleep(25)\n"
            "os._exit(0)\n")

    def test_reparented_child_with_own_pgid_found_by_tag(self):
        tag = "t-" + uuid.uuid4().hex
        env = dict(os.environ, COUNCIL_SEAT_TAG=tag)
        p = subprocess.Popen([PY, "-c", self.CODE], env=env)
        found = []
        try:
            p.wait(timeout=10)
            deadline = time.time() + 5
            while time.time() < deadline and not found:
                found = netwatch.tagged_pids(tag)
                time.sleep(0.05)
            self.assertEqual(len(found), 1)
            self.assertNotEqual(os.getpgid(found[0]), os.getpgid(0))
            ns = netwatch.sample(found, ss_runner=lambda a: "")
            self.assertEqual(list(ns.states), found)
            self.assertIn(ns.states[found[0]], ("S", "R"))
            os.kill(found[0], signal.SIGSTOP)
            time.sleep(0.1)
            self.assertEqual(netwatch.sample(found, ss_runner=lambda a: "").states[found[0]], "T")
        finally:
            for pid in found or netwatch.tagged_pids(tag):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        time.sleep(0.2)
        self.assertEqual(netwatch.tagged_pids(tag), [])


if __name__ == "__main__":
    unittest.main()
