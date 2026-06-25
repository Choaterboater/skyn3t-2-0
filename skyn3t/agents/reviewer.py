"""ReviewerAgent — scores a generated project 0-100 and renders a go/no_go.

The score is a deterministic heuristic blend of objective signals (entrypoint
present, non-empty source files, manifest present, build config parseable),
optionally blended with an LLM opinion when a real LLM backend is available.
The heuristic alone always works offline (design rules #1, #3, #6).

agent_type: "review"   capability: "review"
Stage output: {"score": float 0-100, "verdict": "go"|"no_go", "gaps": [...]}
"""

from __future__ import annotations

import json
from typing import Any

from skyn3t.agents import _verify_common as vc
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

# Pass threshold — projects scoring at/above this get a "go".
GO_THRESHOLD = 60.0


def heuristic_score(root, payload: dict[str, Any]) -> tuple[float, list[str]]:
    """Return (score 0-100, gaps). Pure, deterministic, offline."""
    gaps: list[str] = []
    score = 0.0

    # 1) Entrypoint present (25 pts)
    entrypoints = vc.find_entrypoints(root)
    if entrypoints:
        score += 25.0
    else:
        gaps.append("no recognizable entrypoint (main.py/index.js/app.py/...)")

    # 2) Non-empty source files (35 pts, scaled up to 5 files)
    src = vc.non_empty_source_count(root)
    if src >= 5:
        score += 35.0
    elif src > 0:
        score += 35.0 * (src / 5.0)
        gaps.append(f"only {src} non-empty source file(s); expected more substance")
    else:
        gaps.append("no non-empty source files found")

    # 3) Manifest present (20 pts)
    manifests = vc.find_manifests(root)
    if manifests:
        score += 20.0
    else:
        gaps.append("no dependency manifest (package.json/pyproject.toml/...)")

    # 4) Build config valid / parseable (20 pts)
    ok, why = _build_config_valid(root)
    if ok:
        score += 20.0
    else:
        gaps.append(why)

    return round(min(score, 100.0), 2), gaps


def _build_config_valid(root) -> tuple[bool, str]:
    """Verify the primary manifest parses (JSON for node, TOML-ish for python)."""
    if not root:
        return False, "no project directory to inspect"
    pkg = root / "package.json"
    if pkg.exists():
        text = vc.safe_read(pkg)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return False, "package.json is not valid JSON"
        if not isinstance(data, dict):
            return False, "package.json is not a JSON object"
        return True, ""
    for name in ("pyproject.toml", "requirements.txt", "setup.py"):
        f = root / name
        if f.exists() and vc.file_size(f) > 0:
            return True, ""
    return False, "no parseable build/dependency configuration"


class ReviewerAgent(BaseAgent):
    def __init__(self, name: str = "reviewer", event_bus: EventBus | None = None,
                 config: dict | None = None, llm_client: Any | None = None) -> None:
        super().__init__(name, agent_type="review", provider="local",
                         event_bus=event_bus, config=config or {})
        self.add_capability(AgentCapability(
            name="review",
            description="Scores a project 0-100 and returns a go/no_go verdict",
            tags=("review", "quality"),
        ))
        self.llm = llm_client

    async def initialize(self) -> None:
        self.metadata["go_threshold"] = GO_THRESHOLD

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        payload = task.payload or {}
        root = vc.resolve_project_dir(payload)
        score, gaps = heuristic_score(root, payload)
        blend_meta: dict[str, Any] = {"heuristic_score": score}

        # Optional LLM blend — never required, never blocks offline.
        llm_score = await self._maybe_llm_score(payload, root)
        if llm_score is not None:
            blend_meta["llm_score"] = llm_score
            score = round(0.7 * score + 0.3 * llm_score, 2)
            blend_meta["blended_score"] = score

        # Hard gate: a project with no substantive source content is never "go",
        # however high the additive heuristic climbs (entrypoint name + manifest
        # + parseable config alone could clear the threshold on an empty repo).
        no_source = root is None or vc.non_empty_source_count(root) == 0
        verdict = "go" if (score >= GO_THRESHOLD and not no_source) else "no_go"
        if root is None and "no project directory" not in " ".join(gaps):
            gaps.append("no project directory could be resolved from payload")
        elif no_source and "no non-empty source files found" not in " ".join(gaps):
            gaps.append("no non-empty source files found")

        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={"score": score, "verdict": verdict, "gaps": gaps},
            metadata=blend_meta,
        )

    async def _maybe_llm_score(self, payload: dict[str, Any], root) -> float | None:
        if self.llm is None:
            return None
        backend = getattr(self.llm, "backend", "stub")
        if backend == "stub":
            return None  # stub adds no signal — skip the blend
        brief = payload.get("brief", "")
        entry = vc.find_entrypoints(root)[:5]
        prompt = (
            "Rate this generated software project from 0 to 100 for completeness "
            "and correctness relative to the brief. Reply ONLY with JSON "
            '{"score": <number>}.\n\n'
            f"Brief: {brief}\nEntrypoints: {entry}\n"
        )
        try:
            # A 0-100 completeness rating capped at 128 tokens and blended at only
            # 0.3 weight does NOT need a heavy reasoning model — that was the bulk
            # of the review stage's wall-clock. A fast/cheap tier gives the same
            # signal in a fraction of the time.
            res = await self.llm.complete(
                prompt, tier=Tier.CHEAP, json_mode=True, max_tokens=128, task_type=self.agent_type
            )
            data = json.loads(res.text)
            val = float(data.get("score"))
            return max(0.0, min(100.0, val))
        except Exception:  # noqa: BLE001 - LLM is best-effort, degrade quietly
            return None
