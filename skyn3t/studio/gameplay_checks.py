"""Gameplay specialist checks (roadmap #8) — deterministic, conservative, advisory.

These are static reviews of a generated game's pure sim that catch a class of defect
the runtime headless gate (NaN/pool-leak/determinism/pause/game-over) does not. They
feed the existing fix-loop as GUIDANCE and never hard-block a build — so a (very
unlikely) false positive can at worst cost a bounded repair pass, never a false
``no_go``. Each check is conservative and never raises.

First check: **input-wiring**. A game whose ``step()`` never READS its input
parameter is uncontrollable — a real cheap-model defect. We look at reads off the
input PARAM specifically (not bare words anywhere in the file): that way bounds/
geometry code that mentions ``left``/``right`` off *state* doesn't mask a missing
input read, and an off-contract-but-real read like ``input.moveLeft`` still counts as
wired. When ``step`` can't be confidently located/parsed we degrade open (no gap), so
a controllable game is never false-flagged.
"""

from __future__ import annotations

import re
from pathlib import Path

# Match a `step` definition and capture its parameter list. Covers
# `function step(...)`, `step(...) {` (method), `step = (...) =>`, and
# `step = function(...)`. `[^()]*` keeps it to a flat param list (good enough; a
# param list with nested parens is rare and just degrades open).
_STEP_SIG = re.compile(r"\bstep\b\s*(?:=\s*)?(?:function\b\s*)?\(([^()]*)\)")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _split_top_level(params: str) -> list[str]:
    """Split a param list on top-level commas (so a destructured ``{a, b}`` param
    stays intact)."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in params:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return [p.strip() for p in out]


def _body_after(src: str, start: int) -> str:
    """Return the brace-matched function body starting at/after ``start``."""
    i = src.find("{", start)
    if i == -1:
        return ""
    depth = 0
    out: list[str] = []
    for ch in src[i:]:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        out.append(ch)
        if depth == 0:
            break
    return "".join(out)


def _step_reads_input(src: str) -> bool | None:
    """True if ``step``'s second (input) parameter is read in its body, False if it
    plainly isn't (or there's no input param), None if ``step`` can't be parsed
    (degrade open)."""
    m = _STEP_SIG.search(src)
    if not m:
        return None
    params = _split_top_level(m.group(1))
    if len(params) < 2:
        return False  # no input parameter at all -> uncontrollable
    p = params[1]
    if p.startswith("{"):
        return True  # destructures fields straight off input -> wired
    name = _IDENT.match(p)
    if not name:
        return None  # an odd param shape -> don't guess
    body = _body_after(src, m.end())
    if not body:
        return None
    # Wired iff the body references the input param by name (any read: input.foo,
    # input[k], `const {x} = input`, etc.) — field NAMES don't matter, only that
    # input is consulted at all.
    return bool(re.search(r"\b" + re.escape(name.group(0)) + r"\b", body))


def check_input_wiring(project_dir: str | Path) -> str | None:
    """Return a fix-loop gap string if the pure sim's ``step()`` never reads its
    input parameter (an uncontrollable game), else ``None``. Never raises; a missing/
    unreadable/unparseable sim returns ``None`` (degrade open — never false-flag)."""
    try:
        from skyn3t.studio.headless_gate import _find_sim

        sim = _find_sim(Path(project_dir))
        if sim is None:
            return None
        src = sim.read_text(encoding="utf-8", errors="ignore")
        reads_input = _step_reads_input(src) if src else None
    except Exception:  # noqa: BLE001 - a checker must never break a build
        return None
    if reads_input is None or reads_input:
        return None
    return (
        "the game IGNORES player input — its pure sim step(state, input, dt) never "
        "reads the input parameter, so the game is uncontrollable. Read the input "
        "controls (input.left/right/up/down/action — all booleans) in step() and "
        "move/act on them."
    )
