"""Package-manager selection and imported-project install policy.

No executable discovery or installation happens here. Generated proof keeps its
npm default; imported proof and preview share declaration/lockfile precedence.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

NPM_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json")
_LOCKFILES = (
    ("npm", NPM_LOCKFILES),
    ("pnpm", ("pnpm-lock.yaml",)),
    ("yarn", ("yarn.lock",)),
)
_DECLARATION = re.compile(
    r"(?:npm|pnpm|yarn)(?:@\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)?"
)


def node_package_manager(pdir: Path, *, strict: bool = False) -> str:
    """Declaration wins; otherwise require one lockfile family, default npm.

    Strict validation is for preview. The default retains imported proof's
    historical declaration parsing, including its accepted bare manager names.
    """
    package = json.loads((pdir / "package.json").read_text(encoding="utf-8"))
    if strict and not isinstance(package, dict):
        raise ValueError("package.json must be an object")
    declared = package.get("packageManager", "") if isinstance(package, dict) else ""
    if not isinstance(declared, str):
        raise ValueError("packageManager must be a string")
    if declared:
        if strict and not _DECLARATION.fullmatch(declared):
            raise ValueError("Malformed or unsupported packageManager declaration")
        manager = declared.split("@", 1)[0]
    else:
        if strict and isinstance(package, dict) and "packageManager" in package:
            raise ValueError("packageManager must not be empty")
        locked = [
            name for name, files in _LOCKFILES
            if any((pdir / file).is_file() for file in files)
        ]
        if len(locked) > 1:
            raise ValueError("Conflicting lockfiles: declare packageManager before improving this project")
        manager = locked[0] if locked else "npm"
    if manager not in {"npm", "pnpm", "yarn"}:
        raise ValueError(f"Automatic proof does not support package manager {manager!r}")
    return manager


def existing_node_install_args(pdir: Path, manager: str, command: str) -> list[str]:
    """Imported proof policy; non-npm preview uses these same immutable installs.

    Modern Yarn additionally requires YARN_ENABLE_SCRIPTS=false in the install
    environment (it does not accept the classic --ignore-scripts option).
    """
    if manager == "npm":
        locked = any((pdir / name).is_file() for name in NPM_LOCKFILES)
        return [
            command, "ci" if locked else "install",
            "--ignore-scripts", "--no-audit", "--no-fund",
            *([] if locked else ["--package-lock=false"]),
        ]
    if manager == "pnpm":
        return [command, "install", "--frozen-lockfile", "--ignore-scripts"]
    package = json.loads((pdir / "package.json").read_text(encoding="utf-8"))
    declared = str(package.get("packageManager", ""))
    modern = bool(re.match(r"yarn@(?:[2-9]|\d{2,})\.", declared)) or (pdir / ".yarnrc.yml").is_file()
    return (
        [command, "install", "--immutable"] if modern
        else [command, "install", "--frozen-lockfile", "--ignore-scripts"]
    )
