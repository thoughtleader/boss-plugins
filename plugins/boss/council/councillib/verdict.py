"""Per-seat verdict line and seat lenses (run example-run, row 12).

Display only. A verdict never gates anything: no quorum rule reads it, no run stops on all-REVISE. The advisor
prompt asks each seat to end with `Verdict: APPROVE | REVISE | BLOCK` (or `Verdict: n/a` when the question is open
and nothing can be approved) next to `Confidence:`. `parse(text)` reads it tolerantly: a missing, garbled or
unknown line is `None` and never fails a seat.

Lenses: an optional one-line angle per seat (security, product, architecture, ...). Precedence, highest first:
`--lens SEAT=TEXT` (per run), the seat's own `lens = "..."` key, `[lenses.seat]` <seat id> in config.toml. The master
switch is `[lenses] enabled`, or `--lenses off|on` per run. The runner-only keys `_lenses` and `_lens_overrides` on
cfg carry the per-run values (underscore keys are skipped by drift, so a per-run flag never raises a drift row).
Each seat gets only its own lens: no seat sees another seat's lens or answer.
"""
from __future__ import annotations

import re

VERDICTS = ("APPROVE", "REVISE", "BLOCK")
NA = "n/a"
# Last match wins (the line is asked for at the end). Tolerates markdown emphasis and backticks around the line.
_LINE = re.compile(r"^[ \t>*_`#-]*verdict[ \t*_`]*[:=][ \t*_`]*(approve|revise|block|n\s*/\s*a|na|none)\b",
                   re.I | re.M)
MAX_LENS_CHARS = 200


def parse(text):
    """'APPROVE' | 'REVISE' | 'BLOCK' | 'n/a' | None (no recognisable verdict line)."""
    if not text:
        return None
    found = _LINE.findall(str(text))
    if not found:
        return None
    word = re.sub(r"\s+", "", found[-1]).lower()
    return word.upper() if word.upper() in VERDICTS else NA


def tally(verdicts):
    """{'APPROVE': n, 'REVISE': n, 'BLOCK': n, 'n/a': n, 'missing': n} over an iterable of parse() results."""
    out = dict.fromkeys(VERDICTS + (NA, "missing"), 0)
    for v in verdicts:
        out[v if v in out else "missing"] += 1
    return out


def lens_for(cfg, seat):
    """The lens text for `seat` under `cfg`, or '' (off, or none configured). One short line."""
    cfg = cfg or {}
    mode = cfg.get("_lenses")
    lcfg = cfg.get("lenses") or {}
    if mode == "off" or (mode != "on" and not lcfg.get("enabled", False)):
        return ""
    sid = seat.get("id")
    text = (cfg.get("_lens_overrides") or {}).get(sid) or seat.get("lens") or (lcfg.get("seat") or {}).get(sid) or ""
    return " ".join(str(text).split())[:MAX_LENS_CHARS]


def lens_line(lens):
    """The one line injected into the advisor prompt ('' when the seat has no lens)."""
    if not lens:
        return ""
    return ("Your lens: %s. Weigh the question mainly from that angle, but still give your complete, own answer "
            "and state anything outside the lens that changes it.\n\n" % lens)


def problems(cfg):
    """FAIL reasons for a bad [lenses] table or seat `lens` key (preflight-style, free)."""
    out = []
    lcfg = (cfg or {}).get("lenses")
    if lcfg is not None:
        if not isinstance(lcfg, dict):
            return ["[lenses] must be a table"]
        if not isinstance(lcfg.get("enabled", True), bool):
            out.append("lenses.enabled must be true or false")
        seats = lcfg.get("seat", {})
        if not isinstance(seats, dict) or any(not isinstance(v, str) for v in seats.values()):
            out.append("[lenses.seat] must map seat ids to text")
    for s in (cfg or {}).get("seat") or ():
        if "lens" in s and not isinstance(s["lens"], str):
            out.append("seat %s: lens must be text" % s.get("id"))
    return out


def parse_overrides(items):
    """`--lens SEAT=TEXT` (repeatable) -> ({seat id: text}, [error text])."""
    out, errs = {}, []
    for it in items or ():
        sid, sep, text = str(it).partition("=")
        if not sep or not sid.strip() or not text.strip():
            errs.append("--lens %r: expected SEAT=TEXT" % (it,))
        else:
            out[sid.strip()] = text.strip()
    return out, errs
