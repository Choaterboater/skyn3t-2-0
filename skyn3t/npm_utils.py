"""Shared npm install helpers for generated projects.

Generated builds are short-lived, but package tarballs are not. Keep npm's cache
outside the artifact tree and use the same quiet/offline-friendly flags everywhere
we install dependencies during proof, preview, or verifier stages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

import structlog

from skyn3t.security.secrets import filter_env
from skyn3t.worktree import _source_tree_internal_output

_log = structlog.get_logger(__name__)
_STAMP_VERSION = 1
_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock")
# Install receipts keep their existing version and policy. Build receipts are
# deliberately incompatible with the old source-only stamp.
_BUILD_STAMP_VERSION = 2
_BUILD_SKIP_DIRS = {".git", "node_modules"}
_BUILD_OUTPUTS = {"vite build": "dist", "astro build": "dist", "next build": ".next"}
_BUILD_CHECKS = {"tsc", "tsc -b", "tsc --noEmit", "astro check", "vue-tsc", "svelte-check"}


def npm_cache_dir() -> str:
    configured = os.environ.get("SKYN3T_NPM_CACHE_DIR", "").strip()
    path = Path(configured).expanduser() if configured else Path.home() / ".cache" / "skyn3t" / "npm"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    return str(path)


def npm_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        **filter_env(os.environ),
        "CI": "1",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_progress": "false",
        "npm_config_prefer_offline": "true",
        "npm_config_ignore_scripts": "true",
    }
    cache = npm_cache_dir()
    if cache:
        env["npm_config_cache"] = cache
    if extra:
        env.update(extra)
    return env


def npm_install_args(npm_cmd: str, action: str = "install") -> list[str]:
    args = [
        npm_cmd,
        action,
        "--no-audit",
        "--no-fund",
        "--no-progress",
        "--prefer-offline",
        "--ignore-scripts",
    ]
    cache = npm_cache_dir()
    if cache:
        args += ["--cache", cache]
    return args


def npm_install_fingerprint(project_dir: str | Path) -> str:
    """Hash package manifests that determine installed npm dependencies."""
    pdir = Path(project_dir)
    h = hashlib.sha256()
    for name in ("package.json", *_LOCKFILES):
        path = pdir / name
        if not path.is_file():
            continue
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def _build_file_digest(path: Path) -> str:
    """Hash regular local files only; errors must never become success tokens."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("unsafe build file")
    h = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("build file changed while opening")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
    ):
        raise ValueError("build file changed while hashing")
    return h.hexdigest()


def _build_tree_manifest(root: Path, *, skip: set[str] | None = None) -> dict[str, str]:
    """Sorted traversal, including binary assets and dotfiles, without links."""
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError("unsafe build directory")
    manifest: dict[str, str] = {}

    def onerror(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, onerror=onerror, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(dirs)
        for name in dirs[:]:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if skip is not None and (
                rel in skip or _source_tree_internal_output(Path(rel))
            ):
                dirs.remove(name)
                continue
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError("unsafe build directory")
            if skip is None:
                manifest[rel + "/"] = "directory"
        for name in sorted(files):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if skip is not None and (
                rel in skip or _source_tree_internal_output(Path(rel))
            ):
                continue
            manifest[rel] = _build_file_digest(path)
    return manifest


def _production_output(pdir: Path, build_cmd: str) -> str:
    """Recognize only default website layouts, not arbitrary shell scripts."""
    if build_cmd != "build":
        return ""
    # Validate before parsing; never follow a project-supplied manifest link.
    if not stat.S_ISDIR(pdir.lstat().st_mode):
        return ""
    _build_file_digest(pdir / "package.json")
    package = json.loads((pdir / "package.json").read_text(encoding="utf-8"))
    scripts = package.get("scripts", {})
    script = scripts.get("build", "")
    if not isinstance(script, str) or "prebuild" in scripts or "postbuild" in scripts:
        return ""
    parts = [part.strip() for part in script.split("&&")]
    if len(parts) == 2 and parts[0] in _BUILD_CHECKS:
        script = parts[1]
    else:
        script = script.strip()
    output = _BUILD_OUTPUTS.get(script, "")
    if not output:
        return ""
    # Do not execute configuration to discover output paths. Conservatively
    # decline custom output/root/environment layouts, even in comments.
    for config in sorted(pdir.glob("*.config.*")):
        _build_file_digest(config)
        text = config.read_text(encoding="utf-8")
        if re.search(
            r"\b(?:outDir|distDir|buildDirectory|envDir|root|output)\b|process\.env|import\.meta\.env",
            text,
        ):
            return ""
    return output


def npm_build_fingerprint(project_dir: str | Path, build_cmd: str) -> str:
    """Hash all local inputs; an empty string is an explicit cache miss.

    Environment files are hashed locally as bytes, never copied to receipts or
    diagnostics. Only the recognized output root, Git and dependencies are
    excluded; dependency receipts and installed resolution metadata are included
    separately. No extension allowlist can silently omit a static asset.
    """
    pdir = Path(project_dir)
    try:
        output = _production_output(pdir, build_cmd)
        if not output:
            return ""
        manifest = _build_tree_manifest(pdir, skip=_BUILD_SKIP_DIRS | {output})
        nm = pdir / "node_modules"
        if not stat.S_ISDIR(nm.lstat().st_mode):
            return ""
        for name in (".package-lock.json", ".skyn3t-install.json", ".skyn3t-docker-install.json"):
            path = nm / name
            if path.exists() or path.is_symlink():
                manifest["node_modules/" + name] = _build_file_digest(path)
        for tool in ("vite", "astro", "next", "typescript"):
            tool_dir = nm / tool
            if tool_dir.exists() or tool_dir.is_symlink():
                if not stat.S_ISDIR(tool_dir.lstat().st_mode):
                    return ""
                manifest[f"node_modules/{tool}/package.json"] = _build_file_digest(
                    tool_dir / "package.json"
                )
        payload = [build_cmd, os.name, npm_env(), manifest]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _log.warning("npm.build_cache_inputs_unavailable", error=type(exc).__name__)
        return ""


def _build_receipt(pdir: Path, build_cmd: str) -> dict | None:
    try:
        fingerprint = npm_build_fingerprint(pdir, build_cmd)
        if not fingerprint:
            return None
        output = _production_output(pdir, build_cmd)
        manifest = _build_tree_manifest(pdir / output)
        files = [name for name in manifest if not name.endswith("/")]
        if output == ".next":
            complete = (
                "BUILD_ID" in files
                and (pdir / output / "BUILD_ID").stat().st_size > 0
                and any(name.startswith("server/") for name in files)
                and any(name.startswith("static/") for name in files)
            )
        else:
            complete = "index.html" in files and (pdir / output / "index.html").stat().st_size > 0
        if not complete:
            return None
        return {
            "version": _BUILD_STAMP_VERSION,
            "build_cmd": build_cmd,
            "fingerprint": fingerprint,
            "output": output,
            "output_digest": hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _log.warning("npm.build_cache_outputs_unavailable", error=type(exc).__name__)
        return None


def npm_install_stamp_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "node_modules" / ".skyn3t-install.json"


def npm_docker_install_stamp_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "node_modules" / ".skyn3t-docker-install.json"


def npm_build_stamp_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "node_modules" / ".skyn3t-build.json"


def foreign_node_modules_reason(project_dir: str | Path) -> str:
    """Return why ``node_modules`` cannot be reused by the host, else ``""``.

    Docker proof installs native packages such as esbuild for Linux. If that tree
    is later reused by local macOS preview/build, Vite fails before reading app
    source. The Docker stamp is intentionally separate from the host install
    stamp; this helper makes the host path discard the foreign tree first.
    """
    pdir = Path(project_dir)
    if not (pdir / "node_modules").is_dir():
        return ""
    stamp = npm_docker_install_stamp_path(pdir)
    if not stamp.is_file():
        return ""
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "docker"
    if not isinstance(data, dict):
        return "docker"
    backend = str(data.get("backend") or "docker").strip() or "docker"
    container_os = str(data.get("container_os") or "").strip()
    return f"{backend}:{container_os}" if container_os else backend


def discard_foreign_node_modules(project_dir: str | Path) -> str:
    """Remove host-incompatible ``node_modules`` and return the reason removed."""
    reason = foreign_node_modules_reason(project_dir)
    if not reason:
        return ""
    try:
        shutil.rmtree(Path(project_dir) / "node_modules")
    except FileNotFoundError:
        pass
    except OSError:
        return ""
    return reason


def npm_install_current(project_dir: str | Path) -> bool:
    pdir = Path(project_dir)
    if not (pdir / "package.json").is_file() or not (pdir / "node_modules").is_dir():
        return False
    stamp = npm_install_stamp_path(pdir)
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and data.get("version") == _STAMP_VERSION
        and data.get("fingerprint") == npm_install_fingerprint(pdir)
    )


def mark_npm_install_current(project_dir: str | Path, *, action: str = "install") -> None:
    pdir = Path(project_dir)
    stamp = npm_install_stamp_path(pdir)
    payload = {
        "version": _STAMP_VERSION,
        "action": action,
        "fingerprint": npm_install_fingerprint(pdir),
    }
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def npm_build_current(project_dir: str | Path, build_cmd: str) -> bool:
    pdir = Path(project_dir)
    stamp = npm_build_stamp_path(pdir)
    try:
        if not stat.S_ISDIR(stamp.parent.lstat().st_mode):
            return False
        _build_file_digest(stamp)
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("version") != _BUILD_STAMP_VERSION:
        return False
    current = _build_receipt(pdir, build_cmd)
    return current is not None and data == current


def invalidate_npm_build(project_dir: str | Path) -> bool:
    """A failed rebuild must not leave a previous success receipt reusable."""
    stamp = npm_build_stamp_path(project_dir)
    try:
        if stat.S_ISDIR(stamp.parent.lstat().st_mode):
            stamp.unlink(missing_ok=True)
            return True
        _log.warning("npm.build_cache_invalidation_failed", error="unsafe_directory")
    except FileNotFoundError:
        return True
    except OSError as exc:
        _log.warning("npm.build_cache_invalidation_failed", error=type(exc).__name__)
    return False


def mark_npm_build_current(project_dir: str | Path, build_cmd: str) -> None:
    """Called only after the complete declared production build succeeds."""
    import tempfile

    pdir = Path(project_dir)
    stamp = npm_build_stamp_path(pdir)
    if not invalidate_npm_build(pdir):
        return
    payload = _build_receipt(pdir, build_cmd)
    if payload is None:
        return
    temporary = None
    try:
        if not stat.S_ISDIR(stamp.parent.lstat().st_mode):
            return
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=stamp.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(payload, sort_keys=True))
        os.replace(temporary, stamp)
    except OSError as exc:
        _log.warning("npm.build_cache_write_failed", error=type(exc).__name__)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                _log.warning("npm.build_cache_cleanup_failed", error=type(exc).__name__)
