"""Row 14 (run example-run): the optional critique round, `--depth quick|standard`.

Stub processes only: no network, no paid call. A critique is display only: no test here may need it for quorum, status or
the exit code.

Run: python3 -Es -m unittest tests.test_critique   (cwd ~/.claude/council)
"""
import json
import os
import shutil
import signal
import string
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import council  # noqa: E402
from councillib import council_run, critique, preflight, progress_mirror, providers, status  # noqa: E402
from tests.test_council_run import Base, HedgeBase, PY, hseat, pids_launched, seat  # noqa: E402
from tests.test_grounded import make_repo  # noqa: E402

PROMPTS = os.path.join(ROOT, "prompts")


class Helpers(unittest.TestCase):
    def test_01_letters_follow_roster_order_and_run_past_z(self):
        self.assertEqual(critique.letters(["claude", "codex", "agy"]), {"claude": "A", "codex": "B", "agy": "C"})
        self.assertEqual([critique.letter(i) for i in (0, 25, 26, 27, 51, 52)], ["A", "Z", "AA", "AB", "AZ", "BA"])

    def test_02_depth_and_call_estimate(self):
        self.assertEqual(critique.depth_of({}), "quick")
        self.assertEqual(critique.depth_of({"_depth": "standard"}), "standard")
        self.assertEqual(critique.depth_of({"_depth": "bogus"}), "quick")
        self.assertEqual([critique.estimate_calls(n, "quick") for n in (0, 3, 5)], [1, 4, 6])
        self.assertEqual([critique.estimate_calls(n, "standard") for n in (0, 3, 5)], [1, 7, 11])

    def test_03_peers_are_letters_and_lenses_never_ids(self):
        block = critique.peers_block([("B", "security: abuse", "Answer from B."), ("C", "", "Answer from C.")])
        self.assertIn("### Seat B (lens: security: abuse)", block)
        self.assertIn("### Seat C\n", block)
        self.assertNotIn("claude", block)

    def test_04_long_peer_answer_is_cut_with_a_marker(self):
        text = critique.clip("x" * 100, 40)
        self.assertTrue(text.startswith("x" * 40))
        self.assertIn("cut to fit", text)
        self.assertEqual(critique.clip("short"), "short")

    def test_05_changed_view_and_verdict_change(self):
        self.assertEqual(critique.parse_changed("x\nChanged view: yes\nVerdict: REVISE"), "yes")
        self.assertEqual(critique.parse_changed("**Changed view:** No"), "no")
        self.assertEqual(critique.parse_changed("Changed view: no\n...\nChanged view: yes"), "yes")
        self.assertIsNone(critique.parse_changed("nothing here"))
        self.assertIsNone(critique.parse_changed(None))
        self.assertEqual(critique.verdict_change("APPROVE", "REVISE"), {"from": "APPROVE", "to": "REVISE"})
        self.assertIsNone(critique.verdict_change("APPROVE", "APPROVE"))
        self.assertIsNone(critique.verdict_change(None, "REVISE"))

    def test_06_shipped_templates_render(self):
        for name in ("critique.md", "critique-grounded.md"):
            with open(os.path.join(PROMPTS, name)) as f:
                tpl = string.Template(f.read())
            extra = (__import__("councillib.grounded", fromlist=["x"]).prompt_vars({"root": "/r", "images": []})
                     if "grounded" in name else {})
            text = tpl.substitute(question="Q-TEXT", context="C-TEXT",
                                  **critique.prompt_vars("B", "MY-ANSWER", [("A", "", "THEIRS")], 3, "lens-x"), **extra)
            self.assertNotIn("$", text)
            for needle in ("Q-TEXT", "C-TEXT", "MY-ANSWER", "### Seat A", "THEIRS", "Seat B", "lens-x", "Changed view:",
                           "Verdict: APPROVE"):
                self.assertIn(needle, text, name)

    def test_07_quick_chair_prompt_has_no_critique_text(self):
        with open(os.path.join(PROMPTS, "chair.md")) as f:
            tpl = string.Template(f.read())
        quick = council_run.chair_prompt(tpl, "Q", "A")
        self.assertNotIn("ritique", quick)
        self.assertNotIn("$", quick)
        std = council_run.chair_prompt(tpl, "Q", "A", "CRIT-BLOCK")
        for needle in ("two-round council", "11. Critique round", "## Critiques (round 2)", "CRIT-BLOCK",
                       "which critiques changed"):
            self.assertIn(needle, std)
        self.assertTrue(std.startswith(quick.split("\n")[0]))

    def test_08_item_11_tells_the_chair_how_many_views_changed_and_quick_stays_byte_identical(self):
        # P28 (run example-run, row 24): in code, through ${critique_item}, not in chair.md
        with open(os.path.join(PROMPTS, "chair.md")) as f:
            tpl = string.Template(f.read())
        plain = tpl.substitute(question="Q", answers="A", critique_guide="", critique_item="", critiques="")
        self.assertEqual(council_run.chair_prompt(tpl, "Q", "A"), plain)
        self.assertEqual(council_run.chair_prompt(tpl, "Q", "A", None, (["a"], 3)), plain)   # quick ignores a count
        std = council_run.chair_prompt(tpl, "Q", "A", "CRIT-BLOCK", (["a", "c"], 3))
        self.assertIn("2 of 3 critiques, seat a, seat c.", std)
        self.assertIn("Begin this item with the line `critique_changes: N`", std)
        none = council_run.chair_prompt(tpl, "Q", "A", "CRIT-BLOCK", ([], 2))
        self.assertIn("0 of 2 critiques, none.", none)
        self.assertNotIn("$", std)
        # without a count (an older caller) item 11 is what it was
        self.assertNotIn("critique_changes", council_run.chair_prompt(tpl, "Q", "A", "CRIT-BLOCK"))
        self.assertTrue(critique.view_changed("yes", None))
        self.assertTrue(critique.view_changed("no", {"from": "APPROVE", "to": "REVISE"}))
        self.assertFalse(critique.view_changed("no", None))
        self.assertFalse(critique.view_changed(None, None))


class RunBase(Base):
    def setUp(self):
        super().setUp()
        for name in ("critique.md", "critique-grounded.md", "advisor-grounded.md"):
            shutil.copy(os.path.join(PROMPTS, name), os.path.join(self.prompts, name))
        shutil.copy(os.path.join(PROMPTS, "chair.md"), os.path.join(self.prompts, "chair.md"))
        with open(os.path.join(self.prompts, "advisor.md"), "w") as f:
            f.write("${lens_line}SEAT $seat_id of $n_seats\nQ: $question\nC: $context\n")

    def answer_file(self, name, text):
        path = os.path.join(self.tmp, "ans-%s.txt" % name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def aseat(self, sid, text, crit="echo-stdin", provider="stub", **kw):
        """A seat that answers `text` in round 1 and runs stub mode `crit` in round 2 (a mode string or a (mode, args) list)."""
        kw["stub_critique_mode"] = crit
        return seat(sid, ["replay", self.answer_file(sid, text), "0"], provider=provider, **kw)

    def scfg(self, seats, depth="standard", **kw):
        extra = {"_depth": depth}
        extra.update(kw.pop("extra", {}))
        return self.cfg(seats, extra=extra, **kw)

    def lines(self, name):
        with open(os.path.join(self.state, name)) as f:
            return [json.loads(ln) for ln in f if ln.strip()]


A = "Ship it. The plan A is sound.\nVerdict: APPROVE\nConfidence: high\n"
B = "Fix the cache first.\nVerdict: REVISE\nConfidence: medium\n"
C = "Block: it loses data.\nVerdict: BLOCK\nConfidence: high\n"


class StandardRun(RunBase):
    def test_10_each_seat_critiques_the_others_by_letter_and_the_chair_gets_it_all(self):
        seats = [self.aseat("a", A), self.aseat("b", B), self.aseat("c", C)]
        self.assertEqual(self.run_council(self.scfg(seats)), 0)
        crit_a = self.read("seats", "a.critique.md")
        self.assertIn("You are Seat A, one of 3 advisors", crit_a)
        self.assertIn("## Your own round 1 answer (Seat A)", crit_a)
        self.assertIn("Ship it. The plan A is sound.", crit_a)
        self.assertIn("### Seat B", crit_a)
        self.assertIn("Fix the cache first.", crit_a)
        self.assertIn("### Seat C", crit_a)
        self.assertIn("Block: it loses data.", crit_a)
        # a critic reads letters, never seat ids, providers or models
        for needle in ("seat a |", "seat b |", "seat c |", "provider stub", "stub-model", "seat id"):
            self.assertNotIn(needle, crit_a.lower())
        crit_b = self.read("seats", "b.critique.md")
        self.assertEqual(crit_b.count("Ship it. The plan A is sound."), 1)       # a's answer once, as a peer
        self.assertIn("## Your own round 1 answer (Seat B)", crit_b)
        chair_in = self.read("chair", "synthesis.md")
        self.assertIn("## Critiques (round 2)", chair_in)
        self.assertIn("Seat A = seat a, Seat B = seat b, Seat C = seat c", chair_in)
        self.assertIn("critique by seat a (Seat A) | status ok | round 1 verdict APPROVE -> revised verdict ", chair_in)
        self.assertIn("11. Critique round", chair_in)
        self.assertIn("seat a | provider stub | model stub-model | status ok | verdict APPROVE", chair_in)
        summ = self.summary()
        self.assertEqual(summ["depth"], "standard")
        info = summ["critique"]
        self.assertTrue(info["ran"])
        self.assertEqual(info["letters"], {"a": "A", "b": "B", "c": "C"})
        self.assertEqual(info["critics"], ["a", "b", "c"])
        self.assertEqual(info["ok"], ["a", "b", "c"])
        self.assertEqual(info["failed"], {})
        self.assertGreaterEqual(info["total_s"], 0)
        rec = self.by_id(summ)["a"]
        self.assertEqual(rec["verdict"], "APPROVE")                               # round 1 is never overwritten
        self.assertEqual(rec["critique"]["status"], "ok")
        self.assertEqual(rec["critique"]["letter"], "A")
        self.assertEqual(rec["critique"]["round1_verdict"], "APPROVE")
        for sid in "abc":
            for ext in ("md", "raw", "json"):
                self.assertTrue(os.path.isfile(os.path.join(self.out, "seats", "%s.critique.%s" % (sid, ext))))

    def test_11_critique_verdict_changed_view_and_verdict_change_are_recorded_apart(self):
        text = "I now agree with B.\nChanged view: yes\nVerdict: REVISE\nConfidence: medium\n"
        no = ["replay", self.answer_file("no", "Still the same.\nChanged view: no\nVerdict: REVISE\n"), "0"]
        no_c = ["replay", self.answer_file("no_c", "Still the same.\nChanged view: no\nVerdict: BLOCK\n"), "0"]
        seats = [self.aseat("a", A, ["replay", self.answer_file("a2", text), "0"]), self.aseat("b", B, no),
                 self.aseat("c", C, no_c)]
        self.assertEqual(self.run_council(self.scfg(seats)), 0)
        crit = self.by_id(self.summary())["a"]["critique"]
        self.assertEqual(crit["verdict"], "REVISE")
        self.assertEqual(crit["changed_view"], "yes")
        self.assertEqual(crit["verdict_change"], {"from": "APPROVE", "to": "REVISE"})
        info = self.summary()["critique"]
        self.assertEqual(info["changed_view"], ["a"])
        self.assertEqual(info["verdict_changes"], {"a": {"from": "APPROVE", "to": "REVISE"}})
        chair_in = self.read("chair", "synthesis.md")
        self.assertIn("critique by seat a (Seat A) | status ok | round 1 verdict APPROVE -> revised verdict REVISE | "
                      "changed view yes", chair_in)

    def test_11b_chair_gets_the_count_of_changed_views(self):
        # P28: a says yes (and its verdict moved), b says no with the same verdict, c says no but its verdict moved:
        # two of three seats changed their view, each counted once
        yes = ["replay", self.answer_file("y", "I now agree with B.\nChanged view: yes\nVerdict: REVISE\n"), "0"]
        no = ["replay", self.answer_file("n", "Still the same.\nChanged view: no\nVerdict: REVISE\n"), "0"]
        moved = ["replay", self.answer_file("m", "Hm.\nChanged view: no\nVerdict: REVISE\n"), "0"]
        seats = [self.aseat("a", A, yes), self.aseat("b", B, no), self.aseat("c", C, moved)]
        self.assertEqual(self.run_council(self.scfg(seats)), 0)
        chair_in = self.read("chair", "synthesis.md")
        self.assertIn("2 of 3 critiques, seat a, seat c.", chair_in)
        self.assertIn("`critique_changes: N`", chair_in)
        info = self.summary()["critique"]
        self.assertEqual(sorted(set(info["changed_view"]) | set(info["verdict_changes"])), ["a", "c"])

    def test_12_a_seat_that_failed_round_1_writes_no_critique_and_is_not_shown(self):
        seats = [self.aseat("a", A), self.aseat("b", B), seat("c", "exit 1", stub_critique_mode="echo-stdin")]
        self.assertEqual(self.run_council(self.scfg(seats)), 0)
        self.assertFalse(os.path.exists(os.path.join(self.out, "seats", "c.critique.md")))
        crit_a = self.read("seats", "a.critique.md")
        self.assertIn("one of 3 advisors", crit_a)           # c still holds its letter, so the key stays stable
        self.assertIn("### Seat B", crit_a)
        self.assertNotIn("### Seat C", crit_a)
        chair_in = self.read("chair", "synthesis.md")
        self.assertIn("### no critique by seat c (Seat C): it gave no round 1 answer", chair_in)
        info = self.summary()["critique"]
        self.assertEqual(info["critics"], ["a", "b"])
        self.assertNotIn("critique", self.by_id(self.summary())["c"])

    def test_13_fewer_than_two_answers_skips_the_round_and_the_chair_prompt_stays_quick(self):
        seats = [self.aseat("a", A), seat("b", "exit 1"), seat("c", "exit 1")]
        self.assertEqual(self.run_council(self.scfg(seats, min_ok=1)), 0)
        info = self.summary()["critique"]
        self.assertFalse(info["ran"])
        self.assertIn("needs at least 2", info["reason"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "seats", "a.critique.md")))
        chair_in = self.read("chair", "synthesis.md")
        self.assertNotIn("Critiques (round 2)", chair_in)
        self.assertNotIn("11. Critique round", chair_in)

    def test_14_a_failed_critique_never_fails_the_run(self):
        seats = [self.aseat("a", A), self.aseat("b", B, "exit 1"), self.aseat("c", C)]
        self.assertEqual(self.run_council(self.scfg(seats, min_ok="n")), 0)    # quorum is round 1's alone
        summ = self.summary()
        self.assertEqual(summ["quorum"]["ok"], 3)
        recs = self.by_id(summ)
        self.assertEqual(recs["b"]["status"], "ok")                            # the seat's answer is untouched
        self.assertEqual(recs["b"]["critique"]["status"], "exit_1")
        self.assertEqual(summ["critique"]["failed"], {"b": "exit_1"})
        self.assertEqual(summ["critique"]["ok"], ["a", "c"])
        chair_in = self.read("chair", "synthesis.md")
        self.assertIn("critique by seat b (Seat B) | status exit_1", chair_in)
        self.assertIn("(no critique)", chair_in)
        self.assertEqual(summ["chair"]["status"], "ok")

    def test_15_critique_gets_the_one_salvage_like_round_1(self):
        salv = self.answer_file("salv", "Salvaged: B is right.\nChanged view: yes\nVerdict: REVISE\n")
        seats = [self.aseat("a", A), self.aseat("b", B, "exit 0", stub_salvage_mode=["replay", salv, "0"]),
                 self.aseat("c", C)]
        cfg = self.scfg(seats, extra={"salvage": {"enabled": True, "max_output_tokens": 8000, "timeout": 30,
                                                    "max_partial_chars": 120000}})
        self.assertEqual(self.run_council(cfg), 0)
        crit = self.by_id(self.summary())["b"]["critique"]
        self.assertEqual((crit["status"], crit["salvaged"], crit["salvaged_from"]), ("ok", True, "empty_output"))
        self.assertEqual(crit["verdict"], "REVISE")
        self.assertTrue(os.path.isfile(os.path.join(self.out, "seats", "b.critique.salvage.raw")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "seats", "b.salvage.raw")))   # round 1's slot is untouched
        self.assertIn("SALVAGED from status empty_output", self.read("chair", "synthesis.md"))
        kinds = sorted(r["kind"] for r in self.lines("latency.jsonl"))
        self.assertEqual(kinds.count("critique"), 3)
        self.assertEqual(kinds.count("salvage"), 1)

    def test_16_lens_is_shown_next_to_the_letter_and_the_critic_keeps_its_own(self):
        seats = [self.aseat("a", A, lens="security: abuse"), self.aseat("b", B, lens="product: scope"), self.aseat("c", C)]
        self.assertEqual(self.run_council(self.scfg(seats, extra={"lenses": {"enabled": True}})), 0)
        crit_a = self.read("seats", "a.critique.md")
        self.assertIn("Your lens in round 1 was: security: abuse.", crit_a)
        self.assertIn("### Seat B (lens: product: scope)", crit_a)
        self.assertIn("### Seat A (lens: security: abuse)", self.read("seats", "b.critique.md"))
        self.assertNotIn("Your lens in round 1 was: security", self.read("seats", "b.critique.md"))
        self.assertNotIn("Your lens in round 1", self.read("seats", "c.critique.md"))

    def test_17_latency_records_every_critique_call(self):
        seats = [self.aseat("a", A), self.aseat("b", B)]
        self.assertEqual(self.run_council(self.scfg(seats)), 0)
        rows = [r for r in self.lines("latency.jsonl") if r["kind"] == "critique"]
        self.assertEqual(len(rows), 2)

    def test_18_status_and_events_show_round_2_per_seat(self):
        seats = [self.aseat("a", A), self.aseat("b", B)]
        self.assertEqual(self.run_council(self.scfg(seats)), 0)
        snap = status.load(self.out)
        self.assertEqual(snap["depth"], "standard")
        by = {s["id"]: s for s in snap["seats"]}
        self.assertEqual(by["a"]["critique"]["phase"], "done")
        self.assertEqual(by["a"]["critique"]["letter"], "A")
        self.assertEqual(by["a"]["critique"]["round1_verdict"], "APPROVE")
        self.assertEqual(by["a"]["phase"], "done")                              # the seat's own phase stays round 1's
        self.assertEqual(len(by["a"]["calls"]), 1)                              # a critique is not a retry
        self.assertIn("critique ok", status.render(snap))
        ev = os.path.join(self.out, "events.jsonl")
        if os.path.exists(ev):                                                  # the boss mirror is optional
            with open(ev) as f:
                ids = {json.loads(ln).get("id") for ln in f if ln.strip()}
            self.assertIn("seat:a/critique", ids)

    def test_19_quick_is_unchanged(self):
        seats = [self.aseat("a", A), self.aseat("b", B)]
        self.assertEqual(self.run_council(self.scfg(seats, depth="quick")), 0)
        summ = self.summary()
        self.assertNotIn("depth", summ)
        self.assertNotIn("critique", summ)
        self.assertNotIn("critique", self.by_id(summ)["a"])
        self.assertEqual([f for f in os.listdir(os.path.join(self.out, "seats")) if "critique" in f], [])
        snap = status.load(self.out)
        self.assertNotIn("depth", snap)
        self.assertNotIn("critique", snap["seats"][0])
        chair_in = self.read("chair", "synthesis.md")
        self.assertNotIn("ritique", chair_in)
        self.assertEqual(len(self.lines("latency.jsonl")), 3)                  # two seats and the chair, no extra call

    def test_20_default_cfg_without_the_key_is_quick(self):
        seats = [self.aseat("a", A), self.aseat("b", B)]
        self.assertEqual(self.run_council(self.cfg(seats)), 0)
        self.assertNotIn("critique", self.summary())

    def test_21_missing_critique_template_fails_early_only_for_standard(self):
        os.remove(os.path.join(self.prompts, "critique.md"))
        seats = [self.aseat("a", A), self.aseat("b", B)]
        self.assertEqual(self.run_council(self.scfg(seats, depth="quick")), 0)
        self.assertEqual(self.run_council(self.scfg(seats)), 2)

    def test_22_a_critique_call_gets_the_seat_limits_not_the_salvage_ones(self):
        cfg = self.scfg([self.aseat("a", A)])
        s = cfg["seat"][0]
        lat = os.path.join(self.tmp, "lat.jsonl")
        got = council_run._limits(cfg, s, "critique", lat, 1000)
        want = council_run._limits(cfg, s, "seat", lat, 1000)
        self.assertEqual((got[0].__dict__, got[1:]), (want[0].__dict__, want[1:]))
        salv = council_run._limits(cfg, s, "salvage", lat, 1000)
        self.assertNotEqual(got[0].__dict__, salv[0].__dict__)

    def test_23_a_signal_during_round_2_keeps_the_critique_partial_and_leaves_no_orphans(self):
        seats = [self.aseat("a", A, "partial-then-hang"), self.aseat("b", B, "partial-then-hang")]
        cfg_path = os.path.join(self.tmp, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump(self.scfg(seats), f)
        harness = os.path.join(self.tmp, "harness.py")
        with open(harness, "w") as f:
            f.write("import json, sys\nsys.path.insert(0, %r)\nfrom councillib import council_run\n"
                    "cfg = json.load(open(sys.argv[1]))\n"
                    "sys.exit(council_run.run(cfg, 'Q?', [], sys.argv[2]))\n" % ROOT)
        proc = subprocess.Popen([PY, "-Es", harness, cfg_path, self.out], env=dict(os.environ),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.tmp)
        try:
            end = time.monotonic() + 20
            while time.monotonic() < end and len(pids_launched(self.pidfile)) < 4:    # two seats, then two critiques
                time.sleep(0.1)
            time.sleep(1.0)                                                          # let PARTIAL-123 arrive
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(rc, 128 + signal.SIGTERM)
        rec = self.by_id(self.summary())
        for sid in "ab":
            self.assertEqual(rec[sid]["status"], "ok")                                # round 1 finished before the stop
            self.assertEqual(rec[sid]["critique"]["status"], "signal_kill")
            self.assertIn("PARTIAL-123", self.read("seats", "%s.critique.md" % sid))
        time.sleep(0.5)
        with open(self.pidfile) as f:
            pids = [int(ln.split()[1]) for ln in f if ln.strip()]
        for pid in pids:
            try:
                with open("/proc/%d/stat" % pid) as f:
                    state = f.read().rsplit(")", 1)[1].split()[0]
            except OSError:
                continue
            self.assertEqual(state, "Z", "stub pid %d outlived the run" % pid)


class CritiqueGrounded(RunBase):
    LOG = []

    def setUp(self):
        super().setUp()

        class GAdapter(providers.StubAdapter):
            name = "gstub"
            grounded_capable = True

            def build(inner, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
                CritiqueGrounded.LOG.append((seat.get("id"), bool(seat.get("_critique")), seat.get("_grounded")))
                return super().build(seat, prompt_path, workdir, limits, cfg, launch_opts)

        del CritiqueGrounded.LOG[:]
        providers.register(GAdapter())
        providers.FAMILIES.setdefault("gstub", "stub")
        self.addCleanup(providers._REGISTRY.pop, "gstub", None)
        self.repo, _ = make_repo(self.tmp)

    def test_30_a_tool_capable_seat_keeps_its_read_only_access_in_round_2(self):
        seats = [self.aseat("a", A, provider="gstub"), self.aseat("b", B)]
        cfg = self.scfg(seats, extra={"_grounded": {"root": os.path.realpath(self.repo), "images": []}})
        self.assertEqual(self.run_council(cfg), 0)
        log = {(sid, crit): g for sid, crit, g in CritiqueGrounded.LOG}
        self.assertEqual(log[("a", False)]["root"], os.path.realpath(self.repo))
        self.assertEqual(log[("a", True)], log[("a", False)])                    # round 2 has the same access
        crit_a = self.read("seats", "a.critique.md")
        self.assertIn(os.path.realpath(self.repo), crit_a)                       # critique-grounded.md
        self.assertNotIn(os.path.realpath(self.repo), self.read("seats", "b.critique.md"))   # b is text only
        rec = self.by_id(self.summary())
        self.assertEqual(rec["a"]["critique"]["grounding"]["mode"], "grounded")
        self.assertEqual(rec["b"]["critique"]["grounding"]["mode"], "ungrounded")
        self.assertIn("grounded", self.read("chair", "synthesis.md"))


class CritiqueWatch(HedgeBase):
    """A hedged (watch mode) seat takes part in round 2 like any other: its critique is a plain extra call, never hedged."""

    def setUp(self):
        super().setUp()
        for name in ("critique.md", "critique-grounded.md"):
            shutil.copy(os.path.join(PROMPTS, name), os.path.join(self.prompts, name))
        shutil.copy(os.path.join(PROMPTS, "chair.md"), os.path.join(self.prompts, "chair.md"))

    def test_50_hedged_seat_critiques_in_watch_mode_without_a_hedge_of_its_own(self):
        crit = os.path.join(self.tmp, "crit.txt")
        with open(crit, "w") as f:
            f.write("Fine.\nChanged view: no\nVerdict: APPROVE\n")
        seats = [seat("a"), seat("b"),
                 hseat("x", "quick", hedge_mode="quick", stub_critique_mode=["replay", crit, "0"])]
        cfg = self.hcfg(seats, at=30, extra={"_depth": "standard"})
        self.assertEqual(self.run_council(cfg), 0)
        summ = self.summary()
        self.assertEqual(summ["critique"]["critics"], ["a", "b", "x"])
        x = self.by_id(summ)["x"]
        self.assertEqual(x["critique"]["status"], "ok")
        self.assertEqual(x["critique"]["verdict"], "APPROVE")
        self.assertEqual(x["mode"], "watch")
        self.assertEqual([c["call"] for c in self.calls()], [])            # no hedge or fallback call anywhere
        self.assertEqual(x["status"], "ok")
        self.assert_no_survivors()


class Surfaces(unittest.TestCase):
    def test_40_cli_flag_sets_the_runtime_key(self):
        p = council._parser()
        self.assertIsNone(p.parse_args(["run", "--", "q"]).depth)
        for cmd in ("run", "preflight"):
            self.assertEqual(p.parse_args([cmd, "--depth", "standard"]).depth, "standard")
        with self.assertRaises(SystemExit):
            council._parser().parse_args(["run", "--depth", "deep"])
        cfg, err = council._load(p.parse_args(["run", "--depth", "standard", "--", "q"]))
        self.assertIsNone(err)
        self.assertEqual(cfg["_depth"], "standard")
        cfg, err = council._load(p.parse_args(["run", "--", "q"]))
        self.assertNotIn("_depth", cfg)

    def test_41_preflight_row_shows_depth_and_the_call_estimate(self):
        cfg = {"seat": [{"id": i} for i in "abc"]}
        (row,) = preflight.depth_rows(cfg)
        self.assertEqual((row.check, row.result), ("depth", "PASS"))
        self.assertIn("quick", row.detail)
        self.assertIn("about 4 model calls for 3 seats", row.detail)
        (row,) = preflight.depth_rows(dict(cfg, _depth="standard"))
        self.assertIn("standard", row.detail)
        self.assertIn("about 7 model calls for 3 seats (2N+1", row.detail)

    def test_42_progress_mirror_item_is_its_own_item_not_a_retry(self):
        sd = {"id": "a", "provider": "stub", "model": "m", "phase": "done", "status": "ok", "call": "original",
              "calls": [{"call": "original", "phase": "done", "status": "ok"}],
              "critique": {"letter": "A", "phase": "failed", "call": "critique", "status": "exit_1", "round1_verdict": "APPROVE"}}
        items = progress_mirror.seat_items(sd)
        self.assertEqual([it["id"] for it in items], ["seat:a", "seat:a/original", "seat:a/critique"])
        crit = items[-1]
        self.assertEqual((crit["kind"], crit["state"], crit["parent"], crit["error"]), ("critique", "failed", "seat:a", "exit_1"))
        self.assertNotIn("replaces", crit)
        sd.pop("critique")
        self.assertEqual(len(progress_mirror.seat_items(sd)), 2)

    def test_43_status_signature_moves_with_round_2(self):
        base = {"phase": "critique", "seats": [{"id": "a", "phase": "done", "critique": {"phase": "waiting"}}]}
        later = {"phase": "critique", "seats": [{"id": "a", "phase": "done", "critique": {"phase": "streaming"}}]}
        self.assertNotEqual(status._sig(base), status._sig(later))


if __name__ == "__main__":
    unittest.main()
