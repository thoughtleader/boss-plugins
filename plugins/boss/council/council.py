#!/bin/sh
''''exec python3 -Es "$0" "$@" # '''
# ^ sh runs line 2 as `exec python3 -Es "$0" "$@"` (-E ignores PYTHON* variables, -s skips the user site); Python reads it as a string and
#   skips it. No `/usr/bin/env -S` here: GNU env older than 8.30 rejects -S. It works wherever /bin/sh and python3 exist.
__doc__ = """Council runner CLI.

  council.py preflight [--config P]... [--allow-missing PROV]... [--free-only]
                       [--question-file F [--context-file F]... [--allow-paths]]
  council.py run [--config P]... [--allow-missing PROV]... [--context-file F]... [--allow-paths]
                 [--depth quick|standard] [--lenses on|off] [--lens SEAT=TEXT]... [--out DIR] [--slug S] (--question-file F | -- "<question>")
  council.py ask  (every flag of `run`, plus --rulings-file F; see `council.py ask --help`)
  council.py status [--run DIR] [--watch] [--json] [--config P]...
  council.py drift [--config P]... [--accept [KEY...]] [--record]
  council.py reap --orphans [--config P]...

Exit codes: 0 ok, 1 preflight fail, 2 usage or policy, 3 quorum not met, 4 chair failed, or no answer yet with
pending seats (`pending: no answers yet`, D50), 5 the RUNNER CRASHED (an exception, or it exited 1; a crash is never
exit 1, which means a failed preflight; the cause is in <run dir>/runner.log). A runner killed by a signal exits
128+N (137 kill -9, 143 TERM).
Orphans (D54): every start of council.py sweeps the watch calls whose runner is gone (a preflight WARN row per
call; `drift` prints them on stderr). A finished call has its answer appended as late (ruling 3) and is reaped; a
live one is left running and reported with its age, seat, call and run id. `reap --orphans` runs just that sweep
(exit 0). The sweep never signals a process and never launches a call.
`run` is a launcher (row 31, D18): after the preflight it starts the runner detached (re-parented away from the
caller, its own session, log <run dir>/runner.log), waits until the synthesis is ready, then prints
`synthesis ready: <path>` (plus `below quorum: ...`, `pending seats: <id>,... (runner <pid> keeps supervising)`
or `pending: no answers yet` when they apply), `run dir:` and `exit:`, and returns the run's code. The runner keeps
supervising pending and late calls (never killed) and exits once each is terminal; a late answer is appended to
chair/synthesis.md as a `> LATE` footer. Manual stop: `kill -TERM <runner_pid>` (summary.json `runner_pid`).
D59: closing the chat or terminal that started `run` never stops the runner or its calls: TERM/INT/HUP to the
launcher (or its group) are not forwarded (it prints that the runner keeps running and exits 128+N), its death
is not a stop, and the runner ignores HUP. Only `kill -TERM <runner_pid>` (or INT) stops them.
`drift` (free: --version/--help, agy models, GET /models) compares the effective settings of every seat with
<state_dir>/drift/last.json, the ACCEPTED baseline. Exit 0 first pass or no change, 1 changed (CHANGED / NEW /
GONE / NOT COLLECTED lines). Alarms are sticky: a preflight logs an observation in history.jsonl but never moves
the baseline (only the first one seeds it). `--accept` moves it: with no keys it accepts every pending change, with
keys (exact, or with `*`) only those, and exits 0 when nothing is left raised. `--record` is an alias of a bare
`--accept`.
`run` always runs the full preflight before any council spend: the free checks, then a canary per seat; a seat whose
same canary (provider, model, prompt, launch argv, CLI binary) passed within [canary] cache_s (default 600, 0 = off) shows
as `cached` and is not called again. A FAIL or WARN canary is never cached.
Question path lint (fallback-chain row 2): with a question (`run`, or `preflight --question-file`), every absolute or ~
path it names (/home, ~, /mnt, /tmp, /root, /Users) must be a --context-file or a directory holding one; else a policy
FAIL row per path, `pass --context-file <path>` (exit 1). Seats cannot read disk. `--allow-paths` makes those rows WARN.
`status` (free: reads files, no network, no sweep) prints the live status of a run: a table of seats (phase glyph, current call,
elapsed, bytes, age of the last byte, detail) plus the quorum and chair line, from <run dir>/status.json that the runner rewrites
every state change and at least every [status] interval_s. Default run: the one <state_dir>/current.json points at (if its runner
is alive), else the newest run dir. --watch redraws every 2 s until the run ends (or its runner is gone); --json prints the file
(plus age_s, runner_alive, stale). Exit 0, or 1 when there is no run / no status.json.
Verdicts and lenses (row 12, display only): each advisor ends with `Verdict: APPROVE|REVISE|BLOCK|n/a` (parsed into
summary.json seats[].verdict and verdicts{}, shown in `status`, passed to the chair; never a gate, a missing line is not a
failure). Each seat may get one short lens line ([lenses] in the config): `--lenses off` drops them for this run (`on`
forces them on), `--lens SEAT=TEXT` (repeatable) sets or overrides one seat's lens for this run.
Depth (row 14): `--depth quick` (default) is the council above. `--depth standard` adds a critique round between the
answers and the chair: each seat that answered reads the OTHER answering seats' answers as Seat A/B/... (never by seat id)
and writes a short critique with a revised `Verdict:`; the chair gets round 1 plus the critiques and says which changed the
recommendation. About 2N+1 model calls instead of N+1 (the preflight table shows the estimate). Never a vote or a gate: it
needs 2 answers, a failed critique never fails the run, and a seat that failed round 1 is skipped. Works with --grounded
(a tool-capable seat keeps its read-only access) and with lenses. Files: seats/<id>.critique.{md,raw,json}; summary.json
`depth` + `critique` and seats[].critique; status.json seats[].critique and phase `critique`.
--out is the PARENT of run dirs (default <state_dir>/runs); the run dir is <UTC ts>-<slug>.
`ask` (P27) is the one call a chat makes: the preflight once (free checks, then canaries), then the run, then a report
block (seat table, cost, calls, phase, critique, quorum, flag meanings, preflight notices, synthesis head, leaked-key scan).
A FAIL row stops it before anything else: the table, exit 1, no run dir, no question sent. `--rulings-file F` (ask, run,
preflight) attaches a scrubbed excerpt of owner rulings under an engine-written "Settled rulings (settled; raise only if
you find a defect)" heading, for every seat and the chair; it is refused (exit 2, line numbers and kinds only) if it holds
an email address, a URL, a home-dir or memory-folder path, a key-shaped string or a phone number. `ask` refuses a whole
DECISIONS.md / MEMORY.md / Claude memory-folder file as --context-file or --rulings-file, exit 2: by name always; a
renamed copy only in part, when its content still has that file's shape (a copy with a line put in front, a short one,
or a memory note without frontmatter can pass), so never pass a copy, write an excerpt; so does `run`, and `preflight`
shows a FAIL row for it (exit 1): every form refuses a whole rulings or memory file (preflight.PRIVATE_OUTCOME; no
override flag). Limits: these checks are a backstop; they never see names, employers or figures. The excerpt the caller
writes is the real guard.
Base config: $COUNCIL_CONFIG, else <Claude config dir>/council/config.toml (CLAUDE_CONFIG_DIR, else ~/.claude) when it exists,
else config.default.toml next to this file. `preflight` prints the one it used on its first line (`config: <path> (<source>)`).
"""
import sys

if sys.version_info < (3, 11):      # before the first tomllib import (3.11+): say so plainly, not a ModuleNotFoundError
    sys.stderr.write("council needs Python 3.11+ (found %d.%d.%d); install it and run: python3.11 %s ...\n"
                     % (tuple(sys.version_info[:3]) + (sys.argv[0] or "council.py",)))
    sys.exit(2)

import argparse
import json
import os
import re
import select
import signal
import time
import tomllib
import traceback

# C-bytecode: never write __pycache__ into the plugin dir (the entry shebangs run -E, which ignores PYTHONDONTWRITEBYTECODE)
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from councillib import config, council_run, drift, grounded, preflight, progress_mirror, report, status, verdict  # noqa: E402


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def _parser():
    ap = argparse.ArgumentParser(prog="council.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "run", "ask"):
        p = sub.add_parser(name, **({"help": "preflight, run and report in one call (P27)", "description": ASK_HELP,
                                       "formatter_class": argparse.RawDescriptionHelpFormatter} if name == "ask" else {}))
        p.add_argument("--config", action="append", default=[], metavar="P",
                       help="overlay TOML, repeatable; deep-merged over the base config (see config.default.toml; [[seat]] replaces the roster)")
        p.add_argument("--allow-missing", action="append", default=[], metavar="PROV",
                       help="run without PROV's seats if PROV fails preflight (repeatable)")
        p.add_argument("--context-file", action="append", default=[], metavar="F",
                       help="a file the seats get in their prompt (repeatable); preflight: for the question path lint")
        p.add_argument("--question-file", metavar="F",
                       help="the question; preflight: lints its paths only (row 2)")
        p.add_argument("--depth", choices=("quick", "standard"), default=None,
                       help="quick (default): answers, then the chair. standard: adds one critique round between them "
                            "(each answering seat reads the others' answers as Seat A/B/... and critiques them): about "
                            "2N+1 model calls instead of N+1")
        p.add_argument("--lenses", choices=("on", "off"), default=None,
                       help="seat lenses for this run: off drops them, on forces them (default: [lenses] enabled)")
        p.add_argument("--lens", action="append", default=[], metavar="SEAT=TEXT",
                       help="one seat's lens for this run (repeatable); overrides the config and the seat's own lens")
        p.add_argument("--grounded", nargs="?", const="", default=None, metavar="PATH",
                       help="grounded mode: the seats that can use tools (claude, codex) read PATH (default: the repo root "
                            "of the cwd) read-only and cite path:line; their quotes are checked. API-only seats stay "
                            "text-only, labelled ungrounded")
        p.add_argument("--attach", action="append", default=[], metavar="IMAGE",
                       help="an image file (png, jpg, gif, webp) the grounded seats can open (repeatable; needs --grounded)")
        p.add_argument("--allow-grounded-root", action="store_true",
                       help="with --grounded: go on although the root holds run folders or Claude memory folders a seat could "
                            "read whole (the refusal becomes a WARN row naming what was found)")
        p.add_argument("--allow-paths", action="store_true",
                       help="paths named in the question that are not --context-file are WARN, not FAIL")
        p.add_argument("--rulings-file", metavar="F",
                       help="a scrubbed excerpt of owner rulings that bear on the question (P23): every seat and the chair "
                            "get it under the heading 'Settled rulings (settled; raise only if you find a defect)'. Refused "
                            "(exit 2) if it holds an email address, a URL, a home-dir or memory-folder path, a key-shaped string or "
                            "a phone number. A backstop: names, employers and figures are the caller's to scrub")
        if name == "preflight":
            p.add_argument("--free-only", action="store_true", help="free checks only, no canaries")
        else:
            p.add_argument("--out", metavar="DIR", help="parent dir for run dirs (default <state_dir>/runs)")
            p.add_argument("--slug", metavar="S")
            p.add_argument("question", nargs="*", help='after "--": the question text')
    p = sub.add_parser("reap", help="sweep the watch calls whose runner is gone (D54): never kills a live one")
    p.add_argument("--config", action="append", default=[], metavar="P", help="overlay TOML, repeatable")
    p.add_argument("--orphans", action="store_true", help="required: the only sweep there is")
    p = sub.add_parser("status", help="live status of the current (or --run) council: seats, calls, bytes; free, no network")
    p.add_argument("--config", action="append", default=[], metavar="P", help="overlay TOML, repeatable (state_dir)")
    p.add_argument("--run", metavar="DIR", help="a run dir (path, or a name under <state_dir>/runs); default: current, else newest")
    p.add_argument("--watch", action="store_true", help="redraw every 2 s until the run ends")
    p.add_argument("--json", action="store_true", help="print status.json (plus age_s, runner_alive, stale)")
    p = sub.add_parser("drift", help="free drift check against the accepted baseline (alarms are sticky)")
    p.add_argument("--config", action="append", default=[], metavar="P", help="overlay TOML, repeatable")
    p.add_argument("--accept", nargs="*", default=None, metavar="KEY",
                   help="accept pending changes as the new baseline: all, or only the listed keys (`*` wildcard)")
    p.add_argument("--record", action="store_true", help="alias of a bare --accept")
    return ap


def _load(args):
    try:
        cfg = config.load(overlays=args.config)
        config.apply_cli_env(cfg)
        if getattr(args, "depth", None):
            cfg["_depth"] = args.depth                    # runtime flag (underscore: skipped by drift); quick when absent
        if getattr(args, "lenses", None):
            cfg["_lenses"] = args.lenses                  # runtime flags: underscore keys are skipped by drift
        if getattr(args, "lens", None):
            over, errs = verdict.parse_overrides(args.lens)
            if errs:
                return None, "; ".join(errs)
            ids = {x.get("id") for x in cfg.get("seat") or ()}
            unknown = sorted(set(over) - ids)
            if unknown:
                return None, "--lens: no such seat: %s (roster: %s)" % (", ".join(unknown), ", ".join(sorted(map(str, ids))))
            cfg["_lens_overrides"] = over
        if getattr(args, "grounded", None) is not None or getattr(args, "attach", None) \
                or getattr(args, "allow_grounded_root", False):
            if getattr(args, "grounded", None) is None:
                return None, "%s needs --grounded (only grounded seats can open files)" % (
                    "--attach" if getattr(args, "attach", None) else "--allow-grounded-root")
            if not grounded.cfg_of(cfg)["enabled"]:
                return None, "--grounded: disabled by [grounded] enabled = false"
            root, gerr = grounded.resolve_root(args.grounded, cfg)
            if gerr:
                return None, gerr
            images, gerr = grounded.check_images(args.attach, cfg)
            if gerr:
                return None, gerr
            cfg["_grounded"] = {"root": root, "images": images}      # runtime key: underscore, skipped by drift
            risk = preflight.root_risk(root)                          # names only, bounded; before anything is sent
            risk["allowed"] = bool(getattr(args, "allow_grounded_root", False))
            cfg["_grounded_risk"] = risk
        if getattr(args, "rulings_file", None):
            rulings, rerr = _rulings(args.rulings_file)
            if rerr:
                return None, rerr
            cfg["_rulings"] = rulings                    # runtime key: underscore, skipped by drift; the runner inherits it
        return cfg, None
    except tomllib.TOMLDecodeError as e:
        # tomllib's message is "<what> (at line N, column M)" and holds no value from the file; config.load_file tags
        # the error with the file it was reading
        return None, "config: invalid TOML in %s: %s" % (getattr(e, "council_path", None) or "the config",
                                                         preflight._scrub(e))
    except OSError as e:
        return None, "config: %s (%s)" % (type(e).__name__, getattr(e, "filename", "") or "")


def _rulings(path):
    """-> ({path, text}, None) or (None, error). The strict check (P23, truth 7): before anything is sent, a hit refuses
    with the line number and the kind of hit, never the text."""
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None, "--rulings-file not found: %s" % path
    if not text.strip():
        return None, "--rulings-file is empty: %s" % path
    hits = preflight.rulings_hits(text)
    if hits:
        return None, ("--rulings-file refused, nothing sent: %s. A scrubbed excerpt holds only the rulings that bear on "
                      "the question, each one plain sentence of the decision, with no names, addresses, links, paths, "
                      "keys, application or account details" % "; ".join("line %d: %s" % h for h in hits))
    return {"path": os.path.abspath(path), "text": text}, None


def _slug(args, question):
    s = args.slug or " ".join(question.split()[:6])
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()[:40]
    return s or "council"


def _out_parent(cfg, args):
    return os.path.expanduser(args.out) if args.out else os.path.join(
        cfg.get("state_dir") or os.path.expanduser("~/.claude-council"), "runs")


def _out_problem(cfg, args):
    """None when a run dir can be made under the parent (--out, else <state_dir>/runs); else one line saying why.
    Probes without creating anything, so `ask` can refuse before the paid canaries (a run dir exists only for a council
    that starts)."""
    parent = os.path.abspath(_out_parent(cfg, args))
    probe = parent
    while not os.path.lexists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)              # the nearest ancestor that exists: that is where makedirs starts
    if not os.path.isdir(probe):
        return "cannot create the run dir under %s: %s is not a directory" % (parent, probe)
    if not os.access(probe, os.W_OK | os.X_OK):
        return "cannot create the run dir under %s: %s is not writable" % (parent, probe)
    return None


def _run_dir(cfg, args, question):
    """The new run dir, or None after one stderr line when it cannot be made (the caller returns exit 2)."""
    parent = _out_parent(cfg, args)
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        base = os.path.join(parent, "%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), _slug(args, question)))
        path, n = base, 1
        while os.path.exists(path):
            n += 1
            path = "%s-%d" % (base, n)
        os.makedirs(path, mode=0o700)
    except OSError as e:
        sys.stderr.write("cannot create the run dir under %s: %s\n" % (parent, e.strerror or type(e).__name__))
        return None
    return path


def _policy_exit(e):
    sys.stderr.write("POLICY FAIL\n" + "".join("  - %s\n" % p for p in e.problems))
    return 2


def _private_refused(args, verb):
    """True (message written) when the verb refuses a whole rulings or memory file (preflight.PRIVATE_OUTCOME)."""
    files = list(args.context_file or ()) + ([args.rulings_file] if getattr(args, "rulings_file", None) else [])
    msg = preflight.private_refusal(files, verb)
    if msg:
        sys.stderr.write(msg + "\n")
    return bool(msg)


def _grounded_root_refused(cfg, verb):
    """True (message written) when the verb refuses a --grounded root that holds run or memory folders."""
    msg = preflight.grounded_root_refusal(cfg, verb)
    if msg:
        sys.stderr.write(msg + "\n")
    return bool(msg)


def _context_missing(paths):
    for p in paths:
        if not os.path.isfile(p):
            sys.stderr.write("--context-file not found: %s\n" % p)
            return True
    return False


def cmd_preflight(args):
    cfg, err = _load(args)
    if err:
        sys.stderr.write(err + "\n")
        return 2
    question = None
    if args.question_file:
        try:
            with open(args.question_file, "rb") as f:
                question = f.read().decode("utf-8", "replace")
        except OSError:
            sys.stderr.write("cannot read --question-file\n")
            return 2
    if _context_missing(args.context_file) or _private_refused(args, "preflight"):
        return 2
    try:
        ok, checks = preflight.run(cfg, args.allow_missing, free_only=args.free_only, question=question,
                                   context_files=args.context_file, allow_paths=args.allow_paths, verb="preflight")
    except preflight.PolicyError as e:
        return _policy_exit(e)
    sys.stdout.write("config: %s (%s)\n" % tuple(config.resolve_base()))
    sys.stdout.write(preflight.format_table(checks, args.allow_missing))
    return 0 if ok else 1


def cmd_run(args):
    cfg, err = _load(args)
    if err:
        sys.stderr.write(err + "\n")
        return 2
    if args.question_file:
        try:
            with open(args.question_file, "rb") as f:
                question = f.read().decode("utf-8", "replace")
        except OSError:
            sys.stderr.write("cannot read --question-file\n")
            return 2
    else:
        question = " ".join(args.question)
    if not question.strip():
        sys.stderr.write('no question: pass --question-file F or -- "<question>"\n')
        return 2
    if _context_missing(args.context_file) or _private_refused(args, "run") \
            or _grounded_root_refused(cfg, "run"):
        return 2
    # live status: the run dir exists before the preflight, so status.json / current.json show phase `preflight` (canaries
    # take a while); a policy failure removes it again, as if it had never been made
    out_dir = _run_dir(cfg, args, question)
    if out_dir is None:
        return 2
    sdir, live = status.state_dir_of(cfg), status.cfg_of(cfg)["enabled"]
    prog = status.cfg_of(cfg)["progress"]       # also publish boss.progress/1 (councillib/progress_mirror.py)
    run_id, title = os.path.basename(out_dir), status.title_of(question)
    if live:
        status.write_final(out_dir, status.early_snapshot(run_id, title, "preflight"), progress=prog)
        status.write_current(sdir, out_dir)
    try:
        ok, checks = preflight.run(cfg, args.allow_missing, free_only=False, question=question,
                                   context_files=args.context_file, allow_paths=args.allow_paths, verb="run")
    except BaseException as e:
        if live:
            status.clear_current(sdir, out_dir)
        if isinstance(e, preflight.PolicyError):
            for n in (status.FILE, status.FILE + ".%d.tmp" % os.getpid(), progress_mirror.EVENTS):
                try:
                    os.unlink(os.path.join(out_dir, n))
                except OSError:
                    pass
            for d in (out_dir, os.path.dirname(out_dir)):       # the run dir, then its parent if we made it (empty)
                try:
                    os.rmdir(d)
                except OSError:
                    pass
            if live and prog:                    # the run dir is gone, but the registry entry must not stay open
                progress_mirror.publish(out_dir, status.early_snapshot(
                    run_id, title, "failed", exit_code=2, ended=True, detail="refused by policy"))
            return _policy_exit(e)
        status.close_open(out_dir, code=2, reason="preflight raised %s" % type(e).__name__, progress=prog)
        raise
    table = preflight.format_table(checks, args.allow_missing)
    sys.stdout.write(table)
    return _after_preflight(cfg, args, question, out_dir, ok, checks, table)


def _after_preflight(cfg, args, question, out_dir, ok, checks, table):
    """`run` and `ask` from the preflight result on: the preflight files, then the launch (or the failed status). One
    path, so both commands leave the same run dir."""
    sdir, live, prog = status.state_dir_of(cfg), status.cfg_of(cfg)["enabled"], status.cfg_of(cfg)["progress"]
    run_id = os.path.basename(out_dir)
    _write_private(os.path.join(out_dir, "preflight.txt"), table)
    _write_private(os.path.join(out_dir, "preflight.json"), preflight.to_json(checks, args.allow_missing))
    if not ok:
        if live:
            status.write_final(out_dir, status.early_snapshot(run_id, status.title_of(question), "failed", exit_code=1,
                                                              ended=True, detail="preflight failed"), progress=prog)
            status.clear_current(sdir, out_dir)
        sys.stdout.write("run dir: %s\n" % out_dir)
        return 1
    failed = preflight.failed_providers(checks)
    stand = preflight.standins(checks)
    rc = launch(lambda fd: runner_main(cfg, question, args.context_file, out_dir, args.allow_missing,
                                       failed, fd, stand), out_dir)
    if live:                                     # the runner died before it took over status.json: do not leave it open
        status.close_open(out_dir, code=rc, reason="runner exited before a final status", only_if_dead=True,
                          progress=prog)
    return rc


ASK_HELP = """council.py ask: the preflight, the run and the report in one call (P27). Run it in the background: a council
takes minutes and every seat is a paid call that is never killed or capped.

Order: (1) the preflight ONCE (policy, question paths, free checks, drift, orphans, then the canaries; `run`'s own gate,
not run twice). Any FAIL row: the full table, exit 1, no run dir, the question is sent to no seat. (2) The run: a
short table (every FAIL/WARN/SKIP row, the depth and grounded rows, a count of the PASS rows left out; the full table is
<run dir>/preflight.txt), then `run`'s own lines (synthesis ready / below quorum / pending seats / pending: no answers
yet / still running, `run dir:`, `exit:`). (3) The report block, between `== council report` and `== end of report ==`:
the per-seat table, cost, calls, phase, critique, quorum, the meaning of each flag present, the preflight notices to
name, the first 15 lines of chair/synthesis.md, and one leaked-key scan line (tools/secret-scan.sh on the run dir: clean,
the file names, or not run and why; it never changes the exit code).

Exit codes: run's. 0 done (also below quorum), 1 a preflight FAIL, 2 usage or policy (also a refused --rulings-file or
a whole rulings/memory file), 3 quorum not met, 4 the chair failed or `pending: no answers yet`, 5 the runner crashed
(read <run dir>/runner.log), 128+N the launcher got signal N (the runner keeps running).

Refused before anything is sent (exit 2): a --context-file or --rulings-file that is %s, by name always, a renamed
copy only in part, when its content still has that file's shape (a copy with a line put in front, a short one, or a
memory note without frontmatter can pass): never pass a copy (fix: %s); a --rulings-file with an email address (also
(at) or spaced out), a URL (also without a scheme), a home-dir or memory-folder path, a key-shaped string or a phone
number (the error names line numbers and kinds, never the text). An ordinary --context-file may hold an email address (author lines): only the rulings file is strict. No
override flag. A backstop only: it never sees names, employers or figures; the excerpt you write is the real guard.

Flags used only sometimes:
  --depth standard   adds a critique round: each answering seat reads the others as Seat A/B/... and critiques them;
                     about 2N+1 calls instead of N+1 (the depth row shows it). Display only, never a vote or a gate.
  --lenses off / --lens SEAT=TEXT   drop the per-seat lens lines, or set one seat's lens, for this run.
  --grounded [DIR]   claude (Read/Grep/Glob) and codex (-s read-only) seats read DIR (default: the cwd's git root) and
                     cite path:line with a quote, checked against the files; other seats stay text only (ungrounded).
                     A path inside DIR needs no --context-file for grounded seats (question_path WARN).
                     A DIR that holds run folders or Claude memory folders is refused (exit 2; preflight: FAIL row).
  --allow-grounded-root   with --grounded: go on anyway; the refusal becomes a WARN row naming what was found.
  --attach IMAGE     png/jpg/gif/webp the grounded seats may open (needs --grounded).
  --allow-missing PROV   run without PROV's seats if PROV fails the preflight (only the user may ask for it).
  --allow-paths      a path in the question that is only illustrative: its question_path row is WARN, not FAIL.
""" % (" / ".join(preflight.PRIVATE_FILES), preflight.PRIVATE_FIX)


def cmd_ask(args):
    """P27: `run` with the preflight before the run dir, a short table and the report block."""
    cfg, err = _load(args)
    if err:
        sys.stderr.write(err + "\n")
        return 2
    if args.question_file:
        try:
            with open(args.question_file, "rb") as f:
                question = f.read().decode("utf-8", "replace")
        except OSError:
            sys.stderr.write("cannot read --question-file\n")
            return 2
    else:
        question = " ".join(args.question)
    if not question.strip():
        sys.stderr.write('no question: pass --question-file F or -- "<question>"\n')
        return 2
    if _context_missing(args.context_file):
        return 2
    if _private_refused(args, "ask") or _grounded_root_refused(cfg, "ask"):
        return 2
    problem = _out_problem(cfg, args)               # before the paid canaries, not after them
    if problem:
        sys.stderr.write(problem + "\n")
        return 2
    try:
        ok, checks = preflight.run(cfg, args.allow_missing, free_only=False, question=question,
                                   context_files=args.context_file, allow_paths=args.allow_paths, verb="ask")
    except preflight.PolicyError as e:
        return _policy_exit(e)
    sys.stdout.write("config: %s (%s)\n" % tuple(config.resolve_base()))
    table = preflight.format_table(checks, args.allow_missing)
    if not ok:
        sys.stdout.write(table)
        sys.stdout.write("no run dir: nothing was sent to the seats\nexit: 1\n")
        return 1
    out_dir = _run_dir(cfg, args, question)       # only now: a run dir exists only for a council that starts
    if out_dir is None:                            # raced with the probe above (rare): same one line, still exit 2
        return 2
    if status.cfg_of(cfg)["enabled"]:              # the same early status as `run` (the runner takes it over)
        status.write_final(out_dir, status.early_snapshot(os.path.basename(out_dir), status.title_of(question),
                                                          "preflight"), progress=status.cfg_of(cfg)["progress"])
        status.write_current(status.state_dir_of(cfg), out_dir)
    sys.stdout.write(preflight.format_brief(checks, args.allow_missing, os.path.join(out_dir, "preflight.txt")))
    rc = _after_preflight(cfg, args, question, out_dir, True, checks, table)
    _write_guarded(sys.stdout, report.block(out_dir, checks, args.allow_missing))
    return rc


RUNNER_LOG = "runner.log"
EXIT_RUNNER_CRASH = 5            # not 1: exit 1 means a failed preflight
FORWARD = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)   # caught by the launcher, never forwarded (D59)


def _detach_stdio(log):
    """stdin from /dev/null, stdout/stderr to `log` (an fd, or None for /dev/null): never the caller's
    terminal or pipes, so a closing terminal or a reader that went away cannot reach this process."""
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(null if log is None else log, 1)
    os.dup2(null if log is None else log, 2)
    os.close(null)


def _send(fd, msg):
    """One JSON line on the status pipe; the launcher may be gone (EPIPE is ignored: Python ignores SIGPIPE)."""
    try:
        data = (json.dumps(msg) + "\n").encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        pass


def _write_guarded(stream, text):
    """The launcher's output after the caller may have gone (closed chat or terminal): never raises."""
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):
        try:                                     # a later flush at exit must not raise either
            stream.close()
        except Exception:
            pass


def _spawn_runner(target, r, w, log):
    """The first child (D59): a new session, then the keeper; exits at once, so the keeper and the runner are
    re-parented away from the launcher and the caller's process tree. A tool host that SIGKILLs a closing
    command's descendants (Claude Code walks the ppid tree and kills each pid and its group) or a group kill
    of the launcher's group no longer reaches them. The keeper forks the runner into its own session, waits
    for it and reports its exit code on the status pipe, so the launcher still returns the runner's real code
    (128+N for a signal death) when the runner ends before ready."""
    try:
        os.close(r)
        os.setsid()
        if os.fork() != 0:
            os._exit(0)                          # the first child: gone, the keeper is orphaned
        # the keeper: a closing terminal or a stray INT has nothing to stop here
        for sig in (signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        _detach_stdio(None)
        pid = os.fork()
        if pid == 0:
            _runner_child(target, w, log)        # never returns
        os.close(log)
        _send(w, {"event": "started", "runner_pid": pid})
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except InterruptedError:
                continue
            except ChildProcessError:
                status = None
                break
        if status is None:
            _send(w, {"event": "exit", "code": None, "runner_pid": pid})
        else:
            rc = os.waitstatus_to_exitcode(status)
            _send(w, {"event": "exit", "code": 128 - rc if rc < 0 else rc, "runner_pid": pid})
    finally:
        os._exit(0)


def _runner_child(target, w, log):
    """The runner: its own session (no controlling terminal, its own group), stdio in runner.log, the
    status pipe for the ready report."""
    rc = EXIT_RUNNER_CRASH
    try:
        os.setsid()
        _detach_stdio(log)
        os.close(log)
        sys.stdout = open(1, "w", closefd=False)
        sys.stderr = open(2, "w", closefd=False)
        for sig in FORWARD:
            signal.signal(sig, signal.SIG_DFL)     # run() installs its own handlers
        rc = target(w)
    except BaseException:
        try:
            traceback.print_exc()
        except Exception:
            pass
    finally:
        for f in (sys.stdout, sys.stderr):
            try:
                f.flush()
            except Exception:
                pass
        os._exit(rc if isinstance(rc, int) and rc != 1 else EXIT_RUNNER_CRASH)


def launch(target, out_dir, stream=None):
    """The launcher (row 31, D18; D59): start the runner, `target(status_fd) -> exit code`, detached from the
    caller: a first child makes a new session and exits, its child (the keeper) forks the runner into a session
    of its own with stdin from /dev/null and stdout/stderr in <out_dir>/runner.log (never this process's
    stdout: a background caller waits for EOF on it). Forks, not an exec: the runner keeps the exact config,
    the preflight result and the adapter registry this process loaded. Wait for the runner's ready report on
    the status pipe, print the report lines and return the run's exit code; if it exits before ready, return
    its exit code (from the keeper).
    D59 (supersedes D18's "the launcher's death is a TERM"): nothing that happens to the launcher reaches the
    runner. TERM/INT/HUP here (a closed chat or terminal, Ctrl-C, a group kill) are NOT forwarded: the launcher
    prints that the runner keeps running and how to stop it, and returns 128+N. Only `kill -TERM <runner_pid>`
    stops the runner and its calls (D53)."""
    stream = stream or sys.stdout
    for f in (stream, sys.stdout, sys.stderr):
        try:
            f.flush()
        except Exception:
            pass
    r, w = os.pipe()
    log = os.open(os.path.join(out_dir, RUNNER_LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        first = os.fork()
    except OSError as e:
        for fd in (r, w, log):
            os.close(fd)
        _write_guarded(stream, "cannot start the runner: %s\nrun dir: %s\nexit: %d\n"
                       % (type(e).__name__, out_dir, EXIT_RUNNER_CRASH))
        return EXIT_RUNNER_CRASH
    if first == 0:
        _spawn_runner(target, r, w, log)         # never returns into the caller's code
    os.close(w)
    os.close(log)
    while True:
        try:
            os.waitpid(first, 0)                 # it exits right after the keeper's fork
            break
        except InterruptedError:
            continue
        except ChildProcessError:
            break

    wake_r, wake_w = os.pipe()                   # the handler only records and wakes the loop: it never raises
    os.set_blocking(wake_w, False)
    caught = []

    def on_sig(signum, _frame):
        caught.append(signum)
        try:
            os.write(wake_w, b"x")
        except OSError:
            pass
    prev = {}
    for sig in FORWARD:
        try:
            prev[sig] = signal.signal(sig, on_sig)
        except (ValueError, OSError):
            pass
    ready, ended, runner_pid, buf = None, None, None, b""
    try:
        while ready is None and ended is None and not caught:
            readable, _, _ = select.select([r, wake_r], [], [])
            if caught:
                break
            if r not in readable:
                continue
            chunk = os.read(r, 4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf and ready is None and ended is None:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                runner_pid = msg.get("runner_pid") or runner_pid
                if msg.get("event") == "ready":
                    ready = msg
                elif msg.get("event") == "exit":
                    ended = msg
    finally:
        for sig in FORWARD:                      # ignore a second one while the report is written
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
        for fd in (r, wake_r, wake_w):
            os.close(fd)
    got = caught[0] if caught and ready is None and ended is None else None
    if got is not None:
        rc = 128 + got
        who = "runner %s" % runner_pid if runner_pid else "the runner (summary.json runner_pid)"
        text = ("launcher stopped by signal %d before the synthesis; %s keeps running with its calls (D59): "
                "results land in the run dir; stop it only with kill -TERM %s\n"
                % (got, who, runner_pid or "<runner_pid>"))
    elif ready is not None:
        rc = int(ready.get("code", 1))
        text = report_lines(ready)
    else:
        rc = ended.get("code") if ended else None
        if not isinstance(rc, int) or rc == 1:
            rc = EXIT_RUNNER_CRASH                # never exit 1: that reads as a failed preflight
        text = ""
        if rc not in (0, 2, 3, 4):
            text = ("runner exited with %d before the synthesis (log: %s)\n"
                    % (rc, os.path.join(out_dir, RUNNER_LOG)))
    for sig, h in prev.items():
        try:
            signal.signal(sig, h if h is not None else signal.SIG_DFL)
        except (ValueError, OSError):
            pass
    _write_guarded(stream, "%srun dir: %s\nexit: %d\n" % (text, out_dir, rc))
    return rc


def report_lines(ready):
    """The launcher's report of a ready runner (D18, D39, D49, D50)."""
    out = []
    pid = ready.get("runner_pid")
    if ready.get("synthesis") and ready.get("code") == 0:
        out.append("synthesis ready: %s\n" % ready["synthesis"])
    if ready.get("reason"):
        out.append("%s\n" % ready["reason"])
    if ready.get("below_quorum"):
        out.append("below quorum: %s of %s required answers; the chair synthesised without the pending seats\n"
                   % (ready.get("ok"), ready.get("need")))
    pend = ready.get("pending") or []
    if pend:
        out.append("pending seats: %s (runner %s keeps supervising)\n" % (",".join(pend), pid))
    other = ["%s/%s" % (c.get("seat"), c.get("call")) for c in ready.get("late_pending") or []
             if c.get("seat") not in pend]
    if other:
        out.append("still running: %s (runner %s keeps supervising; a late answer is appended and flagged)\n"
                   % (",".join(other), pid))
    return "".join(out)


def runner_main(cfg, question, context_files, out_dir, allow_missing, failed, status_fd=None, standins=None):
    """The runner: council_run.run with the ready report on status_fd (closed right after, so nothing is ever
    written to the launcher once it has gone). The launcher's death is not a stop (D59, superseding D18)."""

    def on_ready(code, info):
        if status_fd is None:
            return
        msg = {"event": "ready", "code": code}
        msg.update({k: v for k, v in info.items() if k != "work_root"})
        try:
            data = (json.dumps(msg) + "\n").encode("utf-8")
            while data:
                data = data[os.write(status_fd, data):]
        except OSError:
            pass
        try:
            os.close(status_fd)
        except OSError:
            pass
    return council_run.run(cfg, question, context_files, out_dir, allow_missing, set(failed or ()),
                           on_ready=on_ready, standins=standins)


def _sweep(cfg, stream):
    """The orphan sweep (D54) for a command that has no preflight table: its lines go to `stream`."""
    try:
        res = preflight.sweep_orphans(cfg)
    except Exception as e:                       # never fail the command for the sweep
        stream.write("orphan sweep raised %s\n" % type(e).__name__)
        return None
    for line in preflight.orphan_lines(res):
        stream.write("orphans: %s\n" % line)
    return res


def cmd_reap(args):
    if not args.orphans:
        sys.stderr.write("reap: pass --orphans (the only sweep there is)\n")
        return 2
    cfg, err = _load(args)
    if err:
        sys.stderr.write(err + "\n")
        return 2
    res = _sweep(cfg, sys.stdout)
    if res is not None and not (res["live"] or res["swept"] or res["errors"]):
        sys.stdout.write("orphans: none (%d watch calls with a live runner)\n" % res["runner_alive"])
    return 0


def cmd_drift(args):
    cfg, err = _load(args)
    if err:
        sys.stderr.write(err + "\n")
        return 2
    _sweep(cfg, sys.stderr)
    old = drift.load_last(cfg)
    snap = drift.collect(cfg)
    text, rc = drift.report(old, snap)
    sys.stdout.write(text)
    accepting = args.accept is not None or args.record
    if not accepting:
        if not old:
            sys.stdout.write("not recorded: a passing preflight seeds the baseline, or `council.py drift --accept` does\n")
        elif rc:
            sys.stdout.write("alarm stays raised on every preflight until `council.py drift --accept [KEY...]`\n")
        return rc
    keys = list(args.accept or [])
    trigger = "drift --accept" if args.accept is not None else "drift --record"
    try:
        path, picked = drift.accept(cfg, snap, trigger, old, keys or None)
    except ValueError as e:
        sys.stderr.write("drift --accept: %s (nothing recorded)\n" % e)
        return 2
    if picked:
        sys.stdout.write("accepted %d: %s\n" % (len(picked), ", ".join(c["key"] for c in picked)))
    sys.stdout.write("recorded %s\n" % path)
    left = drift.diff(drift.load_last(cfg), snap)
    if left:
        sys.stdout.write("still raised (%d):\n%s" % (len(left), "".join("  %s\n" % drift.change_line(c) for c in left)))
    return 1 if left else 0


WATCH_S = 2.0


def cmd_status(args):
    """Free and read-only: files only, no network, no orphan sweep (the sweep appends late answers)."""
    cfg, err = _load(args)
    if err:
        sys.stderr.write(err + "\n")
        return 2
    sdir = status.state_dir_of(cfg)

    def once():
        run = status.resolve_run(sdir, args.run)
        snap = status.load(run) if run else None
        return run, snap

    def show(run, snap, tty):
        v = status.view(snap)
        text = json.dumps(v, indent=1) + "\n" if args.json else status.render(snap)
        sys.stdout.write(("\033[H\033[2J" if tty and args.watch and not args.json else "") + text)
        sys.stdout.flush()
        return v

    run, snap = once()
    if snap is None:
        sys.stderr.write("status: no council run%s\n" % (" at %s" % args.run if args.run else
                                                           " found (no current run, no run dir with a status.json)"))
        return 1
    tty = sys.stdout.isatty()
    while True:
        v = show(run, snap, tty)
        if not args.watch or v.get("ended") or v["stale"]:
            return 0
        try:
            time.sleep(WATCH_S)
        except KeyboardInterrupt:
            return 0
        run2, snap2 = once() if not args.run else (run, status.load(run))
        if snap2 is not None:
            run, snap = run2, snap2


def main(argv=None):
    args = _parser().parse_args(argv)
    return {"preflight": cmd_preflight, "run": cmd_run, "ask": cmd_ask, "drift": cmd_drift,
            "reap": cmd_reap, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
