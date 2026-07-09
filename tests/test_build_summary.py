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
