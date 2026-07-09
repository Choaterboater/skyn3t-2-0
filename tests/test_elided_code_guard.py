"""Guard against shipping ELIDED edit-style stubs as source code.

An agentic coder authoring a brand-new file sometimes behaves as if PATCHING a
pre-existing file and elides the body with "/* ... unchanged ... */",
"// the rest of the file is unchanged from the original", or
"// ... existing code ...". There is no original here, so the delivered file is a
non-functional STUB. Build #7's `sim.js` shipped exactly this — `createState`
elided to `{ /* ... unchanged ... */ }` returning undefined — so the Phaser game
crashed at run (`this.state.player` of undefined) and only the loading bar showed,
yet it was graded as a deliverable. Three layers now close it: an anti-elision
directive in the codegen prompts, an edit-time `validate_source` reject (triggers
regeneration), and a delivery-time `_clean_agentic_files` reject (revert/drop).
"""

from __future__ import annotations

from skyn3t.agents.validate import (
    elided_code_violation,
    looks_elided,
    validate_source,
)

# The exact idioms build #7's Claude-CLI codegen emitted, plus common siblings.
ELIDED_SAMPLES = [
    "export function createState(seed) { /* ... unchanged ... */ }",
    "// The rest of the file is unchanged from the original\n",
    "function gameLoop() {\n  // ... existing code ...\n}",
    "/* remainder of the file omitted */\n",
    "// unchanged from the previous version\nconst x = 1;",
    "<!-- ... unchanged content here -->",
    "  // ...rest of code unchanged...\n",
    "// existing code here\n",
    "/* code unchanged from original */\n",
    "function step(dt){ /* rest of the implementation unchanged */ }",
]

# Legitimate COMPLETE code that merely mentions 'unchanged'/'rest'/'existing' or
# uses JS rest/spread — must NEVER be flagged.
COMPLETE_SAMPLES = [
    "const unchanged = computeDiff(a, b);\nreturn unchanged;",
    "// the input array remains unchanged by this pure function\nreturn [...arr].sort();",
    "// same as above\nconst x = 1;",
    "const merged = {...existingProps, foo: 1};",
    "const [first, ...rest] = items;\nprocess(rest);",
    "// This sorts the rest of the list in place\nitems.sort();",
    "function f(){ return 'all systems unchanged'; }",
    "this.state = createState(seed);  // keep sim state in sync",
    "// existing users are migrated below\nmigrate(users);",
]


# ---------------------------------------------------------------------------
# elided_code_violation / looks_elided — high-precision detection
# ---------------------------------------------------------------------------

def test_violation_flags_every_elided_idiom():
    for s in ELIDED_SAMPLES:
        why = elided_code_violation(s)
        assert why, f"should flag elided sample: {s!r}"
        assert looks_elided(s) is True


def test_violation_clears_complete_code():
    for s in COMPLETE_SAMPLES:
        assert elided_code_violation(s) == "", f"false positive on: {s!r}"
        assert looks_elided(s) is False


def test_violation_reason_quotes_the_marker():
    why = elided_code_violation("foo() { /* ... unchanged ... */ }")
    assert "stub" in why.lower()
    assert "unchanged" in why.lower()


# ---------------------------------------------------------------------------
# validate_source rejects elided source files, accepts real code
# ---------------------------------------------------------------------------

def test_validate_rejects_elided_js():
    ok, err = validate_source(
        "sim.js", "export function createState(seed) { /* ... unchanged ... */ }"
    )
    assert ok is False
    assert "stub" in err.lower()
    # The fix hint steers regeneration toward a full rewrite.
    assert "complete" in err.lower()


def test_validate_rejects_elided_in_unchecked_language():
    # .go has no syntax checker, but an elided stub must still be caught.
    ok, err = validate_source("game.go", "func Update() { /* rest of the file unchanged */ }")
    assert ok is False and "stub" in err.lower()


def test_validate_accepts_complete_code_with_incidental_words():
    code = (
        "export function createState(seed){\n"
        "  const player = {x: 400, y: 550, hp: 3};\n"
        "  // the seed is left unchanged by this call\n"
        "  return {player, enemies: [], score: 0};\n"
        "}\n"
    )
    assert validate_source("sim.js", code) == (True, "")


def test_validate_accepts_rest_spread():
    assert validate_source("merge.js", "export const m = (a,b)=>({...a, ...b});")[0] is True


# ---------------------------------------------------------------------------
# Agentic path: elided files the CLI wrote are reverted to the scaffold / dropped
# ---------------------------------------------------------------------------

def _agent():
    from skyn3t.agents.code_agent import CodeAgent
    from skyn3t.core.events import EventBus

    return CodeAgent(event_bus=EventBus(), llm=object())


def test_clean_agentic_files_reverts_elided_to_scaffold():
    agent = _agent()
    disk = {
        "src/main.js": "// rest of the file is unchanged from the original\n",  # elided stub
        "src/util.js": "export const add = (a, b) => a + b;\n",                 # real code
        "README.md": "# notes",                                                 # non-code, left alone
    }
    scaffold = {"src/main.js": "import './game.js';\nconsole.log('boot');\n"}
    clean, rejected = agent._clean_agentic_files(disk, scaffold)

    assert "src/main.js" in rejected
    assert clean["src/main.js"] == scaffold["src/main.js"]  # reverted to runnable baseline
    assert clean["src/util.js"] == disk["src/util.js"]      # real code kept
    assert clean["README.md"] == disk["README.md"]          # non-code untouched


def test_clean_agentic_files_drops_elided_without_scaffold():
    agent = _agent()
    disk = {"src/sim.js": "export function createState(seed){ /* ... unchanged ... */ }"}
    clean, rejected = agent._clean_agentic_files(disk, {})
    assert rejected == ["src/sim.js"]
    assert "src/sim.js" not in clean  # no scaffold baseline → dropped, not shipped


def test_clean_agentic_files_keeps_complete_game_code():
    agent = _agent()
    code = (
        "export function createState(seed){\n"
        "  return {player: {x: 1, y: 2}, score: 0};\n"
        "}\n"
    )
    clean, rejected = agent._clean_agentic_files({"src/sim.js": code}, {})
    assert rejected == []
    assert clean["src/sim.js"] == code


# ---------------------------------------------------------------------------
# Prevention: the codegen system prompts forbid elision
# ---------------------------------------------------------------------------

def test_agentic_system_prompt_forbids_elision():
    from skyn3t.adapters.llm import _agentic_system_for

    for stack in ("phaser", "react_vite", "nextjs", "expo", "fastapi", "cli"):
        sys = _agentic_system_for(stack).lower()
        assert "unchanged" in sys, f"missing anti-elision rule for {stack}"
        assert "from scratch" in sys or "no pre-existing" in sys


# ---------------------------------------------------------------------------
# Real build #7 regression: the actual shipped sim.js stub is detected
# ---------------------------------------------------------------------------

def test_real_build7_sim_stub_detected():
    # Preserve the exact regression shape in-repo. A developer's Documents
    # directory is not a portable test fixture and made this guard silently skip
    # on clean machines and CI runners.
    txt = "export function createState(seed) { /* ... unchanged ... */ }\n"
    assert looks_elided(txt) is True
    ok, _ = validate_source("src/sim.js", txt)
    assert ok is False
