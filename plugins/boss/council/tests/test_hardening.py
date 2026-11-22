"""First-run hardening (council round 2): clear one-line failures instead of tracebacks. Offline: stub providers and a fake
`claude` shell script only; nothing here can reach a model or the network."""
import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import council
import progress as P
from councillib import config, preflight, progress_mirror, providers
from tests import needs_proc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS = os.path.join(ROOT, "prompts")


def call(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = council.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hardening-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def overlay(self):
        path = os.path.join(self.tmp, "ov.toml")
        with open(path, "w") as f:
            f.write("\n".join([
                'prompts_dir = "%s"' % PROMPTS, 'state_dir = "%s"' % self.tmp,
                "[chair]", 'provider = "stub"', 'model = "chair-m"', 'stub_mode = "quick"',
                "[providers.stub]", 'mode = "stream"',
                "[canary]", 'prompt = "Reply with exactly: OK"', "timeout_s = 20", "idle_s = 10",
                "[quorum]", 'min_ok = "n-1"', "straggler_grace_s = 5", "[answer_check]", "min_chars = 1",
                "[[seat]]", 'id = "a"', 'provider = "stub"', 'model = "m-a"', 'stub_mode = "quick"',
                "[[seat]]", 'id = "b"', 'provider = "stub"', 'model = "m-b"', 'stub_mode = "quick"', ""]))
        return path

    def blocked_out(self):
        """An --out that no process can create, root included: a path below a regular file."""
        f = os.path.join(self.tmp, "afile")
        with open(f, "w") as fh:
            fh.write("x")
        return os.path.join(f, "runs")


class UnwritableOut(Base):
    def test_ask_refuses_before_any_canary_with_one_line(self):
        out = self.blocked_out()
        with mock.patch.object(preflight, "canary_stage", side_effect=AssertionError("canary ran")), \
                mock.patch.object(preflight, "free_stage", side_effect=AssertionError("free stage ran")):
            rc, o, err = call(["ask", "--config", self.overlay(), "--out", out, "--", "What is 2+2?"])
        self.assertEqual(rc, 2, o + err)
        self.assertNotIn("Traceback", o + err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("cannot create the run dir under %s" % out, err)

    def test_run_dir_oserror_is_one_line_and_exit_2(self):
        out = self.blocked_out()
        with mock.patch.object(preflight, "canary_stage", side_effect=AssertionError("canary ran")):
            rc, o, err = call(["run", "--config", self.overlay(), "--out", out, "--", "What is 2+2?"])
        self.assertEqual(rc, 2, o + err)
        self.assertNotIn("Traceback", o + err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("cannot create the run dir under", err)

    def test_a_writable_out_is_not_refused(self):
        rc, o, err = call(["ask", "--config", self.overlay(), "--out", os.path.join(self.tmp, "new", "runs"),
                           "--", "What is 2+2?"])
        self.assertEqual(rc, 0, o + err)

    def test_probe_names_the_unwritable_ancestor(self):
        class A:
            out = self.blocked_out()
        self.assertIn("is not a directory", council._out_problem({}, A))
        A.out = os.path.join(self.tmp, "fresh", "runs")
        self.assertIsNone(council._out_problem({}, A))


class PythonGuard(unittest.TestCase):
    """The guard runs before `import tomllib`: faked by setting sys.version_info in a child before the file loads."""
    def child(self, code):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, "-Es", "-c", code], capture_output=True, text=True, env=env, timeout=60)

    def test_council_py_prints_the_requirement_and_exits_2(self):
        code = ("import sys, runpy; sys.version_info = (3, 10, 12, 'final', 0); sys.argv = ['council.py', 'preflight']\n"
                "sys.modules['tomllib'] = None\n"                # an import of it would raise: the guard must come first
                "runpy.run_path(%r, run_name='__main__')" % os.path.join(ROOT, "council.py"))
        p = self.child(code)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("council needs Python 3.11+ (found 3.10.12)", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_config_module_prints_the_requirement_and_exits_2(self):
        code = ("import sys; sys.version_info = (3, 9, 1, 'final', 0); sys.path.insert(0, %r)\n"
                "sys.modules['tomllib'] = None\n"
                "import councillib.config" % ROOT)
        p = self.child(code)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("council needs Python 3.11+ (found 3.9.1)", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_council_probe_tool_guards_before_tomllib(self):
        code = ("import sys, runpy; sys.version_info = (3, 10, 12, 'final', 0); sys.argv = ['council-probe.py']\n"
                "sys.modules['tomllib'] = None\n"
                "runpy.run_path(%r, run_name='__main__')" % os.path.join(ROOT, "tools", "council-probe.py"))
        p = self.child(code)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("council needs Python 3.11+ (found 3.10.12)", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    ENTRIES = ("council.py", os.path.join("tools", "council-probe.py"), os.path.join("tools", "liveness-probe.py"))

    def test_shebang_is_portable_and_keeps_the_isolation_flags(self):
        """No `env -S` (GNU env older than 8.30 rejects it): a /bin/sh line 2 execs `python3 -Es`, which Python reads as a string."""
        for rel in self.ENTRIES:
            with open(os.path.join(ROOT, rel)) as f:
                self.assertEqual(f.readline().strip(), "#!/bin/sh", rel)
                self.assertEqual(f.readline().split("#")[0].strip(), "''\'\'exec python3 -Es \"$0\" \"$@\"", rel)

    def test_entry_scripts_exec_python3_with_E_and_s(self):
        """Run through the real shebang with a python3 stub first on PATH: it must be called as `python3 -Es <script> <args>`."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        stub = os.path.join(tmp, "python3")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        os.chmod(stub, 0o755)
        for rel in self.ENTRIES:
            path = os.path.join(ROOT, rel)
            p = subprocess.run([path, "--probe-arg", "x y"], capture_output=True, text=True, timeout=30,
                               env={"PATH": tmp + os.pathsep + "/usr/bin:/bin"})
            self.assertEqual(p.stdout.splitlines(), ["-Es", path, "--probe-arg", "x y"], (rel, p.stderr))


class MissingCli(Base):
    """claude, codex, agy and qwen: a CLI that is not on PATH is said so plainly and is not retried; one that exists but
    fails still is. Fake binaries are shell scripts in a temp dir that is the whole PATH."""
    NAMES = ("claude", "codex", "agy", "qwen")

    def fake(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def rows(self, name):
        with mock.patch.dict(os.environ, {"PATH": self.tmp}), mock.patch.object(preflight.time, "sleep") as sleep:
            return {c.check: c for c in preflight._free_with_retry(name, [], config.load())}, sleep

    def test_absent_binary_says_so_and_skips_the_retry_sleep(self):
        for name in self.NAMES:
            with self.subTest(name):
                by, sleep = self.rows(name)
                self.assertEqual(by["version"].result, "FAIL")
                self.assertIn("%s CLI not found on PATH" % name, by["version"].detail)
                self.assertNotIn("failed twice", by["version"].detail)
                if "auth" in by:
                    self.assertIn("not found on PATH", by["auth"].detail)
                sleep.assert_not_called()

    def test_binary_that_exists_but_fails_is_still_retried(self):
        for name in self.NAMES:
            with self.subTest(name):
                self.fake(name, "exit 1")
                by, sleep = self.rows(name)
                self.assertIn("failed twice", by["version"].detail)
                self.assertNotIn("not found on PATH", by["version"].detail)
                # time.sleep is the shared module function, so subprocess's own wait polling
                # (short sleeps from 0.001 s) can land on the mock too; count only the retry delay.
                retry = mock.call(preflight.FREE_RETRY_DELAY_S)
                self.assertEqual([c for c in sleep.call_args_list if c == retry], [retry])


class BadToml(Base):
    def test_message_names_the_file_line_and_column(self):
        bad = os.path.join(self.tmp, "bad.toml")
        with open(bad, "w") as f:
            f.write('state_dir = "x"\nkey = = 1\n')
        rc, o, err = call(["preflight", "--free-only", "--config", bad])
        self.assertEqual(rc, 2, o + err)
        self.assertIn(bad, err)
        self.assertIn("line 2", err)
        self.assertIn("column", err)
        self.assertNotIn("()", err)
        self.assertNotIn("Traceback", o + err)


class SessionsDir(Base):
    def put(self, root, pid, sid):
        d = os.path.join(root, "sessions")
        os.makedirs(d)
        with open(os.path.join(d, "%d.json" % pid), "w") as f:
            f.write('{"pid": %d, "sessionId": "%s", "status": "busy"}' % (pid, sid))

    @needs_proc
    def test_find_owner_reads_the_claude_config_dir(self):
        ppid = os.getppid()
        self.put(self.tmp, ppid, "sess-cfg")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp}):
            o = progress_mirror.find_owner(P, env={})
        self.assertEqual((o["session_id"], o["pid"]), ("sess-cfg", ppid))

    @needs_proc
    def test_find_owner_falls_back_to_home_dot_claude(self):
        ppid = os.getppid()
        home = os.path.join(self.tmp, "home")
        self.put(os.path.join(home, ".claude"), ppid, "sess-home")
        with mock.patch.dict(os.environ, {"HOME": home}), mock.patch.dict(os.environ, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            o = progress_mirror.find_owner(P, env={})
        self.assertEqual(o["session_id"], "sess-home")

    @needs_proc
    def test_session_state_reads_the_claude_config_dir(self):
        self.put(self.tmp, os.getpid(), "sid-cfg")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp}):
            self.assertEqual(P.default_sessions_dir(), os.path.join(self.tmp, "sessions"))
            st = P.session_state({"session_id": "sid-cfg", "pid": os.getpid()})
        self.assertEqual(st, {"pid": os.getpid(), "status": "busy"})


if __name__ == "__main__":
    unittest.main()
