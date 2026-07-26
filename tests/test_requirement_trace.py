from __future__ import annotations

import copy
import json

import pytest

from skyn3t.studio.gate_verdict import GateVerdict
from skyn3t.studio.headless_gate import HeadlessGateResult
from skyn3t.studio.preview_supervisor import ProofLadderResult, ProofStep
from skyn3t.studio.product_spec import ProductSpecV1, RequirementRecord
from skyn3t.studio.proof_run import ProofResult
from skyn3t.studio.requirement_trace import (
    ACCEPTANCE_REGISTRY_V1,
    MAX_ACCEPTANCE_IDS_PER_REQUIREMENT,
    MAX_DYNAMIC_EVIDENCE_RECORDS,
    MAX_REQUIREMENTS,
    REQUIREMENT_CONTRACT_DIGEST_ALGORITHM,
    REQUIREMENT_EVIDENCE_DIGEST_ALGORITHM,
    REQUIREMENT_TRACE_COMPILER,
    REQUIREMENT_TRACE_SCHEMA_VERSION,
    RequirementTraceValidationError,
    compile_requirement_trace,
    requirement_contract_sha256,
    requirement_evidence_binding,
)
from skyn3t.studio.visual_proof import ResponsiveVisualProof, ViewportProof

COMPILED_AT = "2026-07-25T18:00:00+00:00"
BUILD_ID = "build-123"
EVIDENCE_RUN_ID = "evidence-run-123"


def _snapshot(
    sha256: str = "a" * 64,
    *,
    valid: bool = True,
    algorithm: str = "source-tree-sha256-v1",
) -> dict[str, object]:
    return {
        "algorithm": algorithm,
        "sha256": sha256 if valid else "",
        "valid": valid,
        "file_count": 4,
        "byte_count": 512,
        "files": ["index.html"],
    }


def _runtime(
    sha256: str = "c" * 64,
    *,
    algorithm: str = "preview-input-sha256-v1",
) -> dict[str, object]:
    return {
        "algorithm": algorithm,
        "sha256": sha256,
        "file_count": 7,
        "byte_count": 1024,
    }


def _requirement(
    text: str,
    *acceptance_ids: str,
    priority: str = "must",
    status: str = "planned",
) -> RequirementRecord:
    return RequirementRecord(
        id=f"req-{text.lower().replace(' ', '-')}",
        text=text,
        acceptance_ids=list(acceptance_ids),
        priority=priority,
        status=status,
    )


def _product(*requirements: RequirementRecord, **overrides: object) -> ProductSpecV1:
    values: dict[str, object] = {
        "project_id": "weather-lab",
        "goal": "Build a weather lab",
        "requirements": list(requirements),
        "version": 3,
    }
    values.update(overrides)
    return ProductSpecV1(**values)


def _compile(
    product: ProductSpecV1 | dict[str, object],
    extra: dict[str, object],
    *,
    current: dict[str, object] | None = None,
    binding: dict[str, object] | None | object = ...,
    acceptance_registry: str | None = ACCEPTANCE_REGISTRY_V1,
    build_id: str | None = BUILD_ID,
    evidence_run_id: str | None = EVIDENCE_RUN_ID,
    current_runtime: dict[str, object] | None | object = ...,
    bound_runtime: dict[str, object] | None | object = ...,
) -> dict[str, object]:
    source = current or _snapshot()
    runtime = _runtime() if current_runtime is ... else current_runtime
    runtime_to_bind = runtime if bound_runtime is ... else bound_runtime
    if binding is ... and acceptance_registry == ACCEPTANCE_REGISTRY_V1:
        resolved_binding = requirement_evidence_binding(
            product,
            extra,
            source,
            acceptance_registry=acceptance_registry,
            build_id=build_id,
            evidence_run_id=evidence_run_id,
            runtime_input_fingerprint=runtime_to_bind,
        )
    else:
        resolved_binding = None if binding is ... else binding
    return compile_requirement_trace(
        product,
        extra,
        source,
        acceptance_registry=acceptance_registry,
        evidence_binding=resolved_binding,
        build_id=build_id,
        evidence_run_id=evidence_run_id,
        current_runtime_input_fingerprint=runtime,
        compiled_at=COMPILED_AT,
    )


def _proof(**detail: object) -> dict[str, object]:
    return {
        "passed": True,
        "mode": "sandbox",
        "detail": detail,
    }


def _binding(
    product: ProductSpecV1 | dict[str, object],
    extra: dict[str, object],
    source: dict[str, object] | None = None,
    *,
    runtime: dict[str, object] | None = None,
    build_id: str = BUILD_ID,
    evidence_run_id: str = EVIDENCE_RUN_ID,
) -> dict[str, object]:
    return requirement_evidence_binding(
        product,
        extra,
        source or _snapshot(),
        acceptance_registry=ACCEPTANCE_REGISTRY_V1,
        build_id=build_id,
        evidence_run_id=evidence_run_id,
        runtime_input_fingerprint=runtime or _runtime(),
    )


def _ladder(*steps: ProofStep) -> dict[str, object]:
    result = ProofLadderResult(
        project_dir="/tmp/generated-project",
        stack="react_vite",
        artifact_dir="/tmp/generated-project/.skyn3t/proof-ladder",
        run_id=EVIDENCE_RUN_ID,
        steps=list(steps),
    )
    result.finalize()
    result.report_path = (
        "/tmp/generated-project/.skyn3t/proof-ladder/proof-ladder.json"
    )
    return result.to_dict()


def _route_ladder(*proofs: ResponsiveVisualProof) -> dict[str, object]:
    routes = [proof.route for proof in proofs]
    return _ladder(
        ProofStep(
            "playwright",
            "passed",
            True,
            detail={
                "routes": routes,
                "proofs": [proof.to_dict() for proof in proofs],
            },
        )
    )


def _passing_route(route: str = "/") -> ResponsiveVisualProof:
    return ResponsiveVisualProof(
        url=f"http://127.0.0.1:4173{route}",
        route=route,
        stack="react_vite",
        passed=True,
        skipped=False,
        report_path=f"playwright/{route.strip('/') or 'index'}.json",
        viewports=[
            ViewportProof(
                name="desktop",
                width=1440,
                height=900,
                passed=True,
                screenshot=f"playwright/{route.strip('/') or 'index'}/desktop.png",
                metrics={},
            ),
            ViewportProof(
                name="mobile",
                width=390,
                height=844,
                passed=True,
                screenshot=f"playwright/{route.strip('/') or 'index'}/mobile.png",
                metrics={},
            ),
        ],
    )


def _requirement_by_text(trace: dict[str, object], text: str) -> dict[str, object]:
    return next(
        requirement
        for requirement in trace["requirements"]  # type: ignore[union-attr]
        if requirement["text"] == text
    )


def test_contract_hash_is_canonical_and_ignores_non_requirement_product_state():
    requirement = _requirement("Show forecast", "proof:build", "ui:route:/forecast")
    product = _product(requirement)
    product_dict = product.to_dict()
    reordered = {
        "updated_at": "2099-01-01T00:00:00+00:00",
        "backlog": [{"anything": "ignored"}],
        "goal": "A rewritten product goal",
        "version": 999,
        "requirements": [
            {
                "acceptance_ids": ["proof:build", "ui:route:/forecast"],
                "status": "planned",
                "priority": "must",
                "text": "Show forecast",
                "id": requirement.id,
                "ignored": "not acceptance relevant",
            }
        ],
        "project_id": "a-different-project-id",
    }

    assert requirement_contract_sha256(product) == requirement_contract_sha256(product_dict)
    assert requirement_contract_sha256(product) == requirement_contract_sha256(reordered)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "req-new"),
        ("text", "Show the hourly forecast"),
        ("priority", "should"),
        ("status", "deferred"),
        ("acceptance_ids", ["proof:build"]),
    ],
)
def test_contract_hash_changes_for_every_acceptance_relevant_field(field, value):
    product = _product(
        _requirement("Show forecast", "proof:build", "ui:route:/forecast")
    ).to_dict()
    changed = copy.deepcopy(product)
    changed["requirements"][0][field] = value

    assert requirement_contract_sha256(product) != requirement_contract_sha256(changed)


def test_evidence_binding_is_compact_and_bound_to_contract_and_source():
    product = _product(_requirement("Build", "proof:build"))
    extra = {"proof": _proof(build="passed")}

    binding = _binding(product, extra)

    assert binding["schema_version"] == 1
    assert binding["acceptance_registry"] == ACCEPTANCE_REGISTRY_V1
    assert binding["build_id"] == BUILD_ID
    assert binding["evidence_run_id"] == EVIDENCE_RUN_ID
    assert binding["requirements_algorithm"] == REQUIREMENT_CONTRACT_DIGEST_ALGORITHM
    assert binding["requirements_sha256"] == requirement_contract_sha256(product)
    assert binding["source_tree"] == {
        "algorithm": "source-tree-sha256-v1",
        "sha256": "a" * 64,
        "valid": True,
        "file_count": 4,
        "byte_count": 512,
    }
    assert binding["runtime_input_fingerprint"] == _runtime()
    assert binding["evidence_projection"] == {
        "algorithm": REQUIREMENT_EVIDENCE_DIGEST_ALGORITHM,
        "sha256": binding["evidence_projection"]["sha256"],
        "record_count": 1,
    }
    assert len(binding["evidence_projection"]["sha256"]) == 64
    assert binding != {
        "requirements_algorithm": REQUIREMENT_CONTRACT_DIGEST_ALGORITHM,
        "requirements_sha256": requirement_contract_sha256(product),
        "source_tree": {
            "algorithm": "source-tree-sha256-v1",
            "sha256": "a" * 64,
            "valid": True,
            "file_count": 4,
            "byte_count": 512,
        },
    }


def test_legacy_empty_acceptance_ids_stay_visible_and_never_false_pass():
    product = _product(_requirement("Show current temperature"))

    trace = _compile(product, {"proof": _proof(build="passed")}, binding=None)

    requirement = trace["requirements"][0]
    assert trace["mode"] == "legacy_advisory"
    assert trace["status"] == "unbound"
    assert trace["fresh"] is False
    assert trace["blocks_delivery"] is False
    assert trace["summary"]["must_unbound"] == 1
    assert requirement["status"] == "unbound"
    assert requirement["blocking"] is False
    assert requirement["bindings"] == []


def test_partial_mode_enforces_explicit_musts_but_keeps_legacy_musts_advisory():
    product = _product(
        _requirement("Compile", "proof:build"),
        _requirement("Show current temperature"),
    )

    trace = _compile(product, {"proof": _proof(build="passed")})

    compile_requirement = _requirement_by_text(trace, "Compile")
    legacy_requirement = _requirement_by_text(trace, "Show current temperature")
    assert trace["mode"] == "partial"
    assert trace["status"] == "unbound"
    assert trace["blocks_delivery"] is False
    assert compile_requirement["status"] == "proven"
    assert compile_requirement["blocking"] is False
    assert legacy_requirement["status"] == "unbound"
    assert legacy_requirement["blocking"] is False


def test_partial_mode_explicit_failure_is_visible_but_display_only():
    product = _product(
        _requirement("Compile", "proof:build"),
        _requirement("Legacy behavior"),
    )

    trace = _compile(product, {"proof": _proof(build="failed")})

    assert trace["mode"] == "partial"
    assert trace["status"] == "failed"
    assert trace["blocks_delivery"] is False
    assert trace["go_eligible"] is False
    assert trace["summary"]["blocking_failed"] == 0


def test_enforced_mode_uses_and_semantics_and_skipped_is_not_passing():
    product = _product(
        _requirement("Build and test", "proof:build", "proof:python-tests")
    )

    trace = _compile(
        product,
        {"proof": _proof(build="passed", tests="skipped")},
    )

    requirement = trace["requirements"][0]
    assert trace["mode"] == "enforced"
    assert trace["status"] == "failed"
    assert trace["blocks_delivery"] is True
    assert [binding["status"] for binding in requirement["bindings"]] == [
        "passed",
        "skipped",
    ]
    assert requirement["status"] == "failed"
    assert trace["evidence"]["proof:python-tests"]["status"] == "skipped"


def test_missing_should_runtime_evidence_does_not_stale_passing_must_proof():
    product = _product(
        _requirement("Compile", "proof:build"),
        _requirement(
            "Offer an optional dashboard",
            "ui:route:/dashboard",
            priority="should",
        ),
    )
    extra = {"proof": _proof(build="passed")}
    source = _snapshot()
    binding = requirement_evidence_binding(
        product,
        extra,
        source,
        acceptance_registry=ACCEPTANCE_REGISTRY_V1,
        build_id=BUILD_ID,
        evidence_run_id=EVIDENCE_RUN_ID,
        runtime_input_fingerprint=None,
    )

    trace = compile_requirement_trace(
        product,
        extra,
        source,
        acceptance_registry=ACCEPTANCE_REGISTRY_V1,
        evidence_binding=binding,
        build_id=BUILD_ID,
        evidence_run_id=EVIDENCE_RUN_ID,
        current_runtime_input_fingerprint=None,
        compiled_at=COMPILED_AT,
    )

    assert trace["fresh"] is True
    assert trace["go_eligible"] is True
    assert trace["blocks_delivery"] is False
    assert _requirement_by_text(trace, "Compile")["status"] == "proven"
    assert _requirement_by_text(trace, "Offer an optional dashboard")[
        "status"
    ] == "failed"
    assert trace["evidence"]["ui:route:/dashboard"]["status"] == "missing"


def test_unknown_and_known_but_missing_acceptance_ids_fail_closed_without_inference():
    product = _product(
        _requirement(
            "Show current temperature",
            "accept-current-temperature",
            "gate:mcp",
        )
    )

    trace = _compile(product, {})

    requirement = trace["requirements"][0]
    assert requirement["status"] == "failed"
    assert [binding["status"] for binding in requirement["bindings"]] == [
        "missing",
        "missing",
    ]
    assert "unknown acceptance id" in trace["evidence"]["accept-current-temperature"]["reason"]
    assert trace["evidence"]["gate:mcp"]["source"] == "manifest.extra.mcp_check"


def test_changed_source_tree_makes_observed_pass_stale_and_blocking():
    product = _product(_requirement("Compile", "proof:build"))
    extra = {"proof": _proof(build="passed")}
    old_source = _snapshot("a" * 64)
    current_source = _snapshot("b" * 64)

    trace = _compile(
        product,
        extra,
        current=current_source,
        binding=_binding(product, extra, old_source),
    )

    binding = trace["requirements"][0]["bindings"][0]
    assert trace["fresh"] is False
    assert trace["freshness_reason"] == "source tree sha256 does not match evidence binding"
    assert trace["status"] == "stale"
    assert trace["blocks_delivery"] is True
    assert binding["status"] == "stale"
    assert trace["evidence"]["proof:build"]["observed_status"] == "passed"


def test_matching_but_unsupported_source_digest_algorithms_fail_closed():
    product = _product(_requirement("Compile", "proof:build"))
    extra = {"proof": _proof(build="passed")}
    source = _snapshot(algorithm="made-up-sha256-v9")

    forged_binding = _binding(product, extra)
    forged_binding["source_tree"] = source
    trace = _compile(product, extra, current=source, binding=forged_binding)

    assert trace["fresh"] is False
    assert (
        trace["freshness_reason"]
        == "current source tree snapshot digest algorithm is unsupported"
    )
    assert trace["status"] == "stale"
    assert trace["blocks_delivery"] is True


def test_changed_requirement_contract_invalidates_otherwise_matching_evidence():
    original = _product(_requirement("Compile", "proof:build"))
    changed = _product(_requirement("Compile safely", "proof:build"))
    extra = {"proof": _proof(build="passed")}
    source = _snapshot()

    trace = _compile(
        changed,
        extra,
        current=source,
        binding=_binding(original, extra, source),
    )

    assert trace["fresh"] is False
    assert trace["freshness_reason"] == "requirements sha256 does not match evidence binding"
    assert trace["status"] == "stale"
    assert trace["blocks_delivery"] is True


def test_invalid_or_incomplete_binding_never_passes():
    product = _product(_requirement("Compile", "proof:build"))
    extra = {"proof": _proof(build="passed")}
    invalid_source_binding = _binding(product, extra)
    invalid_source_binding["source_tree"] = {
        "algorithm": "source-tree-sha256-v1",
        "sha256": "",
        "valid": False,
        "file_count": 4,
        "byte_count": 512,
    }
    invalid_requirements_binding = _binding(product, extra)
    invalid_requirements_binding["requirements_sha256"] = ""
    cases = [
        (
            _snapshot(valid=False),
            None,
            "current source tree snapshot is invalid",
        ),
        (
            _snapshot(),
            invalid_source_binding,
            "evidence source tree snapshot is invalid",
        ),
        (
            _snapshot(),
            invalid_requirements_binding,
            "evidence binding requirements sha256 is invalid",
        ),
    ]

    for current, binding, reason in cases:
        trace = _compile(product, extra, current=current, binding=binding)
        assert trace["fresh"] is False
        assert trace["blocks_delivery"] is True
        assert trace["freshness_reason"] == reason


def test_non_must_failure_and_deferred_must_are_nonblocking():
    product = _product(
        _requirement("Core build", "proof:build"),
        _requirement("Optional lint", "proof:ruff", priority="should"),
        _requirement("Old route", "ui:route:/old", status="deferred"),
    )

    trace = _compile(
        product,
        {"proof": _proof(build="passed", ruff="failed")},
    )

    optional = _requirement_by_text(trace, "Optional lint")
    deferred = _requirement_by_text(trace, "Old route")
    assert trace["mode"] == "enforced"
    assert trace["status"] == "passed"
    assert trace["blocks_delivery"] is False
    assert optional["status"] == "failed"
    assert optional["blocking"] is False
    assert deferred["status"] == "not_applicable"
    assert deferred["blocking"] is False
    assert deferred["bindings"] == []
    assert trace["summary"]["must_total"] == 1


@pytest.mark.parametrize(
    ("acceptance_id", "detail_key"),
    [
        ("proof:build", "build"),
        ("proof:python-tests", "tests"),
        ("proof:node-tests", "node_tests"),
        ("proof:swift-tests", "swift_tests"),
        ("proof:ruff", "ruff"),
    ],
)
def test_exact_proof_detail_registry_maps_passed_statuses(acceptance_id, detail_key):
    product = _product(_requirement("Objective check", acceptance_id))

    trace = _compile(product, {"proof": _proof(**{detail_key: "passed"})})

    assert trace["requirements"][0]["status"] == "proven"
    assert trace["evidence"][acceptance_id]["status"] == "passed"


def test_overall_entrypoint_and_stack_artifact_have_strict_evidence_rules():
    product = _product(
        _requirement(
            "Runnable stack",
            "proof:overall",
            "proof:entrypoint",
            "proof:stack-artifact",
        )
    )
    manifest = {
        "proof": _proof(
            entrypoints=["index.html"],
            stack_check="generic",
        )
    }

    trace = _compile(product, manifest)

    statuses = [
        binding["status"] for binding in trace["requirements"][0]["bindings"]
    ]
    assert statuses == ["passed", "passed", "skipped"]
    assert trace["status"] == "failed"
    assert trace["blocks_delivery"] is True


def test_headless_non_applicable_and_skipped_gate_never_count_as_passing():
    product = _product(
        _requirement("Game is playable", "gate:headless", "gate:qa-playtest")
    )
    manifest = {
        "headless_gate": HeadlessGateResult(
            applicable=False,
            passed=True,
        ).to_dict(),
        "qa_playtest": GateVerdict(skipped=True).to_dict(),
    }

    trace = _compile(product, manifest)

    assert [
        binding["status"] for binding in trace["requirements"][0]["bindings"]
    ] == ["skipped", "skipped"]
    assert trace["blocks_delivery"] is True


@pytest.mark.parametrize(
    ("acceptance_id", "manifest_key"),
    [
        ("gate:qa-playtest", "qa_playtest"),
        ("gate:mcp", "mcp_check"),
        ("gate:rag", "rag_check"),
        ("gate:workflow", "workflow_check"),
        ("gate:cli", "cli_check"),
        ("gate:cli-playtest", "cli_playtest"),
    ],
)
def test_gate_registry_requires_explicit_not_skipped_ok(acceptance_id, manifest_key):
    product = _product(_requirement("Gate contract", acceptance_id))

    passing = _compile(product, {manifest_key: GateVerdict().to_dict()})
    skipped = _compile(
        product,
        {manifest_key: GateVerdict(skipped=True, reason="not applicable").to_dict()},
    )
    failing = _compile(
        product,
        {manifest_key: GateVerdict(issues=["gate failed"]).to_dict()},
    )

    assert passing["requirements"][0]["status"] == "proven"
    assert skipped["evidence"][acceptance_id]["status"] == "skipped"
    assert failing["evidence"][acceptance_id]["status"] == "failed"


def test_passing_gate_payload_cannot_hide_issues_or_gaps():
    product = _product(_requirement("Gate contract", "gate:workflow"))
    contradictory = GateVerdict().to_dict()
    contradictory["issues"] = ["hidden failure"]
    contradictory["gaps"] = ["hidden failure"]

    trace = _compile(product, {"workflow_check": contradictory})

    assert trace["evidence"]["gate:workflow"]["status"] == "failed"
    assert trace["requirements"][0]["status"] == "failed"
    assert trace["go_eligible"] is False


def test_passing_headless_payload_cannot_hide_violations():
    product = _product(_requirement("Simulation is sound", "gate:headless"))
    contradictory = HeadlessGateResult(
        applicable=True,
        passed=True,
        violations=["hidden invariant failure"],
    ).to_dict()

    trace = _compile(product, {"headless_gate": contradictory})

    assert trace["evidence"]["gate:headless"]["status"] == "failed"
    assert trace["requirements"][0]["status"] == "failed"
    assert trace["go_eligible"] is False


def test_route_binding_is_exact_and_uses_individual_visual_proof_not_ladder_vibes():
    product = _product(
        _requirement("Forecast route", "ui:route:/forecast"),
        _requirement("Settings route", "ui:route:/settings"),
    )
    manifest = {
        "proof_ladder": _route_ladder(_passing_route("/forecast"))
    }

    trace = _compile(product, manifest)

    assert _requirement_by_text(trace, "Forecast route")["status"] == "proven"
    assert _requirement_by_text(trace, "Settings route")["status"] == "failed"
    assert trace["evidence"]["ui:route:/settings"]["status"] == "missing"


def test_route_normalization_is_syntactic_only_and_does_not_use_requirement_text():
    product = _product(
        _requirement("This prose mentions settings", "ui:route:settings")
    )
    manifest = {
        "proof_ladder": _route_ladder(_passing_route("/settings"))
    }

    trace = _compile(product, manifest)

    assert trace["requirements"][0]["status"] == "proven"


def test_maestro_flow_binding_requires_an_exact_execution_record():
    product = _product(
        _requirement("Login flow", "mobile:maestro:.maestro/login.yaml"),
        _requirement("Purchase flow", "mobile:maestro:.maestro/purchase.yaml"),
    )
    ladder = ProofLadderResult(
        project_dir="/tmp/mobile-project",
        stack="react_native",
        artifact_dir="/tmp/mobile-project/.skyn3t/proof-ladder",
        run_id=EVIDENCE_RUN_ID,
        steps=[
            ProofStep(
                "maestro",
                "passed",
                True,
                detail={
                        "flows": [".maestro/login.yaml"],
                        "executions": [
                            {
                                "flow": ".maestro/login.yaml",
                                "passed": True,
                                "returncode": 0,
                                "timed_out": False,
                                "artifact_written": True,
                                "junit": "maestro/login.xml",
                                "artifact_dir": "maestro/login-artifacts",
                            },
                        ],
                },
            )
        ],
    )
    ladder.finalize()
    ladder.report_path = (
        "/tmp/mobile-project/.skyn3t/proof-ladder/proof-ladder.json"
    )
    manifest = {"proof_ladder": ladder.to_dict()}

    trace = _compile(product, manifest)

    assert _requirement_by_text(trace, "Login flow")["status"] == "proven"
    assert _requirement_by_text(trace, "Purchase flow")["status"] == "failed"
    assert (
        trace["evidence"]["mobile:maestro:.maestro/purchase.yaml"]["status"]
        == "missing"
    )


def test_cli_playtest_scenario_binding_is_exact():
    product = _product(
        _requirement("Happy CLI path", "gate:cli-playtest:happy-path"),
        _requirement("Missing CLI path", "gate:cli-playtest:not-authored"),
    )
    manifest = {
        "cli_playtest": {
            "ok": True,
            "skipped": False,
            "checked": {
                "scenarios": [
                    {"name": "happy-path", "status": "passed"},
                ]
            },
        }
    }

    trace = _compile(product, manifest)

    assert _requirement_by_text(trace, "Happy CLI path")["status"] == "proven"
    assert _requirement_by_text(trace, "Missing CLI path")["status"] == "failed"
    assert (
        trace["evidence"]["gate:cli-playtest:not-authored"]["status"] == "missing"
    )


def test_output_is_compact_json_serializable_and_does_not_mutate_inputs():
    product = _product(_requirement("Compile", "proof:build"))
    manifest = {
        "proof": {
            **_proof(build="passed", build_summary="x" * 10_000),
            "missing": ["huge-detail"],
        }
    }
    original_manifest = copy.deepcopy(manifest)
    source = _snapshot()
    original_source = copy.deepcopy(source)

    trace = _compile(product, manifest, current=source)
    serialized = json.dumps(trace, sort_keys=True)

    assert manifest == original_manifest
    assert source == original_source
    assert len(serialized) < 6_000
    assert "x" * 100 not in serialized
    assert trace["schema_version"] == REQUIREMENT_TRACE_SCHEMA_VERSION
    assert trace["compiler"] == REQUIREMENT_TRACE_COMPILER
    assert trace["compiled_at"] == COMPILED_AT
    assert "files" not in trace["source_tree"]


def test_compile_rejects_invalid_programmer_inputs():
    product = _product(_requirement("Compile", "proof:build"))
    extra = {"proof": _proof(build="passed")}
    binding = _binding(product, extra)

    with pytest.raises(ValueError, match="compiled_at"):
        compile_requirement_trace(
            product,
            extra,
            _snapshot(),
            acceptance_registry=ACCEPTANCE_REGISTRY_V1,
            evidence_binding=binding,
            build_id=BUILD_ID,
            evidence_run_id=EVIDENCE_RUN_ID,
            current_runtime_input_fingerprint=_runtime(),
            compiled_at="",
        )
    with pytest.raises(TypeError, match="manifest_extra"):
        compile_requirement_trace(
            product,
            [],  # type: ignore[arg-type]
            _snapshot(),
            acceptance_registry=ACCEPTANCE_REGISTRY_V1,
            evidence_binding=binding,
            build_id=BUILD_ID,
            evidence_run_id=EVIDENCE_RUN_ID,
            current_runtime_input_fingerprint=_runtime(),
            compiled_at=COMPILED_AT,
        )


def test_acceptance_registry_requires_explicit_opt_in_for_legacy_opaque_ids():
    product = _product(
        _requirement(
            "Existing contract",
            "accept-current-temperature",
            "proof:build",
        )
    )

    trace = _compile(
        product,
        {"proof": _proof(build="passed")},
        acceptance_registry=None,
        build_id=None,
        evidence_run_id=None,
        binding=None,
        current_runtime=None,
    )

    requirement = trace["requirements"][0]
    assert trace["acceptance_registry"] is None
    assert trace["mode"] == "legacy_advisory"
    assert trace["status"] == "unbound"
    assert trace["blocks_delivery"] is False
    assert trace["fresh"] is False
    assert trace["freshness_reason"] == "acceptance registry is not enabled"
    assert requirement["acceptance_ids"] == [
        "accept-current-temperature",
        "proof:build",
    ]
    assert requirement["bindings"] == []
    assert requirement["status"] == "unbound"


def test_registry_v1_unknown_and_reserved_typos_fail_closed():
    product = _product(
        _requirement(
            "Strict contract",
            "accept-current-temperature",
            "proof:buid",
        )
    )

    trace = _compile(product, {"proof": _proof(build="passed")})

    assert trace["acceptance_registry"] == ACCEPTANCE_REGISTRY_V1
    assert trace["requirements"][0]["status"] == "failed"
    assert trace["blocks_delivery"] is True
    assert trace["evidence"]["accept-current-temperature"]["status"] == "missing"
    assert trace["evidence"]["proof:buid"]["status"] == "missing"


def test_unknown_acceptance_registry_version_is_rejected():
    product = _product(_requirement("Compile", "proof:build"))

    with pytest.raises(RequirementTraceValidationError, match="acceptance registry"):
        _compile(
            product,
            {"proof": _proof(build="passed")},
            acceptance_registry="v2-guess",
            binding=None,
        )


def test_evidence_projection_digest_detects_pass_to_fail_tampering_and_preserves_failure():
    product = _product(_requirement("Compile", "proof:build"))
    original = {"proof": _proof(build="passed")}
    binding = _binding(product, original)
    tampered = {"proof": _proof(build="failed")}

    trace = _compile(product, tampered, binding=binding)

    assert trace["fresh"] is False
    assert trace["freshness_reason"] == "evidence projection digest does not match binding"
    assert trace["evidence"]["proof:build"]["status"] == "failed"
    assert "observed_status" not in trace["evidence"]["proof:build"]
    assert trace["status"] == "failed"


def test_evidence_projection_digest_detects_fail_to_pass_tampering_as_stale():
    product = _product(_requirement("Compile", "proof:build"))
    original = {"proof": _proof(build="failed")}
    binding = _binding(product, original)
    tampered = {"proof": _proof(build="passed")}

    trace = _compile(product, tampered, binding=binding)

    assert trace["fresh"] is False
    assert trace["evidence"]["proof:build"]["status"] == "stale"
    assert trace["evidence"]["proof:build"]["observed_status"] == "passed"
    assert trace["status"] == "stale"


def test_forged_projection_digest_and_identity_mismatch_cannot_pass():
    product = _product(_requirement("Compile", "proof:build"))
    extra = {"proof": _proof(build="passed")}
    binding = _binding(product, extra)
    binding["evidence_projection"]["sha256"] = "0" * 64

    forged = _compile(product, extra, binding=binding)
    wrong_build = _compile(
        product,
        extra,
        binding=_binding(product, extra),
        build_id="build-elsewhere",
    )
    wrong_run = _compile(
        product,
        extra,
        binding=_binding(product, extra),
        evidence_run_id="another-run",
    )

    assert forged["evidence"]["proof:build"]["status"] == "stale"
    assert forged["freshness_reason"] == "evidence projection digest does not match binding"
    assert wrong_build["freshness_reason"] == "build identity does not match evidence binding"
    assert wrong_run["freshness_reason"] == "run identity does not match evidence binding"


def test_evidence_projection_digest_covers_build_and_run_identity():
    product = _product(_requirement("Compile", "proof:build"))
    extra = {"proof": _proof(build="passed")}

    first = _binding(product, extra)
    changed_build = _binding(product, extra, build_id="build-elsewhere")
    changed_run = _binding(product, extra, evidence_run_id="run-elsewhere")

    assert (
        first["evidence_projection"]["sha256"]
        != changed_build["evidence_projection"]["sha256"]
    )
    assert (
        first["evidence_projection"]["sha256"]
        != changed_run["evidence_projection"]["sha256"]
    )


def test_binding_helper_sanitizes_identity_and_rejects_unsafe_or_invalid_fields():
    product = _product(_requirement("Compile", "proof:build"))
    extra = {"proof": _proof(build="passed")}

    binding = requirement_evidence_binding(
        product,
        extra,
        _snapshot(),
        acceptance_registry=ACCEPTANCE_REGISTRY_V1,
        build_id="  build-123  ",
        evidence_run_id="  run-123  ",
        runtime_input_fingerprint=_runtime(),
    )

    assert binding["build_id"] == "build-123"
    assert binding["evidence_run_id"] == "run-123"
    with pytest.raises(RequirementTraceValidationError, match="build_id"):
        requirement_evidence_binding(
            product,
            extra,
            _snapshot(),
            acceptance_registry=ACCEPTANCE_REGISTRY_V1,
            build_id="../escape",
            evidence_run_id=EVIDENCE_RUN_ID,
            runtime_input_fingerprint=_runtime(),
        )
    with pytest.raises(RequirementTraceValidationError, match="source_snapshot"):
        requirement_evidence_binding(
            product,
            extra,
            _snapshot("not-a-digest"),
            acceptance_registry=ACCEPTANCE_REGISTRY_V1,
            build_id=BUILD_ID,
            evidence_run_id=EVIDENCE_RUN_ID,
            runtime_input_fingerprint=_runtime(),
        )
    with pytest.raises(RequirementTraceValidationError, match="runtime"):
        requirement_evidence_binding(
            product,
            extra,
            _snapshot(),
            acceptance_registry=ACCEPTANCE_REGISTRY_V1,
            build_id=BUILD_ID,
            evidence_run_id=EVIDENCE_RUN_ID,
            runtime_input_fingerprint=_runtime(algorithm="untrusted-v9"),
        )


def test_untrusted_binding_fields_are_not_reflected_and_trace_stays_json_safe():
    product = _product(_requirement("Compile", "proof:build"))
    extra = {"proof": _proof(build="passed")}
    binding = _binding(product, extra)
    binding["unexpected"] = object()

    trace = _compile(product, extra, binding=binding)

    assert trace["fresh"] is False
    assert trace["evidence"]["proof:build"]["status"] == "stale"
    assert trace["freshness_reason"] == "evidence binding contains unsupported fields"
    assert json.loads(json.dumps(trace, allow_nan=False)) == trace


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda binding: binding["source_tree"].update({"files": ["secret"]}),
            "evidence source tree binding contains unsupported fields",
        ),
        (
            lambda binding: binding["runtime_input_fingerprint"].update(
                {"inputs": ["dist/app.js"]}
            ),
            "runtime evidence binding contains unsupported fields",
        ),
        (
            lambda binding: binding["evidence_projection"].update(
                {"record_count": True}
            ),
            "evidence projection record_count is invalid",
        ),
        (
            lambda binding: binding.update({"schema_version": True}),
            "evidence binding schema_version is invalid",
        ),
    ],
)
def test_nested_binding_fields_and_bool_as_int_forgery_fail_closed(mutate, reason):
    product = _product(_requirement("Compile", "proof:build"))
    extra = {"proof": _proof(build="passed")}
    binding = _binding(product, extra)
    mutate(binding)

    trace = _compile(product, extra, binding=binding)

    assert trace["fresh"] is False
    assert trace["freshness_reason"] == reason
    assert trace["evidence"]["proof:build"]["status"] == "stale"
    json.dumps(trace, allow_nan=False)


def test_changed_runtime_fingerprint_stales_route_evidence_even_when_source_matches():
    product = _product(_requirement("Rendered forecast", "ui:route:/forecast"))
    extra = {"proof_ladder": _route_ladder(_passing_route("/forecast"))}

    trace = _compile(
        product,
        extra,
        current_runtime=_runtime("d" * 64),
        bound_runtime=_runtime("c" * 64),
    )

    assert trace["fresh"] is False
    assert trace["freshness_reason"] == "runtime input sha256 does not match evidence binding"
    assert trace["evidence"]["ui:route:/forecast"]["status"] == "stale"
    assert trace["blocks_delivery"] is True


def test_runtime_gate_requires_runtime_binding_but_static_build_evidence_does_not():
    runtime_product = _product(_requirement("CLI works", "gate:cli"))
    runtime_extra = {"cli_check": GateVerdict().to_dict()}
    runtime_trace = _compile(
        runtime_product,
        runtime_extra,
        current_runtime=_runtime("d" * 64),
        bound_runtime=_runtime("c" * 64),
    )
    static_product = _product(_requirement("Compiles", "proof:build"))
    static_extra = {"proof": _proof(build="passed")}
    static_trace = _compile(
        static_product,
        static_extra,
        current_runtime=_runtime("d" * 64),
        bound_runtime=_runtime("c" * 64),
    )

    assert runtime_trace["evidence"]["gate:cli"]["status"] == "stale"
    assert runtime_trace["blocks_delivery"] is True
    assert static_trace["fresh"] is True
    assert static_trace["evidence"]["proof:build"]["status"] == "passed"


def test_missing_current_runtime_fingerprint_stales_only_passed_runtime_evidence():
    product = _product(_requirement("Rendered home", "ui:route:/"))
    extra = {"proof_ladder": _route_ladder(_passing_route("/"))}
    binding = _binding(product, extra, runtime=_runtime())

    trace = _compile(
        product,
        extra,
        binding=binding,
        current_runtime=None,
    )

    assert trace["fresh"] is False
    assert trace["freshness_reason"] == "current runtime input fingerprint is missing"
    assert trace["evidence"]["ui:route:/"]["status"] == "stale"


def test_stale_binding_preserves_failed_skipped_and_missing_observations():
    cases = [
        ("failed", "failed"),
        ("skipped", "skipped"),
        (None, "missing"),
    ]
    for raw_status, expected in cases:
        product = _product(_requirement(f"Build {expected}", "proof:build"))
        detail = {} if raw_status is None else {"build": raw_status}
        extra = {"proof": _proof(**detail)}
        old_source = _snapshot("a" * 64)
        trace = _compile(
            product,
            extra,
            current=_snapshot("b" * 64),
            binding=_binding(product, extra, old_source),
        )

        assert trace["fresh"] is False
        assert trace["evidence"]["proof:build"]["status"] == expected
        assert "observed_status" not in trace["evidence"]["proof:build"]


def test_no_requirements_or_no_active_must_requirements_is_visibly_unbound():
    empty = _product()
    deferred = _product(
        _requirement("Later", "proof:build", status="deferred"),
    )

    empty_trace = _compile(empty, {})
    deferred_trace = _compile(deferred, {"proof": _proof(build="passed")})

    assert empty_trace["status"] == "unbound"
    assert empty_trace["blocks_delivery"] is False
    assert deferred_trace["status"] == "unbound"
    assert deferred_trace["blocks_delivery"] is False
    assert deferred_trace["summary"]["must_total"] == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("priority", "urgent", "priority"),
        ("status", "mystery", "status"),
    ],
)
def test_unknown_requirement_vocabulary_is_rejected(field, value, message):
    product = _product(_requirement("Compile", "proof:build")).to_dict()
    product["requirements"][0][field] = value

    with pytest.raises(RequirementTraceValidationError, match=message):
        _compile(product, {"proof": _proof(build="passed")})


def test_duplicate_requirement_and_acceptance_ids_are_rejected():
    duplicate_requirements = _product(
        _requirement("One", "proof:build"),
        _requirement("Two", "proof:ruff"),
    ).to_dict()
    duplicate_requirements["requirements"][1]["id"] = duplicate_requirements[
        "requirements"
    ][0]["id"]
    duplicate_acceptance = _product(_requirement("One", "proof:build")).to_dict()
    duplicate_acceptance["requirements"][0]["acceptance_ids"].append("proof:build")

    with pytest.raises(RequirementTraceValidationError, match="duplicate requirement id"):
        _compile(duplicate_requirements, {"proof": _proof(build="passed")})
    with pytest.raises(RequirementTraceValidationError, match="duplicate acceptance id"):
        _compile(duplicate_acceptance, {"proof": _proof(build="passed")})


def test_contract_limits_fail_before_unbounded_work():
    too_many_requirements = {
        "project_id": "limits",
        "version": 1,
        "requirements": [
            {
                "id": f"req-{index}",
                "text": f"Requirement {index}",
                "priority": "must",
                "status": "planned",
                "acceptance_ids": [],
            }
            for index in range(MAX_REQUIREMENTS + 1)
        ],
    }
    too_many_ids = {
        "project_id": "limits",
        "version": 1,
        "requirements": [
            {
                "id": "req-one",
                "text": "One",
                "priority": "must",
                "status": "planned",
                "acceptance_ids": [
                    f"proof:unknown-{index}"
                    for index in range(MAX_ACCEPTANCE_IDS_PER_REQUIREMENT + 1)
                ],
            }
        ],
    }

    with pytest.raises(RequirementTraceValidationError, match="requirements exceeds"):
        _compile(too_many_requirements, {})
    with pytest.raises(RequirementTraceValidationError, match="acceptance_ids exceeds"):
        _compile(too_many_ids, {})


def test_dynamic_evidence_record_limit_fails_closed_without_scanning_every_id():
    product = _product(_requirement("Home", "ui:route:/"))
    proofs = [
        {
            "schema_version": 1,
            "route": f"/route-{index}",
            "passed": True,
            "skipped": False,
        }
        for index in range(MAX_DYNAMIC_EVIDENCE_RECORDS + 1)
    ]
    ladder = _ladder(
        ProofStep(
            "playwright",
            "passed",
            True,
            detail={
                "routes": [proof["route"] for proof in proofs],
                "proofs": proofs,
            },
        )
    )

    trace = _compile(product, {"proof_ladder": ladder})

    assert trace["evidence"]["ui:route:/"]["status"] == "failed"
    assert "limit" in trace["evidence"]["ui:route:/"]["reason"]
    assert trace["blocks_delivery"] is True


def test_entrypoint_presence_cannot_pass_when_overall_proof_failed():
    product = _product(_requirement("Runnable", "proof:entrypoint"))
    proof = ProofResult(
        passed=False,
        mode="sandbox",
        detail={"entrypoints": ["index.html"], "boot_error": ""},
    ).to_dict()

    trace = _compile(product, {"proof": proof})

    assert trace["evidence"]["proof:entrypoint"]["status"] == "failed"
    assert trace["blocks_delivery"] is True


def test_real_proof_gate_headless_and_ladder_payloads_round_trip_to_json():
    product = _product(
        _requirement(
            "Verified delivery",
            "proof:overall",
            "proof:entrypoint",
            "proof:build",
            "gate:headless",
            "gate:workflow",
            "ui:route:/",
        )
    )
    extra = {
        "proof": ProofResult(
            passed=True,
            mode="sandbox",
            detail={"entrypoints": ["index.html"], "build": "passed"},
        ).to_dict(),
        "headless_gate": HeadlessGateResult(
            applicable=True,
            passed=True,
        ).to_dict(),
        "workflow_check": GateVerdict().to_dict(),
        "proof_ladder": _route_ladder(_passing_route("/")),
    }

    trace = _compile(product, extra)
    encoded = json.dumps(trace, allow_nan=False, sort_keys=True)

    assert trace["status"] == "passed"
    assert trace["go_eligible"] is True
    assert trace["blocks_delivery"] is False
    assert trace["summary"]["total"] == trace["summary"]["must_total"] == 1
    assert trace["summary"]["proven"] == trace["summary"]["must_proven"] == 1
    assert trace["summary"]["must_proven"] == 1
    assert json.loads(encoded) == trace


def test_real_maestro_proof_ladder_payload_can_satisfy_exact_flow():
    product = _product(
        _requirement("Login flow", "mobile:maestro:.maestro/login.yaml")
    )
    ladder = ProofLadderResult(
        project_dir="/tmp/mobile",
        stack="react_native",
        artifact_dir="/tmp/mobile/.skyn3t/proof-ladder",
        run_id=EVIDENCE_RUN_ID,
        steps=[
            ProofStep(
                "maestro",
                "passed",
                True,
                detail={
                    "flows": [".maestro/login.yaml"],
                    "executions": [
                        {
                            "flow": ".maestro/login.yaml",
                            "passed": True,
                            "returncode": 0,
                            "timed_out": False,
                            "artifact_written": True,
                            "junit": "maestro/login.xml",
                            "artifact_dir": "maestro/login-artifacts",
                        }
                    ],
                },
            )
        ],
    )
    ladder.finalize()
    ladder.report_path = "/tmp/mobile/.skyn3t/proof-ladder/proof-ladder.json"

    trace = _compile(product, {"proof_ladder": ladder.to_dict()})

    assert trace["evidence"]["mobile:maestro:.maestro/login.yaml"]["status"] == "passed"
    assert trace["status"] == "passed"


def test_strict_ladder_rejects_schema_status_persistence_step_and_record_forgery():
    product = _product(_requirement("Home route", "ui:route:/"))
    base = _route_ladder(_passing_route("/"))
    variants: list[tuple[str, dict[str, object]]] = []

    wrong_schema = copy.deepcopy(base)
    wrong_schema["schema_version"] = 99
    variants.append(("schema", wrong_schema))

    bool_schema = copy.deepcopy(base)
    bool_schema["schema_version"] = True
    variants.append(("boolean schema", bool_schema))

    missing_report = copy.deepcopy(base)
    missing_report["report_path"] = None
    variants.append(("missing persisted report", missing_report))

    contradiction = copy.deepcopy(base)
    contradiction["passed"] = False
    variants.append(("contradiction", contradiction))

    persistence_error = copy.deepcopy(base)
    persistence_error["persistence_error"] = "disk full"
    variants.append(("persistence", persistence_error))

    duplicate_step = copy.deepcopy(base)
    duplicate_step["steps"].append(copy.deepcopy(duplicate_step["steps"][0]))
    variants.append(("duplicate step", duplicate_step))

    optional_step = copy.deepcopy(base)
    optional_step["steps"][0]["required"] = False
    variants.append(("required step", optional_step))

    wrong_record_schema = copy.deepcopy(base)
    wrong_record_schema["steps"][0]["detail"]["proofs"][0]["schema_version"] = 2
    variants.append(("record schema", wrong_record_schema))

    bool_record_schema = copy.deepcopy(base)
    bool_record_schema["steps"][0]["detail"]["proofs"][0]["schema_version"] = True
    variants.append(("boolean record schema", bool_record_schema))

    contradictory_record = copy.deepcopy(base)
    contradictory_record["steps"][0]["detail"]["proofs"][0]["skipped"] = True
    variants.append(("record contradiction", contradictory_record))

    failing_viewport = copy.deepcopy(base)
    failing_viewport["steps"][0]["detail"]["proofs"][0]["viewports"][0][
        "console_errors"
    ] = ["boom"]
    variants.append(("viewport contradiction", failing_viewport))

    missing_screenshot = copy.deepcopy(base)
    missing_screenshot["steps"][0]["detail"]["proofs"][0]["viewports"][0][
        "screenshot"
    ] = None
    variants.append(("viewport artifact", missing_screenshot))

    duplicate_unrelated_step = copy.deepcopy(base)
    duplicate_unrelated_step["steps"].extend(
        [
            {
                "name": "preview",
                "status": "passed",
                "required": True,
                "reason": "",
                "artifacts": [],
                "detail": {},
            },
            {
                "name": "preview",
                "status": "passed",
                "required": True,
                "reason": "",
                "artifacts": [],
                "detail": {},
            },
        ]
    )
    variants.append(("duplicate unrelated step", duplicate_unrelated_step))

    for label, payload in variants:
        trace = _compile(product, {"proof_ladder": payload})
        evidence = trace["evidence"]["ui:route:/"]
        assert evidence["status"] != "passed", label
        assert trace["blocks_delivery"] is True, label


@pytest.mark.parametrize(
    "acceptance_id",
    [
        "ui:route:https://evil.example/",
        "ui:route:/../../admin",
        "mobile:maestro:../escape.yaml",
        "mobile:maestro:/absolute.yaml",
        "gate:cli-playtest:bad scenario",
    ],
)
def test_dynamic_acceptance_ids_are_canonical_bounded_and_fail_closed(acceptance_id):
    product = _product(_requirement("Dynamic evidence", acceptance_id))

    trace = _compile(product, {})

    assert trace["evidence"][acceptance_id]["status"] == "missing"
    assert "invalid dynamic acceptance id" in trace["evidence"][acceptance_id]["reason"]
    assert trace["blocks_delivery"] is True
