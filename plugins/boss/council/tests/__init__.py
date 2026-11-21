"""Council tests. Always run in a scratch environment, whatever the caller's holds.

A Claude session exports BOSS_PROGRESS_DIR (the owner's real progress dir) and the tests run under the owner's real HOME, so
anything the suite resolved from the environment or from `~` used to land in the real ~/.claude/boss/runs/council and
~/.claude-council. This module runs first (before any test module is imported) and OVERRIDES, never defaults:

  HOME, CLAUDE_CONFIG_DIR       a throwaway home (so `~/.claude-council`, `~/.claude/boss`, `~/.claude/council` are scratch)
  BOSS_PROGRESS_DIR             where councillib/progress_mirror.py publishes boss.progress/1 snapshots
  BOSS_DATA, ORCH_DIR, WFP_STATE_DIR   the orchestrator's data/state dirs, in case a test or a child process reads them
  BOSS_PROGRESS_LIB             unset (the default is council/progress.py in this tree)

`config.load()` reads the base config from $COUNCIL_CONFIG first: the suite pins it to tests/fixtures/config.test.toml, so
results never depend on whether the machine has a user config at <Claude config dir>/council/config.toml.
Test files may still set their own values with mock.patch.dict; those are restored afterwards."""
import atexit
import os
import shutil
import sys
import tempfile

# C-bytecode: never write __pycache__ into the plugin dir (the entry shebangs run -E, which ignores PYTHONDONTWRITEBYTECODE)
sys.dont_write_bytecode = True

_root = tempfile.mkdtemp(prefix="council-test-env-")
atexit.register(shutil.rmtree, _root, True)
_home = os.path.join(_root, "home")
os.makedirs(os.path.join(_home, ".claude"))
os.environ.update({
    "HOME": _home,
    "CLAUDE_CONFIG_DIR": os.path.join(_home, ".claude"),
    "BOSS_PROGRESS_DIR": os.path.join(_root, "progress"),
    "BOSS_DATA": os.path.join(_root, "data"),
    "ORCH_DIR": os.path.join(_root, "orch"),
    "WFP_STATE_DIR": os.path.join(_root, "wfp"),
})
os.environ.pop("BOSS_PROGRESS_LIB", None)
os.environ["COUNCIL_CONFIG"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "config.test.toml")
SCRATCH_ROOT = _root
