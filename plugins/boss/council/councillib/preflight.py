"""Preflight: policy, engine, free checks, then canaries. Fails before any council spend.

`run(cfg, allow_missing=(), free_only=False) -> (ok, checks)`

Stages, in order (the first stage with a FAIL ends the run):
  policy   roster rules (validate_roster) and --allow-missing names; raises PolicyError (CLI exit 2)
           before any adapter is touched.
  engine   Python >= 3.11, reap.py present, state_dir writable.
  free     every adapter's free_checks (threads, short subprocess/HTTP calls, no spend), chair
           included. Preflight also requires an `inner_caps*` row and an `output_budget` row from
           every real adapter, and adds WARN rows for the known buffered-provider limits.
  drift    (free, alongside the free stage) `drift.collect`: CLI versions, model pins and labels,
           inner-timeout values; one WARN row per item changed since the last passing preflight
           (never a FAIL). On PASS the new snapshot is recorded (`<state_dir>/drift/last.json`).
  canary   one per distinct (provider, model) across seats and chair, through the same adapter,
           Supervisor, reap.py and classification (`council_run._classify`) as a seat.

A FAIL of a provider named in `allow_missing` does not fail the run (the table shows
`FAIL (allowed missing)`) and that provider's canary is skipped. A passing provider named
there still sits: `council_run.run` only drops seats of providers that failed.

Long watch calls (row 46, D58): a watch call under a LIVE runner that has run more than 2x its soft deadline is
shown as a WARN row (seat, call, run id, age, runner pid). Nothing is killed or changed.
Orphans (row 40, D54): every preflight (and `council.py reap --orphans`) first sweeps the watch calls whose
runner is gone (see `sweep_orphans`). A finished one has its answer appended as late and is reaped; a live one is
left running and shown as a WARN row with its age, seat, call and run id. The sweep never signals a process.

Canaries carry the provider's budgets (`cfg["_canary"]` selects the low canary effort; token
caps stay), and a killed canary keeps its partial output in the row detail. The only wall
clock is `[canary] timeout_s` (default 120 s, canaries take 2-6 s), a hung-provider detector.

Canary cache (C6): a plain PASS is remembered in `<state_dir>/canary-pass.json` for `[canary] cache_s` seconds
(default 600, 0 = off), keyed by provider, model, prompt, the built launch argv and the CLI binary. A hit is a PASS row
("cached: ...") and no call is made; a FAIL, a WARN (retried, rerouted, stand-in), an expired pass or any change in the
key calls the canary as before.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor

from . import config as _config
from . import answer_check, critique, council_run, drift, grounded, netwatch, providers, timing
from .providers import Check
from .supervise import Limits, Supervisor, install_signal_handlers

HERE = os.path.dirname(os.path.abspath(__file__))
REAP_PY = os.path.join(HERE, "reap.py")
POLL_S = 0.2
DETAIL_MAX = 200
PARTIAL_MAX = 100
REQUIRED_ROWS = ("inner_caps", "output_budget")      # every real adapter must report both
NO_OUTPUT_CAP = {"codex": "codex 0.163.0 has no output-token cap (only reasoning effort)"}
_KEYISH = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")


class PolicyError(Exception):
    """Roster policy or --allow-missing misuse. The CLI exits 2."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _scrub(text, n=DETAIL_MAX):
    return _KEYISH.sub("sk-...", str(text if text is not None else "")).replace("\n", " ")[:n]


def _chair_seat(cfg):
    seat = dict(cfg.get("chair") or {})
    seat["id"] = "chair"
    seat["role"] = "chair"
    return seat


def _roster(cfg):
    return [dict(s) for s in (cfg.get("seat") or [])]


def _provider_order(cfg):
    names = []
    for s in _roster(cfg) + [_chair_seat(cfg)]:
        p = s.get("provider")
        if p and p not in names:
            names.append(p)
    return names


def _is_stub(adapter):
    return isinstance(adapter, providers.StubAdapter)


# ---------------------------------------------------------------- stages
def policy_stage(cfg, allow_missing):
    problems = list(_config.validate_roster(cfg))
    roster_providers = {s.get("provider") for s in _roster(cfg)}
    chair_provider = (cfg.get("chair") or {}).get("provider")
    for name in allow_missing or ():
        if name == chair_provider:
            problems.append("--allow-missing %s: it is the chair's provider, and the chair is never skipped" % name)
        elif name not in roster_providers:
            problems.append("--allow-missing %s: not a provider in the roster (%s)"
                            % (name, ", ".join(sorted(p for p in roster_providers if p)) or "empty"))
    if problems:
        raise PolicyError(problems)
    anth = [s.get("id") for s in _roster(cfg) if providers.is_anthropic(s)]
    cap = (cfg.get("policy") or {}).get("max_anthropic_advisors", 1)
    return [Check("policy", "policy", "roster", "PASS",
                  "%d seats, chair %s, anthropic advisors %d <= %s (chair not counted)"
                  % (len(_roster(cfg)), chair_provider, len(anth), cap))]


def engine_stage(cfg):
    out = []
    v = sys.version_info
    out.append(Check("engine", "engine", "python", "PASS" if v >= (3, 11) else "FAIL",
                     "python %d.%d.%d%s" % (v[0], v[1], v[2], "" if v >= (3, 11) else " < 3.11")))
    out.append(Check("engine", "engine", "reap", "PASS" if os.path.isfile(REAP_PY) else "FAIL",
                     "councillib/reap.py present" if os.path.isfile(REAP_PY) else "councillib/reap.py missing"))
    sprobs = council_run.salvage_problems(cfg) + providers.salvage_provider_problems(cfg)
    sv = council_run.salvage_cfg(cfg)
    out.append(Check("engine", "engine", "salvage", "FAIL" if sprobs else "PASS",
                     "; ".join(sprobs) or ("answer salvage on: one follow-up call per answerless seat, cap %d tokens, "
                                           "timeout %gs" % (sv["max_output_tokens"], sv["timeout"]) if sv["enabled"]
                                           else "answer salvage off ([salvage] enabled = false or absent)")))
    aprobs = answer_check.problems(cfg)
    ac = answer_check.cfg_of(cfg)
    out.append(Check("engine", "engine", "answer_check", "FAIL" if aprobs else "PASS",
                     "; ".join(aprobs) or ("answer check on: tool-call markup with < %d chars of prose, or < %d chars, "
                                           "is invalid_answer (salvaged)" % (ac["min_prose_chars"], ac["min_chars"])
                                           if ac["enabled"] else "answer check off ([answer_check] enabled = false "
                                                                 "or absent)")))
    out += table_rows(cfg)
    state_dir = cfg.get("state_dir") or os.path.expanduser("~/.claude-council")
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".preflight-", dir=state_dir)
        os.close(fd)
        os.unlink(tmp)
        out.append(Check("engine", "engine", "state_dir", "PASS", "writable"))
    except OSError as e:
        out.append(Check("engine", "engine", "state_dir", "FAIL", "not writable (%s)" % type(e).__name__))
    return out


def table_rows(cfg):
    """One engine row per run table (row 29): `[hedge]`, `[fallback]`, `[liveness]`, the gate keys on
    `[providers.<p>]` and the OpenRouter routing prefs. A malformed table FAILs before any spend."""
    hedge = cfg.get("hedge") or {}
    live = cfg.get("liveness") or {}
    rows = []
    for name, probs, ok in (
            ("hedge", providers.hedge_problems(cfg),
             "hedge %s (providers %s)" % ("on" if hedge.get("enabled") is True else "off",
                                          ",".join(hedge.get("providers") or []) or "none")),
            ("fallback", providers.fallback_problems(cfg),
             "fallback %s" % ("on" if (cfg.get("fallback") or {}).get("enabled") is True else "off")),
            ("liveness", providers.liveness_problems(cfg),
             "probe-signal flags %s" % ("on" if live.get("signals") is True else "off")),
            ("signals", providers.signal_problems(cfg), "gate keys valid on [providers.*]"),
            ("routing", providers.routing_problems(cfg), "OpenRouter routing prefs valid")):
        rows.append(Check("engine", "engine", name, "FAIL" if probs else "PASS", "; ".join(probs) or ok))
    return rows


def quota_rows(cfg):
    """D47: a provider whose recorded quota reset time is still ahead is `UNAVAILABLE` for every seat it has. The
    state is read from `<state_dir>/quota.json`: no probe call. It never FAILs the preflight (the council runs
    without that seat; D42 covers a short council), and `unavailable_providers` skips its canary."""
    gone = providers.quota_unavailable(cfg)
    rows = []
    for s in _roster(cfg) + [_chair_seat(cfg)]:
        q = gone.get(s.get("provider"))
        if q:
            rows.append(Check(s["provider"], "free", "quota:%s" % s.get("id", "?"), "UNAVAILABLE",
                              "%s until %s (recorded, not probed)" % (q["reason"], q["until"])))
    return rows


def unavailable_providers(checks):
    """Providers with an UNAVAILABLE quota row. Row 30 drops their seats from the roster."""
    return {c.provider for c in checks if c.result == "UNAVAILABLE"}


def _free_for(name, seats, cfg):
    try:
        adapter = providers.get(name)
    except (NotImplementedError, KeyError) as e:
        return [Check(name, "free", "adapter", "FAIL", "no adapter (%s)" % type(e).__name__)]
    try:
        rows = list(adapter.free_checks(seats, cfg))
    except Exception as e:          # an adapter bug must be a FAIL row, not a crash
        return [Check(name, "free", "free_checks", "FAIL", "raised %s" % type(e).__name__)]
    for c in rows:
        c.detail = _scrub(c.detail)
    names = {c.check for c in rows}
    if not _is_stub(adapter):
        for req in REQUIRED_ROWS:
            if not any(n == req or n.startswith(req + ":") or n.startswith(req + "_") for n in names):
                rows.append(Check(name, "free", req, "FAIL",
                                  "adapter returned no %s row (the inner timeouts and output budgets must be checked)" % req))
        mode = ((cfg.get("providers") or {}).get(name) or {}).get("mode", "buffered")
        if mode == "buffered":
            rows.append(Check(name, "free", "buffered_partial", "WARN",
                              "accepted limit: buffered CLI, a kill in the silent phase leaves no partial output "
                              "(tier/effort is the lever; a hedged watch seat has no deadline, only a soft hedge time)"))
        if name in NO_OUTPUT_CAP:
            rows.append(Check(name, "free", "no_output_cap", "WARN", "accepted limit: " + NO_OUTPUT_CAP[name]))
    return rows


def retry_failed(cfg):
    """[preflight] retry_failed (default true; [canary] retry_failed is read too): a provider whose free checks or
    canary FAIL is checked once more before the row stands, so a transient blip does not need the owner."""
    pf, can = (cfg or {}).get("preflight") or {}, (cfg or {}).get("canary") or {}
    return bool(pf.get("retry_failed", can.get("retry_failed", True)))


FREE_RETRY_DELAY_S = 2.0


def _free_with_retry(name, seats, cfg):
    rows = _free_for(name, seats, cfg)
    first = [c for c in rows if c.result == "FAIL"]
    if not first or not retry_failed(cfg) or any(c.check == "adapter" or providers.NOT_FOUND in c.detail for c in first):
        return rows         # no adapter, or a CLI that is not installed: a retry cannot change that, so no 2 s sleep
    time.sleep(FREE_RETRY_DELAY_S)
    again = _free_for(name, seats, cfg)
    note = "; ".join("%s: %s" % (c.check, c.detail) for c in first)
    if not any(c.result == "FAIL" for c in again):
        return again + [Check(name, "free", "retry", "WARN", _scrub("passed on the retry after a failure (%s)" % note))]
    for c in again:
        if c.result == "FAIL":
            c.detail = _scrub("failed twice; " + c.detail)
    return again


def free_stage(cfg):
    names = _provider_order(cfg)
    roster, chair = _roster(cfg), _chair_seat(cfg)
    seats_of = {n: [s for s in roster if s.get("provider") == n]
                + ([chair] if chair.get("provider") == n else []) for n in names}
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as ex:
        futs = [ex.submit(_free_with_retry, n, seats_of[n], cfg) for n in names]
        rows = []
        for f in futs:
            rows.extend(f.result())
    return rows + quota_rows(cfg)     # the pool has joined every thread before this returns


def standins(checks):
    """{seat id: reason} for the seats a WARN row says run as their stand-in (model missing, or its canary failed
    after the retry and the reroute). council_run starts such a seat on the stand-in as its first call."""
    out = {}
    for c in checks:
        if c.result == "WARN" and getattr(c, "standin", None):
            for sid in c.standin.get("seats") or ():
                out.setdefault(sid, c.standin.get("reason"))
    return out


def _standin_ids(cfg, provider, model):
    """The roster seats that run the (provider, model) pair as themselves; [] when the chair uses it too (the chair has
    no stand-in, so a pair it shares must pass as is)."""
    ch = _chair_seat(cfg)
    if (ch.get("provider"), ch.get("model")) == (provider, model):
        return []
    return [s.get("id") for s in _roster(cfg) if (s.get("provider"), s.get("model")) == (provider, model)]


def _canary_pairs(cfg, skip_providers, stand=None):
    """[(provider, model, seat copy)] one per distinct pair, roster order then chair. A seat in `stand` (id -> reason)
    is canaried as its stand-in: that is the model it will call."""
    seen, pairs = set(), []
    for s in _roster(cfg) + [_chair_seat(cfg)]:
        if stand and s.get("id") in stand and s.get("provider") not in skip_providers:
            try:
                st = providers.get(s.get("provider")).standin_start(s, cfg)
            except Exception:
                st = None
            if st is not None:
                s = st
        p, m = s.get("provider"), s.get("model")
        if p in skip_providers or (p, m) in seen:
            continue
        seen.add((p, m))
        seat = {k: v for k, v in s.items() if k != "role"}
        seat["id"] = "canary-%s" % re.sub(r"[^A-Za-z0-9_.-]", "_", str(p))
        pairs.append((p, m, seat))
    return pairs


def _canary_limits(cfg, provider):
    t = cfg.get("timeouts") or {}
    d = dict(t.get("default") or {})
    d.update({k: v for k, v in (t.get(provider) or {}).items() if k != "adaptive"})
    can = cfg.get("canary") or {}
    timeout_s = float(can.get("timeout_s", 120))
    idle_s = float(can.get("idle_s", 60))
    mode = ((cfg.get("providers") or {}).get(provider) or {}).get("mode", "buffered")
    common = dict(idle_s=idle_s, ceiling_s=timeout_s, grace_s=float(d.get("grace_s", 5)),
                  max_extensions=int(d.get("max_extensions", 1)),
                  extension_factor=float(d.get("extension_factor", 1.5)))
    if mode == "stream":
        return Limits(mode="stream", deadline_s=None, **common), "stream"
    return Limits(mode="buffered", deadline_s=timeout_s, **common), "buffered"


CANARY_CACHE = "canary-pass.json"          # in state_dir: {fingerprint: {"at": epoch s, "provider", "model"}}
CANARY_CACHE_S = 600.0                     # default window; [canary] cache_s, 0 turns the cache off


def _canary_cache_s(cfg):
    try:
        return max(0.0, float(((cfg or {}).get("canary") or {}).get("cache_s", CANARY_CACHE_S)))
    except (TypeError, ValueError):
        return CANARY_CACHE_S


def _canary_fingerprint(provider, model, launch, wd, ppath, prompt, mode):
    """What a passing canary vouches for: the same provider, model, prompt, launch argv (efforts, council provider, the
    user-MCP disables, extra args, every config-derived flag; the per-call temp paths blanked) and CLI binary. Any
    change in one of them is a different key, so a cached pass never covers a changed setup."""
    argv = [str(a).replace(wd, "<wd>").replace(ppath, "<prompt>") for a in launch.argv]
    binp = shutil.which(argv[0]) if argv else None
    try:
        st = os.stat(os.path.realpath(binp)) if binp else None
    except OSError:
        st = None
    ident = [os.path.realpath(binp), st.st_mtime_ns, st.st_size] if st else None
    blob = json.dumps([provider, model, mode, prompt, argv, ident], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _canary_cached(state_dir, window_s, key, now=None):
    """-> seconds since the pass when `key` passed within the window, else None."""
    if window_s <= 0:
        return None
    ent = (_read_json(os.path.join(state_dir, CANARY_CACHE)) or {}).get(key)
    now = time.time() if now is None else now
    try:
        age = now - float(ent["at"])
    except (TypeError, KeyError, ValueError):
        return None
    return age if -60 <= age <= window_s else None


def _canary_remember(state_dir, window_s, passed, now=None):
    """Record the plain-PASS fingerprints {key: (provider, model)}; drops entries older than the window. A failed write
    never fails the preflight (the next run just calls the canary again)."""
    if window_s <= 0 or not passed:
        return
    now = time.time() if now is None else now
    path = os.path.join(state_dir, CANARY_CACHE)
    cur = _read_json(path) or {}
    keep = {k: v for k, v in cur.items() if isinstance(v, dict) and isinstance(v.get("at"), (int, float))
            and -60 <= now - v["at"] <= window_s}
    for k, (p, m) in passed.items():
        keep[k] = {"at": now, "provider": p, "model": m}
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        _write_json_atomic(path, keep)
    except OSError:
        pass


def canary_stage(cfg, skip_providers=(), stand=None):
    unavail = set(providers.quota_unavailable(cfg))
    pairs = _canary_pairs(cfg, set(skip_providers), stand)
    ccfg = dict(cfg)
    ccfg["_canary"] = True
    can = cfg.get("canary") or {}
    prompt = str(can.get("prompt", "Reply with exactly: OK"))
    # retry_failed (default true): every failed canary gets one more call before its row says FAIL, not only a
    # fast-fail-fallback provider's. A one-off CLI blip (agy, codex, ...) must not stop the run for an owner check.
    retry_all = retry_failed(cfg)
    state_dir = cfg.get("state_dir") or os.path.expanduser("~/.claude-council")
    log_path = os.path.join(state_dir, "latency.jsonl")
    run_id = "canary-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    cache_s = _canary_cache_s(cfg)
    keys = {}                               # task key -> (fingerprint, provider, model)
    rows = []
    for p in sorted(set(skip_providers) & {s.get("provider") for s in _roster(cfg) + [_chair_seat(cfg)]}):
        rows.append(Check(p, "canary", "canary", "SKIP", "quota recorded as out (unavailable)"
                          if p in unavail else "failed the free stage (allowed missing)"))
    if not pairs:
        return rows

    work_root = tempfile.mkdtemp(prefix="council-canary-")
    sup = Supervisor()
    tasks, order = {}, []
    prev = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                prev[sig] = signal.getsignal(sig)
            install_signal_handlers(sup, None)
        pdata = prompt.encode("utf-8")
        for i, (provider, model, seat) in enumerate(pairs):
            label = "canary:%s" % model
            try:
                adapter = providers.get(provider)
            except (NotImplementedError, KeyError):
                rows.append(Check(provider, "canary", label, "FAIL", "no adapter"))
                continue
            wd = os.path.join(work_root, "c%d" % i)
            os.makedirs(wd, mode=0o700)
            ppath = wd + ".prompt.md"
            fd = os.open(ppath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pdata)
            task = council_run._Task("c%d" % i, "seat", seat, adapter)
            task.prompt_bytes = len(pdata)
            task.label = label
            task.ppath = ppath
            try:
                limits, task.mode = _canary_limits(cfg, provider)
                task.deadline_s = limits.deadline_s
                task.deadline_source = "canary_timeout"
                task.launch = adapter.build(seat, ppath, wd, limits, ccfg)
                task.extractor = council_run._extractor(adapter, seat)
            except Exception as e:
                rows.append(Check(provider, "canary", label, "FAIL", "build failed (%s)" % type(e).__name__))
                continue
            if cache_s > 0:
                fp = _canary_fingerprint(provider, model, task.launch, wd, ppath, prompt, task.mode)
                age = _canary_cached(state_dir, cache_s, fp)
                if age is not None:           # a pass inside the window for this exact setup: no call
                    rows.append(Check(provider, "canary", label, "PASS",
                                      "cached: the same canary passed %s ago (window %ds, [canary] cache_s); no call made"
                                      % (_age(max(0.0, age)), int(cache_s))))
                    continue
                keys[task.key] = (fp, provider, model)
            task.started = time.monotonic()
            tasks[task.key] = task
            order.append(task)
            sup.start(task.key, task.launch, limits, on_output=task.on_output)

        waiting = set(tasks)
        recover = []
        hard_stop = time.monotonic() + float(can.get("timeout_s", 120)) * 3 + 60   # safety valve only
        done_rows = {}
        while waiting:
            for key, res in sup.poll(POLL_S):
                task = tasks[key]
                task.result, task.done = res, True
                ok = council_run._classify(task)
                done_rows[key] = _canary_row(task, ok)
                _log(log_path, run_id, task)
                if not ok and (getattr(task.adapter, "fast_fail_fallback", False) or retry_all):
                    recover.append(task)           # retried below, one at a time, before the row says FAIL
                waiting.discard(key)
            if waiting and time.monotonic() > hard_stop:
                sup.kill_all()
                for key in sorted(waiting):
                    t = tasks[key]
                    done_rows[key] = Check(t.seat["provider"], "canary", t.label, "FAIL", "preflight safety stop")
                break
        for task in recover:
            # fallback-chain row 1: one blip must not FAIL a preflight and drop a seat. The same request again, then the
            # rerouted one (provider pin dropped, capped max_tokens); then, for a roster seat with a `fallback_model`, the stand-in.
            sids = ([] if task.seat.get("_orig_model") else
                    _standin_ids(cfg, task.seat.get("provider"), task.seat.get("model")))
            done_rows[task.key] = _recover_canary(sup, task, cfg, ccfg, work_root, done_rows[task.key], log_path, run_id,
                                                  sids)
        rows.extend(done_rows[t.key] for t in order if t.key in done_rows)
        _canary_remember(state_dir, cache_s, {keys[k][0]: keys[k][1:] for k, r in done_rows.items()
                                              if k in keys and r.result == "PASS"})
    finally:
        if sup.running():
            sup.kill_all()
        for sig, h in prev.items():
            try:
                signal.signal(sig, h if h is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                pass
        shutil.rmtree(work_root, ignore_errors=True)
    return rows


def _recover_canary(sup, first, cfg, ccfg, work_root, first_row, log_path, run_id, standin_ids=()):
    """Retry a failed canary of a fast-fail-fallback provider: the same call, then the rerouted one, then (when
    `standin_ids` names the roster seats on this model) the stand-in. -> the row to report: WARN naming the first error
    when an attempt passed (a stand-in pass marks those seats `standin`), else FAIL listing every attempt's error."""
    adapter, provider = first.adapter, first.seat.get("provider")
    try:
        chain = adapter.fallback_chain(first.seat, ccfg)
    except Exception:
        chain = []
    steps = [("retry", first.seat)] + ([("rerouted", chain[0])] if chain else [])
    if standin_ids and len(chain) > 1:
        steps.append(("standin", chain[1]))
    errs = ["first call: " + first_row.detail]
    can = cfg.get("canary") or {}
    for n, (name, seat) in enumerate(steps):
        t = council_run._Task("%sr%d" % (first.key, n), "seat", seat, adapter)
        t.prompt_bytes, t.label = first.prompt_bytes, first.label
        wd = os.path.join(work_root, "%sr%d" % (first.key, n))
        try:
            os.makedirs(wd, mode=0o700)
            limits, t.mode = _canary_limits(cfg, provider)
            t.deadline_s, t.deadline_source = limits.deadline_s, "canary_timeout"
            t.launch = adapter.build(seat, first.ppath, wd, limits, ccfg)
            t.extractor = council_run._extractor(adapter, seat)
        except Exception as e:
            errs.append("%s: build failed (%s)" % (name, type(e).__name__))
            continue
        t.started = time.monotonic()
        sup.start(t.key, t.launch, limits, on_output=t.on_output)
        stop = time.monotonic() + float(can.get("timeout_s", 120)) * 2 + 30            # safety valve only
        while not t.done:
            for key, res in sup.poll(POLL_S):
                if key == t.key:
                    t.result, t.done = res, True
            if not t.done and time.monotonic() > stop:
                sup.kill_all()
                t.done, t.status = True, "preflight_safety_stop"
                break
        ok = council_run._classify(t) if t.result is not None else False
        _log(log_path, run_id, t)
        if ok:
            row = _canary_row(t, True)
            mark = None
            note = ""
            if name == "standin":
                mark = {"seats": list(standin_ids), "reason": "preflight:" + _scrub(first_row.detail, 160)}
                note = "; seat %s runs as its stand-in %s" % ("/".join(map(str, standin_ids)), seat.get("model"))
            return Check(provider, "canary", first.label, "WARN",
                         "passed on the %s attempt after a failure (%s); %s%s" % (name, first_row.detail, row.detail, note),
                         row.usage, mark)
        errs.append("%s: %s" % (name, _canary_row(t, False).detail))
    return Check(provider, "canary", first.label, "FAIL", " | ".join(errs), first_row.usage)


def _canary_row(task, ok):
    r = task.result
    provider = task.seat.get("provider")
    secs = "%.1fs" % r.total_s if r is not None and r.total_s is not None else "?"
    usage = council_run._usage_of(task)
    if ok and (task.detail or "").startswith("retried "):
        # the seat child retried before the first byte and then passed: a flaky provider, not a broken one
        return Check(provider, "canary", task.label, "WARN",
                     "ok after the seat child's own retry (%s), %s, model %s, %s, %s"
                     % (_scrub(task.detail, 100), secs, _scrub(task.reported or "(not reported)", 80), task.mode,
                        _usage_text(usage)), usage)
    if ok:
        return Check(provider, "canary", task.label, "PASS",
                     "ok %s, model %s, %s, %s" % (secs, _scrub(task.reported or "(not reported)", 80), task.mode,
                                                  _usage_text(usage)), usage)
    bits = ["status=%s" % task.status, secs]
    if r is not None and r.exit_code is not None:
        bits.append("exit %s" % r.exit_code)
    if task.status == "model_mismatch":
        bits.append("pinned %s, reported %s" % (_scrub(task.seat.get("model"), 60), _scrub(task.reported, 60)))
    if task.detail:
        bits.append(_scrub(task.detail, 120))
    part = (task.partial or "").strip()
    if part:
        bits.append("partial kept: %s" % _scrub(part, PARTIAL_MAX))
    return Check(provider, "canary", task.label, "FAIL", "; ".join(bits), usage)


def _usage_text(u):
    """'tokens in/out/reasoning N/N/N, cost $X' with 'unknown' for a field the provider did not report."""
    def f(v):
        return "unknown" if v is None else str(v)
    cost = u.get("cost_usd")
    return "tokens in/out/reasoning %s/%s/%s, cost %s" % (
        f(u.get("input_tokens")), f(u.get("output_tokens")), f(u.get("reasoning_tokens")),
        "unknown" if cost is None else "$%.6f" % cost)


def _log(log_path, run_id, task):
    r = task.result
    timing.append(log_path, {
        "run_id": run_id, "kind": "canary", "provider": task.seat.get("provider"),
        "model": task.seat.get("model"), "mode": task.mode, "prompt_bytes": task.prompt_bytes,
        "out_bytes": r.out_bytes if r else 0, "first_byte_s": r.first_byte_s if r else None,
        "max_gap_s": r.max_gap_s if r else None, "total_s": r.total_s if r else None,
        "outcome": task.status, "extensions": r.extensions if r else 0,
        "deadline_s": task.deadline_s, "deadline_source": task.deadline_source})


# ---------------------------------------------------------------- driver
def _sorted(checks):
    first = {"policy": 0, "engine": 1}
    order = {"policy": 0, "engine": 0, "free": 1, "canary": 2}
    return sorted(checks, key=lambda c: (first.get(c.provider, 2), c.provider, order.get(c.stage, 9)))


def blocking_fails(checks, allow_missing=()):
    allowed = set(allow_missing or ())
    return [c for c in checks if c.result == "FAIL" and c.provider not in allowed]


def failed_providers(checks):
    """Providers with any FAIL row (allowed or not): the set council_run.run needs."""
    return {c.provider for c in checks if c.result == "FAIL" and c.provider not in ("policy", "engine")}


def failed_stage(checks, allow_missing=()):
    bad = blocking_fails(checks, allow_missing)
    return bad[0].stage if bad else None


# ---------------------------------------------------------------- question path lint (fallback-chain row 2)
# Run 20261030T090651Z named two files by absolute path ("read it from disk") with no --context-file. Seats run in a temp
# cwd with the prompt on stdin: none could read them (GLM answered with tool-call markup, the chair saw three seats that
# never read them either). A path the question names must reach the seats as a --context-file.
_PATH_RE = re.compile(r"(?<![\w/~.:-])((?:~|/home|/mnt|/tmp|/root|/Users)/[^\s`'\"<>()\[\]{}|,;]*)")
_PATH_TRAIL = ".,;:)'\"`!?"


def question_paths(question):
    """Absolute or ~ paths named in the question text, in order, deduplicated (trailing punctuation dropped)."""
    out = []
    for m in _PATH_RE.finditer(question or ""):
        p = m.group(1).rstrip(_PATH_TRAIL)
        if p not in ("~", "") and p not in out:
            out.append(p)
    return out


def _norm(p):
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def uncovered_paths(question, context_files=()):
    """The question's paths that no --context-file covers. A path is covered by a context file that IS it, or that
    lies under it (a named directory whose files were attached, run 20261030T052203Z)."""
    ctx = [_norm(f) for f in context_files or ()]
    out = []
    for p in question_paths(question):
        n = _norm(p)
        if not any(c == n or c.startswith(n.rstrip("/") + "/") for c in ctx):
            out.append(p)
    return out


def _inside(path, root):
    n = _norm(path)
    return n == root or n.startswith(root.rstrip("/") + "/")


def grounded_rows(cfg):
    """`policy` rows for a grounded run (`council.py run --grounded`): the root and images, then one row per seat saying
    whether it gets read-only file tools or stays text only (`ungrounded`, with the reason). Free; none without the flag."""
    g = (cfg or {}).get("_grounded")
    if not g:
        return []
    imgs = g.get("images") or []
    rows = [Check("policy", "policy", "grounded_root", "PASS",
                  "read-only root %s; %d image(s)%s" % (g["root"], len(imgs),
                                                        ": " + ", ".join(os.path.basename(i) for i in imgs) if imgs else ""))]
    n = 0
    for s in (cfg.get("seat") or []):
        try:
            adapter = providers.get(s.get("provider")) if providers.known(s.get("provider")) else None
        except (NotImplementedError, KeyError):
            adapter = None
        ok, why = grounded.seat_support(cfg, s, adapter)
        n += bool(ok)
        if ok:
            how = ("Read, Grep, Glob only (no shell, edit or web), file tools confined to the root" if s.get("provider") == "claude"
                   else "-s read-only (no writes, no network, web search off), rooted at the repo; NOTE: codex's "
                        "read-only sandbox is not confined to the root (it can read other files; "
                        "[grounded] codex = false turns it off)" if s.get("provider") == "codex" else "read-only file tools")
            if grounded.probes_on(cfg, s, adapter):
                how += ("; probes ON: may run read-only commands/scripts to confirm claims, reported as `Probe:` "
                        "lines ([grounded] probes = %s; probes = false on the seat turns it off)"
                        % json.dumps(grounded.cfg_of(cfg)["probes"]))
                if s.get("provider") == "claude":
                    how += "; Bash limited to " + ", ".join(providers.CLAUDE_PROBE_BASH)
            rows.append(Check("policy", "policy", "grounded:%s" % s.get("id"), "PASS", "grounded: %s" % how))
        else:
            rows.append(Check("policy", "policy", "grounded:%s" % s.get("id"), "WARN", "ungrounded: %s" % why))
    if not n:
        rows.append(Check("policy", "policy", "grounded_seats", "WARN", "no seat can use file tools: nothing is grounded"))
    return rows


def depth_rows(cfg):
    """One `policy` row: the run's depth (`council.py run --depth quick|standard`) and its model-call estimate, so the
    gate shows what a standard run adds before any spend. N = the roster's seats; salvage / hedge / fallback calls extra."""
    depth = critique.depth_of(cfg)
    n = len((cfg or {}).get("seat") or [])
    calls = critique.estimate_calls(n, depth)
    if depth == "standard":
        detail = ("standard: round 1, then a critique round, then the chair: about %d model calls for %d seats "
                  "(2N+1; N critiques only for the seats that answer; a critique needs 2 answers; salvage, hedge and "
                  "fallback calls extra)" % (calls, n))
    else:
        detail = "quick: independent answers, then the chair: about %d model calls for %d seats (N+1)" % (calls, n)
    return [Check("policy", "policy", "depth", "PASS", detail)]


def question_rows(question, context_files=(), allow_paths=False, root=None):
    """`policy` rows for the question (none without a question): one FAIL per path it names that is not attached (the
    row names the fix), WARN rows under --allow-paths, else one PASS. Free, before every other stage."""
    if question is None:
        return []
    bad = uncovered_paths(question, context_files)
    rows = []
    if root:
        # --grounded: a path inside the root is readable by the grounded seats; a text-only seat still cannot read it
        for p in [p for p in bad if _inside(p, root)]:
            rows.append(Check("policy", "policy", "question_path", "WARN",
                              "%s is inside the --grounded root: grounded seats read it, ungrounded seats cannot" % p))
        bad = [p for p in bad if not _inside(p, root)]
    if not bad:
        return rows or [Check("policy", "policy", "question_path", "PASS",
                              "no file path in the question outside --context-file (%d attached)" % len(context_files or ()))]
    if allow_paths:
        return rows + [Check("policy", "policy", "question_path", "WARN",
                             "--allow-paths: %s is not attached; seats cannot read it (temp cwd, no disk access)" % p)
                       for p in bad]
    return rows + [Check("policy", "policy", "question_path", "FAIL",
                         "pass --context-file %s (seats cannot read disk; --allow-paths if only illustrative)" % p)
                   for p in bad]


# ---------------------------------------------------------------- settled rulings (P23, truth 7)
# Seats are outside services. Owner rulings reach them only as a scrubbed excerpt (`--rulings-file`), never as a
# whole rulings or memory file, which holds names, addresses and application details. ONE list, refused on every verb
# (owner ruling 2026-11-18: truth 7 wins over truth 3 for whole rulings or memory files): `ask` and `run` exit 2 before
# anything is sent, `preflight` shows a FAIL row (exit 1).
# A name matches the basename (any case); an entry ending in "/" is a folder pattern (`*` = one path segment).
# By name always; a renamed or copied whole file only in part, when its content still has that file's shape
# (`private_content`, which states what passes and the measured share).
# Limits: a backstop only. It never sees names, employers or figures; the excerpt the caller writes is the real guard.
PRIVATE_FILES = ("DECISIONS.md", "MEMORY.md", ".claude/projects/*/memory/")
# What each verb does with such a file, in ONE place: "refuse" (exit 2 before anything is sent or a run dir is made),
# "fail" (a FAIL `private_file` row, the verb's failing exit) or "warn" (a WARN row). No override flag: the way out is
# an excerpt.
PRIVATE_OUTCOME = {"ask": "refuse", "run": "refuse", "preflight": "fail"}
RULINGS_LABEL = council_run.RULINGS_LABEL
PRIVATE_FIX = "write a scrubbed excerpt of the rulings that bear on the question and pass it with --rulings-file"
_EMAIL = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_HOME_PATH = re.compile(r"^(?:~|/home|/root|/Users|/mnt/[^/]+/Users)(?:/|$)")


def _private_dirs():
    out = []
    for e in PRIVATE_FILES:
        if e.endswith("/"):
            segs = [r"[^/]+" if s == "*" else re.escape(s) for s in e.strip("/").split("/")]
            out.append(re.compile(r"(?:^|/)" + "/".join(segs) + r"/"))
    return out


def private_file(path):
    """-> the reason `path` is a whole rulings or memory file (PRIVATE_FILES), or None. Checks the path as given and
    resolved, so a symlink or a relative path does not slip past, then the content (`private_content`), which catches
    a renamed or copied whole file only in part: only when it still has that file's shape (see there)."""
    names = {e.casefold() for e in PRIVATE_FILES if not e.endswith("/")}
    for p in {os.path.abspath(os.path.expanduser(path)), _norm(path)}:
        if os.path.basename(p).casefold() in names:
            return "a whole %s file" % os.path.basename(p)
        if any(rx.search(p) for rx in _private_dirs()):
            return "a file in a Claude memory folder"
    try:
        with open(os.path.expanduser(path), "rb") as f:
            text = f.read(_CONTENT_CAP).decode("utf-8", "replace")
    except OSError:
        return None
    return private_content(text)


# Content signatures of the three real shapes (structure only; the test fixtures are synthetic). Each has a threshold,
# so a short scrubbed excerpt passes: an excerpt is a few plain sentences, not the log, the index or a memory file.
_CONTENT_CAP = 4 << 20
_FRONTMATTER = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_FM_KEY = re.compile(r"(?m)^\s*([A-Za-z_]+):")
_MEM_TYPE = re.compile(r"(?m)^\s*type:\s*[\"']?(?:user|feedback|project|reference)[\"']?\s*$")
_BARE_MD_LINK = re.compile(r"\]\((?![A-Za-z][A-Za-z0-9+.-]*:)[\w.-]+\.md\)")    # [title](file_name.md): no folder, no scheme
_MEM_NAME_LINK = re.compile(r"\]\((?:user|feedback|project|reference)_[\w.-]+\.md\)")     # [title](feedback_x.md)
_MEM_HEAD = re.compile(r"(?i)^#{1,2}\s.*\bmemor(?:y|ies)\b")
_DEC_HEAD = re.compile(r"(?i)^#{1,2}\s.*\bdecisions?\b")
_RUN_SLUG = re.compile(r"\b\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)+\b")   # an orchestrator run folder name
_MODE_LINE = re.compile(r"^\**Mode\**:\s*\**(?:away|present)\b")           # `Mode: away` (boss:away)
_PICKS = re.compile(r"^[-*#>\s]*\**OWNER PICKS\b")                          # the marker as an entry, not a quote of it
_DATED = re.compile(r"^\s*(?:[-*]|#{1,4})\s+\**\d{4}-\d{2}-\d{2}\b")
_ATTEMPT = re.compile(r"#\d+[a-z]?\.\d+\b")
_OWNER = re.compile(r"(?i)\bowner(?:'s)?\b")
DECISIONS_MIN_SCORE, DECISIONS_MIN_LINES, INDEX_MIN_LINKS = 3, 6, 8
_DEC_MARKS = ("Decisions heading", "Mode line", "OWNER PICKS")


def private_content(text):
    """-> the reason `text` is a whole rulings log, memory index or memory file (by shape), or None. Counts and kinds
    only, never the text.
    - memory file: frontmatter with `name` and `description` plus `originSessionId`, `node_type` or a memory `type`
      (user / feedback / project / reference).
    - memory index: at least INDEX_MIN_LINKS bullet links to memory-type file names (`feedback_x.md`), or as many links
      to bare `.md` names on at least 40% of the lines of an untitled file (a docs index has a title), or a "memory"
      heading with at least 3 of them.
    - rulings log (an orchestrator DECISIONS.md): at least DECISIONS_MIN_LINES lines, at least one of its own markers
      (a "Decisions" first heading 2, a `Mode: away|present` line in the first five lines 2, an OWNER PICKS entry 2)
      and a score of at least DECISIONS_MIN_SCORE, the rest being run-folder vocabulary that a PLAN.md, brief or review
      shares: a run folder name in the first heading 1, 3+ dated entries 1, 2+ attempt ids (`#26.1`) 1, 3+ lines naming
      the owner 1, and those on 30% of the lines 1 more. A changelog of dated bullets scores 1, a README 0, a plan or a
      review has no marker of its own.
    Limits: a backstop that catches renamed copies only in part (a file's real name is always refused, in
    `private_file`). It passes a rulings log under DECISIONS_MIN_LINES lines or with no own marker where it looks (one
    line put in front defeats the first-heading marker, and lines in front push a Mode line past line five), a
    memory note without its frontmatter, a memory index not in index form, and anything past the first _CONTENT_CAP bytes (4 MiB) of a file. Measured
    2026-11-18 on this machine's real files (counts only): rulings logs 47 of 70 (of the 23 misses, 7 have fewer than
    6 lines, 16 have no marker where the check looks), memory notes 438 of 450, memory indexes 2 of 8 (all 8 are
    refused by name). Never pass a copy of such a file under another name: write an excerpt."""
    text = text or ""
    m = _FRONTMATTER.match(text)
    if m:
        fm = m.group(1)
        keys = set(_FM_KEY.findall(fm))
        if {"name", "description"} <= keys and ({"originSessionId", "node_type"} & keys or _MEM_TYPE.search(fm)):
            return "a whole Claude memory file by its content (memory frontmatter)"
    ne = [l for l in text.splitlines() if l.strip()]
    bullets = [l for l in ne if l.lstrip().startswith(("-", "*"))]
    links = sum(len(_BARE_MD_LINK.findall(l)) for l in bullets)
    linked = sum(1 for l in bullets if _BARE_MD_LINK.search(l))
    typed = sum(len(_MEM_NAME_LINK.findall(l)) for l in bullets)
    untitled = bool(ne) and ne[0].lstrip().startswith(("-", "*"))       # MEMORY.md has no title; a docs index has one
    if typed >= INDEX_MIN_LINKS or (untitled and links >= INDEX_MIN_LINKS and linked >= 0.4 * len(ne)) or \
            (ne and _MEM_HEAD.match(ne[0]) and linked >= 3):
        return "a whole memory index by its content (%d links to .md files, %d to memory-type names, on %d of %d lines)" % (
            links, typed, linked, len(ne))
    if len(ne) < DECISIONS_MIN_LINES:
        return None
    first = ne[0]
    owner = sum(1 for l in ne if _OWNER.search(l))
    marks = [("Decisions heading", 2, bool(_DEC_HEAD.match(first))),
             ("Mode line", 2, any(_MODE_LINE.match(l) for l in ne[:5])),
             ("OWNER PICKS", 2, any(_PICKS.search(l) for l in ne)),
             ("run name heading", 1, bool(first.startswith("#") and _RUN_SLUG.search(first))),
             ("dated entries", 1, sum(1 for l in ne if _DATED.match(l)) >= 3),
             ("attempt ids", 1, sum(len(_ATTEMPT.findall(l)) for l in ne) >= 2),
             ("owner lines", 1, owner >= 3),
             ("owner on 30% of lines", 1, owner >= 3 and owner >= 0.3 * len(ne))]
    hit = [(k, w) for k, w, on in marks if on]
    if any(k in _DEC_MARKS for k, _ in hit) and sum(w for _, w in hit) >= DECISIONS_MIN_SCORE:
        return "a whole DECISIONS.md by its content (%s; %d lines)" % (", ".join(k for k, _ in hit), len(ne))
    return None


# Wider detectors for the strict rulings excerpt only (`--rulings-file`). The table scrub and drift keep `_KEYISH`.
_R_EMAIL = (
    re.compile(r"\w[\w.+-]*\s*[(\[{<]\s*at\s*[)\]}>]\s*\w[\w-]*", re.I),                     # name(at)host, [at], {at}
    re.compile(r"\w[\w.+-]*\s+@\s*\w[\w-]*(?:\s*\.\s*\w[\w-]*)*\s*\.\s*[A-Za-z]{2,}\b"),         # name @ host.tld
    re.compile(r"\w[\w.+-]*@\s*\w[\w-]*(?:\s*\.\s*\w[\w-]*)*\s+\.\s*[A-Za-z]{2,}\b"),          # name@host . tld
    re.compile(r"\b\w+\s+at\s+\w[\w-]*\s+dot\s+[A-Za-z]{2,}\b", re.I),                          # name at host dot tld
)
_TLDS = ("com|net|org|io|dev|co|uk|de|fr|nl|eu|us|ca|au|app|ai|me|edu|gov|info|biz|xyz|site|tech|cloud|online|"
         "page|gg|tv|fm|jp")                    # not sh, so, in, js, py, md: file names in a ruling stay clean
_R_URL = re.compile(r"(?<![\w@.-])(?:www\.[\w-]+(?:\.[\w-]+)+|(?:[a-z0-9][a-z0-9-]*\.)+(?:%s))(?![\w-])(?:/\S*)?"
                    % _TLDS, re.I)
_R_HOME = (
    re.compile(r"\$\{?HOME\}?(?![A-Za-z0-9_])|%(?:USERPROFILE|HOMEPATH|HOMEDRIVE)%|\$env:(?:USERPROFILE|HOME)\b", re.I),
    re.compile(r"(?:^|[\s\"'(=:`])(?:~[\w.-]*|/home|/root|/Users|/mnt/[a-z]/Users)(?:/|\\)"),       # anywhere in the line
    re.compile(r"\b[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]", re.I),                 # C:\Users\..
    re.compile(r"\\\\[\w.$-]+\\(?:Users|home)\\", re.I),                                       # \\host\Users\..
)
_R_MEMORY = re.compile(r"\.claude[\\/]+projects[\\/]+[^\\/\s]+[\\/]+memory\b"
                       r"|(?:^|[\s\"'(=:`\\/])projects[\\/]+-[\w-]+[\\/]+memory\b")
_R_KEY = (
    _KEYISH,
    re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant|or|v\d)[-_]\w[\w-]{8,}"),
    re.compile(r"\b(?:pplx|xai|gsk|hf|r8|npm|dsk|ak)[-_][A-Za-z0-9]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_\w{20,}|\bglpat-[\w-]{16,}"),
    re.compile(r"\bxox[abposr]-[\w-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}"),                                      # a JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[\w.~+/-]{16,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d)\s*[:=]\s*[\"']?[\w.~+/-]{8,}"),
)
_R_PHONE = (
    re.compile(r"(?<![\w.,:/#-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w.,:/-])"),  # 415-555-0100
    re.compile(r"(?<![\w.,:/#-])\+\d{1,3}(?:[\s.-]\(?\d{1,4}\)?){2,5}(?![\w.,:/%-])"),                # +44 20 7946 0000
)
PHONE_MIN_DIGITS = 7                                       # +21.5% or +1.2.3 is a figure, not a number to call


def rulings_hits(text):
    """-> [(line number, kind)] of what a scrubbed rulings excerpt must not hold: an email address (also written with
    (at)/[at] or spaced out, or split over a line break), a URL (with or without a scheme), a home path ($HOME,
    ${HOME}, %USERPROFILE%, ~, /home, /Users, C:\\Users), a Claude memory-folder path, a key-shaped string (sk-, pplx-,
    ghp_, AIza, AKIA, xox, a JWT, a private key, Bearer, key = ...) or a phone number. Kinds only: never the matched
    text. A backstop: it never sees names, employers or figures; the excerpt the caller writes is the real guard."""
    hits = []
    lines = (text or "").splitlines()
    for n, line in enumerate(lines, 1):
        nxt = lines[n] if n < len(lines) else ""
        joined = line.rstrip() + nxt.lstrip()             # an address split over a line break: name.\n@host.tld
        if _EMAIL.search(line) or any(rx.search(line) for rx in _R_EMAIL) or \
                (nxt.lstrip().startswith("@") and _EMAIL.search(joined)):
            hits.append((n, "email address"))
        if grounded._URL.search(line) or _R_URL.search(line):
            hits.append((n, "URL"))
        if any(_HOME_PATH.match(p) for p in question_paths(line)) or any(rx.search(line) for rx in _R_HOME):
            hits.append((n, "path under a home directory"))
        if _R_MEMORY.search(line):
            hits.append((n, "Claude memory-folder path"))
        if any(rx.search(line) for rx in _R_KEY):
            hits.append((n, "key-shaped string"))
        if any(sum(c.isdigit() for c in m.group(0)) >= PHONE_MIN_DIGITS for rx in _R_PHONE for m in rx.finditer(line)):
            hits.append((n, "phone number"))
    return hits


def private_hits(files):
    """-> [(file, reason)] for each --context-file or --rulings-file that is a whole rulings or memory file."""
    out = []
    for f in files or ():
        why = private_file(f)
        if why:
            out.append((f, why))
    return out


def private_refusal(files, verb):
    """-> the refusal message when PRIVATE_OUTCOME[verb] is "refuse" and a file is private, else None. The caller
    exits 2 with it before anything is sent."""
    if PRIVATE_OUTCOME.get(verb) != "refuse":
        return None
    bad = private_hits(files)
    if not bad:
        return None
    return "%s refused, nothing sent: %s. Fix: %s." % (verb, "; ".join("%s is %s" % b for b in bad), PRIVATE_FIX)


def private_rows(files, verb="run"):
    """`policy` rows for a --context-file or --rulings-file that is a whole rulings or memory file, for a verb whose
    PRIVATE_OUTCOME is "fail" (FAIL, today `preflight`) or "warn" (WARN). A "refuse" verb has stopped before this."""
    result = {"fail": "FAIL", "warn": "WARN"}.get(PRIVATE_OUTCOME.get(verb))
    if not result:
        return []
    return [Check("policy", "policy", "private_file", result,
                  "%s is %s: seats are outside services; %s" % (f, why, PRIVATE_FIX))
            for f, why in private_hits(files)]


def run(cfg, allow_missing=(), free_only=False, question=None, context_files=(), allow_paths=False, verb="run"):
    """-> (ok, checks). Raises PolicyError before any adapter is touched. With `question`, the question path lint
    row comes first (fallback-chain row 2). A `--rulings-file` (cfg `_rulings`) counts as attached for that lint."""
    allow_missing = list(allow_missing or ())
    files = list(context_files or ())
    rulings = (cfg or {}).get("_rulings") or {}
    if rulings.get("path"):
        files.append(rulings["path"])
    checks = policy_stage(cfg, allow_missing)
    checks += question_rows(question, files, allow_paths, root=((cfg or {}).get("_grounded") or {}).get("root"))
    checks += private_rows(files, verb)
    checks += grounded_rows(cfg)
    checks += depth_rows(cfg)
    checks += engine_stage(cfg)
    checks += orphan_rows(cfg)
    checks += long_watch_rows(cfg)
    if blocking_fails(checks, allow_missing):
        return False, _sorted(checks)
    with ThreadPoolExecutor(max_workers=1) as ex:
        dfut = ex.submit(_drift_collect, cfg)
        checks += free_stage(cfg)
        snap = dfut.result()
    old = drift.load_last(cfg) if snap else None
    checks += drift_rows(snap, old, cfg)
    if blocking_fails(checks, allow_missing) or free_only:
        ok = not blocking_fails(checks, allow_missing)
        if ok:
            _drift_observe(cfg, snap, old, "preflight --free-only" if free_only else "preflight")
        return ok, _sorted(checks)
    skip = (failed_providers(checks) & set(allow_missing)) | unavailable_providers(checks)
    checks += canary_stage(cfg, skip, standins(checks))
    ok = not blocking_fails(checks, allow_missing)
    if ok:
        _drift_observe(cfg, snap, old, "preflight")
    return ok, _sorted(checks)


# ---------------------------------------------------------------- orphan sweep (row 40, D54)
CAPTURE_MAX = 256 << 20


def _age(sec):
    sec = max(0, int(sec))
    if sec < 90:
        return "%ds" % sec
    if sec < 5400:
        return "%dm%02ds" % (sec // 60, sec % 60)
    if sec < 172800:
        return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dd%02dh" % (sec // 86400, sec % 86400 // 3600)


def _read_json(path):
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json_atomic(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
        try:
            council_run._write_all(fd, json.dumps(data, indent=1).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_capped(path, cap=CAPTURE_MAX):
    try:
        with open(path, "rb") as f:
            return f.read(cap)
    except OSError:
        return b""


def _tail(path, n=4096):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read(n)
    except OSError:
        return b""


def sweep_orphans(cfg):
    """Find the watch calls whose runner is gone, by tag (D54). -> {"live": [...], "swept": [...], "errors": [...],
    "runner_alive": n}.

    Each call has a record <state_dir>/watch-calls/<tag>.json (council_run.register_watch_call). Skipped: a record
    whose runner is alive (pid and start time match). Otherwise the call's tag decides:
    - any live process tagged with it: LIVE. Left running; reported with its age, seat, call and run id.
    - none: FINISHED. Its answer (stdout capture + stderr + the CLI's final file, through the adapter's extractor,
      exactly as a seat) is appended as late per owner ruling 3 when there is one and it is not appended yet; its
      terminal status is recorded in summary.json and calls.jsonl; its leftovers (stderr capture, status file, the
      workdir once no other record uses it, the record itself) are removed.
    The sweep never sends a signal and never launches a call: a finished call with no answer is recorded as such
    (no fallback or salvage: that would be spend with no supervisor)."""
    state_dir = cfg.get("state_dir") or os.path.expanduser("~/.claude-council")
    wdir = council_run.watch_dir(state_dir)
    res = {"live": [], "swept": [], "errors": [], "runner_alive": 0}
    try:
        names = sorted(os.listdir(wdir))
    except OSError:
        return res
    for name in names:
        if not name.endswith(".json") or name.endswith(".end.json"):
            continue
        path = os.path.join(wdir, name)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue                        # another start is sweeping it right now
            if os.fstat(fd).st_nlink == 0:
                continue                        # it was swept (unlinked) between our listdir and our lock
            rec = _read_json(path)
            if rec is None or rec.get("tag") != name[:-5] or not isinstance(rec.get("out_dir"), str):
                res["errors"].append("%s: unreadable record (left in place)" % name)
                continue
            pid = rec.get("runner_pid")
            if isinstance(pid, int) and council_run.proc_alive(pid, rec.get("runner_start")):
                res["runner_alive"] += 1
                continue
            pids = [p for p in netwatch.tagged_pids(rec["tag"]) if p != os.getpid()]
            if pids:
                res["live"].append({
                    "seat": rec.get("seat"), "call": rec.get("call"), "run_id": rec.get("run_id"),
                    "out_dir": rec.get("out_dir"), "tag": rec["tag"], "pids": pids,
                    "started_epoch": float(rec.get("started_epoch") or 0.0),
                    "age_s": max(0.0, time.time() - float(rec.get("started_epoch") or time.time()))})
                continue
            try:
                res["swept"].append(_sweep_finished(state_dir, path, rec))
                res["swept"][-1]["started_epoch"] = float(rec.get("started_epoch") or 0.0)
            except Exception as e:                  # one bad record must not stop the others
                res["errors"].append("%s: sweep raised %s (left in place)" % (name, type(e).__name__))
        finally:
            os.close(fd)
    _drop_stray_end_files(wdir, names)
    _finalize_runs(wdir, {o["out_dir"] for o in res["swept"]})
    for k in ("live", "swept"):                  # oldest call first, whatever the tags are
        res[k].sort(key=lambda o: (o.get("started_epoch") or 0.0, str(o.get("call")), o["tag"]))
    return res


def _drop_stray_end_files(wdir, names):
    """A reaper's end record with no call record is garbage: the record exists before the launch, and the runner
    removes both only once the reaper has ended (a D53 stop removes the record first; the killed reapers write theirs
    afterwards)."""
    have = {n[:-5] for n in names if n.endswith(".json") and not n.endswith(".end.json")}
    for n in names:
        if n.endswith(".end.json") and n[:-9] not in have:
            try:
                os.unlink(os.path.join(wdir, n))
            except OSError:
                pass


def _finalize_runs(wdir, out_dirs):
    """A run whose last orphan has been swept is settled: `phase` done + `runner_lost`, and a `late_synthesis` that
    still says `waiting` (the D50 shape: the runner died before the first answer, so no chair will run) says so."""
    if not out_dirs:
        return
    try:
        left = {(_read_json(os.path.join(wdir, n)) or {}).get("out_dir") for n in os.listdir(wdir)
                if n.endswith(".json") and not n.endswith(".end.json")}
    except OSError:
        left = set()
    for out_dir in sorted(out_dirs - left):
        sp = os.path.join(out_dir, "summary.json")
        summ = _read_json(sp)
        if summ is None:
            continue
        summ["phase"] = "done"
        summ["runner_lost"] = True
        ls = summ.get("late_synthesis")
        if isinstance(ls, dict) and ls.get("status") == "waiting":
            summ["late_synthesis"] = {
                "status": "runner_lost",
                "reason": "the runner was lost before any answer arrived; no chair ran and none will",
                "late": ["seats/%s.%s.late.md" % (council_run._safe(e.get("seat")), e.get("call"))
                         for e in summ.get("late") or []]}
        try:
            _write_json_atomic(sp, summ)
        except OSError:
            pass


def _end_status(rec):
    """-> (council status, exit code or None, ended epoch) from the reaper's end record."""
    end = _read_json(rec.get("status_file") or "")
    ended = None
    code = None
    if end is not None and end.get("via") != "lost_child" and isinstance(end.get("exit_code"), int):
        code = end["exit_code"]
    if end is not None and isinstance(end.get("ended_at"), (int, float)):
        ended = float(end["ended_at"])
    if ended is None:
        try:
            ended = os.stat(rec.get("stdout") or "").st_mtime
        except OSError:
            ended = time.time()
    if code is None:
        return "unknown_end", None, ended
    return ("ok" if code == 0 else "exit_%d" % code), code, ended


def _sweep_finished(state_dir, entry_path, rec):
    from .supervise import Result
    out_dir = rec["out_dir"]
    seat_id = str(rec.get("seat"))
    sid = council_run._safe(seat_id)
    call = str(rec.get("call"))
    status, code, ended = _end_status(rec)
    spec = rec.get("seat_spec") if isinstance(rec.get("seat_spec"), dict) else {}
    adapter = council_run._adapter(spec.get("provider"))
    raw = _read_capped(rec.get("stdout") or "")
    started = float(rec.get("started_epoch") or ended)
    out = {"seat": seat_id, "call": call, "run_id": rec.get("run_id"), "tag": rec["tag"], "out_dir": out_dir,
           "exit_code": code, "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended)),
           "answered": False, "appended": False, "late_md": None}
    text = None
    if adapter is None:
        out["status"] = "no_adapter"
    else:
        task = council_run._Task(str(rec.get("key") or seat_id), str(rec.get("kind") or "seat"), spec, adapter)
        task.call = call
        task.launch = types.SimpleNamespace(final_text_path=rec.get("final_text_path"))
        task.extractor = council_run._extractor(adapter, spec)
        for i in range(0, len(raw), 65536):
            task.on_output(raw[i:i + 65536])
        task.result = Result(status=status, exit_code=code, stdout=b"", stderr_tail=_tail(rec.get("stderr") or ""),
                             first_byte_s=None, max_gap_s=0.0, total_s=max(0.0, ended - started),
                             out_bytes=len(raw), extensions=0, truncated=status != "ok")
        task.done = True
        council_run._classify(task)
        out["status"] = task.status
        out["detail"] = task.detail
        if task.status == "ok" and (task.text or "").strip():
            text = task.text
    at_s = round(ended - float(rec.get("run_t0_epoch") or started), 1)
    if text is not None:
        out["answered"] = True
        out["late_md"] = "seats/%s.%s.late.md" % (sid, call)
        out["appended"] = _append_late(out_dir, sid, seat_id, call, text, raw, at_s)
    _record_end(out_dir, rec, out, at_s)
    _reap_leftovers(state_dir, entry_path, rec, keep_stdout=text is None)
    return out


def _append_late(out_dir, sid, seat_id, call, text, raw, at_s):
    """Owner ruling 3 (same layout as council_run's record_late; every step is guarded, so a sweep that died half
    way finishes the job and a second sweep adds nothing). -> True when something was appended."""
    seats = os.path.join(out_dir, "seats")
    os.makedirs(seats, mode=0o700, exist_ok=True)
    changed = False
    raw_path = os.path.join(seats, "%s.%s.late.raw" % (sid, call))
    if not os.path.exists(raw_path):
        council_run._write(raw_path, bytes(raw))
        changed = True
    md_path = os.path.join(seats, "%s.%s.late.md" % (sid, call))
    if not os.path.exists(md_path):
        council_run._write(md_path, text)
        changed = True
    all_path = os.path.join(seats, sid + ".late.md")
    head = "## %s (late: answered %.1f s into the run, after its runner was lost; found by the orphan sweep; " \
           "not used by the chair)" % (call, at_s)
    try:
        with open(all_path, "r", encoding="utf-8", errors="replace") as f:
            have = f.read()
    except OSError:
        have = ""
    if "## %s (late" % call not in have:
        council_run._write(all_path, (have.rstrip("\n") + "\n\n" if have.strip() else "")
                           + head + "\n\n" + text.strip() + "\n")
        changed = True
    syn = os.path.join(out_dir, "chair", "synthesis.md")
    mark = "> LATE: seat %s (%s) answered" % (seat_id, call)
    try:
        with open(syn, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError:
        body = None
    if body is not None and mark not in body:
        fd = os.open(syn, os.O_WRONLY | os.O_APPEND)
        try:
            council_run._write_all(fd, ("\n%s at %.1f s into the run, after its runner was lost (orphan sweep); "
                                       "not used by the chair\n" % (mark, at_s)).encode("utf-8"))
        finally:
            os.close(fd)
        changed = True
    return changed


def _record_end(out_dir, rec, out, at_s):
    """The call's terminal status, in calls.jsonl, summary.json and (when it lists the call) the seat json."""
    seat_id, call = out["seat"], out["call"]
    try:
        council_run._append_sync(os.path.join(out_dir, "calls.jsonl"), {
            "seat": seat_id, "call": call, "key": rec.get("key"), "tag": rec["tag"], "status": out["status"],
            "ended_at": out["ended_at"], "by": "orphan_sweep", "answered": out["answered"]})
    except OSError:
        pass
    sp = os.path.join(out_dir, "summary.json")
    summ = _read_json(sp)
    if summ is not None:
        orphans = [o for o in summ.get("orphans") or [] if o.get("tag") != rec["tag"]]
        orphans.append({"seat": seat_id, "call": call, "tag": rec["tag"], "status": out["status"],
                        "answered": out["answered"], "ended_at": out["ended_at"]})
        summ["orphans"] = orphans
        if out["answered"] and not any(e.get("seat") == seat_id and e.get("call") == call
                                       for e in summ.get("late") or []):
            summ.setdefault("late", []).append({"seat": seat_id, "call": call, "at_s": at_s, "by": "orphan_sweep"})
        if summ.get("late_pending"):
            summ["late_pending"] = [e for e in summ["late_pending"]
                                    if not (e.get("seat") == seat_id and e.get("call") == call)]
            if not summ["late_pending"]:
                summ.pop("late_pending")
        if summ.get("phase") != "done":
            summ["phase"] = "runner_lost"
        _write_json_atomic(sp, summ)
    jp = os.path.join(out_dir, "seats", council_run._safe(seat_id) + ".json")
    sj = _read_json(jp)
    if sj is not None and isinstance(sj.get("calls"), list):
        for c in sj["calls"]:
            if isinstance(c, dict) and c.get("call") == call and c.get("running"):
                c.update(running=False, status=out["status"], orphan_sweep=True,
                         role="late" if out["answered"] else "failed")
        _write_json_atomic(jp, sj)


def _reap_leftovers(state_dir, entry_path, rec, keep_stdout):
    """Files and directories only (nothing here signals a process). The stdout capture stays when it is the only
    record of a call that gave no answer, and a hedge's or fallback's `seats/<sid>.<call>.raw` always stays; the
    workdir goes once no other record uses it."""
    out = rec.get("stdout")
    private = isinstance(out, str) and out.endswith(".stdout")      # an original's capture; a hedge's raw stays
    for p in (rec.get("stderr"), rec.get("status_file")) + ((out,) if private and not keep_stdout else ()):
        if isinstance(p, str) and p:
            try:
                os.unlink(p)
            except OSError:
                pass
    root = rec.get("work_root")
    if isinstance(root, str) and root and not os.path.islink(root) and os.path.isdir(root):
        base = os.path.basename(os.path.normpath(root))
        parent = os.path.realpath(os.path.dirname(os.path.normpath(root)))
        safe_parents = {os.path.realpath(d) for d in (tempfile.gettempdir(), "/tmp", "/var/tmp")}
        shared = False
        wdir = council_run.watch_dir(state_dir)
        try:
            others = [n for n in os.listdir(wdir) if n.endswith(".json") and not n.endswith(".end.json")
                      and os.path.join(wdir, n) != entry_path]
        except OSError:
            others = []
        for n in others:
            o = _read_json(os.path.join(wdir, n))
            if o is not None and o.get("work_root") == root:
                shared = True
        if base.startswith("council-") and parent in safe_parents and not shared:
            shutil.rmtree(root, ignore_errors=True)
    try:
        os.unlink(entry_path)
    except OSError:
        pass


def orphan_lines(res):
    """Human lines for a sweep result: live first (they need a decision), then what was swept, then problems."""
    lines = []
    for o in res["live"]:
        lines.append("LIVE seat %s call %s run %s age %s pid %s: still running after its runner was lost; left "
                     "running, never killed by the sweep" % (o["seat"], o["call"], o["run_id"], _age(o["age_s"]),
                                                              ",".join(str(p) for p in o["pids"])))
    for o in res["swept"]:
        if o["answered"]:
            lines.append("ENDED seat %s call %s run %s status %s: answer %s as late (%s)"
                         % (o["seat"], o["call"], o["run_id"], o["status"],
                            "appended" if o["appended"] else "already appended", o["late_md"]))
        else:
            lines.append("ENDED seat %s call %s run %s status %s: no answer, recorded unresolved (nothing relaunched)"
                         % (o["seat"], o["call"], o["run_id"], o["status"]))
    lines += ["ERROR %s" % e for e in res["errors"]]
    return lines


def orphan_rows(cfg):
    """Preflight rows for the sweep: one WARN per orphan (never a FAIL), none when there is nothing to report."""
    try:
        res = sweep_orphans(cfg)
    except Exception as e:                       # the sweep must never fail a preflight
        return [Check("orphans", "free", "orphans", "WARN", "orphan sweep raised %s" % type(e).__name__)]
    rows = []
    for o, line in zip(res["live"] + res["swept"], orphan_lines({"live": res["live"], "swept": res["swept"],
                                                                 "errors": []})):
        rows.append(Check("orphans", "free", "orphan:%s.%s" % (o["seat"], o["call"]), "WARN", line))
    rows += [Check("orphans", "free", "orphans", "WARN", e) for e in res["errors"]]
    return rows


# ---------------------------------------------------------------- long watch calls (row 46, D58)
LONG_WATCH_FACTOR = 2.0


def _soft_deadline(cfg, rec):
    """The soft deadline (s) of a registered watch call, or None when it has none.

    Preferred: `soft_at_s` in the record itself. Otherwise the call's seat is looked up the way the runner does it:
    only a hedged seat (hedge on, listed, buffered) has one, = council_run.hedge_at over the latency samples and the
    size of the call's prompt file. A chair call has none."""
    v = rec.get("soft_at_s")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    spec = rec.get("seat_spec") if isinstance(rec.get("seat_spec"), dict) else {}
    provider = spec.get("provider")
    if not provider or str(rec.get("kind") or "").startswith("chair") or not council_run.hedged(cfg, provider):
        return None
    state_dir = cfg.get("state_dir") or os.path.expanduser("~/.claude-council")
    try:
        size = os.stat(str(rec.get("workdir") or "") + ".prompt.md").st_size
    except OSError:
        size = 0
    soft, _src = council_run.hedge_at(cfg, os.path.join(state_dir, "latency.jsonl"), provider, spec.get("model"), size)
    return float(soft) if isinstance(soft, (int, float)) and soft > 0 else None


def long_watch_calls(cfg, now=None):
    """Watch calls whose runner is ALIVE and which have run longer than LONG_WATCH_FACTOR x their soft deadline
    (D58). -> [{seat, call, run_id, tag, age_s, soft_s, runner_pid}], oldest first. Read-only: it takes no lock,
    changes no file and sends no signal. A record with no live process under its tag has ended (its runner is about
    to unregister it) and is not listed; a record whose runner is gone is the orphan sweep's."""
    now = time.time() if now is None else now
    state_dir = cfg.get("state_dir") or os.path.expanduser("~/.claude-council")
    wdir = council_run.watch_dir(state_dir)
    try:
        names = sorted(os.listdir(wdir))
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".json") or name.endswith(".end.json"):
            continue
        rec = _read_json(os.path.join(wdir, name))
        if rec is None or rec.get("tag") != name[:-5]:
            continue
        pid = rec.get("runner_pid")
        if not (isinstance(pid, int) and council_run.proc_alive(pid, rec.get("runner_start"))):
            continue
        started = rec.get("started_epoch")
        if not isinstance(started, (int, float)) or isinstance(started, bool):
            continue
        soft = _soft_deadline(cfg, rec)
        age = max(0.0, now - float(started))
        if soft is None or age <= LONG_WATCH_FACTOR * soft:
            continue
        if not [p for p in netwatch.tagged_pids(rec["tag"]) if p != os.getpid()]:
            continue
        out.append({"seat": rec.get("seat"), "call": rec.get("call"), "run_id": rec.get("run_id"),
                    "tag": rec["tag"], "age_s": age, "soft_s": soft, "runner_pid": pid})
    out.sort(key=lambda o: (-o["age_s"], str(o["call"]), o["tag"]))
    return out


def long_watch_line(o):
    return ("seat %s call %s run %s age %s, over %gx its soft deadline (%s): still running under a live runner; "
            "nothing is killed. To end it: kill -TERM %s (the runner)"
            % (o["seat"], o["call"], o["run_id"], _age(o["age_s"]), LONG_WATCH_FACTOR, _age(o["soft_s"]),
               o["runner_pid"]))


def long_watch_rows(cfg):
    """One WARN per long-running watch call (never a FAIL); none when there are none."""
    try:
        calls = long_watch_calls(cfg)
    except Exception as e:                       # a report must never fail a preflight
        return [Check("watch_age", "free", "long_watch", "WARN", "long-watch check raised %s" % type(e).__name__)]
    return [Check("watch_age", "free", "long_watch:%s.%s" % (o["seat"], o["call"]), "WARN", long_watch_line(o))
            for o in calls]


# ---------------------------------------------------------------- drift (WARN only, never blocking)
def _drift_collect(cfg):
    """-> snapshot with items, None when no real provider is seated, or {"error": name} if it raised."""
    try:
        snap = drift.collect(cfg)
    except Exception as e:
        return {"error": type(e).__name__}
    return snap if snap.get("items") or snap.get("missing") else None


STICKY_HINT = " [raised until `council.py drift --accept`]"


def drift_rows(snap, old, cfg=None):
    """One WARN per actionable changed item (drift.split_changes), plus one PASS row counting the informational
    ones; else one PASS summary row. Provider "drift": never a FAIL. The baseline does not
    move on a preflight, so the same WARN repeats until it is accepted (sticky alarms, D19)."""
    if not snap:
        return []
    if snap.get("error"):
        return [Check("drift", "free", "drift", "WARN", "drift check raised %s (not recorded)" % snap["error"])]
    n = len(snap.get("items") or {})
    if not old:
        return [Check("drift", "free", "drift", "PASS",
                      "first pass: %d items, baseline recorded on PASS (`council.py drift` shows them)" % n)]
    try:
        changes = drift.diff(old, snap)
    except Exception as e:
        return [Check("drift", "free", "drift", "WARN", "drift diff raised %s" % type(e).__name__)]
    since = "%s (%s)" % (old.get("recorded_at") or "?", old.get("trigger") or "?")
    if not changes:
        return [Check("drift", "free", "drift", "PASS", "no change in %d items since %s" % (n, since))]
    warn, info = drift.split_changes(changes, bool((cfg or {}).get("_roster_overlay")))
    rows = [Check("drift", "free", "drift:%s" % c["key"], "WARN", drift.change_line(c) + STICKY_HINT) for c in warn]
    if info:
        eps = sum(1 for c in info if c["key"].startswith(drift.ENDPOINT_PREFIX))
        rows.append(Check("drift", "free", "drift:info", "PASS",
                          "%d informational change(s) not raised since %s: %d OpenRouter endpoint, %d roster/overlay "
                          "(`council.py drift` lists them; `--accept` clears them)" % (len(info), since, eps, len(info) - eps)))
    return rows


def _drift_observe(cfg, snap, old, trigger):
    """A passing preflight appends a history observation and seeds an absent baseline. It never moves an existing
    one: an alarm stays raised until `council.py drift --accept` (D19)."""
    if not snap or snap.get("error"):
        return
    try:
        drift.observe(cfg, snap, trigger, old)
    except Exception:           # a failed snapshot write never fails the preflight
        pass


# ---------------------------------------------------------------- output
def format_table(checks, allow_missing=()):
    allowed = set(allow_missing or ())
    rows = [("provider", "stage", "check", "result", "detail")]
    for c in checks:
        res = c.result + (" (allowed missing)" if c.result == "FAIL" and c.provider in allowed else "")
        rows.append((c.provider, c.stage, c.check, res, _scrub(c.detail)))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = []
    for i, r in enumerate(rows):
        lines.append(" | ".join(r[j].ljust(widths[j]) for j in range(4)) + " | " + r[4])
        if i == 0:
            lines.append("-+-".join("-" * w for w in widths) + "-+-" + "-" * 6)
    bad = blocking_fails(checks, allowed)
    if bad:
        lines.append("PREFLIGHT FAIL (%d) at %s" % (len(bad), bad[0].stage))
    else:
        lines.append("PREFLIGHT PASS")
    return "\n".join(lines) + "\n"


SHOWN_PASS = ("depth", "grounded_root")             # PASS rows the chat is told to show (plus every grounded:<seat>)


def format_brief(checks, allow_missing=(), full_path=None):
    """`council.py ask`'s table: every row that is not a plain PASS (FAIL, WARN, SKIP, allowed missing), the PASS rows
    the chat must show (depth, grounded root and seats), and a count of the PASS rows left out. Same verdict line as
    format_table: FAIL rows are never left out."""
    keep = [c for c in checks if c.result != "PASS" or c.check in SHOWN_PASS or c.check.startswith("grounded:")]
    text = format_table(keep, allow_missing)
    hidden = len(checks) - len(keep)
    if hidden:
        text += "%d PASS row(s) not shown%s\n" % (hidden, " (full table: %s)" % full_path if full_path else "")
    return text


def to_json(checks, allow_missing=(), free_only=False):
    allowed = set(allow_missing or ())
    bad = blocking_fails(checks, allowed)
    return json.dumps({
        "ok": not bad, "free_only": bool(free_only), "failed_stage": bad[0].stage if bad else None,
        "allow_missing": sorted(allowed), "failed_providers": sorted(failed_providers(checks)),
        "unavailable_providers": sorted(unavailable_providers(checks)),
        "checks": [{"provider": c.provider, "stage": c.stage, "check": c.check, "result": c.result,
                    "detail": _scrub(c.detail), "allowed_missing": c.result == "FAIL" and c.provider in allowed,
                    **({"usage": c.usage} if c.usage else {}),
                    **({"standin": c.standin} if c.standin else {})}
                   for c in checks]}, indent=1) + "\n"
