"""Digest-bound reuse of final liveness browser evidence.

The reuse path is deliberately fail closed. Any changed runtime input,
non-canonical route/viewport set, report mismatch, or aliased artifact turns
into a cache miss; the caller then runs the normal isolated preview proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skyn3t.core.stacks import WEB_STACKS
from skyn3t.studio.app_runner import build_run_spec
from skyn3t.studio.visual_proof import DEFAULT_VIEWPORTS, VISUAL_PROOF_SCHEMA_VERSION

REUSABLE_WEB_PROOF_SCHEMA_VERSION = 1
PREVIEW_INPUT_FINGERPRINT_ALGORITHM = "preview-input-sha256-v1"
PROOF_ARTIFACT_DIGEST_ALGORITHM = "proof-artifacts-sha256-v1"
CANONICAL_LIVENESS_ARTIFACT_DIR = ".skyn3t/visual-proof"
PROOF_DESTINATION_MARKER = ".skyn3t-proof-owned.json"
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_ARTIFACT_FILES = 2048
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MARKER_PAYLOAD = {"owner": "skyn3t-proof-ladder", "schema_version": 1}


class _ReuseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReusePromotion:
    hit: bool
    reason: str = ""
    routes: tuple[str, ...] = ()
    proofs: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[str, ...] = ()
    report_sha256: str = ""
    artifact_sha256: str = ""
    runtime_input_fingerprint: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ProofDestination:
    path: Path
    exists: bool
    device: int | None = None
    inode: int | None = None


@dataclass(frozen=True, slots=True)
class _VisualBundle:
    routes: tuple[str, ...]
    viewports: tuple[dict[str, Any], ...]
    proofs: tuple[dict[str, Any], ...]
    artifacts: tuple[str, ...]
    report_sha256: str
    artifact_sha256: str


def _field(digest: Any, value: str) -> None:
    encoded = value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _relative(value: Any, label: str) -> Path:
    if not isinstance(value, str):
        raise _ReuseError(f"{label} must be a relative POSIX path")
    raw = value.strip()
    parts = raw.split("/")
    if (
        not raw
        or raw.startswith("/")
        or "\\" in raw
        or "\x00" in raw
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise _ReuseError(f"{label} is not a confined relative POSIX path")
    return Path(*parts)


def _confined_dir(project_dir: Path, value: Any) -> tuple[Path, str]:
    relative = _relative(value, "artifact_dir")
    cursor = project_dir
    for part in relative.parts:
        cursor /= part
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise _ReuseError(f"artifact_dir is unavailable: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise _ReuseError("artifact_dir contains a symbolic link")
    try:
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(project_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _ReuseError("artifact_dir escapes the generated project") from exc
    if not resolved.is_dir():
        raise _ReuseError("artifact_dir is not a directory")
    return resolved, relative.as_posix()


def _regular_file(root: Path, relative: Path, label: str) -> tuple[Path, os.stat_result]:
    cursor = root
    for part in relative.parts:
        cursor /= part
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise _ReuseError(f"{label} is unavailable: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise _ReuseError(f"{label} contains a symbolic link")
    try:
        cursor.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _ReuseError(f"{label} escapes the evidence root") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise _ReuseError(f"{label} is not a non-empty regular file")
    return cursor, metadata


def _open_regular(root: Path, relative: Path, label: str) -> tuple[int, os.stat_result]:
    path, before = _regular_file(root, relative, label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _ReuseError(f"{label} could not be opened safely: {exc}") from exc
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_dev != before.st_dev
        or opened.st_ino != before.st_ino
    ):
        os.close(descriptor)
        raise _ReuseError(f"{label} changed during validation")
    return descriptor, opened


def _read_json(root: Path, relative: Path, label: str) -> tuple[dict[str, Any], bytes]:
    descriptor, metadata = _open_regular(root, relative, label)
    try:
        if metadata.st_size > _MAX_JSON_BYTES:
            raise _ReuseError(f"{label} is too large")
        raw = b""
        while chunk := os.read(descriptor, 64 * 1024):
            raw += chunk
            if len(raw) > _MAX_JSON_BYTES:
                raise _ReuseError(f"{label} is too large")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise _ReuseError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise _ReuseError(f"{label} must contain a JSON object")
    return payload, raw


def _normalized_routes(routes: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    for route in routes:
        value = str(route or "/").strip() or "/"
        value = "/" + value.lstrip("/")
        if value not in normalized:
            normalized.append(value)
    return normalized or ["/"]


def _prefix(relative: Path, parent: Path) -> bool:
    return (
        len(relative.parts) >= len(parent.parts)
        and relative.parts[: len(parent.parts)] == parent.parts
    )


def preview_input_fingerprint(
    project_dir: str | Path,
    stack: str,
) -> dict[str, Any]:
    """Hash runtime source/config plus built output, excluding proof output."""
    root = Path(project_dir).expanduser().resolve(strict=True)
    normalized_stack = str(stack or "").strip().lower()
    spec = build_run_spec(
        root,
        normalized_stack,
        port=4173,
        allow_secret_passthrough=False,
    )
    if spec is None:
        raise _ReuseError("preview run-spec is unavailable")
    excluded = [
        Path(".git"),
        Path(".skyn3t/visual-proof"),
        Path(".skyn3t/proof-ladder"),
    ]
    if spec.kind == "node":
        excluded.append(Path("node_modules"))
    elif spec.kind == "python_web":
        excluded.extend((Path(".venv"), Path("venv")))

    digest = hashlib.sha256()
    _field(digest, PREVIEW_INPUT_FINGERPRINT_ALGORITHM)
    _field(digest, normalized_stack)
    _field(digest, spec.kind)
    seen: set[str] = set()
    file_count = 0
    byte_count = 0

    def walk_error(exc: OSError) -> None:
        raise _ReuseError(f"runtime inputs could not be enumerated: {exc}") from exc

    for current, directories, filenames in os.walk(
        root,
        followlinks=False,
        onerror=walk_error,
    ):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root)
        kept: list[str] = []
        for name in sorted(directories):
            relative = relative_dir / name
            if any(_prefix(relative, skipped) for skipped in excluded):
                continue
            metadata = (current_path / name).lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise _ReuseError(f"runtime input {relative.as_posix()} is aliased")
            key = relative.as_posix().casefold()
            if key in seen:
                raise _ReuseError("runtime inputs contain a case-colliding path")
            seen.add(key)
            # Liveness may create this internal parent solely to hold excluded
            # proof output. Its delivered children (for example product.json)
            # remain fingerprinted normally.
            if relative != Path(".skyn3t"):
                _field(
                    digest,
                    f"directory:{relative.as_posix()}:{stat.S_IMODE(metadata.st_mode)}",
                )
            kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            relative = relative_dir / name
            if any(_prefix(relative, skipped) for skipped in excluded):
                continue
            key = relative.as_posix().casefold()
            if key in seen:
                raise _ReuseError("runtime inputs contain a case-colliding path")
            seen.add(key)
            path = current_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise _ReuseError(f"runtime input {relative.as_posix()} is aliased")
            _field(digest, f"file:{relative.as_posix()}:{stat.S_IMODE(metadata.st_mode)}")
            _field(digest, str(metadata.st_size))
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                ):
                    raise _ReuseError(
                        f"runtime input {relative.as_posix()} changed during validation"
                    )
                read_bytes = 0
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                    byte_count += len(chunk)
                    read_bytes += len(chunk)
                after = os.fstat(descriptor)
                if (
                    read_bytes != opened.st_size
                    or after.st_size != opened.st_size
                    or after.st_mtime_ns != opened.st_mtime_ns
                ):
                    raise _ReuseError(
                        f"runtime input {relative.as_posix()} changed during validation"
                    )
            finally:
                os.close(descriptor)
            file_count += 1
    return {
        "algorithm": PREVIEW_INPUT_FINGERPRINT_ALGORITHM,
        "sha256": digest.hexdigest(),
        "file_count": file_count,
        "byte_count": byte_count,
    }


def _artifact_digest(root: Path, artifacts: Sequence[str]) -> str:
    if len(artifacts) > _MAX_ARTIFACT_FILES:
        raise _ReuseError("visual proof contains too many artifacts")
    digest = hashlib.sha256()
    _field(digest, PROOF_ARTIFACT_DIGEST_ALGORITHM)
    total = 0
    for value in sorted(artifacts):
        relative = _relative(value, "proof artifact")
        descriptor, metadata = _open_regular(root, relative, f"proof artifact {value}")
        total += metadata.st_size
        if total > _MAX_ARTIFACT_BYTES:
            os.close(descriptor)
            raise _ReuseError("visual proof artifacts exceed the size limit")
        _field(digest, value)
        _field(digest, str(metadata.st_size))
        try:
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
        finally:
            os.close(descriptor)
    return digest.hexdigest()


def _validate_visual_report(
    root: Path,
    stack: str,
    report_path: str,
    expected_routes: Sequence[str] | None,
) -> _VisualBundle:
    report_relative = _relative(report_path, "report_path")
    batch, report_bytes = _read_json(root, report_relative, "visual proof report")
    expected_viewports = tuple(viewport.to_dict() for viewport in DEFAULT_VIEWPORTS)
    if (
        batch.get("schema_version") != VISUAL_PROOF_SCHEMA_VERSION
        or str(batch.get("stack") or "").strip().lower() != stack
        or batch.get("passed") is not True
        or batch.get("skipped") is not False
        or batch.get("viewports") != list(expected_viewports)
    ):
        raise _ReuseError("visual proof report is not a fully passing compatible report")
    proofs = batch.get("routes")
    if (
        not isinstance(proofs, list)
        or not proofs
        or not all(isinstance(item, dict) for item in proofs)
    ):
        raise _ReuseError("visual proof route evidence is missing")

    routes: list[str] = []
    artifacts = [report_relative.as_posix()]
    for proof in proofs:
        route = proof.get("route")
        if not isinstance(route, str) or route != _normalized_routes([route])[0] or route in routes:
            raise _ReuseError("visual proof routes are not canonical and unique")
        routes.append(route)
        if (
            proof.get("schema_version") != VISUAL_PROOF_SCHEMA_VERSION
            or str(proof.get("stack") or "").strip().lower() != stack
            or proof.get("passed") is not True
            or proof.get("skipped") is not False
            or str(proof.get("reason") or "").strip()
        ):
            raise _ReuseError(f"visual proof route {route} did not fully pass")
        route_report = _relative(proof.get("report_path"), f"route {route} report")
        persisted_route, _ = _read_json(root, route_report, f"route {route} report")
        if persisted_route != proof:
            raise _ReuseError(f"route {route} report disagrees with the batch report")
        artifacts.append(route_report.as_posix())

        viewports = proof.get("viewports")
        if not isinstance(viewports, list) or len(viewports) != len(expected_viewports):
            raise _ReuseError(f"route {route} has incomplete viewport evidence")
        for expected, viewport in zip(expected_viewports, viewports, strict=True):
            observed = {key: viewport.get(key) for key in ("name", "width", "height")}
            if (
                observed != expected
                or viewport.get("passed") is not True
                or viewport.get("skipped") is not False
                or str(viewport.get("reason") or "").strip()
                or viewport.get("issues") != []
                or viewport.get("console_errors") != []
                or viewport.get("page_errors") != []
            ):
                raise _ReuseError(f"route {route} viewport evidence did not fully pass")
            screenshot = _relative(viewport.get("screenshot"), f"route {route} screenshot")
            if screenshot.suffix.lower() != ".png":
                raise _ReuseError(f"route {route} screenshot is not a PNG artifact")
            _regular_file(root, screenshot, f"route {route} screenshot")
            artifacts.append(screenshot.as_posix())

    normalized_expected = tuple(_normalized_routes(expected_routes or routes))
    if tuple(routes) != normalized_expected:
        raise _ReuseError("visual proof routes do not exactly match requested routes")
    if len(set(artifacts)) != len(artifacts):
        raise _ReuseError("visual proof reuses an artifact path")
    artifact_tuple = tuple(sorted(artifacts))
    return _VisualBundle(
        routes=tuple(routes),
        viewports=expected_viewports,
        proofs=tuple(proofs),
        artifacts=artifact_tuple,
        report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        artifact_sha256=_artifact_digest(root, artifact_tuple),
    )


def build_reusable_web_proof(
    project_dir: str | Path,
    stack: str,
    *,
    artifact_dir: str | Path,
    runtime_input_fingerprint: Mapping[str, Any],
    report_path: str = "visual-proof.json",
) -> dict[str, Any]:
    """Build a portable, digest-bound reference to final liveness evidence."""
    project = Path(project_dir).expanduser().resolve(strict=True)
    normalized_stack = str(stack or "").strip().lower()
    if normalized_stack not in WEB_STACKS:
        raise _ReuseError("liveness proof reuse is only supported for web stacks")
    evidence_root, artifact_relative = _confined_dir(project, str(artifact_dir))
    if artifact_relative != CANONICAL_LIVENESS_ARTIFACT_DIR:
        raise _ReuseError(f"liveness proof reuse requires {CANONICAL_LIVENESS_ARTIFACT_DIR}")
    bundle = _validate_visual_report(evidence_root, normalized_stack, report_path, None)
    current_fingerprint = preview_input_fingerprint(project, normalized_stack)
    if (
        not isinstance(runtime_input_fingerprint, Mapping)
        or dict(runtime_input_fingerprint) != current_fingerprint
    ):
        raise _ReuseError("runtime inputs changed while liveness evidence was collected")
    return {
        "schema_version": REUSABLE_WEB_PROOF_SCHEMA_VERSION,
        "source": "liveness",
        "stack": normalized_stack,
        "runtime_input_fingerprint": current_fingerprint,
        "routes": list(bundle.routes),
        "viewports": list(bundle.viewports),
        "artifact_dir": artifact_relative,
        "report_path": _relative(report_path, "report_path").as_posix(),
        "report_schema_version": VISUAL_PROOF_SCHEMA_VERSION,
        "report_sha256": bundle.report_sha256,
        "artifact_digest_algorithm": PROOF_ARTIFACT_DIGEST_ALGORITHM,
        "artifact_sha256": bundle.artifact_sha256,
        "artifacts": list(bundle.artifacts),
    }


def _copy_regular(source_root: Path, relative: Path, destination: Path) -> None:
    descriptor, _ = _open_regular(source_root, relative, f"proof artifact {relative.as_posix()}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as output:
            while chunk := os.read(descriptor, 1024 * 1024):
                output.write(chunk)
    finally:
        os.close(descriptor)


def _absolute_path(value: str | Path) -> Path:
    return Path(os.path.abspath(Path(value).expanduser()))


def prepare_proof_artifact_root(value: str | Path) -> Path:
    """Create a proof root one component at a time without accepting aliases."""
    root = _absolute_path(value)
    cursor = Path(root.anchor)
    for part in root.parts[1:]:
        cursor /= part
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            try:
                cursor.mkdir()
                metadata = cursor.lstat()
            except OSError as exc:
                raise _ReuseError(f"proof artifact directory unavailable: {exc}") from exc
        except OSError as exc:
            raise _ReuseError(f"proof artifact directory unavailable: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _ReuseError("proof artifact directory contains an aliased component")
    return root


def _write_destination_marker(target: Path) -> None:
    marker = target / PROOF_DESTINATION_MARKER
    try:
        with marker.open("x", encoding="utf-8") as handle:
            json.dump(_MARKER_PAYLOAD, handle, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise _ReuseError(f"proof destination ownership marker failed: {exc}") from exc


def prepare_owned_proof_destination(value: str | Path) -> ProofDestination:
    """Create/claim an empty destination, or validate an existing owned one."""
    target = _absolute_path(value)
    parent = prepare_proof_artifact_root(target.parent)
    target = parent / target.name
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        try:
            target.mkdir()
        except OSError as exc:
            raise _ReuseError(f"proof destination unavailable: {exc}") from exc
        _write_destination_marker(target)
    except OSError as exc:
        raise _ReuseError(f"proof destination unavailable: {exc}") from exc
    else:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _ReuseError("proof destination is aliased or not a directory")
        marker = target / PROOF_DESTINATION_MARKER
        if not marker.exists() and not marker.is_symlink():
            try:
                is_empty = next(target.iterdir(), None) is None
            except OSError as exc:
                raise _ReuseError(f"proof destination could not be inspected: {exc}") from exc
            if not is_empty:
                raise _ReuseError("proof destination is nonempty and not proof-owned")
            _write_destination_marker(target)

    marker_payload, _ = _read_json(
        target,
        Path(PROOF_DESTINATION_MARKER),
        "proof destination ownership marker",
    )
    if marker_payload != _MARKER_PAYLOAD:
        raise _ReuseError("proof destination ownership marker is invalid")
    current = target.lstat()
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise _ReuseError("proof destination changed during ownership validation")
    return ProofDestination(
        path=target,
        exists=True,
        device=current.st_dev,
        inode=current.st_ino,
    )


def promote_reusable_web_proof(
    project_dir: str | Path,
    stack: str,
    routes: Sequence[str],
    candidate: Mapping[str, Any],
    destination: str | Path,
) -> ReusePromotion:
    """Validate and copy reusable evidence, returning a miss on any uncertainty."""
    temp: Path | None = None
    try:
        project = Path(project_dir).expanduser().resolve(strict=True)
        normalized_stack = str(stack or "").strip().lower()
        expected_routes = _normalized_routes(routes)
        expected_viewports = [viewport.to_dict() for viewport in DEFAULT_VIEWPORTS]
        if (
            candidate.get("schema_version") != REUSABLE_WEB_PROOF_SCHEMA_VERSION
            or candidate.get("source") != "liveness"
            or str(candidate.get("stack") or "").strip().lower() != normalized_stack
            or candidate.get("routes") != expected_routes
            or candidate.get("viewports") != expected_viewports
            or candidate.get("report_schema_version") != VISUAL_PROOF_SCHEMA_VERSION
            or candidate.get("artifact_digest_algorithm") != PROOF_ARTIFACT_DIGEST_ALGORITHM
        ):
            raise _ReuseError("reuse candidate contract does not match this proof run")
        source_root, artifact_relative = _confined_dir(project, candidate.get("artifact_dir"))
        if artifact_relative != CANONICAL_LIVENESS_ARTIFACT_DIR:
            raise _ReuseError(f"reuse candidate requires {CANONICAL_LIVENESS_ARTIFACT_DIR}")
        report_path = _relative(candidate.get("report_path"), "report_path").as_posix()
        bundle = _validate_visual_report(
            source_root,
            normalized_stack,
            report_path,
            expected_routes,
        )
        fingerprint = preview_input_fingerprint(project, normalized_stack)
        if (
            candidate.get("runtime_input_fingerprint") != fingerprint
            or candidate.get("report_sha256") != bundle.report_sha256
            or candidate.get("artifact_sha256") != bundle.artifact_sha256
            or candidate.get("artifacts") != list(bundle.artifacts)
        ):
            raise _ReuseError("reuse candidate digest or artifact manifest changed")

        owned_target = prepare_owned_proof_destination(destination)
        target = owned_target.path
        temp = target.parent / f".{target.name}-reuse-{uuid.uuid4().hex}"
        temp.mkdir()
        _write_destination_marker(temp)
        for value in bundle.artifacts:
            relative = _relative(value, "proof artifact")
            _copy_regular(source_root, relative, temp / relative)
        copied = _validate_visual_report(temp, normalized_stack, report_path, expected_routes)
        if (
            copied.report_sha256 != bundle.report_sha256
            or copied.artifact_sha256 != bundle.artifact_sha256
            or copied.artifacts != bundle.artifacts
        ):
            raise _ReuseError("promoted proof artifacts failed digest validation")
        current_target = prepare_owned_proof_destination(target)
        if (
            current_target.device != owned_target.device
            or current_target.inode != owned_target.inode
        ):
            raise _ReuseError("proof destination changed before promotion")
        backup = target.parent / f".{target.name}-previous-{uuid.uuid4().hex}"
        target.rename(backup)
        moved = backup.lstat()
        if moved.st_dev != owned_target.device or moved.st_ino != owned_target.inode:
            if not target.exists():
                backup.rename(target)
            raise _ReuseError("proof destination changed during promotion")
        try:
            temp.replace(target)
            temp = None
        except Exception:
            if not target.exists():
                backup.rename(target)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        return ReusePromotion(
            hit=True,
            routes=bundle.routes,
            proofs=bundle.proofs,
            artifacts=bundle.artifacts,
            report_sha256=bundle.report_sha256,
            artifact_sha256=bundle.artifact_sha256,
            runtime_input_fingerprint=fingerprint,
        )
    except Exception as exc:  # noqa: BLE001 - every uncertainty is a normal cache miss
        reason = re.sub(r"\s+", " ", str(exc)).strip() or type(exc).__name__
        return ReusePromotion(hit=False, reason=reason[:500])
    finally:
        if temp is not None and temp.exists():
            shutil.rmtree(temp, ignore_errors=True)


__all__ = [
    "CANONICAL_LIVENESS_ARTIFACT_DIR",
    "PREVIEW_INPUT_FINGERPRINT_ALGORITHM",
    "PROOF_ARTIFACT_DIGEST_ALGORITHM",
    "PROOF_DESTINATION_MARKER",
    "REUSABLE_WEB_PROOF_SCHEMA_VERSION",
    "ProofDestination",
    "ReusePromotion",
    "build_reusable_web_proof",
    "prepare_owned_proof_destination",
    "prepare_proof_artifact_root",
    "preview_input_fingerprint",
    "promote_reusable_web_proof",
]
