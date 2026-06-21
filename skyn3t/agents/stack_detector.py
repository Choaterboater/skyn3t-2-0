"""StackDetector — infer a project's stack family from its manifests.

Pure, offline, side-effect free. Reads files from a directory and classifies
the scaffold into one of {web, server, fullstack, unknown} so downstream
agents (packaging, deploy, browser self-heal) know how to treat it.

Usable both as a plain helper (``StackDetector.detect(dir)``) and as a
:class:`BaseAgent` registered under agent_type ``stack_detect``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

# Marker filenames that strongly imply a web frontend.
_WEB_DEP_HINTS = (
    "react", "react-dom", "next", "vite", "vue", "svelte", "@sveltejs",
    "solid-js", "preact", "astro", "@angular/core", "vue-router",
)
# Marker filenames / deps that imply a backend server.
_SERVER_DEP_HINTS = (
    "express", "fastify", "koa", "@nestjs/core", "hapi", "apollo-server",
    "fastapi", "flask", "django", "starlette", "uvicorn", "gunicorn",
    "aiohttp", "sanic", "tornado",
)
_WEB_CONFIG_FILES = (
    "vite.config.js", "vite.config.ts", "next.config.js", "next.config.mjs",
    "svelte.config.js", "astro.config.mjs", "vue.config.js", "angular.json",
    "index.html",
)
_SERVER_CONFIG_FILES = (
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "Procfile", "wsgi.py", "asgi.py", "manage.py",
)


@dataclass(slots=True)
class StackReport:
    family: str = "unknown"  # web | server | fullstack | unknown
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    has_web: bool = False
    has_server: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "languages": self.languages,
            "frameworks": self.frameworks,
            "manifests": self.manifests,
            "has_web": self.has_web,
            "has_server": self.has_server,
            "reasons": self.reasons,
        }


class StackDetector:
    """Classifies a project directory by reading its manifests."""

    @staticmethod
    def _read_package_json(root: Path) -> dict[str, Any]:
        pkg = root / "package.json"
        if not pkg.is_file():
            return {}
        try:
            return json.loads(pkg.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    @classmethod
    def analyze(cls, directory: str | Path) -> StackReport:
        root = Path(directory)
        report = StackReport()
        if not root.is_dir():
            report.reasons.append(f"not a directory: {root}")
            return report

        web_score = 0
        server_score = 0

        # ---- JavaScript / TypeScript ecosystem ----
        pkg = cls._read_package_json(root)
        if pkg:
            report.manifests.append("package.json")
            report.languages.append("javascript")
            deps = {}
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                deps.update(pkg.get(key, {}) or {})
            dep_names = {d.lower() for d in deps}
            # Mobile (Expo / React Native) is NEITHER web nor server — it runs on
            # a device/simulator. Detect it BEFORE the web/server scoring so the
            # packaging agent doesn't slap a Dockerfile / compose / .env / web
            # config onto a mobile app (which has `react` in deps + a `start`
            # script, both of which would otherwise misclassify it as fullstack).
            if {"expo", "react-native"} & dep_names:
                report.family = "mobile"
                report.frameworks.append(
                    "expo" if "expo" in dep_names else "react-native"
                )
                report.reasons.append("mobile app (expo/react-native deps)")
                return report
            for hint in _WEB_DEP_HINTS:
                if hint in dep_names:
                    web_score += 2
                    report.frameworks.append(hint)
                    report.reasons.append(f"web dep: {hint}")
            for hint in _SERVER_DEP_HINTS:
                if hint in dep_names:
                    server_score += 2
                    report.frameworks.append(hint)
                    report.reasons.append(f"server dep: {hint}")
            scripts = pkg.get("scripts", {}) or {}
            if any(k in scripts for k in ("start", "serve")) and not any(
                k in scripts for k in ("build", "dev")
            ):
                server_score += 1

        # ---- Python ecosystem ----
        py_manifests = (
            "pyproject.toml", "requirements.txt", "Pipfile", "setup.py", "setup.cfg",
        )
        py_text = ""
        for m in py_manifests:
            p = root / m
            if p.is_file():
                report.manifests.append(m)
                if "python" not in report.languages:
                    report.languages.append("python")
                try:
                    py_text += "\n" + p.read_text(encoding="utf-8").lower()
                except OSError:
                    pass
        for hint in _SERVER_DEP_HINTS:
            if hint in py_text:
                server_score += 2
                if hint not in report.frameworks:
                    report.frameworks.append(hint)
                report.reasons.append(f"python server dep: {hint}")

        # ---- Config file markers ----
        for fname in _WEB_CONFIG_FILES:
            if (root / fname).is_file():
                web_score += 1
                report.reasons.append(f"web config: {fname}")
        for fname in _SERVER_CONFIG_FILES:
            if (root / fname).is_file():
                server_score += 1
                report.reasons.append(f"server config: {fname}")

        # ---- Directory markers ----
        if (root / "public").is_dir() or (root / "src" / "App.jsx").exists() \
                or (root / "src" / "App.tsx").exists():
            web_score += 1
        if (root / "api").is_dir() or (root / "server").is_dir():
            server_score += 1

        report.has_web = web_score > 0
        report.has_server = server_score > 0

        if web_score > 0 and server_score > 0:
            report.family = "fullstack"
        elif web_score > 0:
            report.family = "web"
        elif server_score > 0:
            report.family = "server"
        else:
            report.family = "unknown"

        # de-dupe frameworks preserving order
        seen: set[str] = set()
        report.frameworks = [f for f in report.frameworks if not (f in seen or seen.add(f))]
        return report

    @classmethod
    def detect(cls, directory: str | Path) -> str:
        """Return just the family string for the directory."""
        return cls.analyze(directory).family


class StackDetectorAgent(BaseAgent):
    """Agent wrapper around :class:`StackDetector`."""

    def __init__(self, name: str = "stack_detector", event_bus: EventBus | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(name, agent_type="stack_detect", provider="local",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="stack_detect",
            description="Classifies a scaffold into web/server/fullstack/unknown",
            tags=("analysis",),
        ))

    async def initialize(self) -> None:
        self.metadata["ready"] = True

    async def execute(self, task: TaskRequest) -> TaskResult:
        target = (task.payload.get("project_dir")
                  or task.payload.get("worktree_dir")
                  or task.payload.get("dir"))
        if not target:
            return TaskResult(task_id=task.task_id, success=False,
                              error="no project_dir in payload")
        report = StackDetector.analyze(target)
        return TaskResult(task_id=task.task_id, success=True, output=report.to_dict())

    async def health_check(self) -> bool:
        return True
