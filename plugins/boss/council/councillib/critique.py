"""Optional critique round (run example-run, row 14): `--depth quick|standard`.

quick (the default) is today's council: independent answers, then the chair. standard adds one round between them:
every seat that ANSWERED in round 1 reads the other answering seats' answers and writes a short critique (agree or
disagree, errors it can show, what changes its view, a revised `Verdict:`). The chair then gets round 1 plus the
critiques and must say which critiques changed the recommendation. Calls: N seats + N critiques + 1 chair = about 2N+1.

Rules this module and the runner (council_run.py) keep:
- Never a vote and never a gate: a critique changes nothing about quorum, status or exit code. A seat that failed
  round 1 writes no critique (and is not shown to the others); a critique that fails, even with no partial, leaves the
  run at its exit code (the one salvage call applies, like round 1; no hedge, fallback or stand-in).
- Peers are shown as `Seat A`, `Seat B`, ... never by seat id, provider or model. The seat ids here are model-family
  names (claude, codex, ...), and a brand on a heading invites deference or a defence of "its own family"; a letter
  makes the critic weigh the argument. One run-wide letter map (roster order, over every launched seat, whether it
  answered or not) is used for every critic and given to the chair, so "Seat B" in a critique resolves to a seat id
  there. A seat's lens, when it has one, is shown next to its letter: it is an assigned angle, not an identity.
- A critic gets its own round-1 answer (labelled as its own) next to the peers': each critique is a fresh call and cannot
  revise a view it cannot see. The critique's `Verdict:` is stored apart (`critique.verdict`); the round 1 verdict is
  never overwritten.
- Grounded runs: the critique seat is a copy of the launched seat, so a tool-capable seat keeps the same read-only access
  and its `path:line` citations in the critique are checked like round 1's.
"""
from __future__ import annotations

import re

DEPTHS = ("quick", "standard")
DEFAULT_DEPTH = "quick"
MAX_PEER_CHARS = 60000           # per answer in a critique prompt: a longer one is cut (kept head + a marker)
MIN_ANSWERS = 2                  # the round needs this many answered seats (a critic needs a peer)


def depth_of(cfg):
    """The run's depth: the per-run `_depth` key (council.py --depth; an underscore key, so drift skips it), else quick."""
    d = (cfg or {}).get("_depth")
    return d if d in DEPTHS else DEFAULT_DEPTH


def estimate_calls(n, depth):
    """Model calls of a run with n seats: n + 1 (quick) or 2n + 1 (standard), before salvage / hedge / fallback calls."""
    n = max(0, int(n))
    return 2 * n + 1 if depth == "standard" else n + 1


def letter(i):
    """0 -> A ... 25 -> Z, 26 -> AA, 27 -> AB ..."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def letters(seat_ids):
    """{seat id: 'A'|'B'|...} in the given (roster) order."""
    return {sid: letter(i) for i, sid in enumerate(seat_ids)}


def clip(text, limit=MAX_PEER_CHARS):
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[... the rest of this answer was cut to fit ...]"


def peers_block(peers, limit=MAX_PEER_CHARS):
    """`peers` = [(letter, lens, text)] -> the prompt block. Letters and lenses only, never a seat id or model."""
    parts = []
    for lt, lens, text in peers:
        head = "### Seat %s%s" % (lt, " (lens: %s)" % lens if lens else "")
        parts.append("%s\n\n%s" % (head, clip(text, limit)))
    return "\n\n".join(parts)


def lens_line(lens):
    """The critique prompt's lens line ('' when the seat has none)."""
    if not lens:
        return ""
    return ("Your lens in round 1 was: %s. Keep weighing from that angle, but judge each of the other answers on its "
            "merits, not only by that angle.\n\n" % lens)


def prompt_vars(letter_, own_text, peers, n_seats, lens=None, limit=MAX_PEER_CHARS):
    return {"letter": letter_, "n_seats": n_seats, "own": clip(own_text, limit), "peers": peers_block(peers, limit),
            "lens_line": lens_line(lens)}


# Last match wins (the line is asked for just above the verdict line). Same tolerance as verdict.parse.
_CHANGED = re.compile(r"^[ \t>*_`#-]*changed[ \t]+(?:my[ \t]+)?view[ \t*_`]*[:=][ \t*_`]*(yes|no)\b", re.I | re.M)


def parse_changed(text):
    """'yes' | 'no' | None: the critique's own `Changed view:` line (display only; a missing line is never a failure)."""
    if not text:
        return None
    found = _CHANGED.findall(str(text))
    return found[-1].lower() if found else None


def verdict_change(before, after):
    """{'from': a, 'to': b} when both verdicts are known and differ, else None."""
    if before and after and before != after:
        return {"from": before, "to": after}
    return None


# ---------------------------------------------------------------- chair input
CHAIR_GUIDE = (
    "\n\n- This was a two-round council. The answers below are round 1. After them come the critiques (round 2): each "
    "advisor that answered then read the other answers as anonymous letters (Seat A, Seat B, ...; the key is given "
    "with the critiques) and wrote a short critique with a revised verdict. A critique is a reviewer's view, never a "
    "vote: weigh it by its evidence. A critique heading shows `round 1 verdict X -> revised verdict Y` and "
    "`changed view yes|no` (the seat's own report that another answer changed its recommendation). A critique marked "
    "SALVAGED is the less considered output of one short follow-up call; one marked TRUNCATED is partial; "
    "\"(no critique)\" means that seat's critique failed or was not asked for: do not guess it. Round 1 answers still "
    "never saw each other.")

CHAIR_ITEM = (
    "\n11. Critique round: which critiques changed the recommended answer, by seat id and what in them did it; which "
    "errors the critiques found in a round 1 answer and whether they hold; and where the recommendation differs from "
    "what round 1 alone would have given. If no critique changed the recommendation, say so plainly.")

# P28 (run example-run, row 24): the chair is told how many seats changed their view, and its
# item 11 opens with one countable line, so a run's synthesis shows whether the round paid for itself. Code, not
# chair.md: the owner's config points `prompts_dir` at another prompts folder, so only `${critique_item}` reaches a run.
CHAIR_COUNT = (
    " Seats that changed their view in round 2 (counted by the runner: a critique saying `Changed view: yes`, or a revised "
    "verdict that differs from that seat's round 1 verdict): %d of %d critiques, %s. Begin this item with the line "
    "`critique_changes: N`, where N is how many critiques changed the recommended answer (0 if none).")


def view_changed(changed_view, verdict_change_):
    """True when one critique changed its seat's view: its own `Changed view: yes` line, or a revised verdict that differs
    from round 1 (`verdict_change` not None). Each seat counts once even when both hold."""
    return changed_view == "yes" or bool(verdict_change_)


def changes_line(changed, n_ok):
    """The count sentence added to item 11: `changed` = seat ids whose view changed, `n_ok` = critiques that came back."""
    return CHAIR_COUNT % (len(changed), n_ok, ("seat " + ", seat ".join(changed)) if changed else "none")


def chair_vars(critiques_text, changes=None):
    """Substitutions for chair.md. quick (None): every one is empty, so the chair prompt is unchanged.
    `changes` = (changed seat ids, number of ok critiques) of a standard run: item 11 also gets the count line."""
    if critiques_text is None:
        return {"critique_guide": "", "critique_item": "", "critiques": ""}
    item = CHAIR_ITEM + (changes_line(*changes) if changes is not None else "")
    return {"critique_guide": CHAIR_GUIDE, "critique_item": item,
            "critiques": "\n\n## Critiques (round 2)\n\n" + critiques_text}
