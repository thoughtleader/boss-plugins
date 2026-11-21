"""Row 12 (run example-run): chair sections, per-seat verdict line, seat lenses.

Stub processes only: no network, no paid call. Verdicts are display only: no test here may need a gate.

Run: python3 -Es -m unittest tests.test_verdict   (cwd ~/.claude/council)
"""
import io
import json
import os
import string
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import council  # noqa: E402
from councillib import config, council_run, status, verdict  # noqa: E402
from tests.test_council_run import Base, seat  # noqa: E402

PROMPTS = os.path.join(ROOT, "prompts")


class Parse(unittest.TestCase):
    def test_01_words(self):
        for text, want in (("a\nVerdict: APPROVE\nConfidence: high", "APPROVE"), ("Verdict: revise", "REVISE"),
                           ("Verdict: BLOCK.", "BLOCK"), ("Verdict: n/a", "n/a"), ("Verdict: N/A (open question)", "n/a"),
                           ("Verdict: none", "n/a")):
            self.assertEqual(verdict.parse(text), want, text)

    def test_02_markdown_and_last_wins(self):
        self.assertEqual(verdict.parse("**Verdict:** REVISE\n**Confidence:** low"), "REVISE")
        self.assertEqual(verdict.parse("`Verdict: BLOCK`"), "BLOCK")
        self.assertEqual(verdict.parse("Verdict: APPROVE\n...changed my mind...\nVerdict: BLOCK\nConfidence: high"), "BLOCK")

    def test_03_missing_or_garbled_is_none_never_an_error(self):
        for text in (None, "", "no verdict here\nConfidence: high", "Verdict: maybe", "the verdict: APPROVE mid-line", "Verdicts: APPROVE"):
            self.assertIsNone(verdict.parse(text), repr(text))

    def test_04_tally(self):
        t = verdict.tally(["APPROVE", "APPROVE", "REVISE", "n/a", None])
        self.assertEqual(t, {"APPROVE": 2, "REVISE": 1, "BLOCK": 0, "n/a": 1, "missing": 1})


class Lenses(unittest.TestCase):
    SEAT = {"id": "a", "provider": "stub", "model": "m"}

    def test_10_off_by_default_without_table(self):
        self.assertEqual(verdict.lens_for({}, self.SEAT), "")

    def test_11_precedence_override_then_seat_then_table(self):
        cfg = {"lenses": {"enabled": True, "seat": {"a": "table"}}}
        self.assertEqual(verdict.lens_for(cfg, self.SEAT), "table")
        self.assertEqual(verdict.lens_for(cfg, dict(self.SEAT, lens="own")), "own")
        cfg["_lens_overrides"] = {"a": "cli"}
        self.assertEqual(verdict.lens_for(cfg, dict(self.SEAT, lens="own")), "cli")

    def test_12_switches(self):
        cfg = {"lenses": {"enabled": True, "seat": {"a": "table"}}, "_lenses": "off"}
        self.assertEqual(verdict.lens_for(cfg, self.SEAT), "")
        cfg = {"lenses": {"enabled": False, "seat": {"a": "table"}}}
        self.assertEqual(verdict.lens_for(cfg, self.SEAT), "")
        cfg["_lenses"] = "on"
        self.assertEqual(verdict.lens_for(cfg, self.SEAT), "table")

    def test_13_one_short_line(self):
        cfg = {"lenses": {"enabled": True, "seat": {"a": "x\n\ny " + "z" * 500}}}
        out = verdict.lens_for(cfg, self.SEAT)
        self.assertNotIn("\n", out)
        self.assertLessEqual(len(out), verdict.MAX_LENS_CHARS)

    def test_14_overrides_parse(self):
        self.assertEqual(verdict.parse_overrides(["a=security", "b = x=y"]), ({"a": "security", "b": "x=y"}, []))
        self.assertEqual(len(verdict.parse_overrides(["nope", "=x", "a="])[1]), 3)

    def test_15_problems(self):
        self.assertEqual(verdict.problems({}), [])
        self.assertTrue(verdict.problems({"lenses": {"enabled": "yes"}}))
        self.assertTrue(verdict.problems({"lenses": {"seat": {"a": 3}}}))
        self.assertTrue(verdict.problems({"seat": [{"id": "a", "lens": 4}]}))

    def test_16_shipped_config_has_a_lens_for_every_roster_seat(self):
        cfg = config.load()
        self.assertEqual(verdict.problems(cfg), [])
        self.assertEqual(config.validate_roster(cfg), [])
        for s in cfg["seat"]:
            lens = verdict.lens_for(cfg, s)
            self.assertTrue(lens, "seat %s has no default lens" % s["id"])
        self.assertEqual(len({verdict.lens_for(cfg, s) for s in cfg["seat"]}), len(cfg["seat"]), "lenses must differ")


class Prompts(unittest.TestCase):
    def tpl(self, name):
        return council_run._load_template({"prompts_dir": PROMPTS}, name)

    def test_20_advisor_asks_for_verdict_and_confidence(self):
        text = council_run.advisor_prompt(self.tpl("advisor.md"), "Q", "C", "s1", 3)
        self.assertNotIn("$", text)
        for needle in ("Verdict: APPROVE", "Verdict: REVISE", "Verdict: BLOCK", "Verdict: n/a", "Confidence: low"):
            self.assertIn(needle, text)
        self.assertNotIn("Your lens", text)

    def test_21_lens_is_one_line_and_only_when_given(self):
        text = council_run.advisor_prompt(self.tpl("advisor.md"), "Q", "C", "s1", 3, lens="security: abuse")
        self.assertEqual(text.count("Your lens:"), 1)
        self.assertIn("Your lens: security: abuse.", text)
        self.assertEqual(council_run.advisor_prompt(self.tpl("advisor.md"), "Q", "C", "s1", 3, lens=""),
                         council_run.advisor_prompt(self.tpl("advisor.md"), "Q", "C", "s1", 3))

    def test_22_chair_sections_and_kept_rules(self):
        text = council_run.chair_prompt(self.tpl("chair.md"), "Q", "A")
        for needle in ("Seats:", "recommended answer", "Where the advisors agree", "What nobody covered",
                       "Minority view", "Risks and unknowns", "Implementation path", "Confidence:", "Next step",
                       "SALVAGED", "TRUNCATED", "(no answer)", "seat id", "Never invent", "never a gate"):
            self.assertIn(needle, text)
        self.assertNotIn("$", text)


class RunWiring(Base):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.prompts, "advisor.md"), "w") as f:
            f.write("${lens_line}SEAT $seat_id of $n_seats\nQ: $question\nC: $context\n")
        self.files = {}

    def answer_seat(self, sid, text, **kw):
        path = os.path.join(self.tmp, "ans-%s.txt" % sid)
        with open(path, "w") as f:
            f.write(text)
        return seat(sid, ["replay", path, "0"], **kw)

    def test_30_verdicts_in_summary_chair_input_and_status(self):
        seats = [self.answer_seat("a", "Ship it.\nVerdict: APPROVE\nConfidence: high\n"),
                 self.answer_seat("b", "Fix first.\nVerdict: REVISE\nConfidence: medium\n"),
                 self.answer_seat("c", "No verdict line at all.\nConfidence: low\n")]
        self.assertEqual(self.run_council(self.cfg(seats)), 0)
        summ = self.summary()
        recs = self.by_id(summ)
        self.assertEqual(recs["a"]["verdict"], "APPROVE")
        self.assertEqual(recs["b"]["verdict"], "REVISE")
        self.assertIn("verdict", recs["c"])
        self.assertIsNone(recs["c"]["verdict"])
        self.assertEqual(recs["c"]["status"], "ok")           # a missing line is never a seat failure
        self.assertEqual(summ["verdicts"], {"APPROVE": 1, "REVISE": 1, "BLOCK": 0, "n/a": 0, "missing": 1})
        chair_in = self.read("chair", "synthesis.md")          # echo-stdin chair: its own prompt
        self.assertIn("seat a | provider stub | model stub-model | status ok | verdict APPROVE", chair_in)
        self.assertIn("seat b | provider stub | model stub-model | status ok | verdict REVISE", chair_in)
        self.assertNotIn("seat c | provider stub | model stub-model | status ok | verdict", chair_in)
        with open(os.path.join(self.out, "seats", "a.json")) as f:
            self.assertEqual(json.load(f)["verdict"], "APPROVE")
        snap = status.load(self.out)
        by = {s["id"]: s for s in snap["seats"]}
        self.assertEqual(by["a"]["verdict"], "APPROVE")
        self.assertIsNone(by["c"]["verdict"])
        table = status.render(snap)
        self.assertIn("verdict APPROVE", table)
        self.assertIn("verdict REVISE", table)

    def test_31_all_revise_is_no_gate(self):
        seats = [self.answer_seat(i, "Needs work.\nVerdict: REVISE\nConfidence: high\n") for i in "abc"]
        self.assertEqual(self.run_council(self.cfg(seats)), 0)
        summ = self.summary()
        self.assertEqual(summ["verdicts"]["REVISE"], 3)
        self.assertEqual(summ["chair"]["status"], "ok")
        self.assertTrue(os.path.exists(os.path.join(self.out, "chair", "synthesis.md")))

    def test_32_open_question_n_a(self):
        seats = [self.answer_seat("a", "Both are fine.\nVerdict: n/a\nConfidence: medium\n"), self.answer_seat("b", "x is better.\nVerdict: n/a\n")]
        self.assertEqual(self.run_council(self.cfg(seats)), 0)
        self.assertEqual(self.summary()["verdicts"]["n/a"], 2)
        self.assertIn("| verdict n/a", self.read("chair", "synthesis.md"))

    def test_33_failed_seat_has_no_verdict_key(self):
        seats = [self.answer_seat("a", "Ok.\nVerdict: APPROVE\n"), self.answer_seat("b", "Ok.\nVerdict: APPROVE\n"),
                 seat("c", "exit 1")]
        self.assertEqual(self.run_council(self.cfg(seats)), 0)
        c = self.by_id(self.summary())["c"]
        self.assertNotEqual(c["status"], "ok")
        self.assertNotIn("verdict", c)

    def test_40_lens_reaches_only_its_seat(self):
        seats = [seat("a", "echo-stdin", lens="security: abuse"), seat("b", "echo-stdin", lens="product: scope"),
                 seat("c", "echo-stdin")]
        self.assertEqual(self.run_council(self.cfg(seats, extra={"lenses": {"enabled": True}})), 0)
        a, b, c = (self.read("seats", "%s.md" % i) for i in "abc")
        self.assertIn("Your lens: security: abuse.", a)
        self.assertNotIn("product: scope", a)
        self.assertIn("Your lens: product: scope.", b)
        self.assertNotIn("security: abuse", b)
        self.assertNotIn("Your lens", c)
        recs = self.by_id(self.summary())
        self.assertEqual(recs["a"]["lens"], "security: abuse")
        self.assertNotIn("lens", recs["c"])
        chair_in = self.read("chair", "synthesis.md")
        self.assertIn("seat a | provider stub | model stub-model | status ok | lens security: abuse", chair_in)

    def test_41_lenses_off_per_run(self):
        seats = [seat("a", "echo-stdin", lens="security: abuse"), seat("b", "echo-stdin")]
        cfg = self.cfg(seats, extra={"lenses": {"enabled": True, "seat": {"b": "table lens"}}, "_lenses": "off"})
        self.assertEqual(self.run_council(cfg), 0)
        for i in "ab":
            self.assertNotIn("Your lens", self.read("seats", "%s.md" % i))
        self.assertNotIn("lens", self.by_id(self.summary())["a"])

    def test_42_template_without_lens_placeholder_still_runs(self):
        with open(os.path.join(self.prompts, "advisor.md"), "w") as f:
            f.write("SEAT $seat_id of $n_seats\nQ: $question\nC: $context\n")
        seats = [seat("a", "echo-stdin"), seat("b", "echo-stdin")]
        self.assertEqual(self.run_council(self.cfg(seats, extra={"lenses": {"enabled": True, "seat": {"a": "x"}}})), 0)


class Cli(unittest.TestCase):
    def test_50_flags_parse_on_both_commands(self):
        for cmd in ("preflight", "run"):
            args = council._parser().parse_args([cmd, "--lenses", "off", "--lens", "a=x", "--lens", "b=y"])
            self.assertEqual((args.lenses, args.lens), ("off", ["a=x", "b=y"]))
        self.assertIsNone(council._parser().parse_args(["run", "--", "q"]).lenses)
        with self.assertRaises(SystemExit):
            council._parser().parse_args(["run", "--lenses", "maybe"])

    def test_51_load_sets_underscore_keys_and_rejects_unknown_seat(self):
        args = council._parser().parse_args(["run", "--lenses", "off", "--lens", "claude=security", "--", "q"])
        cfg, err = council._load(args)
        self.assertIsNone(err)
        self.assertEqual((cfg["_lenses"], cfg["_lens_overrides"]), ("off", {"claude": "security"}))
        args = council._parser().parse_args(["run", "--lens", "nobody=x", "--", "q"])
        cfg, err = council._load(args)
        self.assertIsNone(cfg)
        self.assertIn("no such seat: nobody", err)
        args = council._parser().parse_args(["run", "--lens", "bad", "--", "q"])
        self.assertIn("expected SEAT=TEXT", council._load(args)[1])

    def test_52_run_flags_do_not_raise_drift_rows(self):
        from councillib import drift
        args = council._parser().parse_args(["run", "--lenses", "off", "--lens", "claude=x", "--", "q"])
        cfg, _ = council._load(args)
        items = drift.collect(cfg) if hasattr(drift, "collect") else {}
        self.assertFalse([k for k in items if "_lens" in k])


if __name__ == "__main__":
    unittest.main()
