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
    assert llm.calls[0]["max_tokens"] == 12_000
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
