"""Liveness probe tests (row 20): tools/liveness-probe.py on the real adapters (dry-run argv only) and on stub
CLIs (live runs). No network, no codex, no agy, no paid call. `ss` is faked wherever a test is not about it.

Run: python3 -Es -m unittest tests.test_liveness_probe   (cwd ~/.claude/council)
"""
import glob
import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import providers  # noqa: E402
from councillib.otlp_sink import Sink  # noqa: E402
from councillib.supervise import Launch  # noqa: E402

TOOL = os.path.join(ROOT, "tools", "liveness-probe.py")
STUB = os.path.join(ROOT, "tests", "stub_cli.py")
_spec = importlib.util.spec_from_file_location("liveness_probe", TOOL)
lp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lp)

SENTINEL = "TESTSECRET-liveness-sentinel-0123456789"
QUESTION = "QUESTION-SENTINEL-77 what is two plus two"


def _no_ss(_args):
    return ""


def _toml(v):
    return json.dumps(v) if isinstance(v, str) else repr(v).lower() if isinstance(v, bool) else repr(v)


def stub_launch(tmp, *mode, env=None):
    return Launch(argv=[sys.executable, "-Es", STUB, *[str(m) for m in mode]], env=dict(env or os.environ),
                  cwd=tmp)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out, mode=0o700)
        self.pidfile = os.path.join(self.tmp, "pids")
        self.prompts = os.path.join(self.tmp, "prompts")
        os.makedirs(self.prompts)
        with open(os.path.join(self.prompts, "advisor.md"), "w") as f:
            f.write("SEAT $seat_id of $n_seats\nQ: $question\nC: $context\n")
        self.state = os.path.join(self.tmp, "state")
        env = {"STUB_PIDFILE": self.pidfile, "SOME_API_KEY": SENTINEL}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.before = set(glob.glob(os.path.join(tempfile.gettempdir(), lp.WORK_PREFIX + "*")))
        self.addCleanup(self._reap_stubs)

    def _reap_stubs(self):
        try:
            with open(self.pidfile) as f:
                pids = [int(ln.split()[1]) for ln in f if ln.strip()]
        except (OSError, ValueError, IndexError):
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def stub_pids(self):
        try:
            with open(self.pidfile) as f:
                return [int(ln.split()[1]) for ln in f if ln.strip()]
        except OSError:
            return []

    def overlay(self, stub_mode=None, real=False):
        lines = ["state_dir = %s" % _toml(self.state), "prompts_dir = %s" % _toml(self.prompts)]
        if not real:
            lines += ["[providers.stub]", 'mode = "buffered"', "[[seat]]", 'id = "s"', 'provider = "stub"',
                      'model = "stub-model"',
                      "stub_mode = [%s]" % ", ".join(_toml(str(m)) for m in (stub_mode or ["quick"]))]
        path = os.path.join(self.tmp, "overlay%d.toml" % len(os.listdir(self.tmp)))
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def main(self, *argv, ss=_no_ss):
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err):
            rc = lp.main(list(argv), ss_runner=ss)
        return rc, buf.getvalue(), err.getvalue()

    def rec(self, label):
        with open(os.path.join(self.out, label + ".json")) as f:
            return json.load(f)

    def samples(self, label):
        with open(os.path.join(self.out, label + ".samples.jsonl")) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def opts(self, **kw):
        base = dict(drain_s=0.0, sample_s=0.25, term_grace_s=5.0, ss_runner=_no_ss, horizon=30.0)
        base.update(kw)
        return lp.Options(**base)

    def assert_clean(self):
        left = set(glob.glob(os.path.join(tempfile.gettempdir(), lp.WORK_PREFIX + "*"))) - self.before
        self.assertEqual(left, set(), "probe workdir left behind")
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)
        for name in os.listdir(self.out):
            path = os.path.join(self.out, name)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, name)
            with open(path, "rb") as f:
                self.assertNotIn(SENTINEL.encode(), f.read(), name)


# ---------------------------------------------------------------- pure helpers
class PureTests(unittest.TestCase):
    def test_gap_stats_counts_edges(self):
        g = lp.gap_stats([5.0, 10.0, 12.0, 99.0], 20.0)
        self.assertEqual((g["count"], g["count_total"], g["first_s"], g["last_s"]), (3, 4, 5.0, 12.0))
        self.assertEqual(g["max_gap_s"], 8.0)              # 12 -> 20 is the longest gap

    def test_gap_stats_silent_signal_reports_the_whole_phase(self):
        g = lp.gap_stats([], 42.0)
        self.assertEqual((g["count"], g["first_s"], g["max_gap_s"]), (0, None, 42.0))
        self.assertEqual(lp.gap_stats([30.0], 20.0)["max_gap_s"], 20.0)

    def test_strip_print_timeout_both_forms(self):
        self.assertEqual(lp.strip_print_timeout(["agy", "--print-timeout", "3660s", "--model", "m"]),
                         ["agy", "--model", "m"])
        self.assertEqual(lp.strip_print_timeout(["agy", "--print-timeout=60s", "--x"]), ["agy", "--x"])

    def test_otel_and_agy_flags(self):
        f = lp.otel_flags(4321, "binary")
        self.assertEqual(f[0], "-c")
        self.assertIn('endpoint="http://127.0.0.1:4321/v1/logs"', f[1])
        self.assertIn('protocol="binary"', f[1])
        self.assertEqual(f[2:], ["-c", "otel.log_user_prompt=false"])
        self.assertEqual(lp.agy_log_flags("/x/l.log"), ["--v=3", "--log-file", "/x/l.log"])

    def test_step_tracker_codex_and_agy(self):
        t = lp.StepTracker()
        feed = lambda *evs: t.feed(("\n".join(json.dumps(e) for e in evs) + "\n").encode())    # noqa: E731
        feed({"type": "item.started", "item": {"id": "a", "type": "command_execution"}})
        self.assertEqual(t.open_types(), ["command_execution"])
        feed({"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "tool"}})
        self.assertEqual(t.open_count(), 2)
        feed({"type": "item.completed", "item": {"id": "a", "type": "command_execution"}},
             {"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "tool"}})
        self.assertEqual((t.open_count(), t.max_open), (0, 2))

    def test_step_tracker_survives_split_lines(self):
        t = lp.StepTracker()
        line = json.dumps({"type": "item.started", "item": {"id": "z", "type": "x"}}).encode() + b"\n"
        t.feed(line[:10])
        self.assertEqual(t.open_count(), 0)
        t.feed(line[10:])
        self.assertEqual(t.open_count(), 1)

    def test_read_daemon_pid(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "daemon.pid")
            self.assertIsNone(lp.read_daemon_pid(p))
            with open(p, "w") as f:
                f.write('{"pid":843146,"processStartTime":"x"}')
            self.assertEqual(lp.read_daemon_pid(p), 843146)
            with open(p, "w") as f:
                f.write("777\n")
            self.assertEqual(lp.read_daemon_pid(p), 777)


# ---------------------------------------------------------------- dry-run on the REAL adapters
class DryRunTests(Base):
    def dry(self, *argv):
        qf = os.path.join(self.tmp, "q.md")
        with open(qf, "w") as f:
            f.write(QUESTION)
        return self.main("run", "--dry-run", "--config", self.overlay(real=True), "--question-file", qf, *argv)

    def test_codex_dry_run_otlp_flags_precede_stdin_marker(self):
        rc, out, _ = self.dry("--seat", "codex", "--otlp", "--env", "RUST_LOG=codex_api=trace",
                              "--env", "MY_TOKEN=" + SENTINEL, "--codex-c", "model_reasoning_summary=detailed")
        self.assertEqual(rc, 0)
        argv = shlex.split(next(ln for ln in out.splitlines() if ln.startswith("argv: "))[6:])
        self.assertEqual(argv[:3], ["codex", "exec", "--json"])
        self.assertEqual(argv[-1], "-")
        self.assertIn("model_provider=openai-council", argv)
        i = argv.index('otel.exporter={otlp-http={endpoint="http://127.0.0.1:<sink-port>/v1/logs",protocol="json"}}')
        self.assertEqual(argv[i - 1], "-c")
        self.assertEqual(argv[i + 1:i + 3], ["-c", "otel.log_user_prompt=false"])
        self.assertLess(i, len(argv) - 1)
        self.assertEqual(argv[-3:-1], ["-c", "model_reasoning_summary=detailed"])
        self.assertIn("RUST_LOG=codex_api=trace", out)
        self.assertIn("MY_TOKEN=<hidden>", out)
        self.assertNotIn(SENTINEL, out)
        self.assertIn("COUNCIL_SEAT_TAG=<uuid per launch>", out)
        self.assertIn("<sink-port>", out)

    def test_codex_dry_run_without_flags_equals_the_shipped_build(self):
        rc, out, _ = self.dry("--seat", "codex")
        self.assertEqual(rc, 0)
        self.assertNotIn("otel", out)
        self.assertNotIn(QUESTION, out)
        self.assertIn("stdin: <prompt", out)

    def test_agy_dry_run_log_flags_before_print_and_prompt_redacted(self):
        rc, out, _ = self.dry("--seat", "agy", "--agy-log", "--out", os.path.join(self.tmp, "nope"),
                              "--label", "A")
        self.assertEqual(rc, 0)
        argv = shlex.split(next(ln for ln in out.splitlines() if ln.startswith("argv: "))[6:])
        self.assertEqual(argv[:1], ["agy"])
        i = argv.index("--v=3")
        self.assertEqual(argv[i + 1], "--log-file")
        self.assertEqual(argv[i + 2], os.path.join(self.tmp, "nope", "A.agy.log"))
        self.assertLess(i, argv.index("--print"))
        self.assertTrue(argv[argv.index("--print") + 1].startswith("<prompt "))
        self.assertIn("--print-timeout", argv)
        self.assertNotIn(QUESTION, out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope")), "a dry-run creates nothing")

    def test_agy_no_print_timeout_removes_flag_and_value(self):
        _, with_pt, _ = self.dry("--seat", "agy")
        _, without, _ = self.dry("--seat", "agy", "--no-print-timeout")
        a = shlex.split(next(ln for ln in with_pt.splitlines() if ln.startswith("argv: "))[6:])
        b = shlex.split(next(ln for ln in without.splitlines() if ln.startswith("argv: "))[6:])
        i = a.index("--print-timeout")
        self.assertRegex(a[i + 1], r"^3660s$")
        self.assertEqual(b, a[:i] + a[i + 2:])

    def test_wrong_seat_flags_exit_2(self):
        for argv in (["--seat", "agy", "--otlp"], ["--seat", "codex", "--agy-log"],
                     ["--seat", "codex", "--no-print-timeout"], ["--seat", "agy", "--codex-c", "a=b"]):
            rc, _, err = self.dry(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("flag", err)

    def test_unknown_seat_and_sigstop_pairing_exit_2(self):
        self.assertEqual(self.dry("--seat", "nope")[0], 2)
        self.assertEqual(self.dry("--seat", "codex", "--sigstop-at", "5")[0], 2)

    def test_live_run_requires_out_and_label(self):
        rc, _, err = self.main("run", "--seat", "s", "--config", self.overlay())
        self.assertEqual(rc, 2)
        self.assertIn("--out", err)


# ---------------------------------------------------------------- live runs on stubs
class LiveStubTests(Base):
    def run_tool(self, label, stub_mode, *extra, **kw):
        return self.main("run", "--seat", "s", "--config", self.overlay(stub_mode), "--out", self.out,
                         "--label", label, "--question-file", self._q(), "--drain-s", "0", *extra, **kw)

    def _q(self):
        qf = os.path.join(self.tmp, "q.md")
        with open(qf, "w") as f:
            f.write(QUESTION)
        return qf

    def test_quick_run_records_everything_and_cleans_up(self):
        t0 = time.monotonic()
        rc, out, err = self.run_tool("q1", ["quick"], "--env", "MY_SECRET=" + SENTINEL, "--horizon", "20")
        self.assertEqual(rc, 0, err)
        r = self.rec("q1")
        self.assertEqual((r["ended_by"], r["exit_code"], r["survivors"]), ("exit", 0, []))
        self.assertGreater(r["stdout_bytes"], 0)
        self.assertIsNotNone(r["first_stdout_byte_s"])
        self.assertEqual(r["first_answer_byte_s"], r["first_stdout_byte_s"])
        self.assertIn("QUICK-OK", r["answer_head"])
        self.assertIn("MY_SECRET", r["env_keys"])
        self.assertIn("COUNCIL_SEAT_TAG", r["env_keys"])
        self.assertNotIn("env", r)
        for name in ("q1.stdout", "q1.stderr", "q1.samples.jsonl", "q1.answer.md", "q1.json", "q1.started.json"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)
        self.assertGreaterEqual(len(self.samples("q1")), 1)
        self.assertLess(time.monotonic() - t0, 15)
        self.assert_clean()

    def test_tag_reaches_the_child_environment(self):
        rc, _, err = self.run_tool("tag", ["env-cwd", "COUNCIL_SEAT_TAG"])
        self.assertEqual(rc, 0, err)
        r = self.rec("tag")
        with open(os.path.join(self.out, "tag.stdout")) as f:
            self.assertIn("ENV=%s" % r["tag"], f.read())
        self.assertEqual(len(r["tag"]), 32)

    def test_existing_label_is_an_error_and_nothing_is_overwritten(self):
        self.assertEqual(self.run_tool("dup", ["quick"])[0], 0)
        with open(os.path.join(self.out, "dup.json")) as f:
            before = f.read()
        rc, _, err = self.run_tool("dup", ["quick"])
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)
        with open(os.path.join(self.out, "dup.json")) as f:
            self.assertEqual(f.read(), before)

    def test_horizon_stops_a_silent_call_with_sigterm_and_records_it(self):
        rc, out, err = self.run_tool("hz", ["hung"], "--horizon", "1.5")
        self.assertEqual(rc, 1, err)
        r = self.rec("hz")
        self.assertEqual(r["ended_by"], "horizon")
        self.assertTrue(r["horizon_reached"])
        self.assertTrue(r["still_running_at_horizon"])
        self.assertEqual((r["exit_code"], r["exit_signal"]), (-signal.SIGTERM, signal.SIGTERM))
        self.assertEqual(r["survivors"], [])
        self.assertGreaterEqual(r["total_s"], 1.4)
        self.assertEqual(r["signals"]["stdout"]["count"], 0)
        self.assertEqual(r["signals"]["stdout"]["max_gap_s"], r["silent_end_s"])
        self.assertIsNone(r["first_answer_byte_s"])

    def test_tagged_tree_follows_a_grandchild_and_leaves_no_survivor(self):
        rc, _, err = self.run_tool("gc", ["grandchild-hang"], "--horizon", "2.5")
        self.assertEqual(rc, 1, err)
        r = self.rec("gc")
        self.assertGreaterEqual(r["tagged_tree_max"], 2)
        self.assertEqual(r["survivors"], [])
        deadline = time.monotonic() + 5
        pids = self.stub_pids()
        self.assertGreaterEqual(len(pids), 2)
        while time.monotonic() < deadline and any(os.path.exists("/proc/%d" % p) and self._alive(p) for p in pids):
            time.sleep(0.1)
        self.assertFalse([p for p in pids if self._alive(p)], "stub processes survived the horizon SIGTERM")

    @staticmethod
    def _alive(pid):
        try:
            with open("/proc/%d/stat" % pid, "rb") as f:
                return f.read().rsplit(b")", 1)[1].split()[0] != b"Z"
        except OSError:
            return False

    def test_end_at_ends_only_this_run(self):
        rc, _, _ = self.run_tool("ea", ["hung"], "--end-at", "1", "--horizon", "20")
        r = self.rec("ea")
        self.assertEqual(r["ended_by"], "end_at")
        self.assertLess(r["total_s"], 10)

    def test_observer_loss_kills_only_the_relay(self):
        rc, _, err = self.run_tool("ob", ["partial-then-hang"], "--observer-kill-at", "0.6", "--horizon", "2.5")
        r = self.rec("ob")
        o = r["observer"]
        self.assertIsNotNone(o["killed_at_s"])
        self.assertGreaterEqual(o["killed_at_s"], 0.6)
        self.assertTrue(o["cli_outlived_relay"])
        self.assertEqual(r["ended_by"], "horizon")
        with open(os.path.join(self.out, "ob.stdout")) as f:
            self.assertIn("PARTIAL-123", f.read())
        self.assertEqual(r["signals"]["stdout"]["count"], 1)

    def test_observer_loss_breaks_the_cli_stdout_pipe(self):
        """The CLI keeps writing after the relay dies: it must see the broken pipe (exit non-zero), and the
        probe must record that the call ended by itself, not by a control."""
        code = ("import sys,time\nfor i in range(40):\n print('tick',i,flush=True)\n time.sleep(0.1)\n")
        launch = Launch(argv=[sys.executable, "-Es", "-c", code], env=dict(os.environ), cwd=self.tmp)
        rec = lp.observe(launch, providers.RawTextExtractor("m"), self.out, "ob2", "t" * 32,
                         self.opts(observer_kill_at=0.5))
        self.assertEqual(rec["ended_by"], "exit")
        self.assertNotEqual(rec["exit_code"], 0)
        self.assertLess(rec["total_s"], 3.5)
        self.assertIsNotNone(rec["observer"]["killed_at_s"])

    def test_sigstop_stops_the_tree_then_continues_it(self):
        launch = stub_launch(self.tmp, "slow-stream", 2)
        rec = lp.observe(launch, providers.RawTextExtractor("m"), self.out, "st", "a" * 32,
                         self.opts(sigstop_at=0.6, sigstop_for=1.0))
        self.assertEqual((rec["ended_by"], rec["exit_code"]), ("exit", 0))
        self.assertIsNotNone(rec["sigstop"]["at_s"])
        self.assertGreaterEqual(rec["sigstop"]["cont_at_s"] - rec["sigstop"]["at_s"], 1.0)
        rows = self.samples("st")
        self.assertTrue(any("T" in r["states"].values() and r["stopped"] for r in rows), "no stopped sample")
        self.assertEqual(rec["survivors"], [])
        # no stdout inside the stop window (the stub ticks every 0.5 s)
        win = [r for r in rows if r["stopped"]]
        self.assertGreaterEqual(len(win), 2)
        self.assertEqual(len({r["stdout_bytes"] for r in win[1:]}), 1)

    def test_replay_open_steps_and_first_answer_in_the_silent_phase(self):
        from councillib.providers import CodexExtractor
        events = [{"type": "thread.started", "thread_id": "t"},
                  {"type": "item.started", "item": {"id": "c1", "type": "command_execution"}},
                  {"type": "item.completed", "item": {"id": "c1", "type": "command_execution"}},
                  {"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "FOUR"}},
                  {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
        rf = os.path.join(self.tmp, "replay.jsonl")
        with open(rf, "w") as f:
            f.write("\n".join(json.dumps(e) for e in events) + "\n")
        rec = lp.observe(stub_launch(self.tmp, "replay", rf, 0.6), CodexExtractor(), self.out, "rp", "b" * 32,
                         self.opts())
        self.assertEqual((rec["ended_by"], rec["exit_code"]), ("exit", 0))
        self.assertGreaterEqual(rec["first_answer_byte_s"], 1.7)        # the 4th line, 3 x 0.6 s in
        self.assertLess(rec["first_stdout_byte_s"], 0.6)
        self.assertEqual(rec["silent_end_s"], rec["first_answer_byte_s"])
        self.assertGreaterEqual(rec["open_steps_max"], 1)
        rows = self.samples("rp")
        self.assertTrue(any(r["open_steps"] == 1 and r["open_step_types"] == ["command_execution"] for r in rows))
        self.assertTrue(any(r["answer"] for r in rows))
        self.assertEqual(rec["signals"]["stdout"]["count_total"], 5)       # one arrival per replayed line
        self.assertGreaterEqual(rec["signals"]["stdout"]["count"], 2)
        self.assertGreater(rec["signals"]["stdout"]["max_gap_s"], 0.3)
        self.assertEqual(rec["answer_head"], "FOUR")

    def test_full_stderr_is_kept_beyond_four_kib(self):
        code = "import sys\nsys.stderr.write('e' * 20000)\nprint('x')\n"
        launch = Launch(argv=[sys.executable, "-Es", "-c", code], env=dict(os.environ), cwd=self.tmp)
        rec = lp.observe(launch, providers.RawTextExtractor("m"), self.out, "er", "c" * 32, self.opts())
        self.assertEqual(rec["stderr_bytes"], 20000)
        self.assertEqual(os.path.getsize(os.path.join(self.out, "er.stderr")), 20000)
        self.assertGreaterEqual(rec["signals"]["stderr"]["count"], 1)

    def test_otlp_events_are_logged_per_name_without_bodies(self):
        body = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{
            "body": {"stringValue": "codex.sse_event"},
            "attributes": [{"key": "event.kind", "value": {"stringValue": "reasoning_summary_delta"}},
                           {"key": "prompt", "value": {"stringValue": SENTINEL}}]}]}]}]}
        code = ("import sys,time,urllib.request\n"
                "time.sleep(0.5)\n"
                "r=urllib.request.Request('http://127.0.0.1:%s/v1/logs',data=sys.argv[1].encode(),"
                "headers={'Content-Type':'application/json'})\n"
                "urllib.request.urlopen(r).read()\n"
                "time.sleep(0.3)\n")
        with Sink() as sink:
            launch = Launch(argv=[sys.executable, "-Es", "-c", code % sink.port, json.dumps(body)],
                            env=dict(os.environ), cwd=self.tmp)
            rec = lp.observe(launch, providers.RawTextExtractor("m"), self.out, "ot", "d" * 32,
                             self.opts(), sink=sink)
        sig = rec["signals"]
        self.assertEqual(sig["otlp"]["count"], 1)
        self.assertIn("otlp:codex.sse_event", sig)
        self.assertIn("otlp:event.kind=reasoning_summary_delta", sig)
        self.assertGreaterEqual(sig["otlp"]["first_s"], 0.4)
        with open(os.path.join(self.out, "ot.otlp.jsonl")) as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("codex.sse_event", lines[0]["names"])
        self.assertEqual(self.samples("ot")[-1]["otlp_events"], 1)

    def test_failed_ss_is_recorded_never_read_as_no_socket(self):
        def boom(_a):
            raise RuntimeError("ss missing")
        rec = lp.observe(stub_launch(self.tmp, "slow-stream", 1), providers.RawTextExtractor("m"), self.out, "ssf",
                         "e" * 32, self.opts(ss_runner=boom))
        self.assertGreater(rec["estab"]["ss_failed_samples"], 0)
        self.assertEqual(rec["estab"]["samples_in_silent_phase"], 0)
        self.assertFalse(rec["estab"]["always_estab"])
        self.assertTrue(all(not r["ss_ok"] for r in self.samples("ssf")))

    def test_estab_and_tcp_rx_come_from_the_tagged_tree_only(self):
        """A fake ss reports one ESTAB socket per tagged pid that grows, and one for a stranger pid."""
        state = {"n": 0}
        launch_holder = {}

        def fake_ss(args):
            if args[0] != "-tinpH":
                return ""
            state["n"] += 1
            pid = launch_holder["pid"]
            return ('ESTAB 0 0 10.0.0.5:5000 93.184.216.34:443 users:(("codex",pid=%d,fd=9))\n'
                    '\t cubic wscale:7,7 bytes_acked:100 bytes_received:%d segs_in:%d lastrcv:50\n'
                    'ESTAB 0 0 10.0.0.5:5001 93.184.216.34:443 users:(("other",pid=1,fd=9))\n'
                    '\t cubic bytes_acked:1 bytes_received:%d segs_in:1 lastrcv:5\n'
                    % (pid, 1000 * state["n"], state["n"], 5000 * state["n"]))

        real_popen = subprocess.Popen

        def spy(*a, **k):
            p = real_popen(*a, **k)
            if "pid" not in launch_holder and k.get("start_new_session"):
                launch_holder["pid"] = p.pid
            return p
        with mock.patch.object(subprocess, "Popen", spy):
            rec = lp.observe(stub_launch(self.tmp, "slow-stream", 1.5), providers.RawTextExtractor("m"), self.out,
                             "es", "f" * 32, self.opts(ss_runner=fake_ss, drain_s=0.0))
        self.assertTrue(rec["estab"]["samples_with_estab"] >= 1)
        self.assertEqual(rec["estab"]["ss_failed_samples"], 0)
        self.assertGreaterEqual(rec["signals"]["tcp_rx"]["count_total"], 2)
        self.assertGreaterEqual(rec["tcp_owners_seen"].get("self", 0), 2)
        self.assertNotIn("outside", rec["tcp_owners_seen"])
        self.assertEqual(rec["tcp_peers"], ["93.184.216.34:443"])
        rows = self.samples("es")
        socks = [s for r in rows for s in r["tcp"]]
        self.assertTrue(socks and all(s["pid"] == launch_holder["pid"] for s in socks),
                        "a stranger pid's socket leaked into the sample")

    def test_spawn_error_is_recorded_not_raised(self):
        launch = Launch(argv=["/nonexistent/cli-binary"], env=dict(os.environ), cwd=self.tmp)
        rec = lp.observe(launch, providers.RawTextExtractor("m"), self.out, "sp", "9" * 32, self.opts())
        self.assertEqual(rec["status"], "spawn_error")
        self.assertEqual(self.rec("sp")["ended_by"], "spawn_error")


# ---------------------------------------------------------------- the CLI as a process
class ProcessTests(Base):
    def spawn(self, label, stub_mode, *extra):
        qf = os.path.join(self.tmp, "q.md")
        with open(qf, "w") as f:
            f.write(QUESTION)
        cmd = [sys.executable, "-Es", TOOL, "run", "--seat", "s", "--config", self.overlay(stub_mode),
               "--out", self.out, "--label", label, "--question-file", qf, "--drain-s", "0", *extra]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ))

    def test_signal_to_the_tool_ends_its_own_tree_only(self):
        p = self.spawn("sg", ["hung"], "--horizon", "60")
        started = os.path.join(self.out, "sg.started.json")
        deadline = time.monotonic() + 15
        while not os.path.exists(started) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(started))
        time.sleep(0.5)
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 1, err)
        r = self.rec("sg")
        self.assertEqual(r["ended_by"], "tool_signal")
        self.assertEqual(r["survivors"], [])
        self.assert_clean()

    def test_concurrent_runs_see_each_other_and_end_independently(self):
        a = self.spawn("ca", ["hung"], "--end-at", "4", "--horizon", "30")
        b = self.spawn("cb", ["slow-stream", "2"], "--concurrent-with", "ca", "--horizon", "30")
        ob, eb = b.communicate(timeout=60)
        oa, ea = a.communicate(timeout=60)
        self.assertEqual(b.returncode, 0, eb)
        ra, rb = self.rec("ca"), self.rec("cb")
        self.assertEqual(ra["ended_by"], "end_at")
        self.assertEqual(rb["ended_by"], "exit")
        c = rb["concurrent"]
        self.assertEqual((c["with"], c["other_tag"]), ("ca", ra["tag"]))
        self.assertTrue(c["other_alive_at_start"])
        self.assertNotEqual(ra["tag"], rb["tag"])
        self.assertTrue(any(r["other_pids"] for r in self.samples("cb")), "the other run's tree was not seen")

    def test_concurrent_with_a_missing_label_exits_2(self):
        p = self.spawn("cm", ["quick"], "--concurrent-with", "ghost", "--concurrent-wait", "0.5")
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertIn("ghost", err)


if __name__ == "__main__":
    unittest.main()
