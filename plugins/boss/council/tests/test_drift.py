"""Drift check: collect, diff, report, snapshot files, preflight WARN rows, `council.py drift`.

Stubs only: drift._capture and providers.openrouter_get are replaced, the codex config is a temp file,
state_dir is a temp dir. No network, no real CLI, no real settings, no paid call.
"""
import contextlib
import copy
import io
import json
import os
import re
import shutil
import stat
import random
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

import council
from councillib import config, council_run, drift, netwatch, preflight, providers
from councillib.providers import Check

SENTINEL = "sk-or-v1-TESTSECRETDRIFTSENTINEL0123456789"
NOSK = "NOSKDRIFTSENTINEL9f8e7d6c5b4a"          # a secret that does not look like an sk- key: hidden by its key name

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]
  --effort <level>                 Effort level for the current session
  --safe-mode                      Disable hooks and plugins
  --no-session-persistence         Do not save the session
  --output-format <format>         Output format
  --include-partial-messages       Include partial message chunks
  --tools <tools...>               Built-in tools to allow
"""
CODEX_HELP = """Run Codex non-interactively
      --json                     Print events to stdout as JSONL
      --ephemeral                Run without persisting session files
      --skip-git-repo-check      Allow running outside a Git repository
  -o, --output-last-message <FILE>
  -c, --config <key=value>       Override a configuration value
"""
AGY_HELP = """Usage: agy [flags]
  --model                         Model id or label
  --output-format                 Output format: text, json, stream-json
  --print                         Run a single prompt non-interactively and print the response
  --print-timeout                 Optional time limit for print mode; 0 waits until the turn completes (default 0s)
  --sandbox                       Run in a sandbox
"""
AGY_LABELS = ["Gemini 3.8 Flash (High)", "Gemini 3.8 Flash (Medium)", "Gemini 3.8 Flash (Low)",
              "Claude Sonnet 4.6 (Thinking)"]

CODEX_TOML = """check_for_update_on_startup = false
[model_providers.openai-council]
name = "OpenAI (council)"
stream_idle_timeout_ms = %d
request_max_retries = 1
stream_max_retries = 0
requires_openai_auth = true
experimental_bearer_token = "%s"
[model_providers.openai-council.headers]
X-Org = "%s"
[projects."/home/someone/repo"]
trust_level = "trusted"
[notice]
hide_full_access_warning = true
"""


def read(path):
    with open(path) as f:
        return f.read()


def or_endpoint(tag, mct, **over):
    e = {"provider_name": tag.title(), "tag": tag, "context_length": 262144, "max_completion_tokens": mct,
         "quantization": "fp8", "pricing": {"prompt": "0.0000006", "completion": "0.0000024"},
         "supported_parameters": ["max_tokens", "reasoning", "temperature"], "supports_implicit_caching": True,
         "status": 0, "uptime_last_30m": 99.5, "uptime_last_5m": 100.0, "uptime_last_1d": 99.1,
         "latency_last_30m": {"p50": 1.2, "p75": 2.0}, "throughput_last_30m": {"p50": 40.0},
         "perf_last_30m_by_workload": {"chat": {"p50": 1}}}
    e.update(over)
    return e


def or_model(mid, mct, ctx=262144):
    return {"id": mid, "context_length": ctx, "pricing": {"prompt": "0.0000006", "completion": "0.0000024"},
            "top_provider": {"max_completion_tokens": mct, "context_length": ctx},
            "reasoning": {"mandatory": False, "default_enabled": True, "supported_efforts": ["high", "medium", "none"],
                          "supports_max_tokens": True, "default_effort": "high"}}


class World:
    """The fake outside world: CLI outputs, codex config file, OpenRouter /models."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.versions = {"claude": "2.1.305 (Claude Code)", "codex": "codex-cli 0.163.0", "agy": "1.4.4", "qwen": "0.25.3"}
        self.labels = list(AGY_LABELS)
        self.mct = {"z-ai/glm-5.3": 131072, "deepseek/deepseek-v4-pro-0813": 131072}
        self.models_status = 200
        self.endpoints_status = 200
        self.endpoints = {"z-ai/glm-5.3": [or_endpoint("gmicloud", 182476), or_endpoint("z-ai", 131072),
                                         or_endpoint("venice", 32000)],
                          "deepseek/deepseek-v4-pro-0813": [or_endpoint("deepinfra", 16384),
                                                                or_endpoint("baseten", 131072)]}
        self.debug_models = {"models": [{"slug": "gpt-6-astra", "default_reasoning_summary": "none",
                                         "context_window": 400000, "base_instructions": "x" * 5000},
                                        {"slug": "other-model", "context_window": 1}]}
        self.debug_raw = None           # non-JSON `codex debug models` output
        self.fail = set()               # argv[0] names whose every command fails
        self.calls = []
        self.bins = {}
        for name in ("claude", "codex", "agy", "qwen"):
            self.bins[name] = os.path.join(tmp, "bin-" + name)
            self.set_bin(name, "binary of " + name)
        self.codex_toml = os.path.join(tmp, "codex-config.toml")
        self.write_codex(3600000)

    def set_bin(self, name, content):
        with open(self.bins[name], "w") as f:
            f.write(content)

    def which(self, name):
        return self.bins.get(os.path.basename(name))

    def write_codex(self, idle_ms):
        with open(self.codex_toml, "w") as f:
            f.write(CODEX_TOML % (idle_ms, NOSK, NOSK))

    def capture(self, argv, timeout=30):
        self.calls.append(list(argv))
        name = os.path.basename(argv[0])
        if name in self.fail:
            return 1, "", "boom"
        rest = argv[1:]
        if rest == ["--version"]:
            return 0, self.versions[name] + "\n", ""
        if name == "claude" and rest == ["--help"]:
            return 0, CLAUDE_HELP, ""
        if name == "codex" and rest == ["exec", "--help"]:
            return 0, CODEX_HELP, ""
        if name == "codex" and rest == ["debug", "models"]:
            return 0, (self.debug_raw if self.debug_raw is not None else json.dumps(self.debug_models)), ""
        if name == "agy" and rest == ["--help"]:
            return 0, "", AGY_HELP                     # agy prints its help on stderr
        if name == "agy" and rest == ["models"]:
            ids = ["id-%d" % i for i in range(len(self.labels))]
            return 0, "".join("%s\t%s\n" % (i, lab) for i, lab in zip(ids, self.labels)), \
                "Fetching available models..."
        raise AssertionError("unexpected command %r" % argv)

    def or_get(self, path, cfg, key=None, base_url=None, timeout=20):
        if path.endswith("/endpoints"):
            slug = path[len("/models/"):-len("/endpoints")]
            if self.endpoints_status != 200 or slug not in self.endpoints:
                return self.endpoints_status if self.endpoints_status != 200 else 404, None
            return 200, {"data": {"id": slug, "endpoints": self.endpoints[slug]}}
        assert path == "/models", path
        if self.models_status != 200:
            return self.models_status, None
        return 200, {"data": [or_model(m, n) for m, n in self.mct.items()] + [or_model("other/x", 1)]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drift-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = os.path.join(self.tmp, "state")
        self.w = World(self.tmp)
        for target, attr, new in ((drift, "_capture", self.w.capture), (providers, "openrouter_get", self.w.or_get),
                                  (drift, "_which", self.w.which)):
            p = mock.patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": SENTINEL})
        p.start()
        self.addCleanup(p.stop)
        self.ov = os.path.join(self.tmp, "ov.toml")
        with open(self.ov, "w") as f:
            f.write('state_dir = "%s"\n[providers.codex]\nconfig_path = "%s"\n'
                    '[providers.claude.env]\nANTHROPIC_API_KEY = "%s"\nOTHER_PLAIN = "visible"\n'
                    % (self.state, self.w.codex_toml, NOSK))

    def cfg(self):
        c = config.load(overlays=[self.ov])
        self.assertTrue(drift.drift_dir(c).startswith(self.tmp))        # never the real ~/.claude-council
        return c

    def snap(self):
        return drift.collect(self.cfg())

    def recorded(self, snap=None, old=None):
        c = self.cfg()
        drift.accept(c, snap or drift.collect(c), "test", old)      # the explicit move; preflight only observes
        return drift.load_last(c)

    def changes(self, old):
        return {c["key"]: c for c in drift.diff(old, self.snap())}

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = council.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def files_text(self):
        d = os.path.join(self.state, "drift")
        return "".join(read(os.path.join(d, n)) for n in sorted(os.listdir(d)))


class Collect(Base):
    def test_01_first_pass_collects_versions_pins_labels_and_timeouts(self):
        s = self.snap()
        it = {k: v["value"] for k, v in s["items"].items()}
        src = {k: v["source"] for k, v in s["items"].items()}
        self.assertEqual(s["missing"], [])
        self.assertEqual(it["claude.version"], "2.1.305")
        self.assertEqual(it["codex.version"], "0.163.0")
        self.assertEqual(it["agy.version"], "1.4.4")
        self.assertEqual(it["agy.labels"], sorted(AGY_LABELS))
        self.assertEqual(it["agy.print_timeout_default"], "0s")
        self.assertEqual(it["agy.print_timeout_sent"], "3660s")
        self.assertEqual(it["codex.council_provider.openai-council"],
                         {"stream_idle_timeout_ms": 3600000, "request_max_retries": 1, "stream_max_retries": 0,
                          "requires_openai_auth": True})
        self.assertIs(it["codex.check_for_update_on_startup"], False)
        self.assertEqual(it["openrouter.models.z-ai/glm-5.3"]["max_completion_tokens"], 131072)
        self.assertEqual(it["openrouter.models.deepseek/deepseek-v4-pro-0813"]["reasoning"]["supported_efforts"],
                         ["high", "medium", "none"])
        self.assertNotIn("openrouter.models.other/x", it)                    # only seated models
        self.assertEqual(it["openrouter.socket_timeout_s"], 270.0)
        self.assertEqual(it["claude.env.seat"]["API_TIMEOUT_MS"], "7260000")
        self.assertEqual(it["claude.env.chair"]["CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"], "7200000")
        self.assertEqual(it["roster.codex"], "codex:gpt-6-astra")
        self.assertEqual(it["roster.chair"], "claude:claude-opus-5-5")
        self.assertEqual(it["pins.codex"], "0.163.0")
        self.assertEqual(it["runner.codex"]["ceiling_s"], 0)          # row 35: no ceiling (0 = none, D27)
        self.assertEqual(it["runner.agy"]["ceiling_s"], 3600)
        self.assertEqual(src["claude.version"], "cli")
        self.assertEqual(src["claude.env.seat"], "config")
        self.assertEqual(src["codex.council_provider.openai-council"], "cli")
        self.assertEqual(len(it["claude.help_sha256"]), 16)
        # the help prints these; hidden --thinking / --thinking-display are never listed, never "missing"
        self.assertTrue(all(it["claude.help_flags"].values()), it["claude.help_flags"])
        self.assertFalse(any("thinking" in f for f in it["claude.help_flags"]))
        self.assertTrue(all(it["codex.help_flags"].values()), it["codex.help_flags"])
        self.assertTrue(all(it["agy.help_flags"].values()), it["agy.help_flags"])
        self.assertFalse(any(k.startswith("qwen.") for k in it))             # qwen is not seated
        text, rc = drift.report(None, s)
        self.assertEqual(rc, 0)
        self.assertTrue(text.startswith("DRIFT FIRST PASS: no earlier snapshot; %d items" % len(it)))

    def test_02_no_change_after_record(self):
        old = self.recorded()
        self.assertEqual(drift.diff(old, self.snap()), [])
        text, rc = drift.report(old, self.snap())
        self.assertEqual(rc, 0)
        self.assertIn("DRIFT NO CHANGE", text)

    def test_03_version_bump_is_changed(self):
        old = self.recorded()
        self.w.versions["claude"] = "2.1.310 (Claude Code)"
        ch = self.changes(old)
        self.assertEqual(list(ch), ["claude.version"])
        self.assertEqual((ch["claude.version"]["old"], ch["claude.version"]["new"]), ("2.1.305", "2.1.310"))
        text, rc = drift.report(old, self.snap())
        self.assertEqual(rc, 1)
        self.assertIn("CHANGED claude.version: 2.1.305 -> 2.1.310 [cli]", text)

    def test_04_retired_agy_label_is_changed(self):
        old = self.recorded()
        self.w.labels.remove("Gemini 3.8 Flash (Low)")
        ch = self.changes(old)
        self.assertEqual(list(ch), ["agy.labels"])
        self.assertIn("Gemini 3.8 Flash (Low)", ch["agy.labels"]["old"])
        self.assertNotIn("Gemini 3.8 Flash (Low)", ch["agy.labels"]["new"])

    def test_05_changed_max_completion_tokens(self):
        old = self.recorded()
        self.w.mct["deepseek/deepseek-v4-pro-0813"] = 65536
        ch = self.changes(old)
        key = "openrouter.models.deepseek/deepseek-v4-pro-0813"
        self.assertEqual(list(ch), [key])
        self.assertEqual(ch[key]["old"]["max_completion_tokens"], 131072)
        self.assertEqual(ch[key]["new"]["max_completion_tokens"], 65536)

    def test_06_changed_codex_timeout(self):
        old = self.recorded()
        self.w.write_codex(300000)
        ch = self.changes(old)
        key = "codex.council_provider.openai-council"
        eff = "codex.effective.model_providers.openai-council.stream_idle_timeout_ms"
        self.assertEqual(sorted(ch), sorted([key, eff]))              # the block and the effective setting both name it
        self.assertEqual(ch[key]["new"]["stream_idle_timeout_ms"], 300000)
        self.assertEqual((ch[eff]["old"], ch[eff]["new"]), (3600000, 300000))

    def test_07_config_change_new_and_gone(self):
        old = self.recorded()
        with open(self.ov, "a") as f:
            f.write("[timeouts.codex]\nceiling_s = 5400\n[pins]\nnew_pin = \"1.0.0\"\n")
        ch = self.changes(old)
        self.assertEqual(ch["runner.codex"]["kind"], "CHANGED")
        self.assertEqual(ch["pins.new_pin"]["kind"], "NEW")
        old2 = self.recorded(old=old)
        with open(self.ov, "w") as f:
            f.write('state_dir = "%s"\n[providers.codex]\nconfig_path = "%s"\n[[seat]]\nid = "claude"\n'
                    'provider = "claude"\nmodel = "claude-opus-5-5"\n' % (self.state, self.w.codex_toml))
        ch = self.changes(old2)
        self.assertEqual(ch["roster.agy"]["kind"], "GONE")
        self.assertEqual(ch["agy.version"]["kind"], "GONE")

    def test_08_failed_cli_is_not_collected_and_keeps_its_last_value(self):
        old = self.recorded()
        self.w.fail.add("agy")
        s = self.snap()
        self.assertIn("agy.version", s["missing"])
        ch = {c["key"]: c["kind"] for c in drift.diff(old, s)}
        self.assertEqual(set(ch.values()), {"NOT_COLLECTED"})
        self.assertIn("agy.labels", ch)
        text, rc = drift.report(old, s)
        self.assertEqual(rc, 1)
        self.assertIn("NOT COLLECTED agy.version", text)
        kept = self.recorded(s, old)
        self.assertEqual(kept["items"]["agy.version"]["value"], "1.4.4")
        self.w.fail.clear()
        self.assertEqual(drift.diff(kept, self.snap()), [])           # back online: no GONE/NEW flap

    def test_09_models_unavailable_is_not_collected(self):
        old = self.recorded()
        self.w.models_status = 503
        ch = {c["key"]: c["kind"] for c in drift.diff(old, self.snap())}
        self.assertEqual(ch, {"openrouter.models.z-ai/glm-5.3": "NOT_COLLECTED",
                              "openrouter.models.deepseek/deepseek-v4-pro-0813": "NOT_COLLECTED"})

    def test_10_stub_roster_contributes_nothing(self):
        c = config.load(overlays=[{"state_dir": self.state, "seat": [{"id": "a", "provider": "stub", "model": "m"}],
                                   "chair": {"provider": "stub", "model": "c"}}])
        self.w.calls.clear()
        s = drift.collect(c)
        self.assertEqual((s["items"], s["missing"]), ({}, []))
        self.assertEqual(self.w.calls, [])
        self.assertIsNone(preflight._drift_collect(c))


class Settings(Base):
    """Row 22: the snapshot holds the settings each seat really launches with."""

    def collect_with(self, mutate=None):
        c = self.cfg()
        if mutate:
            mutate(c)
        return drift.collect(c)

    def values(self, snap):
        return {k: v["value"] for k, v in snap["items"].items()}

    def seat(self, c, sid):
        return next(x for x in c["seat"] if x["id"] == sid)

    def changed(self, mutate):
        old = self.snap()
        return {c["key"]: c for c in drift.diff(old, self.collect_with(mutate))}

    # ---- manifest: the real build()
    def test_50_manifest_covers_every_launch_kind(self):
        s = self.snap()
        self.assertEqual(s["missing"], [])
        keys = set(s["items"])
        for sid in ("claude", "codex", "agy", "openrouter", "deepseek"):
            for kind in ("seat", "salvage"):
                for part in ("argv", "env", "io", "limits", "policy"):
                    self.assertIn("manifest.%s.%s.%s" % (sid, kind, part), keys)
        for kind in ("chair", "chair_salvage"):
            self.assertIn("manifest.chair.%s.argv" % kind, keys)
        v = self.values(s)
        argv = v["manifest.codex.seat.argv"]
        self.assertIn("model_reasoning_effort=medium", argv)
        self.assertIn("<tmp>/wd/last.txt", argv)
        self.assertEqual(v["manifest.codex.salvage.argv"][-2], "model_reasoning_effort=low")
        self.assertEqual(v["manifest.chair.chair.argv"][v["manifest.chair.chair.argv"].index("--effort") + 1], "high")
        lim = v["manifest.codex.seat.limits"]
        self.assertEqual((lim["mode"], lim["ceiling_s"], lim["deadline_s"], lim["deadline_source"]),
                         ("watch", None, None, "hedge_cold"))     # row 34: codex is hedged (soft deadline); row 35: no ceiling
        alim = v["manifest.agy.seat.limits"]                                       # agy is not hedged (D46)
        self.assertEqual((alim["mode"], alim["deadline_s"], alim["deadline_source"]), ("buffered", 3600.0, "cold"))
        self.assertEqual(v["manifest.agy.seat.limits"]["ceiling_s"], 3600.0)
        self.assertIn("3660s", v["manifest.agy.seat.argv"])
        self.assertEqual(v["manifest.deepseek.seat.policy"]["reasoning_form"], "effort")
        self.assertEqual(v["manifest.deepseek.seat.policy"]["max_output_tokens"], 131072)
        self.assertEqual(v["manifest.codex.seat.io"]["stdin"], "<prompt>")
        self.assertEqual(v["manifest.chair.chair.env"]["API_TIMEOUT_MS"], "7260000")          # the runner's own: readable
        self.assertEqual(v["manifest.chair.chair_salvage.env"]["API_TIMEOUT_MS"], "1260000")   # the salvage ceiling
        self.assertEqual(s["items"]["manifest.codex.seat.argv"]["source"], "config")

    def test_51_manifest_holds_no_temp_path_or_prompt_and_is_stable(self):
        s1 = self.snap()
        tmpdir = tempfile.mkdtemp(prefix="other-tmp-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        with mock.patch.object(tempfile, "tempdir", tmpdir):
            s2 = self.snap()
        for s in (s1, s2):
            for k, it in s["items"].items():
                if k.startswith("manifest."):
                    text = json.dumps(it["value"])
                    self.assertNotIn(tempfile.gettempdir() + "/drift-", text, k)
                    self.assertNotIn(tmpdir, text, k)
                    self.assertNotIn(drift.PROBE_PROMPT, text, k)
        self.assertEqual(drift.diff(s1, s2), [])
        self.assertEqual(os.listdir(tmpdir), [])                       # every temp dir was removed

    def test_52_manifest_uses_an_empty_latency_log_and_the_real_limits(self):
        seen = []
        real = council_run._limits

        def spy(cfg, seat, kind, log_path, prompt_bytes):
            seen.append((seat["id"], kind, log_path, os.path.exists(log_path)))
            return real(cfg, seat, kind, log_path, prompt_bytes)
        with mock.patch.object(council_run, "_limits", spy):
            self.snap()
        kinds = {(sid, kind) for sid, kind, _, _ in seen if sid != "x"}
        self.assertIn(("codex", "seat"), kinds)
        self.assertIn(("chair", "chair_salvage"), {(("chair" if sid.startswith("chair") else sid), kind) for sid, kind in kinds})
        for _, _, log_path, existed in seen:
            self.assertFalse(existed)
            self.assertTrue(log_path.startswith(tempfile.gettempdir()))
            self.assertNotIn(self.state, log_path)                      # never the real latency history

    def test_53_latency_history_never_moves_the_snapshot(self):
        old = self.recorded()
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "latency.jsonl"), "w") as f:
            for i in range(30):
                f.write(json.dumps({"kind": "seat", "outcome": "ok", "provider": "codex", "model": "gpt-6-astra",
                                    "total_s": 100 + i, "prompt_bytes": 100}) + "\n")
        self.assertEqual(drift.diff(old, self.snap()), [])

    def test_54_reasoning_form_and_min_timeout_are_named(self):
        def tokens(c):
            self.seat(c, "deepseek")["reasoning_form"] = "tokens"
        ch = self.changed(tokens)
        self.assertEqual(ch["config.seat.deepseek.reasoning_form"]["new"], "tokens")
        self.assertEqual(ch["manifest.deepseek.seat.policy"]["new"]["reasoning_form"], "tokens")
        self.assertIn("manifest.deepseek.seat.argv", ch)               # --reasoning-max-tokens replaces --reasoning-effort

        def floor(c):                                                   # agy: codex has no floor since row 35
            c["timeouts"]["agy"]["min_timeout_s"] = 1800
        ch = self.changed(floor)
        self.assertEqual((ch["config.timeouts.agy.min_timeout_s"]["old"], ch["config.timeouts.agy.min_timeout_s"]["new"]),
                         (3600, 1800))
        self.assertEqual(ch["runner.agy"]["new"]["min_timeout_s"], 1800)

    def test_55_every_policy_table_leaf_is_in_the_snapshot(self):
        v = self.values(self.snap())
        for key in ("config.timeouts.default.max_extensions", "config.timeouts.default.extension_factor",
                    "config.timeouts.agy.cold_at_ceiling", "config.salvage.enabled", "config.salvage.timeout",
                    "config.quorum.min_ok", "config.quorum.cut_stream_stragglers", "config.timeouts.adaptive.safety",
                    "config.policy.max_anthropic_advisors", "config.pins.codex", "config.canary.timeout_s",
                    "config.answer_reserve_tokens", "config.providers.codex.reasoning_effort",
                    "config.seat.deepseek.ignores_reasoning_cap", "config.chair.model"):
            self.assertIn(key, v)
        self.assertEqual(v["config.seat.agy.model"], "Gemini 3.8 Flash (Medium)")

    def test_56_roster_order_is_not_drift(self):
        old = self.snap()
        new = self.collect_with(lambda c: c["seat"].reverse())
        self.assertEqual(drift.diff(old, new), [])

    def test_57_build_error_is_not_collected_and_collect_never_raises(self):
        def bad(c):
            n = self.seat(c, "deepseek")
            n["reasoning_form"] = "tokens"
            n["max_output_tokens"] = 300                               # reasoning cap leaves no answer room: build raises
        old = self.snap()
        s = self.collect_with(bad)
        self.assertIn("manifest.deepseek.seat.argv", s["missing"])
        ch = {c["key"]: c["kind"] for c in drift.diff(old, s)}
        self.assertEqual(ch["manifest.deepseek.seat.argv"], "NOT_COLLECTED")

    # ---- secrets
    def test_58_secret_values_are_hashed_names_kept(self):
        s = self.snap()
        text = json.dumps(s["items"])
        self.assertNotIn(NOSK, text)
        self.assertNotIn("TESTSECRETDRIFT", text)
        v = self.values(s)
        for key in ("config.providers.claude.env.ANTHROPIC_API_KEY", "manifest.chair.chair.env",
                    "codex.effective.model_providers.openai-council.experimental_bearer_token",
                    "codex.effective.model_providers.openai-council.headers.X-Org"):
            self.assertIn(key, v)
        self.assertRegex(v["config.providers.claude.env.ANTHROPIC_API_KEY"], r"^sha256:[0-9a-f]{8}$")
        self.assertRegex(v["manifest.chair.chair.env"]["ANTHROPIC_API_KEY"], r"^sha256:[0-9a-f]{8}$")
        self.assertRegex(v["codex.effective.model_providers.openai-council.headers.X-Org"], r"^sha256:[0-9a-f]{8}$")
        self.assertEqual(v["config.providers.claude.env.CLAUDE_CODE_MAX_RETRIES"], "1")
        self.assertEqual(v["config.providers.openrouter.key_env"], "OPENROUTER_API_KEY")     # an env-var NAME, not a secret
        self.assertEqual(v["config.providers.claude.max_output_tokens"], 128000)             # a number, not a secret
        # rotating the secret is drift, with the value still hidden
        self.w.write_codex(3600000)
        old = self.snap()
        with open(self.w.codex_toml) as f:
            body = f.read().replace(NOSK, "ROTATEDNOSK0123456789", 1)
        with open(self.w.codex_toml, "w") as f:
            f.write(body)
        ch = {c["key"]: c for c in drift.diff(old, self.snap())}
        k = "codex.effective.model_providers.openai-council.experimental_bearer_token"
        self.assertIn(k, ch)
        self.assertNotIn("ROTATEDNOSK", json.dumps(ch))

    # ---- codex effective config, models, binaries
    def test_60_codex_effective_applies_overrides_and_skips_tui_tables(self):
        with open(self.w.codex_toml) as f:
            body = f.read()
        with open(self.w.codex_toml, "w") as f:
            f.write('model_reasoning_effort = "high"\nsandbox_mode = "workspace-write"\n' + body)
        v = self.values(self.snap())
        self.assertEqual(v["codex.effective.model_reasoning_effort"], "medium")      # the argv -c wins over the file
        self.assertEqual(v["codex.effective.model_provider"], "openai-council")      # set only by -c
        self.assertEqual(v["codex.effective.sandbox_mode"], "workspace-write")       # file only
        self.assertFalse([k for k in v if k.startswith(("codex.effective.projects", "codex.effective.notice"))])

    def test_61_codex_effective_unreadable_is_not_collected(self):
        old = self.snap()
        os.remove(self.w.codex_toml)
        s = self.snap()
        self.assertIn("codex.effective", s["missing"])
        kinds = {c["key"]: c["kind"] for c in drift.diff(old, s)}
        self.assertEqual(kinds["codex.effective.model_providers.openai-council.stream_idle_timeout_ms"], "NOT_COLLECTED")

    def test_62_codex_debug_models(self):
        v = self.values(self.snap())
        e = v["codex.debug_models.gpt-6-astra"]
        self.assertEqual((e["default_reasoning_summary"], e["context_window"]), ("none", 400000))
        self.assertRegex(e["base_instructions"], r"^sha256:[0-9a-f]{8}\(len 5000\)$")            # long text: hash only
        self.assertNotIn("codex.debug_models.other-model", v)                                    # only seated models
        old = self.snap()
        self.w.debug_models["models"][0]["default_reasoning_summary"] = "detailed"
        ch = {c["key"]: c for c in drift.diff(old, self.snap())}
        self.assertEqual(list(ch), ["codex.debug_models.gpt-6-astra"])
        self.assertEqual(ch["codex.debug_models.gpt-6-astra"]["new"]["default_reasoning_summary"], "detailed")
        self.w.debug_models["models"].pop(0)
        ch = {c["key"]: c for c in drift.diff(old, self.snap())}
        self.assertIsNone(ch["codex.debug_models.gpt-6-astra"]["new"])

    def test_63_codex_debug_models_unparsed_output_is_hashed(self):
        self.w.debug_raw = "not json: model table"
        v = self.values(self.snap())
        self.assertEqual(len(v["codex.debug_models_raw_sha256"]), 64)
        self.assertFalse([k for k in v if k.startswith("codex.debug_models.")])

    def test_64_codex_debug_models_failure_is_not_collected(self):
        old = self.snap()
        orig = self.w.capture

        def cap(argv, timeout=30):
            return (1, "", "boom") if argv[1:] == ["debug", "models"] else orig(argv, timeout)
        with mock.patch.object(drift, "_capture", cap):
            s = self.snap()
        self.assertIn("codex.debug_models", s["missing"])
        self.assertEqual({c["kind"] for c in drift.diff(old, s)}, {"NOT_COLLECTED"})

    def test_65_binary_hashes(self):
        v = self.values(self.snap())
        for p in ("claude", "codex", "agy"):
            self.assertRegex(v["%s.bin_sha256" % p], r"^[0-9a-f]{64}$")
        self.assertIsNone(v["codex.native_bin_sha256"])                 # the fake install has no native binary
        self.assertEqual(v["openrouter.seat_script_sha256"], drift._sha256_file(providers.OPENROUTER_SEAT))
        old = self.snap()
        self.w.set_bin("agy", "agy self-updated")
        ch = {c["key"]: c for c in drift.diff(old, self.snap())}
        self.assertEqual(list(ch), ["agy.bin_sha256"])
        del self.w.bins["claude"]
        s = self.snap()
        self.assertIn("claude.bin_sha256", s["missing"])

    def test_66_codex_native_binary_is_found_and_hashed(self):
        pkg = os.path.join(self.tmp, "lib", "@openai", "codex")
        native = os.path.join(pkg, "node_modules", "@openai", "codex-linux-x64", "vendor", "x86_64-unknown-linux-musl", "bin")
        os.makedirs(native)
        os.makedirs(os.path.join(pkg, "bin"))
        js = os.path.join(pkg, "bin", "codex.js")
        with open(js, "w") as f:
            f.write("js")
        with open(os.path.join(native, "codex"), "w") as f:
            f.write("native codex")
        self.w.bins["codex"] = js
        v = self.values(self.snap())
        self.assertEqual(v["codex.native_bin_sha256"], drift._sha256_file(os.path.join(native, "codex")))
        self.assertNotEqual(v["codex.native_bin_sha256"], v["codex.bin_sha256"])

    # ---- code defaults
    def test_70_defaults_are_read_from_the_code(self):
        d = drift._defaults({})
        self.assertEqual(d["timeouts.default.idle_s"], 240)
        self.assertEqual(d["timeouts.default.ceiling_s"], 1200)
        self.assertEqual(d["timeouts.default.grace_s"], 5)
        self.assertEqual(d["timeouts.default.max_extensions"], 1)
        self.assertEqual(d["timeouts.default.extension_factor"], 1.5)
        self.assertEqual(d["timeouts.default.base_timeout_s"], 600)
        self.assertEqual(d["timeouts.default.min_timeout_s"], 120)
        self.assertEqual(d["timeouts.adaptive.min_samples"], 5)
        self.assertEqual(d["timeouts.adaptive.safety"], 1.5)
        self.assertEqual(d["timeouts.adaptive.size_exponent"], 0.5)
        self.assertEqual(d["salvage.max_output_tokens"], 8000)
        self.assertEqual(d["salvage.enabled"], False)
        self.assertEqual(d["answer_reserve_tokens"], providers.DEFAULT_ANSWER_RESERVE)
        # a key the config sets is not a default
        self.assertNotIn("timeouts.default.idle_s", drift._defaults({"timeouts": {"default": {"idle_s": 10}}}))

    def test_71_omitted_config_key_is_recorded_as_default_and_a_code_change_alarms(self):
        def omit(c):
            c["timeouts"]["default"].pop("idle_s")
            c.pop("answer_reserve_tokens")
        s = self.collect_with(omit)
        v = self.values(s)
        self.assertEqual(v["defaults.timeouts.default.idle_s"], "default:240")
        self.assertEqual(v["defaults.answer_reserve_tokens"], "default:16000")
        self.assertNotIn("config.timeouts.default.idle_s", v)
        self.assertNotIn("defaults.timeouts.default.ceiling_s", v)     # the config sets it
        with mock.patch.object(providers, "DEFAULT_ANSWER_RESERVE", 9000):
            ch = {c["key"]: c for c in drift.diff(s, self.collect_with(omit))}
        self.assertEqual(ch["defaults.answer_reserve_tokens"]["new"], "default:9000")

    # ---- OpenRouter endpoints (D43)
    def test_80_endpoints_hold_stable_fields_only(self):
        s = self.snap()
        v = self.values(s)
        base = "openrouter.endpoints.deepseek/deepseek-v4-pro-0813."
        self.assertEqual(v[base + "baseten"], {"provider_name": "Baseten", "context_length": 262144,
                                              "max_completion_tokens": 131072, "quantization": "fp8",
                                              "params": {"reasoning": True, "max_tokens": True}})
        text = json.dumps({k: x for k, x in v.items() if ".endpoints." in k})
        for volatile in ("pricing", "uptime", "latency", "throughput", "perf_", "status", "0.0000006", "implicit"):
            self.assertNotIn(volatile, text)
        self.assertEqual(v[base + "routable_for_budget"], ["baseten"])               # seat max_output_tokens 131072
        # seat max_output_tokens 131072 since the fast-fail chain row (was 182476, only GMICloud): gmicloud and z-ai both fit
        self.assertEqual(v["openrouter.endpoints.z-ai/glm-5.3.routable_for_budget"], ["gmicloud", "z-ai"])

    def test_81_volatile_endpoint_fields_never_alarm(self):
        old = self.recorded()
        for eps in self.w.endpoints.values():
            for e in eps:
                e.update(status=-5, uptime_last_30m=12.5, uptime_last_5m=0.0, uptime_last_1d=50.0,
                         latency_last_30m={"p50": 99.0}, throughput_last_30m={"p50": 1.0},
                         perf_last_30m_by_workload={"chat": {"p50": 9}}, supports_implicit_caching=False,
                         pricing={"prompt": "0.9", "completion": "0.9"})
        self.assertEqual(drift.diff(old, self.snap()), [])

    def test_82_stable_endpoint_change_join_and_leave_alarm(self):
        old = self.recorded()
        eps = self.w.endpoints["deepseek/deepseek-v4-pro-0813"]
        eps[1]["max_completion_tokens"] = 65536                     # baseten shrinks
        eps.append(or_endpoint("novita", 200000))
        self.w.endpoints["z-ai/glm-5.3"][0]["supported_parameters"] = ["max_tokens"]     # gmicloud drops `reasoning`
        self.w.endpoints["z-ai/glm-5.3"].pop(2)                       # venice leaves
        ch = {c["key"]: c for c in drift.diff(old, self.snap())}
        n, g = "openrouter.endpoints.deepseek/deepseek-v4-pro-0813.", "openrouter.endpoints.z-ai/glm-5.3."
        self.assertEqual(ch[n + "baseten"]["kind"], "CHANGED")
        self.assertEqual(ch[n + "novita"]["kind"], "NEW")
        self.assertEqual(ch[n + "routable_for_budget"]["new"], ["novita"])           # baseten no longer fits 131072
        self.assertEqual(ch[g + "gmicloud"]["new"]["params"], {"reasoning": False, "max_tokens": True})
        self.assertEqual(ch[g + "venice"]["kind"], "GONE")

    def test_83_endpoints_unavailable_is_not_collected_and_keeps_its_last_value(self):
        old = self.recorded()
        self.w.endpoints_status = 503
        s = self.snap()
        ch = {c["key"]: c["kind"] for c in drift.diff(old, s)}
        self.assertEqual(set(ch.values()), {"NOT_COLLECTED"})
        self.assertIn("openrouter.endpoints.z-ai/glm-5.3.gmicloud", ch)
        self.assertIn("openrouter.endpoints.z-ai/glm-5.3", s["missing"])
        kept = self.recorded(s, old)
        self.assertEqual(kept["items"]["openrouter.endpoints.z-ai/glm-5.3.gmicloud"]["value"]["max_completion_tokens"], 182476)

    def test_84_serving_provider_parser(self):
        sse = (b': OPENROUTER PROCESSING\n\ndata: {"id":"gen-1","provider":"GMICloud","choices":[]}\n\n'
               b'data: {"id":"gen-1","provider":"Other"}\n\ndata: [DONE]\n')
        self.assertEqual(drift.serving_provider(sse), "GMICloud")
        self.assertIsNone(drift.serving_provider(b'data: {"id":"x"}\n: keepalive\n'))
        self.assertIsNone(drift.serving_provider(""))

    # ---- qwen overlay (per-launch settings file)
    def test_90_qwen_overlay_is_in_the_manifest_without_secrets(self):
        qs = os.path.join(self.tmp, "qwen-settings.json")
        with open(qs, "w") as f:
            json.dump({"modelProviders": {"openai": [{"id": "qwen/qwen3.6-plus", "envKey": "QWEN_API_KEY",
                                                       "apiKey": NOSK, "generationConfig": {"timeout": 1000}}]}}, f)

        def add_qwen(c):
            c["seat"].append({"id": "qwen", "provider": "qwen", "model": "qwen/qwen3.6-plus"})
            c["providers"]["qwen"]["settings_path"] = qs
        s = self.collect_with(add_qwen)
        v = self.values(s)
        self.assertEqual(s["missing"], [])
        self.assertEqual(v["manifest.qwen.seat.env"]["QWEN_CODE_SYSTEM_SETTINGS_PATH"], "<tmp>/wd/qwen-system-settings.json")
        self.assertEqual(v["manifest.qwen.seat.env"]["QWEN_STREAM_IDLE_TIMEOUT_MS"], "300000")
        self.assertEqual(v["manifest.qwen.seat.io"]["env_unset"], ["DASHSCOPE_API_KEY", "QWEN_API_KEY"])
        ov = v["manifest.qwen.seat.files"]["qwen_overlay"]
        gc = ov["modelProviders"]["openai"][0]["generationConfig"]
        self.assertEqual(gc["streamIdleTimeoutMs"], 300000)
        self.assertRegex(ov["modelProviders"]["openai"][0]["apiKey"], r"^sha256:[0-9a-f]{8}$")
        self.assertNotIn(NOSK, json.dumps(s["items"]))


class Files(Base):
    def test_20_modes_and_history(self):
        c = self.cfg()
        path = drift.record(c, drift.collect(c), "test", None)
        d = os.path.dirname(path)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        hp = os.path.join(d, "history.jsonl")
        self.assertEqual(stat.S_IMODE(os.stat(hp).st_mode), 0o600)
        old = drift.load_last(c)
        self.w.versions["codex"] = "codex-cli 0.164.0"
        drift.record(c, drift.collect(c), "test", old)
        lines = [json.loads(x) for x in read(hp).splitlines()]
        self.assertEqual([x["first_pass"] for x in lines], [True, False])
        self.assertEqual([ch["key"] for ch in lines[1]["changes"]], ["codex.version"])
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_21_no_secret_is_recorded_or_printed(self):
        rc, out, err = self.cli("drift", "--config", self.ov, "--record")
        self.assertEqual(rc, 0)
        self.w.versions["agy"] = "1.4.5"
        rc2, out2, err2 = self.cli("drift", "--config", self.ov)
        text = self.files_text() + out + err + out2 + err2
        self.assertNotIn("TESTSECRETDRIFT", text)                        # an sk- shaped value
        self.assertNotIn("NOSKDRIFTSENTINEL", text)                      # a value that does not look like a key
        self.assertNotIn(NOSK, text)
        # D19 (row 22): key NAMES are kept, the value is sha256-8
        self.assertRegex(text, r'"config\.providers\.claude\.env\.ANTHROPIC_API_KEY":\s*\{\s*"source": "config",\s*"value": "sha256:[0-9a-f]{8}"')
        self.assertRegex(text, r'"codex\.effective\.model_providers\.openai-council\.experimental_bearer_token"')
        self.assertNotIn("Usage: claude", text)                          # help text: hash only


class Cli(Base):
    def test_30_first_pass_then_record_then_no_change_then_changed(self):
        rc, out, _ = self.cli("drift", "--config", self.ov)
        self.assertEqual(rc, 0)
        self.assertIn("DRIFT FIRST PASS", out)
        self.assertIn("not recorded", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "drift")))
        rc, out, _ = self.cli("drift", "--config", self.ov, "--record")
        self.assertEqual(rc, 0)
        self.assertIn("recorded %s" % os.path.join(self.state, "drift", "last.json"), out)
        rc, out, _ = self.cli("drift", "--config", self.ov)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("DRIFT NO CHANGE"), out)
        self.w.labels.remove("Gemini 3.8 Flash (Medium)")
        rc, out, _ = self.cli("drift", "--config", self.ov)
        self.assertEqual(rc, 1)
        self.assertIn("CHANGED agy.labels", out)
        rc, out, _ = self.cli("drift", "--config", self.ov)        # plain `drift` never records
        self.assertEqual(rc, 1)

    def test_31_bad_config_exits_2(self):
        rc, _, err = self.cli("drift", "--config", os.path.join(self.tmp, "nope.toml"))
        self.assertEqual(rc, 2)


class Preflight(Base):
    """preflight.run with the real roster; the adapters' free checks are replaced by fixed rows."""

    def free(self, result="PASS"):
        p = mock.patch.object(preflight, "free_stage",
                              lambda cfg: [Check("claude", "free", "version", result, "x")])
        p.start()
        self.addCleanup(p.stop)

    def run_free(self):
        return preflight.run(self.cfg(), free_only=True)

    def drift_rows(self, checks):
        return [c for c in checks if c.provider == "drift"]

    def test_40_first_pass_records_on_pass(self):
        self.free()
        ok, checks = self.run_free()
        self.assertTrue(ok, preflight.format_table(checks))
        rows = self.drift_rows(checks)
        self.assertEqual([(r.check, r.result) for r in rows], [("drift", "PASS")])
        self.assertIn("first pass", rows[0].detail)
        last = drift.load_last(self.cfg())
        self.assertEqual(last["trigger"], "preflight --free-only")
        ok, checks = self.run_free()
        self.assertIn("no change in", self.drift_rows(checks)[0].detail)

    def test_41_changed_item_is_one_warn_row_and_never_blocks(self):
        self.free()
        self.run_free()
        self.w.versions["claude"] = "2.1.311 (Claude Code)"
        self.w.write_codex(7200000)
        ok, checks = self.run_free()
        self.assertTrue(ok)
        rows = {r.check: r for r in self.drift_rows(checks)}
        self.assertEqual(set(rows), {"drift:claude.version", "drift:codex.council_provider.openai-council",
                                     "drift:codex.effective.model_providers.openai-council.stream_idle_timeout_ms"})
        self.assertTrue(all(r.result == "WARN" and r.stage == "free" for r in rows.values()))
        self.assertIn("2.1.305 -> 2.1.311", rows["drift:claude.version"].detail)
        self.assertIn("PREFLIGHT PASS", preflight.format_table(checks))
        self.assertNotIn("drift", preflight.failed_providers(checks))
        ok, checks = self.run_free()                     # D19: the baseline did not move, so the alarm repeats
        self.assertEqual({r.check for r in self.drift_rows(checks)}, set(rows))

    def test_42_no_snapshot_on_fail(self):
        self.free("FAIL")
        ok, checks = self.run_free()
        self.assertFalse(ok)
        self.assertEqual([r.result for r in self.drift_rows(checks)], ["PASS"])
        self.assertFalse(os.path.exists(os.path.join(self.state, "drift")))

    def test_43_fail_keeps_the_change_for_the_next_pass(self):
        self.free()
        self.run_free()
        self.w.versions["codex"] = "codex-cli 0.170.0"
        preflight.free_stage = lambda cfg: [Check("claude", "free", "version", "FAIL", "x")]
        self.run_free()
        preflight.free_stage = lambda cfg: [Check("claude", "free", "version", "PASS", "x")]
        ok, checks = self.run_free()
        self.assertTrue(ok)
        self.assertEqual([r.check for r in self.drift_rows(checks)], ["drift:codex.version"])

    def test_44_collect_raising_is_a_warn_row(self):
        self.free()
        with mock.patch.object(drift, "collect", side_effect=RuntimeError("x")):
            ok, checks = self.run_free()
        self.assertTrue(ok)
        self.assertEqual([(r.result, r.detail) for r in self.drift_rows(checks)],
                         [("WARN", "drift check raised RuntimeError (not recorded)")])
        self.assertFalse(os.path.exists(os.path.join(self.state, "drift")))

    def test_45_unwritable_drift_dir_never_fails_preflight(self):
        self.free()
        os.makedirs(self.state)
        with open(os.path.join(self.state, "drift"), "w") as f:      # a file where the dir should be
            f.write("x")
        ok, checks = self.run_free()
        self.assertTrue(ok)

    def test_46_json_carries_the_drift_rows(self):
        self.free()
        self.run_free()
        self.w.versions["agy"] = "1.4.9"
        ok, checks = self.run_free()
        rows = [r for r in json.loads(preflight.to_json(checks))["checks"] if r["provider"] == "drift"]
        self.assertEqual([(r["check"], r["result"]) for r in rows], [("drift:agy.version", "WARN")])


# ======================================================================== row 24: generated mutation tests
def walk_leaves(obj, path=(), keep_lists=False):
    """-> [(path, leaf)]. Every scalar is a leaf; an empty dict/list is one leaf. With keep_lists a list of scalars is
    ONE leaf and a list of dicts is keyed by `id` (the way drift._flatten names config leaves)."""
    if isinstance(obj, dict) and obj:
        return [x for k in obj for x in walk_leaves(obj[k], path + (k,), keep_lists)]
    if isinstance(obj, list) and obj:
        if keep_lists:
            if all(isinstance(x, dict) for x in obj):
                return [y for i, x in enumerate(obj) for y in walk_leaves(x, path + (i,), keep_lists)]
            return [(path, obj)]
        return [y for i, x in enumerate(obj) for y in walk_leaves(x, path + (i,), keep_lists)]
    return [(path, obj)]


def get_at(obj, path):
    for k in path:
        obj = obj[k]
    return obj


def set_at(obj, path, val):
    if not path:
        raise ValueError("empty path")
    get_at(obj, path[:-1])[path[-1]] = val


def mutate_value(v):
    """A type-aware mutation that cannot be a no-op under drift._norm."""
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return v + 1
    if isinstance(v, float):
        return v + 0.5
    if isinstance(v, str):
        return v + "-mut"
    if v is None:
        return "mut"
    if isinstance(v, list):
        return list(v) + ["mut"]
    if isinstance(v, dict):
        return dict(v, mut=1)
    raise AssertionError("no mutator for %r" % type(v))


def dict_nodes(obj, path=()):
    """-> [(path, dict)] for every non-empty dict inside obj (lists are walked)."""
    out = []
    if isinstance(obj, dict):
        if obj:
            out.append((path, obj))
        for k, x in obj.items():
            out += dict_nodes(x, path + (k,))
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            out += dict_nodes(x, path + (i,))
    return out


def config_leaves(cfg):
    """-> [(accessor path, flat name, leaf)] over the merged config, named exactly like drift._flatten."""
    out = []

    def go(obj, acc, names):
        if isinstance(obj, dict):
            for k in sorted(obj, key=str):
                if not str(k).startswith("_"):
                    go(obj[k], acc + (k,), names + (str(k),))
            return
        if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
            for i, x in enumerate(obj):
                go(x, acc + (i,), names + (str(x.get("id", i)),))
            return
        out.append((acc, ".".join(names), obj))
    go(cfg, (), ())
    return out


class Mutation(Base):
    """Row 24 (D19): every leaf of the snapshot, the merged config and the mocked CLI world is mutated, and the drift
    check must alarm. Volatile fields must not. The counts are printed to stderr."""

    def say(self, text):
        sys.stderr.write("\n[mutation] %s\n" % text)

    def values(self, snap):
        return {k: v["value"] for k, v in snap["items"].items()}

    # ---- A: every leaf of the stub snapshot, at the diff level
    def test_100_every_snapshot_leaf_alarms(self):
        base = self.recorded()
        keys = sorted(base["items"])
        leaves = silent = structural = noop = 0
        missed = []

        def check(label, key, mutated_items):
            snap = {"items": mutated_items, "missing": []}
            got = {c["key"] for c in drift.diff(base, snap)}
            if key not in got:
                missed.append(label)
        for key in keys:
            for path, leaf in walk_leaves(base["items"][key]["value"]):
                items = copy.deepcopy(base["items"])
                val = items[key]["value"]
                new = mutate_value(leaf)
                if not path:
                    items[key]["value"] = new
                else:
                    set_at(val, path, new)
                if drift._canon(drift._norm(base["items"][key]["value"])) == drift._canon(drift._norm(items[key]["value"])):
                    noop += 1
                leaves += 1
                check("%s %s" % (key, list(path)), key, items)
            for path, node in dict_nodes(base["items"][key]["value"]):
                for k in list(node):                            # a key removed from a value dict
                    items = copy.deepcopy(base["items"])
                    del get_at(items[key]["value"], path)[k]
                    structural += 1
                    check("%s %s -%s" % (key, list(path), k), key, items)
                items = copy.deepcopy(base["items"])            # a key added to a value dict
                get_at(items[key]["value"], path)["zz-added"] = 1
                structural += 1
                check("%s %s +zz-added" % (key, list(path)), key, items)
            items = copy.deepcopy(base["items"])                # the item vanishes / a new item appears
            del items[key]
            check("%s GONE" % key, key, items)
            items = copy.deepcopy(base["items"])
            items["zz.new." + key] = {"value": 1, "source": "cli"}
            got = {c["key"]: c["kind"] for c in drift.diff(base, {"items": items, "missing": []})}
            self.assertEqual(got, {"zz.new." + key: "NEW"})
        self.say("snapshot walk: %d items, %d leaves mutated, %d key add/remove mutations, %d silent, %d no-op mutators"
                 % (len(keys), leaves, structural, len(missed), noop))
        self.assertEqual(missed, [], "silent leaves")
        self.assertEqual(noop, 0)
        self.assertGreater(leaves, 700)
        self.assertGreater(len(keys), 200)

    def test_101_volatile_leaves_never_alarm(self):
        base = self.recorded()
        it = copy.deepcopy(base["items"])
        it["openrouter.endpoints.z-ai/glm-5.3.gmicloud"]["value"].update(
            pricing={"prompt": "9"}, status=-9, uptime_last_30m=1.0, latency_last_30m={"p50": 9}, throughput_x=3,
            perf_y={"a": 1}, supports_implicit_caching=False)
        it["manifest.codex.seat.limits"]["value"].update(pid=4242, port=1, daemon_pid=7, daemon_port=8, otlp_port=9,
                                                         timestamp="2026-01-01T00:00:00Z", latency_samples=[1, 2, 3])
        it["codex.daemon_pid"] = {"value": 1234, "source": "cli"}                    # a volatile item key
        it["codex.daemon.port"] = {"value": 4321, "source": "cli"}
        it["manifest.codex.seat.env"]["value"][netwatch.TAG_ENV] = "tag-aaaa"
        it["manifest.codex.seat.env"]["value"]["COUNCIL_OTLP_PORT"] = "40001"
        it["manifest.codex.seat.argv"]["value"].extend(["-c", "otel.endpoint=\"http://127.0.0.1:40001/v1/logs\"",
                                                        "--x=localhost:5555"])
        it["collected_at"] = {"value": "x", "source": "cli"}
        a = {"items": it, "missing": []}
        b = copy.deepcopy(a)
        b["items"]["openrouter.endpoints.z-ai/glm-5.3.gmicloud"]["value"].update(
            pricing={"prompt": "1"}, status=0, uptime_last_30m=99.0, latency_last_30m={"p50": 1}, throughput_x=9,
            perf_y={"a": 2}, supports_implicit_caching=True)
        b["items"]["manifest.codex.seat.limits"]["value"].update(pid=1, port=2, daemon_pid=3, daemon_port=4, otlp_port=5,
                                                                 timestamp="2027-01-01T00:00:00Z", latency_samples=[9])
        b["items"]["codex.daemon_pid"]["value"] = 99
        b["items"]["codex.daemon.port"]["value"] = 98
        b["items"]["manifest.codex.seat.env"]["value"][netwatch.TAG_ENV] = "tag-bbbb"
        b["items"]["manifest.codex.seat.env"]["value"]["COUNCIL_OTLP_PORT"] = "51234"
        argv = b["items"]["manifest.codex.seat.argv"]["value"]
        argv[-2] = "otel.endpoint=\"http://127.0.0.1:51234/v1/logs\""
        argv[-1] = "--x=localhost:6666"
        b["items"]["collected_at"]["value"] = "y"
        self.assertEqual(drift.diff(a, b), [])
        # the knobs are not volatile: the same name inside the owner's config alarms
        a["items"]["config.hedge.port"] = {"value": 1, "source": "config"}
        b["items"]["config.hedge.port"] = {"value": 2, "source": "config"}
        self.assertEqual([c["key"] for c in drift.diff(a, b)], ["config.hedge.port"])

    # ---- B: every leaf of the merged config, through collect
    def config_alarm(self, cfg_mut):
        return {c["key"] for c in drift.diff(self._base, drift.collect(cfg_mut))}

    def test_102_every_config_leaf_alarms(self):
        cfg = self.cfg()
        self._base = drift.collect(cfg)
        leaves = config_leaves(cfg)
        self.assertEqual(sorted(n for _, n, _ in leaves), sorted(".".join(p) for p, _ in drift._flatten(cfg)))
        self.assertGreaterEqual(len(leaves), 119)
        missed, extra_layers = [], 0
        for acc, name, leaf in leaves:
            c2 = copy.deepcopy(cfg)
            set_at(c2, acc, mutate_value(leaf))
            keys = self.config_alarm(c2)
            if "config." + name not in keys:
                missed.append(name)
            extra_layers += len(keys) > 1
        self.say("config walk: %d leaves mutated, %d silent, %d also alarmed a second layer (runner/manifest/defaults)"
                 % (len(leaves), len(missed), extra_layers))
        self.assertEqual(missed, [])

    def test_103_reasoning_form_alarm_is_named(self):
        cfg = self.cfg()
        names = {n for _, n, _ in config_leaves(cfg)}
        self.assertIn("seat.deepseek.reasoning_form", names)
        base = drift.collect(cfg)
        c2 = copy.deepcopy(cfg)
        next(x for x in c2["seat"] if x["id"] == "deepseek")["reasoning_form"] = "tokens"
        ch = {c["key"]: c for c in drift.diff(base, drift.collect(c2))}
        self.assertEqual((ch["config.seat.deepseek.reasoning_form"]["old"], ch["config.seat.deepseek.reasoning_form"]["new"]),
                         ("effort", "tokens"))
        self.assertEqual(ch["manifest.deepseek.seat.policy"]["new"]["reasoning_form"], "tokens")
        text, rc = drift.report(base, drift.collect(c2))
        self.assertEqual(rc, 1)
        self.assertIn("CHANGED config.seat.deepseek.reasoning_form: effort -> tokens", text)

    def test_104_min_timeout_s_alarm_is_named(self):
        cfg = self.cfg()
        names = {n for _, n, _ in config_leaves(cfg)}
        self.assertIn("timeouts.codex.ceiling_s", names)              # row 35: codex's floor keys are retired
        self.assertIn("timeouts.agy.min_timeout_s", names)
        self.assertIn("timeouts.default.min_timeout_s", names)
        base = drift.collect(cfg)
        for prov in ("agy",):
            c2 = copy.deepcopy(cfg)
            c2["timeouts"][prov]["min_timeout_s"] = 1800
            ch = {c["key"]: c for c in drift.diff(base, drift.collect(c2))}
            k = "config.timeouts.%s.min_timeout_s" % prov
            self.assertEqual((ch[k]["old"], ch[k]["new"]), (3600, 1800))
            self.assertEqual(ch["runner.%s" % prov]["new"]["min_timeout_s"], 1800)
        c3 = copy.deepcopy(cfg)
        del c3["timeouts"]["default"]["min_timeout_s"]               # omitted -> the code default is recorded instead
        ch = {c["key"]: c for c in drift.diff(base, drift.collect(c3))}
        self.assertEqual(ch["config.timeouts.default.min_timeout_s"]["kind"], "GONE")
        self.assertEqual(ch["defaults.timeouts.default.min_timeout_s"]["kind"], "NEW")

    # ---- C: every field of the mocked CLI world, through collect
    def world(self):
        w = self.w
        with open(w.codex_toml, "rb") as f:
            toml = tomllib.load(f)
        models = {m: or_model(m, n) for m, n in w.mct.items()}
        models["other/x"] = or_model("other/x", 1)
        return {"versions": dict(w.versions), "labels": list(w.labels), "models": models,
                "endpoints": copy.deepcopy(w.endpoints), "debug_models": copy.deepcopy(w.debug_models),
                "bins": {n: "binary of " + n for n in w.bins}, "helps": {"claude": CLAUDE_HELP, "codex": CODEX_HELP,
                                                                         "agy": AGY_HELP}, "codex_toml": toml}

    def collect_world(self, data):
        w = self.w
        for n, content in data["bins"].items():
            w.set_bin(n, content)
        drift._HASH_CACHE.clear()
        helps = data["helps"]

        def capture(argv, timeout=30):
            name, rest = os.path.basename(argv[0]), argv[1:]
            if rest == ["--version"]:
                return 0, data["versions"][name] + "\n", ""
            if rest == ["--help"] and name == "claude":
                return 0, helps["claude"], ""
            if rest == ["exec", "--help"]:
                return 0, helps["codex"], ""
            if rest == ["--help"] and name == "agy":
                return 0, "", helps["agy"]
            if name == "codex" and rest == ["debug", "models"]:
                return 0, json.dumps(data["debug_models"]), ""
            if name == "agy" and rest == ["models"]:
                return 0, "".join("id-%d\t%s\n" % (i, lab) for i, lab in enumerate(data["labels"])), ""
            raise AssertionError("unexpected command %r" % argv)

        def or_get(path, cfg, key=None, base_url=None, timeout=20):
            if path.endswith("/endpoints"):
                slug = path[len("/models/"):-len("/endpoints")]
                return 200, {"data": {"id": slug, "endpoints": data["endpoints"][slug]}}
            return 200, {"data": list(data["models"].values())}
        with mock.patch.object(drift, "_capture", capture), mock.patch.object(providers, "openrouter_get", or_get), \
                mock.patch.object(providers.CodexAdapter, "_toml", lambda self_, pc: copy.deepcopy(data["codex_toml"])):
            return drift.collect(self.cfg())

    def world_mutation(self, path, leaf):
        """-> the mutated value. Versions bump the patch number; a help text gains a flag line."""
        if path[0] == "versions":
            return re.sub(r"(\d+\.\d+\.)(\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1), leaf, count=1)
        if path[0] == "helps":
            return leaf + "\n      --zz-new-flag   added\n"
        return mutate_value(leaf)

    def expectation(self, path, data):
        """alarm | silent (volatile: must not alarm) | irrelevant (deliberately not behaviour: must not alarm)."""
        top = path[0]
        if top == "endpoints":
            field = path[3]
            if field in drift.ENDPOINT_STABLE or field == "tag":
                return "alarm"
            if field == "supported_parameters":
                return "alarm" if data["endpoints"][path[1]][path[2]]["supported_parameters"][path[4]] in drift.ENDPOINT_PARAMS \
                    else "irrelevant"
            if field in ("pricing", "status", "supports_implicit_caching") or field.startswith(drift.VOLATILE_PREFIXES):
                return "silent"
            raise AssertionError("unclassified mocked endpoint field %r" % field)
        if top == "models":
            if path[1] == "other/x":
                return "irrelevant"                                # an unseated model
            return "silent" if path[2] == "pricing" else "alarm"
        if top == "versions":
            return "irrelevant" if path[1] == "qwen" else "alarm"      # qwen is not seated
        if top == "bins":
            return "irrelevant" if path[1] == "qwen" else "alarm"
        if top == "debug_models":
            return "alarm" if data["debug_models"]["models"][path[2]]["slug"] == "gpt-6-astra" else "irrelevant"
        if top == "codex_toml":
            return "silent" if path[1] in drift.CODEX_VOLATILE_TABLES else "alarm"     # TUI-written tables
        return "alarm"                                             # labels, helps

    def test_105_every_mocked_cli_field_alarms_and_volatile_ones_do_not(self):
        data = self.world()
        base = self.collect_world(data)
        self.assertEqual(base["missing"], [])
        self.assertEqual(drift.diff(base, self.collect_world(data)), [])
        counts = {"alarm": 0, "silent": 0, "irrelevant": 0}
        bad = []
        walked = walk_leaves(data)
        for path, leaf in walked:
            d2 = copy.deepcopy(data)
            set_at(d2, path, self.world_mutation(path, leaf))
            want = self.expectation(path, data)
            got = drift.diff(base, self.collect_world(d2))
            counts[want] += 1
            if (want == "alarm") != bool(got):
                bad.append((want, path, [c["key"] for c in got]))
        self.say("cli walk: %d leaves mutated: %d alarm, %d volatile stay silent, %d deliberately irrelevant stay silent;"
                 " wrong: %d" % (len(walked), counts["alarm"], counts["silent"], counts["irrelevant"], len(bad)))
        self.assertEqual(bad, [])
        self.assertGreater(counts["alarm"], 80)
        self.assertGreater(counts["silent"], 20)
        self.assertGreaterEqual(len(walked), 150)

    # ---- stability: nothing per-run may move the snapshot
    def test_106_random_pid_port_tag_and_tmp_do_not_alarm(self):
        real = providers.CodexAdapter.build

        def build(self_, seat, prompt_path, workdir, limits, cfg):
            launch = real(self_, seat, prompt_path, workdir, limits, cfg)
            port, tag = random.randint(20000, 60000), "%08x" % random.getrandbits(32)
            launch.argv = launch.argv[:-1] + ["-c", 'otel.exporter.endpoint="http://127.0.0.1:%d/v1/logs"' % port,
                                              "--tag=%s=%s" % (netwatch.TAG_ENV, tag), launch.argv[-1]]
            launch.env = dict(launch.env, **{netwatch.TAG_ENV: tag, "COUNCIL_OTLP_PORT": str(port)})
            return launch
        snaps = []
        for _ in range(3):
            other = tempfile.mkdtemp(prefix="other-tmp-")
            self.addCleanup(shutil.rmtree, other, ignore_errors=True)
            with mock.patch.object(providers.CodexAdapter, "build", build), mock.patch.object(tempfile, "tempdir", other):
                snaps.append(self.snap())
        v0 = self.values(snaps[0])
        self.assertIn("127.0.0.1:", json.dumps(v0["manifest.codex.seat.argv"]))      # the port really is in the record
        self.assertNotEqual(self.values(snaps[0])["manifest.codex.seat.argv"], self.values(snaps[1])["manifest.codex.seat.argv"])
        self.assertEqual(drift.diff(snaps[0], snaps[1]), [])
        self.assertEqual(drift.diff(snaps[1], snaps[2]), [])
        # control: the same launch with a different endpoint HOST (not loopback) does alarm
        def build_host(self_, seat, prompt_path, workdir, limits, cfg):
            launch = real(self_, seat, prompt_path, workdir, limits, cfg)
            launch.argv = launch.argv[:-1] + ["-c", 'otel.exporter.endpoint="http://example.test:4318/v1/logs"', launch.argv[-1]]
            return launch
        with mock.patch.object(providers.CodexAdapter, "build", build_host):
            self.assertIn("manifest.codex.seat.argv", {c["key"] for c in drift.diff(snaps[0], self.snap())})

    def test_107_number_forms_and_bool_are_distinct(self):
        base = self.recorded()
        a = copy.deepcopy(base)
        a["items"]["config.timeouts.agy.ceiling_s"]["value"] = 3600.0         # a float form of 3600 (agy: codex has none)
        self.assertEqual(drift.diff(base, a), [])
        self.assertEqual(drift.diff(a, base), [])
        a["items"]["manifest.agy.seat.limits"]["value"]["ceiling_s"] = 3600         # 3600.0 recorded, int now
        self.assertEqual(drift.diff(a, base), [])
        b = copy.deepcopy(base)
        b["items"]["config.canary.timeout_s"]["value"] = True                 # was 120
        b["items"]["config.salvage.enabled"]["value"] = 1                      # was True
        self.assertEqual({c["key"] for c in drift.diff(base, b)}, {"config.canary.timeout_s", "config.salvage.enabled"})
        c = copy.deepcopy(base)
        c["items"]["zz.pins.codex"] = c["items"].pop("pins.codex")             # renamed: one GONE, one NEW
        self.assertEqual({(x["kind"], x["key"]) for x in drift.diff(base, c)},
                         {("NEW", "zz.pins.codex"), ("GONE", "pins.codex")})

    def test_108_unknown_keys_and_unclassified_fields_alarm_once(self):
        old = self.recorded()
        # a key in only one snapshot is NEW / GONE whatever its name: there is no allowlist
        s = self.snap()
        s["items"]["zz.unheard.of"] = {"value": {"a": 1}, "source": "cli"}
        self.assertEqual([(c["kind"], c["key"]) for c in drift.diff(old, s)], [("NEW", "zz.unheard.of")])
        self.assertEqual([(c["kind"], c["key"]) for c in drift.diff(s, old)], [("GONE", "zz.unheard.of")])
        # a field with no collector: one coverage-gap item, once; the volatile families are not gaps
        eps = self.w.endpoints["z-ai/glm-5.3"]
        eps[0]["new_routing_hint"] = {"weight": 3}
        eps[1]["uptime_last_7d"] = 12.0
        eps[1]["latency_p99"] = 4.0
        ch = {c["key"]: c for c in drift.diff(old, self.snap())}
        gap = "openrouter.endpoints.z-ai/glm-5.3.unclassified_fields"
        self.assertEqual(list(ch), [gap])
        self.assertEqual((ch[gap]["kind"], ch[gap]["new"]), ("NEW", {"gmicloud": ["new_routing_hint"]}))
        old2 = self.recorded(self.snap(), old)
        eps[0]["new_routing_hint"] = {"weight": 99}                            # its value moving is not an alarm
        self.assertEqual(drift.diff(old2, self.snap()), [])
        del eps[0]["new_routing_hint"]                                          # classified or removed: GONE
        self.assertEqual({c["key"]: c["kind"] for c in drift.diff(old2, self.snap())}, {gap: "GONE"})

    def test_109_models_pricing_is_volatile(self):
        old = self.recorded()
        self.assertNotIn("pricing", json.dumps(old["items"]["openrouter.models.z-ai/glm-5.3"]))
        stale = copy.deepcopy(old)                                              # an older baseline still holding pricing
        stale["items"]["openrouter.models.z-ai/glm-5.3"]["value"]["pricing"] = {"prompt": "1", "completion": "2"}
        self.assertEqual(drift.diff(stale, self.snap()), [])


class McpPrivacy(Base):
    """C5: the user's codex MCP servers (URLs, headers, commands) are never printed or stored in plain, not even as the
    header hash; a change still alarms (as `value changed, not shown`) and the server name stays visible."""
    URL, URL2 = "https://private-host.example.invalid:8443/mcp?k=1", "https://other-host.example.invalid/mcp"
    BEARER = "Bearer FIXTUREBEARER0123456789"
    TOML = ('\n[mcp_servers.alpha]\nurl = "%s"\nstartup_timeout_sec = 20\n'
            '[mcp_servers.alpha.http_headers]\nAuthorization = "%s"\nX-Tenant = "tenant-fixture-7"\n'
            '[mcp_servers.beta]\ncommand = "/home/someone/bin/beta-mcp"\nargs = ["--root", "/home/someone/notes"]\n'
            '[model_providers.side]\nbase_url = "https://side-host.example.invalid/v1"\n')
    LEAKS = ("private-host", "other-host", "side-host", "FIXTUREBEARER", "tenant-fixture", "/home/someone")

    def setUp(self):
        super().setUp()
        self.conf(self.URL)
        p = mock.patch.object(preflight, "free_stage", lambda cfg: [Check("claude", "free", "version", "PASS", "x")])
        p.start()
        self.addCleanup(p.stop)

    def conf(self, url):
        self.w.write_codex(3600000)
        with open(self.w.codex_toml, "a") as f:
            f.write(self.TOML % (url, self.BEARER))

    def assertClean(self, text, printed=True):
        """No plain value anywhere; PRINTED text also shows no hash on a codex.effective line."""
        for bad in self.LEAKS:
            self.assertNotIn(bad, text)
        if printed:
            self.assertFalse([l for l in text.splitlines() if "codex.effective." in l and "sha256:" in l])

    def eff(self, snap):
        return {k: v["value"] for k, v in snap["items"].items() if k.startswith("codex.effective.")}

    def test_90_stored_snapshot_holds_no_url_or_header_value(self):
        s = self.snap()
        text = json.dumps(s["items"])
        for bad in ("private-host", "FIXTUREBEARER", "tenant-fixture", "/home/someone", "side-host"):
            self.assertNotIn(bad, text)
        e = self.eff(s)
        self.assertRegex(e["codex.effective.mcp_servers.alpha.url"], r"^sha256:[0-9a-f]{8}$")
        self.assertRegex(e["codex.effective.mcp_servers.alpha.http_headers.X-Tenant"], r"^sha256:")
        self.assertRegex(e["codex.effective.mcp_servers.beta.args"], r"^sha256:")
        self.assertRegex(e["codex.effective.model_providers.side.base_url"], r"^sha256:")
        self.assertEqual(e["codex.effective.mcp_servers.alpha.startup_timeout_sec"], 20)     # a plain number stays readable
        # C4 shows up as readable booleans: both user servers are disabled for the seat
        self.assertIs(e["codex.effective.mcp_servers.alpha.enabled"], False)
        self.assertIs(e["codex.effective.mcp_servers.beta.enabled"], False)

    def test_90b_the_engines_own_loopback_endpoint_is_not_private(self):
        ep = 'http://127.0.0.1:5555/v1/logs'
        self.assertEqual(drift._redact_codex(ep, ("otel", "exporter", "endpoint")), ep)     # masked as <port> at diff time
        self.assertEqual(drift._display("codex.effective.otel.exporter.endpoint", ep), ep)
        self.assertRegex(drift._redact_codex("https://x.example.invalid/mcp", ("otel", "exporter", "endpoint")), r"^sha256:")

    def test_91_first_pass_report_and_cli_print_markers_only(self):
        text, rc = drift.report(None, self.snap())
        self.assertClean(text)
        self.assertIn("codex.effective.mcp_servers.alpha.url = <set>", text)
        self.assertIn("codex.effective.mcp_servers.alpha.enabled = false", text)
        rc, out, err = self.cli("drift", "--config", self.ov)
        self.assertClean(out + err)

    def test_92_a_changed_url_still_alarms_without_printing_either_value(self):
        old = self.recorded()
        self.conf(self.URL2)
        ch = {c["key"]: c for c in drift.diff(old, self.snap())}
        self.assertEqual(sorted(k for k in ch if "mcp_servers" in k), ["codex.effective.mcp_servers.alpha.url"])
        line = drift.change_line(ch["codex.effective.mcp_servers.alpha.url"])
        self.assertIn("value changed, not shown", line)
        self.assertClean(line)
        ok, checks = preflight.run(self.cfg(), free_only=True)
        self.assertTrue(ok)
        table = preflight.format_table(checks)
        self.assertIn("drift:codex.effective.mcp_servers.alpha.url", table)
        self.assertClean(table)
        self.assertClean(json.dumps(drift.diff(old, self.snap())), printed=False)       # what history.jsonl stores

    def test_93_a_baseline_written_before_the_redaction_neither_alarms_nor_prints(self):
        old = self.recorded()
        legacy = copy.deepcopy(old)
        for k, v in legacy["items"].items():           # the pre-redaction shape: plain values under codex.effective
            if k == "codex.effective.mcp_servers.alpha.url":
                v["value"] = self.URL
            elif k == "codex.effective.mcp_servers.alpha.http_headers.Authorization":
                v["value"] = self.BEARER
            elif k == "codex.effective.mcp_servers.beta.args":
                v["value"] = ["--root", "/home/someone/notes"]
            elif k == "codex.effective.model_providers.side.base_url":
                v["value"] = "https://side-host.example.invalid/v1"
        self.assertIn(self.URL, json.dumps(legacy))
        self.assertEqual(drift.diff(legacy, self.snap()), [])               # same canonical value: no alarm
        self.conf(self.URL2)
        ch = drift.diff(legacy, self.snap())
        self.assertEqual([c["key"] for c in ch], ["codex.effective.mcp_servers.alpha.url"])
        self.assertClean(drift.change_line(ch[0]))
        self.assertClean(json.dumps(ch), printed=False)
        gone = copy.deepcopy(legacy)
        gone["items"]["codex.effective.mcp_servers.zeta.url"] = {"value": self.URL, "source": "cli"}
        self.assertClean(drift.report(gone, self.snap())[0])

    def test_94_files_on_disk_hold_no_plain_value(self):
        c = self.cfg()
        drift.accept(c, drift.collect(c), "test", None)
        self.assertClean(self.files_text(), printed=False)


class OwnConfigPrivacy(Base):
    """Review gap 2: codex `http_headers` values (model_providers.<n>.http_headers) and every URL of the council's own config
    (`config.*`, `runner.*`) are stored as sha256-8 and never printed, on every drift / preflight output path."""
    CURL, CHDR = "http://private-proxy.corp.example:8080/v1", "acme-corp-tenant-77"      # the council's own config
    XURL, XHDR = "https://codex-side.corp.example:9443/v1", "codex-tenant-fixture-5"      # the user's codex config
    LEAKS = ("private-proxy", "corp.example", "acme-corp", "tenant-77", "codex-side", "codex-tenant", "tenant-fixture")

    def setUp(self):
        super().setUp()
        p = mock.patch.object(preflight, "free_stage", lambda cfg: [Check("claude", "free", "version", "PASS", "x")])
        p.start()
        self.addCleanup(p.stop)
        self.plant(self.CURL, self.CHDR, self.XURL, self.XHDR)

    def plant(self, curl, chdr, xurl, xhdr):
        self.w.write_codex(3600000)
        with open(self.w.codex_toml, "a") as f:
            f.write('\n[model_providers.side]\nbase_url = "%s"\n[model_providers.side.http_headers]\nX-Tenant = "%s"\n' % (xurl, xhdr))
        with open(self.ov, "w") as f:
            f.write('state_dir = "%s"\n[providers.codex]\nconfig_path = "%s"\n'
                    '[providers.claude.env]\nANTHROPIC_API_KEY = "%s"\nOTHER_PLAIN = "visible"\n'
                    '[providers.openrouter]\nbase_url = "%s"\n[providers.openrouter.http_headers]\nX-Tenant = "%s"\n'
                    % (self.state, self.w.codex_toml, NOSK, curl, chdr))

    def clean(self, text, where):
        for bad in self.LEAKS:
            self.assertNotIn(bad, text, "%r leaked in %s" % (bad, where))

    def test_95_redaction_rules(self):
        self.assertRegex(drift._redact(["providers", "x", "http_headers", "X-Tenant"], "acme"), r"^sha256:")
        self.assertRegex(drift._redact(["a", "env_http_headers", "H"], "ENVNAME"), r"^sha256:")
        self.assertEqual(drift._redact(["a", "b", "timeout"], "30"), "30")                       # not a headers parent
        self.assertRegex(drift._redact(["providers", "custom", "base_url"], self.CURL, True), r"^sha256:")
        self.assertEqual(drift._redact(["providers", "custom", "base_url"], self.CURL), self.CURL)  # only the council's own config hashes URLs
        h = drift._redact(["providers", "custom", "base_url"], self.CURL, True)
        self.assertEqual(drift._redact(["providers", "custom", "base_url"], h, True), h)          # idempotent
        self.assertRegex(drift._redact_codex({"model_providers": {"x": {"http_headers": {"X-Tenant": "acme"}}}})["model_providers"]["x"]["http_headers"]["X-Tenant"], r"^sha256:")
        self.assertEqual(drift._display("config.providers.custom.base_url", self.CURL), "<set>")
        self.assertEqual(drift._display("config.providers.custom.timeout", 30), "30")

    def test_96_stored_snapshot_and_files_hold_neither(self):
        s = self.snap()
        self.clean(json.dumps(s), "the stored snapshot")
        it = {k: v["value"] for k, v in s["items"].items()}
        self.assertRegex(it["config.providers.openrouter.base_url"], r"^sha256:[0-9a-f]{8}$")
        self.assertRegex(it["config.providers.openrouter.http_headers.X-Tenant"], r"^sha256:")
        self.assertRegex(it["codex.effective.model_providers.side.http_headers.X-Tenant"], r"^sha256:")
        self.assertRegex(it["codex.effective.model_providers.side.base_url"], r"^sha256:")
        c = self.cfg()
        drift.accept(c, drift.collect(c), "test", None)
        self.clean(self.files_text(), "last.json / history.jsonl")

    def test_97_every_output_path_prints_neither_when_values_change(self):
        old = self.recorded()
        self.clean(drift.report(None, self.snap())[0], "first-pass report")
        self.clean("".join(self.cli("drift", "--config", self.ov)[1:]), "council.py drift (first pass, baseline exists)")
        self.plant("http://other-proxy.corp.example:9090/v1", "acme-corp-tenant-88", "https://codex-side2.corp.example/v1", "codex-tenant-fixture-6")
        new = self.snap()
        self.clean(json.dumps(new), "changed snapshot")
        ch = drift.diff(old, new)
        keys = {c["key"] for c in ch}
        for k in ("config.providers.openrouter.base_url", "config.providers.openrouter.http_headers.X-Tenant",
                  "codex.effective.model_providers.side.http_headers.X-Tenant", "codex.effective.model_providers.side.base_url"):
            self.assertIn(k, keys)                                                              # a change still alarms
        for c in ch:
            self.clean(drift.change_line(c), "change_line " + c["key"])
        self.clean(json.dumps(ch), "diff (what history.jsonl stores)")
        self.clean(drift.report(old, new)[0], "report with a baseline")
        ok, checks = preflight.run(self.cfg(), free_only=True)
        self.assertTrue(ok)
        table = preflight.format_table(checks)
        self.assertIn("drift:config.providers.openrouter.base_url", table)
        self.clean(table, "the preflight table")
        self.clean(json.dumps([c.__dict__ for c in checks], default=str), "preflight check rows")
        rc, out, err = self.cli("drift", "--config", self.ov)
        self.clean(out + err, "council.py drift")
        rc, out, err = self.cli("drift", "--config", self.ov, "--accept")
        self.clean(out + err, "council.py drift --accept")
        self.clean(self.files_text(), "last.json / history.jsonl after an accept")

    def test_97b_provider_free_checks_print_no_url_even_when_the_endpoint_fails(self):
        """The openrouter checks build their requests from the council's own base_url: no row or error text may carry it."""
        cfg = self.cfg()
        seats = [x for x in preflight._roster(cfg) if x.get("provider") == "openrouter"]
        self.assertTrue(seats)
        for label, fn in (("ok", lambda *a, **k: (200, {"data": []})), ("down", lambda *a, **k: (None, None)), ("500", lambda *a, **k: (500, None))):
            with mock.patch.object(providers, "openrouter_get", fn):
                rows = providers.get("openrouter").free_checks(seats, cfg)
            self.assertTrue(rows)
            self.clean("\n".join("%s %s %s %s" % (c.provider, c.check, c.result, c.detail) for c in rows), "openrouter free checks (%s)" % label)

    def test_98_a_baseline_written_before_the_rule_neither_alarms_nor_prints(self):
        old = self.recorded()
        legacy = copy.deepcopy(old)
        plain = {"config.providers.openrouter.base_url": self.CURL, "config.providers.openrouter.http_headers.X-Tenant": self.CHDR,
                 "codex.effective.model_providers.side.http_headers.X-Tenant": self.XHDR, "codex.effective.model_providers.side.base_url": self.XURL}
        for k, v in plain.items():
            self.assertRegex(legacy["items"][k]["value"], r"^sha256:")
            legacy["items"][k]["value"] = v                   # the pre-rule shape: the plain value
        argv = legacy["items"]["manifest.openrouter.seat.argv"]["value"]
        self.assertTrue(any(a.startswith("sha256:") for a in argv))
        legacy["items"]["manifest.openrouter.seat.argv"]["value"] = [self.CURL if a.startswith("sha256:") else a for a in argv]
        self.assertIn(self.CURL, json.dumps(legacy))
        self.assertEqual(drift.diff(legacy, self.snap()), [])           # same canonical value: no alarm
        self.clean(drift.report(legacy, self.snap())[0], "report against a legacy baseline")
        gone = copy.deepcopy(legacy)                                    # legacy values that no longer exist now: GONE rows print the old value
        gone["items"]["config.providers.zeta.base_url"] = {"value": self.CURL, "source": "config"}
        gone["items"]["config.providers.zeta.http_headers.X"] = {"value": self.CHDR, "source": "config"}
        gone["items"]["manifest.zeta.seat.argv"] = {"value": ["--base-url", self.CURL], "source": "config"}
        ch = drift.diff(gone, self.snap())
        self.assertEqual(len(ch), 3)
        self.clean(drift.report(gone, self.snap())[0], "report listing GONE legacy values")
        for c in ch:
            self.clean(drift.change_line(c), "change_line " + c["key"])
            self.clean(json.dumps(c), "diff entry " + c["key"])
        for k in ("config.providers.zeta.base_url", "manifest.zeta.seat.argv"):      # a legacy plain URL that reaches a print path shows as <set>
            self.assertEqual(drift._display(k, gone["items"][k]["value"]), "<set>")

    # review minor (8b): codex `query_params` values and scheme-less hosts were stored and printed in plain text
    QP, QHOST, CHOST = "qp-tenant-secret-91", "private-box.lan:8080", "cfg-box.lan:9090"
    LEAKS2 = ("qp-tenant", "private-box", "cfg-box", "lan:")

    def plant_more(self, qp, qhost, chost):
        with open(self.w.codex_toml, "a") as f:
            f.write('\n[model_providers.side.query_params]\napi-version = "%s"\n[model_providers.hosty]\nbase_url = "%s"\n' % (qp, qhost))
        with open(self.ov, "a") as f:
            f.write('\n[providers.openrouter.query_params]\nv = "%s"\n[providers.zz]\nbase_url = "%s"\n' % (qp, chost))

    def test_99_redaction_of_query_params_and_scheme_less_hosts(self):
        self.assertRegex(drift._redact(["a", "query_params", "api-version"], self.QP), r"^sha256:")
        self.assertRegex(drift._redact(["a", "base_url"], self.QHOST, True), r"^sha256:")
        self.assertRegex(drift._redact(["a", "endpoint"], "proxy.corp.example/v1", True), r"^sha256:")
        self.assertEqual(drift._redact(["a", "log_file"], "config.toml", True), "config.toml")   # a dotted name under a non-address key stays
        self.assertEqual(drift._redact(["a", "model"], "gpt-5.1", True), "gpt-5.1")
        self.assertEqual(drift._redact(["a", "otel"], "127.0.0.1:4318", True), "127.0.0.1:4318")  # the engine's own loopback endpoint stays
        self.assertRegex(drift._redact_codex({"model_providers": {"x": {"base_url": self.QHOST}}})["model_providers"]["x"]["base_url"], r"^sha256:")
        self.assertRegex(drift._redact_codex({"model_providers": {"x": {"query_params": {"k": self.QP}}}})["model_providers"]["x"]["query_params"]["k"], r"^sha256:")
        self.assertRegex(drift._hash_urls(["--proxy", self.QHOST])[1], r"^sha256:")
        for k, v in (("config.providers.zz.base_url", self.CHOST), ("codex.effective.model_providers.hosty.base_url", self.QHOST),
                     ("codex.effective.model_providers.side.query_params.api-version", self.QP), ("manifest.x.seat.argv", self.QHOST)):
            self.assertEqual(drift._display(k, v), "<set>", k)                                   # a plain (old-baseline) value never prints

    def test_99b_stored_and_printed_output_never_carries_them(self):
        self.plant_more(self.QP, self.QHOST, self.CHOST)
        old = self.recorded()
        s = self.snap()
        for bad in self.LEAKS2:
            self.assertNotIn(bad, json.dumps(s), "stored snapshot")
        it = {k: v["value"] for k, v in s["items"].items()}
        for k in ("config.providers.zz.base_url", "config.providers.openrouter.query_params.v",
                  "codex.effective.model_providers.hosty.base_url", "codex.effective.model_providers.side.query_params.api-version"):
            self.assertRegex(it[k], r"^sha256:[0-9a-f]{8}$", k)
        legacy = copy.deepcopy(s)                                    # a baseline written before the rule: plain values, same canonical hash, no alarm
        legacy["items"]["codex.effective.model_providers.hosty.base_url"]["value"] = self.QHOST
        legacy["items"]["config.providers.zz.base_url"]["value"] = self.CHOST
        self.assertEqual(drift.diff(legacy, self.snap()), [])
        for bad in self.LEAKS2:
            self.assertNotIn(bad, drift.report(legacy, self.snap())[0])
        self.plant(self.CURL, self.CHDR, self.XURL, self.XHDR)
        self.plant_more("qp-tenant-secret-92", "other-box.lan:8081", "cfg-box2.lan:9091")   # values change: still alarms, prints nothing
        new = self.snap()
        ch = drift.diff(s, new)
        self.assertTrue({"config.providers.zz.base_url", "codex.effective.model_providers.hosty.base_url"} <= {c["key"] for c in ch})
        text = drift.report(s, new)[0] + "".join(drift.change_line(c) for c in ch) + json.dumps(ch) + json.dumps(new)
        c = self.cfg()
        drift.accept(c, drift.collect(c), "test", None)
        text += self.files_text()
        for bad in self.LEAKS2 + ("other-box", "cfg-box2", "tenant-secret-92"):
            self.assertNotIn(bad, text)


class Severity(Base):
    """Council perf review 2026-11-11 (P9): endpoint churn and roster-overlay churn are informational in preflight,
    real model / version drift stays a WARN. `diff` itself is unchanged (still exhaustive)."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(preflight, "free_stage", lambda cfg: [Check("claude", "free", "version", "PASS", "x")])
        p.start()
        self.addCleanup(p.stop)

    def pf(self, cfg=None):
        ok, checks = preflight.run(cfg or self.cfg(), free_only=True)
        self.assertTrue(ok)
        return {c.check: c for c in checks if c.provider == "drift"}

    def warns(self, rows):
        return {k for k, r in rows.items() if r.result == "WARN"}

    def test_60_endpoint_churn_is_one_pass_row_not_warns(self):
        self.pf()
        eps = self.w.endpoints["deepseek/deepseek-v4-pro-0813"]
        eps[1]["max_completion_tokens"] = 140000                    # baseten retunes (still fits)
        eps.append(or_endpoint("novita", 200000))                   # novita joins
        self.w.endpoints["z-ai/glm-5.3"].pop(2)                     # venice leaves
        rows = self.pf()
        self.assertEqual(self.warns(rows), set())
        self.assertEqual(rows["drift:info"].result, "PASS")
        self.assertIn("OpenRouter endpoint", rows["drift:info"].detail)
        self.assertGreaterEqual(len(drift.diff(drift.load_last(self.cfg()), self.snap())), 3)    # diff still sees them

    def test_61_nothing_fits_the_budget_still_warns(self):
        self.pf()
        for e in self.w.endpoints["deepseek/deepseek-v4-pro-0813"]:
            e["max_completion_tokens"] = 1000                       # no endpoint fits the seat budget any more
        rows = self.pf()
        self.assertIn("drift:openrouter.endpoints.deepseek/deepseek-v4-pro-0813.routable_for_budget", self.warns(rows))

    def test_62_model_level_cli_and_catalog_drift_stay_warns(self):
        self.pf()
        self.w.versions["claude"] = "2.1.321 (Claude Code)"
        self.w.mct["deepseek/deepseek-v4-pro-0813"] = 65536
        self.w.debug_models["models"][0]["context_window"] = 123
        self.w.labels.remove("Gemini 3.8 Flash (Low)")
        self.w.endpoints["z-ai/glm-5.3"].pop(2)                     # plus informational churn alongside
        rows = self.pf()
        self.assertEqual(self.warns(rows), {"drift:claude.version", "drift:openrouter.models.deepseek/deepseek-v4-pro-0813",
                                            "drift:codex.debug_models.gpt-6-astra", "drift:agy.labels"})
        self.assertIn("drift:info", rows)

    def test_63_pinned_model_id_disappearing_is_a_warn(self):
        self.pf()
        del self.w.mct["z-ai/glm-5.3"]
        rows = self.pf()
        self.assertIn("drift:openrouter.models.z-ai/glm-5.3", self.warns(rows))

    def test_64_new_unclassified_field_name_still_warns(self):
        self.pf()
        self.w.endpoints["z-ai/glm-5.3"][0]["brand_new_field"] = 1
        rows = self.pf()
        self.assertEqual(self.warns(rows), {"drift:openrouter.endpoints.z-ai/glm-5.3.unclassified_fields"})

    def swapped(self, model=None):
        """Roster overlay: the claude seat replaced by a fable seat; every other seat as in the base roster."""
        seats = [dict(s, id="fable", model=model or "claude-fable-5-2") if s["id"] == "claude" else s
                 for s in self.cfg()["seat"]]
        return config.load(overlays=[self.ov, {"seat": seats}])

    def test_65_roster_overlay_is_informational(self):
        self.pf()
        c = self.swapped()
        self.assertTrue(c.get("_roster_overlay"))
        self.assertFalse(self.cfg().get("_roster_overlay"))         # an overlay with no [[seat]] sets no flag
        kinds = {x["key"]: x["kind"] for x in drift.diff(drift.load_last(c), drift.collect(c))}
        self.assertEqual((kinds["roster.fable"], kinds["roster.claude"]), ("NEW", "GONE"))
        rows = self.pf(c)
        self.assertEqual(self.warns(rows), set(), sorted(rows))
        self.assertIn("roster/overlay", rows["drift:info"].detail)

    def test_66_without_a_roster_overlay_a_roster_swap_warns(self):
        self.pf()
        c = self.swapped()
        c.pop("_roster_overlay")                                    # same roster, but not from an overlay
        self.assertIn("drift:roster.fable", self.warns(self.pf(c)))

    def test_67_changed_seat_in_both_rosters_stays_a_warn_under_an_overlay(self):
        self.pf()
        seats = [dict(s, model="gpt-6-other") if s["id"] == "codex" else s for s in self.cfg()["seat"]]
        rows = self.pf(config.load(overlays=[self.ov, {"seat": seats}]))
        self.assertIn("drift:roster.codex", self.warns(rows))
        self.assertIn("drift:config.seat.codex.model", self.warns(rows))


class Sticky(Base):
    """Row 26 (D19): a preflight observes, only `drift --accept` moves the baseline."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(preflight, "free_stage", lambda cfg: [Check("claude", "free", "version", "PASS", "x")])
        p.start()
        self.addCleanup(p.stop)
        self.last = os.path.join(self.state, "drift", "last.json")
        self.hist = os.path.join(self.state, "drift", "history.jsonl")

    def pf(self):
        ok, checks = preflight.run(self.cfg(), free_only=True)
        self.assertTrue(ok)
        return {c.check: c for c in checks if c.provider == "drift"}

    def history(self):
        return [json.loads(x) for x in read(self.hist).splitlines()]

    def two_changes(self):
        self.pf()                                              # seeds the baseline
        self.w.versions["claude"] = "2.1.311 (Claude Code)"
        self.w.versions["codex"] = "codex-cli 0.170.0"
        return {"drift:claude.version", "drift:codex.version"}

    def test_50_alarm_survives_two_preflights_and_the_baseline_file_never_moves(self):
        want = self.two_changes()
        seeded = read(self.last)
        for n in range(3):
            rows = self.pf()
            self.assertEqual({k for k, r in rows.items() if r.result == "WARN"}, want, "preflight %d" % n)
            self.assertIn("council.py drift --accept", rows["drift:claude.version"].detail)
            self.assertEqual(read(self.last), seeded)           # byte for byte
        hist = self.history()
        self.assertEqual([h["kind"] for h in hist], ["seed", "observe", "observe", "observe"])
        self.assertEqual([h["baseline_moved"] for h in hist], [True, False, False, False])
        self.assertEqual({c["key"] for c in hist[-1]["changes"]} >= {"claude.version", "codex.version"}, True)

    def test_51_accept_all_clears_the_alarm_and_stores_raw_values(self):
        self.two_changes()
        rc, out, err = self.cli("drift", "--config", self.ov, "--accept")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("accepted 2: claude.version, codex.version", out)
        last = drift.load_last(self.cfg())
        self.assertEqual(last["trigger"], "drift --accept")
        self.assertEqual(last["items"]["claude.version"], self.snap()["items"]["claude.version"])    # raw, not canonical
        self.assertEqual({r.result for r in self.pf().values()}, {"PASS"})
        self.assertEqual(self.history()[-2]["kind"], "accept")
        self.assertEqual(self.history()[-2]["accepted"], ["claude.version", "codex.version"])

    def test_52_listed_keys_drop_out_the_rest_stay_raised(self):
        self.two_changes()
        rc, out, err = self.cli("drift", "--config", self.ov, "--accept", "claude.version")
        self.assertEqual(rc, 1, out)                                   # codex.version is still raised
        self.assertIn("accepted 1: claude.version", out)
        self.assertIn("still raised (1)", out)
        rows = self.pf()
        self.assertEqual({k for k, r in rows.items() if r.result == "WARN"}, {"drift:codex.version"})
        rc, out, _ = self.cli("drift", "--config", self.ov, "--accept", "codex.*")      # `*` wildcard
        self.assertEqual(rc, 0, out)
        self.assertEqual({r.result for r in self.pf().values()}, {"PASS"})

    def test_53_unknown_key_is_a_usage_error_and_writes_nothing(self):
        self.two_changes()
        before, n = read(self.last), len(self.history())
        rc, out, err = self.cli("drift", "--config", self.ov, "--accept", "claude.version", "nope.key")
        self.assertEqual(rc, 2)
        self.assertIn("nope.key", err)
        self.assertEqual((read(self.last), len(self.history())), (before, n))

    def test_54_plain_drift_and_record_alias(self):
        want = self.two_changes()
        rc, out, _ = self.cli("drift", "--config", self.ov)                  # plain drift never moves it either
        self.assertEqual(rc, 1)
        self.assertIn("until `council.py drift --accept", out)
        self.assertEqual({k for k, r in self.pf().items() if r.result == "WARN"}, want)
        rc, out, _ = self.cli("drift", "--config", self.ov, "--record")      # alias of a bare --accept
        self.assertEqual(rc, 0, out)
        self.assertEqual({r.result for r in self.pf().values()}, {"PASS"})

    def test_55_drift_record_function_no_longer_moves_the_baseline(self):
        self.pf()
        c = self.cfg()
        old = drift.load_last(c)
        self.w.versions["claude"] = "2.1.311 (Claude Code)"
        drift.record(c, drift.collect(c), "test", old)
        self.assertEqual(drift.load_last(c)["items"]["claude.version"], old["items"]["claude.version"])
        self.assertEqual(read(self.last), json.dumps(old, indent=1, sort_keys=True) + "\n")

    def test_56_a_failed_cli_stays_not_collected_after_accept(self):
        self.pf()
        self.w.fail.add("agy")
        self.w.versions["claude"] = "2.1.311 (Claude Code)"
        rc, out, _ = self.cli("drift", "--config", self.ov, "--accept")
        self.assertEqual(rc, 1)                                           # NOT COLLECTED cannot be accepted
        self.assertIn("accepted 1: claude.version", out)
        self.assertIn("NOT COLLECTED agy.version", out)
        self.assertEqual(drift.load_last(self.cfg())["items"]["agy.version"]["value"], "1.4.4")

    def test_57_a_removed_item_is_accepted_as_gone(self):
        self.pf()
        with open(self.ov, "w") as f:
            f.write('state_dir = "%s"\n[providers.codex]\nconfig_path = "%s"\n[[seat]]\nid = "claude"\n'
                    'provider = "claude"\nmodel = "claude-opus-5-5"\n' % (self.state, self.w.codex_toml))
        rows = self.pf()
        self.assertNotIn("drift:roster.agy", rows)               # a roster-replacing overlay: informational, not a WARN
        self.assertEqual(rows["drift:info"].result, "PASS")
        rc, out, _ = self.cli("drift", "--config", self.ov, "--accept", "roster.agy", "agy.*")
        self.assertIn("accepted", out)
        self.assertNotIn("agy.version", drift.load_last(self.cfg())["items"])
        self.assertIn("roster.agy", out.split("accepted")[1].splitlines()[0])


class EngineRoot(unittest.TestCase):
    """Gap 2 of the pre-ship review: launch records map the engine's own root to <engine>, so the drift rows do not
    depend on which copy of the engine computes them (working tree, installed snapshot, a new version, a symlink)."""

    SCRIPT = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from councillib import config, drift\n"
        "cfg = config.load(overlays=[sys.argv[2]])\n"
        "by_p, roster, chair = drift._seats_by_provider(cfg)\n"
        "out = {}\n"
        "def add(key, fn, src):\n"
        "    try:\n"
        "        out[key] = fn()\n"
        "    except Exception as e:\n"
        "        out[key] = 'ERR %s' % type(e).__name__\n"
        "drift._manifest(cfg, [x for l in by_p.values() for x in l], add)\n"
        "print(json.dumps(out, sort_keys=True))\n")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drift-engine-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        skip = shutil.ignore_patterns("__pycache__", "tests", "*.pyc")
        self.a = os.path.join(self.tmp, "copy-a", "council")
        self.b = os.path.join(self.tmp, "copy-b", "deeper", "council")
        shutil.copytree(src, self.a, ignore=skip)
        shutil.copytree(src, self.b, ignore=skip)
        self.link = os.path.join(self.tmp, "link-to-b")
        os.symlink(self.b, self.link)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.ov = os.path.join(self.tmp, "ov.toml")
        with open(self.ov, "w") as f:
            f.write('[[seat]]\nid = "orx"\nprovider = "openrouter"\nmodel = "z-ai/glm-5.3"\n')

    def manifest(self, root):
        import subprocess
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home, "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.run([sys.executable, "-B", "-c", self.SCRIPT, root, self.ov], capture_output=True, text=True,
                           env=env, cwd=self.tmp, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return json.loads(p.stdout)

    def test_01_two_copies_and_a_symlink_give_the_same_launch_records(self):
        ma, mb, ml = self.manifest(self.a), self.manifest(self.b), self.manifest(self.link)
        self.assertEqual(ma, mb)
        self.assertEqual(ma, ml)                                 # abspath (the link) and realpath forms both map
        text = json.dumps(ma)
        for root in (self.a, self.b, self.link, self.tmp):
            self.assertNotIn(root, text)
        self.assertFalse([k for k, v in ma.items() if str(v).startswith("ERR ")])
        self.assertIn("<engine>/councillib/", json.dumps(ma["orx.seat.argv"]))
        self.assertIn("<engine>/councillib/", json.dumps(ma["orx.salvage.argv"]))

    def test_02_an_old_baseline_with_absolute_paths_shows_changed_once(self):
        new_items = {"manifest." + k: {"value": v, "source": "config"} for k, v in self.manifest(self.a).items()}
        old_root = "/old/engine/council"
        old_items = json.loads(json.dumps(new_items).replace("<engine>", old_root))
        ch = drift.diff({"items": old_items}, {"items": new_items, "missing": []})
        self.assertEqual(sorted(c["key"] for c in ch), ["manifest.orx.salvage.argv", "manifest.orx.seat.argv"])
        self.assertEqual({c["kind"] for c in ch}, {"CHANGED"})
        self.assertEqual(drift.diff({"items": new_items}, {"items": new_items, "missing": []}), [])

    def test_03_mapping_is_at_a_path_boundary_only(self):
        rx = drift._engine_re()
        root = os.path.dirname(providers.HERE)
        self.assertEqual(rx.sub("<engine>", root + "/councillib/x.py"), "<engine>/councillib/x.py")
        self.assertEqual(rx.sub("<engine>", "--script=" + root), "--script=<engine>")
        self.assertEqual(rx.sub("<engine>", root + "-other/x"), root + "-other/x")
        self.assertEqual(rx.sub("<engine>", root + ".bak"), root + ".bak")
        self.assertEqual(drift._engine_tree({"a": [root + "/p", 3, None], "b": {"c": root}}),
                         {"a": ["<engine>/p", 3, None], "b": {"c": "<engine>"}})


if __name__ == "__main__":
    unittest.main()
