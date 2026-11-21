"""Provider registry, base classes, the test-only `stub` adapter and the real adapters.

claude and codex (row 7) and agy, qwen and openrouter (row 8) live here and call
`register()`. `known()` accepts every name in FAMILIES so the roster policy can be checked
without an adapter; `get()` raises NotImplementedError for a known name with no adapter.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass

from . import timing
from .supervise import Launch, Limits

HERE = os.path.dirname(os.path.abspath(__file__))
STUB_CLI = os.path.join(os.path.dirname(HERE), "tests", "stub_cli.py")

# provider name -> model family. "stub" exists for tests only.
FAMILIES = {
    "claude": "anthropic",
    "codex": "openai",
    "agy": "google",
    "qwen": "alibaba",
    "openrouter": "openrouter",
    "stub": "stub",
}
_ANTHROPIC_RE = re.compile(r"(^|/)(anthropic|claude)", re.I)


USAGE_KEYS = ("input_tokens", "output_tokens", "reasoning_tokens", "cost_usd")


def usage_dict(input_tokens=None, output_tokens=None, reasoning_tokens=None, cost_usd=None):
    """The one usage contract. A field the CLI/API did not report is None (unknown), never 0 and never
    an estimate. `output_tokens` already INCLUDES `reasoning_tokens` for claude, agy and OpenRouter
    (do not add them); codex is treated the same but that is inferred, not proven. `input_tokens` is
    the provider's own input figure (claude: uncached input only; its cache reads/writes are separate)."""
    def num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None
    return {"input_tokens": num(input_tokens), "output_tokens": num(output_tokens),
            "reasoning_tokens": num(reasoning_tokens), "cost_usd": num(cost_usd)}


def _dig(obj, *path):
    for k in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


class Extractor:
    """Turns a seat's raw stdout into text. Feed it every chunk."""

    def feed(self, chunk: bytes) -> None:
        raise NotImplementedError

    def final_text(self) -> str | None:
        raise NotImplementedError

    def partial_text(self) -> str:
        raise NotImplementedError

    def model_reported(self) -> str | None:
        return None

    def error(self) -> str | None:
        return None

    def usage(self) -> dict:
        """Tokens and cost the provider reported for this call (`usage_dict`); every field None when it
        reported none (a killed run, a subscription CLI with no cost)."""
        return usage_dict()

    def note(self) -> str | None:
        """Why an ok seat is ok in an unusual way (e.g. agy `step_limit`). Becomes the seat's `detail`."""
        return None

    def feed_stderr(self, chunk: bytes) -> None:
        """Optional: the call's stderr (row 28). Most extractors ignore it. A scheduler that has the
        stderr (live chunks or `Result.stderr_tail` after exit) feeds it before `_classify`."""

    def terminal_reason(self) -> str | None:
        """A proven terminal state the CLI itself reported (`cli_timeout`, `quota_exhausted`), or None.
        When set, `error()` starts with the same word and `final_text()` is None."""
        return None


class RawTextExtractor(Extractor):
    """stdout is the answer. `model` is what the CLI is said to have run (or None)."""

    def __init__(self, model: str | None = None):
        self._buf = bytearray()
        self._model = model

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk

    def partial_text(self) -> str:
        return bytes(self._buf).decode("utf-8", "replace")

    def final_text(self) -> str | None:
        text = self.partial_text().strip()
        return text or None

    def model_reported(self) -> str | None:
        return self._model


@dataclass
class Check:
    provider: str
    stage: str      # free | canary
    check: str
    result: str     # PASS | FAIL | WARN | SKIP
    detail: str
    usage: dict | None = None    # canary rows only: the call's usage_dict
    standin: dict | None = None  # {"seats": [seat ids], "reason": "preflight:..."}: those seats run as their stand-in


class Adapter:
    name: str = ""
    family: str = ""
    # False: the salvage prompt never carries this provider's own reasoning/thinking text back to it
    # (Opus 5.5 safeguards refuse that as "reasoning_extraction"; live row 7, 7.1). Answer text still goes back.
    salvage_resends_reasoning: bool = True
    # True: a seat call that ends failed with no partial and no salvage (a fast failure) still gets the [fallback] call,
    # at full effort with the original prompt, down `fallback_chain` (OpenRouter only; fallback-chain row 1).
    fast_fail_fallback: bool = False
    # Grounded mode (councillib/grounded.py): True = the CLI can run with read-only file tools on a repo. Only then does
    # the runner put `_grounded` on this provider's seat dict. `grounded_reason` says why not (shown in the preflight
    # table, status and summary as `ungrounded`).
    grounded_capable: bool = False
    grounded_reason: str = "API provider: text only, no file tools"
    # Grounded probes ([grounded] probes): True = the adapter can let a grounded seat run read-only commands to confirm
    # a claim (claude: an allowlist of read-only shell commands; codex: its read-only sandbox, prompt only).
    probes_capable: bool = False

    def fallback_chain(self, seat: dict, cfg: dict) -> list:
        """Seat dicts of the full-effort fallback steps for a seat that failed fast, in order: [rerouted, stand-in].
        Empty (the default) = this provider keeps the salvage-form fallback only. Each dict carries `_full_call`."""
        return []

    def standin_start(self, seat: dict, cfg: dict) -> dict | None:
        """The seat dict a seat whose own model failed preflight starts with, from the first call (its stand-in model as
        an ordinary original call). None (the default) = this provider has no stand-in."""
        return None

    def build(self, seat: dict, prompt_path: str, workdir: str, limits: Limits, cfg: dict,
              launch_opts: dict | None = None) -> Launch:
        """`launch_opts` (D38) = {tag, otlp_port, log_path}, filled per call by the scheduler. None (or {}) gives
        exactly the argv and env this adapter built before the key existed."""
        raise NotImplementedError

    def extractor(self, seat: dict | None = None) -> Extractor:
        raise NotImplementedError

    def free_checks(self, seats: list, cfg: dict) -> list:
        """Must include a check named "inner_caps"."""
        raise NotImplementedError

    def model_matches(self, pin: str, reported: str) -> bool:
        return reported.startswith(pin)

    def salvage_seat(self, seat: dict, cfg: dict, max_tokens: int) -> dict:
        """The seat dict for its ONE answer-salvage follow-up call (row 16): same provider and
        model, a short output cap, reasoning off or at its lowest. Subclasses add the provider's own
        form. The seat's tokens-form reasoning cap is dropped (8000 - 16000 reserve would raise)."""
        return salvage_base(seat, max_tokens)


class StubAdapter(Adapter):
    """Runs tests/stub_cli.py. Seat keys: stub_mode (str or list), stub_critique_mode (the critique call), stub_fail_free,
    stub_reported."""

    name = "stub"
    family = "stub"

    def salvage_seat(self, seat, cfg, max_tokens):
        s = salvage_base(seat, max_tokens)
        if "stub_salvage_mode" in seat:
            s["stub_mode"] = seat["stub_salvage_mode"]
        return s

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        mode = seat.get("stub_mode", "quick")
        if seat.get("_critique") and "stub_critique_mode" in seat:       # round 2 (--depth standard) of the same seat
            mode = seat["stub_critique_mode"]
        mode = shlex.split(mode) if isinstance(mode, str) else [str(m) for m in mode]
        return Launch(argv=[sys.executable, "-Es", STUB_CLI, *mode], env=_tag_env(dict(os.environ), launch_opts),
                      cwd=workdir, stdin_path=prompt_path)

    def extractor(self, seat=None):
        seat = seat or {}
        return RawTextExtractor(seat.get("stub_reported", seat.get("model")))

    def free_checks(self, seats, cfg):
        out = []
        for s in seats:
            bad = bool(s.get("stub_fail_free"))
            out.append(Check("stub", "free", "stub:%s" % s.get("id", "?"),
                             "FAIL" if bad else "PASS", "stub free check failed" if bad else "ok"))
        out.append(Check("stub", "free", "inner_caps", "PASS", "stub has no inner caps"))
        return out


_REGISTRY: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    _REGISTRY[adapter.name] = adapter
    return adapter


def known(name) -> bool:
    return isinstance(name, str) and name in FAMILIES


def get(name: str) -> Adapter:
    if name in _REGISTRY:
        return _REGISTRY[name]
    if name in FAMILIES:
        raise NotImplementedError("adapter %r is not implemented yet" % name)
    raise KeyError("unknown provider %r" % name)


def is_anthropic(seat: dict) -> bool:
    """claude provider, or a model named anthropic/claude (also an agy "Claude ..." label)."""
    return seat.get("provider") == "claude" or bool(_ANTHROPIC_RE.search(str(seat.get("model", ""))))


# ============================================================ real adapters (row 7)
# claude and codex. agy, qwen and openrouter follow in the next section (row 8).
#
# Output-budget rule (owner, 2026-10-21): every paid call carries a reasoning effort and,
# where the CLI has a cap, a max-output-tokens cap. Advisors and the chair have separate
# defaults in [providers.<p>]; a seat (or the chair table) may override them with the same
# keys. Timeouts only catch hung seats; a kill never discards what was already received.

CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
CODEX_SUMMARIES = ("auto", "concise", "detailed", "none")      # model_reasoning_summary
TRUNC_NOTE = "\n\n[TRUNCATED: the seat hit its output-token cap before finishing]"

# env vars that make a child CLI think it runs inside another agent session
SCRUB_ENV = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
             "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CODEX_THREAD_ID",
             "CODEX_SESSION_ID", "CODEX_TASK_ID", "CODEX_SANDBOX", "CODEX_PLUGIN_ROOT")


def _clean_env(extra_unset=()):
    env = dict(os.environ)
    for k in tuple(SCRUB_ENV) + tuple(extra_unset):
        env.pop(k, None)
    return env


def _pcfg(cfg, name):
    return dict(((cfg or {}).get("providers") or {}).get(name) or {})


# ---------------------------------------------------------------- launch_opts (D38) and probe-signal flags (D41)
TAG_ENV = "COUNCIL_SEAT_TAG"
PROGRESS_SIGNALS = ("otel", "applog", "stream", "none")


def _opts(launch_opts):
    return launch_opts if isinstance(launch_opts, dict) else {}


def _tag_env(env, launch_opts):
    """The per-call tag goes into the child's env, so netwatch finds its process tree. No launch_opts, no change."""
    tag = _opts(launch_opts).get("tag")
    if tag:
        env[TAG_ENV] = str(tag)
    return env


def signals_on(cfg):
    """`[liveness] signals` (D41): the probe-signal flags (OTel exporter, detailed reasoning summaries, agy --v=3
    log) reach a live launch only while this is the boolean true. Shipped false until row 34."""
    return ((cfg or {}).get("liveness") or {}).get("signals") is True


def codex_signal_args(seat, pc, cfg, launch_opts):
    """The per-launch `-c` flags the gate adopted for codex (25-gate.md; the key shape row 21 proved).
    Nothing unless `[liveness] signals` is true. The detailed reasoning summary needs no port, so it also shows
    in a dry run; the OTel exporter needs the call's own sink port (`launch_opts.otlp_port`). A salvage call
    takes neither."""
    if not signals_on(cfg) or seat.get("_salvage"):
        return []
    out = []
    port = _opts(launch_opts).get("otlp_port")
    if pc.get("progress_signal") == "otel" and port:
        out += ["-c", 'otel.exporter={otlp-http={endpoint="http://127.0.0.1:%d/v1/logs",protocol="json"}}' % int(port),
                "-c", "otel.log_user_prompt=false"]
    if pc.get("reasoning_summary") in CODEX_SUMMARIES and pc["reasoning_summary"] != "none":
        out += ["-c", "model_reasoning_summary=%s" % pc["reasoning_summary"]]
    return out


def agy_signal_args(seat, pc, cfg, launch_opts, workdir):
    """agy `--v=3 --log-file <log>` (applog signal), only with signals on, `progress_signal = "applog"` and a
    `launch_opts.log_path` inside the seat's own workdir (mode 700; the log may hold prompt text)."""
    log = _opts(launch_opts).get("log_path")
    if not signals_on(cfg) or seat.get("_salvage") or pc.get("progress_signal") != "applog" or not log:
        return []
    wd, lp = os.path.realpath(workdir), os.path.realpath(log)
    if os.path.commonpath([wd, lp]) != wd:
        raise ValueError("agy log_path must be inside the seat workdir")
    return ["--v=3", "--log-file", str(log)]


def is_chair(seat):
    return seat.get("role") == "chair" or seat.get("id") == "chair"


def salvage_base(seat, max_tokens):
    """Common part of Adapter.salvage_seat: effort form, no reasoning cap, short output cap."""
    s = dict(seat)
    s.pop("reasoning_max_tokens", None)
    s.pop("_grounded", None)            # the follow-up call is text only: it carries no repo
    s.pop("_critique", None)            # a salvage of a critique is a salvage call, not a second critique
    s.update(role="salvage", _salvage=True, max_output_tokens=int(max_tokens),
             reasoning_form="effort", ignores_reasoning_cap=True)
    return s


def salvage_reasoning(seat, pc, default="off"):
    """Salvage reasoning setting of an OpenRouter-style request: "off" (reasoning.enabled=false) or an
    effort name ("none", "minimal", "low"...). Seat key, then provider key."""
    for src in (seat or {}, pc or {}):
        if src.get("salvage_reasoning"):
            return str(src["salvage_reasoning"])
    return default


def budget(seat, pc):
    """-> (reasoning_effort | None, max_output_tokens | None) for this seat or the chair.

    Precedence: the seat's own key, then the provider's chair_* key (chair only), then
    the provider's advisor key."""
    chair = is_chair(seat)
    effort = seat.get("reasoning_effort")
    cap = seat.get("max_output_tokens")
    if effort is None and chair:
        effort = pc.get("chair_reasoning_effort")
    if cap is None and chair:
        cap = pc.get("chair_max_output_tokens")
    if effort is None:
        effort = pc.get("reasoning_effort")
    if cap is None:
        cap = pc.get("max_output_tokens")
    return effort, cap


DEFAULT_ANSWER_RESERVE = 16000
REASONING_FORMS = ("effort", "tokens")


def _pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _stale_ratio(*srcs):
    for src in srcs:
        if "reasoning_ratio" in (src or {}):
            raise ValueError("reasoning_ratio was removed (row 14): reasoning is capped at "
                             "max_output_tokens - answer_reserve_tokens, or sent as an effort")


def answer_reserve(seat, pc, cfg=None):
    """Tokens that must stay free for the answer: the seat's own `answer_reserve_tokens`, then
    [providers.<p>], then the top level, then 16000. A bad value raises ValueError."""
    _stale_ratio(seat, pc, cfg)
    for src in (seat or {}, pc or {}, cfg or {}):
        if "answer_reserve_tokens" in src:
            r = src["answer_reserve_tokens"]
            if not _pos_int(r):
                raise ValueError("answer_reserve_tokens %r must be a positive integer" % (r,))
            return r
    return DEFAULT_ANSWER_RESERVE


def reasoning_form(seat, pc):
    """How a provider is asked to limit reasoning: "effort" (a reasoning.effort level, default)
    or "tokens" (an explicit reasoning cap). Seat key, then provider key."""
    for src in (seat or {}, pc or {}):
        if "reasoning_form" in src:
            f = src["reasoning_form"]
            if f not in REASONING_FORMS:
                raise ValueError("reasoning_form %r must be one of %s" % (f, "/".join(REASONING_FORMS)))
            return f
    return "effort"


def ignores_reasoning_cap(seat, pc):
    for src in (seat or {}, pc or {}):
        if "ignores_reasoning_cap" in src:
            return bool(src["ignores_reasoning_cap"])
    return False


def reasoning_cap(seat, pc, cfg, cap):
    """The reasoning cap a request carries, or None (effort form, or no output cap).

    Tokens form: `reasoning_max_tokens` (seat, then provider) when given, else
    max_output_tokens - answer_reserve_tokens. Either way the answer reserve must stay free:
    cap - reasoning cap >= answer_reserve_tokens, else ValueError."""
    reserve = answer_reserve(seat, pc, cfg)         # also rejects a leftover reasoning_ratio
    if not cap or reasoning_form(seat, pc) != "tokens":
        return None
    rc = None
    for src in (seat or {}, pc or {}):
        if "reasoning_max_tokens" in src:
            rc = src["reasoning_max_tokens"]
            if not _pos_int(rc):
                raise ValueError("reasoning_max_tokens %r must be a positive integer" % (rc,))
            break
    if rc is None:
        rc = int(cap) - reserve
    if rc < 1 or int(cap) - rc < reserve:
        raise ValueError("max_output_tokens %d - reasoning cap %d leaves less than answer_reserve_tokens %d"
                         % (int(cap), rc, reserve))
    return rc


def _budget_seats(provider, pc, cfg, seats):
    """The seats whose budget is checked: the advisor default, the chair default, every roster
    seat of this provider and (when it is this provider) the configured chair."""
    out = [{"id": "x"}, {"id": "chair"}]
    out += [dict(s) for s in (seats or []) if s.get("provider") == provider]
    ch = (cfg or {}).get("chair") or {}
    if ch.get("provider") == provider:
        out.append(dict(ch, id="chair", role="chair"))
    return out


def _budget_problems(provider, pc, efforts, cap_supported, cfg=None, reserve=False, seats=()):
    """Free-check FAIL reasons for the output budget (owner rule 2026-10-21: a council must
    complete). reserve=True adds the answer-room rules for providers with a cap:
    max_output_tokens - (reasoning cap or 0) >= answer_reserve_tokens for every seat, and a
    provider marked `ignores_reasoning_cap` must use the effort form."""
    out = []
    for s in _budget_seats(provider, pc, cfg, seats):
        label = "chair" if is_chair(s) else ("seat %s" % s["id"] if s["id"] != "x" else "advisor")
        eff, cap = budget(s, pc)
        if eff not in efforts:
            out.append("%s reasoning_effort %r not one of %s" % (label, eff, "/".join(efforts)))
        if cap_supported and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 256):
            out.append("%s max_output_tokens %r must be an integer >= 256" % (label, cap))
            continue
        if not reserve:
            continue
        try:
            rc = reasoning_cap(s, pc, cfg, cap)
            res = answer_reserve(s, pc, cfg)
            form = reasoning_form(s, pc)
        except ValueError as e:
            out.append("%s: %s" % (label, e))
            continue
        if ignores_reasoning_cap(s, pc) and form != "effort":
            out.append("%s: provider is marked ignores_reasoning_cap but reasoning_form is %r "
                       "(must be \"effort\")" % (label, form))
        if cap is not None and cap - (rc or 0) < res:
            out.append("%s: max_output_tokens %s - reasoning cap %s < answer_reserve_tokens %s"
                       % (label, cap, rc or 0, res))
    return sorted(set(out), key=out.index)


def _capture(argv, timeout=25, env=None):
    """Run a free command. -> (rc | None, combined text). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, env=env or _clean_env(),
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + b"\n" + p.stderr).decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as e:
        return None, type(e).__name__


NOT_FOUND = "not found on PATH"     # preflight does not retry a row with this text: a missing binary cannot appear in 2 s


def _bin_missing(binp):
    """True when `binp` (a name looked up on PATH, or a path) is not an executable file: the CLI is simply not installed."""
    return shutil.which(binp) is None


def _missing_detail(name):
    return "%s CLI %s (install it, or set [providers.%s] bin)" % (name, NOT_FOUND, name)


def _version(text):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _vstr(t):
    return ".".join(str(x) for x in t) if t else "?"


# Providers whose launch takes no inner timer from the runner ceiling (agy omits --print-timeout, D28), so only they may
# run with no ceiling (`ceiling_s = 0`, row 35), and only while hedged (owner ruling 1). claude, qwen and openrouter derive
# their inner caps from the ceiling (claude_env, qwen_guards_ms, or_socket_timeout) and must keep one.
NO_CEILING_PROVIDERS = ("codex", "agy")
# codex with no ceiling: ~/.codex [model_providers.<council>] stream_idle_timeout_ms must still be >= this (D27). It is an
# idle timer the CLI owns (a proven terminal state, not a wall clock); ~/.codex is never edited.
NO_CEILING_CODEX_IDLE_MS = 3600000


def _no_ceiling(what):
    return ValueError("%s: no runner ceiling (ceiling_s = 0) is only for a hedged %s seat; preflight FAILs it"
                      % (what, "/".join(NO_CEILING_PROVIDERS)))


def _max_ceiling(cfg, provider):
    """Largest runner ceiling this provider can be given, or None when one of them is no ceiling (row 35). Reads
    `timing.ceiling_of` like council_run._limits: a [timeouts.<provider>] ceiling_s replaces the default; the chair
    table applies on top when the chair runs on this provider."""
    vals = [timing.ceiling_of(cfg or {}, provider)]
    if ((cfg or {}).get("chair") or {}).get("provider") == provider:
        vals.append(timing.ceiling_of(cfg or {}, provider, chair=True))
    return None if None in vals else max(vals)


def _json_lines(buf, chunk):
    """Append `chunk` to the bytearray `buf` and return the complete JSON objects in it."""
    buf += chunk
    out = []
    while True:
        i = buf.find(b"\n")
        if i < 0:
            return out
        line = bytes(buf[:i]).strip()
        del buf[:i + 1]
        if not line.startswith(b"{"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue


# ---------------------------------------------------------------- claude
class ClaudeExtractor(Extractor):
    """claude -p --output-format stream-json --verbose --include-partial-messages.

    Liveness needs no code here: the Supervisor resets its idle clock on every stdout
    byte, and claude prints a system/thinking_tokens event about every 1.2 s while it
    reasons. The extractor just ignores those events.

    Text: the result event's text for a normal result, else the text deltas seen so far
    (a killed run, or an error result: its `result` field then holds the error message,
    not the answer). An error result with some streamed text is kept as an answer marked
    TRUNCATED; it is an error only when there is no text at all.

    Output cap (CLAUDE_CODE_MAX_OUTPUT_TOKENS): Claude Code re-asks up to 3 more times
    after a request hits the cap (4 requests in all, verified: num_turns 4, four
    stop_reason max_tokens), then exits 1 with `api_error: max_output_tokens`. The text
    streamed meanwhile is the seat's answer; `salvage_exit()` tells the scheduler to
    accept that exit 1."""

    def __init__(self):
        self._buf = bytearray()
        self._deltas = []
        self._thinking = []            # thinking summaries (--thinking-display summarized)
        self._model = None
        self._result = None
        self.capped_requests = 0       # requests that ended stop_reason max_tokens

    def feed(self, chunk):
        for ev in _json_lines(self._buf, chunk):
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                self._model = ev.get("model") or self._model
            elif t == "stream_event":
                inner = ev.get("event") or {}
                d = inner.get("delta") or {}
                if inner.get("type") == "message_delta" and d.get("stop_reason") == "max_tokens":
                    self.capped_requests += 1
                if (inner.get("type") == "content_block_delta" and d.get("type") == "text_delta"
                        and not ev.get("parent_tool_use_id")):
                    self._deltas.append(str(d.get("text") or ""))
                if (inner.get("type") == "content_block_delta" and d.get("type") == "thinking_delta"
                        and not ev.get("parent_tool_use_id")):
                    self._thinking.append(str(d.get("thinking") or ""))
            elif t == "result":
                self._result = ev

    def _answer(self):
        return "".join(self._deltas)

    def thinking_text(self):
        return "".join(self._thinking)

    def partial_text(self):
        """The answer deltas so far; with none, the labelled thinking summary (as qwen and GLM do),
        so a seat that burned its budget on reasoning still leaves what it thought."""
        text = self._answer()
        if text.strip():
            return text
        think = self.thinking_text()
        return THINKING_NOTE + think if think.strip() else ""

    def _text(self):
        r = self._result
        # After a cap hit Claude Code continues in a new request; the result event then
        # carries only the LAST request's text (verified), so the joined deltas are the answer.
        if (r is not None and not r.get("is_error") and not self.capped_requests
                and isinstance(r.get("result"), str) and r["result"].strip()):
            return r["result"]
        return self._answer()

    def salvage_exit(self):
        """True when the CLI exited non-zero only because of the output cap and did
        stream some answer text."""
        r = self._result
        return bool(r and r.get("is_error") and r.get("api_error") == "max_output_tokens"
                    and self._answer().strip())

    def final_text(self):
        if self._result is None:
            return None            # killed or crashed: the partial is all there is
        text = self._text()
        if not text.strip():
            return None
        r = self._result
        if r.get("is_error") or r.get("stop_reason") == "max_tokens":
            return text.rstrip() + TRUNC_NOTE
        return text

    def model_reported(self):
        return self._model

    def usage(self):
        """Result event: `usage.{input_tokens,output_tokens}`, `usage.output_tokens_details.thinking_tokens`
        (inside output_tokens) and `total_cost_usd` (the whole call, every model billed: not one modelUsage
        entry). No result event (killed run) -> all None."""
        r = self._result
        if not isinstance(r, dict):
            return usage_dict()
        return usage_dict(_dig(r, "usage", "input_tokens"), _dig(r, "usage", "output_tokens"),
                          _dig(r, "usage", "output_tokens_details", "thinking_tokens"),
                          r.get("total_cost_usd"))

    def error(self):
        r = self._result
        if r is None or not r.get("is_error") or self._text().strip():
            return None
        return ("claude_%s %s" % (r.get("subtype") or "error", r.get("terminal_reason") or "")).strip()

    def result_event(self):
        return self._result


CLAUDE_DISPLAY = "summarized"


def _claude_display(seat, pc):
    for src in (seat or {}, pc or {}):
        if "thinking_display" in src:
            return src["thinking_display"]
    return CLAUDE_DISPLAY


def claude_display_problems(seat, pc):
    """FAIL reasons for the seat's thinking display and for extra_args that undo it. Only
    "summarized" keeps the reasoning (the binary says "highlights" needs an Anthropic-hosted session
    and can fall back to "omitted")."""
    out = []
    d = _claude_display(seat, pc)
    if d != CLAUDE_DISPLAY:
        out.append("thinking_display %r must be %r (omitted keeps nothing)" % (d, CLAUDE_DISPLAY))
    args = [str(a) for a in (pc.get("extra_args") or [])] + [str(a) for a in ((seat or {}).get("extra_args") or [])]
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a.startswith("--thinking-display"):
            val = a.split("=", 1)[1] if "=" in a else nxt
            if val != CLAUDE_DISPLAY:
                out.append("extra_args --thinking-display %s must be summarized" % val)
        elif a.startswith("--thinking") and not a.startswith("--thinking-"):
            val = a.split("=", 1)[1] if "=" in a else nxt
            if val == "disabled":
                out.append("extra_args --thinking disabled")
        elif a.startswith("--max-thinking-tokens"):
            val = a.split("=", 1)[1] if "=" in a else nxt
            if val.strip() in ("0", ""):
                out.append("extra_args --max-thinking-tokens 0 turns thinking off")
    return out


def claude_env(ceiling_s, pc, max_output_tokens):
    """Inner caps for one launch (R2-W5). Computed from the launch's own ceiling, so the
    chair (1500 s) gets a larger value than a seat (1200 s). `[providers.claude.env]` can
    only RAISE the two timeouts; any other key there is used as given."""
    if ceiling_s is None:
        raise _no_ceiling("claude_env")
    ms = int(float(ceiling_s) * 1000)
    env = {"API_TIMEOUT_MS": ms + 60000, "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS": ms,
           "CLAUDE_CODE_MAX_RETRIES": 1}
    for k, v in (pc.get("env") or {}).items():
        if k in ("API_TIMEOUT_MS", "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"):
            try:
                env[k] = max(env[k], int(v))
            except (TypeError, ValueError):
                pass
        else:
            env[k] = v
    if max_output_tokens:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = int(max_output_tokens)
    return {k: str(v) for k, v in env.items()}


# Grounded claude seat: these tools and no others (verified: init event lists exactly Glob, Grep, Read).
CLAUDE_GROUNDED_TOOLS = "Read,Grep,Glob"
CLAUDE_GROUNDED_DENY = "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,Agent"
# With `probes` on (opt-in: [grounded] probes = ["claude"] or seat probes = true) the claude seat also gets Bash, but only
# these read-only command prefixes (anything else is refused in -p mode); the deny rules stop git's file-writing and
# pager-running options.
CLAUDE_PROBE_BASH = ("git log", "git show", "git diff", "git blame", "git ls-files", "wc", "ls")     # no git grep: -O<cmd> runs a pager
CLAUDE_PROBE_DENY = ("Bash(*--output*)", "Bash(*--open-files-in-pager*)", "Bash(*--ext-diff*)", "Bash(*--textconv*)",
                     "Bash(git -c*)", "Bash(git config*)")


class ClaudeAdapter(Adapter):
    name = "claude"
    family = "anthropic"
    salvage_resends_reasoning = False
    grounded_capable = True
    probes_capable = True

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        pc = _pcfg(cfg, "claude")
        effort, cap = budget(seat, pc)
        rcap = reasoning_cap(seat, pc, cfg, cap)    # None unless reasoning_form = "tokens"; ValueError if no answer room
        argv = [pc.get("bin", "claude"), "-p", "--model", seat["model"],
                "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--tools", "", "--no-session-persistence"]
        gr = seat.get("_grounded")
        cwd = workdir
        if gr:
            # Grounded (council.py run --grounded): Read, Grep and Glob only, on the repo; no shell, no edit, no web.
            # cwd = the root, so relative citations resolve; --restricted confines the file tools to the working
            # directories (cwd and --add-dir). Images are the runner's copies in the seat's own staging dir.
            tools, allowed, deny = CLAUDE_GROUNDED_TOOLS, [CLAUDE_GROUNDED_TOOLS], [CLAUDE_GROUNDED_DENY]
            if gr.get("probes"):
                # one argv element per rule: the lists are variadic ("Bash(git *) Edit" in --help), so a rule with a
                # space in it is never split by our own comma-join
                tools += ",Bash"
                allowed += ["Bash(%s:*)" % c for c in CLAUDE_PROBE_BASH]
                deny = [",".join(x for x in CLAUDE_GROUNDED_DENY.split(",") if x != "Bash")] + list(CLAUDE_PROBE_DENY)
            argv[argv.index("--tools") + 1] = tools
            argv += ["--allowed-tools"] + allowed + ["--disallowed-tools"] + deny
            if gr.get("restricted", True):
                argv.append("--restricted")
            for d in sorted({os.path.dirname(i) for i in gr.get("images") or ()}):
                argv += ["--add-dir", d]
            cwd = gr["root"]
        if pc.get("safe_mode", True):
            argv.append("--safe-mode")
        if effort:
            argv += ["--effort", str(effort)]
        # Claude Code forces display "omitted" for -p unless told otherwise (row 13: $0.71 of thinking left no
        # text). "summarized" keeps the reasoning as a summary in the stream, so a seat never ends silent.
        if seat.get("_thinking_off"):
            argv += ["--thinking", "disabled"]      # salvage call only: hidden flag, in the 2.1.305 binary
        else:
            argv += ["--thinking-display", str(_claude_display(seat, pc))]
        if rcap:
            # honoured only by models on enabled-type thinking; opus-5-5 uses adaptive thinking and ignores it
            argv += ["--max-thinking-tokens", str(rcap)]
        argv += [str(a) for a in (pc.get("extra_args") or [])]
        argv += [str(a) for a in (seat.get("extra_args") or [])]
        env = _clean_env()
        env.update(claude_env(limits.ceiling_s, pc, cap))
        return Launch(argv=argv, env=_tag_env(env, launch_opts), cwd=cwd, stdin_path=prompt_path)

    def extractor(self, seat=None):
        return ClaudeExtractor()

    def salvage_seat(self, seat, cfg, max_tokens):
        pc = _pcfg(cfg, "claude")
        s = salvage_base(seat, max_tokens)
        s["reasoning_effort"] = pc.get("salvage_reasoning_effort", "low")
        s["_thinking_off"] = pc.get("salvage_thinking", "disabled") == "disabled"
        return s

    def free_checks(self, seats, cfg):
        pc = _pcfg(cfg, "claude")
        binp = pc.get("bin", "claude")
        pins = (cfg or {}).get("pins") or {}
        out = []
        missing = _bin_missing(binp)
        rc, text = (None, "") if missing else _capture([binp, "--version"])
        have, need = _version(text), _version(pins.get("claude_min", "0.0.0"))
        if missing:
            out.append(Check("claude", "free", "version", "FAIL",
                             "claude CLI %s (install Claude Code, or set [providers.claude] bin)" % NOT_FOUND))
        elif rc != 0 or have is None:
            out.append(Check("claude", "free", "version", "FAIL", "claude --version failed"))
        elif have < need:
            out.append(Check("claude", "free", "version", "FAIL",
                             "%s < claude_min %s" % (_vstr(have), _vstr(need))))
        else:
            out.append(Check("claude", "free", "version", "PASS",
                             "%s >= %s" % (_vstr(have), _vstr(need))))
        rc, text = (None, "") if missing else _capture([binp, "auth", "status", "--json"])
        try:
            st = json.loads(text[text.index("{"):text.rindex("}") + 1])
        except ValueError:
            st = {}
        if missing:
            out.append(Check("claude", "free", "auth", "FAIL", "claude auth status: skipped, claude CLI %s" % NOT_FOUND))
        elif rc == 0 and st.get("loggedIn") is True:      # keep only these two keys (W7)
            out.append(Check("claude", "free", "auth", "PASS",
                             "loggedIn authMethod=%s" % st.get("authMethod")))
        else:
            out.append(Check("claude", "free", "auth", "FAIL", "claude auth status: not logged in"))
        # inner_caps: computed per launch from the launch's ceiling, so check both
        # ceilings the provider can get, and the retry cap.
        top = _max_ceiling(cfg, "claude")
        _, cap = budget({"id": "x"}, pc)
        probs = []
        ceils = {timing.ceiling_of(cfg, "default"), top}
        if None in ceils:                                   # row 35: claude's inner caps come from its ceiling
            probs.append("no ceiling (ceiling_s = 0): claude needs one for API_TIMEOUT_MS")
        for c in sorted(c for c in ceils if c is not None):
            e = claude_env(c, pc, cap)
            if int(e["API_TIMEOUT_MS"]) < c * 1000 or int(e["CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"]) < c * 1000:
                probs.append("timeout below ceiling %ds" % c)
            if int(e["CLAUDE_CODE_MAX_RETRIES"]) > 1:
                probs.append("CLAUDE_CODE_MAX_RETRIES=%s > 1" % e["CLAUDE_CODE_MAX_RETRIES"])
        out.append(Check("claude", "free", "inner_caps", "FAIL" if probs else "PASS",
                         "; ".join(sorted(set(probs))) or
                         "API_TIMEOUT_MS = ceiling+60s, first-byte = ceiling (per launch, up to %ds), retries 1" % (top or 0)))
        probs = _budget_problems("claude", pc, CLAUDE_EFFORTS, True, cfg, True, seats)
        a, ac = budget({"id": "x"}, pc)
        c_, cc = budget({"id": "chair"}, pc)
        try:
            res, form = answer_reserve({}, pc, cfg), reasoning_form({}, pc)
        except ValueError:
            res, form = "?", "?"
        out.append(Check("claude", "free", "output_budget", "FAIL" if probs else "PASS",
                         "; ".join(probs) or "advisor %s/%s tok, chair %s/%s tok (thinking + answer, one request), "
                         "answer reserve %s, reasoning form %s" % (a, ac, c_, cc, res, form)))
        dprobs = []
        for s_ in [{"id": "x"}] + [x for x in (seats or []) if x.get("provider") == "claude"]:
            dprobs += claude_display_problems(s_, pc)
        ch = (cfg or {}).get("chair") or {}
        if ch.get("provider") == "claude":
            dprobs += claude_display_problems(ch, pc)
        out.append(Check("claude", "free", "thinking_display", "FAIL" if dprobs else "PASS",
                         "; ".join(sorted(set(dprobs))) or "--thinking-display summarized; the reasoning is kept as a summary"))
        return out


# ---------------------------------------------------------------- codex
class CodexExtractor(Extractor):
    """codex exec --json: thread.started, turn.started, item.completed(agent_message),
    turn.completed. Events land only at item boundaries, so a buffered seat shows nothing
    until the end. The model is not in the events (codex_model_in_events: no)."""

    def __init__(self):
        self._buf = bytearray()
        self._msgs = []
        self._failed = None
        self._done = False
        self._usage = None

    def feed(self, chunk):
        for ev in _json_lines(self._buf, chunk):
            t = ev.get("type")
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if t == "item.completed" and item.get("type") in ("agent_message", "assistant_message"):
                self._msgs.append(str(item.get("text") or ""))
            elif t == "turn.completed":
                self._done = True
                if isinstance(ev.get("usage"), dict):
                    self._usage = ev["usage"]
            elif t == "turn.failed":
                err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
                self._failed = str(err.get("message") or "turn.failed")[:200]

    def partial_text(self):
        return "\n\n".join(m for m in self._msgs if m.strip())

    def final_text(self):
        # the last agent message is the answer; the scheduler prefers the -o file when present
        for m in reversed(self._msgs):
            if m.strip():
                return m
        return None

    def usage(self):
        """turn.completed `usage.{input_tokens,output_tokens,reasoning_output_tokens}`. Subscription: no
        cost. Whether output_tokens includes the reasoning is inferred, not proven (row 1)."""
        u = self._usage
        return usage_dict(u.get("input_tokens"), u.get("output_tokens"), u.get("reasoning_output_tokens")) \
            if u else usage_dict()

    def error(self):
        return None if self._msgs else self._failed


# Features a grounded codex call turns off (names from `codex features list`): apps/plugins (their connectors reach the
# network) and the browser/computer tools; web search is on by default in codex, so `-c web_search="disabled"` (the key
# codex's own deprecation message names) turns it off. The sandbox itself is `-s read-only` (no network).
CODEX_GROUNDED_DISABLE = ("apps", "plugins", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
                          "computer_use")


CODEX_MCP_NAME = re.compile(r"^[A-Za-z0-9_-]+$")      # a server name codex's `-c mcp_servers.<name>.enabled=false` can address


class CodexAdapter(Adapter):
    name = "codex"
    family = "openai"
    grounded_capable = True
    probes_capable = True            # prompt only: `-s read-only` already lets codex run commands that write nothing

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        pc = _pcfg(cfg, "codex")
        effort, _cap = budget(seat, pc)            # codex has no output-token cap
        if (cfg or {}).get("_canary"):
            effort = pc.get("canary_reasoning_effort", "low")
        last = os.path.join(workdir, "last.txt")
        gr = seat.get("_grounded")
        argv = [pc.get("bin", "codex"), "exec", "--json", "--skip-git-repo-check", "--ephemeral"]
        if gr:
            # Grounded: the same read-only sandbox (no writes, no network), rooted at the repo, with the images attached
            # natively (`-i` takes a list, so a flag always follows it). NOT --add-dir: codex makes those directories
            # writable. The sandbox does not confine READS to the root (codex can read anything the user can); the
            # quote check only trusts the root.
            for img in gr.get("images") or ():
                argv += ["-i", img]
            argv += ["-C", gr["root"]]
            argv += ["-c", 'web_search="disabled"']
            for feat in CODEX_GROUNDED_DISABLE:
                argv += ["--disable", feat]
        argv += ["-s", "read-only", "-m", seat["model"], "-o", last]
        if pc.get("council_provider"):
            argv += ["-c", "model_provider=%s" % pc["council_provider"]]
        if effort:
            argv += ["-c", "model_reasoning_effort=%s" % effort]
        for name in self.mcp_disable_names(pc)[0]:
            argv += ["-c", "mcp_servers.%s.enabled=false" % name]
        argv += codex_signal_args(seat, pc, cfg, launch_opts)
        argv += [str(a) for a in (pc.get("extra_args") or [])]
        argv += [str(a) for a in (seat.get("extra_args") or [])]
        argv.append("-")
        return Launch(argv=argv, env=_tag_env(_clean_env(), launch_opts), cwd=workdir, stdin_path=prompt_path,
                      final_text_path=last)

    def extractor(self, seat=None):
        return CodexExtractor()

    def salvage_seat(self, seat, cfg, max_tokens):
        s = salvage_base(seat, max_tokens)          # codex 0.163.0 has no output-token cap: only the effort applies
        s["reasoning_effort"] = _pcfg(cfg, "codex").get("salvage_reasoning_effort", "low")
        return s

    def _toml(self, pc):
        path = pc.get("config_path") or os.path.join(
            os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "config.toml")
        try:
            with open(os.path.expanduser(path), "rb") as f:
                return tomllib.load(f)
        except (OSError, ValueError):
            return None

    def mcp_disable_names(self, pc):
        """-> (names to disable, names that cannot be). A council seat must not load the user's own MCP servers (startup
        cost, and a third-party server would see the question). `-c mcp_servers={}` is a no-op on codex 0.163.0 (a `-c`
        table is merged into the user's, so an empty one removes nothing); only one `-c mcp_servers.<name>.enabled=false`
        per server works, and the names come from the user's config.toml. codex splits the `-c` key on every "."
        and keeps quote characters, so a name outside [A-Za-z0-9_-] cannot be addressed and a try aborts the whole
        launch ("invalid transport in `mcp_servers.\"x\"`"): such a name is never emitted, only reported
        (free_checks). `[providers.codex] disable_user_mcp = false` turns this off."""
        if not pc.get("disable_user_mcp", True):
            return [], []
        servers = (self._toml(pc) or {}).get("mcp_servers")
        names = sorted(str(n) for n in servers) if isinstance(servers, dict) else []
        ok = [n for n in names if CODEX_MCP_NAME.match(n)]
        return ok, [n for n in names if n not in ok]

    def free_checks(self, seats, cfg):
        pc = _pcfg(cfg, "codex")
        binp = pc.get("bin", "codex")
        pins = (cfg or {}).get("pins") or {}
        out = []
        _ok, _bad = self.mcp_disable_names(pc)
        if _bad:
            out.append(Check("codex", "free", "mcp_servers", "WARN",
                             "%d user MCP server(s) cannot be disabled for a seat (name outside [A-Za-z0-9_-], "
                             "codex's -c cannot address it): %s" % (len(_bad), ", ".join(_bad[:5]))))
        missing = _bin_missing(binp)
        rc, text = (None, "") if missing else _capture([binp, "--version"])
        have = _version(text)
        pin, lo = _version(pins.get("codex", "")), _version(pins.get("codex_min", "0.0.0"))
        if missing:
            out.append(Check("codex", "free", "version", "FAIL", _missing_detail("codex")))
        elif rc != 0 or have is None:
            out.append(Check("codex", "free", "version", "FAIL", "codex --version failed"))
        elif pin and have != pin:
            out.append(Check("codex", "free", "version", "FAIL",
                             "%s != pinned %s" % (_vstr(have), _vstr(pin))))
        elif have < lo:
            out.append(Check("codex", "free", "version", "FAIL",
                             "%s < codex_min %s" % (_vstr(have), _vstr(lo))))
        else:
            out.append(Check("codex", "free", "version", "PASS", _vstr(have)))
        rc, text = (None, "") if missing else _capture([binp, "login", "status"])
        out.append(Check("codex", "free", "auth", "PASS" if "Logged in" in text else "FAIL",
                         "Logged in" if "Logged in" in text else
                         ("codex login status: skipped, codex CLI %s" % NOT_FOUND if missing
                          else "codex login status: not logged in")))
        conf = self._toml(pc)
        if conf is None:
            out.append(Check("codex", "free", "update_check", "WARN", "codex config.toml unreadable"))
        else:
            off = conf.get("check_for_update_on_startup") is False
            out.append(Check("codex", "free", "update_check", "PASS" if off else "WARN",
                             "check_for_update_on_startup=false" if off
                             else "check_for_update_on_startup is not false"))
        out.append(self._inner_caps(pc, cfg, conf))
        probs = _budget_problems("codex", pc, CODEX_EFFORTS, False)
        a, _ = budget({"id": "x"}, pc)
        c_, _ = budget({"id": "chair"}, pc)
        out.append(Check("codex", "free", "output_budget", "FAIL" if probs else "PASS",
                         "; ".join(probs) or "no output-token cap in this CLI; reasoning_effort "
                         "advisor=%s chair=%s" % (a, c_)))
        return out

    def _inner_caps(self, pc, cfg, conf):
        name = pc.get("council_provider")
        top = _max_ceiling(cfg, "codex")
        # no ceiling (row 35, D27): the CLI's own idle timer must still be at least an hour
        need_ms = NO_CEILING_CODEX_IDLE_MS if top is None else top * 1000
        accepted = list(pc.get("accepted_limits") or [])
        blk = ((conf or {}).get("model_providers") or {}).get(name) if name else None
        if blk is not None:
            probs = []
            if not blk.get("requires_openai_auth"):
                probs.append("requires_openai_auth is not true")
            if int(blk.get("stream_idle_timeout_ms", 0) or 0) < need_ms:
                probs.append("stream_idle_timeout_ms %s < %d" % (blk.get("stream_idle_timeout_ms"), need_ms))
            for k in ("request_max_retries", "stream_max_retries"):
                if k not in blk or int(blk[k]) > 1:
                    probs.append("%s %s not <= 1" % (k, blk.get(k)))
            return Check("codex", "free", "inner_caps", "FAIL" if probs else "PASS",
                         "; ".join(probs) or "[model_providers.%s] idle %sms >= %dms, retries %s/%s"
                         % (name, blk.get("stream_idle_timeout_ms"), need_ms,
                            blk.get("request_max_retries"), blk.get("stream_max_retries")))
        if accepted:
            return Check("codex", "free", "inner_caps", "WARN",
                         "accepted limits (no council provider block): " + ", ".join(map(str, accepted)))
        return Check("codex", "free", "inner_caps", "FAIL",
                     "no [model_providers.%s] block in codex config.toml and no accepted_limits" % name)




# ============================================================ real adapters (row 8)
# agy, qwen and openrouter. Output-budget levers (verified 2026-10-21):
#   agy         no output-token cap exists in the CLI (agy --help; 1.4.4). `--effort` is rejected
#               for the Gemini 3.8 labels ("--effort is not supported for model ..."), so the
#               reasoning lever is the label's own tier: Gemini 3.8 Flash (High|Medium|Low).
#   qwen        per-launch system-settings overlay (QWEN_CODE_SYSTEM_SETTINGS_PATH):
#               generationConfig.samplingParams.max_tokens and generationConfig.extra_body.reasoning.effort
#               (proved with --openai-logging: request carried max_tokens and reasoning.effort).
#   openrouter  request fields max_tokens and reasoning.effort.

OR_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")      # OpenRouter docs: reasoning.effort
AGY_TIERS = ("Low", "Medium", "High")
THINKING_NOTE = "[reasoning so far, no answer yet]\n"
_KEYISH = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")


def _scrub(text, n=160):
    return _KEYISH.sub("sk-...", str(text or "")).replace("\n", " ")[:n]


def _provider_seats(seats, provider):
    return [s for s in (seats or []) if s.get("provider") == provider]


def _form_detail(pc, cfg):
    try:
        return "reasoning form %s, answer reserve %s tok" % (reasoning_form({}, pc), answer_reserve({}, pc, cfg))
    except ValueError:
        return "reasoning form ?"


def _cap_advertised(model_entry):
    rs = (model_entry or {}).get("reasoning")
    return bool(isinstance(rs, dict) and rs.get("supports_max_tokens"))


def reasoning_cap_check(provider, seat, pc, model_entry):
    """Live /models check for one seat: the token form is only allowed for a model that advertises
    reasoning.supports_max_tokens (row 13: GLM 5 ignored a 6000 cap and reasoned 7,999 tokens).
    The effort form is always allowed. -> Check."""
    name = "reasoning_cap:%s" % seat.get("id", "?")
    slug = seat.get("model")
    form = reasoning_form(seat, pc)
    adv = _cap_advertised(model_entry)
    if form == "tokens" and not adv:
        return Check(provider, "free", name, "FAIL",
                     "%s does not advertise reasoning.max_tokens (supports_max_tokens) but reasoning_form is "
                     "\"tokens\"; use \"effort\"" % slug)
    if form == "tokens":
        return Check(provider, "free", name, "PASS", "%s advertises reasoning.max_tokens; token form" % slug)
    return Check(provider, "free", name, "PASS",
                 "%s: effort form%s" % (slug, "" if not adv else " (the model also advertises reasoning.max_tokens)"))


SALVAGE_OR_MODES = ("off", "none") + OR_EFFORTS


def salvage_reasoning_check(provider, seat, pc, model_entry):
    """Live /models check for the answer-salvage call of one OpenRouter-style seat (row 16). `off` sends
    reasoning.enabled=false; an effort name sends reasoning.effort. WARN, not FAIL: a salvage that cannot turn
    reasoning off still runs (at worst it fails and the seat stays failed). -> Check."""
    name = "salvage_reasoning:%s" % seat.get("id", "?")
    mode = salvage_reasoning(seat, pc)
    r = (model_entry or {}).get("reasoning")
    r = r if isinstance(r, dict) else {}
    sup = r.get("supported_efforts") or []
    if mode not in SALVAGE_OR_MODES:
        return Check(provider, "free", name, "FAIL", "salvage_reasoning %r not one of %s" % (mode, "/".join(SALVAGE_OR_MODES)))
    if mode == "off" and r.get("mandatory"):
        return Check(provider, "free", name, "WARN",
                     "%s: reasoning is mandatory, so enabled=false may be ignored; set salvage_reasoning to its "
                     "lowest effort (%s)" % (seat.get("model"), "/".join(sup) or "unlisted"))
    if mode != "off" and sup and mode not in sup:
        return Check(provider, "free", name, "WARN", "%s not in supported_efforts %s" % (mode, "/".join(sup)))
    return Check(provider, "free", name, "PASS", "salvage call: reasoning %s" % ("enabled=false" if mode == "off"
                                                                                  else "effort " + mode))


def salvage_provider_problems(cfg):
    """FAIL reasons for the per-provider salvage keys (preflight engine stage)."""
    out = []
    for name, efforts in (("claude", CLAUDE_EFFORTS), ("codex", CODEX_EFFORTS)):
        v = _pcfg(cfg, name).get("salvage_reasoning_effort")
        if v is not None and v not in efforts:
            out.append("%s salvage_reasoning_effort %r not one of %s" % (name, v, "/".join(efforts)))
    v = _pcfg(cfg, "claude").get("salvage_thinking")
    if v is not None and v not in ("disabled", "default"):
        out.append("claude salvage_thinking %r must be disabled or default" % (v,))
    v = _pcfg(cfg, "agy").get("salvage_model_tier")
    if v is not None and v not in AGY_TIERS:
        out.append("agy salvage_model_tier %r not one of %s" % (v, "/".join(AGY_TIERS)))
    for name in ("openrouter", "qwen"):
        srcs = [_pcfg(cfg, name)] + [x for x in ((cfg or {}).get("seat") or []) if x.get("provider") == name]
        for src in srcs:
            v = src.get("salvage_reasoning")
            if v is not None and v not in SALVAGE_OR_MODES:
                out.append("%s salvage_reasoning %r not one of %s" % (name, v, "/".join(SALVAGE_OR_MODES)))
    return out


# ---------------------------------------------------------------- run tables (row 29): [hedge] [fallback] [liveness]
HEDGE_KEYS = ("enabled", "providers", "factor", "floor_s", "cap_s", "cold_s", "quiet_s", "recheck_s", "max_defer")
FALLBACK_KEYS = ("enabled",)
LIVENESS_KEYS = ("signals", "sample_s", "grace_s", "no_conn_samples", "baseline_s")
SIGNAL_KEYS = ("progress_signal", "progress_event", "progress_exclude", "negative_evidence", "terminal_lines",
               "log_match", "reasoning_summary")
TERMINAL_REASONS = ("cli_idle_timeout", "stream_error", "cli_timeout", "quota_exhausted", "no_connection")
_SIGNAL_PROVIDER = {"otel": "codex", "applog": "agy"}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _table(cfg, name, keys, out):
    """-> the table (a dict) or None when absent; appends 'unknown key' and 'not a table' problems to `out`."""
    t = (cfg or {}).get(name)
    if t is None:
        return None
    if not isinstance(t, dict):
        out.append("[%s] must be a table" % name)
        return None
    for k in sorted(t):
        if k not in keys:
            out.append("[%s] unknown key %r (allowed: %s)" % (name, k, ", ".join(keys)))
    return t


def _flag(t, name, key, out):
    if key in t and not isinstance(t[key], bool):
        out.append("[%s] %s must be true or false, got %r" % (name, key, t[key]))


def ceiling_problems(cfg):
    """FAIL reasons for a `ceiling_s = 0` (no ceiling, row 35, D27) where it is not allowed. Owner ruling 1 drops the
    ceiling only for a seat whose hedge runs: the provider must be in NO_CEILING_PROVIDERS, buffered, listed in
    [hedge] providers with the hedge on. [timeouts.default] and [timeouts.chair] always keep a ceiling. A negative or
    non-numeric ceiling_s is a FAIL too (timing.ceiling_of raises on it)."""
    out = []
    t = (cfg or {}).get("timeouts") or {}
    if not isinstance(t, dict):
        return out
    h = (cfg or {}).get("hedge") or {}
    on = isinstance(h, dict) and h.get("enabled") is True
    listed = h.get("providers") if isinstance(h, dict) and isinstance(h.get("providers"), list) else []
    for name in sorted(t):
        tab = t[name]
        if name == "adaptive" or not isinstance(tab, dict) or "ceiling_s" not in tab:
            continue
        v = tab["ceiling_s"]
        if not _is_num(v) or v < 0:
            out.append("[timeouts.%s] ceiling_s must be 0 (no ceiling) or a number > 0, got %r" % (name, v))
            continue
        if v != 0:
            continue
        if name in ("default", "chair"):
            out.append("[timeouts.%s] ceiling_s = 0 (no ceiling) is only for one hedged seat's own table" % name)
        elif name not in NO_CEILING_PROVIDERS:
            out.append("[timeouts.%s] ceiling_s = 0 (no ceiling): only %s can run without one (their inner timers do "
                       "not come from the ceiling)" % (name, "/".join(NO_CEILING_PROVIDERS)))
        elif not (on and name in listed and _pcfg(cfg, name).get("mode", "buffered") == "buffered"):
            out.append("[timeouts.%s] ceiling_s = 0 (no ceiling) needs the hedge on for %s (owner ruling 1: the ceiling "
                       "is dropped only where the hedge runs)" % (name, name))
    return out


def hedge_problems(cfg):
    """FAIL reasons for `[hedge]` (D12 numbers; `providers` = the seats the gate marks hedge_safe, D26), plus
    `ceiling_problems` (a seat with no ceiling needs its hedge, row 35)."""
    out = []
    try:
        return _hedge_table_problems(cfg, out) + ceiling_problems(cfg)
    except (TypeError, ValueError) as e:                   # never crash the preflight table
        return out + ["[hedge]/[timeouts] %s" % e]


def _hedge_table_problems(cfg, out):
    t = _table(cfg, "hedge", HEDGE_KEYS, out)
    if t is None:
        return out
    _flag(t, "hedge", "enabled", out)
    for k in ("factor", "floor_s", "cap_s", "cold_s", "recheck_s"):
        if k in t and not (_is_num(t[k]) and t[k] > 0):
            out.append("[hedge] %s must be a number > 0, got %r" % (k, t[k]))
    if "quiet_s" in t and not (_is_num(t["quiet_s"]) and t["quiet_s"] >= 0):
        out.append("[hedge] quiet_s must be a number >= 0, got %r" % (t["quiet_s"],))
    if "max_defer" in t and not (_is_num(t["max_defer"]) and t["max_defer"] >= 1):
        out.append("[hedge] max_defer must be a number >= 1 (multiples of hedge_at), got %r" % (t["max_defer"],))
    lo, hi, cold = t.get("floor_s"), t.get("cap_s"), t.get("cold_s")
    if all(_is_num(x) and x > 0 for x in (lo, hi)) and lo > hi:
        out.append("[hedge] floor_s %r is above cap_s %r" % (lo, hi))
    if all(_is_num(x) and x > 0 for x in (lo, hi, cold)) and not lo <= cold <= hi:
        out.append("[hedge] cold_s %r must sit between floor_s %r and cap_s %r" % (cold, lo, hi))
    if "providers" in t:
        names = t["providers"]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            out.append("[hedge] providers must be a list of provider names")
        else:
            for n in names:
                if not known(n) or n == "stub":
                    out.append("[hedge] providers: unknown provider %r" % n)
                elif _pcfg(cfg, n).get("mode", "buffered") != "buffered":
                    out.append("[hedge] providers: %s is a stream seat (stream seats get salvage and one fallback, not a hedge)" % n)
    return out


def fallback_problems(cfg):
    out = []
    t = _table(cfg, "fallback", FALLBACK_KEYS, out)
    if t is not None:
        _flag(t, "fallback", "enabled", out)
    return out


def liveness_problems(cfg):
    out = []
    t = _table(cfg, "liveness", LIVENESS_KEYS, out)
    if t is None:
        return out
    _flag(t, "liveness", "signals", out)
    for k in ("sample_s", "baseline_s"):
        if k in t and not (_is_num(t[k]) and t[k] > 0):
            out.append("[liveness] %s must be a number > 0, got %r" % (k, t[k]))
    if "grace_s" in t and not (_is_num(t["grace_s"]) and t["grace_s"] >= 0):
        out.append("[liveness] grace_s must be a number >= 0, got %r" % (t["grace_s"],))
    n = t.get("no_conn_samples")
    if "no_conn_samples" in t and not (isinstance(n, int) and not isinstance(n, bool) and n >= 1):
        out.append("[liveness] no_conn_samples must be an integer >= 1, got %r" % (n,))
    return out


def _terminal_line_problem(e):
    """None when `e` is a valid terminal_lines entry: a substring | a list of substrings (all on one line) |
    {reason, match}."""
    def strs(v):
        return isinstance(v, str) and bool(v) or (isinstance(v, list) and bool(v) and all(isinstance(x, str) and x for x in v))
    if isinstance(e, dict):
        if set(e) - {"reason", "match"} or not strs(e.get("match")):
            return "dict entries are {reason, match} with a non-empty match"
        if "reason" in e and e["reason"] not in TERMINAL_REASONS:
            return "reason %r is not one of %s" % (e["reason"], "/".join(TERMINAL_REASONS))
        return None
    return None if strs(e) else "an entry is a non-empty string, a list of them, or {reason, match}"


def signal_problems(cfg):
    """FAIL reasons for the gate keys on `[providers.<p>]` (25-gate.md): progress_signal, progress_event,
    progress_exclude, negative_evidence, terminal_lines, log_match, reasoning_summary."""
    out = []
    for name, pc in sorted(((cfg or {}).get("providers") or {}).items()):
        if not isinstance(pc, dict):
            continue
        tag = "[providers.%s]" % name
        sig = pc.get("progress_signal")
        if sig is not None:
            if sig not in PROGRESS_SIGNALS:
                out.append("%s progress_signal %r not one of %s" % (tag, sig, "/".join(PROGRESS_SIGNALS)))
            elif sig in _SIGNAL_PROVIDER and name != _SIGNAL_PROVIDER[sig]:
                out.append("%s progress_signal %r is only defined for %s" % (tag, sig, _SIGNAL_PROVIDER[sig]))
        if "negative_evidence" in pc and not isinstance(pc["negative_evidence"], bool):
            out.append("%s negative_evidence must be true or false" % tag)
        for k in ("progress_event", "log_match"):
            if k in pc and not (isinstance(pc[k], str) and pc[k]):
                out.append("%s %s must be a non-empty string" % (tag, k))
        if "progress_exclude" in pc and not (isinstance(pc["progress_exclude"], list)
                                             and all(isinstance(x, str) and x for x in pc["progress_exclude"])):
            out.append("%s progress_exclude must be a list of non-empty strings" % tag)
        if "terminal_lines" in pc:
            tl = pc["terminal_lines"]
            if not isinstance(tl, list):
                out.append("%s terminal_lines must be a list" % tag)
            else:
                for i, e in enumerate(tl):
                    bad = _terminal_line_problem(e)
                    if bad:
                        out.append("%s terminal_lines[%d]: %s" % (tag, i, bad))
        if "reasoning_summary" in pc:
            if name != "codex":
                out.append("%s reasoning_summary is only defined for codex" % tag)
            elif pc["reasoning_summary"] not in CODEX_SUMMARIES:
                out.append("%s reasoning_summary %r not one of %s" % (tag, pc["reasoning_summary"], "/".join(CODEX_SUMMARIES)))
    return out


# ---------------------------------------------------------------- recorded quota state (D47)
QUOTA_FILE = "quota.json"


def _state_dir(cfg):
    return (cfg or {}).get("state_dir") or os.path.expanduser("~/.claude-council")


def _iso_epoch(text):
    """'2026-10-28T07:15:56Z' -> epoch seconds, or None."""
    import calendar
    try:
        return calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def read_quota(cfg):
    """-> {provider: {"until": iso, "reason": str, "recorded_at": iso}} from `<state_dir>/quota.json`.
    A missing or unreadable file is {}: the state only ever narrows the roster, it never blocks a run."""
    try:
        with open(os.path.join(_state_dir(cfg), QUOTA_FILE), "rb") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(data, dict) else {}


def record_quota(cfg, provider, until_iso, reason="quota_exhausted", now=None):
    """Record that `provider` is out of quota until `until_iso` (UTC, `%Y-%m-%dT%H:%M:%SZ`). Row 30's `_classify`
    calls this on a `quota_exhausted` seat. Atomic, mode 600 in a mode 700 dir. Raises ValueError on a bad time."""
    if _iso_epoch(until_iso) is None:
        raise ValueError("until must look like 2026-10-28T07:15:56Z")
    d = _state_dir(cfg)
    os.makedirs(d, mode=0o700, exist_ok=True)
    data = read_quota(cfg)
    data[str(provider)] = {"until": until_iso, "reason": str(reason)[:80],
                           "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))}
    path = os.path.join(d, QUOTA_FILE)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    _write_private(tmp, json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, path)


def quota_unavailable(cfg, now=None):
    """-> {provider: {"until", "reason"}} for providers whose recorded reset time is still ahead. No probe call."""
    now = time.time() if now is None else now
    out = {}
    for prov, rec in read_quota(cfg).items():
        until = _iso_epoch(rec.get("until"))
        if until is not None and until > now:
            out[prov] = {"until": rec["until"], "reason": str(rec.get("reason") or "quota_exhausted")}
    return out


# ---------------------------------------------------------------- OpenRouter routing prefs (D43, D45)
ROUTING_KEYS = ("require_parameters", "order", "ignore",       # never `only` or allow_fallbacks=false: a blip would lose the seat
                "sort", "preferred_min_throughput", "preferred_max_latency")
# P26 (run example-run): ordering preferences, never filters. `sort` (string form only) replaces the
# router's price-weighted default order; the two thresholds (a positive number: p50 tok/s, p50 seconds) move endpoints that
# miss them to the end of the list and never stop a request. Filters (`only`, `quantizations`, `max_price`, ...) stay refused.
ROUTING_SORTS = ("price", "throughput", "latency")
ROUTING_THRESHOLDS = ("preferred_min_throughput", "preferred_max_latency")


def _pos_number(v):
    """A positive, finite int or float; a TOML `true` is a bool, not a number."""
    import math
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def routing_prefs(cfg, seat):
    """The `provider` request block for one OpenRouter seat: `[providers.openrouter.provider]` overlaid by the
    seat's own `provider_prefs` (a seat's `provider` key is its adapter name, so it cannot hold the table).
    -> dict | None. D43: derivation and guard. The block IS sent (row 39): `OpenRouterAdapter.build` passes it to the
    seat child (`openrouter_seat.py --provider-json`) and into the request's `provider` field."""
    base = _pcfg(cfg, "openrouter").get("provider")
    own = (seat or {}).get("provider_prefs")
    prefs = {}
    for src in (base, own):
        if isinstance(src, dict):
            prefs.update(src)
    if (seat or {}).get("_reroute"):
        prefs.pop("order", None)               # fallback-chain row 1: the rerouted / stand-in call is not pinned to an endpoint
    return prefs or None


def routing_problems(cfg):
    out = []
    srcs = [("[providers.openrouter.provider]", _pcfg(cfg, "openrouter").get("provider"))]
    srcs += [("seat %r provider_prefs" % s.get("id"), s.get("provider_prefs"))
             for s in ((cfg or {}).get("seat") or []) if "provider_prefs" in s]
    for label, t in srcs:
        if t is None:
            continue
        if not isinstance(t, dict):
            out.append("%s must be a table" % label)
            continue
        for k in sorted(t):
            if k not in ROUTING_KEYS:
                out.append("%s: unknown key %r (allowed: %s; never `only` or allow_fallbacks = false)"
                           % (label, k, ", ".join(ROUTING_KEYS)))
        if "require_parameters" in t and not isinstance(t["require_parameters"], bool):
            out.append("%s: require_parameters must be true or false" % label)
        for k in ("order", "ignore"):
            if k in t and not (isinstance(t[k], list) and all(isinstance(x, str) and x for x in t[k])):
                out.append("%s: %s must be a list of provider names" % (label, k))
        if "sort" in t and not (isinstance(t["sort"], str) and t["sort"] in ROUTING_SORTS):
            out.append("%s: sort must be one of %s (string form only)" % (label, ", ".join(repr(s) for s in ROUTING_SORTS)))
        for k in ROUTING_THRESHOLDS:
            if k in t and not _pos_number(t[k]):
                out.append("%s: %s must be a positive number" % (label, k))
    return out


def _ep_names(e):
    tag = str(e.get("tag") or "")
    return {str(e.get("provider_name") or "").lower(), tag.split("/")[0].lower(), tag.lower()} - {""}


def routing_check(seat, pc, endpoints, prefs):
    """The D43/D45 guard for one seat against the endpoints snapshot (`GET /models/<slug>/endpoints`, free):
    FAIL when `require_parameters` leaves no endpoint that supports every parameter the seat sends at its
    `max_tokens`; WARN when an `order`/`ignore` name matches no endpoint or the first `order` endpoint cannot hold
    the budget. -> Check."""
    name = "routing:%s" % seat.get("id", "?")
    effort, cap = budget(seat, pc)
    sent = ["max_tokens"] * bool(cap) + ["reasoning"] * bool(effort or seat.get("reasoning_max_tokens"))
    eps = [e for e in endpoints if isinstance(e, dict)]
    ignore = {str(x).lower() for x in (prefs.get("ignore") or [])}
    live = [e for e in eps if not (_ep_names(e) & ignore)]
    fits = [e for e in live if all(q in (e.get("supported_parameters") or []) for q in sent)
            and (not cap or (isinstance(e.get("max_completion_tokens"), (int, float)) and e["max_completion_tokens"] >= cap))]
    biggest = max([e["max_completion_tokens"] for e in live if isinstance(e.get("max_completion_tokens"), (int, float))] or [0])
    if not fits:
        res = "FAIL" if prefs.get("require_parameters") else "WARN"
        return Check("openrouter", "free", name, res,
                     "no endpoint of %s supports %s at max_tokens %s (largest %s, %d endpoints after ignore)"
                     % (seat.get("model"), "+".join(sent) or "its parameters", cap, biggest, len(live)))
    notes = []
    names = {n for e in eps for n in _ep_names(e)}
    for k in ("order", "ignore"):
        for n in prefs.get(k) or []:
            if str(n).lower() not in names:
                notes.append("%s entry %r matches no endpoint" % (k, n))
    for n in (prefs.get("order") or [])[:1]:
        first = [e for e in eps if str(n).lower() in _ep_names(e)]
        if first and cap and not any(isinstance(e.get("max_completion_tokens"), (int, float))
                                     and e["max_completion_tokens"] >= cap for e in first):
            notes.append("order[0] %r holds less than max_tokens %s" % (n, cap))
    return Check("openrouter", "free", name, "WARN" if notes else "PASS",
                 "; ".join(notes) or "%d of %d endpoints take %s at max_tokens %s (require_parameters %s, order %s)"
                 % (len(fits), len(eps), "+".join(sent), cap, bool(prefs.get("require_parameters")),
                    prefs.get("order") or "none"))


def _canary_effort(seat, pc, cfg, effort):
    if (cfg or {}).get("_canary") and (seat.get("canary_reasoning_effort") or pc.get("canary_reasoning_effort")):
        return seat.get("canary_reasoning_effort") or pc["canary_reasoning_effort"]
    return effort


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


# ---------------------------------------------------------------- agy
# Copied verbatim from octo 11.14.2 scripts/helpers/agy-exec.sh:139 (inline) and :146 (file
# variant). Without the directive agy writes its answer to a "brain artifact" and the seat
# comes back empty.
AGY_INLINE_DIRECTIVE = ("CRITICAL OUTPUT RULES: Respond with your COMPLETE answer directly inline as plain text "
                        "in THIS response. Do NOT create files. Do NOT write artifacts or brain documents. "
                        "Do NOT use terminal, command, or file tools. Do NOT say 'see the artifact' or "
                        "reference any external document. Write the full answer here now, from your own "
                        "expertise.\n\n")
AGY_INLINE_DIRECTIVE_FILE = ("CRITICAL OUTPUT RULES: Respond with your COMPLETE answer directly inline as plain "
                             "text in THIS response. You may read ONLY the prompt file named below. Apart from "
                             "reading that one file, do NOT create files, do NOT write artifacts or brain "
                             "documents, and do NOT use terminal or command tools. Do NOT say 'see the "
                             "artifact' or reference any external document. Write the full answer here now.\n\n")
# one argv string may not exceed MAX_ARG_STRLEN (128 KiB); octo agy-exec.sh:211 switches to the
# file form above 100000 bytes of directive + prompt, and so do we
AGY_ARG_MAX_BYTES = 100000
_AGY_GEMINI = re.compile(r"^(Gemini|gemini-)")
_AGY_ROWS: list = []       # [(id, label)] from the last catalog fetch, for model_matches


def parse_agy_models(text):
    """`agy models` prints `Fetching available models...` then `id<TAB>label` lines."""
    rows = []
    for line in (text or "").splitlines():
        if "\t" in line:
            a, b = line.split("\t", 1)
            if a.strip() and b.strip():
                rows.append((a.strip(), b.strip()))
    return rows


class AgyExtractor(Extractor):
    """agy --output-format stream-json. Events: init, step_update(agent_response text_delta), result.

    The answer can be split over several agent_response steps (a step that hits the CLI's
    own output-token limit is retried inside agy), and `result.response` holds only the
    last one in some runs, so the partial is every delta joined in arrival order.

    A result whose status is not SUCCESS is an error even when the process exited 0 (row 4:
    status ERROR, exit 0), with ONE exception (row 16, from the row 15 raw): agy's first step hit
    its own output-token limit (`Your previous response was cut off ... Retries remaining: 3`),
    emitted an `error_message` step, then continued in a LATER agent_response step that reached
    state DONE with text, and still reported status ERROR. That seat has a real final answer, so
    it is ok with note `step_limit`. The rule is structural, not textual: the last
    agent_response step is DONE, has text, and comes after the last error_message step. A run
    whose last step is still ACTIVE (cut mid-answer), or whose only DONE response came BEFORE
    the error step, or that has no text at all, stays an error."""

    def __init__(self):
        self._buf = bytearray()
        self._deltas = []
        self._model = None
        self._result = None
        self._resp = {}                # step_index -> [state, text parts] for agent_response steps
        self._err_step = -1            # highest step_index of an error_message step
        self._errbuf = bytearray()
        self._cli_timeout = None       # stderr `[agy] print timeout after ...` (D28/D47)
        self._quota = None             # stderr AGY_ERROR RESOURCE_EXHAUSTED (D47)
        self._resets_in = None

    def feed_stderr(self, chunk):
        """agy's print-timeout warning is stderr-only (rc 0, result SUCCESS, row 23 c2), and the 429
        quota error is on stderr as `AGY_ERROR: {...}` (also on stdout as result.error)."""
        self._errbuf += chunk
        while True:
            i = self._errbuf.find(b"\n")
            if i < 0:
                if len(self._errbuf) > 65536:
                    del self._errbuf[:-4096]
                self._scan_stderr(bytes(self._errbuf).decode("utf-8", "replace"))   # last line, no newline yet
                return
            line = bytes(self._errbuf[:i]).decode("utf-8", "replace")
            del self._errbuf[:i + 1]
            self._scan_stderr(line)

    def _scan_stderr(self, line):
        if "[agy] print timeout after" in line and not self._cli_timeout:
            self._cli_timeout = _scrub(line, 80)
        if "AGY_ERROR:" in line and '"status":"RESOURCE_EXHAUSTED"' in line:
            self._quota = True
            m = re.search(r"Resets in ([0-9hms]+)", line)
            self._resets_in = m.group(1) if m else self._resets_in
        elif "Individual quota reached" in line:
            self._quota = True
            m = re.search(r"Resets in ([0-9hms]+)", line)
            self._resets_in = m.group(1) if m else self._resets_in

    def terminal_reason(self):
        """`cli_timeout` (print-timeout expiry: partial output, not an answer, D28/D47) or
        `quota_exhausted` (429 `Individual quota reached`: terminal, retrying only fails again, D47)."""
        if self._cli_timeout:
            return "cli_timeout"
        if self._quota_seen():
            return "quota_exhausted"
        return None

    def _quota_seen(self):
        if self._quota:
            return True
        r = self._result
        return bool(r) and r.get("status") != "SUCCESS" and "Individual quota reached" in str(r.get("error") or "")

    def feed(self, chunk):
        for ev in _json_lines(self._buf, chunk):
            kind = ev.get("event")
            if kind == "init":
                self._model = (ev.get("init") or {}).get("model") or self._model
            elif kind == "step_update":
                su = ev.get("step_update") or {}
                idx = su.get("step_index")
                if su.get("step_type") == "error_message" and isinstance(idx, int):
                    self._err_step = max(self._err_step, idx)
                if su.get("step_type") == "agent_response":
                    ent = self._resp.setdefault(idx, ["", []])
                    if su.get("state"):
                        ent[0] = su["state"]
                    if su.get("text_delta"):
                        self._deltas.append(str(su["text_delta"]))
                        ent[1].append(str(su["text_delta"]))
            elif kind == "result":
                self._result = ev.get("result") or {}

    def partial_text(self):
        return "".join(self._deltas)

    def _success(self):
        return self._result is not None and self._result.get("status") == "SUCCESS"

    def _finished_answer(self):
        """True when status is not SUCCESS but the LAST agent_response step is DONE with text and
        comes after the last error_message step: a real final answer (see the class docstring)."""
        if self._result is None or self._success() or not self._resp:
            return False
        idx = [i for i in self._resp if isinstance(i, int)]
        if not idx:
            return False
        last = max(idx)
        state, parts = self._resp[last]
        return state == "DONE" and last > self._err_step and bool("".join(parts).strip())

    def final_text(self):
        if self._cli_timeout:
            return None                 # print-timeout: the partial is kept, but it is not an answer
        if self._success():
            resp = self._result.get("response")
            if isinstance(resp, str) and resp.strip():
                return resp
            text = self.partial_text()
            return text if text.strip() else None
        if self._finished_answer():
            # response may hold only the last step; the joined deltas are the whole answer
            return self.partial_text()
        return None

    def model_reported(self):
        return self._model

    def usage(self):
        """result.usage.{input_tokens,output_tokens,thinking_tokens}: the SUM over every step of the
        conversation (a step-limit retry counts), thinking inside output. Subscription: no cost.
        No result event -> all None (per-step updates are partial, not summed here)."""
        u = (self._result or {}).get("usage")
        if not isinstance(u, dict):
            return usage_dict()
        return usage_dict(u.get("input_tokens"), u.get("output_tokens"), u.get("thinking_tokens"))

    def error(self):
        if self._cli_timeout:
            return "cli_timeout %s (partial output, not an answer)" % self._cli_timeout
        r = self._result
        if self._quota_seen() and not (r is not None and (self._success() or self._finished_answer())):
            return "quota_exhausted agy 429 Individual quota reached%s" % (
                " (resets in %s)" % self._resets_in if self._resets_in else "")
        if r is None or self._success() or self._finished_answer():
            return None
        return "agy_%s %s" % (str(r.get("status") or "no_status").lower(), _scrub(r.get("error"), 120))

    def note(self):
        if self._cli_timeout:
            return None
        if not self._finished_answer():
            return None
        if "output token limit" in str(self._result.get("error") or ""):
            return "step_limit"
        return "status_%s_with_answer" % str(self._result.get("status") or "unknown").lower()

    def result_event(self):
        return self._result


class AgyAdapter(Adapter):
    name = "agy"
    family = "google"
    # `agy --help` (1.4.5): no tool allowlist and no read-only mode. Its argv already carries
    # --dangerously-skip-permissions ("auto-approve all tool permission requests"), so --add-dir on a repo would hand
    # it write access too. --mode plan is not documented as read-only. Stays text only until agy gains one.
    grounded_capable = False
    grounded_reason = "agy CLI has no read-only tool allowlist (agy --help)"

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        pc = _pcfg(cfg, "agy")
        with open(prompt_path, "rb") as f:
            prompt = f.read().decode("utf-8", "replace")
        argv = [pc.get("bin", "agy"), "--sandbox", "--dangerously-skip-permissions"]
        if limits.ceiling_s is not None:
            # a seat with no ceiling (row 35) gets no --print-timeout: omitted means unlimited in agy 1.4.4 (row 23, D28)
            argv += ["--print-timeout", "%ds" % int(limits.ceiling_s + 60)]
        argv += ["--model", seat["model"], "--output-format", "stream-json"]
        argv += agy_signal_args(seat, pc, cfg, launch_opts, workdir)
        argv += [str(a) for a in (pc.get("extra_args") or [])]
        argv += [str(a) for a in (seat.get("extra_args") or [])]
        if len((AGY_INLINE_DIRECTIVE + prompt).encode("utf-8")) > AGY_ARG_MAX_BYTES:
            # hand agy a copy inside its own workdir, so --add-dir exposes only this seat
            copy = os.path.join(workdir, "prompt.md")
            _write_private(copy, prompt)
            argv += ["--add-dir", workdir, "--print",
                     "%sRead the file '%s' and follow the instructions in it as your task prompt. "
                     "Do not summarize the file; execute it." % (AGY_INLINE_DIRECTIVE_FILE, copy)]
        else:
            argv += ["--print", AGY_INLINE_DIRECTIVE + prompt]
        return Launch(argv=argv, env=_tag_env(_clean_env(), launch_opts), cwd=workdir, stdin_path=None)

    def extractor(self, seat=None):
        return AgyExtractor()

    def salvage_seat(self, seat, cfg, max_tokens):
        """agy has no output cap and rejects --effort for Gemini 3.8 labels: the label tier is the only
        lever, so the salvage call swaps it for the lowest (`salvage_model_tier`, default Low)."""
        s = salvage_base(seat, max_tokens)
        tier = _pcfg(cfg, "agy").get("salvage_model_tier", "Low")
        s["model"] = re.sub(r"\((?:%s)\)\s*$" % "|".join(AGY_TIERS), "(%s)" % tier, str(seat.get("model", "")))
        return s

    def model_matches(self, pin, reported):
        if reported.startswith(pin):
            return True
        for ident, label in _AGY_ROWS:
            if pin in (ident, label):
                return reported in (ident, label)
        return False

    def free_checks(self, seats, cfg):
        pc = _pcfg(cfg, "agy")
        binp = pc.get("bin", "agy")
        pins = (cfg or {}).get("pins") or {}
        mine = _provider_seats(seats, "agy")
        out = []
        missing = _bin_missing(binp)
        rc, text = (None, "") if missing else _capture([binp, "--version"])
        have = _version(text)
        seen = pins.get("agy_seen")
        if missing:
            out.append(Check("agy", "free", "version", "FAIL", _missing_detail("agy")))
        elif rc != 0 or have is None:
            out.append(Check("agy", "free", "version", "FAIL", "agy --version failed"))
        elif seen and have != _version(seen):
            out.append(Check("agy", "free", "version", "WARN",
                             "%s != agy_seen %s (agy updates only on purpose; AGY_CLI_DISABLE_AUTO_UPDATE is set)" % (_vstr(have), seen)))
        else:
            out.append(Check("agy", "free", "version", "PASS", _vstr(have)))
        rc, text = (None, "") if missing else _capture([binp, "models"], timeout=30)
        rows = parse_agy_models(text)
        _AGY_ROWS[:] = rows
        if missing:
            out.append(Check("agy", "free", "auth", "FAIL", "agy models: skipped, agy CLI %s" % NOT_FOUND))
        elif not rows:
            out.append(Check("agy", "free", "auth", "FAIL",
                             "agy models returned no catalog (not logged in, or offline)"))
        else:
            out.append(Check("agy", "free", "auth", "PASS",
                             "catalog fetched, %d models (agy has no auth status; this is its proof)" % len(rows)))
        for s in mine:
            pin = str(s.get("model", ""))
            row = next((r for r in rows if pin in r), None)
            if not rows:
                out.append(Check("agy", "free", "model:%s" % s.get("id", "?"), "SKIP", "no catalog"))
            elif row is None:
                out.append(Check("agy", "free", "model:%s" % s.get("id", "?"), "FAIL",
                                 "%r is not an id or label in the agy catalog" % pin))
            elif not _AGY_GEMINI.match(pin):
                out.append(Check("agy", "free", "model:%s" % s.get("id", "?"), "FAIL",
                                 "%r is in the catalog but is not a Gemini model" % pin))
            else:
                out.append(Check("agy", "free", "model:%s" % s.get("id", "?"), "PASS",
                                 "%s (%s)" % (row[1], row[0])))
        if rows and ((cfg or {}).get("salvage") or {}).get("enabled"):
            for s in mine:
                lab = self.salvage_seat(s, cfg, 8000).get("model")
                ok = any(lab in r for r in rows)
                out.append(Check("agy", "free", "salvage_model:%s" % s.get("id", "?"), "PASS" if ok else "WARN",
                                 "salvage call uses %s%s" % (lab, "" if ok else " (not in the agy catalog)")))
        top = _max_ceiling(cfg, "agy")
        out.append(Check("agy", "free", "inner_caps", "PASS",
                         "no --print-timeout (no ceiling, row 35; omitted = unlimited in agy 1.4.4, row 23, D28)"
                         if top is None else
                         "--print-timeout = ceiling+60s (per launch, up to %ds) >= ceiling" % int(top + 60)))
        out.append(Check("agy", "free", "inner_retries", "WARN",
                         "retries unknown (accepted limit): agy retries a step that hits its own output "
                         "limit up to 3 times inside the CLI (row 4); no knob found"))
        out.append(Check("agy", "free", "output_budget", "WARN",
                         "accepted limit: no output-token cap in agy %s; --effort is rejected for Gemini 3.8 "
                         "labels, so the lever is the label tier (seat model). Observed step limit 65532 tokens"
                         % (_vstr(have) if have else "?")))
        return out


# ---------------------------------------------------------------- qwen
class QwenExtractor(Extractor):
    """qwen -o stream-json --include-partial-messages (claude-shaped events).

    Errors arrive as a `result` event with `subtype: error_during_execution`, `is_error`
    and `error.message`. A kill during reasoning has only thinking deltas; the partial
    then returns them under a label (the owner's complaint: two 600 s kills left nothing).

    Output cap: qwen gives NO stop signal when max_tokens is hit (verified at max_tokens=300:
    result subtype success, stop_reason null, usage.output_tokens 302, text cut short). So the
    extractor is told the cap and treats output_tokens >= 97% of it as a capped answer: kept,
    marked TRUNCATED."""

    def __init__(self, cap=None):
        self._buf = bytearray()
        self._text = []
        self._thinking = []
        self._model = None
        self._result = None
        self._capped = False
        self._cap = int(cap) if cap else None

    def feed(self, chunk):
        for ev in _json_lines(self._buf, chunk):
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                self._model = ev.get("model") or self._model
            elif t == "stream_event" and not ev.get("parent_tool_use_id"):
                inner = ev.get("event") or {}
                d = inner.get("delta") or {}
                if inner.get("type") == "content_block_delta":
                    if d.get("type") == "text_delta":
                        self._text.append(str(d.get("text") or ""))
                    elif d.get("type") == "thinking_delta":
                        self._thinking.append(str(d.get("thinking") or ""))
                elif inner.get("type") == "message_delta" and d.get("stop_reason") in ("max_tokens", "length"):
                    self._capped = True
            elif t == "assistant" and (ev.get("message") or {}).get("stop_reason") in ("max_tokens", "length"):
                self._capped = True
            elif t == "result":
                self._result = ev
                out = (ev.get("usage") or {}).get("output_tokens")
                if self._cap and isinstance(out, int) and out >= 0.97 * self._cap:
                    self._capped = True

    def partial_text(self):
        text = "".join(self._text)
        if text.strip():
            return text
        think = "".join(self._thinking)
        return THINKING_NOTE + think if think.strip() else ""

    def final_text(self):
        r = self._result
        if r is None or r.get("is_error"):
            return None
        text = r.get("result") if isinstance(r.get("result"), str) and r["result"].strip() else "".join(self._text)
        if not text.strip():
            return None
        return text.rstrip() + TRUNC_NOTE if self._capped else text

    def model_reported(self):
        return self._model

    def usage(self):
        """result `usage.{input_tokens,output_tokens}`; qwen reports no reasoning split and no cost."""
        u = (self._result or {}).get("usage")
        if not isinstance(u, dict):
            return usage_dict()
        return usage_dict(u.get("input_tokens"), u.get("output_tokens"))

    def error(self):
        r = self._result
        if r is None or not r.get("is_error"):
            return None
        msg = (r.get("error") or {}).get("message") if isinstance(r.get("error"), dict) else r.get("error")
        return ("qwen_%s %s" % (r.get("subtype") or "error", _scrub(msg or r.get("result"), 120))).strip()

    def result_event(self):
        return self._result


def _qwen_settings_path(pc):
    return os.path.expanduser(pc.get("settings_path") or "~/.qwen/settings.json")


def _qwen_load(pc):
    try:
        with open(_qwen_settings_path(pc), "rb") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _qwen_entry(settings, model):
    for e in (((settings or {}).get("modelProviders") or {}).get("openai") or []):
        if isinstance(e, dict) and e.get("id") == model:
            return e
    return None


def qwen_guards_ms(limits):
    """(stream idle ms, stream max lifetime ms, request timeout ms) for one launch (R2-W5).
    Qwen's own defaults are idle 240 s and lifetime 900 s; both sit above the runner's."""
    if limits.ceiling_s is None:
        raise _no_ceiling("qwen_guards_ms")
    idle = int((float(limits.idle_s) + 60) * 1000)
    life = int((float(limits.ceiling_s) + 60) * 1000)
    return idle, life, life


def qwen_overlay(entry, limits, effort, cap, reasoning_max=None, reasoning_off=False):
    """System-settings overlay for one launch. It carries NO `env` block, so no key is ever
    copied: the key still comes from the owner's settings (or the shell) by `envKey`.
    The system modelProviders array REPLACES the user one, so the full seat entry is kept.
    Auto-memory is switched off: it added a recall call and a background extractor call per
    seat (3 requests, 25.4k input tokens instead of 1 and 13.6k) and would store council
    prompts in qwen's memory."""
    idle, _life, timeout = qwen_guards_ms(limits)
    gc = json.loads(json.dumps(entry.get("generationConfig") or {}))
    gc["timeout"] = max(int(gc.get("timeout") or 0), timeout)
    gc["maxRetries"] = min(int(gc.get("maxRetries", 1)), 1)
    gc["streamIdleTimeoutMs"] = idle
    gc.pop("streamMaxLifetimeMs", None)            # the env var governs the lifetime
    gc.pop("reasoning", None)                      # not forwarded for this endpoint (verified)
    sp = dict(gc.get("samplingParams") or {})
    if cap:
        sp["max_tokens"] = int(cap)
    gc["samplingParams"] = sp
    eb = dict(gc.get("extra_body") or {})
    if reasoning_off:
        eb["reasoning"] = {"enabled": False}        # answer-salvage call: reasoning off
    elif reasoning_max:
        # OpenRouter takes reasoning.max_tokens OR reasoning.effort, never both: the token form wins
        eb["reasoning"] = {"max_tokens": int(reasoning_max)}
    elif effort:
        eb["reasoning"] = {"effort": effort}
    if eb:
        gc["extra_body"] = eb
    new = dict(entry)
    new["generationConfig"] = gc
    return {"memory": {"enableManagedAutoMemory": False, "enableManagedAutoDream": False},
            "modelProviders": {"openai": [new]}}


class QwenAdapter(Adapter):
    name = "qwen"
    family = "alibaba"
    UNSET = ("QWEN_API_KEY", "DASHSCOPE_API_KEY")

    def __init__(self):
        self._caps = {}

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        pc = _pcfg(cfg, "qwen")
        settings = _qwen_load(pc)
        entry = _qwen_entry(settings, seat["model"])
        if entry is None:
            raise KeyError("qwen model not in modelProviders.openai")
        effort, cap = budget(seat, pc)
        canary = bool((cfg or {}).get("_canary") and pc.get("canary_reasoning_effort"))
        effort = _canary_effort(seat, pc, cfg, effort)
        rmax = reasoning_cap(seat, pc, cfg, cap)    # raises ValueError on a bad ratio
        if canary:
            rmax = None                             # canary keeps its own cheap effort (a request carries one or the other)
        self._caps[seat.get("id")] = cap            # the extractor is created right after build()
        overlay = os.path.join(workdir, "qwen-system-settings.json")
        roff = bool(seat.get("_salvage") and salvage_reasoning(seat, pc) == "off")
        if seat.get("_salvage") and not roff:
            effort = salvage_reasoning(seat, pc)
        _write_private(overlay, json.dumps(qwen_overlay(entry, limits, effort, cap, rmax, roff)))
        argv = [pc.get("bin", "qwen"), "-o", "stream-json", "--include-partial-messages",
                "--approval-mode", "plan", "-m", seat["model"]]
        argv += [str(a) for a in (pc.get("extra_args") or [])]
        argv += [str(a) for a in (seat.get("extra_args") or [])]
        idle, life, _ = qwen_guards_ms(limits)
        env = _clean_env(self.UNSET)
        env.update({"QWEN_CODE_SYSTEM_SETTINGS_PATH": overlay,
                    "QWEN_STREAM_IDLE_TIMEOUT_MS": str(idle),
                    "QWEN_STREAM_MAX_LIFETIME_MS": str(life)})
        return Launch(argv=argv, env=_tag_env(env, launch_opts), cwd=workdir, stdin_path=prompt_path)

    def extractor(self, seat=None):
        return QwenExtractor(self._caps.get((seat or {}).get("id")))

    def salvage_seat(self, seat, cfg, max_tokens):
        s = salvage_base(seat, max_tokens)
        s["reasoning_effort"] = "low"
        return s

    def _key(self, entry, settings):
        name = entry.get("envKey")
        if not name:
            return None
        val = ((settings or {}).get("env") or {}).get(name) or os.environ.get(name)
        return val or None

    def free_checks(self, seats, cfg):
        pc = _pcfg(cfg, "qwen")
        pins = (cfg or {}).get("pins") or {}
        mine = _provider_seats(seats, "qwen")
        out = []
        missing = _bin_missing(pc.get("bin", "qwen"))
        rc, text = (None, "") if missing else _capture([pc.get("bin", "qwen"), "--version"], env=_clean_env(self.UNSET))
        have, pin = _version(text), _version(pins.get("qwen", ""))
        if missing:
            out.append(Check("qwen", "free", "version", "FAIL", _missing_detail("qwen")))
        elif rc != 0 or have is None:
            out.append(Check("qwen", "free", "version", "FAIL", "qwen --version failed"))
        elif pin and have != pin:
            out.append(Check("qwen", "free", "version", "FAIL", "%s != pinned %s" % (_vstr(have), _vstr(pin))))
        else:
            out.append(Check("qwen", "free", "version", "PASS", _vstr(have)))
        path = _qwen_settings_path(pc)
        settings = _qwen_load(pc)
        if settings is None:
            out.append(Check("qwen", "free", "settings", "FAIL", "settings file missing or not JSON: " + path))
            return out
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = None
        out.append(Check("qwen", "free", "settings_mode", "PASS" if mode == 0o600 else "FAIL",
                         "mode %s" % (oct(mode)[2:] if mode is not None else "?")))
        sel = (((settings.get("security") or {}).get("auth") or {}).get("selectedType"))
        out.append(Check("qwen", "free", "selected_type", "PASS" if sel == "openai" else "FAIL",
                         "selectedType=%s" % sel))
        auto = (settings.get("general") or {}).get("enableAutoUpdate")
        out.append(Check("qwen", "free", "auto_update", "PASS" if auto is False else "WARN",
                         "general.enableAutoUpdate=false" if auto is False else "general.enableAutoUpdate is not false"))
        top_q = _max_ceiling(cfg, "qwen")
        need_ms = None if top_q is None else top_q * 1000      # None: no ceiling (row 35), FAILed below
        for s in mine:
            sid, model = s.get("id", "?"), s.get("model")
            sfx = ":%s" % sid if len(mine) > 1 else ""
            entry = _qwen_entry(settings, model)
            if entry is None:
                out.append(Check("qwen", "free", "model_entry" + sfx, "FAIL",
                                 "%r is not in modelProviders.openai[].id" % model))
                continue
            out.append(Check("qwen", "free", "model_entry" + sfx, "PASS", str(model)))
            key = self._key(entry, settings)
            out.append(Check("qwen", "free", "env_key" + sfx, "PASS" if key else "FAIL",
                             "%s present" % entry.get("envKey") if key else "%s is not set" % entry.get("envKey")))
            base = entry.get("baseUrl")
            if key:
                st, _ = openrouter_get("/key", cfg, key=key, base_url=base)
                out.append(Check("qwen", "free", "key" + sfx, "PASS" if st == 200 else "FAIL",
                                 "GET /key -> %s" % st))
                st, data = openrouter_get("/models", cfg, key=key, base_url=base)
                ids = {m.get("id") for m in (data or {}).get("data", []) if isinstance(m, dict)}
                ok = st == 200 and model in ids
                out.append(Check("qwen", "free", "models" + sfx, "PASS" if ok else "FAIL",
                                 "%s in /models" % model if ok else
                                 ("GET /models -> %s" % st if st != 200 else "%s not in /models" % model)))
                if ok:
                    entry_m = next((m for m in data["data"] if isinstance(m, dict) and m.get("id") == model), None)
                    try:
                        out.append(reasoning_cap_check("qwen", s, pc, entry_m))
                    except ValueError as e:
                        out.append(Check("qwen", "free", "reasoning_cap" + sfx, "FAIL", str(e)))
                    out.append(salvage_reasoning_check("qwen", s, pc, entry_m))
            gc = entry.get("generationConfig") or {}
            probs = []
            # The per-launch overlay REPLACES this entry and sets timeout = max(file value, ceiling + 60 s)
            # (qwen_overlay), so a shorter value in the owner's file is raised for every seat and never binds.
            if need_ms is None:
                out.append(Check("qwen", "free", "inner_caps" + sfx, "FAIL",
                                 "no ceiling (ceiling_s = 0): qwen's stream lifetime and request timeout need one"))
                continue
            raised = int(gc.get("timeout") or 0) < need_ms
            if "maxRetries" not in gc:
                probs.append("generationConfig.maxRetries not set (qwen default 3)")
            elif int(gc["maxRetries"]) > 1:
                probs.append("generationConfig.maxRetries %s > 1" % gc["maxRetries"])
            idle, life, _ = qwen_guards_ms(Limits(mode="stream", idle_s=_idle(cfg, "qwen"),
                                                   ceiling_s=_max_ceiling(cfg, "qwen"), deadline_s=None))
            out.append(Check("qwen", "free", "inner_caps" + sfx, "FAIL" if probs else "PASS",
                             "; ".join(probs) or "file timeout %sms%s, maxRetries %s; per launch: stream idle "
                             "%dms, max lifetime %dms (qwen defaults 240000/900000), request timeout >= %dms, overlay set by build()"
                             % (gc.get("timeout"), " (raised per launch)" if raised else "", gc.get("maxRetries"),
                                idle, life, need_ms + 60000)))
        if not mine:
            out.append(Check("qwen", "free", "inner_caps", "PASS", "no qwen seats"))
        probs = _budget_problems("qwen", pc, OR_EFFORTS, True, cfg, True, seats)
        a, ac = budget({"id": "x"}, pc)
        c_, cc = budget({"id": "chair"}, pc)
        out.append(Check("qwen", "free", "output_budget", "FAIL" if probs else "PASS",
                         "; ".join(probs) or "advisor %s/%s tok, chair %s/%s tok (generationConfig "
                         "samplingParams.max_tokens; %s; per-launch overlay)"
                         % (a, ac, c_, cc, _form_detail(pc, cfg))))
        return out


def _idle(cfg, provider):
    t = (cfg or {}).get("timeouts") or {}
    d = dict(t.get("default") or {})
    d.update(t.get(provider) or {})
    return float(d.get("idle_s", 240))


# ---------------------------------------------------------------- openrouter
OPENROUTER_SEAT = os.path.join(HERE, "openrouter_seat.py")
OR_DEFAULT_BASE = "https://openrouter.ai/api/v1"
_OR_CACHE: dict = {}


def openrouter_get(path, cfg, key=None, base_url=None, timeout=20):
    """GET <base>/<path> with a bearer key read in-process. -> (http status | None, parsed JSON | None).

    Cached per process by (base, path, key fingerprint). Never raises; the key is never
    returned, logged or put in an exception. Tests replace this function (W4)."""
    import hashlib
    import urllib.error
    import urllib.request
    pc = _pcfg(cfg, "openrouter")
    base = (base_url or pc.get("base_url") or OR_DEFAULT_BASE).rstrip("/")
    key = key if key is not None else os.environ.get(pc.get("key_env", "OPENROUTER_API_KEY"), "")
    ck = (base, path, hashlib.sha256((key or "").encode()).hexdigest()[:12])
    if ck in _OR_CACHE:
        return _OR_CACHE[ck]
    req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + (key or "")})
    status, data = None, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        status = e.code
    except (OSError, ValueError):
        status = None
    _OR_CACHE[ck] = (status, data)
    return status, data


def cached_endpoints(slug):
    """The `/models/<slug>/endpoints` list this process already fetched (the preflight's routing check and drift
    collect fill `_OR_CACHE`; the runner is forked from that process, council.py `launch`). Cache only: never a
    request. -> list of endpoint dicts | None."""
    path = "/models/%s/endpoints" % slug
    for ck, (st, data) in list(_OR_CACHE.items()):
        if ck[1] != path or st != 200 or not isinstance(data, dict):
            continue
        eps = (data.get("data") or {}).get("endpoints") if isinstance(data.get("data"), dict) else None
        if isinstance(eps, list):
            return [e for e in eps if isinstance(e, dict)]
    return None


def endpoint_quantization(slug, serving):
    """P26: the quantization OpenRouter lists for the endpoint that served a call, from the cached endpoints
    snapshot (no request). The stream's provider name is matched case-insensitively against `provider_name` and the
    tag. -> str | None: None when nothing is cached, no endpoint matches, or one provider's endpoints disagree;
    the listed value is kept as it is (`unknown` included), never a guess."""
    if not (isinstance(slug, str) and slug and isinstance(serving, str) and serving):
        return None
    eps = cached_endpoints(slug)
    if not eps:
        return None
    want = serving.strip().lower()
    vals = {e.get("quantization") for e in eps if want in _ep_names(e)}
    if len(vals) != 1:
        return None
    v = vals.pop()
    return v if isinstance(v, str) and v else None


GEN_EVIDENCE_KEYS = ("provider_name", "model", "finish_reason", "native_finish_reason", "generation_time",
                     "latency", "tokens_prompt", "tokens_completion", "native_tokens_prompt",
                     "native_tokens_completion", "native_tokens_reasoning", "total_cost", "created_at")


def generation_evidence(data):
    """Whitelisted scalar fields of a `GET /generation` body (`{"data": {...}}`). Evidence only."""
    rec = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(rec, dict):
        return {}
    return {k: (_scrub(v, 80) if isinstance(v, str) else v) for k, v in rec.items()
            if k in GEN_EVIDENCE_KEYS and isinstance(v, (str, int, float, bool, type(None)))}


def fetch_generation(gen_id, cfg, tries=3, wait_s=5.0, sleep=time.sleep, key=None, base_url=None):
    """Post-hoc, free `GET /generation?id=` for a stream that errored or ended early (37-openrouter-scout
    section 4): evidence only (serving provider, finish, generation time, native tokens), never a kill or
    a retry input. Retries on 404 (the record may not exist yet), `tries` times, `wait_s` apart.
    `openrouter_get` caches per process, even a 404, so each non-200 answer is dropped from the cache
    before the next try. -> {"http": status, **evidence}; {"http": None} on a network failure."""
    from urllib.parse import quote
    path = "/generation?id=" + quote(str(gen_id or ""), safe="")
    st = None
    for n in range(max(1, int(tries))):
        st, data = openrouter_get(path, cfg, key=key, base_url=base_url)
        if st == 200:
            return dict({"http": 200}, **generation_evidence(data))
        for ck in [k for k in _OR_CACHE if k[1] == path]:
            _OR_CACHE.pop(ck, None)
        if st != 404 or n >= tries - 1:
            break
        sleep(wait_s)
    return {"http": st}


class OpenRouterExtractor(Extractor):
    """SSE from openrouter_seat.py: `data: {chat.completion.chunk}` lines, `: OPENROUTER PROCESSING`
    comments and `data: [DONE]`. Reasoning arrives in delta.reasoning, the answer in delta.content.
    Every chunk carries `model`. finish_reason `length` is a capped answer (kept, marked
    TRUNCATED); with no answer text at all it is an error, because the reasoning used the cap."""

    def __init__(self, clock=time.time):
        self._buf = bytearray()
        self._content = []
        self._reasoning = []
        self._model = None
        self._finish = None
        self._done = False
        self._err = None
        self._usage = None
        self._clock = clock
        self._gen_id = None
        self._serving = None
        self._stderr = b""             # tail of the seat child's stderr: codes and exception names only, never a body or key
        self.data_bytes = 0            # bytes of `data:` lines (content progress)
        self.comment_bytes = 0         # bytes of `: ...` comment lines (labelled keepalive, D44)
        self.keepalives = 0
        self.last_data_at = None       # clock() at the last `data:` line
        self.last_keepalive_at = None

    def stream_stats(self):
        """Counters for SeatLiveness (wall clock). `data:` lines are progress; comments are keepalives."""
        return {"last_data_at": self.last_data_at, "last_keepalive_at": self.last_keepalive_at,
                "keepalives": self.keepalives, "data_bytes": self.data_bytes,
                "comment_bytes": self.comment_bytes}

    def generation_id(self):
        """The stream chunks' `id` (gen-...), kept for a post-hoc `GET /generation?id=`."""
        return self._gen_id

    def serving_provider(self):
        return self._serving

    def feed(self, chunk):
        self._buf += chunk
        while True:
            i = self._buf.find(b"\n")
            if i < 0:
                return
            raw = bytes(self._buf[:i + 1])
            line = bytes(self._buf[:i]).strip()
            del self._buf[:i + 1]
            if line.startswith(b":"):
                self.keepalives += 1
                self.comment_bytes += len(raw)
                self.last_keepalive_at = self._clock()
                continue
            if not line.startswith(b"data:"):
                continue
            self.data_bytes += len(raw)
            self.last_data_at = self._clock()
            payload = line[5:].strip()
            if payload == b"[DONE]":
                self._done = True
                continue
            if self._serving is None:
                from . import drift          # lazy: drift imports providers
                self._serving = drift.serving_provider(line)
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("model"):
                self._model = obj["model"]
            if self._gen_id is None and isinstance(obj.get("id"), str) and obj["id"]:
                self._gen_id = obj["id"]
            if isinstance(obj.get("usage"), dict):
                self._usage = obj["usage"]
            if obj.get("error"):
                e = obj["error"]
                msg = e.get("message") if isinstance(e, dict) else e
                code = e.get("code") if isinstance(e, dict) else ""
                self._err = "openrouter_error %s %s" % (code or "", _scrub(msg, 100))
            for ch in obj.get("choices") or []:
                if not isinstance(ch, dict):
                    continue
                d = ch.get("delta") or {}
                if d.get("content"):
                    self._content.append(str(d["content"]))
                r = d.get("reasoning") or d.get("reasoning_content")
                if r:
                    self._reasoning.append(str(r))
                if ch.get("finish_reason"):
                    self._finish = ch["finish_reason"]
                    if self._finish == "error" and not self._err:
                        self._err = "openrouter_error finish_reason=error"

    def feed_stderr(self, chunk):
        self._stderr = (self._stderr + bytes(chunk))[-4096:]

    def _stderr_lines(self):
        out = []
        for ln in self._stderr.decode("utf-8", "replace").splitlines():
            ln = re.sub(r"[^A-Za-z0-9 _.:/=-]", "", ln).strip()[:80]
            if ln.startswith("openrouter "):
                out.append(ln[len("openrouter "):])
        return out

    def stderr_text(self):
        """The child's stderr as kept on disk: only its `openrouter ...` lines, characters filtered, one per line."""
        return "".join("openrouter %s\n" % ln for ln in self._stderr_lines())

    def retries(self):
        """Reasons of the seat child's pre-byte retries (`openrouter retry N: <reason>` lines), in order."""
        return [ln.split(":", 1)[1].strip() for ln in self._stderr_lines() if ln.startswith("retry ") and ":" in ln]

    def stderr_reason(self):
        """Why the child failed, from its last non-retry stderr line (`http 503`, `URLError`, `read timeout`...), with
        the number of retries it spent. None when the child wrote none."""
        last = [ln for ln in self._stderr_lines() if not ln.startswith("retry ")]
        if not last:
            return None
        r = self.retries()
        return "openrouter %s%s" % (last[-1], " (after %d retr%s)" % (len(r), "y" if len(r) == 1 else "ies") if r else "")

    def note(self):
        """An ok call that needed the child's retry: the first error it hit (a canary turns this into a WARN)."""
        r = self.retries()
        return "retried %d time%s after %s" % (len(r), "" if len(r) == 1 else "s", r[0]) if r else None

    def partial_text(self):
        text = "".join(self._content)
        if text.strip():
            return text
        think = "".join(self._reasoning)
        return THINKING_NOTE + think if think.strip() else ""

    def final_text(self):
        if not (self._finish or self._done):
            return None
        text = "".join(self._content)
        if not text.strip():
            return None
        return text.rstrip() + TRUNC_NOTE if self._finish == "length" else text

    def model_reported(self):
        return self._model

    def usage(self):
        """The stream's `usage` chunk: prompt_tokens, completion_tokens (reasoning inside),
        completion_tokens_details.reasoning_tokens, cost (USD). A cut stream may have none -> all None."""
        u = self._usage
        if not u:
            return usage_dict()
        return usage_dict(u.get("prompt_tokens"), u.get("completion_tokens"),
                          _dig(u, "completion_tokens_details", "reasoning_tokens"), u.get("cost"))

    def error(self):
        if self._err:
            return self._err
        if (self._finish or self._done) and not "".join(self._content).strip():
            return ("openrouter_no_answer finish=%s%s" % (self._finish, " (reasoning used the output cap)"
                                                          if self._finish == "length" else ""))
        return None


def or_socket_timeout(limits):
    """Per-read socket timeout of the seat child (R2-W5): stream mode idle_s + 30 s, buffered mode
    the call's deadline (else its ceiling), so a healthy call is never ended by a read timeout."""
    if limits.mode == "stream":
        return float(limits.idle_s) + 30
    if limits.deadline_s is None and limits.ceiling_s is None:
        raise _no_ceiling("or_socket_timeout")
    return float(limits.deadline_s or limits.ceiling_s) + 30


FALLBACK_MAX_TOKENS = 131072                   # the rerouted / stand-in call's max_tokens when no key says otherwise
_STANDIN_DROP = ("provider_prefs", "reasoning_form", "ignores_reasoning_cap", "salvage_reasoning",
                 "canary_reasoning_effort", "reasoning_max_tokens", "fallback_model")


def fallback_max_tokens(seat, pc):
    """Cap of the rerouted and stand-in calls: the seat's `fallback_max_output_tokens`, then the provider's, then
    131072 (at least 2 endpoints of every roster model take it); never above the seat's own cap."""
    v = (seat or {}).get("fallback_max_output_tokens") or (pc or {}).get("fallback_max_output_tokens") or FALLBACK_MAX_TOKENS
    own = (seat or {}).get("max_output_tokens") or (pc or {}).get("max_output_tokens")
    return min(int(v), int(own)) if own else int(v)


def standin_effort(seat, pc):
    """Reasoning effort of the stand-in call: the seat's `fallback_reasoning_effort`, else the seat's own effort."""
    return (seat or {}).get("fallback_reasoning_effort") or budget(seat or {}, pc or {})[0]


def reroute_seat(seat, pc):
    """Fallback step 1: the same model and prompt at full effort, the seat's provider `order` dropped and max_tokens
    capped to `fallback_max_tokens`, so any endpoint that supports the parameters can serve it."""
    r = dict(seat)
    r.update(_full_call=True, _reroute=True, max_output_tokens=fallback_max_tokens(seat, pc))
    return r


def standin_seat(seat, pc):
    """Fallback step 2: the seat's `fallback_model` at full effort with the same prompt (None without the key).
    The seat's model-specific reasoning keys do not carry over; the stand-in sends `standin_effort`."""
    model = (seat or {}).get("fallback_model")
    if not isinstance(model, str) or not model.strip():
        return None
    st = {k: v for k, v in seat.items() if k not in _STANDIN_DROP}
    st.update(model=model.strip(), _full_call=True, _reroute=True, _standin=True,
              max_output_tokens=fallback_max_tokens(seat, pc))
    eff = standin_effort(seat, pc)
    if eff:
        st["reasoning_effort"] = eff
    else:
        st.pop("reasoning_effort", None)
    return st


def standin_start_seat(seat, pc):
    """The stand-in as the seat's FIRST call (preflight found its model missing or dead): the dict the chain's last step
    uses, without the chain-call marks (it is an original call; `_orig_model` names the model it replaced). None
    without a `fallback_model`."""
    st = standin_seat(seat, pc)
    if st is None:
        return None
    for k in ("_full_call", "_reroute", "_standin"):
        st.pop(k, None)
    st["_orig_model"] = seat.get("model")
    return st


class OpenRouterAdapter(Adapter):
    name = "openrouter"
    family = "openrouter"
    fast_fail_fallback = True

    def fallback_chain(self, seat, cfg):
        pc = _pcfg(cfg, "openrouter")
        chain = [reroute_seat(seat, pc)]
        st = standin_seat(seat, pc)
        if st is not None:
            chain.append(st)
        return chain

    def standin_start(self, seat, cfg):
        return standin_start_seat(seat, _pcfg(cfg, "openrouter"))

    def build(self, seat, prompt_path, workdir, limits, cfg, launch_opts=None):
        pc = _pcfg(cfg, "openrouter")
        effort, cap = budget(seat, pc)
        canary = bool((cfg or {}).get("_canary") and pc.get("canary_reasoning_effort"))
        effort = _canary_effort(seat, pc, cfg, effort)
        rmax = reasoning_cap(seat, pc, cfg, cap)    # raises ValueError on a bad ratio
        if canary:
            rmax = None                             # canary keeps its own cheap effort (one field, never both)
        argv = [pc.get("python", sys.executable or "/usr/bin/python3"), "-Es", OPENROUTER_SEAT,
                "--model", seat["model"], "--base-url", pc.get("base_url", OR_DEFAULT_BASE),
                "--socket-timeout", "%g" % or_socket_timeout(limits),
                "--key-env", pc.get("key_env", "OPENROUTER_API_KEY")]
        if cap:
            argv += ["--max-tokens", str(int(cap))]
        if seat.get("_salvage"):
            mode_ = salvage_reasoning(seat, pc)              # answer-salvage call: reasoning off, or its lowest effort
            argv += ["--reasoning-off"] if mode_ == "off" else ["--reasoning-effort", mode_]
        elif rmax:
            argv += ["--reasoning-max-tokens", str(rmax)]       # OpenRouter: max_tokens OR effort, never both
        elif effort:
            argv += ["--reasoning-effort", str(effort)]
        prefs = routing_prefs(cfg, seat)                    # D43: `provider` routing block; absent table = argv unchanged
        if prefs:
            argv += ["--provider-json", json.dumps(prefs, sort_keys=True, separators=(",", ":"))]
        argv += [str(a) for a in (pc.get("extra_args") or [])]
        argv += [str(a) for a in (seat.get("extra_args") or [])]
        return Launch(argv=argv, env=_tag_env(_clean_env(), launch_opts), cwd=workdir, stdin_path=prompt_path)

    def extractor(self, seat=None):
        return OpenRouterExtractor()

    def salvage_seat(self, seat, cfg, max_tokens):
        s = salvage_base(seat, max_tokens)
        s["reasoning_effort"] = "low"
        return s

    def _standin_check(self, s, pc, by_id):
        """The `fallback_model:<seat>` row: the stand-in is in /models, sends an effort it supports, and fits the cap.
        [] without a `fallback_model`."""
        st = standin_seat(s, pc)
        if st is None:
            return []
        name = "fallback_model:%s" % s.get("id", "?")
        entry = by_id.get(st["model"])
        if not entry:
            return [Check("openrouter", "free", name, "FAIL", "%s not in /models" % st["model"])]
        notes = []
        r = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
        sup = r.get("supported_efforts") or []
        eff = st.get("reasoning_effort")
        if sup and eff and eff not in sup:
            notes.append("effort %s not in supported_efforts %s" % (eff, "/".join(sup)))
        if "reasoning" not in (entry.get("supported_parameters") or []):
            notes.append("no `reasoning` parameter")
        top = ((entry.get("top_provider") or {}).get("max_completion_tokens"))
        if isinstance(top, (int, float)) and st["max_output_tokens"] > top:
            notes.append("max_tokens %s above the model maximum %s" % (st["max_output_tokens"], top))
        return [Check("openrouter", "free", name, "WARN" if notes else "PASS",
                      "; ".join(notes) or "%s in /models; sends effort %s at max_tokens %s" %
                      (st["model"], eff or "(provider default)", st["max_output_tokens"]))]

    def _fallback_checks(self, s, pc, cfg, by_id):
        """Free checks for the fast-fail fallback chain of one roster seat (fallback-chain row 1): the key types, the
        rerouted call (>= 2 distinct providers take its parameters at the fallback cap once `order` is dropped) and the
        stand-in model (in /models, the effort it sends is supported, the cap fits). No paid call."""
        sid, slug = s.get("id", "?"), s.get("model")
        out = []
        bad = []
        v = s.get("fallback_max_output_tokens")
        if v is not None and not _pos_int(v):
            bad.append("fallback_max_output_tokens %r must be a positive integer" % (v,))
        fm = s.get("fallback_model")
        if fm is not None and not (isinstance(fm, str) and fm.strip()):
            bad.append("fallback_model %r must be an OpenRouter model id" % (fm,))
        if bad:
            return [Check("openrouter", "free", "fallback_keys:%s" % sid, "FAIL", "; ".join(bad))]
        rer = reroute_seat(s, pc)
        effort, cap = budget(rer, pc)
        sent = ["max_tokens"] * bool(cap) + ["reasoning"] * bool(effort)
        est, edata = openrouter_get("/models/%s/endpoints" % slug, cfg)
        body = edata.get("data") if est == 200 and isinstance(edata, dict) else None
        eps = body.get("endpoints") if isinstance(body, dict) else None
        if isinstance(eps, list):
            fits = [e for e in eps if isinstance(e, dict) and all(q in (e.get("supported_parameters") or []) for q in sent)
                    and isinstance(e.get("max_completion_tokens"), (int, float)) and e["max_completion_tokens"] >= cap]
            names = {str(e.get("provider_name")) for e in fits}
            out.append(Check("openrouter", "free", "reroute:%s" % sid, "PASS" if len(names) >= 2 else "WARN",
                             "%d providers take %s at max_tokens %s without an `order` pin (%d endpoints)"
                             % (len(names), "+".join(sent), cap, len(fits))
                             + ("" if len(names) >= 2 else ": the rerouted retry has no second endpoint")))
        else:
            out.append(Check("openrouter", "free", "reroute:%s" % sid, "WARN",
                             "GET /models/%s/endpoints -> %s: rerouted fallback not checked" % (slug, est)))
        out.extend(self._standin_check(s, pc, by_id))
        return out

    def free_checks(self, seats, cfg):
        pc = _pcfg(cfg, "openrouter")
        mine = _provider_seats(seats, "openrouter")
        key_env = pc.get("key_env", "OPENROUTER_API_KEY")
        out = []
        have_key = bool(os.environ.get(key_env))
        out.append(Check("openrouter", "free", "key_env", "PASS" if have_key else "FAIL",
                         "%s present" % key_env if have_key else "%s is not set" % key_env))
        if have_key:
            st, _ = openrouter_get("/key", cfg)
            out.append(Check("openrouter", "free", "key", "PASS" if st == 200 else "FAIL", "GET /key -> %s" % st))
            mst, data = openrouter_get("/models", cfg)
            by_id = {m.get("id"): m for m in (data or {}).get("data", []) if isinstance(m, dict)}
            for s in mine:
                slug = s.get("model")
                name = "models:%s" % s.get("id", "?")
                if mst != 200:
                    out.append(Check("openrouter", "free", name, "FAIL", "GET /models -> %s" % mst))
                elif slug not in by_id:
                    # a seat whose model is gone still runs when its stand-in is fine: it starts on the stand-in
                    standin_rows = self._standin_check(s, pc, by_id)
                    if standin_rows and standin_rows[0].result != "FAIL":
                        out.append(Check("openrouter", "free", name, "WARN",
                                         "%s not in /models; the seat runs as its stand-in %s" % (slug, s["fallback_model"]),
                                         standin={"seats": [s.get("id")], "reason": "preflight:%s not in /models" % slug}))
                        out.extend(standin_rows)
                    else:
                        out.append(Check("openrouter", "free", name, "FAIL", "%s not in /models" % slug))
                else:
                    out.append(Check("openrouter", "free", name, "PASS", "%s in /models" % slug))
                    sup = ((by_id[slug].get("reasoning") or {}).get("supported_efforts")
                           if isinstance(by_id[slug].get("reasoning"), dict) else None)
                    try:
                        out.append(reasoning_cap_check("openrouter", s, pc, by_id[slug]))
                    except ValueError as e:
                        out.append(Check("openrouter", "free", "reasoning_cap:%s" % s.get("id", "?"), "FAIL", str(e)))
                    out.append(salvage_reasoning_check("openrouter", s, pc, by_id[slug]))
                    prefs = routing_prefs(cfg, s)
                    if prefs:
                        est, edata = openrouter_get("/models/%s/endpoints" % slug, cfg)
                        body = edata.get("data") if est == 200 and isinstance(edata, dict) else None
                        eps = body.get("endpoints") if isinstance(body, dict) else None
                        if isinstance(eps, list):
                            out.append(routing_check(s, pc, eps, prefs))
                        else:
                            out.append(Check("openrouter", "free", "routing:%s" % s.get("id", "?"), "WARN",
                                             "GET /models/%s/endpoints -> %s: routing prefs not checked" % (slug, est)))
                    eff, _c = budget(s, pc)
                    if sup and eff not in sup:
                        out.append(Check("openrouter", "free", "effort:%s" % s.get("id", "?"), "WARN",
                                         "%s not in supported_efforts %s" % (eff, "/".join(sup))))
                    out.extend(self._fallback_checks(s, pc, cfg, by_id))
        else:
            out.append(Check("openrouter", "free", "key", "SKIP", "no key"))
        # inner_caps: socket timeout is computed per launch from the launch's own limits
        idle, top = _idle(cfg, "openrouter"), _max_ceiling(cfg, "openrouter")
        mode = pc.get("mode", "stream")
        if top is None:                         # no ceiling (row 35): the socket timeout is derived from it
            out.append(Check("openrouter", "free", "inner_caps", "FAIL",
                             "no ceiling (ceiling_s = 0): the seat child's socket timeout needs one"))
        else:
            lim_ = Limits(mode=mode, idle_s=idle, ceiling_s=top, deadline_s=top if mode != "stream" else None)
            sock = or_socket_timeout(lim_)
            need = idle if mode == "stream" else top
            out.append(Check("openrouter", "free", "inner_caps", "PASS" if sock >= need else "FAIL",
                             "socket timeout %gs (%s) >= %gs" % (sock, "idle+30" if mode == "stream" else "deadline+30",
                                                                 need)
                             if sock >= need else "socket timeout %gs < %gs" % (sock, need)))
        probs = _budget_problems("openrouter", pc, OR_EFFORTS, True, cfg, True, seats)
        a, ac = budget({"id": "x"}, pc)
        c_, cc = budget({"id": "chair"}, pc)
        out.append(Check("openrouter", "free", "output_budget", "FAIL" if probs else "PASS",
                         "; ".join(probs) or "advisor %s/%s tok, chair %s/%s tok (request field max_tokens; %s; "
                         "reasoning.effort and reasoning.max_tokens are never sent together)"
                         % (a, ac, c_, cc, _form_detail(pc, cfg))))
        return out


register(ClaudeAdapter())
register(CodexAdapter())
register(AgyAdapter())
register(QwenAdapter())
register(OpenRouterAdapter())
register(StubAdapter())
