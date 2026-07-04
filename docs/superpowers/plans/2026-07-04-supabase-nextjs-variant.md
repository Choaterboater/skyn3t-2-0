# Supabase Next.js Variant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an offline, testable Supabase-shaped scaffold variant for the existing Next.js stack.

**Architecture:** Keep Supabase as a scaffold variant, not a new stack. `scaffold_for("nextjs", ...)` dispatches to a Supabase-specific builder when the brief contains precise Supabase phrases; config detection predicts the required Supabase env vars before generation.

**Tech Stack:** Python scaffold generator and tests; generated app is Next.js App Router with `@supabase/supabase-js`.

## Global Constraints

- Do not add a new stack vocabulary entry.
- Trigger only on multi-word Supabase phrases.
- Do not require a live Supabase account or network access in tests.
- Public browser config keys are `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY`.
- Service-role/admin Supabase access uses server-scoped `SUPABASE_SERVICE_ROLE_KEY` and must not appear in browser code.
- Plain Next.js scaffolds must remain unchanged.

---

### Task 1: Supabase Config Detection

**Files:**
- Modify: `skyn3t/agents/config_detector.py`
- Modify: `tests/test_config_surfacing.py`

**Interfaces:**
- Consumes: `detect_from_brief(brief: str, stack: str = "", *, llm_fn: LLMFn | None = None) -> ConfigSpec`
- Produces: deterministic Supabase keys through `ConfigSpec.keys`

- [ ] **Step 1: Write failing tests**

Add tests to `tests/test_config_surfacing.py`:

```python
def test_detect_from_brief_supabase_public_keys_are_client_scoped():
    spec = detect_from_brief("a Next.js app with Supabase auth", "nextjs", llm_fn=None)
    by_name = {k.name: k for k in spec.keys}
    assert by_name["NEXT_PUBLIC_SUPABASE_URL"].kind == "url"
    assert by_name["NEXT_PUBLIC_SUPABASE_URL"].scope == "client"
    assert by_name["NEXT_PUBLIC_SUPABASE_ANON_KEY"].kind == "api_key"
    assert by_name["NEXT_PUBLIC_SUPABASE_ANON_KEY"].scope == "client"
    assert "Supabase" in spec.apis


def test_detect_from_brief_supabase_service_role_is_server_scoped():
    spec = detect_from_brief(
        "a Supabase admin dashboard that uses the service role key",
        "nextjs",
        llm_fn=None,
    )
    by_name = {k.name: k for k in spec.keys}
    assert by_name["SUPABASE_SERVICE_ROLE_KEY"].kind == "secret"
    assert by_name["SUPABASE_SERVICE_ROLE_KEY"].scope == "server"
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_config_surfacing.py::test_detect_from_brief_supabase_public_keys_are_client_scoped tests/test_config_surfacing.py::test_detect_from_brief_supabase_service_role_is_server_scoped -q
```

Expected: both fail because the Supabase-specific keys are not emitted yet.

- [ ] **Step 3: Implement detection**

In `skyn3t/agents/config_detector.py`, add Supabase-specific detection inside `_keyword_detect(...)` before returning:

```python
    if re.search(r"\bsupabase\s+(auth|database|backend|project|login|dashboard)\b", low):
        keys.setdefault("NEXT_PUBLIC_SUPABASE_URL", ConfigKey(
            name="NEXT_PUBLIC_SUPABASE_URL",
            kind="url",
            scope="client",
            description="Supabase project URL",
        ))
        keys.setdefault("NEXT_PUBLIC_SUPABASE_ANON_KEY", ConfigKey(
            name="NEXT_PUBLIC_SUPABASE_ANON_KEY",
            kind="api_key",
            scope="client",
            description="Supabase anon key",
        ))
        if "Supabase" not in apis:
            apis.append("Supabase")
        if re.search(r"\b(service[-_ ]?role|admin|server)\b", low):
            keys.setdefault("SUPABASE_SERVICE_ROLE_KEY", ConfigKey(
                name="SUPABASE_SERVICE_ROLE_KEY",
                kind="secret",
                scope="server",
                description="Supabase service role key",
            ))
```

- [ ] **Step 4: Verify GREEN**

Run the same two tests. Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add skyn3t/agents/config_detector.py tests/test_config_surfacing.py
git commit -m "Detect Supabase app config"
```

### Task 2: Next.js Supabase Scaffold Variant

**Files:**
- Modify: `skyn3t/agents/_scaffold.py`
- Create: `tests/test_supabase_variant.py`

**Interfaces:**
- Consumes: `scaffold_for(stack: str, app_name: str, brief: str = "", *, art: bool = False) -> dict[str, str]`
- Produces: `_implies_supabase(brief: str) -> bool` and a Supabase Next.js variant returned for `stack == "nextjs"`

- [ ] **Step 1: Write failing tests**

Create `tests/test_supabase_variant.py`:

```python
from __future__ import annotations

import json

from skyn3t.agents._scaffold import _implies_supabase, scaffold_for
from skyn3t.studio.planner import file_checklist
from skyn3t.studio.proof_run import proof_run


def test_supabase_trigger_matches_precise_phrases():
    for brief in (
        "a Next.js app with Supabase auth",
        "Supabase database dashboard",
        "Supabase backend for a member portal",
        "Supabase login for customers",
    ):
        assert _implies_supabase(brief), brief


def test_supabase_trigger_ignores_generic_database_auth():
    for brief in (
        "a database-backed Next.js app",
        "an auth dashboard",
        "a Firebase auth app",
        "a pricing page about Supabase",
    ):
        assert not _implies_supabase(brief), brief


def test_supabase_nextjs_brief_gets_variant_scaffold():
    files = scaffold_for("nextjs", "member-portal", "a Next.js app with Supabase auth")
    pkg = json.loads(files["package.json"])
    assert "@supabase/supabase-js" in pkg["dependencies"]
    assert "lib/supabaseClient.js" in files
    assert "NEXT_PUBLIC_SUPABASE_URL" in files["lib/supabaseClient.js"]
    assert "NEXT_PUBLIC_SUPABASE_ANON_KEY" in files["lib/supabaseClient.js"]
    assert "SUPABASE_SERVICE_ROLE_KEY" not in files["app/page.jsx"]
    assert "Supabase" in files["app/page.jsx"]


def test_plain_nextjs_brief_is_unchanged():
    files = scaffold_for("nextjs", "blog", "a Next.js blog")
    assert "lib/supabaseClient.js" not in files
    assert "@supabase/supabase-js" not in files["package.json"]


def test_supabase_variant_passes_nextjs_structural_proof(tmp_path):
    files = scaffold_for("nextjs", "member-portal", "a Supabase auth dashboard")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(tmp_path, checklist=file_checklist("nextjs"), stack="nextjs")
    assert res.passed, res.to_dict()
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_supabase_variant.py -q
```

Expected: import or assertion failures because `_implies_supabase` and the variant do not exist yet.

- [ ] **Step 3: Implement trigger and builder**

In `skyn3t/agents/_scaffold.py`:

- Add `_SUPABASE_KEYWORDS`.
- Add `_implies_supabase(brief: str) -> bool`.
- Add `_nextjs_supabase(app_name: str, brief: str) -> dict[str, str]`.
- Dispatch before the base Next.js builder:

```python
    if stack == "nextjs" and _implies_supabase(brief):
        return _nextjs_supabase(safe_name, brief)
```

The builder may reuse the base Next.js shape but must return a complete file map with a Supabase-specific `package.json`, `app/page.jsx`, and `lib/supabaseClient.js`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_supabase_variant.py tests/test_config_surfacing.py -q
```

Expected: pass.

- [ ] **Step 5: Broader verification**

Run:

```bash
.venv/bin/python -m pytest tests/test_supabase_variant.py tests/test_more_stacks.py tests/test_scaffold_docs.py tests/test_config_surfacing.py tests/test_serverside_llm_routing.py -q
```

Expected: pass or environment-appropriate skips only.

- [ ] **Step 6: Commit**

```bash
git add skyn3t/agents/_scaffold.py tests/test_supabase_variant.py
git commit -m "Add Supabase Next.js scaffold variant"
```

## Final Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_supabase_variant.py tests/test_more_stacks.py tests/test_scaffold_docs.py tests/test_config_surfacing.py tests/test_serverside_llm_routing.py -q
.venv/bin/python -m pytest -q
```

Then push `main` if the suite is green or only has intended environment skips.
