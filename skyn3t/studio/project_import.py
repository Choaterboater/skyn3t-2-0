"""Adopt local source as a separate, unverified managed project. Never executes it."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skyn3t.agents._common import canonical_project_relpath, slugify
from skyn3t.security.project_files import is_private_project_path
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS
from skyn3t.worktree import (
    SOURCE_TREE_EXCLUDED_DIR_NAMES,
    _open_source_descriptor,
    delivery_staging_dir,
    source_tree_snapshot,
)

MAX_IMPORT_FILES = 20_000
MAX_IMPORT_BYTES = 268_435_456
MAX_IMPORT_ENTRIES = 40_000
_MANIFEST_MAX_BYTES = 1_048_576
_SKIP_NAMES = SOURCE_TREE_EXCLUDED_DIR_NAMES | frozenset({
    ".cache", ".gradle", ".hg", ".svn", ".tox", ".output", "venv",
    "target", "skyn3t-observability.json",
})
IMPORT_STACKS = frozenset(REAL_BUILDER_STACKS) | {"flask", "django", "unknown"}
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")


def _read_manifest(root: Path, name: str) -> str:
    path = root / name
    if not path.is_file():
        return ""
    with os.fdopen(_open_source_descriptor(root, path), "rb") as handle:
        data = handle.read(_MANIFEST_MAX_BYTES + 1)
    if len(data) > _MANIFEST_MAX_BYTES:
        raise ValueError(f"{name} exceeds the 1 MiB manifest limit")
    return data.decode("utf-8")


def detect_project_stack(project: str | Path) -> str:
    """Infer the concrete existing stack, never a scaffold from the change request."""
    root = Path(project).resolve()
    if (root / "src-tauri" / "tauri.conf.json").is_file():
        return "tauri"
    package_text = _read_manifest(root, "package.json")
    if package_text:
        package = json.loads(package_text)
        if not isinstance(package, dict):
            raise ValueError("package.json must contain an object")
        dependencies: set[str] = set()
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            values = package.get(key) or {}
            if not isinstance(values, dict):
                raise ValueError(f"package.json {key} must contain an object")
            dependencies.update(values)
        for names, stack in (
            ({"expo", "react-native"}, "react_native"),
            ({"@tauri-apps/api"}, "tauri"),
            ({"next"}, "nextjs"),
            ({"astro"}, "astro"),
            ({"@remix-run/react", "@remix-run/node"}, "remix"),
            ({"@sveltejs/kit"}, "sveltekit"),
            ({"vue"}, "vue"),
            ({"phaser"}, "phaser"),
            ({"react"}, "react"),
            ({"express"}, "express"),
        ):
            if dependencies & names:
                return stack
        # Do not mistake an unfamiliar JS application for a plain HTML site.
        return "unknown"
    python_text = _read_manifest(root, "pyproject.toml")
    requirements = _read_manifest(root, "requirements.txt")
    if python_text or requirements:
        deps: list[str] = requirements.splitlines()
        if python_text:
            config = tomllib.loads(python_text)
            deps.extend(config.get("project", {}).get("dependencies", []))
            deps.extend(config.get("tool", {}).get("poetry", {}).get("dependencies", {}))
        names = {
            re.split(r"[\s\[<>=!~;@]", dep.strip().lower(), maxsplit=1)[0]
            for dep in deps if isinstance(dep, str)
        }
        for name in ("mcp", "fastapi", "flask", "django"):
            if name in names:
                return name
        return "python"
    if (root / "Package.swift").is_file():
        return "swift"
    if any(root.glob("*.xcodeproj")):
        return "swift_ios"
    if (root / "index.html").is_file():
        return "static"
    if any(root.glob("*.py")):
        return "python"
    return "unknown"


def is_imported_project(manifest: BuildManifest | dict[str, Any] | None) -> bool:
    if manifest is None:
        return False
    extra = manifest.extra if isinstance(manifest, BuildManifest) else manifest.get("extra")
    source = extra.get("source") if isinstance(extra, dict) else None
    return isinstance(source, dict) and source.get("kind") == "local_import"


def project_source_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """Keep project-list rows small; the full import receipt stays on disk."""
    if not is_imported_project(manifest):
        return {}
    source = manifest["extra"]["source"]
    return {key: source.get(key) for key in ("kind", "original_path", "imported_at")}


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
        info.st_ctime_ns, stat.S_IMODE(info.st_mode),
    )


def _inventory(root: Path) -> tuple[dict[str, tuple[int, ...]], list[dict[str, str]]]:
    files: dict[str, tuple[int, ...]] = {}
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    pending = [root]
    entries = total_bytes = 0
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or not directory.resolve().is_relative_to(root):
            raise ValueError("Source directory changed during import")
        with os.scandir(directory) as scan:
            for entry in scan:
                entries += 1
                if entries > MAX_IMPORT_ENTRIES:
                    raise ValueError("Import exceeds the 40,000 entry limit; select a smaller project")
                path = Path(entry.path)
                relative = path.relative_to(root)
                rel = relative.as_posix()
                reason = ""
                if entry.is_symlink() or getattr(os.path, "isjunction", lambda p: False)(path):
                    reason = "symbolic link or junction"
                elif is_private_project_path(relative):
                    reason = "private configuration or local metadata"
                elif entry.name.casefold() in _SKIP_NAMES or entry.name.endswith(".egg-info"):
                    reason = "dependency cache or generated output"
                if reason:
                    skipped.append({"path": rel, "reason": reason})
                    continue
                if canonical_project_relpath(rel) != rel:
                    raise ValueError(f"Unsupported project-relative filename: {rel!r}")
                if rel.casefold() in seen:
                    raise ValueError(f"Case-insensitive filename collision: {rel!r}")
                seen.add(rel.casefold())
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    continue
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"Non-regular source file: {rel!r}")
                total_bytes += info.st_size
                if len(files) >= MAX_IMPORT_FILES or total_bytes > MAX_IMPORT_BYTES:
                    raise ValueError("Import exceeds 20,000 files or 256 MiB; select a smaller project")
                files[rel] = _identity(info)
    return dict(sorted(files.items())), sorted(skipped, key=lambda item: item["path"])


def import_project(
    source: str | Path,
    projects_dir: str | Path,
    *,
    slug: str = "",
    stack: str = "",
) -> dict[str, Any]:
    """Copy a bounded local tree and publish only a complete import, without replacing anything."""
    source_path = Path(source).expanduser()
    if not source_path.is_absolute():
        raise ValueError("Provide an absolute local project directory")
    source_path = source_path.resolve(strict=True)
    if not source_path.is_dir():
        raise ValueError("Source must be a project directory")
    root = Path(projects_dir).expanduser().resolve()
    if source_path == Path(source_path.anchor) or source_path == Path.home().resolve():
        raise ValueError("Select a project directory, not a filesystem root or home directory")
    if is_private_project_path(Path(source_path.name)):
        raise ValueError("Select a source project, not a private configuration or metadata directory")
    if source_path.is_relative_to(root) or root.is_relative_to(source_path):
        raise ValueError("Source and managed Projects directory must not overlap")
    name = slug or slugify(source_path.name, "imported-project")[:80]
    if not _SLUG.fullmatch(name):
        raise ValueError("Name must be 1-80 lowercase letters, digits or hyphens, starting with a letter or digit")
    if stack and stack not in IMPORT_STACKS:
        raise ValueError(f"Unsupported stack {stack!r}; choose from {', '.join(sorted(IMPORT_STACKS))}")
    target = root / name
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Project {name!r} already exists; choose a different name")
    files, skipped = _inventory(source_path)
    if not files:
        raise ValueError("No project files remain after excluding private files and generated output")
    root.mkdir(parents=True, exist_ok=True)
    stage = delivery_staging_dir(f".import-{name}-", root)
    reserved = published = False
    try:
        candidate = stage / "candidate"
        candidate.mkdir()
        for rel, expected in files.items():
            path = source_path / rel
            destination = candidate / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(_open_source_descriptor(source_path, path), "rb") as reader:
                before = os.fstat(reader.fileno())
                if not stat.S_ISREG(before.st_mode) or _identity(before) != expected:
                    raise ValueError("Source changed during import; retry when edits have finished")
                with destination.open("xb") as writer:
                    remaining = before.st_size
                    while remaining:
                        block = reader.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError("Source changed during import")
                        writer.write(block)
                        remaining -= len(block)
                    if reader.read(1) or _identity(os.fstat(reader.fileno())) != expected:
                        raise ValueError("Source changed during import")
                # Preserve normal mode bits (including executable scripts), never setuid/setgid.
                destination.chmod(stat.S_IMODE(before.st_mode) & 0o777)
        if _inventory(source_path) != (files, skipped):
            raise ValueError("Source changed during import; retry when edits have finished")
        warnings = [
            "Imported copy only: the original project was not changed. No code was executed.",
            "Filename exclusions are not a secret-content scan. Review the copy before using an AI provider.",
            "Import is unverified. Use Improve with a specific goal; preview and deploy remain blocked until delivery.",
        ]
        selected_stack = stack
        if not selected_stack:
            try:
                selected_stack = detect_project_stack(candidate)
            except (ValueError, UnicodeError, TypeError, AttributeError) as exc:
                selected_stack = "unknown"
                warnings.append(f"Stack detection could not read the project manifests: {exc}")
        if selected_stack == "unknown":
            warnings.append(
                "No supported root stack detected. Automatic proof may be limited or fail; "
                "for a monorepo, import the application subdirectory or specify --stack."
            )
        snapshot = source_tree_snapshot(
            candidate, max_files=MAX_IMPORT_FILES,
            max_bytes=MAX_IMPORT_BYTES, max_entries=MAX_IMPORT_ENTRIES,
        )
        if not snapshot["valid"]:
            raise ValueError("Imported source snapshot is invalid")
        provenance = {
            "kind": "local_import",
            "original_path": str(source_path),
            "imported_at": datetime.now(UTC).isoformat(),
            "snapshot": snapshot,
            "skipped": skipped,
            "warnings": warnings,
        }
        manifest = BuildManifest(
            slug=name, brief=f"Existing project: {source_path.name}",
            stack=selected_stack, status="imported", artifact_dir=str(target),
            files=list(files), extra={"source": provenance},
        )
        manifest.save(candidate)
        # Reserve exclusively: even concurrent imports/builds cannot be overwritten.
        target.mkdir()
        reserved = True
        os.replace(candidate, target)
        published = True
        return {
            "slug": name, "project_dir": str(target), "stack": selected_stack,
            "status": "imported", "files_count": len(files), "source": provenance,
            "skipped": skipped, "warnings": warnings,
        }
    finally:
        if reserved and not published:
            target.rmdir()
        shutil.rmtree(stage)
