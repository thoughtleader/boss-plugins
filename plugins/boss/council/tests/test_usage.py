"""Usage capture (row 3): every extractor's `usage()`, the seat/salvage/chair records, summary.json
totals, the canary rows and the /council-run table. Fixtures and stub adapters only: no network, no
paid call, no real settings.

Contract: {"input_tokens", "output_tokens", "reasoning_tokens", "cost_usd"}; a field the provider did
not report is None, never 0 and never an estimate. Reasoning tokens are already INSIDE output tokens for
claude, agy and OpenRouter, so nothing here adds them again.

Run: python3 -Es -m unittest discover -s ~/.claude/council/tests -t ~/.claude/council
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import council_run, preflight, providers  # noqa: E402
from tests import test_preflight  # noqa: E402
from tests.test_council_run import Base, seat  # noqa: E402
from tests.test_providers import feed_all, fixture  # noqa: E402

NONE4 = {"input_tokens": None, "output_tokens": None, "reasoning_tokens": None, "cost_usd": None}


def u(i=None, o=None, r=None, c=None):
    return {"input_tokens": i, "output_tokens": o, "reasoning_tokens": r, "cost_usd": c}


class ExtractorUsage(unittest.TestCase):
    def test_01_claude_result_event(self):
        ex = feed_all(providers.ClaudeExtractor(), fixture("claude-usage.ndjson"))
        # total_cost_usd (both models billed), not one modelUsage entry; the assistant event's usage is a decoy
        self.assertEqual(ex.usage(), u(2, 35545, 32234, 0.7209044))
        self.assertGreaterEqual(ex.usage()["output_tokens"], ex.usage()["reasoning_tokens"])   # inside, not added
        self.assertNotEqual(ex.usage()["cost_usd"], 0.7206104)

    def test_02_claude_without_a_result_or_without_usage_is_unknown_not_zero(self):
        lines = fixture("claude-usage.ndjson").splitlines(keepends=True)
        self.assertEqual(feed_all(providers.ClaudeExtractor(), b"".join(lines[:3])).usage(), NONE4)   # killed run
        # the older fixtures' result events carry no usage and no cost
        self.assertEqual(feed_all(providers.ClaudeExtractor(), fixture("claude.ndjson")).usage(), NONE4)
        self.assertEqual(providers.ClaudeExtractor().usage(), NONE4)

    def test_03_codex_turn_completed(self):
        ex = feed_all(providers.CodexExtractor(), fixture("codex.jsonl"))
        self.assertEqual(ex.usage(), u(23026, 11999, 11988, None))     # subscription: no cost
        self.assertEqual(feed_all(providers.CodexExtractor(), fixture("codex-failed.jsonl")).usage(), NONE4)

    def test_04_agy_result_usage_sums_every_step(self):
        self.assertEqual(feed_all(providers.AgyExtractor(), fixture("agy.ndjson")).usage(), u(7395, 97, 96, None))
        ex = feed_all(providers.AgyExtractor(), fixture("agy-step-limit-answer.ndjson"))
        self.assertEqual(ex.note(), "step_limit")
        self.assertEqual(ex.usage(), u(89059, 66271, 63343, None))     # the result's sum, not the last step's
        lines = fixture("agy-step-limit-answer.ndjson").splitlines(keepends=True)
        self.assertEqual(feed_all(providers.AgyExtractor(), b"".join(lines[:-1])).usage(), NONE4)   # no result yet

    def test_05_qwen_has_no_reasoning_split_and_no_cost(self):
        self.assertEqual(feed_all(providers.QwenExtractor(), fixture("qwen.ndjson")).usage(), u(13503, 26, None, None))
        self.assertEqual(providers.QwenExtractor().usage(), NONE4)                  # no result event
        # an error result that reports 0/0 is a reported zero, kept as such
        self.assertEqual(feed_all(providers.QwenExtractor(), fixture("qwen-error.ndjson")).usage(), u(0, 0, None, None))

    def test_06_openrouter_usage_chunk_with_cost_and_reasoning(self):
        ex = feed_all(providers.OpenRouterExtractor(), fixture("openrouter-usage.sse"))
        self.assertEqual(ex.usage(), u(450, 55024, 38208, 0.10591608))
        self.assertEqual(ex.final_text(), "Four.")

    def test_07_openrouter_partial_fields_and_cut_stream(self):
        # older fixture: usage without cost or reasoning details -> those stay None
        self.assertEqual(feed_all(providers.OpenRouterExtractor(), fixture("openrouter.sse")).usage(),
                         u(17, 88, None, None))
        # a stream cut before its usage chunk
        data = b"".join(l for l in fixture("openrouter-usage.sse").splitlines(keepends=True) if b"prompt_tokens" not in l)
        self.assertNotIn(b"prompt_tokens", data)
        self.assertEqual(feed_all(providers.OpenRouterExtractor(), data).usage(), NONE4)

    def test_08_raw_text_and_garbage_are_unknown(self):
        self.assertEqual(providers.RawTextExtractor("m").usage(), NONE4)
        self.assertEqual(providers.usage_dict(True, -1, "7", float("nan")), NONE4)
        self.assertEqual(providers.usage_dict(0, 0, 0, 0.0), u(0, 0, 0, 0.0))   # a reported zero stays zero
        ex = providers.OpenRouterExtractor()
        ex.feed(b'data: {"model":"m","choices":[],"usage":{"prompt_tokens":"x","completion_tokens":5}}\n')
        self.assertEqual(ex.usage(), u(None, 5, None, None))


class UExtractor(providers.RawTextExtractor):
    """Stub extractor: usage comes from the seat dict (`stub_usage`, or `stub_salvage_usage` on the
    salvage call); `stub_reason_only` seats exit 0 with no answer so the scheduler salvages them."""

    def __init__(self, seat):
        super().__init__(seat.get("model"))
        self._seat = seat

    def final_text(self):
        if self._seat.get("stub_reason_only") and not self._seat.get("_salvage"):
            return None
        return super().final_text()

    def usage(self):
        v = self._seat.get("stub_salvage_usage" if self._seat.get("_salvage") else "stub_usage")
        return providers.usage_dict(**v) if v else super().usage()


class UAdapter(providers.StubAdapter):
    name = "ustub"

    def extractor(self, seat=None):
        return UExtractor(seat or {})


class SchedulerUsage(Base):
    def setUp(self):
        super().setUp()
        providers.FAMILIES.setdefault("ustub", "stub")
        providers.register(UAdapter())
        self.addCleanup(providers._REGISTRY.pop, "ustub", None)

    def ucfg(self, seats, chair=None, salvage=False):
        cfg = self.cfg(seats, min_ok="n")
        cfg["providers"]["ustub"] = {"mode": "stream"}
        cfg["chair"] = dict({"provider": "ustub", "model": "stub-model", "stub_mode": "echo-stdin"}, **(chair or {}))
        if salvage:
            cfg["salvage"] = {"enabled": True, "max_output_tokens": 8000, "timeout": 30, "max_partial_chars": 120000}
        return cfg

    def test_10_seat_and_chair_records_carry_usage_and_summary_totals(self):
        seats = [seat("a", "quick", "ustub", stub_usage={"input_tokens": 10, "output_tokens": 50,
                                                        "reasoning_tokens": 30, "cost_usd": 0.25}),
                 seat("b", "quick", "ustub", stub_usage={"input_tokens": 7, "output_tokens": 9}),   # no cost reported
                 seat("c", "quick", "ustub")]                                                      # no usage at all
        chair = {"stub_usage": {"input_tokens": 100, "output_tokens": 20, "reasoning_tokens": 0, "cost_usd": 0.5}}
        self.assertEqual(self.run_council(self.ucfg(seats, chair)), 0)
        summ = self.summary()
        by = self.by_id(summ)
        self.assertEqual(by["a"]["usage"], u(10, 50, 30, 0.25))
        self.assertEqual(by["b"]["usage"], u(7, 9, None, None))
        self.assertEqual(by["c"]["usage"], NONE4)
        self.assertEqual(summ["chair"]["usage"], u(100, 20, 0, 0.5))
        self.assertEqual(summ["cost_reported_usd"], 0.75)
        self.assertEqual(summ["cost_unreported"], ["b", "c"])
        # the per-seat file carries the same record
        self.assertEqual(json.loads(self.read("seats", "a.json"))["usage"], u(10, 50, 30, 0.25))

    def test_11_nothing_reported_means_no_total_not_zero(self):
        self.assertEqual(self.run_council(self.ucfg([seat("a", "quick", "ustub"), seat("b", "quick", "ustub")])), 0)
        summ = self.summary()
        self.assertIsNone(summ["cost_reported_usd"])
        self.assertEqual(summ["cost_unreported"], ["a", "b", "chair"])

    def test_12_salvage_call_has_its_own_usage_and_cost_counts_in_the_total(self):
        seats = [seat("a", "quick", "ustub", stub_usage={"output_tokens": 5, "cost_usd": 0.1}),
                 seat("r", "echo-stdin", "ustub", stub_reason_only=True, stub_salvage_mode="echo-stdin",
                      stub_usage={"input_tokens": 1000, "output_tokens": 8000, "reasoning_tokens": 7990, "cost_usd": 0.2},
                      stub_salvage_usage={"input_tokens": 1100, "output_tokens": 40, "reasoning_tokens": 0,
                                          "cost_usd": 0.01})]
        self.assertEqual(self.run_council(self.ucfg(seats, salvage=True)), 0)
        summ = self.summary()
        r = self.by_id(summ)["r"]
        self.assertTrue(r["salvaged"])
        self.assertEqual(r["usage"], u(1000, 8000, 7990, 0.2))            # the seat's own spend stays on the seat
        self.assertEqual(r["salvage"]["usage"], u(1100, 40, 0, 0.01))     # the follow-up call is separate
        self.assertEqual(summ["cost_reported_usd"], 0.31)
        self.assertEqual(summ["cost_unreported"], ["chair"])
        self.assertEqual(json.loads(self.read("seats", "r.json"))["salvage"]["usage"], u(1100, 40, 0, 0.01))

    def test_13_chair_salvage_usage(self):
        chair = {"stub_reason_only": True, "stub_salvage_mode": "echo-stdin",
                 "stub_usage": {"output_tokens": 16000, "reasoning_tokens": 15990, "cost_usd": 0.4},
                 "stub_salvage_usage": {"output_tokens": 300, "reasoning_tokens": 0, "cost_usd": 0.02}}
        seats = [seat("a", "quick", "ustub", stub_usage={"cost_usd": 0.1}), seat("b", "quick", "ustub", stub_usage={"cost_usd": 0.1})]
        self.assertEqual(self.run_council(self.ucfg(seats, chair, salvage=True)), 0)
        summ = self.summary()
        ch = summ["chair"]
        self.assertTrue(ch["salvaged"])
        self.assertEqual(ch["usage"], u(None, 16000, 15990, 0.4))
        self.assertEqual(ch["salvage"]["usage"], u(None, 300, 0, 0.02))
        self.assertEqual(summ["cost_reported_usd"], 0.62)
        self.assertEqual(summ["cost_unreported"], [])

    def test_14_failed_and_killed_calls_are_unknown_and_listed(self):
        seats = [seat("a", "quick", "ustub", stub_usage={"cost_usd": 0.1}),
                 seat("b", "quick", "ustub", stub_usage={"cost_usd": 0.1}),
                 seat("x", "exit 3", "ustub")]                          # ran, failed, reported nothing
        cfg = self.ucfg(seats)
        cfg["quorum"]["min_ok"] = "n-1"
        self.assertEqual(self.run_council(cfg), 0)
        summ = self.summary()
        self.assertEqual(self.by_id(summ)["x"]["usage"], NONE4)
        self.assertEqual(summ["cost_reported_usd"], 0.2)
        self.assertEqual(summ["cost_unreported"], ["x", "chair"])

    def test_15_early_exit_summary_has_the_total_fields(self):
        cfg = self.ucfg([seat("a", "quick", "ustub")])
        os.remove(os.path.join(self.prompts, "advisor.md"))
        self.assertEqual(self.run_council(cfg), 2)
        summ = self.summary()
        self.assertIsNone(summ["cost_reported_usd"])
        self.assertEqual(summ["cost_unreported"], [])


class CanaryUsage(test_preflight.Base):
    def setUp(self):
        super().setUp()
        providers.FAMILIES["ustub"] = "stub"
        providers.register(UAdapter())
        self.addCleanup(providers._REGISTRY.pop, "ustub", None)
        self.addCleanup(providers.FAMILIES.pop, "ustub", None)

    def cfg(self):
        seats = [("a", "ustub", ['stub_usage = { input_tokens = 12, output_tokens = 34, reasoning_tokens = 5, '
                                 'cost_usd = 0.0123 }']),
                 ("b", "ustub", [])]
        return test_preflight.cfg_of(self.tmp, seats, extra='[providers.ustub]\nmode = "stream"\n')

    def test_20_canary_rows_and_json_carry_usage_with_unknown_for_missing(self):
        ok, checks = preflight.run(self.cfg())
        self.assertTrue(ok, preflight.format_table(checks))
        cans = {c.check: c for c in self.rows(checks, "canary")}
        a = cans["canary:m-a"]
        self.assertEqual(a.usage, u(12, 34, 5, 0.0123))
        self.assertIn("tokens in/out/reasoning 12/34/5, cost $0.012300", a.detail)
        b = cans["canary:m-b"]
        self.assertEqual(b.usage, NONE4)
        self.assertIn("tokens in/out/reasoning unknown/unknown/unknown, cost unknown", b.detail)
        rows = {r["check"]: r for r in json.loads(preflight.to_json(checks))["checks"]}
        self.assertEqual(rows["canary:m-a"]["usage"], u(12, 34, 5, 0.0123))
        self.assertNotIn("usage", [r for r in rows.values() if r["stage"] == "free"][0])


class TableCommand(unittest.TestCase):
    """The jq in skills/council/reference.md (the hand re-read of a run) prints out_tok, reasoning_tok and cost_usd."""

    CMD = os.path.join(os.path.dirname(ROOT), "skills", "council", "reference.md")

    def script(self):
        with open(self.CMD) as f:
            text = f.read()
        m = re.search(r"```\n(CAPPED=.*?)```", text, re.S)
        self.assertIsNotNone(m, "table script block not found")
        return m.group(1)

    def test_30_table_shows_usage_columns_and_unknown_dashes(self):
        if not all(shutil.which(x) for x in ("jq", "column", "bash")):
            self.skipTest("jq/column not installed")
        import tempfile
        run = tempfile.mkdtemp(prefix="usage-tbl-")
        self.addCleanup(shutil.rmtree, run, True)
        os.makedirs(os.path.join(run, "seats"))
        rec = lambda i, us, **kw: dict({"id": i, "provider": "p", "model": "m", "status": "ok", "total_s": 1.234,  # noqa: E731
                                        "truncated": False, "usage": us}, **kw)
        summ = {"seats": [rec("a", u(1, 50, 30, 0.25)), rec("b", NONE4),
                          rec("r", u(1, 8000, 7990, 0.2), salvaged=True,
                              salvage={"status": "ok", "model": "m", "total_s": 2.0, "usage": u(5, 40, 0, 0.01)}),
                          {"id": "n", "provider": "p", "model": "m", "status": "not_launched"}],
                "chair": rec("chair", u(100, 20, 0, 0.5)), "quorum": {}, "cost_reported_usd": 0.96,
                "cost_unreported": ["b"]}
        with open(os.path.join(run, "summary.json"), "w") as f:
            json.dump(summ, f)
        p = subprocess.run(["bash", "-c", self.script()], env=dict(os.environ, RUN=run), capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.splitlines()
        head = lines[0].split()
        self.assertEqual(head[head.index("out_tok"):head.index("out_tok") + 3], ["out_tok", "reasoning_tok", "cost_usd"])
        rows = {(l.split()[0], l.split()[1]): l.split() for l in lines[1:] if l.strip() and not l.startswith("{")}
        i = head.index("out_tok")
        self.assertEqual(rows[("advisor", "a")][i:i + 3], ["50", "30", "0.25"])
        self.assertEqual(rows[("advisor", "b")][i:i + 3], ["-", "-", "-"])        # unknown, not 0
        self.assertEqual(rows[("advisor", "n")][i:i + 3], ["-", "-", "-"])
        self.assertEqual(rows[("salvage", "r")][i:i + 3], ["40", "0", "0.01"])
        self.assertEqual(rows[("chair", "chair")][i:i + 3], ["20", "0", "0.5"])
        self.assertIn("cost_reported_usd", p.stdout)


if __name__ == "__main__":
    unittest.main()
