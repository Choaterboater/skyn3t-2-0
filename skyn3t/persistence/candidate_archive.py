"""Bounded, unverified source fragments, separate from boot checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

from skyn3t.agents._common import canonical_project_relpath, confined_path
from skyn3t.config.settings import Settings
from skyn3t.persistence.checkpoint import CheckpointManager
from skyn3t.security.project_files import is_private_project_path
from skyn3t.security.secrets import SecretsStore, scrub_text
from skyn3t.worktree import _open_source_descriptor, source_tree_snapshot

_log = structlog.get_logger(__name__)
_MAX_FILE_BYTES = 1_000_000
_MAX_STATE_BYTES = 2 * 1024 * 1024 - 4096
_OWN_RECEIPT = re.compile(r"candidate-[0-9a-f]{32}\.json")


class CandidateArchive:
    """Bind retained text to a pre-generation source digest."""

    def __init__(self, root: Path, settings: Settings) -> None:
        self.root = root.resolve()
        self.settings = settings
        self.directory = settings.data_dir.absolute() / ".skyn3t-recovery" / "improve_candidates"
        self.storage_in_worktree = settings.data_dir.absolute().is_relative_to(self.root)
        self.base_sha256 = ""
        try:
            snapshot = source_tree_snapshot(
                self.root, max_files=20_000, max_bytes=256 * 1024 * 1024,
                max_entries=50_000,
            )
            if snapshot.get("valid"):
                self.base_sha256 = str(snapshot["sha256"])
            else:
                _log.warning("candidate_archive.invalid_base")
        except OSError as exc:
            _log.warning("candidate_archive.base_unavailable", error=type(exc).__name__)

    def save(
        self, before: Mapping[str, str], after: Mapping[str, str | None],
    ) -> dict[str, Any]:
        """Retain changed text; a None value selects safely read on-disk content."""
        if not self.base_sha256:
            return {"status": "unavailable", "reason": "invalid_base_snapshot"}
        if (
            not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
            or os.open not in os.supports_dir_fd
        ):
            _log.warning("candidate_archive.safe_file_access_unavailable")
            return {"status": "unavailable", "reason": "safe_file_access_unavailable"}
        secrets = SecretsStore(self.settings)
        files: dict[str, Any] = {}
        omitted: dict[str, int] = {}
        state: dict[str, Any] = {
            "status": "unverified", "delivered": False, "proof_passed": False,
            "scope": "changed_text_files_only", "complete_backup": False,
            "base_source_sha256": self.base_sha256,
            "files": files, "omitted": omitted,
        }
        for rel, expected in sorted(after.items()):
            if rel in before and before[rel] == expected:
                continue
            reason = ""
            relative = Path(rel)
            if canonical_project_relpath(rel) != rel:
                reason = "invalid_path"
            elif is_private_project_path(relative) or scrub_text(rel, secrets) != rel:
                reason = "private_path"
            elif len(files) >= 20:
                reason = "file_limit"
            else:
                target = confined_path(self.root, rel)
                ancestors = [self.root.joinpath(*relative.parts[:i])
                             for i in range(1, len(relative.parts) + 1)]
                if target is None or any(path.is_symlink() for path in ancestors):
                    reason = "unsafe_path"
                else:
                    try:
                        fd = _open_source_descriptor(self.root, self.root / relative)
                        with os.fdopen(fd, "rb") as handle:
                            original_stat = os.fstat(handle.fileno())
                            if not stat.S_ISREG(original_stat.st_mode):
                                reason = "nonregular_file"
                            elif original_stat.st_nlink > 1:
                                reason = "file_alias"
                            elif original_stat.st_size > _MAX_FILE_BYTES:
                                reason = "file_size"
                            else:
                                raw = handle.read(_MAX_FILE_BYTES + 1)
                                current_stat = os.fstat(handle.fileno())
                                path_stat = (self.root / relative).stat(follow_symlinks=False)
                                identities = [
                                    (info.st_dev, info.st_ino, info.st_size,
                                     info.st_mtime_ns, info.st_mode)
                                    for info in (original_stat, current_stat, path_stat)
                                ]
                                if (
                                    len(raw) > _MAX_FILE_BYTES
                                    or any(identity != identities[0] for identity in identities[1:])
                                ):
                                    reason = "unstable_file"
                                else:
                                    text = raw.decode("utf-8")
                                    if "\x00" in text:
                                        reason = "binary_file"
                                    elif (
                                        expected is not None
                                        and text.replace("\r\n", "\n").replace("\r", "\n") != expected
                                    ):
                                        reason = "unstable_file"
                                    elif scrub_text(text, secrets) != text:
                                        reason = "secret_content"
                                    else:
                                        files[rel] = {
                                            "content": text,
                                            "sha256": hashlib.sha256(raw).hexdigest(),
                                        }
                                        if len(json.dumps(state).encode("utf-8")) > _MAX_STATE_BYTES:
                                            del files[rel]
                                            reason = "archive_size"
                    except (OSError, UnicodeError) as exc:
                        reason = "unreadable_file"
                        _log.warning(
                            "candidate_archive.file_omitted", path=rel,
                            error=type(exc).__name__,
                        )
            if reason:
                omitted[reason] = omitted.get(reason, 0) + 1
        if not files:
            return {"status": "skipped", "reason": "no_safe_text_changes", "omitted": omitted}

        directory = self.directory
        if (
            self.storage_in_worktree or directory.is_symlink() or directory.parent.is_symlink()
            or directory.resolve().is_relative_to(self.root)
        ):
            raise OSError("candidate archive must be outside the worktree without a directory alias")
        manager = CheckpointManager(settings=self.settings, _dir=directory)
        path = manager.save(f"candidate-{uuid.uuid4().hex}", state=state)
        receipt: dict[str, Any] = {
            "status": "unverified", "path": str(path), "files_saved": len(files),
            "omitted": omitted,
            "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "base_source_sha256": self.base_sha256,
        }
        try:
            own_files = [
                item for item in directory.iterdir()
                if item != path and _OWN_RECEIPT.fullmatch(item.name)
                and not item.is_symlink() and item.is_file()
            ]
            own_files.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
            for old in own_files[9:]:
                old.unlink()
        except OSError as exc:
            receipt["cleanup_error"] = type(exc).__name__
            _log.warning("candidate_archive.cleanup_failed", error=type(exc).__name__)
        _log.warning("candidate_archive.retained_unverified", **receipt)
        return receipt


class CandidateRecovery:
    """Inspect only server-associated receipts; recover to a new imported project.

    ``receipts`` MUST come from trusted persisted build records for ``project``,
    never from request bodies or a scan of the shared archive directory.
    """

    def __init__(self, settings: Settings, project: Path, receipts: list[dict[str, Any]] | None = None) -> None:
        self.settings = settings
        self.project = project.resolve(strict=True)
        self.directory = settings.data_dir.absolute() / ".skyn3t-recovery" / "improve_candidates"
        self.receipts: dict[str, dict[str, Any]] = {}
        if receipts is None:
            receipts = self._registered_receipts()
        for receipt in receipts[:100]:
            if not isinstance(receipt, dict) or receipt.get("status") != "unverified":
                continue
            path = Path(str(receipt.get("path", "")))
            if _OWN_RECEIPT.fullmatch(path.name) and path == self.directory / path.name:
                self.receipts.setdefault(path.stem, receipt)

    @property
    def _registry(self) -> Path:
        project_key = hashlib.sha256(str(self.project).encode("utf-8")).hexdigest()
        return self.settings.data_dir.absolute() / ".skyn3t-recovery" / "associations" / project_key

    def _registered_receipts(self) -> list[dict[str, Any]]:
        registry = self._registry
        if registry.resolve() != registry:
            raise ValueError("Saved-work association storage is aliased")
        if not registry.exists():
            return []
        receipts = []
        with os.scandir(registry) as entries:
            for index, entry in enumerate(entries):
                if index >= 100:
                    break
                if not _OWN_RECEIPT.fullmatch(entry.name):
                    continue
                try:
                    with os.fdopen(_open_source_descriptor(registry, Path(entry.path)), "rb") as reader:
                        info = os.fstat(reader.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            continue
                        raw = reader.read(8193)
                    if len(raw) > 8192:
                        continue
                    value = json.loads(raw)
                    if isinstance(value, dict) and value.get("project") == str(self.project) and isinstance(value.get("receipt"), dict):
                        receipts.append((info.st_mtime_ns, value["receipt"]))
                except (OSError, ValueError, UnicodeError):
                    continue
        receipts.sort(key=lambda row: row[0], reverse=True)
        return [value for _, value in receipts]

    @classmethod
    def register(cls, settings: Settings, project: Path, receipt: dict[str, Any]) -> None:
        """Called ONLY by the trusted outcome producer carrying its canonical project."""
        recovery = cls(settings, project, [receipt])
        if not recovery.receipts:
            raise ValueError("No valid retained candidate receipt to associate")
        archive_id = next(iter(recovery.receipts))
        recovery._load(archive_id)
        registry = recovery._registry
        if registry.resolve() != registry or registry.is_relative_to(recovery.project):
            raise ValueError("Saved-work association storage must be unaliased and outside the project")
        registry.mkdir(parents=True, exist_ok=True)
        value = json.dumps({"project": str(recovery.project), "receipt": receipt}).encode("utf-8")
        if len(value) > 8192:
            raise ValueError("Saved-work receipt exceeds the association limit")
        target = registry / f"{archive_id}.json"
        try:
            with target.open("xb") as writer:
                writer.write(value)
        except FileExistsError:
            # Existing association is never replaced, including by a duplicate failure callback.
            return
        entries = sorted(registry.glob("candidate-*.json"), key=lambda path: path.stat(follow_symlinks=False).st_mtime_ns, reverse=True)
        for old in entries[10:]:
            if _OWN_RECEIPT.fullmatch(old.name) and not old.is_symlink():
                old.unlink()

    def _snapshot(self) -> dict[str, Any]:
        return source_tree_snapshot(
            self.project, max_files=20_000, max_bytes=256 * 1024 * 1024,
            max_entries=40_000,
        )

    def _load(self, archive_id: str) -> tuple[dict[str, Any], dict[str, Any], bool]:
        if not isinstance(archive_id, str) or not _OWN_RECEIPT.fullmatch(f"{archive_id}.json"):
            raise ValueError("Invalid saved-work identifier")
        receipt = self.receipts.get(archive_id)
        if receipt is None:
            raise FileNotFoundError("Saved work is not associated with this project")
        if self.directory.resolve() != self.directory or self.directory.is_relative_to(self.project):
            raise ValueError("Saved-work storage is aliased or overlaps the project")
        path = self.directory / f"{archive_id}.json"
        with os.fdopen(_open_source_descriptor(self.directory, path), "rb") as reader:
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("Saved work is not a private regular archive")
            raw = reader.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Saved work exceeds the 2 MiB archive limit")
        archive_digest = hashlib.sha256(raw).hexdigest()
        authenticated = bool(receipt.get("archive_sha256"))
        if authenticated and receipt["archive_sha256"] != archive_digest:
            raise ValueError("Saved-work archive hash mismatch; preserve the archive for investigation")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Saved work is not a readable JSON archive") from exc
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ValueError("Unsupported saved-work archive version")
        state = document.get("state")
        if not isinstance(state, dict) or any((
            state.get("status") != "unverified", state.get("delivered") is not False,
            state.get("proof_passed") is not False, state.get("complete_backup") is not False,
            state.get("scope") != "changed_text_files_only",
        )):
            raise ValueError("Saved-work provenance is invalid")
        base = state.get("base_source_sha256")
        if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{64}", base):
            raise ValueError("Saved work has no valid base digest")
        if authenticated and receipt.get("base_source_sha256") != base:
            raise ValueError("Saved-work base digest differs from its build receipt")
        files, omitted = state.get("files"), state.get("omitted")
        if not isinstance(files, dict) or not 1 <= len(files) <= 20:
            raise ValueError("Saved work must contain 1-20 changed text files")
        if not isinstance(omitted, dict) or len(omitted) > 20 or any(
            not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,40}", key)
            or type(count) is not int or not 0 <= count <= 50_000
            for key, count in omitted.items()
        ):
            raise ValueError("Saved-work omissions are invalid")
        if receipt.get("files_saved") != len(files) or receipt.get("omitted") != omitted:
            raise ValueError("Saved work differs from its build receipt")
        secrets = SecretsStore(self.settings)
        seen: dict[str, str] = {}
        from skyn3t.studio.project_import import _SKIP_NAMES

        for rel, value in files.items():
            if (
                canonical_project_relpath(rel) != rel or is_private_project_path(Path(rel))
                or any(part.casefold() in _SKIP_NAMES for part in Path(rel).parts)
                or scrub_text(rel, secrets) != rel
            ):
                raise ValueError("Saved work contains an unsafe or private filename")
            for prefix in [Path(*Path(rel).parts[:n]).as_posix() for n in range(1, len(Path(rel).parts) + 1)]:
                old = seen.setdefault(prefix.casefold(), prefix)
                if old != prefix:
                    raise ValueError("Saved work contains case-insensitive path collisions")
            if not isinstance(value, dict) or not isinstance(value.get("content"), str):
                raise ValueError("Saved work contains invalid text")
            content = value["content"]
            try:
                encoded = content.encode("utf-8")
            except UnicodeError as exc:
                raise ValueError("Saved work contains invalid Unicode") from exc
            if len(encoded) > _MAX_FILE_BYTES or "\x00" in content or scrub_text(content, secrets) != content:
                raise ValueError("Saved work contains oversized, binary or secret content")
            if hashlib.sha256(encoded).hexdigest() != value.get("sha256"):
                raise ValueError(f"Saved-work file hash mismatch: {rel}")
        return document, state, authenticated

    def inspect(self, archive_id: str) -> dict[str, Any]:
        document, state, authenticated = self._load(archive_id)
        snapshot = self._snapshot()
        matches = snapshot.get("valid") and snapshot.get("sha256") == state["base_source_sha256"]
        reason = "" if matches else (
            "Original source no longer matches the saved base. Keep the authoritative project unchanged; "
            "inspect the retained text and reconcile it manually in a separate copy."
        )
        if not authenticated:
            reason = "Legacy receipt lacks an archive integrity digest: inspection only; automatic recovery is refused."
        return {
            "id": archive_id, "created": document.get("created"),
            "status": "unverified", "proof_passed": False, "complete_backup": False,
            "scope": "changed_text_files_only", "files": state["files"],
            "omitted": state["omitted"], "base_source_sha256": state["base_source_sha256"],
            "live_source_sha256": snapshot.get("sha256", ""), "live_source_valid": snapshot.get("valid", False),
            "recoverable": bool(matches and authenticated), "reason": reason,
            "warnings": [
                "Only changed text is retained: deleted files, binary files and omitted edits cannot be restored.",
                "Recovery copies safe current source into a separate unverified workspace. Nothing is executed.",
                "No task resumes and no model budget resets. Review and prove the new workspace separately.",
            ],
        }

    def list(self) -> dict[str, Any]:
        candidates = []
        for archive_id in list(self.receipts)[:10]:
            try:
                detail = self.inspect(archive_id)
                detail["files"] = [
                    {"path": rel, "sha256": value["sha256"], "bytes": len(value["content"].encode("utf-8"))}
                    for rel, value in detail["files"].items()
                ]
                candidates.append(detail)
            except (OSError, ValueError) as exc:
                candidates.append({"id": archive_id, "status": "unavailable", "recoverable": False, "reason": str(exc)})
        return {"candidates": candidates}

    def recover(self, archive_id: str, *, slug: str) -> dict[str, Any]:
        from datetime import UTC, datetime

        from skyn3t.studio.manifest import BuildManifest
        from skyn3t.studio.project_import import _SLUG, _identity, _inventory, detect_project_stack

        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise ValueError("Choose a new name: 1-80 lowercase letters, digits or hyphens")
        detail = self.inspect(archive_id)
        if not detail["recoverable"]:
            raise ValueError(f"{detail['reason']} Expected base {detail['base_source_sha256']}; live {detail['live_source_sha256'] or 'invalid'}")
        root = self.settings.projects_dir.absolute()
        if root.resolve() != root or self.project.parent != root:
            raise ValueError("Recovery requires an authoritative managed project and an unaliased Projects directory")
        target = root / slug
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Project {slug!r} already exists; choose another name. Nothing was overwritten.")
        inventory, skipped = _inventory(self.project)
        path_spellings: dict[str, str] = {}
        all_files = set(inventory) | set(detail["files"])
        for rel in all_files:
            parts = Path(rel).parts
            for length in range(1, len(parts) + 1):
                prefix = Path(*parts[:length]).as_posix()
                previous = path_spellings.setdefault(prefix.casefold(), prefix)
                if previous != prefix or (length < len(parts) and prefix in all_files):
                    raise ValueError("Saved paths conflict with the base copy; reconcile them manually")
        stage = Path(tempfile.mkdtemp(prefix=".recovery-", dir=root))
        reserved = published = False
        try:
            candidate = stage / "candidate"
            candidate.mkdir()
            for rel, expected in inventory.items():
                destination = candidate / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                with os.fdopen(_open_source_descriptor(self.project, self.project / rel), "rb") as reader:
                    before = os.fstat(reader.fileno())
                    if _identity(before) != expected or before.st_nlink != 1:
                        raise ValueError("Original source changed or has a hard-linked file; recovery refused")
                    with destination.open("xb") as writer:
                        remaining = before.st_size
                        while remaining:
                            block = reader.read(min(remaining, 1024 * 1024))
                            if not block:
                                raise ValueError("Original source changed during recovery")
                            writer.write(block)
                            remaining -= len(block)
                        if reader.read(1) or _identity(os.fstat(reader.fileno())) != expected:
                            raise ValueError("Original source changed during recovery")
                destination.chmod(stat.S_IMODE(before.st_mode) & 0o777)
            if _inventory(self.project) != (inventory, skipped) or self._snapshot().get("sha256") != detail["base_source_sha256"]:
                raise ValueError("Original source changed during recovery; nothing was published")
            for rel, value in detail["files"].items():
                destination = candidate / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(value["content"], encoding="utf-8", newline="")
            snapshot = source_tree_snapshot(candidate, max_files=20_000, max_bytes=256 * 1024 * 1024, max_entries=40_000)
            if not snapshot["valid"]:
                raise ValueError("Recovered source is ambiguous or exceeds the managed import limits")
            # Reuse import inventory policy to catch newly introduced case collisions and omitted paths.
            recovered_files, recovered_skipped = _inventory(candidate)
            if recovered_skipped:
                raise ValueError("Saved edits introduced paths excluded by managed import policy")
            try:
                stack = detect_project_stack(candidate)
            except (ValueError, UnicodeError, TypeError, AttributeError):
                stack = "unknown"
            warnings = detail["warnings"] + ["Private files and generated/dependency caches are not copied. Base files are not a secret-content scan."]
            provenance = {
                "kind": "local_import", "original_path": str(self.project),
                "imported_at": datetime.now(UTC).isoformat(), "snapshot": snapshot,
                "skipped": skipped, "warnings": warnings,
                "recovery": {"archive_id": archive_id, "base_source_sha256": detail["base_source_sha256"], "omitted": detail["omitted"], "status": "unverified"},
            }
            manifest = BuildManifest(
                slug=slug, brief=f"Unverified recovered work from {self.project.name}",
                stack=stack, status="imported", artifact_dir=str(target),
                files=list(recovered_files), extra={"source": provenance},
            )
            manifest.save(candidate)
            if _inventory(self.project) != (inventory, skipped) or self._snapshot().get("sha256") != detail["base_source_sha256"]:
                raise ValueError("Original source changed before publication; nothing was published")
            target.mkdir()  # Exclusive reservation: duplicate requests never replace another project.
            reserved = True
            os.rename(candidate, target)
            published = True
            return {
                "slug": slug, "project_dir": str(target), "status": "imported", "unverified": True,
                "proof_passed": False, "archive_retained": True, "original_preserved": True,
                "files_count": len(recovered_files), "omitted": detail["omitted"],
                "skipped": skipped, "warnings": warnings,
            }
        finally:
            if reserved and not published:
                target.rmdir()
            shutil.rmtree(stage)
