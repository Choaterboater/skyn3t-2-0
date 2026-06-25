"""CriticAgent — dedicated ADVERSARIAL reviewer (2.0 P0).

Distinct from :class:`ReviewerAgent`: where the reviewer asks "is this good
enough?", the critic actively HUNTS for bugs, security holes, and missing edge
cases in the generated code, and can BLOCK delivery. It uses ``Tier.STRONG``
for its LLM pass and combines that with deterministic static heuristics so it
still finds real problems offline (design rules #3, #4, #6).

agent_type: "critique"   capability: "critique"
Stage output: {"blocking_issues": [...], "verdict": "pass"|"block"}
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from skyn3t.agents import _verify_common as vc
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

# Deterministic red flags. Each entry: (compiled regex, severity, message).
# Severity "block" => hard stop; "warn" => advisory (non-blocking).
_PY_DANGER = [
    (re.compile(r"\beval\s*\("), "block", "use of eval() (code injection risk)"),
    (re.compile(r"\bexec\s*\("), "block", "use of exec() (code injection risk)"),
    (re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"), "block",
     "subprocess called with shell=True (shell injection risk)"),
    (re.compile(r"\bos\.system\s*\("), "block", "os.system() call (shell injection risk)"),
    (re.compile(r"pickle\.loads?\s*\("), "warn", "pickle load on untrusted data is unsafe"),
    (re.compile(r"verify\s*=\s*False"), "block", "TLS verification disabled (verify=False)"),
    (re.compile(r"yaml\.load\s*\((?!.*Loader)"), "warn", "yaml.load without SafeLoader"),
]
_JS_DANGER = [
    (re.compile(r"\beval\s*\("), "block", "use of eval() (code injection risk)"),
    (re.compile(r"dangerouslySetInnerHTML"), "warn", "dangerouslySetInnerHTML (XSS risk)"),
    (re.compile(r"child_process"), "warn", "child_process usage — audit for command injection"),
    (re.compile(r"\.innerHTML\s*="), "warn", "direct innerHTML assignment (XSS risk)"),
]
# Hardcoded-secret heuristics (any language).
_SECRET = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|token|aws_access_key)\s*[:=]\s*"
    r"['\"][A-Za-z0-9_\-/+]{12,}['\"]"
)
# Things that look like unfinished work.
_UNFINISHED = re.compile(r"(?i)\b(TODO|FIXME|XXX|HACK|NotImplementedError|raise NotImplemented)\b")


def static_scan(root: Path | None) -> list[dict[str, Any]]:
    """Run deterministic adversarial checks. Returns a list of issue dicts."""
    issues: list[dict[str, Any]] = []
    if root is None:
        return [{"severity": "block", "message": "no project directory to critique",
                 "file": "", "rule": "missing_project"}]

    for path in vc.iter_files(root):
        rel = str(path.relative_to(root))
        if path.suffix not in vc.SOURCE_SUFFIXES:
            continue
        text = vc.safe_read(path)
        if not text:
            continue
        rules = _PY_DANGER if path.suffix == ".py" else (
            _JS_DANGER if path.suffix in {".js", ".ts", ".tsx", ".jsx"} else []
        )
        for pattern, sev, msg in rules:
            if pattern.search(text):
                issues.append({"severity": sev, "message": msg, "file": rel, "rule": "danger"})
        if _SECRET.search(text):
            issues.append({"severity": "block", "message": "hardcoded secret/credential detected",
                           "file": rel, "rule": "secret"})
        # Many TODO/NotImplemented markers in the same file => unfinished delivery.
        unfinished = len(_UNFINISHED.findall(text))
        if unfinished >= 3:
            issues.append({"severity": "block",
                           "message": f"{unfinished} unfinished markers (TODO/FIXME/NotImplemented)",
                           "file": rel, "rule": "unfinished"})
        elif unfinished:
            issues.append({"severity": "warn",
                           "message": f"{unfinished} unfinished marker(s)",
                           "file": rel, "rule": "unfinished"})
    return issues


class CriticAgent(BaseAgent):
    def __init__(self, name: str = "critic", event_bus: EventBus | None = None,
                 config: dict | None = None, llm_client: Any | None = None) -> None:
        super().__init__(name, agent_type="critique", provider="local",
                         event_bus=event_bus, config=config or {})
        self.add_capability(AgentCapability(
            name="critique",
            description="Adversarial reviewer that hunts bugs/security/edge-cases and can block delivery",
            tags=("critique", "security", "adversarial"),
        ))
        self.llm = llm_client

    async def initialize(self) -> None:
        self.metadata["adversarial"] = True

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        payload = task.payload or {}
        root = vc.resolve_project_dir(payload)

        issues = static_scan(root)
        llm_issues = await self._maybe_llm_critique(payload, root)
        issues.extend(llm_issues)

        blocking = [i for i in issues if i.get("severity") == "block"]
        verdict = "block" if blocking else "pass"

        return TaskResult(
            task_id=task.task_id,
            success=True,
            output={
                "blocking_issues": blocking,
                "advisory_issues": [i for i in issues if i.get("severity") != "block"],
                "verdict": verdict,
            },
            metadata={"total_issues": len(issues), "blocking_count": len(blocking)},
        )

    async def _maybe_llm_critique(self, payload: dict[str, Any], root) -> list[dict[str, Any]]:
        if self.llm is None or getattr(self.llm, "backend", "stub") == "stub":
            return []
        brief = payload.get("brief", "")
        # Sample a few source files for context (bounded for cost — rule #5).
        sample = []
        for p in (vc.iter_files(root) if root else [])[:6]:
            if p.suffix in vc.SOURCE_SUFFIXES:
                sample.append(f"# {p.name}\n{vc.safe_read(p, max_bytes=2500)}")
        prompt = (
            "You are an adversarial code reviewer. Find concrete bugs, security "
            "issues, and unhandled edge cases that should BLOCK shipping. Reply "
            'ONLY with JSON {"blocking_issues": [{"file": str, "message": str}]}.'
            f"\n\nBrief: {brief}\n\n" + "\n\n".join(sample)
        )
        try:
            res = await self.llm.complete(prompt, tier=Tier.STRONG, json_mode=True, max_tokens=1024, task_type=self.agent_type)
            data = json.loads(res.text)
            out = []
            for item in data.get("blocking_issues", []) or []:
                out.append({
                    "severity": "block",
                    "message": str(item.get("message", "unspecified")),
                    "file": str(item.get("file", "")),
                    "rule": "llm",
                })
            return out
        except Exception:  # noqa: BLE001 - best-effort, degrade quietly
            return []
