# skyn3t/agents/validate.py
"""Edit-time source validation. Advisory: a missing toolchain soft-skips
(returns ok) so generation is never blocked. Never raises."""
from __future__ import annotations

import json
import re

# Source-code extensions a generated file is expected to be CODE for. A model
# that replies with chat prose instead of code must not ship as one of these.
_CODE_EXTS = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".rb",
    ".java", ".c", ".h", ".cpp", ".cc", ".php", ".vue", ".svelte", ".swift", ".kt",
)

# Any one of these is strong evidence the content is actually code, not prose:
# block/statement punctuation, JSX/generics angle brackets, an arrow function, or
# a common code keyword. Deliberately excludes bare ``()``/``:`` which appear in
# prose too.
_CODE_SIGNAL = re.compile(
    r"[{};=<>]|=>|\b(function|const|let|var|class|import|export|from|require|"
    r"return|module|exports|async|await|def|print|console|new|public|private|"
    r"void|interface|type|enum|struct|fn|package|func|println|echo|namespace)\b"
)


def _looks_like_prose(content: str) -> bool:
    """True when ``content`` is substantial natural-language prose, not code.

    Heuristic, high-precision: short snippets are never judged (avoids
    false-rejecting tiny valid files), and any code signal at all clears it. Only
    a long body with ZERO code structure is treated as prose — exactly the
    "the model chatted instead of emitting code" failure mode.
    """
    stripped = content.strip()
    if len(stripped) < 60:
        return False
    return _CODE_SIGNAL.search(content) is None


def validate_source(path: str, content: str) -> tuple[bool, str]:
    """Return (ok, error). ok=True when valid OR unvalidatable for this type."""
    p = path.lower()
    try:
        if p.endswith(".py"):
            try:
                compile(content, path, "exec")
            except SyntaxError as exc:
                return False, f"SyntaxError line {exc.lineno}: {exc.msg}"
        elif p.endswith(".json"):
            try:
                json.loads(content)
                return True, ""
            except json.JSONDecodeError as exc:
                return False, f"JSON error line {exc.lineno}: {exc.msg}"
        elif p.endswith(".toml"):
            try:
                import tomllib
                tomllib.loads(content)
                return True, ""
            except Exception as exc:  # noqa: BLE001
                return False, f"TOML error: {exc}"
        elif p.endswith((".js", ".jsx", ".ts", ".tsx")):
            ok, err = _balanced(content)
            if not ok:
                return ok, err
        # Generic prose guard for any source-code file: chat prose that happens to
        # pass (or skip) the type-specific check must not ship as source.
        if p.endswith(_CODE_EXTS) and _looks_like_prose(content):
            return False, "content looks like prose, not code"
    except Exception:  # noqa: BLE001 - validation must never raise
        return True, ""
    return True, ""


def _balanced(content: str) -> tuple[bool, str]:
    """Cheap brace/bracket/paren balance check for JS/TS (no toolchain needed).
    Ignores chars inside strings/line comments/block comments. Best-effort, never
    false-negatives a real syntax error class but only catches gross imbalance.
    Known blind spot: regex literals (e.g. /[{]/) — reliably detecting a regex
    needs full JS semantics, so an unbalanced brace inside a regex literal may
    false-positive; acceptable for this best-effort check."""
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set("([{")
    stack: list[str] = []
    i, n = 0, len(content)
    in_str = ""
    while i < n:
        c = content[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = ""
        elif c in "\"'`":
            in_str = c
        elif c == "/" and i + 1 < n and content[i + 1] == "/":
            while i < n and content[i] != "\n":
                i += 1
            continue
        elif c == "/" and i + 1 < n and content[i + 1] == "*":
            i += 2
            while i + 1 < n and not (content[i] == "*" and content[i + 1] == "/"):
                i += 1
            i += 2  # skip the closing */
            continue
        elif c in opens:
            stack.append(c)
        elif c in pairs:
            if not stack or stack[-1] != pairs[c]:
                return False, f"Unbalanced '{c}' at offset {i}"
            stack.pop()
        i += 1
    if stack:
        return False, f"Unclosed '{stack[-1]}'"
    return True, ""
