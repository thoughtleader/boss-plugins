"""Answer validity + question path lint (run example-run, row 2). Stub CLIs only, no paid call.

Fixtures: openrouter-toolcall-glm.sse is the GLM seat's raw of run 20261030T090651Z (trimmed: 4 reasoning chunks kept,
every content chunk, finish and usage); answers/ holds answers that must pass (052203Z-*: synthetic seat reviews of a made-up
todo app, ~1.5 KB each, real-answer shape; and the 2+2 smoke run 20261021T091457Z in full).

Run: python3 -Es -m unittest tests.test_answer_check   (cwd ~/.claude/council)
"""
import contextlib
import glob
import io
import json
import os
import sys
import tempfile
import types
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import council  # noqa: E402
from councillib import answer_check, config, council_run, preflight, providers  # noqa: E402
from tests.test_council_run import Base, seat  # noqa: E402
from tests.test_or_fallback import FastFailBase, FStub, fseat  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
ON = {"enabled": True, "min_chars": 16, "min_prose_chars": 200}
ANSWER = ("The draft is sound: it names the gap, cites the precedent and proposes one change that fits the repo. "
          "Sharpen the opening line and drop the second example. Confidence: high.")
MARKUP = ("I'll read the relevant files to evaluate the draft issue<tool_call>Read<arg_key>file_path</arg_key>"
          "<arg_value>/home/x/issue.md</arg_value>\n")


def glm_text():
    ex = providers.OpenRouterExtractor()
    with open(os.path.join(FIX, "openrouter-toolcall-glm.sse"), "rb") as f:
        ex.feed(f.read())
    return ex


class CheckTests(unittest.TestCase):
    def test_01_the_090651Z_glm_answer_is_tool_call_markup(self):
        ex = glm_text()
        self.assertIsNone(ex.error())
        final = ex.final_text()
        self.assertIn("<tool_call>Read", final)
        why = answer_check.problem(final, ON)
        self.assertTrue(why.startswith("tool_call_markup"), why)
        self.assertEqual(answer_check.prose(final, answer_check.blocks(final)),
                         "I'll read the relevant files to evaluate the draft issue")

    def test_02_real_answers_pass(self):
        files = sorted(glob.glob(os.path.join(FIX, "answers", "*.md")))
        self.assertGreaterEqual(len(files), 10)
        for f in files:
            with open(f) as fh:
                self.assertIsNone(answer_check.problem(fh.read(), ON), os.path.basename(f))

    def test_03_near_empty(self):
        self.assertEqual(answer_check.problem("  ok.\n", ON), "near_empty: 3 chars")
        self.assertIsNone(answer_check.problem("2 + 2 = 4.\n\nConfidence: high", ON))     # qwen's 28 chars

    def test_04_markup_forms(self):
        for text in ("<|tool_call|>read_file(path='/x')<|tool_call_end|>",
                     "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>read<｜tool▁calls▁end｜>",
                     "<function_calls><invoke name=\"Read\"><parameter name=\"p\">/x</parameter></invoke></function_calls>",
                     '{"name": "read_file", "arguments": {"path": "/home/x/a.md"}}',
                     'Let me look.\n{"type": "function", "function": {"name": "read"}}',
                     '[TOOL_CALLS] [{"name": "read", "arguments": {}}]',
                     MARKUP):
            self.assertTrue((answer_check.problem(text, ON) or "").startswith("tool_call_markup"), text)

    def test_05_markup_inside_code_or_beside_a_real_answer_passes(self):
        quoted = "The seat returned `<tool_call>Read<arg_key>` instead of prose.\n\n```\n<tool_call>Read\n```\n" + ANSWER
        self.assertIsNone(answer_check.problem(quoted, ON))
        self.assertIsNone(answer_check.problem(ANSWER + " " + ANSWER + "\n<tool_call>Read x</tool_call>", ON))
        self.assertIsNone(answer_check.problem('In JSON: {"name": "x", "arguments": {}} is a call. ' + ANSWER * 2, ON))

    def test_05b_prose_that_names_a_tag_is_not_markup(self):
        # Review row 3 gap 2: an unclosed opener named in plain prose used to run to the end and eat the answer.
        body = ("The seat returned markup and nothing else, which means the prompt asked it to read a file it could "
                "not open. ") * 10
        self.assertGreater(len(body), 1000)
        for text in ("Paragraph one: it emitted <tool_call> markup instead of an answer.\n\n" + body,        # ~1192 chars
                     "Anthropic's old format used <function_calls> blocks. " + body,
                     "Models write <tool_call> and <function_calls> markup. " + body,
                     "See `<tool_call>` and <invoke> in the log.\n\n```\n<tool_call>Read<arg_key>p</arg_key>\n```\n" + body):
            self.assertEqual(answer_check.blocks(text) if "```" not in text else [], [], text[:60])
            self.assertIsNone(answer_check.problem(text, ON), text[:60])
        # a bare opener that starts a line is a markup run (rest of the line only), still flagged when nothing else
        # is there, and never swallows a long answer after it
        self.assertTrue(answer_check.problem("<tool_call>\nRead\n/x.md", ON).startswith("tool_call_markup"))
        self.assertIsNone(answer_check.problem("<tool_call>Read\n\n" + body, ON))
        # structured markup beside real prose is still cut out and counted
        self.assertTrue(answer_check.problem("Let me look.<tool_call>Read<arg_key>p</arg_key>", ON)
                        .startswith("tool_call_markup"))

    def test_06_off_and_absent(self):
        self.assertIsNone(answer_check.problem(MARKUP, None))
        self.assertIsNone(answer_check.problem(MARKUP, dict(ON, enabled=False)))
        self.assertFalse(answer_check.cfg_of({})["enabled"])
        self.assertTrue(answer_check.cfg_of(config.load())["enabled"])          # config.toml ships it on

    def test_07_bad_table_is_a_preflight_fail(self):
        self.assertEqual(answer_check.problems({}), [])
        self.assertEqual(answer_check.problems(config.load()), [])
        bad = answer_check.problems({"answer_check": {"enabled": "yes", "min_chars": 0, "min_prose_chars": True}})
        self.assertEqual(len(bad), 3, bad)
        rows = [c for c in preflight.engine_stage({"answer_check": {"min_chars": -1}}) if c.check == "answer_check"]
        self.assertEqual(rows[0].result, "FAIL")

    def test_08_classify_gives_invalid_answer_with_the_text_as_partial(self):
        ad = providers.get("openrouter")
        t = council_run._Task("s", "seat", {"id": "s", "provider": "openrouter", "model": "z-ai/glm-5"}, ad)
        t.extractor = glm_text()
        t.result = types.SimpleNamespace(status="ok", stderr_tail=b"", truncated=False)
        t.answer_check = ON
        self.assertFalse(council_run._classify(t))
        self.assertEqual(t.status, "invalid_answer")
        self.assertTrue(t.detail.startswith("tool_call_markup"))
        self.assertIn("<tool_call>", t.partial)
        self.assertIn("invalid_answer", council_run.SALVAGE_STATUSES)
        self.assertTrue(council_run._salvage_eligible(t, {"enabled": True}))
        t.prompt_text = "Q"
        sp = council_run._salvage_prompt(t, 120000)
        self.assertIn("ended (invalid_answer)", sp)
        self.assertIn("not an answer (tool_call_markup)", sp)
        # a canary / orphan task (no answer_check set) is never checked
        t2 = council_run._Task("c", "seat", t.seat, ad)
        t2.extractor, t2.result = glm_text(), t.result
        self.assertTrue(council_run._classify(t2))


class AnswerCheckRunTests(Base):
    """Through council_run.run with stub CLIs: the markup seat is invalid_answer, salvaged, and only then counts."""

    def files(self):
        mk = os.path.join(self.tmp, "markup.txt")
        ok = os.path.join(self.tmp, "answer.txt")
        with open(mk, "w") as f:
            f.write(MARKUP)
        with open(ok, "w") as f:
            f.write(ANSWER + "\n")
        return mk, ok

    def cfg3(self, seats, ac=True, salvage=True, **kw):
        ex = {"answer_check": dict(ON, enabled=ac)}
        if salvage:
            ex["salvage"] = {"enabled": True, "timeout": 30}
        return self.cfg(seats, chair_mode="replay %s 0" % self.files()[1], extra=ex, **kw)

    def test_r1_markup_seat_is_salvaged_then_counts(self):
        mk, ok = self.files()
        seats = [seat("a", "replay %s 0" % ok), seat("m", "replay %s 0" % mk, stub_salvage_mode="replay %s 0" % ok)]
        code = self.run_council(self.cfg3(seats, min_ok="n"))
        self.assertEqual(code, 0)
        m = self.by_id(self.summary())["m"]
        self.assertEqual((m["status"], m["salvaged"]), ("ok", True))
        self.assertEqual(m.get("salvaged_from"), "invalid_answer")

    def test_r2_markup_seat_without_salvage_is_not_counted(self):
        mk, ok = self.files()
        seats = [seat("a", "replay %s 0" % ok), seat("m", "replay %s 0" % mk)]
        code = self.run_council(self.cfg3(seats, salvage=False, min_ok="n"))
        self.assertEqual(code, 3)                                   # quorum 2 of 2 needed, 1 ok
        m = self.by_id(self.summary())["m"]
        self.assertEqual(m["status"], "invalid_answer")
        self.assertTrue(m["detail"].startswith("tool_call_markup"))
        self.assertIn("FAILED status=invalid_answer", self.read("seats", "m.md"))

    def test_r3_check_off_counts_it_as_before(self):
        mk, ok = self.files()
        seats = [seat("a", "replay %s 0" % ok), seat("m", "replay %s 0" % mk)]
        self.assertEqual(self.run_council(self.cfg3(seats, ac=False, salvage=False, min_ok="n")), 0)
        self.assertEqual(self.by_id(self.summary())["m"]["status"], "ok")

    def test_r4_markup_chair_is_salvaged(self):
        mk, ok = self.files()
        cfg = self.cfg3([seat("a", "replay %s 0" % ok), seat("b", "replay %s 0" % ok)], min_ok="n")
        cfg["chair"].update(stub_mode="replay %s 0" % mk, stub_salvage_mode="replay %s 0" % ok)
        self.assertEqual(self.run_council(cfg), 0)
        ch = self.summary()["chair"]
        self.assertEqual((ch["salvaged"], ch.get("salvaged_from")), (True, "invalid_answer"))


class MStub(FStub):
    """FStub whose original call streams the markup as an ok SSE answer (finish=stop)."""
    name = "fstub"

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        launch = super().build(seat, prompt_path, workdir, limits, cfg, launch_opts)
        if seat.get("stub_original") == "markup" and not (seat.get("_reroute") or seat.get("_standin")
                                                          or seat.get("_salvage")):
            ev = {"id": "gen-1", "model": seat["model"], "choices": [{"delta": {"content": MARKUP},
                                                                     "finish_reason": "stop"}]}
            sse = "data: %s\n\ndata: [DONE]\n\n" % json.dumps(ev)
            launch.argv = [sys.executable, "-Es", "-c", "import sys; sys.stdout.write(%r)" % sse]
        return launch


class FastFailWiringTests(FastFailBase):
    """With salvage off, an OpenRouter-style seat whose answer is markup goes down the row 1 fast-fail chain (the markup
    partial is not something to salvage); with salvage on it is salvaged first, as every salvage status is."""

    def setUp(self):
        super().setUp()
        providers.register(MStub())

    def good(self, sid):
        path = os.path.join(self.tmp, "answer.txt")
        with open(path, "w") as f:
            f.write(ANSWER + "\n")
        return seat(sid, "replay %s 0" % path)

    def test_f1_markup_with_salvage_off_is_rerouted(self):
        seats = [self.good("a"), fseat("f", stub_original="markup", stub_fallback="ok")]
        cfg = self.fcfg(seats, min_ok="n")
        cfg["answer_check"] = dict(ON)
        cfg["chair"]["stub_mode"] = self.good("x")["stub_mode"]
        self.assertEqual(self.run_council(cfg), 0)
        f = self.seat_json("f")
        self.assertEqual((f["status"], f["answered_by"]), ("ok", "fallback"))
        self.assertEqual(f["recovered"]["from_status"], "invalid_answer")
        self.assertEqual([s for s, _ in self.steps()], ["original", "fallback"])

    def test_f2_markup_with_salvage_on_is_salvaged_first(self):
        seats = [self.good("a"), fseat("f", stub_original="markup", stub_salvage="ok")]
        cfg = self.fcfg(seats, salvage=True, min_ok="n")
        cfg["answer_check"] = dict(ON)
        cfg["chair"]["stub_mode"] = self.good("x")["stub_mode"]
        self.assertEqual(self.run_council(cfg), 0)
        f = self.seat_json("f")
        self.assertEqual((f["status"], f["salvaged_from"]), ("ok", "invalid_answer"))


Q090651 = ("Evaluate one draft GitHub issue: /home/example/.claude/orchestrator/runs/2026-10-29-example-run/"
           "todo-3-notes-draft.md (read it from disk). Repo precedent: "
           "/home/example/.claude/orchestrator/runs/2026-10-29-example-run/3-precedent.md (PR #153).")


class PathLintTests(unittest.TestCase):
    def test_p1_090651Z_question_fails_one_row_per_path(self):
        rows = preflight.question_rows(Q090651)
        self.assertEqual([r.result for r in rows], ["FAIL", "FAIL"])
        self.assertIn("pass --context-file /home/example/.claude/orchestrator/runs/2026-10-29-example-run/"
                      "todo-3-notes-draft.md", rows[0].detail)
        self.assertTrue(rows[1].detail.startswith("pass --context-file /home/example/") and
                        rows[1].detail.split()[2].endswith("3-precedent.md"))       # trailing ")" / "." dropped

    def test_p2_attached_files_and_their_directory_are_covered(self):
        d = "/home/example/.claude/orchestrator/runs/2026-10-29-example-run/"
        ctx = [d + "todo-3-notes-draft.md", d + "3-precedent.md"]
        self.assertEqual([r.result for r in preflight.question_rows(Q090651, ctx)], ["PASS"])
        q = "issue-1.md in ~/.claude/orchestrator/runs/2026-10-29-example-run/ (context: 3-precedent.md)"
        self.assertEqual(preflight.question_rows(q, [os.path.expanduser("~/.claude/orchestrator/runs/"
                                                                        "2026-10-29-example-run/3-precedent.md")]
                                                 )[0].result, "PASS")
        self.assertEqual(preflight.question_rows(q)[0].result, "FAIL")

    def test_p3_forms_and_non_paths(self):
        self.assertEqual(preflight.question_paths("see (/tmp/x.log), ~/a/b.md. and /mnt/c/x; /home/u/y`"),
                         ["/tmp/x.log", "~/a/b.md", "/mnt/c/x", "/home/u/y"])
        self.assertEqual(preflight.question_paths("https://x.com/home/a, a/home/b, /usr/bin/env, ~ alone, 1/2"), [])
        self.assertEqual(preflight.question_rows(None), [])
        self.assertEqual(preflight.question_rows("What is 2+2?")[0].result, "PASS")

    def test_p4_allow_paths_is_warn(self):
        rows = preflight.question_rows(Q090651, allow_paths=True)
        self.assertEqual([r.result for r in rows], ["WARN", "WARN"])
        self.assertFalse(preflight.blocking_fails(rows))
        self.assertEqual(preflight.failed_providers(preflight.question_rows(Q090651)), set())   # policy, never a seat

    def test_p5_preflight_cli_fails_before_free_checks(self):
        with tempfile.TemporaryDirectory() as td:
            qf = os.path.join(td, "q.md")
            with open(qf, "w") as f:
                f.write(Q090651)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), unittest.mock.patch.object(preflight, "free_stage") as free:
                code = council.main(["preflight", "--free-only", "--question-file", qf])
            self.assertEqual(code, 1)
            free.assert_not_called()
            self.assertIn("question_path", out.getvalue())
            self.assertIn("PREFLIGHT FAIL (2) at policy", out.getvalue())
            ctx = os.path.join(td, "missing.md")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(council.main(["preflight", "--question-file", qf, "--context-file", ctx]), 2)

    def test_p6_run_cli_has_the_flags(self):
        args = council._parser().parse_args(["run", "--allow-paths", "--context-file", "a", "--", "q"])
        self.assertTrue(args.allow_paths)
        args = council._parser().parse_args(["preflight", "--allow-paths", "--question-file", "q", "--context-file", "a"])
        self.assertEqual((args.allow_paths, args.question_file, args.context_file), (True, "q", ["a"]))



if __name__ == "__main__":
    unittest.main()
