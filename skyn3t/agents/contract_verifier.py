"""ContractVerifierAgent — required-files / file-level-plan checker.

Verifies that the artifacts the plan promised actually exist and are non-empty.
Implements the 2.0 P2 item "File-level plan as verifier checklist": if the
plan declares a list of files (the architect's file-level plan), every one of
them becomes a checklist item that must be satisfied (design rule #3).

agent_type: "verify_contract"   capability: "verify_contract"
Stage output: {"ok": bool, "verdict": "pass"|"fail", "missing": [...],
               "satisfied": [...], "checklist": [...]}
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

from skyn3t.agents import _verify_common as vc


def extract_planned_files(payload: dict[str, Any]) -> list[str]:
    """Pull the file-level plan out of the payload, however it was shaped.

    Accepts: payload['files'], payload['plan']['files'],
    payload['plan']['file_plan'], or a list of {"path": ...} dicts.
    """
    candidates: list[Any] = []
    plan = payload.get("plan")
    if isinstance(plan, dict):
        for key in ("files", "file_plan", "planned_files", "checklist"):
            v = plan.get(key)
            if v:
                candidates = v
                break
    if not candidates:
        candidates = payload.get("files") or payload.get("required_files") or []

    out: list[str] = []
    if isinstance(candidates, dict):
        candidates = list(candidates.keys())
    for item in candidates or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            p = item.get("path") or item.get("file") or item.get("name")
            if p:
                out.append(str(p))
    return out


def build_checklist(root: Path | None, planned: list[str]) -> list[dict[str, Any]]:
    """Turn the planned files into a satisfied/missing checklist."""
    checklist: list[dict[str, Any]] = []
    root_resolved = root.resolve() if root else None
    for rel in planned:
        target: Path | None = None
        if root_resolved is not None and not Path(rel).is_absolute():
            # Constrain to the project tree: an absolute path or '../' traversal
            # in the (LLM-produced) plan must NOT be satisfied by a host file
            # outside the delivered project (would yield a false 'pass').
            candidate = (root_resolved / rel).resolve()
            try:
                common = os.path.commonpath([str(root_resolved), str(candidate)])
            except ValueError:
                common = ""
            if common == str(root_resolved):
                target = candidate
        exists = bool(target and target.exists() and target.is_file())
        size = vc.file_size(target) if (target and exists) else 0
        checklist.append({
            "file": rel,
            "exists": exists,
            "non_empty": size > 0,
            "satisfied": exists and size > 0,
        })
    return checklist


class ContractVerifierAgent(BaseAgent):
    def __init__(self, name: str = "contract_verifier", event_bus: EventBus | None = None,
                 config: dict | None = None) -> None:
        super().__init__(name, agent_type="verify_contract", provider="local",
                         event_bus=event_bus, config=config or {})
        self.add_capability(AgentCapability(
            name="verify_contract",
            description="Checks that planned/required files exist and are non-empty",
            tags=("verify", "contract"),
        ))

    async def initialize(self) -> None:
        self.metadata["ready"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        payload = task.payload or {}
        root = vc.resolve_project_dir(payload)
        planned = extract_planned_files(payload)

        if not planned:
            # No explicit plan: fall back to a baseline contract — at least one
            # entrypoint and one manifest must exist (rule #1, delivered != empty).
            entry = vc.find_entrypoints(root)
            manifests = vc.find_manifests(root)
            missing = []
            if not entry:
                missing.append("<entrypoint>")
            if not manifests:
                missing.append("<manifest>")
            ok = root is not None and not missing
            return TaskResult(
                task_id=task.task_id, success=True,
                output={
                    "ok": ok,
                    "verdict": "pass" if ok else "fail",
                    "missing": missing,
                    "satisfied": entry + manifests,
                    "checklist": [],
                    "mode": "baseline",
                },
            )

        checklist = build_checklist(root, planned)
        missing = [c["file"] for c in checklist if not c["satisfied"]]
        satisfied = [c["file"] for c in checklist if c["satisfied"]]
        ok = root is not None and not missing
        return TaskResult(
            task_id=task.task_id, success=True,
            output={
                "ok": ok,
                "verdict": "pass" if ok else "fail",
                "missing": missing,
                "satisfied": satisfied,
                "checklist": checklist,
                "mode": "file_plan",
            },
        )
