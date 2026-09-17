"""Unverified partial edits survive provider failure, not as delivered source."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.persistence.checkpoint import CheckpointManager
from skyn3t.studio.improve import ImproveEngine
from skyn3t.worktree import merge_back, source_tree_snapshot

pytestmark = pytest.mark.skipif(
    not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
    or os.open not in os.supports_dir_fd,
    reason="Unverified candidate storage requires no-follow descriptor-relative file access",
)


def _archive_dir(settings: Settings) -> Path:
    return settings.data_dir / ".skyn3t-recovery" / "improve_candidates"


class _FailingWriter:
    backend = "openrouter"

    def __init__(self, settings: Settings, *, raise_after_write=False) -> None:
        self.settings = settings
        self.raise_after_write = raise_after_write

    async def agentic_build(self, prompt, workdir, **kwargs):
        Path(workdir, "main.py").write_text("value = 2\n")
        if self.raise_after_write:
            raise TimeoutError("provider timeout after writing source")
        return {
            "ok": False, "backend": "openrouter", "provider_requests": 2,
            "files_written": 1, "error": "429 provider capacity exhausted",
        }

    async def complete(self, *args, **kwargs):
        raise AssertionError("An executed failure must not start per-file rewriting")


@pytest.fixture
def candidate_project(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "state",
        projects_dir=tmp_path / "projects",
        logs_dir=tmp_path / "logs",
        vector_db_path=tmp_path / "vectors",
        llm_backend="openrouter",
    )
    project = tmp_path / "worktree"
    project.mkdir()
    (project / "main.py").write_text("value = 1\n")
    return settings, project


async def _run(settings, project, writer=None):
    agent = CodeImproverAgent(event_bus=EventBus(), llm=writer or _FailingWriter(settings))
    await agent.start()
    return await agent.run(TaskRequest(
        type="code_improve",
        payload={
            "worktree_dir": str(project), "brief": "Improve the value",
            "gaps": ["Improve the value"], "stack": "python",
            "agentic": True, "existing_project": True,
        },
    ))


async def test_partial_candidate_survives_failure_and_worktree_cleanup(candidate_project):
    settings, project = candidate_project
    base_hash = source_tree_snapshot(project)["sha256"]
    result = await _run(settings, project)
    assert not result.success
    assert result.retryable is False
    assert (project / "main.py").read_text() == "value = 1\n"
    retained = result.output["candidate_retention"]
    assert retained["status"] == "unverified"
    receipt_path = Path(retained["path"])
    assert receipt_path.is_relative_to(settings.data_dir)
    assert not receipt_path.is_relative_to(project)
    (project / "main.py").unlink()
    project.rmdir()

    state = json.loads(receipt_path.read_text())["state"]
    assert state["status"] == "unverified"
    assert state["delivered"] is False and state["proof_passed"] is False
    assert state["base_source_sha256"] == base_hash
    saved = state["files"]["main.py"]
    assert saved["content"] == "value = 2\n"
    assert saved["sha256"] == hashlib.sha256(b"value = 2\n").hexdigest()
    assert not (settings.data_dir / "checkpoints" / "latest.json").exists()


async def test_candidate_access_time_changes_do_not_discard_unchanged_content(
    candidate_project, monkeypatch
):
    settings, project = candidate_project
    fstat = os.fstat

    def update_access_time(fd):
        current = fstat(fd)
        os.utime(fd, ns=(current.st_atime_ns + 2_000_000_000, current.st_mtime_ns))
        return fstat(fd)

    monkeypatch.setattr(os, "fstat", update_access_time)
    result = await _run(settings, project)
    assert result.output["candidate_retention"]["status"] == "unverified"
    assert (project / "main.py").read_text() == "value = 1\n"


async def test_raised_provider_error_keeps_candidate_without_replaying(candidate_project):
    settings, project = candidate_project
    result = await _run(settings, project, _FailingWriter(settings, raise_after_write=True))
    assert not result.success and result.retryable is False
    assert "provider timeout after writing source" in result.error
    retained = result.output["candidate_retention"]
    assert retained["status"] == "unverified"
    assert retained["path"] in result.error
    assert (project / "main.py").read_text() == "value = 1\n"


async def test_candidate_omits_private_secret_binary_and_symlinked_source(candidate_project):
    settings, project = candidate_project
    known_secret = "unit-test-only-credential-not-a-real-key"
    patterned_secret = "sk-or-" + "x" * 32
    settings.openrouter_api_key = known_secret
    outside = project.parent / "outside.py"
    outside.write_text("private_outside_value = 99\n")

    class UnsafeWriter(_FailingWriter):
        async def agentic_build(self, prompt, workdir, **kwargs):
            result = await super().agentic_build(prompt, workdir, **kwargs)
            root = Path(workdir)
            (root / ".env").write_text("PRIVATE_SETTING=do-not-archive\n")
            (root / "known.py").write_text(f"key = {known_secret!r}\n")
            (root / "patterned.py").write_text(f"key = {patterned_secret!r}\n")
            (root / "binary.py").write_bytes(b"\x00do-not-archive-binary")
            (root / "linked.py").symlink_to(outside)
            (root / "alias").symlink_to(project.parent, target_is_directory=True)
            return result

    result = await _run(settings, project, UnsafeWriter(settings))
    raw = Path(result.output["candidate_retention"]["path"]).read_text()
    state = json.loads(raw)["state"]
    assert set(state["files"]) == {"main.py"}
    assert state["complete_backup"] is False
    for excluded in (known_secret, patterned_secret, "do-not-archive", "private_outside_value"):
        assert excluded not in raw
    assert outside.read_text() == "private_outside_value = 99\n"
    assert (project / "main.py").read_text() == "value = 1\n"


async def test_candidate_bounds_serialized_unicode_bytes_and_file_count(candidate_project):
    settings, project = candidate_project

    class ManyFilesWriter(_FailingWriter):
        async def agentic_build(self, prompt, workdir, **kwargs):
            result = await super().agentic_build(prompt, workdir, **kwargs)
            root = Path(workdir)
            (root / "a_large.py").write_text("# " + "\u03b1" * 400_000 + "\n")
            for index in range(25):
                (root / f"b_{index:02}.py").write_text(f"value = {index}\n")
            return result

    result = await _run(settings, project, ManyFilesWriter(settings))
    raw = Path(result.output["candidate_retention"]["path"]).read_bytes()
    state = json.loads(raw)["state"]
    assert len(raw) <= 2 * 1024 * 1024
    assert len(state["files"]) == 20
    assert "a_large.py" not in state["files"]
    assert state["omitted"]["archive_size"] == 1
    assert state["omitted"]["file_limit"] > 0
    assert (project / "main.py").read_text() == "value = 1\n"


async def test_candidate_retention_keeps_ten_own_receipts_across_clock_changes(candidate_project):
    settings, project = candidate_project
    CheckpointManager(settings=settings).save("boot", state={"phase": "original"})
    boot_latest = settings.data_dir / "checkpoints" / "latest.json"
    original_boot = boot_latest.read_bytes()
    directory = _archive_dir(settings)
    directory.mkdir(parents=True)
    (directory / "notes.json").write_text("keep this unrelated file")
    previous = []
    for _ in range(10):
        result = await _run(settings, project)
        previous.append(Path(result.output["candidate_retention"]["path"]))
    for path in previous:
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 3_600_000_000_000))

    result = await _run(settings, project)
    newest = Path(result.output["candidate_retention"]["path"])
    remaining = set(directory.glob("candidate-*.json"))
    assert len(remaining) == 10
    assert newest in remaining
    assert len(remaining.intersection(previous)) == 9
    assert (directory / "notes.json").read_text() == "keep this unrelated file"
    assert boot_latest.read_bytes() == original_boot


async def test_candidate_storage_failure_does_not_mask_failure_or_prevent_restore(candidate_project):
    settings, project = candidate_project
    directory = _archive_dir(settings)
    directory.parent.mkdir()
    directory.write_text("occupied by a file")
    result = await _run(settings, project)
    assert not result.success and result.retryable is False
    assert result.output["candidate_retention"]["status"] == "unavailable"
    assert "429 provider capacity exhausted" in result.error
    assert "candidate retention unavailable" in result.error
    assert (project / "main.py").read_text() == "value = 1\n"


@pytest.mark.parametrize("successful", [False, True])
async def test_no_candidate_without_failed_source_changes(candidate_project, successful):
    settings, project = candidate_project

    class ConditionalWriter(_FailingWriter):
        async def agentic_build(self, prompt, workdir, **kwargs):
            if successful:
                result = await super().agentic_build(prompt, workdir, **kwargs)
                result["ok"] = True
                return result
            return {"ok": False, "provider_requests": 1, "error": "provider declined"}

    result = await _run(settings, project, ConditionalWriter(settings))
    assert result.success is successful
    assert result.output.get("candidate_retention", {}).get("status") != "unverified"
    assert not list(_archive_dir(settings).glob("*.json"))
    assert (project / "main.py").read_text() == ("value = 2\n" if successful else "value = 1\n")


async def test_engine_surfaces_unverified_bytes_after_real_worktree_cleanup(candidate_project):
    settings, project = candidate_project
    settings.llm_backend = "stub"
    settings.run_generated_tests = False
    settings.run_generated_build = False
    settings.web_interact_check_enabled = False
    original = b"value = 1\r\n"
    candidate = b"value = 2\r\n"
    (project / "main.py").write_bytes(original)
    base_hash = source_tree_snapshot(project)["sha256"]
    worktrees = []

    class WindowsWriter(_FailingWriter):
        async def agentic_build(self, prompt, workdir, **kwargs):
            worktrees.append(Path(workdir))
            result = await super().agentic_build(prompt, workdir, **kwargs)
            Path(workdir, "main.py").write_bytes(candidate)
            return result

    bus = EventBus()
    orchestrator = Orchestrator(bus)
    await orchestrator.register(CodeImproverAgent(event_bus=bus, llm=WindowsWriter(settings)))
    engine = ImproveEngine(bus, orchestrator, settings=settings)
    outcome = await engine.improve(str(project), "Improve the value")
    assert outcome.status == "failed"
    assert len(worktrees) == 1 and not worktrees[0].exists()
    assert (project / "main.py").read_bytes() == original
    receipts = list(_archive_dir(settings).glob("candidate-*.json"))
    assert len(receipts) == 1
    assert str(receipts[0]) in json.dumps(outcome.to_dict())
    state = json.loads(receipts[0].read_text())["state"]
    assert state["base_source_sha256"] == base_hash
    assert state["files"]["main.py"]["content"].encode("utf-8") == candidate
    assert state["files"]["main.py"]["sha256"] == hashlib.sha256(candidate).hexdigest()


@pytest.mark.parametrize("storage_failed", [False, True])
async def test_engine_retains_completed_generation_when_proof_rejects_it(
    candidate_project, monkeypatch, storage_failed,
):
    settings, project = candidate_project
    settings.llm_backend = "stub"
    settings.run_generated_tests = False
    settings.run_generated_build = False
    settings.web_interact_check_enabled = False
    original = (project / "main.py").read_bytes()
    base_hash = source_tree_snapshot(project)["sha256"]
    worktrees = []

    class UnverifiedWriter(_FailingWriter):
        async def agentic_build(self, prompt, workdir, **kwargs):
            worktrees.append(Path(workdir))
            Path(workdir, "main.py").write_text("value = 2\n")
            return {"ok": True, "backend": "openrouter", "files_written": 1}

    bus = EventBus()
    orchestrator = Orchestrator(bus)
    await orchestrator.register(CodeImproverAgent(event_bus=bus, llm=UnverifiedWriter(settings)))
    engine = ImproveEngine(bus, orchestrator, settings=settings)

    from unittest.mock import patch

    from skyn3t.persistence.candidate_archive import CandidateArchive
    from skyn3t.studio.proof_run import ProofResult

    if storage_failed:
        def fail_storage(*args):
            raise OSError("storage unavailable")

        monkeypatch.setattr(CandidateArchive, "save", fail_storage)
    with patch("skyn3t.studio.improve.proof_run", return_value=ProofResult(
        passed=False, mode="inline",
        detail={"tests": "failed", "tests_summary": "one failing assertion"},
    )):
        outcome = await engine.improve(str(project), "Improve the value")

    assert outcome.status == "failed"
    assert outcome.detail["delivery_blocked"] == "proof_failed"
    assert (project / "main.py").read_bytes() == original
    assert len(worktrees) == 1 and not worktrees[0].exists()
    retained = outcome.detail["candidate_retention"]
    if storage_failed:
        assert retained == {"status": "unavailable", "reason": "OSError"}
        return
    state = json.loads(Path(retained["path"]).read_text())["state"]
    assert retained["status"] == "unverified"
    assert state["base_source_sha256"] == base_hash
    assert state["files"]["main.py"]["content"] == "value = 2\n"
    assert state["delivered"] is False and state["proof_passed"] is False


async def test_candidate_directory_swap_cannot_open_outside_files(candidate_project, monkeypatch):
    settings, project = candidate_project
    source = b"value = 3\n"
    outside = project.parent / "outside"
    outside.mkdir()
    (outside / "new.py").write_bytes(source)
    outside_inode = (outside / "new.py").stat().st_ino
    opened_outside = []
    original_open = os.open
    swapped = False

    class NestedWriter(_FailingWriter):
        async def agentic_build(self, prompt, workdir, **kwargs):
            result = await super().agentic_build(prompt, workdir, **kwargs)
            (Path(workdir) / "src").mkdir()
            (Path(workdir) / "src" / "new.py").write_bytes(source)
            return result

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and str(path) in {str(project / "src" / "new.py"), "new.py"}:
            (project / "src").rename(project / "moved")
            (project / "src").symlink_to(outside, target_is_directory=True)
            swapped = True
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if os.fstat(fd).st_ino == outside_inode:
            opened_outside.append(str(path))
        return fd

    monkeypatch.setattr(os, "open", swap_before_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {swap_before_open})
    result = await _run(settings, project, NestedWriter(settings))
    assert swapped
    assert opened_outside == []
    assert result.output["candidate_retention"]["status"] == "unverified"
    assert (outside / "new.py").read_bytes() == source


async def test_candidate_unavailable_without_safe_file_access(candidate_project, monkeypatch):
    settings, project = candidate_project
    monkeypatch.delattr(os, "O_NOFOLLOW")
    result = await _run(settings, project)
    assert result.output["candidate_retention"] == {
        "status": "unavailable", "reason": "safe_file_access_unavailable",
    }
    assert not result.success and result.retryable is False
    assert (project / "main.py").read_text() == "value = 1\n"


async def test_candidate_receipts_are_not_authored_or_deliverable_source(candidate_project):
    settings, project = candidate_project
    before = source_tree_snapshot(settings.data_dir)["sha256"]
    result = await _run(settings, project)
    assert result.output["candidate_retention"]["status"] == "unverified"
    assert source_tree_snapshot(settings.data_dir)["sha256"] == before
    copied = merge_back(settings.data_dir, project.parent / "export")
    assert not any("candidate-" in rel or "latest.json" in rel for rel in copied)
    private = project.parent / "export" / ".skyn3t-recovery" / "local.json"
    private.parent.mkdir()
    private.write_text("keep local recovery state")
    merge_back(project, project.parent / "export", clean=True)
    assert private.read_text() == "keep local recovery state"


async def test_unusable_archive_storage_cannot_block_successful_generation(
    candidate_project, monkeypatch
):
    settings, project = candidate_project
    loop = project.parent / "loop-data"
    loop.symlink_to(loop.name, target_is_directory=True)
    settings.data_dir = loop
    resolve = Path.resolve

    def fail_storage_resolution(path, *args, **kwargs):
        if path == loop:
            raise RuntimeError("archive storage path resolution failed")
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_storage_resolution)

    class SuccessfulWriter(_FailingWriter):
        async def agentic_build(self, prompt, workdir, **kwargs):
            result = await super().agentic_build(prompt, workdir, **kwargs)
            result["ok"] = True
            return result

    result = await _run(settings, project, SuccessfulWriter(settings))
    assert result.success
    assert (project / "main.py").read_text() == "value = 2\n"
