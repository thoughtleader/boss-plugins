"""Live status (run example-run, row 1): status.json, current.json and `council.py status`.

Stub processes only: no network, no paid call. Snapshots are collected two ways: a thread that polls the real file (the
"appears early / changes while seats stream" cases), and a wrapper around status.write_atomic that keeps EVERY snapshot
(the fast-fail chain, whose steps last milliseconds).

Run: python3 -Es -m unittest tests.test_status   (cwd ~/.claude/council)
"""
import contextlib
import io
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import council  # noqa: E402
from councillib import council_run, preflight, status  # noqa: E402
from tests.test_council_run import Base, seat  # noqa: E402
from tests.test_or_fallback import FastFailBase, fseat  # noqa: E402


def poll_file(path, stop, out, every=0.05):
    """Append every distinct parse of the file to `out` as (monotonic, dict); a torn read is recorded as None."""
    last = None
    while not stop.is_set():
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = None
        if text is not None and text != last:
            last = text
            try:
                out.append((time.monotonic(), json.loads(text)))
            except ValueError:
                out.append((time.monotonic(), None))
        time.sleep(every)


class Recorder:
    """Patches status.write_atomic so every snapshot written is kept (and still written)."""

    def __init__(self):
        self.snaps = []
        self._real = status.write_atomic

    def __call__(self, path, data):
        if os.path.basename(path) == status.FILE:
            self.snaps.append(json.loads(json.dumps(data)))
        return self._real(path, data)


# ---------------------------------------------------------------- the writer
class WriterTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.tmp = tempfile.mkdtemp(prefix="council-status-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.t = [100.0]
        self.n = [0]

    def snap(self, phase="seats", extra=0):
        self.n[0] += 1
        return {"phase": phase, "seats": [{"id": "a", "phase": "streaming", "call": "original", "out_bytes": extra}]}

    def writer(self, **kw):
        return status.Writer(self.tmp, dict({"enabled": True, "interval_s": 3.0}, **kw), clock=lambda: self.t[0])

    def test_01_cfg_defaults_and_bad_values(self):
        self.assertEqual(status.cfg_of({}), {"enabled": True, "interval_s": 3.0, "progress": True})
        self.assertEqual(status.cfg_of({"status": {"enabled": False, "interval_s": 1, "progress": False}}),
                         {"enabled": False, "interval_s": 1.0, "progress": False})
        for bad in (0, -2, "x", None):
            self.assertEqual(status.cfg_of({"status": {"interval_s": bad}})["interval_s"], 3.0)

    def test_02_writes_on_change_and_on_the_interval_only(self):
        w = self.writer()
        self.assertTrue(w.tick(lambda: self.snap()))                      # first tick writes
        self.t[0] += 1.0
        self.assertFalse(w.tick(lambda: self.snap(extra=50)))             # only a counter moved, interval not over
        self.t[0] += 2.5
        self.assertTrue(w.tick(lambda: self.snap(extra=90)))              # 3.5 s since the last write: interval
        self.t[0] += 0.1
        self.assertFalse(w.tick(lambda: self.snap("chair")))              # a change, but inside MIN_GAP_S
        self.t[0] += 0.2
        self.assertTrue(w.tick(lambda: self.snap("chair")))               # the change is written as soon as the gap is over
        self.assertEqual(w.writes, 3)

    def test_03_force_always_writes_and_leaves_no_temp_file(self):
        w = self.writer()
        w.tick(lambda: self.snap())
        self.assertTrue(w.tick(lambda: self.snap(), force=True))
        self.assertEqual(os.listdir(self.tmp), [status.FILE])
        self.assertEqual(os.stat(os.path.join(self.tmp, status.FILE)).st_mode & 0o777, 0o600)

    def test_04_a_failing_build_or_write_is_swallowed_and_noted_once(self):
        w = self.writer()
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertFalse(w.tick(lambda: 1 / 0))
            self.t[0] += 5
            with mock.patch.object(status, "write_atomic", side_effect=OSError("disk full")):
                self.assertFalse(w.tick(lambda: self.snap()))
                self.t[0] += 5
                self.assertFalse(w.tick(lambda: self.snap()))
        self.assertEqual(err.getvalue().count("status: cannot write"), 1)
        self.t[0] += 5
        self.assertTrue(w.tick(lambda: self.snap()))                      # it keeps trying: the next good write lands

    def test_05_disabled_writes_nothing(self):
        w = self.writer(enabled=False)
        self.assertFalse(w.tick(lambda: self.snap(), force=True))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_06_title_is_the_first_line_without_markup(self):
        self.assertEqual(status.title_of("\n## Should we ship?\nmore"), "Should we ship?")
        self.assertEqual(len(status.title_of("x" * 300)), 80)
        self.assertEqual(status.title_of(""), "")


# ---------------------------------------------------------------- the pointer
class PointerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.tmp = tempfile.mkdtemp(prefix="council-status-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run1 = os.path.join(self.tmp, "runs", "20261031T000000Z-one")
        os.makedirs(self.run1)

    def test_10_write_read_clear(self):
        status.write_current(self.tmp, self.run1)
        cur = status.current(self.tmp)
        self.assertEqual((cur["out_dir"], cur["pid"]), (self.run1, os.getpid()))
        self.assertTrue(cur["started_at"].endswith("Z"))
        self.assertTrue(status.clear_current(self.tmp, self.run1, os.getpid()))
        self.assertIsNone(status.current(self.tmp))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, status.POINTER)))

    def test_11_a_stale_pointer_is_ignored_and_the_newest_run_is_used(self):
        status.write_current(self.tmp, self.run1, pid=2 ** 22 + 12345)   # no such pid
        self.assertIsNone(status.current(self.tmp))
        self.assertEqual(status.resolve_run(self.tmp), self.run1)        # falls back to the newest run dir
        run2 = os.path.join(self.tmp, "runs", "20261031T010000Z-two")
        os.makedirs(run2)
        self.assertEqual(status.resolve_run(self.tmp), run2)
        status.write_current(self.tmp, self.run1)                        # a live pointer wins over "newest"
        self.assertEqual(status.resolve_run(self.tmp), self.run1)

    def test_12_clear_leaves_a_pointer_that_names_another_run(self):
        status.write_current(self.tmp, self.run1)
        self.assertFalse(status.clear_current(self.tmp, os.path.join(self.tmp, "runs", "other")))
        self.assertIsNotNone(status.current(self.tmp))

    def test_13_a_garbled_pointer_is_no_pointer(self):
        with open(os.path.join(self.tmp, status.POINTER), "w") as f:
            f.write("{not json")
        self.assertIsNone(status.current(self.tmp))
        self.assertFalse(status.clear_current(self.tmp, self.run1))


# ---------------------------------------------------------------- a real run, stub seats
class RunStatusTests(Base):
    def cfg2(self, seats, **kw):
        cfg = self.cfg(seats, **kw)
        cfg["status"] = {"enabled": True, "interval_s": 0.5}
        return cfg

    def test_20_appears_early_changes_while_seats_stream_and_ends_done(self):
        seats = [seat("a", "slow-stream 3"), seat("b", "slow-stream 3"), seat("c", "slow-stream 3")]
        seen, stop = [], threading.Event()
        os.makedirs(self.out)
        th = threading.Thread(target=poll_file, args=(os.path.join(self.out, status.FILE), stop, seen), daemon=True)
        t0 = time.monotonic()
        th.start()
        ptr = []

        def watch_pointer():
            while not stop.is_set():
                cur = status.current(self.state)
                if cur:
                    ptr.append(cur)
                    return
                time.sleep(0.05)
        pt = threading.Thread(target=watch_pointer, daemon=True)
        pt.start()
        rec = Recorder()                                    # every write, for the phase order (a 50 ms poller can miss a short chair phase)
        with mock.patch.object(status, "write_atomic", rec):
            code = self.run_council(self.cfg2(seats))
        stop.set()
        th.join(2)
        pt.join(2)
        with open(os.path.join(self.out, status.FILE), encoding="utf-8") as f:     # the poller may have missed the last write
            last = json.load(f)
        if not seen or seen[-1][1] != last:
            seen.append((time.monotonic(), last))
        self.assertEqual(code, 0)
        self.assertTrue(seen, "status.json was never seen")
        self.assertLess(seen[0][0] - t0, 1.5, "status.json must appear at the start, not at the end")
        self.assertNotIn(None, [s for _, s in seen], "a torn (non-atomic) read was seen")
        snaps = [s for _, s in seen]
        self.assertTrue(all(s["schema_version"] == 1 and s["run_id"] == "20261021T000000Z-test" for s in snaps))
        self.assertEqual(snaps[0]["title"], "What is 2+2?")
        # it changes while the seats stream: bytes grow, a seat goes waiting -> streaming -> done
        streaming = [s for s in snaps if s["phase"] == "seats" and all(x["phase"] == "streaming" for x in s["seats"])]
        self.assertGreaterEqual(len(streaming), 2)
        a_bytes = [s["seats"][0]["out_bytes"] for s in streaming]
        self.assertGreater(max(a_bytes), min(a_bytes))
        self.assertTrue(any(x["last_byte_age_s"] is not None for s in streaming for x in s["seats"]))
        self.assertTrue(any(x["first_byte_s"] is not None for s in streaming for x in s["seats"]))
        phases = []                                         # from the recorder, not the poller: the echo chair lasts milliseconds
        for s in rec.snaps:
            if not phases or phases[-1] != s["phase"]:
                phases.append(s["phase"])
        self.assertEqual(phases, ["seats", "chair", "done"])
        self.assertEqual(rec.snaps[-1], last)               # the poller's final file is the recorder's final write
        # the pointer named this run, with the runner's pid, while it ran; it is gone now
        self.assertEqual((ptr[0]["out_dir"], ptr[0]["pid"]), (self.out, os.getpid()))
        self.assertIsNone(status.current(self.state))
        self.assertFalse(os.path.exists(os.path.join(self.state, status.POINTER)))
        # the final write
        fin = snaps[-1]
        self.assertEqual((fin["phase"], fin["ended"], fin["exit_code"], fin["supervising"]), ("done", True, 0, False))
        self.assertEqual([x["phase"] for x in fin["seats"]], ["done"] * 3)
        self.assertEqual([x["status"] for x in fin["seats"]], ["ok"] * 3)
        self.assertEqual(fin["quorum"]["ok"], 3)
        self.assertEqual(fin["quorum"]["min_ok"], 2)
        self.assertIsNotNone(fin["quorum"]["reached_at_s"])
        self.assertEqual(fin["chair"]["phase"], "done")
        self.assertEqual([n for n in os.listdir(self.out) if n.endswith(".tmp")], [])

    def test_21_a_silent_seat_is_waiting_then_streaming(self):
        seats = [seat("a", "quick"), seat("b", "quick"), seat("c", "partial-then-hang")]
        rec = Recorder()
        with mock.patch.object(status, "write_atomic", rec):
            self.run_council(self.cfg2(seats, idle=2, min_ok="n-1"))
        c_phases = []
        for s in rec.snaps:
            ph = [x["phase"] for x in s["seats"] if x["id"] == "c"][0]
            if not c_phases or c_phases[-1] != ph:
                c_phases.append(ph)
        self.assertEqual(c_phases[0], "waiting")
        self.assertIn("streaming", c_phases)
        self.assertEqual(c_phases[-1], "failed")                          # cut after quorum (Base.cfg cuts stream stragglers)
        fin = rec.snaps[-1]
        self.assertEqual([x["status"] for x in fin["seats"] if x["id"] == "c"], ["cut_after_quorum"])

    def test_22_a_run_below_quorum_ends_failed_with_its_exit_code(self):
        seats = [seat("a", "exit 3"), seat("b", "exit 3"), seat("c", "quick")]
        code = self.run_council(self.cfg2(seats, min_ok="n"))
        self.assertEqual(code, 3)
        fin = status.load(self.out)
        self.assertEqual((fin["phase"], fin["ended"], fin["exit_code"]), ("failed", True, 3))
        self.assertIsNone(fin["chair"])
        self.assertEqual({x["id"]: x["phase"] for x in fin["seats"]}, {"a": "failed", "b": "failed", "c": "done"})
        self.assertIsNone(status.current(self.state))

    def test_23_disabled_writes_no_files(self):
        cfg = self.cfg2([seat("a"), seat("b"), seat("c")])
        cfg["status"] = {"enabled": False}
        self.assertEqual(self.run_council(cfg), 0)
        self.assertFalse(os.path.exists(os.path.join(self.out, status.FILE)))
        self.assertFalse(os.path.exists(os.path.join(self.state, status.POINTER)))

    def test_24_an_unwritable_status_never_fails_the_run(self):
        err = io.StringIO()
        with mock.patch.object(status, "write_atomic", side_effect=OSError("read-only")), \
                mock.patch.object(sys, "stderr", err):
            code = self.run_council(self.cfg2([seat("a"), seat("b"), seat("c")]))
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue().count("status: cannot write"), 1)

    def test_25_a_crash_leaves_the_file_closed_not_running(self):
        os.makedirs(self.out)
        status.write_final(self.out, status.early_snapshot("r", "t", "seats"))
        status.write_current(self.state, self.out)
        with mock.patch.object(council_run, "_run", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                council_run.run(self.cfg2([seat("a")]), "q", [], self.out)
        fin = status.load(self.out)
        self.assertEqual((fin["phase"], fin["ended"]), ("failed", True))
        self.assertIsNone(status.current(self.state))


# ---------------------------------------------------------------- the fast-fail chain shows its steps
class ChainStatusTests(FastFailBase):
    def test_30_original_fallback_reroute_and_stand_in_are_named_and_the_stand_in_is_substituted(self):
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503", stub_fallback="http503", stub_standin="ok",
                                             fallback_model="stand/in-model", fallback_reasoning_effort="medium")]
        cfg = self.fcfg(seats, min_ok="n")
        cfg["status"] = {"enabled": True, "interval_s": 0.5}
        rec = Recorder()
        with mock.patch.object(status, "write_atomic", rec), mock.patch.object(status, "MIN_GAP_S", 0):   # steps last ms here
            self.assertEqual(self.run_council(cfg), 0)
        f_calls = []
        for s in rec.snaps:
            x = [y for y in s["seats"] if y["id"] == "f"][0]
            if x["call"] and x["phase"] in ("waiting", "streaming") and (not f_calls or f_calls[-1] != x["call"]):
                f_calls.append(x["call"])
        self.assertEqual(f_calls, ["original", "fallback-reroute", "stand-in"])
        mid = [y for s in rec.snaps for y in s["seats"] if y["id"] == "f" and y["call"] == "stand-in"
               and y["phase"] in ("waiting", "streaming")][0]
        self.assertEqual(mid["model"], "stand/in-model")                  # the model that really runs
        self.assertIn("original exit_1", mid["detail"])                   # why this call is running
        self.assertIn("fallback-reroute exit_1", mid["detail"])
        fin = rec.snaps[-1]
        f = [y for y in fin["seats"] if y["id"] == "f"][0]
        self.assertEqual((f["phase"], f["status"], f["call"], f["model"]), ("done", "ok", "stand-in", "stand/in-model"))
        self.assertEqual(f["substituted"]["model"], "stand/in-model")
        self.assertEqual([c["call"] for c in f["calls"]], ["original", "fallback-reroute", "stand-in"])
        self.assertEqual([c["phase"] for c in f["calls"]], ["failed", "failed", "done"])
        self.assertTrue(fin["ended"])

    def test_31_a_failed_chain_ends_failed_with_every_step_in_the_detail(self):
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503", stub_fallback="http503")]
        cfg = self.fcfg(seats)
        rec = Recorder()
        with mock.patch.object(status, "write_atomic", rec):
            self.run_council(cfg)
        f = [y for y in rec.snaps[-1]["seats"] if y["id"] == "f"][0]
        self.assertEqual(f["phase"], "failed")
        self.assertEqual(f["detail"], "original exit_1; fallback exit_1")


# ---------------------------------------------------------------- the status command
class CommandTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.tmp = tempfile.mkdtemp(prefix="council-status-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.run1 = os.path.join(self.state, "runs", "20261031T000000Z-one")
        os.makedirs(self.run1)
        self.overlay = os.path.join(self.tmp, "ov.toml")
        with open(self.overlay, "w") as f:
            f.write('state_dir = "%s"\n' % self.state)
        now = time.time()
        snap = status.early_snapshot("20261031T000000Z-one", "Ship it?", "seats", started_epoch=now - 42)
        snap.update(quorum={"ok": 1, "min_ok": 2, "need": 2, "reached_at_s": None}, interval_s=3, seats=[
            {"id": "glm", "provider": "openrouter", "model": "z-ai/glm", "call": "fallback-reroute", "retry": 0,
             "phase": "streaming", "status": None, "elapsed_s": 31.0, "call_elapsed_s": 4.0, "first_byte_s": 1.2,
             "out_bytes": 12345, "last_byte_age_s": 0.8, "detail": "original exit_1", "substituted": None, "calls": []},
            {"id": "claude", "provider": "claude", "model": "opus", "call": "original", "retry": 0, "phase": "done",
             "status": "ok", "elapsed_s": 20.0, "call_elapsed_s": 20.0, "first_byte_s": 3.0, "out_bytes": 4200,
             "last_byte_age_s": None, "detail": None, "substituted": None, "calls": []},
            {"id": "qwen", "provider": "openrouter", "model": "qwen/x", "call": "stand-in", "retry": 2, "phase": "waiting",
             "status": None, "elapsed_s": 2.0, "call_elapsed_s": 2.0, "first_byte_s": None, "out_bytes": 0,
             "last_byte_age_s": None, "detail": None, "substituted": {"model": "qwen/x", "reason": "preflight"},
             "calls": []}])
        self.snap = snap
        status.write_final(self.run1, snap)

    def cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = council.main(["status", "--config", self.overlay, *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_40_table_for_the_newest_run(self):
        rc, out, _ = self.cmd()
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertIn("20261031T000000Z-one", lines[0])
        self.assertIn("Ship it?", lines[0])
        self.assertIn("seats", lines[0])
        glm = [ln for ln in lines if ln.strip().startswith("glm")][0]
        for want in ("◐", "fallback-reroute", "31s", "12.3k", "1s", "original exit_1"):
            self.assertIn(want, glm)
        self.assertIn("●", [ln for ln in lines if ln.strip().startswith("claude")][0])
        qwen = [ln for ln in lines if ln.strip().startswith("qwen")][0]
        self.assertIn("(retry 2)", qwen)
        self.assertIn("stand-in qwen/x", qwen)
        self.assertIn("quorum 1/2", lines[-1])
        self.assertIn("chair not started", lines[-1])

    def test_41_json_adds_the_derived_fields(self):
        rc, out, _ = self.cmd("--json")
        data = json.loads(out)
        self.assertEqual((rc, data["run_id"], data["schema_version"]), (0, "20261031T000000Z-one", 1))
        self.assertTrue(data["runner_alive"])                             # the fixture's pid is this process
        self.assertFalse(data["stale"])
        self.assertIn("age_s", data)

    def test_42_the_pointer_wins_over_the_newest_dir_and_run_selects_by_name(self):
        run2 = os.path.join(self.state, "runs", "20261031T020000Z-two")
        os.makedirs(run2)
        status.write_final(run2, dict(self.snap, run_id="20261031T020000Z-two"))
        self.assertIn("20261031T020000Z-two", self.cmd()[1])              # no pointer: the newest
        status.write_current(self.state, self.run1)
        self.assertIn("20261031T000000Z-one", self.cmd()[1])              # live pointer: that run
        self.assertIn("20261031T020000Z-two", self.cmd("--run", "20261031T020000Z-two")[1])

    def test_43_a_dead_runner_is_shown_stale(self):
        status.write_final(self.run1, dict(self.snap, pid=2 ** 22 + 777))
        rc, out, _ = self.cmd()
        self.assertEqual(rc, 0)
        self.assertIn("STALE: runner gone", out.splitlines()[0])
        self.assertTrue(json.loads(self.cmd("--json")[1])["stale"])

    def test_44_no_run_exits_1(self):
        os.unlink(os.path.join(self.run1, status.FILE))
        rc, out, err = self.cmd()
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("no council run", err)
        self.assertEqual(self.cmd("--run", "nope")[0], 1)

    def test_45_watch_redraws_until_the_run_ends(self):
        calls = []

        def fake_sleep(s):
            calls.append(s)
            status.write_final(self.run1, dict(self.snap, phase="done", ended=True, exit_code=0))
        with mock.patch.object(council.time, "sleep", fake_sleep):
            rc, out, _ = self.cmd("--watch")
        self.assertEqual((rc, calls), (0, [council.WATCH_S]))
        self.assertEqual(out.count("20261031T000000Z-one"), 2)            # drawn twice: running, then the final one
        self.assertIn("exit 0", out)

    def test_46_watch_stops_on_a_dead_runner(self):
        status.write_final(self.run1, dict(self.snap, pid=2 ** 22 + 777))
        with mock.patch.object(council.time, "sleep", side_effect=AssertionError("must not wait")):
            self.assertEqual(self.cmd("--watch")[0], 0)


# ---------------------------------------------------------------- the launcher's preflight phase
class LauncherTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.tmp = tempfile.mkdtemp(prefix="council-status-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.overlay = os.path.join(self.tmp, "ov.toml")
        with open(self.overlay, "w") as f:
            f.write('state_dir = "%s"\n' % self.state)

    def run_cmd(self, **pf):
        seen = {}

        def fake_preflight(*a, **kw):
            runs = os.path.join(self.state, "runs")
            d = os.path.join(runs, os.listdir(runs)[0])
            seen["status"] = status.load(d)
            seen["current"] = status.current(self.state)
            if "raises" in pf:
                raise pf["raises"]
            return pf["result"]
        out = io.StringIO()
        with mock.patch.object(preflight, "run", fake_preflight), \
                mock.patch.object(preflight, "format_table", lambda c, a: "TABLE\n"), \
                mock.patch.object(preflight, "to_json", lambda c, a: "{}"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = council.main(["run", "--config", self.overlay, "--", "Is the preflight visible?"])
        return rc, seen

    def test_50_the_preflight_phase_is_visible_and_a_failed_preflight_closes_it(self):
        rc, seen = self.run_cmd(result=(False, []))
        self.assertEqual(rc, 1)
        self.assertEqual(seen["status"]["phase"], "preflight")
        self.assertEqual(seen["status"]["title"], "Is the preflight visible?")
        self.assertEqual(seen["current"]["pid"], os.getpid())
        runs = os.path.join(self.state, "runs")
        fin = status.load(os.path.join(runs, os.listdir(runs)[0]))
        self.assertEqual((fin["phase"], fin["ended"], fin["exit_code"]), ("failed", True, 1))
        self.assertIsNone(status.current(self.state))

    def test_51_a_policy_failure_leaves_no_run_dir_and_no_pointer(self):
        rc, seen = self.run_cmd(raises=preflight.PolicyError(["bad"]))
        self.assertEqual(rc, 2)
        self.assertEqual(seen["status"]["phase"], "preflight")
        self.assertFalse(os.path.exists(os.path.join(self.state, "runs")))      # the run dir and its empty parent are gone
        self.assertIsNone(status.current(self.state))


if __name__ == "__main__":
    unittest.main()
