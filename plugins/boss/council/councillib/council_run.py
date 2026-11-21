"""Council scheduler: parallel seats, early quorum, chair synthesis, run outputs.

`run(cfg, question, context_files, out_dir, allow_missing, preflight_result, on_ready=None, parent_pid=None) -> int`

Exit codes: 0 done, 2 bad input (missing template), 3 quorum not met, 4 chair failed, or (D50) no answer yet
with at least one pending seat (`summary.late_synthesis` then says what happened later).

Runner lifetime (row 31, D18/D39/D50): `run()` is the runner. It returns only when every call it started has
reached a terminal state; `on_ready(code, info)` fires ONCE, at the moment the caller's answer is known
(synthesis written, exit 3/4, or D50's "pending: no answers yet"), so a launcher (`council.py run`) can return
then while this process keeps supervising pending / late calls. A late answer is written to
`seats/<id>.<call>.late.{md,raw}` and `seats/<id>.late.md`, listed in `summary.late[]`, and appended to
chair/synthesis.md as a `> LATE` footer; the chair never re-runs. summary.json carries `runner_pid`, `phase`
(seats | chair | supervising | done) and `late_pending[]` (calls still running after the chair started).
`parent_pid` is accepted and ignored: the launcher's death is not a stop (D59 supersedes D18), and neither is a
hang-up (the runner ignores SIGHUP: a closing terminal or chat never reaches its calls). TERM or INT to the runner
is the owner's manual stop (D53): today's kill_all with partials. Watch calls (`[hedge]`/`[fallback]`) run
under a detached reaper (reap.py COUNCIL_REAP_DETACH): they survive the runner's death and are never killed by
the runner's own exit path; nothing of theirs outlives their own terminal state.

Rules this module owns:
- A seat is ok only when its Result is ok AND the adapter reports no error AND the
  final text is non-empty AND the reported model (when there is one) matches the pin.
  Exit 0 with an adapter-reported error (agy can do this) is a failed seat.
- No substitution: a failed seat keeps its own status, nothing is launched in its
  place, and a failed chair is exit 4 with no fallback.
- After quorum, no seat that is still producing output is cut (owner rule: never wall-clock-kill
  a seat still producing; council-hardening row 11 saw a streaming Nemotron cut after quorum).
  Stream seats end by their own idle timeout or ceiling. Only with `[quorum] cut_stream_stragglers
  = true` do the STREAM stragglers get `straggler_grace_s`, then are cut
  (status `cut_after_quorum`, partial kept under a TRUNCATED header). BUFFERED seats
  (mode from `[providers.<p>].mode`, e.g. codex, agy) are never cut by the grace: they
  have no partial, so a cut would throw away paid output. The chair waits for them until
  they finish or hit their own deadline / ceiling (owner ruling 2026-10-21).
- Answer salvage (row 16, `[salvage]`): a seat that ends WITHOUT a usable answer (empty_output,
  cut_after_quorum, idle/deadline/ceiling kill, or an adapter_error / non-zero exit that kept
  some partial) gets ONE follow-up call to the same provider: reasoning off or lowest, a short
  output cap, the original prompt + the seat's own reasoning/partial (front-truncated) + "give your
  final answer now". It runs in parallel with the other seats, is supervised like any seat (idle
  timeout, partials kept), and when it answers the seat becomes ok (detail `salvaged`) and counts
  toward quorum. A seat is salvaged at most once; a failed salvage leaves the seat failed. A seat
  that is ok (even with a truncated answer) is never salvaged. The chair is told.
- Chair salvage (row 17): the chair is salvaged by the same rule, once. The follow-up call carries the
  chair prompt (all seat answers) + the chair's own reasoning/partial (front-truncated) + "Write the
  final synthesis now ...", with `[salvage] chair_max_output_tokens` / `chair_timeout`. A salvaged
  synthesis is marked in chair/synthesis.md and in the chair record (`salvaged`); if the salvage
  fails too, the run is exit 4 with the seat answers kept (chair/chair.raw + chair/salvage.raw hold
  what the two calls produced).
- Every Supervisor call comes from the calling (main) thread.

`preflight_result` is the set of providers that FAILed preflight (a set/list of names,
or a dict provider -> "FAIL"|"PASS"|bool); None means none failed. `allow_missing`
only drops seats of providers that are in that failed set (skipped_allowed_missing).

Run dir layout: question.md, seats/<id>.{md,raw,json}, chair/synthesis.md,
summary.json (row 9 adds preflight.{txt,json}). Dirs are 700 and files 600.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import answer_check as _answer_check
from . import config as _config
from . import liveness as _liveness
from . import netwatch, providers, timing
from . import progress_mirror as _progress
from . import status as _status
from . import critique as _critique
from . import grounded as _grounded
from . import verdict as _verdict
from .supervise import Limits, Notice, Supervisor, WatchLimits, install_signal_handlers

POLL_S = 0.2
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")
_ERR_MAX = 200
SALVAGE_STATUSES = ("empty_output", "invalid_answer", "cut_after_quorum", "idle_kill", "deadline_kill", "ceiling_kill")
# invalid_answer (fallback-chain row 2, `[answer_check]`): an "ok" text that is tool-call markup or near-empty, not an answer
SALVAGE_NEEDS_PARTIAL = ("adapter_error", "cli_timeout")   # and any exit_<n>: a CLI failure with nothing kept is not a stalled seat
# cli_timeout: agy's print-timeout expiry (row 28), shown as adapter_error before row 30; same salvage rule as then
NO_RETRY_STATUSES = ("quota_exhausted",)          # D47: no salvage, hedge or fallback against the same quota
SALVAGE_INSTRUCTION = "Give your final answer now, concisely, without further deliberation."
CHAIR_SALVAGE_INSTRUCTION = "Write the final synthesis now, concisely, without further deliberation."


# ---------------------------------------------------------------- small helpers
def _safe(name):
    return _SAFE_ID.sub("_", str(name)) or "_"


def _write(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _failed_providers(preflight_result):
    if not preflight_result:
        return set()
    if isinstance(preflight_result, dict):
        out = set()
        for k, v in preflight_result.items():
            if isinstance(v, str):
                if v.upper().startswith("FAIL"):
                    out.add(k)
            elif v:
                out.add(k)
        return out
    return set(preflight_result)


def _fence(text):
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


RULINGS_LABEL = "settled; raise only if you find a defect"     # P23's label, in the heading the engine writes
RULINGS_HEAD = "Settled rulings (%s)" % RULINGS_LABEL           # in code, not a prompt file (the prompts trap)


def rulings_section(rulings, level="###"):
    """The `--rulings-file` excerpt (cfg `_rulings`, P23) under a heading the engine writes itself, so it reaches every
    prompt whatever `prompts_dir` points at. '' without one."""
    text = ((rulings or {}).get("text") or "").strip("\n")
    if not text.strip():
        return ""
    fence = _fence(text)
    return ("%s %s\nThe owner already decided these points. Treat them as settled: do not reopen one, and raise one only "
            "if you find a defect in it (say which and why).\n%s\n%s\n%s"
            % (level, RULINGS_HEAD, fence, text, fence))


def _context_block(paths, rulings=None):
    sec = rulings_section(rulings)
    parts = [sec] if sec else []
    for p in paths or ():
        with open(p, "rb") as f:
            text = f.read().decode("utf-8", "replace")
        fence = _fence(text)
        parts.append("### %s\n%s\n%s\n%s" % (p, fence, text.rstrip("\n"), fence))
    return "\n\n".join(parts) if parts else "(none)"


def _load_template(cfg, name):
    path = os.path.join(_config.prompts_dir(cfg), name)
    with open(path, "r", encoding="utf-8") as f:
        return string.Template(f.read())


def advisor_prompt(tpl, question, context, seat_id, n_seats, lens=None, grounded=None):
    """One advisor seat's prompt from the advisor.md template (the runner and tools/council-probe.py).
    `lens` (optional, one short line) fills the template's `${lens_line}`; None or '' leaves it empty.
    `grounded` = the run's {root, images} (councillib/grounded.py): the template is then advisor-grounded.md, which
    also takes `${root}`, `${images_line}` and `${access_line}`."""
    extra = _grounded.prompt_vars(grounded) if grounded else {}
    return tpl.substitute(question=question, context=context, seat_id=seat_id, n_seats=n_seats,
                          lens_line=_verdict.lens_line(lens), **extra)


def chair_prompt(tpl, question, answers, critiques=None, changes=None, rulings=None):
    """The chair's prompt from the chair.md template and `_answers(...)` (the runner and the probe tool).
    `critiques` = `_critiques(...)` of a `--depth standard` run: adds the two-round guide, item 11 and the critique block.
    None (quick): those placeholders are empty and the prompt is exactly what it was before the critique round.
    `changes` = `_critique_changes(order)`: item 11 then also tells the chair how many seats changed their view.
    `rulings` = cfg `_rulings` (`--rulings-file`, P23): the chair template has no context slot, so the settled rulings
    are appended in code after the template (the prompts trap); its salvage call resends this whole prompt."""
    text = tpl.substitute(question=question, answers=answers, **_critique.chair_vars(critiques, changes))
    sec = rulings_section(rulings, "##")
    return text.rstrip("\n") + "\n\n" + sec + "\n" if sec else text


def critique_prompt(tpl, question, context, letter, own, peers, n_seats, lens=None, grounded=None):
    """One seat's round 2 prompt from critique.md (or critique-grounded.md): its own round 1 answer, the other answering
    seats' answers as `peers` = [(letter, lens, text)], never a seat id or model."""
    extra = _grounded.prompt_vars(grounded) if grounded else {}
    return tpl.substitute(question=question, context=context,
                          **_critique.prompt_vars(letter, own, peers, n_seats, lens), **extra)


def _extractor(adapter, seat):
    try:
        return adapter.extractor(seat)
    except TypeError:
        return adapter.extractor()


def _adapter(name):
    try:
        return providers.get(name) if providers.known(name) else None
    except (NotImplementedError, KeyError):
        return None


def _short(text):
    return None if text is None else str(text)[:_ERR_MAX]


# ---------------------------------------------------------------- salvage
def salvage_cfg(cfg):
    """[salvage] with defaults. `enabled` is false when the table is absent (config.toml ships true)."""
    sv = (cfg or {}).get("salvage") or {}
    return {"enabled": bool(sv.get("enabled", False)),
            "max_output_tokens": int(sv.get("max_output_tokens", 8000)),
            "timeout": float(sv.get("timeout", 600)),
            "max_partial_chars": int(sv.get("max_partial_chars", 120000)),
            "chair_max_output_tokens": int(sv.get("chair_max_output_tokens", 16000)),
            "chair_timeout": float(sv.get("chair_timeout", 1200))}


def salvage_problems(cfg):
    """FAIL reasons for a bad [salvage] table (preflight)."""
    sv = (cfg or {}).get("salvage")
    if sv is None:
        return []
    out = []
    if not isinstance(sv, dict):
        return ["[salvage] must be a table"]
    if not isinstance(sv.get("enabled", True), bool):
        out.append("salvage.enabled must be true or false")
    for k, lo in (("max_output_tokens", 256), ("max_partial_chars", 1000), ("chair_max_output_tokens", 256)):
        v = sv.get(k, lo)
        if isinstance(v, bool) or not isinstance(v, int) or v < lo:
            out.append("salvage.%s %r must be an integer >= %d" % (k, v, lo))
    for k in ("timeout", "chair_timeout"):
        t = sv.get(k, 600)
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
            out.append("salvage.%s %r must be a positive number of seconds" % (k, t))
    return out


def _salvage_eligible(task, scfg):
    chain_call = (task.kind in ("fallback", "standin") and task.parent is not None
                  and bool(getattr(task.parent, "chain", None)))
    if not scfg["enabled"] or not (task.kind in ("seat", "chair", "critique") or chain_call) or task.salvage_started \
            or task.adapter is None:
        return False
    if task.result is None:
        return False                    # never ran (build_error)
    if chain_call and not (task.partial or "").strip():
        return False                    # a chain call is salvaged only when it left something (paid output, kept once)
    st = task.status
    if st in SALVAGE_STATUSES:
        return True
    if st in SALVAGE_NEEDS_PARTIAL or st.startswith("exit_"):
        return bool((task.partial or "").strip())
    return False


def _salvage_prompt(task, max_chars, partial=None, status=None):
    """Original prompt + the seat's own reasoning/partial (cut from the front to fit) + the instruction.
    An adapter with salvage_resends_reasoning False (claude) gets answer-text partials only: reasoning is dropped.
    `partial` / `status` override the task's own (the D14 fallback sends the longest partial of its calls)."""
    part = ((task.partial if partial is None else partial) or "").strip()
    status = task.status if status is None else status
    label = "partial answer"
    if part.startswith(providers.THINKING_NOTE.strip()):
        part = part[len(providers.THINKING_NOTE.strip()):].strip()
        label = "reasoning"
        if not getattr(task.adapter, "salvage_resends_reasoning", True):
            part = ""               # claude: its own thinking is never sent back (reasoning_extraction refusal)
    cut = ""
    if len(part) > max_chars:
        part = part[-max_chars:]
        cut = "[... the start of it was cut to fit ...]\n"
    chair = task.kind == "chair"
    head = ("%s\n\n---\nSALVAGE: your earlier attempt at the task above ended (%s) without a usable final %s."
            % (task.prompt_text.rstrip(), status, "synthesis" if chair else "critique" if task.kind == "critique" else "answer"))
    if part:
        fence = _fence(part)
        head += (" Your own %s from that attempt follows, between the fences. Use it; do not redo it.\n\n%s\n%s%s\n%s"
                 % (label, fence, cut, part, fence))
    else:
        head += " Nothing from that attempt was kept."
    if status == "invalid_answer":
        head += (" That attempt was not an answer (%s). There are no tools here and nothing can be read from disk: "
                 "answer from the text above only." % ((task.detail or "").split(":")[0] or "invalid"))
    return head + "\n\n" + (CHAIR_SALVAGE_INSTRUCTION if chair else SALVAGE_INSTRUCTION) + "\n"


# ---------------------------------------------------------------- hedge / fallback (row 30)
def hedge_cfg(cfg):
    """[hedge] with the D12 defaults. `enabled` is true only for the boolean true (shipped false, D25)."""
    h = (cfg or {}).get("hedge") or {}
    names = h.get("providers") or []
    return {"enabled": h.get("enabled") is True,
            "providers": [n for n in names if isinstance(n, str)] if isinstance(names, list) else [],
            "factor": float(h.get("factor", 2.5)), "floor_s": float(h.get("floor_s", 900)),
            "cap_s": float(h.get("cap_s", 1800)), "cold_s": float(h.get("cold_s", 1200)),
            "quiet_s": float(h.get("quiet_s", 120)), "recheck_s": float(h.get("recheck_s", 30)),
            "max_defer": float(h.get("max_defer", 2.0))}


def fallback_on(cfg):
    """[fallback] enabled (D40: its own flag, shipped false)."""
    return ((cfg or {}).get("fallback") or {}).get("enabled") is True


def _provider_mode(cfg, provider):
    mode = (cfg.get("providers") or {}).get(provider, {}).get("mode", "buffered")
    return mode if mode in ("stream", "buffered") else "buffered"


def hedged(cfg, provider):
    """True when this provider's seats get a hedge: [hedge] enabled, the provider listed (the gate's hedge_safe
    seats, D26) and buffered (stream seats get salvage + one fallback, D14)."""
    hc = hedge_cfg(cfg)
    return hc["enabled"] and provider in hc["providers"] and _provider_mode(cfg, provider) == "buffered"


def hedge_at(cfg, log_path, provider, model, prompt_bytes, grounded=False):
    """D12 soft deadline -> (seconds, "p95" | "cold"): clamp(p95 x size_factor x factor, floor_s, cap_s) over the
    same latency samples as `timing.adaptive_deadline` (it is called with the [hedge] numbers in place of the
    timeout ones); fewer than min_samples -> cold_s."""
    hc = hedge_cfg(cfg)
    adaptive = dict(((cfg.get("timeouts") or {}).get("adaptive")) or {})
    adaptive["safety"] = hc["factor"]
    table = {"timeouts": {"default": {"min_timeout_s": hc["floor_s"], "ceiling_s": hc["cap_s"],
                                      "base_timeout_s": hc["cold_s"], "cold_at_ceiling": False},
                          "adaptive": adaptive}}
    return timing.adaptive_deadline(log_path, provider, model, prompt_bytes, table, grounded=grounded)


def make_liveness(provider, tag, policy, sources, log_path):
    """One SeatLiveness per watch call (row 28). A seam: tests replace it so no real `ss` runs."""
    return _liveness.SeatLiveness(provider, tag, policy, sources, log_path=log_path)


def _liveness_policy(cfg, provider):
    try:
        return _liveness.LivenessPolicy.from_config(providers._pcfg(cfg, provider), cfg.get("liveness") or {})
    except Exception:
        return _liveness.LivenessPolicy()           # hedge-only, no negative evidence: the safe side


def _append_sync(path, rec):
    """Append one JSON line and fsync it (D17: on disk before the launch it records)."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _write_all(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


# ---- row 40 (D54): the watch-call registry --------------------------------------------------------------
# A watch call outlives its runner (D18). Each one is recorded under <state_dir>/watch-calls/<tag>.json before it
# launches and removed once the runner has seen it end; a record whose runner is gone is an orphan, and
# preflight.sweep_orphans (every council.py start, `council.py reap --orphans`) finds it by its tag.
WATCH_DIR = "watch-calls"


def watch_dir(state_dir):
    return os.path.join(state_dir, WATCH_DIR)


def _ps_field(pid, field):
    """No /proc (macOS, BSD): `ps -o <field>= -p pid` as text, "" when the pid is gone, None when ps cannot run."""
    try:
        r = subprocess.run(["ps", "-o", field + "=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5,
                           env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout.strip() if r.returncode in (0, 1) else None


def proc_start(pid):
    """Start identity of pid, or None: with the pid it identifies a process for good. Clock ticks since boot from
    /proc; without /proc a number made from `ps -o lstart=` (whole seconds), stable for the life of the process."""
    if not os.path.isdir("/proc/self"):
        lstart = _ps_field(pid, "lstart")
        return int(hashlib.sha1(lstart.encode()).hexdigest()[:12], 16) if lstart else None
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        return int(data[data.rfind(b")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def proc_alive(pid, start):
    """True when pid is a live (non-zombie) process with the recorded start time (a reused pid is not it)."""
    if not os.path.isdir("/proc/self"):
        stat = _ps_field(pid, "stat")
        if stat is None:                       # ps cannot run: a pid that answers signal 0 counts as alive, never as dead
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except OSError:
                pass
            return True
        return bool(stat) and not stat.startswith("Z") and (start is None or proc_start(pid) == start)
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        fields = data[data.rfind(b")") + 2:].split()
        return fields[0] != b"Z" and (start is None or int(fields[19]) == start)
    except (OSError, ValueError, IndexError):
        return False


def register_watch_call(state_dir, rec):
    """Write <state_dir>/watch-calls/<tag>.json (tmp + fsync + rename). -> the path, or None when it could not be
    written (the call still runs; it just cannot be swept if the runner dies)."""
    d = watch_dir(state_dir)
    path = os.path.join(d, rec["tag"] + ".json")
    tmp = path + ".%d.tmp" % os.getpid()
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        try:
            _write_all(fd, json.dumps(rec, sort_keys=True, default=str).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, path)
        return path
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None


def unregister_watch_call(state_dir, tag):
    """The runner saw the call end: drop its record and its reaper status file."""
    if not tag:
        return
    for name in (tag + ".json", tag + ".end.json"):
        try:
            os.unlink(os.path.join(watch_dir(state_dir), name))
        except OSError:
            pass


def _read_calls(path):
    """calls.jsonl -> [records]; a torn last line is skipped."""
    out = []
    try:
        with open(path, "rb") as f:
            for line in f:
                try:
                    rec = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


_RESETS = re.compile(r"resets in ((?:\d+[dhms])+)", re.I)


def _reset_iso(detail, now=None):
    """'... (resets in 163h12m5s)' -> UTC ISO time, or None when the text holds no reset time."""
    m = _RESETS.search(detail or "")
    if not m:
        return None
    secs = sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u.lower()]
               for n, u in re.findall(r"(\d+)([dhmsDHMS])", m.group(1)))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now if now is not None else time.time()) + secs))


def _iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- limits
def _limits(cfg, seat, kind, log_path, prompt_bytes):
    """-> (Limits, mode, deadline_s | None, deadline_source).

    Kinds: seat | chair | salvage | chair_salvage | hedge | fallback | chair_fallback (row 30, D29).
    Watch mode (WatchLimits, mode "watch": no deadline, no idle window, no 600 s salvage kill) for a BUFFERED
    provider's hedge and fallback calls, and for its original seat call when the seat is hedged (`hedged()`): then
    soft_at_s = the D12 hedge_at. The ceiling is `timing.ceiling_of()` (row 35, D27): None (no ceiling timer) for a
    seat whose table says `ceiling_s = 0`, else the seat's own. Everything else is e1fd082's: with [hedge] and
    [fallback] off no call gets a watch limit (D25). Stream fallbacks keep the salvage limits (D3)."""
    provider = seat["provider"]
    if kind == "critique":
        kind = "seat"     # round 2 is a full-effort seat call: the same limits as round 1, hedge-style watch mode included
    if seat.get("_full_call"):
        # fallback-chain row 1: a rerouted / stand-in call is a full-effort seat call, not a short salvage-form one, so
        # it gets the seat's own limits (idle clock, ceiling), never the 600 s salvage timeout
        return _no_zero_ceiling(_base_limits(cfg, seat, "seat", log_path, prompt_bytes), provider, "seat")
    if kind in ("hedge", "fallback", "chair_fallback") or (kind == "seat" and hedged(cfg, provider)):
        if _provider_mode(cfg, provider) == "buffered":
            got = _watch_limits(cfg, seat, kind, log_path, prompt_bytes)
            return _no_zero_ceiling(got, provider, kind)
        kind = {"hedge": "seat", "fallback": "salvage", "chair_fallback": "chair_salvage"}.get(kind, kind)
    return _no_zero_ceiling(_base_limits(cfg, seat, kind, log_path, prompt_bytes), provider, kind)


def _no_zero_ceiling(got, provider, kind):
    """A ceiling of 0 s would end the call the moment it starts (supervise: ceiling_at = now + 0). No ceiling is None."""
    c = got[0].ceiling_s
    if c is not None and not c > 0:
        raise ValueError("%s %s: ceiling_s %r; no ceiling is None (timing.ceiling_of)" % (provider, kind, c))
    return got


def _limit_table(cfg, provider, kind):
    t = cfg.get("timeouts") or {}
    d = dict(t.get("default") or {})
    d.update({k: v for k, v in (t.get(provider) or {}).items() if k != "adaptive"})
    if kind in ("chair", "chair_fallback"):
        d.update(t.get("chair") or {})
    return d


def _watch_limits(cfg, seat, kind, log_path, prompt_bytes):
    provider = seat["provider"]
    d = _limit_table(cfg, provider, kind)
    sample = float((cfg.get("liveness") or {}).get("sample_s", 10))
    soft, source = None, "watch"
    if kind == "seat":
        soft, src = hedge_at(cfg, log_path, provider, seat.get("model"), prompt_bytes,
                             grounded=bool(seat.get("_grounded")))
        soft, source = float(soft), "hedge_%s" % src
    ceiling = timing.ceiling_of(cfg, provider, chair=kind in ("chair", "chair_fallback"))     # None: no ceiling (row 35)
    lim = WatchLimits(mode="watch", idle_s=float(d.get("idle_s", 240)), ceiling_s=ceiling,
                      deadline_s=None, grace_s=float(d.get("grace_s", 5)),
                      max_extensions=int(d.get("max_extensions", 1)),
                      extension_factor=float(d.get("extension_factor", 1.5)),
                      soft_at_s=soft, sample_s=sample if sample > 0 else None)
    return lim, "watch", None, source


def _base_limits(cfg, seat, kind, log_path, prompt_bytes):
    provider = seat["provider"]
    t = cfg.get("timeouts") or {}
    d = dict(t.get("default") or {})
    d.update({k: v for k, v in (t.get(provider) or {}).items() if k != "adaptive"})
    if kind == "chair":
        d.update(t.get("chair") or {})
    mode = (cfg.get("providers") or {}).get(provider, {}).get("mode", "buffered")
    if mode not in ("stream", "buffered"):
        mode = "buffered"
    ceiling = timing.ceiling_of(cfg, provider, chair=kind == "chair")           # None: no ceiling (row 35, D27)
    common = dict(idle_s=float(d.get("idle_s", 240)), ceiling_s=ceiling,
                  grace_s=float(d.get("grace_s", 5)),
                  max_extensions=int(d.get("max_extensions", 1)),
                  extension_factor=float(d.get("extension_factor", 1.5)))
    if kind in ("salvage", "chair_salvage"):
        # one short follow-up call: its own timeout is the ceiling; stream seats keep idle supervision
        # (and the partial), buffered ones get it as the deadline (no idle signal to watch)
        sc = salvage_cfg(cfg)
        tmo = sc["chair_timeout"] if kind == "chair_salvage" else sc["timeout"]
        common["ceiling_s"] = tmo
        common["idle_s"] = min(common["idle_s"], tmo)
        if mode == "stream":
            return Limits(mode="stream", deadline_s=None, **common), mode, None, "stream_idle"
        return Limits(mode="buffered", deadline_s=tmo, **common), mode, tmo, "salvage_timeout"
    if mode == "stream":
        return Limits(mode="stream", deadline_s=None, **common), mode, None, "stream_idle"
    if ceiling is None:
        # A buffered seat with no ceiling that is not hedged (preflight FAILs this config: owner ruling 1 drops the
        # ceiling only where the hedge runs). It is still never given a wall-clock deadline: watch mode, no soft
        # notice, so it ends only on its own terminal state.
        lim = WatchLimits(mode="watch", deadline_s=None, soft_at_s=None, sample_s=None, **common)
        return lim, "watch", None, "no_ceiling"
    deadline, source = timing.adaptive_deadline(log_path, provider, seat.get("model"),
                                                prompt_bytes, cfg, grounded=bool(seat.get("_grounded")))
    deadline = ceiling if deadline is None else min(float(deadline), ceiling)
    return Limits(mode="buffered", deadline_s=deadline, **common), mode, deadline, source


# ---------------------------------------------------------------- task state
class _Task:
    def __init__(self, key, kind, seat, adapter):
        self.key = key
        self.kind = kind                  # seat | chair | salvage
        self.seat = seat
        self.adapter = adapter
        self.launch = None                # set by start(); final_text_path lives here
        self.extractor = None
        self.raw = bytearray()
        self.feed_error = None
        self.prompt_bytes = 0
        self.mode = None
        self.deadline_s = None
        self.deadline_source = None
        self.started = None
        self.done = False
        self.cut_requested = False
        self.status = "pending"
        self.detail = None
        self.result = None
        self.reported = None
        self.usage = None                 # providers.usage_dict, set by _classify
        self.text = None                  # final text when ok
        self.partial = ""
        self.signal_written = False
        self.prompt_text = ""
        self.parent = None                # salvage task -> the seat it serves
        self.soft_at_s = None             # D58: the watch call's soft deadline (s), in its registry record
        self.salvage_started = False
        self.salvaged = False
        self.salvaged_from = None
        self.salvage = None               # outcome of this seat's salvage call, when one ran
        # row 30: hedge / fallback / pending (all unused while [hedge] and [fallback] are off)
        self.call = {"seat": "original", "chair": "original"}.get(kind, kind)
        self.tag = None                   # COUNCIL_SEAT_TAG of a watch call
        self.sink = None                  # per-call otlp_sink.Sink (codex, progress_signal otel, signals on)
        self.hedgeable = False            # seat original: hedged(cfg, provider) at launch
        self.hedge_at = None              # D12 value (= the original's soft_at_s)
        self.hedge_next = None            # monotonic time of the next hedge check (soft notice, D12 recheck)
        self.hedge_deferred = 0
        self.hedge = None                 # the <id>~hedge task
        self.fallback = None              # the <id>~fallback task (D14/D24 form: salvage form, one call)
        self.standin = None               # the <id>~standin task: the fast-fail chain's last step (fallback_model)
        self.chain = None                 # fast-fail fallback chain [rerouted seat, stand-in seat?] once it started
        self.recovered = None             # {call, from_status, from_detail}: a full-effort fallback / stand-in answered
        self.substituted = None           # {model, reason}: the seat's answer is from its fallback_model
        self.lens = ""                    # the seat's lens text (councillib/verdict.py), '' when none
        self.critique = None              # a seat: its round 2 critique _Task (--depth standard), once launched
        self.letter = None                # a critique task: the Seat letter of its seat (councillib/critique.py)
        self.round1 = None                # a critique task: the seat's round 1 verdict (parse of its answer)
        self.grounding = None             # grounded run: {mode, root, reason?} of this seat (councillib/grounded.py)
        self.grounding_cfg = None         # cfg the quote check reads its [grounded] limits from
        self._gcache = (None, None)       # (hash of the answer text, its quote-check result)
        self.prior = {}                   # call -> calls.jsonl start record of an earlier run() (D17)
        self.own = None                   # the original call's own outcome, once it ended (hedged seats)
        self.resolved = False             # the seat has a usable answer from one of its calls
        self.answered_by = None           # original | hedge | salvage | fallback | standin
        self.pending = False              # D39: chair started without it, its calls still running
        self.frozen = None                # the seat view the chair used (D18: later answers are late)
        self.late = []                    # [{call, at_s}] answers that arrived after the chair started
        self.ended_at = None              # monotonic end of this call
        self.progress = None              # row 31: {signal, last_progress_s, events, ...} of a watch call
        self.private_caps = []            # row 40: capture files that are removed once the call ended
        self.stop_signal = None           # the operator signal (D53) that ended this call, when one did
        self.answer_check = None          # row 2: the [answer_check] cfg run() checks this call's text with (None: none)

    def on_output(self, chunk):
        self.raw += chunk
        if self.extractor is not None and self.feed_error is None:
            try:
                self.extractor.feed(chunk)
            except Exception as e:          # a broken extractor must not crash the loop
                self.feed_error = type(e).__name__


_FINAL_FILE_MAX = 4 << 20


def _final_file_text(task):
    """Text of the launch's final_text_path (e.g. codex -o last.txt), or None."""
    path = getattr(task.launch, "final_text_path", None)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            text = f.read(_FINAL_FILE_MAX).decode("utf-8", "replace")
    except OSError:
        return None
    return text if text.strip() else None


def _classify(task):
    """Fill status/detail/text from the Result and the extractor. Returns True if ok."""
    res = task.result
    ex = task.extractor
    err = None
    try:
        task.usage = ex.usage()
    except Exception:
        task.usage = None             # unknown, never invented
    try:
        feed = getattr(ex, "feed_stderr", None)
        if feed is not None and res.stderr_tail:
            feed(bytes(res.stderr_tail))     # row 28 wiring: before error(), which may name the reason
    except Exception:
        pass
    try:
        err = ex.error()
        task.reported = ex.model_reported()
        task.partial = ex.partial_text() or ""
        final = ex.final_text()
    except Exception as e:
        err, final = "extractor_%s" % type(e).__name__, None
    if task.feed_error:
        err = err or "extractor_%s" % task.feed_error
    term = None
    try:
        term = ex.terminal_reason()      # row 28: cli_timeout / quota_exhausted, read from stderr
    except Exception:
        term = None
    task.detail = _short(err)
    if not task.detail and res.status != "ok":
        # fallback-chain row 1: the seat child's own stderr reason (`openrouter http 503`...), so a 0-byte failure says why
        try:
            reason = getattr(ex, "stderr_reason", lambda: None)()
            task.detail = _short(reason) if reason else task.detail
        except Exception:
            pass
    ftext = _final_file_text(task)
    if ftext is not None:
        # the CLI's own last-message file is authoritative; it is also the best partial
        if res.status == "ok":
            final = ftext
        if not task.partial.strip():
            task.partial = ftext
    # an extractor may accept a non-zero exit when the CLI failed only because of its
    # output cap but streamed an answer (claude); the seat stays ok, detail says why
    if term and (res.status == "ok" or res.status.startswith("exit_")):
        task.status = term               # a non-answer the CLI itself reported (was adapter_error / exit_n)
        return False
    salvage = False
    if res.status.startswith("exit_") and not err:
        try:
            salvage = bool(getattr(ex, "salvage_exit", lambda: False)())
        except Exception:
            salvage = False
    if salvage:
        task.detail = "output_cap"
    elif res.status != "ok":
        task.status = "cut_after_quorum" if (res.status == "cut" and task.cut_requested) else res.status
        return False
    if err:
        task.status = "adapter_error"
        return False
    if not task.detail:
        try:
            task.detail = _short(getattr(ex, "note", lambda: None)())
        except Exception:
            pass
    pin = task.seat.get("model")
    if task.reported is not None and pin and not task.adapter.model_matches(pin, task.reported):
        task.status = "model_mismatch"
        return False
    if not final or not final.strip():
        task.status = "empty_output"
        return False
    bad = _answer_check.problem(final, task.answer_check)
    if bad:
        # fallback-chain row 2 (run 090651Z: 924 bytes of `<tool_call>Read...` counted as ok): not an answer. The text is
        # kept as the partial for the salvage prompt when the extractor kept nothing else.
        task.status, task.detail = "invalid_answer", _short(bad)
        if not task.partial.strip():
            task.partial = final
        return False
    task.status = "ok"
    task.text = final
    return True


def _md(task):
    """Markdown for a seat or the chair, by outcome."""
    after = task.result.total_s if task.result else (time.monotonic() - task.started if task.started else 0.0)
    if task.recovered and task.status == "ok":
        why = task.recovered.get("from_status")
        if task.substituted:
            return ("> STAND-IN: the seat's own model %s %s (%s); this answer is from %s, called once at full "
                    "effort with the same prompt\n\n%s\n" % (task.seat.get("_orig_model") or task.seat.get("model"),
                                                              "was unavailable" if why == "preflight" else "gave no answer",
                                                              task.substituted.get("reason"),
                                                              task.substituted.get("model"), task.text.rstrip()))
        return ("> RETRIED from status=%s: the seat's first call failed with nothing to salvage; this answer is from one "
                "full-effort retry of the same model (provider pin dropped, max_tokens capped)\n\n%s\n"
                % (why, task.text.rstrip()))
    if task.salvaged:
        what = ("the chair's own synthesis never arrived; this is the output of one short follow-up call "
                "(reasoning off), written from the seat answers and the chair's own notes" if task.kind == "chair"
                else "the seat's own critique never arrived; this is the output of one short follow-up call "
                     "(reasoning off)" if task.kind == "critique"
                else "the seat's own answer never arrived; this is the answer of one short follow-up call "
                     "(reasoning off)")
        return "> SALVAGED from status=%s: %s\n\n%s\n" % (task.salvaged_from, what, task.text.rstrip())
    if task.status == "ok":
        return task.text
    if task.status == "pending" and task.pending:
        return ("> PENDING after=%.1fs: no answer yet from the original or its hedge; both keep running and are "
                "not killed (D39). A later answer is written as late.\n" % after)
    body = task.partial or ""
    if (task.result and task.result.truncated) or task.status == "signal_kill":
        head = "> TRUNCATED reason=%s after=%.1fs" % (task.status, after)
    else:
        head = "> FAILED status=%s after=%.1fs" % (task.status, after)
    if task.salvage:
        head += "\n> SALVAGE tried: status=%s" % task.salvage.get("status")
    return head + ("\n\n" + body if body else "") + "\n"


def _usage_of(task):
    """The task's usage_dict; all-None when it never reached _classify (killed by a signal, not built)."""
    return dict(task.usage) if task.usage else providers.usage_dict()


def _or_ids(task):
    """D56: the serving provider and the generation id of one OpenRouter call, read from the call's own extractor
    (the stream chunks' `provider` and `id`, verbatim; no extra request). {} for any other provider and for a call
    that never built an extractor; null for a field the stream did not carry, never a guess. Read when a record is
    written, not only after _classify: an operator stop (D53) ends a call without classifying it."""
    ex = task.extractor
    if not isinstance(ex, providers.OpenRouterExtractor):
        return {}
    out = {}
    for key, get in (("serving_provider", ex.serving_provider), ("generation_id", ex.generation_id)):
        try:
            v = get()
        except Exception:
            v = None
        out[key] = _short(v) if isinstance(v, str) and v else None
    return out


def _or_end(task):
    """P26: an OpenRouter call's calls.jsonl end fields: `_or_ids` plus `output_tok_s` (the call's reported output
    tokens, reasoning included, over its whole `total_s`: row 9's measure) and `quantization` (the serving endpoint's,
    from the preflight's cached endpoints snapshot, `providers.endpoint_quantization`; no request). {} for any other
    provider; null for a value the call did not give, never a guess. seats/<id>.json and summary.json keep `_or_ids`."""
    ids = _or_ids(task)
    if not ids:
        return {}
    tok_s = q = None
    try:
        out_t = (task.usage or {}).get("output_tokens")
        total = task.result.total_s if task.result is not None else None
        if (isinstance(out_t, (int, float)) and not isinstance(out_t, bool) and out_t >= 0
                and isinstance(total, (int, float)) and total > 0):
            tok_s = round(out_t / total, 1)
    except Exception:
        tok_s = None
    try:
        q = providers.endpoint_quantization((task.seat or {}).get("model"), ids.get("serving_provider"))
    except Exception:
        q = None
    return dict(ids, output_tok_s=tok_s, quantization=_short(q) if q else None)


def _cost_totals(recs):
    """(sum of reported costs or None, ids that ran without a reported cost) over seat/chair records
    and their salvage calls. An unreported cost is not zero: subscription CLIs and killed runs land in
    the id list."""
    total, missing = None, []
    for rec in recs:
        extra = [("%s.%s" % (rec.get("id"), c.get("call")), c.get("usage")) for c in rec.get("calls") or ()
                 if c.get("call") in ("hedge", "fallback", "standin")]
        crit = rec.get("critique") or {}
        if crit:         # --depth standard: the round 2 call and its salvage are real spend too
            extra += [("%s.critique" % rec.get("id"), crit.get("usage")),
                      ("%s.critique.salvage" % rec.get("id"), (crit.get("salvage") or {}).get("usage"))]
        for rid, u in [(rec.get("id"), rec.get("usage")),
                       ("%s.salvage" % rec.get("id"), (rec.get("salvage") or {}).get("usage"))] + extra:
            if not isinstance(u, dict) or rec.get("status") in ("not_launched", "skipped_allowed_missing", "build_error"):
                continue
            c = u.get("cost_usd")
            if c is None:
                missing.append(rid)
            else:
                total = (total or 0.0) + c
    return (round(total, 8) if total is not None else None), missing


def _grounding_rec(task):
    """The seat's grounding record: {mode, root, reason?} plus, for a grounded seat with an answer, `check` = the quote
    check of that answer (cached per text; never changes the seat's status). None in a run that is not grounded."""
    g = task.grounding
    if not g:
        return None
    rec = dict(g)
    if g.get("mode") == "grounded" and task.kind in ("seat", "critique") and task.status == "ok" and task.text:
        key = hash(task.text)
        if task._gcache[0] != key:
            try:
                chk = _grounded.verify(task.text, g["root"], task.grounding_cfg)
            except Exception as e:                       # a checker bug must not cost a seat its answer
                chk = {"n_citations": 0, "n_quotes": 0, "n_bad": 0, "n_line_off": 0, "n_unchecked": 0, "bad": [],
                       "grounded_ok": None, "error": type(e).__name__}
            task._gcache = (key, chk)
        rec["check"] = task._gcache[1]
    return rec


def _task_record(task, rel_paths=None):
    r = task.result
    rec = {
        "id": task.seat.get("id"), "provider": task.seat.get("provider"),
        "model": task.seat.get("model"), "model_reported": _short(task.reported),
        "status": task.status, "detail": task.detail,
        "exit_code": r.exit_code if r else None,
        "first_byte_s": r.first_byte_s if r else None,
        "max_gap_s": r.max_gap_s if r else None,
        "total_s": r.total_s if r else None,
        "out_bytes": r.out_bytes if r else len(task.raw),
        "extensions": r.extensions if r else 0,
        "deadline_s": task.deadline_s, "deadline_source": task.deadline_source,
        "truncated": bool(r.truncated) if r else task.status == "signal_kill",
        "mode": task.mode,
        "usage": _usage_of(task),
    }
    rec.update(_or_ids(task))
    if task.kind in ("seat", "critique"):
        if task.status == "ok":
            rec["verdict"] = _verdict.parse(task.text)       # display only; None = no verdict line (never a failure)
        if task.lens:
            rec["lens"] = task.lens
        grec = _grounding_rec(task)
        if grec is not None:
            rec["grounding"] = grec
            chk = grec.get("check")
            if chk is not None:
                rec["grounded_ok"] = chk["grounded_ok"]
                rec["n_citations"], rec["n_bad"] = chk["n_citations"], chk["n_bad"]
    if task.salvage_started:
        rec["salvaged"] = task.salvaged
        rec["salvaged_from"] = task.salvaged_from
        rec["salvage"] = task.salvage
    if task.recovered:
        rec["recovered"] = dict(task.recovered)
    if task.substituted:
        rec["substituted"] = dict(task.substituted)
    if task.hedgeable or task.fallback is not None or task.prior:
        rec["answered_by"] = task.answered_by
        rec["calls"] = _calls_record(task)
        if task.hedge_at is not None:
            rec["hedge_at_s"] = task.hedge_at
            rec["hedge_deferred"] = task.hedge_deferred
        if task.pending:
            rec["pending"] = True
        if task.late:
            rec["late"] = list(task.late)
        rec["outcome"] = _outcome(task)
    if task.progress is not None:
        rec["progress"] = task.progress
    if rel_paths:
        rec["paths"] = rel_paths
    return rec


def _critique_record(ct, paths=None):
    """One seat's round 2 record (summary.json seats[].critique, seats/<id>.critique.json): the call's own record plus
    `verdict` (the REVISED verdict; the round 1 verdict stays on the seat) and how it moved. Display only."""
    rec = _task_record(ct, paths)
    rec["letter"] = ct.letter
    rec["round1_verdict"] = ct.round1
    if ct.status == "ok":
        rec["changed_view"] = _critique.parse_changed(ct.text)
        rec["verdict_change"] = _critique.verdict_change(ct.round1, rec.get("verdict"))
    return rec


def _call_entry(call, used_by):
    r = call.result
    e = {"call": call.call, "key": call.key, "tag": call.tag, "status": call.status, "detail": call.detail,
         "mode": call.mode, "exit_code": r.exit_code if r else None,
         "total_s": r.total_s if r else None, "out_bytes": r.out_bytes if r else len(call.raw),
         "truncated": bool(r.truncated) if r else False, "running": not call.done,
         "role": used_by, "usage": _usage_of(call)}
    e.update(_or_ids(call))
    if call.progress is not None:
        e["progress"] = call.progress
    if call.stop_signal is not None:
        e["signal"] = call.stop_signal
    return e


def _progress_of(verdict, sink_events=None):
    """seats/<id>.json `progress` (row 31) from a watch call's last liveness verdict: the adopted signal, the
    last progress (s since the call started; keepalives never count, D44) and how many events were seen
    (OTLP events of the call's sink when it has one, else counted liveness samples)."""
    if verdict is None or not hasattr(verdict, "progress_signal"):
        return None
    events = sink_events if sink_events is not None else getattr(verdict, "samples", None)
    return {"signal": getattr(verdict, "progress_signal", None),
            "last_progress_s": getattr(verdict, "progress_at", None),
            "events": events,
            "keepalives": getattr(verdict, "keepalives", None),
            "elapsed_s": getattr(verdict, "elapsed_s", None)}


def _calls_record(seat):
    """seat json `calls: [...]` (row 30): every call of a seat in launch order, with its role:
    used (the chair got it) | alternate (an answer not used, D33) | late (after the chair started, D18) |
    failed | running | observer_lost (an earlier run()'s call, D17)."""
    out = []
    for name, rec in sorted(seat.prior.items()):
        out.append({"call": name, "key": rec.get("key"), "tag": rec.get("tag"), "status": "observer_lost",
                    "started_at": rec.get("started_at"), "running": False, "role": "observer_lost"})
    own = seat.own or {}
    calls = [("original", seat)]
    if seat.hedge is not None:
        calls.append(("hedge", seat.hedge))
    if seat.fallback is not None:
        calls.append(("fallback", seat.fallback))
    if seat.standin is not None:
        calls.append(("standin", seat.standin))
    late = {e["call"] for e in seat.late}
    for name, c in calls:
        if name == "original":
            e = _call_entry(c, None)
            if c.done and own:
                e.update(status=own.get("status"), detail=own.get("detail"))
            ok = c.done and own.get("status") == "ok"
        else:
            e = _call_entry(c, None)
            ok = c.done and c.status == "ok"
        if name in late:
            e["role"] = "late"
        elif not c.done:
            e["role"] = "running"
        elif ok and seat.answered_by == name:
            e["role"] = "used"
        elif ok:
            e["role"] = "alternate"
        else:
            e["role"] = "failed"
        out.append(e)
    return out


def _outcome(seat):
    """ok | pending (calls still running, no answer) | late (answered only after the chair) | unresolved."""
    if seat.answered_by is not None and not (seat.frozen and seat.frozen.get("status") != "ok"):
        return "ok"
    if seat.late:
        return "late"
    running = [c for c in (seat, seat.hedge, seat.fallback, seat.standin) if c is not None and not c.done]
    if running:
        return "pending"
    return "unresolved"


_FROZEN = ("status", "text", "detail", "answered_by", "salvaged", "salvaged_from", "partial", "pending")


def _close_sink(task):
    sink, task.sink = task.sink, None
    if sink is not None:
        try:
            sink.close()
        except Exception:
            pass


LONG_WATCH_FACTOR = 2.0            # D58: list a watch call once it runs longer than this x its soft deadline


def _ignore_hup(_signum, _frame):
    """D59: SIGHUP to the runner (a closed terminal or chat) never stops it or its calls."""


# ---------------------------------------------------------------- run
def _as_standin(seat, standins, cfg):
    """The seat dict to launch: the stand-in start (adapter.standin_start) when the preflight put the seat in
    `standins`, marked `_substituted` {model, reason}; else the seat itself."""
    reason = (standins or {}).get(seat.get("id"))
    if not reason:
        return seat
    try:
        st = providers.get(seat.get("provider")).standin_start(seat, cfg)
    except Exception:
        st = None
    if st is None:
        return seat
    st["_substituted"] = {"model": st.get("model"), "reason": str(reason)}
    return st


def run(cfg, question, context_files, out_dir, allow_missing=(), preflight_result=None, on_ready=None,
        parent_pid=None, standins=None):
    """The runner (see the module docstring). Live status (row 1 of run example-run): this wrapper owns
    `<state_dir>/current.json` (the pointer to this run, pid = THIS process, the runner) from start to end and closes a
    status.json left open by a crash; `_run` writes `<out_dir>/status.json` as it goes (councillib/status.py)."""
    out_abs = os.path.abspath(out_dir)
    sdir = _status.state_dir_of(cfg)
    scfg = _status.cfg_of(cfg)
    if scfg["enabled"]:
        _status.write_current(sdir, out_abs)
        if scfg["progress"]:
            _progress.prune_old()                    # boss.progress/1 registry retention (writers prune, 7 days)
    try:
        return _run(cfg, question, context_files, out_dir, allow_missing, preflight_result, on_ready,
                    parent_pid, standins)
    finally:
        _status.close_open(out_abs, reason="runner ended without a final status (crash)", progress=scfg["progress"])
        _status.clear_current(sdir, out_abs, os.getpid())


def _run(cfg, question, context_files, out_dir, allow_missing=(), preflight_result=None, on_ready=None,
         parent_pid=None, standins=None):
    """`standins` = {seat id: reason} from the preflight (preflight.standins): those seats start on their stand-in
    model as the first call, marked `substituted` (the chair is told). A seat with no stand-in launches as configured."""
    t0 = time.monotonic()
    out_dir = os.path.abspath(out_dir)
    run_id = os.path.basename(out_dir.rstrip("/"))
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    state_dir = cfg.get("state_dir") or os.path.expanduser("~/.claude-council")
    log_path = os.path.join(state_dir, "latency.jsonl")
    seat_kind = "test" if cfg.get("test") else "seat"
    roster = [dict(s) for s in (cfg.get("seat") or [])]
    qdata = question.encode("utf-8") if isinstance(question, str) else bytes(question)
    qtext = qdata.decode("utf-8", "replace")
    caller_session = _caller_session()                 # once: summary() is written many times during a run

    # 1. prompts -----------------------------------------------------------------
    _write(os.path.join(out_dir, "question.md"), qdata)
    try:
        advisor_tpl = _load_template(cfg, "advisor.md")
        chair_tpl = _load_template(cfg, "chair.md")
        context = _context_block(context_files, cfg.get("_rulings"))
        advisor_tpl.substitute(question="", context="", seat_id="", n_seats=0, lens_line="")
        chair_tpl.substitute(question="", answers="", **_critique.chair_vars(None))
        depth = _critique.depth_of(cfg)                # council.py run --depth quick|standard (underscore: not drift)
        critique_tpl = gcritique_tpl = None
        if depth == "standard":                        # the round 2 templates are only needed (and checked) then
            critique_tpl = _load_template(cfg, "critique.md")
            critique_tpl.substitute(question="", context="", letter="", own="", peers="", n_seats=0, lens_line="")
        gstate = cfg.get("_grounded")                  # council.py run --grounded: {root, images} (underscore: not drift)
        gadvisor_tpl = None
        if gstate:
            gadvisor_tpl = _load_template(cfg, "advisor-grounded.md")
            gadvisor_tpl.substitute(question="", context="", seat_id="", n_seats=0, lens_line="", root="", images_line="", access_line="")
            if depth == "standard":
                gcritique_tpl = _load_template(cfg, "critique-grounded.md")
                gcritique_tpl.substitute(question="", context="", letter="", own="", peers="", n_seats=0, lens_line="",
                                         root="", images_line="", access_line="")
    except (OSError, KeyError, ValueError):
        return _finish_early(cfg, out_dir, run_id, qdata, roster, [], None, 2)

    # 2. drop allowed-missing seats (only providers that FAILed preflight), and seats whose provider is out of
    #    quota until a recorded reset time (D47: <state_dir>/quota.json, no probe call) ---------------------
    failed = _failed_providers(preflight_result)
    allowed = set(allow_missing or ())
    try:
        out_of_quota = providers.quota_unavailable(cfg)
    except Exception:
        out_of_quota = {}
    skipped, unavail, launched = [], [], []
    for s in roster:
        if s.get("provider") in allowed and s.get("provider") in failed:
            skipped.append(s)
        elif s.get("provider") in out_of_quota:
            unavail.append(s)
        else:
            launched.append(_as_standin(s, standins, cfg))
    n = len(launched)
    quorum_cfg = cfg.get("quorum") or {}
    try:
        min_ok = _config.resolve_min_ok(quorum_cfg.get("min_ok", "n-1"), n)
    except ValueError:
        return _finish_early(cfg, out_dir, run_id, qdata, roster, skipped, None, 2, unavailable=unavail)
    grace_s = float(quorum_cfg.get("straggler_grace_s", 90))
    cut_stream = bool(quorum_cfg.get("cut_stream_stragglers", False))
    need = max(1, min_ok)
    if n == 0 or min_ok > n:
        return _finish_early(cfg, out_dir, run_id, qdata, roster, skipped, min_ok, 3,
                             grace_s=grace_s, unavailable=unavail)

    work_root = tempfile.mkdtemp(prefix="council-")
    if gstate and gstate.get("images"):
        # the seats read copies in a dir of ours, never the image's own folder (its siblings stay out of reach)
        gstate = dict(gstate, images=_grounded.stage_images(gstate["images"], os.path.join(work_root, "attachments")))
    board = _liveness.LivenessBoard()
    sup = Supervisor(sampler=board)       # only watch calls carry sample_s; others are never sampled
    tasks = {}
    salvage_tasks = {}            # seat id -> its salvage task
    scfg = salvage_cfg(cfg)
    acfg = _answer_check.cfg_of(cfg)
    hcfg = hedge_cfg(cfg)
    fb_on = fallback_on(cfg)
    calls_path = os.path.join(out_dir, "calls.jsonl")
    order = []
    state = {"chair": None, "quorum_at": None, "ok": 0, "frozen": False, "late": [], "below_quorum": False,
             # row 31: runner lifetime
             "phase": "seats", "ready": None, "synth_at": None, "footers": [], "late_text": {},
             "d50": None, "late_synthesis": None,
             # D58 (row 47): watch calls past LONG_WATCH_FACTOR x their soft deadline, key -> entry; shown, never killed
             "long_watch": {},
             # --depth standard: the critique round's summary {ran, reason?, letters, started_s, total_s}
             "critique": None}

    # D17/D37: hedge / fallback calls an earlier run() on this out dir started. Never started again.
    prior_calls, lost = {}, set()
    for rec in _read_calls(calls_path):
        if rec.get("call") not in ("hedge", "fallback", "standin") or not rec.get("seat"):
            continue
        if rec.get("status") == "observer_lost":
            lost.add((rec["seat"], rec["call"], rec.get("key")))
        elif "status" not in rec:
            prior_calls.setdefault(rec["seat"], {})[rec["call"]] = rec

    # ---- live status (row 1 of run example-run): status.json, rewritten from the event loop ------
    st_writer = _status.Writer(out_dir, _status.cfg_of(cfg))
    st = {"code": None, "ended": False}
    started_epoch = time.time() - (time.monotonic() - t0)
    st_title = _status.title_of(qtext)

    def st_call_name(c, seat):
        if c.kind == "salvage":
            return "salvage"
        if c.kind == "fallback":
            return "fallback-reroute" if seat.chain else "fallback"
        if c.kind == "standin":
            return "stand-in"
        if c.kind == "hedge":
            return "hedge"
        # the seat's own first call; a preflight stand-in is the seat's model replaced before launch
        return "stand-in" if (seat.substituted and (seat.recovered or {}).get("from_status") == "preflight") else "original"

    def st_calls(seat):
        out = [seat] + [c for c in (seat.hedge, seat.fallback, seat.standin) if c is not None]
        out += [salvage_tasks[c.key] for c in list(out) if c.key in salvage_tasks]
        return [c for c in out if c.started is not None]

    def st_numbers(c, now):
        """Live numbers of one call: the supervisor's view while it runs, the Result once it ended."""
        if not c.done and not st["ended"]:
            i = sup.info(c.key)
            if i is not None:
                return {"elapsed_s": i.elapsed_s, "out_bytes": i.out_bytes, "first_byte_s": i.first_byte_s,
                        "last_byte_age_s": (i.elapsed_s - i.last_out_s) if i.last_out_s is not None else None}
            return {"elapsed_s": now - c.started, "out_bytes": len(c.raw), "first_byte_s": None, "last_byte_age_s": None}
        r = c.result
        return {"elapsed_s": r.total_s if r else (c.ended_at - c.started if c.ended_at else 0.0),
                "out_bytes": r.out_bytes if r else len(c.raw), "first_byte_s": r.first_byte_s if r else None,
                "last_byte_age_s": None}

    def st_retries(c):
        """Pre-byte retries of an OpenRouter seat child (`openrouter retry N` stderr lines), live or ended."""
        ex = c.extractor
        if not isinstance(ex, providers.OpenRouterExtractor):
            return 0
        try:
            if c.done or st["ended"]:
                return len(ex.retries())
            tmp = providers.OpenRouterExtractor()      # a fresh one: the task's own is fed once, by _classify
            tmp.feed_stderr(sup.stderr_tail(c.key))
            return len(tmp.retries())
        except Exception:
            return 0

    def st_seat(seat, now):
        calls = st_calls(seat)
        live_calls = [] if st["ended"] else [c for c in calls if not c.done]
        names = {id(c): st_call_name(c, seat) for c in calls}
        if live_calls:
            cur = max(live_calls, key=lambda c: c.started)
        else:
            by = {"original": seat, "hedge": seat.hedge, "fallback": seat.fallback, "standin": seat.standin,
                  "salvage": salvage_tasks.get(seat.key)}
            cur = by.get(seat.answered_by) if seat.status == "ok" else None
            cur = cur or (max(calls, key=lambda c: c.started) if calls else seat)
        num = st_numbers(cur, now) if cur.started is not None else {"elapsed_s": None, "out_bytes": 0,
                                                                     "first_byte_s": None, "last_byte_age_s": None}
        if cur.started is None:
            phase = "waiting"
        elif live_calls:
            phase = "streaming" if (num["out_bytes"] or num["first_byte_s"] is not None) else "waiting"
        else:
            phase = "done" if seat.status == "ok" else "failed"
        # earlier calls that failed, as the reason a later call is running or answered
        own = seat.own or {}
        earlier = []
        for c in calls:
            if c is cur:
                continue
            stt = own.get("status") if c is seat else c.status
            if c.done and stt not in (None, "ok", "running", "pending"):
                earlier.append("%s %s" % (names[id(c)], stt))
        detail = None
        if phase == "failed":
            detail = seat.detail or ("; ".join(earlier) if earlier else None)
        elif earlier:
            detail = "; ".join(earlier)
        started = seat.started
        end = max([c.started + (c.result.total_s if c.result else 0.0) for c in calls if c.done] or [now]) \
            if phase in ("done", "failed") else now
        centries = []
        for c in calls:
            cn = st_numbers(c, now)
            running = not c.done and not st["ended"]
            cstat = own.get("status") if c is seat and seat.own else c.status
            centries.append({"call": names[id(c)], "model": c.seat.get("model"), "phase": ("streaming" if (cn["out_bytes"] or cn["first_byte_s"] is not None)
                                                              else "waiting") if running
                             else ("done" if cstat == "ok" else "failed"),
                             "status": None if running else cstat, "elapsed_s": round(cn["elapsed_s"] or 0.0, 1),
                             "out_bytes": cn["out_bytes"]})
        rt = st_retries(cur) if cur.started is not None else 0
        sub = (dict(seat.substituted) if seat.substituted else None)
        entry = {"id": seat.seat.get("id"), "provider": cur.seat.get("provider"), "model": cur.seat.get("model"),
                "call": names.get(id(cur)), "retry": rt, "phase": phase,
                "status": seat.status if phase in ("done", "failed") else None,
                "elapsed_s": round(end - started, 1) if started is not None else None,
                "call_elapsed_s": round(num["elapsed_s"], 1) if num["elapsed_s"] is not None else None,
                "first_byte_s": round(num["first_byte_s"], 2) if num["first_byte_s"] is not None else None,
                "out_bytes": num["out_bytes"],
                "last_byte_age_s": round(num["last_byte_age_s"], 1) if num["last_byte_age_s"] is not None else None,
                "detail": detail, "substituted": sub, "calls": centries,
                "verdict": _verdict.parse(seat.text) if seat.status == "ok" and seat.kind == "seat" else None,
                "grounding": _grounded.label(_grounding_rec(seat)) or None}
        crit = st_critique(seat, now)
        if crit is not None:             # --depth standard only: a quick run's seat entry is unchanged
            entry["critique"] = crit
        return entry

    def st_critique(seat, now):
        """Round 2 of one seat (--depth standard): its own object, never one of the seat's `calls` (a critique is not a
        retry of the answer, and the seat's phase / elapsed above stay those of round 1)."""
        ct = seat.critique
        if ct is None:
            return None
        sv = salvage_tasks.get(ct.key)
        live = [] if st["ended"] else [c for c in (ct, sv) if c is not None and c.started is not None and not c.done]
        cur = max(live, key=lambda c: c.started) if live else ct
        num = st_numbers(cur, now) if cur.started is not None else {"elapsed_s": None, "out_bytes": 0, "first_byte_s": None,
                                                                     "last_byte_age_s": None}
        if cur.started is None:
            phase = "waiting"
        elif live:
            phase = "streaming" if (num["out_bytes"] or num["first_byte_s"] is not None) else "waiting"
        else:
            phase = "done" if ct.status == "ok" else "failed"
        v2 = _verdict.parse(ct.text) if ct.status == "ok" else None
        total = None
        if ct.started is not None:
            end = max([c.started + (c.result.total_s if c.result else 0.0) for c in (ct, sv)
                       if c is not None and c.done and c.started is not None] or [now]) if phase in ("done", "failed") else now
            total = round(end - ct.started, 1)
        return {"letter": ct.letter, "phase": phase, "call": "salvage" if cur is sv and sv is not None else "critique",
                "status": ct.status if phase in ("done", "failed") else None, "elapsed_s": total,
                "out_bytes": num["out_bytes"],
                "last_byte_age_s": round(num["last_byte_age_s"], 1) if num["last_byte_age_s"] is not None else None,
                "detail": ct.detail if phase == "failed" else None, "round1_verdict": ct.round1, "verdict": v2,
                "changed_view": _critique.parse_changed(ct.text) if ct.status == "ok" else None}

    def status_snapshot():
        now = time.monotonic()
        by_id = {t.seat.get("id"): t for t in order}
        seats = []
        for s in roster:
            t = by_id.get(s["id"])
            if t is not None:
                seats.append(st_seat(t, now))
            else:
                why = "skipped_allowed_missing" if s in skipped else "skipped_unavailable" if s in unavail else None
                seats.append({"id": s["id"], "provider": s.get("provider"), "model": s.get("model"), "call": None,
                              "retry": 0, "phase": "skipped" if why else "waiting", "status": why, "elapsed_s": None,
                              "call_elapsed_s": None, "first_byte_s": None, "out_bytes": 0, "last_byte_age_s": None,
                              "detail": None, "substituted": None, "calls": []})
        chair = None
        if state["chair"] is not None:
            c = st_seat(state["chair"], now)
            chair = {k: c[k] for k in ("provider", "model", "call", "retry", "phase", "status", "elapsed_s", "first_byte_s",
                                       "out_bytes", "last_byte_age_s", "detail")}
        code = st["code"]
        phase = state["phase"] if code is None else ("done" if code == 0 else "failed")
        if phase not in ("seats", "critique", "chair", "done", "failed"):
            phase = "chair"
        wall = time.time()
        lp = late_pending() if code is not None and not st["ended"] else []
        snap = {"schema_version": _status.SCHEMA_VERSION, "run_id": run_id, "title": st_title, "pid": os.getpid(),
                "started_at": _status.iso(started_epoch), "started_epoch": round(started_epoch, 3),
                "updated_at": _status.iso(wall), "updated_epoch": round(wall, 3), "interval_s": st_writer.interval,
                "elapsed_s": round(now - t0, 1), "phase": phase, "ended": st["ended"],
                "supervising": code is not None and not st["ended"], "exit_code": code,
                "quorum": {"ok": state["ok"], "min_ok": min_ok, "need": need, "reached_at_s": state["quorum_at"]},
                "seats": seats, "chair": chair,
                "late_pending": [("%s/%s" % (c.get("seat"), c.get("call"))) for c in lp]}
        if depth == "standard":
            snap["depth"] = depth
        return snap

    def status_tick(force=False):
        st_writer.tick(status_snapshot, force=force)

    def paths_for(sid):
        return {"md": "seats/%s.md" % sid, "raw": "seats/%s.raw" % sid, "json": "seats/%s.json" % sid}

    def write_seat(task):
        for c in (task, task.hedge, task.fallback, task.standin):
            if c is not None and c.started is not None and not c.done:
                snap_progress(c)
        sid = _safe(task.seat["id"])
        _write(os.path.join(out_dir, "seats", sid + ".raw"), bytes(task.raw))
        _write(os.path.join(out_dir, "seats", sid + ".md"), _md(task))
        _write(os.path.join(out_dir, "seats", sid + ".json"),
               json.dumps(_task_record(task, paths_for(sid)), indent=1))
        sv = salvage_tasks.get(task.key)
        if sv is not None:
            _write(os.path.join(out_dir, "seats", sid + ".salvage.raw"), bytes(sv.raw))

    def critique_paths(sid):
        return {"md": "seats/%s.critique.md" % sid, "raw": "seats/%s.critique.raw" % sid,
                "json": "seats/%s.critique.json" % sid}

    def write_critique(ct):
        """seats/<id>.critique.{md,raw,json} (and .critique.salvage.raw): round 2 of one seat. Display only."""
        sid = _safe(ct.seat["id"])
        _write(os.path.join(out_dir, "seats", sid + ".critique.raw"), bytes(ct.raw))
        _write(os.path.join(out_dir, "seats", sid + ".critique.md"), _md(ct))
        _write(os.path.join(out_dir, "seats", sid + ".critique.json"),
               json.dumps(_critique_record(ct, critique_paths(sid)), indent=1))
        sv = salvage_tasks.get(ct.key)
        if sv is not None:
            _write(os.path.join(out_dir, "seats", sid + ".critique.salvage.raw"), bytes(sv.raw))

    def log_task(task):
        r = task.result
        kind = (seat_kind if task.kind == "seat" else
                task.kind if task.kind in ("salvage", "hedge", "fallback", "standin", "critique") else "chair")
        timing.append(log_path, {
            "run_id": run_id,
            "kind": kind,
            "provider": task.seat.get("provider"), "model": task.seat.get("model"),
            "mode": task.mode, "prompt_bytes": task.prompt_bytes,
            "out_bytes": r.out_bytes if r else len(task.raw),
            "first_byte_s": r.first_byte_s if r else None,
            "max_gap_s": r.max_gap_s if r else None,
            "total_s": r.total_s if r else None,
            "outcome": task.status, "extensions": r.extensions if r else 0,
            "deadline_s": task.deadline_s, "deadline_source": task.deadline_source,
            "grounded": True if task.seat.get("_grounded") else None})

    def critique_summary(recs):
        """summary.json `critique` of a --depth standard run: whether the round ran (and why not), the letter map the chair
        also got, who critiqued, who failed, who said their view changed and how the verdicts moved. Display only."""
        info = dict(state["critique"] or {"ran": False, "reason": "the run ended before the critique round"})
        cs = [(r["id"], r["critique"]) for r in recs if r.get("critique")]
        info["critics"] = [i for i, _ in cs]
        info["ok"] = [i for i, c in cs if c["status"] == "ok"]
        info["failed"] = {i: c["status"] for i, c in cs if c["status"] != "ok"}
        info["changed_view"] = [i for i, c in cs if c.get("changed_view") == "yes"]
        info["verdict_changes"] = {i: c["verdict_change"] for i, c in cs if c.get("verdict_change")}
        info["revised_verdicts"] = _verdict.tally(c.get("verdict") for _, c in cs if c["status"] == "ok")
        return info

    def summary(exit_code, extra=None):
        recs = []
        done_ids = {t.seat["id"]: t for t in tasks.values() if t.kind == "seat"}
        for s in roster:
            t = done_ids.get(s["id"])
            if t is not None:
                recs.append(_task_record(t, paths_for(_safe(s["id"]))))
                if t.critique is not None:
                    recs[-1]["critique"] = _critique_record(t.critique, critique_paths(_safe(s["id"])))
            else:
                recs.append({"id": s["id"], "provider": s.get("provider"), "model": s.get("model"),
                             "status": ("skipped_allowed_missing" if s in skipped else
                                        "skipped_unavailable" if s in unavail else "not_launched")})
        data = {
            "run_id": run_id, "question_bytes": len(qdata),
            "caller_session": caller_session, "engine_root": _config.ROOT,   # P33c: who called, which engine ran
            "roster": [{"id": s["id"], "provider": s.get("provider"), "model": s.get("model")}
                       for s in roster],
            "seats": recs,
            "quorum": {"min_ok": min_ok, "ok": state["ok"], "reached_at_s": state["quorum_at"],
                       "grace_s": grace_s},
            "chair": (_task_record(state["chair"], {"md": "chair/synthesis.md"})
                      if state["chair"] is not None else None),
            "exit_code": exit_code}
        if gstate:
            gs = [r for r in recs if r.get("grounding")]
            data["grounded"] = {
                "root": gstate["root"], "images": [os.path.basename(i) for i in gstate.get("images") or ()],
                "grounded_seats": [r["id"] for r in gs if r["grounding"]["mode"] == "grounded"],
                "ungrounded_seats": {r["id"]: r["grounding"].get("reason") for r in gs
                                     if r["grounding"]["mode"] == "ungrounded"},
                "flagged_seats": [r["id"] for r in gs if r.get("grounded_ok") is False],
                "n_citations": sum(r.get("n_citations") or 0 for r in gs), "n_bad": sum(r.get("n_bad") or 0 for r in gs)}
        if any("verdict" in r for r in recs):
            data["verdicts"] = _verdict.tally(r["verdict"] for r in recs if r.get("status") == "ok" and "verdict" in r)
        if depth == "standard":
            data["depth"] = depth
            data["critique"] = critique_summary(recs)
        cost, unreported = _cost_totals(recs + ([data["chair"]] if data["chair"] else []))
        data["cost_reported_usd"], data["cost_unreported"] = cost, unreported
        # row 30 keys: present only when they hold something, so a run with [hedge]/[fallback] off is unchanged
        pend = [t.seat["id"] for t in order if t.pending]
        if pend:
            data["pending"] = pend
        if state["late"]:
            data["late"] = list(state["late"])
        if state["below_quorum"]:
            data["below_quorum"] = True
        if unavail:
            data["unavailable"] = {s["id"]: out_of_quota.get(s.get("provider")) for s in unavail}
        # row 31: the runner (D18). late_pending: calls still running after the chair started
        data["runner_pid"] = os.getpid()
        data["phase"] = state["phase"]
        lp = late_pending()
        if lp:
            data["late_pending"] = lp
        if state["late_synthesis"] is not None:
            data["late_synthesis"] = dict(state["late_synthesis"])
        lw = long_watch_now()
        if lw:
            data["long_watch"] = lw
        if extra:
            data.update(extra)
        _write(os.path.join(out_dir, "summary.json"), json.dumps(data, indent=1))

    def on_signal():
        signum = sup.last_signal
        try:
            # D53: the operator's stop ends every call. Stop them first, so each call's record carries its real
            # end (the supervisor's own result for it), not a guess made while its process still runs. Nothing is
            # killed that the handler would not kill next (it calls kill_all again, a no-op by then).
            sup.kill_all()
            results = {}
            for item in sup.poll(0):
                if not isinstance(item, Notice) and item[0] in tasks:
                    results[item[0]] = item[1]
            touched, ended = [], []
            for t in list(tasks.values()):
                if t.done:
                    continue
                res = results.get(t.key)
                if res is not None:
                    t.result, t.done, t.ended_at = res, True, time.monotonic()
                    t.stop_signal = signum
                    if res.status == "signal_kill":
                        t.status = "signal_kill"
                        t.partial = _safe_partial(t)
                    else:
                        try:
                            _classify(t)          # it had already ended before the stop: its own status stands
                        except Exception:
                            t.status = res.status
                    ended.append(t)
                elif t.kind in ("seat", "chair", "critique"):
                    t.status = "signal_kill"      # the run's view; a call with no result is not marked ended
                    t.partial = _safe_partial(t)
                else:
                    continue
                touched.append(t)
            for t in touched:
                log_task(t)
                if t.kind in ("hedge", "fallback", "standin") and t in ended:
                    record_call(t, status=t.status, ended_at=_iso_now(),
                                exit_code=t.result.exit_code if t.result else None, signal=signum, **_or_end(t))
                elif t in ended:
                    record_or_end(t, signal=signum)
            for t in touched:
                if t.kind == "chair":
                    _write(os.path.join(out_dir, "chair", "synthesis.md"), _md(t))
            for sd in {(t if t.kind == "seat" else t.parent) for t in touched}:
                if sd is not None and sd.kind == "seat":
                    write_seat(sd)               # after every call above has its end: the seat json is complete
            for t in touched:
                if t.kind == "critique" or (t.kind == "salvage" and t.parent is not None and t.parent.kind == "critique"):
                    write_critique(t if t.kind == "critique" else t.parent)
            summary(128 + (signum or 0), {"interrupted": True})
            st["code"], st["ended"] = 128 + (signum or 0), True
            status_tick(force=True)
        except Exception:
            pass
        _status.clear_current(state_dir, out_dir, os.getpid())     # the handler ends in os._exit: no finally runs
        for t in list(tasks.values()):          # the explicit stop (D53) ended every call: none is an orphan
            if t.mode == "watch":
                unregister_watch_call(state_dir, t.tag)

    def watch_opts(task, wd, tag):
        """launch_opts (D38) for a watch call: its tag, plus the probe-signal inputs while [liveness] signals is on
        (codex: a per-call OTLP sink port; agy: an app log inside the call's own workdir)."""
        task.tag = tag or uuid.uuid4().hex
        opts = {"tag": task.tag}
        pc = providers._pcfg(cfg, task.seat.get("provider"))
        if providers.signals_on(cfg) and not task.seat.get("_salvage"):
            if pc.get("progress_signal") == "otel":
                try:
                    from . import otlp_sink
                    task.sink = otlp_sink.Sink()
                    opts["otlp_port"] = task.sink.port
                except OSError:
                    task.sink = None              # no signal: the hedge starts on time (the safe side)
            elif pc.get("progress_signal") == "applog":
                opts["log_path"] = os.path.join(wd, "applog.log")
        return opts

    def start(task, prompt_text, kind, tag=None, capture=None):
        wd = os.path.join(work_root, _safe(task.key))
        os.makedirs(wd, mode=0o700, exist_ok=True)
        pdata = prompt_text.encode("utf-8")
        ppath = wd + ".prompt.md"
        _write(ppath, pdata)
        task.prompt_bytes = len(pdata)
        task.prompt_text = prompt_text
        task.answer_check = acfg if acfg["enabled"] else None     # every call of the run: seats, chair and extras
        tasks[task.key] = task
        opts = None
        try:
            limits, task.mode, task.deadline_s, task.deadline_source = _limits(
                cfg, task.seat, kind, log_path, len(pdata))
            # build first: an adapter may stash per-launch state (codex -o last.txt) that
            # its extractor reads, and the launch carries final_text_path
            if task.mode == "watch":
                opts = watch_opts(task, wd, tag)
                launch = task.adapter.build(task.seat, ppath, wd, limits, cfg, launch_opts=opts)
                # D18 (row 31): a watch call outlives the runner; its reaper does not kill it when the runner
                # dies (reap.py pops the variable before the CLI starts, so the CLI's env is unchanged)
                launch.env = dict(launch.env if launch.env is not None else os.environ)
                launch.env["COUNCIL_REAP_DETACH"] = "1"
                # kept on disk (O_EXCL, mode 600): the raw survives the runner (D17 observer_lost), and so do the
                # original's stdout and every call's stderr, which the orphan sweep (row 40) classifies. Names carry
                # the call's tag: a run() on the same out dir never collides with an earlier call's capture.
                cbase = os.path.join(out_dir, "chair" if (task.parent or task).kind == "chair" else "seats")
                cname = "%s.%s.%s" % (_safe((task.parent or task).seat["id"]), task.call, task.tag[:12])
                os.makedirs(cbase, mode=0o700, exist_ok=True)
                launch.stdout_path = capture or os.path.join(cbase, cname + ".stdout")
                launch.stderr_path = os.path.join(cbase, cname + ".stderr")
                task.private_caps = [launch.stderr_path] + ([] if capture else [launch.stdout_path])
                status_file = os.path.join(watch_dir(state_dir), task.tag + ".end.json")
                launch.env["COUNCIL_REAP_STATUS"] = status_file
                soft = getattr(limits, "soft_at_s", None) or (task.parent.hedge_at if task.parent is not None
                                                              else None)
                task.soft_at_s = soft if isinstance(soft, (int, float)) and soft > 0 else None
                register_watch_call(state_dir, {
                    "tag": task.tag, "run_id": run_id, "out_dir": out_dir, "seat": (task.parent or task).seat["id"],
                    "call": task.call, "key": task.key, "kind": task.kind, "seat_spec": task.seat,
                    "runner_pid": os.getpid(), "runner_start": proc_start(os.getpid()),
                    "started_at": _iso_now(), "started_epoch": time.time(),
                    "run_t0_epoch": time.time() - (time.monotonic() - t0),
                    "stdout": launch.stdout_path, "stderr": launch.stderr_path, "status_file": status_file,
                    "final_text_path": launch.final_text_path, "work_root": work_root, "workdir": wd,
                    "soft_at_s": task.soft_at_s})
            else:
                launch = task.adapter.build(task.seat, ppath, wd, limits, cfg)   # e1fd082's call, unchanged
            task.launch = launch
            task.extractor = _extractor(task.adapter, task.seat)
        except Exception as e:
            _close_sink(task)
            task.done = True
            task.status = "build_error"
            task.detail = type(e).__name__
            task.result = None
            task.started = time.monotonic()
            log_task(task)
            return False
        task.started = time.monotonic()
        if task.mode == "watch":
            if task.kind == "seat":
                task.hedge_at = getattr(limits, "soft_at_s", None)
            if getattr(limits, "sample_s", None):
                provider = task.seat.get("provider")
                sources = {"otlp_events": task.sink.events} if task.sink is not None else {}
                try:
                    board.add(task.key, make_liveness(provider, task.tag, _liveness_policy(cfg, provider), sources,
                                                      (opts or {}).get("log_path")))
                except Exception:
                    pass                          # no evidence: hedge-only timing, nothing is reaped early
        sup.start(task.key, launch, limits, on_output=task.on_output)
        return True

    def start_salvage(task):
        """Launch the ONE salvage call for a seat or the chair that ended without a usable answer.
        -> its key or None."""
        if not _salvage_eligible(task, scfg):
            return None
        task.salvage_started = True
        chair = task.kind == "chair"
        try:
            sseat = task.adapter.salvage_seat(task.seat, cfg, scfg["chair_max_output_tokens" if chair
                                                                     else "max_output_tokens"])
            if chair:
                sseat["id"] = "chair-salvage"     # not "chair": adapters would pick the chair budget keys again
        except Exception:
            task.salvage = {"status": "build_error", "detail": "salvage_seat_failed"}
            return None
        sv = _Task(task.key + "~salvage", "salvage", sseat, task.adapter)
        sv.parent = task
        prompt = _salvage_prompt(task, scfg["max_partial_chars"])
        salvage_tasks[task.key] = sv
        if not start(sv, prompt, "chair_salvage" if chair else "salvage"):
            task.salvage = {"status": "build_error", "detail": sv.detail}
            return None
        return sv.key

    def finish_salvage(sv):
        task = sv.parent
        r = sv.result
        task.salvage = {"status": sv.status, "detail": sv.detail, "model": sv.seat.get("model"),
                        "mode": sv.mode, "total_s": r.total_s if r else None,
                        "out_bytes": r.out_bytes if r else len(sv.raw),
                        "truncated": bool(r.truncated) if r else False,
                        "usage": _usage_of(sv),
                        "paths": {"raw": ("chair/salvage.raw" if task.kind == "chair"
                                          else "seats/%s.critique.salvage.raw" % _safe(task.seat["id"])
                                          if task.kind == "critique"
                                          else "seats/%s.salvage.raw" % _safe(task.seat["id"]))}}
        task.salvage.update(_or_ids(sv))                 # the salvage call's own stream, never its parent's
        record_or_end(sv)
        if sv.status == "ok":
            task.salvaged_from = task.status
            task.salvaged = True
            was_ok = task.status == "ok"
            task.status, task.text, task.detail = "ok", sv.text, "salvaged"
            task.answered_by = "salvage"
            if not was_ok and task.kind == "seat":
                state["ok"] += 1
        if task.kind == "seat":
            write_seat(task)
        elif task.kind == "critique":
            write_critique(task)

    # ---- row 30: hedge, fallback, pending, late --------------------------------------------------------
    def no_retry(seat):
        """D47: a call that hit the quota means no further call against the same quota."""
        sts = [(seat.own or {}).get("status"), (seat.salvage or {}).get("status")]
        sts += [c.status for c in (seat.hedge, seat.fallback, seat.standin) if c is not None and c.done]
        return any(s in NO_RETRY_STATUSES for s in sts)

    def running_calls(seat):
        return [c for c in (seat, seat.hedge, seat.fallback, seat.standin) if c is not None
                and c.started is not None and not c.done]

    def note_quota(task):
        """D47: record the provider's reset time (preflight then marks it unavailable; the next run skips it)."""
        if task.status != "quota_exhausted":
            return
        try:
            until = _reset_iso(task.extractor.error() if task.extractor is not None else task.detail)
            if until:
                providers.record_quota(cfg, task.seat.get("provider"), until)
        except Exception:
            pass

    def record_call(task, **kw):
        seat = task.parent if task.parent is not None else task
        rec = {"seat": seat.seat["id"], "call": task.call, "key": task.key, "tag": task.tag}
        rec.update(kw)
        _append_sync(calls_path, rec)

    def record_or_end(task, **kw):
        """D56: an OpenRouter original's or salvage call's end in calls.jsonl, with its serving provider and
        generation id (P26: and its output_tok_s and endpoint quantization, `_or_end`). Other providers' originals and
        salvages keep having no record there."""
        ids = _or_end(task)
        if ids:
            r = task.result
            record_call(task, status=task.status, detail=task.detail, ended_at=_iso_now(),
                        exit_code=r.exit_code if r else None, **kw, **ids)

    def launch_extra(seat, name, prompt, kind, sseat=None):
        """Start the seat's hedge or fallback: recorded in calls.jsonl (fsync) BEFORE the launch (D17).
        -> (task, started)."""
        sid = _safe(seat.seat["id"])
        call = _Task(seat.key + "~" + name, name, sseat if sseat is not None else seat.seat, seat.adapter)
        call.parent = seat
        call.tag = uuid.uuid4().hex
        setattr(seat, name, call)
        record_call(call, started_at=_iso_now(),
                    after_s=round(time.monotonic() - seat.started, 3) if seat.started else None)
        capture = (os.path.join(out_dir, "chair", name + ".raw") if seat.kind == "chair"
                   else os.path.join(out_dir, "seats", "%s.%s.raw" % (sid, name)))
        ok = start(call, prompt, kind, tag=call.tag, capture=capture)
        return call, ok

    def start_hedge(seat):
        """D11/D12: one parallel full-effort call with the same build and the original advisor prompt.
        -> keys to wait for."""
        if seat.hedge is not None or "hedge" in seat.prior or seat.adapter is None or seat.resolved:
            return []
        if no_retry(seat):
            return []
        call, ok = launch_extra(seat, "hedge", seat.prompt_text, "hedge")
        if ok:
            return [call.key]
        return finish_extra(call)                 # build_error: terminal with no answer

    def fast_fail(seat):
        """Fallback-chain row 1: a seat call of an adapter with `fast_fail_fallback` (OpenRouter) that ended failed with no
        partial, no salvage (not eligible, or off) and no quota stop. Nothing is left to salvage, so the [fallback] call
        runs the full prompt again down the adapter's chain. Never the chair; never a hedged seat; once per seat."""
        if seat.kind != "seat" or seat.hedgeable or not getattr(seat.adapter, "fast_fail_fallback", False):
            return False
        if seat.status == "ok" or seat.result is None or seat.salvage_started or seat.salvage is not None:
            return False
        if seat.status in ("model_mismatch", "build_error", "signal_kill", "pending"):
            return False
        if (seat.partial or "").strip() and seat.status != "invalid_answer":    # row 2: markup is nothing to salvage
            return False
        return "standin" not in seat.prior

    def start_fast_fallback(seat):
        """Step 1 of the chain: the rerouted call (the seat's model, `order` dropped, capped max_tokens), full effort,
        the original prompt. Counts toward quorum like any seat answer. -> keys to wait for."""
        if no_retry(seat):
            return []
        try:
            chain = seat.adapter.fallback_chain(seat.seat, cfg)
        except Exception:
            return []
        if not chain:
            return []
        seat.chain = chain
        call, ok = launch_extra(seat, "fallback", seat.prompt_text, "fallback", sseat=chain[0])
        if ok:
            return [call.key]
        return finish_extra(call)

    def start_standin(seat):
        """Step 2 of the chain: one call to the seat's `fallback_model`, full effort, the original prompt.
        -> keys to wait for ([] when the seat has no stand-in)."""
        chain = seat.chain or []
        if len(chain) < 2 or seat.standin is not None or "standin" in seat.prior or no_retry(seat):
            return []
        call, ok = launch_extra(seat, "standin", seat.prompt_text, "standin", sseat=chain[1])
        if ok:
            return [call.key]
        return finish_extra(call)

    def start_fallback(seat):
        """D24: the ONE extra call, only once the original AND the second call (the hedge, or a stream seat's /
        the chair's salvage) are both terminal with no usable answer; never while either is still working.
        Form: the salvage form (D14) with the longest partial of the seat's calls. -> keys to wait for."""
        if not fb_on or seat.fallback is not None or "fallback" in seat.prior or seat.adapter is None:
            return []
        if seat.resolved or not seat.done:
            return []
        if fast_fail(seat):
            return start_fast_fallback(seat)
        if seat.kind == "seat" and seat.hedgeable:
            h = seat.hedge
            if h is None and "hedge" not in seat.prior:
                return []                          # the hedge has not had its turn
            if h is not None and (not h.done or h.status == "ok"):
                return []
            srcs = [((seat.own or {}).get("status"), (seat.own or {}).get("partial"))]
            if h is not None:
                srcs.append((h.status, h.partial))
        else:
            if seat.status == "ok" or seat.salvage is None or seat.salvage.get("status") == "ok":
                return []
            sv = salvage_tasks.get(seat.key)
            if sv is not None and not sv.done:
                return []
            srcs = [((seat.own or {}).get("status", seat.status), (seat.own or {}).get("partial", seat.partial))]
        if no_retry(seat):
            return []
        status, part = max(srcs, key=lambda x: len((x[1] or "").strip()))
        chair = seat.kind == "chair"
        try:
            fseat = seat.adapter.salvage_seat(seat.seat, cfg, scfg["chair_max_output_tokens" if chair
                                                                    else "max_output_tokens"])
            if chair:
                fseat["id"] = "chair-salvage"
        except Exception:
            return []
        prompt = _salvage_prompt(seat, scfg["max_partial_chars"], partial=part or "", status=status)
        call, ok = launch_extra(seat, "fallback", prompt, "chair_fallback" if chair else "fallback", sseat=fseat)
        if ok:
            return [call.key]
        return finish_extra(call)

    def record_late(seat, name, text, raw=None):
        """D18 / owner ruling 3: an answer after the chair started. Kept and flagged; the chair never re-runs."""
        now = time.monotonic()
        at = round(now - t0, 3)
        sid = _safe(seat.seat["id"])
        seat.late.append({"call": name, "at_s": at})
        state["late"].append({"seat": seat.seat["id"], "call": name, "at_s": at})
        _write(os.path.join(out_dir, "seats", "%s.%s.late.md" % (sid, name)), text or "")
        if raw is not None:
            _write(os.path.join(out_dir, "seats", "%s.%s.late.raw" % (sid, name)), bytes(raw))
        texts = state["late_text"].setdefault(sid, [])
        texts.append("## %s (late: answered %.1f s into the run, after the chair started; not used by the chair)"
                     "\n\n%s\n" % (name, at, (text or "").strip()))
        _write(os.path.join(out_dir, "seats", sid + ".late.md"), "\n".join(texts))
        state["footers"].append((seat.seat["id"], name, now))
        flush_footers()

    def flush_footers():
        """Append `> LATE` footers to chair/synthesis.md once it exists (answers during the chair wait for it)."""
        if state["synth_at"] is None or not state["footers"]:
            return
        lines = []
        for sid, name, when in state["footers"]:
            d = when - state["synth_at"]
            when_txt = ("%.1f s after synthesis" % d if d >= 0 else
                        "%.1f s before synthesis, while the chair ran" % -d)
            lines.append("> LATE: seat %s (%s) answered at %s; not used by the chair\n" % (sid, name, when_txt))
        state["footers"] = []
        path = os.path.join(out_dir, "chair", "synthesis.md")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        except OSError:
            return
        try:
            _write_all(fd, ("\n" + "".join(lines)).encode("utf-8"))
        finally:
            os.close(fd)

    def late_pending():
        """Calls still running after the chair started (D18): [{seat, call}]."""
        if not state["frozen"]:
            return []
        out = []
        for t in order:
            for name, c in (("original", t), ("hedge", t.hedge), ("fallback", t.fallback), ("standin", t.standin)):
                if c is not None and c.started is not None and not c.done:
                    out.append({"seat": t.seat["id"], "call": name})
        return out

    def snap_progress(task):
        if task.mode != "watch":
            return
        n = None
        if task.sink is not None:
            try:
                n = len(task.sink.events() or [])
            except Exception:
                n = None
        p = _progress_of(sup.liveness(task.key), n)
        if p is not None:
            task.progress = p

    def sweep_tagged(task, sig):
        """codex's native child has its own pgid (row 27): reach every process tagged with the call's tag."""
        if task.mode != "watch" or not task.tag:
            return
        try:
            pids = netwatch.tagged_pids(task.tag)
        except Exception:
            return
        for pid in pids:
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def notify_ready(code, reason=None):
        """on_ready fires once: the caller's answer is known; this process keeps supervising (D18)."""
        if state["ready"] is not None:
            return
        pend = [t.seat["id"] for t in order if t.pending]
        info = {"code": code, "out_dir": out_dir, "runner_pid": os.getpid(),
                "synthesis": (os.path.join(out_dir, "chair", "synthesis.md")
                              if state["synth_at"] is not None else None),
                "pending": pend, "late_pending": late_pending(),
                "below_quorum": bool(state["below_quorum"]), "ok": state["ok"], "need": need,
                "reason": reason, "work_root": work_root}
        state["ready"] = info
        if on_ready is not None:
            try:
                on_ready(code, info)
            except Exception:
                pass

    def settle(seat):
        """Seat view of a hedged seat from its calls: the first usable answer, precedence original > hedge >
        fallback (D13/D33). After the chair started the view is frozen; a later answer is late (D18)."""
        if seat.frozen is not None:
            for k, v in seat.frozen.items():
                setattr(seat, k, v)
            seen = {e["call"] for e in seat.late}
            own = seat.own or {}
            for name, c in (("original", seat), ("hedge", seat.hedge), ("fallback", seat.fallback)):
                if c is None or not c.done or name in seen or name == seat.frozen.get("answered_by"):
                    continue
                ok = own.get("status") == "ok" if name == "original" else c.status == "ok"
                if ok:
                    record_late(seat, name, own.get("text") if name == "original" else c.text,
                                seat.raw if name == "original" else c.raw)
            write_seat(seat)
            return
        own = seat.own or {}
        pick = None
        if own.get("status") == "ok":
            pick = ("original", own.get("text"), own.get("detail"))
        elif seat.hedge is not None and seat.hedge.done and seat.hedge.status == "ok":
            pick = ("hedge", seat.hedge.text, "hedge")
        elif seat.fallback is not None and seat.fallback.done and seat.fallback.status == "ok":
            pick = ("fallback", seat.fallback.text, "fallback")
        if pick is not None:
            name, text, detail = pick
            if not seat.resolved:
                seat.resolved = True
                state["ok"] += 1
            seat.answered_by = name
            seat.status, seat.text, seat.detail = "ok", text, detail
            seat.salvaged = name == "fallback"
            seat.salvaged_from = (own.get("status") or "no_answer") if name == "fallback" else None
        elif running_calls(seat):
            if not seat.done:
                seat.status = "running"
        elif seat.hedge is not None or seat.fallback is not None or seat.prior:
            seat.detail = "; ".join("%s %s" % (n, s) for n, s in
                                    (("original", own.get("status")),
                                     ("hedge", seat.hedge.status if seat.hedge else None),
                                     ("fallback", seat.fallback.status if seat.fallback else None)) if s)
            seat.status = "unresolved"
        write_seat(seat)

    def apply_fallback(seat, call):
        """A stream / unhedged seat's or the chair's fallback ended (original and salvage already failed)."""
        chain_call = bool(seat.chain)
        first = seat.own or {}
        if call.status == "ok":
            if chain_call:
                # the full-effort chain: a retry of the same model, or its stand-in. Not the short salvage-form answer.
                reason = ("%s: %s" % (first.get("status"), first.get("detail")) if first.get("detail")
                          else str(first.get("status")))
                seat.recovered = {"call": call.call, "from_status": first.get("status"),
                                  "from_detail": first.get("detail")}
                if call.salvaged:
                    seat.recovered["salvaged"] = True
                if call.call == "standin":
                    seat.substituted = {"model": call.seat.get("model"), "reason": reason}
            else:
                seat.salvaged_from = seat.status
                seat.salvaged = True
            seat.status, seat.text, seat.detail = "ok", call.text, call.call if chain_call else "fallback"
            seat.answered_by = call.call
            if seat.kind == "seat" and not seat.resolved and not state["frozen"]:
                seat.resolved = True
                state["ok"] += 1
        else:
            if chain_call and call.call == "fallback":
                new = start_standin(seat)
                if new:
                    if seat.kind == "seat":
                        write_seat(seat)
                    return new
            if chain_call:
                steps = [("original", first.get("status")), ("fallback", seat.fallback.status if seat.fallback else None),
                         ("standin", seat.standin.status if seat.standin else None)]
                seat.detail = "; ".join("%s %s" % (n, v) for n, v in steps if v)
            else:
                seat.detail = "%s; salvage %s; fallback %s" % (seat.status, (seat.salvage or {}).get("status"),
                                                              call.status)
            seat.status = "unresolved"
        if seat.kind == "seat":
            write_seat(seat)
        return []

    def finish_extra(call):
        """A hedge or fallback call ended (or failed to build). -> keys to wait for."""
        seat = call.parent
        _close_sink(call)
        record_call(call, status=call.status, detail=call.detail, ended_at=_iso_now(), **_or_end(call))
        base = (os.path.join(out_dir, "chair", call.call) if seat.kind == "chair"
                else os.path.join(out_dir, "seats", "%s.%s" % (_safe(seat.seat["id"]), call.call)))
        if call.launch is None or not getattr(call.launch, "stdout_path", None):
            _write(base + ".raw", bytes(call.raw))
        _write(base + ".md", _md(call))
        if seat.kind == "seat" and seat.hedgeable:
            settle(seat)
            return start_fallback(seat) if call.call == "hedge" else []
        if call.call in ("fallback", "standin"):
            if seat.chain and seat.kind == "seat":
                sk = start_salvage(call)               # a chain call that left a partial gets the ONE salvage (bounded)
                if sk:
                    return [sk]
            return apply_fallback(seat, call)
        return []

    def keep_stderr(task):
        """Fallback-chain row 1: a failed OpenRouter call's stderr (the seat child writes codes and exception names only,
        never a body or a key) is kept next to the seat's files. It used to be lost: the stream-mode seat has no capture."""
        ex, r = task.extractor, task.result
        if not isinstance(ex, providers.OpenRouterExtractor) or r is None or task.status == "ok":
            return
        try:
            text = ex.stderr_text()
            if text:
                seat = task.parent if task.parent is not None else task
                where = "chair" if seat.kind == "chair" else "seats"
                _write(os.path.join(out_dir, where, "%s.%s.stderr" % (_safe(seat.seat["id"]), task.call)), text)
        except Exception:
            pass

    def finished(task):
        """One call ended. -> keys of the calls it started (salvage, hedge, fallback, standin)."""
        _classify(task)
        keep_stderr(task)
        snap_progress(task)
        sweep_tagged(task, signal.SIGKILL)      # its terminal state: nothing tagged with it may outlive it
        if task.mode == "watch":
            unregister_watch_call(state_dir, task.tag)      # seen by this runner: not an orphan
        _close_sink(task)
        note_quota(task)
        log_task(task)
        if task.kind in ("seat", "chair"):
            record_or_end(task)                 # before a salvage can change task.status
        if task.kind == "critique":
            # round 2 (--depth standard): its own path. The ONE salvage like a seat; never a hedge, fallback, stand-in or
            # fast-fail chain, never state["ok"] (quorum is round 1's), and a failure never changes the run's exit code
            record_or_end(task)
            sk = start_salvage(task)
            write_critique(task)
            return [sk] if sk else []
        if task.kind == "salvage":
            finish_salvage(task)
            if task.parent.kind == "critique":
                return []
            if task.parent.kind in ("fallback", "standin"):          # the salvage of a chain call: on to the next step
                return apply_fallback(task.parent.parent, task.parent)
            return start_fallback(task.parent)
        if task.kind in ("hedge", "fallback", "standin"):
            return finish_extra(task)
        task.own = {"status": task.status, "detail": task.detail, "text": task.text, "partial": task.partial}
        if task.kind == "seat" and task.hedgeable:
            task.hedge_next = None
            new = []
            if task.status != "ok" and task.hedge is None:
                new += start_hedge(task)           # ended with no answer before its hedge: the hedge starts now
            settle(task)
            return new + start_fallback(task)
        if task.kind == "seat":
            if task.status == "ok":
                state["ok"] += 1
            write_seat(task)
        sk = start_salvage(task)
        if sk:
            if task.kind == "seat":
                write_seat(task)     # the failed seat is on disk now; rewritten if the salvage answers
            return [sk]
        return start_fallback(task)

    def on_notice(item):
        task = tasks.get(item.key)
        if (item.kind == "soft" and task is not None and task.kind == "seat" and task.hedgeable
                and not task.done and task.hedge is None):
            task.hedge_next = time.monotonic()

    def check_hedges():
        """D12: start the hedge at hedge_at, unless an ADOPTED signal showed progress in the last quiet_s; then
        re-check every recheck_s, up to max_defer x hedge_at, and start it regardless."""
        new = []
        now = time.monotonic()
        for t in order:
            if t.hedge_next is None or now < t.hedge_next:
                continue
            if t.done or t.hedge is not None or t.resolved:
                t.hedge_next = None
                continue
            limit = t.started + hcfg["max_defer"] * (t.hedge_at or 0.0)
            v = sup.liveness(t.key)
            recent = getattr(v, "progress_recent", None)
            try:
                busy = bool(recent(hcfg["quiet_s"])) if callable(recent) else False
            except Exception:
                busy = False
            if busy and now < limit:
                t.hedge_deferred += 1
                t.hedge_next = min(now + hcfg["recheck_s"], limit)
                continue
            t.hedge_next = None
            new += start_hedge(t)
        return new

    def long_watch_now():
        """D58: the watch calls still running past LONG_WATCH_FACTOR x their soft deadline, with their age now."""
        out = []
        for key, e in state["long_watch"].items():
            t = tasks.get(key)
            if t is None or t.done:
                continue
            out.append(dict(e, age_s=round(time.monotonic() - t.started, 1)))
        return out

    def check_long_watch():
        """D58 (row 47): list a watch call once it has run longer than LONG_WATCH_FACTOR x its soft deadline: a
        runner.log line and summary.json `long_watch[]` (rewritten now when the caller already has its answer).
        Kills nothing (owner rule 1); the way to end it is `kill -TERM <runner_pid>`."""
        new = False
        for t in list(tasks.values()):
            soft = getattr(t, "soft_at_s", None)
            if t.mode != "watch" or t.done or t.started is None or not soft or t.key in state["long_watch"]:
                continue
            age = time.monotonic() - t.started
            if age <= LONG_WATCH_FACTOR * soft:
                continue
            sid = (t.parent or t).seat["id"]
            state["long_watch"][t.key] = {"seat": sid, "call": t.call, "soft_s": round(soft, 1),
                                          "over_at_s": round(time.monotonic() - t0, 1)}
            new = True
            try:
                sys.stderr.write("long_watch: seat %s call %s run %s age %.0f s, over %gx its soft deadline (%.1f s): "
                                 "still running; nothing is killed. To end it: kill -TERM %d (the runner)\n"
                                 % (sid, t.call, run_id, age, LONG_WATCH_FACTOR, soft, os.getpid()))
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
        if new and state["ready"] is not None:
            summary(state["ready"]["code"] if state["d50"] is None else 4)

    def reap_terminal():
        """D15/D30: a watch call is reaped only on a proven terminal verdict (a gate terminal line, or adopted
        negative evidence); never on elapsed time or silence."""
        for key in sup.running():
            t = tasks.get(key)
            if t is None or t.mode != "watch" or t.done or getattr(t, "terminated", False):
                continue
            v = sup.liveness(key)
            reason = getattr(v, "terminal", None)
            if isinstance(reason, str) and reason in _liveness.REASONS \
                    and getattr(v, "terminal_source", None) in ("line", "no_connection"):
                t.terminated = sup.terminate(key, reason) or getattr(t, "terminated", False)
                if t.terminated:
                    sweep_tagged(t, signal.SIGTERM)   # codex's native child is outside the reaper's group

    def pending_ready(seat):
        """D39: the hedge has run hedge_at s with no answer from either call (and no fallback is running)."""
        h = seat.hedge
        if h is None or h.started is None or seat.fallback is not None or seat.hedge_at is None:
            return False
        return time.monotonic() - h.started >= seat.hedge_at

    def exempt(key, need_answer=True):
        """A running call the seat phase does not wait for: a resolved hedged seat's other call (D13), or a
        pending seat's calls once at least one seat has answered (D39; D42 below quorum). Never a stream seat,
        a salvage or a fallback call. need_answer=False: the D50 test (pending, whatever the answer count)."""
        t = tasks[key]
        s = t.parent if t.kind == "hedge" else t
        if t.kind not in ("seat", "hedge") or s is None or not s.hedgeable:
            return False
        if s.resolved:
            return True
        return (state["ok"] >= 1 or not need_answer) and pending_ready(s)

    def check_d50(waiting):
        """D50: zero answers, and every call the seat phase still waits for belongs to a pending seat. The caller
        gets exit 4 `pending: no answers yet` now (on_ready); this runner keeps the calls alive and runs the chair
        when the first answer arrives (summary.late_synthesis)."""
        if state["d50"] is not None or state["ok"] > 0 or not waiting:
            return
        if not all(exempt(k, need_answer=False) for k in waiting):
            return
        pend = []
        for t in order:
            if t.hedgeable and not t.resolved and running_calls(t) and pending_ready(t):
                t.pending = True
                t.status = "pending"
                t.detail = "no answer yet; its calls keep running and are not killed (D39/D50)"
                write_seat(t)
                pend.append(t.seat["id"])
        state["d50"] = {"pending": pend, "at_s": round(time.monotonic() - t0, 3)}
        state["late_synthesis"] = {"status": "waiting", "reason": "pending: no answers yet"}
        summary(4)
        notify_ready(4, reason="pending: no answers yet")

    def drive(running_keys, quorum=False, wait_all=False):
        """Poll until every key in running_keys has finished (and every call they start), except the calls
        `exempt` lets the chair go without (wait_all: none). Nothing is killed here but by the grace cut
        (stream seats, cut_stream_stragglers only) and proven terminal verdicts (watch calls)."""
        grace_at = None
        waiting = set(running_keys)
        while (waiting if wait_all else any(not exempt(k) for k in waiting)):
            for item in sup.poll(POLL_S):
                if isinstance(item, Notice):
                    on_notice(item)
                    continue
                key, res = item
                task = tasks[key]
                task.result = res
                task.done = True
                mine = key in waiting
                waiting.discard(key)
                new = finished(task)
                if mine:
                    waiting.update(new)        # else (a seat call ending during the chair): picked up later
            waiting.update(check_hedges())
            reap_terminal()
            check_long_watch()
            status_tick()
            if quorum:
                check_d50(waiting)
            if quorum and state["quorum_at"] is None and state["ok"] >= need:
                state["quorum_at"] = round(time.monotonic() - t0, 3)
                grace_at = time.monotonic() + grace_s
            if grace_at is not None and waiting and time.monotonic() >= grace_at:
                for key in list(waiting):
                    if tasks[key].kind in ("salvage", "fallback", "standin"):
                        continue            # short, bounded by [salvage] timeout; a cut would waste it
                    if tasks[key].mode in ("buffered", "watch"):
                        continue            # no partial to keep: its own deadline/ceiling decides
                    if not cut_stream:
                        continue            # still producing: its idle timeout / ceiling decides
                    tasks[key].cut_requested = True
                    sup.cut(key)
                grace_at = float("inf")

    def critique_round():
        """--depth standard: round 2, between the freeze and the chair. Every seat that ANSWERED round 1 reads the other
        answering seats' answers (as Seat A/B/..., never by id) and writes a short critique. Never a vote, never a gate:
        needs two answers, a failed critique costs nothing but itself, and the run's exit code is round 1's and the
        chair's. -> the letter map when the round ran, else None (the chair then gets the quick prompt)."""
        letters = _critique.letters([s["id"] for s in launched])
        info = {"ran": False, "letters": letters}
        state["critique"] = info
        answered = [t for t in order if t.kind == "seat" and t.status == "ok" and (t.text or "").strip()
                    and t.adapter is not None]
        if state["d50"] is not None:
            info["reason"] = "late synthesis (D50): the caller already had exit 4, so no extra round"
        elif len(answered) < _critique.MIN_ANSWERS:
            info["reason"] = "%d seat(s) answered round 1; a critique needs at least %d" % (
                len(answered), _critique.MIN_ANSWERS)
        if info.get("reason"):
            return None
        state["phase"] = "critique"
        t_start = time.monotonic()
        info.update(ran=True, started_s=round(t_start - t0, 3))
        status_tick(force=True)
        keys = []
        for t in answered:
            sid = t.seat["id"]
            cseat = dict(t.seat)
            cseat["_critique"] = True          # runtime marker (underscore key: not drift); the stub adapter reads it
            ct = _Task(t.key + "~critique", "critique", cseat, t.adapter)
            ct.letter, ct.round1, ct.lens = letters[sid], _verdict.parse(t.text), t.lens
            t.critique = ct
            grounded_seat = bool(t.grounding and t.grounding.get("mode") == "grounded")
            if t.grounding is not None:
                ct.grounding, ct.grounding_cfg = dict(t.grounding), cfg
            peers = [(letters[o.seat["id"]], o.lens, o.text) for o in answered if o is not t]
            prompt = critique_prompt(gcritique_tpl if grounded_seat else critique_tpl, qtext, context, ct.letter,
                                     t.text, peers, n, lens=t.lens,
                                     grounded=_grounded.with_probes(gstate, t.grounding, t.seat) if grounded_seat else None)
            if start(ct, prompt, "critique"):
                keys.append(ct.key)
            else:
                write_critique(ct)
        if keys:
            drive(keys)
        info["total_s"] = round(time.monotonic() - t_start, 3)
        return letters

    def close_out(code):
        """Write the summary and tell the caller (on_ready: the launcher returns now, D18); then keep
        supervising, killing nothing, the calls still running (pending seats, a resolved seat's other call)
        until each is terminal: later answers are recorded as late and footed on the synthesis. With
        [hedge]/[fallback] off nothing is running here. After D50 the caller already had exit 4: the run's
        code stays 4 and summary.late_synthesis says what happened."""
        if state["d50"] is not None:
            code = 4
        running = bool(sup.running())
        state["phase"] = "supervising" if running else "done"
        summary(code)
        st["code"], st["ended"] = code, not running
        status_tick(force=True)           # before ready: with nothing running, status.json is final when the caller reads
        notify_ready(code)
        if not running:
            return code                   # nothing written after ready: the caller may read the run dir now
        while sup.running():
            drive(sup.running(), wait_all=True)
            summary(code)
        state["phase"] = "done"
        summary(code)
        st["ended"] = True
        status_tick(force=True)
        return code

    prev_handlers = {}
    try:
        # 4. signal handlers (restored on the way out) ---------------------------------
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                prev_handlers[sig] = signal.getsignal(sig)
            install_signal_handlers(sup, on_signal)
            # D59: a hang-up is not an operator stop. A no-op handler, not SIG_IGN, so nothing the runner
            # starts inherits an ignored HUP.
            signal.signal(signal.SIGHUP, _ignore_hup)

        # D17: calls an earlier run() started on this out dir are lost to this observer
        for sid, recs in sorted(prior_calls.items()):
            for call, rec in sorted(recs.items()):
                if (sid, call, rec.get("key")) not in lost:
                    _append_sync(calls_path, {"seat": sid, "call": call, "key": rec.get("key"),
                                              "tag": rec.get("tag"), "status": "observer_lost",
                                              "ended_at": _iso_now()})

        status_tick(force=True)
        # prompts + launch every seat from the main thread
        keys = []
        for s in launched:
            adapter = _adapter(s.get("provider"))
            task = _Task(s["id"], "seat", s, adapter)
            if s.get("_substituted"):                # preflight put the seat on its stand-in: the chair is told
                task.substituted = dict(s["_substituted"])
                task.recovered = {"call": "standin", "from_status": "preflight", "from_detail": task.substituted["reason"]}
            task.prior = dict(prior_calls.get(s["id"]) or {})
            task.hedgeable = adapter is not None and hedged(cfg, s.get("provider"))
            order.append(task)
            if adapter is None:
                task.status, task.done = "build_error", True
                tasks[task.key] = task
                task.started = time.monotonic()
                continue
            task.lens = _verdict.lens_for(cfg, s)
            if gstate:
                # per call, never per run: only a tool-capable seat's original call carries `_grounded` (salvage, the
                # chair, canaries and API-only seats stay text only); the others are labelled ungrounded
                task.grounding = _grounded.seat_grounding(cfg, s, adapter, gstate)
                task.grounding_cfg = cfg
            if gstate and task.grounding["mode"] == "grounded":
                prompt = advisor_prompt(gadvisor_tpl, qtext, context, s["id"], n, lens=task.lens,
                                         grounded=_grounded.with_probes(gstate, task.grounding, s))
            else:
                prompt = advisor_prompt(advisor_tpl, qtext, context, s["id"], n, lens=task.lens)
            if start(task, prompt, "seat"):
                keys.append(task.key)

        # build_error tasks never reach poll(): write them now
        for task in order:
            if task.done and task.result is None:
                write_seat(task)

        status_tick(force=True)
        # 5. poll, quorum, grace, cut, hedge, pending -------------------------------------------
        drive(keys, quorum=True)

        # 7. quorum check (D42: short of quorum with a pending seat, the chair still synthesises) -----------
        pend = [t for t in order if t.hedgeable and not t.resolved and running_calls(t)]
        if state["d50"] is not None and state["ok"] < 1:
            state["late_synthesis"] = {"status": "no_answer",
                                       "reason": "every pending call ended with no answer; no chair ran"}
            return close_out(4)
        if state["ok"] < need:
            if state["d50"] is None and (not pend or state["ok"] < 1):
                return close_out(3)
            state["below_quorum"] = True

        # freeze what the chair gets (D18): later answers are late; pending seats keep running (D39)
        for t in order:
            if t.hedgeable or t.fallback is not None:
                t.pending = False                 # a D50 pending seat that answered since is not pending
                if not t.resolved and running_calls(t):
                    t.pending = True
                    t.status = "pending"
                    t.detail = "no answer yet; its calls keep running and are not killed (D39)"
                t.frozen = {k: getattr(t, k) for k in _FROZEN}
                write_seat(t)
        state["frozen"] = True
        letters = critique_round() if depth == "standard" else None
        state["phase"] = "chair"
        status_tick(force=True)

        # 8. chair -----------------------------------------------------------------------
        chair_seat = dict(cfg.get("chair") or {})
        chair_seat.setdefault("id", "chair")
        chair_seat["id"] = "chair"
        chair_seat["role"] = "chair"        # adapters pick the chair output budget from this
        answers = _answers(order, skipped, unavail)
        chair_text = chair_prompt(chair_tpl, qtext, answers,
                                  _critiques(order, letters) if letters is not None else None,
                                  _critique_changes(order) if letters is not None else None,
                                  rulings=cfg.get("_rulings"))
        chair_adapter = _adapter(chair_seat.get("provider"))
        chair = _Task("chair", "chair", chair_seat, chair_adapter)
        chair.prior = dict(prior_calls.get("chair") or {})
        state["chair"] = chair
        ok_chair = False
        if chair_adapter is None:
            chair.status, chair.done = "build_error", True
            chair.started = time.monotonic()
        elif start(chair, chair_text, "chair"):
            drive([chair.key])
            ok_chair = chair.status == "ok"
        if chair.salvage_started:
            # both calls' raw output stays on disk, whether or not the salvage produced a synthesis
            _write(os.path.join(out_dir, "chair", "chair.raw"), bytes(chair.raw))
            csv = salvage_tasks.get(chair.key)
            if csv is not None:
                _write(os.path.join(out_dir, "chair", "salvage.raw"), bytes(csv.raw))
        if not ok_chair:
            if state["d50"] is not None:
                state["late_synthesis"] = {"status": "chair_failed", "chair_status": chair.status}
            return close_out(4)
        text = _md(chair) if chair.salvaged else chair.text
        if state["below_quorum"]:
            text = ("> BELOW QUORUM: %d of %d required answers; pending seats: %s (still running, not killed; "
                    "a later answer is appended as late)\n\n%s" % (state["ok"], need,
                                                                   ",".join(t.seat["id"] for t in order if t.pending),
                                                                   text))
        if state["d50"] is not None:
            text = ("> LATE SYNTHESIS: the caller was told `pending: no answers yet` (exit 4) at %.1f s; the first "
                    "answer arrived later and this synthesis was written at %.1f s (D50)\n\n%s"
                    % (state["d50"]["at_s"], time.monotonic() - t0, text))
        _write(os.path.join(out_dir, "chair", "synthesis.md"), text)
        state["synth_at"] = time.monotonic()
        if state["d50"] is not None:
            state["late_synthesis"] = {"status": "ok", "at_s": round(state["synth_at"] - t0, 3),
                                       "md": "chair/synthesis.md", "below_quorum": bool(state["below_quorum"]),
                                       "ok": state["ok"], "need": need}
        flush_footers()
        return close_out(0)
    finally:
        keep_work = False
        if sup.running():
            watch = [k for k in sup.running() if tasks.get(k) is not None and tasks[k].mode == "watch"]
            if not watch:
                sup.kill_all()                    # e1fd082's exit path, unchanged without watch calls
            else:
                keep_work = not _wait_detached(sup, tasks, watch)
        for t in list(tasks.values()):
            _close_sink(t)
            if t.mode == "watch" and (t.done or not keep_work):
                # ended (or ended just now by the wait above): not an orphan; its private captures go
                unregister_watch_call(state_dir, t.tag)
                for cap in t.private_caps:
                    try:
                        os.unlink(cap)
                    except OSError:
                        pass
        for sig, h in prev_handlers.items():
            try:
                signal.signal(sig, h if h is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                pass
        if keep_work:
            # a watch call still runs under its detached reaper and needs its workdir (codex -o file)
            try:
                _write(os.path.join(out_dir, "workdir-kept.txt"), work_root + "\n")
            except OSError:
                pass
        else:
            shutil.rmtree(work_root, ignore_errors=True)


def _wait_detached(sup, tasks, watch):
    """The runner's own exit path (an exception, not a signal) with watch calls still running (D18): kill the
    other calls as before, but never a watch call; keep supervising until each watch call is terminal.
    -> True when every call ended (the workdir may go), False if supervising failed (the watch calls are left
    to their detached reapers and the workdir is kept)."""
    try:
        for key in sup.running():
            if key not in watch:
                sup.terminate(key, "signal_kill")
        while sup.running():
            for item in sup.poll(POLL_S):
                if isinstance(item, Notice):
                    continue
                t = tasks.get(item[0])
                if t is not None and t.mode == "watch" and t.tag:
                    for pid in netwatch.tagged_pids(t.tag):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
        return True
    except Exception:
        return False


def _safe_partial(task):
    try:
        return task.extractor.partial_text() if task.extractor is not None else ""
    except Exception:
        return ""


def _answers(order, skipped, unavailable=()):
    """Chair input: ok seats in full, TRUNCATED partials marked, failed / pending seats status only."""
    parts = []
    for t in order:
        s = t.seat
        label = "seat %s | provider %s | model %s | status %s" % (
            s.get("id"), s.get("provider"), s.get("model"), t.status)
        if t.status == "ok" and t.kind == "seat":
            v = _verdict.parse(t.text)
            if v:
                label += " | verdict %s" % v
        if t.lens:
            label += " | lens %s" % t.lens
        grec = _grounding_rec(t) if t.kind == "seat" else None
        if grec is not None:
            if grec["mode"] == "ungrounded":
                label += " | ungrounded (text only: no file access, its claims about the source are unchecked)"
            elif t.status == "ok":
                label += " | %s%s" % (_grounded.label(grec), _grounded.chair_note(grec))
            else:
                label += " | grounded"
        if t.status == "ok" and t.substituted:
            parts.append("### %s (STAND-IN: the %s seat answered by stand-in %s after its own model %s %s "
                         "(%s); this is a different model's view at full effort, so say so)\n\n%s"
                         % (label.replace("model %s" % s.get("model"), "model %s" % t.substituted.get("model"), 1),
                            s.get("id"), t.substituted.get("model"), s.get("_orig_model") or s.get("model"),
                            "was unavailable" if (t.recovered or {}).get("from_status") == "preflight"
                            else "gave no answer",
                            t.substituted.get("reason"),
                            t.text.strip()))
        elif t.status == "ok" and t.recovered:
            parts.append("### %s (RETRIED from status %s: the seat's first call failed with nothing to salvage; this is "
                         "one full-effort retry of the same model, %s)\n\n%s"
                         % (label, t.recovered.get("from_status"),
                            "completed by one short low-effort follow-up, so weigh it as less considered"
                            if t.recovered.get("salvaged") else "a complete answer", t.text.strip()))
        elif t.status == "ok" and t.salvaged:
            parts.append("### %s (SALVAGED from status %s: this seat's own answer never arrived; the text below "
                         "is from one short follow-up call made without extended reasoning, so weigh it as "
                         "less considered)\n\n%s" % (label, t.salvaged_from, t.text.strip()))
        elif t.status == "ok":
            parts.append("### %s\n\n%s" % (label, t.text.strip()))
        elif t.result is not None and t.result.truncated and t.partial.strip() \
                and t.status != "model_mismatch":
            parts.append("### %s (TRUNCATED, incomplete)\n\n%s" % (label, t.partial.strip()))
        else:
            parts.append("### %s\n\n(no answer)" % label)
    for s in skipped:
        parts.append("### seat %s | provider %s | model %s | status skipped_allowed_missing\n\n(no answer)"
                     % (s.get("id"), s.get("provider"), s.get("model")))
    for s in unavailable:
        parts.append("### seat %s | provider %s | model %s | status skipped_unavailable (out of quota)\n\n(no answer)"
                     % (s.get("id"), s.get("provider"), s.get("model")))
    return "\n\n".join(parts)


def _critiques(order, letters):
    """Chair input for a `--depth standard` run, after the round 1 answers: the letter key, then one block per seat that
    was asked to critique (ok in full, SALVAGED / TRUNCATED marked, failed as '(no critique)'), then the seats that were not."""
    key = ", ".join("Seat %s = seat %s" % (letters[t.seat["id"]], t.seat["id"]) for t in order if t.seat["id"] in letters)
    parts = ["Letter key (the critics saw only the letters, never seat ids, providers or models): %s." % key]
    for t in order:
        ct = t.critique
        sid, lt = t.seat.get("id"), letters.get(t.seat.get("id"))
        if ct is None:
            why = "it gave no round 1 answer" if t.status != "ok" else "the critique round did not reach it"
            parts.append("### no critique by seat %s (Seat %s): %s" % (sid, lt, why))
            continue
        label = "critique by seat %s (Seat %s) | status %s" % (sid, lt, ct.status)
        if ct.status == "ok":
            v2, ch = _verdict.parse(ct.text), _critique.parse_changed(ct.text)
            label += " | round 1 verdict %s -> revised verdict %s" % (ct.round1 or "none given", v2 or "none given")
            label += " | changed view %s" % (ch or "not stated")
        grec = _grounding_rec(ct)
        if grec is not None and grec["mode"] == "grounded":
            label += " | %s%s" % (_grounded.label(grec), _grounded.chair_note(grec)) if ct.status == "ok" else " | grounded"
        if ct.status == "ok" and ct.salvaged:
            parts.append("### %s (SALVAGED from status %s: this seat's own critique never arrived; the text below is from "
                         "one short follow-up call made without extended reasoning, so weigh it as less considered)\n\n%s"
                         % (label, ct.salvaged_from, ct.text.strip()))
        elif ct.status == "ok":
            parts.append("### %s\n\n%s" % (label, ct.text.strip()))
        elif ct.result is not None and ct.result.truncated and (ct.partial or "").strip():
            parts.append("### %s (TRUNCATED, incomplete)\n\n%s" % (label, ct.partial.strip()))
        else:
            parts.append("### %s\n\n(no critique)" % label)
    return "\n\n".join(parts)


class _NoTicks:
    """Stand-in for the progress lib in `find_owner`: only the session id is kept, so no start ticks are read."""
    @staticmethod
    def proc_start_ticks(_pid):
        return None


def _caller_session():
    """P33c (run example-run, row 24): the Claude session id that launched this run, or None.
    Same lookup as the progress mirror's owner (`progress_mirror.find_owner`: parent chain to ~/.claude/sessions/<pid>.json,
    else $CLAUDE_CODE_SESSION_ID); never an error. summary.json `caller_session`; an older summary has no such key."""
    try:
        owner = _progress.find_owner(_NoTicks)
    except Exception:
        return None
    sid = owner.get("session_id") if isinstance(owner, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _critique_changes(order):
    """-> (seat ids whose critique changed their view, number of ok critiques), roster order. The same signals as
    summary.json seats[].critique (`changed_view`, `verdict_change`, see `_critique_record`), one count per seat."""
    changed, n_ok = [], 0
    for t in order:
        ct = t.critique
        if ct is None or ct.status != "ok":
            continue
        n_ok += 1
        if _critique.view_changed(_critique.parse_changed(ct.text),
                                  _critique.verdict_change(ct.round1, _verdict.parse(ct.text))):
            changed.append(t.seat.get("id"))
    return changed, n_ok


def _finish_early(cfg, out_dir, run_id, qdata, roster, skipped, min_ok, code, grace_s=None, unavailable=()):
    """Write summary.json for a run that ends before any launch."""
    data = {
        "run_id": run_id, "question_bytes": len(qdata),
        "caller_session": _caller_session(), "engine_root": _config.ROOT,
        "roster": [{"id": s.get("id"), "provider": s.get("provider"), "model": s.get("model")}
                   for s in roster],
        "seats": [{"id": s.get("id"), "provider": s.get("provider"), "model": s.get("model"),
                   "status": ("skipped_allowed_missing" if s in skipped else
                              "skipped_unavailable" if s in unavailable else "not_launched")}
                  for s in roster],
        "quorum": {"min_ok": min_ok, "ok": 0, "reached_at_s": None, "grace_s": grace_s},
        "chair": None, "cost_reported_usd": None, "cost_unreported": [], "exit_code": code}
    _write(os.path.join(out_dir, "summary.json"), json.dumps(data, indent=1))
    if _status.cfg_of(cfg)["enabled"]:       # live status: a run that ends before any launch still leaves a final file
        _status.write_final(progress=_status.cfg_of(cfg)["progress"], out_dir=out_dir, snap=_status.early_snapshot(
            run_id, _status.title_of(qdata.decode("utf-8", "replace")), "failed", exit_code=code, ended=True,
            seats=[{"id": r["id"], "provider": r.get("provider"), "model": r.get("model"), "call": None, "retry": 0,
                    "phase": "skipped" if r["status"].startswith("skipped") else "failed", "status": r["status"],
                    "elapsed_s": None, "call_elapsed_s": None, "first_byte_s": None, "out_bytes": 0,
                    "last_byte_age_s": None, "detail": None, "substituted": None, "calls": []} for r in data["seats"]],
            quorum={"ok": 0, "min_ok": min_ok, "need": None, "reached_at_s": None}))
    return code
