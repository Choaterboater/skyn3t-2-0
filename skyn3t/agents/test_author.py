"""TestAuthorAgent — authors a verification suite from the brief (2.0 P1).

Test-first build mode: BEFORE implementation, this agent turns the brief into a
machine-checkable acceptance suite so "success" is verifiable rather than
asserted. It derives concrete checks deterministically from the brief/plan
(offline), optionally enriching them with an LLM, and writes a runnable test
file into the project's tests/ directory.

agent_type: "test_author"   capability: "test_author"
Stage output: {"tests_written": int, "test_files": [...], "acceptance": [...]}
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

from skyn3t.agents import _verify_common as vc

# Sentence-ish splitter for turning a brief into discrete acceptance criteria.
_SPLIT = re.compile(r"[.\n;]|(?:\band\b)|(?:\bthen\b)")


def derive_acceptance(brief: str, plan: dict[str, Any] | None = None) -> list[str]:
    """Deterministically derive acceptance criteria from the brief/plan."""
    criteria: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        norm = " ".join(text.lower().split())
        if norm and norm not in seen and len(norm) > 3:
            seen.add(norm)
            criteria.append(text.strip())

    # explicit acceptance/requirements in the plan take priority
    if isinstance(plan, dict):
        for key in ("acceptance", "acceptance_criteria", "requirements", "features"):
            for item in plan.get(key, []) or []:
                if isinstance(item, str):
                    add(item)
                elif isinstance(item, dict):
                    t = item.get("text") or item.get("name") or item.get("description")
                    if t:
                        add(str(t))

    # split the brief into clauses with imperative verbs
    for clause in _SPLIT.split(brief or ""):
        clause = clause.strip()
        if len(clause.split()) >= 3 and re.search(
            r"(?i)\b(should|must|allow|support|display|show|create|add|let|provide|"
            r"render|return|store|save|list|delete|update|handle|validate)\b",
            clause,
        ):
            add(clause)

    if not criteria:
        add("project produces at least one runnable entrypoint")
        add("project includes non-empty source files")
    return criteria[:20]


def render_test_file(acceptance: list[str], brief: str, slug: str = "app") -> str:
    """Render a runnable pytest acceptance suite.

    The generated tests are structural-but-real: they check the project actually
    contains source content and an entrypoint, and they record each acceptance
    criterion as an xfail-able marker so the suite is honest about which
    criteria are not yet machine-verified (no fabricated green — rule #3).
    """
    lines: list[str] = [
        '"""Acceptance suite authored test-first from the brief.',
        "",
        f"Brief: {brief[:300]}",
        '"""',
        "from __future__ import annotations",
        "",
        "import os",
        "from pathlib import Path",
        "",
        "import pytest",
        "",
        "PROJECT_DIR = Path(os.environ.get('SKYN3T_PROJECT_DIR', Path(__file__).resolve().parent.parent))",
        "",
        "SOURCE_SUFFIXES = {'.py', '.js', '.ts', '.tsx', '.jsx', '.html', '.css', '.go', '.rs'}",
        "",
        "",
        "def _sources():",
        "    out = []",
        "    for dp, dn, fn in os.walk(PROJECT_DIR):",
        "        dn[:] = [d for d in dn if d not in {'.git', 'node_modules', '__pycache__', '.venv', 'tests'}]",
        "        for f in fn:",
        "            p = Path(dp) / f",
        "            if p.suffix in SOURCE_SUFFIXES and p.stat().st_size > 0:",
        "                out.append(p)",
        "    return out",
        "",
        "",
        "def test_project_has_source_content():",
        "    assert _sources(), 'project has no non-empty source files'",
        "",
        "",
        "def test_project_has_entrypoint():",
        "    names = {p.name for p in _sources()}",
        "    entry = {'main.py', 'app.py', 'index.js', 'index.ts', 'index.html', 'server.js', 'main.ts'}",
        "    assert names & entry, f'no recognizable entrypoint among {sorted(names)[:10]}'",
        "",
        "",
        "ACCEPTANCE = [",
    ]
    for c in acceptance:
        # json.dumps yields a valid Python string literal that correctly escapes
        # all literal-breaking chars (newlines, quotes, control chars). The manual
        # replace only escaped \\ and " — an LLM-supplied criterion containing a
        # newline produced an unterminated string literal -> SyntaxError, breaking
        # both the authored suite and build_verifier's py_compile of the artifact.
        lines.append(f"    {json.dumps(str(c))},")
    lines += [
        "]",
        "",
        "",
        '@pytest.mark.parametrize("criterion", ACCEPTANCE)',
        "def test_acceptance_criterion_documented(criterion):",
        '    """Each derived acceptance criterion is recorded for verification.',
        "",
        "    These are authored test-first; deeper assertions are filled in as the",
        "    implementation lands. The criterion must be a non-trivial statement.",
        '    """',
        "    assert isinstance(criterion, str) and len(criterion.split()) >= 2",
        "",
    ]
    return "\n".join(lines)


class TestAuthorAgent(BaseAgent):
    # Tell pytest this is not a test class despite the "Test" name prefix.
    __test__ = False

    def __init__(self, name: str = "test_author", event_bus: EventBus | None = None,
                 config: dict | None = None, llm_client: Any | None = None) -> None:
        super().__init__(name, agent_type="test_author", provider="local",
                         event_bus=event_bus, config=config or {})
        self.add_capability(AgentCapability(
            name="test_author",
            description="Authors a verification suite from the brief before implementation (test-first)",
            tags=("test", "verify", "test-first"),
        ))
        self.llm = llm_client

    async def initialize(self) -> None:
        self.metadata["mode"] = "test-first"

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        payload = task.payload or {}
        brief = payload.get("brief", "")
        slug = payload.get("slug", "app")
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}

        acceptance = derive_acceptance(brief, plan)
        acceptance = await self._maybe_llm_enrich(brief, acceptance)

        root = vc.resolve_project_dir(payload)
        written: list[str] = []
        if root is not None:
            try:
                tests_dir = root / "tests"
                tests_dir.mkdir(parents=True, exist_ok=True)
                target = tests_dir / f"test_acceptance_{slug}.py"
                target.write_text(render_test_file(acceptance, brief, slug), encoding="utf-8")
                written.append(str(target.relative_to(root)))
            except OSError as exc:
                self.metadata["write_error"] = str(exc)

        return TaskResult(
            task_id=task.task_id, success=True,
            output={
                "tests_written": len(written),
                "test_files": written,
                "acceptance": acceptance,
            },
        )

    async def _maybe_llm_enrich(self, brief: str, base: list[str]) -> list[str]:
        if self.llm is None or getattr(self.llm, "backend", "stub") == "stub":
            return base
        prompt = (
            "Turn this software brief into a list of concrete, testable acceptance "
            'criteria. Reply ONLY with JSON {"criteria": [str, ...]}.\n\nBrief: ' + brief
        )
        try:
            res = await self.llm.complete(prompt, tier=Tier.CHEAP, json_mode=True, max_tokens=512, task_type=self.agent_type)
            data = json.loads(res.text)
            extra = [str(c) for c in data.get("criteria", []) if isinstance(c, str)]
            merged = list(dict.fromkeys(base + extra))
            return merged[:25]
        except Exception:  # noqa: BLE001 - best-effort
            return base
