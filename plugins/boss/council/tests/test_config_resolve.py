"""Which base config `config.load()` reads: $COUNCIL_CONFIG, then <Claude config dir>/council/config.toml, then the shipped
config.default.toml; the prompts_dir fallback; and that the shipped default is a valid, owner-free config."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import config  # noqa: E402

MINI = 'state_dir = "%s"\n[chair]\nprovider = "stub"\nmodel = "m"\n[[seat]]\nid = "mini"\nprovider = "stub"\nmodel = "m"\n'


class Resolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="council-cfg-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {k: v for k, v in os.environ.items() if k not in (config.CONFIG_ENV, "CLAUDE_CONFIG_DIR")}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")

    def write(self, path, text="x = 1\n"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def user_path(self):
        return os.path.join(self.tmp, "claude", "council", "config.toml")

    def test_01_no_user_config_falls_back_to_the_shipped_default(self):
        self.assertEqual(config.resolve_base(), (config.SHIPPED_CONFIG, "shipped default"))
        self.assertTrue(os.path.isfile(config.SHIPPED_CONFIG))
        self.assertEqual(config.load()["seat"][0]["id"], "claude")

    def test_02_user_config_in_the_claude_config_dir_wins_over_the_default(self):
        self.write(self.user_path(), MINI % self.tmp)
        self.assertEqual(config.resolve_base(), (self.user_path(), "user config"))
        self.assertEqual([s["id"] for s in config.load()["seat"]], ["mini"])

    def test_03_env_override_wins_over_the_user_config(self):
        self.write(self.user_path(), MINI % self.tmp)
        alt = self.write(os.path.join(self.tmp, "alt.toml"), MINI.replace("mini", "alt") % self.tmp)
        os.environ[config.CONFIG_ENV] = alt
        self.assertEqual(config.resolve_base(), (alt, "$COUNCIL_CONFIG"))
        self.assertEqual([s["id"] for s in config.load()["seat"]], ["alt"])

    def test_04_env_override_pointing_nowhere_is_an_error_not_a_silent_fallback(self):
        os.environ[config.CONFIG_ENV] = os.path.join(self.tmp, "missing.toml")
        with self.assertRaises(FileNotFoundError):
            config.load()

    def test_05_explicit_base_and_overlays_still_work(self):
        base = self.write(os.path.join(self.tmp, "b.toml"), MINI % self.tmp)
        ov = self.write(os.path.join(self.tmp, "o.toml"), 'answer_reserve_tokens = 1\n')
        cfg = config.load(overlays=[ov], base=base)
        self.assertEqual(cfg["answer_reserve_tokens"], 1)
        self.assertEqual([s["id"] for s in cfg["seat"]], ["mini"])

    def test_06_prompts_dir_falls_back_to_the_shipped_prompts(self):
        shipped = os.path.join(ROOT, "prompts")
        self.assertEqual(config.prompts_dir({}), shipped)
        self.assertEqual(config.prompts_dir({"prompts_dir": os.path.join(self.tmp, "gone")}), shipped)
        d = os.path.join(self.tmp, "mine")
        os.makedirs(d)
        self.assertEqual(config.prompts_dir({"prompts_dir": d}), d)
        for name in ("advisor.md", "chair.md"):
            self.assertTrue(os.path.isfile(os.path.join(shipped, name)), name)

    def test_07_preflight_free_only_names_the_config_it_used(self):
        env = dict(os.environ, HOME=self.tmp)
        p = subprocess.run([sys.executable, "-Es", os.path.join(ROOT, "council.py"), "preflight", "--free-only"],
                           capture_output=True, text=True, timeout=120, env=env)
        first = p.stdout.splitlines()[0] if p.stdout else ""
        self.assertEqual(first, "config: %s (shipped default)" % config.SHIPPED_CONFIG, p.stdout + p.stderr)


class ShippedDefault(unittest.TestCase):
    def test_10_default_loads_and_passes_the_roster_policy(self):
        cfg = config.load(base=config.SHIPPED_CONFIG)
        self.assertTrue(cfg["seat"])
        self.assertEqual(config.validate_roster(cfg), [])

    def test_11_default_carries_no_owner_strings(self):
        with open(config.SHIPPED_CONFIG, encoding="utf-8") as f:
            text = f.read()
        machine = r"/home/|/Users/|[A-Za-z]:\\Users"        # a home path means the file was copied from someone's machine
        self.assertIsNone(re.search(machine + r"|@[a-z0-9-]+\.(com|net|io)", text, re.I))


if __name__ == "__main__":
    unittest.main()
