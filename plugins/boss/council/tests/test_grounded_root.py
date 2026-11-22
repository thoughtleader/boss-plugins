"""A risky --grounded root (owner-approved 2026-11-21): a root that holds orchestrator run folders or Claude memory
folders is refused (ask and run exit 2, preflight a FAIL row) unless --allow-grounded-root (a WARN row). Scratch HOME,
scratch folders with invented content, stub seats: no network, no paid call, nothing under the real ~/.claude."""
import builtins
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import council
from councillib import council_run, grounded, preflight
from tests.test_ask import call, overlay

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = "scratch-marker-7f3a91"                  # written into scratch rulings and memory files: must never be printed
CLEAR = ("ORCH_DIR", "ORCH_RUNS_DIR", "ORCH_PROJECTS_DIR", "BOSS_DATA", "CLAUDE_CONFIG_DIR", "COUNCIL_CONFIG")


def put(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="groot-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        p = mock.patch.dict(os.environ, {"HOME": self.home, "BOSS_PROGRESS_DIR": os.path.join(self.tmp, "progress")})
        p.start()
        self.addCleanup(p.stop)
        for k in CLEAR:
            os.environ.pop(k, None)
        self.runs = os.path.join(self.home, ".claude", "orchestrator", "runs")
        self.run1 = os.path.join(self.runs, "2026-01-01-demo")
        put(os.path.join(self.run1, "DECISIONS.md"), "- 2026-01-01 %s\n" % MARK)
        put(os.path.join(self.run1, "manifest.jsonl"), '{"note": "%s"}\n' % MARK)
        self.mem = os.path.join(self.home, ".claude", "projects", "-demo", "memory")
        put(os.path.join(self.mem, "MEMORY.md"), "- %s\n" % MARK)
        self.ov = overlay(self.tmp)

    def argv(self, cmd, root, *extra):
        if cmd == "preflight":
            return [cmd, "--free-only", "--config", self.ov, "--grounded", root] + list(extra)
        return [cmd, "--config", self.ov, "--out", os.path.join(self.tmp, "out"), "--grounded", root] + list(extra) + ["--", "q"]

    def refused(self, cmd, root):
        """Drive one verb with nothing allowed to launch or run; -> (rc, out, err)."""
        with mock.patch.object(council, "launch", side_effect=AssertionError("launched")), \
                mock.patch.object(council_run, "run", side_effect=AssertionError("ran")):
            return call(self.argv(cmd, root))


class Refused(Base):
    def cases(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(proj, ".links"))
        os.symlink(self.runs, os.path.join(proj, ".links", "orch-runs"))
        shaped = os.path.join(self.tmp, "proj2")
        put(os.path.join(shaped, "archive", "x", "DECISIONS.md"), MARK)
        put(os.path.join(shaped, "archive", "x", "manifest.jsonl"), MARK)
        memdir = os.path.join(self.tmp, "proj3")
        put(os.path.join(memdir, "docs", "memory", "MEMORY.md"), MARK)
        return {
            "root is an ancestor of the runs folder": (os.path.join(self.home, ".claude", "orchestrator"), "run folder: runs (inside the root)"),
            "root is inside a run folder": (self.run1, "run folder: . (the root is inside it)"),
            "root is an ancestor of a memory folder": (os.path.join(self.home, ".claude", "projects"),
                                                       "memory folder: -demo/memory (inside the root)"),
            "symlink into the runs folder": (proj, "run folder: .links/orch-runs -> outside the root"),
            "run-folder shape": (shaped, "run folder: archive/x (holds manifest.jsonl and DECISIONS.md)"),
            "memory/MEMORY.md": (memdir, "memory folder: docs/memory (memory/MEMORY.md)"),
        }

    def test_01_ask_and_run_exit_2_before_anything_is_sent(self):
        for name, (root, want) in self.cases().items():
            for cmd in ("ask", "run"):
                rc, out, err = self.refused(cmd, root)
                self.assertEqual(rc, 2, (name, cmd, out, err))
                self.assertIn("%s refused, nothing sent" % cmd, err, name)
                self.assertIn(want, err, (name, cmd))
                self.assertIn("--allow-grounded-root", err)
                self.assertNotIn(MARK, out + err, name)
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "out")), (name, cmd))

    def test_02_preflight_shows_a_fail_row(self):
        for name, (root, want) in self.cases().items():
            rc, out, err = self.refused("preflight", root)
            self.assertEqual(rc, 1, (name, out, err))
            self.assertRegex(out, r"grounded_risk\s+\| FAIL\s+\|", name)
            self.assertIn(want, out, name)
            self.assertIn("PREFLIGHT FAIL", out)
            self.assertNotIn(MARK, out + err, name)

    def test_03_at_most_three_findings_then_and_n_more(self):
        root = os.path.join(self.tmp, "many")
        for i in range(5):
            put(os.path.join(root, "r%d" % i, "DECISIONS.md"), MARK)
            put(os.path.join(root, "r%d" % i, "manifest.jsonl"), MARK)
        rc, out, err = self.refused("ask", root)
        self.assertEqual(rc, 2, err)
        self.assertEqual(len(re.findall(r"run folder: r\d \(", err)), 3, err)
        self.assertIn("and 2 more", err)
        self.assertNotIn(MARK, out + err)

    def test_04_a_lone_rulings_file_or_empty_folder_is_not_a_risk(self):
        root = os.path.join(self.tmp, "ok")
        put(os.path.join(root, "DECISIONS.md"), MARK)                    # no manifest.jsonl: not a run folder
        put(os.path.join(root, "notes", "MEMORY.md"), MARK)              # not in a folder named memory
        put(os.path.join(root, "memory", "notes.md"), MARK)              # a memory folder without MEMORY.md
        os.makedirs(os.path.join(root, "empty"))
        self.assertEqual(preflight.root_risk(root)["findings"], [])


class Allowed(Base):
    def test_10_warn_row_names_the_finding_and_the_run_goes_on(self):
        root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(root, ".links"))
        os.symlink(self.runs, os.path.join(root, ".links", "orch-runs"))
        for cmd in ("ask", "run", "preflight"):
            rc, out, err = call(self.argv(cmd, root, "--allow-grounded-root"))
            self.assertEqual(rc, 0, (cmd, out, err))
            self.assertRegex(out, r"grounded_risk\s+\| WARN\s+\|", cmd)
            self.assertIn("run folder: .links/orch-runs -> outside the root", out, cmd)
            self.assertNotIn(MARK, out + err, cmd)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "out")))        # ask / run really ran

    def test_11_the_flag_needs_grounded(self):
        for cmd in ("ask", "run", "preflight"):
            argv = [cmd, "--config", self.ov, "--allow-grounded-root"] + ([] if cmd == "preflight" else ["--", "q"])
            rc, out, err = call(argv)
            self.assertEqual(rc, 2, (cmd, out, err))
            self.assertIn("--allow-grounded-root needs --grounded", err)


class Clean(Base):
    def test_20_clean_root_passes_and_the_existing_rows_stay(self):
        root = os.path.join(self.tmp, "clean")
        put(os.path.join(root, "src", "a.txt"), "hello\n")
        put(os.path.join(root, "DECISIONS.md"), "lone file, not a run folder\n")
        for cmd in ("ask", "run", "preflight"):
            rc, out, err = call(self.argv(cmd, root))
            self.assertEqual(rc, 0, (cmd, out, err))
            self.assertNotRegex(out, r"grounded_risk\s+\| (FAIL|WARN)")
            self.assertIn("grounded_root", out, cmd)
        rc, out, err = call(self.argv("preflight", root))
        self.assertRegex(out, r"grounded_risk\s+\| PASS\s+\| none found within %d levels of the root \(\d+ entries read\)" % preflight.RISK_MAX_DEPTH)

    def test_21_existing_grounded_rows_are_untouched(self):
        clean = os.path.join(self.tmp, "clean")
        put(os.path.join(clean, "a.txt"), "x")
        cfg, err = council._load(council._parser().parse_args(["run", "--config", self.ov, "--grounded", clean, "--", "q"]))
        self.assertIsNone(err)
        with_risk = [(c.check, c.result, c.detail) for c in preflight.grounded_rows(cfg)]
        cfg.pop("_grounded_risk")
        self.assertEqual(with_risk, [(c.check, c.result, c.detail) for c in preflight.grounded_rows(cfg)])
        self.assertTrue(all(c[0] != "grounded_risk" for c in with_risk))
        # the Codex row still states that the seat is not confined to the root, and the risk row says nothing of it
        with open(preflight.__file__) as f:
            self.assertIn("not confined to the root", f.read())
        cfg["_grounded_risk"] = {"findings": [], "capped": False, "read": 1}
        self.assertNotIn("confined", preflight.grounded_risk_rows(cfg)[0].detail)


class NoGrounded(Base):
    def test_30_no_grounded_no_walk_no_row(self):
        os.chdir(self.run1)                                               # a cwd that would be refused as a root
        self.addCleanup(os.chdir, ROOT)
        with mock.patch.object(preflight, "root_risk", side_effect=AssertionError("walked")), \
                mock.patch.object(grounded, "resolve_root", side_effect=AssertionError("resolved a root")):
            rc, out, err = call(["preflight", "--free-only", "--config", self.ov])
            rc2, out2, err2 = call(["ask", "--config", self.ov, "--out", os.path.join(self.tmp, "out"), "--", "q"])
        self.assertEqual((rc, rc2), (0, 0), out + err + out2 + err2)
        self.assertNotIn("grounded_risk", out + out2)
        cfg, e = council._load(council._parser().parse_args(["run", "--config", self.ov, "--", "q"]))
        self.assertNotIn("_grounded_risk", cfg)
        self.assertEqual(preflight.grounded_risk_rows(cfg), [])


class Walk(Base):
    def test_40_cap_gives_a_partly_checked_warn_and_never_a_refusal(self):
        root = os.path.join(self.tmp, "wide")
        os.makedirs(root)
        for i in range(preflight.RISK_MAX_ENTRIES + 100):
            open(os.path.join(root, "f%04d" % i), "w").close()
        risk = preflight.root_risk(root)
        self.assertEqual((risk["findings"], risk["capped"], risk["read"]), ([], True, preflight.RISK_MAX_ENTRIES))
        for cmd in ("ask", "run", "preflight"):
            rc, out, err = call(self.argv(cmd, root))
            self.assertEqual(rc, 0, (cmd, out, err))
            self.assertRegex(out, r"grounded_risk\s+\| WARN\s+\| the root was only partly checked", cmd)

    def test_41_the_cap_does_not_hide_a_finding(self):
        root = os.path.join(self.tmp, "wide2")
        put(os.path.join(root, "a", "DECISIONS.md"), MARK)
        put(os.path.join(root, "a", "manifest.jsonl"), MARK)
        os.makedirs(os.path.join(root, "z"))
        for i in range(preflight.RISK_MAX_ENTRIES + 10):
            open(os.path.join(root, "z", "f%04d" % i), "w").close()
        risk = preflight.root_risk(root)
        self.assertTrue(risk["capped"])
        self.assertEqual([f[1] for f in risk["findings"]], ["a"])
        rc, out, err = self.refused("ask", root)
        self.assertEqual(rc, 2, err)
        self.assertNotIn("partly checked", err)

    def test_42_git_and_node_modules_are_not_entered(self):
        root = os.path.join(self.tmp, "repo")
        for skipped in (".git", "node_modules"):
            for name in ("DECISIONS.md", "manifest.jsonl"):
                put(os.path.join(root, skipped, "pkg", name), MARK)
            put(os.path.join(root, skipped, "pkg", "memory", "MEMORY.md"), MARK)
        put(os.path.join(root, "src", "a.txt"), "x")
        seen = []
        real = os.scandir

        def spy(p="."):
            seen.append(os.fspath(p))
            return real(p)
        with mock.patch.object(os, "scandir", spy):
            risk = preflight.root_risk(root)
        self.assertEqual(risk["findings"], [])
        self.assertFalse([p for p in seen if "/.git" in p or "node_modules" in p], seen)
        self.assertIn(os.path.join(root, "src"), seen)

    def test_43_depth_bound_and_a_symlink_is_resolved_once(self):
        root = os.path.join(self.tmp, "deep")
        put(os.path.join(root, "a", "b", "c", "DECISIONS.md"), MARK)         # depth 3: found
        put(os.path.join(root, "a", "b", "c", "manifest.jsonl"), MARK)
        self.assertEqual([f[1] for f in preflight.root_risk(root)["findings"]], ["a/b/c"])
        # a copy one level deeper is not looked at: a PASS whose text states the bound, never a WARN
        deeper = os.path.join(self.tmp, "deeper")
        put(os.path.join(deeper, "a", "b", "c", "d", "DECISIONS.md"), MARK)
        put(os.path.join(deeper, "a", "b", "c", "d", "manifest.jsonl"), MARK)
        risk = preflight.root_risk(deeper)
        self.assertEqual((risk["findings"], risk["capped"], risk["links"]), ([], False, []))
        row = preflight.grounded_risk_rows({"_grounded_risk": risk})[0]
        self.assertEqual(row.result, "PASS")
        self.assertIn("none found within %d levels of the root (" % preflight.RISK_MAX_DEPTH, row.detail)
        # a link to a folder that only holds a run folder one level down: resolved once, not followed further
        outside = os.path.join(self.tmp, "outside")
        put(os.path.join(outside, "inner", "DECISIONS.md"), MARK)
        put(os.path.join(outside, "inner", "manifest.jsonl"), MARK)
        root2 = os.path.join(self.tmp, "linked")
        os.makedirs(root2)
        os.symlink(outside, os.path.join(root2, "shortcut"))
        risk = preflight.root_risk(root2)
        self.assertEqual((risk["findings"], risk["links"]), ([], ["shortcut"]))
        row = preflight.grounded_risk_rows({"_grounded_risk": risk})[0]       # its subfolder was not read: WARN, link named
        self.assertEqual(row.result, "WARN")
        self.assertIn("only partly checked", row.detail)
        self.assertIn("shortcut", row.detail)
        self.assertIn("only the top level was read of the folder outside the root", row.detail)     # its top level was read
        self.assertNotIn("could not be read", row.detail)
        for hidden in ("inner", outside, self.tmp, MARK, "DECISIONS"):               # nothing from the target is printed
            self.assertNotIn(hidden, row.detail)
        # the same through the three verbs: a WARN never stops the run
        for cmd in ("ask", "run", "preflight"):
            rc, out, err = call(self.argv(cmd, root2))
            self.assertEqual(rc, 0, (cmd, out, err))
            self.assertRegex(out, r"grounded_risk\s+\| WARN\s+\| the root was only partly checked", cmd)
            self.assertNotIn(MARK, out + err)
        # an outside link with nothing left unread (files only, or a file) stays a PASS
        flat = os.path.join(self.tmp, "flat")
        put(os.path.join(flat, "x.txt"), MARK)
        root4 = os.path.join(self.tmp, "linked4")
        os.makedirs(root4)
        os.symlink(flat, os.path.join(root4, "to-flat"))
        os.symlink(os.path.join(flat, "x.txt"), os.path.join(root4, "to-file"))
        risk = preflight.root_risk(root4)
        self.assertEqual((risk["findings"], risk["links"]), ([], []))
        self.assertEqual(preflight.grounded_risk_rows({"_grounded_risk": risk})[0].result, "PASS")
        # a link straight to a run-shaped folder is found
        os.symlink(os.path.join(outside, "inner"), os.path.join(root2, "direct"))
        self.assertEqual(preflight.root_risk(root2)["findings"], [("run folder", "direct", "symlink -> outside the root")])
        # a link to a memory file inside the memory folder
        root3 = os.path.join(self.tmp, "linked3")
        os.makedirs(root3)
        os.symlink(os.path.join(self.mem, "MEMORY.md"), os.path.join(root3, "m.md"))
        self.assertEqual([f[:2] for f in preflight.root_risk(root3)["findings"]], [("memory folder", "m.md")])

    def test_46_an_outside_folder_that_cannot_be_listed_is_a_warn_not_a_pass(self):
        outside = os.path.join(self.tmp, "closed")
        put(os.path.join(outside, "inner", "x.txt"), MARK)
        root = os.path.join(self.tmp, "linked-closed")
        os.makedirs(root)
        os.symlink(outside, os.path.join(root, "shortcut"))
        real = os.scandir

        def scandir(path="."):
            if os.path.realpath(os.fspath(path)) in (outside, self.runs):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real(path)
        with mock.patch.object(os, "scandir", scandir):
            risk = preflight.root_risk(root)
        self.assertEqual((risk["findings"], risk["links"], risk["unread"]), ([], ["shortcut"], ["shortcut"]))
        row = preflight.grounded_risk_rows({"_grounded_risk": risk})[0]
        self.assertEqual(row.result, "WARN")
        self.assertIn("only partly checked", row.detail)
        self.assertIn("shortcut", row.detail)
        # nothing was read of that folder: the WARN says so, not "only the top level was read"
        self.assertIn("the folder outside the root that a symlink leads to could not be read: shortcut", row.detail)
        self.assertNotIn("only the top level was read", row.detail)
        for hidden in ("closed", outside, self.tmp, MARK):                           # the link's own relative path only
            self.assertNotIn(hidden, row.detail)
        # a readable files-only folder behind a link stays a PASS (nothing left unread)
        os.remove(os.path.join(root, "shortcut"))
        flat = os.path.join(self.tmp, "flat2")
        put(os.path.join(flat, "x.txt"), MARK)
        os.symlink(flat, os.path.join(root, "flat"))
        self.assertEqual(preflight.root_risk(root)["links"], [])
        # a refusal stays a refusal when the target cannot be listed: a link into the runs folder
        os.symlink(self.run1, os.path.join(root, "to-run"))
        with mock.patch.object(os, "scandir", scandir):
            self.assertEqual(preflight.root_risk(root)["findings"], [("run folder", "to-run", "symlink -> outside the root")])

    def test_47_printed_names_lose_line_separators_and_bidi_controls(self):
        for ch in ("\u2028", "\u2029", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
                   "\u200e", "\u200f", "\u061c",                      # left-to-right, right-to-left and Arabic letter marks
                   "\n", "\r", "\x1b", "\x07", "\x7f", "\x85"):
            self.assertEqual(preflight._clean("a%sb" % ch), "a?b", repr(ch))
        for ok in ("plain name.txt", "caf\u00e9", "\u00a0nbsp", "\u4e2d\u6587", "\u200b"):            # printable text, NBSP, CJK, ZWSP stay
            self.assertEqual(preflight._clean(ok), ok, repr(ok))
        # the findings text uses it too: a hostile folder name cannot add a line or flip the text
        risk = {"findings": [("run folder", "x\u2028y\u202ez", "inside the root")], "capped": False, "read": 1, "links": ["l\u2067k"]}
        self.assertEqual(preflight._risk_text(risk), "run folder: x?y?z (inside the root)")
        row = preflight.grounded_risk_rows({"_grounded_risk": {"findings": [], "capped": False, "read": 1, "links": ["l\u2067k"]}})[0]
        self.assertIn("l?k", row.detail)
        self.assertNotIn("\u2067", row.detail)

    def test_46b_one_readable_and_one_unlistable_link_each_get_their_own_wording(self):
        readable = os.path.join(self.tmp, "readable")
        put(os.path.join(readable, "inner", "x.txt"), MARK)
        closed = os.path.join(self.tmp, "closed2")
        put(os.path.join(closed, "inner", "x.txt"), MARK)
        root = os.path.join(self.tmp, "two-links")
        os.makedirs(root)
        os.symlink(readable, os.path.join(root, "open-link"))
        os.symlink(closed, os.path.join(root, "closed-link"))
        real = os.scandir

        def scandir(path="."):
            if os.path.realpath(os.fspath(path)) == closed:
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real(path)
        with mock.patch.object(os, "scandir", scandir):
            risk = preflight.root_risk(root)
        self.assertEqual((sorted(risk["links"]), risk["unread"]), (["closed-link", "open-link"], ["closed-link"]))
        detail = preflight.grounded_risk_rows({"_grounded_risk": risk})[0].detail
        self.assertIn("only the top level was read of the folder outside the root that a symlink leads to: open-link", detail)
        self.assertIn("could not be read: closed-link", detail)

    def test_48_under_confirms_a_case_variant_spelling_with_the_file_system_and_changes_nothing_else(self):
        d = os.path.join(self.tmp, "Runs")
        os.makedirs(os.path.join(d, "sub"))
        variant = os.path.join(self.tmp, "RUNS")
        # unchanged: same case, plain prefix, a sibling that only shares the start, a different folder
        self.assertTrue(preflight._under(d, d))
        self.assertTrue(preflight._under(os.path.join(d, "sub"), d))
        self.assertFalse(preflight._under(d + "-more", d))
        self.assertFalse(preflight._under(os.path.join(self.tmp, "other", "sub"), d))
        # a case-sensitive file system (this one) says the variant is another folder: not under, as before
        self.assertFalse(preflight._under(os.path.join(variant, "sub"), d))
        # a case-insensitive one says it is the same folder: compared through samefile on the leading components of p
        seen = []

        def same(a, b):
            seen.append((a, b))
            return True
        with mock.patch.object(os.path, "samefile", same):
            self.assertTrue(preflight._under(variant, d))
            self.assertTrue(preflight._under(os.path.join(variant, "sub"), d))
            self.assertEqual(seen, [(variant, d), (variant, d)])
            seen.clear()
            # only a spelling that differs by case alone reaches the file system
            self.assertFalse(preflight._under(os.path.join(self.tmp, "other"), d))
            self.assertFalse(preflight._under(d + "-more", d))
            self.assertEqual(seen, [])
        with mock.patch.object(os.path, "samefile", side_effect=OSError("gone")):
            self.assertFalse(preflight._under(variant, d))

    def test_44_it_reads_names_never_contents(self):
        opened = []
        real = builtins.open

        def spy(file, *a, **k):
            opened.append(os.fspath(file) if isinstance(file, (str, bytes, os.PathLike)) else file)
            return real(file, *a, **k)
        with mock.patch.object(builtins, "open", spy):
            preflight.root_risk(os.path.join(self.home, ".claude"))
            preflight.root_risk(self.run1)
        self.assertEqual([p for p in opened if str(self.tmp) in str(p)], [])

    def test_45_the_env_overrides_the_plugin_uses_are_honoured(self):
        other = os.path.join(self.tmp, "elsewhere", "runs")
        put(os.path.join(other, "x", "keep.txt"), "x")
        with mock.patch.dict(os.environ, {"ORCH_RUNS_DIR": other}):
            self.assertEqual(preflight.root_risk(os.path.join(self.tmp, "elsewhere"))["findings"],
                             [("run folder", "runs", "inside the root")])
            # the real runs folder is no longer the runs folder: only its run-folder shape is left to find
            self.assertEqual([f[2] for f in preflight.root_risk(os.path.join(self.home, ".claude", "orchestrator"))["findings"]],
                             ["holds manifest.jsonl and DECISIONS.md"])
        cfgdir = os.path.join(self.tmp, "cfg")
        put(os.path.join(cfgdir, "projects", "-p", "memory", "MEMORY.md"), "x")
        os.makedirs(os.path.join(cfgdir, "boss-data", "runs"))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfgdir}):
            self.assertEqual(sorted(f[1] for f in preflight.root_risk(cfgdir)["findings"]),
                             ["boss-data/runs", "projects/-p/memory"])


class Text(unittest.TestCase):
    def test_50_the_shapes_use_names_the_whole_file_refusal_knows(self):
        self.assertIn("DECISIONS.md", preflight.PRIVATE_FILES)
        self.assertIn("MEMORY.md", preflight.PRIVATE_FILES)
        self.assertIn(preflight.RUN_SHAPE[1], preflight.PRIVATE_FILES)
        self.assertIn(preflight.MEMORY_SHAPE[1], preflight.PRIVATE_FILES)

    def test_51_the_flag_is_in_the_help_and_the_skill(self):
        self.assertIn("--allow-grounded-root", council.ASK_HELP)
        for cmd in ("ask", "run", "preflight"):
            self.assertTrue(council._parser().parse_args([cmd, "--allow-grounded-root"]).allow_grounded_root)
        skill = os.path.join(os.path.dirname(ROOT), "skills", "council", "SKILL.md")
        with open(skill) as f:
            text = f.read()
        self.assertGreaterEqual(text.count("--allow-grounded-root"), 2)


if __name__ == "__main__":
    unittest.main()
