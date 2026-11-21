"""Drift check: the settings every seat actually runs with, diffed on every preflight against the ACCEPTED baseline.

Free only: local `--version` / `--help` / `agy models` / `codex debug models`, the CLI-owned config files (codex
config.toml, qwen generationConfig), file hashes of the binaries, and the free OpenRouter `GET /models` and
`GET /models/<slug>/endpoints` (cached per process, shared with the free checks). No paid call, no prompt.

  collect(cfg) -> snapshot {"schema", "collected_at", "items", "missing"}
      items:   {"<group>.<name>": {"value": ..., "source": "cli" | "config"}}
               cli    = the CLI, its own config file, a binary or OpenRouter reports it
               config = the runner computes it from config.toml (or from the code defaults)
      missing: key prefixes that could not be collected this pass (CLI failed, offline). A missing key is
               reported as NOT COLLECTED and its last value is kept, never reported GONE.
  diff(old, new) -> [{"kind": CHANGED | NEW | GONE | NOT_COLLECTED, "key", "old", "new", "source"}]
  report(old, new) -> (text, rc)    rc 0 first pass or no change, 1 anything to report
  observe(cfg, new, trigger, old)   preflight: ONE history.jsonl line; moves last.json only to seed it when absent
  accept(cfg, new, trigger, old, keys=None)   the only call that moves last.json (600; dir 700) and appends history
      Sticky alarms (D19): an alarm stays raised on every preflight until `council.py drift --accept [KEY...]`.
      `record` is the old name of `observe`.

Canonical form (row 24; D19): `diff` canonicalises BOTH snapshots before comparing (`canonical_items`):
  numbers 3600 == 3600.0 (a bool never equals 0/1), keys sorted; volatile fields are dropped (VOLATILE_NAMES /
  VOLATILE_PREFIXES: pid, port, otlp_port, daemon_*, timestamps, OpenRouter pricing / status / uptime_* / latency_* /
  throughput_* / perf_*), never in the owner's knobs (KNOB_PREFIXES); in manifest.* and codex.effective.* strings a
  loopback host:port becomes <port> and the launch tag value (netwatch.TAG_ENV) <tag>. Temp paths and the prompt are
  already "<tmp>" / "<prompt>" at collect time. There is no allowlist: a key in only one snapshot is NEW or GONE. A
  field with no collector surfaces as a coverage-gap item (openrouter.endpoints.<slug>.unclassified_fields). The
  generated mutation test (tests/test_drift.py, class Mutation) mutates every leaf and asserts the alarm.

Snapshot shape (schema 2; the mutation test walks exactly these keys):
  manifest.<seat>.<kind>.{argv,env,limits,policy[,files]}
        <kind> = seat | salvage (every roster seat), chair | chair_salvage (the chair). Built by the REAL
        `adapter.build()` with the REAL `council_run._limits` against an empty latency log, in a temp dir.
        argv: the launch argv with the temp workdir -> "<tmp>", the prompt path and the probe prompt text ->
          "<prompt>", the engine's own root (abspath or realpath of the council dir) -> "<engine>" in every value of
          the record, so the rows do not depend on which engine copy runs (a baseline with absolute paths shows those
          rows CHANGED once). env: only the keys that differ from `providers._clean_env()` (values redacted by rule below);
          env_unset: names the adapter removes (qwen). limits: the Limits fields + mode + deadline_s/source.
          policy: the resolved reasoning_form / effort / max_output_tokens / reasoning cap / answer reserve.
          files: the qwen per-launch overlay (redacted).
  config.<dotted.path>        every leaf of the merged config.toml, no allowlist. `[[seat]]` is keyed by id
        (config.seat.<id>.<key>); keys starting "_" (runtime flags) are skipped.
  runner.<provider>           the full merged [timeouts.default] + [timeouts.<provider>] table (all keys).
  defaults.<dotted.path>      "default:<v>" for a key the config omits: the value the code uses, read from the
        code (`_limits`, `timing.adaptive_deadline`, `salvage_cfg`, DEFAULT_ANSWER_RESERVE), never copied.
  <cli>.bin_sha256, codex.native_bin_sha256, openrouter.seat_script_sha256     file hashes (realpath of which <bin>)
  codex.effective.<dotted.key>   ~/.codex/config.toml flattened, with the first codex seat's `-c k=v` overrides on
        top; the TUI-written tables `projects` and `notice` are skipped as volatile.
  codex.debug_models.<model>     the seated models' entry from `codex debug models` (JSON expected; long values are
        hashed; non-JSON output -> codex.debug_models_raw_sha256). The output format is UNVERIFIED until row 21.
  openrouter.endpoints.<slug>.<tag>   stable per-endpoint fields only (provider_name, context_length,
        max_completion_tokens, quantization, reasoning/max_tokens support). Volatile, never recorded: pricing
        (D43), status, uptime_*, latency_*, throughput_*, perf_*, supports_implicit_caching. Any other field name is
        listed in openrouter.endpoints.<slug>.unclassified_fields (names only).
  openrouter.endpoints.<slug>.routable_for_budget   tags whose max_completion_tokens >= the largest seat budget.
  plus the earlier items: <cli>.version / help_sha256 / help_flags, agy.labels, claude.env.*, qwen.*,
  openrouter.models.<slug>, roster.*, pins.*.

Secrets (D19): key NAMES are kept. A value is sha256-8 ("sha256:ab12cd34") when any path segment is env / auth / a
`*headers` table (`headers`, codex's `http_headers`), or the key name matches token|secret|password|credential|bearer|auth|key (a string only; names ending
`_env` are env-var names, not secrets). The runner's own timeout vars stay readable. `sk-...` shapes are scrubbed
anywhere. Never recorded: prompt text, help text (only its sha256).
The user's codex config (`codex.effective.*`) is stricter (C5): every leaf of `mcp_servers.<name>` except enabled /
required / *_timeout_sec, and every URL-shaped string (a private MCP URL, `base_url`), is recorded as sha256-8 so a
change still alarms, and is PRINTED only as `<set>` / `<unset>` (`_display`), by path and shape, so a baseline written
before this rule prints nothing either; a changed one reads "value changed, not shown". Server names stay visible.
The council's own config (`config.*`, `runner.*`) and the launch argv built from it (`manifest.*`) are stored the same way for URLs: any URL-shaped string is sha256-8
(`_redact(..., urls=True)`; so is a scheme-less host such as `private-box.lan:8080`, see `_private_host`; codex's `query_params` values are secret like headers), and one that still reaches a print path in plain (a baseline written before this rule) shows
as `<set>`. `_stored` re-applies the rules to a baseline value, so an old baseline neither alarms nor prints.

Note: `claude --help` hides `--thinking` and `--thinking-display` (.hideHelp()), so the help-flag
item lists only flags the help prints; a hidden flag is never reported missing.
"""
from __future__ import annotations

import dataclasses
import glob
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor

from . import netwatch, providers
from .supervise import Limits

SCHEMA = 2
DRIFT_DIR = "drift"
LAST = "last.json"
HISTORY = "history.jsonl"
VALUE_MAX = 160

# flags the adapters send that the CLI's --help prints (hidden ones excluded on purpose, see above)
HELP_FLAGS = {
    "claude": ("--effort", "--safe-mode", "--no-session-persistence", "--output-format",
               "--include-partial-messages", "--tools"),
    "codex": ("--json", "--ephemeral", "--skip-git-repo-check", "--output-last-message", "--config"),
    "agy": ("--print-timeout", "--output-format", "--sandbox", "--model", "--print"),
}
HELP_ARGV = {"claude": ["--help"], "codex": ["exec", "--help"], "agy": ["--help"], "qwen": ["--help"]}
CLAUDE_ENV_KEYS = ("API_TIMEOUT_MS", "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS", "CLAUDE_CODE_MAX_RETRIES",
                   "CLAUDE_CODE_MAX_OUTPUT_TOKENS")
CODEX_BLOCK_KEYS = ("stream_idle_timeout_ms", "request_max_retries", "stream_max_retries", "requires_openai_auth")
QWEN_GC_KEYS = ("timeout", "maxRetries")
_KEYISH = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
_SECRET_NAME = re.compile(r"token|secret|password|credential|bearer|auth|key", re.I)
SECRET_PARENTS = ("env", "headers", "auth", "query_params")
def _secret_parent(seg):
    """A path segment whose children are secret values: env / auth, codex's `query_params` (values appended to a provider URL),
    and any `*headers` table (`headers`, codex's `http_headers` and `env_http_headers`). Header VALUES are tenant ids, tokens and
    the like, whatever the header is called."""
    seg = str(seg).lower()
    return seg in SECRET_PARENTS or seg.endswith("headers")
# The user's own codex config (`codex.effective.*`) carries private MCP server URLs, commands and header values. Those
# are recorded as sha256-8 (a change still alarms) and never printed: see `_redact_codex` and `_display`.
CODEX_EFF = "codex.effective."
OWN_CFG = ("config.", "runner.")
MCP_PLAIN = ("enabled", "required", "startup_timeout_sec", "tool_timeout_sec")    # mcp_servers.<name>.<key> shown as is
_URLISH = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_HASHED = re.compile(r"^sha256:[0-9a-f]{8}$")
_HASHED_ANY = re.compile(r"sha256:[0-9a-f]{8}")
def _private_url(v):
    """A URL-shaped string that is not the engine's own loopback endpoint (the per-launch OTLP sink, masked as <port>)."""
    return bool(_URLISH.search(v)) and not _LOOPBACK.search(v)


# A scheme-less host (`private-box.lan:8080`, `10.0.0.5:9000/v1`) is as private as a URL. `host:port` is recognised by shape; a dotted
# name with a real TLD and no port (`proxy.corp.example/v1`) only under a key that names an address, so file names and versions stay readable.
_HOSTPORT = re.compile(r"^(?:[A-Za-z][A-Za-z0-9\-]*(?:\.[A-Za-z0-9\-]+)*|\d{1,3}(?:\.\d{1,3}){3}):\d{2,5}(?:/\S*)?$")
_DOTTED_HOST = re.compile(r"^[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}(?::\d{2,5})?(?:/\S*)?$")
_ADDR_KEY = re.compile(r"url|host|endpoint|base|proxy|server|domain|address|origin", re.I)
def _private_host(v, key=""):
    """A scheme-less host string that is not the engine's own loopback endpoint (see `_private_url`)."""
    if not isinstance(v, str) or not v or _LOOPBACK.search(v) or _URLISH.search(v):
        return False
    return bool(_HOSTPORT.match(v)) or bool(key and _ADDR_KEY.search(str(key)) and _DOTTED_HOST.match(v))


SET, UNSET = "<set>", "<unset>"
PLAIN_ENV = ("API_TIMEOUT_MS", "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS", "CLAUDE_CODE_MAX_RETRIES",
             "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "QWEN_STREAM_IDLE_TIMEOUT_MS", "QWEN_STREAM_MAX_LIFETIME_MS",
             "QWEN_CODE_SYSTEM_SETTINGS_PATH")
CODEX_VOLATILE_TABLES = ("projects", "notice")        # written by the codex TUI as the owner trusts folders
PROBE_PROMPT = "COUNCIL-DRIFT-PROBE-PROMPT"
SALVAGE_KINDS = {"seat": "salvage", "chair": "chair_salvage"}
# OpenRouter endpoint fields, three classes (D43, D19 "no allowlist"): STABLE is recorded; VOLATILE (below) and the
# identity fields are never alarms; any other field is unclassified and surfaces as one `unclassified_fields` item, so
# a new field alarms NEW once instead of being dropped silently.
ENDPOINT_STABLE = ("provider_name", "context_length", "max_completion_tokens", "quantization")
ENDPOINT_IDENTITY = ("tag", "name", "model_name")     # tag keys the item; the others restate provider + model
# supported_parameters is recorded for the members the seats send (openrouter_seat.py body: stream, max_tokens,
# reasoning). Any other member is "not sent by any seat" and not behaviour-relevant; add it here when a seat sends it.
ENDPOINT_PARAMS = ("reasoning", "max_tokens")
# Volatile (row 24): never an alarm. Exact names, plus the prefixes below. Applied at diff time to both snapshots, so
# an older baseline that still holds them cannot alarm either. NOT applied to user knobs (KNOB_PREFIXES).
VOLATILE_NAMES = ("pricing", "status", "supports_implicit_caching",           # OpenRouter endpoint / model (D43)
                  "pid", "ppid", "port", "otlp_port", "daemon_pid", "daemon_port",          # runtime (launch_opts, daemon)
                  "timestamp", "collected_at", "recorded_at", "latency_samples")
VOLATILE_PREFIXES = ("uptime_", "latency_", "throughput_", "perf_")
KNOB_PREFIXES = ("config.", "runner.", "defaults.", "pins.", "roster.", "codex.effective.")    # values the owner sets
MASK_STR_PREFIXES = ("manifest.", "codex.effective.")      # strings that can carry a per-launch loopback port or tag
_LOOPBACK = re.compile(r"(?<![\w.])(127\.0\.0\.1|localhost|\[::1\]|::1):\d{2,5}\b")
_TAG_ASSIGN = re.compile(r"(%s=)[^\s\"',;]+" % re.escape(netwatch.TAG_ENV))
_PORT_ENV = re.compile(r"(?:^|_)PORT$")
LONG_VALUE = 200
_HASH_CACHE: dict = {}


class NotCollected(Exception):
    """A value could not be read this pass (CLI failed, offline, file unreadable)."""


# ---------------------------------------------------------------- free probes (tests replace these)
def _capture(argv, timeout=30):
    """-> (rc | None, stdout, stderr). Free local command, never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, env=providers._clean_env(),
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", type(e).__name__


def _or_get(path, cfg):
    return providers.openrouter_get(path, cfg)        # module attribute: tests replace it


def _which(name):
    return shutil.which(name)                         # tests replace it: no real binary is hashed in a test


def _sha256_file(path):
    """sha256 hex of a file, cached per process by (path, mtime, size). Raises OSError."""
    st = os.stat(path)
    ck = (path, st.st_mtime_ns, st.st_size)
    if ck not in _HASH_CACHE:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        _HASH_CACHE[ck] = h.hexdigest()
    return _HASH_CACHE[ck]


# ---------------------------------------------------------------- helpers
def _scrub(v):
    if isinstance(v, str):
        return _KEYISH.sub("sk-...", v)
    if isinstance(v, dict):
        return {str(k): _scrub(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub(x) for x in v]
    return v


def _canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def _show(v):
    s = v if isinstance(v, str) else _canon(v)
    return s if len(s) <= VALUE_MAX else s[:VALUE_MAX - 3] + "..."


def _h8(v):
    return "sha256:" + hashlib.sha256(_canon(v).encode("utf-8")).hexdigest()[:8]


def _redact(path, v, urls=False):
    """One leaf at `path` (list of key names) -> the value to record. See the module docstring. With `urls` (the council's own
    config, `config.*` and `runner.*`) any URL-shaped string is recorded as sha256-8 too. Idempotent: a sha256-8 stays."""
    if isinstance(v, (dict, list, tuple)) or v is None:
        return v
    key = str(path[-1]) if path else ""
    if key in PLAIN_ENV:
        return _scrub(v)
    if isinstance(v, str) and _HASHED.match(v):
        return v
    if any(_secret_parent(seg) for seg in path[:-1]) or \
            (isinstance(v, str) and v and _SECRET_NAME.search(key) and not key.lower().endswith("_env")):
        return _h8(v)
    if urls and isinstance(v, str) and (_URLISH.search(v) or _private_host(v, key)):
        return _h8(v)
    return _scrub(v)


def _redact_tree(obj, path=(), urls=False):
    if isinstance(obj, dict):
        return {str(k): _redact_tree(x, path + (str(k),), urls) for k, x in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_tree(x, path, urls) for x in obj]
    return _redact(list(path), obj, urls)


def _hash_urls(v):
    """Every non-loopback URL-shaped string anywhere in `v` -> sha256-8 (the argv a launch is built with carries the council's own
    `base_url`). The engine's own loopback sink stays readable: it is masked as <port> at diff time."""
    if isinstance(v, dict):
        return {k: _hash_urls(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_hash_urls(x) for x in v]
    return _h8(v) if isinstance(v, str) and (_private_url(v) or _private_host(v)) else v


def _own_cfg(key):
    """True for the items that mirror the council's own config (`config.*`, `runner.*`): their URLs are private."""
    return key.startswith(OWN_CFG)


def _stored(key, v):
    """The recorded form of the value of item `key` (what `collect` writes), computed again for a value read back from a baseline or
    handed to history.jsonl: a baseline written before a redaction rule holds the plain value and must equal today's hash."""
    if key.startswith(CODEX_EFF):
        return _redact_codex(v, tuple(key[len(CODEX_EFF):].split(".")))
    if key.startswith(MASK_STR_PREFIXES[0]):                     # manifest.<seat>.<kind>.<part>
        return _hash_urls(v)
    if _own_cfg(key):
        return _redact_tree(v, tuple(key.split(".")[1:]) if key.startswith("config.") else (), True)
    return v


def _redact_codex(v, path=()):
    """`_redact_tree` for the user's codex config. On top of the secret-name rule: every leaf of an `mcp_servers.<name>`
    table except the MCP_PLAIN keys and every URL-shaped string is recorded as sha256-8 (never the value); bools and
    numbers stay readable. Idempotent: a value that is already sha256-8 is left alone."""
    if isinstance(v, dict):
        return {str(k): _redact_codex(x, path + (str(k),)) for k, x in v.items()}
    secret = len(path) >= 3 and path[0] == "mcp_servers" and str(path[-1]) not in MCP_PLAIN
    if isinstance(v, (list, tuple)):
        return _h8(list(v)) if secret and v else [_redact_codex(x, path) for x in v]
    if v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and _HASHED.match(v)):
        return v
    if isinstance(v, str) and (secret or _private_url(v) or _private_host(v, path[-1] if path else "")):
        return _h8(v) if v else v
    return _redact(list(path), v)


def _display(key, v):
    """How one recorded value is PRINTED (drift rows, the preflight table). Under `codex.effective.` an MCP server
    leaf, a URL and a sha256-8 are shown as `<set>` / `<unset>` only; this keys off the path and the shape, not on
    how the value was stored, so a baseline written before the redaction (plain URLs) prints nothing either."""
    if key.startswith(CODEX_EFF):
        rest = key[len(CODEX_EFF):].split(".")
        mcp = len(rest) >= 3 and rest[0] == "mcp_servers" and rest[-1] not in MCP_PLAIN
        if isinstance(v, (bool, int, float)) and not mcp:
            return _show(v)
        if mcp or _HASHED_ANY.search(_canon(v)) or _private_url(_canon(v)) or _private_host(v, rest[-1]) \
                or any(_secret_parent(seg) for seg in rest[:-1]):
            return UNSET if v in (None, "", [], {}) else SET
    elif (_own_cfg(key) and (_URLISH.search(_canon(v)) or _private_host(v, key.rsplit(".", 1)[-1]))) or \
            (key.startswith(MASK_STR_PREFIXES[0]) and (_private_url(_canon(v)) or _private_host(v))):   # a URL of the council's own config: recorded hashed, and a plain one (old baseline) never printed
        return SET
    return _show(v)


def _flatten(obj, prefix=()):
    """-> [(path tuple, leaf)]. A list of dicts that carry `id` is keyed by id (reorder-proof); any other list
    is one leaf unless it holds dicts (then keyed by index)."""
    out = []
    if isinstance(obj, dict):
        for k in sorted(obj, key=str):
            if str(k).startswith("_"):
                continue
            out += _flatten(obj[k], prefix + (str(k),))
        return out
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
        for i, x in enumerate(obj):
            out += _flatten(x, prefix + (str(x.get("id", i)),))
        return out
    return [(prefix, obj)]


def _long(v):
    """A long string or big container is replaced by its hash (prompt-sized catalog fields)."""
    s = v if isinstance(v, str) else _canon(v)
    return v if len(s) <= LONG_VALUE else "%s(len %d)" % (_h8(v), len(s))


def _runner(cfg, provider, chair=False):
    t = (cfg or {}).get("timeouts") or {}
    d = dict(t.get("default") or {})
    d.update({k: v for k, v in (t.get(provider) or {}).items() if k != "adaptive"})
    if chair:
        d.update(t.get("chair") or {})
    return d


def _seat_ceiling(cfg, provider):
    return float(_runner(cfg, provider).get("ceiling_s", 1200))


def _ok(rc, out):
    if rc != 0:
        raise NotCollected("rc %s" % rc)
    return out


def _cli_version(binp):
    rc, out, err = _capture([binp, "--version"])
    v = providers._version(_ok(rc, out) or err)
    if v is None:
        raise NotCollected("no version")
    return providers._vstr(v)


def _help(binp, provider):
    """-> (sha256 first 16 hex, text). Hash stdout; agy prints its help on stderr, so fall back to it."""
    rc, out, err = _capture([binp] + HELP_ARGV[provider])
    text = _ok(rc, out) or err
    if not text.strip():
        raise NotCollected("empty help")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], text


def _flags(text, provider):
    return {f: bool(re.search(r"(^|[\s,])%s(?![\w-])" % re.escape(f), text)) for f in HELP_FLAGS.get(provider, ())}


# ---------------------------------------------------------------- collectors: add(key, value, source)
def _cli_common(provider, binp, add):
    add("version", lambda: _cli_version(binp), "cli")
    holder = {}

    def sha():
        holder["sha"], holder["text"] = _help(binp, provider)
        return holder["sha"]
    add("help_sha256", sha, "cli")
    if provider in HELP_FLAGS:
        add("help_flags", lambda: _flags(holder["text"], provider) if "text" in holder else _raise(), "cli")
    return holder


def _raise():
    raise NotCollected("help unavailable")


def _claude(cfg, seats, add):
    pc = providers._pcfg(cfg, "claude")
    _cli_common("claude", pc.get("bin", "claude"), add)
    _bins(pc.get("bin", "claude"), add)

    def env_for(seat, ceiling):
        _, cap = providers.budget(seat, pc)
        e = providers.claude_env(ceiling, pc, cap)
        return {k: e[k] for k in CLAUDE_ENV_KEYS if k in e}
    if any(not providers.is_chair(s) for s in seats):
        add("env.seat", lambda: env_for({"id": "x"}, _seat_ceiling(cfg, "claude")), "config")
    if any(providers.is_chair(s) for s in seats):
        add("env.chair", lambda: env_for({"id": "chair"}, float(_runner(cfg, "claude", True).get("ceiling_s", 1200))),
            "config")


def _codex(cfg, seats, add):
    pc = providers._pcfg(cfg, "codex")
    binp = pc.get("bin", "codex")
    _cli_common("codex", binp, add)
    real = _bins(binp, add)

    def native():
        path = _codex_native(real())
        try:
            return None if path is None else _sha256_file(path)
        except OSError:
            raise NotCollected("cannot read the native codex binary")
    add("native_bin_sha256", native, "cli")
    conf_box = {}

    def conf():
        if "c" not in conf_box:
            conf_box["c"] = providers.CodexAdapter()._toml(pc)
        if conf_box["c"] is None:
            raise NotCollected("codex config.toml unreadable")
        return conf_box["c"]
    name = pc.get("council_provider")
    if name:
        def blk():
            b = ((conf().get("model_providers") or {}).get(name))
            return None if b is None else {k: b.get(k) for k in CODEX_BLOCK_KEYS}
        add("council_provider.%s" % name, blk, "cli")
    add("check_for_update_on_startup", lambda: conf().get("check_for_update_on_startup"), "cli")

    def effective():
        """~/.codex/config.toml (minus the TUI-written tables) with the first codex seat's `-c` overrides on top."""
        eff = json.loads(json.dumps({k: v for k, v in conf().items() if k not in CODEX_VOLATILE_TABLES}, default=str))
        seat = next((x for x in seats if not providers.is_chair(x)), seats[0])
        for k, v in _c_overrides(_launch_record(cfg, seat, "chair" if providers.is_chair(seat) else "seat")["argv"]).items():
            _set_dotted(eff, k, v)
        return _flatten(eff)
    try:
        leaves = effective()
    except Exception:
        add("effective", _raise, "cli")
    else:
        for path, v in leaves:
            add("effective.%s" % ".".join(path), lambda v=v, path=path: _redact_codex(v, path), "cli")

    box = {}

    def dm():
        if "t" not in box:
            rc, out, _ = _capture([binp, "debug", "models"], timeout=45)
            box["t"] = _ok(rc, out)
        return box["t"]

    def catalog():
        try:
            data = json.loads(dm())
        except ValueError:
            return None
        rows = data.get("models") if isinstance(data, dict) else data
        out = {}
        for e in rows if isinstance(rows, list) else []:
            if isinstance(e, dict):
                for k in ("slug", "id", "model", "name"):
                    if isinstance(e.get(k), str):
                        out.setdefault(e[k], e)
        return out
    try:
        cat = catalog()
    except Exception:
        add("debug_models", _raise, "cli")
    else:
        if cat is None:           # format not JSON (UNVERIFIED until row 21): a hash is the best stable record
            add("debug_models_raw_sha256", lambda: hashlib.sha256(dm().encode("utf-8")).hexdigest(), "cli")
        else:
            for m in sorted({str(x.get("model")) for x in seats}):
                add("debug_models.%s" % m,
                    lambda m=m: (None if m not in cat else
                                 {k: _long(_redact_tree(v, (k,))) for k, v in sorted(cat[m].items())}), "cli")


def _agy(cfg, seats, add):
    pc = providers._pcfg(cfg, "agy")
    binp = pc.get("bin", "agy")
    holder = _cli_common("agy", binp, add)
    _bins(binp, add)

    def default_timeout():
        if "text" not in holder:
            raise NotCollected("help unavailable")
        for line in holder["text"].splitlines():
            if "--print-timeout" in line:
                m = re.search(r"\(default ([^)]*)\)", line)
                return m.group(1) if m else None
        return None
    add("print_timeout_default", default_timeout, "cli")

    def labels():
        rc, out, _ = _capture([binp, "models"], timeout=45)
        rows = providers.parse_agy_models(_ok(rc, out))
        if not rows:
            raise NotCollected("no catalog")
        return sorted(lab for _, lab in rows)
    add("labels", labels, "cli")
    add("print_timeout_sent", lambda: "%ds" % int(providers._max_ceiling(cfg, "agy") + 60), "config")


def _qwen(cfg, seats, add):
    pc = providers._pcfg(cfg, "qwen")
    add("version", lambda: _cli_version(pc.get("bin", "qwen")), "cli")
    _bins(pc.get("bin", "qwen"), add)
    settings_box = {}

    def entry(model):
        if "s" not in settings_box:
            settings_box["s"] = providers._qwen_load(pc)
        if settings_box["s"] is None:
            raise NotCollected("qwen settings unreadable")
        e = providers._qwen_entry(settings_box["s"], model)       # one modelProviders.openai[] entry, no env block
        gc = (e or {}).get("generationConfig") or {}
        return None if e is None else {k: gc.get(k) for k in QWEN_GC_KEYS}
    for s in seats:
        if not providers.is_chair(s):
            add("generation_config.%s" % s.get("model"), lambda m=s.get("model"): entry(m), "cli")

    def guards():
        idle, life, req = providers.qwen_guards_ms(Limits(mode="stream", idle_s=providers._idle(cfg, "qwen"),
                                                          ceiling_s=providers._max_ceiling(cfg, "qwen"),
                                                          deadline_s=None))
        return {"stream_idle_ms": idle, "max_lifetime_ms": life, "request_timeout_ms": req}
    add("guards", guards, "config")


def _or_fields(m):
    tp = m.get("top_provider") or {}
    r = m.get("reasoning") if isinstance(m.get("reasoning"), dict) else {}
    return {
        "max_completion_tokens": tp.get("max_completion_tokens"),
        "context_length": m.get("context_length"),
        "top_provider_context_length": tp.get("context_length"),
        "reasoning": {k: r.get(k) for k in ("mandatory", "default_enabled", "supported_efforts",
                                            "supports_max_tokens", "default_effort")},
    }


def _openrouter(cfg, seats, add):
    pc = providers._pcfg(cfg, "openrouter")
    box = {}

    def models():
        if "d" not in box:
            st, data = _or_get("/models", cfg)
            if st != 200 or not isinstance(data, dict):
                raise NotCollected("GET /models -> %s" % st)
            box["d"] = {m.get("id"): m for m in data.get("data", []) if isinstance(m, dict)}
        return box["d"]
    for slug in sorted({str(s.get("model")) for s in seats}):
        add("models.%s" % slug, lambda sl=slug: (_or_fields(models()[sl]) if sl in models() else None), "cli")

    def sock():
        mode = pc.get("mode", "stream")
        top = providers._max_ceiling(cfg, "openrouter")
        lim = Limits(mode=mode, idle_s=providers._idle(cfg, "openrouter"), ceiling_s=top,
                     deadline_s=top if mode != "stream" else None)
        return providers.or_socket_timeout(lim)
    add("socket_timeout_s", sock, "config")
    add("seat_script_sha256", lambda: _sha256_file(providers.OPENROUTER_SEAT), "config")

    # D43: the endpoints API, stable per-endpoint fields only (pricing, status, uptime_*, latency_*, throughput_*,
    # perf_* are volatile and never recorded). One item per endpoint tag, so a provider joining or leaving alarms.
    def endpoints(slug):
        st, data = _or_get("/models/%s/endpoints" % slug, cfg)
        eps = ((data or {}).get("data") or {}).get("endpoints") if st == 200 and isinstance(data, dict) else None
        if not isinstance(eps, list):
            raise NotCollected("GET /models/%s/endpoints -> %s" % (slug, st))
        return [e for e in eps if isinstance(e, dict)]

    def fields(e):
        params = e.get("supported_parameters") or []
        out = {k: e.get(k) for k in ENDPOINT_STABLE}
        out["params"] = {q: q in params for q in ENDPOINT_PARAMS}
        return out

    def unclassified(e):
        known = set(ENDPOINT_STABLE) | set(ENDPOINT_IDENTITY) | {"supported_parameters"}
        return sorted(str(k) for k in e if k not in known and not _volatile_name(k))
    for slug in sorted({str(x.get("model")) for x in seats}):
        need = [providers.budget(x, pc)[1] for x in seats if str(x.get("model")) == slug]
        need = max([int(c) for c in need if c] or [0])
        try:
            eps = endpoints(slug)
        except Exception:
            add("endpoints.%s" % slug, _raise, "cli")
            continue
        seen, gaps = {}, {}
        for e in eps:
            tag = str(e.get("tag") or e.get("provider_name") or "unknown")
            seen[tag] = seen.get(tag, 0) + 1
            if seen[tag] > 1:
                tag += "#%d" % seen[tag]
            add("endpoints.%s.%s" % (slug, tag), lambda e=e: fields(e), "cli")
            if unclassified(e):
                gaps[tag] = unclassified(e)
        if gaps:          # coverage-gap row: names only (a value can be volatile), alarms NEW when a field appears
            add("endpoints.%s.unclassified_fields" % slug, lambda gaps=gaps: dict(sorted(gaps.items())), "cli")
        add("endpoints.%s.routable_for_budget" % slug,
            lambda eps=eps, need=need: sorted(str(e.get("tag") or e.get("provider_name") or "unknown") for e in eps
                                              if int(e.get("max_completion_tokens") or 0) >= need), "cli")


# ---------------------------------------------------------------- the real launch (manifest)
def _tryv(f):
    try:
        return f()
    except (ValueError, KeyError, TypeError):
        return None


ENGINE = "<engine>"


def _engine_re():
    """The engine's own root (the council dir `providers.HERE` sits in), abspath and realpath, as one pattern that
    matches only at a path boundary. Launch records hold it (the OpenRouter seat script, ...): without the mapping the
    argv rows would depend on which copy of the engine computes them (the working tree, the installed snapshot, a new
    version) and flap between copies. `summary.json` `engine_root` still says which copy ran."""
    root = os.path.dirname(providers.HERE)
    forms = sorted({os.path.abspath(root), os.path.realpath(root)}, key=len, reverse=True)
    return re.compile("(?:%s)(?=/|\\\\|$|[^\\w.-])" % "|".join(re.escape(f) for f in forms))


def _engine_tree(obj, rx=None):
    """Every string in a launch record with the engine root -> "<engine>" (argv, env, io, limits, policy, files)."""
    rx = rx or _engine_re()
    if isinstance(obj, str):
        return rx.sub(ENGINE, obj)
    if isinstance(obj, dict):
        return {k: _engine_tree(v, rx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_engine_tree(v, rx) for v in obj]
    return obj


def _place(text, tmp, ppath):
    t = str(text).replace(ppath, "<prompt>").replace(PROBE_PROMPT, "<prompt>").replace(tmp, "<tmp>")
    return _scrub(_engine_re().sub(ENGINE, t))


def _launch_record(cfg, seat, kind):
    """What one launch of `seat` (kind seat | chair | salvage | chair_salvage) really runs with: the REAL
    adapter.build() and council_run._limits against an EMPTY latency log (history never moves it), in a temp
    dir. Temp paths and the probe prompt become "<tmp>" / "<prompt>", the engine's own root "<engine>" (any copy). Raises
    when the build raises."""
    from . import council_run                       # late: council_run may import drift
    adapter = providers.get(seat["provider"])
    pc = providers._pcfg(cfg, seat["provider"])
    with tempfile.TemporaryDirectory(prefix="drift-") as tmp:
        wd = os.path.join(tmp, "wd")
        os.makedirs(wd, mode=0o700)
        ppath = os.path.join(tmp, "prompt.md")
        with open(ppath, "w") as f:
            f.write(PROBE_PROMPT)
        limits, mode, deadline, source = council_run._limits(cfg, seat, kind, os.path.join(tmp, "latency.jsonl"),
                                                             len(PROBE_PROMPT))
        launch = adapter.build(seat, ppath, wd, limits, cfg)
        pl = lambda x: _place(x, tmp, ppath)        # noqa: E731
        base = providers._clean_env()
        env = {k: _redact(["env", k], pl(v)) for k, v in sorted(launch.env.items()) if base.get(k) != v}
        effort, cap = providers.budget(seat, pc)
        rec = {
            "argv": [pl(a) for a in launch.argv],
            "env": env,
            "io": {"cwd": pl(launch.cwd), "stdin": None if launch.stdin_path is None else pl(launch.stdin_path),
                   "final_text": None if not launch.final_text_path else pl(launch.final_text_path),
                   "env_unset": sorted(getattr(adapter, "UNSET", ()))},
            "limits": dict(dataclasses.asdict(limits), mode_resolved=mode, deadline_s=deadline,
                           deadline_source=source),
            "policy": {"reasoning_form": _tryv(lambda: providers.reasoning_form(seat, pc)),
                       "reasoning_effort": effort, "max_output_tokens": cap,
                       "reasoning_cap": _tryv(lambda: providers.reasoning_cap(seat, pc, cfg, cap)),
                       "ignores_reasoning_cap": providers.ignores_reasoning_cap(seat, pc),
                       "answer_reserve": _tryv(lambda: providers.answer_reserve(seat, pc, cfg))},
            "files": {},
        }
        ov = launch.env.get("QWEN_CODE_SYSTEM_SETTINGS_PATH")
        if ov:
            try:
                with open(ov, "rb") as f:
                    rec["files"]["qwen_overlay"] = _redact_tree(json.loads(f.read().decode("utf-8")))
            except (OSError, ValueError):
                raise NotCollected("qwen overlay unreadable")
        return _hash_urls(_engine_tree(rec))


def _manifest(cfg, seats, add):
    """manifest.<seat>.<kind>.<part> for every launch the runner can make (see the module docstring)."""
    from . import council_run
    scfg = council_run.salvage_cfg(cfg)
    for seat in seats:
        chair = providers.is_chair(seat)
        base_kind = "chair" if chair else "seat"
        adapter = providers.get(seat["provider"])

        def salvage(seat=seat, chair=chair, adapter=adapter):
            s = adapter.salvage_seat(seat, cfg, scfg["chair_max_output_tokens" if chair else "max_output_tokens"])
            if chair:
                s["id"] = "chair-salvage"           # as council_run.start_salvage does
            return s
        for kind, make in ((base_kind, lambda seat=seat: seat), (SALVAGE_KINDS[base_kind], salvage)) + ((("hedge", lambda seat=seat: seat),) if not chair and seat["provider"] in council_run.hedge_cfg(cfg)["providers"] else ()):
            box = {}

            def rec(make=make, kind=kind, box=box):
                if "e" in box:
                    raise box["e"]
                if "r" not in box:
                    try:
                        box["r"] = _launch_record(cfg, make(), kind)
                    except Exception as e:           # kept: the other parts of this launch fail the same way
                        box["e"] = e
                        raise
                return box["r"]
            parts = ["argv", "env", "io", "limits", "policy"] + (["files"] if seat["provider"] == "qwen" else [])
            for part in parts:
                add("%s.%s.%s" % (seat["id"], kind, part), lambda r=rec, part=part: r()[part], "config")


def _bins(binp, add):
    """-> a function giving the realpath of the binary; adds `bin_sha256`."""
    def real():
        w = _which(binp)
        if not w:
            raise NotCollected("%s not found" % binp)
        return os.path.realpath(w)

    def sha():
        try:
            return _sha256_file(real())
        except OSError:
            raise NotCollected("cannot read %s" % binp)
    add("bin_sha256", sha, "cli")
    return real


def _codex_native(real):
    pkg = os.path.dirname(os.path.dirname(real))
    for pat in (os.path.join(pkg, "node_modules", "@openai", "codex-*", "vendor", "*", "bin", "codex"),
                os.path.join(os.path.dirname(pkg), "codex-*", "vendor", "*", "bin", "codex")):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def _c_overrides(argv):
    """`-c key=value` pairs of a codex argv -> {dotted key: parsed value}. TOML value, else the plain string."""
    out = {}
    for i, a in enumerate(argv):
        if a in ("-c", "--config") and i + 1 < len(argv) and "=" in str(argv[i + 1]):
            k, v = str(argv[i + 1]).split("=", 1)
            try:
                val = tomllib.loads("v = " + v)["v"]
            except (ValueError, KeyError):
                val = v
            out[k.strip()] = val
    return out


def _set_dotted(d, dotted, val):
    parts = dotted.split(".")
    for k in parts[:-1]:
        if not isinstance(d.get(k), dict):
            d[k] = {}
        d = d[k]
    d[parts[-1]] = val


# ---------------------------------------------------------------- code defaults, read from the code
def _num(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _defaults(cfg):
    """-> {dotted config path: value} for every key the config omits and the code defaults. The values come
    from running the code with an empty config; none is copied here. Not derivable (literals inside
    council_run.run): quorum.straggler_grace_s and cut_stream_stragglers, timeouts.adaptive.window_n."""
    from . import council_run, timing
    t = cfg.get("timeouts") or {}
    td, ta, sv = t.get("default") or {}, t.get("adaptive") or {}, cfg.get("salvage") or {}
    out = {}
    with tempfile.TemporaryDirectory(prefix="drift-") as tmp:
        log = os.path.join(tmp, "latency.jsonl")
        lim, _mode, base, _src = council_run._limits({"timeouts": {}, "providers": {}},
                                                     {"provider": "drift", "model": "m"}, "seat", log, 1)
        got = {"idle_s": lim.idle_s, "ceiling_s": lim.ceiling_s, "grace_s": lim.grace_s,
               "max_extensions": lim.max_extensions, "extension_factor": lim.extension_factor,
               "base_timeout_s": base}

        def deadline(n, total, qbytes=100):
            with open(log, "w") as f:
                for _ in range(n):
                    f.write(json.dumps({"kind": "seat", "outcome": "ok", "provider": "drift", "model": "m",
                                        "total_s": total, "prompt_bytes": 100}) + "\n")
            return timing.adaptive_deadline(log, "drift", "m", qbytes, {"timeouts": {}})
        got["min_timeout_s"] = deadline(60, 1)[0]
        adaptive = {}
        for n in range(1, 21):
            if deadline(n, 1)[1] == "p95":
                adaptive["min_samples"] = n
                break
        hi = deadline(60, 200)[0]
        safety = hi / 200.0
        adaptive["safety"] = round(safety, 6)
        if hi < got["ceiling_s"]:
            adaptive["size_exponent"] = round(math.log(deadline(60, 200, 400)[0] / hi) / math.log(4), 6)
    for k, v in got.items():
        if k not in td:
            out["timeouts.default.%s" % k] = v
    for k, v in adaptive.items():
        if k not in ta:
            out["timeouts.adaptive.%s" % k] = v
    for k, v in council_run.salvage_cfg({}).items():
        if k not in sv:
            out["salvage.%s" % k] = v
    if "answer_reserve_tokens" not in cfg:
        out["answer_reserve_tokens"] = providers.DEFAULT_ANSWER_RESERVE
    return out


def serving_provider(sse):
    """The OpenRouter provider that served a call: the `provider` field of the first stream chunk that has one
    (bytes or str of `data: {...}` lines). None when absent. Per-call capture belongs to OpenRouterExtractor
    (row 28); this is the parser it can call."""
    text = sse.decode("utf-8", "replace") if isinstance(sse, (bytes, bytearray)) else str(sse or "")
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            obj = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("provider"), str) and obj["provider"]:
            return obj["provider"]
    return None


COLLECTORS = {"claude": _claude, "codex": _codex, "agy": _agy, "qwen": _qwen, "openrouter": _openrouter}


# ---------------------------------------------------------------- collect
def _seats_by_provider(cfg):
    roster = [dict(s) for s in ((cfg or {}).get("seat") or [])]
    chair = dict((cfg or {}).get("chair") or {})
    chair.update(id="chair", role="chair")
    out = {}
    for s in roster + ([chair] if chair.get("provider") else []):
        p = s.get("provider")
        if p in COLLECTORS:
            out.setdefault(p, []).append(s)
    return out, roster, chair


def _run_collector(fn, provider, cfg, seats):
    items, missing = {}, []

    def add(name, getter, source):
        key = "%s.%s" % (provider, name)
        try:
            items[key] = {"value": _scrub(getter()), "source": source}
        except Exception:                 # NotCollected, or an adapter helper raising on a bad config
            missing.append(key)
    try:
        fn(cfg, seats, add)
    except Exception:
        missing.append(provider)
    return items, missing


def collect(cfg):
    """-> snapshot. Free calls only; one thread per seated provider (plus one for the manifest). Never raises."""
    by_p, roster, chair = _seats_by_provider(cfg)
    items, missing = {}, []
    if by_p:
        jobs = [(COLLECTORS[p], p, s) for p, s in by_p.items()]
        jobs.append((_manifest, "manifest", [x for lst in by_p.values() for x in lst]))
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = [ex.submit(_run_collector, fn, p, cfg, s) for fn, p, s in jobs]
            for f in futs:
                i, m = f.result()
                items.update(i)
                missing += m
        for s in roster:
            if s.get("provider") in COLLECTORS:
                items["roster.%s" % s.get("id")] = {"value": _scrub("%s:%s" % (s.get("provider"), s.get("model"))),
                                                    "source": "config"}
        if chair.get("provider") in COLLECTORS:
            items["roster.chair"] = {"value": _scrub("%s:%s" % (chair.get("provider"), chair.get("model"))),
                                     "source": "config"}
        for k, v in sorted(((cfg or {}).get("pins") or {}).items()):
            items["pins.%s" % k] = {"value": _scrub(v), "source": "config"}
        for p in sorted(by_p):
            items["runner.%s" % p] = {"value": _redact_tree(dict(sorted(_runner(cfg, p).items())), (), True), "source": "config"}
        for path, v in _flatten(cfg or {}):                      # every leaf of the merged config, no allowlist
            items["config.%s" % ".".join(path)] = {"value": _redact_tree(v, path, True), "source": "config"}
        try:
            for k, v in sorted(_defaults(cfg).items()):
                items["defaults.%s" % k] = {"value": "default:%s" % _num(v), "source": "config"}
        except Exception:
            missing.append("defaults")
    return {"schema": SCHEMA, "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "items": dict(sorted(items.items())), "missing": sorted(set(missing))}


# ---------------------------------------------------------------- compare
def _is_missing(key, missing):
    return any(key == m or key.startswith(m + ".") for m in missing or ())


def _volatile_name(k):
    k = str(k)
    return k in VOLATILE_NAMES or k.startswith(VOLATILE_PREFIXES)


def _is_knob(key):
    return key.startswith(KNOB_PREFIXES)


def _mask_str(v):
    v = _LOOPBACK.sub(lambda m: m.group(1) + ":<port>", v)
    return _TAG_ASSIGN.sub(r"\1<tag>", v)


def _norm(v, mask=False, volatile=False):
    """Diff-time canonical form of one value (row 24). Idempotent. Integral floats become ints (3600.0 == 3600);
    bool is never merged with 0/1; keys sorted; with `volatile`, dict fields named like a runtime value or a
    OpenRouter volatile field are dropped; with `mask`, a loopback host:port becomes <port> and the launch tag
    value <tag> (the env entry COUNCIL_SEAT_TAG, a `COUNCIL_SEAT_TAG=x` string, an env key ending PORT)."""
    if isinstance(v, bool) or v is None or isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else v
    if isinstance(v, str):
        return _mask_str(v) if mask else v
    if isinstance(v, (list, tuple)):
        return [_norm(x, mask, volatile) for x in v]
    if isinstance(v, dict):
        out = {}
        for k in sorted(v, key=str):
            if volatile and _volatile_name(k):
                continue
            if mask and (str(k) == netwatch.TAG_ENV or (_PORT_ENV.search(str(k)) and isinstance(v[k], (str, int)))):
                out[str(k)] = "<tag>" if str(k) == netwatch.TAG_ENV else "<port>"
            else:
                out[str(k)] = _norm(v[k], mask, volatile)
        return out
    return v


def canonical_items(items):
    """-> {key: canonical value} with the volatile item keys left out (see VOLATILE_NAMES)."""
    out = {}
    for k, it in (items or {}).items():
        knob = _is_knob(k)
        if not knob and _volatile_name(k.rsplit(".", 1)[-1]):
            continue
        val = it.get("value") if isinstance(it, dict) else it
        val = _stored(k, val)              # a baseline written before a redaction rule holds plain values: same hash as now
        out[k] = _norm(val, mask=k.startswith(MASK_STR_PREFIXES), volatile=not knob)
    return out


def diff(old, new):
    """old: last snapshot or None. -> sorted change list (empty = no change). Both sides are canonicalised first
    (canonical_items): numbers, key order, volatile fields, per-launch ports and tags. No allowlist: a key that is
    in only one snapshot is NEW or GONE, whatever its name."""
    if not old:
        return []
    oi, ni, miss = old.get("items") or {}, new.get("items") or {}, new.get("missing") or []
    oc, nc = canonical_items(oi), canonical_items(ni)
    out = []

    def val(k, it):      # the value a change carries (and history.jsonl stores): never a plain codex-config or council-config value
        return _stored(k, it["value"])
    for k in sorted(set(oc) | set(nc)):
        o, n = oi.get(k), ni.get(k)
        if k not in nc:
            kind = "NOT_COLLECTED" if _is_missing(k, miss) else "GONE"
            out.append({"kind": kind, "key": k, "old": val(k, o), "new": None, "source": o.get("source")})
        elif k not in oc:
            out.append({"kind": "NEW", "key": k, "old": None, "new": val(k, n), "source": n.get("source")})
        elif _canon(oc[k]) != _canon(nc[k]):
            out.append({"kind": "CHANGED", "key": k, "old": val(k, o), "new": val(k, n), "source": n.get("source")})
    for m in miss:                       # a never-recorded key that also failed now
        if not any(c["key"] == m for c in out) and m not in oi:
            out.append({"kind": "NOT_COLLECTED", "key": m, "old": None, "new": None, "source": None})
    return out


# ---------------------------------------------------------------- severity (preflight WARN vs INFO)
# `diff` stays exhaustive (the mutation tests rely on it). Preflight raises a WARN only for the changes below that are
# not informational; informational ones are counted in one PASS row and still listed by `council.py drift`, and they
# clear with the same `--accept`. Two kinds of churn carry no action (council perf review 2026-11-11, P9):
#   1. OpenRouter per-endpoint churn (`openrouter.endpoints.*`): providers join, leave and retune a model all week.
#      Still a WARN: `routable_for_budget` becoming empty or uncollected (no endpoint fits the seat budget) and a
#      NEW field name in `unclassified_fields` (a coverage gap). The model-level `openrouter.models.<slug>` stays a WARN.
#   2. A `--config` overlay that replaces the `[[seat]]` roster (cfg["_roster_overlay"], set by config.load), such as a
#      fable seat replacing claude: NEW / GONE of `config.seat.<id>.*`, `manifest.<id>.*`, `roster.<id>`. Without such
#      an overlay a roster change is a WARN. A CHANGED value of a seat in both rosters (model, limits, argv) stays a
#      WARN, and so do CLI versions, binary hashes, codex catalog, pins and agy labels.
ENDPOINT_PREFIX = "openrouter.endpoints."
_ROSTER_KEY = re.compile(r"(?:config\.seat|manifest|roster)\.([^.]+)(?:\.|$)")


def _field_names(v):
    return {n for names in (v.values() if isinstance(v, dict) else ()) for n in (names or ())}


def is_informational(c, roster_overlay=False):
    """True for a change that preflight counts but does not raise as a WARN (see the block comment above)."""
    key, kind = c["key"], c["kind"]
    if key.startswith(ENDPOINT_PREFIX):
        if key.endswith(".routable_for_budget"):
            return kind == "CHANGED" and bool(c.get("new"))
        if key.endswith(".unclassified_fields"):
            return kind == "CHANGED" and _field_names(c.get("new")) <= _field_names(c.get("old"))
        return kind != "NOT_COLLECTED"
    m = _ROSTER_KEY.match(key)
    return roster_overlay and bool(m) and m.group(1) != "chair" and kind in ("NEW", "GONE")


def split_changes(changes, roster_overlay=False):
    """-> (actionable, informational), each in diff order."""
    warn, info = [], []
    for c in changes:
        (info if is_informational(c, roster_overlay) else warn).append(c)
    return warn, info


def change_line(c):
    k = c["kind"]
    src = " [%s]" % c["source"] if c.get("source") else ""
    key = c["key"]
    if k == "CHANGED":
        o, n = _display(key, c["old"]), _display(key, c["new"])
        if SET in (o, n) and key.startswith(CODEX_EFF):
            return "CHANGED %s: %s -> %s (value changed, not shown)%s" % (key, o, n, src)
        return "CHANGED %s: %s -> %s%s" % (key, o, n, src)
    if k == "NEW":
        return "NEW %s = %s%s" % (key, _display(key, c["new"]), src)
    if k == "GONE":
        return "GONE %s (was %s)%s" % (key, _display(key, c["old"]), src)
    return "NOT COLLECTED %s (CLI failed or offline; last value kept: %s)" % (
        key, "none" if c["old"] is None else _display(key, c["old"]))


def report(old, new):
    """-> (text, rc). rc 0: first pass or no change; 1: something changed or could not be collected."""
    n = len(new.get("items") or {})
    if not old:
        lines = ["DRIFT FIRST PASS: no earlier snapshot; %d items collected" % n]
        lines += ["  %s = %s [%s]" % (k, _display(k, v["value"]), v["source"]) for k, v in (new.get("items") or {}).items()]
        lines += ["  NOT COLLECTED %s" % m for m in new.get("missing") or []]
        return "\n".join(lines) + "\n", 0
    since = "%s (%s)" % (old.get("recorded_at") or old.get("collected_at") or "?", old.get("trigger") or "?")
    ch = diff(old, new)
    if not ch:
        return "DRIFT NO CHANGE: %d items match the snapshot of %s\n" % (n, since), 0
    return ("DRIFT CHANGED: %d item(s) since %s\n" % (len(ch), since)
            + "".join("  %s\n" % change_line(c) for c in ch)), 1


# ---------------------------------------------------------------- snapshot files
def drift_dir(cfg):
    return os.path.join((cfg or {}).get("state_dir") or os.path.expanduser("~/.claude-council"), DRIFT_DIR)


def load_last(cfg):
    try:
        with open(os.path.join(drift_dir(cfg), LAST), "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("items"), dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, obj, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(obj, indent=1, sort_keys=True) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _append_history(d, line):
    hp = os.path.join(d, HISTORY)
    fd = os.open(hp, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(line, sort_keys=True) + "\n")
    os.chmod(hp, 0o600)


def _ensure_dir(cfg):
    d = drift_dir(cfg)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _match_keys(changes, patterns):
    """-> (matched changes, patterns that matched nothing). A pattern is a key, or a key with `*` wildcards.
    NOT_COLLECTED rows are never matched: there is no new value to accept."""
    acceptable = [c for c in changes if c["kind"] != "NOT_COLLECTED"]
    picked, unmatched = {}, []
    for pat in patterns:
        rx = re.compile("".join(".*" if ch == "*" else re.escape(ch) for ch in pat))
        hit = [c for c in acceptable if c["key"] == pat or ("*" in pat and rx.fullmatch(c["key"]))]
        if not hit:
            unmatched.append(pat)
        for c in hit:
            picked[c["key"]] = c
    return [picked[k] for k in sorted(picked)], unmatched


def _snapshot(items, missing, trigger):
    return {"schema": SCHEMA, "recorded_at": _stamp(), "trigger": trigger,
            "items": dict(sorted(items.items())), "missing": list(missing or [])}


def observe(cfg, new, trigger, old=None):
    """What a preflight does (D19, sticky alarms): append one observation to history.jsonl and NEVER move an
    existing baseline, so an alarm stays raised until `accept`. With no baseline at all (`old` falsy) there is
    nothing to protect, so the first observation seeds last.json. -> path of last.json."""
    d = _ensure_dir(cfg)
    path = os.path.join(d, LAST)
    seed = not old
    if seed:
        snap = _snapshot(new.get("items") or {}, new.get("missing"), trigger)
        _write_json(path, snap)
    _append_history(d, {"recorded_at": _stamp(), "trigger": trigger, "kind": "seed" if seed else "observe",
                        "changes": diff(old, new), "first_pass": seed, "baseline_moved": seed,
                        "items": dict(sorted((new.get("items") or {}).items())),
                        "missing": list(new.get("missing") or [])})
    return path


record = observe        # the old name: it no longer moves a baseline (use `accept`)


def accept(cfg, new, trigger, old=None, keys=None):
    """The only call that moves last.json once it exists. keys=None accepts every CHANGED / NEW / GONE item;
    a list accepts just those keys (exact, or with `*`). The RAW values are stored, since canonicalisation lives
    in `diff`. A key not collected this pass keeps its last value.
    -> (path of last.json, accepted change list). Raises ValueError (nothing written) if a key matches no change."""
    changes = diff(old, new) if old else []
    if keys:
        picked, unmatched = _match_keys(changes, keys)
        if unmatched:
            raise ValueError("no pending change matches: %s" % ", ".join(unmatched))
        items = dict((old or {}).get("items") or {})
        ni = new.get("items") or {}
        for c in picked:
            if c["kind"] == "GONE":
                items.pop(c["key"], None)
            else:
                items[c["key"]] = ni[c["key"]]
        missing = (old or {}).get("missing") or []
    else:
        picked = [c for c in changes if c["kind"] != "NOT_COLLECTED"]
        items = dict(new.get("items") or {})
        for k, v in ((old or {}).get("items") or {}).items():
            if k not in items and _is_missing(k, new.get("missing")):
                items[k] = v
        missing = new.get("missing") or []
    d = _ensure_dir(cfg)
    snap = _snapshot(items, missing, trigger)
    path = os.path.join(d, LAST)
    _write_json(path, snap)
    _append_history(d, {"recorded_at": snap["recorded_at"], "trigger": trigger, "kind": "accept",
                        "accepted": [c["key"] for c in picked], "changes": changes, "first_pass": not old,
                        "baseline_moved": True, "items": snap["items"], "missing": snap["missing"]})
    return path, picked
