"""Seat child tests (D43 seat-child half): `--provider-json` becomes the request's `provider` block.
No network, no paid call: urlopen is replaced by a fake that records the request.

Run: python3 -Es -m unittest discover -s ~/.claude/council/tests -t ~/.claude/council
"""
import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import config, openrouter_seat, providers  # noqa: E402
from councillib.supervise import Limits  # noqa: E402

DONE = (b'data: {"id":"gen-1","model":"m","choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n")


class FakeResp:
    status = 200

    def __init__(self, data=DONE):
        self._io = io.BytesIO(data)

    def readline(self):
        return self._io.readline()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeStdin:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


def run_child(argv, stdin=b"hello"):
    """-> (exit code, [recorded Requests], stderr text). urlopen and stdin are faked."""
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return FakeResp()

    err = io.StringIO()
    with mock.patch.object(openrouter_seat.urllib.request, "urlopen", fake_urlopen), \
            mock.patch.object(openrouter_seat.sys, "stdin", FakeStdin(stdin)), \
            mock.patch.object(openrouter_seat.sys, "stdout", mock.Mock(buffer=io.BytesIO())), \
            mock.patch.object(openrouter_seat.sys, "stderr", err), \
            mock.patch.dict(os.environ, {"TEST_OR_KEY": "k-test"}):
        code = openrouter_seat.main(argv)
    return code, seen, err.getvalue()


BASE = ["--model", "m", "--max-tokens", "8000", "--reasoning-effort", "medium", "--key-env", "TEST_OR_KEY"]


class SeatChildTests(unittest.TestCase):
    def test_01_provider_block_goes_into_the_request_body(self):
        prefs = {"require_parameters": True, "order": ["gmicloud"]}
        code, seen, _ = run_child(BASE + ["--provider-json", json.dumps(prefs)])
        self.assertEqual(code, 0)
        body = json.loads(seen[0].data)
        self.assertEqual(body["provider"], prefs)
        self.assertIs(body["provider"]["require_parameters"], True)
        self.assertEqual(body["provider"]["order"], ["gmicloud"])
        # nothing else changes
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["max_tokens"], 8000)
        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertIs(body["stream"], True)
        self.assertEqual(body["messages"], [{"role": "user", "content": "hello"}])

    def test_02_no_flag_means_no_provider_key_and_the_same_body_as_before(self):
        code, seen, _ = run_child(BASE)
        self.assertEqual(code, 0)
        body = json.loads(seen[0].data)
        self.assertNotIn("provider", body)
        self.assertEqual(sorted(body), ["max_tokens", "messages", "model", "reasoning", "stream"])

    def test_03_bad_json_fails_fast_before_any_http_call(self):
        for bad in ("{not json", "", "[1,2]", '"gmicloud"', "42", "null", "true"):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as cm:
                    run_child(BASE + ["--provider-json", bad])
                self.assertEqual(cm.exception.code, 2)

    def test_04_bad_json_makes_no_http_call_and_never_echoes_the_value(self):
        called = []
        err = io.StringIO()
        with mock.patch.object(openrouter_seat.urllib.request, "urlopen", lambda *a, **k: called.append(a)), \
                mock.patch.object(openrouter_seat.sys, "stderr", err), \
                mock.patch.dict(os.environ, {"TEST_OR_KEY": "k-test"}):
            with self.assertRaises(SystemExit) as cm:
                openrouter_seat.main(BASE + ["--provider-json", "SECRET-NOT-JSON"])
        self.assertEqual((cm.exception.code, called), (2, []))
        self.assertNotIn("SECRET-NOT-JSON", err.getvalue())
        self.assertIn("--provider-json", err.getvalue())

    def test_05_build_body_takes_the_parsed_block(self):
        args = openrouter_seat.parse_args(["--model", "m", "--provider-json", '{"order":["baseten"]}'])
        self.assertEqual(openrouter_seat.build_body(args, "q")["provider"], {"order": ["baseten"]})
        args = openrouter_seat.parse_args(["--model", "m"])
        self.assertNotIn("provider", openrouter_seat.build_body(args, "q"))


class ArgvTests(unittest.TestCase):
    """providers.OpenRouterAdapter.build puts the seat's resolved prefs into argv for the shipped config."""

    def setUp(self):
        self.cfg = config.load()
        self.lim = Limits(mode="stream", idle_s=240, ceiling_s=1200.0, deadline_s=None)

    def argv(self, seat, cfg=None):
        return providers.get("openrouter").build(seat, "/p/prompt.md", "/w", self.lim, cfg or self.cfg).argv

    def seats(self):
        return {s["id"]: s for s in self.cfg["seat"] if s.get("provider") == "openrouter"}

    def test_10_both_shipped_openrouter_seats_carry_their_prefs(self):
        seats = self.seats()
        self.assertEqual(sorted(seats), ["deepseek", "openrouter"])
        for sid in seats:
            with self.subTest(seat=sid):
                a = self.argv(seats[sid])
                blk = json.loads(a[a.index("--provider-json") + 1])
                self.assertEqual(blk, {"require_parameters": True})
                self.assertEqual(blk, providers.routing_prefs(self.cfg, seats[sid]))
                # the same block reaches the request body through the child's own parser
                body = openrouter_seat.build_body(openrouter_seat.parse_args(a[3:]), "q")
                self.assertEqual(body["provider"], blk)
                self.assertIs(body["provider"]["require_parameters"], True)
                self.assertNotIn("only", body["provider"])
                self.assertNotIn("allow_fallbacks", body["provider"])

    def test_11_seat_model_identity_stays_fixed(self):
        for sid, s in self.seats().items():
            a = self.argv(s)
            self.assertEqual(a[a.index("--model") + 1], s["model"])
            self.assertNotIn("models", json.loads(a[a.index("--provider-json") + 1]))

    def test_12_no_prefs_table_leaves_argv_unchanged(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["providers"]["openrouter"].pop("provider", None)
        seat = {"id": "x", "provider": "openrouter", "model": "z-ai/glm-5"}
        a = self.argv(seat, cfg)
        self.assertNotIn("--provider-json", a)
        with_table = self.argv(seat)
        self.assertEqual(with_table[with_table.index("--provider-json") + 2:], a[with_table.index("--provider-json"):])
        self.assertEqual([x for i, x in enumerate(with_table)
                          if i not in (with_table.index("--provider-json"), with_table.index("--provider-json") + 1)], a)

    def test_13_provider_flag_precedes_extra_args_and_holds_no_key(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["providers"]["openrouter"]["extra_args"] = ["--zzz"]
        a = self.argv(self.seats()["openrouter"], cfg)
        self.assertLess(a.index("--provider-json"), a.index("--zzz"))
        self.assertNotIn(os.environ.get("OPENROUTER_API_KEY") or "\0", " ".join(a))

    def test_14_salvage_call_keeps_the_same_routing(self):
        s = self.seats()["openrouter"]
        sv = providers.get("openrouter").salvage_seat(s, self.cfg, 2000)
        a = self.argv(sv)
        self.assertEqual(json.loads(a[a.index("--provider-json") + 1]),
                         {"require_parameters": True})


if __name__ == "__main__":
    unittest.main()
