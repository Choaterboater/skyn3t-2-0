from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from scripts.prepare_release import (
    normalize_gzip_mtime,
    normalize_sdist_archive,
    validate_release_tag,
    verify_reproducible,
    write_checksums,
)


def _sdist_bytes(*, gzip_time: int, member_time: int, reverse: bool) -> bytes:
    payload = io.BytesIO()
    names = ["skyn3t-2.0.0/PKG-INFO", "skyn3t-2.0.0/skyn3t/__init__.py"]
    if reverse:
        names.reverse()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for index, name in enumerate(names):
            content = f"stable-content-{index if not reverse else 1 - index}\n".encode()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = member_time + index
            info.uid = 1000 + index
            info.gid = 2000 + index
            info.uname = "builder"
            info.gname = "builder"
            archive.addfile(info, io.BytesIO(content))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="sdist.tar", mode="wb", fileobj=compressed, mtime=gzip_time) as gz:
        gz.write(payload.getvalue())
    return compressed.getvalue()


def _release_pair(root: Path, *, left_time: int = 10, right_time: int = 20) -> tuple[Path, Path]:
    left = root / "left"
    right = root / "right"
    left.mkdir()
    right.mkdir()
    wheel = b"deterministic wheel bytes"
    (left / "skyn3t-2.0.0-py3-none-any.whl").write_bytes(wheel)
    (right / "skyn3t-2.0.0-py3-none-any.whl").write_bytes(wheel)
    (left / "skyn3t-2.0.0.tar.gz").write_bytes(
        _sdist_bytes(gzip_time=left_time, member_time=left_time, reverse=False)
    )
    (right / "skyn3t-2.0.0.tar.gz").write_bytes(
        _sdist_bytes(gzip_time=right_time, member_time=right_time, reverse=True)
    )
    return left, right


def test_release_tag_must_match_project_version() -> None:
    assert validate_release_tag("v2.0.0", "2.0.0") == "2.0.0"
    with pytest.raises(ValueError, match="does not match"):
        validate_release_tag("v2.0.1", "2.0.0")
    with pytest.raises(ValueError, match="form"):
        validate_release_tag("release-2.0.0", "2.0.0")


def test_gzip_timestamp_normalization_preserves_payload(tmp_path: Path) -> None:
    archive = tmp_path / "package.tar.gz"
    archive.write_bytes(gzip.compress(b"payload", mtime=42))

    normalize_gzip_mtime(archive, 1_700_000_000)

    assert int.from_bytes(archive.read_bytes()[4:8], "little") == 1_700_000_000
    assert gzip.decompress(archive.read_bytes()) == b"payload"


def test_sdist_normalization_stabilizes_member_order_owner_and_time(tmp_path: Path) -> None:
    archive = tmp_path / "package.tar.gz"
    epoch = 1_700_000_000
    archive.write_bytes(_sdist_bytes(gzip_time=42, member_time=99, reverse=True))

    normalize_sdist_archive(archive, epoch)

    assert int.from_bytes(archive.read_bytes()[4:8], "little") == epoch
    with tarfile.open(archive, mode="r:gz") as normalized:
        members = normalized.getmembers()
        assert [member.name for member in members] == sorted(member.name for member in members)
        assert all(member.mtime == epoch for member in members)
        assert all(member.uid == member.gid == 0 for member in members)
        assert all(member.uname == member.gname == "" for member in members)


def test_reproducibility_comparison_normalizes_sdist_and_writes_checksums(
    tmp_path: Path,
) -> None:
    left, right = _release_pair(tmp_path)

    artifacts = verify_reproducible(left, right, epoch=1_700_000_000)
    output = write_checksums(artifacts, left / "SHA256SUMS")

    assert [path.name for path in artifacts] == [
        "skyn3t-2.0.0-py3-none-any.whl",
        "skyn3t-2.0.0.tar.gz",
    ]
    lines = output.read_text(encoding="ascii").splitlines()
    assert len(lines) == 2
    for artifact, line in zip(artifacts, lines, strict=True):
        assert line == f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}"


def test_reproducibility_comparison_rejects_changed_payload(tmp_path: Path) -> None:
    left, right = _release_pair(tmp_path)
    (right / "skyn3t-2.0.0-py3-none-any.whl").write_bytes(b"changed")

    with pytest.raises(ValueError, match="not reproducible"):
        verify_reproducible(left, right, epoch=1_700_000_000)


def test_reproducibility_comparison_requires_one_wheel_and_sdist(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()

    with pytest.raises(ValueError, match="exactly one wheel and one sdist"):
        verify_reproducible(left, right, epoch=1_700_000_000)
