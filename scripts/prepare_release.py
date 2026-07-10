"""Validate and normalize reproducible Python release artifacts."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import os
import re
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DIST_SUFFIXES = (".whl", ".tar.gz")
_TAG_RE = re.compile(r"v([0-9]+(?:\.[0-9]+){2}(?:[a-zA-Z0-9.-]*)?)\Z")


def project_version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = str(data.get("project", {}).get("version", "")).strip()
    if not version:
        raise ValueError(f"project.version is missing from {pyproject}")
    return version


def validate_release_tag(tag: str, version: str | None = None) -> str:
    """Require an immutable release tag to match the package version exactly."""
    match = _TAG_RE.fullmatch(str(tag or "").strip())
    if match is None:
        raise ValueError("release tag must have the form vMAJOR.MINOR.PATCH")
    expected = version or project_version()
    if match.group(1) != expected:
        raise ValueError(f"release tag {tag!r} does not match project version {expected!r}")
    return expected


def _validate_epoch(epoch: int) -> None:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or not 0 <= epoch <= 0xFFFFFFFF:
        raise ValueError("SOURCE_DATE_EPOCH must fit the gzip 32-bit timestamp field")


def normalize_gzip_mtime(path: Path, epoch: int) -> None:
    """Set the gzip header timestamp without changing the compressed payload."""
    _validate_epoch(epoch)
    payload = bytearray(path.read_bytes())
    if len(payload) < 18 or payload[:3] != b"\x1f\x8b\x08":
        raise ValueError(f"not a gzip archive: {path}")
    payload[4:8] = epoch.to_bytes(4, "little")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def normalize_sdist_archive(path: Path, epoch: int) -> None:
    """Repack a source distribution with deterministic tar and gzip metadata."""
    _validate_epoch(epoch)
    records: list[tuple[tarfile.TarInfo, bytes | None]] = []
    try:
        with tarfile.open(path, mode="r:gz") as source:
            for member in sorted(
                source.getmembers(), key=lambda item: (item.name, item.type, item.linkname)
            ):
                info = copy.copy(member)
                data: bytes | None = None
                if member.isfile():
                    extracted = source.extractfile(member)
                    if extracted is None:
                        raise ValueError(f"could not read sdist member: {member.name}")
                    data = extracted.read()
                    info.size = len(data)
                info.mtime = epoch
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.devmajor = 0
                info.devminor = 0
                info.pax_headers = {}
                records.append((info, data))
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(f"not a readable source tar archive: {path}") from exc

    tar_payload = io.BytesIO()
    with tarfile.open(fileobj=tar_payload, mode="w", format=tarfile.PAX_FORMAT) as target:
        for info, data in records:
            target.addfile(info, io.BytesIO(data) if data is not None else None)

    compressed = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=compressed,
        mtime=epoch,
    ) as target_gzip:
        target_gzip.write(tar_payload.getvalue())

    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(compressed.getvalue())
    os.replace(temporary, path)


def _distribution_files(directory: Path) -> dict[str, Path]:
    if not directory.is_dir():
        raise ValueError(f"release directory does not exist: {directory}")
    files = {
        path.name: path
        for path in directory.iterdir()
        if path.is_file() and path.name.endswith(_DIST_SUFFIXES)
    }
    wheels = [name for name in files if name.endswith(".whl")]
    sdists = [name for name in files if name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            f"expected exactly one wheel and one sdist in {directory}; "
            f"found wheels={sorted(wheels)}, sdists={sorted(sdists)}"
        )
    return files


def verify_reproducible(primary: Path, comparison: Path, *, epoch: int) -> list[Path]:
    """Normalize sdists and require two independent builds to be byte-identical."""
    left = _distribution_files(primary)
    right = _distribution_files(comparison)
    if set(left) != set(right):
        raise ValueError(
            "release builds produced different filenames; "
            f"left={sorted(left)}, right={sorted(right)}"
        )
    for name in sorted(left):
        if name.endswith(".tar.gz"):
            normalize_sdist_archive(left[name], epoch)
            normalize_sdist_archive(right[name], epoch)
        left_bytes = left[name].read_bytes()
        right_bytes = right[name].read_bytes()
        if left_bytes != right_bytes:
            raise ValueError(f"release artifact is not reproducible: {name}")
    return [left[name] for name in sorted(left)]


def write_checksums(artifacts: list[Path], output: Path) -> Path:
    """Write a stable GNU-compatible SHA-256 manifest for release consumers."""
    if not artifacts:
        raise ValueError("no release artifacts were provided for checksumming")
    names = [path.name for path in artifacts]
    if len(names) != len(set(names)):
        raise ValueError("release artifact names must be unique")
    lines = []
    for path in sorted(artifacts, key=lambda item: item.name):
        if not path.is_file():
            raise ValueError(f"release artifact is missing: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    temporary.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    os.replace(temporary, output)
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    tag = commands.add_parser("check-tag", help="verify tag/package version agreement")
    tag.add_argument("--tag", required=True)

    verify = commands.add_parser("verify", help="normalize and compare two release builds")
    verify.add_argument("--dist", type=Path, required=True)
    verify.add_argument("--compare", type=Path, required=True)
    verify.add_argument("--tag", required=True)
    verify.add_argument("--epoch", type=int, required=True)
    verify.add_argument("--checksums", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    version = validate_release_tag(args.tag)
    if args.command == "check-tag":
        print(f"PASS tag {args.tag} matches skyn3t {version}")
        return
    artifacts = verify_reproducible(args.dist, args.compare, epoch=args.epoch)
    checksum_path = write_checksums(artifacts, args.checksums)
    print(
        f"PASS {len(artifacts)} reproducible release artifacts for skyn3t {version}; "
        f"checksums={checksum_path}"
    )


if __name__ == "__main__":
    main()
