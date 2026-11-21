"""Preflight driver, CLI and prompt templates. Stub adapters only: no network, no paid calls."""
import contextlib
import io
import json
import os
import shutil
import string
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


def read(path):
    with open(path) as f:
        return f.read()


def load_json(path):
    with open(path) as f:
        return json.load(f)

import council
from councillib import config, council_run, netwatch, preflight, providers
from councillib.providers import Check, StubAdapter
from councillib.supervise import Supervisor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS = os.path.join(ROOT, "prompts")


class StubB(StubAdapter):
    """A second always-available provider name, so one can fail while the other passes."""
    name = "stubb"

    def free_checks(self, seats, cfg):
        rows = super().free_checks(seats, cfg)
        for c in rows:
            c.provider = self.name
        return rows


class Bare(providers.Adapter):
    """Reports neither inner_caps nor output_budget (a real-adapter contract violation)."""
    name = "bare"
    family = "stub"

    def build(self, seat, prompt_path, workdir, limits, cfg):
        return StubAdapter().build(seat, prompt_path, workdir, limits, cfg)

    def extractor(self, seat=None):
        return StubAdapter().extractor(seat)

    def free_checks(self, seats, cfg):
        return [Check("bare", "free", "version", "PASS", "ok")]


def setUpModule():
    for cls in (StubB, Bare):
        providers.FAMILIES[cls.name] = "stub"
        providers.register(cls())


def tearDownModule():
    for name in ("stubb", "bare"):
        providers.FAMILIES.pop(name, None)
        providers._REGISTRY.pop(name, None)


def toml_overlay(path, seats, chair="stub", extra=""):
    lines = ['prompts_dir = "%s"' % PROMPTS, 'state_dir = "%s"' % os.path.dirname(path),
             "[chair]", 'provider = "%s"' % chair, 'model = "chair-m"', 'stub_mode = "quick"',
             "[providers.stub]", 'mode = "stream"', "[providers.stubb]", 'mode = "stream"',
             "[providers.bare]", 'mode = "stream"',
             "[canary]", 'prompt = "Reply with exactly: OK"', "timeout_s = 20", "idle_s = 10",
             "[quorum]", 'min_ok = "n-1"', "straggler_grace_s = 5",
             "[answer_check]", "min_chars = 1"]        # row 2: the stub answer QUICK-OK (8 chars) is near_empty at 16
    for sid, prov, extras in seats:
        lines += ["[[seat]]", 'id = "%s"' % sid, 'provider = "%s"' % prov, 'model = "m-%s"' % sid,
                  'stub_mode = "quick"'] + list(extras)
    lines.append(extra)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def cfg_of(tmp, seats, chair="stub", extra=""):
    return config.load(overlays=[toml_overlay(os.path.join(tmp, "ov.toml"), seats, chair, extra)])


def call(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = council.main(argv)
        except SystemExit as e:        # argparse usage errors
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pf-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def overlay(self, seats, **kw):
        return toml_overlay(os.path.join(self.tmp, "ov.toml"), seats, **kw)

    def rows(self, checks, stage=None, provider=None):
        return [c for c in checks if (stage is None or c.stage == stage)
                and (provider is None or c.provider == provider)]


class FreeStage(Base):
    def test_01_free_fail_starts_no_canary(self):
        cfg = cfg_of(self.tmp, [("a", "stub", []), ("b", "stub", ['stub_fail_free = true'])])
        with mock.patch.object(Supervisor, "start") as start:
            ok, checks = preflight.run(cfg)
        self.assertFalse(ok)
        start.assert_not_called()
        self.assertEqual(self.rows(checks, "canary"), [])
        self.assertTrue([c for c in checks if c.result == "FAIL" and c.stage == "free"])
        txt = preflight.format_table(checks)
        self.assertIn("PREFLIGHT FAIL (1) at free", txt)

    def test_02_free_only_runs_no_canary(self):
        cfg = cfg_of(self.tmp, [("a", "stub", [])])
        with mock.patch.object(Supervisor, "start") as start:
            ok, checks = preflight.run(cfg, free_only=True)
        self.assertTrue(ok)
        start.assert_not_called()
        self.assertEqual(self.rows(checks, "canary"), [])
        self.assertTrue(self.rows(checks, "free"))

    def test_03_adapter_missing_required_rows_fails(self):
        cfg = cfg_of(self.tmp, [("a", "stub", []), ("x", "bare", [])])
        ok, checks = preflight.run(cfg, free_only=True)
        self.assertFalse(ok)
        failed = {c.check for c in checks if c.provider == "bare" and c.result == "FAIL"}
        self.assertEqual(failed, {"inner_caps", "output_budget"})
        # the stub is exempt from the required rows and gets no buffered WARN rows
        self.assertEqual([c for c in checks if c.provider == "stub" and c.result == "WARN"], [])

    def test_04_buffered_and_no_cap_warn_rows(self):
        class Rich(Bare):
            name = "rich"

            def free_checks(self, seats, cfg):
                return [Check("rich", "free", "inner_caps", "PASS", "ok"),
                        Check("rich", "free", "output_budget", "PASS", "ok")]

        providers.FAMILIES["rich"] = "stub"
        providers.register(Rich())
        self.addCleanup(lambda: (providers.FAMILIES.pop("rich", None), providers._REGISTRY.pop("rich", None)))
        cfg = cfg_of(self.tmp, [("a", "stub", []), ("r", "rich", [])])    # rich has no mode: buffered
        ok, checks = preflight.run(cfg, free_only=True)
        self.assertTrue(ok)
        warns = {c.check for c in checks if c.provider == "rich" and c.result == "WARN"}
        self.assertEqual(warns, {"buffered_partial"})

    def test_05_adapter_exception_is_a_fail_row(self):
        cfg = cfg_of(self.tmp, [("a", "stub", [])])
        with mock.patch.object(StubAdapter, "free_checks", side_effect=RuntimeError("boom")):
            ok, checks = preflight.run(cfg, free_only=True)
        self.assertFalse(ok)
        self.assertTrue([c for c in checks if c.check == "free_checks" and c.result == "FAIL"])


class Policy(Base):
    def test_10_two_claude_advisors_exit_2_no_adapter_called(self):
        ov = self.overlay([("c1", "claude", []), ("c2", "claude", []), ("s", "stub", [])], chair="claude")
        with mock.patch.object(providers, "get", side_effect=AssertionError("adapter called")), \
                mock.patch.object(Supervisor, "start", side_effect=AssertionError("spawned")):
            rc, out, err = call(["preflight", "--config", ov])
            self.assertEqual(rc, 2)
            self.assertIn("Anthropic advisor seats", err)
            rc, out, err = call(["run", "--config", ov, "--", "q?"])
            self.assertEqual(rc, 2)
        self.assertEqual(os.listdir(self.tmp), ["ov.toml"])      # no run dir was made

    def test_11_chair_provider_may_be_claude_with_one_claude_advisor(self):
        cfg = cfg_of(self.tmp, [("c1", "claude", []), ("s", "stub", [])], chair="claude")
        checks = preflight.policy_stage(cfg, [])
        self.assertEqual(checks[0].result, "PASS")

    def test_12_allow_missing_chair_provider_or_unknown_exits_2(self):
        ov = self.overlay([("a", "stub", []), ("b", "stubb", [])])
        with mock.patch.object(providers, "get", side_effect=AssertionError("adapter called")):
            rc, out, err = call(["preflight", "--config", ov, "--allow-missing", "stub"])
            self.assertEqual(rc, 2)
            self.assertIn("chair", err)
            rc, out, err = call(["preflight", "--config", ov, "--allow-missing", "nothere"])
            self.assertEqual(rc, 2)
            self.assertIn("not a provider in the roster", err)

    def test_13_bad_max_anthropic_advisors_exit_2(self):
        ov = self.overlay([("a", "stub", [])], extra="[policy]\nmax_anthropic_advisors = 2")
        rc, out, err = call(["preflight", "--config", ov])
        self.assertEqual(rc, 2)


class AllowMissing(Base):
    def seats(self):
        return [("a", "stub", []), ("b", "stubb", ['stub_fail_free = true']), ("c", "stub", [])]

    def test_20_allowed_missing_provider_drops_its_seat(self):
        ov = self.overlay(self.seats())
        rc, out, err = call(["preflight", "--config", ov])
        self.assertEqual(rc, 1)
        rc, out, err = call(["preflight", "--config", ov, "--allow-missing", "stubb"])
        self.assertEqual(rc, 0, out)
        self.assertIn("FAIL (allowed missing)", out)
        self.assertIn("PREFLIGHT PASS", out)
        self.assertRegex(out, r"stubb\s+\| canary\s+\| canary\s+\| SKIP")
        rc, out, err = call(["run", "--config", ov, "--allow-missing", "stubb", "--slug", "drop", "--", "why?"])
        self.assertEqual(rc, 0, out + err)
        run_dir = [l for l in out.splitlines() if l.startswith("run dir: ")][0][9:]
        summary = load_json(os.path.join(run_dir, "summary.json"))
        st = {s["id"]: s["status"] for s in summary["seats"]}
        self.assertEqual(st, {"a": "ok", "b": "skipped_allowed_missing", "c": "ok"})

    def test_21_allow_missing_a_passing_provider_still_sits(self):
        ov = self.overlay([("a", "stub", []), ("b", "stubb", [])])
        rc, out, err = call(["run", "--config", ov, "--allow-missing", "stubb", "--slug", "sits", "--", "q"])
        self.assertEqual(rc, 0, out + err)
        run_dir = [l for l in out.splitlines() if l.startswith("run dir: ")][0][9:]
        summary = load_json(os.path.join(run_dir, "summary.json"))
        self.assertEqual({s["id"]: s["status"] for s in summary["seats"]}, {"a": "ok", "b": "ok"})

    def test_22_unallowed_failure_still_exits_1_and_run_has_no_seats(self):
        ov = self.overlay(self.seats())
        rc, out, err = call(["run", "--config", ov, "--slug", "nope", "--", "q"])
        self.assertEqual(rc, 1)
        run_dir = [l for l in out.splitlines() if l.startswith("run dir: ")][0][9:]
        self.assertEqual(sorted(os.listdir(run_dir)), ["events.jsonl", "preflight.json", "preflight.txt", "status.json"])


class Canary(Base):
    def test_30_canaries_one_per_pair_logged_as_canary(self):
        cfg = cfg_of(self.tmp, [("a", "stub", []), ("b", "stub", []), ("c", "stubb", [])])
        ok, checks = preflight.run(cfg)
        self.assertTrue(ok, preflight.format_table(checks))
        cans = self.rows(checks, "canary")
        self.assertEqual(len(cans), 4)       # m-a, m-b, m-c, chair-m
        self.assertTrue(all(c.result == "PASS" for c in cans))
        log = [json.loads(l) for l in read(os.path.join(self.tmp, "latency.jsonl")).splitlines()]
        self.assertEqual([r["kind"] for r in log], ["canary"] * 4)
        self.assertNotIn("Reply with exactly", json.dumps(log))

    def test_31_model_mismatch_fails_canary(self):
        cfg = cfg_of(self.tmp, [("a", "stub", ["stub_reported = \"other-model\""])])
        ok, checks = preflight.run(cfg)
        self.assertFalse(ok)
        bad = [c for c in self.rows(checks, "canary") if c.result == "FAIL"]
        self.assertEqual(len(bad), 1)
        self.assertIn("model_mismatch", bad[0].detail)

    def test_32_killed_canary_keeps_partial_in_detail(self):
        cfg = cfg_of(self.tmp, [("a", "stub", [])])
        cfg["seat"][0]["stub_mode"] = "partial-then-hang"
        cfg["canary"]["idle_s"] = 1.5
        cfg["canary"]["timeout_s"] = 6
        ok, checks = preflight.run(cfg)
        self.assertFalse(ok)
        bad = [c for c in self.rows(checks, "canary") if c.result == "FAIL"]
        self.assertEqual(len(bad), 1)
        self.assertIn("idle_kill", bad[0].detail)
        self.assertIn("PARTIAL-123", bad[0].detail)

    def test_33_canary_carries_canary_flag_to_build(self):
        cfg = cfg_of(self.tmp, [("a", "stub", [])])
        seen = []
        orig = StubAdapter.build

        def spy(self_, seat, prompt_path, workdir, limits, c):
            seen.append((c.get("_canary"), limits.ceiling_s, limits.mode, read(prompt_path)))
            return orig(self_, seat, prompt_path, workdir, limits, c)

        with mock.patch.object(StubAdapter, "build", spy):
            ok, _ = preflight.run(cfg)
        self.assertTrue(ok)
        self.assertEqual(seen[0], (True, 20.0, "stream", "Reply with exactly: OK"))

    def test_34_json_output_shape(self):
        cfg = cfg_of(self.tmp, [("a", "stub", [])])
        ok, checks = preflight.run(cfg, free_only=True)
        data = json.loads(preflight.to_json(checks, free_only=True))
        self.assertTrue(data["ok"])
        self.assertEqual(set(data["checks"][0]), {"provider", "stage", "check", "result", "detail", "allowed_missing"})

    # C6: a seat that passed its canary inside [canary] cache_s is not called again
    def canary_runs(self, cfg):
        starts = []
        orig = Supervisor.start

        def spy(self_, key, *a, **kw):
            starts.append(key)
            return orig(self_, key, *a, **kw)
        with mock.patch.object(Supervisor, "start", spy):
            ok, checks = preflight.run(cfg)
        return ok, checks, starts

    def cached_cfg(self, window=600, **kw):
        cfg = cfg_of(self.tmp, [("a", "stub", []), ("b", "stub", [])], **kw)
        cfg["canary"]["cache_s"] = window
        return cfg

    def test_35_a_recent_pass_is_cached_and_not_called_again(self):
        cfg = self.cached_cfg()
        ok, checks, starts = self.canary_runs(cfg)
        self.assertTrue(ok)
        self.assertEqual(len(starts), 3)                                  # m-a, m-b, chair-m
        path = os.path.join(self.tmp, preflight.CANARY_CACHE)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(len(load_json(path)), 3)
        ok, checks, starts = self.canary_runs(cfg)
        self.assertTrue(ok, preflight.format_table(checks))
        self.assertEqual(starts, [])
        cans = self.rows(checks, "canary")
        self.assertEqual([c.result for c in cans], ["PASS"] * 3)
        self.assertTrue(all(c.detail.startswith("cached: the same canary passed") for c in cans), cans[0].detail)
        self.assertEqual(len(read(os.path.join(self.tmp, "latency.jsonl")).splitlines()), 3)   # no new canary logged

    def test_36_the_cache_never_covers_a_failure_an_expired_pass_or_a_changed_setup(self):
        cfg = cfg_of(self.tmp, [("a", "stub", ["stub_reported = \"other-model\""])])
        cfg["canary"]["cache_s"] = 600
        for _ in range(2):                                               # a FAIL is never remembered
            ok, checks, starts = self.canary_runs(cfg)
            self.assertFalse(ok)
            self.assertTrue(starts)
        cfg = self.cached_cfg()
        self.canary_runs(cfg)
        path = os.path.join(self.tmp, preflight.CANARY_CACHE)
        old = load_json(path)
        for v in old.values():
            v["at"] -= 601                                              # just outside the window
        with open(path, "w") as f:
            json.dump(old, f)
        ok, checks, starts = self.canary_runs(cfg)
        self.assertEqual(len(starts), 3)
        cfg["canary"]["prompt"] = "Reply with exactly: OK."            # a changed setup is a different key
        ok, checks, starts = self.canary_runs(cfg)
        self.assertEqual(len(starts), 3)
        ok, checks, starts = self.canary_runs(self.cached_cfg(window=0))    # window 0: off, nothing read
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(not c.detail.startswith("cached:") for c in self.rows(checks, "canary")))

    def test_37_an_unwritable_or_corrupt_cache_never_fails_the_preflight(self):
        cfg = self.cached_cfg()
        with open(os.path.join(self.tmp, preflight.CANARY_CACHE), "w") as f:
            f.write("{not json")
        ok, checks, starts = self.canary_runs(cfg)
        self.assertTrue(ok)
        self.assertEqual(len(starts), 3)
        with mock.patch.object(preflight, "_write_json_atomic", side_effect=OSError("ro")):
            self.assertTrue(self.canary_runs(cfg)[0])


class RunCli(Base):
    def test_40_stub_run_layout(self):
        ov = self.overlay([("a", "stub", []), ("b", "stubb", [])])
        qf = os.path.join(self.tmp, "q.md")
        with open(qf, "w") as f:
            f.write("What is $5 plus $5?")
        ctx = os.path.join(self.tmp, "ctx.txt")
        with open(ctx, "w") as f:
            f.write("some context")
        rc, out, err = call(["run", "--config", ov, "--question-file", qf, "--context-file", ctx,
                             "--out", os.path.join(self.tmp, "runs"), "--slug", "Layout Test!"])
        self.assertEqual(rc, 0, out + err)
        run_dir = [l for l in out.splitlines() if l.startswith("run dir: ")][0][9:]
        self.assertRegex(os.path.basename(run_dir), r"^\d{8}T\d{6}Z-layout-test$")
        self.assertEqual(os.path.dirname(run_dir), os.path.join(self.tmp, "runs"))
        for rel in ("question.md", "preflight.txt", "preflight.json", "summary.json",
                    "seats/a.md", "seats/a.raw", "seats/a.json", "seats/b.md", "chair/synthesis.md"):
            self.assertTrue(os.path.isfile(os.path.join(run_dir, rel)), rel)
        self.assertEqual(read(os.path.join(run_dir, "question.md")), "What is $5 plus $5?")
        self.assertEqual(load_json(os.path.join(run_dir, "summary.json"))["exit_code"], 0)
        self.assertIn("PREFLIGHT PASS", read(os.path.join(run_dir, "preflight.txt")))
        self.assertEqual(os.stat(os.path.join(run_dir, "preflight.json")).st_mode & 0o777, 0o600)

    def test_41_usage_errors_exit_2(self):
        ov = self.overlay([("a", "stub", [])])
        with mock.patch.object(providers, "get", side_effect=AssertionError("adapter called")):
            self.assertEqual(call(["run", "--config", ov])[0], 2)                         # no question
            self.assertEqual(call(["run", "--config", ov, "--context-file", "/nope", "--", "q"])[0], 2)
            self.assertEqual(call(["run", "--config", ov, "--question-file", "/nope"])[0], 2)
            self.assertEqual(call(["preflight", "--config", "/nope.toml"])[0], 2)
            self.assertEqual(call(["frobnicate"])[0], 2)

    def test_42_cli_file_is_executable_with_shebang(self):
        path = os.path.join(ROOT, "council.py")
        self.assertTrue(os.access(path, os.X_OK))
        self.assertEqual(read(path).splitlines()[0].strip(), "#!/bin/sh")        # line 2 execs `python3 -Es` (test_hardening checks it)


class Prompts(unittest.TestCase):
    def render(self, name, **kw):
        with open(os.path.join(PROMPTS, name)) as f:
            return string.Template(f.read()).substitute(**kw)

    def test_50_advisor_renders_with_schedulers_kwargs(self):
        text = self.render("advisor.md", question="Q-TEXT", context="CTX-TEXT", seat_id="seat-x", n_seats=5, lens_line="")
        self.assertNotIn("$", text)
        for needle in ("5 independent advisors", "seat-x", "Q-TEXT", "CTX-TEXT", "Confidence: low"):
            self.assertIn(needle, text)
        self.assertRegex(text, r"(?i)will not see the other answers")

    def test_51_chair_renders_and_marks_truncated_and_failed(self):
        text = self.render("chair.md", question="Q-TEXT", answers="ANS-TEXT", critique_guide="", critique_item="",
                           critiques="")
        self.assertNotIn("$", text)
        for needle in ("Q-TEXT", "ANS-TEXT", "TRUNCATED", "(no answer)", "seat id", "Never invent"):
            self.assertIn(needle, text)

    def test_52_dollar_in_question_survives(self):
        text = self.render("advisor.md", question="Is $5 > $3?", context="(none)", seat_id="s", n_seats=2, lens_line="")
        self.assertIn("Is $5 > $3?", text)

    def test_53_scheduler_accepts_the_templates(self):
        from councillib import council_run
        cfg = {"prompts_dir": PROMPTS}
        council_run._load_template(cfg, "advisor.md").substitute(question="", context="", seat_id="", n_seats=0, lens_line="")
        council_run._load_template(cfg, "chair.md").substitute(
            question="", answers="", critique_guide="", critique_item="", critiques="")

class LongWatch(Base):
    """Row 46 (D58): a watch call under a LIVE runner older than 2x its soft deadline is a WARN row. Stub records and
    a stubbed process table only; nothing is ever signalled."""

    SOFT = 100.0

    def setUp(self):
        super().setUp()
        self.cfg = cfg_of(self.tmp, [("a", "stub", [])])
        self.cfg["state_dir"] = self.tmp
        self.wdir = council_run.watch_dir(self.tmp)
        os.makedirs(self.wdir)
        self.kills = mock.Mock()
        for target in ("os.kill", "os.killpg"):
            p = mock.patch(target, self.kills)
            p.start()
            self.addCleanup(p.stop)
        self.live = [4242]
        p = mock.patch.object(netwatch, "tagged_pids", lambda tag, *a, **k: list(self.live))
        p.start()
        self.addCleanup(p.stop)

    def record(self, tag, age, soft=SOFT, runner_pid=None, kind="seat", **extra):
        pid = os.getpid() if runner_pid is None else runner_pid
        rec = {"tag": tag, "run_id": "run-" + tag, "out_dir": os.path.join(self.tmp, "run-" + tag), "seat": "codex1",
               "call": "original", "kind": kind, "seat_spec": {"id": "codex1", "provider": "stub", "model": "m"},
               "runner_pid": pid, "runner_start": council_run.proc_start(pid),
               "started_epoch": time.time() - age}
        if soft is not None:
            rec["soft_at_s"] = soft
        rec.update(extra)
        with open(os.path.join(self.wdir, tag + ".json"), "w") as f:
            json.dump(rec, f)
        return rec

    def warns(self):
        return [c for c in preflight.long_watch_rows(self.cfg)]

    def test_60_over_twice_the_soft_deadline_warns_with_seat_call_run_age(self):
        self.record("t1", 2.5 * self.SOFT)
        rows = self.warns()
        self.assertEqual(len(rows), 1)
        c = rows[0]
        self.assertEqual((c.result, c.stage, c.check), ("WARN", "free", "long_watch:codex1.original"))
        for needle in ("seat codex1", "call original", "run run-t1", "age 4m10s", "kill -TERM %d" % os.getpid(),
                       "nothing is killed"):
            self.assertIn(needle, c.detail)
        self.kills.assert_not_called()

    def test_61_under_twice_the_soft_deadline_no_warn(self):
        self.record("t1", 1.9 * self.SOFT)
        self.assertEqual(self.warns(), [])
        self.kills.assert_not_called()

    def test_62_only_a_live_runner_and_a_live_call_count(self):
        dead = subprocess.Popen([sys.executable, "-Es", "-c", "pass"])
        dead.wait()
        self.record("t-orphan", 5 * self.SOFT, runner_pid=dead.pid)     # the orphan sweep's, not this row's
        self.record("t-ended", 5 * self.SOFT)
        self.live = []                                                   # no process under the tag: it ended
        self.assertEqual(self.warns(), [])
        self.kills.assert_not_called()

    def test_63_no_soft_deadline_no_warn_and_chair_has_none(self):
        self.record("t1", 50 * self.SOFT, soft=None)                    # stream seat: not hedged, no soft deadline
        self.record("t2", 50 * self.SOFT, soft=None, kind="chair")
        self.assertEqual(self.warns(), [])

    def test_64_soft_deadline_is_computed_for_a_hedged_seat_when_the_record_has_none(self):
        self.cfg["providers"]["stub"]["mode"] = "buffered"
        self.cfg["hedge"] = {"enabled": True, "providers": ["stub"], "cold_s": 100, "floor_s": 50, "cap_s": 200}
        self.record("t1", 250, soft=None)               # no latency samples: cold_s = 100, so 250 s is over 2x
        self.record("t2", 150, soft=None)
        rows = self.warns()
        self.assertEqual([c.check for c in rows], ["long_watch:codex1.original"])
        self.assertIn("run run-t1", rows[0].detail)

    def test_65_run_adds_the_rows_and_a_warn_never_fails_preflight(self):
        self.record("t1", 3 * self.SOFT)
        ok, checks = preflight.run(self.cfg, free_only=True)
        self.assertTrue(ok)
        self.assertEqual([c.result for c in checks if c.check.startswith("long_watch")], ["WARN"])
        self.kills.assert_not_called()

    def test_66_a_broken_record_is_skipped_and_the_check_never_raises(self):
        with open(os.path.join(self.wdir, "bad.json"), "w") as f:
            f.write("{not json")
        self.record("t1", 3 * self.SOFT)
        self.assertEqual(len(self.warns()), 1)


if __name__ == "__main__":
    unittest.main()
