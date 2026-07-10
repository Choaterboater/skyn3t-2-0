"""Identify and preserve system-authored TestAuthor acceptance contracts."""

from __future__ import annotations

import shutil
from pathlib import Path

GENERATED_ACCEPTANCE_HEADER = "Acceptance suite authored test-first from the brief."
GENERATED_ACCEPTANCE_PENDING_MARKER = (
    "behavioral assertion pending — criterion documented, not yet verified"
)


def _is_exact_contract_path(relative: Path) -> bool:
    return (
        len(relative.parts) == 2
        and relative.parts[0] == "tests"
        and relative.name.startswith("test_acceptance_")
        and relative.name.endswith(".py")
        and len(relative.name) > len("test_acceptance_.py")
    )


def is_system_acceptance_contract(
    path: str | Path,
    project_dir: str | Path | None = None,
    *,
    content: str | bytes | None = None,
) -> bool:
    """Return true only for an exact, generated TestAuthor contract.

    Absolute paths require ``project_dir`` so a same-named file outside the
    project cannot be mistaken for the system contract. Both stable generator
    markers are required; user-authored tests with a matching filename remain
    ordinary mutable tests and receive no skip-policy exemption.
    """
    candidate = Path(path)
    if project_dir is None:
        if candidate.is_absolute():
            return False
        relative = candidate
        target = candidate
    else:
        root = Path(project_dir).resolve()
        target = candidate if candidate.is_absolute() else root / candidate
        try:
            relative = target.resolve(strict=False).relative_to(root)
        except (OSError, ValueError):
            return False

    if not _is_exact_contract_path(relative) or target.is_symlink():
        return False
    if content is None:
        try:
            content = target.read_bytes()
        except OSError:
            return False
    text = (
        content.decode("utf-8", errors="replace")
        if isinstance(content, bytes)
        else str(content)
    )
    header = f'"""{GENERATED_ACCEPTANCE_HEADER}'
    pending = f'@pytest.mark.skip(reason="{GENERATED_ACCEPTANCE_PENDING_MARKER}")'
    return header in text[:512] and pending in text


def snapshot_acceptance_contracts(project_dir: str | Path) -> dict[str, bytes]:
    """Snapshot preexisting direct TestAuthor contracts by project-relative path."""
    root = Path(project_dir).resolve()
    tests_dir = root / "tests"
    if not tests_dir.is_dir() or tests_dir.is_symlink():
        return {}
    snapshot: dict[str, bytes] = {}
    for target in sorted(tests_dir.glob("test_acceptance_*.py")):
        if not is_system_acceptance_contract(target, root):
            continue
        try:
            snapshot[target.relative_to(root).as_posix()] = target.read_bytes()
        except (OSError, ValueError):
            continue
    return snapshot


def restore_acceptance_contracts(
    project_dir: str | Path,
    snapshot: dict[str, bytes],
) -> list[str]:
    """Restore snapshotted contracts byte-for-byte and report changed paths."""
    root = Path(project_dir).resolve()
    restored: list[str] = []
    for rel, expected in sorted(snapshot.items()):
        relative = Path(rel)
        if not _is_exact_contract_path(relative) or not is_system_acceptance_contract(
            relative, content=expected
        ):
            continue
        tests_dir = root / "tests"
        target = tests_dir / relative.name
        try:
            actual = target.read_bytes() if target.is_file() else None
        except OSError:
            actual = None
        if actual == expected and not target.is_symlink():
            continue
        try:
            if tests_dir.is_symlink() or (
                tests_dir.exists() and not tests_dir.is_dir()
            ):
                tests_dir.unlink()
            tests_dir.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            elif target.exists() and not target.is_file():
                shutil.rmtree(target)
            target.write_bytes(expected)
            restored.append(relative.as_posix())
        except OSError:
            continue
    return restored
