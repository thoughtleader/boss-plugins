"""agy self-update switch (row 44): config.apply_cli_env sets AGY_CLI_DISABLE_AUTO_UPDATE=true for every child."""
import os
import unittest
from unittest import mock

from councillib import config, providers

NAME = "AGY_CLI_DISABLE_AUTO_UPDATE"


class AgyAutoUpdateEnv(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(NAME, None)

    def test_1_shipped_config_turns_it_off(self):
        cfg = config.load()
        self.assertIs(cfg["providers"]["agy"]["disable_auto_update"], True)
        self.assertEqual(config.apply_cli_env(cfg), {NAME: "true"})
        self.assertEqual(os.environ[NAME], "true")

    def test_2_children_inherit_it_even_over_a_wrong_ambient_value(self):
        os.environ[NAME] = "1"            # agy ignores anything but the exact string "true"
        config.apply_cli_env({"providers": {"agy": {"disable_auto_update": True}}})
        self.assertEqual(providers._clean_env()[NAME], "true")

    def test_3_off_or_missing_leaves_the_environment_alone(self):
        for cfg in ({}, {"providers": {}}, {"providers": {"agy": {}}},
                    {"providers": {"agy": {"disable_auto_update": False}}},
                    {"providers": {"agy": {"disable_auto_update": "true"}}}):
            self.assertEqual(config.apply_cli_env(cfg), {}, cfg)
            self.assertNotIn(NAME, os.environ)
        os.environ[NAME] = "true"
        config.apply_cli_env({"providers": {"agy": {"disable_auto_update": False}}})
        self.assertEqual(os.environ[NAME], "true")


if __name__ == "__main__":
    unittest.main()
