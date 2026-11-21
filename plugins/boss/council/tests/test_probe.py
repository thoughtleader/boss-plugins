"""Probe tool tests (row 6): tools/council-probe.py on stub adapters + stub CLI. No network, no paid call.

The dry-run case builds the REAL adapters from the real config.toml (argv only, nothing launched); its
overlay points state_dir at a temp dir, so the real latency log and drift snapshot are never touched.
Every live case uses stub seats through an overlay ([[seat]] replaces the roster).

Run: python3 -Es -m unittest tests.test_probe   (cwd ~/.claude/council)
"""
import glob
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import council_run, providers  # noqa: E402
from tests.test_salvage import ReasonAdapter  # noqa: E402

_spec = importlib.util.spec_from_file_location("council_probe", os.path.join(ROOT, "tools", "council-probe.py"))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

ADVISOR = "SEAT $seat_id of $n_seats\nQ: $question\nC: $context\n"
CHAIR = "CHAIR\nQ: $question\nANSWERS:\n$answers\n"
SENTINEL = "TESTSECRET-probe-sentinel-0123456789"


def _toml_val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v))


def _pids(pidfile):
    try:
        with open(pidfile) as f:
            return [ln for ln in f if ln.strip()]
    except OSError:
        return []


class ProbeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.prompts = os.path.join(self.tmp, "prompts")
        os.makedirs(self.prompts)
        for name, text in (("advisor.md", ADVISOR), ("chair.md", CHAIR)):
            with open(os.path.join(self.prompts, name), "w") as f:
                f.write(text)
        self.out = os.path.join(self.tmp, "probes")
        self.pidfile = os.path.join(self.tmp, "pids")
        env = {"STUB_PIDFILE": self.pidfile, "OPENROUTER_API_KEY": SENTINEL, "SOME_OTHER_TOKEN": SENTINEL}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        ReasonAdapter.caps, ReasonAdapter.built = [], []
        providers.register(ReasonAdapter())
        self.addCleanup(providers._REGISTRY.pop, ReasonAdapter.name, None)
        self.work_before = set(glob.glob(os.path.join(tempfile.gettempdir(), probe.WORK_PREFIX + "*")))

    def overlay(self, seats=None, chair=None, real_prompts=False):
        lines = ['state_dir = %s' % _toml_val(self.state)]
        if not real_prompts:
            lines.append('prompts_dir = %s' % _toml_val(self.prompts))
        if chair is not None:
            lines.append("[chair]")
            lines += ["%s = %s" % (k, _toml_val(v)) for k, v in chair.items()]
        if seats is not None:
            lines += ["[salvage]", "enabled = true", "max_output_tokens = 8000", "timeout = 30",
                      "max_partial_chars = 120000", "chair_max_output_tokens = 16000", "chair_timeout = 45",
                      "[providers.stub]", 'mode = "stream"', "[providers.rstub]", 'mode = "stream"']
            for s in seats:
                lines.append("[[seat]]")
                lines += ["%s = %s" % (k, _toml_val(v)) for k, v in s.items()]
        path = os.path.join(self.tmp, "overlay.toml")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def main(self, *argv):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", io.StringIO()):
            rc = probe.main(list(argv))
        return rc, buf.getvalue()

    def rec(self, label):
        with open(os.path.join(self.out, label + ".json")) as f:
            return json.load(f)

    def latency(self):
        path = os.path.join(self.state, "latency.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def assert_clean(self):
        """No drift snapshot, no workdir left behind, no secret in any output file."""
        self.assertFalse(os.path.exists(os.path.join(self.state, "drift")), "the probe must never write drift")
        left = set(glob.glob(os.path.join(tempfile.gettempdir(), probe.WORK_PREFIX + "*"))) - self.work_before
        self.assertEqual(left, set(), "probe workdir left behind")
        if os.path.isdir(self.out):
            self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)
            for name in os.listdir(self.out):
                path = os.path.join(self.out, name)
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, name)
                with open(path, "rb") as f:
                    self.assertNotIn(SENTINEL.encode(), f.read(), name)


def stub(sid, mode="echo-stdin", provider="stub", **kw):
    d = {"id": sid, "provider": provider, "model": "stub-model", "stub_mode": mode}
    d.update(kw)
    return d


class DryRunTests(ProbeBase):
    def test_01_dry_run_real_adapters_all_seats_and_chair(self):
        ov = self.overlay(real_prompts=True)
        rc, out = self.main("salvage", "--seat", "all", "--chair", "--dry-run", "--config", ov)
        self.assertEqual(rc, 0, out)
        for sid in ("claude", "codex", "agy", "openrouter", "deepseek", "chair"):
            self.assertIn("== salvage %s (DRY RUN" % sid, out)
        self.assertEqual(out.count("--effort low --thinking disabled"), 2)          # claude seat + chair (G1)
        self.assertIn("-c model_reasoning_effort=low", out)                         # codex
        self.assertIn("'Gemini 3.8 Flash (Low)'", out)                              # agy tier swap
        self.assertIn("--reasoning-effort low", out)                                # GLM 5.3: reasoning is mandatory, so low
        self.assertIn('"reasoning": {"effort": "low"}', out)
        self.assertIn("--reasoning-effort none", out)                               # DeepSeek
        self.assertIn('"reasoning": {"effort": "none"}', out)
        self.assertIn("id=chair-salvage role=salvage", out)
        self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS=16000", out)
        self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS=8000", out)
        self.assertIn("ceiling_s=1200", out)                                        # chair_timeout
        # no key value, no non-allowlisted env, no prompt text
        self.assertNotIn(SENTINEL, out)
        self.assertNotIn("SOME_OTHER_TOKEN", out)
        self.assertNotIn("CLAUDE_CODE_MAX_RETRIES", out)
        self.assertNotIn("bat and a ball", out)
        self.assertIn("'<prompt ", out)                                             # agy's prompt argv
        # nothing launched, nothing written
        self.assertEqual(_pids(self.pidfile), [])
        self.assertFalse(os.path.exists(self.state))
        self.assert_clean()

    def test_02_dry_run_seat_mode_shows_real_build_and_its_salvage(self):
        ov = self.overlay(real_prompts=True)
        rc, out = self.main("seat", "--seat", "deepseek", "--dry-run", "--config", ov)
        self.assertEqual(rc, 0, out)
        self.assertIn("--max-tokens 131072 --reasoning-effort high", out)
        self.assertIn("-> its one salvage if answerless", out)
        self.assertIn("--max-tokens 8000 --reasoning-effort none", out)
        self.assertEqual(_pids(self.pidfile), [])
        self.assert_clean()

    def test_03_dry_run_stub_launches_nothing(self):
        ov = self.overlay(seats=[stub("s1")])
        rc, out = self.main("salvage", "--seat", "s1", "--dry-run", "--config", ov)
        self.assertEqual(rc, 0, out)
        self.assertEqual(_pids(self.pidfile), [])
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.latency(), [])

    def test_04_live_needs_one_target_and_out(self):
        ov = self.overlay(seats=[stub("s1"), stub("s2")])
        rc, _ = self.main("salvage", "--seat", "all", "--out", self.out, "--config", ov)
        self.assertEqual(rc, 2)
        rc, _ = self.main("salvage", "--seat", "s1", "--config", ov)
        self.assertEqual(rc, 2)
        rc, _ = self.main("salvage", "--seat", "nope", "--out", self.out, "--config", ov)
        self.assertEqual(rc, 2)
        self.assertEqual(_pids(self.pidfile), [])


class SalvageModeTests(ProbeBase):
    def test_10_salvage_call_answers(self):
        ov = self.overlay(seats=[stub("s1")])
        rc, out = self.main("salvage", "--seat", "s1", "--out", self.out, "--config", ov)
        self.assertEqual(rc, 0, out)
        r = self.rec("salvage-s1")
        self.assertEqual((r["status"], r["probe"], r["role"], r["id"]), ("ok", "salvage", "salvage", "s1"))
        self.assertEqual(r["ceiling_s"], 30.0)                                       # [salvage] timeout
        # the stub echoes its prompt: the real _salvage_prompt over the synthetic reasoning partial
        head = r["answer_head"]
        self.assertIn("SEAT s1 of 1", head)
        self.assertIn("bat and a ball", head)
        self.assertIn("SALVAGE: your earlier attempt at the task above ended (empty_output)", head)
        self.assertIn("Your own reasoning from that attempt follows", head)
        self.assertLessEqual(len(head), probe.HEAD_MAX)
        self.assertEqual(r["synthetic_parent"]["status_given"], "empty_output")
        self.assertEqual(r["env"], {})                                                # stub env: nothing allowlisted
        self.assertTrue(r["workdir"].startswith(os.path.join(tempfile.gettempdir(), probe.WORK_PREFIX)))
        self.assertIn("usage", r)
        for ext in (".raw", ".md"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, "salvage-s1" + ext)))
        with open(os.path.join(self.out, "salvage-s1.md")) as f:
            self.assertIn(council_run.SALVAGE_INSTRUCTION, f.read())
        self.assertEqual(len(_pids(self.pidfile)), 1)
        lat = self.latency()
        self.assertEqual([x["kind"] for x in lat], ["probe"])
        self.assertEqual(lat[0]["outcome"], "ok")
        self.assertIn("salvage-s1: status=ok", out)
        self.assert_clean()

    def test_11_chair_salvage_label_cap_and_answers(self):
        seats_dir = os.path.join(self.tmp, "seats")
        os.makedirs(seats_dir)
        for sid, st, text in (("a", "ok", "ANSWER-A 11, 13"), ("b", "adapter_error", "> FAILED status=x\n\nbits")):
            with open(os.path.join(seats_dir, sid + ".json"), "w") as f:
                json.dump({"id": sid, "provider": "p" + sid, "model": "m" + sid, "status": st}, f)
            with open(os.path.join(seats_dir, sid + ".md"), "w") as f:
                f.write(text)
        ov = self.overlay(seats=[stub("s1")], chair={"provider": "rstub", "model": "stub-model",
                                                      "stub_mode": "echo-stdin", "stub_salvage_mode": "echo-stdin"})
        rc, out = self.main("salvage", "--chair", "--answers-dir", seats_dir, "--out", self.out, "--config", ov)
        self.assertEqual(rc, 0, out)
        r = self.rec("salvage-chair")
        self.assertEqual((r["id"], r["probe"], r["status"]), ("chair-salvage", "chair_salvage", "ok"))
        self.assertEqual(r["ceiling_s"], 45.0)                                       # chair_timeout
        self.assertEqual(ReasonAdapter.caps, [("chair", 16000)])                     # chair_max_output_tokens
        self.assertEqual(ReasonAdapter.built, [("chair-salvage", "salvage")])
        with open(os.path.join(self.out, "salvage-chair.md")) as f:
            md = f.read()
        self.assertIn("CHAIR\n", md)
        self.assertIn("### seat a | provider pa | model ma | status ok\n\nANSWER-A 11, 13", md)
        self.assertIn("### seat b | provider pb | model mb | status adapter_error\n\n(no answer)", md)
        self.assertIn("without a usable final synthesis", md)
        self.assertIn(council_run.CHAIR_SALVAGE_INSTRUCTION, md)
        self.assert_clean()

    def test_12_labels_never_overwrite(self):
        ov = self.overlay(seats=[stub("s1")])
        for _ in range(2):
            rc, _out = self.main("salvage", "--seat", "s1", "--out", self.out, "--config", ov)
            self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "salvage-s1.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "salvage-s1-2.json")))
        self.assertEqual(self.rec("salvage-s1-2")["label"], "salvage-s1-2")

    def test_13_partial_file_and_failed_call(self):
        part = os.path.join(self.tmp, "partial.txt")
        with open(part, "w") as f:
            f.write("PARTIAL-ANSWER-SO-FAR")
        ov = self.overlay(seats=[stub("s1", stub_salvage_mode="exit 3")])
        rc, out = self.main("salvage", "--seat", "s1", "--partial-file", part, "--from-status", "idle_kill",
                            "--out", self.out, "--config", ov)
        self.assertEqual(rc, 1, out)
        r = self.rec("salvage-s1")
        self.assertEqual(r["status"], "exit_3")
        self.assertEqual(r["answer_head"], "")
        self.assertEqual(r["synthetic_parent"]["status_given"], "idle_kill")
        self.assert_clean()


class SeatModeTests(ProbeBase):
    def q(self):
        path = os.path.join(self.tmp, "q.md")
        with open(path, "w") as f:
            f.write("What is 2+2?")
        return path

    def test_20_ok_seat_is_not_salvaged(self):
        ov = self.overlay(seats=[stub("s1", "quick")])
        rc, out = self.main("seat", "--seat", "s1", "--question-file", self.q(), "--out", self.out, "--config", ov)
        self.assertEqual(rc, 0, out)
        r = self.rec("seat-s1")
        self.assertEqual((r["status"], r["probe"]), ("ok", "seat"))
        self.assertNotIn("salvage_call", r)
        self.assertEqual(len(_pids(self.pidfile)), 1)
        self.assertEqual([x["kind"] for x in self.latency()], ["probe"])
        self.assert_clean()

    def test_21_answerless_seat_salvaged_exactly_once(self):
        ov = self.overlay(seats=[stub("r1", provider="rstub", stub_salvage_mode="echo-stdin")])
        rc, out = self.main("seat", "--seat", "r1", "--question-file", self.q(), "--out", self.out, "--config", ov)
        self.assertEqual(rc, 0, out)
        r = self.rec("seat-r1")
        self.assertEqual((r["status"], r["detail"], r["salvaged"], r["salvaged_from"]),
                         ("ok", "salvaged", True, "empty_output"))
        self.assertEqual(r["salvage"]["status"], "ok")
        self.assertEqual(r["salvage_call"]["probe"], "salvage")
        self.assertIn("SALVAGE: your earlier attempt", r["salvage_call"]["answer_head"])
        self.assertEqual(ReasonAdapter.built, [("r1", "salvage")])
        self.assertEqual(len(_pids(self.pidfile)), 2)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "seat-r1.salvage.raw")))
        with open(os.path.join(self.out, "seat-r1.md")) as f:
            self.assertIn("SALVAGED from status=empty_output", f.read())
        self.assertEqual([(x["kind"], x["outcome"]) for x in self.latency()],
                         [("probe", "empty_output"), ("probe", "ok")])
        self.assert_clean()

    def test_22_answerless_salvage_is_not_retried(self):
        ov = self.overlay(seats=[stub("r1", provider="rstub", stub_salvage_mode="echo-stdin",
                                      salvage_reasons_again=True)])
        rc, out = self.main("seat", "--seat", "r1", "--question-file", self.q(), "--out", self.out, "--config", ov)
        self.assertEqual(rc, 1, out)
        r = self.rec("seat-r1")
        self.assertEqual((r["status"], r["salvaged"]), ("empty_output", False))
        self.assertEqual(r["salvage"]["status"], "empty_output")
        self.assertEqual(len(ReasonAdapter.built), 1)
        self.assertEqual(len(_pids(self.pidfile)), 2)
        self.assert_clean()

    def test_23_seat_needs_question_file(self):
        ov = self.overlay(seats=[stub("s1")])
        rc, _ = self.main("seat", "--seat", "s1", "--out", self.out, "--config", ov)
        self.assertEqual(rc, 2)
        self.assertEqual(_pids(self.pidfile), [])


class SignalTests(ProbeBase):
    def test_25_sigterm_keeps_partial_and_leaves_no_survivor(self):
        import signal
        import subprocess
        import time
        ov = self.overlay(seats=[stub("s1", stub_salvage_mode="partial-then-hang")])
        tool = os.path.join(ROOT, "tools", "council-probe.py")
        proc = subprocess.Popen([sys.executable, "-Es", tool, "salvage", "--seat", "s1", "--out", self.out,
                                 "--config", ov], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        end = time.monotonic() + 10
        while time.monotonic() < end and not _pids(self.pidfile):
            time.sleep(0.05)
        self.assertTrue(_pids(self.pidfile), "stub never started")
        time.sleep(0.5)                                     # let PARTIAL-123 reach the supervisor
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 128 + signal.SIGTERM)
        r = self.rec("salvage-s1")
        self.assertEqual((r["status"], r["interrupted"]), ("signal_kill", True))
        with open(os.path.join(self.out, "salvage-s1.raw"), "rb") as f:
            self.assertIn(b"PARTIAL-123", f.read())
        stub_pid = int(_pids(self.pidfile)[0].split()[1])
        end = time.monotonic() + 10
        while time.monotonic() < end and os.path.exists("/proc/%d" % stub_pid):
            time.sleep(0.05)
        self.assertFalse(os.path.exists("/proc/%d" % stub_pid), "stub survived the probe's SIGTERM")
        self.assert_clean()


class HelperTests(unittest.TestCase):
    def test_30_redact_argv(self):
        prompt = "line one\nline two of the prompt"
        argv = ["agy", "--print", "DIRECTIVE " + prompt, "--key", "sk-abcdefghijkl", "x" * 600]
        out = probe.redact_argv(argv, prompt)
        self.assertEqual(out[0:2], ["agy", "--print"])
        self.assertTrue(out[2].startswith("<prompt "))
        self.assertEqual(out[4], "sk-...")
        self.assertEqual(out[5], "<prompt 600 bytes>")

    def test_31_prompt_helpers_match_templates(self):
        import string
        a = string.Template("$seat_id/$n_seats/$question/$context")
        c = string.Template("$question|$answers")
        self.assertEqual(council_run.advisor_prompt(a, "Q", "C", "s1", 5), "s1/5/Q/C")
        self.assertEqual(council_run.chair_prompt(c, "Q", "A"), "Q|A")

    def test_32_openrouter_request_from_real_builder(self):
        argv = ["py", "-Es", providers.OPENROUTER_SEAT, "--model", "m", "--max-tokens", "8000", "--reasoning-off"]
        body = probe.openrouter_request(argv, 42)
        self.assertEqual(body["reasoning"], {"enabled": False})
        self.assertEqual(body["messages"][0]["content"], "<prompt 42 bytes>")
        self.assertIsNone(probe.openrouter_request(["claude", "-p"], 1))


if __name__ == "__main__":
    unittest.main()
