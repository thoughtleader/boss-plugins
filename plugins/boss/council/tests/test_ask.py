"""`council.py ask` (P27), `--rulings-file` (P23, truth 7), the whole-file deny list and the leaked-key scan in the
plugin. Stub CLI seats in the suite's scratch HOME only: no network, no paid call. Every fixture is synthetic."""
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import council
from councillib import council_run, preflight, report
from councillib.providers import Check

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS = os.path.join(ROOT, "prompts")
SCAN = os.path.join(ROOT, "tools", "secret-scan.sh")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(ROOT)))
SKILL_DIR = os.path.join(os.path.dirname(ROOT), "skills", "council")

RULINGS = ("Keep the release on the existing branch model.\n"
           "The test suite stays in a scratch home.\n")


def fake_key():
    return "sk-" + "Q7x" * 8               # built at run time: no key-shaped literal in this file (CI scans the repo)


def overlay(tmp, seats=(("a", ()), ("b", ())), chair_mode="quick", extra=""):
    lines = ['prompts_dir = "%s"' % PROMPTS, 'state_dir = "%s"' % tmp,
             "[chair]", 'provider = "stub"', 'model = "chair-m"', 'stub_mode = "%s"' % chair_mode,
             "[providers.stub]", 'mode = "stream"',
             "[canary]", 'prompt = "Reply with exactly: OK"', "timeout_s = 20", "idle_s = 10",
             "[quorum]", 'min_ok = "n-1"', "straggler_grace_s = 5", "[answer_check]", "min_chars = 1"]
    for sid, extras in seats:
        lines += ["[[seat]]", 'id = "%s"' % sid, 'provider = "stub"', 'model = "m-%s"' % sid] + list(extras)
        if not any(e.startswith("stub_mode") for e in extras):
            lines.append('stub_mode = "quick"')
    lines.append(extra)
    path = os.path.join(tmp, "ov-%d.toml" % len(os.listdir(tmp)))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def call(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = council.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def read(path):
    with open(path) as f:
        return f.read()


def run_dir_of(out):
    return [l for l in out.splitlines() if l.startswith("run dir: ")][0][9:]


def tree(path):
    out = set()
    for d, _, files in os.walk(path):
        for f in files:
            out.add(os.path.relpath(os.path.join(d, f), path))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ask-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path


class AskGate(Base):
    def test_01_fail_row_exit_1_no_run_dir_nothing_launched(self):
        ov = overlay(self.tmp, seats=(("a", ()), ("b", ("stub_fail_free = true",))))
        out_parent = os.path.join(self.tmp, "runs")
        with mock.patch.object(council, "launch", side_effect=AssertionError("launched")), \
                mock.patch.object(council_run, "run", side_effect=AssertionError("ran")):
            rc, out, err = call(["ask", "--config", ov, "--out", out_parent, "--", "What is 2+2?"])
        self.assertEqual(rc, 1, out + err)
        self.assertIn("PREFLIGHT FAIL", out)
        self.assertIn("no run dir: nothing was sent to the seats", out)
        self.assertNotRegex(out, r"(?m)^run dir: ")
        self.assertFalse(os.path.exists(out_parent))
        self.assertNotIn("current.json", os.listdir(self.tmp))

    def test_02_clean_ask_leaves_the_same_run_dir_and_code_as_run(self):
        ov = overlay(self.tmp)
        q = self.write("q.md", "What is 2+2?")
        rc1, out1, err1 = call(["run", "--config", ov, "--question-file", q, "--out", os.path.join(self.tmp, "r1"),
                                "--slug", "same"])
        rc2, out2, err2 = call(["ask", "--config", ov, "--question-file", q, "--out", os.path.join(self.tmp, "r2"),
                                "--slug", "same"])
        self.assertEqual((rc1, rc2), (0, 0), out1 + err1 + out2 + err2)
        d1, d2 = run_dir_of(out1), run_dir_of(out2)
        self.assertEqual(tree(d1), tree(d2))
        s1, s2 = (json.loads(read(os.path.join(d, "summary.json"))) for d in (d1, d2))
        self.assertEqual((s1["exit_code"], s2["exit_code"]), (0, 0))
        self.assertEqual({s["id"]: s["status"] for s in s1["seats"]}, {s["id"]: s["status"] for s in s2["seats"]})
        self.assertEqual(read(os.path.join(d1, "question.md")), read(os.path.join(d2, "question.md")))
        # the table, run's own lines, then the report block, in that order
        i_table, i_run, i_exit = out2.index("PREFLIGHT PASS"), out2.index("run dir: "), out2.index("exit: 0")
        i_rep, i_end = out2.index("== council report"), out2.index("== end of report ==")
        self.assertLess(i_table, i_run)
        self.assertLess(i_run, i_exit)
        self.assertLess(i_exit, i_rep)
        self.assertLess(i_rep, i_end)
        rep = out2[i_rep:]
        for want in ("seats:\n", "cost: ", "phase: ", "quorum: ", "synthesis: %s" % os.path.join(d2, "chair", "synthesis.md"),
                     "leaked-key scan: clean"):
            self.assertIn(want, rep)
        self.assertIn("PASS row(s) not shown (full table: %s)" % os.path.join(d2, "preflight.txt"), out2)
        self.assertIn("PREFLIGHT PASS", read(os.path.join(d2, "preflight.txt")))

    def test_03_free_checks_and_canaries_run_once(self):
        ov = overlay(self.tmp)
        counts = {"free": 0, "canary": 0}
        free, canary = preflight.free_stage, preflight.canary_stage

        def f(*a, **k):
            counts["free"] += 1
            return free(*a, **k)

        def c(*a, **k):
            counts["canary"] += 1
            return canary(*a, **k)
        with mock.patch.object(preflight, "free_stage", f), mock.patch.object(preflight, "canary_stage", c):
            rc, out, err = call(["ask", "--config", ov, "--out", os.path.join(self.tmp, "r"), "--", "What is 2+2?"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(counts, {"free": 1, "canary": 1})

    def test_04_warn_rows_are_printed_and_do_not_block(self):
        ov = overlay(self.tmp)
        rc, out, err = call(["ask", "--config", ov, "--allow-paths", "--out", os.path.join(self.tmp, "r"),
                             "--", "Is /tmp/example/notes.txt relevant?"])
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"question_path\s+\| WARN")                         # in the short table
        notes = out[out.index("preflight notices"):]
        self.assertIn("WARN policy/question_path: --allow-paths: /tmp/example/notes.txt", notes)

    def test_05_usage_errors_exit_2(self):
        ov = overlay(self.tmp)
        with mock.patch.object(council, "launch", side_effect=AssertionError("launched")):
            self.assertEqual(call(["ask", "--config", ov])[0], 2)
            self.assertEqual(call(["ask", "--config", ov, "--context-file", "/nope", "--", "q"])[0], 2)
            self.assertEqual(call(["ask", "--config", ov, "--question-file", "/nope"])[0], 2)
            self.assertEqual(call(["ask", "--config", ov, "--free-only", "--", "q"])[0], 2)    # not an ask flag

    def test_06_help_names_order_exit_codes_and_rules(self):
        p = subprocess.run([os.path.join(ROOT, "council.py"), "ask", "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        for want in ("--rulings-file", "exit 1, no run dir", "128+N", "DECISIONS.md", "MEMORY.md", "--depth standard",
                     "--grounded", "leaked-key scan", "never killed or capped"):
            self.assertIn(want, p.stdout)

    def test_07_run_preflight_drift_keep_their_flags(self):
        p = council._parser()
        for cmd in ("run", "preflight", "ask"):
            a = p.parse_args([cmd, "--rulings-file", "r.md"] + (["--", "q"] if cmd != "preflight" else []))
            self.assertEqual(a.rulings_file, "r.md")
        self.assertTrue(p.parse_args(["preflight", "--free-only"]).free_only)
        self.assertIsNone(p.parse_args(["run", "--", "q"]).rulings_file)


class Rulings(Base):
    def test_10_heading_reaches_every_seat_and_the_chair(self):
        ov = overlay(self.tmp, seats=(("a", ('stub_mode = "echo-stdin"',)), ("b", ('stub_mode = "echo-stdin"',))),
                     chair_mode="echo-stdin")
        rf = self.write("excerpt.md", RULINGS)
        rc, out, err = call(["ask", "--config", ov, "--rulings-file", rf, "--out", os.path.join(self.tmp, "r"),
                             "--", "What is 2+2?"])
        self.assertEqual(rc, 0, out + err)
        d = run_dir_of(out)
        head = "Settled rulings (%s)" % preflight.RULINGS_LABEL
        for sid in ("a", "b"):
            text = read(os.path.join(d, "seats", sid + ".md"))       # an echo seat answers with its prompt
            self.assertIn("### " + head, text)
            self.assertIn("Keep the release on the existing branch model.", text)
        synth = read(os.path.join(d, "chair", "synthesis.md"))       # the echo chair: its own prompt
        self.assertRegex(synth, r"(?m)^## " + re.escape(head))
        self.assertEqual(preflight.RULINGS_LABEL, "settled; raise only if you find a defect")

    def test_11_chair_and_context_unchanged_without_rulings(self):
        tpl = council_run._load_template({"prompts_dir": PROMPTS}, "chair.md")
        plain = council_run.chair_prompt(tpl, "Q", "A")
        self.assertEqual(council_run.chair_prompt(tpl, "Q", "A", rulings=None), plain)
        self.assertEqual(council_run.chair_prompt(tpl, "Q", "A", rulings={"text": "  \n"}), plain)
        withr = council_run.chair_prompt(tpl, "Q", "A", rulings={"text": RULINGS})
        self.assertTrue(withr.startswith(plain.rstrip("\n")))
        self.assertIn("## Settled rulings (settled; raise only if you find a defect)", withr)
        ctx = self.write("c.txt", "ctx body")
        self.assertEqual(council_run._context_block([ctx]), council_run._context_block([ctx], None))
        self.assertEqual(council_run._context_block([]), "(none)")
        block = council_run._context_block([ctx], {"text": RULINGS})
        self.assertLess(block.index("### Settled rulings"), block.index("### " + ctx))

    def test_12_each_kind_of_hit_refuses_with_exit_2_and_no_text(self):
        ov = overlay(self.tmp)
        cases = {
            "email address": "Reply to someone@example.invalid about it.",
            "URL": "The page is at https://example.invalid/page today.",
            "path under a home directory": "The draft sits in /home/user/drafts/plan.md for now.",
            "key-shaped string": "Rotate %s soon." % fake_key(),
        }
        for kind, line in cases.items():
            rf = self.write("r-%d.md" % len(kind), "First ruling stays.\n" + line + "\n")
            for cmd in ("ask", "run", "preflight"):
                with mock.patch.object(council, "launch", side_effect=AssertionError("launched")), \
                        mock.patch.object(preflight, "run", side_effect=AssertionError("preflight ran")):
                    argv = [cmd, "--config", ov, "--rulings-file", rf, "--out", os.path.join(self.tmp, "r"), "--", "q"]
                    if cmd == "preflight":
                        argv = [cmd, "--config", ov, "--rulings-file", rf]
                    rc, out, err = call(argv)
                self.assertEqual(rc, 2, (kind, cmd, out, err))
                self.assertIn("line 2: %s" % kind, err)
                self.assertIn("nothing sent", err)
                for frag in re.findall(r"[\w@/.:-]{6,}", line):
                    self.assertNotIn(frag, out + err, (kind, frag))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "r")))

    def test_13_clean_excerpt_and_ordinary_context_with_email_pass(self):
        self.assertEqual(preflight.rulings_hits(RULINGS), [])
        ov = overlay(self.tmp)
        src = self.write("src/module.py", "# Author: Someone <someone@example.invalid>\nX = 1\n")
        rc, out, err = call(["ask", "--config", ov, "--context-file", src, "--out", os.path.join(self.tmp, "r"),
                             "--", "Is X right?"])
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("private_file", out)

    def test_14_whole_rulings_or_memory_file(self):
        ov = overlay(self.tmp)
        files = [self.write("run/DECISIONS.md", RULINGS), self.write("notes/memory.md".replace("memory", "MEMORY"), RULINGS),
                 self.write("home/.claude/projects/-some-slug/memory/feedback_x.md", RULINGS)]
        for f in files:
            for flag in ("--context-file", "--rulings-file"):
                with mock.patch.object(council, "launch", side_effect=AssertionError("launched")), \
                        mock.patch.object(preflight, "run", side_effect=AssertionError("preflight ran")):
                    rc, out, err = call(["ask", "--config", ov, flag, f, "--out", os.path.join(self.tmp, "r"), "--", "q"])
                self.assertEqual(rc, 2, (f, flag, out, err))
                self.assertIn("pass it with --rulings-file", err)
            # every form refuses (owner ruling 2026-11-18): preflight shows a FAIL row and exits 1
            rc, out, err = call(["preflight", "--free-only", "--config", ov, "--context-file", f])
            self.assertEqual(rc, 1, out + err)
            self.assertRegex(out, r"private_file\s+\| FAIL")
        launch = mock.Mock(side_effect=AssertionError("launched"))
        with mock.patch.object(council, "launch", launch):
            rc, out, err = call(["run", "--config", ov, "--context-file", files[0], "--out", os.path.join(self.tmp, "rr"),
                                 "--", "q"])
        self.assertEqual(rc, 2, out + err)                    # run: refused before any seat or chair, nothing sent
        self.assertEqual(launch.call_count, 0)
        self.assertIn("run refused, nothing sent: %s is a whole DECISIONS.md file" % files[0], err)
        self.assertIn("pass it with --rulings-file", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "rr")))
        self.assertIsNone(preflight.private_file(self.write("notes/decisions-summary.md", "x")))
        self.assertIsNone(preflight.private_file(self.write("docs/memory/notes.md", "x")))       # not under .claude/projects

    def test_15_one_constant(self):
        self.assertEqual(preflight.PRIVATE_FILES, ("DECISIONS.md", "MEMORY.md", ".claude/projects/*/memory/"))
        src = read(os.path.join(ROOT, "council.py"))
        self.assertNotIn('"DECISIONS.md"', src)


def _j(*parts):
    return "".join(parts)                  # key-shaped probes are built at run time (CI and push protection scan the repo)


# Synthetic fixtures in the real files' SHAPES only (line forms, headings, frontmatter keys); no real text.
WHOLE_DECISIONS = "\n".join([
    "# Decisions — 2099-01-01-sample-widget-run", "", "Mode: away", "",
    "- 2099-01-01 Owner ruled the widget stays blue (#3.1).",
    "- 2099-01-01 Owner picked the short label for the button (#3.2).",
    "- 2099-01-02 Row 4 retried one step up after a weak pass (#4.2).",
    "- 2099-01-02 Owner approved the plan with two extra rows.",
    "- 2099-01-03 The review found two gaps; both patched in place.",
    "- 2099-01-03 OWNER PICKS: option one for the banner, option two for the footer.",
    "- 2099-01-04 Owner said the run closes after the last wave.",
    "- 2099-01-04 Away mode decided the retry within the stated limit.", ""])
WHOLE_INDEX = "\n".join(["- Widgets: [blue widget rule](feedback_widget_blue.md) · [short labels](feedback_short_labels.md)",
                         "- Buttons: [one size](project_button_size.md) — keep one size",
                         "- Forms: [no nested scroll](feedback_no_nested_scroll.md) · [labels above](feedback_labels_above.md)",
                         "- Tools: [use npm](reference_use_npm.md) · [run tests in scratch](feedback_tests_in_scratch.md)",
                         "- Owner: [plain words](user_plain_words.md) — short sentences",
                         "- Site: [light only](project_light_only.md) · [desktop first](feedback_desktop_first.md)", ""])
WHOLE_MEMORY = "\n".join(["---", "name: widget-colour", 'description: "The widget stays blue across every page"',
                          "metadata:", "  type: feedback", "---", "", "The widget stays blue.", "",
                          "**Why:** one colour reads as one control.", ""])
EXCERPT = "\n".join(["The widget stays blue on every page.", "Buttons keep one size.", "Labels sit above their fields.",
                     "The owner wants plain words in error messages.", "Tests run in a scratch home.",
                     "A retry goes one step up.", "The release keeps the existing branch model.",
                     "Forms never scroll inside the page.", "The footer keeps the short label.",
                     "The plan closes after the last wave.", ""])
CHANGELOG = "\n".join(["# Changelog", "", "## 1.3.0 - 2099-01-05", "- 2099-01-05 Added the blue widget.",
                       "- 2099-01-04 Fixed the button size on small screens.", "- 2099-01-03 Dropped the old footer.",
                       "", "## 1.2.0 - 2099-01-01", "- 2099-01-01 First public release.",
                       "- 2099-01-01 Moved the docs into one folder.", ""])
PLAN_LIKE = "\n".join(["# 2099-01-01-sample-widget-run", "", "Goal: ship the blue widget; the owner approved the scope.", "",
                       "| # | Subtask | Agent type | Done when |", "|---|---|---|---|",
                       "| 1 | Build the widget (#1.1) | builder | the owner sees it blue |",
                       "| 2 | Review it (#2.1) | reviewer | the owner reads the verdict |",
                       "| 3 | Fix findings (#3.1) | builder | the owner says go |", "",
                       "- 2099-01-01 Wave 1 launched.", "- 2099-01-02 Wave 2 launched.", "- 2099-01-03 Review done.",
                       "Status: the owner decides after the review; quoting the OWNER PICKS marker does not make a log.", ""])
README = "\n".join(["# Widget", "", "A small widget for forms.", "", "## Install", "", "Run the installer, then restart.",
                    "", "## Docs", "", "- [Guide](GUIDE.md)", "- [Changes](CHANGELOG.md)", "- [Contributing](CONTRIBUTING.md)",
                    "", "Owner: the forms team.", ""])

MUST_HIT = {
    "email address": ["Send the reply to jane.doe\n@example.com when ready.", "Contact jane.doe @ example.com",
                      "jane.doe(at)example(dot)com", "jane.doe [at] example [dot] com", "jane at example dot com"],
    "URL": ["The posting is at example.com/jobs/123", "See www.example.com/careers", "see example.org"],
    "path under a home directory": ["The notes live in $HOME/notes/a.md", "${HOME}/notes", "%USERPROFILE%\\Documents",
                                    "Saved to C:\\Users\\someone\\Documents\\cv.docx", "from /home/someone/x.md today"],
    "Claude memory-folder path": [".claude/projects/-some-slug/memory/feedback_x.md", "projects/-some-slug/memory/a.md"],
    "phone number": ["Call 415-555-0100 about it", "Call (415) 555-0100", "Call +44 20 7946 0000"],
}
MUST_NOT_HIT = ["PLAN.md", "council.py", "config.toml", "ledger.js", "lib/ledger.js", "0.163.0", "57d4fdc",
                "0123456789abcdef" * 2 + "01234567", "0123456789abcdef" * 4, "2026-11-18", "08:16 PST",
                "1,418 of 1,539", "20,084 B", "241.8k", "opus/xhigh", "orch/feature-merged", "gpt-6-astra", "#26.1",
                "1920x1060", "300000", "+21.5%", "+300000", "+1485", "up +3.2 points",
                "Look at council.py and lib/ledger.js before PLAN.md (#26.1, 1,418 of 1,539)."]


class Backstop(Base):
    """Pre-ship review gap 3 (F2): a renamed whole file that keeps its shape is caught by content (only in part on
    real files: private_content's limits); the rulings detectors are wider; the
    verb outcome lives in one table."""

    def refuse(self, flag, path):
        ov = overlay(self.tmp)
        with mock.patch.object(council, "launch", side_effect=AssertionError("launched")), \
                mock.patch.object(preflight, "run", side_effect=AssertionError("preflight ran")):
            return call(["ask", "--config", ov, flag, path, "--out", os.path.join(self.tmp, "r"), "--", "q"])

    def test_30_renamed_whole_files_are_caught_by_content(self):
        ov = overlay(self.tmp)
        for name, text, reason in (("notes/run-log.md", WHOLE_DECISIONS, "a whole DECISIONS.md by its content"),
                                   ("notes/index.md", WHOLE_INDEX, "a whole memory index by its content"),
                                   ("notes/widget.md", WHOLE_MEMORY, "a whole Claude memory file by its content")):
            f = self.write(name, text)
            self.assertEqual(preflight.rulings_hits(text), [], name)      # the content check, not the line check, fires
            for flag in ("--context-file", "--rulings-file"):
                rc, out, err = self.refuse(flag, f)
                self.assertEqual(rc, 2, (name, flag, out, err))
                self.assertIn("ask refused, nothing sent: %s is %s" % (f, reason), err)
                self.assertIn("pass it with --rulings-file", err)
                for line in text.splitlines():
                    if len(line) > 12:
                        self.assertNotIn(line.strip("- "), out + err)     # counts and kinds only, never the text
            for flag in ("--context-file", "--rulings-file"):             # preflight: a FAIL row, exit 1
                rc, out, err = call(["preflight", "--free-only", "--config", ov, flag, f])
                self.assertEqual(rc, 1, out + err)
                self.assertRegex(out, r"private_file\s+\| FAIL")
                self.assertIn(reason, out)
                launch = mock.Mock(side_effect=AssertionError("launched"))  # run: refused, zero launches
                with mock.patch.object(council, "launch", launch):
                    rc, out, err = call(["run", "--config", ov, flag, f, "--out", os.path.join(self.tmp, "rr"), "--", "q"])
                self.assertEqual(rc, 2, (name, flag, out, err))
                self.assertEqual(launch.call_count, 0)
                self.assertIn("run refused, nothing sent: %s is %s" % (f, reason), err)
                for line in text.splitlines():
                    if len(line) > 12:
                        self.assertNotIn(line.strip("- "), out + err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "r")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "rr")))

    def test_31_excerpt_changelog_and_readme_pass(self):
        ov = overlay(self.tmp)
        for name, text in (("excerpt.md", EXCERPT), ("CHANGELOG.md", CHANGELOG), ("README.md", README),
                           ("PLAN.md", PLAN_LIKE)):
            self.assertIsNone(preflight.private_content(text), name)
            f = self.write("ok/" + name, text)
            self.assertIsNone(preflight.private_file(f), name)
            rc, out, err = call(["preflight", "--free-only", "--config", ov, "--context-file", f])
            self.assertEqual(rc, 0, out + err)
            self.assertNotIn("private_file", out)
            rc, out, err = call(["run", "--config", ov, "--context-file", f, "--out", os.path.join(self.tmp, "r"), "--", "q"])
            self.assertEqual(rc, 0, out + err)
            self.assertNotIn("private_file", out)
        self.assertEqual(preflight.rulings_hits(EXCERPT), [])
        self.assertIsNone(preflight.private_content(WHOLE_DECISIONS.split("\n- 2099-01-02")[0]))   # a short head passes

    def test_32_wider_rulings_detectors(self):
        cases = dict(MUST_HIT)
        cases["key-shaped string"] = [_j("pplx", "-", "abcdef1234567890abcdef12"), _j("sk", "-or-v1-", "abcdef0123456789"),
                                      _j("gh", "p_", "abcdefghijklmnopqrstuvwxyz0123"), _j("AI", "za", "SyA" + "1" * 33),
                                      _j("AK", "IA", "ABCDEFGHIJKLMNOP"), _j("Authorization: Bear", "er ", "0123abcd" * 4),
                                      _j("api", "_key = ", "abcd1234efgh")]
        for kind, lines in cases.items():
            for text in lines:
                self.assertIn(kind, [h[1] for h in preflight.rulings_hits(text)], (kind, text))
        for text in MUST_NOT_HIT:
            self.assertEqual(preflight.rulings_hits(text), [], text)

    def test_33_table_scrub_and_drift_keep_the_narrow_key_shape(self):
        from councillib import drift
        self.assertEqual(preflight._KEYISH.pattern, r"sk-[A-Za-z0-9_\-]{6,}")
        self.assertEqual(drift._KEYISH.pattern, r"sk-[A-Za-z0-9_\-]{6,}")
        self.assertEqual(preflight._scrub(_j("pplx", "-", "abcdef1234567890abcdef12")), _j("pplx", "-", "abcdef1234567890abcdef12"))

    def test_34_outcome_is_one_table(self):
        # owner ruling 2026-11-18: every form refuses a whole rulings or memory file (truth 7 over truth 3)
        self.assertEqual(preflight.PRIVATE_OUTCOME, {"ask": "refuse", "run": "refuse", "preflight": "fail"})
        src = read(os.path.join(ROOT, "council.py"))
        self.assertNotIn("private_file(", src)                      # council.py only asks the table (private_refusal)
        self.assertNotIn("allow-private", src)                      # no override flag
        f = self.write("run/DECISIONS.md", RULINGS)
        ov = overlay(self.tmp)
        with mock.patch.dict(preflight.PRIVATE_OUTCOME, {"preflight": "refuse"}), \
                mock.patch.object(preflight, "run", side_effect=AssertionError("preflight ran")):
            rc, out, err = call(["preflight", "--free-only", "--config", ov, "--context-file", f])
            self.assertEqual(rc, 2, out + err)                      # one value switches a verb
            self.assertIn("preflight refused, nothing sent", err)
        for outcome, result in (("warn", ["WARN"]), ("fail", ["FAIL"]), ("refuse", [])):
            with mock.patch.dict(preflight.PRIVATE_OUTCOME, {"ask": outcome}):
                self.assertEqual([c.result for c in preflight.private_rows([f], "ask")], result)

    def test_35_ordinary_context_with_urls_paths_and_email_passes_every_verb(self):
        ov = overlay(self.tmp)
        f = self.write("src/notes.md", "# Notes\n\nAuthor: Someone <someone@example.invalid>\n"
                                       "See https://example.invalid/docs and www.example.com/x.\n"
                                       "Built from ~/work/x and /home/someone/y; call 415-555-0100.\n")
        self.assertNotEqual(preflight.rulings_hits(read(f)), [])     # the line detectors are for --rulings-file only
        rc, out, err = call(["preflight", "--free-only", "--config", ov, "--context-file", f])
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("private_file", out)
        for cmd in ("run", "ask"):
            rc, out, err = call([cmd, "--config", ov, "--context-file", f, "--out", os.path.join(self.tmp, cmd), "--",
                                 "Is it right?"])
            self.assertEqual(rc, 0, (cmd, out, err))
            self.assertNotIn("private_file", out)


class Scan(Base):
    def test_20_plugin_copy_exists_and_is_executable(self):
        self.assertTrue(os.path.isfile(SCAN))
        self.assertTrue(os.stat(SCAN).st_mode & stat.S_IXUSR)
        root = os.path.join(REPO, "scripts", "secret-scan.sh")
        if os.path.isfile(root):                                  # in the repo: the root path forwards to this copy
            self.assertIn("plugins/boss/council/tools/secret-scan.sh", read(root))

    def test_21_key_shaped_string_gives_its_file_name_clean_dir_clean(self):
        d = os.path.join(self.tmp, "run")
        os.makedirs(os.path.join(d, "seats"))
        with open(os.path.join(d, "seats", "a.md"), "w") as f:
            f.write("an answer\n")
        self.assertEqual(report.scan_line(d), "leaked-key scan: clean\n")
        with open(os.path.join(d, "seats", "b.md"), "w") as f:
            f.write("oops %s here\n" % fake_key())
        line = report.scan_line(d)
        self.assertEqual(line, "leaked-key scan: key-shaped strings in seats/b.md (file names only; open them by hand)\n")
        self.assertNotIn(fake_key(), line)
        self.assertIn("not run", report.scan_line(d, script=os.path.join(self.tmp, "missing.sh")))

    def test_22_ask_prints_the_scan_line_and_keeps_its_exit_code(self):
        ov = overlay(self.tmp)
        with mock.patch.object(report, "scan_line", return_value="leaked-key scan: key-shaped strings in seats/a.md\n"):
            rc, out, err = call(["ask", "--config", ov, "--out", os.path.join(self.tmp, "r"), "--", "What is 2+2?"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("leaked-key scan: key-shaped strings in seats/a.md", out)


def u(i, o, r, c):
    return {"input_tokens": i, "output_tokens": o, "reasoning_tokens": r, "cost_usd": c}


NONE4 = {"input_tokens": None, "output_tokens": None, "reasoning_tokens": None, "cost_usd": None}


def sample_summary():
    rec = lambda i, us, **kw: dict({"id": i, "provider": "p", "model": "m", "status": "ok", "total_s": 1.234,  # noqa: E731
                                    "truncated": False, "usage": us}, **kw)
    return {"seats": [rec("a", u(1, 50, 30, 0.25), verdict="APPROVE"), rec("b", NONE4, answered_by="hedge", late=[{}]),
                      rec("r", u(1, 8000, 7990, 0.2), salvaged=True,
                          salvage={"status": "ok", "model": "m", "total_s": 2.0, "usage": u(5, 40, 0, 0.01)}),
                      rec("g", u(1, 9, 0, 0.0), grounding={"mode": "grounded", "check": {"n_citations": 3, "n_bad": 1}},
                          grounded_ok=False, substituted={"model": "m2", "reason": "preflight:x"}, total_s=12.0),
                      {"id": "n", "provider": "p", "model": "m", "status": "not_launched"},
                      rec("c", u(1, 2, 0, 0.0), status="pending")],
            "chair": rec("chair", u(100, 20, 0, 0.5)), "quorum": {"need": 2, "ok": 3}, "cost_reported_usd": 0.96,
            "cost_unreported": ["b"], "phase": "done", "runner_pid": 42}


class Report(Base):
    def test_30_python_table_matches_the_reference_jq(self):
        ref = read(os.path.join(SKILL_DIR, "reference.md"))
        m = re.search(r"```\n(CAPPED=.*?)```", ref, re.S)
        self.assertIsNotNone(m)
        if not all(shutil.which(x) for x in ("jq", "column", "bash")):
            self.skipTest("jq/column not installed")
        run = os.path.join(self.tmp, "run")
        os.makedirs(os.path.join(run, "seats"))
        with open(os.path.join(run, "seats", "a.md"), "w") as f:
            f.write("text [TRUNCATED: the seat hit its output-token cap]\n")
        with open(os.path.join(run, "summary.json"), "w") as f:
            json.dump(sample_summary(), f)
        p = subprocess.run(["bash", "-c", m.group(1)], env=dict(os.environ, RUN=run), capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        jq_rows = [l.split() for l in p.stdout.splitlines() if l.strip() and not l.startswith("{")
                   and ": outcome" not in l]
        py_rows = [l.split() for l in report.seat_table(sample_summary(), report.capped_ids(run)).splitlines()]
        self.assertEqual(py_rows, jq_rows)

    def test_31_unknown_is_a_dash_flags_explained(self):
        text = report.block(self.tmp, None)
        self.assertIn("no summary.json", text)
        run = os.path.join(self.tmp, "run")
        os.makedirs(os.path.join(run, "chair"))
        with open(os.path.join(run, "summary.json"), "w") as f:
            json.dump(sample_summary(), f)
        with open(os.path.join(run, "chair", "synthesis.md"), "w") as f:
            f.write("".join("line %d\n" % i for i in range(1, 30)))
        checks = [Check("drift", "free", "drift:cli.x.version", "WARN", "CHANGED 1 -> 2"),
                  Check("p", "free", "version", "PASS", "fine")]
        text = report.block(run, checks, scan="leaked-key scan: clean\n")
        rows = {(l.split()[0], l.split()[1]): l.split() for l in text.split("seats:\n")[1].split("cost:")[0].splitlines()[1:]}
        self.assertEqual(rows[("advisor", "b")][9:12], ["-", "-", "-"])           # unknown, not 0
        self.assertEqual(rows[("advisor", "n")][9:12], ["-", "-", "-"])
        self.assertEqual(rows[("salvage", "r")][9:12], ["40", "0", "0.01"])
        self.assertEqual(rows[("chair", "chair")][9:12], ["20", "0", "0.5"])
        for f in ("PENDING", "FAILED", "BY_HEDGE", "LATE", "SALVAGED", "SUBSTITUTED", "UNVERIFIED_CITES"):
            self.assertIn("  %s: " % f, text)
        self.assertNotIn("  CAPPED: ", text)
        self.assertIn("WARN drift/drift:cli.x.version: CHANGED 1 -> 2", text)
        self.assertNotIn("version: fine", text)
        self.assertIn("line 15\n", text)
        self.assertNotIn("line 16\n", text)
        self.assertIn('"cost_unreported":["b"]', text)


class Skill(unittest.TestCase):
    def test_40_skill_names_ask_the_flag_the_label_and_the_scan(self):
        text = read(os.path.join(SKILL_DIR, "SKILL.md"))
        for want in ("council.py ask", "--rulings-file", "settled; raise only if you find a defect",
                     "tools/secret-scan.sh", "run_in_background", "reference.md"):
            self.assertIn(want, text)
        front = text.split("---")[1]
        self.assertIn("--rulings-file", re.search(r"argument-hint: (.*)", front).group(1))


if __name__ == "__main__":
    unittest.main()
