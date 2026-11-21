"""Grounded mode (run example-run, row 13): read-only repo and image access for the seats
that can use tools, and a check of the `path:line` citations and quoted snippets their answers carry.

Off by default (`council.py run --grounded [PATH] [--attach IMAGE]...`). Per call, never per run: the runner puts a
`_grounded` key ({root, images, restricted}) on the seat dict of a tool-capable seat's original (and hedge) call, and
the adapter's `build()` turns it into read-only tool argv. The chair, the preflight canaries, the salvage call and
every API-only seat (OpenRouter) never get the key, so they stay text-only. A seat is tool-capable when its adapter
says `grounded_capable`: claude (Read, Grep, Glob only) and codex (`-s read-only`); agy is not (see its adapter).

The check (`verify`) never changes a seat's status and never kills anything: it only reports. A citation is
`rel/path:LINE` or `rel/path:A-B` (also `path#L12`); a quote is a backtick span of 8 or more non-space characters on
the same line as the citation. It is bad when the file does not exist under the root (symlink and `..` escapes
included), the line is past the end of the file, or a quote is not in the file (whitespace-insensitive, `...` splits a
quote into fragments that must each be present). A quote found in the file but outside the +-`line_window` lines
around the citation is accepted and counted in `n_line_off`. Binary and oversized files are `n_unchecked`, not bad.
A bare `:NNN` ref takes the path of the last full citation (see `parse`). `[grounded] probes` lets listed providers' seats
run read-only probes (see `prompt_vars`, `probes_on`).
"""
from __future__ import annotations

import os
import re
import subprocess

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
DEFAULTS = {"enabled": True, "default_root": "repo", "line_window": 6, "max_file_bytes": 2000000,
            "max_images": 8, "max_image_bytes": 15000000, "max_bad_listed": 8, "restricted": True,
            "codex": True, "probes": ["codex"]}
PROBE_PROVIDERS = ("codex", "claude")     # adapters with `probes_capable`; [grounded] probes lists the ones that use it
MIN_QUOTE = 8                      # non-space characters: shorter backtick spans are identifiers, not quotes
MIN_FRAGMENT = 6
_TLDS = {"com", "org", "net", "io", "dev", "app", "co", "ai", "edu", "gov", "local", "me", "so", "sh"}


# ---------------------------------------------------------------- config
def cfg_of(cfg):
    """[grounded] with defaults."""
    g = dict(DEFAULTS)
    g.update({k: v for k, v in ((cfg or {}).get("grounded") or {}).items() if k in DEFAULTS})
    return g


def problems(cfg):
    """FAIL reasons for a bad [grounded] table (preflight-style)."""
    t = (cfg or {}).get("grounded")
    if t is None:
        return []
    if not isinstance(t, dict):
        return ["[grounded] must be a table"]
    out = []
    for k, v in t.items():
        if k not in DEFAULTS:
            out.append("grounded.%s: unknown key" % k)
        elif isinstance(DEFAULTS[k], bool):
            if not isinstance(v, bool):
                out.append("grounded.%s must be true or false" % k)
        elif isinstance(DEFAULTS[k], int):
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                out.append("grounded.%s must be a whole number >= 0" % k)
        elif k == "default_root" and v not in ("repo", "cwd"):
            out.append("grounded.default_root must be \"repo\" or \"cwd\"")
        elif k == "probes":
            if not isinstance(v, list) or any(x not in PROBE_PROVIDERS for x in v):
                out.append("grounded.probes must be a list of provider names from %s ([] = off)" % ", ".join(PROBE_PROVIDERS))
    return out


# ---------------------------------------------------------------- root and images
def resolve_root(path, cfg=None, cwd=None):
    """-> (absolute real root, None) or (None, error). `path` empty = the repo root of `cwd` ([grounded] default_root
    = "repo", else cwd itself). Refuses a missing path, a non-directory, `/` and any ancestor of $HOME (or $HOME)."""
    cwd = cwd or os.getcwd()
    if path:
        root = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    else:
        root = os.path.realpath(cwd)
        if cfg_of(cfg)["default_root"] == "repo":
            try:
                top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                                     text=True, timeout=10).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                top = ""
            if top and os.path.isdir(top):
                root = os.path.realpath(top)
    if not os.path.isdir(root):
        return None, "--grounded: %s is not a directory" % root
    home = os.path.realpath(os.path.expanduser("~"))
    if root == os.sep or root == home or home.startswith(root.rstrip(os.sep) + os.sep):
        return None, "--grounded: refusing root %s (too broad: name the repo directory)" % root
    return root, None


def check_images(paths, cfg=None):
    """-> (absolute real paths, None) or (None, error): each exists, is a regular image file within the size cap."""
    g = cfg_of(cfg)
    out = []
    if len(paths or ()) > g["max_images"]:
        return None, "--attach: %d images (max %d)" % (len(paths), g["max_images"])
    for p in paths or ():
        real = os.path.realpath(os.path.abspath(os.path.expanduser(p)))
        if not os.path.isfile(real):
            return None, "--attach: not a file: %s" % p
        if os.path.splitext(real)[1].lower() not in IMAGE_EXTS:
            return None, "--attach: not an image (%s): %s" % ("/".join(e.lstrip(".") for e in IMAGE_EXTS), p)
        if os.path.getsize(real) > g["max_image_bytes"]:
            return None, "--attach: %s is over %d bytes" % (p, g["max_image_bytes"])
        out.append(real)
    return out, None


def stage_images(paths, dest):
    """Copy the images into `dest` (a dir the runner owns, outside the repo) and return the new paths. The seats read
    these copies: a seat is never pointed at the image's own folder, so its siblings stay out of reach."""
    import shutil
    os.makedirs(dest, mode=0o700, exist_ok=True)
    out = []
    for i, p in enumerate(paths or (), 1):
        name = "%02d-%s" % (i, re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(p)))
        target = os.path.join(dest, name)
        shutil.copyfile(p, target)
        os.chmod(target, 0o400)
        out.append(target)
    return out


def seat_support(cfg, seat, adapter):
    """-> (True, "") when `seat` gets tools in a grounded run, else (False, reason). The seat's `grounded = false` and
    the provider table's `grounded = false` opt a capable seat out."""
    if seat.get("grounded") is False:
        return False, "seat grounded = false"
    pc = ((cfg or {}).get("providers") or {}).get(seat.get("provider")) or {}
    if pc.get("grounded") is False:
        return False, "providers.%s grounded = false" % seat.get("provider")
    if adapter is None:
        return False, "no adapter"
    if seat.get("provider") == "codex" and not cfg_of(cfg)["codex"]:
        return False, "grounded.codex = false (codex's read-only sandbox does not confine reads to the root)"
    if getattr(adapter, "grounded_capable", False):
        return True, ""
    return False, getattr(adapter, "grounded_reason", "") or "text-only provider (no file tools)"


def probes_on(cfg, seat, adapter):
    """True when this (grounded) seat may run read-only probes: the adapter supports them (`probes_capable`) and the
    seat's own `probes = true|false` says so, else its provider is in `[grounded] probes` (default ["codex"])."""
    if not getattr(adapter, "probes_capable", False):
        return False
    if isinstance(seat.get("probes"), bool):
        return seat["probes"]
    return seat.get("provider") in (cfg_of(cfg)["probes"] or ())


def seat_grounding(cfg, seat, adapter, state):
    """The per-seat grounding record for a grounded run: stamps `seat["_grounded"]` on a capable seat (the runner's
    launched copy) and returns {"mode": "grounded"|"ungrounded", "reason", "root"}. `state` = cfg["_grounded"]."""
    ok, why = seat_support(cfg, seat, adapter)
    if ok:
        seat["_grounded"] = {"root": state["root"], "images": list(state.get("images") or ()),
                             "restricted": bool(cfg_of(cfg)["restricted"])}
        rec = {"mode": "grounded", "root": state["root"]}
        if probes_on(cfg, seat, adapter):
            seat["_grounded"]["probes"] = True
            rec["probes"] = True
        return rec
    return {"mode": "ungrounded", "reason": why, "root": state["root"]}


NO_PROBE_LINE = ("You cannot change anything and have no network; do not try to run, edit, install or fetch. "
                 "Use only file reading and searching (Read, Grep, Glob, or read-only listing and viewing commands).")
_PROBE_TAIL = (" Report each probe on its own line as `Probe: <exact command> -> <what it printed, shortened>`, with no "
               "path:LINE citation on that line: a probe is evidence for a claim (say what it confirmed or refuted), "
               "not a replacement for reading and citing the source.")
PROBE_LINES = {
    "codex": ("You cannot change anything and have no network; do not try to edit, install or fetch. Besides reading "
              "and searching files, you may run read-only commands and short scripts to confirm a claim about "
              "behaviour or state (for example `node -e '...'`, `python3 -c '...'`, `git log`, `git show`, `rg`). The "
              "sandbox is read-only: pass the code inline instead of creating a script file, and expect any write to "
              "fail." + _PROBE_TAIL),
    "claude": ("You cannot change anything and have no network; do not try to edit, install or fetch. Besides Read, "
               "Grep and Glob, you may run these read-only shell commands to confirm a claim: git log, git show, git "
               "diff, git blame, git ls-files, wc and ls. Any other shell command is refused." + _PROBE_TAIL),
}


def prompt_vars(state):
    """Substitutions for prompts/advisor-grounded.md and critique-grounded.md. `state["probes"]` (set per seat by
    `with_probes`) = a provider name from PROBE_PROVIDERS whose seat may run read-only probes."""
    imgs = [p for p in (state.get("images") or ())]
    if imgs:
        line = ("Attached images (open each with your file reader; they are copies, outside the repository):\n"
                + "\n".join("- %s" % p for p in imgs) + "\n\n")
    else:
        line = ""
    return {"root": state["root"], "images_line": line,
            "access_line": (PROBE_LINES.get(state["probes"]) or PROBE_LINES["codex"]) if state.get("probes")
            else NO_PROBE_LINE}


def with_probes(state, rec, seat):
    """The run's grounded state for one seat's prompt: adds `probes` (the seat's provider) when its grounding record
    `rec` says it may probe."""
    if state and rec and rec.get("probes"):
        return dict(state, probes=seat.get("provider"))
    return state


# ---------------------------------------------------------------- citation check
_CITE = re.compile(r"(?<![\w/.@:~-])((?:~/|\.{1,2}/|/)?(?:[A-Za-z0-9_.@+~-]+/)*[A-Za-z0-9_@+~-][A-Za-z0-9_.@+~-]*"
                   r"\.[A-Za-z][A-Za-z0-9]{0,7})(?::(\d+)(?:\s*[-–]\s*(\d+))?|#L(\d+)(?:\s*-\s*L?(\d+))?)")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_TICKS = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
# a backtick span that is only a file name or path (`Widget.tsx`, `lib/x.py`), no line ref: names a file, quotes nothing
_FILE_NAME = re.compile(r"^(?:~/|\.{1,2}/|/)?(?:[A-Za-z0-9_.@+~-]+/)*[A-Za-z0-9_@+~-][A-Za-z0-9_.@+~-]*"
                        r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
_ELLIPSIS = re.compile(r"\.\.\.|…")
_SPACE = re.compile(r"\s+")


def _squash(s):
    return _SPACE.sub("", s)


# A bare line ref (`:1006`, `:940-951`): shorthand for the path of the most recent full citation before it.
_BARE = re.compile(r"(?<![\w/.:#~@-]):(\d+)(?:\s*[-–]\s*(\d+))?(?![\w:])")
_BARE_ONLY = re.compile(r"^(?::\d+(?:\s*[-–]\s*\d+)?(?:\s*[,/&;]\s*|\s+(?:and|or)\s+|\s*)?)+$")
_ADJACENT = re.compile(r"^[\s:,;()\[\]—–*-]{0,4}$")
# words right before a `:NNN` that make it a port, not a line (`dev server on :4000`, `localhost:3000`, `(port :9333)`)
_PORT_CTX = re.compile(r"\b(?:ports?|localhost|hosts?|servers?|dev|listen(?:s|ed|ing)?|bound|binds?|canary|chrome)\W{0,4}(?:on\W{0,3})?$",
                       re.I)
_BREAK = re.compile(r"[.!?;](?=\s|$)")
# A probe report line (`Probe: <command> -> <output>`, see prompt_vars): it quotes commands and their output, not the
# source, so it is skipped by the check.
_PROBE_LINE = re.compile(r"^[\s>*_\-\d.)#]*Probe\b[^:\n]{0,30}:", re.I)


def _overlaps(spans, s, e):
    return any(s < b and a < e for a, b in spans)


def parse(text):
    """-> [{path, line, end, quotes: [str], bare: bool, alts: [path]}] in order, deduplicated by (path, line, end);
    quotes merged.

    A bare ref (`:1006` or `:940-951`, not part of a longer token) takes the path of the most recent full citation
    before it (none yet: ignored); `bare` is True for those and `alts` lists the other paths cited earlier in the
    answer, most recent first, which `verify` tries when the inherited one does not fit (a seat often moves on to
    another file without naming it again).

    A quote (backtick span of MIN_QUOTE+ non-space characters) belongs to a citation of its own sentence/clause
    (breaks: `. ! ? ;` before whitespace): the nearest citation before it, else the nearest one after it. A quote
    in a clause with no citation falls back to the nearest citation before it on the line, else after it, unless the
    span is only a file name or path (`Widget.tsx`): that names a file, it quotes no line of the cited one."""
    from bisect import bisect_right
    cites = {}
    order = []
    last_path = None
    seen = []                                       # distinct cited paths, most recent last
    for raw in str(text or "").split("\n"):
        if _PROBE_LINE.match(raw):
            continue
        line = _URL.sub(lambda m: " " * len(m.group(0)), raw)
        ticks = [(tm.start(), tm.end(), tm.group(2).strip()) for tm in _TICKS.finditer(line)]
        full = []
        for m in _CITE.finditer(line):
            path = m.group(1)
            if "/" not in path and path.rsplit(".", 1)[-1].lower() in _TLDS:
                continue
            ln = int(m.group(2) or m.group(4))
            en = m.group(3) or m.group(5)
            full.append((m.start(), m.end(), path, ln, int(en) if en else None, False))
        full_spans = [(f[0], f[1]) for f in full]
        bare = []
        for m in _BARE.finditer(line):
            if _overlaps(full_spans, m.start(), m.end()) or _PORT_CTX.search(line[max(0, m.start() - 24):m.start()]):
                continue
            tk = next((t for t in ticks if t[0] <= m.start() and m.end() <= t[1]), None)
            if tk is not None and not _BARE_ONLY.match(tk[2]):
                continue                            # inside a longer backtick span: code, not a line ref
            en = m.group(2)
            lo, hi = (tk[0], tk[1]) if tk is not None else (m.start(), m.end())      # the backticks belong to the ref
            bare.append((lo, hi, None, int(m.group(1)), int(en) if en else None, True))
        mine = []                                   # this line's citations in text order: (start, end, key or None)
        for start, end, path, ln, en, is_bare in sorted(full + bare):
            if is_bare:
                if last_path is None:               # nothing to inherit from yet: not counted, but it still marks
                    mine.append((start, end, None))  # which backticks are its own, so they are not given to a later one
                    continue
                path = last_path
            else:
                last_path = path
                if path in seen:
                    seen.remove(path)
                seen.append(path)
            key = (path, ln, en)
            rec = cites.get(key)
            if rec is None:
                alts = [x for x in reversed(seen) if x != path] if is_bare else []
                rec = cites[key] = {"path": path, "line": ln, "end": en, "quotes": [], "bare": is_bare, "alts": alts}
                order.append(key)
            elif rec["bare"] and not is_bare:
                rec["bare"], rec["alts"] = False, []
            mine.append((start, end, key))
        if not mine:
            continue
        masked = list(line)
        for a, b, _t in ticks:
            masked[a:b] = "x" * (b - a)
        for a, b, _k in mine:
            masked[a:b] = "x" * (b - a)
        breaks = [m.end() for m in _BREAK.finditer("".join(masked))]
        seg = lambda pos: bisect_right(breaks, pos)
        for a, b, span in ticks:
            if len(_squash(span)) < MIN_QUOTE or _CITE.search(span) or _BARE_ONLY.match(span):
                continue                            # an identifier, or the backticks wrap a citation itself
            before = [c for c in mine if c[1] <= a]
            after = [c for c in mine if c[0] >= b]
            here = [c for c in mine if seg(c[0]) == seg(a)]
            ranked = [c for c in reversed(before) if c in here][:1] + [c for c in after if c in here][:1]
            if not _FILE_NAME.match(span):          # a file name in another clause is not given to a citation
                ranked += before[-1:] + after[:1]
            # a bare ref only owns the quote right beside it (`:940` `if (x) return;`): its shorthand gives no other
            # sign of which backticks are meant, and a seat's proposed new code often sits in the same sentence
            owner = next((c for c in ranked if c[2] is None or not cites[c[2]]["bare"]
                          or _ADJACENT.match(line[c[1]:a] if c[1] <= a else line[b:c[0]])), None)
            if owner is None or owner[2] is None:
                continue
            q = cites[owner[2]]["quotes"]
            if span not in q:
                q.append(span)
    return [cites[k] for k in order]


class _Files:
    """Per-check cache: resolved path -> (lines, squashed text) or a reason it cannot be checked."""

    def __init__(self, root, g):
        self.root, self.g = os.path.realpath(root), g
        self._by_name = None
        self._cache = {}

    def _index(self):
        if self._by_name is None:
            self._by_name = {}
            n = 0
            for d, dirs, files in os.walk(self.root):
                dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__")]
                for f in files:
                    self._by_name.setdefault(f, []).append(os.path.join(d, f))
                    n += 1
                if n > 60000:
                    break
        return self._by_name

    def inside(self, real):
        return real == self.root or real.startswith(self.root.rstrip(os.sep) + os.sep)

    def candidates(self, path):
        """-> (list of real paths inside the root, reason or None)."""
        if path.startswith("~/"):
            path = os.path.expanduser(path)
        if os.path.isabs(path):
            real = os.path.realpath(path)
            return ([real], None) if self.inside(real) else ([], "outside the root")
        real = os.path.realpath(os.path.join(self.root, path))
        if not self.inside(real):
            return [], "outside the root"
        if os.path.isfile(real):
            return [real], None
        if "/" not in path:                         # a bare file name: find it in the repo
            hits = [os.path.realpath(p) for p in self._index().get(path, ())]
            hits = [h for h in hits if self.inside(h)]
            if hits:
                return hits[:20], None
        return [], "no such file"

    def load(self, real):
        if real in self._cache:
            return self._cache[real]
        try:
            if os.path.getsize(real) > self.g["max_file_bytes"]:
                out = ("unchecked", "file over %d bytes" % self.g["max_file_bytes"])
            else:
                with open(real, "rb") as f:
                    data = f.read()
                if b"\0" in data[:8192]:
                    out = ("unchecked", "binary file")
                else:
                    text = data.decode("utf-8", "replace")
                    lines = text.split("\n")
                    out = ("ok", lines, _squash(text))
        except OSError:
            out = ("missing", "unreadable")
        self._cache[real] = out
        return out


def _quote_ok(quote, loaded, line, end, window):
    """-> 'ok' | 'off' (present in the file, outside the window) | 'bad'."""
    lines, whole = loaded[1], loaded[2]
    frags = [f for f in (_squash(x) for x in _ELLIPSIS.split(quote)) if len(f) >= MIN_FRAGMENT] or [_squash(quote)]
    lo, hi = max(0, line - 1 - window), min(len(lines), (end or line) + window)
    near = _squash("\n".join(lines[lo:hi]))
    state = "ok"
    for f in frags:
        if f in near:
            continue
        if f in whole:
            state = "off"
            continue
        return "bad"
    return state


def _judge(files, g, c, path):
    """One citation against one path -> ("ok" | "off" | "unchecked" | "bad", reason)."""
    cands, why = files.candidates(path)
    if not cands:
        return "bad", why or "no such file"
    verdicts = []                                     # per candidate file: (state, reason)
    for real in cands:
        loaded = files.load(real)
        if loaded[0] == "unchecked":
            verdicts.append(("unchecked", loaded[1]))
            continue
        if loaded[0] != "ok":
            verdicts.append(("bad", loaded[1]))
            continue
        top = max(c["line"], c["end"] or 0)
        n_lines = len(loaded[1]) - (1 if loaded[1] and loaded[1][-1] == "" else 0)
        if c["line"] < 1 or top > n_lines:
            verdicts.append(("bad", "line %d past the end (%d lines)" % (top, n_lines)))
            continue
        state, reason = "ok", ""
        for q in c["quotes"]:
            got = _quote_ok(q, loaded, c["line"], c["end"], g["line_window"])
            if got == "bad":
                state = "bad"
                reason = "quote not in file: %s" % (q if len(q) <= 60 else q[:57] + "...")
                break
            if got == "off":
                state = "off" if state == "ok" else state
        verdicts.append((state, reason))
    states = [v[0] for v in verdicts]
    if "ok" in states:
        return "ok", ""
    if "off" in states:
        return "off", ""
    if "unchecked" in states:
        return "unchecked", next(v[1] for v in verdicts if v[0] == "unchecked")
    return "bad", verdicts[0][1]


_RANK = {"ok": 0, "off": 1, "unchecked": 2, "bad": 3}


def verify(text, root, cfg=None):
    """The quote check over one answer. -> {"n_citations", "n_quotes", "n_bad", "n_line_off", "n_unchecked",
    "grounded_ok" (True/False, None when the answer cites nothing), "bad": [{"cite", "reason"}] (capped)}.
    A bare ref (`:1006`) is judged against the path it inherits first, then against the other paths the answer cited
    earlier; it is bad only when none of them fits."""
    g = cfg_of(cfg)
    files = _Files(root, g)
    res = {"n_citations": 0, "n_quotes": 0, "n_bad": 0, "n_line_off": 0, "n_unchecked": 0, "bad": []}
    for c in parse(text):
        res["n_citations"] += 1
        res["n_quotes"] += len(c["quotes"])
        cite = "%s:%d%s" % (c["path"], c["line"], "-%d" % c["end"] if c["end"] else "")
        paths = [c["path"]] + (list(c.get("alts") or ()) if c.get("bare") else [])
        best = None
        for p in paths:
            got = _judge(files, g, c, p)
            if best is None or _RANK[got[0]] < _RANK[best[0]]:
                best = got
            if got[0] == "ok":
                break
        state, why = best
        if c.get("bare") and not c["quotes"] and state in ("ok", "off") and _judge(files, g, c, c["path"])[0] not in ("ok", "off"):
            state = "unchecked"                     # a bare ref with no quote claims only a number: it must fit its own path
        if state == "off":
            res["n_line_off"] += 1
        elif state == "unchecked":
            res["n_unchecked"] += 1
        elif state == "bad":
            res["n_bad"] += 1
            if c.get("bare"):
                cite = ":%d%s (bare ref, after %s)" % (c["line"], "-%d" % c["end"] if c["end"] else "", c["path"])
                if len(paths) > 1:
                    why = "%s; also tried %d other cited file(s)" % (why, len(paths) - 1)
            res["bad"].append({"cite": cite, "reason": why})
    res["bad"] = res["bad"][:g["max_bad_listed"]]
    res["grounded_ok"] = None if res["n_citations"] == 0 else res["n_bad"] == 0
    return res


def label(rec):
    """The one-line tag for a seat heading / status detail from a grounding record."""
    if not rec:
        return ""
    if rec.get("mode") == "ungrounded":
        return "ungrounded"
    v = rec.get("check")
    if v is None:
        return "grounded"
    if not v["n_citations"]:
        return "grounded, no citations"
    ok = v["n_citations"] - v["n_bad"]
    if v["n_bad"]:
        return "grounded %d/%d citations verified, %d FAILED" % (ok, v["n_citations"], v["n_bad"])
    return "grounded %d/%d citations verified" % (ok, v["n_citations"])


def chair_note(rec):
    """Extra heading text for the chair: names the failed citations so it does not rely on them."""
    v = (rec or {}).get("check")
    if not v or not v.get("n_bad"):
        return ""
    return " [unverified: %s]" % "; ".join("%s (%s)" % (b["cite"], b["reason"]) for b in v["bad"])
