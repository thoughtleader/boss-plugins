"""Answer validity (row 2 of run example-run, `[answer_check]`).

A call can end "ok" (exit 0, finish=stop, text present) with something that is not an answer. Run 20261030T090651Z: the
GLM seat was asked to "read it from disk", had no tools (seats run in a temp cwd, prompt on stdin), and returned 924
bytes of `<tool_call>Read<arg_key>file_path</arg_key>...` markup that counted toward quorum. `problem(text, acfg)` names
such a text, and `council_run._classify` gives it the status `invalid_answer` (salvage-eligible) with that reason as its
detail.

Two rules, both measured on the text with fenced and inline code masked out (an answer that QUOTES `<tool_call>` in
code, e.g. a council about this very bug, is never markup):
- tool_call_markup: the text holds tool-call markup (`<tool_call>`, `<arg_key>`, `<|tool_call|>`, DeepSeek
  `<｜tool▁calls▁begin｜>`, `<function_calls>`/`<invoke`, `[TOOL_CALLS]`, `{"name": ..., "arguments": ...}`
  function-call JSON, ...) and what is left after cutting every block out is under `min_prose_chars` of prose. A block
  runs from its opener to its closer; an UNCLOSED one with argument/invoke/JSON structure after it (GLM writes no
  `</tool_call>`) runs to the next opener or the end. A bare mention in prose ("it emitted <tool_call> markup") is not
  a block (see `blocks`).
  The 090651Z text leaves 56 chars ("I'll read the relevant files to evaluate the draft issue"), a real answer that
  ends on one stray call keeps its whole body.
- near_empty: no markup, and the stripped text is under `min_chars`. The shortest real answer seen (the 2+2 smoke run
  20261021T091457Z, qwen: "2 + 2 = 4.\\n\\nConfidence: high") is 28 chars; the default floor is 16.

`enabled` is false when the table is absent (config.toml ships true), the [salvage] convention: hand-built test configs
and canaries (preflight never sets a task's answer_check) are not checked.
"""
from __future__ import annotations

import json
import re

DEFAULT_MIN_CHARS = 16
DEFAULT_MIN_PROSE_CHARS = 200

# (opener, closer or None). Case-insensitive. `｜`/`▁` are DeepSeek's full-width bar and block characters.
_PAIRS = [
    (r"<tool_call>", r"</tool_call>"),
    (r"<tool_calls>", r"</tool_calls>"),
    (r"<tool_use>", r"</tool_use>"),
    (r"<function_calls>", r"</function_calls>"),
    (r"<invoke\b[^>]*>", r"</invoke>"),
    (r"<function=[^>]*>", r"</function>"),
    (r"<\|tool_calls?(?:_section)?(?:_begin)?\|>", r"<\|tool_calls?(?:_section)?_end\|>"),
    (r"<[|｜]tool▁calls?▁begin[|｜]>", r"<[|｜]tool▁calls?▁end[|｜]>"),
    (r"<arg_key>", r"</arg_value>|</arg_key>"),
    (r"<arg_value>", r"</arg_value>"),
    (r"\[TOOL_CALLS\]", None),
    (r"<\|python_tag\|>", r"<\|eom_id\|>|<\|eot_id\|>"),
    # function-call JSON: {"name": "...", "arguments"/"parameters"/"args"/"input": ...} and {"type": "function", ...}
    (r"\{\s*\"(?:name|function|tool|tool_name)\"\s*:\s*\"[^\"\n]{1,80}\"\s*,\s*\"(?:arguments|parameters|args|input)\"\s*:",
     "json"),
    (r"\{\s*\"type\"\s*:\s*\"function\"\s*,", "json"),
    (r"\"tool_calls\"\s*:\s*\[", "json"),
]
_OPENERS = [(re.compile(o, re.I), c if c in (None, "json") else re.compile(c, re.I)) for o, c in _PAIRS]
_INNER = (r"<arg_key>", r"<arg_value>")      # GLM's argument tags live INSIDE a <tool_call>: they never end one
_ANY_OPENER = re.compile("|".join("(?:%s)" % o for o, _ in _PAIRS if o not in _INNER), re.I)
_FENCE = re.compile(r"(^|\n)[ \t]*(```|~~~)[^\n]*\n.*?(\n[ \t]*\2[ \t]*(?=\n|$)|$)", re.S)
_INLINE = re.compile(r"`[^`\n]+`")
_WS = re.compile(r"\s+")


def cfg_of(cfg):
    """[answer_check] with defaults; `enabled` false when the table is absent."""
    ac = (cfg or {}).get("answer_check") or {}
    return {"enabled": bool(ac.get("enabled", False)),
            "min_chars": int(ac.get("min_chars", DEFAULT_MIN_CHARS)),
            "min_prose_chars": int(ac.get("min_prose_chars", DEFAULT_MIN_PROSE_CHARS))}


def problems(cfg):
    """FAIL reasons for a bad [answer_check] table (preflight engine row)."""
    ac = (cfg or {}).get("answer_check")
    if ac is None:
        return []
    if not isinstance(ac, dict):
        return ["[answer_check] must be a table"]
    out = []
    if not isinstance(ac.get("enabled", True), bool):
        out.append("answer_check.enabled must be true or false")
    for k, lo, hi in (("min_chars", 1, 2000), ("min_prose_chars", 1, 20000)):
        v = ac.get(k, lo)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            out.append("answer_check.%s %r must be an integer %d..%d" % (k, v, lo, hi))
    return out


def _mask(text):
    """Same length as text, with fenced blocks and inline code spans blanked (markers in code are quotes)."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    return _INLINE.sub(blank, _FENCE.sub(blank, text))


def _json_end(text, start, op_end):
    """End of the JSON value the opener starts: the object at `{`, or the `[` array of a `"tool_calls":` key. The
    line end when it is not valid JSON (a model's broken call is still a call)."""
    at = start if text[start] == "{" else op_end - 1
    try:
        _, end = json.JSONDecoder().raw_decode(text, at)
        return end
    except ValueError:
        nl = text.find("\n", op_end)
        return len(text) if nl < 0 else nl


# What follows an unclosed opener when it is real markup: argument/parameter tags, an invoke, a tool token, or JSON.
_STRUCT = re.compile(r"<arg_key>|<arg_value>|<parameter[\s>=]|<invoke\b|<[|｜]tool|\{\s*\"|\[\s*\{", re.I)
_BLANK = re.compile(r"\n[ \t]*\n")
_STRUCT_WINDOW = 400
_LINE_CAP = 300


def _structured(m_text, end):
    """True when argument/invoke/JSON structure follows an opener ending at `end`, inside its own paragraph."""
    stop = min(len(m_text), end + _STRUCT_WINDOW)
    b = _BLANK.search(m_text, end, stop)
    if b is not None:
        stop = b.start()
    return _STRUCT.search(m_text, end, stop) is not None


def _line_start(m_text, start):
    nl = m_text.rfind("\n", 0, start)
    return not m_text[nl + 1:start].strip()


def blocks(text):
    """-> [(start, end)] of the tool-call markup blocks in text (code masked out).

    Well-formed blocks always count: opener..closer, function-call JSON, and an unclosed opener with argument/invoke/
    JSON structure after it (GLM writes no `</tool_call>`; it runs to the next opener or the end, as it always did).
    A LOOSE opener (unclosed, no structure) only names a tag: prose such as "it emitted <tool_call> markup" must not
    swallow the rest of the answer. It counts when it starts a line (a markup run: the rest of the line, capped), or,
    keeping its old reach to the next opener or the end, when the text also holds firm markup, i.e. the call is real
    (the 090651Z text: garbled `<tool_call>Read ...` runs between structured ones)."""
    m_text = _mask(text or "")
    cand, pos = [], 0                      # (start, end, kind) kind: firm | loose
    while True:
        best = None
        for op, close in _OPENERS:
            m = op.search(m_text, pos)
            if m and (best is None or m.start() < best[0].start()):
                best = (m, close)
        if best is None:
            break
        m, close = best
        kind = "firm"
        if close == "json":
            end = max(_json_end(m_text, m.start(), m.end()), m.end())
        else:
            nxt = _ANY_OPENER.search(m_text, m.end())
            c = close.search(m_text, m.end()) if close is not None else None
            if c is not None and (nxt is None or c.start() <= nxt.start()):
                end = c.end()
            else:
                end = nxt.start() if nxt is not None else len(m_text)
                if not _structured(m_text, m.end()):
                    kind = "loose"
        cand.append((m.start(), end, kind))
        pos = end
    has_firm = any(k == "firm" for _, _, k in cand)
    out = []
    for a, b, kind in cand:
        if kind == "loose" and not has_firm:
            if not _line_start(m_text, a):
                continue
            nl = m_text.find("\n", a)
            b = min(b, len(m_text) if nl < 0 else nl, a + _LINE_CAP)
        out.append((a, b))
    return out


def prose(text, spans):
    """The text with the spans cut out, whitespace collapsed."""
    parts, pos = [], 0
    for a, b in spans:
        parts.append(text[pos:a])
        pos = b
    parts.append(text[pos:])
    return _WS.sub(" ", " ".join(parts)).strip()


def problem(text, acfg):
    """None for an answer; else the reason (`tool_call_markup: ...` / `near_empty: ...`), the call's detail."""
    if not acfg or not acfg.get("enabled"):
        return None
    text = text or ""
    spans = blocks(text)
    if spans:
        left = prose(text, spans)
        if len(left) < acfg.get("min_prose_chars", DEFAULT_MIN_PROSE_CHARS):
            return "tool_call_markup: %d block%s, %d chars of prose outside them" % (
                len(spans), "" if len(spans) == 1 else "s", len(left))
        return None
    n = len(_WS.sub(" ", text).strip())
    if n < acfg.get("min_chars", DEFAULT_MIN_CHARS):
        return "near_empty: %d chars" % n
    return None
