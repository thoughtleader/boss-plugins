"""Supervisor + reaper tests (row 3; watch mode row 27). Limits: idle 2, ceiling 8, grace 1, deadline 3.

Run: python3 -Es -m unittest discover -s ~/.claude/council/tests -t ~/.claude/council
"""
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib.supervise import (  # noqa: E402
    Launch, Limits, Notice, SamplerError, Supervisor, WatchLimits, run_one)

STUB = os.path.join(ROOT, "tests", "stub_cli.py")
PY = sys.executable or "/usr/bin/python3"

HARNESS = r'''
import json, os, sys
sys.path.insert(0, %(root)r)
from councillib.supervise import Supervisor, Launch, Limits, install_signal_handlers
info, mode, pidfile = sys.argv[1], sys.argv[2], sys.argv[3]
sup = Supervisor()
env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "STUB_PIDFILE": pidfile}
sup.start("s", Launch(argv=[sys.executable, "-Es", %(stub)r, mode], env=env,
                      cwd=os.path.dirname(info)),
          Limits(mode="stream", idle_s=60, ceiling_s=120, deadline_s=None, grace_s=1))
# installed after start on purpose: the reaper inherits whatever dispositions we had
install_signal_handlers(sup, lambda: None)
with open(info + ".tmp", "w") as f:
    json.dump({"pid": os.getpid(), "pgid": sup.pgid("s")}, f)
os.replace(info + ".tmp", info)
while True:
    sup.poll(0.5)
'''


def alive(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
    except OSError:
        return False
    return data[data.rfind(b")") + 2:].split()[0] != b"Z"


def group_pids(pgid):
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as f:
                fields = f.read().rsplit(b")", 1)[1].split()
        except OSError:
            continue
        if fields[0] != b"Z" and int(fields[2]) == pgid:
            out.append(int(name))
    return out


def wait_gone(pids, timeout):
    """Poll until no pid is alive (zombies count as gone); return survivors."""
    end = time.monotonic() + timeout
    while True:
        left = [p for p in pids if alive(p)]
        if not left or time.monotonic() >= end:
            return left
        time.sleep(0.05)


def wait_for(pred, timeout, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-sup-")
        self.pidfile = os.path.join(self.tmp, "pids")
        self.sup = Supervisor()
        self.pgids = []
        self.extra_pids = []

    def tearDown(self):
        try:
            if self.sup.running():
                self.sup.kill_all()
        finally:
            for pg in self.pgids:
                try:
                    os.killpg(pg, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            for pid in self.extra_pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for pid in self.read_pids_flat():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers -----------------------------------------------------------
    def env(self, **extra):
        e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "STUB_PIDFILE": self.pidfile}
        e.update(extra)
        return e

    def launch(self, *stub_args, stdin_path=None, env=None, cwd=None):
        return Launch(argv=[PY, "-Es", STUB, *stub_args], env=env or self.env(),
                      cwd=cwd or self.tmp, stdin_path=stdin_path)

    @staticmethod
    def limits(mode="stream", deadline_s=3):
        return Limits(mode=mode, idle_s=2, ceiling_s=8, deadline_s=deadline_s, grace_s=1)

    def start(self, key, launch, limits, on_output=None):
        self.sup.start(key, launch, limits, on_output=on_output)
        pg = self.sup.pgid(key)
        if pg:
            self.pgids.append(pg)
        return pg

    def collect(self, n=1, timeout=20):
        got = {}
        end = time.monotonic() + timeout
        while len(got) < n and time.monotonic() < end:
            for k, r in self.sup.poll(0.5):
                got[k] = r
        self.assertEqual(len(got), n, "seats did not finish in %ss" % timeout)
        return got

    def run_seat(self, launch, limits, on_output=None):
        t0 = time.monotonic()
        pg = self.start("s", launch, limits, on_output)
        res = self.collect(1)["s"]
        return res, time.monotonic() - t0, pg

    def read_pids(self):
        roles = {}
        try:
            with open(self.pidfile) as f:
                for line in f:
                    role, pid = line.split()
                    roles.setdefault(role, []).append(int(pid))
        except OSError:
            pass
        return roles

    def read_pids_flat(self):
        return [p for ps in self.read_pids().values() for p in ps]

    def harness(self, mode, trap=False):
        path = os.path.join(self.tmp, "harness.py")
        with open(path, "w") as f:
            f.write(HARNESS % {"root": ROOT, "stub": STUB})
        info = os.path.join(self.tmp, "info.json")
        cmd = [PY, "-Es", path, info, mode, self.pidfile]
        if trap:
            argv = ["bash", "-c", "trap '' TERM HUP INT; exec " + " ".join(map(shlex.quote, cmd))]
        else:
            argv = cmd
        with open(os.path.join(self.tmp, "harness.err"), "wb") as err:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err)
        self.extra_pids.append(proc.pid)
        ok = wait_for(lambda: os.path.exists(info), 10)
        self.assertTrue(ok, "harness never wrote info")
        with open(info) as f:
            meta = json.load(f)
        self.pgids.append(meta["pgid"])
        self.assertEqual(meta["pid"], proc.pid)  # exec kept the pid
        return proc, meta

    # ---- cases -------------------------------------------------------------
    def test_01_slow_stream_ok(self):
        res, _, _ = self.run_seat(self.launch("slow-stream", "6"), self.limits())
        self.assertEqual(res.status, "ok")
        self.assertGreaterEqual(res.total_s, 5)
        self.assertFalse(res.truncated)
        self.assertIn(b"DONE", res.stdout)
        self.assertLess(res.max_gap_s, 1.5)

    def test_02_hung_idle_kill(self):
        res, _, _ = self.run_seat(self.launch("hung"), self.limits())
        self.assertEqual(res.status, "idle_kill")
        self.assertTrue(1.5 <= res.total_s <= 4, res.total_s)
        self.assertTrue(res.truncated)
        self.assertEqual(res.extensions, 0)

    def test_03_partial_then_hang_keeps_partial(self):
        res, _, _ = self.run_seat(self.launch("partial-then-hang"), self.limits())
        self.assertEqual(res.status, "idle_kill")
        self.assertIn(b"PARTIAL-123", res.stdout)
        self.assertTrue(res.truncated)
        self.assertIsNotNone(res.first_byte_s)

    def test_04_busy_silent_one_extension_ok(self):
        res, _, _ = self.run_seat(self.launch("busy-silent", "3"), self.limits())
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.extensions, 1)
        self.assertFalse(res.truncated)

    def test_05_forever_busy_extension_then_idle_kill(self):
        res, _, _ = self.run_seat(self.launch("forever-busy"), self.limits())
        self.assertEqual(res.extensions, 1)
        self.assertEqual(res.status, "idle_kill")
        self.assertTrue(3 <= res.total_s <= 7, res.total_s)

    def test_06_drip_forever_ceiling_kill(self):
        res, _, _ = self.run_seat(self.launch("drip-forever"), self.limits())
        self.assertEqual(res.status, "ceiling_kill")
        self.assertTrue(7 <= res.total_s <= 10, res.total_s)
        self.assertTrue(res.truncated)
        self.assertGreater(res.out_bytes, 5)

    def test_07_buffered_hung_deadline_kill(self):
        res, _, _ = self.run_seat(self.launch("hung"), self.limits("buffered", 3))
        self.assertEqual(res.status, "deadline_kill")
        self.assertTrue(2.5 <= res.total_s <= 5, res.total_s)
        self.assertTrue(res.truncated)

    def test_08_grandchild_killed_with_group(self):
        res, _, pg = self.run_seat(self.launch("grandchild-hang"), self.limits())
        self.assertEqual(res.status, "idle_kill")
        pids = self.read_pids()
        self.assertEqual(len(pids.get("grandchild", [])), 1, pids)
        self.assertEqual(wait_gone(pids["main"] + pids["grandchild"] + [pg], 2), [])
        self.assertEqual(group_pids(pg), [])

    def test_09_sig_ign_parent_term_cleans_all(self):
        proc, meta = self.harness("hung", trap=True)
        self.assertTrue(wait_for(lambda: self.read_pids().get("main"), 5))
        stub = self.read_pids()["main"][0]
        everyone = [proc.pid, meta["pgid"], stub]
        self.assertTrue(all(alive(p) for p in everyone))
        t0 = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        rc = proc.wait(timeout=5)
        self.assertEqual(wait_gone(everyone, 2), [])
        self.assertLessEqual(time.monotonic() - t0, 2.5)
        self.assertEqual(rc, 128 + signal.SIGTERM)
        self.assertEqual(group_pids(meta["pgid"]), [])

    def test_10_kill9_harness_stub_gone(self):
        proc, meta = self.harness("hung")
        self.assertTrue(wait_for(lambda: self.read_pids().get("main"), 5))
        stub = self.read_pids()["main"][0]
        self.assertTrue(alive(stub) and alive(meta["pgid"]))
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        self.assertEqual(wait_gone([stub, meta["pgid"]], 2), [])

    def test_11_three_parallel_seats(self):
        t0 = time.monotonic()
        for k in ("a", "b", "c"):
            self.start(k, self.launch("slow-stream", "2"), self.limits())
        got = self.collect(3)
        wall = time.monotonic() - t0
        self.assertEqual({r.status for r in got.values()}, {"ok"})
        self.assertLess(wall, 3.5)

    def test_12_cut_keeps_partial(self):
        seen = []
        self.start("s", self.launch("partial-then-hang"), self.limits(), on_output=seen.append)
        end = time.monotonic() + 5
        while not seen and time.monotonic() < end:
            self.assertEqual(self.sup.poll(0.2), [])
        self.assertTrue(seen and isinstance(seen[0], bytes))
        self.sup.cut("s")
        res = self.collect(1)["s"]
        self.assertEqual(res.status, "cut")
        self.assertTrue(res.truncated)
        self.assertIn(b"PARTIAL-123", res.stdout)

    def test_13_exit_code_passthrough(self):
        res, _, _ = self.run_seat(self.launch("exit", "3"), self.limits())
        self.assertEqual(res.status, "exit_3")
        self.assertEqual(res.exit_code, 3)
        self.assertFalse(res.truncated)

    def test_14_echo_stdin_file(self):
        path = os.path.join(self.tmp, "in.txt")
        body = b"line one\nline two \xe2\x9c\x93\n" * 200
        with open(path, "wb") as f:
            f.write(body)
        res, _, _ = self.run_seat(self.launch("echo-stdin", stdin_path=path), self.limits())
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.stdout, body)

    def test_15_env_and_cwd(self):
        cwd = os.path.join(self.tmp, "work dir")
        os.mkdir(cwd)
        env = self.env(COUNCIL_TEST_VAR="v-15")
        res, _, _ = self.run_seat(self.launch("env-cwd", "COUNCIL_TEST_VAR", env=env, cwd=cwd),
                                  self.limits())
        self.assertEqual(res.status, "ok")
        lines = res.stdout.decode().splitlines()
        self.assertEqual(lines, ["ENV=v-15", "CWD=" + cwd])
        del res, lines, env  # assert, then discard
        # a cwd that doesn't exist fails at spawn
        self.sup.start("bad", self.launch("quick", cwd=os.path.join(self.tmp, "missing")),
                       self.limits())
        bad = self.collect(1)["bad"]
        self.assertEqual(bad.status, "spawn_error")

    def test_16_kill9_harness_wrapper_and_grandchild_gone(self):
        proc, meta = self.harness("wrapper-grandchild-hang")
        roles = wait_for(lambda: (lambda r: r if r.get("main") and r.get("grandchild") else None)
                         (self.read_pids()), 5)
        self.assertTrue(roles, "wrapper/grandchild never started")
        wrapper, grand = roles["main"][0], roles["grandchild"][0]
        self.assertTrue(alive(wrapper) and alive(grand) and alive(meta["pgid"]))
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        self.assertEqual(wait_gone([wrapper, grand, meta["pgid"]], 2), [])

    def test_17_wrapper_exits_grandchild_holds_pipe(self):
        # R2-W2: completion = reaper exit, not pipe EOF; survivors are killed
        res, _, pg = self.run_seat(self.launch("wrapper-exit-grandchild-hang"), self.limits())
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.exit_code, 0)
        self.assertFalse(res.truncated)
        self.assertIn(b"WRAPPER-DONE", res.stdout)
        self.assertLess(res.total_s, 1.5)
        grand = self.read_pids().get("grandchild", [])
        self.assertEqual(len(grand), 1)
        self.assertEqual(wait_gone(grand, 1), [])
        self.assertEqual(group_pids(pg), [])


def wlimits(ceiling_s=None, **kw):
    """Watch mode with tiny idle/deadline values that watch mode must ignore."""
    return WatchLimits(mode="watch", idle_s=1, ceiling_s=ceiling_s, deadline_s=1, grace_s=1, **kw)


WRITER = r"""
import sys, time
try:
    for i in range(10):
        sys.stdout.write("line %d\n" % i)
        sys.stdout.flush()
        sys.stderr.write("e%d\n" % i)
        sys.stderr.flush()
        time.sleep(0.1)
    sys.stdout.write("ANSWER\n")
    sys.stdout.flush()
except BrokenPipeError:
    sys.exit(101)
"""


class WatchModeTests(unittest.TestCase):
    """Row 27: watch mode, soft notice, explicit terminate, sampler seam."""
    setUp = SupervisorTests.setUp
    tearDown = SupervisorTests.tearDown
    env = SupervisorTests.env
    launch = SupervisorTests.launch
    start = SupervisorTests.start
    read_pids = SupervisorTests.read_pids
    read_pids_flat = SupervisorTests.read_pids_flat

    def drain(self, n=1, timeout=20):
        """Poll until n results; returns ({key: Result}, [Notice])."""
        got, notes = {}, []
        end = time.monotonic() + timeout
        while len(got) < n and time.monotonic() < end:
            for item in self.sup.poll(0.5):
                if isinstance(item, Notice):
                    notes.append(item)
                else:
                    got[item[0]] = item[1]
        self.assertEqual(len(got), n, "seats did not finish in %ss" % timeout)
        return got, notes

    def poll_for(self, seconds):
        items = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            items += self.sup.poll(0.2)
        return items

    def test_w01_silent_and_busy_never_killed_on_time(self):
        self.start("hung", self.launch("hung"), wlimits())
        self.start("busy", self.launch("forever-busy"), wlimits())
        self.assertEqual(self.poll_for(4.0), [])      # idle_s/deadline_s = 1 are ignored
        self.assertEqual(sorted(self.sup.running()), ["busy", "hung"])
        self.assertTrue(self.sup.terminate("hung", "no_connection"))
        self.assertTrue(self.sup.terminate("busy", "no_connection"))
        got, notes = self.drain(2)
        self.assertEqual(notes, [])
        for res in got.values():
            self.assertEqual(res.status, "no_connection")
            self.assertEqual(res.extensions, 0)
            self.assertTrue(res.truncated)
            self.assertGreaterEqual(res.total_s, 4.0)

    def test_w02_finite_ceiling_still_kills(self):
        self.start("s", self.launch("hung"), wlimits(ceiling_s=2))
        res = self.drain()[0]["s"]
        self.assertEqual(res.status, "ceiling_kill")
        self.assertTrue(1.5 <= res.total_s <= 4.5, res.total_s)
        self.assertTrue(res.truncated)

    def test_w03_soft_notice_once_then_ok(self):
        self.start("s", self.launch("slow-stream", "2"), wlimits(soft_at_s=0.5))
        got, notes = self.drain()
        self.assertEqual(len(notes), 1, notes)
        kind, key, t = notes[0]
        self.assertEqual((kind, key), ("soft", "s"))
        self.assertTrue(0.5 <= t < 1.5, t)
        res = got["s"]
        self.assertEqual(res.status, "ok")
        self.assertFalse(res.truncated)
        self.assertIn(b"DONE", res.stdout)
        self.assertLess(res.max_gap_s, 1.5)
        # run_one skips notices
        res = run_one(self.launch("quick"), wlimits(soft_at_s=0))
        self.assertEqual((res.status, res.stdout), ("ok", b"QUICK-OK\n"))

    def test_w04_terminate_keeps_partial(self):
        seen = []
        self.start("s", self.launch("partial-then-hang"), wlimits(), on_output=seen.append)
        end = time.monotonic() + 5
        while not seen and time.monotonic() < end:
            self.assertEqual(self.sup.poll(0.2), [])
        self.assertTrue(seen and isinstance(seen[0], bytes))
        with self.assertRaises(ValueError):
            self.sup.terminate("s", "ok")
        self.assertFalse(self.sup.terminate("nope", "no_connection"))
        self.assertTrue(self.sup.terminate("s", "quota_exhausted"))
        self.assertFalse(self.sup.terminate("s", "no_connection"))   # first reason wins
        res = self.drain()[0]["s"]
        self.assertEqual(res.status, "quota_exhausted")
        self.assertTrue(res.truncated)
        self.assertIn(b"PARTIAL-123", res.stdout)
        self.assertIsNone(res.stdout_path)                            # temp capture

    def test_w05_terminate_after_exit_keeps_real_status(self):
        self.start("s", self.launch("quick"), wlimits())
        info = self.sup.info("s")
        tmp_dir = os.path.dirname(info.stdout_path)
        self.assertTrue(os.path.isfile(info.stdout_path))
        self.assertEqual(os.stat(tmp_dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(info.stdout_path).st_mode & 0o777, 0o600)
        time.sleep(1.0)                    # exits while nobody polls
        self.assertFalse(self.sup.terminate("s", "no_connection"))
        res = self.drain()[0]["s"]
        self.assertEqual((res.status, res.exit_code, res.truncated), ("ok", 0, False))
        self.assertEqual(res.stdout, b"QUICK-OK\n")
        self.assertFalse(os.path.exists(tmp_dir))   # private temp capture removed

    def test_w06_sampler_throttled_and_exposed(self):
        calls = []

        def sampler(info):
            calls.append(info)
            return {"n": len(calls), "key": info.key}
        self.sup = Supervisor(sampler=sampler)
        env = self.env(COUNCIL_SEAT_TAG="tag-27")
        pg = self.start("s", self.launch("slow-stream", "2", env=env), wlimits(sample_s=0.4))
        self.start("q", self.launch("slow-stream", "1"), wlimits())   # no sample_s: never sampled
        self.assertIsNone(self.sup.liveness("s"))
        self.drain(2)
        self.assertEqual({c.key for c in calls}, {"s"})
        self.assertTrue(3 <= len(calls) <= 7, len(calls))
        steps = [b.elapsed_s - a.elapsed_s for a, b in zip(calls, calls[1:])]
        self.assertTrue(all(d >= 0.39 for d in steps), steps)
        self.assertGreaterEqual(calls[0].elapsed_s, 0.4)
        c = calls[-1]
        self.assertEqual((c.mode, c.pid, c.tag), ("watch", pg, "tag-27"))
        self.assertGreater(c.out_bytes, 0)
        self.assertIsNotNone(c.last_out_s)
        self.assertEqual(self.sup.liveness("s"), {"n": len(calls), "key": "s"})  # kept after end
        self.assertIsNone(self.sup.liveness("q"))

    def test_w07_raising_sampler_and_callback_never_touch_the_seat(self):
        def sampler(info):
            raise RuntimeError("boom")

        def bad_output(chunk):
            raise ValueError("callback")
        self.sup = Supervisor(sampler=sampler)
        self.start("s", self.launch("slow-stream", "1"), wlimits(sample_s=0.2),
                   on_output=bad_output)
        res = self.drain()[0]["s"]
        self.assertEqual(res.status, "ok")
        self.assertIn(b"DONE", res.stdout)
        v = self.sup.liveness("s")
        self.assertIsInstance(v, SamplerError)
        self.assertIn("RuntimeError: boom", v.error)

    def test_w08_cli_writes_to_files_not_pipes(self):
        out = os.path.join(self.tmp, "s.stdout")
        err = os.path.join(self.tmp, "s.stderr")
        la = Launch(argv=[PY, "-Es", STUB, "partial-then-hang"], env=self.env(), cwd=self.tmp,
                    stdout_path=out, stderr_path=err)
        pg = self.start("s", la, wlimits())
        self.assertTrue(wait_for(lambda: self.read_pids().get("main"), 5))
        main = self.read_pids()["main"][0]
        for pid in (pg, main):     # no pipe the CLI could hit EPIPE on (row 25, rc 101)
            self.assertEqual(os.readlink("/proc/%d/fd/1" % pid), out)
            self.assertEqual(os.readlink("/proc/%d/fd/2" % pid), err)
        self.assertTrue(wait_for(lambda: os.path.getsize(out) > 0, 5))
        self.assertTrue(self.sup.terminate("s", "no_connection"))
        res = self.drain()[0]["s"]
        self.assertEqual((res.stdout_path, res.stderr_path), (out, err))
        with open(out, "rb") as f:
            self.assertEqual(f.read(), res.stdout)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        # a capture is never overwritten: an existing path is a spawn_error
        self.sup.start("again", la, wlimits())
        again = self.drain()[0]["again"]
        self.assertEqual(again.status, "spawn_error")
        with open(out, "rb") as f:
            self.assertIn(b"PARTIAL-123", f.read())

    def test_w09_stalled_reader_and_raising_callback_lose_nothing(self):
        out = os.path.join(self.tmp, "w.stdout")
        la = Launch(argv=[PY, "-Es", "-c", WRITER], env=self.env(), cwd=self.tmp,
                    stdout_path=out, stderr_path=os.path.join(self.tmp, "w.stderr"))

        def bad_output(chunk):
            raise ValueError("callback")
        self.start("w", la, wlimits(), on_output=bad_output)
        self.sup.poll(0.1)
        time.sleep(1.5)                    # supervisor stalls: nobody reads for 1.5 s
        res = self.drain()[0]["w"]
        self.assertEqual((res.status, res.exit_code), ("ok", 0))   # not 101
        lines = res.stdout.decode().splitlines()
        self.assertEqual(lines, ["line %d" % i for i in range(10)] + ["ANSWER"])
        self.assertIn(b"e9", res.stderr_tail)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), res.stdout)

    def test_w11_limits_fields_unchanged_for_drift(self):
        # drift.py snapshots dataclasses.asdict(limits): new knobs live on WatchLimits only
        import dataclasses
        self.assertEqual([f.name for f in dataclasses.fields(Limits)],
                         ["mode", "idle_s", "ceiling_s", "deadline_s", "grace_s",
                          "max_extensions", "extension_factor"])
        res = run_one(self.launch("quick"), Limits("watch", 1, None, 1, grace_s=1))
        self.assertEqual(res.status, "ok")       # plain Limits can still run watch mode

    def test_w10_mode_and_sample_validation(self):
        with self.assertRaises(ValueError):
            self.sup.start("x", self.launch("quick"), Limits("bogus", 1, 1, None))
        with self.assertRaises(ValueError):
            self.sup.start("x", self.launch("quick"), wlimits(sample_s=0))
        self.assertEqual(self.sup.running(), [])


class NoProcFallbackTests(unittest.TestCase):
    """No /proc (macOS, BSD): the group listing and the activity sample come from `ps`; nothing raises, and an
    unlistable group is never read as an empty one. /proc is faked absent by patching `_have_proc` (fast, no sleep)."""

    def setUp(self):
        from unittest import mock
        from councillib import reap, supervise
        self.supervise, self.reap = supervise, reap
        for mod in (supervise, reap):
            p = mock.patch.object(mod, "_have_proc", return_value=False)
            p.start()
            self.addCleanup(p.stop)
        # a seat group: its own session, so its pgid is its pid
        self.proc = subprocess.Popen([PY, "-Es", "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(lambda: (self.proc.kill(), self.proc.wait()))
        self.pgid = self.proc.pid

    def test_cpu_seconds_parses_ps_time(self):
        f = self.supervise._cpu_seconds
        self.assertEqual(f("0:01.50"), 1.5)
        self.assertEqual(f("01:02:03"), 3723.0)
        self.assertEqual(f("2-00:00:10"), 2 * 86400 + 10)
        self.assertEqual(f("garbage"), 0.0)

    def test_supervise_lists_the_group_and_samples_activity_from_ps(self):
        self.assertIn(self.proc.pid, self.supervise._group_pids(self.pgid))
        self.assertNotIn(self.proc.pid, self.supervise._group_pids(self.pgid, exclude=(self.proc.pid,)))
        cpu, io = self.supervise._activity(self.pgid, reaper_pid=-1)
        self.assertIsInstance(cpu, int)
        self.assertEqual(io, 0)

    def test_supervise_without_ps_is_empty_not_an_error(self):
        from unittest import mock
        with mock.patch.object(self.supervise.subprocess, "run", side_effect=FileNotFoundError("ps")):
            self.assertEqual(self.supervise._group_pids(self.pgid), [])
            self.assertEqual(self.supervise._activity(self.pgid, -1), (0, 0))

    def test_reap_group_alive_follows_the_group(self):
        self.assertTrue(self.reap.group_alive(self.pgid))
        self.assertEqual(self.reap.group_members(self.pgid), [self.proc.pid])
        self.proc.kill()
        self.proc.wait()
        self.assertFalse(self.reap.group_alive(self.pgid))

    def test_reap_unlistable_group_counts_as_alive_so_the_sigkill_still_follows(self):
        from unittest import mock
        with mock.patch.object(self.reap.subprocess, "run", side_effect=FileNotFoundError("ps")):
            self.assertTrue(self.reap.group_alive(self.pgid))
            self.assertEqual(self.reap.group_members(self.pgid), [])      # the sweep never signals a guess
            self.assertTrue(self.reap.group_unknown(self.pgid))           # so the final sweep kills the group whole instead
        self.assertEqual(self.reap.tagged("t"), [])                       # environ scan: Linux only, empty elsewhere
        self.assertFalse(self.reap.group_unknown(self.pgid))              # ps works: the members are named instead

    def test_council_run_proc_identity_from_ps_without_proc(self):
        from unittest import mock
        from councillib import council_run
        with mock.patch.object(council_run.os.path, "isdir", return_value=False):
            start = council_run.proc_start(self.proc.pid)
            self.assertIsInstance(start, int)
            self.assertEqual(council_run.proc_start(self.proc.pid), start)            # stable
            self.assertTrue(council_run.proc_alive(self.proc.pid, start))
            self.assertTrue(council_run.proc_alive(self.proc.pid, None))
            self.assertFalse(council_run.proc_alive(self.proc.pid, start + 1))        # a reused pid is not it
            with mock.patch.object(council_run.subprocess, "run", side_effect=FileNotFoundError("ps")):
                self.assertIsNone(council_run.proc_start(self.proc.pid))
                self.assertTrue(council_run.proc_alive(self.proc.pid, start))          # ps missing: signal 0 says alive, never dead
            self.proc.kill()
            self.proc.wait()
            self.assertFalse(council_run.proc_alive(self.proc.pid, start))


if __name__ == "__main__":
    unittest.main()
