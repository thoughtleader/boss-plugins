import os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from councillib import preflight  # noqa: E402
from councillib.preflight import Check  # noqa: E402


def seq(*results):
    """A fake _free_for answering each call with the next list of (check, result, detail)."""
    calls = []

    def fake(name, seats, cfg):
        calls.append(name)
        return [Check(name, "free", c, r, d) for c, r, d in results[min(len(calls), len(results)) - 1]]
    return fake, calls


class FreeRetryTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(preflight, "FREE_RETRY_DELAY_S", 0)
        p.start()
        self.addCleanup(p.stop)

    def run_one(self, fake, cfg=None):
        with mock.patch.object(preflight, "_free_for", fake):
            return preflight._free_with_retry("agy", [], cfg or {})

    def test_a_blip_then_pass_is_a_warn_and_no_fail(self):
        fake, calls = seq([("auth", "FAIL", "catalog fetch timed out")], [("auth", "PASS", "ok")])
        rows = self.run_one(fake)
        self.assertEqual(len(calls), 2)
        self.assertFalse([r for r in rows if r.result == "FAIL"])
        warn = [r for r in rows if r.check == "retry"]
        self.assertEqual(warn[0].result, "WARN")
        self.assertIn("catalog fetch timed out", warn[0].detail)

    def test_failing_twice_stays_a_fail(self):
        fake, calls = seq([("auth", "FAIL", "x")], [("auth", "FAIL", "y")])
        rows = self.run_one(fake)
        self.assertEqual(len(calls), 2)
        self.assertEqual([r.detail for r in rows if r.result == "FAIL"], ["failed twice; y"])

    def test_a_pass_is_one_call(self):
        fake, calls = seq([("auth", "PASS", "ok")])
        self.run_one(fake)
        self.assertEqual(len(calls), 1)

    def test_no_adapter_is_not_retried(self):
        fake, calls = seq([("adapter", "FAIL", "no adapter")])
        self.run_one(fake)
        self.assertEqual(len(calls), 1)

    def test_retry_failed_false_is_one_call(self):
        fake, calls = seq([("auth", "FAIL", "x")], [("auth", "PASS", "ok")])
        self.run_one(fake, {"preflight": {"retry_failed": False}})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
