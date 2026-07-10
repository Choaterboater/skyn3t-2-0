from __future__ import annotations

from pathlib import Path

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus
from skyn3t.studio.acceptance_contract import (
    GENERATED_ACCEPTANCE_HEADER,
    GENERATED_ACCEPTANCE_PENDING_MARKER,
)


def _contract_bytes() -> bytes:
    return (
        f'"""{GENERATED_ACCEPTANCE_HEADER}\n"""\n'
        "import pytest\n"
        f'@pytest.mark.skip(reason="{GENERATED_ACCEPTANCE_PENDING_MARKER}")\n'
        "def test_required_behavior():\n    assert False, 'required'\n"
    ).encode()


def _seed_contract(root: Path) -> tuple[Path, bytes]:
    contract = root / "tests" / "test_acceptance_app.py"
    contract.parent.mkdir(parents=True, exist_ok=True)
    expected = _contract_bytes()
    contract.write_bytes(expected)
    return contract, expected


def _task(root: Path, *, slice_scope: dict | None = None) -> TaskRequest:
    payload = {
        "brief": "a python application",
        "slug": "app",
        "stack": "python",
        "worktree_dir": str(root),
        "plan": {"stack": "python", "files": [{"path": "main.py"}]},
    }
    if slice_scope is not None:
        payload["slice_scope"] = slice_scope
    return TaskRequest(
        type="codegen", payload=payload, capabilities_required=("codegen",)
    )


async def test_monolithic_agentic_retries_restore_contract_between_attempts(
    tmp_path, monkeypatch
):
    contract, expected = _seed_contract(tmp_path)
    llm = LLMClient(Settings(llm_backend="claude_cli", agentic_retries=1))
    monkeypatch.setattr(llm, "_cli_available", lambda provider: provider == "claude")
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    calls = {"n": 0}

    async def adversarial_agentic(prompt, workdir, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            assert contract.read_bytes() == expected
        contract.write_text("def test_fake_green():\n    assert True\n", encoding="utf-8")
        target = Path(workdir) / "main.py"
        if calls["n"] == 2:
            target.write_text("print('complete')\n" + "x = 1\n" * 300, encoding="utf-8")
        return {
            "ok": calls["n"] == 2,
            "completed": calls["n"] == 2,
            "auto_converged": calls["n"] == 2,
            "backend": "claude_cli",
            "error": "retry" if calls["n"] == 1 else "",
        }

    monkeypatch.setattr(llm, "agentic_build", adversarial_agentic)
    result = await agent.run(_task(tmp_path))

    assert calls["n"] == 2
    assert contract.read_bytes() == expected
    assert result.output["acceptance_contracts_protected"] == [
        "tests/test_acceptance_app.py"
    ]
    assert result.output["acceptance_contracts_restored"] == [
        "tests/test_acceptance_app.py"
    ]
    assert result.output["agentic"]["auto_converged"] is True


async def test_completion_batch_cannot_delete_preexisting_contract(tmp_path, monkeypatch):
    contract, expected = _seed_contract(tmp_path)
    llm = LLMClient(Settings(
        llm_backend="openrouter",
        openrouter_api_key="x",
        openrouter_agentic=False,
    ))
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()

    async def adversarial_completion(*args, **kwargs):
        contract.unlink()
        return "print('generated')\n"

    monkeypatch.setattr(agent, "_generate_file", adversarial_completion)
    result = await agent.run(_task(tmp_path))

    assert contract.read_bytes() == expected
    assert result.output["acceptance_contracts_restored"] == [
        "tests/test_acceptance_app.py"
    ]


async def test_agentic_slice_cannot_weaken_seeded_contract(tmp_path, monkeypatch):
    contract, expected = _seed_contract(tmp_path)
    llm = LLMClient(Settings(llm_backend="claude_cli"))
    monkeypatch.setattr(llm, "_cli_available", lambda provider: provider == "claude")
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()

    async def adversarial_slice(prompt, workdir, **kwargs):
        contract.write_text("def test_fake_green():\n    assert True\n", encoding="utf-8")
        Path(workdir, "main.py").write_text("print('slice')\n", encoding="utf-8")
        return {"ok": True, "completed": True, "backend": "claude_cli"}

    monkeypatch.setattr(llm, "agentic_build", adversarial_slice)
    result = await agent.run(_task(
        tmp_path,
        slice_scope={"name": "backend", "files": ["main.py"], "manifest": ""},
    ))

    assert contract.read_bytes() == expected
    assert result.output["acceptance_contracts_protected"] == [
        "tests/test_acceptance_app.py"
    ]
    assert result.output["acceptance_contracts_restored"] == [
        "tests/test_acceptance_app.py"
    ]
