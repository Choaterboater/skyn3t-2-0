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
