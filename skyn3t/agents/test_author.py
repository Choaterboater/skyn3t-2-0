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
from pathlib import PurePosixPath
from typing import Any

from skyn3t.agents import _verify_common as vc
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier
from skyn3t.studio.acceptance_contract import (
    GENERATED_ACCEPTANCE_HEADER,
    GENERATED_ACCEPTANCE_PENDING_MARKER,
)

# Sentence-ish splitter for turning a brief into discrete acceptance criteria.
_SPLIT = re.compile(r"[.\n;]|(?:\band\b)|(?:\bthen\b)")
_EXPLICIT_COUNT = re.compile(
    r"\b(\d{1,4})\s*(?:-| )?\s*(levels?|islands?|phases?|worlds?|stages?|waves?)\b",
    re.IGNORECASE,
)

_PAGE_SUFFIXES_BY_STACK: dict[str, tuple[str, ...]] = {
    "astro": (".astro",),
    "static": (".html", ".htm"),
    "static_html": (".html", ".htm"),
}
_EXCLUDED_PATH_PARTS = frozenset(
    {".git", ".preview", "node_modules", "dist", "build", "out", ".next"}
)
_NON_PAGE_DIRS = frozenset({"components", "includes", "layouts", "partials", "templates"})


def _safe_relative_path(raw: Any, *, allow_web_root: bool = False) -> str | None:
    path = str(raw or "").strip().replace("\\", "/").split("?", 1)[0].split("#", 1)[0]
    if allow_web_root:
        path = path.lstrip("/")
    if not path or "\x00" in path or ":" in path or path.startswith("/"):
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    if _EXCLUDED_PATH_PARTS.intersection(part.casefold() for part in pure.parts):
        return None
    return pure.as_posix()


def derive_planned_pages(plan: dict[str, Any] | None, stack: str = "") -> list[str]:
    """Safe architect-owned feature pages for stack-native acceptance checks."""
    if not isinstance(plan, dict):
        return []
    effective_stack = str(stack or plan.get("stack") or "").strip().lower()
    suffixes = _PAGE_SUFFIXES_BY_STACK.get(effective_stack, ())
    if not suffixes:
        return []
    pages: list[str] = []
    seen: set[str] = set()
    for item in plan.get("files") or []:
        raw = item.get("path") or item.get("file") if isinstance(item, dict) else item
        path = _safe_relative_path(raw)
        if path is None or not path.casefold().endswith(suffixes):
            continue
        parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
        if effective_stack == "astro" and not (
            path.casefold().startswith("src/pages/") or path.casefold().startswith("pages/")
        ):
            continue
        if effective_stack in {"static", "static_html"} and _NON_PAGE_DIRS.intersection(parts):
            continue
        identity = path.casefold()
        if identity not in seen:
            seen.add(identity)
            pages.append(path)
    return pages


def derive_asset_paths(payload: dict[str, Any] | None) -> list[str]:
    """Safe generated/foundry asset paths supplied to this build stage."""
    if not isinstance(payload, dict):
        return []
    raw_extra = payload.get("extra")
    extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    raw_assets: list[Any] = []
    for value in (payload.get("assets"), extra.get("assets")):
        if isinstance(value, dict):
            value = value.get("assets")
        if isinstance(value, list):
            raw_assets.extend(value)

    for value in (payload.get("asset_foundry"), extra.get("asset_foundry")):
        if not isinstance(value, dict):
            continue
        selected = value.get("selected")
        if isinstance(selected, dict):
            raw_assets.extend(item for item in selected.values() if isinstance(item, dict))

    paths: list[str] = []
    seen: set[str] = set()
    for item in raw_assets:
        raw = item.get("file") or item.get("path") if isinstance(item, dict) else item
        path = _safe_relative_path(raw, allow_web_root=True)
        if path is None:
            continue
        identity = path.casefold()
        if identity not in seen:
            seen.add(identity)
            paths.append(path)
    return paths


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

    # Product briefs often carry numeric scope promises ("120 levels", "12 islands").
    # These must become machine-visible contracts, not skipped prose.
    for match in _EXPLICIT_COUNT.finditer(brief or ""):
        count, unit = match.group(1), match.group(2).lower()
        if count != "1" and not unit.endswith("s"):
            unit += "s"
        add(f"include exact brief count: {count} {unit}")

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


def render_test_file(
    acceptance: list[str],
    brief: str,
    slug: str = "app",
    *,
    planned_pages: list[str] | None = None,
    asset_paths: list[str] | None = None,
) -> str:
    """Render a runnable pytest acceptance suite.

    The generated tests are structural-but-real: they check the project actually
    contains source content and an entrypoint, and they record each acceptance
    criterion as an xfail-able marker so the suite is honest about which
    criteria are not yet machine-verified (no fabricated green — rule #3).
    """
    lines: list[str] = [
        f'"""{GENERATED_ACCEPTANCE_HEADER}',
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
        "SOURCE_SUFFIXES = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', ",
        "                   '.html', '.css', '.astro', '.vue', '.svelte', '.swift', ",
        "                   '.go', '.rs'}",
        "",
        "",
        "def _sources():",
        "    out = []",
        "    for dp, dn, fn in os.walk(PROJECT_DIR):",
        "        dn[:] = [d for d in dn if d not in {'.git', '.astro', '.next', 'node_modules',",
        "                   '__pycache__', '.venv', 'tests', 'dist', 'build', 'out'}]",
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
        "    sources = _sources()",
        "    names = {p.name for p in sources}",
        "    rels = {p.relative_to(PROJECT_DIR).as_posix() for p in sources}",
        "    entry_names = {'main.py', 'app.py', 'index.js', 'index.ts', 'index.html', ",
        "                   'server.js', 'main.ts', 'index.astro', 'App.vue', ",
        "                   '+page.svelte', 'App.svelte', 'MainApp.swift', 'main.swift'}",
        "    entry_paths = {'src/pages/index.astro', 'src/App.vue', ",
        "                   'src/routes/+page.svelte', 'Sources/App/MainApp.swift'}",
        "    assert names & entry_names or rels & entry_paths, (",
        "        f'no recognizable entrypoint among {sorted(rels)[:10]}'",
        "    )",
        "",
        "",
        "PLANNED_FEATURE_PAGES = [",
    ]
    for path in planned_pages or []:
        lines.append(f"    {json.dumps(path)},")
    lines += [
        "]",
        "",
        "",
        "def _project_file(relative_path):",
        "    target = (PROJECT_DIR / relative_path).resolve()",
        "    assert target.is_relative_to(PROJECT_DIR.resolve()), f'path escapes project: {relative_path}'",
        "    return target",
        "",
        "",
        '@pytest.mark.parametrize("relative_path", PLANNED_FEATURE_PAGES)',
        "def test_planned_feature_page_exists(relative_path):",
        "    target = _project_file(relative_path)",
        "    assert target.is_file(), f'architect-planned feature page is missing: {relative_path}'",
        "    assert target.stat().st_size > 0, f'architect-planned feature page is empty: {relative_path}'",
        "",
        "",
        "GENERATED_ASSETS = [",
    ]
    for path in asset_paths or []:
        lines.append(f"    {json.dumps(path)},")
    lines += [
        "]",
        "",
        "",
        "def _asset_file(relative_path):",
        "    candidates = [_project_file(relative_path)]",
        "    if relative_path.startswith('assets/'):",
        "        candidates.append(_project_file('public/' + relative_path))",
        "    return next((path for path in candidates if path.is_file()), candidates[0])",
        "",
        "",
        "def _image_signature_matches(target):",
        "    data = target.read_bytes()[:32]",
        "    suffix = target.suffix.lower()",
        "    if suffix == '.png':",
        "        return data.startswith(b'\\x89PNG\\r\\n\\x1a\\n')",
        "    if suffix in {'.jpg', '.jpeg'}:",
        "        return data.startswith(b'\\xff\\xd8\\xff')",
        "    if suffix == '.webp':",
        "        return data.startswith(b'RIFF') and data[8:12] == b'WEBP'",
        "    if suffix == '.gif':",
        "        return data.startswith((b'GIF87a', b'GIF89a'))",
        "    if suffix == '.bmp':",
        "        return data.startswith(b'BM')",
        "    if suffix == '.ico':",
        "        return data.startswith(b'\\x00\\x00\\x01\\x00')",
        "    if suffix == '.avif':",
        "        return data[4:12] in {b'ftypavif', b'ftypavis'}",
        "    return True",
        "",
        "",
        "def _asset_reference_tokens(relative_path):",
        "    web_path = relative_path[7:] if relative_path.startswith('public/') else relative_path",
        "    return {relative_path.lower(), ('/' + relative_path).lower(),",
        "            web_path.lower(), ('/' + web_path).lower(), Path(web_path).name.lower()}",
        "",
        "",
        '@pytest.mark.parametrize("relative_path", GENERATED_ASSETS)',
        "def test_generated_asset_exists(relative_path):",
        "    target = _asset_file(relative_path)",
        "    assert target.is_file(), f'generated asset is missing: {relative_path}'",
        "    assert target.stat().st_size > 0, f'generated asset is empty: {relative_path}'",
        "    assert _image_signature_matches(target), (",
        "        f'generated asset bytes do not match its image extension: {relative_path}'",
        "    )",
        "",
        "",
        '@pytest.mark.parametrize("relative_path", GENERATED_ASSETS)',
        "def test_generated_asset_is_referenced(relative_path):",
        "    corpus = '\\n'.join(",
        "        path.read_text(encoding='utf-8', errors='ignore').lower() for path in _sources()",
        "    )",
        "    tokens = _asset_reference_tokens(relative_path)",
        "    assert any(token in corpus for token in tokens), (",
        "        f'generated asset exists but is not referenced by source: {relative_path}'",
        "    )",
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
        # Skipped, not silently green: a documented criterion is NOT a behavioral
        # guarantee, so it must not read as a passing acceptance test (false
        # confidence). The structural tests above DO assert, so the file still
        # carries real coverage. Replace skip with real assertions per criterion
        # as the implementation lands.
        f'@pytest.mark.skip(reason="{GENERATED_ACCEPTANCE_PENDING_MARKER}")',
        '@pytest.mark.parametrize("criterion", ACCEPTANCE)',
        "def test_acceptance_criterion_documented(criterion):",
        '    """Each derived acceptance criterion is recorded for verification.',
        "",
        "    Authored test-first; the skip marks it as PENDING (documented, not",
        "    yet behaviorally verified) so it never reads as a passing acceptance.",
        '    """',
        "    assert isinstance(criterion, str) and len(criterion.split()) >= 2",
        "",
        "",
        "EXACT_COUNT_PHRASES = [",
    ]
    for c in acceptance:
        prefix = "include exact brief count: "
        if str(c).lower().startswith(prefix):
            lines.append(f"    {json.dumps(str(c)[len(prefix):])},")
    lines += [
        "]",
        "",
        "",
        "def test_exact_count_phrases_are_implemented_or_documented():",
        "    if not EXACT_COUNT_PHRASES:",
        "        return",
        "    corpus = '\\n'.join(p.read_text(encoding='utf-8', errors='ignore').lower() for p in _sources())",
        "    missing = [phrase for phrase in EXACT_COUNT_PHRASES if phrase.lower() not in corpus]",
        "    assert not missing, f'exact brief count phrases missing from source: {missing}'",
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
        raw_plan = payload.get("plan")
        plan: dict[str, Any] = raw_plan if isinstance(raw_plan, dict) else {}
        stack = str(payload.get("stack") or plan.get("stack") or "")
        planned_pages = derive_planned_pages(plan, stack)
        asset_paths = derive_asset_paths(payload)

        acceptance = derive_acceptance(brief, plan)
        acceptance = await self._maybe_llm_enrich(brief, acceptance)

        root = vc.resolve_project_dir(payload)
        written: list[str] = []
        if root is not None:
            try:
                tests_dir = root / "tests"
                tests_dir.mkdir(parents=True, exist_ok=True)
                target = tests_dir / f"test_acceptance_{slug}.py"
                target.write_text(
                    render_test_file(
                        acceptance,
                        brief,
                        slug,
                        planned_pages=planned_pages,
                        asset_paths=asset_paths,
                    ),
                    encoding="utf-8",
                )
                written.append(str(target.relative_to(root)))
            except OSError as exc:
                self.metadata["write_error"] = str(exc)

        return TaskResult(
            task_id=task.task_id, success=True,
            output={
                "tests_written": len(written),
                "test_files": written,
                "acceptance": acceptance,
                "planned_pages": planned_pages,
                "asset_paths": asset_paths,
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
