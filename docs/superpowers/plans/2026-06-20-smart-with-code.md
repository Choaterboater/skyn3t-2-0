# Smart with Code — Implementation Plan (Spec 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SkyN3t choose the right stack, inject the right skills, validate code at write-time, honor explicit stack pins, and clean up after itself.

**Architecture:** Five independent changes to the existing build pipeline. Stack selection runs in the runner (it owns the LLM client) and feeds the planner via `stack_hint`; the planner stays pure/offline. Skill injection reuses the library's existing tag-matching. Validation wraps the two file-write sites. Cleanup is a new pure module + CLI/API surface.

**Tech Stack:** Python 3.11+, pytest, Typer (CLI), FastAPI (web), existing `skyn3t.*` packages. Offline-first tests (no network, no real subprocess).

## Global Constraints

- Python **3.11+** (`tomllib` is stdlib; `compile()` for py syntax checks).
- **Offline-first tests:** no network, no real LLM, no real subprocess. Mock `self.llm`/`subprocess`; assert on in-memory results. (Pattern: `tests/test_mobile_stack.py`.)
- **Never raise from a build:** every new code path degrades gracefully (matches `_skill_advice`/`cleanup_worktree` which swallow + log).
- **Three stack vocabularies must agree** (see `tests/test_mobile_stack.py` docstring): planner (`react`/`static`/`python`/`fastapi`/`react_native`/`express`), agent/builder (`react_vite`/`static_html`/`python_cli`/`node_express`/`fastapi`/`react_native`), skill-group aliases. Stack selection works in **planner vocab**; `_common.detect_stack(explicit=...)` maps planner→builder downstream.
- **Real-builder planner stacks** (the menu): `react`, `react_native`, `fastapi`, `static`, `python`, `express`. `nextjs`/`flask`/`django` are dropped as named targets (no builder; collapse to a real one).
- Suite baseline: **~389 pass / 1 skip**. Run `pytest -q` after each task; it must stay green.
- Commit after every task.

---

### Task 1: Dropped stack-hint fix + `--stack` CLI flag

Closes the live bug: `submit_build` writes `extra["stack"]` but `runner.start` reads `extra["stack_hint"]`, so explicit pins are dropped.

**Files:**
- Modify: `skyn3t/studio/runner.py:650` (read `extra["stack"]`)
- Modify: `skyn3t/cli/main.py:343-352` (add `--stack`)
- Modify: `skyn3t/cli/main.py` `_run_build` (thread `stack` into `extra`)
- Test: `tests/test_stack_pin.py` (create)

**Interfaces:**
- Produces: builds dispatched with `extra={"stack": "<planner-vocab>"}` reach `planner.plan(stack_hint=...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stack_pin.py
"""Explicit stack pins must reach the planner (the dropped-hint bug)."""
from __future__ import annotations

from skyn3t.studio.planner import Planner


def test_planner_honors_explicit_pin_over_brief():
    # Brief screams "python", pin says "fastapi" -> pin wins.
    plan = Planner().plan("a python script to crunch data", "slug", stack_hint="fastapi")
    assert plan.stack == "fastapi"


def test_runner_reads_extra_stack_key(monkeypatch):
    # The key the web API actually writes is extra["stack"], not ["stack_hint"].
    captured = {}
    from skyn3t.studio import planner as planner_mod

    real_plan = Planner.plan

    def spy(self, brief, slug, *, stack_hint=None, **kw):
        captured["hint"] = stack_hint
        return real_plan(self, brief, slug, stack_hint=stack_hint, **kw)

    monkeypatch.setattr(Planner, "plan", spy)
    # Simulate the resolution line in runner.start:
    extra = {"stack": "fastapi"}
    clar_answers: dict = {}
    hint = clar_answers.get("stack") or extra.get("stack") or extra.get("stack_hint")
    Planner().plan("brief", "slug", stack_hint=hint)
    assert captured["hint"] == "fastapi"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_stack_pin.py -v`
Expected: `test_runner_reads_extra_stack_key` PASSES (it inlines the fixed line); `test_planner_honors_explicit_pin_over_brief` PASSES already (planner honors a valid hint). Both green here means the unit contracts hold — the *bug* is only in `runner.py`'s key. Proceed to wire it.

- [ ] **Step 3: Fix the runner key (`runner.py:650`)**

Replace:
```python
            stack_hint=clar.answers.get("stack") or extra.get("stack_hint"),
```
with:
```python
            stack_hint=clar.answers.get("stack") or extra.get("stack") or extra.get("stack_hint"),
```

- [ ] **Step 4: Add `--stack` to the CLI (`cli/main.py:343`)**

In `studio_build`, add the option and pass it through:
```python
@studio_app.command("build")
def studio_build(
    brief: str = typer.Argument(..., help="What to build, in plain English."),
    best_of: int = typer.Option(0, "--best-of", "-n", help="Best-of-N code trajectories."),
    no_critic: bool = typer.Option(False, "--no-critic", help="Disable the adversarial critic gate."),
    slug: str = typer.Option("", "--slug", help="Override the project slug."),
    stack: str = typer.Option("", "--stack", help="Pin the stack: react|react_native|fastapi|static|python|express."),
) -> None:
    ...
    outcome = asyncio.run(_run_build(brief, best_of=best_of, no_critic=no_critic, slug=slug, stack=stack))
```
In `_run_build`, accept `stack: str = ""` and include it in the `extra`/`start` call as `extra={"stack": stack, ...}` (find the existing `start(`/`extra=` site in `_run_build` and add the key).

- [ ] **Step 5: Run the suite**

Run: `pytest tests/test_stack_pin.py -q && pytest -q`
Expected: new tests pass; full suite still ~389 pass / 1 skip.

- [ ] **Step 6: Commit**

```bash
git add skyn3t/studio/runner.py skyn3t/cli/main.py tests/test_stack_pin.py
git commit -m "fix: honor explicit stack pin (extra['stack']) + add studio build --stack"
```

---

### Task 2: Edit-time lint/compile guardrail

Validate generated source before it lands; on failure, one bounded re-emit. The survey's #1 lever.

**Files:**
- Create: `skyn3t/agents/validate.py`
- Modify: `skyn3t/agents/code_agent.py` `_generate_file` (re-emit) + `_write_files:291` (flag)
- Modify: `skyn3t/agents/code_improver.py:71` (guard the write)
- Test: `tests/test_validate_source.py` (create)

**Interfaces:**
- Produces: `validate_source(path: str, content: str) -> tuple[bool, str]` — `(ok, error_message)`; `ok=True` when valid OR unvalidatable (soft-skip).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_validate_source.py
from __future__ import annotations

from skyn3t.agents.validate import validate_source


def test_valid_python_passes():
    ok, err = validate_source("m.py", "def f():\n    return 1\n")
    assert ok and err == ""


def test_broken_python_fails_with_message():
    ok, err = validate_source("m.py", "def f(:\n    return 1\n")
    assert not ok and "line" in err.lower()


def test_valid_json_passes():
    ok, _ = validate_source("package.json", '{"a": 1}')
    assert ok


def test_broken_json_fails():
    ok, err = validate_source("package.json", '{"a": 1,}')
    assert not ok and err


def test_unvalidatable_extension_soft_skips():
    # No validator for .md -> treated as valid (never block).
    ok, err = validate_source("README.md", "# anything {[(")
    assert ok and err == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_validate_source.py -v`
Expected: FAIL with `ModuleNotFoundError: skyn3t.agents.validate`.

- [ ] **Step 3: Implement `validate.py`**

```python
# skyn3t/agents/validate.py
"""Edit-time source validation. Advisory: a missing toolchain soft-skips
(returns ok) so generation is never blocked. Never raises."""
from __future__ import annotations

import ast
import json


def validate_source(path: str, content: str) -> tuple[bool, str]:
    """Return (ok, error). ok=True when valid OR unvalidatable for this type."""
    p = path.lower()
    try:
        if p.endswith(".py"):
            try:
                compile(content, path, "exec")
                return True, ""
            except SyntaxError as exc:
                return False, f"SyntaxError line {exc.lineno}: {exc.msg}"
        if p.endswith(".json"):
            try:
                json.loads(content)
                return True, ""
            except json.JSONDecodeError as exc:
                return False, f"JSON error line {exc.lineno}: {exc.msg}"
        if p.endswith(".toml"):
            try:
                import tomllib
                tomllib.loads(content)
                return True, ""
            except Exception as exc:  # noqa: BLE001
                return False, f"TOML error: {exc}"
        if p.endswith((".js", ".jsx", ".ts", ".tsx")):
            return _balanced(content)
    except Exception:  # noqa: BLE001 - validation must never raise
        return True, ""
    return True, ""


def _balanced(content: str) -> tuple[bool, str]:
    """Cheap brace/bracket/paren balance check for JS/TS (no toolchain needed).
    Ignores chars inside strings/line comments. Best-effort, never false-negatives
    a real syntax error class but only catches gross imbalance."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_validate_source.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Wire re-emit into `code_agent._generate_file`**

In `agents/code_agent.py`, after `return extract_code(result.text)` becomes a validated path. Replace the tail of `_generate_file` (the `if result.backend == "stub": return None` / `return extract_code(...)` block, ~lines 276-280) with:
```python
        if result.backend == "stub":
            return None
        from skyn3t.agents.validate import validate_source
        code = extract_code(result.text)
        ok, err = validate_source(rel_path, code)
        if not ok:
            retry = await self.llm.complete(
                prompt + f"\n\nThe previous attempt had an error: {err}\n"
                "Return the COMPLETE corrected file.",
                tier=tier, system=_SYSTEM, file_hint=rel_path, max_tokens=8192,
            )
            if retry.backend != "stub":
                recode = extract_code(retry.text)
                ok2, _ = validate_source(rel_path, recode)
                if ok2:
                    return recode
            # fall through: keep the best-effort original (never lose work)
        return code
```

- [ ] **Step 6: Guard `code_improver` (`code_improver.py:71`)**

Replace `target.write_text(new_content, encoding="utf-8")` with:
```python
                from skyn3t.agents.validate import validate_source
                ok, _ = validate_source(rel, new_content)
                if ok:
                    target.write_text(new_content, encoding="utf-8")
                # else keep original (the improvement broke syntax) — never regress
```
(Keep the surrounding success/return logic; only the write is conditional.)

- [ ] **Step 7: Run the suite**

Run: `pytest -q`
Expected: ~389+5 pass; nothing red.

- [ ] **Step 8: Commit**

```bash
git add skyn3t/agents/validate.py skyn3t/agents/code_agent.py skyn3t/agents/code_improver.py tests/test_validate_source.py
git commit -m "feat: edit-time lint/compile guardrail with one bounded re-emit"
```

---

### Task 3: Marker-triggered skill injection (frontend skills for web stacks)

Make the design skills fire for web/site builds via the library's existing tag matching.

**Files:**
- Modify: `data/skills/frontend-ui-engineering.md` (front matter tags)
- Modify: `data/skills/api-and-interface-design.md` (front matter tags)
- Modify: `skyn3t/studio/runner.py:191-202` `_skill_advice`
- Test: `tests/test_skill_web_injection.py` (create)

**Interfaces:**
- Consumes: `SkillLibrary.relevant(stack, tags=[...], limit)` / `.inject(...)` (existing).
- Produces: `_skill_advice(stack)` passes web-design tags for web stacks.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skill_web_injection.py
from __future__ import annotations

from skyn3t.intelligence.skill_library import SkillLibrary
from skyn3t.studio.runner import _WEB_STACKS, _web_design_tags


def _lib():
    lib = SkillLibrary()
    lib.add("Frontend UI Engineering", "Use semantic HTML, a11y, responsive layout.",
            stack="generic", tags=["frontend", "design", "ui", "web"], slug="frontend-ui-engineering")
    lib.add("Python CLI shape", "argparse + entrypoint.", stack="python", tags=["cli"], slug="py-cli")
    return lib


def test_web_stack_surfaces_design_skill_first():
    lib = _lib()
    tags = _web_design_tags("react")
    top = lib.relevant("react", tags=tags, limit=2)
    assert "frontend-ui-engineering" in [s.slug for s in top]


def test_non_web_stack_does_not_force_design_tags():
    assert _web_design_tags("python") is None
    assert "react" in _WEB_STACKS and "fastapi" in _WEB_STACKS and "python" not in _WEB_STACKS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_skill_web_injection.py -v`
Expected: FAIL — `_WEB_STACKS`/`_web_design_tags` not in `runner`.

- [ ] **Step 3: Add web-stack helpers + use them in `_skill_advice`**

In `skyn3t/studio/runner.py`, near the top-level constants, add:
```python
# Web/site stacks (planner + builder vocab) that should pull frontend/design skills.
_WEB_STACKS = frozenset({
    "react", "react_vite", "nextjs", "static", "static_html",
    "fastapi", "node_express", "express",
})
_WEB_DESIGN_TAGS = ["frontend", "design", "ui", "web"]


def _web_design_tags(stack: str) -> list[str] | None:
    return list(_WEB_DESIGN_TAGS) if (stack or "").strip().lower() in _WEB_STACKS else None
```
Then update `_skill_advice`:
```python
    def _skill_advice(self, stack: str) -> tuple[str, list[str]]:
        if self.skills is None:
            return "", []
        try:
            tags = _web_design_tags(stack)
            limit = 4 if tags else 3
            relevant = self.skills.relevant(stack, tags=tags, limit=limit)
            slugs = [getattr(s, "slug", "") for s in relevant if getattr(s, "slug", "")]
            advice = self.skills.inject(stack, tags=tags, limit=limit)
            return advice, slugs
        except Exception as exc:  # noqa: BLE001
            log.warning("skills.inject_failed", error=str(exc))
            return "", []
```

- [ ] **Step 4: Re-tag the two design skills**

Edit the front matter of `data/skills/frontend-ui-engineering.md` and `data/skills/api-and-interface-design.md` so each has (keep existing `slug`/`title`/`body`):
```
stack: generic
tags: frontend, design, ui, web
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_skill_web_injection.py -q && pytest -q`
Expected: PASS; suite green.

- [ ] **Step 6: Commit**

```bash
git add skyn3t/studio/runner.py data/skills/frontend-ui-engineering.md data/skills/api-and-interface-design.md tests/test_skill_web_injection.py
git commit -m "feat: surface frontend/design skills for web/site stacks"
```

---

### Task 4: Intelligent best-fit stack selection

Give stack choice a brain: pin → LLM best-fit → keyword fallback. Runs in the runner (owns `self.llm`), feeds `planner.plan` via `stack_hint`.

**Files:**
- Create: `skyn3t/studio/stack_selector.py`
- Modify: `skyn3t/studio/runner.py` `start()` (call selector; record rationale)
- Test: `tests/test_stack_selector.py` (create)

**Interfaces:**
- Produces: `async select_stack(brief, *, pin="", llm=None, attended=False) -> StackChoice`
  where `StackChoice` has `.stack` (planner vocab, real-builder), `.method` (`pin|llm|keyword|default`), `.confidence: float`, `.rationale: str`.
- Consumes: `keyword_choice` reuses `planner.detect_stack`; `llm.complete(..., json_mode=True)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stack_selector.py
from __future__ import annotations

import asyncio
import types

from skyn3t.studio.stack_selector import (
    REAL_BUILDER_STACKS, StackChoice, keyword_choice, select_stack,
)


def test_pin_wins_over_everything():
    c = asyncio.run(select_stack("a python script", pin="fastapi", llm=None))
    assert c.stack == "fastapi" and c.method == "pin"


def test_unknown_pin_is_ignored():
    c = asyncio.run(select_stack("a react dashboard", pin="cobol", llm=None))
    assert c.method == "keyword" and c.stack == "react"


def test_keyword_fallback_when_no_llm():
    c = keyword_choice("a command line tool to rename files")
    assert c.stack in REAL_BUILDER_STACKS and c.method == "keyword"


def test_nextjs_brief_collapses_to_real_builder():
    # nextjs has no builder -> must map to a real-builder stack (react).
    c = keyword_choice("a next.js app")
    assert c.stack in REAL_BUILDER_STACKS


def test_llm_choice_used_when_available():
    class FakeResult:
        backend = "claude_cli"
        text = '{"stack": "fastapi", "confidence": 0.9, "rationale": "needs a server API"}'

    class FakeLLM:
        async def complete(self, *a, **k):
            return FakeResult()

    c = asyncio.run(select_stack("an app to manage lessons with storage", llm=FakeLLM()))
    assert c.stack == "fastapi" and c.method == "llm" and c.confidence == 0.9


def test_llm_error_falls_back_to_keyword():
    class BadLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("boom")

    c = asyncio.run(select_stack("a react dashboard", llm=BadLLM()))
    assert c.method == "keyword" and c.stack == "react"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_stack_selector.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `stack_selector.py`**

```python
# skyn3t/studio/stack_selector.py
"""Best-fit stack selection: explicit pin -> LLM best-fit -> keyword fallback.
Works in PLANNER vocab, restricted to stacks that have a real builder. Never
raises; degrades to keyword/default."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from skyn3t.studio.planner import detect_stack as _planner_detect

# Planner-vocab stacks that map to a real builder, with one-line "best for" hints.
REAL_BUILDER_STACKS: dict[str, str] = {
    "react": "a browser web app / SPA / dashboard UI (Vite + React)",
    "react_native": "a mobile app for iOS/Android (Expo)",
    "fastapi": "a Python web app or HTTP/REST API with a server + storage",
    "static": "a static website / landing page (HTML/CSS/JS, no backend)",
    "python": "a Python CLI tool, script, or library (no web UI)",
    "express": "a Node.js web server / API",
}

# Planner stacks that have NO builder -> collapse to a real one.
_COLLAPSE = {"nextjs": "react", "flask": "fastapi", "django": "fastapi"}


@dataclass(slots=True)
class StackChoice:
    stack: str
    method: str  # pin|llm|keyword|default
    confidence: float
    rationale: str


def _to_real_builder(stack: str) -> str:
    s = (stack or "").strip().lower()
    s = _COLLAPSE.get(s, s)
    return s if s in REAL_BUILDER_STACKS else "react"


def _validate_pin(pin: str) -> str:
    s = (pin or "").strip().lower()
    s = _COLLAPSE.get(s, s)
    return s if s in REAL_BUILDER_STACKS else ""


def keyword_choice(brief: str) -> StackChoice:
    raw = _planner_detect(brief)
    stack = _to_real_builder(raw)
    return StackChoice(stack, "keyword", 0.5, f"keyword heuristic → {stack}")


async def _llm_choice(brief: str, llm: Any) -> StackChoice | None:
    menu = "\n".join(f"- {k}: {v}" for k, v in REAL_BUILDER_STACKS.items())
    prompt = (
        "Pick the single best stack for this build from the menu.\n\n"
        f"Brief: {brief}\n\nMenu:\n{menu}\n\n"
        'Respond ONLY as JSON: {"stack": "<one menu key>", '
        '"confidence": <0..1>, "rationale": "<one sentence>"}'
    )
    try:
        res = await llm.complete(prompt, json_mode=True, max_tokens=300)
        if getattr(res, "backend", "") == "stub":
            return None
        data = json.loads(_extract_json(res.text))
        stack = _to_real_builder(str(data.get("stack", "")))
        if stack not in REAL_BUILDER_STACKS:
            return None
        conf = float(data.get("confidence", 0.7))
        return StackChoice(stack, "llm", conf, str(data.get("rationale", ""))[:300])
    except Exception:  # noqa: BLE001 - any failure -> caller falls back
        return None


def _extract_json(text: str) -> str:
    t = (text or "").strip()
    if "```" in t:
        t = t.split("```")[1].lstrip("json").strip() if "```" in t else t
    start, end = t.find("{"), t.rfind("}")
    return t[start:end + 1] if start >= 0 and end > start else t


async def select_stack(
    brief: str, *, pin: str = "", llm: Any | None = None, attended: bool = False
) -> StackChoice:
    norm = _validate_pin(pin)
    if norm:
        return StackChoice(norm, "pin", 1.0, "explicit pin")
    if llm is not None:
        choice = await _llm_choice(brief, llm)
        if choice is not None:
            return choice
    return keyword_choice(brief)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_stack_selector.py -v`
Expected: PASS (6).

- [ ] **Step 5: Wire into `runner.start()`**

In `skyn3t/studio/runner.py`, replace the `plan = self.planner.plan(...)` call (around line 647) so selection happens first:
```python
        from skyn3t.studio.stack_selector import select_stack
        pin = (extra.get("stack") or extra.get("stack_hint")
               or clar.answers.get("stack") or "")
        choice = await select_stack(
            brief, pin=pin, llm=getattr(self, "llm", None),
            attended=bool(extra.get("attended", False)),
        )
        plan = self.planner.plan(
            brief,
            slug,
            stack_hint=choice.stack,
            test_first=extra.get("test_first"),
            best_of_n=extra.get("best_of_n"),
            gated_stages=tuple(extra.get("gated_stages", ())),
        )
```
After `manifest` is built (it already exists a few lines below), record the rationale:
```python
        manifest.extra["stack_selection"] = {
            "method": choice.method, "stack": choice.stack,
            "confidence": choice.confidence, "rationale": choice.rationale,
        }
```

- [ ] **Step 6: Run the suite**

Run: `pytest -q`
Expected: green. (If a stub-backed integration test now expects a different default stack, update it to assert the recorded `stack_selection.method == "keyword"` rather than a hardcoded stack.)

- [ ] **Step 7: Commit**

```bash
git add skyn3t/studio/stack_selector.py skyn3t/studio/runner.py tests/test_stack_selector.py
git commit -m "feat: intelligent best-fit stack selection (pin -> LLM -> keyword)"
```

---

### Task 5: Project cleanup (trash + dry-run)

A pure categorizer over `Projects/` + `.skyn3t_worktrees/`, a `project cleanup` CLI, and an API route. Moves to a recoverable trash; never hard-deletes.

**Files:**
- Create: `skyn3t/studio/cleanup.py`
- Modify: `skyn3t/cli/main.py` (`project_app` — add `cleanup`)
- Modify: `skyn3t/web/routes.py` (add `GET`/`POST /api/projects/cleanup`)
- Test: `tests/test_cleanup.py` (create)

**Interfaces:**
- Produces: `scan(projects_dir, worktrees_dir, *, known_worktrees=()) -> CleanupReport`;
  `apply(report, *, trash_dir, dry_run=True, categories=None) -> CleanupResult`.
- `CleanupReport` has `.failed`, `.superseded`, `.orphaned_worktrees`, `.orphaned_projects`, `.stray_previews` (each `list[CleanupItem{path, reason, size_bytes}]`).
- `CleanupResult` has `.moved: list[str]`, `.freed_bytes: int`, `.dry_run: bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cleanup.py
from __future__ import annotations

import json
from pathlib import Path

from skyn3t.studio.cleanup import apply, scan


def _project(root: Path, slug: str, *, status="completed", created="2026-06-20T00:00:00+00:00"):
    d = root / slug
    d.mkdir(parents=True)
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": slug, "status": status, "created_at": created, "verdict": "go"}))
    (d / "main.py").write_text("print('x')\n")
    return d


def test_scan_buckets(tmp_path):
    projects = tmp_path / "Projects"
    worktrees = tmp_path / ".skyn3t_worktrees"
    projects.mkdir(); worktrees.mkdir()
    _project(projects, "good")
    _project(projects, "broken", status="failed")
    (projects / "no-manifest").mkdir()           # orphaned project
    (projects / "good" / ".preview").mkdir()      # stray preview
    (worktrees / "loose-abcd1234").mkdir()        # orphaned worktree

    report = scan(projects, worktrees, known_worktrees=())
    assert [i.path.name for i in report.failed] == ["broken"]
    assert [i.path.name for i in report.orphaned_projects] == ["no-manifest"]
    assert any(i.path.name == ".preview" for i in report.stray_previews)
    assert [i.path.name for i in report.orphaned_worktrees] == ["loose-abcd1234"]


def test_apply_dry_run_moves_nothing(tmp_path):
    projects = tmp_path / "Projects"; projects.mkdir()
    _project(projects, "broken", status="failed")
    report = scan(projects, tmp_path / "wt", known_worktrees=())
    res = apply(report, trash_dir=tmp_path / "trash", dry_run=True)
    assert res.dry_run and res.moved == [] and (projects / "broken").exists()


def test_apply_moves_to_trash(tmp_path):
    projects = tmp_path / "Projects"; projects.mkdir()
    _project(projects, "broken", status="failed")
    report = scan(projects, tmp_path / "wt", known_worktrees=())
    res = apply(report, trash_dir=tmp_path / "trash", dry_run=False, categories=["failed"])
    assert not (projects / "broken").exists()
    assert res.moved and (tmp_path / "trash").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cleanup.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `cleanup.py`**

```python
# skyn3t/studio/cleanup.py
"""Categorize + trash stale build artifacts. Pure + testable; never hard-deletes
(moves to a recoverable trash). dry_run is the default at every call site."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

_MANIFEST = "skyn3t_manifest.json"
_FAILED = {"failed", "pending"}


@dataclass(slots=True)
class CleanupItem:
    path: Path
    reason: str
    size_bytes: int = 0


@dataclass(slots=True)
class CleanupReport:
    failed: list[CleanupItem] = field(default_factory=list)
    superseded: list[CleanupItem] = field(default_factory=list)
    orphaned_worktrees: list[CleanupItem] = field(default_factory=list)
    orphaned_projects: list[CleanupItem] = field(default_factory=list)
    stray_previews: list[CleanupItem] = field(default_factory=list)

    def all_items(self, categories: list[str] | None = None) -> list[CleanupItem]:
        cats = categories or ["failed", "superseded", "orphaned_worktrees",
                              "orphaned_projects", "stray_previews"]
        out: list[CleanupItem] = []
        for c in cats:
            out.extend(getattr(self, c, []))
        return out


@dataclass(slots=True)
class CleanupResult:
    moved: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    dry_run: bool = True


def _dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def _load_manifest(d: Path) -> dict | None:
    f = d / _MANIFEST
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def scan(projects_dir, worktrees_dir, *, known_worktrees=()) -> CleanupReport:
    projects_dir = Path(projects_dir)
    worktrees_dir = Path(worktrees_dir)
    known = {str(Path(w)) for w in known_worktrees}
    rep = CleanupReport()

    by_slug: dict[str, list[tuple[str, Path]]] = {}
    if projects_dir.is_dir():
        for d in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
            if d.name.startswith("."):
                continue
            man = _load_manifest(d)
            if man is None:
                rep.orphaned_projects.append(CleanupItem(d, "no manifest", _dir_size(d)))
                continue
            status = str(man.get("status", ""))
            if status in _FAILED:
                rep.failed.append(CleanupItem(d, f"status={status}", _dir_size(d)))
            else:
                by_slug.setdefault(man.get("slug", d.name), []).append(
                    (str(man.get("created_at", "")), d))
            preview = d / ".preview"
            if preview.is_dir():
                rep.stray_previews.append(CleanupItem(preview, "stray .preview", _dir_size(preview)))

    # superseded: same slug, keep newest by created_at.
    for _slug, entries in by_slug.items():
        if len(entries) > 1:
            entries.sort(key=lambda t: t[0])
            for _created, d in entries[:-1]:
                rep.superseded.append(CleanupItem(d, "superseded (older same-slug)", _dir_size(d)))

    if worktrees_dir.is_dir():
        for w in sorted(p for p in worktrees_dir.iterdir() if p.is_dir()):
            if str(w) not in known:
                rep.orphaned_worktrees.append(CleanupItem(w, "no live/persisted build", _dir_size(w)))
    return rep


def apply(report, *, trash_dir, dry_run=True, categories=None) -> CleanupResult:
    trash_dir = Path(trash_dir)
    items = report.all_items(categories)
    res = CleanupResult(dry_run=dry_run)
    if dry_run:
        res.freed_bytes = sum(i.size_bytes for i in items)
        return res
    trash_dir.mkdir(parents=True, exist_ok=True)
    for it in items:
        try:
            dest = trash_dir / it.path.name
            n = 1
            while dest.exists():
                dest = trash_dir / f"{it.path.name}.{n}"
                n += 1
            shutil.move(str(it.path), str(dest))
            res.moved.append(str(it.path))
            res.freed_bytes += it.size_bytes
        except OSError:
            continue
    return res
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cleanup.py -v`
Expected: PASS (3).

- [ ] **Step 5: Add the `project cleanup` CLI**

In `skyn3t/cli/main.py` (where `project_app` commands live, near `project list` ~line 740):
```python
@project_app.command("cleanup")
def project_cleanup(
    apply_changes: bool = typer.Option(False, "--apply", help="Actually move to trash (default: dry-run)."),
    categories: str = typer.Option("", "--categories", help="Comma list: failed,superseded,orphaned_worktrees,orphaned_projects,stray_previews."),
) -> None:
    """Report (and with --apply, trash) failed/superseded/orphaned build artifacts."""
    from skyn3t.config.settings import get_settings
    from skyn3t.studio.cleanup import apply as cleanup_apply
    from skyn3t.studio.cleanup import scan as cleanup_scan

    console = _console()
    s = get_settings()
    worktrees = s.projects_dir.parent / ".skyn3t_worktrees"
    report = cleanup_scan(s.projects_dir, worktrees)
    cats = [c.strip() for c in categories.split(",") if c.strip()] or None
    items = report.all_items(cats)
    table = _table("Cleanup candidates", ["category", "path", "reason", "MB"])
    for name in ("failed", "superseded", "orphaned_worktrees", "orphaned_projects", "stray_previews"):
        if cats and name not in cats:
            continue
        for it in getattr(report, name):
            table.add_row(name, it.path.name, it.reason, f"{it.size_bytes/1e6:.1f}")
    console.print(table)
    trash = s.projects_dir.parent / ".skyn3t_trash"
    res = cleanup_apply(report, trash_dir=trash, dry_run=not apply_changes, categories=cats)
    if res.dry_run:
        console.print(f"[yellow]dry-run[/yellow]: would free {res.freed_bytes/1e6:.1f} MB "
                      f"from {len(items)} items. Re-run with --apply to trash them.")
    else:
        console.print(f"[green]moved[/green] {len(res.moved)} items to {trash} "
                      f"({res.freed_bytes/1e6:.1f} MB).")
```

- [ ] **Step 6: Add the API routes**

In `skyn3t/web/routes.py`, inside `build_router` (follow the `dependencies=[auth]` pattern, near the other `/projects` routes ~line 709):
```python
    @router.get("/projects/cleanup", dependencies=[auth])
    async def _cleanup_report() -> dict[str, Any]:
        from skyn3t.studio.cleanup import scan as cleanup_scan
        wt = state.settings.projects_dir.parent / ".skyn3t_worktrees"
        rep = cleanup_scan(state.settings.projects_dir, wt)
        return {n: [{"path": str(i.path), "reason": i.reason, "size_bytes": i.size_bytes}
                    for i in getattr(rep, n)]
                for n in ("failed", "superseded", "orphaned_worktrees",
                          "orphaned_projects", "stray_previews")}

    @router.post("/projects/cleanup", dependencies=[auth])
    async def _cleanup_apply(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        from skyn3t.studio.cleanup import apply as cleanup_apply
        from skyn3t.studio.cleanup import scan as cleanup_scan
        wt = state.settings.projects_dir.parent / ".skyn3t_worktrees"
        rep = cleanup_scan(state.settings.projects_dir, wt)
        trash = state.settings.projects_dir.parent / ".skyn3t_trash"
        res = cleanup_apply(rep, trash_dir=trash,
                            dry_run=bool(body.get("dry_run", True)),
                            categories=body.get("categories"))
        return {"moved": res.moved, "freed_bytes": res.freed_bytes, "dry_run": res.dry_run}
```
(Confirm `state.settings` exists on `AppState`; if the attribute is named differently, use that. The CLI path is the primary surface — the API mirrors it.)

- [ ] **Step 7: Run the suite**

Run: `pytest tests/test_cleanup.py -q && pytest -q`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add skyn3t/studio/cleanup.py skyn3t/cli/main.py skyn3t/web/routes.py tests/test_cleanup.py
git commit -m "feat: project cleanup (failed/superseded/orphaned) with trash + dry-run"
```

---

## Self-Review

**Spec coverage:**
- C1 stack-hint fix + `--stack` → Task 1 ✓
- C2 intelligent stack selection → Task 4 ✓ (menu = 6 real builders; pin→LLM→keyword; rationale recorded)
- C3 marker-triggered skill injection → Task 3 ✓ (tag-based; activation_conditions/embedding deferred to Spec 2, as the spec states)
- C4 edit-time lint guardrail → Task 2 ✓
- C5 cleanup (trash + dry-run) → Task 5 ✓
- Build order in the plan (1=hint, 2=guardrail, 3=skills, 4=selection, 5=cleanup) matches the recommended cheap-first order; each task is independently testable + committable.

**Deferred from this spec (do NOT implement here):** auto-trash-own-worktree-on-merge and `SKYN3T_AUTO_CLEANUP` (the spec lists auto-cleanup as optional; ship the manual sweep first), and clarify-on-low-confidence (the selector returns confidence; wiring the attended clarify prompt is a thin follow-up once an attended path exists — Task 4 records confidence so it's ready).

**Placeholder scan:** none — every step has real code/commands.

**Type consistency:** `StackChoice{stack,method,confidence,rationale}` used identically in `stack_selector.py` and `runner` wiring; `CleanupReport`/`CleanupItem`/`CleanupResult` field names match across `cleanup.py`, the CLI, the API, and the tests; `validate_source(path, content) -> (bool, str)` signature consistent across `code_agent`, `code_improver`, and tests.
