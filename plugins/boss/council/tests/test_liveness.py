"""Liveness layer tests (row 28; D10, D15, D16, D30, D44, D47). Scripted samples only: a fake clock,
fake OTLP events, a fake NetSample and fake stdout/stderr lines. No network, no real CLI, no kill.

Run: python3 -Es -m unittest discover -s ~/.claude/council/tests -t ~/.claude/council
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import liveness, netwatch  # noqa: E402
from councillib.liveness import LineTail, LivenessBoard, LivenessPolicy, SeatLiveness  # noqa: E402
from councillib.supervise import Launch, Supervisor, WatchLimits  # noqa: E402

PY = sys.executable or "/usr/bin/python3"
IDLE = "stream disconnected before completion: idle timeout waiting for SSE"
CODEX_LINES = [IDLE]
AGY_LINES = ["[agy] print timeout after", ["AGY_ERROR:", "\"status\":\"RESOURCE_EXHAUSTED\""]]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def sse(*kinds, name="codex.sse_event", t=0.0):
    names = [name] + ["event.kind=%s" % k for k in kinds]
    return {"t": t, "path": "/v1/logs", "bytes": 100, "names": names}


def net(tcp=(), states=None, ok=True, pids=(100,)):
    ns = netwatch.NetSample(t=0.0)
    ns.ss_ok = ok
    ns.tcp = list(tcp)
    ns.states = dict(states if states is not None else {p: "S" for p in pids})
    return ns


ESTAB = {"pid": 100, "state": "ESTAB", "peer": "104.18.1.1", "port": 443}


class Rig:
    """One SeatLiveness with scriptable sources; step(dt) advances the fake clock and samples."""

    def __init__(self, policy=None, **kw):
        self.clock = Clock()
        self.events = []
        self.out_lines = []
        self.err_lines = []
        self.pids = [100]
        self.net = net()
        self.tool = None
        self.policy = policy or LivenessPolicy(progress_signal="otel", negative_evidence=True,
                                               terminal_lines=CODEX_LINES)

        def take(name):
            def f():
                lines, getattr(self, name)[:] = list(getattr(self, name)), []
                return lines
            return f

        src = {"otlp_events": lambda: list(self.events), "pids": lambda info: list(self.pids),
               "net": lambda pids: self.net, "stdout_lines": take("out_lines"),
               "stderr_lines": take("err_lines")}
        if kw.pop("with_tool", False):
            src["tool_open"] = lambda: self.tool
        src.update(kw.pop("sources", {}))
        self.t0 = self.clock.t
        self.seat = SeatLiveness("codex", "tag-1", self.policy, src, clock=self.clock, started_wall=self.t0, **kw)
        self.v = None

    def step(self, dt=10.0):
        self.clock.t += dt
        self.v = self.seat.sample()
        return self.v

    def ev(self, *kinds, at=None, **kw):
        e = sse(*kinds, t=self.clock.t if at is None else self.t0 + at, **kw)
        self.events.append(e)
        return e

    def run_quiet(self, upto):
        """10 s steps until elapsed >= upto; returns the verdict list."""
        out = []
        while self.clock.t - self.t0 < upto:
            out.append(self.step(10.0))
        return out


class PolicyTests(unittest.TestCase):
    def test_01_defaults_are_hedge_only_and_safe(self):
        p = LivenessPolicy.from_config()
        self.assertEqual((p.progress_signal, p.negative_evidence, p.terminal_lines), ("none", False, ()))
        self.assertEqual((p.sample_s, p.grace_s, p.no_conn_samples), (10.0, 120.0, 18))

    def test_02_from_config_reads_gate_and_liveness_tables(self):
        p = LivenessPolicy.from_config(
            {"progress_signal": "otel", "negative_evidence": True, "terminal_lines": CODEX_LINES},
            {"sample_s": 5, "grace_s": 60, "no_conn_samples": 9})
        self.assertEqual((p.progress_signal, p.negative_evidence, p.sample_s, p.grace_s, p.no_conn_samples),
                         ("otel", True, 5, 60, 9))
        self.assertEqual(p.terminal_lines, (("cli_idle_timeout", (IDLE,)),))

    def test_03_terminal_line_entries_and_reasons(self):
        got = liveness.normalize_terminal_lines(AGY_LINES + [{"reason": "stream_error", "match": "boom"}, "other"])
        self.assertEqual(got[0], ("cli_timeout", ("[agy] print timeout after",)))
        self.assertEqual(got[1][0], "quota_exhausted")
        self.assertEqual(len(got[1][1]), 2)
        self.assertEqual(got[2], ("stream_error", ("boom",)))
        self.assertEqual(got[3][0], "stream_error")

    def test_04_bad_policy_is_rejected(self):
        for kw in ({"progress_signal": "magic"}, {"sample_s": 0}, {"no_conn_samples": 0},
                   {"terminal_lines": [""]}, {"terminal_lines": [[]]},
                   {"terminal_lines": [{"reason": "nope", "match": "x"}]}):
            with self.assertRaises(ValueError, msg=str(kw)):
                LivenessPolicy(**kw)


class ProgressTests(unittest.TestCase):
    def test_10_only_non_keepalive_sse_events_are_progress(self):
        r = Rig()
        r.ev("response.created", at=3)
        v = r.step(5)
        self.assertAlmostEqual(v.progress_at, 3.0)
        r.ev("keepalive", at=33)
        v = r.step(30)
        self.assertAlmostEqual(v.progress_at, 3.0)           # a keepalive does not move it
        self.assertEqual((v.keepalives, v.keepalive_at), (1, 33.0))
        r.ev("keepalive", "response.output_text.delta", at=50)   # a batch with real kinds is progress
        v = r.step(20)
        self.assertAlmostEqual(v.progress_at, 50.0)
        self.assertEqual(v.keepalives, 1)

    def test_11_keepalive_only_batches_never_make_progress(self):
        r = Rig()
        for i in range(10):
            r.ev("keepalive", at=30 * (i + 1))
        v = r.step(400)
        self.assertIsNone(v.progress_at)
        self.assertEqual(v.keepalives, 10)
        self.assertFalse(v.progress_recent(120))

    def test_12_sse_event_with_no_kind_is_unclassified_not_progress(self):
        r = Rig()
        r.ev(at=5)
        v = r.step(10)
        self.assertIsNone(v.progress_at)
        self.assertEqual(v.unclassified, 1)

    def test_13_other_codex_events_are_not_progress(self):
        r = Rig()
        for n in ("codex.api_request", "codex.turn_ttft", "codex.conversation_starts", "codex.user_prompt"):
            r.ev("response.created", at=2, name=n)
        self.assertIsNone(r.step(10).progress_at)

    def test_14_hedge_only_seat_has_no_progress(self):
        r = Rig(LivenessPolicy(progress_signal="none"))
        r.ev("response.created", at=3)
        v = r.step(10)
        self.assertTrue(v.hedge_only)
        self.assertIsNone(v.progress_at)
        self.assertIsNone(r.seat.progress_at())
        self.assertFalse(v.progress_recent(120))

    def test_15_progress_recent_window(self):
        r = Rig()
        r.ev("response.output_text.delta", at=100)
        v = r.step(150)                                       # elapsed 150, progress at 100
        self.assertTrue(v.progress_recent(120))
        self.assertFalse(v.progress_recent(30))

    def test_16_events_are_attributed_by_the_seats_own_sink(self):
        a, b = Rig(), Rig()
        a.ev("response.created", at=3)
        self.assertIsNotNone(a.step(5).progress_at)
        self.assertIsNone(b.step(5).progress_at)

    def test_17_stream_seat_progress_is_the_last_data_line_only(self):
        stats = {"last_data_at": None, "last_keepalive_at": None, "keepalives": 0}
        r = Rig(LivenessPolicy(progress_signal="stream"), sources={"stream_stats": lambda: dict(stats)})
        stats.update(last_keepalive_at=r.t0 + 20, keepalives=1000)
        v = r.step(30)
        self.assertIsNone(v.progress_at)                      # 1000 comments, no data line
        self.assertEqual((v.keepalives, v.keepalive_at), (1000, 20.0))
        stats.update(last_data_at=r.t0 + 31)
        self.assertAlmostEqual(r.step(5).progress_at, 31.0)

    def test_18_applog_progress_needs_the_log_marker(self):
        log = []
        r = Rig(LivenessPolicy(progress_signal="applog"), sources={"log_lines": lambda: [log.pop(0) for _ in range(len(log))]})
        log += [b"W poll.go:188 something", b"I auth_provider ResolveAuthMode"]
        self.assertIsNone(r.step(10).progress_at)             # 60 s pollers are not progress
        log += [b"I stream_helpers.go:268 chunk"]
        self.assertAlmostEqual(r.step(10).progress_at, 20.0, delta=0.01)


class TerminalLineTests(unittest.TestCase):
    def jline(self, **kw):
        return json.dumps(kw).encode()

    def test_20_codex_idle_line_on_stdout_error_event(self):
        r = Rig()
        r.out_lines += [self.jline(type="error", message=IDLE),
                        self.jline(type="turn.failed", error={"message": IDLE})]
        v = r.step(10)
        self.assertEqual((v.terminal, v.terminal_source, v.terminal_match), ("cli_idle_timeout", "line", IDLE))
        self.assertEqual(r.seat.terminal(), "cli_idle_timeout")

    def test_21_answer_text_holding_the_line_is_not_terminal(self):
        r = Rig()
        r.out_lines += [self.jline(type="item.completed", item={"id": "i", "type": "agent_message", "text": IDLE})]
        r.err_lines += [b"unrelated"]
        self.assertIsNone(r.step(10).terminal)

    def test_22_codex_idle_line_on_stderr(self):
        r = Rig()
        r.err_lines += [b"ERROR codex_core::session::turn: Turn error: " + IDLE.encode()]
        self.assertEqual(r.step(10).terminal, "cli_idle_timeout")

    def test_23_unlisted_websocket_form_is_not_terminal(self):
        r = Rig()
        r.err_lines += [b"Turn error: stream disconnected before completion: idle timeout waiting for websocket",
                        b"idle timeout sending websocket request"]
        r.out_lines += [self.jline(type="error", message="idle timeout waiting for websocket")]
        self.assertIsNone(r.step(10).terminal)

    def test_24_websocket_form_counts_once_the_gate_lists_it(self):
        pol = LivenessPolicy(progress_signal="otel", terminal_lines=[IDLE, "idle timeout waiting for websocket"])
        r = Rig(pol)
        r.err_lines += [b"x idle timeout waiting for websocket"]
        self.assertEqual(r.step(10).terminal, "cli_idle_timeout")

    def test_25_agy_print_timeout_is_cli_timeout(self):
        r = Rig(LivenessPolicy(progress_signal="none", terminal_lines=AGY_LINES))
        r.err_lines += [b"[agy] print timeout after 30s with turn in progress; returning partial output"]
        v = r.step(10)
        self.assertEqual((v.terminal, v.terminal_match), ("cli_timeout", "[agy] print timeout after"))

    def test_26_agy_quota_needs_both_substrings_on_one_line(self):
        pol = LivenessPolicy(progress_signal="none", terminal_lines=AGY_LINES)
        r = Rig(pol)
        r.err_lines += [b'AGY_ERROR: {"status":"UNAVAILABLE","error_code":503}', b'"status":"RESOURCE_EXHAUSTED"']
        self.assertIsNone(r.step(10).terminal)                # substrings on two different lines
        r.err_lines += [b'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429)","status":"RESOURCE_EXHAUSTED",'
                        b'"error_code":429,"retryable":true}']
        self.assertEqual(r.step(10).terminal, "quota_exhausted")

    def test_27_line_terminals_are_sticky_and_hold_no_raw_line(self):
        r = Rig()
        secret = "user.email=somebody@example.com Authorization: Bearer abc123"
        r.err_lines += [("%s %s" % (secret, IDLE)).encode()]
        v = r.step(10)
        self.assertEqual(v.terminal, "cli_idle_timeout")
        self.assertNotIn("somebody", repr(v))
        self.assertNotIn("Bearer", repr(v))
        r.out_lines += [b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}']
        self.assertEqual(r.step(10).terminal, "cli_idle_timeout")

    def test_28_a_line_split_over_two_reads_still_matches(self):
        tmp = tempfile.mkdtemp(prefix="live-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "err")
        open(path, "w").close()
        tail = LineTail(path)
        with open(path, "ab") as f:
            f.write(b"first line\nTurn error: stream disconnected before ")
        self.assertEqual(tail.poll(), [b"first line"])
        with open(path, "ab") as f:
            f.write(b"completion: idle timeout waiting for SSE\n")
        lines = tail.poll()
        self.assertEqual(lines, [b"Turn error: " + IDLE.encode()])
        r = Rig(sources={"stderr_lines": lambda: lines})
        self.assertEqual(r.step(10).terminal, "cli_idle_timeout")

    def test_29_line_tail_handles_missing_truncated_and_none(self):
        self.assertEqual(LineTail(None).poll(), [])
        self.assertEqual(LineTail("/nonexistent/x").poll(), [])
        tmp = tempfile.mkdtemp(prefix="live-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "f")
        t = LineTail(path)
        with open(path, "wb") as f:
            f.write(b"aaaa\nbbbb\n")
        self.assertEqual(t.poll(), [b"aaaa", b"bbbb"])
        with open(path, "wb") as f:
            f.write(b"c\n")
        self.assertEqual(t.poll(), [b"c"])                    # rotated/truncated: starts over


class NoConnectionTests(unittest.TestCase):
    def test_30_fires_only_after_grace_and_18_quiet_samples(self):
        r = Rig()
        verdicts = r.run_quiet(400)
        fired = [v for v in verdicts if v.terminal == "no_connection"]
        self.assertTrue(fired)
        first = fired[0]
        self.assertEqual(first.terminal_source, "no_connection")
        self.assertEqual(first.no_conn_streak, 18)
        # grace 120 s, then 18 samples x 10 s: never before 290 s, never from the early samples
        self.assertGreaterEqual(first.elapsed_s, 290.0)
        self.assertTrue(all(v.terminal is None for v in verdicts if v.elapsed_s < 290))
        self.assertEqual(first.no_conn_needed, 18)

    def test_31_negative_evidence_off_never_fires(self):
        r = Rig(LivenessPolicy(progress_signal="otel", negative_evidence=False))
        vs = r.run_quiet(1000)
        self.assertTrue(all(v.terminal is None for v in vs))
        self.assertEqual(vs[-1].no_conn_blocked, "disabled")

    def test_32_a_live_model_socket_blocks_and_resets(self):
        r = Rig()
        r.net = net(tcp=[ESTAB])
        vs = r.run_quiet(1000)
        self.assertTrue(all(v.terminal is None for v in vs))
        self.assertEqual(vs[-1].no_conn_blocked, "socket")
        self.assertEqual(vs[-1].estab, 1)
        # the socket vanishes: the count starts over from zero
        r.net = net()
        a = r.step()
        self.assertEqual(a.no_conn_streak, 1)
        r.net = net(tcp=[ESTAB])
        self.assertEqual(r.step().no_conn_streak, 0)

    def test_33_huge_elapsed_with_a_socket_is_still_none(self):
        r = Rig()
        r.net = net(tcp=[ESTAB])
        info = SimpleNamespace(elapsed_s=86400.0, out_bytes=0, err_bytes=0, pid=100, stdout_path=None, stderr_path=None)
        r.clock.t += 86400
        self.assertIsNone(r.seat.sample(info).terminal)

    def test_34_failed_ss_is_no_evidence(self):
        r = Rig()
        r.net = net(ok=False)
        vs = r.run_quiet(1000)
        self.assertTrue(all(v.terminal is None for v in vs))
        self.assertEqual((vs[-1].no_conn_blocked, vs[-1].ss_ok), ("ss_failed", False))

    def test_35_ss_exception_is_no_evidence(self):
        r = Rig(sources={"net": lambda pids: (_ for _ in ()).throw(RuntimeError("ss gone"))})
        vs = r.run_quiet(600)
        self.assertTrue(all(v.terminal is None for v in vs))

    def test_36_no_tagged_pids_is_not_a_hang(self):
        r = Rig()
        r.pids = []
        vs = r.run_quiet(1000)
        self.assertTrue(all(v.terminal is None for v in vs))
        self.assertEqual(vs[-1].no_conn_blocked, "no_pids")

    def test_37_guard_a_open_tool_step_blocks(self):
        r = Rig()
        r.out_lines += [json.dumps({"type": "item.started", "item": {"id": "item_1", "type": "command_execution"}}).encode()]
        vs = r.run_quiet(1000)
        self.assertTrue(all(v.terminal is None for v in vs))
        self.assertTrue(vs[-1].tool_open)
        self.assertEqual(vs[-1].no_conn_blocked, "tool_open")
        r.out_lines += [json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution"}}).encode()]
        v = r.step()
        self.assertFalse(v.tool_open)
        # the count restarts after the step closes (the completed line is itself new output)
        vs = r.run_quiet(r.clock.t - r.t0 + 200)
        self.assertEqual(vs[-1].terminal, "no_connection")

    def test_38_guard_a_agy_tool_step(self):
        r = Rig(LivenessPolicy(progress_signal="none", negative_evidence=True, terminal_lines=AGY_LINES))
        step = lambda state: json.dumps({"event": "step_update", "step_update": {  # noqa: E731
            "step_index": 2, "step_type": "tool", "state": state}}).encode()
        r.out_lines += [step("ACTIVE")]
        vs = r.run_quiet(600)
        self.assertTrue(vs[-1].tool_open)
        self.assertIsNone(vs[-1].terminal)
        r.out_lines += [step("DONE")]
        self.assertFalse(r.step().tool_open)

    def test_39_guard_a_injected_tool_open_source(self):
        r = Rig(with_tool=True)
        r.tool = True
        vs = r.run_quiet(600)
        self.assertTrue(all(v.terminal is None for v in vs))

    def test_40_guard_b_children_beyond_baseline_block(self):
        r = Rig()
        r.step(5)                                         # elapsed 5 >= baseline_s: baseline {100}
        r.pids = [100, 101]                               # a tool subprocess appears
        vs = r.run_quiet(1000)
        self.assertTrue(all(v.terminal is None for v in vs))
        self.assertTrue(vs[-1].extra_children)
        self.assertEqual(vs[-1].no_conn_blocked, "children")
        r.pids = [100]                                    # it exits: the count may start again
        self.assertFalse(r.step().extra_children)

    def test_41_guard_c_stopped_process_blocks(self):
        r = Rig()
        r.net = net(states={100: "T"})
        vs = r.run_quiet(1000)
        self.assertTrue(all(v.terminal is None for v in vs))
        self.assertTrue(vs[-1].stopped)
        self.assertEqual(vs[-1].no_conn_blocked, "stopped")

    def test_42_any_new_output_byte_resets_the_count(self):
        r = Rig()
        r.run_quiet(200)
        self.assertGreater(r.v.no_conn_streak, 0)
        r.out_lines += [b'{"type":"thread.started"}']
        v = r.step()
        self.assertEqual(v.no_conn_streak, 0)
        self.assertEqual(v.no_conn_blocked, "activity")
        r.err_lines += [b"a stderr line"]
        self.assertEqual(r.step().no_conn_streak, 0)

    def test_43_byte_counters_reset_too(self):
        state = {"out": 0, "err": 0, "log": 0}
        r = Rig(sources={"out_bytes": lambda: state["out"], "err_bytes": lambda: state["err"],
                         "log_bytes": lambda: state["log"]})
        r.run_quiet(200)
        self.assertGreater(r.v.no_conn_streak, 0)
        for key in ("out", "err", "log"):
            r.run_quiet(r.clock.t - r.t0 + 30)
            self.assertGreater(r.v.no_conn_streak, 0, key)
            state[key] += 10
            self.assertEqual(r.step().no_conn_streak, 0, key)

    def test_44_any_otlp_event_resets_even_a_keepalive(self):
        r = Rig()
        r.run_quiet(200)
        self.assertGreater(r.v.no_conn_streak, 0)
        r.ev("keepalive")
        v = r.step()
        self.assertEqual(v.no_conn_streak, 0)
        self.assertIsNone(v.progress_at)                  # and the keepalive is still not progress

    def test_45_unobservable_stdout_disables_negative_evidence(self):
        r = Rig()
        r.seat.src.pop("stdout_lines")
        vs = r.run_quiet(1000)
        self.assertTrue(all(v.terminal is None for v in vs))
        self.assertEqual(vs[-1].no_conn_blocked, "stdout_unobservable")

    def test_46_a_one_hertz_caller_cannot_shorten_the_window(self):
        r = Rig()
        fired_at = None
        for _ in range(900):
            v = r.step(1.0)
            if v.terminal == "no_connection":
                fired_at = v.elapsed_s
                break
        self.assertIsNotNone(fired_at)
        self.assertGreaterEqual(fired_at, 120 + 0.9 * 18 * 10)

    def test_47_no_connection_clears_when_activity_returns(self):
        r = Rig()
        r.run_quiet(400)
        self.assertEqual(r.v.terminal, "no_connection")
        r.net = net(tcp=[ESTAB])
        self.assertIsNone(r.step().terminal)

    def test_48_a_line_terminal_wins_over_no_connection(self):
        r = Rig()
        r.run_quiet(400)
        r.err_lines += [IDLE.encode()]
        self.assertEqual(r.step().terminal, "cli_idle_timeout")

    def test_49_nothing_here_ever_signals(self):
        # the layer imports no kill path: no os.kill/killpg/signal use and no Supervisor.terminate
        with open(os.path.join(ROOT, "councillib", "liveness.py")) as f:
            src = f.read()
        for word in ("os.kill", "killpg", "import signal", ".terminate(", "subprocess"):
            self.assertNotIn(word, src.replace("never kills, signals or reaps", ""), word)


class ExitClassTests(unittest.TestCase):
    def test_50_rc101_and_broken_pipe_are_not_answers(self):
        self.assertEqual(liveness.exit_class("codex", 101, "failed printing to stdout: Broken pipe (os error 32)"),
                         "observer_lost")
        self.assertEqual(liveness.exit_class("codex", 1, "Broken pipe"), "observer_lost")

    def test_51_rc0_after_sigterm_is_not_an_answer(self):
        self.assertEqual(liveness.exit_class("codex", 0, terminated=True), "terminated_not_answer")
        self.assertEqual(liveness.exit_class("codex", 0), "no_turn_completed")
        self.assertIsNone(liveness.exit_class("codex", 0, saw_turn_completed=True))

    def test_52_agy_print_timeout_is_cli_timeout_even_at_rc0(self):
        self.assertEqual(liveness.exit_class("agy", 0, "[agy] print timeout after 30s with turn in progress"),
                         "cli_timeout")
        self.assertIsNone(liveness.exit_class("agy", 0, ""))


class BoardAndSupervisorTests(unittest.TestCase):
    def test_60_board_routes_by_key(self):
        board = LivenessBoard()
        r = Rig()
        board.add("codex", r.seat)
        v = board(SimpleNamespace(key="codex", elapsed_s=12.0, out_bytes=0, err_bytes=0, pid=1,
                                  stdout_path=None, stderr_path=None))
        self.assertEqual(v.provider, "codex")
        self.assertIsNone(board(SimpleNamespace(key="other")))
        board.remove("codex")
        self.assertIsNone(board.get("codex"))

    def test_61_verdict_is_frozen(self):
        v = Rig().step(10)
        with self.assertRaises(Exception):
            v.terminal = "x"

    def test_62_a_terminal_verdict_never_kills_the_seat(self):
        """Watch-mode Supervisor + a stub CLI that prints the idle line on stderr and keeps running:
        the verdict says cli_idle_timeout, the seat is untouched and finishes with rc 0."""
        tmp = tempfile.mkdtemp(prefix="live-sup-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        prog = ("import sys,time\n"
                "sys.stderr.write(%r + '\\n'); sys.stderr.flush()\n"
                "time.sleep(2.0)\n"
                "print('STILL-WORKING-ANSWER')\n" % IDLE)
        board = LivenessBoard()
        pol = LivenessPolicy(progress_signal="none", terminal_lines=CODEX_LINES)
        board.add("s", SeatLiveness("codex", "tag-s", pol, {"pids": lambda info: []}))
        sup = Supervisor(sampler=board)
        self.addCleanup(sup.kill_all)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "COUNCIL_SEAT_TAG": "tag-s"}
        sup.start("s", Launch(argv=[PY, "-Es", "-c", prog], env=env, cwd=tmp),
                  WatchLimits(mode="watch", idle_s=1, ceiling_s=None, deadline_s=1, grace_s=1, sample_s=0.3))
        seen, res = [], None
        end = time.monotonic() + 20
        while res is None and time.monotonic() < end:
            for item in sup.poll(0.2):
                if not hasattr(item, "kind"):
                    res = item[1]
            v = sup.liveness("s")
            if v is not None:
                seen.append(v.terminal)
        self.assertIsNotNone(res, "seat did not finish")
        self.assertIn("cli_idle_timeout", seen)
        self.assertEqual((res.status, res.exit_code, res.truncated), ("ok", 0, False))
        self.assertIn(b"STILL-WORKING-ANSWER", res.stdout)


if __name__ == "__main__":
    unittest.main()
