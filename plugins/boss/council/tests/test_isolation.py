"""The suite runs in a scratch environment (tests/__init__.py): nothing here may resolve to the owner's real dirs."""
import os
import unittest

from councillib import config
from tests import SCRATCH_ROOT
import progress


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


class ScratchEnvironment(unittest.TestCase):
    def test_home_and_state_dirs_are_scratch(self):
        for name in ("HOME", "CLAUDE_CONFIG_DIR", "BOSS_PROGRESS_DIR", "BOSS_DATA", "ORCH_DIR", "WFP_STATE_DIR"):
            self.assertTrue(_inside(os.environ[name], SCRATCH_ROOT), "%s=%s" % (name, os.environ[name]))

    def test_tilde_paths_resolve_into_scratch(self):
        self.assertTrue(_inside(os.path.expanduser("~/.claude-council"), SCRATCH_ROOT))
        self.assertTrue(_inside(config.load()["state_dir"], SCRATCH_ROOT))
        self.assertTrue(_inside(progress.root(), SCRATCH_ROOT))

    def test_config_is_pinned_to_the_test_fixture(self):
        self.assertTrue(os.environ["COUNCIL_CONFIG"].endswith(os.path.join("fixtures", "config.test.toml")))


if __name__ == "__main__":
    unittest.main()
