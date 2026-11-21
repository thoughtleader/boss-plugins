"""Config loading, overlays and the roster policy check (runs before any spend)."""
from __future__ import annotations

import copy
import os
import re
import sys

if sys.version_info < (3, 11):      # before the first tomllib import (3.11+): a clear line, not a ModuleNotFoundError
    sys.stderr.write("council needs Python 3.11+ (found %d.%d.%d); install it and run it with python3.11\n"
                     % tuple(sys.version_info[:3]))
    sys.exit(2)

import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                                   # the council dir (council.py, prompts/, config.default.toml)
SHIPPED_CONFIG = os.path.join(ROOT, "config.default.toml")     # the config that ships with the plugin
CONFIG_ENV = "COUNCIL_CONFIG"                                  # explicit base config path (wins over everything)


def user_config_path():
    """`<Claude config dir>/council/config.toml`: CLAUDE_CONFIG_DIR, else ~/.claude."""
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(os.path.expanduser(root), "council", "config.toml")


def resolve_base():
    """The base config file and where it came from: (path, source).

    1. $COUNCIL_CONFIG (an explicit path; a missing file is an error, never a silent fallback),
    2. <Claude config dir>/council/config.toml when it exists (your own roster and providers),
    3. config.default.toml shipped next to council.py.
    """
    env = os.environ.get(CONFIG_ENV)
    if env:
        path = os.path.expanduser(env)
        if not os.path.isfile(path):
            raise FileNotFoundError(2, "%s points at a missing file" % CONFIG_ENV, path)
        return path, "$" + CONFIG_ENV
    path = user_config_path()
    if os.path.isfile(path):
        return path, "user config"
    return SHIPPED_CONFIG, "shipped default"


def default_config():
    """Path of the base config `load()` uses when no `base` is given."""
    return resolve_base()[0]


def prompts_dir(cfg):
    """The prompts directory: `prompts_dir` from the config when it is a directory, else the shipped council/prompts."""
    d = (cfg or {}).get("prompts_dir")
    if d:
        d = os.path.expanduser(d)
        if os.path.isdir(d):
            return d
    return os.path.join(ROOT, "prompts")
_MIN_OK_WORDS = ("n-1", "n", "all")


def deep_merge(base, over):
    """Dicts merge key by key; every other value (lists included) is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = deep_merge(base[k], v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def _expand(v):
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, str) and (v == "~" or v.startswith("~/")):
        return os.path.expanduser(v)
    return v


def load_file(path):
    with open(path, "rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            e.council_path = os.path.abspath(path)      # so the CLI can say which file (and tomllib says line and column)
            raise


def load(overlays=(), base=None):
    """Base config (see resolve_base) + each overlay (a path or a dict), deep-merged in order.

    `[[seat]]` (a list) in an overlay replaces the base roster. Strings that
    start with `~` are expanded. Returns a plain dict; `cfg["seat"]` is the roster.
    """
    cfg = load_file(base or default_config())
    roster_overlay = False
    for ov in overlays or ():
        ov = ov if isinstance(ov, dict) else load_file(ov)
        roster_overlay = roster_overlay or "seat" in ov
        cfg = deep_merge(cfg, ov)
    cfg = _expand(cfg)
    cfg.setdefault("seat", [])
    if roster_overlay:
        cfg["_roster_overlay"] = True           # runtime flag (skipped by drift): an overlay replaced the roster
    return cfg


AGY_NO_UPDATE_ENV = "AGY_CLI_DISABLE_AUTO_UPDATE"


def apply_cli_env(cfg):
    """`[providers.agy] disable_auto_update = true` sets agy's self-update switch in this process's environment,
    so every agy child (seat launches, preflight and drift captures) inherits it and none can update agy behind
    the baseline. agy reads the variable as the exact string "true" (1.4.5: any other value is ignored). Returns
    the {name: value} it set, empty when the key is absent or not the boolean true (the ambient value is left alone)."""
    pc = ((cfg or {}).get("providers") or {}).get("agy") or {}
    if pc.get("disable_auto_update") is True:
        os.environ[AGY_NO_UPDATE_ENV] = "true"
        return {AGY_NO_UPDATE_ENV: "true"}
    return {}


def resolve_min_ok(spec, n):
    """`min_ok` ("n-1" | "n" | "all" | int) -> a count for n launched seats."""
    if isinstance(spec, bool):
        raise ValueError("min_ok must be 'n-1', 'n', 'all' or an integer")
    if isinstance(spec, int):
        return spec
    if isinstance(spec, str):
        s = spec.strip().lower()
        if s == "n-1":
            return max(0, n - 1)
        if s in ("n", "all"):
            return n
        if re.fullmatch(r"\d+", s):
            return int(s)
    raise ValueError("min_ok must be 'n-1', 'n', 'all' or an integer, got %r" % (spec,))


def validate_roster(cfg):
    """Return a list of problems; empty means the roster is allowed to run."""
    from . import providers   # late import: providers imports nothing from here

    errs = []
    seats = cfg.get("seat") or []
    if not seats:
        errs.append("roster is empty: no [[seat]] entries")

    seen = set()
    for i, seat in enumerate(seats):
        sid = seat.get("id")
        label = "seat #%d" % (i + 1) if not sid else "seat %r" % sid
        if not sid:
            errs.append("%s has no id" % label)
        elif sid in seen:
            errs.append("duplicate seat id %r" % sid)
        else:
            seen.add(sid)
        prov = seat.get("provider")
        if not prov or not providers.known(prov):
            errs.append("%s: unknown provider %r" % (label, prov))
        if not seat.get("model"):
            errs.append("%s has no model" % label)

    chair = cfg.get("chair") or {}
    if not providers.known(chair.get("provider")):
        errs.append("chair: unknown provider %r" % (chair.get("provider"),))

    cap = (cfg.get("policy") or {}).get("max_anthropic_advisors", 1)
    if isinstance(cap, bool) or cap not in (0, 1):
        errs.append("policy.max_anthropic_advisors must be 0 or 1, got %r" % (cap,))
    else:
        anth = [s.get("id") for s in seats if providers.is_anthropic(s)]
        if len(anth) > cap:
            errs.append(
                "%d Anthropic advisor seats (%s) but max_anthropic_advisors=%d; "
                "the chair is not counted" % (len(anth), ", ".join(map(str, anth)), cap))

    from . import verdict            # late import, like providers above
    errs.extend(verdict.problems(cfg))
    from . import grounded           # late import, like verdict above
    errs.extend(grounded.problems(cfg))

    try:
        resolve_min_ok((cfg.get("quorum") or {}).get("min_ok", "n-1"), max(1, len(seats)))
    except ValueError as e:
        errs.append("quorum: %s" % e)
    return errs
