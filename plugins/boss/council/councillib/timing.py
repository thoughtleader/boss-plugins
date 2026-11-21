"""Latency log (append-only JSONL) and the adaptive deadline for buffered seats."""
from __future__ import annotations

import json
import math
import os
import time

LOG_KEYS = ("ts", "run_id", "kind", "provider", "model", "mode", "prompt_bytes", "out_bytes",
            "first_byte_s", "max_gap_s", "total_s", "outcome", "extensions", "deadline_s",
            "deadline_source", "grounded")
_TAIL_BYTES = 2 * 1024 * 1024
_MAX_STR = 200


def append(log_path, record):
    """Append one line holding only LOG_KEYS with scalar values (never argv, env or prompt text)."""
    rec = {}
    for k in LOG_KEYS:
        v = record.get(k)
        if v is None and k != "ts":
            continue
        if isinstance(v, str):
            v = v[:_MAX_STR]
        elif not isinstance(v, (int, float, bool)) and v is not None:
            continue
        rec[k] = v
    if rec.get("ts") is None:
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    parent = os.path.dirname(os.path.abspath(log_path))
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
    data = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)           # one write on an O_APPEND fd: lines don't interleave
    finally:
        os.close(fd)


def _read_lines(log_path):
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            raw = f.read()
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def _p95(vals):
    s = sorted(vals)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]    # nearest rank


def _median(vals):
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def ceiling_of(cfg, provider, chair=False):
    """The runner ceiling of one launch -> seconds, or None for no ceiling (row 35, D27).

    `cfg` is the full config or just its `[timeouts]` table (as in adaptive_deadline). The table is
    [timeouts.default] with [timeouts.<provider>] on top, then [timeouts.chair] when `chair` is true.
    `ceiling_s = 0` means NO ceiling: TOML has no null, and a deleted key would inherit the default
    (7200 s). It is valid only for a hedged buffered seat (owner ruling 1; `providers.hedge_problems`
    FAILs it anywhere else). A negative or non-numeric value raises ValueError. Every ceiling reader
    goes through here: council_run._limits, providers._max_ceiling, adaptive_deadline, preflight."""
    t = (cfg or {}).get("timeouts", cfg) if isinstance(cfg, dict) else {}
    t = t if isinstance(t, dict) else {}
    d = dict(t.get("default") or {})
    d.update({k: v for k, v in (t.get(provider) or {}).items() if k != "adaptive"})
    if chair:
        d.update(t.get("chair") or {})
    v = d.get("ceiling_s", 1200)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ValueError("ceiling_s must be 0 (no ceiling) or a number > 0, got %r (%s)" % (v, provider))
    return None if v == 0 else float(v)


def adaptive_deadline(log_path, provider, model, prompt_bytes, cfg, grounded=False):
    """Buffered-seat deadline -> (seconds | None, "p95" | "cold").

    `cfg` is the full config (or just its `[timeouts]` table). Samples are the last
    `window_n` seat lines with outcome "ok" for this provider; this model's own
    lines are used when there are at least `min_samples` of them. Canary, chair and
    test lines never count. Grounded calls (tool use, council.py --grounded) are timed apart: `grounded` picks only
    lines tagged grounded, otherwise only untagged ones, so a seat that reads files never gets the plain seats' p95
    (and does not stretch theirs); too few of its own samples is "cold". p95 is nearest-rank over total_s:
    clamp(p95 * max(1, pb/ref) ** size_exponent * safety, min_timeout_s, ceiling_s).
    Fewer than `min_samples` -> (base_timeout_s, "cold"); when the provider's table sets
    `cold_at_ceiling = true` (owner ruling 2026-10-21: codex, agy) a cold seat starts at
    `ceiling_s` instead, so a first run cannot be killed in the silent phase before a
    sample exists. Warm p95 deadlines are unchanged.
    No ceiling (`ceiling_of` is None, row 35): the warm deadline is not clamped from above, and a cold seat
    with `cold_at_ceiling` gets None (no deadline). council_run._limits never asks for a buffered deadline
    of a seat with no ceiling (it runs in watch mode); this only keeps direct callers from a kill at 0 s.
    """
    t = cfg.get("timeouts", cfg)
    d = dict(t.get("default", {}))
    d.update({k: v for k, v in (t.get(provider) or {}).items() if k != "adaptive"})
    a = t.get("adaptive", {})
    window_n = int(a.get("window_n", 50))
    min_samples = int(a.get("min_samples", 5))
    exponent = float(a.get("size_exponent", 0.5))
    safety = float(a.get("safety", 1.5))
    base = float(d.get("base_timeout_s", 600))
    lo = float(d.get("min_timeout_s", 120))
    hi = ceiling_of(t, provider)

    rows = [r for r in _read_lines(log_path)
            if r.get("kind") == "seat" and r.get("outcome") == "ok"
            and r.get("provider") == provider and _num(r.get("total_s"))
            and bool(r.get("grounded")) == bool(grounded)]
    rows = rows[-window_n:] if window_n > 0 else rows
    mine = [r for r in rows if r.get("model") == model]
    use = mine if len(mine) >= min_samples else rows
    if len(use) < min_samples:
        return ((hi if d.get("cold_at_ceiling") is True else base), "cold")

    p95 = _p95([float(r["total_s"]) for r in use])
    sizes = [float(r["prompt_bytes"]) for r in use if _num(r.get("prompt_bytes")) and r["prompt_bytes"] > 0]
    ref = _median(sizes) if sizes else 0.0
    ratio = (prompt_bytes / ref) if (ref > 0 and prompt_bytes) else 1.0
    size = max(1.0, ratio) ** exponent
    dl = max(lo, p95 * size * safety)
    return ((dl if hi is None else min(hi, dl)), "p95")
