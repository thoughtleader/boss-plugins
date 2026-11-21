"""Row 32: fake-event replay harness. Four scenarios from the council synthesis, each with the progress signal
adopted (`otel`, a per-call OTLP sink) and again with `none` (hedge-only), through council_run.run / runner_main.

  S1 long silence, then an answer   S2 endless keepalives   S3 explicit failure   S4 observer loss

What is real: council_run (scheduler, calls.jsonl, hedge, fallback, pending), the Supervisor in watch mode, the
real SeatLiveness (policy from the gate lines in 25-gate.md) and the real otlp_sink.Sink. What is fake: the CLI
(tests/stub_cli.py `timeline`, replaying tests/fixtures/replay.jsonl, event shapes from the row 21/25 codex
probes with ids and account fields left out) and two liveness sources (`pids`, `net`), so no `ss` runs.
Time is scaled: the soft notice is 1.5 s, quiet_s 0.5 s, a keepalive 0.25 s (30 s in the probes).

Rules every case asserts (owner, D12/D15/D24/D39/D44): nothing is killed on elapsed time (no ceiling, deadline,
idle or cut status); the hedge starts at the soft notice and only for a hedge_safe provider; keepalives are
never progress and never a stop; a stop happens only on a proven terminal state (a gate line) or the operator's
TERM. Stubs only: no network beyond 127.0.0.1, no real CLI settings, state in a temp dir.
"""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import council_run, liveness, netwatch, preflight, providers  # noqa: E402
from councillib.supervise import Launch  # noqa: E402
from tests.test_council_run import (  # noqa: E402
    ChairProbe, HedgeBase, HedgeStub, PY, StreamHedgeStub, QuotaStub, _gone, _wait, hseat, roles_alive, seat)

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "replay.jsonl")
IDLE_SSE = "stream disconnected before completion: idle timeout waiting for SSE"     # 25-gate.md terminal_lines
SOFT = 1.5                        # hedge_at (scaled)
# statuses that mean "ended by elapsed time or by the scheduler cutting it"; none may ever show up here
TIME_KILLS = {"ceiling_kill", "deadline_kill", "idle_kill", "cut_after_quorum", "cut", "hung_kill"}


class ReplayStub(HedgeStub):
    """A buffered, hedge_safe stand-in for codex: each call replays its own timeline of the seat's `replay` case."""
    name = "rstub"

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        wd = os.path.basename(workdir)
        call = "hedge" if wd.endswith("_hedge") else "fallback" if wd.endswith("_fallback") else "original"
        HedgeStub.launches.append({"wd": wd, "opts": launch_opts, "limits": limits,
                                   "salvage": bool(seat.get("_salvage"))})
        env = providers._tag_env(dict(os.environ), launch_opts)
        env["STUB_ROLE"] = wd
        if (launch_opts or {}).get("otlp_port"):
            env["STUB_OTLP_PORT"] = str(launch_opts["otlp_port"])
        return Launch(argv=[sys.executable, "-Es", providers.STUB_CLI, "timeline", FIXTURE, seat["replay"], call],
                      env=env, cwd=workdir, stdin_path=prompt_path)


def replay_liveness(provider, tag, policy, sources, log_path):
    """council_run.make_liveness for the harness: the real SeatLiveness over the call's real OTLP sink, with the
    process lookups faked. `net` reports an open ESTAB socket (as in every probe run) unless REPLAY_ESTAB=0."""
    src = dict(sources)
    src["pids"] = lambda info: [info.pid]

    def net(pids):
        tcp = [{"pid": p, "state": "ESTAB", "peer": "203.0.113.7:443"} for p in pids] \
            if os.environ.get("REPLAY_ESTAB", "1") == "1" else []
        return netwatch.NetSample(t=time.time(), states={p: "S" for p in pids}, tcp=tcp, ss_ok=True)
    src["net"] = net
    return liveness.SeatLiveness(provider, tag, policy, src, log_path=log_path)


def install():
    """Register the adapters and the liveness seam in a runner subprocess (the in-process cases use setUp)."""
    for cls in (ReplayStub, HedgeStub, StreamHedgeStub, QuotaStub, ChairProbe):
        providers.register(cls())
    council_run.make_liveness = replay_liveness


providers.FAMILIES.setdefault("rstub", "stub")

RUNNER = """import json, sys
sys.path.insert(0, %(root)r)
import council
from tests import test_replay as R
R.install()
cfg = json.load(open(sys.argv[1]))
sys.exit(council.runner_main(cfg, "Q?", [], sys.argv[2], [], (), None))
"""


class ReplayBase(HedgeBase):
    signal_on = True

    def setUp(self):
        super().setUp()
        providers.register(ReplayStub())
        self.addCleanup(providers._REGISTRY.pop, ReplayStub.name, None)
        p = mock.patch.object(council_run, "make_liveness", replay_liveness)
        p.start()
        self.addCleanup(p.stop)
        self.procs = []
        self.addCleanup(self.cleanup_procs)

    def cleanup_procs(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        for _role, pid in self.stub_pids():            # detached stubs a failed assertion could leave
            if not _gone(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def stub_pids(self):
        try:
            with open(self.pidfile) as f:
                return [(ln.split()[0], int(ln.split()[1])) for ln in f if ln.strip()]
        except OSError:
            return []

    def pid_of(self, role):
        return next((p for r, p in self.stub_pids() if r == role), None)

    # ---- config -------------------------------------------------------------------------------------------
    def rcfg(self, seats, ceiling=600, **kw):
        """Hedge and fallback on; only rstub is hedge_safe; the codex gate lines for rstub; a big ceiling so
        that only a scripted end, a gate line or the operator's TERM can end a call."""
        on = self.signal_on
        cfg = self.hcfg(seats, hedge=True, fallback=True, at=SOFT, ceiling=ceiling, **kw)
        cfg["hedge"].update({"providers": ["rstub"], "quiet_s": 0.5, "recheck_s": 0.1})
        cfg["providers"]["rstub"] = {
            "mode": "buffered",
            "progress_signal": "otel" if on else "none",
            "progress_event": "codex.sse_event", "progress_exclude": ["event.kind=keepalive"],
            "negative_evidence": True, "terminal_lines": [IDLE_SSE]}
        cfg["liveness"] = {"signals": on, "sample_s": 0.1, "grace_s": 0.3, "no_conn_samples": 5}
        return cfg

    def rseat(self, sid, case):
        return seat(sid, "quick", provider="rstub", replay=case)

    # ---- readers / assertions ------------------------------------------------------------------------------
    def starts(self, call):
        return [c for c in self.calls() if c["call"] == call and "status" not in c]

    def by_call(self, sid):
        return {c["call"]: c for c in self.seat_json(sid)["calls"]}

    def assert_not_killed_by_time(self, sid):
        for name, c in self.by_call(sid).items():
            self.assertNotIn(c["status"], TIME_KILLS, "%s/%s was cut: %s" % (sid, name, c))
            self.assertNotEqual(c["status"], "signal_kill", "%s/%s was killed before any TERM" % (sid, name))

    def assert_progress(self, entry, keepalives=None, progressed=None):
        """The seat json `progress` block tells the two signal modes apart."""
        pr = entry.get("progress")
        self.assertIsNotNone(pr, entry)
        if not self.signal_on:
            self.assertEqual(pr["signal"], "none", pr)
            self.assertIsNone(pr["last_progress_s"], pr)
            return pr
        self.assertEqual(pr["signal"], "otel", pr)
        if progressed is not None:
            self.assertEqual(pr["last_progress_s"] is not None, progressed, pr)
        if keepalives is not None:
            self.assertEqual((pr.get("keepalives") or 0) > 0, keepalives, pr)
        return pr

    def launch_runner(self, cfg):
        cfg_path = os.path.join(self.tmp, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        script = os.path.join(self.tmp, "runner_harness.py")
        with open(script, "w") as f:
            f.write(RUNNER % {"root": ROOT})
        os.makedirs(self.out, mode=0o700, exist_ok=True)
        env = dict(os.environ, REPLAY_ESTAB=os.environ.get("REPLAY_ESTAB", "1"))
        log = open(os.path.join(self.tmp, "runner.out"), "wb")
        self.addCleanup(log.close)
        proc = subprocess.Popen([PY, "-Es", script, cfg_path, self.out], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, env=env, cwd=self.tmp)
        self.procs.append(proc)
        return proc

    def runner_log(self):
        try:
            with open(os.path.join(self.tmp, "runner.out")) as f:
                return f.read()[-1500:]
        except OSError:
            return ""

    # ---- S1 -----------------------------------------------------------------------------------------------
    def scenario_1_long_silence(self):
        # x: early progress, then silence past the soft notice, then the answer. z is not hedge_safe.
        seats = [seat("a"), self.rseat("x", "s1"), hseat("z", "busy-silent 2.2")]
        code = self.run_council(self.rcfg(seats))
        self.assertEqual(code, 0)
        hedges = self.starts("hedge")
        self.assertEqual([h["seat"] for h in hedges], ["x"], "one hedge, for the hedge_safe seat only")
        self.assertGreaterEqual(hedges[0]["after_s"], SOFT - 0.05)
        self.assertLess(hedges[0]["after_s"], SOFT + 0.4, "the hedge starts at the soft notice, not later")
        self.assertEqual(self.seat_json("x").get("hedge_deferred", 0), 0, "silence defers nothing")
        self.assertEqual(self.starts("fallback"), [])
        self.assertNotIn("z_hedge", self.wds())
        calls = self.by_call("x")
        self.assertEqual((calls["original"]["status"], calls["original"]["exit_code"],
                          calls["original"]["truncated"]), ("ok", 0, False), "the silent original was never killed")
        self.assertIn(calls["hedge"]["role"], ("alternate", "late"))
        self.assertEqual(calls["hedge"]["status"], "ok")
        self.assertEqual(self.seat_json("x")["answered_by"], "original")
        self.assertEqual(calls["original"]["role"], "used")
        self.assertIn("S1-ORIGINAL", self.read("seats", "x.md"))
        self.assert_not_killed_by_time("x")
        self.assertEqual(self.seat_json("z")["status"], "ok")
        pr = self.assert_progress(calls["original"], keepalives=False, progressed=True)
        if self.signal_on:
            self.assertGreaterEqual(pr["events"], 3)
            self.assertLess(pr["last_progress_s"], 1.0, "the last progress was before the silence")
        self.assert_no_survivors()

    # ---- S2 -----------------------------------------------------------------------------------------------
    def scenario_2_endless_keepalives(self):
        if self.signal_on:
            # adversarial: the socket evidence says "no connection" all the time, so only the keepalive events
            # (activity) stand between the call and a no_connection stop
            os.environ["REPLAY_ESTAB"] = "0"
            self.addCleanup(os.environ.pop, "REPLAY_ESTAB", None)
        seats = [seat("a"), seat("b"), self.rseat("x", "s2")]
        cfg = self.rcfg(seats)
        proc = self.launch_runner(cfg)
        syn = os.path.join(self.out, "chair", "synthesis.md")
        self.assertTrue(_wait(lambda: os.path.exists(syn), 20), "no synthesis: " + self.runner_log())
        self.assertTrue(_wait(lambda: self.summary().get("phase") == "supervising", 5), self.runner_log())
        summ = self.summary()
        self.assertEqual(summ["pending"], ["x"])
        self.assertEqual(summ["runner_pid"], proc.pid)
        starts = self.starts("hedge")
        self.assertEqual(len(starts), 1)
        self.assertGreaterEqual(starts[0]["after_s"], SOFT - 0.05)
        self.assertLess(starts[0]["after_s"], SOFT + 0.4, "keepalives are not progress: no D12 deferral")
        alive = roles_alive(self.pidfile)
        self.assertTrue(alive.get("x") and alive.get("x_hedge"), alive)
        self.assertIn("status pending", self.read("chair", "synthesis.md"))
        # synthesis is out; nothing is killed and no fallback runs while both calls are working
        time.sleep(1.6)
        alive = roles_alive(self.pidfile)
        self.assertTrue(alive.get("x") and alive.get("x_hedge"), alive)
        self.assertIsNone(proc.poll(), "the runner keeps supervising")
        self.assertEqual(self.starts("fallback"), [])
        self.assertNotIn("x_fallback", alive)
        self.assertEqual([c["status"] for c in self.calls() if "status" in c], [], "no call ended")
        # teardown: the operator's TERM (D53) is the only stop
        os.kill(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=15), 128 + signal.SIGTERM)
        summ = self.summary()
        self.assertTrue(summ.get("interrupted"), summ)
        x = self.seat_json("x")
        self.assertEqual(x["status"], "signal_kill")
        # row 41: every call whose process the stop ended carries its real end in the seat json and calls.jsonl
        self.assertEqual({c["call"]: c["status"] for c in x["calls"]},
                         {"original": "signal_kill", "hedge": "signal_kill"})
        for c in x["calls"]:
            self.assertIs(c["running"], False, "%s still reads running after the stop: %s" % (c["call"], c))
            self.assertEqual(c["signal"], signal.SIGTERM, c)
            self.assertIsNotNone(c["total_s"], c)
            self.assertIs(c["truncated"], True, c)
        ends = [c for c in self.calls() if "status" in c]
        self.assertEqual([(c["seat"], c["call"], c["status"], c["signal"]) for c in ends],
                         [("x", "hedge", "signal_kill", signal.SIGTERM)], "the hedge's end is in calls.jsonl")
        self.assertIn("exit_code", ends[0])
        self.assertEqual(summ.get("late_pending", []), [], "no call is listed as still running")
        # the orphan sweep (row 40) sees no live call and nothing to settle for this run
        self.assertEqual(preflight.sweep_orphans(cfg), {"live": [], "swept": [], "errors": [], "runner_alive": 0})
        self.assertTrue(os.path.exists(os.path.join(self.out, "seats", "x.hedge.raw")), "the capture is kept")
        self.assertIn("thread.started", self.read("seats", "x.hedge.raw"))
        self.assertTrue(os.path.exists(syn), "the synthesis is kept")
        kept = [n for n in os.listdir(os.path.join(self.out, "seats")) if n.startswith("x.original.")
                and n.endswith(".stdout")]
        self.assertEqual(len(kept), 1, "the original's partial output is kept")
        self.assertIn("thread.started", self.read("seats", kept[0]))
        for c in x["calls"]:
            self.assert_progress(c, keepalives=True, progressed=False)
        self.assertTrue(_wait(lambda: all(_gone(p) for _, p in self.stub_pids()), 5))

    # ---- S3 -----------------------------------------------------------------------------------------------
    def scenario_3_explicit_failure(self):
        # x: original = gate line on stdout then hang; hedge = gate line on stderr then hang; the fallback answers.
        # y: an unlisted idle line (websocket form, D30) and silence; it must not be reaped, and it answers late.
        seats = [seat("a"), self.rseat("x", "s3x"), self.rseat("y", "s3y")]
        code = self.run_council(self.rcfg(seats))
        self.assertEqual(code, 0)
        x = self.by_call("x")
        self.assertEqual({k: x[k]["status"] for k in x},
                         {"original": "cli_idle_timeout", "hedge": "cli_idle_timeout", "fallback": "ok"})
        self.assertEqual({k: x[k]["role"] for k in x}, {"original": "failed", "hedge": "failed", "fallback": "used"})
        self.assertEqual(self.seat_json("x")["answered_by"], "fallback")
        self.assertIn("S3-FALLBACK", self.read("seats", "x.md"))
        recs = [(c["seat"], c["call"], "end" if "status" in c else "start") for c in self.calls() if c["seat"] == "x"]
        self.assertEqual(recs, [("x", "hedge", "start"), ("x", "hedge", "end"), ("x", "fallback", "start"),
                                ("x", "fallback", "end")], "D24: the fallback waits for both calls to end")
        hedge_start = [c for c in self.starts("hedge") if c["seat"] == "x"][0]
        self.assertLess(hedge_start["after_s"], SOFT - 0.2, "the original was reaped, so the hedge starts at once")
        # y: no stop without a proven terminal state
        y = self.by_call("y")
        self.assertEqual((y["original"]["status"], y["original"]["exit_code"]), ("ok", 0))
        self.assertEqual(y["hedge"]["status"], "ok")
        self.assertEqual(self.seat_json("y")["answered_by"], "hedge")
        self.assertEqual(y["original"]["role"], "late")
        self.assertIn("S3-Y-ORIGINAL", self.read("seats", "y.original.late.md"))
        yh = [c for c in self.starts("hedge") if c["seat"] == "y"][0]
        self.assertGreaterEqual(yh["after_s"], SOFT - 0.05)
        self.assertLess(yh["after_s"], SOFT + 0.4, "the unlisted line does not move the hedge")
        self.assertEqual([c for c in self.calls() if c["seat"] == "y" and c["call"] == "fallback"], [])
        self.assert_not_killed_by_time("y")
        self.assert_not_killed_by_time("x")
        self.assertIn("S3-Y-HEDGE", self.read("seats", "y.md"))
        self.assert_no_survivors()

    # ---- S4 -----------------------------------------------------------------------------------------------
    def scenario_4_observer_loss(self):
        seats = [seat("a"), seat("b"), self.rseat("x", "s4")]
        cfg = self.rcfg(seats)
        proc = self.launch_runner(cfg)
        raw = os.path.join(self.out, "seats", "x.hedge.raw")

        def hedge_running():
            try:
                with open(raw) as f:
                    return "item.started" in f.read() and bool(self.starts("hedge"))
            except OSError:
                return False
        self.assertTrue(_wait(hedge_running, 15), "the hedge never started: " + self.runner_log())
        x_pid, hedge_pid = self.pid_of("x"), self.pid_of("x_hedge")
        os.kill(proc.pid, signal.SIGKILL)               # the observer is lost
        proc.wait()
        time.sleep(0.5)
        self.assertFalse(_gone(hedge_pid) or _gone(x_pid), "the watch calls outlive the runner (D18)")
        # the same out dir again
        code = self.run_council(cfg)
        self.assertEqual(code, 0)
        roles = [r for r, _p in self.stub_pids()]
        self.assertEqual(roles.count("x_hedge"), 1, "no second hedge across the two runs: %s" % roles)
        self.assertNotIn("x_fallback", roles)
        self.assertEqual(roles.count("x"), 2, "run 2 started its own original")
        lost = [c for c in self.calls() if c.get("status") == "observer_lost"]
        self.assertEqual([(c["seat"], c["call"]) for c in lost], [("x", "hedge")])
        x = self.seat_json("x")
        self.assertEqual({c["call"]: c["role"] for c in x["calls"]}, {"hedge": "observer_lost", "original": "used"})
        self.assertEqual((x["status"], x["answered_by"]), ("ok", "original"))
        self.assertIn("S4-ORIGINAL", self.read("seats", "x.md"))
        self.assert_not_killed_by_time("x")
        # the lost hedge is never reaped by run 2 and keeps writing its capture to disk
        self.assertTrue(_wait(lambda: _gone(hedge_pid) and _gone(x_pid), 8), "the lost calls end on their own")
        with open(raw) as f:
            text = f.read()
        self.assertIn("item.started", text)
        self.assertIn("S4-HEDGE", text, "the lost call's raw output is kept")
        self.assertEqual(len(self.starts("hedge")), 1)
        self.assertTrue(_wait(lambda: all(_gone(p) for _, p in self.stub_pids()), 5))


class ReplaySignalOn(ReplayBase):
    signal_on = True

    def test_s1_long_silence_signal_on(self):
        self.scenario_1_long_silence()

    def test_s2_endless_keepalives_signal_on(self):
        self.scenario_2_endless_keepalives()

    def test_s3_explicit_failure_signal_on(self):
        self.scenario_3_explicit_failure()

    def test_s4_observer_loss_signal_on(self):
        self.scenario_4_observer_loss()


class ReplaySignalOff(ReplayBase):
    signal_on = False

    def test_s1_long_silence_signal_off(self):
        self.scenario_1_long_silence()

    def test_s2_endless_keepalives_signal_off(self):
        self.scenario_2_endless_keepalives()

    def test_s3_explicit_failure_signal_off(self):
        self.scenario_3_explicit_failure()

    def test_s4_observer_loss_signal_off(self):
        self.scenario_4_observer_loss()


if __name__ == "__main__":
    unittest.main()
