# tests/test_improve_agentic.py
"""Agentic improve: free-text dashboard goals route through the whole-project
agentic tool-loop (the same `agentic_build` machinery builds use), so a goal
like "add an SEO audit tool" can CREATE new pages/components instead of being
squeezed into a single-entrypoint rewrite. The classic per-file path remains
as the fallback whenever agentic is unavailable, fails, or lands nothing —
behavior never regresses below the pre-agentic baseline.

Do-no-harm invariants pinned here:
- every file the agentic session changed/created is re-validated; a broken
  rewrite is reverted (a broken NEW file is deleted) and reported in `skipped`
- unchanged files (byte-identical rewrites) are not counted as changes
- ImproveEngine threads the `improve_agentic` setting + CLI routing into the
  improver's payload; the flag defaults ON.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus

_PAGE = "export default function Page() {\n  return <main>home</main>;\n}\n"
_PAGE_IMPROVED = (
    "export default function Page() {\n"
    "  return <main>home<section>audit tool</section></main>;\n}\n"
)
_NEW_TOOL = (
    "export default function AuditTool() {\n"
    "  return <div>full SEO audit</div>;\n}\n"
)
_BROKEN = "export default function Page() {\n  return (<div>\n"  # unbalanced
_VALID_REWRITE_FOR_FALLBACK = (
    "export default function Page() {\n"
    "  return <main>home<aside>fallback change</aside></main>;\n}\n"
)


class _AgenticLLM:
    """Fake LLMClient: agentic_build writes scripted files into the workdir;
    complete() serves the classic per-file fallback path."""

    backend = "openrouter"

    def __init__(self, writes=None, ok=True, error="", completions=None):
        self.writes = dict(writes or {})
        self.ok = ok
        self.error = error
        self.completions = list(completions or [])
        self.agentic_calls: list[dict] = []
        self.complete_calls: list[dict] = []

    async def agentic_build(self, prompt, workdir, **kwargs):
        self.agentic_calls.append({"prompt": prompt, "workdir": workdir, **kwargs})
        for rel, content in self.writes.items():
            p = Path(workdir) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return {"ok": self.ok, "backend": "openrouter", "error": self.error}

    async def complete(self, prompt, **kwargs):
        self.complete_calls.append(kwargs)
        text = self.completions.pop(0) if self.completions else ""
        return SimpleNamespace(text=text, backend="openrouter")


def _seed(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(_PAGE, encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    return tmp_path


def _run(tmp_path: Path, llm, goal="add a full SEO audit tool", extra=None):
    improver = CodeImproverAgent(event_bus=EventBus(), llm=llm)

    async def go():
        await improver.start()
        return await improver.run(TaskRequest(
            type="code_improve",
            payload={"brief": goal, "worktree_dir": str(tmp_path),
                     "gaps": [goal], "agentic": True, **(extra or {})}))

    return asyncio.run(go())


def test_agentic_improve_counts_changed_and_created_files(tmp_path):
    _seed(tmp_path)
    llm = _AgenticLLM(writes={
        "app/page.jsx": _PAGE_IMPROVED,          # modified
        "app/tools/audit/page.jsx": _NEW_TOOL,   # created (multi-file!)
        "README.md": "# demo\n",                 # byte-identical — not a change
    })
    result = _run(tmp_path, llm)
    assert result.success
    assert result.output["files"] == ["app/page.jsx", "app/tools/audit/page.jsx"]
    assert result.output["skipped"] == {}
    assert llm.complete_calls == []  # classic path not consulted
    assert (tmp_path / "app" / "tools" / "audit" / "page.jsx").read_text() == _NEW_TOOL


def test_agentic_improve_reverts_broken_rewrites(tmp_path):
    _seed(tmp_path)
    llm = _AgenticLLM(writes={
        "app/page.jsx": _BROKEN,                 # broken rewrite of existing file
        "app/tools/audit/page.jsx": _NEW_TOOL,   # valid new file
        "app/tools/broken.jsx": _BROKEN,         # broken NEW file
    })
    result = _run(tmp_path, llm)
    assert result.success
    # The valid new file lands; both broken writes are unwound.
    assert result.output["files"] == ["app/tools/audit/page.jsx"]
    assert result.output["skipped"]["app/page.jsx"] == "invalid_rewrite"
    assert result.output["skipped"]["app/tools/broken.jsx"] == "invalid_rewrite"
    assert (tmp_path / "app" / "page.jsx").read_text() == _PAGE  # reverted
    assert not (tmp_path / "app" / "tools" / "broken.jsx").exists()  # deleted


def test_agentic_unavailable_falls_back_to_classic_path(tmp_path):
    _seed(tmp_path)
    llm = _AgenticLLM(ok=False, error="agentic unsupported",
                      completions=[_VALID_REWRITE_FOR_FALLBACK])
    result = _run(tmp_path, llm, extra={"files": ["app/page.jsx"]})
    assert result.success
    assert result.output["files"] == ["app/page.jsx"]
    assert len(llm.complete_calls) >= 1  # classic path ran
    assert (tmp_path / "app" / "page.jsx").read_text() == _VALID_REWRITE_FOR_FALLBACK


def test_agentic_landed_nothing_falls_back_to_classic_path(tmp_path):
    _seed(tmp_path)
    # agentic "succeeds" but writes nothing usable; classic path then reports
    # the honest reason (ALREADY_SATISFIED sentinel).
    llm = _AgenticLLM(writes={}, ok=True, completions=["ALREADY_SATISFIED"])
    result = _run(tmp_path, llm, extra={"files": ["app/page.jsx"]})
    assert result.success
    assert result.output["files"] == []
    assert result.output["skipped"]["app/page.jsx"] == "already_satisfied"


def test_agentic_payload_routing_reaches_agentic_build(tmp_path):
    _seed(tmp_path)
    llm = _AgenticLLM(writes={"app/page.jsx": _PAGE_IMPROVED})
    _run(tmp_path, llm, extra={"agentic_timeout": 555,
                               "agentic_provider": "claude",
                               "agentic_model": "sonnet"})
    call = llm.agentic_calls[0]
    assert call["timeout"] == 555
    assert call["provider"] == "claude"
    assert call["model"] == "sonnet"
    # The prompt frames this as improving an EXISTING app, not a fresh build.
    assert "existing" in call["prompt"].lower()


def test_engine_threads_improve_agentic_setting(tmp_path):
    import json

    from skyn3t.studio.improve import ImproveEngine

    projects = tmp_path / "Projects"
    projects.mkdir()
    d = projects / "demo"
    d.mkdir()
    (d / "main.py").write_text("print('x')\n")
    (d / "skyn3t_manifest.json").write_text(json.dumps(
        {"slug": "demo", "brief": "demo", "stack": "python", "status": "completed"}))

    submitted = []

    class _Orch:
        async def submit(self, task):
            submitted.append(task)
            return SimpleNamespace(success=True,
                                   output={"files": [], "skipped": {}, "backend": "stub"})

    settings = SimpleNamespace(
        projects_dir=projects, execution_backend="inline",
        run_generated_tests=False, run_generated_build=False,
        generated_test_timeout=90, generated_build_timeout=300,
        improve_agentic=True, improve_agentic_timeout=777,
        codegen_cli_provider="claude", codegen_cli_model="",
    )
    engine = ImproveEngine(EventBus(), _Orch(), settings=settings)
    asyncio.run(engine.improve("demo", "add a feature"))
    p = submitted[0].payload
    assert p["agentic"] is True
    assert p["agentic_timeout"] == 777
    assert p["agentic_provider"] == "claude"

    settings.improve_agentic = False
    asyncio.run(engine.improve("demo", "add a feature"))
    assert submitted[1].payload["agentic"] is False


def test_settings_default_on():
    from skyn3t.config.settings import Settings
    s = Settings()
    assert s.improve_agentic is True
    assert s.improve_agentic_timeout == 900
