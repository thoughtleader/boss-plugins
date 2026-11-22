import json
import os
import stat
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import progress as P  # noqa: E402
from tests import needs_proc  # noqa: E402

PLUGIN = os.path.dirname(os.path.dirname(HERE))          # plugins/boss
SCHEMA = json.load(open(os.path.join(PLUGIN, "lib", "progress.schema.json")))
CASES = json.load(open(os.path.join(HERE, "liveness-cases.json")))


class FakeProbe:
    def __init__(self, d):
        self.d = d

    def boot_id(self):
        return self.d["boot_id"]

    def start_ticks(self, pid):
        return self.d["procs"].get(str(pid))

    def session(self, owner):
        st = self.d["sessions"].get(owner.get("session_id"))
        return None if st is None else {"pid": 1, "status": st}


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "boss")
        self._prev = os.environ.get("BOSS_PROGRESS_DIR")
        os.environ["BOSS_PROGRESS_DIR"] = self.root

    def tearDown(self):
        # restore the suite's scratch value (tests/__init__.py); popping it would send later tests to ~/.claude/boss
        if self._prev is None:
            os.environ.pop("BOSS_PROGRESS_DIR", None)
        else:
            os.environ["BOSS_PROGRESS_DIR"] = self._prev
        self.tmp.cleanup()


class TestWriter(Base):
    def snap(self, **kw):
        s = {"tool": "council", "run_id": "r1", "state": "running", "seq": 1, "items": [],
             "writer": P.writer_identity()}
        s.update(kw)
        return s

    def test_atomic_write_modes_and_no_temp_left(self):
        path = P.write_snapshot(self.snap())
        d = os.path.dirname(path)
        self.assertEqual(mode(path), 0o600)
        for p in (self.root, os.path.join(self.root, "runs"), os.path.dirname(d), d):
            self.assertEqual(mode(p), 0o700, p)
        self.assertEqual(sorted(os.listdir(d)), ["progress.json", "run.json"])
        self.assertEqual(P.read_snapshot(d)["schema"], P.SCHEMA)

    def test_reader_never_sees_partial(self):
        P.write_snapshot(self.snap())
        d = P.run_dir("council", "r1")
        stop, bad = threading.Event(), []

        def read():
            while not stop.is_set():
                if P.read_snapshot(d) is None:
                    bad.append(1)
        t = threading.Thread(target=read)
        t.start()
        for i in range(200):
            P.write_snapshot(self.snap(seq=i, summary="x" * 4000))
        stop.set()
        t.join()
        self.assertEqual(bad, [])

    def test_run_json_write_once(self):
        P.write_snapshot(self.snap(title="first"))
        P.write_snapshot(self.snap(title="second"))
        with open(os.path.join(P.run_dir("council", "r1"), "run.json")) as f:
            self.assertEqual(json.load(f)["title"], "first")

    def test_hygiene_and_caps(self):
        s = self.snap(title="a\x1b[31mred\x07" + "t" * 300,
                      items=[{"id": "i", "kind": "seat", "label": "l", "state": "failed", "error": "e" * 5000}])
        P.write_snapshot(s)
        got = P.read_snapshot(P.run_dir("council", "r1"))
        self.assertTrue(got["title"].startswith("ared"))
        self.assertEqual(len(got["title"]), 200)
        self.assertEqual(len(got["items"][0]["error"]), 2000)
        self.assertEqual(P.validate(got, SCHEMA), [])

    def test_unsafe_ids_rejected(self):
        for rid in ("../x", "a/b", "", ".."):
            with self.assertRaises(ValueError):
                P.run_dir("council", rid)
        with self.assertRaises(ValueError):
            P.run_dir("Bad Tool", "r1")

    def test_prune(self):
        P.write_snapshot(self.snap())
        P.write_snapshot(self.snap(run_id="r2"))
        old = os.path.join(P.run_dir("council", "r1"), "progress.json")
        os.utime(old, (0, 0))
        removed = P.prune()
        self.assertEqual([os.path.basename(r) for r in removed], ["r1"])
        self.assertEqual([os.path.basename(r) for r in P.list_runs()], ["r2"])

    @needs_proc
    def test_writer_identity_matches_proc(self):
        w = P.writer_identity()
        self.assertEqual(w["pid"], os.getpid())
        self.assertIsInstance(w["start_ticks"], int)
        self.assertEqual(P.proc_start_ticks(os.getpid()), w["start_ticks"])


class TestSchema(unittest.TestCase):
    def test_examples_validate(self):
        for n in ("council.json", "orchestrator.json"):
            with open(os.path.join(HERE, "fixtures", "progress-examples", n)) as f:
                self.assertEqual(P.validate(json.load(f), SCHEMA), [], n)

    def test_validator_rejects(self):
        with open(os.path.join(HERE, "fixtures", "progress-examples", "council.json")) as f:
            doc = json.load(f)
        doc["state"] = "exploded"
        doc["items"][0]["label"] = "x" * 201
        del doc["writer"]
        errs = P.validate(doc, SCHEMA)
        self.assertEqual(len(errs), 3, errs)

    def test_unknown_fields_allowed(self):
        with open(os.path.join(HERE, "fixtures", "progress-examples", "council.json")) as f:
            doc = json.load(f)
        doc["future_field"] = {"x": 1}
        doc["items"][0]["new_thing"] = 2
        self.assertEqual(P.validate(doc, SCHEMA), [])

    def test_stand_in_is_its_own_item(self):
        with open(os.path.join(HERE, "fixtures", "progress-examples", "council.json")) as f:
            items = {i["id"]: i for i in json.load(f)["items"]}
        si = items["seat:glm/stand-in"]
        self.assertEqual(si["replaces"], "seat:glm/fallback-reroute")
        self.assertEqual(si["model"], "stand/in-model")


class TestLiveness(unittest.TestCase):
    def test_shared_cases(self):
        now = P.parse_ts(CASES["now"])
        probe = FakeProbe(CASES["probe"])
        for c in CASES["cases"]:
            snap = dict(CASES["base"], **c["snap"])
            got = P.liveness(snap, now=now, probe=probe, resumed_at=P.parse_ts(c.get("resumed_at")))
            for k, v in c["expect"].items():
                self.assertEqual(got[k], v, "%s: %s" % (c["name"], got))

    @needs_proc
    def test_real_probe_on_self(self):
        snap = dict(CASES["base"], writer=P.writer_identity(), heartbeat_at=P.iso(), updated_at=P.iso())
        self.assertEqual(P.liveness(snap)["status"], "running")


class TestSessionAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        me = os.getpid()
        started_ms = int(P._proc_start_epoch(me) * 1000)
        with open(os.path.join(self.d, "%d.json" % me), "w") as f:
            json.dump({"pid": me, "sessionId": "sid-me", "startedAt": started_ms, "status": "busy"}, f)
        with open(os.path.join(self.d, "999999999.json"), "w") as f:
            json.dump({"pid": 999999999, "sessionId": "sid-dead", "startedAt": started_ms, "status": "idle"}, f)
        # pid reuse: a file naming this pid but started a day earlier
        with open(os.path.join(self.d, "1.json"), "w") as f:
            json.dump({"pid": me, "sessionId": "sid-old", "startedAt": started_ms - 86400000, "status": "idle"}, f)
        key = os.path.join(self.d, "%d.0123abcd.key" % me)
        with open(key, "w") as f:
            f.write("secret")
        os.chmod(key, 0)  # reading it would raise PermissionError

    def tearDown(self):
        self.tmp.cleanup()

    @needs_proc
    def test_lookup_by_session_id_skips_key(self):
        self.assertEqual(P.session_state({"session_id": "sid-me"}, self.d), {"pid": os.getpid(), "status": "busy"})

    @needs_proc
    def test_lookup_by_pid(self):
        self.assertEqual(P.session_state({"pid": os.getpid()}, self.d)["status"], "busy")

    @needs_proc
    def test_dead_and_reused(self):
        self.assertIsNone(P.session_state({"session_id": "sid-dead"}, self.d))
        self.assertIsNone(P.session_state({"session_id": "sid-old"}, self.d))
        self.assertIsNone(P.session_state({"session_id": "nope"}, self.d))


class TestClaim(Base):
    def test_claim_once(self):
        self.assertTrue(P.claim_notification("council", "r1", "done"))
        self.assertFalse(P.claim_notification("council", "r1", "done"))
        self.assertTrue(P.claim_notification("council", "r1", "failed"))
        self.assertEqual(mode(os.path.join(P.run_dir("council", "r1"), "claims")), 0o700)

    def test_claim_race(self):
        wins = []
        ts = [threading.Thread(target=lambda: wins.append(P.claim_notification("council", "r1", "done")))
              for _ in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(wins.count(True), 1)

    def test_claim_key_sanitized(self):
        with self.assertRaises(ValueError):
            P.claim_notification("council", "r1", "../x")


if __name__ == "__main__":
    unittest.main()
