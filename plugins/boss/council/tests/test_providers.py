"""Adapter tests: claude and codex (row 7), agy, qwen and openrouter (row 8). Fixtures, stub
binaries and a localhost SSE server only: no internet, no paid call, no real settings. Live
canaries are recorded in the row results, not here.

Run: python3 -Es -m unittest discover -s ~/.claude/council/tests -t ~/.claude/council
"""
import http.server
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from councillib import config, providers  # noqa: E402
from councillib.supervise import Launch, Limits, run_one  # noqa: E402

PY = sys.executable or "/usr/bin/python3"
STUB = os.path.join(ROOT, "tests", "stub_cli.py")
FIX = os.path.join(ROOT, "tests", "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def feed_all(ex, data):
    for line in data.splitlines(keepends=True):
        ex.feed(line)
    return ex


def lim(ceiling=1200.0, mode="stream"):
    return Limits(mode=mode, idle_s=240, ceiling_s=ceiling, deadline_s=None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prov-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = config.load()

    def script(self, name, body):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return p

    def cfg_with(self, provider, **kw):
        cfg = config.deep_merge(self.cfg, {"providers": {provider: kw}})
        return cfg

    def checks(self, adapter, cfg):
        return {c.check: c for c in adapter.free_checks(cfg["seat"], cfg)}


class ClaudeExtractorTests(Base):
    def test_01_fixture_replays_to_final_text(self):
        ex = feed_all(providers.ClaudeExtractor(), fixture("claude.ndjson"))
        self.assertEqual(ex.final_text(), "OK")
        self.assertEqual(ex.model_reported(), "claude-opus-5-5")
        self.assertIsNone(ex.error())
        self.assertFalse(ex.salvage_exit())
        # the thinking_tokens liveness events are in the fixture and are ignored
        self.assertGreaterEqual(fixture("claude.ndjson").count(b'"thinking_tokens"'), 3)

    def test_02_half_cut_gives_partial_not_final(self):
        lines = fixture("claude-cap.ndjson").splitlines(keepends=True)
        n = next(i for i, l in enumerate(lines) if b'"text_delta"' in l)
        cut = b"".join(lines[:n + 4]) + lines[n + 4][:25]          # ends mid-JSON
        ex = feed_all(providers.ClaudeExtractor(), cut)
        self.assertIsNone(ex.final_text())
        self.assertGreater(len(ex.partial_text()), 10)
        self.assertEqual(ex.model_reported(), "claude-opus-5-5")

    def test_03_output_cap_keeps_the_streamed_answer(self):
        ex = feed_all(providers.ClaudeExtractor(), fixture("claude-cap.ndjson"))
        text = ex.final_text()
        self.assertTrue(text.startswith(ex.partial_text().rstrip()[:40]))
        self.assertIn("[TRUNCATED", text)
        self.assertNotIn("API Error", text)           # the error message is not the answer
        self.assertIsNone(ex.error())
        self.assertTrue(ex.salvage_exit())

    def test_03b_recovered_cap_hit_returns_every_segment_not_just_the_result_text(self):
        # Claude Code continued after the cap; the result event holds only the last request.
        data = fixture("claude-cap-recovered.ndjson")
        ex = feed_all(providers.ClaudeExtractor(), data)
        self.assertEqual(ex.capped_requests, 1)
        last = json.loads(data.splitlines()[-1])["result"]
        text = ex.final_text()
        self.assertTrue(text.endswith(last), text)
        self.assertTrue(text.startswith("# The Long P"), text)   # the first request's text is in there too
        self.assertNotIn("[TRUNCATED", text)                   # the continuation finished: end_turn
        self.assertFalse(ex.salvage_exit())
        self.assertIsNone(ex.error())

    def test_04_error_result_without_text_is_an_error(self):
        ev = {"type": "result", "subtype": "success", "is_error": True, "api_error": "max_output_tokens",
              "terminal_reason": "api_error", "result": "API Error: cap"}
        ex = feed_all(providers.ClaudeExtractor(), json.dumps(ev).encode() + b"\n")
        self.assertIsNone(ex.final_text())
        self.assertIn("claude_success", ex.error())
        self.assertFalse(ex.salvage_exit())

    @staticmethod
    def _ev(kind, **d):
        return json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                             "delta": dict({"type": kind}, **d)}}).encode() + b"\n"

    def test_04b_thinking_summary_is_kept_as_the_labelled_partial_when_there_is_no_answer(self):
        # row 13: Opus spent 32k tokens thinking and left nothing. With display "summarized" the
        # summary arrives as thinking_delta; it must survive a capped, answerless run.
        res = {"type": "result", "subtype": "success", "is_error": True, "api_error": "max_output_tokens",
               "terminal_reason": "api_error", "result": "API Error: cap"}
        data = (self._ev("thinking_delta", thinking="Part 1 looks true; ") +
                self._ev("thinking_delta", thinking="checking n=4.") + json.dumps(res).encode() + b"\n")
        ex = feed_all(providers.ClaudeExtractor(), data)
        self.assertIsNone(ex.final_text())
        self.assertFalse(ex.salvage_exit())              # reasoning is not an answer
        self.assertTrue(ex.partial_text().startswith(providers.THINKING_NOTE))
        self.assertIn("Part 1 looks true; checking n=4.", ex.partial_text())
        self.assertIn("claude_success", ex.error())

    def test_04c_thinking_never_leaks_into_the_answer(self):
        data = (self._ev("thinking_delta", thinking="secret reasoning") + self._ev("text_delta", text="The answer.") +
                json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "The answer."}).encode() + b"\n")
        ex = feed_all(providers.ClaudeExtractor(), data)
        self.assertEqual(ex.final_text(), "The answer.")
        self.assertEqual(ex.partial_text(), "The answer.")
        self.assertEqual(ex.thinking_text(), "secret reasoning")

    def test_05_replay_through_supervisor_survives_idle_clock(self):
        path = os.path.join(FIX, "claude.ndjson")
        launch = Launch(argv=[PY, "-Es", STUB, "replay", path, "0.3"], env=dict(os.environ), cwd=self.tmp)
        res = run_one(launch, Limits(mode="stream", idle_s=2, ceiling_s=60, deadline_s=None))
        self.assertEqual(res.status, "ok", res)
        self.assertLess(res.max_gap_s, 2)
        ex = feed_all(providers.ClaudeExtractor(), res.stdout)
        self.assertEqual(ex.final_text(), "OK")


class ClaudeBuildTests(Base):
    def build(self, seat, ceiling=1200.0, cfg=None):
        s = dict({"id": "claude", "provider": "claude", "model": "claude-opus-5-5"}, **seat)
        return providers.get("claude").build(s, "/p/prompt.md", "/w/dir", lim(ceiling), cfg or self.cfg)

    def test_06_argv_and_advisor_budget(self):
        la = self.build({})
        a = la.argv
        self.assertEqual(a[:3], ["claude", "-p", "--model"])
        for flag in ("--verbose", "--include-partial-messages", "--no-session-persistence", "--safe-mode"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--tools") + 1], "")
        self.assertEqual(a[a.index("--effort") + 1], "medium")
        self.assertEqual(a[a.index("--thinking-display") + 1], "summarized")       # row 14: -p would force "omitted"
        self.assertNotIn("--max-thinking-tokens", a)         # opus-5-5 is adaptive: the flag would be inert (config says so)
        for bad in ("--bare", "--max-budget-usd"):
            self.assertNotIn(bad, a)
        self.assertEqual((la.stdin_path, la.cwd, la.final_text_path), ("/p/prompt.md", "/w/dir", None))
        self.assertEqual(la.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "128000")     # the model's real maximum
        self.assertEqual(la.env["CLAUDE_CODE_MAX_RETRIES"], "1")

    def test_06b_tokens_form_sends_a_thinking_budget_of_max_minus_reserve(self):
        cfg = self.cfg_with("claude", reasoning_form="tokens", ignores_reasoning_cap=False)
        a = self.build({}, cfg=cfg).argv
        self.assertEqual(a[a.index("--max-thinking-tokens") + 1], "112000")          # 128000 - 16000
        self.assertEqual(a[a.index("--effort") + 1], "medium")                       # effort stays alongside
        a = self.build({"answer_reserve_tokens": 28000}, cfg=cfg).argv
        self.assertEqual(a[a.index("--max-thinking-tokens") + 1], "100000")
        a = self.build({"reasoning_max_tokens": 50000}, cfg=cfg).argv
        self.assertEqual(a[a.index("--max-thinking-tokens") + 1], "50000")
        with self.assertRaises(ValueError):                                          # explicit cap eats the reserve
            self.build({"reasoning_max_tokens": 120000}, cfg=cfg)
        with self.assertRaises(ValueError):                                          # no room beyond the reserve
            self.build({"max_output_tokens": 16000}, cfg=cfg)

    def test_06c_leftover_reasoning_ratio_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self.build({}, cfg=self.cfg_with("claude", reasoning_ratio=0.75))
        self.assertIn("answer_reserve_tokens", str(cm.exception))

    def test_07_inner_timeouts_follow_the_launch_ceiling(self):
        seat = self.build({}, 1200.0).env
        chair = self.build({"id": "chair"}, 1500.0).env
        self.assertEqual(seat["API_TIMEOUT_MS"], "1260000")
        self.assertEqual(seat["CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"], "1200000")
        self.assertGreaterEqual(int(chair["API_TIMEOUT_MS"]), 1500000)
        self.assertGreaterEqual(int(chair["CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"]), 1500000)

    def test_08_chair_gets_the_chair_budget_and_seat_keys_override(self):
        chair = self.build({"id": "chair"}, 1500.0)
        self.assertEqual(chair.argv[chair.argv.index("--effort") + 1], "high")
        self.assertEqual(chair.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "128000")
        role = self.build({"id": "x", "role": "chair"}, 1500.0)
        self.assertEqual(role.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "128000")
        own = self.build({"reasoning_effort": "low", "max_output_tokens": 3000})
        self.assertEqual(own.argv[own.argv.index("--effort") + 1], "low")
        self.assertEqual(own.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "3000")

    def test_09_static_env_can_raise_timeouts_but_not_lower_them(self):
        low = self.cfg_with("claude", env={"API_TIMEOUT_MS": "1000", "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS": "1000"})
        e = self.build({}, 1200.0, low).env
        self.assertEqual(e["API_TIMEOUT_MS"], "1260000")
        high = self.cfg_with("claude", env={"API_TIMEOUT_MS": "9000000"})
        self.assertEqual(self.build({}, 1200.0, high).env["API_TIMEOUT_MS"], "9000000")

    def test_10_scrub_list_is_applied(self):
        os.environ["CLAUDECODE"] = "1"
        self.addCleanup(os.environ.pop, "CLAUDECODE", None)
        self.assertNotIn("CLAUDECODE", self.build({}).env)


class ClaudeChecksTests(Base):
    def claude_bin(self, version="2.1.305", logged_in="true"):
        return self.script("claude", 'case "$1" in\n --version) echo "%s (Claude Code)";;\n'
                           ' auth) echo \'{"loggedIn": %s, "authMethod": "oauth_token", "email": "a@b.c"}\';;\nesac\n'
                           % (version, logged_in))

    def run_checks(self, **kw):
        cfg = self.cfg_with("claude", bin=self.claude_bin(**{k: kw.pop(k) for k in ("version", "logged_in") if k in kw}), **kw)
        return self.checks(providers.get("claude"), cfg), cfg

    def test_11_all_pass_and_no_email_in_detail(self):
        c, _ = self.run_checks()
        self.assertEqual({k: v.result for k, v in c.items()},
                         {"version": "PASS", "auth": "PASS", "inner_caps": "PASS", "output_budget": "PASS",
                          "thinking_display": "PASS"})
        self.assertNotIn("@", " ".join(v.detail for v in c.values()))

    def test_12_old_version_fails(self):
        c, _ = self.run_checks(version="2.1.100")
        self.assertEqual(c["version"].result, "FAIL")

    def test_13_logged_out_fails(self):
        c, _ = self.run_checks(logged_in="false")
        self.assertEqual(c["auth"].result, "FAIL")

    def test_14_inner_caps_fail_when_env_retries_exceed_one(self):
        c, _ = self.run_checks(env={"CLAUDE_CODE_MAX_RETRIES": "5"})
        self.assertEqual(c["inner_caps"].result, "FAIL")
        self.assertIn("RETRIES", c["inner_caps"].detail)

    def test_15_bad_budget_fails(self):
        c, _ = self.run_checks(reasoning_effort="turbo")
        self.assertEqual(c["output_budget"].result, "FAIL")
        c, _ = self.run_checks(max_output_tokens=12)
        self.assertEqual(c["output_budget"].result, "FAIL")

    def test_15b_answer_reserve_is_enforced(self):
        # max_output_tokens - (reasoning cap or 0) must be >= answer_reserve_tokens, for the advisor, the chair and
        # every roster seat
        c, _ = self.run_checks(max_output_tokens=15999)
        self.assertEqual(c["output_budget"].result, "FAIL")
        self.assertIn("answer_reserve_tokens", c["output_budget"].detail)
        c, _ = self.run_checks(chair_max_output_tokens=8000)
        self.assertEqual(c["output_budget"].result, "FAIL")
        self.assertIn("chair", c["output_budget"].detail)
        c, _ = self.run_checks(max_output_tokens=20000, answer_reserve_tokens=20000)
        self.assertEqual(c["output_budget"].result, "PASS")
        c, _ = self.run_checks(max_output_tokens=20000, answer_reserve_tokens=20001)
        self.assertEqual(c["output_budget"].result, "FAIL")
        # a roster seat's own cap counts
        cfg = self.cfg_with("claude", bin=self.claude_bin())
        cfg["seat"] = [dict(s, max_output_tokens=9000) if s["provider"] == "claude" else s for s in cfg["seat"]]
        c = self.checks(providers.get("claude"), cfg)
        self.assertEqual(c["output_budget"].result, "FAIL")
        self.assertIn("seat claude", c["output_budget"].detail)

    def test_15c_cap_marked_ignored_must_use_the_effort_form(self):
        c, _ = self.run_checks(ignores_reasoning_cap=True, reasoning_form="tokens")
        self.assertEqual(c["output_budget"].result, "FAIL")
        self.assertIn("ignores_reasoning_cap", c["output_budget"].detail)
        c, _ = self.run_checks(ignores_reasoning_cap=False, reasoning_form="tokens")
        self.assertEqual(c["output_budget"].result, "PASS")
        c, _ = self.run_checks(reasoning_form="tokens", ignores_reasoning_cap=False, reasoning_max_tokens=120000)
        self.assertEqual(c["output_budget"].result, "FAIL")                 # explicit cap leaves 8000 < 16000

    def test_15d_a_leftover_reasoning_ratio_fails(self):
        c, _ = self.run_checks(reasoning_ratio=0.75)
        self.assertEqual(c["output_budget"].result, "FAIL")
        self.assertIn("reasoning_ratio", c["output_budget"].detail)
        cfg = self.cfg_with("claude", bin=self.claude_bin())
        cfg["reasoning_ratio"] = 0.75                                        # the old top-level key
        self.assertEqual(self.checks(providers.get("claude"), cfg)["output_budget"].result, "FAIL")

    def test_15e_thinking_display_must_be_summarized(self):
        for kw in ({"thinking_display": "omitted"}, {"thinking_display": "highlights"},
                   {"extra_args": ["--thinking-display", "omitted"]},
                   {"extra_args": ["--thinking-display=omitted"]},
                   {"extra_args": ["--thinking", "disabled"]},
                   {"extra_args": ["--max-thinking-tokens", "0"]}):
            c, _ = self.run_checks(**kw)
            self.assertEqual(c["thinking_display"].result, "FAIL", kw)
        c, _ = self.run_checks(extra_args=["--thinking-display", "summarized"])
        self.assertEqual(c["thinking_display"].result, "PASS")
        cfg = self.cfg_with("claude", bin=self.claude_bin())
        cfg["seat"] = [dict(s, thinking_display="omitted") if s["provider"] == "claude" else s for s in cfg["seat"]]
        self.assertEqual(self.checks(providers.get("claude"), cfg)["thinking_display"].result, "FAIL")


class CodexTests(Base):
    def build(self, seat=None, cfg=None, canary=False):
        cfg = dict(cfg or self.cfg)
        if canary:
            cfg["_canary"] = True
        s = dict({"id": "codex", "provider": "codex", "model": "gpt-6-astra"}, **(seat or {}))
        return providers.get("codex").build(s, "/p/prompt.md", "/w/dir", lim(1200.0, "buffered"), cfg)

    def test_16_extractor_replays_fixture(self):
        ex = feed_all(providers.CodexExtractor(), fixture("codex.jsonl"))
        self.assertEqual(ex.final_text(), "ANSWER: 3984")
        self.assertIsNone(ex.error())
        self.assertIsNone(ex.model_reported())

    def test_17_turn_failed_is_an_error_without_text(self):
        ex = feed_all(providers.CodexExtractor(), fixture("codex-failed.jsonl"))
        self.assertIsNone(ex.final_text())
        self.assertIn("idle timeout", ex.error())

    def test_18_half_cut_codex_gives_nothing_final(self):
        data = fixture("codex.jsonl").splitlines(keepends=True)
        ex = feed_all(providers.CodexExtractor(), b"".join(data[:2]) + data[2][:30])
        self.assertIsNone(ex.final_text())
        self.assertEqual(ex.partial_text(), "")

    def test_19_argv_uses_last_file_council_provider_and_effort(self):
        la = self.build()
        a = la.argv
        self.assertEqual(a[:3], ["codex", "exec", "--json"])
        self.assertEqual(a[-1], "-")
        self.assertEqual(la.final_text_path, "/w/dir/last.txt")
        self.assertEqual(a[a.index("-o") + 1], "/w/dir/last.txt")
        self.assertIn("model_provider=openai-council", a)
        self.assertIn("model_reasoning_effort=medium", a)
        self.assertEqual(a[a.index("-m") + 1], "gpt-6-astra")
        self.assertFalse([x for x in a if "max_output" in x or "max-budget" in x])

    def test_20_canary_is_low_effort_and_chair_is_high(self):
        self.assertIn("model_reasoning_effort=low", self.build(canary=True).argv)
        self.assertIn("model_reasoning_effort=high", self.build({"id": "chair"}).argv)
        self.assertIn("model_reasoning_effort=xhigh", self.build({"reasoning_effort": "xhigh"}).argv)

    def codex_cfg(self, toml_text, ver="0.163.0", login="Logged in using ChatGPT", **kw):
        conf = os.path.join(self.tmp, "codex.toml")
        with open(conf, "w") as f:
            f.write(toml_text)
        binp = self.script("codex", 'case "$1" in\n --version) echo "codex-cli %s";;\n login) echo "%s";;\nesac\n' % (ver, login))
        return self.cfg_with("codex", bin=binp, config_path=conf, **kw)

    BLOCK = ('check_for_update_on_startup = false\n[model_providers.openai-council]\n'
             'requires_openai_auth = true\nrequest_max_retries = 1\nstream_max_retries = 0\n'
             'stream_idle_timeout_ms = 3600000\n')

    def test_21_checks_pass_with_a_good_council_block(self):
        c = self.checks(providers.get("codex"), self.codex_cfg(self.BLOCK))
        self.assertEqual({k: v.result for k, v in c.items()},
                         {"version": "PASS", "auth": "PASS", "update_check": "PASS",
                          "inner_caps": "PASS", "output_budget": "PASS"})
        self.assertIn("no output-token cap", c["output_budget"].detail)

    def test_22_inner_caps_fail_when_idle_is_below_the_ceiling(self):
        c = self.checks(providers.get("codex"), self.codex_cfg(self.BLOCK.replace("3600000", "300000")))
        self.assertEqual(c["inner_caps"].result, "FAIL")
        c = self.checks(providers.get("codex"), self.codex_cfg(self.BLOCK.replace("stream_max_retries = 0", "stream_max_retries = 5")))
        self.assertEqual(c["inner_caps"].result, "FAIL")

    def test_23_missing_block_fails_unless_limits_are_accepted(self):
        text = "check_for_update_on_startup = false\n"
        c = self.checks(providers.get("codex"), self.codex_cfg(text))
        self.assertEqual(c["inner_caps"].result, "FAIL")
        c = self.checks(providers.get("codex"), self.codex_cfg(text, accepted_limits=["stream_idle_timeout_ms=300000"]))
        self.assertEqual(c["inner_caps"].result, "WARN")
        self.assertIn("stream_idle_timeout_ms=300000", c["inner_caps"].detail)

    def test_24_version_pin_login_and_update_check(self):
        c = self.checks(providers.get("codex"), self.codex_cfg(self.BLOCK, ver="0.164.0"))
        self.assertEqual(c["version"].result, "FAIL")           # exact pin
        c = self.checks(providers.get("codex"), self.codex_cfg(self.BLOCK, login="Not logged in"))
        self.assertEqual(c["auth"].result, "FAIL")
        c = self.checks(providers.get("codex"), self.codex_cfg(self.BLOCK.replace("false", "true", 1)))
        self.assertEqual(c["update_check"].result, "WARN")

    # C4: a codex seat must not load the user's own MCP servers (codex 0.163.0: only the per-server `-c` works)
    MCP = ('[mcp_servers.alpha]\nurl = "https://private.example.invalid/mcp"\n'
           '[mcp_servers.alpha.http_headers]\nAuthorization = "Bearer sk-fixture-secret"\n'
           '[mcp_servers.beta-2]\ncommand = "true"\n')

    def cs(self, cfg):
        a = self.build(cfg=cfg).argv
        return [a[i + 1] for i, x in enumerate(a) if x == "-c" and a[i + 1].startswith("mcp_servers")], a

    def test_24a_each_user_mcp_server_is_disabled_before_extra_args(self):
        cfg = self.codex_cfg(self.BLOCK + self.MCP, extra_args=["-c", "x=1"])
        got, a = self.cs(cfg)
        self.assertEqual(got, ["mcp_servers.alpha.enabled=false", "mcp_servers.beta-2.enabled=false"])
        self.assertLess(a.index("mcp_servers.beta-2.enabled=false"), a.index("x=1"))     # the user's own -c still wins
        self.assertNotIn("mcp_servers={}", a)                                             # the form that is a no-op

    def test_24b_no_config_or_no_servers_adds_nothing(self):
        self.assertEqual(self.cs(self.cfg_with("codex", config_path=os.path.join(self.tmp, "absent.toml")))[0], [])
        self.assertEqual(self.cs(self.codex_cfg(self.BLOCK))[0], [])
        self.assertEqual(self.cs(self.codex_cfg(self.BLOCK + self.MCP, disable_user_mcp=False))[0], [])

    def test_24c_a_name_codex_cannot_address_is_never_emitted_but_warned(self):
        # codex splits the -c key on "." and keeps quotes: emitting one aborts the whole launch
        cfg = self.codex_cfg(self.BLOCK + self.MCP + '[mcp_servers."a.b"]\ncommand = "true"\n')
        got, a = self.cs(cfg)
        self.assertEqual(got, ["mcp_servers.alpha.enabled=false", "mcp_servers.beta-2.enabled=false"])
        self.assertFalse([x for x in a if "a.b" in x])
        row = self.checks(providers.get("codex"), cfg)["mcp_servers"]
        self.assertEqual(row.result, "WARN")
        self.assertIn("a.b", row.detail)
        self.assertNotIn("private.example", row.detail)
        self.assertNotIn("mcp_servers", self.checks(providers.get("codex"), self.codex_cfg(self.BLOCK + self.MCP)))


class RegistryTests(Base):
    def test_25_real_adapters_registered_with_families(self):
        self.assertEqual((providers.get("claude").family, providers.get("codex").family), ("anthropic", "openai"))
        self.assertTrue(providers.is_anthropic({"provider": "claude", "model": "x"}))

    def test_26_budget_precedence(self):
        pc = {"reasoning_effort": "a", "max_output_tokens": 1, "chair_reasoning_effort": "b", "chair_max_output_tokens": 2}
        self.assertEqual(providers.budget({"id": "s"}, pc), ("a", 1))
        self.assertEqual(providers.budget({"id": "chair"}, pc), ("b", 2))
        self.assertEqual(providers.budget({"id": "chair", "max_output_tokens": 9}, pc), ("b", 9))
        self.assertEqual(providers.budget({"id": "s", "reasoning_effort": "z"}, pc), ("z", 1))
        self.assertEqual(providers.budget({"id": "chair"}, {"reasoning_effort": "a"}), ("a", None))



# ====================================================================== row 8
FAKE_KEY = "sk-or-v1-TESTSECRET0000000000000000"


class AgyTests(Base):
    def build(self, prompt, seat=None, ceiling=1200.0):
        p = os.path.join(self.tmp, "p.prompt.md")
        with open(p, "w") as f:
            f.write(prompt)
        wd = os.path.join(self.tmp, "wd")
        os.makedirs(wd, exist_ok=True)
        s = dict({"id": "agy", "provider": "agy", "model": "Gemini 3.8 Flash (Medium)"}, **(seat or {}))
        return providers.get("agy").build(s, p, wd, lim(ceiling, "buffered"), self.cfg), wd

    def test_30_fixture_extracts_to_final_text(self):
        ex = feed_all(providers.AgyExtractor(), fixture("agy.ndjson"))
        self.assertEqual(ex.final_text().strip(), "OK")
        self.assertEqual(ex.model_reported(), "Gemini 3.8 Flash (Medium)")
        self.assertIsNone(ex.error())
        self.assertTrue(providers.get("agy").model_matches("Gemini 3.8 Flash (Medium)", ex.model_reported()))

    def test_31_error_result_with_exit_zero_is_an_error_and_keeps_every_step(self):
        ex = feed_all(providers.AgyExtractor(), fixture("agy-error.ndjson"))
        self.assertIsNone(ex.final_text())
        self.assertIn("agy_error", ex.error())
        self.assertIn("output token limit", ex.error())
        part = ex.partial_text()              # step 1 (cut off) and step 3 (the retry), not result.response alone
        self.assertGreater(len(part), len(ex.result_event()["response"]))
        self.assertTrue(part.startswith(" R R L R L"))

    def test_32_half_cut_stream_gives_partial_not_final(self):
        data = fixture("agy-error.ndjson").splitlines(keepends=True)
        ex = feed_all(providers.AgyExtractor(), b"".join(data[:4]) + data[4][:30])
        self.assertIsNone(ex.final_text())
        self.assertIsNone(ex.error())
        self.assertGreater(len(ex.partial_text()), 5)

    def test_33_small_prompt_is_inline_with_the_directive(self):
        la, wd = self.build("Reply with exactly: OK")
        a = la.argv
        self.assertEqual(a[0], "agy")
        self.assertEqual(a[a.index("--model") + 1], "Gemini 3.8 Flash (Medium)")
        self.assertEqual(a[a.index("--print-timeout") + 1], "1260s")
        self.assertEqual(a[a.index("--output-format") + 1], "stream-json")
        for flag in ("--sandbox", "--dangerously-skip-permissions"):
            self.assertIn(flag, a)
        self.assertNotIn("--effort", a)                  # rejected for Gemini 3.8 labels
        self.assertNotIn("--add-dir", a)
        printed = a[a.index("--print") + 1]
        self.assertTrue(printed.startswith(providers.AGY_INLINE_DIRECTIVE))
        self.assertTrue(printed.endswith("Reply with exactly: OK"))
        self.assertIsNone(la.stdin_path)
        self.assertEqual(la.cwd, wd)

    def test_34_130_kib_prompt_goes_through_a_file_in_the_seat_workdir(self):
        la, wd = self.build("x" * (130 * 1024))
        a = la.argv
        self.assertEqual(a[a.index("--add-dir") + 1], wd)
        printed = a[a.index("--print") + 1]
        self.assertTrue(printed.startswith(providers.AGY_INLINE_DIRECTIVE_FILE))
        self.assertIn(os.path.join(wd, "prompt.md"), printed)
        self.assertEqual(os.path.getsize(os.path.join(wd, "prompt.md")), 130 * 1024)
        self.assertEqual(os.stat(os.path.join(wd, "prompt.md")).st_mode & 0o777, 0o600)
        self.assertLess(max(len(x.encode()) for x in a), 120 * 1024)

    def test_35_the_ceiling_decides_print_timeout(self):
        la, _ = self.build("hi", ceiling=1500.0)
        self.assertEqual(la.argv[la.argv.index("--print-timeout") + 1], "1560s")

    MODELS = ("Fetching available models...\n"
              "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
              "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
              "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n")

    def agy_cfg(self, models=None, version=None, **seat):
        version = version or config.load()["pins"]["agy_seen"]          # the pin in config.toml, so a pin bump needs no edit here
        body = 'case "$1" in\n --version) echo "%s";;\n models) printf \'%%b\' %s;;\nesac\n' % (
            version, json.dumps(models if models is not None else self.MODELS).replace("'", ""))
        binp = self.script("agy", body.replace("\\n", "\\n"))
        cfg = self.cfg_with("agy", bin=binp)
        cfg["seat"] = [dict(s, **seat) if s["provider"] == "agy" else s for s in cfg["seat"]]
        return cfg

    def test_36_checks_pass_with_a_live_label(self):
        cfg = self.agy_cfg()
        c = self.checks(providers.get("agy"), cfg)
        self.assertEqual(c["version"].result, "PASS")
        self.assertEqual(c["auth"].result, "PASS")
        self.assertEqual(c["model:agy"].result, "PASS")
        self.assertEqual(c["inner_caps"].result, "PASS")
        self.assertEqual(c["inner_retries"].result, "WARN")
        self.assertEqual(c["output_budget"].result, "WARN")

    def test_37_bad_pin_fails_and_a_non_gemini_row_fails(self):
        c = self.checks(providers.get("agy"), self.agy_cfg(model="Gemini 3.1 Pro (High)"))
        self.assertEqual(c["model:agy"].result, "FAIL")
        c = self.checks(providers.get("agy"), self.agy_cfg(model="Claude Sonnet 4.6 (Thinking)"))
        self.assertEqual(c["model:agy"].result, "FAIL")
        self.assertIn("not a Gemini", c["model:agy"].detail)

    def test_38_version_drift_is_a_warn_and_an_empty_catalog_fails_auth(self):
        c = self.checks(providers.get("agy"), self.agy_cfg(version="1.5.0"))
        self.assertEqual(c["version"].result, "WARN")
        c = self.checks(providers.get("agy"), self.agy_cfg(models="Fetching available models...\n"))
        self.assertEqual(c["auth"].result, "FAIL")
        self.assertEqual(c["model:agy"].result, "SKIP")

    def test_39_model_matches_accepts_either_catalog_column(self):
        self.checks(providers.get("agy"), self.agy_cfg())            # fills the catalog rows
        a = providers.get("agy")
        self.assertTrue(a.model_matches("Gemini 3.8 Flash (Medium)", "gemini-3.8-flash-medium"))
        self.assertTrue(a.model_matches("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"))
        self.assertFalse(a.model_matches("Gemini 3.8 Flash (Medium)", "Gemini 3.8 Flash (High)"))


class QwenTests(Base):
    SETTINGS = os.path.join(FIX, "qwen-settings.json")

    def setUp(self):
        super().setUp()
        self.settings = os.path.join(self.tmp, "settings.json")
        shutil.copy(self.SETTINGS, self.settings)
        os.chmod(self.settings, 0o600)
        self.calls = []
        self._orig = providers.openrouter_get
        providers.openrouter_get = self.fake_get
        self.addCleanup(setattr, providers, "openrouter_get", self._orig)
        self.models = ["qwen/qwen3.6-plus", "qwen/short-timeout", "qwen/many-retries", "qwen/no-key"]
        self.key_status = 200
        os.environ["TEST_QWEN_KEY_NOT_SET"] = ""
        os.environ.pop("TEST_QWEN_KEY_NOT_SET")

    def fake_get(self, path, cfg, key=None, base_url=None, timeout=20):
        self.calls.append((path, base_url, bool(key)))
        if path == "/key":
            return self.key_status, {"data": {}}
        return 200, {"data": [{"id": m} for m in self.models]}

    def qcfg(self, version="0.25.3", **kw):
        binp = self.script("qwen", 'case "$1" in\n --version) echo "%s";;\nesac\n' % version)
        return self.cfg_with("qwen", bin=binp, settings_path=self.settings, **kw)

    def qchecks(self, seat_model="qwen/qwen3.6-plus", **kw):
        cfg = self.qcfg(**kw)
        seat = {"id": "qwen", "provider": "qwen", "model": seat_model}
        return {c.check: c for c in providers.get("qwen").free_checks([seat], cfg)}

    def build(self, seat=None, ceiling=1200.0, idle=240.0, cfg=None, canary=False):
        cfg = dict(cfg or self.qcfg())
        if canary:
            cfg["_canary"] = True
        s = dict({"id": "qwen", "provider": "qwen", "model": "qwen/qwen3.6-plus"}, **(seat or {}))
        wd = os.path.join(self.tmp, "wd")
        os.makedirs(wd, exist_ok=True)
        la = providers.get("qwen").build(s, "/p/prompt.md", wd, Limits("stream", idle, ceiling, None), cfg)
        with open(la.env["QWEN_CODE_SYSTEM_SETTINGS_PATH"]) as f:
            return la, json.load(f), wd

    # ---- extractor
    def test_40_fixture_extracts_and_reports_the_model(self):
        ex = feed_all(providers.QwenExtractor(8000), fixture("qwen.ndjson"))
        self.assertEqual(ex.final_text(), "OK")
        self.assertEqual(ex.model_reported(), "qwen/qwen3.6-plus")
        self.assertIsNone(ex.error())

    def test_41_cap_hit_is_detected_from_usage_and_marked_truncated(self):
        ex = feed_all(providers.QwenExtractor(300), fixture("qwen-cap.ndjson"))
        self.assertIn("[TRUNCATED", ex.final_text())
        ex = feed_all(providers.QwenExtractor(8000), fixture("qwen-cap.ndjson"))
        self.assertNotIn("[TRUNCATED", ex.final_text())

    def test_42_error_result_is_an_error_with_no_text(self):
        ex = feed_all(providers.QwenExtractor(), fixture("qwen-error.ndjson"))
        self.assertIsNone(ex.final_text())
        self.assertIn("qwen_error_during_execution", ex.error())
        self.assertIn("Missing API key", ex.error())

    def test_43_kill_during_reasoning_keeps_the_thinking_text(self):
        data = fixture("qwen.ndjson").splitlines(keepends=True)
        n = next(i for i, l in enumerate(data) if b"text_delta" in l)
        ex = feed_all(providers.QwenExtractor(), b"".join(data[:n]))     # thinking only, no answer, no result
        self.assertIsNone(ex.final_text())
        self.assertTrue(ex.partial_text().startswith(providers.THINKING_NOTE))
        self.assertGreater(len(ex.partial_text()), len(providers.THINKING_NOTE))

    def test_44_replay_through_supervisor_survives_the_idle_clock(self):
        path = os.path.join(FIX, "qwen.ndjson")
        launch = Launch(argv=[PY, "-Es", STUB, "replay", path, "0.2"], env=dict(os.environ), cwd=self.tmp)
        res = run_one(launch, Limits(mode="stream", idle_s=2, ceiling_s=60, deadline_s=None))
        self.assertEqual(res.status, "ok", res)
        self.assertEqual(feed_all(providers.QwenExtractor(), res.stdout).final_text(), "OK")

    # ---- build
    def test_45_argv_has_the_stream_flags_and_never_logging_or_wall_time(self):
        la, _, wd = self.build()
        a = la.argv
        self.assertEqual(os.path.basename(a[0]), "qwen")
        self.assertEqual(a[1:3], ["-o", "stream-json"])
        for flag in ("--include-partial-messages", "--approval-mode"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--approval-mode") + 1], "plan")
        self.assertEqual(a[a.index("-m") + 1], "qwen/qwen3.6-plus")
        for bad in ("--openai-logging", "--max-wall-time", "--yolo", "-y"):
            self.assertNotIn(bad, a)
        self.assertEqual((la.stdin_path, la.cwd), ("/p/prompt.md", wd))
        self.assertNotIn("QWEN_API_KEY", la.env)
        self.assertNotIn("DASHSCOPE_API_KEY", la.env)

    def test_46_overlay_carries_the_budget_and_switches_memory_off(self):
        la, ov, _ = self.build()
        gc = ov["modelProviders"]["openai"][0]["generationConfig"]
        self.assertEqual(gc["samplingParams"]["max_tokens"], 65536)         # the model's real maximum
        self.assertEqual(gc["samplingParams"]["temperature"], 0.3)         # the owner's value survives
        self.assertEqual(gc["extra_body"], {"reasoning": {"effort": "medium"}})      # effort form; no reasoning.max_tokens
        self.assertEqual(ov["memory"], {"enableManagedAutoMemory": False, "enableManagedAutoDream": False})
        entry = ov["modelProviders"]["openai"][0]
        self.assertEqual((entry["id"], entry["envKey"]), ("qwen/qwen3.6-plus", "OPENROUTER_API_KEY_PERSONAL"))
        self.assertEqual(len(ov["modelProviders"]["openai"]), 1)

    def test_47_overlay_never_holds_the_key_and_is_private(self):
        la, _, _ = self.build()
        path = la.env["QWEN_CODE_SYSTEM_SETTINGS_PATH"]
        with open(path) as f:
            text = f.read()
        self.assertNotIn("sk-", text)
        self.assertNotIn("TESTSECRET", text)
        self.assertNotIn('"env"', text)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertNotIn(FAKE_KEY, " ".join(la.argv))

    def test_48_inner_guards_come_from_the_launch_limits(self):
        la, ov, _ = self.build(ceiling=1200.0, idle=240.0)
        self.assertEqual(la.env["QWEN_STREAM_IDLE_TIMEOUT_MS"], "300000")        # idle 240 + 60
        self.assertEqual(la.env["QWEN_STREAM_MAX_LIFETIME_MS"], "1260000")       # ceiling 1200 + 60
        gc = ov["modelProviders"]["openai"][0]["generationConfig"]
        self.assertEqual((gc["timeout"], gc["maxRetries"], gc["streamIdleTimeoutMs"]), (1260000, 1, 300000))
        la2, ov2, _ = self.build(ceiling=1500.0)
        self.assertEqual(la2.env["QWEN_STREAM_MAX_LIFETIME_MS"], "1560000")
        self.assertEqual(ov2["modelProviders"]["openai"][0]["generationConfig"]["timeout"], 1560000)

    def test_49_chair_canary_and_seat_budgets(self):
        _, ov, _ = self.build({"id": "chair"})
        gc = ov["modelProviders"]["openai"][0]["generationConfig"]
        self.assertEqual((gc["samplingParams"]["max_tokens"], gc["extra_body"]["reasoning"]), (65536, {"effort": "high"}))
        _, ov, _ = self.build(canary=True)
        self.assertEqual(ov["modelProviders"]["openai"][0]["generationConfig"]["extra_body"]["reasoning"], {"effort": "low"})
        _, ov, _ = self.build({"reasoning_effort": "xhigh", "max_output_tokens": 3000})
        gc = ov["modelProviders"]["openai"][0]["generationConfig"]
        self.assertEqual((gc["samplingParams"]["max_tokens"], gc["extra_body"]["reasoning"]), (3000, {"effort": "xhigh"}))

    def test_49b_tokens_form_caps_reasoning_at_max_minus_reserve(self):
        def rmax(seat=None, cfg=None):
            _, ov, _ = self.build(seat, cfg=cfg)
            eb = ov["modelProviders"]["openai"][0]["generationConfig"]["extra_body"]["reasoning"]
            return eb
        tok = self.qcfg(reasoning_form="tokens", ignores_reasoning_cap=False)
        self.assertEqual(rmax(cfg=tok), {"max_tokens": 49536})                              # 65536 - 16000, no effort beside it
        self.assertEqual(rmax(cfg=self.qcfg(reasoning_form="tokens", ignores_reasoning_cap=False,
                                            answer_reserve_tokens=30000)), {"max_tokens": 35536})   # provider reserve
        self.assertEqual(rmax({"answer_reserve_tokens": 20000}, cfg=tok), {"max_tokens": 45536})  # seat wins
        glob = dict(tok, answer_reserve_tokens=10000)                                      # top-level default
        self.assertEqual(rmax(cfg=glob), {"max_tokens": 55536})
        self.assertEqual(rmax({"reasoning_max_tokens": 40000}, cfg=tok), {"max_tokens": 40000})
        with self.assertRaises(ValueError):
            rmax({"reasoning_max_tokens": 60000}, cfg=tok)                                 # leaves 5536 < 16000
        with self.assertRaises(ValueError):
            rmax({"max_output_tokens": 16000}, cfg=tok)

    def test_49c_invalid_reserve_form_and_leftover_ratio_are_rejected(self):
        for bad in (0, -1, 1.5, True, "16000", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.build(cfg=self.qcfg(answer_reserve_tokens=bad))
        with self.assertRaises(ValueError):
            self.build(cfg=self.qcfg(reasoning_form="both"))
        with self.assertRaises(ValueError):
            self.build(cfg=self.qcfg(reasoning_ratio=0.75))
        self.assertEqual(self.qchecks(reasoning_ratio=0.75)["output_budget"].result, "FAIL")
        self.assertEqual(self.qchecks(answer_reserve_tokens=-5)["output_budget"].result, "FAIL")
        self.assertEqual(self.qchecks()["output_budget"].result, "PASS")

    def test_49d_no_cap_falls_back_to_effort(self):
        pc = dict(self.qcfg()["providers"]["qwen"])
        pc.pop("max_output_tokens"), pc.pop("chair_max_output_tokens")
        cfg = dict(self.qcfg())
        cfg["providers"] = dict(cfg["providers"], qwen=pc)
        _, ov, _ = self.build(cfg=cfg)
        self.assertEqual(ov["modelProviders"]["openai"][0]["generationConfig"]["extra_body"]["reasoning"],
                         {"effort": "medium"})

    def test_50_unknown_model_is_a_build_error(self):
        with self.assertRaises(KeyError):
            self.build({"model": "qwen/nope"})

    def test_51_the_extractor_gets_the_cap_the_build_chose(self):
        a = providers.get("qwen")
        self.build({"id": "qwen"})
        self.assertEqual(a.extractor({"id": "qwen"})._cap, 65536)
        self.build({"id": "chair"})
        self.assertEqual(a.extractor({"id": "chair"})._cap, 65536)

    # ---- free checks
    def test_52_all_pass_with_the_good_entry(self):
        c = self.qchecks()
        self.assertEqual({k: v.result for k, v in c.items()},
                         {"version": "PASS", "settings_mode": "PASS", "selected_type": "PASS", "auto_update": "PASS",
                          "model_entry": "PASS", "env_key": "PASS", "key": "PASS", "models": "PASS",
                          "reasoning_cap:qwen": "PASS", "salvage_reasoning:qwen": "PASS",
                          "inner_caps": "PASS", "output_budget": "PASS"})
        self.assertIn("max lifetime 7260000ms", c["inner_caps"].detail)   # row 14: ceiling 7200 + 60 s
        self.assertIn("max lifetime", c["inner_caps"].detail)
        self.assertNotIn("TESTSECRET", " ".join(v.detail for v in c.values()))
        self.assertIn(("/key", "https://openrouter.test/api/v1", True), self.calls)

    def test_53_inner_caps_raise_a_short_file_timeout_and_fail_on_retries(self):
        # row 14: the ceiling is 7200 s, far above the owner's file value (1260000 ms). The overlay raises it
        # for every launch (test_48), so a short file value is reported, not failed.
        c = self.qchecks("qwen/short-timeout")["inner_caps"]
        self.assertEqual(c.result, "PASS")
        self.assertIn("120000ms (raised per launch)", c.detail)
        c = self.qchecks("qwen/many-retries")
        self.assertEqual(c["inner_caps"].result, "FAIL")
        self.assertIn("maxRetries", c["inner_caps"].detail)

    def test_54_missing_key_bad_key_and_missing_model_fail(self):
        c = self.qchecks("qwen/no-key")
        self.assertEqual(c["env_key"].result, "FAIL")
        self.assertNotIn("key", c)               # no network call without a key
        self.key_status = 401
        self.assertEqual(self.qchecks()["key"].result, "FAIL")
        self.key_status = 200
        self.models = ["something/else"]
        c = self.qchecks()
        self.assertEqual(c["models"].result, "FAIL")
        self.assertIn("not in /models", c["models"].detail)
        self.assertEqual(self.qchecks("qwen/nope")["model_entry"].result, "FAIL")

    def test_55_pin_mode_and_auto_update(self):
        self.assertEqual(self.qchecks(version="0.26.0")["version"].result, "FAIL")
        os.chmod(self.settings, 0o644)
        self.assertEqual(self.qchecks()["settings_mode"].result, "FAIL")
        with open(self.settings) as f:
            d = json.load(f)
        d["general"]["enableAutoUpdate"] = True
        d["security"]["auth"]["selectedType"] = "qwen-oauth"
        with open(self.settings, "w") as f:
            json.dump(d, f)
        c = self.qchecks()
        self.assertEqual((c["auto_update"].result, c["selected_type"].result), ("WARN", "FAIL"))

    def test_56_bad_budget_fails(self):
        self.assertEqual(self.qchecks(reasoning_effort="turbo")["output_budget"].result, "FAIL")
        self.assertEqual(self.qchecks(max_output_tokens=10)["output_budget"].result, "FAIL")
        self.assertEqual(self.qchecks(max_output_tokens=15000)["output_budget"].result, "FAIL")      # < 16000 reserve

    def test_56b_token_form_needs_a_model_that_advertises_it(self):
        self.models = ["qwen/qwen3.6-plus"]
        c = self.qchecks(reasoning_form="tokens", ignores_reasoning_cap=False)    # /models entry has no supports_max_tokens
        self.assertEqual(c["reasoning_cap:qwen"].result, "FAIL")
        self.assertIn("supports_max_tokens", c["reasoning_cap:qwen"].detail)
        self.assertEqual(self.qchecks(reasoning_form="tokens")["output_budget"].result, "FAIL")   # marked ignoring + tokens
        self.assertEqual(self.qchecks()["reasoning_cap:qwen"].result, "PASS")

    def test_56c_qwen_stream_guards_follow_the_long_ceiling(self):
        # qwen's own max lifetime defaults to 900 s; it is raised to the runner ceiling + 60 s so it never fires first
        la, ov, _ = self.build(ceiling=7200.0)
        self.assertEqual(la.env["QWEN_STREAM_MAX_LIFETIME_MS"], "7260000")
        self.assertGreater(int(la.env["QWEN_STREAM_MAX_LIFETIME_MS"]), 900000)
        self.assertEqual(ov["modelProviders"]["openai"][0]["generationConfig"]["timeout"], 7260000)


class SSEHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    script = []          # list of (delay_s, bytes), set per test
    status = 200
    seen = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        SSEHandler.seen = {"body": json.loads(self.rfile.read(n)), "auth": self.headers.get("Authorization")}
        self.send_response(SSEHandler.status)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for delay, data in SSEHandler.script:
                time.sleep(delay)
                self.wfile.write(data)
                self.wfile.flush()
        except OSError:
            pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "z-ai/glm-5", "reasoning": {"supported_efforts": ["high", "medium", "low"]}}]}).encode()
        self.send_response(200 if self.headers.get("Authorization") == "Bearer " + FAKE_KEY else 401)
        self.end_headers()
        self.wfile.write(body)


class OpenRouterTests(Base):
    def setUp(self):
        super().setUp()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SSEHandler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        SSEHandler.status = 200
        SSEHandler.seen = {}
        os.environ["TEST_OR_KEY"] = FAKE_KEY
        self.addCleanup(os.environ.pop, "TEST_OR_KEY", None)
        providers._OR_CACHE.clear()

    def ocfg(self, **kw):
        return self.cfg_with("openrouter", base_url=self.base, key_env="TEST_OR_KEY", **kw)

    def build(self, seat=None, limits=None, cfg=None, canary=False):
        cfg = dict(cfg or self.ocfg())
        if canary:
            cfg["_canary"] = True
        s = dict({"id": "openrouter", "provider": "openrouter", "model": "z-ai/glm-5"}, **(seat or {}))
        return providers.get("openrouter").build(s, "/p/prompt.md", "/w/dir", limits or lim(1200.0), cfg)

    # ---- extractor
    def test_60_fixture_extracts_the_answer_not_the_reasoning(self):
        ex = feed_all(providers.OpenRouterExtractor(), fixture("openrouter.sse"))
        self.assertEqual(ex.final_text(), "OK")
        self.assertEqual(ex.model_reported(), "z-ai/glm-5")
        self.assertIsNone(ex.error())
        self.assertTrue(providers.get("openrouter").model_matches("z-ai/glm-5", ex.model_reported()))

    def test_61_length_without_an_answer_is_an_error_and_keeps_the_reasoning(self):
        ex = feed_all(providers.OpenRouterExtractor(), fixture("openrouter-length.sse"))
        self.assertIsNone(ex.final_text())
        self.assertIn("finish=length", ex.error())
        self.assertTrue(ex.partial_text().startswith(providers.THINKING_NOTE))

    def test_62_length_with_text_is_a_truncated_answer(self):
        data = (b'data: {"model":"m","choices":[{"delta":{"content":"half an answer"},"finish_reason":null}]}\n\n'
                b'data: {"model":"m","choices":[{"delta":{"content":""},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n')
        ex = feed_all(providers.OpenRouterExtractor(), data)
        self.assertIn("half an answer", ex.final_text())
        self.assertIn("[TRUNCATED", ex.final_text())
        self.assertIsNone(ex.error())

    def test_63_error_chunk_is_an_error_and_the_partial_survives(self):
        ex = feed_all(providers.OpenRouterExtractor(), fixture("openrouter-error.sse"))
        self.assertIn("openrouter_error", ex.error())
        self.assertIn("Provider disconnected", ex.error())
        self.assertEqual(ex.partial_text(), "Partial answer so far")

    def test_64_killed_stream_has_no_final_and_comments_are_ignored(self):
        data = fixture("openrouter.sse").splitlines(keepends=True)
        ex = feed_all(providers.OpenRouterExtractor(), b"".join(data[:6]) + b'data: {"model":"m","choi')
        self.assertIsNone(ex.final_text())
        self.assertIsNone(ex.error())
        self.assertTrue(ex.partial_text().startswith(providers.THINKING_NOTE))

    # ---- build
    def test_65_argv_has_the_budget_and_no_key(self):
        la = self.build()
        a = la.argv
        self.assertEqual(a[1:3], ["-Es", providers.OPENROUTER_SEAT])
        self.assertEqual(a[a.index("--model") + 1], "z-ai/glm-5")
        self.assertEqual(a[a.index("--max-tokens") + 1], "131072")                  # GLM 5 top_provider.max_completion_tokens
        self.assertEqual(a[a.index("--reasoning-effort") + 1], "medium")             # effort form: GLM ignores a token cap
        self.assertNotIn("--reasoning-max-tokens", a)                                # one or the other, never both
        self.assertEqual(a[a.index("--key-env") + 1], "TEST_OR_KEY")
        self.assertNotIn(FAKE_KEY, " ".join(a))
        self.assertEqual((la.stdin_path, la.cwd), ("/p/prompt.md", "/w/dir"))
        chair = self.build({"id": "chair"})
        self.assertEqual(chair.argv[chair.argv.index("--max-tokens") + 1], "131072")
        self.assertEqual(chair.argv[chair.argv.index("--reasoning-effort") + 1], "high")
        self.assertNotIn("--reasoning-max-tokens", chair.argv)
        can = self.build(canary=True)
        self.assertEqual(can.argv[can.argv.index("--reasoning-effort") + 1], "low")
        self.assertNotIn("--reasoning-max-tokens", can.argv)

    def test_65b_tokens_form_reserve_lookup_and_rejection(self):
        def rmax(seat=None, cfg=None):
            a = self.build(seat, cfg=cfg).argv
            self.assertNotIn("--reasoning-effort", a)
            return a[a.index("--reasoning-max-tokens") + 1]
        tok = self.ocfg(reasoning_form="tokens", ignores_reasoning_cap=False)
        self.assertEqual(rmax(cfg=tok), "115072")                                             # 131072 - 16000
        self.assertEqual(rmax(cfg=self.ocfg(reasoning_form="tokens", ignores_reasoning_cap=False,
                                            answer_reserve_tokens=31072)), "100000")          # provider
        self.assertEqual(rmax({"answer_reserve_tokens": 1072}, cfg=tok), "130000")            # seat wins
        self.assertEqual(rmax(cfg=dict(tok, answer_reserve_tokens=21072)), "110000")          # global default
        self.assertEqual(rmax({"max_output_tokens": 20000}, cfg=tok), "4000")
        with self.assertRaises(ValueError):                                                   # no room beyond the reserve
            rmax({"max_output_tokens": 16000}, cfg=tok)
        for bad in (0, -1, 1.5, True, "16000"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.build(cfg=self.ocfg(answer_reserve_tokens=bad))
        with self.assertRaises(ValueError):
            self.build(cfg=self.ocfg(reasoning_ratio=0.75))
        chk = {c.check: c for c in providers.get("openrouter").free_checks([], self.ocfg(reasoning_ratio=0.75))}
        self.assertEqual(chk["output_budget"].result, "FAIL")
        self.assertIn("reasoning_ratio", chk["output_budget"].detail)

    def test_65c_no_cap_falls_back_to_effort(self):
        pc = {k: v for k, v in self.ocfg()["providers"]["openrouter"].items()
              if k not in ("max_output_tokens", "chair_max_output_tokens")}
        cfg = dict(self.ocfg())
        cfg["providers"] = dict(cfg["providers"], openrouter=pc)
        a = self.build(cfg=cfg).argv
        self.assertNotIn("--max-tokens", a)
        self.assertNotIn("--reasoning-max-tokens", a)
        self.assertEqual(a[a.index("--reasoning-effort") + 1], "medium")

    def test_66_socket_timeout_follows_the_mode(self):
        a = self.build(limits=Limits("stream", 240, 1200, None)).argv
        self.assertEqual(a[a.index("--socket-timeout") + 1], "270")            # idle + 30
        a = self.build(limits=Limits("buffered", 240, 1200, 700)).argv
        self.assertEqual(a[a.index("--socket-timeout") + 1], "730")            # deadline + 30
        a = self.build(limits=Limits("stream", 600, 1200, None)).argv
        self.assertEqual(a[a.index("--socket-timeout") + 1], "630")

    # ---- the seat child against a local SSE server
    def run_child(self, script, status=200, idle=5.0, **seat):
        SSEHandler.script, SSEHandler.status = script, status
        la = self.build(seat or None, Limits("stream", idle, 60, None))
        la = Launch(argv=la.argv, env=dict(os.environ), cwd=self.tmp, stdin_path=self._prompt())
        res = run_one(la, Limits("stream", 10, 60, None))
        return res

    def _prompt(self):
        p = os.path.join(self.tmp, "prompt.md")
        with open(p, "w") as f:
            f.write("Reply with exactly: OK")
        return p

    def test_67_child_streams_every_line_and_exits_zero(self):
        res = self.run_child([(0, b": OPENROUTER PROCESSING\n\n"), (0.3, fixture("openrouter.sse"))])
        self.assertEqual((res.status, res.exit_code), ("ok", 0), res)
        ex = feed_all(providers.OpenRouterExtractor(), res.stdout)
        self.assertEqual(ex.final_text(), "OK")
        self.assertIn(b"OPENROUTER PROCESSING", res.stdout)
        body = SSEHandler.seen["body"]
        self.assertEqual((body["stream"], body["max_tokens"], body["reasoning"]), (True, 131072, {"effort": "medium"}))
        self.assertEqual(body["messages"], [{"role": "user", "content": "Reply with exactly: OK"}])
        self.assertEqual(SSEHandler.seen["auth"], "Bearer " + FAKE_KEY)

    def test_67b_request_body_never_carries_effort_and_max_tokens_together(self):
        # extra_args cannot produce both either: the child drops effort whenever a token cap is present
        SSEHandler.script, SSEHandler.status = [(0, fixture("openrouter.sse"))], 200
        la = self.build(limits=Limits("stream", 5, 60, None))
        argv = list(la.argv) + ["--reasoning-max-tokens", "6000"]
        res = run_one(Launch(argv=argv, env=dict(os.environ), cwd=self.tmp, stdin_path=self._prompt()),
                      Limits("stream", 10, 60, None))
        self.assertEqual(res.exit_code, 0, res)
        self.assertEqual(SSEHandler.seen["body"]["reasoning"], {"max_tokens": 6000})

    def test_68_http_error_prints_the_code_only(self):
        res = self.run_child([(0, b'{"error":"bad key sk-or-v1-LEAK"}')], status=401)
        self.assertEqual((res.status, res.exit_code), ("exit_1", 1), res)
        self.assertEqual(res.stderr_tail.strip(), b"openrouter http 401")
        self.assertNotIn(b"LEAK", res.stdout + res.stderr_tail)

    def test_69_stream_without_a_finish_exits_three(self):
        res = self.run_child([(0, b'data: {"model":"m","choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n')])
        self.assertEqual(res.exit_code, 3, res)
        self.assertIn(b"stream ended early", res.stderr_tail)

    def test_70_error_chunk_exits_four_and_keeps_the_stdout(self):
        res = self.run_child([(0, fixture("openrouter-error.sse"))])
        self.assertEqual(res.exit_code, 4, res)
        self.assertIn(b"Partial answer so far", res.stdout)

    def test_71_silent_server_hits_the_socket_timeout_not_the_idle_kill(self):
        SSEHandler.script, SSEHandler.status = [(6, b"data: [DONE]\n\n")], 200
        la = self.build(limits=Limits("stream", 1, 60, None))                    # socket timeout = 1 + 30 s
        a = list(la.argv)
        a[a.index("--socket-timeout") + 1] = "1"                                  # shorten for the test
        res = run_one(Launch(argv=a, env=dict(os.environ), cwd=self.tmp, stdin_path=self._prompt()),
                      Limits("stream", 20, 60, None))
        self.assertEqual(res.exit_code, 5, res)
        self.assertIn(b"read timeout", res.stderr_tail)

    def test_72_missing_key_exits_two_before_any_request(self):
        SSEHandler.script, SSEHandler.seen = [], {}
        la = self.build()
        env = dict(os.environ)
        env.pop("TEST_OR_KEY")
        res = run_one(Launch(argv=la.argv, env=env, cwd=self.tmp, stdin_path=self._prompt()),
                      Limits("stream", 10, 60, None))
        self.assertEqual(res.exit_code, 2, res)
        self.assertEqual(SSEHandler.seen, {})

    def test_73_cut_mid_stream_keeps_the_partial_and_leaves_no_survivor(self):
        SSEHandler.script, SSEHandler.status = [(0, fixture("openrouter-length.sse")[:700]), (30, b"")], 200
        from councillib.supervise import Supervisor
        sup = Supervisor()
        la = self.build()
        sup.start("or", Launch(argv=la.argv, env=dict(os.environ), cwd=self.tmp, stdin_path=self._prompt()),
                  Limits("stream", 10, 60, None, grace_s=1))
        t0, results, cut = time.monotonic(), [], False
        while time.monotonic() - t0 < 20 and not results:
            results = sup.poll(0.2)
            if not cut and time.monotonic() - t0 > 1.5:
                sup.cut("or")
                cut = True
        self.assertTrue(results)
        res = results[0][1]
        self.assertEqual((res.status, res.truncated), ("cut", True), res)
        ex = feed_all(providers.OpenRouterExtractor(), res.stdout)
        self.assertIsNone(ex.final_text())
        self.assertTrue(ex.partial_text().startswith(providers.THINKING_NOTE))     # paid reasoning is kept
        self.assertEqual(sup.running(), [])

    # ---- openrouter_get
    def test_74_openrouter_get_returns_status_and_json_and_caches(self):
        st, data = providers.openrouter_get("/models", self.ocfg())
        self.assertEqual(st, 200)
        self.assertEqual(data["data"][0]["id"], "z-ai/glm-5")
        self.server.shutdown()
        self.assertEqual(providers.openrouter_get("/models", self.ocfg())[0], 200)      # cached, server is gone
        st, _ = providers.openrouter_get("/models", self.ocfg(), key="wrong")
        self.assertIsNone(st)                                                           # different key, not cached

    # ---- free checks (stubbed openrouter_get, W4)
    def fchecks(self, model="z-ai/glm-5", get=None, key=True, **kw):
        orig = providers.openrouter_get
        providers.openrouter_get = get or (lambda path, cfg, key=None, base_url=None, timeout=20: (
            (200, {"data": []}) if path == "/key" else
            (200, {"data": [{"id": "z-ai/glm-5", "reasoning": {"supported_efforts": ["high", "medium", "low"],
                                                                "supports_max_tokens": True}}]})))
        self.addCleanup(setattr, providers, "openrouter_get", orig)
        if not key:
            os.environ.pop("TEST_OR_KEY")
        cfg = self.ocfg(**kw)
        seat = {"id": "openrouter", "provider": "openrouter", "model": model}
        return {c.check: c for c in providers.get("openrouter").free_checks([seat], cfg)}

    def test_75_checks_pass(self):
        c = self.fchecks()
        self.assertEqual({k: v.result for k, v in c.items()},
                         {"key_env": "PASS", "key": "PASS", "models:openrouter": "PASS",
                          "reasoning_cap:openrouter": "PASS", "salvage_reasoning:openrouter": "PASS",
                          "routing:openrouter": "WARN",       # row 29 (D43): this fake serves no endpoints list
                          "reroute:openrouter": "WARN",       # fast-fail chain: the fake serves no endpoints list either
                          "inner_caps": "PASS", "output_budget": "PASS"})
        self.assertIn("routing prefs not checked", c["routing:openrouter"].detail)
        self.assertIn("idle+30", c["inner_caps"].detail)

    def test_75b_model_without_supports_max_tokens_needs_the_effort_form(self):
        # live /models for z-ai/glm-5 and qwen/qwen3.6-plus carries no supports_max_tokens (2026-10-21);
        # row 13: GLM 5 reasoned 7,999 tokens against a 6,000 cap. The token form on such a model is a FAIL.
        get = lambda path, cfg, key=None, base_url=None, timeout=20: (
            (200, {"data": []}) if path == "/key" else (200, {"data": [{"id": "z-ai/glm-5", "reasoning": {"mandatory": False}}]}))
        c = self.fchecks(get=get)
        self.assertEqual(c["reasoning_cap:openrouter"].result, "PASS")          # shipped config: effort form
        self.assertEqual(c["output_budget"].result, "PASS")
        c = self.fchecks(get=get, reasoning_form="tokens", ignores_reasoning_cap=False)
        self.assertEqual(c["reasoning_cap:openrouter"].result, "FAIL")
        self.assertIn("supports_max_tokens", c["reasoning_cap:openrouter"].detail)
        c = self.fchecks(reasoning_form="tokens", ignores_reasoning_cap=False)   # default stub advertises it
        self.assertEqual(c["reasoning_cap:openrouter"].result, "PASS")
        c = self.fchecks(reasoning_form="tokens")                                 # tokens while marked as ignoring
        self.assertEqual(c["output_budget"].result, "FAIL")
        self.assertEqual(self.fchecks(max_output_tokens=15000)["output_budget"].result, "FAIL")   # < 16000 reserve

    def test_76_bad_slug_fails_without_a_canary_row(self):
        c = self.fchecks(model="bogus/none")
        self.assertEqual(c["models:openrouter"].result, "FAIL")
        self.assertIn("not in /models", c["models:openrouter"].detail)
        self.assertFalse([k for k in c if "canary" in k])

    def test_77_key_401_and_missing_env_fail(self):
        c = self.fchecks(get=lambda p, cfg, key=None, base_url=None, timeout=20: (401, None))
        self.assertEqual((c["key"].result, c["models:openrouter"].result), ("FAIL", "FAIL"))
        c = self.fchecks(key=False)
        self.assertEqual((c["key_env"].result, c["key"].result), ("FAIL", "SKIP"))

    def test_78_unsupported_effort_warns_and_buffered_mode_checks_the_ceiling(self):
        c = self.fchecks(reasoning_effort="xhigh")
        self.assertEqual(c["effort:openrouter"].result, "WARN")
        c = self.fchecks(mode="buffered")
        self.assertEqual(c["inner_caps"].result, "PASS")
        self.assertIn("deadline+30", c["inner_caps"].detail)
        self.assertEqual(self.fchecks(reasoning_effort="turbo")["output_budget"].result, "FAIL")


class RegistryRow8Tests(Base):
    def test_79_every_provider_is_real_now(self):
        for name in ("claude", "codex", "agy", "qwen", "openrouter"):
            self.assertEqual(providers.get(name).name, name)
        self.assertEqual([providers.get(n).family for n in ("agy", "qwen", "openrouter")],
                         ["google", "alibaba", "openrouter"])
        with self.assertRaises(KeyError):
            providers.get("nope")

    def test_80_every_adapter_has_an_inner_caps_row(self):
        # stub binaries, a temp copy of the fixture settings and a stubbed openrouter_get: no real CLI,
        # no real ~/.qwen/settings.json, no network (W4)
        settings = os.path.join(self.tmp, "settings.json")
        shutil.copy(os.path.join(FIX, "qwen-settings.json"), settings)
        os.chmod(settings, 0o600)
        cfg = self.cfg_with("agy", bin=self.script("agy", 'case "$1" in\n --version) echo 1.4.4;;\n models) echo;;\nesac\n'))
        cfg = config.deep_merge(cfg, {"providers": {"qwen": {
            "bin": self.script("qwen", 'echo 0.25.3\n'), "settings_path": settings}}})
        orig = providers.openrouter_get
        providers.openrouter_get = lambda *a, **k: (200, {"data": []})
        self.addCleanup(setattr, providers, "openrouter_get", orig)
        for name in ("agy", "qwen", "openrouter"):
            names = [c.check for c in providers.get(name).free_checks([], cfg)]
            self.assertTrue(any(n.startswith("inner_caps") for n in names), (name, names))

    def test_81_budget_keys_are_in_config_for_all_three(self):
        for p in ("qwen", "openrouter"):
            pc = self.cfg["providers"][p]
            self.assertEqual((pc["reasoning_effort"], pc["chair_reasoning_effort"]), ("medium", "high"))
            self.assertEqual(pc["reasoning_form"], "effort")           # neither model honours a reasoning cap
            self.assertTrue(pc["ignores_reasoning_cap"])
        # real model maximums (OpenRouter /models top_provider.max_completion_tokens, 2026-10-21; claude upper limit)
        self.assertEqual([self.cfg["providers"][p][k] for p in ("qwen", "openrouter", "claude")
                          for k in ("max_output_tokens", "chair_max_output_tokens")],
                         [65536, 65536, 131072, 131072, 128000, 128000])
        self.assertEqual(self.cfg["answer_reserve_tokens"], 16000)
        self.assertNotIn("reasoning_ratio", self.cfg)
        self.assertEqual(self.cfg["providers"]["agy"]["mode"], "buffered")
        self.assertEqual({p: self.cfg["providers"][p]["mode"] for p in ("qwen", "openrouter")},
                         {"qwen": "stream", "openrouter": "stream"})
        self.assertGreaterEqual(self.cfg["timeouts"]["agy"]["base_timeout_s"], 900)


# ====================================================================== row 16
class AgyStepLimitTests(Base):
    """agy hits its own step limit, continues and finishes: ok. No answer text: still an error."""

    def test_90_step_limit_then_a_finished_answer_is_ok_with_note_step_limit(self):
        # fixture copied from the real row 15 raw (conversation id, cwd and tool list redacted)
        ex = feed_all(providers.AgyExtractor(), fixture("agy-step-limit-answer.ndjson"))
        self.assertEqual(ex.result_event()["status"], "ERROR")
        self.assertIn("output token limit", ex.result_event()["error"])
        self.assertIsNone(ex.error())
        self.assertEqual(ex.note(), "step_limit")
        text = ex.final_text()
        self.assertTrue(text.rstrip().endswith("Confidence: high"))
        self.assertIn("ANSWER: part1=true; 4x13=28; 4x14=37", text)
        self.assertEqual(text, ex.partial_text())            # steps 1 and 3 joined, not the last step alone

    def test_91_the_existing_error_fixture_stays_an_error(self):
        # the retry step never reached DONE: it was cut mid-answer, so there is no final answer
        ex = feed_all(providers.AgyExtractor(), fixture("agy-error.ndjson"))
        self.assertIsNone(ex.final_text())
        self.assertIn("agy_error", ex.error())
        self.assertIsNone(ex.note())

    def test_92_error_with_no_answer_text_stays_an_error(self):
        evs = [{"event": "init", "init": {"model": "m"}},
               {"event": "step_update", "step_update": {"step_index": 0, "state": "DONE", "step_type": "user_input"}},
               {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response"}},
               {"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "error_message"}},
               {"event": "step_update", "step_update": {"step_index": 3, "state": "DONE", "step_type": "agent_response",
                                                        "text_delta": "  \n"}},
               {"event": "result", "result": {"status": "ERROR", "response": "",
                                              "error": "exceeded the output token limit"}}]
        ex = feed_all(providers.AgyExtractor(), "".join(json.dumps(e) + "\n" for e in evs).encode())
        self.assertIsNone(ex.final_text())
        self.assertIn("agy_error", ex.error())
        self.assertIsNone(ex.note())

    def test_93_an_answer_that_came_before_the_error_step_is_not_a_final_answer(self):
        evs = [{"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response",
                                                        "text_delta": "half an answer"}},
               {"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "error_message"}},
               {"event": "result", "result": {"status": "ERROR", "response": "half an answer", "error": "boom"}}]
        ex = feed_all(providers.AgyExtractor(), "".join(json.dumps(e) + "\n" for e in evs).encode())
        self.assertIsNone(ex.final_text())
        self.assertIsNotNone(ex.error())
        self.assertEqual(ex.partial_text(), "half an answer")      # kept as the partial

    def test_94_other_non_ok_status_with_a_finished_answer_gets_its_own_note(self):
        evs = [{"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response",
                                                        "text_delta": "the answer"}},
               {"event": "result", "result": {"status": "CANCELLED", "response": "the answer", "error": "x"}}]
        ex = feed_all(providers.AgyExtractor(), "".join(json.dumps(e) + "\n" for e in evs).encode())
        self.assertIsNone(ex.error())
        self.assertEqual((ex.final_text(), ex.note()), ("the answer", "status_cancelled_with_answer"))

    def test_95_success_status_is_unchanged(self):
        ex = feed_all(providers.AgyExtractor(), fixture("agy.ndjson"))
        self.assertIsNone(ex.note())


class SalvageBuildTests(Base):
    """Per-provider form of the answer-salvage call (docs / --help / binary only; not run live)."""

    def sseat(self, provider, model, cfg=None, **kw):
        cfg = cfg or self.cfg
        seat = dict({"id": provider, "provider": provider, "model": model}, **kw)
        return providers.get(provider).salvage_seat(seat, cfg, 8000), cfg

    def test_96_claude_effort_low_thinking_disabled_and_the_short_cap(self):
        s, cfg = self.sseat("claude", "claude-opus-5-5")
        la = providers.get("claude").build(s, "/p", "/w", lim(600.0), cfg)
        a = la.argv
        self.assertEqual(a[a.index("--effort") + 1], "low")
        self.assertEqual(a[a.index("--thinking") + 1], "disabled")
        self.assertNotIn("--thinking-display", a)
        self.assertNotIn("--max-thinking-tokens", a)
        self.assertEqual(la.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "8000")

    def test_97_claude_salvage_thinking_default_keeps_only_the_low_effort(self):
        cfg = self.cfg_with("claude", salvage_thinking="default")
        s, _ = self.sseat("claude", "claude-opus-5-5", cfg)
        a = providers.get("claude").build(s, "/p", "/w", lim(600.0), cfg).argv
        self.assertNotIn("--thinking", a)
        self.assertEqual(a[a.index("--effort") + 1], "low")

    def test_98_codex_lowest_known_effort_and_no_cap(self):
        s, cfg = self.sseat("codex", "gpt-6-astra")
        la = providers.get("codex").build(s, "/p", "/w", lim(600.0, "buffered"), cfg)
        self.assertIn("model_reasoning_effort=low", la.argv)
        self.assertFalse(any("max" in a and "token" in a for a in la.argv))

    def test_99_agy_swaps_the_label_tier_for_low(self):
        s, cfg = self.sseat("agy", "Gemini 3.8 Flash (Medium)")
        self.assertEqual(s["model"], "Gemini 3.8 Flash (Low)")
        s2, _ = self.sseat("agy", "Gemini 3.8 Flash (High)")
        self.assertEqual(s2["model"], "Gemini 3.8 Flash (Low)")
        s3, _ = self.sseat("agy", "gemini-x")
        self.assertEqual(s3["model"], "gemini-x")            # no tier to swap

    def test_100_openrouter_reasoning_off_or_the_named_effort(self):
        cfg = self.cfg
        glm, _ = self.sseat("openrouter", "z-ai/glm-5")
        a = providers.get("openrouter").build(glm, "/p", "/w", lim(600.0), cfg).argv
        self.assertIn("--reasoning-off", a)
        self.assertNotIn("--reasoning-effort", a)
        self.assertNotIn("--reasoning-max-tokens", a)
        self.assertEqual(a[a.index("--max-tokens") + 1], "8000")
        nemo_seat = next(x for x in cfg["seat"] if x["id"] == "deepseek")
        nemo = providers.get("openrouter").salvage_seat(nemo_seat, cfg, 8000)
        a = providers.get("openrouter").build(nemo, "/p", "/w", lim(600.0), cfg).argv
        self.assertEqual(a[a.index("--reasoning-effort") + 1], "none")
        self.assertNotIn("--reasoning-off", a)
        self.assertNotIn("--reasoning-max-tokens", a)         # the tokens form of the seat is dropped
        self.assertEqual(a[a.index("--max-tokens") + 1], "8000")

    def test_101_openrouter_seat_child_sends_enabled_false(self):
        from councillib import openrouter_seat
        args = openrouter_seat.parse_args(["--model", "m", "--max-tokens", "8000", "--reasoning-off"])
        self.assertEqual(openrouter_seat.build_body(args, "q")["reasoning"], {"enabled": False})
        args = openrouter_seat.parse_args(["--model", "m", "--reasoning-effort", "none"])
        self.assertEqual(openrouter_seat.build_body(args, "q")["reasoning"], {"effort": "none"})

    def test_102_qwen_overlay_reasoning_off(self):
        entry = {"id": "qwen/qwen3.6-plus", "generationConfig": {}}
        lm = Limits(mode="stream", idle_s=240, ceiling_s=600, deadline_s=None)
        ov = providers.qwen_overlay(entry, lm, "low", 8000, None, True)
        gc = ov["modelProviders"]["openai"][0]["generationConfig"]
        self.assertEqual(gc["extra_body"]["reasoning"], {"enabled": False})
        self.assertEqual(gc["samplingParams"]["max_tokens"], 8000)

    def test_103_shipped_roster_has_deepseek_on_the_effort_form(self):
        seats = {s["id"]: s for s in self.cfg["seat"]}
        self.assertNotIn("qwen", seats)
        self.assertNotIn("nemotron", seats)
        n = seats["deepseek"]
        pc = self.cfg["providers"]["openrouter"]
        self.assertEqual((n["provider"], n["model"]), ("openrouter", "deepseek/deepseek-v4-pro-0813"))
        eff, cap = providers.budget(n, pc)
        self.assertEqual((eff, cap), ("high", 131072))        # its own effort (no medium in supported_efforts), the 131072 cap
        # council-hardening row 8.2: reasoning.max_tokens is accepted but ignored live, so no reasoning cap is sent
        self.assertIsNone(providers.reasoning_cap(n, pc, self.cfg, cap))
        self.assertEqual(providers._budget_problems("openrouter", pc, providers.OR_EFFORTS, True, self.cfg, True,
                                                    self.cfg["seat"]), [])
        la = providers.get("openrouter").build(dict(n), "/p", "/w", lim(600.0), self.cfg)
        self.assertEqual(la.argv[la.argv.index("--reasoning-effort") + 1], "high")
        self.assertEqual(la.argv[la.argv.index("--max-tokens") + 1], "131072")
        self.assertNotIn("--reasoning-max-tokens", la.argv)
        # the tokens form on a seat that ignores the cap is a preflight problem
        bad = dict(n, reasoning_form="tokens")
        self.assertTrue(providers._budget_problems("openrouter", pc, providers.OR_EFFORTS, True, self.cfg, True,
                                                   [bad]))
        # GLM keeps the effort form
        glm = seats["openrouter"]
        self.assertEqual(glm["model"], "z-ai/glm-5.3")
        self.assertIsNone(providers.reasoning_cap(glm, pc, self.cfg, providers.budget(glm, pc)[1]))

    def test_104_deepseek_check_against_a_catalog_entry(self):
        pc = self.cfg["providers"]["openrouter"]
        n = next(x for x in self.cfg["seat"] if x["id"] == "deepseek")
        entry = {"reasoning": {"mandatory": False, "supports_max_tokens": True,
                               "supported_efforts": ["high", "medium", "none"]}}
        self.assertEqual(providers.reasoning_cap_check("openrouter", n, pc, entry).result, "PASS")
        self.assertEqual(providers.salvage_reasoning_check("openrouter", n, pc, entry).result, "PASS")
        # reasoning off on a mandatory-reasoning model is a WARN, a bad value a FAIL
        glm = {"id": "g", "model": "m"}
        self.assertEqual(providers.salvage_reasoning_check("openrouter", glm, pc, {"reasoning": {"mandatory": True}}).result, "WARN")
        bad = dict(glm, salvage_reasoning="bogus")
        self.assertEqual(providers.salvage_reasoning_check("openrouter", bad, pc, {}).result, "FAIL")

    def test_105_salvage_config_problems(self):
        from councillib import council_run
        self.assertEqual(council_run.salvage_problems(self.cfg), [])
        self.assertTrue(council_run.salvage_problems({"salvage": {"max_output_tokens": 5}}))
        self.assertTrue(council_run.salvage_problems({"salvage": {"timeout": 0}}))
        self.assertTrue(council_run.salvage_problems({"salvage": {"enabled": "yes"}}))
        self.assertEqual(providers.salvage_provider_problems(self.cfg), [])
        bad = config.deep_merge(self.cfg, {"providers": {"claude": {"salvage_reasoning_effort": "tiny"},
                                                         "agy": {"salvage_model_tier": "Huge"}}})
        self.assertEqual(len(providers.salvage_provider_problems(bad)), 2)


def _agy_stream(*evs):
    return "".join(json.dumps(e) + "\n" for e in evs).encode()


PRINT_TIMEOUT = b"[agy] print timeout after 30s with turn in progress; returning partial output\n"
AGY_429 = (b"error: Individual quota reached. Please upgrade your subscription to increase your limits. "
           b"Resets in 149h33m23s.\n"
           b'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): Individual quota reached.",'
           b'"status":"RESOURCE_EXHAUSTED","error_code":429,"code_kind":"http","retryable":true}\n')


class AgyTerminalStateTests(Base):
    """Row 28 (D28, D47): the print-timeout warning and a 429 quota are non-answers with a reason."""

    def partial(self):
        return [{"event": "init", "init": {"model": "m"}},
                {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response",
                                                         "text_delta": "a partial answer"}},
                {"event": "result", "result": {"status": "SUCCESS", "response": "a partial answer"}}]

    def test_110_print_timeout_is_cli_timeout_not_ok_even_with_rc0_and_success(self):
        ex = feed_all(providers.AgyExtractor(), _agy_stream(*self.partial()))
        self.assertEqual(ex.final_text(), "a partial answer")      # without the warning it is an answer
        ex.feed_stderr(PRINT_TIMEOUT)
        self.assertEqual(ex.terminal_reason(), "cli_timeout")
        self.assertIsNone(ex.final_text())
        self.assertTrue(ex.error().startswith("cli_timeout"))
        self.assertEqual(ex.partial_text(), "a partial answer")    # the partial is kept
        self.assertIsNone(ex.note())

    def test_111_the_real_probe_shape_empty_success_plus_stderr_warning(self):
        evs = [{"event": "init", "init": {"model": "m"}},
               {"event": "step_update", "step_update": {"step_index": 0, "state": "DONE", "step_type": "user_input"}},
               {"event": "result", "result": {"status": "SUCCESS", "response": ""}}]
        ex = feed_all(providers.AgyExtractor(), _agy_stream(*evs))
        ex.feed_stderr(PRINT_TIMEOUT)
        self.assertEqual((ex.terminal_reason(), ex.final_text()), ("cli_timeout", None))
        self.assertIn("cli_timeout", ex.error())

    def test_112_warning_split_over_chunks_and_without_a_final_newline(self):
        ex = feed_all(providers.AgyExtractor(), _agy_stream(*self.partial()))
        ex.feed_stderr(b"[agy] print time")
        self.assertIsNone(ex.terminal_reason())
        ex.feed_stderr(b"out after 30s with turn in progress; returning partial output")
        self.assertEqual(ex.terminal_reason(), "cli_timeout")

    def test_113_stderr_without_the_warning_changes_nothing(self):
        ex = feed_all(providers.AgyExtractor(), fixture("agy.ndjson"))
        before = (ex.final_text(), ex.error(), ex.terminal_reason())
        ex.feed_stderr(b"some other warning\nW poll.go:188 stuff\n")
        self.assertEqual((ex.final_text(), ex.error(), ex.terminal_reason()), before)
        self.assertIsNone(before[1])

    def test_114_quota_429_on_stderr_is_quota_exhausted(self):
        ex = providers.AgyExtractor()
        ex.feed_stderr(AGY_429)
        self.assertEqual(ex.terminal_reason(), "quota_exhausted")
        self.assertTrue(ex.error().startswith("quota_exhausted"))
        self.assertIn("149h33m23s", ex.error())
        self.assertIsNone(ex.final_text())

    def test_115_quota_429_on_stdout_alone_is_quota_exhausted(self):
        evs = [{"event": "init", "init": {"model": "m"}},
               {"event": "step_update", "step_update": {"step_index": 3, "state": "DONE", "step_type": "error_message"}},
               {"event": "result", "result": {"status": "ERROR", "response": "",
                                              "error": "Individual quota reached. Please upgrade. Resets in 149h33m23s."}}]
        ex = feed_all(providers.AgyExtractor(), _agy_stream(*evs))
        self.assertEqual(ex.terminal_reason(), "quota_exhausted")
        self.assertTrue(ex.error().startswith("quota_exhausted"))
        self.assertIsNone(ex.final_text())

    def test_116_a_different_agy_error_is_not_a_quota(self):
        ex = feed_all(providers.AgyExtractor(), fixture("agy-error.ndjson"))
        ex.feed_stderr(b'AGY_ERROR: {"status":"UNAVAILABLE","error_code":503}\n')
        self.assertIsNone(ex.terminal_reason())
        self.assertTrue(ex.error().startswith("agy_error"))

    def test_117_a_real_answer_is_never_downgraded_by_stray_stderr(self):
        ex = feed_all(providers.AgyExtractor(), fixture("agy.ndjson"))
        ex.feed_stderr(b"error: Individual quota reached (a different, later call)\n")
        # SUCCESS with an answer stays an answer; quota text on stderr only matters without one
        self.assertIsNotNone(ex.final_text())
        self.assertIsNone(ex.error())

    def test_118_base_extractor_hooks_are_no_ops(self):
        for ex in (providers.ClaudeExtractor(), providers.CodexExtractor(), providers.OpenRouterExtractor()):
            ex.feed_stderr(PRINT_TIMEOUT)
            self.assertIsNone(ex.terminal_reason())

    def test_119_build_argv_unchanged(self):
        # row 28 adds no flag, no env: the agy launch is byte-identical to before
        prompt = os.path.join(self.tmp, "p.md")
        with open(prompt, "w") as f:
            f.write("hello")
        seat = {"id": "agy", "provider": "agy", "model": "Gemini 3.8 Flash (Medium)"}
        launch = providers.get("agy").build(seat, prompt, self.tmp, lim(), self.cfg)
        self.assertEqual(launch.argv[1:7], ["--sandbox", "--dangerously-skip-permissions", "--print-timeout",
                                            "1260s", "--model", seat["model"]])
        self.assertNotIn("--v=3", launch.argv)


class CodexNonAnswerTests(Base):
    """Row 25/28: rc 0 after SIGTERM and rc 101 (Broken pipe) are not answers."""

    def classify(self, status, exit_code, stdout=b""):
        from councillib import council_run
        from councillib.supervise import Result
        seat = {"id": "codex", "provider": "codex", "model": "gpt-6-astra"}
        task = council_run._Task("codex", "seat", seat, providers.get("codex"))
        task.extractor = providers.CodexExtractor()
        task.launch = Launch(argv=["codex"], env={}, cwd=self.tmp)       # no -o file: nothing written
        feed_all(task.extractor, stdout)
        task.result = Result(status=status, exit_code=exit_code, stdout=stdout, stderr_tail=b"", first_byte_s=None,
                             max_gap_s=0.0, total_s=1.0, out_bytes=len(stdout), extensions=0, truncated=False)
        return council_run._classify(task), task

    def test_120_rc0_with_no_agent_message_and_no_turn_completed_is_empty_output(self):
        ok, task = self.classify("ok", 0, b'{"type":"thread.started","thread_id":"t"}\n')
        self.assertFalse(ok)
        self.assertEqual(task.status, "empty_output")

    def test_121_rc101_after_a_lost_reader_is_not_ok(self):
        ok, task = self.classify("exit_101", 101, b'{"type":"thread.started","thread_id":"t"}\n')
        self.assertFalse(ok)
        self.assertEqual(task.status, "exit_101")

    def test_122_a_terminate_status_is_not_ok_even_with_rc0(self):
        ok, task = self.classify("no_connection", 0)
        self.assertFalse(ok)
        self.assertEqual(task.status, "no_connection")


class OpenRouterLivenessTests(Base):
    """Row 28 (D43, D44): keepalive comments are labelled, never data; the chunk id is kept."""

    def test_130_a_thousand_comments_then_one_delta_moves_progress_only_at_the_delta(self):
        now = [100.0]
        ex = providers.OpenRouterExtractor(clock=lambda: now[0])
        for i in range(1000):
            now[0] = 100.0 + i
            ex.feed(b": OPENROUTER PROCESSING\n\n")
        st = ex.stream_stats()
        self.assertEqual((st["keepalives"], st["last_data_at"], st["data_bytes"]), (1000, None, 0))
        self.assertEqual(st["last_keepalive_at"], 1099.0)
        self.assertGreater(st["comment_bytes"], 0)
        now[0] = 2000.0
        ex.feed(b'data: {"id":"gen-1","model":"m","provider":"P","choices":[{"delta":{"content":"hi"}}]}\n\n')
        st = ex.stream_stats()
        self.assertEqual(st["last_data_at"], 2000.0)
        self.assertGreater(st["data_bytes"], 0)
        self.assertEqual(st["keepalives"], 1000)

    def test_131_keepalives_do_not_change_the_answer_or_the_error_logic(self):
        ex = feed_all(providers.OpenRouterExtractor(), fixture("openrouter.sse"))
        self.assertIsNone(ex.error())
        self.assertGreaterEqual(ex.keepalives, 1)
        self.assertTrue(ex.final_text())

    def test_132_chunk_id_and_serving_provider_are_kept(self):
        ex = feed_all(providers.OpenRouterExtractor(), fixture("openrouter.sse"))
        self.assertEqual(ex.generation_id(), "gen-TEST")
        self.assertEqual(ex.serving_provider(), "TestProvider")
        none = providers.OpenRouterExtractor()
        none.feed(b': hi\n\ndata: {"model":"m","choices":[{"delta":{"content":"x"}}]}\n\n')
        self.assertEqual((none.generation_id(), none.serving_provider()), (None, None))

    def test_133_stats_feed_a_stream_seat_liveness_record(self):
        from councillib.liveness import LivenessPolicy, SeatLiveness
        now = [1000.0]
        ex = providers.OpenRouterExtractor(clock=lambda: now[0])
        seat = SeatLiveness("openrouter", "t", LivenessPolicy(progress_signal="stream"),
                            {"stream_stats": ex.stream_stats}, clock=lambda: now[0], started_wall=1000.0)
        for i in range(50):
            now[0] += 10
            ex.feed(b": OPENROUTER PROCESSING\n\n")
        v = seat.sample()
        self.assertEqual((v.progress_at, v.keepalives), (None, 50))
        self.assertEqual(v.keepalive_at, 500.0)
        now[0] += 10
        ex.feed(b'data: {"model":"m","choices":[{"delta":{"reasoning":"x"}}]}\n\n')
        self.assertEqual(seat.sample().progress_at, 510.0)


class GenerationFetchTests(Base):
    """Row 28: free post-hoc GET /generation, evidence only; retried on 404 through the process cache."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.real = providers.openrouter_get
        self.addCleanup(setattr, providers, "openrouter_get", self.real)

    def script(self, *answers):
        seq = list(answers)

        def fake(path, cfg, key=None, base_url=None, timeout=20):
            self.calls.append(path)
            st, data = seq.pop(0)
            providers._OR_CACHE[("b", path, "k")] = (st, data)      # as the real function would
            return st, data
        providers.openrouter_get = fake

    BODY = {"data": {"provider_name": "BaseTen", "finish_reason": "error", "generation_time": 1234,
                     "native_tokens_completion": 55, "total_cost": 0, "api_key_hash": "zzz",
                     "nested": {"a": 1}}}

    def test_140_404_404_200_sequence_returns_whitelisted_evidence(self):
        self.script((404, None), (404, None), (200, self.BODY))
        sleeps = []
        out = providers.fetch_generation("gen-abc", self.cfg, sleep=sleeps.append)
        self.assertEqual(self.calls, ["/generation?id=gen-abc"] * 3)
        self.assertEqual(sleeps, [5.0, 5.0])
        self.assertEqual(out, {"http": 200, "provider_name": "BaseTen", "finish_reason": "error",
                               "generation_time": 1234, "native_tokens_completion": 55, "total_cost": 0})

    def test_141_gives_up_after_three_404s_and_never_raises(self):
        self.script((404, None), (404, None), (404, None))
        out = providers.fetch_generation("gen-x", self.cfg, sleep=lambda s: None)
        self.assertEqual((out, len(self.calls)), ({"http": 404}, 3))

    def test_142_other_errors_are_not_retried(self):
        self.script((500, None))
        self.assertEqual(providers.fetch_generation("g", self.cfg, sleep=lambda s: None), {"http": 500})
        self.assertEqual(len(self.calls), 1)
        self.script((None, None))
        self.assertEqual(providers.fetch_generation("g", self.cfg, sleep=lambda s: None), {"http": None})

    def test_143_id_is_url_quoted(self):
        self.script((200, self.BODY))
        providers.fetch_generation("gen-a/b c&d", self.cfg, sleep=lambda s: None)
        self.assertEqual(self.calls, ["/generation?id=gen-a%2Fb%20c%26d"])

    def test_144_a_cached_404_does_not_poison_the_retry(self):
        # drive the real openrouter_get (cache included) through a fake urlopen
        import urllib.error
        import urllib.request
        providers._OR_CACHE.clear()
        answers = [404, 404, 200]
        seen = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(GenerationFetchTests.BODY).encode()

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            code = answers.pop(0)
            if code == 404:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
            return Resp()
        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", orig)
        out = providers.fetch_generation("gen-9", self.cfg, key="k", sleep=lambda s: None)
        self.assertEqual(len(seen), 3)
        self.assertEqual(out["http"], 200)


# ---------------------------------------------------------------- row 29: launch_opts (D38), probe-signal flags (D41)
CODEX_SEAT = {"id": "codex", "provider": "codex", "model": "gpt-6-astra"}
AGY_SEAT = {"id": "agy", "provider": "agy", "model": "Gemini 3.8 Flash (Medium)"}
OTEL_EXPORTER = 'otel.exporter={otlp-http={endpoint="http://127.0.0.1:43211/v1/logs",protocol="json"}}'


class LaunchOptsTests(Base):
    def setUp(self):
        super().setUp()
        os.environ.pop("COUNCIL_SEAT_TAG", None)
        self.prompt = os.path.join(self.tmp, "p.md")
        with open(self.prompt, "w") as f:
            f.write("question")
        self.wd = os.path.join(self.tmp, "wd")
        os.makedirs(self.wd, mode=0o700)

    def codex(self, signals=False, opts=None, seat=None, **pkw):
        cfg = config.deep_merge(self.cfg, {"liveness": {"signals": signals}, "providers": {"codex": pkw}})
        return providers.get("codex").build(seat or CODEX_SEAT, self.prompt, self.wd, lim(3600.0, "buffered"), cfg, opts)

    def agy(self, signals=False, opts=None, ceiling=3600.0, seat=None, **pkw):
        cfg = config.deep_merge(self.cfg, {"liveness": {"signals": signals}, "providers": {"agy": pkw}})
        return providers.get("agy").build(seat or AGY_SEAT, self.prompt, self.wd, lim(ceiling, "buffered"), cfg, opts)

    def test_145_every_adapter_takes_launch_opts_defaulting_to_none(self):
        import inspect
        for name in ("claude", "codex", "agy", "qwen", "openrouter", "stub"):
            p = inspect.signature(providers.get(name).build).parameters
            self.assertIn("launch_opts", p, name)
            self.assertIsNone(p["launch_opts"].default, name)
        self.assertIn("launch_opts", inspect.signature(providers.Adapter.build).parameters)

    def test_146_none_and_empty_opts_give_todays_argv_and_env(self):
        seats = {"claude": {"id": "claude", "provider": "claude", "model": "claude-opus-5-5"}, "codex": CODEX_SEAT,
                 "agy": AGY_SEAT, "openrouter": {"id": "o", "provider": "openrouter", "model": "z-ai/glm-5"},
                 "stub": {"id": "s", "provider": "stub", "model": "m"}}
        for name, seat in seats.items():
            ad = providers.get(name)
            a = ad.build(seat, self.prompt, self.wd, lim(3600.0, "buffered"), self.cfg)
            for opts in (None, {}):
                b = ad.build(seat, self.prompt, self.wd, lim(3600.0, "buffered"), self.cfg, opts)
                self.assertEqual((a.argv, a.env), (b.argv, b.env), name)
            self.assertNotIn("COUNCIL_SEAT_TAG", a.env, name)

    def test_147_the_tag_always_goes_into_env(self):
        seats = {"claude": {"id": "claude", "provider": "claude", "model": "claude-opus-5-5"}, "codex": CODEX_SEAT,
                 "agy": AGY_SEAT, "openrouter": {"id": "o", "provider": "openrouter", "model": "z-ai/glm-5"},
                 "stub": {"id": "s", "provider": "stub", "model": "m"}}
        for name, seat in seats.items():
            plain = providers.get(name).build(seat, self.prompt, self.wd, lim(3600.0, "buffered"), self.cfg)
            tagged = providers.get(name).build(seat, self.prompt, self.wd, lim(3600.0, "buffered"), self.cfg,
                                               {"tag": "tag-%s" % name})
            self.assertEqual(tagged.env["COUNCIL_SEAT_TAG"], "tag-%s" % name)
            self.assertEqual(tagged.argv, plain.argv, name)         # the tag alone never touches argv
            rest = {k: v for k, v in tagged.env.items() if k != "COUNCIL_SEAT_TAG"}
            self.assertEqual(rest, plain.env, name)

    def test_148_codex_signals_off_adds_nothing_even_with_full_opts(self):
        full = {"tag": "t", "otlp_port": 43211, "log_path": os.path.join(self.wd, "x.log")}
        base = self.codex()
        self.assertEqual(self.codex(False, full).argv, base.argv)
        self.assertEqual(base.argv, [
            "codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-m", "gpt-6-astra",
            "-o", os.path.join(self.wd, "last.txt"), "-c", "model_provider=openai-council",
            "-c", "model_reasoning_effort=medium", "-"])
        self.assertTrue(self.cfg["liveness"]["signals"] is True)       # shipped on since row 34 (D41); `codex()` forces it off

    def test_149_codex_signals_on_adds_the_adopted_flags_before_the_dash(self):
        b = self.codex(True, {"tag": "t", "otlp_port": 43211})
        off = self.codex()
        self.assertEqual(b.argv[:-1], off.argv[:-1] + [
            "-c", OTEL_EXPORTER, "-c", "otel.log_user_prompt=false", "-c", "model_reasoning_summary=detailed"])
        self.assertEqual(b.argv[-1], "-")
        self.assertEqual(b.env["COUNCIL_SEAT_TAG"], "t")

    def test_150_codex_signals_on_without_a_port_shows_only_the_summary_flag(self):
        for opts in (None, {}, {"tag": "t"}):
            b = self.codex(True, opts)
            self.assertEqual(b.argv[len(self.codex().argv) - 1:], ["-c", "model_reasoning_summary=detailed", "-"])
            self.assertFalse([a for a in b.argv if a.startswith("otel.")])

    def test_151_codex_gate_keys_drive_the_flags(self):
        o = {"otlp_port": 43211}
        self.assertFalse([a for a in self.codex(True, o, progress_signal="none").argv if a.startswith("otel.")])
        self.assertNotIn("model_reasoning_summary=detailed", self.codex(True, o, reasoning_summary="none").argv)
        self.assertIn("model_reasoning_summary=concise", self.codex(True, o, reasoning_summary="concise").argv)
        salvage = dict(CODEX_SEAT, _salvage=True)
        self.assertEqual(self.codex(True, o, seat=salvage).argv, self.codex(False, o, seat=salvage).argv)

    def test_152_agy_log_flags_need_signals_applog_and_a_log_in_the_workdir(self):
        log = os.path.join(self.wd, "agy.log")
        o = {"tag": "t", "log_path": log}
        shipped = self.agy(True, o)                                  # shipped progress_signal = none
        self.assertEqual(shipped.argv, self.agy().argv)
        self.assertEqual(self.agy(False, o, progress_signal="applog").argv, self.agy().argv)
        on = self.agy(True, o, progress_signal="applog")
        i = on.argv.index("--v=3")
        self.assertEqual(on.argv[i:i + 3], ["--v=3", "--log-file", log])
        self.assertLess(on.argv.index("--output-format"), i)
        self.assertLess(i, on.argv.index("--print"))
        self.assertEqual(on.env["COUNCIL_SEAT_TAG"], "t")
        self.assertEqual(self.agy(True, {"tag": "t"}, progress_signal="applog").argv, self.agy().argv)
        with self.assertRaises(ValueError):
            self.agy(True, {"log_path": os.path.join(self.tmp, "elsewhere.log")}, progress_signal="applog")
        with self.assertRaises(ValueError):
            self.agy(True, {"log_path": os.path.join(self.wd, "..", "esc.log")}, progress_signal="applog")

    def test_153_agy_without_a_ceiling_has_no_print_timeout(self):
        with_ceiling = self.agy(ceiling=3600.0).argv
        self.assertEqual(with_ceiling[:5], ["agy", "--sandbox", "--dangerously-skip-permissions",
                                            "--print-timeout", "3660s"])
        self.assertEqual(with_ceiling[5:9], ["--model", "Gemini 3.8 Flash (Medium)", "--output-format", "stream-json"])
        none = self.agy(ceiling=None).argv
        self.assertNotIn("--print-timeout", none)
        self.assertEqual(none, [a for a in with_ceiling if a not in ("--print-timeout", "3660s")])


if __name__ == "__main__":
    unittest.main()
