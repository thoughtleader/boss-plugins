"""OpenRouter fallback chain (run example-run, row 1): stub processes only, no network, no paid call.

Covers: the seat child's pre-byte retry and its no-retry codes, the failure reason landing in `detail` / calls.jsonl / a kept
.stderr file, the fast-fail fallback (a seat that failed with nothing to salvage), the rerouted request, the stand-in call
and its `substituted` mark, and the preflight canary's retry chain.

Run: python3 -Es -m unittest tests.test_or_fallback   (cwd ~/.claude/council)
"""
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import council_run, openrouter_seat, preflight, providers  # noqa: E402
from tests.test_council_run import Base, seat  # noqa: E402
from tests.test_openrouter_seat import BASE, DONE, FakeResp, FakeStdin  # noqa: E402

KEY = "k-secret-do-not-leak"


# ---------------------------------------------------------------- the seat child
class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, headers=None):
        super().__init__("http://x/chat/completions", code, "msg", headers or {}, io.BytesIO(b"BODY-SECRET " + KEY.encode()))


def run_child(outcomes, argv=BASE):
    """outcomes: one per urlopen call (an exception to raise, or bytes for a 200 stream). -> (code, calls, stderr, sleeps)."""
    calls, sleeps, errbuf = [], [], io.StringIO()

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        o = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(o, BaseException):
            raise o
        return FakeResp(o)

    with mock.patch.object(openrouter_seat.urllib.request, "urlopen", fake_urlopen), \
            mock.patch.object(openrouter_seat.time, "sleep", sleeps.append), \
            mock.patch.object(openrouter_seat.sys, "stdin", FakeStdin(b"hello")), \
            mock.patch.object(openrouter_seat.sys, "stdout", mock.Mock(buffer=io.BytesIO())), \
            mock.patch.object(openrouter_seat.sys, "stderr", errbuf), \
            mock.patch.dict(os.environ, {"TEST_OR_KEY": KEY}):
        code = openrouter_seat.main(argv)
    return code, calls, errbuf.getvalue(), sleeps


class SeatChildRetryTests(unittest.TestCase):
    def test_01_retries_5xx_before_the_first_byte_then_succeeds(self):
        code, calls, err, sleeps = run_child([FakeHTTPError(503), FakeHTTPError(502), DONE])
        self.assertEqual((code, len(calls)), (0, 3))
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertIn("openrouter retry 1: http 503", err)
        self.assertIn("openrouter retry 2: http 502", err)

    def test_02_gives_up_after_two_retries_with_the_same_exit_code_and_line(self):
        code, calls, err, _ = run_child([FakeHTTPError(503)])
        self.assertEqual((code, len(calls)), (1, 3))
        self.assertEqual(err.strip().splitlines()[-1], "openrouter http 503")

    def test_03_retries_408_429_and_connect_errors(self):
        for first in (FakeHTTPError(408), FakeHTTPError(429), urllib.error.URLError("refused"), ConnectionResetError()):
            with self.subTest(first=type(first).__name__):
                code, calls, err, _ = run_child([first, DONE])
                self.assertEqual((code, len(calls)), (0, 2))

    def test_04_never_retries_400_401_402_403_404(self):
        for http in (400, 401, 402, 403, 404):
            with self.subTest(http=http):
                code, calls, err, sleeps = run_child([FakeHTTPError(http), DONE])
                self.assertEqual((code, len(calls), sleeps), (1, 1, []))
                self.assertEqual(err.strip(), "openrouter http %d" % http)

    def test_05_retry_after_is_honoured_and_capped(self):
        run = run_child([FakeHTTPError(429, {"Retry-After": "3"}), DONE])
        self.assertEqual(run[3], [3.0])
        run = run_child([FakeHTTPError(429, {"Retry-After": "900"}), DONE])
        self.assertEqual(run[3], [openrouter_seat.RETRY_AFTER_CAP])
        run = run_child([FakeHTTPError(503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), DONE])
        self.assertEqual(run[3], [1.0])                       # an HTTP-date is not an integer: the backoff table

    def test_06_bytes_already_streamed_are_never_retried(self):
        cut = b'data: {"id":"g","choices":[{"delta":{"content":"par"}}]}\n\n'   # 200, then the stream just ends
        code, calls, err, _ = run_child([cut, DONE])
        self.assertEqual((code, len(calls)), (3, 1))
        self.assertEqual(err.strip(), "openrouter stream ended early")

    def test_07_a_silent_connect_timeout_is_not_retried(self):
        import socket
        code, calls, err, _ = run_child([socket.timeout()])
        self.assertEqual((code, len(calls)), (1, 1))

    def test_08_neither_key_nor_body_reaches_stderr(self):
        _, _, err, _ = run_child([FakeHTTPError(500)])
        self.assertNotIn(KEY, err)
        self.assertNotIn("BODY-SECRET", err)


# ---------------------------------------------------------------- the extractor's view of that stderr
class ExtractorReasonTests(unittest.TestCase):
    def test_10_stderr_reason_and_retries(self):
        ex = providers.OpenRouterExtractor()
        ex.feed_stderr(b"openrouter retry 1: http 503\nopenrouter retry 2: URLError\nopenrouter http 503\n")
        self.assertEqual(ex.stderr_reason(), "openrouter http 503 (after 2 retries)")
        self.assertEqual(ex.retries(), ["http 503", "URLError"])
        self.assertEqual(ex.stderr_text(), "openrouter retry 1: http 503\nopenrouter retry 2: URLError\nopenrouter http 503\n")

    def test_11_nothing_but_openrouter_lines_survive(self):
        ex = providers.OpenRouterExtractor()
        ex.feed_stderr(("Traceback (most recent call last):\n  secret=%s\nopenrouter http 429\n" % KEY).encode())
        self.assertEqual(ex.stderr_reason(), "openrouter http 429")
        self.assertNotIn(KEY, ex.stderr_text())

    def test_12_an_ok_call_that_retried_says_so(self):
        ex = providers.OpenRouterExtractor()
        ex.feed_stderr(b"openrouter retry 1: http 502\n")
        self.assertEqual(ex.note(), "retried 1 time after http 502")


# ---------------------------------------------------------------- the stub adapter
SSE = ('data: {"id":"gen-1","model":"%s","choices":[{"delta":{"content":"%s"},"finish_reason":"stop"}]}\n\n'
       "data: [DONE]\n\n")
PARTIAL = 'data: {"id":"gen-1","model":"%s","choices":[{"delta":{"content":"half an answer"}}]}\n\n'


def program(kind, model, counter=None, fail_first=0):
    """Python source of one stub call. kinds: ok | http503 | partial503."""
    if kind == "ok":
        body = "sys.stdout.write(%r); sys.stdout.flush()" % (SSE % (model, "ANSWER-FROM-" + model))
    elif kind == "partial503":
        body = ("sys.stdout.write(%r); sys.stdout.flush(); sys.stderr.write('openrouter http 503\\n'); sys.exit(1)"
                % (PARTIAL % model))
    else:
        body = "sys.stderr.write('openrouter retry 1: http 503\\nopenrouter retry 2: http 503\\nopenrouter http 503\\n'); sys.exit(1)"
    if counter:
        # fail the first `fail_first` calls (counted in a file), then answer
        body = ("n = int(open(%r).read() or 0) if os.path.exists(%r) else 0\nopen(%r, 'w').write(str(n + 1))\n"
                "if n < %d:\n    sys.stderr.write('openrouter http 503\\n'); sys.exit(1)\n" % (counter, counter, counter, fail_first)
                + "sys.stdout.write(%r); sys.stdout.flush()" % (SSE % (model, "ANSWER-FROM-" + model)))
    return "import os, sys\n" + body


class FStub(providers.StubAdapter):
    """A stream stub that reads like OpenRouter: SSE stdout, `openrouter ...` stderr, the fast-fail chain."""
    name = "fstub"
    fast_fail_fallback = True
    launches = []

    def fallback_chain(self, seat, cfg):
        pc = providers._pcfg(cfg, "fstub")
        chain = [providers.reroute_seat(seat, pc)]
        st = providers.standin_seat(seat, pc)
        if st is not None:
            chain.append(st)
        return chain

    def standin_start(self, seat, cfg):
        return providers.standin_start_seat(seat, providers._pcfg(cfg, "fstub"))

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        step = "standin" if seat.get("_standin") else "fallback" if seat.get("_reroute") else "original"
        kind = seat.get("stub_" + step, "ok") if not seat.get("_salvage") else seat.get("stub_salvage", "ok")
        prog = program(kind, seat["model"], seat.get("stub_counter") if step == "original" else None,
                       seat.get("stub_fail_first", 0))
        with open(prompt_path, "rb") as f:
            prompt = f.read().decode()
        FStub.launches.append({"step": step, "model": seat["model"], "cap": seat.get("max_output_tokens"),
                               "prefs": providers.routing_prefs(cfg, seat), "prompt": prompt, "limits": limits,
                               "effort": seat.get("reasoning_effort"), "full": bool(seat.get("_full_call"))})
        launch = super().build(dict(seat, stub_mode=["-c", "pass"]), prompt_path, workdir, limits, cfg, launch_opts)
        launch.argv = [sys.executable, "-Es", "-c", prog]
        return launch

    def extractor(self, seat=None):
        return providers.OpenRouterExtractor()


providers.FAMILIES.setdefault("fstub", "stub")


def fseat(sid, **kw):
    d = {"id": sid, "provider": "fstub", "model": "primary/model", "max_output_tokens": 182476,
         "provider_prefs": {"order": ["onlyhost"]}, "reasoning_effort": "high", "reasoning_form": "effort"}
    d.update(kw)
    return d


class FastFailBase(Base):
    def setUp(self):
        super().setUp()
        FStub.launches = []
        providers.register(FStub())
        self.addCleanup(providers._REGISTRY.pop, "fstub", None)

    def fcfg(self, seats, fallback=True, salvage=False, **kw):
        cfg = self.cfg(seats, mode="stream", **kw)
        cfg["providers"]["fstub"] = {"mode": "stream", "fallback_max_output_tokens": 131072}
        cfg["providers"]["openrouter"] = {"provider": {"require_parameters": True}}     # routing_prefs reads this table
        cfg["fallback"] = {"enabled": fallback}
        cfg["salvage"] = {"enabled": salvage, "max_output_tokens": 8000, "timeout": 10, "max_partial_chars": 1000}
        return cfg

    def read(self, *parts):
        with open(os.path.join(self.out, *parts)) as f:
            return f.read()

    def seat_json(self, sid):
        return json.loads(self.read("seats", sid + ".json"))

    def calls(self):
        with open(os.path.join(self.out, "calls.jsonl")) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def steps(self):
        return [(ln["step"], ln["model"]) for ln in FStub.launches if ln["model"] != "stub-model"]


class FastFailTests(FastFailBase):
    def test_20_reason_lands_in_detail_calls_jsonl_and_a_kept_stderr_file(self):
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503", stub_fallback="http503")]
        self.run_council(self.fcfg(seats, fallback=False))
        f = self.seat_json("f")
        self.assertEqual((f["status"], f["detail"]), ("exit_1", "openrouter http 503 (after 2 retries)"))
        rec = [c for c in self.calls() if c["call"] == "original" and c["seat"] == "f"][-1]
        self.assertEqual(rec["detail"], "openrouter http 503 (after 2 retries)")
        kept = self.read("seats", "f.original.stderr")
        self.assertIn("openrouter http 503", kept)
        self.assertNotIn(KEY, kept)

    def test_21_fast_fail_fires_the_full_prompt_rerouted_and_counts_toward_quorum(self):
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503", stub_fallback="ok")]
        code = self.run_council(self.fcfg(seats, min_ok="n"), question="Why is the sky blue?")
        self.assertEqual(code, 0)                                    # 3 of 3 needed: the fallback answered
        f = self.seat_json("f")
        self.assertEqual((f["status"], f["detail"], f["answered_by"]), ("ok", "fallback", "fallback"))
        self.assertEqual(f["recovered"]["from_status"], "exit_1")
        self.assertNotIn("substituted", f)
        self.assertFalse(f.get("salvaged", False))
        self.assertEqual(self.summary()["quorum"]["ok"], 3)
        orig, fb = [ln for ln in FStub.launches if ln["model"] == "primary/model"]
        self.assertEqual(fb["prompt"], orig["prompt"])               # the original prompt, not the salvage form
        self.assertNotIn("SALVAGE", fb["prompt"])
        self.assertNotIn("_salvage", [k for k in fb])
        self.assertTrue(fb["full"])
        self.assertEqual(fb["effort"], "high")                       # full effort, unchanged
        self.assertIn("RETRIED from status exit_1", self.read("chair", "synthesis.md"))
        self.assertIn("RETRIED", self.read("seats", "f.md"))
        self.assertEqual([c["role"] for c in f["calls"]], ["failed", "used"])

    def test_22_rerouted_request_drops_the_order_pin_and_caps_max_tokens(self):
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503", stub_fallback="ok")]
        self.run_council(self.fcfg(seats))
        orig, fb = [ln for ln in FStub.launches if ln["model"] == "primary/model"]
        self.assertEqual((orig["cap"], orig["prefs"]), (182476, {"require_parameters": True, "order": ["onlyhost"]}))
        self.assertEqual((fb["cap"], fb["prefs"]), (131072, {"require_parameters": True}))
        self.assertEqual(fb["limits"].mode, "stream")                # the seat's own limits, not the 600 s salvage ones
        self.assertEqual(fb["limits"].ceiling_s, 120)

    def test_23_a_per_seat_cap_wins_and_never_exceeds_the_seat_cap(self):
        pc = {"fallback_max_output_tokens": 131072}
        self.assertEqual(providers.fallback_max_tokens({"fallback_max_output_tokens": 65536, "max_output_tokens": 182476}, pc), 65536)
        self.assertEqual(providers.fallback_max_tokens({"max_output_tokens": 100000}, pc), 100000)
        self.assertEqual(providers.fallback_max_tokens({}, {}), 131072)

    def test_24_stand_in_fires_when_the_reroute_also_fails_and_is_marked_substituted(self):
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503", stub_fallback="http503", stub_standin="ok",
                                             fallback_model="stand/in-model", fallback_reasoning_effort="medium")]
        code = self.run_council(self.fcfg(seats, min_ok="n"))
        self.assertEqual(code, 0)
        self.assertEqual(self.steps(), [("original", "primary/model"), ("fallback", "primary/model"),
                                        ("standin", "stand/in-model")])
        f = self.seat_json("f")
        self.assertEqual((f["status"], f["answered_by"], f["detail"]), ("ok", "standin", "standin"))
        self.assertEqual(f["substituted"]["model"], "stand/in-model")
        self.assertIn("exit_1", f["substituted"]["reason"])
        self.assertIn("openrouter http 503", f["substituted"]["reason"])
        si = FStub.launches[-1]
        self.assertEqual((si["cap"], si["effort"], si["prefs"]), (131072, "medium", {"require_parameters": True}))
        self.assertEqual(si["prompt"], FStub.launches[0]["prompt"])
        self.assertEqual([c["role"] for c in f["calls"]], ["failed", "failed", "used"])
        chair = self.read("chair", "synthesis.md")
        self.assertIn("STAND-IN", chair)
        self.assertIn("f seat answered by stand-in stand/in-model", chair)
        self.assertIn("ANSWER-FROM-stand/in-model", chair)
        self.assertIn("model stand/in-model", chair)                 # the heading names the model that really answered
        self.assertIn("STAND-IN", self.read("seats", "f.md"))
        self.assertEqual(self.summary()["quorum"]["ok"], 3)
        starts = [(c["call"], c.get("status")) for c in self.calls() if c["seat"] == "f"]
        self.assertIn(("standin", "ok"), starts)

    def test_25_no_stand_in_without_the_key_and_every_step_failing_is_unresolved(self):
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503", stub_fallback="http503")]
        code = self.run_council(self.fcfg(seats))
        self.assertEqual(code, 0)                                    # n-1 quorum: a and b
        self.assertEqual(self.steps(), [("original", "primary/model"), ("fallback", "primary/model")])
        f = self.seat_json("f")
        self.assertEqual(f["status"], "unresolved")
        self.assertEqual(f["detail"], "original exit_1; fallback exit_1")
        self.assertNotIn("substituted", f)

    def test_26_all_three_failing_records_each_step_and_no_fourth_call(self):
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503", stub_fallback="http503", stub_standin="http503",
                                             fallback_model="stand/in-model")]
        self.run_council(self.fcfg(seats))
        self.assertEqual(len(self.steps()), 3)
        f = self.seat_json("f")
        self.assertEqual((f["status"], f["detail"]), ("unresolved", "original exit_1; fallback exit_1; standin exit_1"))

    def test_27_fallback_flag_off_means_no_extra_call(self):
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503", fallback_model="stand/in-model")]
        self.run_council(self.fcfg(seats, fallback=False))
        self.assertEqual(self.steps(), [("original", "primary/model")])
        self.assertEqual(self.seat_json("f")["status"], "exit_1")

    def test_28_a_failure_with_a_partial_keeps_the_salvage_path(self):
        seats = [seat("a"), seat("b"), fseat("f", stub_original="partial503", fallback_model="stand/in-model")]
        self.run_council(self.fcfg(seats, salvage=True))
        # the original kept a partial: the salvage-form call ran (and answered) and the chain never started
        self.assertEqual([ln["step"] for ln in FStub.launches if ln["model"] != "stub-model"], ["original", "original"])
        f = self.seat_json("f")
        self.assertEqual((f["status"], f["detail"], f["salvaged"]), ("ok", "salvaged", True))
        self.assertNotIn("recovered", f)

    def test_29_providers_without_the_flag_are_unchanged(self):
        seats = [seat("a"), seat("b"), seat("c", "exit 1")]
        self.run_council(self.fcfg(seats))
        self.assertEqual(self.seat_json("c")["status"], "exit_1")
        self.assertNotIn("calls", self.seat_json("c"))
        self.assertEqual(self.steps(), [])

    def test_30_a_hung_original_that_gets_idle_killed_is_not_a_fast_failure(self):
        # idle_kill is a SALVAGE status: salvage runs when on; with it off the chain still must not double-fire
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503")]
        cfg = self.fcfg(seats)
        self.run_council(cfg)
        self.assertEqual(self.steps().count(("original", "primary/model")), 1)

    def test_31_the_chair_is_never_chained(self):
        self.assertFalse(getattr(providers.get("stub"), "fast_fail_fallback", False))
        self.assertEqual(providers.get("stub").fallback_chain({"id": "x"}, {}), [])


class AdapterChainTests(unittest.TestCase):
    def setUp(self):
        from councillib import config
        self.cfg = config.load()
        self.adapter = providers.get("openrouter")
        self.seats = {s["id"]: s for s in self.cfg["seat"] if s.get("provider") == "openrouter"}

    def test_40_shipped_roster_has_deepseek_and_glm_with_stand_ins_and_no_pin(self):
        self.assertEqual(sorted(self.seats), ["deepseek", "openrouter"])
        ds, glm = self.seats["deepseek"], self.seats["openrouter"]
        self.assertEqual((ds["model"], ds["max_output_tokens"], ds["fallback_model"]),
                         ("deepseek/deepseek-v4-pro-0813", 131072, "qwen/qwen3.8-max-0902"))
        self.assertEqual((glm["model"], glm["max_output_tokens"], glm["fallback_model"]),
                         ("z-ai/glm-5.3", 131072, "moonshotai/kimi-k3.1-pro"))
        for s in (ds, glm):
            self.assertNotIn("provider_prefs", s)
            self.assertEqual(providers.routing_prefs(self.cfg, s), {"require_parameters": True})

    def test_41_chain_steps_for_the_shipped_seats(self):
        for sid, standin in (("deepseek", "qwen/qwen3.8-max-0902"), ("openrouter", "moonshotai/kimi-k3.1-pro")):
            with self.subTest(seat=sid):
                chain = self.adapter.fallback_chain(self.seats[sid], self.cfg)
                self.assertEqual([c["model"] for c in chain], [self.seats[sid]["model"], standin])
                self.assertTrue(all(c["_full_call"] and c["_reroute"] for c in chain))
                self.assertTrue(chain[1]["_standin"])
                for c in chain:
                    self.assertEqual(c["max_output_tokens"], 131072)
                    self.assertNotIn("order", providers.routing_prefs(self.cfg, c) or {})
                # the stand-in sends an effort its own model supports; the seat's model-specific keys do not carry over
                for k in ("salvage_reasoning", "canary_reasoning_effort", "ignores_reasoning_cap", "provider_prefs"):
                    self.assertNotIn(k, chain[1])

    def test_42_rerouted_argv_has_no_order_and_the_capped_max_tokens(self):
        from councillib.supervise import Limits
        lim = Limits(mode="stream", idle_s=240, ceiling_s=1200.0, deadline_s=None)
        chain = self.adapter.fallback_chain(dict(self.seats["deepseek"], max_output_tokens=182476), self.cfg)
        a = self.adapter.build(chain[0], "/p", "/w", lim, self.cfg).argv
        self.assertEqual(a[a.index("--max-tokens") + 1], "131072")
        self.assertEqual(json.loads(a[a.index("--provider-json") + 1]), {"require_parameters": True})
        self.assertEqual(a[a.index("--model") + 1], "deepseek/deepseek-v4-pro-0813")
        self.assertNotIn("--reasoning-off", a)                       # full effort, never the salvage form
        s = self.adapter.build(chain[1], "/p", "/w", lim, self.cfg).argv
        self.assertEqual(s[s.index("--model") + 1], "qwen/qwen3.8-max-0902")

    def test_42b_p26_ordering_preferences_reach_provider_json_and_the_request_unchanged(self):
        """P26: a seat's `sort` / `preferred_min_throughput` block goes into `--provider-json` and, through the seat child,
        into the request's `provider` field, as written (merged over the base table). The rerouted step keeps it (only
        `order` is dropped); the stand-in carries none of the seat's provider_prefs."""
        from councillib.supervise import Limits
        lim = Limits(mode="stream", idle_s=240, ceiling_s=1200.0, deadline_s=None)
        own = {"sort": "throughput", "preferred_min_throughput": 80, "preferred_max_latency": 4.5}
        s = dict(self.seats["deepseek"], provider_prefs=dict(own))
        self.assertEqual(providers.routing_problems(dict(self.cfg, seat=[s])), [])
        want = dict({"require_parameters": True}, **own)
        argvs = {"seat": self.adapter.build(s, "/p", "/w", lim, self.cfg).argv,
                 "salvage": self.adapter.build(dict(s, _salvage=True), "/p", "/w", lim, self.cfg).argv}
        chain = self.adapter.fallback_chain(s, self.cfg)
        argvs["rerouted"] = self.adapter.build(chain[0], "/p", "/w", lim, self.cfg).argv
        for kind, a in argvs.items():
            with self.subTest(kind=kind):
                self.assertEqual(json.loads(a[a.index("--provider-json") + 1]), want)
        st = self.adapter.build(chain[1], "/p", "/w", lim, self.cfg).argv
        self.assertEqual(json.loads(st[st.index("--provider-json") + 1]), {"require_parameters": True})
        a = argvs["seat"]
        code, calls, _, _ = run_child([DONE], BASE + ["--provider-json", a[a.index("--provider-json") + 1]])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(calls[0].data.decode())["provider"], want)

    def test_43_a_seat_without_fallback_model_has_only_the_rerouted_step(self):
        s = dict(self.seats["deepseek"])
        s.pop("fallback_model")
        self.assertEqual(len(self.adapter.fallback_chain(s, self.cfg)), 1)

    def test_44_free_checks_cover_the_stand_in_without_a_paid_call(self):
        models = {"data": [
            {"id": "deepseek/deepseek-v4-pro-0813", "supported_parameters": ["reasoning", "max_tokens"],
             "reasoning": {"supported_efforts": ["high", "low", "none"]}},
            {"id": "qwen/qwen3.8-max-0902", "supported_parameters": ["reasoning", "max_tokens"],
             "top_provider": {"max_completion_tokens": 131072},
             "reasoning": {"supported_efforts": ["high", "medium", "low"]}}]}
        eps = {"data": {"endpoints": [
            {"provider_name": p, "max_completion_tokens": 393216, "supported_parameters": ["reasoning", "max_tokens"]}
            for p in ("A", "B")]}}

        def fake_get(path, cfg, *a, **k):
            if path == "/models":
                return 200, models
            if path.endswith("/endpoints"):
                return 200, eps
            return 200, {"data": {}}
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}), \
                mock.patch.object(providers, "openrouter_get", fake_get):
            rows = {c.check: c for c in self.adapter.free_checks([self.seats["deepseek"]], self.cfg)}
        self.assertEqual(rows["fallback_model:deepseek"].result, "PASS")
        self.assertEqual(rows["reroute:deepseek"].result, "PASS")
        models["data"][1]["reasoning"]["supported_efforts"] = ["low"]
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}), \
                mock.patch.object(providers, "openrouter_get", fake_get):
            rows = {c.check: c for c in self.adapter.free_checks([self.seats["deepseek"]], self.cfg)}
        self.assertEqual(rows["fallback_model:deepseek"].result, "WARN")
        models["data"].pop(1)
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}), \
                mock.patch.object(providers, "openrouter_get", fake_get):
            rows = {c.check: c for c in self.adapter.free_checks([self.seats["deepseek"]], self.cfg)}
        self.assertEqual(rows["fallback_model:deepseek"].result, "FAIL")


# ---------------------------------------------------------------- the preflight canary
class CanaryRetryTests(FastFailBase):
    def canary(self, **seat_kw):
        cfg = self.fcfg([fseat("f", **seat_kw)])
        cfg["chair"] = {"provider": "fstub", "model": "primary/model"}
        cfg["canary"] = {"prompt": "Reply with exactly: OK", "timeout_s": 20, "idle_s": 10}
        rows = [r for r in preflight.canary_stage(cfg) if r.stage == "canary"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_50_a_passing_canary_is_one_call_and_a_pass(self):
        row = self.canary()
        self.assertEqual(row.result, "PASS")
        self.assertEqual(len(FStub.launches), 1)

    def test_51_one_blip_then_a_pass_is_a_warn_naming_the_first_error(self):
        counter = os.path.join(self.tmp, "ctr")
        row = self.canary(stub_counter=counter, stub_fail_first=1)
        self.assertEqual(row.result, "WARN")
        self.assertIn("passed on the retry attempt", row.detail)
        self.assertIn("exit_1", row.detail)
        self.assertIn("openrouter http 503", row.detail)
        self.assertEqual([ln["step"] for ln in FStub.launches], ["original", "original"])

    def test_52_second_failure_goes_to_the_rerouted_request(self):
        counter = os.path.join(self.tmp, "ctr")
        row = self.canary(stub_counter=counter, stub_fail_first=2)
        self.assertEqual(row.result, "WARN")
        self.assertIn("rerouted", row.detail)
        steps = [(ln["step"], ln["cap"], ln["prefs"]) for ln in FStub.launches]
        self.assertEqual(steps[2], ("fallback", 131072, {"require_parameters": True}))

    def test_53_every_attempt_failing_is_a_fail_listing_them_and_the_stand_in_is_never_called(self):
        row = self.canary(stub_original="http503", stub_fallback="http503", fallback_model="stand/in-model")
        self.assertEqual(row.result, "FAIL")
        self.assertIn("first call:", row.detail)
        self.assertIn("retry:", row.detail)
        self.assertIn("rerouted:", row.detail)
        self.assertNotIn("stand/in-model", [ln["model"] for ln in FStub.launches])


class PlainStub(FStub):
    """A provider with no fast-fail chain (like agy or codex): its canary gets one plain retry."""
    name = "fstub"
    fast_fail_fallback = False

    def fallback_chain(self, seat, cfg):
        return []


class PlainCanaryRetryTests(CanaryRetryTests):
    def setUp(self):
        super().setUp()
        providers.register(PlainStub())

    def test_54_a_plain_provider_blip_is_retried_once_and_warns(self):
        counter = os.path.join(self.tmp, "ctr")
        row = self.canary(stub_counter=counter, stub_fail_first=1)
        self.assertEqual(row.result, "WARN")
        self.assertIn("passed on the retry attempt", row.detail)
        self.assertEqual(len(FStub.launches), 2)

    def test_55_a_plain_provider_failing_twice_fails_with_no_reroute(self):
        row = self.canary(stub_original="http503")
        self.assertEqual(row.result, "FAIL")
        self.assertIn("retry:", row.detail)
        self.assertNotIn("rerouted:", row.detail)
        self.assertEqual(len(FStub.launches), 2)

    def test_56_retry_failed_false_keeps_one_call(self):
        cfg = self.fcfg([fseat("f", stub_original="http503")])
        cfg["chair"] = {"provider": "fstub", "model": "primary/model"}
        cfg["canary"] = {"prompt": "Reply with exactly: OK", "timeout_s": 20, "idle_s": 10, "retry_failed": False}
        rows = [r for r in preflight.canary_stage(cfg) if r.stage == "canary"]
        self.assertEqual(rows[0].result, "FAIL")
        self.assertEqual(len(FStub.launches), 1)

    # the inherited fast-fail cases do not apply to a provider with no chain
    test_52_second_failure_goes_to_the_rerouted_request = None
    test_53_every_attempt_failing_is_a_fail_listing_them_and_the_stand_in_is_never_called = None


# ---------------------------------------------------------------- review gap 1: a seat that fails PREFLIGHT runs as its stand-in
class StandinStartTests(FastFailBase):
    def standin_seats(self, **kw):
        return [seat("a"), seat("b"), fseat("f", fallback_model="stand/in-model", **kw)]

    def test_60_a_seat_starts_on_its_stand_in_and_is_marked_substituted(self):
        reason = "preflight:primary/model not in /models"
        code = self.run_council(self.fcfg(self.standin_seats(), min_ok="n"), standins={"f": reason})
        self.assertEqual(code, 0)
        self.assertEqual(self.steps(), [("original", "stand/in-model")])         # the dead model is never called
        f = self.seat_json("f")
        self.assertEqual((f["status"], f["answered_by"] if "answered_by" in f else "original"), ("ok", "original"))
        self.assertEqual(f["substituted"], {"model": "stand/in-model", "reason": reason})
        self.assertEqual(self.summary()["quorum"]["ok"], 3)
        launch = FStub.launches[-1]
        self.assertEqual((launch["cap"], launch["prefs"], launch["effort"]),
                         (131072, {"require_parameters": True}, "high"))           # unpinned, capped, the seat's effort
        chair = self.read("chair", "synthesis.md")
        self.assertIn("STAND-IN", chair)
        self.assertIn("f seat answered by stand-in stand/in-model", chair)
        self.assertIn("own model primary/model", chair)                           # the chair is told what it replaced
        self.assertIn("preflight:primary/model not in /models", chair)
        md = self.read("seats", "f.md")
        self.assertTrue(md.startswith("> STAND-IN"), md[:80])
        self.assertIn("primary/model", md)

    def test_61_a_seat_without_a_stand_in_is_launched_as_configured(self):
        seats = [seat("a"), seat("b"), fseat("f")]
        self.run_council(self.fcfg(seats), standins={"f": "preflight:x"})
        self.assertEqual(self.steps(), [("original", "primary/model")])
        self.assertNotIn("substituted", self.seat_json("f"))

    def test_62_the_stand_in_failing_is_bounded_one_reroute_then_unresolved(self):
        seats = self.standin_seats(stub_original="http503", stub_fallback="http503")
        self.run_council(self.fcfg(seats), standins={"f": "preflight:x"})
        self.assertEqual(self.steps(), [("original", "stand/in-model"), ("fallback", "stand/in-model")])
        f = self.seat_json("f")
        self.assertEqual(f["status"], "unresolved")

    def test_63_the_cli_gate_hands_the_marks_to_the_runner(self):
        checks = [providers.Check("openrouter", "free", "models:f", "WARN", "x",
                                  standin={"seats": ["f"], "reason": "preflight:m not in /models"}),
                  providers.Check("openrouter", "canary", "canary:y", "WARN", "y",
                                  standin={"seats": ["g", "h"], "reason": "preflight:exit_1"}),
                  providers.Check("openrouter", "free", "models:z", "PASS", "z")]
        self.assertEqual(preflight.standins(checks), {"f": "preflight:m not in /models", "g": "preflight:exit_1",
                                                      "h": "preflight:exit_1"})
        self.assertEqual(preflight.standins([]), {})
        self.assertIn('"standin"', preflight.to_json(checks))


class PreflightStandinTests(unittest.TestCase):
    """OpenRouterAdapter.free_checks: a missing model with a good stand-in is a WARN that marks the seat."""

    def setUp(self):
        from councillib import config
        self.cfg = config.load()
        self.adapter = providers.get("openrouter")
        self.seat = {s["id"]: s for s in self.cfg["seat"]}["deepseek"]
        self.models = {"data": [
            {"id": "qwen/qwen3.8-max-0902", "supported_parameters": ["reasoning", "max_tokens"],
             "top_provider": {"max_completion_tokens": 131072},
             "reasoning": {"supported_efforts": ["high", "medium", "low"]}}]}

    def rows(self):
        def fake_get(path, cfg, *a, **k):
            return (200, self.models) if path == "/models" else (200, {"data": {}})
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}), mock.patch.object(providers, "openrouter_get", fake_get):
            return {c.check: c for c in self.adapter.free_checks([self.seat], self.cfg)}

    def test_70_missing_model_with_a_passing_stand_in_is_a_warn_not_a_fail(self):
        rows = self.rows()
        row = rows["models:deepseek"]
        self.assertEqual(row.result, "WARN")
        self.assertIn("runs as its stand-in qwen/qwen3.8-max-0902", row.detail)
        self.assertEqual(row.standin, {"seats": ["deepseek"],
                                       "reason": "preflight:deepseek/deepseek-v4-pro-0813 not in /models"})
        self.assertEqual(rows["fallback_model:deepseek"].result, "PASS")
        self.assertNotIn("reroute:deepseek", rows)                     # the dead model has nothing to reroute
        self.assertEqual([c.check for c in rows.values() if c.result == "FAIL"], [])

    def test_71_the_stand_in_missing_too_still_fails(self):
        self.models["data"].pop(0)
        rows = self.rows()
        self.assertEqual(rows["models:deepseek"].result, "FAIL")
        self.assertIsNone(rows["models:deepseek"].standin)

    def test_72_no_fallback_model_still_fails(self):
        self.seat = {k: v for k, v in self.seat.items() if k != "fallback_model"}
        self.assertEqual(self.rows()["models:deepseek"].result, "FAIL")

    def test_73_a_models_listing_error_is_still_a_fail(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}), \
                mock.patch.object(providers, "openrouter_get", lambda path, cfg, *a, **k: (500, {})):
            rows = {c.check: c for c in self.adapter.free_checks([self.seat], self.cfg)}
        self.assertEqual(rows["models:deepseek"].result, "FAIL")


class CanaryStandinTests(FastFailBase):
    def canary(self, seats, stand=None):
        cfg = self.fcfg(seats)
        cfg["chair"] = {"provider": "fstub", "model": "chair/model"}
        cfg["canary"] = {"prompt": "Reply with exactly: OK", "timeout_s": 20, "idle_s": 10}
        return [r for r in preflight.canary_stage(cfg, stand=stand) if r.stage == "canary"]

    def test_80_a_dead_model_whose_stand_in_passes_is_a_warn_that_marks_its_seats(self):
        seats = [fseat("f", stub_original="http503", stub_fallback="http503", stub_standin="ok",
                       fallback_model="stand/in-model")]
        rows = {r.check: r for r in self.canary(seats)}
        row = rows["canary:primary/model"]
        self.assertEqual(row.result, "WARN")
        self.assertEqual(row.standin["seats"], ["f"])
        self.assertTrue(row.standin["reason"].startswith("preflight:"), row.standin)
        self.assertIn("openrouter http 503", row.standin["reason"])
        self.assertEqual([x for x in self.steps() if x[1] != "chair/model"],
                         [("original", "primary/model"), ("original", "primary/model"),
                          ("fallback", "primary/model"), ("standin", "stand/in-model")])
        self.assertEqual(preflight.standins(list(rows.values())), {"f": row.standin["reason"]})
        self.assertEqual(rows["canary:chair/model"].result, "PASS")

    def test_81_stand_in_failing_too_is_a_fail(self):
        seats = [fseat("f", stub_original="http503", stub_fallback="http503", stub_standin="http503",
                       fallback_model="stand/in-model")]
        row = {r.check: r for r in self.canary(seats)}["canary:primary/model"]
        self.assertEqual(row.result, "FAIL")
        self.assertIn("standin:", row.detail)
        self.assertEqual(len([x for x in self.steps() if x[1] != "chair/model"]), 4)   # bounded: retry, reroute, one stand-in call

    def test_82_a_model_the_chair_also_uses_has_no_stand_in_step(self):
        seats = [fseat("f", model="chair/model", stub_original="http503", stub_fallback="http503",
                       fallback_model="stand/in-model")]
        row = {r.check: r for r in self.canary(seats)}["canary:chair/model"]
        self.assertEqual(row.result, "FAIL")
        self.assertNotIn("stand/in-model", [m for _, m in self.steps()])

    def test_83_a_seat_in_the_standin_map_is_canaried_as_its_stand_in(self):
        seats = [fseat("f", fallback_model="stand/in-model")]
        rows = self.canary(seats, stand={"f": "preflight:primary/model not in /models"})
        self.assertEqual(sorted(r.check for r in rows), ["canary:chair/model", "canary:stand/in-model"])
        self.assertEqual(sorted(m for _, m in self.steps()), ["chair/model", "stand/in-model"])
        self.assertEqual({r.result for r in rows}, {"PASS"})


# ---------------------------------------------------------------- review Important: a chain call that left a partial is salvaged once
class ChainSalvageTests(FastFailBase):
    def chain_seats(self, **kw):
        kw.setdefault("fallback_model", "stand/in-model")
        return [seat("a"), seat("b"), fseat("f", stub_original="http503", **kw)]

    def launches(self):
        return [(ln["step"], ln["model"], "SALVAGE:" in ln["prompt"]) for ln in FStub.launches if ln["model"] != "stub-model"]

    def test_90_a_rerouted_call_with_a_partial_is_salvaged_once_and_counts(self):
        seats = self.chain_seats(stub_fallback="partial503")
        code = self.run_council(self.fcfg(seats, salvage=True, min_ok="n"))
        self.assertEqual(code, 0)
        self.assertEqual([(m, salv) for _, m, salv in self.launches()],
                         [("primary/model", False), ("primary/model", False), ("primary/model", True)])
        f = self.seat_json("f")
        self.assertEqual((f["status"], f["answered_by"]), ("ok", "fallback"))
        self.assertTrue(f["recovered"]["salvaged"])
        self.assertNotIn("standin", [c["call"] for c in f["calls"]])
        self.assertIn("short low-effort follow-up", self.read("chair", "synthesis.md"))

    def test_91_a_failed_salvage_goes_on_to_the_stand_in_and_nothing_loops(self):
        seats = self.chain_seats(stub_fallback="partial503", stub_salvage="http503", stub_standin="ok")
        self.run_council(self.fcfg(seats, salvage=True, min_ok="n"))
        got = self.launches()
        self.assertEqual([m for _, m, _ in got], ["primary/model", "primary/model", "primary/model", "stand/in-model"])
        self.assertEqual(sum(1 for *_, salv in got if salv), 1)
        f = self.seat_json("f")
        self.assertEqual((f["status"], f["answered_by"]), ("ok", "standin"))
        self.assertEqual(f["substituted"]["model"], "stand/in-model")

    def test_92_a_stand_in_with_a_partial_is_salvaged_once_then_the_seat_ends(self):
        seats = self.chain_seats(stub_fallback="http503", stub_standin="partial503", stub_salvage="http503")
        self.run_council(self.fcfg(seats, salvage=True))
        got = self.launches()
        self.assertEqual(len(got), 4)
        self.assertEqual(sum(1 for *_, salv in got if salv), 1)
        self.assertEqual(self.seat_json("f")["status"], "unresolved")

    def test_93_no_partial_means_no_chain_salvage(self):
        seats = self.chain_seats(stub_fallback="http503", stub_standin="http503")
        self.run_council(self.fcfg(seats, salvage=True))
        self.assertEqual([salv for *_, salv in self.launches()], [False, False, False])

    def test_94_salvage_off_means_no_chain_salvage(self):
        seats = self.chain_seats(stub_fallback="partial503", stub_standin="ok")
        self.run_council(self.fcfg(seats, salvage=False))
        self.assertEqual([salv for *_, salv in self.launches()], [False, False, False])


if __name__ == "__main__":
    unittest.main()
