"""Row 43 (D56): the serving provider and the generation id of every OpenRouter call are written into the call's
entry in seats/<id>.json and calls.jsonl, taken from the call's own stream. Stubs only: no network, no paid call.

The streams are replayed by tests/stub_cli.py `replay` into the real OpenRouterExtractor. Their chunk shapes are the
ones probe 33 saw (`id`, `object`, `created`, `model`, `provider`, `choices[].delta` with `reasoning_details`,
`native_finish_reason`, a final `usage` chunk, `: OPENROUTER PROCESSING` comments, `data: [DONE]`); ids and
providers are made up, and every call below gets its OWN pair so a record that borrowed another call's cannot pass.

Run: python3 -Es -m unittest tests.test_openrouter_record   (cwd ~/.claude/council)
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import council_run, providers  # noqa: E402
from tests.test_council_run import HedgeBase, HedgeStub, hseat  # noqa: E402

MODEL = "z-ai/glm-5"


def chunk(gen_id, provider, delta, finish=None, usage=None, model=MODEL):
    d = {"id": gen_id, "object": "chat.completion.chunk", "created": 1792645795, "model": model,
         "provider": provider, "choices": [{"index": 0, "delta": dict(role="assistant", **delta),
                                            "finish_reason": finish, "native_finish_reason": finish}]}
    if gen_id is None:
        del d["id"]
    if provider is None:
        del d["provider"]
    if usage:
        d["usage"] = usage
    return "data: " + json.dumps(d, separators=(",", ":")) + "\n\n"


def stream(gen_id, provider, ok=True, model=MODEL):
    """A probe-33 shaped stream: keepalive comment, reasoning chunks, then the answer (ok) or finish_reason
    `error` with reasoning only (the nemotron/BaseTen end), a usage chunk and [DONE]."""
    out = [": OPENROUTER PROCESSING\n\n",
           chunk(gen_id, provider, {"content": "", "reasoning": "The", "reasoning_details": [
               {"type": "reasoning.text", "text": "The", "format": "unknown", "index": 0}]}, model=model),
           chunk(gen_id, provider, {"content": "", "reasoning": " user wants 2+2"}, model=model)]
    if ok:
        out.append(chunk(gen_id, provider, {"content": "ANSWER 4"}, model=model))
    usage = {"prompt_tokens": 450, "completion_tokens": 20, "total_tokens": 470, "cost": 0.001,
             "completion_tokens_details": {"reasoning_tokens": 10}}
    out.append(chunk(gen_id, provider, {"content": ""}, finish="stop" if ok else "error", usage=usage, model=model))
    out.append("data: [DONE]\n\n")
    return "".join(out)


class ORStub(HedgeStub):
    """HedgeStub (call by workdir: original, _hedge, _fallback; salvage via stub_salvage_mode) whose extractor is the
    real OpenRouterExtractor."""
    name = "orstub"

    def extractor(self, seat=None):
        return providers.OpenRouterExtractor()


class PlainStub(HedgeStub):
    name = "plainstub"


for _n in ("orstub", "plainstub"):
    providers.FAMILIES.setdefault(_n, "stub")


class Base(HedgeBase):
    def setUp(self):
        super().setUp()
        for cls in (ORStub, PlainStub):
            providers.register(cls())
            self.addCleanup(providers._REGISTRY.pop, cls.name, None)

    def sse(self, name, text):
        path = os.path.join(self.tmp, name + ".sse")
        with open(path, "w") as f:
            f.write(text)
        return ["replay", path, "0"]

    def oseat(self, sid, mode, **kw):
        return hseat(sid, mode, provider="orstub", model=MODEL, **kw)

    def ocfg(self, seats, fallback=False, salvage=True, **kw):
        cfg = self.hcfg(seats, hedge=False, fallback=fallback, salvage=salvage, **kw)
        cfg["providers"]["orstub"] = {"mode": "stream"}
        cfg["providers"]["plainstub"] = {"mode": "stream"}
        return cfg

    def seat_json(self, sid):
        return json.loads(self.read("seats", sid + ".json"))


class RecordTests(Base):
    def test_01_ok_original_records_its_provider_and_id(self):
        seats = [self.oseat("a", self.sse("a", stream("gen-AAA-1", "GMICloud"))),
                 self.oseat("b", self.sse("b", stream("gen-BBB-2", "BaseTen")))]
        self.assertEqual(self.run_council(self.ocfg(seats)), 0)
        for sid, prov, gid in (("a", "GMICloud", "gen-AAA-1"), ("b", "BaseTen", "gen-BBB-2")):
            rec = self.seat_json(sid)
            self.assertEqual(rec["status"], "ok", rec)
            self.assertEqual((rec["serving_provider"], rec["generation_id"]), (prov, gid))
            row = self.by_id(self.summary())[sid]
            self.assertEqual((row["serving_provider"], row["generation_id"]), (prov, gid), "summary.json too")
        ends = {c["seat"]: c for c in self.calls()}
        self.assertEqual({k: (c["call"], c["status"], c["serving_provider"], c["generation_id"])
                          for k, c in ends.items()},
                         {"a": ("original", "ok", "GMICloud", "gen-AAA-1"),
                          "b": ("original", "ok", "BaseTen", "gen-BBB-2")}, "calls.jsonl end record per call")
        self.assertIn("exit_code", ends["a"])
        self.assertEqual(self.summary()["chair"]["status"], "ok")
        self.assertNotIn("serving_provider", self.summary()["chair"], "the stub chair is not an OpenRouter call")

    def test_02_stream_without_provider_or_id_gives_null(self):
        seats = [self.oseat("a", self.sse("a", stream(None, None)))]
        self.assertEqual(self.run_council(self.ocfg(seats), ), 0)
        rec = self.seat_json("a")
        self.assertIn("serving_provider", rec)
        self.assertIsNone(rec["serving_provider"])
        self.assertIsNone(rec["generation_id"])
        end = self.calls()[0]
        self.assertEqual((end["serving_provider"], end["generation_id"]), (None, None))

    def test_03_id_without_provider_keeps_only_what_the_stream_carried(self):
        seats = [self.oseat("a", self.sse("a", stream("gen-ONLY-ID", None)))]
        self.run_council(self.ocfg(seats))
        rec = self.seat_json("a")
        self.assertEqual((rec["serving_provider"], rec["generation_id"]), (None, "gen-ONLY-ID"))

    def test_04_non_openrouter_calls_carry_no_keys(self):
        seats = [hseat("p", "quick", provider="plainstub")]
        self.assertEqual(self.run_council(self.ocfg(seats)), 0)
        rec = self.seat_json("p")
        self.assertNotIn("serving_provider", rec)
        self.assertNotIn("generation_id", rec)
        self.assertEqual(self.calls(), [], "no hedge, no fallback, no OpenRouter call: no calls.jsonl")

    def test_05_salvage_records_its_own_call_not_the_parents(self):
        # the nemotron end: finish_reason error with reasoning only, then one salvage call on the same host
        seats = [self.oseat("n", self.sse("n", stream("gen-ORIG-1", "BaseTen", ok=False)),
                            stub_salvage_mode=self.sse("n.salvage", stream("gen-SALV-2", "Fireworks")))]
        cfg = self.ocfg(seats)
        self.assertEqual(self.run_council(cfg), 0)
        rec = self.seat_json("n")
        self.assertEqual((rec["status"], rec["detail"], rec["salvaged_from"]), ("ok", "salvaged", "adapter_error"))
        self.assertEqual((rec["serving_provider"], rec["generation_id"]), ("BaseTen", "gen-ORIG-1"),
                         "the original's own stream")
        sv = rec["salvage"]
        self.assertEqual(sv["status"], "ok", sv)
        self.assertEqual((sv["serving_provider"], sv["generation_id"]), ("Fireworks", "gen-SALV-2"))
        ends = [(c["call"], c["status"], c["serving_provider"], c["generation_id"]) for c in self.calls()]
        self.assertEqual(ends, [("original", "adapter_error", "BaseTen", "gen-ORIG-1"),
                                ("salvage", "ok", "Fireworks", "gen-SALV-2")])

    def test_06_fallback_records_its_own_call(self):
        seats = [self.oseat("f", self.sse("f", stream("gen-ORIG-1", "BaseTen", ok=False)),
                            stub_salvage_mode=self.sse("f.salvage", stream("gen-SALV-2", "Fireworks", ok=False)),
                            stub_fallback_mode=self.sse("f.fb", stream("gen-FALL-3", "Together")))]
        cfg = self.ocfg(seats, fallback=True)
        self.assertEqual(self.run_council(cfg), 0)
        rec = self.seat_json("f")
        self.assertEqual((rec["status"], rec["detail"], rec["answered_by"]), ("ok", "fallback", "fallback"))
        self.assertEqual((rec["serving_provider"], rec["generation_id"]), ("BaseTen", "gen-ORIG-1"))
        self.assertEqual((rec["salvage"]["serving_provider"], rec["salvage"]["generation_id"]),
                         ("Fireworks", "gen-SALV-2"))
        by = {c["call"]: c for c in rec["calls"]}
        self.assertEqual(sorted(by), ["fallback", "original"])
        self.assertEqual((by["fallback"]["serving_provider"], by["fallback"]["generation_id"]),
                         ("Together", "gen-FALL-3"))
        self.assertEqual((by["original"]["serving_provider"], by["original"]["generation_id"]),
                         ("BaseTen", "gen-ORIG-1"))
        ends = {c["call"]: c for c in self.calls() if "status" in c}
        self.assertEqual({k: (c["status"], c["serving_provider"], c["generation_id"]) for k, c in ends.items()},
                         {"original": ("adapter_error", "BaseTen", "gen-ORIG-1"),
                          "salvage": ("adapter_error", "Fireworks", "gen-SALV-2"),
                          "fallback": ("ok", "Together", "gen-FALL-3")})
        start = [c for c in self.calls() if c["call"] == "fallback" and "status" not in c]
        self.assertEqual(len(start), 1)
        self.assertNotIn("serving_provider", start[0], "the start record is written before anything streamed")

    def test_07_restart_filter_still_ignores_the_new_original_and_salvage_records(self):
        path = os.path.join(self.tmp, "calls.jsonl")
        recs = [{"seat": "n", "call": "original", "status": "ok", "serving_provider": "X", "generation_id": "g"},
                {"seat": "n", "call": "salvage", "status": "ok", "serving_provider": "Y", "generation_id": "h"},
                {"seat": "n", "call": "fallback", "tag": "t", "started_at": "now"}]
        with open(path, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
        got = council_run._read_calls(path)
        prior = [r for r in got if r.get("call") in ("hedge", "fallback") and "status" not in r]
        self.assertEqual([r["call"] for r in prior], ["fallback"])


class UnitTests(unittest.TestCase):
    def task(self, call, text, provider="orstub"):
        t = council_run._Task("a~" + call, call, {"id": "a", "provider": provider, "model": MODEL}, None)
        t.extractor = providers.OpenRouterExtractor()
        t.extractor.feed(text.encode())
        return t

    def test_10_hedge_entry_carries_its_own_ids(self):
        # OpenRouter is stream mode, so [hedge] never applies to it in a real run; the entry builder is generic
        hedge = self.task("hedge", stream("gen-HEDGE-9", "Chutes"))
        e = council_run._call_entry(hedge, None)
        self.assertEqual((e["call"], e["serving_provider"], e["generation_id"]), ("hedge", "Chutes", "gen-HEDGE-9"))

    def test_11_a_cut_stream_keeps_what_arrived(self):
        full = stream("gen-CUT-1", "BaseTen")
        first = full.index("data:")
        cut = full.index("\n\n", first) + 2 + 40            # the first chunk whole, the second torn mid-line
        t = self.task("seat", full[:cut])
        self.assertEqual(council_run._or_ids(t), {"serving_provider": "BaseTen", "generation_id": "gen-CUT-1"})

    def test_12_nothing_streamed_is_null_not_absent(self):
        t = self.task("seat", "")
        self.assertEqual(council_run._or_ids(t), {"serving_provider": None, "generation_id": None})

    def test_13_no_extractor_or_other_extractor_gives_no_keys(self):
        t = council_run._Task("a", "seat", {"id": "a", "provider": "orstub"}, None)
        self.assertEqual(council_run._or_ids(t), {})
        t.extractor = providers.RawTextExtractor("m")
        self.assertEqual(council_run._or_ids(t), {})

    def test_14_values_are_verbatim_strings_and_capped(self):
        t = self.task("seat", stream("gen-" + "x" * 5000, "GMICloud"))
        ids = council_run._or_ids(t)
        self.assertEqual(ids["serving_provider"], "GMICloud", "case kept as the stream wrote it")
        self.assertLessEqual(len(ids["generation_id"]), council_run._ERR_MAX)
        self.assertIsInstance(json.dumps(ids), str)


# ---------------------------------------------------------------- P26: per-call speed and endpoint quantization
ENDPOINTS = [  # made-up snapshot in the `GET /models/<slug>/endpoints` shape
    {"provider_name": "GMICloud", "tag": "gmicloud/fp8", "quantization": "fp8"},
    {"provider_name": "BaseTen", "tag": "baseten/fp4", "quantization": "fp4"},
    {"provider_name": "Chutes", "tag": "chutes/fp8", "quantization": "fp8"},
    {"provider_name": "Chutes", "tag": "chutes/bf16", "quantization": "bf16"},
    {"provider_name": "Wafer", "tag": "wafer", "quantization": "unknown"},
    {"provider_name": "Novita", "tag": "novita"},
]


def no_usage(gen_id, provider):
    """A stream whose last chunk carries no `usage`: the call gives no output token count."""
    return "".join([chunk(gen_id, provider, {"content": "ANSWER 4"}),
                    chunk(gen_id, provider, {"content": ""}, finish="stop"), "data: [DONE]\n\n"])


class CacheGuard:
    """Clears the module-global endpoints cache around each test (other tests may leave entries) and records any
    attempt at a request: the P26 fields must come from what the preflight already fetched, never a new call."""
    def guard(self):
        saved = dict(providers._OR_CACHE)
        providers._OR_CACHE.clear()

        def restore():
            providers._OR_CACHE.clear()
            providers._OR_CACHE.update(saved)
        self.addCleanup(restore)
        self.requests = []
        import urllib.request
        from unittest import mock
        for target, name in ((providers, "openrouter_get"), (urllib.request, "urlopen")):
            p = mock.patch.object(target, name, lambda *a, _n=name, **k: self.requests.append(_n) or (None, None))
            p.start()
            self.addCleanup(p.stop)

    def cache(self, eps=ENDPOINTS, model=MODEL, status=200):
        providers._OR_CACHE[("https://openrouter.ai/api/v1", "/models/%s/endpoints" % model, "0" * 12)] = (
            status, {"data": {"id": model, "endpoints": [dict(e) for e in eps]}})


class PerfRecordTests(CacheGuard, Base):
    def setUp(self):
        super().setUp()
        self.guard()

    def ends(self):
        return {c["seat"]: c for c in self.calls() if "status" in c}

    def test_20_end_records_carry_tok_s_and_the_serving_endpoints_quantization(self):
        self.cache()
        seats = [self.oseat("a", self.sse("a", stream("gen-AAA-1", "GMICloud"))),
                 self.oseat("b", self.sse("b", stream("gen-BBB-2", "baseten")))]
        self.assertEqual(self.run_council(self.ocfg(seats)), 0)
        ends = self.ends()
        self.assertEqual({k: c["quantization"] for k, c in ends.items()}, {"a": "fp8", "b": "fp4"},
                         "matched by provider name, case-insensitively")
        for sid in ("a", "b"):
            rec, end = self.seat_json(sid), ends[sid]
            self.assertEqual(rec["usage"]["output_tokens"], 20)
            self.assertIsInstance(end["output_tok_s"], float)
            self.assertGreater(end["output_tok_s"], 0)
            self.assertAlmostEqual(end["output_tok_s"], 20 / rec["total_s"], delta=0.06 + 20 / rec["total_s"] * 0.01)
            for key in ("output_tok_s", "quantization"):
                self.assertNotIn(key, rec, "seats/<id>.json keeps its D56 fields only")
                self.assertNotIn(key, self.by_id(self.summary())[sid], "summary.json too")
        self.assertEqual(self.requests, [], "no request: the cache is read, never filled")

    def test_21_without_a_snapshot_or_a_token_count_the_fields_are_null(self):
        seats = [self.oseat("a", self.sse("a", stream("gen-AAA-1", "GMICloud"))),
                 self.oseat("b", self.sse("b", no_usage("gen-BBB-2", "BaseTen")))]
        self.assertEqual(self.run_council(self.ocfg(seats)), 0)
        ends = self.ends()
        for sid in ("a", "b"):
            self.assertIn("quantization", ends[sid])
            self.assertIsNone(ends[sid]["quantization"], "nothing cached: null, and no request to fill it")
        self.assertIsInstance(ends["a"]["output_tok_s"], float)
        self.assertIn("output_tok_s", ends["b"])
        self.assertIsNone(ends["b"]["output_tok_s"], "no reported output tokens: null, never an estimate")
        self.assertEqual(self.requests, [])

    def test_22_salvage_end_record_has_its_own_fields(self):
        self.cache()
        seats = [self.oseat("n", self.sse("n", stream("gen-ORIG-1", "BaseTen", ok=False)),
                            stub_salvage_mode=self.sse("n-salvage", stream("gen-SALV-2", "GMICloud")))]
        self.run_council(self.ocfg(seats))
        by = {c["call"]: c for c in self.calls() if "status" in c}
        self.assertEqual((by["original"]["quantization"], by["salvage"]["quantization"]), ("fp4", "fp8"))
        for call in ("original", "salvage"):
            self.assertIn("output_tok_s", by[call])

    def test_23_an_old_calls_jsonl_line_without_the_fields_still_reads(self):
        path = os.path.join(self.tmp, "calls.jsonl")
        recs = [{"seat": "n", "call": "original", "status": "ok", "serving_provider": "X", "generation_id": "g"},
                {"seat": "n", "call": "salvage", "status": "ok", "serving_provider": "Y", "generation_id": "h",
                 "output_tok_s": 93.5, "quantization": "fp8"},
                {"seat": "n", "call": "fallback", "tag": "t", "started_at": "now"}]
        with open(path, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
        got = council_run._read_calls(path)
        self.assertEqual(got, recs)
        self.assertEqual([r.get("output_tok_s") for r in got], [None, 93.5, None])
        prior = [r for r in got if r.get("call") in ("hedge", "fallback") and "status" not in r]
        self.assertEqual([r["call"] for r in prior], ["fallback"])


class PerfUnitTests(CacheGuard, unittest.TestCase):
    def setUp(self):
        self.guard()

    def task(self, text, provider="orstub", model=MODEL, total_s=2.0, out_tokens=200):
        from councillib.supervise import Result
        t = council_run._Task("a", "seat", {"id": "a", "provider": provider, "model": model}, None)
        t.extractor = providers.OpenRouterExtractor()
        t.extractor.feed(text.encode())
        if total_s is not None:
            t.result = Result("ok", 0, b"", b"", 0.1, 0.1, total_s, 10, 0, False)
        if out_tokens is not None:
            t.usage = providers.usage_dict(output_tokens=out_tokens)
        return t

    def test_30_values(self):
        self.cache()
        t = self.task(stream("gen-1", "GMICloud"))
        self.assertEqual(council_run._or_end(t), {"serving_provider": "GMICloud", "generation_id": "gen-1",
                                                  "output_tok_s": 100.0, "quantization": "fp8"})
        self.assertEqual(council_run._or_ids(t), {"serving_provider": "GMICloud", "generation_id": "gen-1"},
                         "_or_ids (seat json, summary.json) is unchanged")

    def test_31_nulls_never_guesses(self):
        self.cache()
        cases = [(dict(total_s=None), "output_tok_s"), (dict(out_tokens=None), "output_tok_s"),
                 (dict(total_s=0.0), "output_tok_s")]
        for kw, key in cases:
            with self.subTest(kw=kw):
                self.assertIsNone(council_run._or_end(self.task(stream("g", "GMICloud"), **kw))[key])
        for prov, want in (("Chutes", None), ("Novita", None), ("Wafer", "unknown"), ("Nobody", None), (None, None)):
            with self.subTest(provider=prov):
                self.assertEqual(council_run._or_end(self.task(stream("g", prov)))["quantization"], want)
        other = self.task(stream("g", "GMICloud"), model="other/model")
        self.assertIsNone(council_run._or_end(other)["quantization"], "another model's snapshot is not used")
        self.assertEqual(self.requests, [])

    def test_32_a_failed_snapshot_is_not_used(self):
        self.cache(status=404)
        self.assertIsNone(providers.cached_endpoints(MODEL))
        self.assertIsNone(council_run._or_end(self.task(stream("g", "GMICloud")))["quantization"])
        self.assertEqual(self.requests, [])

    def test_33_non_openrouter_calls_get_no_keys(self):
        t = council_run._Task("a", "seat", {"id": "a", "provider": "orstub"}, None)
        self.assertEqual(council_run._or_end(t), {})
        t.extractor = providers.RawTextExtractor("m")
        self.assertEqual(council_run._or_end(t), {})


if __name__ == "__main__":
    unittest.main()
