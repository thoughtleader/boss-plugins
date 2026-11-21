"""Answer-salvage tests (row 16): stub adapters + stub CLI only. No network, no paid call.

`rstub` is a stub provider whose ORIGINAL run is reasoning-only (the extractor never yields a final
answer; its stdout is the "reasoning"), while its SALVAGE call (seat key `_salvage`, set by
`salvage_seat`) is read as plain text. `stub_salvage_mode` picks the stub CLI mode of that second call:
`echo-stdin` returns the salvage prompt itself, so the tests can read what the seat was asked.

Run: python3 -Es -m unittest tests.test_salvage   (cwd ~/.claude/council)
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import council_run, providers  # noqa: E402
from tests.test_council_run import Base, pids_launched, seat  # noqa: E402

INSTR = "Give your final answer now, concisely, without further deliberation."


class ReasonOnlyExtractor(providers.RawTextExtractor):
    """Exit 0, reasoning on stdout, no answer: the seat ends `empty_output` with the reasoning as partial."""

    def final_text(self):
        return None

    def partial_text(self):
        t = super().partial_text()
        return providers.THINKING_NOTE + t if t.strip() else ""


class ReasonAdapter(providers.StubAdapter):
    name = "rstub"
    caps = []                       # (seat id, max_tokens) of every salvage_seat call
    built = []                      # (seat id, role) of every salvage launch

    def build(self, seat, prompt_path, workdir, limits, cfg):
        if seat.get("_salvage"):
            ReasonAdapter.built.append((seat.get("id"), seat.get("role")))
        return super().build(seat, prompt_path, workdir, limits, cfg)

    def salvage_seat(self, seat, cfg, max_tokens):
        ReasonAdapter.caps.append((seat.get("id"), max_tokens))
        return super().salvage_seat(seat, cfg, max_tokens)

    def extractor(self, seat=None):
        seat = seat or {}
        if seat.get("_salvage") and not seat.get("salvage_reasons_again"):
            return providers.RawTextExtractor(seat.get("model"))
        return ReasonOnlyExtractor(seat.get("model"))


class ErrOnlyOnce(providers.RawTextExtractor):
    def error(self):
        return "stub-adapter-error"


class PartialErrAdapter(providers.StubAdapter):
    """Exit 0 with output and an adapter-reported error (agy-style); its salvage call is a plain read."""
    name = "pstub"

    def extractor(self, seat=None):
        seat = seat or {}
        return (providers.RawTextExtractor(seat.get("model")) if seat.get("_salvage")
                else ErrOnlyOnce(seat.get("model")))


providers.FAMILIES.setdefault("rstub", "stub")
providers.FAMILIES.setdefault("pstub", "stub")


def rseat(sid, mode="echo-stdin", salvage="echo-stdin", provider="rstub", **kw):
    return seat(sid, mode, provider, stub_salvage_mode=salvage, **kw)


class SalvageBase(Base):
    def setUp(self):
        super().setUp()
        ReasonAdapter.caps, ReasonAdapter.built = [], []
        for cls in (ReasonAdapter, PartialErrAdapter):
            providers.register(cls())
            self.addCleanup(providers._REGISTRY.pop, cls.name, None)

    def cfg2(self, seats, salvage=True, mode="stream", extra=None, **kw):
        ex = {"salvage": {"enabled": True, "max_output_tokens": 8000, "timeout": 30,
                          "max_partial_chars": 120000} if salvage is True else salvage}
        ex.update(extra or {})
        cfg = self.cfg(seats, mode=mode, extra=ex, **kw)
        cfg["providers"]["rstub"] = {"mode": mode}
        cfg["providers"]["pstub"] = {"mode": mode}
        return cfg

    def seat_md(self, sid):
        with open(os.path.join(self.out, "seats", sid + ".md")) as f:
            return f.read()

    def chair_md(self):
        with open(os.path.join(self.out, "chair", "synthesis.md")) as f:
            return f.read()



class SalvageTests(SalvageBase):
    # ------------------------------------------------------------------ 1
    def test_1_reasoning_only_seat_is_salvaged_and_counts_toward_quorum(self):
        # min_ok "n": without the salvage this council misses quorum (3 of 3 needed, 2 ok)
        seats = [seat("a"), seat("b"), rseat("c")]
        code = self.run_council(self.cfg2(seats, min_ok="n"), question="What is 2+2?")
        self.assertEqual(code, 0)
        summ = self.summary()
        c = self.by_id(summ)["c"]
        self.assertEqual((c["status"], c["detail"], c["salvaged"], c["salvaged_from"]),
                         ("ok", "salvaged", True, "empty_output"))
        self.assertEqual(c["salvage"]["status"], "ok")
        self.assertEqual(summ["quorum"]["ok"], 3)
        self.assertIsNotNone(summ["quorum"]["reached_at_s"])
        md = self.seat_md("c")
        self.assertIn("SALVAGED from status=empty_output", md)
        # the salvage prompt: original prompt, the seat's own reasoning, the instruction
        self.assertIn("What is 2+2?", md)
        self.assertIn("SALVAGE: your earlier attempt", md)
        self.assertIn(INSTR, md)
        self.assertNotIn(providers.THINKING_NOTE.strip(), md.split("---", 1)[1].split("SALVAGE:", 1)[0])
        self.assertTrue(os.path.isfile(os.path.join(self.out, "seats", "c.salvage.raw")))
        # the chair is told
        chair = self.chair_md()
        self.assertIn("SALVAGED from status empty_output", chair)
        self.assertIn("seat c | provider rstub | model stub-model | status ok", chair)
        # the salvage call is logged as its own kind, never as a seat latency sample
        with open(os.path.join(self.state, "latency.jsonl")) as f:
            kinds = [json.loads(ln)["kind"] for ln in f]
        self.assertEqual(sorted(kinds), ["chair", "salvage", "test", "test", "test"])

    def test_2_salvage_prompt_carries_the_seats_own_reasoning_cut_from_the_front(self):
        big = "x" * 5000 + "TAIL-MARK"
        # the seat's stub CLI echoes its prompt, so its "reasoning" is the advisor prompt (question first, then the
        # TAIL-MARK); with max_partial_chars 60 only the END of it can come back
        seats = [seat("a"), rseat("c")]
        cfg = self.cfg2(seats, min_ok="n", extra={"salvage": {"enabled": True, "timeout": 30, "max_partial_chars": 60}})
        code = self.run_council(cfg, question="Q-" + big)
        self.assertEqual(code, 0)
        md = self.seat_md("c")
        self.assertIn("[... the start of it was cut to fit ...]", md)
        self.assertIn("TAIL-MARK", md)                       # the end of the reasoning survives
        self.assertIn(INSTR, md)

    # ------------------------------------------------------------------ 2
    def test_3_failed_salvage_leaves_the_seat_failed(self):
        seats = [seat("a"), seat("b"), rseat("c", salvage="exit 1")]
        code = self.run_council(self.cfg2(seats, min_ok="n"))
        self.assertEqual(code, 3)
        summ = self.summary()
        c = self.by_id(summ)["c"]
        self.assertEqual((c["status"], c["salvaged"]), ("empty_output", False))
        self.assertEqual(c["salvage"]["status"], "exit_1")
        self.assertEqual(summ["quorum"]["ok"], 2)
        self.assertIsNone(summ["chair"])
        self.assertIn("SALVAGE tried: status=exit_1", self.seat_md("c"))
        self.assertIn("reasoning so far", self.seat_md("c"))        # the original reasoning partial is still there

    # ------------------------------------------------------------------ 3
    def test_4_a_cut_streaming_seat_is_salvaged_from_its_partial(self):
        seats = [seat("a"), seat("b"), seat("c"),
                 seat("d", "partial-then-hang", stub_salvage_mode="echo-stdin")]
        code = self.run_council(self.cfg2(seats, grace=1))             # n-1 = 3 of 4: quorum without d, d is cut
        self.assertEqual(code, 0)
        d = self.by_id(self.summary())["d"]
        self.assertEqual((d["status"], d["salvaged_from"]), ("ok", "cut_after_quorum"))
        md = self.seat_md("d")
        self.assertIn("SALVAGED from status=cut_after_quorum", md)
        self.assertIn("PARTIAL-123", md)                               # its own partial went into the prompt
        self.assertIn(INSTR, md)
        self.assertEqual(self.summary()["quorum"]["ok"], 4)

    def test_5_salvage_started_before_quorum_is_not_cut_by_the_grace(self):
        # c is reasoning-only and fails at once; a and b give quorum at once; c's salvage (3 s) outlives the 1 s grace
        seats = [seat("a"), seat("b"), rseat("c", salvage="slow-stream 3")]
        code = self.run_council(self.cfg2(seats, grace=1))
        self.assertEqual(code, 0)
        c = self.by_id(self.summary())["c"]
        self.assertEqual((c["status"], c["salvaged"]), ("ok", True))
        self.assertIn("DONE", self.seat_md("c"))
        self.assertFalse(c["salvage"]["truncated"])

    # ------------------------------------------------------------------ 4
    def test_6_no_double_salvage(self):
        # the salvage call is reasoning-only again: it fails, and nothing is launched a second time
        seats = [seat("a"), seat("b"), rseat("c", salvage="quick", salvage_reasons_again=True)]
        code = self.run_council(self.cfg2(seats, min_ok="n"))
        self.assertEqual(code, 3)
        c = self.by_id(self.summary())["c"]
        self.assertEqual((c["status"], c["salvaged"], c["salvage"]["status"]), ("empty_output", False, "empty_output"))
        self.assertEqual(len(pids_launched(self.pidfile)), 4)          # a, b, c, and exactly one salvage of c

    # ------------------------------------------------------------------ eligibility and config
    def test_7_disabled_or_absent_means_no_salvage(self):
        for salvage in (False, {"enabled": False}):
            os.environ["STUB_PIDFILE"] = self.pidfile = self.pidfile + "x"
            code = self.run_council(self.cfg2([seat("a"), seat("b"), rseat("c")], salvage=salvage, min_ok="n"))
            self.assertEqual(code, 3)
            c = self.by_id(self.summary())["c"]
            self.assertEqual(c["status"], "empty_output")
            self.assertNotIn("salvage", c)
            self.assertEqual(len(pids_launched(self.pidfile)), 3)

    def test_8_ok_seats_failed_clis_and_the_chair_are_not_salvaged(self):
        # a: ok. e: adapter_error with nothing kept (errstub exit 0). x: exit 1 with nothing kept.
        seats = [seat("a"), seat("e", "exit 0", provider="errstub"), seat("x", "exit 1")]
        code = self.run_council(self.cfg2(seats, min_ok=1))
        self.assertEqual(code, 0)
        by = self.by_id(self.summary())
        self.assertEqual((by["e"]["status"], by["x"]["status"]), ("adapter_error", "exit_1"))
        self.assertNotIn("salvage", by["a"])
        self.assertNotIn("salvage", by["e"])
        self.assertNotIn("salvage", by["x"])
        self.assertEqual(len(pids_launched(self.pidfile)), 4)          # three seats + the chair, no salvage

    def test_9_adapter_error_with_a_kept_partial_is_salvaged(self):
        seats = [seat("a"), seat("e", "quick", provider="pstub", stub_salvage_mode="echo-stdin")]
        code = self.run_council(self.cfg2(seats, min_ok="n"))
        self.assertEqual(code, 0)
        e = self.by_id(self.summary())["e"]
        self.assertEqual((e["status"], e["salvaged_from"]), ("ok", "adapter_error"))
        self.assertIn("QUICK-OK", self.seat_md("e"))                    # the partial the CLI kept went back in

    def test_10_salvage_call_has_idle_supervision_and_keeps_its_partial(self):
        seats = [seat("a"), seat("b"), rseat("c", salvage="partial-then-hang")]
        code = self.run_council(self.cfg2(seats, min_ok="n", idle=1.5))
        self.assertEqual(code, 3)
        c = self.by_id(self.summary())["c"]
        self.assertEqual(c["salvage"]["status"], "idle_kill")
        self.assertTrue(c["salvage"]["truncated"])
        with open(os.path.join(self.out, "seats", "c.salvage.raw")) as f:
            self.assertIn("PARTIAL-123", f.read())
        self.assertLess(self.wall, 15)

    def test_11_buffered_provider_salvage_uses_the_salvage_timeout_as_its_deadline(self):
        seats = [seat("a", provider="xstub"), seat("b", provider="xstub"), rseat("c", salvage="hung")]
        cfg = self.cfg2(seats, mode="buffered", min_ok="n",
                        extra={"salvage": {"enabled": True, "timeout": 2, "max_output_tokens": 8000}})
        code = self.run_council(cfg)
        self.assertEqual(code, 3)
        c = self.by_id(self.summary())["c"]
        self.assertIn(c["salvage"]["status"], ("deadline_kill", "ceiling_kill"))   # deadline == ceiling == the timeout
        self.assertEqual(c["salvage"]["mode"], "buffered")
        self.assertLess(self.wall, 15)

    def test_12_salvage_seat_is_what_the_adapter_returns(self):
        # the scheduler asks the ADAPTER for the salvage seat: short cap, reasoning cap dropped, role salvage
        s = providers.get("stub").salvage_seat({"id": "s", "provider": "stub", "model": "m", "reasoning_max_tokens": 9,
                                                "reasoning_form": "tokens"}, {}, 8000)
        self.assertEqual((s["max_output_tokens"], s["role"], s["_salvage"], s["reasoning_form"]),
                         (8000, "salvage", True, "effort"))
        self.assertNotIn("reasoning_max_tokens", s)
        self.assertFalse(providers.is_chair(s))
        cfg = {"salvage": {"enabled": True}}
        self.assertEqual(council_run.salvage_cfg(cfg), {"enabled": True, "max_output_tokens": 8000, "timeout": 600.0,
                                                        "max_partial_chars": 120000, "chair_max_output_tokens": 16000,
                                                        "chair_timeout": 1200.0})
        self.assertFalse(council_run.salvage_cfg({})["enabled"])


class ChairSalvageTests(SalvageBase):
    """Row 17: the chair gets the same one follow-up call."""

    def chair_cfg(self, seats=None, salvage=True, chair_extra=None, **kw):
        cfg = self.cfg2(seats or [seat("a"), seat("b")], salvage=salvage, **kw)
        cfg["chair"] = dict({"provider": "rstub", "model": "stub-model", "stub_mode": "echo-stdin",
                             "stub_salvage_mode": "echo-stdin"}, **(chair_extra or {}))
        return cfg

    def chair_rec(self):
        return self.summary()["chair"]

    def test_c1_reasoning_only_chair_is_salvaged_and_the_run_exits_0(self):
        code = self.run_council(self.chair_cfg(), question="What is 2+2?")
        self.assertEqual(code, 0)
        summ = self.summary()
        ch = summ["chair"]
        self.assertEqual((ch["status"], ch["detail"], ch["salvaged"], ch["salvaged_from"]),
                         ("ok", "salvaged", True, "empty_output"))
        self.assertEqual(ch["salvage"]["status"], "ok")
        self.assertEqual(summ["exit_code"], 0)
        self.assertEqual(summ["quorum"]["ok"], 2)               # a salvaged chair is not a seat
        syn = self.chair_md()
        self.assertIn("SALVAGED from status=empty_output", syn)
        self.assertIn("the chair's own synthesis never arrived", syn)
        # the salvage prompt: the original chair prompt with every seat answer, the chair's own reasoning, the instruction
        self.assertIn("CHAIR", syn)
        self.assertIn("What is 2+2?", syn)
        self.assertEqual(syn.count("QUICK-OK"), 4)              # 2 seat answers, each in the prompt and in the echoed reasoning
        self.assertIn("SALVAGE: your earlier attempt", syn)
        self.assertIn("without a usable final synthesis", syn)
        self.assertIn("Write the final synthesis now, concisely, without further deliberation.", syn)
        self.assertNotIn(INSTR, syn)
        self.assertNotIn(providers.THINKING_NOTE.strip(), syn.split("---", 1)[1].split("SALVAGE:", 1)[0])
        for f in ("chair.raw", "salvage.raw"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, "chair", f)), f)
        self.assertEqual(ch["salvage"]["paths"]["raw"], "chair/salvage.raw")
        self.assertEqual(len(pids_launched(self.pidfile)), 4)   # a, b, chair, one chair salvage
        with open(os.path.join(self.state, "latency.jsonl")) as f:
            kinds = [json.loads(ln)["kind"] for ln in f]
        self.assertEqual(sorted(kinds), ["chair", "salvage", "test", "test"])

    def test_c2_chair_salvage_uses_the_chair_cap_and_a_non_chair_seat_id(self):
        cfg = self.chair_cfg([seat("a"), seat("b"), rseat("c")], min_ok="n")
        cfg["salvage"]["chair_max_output_tokens"] = 12345
        self.assertEqual(self.run_council(cfg), 0)
        self.assertEqual(sorted(ReasonAdapter.caps), [("c", 8000), ("chair", 12345)])
        self.assertEqual(sorted(ReasonAdapter.built), [("c", "salvage"), ("chair-salvage", "salvage")])
        self.assertFalse(providers.is_chair({"id": "chair-salvage", "role": "salvage"}))
        self.assertEqual(council_run.salvage_cfg({"salvage": {"enabled": True}})["chair_max_output_tokens"], 16000)
        self.assertEqual(council_run.salvage_cfg({"salvage": {"enabled": True}})["chair_timeout"], 1200.0)
        self.assertEqual(council_run.salvage_problems({"salvage": {"chair_max_output_tokens": 5, "chair_timeout": 0}}),
                         ["salvage.chair_max_output_tokens 5 must be an integer >= 256",
                          "salvage.chair_timeout 0 must be a positive number of seconds"])

    def test_c3_failed_chair_salvage_is_exit_4_and_the_answers_are_kept(self):
        code = self.run_council(self.chair_cfg(chair_extra={"stub_salvage_mode": "exit 1"}))
        self.assertEqual(code, 4)
        summ = self.summary()
        self.assertEqual(summ["exit_code"], 4)
        ch = summ["chair"]
        self.assertEqual((ch["status"], ch["salvaged"]), ("empty_output", False))
        self.assertEqual(ch["salvage"]["status"], "exit_1")
        self.assertFalse(os.path.exists(os.path.join(self.out, "chair", "synthesis.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "chair", "chair.raw")))   # its reasoning is kept
        for sid in ("a", "b"):
            self.assertIn("QUICK-OK", self.seat_md(sid))
            self.assertEqual(self.by_id(summ)[sid]["status"], "ok")
        self.assertEqual(len(pids_launched(self.pidfile)), 4)

    def test_c4_no_double_chair_salvage(self):
        # the salvage call is reasoning-only again: it fails, and no third chair call is made
        code = self.run_council(self.chair_cfg(chair_extra={"stub_salvage_mode": "quick", "salvage_reasons_again": True}))
        self.assertEqual(code, 4)
        ch = self.chair_rec()
        self.assertEqual((ch["status"], ch["salvaged"], ch["salvage"]["status"]), ("empty_output", False, "empty_output"))
        self.assertEqual(len(pids_launched(self.pidfile)), 4)           # a, b, chair, exactly one salvage
        self.assertEqual(len(ReasonAdapter.caps), 1)

    def test_c5_salvage_disabled_means_a_failed_chair_is_exit_4_as_before(self):
        for salvage in (False, {"enabled": False}):
            os.environ["STUB_PIDFILE"] = self.pidfile = self.pidfile + "x"
            self.assertEqual(self.run_council(self.chair_cfg(salvage=salvage)), 4)
            self.assertNotIn("salvage", self.chair_rec())
            self.assertEqual(len(pids_launched(self.pidfile)), 3)
            self.assertFalse(os.path.exists(os.path.join(self.out, "chair", "chair.raw")))

    def test_c6_a_chair_that_answers_or_fails_with_nothing_kept_is_not_salvaged(self):
        self.assertEqual(self.run_council(self.cfg2([seat("a"), seat("b")])), 0)              # chair echo-stdin on stub: ok
        self.assertNotIn("salvage", self.chair_rec())
        self.assertEqual(len(pids_launched(self.pidfile)), 3)
        os.environ["STUB_PIDFILE"] = self.pidfile = self.pidfile + "x"
        cfg = self.cfg2([seat("a"), seat("b")])
        cfg["chair"] = {"provider": "stub", "model": "stub-model", "stub_mode": "exit 1"}
        self.assertEqual(self.run_council(cfg), 4)                                             # exit 1, nothing kept
        self.assertNotIn("salvage", self.chair_rec())
        self.assertEqual(len(pids_launched(self.pidfile)), 3)

    def test_c7_chair_cut_by_its_idle_kill_keeps_its_partial_and_is_salvaged(self):
        cfg = self.chair_cfg(chair_extra={"stub_mode": "partial-then-hang"}, idle=1.5)
        self.assertEqual(self.run_council(cfg), 0)
        ch = self.chair_rec()
        self.assertEqual((ch["status"], ch["salvaged_from"]), ("ok", "idle_kill"))
        self.assertIn("PARTIAL-123", self.chair_md())                   # the partial went into the salvage prompt
        self.assertLess(self.wall, 15)

    def test_c8_chair_salvage_timeout_is_its_own_ceiling_for_buffered_providers(self):
        cfg = self.chair_cfg(chair_extra={"stub_salvage_mode": "hung"}, mode="buffered",
                             salvage={"enabled": True, "timeout": 30, "chair_timeout": 2})
        cfg["seat"] = [seat("a", provider="xstub"), seat("b", provider="xstub")]
        cfg["providers"]["xstub"] = {"mode": "buffered"}
        self.assertEqual(self.run_council(cfg), 4)
        ch = self.chair_rec()
        self.assertIn(ch["salvage"]["status"], ("deadline_kill", "ceiling_kill"))
        self.assertEqual(ch["salvage"]["mode"], "buffered")
        self.assertLess(self.wall, 15)

class ReasoningNotResentTests(unittest.TestCase):
    """Row 7b: the claude salvage prompt (seat and chair) never carries the model's own reasoning back
    (Opus 5.5 refused that as `reasoning_extraction`, live row 7.1). Other providers are unchanged. No network."""
    REASON = providers.THINKING_NOTE + "Let x be the ball. 2x + 1.00 = 1.10, so REASON-MARK-9"
    ANSWER = "ANSWER-TEXT-MARK: the ball is about five cents and I was writing the working when"

    def task(self, provider, kind="seat", partial=None, status="empty_output"):
        t = council_run._Task("s1" if kind == "seat" else "chair", kind, {"id": "s1", "provider": provider},
                              providers.get(provider))
        t.prompt_text = "THE-ORIGINAL-PROMPT\n"
        t.status = status
        t.partial = self.REASON if partial is None else partial
        return t

    def test_r1_claude_seat_salvage_sends_no_reasoning(self):
        p = council_run._salvage_prompt(self.task("claude"), 120000)
        self.assertIn("THE-ORIGINAL-PROMPT", p)
        self.assertNotIn("REASON-MARK-9", p)
        self.assertNotIn(providers.THINKING_NOTE.strip(), p)
        self.assertNotIn("reasoning", p)
        self.assertNotIn("follows, between the fences", p)
        self.assertIn("ended (empty_output) without a usable final answer. Nothing from that attempt was kept.", p)
        self.assertTrue(p.endswith(council_run.SALVAGE_INSTRUCTION + "\n"))

    def test_r2_claude_visible_answer_text_partial_is_kept(self):
        p = council_run._salvage_prompt(self.task("claude", partial=self.ANSWER, status="idle_kill"), 120000)
        self.assertIn("ANSWER-TEXT-MARK", p)
        self.assertIn("Your own partial answer from that attempt follows", p)

    def test_r3_claude_chair_salvage_keeps_seat_answers_but_drops_own_reasoning(self):
        answers = "## seat a (codex)\nSEAT-A-ANSWER\n\n## seat b (agy)\nSEAT-B-ANSWER\n"
        t = self.task("claude", kind="chair")
        t.prompt_text = "CHAIR-PROMPT\n" + answers
        p = council_run._salvage_prompt(t, 120000)
        self.assertIn("SEAT-A-ANSWER", p)
        self.assertIn("SEAT-B-ANSWER", p)
        self.assertNotIn("REASON-MARK-9", p)
        self.assertNotIn(providers.THINKING_NOTE.strip(), p)
        self.assertIn("without a usable final synthesis. Nothing from that attempt was kept.", p)
        self.assertTrue(p.endswith(council_run.CHAIR_SALVAGE_INSTRUCTION + "\n"))

    def test_r4_other_providers_still_get_their_reasoning_back(self):
        for prov in ("codex", "agy", "qwen", "openrouter"):
            for kind in ("seat", "chair"):
                p = council_run._salvage_prompt(self.task(prov, kind=kind), 120000)
                self.assertIn("REASON-MARK-9", p, (prov, kind))
                self.assertIn("Your own reasoning from that attempt follows", p, (prov, kind))
                self.assertNotIn("Nothing from that attempt was kept", p, (prov, kind))

    def test_r5_only_claude_opts_out(self):
        off = sorted(n for n in ("claude", "codex", "agy", "qwen", "openrouter")
                     if not providers.get(n).salvage_resends_reasoning)
        self.assertEqual(off, ["claude"])


if __name__ == "__main__":
    unittest.main()
