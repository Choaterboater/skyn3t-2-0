# skyn3t/agents/validate.py
"""Edit-time source validation. Advisory: a missing toolchain soft-skips
(returns ok) so generation is never blocked. Never raises."""
from __future__ import annotations

import json
import re
from collections import Counter

# Source-code extensions a generated file is expected to be CODE for. A model
# that replies with chat prose instead of code must not ship as one of these.
_CODE_EXTS = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".rb",
    ".java", ".c", ".h", ".cpp", ".cc", ".php", ".vue", ".svelte", ".astro",
    ".swift", ".kt",
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


# Native provider LLM SDKs a generated app must NOT use. SkyN3t routes EVERY LLM
# call through OpenRouter via the OpenAI SDK (base_url https://openrouter.ai/api/v1,
# key OPENROUTER_API_KEY) — the only key the user holds. A model sometimes follows
# a brief that pasted Anthropic/Claude docs and emits `import anthropic` +
# ANTHROPIC_API_KEY; that ships a go-graded app which crashes at run for a key the
# host never set. DELIBERATELY excludes the `openai` SDK — that IS the prescribed
# OpenRouter client, so flagging it would break every compliant LLM app.
_NATIVE_LLM_SDK = re.compile(
    r"^\s*import\s+anthropic\b"            # py: import anthropic[.x]
    r"|^\s*from\s+anthropic\b"            # py: from anthropic import ...
    r"|['\"]@anthropic-ai/sdk['\"]"       # js: import/require '@anthropic-ai/sdk'
    r"|\bnew\s+Anthropic\s*\("            # js: new Anthropic(...)
    r"|\banthropic\.Anthropic\s*\(",      # py: anthropic.Anthropic(...)
    re.MULTILINE,
)
# Native per-provider key the user does NOT hold. Excludes OPENAI_API_KEY: the
# compliant openai client reads OPENROUTER_API_KEY, and flagging OPENAI_API_KEY
# risks false-positives on near-compliant apps (verifier risk note).
#
# Requires an actual READ or .env ASSIGNMENT context so a mere mention in a comment
# or docstring (e.g. "no ANTHROPIC_API_KEY needed") is NOT flagged — only real use.
_NATIVE_KEY_NAMES = r"(?:ANTHROPIC|GEMINI|MISTRAL|COHERE|GROQ)_API_KEY"
_NATIVE_LLM_KEY = re.compile(
    rf"{_NATIVE_KEY_NAMES}\s*=\s*\S"                                  # .env assignment
    rf"|(?:getenv|environ|process\.env|import\.meta\.env)"
    rf"[\s\[\(.'\"]*{_NATIVE_KEY_NAMES}"                              # code read
)


def native_llm_violation(content: str) -> str:
    """Reason string if ``content`` uses a native provider LLM SDK or reads a
    native provider key; '' when compliant.

    Anthropic-scoped on purpose: never flags the `openai` SDK or OPENROUTER_API_KEY
    (the prescribed OpenRouter client). Used both at codegen-time (validate_source)
    and as a delivery-verdict backstop in the runner."""
    if _NATIVE_LLM_SDK.search(content):
        return "imports a native provider LLM SDK (e.g. anthropic / @anthropic-ai/sdk)"
    m = _NATIVE_LLM_KEY.search(content)
    if m:
        return f"reads {m.group(0)} (a native provider key the user does not hold)"
    return ""


_OPENROUTER_FIX_HINT = (
    " Route every LLM call through OpenRouter instead: use the OpenAI SDK with "
    "base_url='https://openrouter.ai/api/v1' reading OPENROUTER_API_KEY (for Claude "
    "use model id 'anthropic/claude-3.5-haiku'), behind the app's own /api/llm "
    "endpoint. Do NOT import 'anthropic'/'@anthropic-ai/sdk' or read ANTHROPIC_API_KEY."
)


# UI that PROMPTS THE END USER to paste an API key. A delivered app must read its
# LLM key from env/config (the OpenRouter passthrough) — never nag the user, who
# is then BLOCKED from using the app without pasting a secret. This is item 52's
# other half: the seam mandate says LLM calls read a configurable base url + env
# key; a key-in-the-UI defeats that (and can't be proven headlessly).
#
# HIGH PRECISION — a match needs BOTH an input/prompt ELEMENT and api-key WORDING
# TOGETHER, so prose that merely mentions an API key (docs, comments, a `.env`
# assignment, an `os.getenv('OPENAI_API_KEY')` read) is NEVER flagged. Tradeoff
# (documented, accepted): an admin-config settings page that legitimately collects
# a THIRD-PARTY service key via an input WILL be flagged too — a delivered app is
# expected to route keys through env/OpenRouter, not an end-user input, so the
# false-positive risk is deliberately taken in favor of the ban.
#
# Wording: "api key" / "api_key" / "apikey" / "api-key" (covers OPENAI_API_KEY,
# ANTHROPIC_API_KEY, "OpenAI API Key", ...) OR an obvious `sk-...` key placeholder.
# The sk- alternative is deliberately narrow (an ellipsis, `sk-xxx`, or a long
# token-shaped run) so a short CSS class like `sk-btn` inside a tag is NOT flagged.
_KEY_WORDING = r"(?:api[\s_-]?key|sk-(?:\.{3,}|x{3,}|[A-Za-z0-9]{8,}))"
_KEY_PROMPT_PATTERNS = (
    # Streamlit (and st.sidebar) text inputs collecting a key.
    re.compile(rf"st(?:\.sidebar)?\.text_input\([^)]*?{_KEY_WORDING}", re.IGNORECASE | re.DOTALL),
    # An HTML/JSX <input> whose attributes (placeholder/label/name/aria-label) name a key.
    re.compile(rf"<input\b[^>]*?{_KEY_WORDING}", re.IGNORECASE),
    # A JS prompt()/window.prompt() asking for a key.
    re.compile(rf"\bprompt\(\s*[`'\"][^`'\"]*?{_KEY_WORDING}", re.IGNORECASE),
)


def key_prompt_violation(content: str) -> str:
    """Reason string if ``content`` PROMPTS the end user for an API key in the UI
    (an input element/prompt paired with api-key wording); '' when compliant.

    High-precision by construction (element + wording must co-occur), so a mere
    mention of a key — a `.env` line, an ``os.getenv`` read, a doc comment — is
    never flagged. Same (reason-or-empty) shape as :func:`native_llm_violation`."""
    for pat in _KEY_PROMPT_PATTERNS:
        m = pat.search(content)
        if m:
            snippet = " ".join(m.group(0).split())[:80]
            return (
                "prompts the end user for an API key in the UI "
                f"({snippet!r}); a delivered app must read keys from env/config "
                "(the OpenRouter passthrough), never require the user to paste one"
            )
    return ""


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


# Edit-style ELISION placeholders. An agentic coder authoring a brand-new file
# from scratch sometimes behaves as if PATCHING a pre-existing file and elides
# the body with "/* ... unchanged ... */", "// rest of the file is unchanged",
# or "// ... existing code ...". There is no original here, so the delivered file
# is a non-functional STUB (e.g. an exported function with an empty body) that
# ships a go-graded app which crashes at run (build #7: `createState` elided ->
# `state.player` undefined -> the whole game never starts). These idioms never
# appear in complete from-scratch code, so detection is high-precision.
#
# Deliberately anchored: a bare ellipsis or a lone "unchanged"/"existing" is NOT
# flagged (JS rest/spread `...rest`, `{...existing}`, or a comment like "the
# array remains unchanged" are legitimate) — only the ellipsis-PAIRED elision
# idioms and the explicit "rest/remainder of the file/code ... unchanged/omitted"
# / "unchanged from the original" phrases.
_ELISION_MARKERS = re.compile(
    r"\.\.\.\s*(?:unchanged|existing\s+code|rest\s+of|same\s+as\s+before|snipped?|omitted|elided)"
    r"|(?:unchanged|rest\s+of\s+(?:the\s+)?(?:file|code|implementation)|same\s+as\s+before|snipped?|omitted|elided)\s*\.\.\."
    r"|(?:rest|remainder|everything\s+else)\s+of\s+(?:the\s+)?(?:file|code|implementation|function|class|module|method)\b[^.\n]{0,40}?(?:unchanged|the\s+same|omitted|identical|as\s+before|as\s+the\s+original)"
    r"|unchanged\s+from\s+(?:the\s+)?(?:original|previous|earlier\s+version)"
    r"|\.\.\.\s*existing\s+code\s*\.\.\."
    r"|//\s*existing\s+code\s+here\b"
    r"|<!--\s*\.\.\.\s*(?:unchanged|existing|omitted|snip)",
    re.IGNORECASE,
)


def _elision_context(content: str, match: re.Match[str]) -> str:
    start = content.rfind("\n", 0, match.start()) + 1
    end = content.find("\n", match.end())
    return content[start:end if end >= 0 else len(content)]


def elided_code_violation(content: str, *, original: str | None = None) -> str:
    """Reason string if ``content`` elides its body with an edit-style 'unchanged'
    / 'existing code' placeholder (a non-functional stub); '' when complete.

    High-precision: only the ellipsis-paired elision idioms and explicit
    "rest of the file ... unchanged" / "unchanged from the original" phrases
    match, so legitimate complete code (rest/spread operators, incidental
    'unchanged' comments) is never flagged. Existing-project edits may preserve
    markers on unchanged lines; extra occurrences or changed contexts still fail.
    """
    preserved = Counter(
        _elision_context(original, match)
        for match in _ELISION_MARKERS.finditer(original)
    ) if original is not None else Counter()
    for match in _ELISION_MARKERS.finditer(content):
        context = _elision_context(content, match)
        if preserved[context]:
            preserved[context] -= 1
            continue
        return f"elides code with an edit-style placeholder ({match.group(0).strip()!r}); the file is an incomplete stub"
    return ""


def looks_elided(content: str, *, original: str | None = None) -> bool:
    """True when ``content`` is a code file whose body was elided with an
    edit-style 'unchanged'/'existing code' placeholder. Thin bool wrapper over
    :func:`elided_code_violation`."""
    return bool(elided_code_violation(content, original=original))


def validate_source(
    path: str, content: str, *, existing_project: bool = False, original: str = "",
) -> tuple[bool, str]:
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
            ok, err = _balanced(content, jsx_allowed=not p.endswith(".ts"))
            if not ok:
                return ok, err
        elif p.endswith((".html", ".htm")):
            # `.html` is NOT in _CODE_EXTS, so the prose/elision/native guards below
            # never see it — an LLM reply would otherwise ship to an .html target
            # unvalidated. Require a real document root AND a closing </html> (a
            # token-truncated rewrite loses the tail, leaving a half-page that renders
            # a blank/broken app). Case-insensitive; attribute-tolerant.
            low = content.lower()
            document_expected = (
                not existing_project
                or "<html" in original.lower() or "<!doctype" in original.lower()
                or "<html" in low or "<!doctype" in low
            )
            if document_expected:
                if "<html" not in low and "<!doctype" not in low:
                    return False, "HTML file has no <html> root or <!doctype> declaration"
                if "</html" not in low:
                    return False, "HTML file appears truncated (no closing </html>)"
            elif _looks_like_prose(content) or looks_elided(
                content, original=original if existing_project else None
            ):
                return False, "HTML fragment looks like prose or elided code"
        # Generic prose guard for any source-code file: chat prose that happens to
        # pass (or skip) the type-specific check must not ship as source.
        if p.endswith(_CODE_EXTS) and _looks_like_prose(content):
            return False, "content looks like prose, not code"
        # Elision guard: an edit-style "/* ... unchanged ... */" placeholder leaves
        # a non-functional stub (there is no original to be unchanged from) — reject
        # so codegen's retry rewrites the file IN FULL.
        if p.endswith(_CODE_EXTS):
            why = elided_code_violation(
                content, original=original if existing_project else None
            )
            if why:
                if existing_project:
                    return False, (
                        why + ". Write the COMPLETE edited file; do not replace "
                        "implementation with new placeholders."
                    )
                return False, (
                    why + ". Write the COMPLETE file from scratch — implement every "
                    "function and class body in full; there is no pre-existing version."
                )
        # Native-provider-LLM guard: reject `import anthropic` / ANTHROPIC_API_KEY
        # so codegen's retry regenerates the call the compliant OpenRouter way.
        if p.endswith(_CODE_EXTS) and not existing_project:
            why = native_llm_violation(content)
            if why:
                return False, why + "." + _OPENROUTER_FIX_HINT
    except Exception:  # noqa: BLE001 - validation must never raise
        return True, ""
    return True, ""


def _balanced(content: str, *, jsx_allowed: bool = True) -> tuple[bool, str]:
    """Cheap brace/bracket/paren balance check for JS/TS (no toolchain needed).
    Ignores chars inside strings, comments, and JSX child text. JavaScript inside
    JSX expression containers is still checked. This is a best-effort scanner
    that catches gross imbalance, not a replacement for a compiler or parser.
    Known blind spot: regex literals (e.g. /[{]/) — reliably detecting a regex
    needs full JS semantics, so an unbalanced brace inside a regex literal may
    false-positive; acceptable for this best-effort check."""
    pairs = {")": "(", "]": "[", "}": "{"}
    opens = set("([{")
    stack: list[str] = []
    i, n = 0, len(content)
    in_str = ""
    mode = "js"
    jsx_depth = 0
    jsx_returns: list[tuple[str, int]] = []
    expression_returns: list[tuple[str, int]] = []

    def starts_jsx(offset: int) -> bool:
        """Whether ``<`` begins JSX rather than a comparison or TS generic."""
        if not jsx_allowed:
            return False
        if offset + 1 >= n or not (content[offset + 1].isalpha() or content[offset + 1] == ">"):
            return False
        before = content[:offset].rstrip()
        if not before:
            return True
        if before.endswith("return") and (len(before) == 6 or not before[-7].isalnum()):
            return True
        if before[-1] not in "=>([,:{;!?&|":
            return False

        # In TSX, generic arrow functions occupy the same expression positions as
        # JSX. A comma or constraint *immediately after the first parameter name*
        # identifies that syntax. Do not inspect the rest of a possible tag: commas
        # and words such as ``in`` may occur in ordinary JSX attributes.
        tag_end = content.find(">", offset + 1)
        if tag_end >= 0:
            head = content[offset + 1:tag_end]
            if re.match(r"[A-Za-z_$][\w$]*\s*(?:,|extends\b)", head):
                return False
        return True

    while i < n:
        c = content[i]

        if mode == "jsx_text":
            if content.startswith("{/*", i):
                end = content.find("*/}", i + 3)
                if end < 0:
                    return False, "Unclosed JSX comment"
                i = end + 3
                continue
            if content.startswith("</", i):
                mode = "jsx_close_tag"
                i += 2
                continue
            if c == "<" and i + 1 < n and (content[i + 1].isalpha() or content[i + 1] == ">"):
                jsx_returns.append((mode, jsx_depth))
                mode = "jsx_tag"
                i += 1
                continue
            if c == "{":
                stack.append(c)
                expression_returns.append((mode, len(stack) - 1))
                mode = "js_expression"
            elif c == "}":
                return False, f"Unbalanced '}}' at offset {i}"
            i += 1
            continue

        if mode in {"jsx_tag", "jsx_close_tag"}:
            if c == "/" and i + 1 < n and content[i + 1] == "/":
                while i < n and content[i] != "\n":
                    i += 1
                continue
            if c == "/" and i + 1 < n and content[i + 1] == "*":
                i += 2
                while i + 1 < n and not (content[i] == "*" and content[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            if c in "\"'":
                quote = c
                i += 1
                while i < n and content[i] != quote:
                    i += 2 if content[i] == "\\" else 1
                i += 1
                continue
            if c == "{" and mode == "jsx_tag":
                stack.append(c)
                expression_returns.append((mode, len(stack) - 1))
                mode = "js_expression"
                i += 1
                continue
            if c == ">":
                self_closing = mode == "jsx_tag" and content[:i].rstrip().endswith("/")
                if mode == "jsx_close_tag":
                    jsx_depth -= 1
                elif not self_closing:
                    jsx_depth += 1
                return_mode, base_depth = jsx_returns[-1]
                if self_closing or jsx_depth == base_depth:
                    jsx_returns.pop()
                    mode = return_mode
                else:
                    mode = "jsx_text"
                i += 1
                continue
            i += 1
            continue

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
        elif c == "<" and starts_jsx(i):
            jsx_returns.append((mode, jsx_depth))
            mode = "jsx_tag"
        elif c in opens:
            stack.append(c)
        elif c in pairs:
            if not stack or stack[-1] != pairs[c]:
                return False, f"Unbalanced '{c}' at offset {i}"
            stack.pop()
            if c == "}" and expression_returns and len(stack) == expression_returns[-1][1]:
                mode, _ = expression_returns.pop()
        i += 1
    if stack:
        return False, f"Unclosed '{stack[-1]}'"
    return True, ""
