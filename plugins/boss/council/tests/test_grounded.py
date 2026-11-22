"""Row 13 (run example-run): grounded mode, read-only repo and image access for the seats that
can use tools, and the citation / quote check.

Stub processes and temp dirs only: no network, no paid call, no real claude/codex launch.

Run: python3 -Es -m unittest tests.test_grounded   (cwd ~/.claude/council)
"""
import json
import os
import subprocess
import sys
import tempfile
import shutil
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import council  # noqa: E402
from councillib import config, council_run, grounded, preflight, providers, status, timing  # noqa: E402
from councillib.supervise import Limits  # noqa: E402
from tests.test_council_run import Base, seat  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
PROMPTS = os.path.join(ROOT, "prompts")
APP = "const a = 1;\nif (!user) return null;\n\nfunction  render(x) {\n  return x   +   1;\n}\n" + "\n".join(
    "// filler %d" % i for i in range(30)) + "\nexport default render;\n"


def make_repo(tmp):
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "src", "deep"))
    with open(os.path.join(repo, "src", "app.js"), "w") as f:
        f.write(APP)
    with open(os.path.join(repo, "src", "deep", "util.py"), "w") as f:
        f.write("def helper():\n    return 'ok'\n")
    with open(os.path.join(repo, "blob.bin"), "wb") as f:
        f.write(b"\0\1\2" * 100)
    outside = os.path.join(tmp, "outside.txt")
    with open(outside, "w") as f:
        f.write("secret line that is not in the repo\n")
    os.symlink(outside, os.path.join(repo, "link.txt"))
    return repo, outside


class Verify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-g-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo, self.outside = make_repo(self.tmp)

    def v(self, text, cfg=None):
        return grounded.verify(text, self.repo, cfg)

    def test_01_good_citation_and_quote(self):
        r = self.v("The guard: `src/app.js:2` `if (!user) return null;` is fine.")
        self.assertEqual((r["n_citations"], r["n_quotes"], r["n_bad"], r["grounded_ok"]), (1, 1, 0, True))

    def test_02_whitespace_insensitive_and_range(self):
        r = self.v("See src/app.js:4-5 `function render(x) { return x + 1; }` here.")
        self.assertEqual((r["n_bad"], r["grounded_ok"]), (0, True))

    def test_03_fabricated_quote_is_bad(self):
        r = self.v("`src/app.js:2` `if (user === undefined) throw new Error('x');`")
        self.assertEqual((r["n_citations"], r["n_bad"], r["grounded_ok"]), (1, 1, False))
        self.assertIn("quote not in file", r["bad"][0]["reason"])

    def test_04_missing_file_and_line_past_end(self):
        r = self.v("src/nope.js:3 and src/app.js:9999")
        self.assertEqual(r["n_bad"], 2)
        reasons = " ".join(b["reason"] for b in r["bad"])
        self.assertIn("no such file", reasons)
        self.assertIn("past the end", reasons)

    def test_05_citation_without_quote_passes_when_line_exists(self):
        r = self.v("src/deep/util.py:2")
        self.assertEqual((r["n_citations"], r["n_quotes"], r["n_bad"]), (1, 0, 0))

    def test_06_escapes_are_bad(self):
        for text in ("../outside.txt:1", "link.txt:1", "%s:1" % self.outside, "src/../../outside.txt:1"):
            r = self.v(text)
            self.assertEqual(r["n_bad"], 1, text)
            self.assertIn("outside the root", r["bad"][0]["reason"], text)

    def test_07_absolute_path_inside_root_ok(self):
        r = self.v("%s:1 `const a = 1;`" % os.path.join(self.repo, "src", "app.js"))
        self.assertEqual((r["n_citations"], r["n_bad"]), (1, 0))

    def test_08_binary_is_unchecked_not_bad(self):
        r = self.v("blob.bin:2")
        self.assertEqual((r["n_bad"], r["n_unchecked"]), (0, 1))

    def test_09_oversized_is_unchecked(self):
        r = self.v("src/app.js:2 `if (!user) return null;`", {"grounded": {"max_file_bytes": 10}})
        self.assertEqual((r["n_bad"], r["n_unchecked"]), (0, 1))

    def test_10_urls_and_tlds_are_not_citations(self):
        r = self.v("See https://example.com:8080/x.js:3 and visit example.com:443 or foo.io:80.")
        self.assertEqual(r["n_citations"], 0)
        self.assertIsNone(r["grounded_ok"])

    def test_11_bare_file_name_resolved_in_repo(self):
        r = self.v("util.py:1 `def helper():` and app.js:2")
        self.assertEqual((r["n_citations"], r["n_bad"]), (2, 0))

    def test_12_quote_outside_window_is_accepted_and_counted(self):
        r = self.v("src/app.js:2 `export default render;`")
        self.assertEqual((r["n_bad"], r["n_line_off"]), (0, 1))

    def test_13_ellipsis_fragments(self):
        good = self.v("src/app.js:4-6 `function render(x) {... return x + 1; ...}`")
        self.assertEqual(good["n_bad"], 0)
        bad = self.v("src/app.js:4-6 `function render(x) {... return x * 99999; ...}`")
        self.assertEqual(bad["n_bad"], 1)

    def test_14_hash_L_form_and_short_spans_ignored(self):
        r = self.v("src/app.js#L2 `render` and `x`")
        self.assertEqual((r["n_citations"], r["n_quotes"], r["n_bad"]), (1, 0, 0))

    def test_15_backticked_citation_is_not_a_quote(self):
        r = self.v("`src/app.js:2` is where it is.")
        self.assertEqual((r["n_citations"], r["n_quotes"], r["n_bad"]), (1, 0, 0))

    def test_16_quote_belongs_to_nearest_preceding_citation(self):
        r = self.v("src/app.js:2 `if (!user) return null;` vs src/deep/util.py:1 `def helper():`")
        self.assertEqual((r["n_citations"], r["n_quotes"], r["n_bad"]), (2, 2, 0))

    def test_17_duplicates_counted_once_and_bad_list_capped(self):
        r = self.v("a.py:1 a.py:1 " + " ".join("no%d.py:1" % i for i in range(20)), {"grounded": {"max_bad_listed": 3}})
        self.assertEqual(r["n_citations"], 21)
        self.assertEqual(len(r["bad"]), 3)

    def test_18_no_citations_is_none_not_false(self):
        r = self.v("No source claims at all.\nVerdict: n/a")
        self.assertEqual((r["n_citations"], r["grounded_ok"]), (0, None))

    def test_19_labels(self):
        self.assertEqual(grounded.label({"mode": "ungrounded"}), "ungrounded")
        self.assertEqual(grounded.label({"mode": "grounded"}), "grounded")
        rec = {"mode": "grounded", "check": self.v("src/app.js:2 src/nope.js:1")}
        self.assertEqual(grounded.label(rec), "grounded 1/2 citations verified, 1 FAILED")
        self.assertIn("src/nope.js:1 (no such file)", grounded.chair_note(rec))
        self.assertEqual(grounded.chair_note({"mode": "grounded", "check": self.v("src/app.js:2")}), "")


    # ---- fixes 1 and 2 (run example-run): quote attribution and bare `:NNN` refs
    def test_19a_quote_pairs_with_the_citation_of_its_own_sentence(self):
        # synthetic seat text (same shape as a real claude review): `navigated` precedes the citation and used to be paired with it
        with open(os.path.join(FIX, "grounded-claude-seat.md")) as f:
            text = f.read()
        by = {(c["path"], c["line"]): c for c in grounded.parse(text)}
        rs = by[("src/Router.tsx", 551)]
        self.assertEqual(rs["quotes"], ["scrollTo({ top: 0, behavior: 'instant' });"])
        self.assertFalse(rs["bare"])
        # a tree that has only what the sentence says is in Router.tsx
        os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
        with open(os.path.join(self.repo, "src", "Router.tsx"), "w") as f:
            f.write("\n" * 550 + "  scrollTo({ top: 0, behavior: 'instant' });\n")
        sent = [ln for ln in text.split("\n") if "src/Router.tsx:551" in ln][0]
        r = self.v(sent)
        self.assertEqual((r["n_bad"], r["grounded_ok"]), (0, True), r["bad"])
        # the old pairing (every quote to the first citation of the line) flagged this correct seat
        self.assertTrue(grounded.parse(sent)[-1]["path"] == "src/Router.tsx")

    def test_19b_leading_quote_goes_to_the_citation_after_it_in_its_clause(self):
        r = self.v("src/deep/util.py:1 is fine. `const a = 1;` lives at src/app.js:1.")
        self.assertEqual((r["n_citations"], r["n_quotes"], r["n_bad"]), (2, 1, 0), r["bad"])
        r = self.v("src/deep/util.py:1 `def helper():`; `const a = 1;` sits at src/app.js:1; none of it `nowhere in any file`.")
        self.assertEqual(r["n_bad"], 1)                      # the clause with no citation falls back to the nearest before it

    def test_19c_bare_refs_inherit_the_last_path_and_are_counted(self):
        r = self.v("`src/app.js:2` `if (!user) return null;` and `:4` `function  render(x) {` (also :6, :7-8).")
        self.assertEqual((r["n_citations"], r["n_quotes"], r["n_bad"]), (4, 2, 0), r["bad"])
        cs = grounded.parse("src/app.js:2 then `:4`, :6 and :7-8")
        self.assertEqual([(c["path"], c["line"], c["end"], c["bare"]) for c in cs],
                         [("src/app.js", 2, None, False), ("src/app.js", 4, None, True),
                          ("src/app.js", 6, None, True), ("src/app.js", 7, 8, True)])

    def test_19d_bare_refs_are_checked(self):
        r = self.v("src/deep/util.py:1 and then :99")
        self.assertEqual((r["n_citations"], r["n_bad"]), (2, 1))
        self.assertEqual(r["bad"][0]["cite"], ":99 (bare ref, after src/deep/util.py)")
        self.assertIn("past the end", r["bad"][0]["reason"])
        r = self.v("src/app.js:2 and :5 `a quote that is nowhere in the file`")
        self.assertEqual((r["n_citations"], r["n_quotes"], r["n_bad"]), (2, 1, 1))

    def test_19e_bare_ref_falls_back_to_other_cited_files(self):
        # the seat moved on to app.js without naming it again: util.py (2 lines) is the inherited path
        r = self.v("src/app.js:1 `const a = 1;`, src/deep/util.py:1 `def helper():`, and :2 `if (!user) return null;`")
        self.assertEqual((r["n_citations"], r["n_bad"]), (3, 0), r["bad"])

    def test_19f_things_that_are_not_bare_refs(self):
        self.assertEqual(grounded.parse("see :12 first (no path cited yet)"), [])
        text = "src/app.js:1 at 12:30, http://x.com:8080/a, ratio a:3, `{ k:4 }`, `foo(:5)`, std::vector and :a"
        self.assertEqual([c["line"] for c in grounded.parse(text)], [1])

    def test_19f2_ports_and_quote_less_bare_refs(self):
        self.assertEqual([c["line"] for c in grounded.parse("src/app.js:2; the dev server on :4000, localhost :3000 and (port :9333)")], [2])
        r = self.v("src/app.js:2 and the preview (:9333) and also :3001")        # no port word: past EOF everywhere = bad
        self.assertEqual((r["n_citations"], r["n_bad"], r["n_unchecked"]), (3, 2, 0))
        r = self.v("src/app.js:2 and :4000 `fabricated line of code`")
        self.assertEqual(r["n_bad"], 1)
        # a quote-less bare ref must fit its own (inherited) path; fitting only another cited file is unchecked
        r = self.v("src/app.js:1 `const a = 1;`, src/deep/util.py:1 `def helper():`, and :20")     # util.py has 2 lines, app.js 37
        self.assertEqual((r["n_citations"], r["n_bad"], r["n_unchecked"], r["grounded_ok"]), (3, 0, 1, True))
        r = self.v("src/app.js:1, src/deep/util.py:1, and :2")                                      # fits util.py: checked
        self.assertEqual((r["n_bad"], r["n_unchecked"]), (0, 0))

    def test_19g_bare_ref_owns_only_the_quote_beside_it(self):
        r = self.v("src/app.js:2 `if (!user) return null;`; later at :3 use `brand new code goes here` instead.")
        self.assertEqual((r["n_citations"], r["n_quotes"], r["n_bad"]), (2, 1, 0), r["bad"])
        r = self.v("src/app.js:2 and `!user) return null` (`:2`); also :2 `fabricated line of code`")
        self.assertEqual(r["n_bad"], 1)

    def test_19h_file_name_in_another_clause_is_not_a_quote(self):
        # run example-run row 24 (P22): a backtick file name with no line ref, in a clause that
        # has no citation of its own, was given to the nearest citation and flagged as a quote missing from that line
        for text in ("src/app.js:1 `const a = 1;` sets it; the caller lives in `OtherWidget.tsx`.",
                     "`OtherWidget.tsx` imports it. src/app.js:1 `const a = 1;` sets it.",
                     "src/app.js:1 `const a = 1;` sets it. See `lib/deep/helpers.py` for the rest."):
            c = grounded.parse(text)[0]
            self.assertEqual(c["quotes"], ["const a = 1;"], text)
            r = self.v(text)
            self.assertEqual((r["n_citations"], r["n_quotes"], r["n_bad"], r["grounded_ok"]), (1, 1, 0, True), r["bad"])
        # true positives still attach and are flagged: a made-up code quote in a clause with no citation (fallback), and
        # one in the citation's own clause; a file name inside the citation's own clause is still that citation's quote
        r = self.v("src/app.js:1 `const a = 1;` sets it; later `return madeUpValue;` runs.")
        self.assertEqual((r["n_quotes"], r["n_bad"], r["grounded_ok"]), (2, 1, False), r["bad"])
        r = self.v("src/app.js:2 `if (!user) return undefined;` guards it.")
        self.assertEqual((r["n_quotes"], r["n_bad"]), (1, 1), r["bad"])
        self.assertEqual(grounded.parse("src/app.js:2 calls `OtherWidget.tsx` here")[0]["quotes"], ["OtherWidget.tsx"])

    def test_19h_probe_lines_are_not_checked(self):
        r = self.v("Probe: `node -e \"console.log(1)\"` -> 1 (see src/ghost.js:99)\n- **Probe 2:** `ls src` -> app.js")
        self.assertEqual((r["n_citations"], r["grounded_ok"]), (0, None))
        r = self.v("Probes confirm `src/app.js:2` `if (!user) return null;`")
        self.assertEqual(r["n_citations"], 1)

class RootAndImages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-g-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_20_rejects_missing_file_root_and_home(self):
        self.assertIn("not a directory", grounded.resolve_root(os.path.join(self.tmp, "nope"))[1])
        f = os.path.join(self.tmp, "f")
        open(f, "w").close()
        self.assertIn("not a directory", grounded.resolve_root(f)[1])
        self.assertIn("too broad", grounded.resolve_root("/")[1])
        self.assertIn("too broad", grounded.resolve_root("~")[1])
        self.assertIn("too broad", grounded.resolve_root(os.path.dirname(os.path.expanduser("~")))[1])

    def test_21_explicit_root_and_default_repo_root(self):
        root, err = grounded.resolve_root(self.tmp)
        self.assertEqual((root, err), (os.path.realpath(self.tmp), None))
        repo = os.path.join(self.tmp, "r")
        os.makedirs(os.path.join(repo, "sub", "dir"))
        subprocess.run(["git", "init", "-q", repo], check=True)
        sub = os.path.join(repo, "sub", "dir")
        self.assertEqual(grounded.resolve_root("", cwd=sub)[0], os.path.realpath(repo))
        self.assertEqual(grounded.resolve_root("", {"grounded": {"default_root": "cwd"}}, cwd=sub)[0], os.path.realpath(sub))

    def test_22_images(self):
        png = os.path.join(self.tmp, "a.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG")
        txt = os.path.join(self.tmp, "a.txt")
        open(txt, "w").close()
        self.assertEqual(grounded.check_images([png]), ([os.path.realpath(png)], None))
        self.assertIn("not an image", grounded.check_images([txt])[1])
        self.assertIn("not a file", grounded.check_images([os.path.join(self.tmp, "x.png")])[1])
        self.assertIn("over", grounded.check_images([png], {"grounded": {"max_image_bytes": 2}})[1])
        self.assertIn("max 1", grounded.check_images([png, png], {"grounded": {"max_images": 1}})[1])

    def test_23_stage_images_copies_into_dest(self):
        png = os.path.join(self.tmp, "my shot.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG")
        out = grounded.stage_images([png], os.path.join(self.tmp, "stage"))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith(os.path.join(self.tmp, "stage")))
        self.assertTrue(os.path.basename(out[0]).startswith("01-my_shot"))
        with open(out[0], "rb") as f:
            self.assertEqual(f.read(), b"\x89PNG")

    def test_24_problems(self):
        self.assertEqual(grounded.problems({}), [])
        self.assertEqual(grounded.problems({"grounded": {"enabled": False, "line_window": 3, "default_root": "cwd"}}), [])
        bad = grounded.problems({"grounded": {"enabled": "yes", "nope": 1, "line_window": -1, "default_root": "x"}})
        self.assertEqual(len(bad), 4)
        self.assertEqual(grounded.problems({"grounded": 3}), ["[grounded] must be a table"])


class Argv(unittest.TestCase):
    """Adapter argv in grounded mode. build() only assembles argv: nothing is launched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-g-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prompt = os.path.join(self.tmp, "p.md")
        with open(self.prompt, "w") as f:
            f.write("hi")
        self.lim = Limits(mode="stream", idle_s=240, ceiling_s=1200.0, deadline_s=None)
        self.cfg = config.load()
        self.gr = {"root": "/repo/root", "images": ["/stage/01-a.png", "/stage/02-b.png"], "restricted": True}

    def build(self, name, seat, **kw):
        return providers.get(name).build(seat, self.prompt, self.tmp, self.lim, self.cfg, **kw)

    def after(self, argv, flag):
        return argv[argv.index(flag) + 1]

    def test_30_claude_plain_is_unchanged(self):
        la = self.build("claude", {"id": "claude", "provider": "claude", "model": "claude-opus-5-5"})
        self.assertEqual(self.after(la.argv, "--tools"), "")
        self.assertNotIn("--allowed-tools", la.argv)
        self.assertNotIn("--restricted", la.argv)
        self.assertEqual(la.cwd, self.tmp)

    def test_31_claude_grounded_read_only_tools(self):
        la = self.build("claude", {"id": "claude", "provider": "claude", "model": "claude-opus-5-5", "_grounded": self.gr})
        a = la.argv
        self.assertEqual(self.after(a, "--tools"), "Read,Grep,Glob")
        self.assertEqual(self.after(a, "--allowed-tools"), "Read,Grep,Glob")
        deny = self.after(a, "--disallowed-tools").split(",")
        for t in ("Bash", "Edit", "Write", "WebFetch", "WebSearch"):
            self.assertIn(t, deny)
        self.assertIn("--restricted", a)
        self.assertIn("--safe-mode", a)
        self.assertEqual(self.after(a, "--add-dir"), "/stage")           # the copies' dir, never the images' own folder
        self.assertEqual(la.cwd, "/repo/root")
        self.assertEqual(a.count("--tools"), 1)
        self.assertNotIn("--dangerously-skip-permissions", a)
        self.assertNotIn("bypassPermissions", " ".join(a))

    def test_32_claude_restricted_can_be_switched_off_in_gr(self):
        la = self.build("claude", {"id": "c", "provider": "claude", "model": "m", "_grounded": dict(self.gr, restricted=False)})
        self.assertNotIn("--restricted", la.argv)
        self.assertEqual(self.after(la.argv, "--tools"), "Read,Grep,Glob")

    def test_33_codex_grounded(self):
        plain = self.build("codex", {"id": "codex", "provider": "codex", "model": "gpt-6-astra"}).argv
        self.assertNotIn("-C", plain)
        self.assertNotIn("-i", plain)
        la = self.build("codex", {"id": "codex", "provider": "codex", "model": "gpt-6-astra", "_grounded": self.gr})
        a = la.argv
        self.assertEqual(self.after(a, "-s"), "read-only")
        self.assertEqual(self.after(a, "-C"), "/repo/root")
        self.assertEqual([a[i + 1] for i, x in enumerate(a) if x == "-i"], self.gr["images"])
        self.assertNotIn("--add-dir", a)                                    # codex would make those writable
        self.assertNotIn("danger-full-access", a)
        self.assertIn('web_search="disabled"', a)
        self.assertEqual(a[-1], "-")
        i = a.index("-i")
        self.assertTrue(a[a.index("-i", i + 1) + 2].startswith("-"))        # a flag follows the last image (list option)
        self.assertEqual(la.cwd, self.tmp)

    def test_34_salvage_seat_is_text_only(self):
        for name in ("claude", "codex"):
            s = {"id": name, "provider": name, "model": "m", "_grounded": self.gr}
            sv = providers.get(name).salvage_seat(s, self.cfg, 8000)
            self.assertNotIn("_grounded", sv)
            self.assertEqual(s["_grounded"], self.gr)                      # the original seat is untouched

    def test_35_capability_table(self):
        self.assertTrue(providers.get("claude").grounded_capable)
        self.assertTrue(providers.get("codex").grounded_capable)
        self.assertFalse(providers.get("agy").grounded_capable)
        self.assertIn("agy --help", providers.get("agy").grounded_reason)
        self.assertFalse(providers.get("openrouter").grounded_capable)
        a = self.build("agy", {"id": "agy", "provider": "agy", "model": "Gemini 3.8 Flash (Medium)", "_grounded": self.gr})
        self.assertNotIn("/repo/root", " ".join(a.argv))                   # agy never gets the repo, even if the key leaks
        self.assertNotIn("/stage", " ".join(a.argv))

    def test_36_seat_support_opt_outs(self):
        cl = providers.get("claude")
        s = {"id": "claude", "provider": "claude", "model": "m"}
        self.assertEqual(grounded.seat_support({}, s, cl), (True, ""))
        self.assertFalse(grounded.seat_support({}, dict(s, grounded=False), cl)[0])
        self.assertFalse(grounded.seat_support({"providers": {"claude": {"grounded": False}}}, s, cl)[0])
        self.assertFalse(grounded.seat_support({}, s, None)[0])


    # ---- fix 3: read-only probes
    def test_37_claude_probes_opt_in_adds_a_read_only_bash_allowlist(self):
        cl = {"id": "claude", "provider": "claude", "model": "claude-opus-5-5"}
        a = self.build("claude", dict(cl, _grounded=dict(self.gr, probes=True))).argv
        self.assertEqual(self.after(a, "--tools"), "Read,Grep,Glob,Bash")
        i, j = a.index("--allowed-tools"), a.index("--disallowed-tools")
        allowed = a[i + 1:j]
        self.assertEqual(allowed[0], "Read,Grep,Glob")
        self.assertIn("Bash(git log:*)", allowed)                       # one rule per argv element
        self.assertNotIn("Bash", allowed)                               # never a bare Bash
        self.assertTrue(all(x.startswith("Bash(") for x in allowed[1:]))
        deny = a[j + 1:a.index("--restricted")]
        self.assertNotIn("Bash", deny[0].split(","))                    # the blanket Bash deny is lifted, the rules below stay
        for t in ("Edit", "Write", "WebFetch", "WebSearch"):
            self.assertIn(t, deny[0].split(","))
        self.assertIn("Bash(*--output*)", deny)
        self.assertIn("--restricted", a)
        self.assertNotIn("--dangerously-skip-permissions", a)
        off = self.build("claude", dict(cl, _grounded=self.gr)).argv     # the key not set: Read/Grep/Glob only
        self.assertEqual(self.after(off, "--tools"), "Read,Grep,Glob")
        self.assertIn("Bash", self.after(off, "--disallowed-tools").split(","))

    def test_37a_probe_allowlist_has_nothing_that_runs_a_program_or_writes(self):
        # commands whose flags run a program (git grep -O<cmd>/--open-files=, pagers, external diff) or write files
        for cmd in providers.CLAUDE_PROBE_BASH:
            self.assertNotIn(cmd, ("git grep", "git config", "git -c", "git stash", "git checkout", "git commit",
                                   "git apply", "git am", "git clean", "git reset", "git rm", "git mv", "git branch",
                                   "git tag", "git fetch", "git pull", "git push", "git remote", "git worktree",
                                   "git submodule", "git bisect", "git rebase", "git merge", "git difftool",
                                   "git mergetool", "git instaweb", "git daemon", "git gc", "git add", "git restore"))
            self.assertRegex(cmd, r"^(?:git (?:log|show|diff|blame|ls-files)|wc|ls)$")
        self.assertNotIn("git grep", grounded.PROBE_LINES["claude"])
        self.assertNotIn("git grep", providers.CLAUDE_PROBE_BASH)

    def test_38_codex_probes_change_the_prompt_not_the_sandbox(self):
        cx = {"id": "codex", "provider": "codex", "model": "gpt-6-astra"}
        a = self.build("codex", dict(cx, _grounded=dict(self.gr, probes=True))).argv
        b = self.build("codex", dict(cx, _grounded=self.gr)).argv
        self.assertEqual(a, b)
        self.assertEqual(self.after(a, "-s"), "read-only")
        self.assertNotIn("danger-full-access", a)

    def test_39_probes_on_defaults_and_overrides(self):
        cx, cl = providers.get("codex"), providers.get("claude")
        s = lambda p, **kw: dict({"id": p, "provider": p, "model": "m"}, **kw)
        self.assertEqual(grounded.cfg_of({})["probes"], ["codex"])
        self.assertTrue(grounded.probes_on({}, s("codex"), cx))                      # default on for codex
        self.assertFalse(grounded.probes_on({}, s("claude"), cl))                    # claude: opt-in
        self.assertFalse(grounded.probes_on({"grounded": {"probes": []}}, s("codex"), cx))
        self.assertTrue(grounded.probes_on({"grounded": {"probes": ["codex", "claude"]}}, s("claude"), cl))
        self.assertTrue(grounded.probes_on({}, s("claude", probes=True), cl))        # per seat
        self.assertFalse(grounded.probes_on({}, s("codex", probes=False), cx))
        self.assertFalse(grounded.probes_on({"grounded": {"probes": ["agy"]}}, s("agy"), providers.get("agy")))
        for bad in ("codex", [1], ["gemini"], True):
            self.assertEqual(len(grounded.problems({"grounded": {"probes": bad}})), 1, bad)
        self.assertEqual(grounded.problems({"grounded": {"probes": []}}), [])
        self.assertEqual(grounded.problems(config.load()), [])
        cfg = config.load()
        cfg["seat"][0]["probes"] = True                                              # the documented per-seat key is accepted
        self.assertEqual(config.validate_roster(cfg), [])
        seat_ = s("codex")
        rec = grounded.seat_grounding({}, seat_, cx, {"root": "/r", "images": []})
        self.assertTrue(rec["probes"] and seat_["_grounded"]["probes"])
        seat_ = s("claude")
        rec = grounded.seat_grounding({}, seat_, cl, {"root": "/r", "images": []})
        self.assertNotIn("probes", rec)
        self.assertNotIn("probes", seat_["_grounded"])

    def test_39a_probe_prompt_lines(self):
        base = {"root": "/r", "images": []}
        off = grounded.prompt_vars(base)["access_line"]
        self.assertIn("do not try to run", off)                                       # unchanged without probes
        self.assertEqual(off, grounded.NO_PROBE_LINE)
        for who in ("codex", "claude"):
            line = grounded.prompt_vars(grounded.with_probes(base, {"probes": True}, {"provider": who}))["access_line"]
            self.assertIn("Probe: <exact command>", line)
            self.assertIn("read-only", line)
            self.assertNotIn("do not try to run", line)
        self.assertIn("node -e", grounded.PROBE_LINES["codex"])
        self.assertIn("git blame", grounded.PROBE_LINES["claude"])
        self.assertIs(grounded.with_probes(base, {"mode": "grounded"}, {"provider": "codex"}), base)
        import string
        for name, fields in (("advisor-grounded.md", {}), ("critique-grounded.md", {"letter": "A", "own": "o", "peers": "p"})):
            with open(os.path.join(PROMPTS, name)) as f:
                tpl = string.Template(f.read())
            for st in (base, grounded.with_probes(base, {"probes": True}, {"provider": "codex"})):
                text = tpl.substitute(question="Q", context="C", seat_id="s", n_seats=3, lens_line="", **fields,
                                      **grounded.prompt_vars(st))
                self.assertIn("path:LINE", text)
                self.assertNotIn("${", text)
        crit = council_run.critique_prompt(tpl, "Q", "C", "A", "own", [("B", None, "x")], 3,
                                          grounded=grounded.with_probes(base, {"probes": True}, {"provider": "codex"}))
        self.assertIn("Probe: <exact command>", crit)

class Extractor(unittest.TestCase):
    def test_40_tool_turns_do_not_leak_narration_into_the_answer(self):
        from tests.test_providers import feed_all, fixture
        ex = feed_all(providers.ClaudeExtractor(), fixture("claude-tools.ndjson"))
        text = ex.final_text()
        self.assertTrue(text.startswith("The guard is at"))
        self.assertNotIn("Let me read the file first", text)
        self.assertIsNone(ex.error())
        from councillib import verdict
        self.assertEqual(verdict.parse(text), "APPROVE")
        r = grounded.parse(text)
        self.assertEqual((r[0]["path"], r[0]["line"]), ("src/app.js", 2))
        self.assertEqual(ex.usage()["output_tokens"], 20)


class Timing(unittest.TestCase):
    def test_50_grounded_samples_are_timed_apart(self):
        tmp = tempfile.mkdtemp(prefix="council-g-")
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "latency.jsonl")
        for t in (10, 11, 12, 10, 11):
            timing.append(log, {"kind": "seat", "provider": "codex", "model": "m", "total_s": t, "outcome": "ok"})
        for t in (300, 310, 320, 330, 340):
            timing.append(log, {"kind": "seat", "provider": "codex", "model": "m", "total_s": t, "outcome": "ok", "grounded": True})
        cfg = {"timeouts": {"default": {"min_timeout_s": 1, "ceiling_s": 9999, "base_timeout_s": 77},
                            "adaptive": {"min_samples": 5, "safety": 1.0, "size_exponent": 0.5}}}
        plain, src_p = timing.adaptive_deadline(log, "codex", "m", 0, cfg)
        ground, src_g = timing.adaptive_deadline(log, "codex", "m", 0, cfg, grounded=True)
        self.assertEqual((src_p, src_g), ("p95", "p95"))
        self.assertLess(plain, 20)
        self.assertGreaterEqual(ground, 300)
        _d, src = timing.adaptive_deadline(log, "claude", "m", 0, cfg, grounded=True)
        self.assertEqual(src, "cold")                                       # too few grounded samples of its own


class GAdapter(providers.StubAdapter):
    """A tool-capable stub: records the `_grounded` key its seat dict carried at build time."""
    name = "gstub"
    grounded_capable = True

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        with open(workdir + ".grounded.json", "w") as f:
            json.dump(seat.get("_grounded"), f)
        return super().build(seat, prompt_path, workdir, limits, cfg, launch_opts)


class RunWiring(Base):
    def setUp(self):
        super().setUp()
        providers.register(GAdapter())
        providers.FAMILIES.setdefault("gstub", "stub")
        self.addCleanup(providers._REGISTRY.pop, "gstub", None)
        shutil.copy(os.path.join(PROMPTS, "advisor-grounded.md"), os.path.join(self.prompts, "advisor-grounded.md"))
        with open(os.path.join(self.prompts, "advisor.md"), "w") as f:
            f.write("${lens_line}PLAIN $seat_id of $n_seats\nQ: $question\nC: $context\n")
        self.repo, _ = make_repo(self.tmp)
        self.png = os.path.join(self.tmp, "shot.png")
        with open(self.png, "wb") as f:
            f.write(b"\x89PNG\r\n")

    def answer_seat(self, sid, text, provider="gstub", **kw):
        path = os.path.join(self.tmp, "ans-%s.txt" % sid)
        with open(path, "w") as f:
            f.write(text)
        return seat(sid, ["replay", path, "0"], provider=provider, **kw)

    def gcfg(self, seats, images=(), **kw):
        extra = {"_grounded": {"root": os.path.realpath(self.repo), "images": list(images)}}
        extra.update(kw)
        return self.cfg(seats, extra=extra)

    GOOD = "The guard holds: `src/app.js:2` `if (!user) return null;`.\nVerdict: APPROVE\nConfidence: high\n"
    FAKE = "It is at `src/app.js:2` `if (user === undefined) throw new Error(1);`, and src/ghost.js:7.\nVerdict: REVISE\n"
    PLAIN = "No file access here.\nVerdict: n/a\n"

    def test_60_run_records_grounding_per_seat(self):
        seats = [self.answer_seat("a", self.GOOD), self.answer_seat("b", self.FAKE),
                 self.answer_seat("c", self.PLAIN, provider="stub")]
        self.assertEqual(self.run_council(self.gcfg(seats, images=[self.png])), 0)
        summ = self.summary()
        recs = self.by_id(summ)
        self.assertEqual(recs["a"]["grounding"]["mode"], "grounded")
        self.assertTrue(recs["a"]["grounded_ok"])
        self.assertEqual((recs["a"]["n_citations"], recs["a"]["n_bad"]), (1, 0))
        self.assertEqual(recs["b"]["status"], "ok")                         # a fabricated quote flags, never fails the seat
        self.assertIs(recs["b"]["grounded_ok"], False)
        self.assertEqual((recs["b"]["n_citations"], recs["b"]["n_bad"]), (2, 2))
        self.assertEqual(recs["c"]["grounding"]["mode"], "ungrounded")
        self.assertIn("text only", recs["c"]["grounding"]["reason"])
        self.assertNotIn("grounded_ok", recs["c"])
        g = summ["grounded"]
        self.assertEqual((g["grounded_seats"], g["flagged_seats"], g["images"]), (["a", "b"], ["b"], ["01-shot.png"]))
        self.assertEqual(list(g["ungrounded_seats"]), ["c"])
        self.assertEqual((g["n_citations"], g["n_bad"]), (3, 2))
        self.assertIsNone(summ["chair"].get("grounding"))                   # the chair is never grounded
        with open(os.path.join(self.out, "seats", "b.json")) as f:
            self.assertEqual(json.load(f)["grounding"]["check"]["bad"][1]["cite"], "src/ghost.js:7")

    def test_61_chair_is_told_what_was_verified(self):
        seats = [self.answer_seat("a", self.GOOD), self.answer_seat("b", self.FAKE),
                 self.answer_seat("c", self.PLAIN, provider="stub")]
        self.assertEqual(self.run_council(self.gcfg(seats)), 0)
        chair_in = self.read("chair", "synthesis.md")
        self.assertIn("seat a | provider gstub | model stub-model | status ok | verdict APPROVE | grounded 1/1 citations verified", chair_in)
        self.assertIn("grounded 0/2 citations verified, 2 FAILED [unverified: src/app.js:2 (quote not in file", chair_in)
        self.assertIn("src/ghost.js:7 (no such file)", chair_in)
        self.assertIn("| ungrounded (text only", chair_in)

    def test_62_per_call_key_and_prompt_variant(self):
        seats = [self.answer_seat("a", self.GOOD), seat("c", "echo-stdin")]
        seats[1]["provider"] = "stub"
        self.assertEqual(self.run_council(self.gcfg(seats, images=[self.png])), 0)
        prompt_a, prompt_c = self.read("seats", "a.md"), self.read("seats", "c.md")
        self.assertIn("PLAIN c of 2", prompt_c)                             # the ungrounded seat keeps advisor.md
        self.assertNotIn("read-only access", prompt_c)
        with open(os.path.join(self.out, "seats", "a.json")) as f:
            self.assertTrue(json.load(f)["grounding"]["root"].endswith("repo"))

    def test_63_template_gets_root_and_images(self):
        seats = [seat("a", "echo-stdin", provider="gstub"), seat("b", "echo-stdin", provider="gstub")]
        self.assertEqual(self.run_council(self.gcfg(seats, images=[self.png])), 0)
        text = self.read("seats", "a.md")
        self.assertIn(os.path.realpath(self.repo), text)
        self.assertIn("01-shot.png", text)
        self.assertIn("path:LINE", text)
        self.assertIn("Verdict: APPROVE", text)
        self.assertNotIn("Do not use files or tools", text)
        self.assertNotIn("$root", text)

    def test_64_image_copies_are_staged_outside_the_repo_and_the_original_folder(self):
        seats = [seat("a", "echo-stdin", provider="gstub"), seat("b", "echo-stdin", provider="gstub")]
        self.assertEqual(self.run_council(self.gcfg(seats, images=[self.png])), 0)
        text = self.read("seats", "a.md")
        staged = [ln[2:] for ln in text.splitlines() if ln.startswith("- ") and ln.endswith("01-shot.png")]
        self.assertEqual(len(staged), 1)
        self.assertNotEqual(os.path.dirname(staged[0]), os.path.dirname(self.png))
        self.assertFalse(staged[0].startswith(self.repo))

    def test_65_not_grounded_run_is_unchanged(self):
        seats = [self.answer_seat("a", self.GOOD), self.answer_seat("c", self.PLAIN, provider="stub")]
        self.assertEqual(self.run_council(self.cfg(seats)), 0)
        summ = self.summary()
        self.assertNotIn("grounded", summ)
        for r in summ["seats"]:
            self.assertNotIn("grounding", r)
            self.assertNotIn("grounded_ok", r)
        self.assertNotIn("grounded", self.read("chair", "synthesis.md"))
        snap = status.load(self.out)
        self.assertTrue(all(not s.get("grounding") for s in snap["seats"]))

    def test_66_latency_lines_tag_grounded_calls_only(self):
        seats = [self.answer_seat("a", self.GOOD), self.answer_seat("c", self.PLAIN, provider="stub")]
        self.assertEqual(self.run_council(self.gcfg(seats)), 0)
        rows = [json.loads(x) for x in self.latency_lines()]
        by = {r["provider"]: r for r in rows if r["kind"] in ("seat", "test")}
        self.assertTrue(by["gstub"].get("grounded"))
        self.assertNotIn("grounded", by["stub"])
        chair = [r for r in rows if r["kind"] not in ("seat", "test")]
        self.assertTrue(chair and all("grounded" not in r for r in chair))

    def latency_lines(self):
        with open(os.path.join(self.state, "latency.jsonl")) as f:
            return [ln for ln in f if ln.strip()]

    def test_66a_probes_reach_the_prompt_and_the_build_key(self):
        seats = [self.answer_seat("a", self.GOOD), self.answer_seat("b", self.GOOD, probes=False),
                 self.answer_seat("c", self.GOOD, provider="stub")]
        extra = {"grounded": {"probes": ["gstub"]}}
        with mock.patch.object(GAdapter, "probes_capable", True, create=True):
            self.assertEqual(self.run_council(self.gcfg(seats, **extra)), 0)
        recs = self.by_id(self.summary())
        self.assertTrue(recs["a"]["grounding"]["probes"])
        self.assertNotIn("probes", recs["b"]["grounding"])
        self.assertNotIn("probes", recs["c"]["grounding"])
        # the prompt file each seat's call read is not kept for replay seats; the build key is
        with open(os.path.join(self.out, "seats", "a.json")) as f:
            self.assertTrue(json.load(f)["grounding"]["probes"])

    def test_66b_probe_prompt_text_in_the_launched_prompt(self):
        seats = [seat("a", "echo-stdin", provider="gstub"), seat("b", "echo-stdin", provider="gstub", probes=False)]
        with mock.patch.object(GAdapter, "probes_capable", True, create=True):
            self.assertEqual(self.run_council(self.gcfg(seats, grounded={"probes": ["gstub"]})), 0)
        self.assertIn("Probe: <exact command>", self.read("seats", "a.md"))
        self.assertNotIn("Probe: <exact command>", self.read("seats", "b.md"))
        self.assertIn("do not try to run", self.read("seats", "b.md"))
        shutil.rmtree(self.out)
        os.makedirs(self.out)
        seats = [seat("a", "echo-stdin", provider="gstub")]
        with mock.patch.object(GAdapter, "probes_capable", True, create=True):
            self.assertEqual(self.run_council(self.gcfg(seats)), 0)             # no [grounded] probes: gstub is not listed
        self.assertNotIn("Probe: <exact command>", self.read("seats", "a.md"))

    def test_67_status_shows_grounding_and_check(self):
        seats = [self.answer_seat("a", self.GOOD), self.answer_seat("b", self.FAKE),
                 self.answer_seat("c", self.PLAIN, provider="stub")]
        self.assertEqual(self.run_council(self.gcfg(seats)), 0)
        table = status.render(status.load(self.out))
        self.assertIn("grounded 1/1 citations verified", table)
        self.assertIn("grounded 0/2 citations verified, 2 FAILED", table)
        self.assertIn("ungrounded", table)

    def test_68_missing_grounded_template_ends_the_run_early(self):
        os.unlink(os.path.join(self.prompts, "advisor-grounded.md"))
        seats = [self.answer_seat("a", self.GOOD), self.answer_seat("b", self.GOOD)]
        self.assertEqual(self.run_council(self.gcfg(seats)), 2)

    def test_69_checker_crash_never_fails_a_seat(self):
        seats = [self.answer_seat("a", self.GOOD), self.answer_seat("b", self.GOOD)]
        with mock.patch.object(grounded, "verify", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_council(self.gcfg(seats)), 0)
        recs = self.by_id(self.summary())
        self.assertEqual(recs["a"]["status"], "ok")
        self.assertIsNone(recs["a"]["grounded_ok"])


class GroundedKeyReachesBuild(Base):
    def setUp(self):
        super().setUp()
        providers.register(GAdapter())
        providers.FAMILIES.setdefault("gstub", "stub")
        self.addCleanup(providers._REGISTRY.pop, "gstub", None)
        shutil.copy(os.path.join(PROMPTS, "advisor-grounded.md"), os.path.join(self.prompts, "advisor-grounded.md"))
        self.repo, _ = make_repo(self.tmp)

    def test_70_only_capable_seats_get_the_key(self):
        seats = [seat("a", "echo-stdin", provider="gstub"), seat("b", "echo-stdin", provider="gstub", grounded=False),
                 seat("c", "echo-stdin", provider="stub")]
        cfg = self.cfg(seats, extra={"_grounded": {"root": os.path.realpath(self.repo), "images": []}})
        seen = {}
        orig = GAdapter.build

        def spy(self_, seat_, *a, **k):
            seen[seat_["id"]] = seat_.get("_grounded")
            return orig(self_, seat_, *a, **k)
        with mock.patch.object(GAdapter, "build", spy):
            self.assertEqual(self.run_council(cfg), 0)
        self.assertEqual(seen["a"]["root"], os.path.realpath(self.repo))
        self.assertIsNone(seen["b"])
        self.assertNotIn("c", seen)
        self.assertEqual(self.by_id(self.summary())["b"]["grounding"]["mode"], "ungrounded")
        # the chair (a stub here) and every salvage call are built without the key
        self.assertNotIn("grounding", self.summary()["chair"])


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-g-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.png = os.path.join(self.tmp, "a.png")
        with open(self.png, "wb") as f:
            f.write(b"\x89PNG")

    def parse(self, *argv):
        return council._parser().parse_args(list(argv))

    def test_80_flags_parse_on_both_commands(self):
        for cmd in ("preflight", "run"):
            a = self.parse(cmd, "--grounded", "--attach", "x.png", "--attach", "y.png")
            self.assertEqual((a.grounded, a.attach), ("", ["x.png", "y.png"]))
            self.assertEqual(self.parse(cmd, "--grounded", "/some/dir").grounded, "/some/dir")
            self.assertIsNone(self.parse(cmd).grounded)
        a = self.parse("run", "--grounded", "--", "what is it?")
        self.assertEqual((a.grounded, a.question), ("", ["what is it?"]))

    def test_81_load_sets_runtime_key(self):
        cfg, err = council._load(self.parse("run", "--grounded", self.repo, "--attach", self.png, "--", "q"))
        self.assertIsNone(err)
        self.assertEqual(cfg["_grounded"], {"root": os.path.realpath(self.repo), "images": [os.path.realpath(self.png)]})
        cfg, err = council._load(self.parse("run", "--", "q"))
        self.assertNotIn("_grounded", cfg)

    def test_82_attach_needs_grounded_and_bad_input_exits_2(self):
        self.assertIn("needs --grounded", council._load(self.parse("run", "--attach", self.png, "--", "q"))[1])
        self.assertIn("not a directory", council._load(self.parse("run", "--grounded", "/no/such/dir", "--", "q"))[1])
        self.assertIn("too broad", council._load(self.parse("run", "--grounded", "/", "--", "q"))[1])
        self.assertIn("not an image", council._load(self.parse("run", "--grounded", self.repo, "--attach", __file__, "--", "q"))[1])
        out = subprocess.run([sys.executable, "-Es", os.path.join(ROOT, "council.py"), "run", "--grounded", "/no/such/dir", "--", "q"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)

    def test_83_disabled_in_config(self):
        ov = os.path.join(self.tmp, "o.toml")
        with open(ov, "w") as f:
            f.write("[grounded]\nenabled = false\n")
        self.assertIn("disabled", council._load(self.parse("run", "--config", ov, "--grounded", self.repo, "--", "q"))[1])

    def test_84_runtime_key_does_not_raise_drift_rows(self):
        from councillib import drift
        cfg, _ = council._load(self.parse("run", "--grounded", self.repo, "--", "q"))
        items = drift.collect(cfg) if hasattr(drift, "collect") else {}
        self.assertFalse([k for k in items if "_grounded" in k])

    def test_85_shipped_config_validates_and_template_has_only_known_placeholders(self):
        cfg = config.load()
        self.assertEqual(grounded.problems(cfg), [])
        self.assertTrue(grounded.cfg_of(cfg)["restricted"])
        import string
        with open(os.path.join(PROMPTS, "advisor-grounded.md")) as f:
            tpl = string.Template(f.read())
        text = tpl.substitute(question="Q", context="C", seat_id="s", n_seats=3, lens_line="", root="/r", images_line="", access_line=grounded.NO_PROBE_LINE)
        for needle in ("path:LINE", "Verdict: APPROVE", "Confidence: low", "read-only access", "/r"):
            self.assertIn(needle, text)
        self.assertNotIn("Do not use files", text)


class PreflightRows(unittest.TestCase):
    def test_89_codex_key(self):
        cx = providers.get("codex")
        s = {"id": "codex", "provider": "codex", "model": "m"}
        self.assertTrue(grounded.cfg_of({})["codex"])
        self.assertEqual(grounded.seat_support({}, s, cx), (True, ""))                  # default: grounded
        off = {"grounded": {"codex": False}}
        ok, why = grounded.seat_support(off, s, cx)
        self.assertFalse(ok)
        self.assertIn("grounded.codex = false", why)
        self.assertTrue(grounded.seat_support(off, {"id": "c", "provider": "claude", "model": "m"}, providers.get("claude"))[0])
        self.assertEqual(grounded.problems(off), [])
        self.assertEqual(grounded.problems({"grounded": {"codex": "no"}}), ["grounded.codex must be true or false"])
        cfg = config.load()
        cfg["grounded"] = dict(cfg.get("grounded") or {}, codex=False)
        cfg["_grounded"] = {"root": "/repo", "images": []}
        rows = {c.check: c for c in preflight.grounded_rows(cfg)}
        self.assertEqual(rows["grounded:codex"].result, "WARN")
        self.assertIn("grounded.codex = false", rows["grounded:codex"].detail)
        self.assertEqual(rows["grounded:claude"].result, "PASS")
        seat = dict(s)
        rec = grounded.seat_grounding(off, seat, cx, {"root": "/repo", "images": []})
        self.assertEqual(rec["mode"], "ungrounded")
        self.assertNotIn("_grounded", seat)

    def test_90_rows_per_seat(self):
        cfg = config.load()
        cfg["_grounded"] = {"root": "/repo", "images": ["/s/01-a.png"]}
        rows = {c.check: c for c in preflight.grounded_rows(cfg)}
        self.assertEqual(rows["grounded_root"].result, "PASS")
        self.assertIn("01-a.png", rows["grounded_root"].detail)
        self.assertEqual(rows["grounded:claude"].result, "PASS")
        self.assertIn("Read, Grep, Glob", rows["grounded:claude"].detail)
        self.assertIn("-s read-only", rows["grounded:codex"].detail)
        self.assertEqual(rows["grounded:codex"].result, "PASS")                      # PASS with a note, not WARN
        self.assertIn("not confined to the root", rows["grounded:codex"].detail)
        self.assertNotIn("not confined", rows["grounded:claude"].detail)
        for sid in ("agy", "openrouter", "deepseek"):
            self.assertEqual(rows["grounded:" + sid].result, "WARN")
            self.assertTrue(rows["grounded:" + sid].detail.startswith("ungrounded: "))
        self.assertEqual(preflight.grounded_rows(config.load()), [])
        table = preflight.format_table(list(rows.values()))
        self.assertIn("PREFLIGHT PASS", table)

    def test_91_no_capable_seat_warns(self):
        cfg = {"_grounded": {"root": "/r", "images": []}, "seat": [{"id": "x", "provider": "openrouter", "model": "m"}]}
        got = {c.check: c for c in preflight.grounded_rows(cfg)}
        self.assertEqual(got["grounded_seats"].result, "WARN")

    def test_92_question_path_lint_inside_root_is_a_warning(self):
        q = "Read /home/example/repo/src/app.js and /tmp/other/file.txt please"
        plain = preflight.question_rows(q, [], False)
        self.assertEqual({c.result for c in plain}, {"FAIL"})
        root = preflight._norm("/home/example/repo")    # as the caller passes it (realpath'd; macOS resolves /home through the automounter)
        got = preflight.question_rows(q, [], False, root=root)
        self.assertEqual(sorted(c.result for c in got), ["FAIL", "WARN"])
        inside = [c for c in got if c.result == "WARN"][0]
        self.assertIn("inside the --grounded root", inside.detail)
        only_in = preflight.question_rows("Read /home/example/repo/src/app.js", [], False, root=root)
        self.assertEqual([c.result for c in only_in], ["WARN"])


    def test_93_probes_note_in_the_seat_row(self):
        cfg = config.load()
        cfg["_grounded"] = {"root": "/repo", "images": []}
        rows = {c.check: c for c in preflight.grounded_rows(cfg)}
        self.assertIn("probes ON", rows["grounded:codex"].detail)
        self.assertIn("[grounded] probes", rows["grounded:codex"].detail)
        self.assertNotIn("probes ON", rows["grounded:claude"].detail)
        cfg["grounded"] = dict(cfg.get("grounded") or {}, probes=["codex", "claude"])
        rows = {c.check: c for c in preflight.grounded_rows(cfg)}
        self.assertIn("probes ON", rows["grounded:claude"].detail)
        self.assertIn("Bash limited to git log", rows["grounded:claude"].detail)
        cfg["grounded"]["probes"] = []
        rows = {c.check: c for c in preflight.grounded_rows(cfg)}
        self.assertNotIn("probes ON", rows["grounded:codex"].detail)

if __name__ == "__main__":
    unittest.main()
