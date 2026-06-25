"""IntegrationVerifierAgent — frontend<->backend wiring checks.

Verifies that the pieces of a full-stack app are actually connected: the
frontend references an API base/endpoint, and the backend declares routes that
plausibly serve it. For single-tier projects it reports a benign "pass" with a
note. Heuristic and offline; it surfaces wiring gaps rather than proving full
runtime integration (design rules #3, #6).

agent_type: "verify_integration"   capability: "verify_integration"
Stage output: {"ok": bool, "verdict": "pass"|"fail", "gaps": [...], "wiring": {...}}
"""

from __future__ import annotations

import re
from pathlib import Path

from skyn3t.agents import _verify_common as vc
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

# Frontend signals that it talks to a backend.
_FE_CALL = re.compile(r"""(fetch\s*\(|axios\.|XMLHttpRequest|\$\.ajax|useSWR|useQuery)""")
_FE_URL = re.compile(r"""['"](/(api|v1|graphql)[^'"]*|https?://[^'"]+)['"]""")
# Backend route declarations across common frameworks.
_BE_ROUTE = re.compile(
    r"""(@app\.(get|post|put|delete|patch|route)|@router\.(get|post|put|delete|patch)"""
    r"""|app\.(get|post|put|delete|patch|use)\s*\(|router\.(get|post|put|delete|patch)\s*\(|"""
    r"""\.add_route|Flask\(|FastAPI\(|express\(\))"""
)

_FRONTEND_EXT = {".tsx", ".jsx", ".vue", ".svelte", ".html"}
_BACKEND_HINT = {"server", "api", "app", "main", "backend", "routes", "views", "urls"}


def _classify(root: Path):
    frontend: list[Path] = []
    backend: list[Path] = []
    for p in vc.iter_files(root):
        if p.suffix in _FRONTEND_EXT:
            frontend.append(p)
        elif p.suffix in {".js", ".ts", ".py"} and any(h in p.stem.lower() for h in _BACKEND_HINT):
            backend.append(p)
        elif p.suffix == ".py":
            backend.append(p)  # python files default to backend candidates
    return frontend, backend


def analyze(root: Path) -> dict:
    frontend, backend = _classify(root)
    fe_calls = False
    fe_urls: set[str] = set()
    for p in frontend + [b for b in backend if b.suffix in {".js", ".ts"}]:
        text = vc.safe_read(p)
        if _FE_CALL.search(text):
            fe_calls = True
        for m in _FE_URL.finditer(text):
            fe_urls.add(m.group(1))
    be_routes = False
    for p in backend:
        if _BE_ROUTE.search(vc.safe_read(p)):
            be_routes = True
            break
    return {
        "has_frontend": bool(frontend),
        "has_backend": bool(backend),
        "frontend_makes_calls": fe_calls,
        "frontend_urls": sorted(fe_urls)[:10],
        "backend_declares_routes": be_routes,
    }


class IntegrationVerifierAgent(BaseAgent):
    def __init__(self, name: str = "integration_verifier", event_bus: EventBus | None = None,
                 config: dict | None = None) -> None:
        super().__init__(name, agent_type="verify_integration", provider="local",
                         event_bus=event_bus, config=config or {})
        self.add_capability(AgentCapability(
            name="verify_integration",
            description="Checks frontend<->backend wiring is present",
            tags=("verify", "integration"),
        ))

    async def initialize(self) -> None:
        self.metadata["ready"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        payload = task.payload or {}
        root = vc.resolve_project_dir(payload)
        if root is None:
            return TaskResult(task_id=task.task_id, success=True,
                              output={"ok": False, "verdict": "fail", "gaps": ["no project directory"],
                                      "wiring": {}})
        wiring = analyze(root)
        gaps: list[str] = []

        full_stack = wiring["has_frontend"] and wiring["has_backend"]
        if full_stack:
            if not wiring["frontend_makes_calls"]:
                gaps.append("frontend makes no API calls to the backend")
            if not wiring["backend_declares_routes"]:
                gaps.append("backend declares no routes for the frontend to hit")
            if wiring["frontend_makes_calls"] and not wiring["frontend_urls"]:
                gaps.append("frontend calls have no resolvable endpoint URLs")
            ok = not gaps
            note = "full-stack wiring checked"
        else:
            # single-tier: nothing to wire across, pass with a note
            ok = True
            note = "single-tier project; no cross-tier wiring required"

        return TaskResult(
            task_id=task.task_id, success=True,
            output={"ok": ok, "verdict": "pass" if ok else "fail",
                    "gaps": gaps, "wiring": wiring, "note": note},
        )
