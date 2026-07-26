from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyn3t.studio import preview_supervisor as preview_supervisor_mod
from skyn3t.studio import proof_reuse as proof_reuse_mod
from skyn3t.studio.app_runner import RunningApp
from skyn3t.studio.preview_supervisor import (
    CommandResult,
    PreviewSupervisor,
    ProofLadderCoordinator,
)
from skyn3t.studio.visual_proof import DEFAULT_VIEWPORTS


def _tool_report(
    *,
    stack: str = "react",
    docker: bool = True,
    playwright: bool = True,
    maestro: bool = True,
):
    checks = {
        "docker": SimpleNamespace(ready=docker, detail="daemon ready" if docker else "daemon down"),
        "playwright": SimpleNamespace(
            ready=playwright,
            detail="CLI ready" if playwright else "playwright missing",
        ),
        "maestro": SimpleNamespace(
            ready=maestro,
            detail="CLI ready" if maestro else "maestro missing",
        ),
    }
    return SimpleNamespace(stack=stack, checks=checks)


class _RecordingRunner:
    def __init__(self, *, run_result: CommandResult | None = None) -> None:
        self.calls: list[tuple[list[str], Path | None, float]] = []
        self.run_result = run_result or CommandResult(0, "container-id\n", "")

    async def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        del env
        self.calls.append((list(argv), cwd, timeout))
        if argv[:2] == ["docker", "logs"]:
            return CommandResult(0, "preview log", "")
        if argv[:3] == ["docker", "rm", "--force"]:
            return CommandResult(0, argv[-1], "")
        return self.run_result


def _write_node_project(root: Path, *, lockfile: bool = True) -> None:
    (root / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"dev": "vite"},
                "dependencies": {"vite": "5.4.0"},
            }
        ),
        encoding="utf-8",
    )
    if lockfile:
        (root / "package-lock.json").write_text(
            json.dumps(
                {
                    "name": "preview",
                    "lockfileVersion": 3,
                    "requires": True,
                    "packages": {
                        "": {
                            "dependencies": {"vite": "5.4.0"},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )


def test_production_code_never_constructs_legacy_host_app_runner() -> None:
    source_root = Path(__file__).parents[1] / "skyn3t"
    legacy_definition = source_root / "studio" / "app_runner.py"
    offenders = [
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if path != legacy_definition
        and re.search(r"\bAppRunner\s*\(", path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


@pytest.mark.asyncio
async def test_preview_runs_in_hardened_docker_on_localhost(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>Ready</h1>", encoding="utf-8")
    runner = _RecordingRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(),
        readiness_probe=lambda _url: True,
    )

    app = await supervisor.start(tmp_path, "static", port=43123)

    assert app.status == "running"
    assert app.url == "http://127.0.0.1:43123"
    assert app.pid is None
    assert app.detail["engine"] == "docker"
    assert app.detail["fallback_used"] is False
    prepare = next(
        call[0]
        for call in runner.calls
        if call[0][:2] == ["docker", "run"] and "--detach" not in call[0]
    )
    command = next(
        call[0]
        for call in runner.calls
        if call[0][:3] == ["docker", "run", "--detach"]
    )
    gateway = next(
        call[0]
        for call in runner.calls
        if call[0][:3] == ["docker", "run", "--detach"]
        and call[0][call[0].index("--network") + 1] == "bridge"
    )
    assert command[:3] == ["docker", "run", "--detach"]
    assert "--publish" not in command
    assert gateway[gateway.index("--publish") + 1] == "127.0.0.1:43123:43123"
    assert ["--cap-drop", "ALL"] == command[
        command.index("--cap-drop") : command.index("--cap-drop") + 2
    ]
    assert ["--security-opt", "no-new-privileges:true"] == command[
        command.index("--security-opt") : command.index("--security-opt") + 2
    ]
    source_mount = prepare[prepare.index("--mount") + 1]
    assert "target=/workspace" in source_mount and source_mount.endswith(",readonly")
    assert "rm -rf /app/.git" in prepare[-1]
    assert "/app/.skyn3t/visual-proof" in prepare[-1]
    assert "/app/.skyn3t/proof-ladder" in prepare[-1]
    assert "/app/skyn3t_manifest.json" in prepare[-1]
    assert "/app/skyn3t-observability.json" in prepare[-1]
    assert "chmod -R a+rwX /app" in prepare[-1]
    assert "chown" not in prepare[-1]
    runtime_mount = command[command.index("--mount") + 1]
    assert runtime_mount.startswith("type=volume,") and runtime_mount.endswith("target=/app")
    assert command[command.index("--network") + 1].endswith("-internal")
    assert "--read-only" in command
    assert app.detail["container_name"].startswith("skyn3t-preview-")
    assert app.detail["image"] == (
        "python:3.12-slim@"
        "sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
    )
    assert app.detail["isolation"]["image_digest_pinned"] is True
    assert app.detail["isolation"]["gateway_image_digest_pinned"] is True

    await supervisor.stop(app)
    cleanup = [call[0][:3] for call in runner.calls[-4:]]
    assert cleanup == [
        ["docker", "rm", "--force"],
        ["docker", "rm", "--force"],
        ["docker", "network", "rm"],
        ["docker", "volume", "rm"],
    ]
    assert app.status == "stopped"


@pytest.mark.asyncio
async def test_preview_container_names_are_unique(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    runner = _RecordingRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(),
        readiness_probe=lambda _url: True,
    )

    first = await supervisor.start(tmp_path, "static", port=43124)
    second = await supervisor.start(tmp_path, "static", port=43125)

    assert first.detail["container_name"] != second.detail["container_name"]
    await supervisor.stop_all()


@pytest.mark.asyncio
async def test_python_preview_installs_into_ephemeral_venv(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    runner = _RecordingRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(stack="fastapi"),
        readiness_probe=lambda _url: True,
    )

    app = await supervisor.start(tmp_path, "fastapi", port=43126)

    assert app.status == "running"
    prepare_steps = [
        call[0]
        for call in runner.calls
        if call[0][:2] == ["docker", "run"] and "--detach" not in call[0]
    ]
    dependency_prepare = next(
        command
        for command in prepare_steps
        if command[command.index("--network") + 1] == "bridge"
    )
    source_copy = next(
        command
        for command in prepare_steps
        if command[command.index("--network") + 1] == "none"
        and "cp -R /workspace/." in command[-1]
    )
    runtime = next(
        call[0]
        for call in runner.calls
        if call[0][:3] == ["docker", "run", "--detach"]
    )
    dependency_mounts = [
        dependency_prepare[index + 1]
        for index, value in enumerate(dependency_prepare)
        if value == "--mount"
    ]
    assert all("target=/workspace" not in mount for mount in dependency_mounts)
    assert any(
        f"source={tmp_path / 'requirements.txt'}" in mount
        and "target=/requirements.txt" in mount
        and mount.endswith(",readonly")
        for mount in dependency_mounts
    )
    assert "/deps/venv/bin/python -m pip install" in dependency_prepare[-1]
    assert "pip install ." not in dependency_prepare[-1]
    assert source_copy[source_copy.index("--network") + 1] == "none"
    assert "target=/workspace" in source_copy[source_copy.index("--mount") + 1]
    assert "/app/.venv" in source_copy[-1]
    assert "/app/venv" in source_copy[-1]
    assert "/app/.skyn3t/visual-proof" in source_copy[-1]
    assert "/app/skyn3t_manifest.json" in source_copy[-1]
    assert "/app/skyn3t-observability.json" in source_copy[-1]
    assert "exec /deps/venv/bin/python app.py" in runtime[-1]
    assert runtime[runtime.index("--network") + 1].endswith("-internal")
    await supervisor.stop(app)
    assert app.detail["dependency_volume_name"].endswith("-deps")
    assert [
        call[0][-1]
        for call in runner.calls
        if call[0][:4] == ["docker", "volume", "rm", "--force"]
    ][-2:] == [
        app.detail["volume_name"],
        app.detail["dependency_volume_name"],
    ]


@pytest.mark.asyncio
async def test_python_preview_rejects_pyproject_build_hooks(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\n",
        encoding="utf-8",
    )
    runner = _RecordingRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(stack="fastapi"),
        readiness_probe=lambda _url: True,
    )

    app = await supervisor.start(tmp_path, "fastapi")

    assert app.status == "failed"
    assert "requirements.txt" in app.detail["reason"]
    assert "pyproject" in app.detail["reason"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_python_preview_rejects_symlinked_requirements(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()",
        encoding="utf-8",
    )
    outside = tmp_path.parent / f"{tmp_path.name}-requirements.txt"
    outside.write_text("fastapi\n", encoding="utf-8")
    (tmp_path / "requirements.txt").symlink_to(outside)
    runner = _RecordingRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(stack="fastapi"),
        readiness_probe=lambda _url: True,
    )

    app = await supervisor.start(tmp_path, "fastapi")

    assert app.status == "failed"
    assert "regular requirements.txt" in app.detail["reason"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_docker_unavailable_is_explicit_and_never_falls_back(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    runner = _RecordingRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(docker=False),
    )

    app = await supervisor.start(tmp_path, "static")

    assert app.status == "failed"
    assert app.detail["fallback_used"] is False
    assert app.detail["engine"] == "docker"
    assert "daemon down" in app.detail["reason"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_node_preview_requires_lockfile_before_docker_commands(
    tmp_path: Path,
) -> None:
    _write_node_project(tmp_path, lockfile=False)
    runner = _RecordingRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(),
        readiness_probe=lambda _url: True,
    )

    app = await supervisor.start(tmp_path, "react")

    assert app.status == "failed"
    assert "package-lock.json" in app.detail["reason"]
    assert app.detail["fallback_used"] is False
    assert runner.calls == []


@pytest.mark.asyncio
async def test_preview_rejects_mutable_image_by_default(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    runner = _RecordingRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(),
        python_image="python:3.12-slim",
    )

    app = await supervisor.start(tmp_path, "static")

    assert app.status == "failed"
    assert "immutable sha256 digest" in app.detail["reason"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_node_prepare_is_lockfile_only_and_runtime_has_no_external_egress(
    tmp_path: Path,
) -> None:
    _write_node_project(tmp_path)
    runner = _RecordingRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(),
        readiness_probe=lambda _url: True,
    )

    app = await supervisor.start(tmp_path, "react", port=43127)

    docker_runs = [call[0] for call in runner.calls if call[0][:2] == ["docker", "run"]]
    prepare = next(command for command in docker_runs if "--detach" not in command)
    detached = [command for command in docker_runs if "--detach" in command]
    runtime = next(
        command
        for command in detached
        if command[command.index("--network") + 1] != "bridge"
    )
    gateway = next(
        command
        for command in detached
        if command[command.index("--network") + 1] == "bridge"
    )
    prepare_script = prepare[-1]
    runtime_network = runtime[runtime.index("--network") + 1]
    network_create = next(
        call[0]
        for call in runner.calls
        if call[0][:3] == ["docker", "network", "create"]
    )
    assert "npm ci --ignore-scripts --no-audit --no-fund" in prepare_script
    assert "/app/node_modules" in prepare_script
    assert "/app/.skyn3t/visual-proof" in prepare_script
    assert "npm install" not in prepare_script
    assert "||" not in prepare_script
    assert prepare[prepare.index("--network") + 1] == "bridge"
    assert "--internal" in network_create
    assert runtime_network == network_create[-1]
    assert "--publish" not in runtime
    assert gateway[gateway.index("--publish") + 1] == "127.0.0.1:43127:43127"
    assert any(
        call[0][:3] == ["docker", "network", "connect"]
        and call[0][-2:] == [runtime_network, gateway[gateway.index("--name") + 1]]
        for call in runner.calls
    )
    assert app.detail["isolation"]["runtime_egress"] == "blocked"
    assert app.detail["isolation"]["dependency_egress"] == "package-manager-only"
    assert app.detail["image"] == (
        "node:22-alpine@"
        "sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2"
    )
    await supervisor.stop(app)


class _FailPrepareRunner(_RecordingRunner):
    async def __call__(self, argv: list[str], **kwargs) -> CommandResult:
        result = await super().__call__(argv, **kwargs)
        if argv[:2] == ["docker", "run"] and "--detach" not in argv:
            return CommandResult(1, "", "lock drift")
        return result


@pytest.mark.asyncio
async def test_dependency_prepare_failure_never_launches_runtime_and_cleans_resources(
    tmp_path: Path,
) -> None:
    _write_node_project(tmp_path)
    runner = _FailPrepareRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(),
    )

    app = await supervisor.start(tmp_path, "react")

    commands = [call[0] for call in runner.calls]
    assert app.status == "failed"
    assert "dependency preparation failed" in app.detail["reason"].lower()
    assert not any(command[:2] == ["docker", "run"] and "--detach" in command for command in commands)
    assert any(command[:3] == ["docker", "volume", "rm"] for command in commands)


@pytest.mark.asyncio
async def test_readiness_timeout_collects_logs_and_removes_container(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    runner = _RecordingRunner()
    supervisor = PreviewSupervisor(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(),
        readiness_probe=lambda _url: False,
        poll_interval=0,
    )

    app = await supervisor.start(tmp_path, "static", ready_timeout=0)

    assert app.status == "failed"
    assert app.detail["fallback_used"] is False
    assert app.detail["log_tail"] == "preview log"
    assert [call[0][:3] for call in runner.calls] == [
        ["docker", "volume", "create"],
        ["docker", "run", "--rm"],
        ["docker", "network", "create"],
        ["docker", "run", "--detach"],
        ["docker", "run", "--detach"],
        ["docker", "network", "connect"],
        ["docker", "logs", "--tail"],
        ["docker", "rm", "--force"],
        ["docker", "rm", "--force"],
        ["docker", "network", "rm"],
        ["docker", "volume", "rm"],
    ]


class _FakePreviewSupervisor:
    def __init__(self, app: RunningApp) -> None:
        self.app = app
        self.started = 0
        self.stopped = 0

    async def start(self, *_args, **_kwargs) -> RunningApp:
        self.started += 1
        return self.app

    async def stop(self, _app: RunningApp) -> CommandResult:
        self.stopped += 1
        return CommandResult(0, "", "")


def _reusable_liveness_proof(
    project: Path,
    routes: tuple[str, ...] = ("/", "/settings"),
) -> tuple[dict[str, object], Path]:
    (project / "index.html").write_text("<h1>Ready</h1>", encoding="utf-8")
    (project / "dist").mkdir()
    (project / "dist" / "app.js").write_text("ready", encoding="utf-8")
    proof_root = project / ".skyn3t" / "visual-proof"
    proof_root.mkdir(parents=True)
    proofs = []
    first_screenshot = proof_root / "route-0" / "desktop.png"
    for index, route in enumerate(routes):
        route_dir = proof_root / f"route-{index}"
        route_dir.mkdir()
        viewport_proofs = []
        for viewport in DEFAULT_VIEWPORTS:
            screenshot = route_dir / f"{viewport.name}.png"
            screenshot.write_bytes(b"\x89PNG\r\n\x1a\nproof")
            viewport_proofs.append({
                **viewport.to_dict(),
                "passed": True,
                "skipped": False,
                "reason": "",
                "screenshot": screenshot.relative_to(proof_root).as_posix(),
                "metrics": {},
                "issues": [],
                "console_errors": [],
                "page_errors": [],
            })
        proof = {
            "schema_version": 1,
            "url": f"http://127.0.0.1:44000{route}",
            "route": route,
            "stack": "react",
            "passed": True,
            "skipped": False,
            "reason": "",
            "report_path": f"route-{index}/report.json",
            "viewports": viewport_proofs,
        }
        (route_dir / "report.json").write_text(
            json.dumps(proof, sort_keys=True),
            encoding="utf-8",
        )
        proofs.append(proof)
    (proof_root / "visual-proof.json").write_text(
        json.dumps({
            "schema_version": 1,
            "stack": "react",
            "passed": True,
            "skipped": False,
            "viewports": [viewport.to_dict() for viewport in DEFAULT_VIEWPORTS],
            "routes": proofs,
        }, sort_keys=True),
        encoding="utf-8",
    )
    runtime_fingerprint = proof_reuse_mod.preview_input_fingerprint(project, "react")
    return (
        preview_supervisor_mod.build_reusable_web_proof(
            project,
            "react",
            artifact_dir=".skyn3t/visual-proof",
            runtime_input_fingerprint=runtime_fingerprint,
        ),
        first_screenshot,
    )


@pytest.mark.asyncio
async def test_web_proof_reuses_digest_bound_liveness_evidence(
    tmp_path: Path,
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    candidate, _ = _reusable_liveness_proof(project)
    preview = _FakePreviewSupervisor(
        RunningApp("", 0, None, "none", str(project), status="failed")
    )

    def unexpected_toolchain(**_kwargs):
        raise AssertionError("cache hit must not repeat toolchain inspection")

    coordinator = ProofLadderCoordinator(
        preview_supervisor=preview,
        toolchain_inspector=unexpected_toolchain,
    )
    result = await coordinator.run(
        project,
        "react",
        routes=("/", "/settings"),
        artifact_dir=tmp_path / "proof",
        reusable_web_proof=candidate,
    )

    assert result.passed is True
    assert result.cache_hit is True
    assert result.reused_from == "liveness"
    assert preview.started == 0 and preview.stopped == 0
    assert [step.name for step in result.steps] == ["preview", "playwright"]
    assert (tmp_path / "proof/playwright/visual-proof.json").is_file()
    persisted = json.loads(
        (tmp_path / "proof/proof-ladder.json").read_text(encoding="utf-8")
    )
    assert persisted["cache_hit"] is True
    assert persisted["reused_from"] == "liveness"

    repeated = await coordinator.run(
        project,
        "react",
        routes=("/", "/settings"),
        artifact_dir=tmp_path / "proof",
        reusable_web_proof=candidate,
    )
    assert repeated.passed is True
    assert repeated.cache_hit is True
    assert preview.started == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    (
        "runtime_input",
        "report",
        "screenshot",
        "routes",
        "viewports",
        "source_dir",
        "symlink",
    ),
)
async def test_web_proof_falls_back_when_reuse_is_uncertain_or_tampered(
    tmp_path: Path,
    tamper: str,
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    candidate, screenshot = _reusable_liveness_proof(project)
    proof_root = project / ".skyn3t" / "visual-proof"
    if tamper == "runtime_input":
        (project / "dist" / "app.js").write_text("changed", encoding="utf-8")
    elif tamper == "report":
        (proof_root / "visual-proof.json").write_text("{}", encoding="utf-8")
    elif tamper == "screenshot":
        screenshot.write_bytes(b"changed")
    elif tamper == "routes":
        candidate["routes"] = ["/"]
    elif tamper == "viewports":
        candidate["viewports"] = [{"name": "desktop", "width": 1024, "height": 768}]
    elif tamper == "source_dir":
        candidate["artifact_dir"] = "dist"
    else:
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"outside")
        screenshot.unlink()
        screenshot.symlink_to(outside)

    preview = _FakePreviewSupervisor(RunningApp(
        "http://127.0.0.1:44001",
        44001,
        None,
        "static",
        str(project),
        status="running",
    ))

    def visual_auditor(pages, artifact_dir, **_kwargs):
        Path(artifact_dir).mkdir(parents=True, exist_ok=True)
        (Path(artifact_dir) / "visual-proof.json").write_text(
            '{"passed": true}', encoding="utf-8"
        )
        return [
            SimpleNamespace(
                passed=True,
                skipped=False,
                reason="",
                to_dict=lambda route=route: {"route": route, "passed": True},
            )
            for route, _url in pages
        ]

    result = await ProofLadderCoordinator(
        preview_supervisor=preview,
        toolchain_inspector=lambda **_: _tool_report(),
        visual_auditor=visual_auditor,
    ).run(
        project,
        "react",
        routes=("/", "/settings"),
        artifact_dir=tmp_path / "proof",
        reusable_web_proof=candidate,
    )

    assert result.passed is True
    assert result.cache_hit is False
    assert result.reused_from is None
    assert result.reuse_miss_reason
    assert preview.started == 1 and preview.stopped == 1


@pytest.mark.asyncio
async def test_web_proof_reuse_never_follows_destination_symlink(
    tmp_path: Path,
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    candidate, _ = _reusable_liveness_proof(project)
    artifact_root = tmp_path / "proof"
    artifact_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (artifact_root / "playwright").symlink_to(outside, target_is_directory=True)
    preview = _FakePreviewSupervisor(
        RunningApp(
            "http://127.0.0.1:44002",
            44002,
            None,
            "static",
            str(project),
            status="running",
        )
    )
    audited = False

    def unsafe_auditor(*_args, **_kwargs):
        nonlocal audited
        audited = True
        sentinel.write_text("overwritten", encoding="utf-8")
        return []

    result = await ProofLadderCoordinator(
        preview_supervisor=preview,
        toolchain_inspector=lambda **_: _tool_report(),
        visual_auditor=unsafe_auditor,
    ).run(
        project,
        "react",
        routes=("/", "/settings"),
        artifact_dir=artifact_root,
        reusable_web_proof=candidate,
    )

    assert result.cache_hit is False
    assert result.status == "failed"
    assert result.steps[0].name == "artifact_store"
    assert preview.started == 0
    assert audited is False
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert (artifact_root / "playwright").is_symlink()


@pytest.mark.asyncio
async def test_web_proof_reuse_never_deletes_unowned_destination(
    tmp_path: Path,
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    candidate, _ = _reusable_liveness_proof(project)
    artifact_root = tmp_path / "proof"
    destination = artifact_root / "playwright"
    destination.mkdir(parents=True)
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    preview = _FakePreviewSupervisor(
        RunningApp(
            "http://127.0.0.1:44003",
            44003,
            None,
            "static",
            str(project),
            status="running",
        )
    )

    result = await ProofLadderCoordinator(
        preview_supervisor=preview,
        toolchain_inspector=lambda **_: _tool_report(),
    ).run(
        project,
        "react",
        routes=("/", "/settings"),
        artifact_dir=artifact_root,
        reusable_web_proof=candidate,
    )

    assert result.status == "failed"
    assert result.steps[0].name == "artifact_store"
    assert preview.started == 0
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_web_proof_never_writes_through_aliased_artifact_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    artifact_root = tmp_path / "proof"
    artifact_root.symlink_to(outside, target_is_directory=True)
    preview = _FakePreviewSupervisor(
        RunningApp(
            "http://127.0.0.1:44004",
            44004,
            None,
            "static",
            str(project),
            status="running",
        )
    )

    result = await ProofLadderCoordinator(
        preview_supervisor=preview,
        toolchain_inspector=lambda **_: _tool_report(),
    ).run(project, "react", artifact_dir=artifact_root)

    assert result.status == "failed"
    assert result.steps[0].name == "artifact_store"
    assert preview.started == 0
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(outside.iterdir()) == [sentinel]


def test_reusable_proof_requires_matching_pre_and_post_runtime_fingerprint(
    tmp_path: Path,
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    candidate, _ = _reusable_liveness_proof(project)
    before = candidate["runtime_input_fingerprint"]
    (project / "dist" / "app.js").write_text("changed after audit", encoding="utf-8")

    with pytest.raises(ValueError, match="changed"):
        preview_supervisor_mod.build_reusable_web_proof(
            project,
            "react",
            artifact_dir=".skyn3t/visual-proof",
            runtime_input_fingerprint=before,
        )


@pytest.mark.parametrize("runtime_dir", ("dist", "build", "out"))
def test_reusable_proof_artifacts_cannot_hide_runtime_output(
    tmp_path: Path,
    runtime_dir: str,
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    (project / "index.html").write_text("ready", encoding="utf-8")
    (project / runtime_dir).mkdir()

    with pytest.raises(ValueError, match="visual-proof"):
        preview_supervisor_mod.build_reusable_web_proof(
            project,
            "react",
            artifact_dir=runtime_dir,
            runtime_input_fingerprint=proof_reuse_mod.preview_input_fingerprint(
                project,
                "react",
            ),
        )


def test_runtime_fingerprint_fails_closed_on_walk_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "index.html").write_text("ready", encoding="utf-8")

    def broken_walk(*_args, onerror=None, **_kwargs):
        assert onerror is not None
        onerror(OSError("walk failed"))
        return iter(())

    monkeypatch.setattr(proof_reuse_mod.os, "walk", broken_walk)

    with pytest.raises(ValueError, match="walk failed"):
        proof_reuse_mod.preview_input_fingerprint(tmp_path, "react")


def test_runtime_fingerprint_rejects_file_swapped_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "index.html").write_text("ready", encoding="utf-8")
    real_fstat = proof_reuse_mod.os.fstat

    def mismatched_fstat(descriptor):
        metadata = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino + 1,
            st_size=metadata.st_size,
            st_mtime_ns=metadata.st_mtime_ns,
        )

    monkeypatch.setattr(proof_reuse_mod.os, "fstat", mismatched_fstat)

    with pytest.raises(ValueError, match="changed"):
        proof_reuse_mod.preview_input_fingerprint(tmp_path, "react")


def test_runtime_fingerprint_ignores_only_internal_proof_parent_churn(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.html").write_text("ready", encoding="utf-8")
    before = proof_reuse_mod.preview_input_fingerprint(tmp_path, "react")
    proof_root = tmp_path / ".skyn3t" / "visual-proof"
    proof_root.mkdir(parents=True)
    (proof_root / "visual-proof.json").write_text("{}", encoding="utf-8")

    after_proof = proof_reuse_mod.preview_input_fingerprint(tmp_path, "react")
    assert after_proof == before

    (tmp_path / "skyn3t_manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "skyn3t-observability.json").write_text("{}", encoding="utf-8")
    after_runner_metadata = proof_reuse_mod.preview_input_fingerprint(
        tmp_path,
        "react",
    )
    assert after_runner_metadata == before

    nested_runner_lookalike = tmp_path / "public" / "skyn3t-observability.json"
    nested_runner_lookalike.parent.mkdir()
    nested_runner_lookalike.write_text("{}", encoding="utf-8")
    after_authored_lookalike = proof_reuse_mod.preview_input_fingerprint(
        tmp_path,
        "react",
    )
    assert after_authored_lookalike != before

    (tmp_path / ".skyn3t" / "product.json").write_text("{}", encoding="utf-8")
    after_product = proof_reuse_mod.preview_input_fingerprint(tmp_path, "react")
    assert after_product != before


@pytest.mark.asyncio
async def test_web_proof_persists_playwright_evidence_and_stops_preview(
    tmp_path: Path,
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    artifact_root = tmp_path / "proof"
    (artifact_root / "playwright").mkdir(parents=True)
    preview = _FakePreviewSupervisor(
        RunningApp(
            url="http://127.0.0.1:44000",
            port=44000,
            pid=None,
            kind="static",
            project_dir=str(project),
            status="running",
            detail={"container_name": "preview"},
        )
    )

    def visual_auditor(pages, artifact_dir, **_kwargs):
        Path(artifact_dir).mkdir(parents=True, exist_ok=True)
        (Path(artifact_dir) / "visual-proof.json").write_text(
            '{"passed": true}',
            encoding="utf-8",
        )
        return [
            SimpleNamespace(
                passed=True,
                skipped=False,
                reason="",
                report_path="routes/home.json",
                to_dict=lambda route=route, url=url: {
                    "route": route,
                    "url": url,
                    "passed": True,
                    "skipped": False,
                },
            )
            for route, url in pages
        ]

    coordinator = ProofLadderCoordinator(
        preview_supervisor=preview,
        toolchain_inspector=lambda **_: _tool_report(),
        visual_auditor=visual_auditor,
    )
    result = await coordinator.run(
        project,
        "react",
        routes=("/", "/settings"),
        artifact_dir=artifact_root,
    )

    assert result.passed is True
    assert result.status == "passed"
    assert preview.started == 1 and preview.stopped == 1
    report = json.loads((artifact_root / "proof-ladder.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert [step["name"] for step in report["steps"]] == ["preview", "playwright"]
    assert report["steps"][1]["detail"]["routes"] == ["/", "/settings"]


@pytest.mark.asyncio
async def test_missing_playwright_is_required_skip_not_pass(tmp_path: Path) -> None:
    preview = _FakePreviewSupervisor(
        RunningApp("", 0, None, "none", str(tmp_path), status="failed")
    )
    coordinator = ProofLadderCoordinator(
        preview_supervisor=preview,
        toolchain_inspector=lambda **_: _tool_report(playwright=False),
    )

    result = await coordinator.run(tmp_path, "react")

    assert result.passed is False
    assert result.status == "skipped"
    assert result.steps[0].name == "playwright"
    assert result.steps[0].required is True
    assert result.steps[0].status == "skipped"
    assert preview.started == 0


@pytest.mark.asyncio
async def test_playwright_success_without_batch_artifact_is_failed(tmp_path: Path) -> None:
    preview = _FakePreviewSupervisor(
        RunningApp(
            "http://127.0.0.1:44001",
            44001,
            None,
            "static",
            str(tmp_path),
            status="running",
        )
    )
    coordinator = ProofLadderCoordinator(
        preview_supervisor=preview,
        toolchain_inspector=lambda **_: _tool_report(),
        visual_auditor=lambda *_args, **_kwargs: [
            SimpleNamespace(
                passed=True,
                skipped=False,
                reason="",
                to_dict=lambda: {"passed": True, "skipped": False},
            )
        ],
    )

    result = await coordinator.run(tmp_path, "static")

    assert result.passed is False
    assert result.steps[1].name == "playwright"
    assert result.steps[1].status == "failed"
    assert "report" in result.steps[1].reason.lower()


class _MaestroRunner(_RecordingRunner):
    async def __call__(self, argv: list[str], **kwargs) -> CommandResult:
        result = await super().__call__(argv, **kwargs)
        if argv and argv[0] == "maestro":
            report = Path(argv[argv.index("--output") + 1])
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("<testsuite failures='0'/>", encoding="utf-8")
        return result


@pytest.mark.asyncio
async def test_react_native_runs_each_maestro_flow_and_persists_reports(
    tmp_path: Path,
) -> None:
    flow_dir = tmp_path / ".maestro"
    flow_dir.mkdir()
    (flow_dir / "login.yaml").write_text("appId: test.app\n---\n- launchApp", encoding="utf-8")
    (flow_dir / "settings.yml").write_text("appId: test.app\n---\n- launchApp", encoding="utf-8")
    runner = _MaestroRunner()
    coordinator = ProofLadderCoordinator(
        command_runner=runner,
        toolchain_inspector=lambda **_: _tool_report(stack="react_native"),
        maestro_binary="maestro",
    )

    result = await coordinator.run(
        tmp_path,
        "react_native",
        reusable_web_proof={"source": "liveness", "passed": True},
    )

    assert result.passed is True
    assert result.status == "passed"
    assert result.cache_hit is False
    assert result.reused_from is None
    maestro_calls = [call[0] for call in runner.calls if call[0][0] == "maestro"]
    assert len(maestro_calls) == 2
    assert all("--format" in call and "junit" in call for call in maestro_calls)
    assert all("--test-output-dir" in call for call in maestro_calls)
    assert len(list((tmp_path / ".skyn3t/proof-ladder/maestro").glob("*.xml"))) == 2


@pytest.mark.asyncio
async def test_missing_maestro_or_flows_never_passes_mobile_proof(tmp_path: Path) -> None:
    flow_dir = tmp_path / ".maestro"
    flow_dir.mkdir()
    flow = flow_dir / "smoke.yaml"
    flow.write_text("appId: test.app\n---\n- launchApp", encoding="utf-8")
    missing_tool = ProofLadderCoordinator(
        toolchain_inspector=lambda **_: _tool_report(stack="react_native", maestro=False),
    )

    missing_result = await missing_tool.run(tmp_path, "react_native")
    assert missing_result.passed is False
    assert missing_result.steps[0].status == "skipped"

    flow.unlink()
    no_flows = ProofLadderCoordinator(
        toolchain_inspector=lambda **_: _tool_report(stack="react_native"),
    )
    no_flow_result = await no_flows.run(tmp_path, "react_native")
    assert no_flow_result.passed is False
    assert no_flow_result.steps[0].status == "skipped"
    assert "flow" in no_flow_result.steps[0].reason.lower()
