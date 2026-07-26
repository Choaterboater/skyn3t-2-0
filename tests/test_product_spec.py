from __future__ import annotations

import json

import pytest

from skyn3t.studio.product_spec import (
    BacklogRecord,
    ComponentRefRecord,
    ProductSpecConflictError,
    ProductSpecStore,
    ProductSpecV1,
    ProductSpecValidationError,
    RequirementRecord,
    ResearchSourceRecord,
    deterministic_backlog_id,
    deterministic_requirement_id,
)


def _spec() -> ProductSpecV1:
    return ProductSpecV1(
        project_id="weather-desk",
        goal="Show a useful local weather dashboard",
        personas=["commuter"],
        requirements=[
            RequirementRecord(
                text="Show the current temperature",
                acceptance_ids=["accept-current-temperature"],
            )
        ],
        non_goals=["Long-range climate modelling"],
        architecture_decisions=["Keep the weather provider behind a typed adapter"],
        backlog=[BacklogRecord(title="Add severe-weather alerts", source="brief")],
        research_sources=[
            ResearchSourceRecord(
                url="https://github.com/example/weather-ui",
                repository="example/weather-ui",
                commit="abc123",
                license="MIT",
                retrieved_at="2026-07-25T12:00:00+00:00",
                ideas=["Use a compact condition summary"],
                usage_policy="patterns_allowed",
            )
        ],
        component_refs=[
            ComponentRefRecord(
                name="ForecastCard",
                source="portfolio",
                path="src/components/ForecastCard.tsx",
            )
        ],
        regression_seals=["accept-current-temperature"],
    )


def test_deterministic_ids_are_stable_and_normalized() -> None:
    assert deterministic_requirement_id("  Show CURRENT temperature\n") == (
        deterministic_requirement_id("show current temperature")
    )
    assert deterministic_backlog_id("Add alerts", "Push and email") == (
        deterministic_backlog_id(" add  ALERTS ", "push and EMAIL")
    )

    first = RequirementRecord(text="Show current temperature")
    second = RequirementRecord(text="  SHOW current temperature ")
    assert first.id == second.id
    assert first.id.startswith("req-")

    backlog = BacklogRecord(title="Add alerts", description="Push and email")
    assert backlog.id.startswith("backlog-")


def test_product_contract_prompt_block_is_bounded_and_labels_backlog_optional() -> None:
    from skyn3t.studio.product_spec import product_contract_prompt_block

    spec = ProductSpecV1(
        project_id="prompt-contract",
        goal="Help dispatchers coordinate field work",
        personas=["dispatcher", "field technician"],
        requirements=[
            RequirementRecord(text="Show assigned work with offline status")
        ],
        non_goals=["Never dispatch work without confirmation"],
        architecture_decisions=["Keep offline state behind a local adapter"],
        backlog=[
            BacklogRecord(
                title=f"Research idea {index}: " + ("optional detail " * 20),
                source="github_research",
            )
            for index in range(20)
        ],
    )

    block = product_contract_prompt_block(spec, max_chars=1600)

    assert len(block) <= 1600
    assert "CURRENT PRODUCT CONTRACT" in block
    assert "Show assigned work with offline status" in block
    assert "Never dispatch work without confirmation" in block
    assert "Keep offline state behind a local adapter" in block
    assert "OPTIONAL RESEARCH BACKLOG" in block
    assert "never treat as current requirements" in block


def test_product_contract_prompt_includes_only_opted_in_evidence_ids() -> None:
    from skyn3t.studio.product_spec import product_contract_prompt_block

    legacy = ProductSpecV1(
        project_id="legacy-contract",
        requirements=[
            RequirementRecord(
                text="Render the dashboard",
                acceptance_ids=["ui:route:/dashboard"],
            )
        ],
    )
    strict = ProductSpecV1.from_dict(
        {
            **legacy.to_dict(),
            "project_id": "strict-contract",
            "acceptance_registry_version": 1,
        }
    )

    assert "ui:route:/dashboard" not in product_contract_prompt_block(legacy)
    strict_prompt = product_contract_prompt_block(strict)
    assert "Required evidence: ui:route:/dashboard" in strict_prompt


def test_from_dict_applies_backward_safe_defaults_but_rejects_invalid_types() -> None:
    loaded = ProductSpecV1.from_dict(
        {
            "project_id": "legacy-project",
            "goal": "Keep the existing project useful",
            "requirements": [{"text": "Retain saved data"}],
        }
    )

    assert loaded.schema_version == 1
    assert loaded.version == 1
    assert loaded.acceptance_registry_version is None
    assert loaded.personas == []
    assert loaded.backlog == []
    assert loaded.requirements[0].id == deterministic_requirement_id("Retain saved data")

    with pytest.raises(ProductSpecValidationError, match="personas"):
        ProductSpecV1.from_dict({"project_id": "bad", "goal": "Bad data", "personas": "everyone"})

    with pytest.raises(ProductSpecValidationError, match="unknown field"):
        ProductSpecV1.from_dict({"project_id": "bad", "goal": "Bad data", "surprise": True})

    duplicate_id = deterministic_requirement_id("One")
    with pytest.raises(ProductSpecValidationError, match="duplicate requirement id"):
        ProductSpecV1.from_dict(
            {
                "project_id": "bad",
                "goal": "Bad data",
                "requirements": [
                    {"id": duplicate_id, "text": "One"},
                    {"id": duplicate_id, "text": "Two"},
                ],
            }
        )


def test_acceptance_registry_v1_is_an_explicit_versioned_opt_in() -> None:
    opted_in = ProductSpecV1.from_dict(
        {
            "project_id": "strict-project",
            "goal": "Bind requirements to final evidence",
            "acceptance_registry_version": 1,
            "requirements": [
                {
                    "text": "The final build passes",
                    "acceptance_ids": ["proof:build"],
                }
            ],
        }
    )

    assert opted_in.acceptance_registry_version == 1
    assert opted_in.to_dict()["acceptance_registry_version"] == 1

    with pytest.raises(
        ProductSpecValidationError,
        match="acceptance_registry_version",
    ):
        ProductSpecV1.from_dict(
            {
                "project_id": "future-project",
                "acceptance_registry_version": 2,
            }
        )
    with pytest.raises(
        ProductSpecValidationError,
        match="acceptance_registry_version",
    ):
        ProductSpecV1.from_dict(
            {
                "project_id": "ambiguous-project",
                "acceptance_registry_version": True,
            }
        )


def test_product_spec_saves_atomically_under_dot_skyn3t_and_round_trips(tmp_path) -> None:
    spec = _spec()

    path = spec.save(tmp_path)

    assert path == tmp_path / ".skyn3t" / "product.json"
    assert ProductSpecV1.load(tmp_path) == spec
    assert not list((tmp_path / ".skyn3t").glob("*.tmp"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["requirements"][0]["acceptance_ids"] == ["accept-current-temperature"]


def test_improve_requires_matching_base_version_and_retains_history_and_provenance() -> None:
    original = _spec()

    improved = original.improve(
        {
            "goal": "Show a useful local weather dashboard with saved locations",
            "backlog": [
                *[item.to_dict() for item in original.backlog],
                {"title": "Add saved locations", "source": "user"},
            ],
        },
        base_version=1,
        actor="codex-cli",
        reason="User requested saved locations",
        provenance={"build_id": "build-123", "route": "codex-cli"},
    )

    assert original.version == 1
    assert original.goal == "Show a useful local weather dashboard"
    assert improved.version == 2
    assert improved.requirements == original.requirements
    assert improved.research_sources == original.research_sources
    assert len(improved.history) == 1
    revision = improved.history[0]
    assert revision.base_version == 1
    assert revision.version == 2
    assert revision.actor == "codex-cli"
    assert revision.changed_fields == ["backlog", "goal"]
    assert revision.previous_values["goal"] == original.goal
    assert revision.provenance["build_id"] == "build-123"

    with pytest.raises(ProductSpecConflictError) as conflict:
        original.improve({"goal": "Wrong base"}, base_version=7)
    assert conflict.value.expected_version == 7
    assert conflict.value.actual_version == 1


def test_store_update_uses_optimistic_version_and_persists_new_revision(tmp_path) -> None:
    store = ProductSpecStore(tmp_path)
    store.create(_spec())

    updated = store.update(
        base_version=1,
        patch={"personas": ["commuter", "cyclist"]},
        actor="studio-gui",
        reason="Persona refinement",
    )

    assert updated.version == 2
    assert store.load() == updated

    with pytest.raises(ProductSpecConflictError):
        store.update(
            base_version=1,
            patch={"goal": "A stale editor must not overwrite version two"},
        )
    assert store.load() == updated


def test_record_research_adds_sources_and_optional_ideas_without_changing_requirements() -> None:
    original = _spec()
    source = ResearchSourceRecord(
        url="https://github.com/example/alerts",
        repository="example/alerts",
        commit="def456",
        license="unknown",
        retrieved_at="2026-07-25T13:00:00+00:00",
        ideas=["Make alert urgency visually distinct"],
        usage_policy="idea_only",
    )
    idea = BacklogRecord(
        title="Make alert urgency visually distinct",
        source="github_research",
        source_refs=[source.id],
    )

    researched = original.record_research(
        sources=[source],
        backlog=[idea],
        base_version=1,
        provenance={"query": "weather alert dashboard"},
    )

    assert researched.version == 2
    assert researched.requirements == original.requirements
    assert source in researched.research_sources
    assert idea in researched.backlog
    assert researched.history[-1].actor == "similarity-scout"


def test_store_records_research_atomically_and_rejects_a_stale_base(tmp_path) -> None:
    store = ProductSpecStore(tmp_path)
    original = store.create(_spec())
    source = ResearchSourceRecord(
        url="https://github.com/example/keyboard-navigation",
        repository="example/keyboard-navigation",
        commit="abc789",
        license="MIT",
        retrieved_at="2026-07-25T14:00:00+00:00",
        ideas=["Add a keyboard shortcut overlay"],
        usage_policy="patterns_allowed",
    )
    idea = BacklogRecord(
        title="Add a keyboard shortcut overlay",
        source="github_research",
        source_refs=[source.id],
    )

    updated = store.record_research(
        base_version=original.version,
        sources=[source],
        backlog=[idea],
        provenance={"requirements_modified": False},
    )

    assert updated.version == original.version + 1
    assert updated.requirements == original.requirements
    assert updated.research_sources[-1] == source
    assert store.load() == updated

    with pytest.raises(ProductSpecConflictError):
        store.record_research(
            base_version=original.version,
            sources=[source],
            backlog=[idea],
        )
