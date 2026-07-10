"""Full-app architecture must survive malformed or undersized model output."""

from __future__ import annotations

import json

import pytest

from skyn3t.adapters.llm import LLMResult
from skyn3t.agents.architect import ArchitectAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


class _ArchitectLLM:
    backend = "openrouter"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    async def complete(self, prompt: str, **kwargs) -> LLMResult:
        self.calls.append({"prompt": prompt, **kwargs})
        return LLMResult(
            text=self.text,
            model="test/architect",
            backend="openrouter",
        )


@pytest.mark.asyncio
async def test_full_app_truncated_plan_gets_brief_derived_astro_contract() -> None:
    llm = _ArchitectLLM('{"stack":"astro","files":[')
    agent = ArchitectAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(TaskRequest(
        type="architecture",
        payload={
            "brief": (
                "A golf website for adult beginners with lesson paths, drills, "
                "equipment basics, tutorial resources, and tee-time CTAs"
            ),
            "stack": "astro",
            "slug": "golf",
            "extra": {"full_app_contract": True},
        },
    ))

    paths = {item["path"] for item in result.output["plan"]["files"]}
    assert llm.calls[0]["max_tokens"] is None
    assert {
        "src/pages/lessons.astro",
        "src/pages/drills.astro",
        "src/pages/equipment.astro",
        "src/pages/resources.astro",
        "src/pages/book.astro",
        "src/components/SiteHeader.astro",
        "tests/site-contract.test.mjs",
    } <= paths
    assert len(paths) >= 18


@pytest.mark.asyncio
async def test_full_app_model_plan_is_augmented_with_hvac_pages() -> None:
    model_plan = {
        "plan": {
            "stack": "static_html",
            "summary": "Small model plan",
            "files": [
                {"path": "index.html", "purpose": "home"},
                {"path": "assets/fake-service.webp", "purpose": "invented binary"},
            ],
        },
    }
    llm = _ArchitectLLM(json.dumps(model_plan))
    agent = ArchitectAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(TaskRequest(
        type="architecture",
        payload={
            "brief": (
                "A local HVAC company website with service pages, financing "
                "calls-to-action, reviews, emergency contact, and generated service photos"
            ),
            "stack": "static_html",
            "slug": "hvac",
            "extra": {"full_app_contract": True},
        },
    ))

    paths = [item["path"] for item in result.output["plan"]["files"]]
    assert len(paths) == len(set(paths))
    assert "assets/fake-service.webp" not in paths
    assert "Do NOT plan binary image" in llm.calls[0]["prompt"]
    assert {
        "services.html",
        "financing.html",
        "reviews.html",
        "emergency.html",
        "contact.html",
        "heating.html",
        "cooling.html",
        "maintenance.html",
    } <= set(paths)


@pytest.mark.asyncio
async def test_full_app_recovery_does_not_duplicate_astro_index_routes() -> None:
    model_plan = {
        "stack": "astro",
        "summary": "Detailed golf architecture",
        "files": [
            {"path": "src/pages/index.astro", "purpose": "home"},
            {"path": "src/pages/lessons/index.astro", "purpose": "lesson hub"},
            {"path": "src/pages/drills/index.astro", "purpose": "drill hub"},
            {"path": "src/pages/equipment/index.astro", "purpose": "equipment hub"},
        ],
    }
    llm = _ArchitectLLM(json.dumps(model_plan))
    agent = ArchitectAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(TaskRequest(
        type="architecture",
        payload={
            "brief": (
                "A golf website for adult beginners with lesson paths, drills, "
                "equipment basics, tutorial resources, and tee-time CTAs"
            ),
            "stack": "astro",
            "extra": {"full_app_contract": True},
        },
    ))

    paths = {item["path"] for item in result.output["plan"]["files"]}
    assert "src/pages/lessons/index.astro" in paths
    assert "src/pages/drills/index.astro" in paths
    assert "src/pages/equipment/index.astro" in paths
    assert "src/pages/lessons.astro" not in paths
    assert "src/pages/drills.astro" not in paths
    assert "src/pages/equipment.astro" not in paths
    assert "src/pages/resources.astro" in paths
    assert "src/pages/book.astro" in paths


@pytest.mark.asyncio
async def test_full_app_canonicalizes_aliases_and_dedupes_recovery_files() -> None:
    llm = _ArchitectLLM(json.dumps({
        "stack": "astro",
        "summary": "Malformed config paths",
        "files": [
            {"path": "package.", "purpose": "package alias"},
            {"path": "package.json", "purpose": "duplicate package"},
            {"path": "tsconfig.", "purpose": "TypeScript config alias"},
            {"path": "tsconfig.json", "purpose": "duplicate TypeScript config"},
            {"path": "src/pages/index.astro", "purpose": "home"},
            {"path": "src/content/notes.", "purpose": "ambiguous trailing dot"},
        ],
    }))
    agent = ArchitectAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(TaskRequest(
        type="architecture",
        payload={
            "brief": "A complete Astro golf site with lesson paths",
            "stack": "astro",
            "extra": {"full_app_contract": True},
        },
    ))

    files = result.output["plan"]["files"]
    paths = [item["path"] for item in files]
    assert paths.count("package.json") == 1
    assert paths.count("tsconfig.json") == 1
    assert "package." not in paths
    assert "tsconfig." not in paths
    assert "src/content/notes." not in paths
    assert len(paths) == len({path.casefold() for path in paths})
    assert result.output["plan"]["build_order"] == paths


@pytest.mark.asyncio
async def test_unsafe_model_routes_cannot_shadow_full_app_recovery() -> None:
    unsafe_routes = [
        "/src/pages/lessons.astro",
        "../src/pages/drills.astro",
        r"C:\src\pages\equipment.astro",
        r"\\server\share\src\pages\resources.astro",
        "src/pages/book.astro:payload",
    ]
    llm = _ArchitectLLM(json.dumps({
        "stack": "astro",
        "summary": "Unsafe routes",
        "files": [
            {"path": path, "purpose": "unsafe route"}
            for path in unsafe_routes
        ] + [
            {"path": "src//pages/./index.astro", "purpose": "home"},
        ],
    }))
    agent = ArchitectAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(TaskRequest(
        type="architecture",
        payload={
            "brief": (
                "A golf site with lesson paths, drills, equipment, tutorial "
                "resources, and tee-time booking"
            ),
            "stack": "astro",
            "extra": {"full_app_contract": True},
        },
    ))

    paths = [item["path"] for item in result.output["plan"]["files"]]
    assert "src/pages/index.astro" in paths
    for route in ("lessons", "drills", "equipment", "resources", "book"):
        assert paths.count(f"src/pages/{route}.astro") == 1
    assert not set(unsafe_routes) & set(paths)


@pytest.mark.asyncio
async def test_standard_architect_accepts_plan_wrapper_and_uses_larger_floor() -> None:
    llm = _ArchitectLLM(json.dumps({
        "plan": {
            "stack": "astro",
            "summary": "wrapped",
            "files": [{"path": "src/pages/index.astro", "purpose": "home"}],
        },
    }))
    agent = ArchitectAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(TaskRequest(
        type="architecture",
        payload={"brief": "Astro brochure", "stack": "astro"},
    ))

    assert llm.calls[0]["max_tokens"] == 4_096
    assert result.output["plan"]["summary"] == "wrapped"
    assert result.output["plan"]["files"][0]["path"] == "src/pages/index.astro"


@pytest.mark.asyncio
async def test_standard_plan_canonicalizes_only_known_trailing_dot_aliases() -> None:
    llm = _ArchitectLLM(json.dumps({
        "stack": "astro",
        "summary": "config aliases",
        "files": [
            {"path": "./package.", "purpose": "package alias"},
            {"path": "PACKAGE.JSON", "purpose": "case-insensitive duplicate"},
            {"path": "tsconfig.", "purpose": "TypeScript config alias"},
            {"path": "jsconfig.", "purpose": "JavaScript config alias"},
            {"path": "vite.config.", "purpose": "ambiguous extension"},
            {"path": "/src/pages/absolute.astro", "purpose": "absolute"},
            {"path": "../outside.js", "purpose": "traversal"},
            {"path": r"C:\src\pages\drive.astro", "purpose": "drive"},
            {"path": r"\\server\share\unc.astro", "purpose": "UNC"},
            {"path": "src//pages//about.astro", "purpose": "repeated separators"},
            {"path": "src/pages/./contact.astro", "purpose": "dot segment"},
            {"path": "src./pages/bad.astro", "purpose": "trailing dot segment"},
            {"path": "src /pages/bad.astro", "purpose": "trailing space segment"},
            {"path": "src/pages/bad.astro ", "purpose": "trailing space"},
            {"path": "src/pages/control\x00.astro", "purpose": "control"},
            {"path": ["src", "pages", "list.astro"], "purpose": "list"},
            {"path": {"file": "src/pages/object.astro"}, "purpose": "object"},
            {"path": 123, "purpose": "number"},
            {"path": "src/pages/index.astro", "purpose": "home"},
        ],
    }))
    agent = ArchitectAgent(event_bus=EventBus(), llm=llm)  # type: ignore[arg-type]

    result = await agent.execute(TaskRequest(
        type="architecture",
        payload={"brief": "Astro brochure", "stack": "astro"},
    ))

    files = result.output["plan"]["files"]
    paths = [item["path"] for item in files]
    assert paths == [
        "package.json",
        "tsconfig.json",
        "jsconfig.json",
        "src/pages/about.astro",
        "src/pages/contact.astro",
        "src/pages/index.astro",
    ]
    assert files[0]["purpose"] == "package alias"
    assert result.output["plan"]["build_order"] == paths
