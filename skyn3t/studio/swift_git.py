"""Proof-private compatibility for SwiftPM versions without explicit bare Git access."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SwiftGitSession:
    env: dict[str, str]
    scratch_dir: Path

    def command(self, swift: str, operation: str) -> list[str]:
        return [
            swift, operation, "--scratch-path", str(self.scratch_dir),
            "--disable-dependency-cache",
        ]


@contextmanager
def swift_git_session(env: Mapping[str, str]) -> Iterator[SwiftGitSession]:
    from skyn3t.exec_paths import find_executable

    if os.name != "posix":
        raise OSError("SwiftPM bare-cache compatibility requires a fixed SwiftPM on this platform")
    git = find_executable("git")
    if git is None:
        raise FileNotFoundError("Git is unavailable for native Swift verification")
    with tempfile.TemporaryDirectory(prefix="skyn3t-swift-proof-") as directory:
        root = Path(directory).resolve()
        tools = root / "tools"
        tools.mkdir()
        scratch = root / "scratch"
        repositories = scratch / "repositories"
        repositories.mkdir(parents=True)
        registry = root / "registry"
        registry.mkdir()
        config = root / "git.json"
        config.write_text(json.dumps({
            "git": str(Path(git).resolve()),
            "repositories": str(repositories),
            "registry": str(registry),
        }), encoding="utf-8")
        config.chmod(0o600)
        launcher = tools / "git"
        launcher.write_text(
            "#!/bin/sh\nexec "
            + shlex.join([
                str(Path(sys.executable).resolve()), "-I",
                str(Path(__file__).resolve()), str(config),
            ])
            + ' "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        child_env = dict(env)
        child_env["PATH"] = str(tools) + os.pathsep + child_env.get("PATH", "")
        yield SwiftGitSession(env=child_env, scratch_dir=scratch)


def _cache_path(value: str, repositories: Path) -> Path | None:
    path = Path(value)
    if not path.is_absolute() or path.parent != repositories or ".." in path.parts:
        return None
    try:
        if path.resolve() != path or path.is_symlink():
            return None
    except OSError:
        return None
    return path


def _identity(path: Path) -> dict[str, str | int] | None:
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (path / ".git").exists():
            return None
        for name in ("HEAD", "config", "objects", "refs", ".git"):
            if (path / name).is_symlink():
                return None
        if not (path / "HEAD").is_file() or not (path / "config").is_file():
            return None
        if not (path / "objects").is_dir():
            return None
        return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
    except OSError:
        return None


def _receipt_path(registry: Path, path: Path) -> Path:
    return registry / (hashlib.sha256(str(path).encode()).hexdigest() + ".json")


def _authorized(registry: Path, path: Path) -> bool:
    identity = _identity(path)
    if identity is None:
        return False
    receipt = _receipt_path(registry, path)
    try:
        if receipt.is_symlink() or receipt.stat().st_size > 2048:
            return False
        return json.loads(receipt.read_text(encoding="utf-8")) == identity
    except (OSError, ValueError):
        return False


def _authorize_clone(registry: Path, path: Path) -> None:
    identity = _identity(path)
    if identity is None:
        raise ValueError("Git clone did not produce a safe bare-cache directory")
    descriptor, temporary = tempfile.mkstemp(prefix="clone-", dir=registry)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(identity, handle)
        temporary_path.replace(_receipt_path(registry, path))
    finally:
        temporary_path.unlink(missing_ok=True)


def _explicit_cache_args(
    arguments: list[str], repositories: Path, registry: Path,
) -> list[str]:
    if len(arguments) < 3 or arguments[0] != "-C":
        return arguments
    if "GIT_DIR" in os.environ or "GIT_WORK_TREE" in os.environ:
        return arguments
    index = 2
    while index < len(arguments):
        option = arguments[index]
        if option == "-c" and index + 1 < len(arguments):
            index += 2
        elif option in {"--no-pager", "--literal-pathspecs"}:
            index += 1
        elif option.startswith("-"):
            return arguments
        else:
            break
    if index == len(arguments) or arguments[index] in {"clone", "init", "worktree"}:
        return arguments
    path = _cache_path(arguments[1], repositories)
    if path is None or not _authorized(registry, path):
        return arguments
    return [f"--git-dir={path}", *arguments]


def _main() -> int:
    try:
        config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not all(
            isinstance(config.get(name), str) and Path(config[name]).is_absolute()
            for name in ("git", "repositories", "registry")
        ):
            raise ValueError("Invalid Swift Git adapter configuration")
        git = config["git"]
        repositories, registry = Path(config["repositories"]), Path(config["registry"])
        arguments = sys.argv[2:]
        destination = None
        if arguments and arguments[0] == "clone" and arguments.count("--mirror") == 1:
            candidate = arguments[-2] if arguments[-1] == "--progress" else arguments[-1]
            destination = _cache_path(candidate, repositories)
        if destination is not None:
            result = subprocess.run([git, *arguments], check=False)
            if result.returncode == 0:
                _authorize_clone(registry, destination)
            return result.returncode
        os.execv(git, [git, *_explicit_cache_args(arguments, repositories, registry)])
    except (OSError, ValueError, IndexError) as exc:
        print(f"Swift Git compatibility failed: {exc}", file=sys.stderr)
        return 128
    return 128


if __name__ == "__main__":
    raise SystemExit(_main())
