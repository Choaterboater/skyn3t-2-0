from skyn3t.studio.build_summary import build_summary


def test_build_summary_preserves_submission_routing_snapshot():
    summary = build_summary(
        {
            "extra": {
                "llm_backend": "codex_cli",
                "routing_snapshot": {
                    "requested_backend": "codex_cli",
                    "effective_backend": "codex_cli",
                    "requested_model": "",
                    "effective_model": "codex-cli:default",
                    "codegen": {
                        "source": "codegen_cli_pin",
                        "requested_backend": "codex_cli",
                        "effective_backend": "codex_cli",
                        "requested_model": "gpt-5.6-codex",
                        "effective_model": "gpt-5.6-codex",
                    },
                },
            }
        }
    )

    trace = summary["model_trace"]
    assert trace["requested_backend"] == "codex_cli"
    assert trace["effective_backend"] == "codex_cli"
    assert trace["backend"] == "codex_cli"
    assert trace["requested_model"] == ""
    assert trace["effective_model"] == "codex-cli:default"
    assert trace["codegen"]["requested_model"] == "gpt-5.6-codex"
    assert trace["requested_codegen_model"] == "gpt-5.6-codex"
    assert trace["effective_codegen_model"] == "gpt-5.6-codex"


def test_build_summary_prefers_actual_openrouter_codegen_fallback_evidence():
    summary = build_summary(
        {
            "extra": {
                "routing_snapshot": {
                    "requested_backend": "openrouter",
                    "effective_backend": "openrouter",
                    "requested_model": "",
                    "effective_model": "router:auto",
                    "submission": {
                        "requested_backend": "openrouter",
                        "model_override": "",
                    },
                    "codegen": {
                        "source": "global_backend",
                        "requested_backend": "openrouter",
                        "effective_backend": "openrouter",
                        "requested_model": "",
                        "effective_model": "router:auto",
                    },
                },
                "effective_codegen_model": "openai/fallback-that-ran",
                "agentic": {
                    "backend": "openrouter",
                    "model": "openai/fallback-that-ran",
                    "attempted_model": "openai/primary-that-failed",
                    "fallback_model": "openai/fallback-that-ran",
                },
            }
        }
    )

    trace = summary["model_trace"]
    assert trace["submission"]["codegen"]["effective_model"] == "router:auto"
    assert trace["codegen"]["requested_model"] == ""
    assert trace["codegen"]["effective_backend"] == "openrouter"
    assert trace["codegen"]["effective_model"] == "openai/fallback-that-ran"
    assert trace["effective_codegen_backend"] == "openrouter"
    assert trace["effective_codegen_model"] == "openai/fallback-that-ran"
    assert trace["codegen_model"] == "openai/fallback-that-ran"


def test_build_summary_keeps_submission_backend_separate_from_actual_codegen_backend():
    summary = build_summary(
        {
            "extra": {
                "routing_snapshot": {
                    "requested_backend": "openrouter",
                    "effective_backend": "openrouter",
                    "requested_model": "",
                    "effective_model": "router:auto",
                    "codegen": {
                        "source": "codegen_cli_pin",
                        "requested_backend": "claude_cli",
                        "effective_backend": "claude_cli",
                        "requested_model": "sonnet",
                        "effective_model": "sonnet",
                    },
                },
                "agentic": {
                    "backend": "codex_cli",
                    "model": "gpt-5.6-codex",
                },
            }
        }
    )

    trace = summary["model_trace"]
    assert trace["effective_backend"] == "openrouter"
    assert trace["submission"]["codegen"]["effective_backend"] == "claude_cli"
    assert trace["effective_codegen_backend"] == "codex_cli"
    assert trace["codegen"]["effective_backend"] == "codex_cli"
    assert trace["backend"] == "codex_cli"


def test_build_summary_ignores_malformed_collection_fields():
    summary = build_summary({
        "stages": None,
        "extra": {
            "prompts": "not-a-list",
            "stage_costs": {"unexpected": "mapping"},
            "skills_used": 42,
            "recall_used": "not-a-list",
        },
    })

    assert summary["model_trace"]["prompt_count"] == 0
    assert summary["model_trace"]["stages"] == []
    assert summary["model_trace"]["stage_costs"] == []
    assert summary["quality_scorecard"]["skills_count"] == 0
    assert summary["quality_scorecard"]["recall_count"] == 0
    assert summary["skills_used"] == []
    assert summary["recall_used"] == []


def test_build_summary_surfaces_responsive_visual_proof_without_route_payloads():
    summary = build_summary({
        "extra": {
            "responsive_visual_proof": {
                "schema_version": 1,
                "status": "skipped",
                "routes_checked": 0,
                "routes_failed": 0,
                "routes_skipped": 2,
                "artifact_dir": ".skyn3t/visual-proof",
                "report_path": "visual-proof.json",
                "viewports": [
                    {"name": "desktop", "width": 1440, "height": 900},
                    {"name": "mobile", "width": 390, "height": 844},
                ],
                "failed_routes": ["large payload deliberately omitted"],
            },
        },
    })

    responsive = summary["quality_scorecard"]["responsive_visual"]
    assert responsive["schema_version"] == 1
    assert responsive["status"] == "skipped"
    assert responsive["routes_skipped"] == 2
    assert responsive["artifact_dir"] == ".skyn3t/visual-proof"
    assert "failed_routes" not in responsive


def test_build_summary_compacts_requirement_trace_without_evidence_payloads():
    summary = build_summary({
        "extra": {
            "requirement_trace": {
                "schema_version": 1,
                "mode": "enforced",
                "status": "failed",
                "fresh": True,
                "blocks_delivery": True,
                "summary": {
                    "total": 3,
                    "must_total": 3,
                    "proven": 2,
                    "must_proven": 2,
                    "must_failed": 1,
                    "must_unbound": 0,
                    "must_stale": 0,
                    "requirements_total": 99,
                    "blocking_failed": 1,
                },
                "requirements": [{"text": "large private contract payload"}],
                "evidence": {"proof:build": {"status": "failed", "reason": "large"}},
                "binding": {"sha256": "a" * 64},
            },
        },
    })

    trace = summary["quality_scorecard"]["requirement_trace"]
    assert trace == {
        "schema_version": 1,
        "mode": "enforced",
        "status": "failed",
        "fresh": True,
        "blocks_delivery": True,
        "summary": {
            "total": 3,
            "must_total": 3,
            "proven": 2,
            "must_proven": 2,
            "must_failed": 1,
            "must_unbound": 0,
            "must_stale": 0,
        },
    }
    assert "requirements" not in trace
    assert "evidence" not in trace
    assert "binding" not in trace


def test_build_summary_requirement_trace_supports_must_count_aliases():
    summary = build_summary({
        "extra": {
            "requirement_trace": {
                "mode": "legacy_advisory",
                "status": "unbound",
                "fresh": False,
                "blocks_delivery": False,
                "summary": {
                    "must_total": 2,
                    "must_proven": 0,
                    "must_failed": 0,
                    "must_unbound": 2,
                },
            },
        },
    })

    trace = summary["quality_scorecard"]["requirement_trace"]
    assert trace["summary"]["total"] == 2
    assert trace["summary"]["proven"] == 0
    assert trace["summary"]["must_total"] == 2
    assert trace["summary"]["must_proven"] == 0
    assert "must_stale" not in trace["summary"]


def test_build_summary_tolerates_absent_or_malformed_requirement_trace():
    absent = build_summary({"extra": {}})
    malformed = build_summary({
        "extra": {
            "requirement_trace": {
                "schema_version": 10_000,
                "mode": ["enforced"],
                "status": {"value": "passed"},
                "fresh": "yes",
                "blocks_delivery": 1,
                "summary": {
                    "total": -1,
                    "must_total": True,
                    "proven": 10**20,
                    "must_failed": "not-a-number",
                },
                "evidence": "must not be projected",
            },
        },
    })

    assert absent["quality_scorecard"]["requirement_trace"] == {}
    assert malformed["quality_scorecard"]["requirement_trace"] == {}


def test_build_summary_surfaces_invalid_requirement_trace_without_raw_error():
    summary = build_summary({
        "extra": {
            "requirement_trace": {
                "schema_version": 1,
                "mode": "invalid",
                "status": "failed",
                "fresh": False,
                "blocks_delivery": True,
                "freshness_reason": "private compiler detail",
                "evidence": {"private": "payload"},
            },
        },
    })

    assert summary["quality_scorecard"]["requirement_trace"] == {
        "schema_version": 1,
        "mode": "invalid",
        "status": "failed",
        "fresh": False,
        "blocks_delivery": True,
    }


def test_build_summary_compacts_cli_execution_evidence():
    summary = build_summary({
        "extra": {
            "agentic": {
                "backend": "codex_cli",
                "cli_execution": {
                    "schema_version": 1,
                    "provider": "codex",
                    "streamed": True,
                    "event_count": 3,
                    "parsed_event_count": 3,
                    "event_type_counts": {
                        "thread.started": 1,
                        "turn.completed": 1,
                        "bad type with whitespace": 99,
                    },
                    "thread_id": "thread-123",
                    "session_persistence": "ephemeral",
                    "terminal_event_type": "turn.completed",
                    "exit_code": 0,
                    "exit_status": "exited",
                    "cli_version": "codex-cli 9.9.9\nignored extra line",
                    "raw_event": {"prompt": "must not survive"},
                },
            },
        },
    })

    execution = summary["model_trace"]["agentic"]["cli_execution"]
    assert execution["provider"] == "codex"
    assert execution["thread_id"] == "thread-123"
    assert execution["session_persistence"] == "ephemeral"
    assert execution["event_type_counts"] == {
        "thread.started": 1,
        "turn.completed": 1,
    }
    assert execution["cli_version"] == "codex-cli 9.9.9 ignored extra line"
    assert "raw_event" not in execution
