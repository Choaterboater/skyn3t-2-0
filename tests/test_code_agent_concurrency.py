"""CodeAgent concurrency is isolated by target worktree, not singleton instance."""

from __future__ import annotations

import asyncio
from pathlib import Path

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


def _source(label: str) -> str:
    return "\n".join(f"export const {label}_{i} = {i};" for i in range(1400))


def _task(worktree: Path, label: str) -> TaskRequest:
    rel = f"src/{label}.jsx"
    return TaskRequest(
        type="codegen",
        payload={
            "brief": f"the isolated {label} application",
            "slug": label,
            "stack": "react_vite",
            "plan": {
                "stack": "react_vite",
                "summary": f"architecture for {label}",
                "files": [{"path": rel, "purpose": f"the {label} application"}],
            },
            "worktree_dir": str(worktree),
        },
        capabilities_required=("codegen",),
    )


class _ConcurrentLLM:
    backend = "claude_cli"
    supports_agentic = True
    last_model = None
    last_route = None
    routes: list = []

    def __init__(self, *, wait_for_release: bool = False):
        self.wait_for_release = wait_for_release
        self.release = asyncio.Event()
        self.two_entered = asyncio.Event()
        self.first_entered = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def agentic_build(self, prompt, workdir, **kwargs):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.first_entered.set()
        if self.active >= 2:
            self.two_entered.set()
        try:
            if self.wait_for_release:
                await self.release.wait()
            else:
                await asyncio.sleep(0.03)
            label = Path(workdir).name
            target = Path(workdir) / "src" / f"{label}.jsx"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_source(label), encoding="utf-8")
            return {"ok": True, "completed": True, "backend": "claude_cli"}
        finally:
            self.active -= 1


async def test_distinct_worktrees_execute_concurrently_with_isolated_metadata(tmp_path):
    llm = _ConcurrentLLM(wait_for_release=True)
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    left = tmp_path / "left"
    right = tmp_path / "right"
    pending = asyncio.gather(
        agent.run(_task(left, "left")),
        agent.run(_task(right, "right")),
    )
    try:
        await asyncio.wait_for(llm.two_entered.wait(), timeout=1)
    finally:
        llm.release.set()
    results = await pending

    assert llm.max_active == 2
    assert "left application" in results[0].output["prompts"][0]["text"]
    assert "right application" not in results[0].output["prompts"][0]["text"]
    assert "right application" in results[1].output["prompts"][0]["text"]
    assert all("degraded" not in result.output for result in results)
    assert agent._worktree_locks == {}
    assert "prompts" not in agent.metadata


async def test_same_worktree_is_serialized_and_lock_entry_is_released(tmp_path):
    llm = _ConcurrentLLM()
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    shared = tmp_path / "shared"

    results = await asyncio.gather(
        agent.run(_task(shared, "shared")),
        agent.run(_task(shared, "shared")),
    )

    assert llm.max_active == 1
    assert all(result.success for result in results)
    assert agent._worktree_locks == {}


async def test_cancelled_same_worktree_waiter_does_not_leak_lock_entry(tmp_path):
    llm = _ConcurrentLLM(wait_for_release=True)
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    shared = tmp_path / "cancelled"
    holder = asyncio.create_task(agent.run(_task(shared, "cancelled")))
    await asyncio.wait_for(llm.first_entered.wait(), timeout=1)
    waiter = asyncio.create_task(agent.run(_task(shared, "cancelled")))
    await asyncio.sleep(0)
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
    llm.release.set()
    await holder

    assert agent._worktree_locks == {}
