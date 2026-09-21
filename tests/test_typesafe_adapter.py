"""Tests for TypeSafeAdapter (System One typed decisions integration)."""

from unittest.mock import MagicMock

import pytest

from skyn3t.adapters.typesafe import TypeSafeAdapter
from skyn3t.config.settings import Settings


def test_typesafe_adapter_unconfigured(monkeypatch):
    """Adapter gracefully handles unconfigured state (no API key)."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("SKYN3T_TYPESAFE_API_KEY", raising=False)
    settings = Settings(typesafe_api_key="")
    adapter = TypeSafeAdapter(settings=settings, api_key="")
    assert not adapter.available
    res = adapter.evaluate_system_one("test state", {"q": {"type": "noul", "instructions": "test"}})
    assert res == {}


def test_typesafe_adapter_mocked_eval():
    """Adapter correctly maps SDK response types in evaluate_system_one."""
    mock_choice_ans = MagicMock()
    mock_choice_ans.type = "choice"
    mock_choice_ans.choice = "game"
    mock_choice_ans.probabilities = {"game": 0.9}
    mock_choice_ans.confidence = 0.85

    mock_noul_ans = MagicMock()
    mock_noul_ans.type = "noul"
    mock_noul_ans.noul = 0.98

    mock_response = MagicMock()
    mock_response.answers = {
        "project_type": mock_choice_ans,
        "is_urgent": mock_noul_ans,
    }

    mock_client = MagicMock()
    mock_client.system_one.return_value = mock_response

    adapter = TypeSafeAdapter(api_key="test_key")
    adapter._client = mock_client

    res = adapter.evaluate_system_one(
        state="Build a retro arcade game",
        questions={
            "project_type": {"type": "choice", "instructions": "Project type", "criteria": {"game": "Game"}},
            "is_urgent": {"type": "noul", "instructions": "Is urgent"},
        },
    )

    assert "project_type" in res
    assert res["project_type"]["choice"] == "game"
    assert res["is_urgent"]["noul"] == 0.98


def test_response_evidence_is_attached_without_headers_or_stale_metadata():
    from types import SimpleNamespace

    answer = SimpleNamespace(
        type="choice", choice="react", confidence=0.8,
        probabilities={"react": 0.9, "python": 0.1},
    )
    client = MagicMock()
    client.system_one.side_effect = [
        SimpleNamespace(
            answers={"stack": answer}, model="jev-test-version", request_id="req_test",
            usage=SimpleNamespace(input_tokens=120, output_tokens=15),
            headers={"Authorization": "must-not-be-recorded"},
        ),
        SimpleNamespace(answers={"stack": answer}),
    ]
    adapter = TypeSafeAdapter(api_key="test_key")
    adapter._client = client
    questions = {"stack": {"type": "choice", "criteria": {"react": "web", "python": "CLI"}}}
    first = adapter.evaluate_system_one("synthetic brief", questions)
    second = adapter.evaluate_system_one("synthetic brief", questions)

    assert first["stack"]["response_metadata"] == {
        "model": "jev-test-version", "request_id": "req_test",
        "usage": {"input_tokens": 120, "output_tokens": 15},
    }
    assert second["stack"]["response_metadata"] == {}
    assert first["stack"]["probabilities"] == {"react": 0.9, "python": 0.1}


def test_response_evidence_ignores_malformed_usage_values():
    from types import SimpleNamespace

    client = MagicMock()
    client.system_one.return_value = SimpleNamespace(
        answers={"flag": SimpleNamespace(type="noul", noul=0.9)},
        model={"invalid": True}, request_id=None,
        usage=SimpleNamespace(input_tokens=-3, output_tokens=True),
    )
    adapter = TypeSafeAdapter(api_key="test_key")
    adapter._client = client
    assert adapter.evaluate_system_one(
        "synthetic state", {"flag": {"type": "noul"}}
    )["flag"]["response_metadata"] == {}


@pytest.mark.parametrize("bad_question", [
    {"type": "unsupported"}, {"type": 42}, object(), None,
])
def test_invalid_question_rejects_whole_batch_before_api_call(bad_question):
    adapter = TypeSafeAdapter(api_key="test_key")
    client = MagicMock()
    adapter._client = client
    questions = {"valid": {"type": "noul"}, "invalid": bad_question}
    assert adapter.evaluate_system_one("synthetic state", questions) == {}
    client.system_one.assert_not_called()
    with pytest.raises((TypeError, ValueError)):
        adapter.evaluate_system_one("synthetic state", questions, raise_on_error=True)
    client.system_one.assert_not_called()


@pytest.mark.parametrize("answers", [
    {}, {"unexpected": MagicMock(type="noul", noul=0.9)},
    {"flag": MagicMock(type="choice", choice="yes")},
])
def test_partial_or_wrong_answer_types_are_not_success(answers):
    from types import SimpleNamespace

    adapter = TypeSafeAdapter(api_key="test_key")
    adapter._client = MagicMock()
    adapter._client.system_one.return_value = SimpleNamespace(answers=answers)
    assert adapter.evaluate_system_one("synthetic state", {"flag": {"type": "noul"}}) == {}
    with pytest.raises(ValueError):
        adapter.evaluate_system_one(
            "synthetic state", {"flag": {"type": "noul"}}, raise_on_error=True,
        )


def test_empty_question_batch_is_rejected_without_network():
    adapter = TypeSafeAdapter(api_key="test_key")
    adapter._client = MagicMock()
    with pytest.raises(ValueError):
        adapter.evaluate_system_one("synthetic state", {}, raise_on_error=True)
    adapter._client.system_one.assert_not_called()


def test_provider_failure_logs_type_without_private_payload(capsys):
    adapter = TypeSafeAdapter(api_key="test_key")
    adapter._client = MagicMock()
    adapter._client.system_one.side_effect = RuntimeError("private-prompt-or-token")
    assert adapter.evaluate_system_one("synthetic state", {"flag": {"type": "noul"}}) == {}
    output = capsys.readouterr().out
    assert "typesafe.evaluate_failed" in output
    assert "RuntimeError" in output
    assert "private-prompt-or-token" not in output


@pytest.mark.asyncio
async def test_typesafe_live_api_if_key_present():
    """Live API test against SkyN3t using an environment-provided API key.

    Never hardcode a real key here — this test SKIPS (not fails) when no key
    is present in the environment, so CI and contributors without a key are
    unaffected.
    """
    import os
    key = os.getenv("TYPESAFE_API_KEY") or os.getenv("SKYN3T_TYPESAFE_API_KEY") or ""
    adapter = TypeSafeAdapter(api_key=key)
    if not adapter.available:
        pytest.skip("TYPESAFE_API_KEY/SKYN3T_TYPESAFE_API_KEY not set — skipping live API test")

    # 1. Test triage_intent on a SkyN3t brief
    triage = adapter.triage_intent(
        "Build a fast 2D Space Shooter arcade game with local high score storage in React"
    )
    assert "project_type" in triage
    assert triage["project_type"]["choice"] == "game"

    # 2. Test evaluate_verdict on build proof evidence
    proof_evidence = "Build succeeded in 12s. 14/14 unit tests passed. Preview launched on port 8080."
    verdict = adapter.evaluate_verdict(proof_evidence)
    assert "is_runnable" in verdict
    assert verdict["is_runnable"]["noul"] > 0.8


def test_typesafe_build_advisory_active(monkeypatch):
    """The build-result advisory hook returns a real risk assessment when
    TypeSafe is configured, using the caller's own build evidence."""
    import os

    from skyn3t.cli.main import _typesafe_build_advisory
    from skyn3t.config.settings import get_settings

    key = os.getenv("TYPESAFE_API_KEY") or os.getenv("SKYN3T_TYPESAFE_API_KEY") or ""
    if not key:
        pytest.skip("TYPESAFE_API_KEY/SKYN3T_TYPESAFE_API_KEY not set — skipping live API test")

    settings = get_settings()
    fake_result = {
        "status": "completed",
        "verdict": "go",
        "score": 68.0,
        "files": ["a.js"] * 29,
        "stack": "static",
        "cost_usd": 0.0,
        "extra": {"gaps": ["design.contrast_fail: 23 elements below WCAG AA"]},
    }
    advisory = _typesafe_build_advisory(fake_result, settings)
    assert advisory
    assert "risk_category" in advisory


def test_typesafe_build_advisory_degrades_without_key(monkeypatch):
    """No key/unavailable SDK -> advisory is None, never raises (design rule #6)."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("SKYN3T_TYPESAFE_API_KEY", raising=False)

    from skyn3t.cli.main import _typesafe_build_advisory
    from skyn3t.config.settings import Settings

    settings = Settings(typesafe_api_key="")
    fake_result = {"status": "completed", "verdict": "go", "score": 68.0, "files": [], "stack": "static"}
    assert _typesafe_build_advisory(fake_result, settings) is None


def test_typesafe_build_advisory_skips_incomplete_builds():
    """Advisory never runs against an aborted/failed build (no evidence to judge)."""
    from skyn3t.cli.main import _typesafe_build_advisory
    from skyn3t.config.settings import get_settings

    settings = get_settings()
    assert _typesafe_build_advisory({"status": "failed"}, settings) is None
    assert _typesafe_build_advisory({}, settings) is None
