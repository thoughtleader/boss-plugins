"""Row 40 (D54): the orphan sweep, the reaper's end record, the runner-crash exit code and the operator stop (D53; D59: no launcher forwarding).

Stub CLIs and stub adapters only: no network, no real CLI, no paid call. Every case runs in its own mkdtemp dir.
The runner harness comes from tests.test_council_run (reached through the module, so unittest collects nothing twice).
"""
import contextlib
import io
import json
import os
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

import council  # noqa: E402
from councillib import council_run, netwatch, preflight, reap  # noqa: E402
from tests import test_council_run as T  # noqa: E402

PY = sys.executable or "/usr/bin/python3"
STUB = os.path.join(ROOT, "tests", "stub_cli.py")


def _dead_pid():
    p = subprocess.Popen([PY, "-Es", "-c", "pass"])
    p.wait()
    return p.pid


class ReapStatusTests(unittest.TestCase):
    """reap.py writes its end record before it sweeps or exits, on every path (the orphan sweep reads it)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.status = os.path.join(self.tmp, "end.json")

    def reaper(self, *cli):
        env = dict(os.environ, COUNCIL_REAP_DETACH="1", COUNCIL_REAP_STATUS=self.status, COUNCIL_SEAT_TAG="t-reap")
        return subprocess.Popen([PY, "-Es", reap.__file__, "1", "--", PY, STUB, *cli], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                process_group=0)

    def end(self):
        with open(self.status) as f:
            return json.load(f)

    def test_o01_exit_code_is_recorded_and_the_cli_never_sees_the_variables(self):
        p = self.reaper("exit", "3")
        p.communicate(timeout=10)
        self.assertEqual(p.returncode, 3)
        e = self.end()
        self.assertEqual((e["exit_code"], e["via"]), (3, "exit"))
        self.assertGreater(e["ended_at"], time.time() - 30)
        for name in ("COUNCIL_REAP_STATUS", "COUNCIL_REAP_DETACH"):
            p = self.reaper("env-cwd", name)
            out, _ = p.communicate(timeout=10)
            self.assertIn(b"ENV=\n", out, name + " leaked into the CLI's environment")

    def test_o02_a_term_to_the_reaper_is_recorded_as_a_signal_end(self):
        p = self.reaper("hung")
        time.sleep(0.5)
        os.kill(p.pid, signal.SIGTERM)
        p.communicate(timeout=10)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM)
        e = self.end()
        self.assertEqual((e["exit_code"], e["via"]), (128 + signal.SIGTERM, "signal"))


class SweepBase(T.HedgeBase):
    """A synthetic registry: records of watch calls whose runner is gone, and run dirs shaped like a dead runner's."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self.kill_children)
        self.children = []
        T.HedgeStub.launches = []
        self.cfg_ = self.hcfg([T.seat("a"), T.hseat("x")])

    def kill_children(self):
        for p in self.children:
            if p.poll() is None:
                p.kill()
                p.wait()

    def wdir(self):
        return council_run.watch_dir(self.state)

    def entry(self, tag, call="original", seat="x", stdout=b"ANSWER-X\n", stderr=b"", exit_code=0, runner_pid=None,
              runner_start=1, provider="hstub", synthesis=True, status_file=True, work=True, **kw):
        os.makedirs(os.path.join(self.out, "seats"), exist_ok=True)
        os.makedirs(os.path.join(self.out, "chair"), exist_ok=True)
        out_p = os.path.join(self.out, "seats", "%s.%s.%s.stdout" % (seat, call, tag[:12]))
        err_p = os.path.join(self.out, "seats", "%s.%s.%s.stderr" % (seat, call, tag[:12]))
        for path, data in ((out_p, stdout), (err_p, stderr)):
            with open(path, "wb") as f:
                f.write(data)
        end_p = os.path.join(self.wdir(), tag + ".end.json")
        os.makedirs(self.wdir(), exist_ok=True)
        if status_file:
            with open(end_p, "w") as f:
                json.dump({"exit_code": exit_code, "ended_at": time.time(), "via": "exit"}, f)
        root = tempfile.mkdtemp(prefix="council-") if work else None
        if root:
            self.addCleanup(shutil.rmtree, root, True)
        if synthesis:
            with open(os.path.join(self.out, "chair", "synthesis.md"), "w") as f:
                f.write("SYNTHESIS\n")
        if not os.path.exists(os.path.join(self.out, "summary.json")):
            with open(os.path.join(self.out, "summary.json"), "w") as f:
                json.dump({"run_id": os.path.basename(self.out), "phase": "supervising", "runner_pid": 1,
                           "late_pending": [{"seat": seat, "call": call}]}, f)
        rec = {"tag": tag, "run_id": os.path.basename(self.out), "out_dir": self.out, "seat": seat, "call": call,
               "key": seat if call == "original" else "%s~%s" % (seat, call), "kind": "seat" if call == "original" else call,
               "seat_spec": {"id": seat, "provider": provider, "model": "stub-model"},
               "runner_pid": _dead_pid() if runner_pid is None else runner_pid, "runner_start": runner_start,
               "started_at": "2026-10-21T00:00:00Z", "started_epoch": time.time() - 100,
               "run_t0_epoch": time.time() - 100, "stdout": out_p, "stderr": err_p, "status_file": end_p,
               "final_text_path": None, "work_root": root, "workdir": root}
        rec.update(kw)
        with open(os.path.join(self.wdir(), tag + ".json"), "w") as f:
            json.dump(rec, f)
        return rec

    def registry(self):
        try:
            return sorted(n for n in os.listdir(self.wdir()) if n.endswith(".json") and not n.endswith(".end.json"))
        except OSError:
            return []

    def sweep(self):
        return preflight.sweep_orphans(self.cfg_)


class SweepTests(SweepBase):
    def test_o10_finished_orphan_answer_is_appended_once_and_reaped(self):
        rec = self.entry("a" * 32)
        res = self.sweep()
        self.assertEqual([(o["seat"], o["call"], o["status"], o["answered"], o["appended"]) for o in res["swept"]],
                         [("x", "original", "ok", True, True)])
        self.assertEqual(res["live"], [])
        self.assertIn("ANSWER-X", self.read("seats", "x.original.late.md"))
        self.assertIn("ANSWER-X", self.read("seats", "x.original.late.raw"))
        self.assertIn("## original (late", self.read("seats", "x.late.md"))
        syn = self.read("chair", "synthesis.md")
        self.assertEqual(syn.count("> LATE: seat x (original) answered at"), 1)
        self.assertIn("not used by the chair", syn)
        summ = self.summary()
        self.assertEqual([(e["seat"], e["call"], e["by"]) for e in summ["late"]], [("x", "original", "orphan_sweep")])
        self.assertEqual((summ["phase"], summ["runner_lost"]), ("done", True), "the last orphan settles the run")
        self.assertNotIn("late_pending", summ)
        self.assertEqual([(o["seat"], o["status"], o["answered"]) for o in summ["orphans"]], [("x", "ok", True)])
        calls = [json.loads(ln) for ln in self.read("calls.jsonl").splitlines()]
        self.assertEqual([(c["call"], c["status"], c["by"]) for c in calls], [("original", "ok", "orphan_sweep")])
        self.assertEqual(self.registry(), [])
        for gone in (rec["stderr"], rec["status_file"], rec["stdout"], rec["work_root"]):
            self.assertFalse(os.path.exists(gone), gone)
        self.assertEqual(T.HedgeStub.launches, [], "the sweep launches nothing")
        self.assertEqual(self.sweep()["swept"], [], "a second sweep finds nothing")

    def test_o11_a_sweep_that_died_half_way_finishes_without_a_duplicate(self):
        self.entry("b" * 32)
        self.sweep()
        self.entry("b" * 32, synthesis=False)     # the same record again: the earlier sweep "died" before its unlink
        res = self.sweep()
        self.assertEqual([(o["answered"], o["appended"]) for o in res["swept"]], [(True, False)])
        syn = self.read("chair", "synthesis.md")
        self.assertEqual(syn.count("> LATE: seat x (original)"), 1)
        self.assertEqual(self.read("seats", "x.late.md").count("## original (late"), 1)
        self.assertEqual(len(self.summary()["late"]), 1)
        self.assertEqual(len(self.summary()["orphans"]), 1)

    def test_o111_runner_lost_before_the_first_answer_settles_late_synthesis(self):
        self.entry("7" * 32, synthesis=False)
        with open(os.path.join(self.out, "summary.json")) as f:
            summ = json.load(f)
        summ["late_synthesis"] = {"status": "waiting", "reason": "pending: no answers yet"}
        with open(os.path.join(self.out, "summary.json"), "w") as f:
            json.dump(summ, f)
        self.sweep()
        summ = self.summary()
        self.assertEqual((summ["phase"], summ["runner_lost"]), ("done", True))
        ls = summ["late_synthesis"]
        self.assertEqual((ls["status"], ls["late"]), ("runner_lost", ["seats/x.original.late.md"]))
        self.assertFalse(os.path.exists(os.path.join(self.out, "chair", "synthesis.md")))

    def test_o112_a_second_orphan_of_the_run_keeps_it_unsettled_until_it_is_swept(self):
        self.entry("7" * 32)
        child = subprocess.Popen([PY, "-Es", "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                 env=dict(os.environ, COUNCIL_SEAT_TAG="8" * 32))
        self.children.append(child)
        self.entry("8" * 32, call="hedge", synthesis=False)
        self.sweep()
        self.assertEqual(self.summary()["phase"], "runner_lost")
        self.assertNotIn("runner_lost", self.summary())
        child.kill()
        child.wait()
        self.sweep()
        self.assertEqual(self.summary()["phase"], "done")

    def test_o12_no_answer_is_recorded_unresolved_and_nothing_is_relaunched(self):
        rec = self.entry("c" * 32, stdout=b"partial", exit_code=3, call="hedge")
        res = self.sweep()
        o = res["swept"][0]
        self.assertEqual((o["status"], o["answered"], o["exit_code"]), ("exit_3", False, 3))
        self.assertFalse(os.path.exists(os.path.join(self.out, "seats", "x.hedge.late.md")))
        self.assertNotIn("LATE", self.read("chair", "synthesis.md"))
        self.assertNotIn("late", self.summary())
        self.assertEqual(self.summary()["orphans"][0]["status"], "exit_3")
        self.assertTrue(os.path.exists(rec["stdout"]), "the raw of an unanswered call is kept")
        self.assertEqual(T.HedgeStub.launches, [])
        self.assertEqual(self.registry(), [])
        line = preflight.orphan_lines(res)[0]
        self.assertIn("no answer, recorded unresolved (nothing relaunched)", line)

    def test_o13_a_cli_that_exited_0_after_a_quota_or_timeout_line_is_not_an_answer(self):
        self.entry("d" * 32, provider="qstub", stderr=b"quota_429\n")
        o = self.sweep()["swept"][0]
        self.assertEqual((o["status"], o["answered"]), ("quota_exhausted", False))

    def test_o14_no_reaper_record_is_unknown_end_never_an_answer(self):
        self.entry("e" * 32, status_file=False)
        o = self.sweep()["swept"][0]
        self.assertEqual((o["status"], o["answered"]), ("unknown_end", False))

    def test_o15_a_record_whose_runner_is_alive_is_left_alone(self):
        start = council_run.proc_start(os.getpid())
        rec = self.entry("f" * 32, runner_pid=os.getpid(), runner_start=start)
        res = self.sweep()
        self.assertEqual((res["runner_alive"], res["live"], res["swept"]), (1, [], []))
        self.assertEqual(self.registry(), [rec["tag"] + ".json"])
        self.assertTrue(os.path.exists(rec["stdout"]))
        self.assertFalse(os.path.exists(os.path.join(self.out, "seats", "x.original.late.md")))
        # a reused pid (same number, other start time) is not the runner
        self.entry("f" * 32, runner_pid=os.getpid(), runner_start=start + 1)
        self.assertEqual(len(self.sweep()["swept"]), 1)

    def test_o16_a_live_orphan_is_reported_with_age_seat_call_and_run_and_never_signalled(self):
        tag = "9" * 32
        child = subprocess.Popen([PY, "-Es", "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                 env=dict(os.environ, COUNCIL_SEAT_TAG=tag))
        self.children.append(child)
        rec = self.entry(tag)
        for _ in range(2):
            res = self.sweep()
            self.assertEqual(res["swept"], [])
            self.assertEqual(len(res["live"]), 1)
            live = res["live"][0]
            self.assertEqual((live["seat"], live["call"], live["run_id"], live["pids"]),
                             ("x", "original", os.path.basename(self.out), [child.pid]))
            self.assertGreaterEqual(live["age_s"], 99)
            self.assertIsNone(child.poll(), "the sweep killed a live call")
        self.assertEqual(self.registry(), [rec["tag"] + ".json"], "a live call keeps its record")
        self.assertTrue(os.path.exists(rec["stdout"]) and os.path.isdir(rec["work_root"]))
        self.assertFalse(os.path.exists(os.path.join(self.out, "seats", "x.original.late.md")))
        rows = preflight.orphan_rows(self.cfg_)
        self.assertEqual([(r.provider, r.stage, r.check, r.result) for r in rows],
                         [("orphans", "free", "orphan:x.original", "WARN")])
        d = rows[0].detail
        for want in ("LIVE", "seat x", "call original", "run " + os.path.basename(self.out), "age 1m", str(child.pid),
                     "never killed"):
            self.assertIn(want, d)
        child.kill()
        child.wait()
        self.assertEqual(len(self.sweep()["swept"]), 1, "once it has ended it is swept")

    def test_o17_the_work_dir_stays_while_another_record_uses_it(self):
        a = self.entry("1" * 32, call="original")
        child = subprocess.Popen([PY, "-Es", "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                 env=dict(os.environ, COUNCIL_SEAT_TAG="2" * 32))
        self.children.append(child)
        self.entry("2" * 32, call="hedge", work_root=a["work_root"], workdir=a["work_root"])
        res = self.sweep()
        self.assertEqual(([o["call"] for o in res["swept"]], [o["call"] for o in res["live"]]),
                         (["original"], ["hedge"]))
        self.assertTrue(os.path.isdir(a["work_root"]), "the live call still needs the workdir")
        child.kill()
        child.wait()
        self.sweep()
        self.assertFalse(os.path.exists(a["work_root"]))

    def test_o18_an_unrelated_dir_is_never_removed(self):
        keep = tempfile.mkdtemp(prefix="keepme-")
        self.addCleanup(shutil.rmtree, keep, True)
        self.entry("3" * 32, work_root=keep, workdir=keep)
        self.sweep()
        self.assertTrue(os.path.isdir(keep))

    def test_o19_cli_reap_orphans_and_the_preflight_row(self):
        self.entry("4" * 32)
        ov = os.path.join(self.tmp, "ov.toml")
        with open(ov, "w") as f:
            f.write('prompts_dir = "%s"\nstate_dir = "%s"\n[chair]\nprovider = "stub"\nmodel = "m"\n'
                    'stub_mode = "quick"\n[providers.stub]\nmode = "stream"\n[quorum]\nmin_ok = "n-1"\n'
                    '[[seat]]\nid = "a"\nprovider = "stub"\nmodel = "m-a"\nstub_mode = "quick"\n'
                    % (os.path.join(ROOT, "prompts"), self.state))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = council.main(["reap", "--orphans", "--config", ov])
        self.assertEqual(rc, 0)
        self.assertIn("orphans: ENDED seat x call original run %s status ok: answer appended as late"
                      % os.path.basename(self.out), out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(council.main(["reap", "--orphans", "--config", ov]), 0)
        self.assertIn("orphans: none", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(council.main(["reap", "--config", ov]), 2)      # --orphans is required
        # preflight.run puts the sweep's rows in its table, and an orphan row never fails it
        self.entry("5" * 32, call="hedge", exit_code=3)
        from councillib import config
        ok, checks = preflight.run(config.load(overlays=[ov]), free_only=True)
        orph = [c for c in checks if c.provider == "orphans"]
        self.assertEqual([(c.check, c.result) for c in orph], [("orphan:x.hedge", "WARN")])
        self.assertTrue(ok)
        self.assertIn("orphan:x.hedge", preflight.format_table(checks))


class RunnerRegistryTests(T.HedgeBase):
    """The runner side: a record while a watch call runs, none once the runner has seen it end."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self.kill_stubs)

    kill_stubs = T.RunnerTests.kill_stubs
    stub_pids = T.RunnerTests.stub_pids
    pid_of = T.RunnerTests.pid_of

    def registry(self):
        d = council_run.watch_dir(self.state)
        try:
            return sorted(n for n in os.listdir(d))
        except OSError:
            return []

    def test_o20_clean_run_leaves_no_record_and_no_private_capture(self):
        seen = []

        def on_ready(code, info):
            seen.append(self.registry())
        cfg = self.hcfg([T.seat("a"), T.seat("b"), T.hseat("x", "busy-silent 1.8", hedge_mode="quick")])
        self.assertEqual(council_run.run(cfg, "Q?", [], self.out, on_ready=on_ready), 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.registry(), [])
        self.assertEqual([n for n in os.listdir(os.path.join(self.out, "seats")) if n.endswith((".stdout", ".stderr"))],
                         [], "private captures are removed once the call ended")

    def test_o21_record_exists_while_the_call_runs(self):
        seen = []

        def on_ready(code, info):
            recs = []
            for n in self.registry():
                if n.endswith(".json") and not n.endswith(".end.json"):
                    with open(os.path.join(council_run.watch_dir(self.state), n)) as f:
                        recs.append(json.load(f))
            seen.append(recs)
        cfg = self.hcfg([T.seat("a"), T.seat("b"), T.hseat("x", "busy-silent 1.8", hedge_mode="quick")])
        council_run.run(cfg, "Q?", [], self.out, on_ready=on_ready)
        (recs,) = seen
        self.assertEqual([(r["seat"], r["call"], r["runner_pid"]) for r in recs], [("x", "original", os.getpid())])
        r = recs[0]
        self.assertEqual(r["out_dir"], self.out)
        self.assertEqual(r["runner_start"], council_run.proc_start(os.getpid()))
        self.assertEqual(r["seat_spec"]["provider"], "hstub")
        self.assertTrue(r["stdout"].startswith(os.path.join(self.out, "seats", "x.original.")))
        self.assertEqual(r["status_file"], os.path.join(council_run.watch_dir(self.state), r["tag"] + ".end.json"))
        self.assertEqual(self.registry(), [])


class ProcessTests(SweepBase):
    """Real runners, real detached reapers (stub CLIs). A runner killed by kill -9 leaves orphans; TERM stops them."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self.kill_stubs)

    kill_stubs = T.RunnerTests.kill_stubs
    stub_pids = T.RunnerTests.stub_pids
    pid_of = T.RunnerTests.pid_of
    harness = T.RunnerTests.harness
    launch = T.RunnerTests.launch
    wait_runner_done = T.RunnerTests.wait_runner_done
    read_log = T.RunnerTests.read_log

    def test_o30_kill9_runner_live_orphan_is_warned_not_killed_then_its_answer_is_appended_late(self):
        # a, b answer; x's original and hedge stay silent past the pending point, so the chair runs and the launcher
        # returns; then the runner is killed with SIGKILL while x is still running
        seats = [T.seat("a"), T.seat("b"), T.hseat("x", "busy-silent 2.2", hedge_mode="busy-silent 3.2")]
        cfg = self.hcfg(seats, ceiling=30, at=0.5)
        rc, out, wall = self.launch(cfg)
        self.assertEqual(rc, 0, out + self.read_log())
        pid = self.summary()["runner_pid"]
        self.assertTrue(os.path.exists(os.path.join(self.out, "chair", "synthesis.md")))
        os.kill(pid, signal.SIGKILL)
        self.assertTrue(T._wait(lambda: T._gone(pid), 3))
        x = self.pid_of("x")
        tags = {}
        for n in self.registry():
            with open(os.path.join(self.wdir(), n)) as f:
                r = json.load(f)
            tags[r["call"]] = r["tag"]
        self.assertEqual(sorted(tags), ["hedge", "original"])
        self.assertFalse(T._gone(x), "the watch original survives its runner (D18)")
        for _ in range(2):
            res = preflight.sweep_orphans(self.cfg_)
            self.assertEqual([(o["seat"], o["call"]) for o in res["live"]],
                             [("x", "original"), ("x", "hedge")])
            self.assertEqual(res["swept"], [])
            self.assertFalse(T._gone(x), "the sweep never kills a live call")
        self.assertEqual(len(self.registry()), 2)
        rows = preflight.orphan_rows(self.cfg_)
        self.assertEqual([r.check for r in rows], ["orphan:x.original", "orphan:x.hedge"])
        # the original ends on its own (about 2.2 s): its answer is appended, the hedge is still live
        self.assertTrue(T._wait(lambda: not netwatch.tagged_pids(tags["original"]), 6))
        res = preflight.sweep_orphans(self.cfg_)
        self.assertEqual([(o["seat"], o["call"], o["answered"], o["appended"]) for o in res["swept"]],
                         [("x", "original", True, True)])
        self.assertEqual([o["call"] for o in res["live"]], ["hedge"])
        self.assertIn("DONE", self.read("seats", "x.original.late.md"))
        self.assertIn("> LATE: seat x (original) answered at", self.read("chair", "synthesis.md"))
        self.assertTrue(T._wait(lambda: not netwatch.tagged_pids(tags["hedge"]), 8))
        res = preflight.sweep_orphans(self.cfg_)
        self.assertEqual([(o["call"], o["answered"]) for o in res["swept"]], [("hedge", True)])
        syn = self.read("chair", "synthesis.md")
        self.assertEqual(syn.count("> LATE: seat x (original)"), 1)
        self.assertEqual(syn.count("> LATE: seat x (hedge)"), 1)
        summ = self.summary()
        self.assertEqual(sorted(e["call"] for e in summ["late"]), ["hedge", "original"])
        self.assertEqual((summ["phase"], summ["runner_lost"]), ("done", True))
        self.assertTrue(os.path.exists(os.path.join(self.out, "seats", "x.hedge.raw")),
                        "an answered hedge keeps its canonical raw")
        self.assertEqual(os.listdir(self.wdir()), [])
        self.assertEqual(self.registry(), [])
        self.assertEqual(preflight.sweep_orphans(self.cfg_)["swept"], [])
        self.assert_no_survivors()

    def test_o31_term_to_the_launcher_is_not_forwarded_kill_term_runner_pid_stops_every_call(self):
        # D59 (row 47, supersedes D18): a TERM to the launcher (a closing chat or terminal) is not an operator stop;
        # the calls keep running. Only kill -TERM <runner_pid> (D53) stops them, each with its real end.
        seats = [T.seat("a", "partial-then-hang", provider="sstub"), T.seat("b"),
                 T.hseat("x", "hung", hedge_mode="hung")]
        cfg = self.hcfg(seats, ceiling=30, at=0.4)
        launcher = os.path.join(self.tmp, "launcher.py")
        with open(launcher, "w") as f:
            f.write(T.LAUNCHER % {"root": ROOT})
        proc = subprocess.Popen([PY, "-Es", launcher, *self.harness(cfg)[3:]], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dict(os.environ), cwd=self.tmp)
        try:
            self.assertTrue(T._wait(lambda: self.pid_of("x_hedge") and self.pid_of("a"), 10),
                            "the hedge never started: %s" % self.stub_pids())
            time.sleep(0.2)
            runner = T._ppid(T._ppid(self.pid_of("x_hedge")))
            os.kill(proc.pid, signal.SIGTERM)
            out, _ = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        out = out.decode()
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, out)
        self.assertIn("runner %d keeps running with its calls (D59)" % runner, out)
        time.sleep(0.5)
        for role in ("a", "x", "x_hedge"):                # b ("main") answered on its own
            self.assertFalse(T._gone(self.pid_of(role)), "%s died with the launcher's TERM" % role)
        self.assertFalse(T._gone(runner))
        self.assertEqual(len(self.registry()), 2, "the watch calls are still registered to a live runner")
        self.assertEqual(preflight.sweep_orphans(self.cfg_)["live"], [], "a live runner's calls are not orphans")
        os.kill(runner, signal.SIGTERM)                  # D53: the owner's explicit stop
        self.assertTrue(T._wait(lambda: all(T._gone(p) for _, p in self.stub_pids()), 8),
                        "a call outlived kill -TERM <runner_pid>: %s" % self.stub_pids())
        self.assertTrue(T._wait(lambda: T._gone(runner), 5))
        roles = {r for r, _ in self.stub_pids()}
        self.assertTrue({"x", "x_hedge", "a"} <= roles, roles)
        summ = self.summary()
        self.assertEqual((summ["exit_code"], summ.get("interrupted")), (128 + signal.SIGTERM, True))
        self.assertEqual(self.by_id(summ)["a"]["status"], "signal_kill")
        x = self.read("seats", "x.json")
        self.assertEqual({c["call"]: c["status"] for c in json.loads(x)["calls"]},
                         {"original": "signal_kill", "hedge": "signal_kill"})
        self.assertEqual(self.registry(), [], "an explicit stop leaves no orphan record")
        self.assertEqual(preflight.sweep_orphans(self.cfg_)["swept"], [])
        self.assertEqual(os.listdir(self.wdir()), [], "the killed reapers' end records are swept as garbage")
        self.assert_no_survivors()

    def test_o32_kill_term_runner_pid_after_ready_stops_every_call_watch_calls_included(self):
        seats = [T.seat("a"), T.seat("b"), T.hseat("x", "hung", hedge_mode="hung")]
        cfg = self.hcfg(seats, ceiling=60, at=0.4)
        rc, out, wall = self.launch(cfg)
        self.assertEqual(rc, 0, out + self.read_log())
        summ = self.summary()
        self.assertEqual(summ["pending"], ["x"])
        pid = summ["runner_pid"]
        alive = T.roles_alive(self.pidfile)
        self.assertTrue(alive.get("x") and alive.get("x_hedge"), alive)
        self.assertEqual(len(self.registry()), 2)
        os.kill(pid, signal.SIGTERM)                 # D53: the owner's explicit stop
        self.assertTrue(T._wait(lambda: all(T._gone(p) for _, p in self.stub_pids()), 8),
                        "a call survived kill -TERM <runner_pid>: %s" % self.stub_pids())
        self.assertTrue(T._wait(lambda: T._gone(pid), 5))
        summ = self.summary()
        self.assertEqual((summ["exit_code"], summ.get("interrupted")), (128 + signal.SIGTERM, True))
        self.assertEqual(self.registry(), [], "the stopped calls are not orphans")
        self.assertEqual(preflight.sweep_orphans(self.cfg_), {"live": [], "swept": [], "errors": [], "runner_alive": 0})
        self.assertEqual(os.listdir(self.wdir()), [], "the killed reapers' end records are swept as garbage")
        self.assert_no_survivors()

    def registry(self):
        try:
            return sorted(n for n in os.listdir(self.wdir()) if n.endswith(".json") and not n.endswith(".end.json"))
        except OSError:
            return []


CRASH = """import sys
sys.path.insert(0, %(root)r)
import council
def target(fd):
    %(body)s
sys.exit(council.launch(target, sys.argv[1]))
"""


class CrashCodeTests(unittest.TestCase):
    """A runner crash is never exit 1 (a failed preflight)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_launcher(self, body):
        script = os.path.join(self.tmp, "crash.py")
        with open(script, "w") as f:
            f.write(CRASH % {"root": ROOT, "body": body})
        p = subprocess.run([PY, "-Es", script, self.tmp], stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        return p.returncode, p.stdout.decode()

    def test_o40_a_runner_exception_exits_5_with_the_log_named(self):
        rc, out = self.run_launcher('raise RuntimeError("boom")')
        self.assertEqual(rc, council.EXIT_RUNNER_CRASH)
        self.assertEqual(council.EXIT_RUNNER_CRASH, 5)
        self.assertIn("runner exited with 5 before the synthesis (log: %s)" % os.path.join(self.tmp, "runner.log"), out)
        self.assertIn("exit: 5", out)
        with open(os.path.join(self.tmp, "runner.log")) as f:
            self.assertIn("RuntimeError: boom", f.read())

    def test_o41_a_runner_that_exits_1_is_still_not_read_as_a_preflight_failure(self):
        for body in ("return 1", "import os; os._exit(1)", "return None"):
            rc, out = self.run_launcher(body)
            self.assertEqual(rc, 5, (body, out))

    def test_o42_other_codes_pass_through_and_a_signal_death_is_128_plus_n(self):
        self.assertEqual(self.run_launcher("return 3")[0], 3)
        rc, out = self.run_launcher("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
        self.assertEqual(rc, 128 + signal.SIGKILL)
        self.assertIn("runner exited with 137", out)


if __name__ == "__main__":
    unittest.main()
