from skyn3t.studio.build_summary import build_summary


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
