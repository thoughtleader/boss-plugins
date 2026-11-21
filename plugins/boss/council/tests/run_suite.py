#!/usr/bin/env python3
"""Council test runner (D51): the whole suite under a 600 s timeout, or only the slow or the fast half.

    python3 -Es ~/.claude/council/tests/run_suite.py            # everything (default), timeout 600 s
    python3 -Es ~/.claude/council/tests/run_suite.py --fast     # skip the slow stub-process classes
    python3 -Es ~/.claude/council/tests/run_suite.py --slow     # only those
    python3 -Es ~/.claude/council/tests/run_suite.py --durations   # + the slowest classes, to refresh SLOW below

Nothing is trimmed: the default run is exactly what `unittest discover -s tests -t .` runs. "Slow" means the classes
that run real stub CLI processes through council_run (supervisor, hedge, fallback, runner, replay; each case is
seconds, not milliseconds). Keep SLOW in step with --durations. Exit status: unittest's, or 124 on the timeout.
The Bash tool's own foreground limit is 600 s: start a full run with run_in_background.
"""
import argparse
import faulthandler
import os
import signal
import sys
import time
import unittest

# C-bytecode: never write __pycache__ into the plugin dir (the entry shebangs run -E, which ignores PYTHONDONTWRITEBYTECODE)
sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# module -> classes (None = every class in the module)
SLOW = {
    "tests.test_replay": None,
    "tests.test_council_run": {"TestCouncilRun", "HedgeTests", "FallbackTests", "RestartTests",
                               "QuotaAndTerminalStatusTests", "SinkTests", "HedgeOffTests", "RunnerTests"},
    "tests.test_supervise": {"SupervisorTests", "WatchModeTests"},
    "tests.test_providers": {"OpenRouterTests"},
    "tests.test_liveness_probe": {"LiveStubTests"},
    "tests.test_salvage": {"SalvageTests"},
    "tests.test_drift": {"Mutation"},
    "tests.test_answer_check": {"AnswerCheckRunTests", "FastFailWiringTests"},
}


def is_slow(test):
    cls = type(test)
    classes = SLOW.get(cls.__module__, False)
    return classes is None or (classes is not False and cls.__name__ in classes)


def flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from flatten(t)
        else:
            yield t


class Timed(unittest.TextTestResult):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.spent = {}
        self._t0 = None

    def startTest(self, test):
        self._t0 = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        key = "%s.%s" % (type(test).__module__, type(test).__name__)
        self.spent[key] = self.spent.get(key, 0.0) + time.monotonic() - self._t0
        super().stopTest(test)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--fast", action="store_true", help="skip the slow classes")
    g.add_argument("--slow", action="store_true", help="only the slow classes")
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds for the whole run (default 600)")
    ap.add_argument("--durations", action="store_true", help="print the 12 slowest classes at the end")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    def expired(_sig, _frm):
        faulthandler.dump_traceback(file=sys.stderr)
        sys.stderr.write("\nrun_suite: timed out after %g s\n" % args.timeout)
        sys.stderr.flush()
        os._exit(124)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, args.timeout)

    loaded = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)
    tests = list(flatten(loaded))
    if args.fast:
        tests = [t for t in tests if not is_slow(t)]
    elif args.slow:
        tests = [t for t in tests if is_slow(t)]
    suite = unittest.TestSuite(tests)
    t0 = time.monotonic()
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1, resultclass=Timed)
    result = runner.run(suite)
    sys.stderr.write("run_suite: %d tests in %.1f s of %g s allowed\n" % (result.testsRun, time.monotonic() - t0,
                                                                         args.timeout))
    if args.durations:
        for key, secs in sorted(result.spent.items(), key=lambda kv: -kv[1])[:12]:
            sys.stderr.write("  %7.1f s  %s%s\n" % (secs, key, "  [slow]" if is_slow_name(key) else ""))
    return 0 if result.wasSuccessful() else 1


def is_slow_name(key):
    mod, _, cls = key.rpartition(".")
    classes = SLOW.get(mod, False)
    return classes is None or (classes is not False and cls in classes)


if __name__ == "__main__":
    sys.exit(main())
