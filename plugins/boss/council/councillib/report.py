"""The ready-to-relay report block of `council.py ask` (P27): what the council skill's step 5 had the calling chat build
by hand from files (the per-seat table, the cost, calls and phase lines, the critique and quorum lines, the flag
meanings, the preflight notices, the synthesis head, the leaked-key scan). Read-only: files of one run dir.

`block(run_dir, checks=None, allow_missing=(), scan=None) -> str`. The seat table is the same table as the jq script in
`skills/council/reference.md` (kept for a hand re-read of a later summary.json; tests/test_ask.py checks they agree).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess

CAP_MARK = "hit its output-token cap"
SYNTH_HEAD_LINES = 15
SCAN_TIMEOUT_S = 60
SCAN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "secret-scan.sh")

COLUMNS = ("kind", "seat", "provider", "model", "status", "verdict", "grounding", "total_s", "truncated", "out_tok",
           "reasoning_tok", "cost_usd", "flags")

# every flag the table can show -> the one line the reply must say about it (only the flags present are printed)
FLAGS = {
    "PENDING": "no answer when the chair started; its calls keep running and are not killed; a later answer is LATE",
    "FAILED": "status not ok (the status column says which); the seat did not answer",
    "UNRESOLVED": "every call of the seat ended with no answer, its one fallback included",
    "BY_HEDGE": "the answer came from the seat's parallel hedge call, not the original",
    "BY_FALLBACK": "the answer came from the seat's one fallback call (OpenRouter: the rerouted retry), not the original",
    "BY_STANDIN": "the answer came from the seat's stand-in call, not the original",
    "LATE": "a call answered after the chair started: kept in seats/<id>.<call>.late.md, not used by the chair",
    "TRUNCATED": "the output was cut (cut_after_quorum or a kill); the partial is kept",
    "UNVERIFIED_CITES": "a path:line citation or quote did not hold against the files; the answer is kept and the chair "
                        "was told which claims failed",
    "SALVAGED": "the answer is from the one short follow-up call after the first run ended without one (weaker)",
    "SUBSTITUTED": "the model column is the stand-in model that really answered (summary.json substituted.reason)",
    "CAPPED": "answered, but hit its output-token cap (marked [TRUNCATED: ...] in seats/<id>.md)",
}


def _get(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _alt(v, default):
    """jq's `//`: null and false take the default."""
    return default if v is None or v is False else v


def _jq_str(v, computed=False):
    """jq's tostring for the values the table shows. jq 1.7 keeps a number read from the file as written (0.0 stays
    0.0); a computed number prints in jq's shortest form (12.0 -> 12)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if computed and isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def _secs(v):
    if v is None:
        return "-"
    return _jq_str(round(v * 10) / 10, computed=True)


def _grounding(r):
    g = r.get("grounding")
    if not g:
        return "-"
    if g.get("mode") == "ungrounded":
        return "ungrounded"
    chk = g.get("check")
    if chk:
        n = chk.get("n_citations") or 0
        return "grounded %s/%s" % (_jq_str(n - (chk.get("n_bad") or 0), computed=True), _jq_str(n))
    return "grounded"


def _flags(r, capped):
    out = []
    status = r.get("status")
    if status == "pending":
        out.append("PENDING")
    elif status != "ok":
        out.append("FAILED")
    if status == "unresolved":
        out.append("UNRESOLVED")
    by = _alt(r.get("answered_by"), "original")
    if by != "original":
        out.append("BY_" + str(by).upper())
    if len(_alt(r.get("late"), [])) > 0:
        out.append("LATE")
    if r.get("truncated"):
        out.append("TRUNCATED")
    if r.get("grounded_ok") is False:
        out.append("UNVERIFIED_CITES")
    if r.get("salvaged"):
        out.append("SALVAGED")
    if r.get("substituted"):
        out.append("SUBSTITUTED")
    if r.get("detail") == "output_cap" or (_alt(r.get("id"), "") in capped):
        out.append("CAPPED")
    return out


def _row(r, kind, capped):
    usage = r.get("usage") or {}
    model = _alt(_get(r, "substituted", "model"), r.get("model"))
    flags = _flags(r, capped)
    return [kind, _jq_str(r.get("id")), _jq_str(r.get("provider")), _jq_str(model), _jq_str(r.get("status")),
            _jq_str(_alt(r.get("verdict"), "-")), _grounding(r), _secs(r.get("total_s")),
            _jq_str(_alt(r.get("truncated"), False)), _jq_str(_alt(usage.get("output_tokens"), "-")),
            _jq_str(_alt(usage.get("reasoning_tokens"), "-")), _jq_str(_alt(usage.get("cost_usd"), "-")),
            ",".join(flags) or "-"]


def _salvage_row(r, capped):
    sv = r.get("salvage") or {}
    return _row({"id": r.get("id"), "provider": r.get("provider"), "model": sv.get("model"), "status": sv.get("status"),
                 "total_s": sv.get("total_s"), "truncated": sv.get("truncated"), "usage": sv.get("usage"),
                 "detail": sv.get("detail")}, "salvage", capped)


def capped_ids(run_dir):
    out = set()
    for p in glob.glob(os.path.join(run_dir, "seats", "*.md")):
        try:
            with open(p, "rb") as f:
                if CAP_MARK.encode() in f.read():
                    out.add(os.path.basename(p)[:-3])
        except OSError:
            pass
    return out


def table_rows(summary, capped=()):
    rows = [list(COLUMNS)]
    for s in summary.get("seats") or []:
        rows.append(_row(s, "advisor", capped))
        if s.get("salvage"):
            rows.append(_salvage_row(s, capped))
    if summary.get("chair"):
        ch = dict(summary["chair"], id="chair")
        rows.append(_row(ch, "chair", capped))
        if ch.get("salvage"):
            rows.append(_salvage_row(ch, capped))
    return rows


def seat_table(summary, capped=()):
    """The per-seat table, aligned like `column -t` (two spaces between columns)."""
    rows = table_rows(summary, capped)
    widths = [max(len(r[i]) for r in rows) for i in range(len(COLUMNS))]
    return "".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() + "\n" for r in rows)


def _compact(summary, keys):
    return json.dumps({k: summary[k] for k in keys if summary.get(k) is not None}, separators=(",", ":"))


def calls_lines(summary):
    out = []
    for s in summary.get("seats") or []:
        if not (s.get("calls") or s.get("progress")):
            continue
        calls = " ".join("%s=%s/%s" % (c.get("call"), c.get("status"), c.get("role")) for c in s.get("calls") or [])
        line = "%s: outcome %s, answered_by %s, calls %s" % (s.get("id"), _alt(s.get("outcome"), s.get("status")),
                                                              _alt(s.get("answered_by"), "-"), calls)
        pr = s.get("progress")
        if pr:
            line += ", progress %s last %s s, %s events" % (pr.get("signal"), _jq_str(_alt(pr.get("last_progress_s"), "-")),
                                                          _jq_str(_alt(pr.get("events"), "-")))
        out.append(line + "\n")
    return out


def notices(checks, allow_missing=()):
    """The preflight rows the reply must name: every WARN (drift, orphans, long watch calls, stand-ins, ungrounded seats,
    question paths, private files) and every allowed-missing FAIL."""
    allowed = set(allow_missing or ())
    out = []
    for c in checks or ():
        if c.result == "WARN" or (c.result == "FAIL" and c.provider in allowed):
            tag = "WARN" if c.result == "WARN" else "FAIL (allowed missing)"
            out.append("  %s %s/%s: %s\n" % (tag, c.provider, c.check, " ".join(str(c.detail).split())[:240]))
    return out


def scan_line(run_dir, script=SCAN):
    """Rule 6's leaked-key check on the run dir: one line, file names only (the script never prints a key or a matching
    line). Never changes the run's exit code."""
    if not os.path.isfile(script):
        return "leaked-key scan: not run (no %s)\n" % script
    try:
        p = subprocess.run(["bash", script, run_dir], capture_output=True, text=True, timeout=SCAN_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return "leaked-key scan: not run (bash not found)\n"
    except subprocess.TimeoutExpired:
        return "leaked-key scan: not run (timed out after %d s)\n" % SCAN_TIMEOUT_S
    except OSError as e:
        return "leaked-key scan: not run (%s)\n" % type(e).__name__
    if p.returncode == 0:
        return "leaked-key scan: clean\n"
    if p.returncode == 1:
        names = sorted({os.path.relpath(l, run_dir) for l in p.stdout.splitlines() if l.strip()})
        return "leaked-key scan: key-shaped strings in %s (file names only; open them by hand)\n" % ", ".join(names)
    return "leaked-key scan: not run (script exit %d)\n" % p.returncode


def block(run_dir, checks=None, allow_missing=(), scan=None):
    """The report block. `scan` = scan_line's text (None: run it here)."""
    out = ["== council report (relay this; summary.json is a snapshot: late answers come later) ==\n",
           "run dir: %s\n" % run_dir]
    try:
        with open(os.path.join(run_dir, "summary.json")) as f:
            summary = json.load(f)
    except (OSError, ValueError):
        summary = None
    if summary is None:
        out.append("no summary.json: see %s\n" % os.path.join(run_dir, "runner.log"))
    else:
        capped = capped_ids(run_dir)
        out.append("seats:\n" + seat_table(summary, capped))
        out.append("cost: %s\n" % _compact(summary, ("cost_reported_usd", "cost_unreported", "verdicts", "grounded")))
        cl = calls_lines(summary)
        if cl:
            out.append("calls:\n" + "".join("  " + l for l in cl))
        out.append("phase: %s\n" % _compact(summary, ("phase", "runner_pid", "below_quorum", "pending", "late",
                                                       "late_pending", "late_synthesis", "unavailable")))
        if summary.get("critique") is not None:
            out.append("critique: %s\n" % json.dumps(summary["critique"], separators=(",", ":")))
        out.append("quorum: %s\n" % json.dumps(summary.get("quorum"), separators=(",", ":")))
        present = {f for r in table_rows(summary, capped)[1:] for f in r[-1].split(",") if f in FLAGS}
        if present:
            out.append("flags (say each; a council with flagged seats is weaker than its seat count):\n"
                       + "".join("  %s: %s\n" % (f, FLAGS[f]) for f in FLAGS if f in present))
    nt = notices(checks, allow_missing)
    if nt:
        out.append("preflight notices (name each in the reply; none stopped the run):\n" + "".join(nt))
    synth = os.path.join(run_dir, "chair", "synthesis.md")
    try:
        with open(synth, encoding="utf-8", errors="replace") as f:
            head = [next(f, None) for _ in range(SYNTH_HEAD_LINES)]
        head = [h for h in head if h is not None]
        out.append("synthesis: %s (first %d lines; read the rest of it)\n" % (synth, len(head)))
        out.append("".join(head).rstrip("\n") + "\n")
    except OSError:
        out.append("synthesis: none yet (%s); the seats' answers are in %s\n" % (synth, os.path.join(run_dir, "seats")))
    out.append(scan if scan is not None else scan_line(run_dir))
    out.append("== end of report ==\n")
    return "".join(out)
