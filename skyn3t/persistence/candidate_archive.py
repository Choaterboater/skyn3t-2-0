"""Bounded, unverified source fragments, separate from boot checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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
    """Bind retained text to a pre-generation source digest; never apply it."""

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
