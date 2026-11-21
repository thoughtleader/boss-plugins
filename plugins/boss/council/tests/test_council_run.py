"""Scheduler tests (row 6): stub adapter + stub CLI only. No network, no real settings.

Every case runs in its own mkdtemp dir (state dir, templates, run dir, pid file).
Timing bounds are loose on purpose; they were measured on a lightly loaded host.

Run: python3 -Es -m unittest discover -s ~/.claude/council/tests -t ~/.claude/council
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import council_run, providers  # noqa: E402
from councillib.supervise import Launch  # noqa: E402

PY = sys.executable or "/usr/bin/python3"

ADVISOR = "SEAT $seat_id of $n_seats\nQ: $question\nC: $context\n"
CHAIR = "CHAIR\nQ: $question\nANSWERS:\n$answers\n"


class ErrExtractor(providers.RawTextExtractor):
    def error(self):
        return "stub-adapter-error"


class ErrAdapter(providers.StubAdapter):
    """Exits 0 with output, but the adapter reports an error (agy does this)."""
    name = "errstub"

    def extractor(self, seat=None):
        return ErrExtractor((seat or {}).get("model"))


class OtherAdapter(providers.StubAdapter):
    name = "xstub"


for _n in ("errstub", "xstub"):
    providers.FAMILIES.setdefault(_n, "stub")


def seat(sid, mode="quick", provider="stub", **kw):
    d = {"id": sid, "provider": provider, "model": "stub-model", "stub_mode": mode}
    d.update(kw)
    return d


def pids_launched(pidfile):
    try:
        with open(pidfile) as f:
            return [ln.split()[0] for ln in f if ln.strip()]
    except OSError:
        return []


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.prompts = os.path.join(self.tmp, "prompts")
        os.makedirs(self.prompts)
        with open(os.path.join(self.prompts, "advisor.md"), "w") as f:
            f.write(ADVISOR)
        with open(os.path.join(self.prompts, "chair.md"), "w") as f:
            f.write(CHAIR)
        self.out = os.path.join(self.tmp, "runs", "20261021T000000Z-test")
        self.pidfile = os.path.join(self.tmp, "pids")
        p = mock.patch.dict(os.environ, {"STUB_PIDFILE": self.pidfile})
        p.start()
        self.addCleanup(p.stop)
        for cls in (ErrAdapter, OtherAdapter):
            providers.register(cls())
            self.addCleanup(providers._REGISTRY.pop, cls.name, None)

    def cfg(self, seats, chair_mode="echo-stdin", mode="stream", min_ok="n-1", grace=1,
            idle=60, ceiling=120, extra=None, cut_stream=True):
        cfg = {
            "test": True, "state_dir": self.state, "prompts_dir": self.prompts,
            "seat": seats,
            "chair": {"provider": "stub", "model": "stub-model", "stub_mode": chair_mode},
            "quorum": {"min_ok": min_ok, "straggler_grace_s": grace, "cut_stream_stragglers": cut_stream},
            "timeouts": {
                "default": {"idle_s": idle, "ceiling_s": ceiling, "grace_s": 1,
                            "max_extensions": 1, "extension_factor": 1.5,
                            "base_timeout_s": 30, "min_timeout_s": 2},
                "adaptive": {"window_n": 50, "min_samples": 5, "size_exponent": 0.5,
                             "safety": 1.5},
                "chair": {"ceiling_s": 60}},
            "providers": {"stub": {"mode": mode}, "xstub": {"mode": mode},
                          "errstub": {"mode": mode}},
        }
        if extra:
            cfg.update(extra)
        return cfg

    def run_council(self, cfg, **kw):
        t = time.monotonic()
        code = council_run.run(cfg, kw.pop("question", "What is 2+2?"), kw.pop("context_files", []),
                               self.out, **kw)
        self.wall = time.monotonic() - t
        return code

    def summary(self):
        with open(os.path.join(self.out, "summary.json")) as f:
            return json.load(f)

    def by_id(self, summ):
        return {s["id"]: s for s in summ["seats"]}

    def read(self, *parts):
        with open(os.path.join(self.out, *parts)) as f:
            return f.read()

    def latency(self):
        with open(os.path.join(self.state, "latency.jsonl")) as f:
            return [json.loads(ln) for ln in f if ln.strip()]


class TestCouncilRun(Base):
    def test_01_parallel_stream_all_ok(self):
        seats = [seat("a", "slow-stream 2"), seat("b", "slow-stream 2"), seat("c", "slow-stream 2")]
        code = self.run_council(self.cfg(seats))
        self.assertEqual(code, 0)
        self.assertLess(self.wall, 4.0, "seats must run in parallel")
        s = self.by_id(self.summary())
        self.assertTrue(all(s[k]["status"] == "ok" for k in "abc"))
        self.assertIn("DONE", self.read("seats", "a.md"))

    def test_02_quorum_grace_cuts_straggler_keeps_partial(self):
        seats = [seat("a", "quick"), seat("b", "quick"), seat("c", "partial-then-hang")]
        code = self.run_council(self.cfg(seats, grace=1))
        self.assertEqual(code, 0)
        self.assertGreaterEqual(self.wall, 1.0)
        self.assertLessEqual(self.wall, 4.0)
        summ = self.summary()
        c = self.by_id(summ)["c"]
        self.assertEqual(c["status"], "cut_after_quorum")
        self.assertTrue(c["truncated"])
        md = self.read("seats", "c.md")
        self.assertIn("TRUNCATED reason=cut_after_quorum", md)
        self.assertIn("PARTIAL-123", md)
        self.assertIsNotNone(summ["quorum"]["reached_at_s"])
        synthesis = self.read("chair", "synthesis.md")      # echo-stdin chair: its own prompt
        self.assertIn("TRUNCATED", synthesis)
        self.assertIn("PARTIAL-123", synthesis)

    def test_03_quorum_not_met_exit_3_no_chair(self):
        seats = [seat("a", "quick"), seat("b", "hung"), seat("c", "hung")]
        code = self.run_council(self.cfg(seats, min_ok=2, idle=1.5))
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(os.path.join(self.out, "chair")))
        summ = self.summary()
        self.assertEqual(summ["exit_code"], 3)
        self.assertEqual(self.by_id(summ)["b"]["status"], "idle_kill")
        self.assertTrue(os.path.exists(os.path.join(self.out, "seats", "a.md")))

    def test_04_chair_failure_exit_4_answers_kept(self):
        seats = [seat("a"), seat("b")]
        code = self.run_council(self.cfg(seats, chair_mode="exit 1"))
        self.assertEqual(code, 4)
        self.assertTrue(os.path.exists(os.path.join(self.out, "seats", "a.md")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "seats", "b.md")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "chair", "synthesis.md")))
        summ = self.summary()
        self.assertEqual(summ["chair"]["status"], "exit_1")
        self.assertEqual(summ["exit_code"], 4)
        # no fallback chair: 2 seats + exactly 1 chair launch
        self.assertEqual(len(pids_launched(self.pidfile)), 3)

    def test_05_failed_seat_keeps_status_no_replacement(self):
        seats = [seat("a"), seat("b"), seat("c", "exit 1")]
        code = self.run_council(self.cfg(seats))
        self.assertEqual(code, 0)
        c = self.by_id(self.summary())["c"]
        self.assertEqual(c["status"], "exit_1")
        self.assertEqual(c["exit_code"], 1)
        # 3 seats + 1 chair, nothing launched in the failed seat's place
        self.assertEqual(len(pids_launched(self.pidfile)), 4)
        synthesis = self.read("chair", "synthesis.md")
        self.assertIn("status exit_1", synthesis)
        self.assertIn("(no answer)", synthesis)

    def test_06_allow_missing_only_drops_failed_providers(self):
        seats = [seat("a"), seat("x", provider="xstub")]
        # x passes preflight: it still sits
        code = self.run_council(self.cfg(seats, min_ok="n"), allow_missing={"xstub"},
                                preflight_result=set())
        self.assertEqual(code, 0)
        self.assertEqual(self.by_id(self.summary())["x"]["status"], "ok")
        self.assertEqual(len(pids_launched(self.pidfile)), 3)
        shutil.rmtree(self.out)
        os.remove(self.pidfile)
        # x failed preflight: dropped, n = 1
        code = self.run_council(self.cfg(seats, min_ok="n"), allow_missing={"xstub"},
                                preflight_result={"xstub"})
        self.assertEqual(code, 0)
        summ = self.summary()
        self.assertEqual(self.by_id(summ)["x"]["status"], "skipped_allowed_missing")
        self.assertEqual(len(pids_launched(self.pidfile)), 2)       # a + chair
        self.assertFalse(os.path.exists(os.path.join(self.out, "seats", "x.md")))
        # failed preflight but NOT allowed-missing: not dropped by this module
        shutil.rmtree(self.out)
        code = self.run_council(self.cfg(seats, min_ok="n"), allow_missing=set(),
                                preflight_result={"xstub"})
        self.assertEqual(self.by_id(self.summary())["x"]["status"], "ok")

    def test_07_model_mismatch(self):
        seats = [seat("a"), seat("b", stub_reported="some-other-model"), seat("c")]
        code = self.run_council(self.cfg(seats))
        self.assertEqual(code, 0)
        b = self.by_id(self.summary())["b"]
        self.assertEqual(b["status"], "model_mismatch")
        self.assertEqual(b["model_reported"], "some-other-model")
        self.assertNotIn("QUICK-OK", self.read("chair", "synthesis.md").split("seat b")[1].split("###")[0])

    def test_08_buffered_seat_uses_p95_from_log(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "latency.jsonl"), "w") as f:
            for i in range(6):
                f.write(json.dumps({"kind": "seat", "provider": "stub", "model": "stub-model",
                                    "outcome": "ok", "total_s": 4.0 + i, "prompt_bytes": 100}) + "\n")
        code = self.run_council(self.cfg([seat("a"), seat("b")], mode="buffered"))
        self.assertEqual(code, 0)
        s = self.by_id(self.summary())
        self.assertEqual(s["a"]["deadline_source"], "p95")
        self.assertGreater(s["a"]["deadline_s"], 2.0)
        self.assertEqual(s["a"]["mode"], "buffered")
        # without a log (and a missing mode) it is the cold buffered path
        shutil.rmtree(self.out)
        shutil.rmtree(self.state)
        cfg = self.cfg([seat("a"), seat("b")])
        del cfg["providers"]
        self.assertEqual(self.run_council(cfg), 0)
        self.assertEqual(self.by_id(self.summary())["a"]["deadline_source"], "cold")

    def test_09_layout(self):
        ctx = os.path.join(self.tmp, "ctx.txt")
        with open(ctx, "w") as f:
            f.write("some context")
        code = self.run_council(self.cfg([seat("a"), seat("b")]), context_files=[ctx])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(self.out)), ["chair", "events.jsonl", "question.md", "seats", "status.json", "summary.json"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, "seats"))),
                         ["a.json", "a.md", "a.raw", "b.json", "b.md", "b.raw"])
        self.assertEqual(os.listdir(os.path.join(self.out, "chair")), ["synthesis.md"])
        self.assertEqual(self.read("question.md"), "What is 2+2?")
        for rel in ("summary.json", "seats/a.raw", "chair/synthesis.md"):
            self.assertEqual(os.stat(os.path.join(self.out, rel)).st_mode & 0o777, 0o600)
        summ = self.summary()
        for k in ("run_id", "question_bytes", "roster", "seats", "quorum", "chair", "exit_code"):
            self.assertIn(k, summ)
        for k in ("id", "provider", "model", "model_reported", "status", "exit_code", "first_byte_s",
                  "max_gap_s", "total_s", "out_bytes", "extensions", "deadline_s",
                  "deadline_source", "truncated", "paths"):
            self.assertIn(k, summ["seats"][0])
        for k in ("min_ok", "ok", "reached_at_s", "grace_s"):
            self.assertIn(k, summ["quorum"])
        self.assertEqual(summ["quorum"]["ok"], 2)
        self.assertEqual(summ["chair"]["status"], "ok")
        # advisor prompt got the fenced context, the seat id and n (echoed back by a stub)
        shutil.rmtree(self.out)
        self.assertEqual(self.run_council(self.cfg([seat("a", "echo-stdin"), seat("b", "echo-stdin")]),
                                          context_files=[ctx]), 0)
        md = self.read("seats", "a.md")
        self.assertIn("SEAT a of 2", md)
        self.assertIn("some context", md)
        self.assertIn(ctx, md)
        self.assertNotIn("$", md)

    def test_10_latency_log(self):
        seats = [seat("a"), seat("b"), seat("c")]
        self.assertEqual(self.run_council(self.cfg(seats)), 0)
        lines = self.latency()
        kinds = [ln["kind"] for ln in lines]
        self.assertEqual(kinds.count("test"), 3)
        self.assertEqual(kinds.count("chair"), 1)
        self.assertEqual(len(lines), 4)
        text = json.dumps(lines)
        self.assertNotIn("What is 2+2?", text)
        self.assertNotIn(PY, text)
        for ln in lines:
            self.assertEqual(ln["run_id"], os.path.basename(self.out))
            self.assertEqual(ln["outcome"], "ok")
        # a real (non-test) run logs kind "seat"
        shutil.rmtree(self.out)
        os.remove(os.path.join(self.state, "latency.jsonl"))
        cfg = self.cfg(seats)
        cfg["test"] = False
        self.assertEqual(self.run_council(cfg), 0)
        self.assertEqual([ln["kind"] for ln in self.latency()].count("seat"), 3)

    def test_11_adapter_reported_error_is_a_failed_seat(self):
        # exit 0 with output, but the adapter says ERROR: failed seat, never ok
        seats = [seat("a"), seat("b"), seat("e", provider="errstub")]
        self.assertEqual(self.run_council(self.cfg(seats)), 0)
        e = self.by_id(self.summary())["e"]
        self.assertEqual(e["status"], "adapter_error")
        self.assertEqual(e["detail"], "stub-adapter-error")
        self.assertEqual(self.summary()["quorum"]["ok"], 2)
        self.assertEqual(len(pids_launched(self.pidfile)), 4)       # no replacement
        # a lone errored seat can never reach quorum
        shutil.rmtree(self.out)
        self.assertEqual(self.run_council(self.cfg([seat("e", provider="errstub")], min_ok="n")), 3)

    def test_12_empty_output_and_bad_inputs(self):
        seats = [seat("a"), seat("b"), seat("c", "exit 0")]
        self.assertEqual(self.run_council(self.cfg(seats)), 0)
        self.assertEqual(self.by_id(self.summary())["c"]["status"], "empty_output")
        # min_ok above the seats launched: exit 3 before any launch
        shutil.rmtree(self.out)
        os.remove(self.pidfile)
        self.assertEqual(self.run_council(self.cfg([seat("a"), seat("b")], min_ok=5)), 3)
        self.assertEqual(pids_launched(self.pidfile), [])
        # missing template: exit 2 before any launch
        os.remove(os.path.join(self.prompts, "chair.md"))
        shutil.rmtree(self.out)
        self.assertEqual(self.run_council(self.cfg([seat("a")])), 2)
        self.assertEqual(pids_launched(self.pidfile), [])

    def test_13_signal_writes_partials_and_leaves_no_orphans(self):
        harness = os.path.join(self.tmp, "harness.py")
        cfg = self.cfg([seat("a", "partial-then-hang"), seat("b", "partial-then-hang")])
        cfg_path = os.path.join(self.tmp, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        with open(harness, "w") as f:
            f.write("import json, sys\nsys.path.insert(0, %r)\n"
                    "from councillib import council_run\n"
                    "cfg = json.load(open(sys.argv[1]))\n"
                    "sys.exit(council_run.run(cfg, 'Q?', [], sys.argv[2]))\n" % ROOT)
        proc = subprocess.Popen([PY, "-Es", harness, cfg_path, self.out], env=dict(os.environ),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, cwd=self.tmp)
        try:
            end = time.monotonic() + 15
            while time.monotonic() < end and len(pids_launched(self.pidfile)) < 2:
                time.sleep(0.1)
            time.sleep(1.0)                          # let PARTIAL-123 arrive
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(rc, 128 + signal.SIGTERM)
        summ = self.summary()
        self.assertEqual(summ["exit_code"], 128 + signal.SIGTERM)
        # boss.progress/1: the signal handler's forced final write leaves a terminal `cancelled` snapshot
        with open(os.path.join(os.environ["BOSS_PROGRESS_DIR"], "runs", "council", os.path.basename(self.out),
                               "progress.json")) as f:
            prog = json.load(f)
        self.assertEqual((prog["state"], prog["ext"]["council"]["exit_code"]), ("cancelled", 128 + signal.SIGTERM))
        self.assertTrue(prog["ended_at"])
        self.assertEqual(self.by_id(summ)["a"]["status"], "signal_kill")
        md = self.read("seats", "a.md")
        self.assertIn("TRUNCATED reason=signal_kill", md)
        self.assertIn("PARTIAL-123", md)
        time.sleep(0.5)
        with open(self.pidfile) as f:
            pids = [int(ln.split()[1]) for ln in f if ln.strip()]
        for pid in pids:
            try:
                with open("/proc/%d/stat" % pid, "rb") as f:
                    data = f.read()
                state = data[data.rfind(b")") + 2:].split()[0]
            except OSError:
                continue
            self.assertEqual(state, b"Z", "stub pid %d outlived the runner" % pid)

    # ---- buffered seats are not cut by the straggler grace (owner ruling 2026-10-21)
    def mixed_cfg(self, seats, **kw):
        """provider stub = stream, provider xstub = buffered."""
        cfg = self.cfg(seats, **kw)
        cfg["providers"] = {"stub": {"mode": "stream"}, "xstub": {"mode": "buffered"}}
        return cfg

    def test_17_buffered_straggler_after_quorum_finishes_and_reaches_chair(self):
        seats = [seat("a"), seat("b"), seat("c"),
                 seat("d", "busy-silent 3", provider="xstub")]
        code = self.run_council(self.mixed_cfg(seats, grace=1))
        self.assertEqual(code, 0)
        self.assertGreaterEqual(self.wall, 2.5, "the chair must wait for the buffered seat")
        summ = self.summary()
        d = self.by_id(summ)["d"]
        self.assertEqual((d["status"], d["mode"], d["truncated"]), ("ok", "buffered", False))
        self.assertIsNotNone(summ["quorum"]["reached_at_s"])
        self.assertLess(summ["quorum"]["reached_at_s"], 1.5)
        self.assertIn("DONE", self.read("seats", "d.md"))
        synthesis = self.read("chair", "synthesis.md")      # echo-stdin chair: its own prompt
        self.assertIn("seat d", synthesis)
        self.assertIn("status ok", synthesis.split("seat d")[1])
        self.assertIn("DONE", synthesis.split("seat d")[1])

    def test_18_stream_straggler_still_cut_while_buffered_one_is_kept(self):
        seats = [seat("a"), seat("b"), seat("s", "partial-then-hang"),
                 seat("d", "busy-silent 3", provider="xstub")]
        code = self.run_council(self.mixed_cfg(seats, grace=1, min_ok=2))
        self.assertEqual(code, 0)
        s = self.by_id(self.summary())
        self.assertEqual(s["s"]["status"], "cut_after_quorum")
        self.assertTrue(s["s"]["truncated"])
        self.assertIn("PARTIAL-123", self.read("seats", "s.md"))
        self.assertLess(s["s"]["total_s"], 2.5, "stream seat is cut at the grace, not later")
        self.assertEqual(s["d"]["status"], "ok")
        self.assertGreaterEqual(self.wall, 2.5)

    def test_18b_stream_straggler_still_producing_is_not_cut_by_default(self):
        """council-hardening row 11: no wall-clock cut of a seat that is still producing output."""
        seats = [seat("a"), seat("b"), seat("s", "slow-stream 3")]
        cfg = self.cfg(seats, grace=1)
        del cfg["quorum"]["cut_stream_stragglers"]            # the default is off
        code = self.run_council(cfg)
        self.assertEqual(code, 0)
        s = self.by_id(self.summary())["s"]
        self.assertEqual((s["status"], s["truncated"]), ("ok", False))
        self.assertIn("DONE", self.read("seats", "s.md"))
        self.assertGreaterEqual(self.wall, 2.5, "the chair waits for the producing stream seat")
        self.assertIn("DONE", self.read("chair", "synthesis.md").split("seat s")[1])

    def test_18c_stream_straggler_silent_after_quorum_ends_by_idle_timeout(self):
        seats = [seat("a"), seat("b"), seat("s", "partial-then-hang")]
        code = self.run_council(self.cfg(seats, grace=1, idle=2, cut_stream=False))
        self.assertEqual(code, 0)
        s = self.by_id(self.summary())["s"]
        self.assertEqual(s["status"], "idle_kill")             # not cut_after_quorum at 1 s
        self.assertTrue(s["truncated"])

    def test_19_buffered_seat_silent_past_its_deadline_is_still_killed(self):
        seats = [seat("a"), seat("b"), seat("h", "hung", provider="xstub")]
        cfg = self.mixed_cfg(seats, grace=1)
        cfg["timeouts"]["default"]["base_timeout_s"] = 2       # cold buffered deadline
        code = self.run_council(cfg)
        self.assertEqual(code, 0)
        h = self.by_id(self.summary())["h"]
        self.assertEqual(h["status"], "deadline_kill")        # not cut_after_quorum at 1 s
        self.assertTrue(h["truncated"])
        self.assertGreaterEqual(h["total_s"], 1.8)
        self.assertLessEqual(self.wall, 6.0)


# ---------------------------------------------------------------- row 7 additions
class FileAdapter(providers.StubAdapter):
    """Like codex: the answer is in a -o file (final_text_path); stdout is noise."""
    name = "filestub"
    salvage = False
    events = []

    def build(self, seat, prompt_path, workdir, limits, cfg):
        FileAdapter.events.append(("build", seat.get("id"), seat.get("role")))
        last = os.path.join(workdir, "last.txt")
        code = ("import sys;open(sys.argv[1],'w').write('FILE-ANSWER');"
                "print('STDOUT-NOISE');sys.exit(int(sys.argv[2]))")
        return Launch(argv=[PY, "-Es", "-c", code, last, str(seat.get("rc", 0))],
                      env=dict(os.environ), cwd=workdir, stdin_path=prompt_path, final_text_path=last)

    def extractor(self, seat=None):
        FileAdapter.events.append(("extractor", (seat or {}).get("id"), None))
        ex = providers.RawTextExtractor((seat or {}).get("model"))
        if self.salvage:
            ex.salvage_exit = lambda: True
        return ex


class SalvageAdapter(FileAdapter):
    name = "salvstub"
    salvage = True


class TestAdapterHooks(Base):
    def setUp(self):
        super().setUp()
        for cls in (FileAdapter, SalvageAdapter):
            providers.FAMILIES.setdefault(cls.name, "stub")
            providers.register(cls())
            self.addCleanup(providers._REGISTRY.pop, cls.name, None)
        FileAdapter.events = []

    def cfg2(self, seats, chair_provider="filestub"):
        cfg = self.cfg(seats)
        cfg["chair"] = {"provider": chair_provider, "model": "stub-model"}
        cfg["providers"].update({"filestub": {"mode": "stream"}, "salvstub": {"mode": "stream"}})
        return cfg

    def test_14_build_runs_before_extractor_and_final_file_is_the_answer(self):
        seats = [seat("a", provider="filestub"), seat("b", provider="filestub")]
        code = self.run_council(self.cfg2(seats))
        self.assertEqual(code, 0)
        ev = FileAdapter.events
        for sid in ("a", "b", "chair"):
            kinds = [k for k, i, _ in ev if i == sid]
            self.assertEqual(kinds, ["build", "extractor"], (sid, ev))
        self.assertEqual(self.read("seats", "a.md"), "FILE-ANSWER")       # file, not STDOUT-NOISE
        self.assertIn("STDOUT-NOISE", self.read("seats", "a.raw"))
        self.assertEqual(self.read("chair", "synthesis.md"), "FILE-ANSWER")

    def test_15_chair_launch_is_marked_with_its_role(self):
        self.run_council(self.cfg2([seat("a", provider="filestub")]))
        roles = {i: r for k, i, r in FileAdapter.events if k == "build"}
        self.assertEqual(roles["chair"], "chair")
        self.assertIsNone(roles["a"])

    def test_16_extractor_may_accept_a_nonzero_exit_after_an_output_cap(self):
        seats = [seat("cap", provider="salvstub", rc=1), seat("bad", provider="filestub", rc=1),
                 seat("fine", provider="filestub")]
        code = self.run_council(self.cfg2(seats), )
        self.assertEqual(code, 0)
        s = self.by_id(self.summary())
        self.assertEqual((s["cap"]["status"], s["cap"]["detail"]), ("ok", "output_cap"))
        self.assertEqual(s["bad"]["status"], "exit_1")                     # no hook, still a failure
        self.assertEqual(s["fine"]["status"], "ok")
        self.assertIn("FAILED status=exit_1", self.read("seats", "bad.md"))


# ---------------------------------------------------------------- row 30: hedge, fallback, pending, calls.jsonl
class HedgeStub(providers.StubAdapter):
    """Buffered stub for row 30. Its hedge (workdir <id>_hedge) runs `stub_hedge_mode` and its fallback
    (<id>_fallback) `stub_fallback_mode`; every launch is logged with its launch_opts and limits, and STUB_ROLE is
    the workdir name, so the pid file names each call."""
    name = "hstub"
    launches = []

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        wd = os.path.basename(workdir)
        s = dict(seat)
        for suffix, key in (("_hedge", "stub_hedge_mode"), ("_fallback", "stub_fallback_mode")):
            if wd.endswith(suffix) and key in seat:
                s["stub_mode"] = seat[key]
        HedgeStub.launches.append({"wd": wd, "opts": launch_opts, "limits": limits,
                                   "salvage": bool(seat.get("_salvage"))})
        launch = super().build(s, prompt_path, workdir, limits, cfg, launch_opts)
        launch.env["STUB_ROLE"] = wd
        return launch


class StreamHedgeStub(HedgeStub):
    name = "sstub"


class QuotaExtractor(providers.RawTextExtractor):
    """stderr `quota_429` (the stub CLI's unknown-mode line) -> quota_exhausted, the way AgyExtractor reads a 429."""

    def __init__(self, model=None):
        super().__init__(model)
        self.err = b""

    def feed_stderr(self, chunk):
        self.err += chunk

    def terminal_reason(self):
        return "quota_exhausted" if b"quota_429" in self.err else None

    def error(self):
        return "quota_exhausted: Individual quota reached (resets in 2h)" if self.terminal_reason() else None


class QuotaStub(HedgeStub):
    name = "qstub"

    def extractor(self, seat=None):
        return QuotaExtractor((seat or {}).get("model"))


class ChairProbe(providers.StubAdapter):
    """The chair: at its build (right before its launch) it records which stub calls are alive."""
    name = "cstub"
    seen = None

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        if seat.get("role") == "chair" and ChairProbe.seen is None:
            ChairProbe.seen = roles_alive(os.environ["STUB_PIDFILE"])
        return super().build(seat, prompt_path, workdir, limits, cfg, launch_opts)


def _state(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        return data[data.rfind(b")") + 2:].split()[0]
    except OSError:
        return None


def roles_alive(pidfile):
    """{role: alive} from the stub pid file (alive = exists and not a zombie)."""
    out = {}
    try:
        with open(pidfile) as f:
            for ln in f:
                if ln.strip():
                    role, pid = ln.split()
                    st = _state(int(pid))
                    out[role] = st is not None and st != b"Z"
    except OSError:
        pass
    return out


class FakeLive:
    """make_liveness stand-in (no real `ss`): a fixed verdict."""

    def __init__(self, progress=False, terminal=None):
        self.progress, self.terminal = progress, terminal

    def __call__(self, info=None):
        fake = self

        class V:
            terminal = fake.terminal
            terminal_source = "line" if fake.terminal else None

            def progress_recent(self, quiet_s):
                return fake.progress
        return V()

    sample = __call__                    # LivenessBoard calls seat.sample(info)


for _n in ("hstub", "sstub", "qstub", "cstub"):
    providers.FAMILIES.setdefault(_n, "stub")


def hseat(sid, mode="quick", hedge_mode=None, provider="hstub", **kw):
    d = seat(sid, mode, provider, **kw)
    if hedge_mode is not None:
        d["stub_hedge_mode"] = hedge_mode
    return d


class HedgeBase(Base):
    def setUp(self):
        super().setUp()
        for cls in (HedgeStub, StreamHedgeStub, QuotaStub, ChairProbe):
            providers.register(cls())
            self.addCleanup(providers._REGISTRY.pop, cls.name, None)
        HedgeStub.launches = []
        ChairProbe.seen = None
        self.lives = []

        def make(provider, tag, policy, sources, log_path):
            live = self.live_factory(len(self.lives))
            self.lives.append((provider, tag, policy, sources, log_path))
            return live
        self.live_factory = lambda i: FakeLive()
        p = mock.patch.object(council_run, "make_liveness", make)
        p.start()
        self.addCleanup(p.stop)

    def hcfg(self, seats, hedge=True, fallback=False, at=0.5, ceiling=4, salvage=False, **kw):
        cfg = self.cfg(seats, **kw)
        cfg["chair"] = {"provider": "cstub", "model": "stub-model", "stub_mode": "echo-stdin"}
        cfg["providers"].update({"hstub": {"mode": "buffered"}, "qstub": {"mode": "buffered"},
                                 "sstub": {"mode": "stream"}, "cstub": {"mode": "stream"}})
        cfg["timeouts"]["default"]["ceiling_s"] = ceiling
        cfg["hedge"] = {"enabled": hedge, "providers": ["hstub", "qstub"], "floor_s": at, "cold_s": at,
                        "cap_s": at, "factor": 2.5, "quiet_s": 120, "recheck_s": 0.1, "max_defer": 2.0}
        cfg["fallback"] = {"enabled": fallback}
        cfg["salvage"] = {"enabled": salvage, "timeout": 5, "chair_timeout": 5}
        return cfg

    def calls(self):
        try:
            with open(os.path.join(self.out, "calls.jsonl")) as f:
                return [json.loads(ln) for ln in f if ln.strip()]
        except OSError:
            return []

    def seat_json(self, sid):
        return json.loads(self.read("seats", sid + ".json"))

    def roles(self, sid):
        return {c["call"]: c["role"] for c in self.seat_json(sid)["calls"]}

    def wds(self):
        return [ln["wd"] for ln in HedgeStub.launches]

    def assert_no_survivors(self):
        time.sleep(0.3)
        with open(self.pidfile) as f:
            pids = [int(ln.split()[1]) for ln in f if ln.strip()]
        for pid in pids:
            self.assertIn(_state(pid), (None, b"Z"), "stub pid %d outlived the run" % pid)


class LimitsTests(unittest.TestCase):
    """Step 1: `_limits` kinds hedge / fallback (D29) and e1fd082's limits with [hedge]/[fallback] off (D25)."""

    def setUp(self):
        from councillib import config
        self.cfg = config.load()
        self.dir = tempfile.mkdtemp(prefix="lim30-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = os.path.join(self.dir, "latency.jsonl")

    def seat_of(self, provider):
        return next(dict(s) for s in self.cfg["seat"] if s["provider"] == provider)

    def test_h01_shipped_hedge_and_fallback_off_give_e1fd082_limits_for_every_launch(self):
        from councillib import config
        # row 34 ships the hedge and the fallback on, so the e1fd082 equality is checked with both switched off; row 35
        # removed codex's ceiling, so the copy also restores codex's e1fd082 timeouts (hedge off means restoring them)
        e1fd082_codex = {"timeouts": {"codex": {"base_timeout_s": 900, "cold_at_ceiling": True, "min_timeout_s": 3600,
                                                "ceiling_s": 3600}}}
        off = config.deep_merge(self.cfg, dict(e1fd082_codex, hedge={"enabled": False}, fallback={"enabled": False}))
        self.assertFalse(off["hedge"]["enabled"])
        self.assertFalse(off["fallback"]["enabled"])
        bare = {k: v for k, v in config.deep_merge(self.cfg, e1fd082_codex).items()
                if k not in ("hedge", "fallback", "liveness")}
        launches = [(s, k) for s in self.cfg["seat"] for k in ("seat", "salvage")]
        launches += [(dict(self.cfg["chair"], id="chair", role="chair"), k) for k in ("chair", "chair_salvage")]
        for s, kind in launches:
            got = council_run._limits(off, s, kind, self.log, 1000)
            want = council_run._limits(bare, s, kind, self.log, 1000)
            self.assertIs(type(got[0]), council_run.Limits, (s["id"], kind))
            self.assertEqual(got, want, (s["id"], kind))
            self.assertNotEqual(got[1], "watch")

    def test_h02_hedged_buffered_seat_runs_in_watch_mode_with_the_d12_soft_notice(self):
        cfg = dict(self.cfg, hedge=dict(self.cfg["hedge"], enabled=True))
        codex = self.seat_of("codex")
        lim, mode, deadline, source = council_run._limits(cfg, codex, "seat", self.log, 1000)
        self.assertIs(type(lim), council_run.WatchLimits)
        self.assertEqual((lim.mode, mode, deadline, lim.deadline_s), ("watch", "watch", None, None))
        self.assertIsNone(lim.ceiling_s)                           # row 35: no ceiling (D27; its own 3600 s before)
        self.assertEqual((lim.soft_at_s, source), (1200.0, "hedge_cold"))
        self.assertEqual(lim.sample_s, float(self.cfg["liveness"]["sample_s"]))
        hlim, hmode, _, _ = council_run._limits(cfg, codex, "hedge", self.log, 1000)
        self.assertEqual((hlim.mode, hmode, hlim.deadline_s, hlim.soft_at_s, hlim.ceiling_s),
                         ("watch", "watch", None, None, None))                    # row 35: no ceiling
        # agy is not in [hedge] providers (D46): its original keeps e1fd082's buffered deadline
        agy = self.seat_of("agy")
        self.assertEqual(council_run._limits(cfg, agy, "seat", self.log, 1000),
                         council_run._limits(self.cfg, agy, "seat", self.log, 1000))

    def test_h03_buffered_fallback_has_no_600s_deadline_and_stream_fallback_keeps_salvage_limits(self):
        for prov in ("codex", "agy"):
            fseat = providers.get(prov).salvage_seat(self.seat_of(prov), self.cfg, 8000)
            lim, mode, deadline, _ = council_run._limits(self.cfg, fseat, "fallback", self.log, 1000)
            self.assertEqual((lim.mode, mode, deadline, lim.deadline_s, lim.ceiling_s),
                             ("watch", "watch", None, None, None if prov == "codex" else 3600.0), prov)  # row 35
            sv = council_run._limits(self.cfg, fseat, "salvage", self.log, 1000)[0]
            self.assertEqual((sv.mode, sv.deadline_s),
                             ("buffered", council_run.salvage_cfg(self.cfg)["timeout"]))   # salvage unchanged
        claude = self.seat_of("claude")
        self.assertEqual(council_run._limits(self.cfg, claude, "fallback", self.log, 1000),
                         council_run._limits(self.cfg, claude, "salvage", self.log, 1000))
        chair = dict(self.cfg["chair"], id="chair-salvage")
        self.assertEqual(council_run._limits(self.cfg, chair, "chair_fallback", self.log, 1000),
                         council_run._limits(self.cfg, chair, "chair_salvage", self.log, 1000))

    def test_h04_hedge_at_is_clamped_p95_x_factor_or_cold(self):
        cfg = dict(self.cfg, hedge=dict(self.cfg["hedge"], enabled=True))
        self.assertEqual(council_run.hedge_at(cfg, self.log, "codex", "m", 1000), (1200.0, "cold"))
        for total, want in ((400, 1000.0), (100, 900.0), (1000, 1800.0)):
            with open(self.log, "w") as f:
                for _ in range(10):
                    f.write(json.dumps({"kind": "seat", "outcome": "ok", "provider": "codex", "model": "m",
                                        "total_s": total, "prompt_bytes": 1000}) + "\n")
            self.assertEqual(council_run.hedge_at(cfg, self.log, "codex", "m", 1000), (want, "p95"))

    def test_h05_drift_manifest_gets_a_hedge_launch_only_for_hedge_providers(self):
        from councillib import drift
        keys = []
        drift._manifest(self.cfg, [dict(s) for s in self.cfg["seat"]] + [dict(self.cfg["chair"], id="chair",
                                                                              role="chair")],
                        lambda k, fn, src: keys.append(k))
        hedge = sorted(k for k in keys if ".hedge." in k)
        self.assertEqual(hedge, ["codex.hedge.%s" % p for p in sorted(("argv", "env", "io", "limits", "policy"))])


class HedgeOffTests(HedgeBase):
    def test_h10_hedge_off_is_e1fd082_deadline_kill_then_salvage_with_no_launch_opts(self):
        seats = [seat("a"), seat("b"), hseat("x", "hung", hedge_mode="quick", stub_salvage_mode="quick")]
        cfg = self.hcfg(seats, hedge=False, salvage=True)
        cfg["timeouts"]["default"]["base_timeout_s"] = 1.5
        code = self.run_council(cfg)
        self.assertEqual(code, 0)
        x = self.seat_json("x")
        self.assertEqual((x["status"], x["salvaged_from"], x["mode"]), ("ok", "deadline_kill", "buffered"))
        self.assertNotIn("calls", x)
        self.assertNotIn("answered_by", x)
        self.assertEqual(self.wds(), ["x", "x_salvage"])
        for ln in HedgeStub.launches:
            self.assertIsNone(ln["opts"])                          # build() called exactly as in e1fd082
            self.assertIs(type(ln["limits"]), council_run.Limits)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.out, "calls.jsonl")))
        summ = self.summary()
        for k in ("pending", "late", "below_quorum", "unavailable"):
            self.assertNotIn(k, summ)
        self.assertEqual(self.lives, [])


class HedgeTests(HedgeBase):
    def test_h20_hedge_starts_at_soft_at_original_stays_alive_and_wins_by_precedence(self):
        seats = [seat("a", "slow-stream 3"), seat("b"), hseat("x", "busy-silent 2", hedge_mode="quick")]
        code = self.run_council(self.hcfg(seats, cut_stream=False))     # the chair waits for a (3 s)
        self.assertEqual(code, 0)
        calls = self.calls()
        self.assertEqual([(c["call"], "status" in c) for c in calls], [("hedge", False), ("hedge", True)])
        self.assertGreaterEqual(calls[0]["after_s"], 0.45)
        self.assertLess(calls[0]["after_s"], 1.5)
        x = self.seat_json("x")
        self.assertEqual((x["status"], x["answered_by"], x["mode"]), ("ok", "original", "watch"))
        self.assertEqual(self.roles("x"), {"original": "used", "hedge": "alternate"})
        orig = x["calls"][0]
        self.assertEqual((orig["status"], orig["exit_code"], orig["truncated"]), ("ok", 0, False))
        self.assertEqual(self.summary()["quorum"]["ok"], 3)       # one answer per seat
        self.assertIn("DONE", self.read("seats", "x.md"))
        self.assertIn("QUICK-OK", self.read("seats", "x.hedge.md"))
        self.assertIn("QUICK-OK", self.read("seats", "x.hedge.raw"))   # the hedge's capture file, kept
        hedge_launch = next(ln for ln in HedgeStub.launches if ln["wd"] == "x_hedge")
        self.assertEqual(hedge_launch["opts"]["tag"], calls[0]["tag"])
        self.assertIs(type(hedge_launch["limits"]), council_run.WatchLimits)
        self.assert_no_survivors()

    def test_h21_hedge_answers_first_chair_goes_original_is_late_not_killed(self):
        seats = [seat("a"), seat("b"), hseat("x", "busy-silent 2.5", hedge_mode="quick")]
        code = self.run_council(self.hcfg(seats))
        self.assertEqual(code, 0)
        self.assertGreaterEqual(self.wall, 2.4, "the run waits for the original; it is never killed")
        x = self.seat_json("x")
        self.assertEqual((x["status"], x["answered_by"]), ("ok", "hedge"))
        self.assertEqual(self.roles("x"), {"original": "late", "hedge": "used"})
        self.assertEqual(x["calls"][0]["status"], "ok")
        self.assertIn("QUICK-OK", self.read("chair", "synthesis.md").split("seat x")[1])
        late = self.summary()["late"]
        self.assertEqual([(e["seat"], e["call"]) for e in late], [("x", "original")])
        self.assertIn("DONE", self.read("seats", "x.original.late.md"))
        self.assert_no_survivors()

    def test_h22_d39_two_silent_calls_chair_proceeds_seat_pending_nothing_killed(self):
        seats = [seat("a"), seat("b"), hseat("x", "hung", hedge_mode="hung")]
        code = self.run_council(self.hcfg(seats, ceiling=3))
        self.assertEqual(code, 0)
        self.assertEqual(ChairProbe.seen.get("x"), True, ChairProbe.seen)
        self.assertEqual(ChairProbe.seen.get("x_hedge"), True, ChairProbe.seen)
        summ = self.summary()
        self.assertEqual(summ["pending"], ["x"])
        self.assertNotIn("below_quorum", summ)
        x = self.seat_json("x")
        self.assertEqual((x["status"], x["pending"]), ("pending", True))
        self.assertIn("status pending", self.read("chair", "synthesis.md"))
        self.assertIn("PENDING", self.read("seats", "x.md"))
        # neither call was killed or cut by the scheduler: both ran to their own ceiling (D29, until row 35)
        sts = {c["call"]: c["status"] for c in x["calls"]}
        self.assertEqual(sts, {"original": "ceiling_kill", "hedge": "ceiling_kill"})
        self.assertNotIn("cut", json.dumps(x))
        self.assertEqual(x["outcome"], "unresolved")
        self.assertGreaterEqual(self.wall, 2.9)
        self.assert_no_survivors()

    def test_h23_d42_short_of_quorum_with_a_pending_seat_synthesises_below_quorum(self):
        seats = [seat("a"), hseat("x", "hung", hedge_mode="hung")]
        code = self.run_council(self.hcfg(seats, min_ok="n", ceiling=2.5))
        self.assertEqual(code, 0)
        summ = self.summary()
        self.assertEqual((summ["below_quorum"], summ["pending"], summ["quorum"]["ok"]), (True, ["x"], 1))
        syn = self.read("chair", "synthesis.md")
        self.assertTrue(syn.startswith("> BELOW QUORUM: 1 of 2"), syn[:80])
        self.assertIn("pending seats: x", syn)
        self.assertEqual(ChairProbe.seen.get("x_hedge"), True)
        self.assert_no_survivors()

    def test_h24_short_of_quorum_without_a_pending_seat_is_still_exit_3(self):
        seats = [seat("a"), hseat("x", "exit 3", hedge_mode="exit 4")]
        code = self.run_council(self.hcfg(seats, min_ok="n"))
        self.assertEqual(code, 3)
        self.assertEqual(self.seat_json("x")["status"], "unresolved")
        self.assertFalse(os.path.exists(os.path.join(self.out, "chair")))

    def test_h25_d12_progress_defers_the_hedge_up_to_max_defer(self):
        self.live_factory = lambda i: FakeLive(progress=True)
        seats = [seat("a"), seat("b"), hseat("x", "busy-silent 1.6", hedge_mode="quick")]
        cfg = self.hcfg(seats)
        cfg["liveness"] = {"sample_s": 0.1}
        code = self.run_council(cfg)
        self.assertEqual(code, 0)
        start = [c for c in self.calls() if c["call"] == "hedge" and "status" not in c][0]
        self.assertGreaterEqual(start["after_s"], 0.95)          # 2 x hedge_at, not hedge_at
        x = self.seat_json("x")
        self.assertGreaterEqual(x["hedge_deferred"], 2)
        self.assertEqual(self.lives[0][0], "hstub")               # the original was registered on the board
        self.assert_no_survivors()

    def test_h26_proven_terminal_verdict_reaps_the_call_and_the_hedge_starts_at_once(self):
        self.live_factory = lambda i: FakeLive(terminal="cli_idle_timeout" if i == 0 else None)
        seats = [seat("a"), seat("b"), hseat("x", "hung", hedge_mode="quick")]
        cfg = self.hcfg(seats, at=3)
        cfg["liveness"] = {"sample_s": 0.1}
        code = self.run_council(cfg)
        self.assertEqual(code, 0)
        x = self.seat_json("x")
        self.assertEqual(x["calls"][0]["status"], "cli_idle_timeout")
        self.assertEqual((x["status"], x["answered_by"]), ("ok", "hedge"))
        self.assertLess(self.wall, 2.5, "the hedge started when the original ended, not at hedge_at")
        self.assert_no_survivors()


class FallbackTests(HedgeBase):
    def test_h30_d24_fallback_after_original_and_hedge_both_end_with_no_answer(self):
        seats = [seat("a"), seat("b"),
                 hseat("x", "exit 3", hedge_mode="exit 4", stub_salvage_mode="exit 9", stub_fallback_mode="quick")]
        code = self.run_council(self.hcfg(seats, fallback=True))
        self.assertEqual(code, 0)
        x = self.seat_json("x")
        self.assertEqual((x["status"], x["answered_by"], x["detail"]), ("ok", "fallback", "fallback"))
        self.assertEqual(self.roles("x"), {"original": "failed", "hedge": "failed", "fallback": "used"})
        self.assertEqual(self.wds().count("x_fallback"), 1)
        self.assertNotIn("x_salvage", self.wds())                 # a hedged seat's second call is its hedge
        seq = [(c["call"], c.get("status")) for c in self.calls()]
        self.assertEqual(seq, [("hedge", None), ("hedge", "exit_4"), ("fallback", None), ("fallback", "ok")])
        fb = next(ln for ln in HedgeStub.launches if ln["wd"] == "x_fallback")
        self.assertTrue(fb["salvage"])                            # the salvage form (D14)
        self.assertEqual((fb["limits"].mode, fb["limits"].deadline_s), ("watch", None))   # no 600 s kill (D29)
        self.assertIn("SALVAGED", self.read("chair", "synthesis.md").split("seat x")[1])

    def test_h31_no_fallback_while_a_call_still_works_then_it_runs_and_is_late(self):
        seats = [seat("a"), seat("b"), hseat("x", "hung", hedge_mode="exit 4", stub_fallback_mode="quick")]
        code = self.run_council(self.hcfg(seats, fallback=True, ceiling=2.5))
        self.assertEqual(code, 0)
        self.assertNotIn("x_fallback", ChairProbe.seen)           # not started while the original lived
        self.assertTrue(ChairProbe.seen.get("x"))
        summ = self.summary()
        self.assertEqual(summ["pending"], ["x"])
        self.assertEqual([(e["seat"], e["call"]) for e in summ["late"]], [("x", "fallback")])
        x = self.seat_json("x")
        self.assertEqual((x["status"], x["outcome"]), ("pending", "late"))
        self.assertEqual(x["calls"][0]["status"], "ceiling_kill")
        self.assertIn("QUICK-OK", self.read("seats", "x.fallback.late.md"))
        self.assert_no_survivors()

    def test_h32_fallback_off_means_unresolved_and_no_third_call(self):
        seats = [seat("a"), seat("b"), hseat("x", "exit 3", hedge_mode="exit 4", stub_fallback_mode="quick")]
        code = self.run_council(self.hcfg(seats, fallback=False))
        self.assertEqual(code, 0)
        self.assertEqual(self.seat_json("x")["status"], "unresolved")
        self.assertNotIn("x_fallback", self.wds())

    def test_h33_stream_seat_original_salvage_then_one_fallback(self):
        seats = [seat("a"), seat("b"),
                 seat("s", "partial-then-hang", "sstub", stub_salvage_mode="exit 1", stub_fallback_mode="quick")]
        code = self.run_council(self.hcfg(seats, fallback=True, salvage=True, idle=1, cut_stream=False))
        self.assertEqual(code, 0)
        s = self.seat_json("s")
        self.assertEqual((s["status"], s["answered_by"], s["salvaged_from"]), ("ok", "fallback", "idle_kill"))
        self.assertEqual(s["salvage"]["status"], "exit_1")
        self.assertEqual(self.wds(), ["s", "s_salvage", "s_fallback"])
        fb = HedgeStub.launches[-1]
        self.assertEqual(fb["limits"].mode, "stream")             # stream fallbacks keep salvage limits (D3)
        self.assertIsNone(fb["opts"])
        self.assertEqual([c["call"] for c in self.calls()], ["fallback", "fallback"])

    def test_h34_chair_chair_salvage_then_fallback_else_exit_4(self):
        seats = [seat("a"), seat("b")]
        cfg = self.hcfg(seats, fallback=True, salvage=True, idle=1)
        cfg["chair"] = {"provider": "sstub", "model": "stub-model", "stub_mode": "partial-then-hang",
                        "stub_salvage_mode": "exit 1", "stub_fallback_mode": "echo-stdin"}
        code = self.run_council(cfg)
        self.assertEqual(code, 0)
        chair = self.summary()["chair"]
        self.assertEqual((chair["status"], chair["answered_by"], chair["salvaged_from"]),
                         ("ok", "fallback", "idle_kill"))
        self.assertIn("SALVAGED from status=idle_kill", self.read("chair", "synthesis.md"))
        self.assertEqual(self.wds(), ["chair", "chair_salvage", "chair_fallback"])
        self.assertTrue(os.path.exists(os.path.join(self.out, "chair", "fallback.raw")))
        HedgeStub.launches = []
        cfg["chair"]["stub_fallback_mode"] = "exit 2"
        shutil.rmtree(self.out)
        self.assertEqual(self.run_council(cfg), 4)
        self.assertEqual(self.summary()["chair"]["status"], "unresolved")


class RestartTests(HedgeBase):
    def seed(self, *calls):
        os.makedirs(self.out, mode=0o700)
        with open(os.path.join(self.out, "calls.jsonl"), "w") as f:
            for c in calls:
                f.write(json.dumps({"seat": "x", "call": c, "key": "x~" + c, "tag": "t-" + c,
                                    "started_at": "2026-10-21T00:00:00Z"}) + "\n")

    def test_h40_recorded_hedge_and_fallback_are_never_started_again_and_are_observer_lost(self):
        self.seed("hedge", "fallback")
        seats = [seat("a"), seat("b"), hseat("x", "exit 3", hedge_mode="quick", stub_fallback_mode="quick")]
        cfg = self.hcfg(seats, fallback=True)
        self.assertEqual(self.run_council(cfg), 0)
        self.assertEqual(self.wds(), ["x"])                       # no second hedge, no second fallback
        lost = [(c["call"], c["status"]) for c in self.calls() if c.get("status")]
        self.assertEqual(lost, [("fallback", "observer_lost"), ("hedge", "observer_lost")])
        x = self.seat_json("x")
        self.assertEqual(x["status"], "unresolved")
        self.assertEqual({c["call"]: c["role"] for c in x["calls"] if c["role"] == "observer_lost"},
                         {"hedge": "observer_lost", "fallback": "observer_lost"})
        self.assertEqual(self.run_council(cfg), 0)                # a third run(): still none, no duplicate marks
        self.assertEqual(self.wds(), ["x", "x"])
        self.assertEqual(len([c for c in self.calls() if c.get("status") == "observer_lost"]), 2)

    def test_h41_recorded_hedge_only_still_allows_the_one_fallback(self):
        self.seed("hedge")
        seats = [seat("a"), seat("b"), hseat("x", "exit 3", hedge_mode="quick", stub_fallback_mode="quick")]
        self.assertEqual(self.run_council(self.hcfg(seats, fallback=True)), 0)
        self.assertEqual(self.wds(), ["x", "x_fallback"])
        self.assertEqual(self.seat_json("x")["answered_by"], "fallback")


class QuotaAndTerminalStatusTests(HedgeBase):
    def test_h50_quota_exhausted_no_salvage_hedge_or_fallback_and_recorded_for_the_next_run(self):
        seats = [seat("a"), seat("b"), hseat("x", "quota_429", provider="qstub", hedge_mode="quick",
                                            stub_salvage_mode="quick", stub_fallback_mode="quick")]
        cfg = self.hcfg(seats, fallback=True, salvage=True)
        t = time.time()
        self.assertEqual(self.run_council(cfg), 0)
        x = self.seat_json("x")
        self.assertEqual(x["calls"][0]["status"], "quota_exhausted")
        self.assertEqual(self.wds(), ["x"])
        q = providers.read_quota(cfg)["qstub"]
        until = providers._iso_epoch(q["until"])
        self.assertTrue(t + 7200 - 5 <= until <= time.time() + 7200 + 5, q)
        shutil.rmtree(self.out)
        self.assertEqual(self.run_council(cfg), 0)                # a, b answer; x is not launched
        self.assertEqual(self.wds(), ["x"])
        summ = self.summary()
        self.assertEqual(self.by_id(summ)["x"]["status"], "skipped_unavailable")
        self.assertEqual(summ["unavailable"]["x"]["until"], q["until"])
        self.assertIn("skipped_unavailable", self.read("chair", "synthesis.md"))

    def test_h51_stderr_terminal_reason_is_the_status_and_cli_timeout_with_a_partial_is_salvaged(self):
        class CliTimeoutExtractor(providers.RawTextExtractor):
            def __init__(self, model=None):
                super().__init__(model)
                self.seen = False

            def feed_stderr(self, chunk):
                self.seen = self.seen or b"print_timeout" in chunk

            def terminal_reason(self):
                return "cli_timeout" if self.seen else None

            def error(self):
                return "cli_timeout: [agy] print timeout after 1s" if self.seen else None

            def partial_text(self):
                return "PARTIAL-CLI" if self.seen else super().partial_text()

        class CliTimeoutStub(providers.StubAdapter):
            name = "tstub"

            def extractor(self, seat=None):
                seat = seat or {}
                return (providers.RawTextExtractor(seat.get("model")) if seat.get("_salvage")
                        else CliTimeoutExtractor(seat.get("model")))
        providers.FAMILIES.setdefault("tstub", "stub")
        providers.register(CliTimeoutStub())
        self.addCleanup(providers._REGISTRY.pop, "tstub", None)
        seats = [seat("a"), seat("b"), seat("t", "print_timeout", "tstub", stub_salvage_mode="quick")]
        cfg = self.hcfg(seats, salvage=True)
        cfg["providers"]["tstub"] = {"mode": "buffered"}
        self.assertEqual(self.run_council(cfg), 0)
        t = self.by_id(self.summary())["t"]
        self.assertEqual((t["status"], t["salvaged_from"]), ("ok", "cli_timeout"))   # was adapter_error/exit_2


class SinkTests(HedgeBase):
    def test_h60_codex_style_otel_signal_gets_a_per_call_sink_port_closed_at_the_end(self):
        import socket
        seats = [seat("a"), seat("b"), hseat("x", "busy-silent 1.2", hedge_mode="quick")]
        cfg = self.hcfg(seats)
        cfg["providers"]["hstub"]["progress_signal"] = "otel"
        cfg["liveness"] = {"signals": True, "sample_s": 0.1}
        self.assertEqual(self.run_council(cfg), 0)
        ports = [ln["opts"]["otlp_port"] for ln in HedgeStub.launches]
        self.assertEqual(len(ports), 2)
        self.assertEqual(len(set(ports)), 2)                      # one sink per call
        self.assertTrue(all(isinstance(p, int) and p > 0 for p in ports))
        self.assertTrue(all("otlp_events" in src for _, _, _, src, _ in self.lives))
        for p in ports:
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", p), timeout=0.5).close()


# ---- row 31: runner lifetime, late originals, launcher split, D50, detached watch calls --------------------

RUNNER_HARNESS = """import json, os, sys
sys.path.insert(0, %(root)r)
import council
from councillib import council_run, providers
from tests import test_council_run as T
for cls in (T.HedgeStub, T.StreamHedgeStub, T.ChairProbe):
    providers.register(cls())
council_run.make_liveness = lambda *a: T.FakeLive()
cfg = json.load(open(sys.argv[1]))
fd = int(sys.argv[sys.argv.index("--status-fd") + 1]) if "--status-fd" in sys.argv else None
sys.exit(council.runner_main(cfg, "Q?", [], sys.argv[2], [], (), fd))
"""

LAUNCHER = """import json, sys
sys.path.insert(0, %(root)r)
import council
from councillib import council_run, providers
from tests import test_council_run as T
for cls in (T.HedgeStub, T.StreamHedgeStub, T.ChairProbe):
    providers.register(cls())
council_run.make_liveness = lambda *a: T.FakeLive()
cfg = json.load(open(sys.argv[1]))
out = sys.argv[2]
sys.exit(council.launch(lambda fd: council.runner_main(cfg, "Q?", [], out, [], (), fd), out))
"""


class ProgLive(FakeLive):
    """A liveness fake with the Verdict fields seats/<id>.json `progress` reads."""

    def __call__(self, info=None):
        class V:
            terminal = None
            terminal_source = None
            progress_signal = "otel"
            progress_at = 0.3
            samples = 4
            keepalives = 1
            elapsed_s = 0.4

            def progress_recent(self, quiet_s):
                return False
        return V()

    sample = __call__


def _ppid(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        return int(data[data.rfind(b")") + 2:].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def _gone(pid):
    return _state(pid) in (None, b"Z")


def _wait(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


class RunnerTests(HedgeBase):
    """Row 31 (D18/D39/D50): the runner keeps supervising after the caller's answer; nothing is killed."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self.kill_stubs)

    def kill_stubs(self):
        """Detached stubs a failed assertion could leave: kill them by our own pids only."""
        for role, pid in self.stub_pids():
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

    def harness(self, cfg):
        cfg_path = os.path.join(self.tmp, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        runner = os.path.join(self.tmp, "runner_harness.py")
        with open(runner, "w") as f:
            f.write(RUNNER_HARNESS % {"root": ROOT})
        os.makedirs(self.out, mode=0o700, exist_ok=True)
        return [PY, "-Es", runner, cfg_path, self.out]

    def launch(self, cfg, timeout=15):
        """Run the launcher as a process; returns (rc, stdout, wall). communicate() returns at stdout EOF, so
        the runner must not hold the launcher's stdout."""
        launcher = os.path.join(self.tmp, "launcher.py")
        with open(launcher, "w") as f:
            f.write(LAUNCHER % {"root": ROOT})
        t = time.monotonic()
        proc = subprocess.Popen([PY, "-Es", launcher, *self.harness(cfg)[3:]], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dict(os.environ),
                                cwd=self.tmp)
        try:
            out, _ = proc.communicate(timeout=timeout)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return proc.returncode, out.decode("utf-8", "replace"), time.monotonic() - t

    def wait_runner_done(self, timeout=10):
        self.assertTrue(_wait(lambda: os.path.exists(os.path.join(self.out, "summary.json"))
                              and self.summary().get("phase") == "done", timeout),
                        "the runner never finished: %s" % self.read_log())
        pid = self.summary()["runner_pid"]
        self.assertTrue(_wait(lambda: _gone(pid), 5), "runner %d still alive" % pid)

    def read_log(self):
        try:
            return self.read("runner.log")[-2000:]
        except OSError:
            return ""

    # step 1 ---------------------------------------------------------------------------------------------
    def test_r01_late_original_is_appended_with_a_late_footer_and_the_chair_does_not_rerun(self):
        self.live_factory = lambda i: ProgLive()
        seats = [seat("a"), seat("b"), hseat("x", "busy-silent 1.8", hedge_mode="quick")]
        cfg = self.hcfg(seats)
        cfg["liveness"] = {"sample_s": 0.1}
        seen = []

        def on_ready(code, info):
            seen.append((code, dict(info), os.path.isdir(info["work_root"]),
                         roles_alive(self.pidfile).get("x"), time.monotonic()))
        t = time.monotonic()
        code = council_run.run(cfg, "Q?", [], self.out, on_ready=on_ready)
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1, "on_ready fires once")
        rcode, info, wd_there, x_alive, at = seen[0]
        self.assertEqual(rcode, 0)
        self.assertLess(at - t, 1.6, "ready at synthesis, before the late call ended")
        self.assertTrue(wd_there and x_alive, "workdir and the late call are still there at ready")
        self.assertEqual(info["late_pending"], [{"seat": "x", "call": "original"}])
        self.assertEqual(info["synthesis"], os.path.join(self.out, "chair", "synthesis.md"))
        self.assertFalse(os.path.isdir(info["work_root"]), "workdir removed once the last call ended")
        summ = self.summary()
        self.assertEqual((summ["phase"], summ["runner_pid"]), ("done", os.getpid()))
        self.assertNotIn("late_pending", summ)
        self.assertEqual([(e["seat"], e["call"]) for e in summ["late"]], [("x", "original")])
        syn = self.read("chair", "synthesis.md")
        self.assertEqual(syn.count("CHAIR\n"), 1, "the chair ran once")
        self.assertRegex(syn, r"> LATE: seat x \(original\) answered at \d+\.\d s after synthesis; "
                              r"not used by the chair\n$")
        self.assertIn("DONE", self.read("seats", "x.original.late.raw"))
        self.assertIn("## original (late", self.read("seats", "x.late.md"))
        x = self.seat_json("x")
        self.assertEqual(x["progress"]["signal"], "otel")
        self.assertEqual((x["progress"]["last_progress_s"], x["progress"]["events"]), (0.3, 4))
        self.assert_no_survivors()

    def test_r02_exception_exit_path_never_kills_a_watch_call(self):
        seats = [seat("a"), seat("b"), hseat("x", "busy-silent 1.5", hedge_mode="hung")]
        cfg = self.hcfg(seats, ceiling=2.5)
        boom = mock.patch.object(council_run, "_answers", side_effect=RuntimeError("boom"))
        boom.start()
        self.addCleanup(boom.stop)
        t = time.monotonic()
        with self.assertRaises(RuntimeError):
            council_run.run(cfg, "Q?", [], self.out)
        self.assertGreaterEqual(time.monotonic() - t, 2.3, "the exit path waited for the watch calls")
        self.assertFalse(os.path.exists(os.path.join(self.out, "workdir-kept.txt")))
        self.assert_no_survivors()

    # step 2: launcher -------------------------------------------------------------------------------------
    def test_r10_launcher_returns_at_synthesis_and_the_pending_seat_survives_it(self):
        seats = [seat("a"), seat("b"), hseat("x", "busy-silent 2.6", hedge_mode="hung")]
        rc, out, wall = self.launch(self.hcfg(seats, ceiling=3.4))
        self.assertEqual(rc, 0, out + self.read_log())
        alive = roles_alive(self.pidfile)
        self.assertLess(wall, 2.4, "the launcher returned before the late call ended")
        self.assertTrue(alive.get("x") and alive.get("x_hedge"), alive)
        summ = self.summary()
        pid = summ["runner_pid"]
        self.assertIn("synthesis ready: %s" % os.path.join(self.out, "chair", "synthesis.md"), out)
        self.assertIn("pending seats: x (runner %d keeps supervising)" % pid, out)
        self.assertIn("run dir: %s\nexit: 0\n" % self.out, out)
        self.assertEqual(summ["pending"], ["x"])
        self.wait_runner_done()
        summ = self.summary()
        self.assertEqual([(e["seat"], e["call"]) for e in summ["late"]], [("x", "original")])
        self.assertIn("> LATE: seat x (original)", self.read("chair", "synthesis.md"))
        sts = {c["call"]: c["status"] for c in self.seat_json("x")["calls"]}
        self.assertEqual(sts, {"original": "ok", "hedge": "ceiling_kill"})   # own ceiling (until row 35)
        self.assert_no_survivors()

    def test_r11_d50_zero_answers_launcher_exits_4_pending_and_the_chair_runs_late(self):
        seats = [seat("a", "exit 3"), hseat("x", "busy-silent 2.2", hedge_mode="hung")]
        rc, out, wall = self.launch(self.hcfg(seats, ceiling=3.0))
        self.assertEqual(rc, 4, out + self.read_log())
        self.assertLess(wall, 2.0, "D50: the launcher does not wait for the first answer")
        self.assertIn("pending: no answers yet\n", out)
        self.assertRegex(out, r"pending seats: x \(runner \d+ keeps supervising\)")
        self.assertNotIn("synthesis ready", out)
        self.assertEqual(self.summary()["late_synthesis"]["status"], "waiting")
        self.assertTrue(roles_alive(self.pidfile).get("x"))
        self.wait_runner_done()
        summ = self.summary()
        self.assertEqual(summ["exit_code"], 4)
        ls = summ["late_synthesis"]
        self.assertEqual((ls["status"], ls["md"]), ("ok", "chair/synthesis.md"))
        self.assertGreater(ls["at_s"], 2.0)
        syn = self.read("chair", "synthesis.md")
        self.assertTrue(syn.startswith("> LATE SYNTHESIS:"), syn[:80])
        self.assertIn("DONE", syn)
        self.assertEqual(self.seat_json("x")["answered_by"], "original")
        self.assert_no_survivors()

    # D59 (row 47): closing the session that started the council never stops the runner or its calls --------
    def close_cfg(self):
        """A stream seat, a buffered seat and a watch (hedged) seat, all answering in about 2 s, so the close
        lands before the synthesis."""
        seats = [seat("a", "slow-stream 2", provider="sstub"), seat("b", "busy-silent 1.2"),
                 hseat("x", "busy-silent 1.5", hedge_mode="hung")]
        return self.hcfg(seats, ceiling=30, at=20)

    def start_launcher(self, cfg, **kw):
        launcher = os.path.join(self.tmp, "launcher.py")
        with open(launcher, "w") as f:
            f.write(LAUNCHER % {"root": ROOT})
        opts = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=dict(os.environ), cwd=self.tmp)
        opts.update(kw)
        proc = subprocess.Popen([PY, "-Es", launcher, *self.harness(cfg)[3:]], **opts)
        self.addCleanup(self.reap_proc, proc)
        return proc

    @staticmethod
    def reap_proc(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    def wait_calls_started(self):
        self.assertTrue(_wait(lambda: self.pid_of("a") and self.pid_of("x") and len(self.stub_pids()) >= 3, 10),
                        "the calls never started: %s" % self.stub_pids())
        time.sleep(0.2)
        self.assertFalse(os.path.exists(os.path.join(self.out, "chair", "synthesis.md")), "closed too late")
        return self.runner_pid_of(self.pid_of("x"))

    @staticmethod
    def runner_pid_of(stub):
        """stub -> its reaper -> the runner (before ready summary.json has no runner_pid yet)."""
        return _ppid(_ppid(stub))

    def assert_completed_after_close(self, runner):
        pids = dict(self.stub_pids())
        self.wait_runner_done(timeout=15)
        summ = self.summary()
        self.assertEqual(summ["runner_pid"], runner)
        self.assertEqual(summ["exit_code"], 0, summ)
        self.assertFalse(summ.get("interrupted"), "a closed session is not an operator stop (D59)")
        self.assertEqual({s["id"]: s["status"] for s in summ["seats"]}, {"a": "ok", "b": "ok", "x": "ok"})
        self.assertIn("CHAIR", self.read("chair", "synthesis.md"))
        self.assertEqual(dict(self.stub_pids()).get("a"), pids.get("a"), "nothing was relaunched")
        self.assert_no_survivors()

    def test_r12_launcher_killed_before_synthesis_the_runner_completes(self):
        proc = self.start_launcher(self.close_cfg())
        runner = self.wait_calls_started()
        proc.kill()
        proc.wait()
        self.assertFalse(_gone(runner))
        self.assertNotEqual(_ppid(runner), proc.pid, "the runner is not the launcher's child (D59)")
        self.assertTrue(all(not _gone(p) for _, p in self.stub_pids()), "a call died with the launcher")
        self.assert_completed_after_close(runner)

    def test_r15_term_to_the_launchers_group_is_not_forwarded(self):
        proc = self.start_launcher(self.close_cfg(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        runner = self.wait_calls_started()
        os.killpg(proc.pid, signal.SIGTERM)              # a tool host's group kill of the closing command
        out, _ = proc.communicate(timeout=10)
        out = out.decode()
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, out)
        self.assertIn("launcher stopped by signal %d before the synthesis; runner %d keeps running with its calls "
                      "(D59)" % (signal.SIGTERM, runner), out)
        self.assertIn("kill -TERM %d" % runner, out)
        self.assertIn("run dir: %s\nexit: 143\n" % self.out, out)
        self.assert_completed_after_close(runner)

    def test_r16_terminal_hangup_before_synthesis_the_runner_completes(self):
        import pty
        launcher = os.path.join(self.tmp, "launcher.py")
        with open(launcher, "w") as f:
            f.write(LAUNCHER % {"root": ROOT})
        argv = [PY, "-Es", launcher, *self.harness(self.close_cfg())[3:]]
        pid, master = pty.fork()                         # the launcher leads a session with this pty as its tty
        if pid == 0:
            try:
                os.chdir(self.tmp)
                os.execv(PY, argv)
            finally:
                os._exit(127)
        try:
            runner = self.wait_calls_started()
            os.close(master)                             # the terminal closes: SIGHUP to its session
            master = None
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 128 + signal.SIGHUP)
        finally:
            if master is not None:
                os.close(master)
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
        self.assert_completed_after_close(runner)

    def test_r17_a_tool_hosts_descendant_tree_kill_misses_the_runner(self):
        # Claude Code SIGKILLs a closing command's descendants (a ppid-tree walk) and their process groups.
        proc = self.start_launcher(self.close_cfg(), start_new_session=True)
        runner = self.wait_calls_started()
        tree, todo = set(), [proc.pid]
        while todo:
            parent = todo.pop()
            tree.add(parent)
            for name in os.listdir("/proc"):
                if name.isdigit() and int(name) not in tree and _ppid(int(name)) == parent:
                    todo.append(int(name))
        self.assertEqual(tree, {proc.pid}, "the runner, its reapers and calls are out of the launcher's tree")
        mine = os.getpgrp()
        for pid in tree:
            try:
                g = os.getpgid(pid)
                if g != mine:
                    os.killpg(g, signal.SIGKILL)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait()
        self.assert_completed_after_close(runner)

    def test_r18_stdout_reader_gone_and_hup_to_the_runner_change_nothing(self):
        proc = self.start_launcher(self.close_cfg(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        proc.stdout.close()                              # the caller stops reading (its pipe closes)
        runner = self.wait_calls_started()
        os.kill(runner, signal.SIGHUP)                   # a hang-up is not an operator stop (D59)
        self.assertEqual(proc.wait(timeout=15), 0, "the launcher's report to a gone reader is not a failure")
        self.assert_completed_after_close(runner)

    def test_r12b_a_stop_records_the_real_end_of_a_call_while_the_seat_itself_is_already_done(self):
        # row 41: x's original and hedge failed (the seat is done) and its fallback hangs; the operator's TERM ends
        # the fallback. The seat json and calls.jsonl must show it ended (signal_kill, not running) and the orphan
        # sweep (row 40) must see no live call.
        from councillib import preflight
        seats = [seat("a"), seat("b"), hseat("x", "exit 3", hedge_mode="exit 4", stub_fallback_mode="hung")]
        cfg = self.hcfg(seats, fallback=True, ceiling=60, at=0.4)
        launcher = os.path.join(self.tmp, "launcher.py")
        with open(launcher, "w") as f:
            f.write(LAUNCHER % {"root": ROOT})
        proc = subprocess.Popen([PY, "-Es", launcher, *self.harness(cfg)[3:]], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dict(os.environ), cwd=self.tmp)
        try:
            self.assertTrue(_wait(lambda: roles_alive(self.pidfile).get("x_fallback"), 15),
                            "the fallback never started: %s" % self.stub_pids())
            time.sleep(0.2)
            runner = self.runner_pid_of(self.pid_of("x_fallback"))
            os.kill(runner, signal.SIGTERM)                # D53: the owner's stop (the launcher's TERM no longer is, D59)
            out, _ = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, out.decode())
        self.assertIn("runner exited with 143 before the synthesis", out.decode())
        self.assertTrue(_wait(lambda: all(_gone(p) for _, p in self.stub_pids()), 6), self.stub_pids())
        self.assertTrue(self.summary().get("interrupted"))
        x = self.seat_json("x")
        by = {c["call"]: c for c in x["calls"]}
        self.assertEqual({k: v["status"] for k, v in by.items()},
                         {"original": "exit_3", "hedge": "exit_4", "fallback": "signal_kill"})
        self.assertEqual([c["running"] for c in x["calls"]], [False] * 3)
        self.assertEqual(by["fallback"]["signal"], signal.SIGTERM)
        self.assertNotIn("signal", by["hedge"])
        ends = {c["call"]: c for c in self.calls() if "status" in c}
        self.assertEqual({k: v["status"] for k, v in ends.items()}, {"hedge": "exit_4", "fallback": "signal_kill"})
        self.assertEqual(ends["fallback"]["signal"], signal.SIGTERM)
        self.assertIn("exit_code", ends["fallback"])
        self.assertNotIn("late_pending", self.summary())
        wdir = council_run.watch_dir(self.state)
        self.assertEqual([n for n in os.listdir(wdir) if not n.endswith(".end.json")] if os.path.isdir(wdir) else [], [])
        self.assertEqual(preflight.sweep_orphans(cfg), {"live": [], "swept": [], "errors": [], "runner_alive": 0})
        self.assert_no_survivors()

    def test_r13_runner_kill9_watch_call_survives_then_nothing_outlives_it(self):
        seats = [seat("a", "hung", provider="sstub"), hseat("x", "busy-silent 1.6", hedge_mode="hung")]
        cfg = self.hcfg(seats, ceiling=30, at=20)
        proc = subprocess.Popen(self.harness(cfg), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=dict(os.environ), cwd=self.tmp)
        try:
            self.assertTrue(_wait(lambda: self.pid_of("x") and self.pid_of("a"), 10))
            a, x = self.pid_of("a"), self.pid_of("x")
            reaper = _ppid(x)
            self.assertNotEqual(reaper, proc.pid)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertTrue(_wait(lambda: _gone(a), 2), "a non-watch call dies with the runner (orphan property)")
        self.assertFalse(_gone(x), "the watch call survives the runner (D18)")
        self.assertFalse(_gone(reaper))
        self.assertTrue(_wait(lambda: _gone(x) and _gone(reaper), 4), "it ends on its own terminal state")
        self.assert_no_survivors()

    def test_r14_detached_reaper_sweeps_group_and_tagged_strays_when_its_cli_ends(self):
        info = os.path.join(self.tmp, "pids.json")
        cli = os.path.join(self.tmp, "cli.py")
        with open(cli, "w") as f:
            f.write("import json, os, subprocess, sys, time\n"
                    "hang = [sys.executable, '-c', 'import time\\nwhile True: time.sleep(60)']\n"
                    "g = subprocess.Popen(hang)\n"
                    "s = subprocess.Popen(hang, start_new_session=True)\n"
                    "json.dump({'cli': os.getpid(), 'group': g.pid, 'session': s.pid,\n"
                    "           'detach_env': os.environ.get('COUNCIL_REAP_DETACH')}, open(%r, 'w'))\n"
                    "time.sleep(1.2)\n" % info)
        parent = os.path.join(self.tmp, "parent.py")
        from councillib import supervise
        with open(parent, "w") as f:
            f.write("import os, subprocess, sys, time\n"
                    "env = dict(os.environ, COUNCIL_REAP_DETACH='1', COUNCIL_SEAT_TAG='r14-%d')\n"
                    "subprocess.Popen([sys.executable, '-Es', %r, '1', '--', sys.executable, %r],\n"
                    "                 process_group=0, env=env)\n"
                    "time.sleep(60)\n" % (os.getpid(), supervise.REAP_PY, cli))
        proc = subprocess.Popen([PY, "-Es", parent], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        pids = {}
        try:
            self.assertTrue(_wait(lambda: os.path.exists(info) and os.path.getsize(info) > 0, 5))
            time.sleep(0.05)
            with open(info) as f:
                pids = json.load(f)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait()
            self.assertIsNone(pids["detach_env"], "the CLI never sees COUNCIL_REAP_DETACH")
            time.sleep(0.4)
            reaper = _ppid(pids["cli"])
            for k in ("cli", "group", "session"):
                self.assertFalse(_gone(pids[k]), "%s died with the parent" % k)
            self.assertTrue(_wait(lambda: all(_gone(pids[k]) for k in ("cli", "group", "session"))
                                  and _gone(reaper), 3), "a stray outlived the detached seat's end")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for p in pids.values():
                if isinstance(p, int) and not _gone(p):
                    os.kill(p, signal.SIGKILL)

    def test_r19_d58_a_watch_call_past_twice_its_soft_deadline_is_listed_never_killed(self):
        import contextlib
        import io
        import threading
        seats = [seat("a"), seat("b"), hseat("x", "busy-silent 1.6", hedge_mode="quick")]
        cfg = self.hcfg(seats, at=0.4, ceiling=30)
        recs, seen, stop = [], [], threading.Event()

        def on_ready(code, info):
            wdir = council_run.watch_dir(self.state)
            for n in sorted(os.listdir(wdir)):
                if n.endswith(".json") and not n.endswith(".end.json"):
                    with open(os.path.join(wdir, n)) as f:
                        recs.append(json.load(f))

        def watch_summary():
            while not stop.is_set():
                try:
                    lw = self.summary().get("long_watch")
                except (OSError, ValueError):
                    lw = None
                if lw:
                    seen.append(lw)
                time.sleep(0.05)
        th = threading.Thread(target=watch_summary, daemon=True)
        th.start()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                code = council_run.run(cfg, "Q?", [], self.out, on_ready=on_ready)
        finally:
            stop.set()
            th.join(2)
        self.assertEqual(code, 0)
        self.assertEqual([(r["call"], r["soft_at_s"]) for r in recs], [("original", 0.4)],
                         "the registry record carries the call's soft deadline")
        self.assertRegex(err.getvalue(), r"long_watch: seat x call original run \S+ age \d+ s, over 2x its soft "
                                         r"deadline \(0\.4 s\): still running; nothing is killed\. To end it: kill -TERM "
                                         + str(os.getpid()))
        self.assertEqual(err.getvalue().count("long_watch: seat x call original"), 1, "listed once")
        self.assertTrue(seen, "summary.json lists it while it runs")
        self.assertEqual({(e["seat"], e["call"], e["soft_s"]) for e in seen[-1]}, {("x", "original", 0.4)})
        self.assertNotIn("long_watch", self.summary(), "nothing listed once it ended")
        self.assertEqual({c["call"]: c["status"] for c in self.seat_json("x")["calls"]},
                         {"original": "ok", "hedge": "ok"}, "nothing was killed")
        self.assert_no_survivors()

    # step 3: reporting --------------------------------------------------------------------------------------
    def test_r20_launcher_report_lines(self):
        import council
        rep = council.report_lines({"code": 0, "synthesis": "/r/chair/synthesis.md", "runner_pid": 42,
                                    "pending": ["x"], "below_quorum": True, "ok": 1, "need": 2,
                                    "late_pending": [{"seat": "x", "call": "original"},
                                                     {"seat": "y", "call": "original"}]})
        self.assertEqual(rep, "synthesis ready: /r/chair/synthesis.md\n"
                              "below quorum: 1 of 2 required answers; the chair synthesised without the pending "
                              "seats\n"
                              "pending seats: x (runner 42 keeps supervising)\n"
                              "still running: y/original (runner 42 keeps supervising; a late answer is appended "
                              "and flagged)\n")
        rep = council.report_lines({"code": 4, "synthesis": None, "runner_pid": 7, "pending": ["x", "z"],
                                    "reason": "pending: no answers yet"})
        self.assertEqual(rep, "pending: no answers yet\npending seats: x,z (runner 7 keeps supervising)\n")
        self.assertEqual(council.report_lines({"code": 3, "pending": []}), "")


class CallerSession(Base):
    """P33c (run example-run, row 24): summary.json names the calling Claude session
    (`caller_session`, from progress_mirror.find_owner; null when there is none) and the engine dir (`engine_root`)."""

    def owner(self, value):
        p = mock.patch.object(council_run._progress, "find_owner", return_value=value)
        p.start()
        self.addCleanup(p.stop)

    def test_c01_caller_session_and_engine_root_with_an_owner(self):
        self.owner({"session_id": "sess-test-1", "pid": None, "start_ticks": None, "inbox": None})
        self.assertEqual(self.run_council(self.cfg([seat("a", "echo-stdin"), seat("b", "echo-stdin")])), 0)
        summ = self.summary()
        self.assertEqual(summ["caller_session"], "sess-test-1")
        self.assertEqual(summ["engine_root"], os.path.dirname(os.path.dirname(os.path.abspath(council_run.__file__))))

    def test_c02_no_owner_is_null_never_an_error(self):
        self.owner(None)
        self.assertEqual(self.run_council(self.cfg([seat("a", "echo-stdin"), seat("b", "echo-stdin")])), 0)
        summ = self.summary()
        self.assertIn("caller_session", summ)
        self.assertIsNone(summ["caller_session"])
        with mock.patch.object(council_run._progress, "find_owner", side_effect=OSError("boom")):
            self.assertIsNone(council_run._caller_session())

    def test_c03_real_lookup_reads_the_env_session_and_none_without_it(self):
        home = os.path.join(self.tmp, "home")             # no ~/.claude/sessions here: the parent walk finds nothing
        os.makedirs(home)
        with mock.patch.dict(os.environ, {"HOME": home, "CLAUDE_CODE_SESSION_ID": "sess-env-2"}):
            self.assertEqual(council_run._caller_session(), "sess-env-2")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        env["HOME"] = home
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(council_run._caller_session())

    def test_c04_early_exit_summary_has_the_fields(self):
        self.owner({"session_id": "sess-test-3"})
        os.remove(os.path.join(self.prompts, "advisor.md"))
        self.assertEqual(self.run_council(self.cfg([seat("a", "echo-stdin")])), 2)
        summ = self.summary()
        self.assertEqual((summ["caller_session"], summ["engine_root"]), ("sess-test-3", council_run._config.ROOT))

    def test_c05_an_old_summary_without_the_fields_still_reads(self):
        self.owner(None)
        self.assertEqual(self.run_council(self.cfg([seat("a", "echo-stdin"), seat("b", "echo-stdin")])), 0)
        sp = os.path.join(self.out, "summary.json")
        old = self.summary()
        for k in ("caller_session", "engine_root"):
            old.pop(k)
        with open(sp, "w") as f:
            json.dump(old, f)
        from councillib import preflight, status
        wdir = os.path.join(self.tmp, "watch-empty")
        os.makedirs(wdir)
        preflight._finalize_runs(wdir, {self.out})        # the orphan sweep rewrites an old summary as it is
        summ = self.summary()
        self.assertTrue(summ["runner_lost"])
        self.assertNotIn("caller_session", summ)
        self.assertIsNotNone(status.load(self.out))


if __name__ == "__main__":
    unittest.main()
