"""boss.progress/1 mirror of status.json (run example-run, row 1b).

Stub processes only: no network, no paid call. Every test points BOSS_PROGRESS_DIR at its own temp dir. Snapshots are
collected by wrapping the helper library's write_snapshot (so every one written is kept, and still written), then
checked with the library's own `validate` against plugins/boss/lib/progress.schema.json.

Run: python3 -Es -m unittest tests.test_progress_mirror   (cwd ~/.claude/council)
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import council  # noqa: E402
from councillib import council_run, preflight, progress_mirror as pm, status  # noqa: E402
from tests.test_council_run import Base, seat  # noqa: E402
from tests.test_or_fallback import FastFailBase, fseat  # noqa: E402
from tests import needs_proc  # noqa: E402

P = pm.lib()
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "lib", "progress.schema.json")   # plugins/boss/lib: the one schema copy


def schema():
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


class Registry:
    """A throwaway boss root; keeps every snapshot written through P.write_snapshot."""

    def __init__(self, tc):
        self.dir = tempfile.mkdtemp(prefix="council-boss-")
        tc.addCleanup(shutil.rmtree, self.dir, True)
        env = mock.patch.dict(os.environ, {"BOSS_PROGRESS_DIR": self.dir})
        env.start()
        tc.addCleanup(env.stop)
        self.snaps = []
        real = P.write_snapshot

        def rec(snap, base=None):
            self.snaps.append(json.loads(json.dumps(snap)))
            return real(snap, base)
        w = mock.patch.object(P, "write_snapshot", rec)
        w.start()
        tc.addCleanup(w.stop)

    def path(self, run_id):
        return os.path.join(self.dir, "runs", "council", run_id, "progress.json")

    def load(self, run_id):
        with open(self.path(run_id), encoding="utf-8") as f:
            return json.load(f)


def items_by_id(doc):
    return {it["id"]: it for it in doc["items"]}


@unittest.skipIf(P is None, "council/progress.py not found")
class ConvertTests(unittest.TestCase):
    """The pure mapping, from hand-made status snapshots."""

    def snap(self, **kw):
        s = status.early_snapshot("20261031T000000Z-x", "A title", "seats", started_epoch=1793430490.0)
        s.update(interval_s=1.0, quorum={"ok": 1, "min_ok": 3, "need": 3, "reached_at_s": None})
        s.update(kw)
        return s

    def mirror(self, out):
        return pm.Mirror(out, P)

    def seatd(self, sid, phase="done", call="original", status_="ok", calls=None, **kw):
        d = {"id": sid, "provider": "stub", "model": "m", "call": call, "retry": 0, "phase": phase, "status": status_,
             "elapsed_s": 2.0, "call_elapsed_s": 2.0, "first_byte_s": 0.1, "out_bytes": 10, "last_byte_age_s": None,
             "detail": None, "substituted": None,
             "calls": calls if calls is not None else [{"call": call, "model": "m", "phase": phase, "status": status_,
                                                         "elapsed_s": 2.0, "out_bytes": 10}]}
        d.update(kw)
        return d

    def publish(self, s, **kw):
        tmp = tempfile.mkdtemp(prefix="council-conv-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "run"))
        out = os.path.join(tmp, "run")
        with mock.patch.dict(os.environ, {"BOSS_PROGRESS_DIR": os.path.join(tmp, "boss")}):
            m = self.mirror(out)
            doc = m.publish(s)
            self.assertEqual(P.validate(doc, schema()), [])
            on_disk = P.read_snapshot(P.run_dir("council", m.run_id))
            self.assertEqual(on_disk["seq"], doc["seq"])
        return doc, out

    def test_01_running_snapshot_validates_and_maps_seats_chair_counts(self):
        s = self.snap(seats=[self.seatd("a"), self.seatd("b", "streaming", status_=None, out_bytes=5),
                             self.seatd("c", "waiting", call=None, status_=None, calls=[])])
        doc, _ = self.publish(s)
        self.assertEqual((doc["state"], doc["phase"], doc["tool"]), ("running", "seats", "council"))
        by = items_by_id(doc)
        self.assertEqual([by[k]["state"] for k in ("seat:a", "seat:b", "seat:c", "chair")],
                         ["done", "running", "queued", "queued"])
        self.assertEqual(by["seat:a/original"]["parent"], "seat:a")
        self.assertEqual(doc["counts"][0], {"unit": "seats", "label": "seats ok", "total": 3, "done": 1, "running": 1,
                                            "failed": 0})
        self.assertEqual(doc["counts"][1]["unit"], "quorum")
        self.assertEqual((doc["counts"][1]["done"], doc["counts"][1]["total"]), (1, 3))
        self.assertIn("1/3 seats ok", doc["summary"])
        self.assertEqual(doc["attention"], [])
        self.assertIsNone(doc["ended_at"])
        self.assertEqual(doc["writer"]["pid"], os.getpid())
        self.assertEqual(doc["writer"]["kind"], "process")

    def test_02_stand_in_chain_is_separate_items_linked_by_replaces(self):
        calls = [{"call": "original", "model": "orig/m", "phase": "failed", "status": "exit_1", "elapsed_s": 1.0, "out_bytes": 0},
                 {"call": "fallback-reroute", "model": "orig/m", "phase": "failed", "status": "exit_1", "elapsed_s": 1.0,
                  "out_bytes": 0},
                 {"call": "stand-in", "model": "stand/in", "phase": "done", "status": "ok", "elapsed_s": 1.0, "out_bytes": 9}]
        sd = self.seatd("g", call="stand-in", calls=calls, model="stand/in",
                        substituted={"model": "stand/in", "reason": "exit_1: http 503"},
                        detail="original exit_1; fallback-reroute exit_1")
        doc, _ = self.publish(self.snap(seats=[sd]))
        by = items_by_id(doc)
        self.assertEqual((by["seat:g/original"]["attempt"], by["seat:g/original"].get("replaces")), (1, None))
        self.assertEqual(by["seat:g/fallback-reroute"]["replaces"], "seat:g/original")
        si = by["seat:g/stand-in"]
        self.assertEqual((si["kind"], si["replaces"], si["attempt"], si["model"], si["state"]),
                         ("stand-in", "seat:g/fallback-reroute", 3, "stand/in", "done"))
        self.assertIn("stand-in for g", si["detail"])
        self.assertEqual(by["seat:g"]["meta"]["substituted"]["model"], "stand/in")
        self.assertEqual(by["seat:g/original"]["state"], "failed")

    def test_03_terminal_states_and_ended_at(self):
        ok = self.snap(phase="done", ended=True, exit_code=0, quorum={"ok": 3, "min_ok": 3, "need": 3, "reached_at_s": 2.0},
                       seats=[self.seatd("a")], chair={"provider": "stub", "model": "m", "call": "original", "retry": 0,
                                                       "phase": "done", "status": "ok", "elapsed_s": 1.0,
                                                       "first_byte_s": 0.1, "out_bytes": 4, "last_byte_age_s": None,
                                                       "detail": None})
        doc, _ = self.publish(ok)
        self.assertEqual((doc["state"], doc["phase"]), ("done", "done"))
        self.assertTrue(doc["ended_at"])
        self.assertEqual(doc["attention"], [])
        sig = self.snap(phase="failed", ended=True, exit_code=143, seats=[self.seatd("a", "failed", status_="idle_kill")])
        doc, _ = self.publish(sig)
        self.assertEqual(doc["state"], "cancelled")
        self.assertEqual(items_by_id(doc)["chair"]["state"], "skipped")
        sup = self.snap(phase="done", ended=False, exit_code=0, seats=[self.seatd("a")], late_pending=["a/hedge"])
        doc, _ = self.publish(sup)
        self.assertEqual((doc["state"], doc["phase"], doc["ended_at"]), ("done", "supervising", None))
        self.assertTrue(doc["ext"]["council"]["supervising"])

    def test_04_chair_failed_and_quorum_not_met_raise_attention(self):
        chair = {"provider": "stub", "model": "m", "call": "original", "retry": 0, "phase": "failed", "status": "exit_1",
                 "elapsed_s": 1.0, "first_byte_s": None, "out_bytes": 0, "last_byte_age_s": None, "detail": None}
        doc, _ = self.publish(self.snap(phase="failed", ended=True, exit_code=4, chair=chair,
                                        quorum={"ok": 2, "min_ok": 2, "need": 2, "reached_at_s": 1.0},
                                        seats=[self.seatd("a"), self.seatd("b")]))
        self.assertEqual([a["id"] for a in doc["attention"]], ["chair-failed"])
        self.assertEqual(doc["attention"][0]["kind"], "error")
        doc, _ = self.publish(self.snap(phase="failed", ended=True, exit_code=3,
                                        seats=[self.seatd("a", "failed", status_="exit_1"), self.seatd("b")]))
        self.assertEqual([a["id"] for a in doc["attention"]], ["quorum-not-met"])
        self.assertIn("1 of 3", doc["attention"][0]["text"])

    def test_05_events_follow_state_changes_and_seq_matches_the_log(self):
        tmp = tempfile.mkdtemp(prefix="council-conv-")
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "run")
        os.makedirs(out)
        with mock.patch.dict(os.environ, {"BOSS_PROGRESS_DIR": os.path.join(tmp, "boss")}):
            m = self.mirror(out)
            d1 = m.publish(self.snap(seats=[self.seatd("a", "streaming", status_=None)]))
            time.sleep(0.01)   # the stamp has millisecond resolution: two publishes in one ms would compare equal on a fast host
            d2 = m.publish(self.snap(seats=[self.seatd("a", "streaming", status_=None, out_bytes=99)]))   # bytes only
            d3 = m.publish(self.snap(seats=[self.seatd("a")]))
            self.assertEqual(d1["seq"], d2["seq"])                     # no state change, no event
            self.assertGreater(d3["seq"], d2["seq"])
            with open(os.path.join(out, "events.jsonl")) as f:
                lines = [json.loads(x) for x in f]
            self.assertEqual(len(lines), d3["seq"])
            self.assertEqual([x["seq"] for x in lines], list(range(1, d3["seq"] + 1)))
            self.assertEqual((lines[-1]["kind"], lines[-1]["state"]), ("item", "done"))
            # last_progress_at moves with bytes, not with a pure heartbeat
            self.assertNotEqual(d1["last_progress_at"], d2["last_progress_at"])
            d4 = m.publish(self.snap(seats=[self.seatd("a")]))
            self.assertEqual(d3["last_progress_at"], d4["last_progress_at"])
            # a fresh process (new Mirror) continues the numbering from the log
            m2 = self.mirror(out)
            d5 = m2.publish(self.snap(phase="failed", ended=True, exit_code=3, seats=[self.seatd("a")]))
            self.assertGreater(d5["seq"], d4["seq"])

    def test_06_item_timestamps_stay_stable_across_writes(self):
        tmp = tempfile.mkdtemp(prefix="council-conv-")
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "run")
        os.makedirs(out)
        with mock.patch.dict(os.environ, {"BOSS_PROGRESS_DIR": os.path.join(tmp, "boss")}):
            m = self.mirror(out)
            a = items_by_id(m.publish(self.snap(seats=[self.seatd("a", "streaming", status_=None)])))["seat:a"]
            b = items_by_id(m.publish(self.snap(seats=[self.seatd("a", "streaming", status_=None, elapsed_s=3.0)])))["seat:a"]
            self.assertEqual(a["started_at"], b["started_at"])
            c = items_by_id(m.publish(self.snap(seats=[self.seatd("a")])))["seat:a"]
            self.assertTrue(c["ended_at"])
            d = items_by_id(m.publish(self.snap(seats=[self.seatd("a")])))["seat:a"]
            self.assertEqual(c["ended_at"], d["ended_at"])

    def test_06b_a_stale_mirror_in_another_process_rebuilds_from_disk(self):
        """The launcher keeps its preflight-era mirror; the runner then writes many events. A late launcher write
        (crash guard) must continue the numbering, never restart it."""
        tmp = tempfile.mkdtemp(prefix="council-conv-")
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "run")
        os.makedirs(out)
        with mock.patch.dict(os.environ, {"BOSS_PROGRESS_DIR": os.path.join(tmp, "boss")}), \
                mock.patch.dict(pm._MIRRORS, {}, clear=True):
            pm.publish(out, self.snap(phase="preflight"))                       # the launcher's write
            launcher = pm._MIRRORS[os.path.abspath(out)]
            runner = self.mirror(out)                                           # the runner: a separate Mirror
            last = None
            for i in range(4):
                last = runner.publish(self.snap(seats=[self.seatd("s%d" % i)]))
            pm.publish(out, self.snap(phase="failed", ended=True, exit_code=2))  # the launcher's crash-guard write
            doc = P.read_snapshot(P.run_dir("council", launcher.run_id))
            self.assertEqual(doc["state"], "failed")
            self.assertGreater(doc["seq"], last["seq"])
            with open(os.path.join(out, "events.jsonl")) as f:
                seqs = [json.loads(x)["seq"] for x in f]
            self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
            self.assertEqual(doc["seq"], len(seqs))

    def test_07_text_is_capped_and_ids_are_made_safe(self):
        s = self.snap(seats=[self.seatd("a", "failed", status_="x" * 5000)], title="t" * 500)
        s["run_id"] = "bad id/..".strip()
        doc, out = self.publish(s)
        self.assertEqual(P.validate(doc, schema()), [])
        self.assertLessEqual(len(doc["title"]), 200)
        self.assertRegex(doc["run_id"], r"^[A-Za-z0-9._-]{1,128}$")

    @needs_proc
    def test_08_find_owner_walks_to_a_session_file_and_never_opens_a_key(self):
        tmp = tempfile.mkdtemp(prefix="council-sessions-")
        self.addCleanup(shutil.rmtree, tmp, True)
        ppid = os.getppid()
        with open(os.path.join(tmp, "%d.json" % ppid), "w") as f:
            json.dump({"pid": ppid, "sessionId": "sess-1", "status": "busy", "secret": "x"}, f)
        key = os.path.join(tmp, "%d.1234.key" % ppid)
        with open(key, "w") as f:
            f.write("k")
        os.chmod(key, 0)
        o = pm.find_owner(P, sessions_dir=tmp, env={})
        self.assertEqual((o["session_id"], o["pid"]), ("sess-1", ppid))
        self.assertNotIn("secret", o)
        self.assertIsNone(pm.find_owner(P, sessions_dir=os.path.join(tmp, "none"), env={}))
        o = pm.find_owner(P, sessions_dir=os.path.join(tmp, "none"), env={"CLAUDE_CODE_SESSION_ID": "from-env"})
        self.assertEqual((o["session_id"], o["pid"]), ("from-env", None))


@unittest.skipIf(P is None, "council/progress.py not found")
class RunProgressTests(Base):
    def cfg2(self, seats, **kw):
        cfg = self.cfg(seats, **kw)
        cfg["status"] = {"enabled": True, "interval_s": 0.5}
        return cfg

    def setUp(self):
        super().setUp()
        self.reg = Registry(self)
        self.run_id = os.path.basename(self.out)

    def check_all_valid(self):
        self.assertTrue(self.reg.snaps)
        for s in self.reg.snaps:
            self.assertEqual(P.validate(s, schema()), [], s["summary"])

    def test_10_a_done_run_publishes_valid_snapshots_and_ends_done(self):
        code = self.run_council(self.cfg2([seat("a", "slow-stream 2"), seat("b", "slow-stream 2"), seat("c")]))
        self.assertEqual(code, 0)
        self.check_all_valid()
        first, last = self.reg.snaps[0], self.reg.load(self.run_id)
        self.assertEqual((first["state"], first["tool"]), ("running", "council"))
        self.assertEqual((last["state"], last["phase"]), ("done", "done"))
        self.assertTrue(last["ended_at"])
        self.assertEqual(last["writer"]["pid"], os.getpid())
        self.assertEqual(last["writer"]["start_ticks"], P.proc_start_ticks(os.getpid()))
        self.assertEqual(last["writer"]["boot_id"], P.boot_id())
        self.assertEqual(items_by_id(last)["chair"]["state"], "done")
        self.assertEqual(P.liveness(last)["status"], "ended")
        seqs = [s["seq"] for s in self.reg.snaps]
        self.assertEqual(seqs, sorted(seqs))                               # never goes down
        with open(os.path.join(self.out, "events.jsonl")) as f:
            self.assertEqual(sum(1 for _ in f), last["seq"])
        st = status.load(self.out)                                         # status.json is unchanged in kind
        self.assertEqual((st["phase"], st["ended"], st["exit_code"]), ("done", True, 0))
        self.assertEqual(last["ext"]["council"]["status_json"], os.path.join(self.out, "status.json"))
        self.assertEqual(oct(os.stat(self.reg.path(self.run_id)).st_mode & 0o777), "0o600")

    def test_11_a_run_below_quorum_ends_failed_with_attention(self):
        code = self.run_council(self.cfg2([seat("a", "exit 3"), seat("b", "exit 3"), seat("c", "quick")], min_ok="n"))
        self.assertEqual(code, 3)
        self.check_all_valid()
        last = self.reg.load(self.run_id)
        self.assertEqual(last["state"], "failed")
        self.assertEqual([a["id"] for a in last["attention"]], ["quorum-not-met"])
        self.assertEqual(items_by_id(last)["chair"]["state"], "skipped")
        self.assertEqual(items_by_id(last)["seat:a"]["state"], "failed")
        self.assertEqual(P.liveness(last)["status"], "ended")

    def test_12_a_failed_chair_ends_failed_with_attention(self):
        code = self.run_council(self.cfg2([seat("a"), seat("b")], chair_mode="exit 1"))
        self.assertEqual(code, 4)
        last = self.reg.load(self.run_id)
        self.check_all_valid()
        self.assertEqual(last["state"], "failed")
        self.assertIn("chair-failed", [a["id"] for a in last["attention"]])
        self.assertEqual(items_by_id(last)["chair"]["state"], "failed")

    def test_13_a_crash_closes_the_snapshot_failed(self):
        os.makedirs(self.out)
        status.write_final(self.out, status.early_snapshot(self.run_id, "t", "seats"))
        self.assertEqual(self.reg.load(self.run_id)["state"], "running")
        with mock.patch.object(council_run, "_run", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                council_run.run(self.cfg2([seat("a")]), "q", [], self.out)
        last = self.reg.load(self.run_id)
        self.assertEqual((last["state"], last["phase"]), ("failed", "failed"))
        self.assertTrue(last["ended_at"])

    def test_14_an_early_exit_before_any_launch_is_terminal(self):
        cfg = self.cfg2([seat("a"), seat("b"), seat("c")])
        with mock.patch.object(council_run, "providers") as prov:
            prov.quota_unavailable.return_value = {"stub": "2026-11-01"}
            self.assertEqual(self.run_council(cfg), 3)
        last = self.reg.load(self.run_id)
        self.assertEqual(last["state"], "failed")
        self.assertEqual({it["state"] for it in last["items"] if it["kind"] == "seat"}, {"skipped"})

    def test_15_disabled_progress_or_status_publishes_nothing_and_the_run_is_unaffected(self):
        cfg = self.cfg2([seat("a"), seat("b"), seat("c")])
        cfg["status"] = {"enabled": True, "interval_s": 0.5, "progress": False}
        self.assertEqual(self.run_council(cfg), 0)
        self.assertFalse(os.path.exists(os.path.join(self.reg.dir, "runs")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "status.json")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "events.jsonl")))

    def test_16_an_unwritable_registry_never_fails_the_run(self):
        err = io.StringIO()
        pm._NOTED["done"] = False
        with mock.patch.object(P, "write_snapshot", side_effect=OSError("read-only")), mock.patch.object(sys, "stderr", err):
            code = self.run_council(self.cfg2([seat("a"), seat("b"), seat("c")]))
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue().count("progress: cannot write"), 1)
        self.assertEqual(status.load(self.out)["phase"], "done")


@unittest.skipIf(P is None, "council/progress.py not found")
class ChainProgressTests(FastFailBase):
    def test_20_original_fallback_stand_in_are_items_with_replaces(self):
        reg = Registry(self)
        seats = [seat("a"), seat("b"), fseat("f", stub_original="http503", stub_fallback="http503", stub_standin="ok",
                                             fallback_model="stand/in-model", fallback_reasoning_effort="medium")]
        cfg = self.fcfg(seats, min_ok="n")
        cfg["status"] = {"enabled": True, "interval_s": 0.5}
        with mock.patch.object(status, "MIN_GAP_S", 0):
            self.assertEqual(self.run_council(cfg), 0)
        run_id = os.path.basename(self.out)
        for s in reg.snaps:
            self.assertEqual(P.validate(s, schema()), [])
        last = reg.load(run_id)
        by = items_by_id(last)
        self.assertEqual(by["seat:f/fallback-reroute"]["replaces"], "seat:f/original")
        self.assertEqual(by["seat:f/stand-in"]["replaces"], "seat:f/fallback-reroute")
        self.assertEqual((by["seat:f/stand-in"]["kind"], by["seat:f/stand-in"]["model"]), ("stand-in", "stand/in-model"))
        self.assertEqual(by["seat:f/original"]["state"], "failed")
        self.assertEqual(by["seat:f"]["state"], "done")
        # while the chain was running, the stand-in item existed before the run ended
        mid = [s for s in reg.snaps if s["state"] == "running" and "seat:f/stand-in" in items_by_id(s)]
        self.assertTrue(mid)
        with open(os.path.join(self.out, "events.jsonl")) as f:
            ev = [json.loads(x) for x in f]
        self.assertTrue(any(e.get("id") == "seat:f/stand-in" and e["state"] == "done" for e in ev))


@unittest.skipIf(P is None, "council/progress.py not found")
class LauncherProgressTests(unittest.TestCase):
    def setUp(self):
        self.reg = Registry(self)
        self.tmp = tempfile.mkdtemp(prefix="council-progress-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.overlay = os.path.join(self.tmp, "ov.toml")
        with open(self.overlay, "w") as f:
            f.write('state_dir = "%s"\n' % self.state)

    def run_cmd(self, **pf):
        seen = {}

        def fake_preflight(*a, **kw):
            runs = os.path.join(self.state, "runs")
            seen["run_id"] = os.listdir(runs)[0]
            seen["during"] = self.reg.load(seen["run_id"])
            if "raises" in pf:
                raise pf["raises"]
            return pf["result"]
        with mock.patch.object(preflight, "run", fake_preflight), \
                mock.patch.object(preflight, "format_table", lambda c, a: "TABLE\n"), \
                mock.patch.object(preflight, "to_json", lambda c, a: "{}"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = council.main(["run", "--config", self.overlay, "--", "Is the preflight visible?"])
        return rc, seen

    def test_30_preflight_phase_is_published_and_a_failed_preflight_is_terminal(self):
        rc, seen = self.run_cmd(result=(False, []))
        self.assertEqual(rc, 1)
        self.assertEqual((seen["during"]["state"], seen["during"]["phase"]), ("running", "preflight"))
        self.assertEqual(P.validate(seen["during"], schema()), [])
        last = self.reg.load(seen["run_id"])
        self.assertEqual((last["state"], last["phase"]), ("failed", "failed"))
        self.assertIn("preflight failed", last["summary"])
        self.assertTrue(last["ended_at"])

    def test_31_a_policy_failure_is_terminal_even_though_the_run_dir_is_removed(self):
        rc, seen = self.run_cmd(raises=preflight.PolicyError(["bad"]))
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(os.path.join(self.state, "runs")))
        last = self.reg.load(seen["run_id"])
        self.assertEqual(last["state"], "failed")
        self.assertIn("refused by policy", last["summary"])
        self.assertEqual(P.validate(last, schema()), [])
        self.assertEqual(P.liveness(last)["status"], "ended")

    def test_32_a_raising_preflight_is_terminal(self):
        with self.assertRaises(RuntimeError):
            self.run_cmd(raises=RuntimeError("boom"))
        run_id = os.listdir(os.path.join(self.reg.dir, "runs", "council"))[0]
        self.assertEqual(self.reg.load(run_id)["state"], "failed")


if __name__ == "__main__":
    unittest.main()
