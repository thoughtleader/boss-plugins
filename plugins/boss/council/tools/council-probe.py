#!/bin/sh
''''exec python3 -Es "$0" "$@" # '''
# ^ sh runs line 2 as `exec python3 -Es "$0" "$@"` (-E ignores PYTHON* variables, -s skips the user site); Python reads it as a string and
#   skips it. No `/usr/bin/env -S` here: GNU env older than 8.30 rejects -S. It works wherever /bin/sh and python3 exist.
__doc__ = """Council probe: one direct salvage call, or one real seat (with its one salvage), on the real adapters.

  council-probe.py salvage (--seat ID|all)... [--chair] [--question-file F] [--answers-dir D]
                   [--partial-file F] [--from-status S] [--context-file F]... [--label L]
                   (--out DIR | --dry-run) [--config P]...
  council-probe.py seat (--seat ID|all)... [--question-file F] [--context-file F]... [--label L]
                   (--out DIR | --dry-run) [--config P]...

salvage: the ONE answer-salvage follow-up call the runner makes for an answerless seat, made directly on a
  synthetic reasoning partial (default: a bat-and-ball reasoning that stops before the answer, prefixed
  providers.THINKING_NOTE). Seat from adapter.salvage_seat(seat, cfg, [salvage] max_output_tokens), prompt
  from council_run._salvage_prompt, limits from council_run._limits(kind "salvage"). --chair does the
  chair salvage instead: the chair seat, [salvage] chair_max_output_tokens, id "chair-salvage", kind
  "chair_salvage", over council_run.chair_prompt(question, _answers(<--answers-dir seats>)).
seat: the real advisor call (advisor_prompt, kind "seat" limits, the seat's own build); when it ends
  answerless (council_run._salvage_eligible) it gets exactly one salvage call, as in a council.

Every call is supervised by supervise.Supervisor with the limits _limits gives (no clock of its own,
nothing cuts a call still producing) and classified by council_run._classify. Output per call, in
--out (dir 700, files 600): <label>.json (status, detail, model_reported, usage, total_s, first_byte_s,
max_gap_s, ceiling, workdir, argv with prompt text as <prompt N bytes>, env limited to ENV_ALLOW, the
OpenRouter request body, answer_head), <label>.raw (stdout as received, kept on any kill or signal),
<label>.md; seat mode adds <label>.salvage.raw. A label that exists gets -2, -3 ... (never overwritten).
Latency lines use kind "probe" (the adaptive deadline reads only kind "seat"). The probe never runs
preflight and never writes the drift snapshot, so a --config overlay can't reach it.

--dry-run builds every target (several --seat, "all", --chair) and prints argv, allowlisted env, limits
and the OpenRouter request body; it launches nothing and costs nothing. A live call takes ONE target.
No key value is ever printed or written: env keys outside ENV_ALLOW are never read out.

Exit: 0 the call (or the seat, after its salvage) has an answer; 1 no answer; 2 usage or config error.
"""
import argparse
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
import types

if sys.version_info < (3, 11):      # before the first tomllib import (3.11+): a clear line, not a ModuleNotFoundError
    sys.stderr.write("council needs Python 3.11+ (found %d.%d.%d); install it and run it with python3.11\n"
                     % tuple(sys.version_info[:3]))
    sys.exit(2)

import tomllib

# C-bytecode: never write __pycache__ into the plugin dir (the entry shebangs run -E, which ignores PYTHONDONTWRITEBYTECODE)
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from councillib import config, council_run, openrouter_seat, providers, timing  # noqa: E402
from councillib.supervise import Supervisor, install_signal_handlers  # noqa: E402

ENV_ALLOW = ("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "API_TIMEOUT_MS", "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS")
HEAD_MAX = 400
LONG_ARG = 512                  # an argv element this long is prompt-sized: never printed
WORK_PREFIX = "council-probe-"  # under $TMPDIR, so secret-scan.sh --leftover (council-*) sees it

DEFAULT_QUESTION = ("A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
                    "How much does the ball cost? Give the amount and one line of working.")
DEFAULT_PARTIAL = providers.THINKING_NOTE + (
    "Let the ball cost x dollars. Then the bat costs x + 1.00, and together they cost x + (x + 1.00) = 1.10.\n"
    "So 2x + 1.00 = 1.10, which gives 2x = 0.10.\n"
    "The quick answer of 10 cents is the trap: a 0.10 ball and a 1.10 bat make 1.20, too much.\n"
    "Now I divide 0.10 by 2 to get the ball's price, then check the total once more before answering")
DEFAULT_CHAIR_PARTIAL = providers.THINKING_NOTE + (
    "I have the seat answers above. First I note which seats completed and which were truncated, salvaged or "
    "failed. Next I compare the recommended answers seat by seat, then where they disagree and which side argues "
    "it better, then what nobody covered. I have not started writing the synthesis yet")
SYNTHETIC_ANSWERS = (
    ("a", "The ball costs $0.05: the bat is then $1.05, and $0.05 + $1.05 = $1.10.\n\nConfidence: high"),
    ("b", "Ball = $0.05. If the ball were $0.10 the bat would be $1.10 and the total $1.20.\n\nConfidence: high"),
)


# ---------------------------------------------------------------- small helpers
def _scrub(text):
    return providers._KEYISH.sub("sk-...", str(text or ""))


def _read(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


def _strip_head(md):
    """Drop the '> FAILED/TRUNCATED/SALVAGED ...' lines a seat .md opens with."""
    lines = md.splitlines()
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    return "\n".join(lines[i:])


def chair_seat(cfg):
    """The chair seat exactly as council_run.run builds it."""
    c = dict(cfg.get("chair") or {})
    c["id"] = "chair"
    c["role"] = "chair"
    return c


def answers_from_dir(d):
    """council_run._answers over a run's seats/ dir (<id>.json + <id>.md), or plain *.md files as ok seats."""
    order = []
    names = sorted(os.listdir(d))
    for name in names:
        if not name.endswith(".json") or name.count(".") != 1:
            continue
        try:
            with open(os.path.join(d, name), "rb") as f:
                rec = json.loads(f.read())
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        md_path = os.path.join(d, name[:-5] + ".md")
        text = _read(md_path) if os.path.isfile(md_path) else ""
        t = council_run._Task(rec["id"], "seat", {"id": rec["id"], "provider": rec.get("provider"),
                                                  "model": rec.get("model")}, None)
        t.status = rec.get("status") or "unknown"
        if t.status == "ok" and text.strip():
            if rec.get("salvaged"):
                t.salvaged, t.salvaged_from = True, rec.get("salvaged_from")
                text = _strip_head(text)
            t.text = text
        elif t.status == "ok":
            t.status = "empty_output"
        elif rec.get("truncated"):
            t.result = types.SimpleNamespace(truncated=True)
            t.partial = _strip_head(text)
        order.append(t)
    if not order:
        for name in names:
            if name.endswith(".md"):
                t = council_run._Task(name[:-3], "seat", {"id": name[:-3], "provider": "unknown",
                                                          "model": "unknown"}, None)
                t.status, t.text = "ok", _read(os.path.join(d, name))
                order.append(t)
    if not order:
        raise ValueError("no seat answers in --answers-dir")
    return council_run._answers(order, [])


def synthetic_answers():
    order = []
    for sid, text in SYNTHETIC_ANSWERS:
        t = council_run._Task(sid, "seat", {"id": sid, "provider": "synthetic", "model": "synthetic"}, None)
        t.status, t.text = "ok", text
        order.append(t)
    return council_run._answers(order, [])


# ---------------------------------------------------------------- one call
class Call:
    """One launch: prompt file, _limits, adapter.build, extractor; then supervise + _classify."""

    def __init__(self, task, prompt, kind, cfg, work_root, log_path):
        self.task, self.kind, self.cfg, self.log_path = task, kind, cfg, log_path
        self.launch = self.limits = None
        self.workdir = work_root
        wd = os.path.join(work_root, council_run._safe(task.key))
        os.makedirs(wd, mode=0o700, exist_ok=True)
        pdata = prompt.encode("utf-8")
        ppath = wd + ".prompt.md"
        council_run._write(ppath, pdata)
        task.prompt_bytes, task.prompt_text = len(pdata), prompt
        try:
            self.limits, task.mode, task.deadline_s, task.deadline_source = council_run._limits(
                cfg, task.seat, kind, log_path, len(pdata))
            self.launch = task.adapter.build(task.seat, ppath, wd, self.limits, cfg)
            task.launch = self.launch
            task.extractor = council_run._extractor(task.adapter, task.seat)
        except Exception as e:
            task.done, task.status, task.result = True, "build_error", None
            task.detail = providers._scrub("%s: %s" % (type(e).__name__, e), 200)
            task.started = time.monotonic()

    @property
    def built(self):
        return self.task.status != "build_error"

    def view(self):
        """What is sent, safe to print: argv (prompt text replaced), allowlisted env, limits, request."""
        t, L = self.task, self.limits
        out = {"call": self.kind, "id": t.seat.get("id"), "provider": t.seat.get("provider"),
               "model": t.seat.get("model"), "role": t.seat.get("role")}
        if not self.built:
            out["build_error"] = t.detail
            return out
        out["limits"] = {"mode": L.mode, "idle_s": L.idle_s, "ceiling_s": L.ceiling_s, "deadline_s": L.deadline_s,
                         "deadline_source": t.deadline_source}
        out["argv"] = redact_argv(self.launch.argv, t.prompt_text)
        out["env"] = {k: self.launch.env[k] for k in ENV_ALLOW if k in (self.launch.env or {})}
        out["stdin"] = ("<prompt %d bytes>" % t.prompt_bytes) if self.launch.stdin_path else None
        req = openrouter_request(self.launch.argv, t.prompt_bytes)
        if req is not None:
            out["request"] = req
        return out

    def run(self, on_signal):
        """Supervise to the end; the limits are the only clock. -> the task, classified."""
        t = self.task
        if not self.built:
            return t
        sup = Supervisor()
        prev = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                prev[sig] = signal.getsignal(sig)
            install_signal_handlers(sup, lambda: on_signal(self))
        try:
            t.started = time.monotonic()
            sup.start(t.key, self.launch, self.limits, on_output=t.on_output)
            while not t.done:
                for _key, res in sup.poll(council_run.POLL_S):
                    t.result, t.done = res, True
                    council_run._classify(t)      # reads codex's -o file: before the workdir goes
        finally:
            if sup.running():
                sup.kill_all()
            for sig, h in prev.items():
                try:
                    signal.signal(sig, h if h is not None else signal.SIG_DFL)
                except (ValueError, OSError):
                    pass
        self.log()
        return t

    def log(self):
        t, r = self.task, self.task.result
        timing.append(self.log_path, {
            "run_id": "probe", "kind": "probe", "provider": t.seat.get("provider"), "model": t.seat.get("model"),
            "mode": t.mode, "prompt_bytes": t.prompt_bytes, "out_bytes": r.out_bytes if r else len(t.raw),
            "first_byte_s": r.first_byte_s if r else None, "max_gap_s": r.max_gap_s if r else None,
            "total_s": r.total_s if r else None, "outcome": t.status, "extensions": r.extensions if r else 0,
            "deadline_s": t.deadline_s, "deadline_source": t.deadline_source})

    def record(self, label):
        t = self.task
        rec = council_run._task_record(t)
        v = self.view()
        rec.update(probe=self.kind, label=label, role=t.seat.get("role"), workdir=self.workdir,
                   ceiling_s=self.limits.ceiling_s if self.limits else None,
                   limits=v.get("limits"), argv=v.get("argv"), env=v.get("env"), stdin=v.get("stdin"),
                   request=v.get("request"), prompt_bytes=t.prompt_bytes,
                   answer_head=_scrub(t.text or "")[:HEAD_MAX],
                   partial_tail=None if t.status == "ok" else _scrub((t.partial or "")[-HEAD_MAX:]),
                   stderr_tail=(providers._scrub(t.result.stderr_tail.decode("utf-8", "replace")[-HEAD_MAX:],
                                                 HEAD_MAX) if t.result and t.result.stderr_tail else None))
        return rec


def redact_argv(argv, prompt):
    p = (prompt or "").strip()
    out = []
    for a in argv:
        a = str(a)
        if (p and p in a) or len(a) > LONG_ARG:
            out.append("<prompt %d bytes>" % len(a.encode("utf-8")))
        else:
            out.append(_scrub(a))
    return out


def openrouter_request(argv, prompt_bytes):
    """The JSON body openrouter_seat.py would POST (its own parse_args + build_body), prompt replaced."""
    argv = [str(a) for a in argv]
    if providers.OPENROUTER_SEAT not in argv:
        return None
    try:
        args = openrouter_seat.parse_args(argv[argv.index(providers.OPENROUTER_SEAT) + 1:])
    except SystemExit:
        return None
    body = openrouter_seat.build_body(args, "<prompt %d bytes>" % prompt_bytes)
    body["key_env"] = args.key_env          # the variable's NAME; its value is never read here
    return body


# ---------------------------------------------------------------- targets
def targets(cfg, args):
    """-> [(target id, seat dict)]; "chair" for the chair."""
    roster = cfg.get("seat") or []
    by_id = {s.get("id"): s for s in roster}
    out = []
    for sid in args.seat or ():
        if sid == "all":
            out += [(s["id"], dict(s)) for s in roster]
        elif sid in by_id:
            out.append((sid, dict(by_id[sid])))
        else:
            raise ValueError("--seat %r is not in the roster (%s)" % (sid, ", ".join(by_id) or "empty"))
    if getattr(args, "chair", False):
        out.append(("chair", chair_seat(cfg)))
    if not out:
        raise ValueError("no target: pass --seat ID (or all)%s" % (" or --chair" if hasattr(args, "chair") else ""))
    return out


def _question(args):
    return _read(args.question_file) if args.question_file else DEFAULT_QUESTION


def _adapter_for(seat):
    a = council_run._adapter(seat.get("provider"))
    if a is None:
        raise ValueError("no adapter for provider %r" % seat.get("provider"))
    return a


def plan_salvage(cfg, args, tid, seat, work_root, log_path):
    """The parent (synthetic, never launched) and its salvage Call, built as start_salvage builds it."""
    scfg = council_run.salvage_cfg(cfg)
    chair = tid == "chair"
    adapter = _adapter_for(seat)
    question = _question(args)
    if chair:
        answers = answers_from_dir(args.answers_dir) if args.answers_dir else synthetic_answers()
        prompt_text = council_run.chair_prompt(council_run._load_template(cfg, "chair.md"), question, answers)
    else:
        prompt_text = council_run.advisor_prompt(council_run._load_template(cfg, "advisor.md"), question,
                                                 council_run._context_block(args.context_file),
                                                 seat["id"], len(cfg.get("seat") or []))
    parent = council_run._Task(seat["id"], "chair" if chair else "seat", seat, adapter)
    parent.prompt_text = prompt_text
    parent.status = args.from_status
    parent.partial = (_read(args.partial_file) if args.partial_file
                      else DEFAULT_CHAIR_PARTIAL if chair else DEFAULT_PARTIAL)
    parent.salvage_started = True
    sseat = adapter.salvage_seat(seat, cfg, scfg["chair_max_output_tokens" if chair else "max_output_tokens"])
    if chair:
        sseat["id"] = "chair-salvage"
    sv = council_run._Task(parent.key + "~salvage", "salvage", sseat, adapter)
    sv.parent = parent
    prompt = council_run._salvage_prompt(parent, scfg["max_partial_chars"])
    return parent, Call(sv, prompt, "chair_salvage" if chair else "salvage", cfg, work_root, log_path)


def plan_seat(cfg, args, tid, seat, work_root, log_path):
    adapter = _adapter_for(seat)
    prompt = council_run.advisor_prompt(council_run._load_template(cfg, "advisor.md"), _question(args),
                                        council_run._context_block(args.context_file), seat["id"],
                                        len(cfg.get("seat") or []))
    task = council_run._Task(seat["id"], "seat", seat, adapter)
    return Call(task, prompt, "seat", cfg, work_root, log_path)


def seat_salvage_call(cfg, task, work_root, log_path):
    """The runner's one salvage for an answerless seat (start_salvage), or None when not eligible."""
    scfg = council_run.salvage_cfg(cfg)
    if not council_run._salvage_eligible(task, scfg):
        return None
    task.salvage_started = True
    try:
        sseat = task.adapter.salvage_seat(task.seat, cfg, scfg["max_output_tokens"])
    except Exception:
        task.salvage = {"status": "build_error", "detail": "salvage_seat_failed"}
        return None
    sv = council_run._Task(task.key + "~salvage", "salvage", sseat, task.adapter)
    sv.parent = task
    return Call(sv, council_run._salvage_prompt(task, scfg["max_partial_chars"]), "salvage", cfg, work_root,
                log_path)


# ---------------------------------------------------------------- output
def _out_dir(path):
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def unique_label(out, base):
    base = council_run._safe(base)
    label, n = base, 1
    while any(os.path.exists(os.path.join(out, label + ext)) for ext in (".json", ".raw", ".md")):
        n += 1
        label = "%s-%d" % (base, n)
    return label


def _write_json(path, data):
    council_run._write(path, json.dumps(data, indent=1, default=str))


def _summary_line(label, rec, path):
    u = rec.get("usage") or {}
    return ("%s: status=%s detail=%s total_s=%s out_tok=%s reasoning_tok=%s cost_usd=%s -> %s\n"
            % (label, rec.get("status"), rec.get("detail"), rec.get("total_s"), u.get("output_tokens"),
               u.get("reasoning_tokens"), u.get("cost_usd"), path))


def _print_view(v, head):
    w = sys.stdout.write
    w("== %s\n" % head)
    w("seat: id=%s role=%s provider=%s model=%s\n" % (v["id"], v.get("role"), v["provider"], v["model"]))
    if "build_error" in v:
        w("BUILD ERROR: %s\n" % v["build_error"])
        return
    L = v["limits"]
    w("limits: mode=%s idle_s=%g ceiling_s=%s deadline_s=%s (%s)\n"           # ceiling None: no ceiling (row 35)
      % (L["mode"], L["idle_s"], "None" if L["ceiling_s"] is None else "%g" % L["ceiling_s"], L["deadline_s"],
         L["deadline_source"]))
    w("argv: %s\n" % shlex.join(v["argv"]))
    w("env: %s\n" % (" ".join("%s=%s" % kv for kv in v["env"].items()) or "(none of %s)" % ", ".join(ENV_ALLOW)))
    w("stdin: %s\n" % (v["stdin"] or "(none)"))
    if "request" in v:
        w("request: %s\n" % json.dumps(v["request"], sort_keys=True))


# ---------------------------------------------------------------- modes
def dry_run(cfg, args):
    log_path = os.path.join(cfg.get("state_dir") or os.path.expanduser("~/.claude-council"), "latency.jsonl")
    rc = 0
    work_root = tempfile.mkdtemp(prefix=WORK_PREFIX)
    try:
        for tid, seat in targets(cfg, args):
            if args.cmd == "salvage":
                _parent, call = plan_salvage(cfg, args, tid, seat, work_root, log_path)
                _print_view(call.view(), "salvage %s (DRY RUN: nothing launched)" % tid)
                rc = rc or (0 if call.built else 1)
                continue
            call = plan_seat(cfg, args, tid, seat, work_root, log_path)
            _print_view(call.view(), "seat %s (DRY RUN: nothing launched)" % tid)
            rc = rc or (0 if call.built else 1)
            # the salvage it would get if it ended answerless (status empty_output, nothing kept)
            t = call.task
            t.status, t.result = "empty_output", types.SimpleNamespace(truncated=False)
            sv = seat_salvage_call(cfg, t, work_root, log_path) if call.built else None
            if sv is not None:
                _print_view(sv.view(), "seat %s -> its one salvage if answerless (DRY RUN)" % tid)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
    return rc


def live(cfg, args):
    tg = targets(cfg, args)
    if len(tg) != 1:
        raise ValueError("a live probe takes exactly one target (got %d); --dry-run shows several" % len(tg))
    tid, seat = tg[0]
    if args.cmd == "seat" and not args.question_file:
        raise ValueError("seat needs --question-file")
    out = _out_dir(args.out)
    label = unique_label(out, args.label or "%s-%s" % (args.cmd, tid))
    p = lambda ext: os.path.join(out, label + ext)          # noqa: E731
    state_dir = cfg.get("state_dir") or os.path.expanduser("~/.claude-council")
    log_path = os.path.join(state_dir, "latency.jsonl")
    work_root = tempfile.mkdtemp(prefix=WORK_PREFIX)
    try:
        if args.cmd == "salvage":
            parent, call = plan_salvage(cfg, args, tid, seat, work_root, log_path)

            def write(c, interrupted=False):
                rec = c.record(label)
                rec["synthetic_parent"] = {"id": parent.seat.get("id"), "kind": parent.kind,
                                           "status_given": parent.status,
                                           "partial_bytes": len(parent.partial.encode("utf-8"))}
                if interrupted:
                    rec["interrupted"] = True
                council_run._write(p(".raw"), bytes(c.task.raw))
                council_run._write(p(".md"), council_run._md(c.task))
                _write_json(p(".json"), rec)
                return rec

            def on_signal(c):
                c.task.status, c.task.partial = "signal_kill", council_run._safe_partial(c.task)
                write(c, interrupted=True)
                shutil.rmtree(work_root, ignore_errors=True)     # the handler ends with os._exit

            call.run(on_signal)
            rec = write(call)
            sys.stdout.write(_summary_line(label, rec, p(".json")))
            return 0 if call.task.status == "ok" else 1

        call = plan_seat(cfg, args, tid, seat, work_root, log_path)
        state = {"sv": None}

        def write_seat(interrupted=False):
            t = call.task
            rec = call.record(label)
            sv = state["sv"]
            if sv is not None:
                rec["salvage_call"] = sv.record(label + ".salvage")
                council_run._write(p(".salvage.raw"), bytes(sv.task.raw))
            if interrupted:
                rec["interrupted"] = True
            council_run._write(p(".raw"), bytes(t.raw))
            council_run._write(p(".md"), council_run._md(t))
            _write_json(p(".json"), rec)
            return rec

        def on_signal(c):
            c.task.status, c.task.partial = "signal_kill", council_run._safe_partial(c.task)
            write_seat(interrupted=True)
            shutil.rmtree(work_root, ignore_errors=True)         # the handler ends with os._exit

        call.run(on_signal)
        sv = seat_salvage_call(cfg, call.task, work_root, log_path) if call.built else None
        if sv is not None:
            state["sv"] = sv
            write_seat()                         # the answerless seat is on disk before its salvage runs
            sv.run(on_signal)
            t, s, r = call.task, sv.task, sv.task.result
            t.salvage = {"status": s.status, "detail": s.detail, "model": s.seat.get("model"), "mode": s.mode,
                         "total_s": r.total_s if r else None, "out_bytes": r.out_bytes if r else len(s.raw),
                         "truncated": bool(r.truncated) if r else False, "usage": council_run._usage_of(s),
                         "paths": {"raw": label + ".salvage.raw"}}
            if s.status == "ok":
                t.salvaged_from, t.salvaged = t.status, True
                t.status, t.text, t.detail = "ok", s.text, "salvaged"
        rec = write_seat()
        sys.stdout.write(_summary_line(label, rec, p(".json")))
        return 0 if call.task.status == "ok" else 1
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


# ---------------------------------------------------------------- CLI
def _parser():
    ap = argparse.ArgumentParser(prog="council-probe.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("salvage", "seat"):
        p = sub.add_parser(name)
        p.add_argument("--seat", action="append", default=[], metavar="ID", help='roster seat id, or "all"; repeatable')
        p.add_argument("--config", action="append", default=[], metavar="P",
                       help="overlay TOML, repeatable; deep-merged over config.toml ([[seat]] replaces the roster)")
        p.add_argument("--question-file", metavar="F")
        p.add_argument("--context-file", action="append", default=[], metavar="F")
        p.add_argument("--out", metavar="DIR", help="result dir (700); files 600")
        p.add_argument("--label", metavar="L", help="file name stem (default <mode>-<id>; -2, -3 ... if taken)")
        p.add_argument("--dry-run", action="store_true", help="print argv/env/limits/request, launch nothing")
        if name == "salvage":
            p.add_argument("--chair", action="store_true", help="the chair salvage (id chair-salvage)")
            p.add_argument("--answers-dir", metavar="D", help="a run's seats/ dir: the chair prompt's answers")
            p.add_argument("--partial-file", metavar="F", help="the reasoning/partial to salvage (as given)")
            p.add_argument("--from-status", default="empty_output", metavar="S",
                           help="the status the salvage prompt says the attempt ended with")
    return ap


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        cfg = config.load(overlays=args.config)
    except (OSError, tomllib.TOMLDecodeError) as e:
        sys.stderr.write("config: %s (%s)\n" % (type(e).__name__, getattr(e, "filename", "") or ""))
        return 2
    if not args.dry_run and not args.out:
        sys.stderr.write("--out DIR is required (or --dry-run)\n")
        return 2
    try:
        return dry_run(cfg, args) if args.dry_run else live(cfg, args)
    except (ValueError, KeyError, OSError) as e:
        sys.stderr.write("probe: %s\n" % providers._scrub("%s: %s" % (type(e).__name__, e), 300))
        return 2


if __name__ == "__main__":
    sys.exit(main())
