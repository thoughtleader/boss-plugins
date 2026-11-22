import copy
import glob
import json
import os
import shutil
import tempfile
import unittest

from councillib import config, providers, timing
from tests import needs_proc  # noqa: E402


def seat(i, provider, model):
    return {"id": i, "provider": provider, "model": model}


def cfg_with(seats, cap=1):
    return {"seat": seats, "chair": {"provider": "claude", "model": "claude-opus-5-5"},
            "policy": {"max_anthropic_advisors": cap}, "quorum": {"min_ok": "n-1"}}


class RosterPolicy(unittest.TestCase):
    def test_1_two_claude_advisors(self):
        errs = config.validate_roster(cfg_with([seat("a", "claude", "x"), seat("b", "claude", "y")]))
        self.assertTrue(any("Anthropic" in e for e in errs), errs)

    def test_2_claude_plus_openrouter_anthropic(self):
        errs = config.validate_roster(cfg_with(
            [seat("a", "claude", "x"), seat("b", "openrouter", "anthropic/claude-x")]))
        self.assertTrue(any("Anthropic" in e for e in errs), errs)

    def test_3_claude_plus_agy_claude_label(self):
        errs = config.validate_roster(cfg_with(
            [seat("a", "claude", "x"), seat("b", "agy", "Claude Opus 4.6 (Thinking)")]))
        self.assertTrue(any("Anthropic" in e for e in errs), errs)

    def test_4_cap_value_and_chair_not_counted(self):
        errs = config.validate_roster(cfg_with([seat("a", "codex", "m")], cap=2))
        self.assertTrue(any("max_anthropic_advisors" in e for e in errs), errs)
        self.assertEqual(config.validate_roster(cfg_with([seat("a", "claude", "x")])), [])
        self.assertEqual(config.validate_roster(cfg_with([seat("a", "claude", "x")], cap=1)), [])
        # cap 0 rejects even one Claude advisor
        self.assertTrue(config.validate_roster(cfg_with([seat("a", "claude", "x")], cap=0)))
        # unknown provider and duplicate id are reported too
        errs = config.validate_roster(cfg_with([seat("a", "nope", "m"), seat("a", "codex", "m")]))
        self.assertTrue(any("unknown provider" in e for e in errs), errs)
        self.assertTrue(any("duplicate" in e for e in errs), errs)

    def test_5_overlay_replaces_seats_and_merges_quorum(self):
        base = config.load()
        self.assertGreater(len(base["seat"]), 1)
        cfg = config.load([{"seat": [seat("only", "stub", "m")], "quorum": {"min_ok": "all"}}])
        self.assertEqual([s["id"] for s in cfg["seat"]], ["only"])
        self.assertEqual(cfg["quorum"]["min_ok"], "all")
        self.assertEqual(cfg["quorum"]["straggler_grace_s"], base["quorum"]["straggler_grace_s"])
        self.assertEqual(config.resolve_min_ok("n-1", 5), 4)
        self.assertEqual(config.resolve_min_ok("all", 3), 3)
        self.assertEqual(config.resolve_min_ok(2, 5), 2)
        self.assertFalse(os.path.expanduser("~") not in cfg["state_dir"])


class ShippedLimits(unittest.TestCase):
    """Row 14 (owner: a council must COMPLETE): the shipped timeouts never end a healthy seat."""

    def setUp(self):
        self.cfg = config.load()
        self.log = os.path.join(tempfile.mkdtemp(prefix="lim-"), "latency.jsonl")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.log), True)

    def limits(self, seat_provider, kind="seat"):
        from councillib import council_run
        model = next(s["model"] for s in self.cfg["seat"] if s["provider"] == seat_provider)
        return council_run._limits(self.cfg, {"provider": seat_provider, "model": model}, kind, self.log, 1000)

    def test_1_stream_seats_are_idle_only_with_a_ceiling_of_at_least_an_hour(self):
        for prov in ("claude", "openrouter"):               # qwen is off the roster (row 16)
            lim, mode, deadline, source = self.limits(prov)
            self.assertEqual((mode, deadline, source), ("stream", None, "stream_idle"), prov)
            self.assertEqual(lim.idle_s, 240.0)
            self.assertGreaterEqual(lim.ceiling_s, 3600, prov)
        lim, *_ = self.limits("claude", "chair")
        self.assertGreaterEqual(lim.ceiling_s, 3600)

    def test_2_buffered_seats_have_no_ceiling_when_hedged_else_a_generous_one_and_a_600s_floor(self):
        for prov in ("codex", "agy"):
            lim, mode, deadline, source = self.limits(prov)
            if prov == "codex":                                      # row 34: hedged, so no hard deadline, a soft hedge time
                self.assertEqual((mode, deadline), ("watch", None))
                self.assertGreaterEqual(lim.soft_at_s, 900)
                self.assertIsNone(lim.ceiling_s)                     # row 35: and no ceiling
            else:                                                    # agy is not hedged (D46): the pre-hedge deadline
                self.assertEqual(mode, "buffered")
                self.assertGreaterEqual(deadline, 600, prov)         # cold deadline (base_timeout_s)
                self.assertGreaterEqual(lim.ceiling_s, 2400, prov)
        self.assertEqual(self.cfg["timeouts"]["default"]["min_timeout_s"], 600)

    def test_3_max_ceiling_mirrors_the_limits_resolution(self):
        # a [timeouts.<p>] ceiling replaces the default (it is what council_run._limits uses), and the chair
        # table applies to the chair's provider only
        self.assertIsNone(providers._max_ceiling(self.cfg, "codex"))              # row 35: no ceiling (not 7200)
        self.assertEqual(providers._max_ceiling(self.cfg, "agy"), 3600)
        self.assertEqual(providers._max_ceiling(self.cfg, "qwen"), 7200)
        self.assertEqual(providers._max_ceiling(self.cfg, "claude"), 7200)          # claude is the chair
        cfg = copy.deepcopy(self.cfg)
        cfg["timeouts"]["chair"]["ceiling_s"] = 9000
        self.assertEqual(providers._max_ceiling(cfg, "claude"), 9000)
        self.assertEqual(providers._max_ceiling(cfg, "qwen"), 7200)
        # a codex chair keeps the chair table's ceiling; no ceiling on either side wins (None, never max(None, x))
        cfg["chair"] = {"provider": "codex", "model": "m"}
        self.assertIsNone(providers._max_ceiling(cfg, "codex"))
        self.assertEqual(timing.ceiling_of(cfg, "codex", chair=True), 9000.0)

    def test_4_every_seat_keeps_the_answer_reserve(self):
        res = self.cfg["answer_reserve_tokens"]
        for name in ("claude", "qwen", "openrouter"):
            pc = self.cfg["providers"][name]
            for key in ("max_output_tokens", "chair_max_output_tokens"):
                self.assertGreaterEqual(pc[key] - 0, res, (name, key))   # effort form: reasoning cap 0
            self.assertEqual(pc["reasoning_form"], "effort")
            self.assertTrue(pc["ignores_reasoning_cap"])
            self.assertEqual(providers._budget_problems(name, pc, providers.OR_EFFORTS + providers.CLAUDE_EFFORTS,
                                                        True, self.cfg, True, self.cfg["seat"]), [], name)
        self.assertEqual(self.cfg["providers"]["claude"]["thinking_display"], "summarized")
        self.assertEqual(self.cfg["quorum"]["min_ok"], "n-1")                    # unchanged: 4 of 5, no chair when missed


class Timing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-cfg-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "state", "latency.jsonl")
        self.cfg = config.load()

    def test_6b_cold_buffered_seat_starts_at_the_ceiling_warm_one_stays_adaptive(self):
        # row 35: only agy still has a buffered deadline; codex has no ceiling and no deadline (test_6e, CeilingOf)
        for prov in ("agy",):
            ceiling = self.cfg["timeouts"][prov]["ceiling_s"]
            self.assertEqual(ceiling, 3600)
            self.assertIs(self.cfg["timeouts"][prov]["cold_at_ceiling"], True)
            # no log, and fewer than min_samples (5) ok lines: both are cold -> ceiling
            self.assertEqual(timing.adaptive_deadline(self.log, prov, "m", 1000, self.cfg), (ceiling, "cold"))
            for t in (100, 110, 120, 130):
                timing.append(self.log, {"kind": "seat", "provider": prov, "model": "m",
                                         "outcome": "ok", "total_s": t, "prompt_bytes": 1000})
            self.assertEqual(timing.adaptive_deadline(self.log, prov, "m", 1000, self.cfg), (ceiling, "cold"))
            # the fifth sample makes it warm (source "p95"): p95 (140) x 1.5 = 210, but the provider floor
            # min_timeout_s (owner ruling 2026-10-21) is the full ceiling, so the deadline stays 3600
            timing.append(self.log, {"kind": "seat", "provider": prov, "model": "m",
                                     "outcome": "ok", "total_s": 140, "prompt_bytes": 1000})
            dl, src = timing.adaptive_deadline(self.log, prov, "m", 1000, self.cfg)
            self.assertEqual((dl, src), (ceiling, "p95"))
            for t in (1000, 1100, 1200):
                timing.append(self.log, {"kind": "seat", "provider": prov, "model": "m",
                                         "outcome": "ok", "total_s": t, "prompt_bytes": 1000})
            dl, src = timing.adaptive_deadline(self.log, prov, "m", 1000, self.cfg)
            self.assertEqual((dl, src), (ceiling, "p95"))      # 1200 x 1.5 = 1800 is still raised to the floor

    def test_6e_warm_buffered_codex_and_agy_seats_never_get_a_short_deadline(self):
        # owner ruling 2026-10-21: short-sample history (quick answers, small prompts) must never set a lower deadline.
        # agy keeps the full 3600 s ceiling as its deadline; codex (row 35) has no ceiling and no deadline at all.
        self.assertEqual(self.cfg["timeouts"]["codex"], {"idle_s": 240, "ceiling_s": 0})   # floor/cold keys retired
        for prov in ("codex", "agy"):
            t = self.cfg["timeouts"][prov]
            if prov == "agy":
                self.assertEqual((t["min_timeout_s"], t["ceiling_s"]), (3600, 3600))
            for _ in range(8):
                timing.append(self.log, {"kind": "seat", "provider": prov, "model": "m",
                                         "outcome": "ok", "total_s": 5, "prompt_bytes": 500})
            if prov == "agy":
                self.assertEqual(timing.adaptive_deadline(self.log, prov, "m", 500, self.cfg), (3600, "p95"))
                self.assertEqual(timing.adaptive_deadline(self.log, prov, "m", 50, self.cfg), (3600, "p95"))
            from councillib import council_run
            model = next(s["model"] for s in self.cfg["seat"] if s["provider"] == prov)
            timing.append(self.log, {"kind": "seat", "provider": prov, "model": model,
                                     "outcome": "ok", "total_s": 5, "prompt_bytes": 500})
            if prov == "agy":
                lim, mode, deadline, source = council_run._limits(self.cfg, {"provider": prov, "model": model},
                                                                  "seat", self.log, 500)
                self.assertEqual((mode, deadline, source, lim.deadline_s), ("buffered", 3600.0, "p95", 3600.0), prov)
                continue
            # codex, hedge on (rows 34/35): no deadline and no ceiling, a soft hedge time
            lim, mode, deadline, source = council_run._limits(self.cfg, {"provider": prov, "model": model},
                                                              "seat", self.log, 500)
            self.assertEqual((mode, deadline, source), ("watch", None, "hedge_p95"))
            self.assertEqual((lim.deadline_s, lim.ceiling_s), (None, None))
            # hedge switched off by an overlay but the ceiling left at 0: preflight FAILs it (owner ruling 1), and even
            # then the seat gets no wall-clock deadline (watch, no soft notice), never the 600 s default floor
            off = config.deep_merge(self.cfg, {"hedge": {"enabled": False}})
            self.assertTrue(any("needs the hedge on for codex" in p for p in providers.hedge_problems(off)))
            lim, mode, deadline, source = council_run._limits(off, {"provider": prov, "model": model},
                                                              "seat", self.log, 500)
            self.assertEqual((mode, deadline, source, lim.deadline_s, lim.ceiling_s, lim.soft_at_s),
                             ("watch", None, "no_ceiling", None, None, None))
        # only codex and agy: other providers keep the default floor, stream seats and salvage are untouched
        for prov in ("claude", "qwen", "openrouter"):
            self.assertNotIn("min_timeout_s", self.cfg["timeouts"].get(prov, {}))
        self.assertEqual(self.cfg["timeouts"]["default"]["min_timeout_s"], 600)
        self.assertEqual(self.cfg["timeouts"]["default"]["ceiling_s"], 7200)
        self.assertEqual(self.cfg["salvage"]["timeout"], 600)

    def test_6c_cold_at_ceiling_is_opt_in_and_tracks_the_providers_ceiling(self):
        cfg = copy.deepcopy(self.cfg)                     # agy: the buffered seat that keeps its ceiling (row 35)
        cfg["timeouts"]["agy"]["ceiling_s"] = 4000
        self.assertEqual(timing.adaptive_deadline(self.log, "agy", "m", 1000, cfg), (4000, "cold"))
        cfg["timeouts"]["agy"]["cold_at_ceiling"] = False
        self.assertEqual(timing.adaptive_deadline(self.log, "agy", "m", 1000, cfg), (900, "cold"))
        # no ceiling (row 35): cold_at_ceiling has no ceiling to start at -> no deadline (None), never 0 s
        cfg["timeouts"]["agy"].update(ceiling_s=0, cold_at_ceiling=True)
        self.assertEqual(timing.adaptive_deadline(self.log, "agy", "m", 1000, cfg), (None, "cold"))
        # providers without the flag keep their base_timeout_s
        self.assertEqual(timing.adaptive_deadline(self.log, "nosuch", "m", 1000, self.cfg), (600, "cold"))

    def test_6d_limits_gives_a_cold_buffered_seat_the_ceiling_deadline(self):
        from councillib import council_run
        for prov in ("codex", "agy"):
            model = next(s["model"] for s in self.cfg["seat"] if s["provider"] == prov)
            lim, mode, deadline, source = council_run._limits(self.cfg, {"provider": prov, "model": model},
                                                              "seat", self.log, 1000)
            if prov == "agy":                                  # agy keeps its ceiling (D46): cold deadline = ceiling
                self.assertEqual((mode, deadline, source), ("buffered", 3600.0, "cold"), prov)
                self.assertEqual(lim.deadline_s, lim.ceiling_s)
            else:                                              # codex (row 35): hedged, no deadline, no ceiling
                self.assertEqual((mode, deadline, source, lim.ceiling_s), ("watch", None, "hedge_cold", None))
            # salvage call deadline unchanged
            _, _, sdl, ssrc = council_run._limits(self.cfg, {"provider": prov, "model": model},
                                                  "salvage", self.log, 1000)
            self.assertEqual((sdl, ssrc), (float(self.cfg["salvage"]["timeout"]), "salvage_timeout"))

    def test_6_adaptive_deadline(self):
        # agy: the buffered seat that still has a ceiling and the floor keys (codex has none since row 35)
        # cold: no log
        self.assertEqual(timing.adaptive_deadline(self.log, "agy", "m", 1000, self.cfg), (3600, "cold"))  # [timeouts.agy] cold_at_ceiling (owner ruling 2026-10-21)
        for t in (100, 110, 120, 130, 140, 150, 160, 170, 180, 200):
            timing.append(self.log, {"kind": "seat", "provider": "agy", "model": "m",
                                     "outcome": "ok", "total_s": t, "prompt_bytes": 1000})
        # canary, chair, test, failed and other-provider lines must not count
        for kind in ("canary", "chair", "test"):
            timing.append(self.log, {"kind": kind, "provider": "agy", "model": "m",
                                     "outcome": "ok", "total_s": 900, "prompt_bytes": 1000})
        timing.append(self.log, {"kind": "seat", "provider": "agy", "model": "m",
                                 "outcome": "exit_1", "total_s": 900})
        timing.append(self.log, {"kind": "seat", "provider": "qwen", "model": "m",
                                 "outcome": "ok", "total_s": 900})
        # row 14 (R4): the shipped floor is 600 s, so a history of quick answers cannot kill a hard prompt
        self.assertEqual(self.cfg["timeouts"]["default"]["min_timeout_s"], 600)
        dl, src = timing.adaptive_deadline(self.log, "agy", "m", 1000, self.cfg)
        self.assertEqual((src, dl), ("p95", 3600))                # p95 200 x 1.5 = 300, lifted to the agy floor (3600)
        low = copy.deepcopy(self.cfg)
        low["timeouts"]["default"]["min_timeout_s"] = 100         # floor out of the way for the arithmetic below
        low["timeouts"]["agy"]["min_timeout_s"] = 100
        # p95 of 10 samples (nearest rank) = 200; size 1; safety 1.5 -> 300
        dl, src = timing.adaptive_deadline(self.log, "agy", "m", 1000, low)
        self.assertEqual(src, "p95")
        self.assertAlmostEqual(dl, 300, delta=1)
        # 4x the median prompt size: factor sqrt(4)=2 -> 600
        dl, _ = timing.adaptive_deadline(self.log, "agy", "m", 4000, low)
        self.assertAlmostEqual(dl, 600, delta=1)
        # clamped to the buffered seat ceiling ([timeouts.agy] ceiling_s, row 14: at least 2400)
        dl, _ = timing.adaptive_deadline(self.log, "agy", "m", 400000, self.cfg)
        self.assertEqual(dl, self.cfg["timeouts"]["agy"]["ceiling_s"])
        self.assertGreaterEqual(dl, 2400)
        # a different model with too few own lines falls back to the provider's lines
        dl, src = timing.adaptive_deadline(self.log, "agy", "other", 1000, self.cfg)
        self.assertEqual(src, "p95")
        # provider with no samples is cold
        self.assertEqual(timing.adaptive_deadline(self.log, "nosuch", "m", 1000, self.cfg), (600, "cold"))
        # per-provider floor
        cfg = copy.deepcopy(self.cfg)
        cfg["timeouts"]["default"]["min_timeout_s"] = 700
        self.assertEqual(timing.adaptive_deadline(self.log, "agy", "m", 1000, cfg)[0], 3600)   # provider floor wins
        del cfg["timeouts"]["agy"]["min_timeout_s"]
        self.assertEqual(timing.adaptive_deadline(self.log, "agy", "m", 1000, cfg)[0], 700)
        # no ceiling (row 35): a warm deadline is not clamped from above (and never becomes 0 s)
        cfg["timeouts"]["agy"]["ceiling_s"] = 0
        dl, src = timing.adaptive_deadline(self.log, "agy", "m", 400000, cfg)
        self.assertEqual(src, "p95")
        self.assertGreater(dl, 3600)

    def test_7_append_whitelist_and_no_secret(self):
        key = "sk-or-v1-TESTSECRET123456"
        os.environ["OPENROUTER_API_KEY"] = key
        self.addCleanup(os.environ.pop, "OPENROUTER_API_KEY", None)
        timing.append(self.log, {"kind": "seat", "provider": "stub", "outcome": "ok", "total_s": 1.5,
                                 "argv": ["codex", "--key", key], "env": {"K": key},
                                 "prompt": "secret question", "extra": 1})
        with open(self.log) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertTrue(set(rec) <= set(timing.LOG_KEYS), rec)
        for bad in ("argv", "env", "prompt", "extra"):
            self.assertNotIn(bad, rec)
        self.assertEqual(oct(os.stat(os.path.dirname(self.log)).st_mode & 0o777), oct(0o700))
        for path in glob.glob(os.path.join(self.tmp, "**"), recursive=True):
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    self.assertNotIn(b"TESTSECRET", f.read(), path)


class BaseConfig(unittest.TestCase):
    def test_8_base_loads_and_validates(self):
        cfg = config.load()
        self.assertEqual(config.validate_roster(cfg), [])
        self.assertEqual(cfg["chair"]["provider"], "claude")
        self.assertEqual(cfg["policy"]["max_anthropic_advisors"], 1)
        self.assertEqual([s["id"] for s in cfg["seat"]],
                         ["claude", "codex", "agy", "openrouter", "deepseek"])
        self.assertEqual(cfg["pins"]["codex"], "0.163.0")


class StubAdapter(unittest.TestCase):
    def test_stub_adapter(self):
        a = providers.get("stub")
        s = {"id": "s", "provider": "stub", "model": "m", "stub_mode": "slow-stream 2"}
        launch = a.build(s, "/tmp/p.md", "/tmp", None, {})
        self.assertEqual(launch.argv[-2:], ["slow-stream", "2"])
        self.assertEqual(launch.stdin_path, "/tmp/p.md")
        ex = a.extractor(s)
        ex.feed(b"hello ")
        ex.feed(b"world\n")
        self.assertEqual((ex.final_text(), ex.model_reported()), ("hello world", "m"))
        self.assertEqual(a.extractor({"model": "m", "stub_reported": "x"}).model_reported(), "x")
        self.assertTrue(a.model_matches("m", "m"))
        rows = a.free_checks([s, dict(s, id="t", stub_fail_free=True)], {})
        self.assertEqual([r.result for r in rows if r.check != "inner_caps"], ["PASS", "FAIL"])
        self.assertTrue(any(r.check == "inner_caps" for r in rows))
        with self.assertRaises(KeyError):
            providers.get("nope")        # every real provider is registered now (row 8)
        self.assertTrue(providers.is_anthropic({"provider": "agy", "model": "Claude Opus 4.6"}))
        self.assertFalse(providers.is_anthropic({"provider": "agy", "model": "Gemini 3.8 Flash (High)"}))


# ---------------------------------------------------------------- row 29: run tables, gate keys, quota, routing
GLM_ENDPOINTS = [      # shape of GET /models/z-ai/glm-5/endpoints (37-openrouter-scout, 2026-10-21); stable fields only
    {"provider_name": "GMICloud", "tag": "gmicloud/fp8", "max_completion_tokens": 182476,
     "supported_parameters": ["max_tokens", "reasoning", "temperature"]},
    {"provider_name": "Z.AI", "tag": "z-ai/fp8", "max_completion_tokens": 131072,
     "supported_parameters": ["max_tokens", "reasoning"]},
    {"provider_name": "Venice", "tag": "venice/fp8", "max_completion_tokens": 32000,
     "supported_parameters": ["max_tokens"]},
]


def merged(**tables):
    return config.deep_merge(config.load(), tables)


class RunTables(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfg-tables-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_9_shipped_tables_are_valid(self):
        cfg = config.load()
        self.assertEqual(cfg["hedge"], {"enabled": True, "providers": ["codex"], "factor": 2.5, "floor_s": 900,
                                        "cap_s": 1800, "cold_s": 1200, "quiet_s": 120, "recheck_s": 30, "max_defer": 2.0})
        self.assertEqual(cfg["fallback"], {"enabled": True})
        self.assertEqual(cfg["liveness"], {"signals": True, "sample_s": 10, "grace_s": 120, "no_conn_samples": 18})
        for fn in (providers.hedge_problems, providers.fallback_problems, providers.liveness_problems,
                   providers.signal_problems, providers.routing_problems):
            self.assertEqual(fn(cfg), [], fn.__name__)

    def test_9b_row_34_rollout_matches_the_gate(self):
        """Row 34 (D26, D40, D41, D46): the hedge is on for exactly the hedge_safe seats row 33 saw run beside the original
        (codex); agy is not hedge_safe and is not hedged and keeps its ceiling; the extra fallback is on for every seat;
        the probe-signal flags are on and reach only codex (agy has no signal to add)."""
        cfg = config.load()
        self.assertIs(cfg["hedge"]["enabled"], True)
        self.assertEqual(cfg["hedge"]["providers"], ["codex"])
        self.assertNotIn("agy", cfg["hedge"]["providers"])
        self.assertEqual(providers.hedge_problems(cfg), [])
        for p in ("claude", "qwen", "openrouter"):                  # stream seats are never watch-hedged
            self.assertNotIn(p, cfg["hedge"]["providers"])
        self.assertIs(cfg["fallback"]["enabled"], True)
        self.assertIs(cfg["liveness"]["signals"], True)
        self.assertIs(providers.signals_on(cfg), True)
        pcs = cfg["providers"]
        self.assertEqual(pcs["codex"]["progress_signal"], "otel")
        self.assertEqual(pcs["agy"]["progress_signal"], "none")      # nothing for the signal flag to add on agy
        self.assertEqual(cfg["timeouts"]["agy"]["ceiling_s"], 3600)  # agy keeps its ceiling (row 35 leaves it alone)
        self.assertEqual(cfg["timeouts"]["agy"]["min_timeout_s"], 3600)
        seats = {s["id"]: s for s in cfg["seat"]}
        codex = providers.codex_signal_args(seats["codex"], pcs["codex"], cfg, {"otlp_port": 4318})
        self.assertIn("model_reasoning_summary=detailed", codex)
        self.assertTrue(any(a.startswith("otel.exporter=") for a in codex))
        agy_wd = tempfile.mkdtemp(prefix="cfg-agy-")
        self.addCleanup(shutil.rmtree, agy_wd, ignore_errors=True)
        self.assertEqual(providers.agy_signal_args(seats["agy"], pcs["agy"], cfg, {"log_path": agy_wd + "/x.log"}, agy_wd), [])

    def test_10_gate_keys_ship_as_the_gate_says(self):
        pcs = config.load()["providers"]
        c, a = pcs["codex"], pcs["agy"]
        self.assertEqual((c["progress_signal"], c["progress_event"], c["progress_exclude"], c["negative_evidence"],
                          c["reasoning_summary"]),
                         ("otel", "codex.sse_event", ["event.kind=keepalive"], True, "detailed"))
        self.assertEqual(c["terminal_lines"], ["stream disconnected before completion: idle timeout waiting for SSE"])
        self.assertEqual((a["progress_signal"], a["negative_evidence"]), ("none", False))
        self.assertEqual(a["terminal_lines"], ["[agy] print timeout after",
                                               ["AGY_ERROR:", '"status":"RESOURCE_EXHAUSTED"']])
        for stream in ("claude", "openrouter", "qwen"):
            self.assertNotIn("progress_signal", pcs[stream])

    def test_11_the_liveness_layer_loads_the_shipped_tables(self):
        from councillib import liveness
        cfg = config.load()
        c = liveness.LivenessPolicy.from_config(cfg["providers"]["codex"], cfg["liveness"])
        a = liveness.LivenessPolicy.from_config(cfg["providers"]["agy"], cfg["liveness"])
        self.assertEqual((c.progress_signal, c.negative_evidence, c.sample_s, c.grace_s, c.no_conn_samples),
                         ("otel", True, 10, 120, 18))
        self.assertEqual([r for r, _ in c.terminal_lines], ["cli_idle_timeout"])
        self.assertEqual((a.progress_signal, a.negative_evidence), ("none", False))
        self.assertEqual([r for r, _ in a.terminal_lines], ["cli_timeout", "quota_exhausted"])

    def test_12_hedge_validation(self):
        bad = {"enabled": "yes", "factor": 0, "floor_s": -1, "cap_s": "x", "recheck_s": True, "quiet_s": -5,
               "max_defer": 0.5, "providers": "codex", "typo": 1}
        probs = " | ".join(providers.hedge_problems(merged(hedge=bad)))
        for needle in ("enabled must be true or false", "factor must be", "floor_s must be", "cap_s must be",
                       "recheck_s must be", "quiet_s must be", "max_defer must be", "providers must be a list",
                       "unknown key 'typo'"):
            self.assertIn(needle, probs)
        self.assertTrue(any("floor_s" in p and "above cap_s" in p
                            for p in providers.hedge_problems(merged(hedge={"floor_s": 2000}))))
        self.assertTrue(any("cold_s" in p for p in providers.hedge_problems(merged(hedge={"cold_s": 100}))))
        self.assertTrue(any("cold_s" in p for p in providers.hedge_problems(merged(hedge={"cold_s": 5000}))))
        for names, needle in ((["nope"], "unknown provider"), (["claude"], "stream seat"), (["stub"], "unknown provider"),
                              (["openrouter"], "stream seat")):
            self.assertTrue(any(needle in p for p in providers.hedge_problems(merged(hedge={"providers": names}))), names)
        self.assertEqual(providers.hedge_problems(merged(hedge={"providers": ["codex", "agy"], "enabled": True})), [])
        self.assertEqual(providers.hedge_problems({}), [])                # absent table: nothing to check
        self.assertTrue(providers.hedge_problems({"hedge": 3}))

    def test_13_fallback_and_liveness_validation(self):
        self.assertTrue(providers.fallback_problems(merged(fallback={"enabled": 1})))
        self.assertTrue(any("typo" in p for p in providers.fallback_problems(merged(fallback={"typo": 1}))))
        probs = " | ".join(providers.liveness_problems(merged(
            liveness={"signals": 1, "sample_s": 0, "grace_s": -1, "no_conn_samples": 0, "baseline_s": "x", "extra": 1})))
        for needle in ("signals must be true or false", "sample_s must be", "grace_s must be",
                       "no_conn_samples must be an integer", "baseline_s must be", "unknown key 'extra'"):
            self.assertIn(needle, probs)
        self.assertTrue(providers.liveness_problems(merged(liveness={"no_conn_samples": True})))
        self.assertEqual(providers.liveness_problems(merged(liveness={"signals": True})), [])

    def test_14_gate_key_validation(self):
        def probs(name, **kw):
            return " | ".join(providers.signal_problems(merged(providers={name: kw})))
        self.assertIn("progress_signal 'wat'", probs("codex", progress_signal="wat"))
        self.assertIn("only defined for codex", probs("agy", progress_signal="otel"))
        self.assertIn("only defined for agy", probs("codex", progress_signal="applog"))
        self.assertIn("negative_evidence", probs("codex", negative_evidence="yes"))
        self.assertIn("progress_exclude", probs("codex", progress_exclude="event.kind=keepalive"))
        self.assertIn("terminal_lines must be a list", probs("codex", terminal_lines="x"))
        self.assertIn("terminal_lines[0]", probs("codex", terminal_lines=[""]))
        self.assertIn("terminal_lines[0]", probs("codex", terminal_lines=[[]]))
        self.assertIn("terminal_lines[0]", probs("codex", terminal_lines=[{"match": "x", "reason": "nope"}]))
        self.assertIn("terminal_lines[0]", probs("codex", terminal_lines=[{"reason": "stream_error"}]))
        self.assertIn("reasoning_summary 'loud'", probs("codex", reasoning_summary="loud"))
        self.assertIn("only defined for codex", probs("agy", reasoning_summary="detailed"))
        self.assertEqual(probs("codex", terminal_lines=["a", ["b", "c"], {"reason": "stream_error", "match": "d"}]), "")

    def test_15_preflight_engine_rows_fail_a_bad_table_before_any_spend(self):
        from councillib import preflight
        rows = {c.check: c for c in preflight.table_rows(config.load())}
        self.assertEqual({k: rows[k].result for k in ("hedge", "fallback", "liveness", "signals", "routing")},
                         dict.fromkeys(("hedge", "fallback", "liveness", "signals", "routing"), "PASS"))
        self.assertEqual(rows["hedge"].detail, "hedge on (providers codex)")
        self.assertEqual(rows["fallback"].detail, "fallback on")
        self.assertEqual(rows["liveness"].detail, "probe-signal flags on")
        bad = preflight.table_rows(merged(hedge={"cap_s": -1}, liveness={"sample_s": 0}, fallback={"x": 1},
                                          providers={"codex": {"progress_signal": "wat"}}))
        self.assertEqual({c.check for c in bad if c.result == "FAIL"}, {"hedge", "liveness", "fallback", "signals"})
        self.assertTrue(all(c.provider == "engine" and c.stage == "engine" for c in bad))
        eng = preflight.engine_stage(merged(hedge={"cap_s": -1}, state_dir=self.tmp))
        self.assertEqual([c.check for c in eng if c.result == "FAIL"], ["hedge"])

    # ---- D47: recorded quota state
    def test_16_quota_state_roundtrip_and_expiry(self):
        cfg = {"state_dir": self.tmp}
        self.assertEqual(providers.read_quota(cfg), {})
        providers.record_quota(cfg, "agy", "2026-10-28T07:15:56Z", now=1_000_000)
        path = os.path.join(self.tmp, "quota.json")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o600))
        rec = providers.read_quota(cfg)["agy"]
        self.assertEqual((rec["until"], rec["reason"]), ("2026-10-28T07:15:56Z", "quota_exhausted"))
        before = 1_792_000_000          # 2026-10-14
        after = 1_793_500_000           # 2026-11-01
        self.assertEqual(list(providers.quota_unavailable(cfg, now=before)), ["agy"])
        self.assertEqual(providers.quota_unavailable(cfg, now=after), {})
        providers.record_quota(cfg, "codex", "2026-10-30T00:00:00Z")
        self.assertEqual(sorted(providers.read_quota(cfg)), ["agy", "codex"])       # a second record keeps the first
        with self.assertRaises(ValueError):
            providers.record_quota(cfg, "agy", "tomorrow")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(providers.quota_unavailable(cfg, now=before), {})      # unreadable state never blocks a run

    def test_17_preflight_marks_the_seat_unavailable_without_a_probe_call(self):
        from councillib import preflight
        cfg = merged(state_dir=self.tmp, seat=[{"id": "a", "provider": "stub", "model": "m"},
                                               {"id": "b", "provider": "codex", "model": "gpt-6-astra"}])
        self.assertEqual(preflight.quota_rows(cfg), [])
        providers.record_quota(cfg, "codex", "2999-01-01T00:00:00Z")
        rows = preflight.quota_rows(cfg)
        self.assertEqual([(r.provider, r.check, r.result) for r in rows], [("codex", "quota:b", "UNAVAILABLE")])
        self.assertIn("2999-01-01T00:00:00Z", rows[0].detail)
        self.assertEqual(preflight.unavailable_providers(rows), {"codex"})
        self.assertEqual(preflight.blocking_fails(rows), [])                    # unavailable never fails the preflight
        self.assertEqual(preflight.failed_providers(rows), set())
        self.assertEqual(json.loads(preflight.to_json(rows))["unavailable_providers"], ["codex"])
        providers.record_quota(cfg, "codex", "2000-01-01T00:00:00Z")            # reset time in the past
        self.assertEqual(preflight.quota_rows(cfg), [])

    def test_18_an_unavailable_provider_is_not_canaried(self):
        from councillib import preflight
        cfg = merged(state_dir=self.tmp, prompts_dir=os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts"),
                     seat=[{"id": "a", "provider": "stub", "model": "m", "stub_mode": "quick"}],
                     chair={"provider": "stub", "model": "chair-m", "stub_mode": "quick"},
                     providers={"stub": {"mode": "stream"}}, canary={"timeout_s": 20, "idle_s": 10})
        providers.record_quota(cfg, "stub", "2999-01-01T00:00:00Z")
        ok, checks = preflight.run(cfg)
        self.assertTrue(ok)
        canary = [c for c in checks if c.stage == "canary"]
        self.assertEqual([(c.result, "unavailable" in c.detail) for c in canary], [("SKIP", True)])
        self.assertEqual(preflight.unavailable_providers(checks), {"stub"})

    # ---- D43/D45: OpenRouter routing prefs and the budget, against the endpoints snapshot
    def test_19_openrouter_seats_are_unpinned_at_131072(self):
        # fallback-chain row 1 (2026-10-30): the 182476 / 182520 budgets pinned each seat to ONE endpoint (gmicloud, baseten), so a
        # blip there was an unroutable error. Both seats now ask for 131072, which most endpoints serve, with no `order`.
        cfg = config.load()
        glm = next(s for s in cfg["seat"] if s["id"] == "openrouter")
        ds = next(s for s in cfg["seat"] if s["id"] == "deepseek")
        self.assertEqual((glm["max_output_tokens"], ds["max_output_tokens"]), (131072, 131072))
        chk = providers.routing_check(glm, providers._pcfg(cfg, "openrouter"), GLM_ENDPOINTS,
                                      providers.routing_prefs(cfg, glm))
        self.assertEqual((chk.check, chk.result), ("routing:openrouter", "PASS"), chk.detail)
        self.assertEqual(providers.routing_prefs(cfg, ds), {"require_parameters": True})
        self.assertEqual(providers.routing_prefs(cfg, glm), {"require_parameters": True})
        self.assertIsNone(providers.routing_prefs({}, {"id": "x"}))

    def test_20_routing_check_fails_when_no_endpoint_can_serve_the_seat(self):
        cfg = config.load()
        pc = providers._pcfg(cfg, "openrouter")
        seat = {"id": "g", "provider": "openrouter", "model": "z-ai/glm-5", "max_output_tokens": 182476}
        prefs = {"require_parameters": True}
        only_small = [e for e in GLM_ENDPOINTS if e["provider_name"] != "GMICloud"]
        r = providers.routing_check(seat, pc, only_small, prefs)
        self.assertEqual(r.result, "FAIL")
        self.assertIn("largest 131072", r.detail)
        self.assertEqual(providers.routing_check(seat, pc, only_small, {}).result, "WARN")       # not required: a WARN
        r = providers.routing_check(seat, pc, GLM_ENDPOINTS, {"require_parameters": True, "ignore": ["gmicloud"]})
        self.assertEqual(r.result, "FAIL")                                                          # the only fit is ignored
        noreason = [dict(e, supported_parameters=["max_tokens"]) for e in GLM_ENDPOINTS]
        self.assertEqual(providers.routing_check(seat, pc, noreason, prefs).result, "FAIL")
        self.assertEqual(providers.routing_check(seat, pc, [], prefs).result, "FAIL")

    def test_21_routing_check_warns_on_a_name_that_matches_nothing_or_holds_too_little(self):
        cfg = config.load()
        pc = providers._pcfg(cfg, "openrouter")
        seat = {"id": "g", "provider": "openrouter", "model": "z-ai/glm-5", "max_output_tokens": 182476}
        r = providers.routing_check(seat, pc, GLM_ENDPOINTS, {"require_parameters": True, "order": ["nobody"]})
        self.assertEqual(r.result, "WARN")
        self.assertIn("'nobody' matches no endpoint", r.detail)
        r = providers.routing_check(seat, pc, GLM_ENDPOINTS, {"require_parameters": True, "order": ["z-ai"]})
        self.assertEqual(r.result, "WARN")
        self.assertIn("holds less than", r.detail)
        self.assertEqual(providers.routing_check(seat, pc, GLM_ENDPOINTS, {"order": ["Z.AI", "GMICloud"]}).result, "WARN")
        self.assertEqual(providers.routing_check(seat, pc, GLM_ENDPOINTS, {"order": ["GMICloud"]}).result, "PASS")

    def test_22_routing_prefs_validation_forbids_hard_pins(self):
        for key in ("only", "allow_fallbacks", "max_price", "quantizations"):
            for val in (["x"], False, "x"):
                cfg = merged(providers={"openrouter": {"provider": {key: val}}})
                self.assertTrue(any(repr(key) in p and "unknown key" in p for p in providers.routing_problems(cfg)),
                                (key, val))
                cfg = merged(seat=[{"id": "a", "provider": "openrouter", "model": "m", "provider_prefs": {key: val}}])
                self.assertTrue(any("seat 'a'" in p and repr(key) in p for p in providers.routing_problems(cfg)),
                                (key, val))
        cfg = merged(seat=[{"id": "a", "provider": "openrouter", "model": "m", "provider_prefs": {"order": "gmicloud"}}])
        self.assertTrue(any("order must be a list" in p for p in providers.routing_problems(cfg)))
        cfg = merged(seat=[{"id": "a", "provider": "openrouter", "model": "m", "provider_prefs": 5}])
        self.assertTrue(any("must be a table" in p for p in providers.routing_problems(cfg)))
        cfg = merged(providers={"openrouter": {"provider": {"require_parameters": "yes"}}})
        self.assertTrue(any("require_parameters" in p for p in providers.routing_problems(cfg)))

    def test_22b_p26_ordering_preferences_pass_with_valid_values(self):
        """P26: `sort` and the two `preferred_*` thresholds are preferences (they order endpoints, never drop one), so
        they are allowed in the base table and in a seat's provider_prefs; their values are checked."""
        good = [{"sort": s} for s in ("price", "throughput", "latency")]
        good += [{"preferred_min_throughput": 80}, {"preferred_min_throughput": 42.5},
                 {"preferred_max_latency": 3}, {"preferred_max_latency": 0.5},
                 {"require_parameters": True, "sort": "throughput", "preferred_min_throughput": 80,
                  "preferred_max_latency": 2}]
        for prefs in good:
            cfg = merged(providers={"openrouter": {"provider": dict(prefs)}})
            self.assertEqual(providers.routing_problems(cfg), [], prefs)
            cfg = merged(seat=[{"id": "a", "provider": "openrouter", "model": "m", "provider_prefs": dict(prefs)}])
            self.assertEqual(providers.routing_problems(cfg), [], prefs)

    def test_22c_p26_bad_preference_values_are_refused(self):
        bad = [("sort", "fastest", "sort must be one of"), ("sort", "Throughput", "sort must be one of"),
               ("sort", {"by": "throughput", "partition": "none"}, "string form only"),
               ("sort", ["throughput"], "sort must be one of"), ("sort", True, "sort must be one of")]
        for k in ("preferred_min_throughput", "preferred_max_latency"):
            bad += [(k, v, "%s must be a positive number" % k)
                    for v in (-5, 0, 0.0, "80", True, float("nan"), float("inf"), {"p50": 80}, [80])]
        for key, val, msg in bad:
            cfg = merged(providers={"openrouter": {"provider": {key: val}}})
            probs = providers.routing_problems(cfg)
            self.assertTrue(any(msg in p and "[providers.openrouter.provider]" in p for p in probs), (key, val, probs))
            cfg = merged(seat=[{"id": "a", "provider": "openrouter", "model": "m", "provider_prefs": {key: val}}])
            probs = providers.routing_problems(cfg)
            self.assertTrue(any(msg in p and "seat 'a'" in p for p in probs), (key, val, probs))
            self.assertFalse(any("unknown key" in p for p in probs), "a known key with a bad value is not unknown")

    def test_22d_p26_keys_keep_the_engine_routing_row_green_and_pins_still_fail_it(self):
        """The preflight row the shared config trap is about (`engine/routing`, preflight.py): PASS with the new keys,
        FAIL with a pin, exactly as before for a config without them."""
        from councillib import preflight
        def row(cfg):
            return [r for r in preflight.table_rows(cfg) if r.check == "routing"][0]
        self.assertEqual(row(merged()).result, "PASS")
        ok = merged(seat=[{"id": "deepseek", "provider": "openrouter", "model": "m",
                           "provider_prefs": {"preferred_min_throughput": 80, "sort": "throughput"}}])
        self.assertEqual(row(ok).result, "PASS")
        for pin in ({"only": ["x"]}, {"allow_fallbacks": False}):
            bad = merged(seat=[{"id": "deepseek", "provider": "openrouter", "model": "m", "provider_prefs": pin}])
            self.assertEqual(row(bad).result, "FAIL", pin)

    def test_23_the_openrouter_free_check_reports_the_routing_row(self):
        cfg = config.load()
        seen = []

        def fake_get(path, cfg_, **kw):
            seen.append(path)
            if path == "/key":
                return 200, {}
            if path == "/models":
                return 200, {"data": [{"id": "z-ai/glm-5.3", "reasoning": {"supported_efforts": ["high", "low"]}},
                                      {"id": "deepseek/deepseek-v4-pro-0813", "reasoning": {"supported_efforts": ["high", "low", "none"]}}]}
            if path.startswith("/models/z-ai/glm-5.3/endpoints"):
                return 200, {"data": {"endpoints": GLM_ENDPOINTS}}
            return 404, None
        old = providers.openrouter_get
        providers.openrouter_get = fake_get
        self.addCleanup(setattr, providers, "openrouter_get", old)
        os.environ["OPENROUTER_API_KEY"] = "sk-test-not-real"
        self.addCleanup(os.environ.pop, "OPENROUTER_API_KEY", None)
        rows = {c.check: c for c in providers.get("openrouter").free_checks(cfg["seat"], cfg)}
        self.assertEqual(rows["routing:openrouter"].result, "PASS")
        self.assertEqual(rows["routing:deepseek"].result, "WARN")       # the endpoints GET answered 404: not checked, not a FAIL
        self.assertIn("/models/z-ai/glm-5.3/endpoints", seen)


class NoCeiling(unittest.TestCase):
    """Row 35 (owner ruling 1, D27, D28, D46): a seat whose hedge is proven (codex) has no ceiling: `ceiling_s = 0` read
    through the one helper `timing.ceiling_of` -> None. agy keeps 3600 s and --print-timeout 3660s, stream seats 7200."""

    def setUp(self):
        self.cfg = config.load()
        self.tmp = tempfile.mkdtemp(prefix="nc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "latency.jsonl")

    def seat_of(self, prov):
        return next(dict(s) for s in self.cfg["seat"] if s["provider"] == prov)

    def test_n01_ceiling_of_zero_is_none_both_cfg_shapes_and_bad_values_raise(self):
        c = self.cfg
        self.assertEqual(c["timeouts"]["codex"]["ceiling_s"], 0)
        self.assertIsNone(timing.ceiling_of(c, "codex"))
        self.assertIsNone(timing.ceiling_of(c["timeouts"], "codex"))          # the [timeouts] table alone works too
        self.assertEqual(timing.ceiling_of(c, "agy"), 3600.0)
        for p in ("claude", "qwen", "openrouter"):
            self.assertEqual(timing.ceiling_of(c, p), 7200.0, p)
        self.assertEqual(timing.ceiling_of(c, "claude", chair=True), 7200.0)
        self.assertEqual(timing.ceiling_of(c, "codex", chair=True), 7200.0)   # a codex chair keeps the chair ceiling
        self.assertEqual(timing.ceiling_of({}, "x"), 1200.0)                  # code default unchanged
        for bad in (-1, "3600", True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                timing.ceiling_of({"timeouts": {"x": {"ceiling_s": bad}}}, "x")

    def test_n02_done_when_proven_seat_has_no_ceiling_for_seat_hedge_and_fallback(self):
        from councillib import council_run
        codex, agy = self.seat_of("codex"), self.seat_of("agy")
        for kind in ("seat", "hedge", "fallback"):
            s = codex if kind != "fallback" else providers.get("codex").salvage_seat(codex, self.cfg, 8000)
            lim, mode, deadline, _src = council_run._limits(self.cfg, s, kind, self.log, 1000)
            self.assertIs(type(lim), council_run.WatchLimits, kind)
            self.assertEqual((mode, deadline, lim.deadline_s), ("watch", None, None), kind)
            self.assertIsNone(lim.ceiling_s, kind)                            # None, never 0.0 (an instant kill)
        lim = council_run._limits(self.cfg, codex, "seat", self.log, 1000)[0]
        self.assertGreater(lim.soft_at_s, 0)                                  # the soft notice (hedge time) is kept
        # agy (D46: no hedge) keeps its 3600 s ceiling, its cold deadline and --print-timeout 3660s (D28)
        lim, mode, deadline, _ = council_run._limits(self.cfg, agy, "seat", self.log, 1000)
        self.assertEqual((mode, deadline, lim.ceiling_s), ("buffered", 3600.0, 3600.0))
        flim = council_run._limits(self.cfg, providers.get("agy").salvage_seat(agy, self.cfg, 8000), "fallback",
                                   self.log, 1000)[0]
        self.assertEqual((flim.mode, flim.ceiling_s), ("watch", 3600.0))
        prompt = os.path.join(self.tmp, "p.md")
        with open(prompt, "w") as f:
            f.write("Q?")
        wd = os.path.join(self.tmp, "wd")
        os.makedirs(wd)
        argv = providers.get("agy").build(agy, prompt, wd, lim, self.cfg).argv
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "3660s")
        self.assertEqual(providers._max_ceiling(self.cfg, "agy"), 3600.0)
        # codex builds with no ceiling (its argv carries no ceiling-derived value)
        clim = council_run._limits(self.cfg, codex, "seat", self.log, 1000)[0]
        self.assertTrue(providers.get("codex").build(codex, prompt, wd, clim, self.cfg).argv)
        # stream seats keep 7200 (D3), the chair too; their salvage limits are unchanged
        for p in ("claude", "openrouter"):
            lim, mode, *_ = council_run._limits(self.cfg, self.seat_of(p), "seat", self.log, 1000)
            self.assertEqual((mode, lim.ceiling_s), ("stream", 7200.0), p)
        chair = dict(self.cfg["chair"], id="chair", role="chair")
        self.assertEqual(council_run._limits(self.cfg, chair, "chair", self.log, 1000)[0].ceiling_s, 7200.0)
        sv = council_run._limits(self.cfg, codex, "salvage", self.log, 1000)
        self.assertEqual((sv[0].ceiling_s, sv[2]), (600, 600))

    def test_n03_a_zero_ceiling_never_reaches_a_launch(self):
        from councillib import council_run
        got = (council_run.Limits(mode="buffered", idle_s=1, ceiling_s=0.0, deadline_s=1.0), "buffered", 1.0, "x")
        with self.assertRaises(ValueError):
            council_run._no_zero_ceiling(got, "p", "seat")
        cfg = config.deep_merge(self.cfg, {"salvage": {"timeout": 0}})       # a 0 s salvage ceiling is refused too
        with self.assertRaises(ValueError):
            council_run._limits(cfg, self.seat_of("codex"), "salvage", self.log, 1000)

    def test_n04_preflight_allows_no_ceiling_only_for_a_hedged_codex_or_agy(self):
        self.assertEqual(providers.ceiling_problems(self.cfg), [])
        self.assertEqual(providers.hedge_problems(self.cfg), [])

        def probs(over):
            return providers.hedge_problems(config.deep_merge(self.cfg, over))
        self.assertTrue(any("needs the hedge on for codex" in p for p in probs({"hedge": {"enabled": False}})))
        self.assertTrue(any("needs the hedge on for codex" in p for p in probs({"hedge": {"providers": []}})))
        self.assertTrue(any("needs the hedge on for agy" in p
                            for p in probs({"timeouts": {"agy": {"ceiling_s": 0}}})))     # agy has no hedge (D46)
        for name in ("default", "chair"):
            self.assertTrue(any("[timeouts.%s] ceiling_s = 0" % name in p
                                for p in probs({"timeouts": {name: {"ceiling_s": 0}}})), name)
        for name in ("claude", "qwen", "openrouter"):
            self.assertTrue(any("only codex/agy can run without one" in p
                                for p in probs({"timeouts": {name: {"ceiling_s": 0}}})), name)
        self.assertTrue(any("must be 0 (no ceiling) or a number > 0" in p
                            for p in probs({"timeouts": {"codex": {"ceiling_s": -5}}})))
        cfg = copy.deepcopy(self.cfg)
        del cfg["hedge"]                                                     # no [hedge] table: still checked
        self.assertTrue(any("needs the hedge on" in p for p in providers.hedge_problems(cfg)))

    def test_n05_every_reader_handles_no_ceiling(self):
        from councillib.supervise import Limits
        # codex preflight: the CLI idle timer must still be >= 3600000 ms (D27)
        pc = self.cfg["providers"]["codex"]
        name = pc["council_provider"]
        ad = providers.get("codex")
        blk = {"requires_openai_auth": True, "stream_idle_timeout_ms": 3600000, "request_max_retries": 1,
               "stream_max_retries": 1}
        ok = ad._inner_caps(pc, self.cfg, {"model_providers": {name: blk}})
        self.assertEqual(ok.result, "PASS", ok.detail)
        self.assertIn(">= 3600000ms", ok.detail)
        low = ad._inner_caps(pc, self.cfg, {"model_providers": {name: dict(blk, stream_idle_timeout_ms=3599999)}})
        self.assertEqual(low.result, "FAIL")
        self.assertEqual(providers.NO_CEILING_CODEX_IDLE_MS, 3600000)
        # stream readers take their inner caps from the ceiling: with none they refuse (preflight FAILs it first)
        with self.assertRaises(ValueError):
            providers.claude_env(None, self.cfg["providers"]["claude"], 1000)
        with self.assertRaises(ValueError):
            providers.qwen_guards_ms(Limits(mode="stream", idle_s=240, ceiling_s=None, deadline_s=None))
        with self.assertRaises(ValueError):
            providers.or_socket_timeout(Limits(mode="buffered", idle_s=240, ceiling_s=None, deadline_s=None))
        self.assertEqual(providers.or_socket_timeout(Limits(mode="stream", idle_s=240, ceiling_s=None,
                                                            deadline_s=None)), 270.0)
        # agy with no ceiling (not shipped): the build omits --print-timeout (D28, row 23)
        lim = Limits(mode="buffered", idle_s=240, ceiling_s=None, deadline_s=None)
        prompt = os.path.join(self.tmp, "p.md")
        with open(prompt, "w") as f:
            f.write("Q?")
        os.makedirs(os.path.join(self.tmp, "wd"))
        argv = providers.get("agy").build(self.seat_of("agy"), prompt, os.path.join(self.tmp, "wd"), lim, self.cfg).argv
        self.assertNotIn("--print-timeout", argv)

    def test_n06_extra_claude_env_key_reaches_the_seat_unchanged(self):
        # P14 (run example-run, row 24): a key under [providers.claude.env] that is not one of
        # the two raised timeouts (e.g. a prompt-cache switch) is passed to the claude launch as given, as a string
        key = "FORCE_PROMPT_CACHING_5M"
        pc = dict(self.cfg["providers"]["claude"])
        pc["env"] = dict(pc.get("env") or {}, **{key: "1"})
        env = providers.claude_env(1200, pc, None)
        self.assertEqual(env[key], "1")
        self.assertEqual((env["API_TIMEOUT_MS"], env["CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"]), ("1260000", "1200000"))
        self.assertEqual(providers.claude_env(1200, dict(pc, env={key: 1}), None)[key], "1")     # a TOML int works too
        self.assertNotIn(key, providers.claude_env(1200, dict(pc, env={}), None))
        # through the adapter: the seat's launch env carries it
        from councillib.supervise import Limits
        cfg = dict(self.cfg, providers=dict(self.cfg["providers"], claude=pc))
        prompt = os.path.join(self.tmp, "p.md")
        with open(prompt, "w") as f:
            f.write("Q?")
        os.makedirs(os.path.join(self.tmp, "wd"))
        lim = Limits(mode="stream", idle_s=240, ceiling_s=1200, deadline_s=None)
        launch = providers.get("claude").build(self.seat_of("claude"), prompt, os.path.join(self.tmp, "wd"), lim, cfg)
        self.assertEqual(launch.env.get(key), "1")


from tests import test_council_run as T  # noqa: E402  (stub council harness; reached through the module)


class NoCeilingCouncil(T.HedgeBase):
    """Row 35 stub councils: a hedged buffered seat with `ceiling_s = 0` still gets the soft notice and its hedge,
    ends only on its own terminal state, goes pending (D39), lets the launcher return (D50) and survives a dead
    runner for the orphan sweep (D54). Stub modes all end by themselves: with no ceiling a hung stub would never end."""

    kill_stubs = T.RunnerTests.kill_stubs
    stub_pids = T.RunnerTests.stub_pids
    pid_of = T.RunnerTests.pid_of
    harness = T.RunnerTests.harness
    launch = T.RunnerTests.launch
    wait_runner_done = T.RunnerTests.wait_runner_done
    read_log = T.RunnerTests.read_log

    def setUp(self):
        super().setUp()
        self.addCleanup(self.kill_stubs)

    def ncfg(self, seats, **kw):
        cfg = self.hcfg(seats, **kw)
        cfg["timeouts"]["hstub"] = {"ceiling_s": 0}          # no ceiling for the hedged stub seat only
        return cfg

    def x_limits(self):
        return {ln["wd"]: ln["limits"] for ln in T.HedgeStub.launches if ln["wd"].startswith("x")}

    def test_n10_soft_notice_starts_the_hedge_original_ends_on_its_own_exit(self):
        # the soft notice is at AT s; with no ceiling a hedge that has not answered AT s after it started leaves the seat "pending" (D39),
        # so AT is generous (a loaded macOS runner starts the stub slowly) and the original outlives it by 2 s
        AT = 1.5
        seats = [T.seat("a"), T.seat("b"), T.hseat("x", "busy-silent %s" % (AT + 2), hedge_mode="quick")]
        code = self.run_council(self.ncfg(seats, at=AT))
        self.assertEqual(code, 0)
        lims = self.x_limits()
        self.assertEqual(sorted(lims), ["x", "x_hedge"])
        for wd, lim in lims.items():
            self.assertIsNone(lim.ceiling_s, wd)
            self.assertIsNone(lim.deadline_s, wd)
        self.assertEqual(lims["x"].soft_at_s, AT)
        calls = self.calls()
        self.assertEqual(calls[0]["call"], "hedge")
        self.assertGreaterEqual(calls[0]["after_s"], AT - 0.05)
        self.assertLess(calls[0]["after_s"], AT + 1.0)
        x = self.seat_json("x")
        self.assertEqual((x["status"], x["answered_by"]), ("ok", "hedge"))
        self.assertEqual(self.roles("x"), {"original": "late", "hedge": "used"})
        self.assertEqual((x["calls"][0]["status"], x["calls"][0]["exit_code"]), ("ok", 0))   # its own exit, not killed
        self.assertGreaterEqual(self.wall, AT + 1.9)
        self.assertIn("DONE", self.read("seats", "x.original.late.md"))
        self.assert_no_survivors()

    @needs_proc
    def test_n11_d39_pending_with_no_ceiling_nothing_killed_both_answers_late(self):
        seats = [T.seat("a"), T.seat("b"), T.hseat("x", "busy-silent 2.2", hedge_mode="busy-silent 2.6")]
        code = self.run_council(self.ncfg(seats))
        self.assertEqual(code, 0)
        self.assertEqual(T.ChairProbe.seen.get("x"), True, T.ChairProbe.seen)
        self.assertEqual(T.ChairProbe.seen.get("x_hedge"), True, T.ChairProbe.seen)
        summ = self.summary()
        self.assertEqual(summ["pending"], ["x"])
        x = self.seat_json("x")
        self.assertEqual({c["call"]: c["status"] for c in x["calls"]}, {"original": "ok", "hedge": "ok"})
        self.assertNotIn("ceiling_kill", json.dumps(x))
        self.assertNotIn("cut", json.dumps(x))
        self.assertIn(("x", "original"), [(e["seat"], e["call"]) for e in summ["late"]])
        self.assertIn("> LATE: seat x (original)", self.read("chair", "synthesis.md"))
        self.assert_no_survivors()

    def test_n12_d50_zero_answers_launcher_exits_4_and_the_runner_finishes_with_no_ceiling(self):
        seats = [T.seat("a", "exit 3"), T.hseat("x", "busy-silent 2.2", hedge_mode="busy-silent 2.8")]
        rc, out, wall = self.launch(self.ncfg(seats))
        self.assertEqual(rc, 4, out + self.read_log())
        self.assertLess(wall, 2.0, "D50: the launcher does not wait for the first answer")
        self.assertIn("pending: no answers yet\n", out)
        self.assertRegex(out, r"pending seats: x \(runner \d+ keeps supervising\)")
        self.wait_runner_done()
        summ = self.summary()
        self.assertEqual(summ["late_synthesis"]["status"], "ok")
        x = self.seat_json("x")
        self.assertEqual(x["answered_by"], "original")
        self.assertEqual({c["call"]: c["status"] for c in x["calls"]}, {"original": "ok", "hedge": "ok"})
        self.assert_no_survivors()

    @needs_proc
    def test_n13_orphan_sweep_leaves_a_live_no_ceiling_call_alone_then_appends_its_answer(self):
        from councillib import netwatch, preflight
        seats = [T.seat("a"), T.seat("b"), T.hseat("x", "busy-silent 2.2", hedge_mode="busy-silent 3.2")]
        cfg = self.ncfg(seats, ceiling=30)
        rc, out, _ = self.launch(cfg)
        self.assertEqual(rc, 0, out + self.read_log())
        pid = self.summary()["runner_pid"]
        os.kill(pid, 9)
        self.assertTrue(T._wait(lambda: T._gone(pid), 3))
        x = self.pid_of("x")
        tags = {}
        wdir = T.council_run.watch_dir(self.state)
        for n in os.listdir(wdir):
            if n.endswith(".json") and not n.endswith(".end.json"):
                with open(os.path.join(wdir, n)) as f:
                    r = json.load(f)
                tags[r["call"]] = r["tag"]
        self.assertEqual(sorted(tags), ["hedge", "original"])
        res = preflight.sweep_orphans(cfg)
        self.assertEqual(sorted(o["call"] for o in res["live"]), ["hedge", "original"])
        self.assertEqual(res["swept"], [])
        self.assertFalse(T._gone(x), "the sweep never kills a live call, ceiling or not")
        self.assertTrue(T._wait(lambda: not netwatch.tagged_pids(tags["original"]), 6))
        res = preflight.sweep_orphans(cfg)
        self.assertEqual([(o["call"], o["answered"], o["appended"]) for o in res["swept"]], [("original", True, True)])
        self.assertTrue(T._wait(lambda: not netwatch.tagged_pids(tags["hedge"]), 8))
        res = preflight.sweep_orphans(cfg)
        self.assertEqual([(o["call"], o["answered"]) for o in res["swept"]], [("hedge", True)])
        self.assertEqual(self.summary()["phase"], "done")
        self.assert_no_survivors()


if __name__ == "__main__":
    unittest.main()
