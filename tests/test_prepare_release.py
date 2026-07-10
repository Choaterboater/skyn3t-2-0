from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import pytest

from scripts.prepare_release import (
    normalize_gzip_mtime,
    validate_release_tag,
    verify_reproducible,
    write_checksums,
)


def _release_pair(root: Path, *, left_time: int = 10, right_time: int = 20) -> tuple[Path, Path]:
    left = root / "left"
    right = root / "right"
    left.mkdir()
    right.mkdir()
    wheel = b"deterministic wheel bytes"
    (left / "skyn3t-2.0.0-py3-none-any.whl").write_bytes(wheel)
    (right / "skyn3t-2.0.0-py3-none-any.whl").write_bytes(wheel)
    content = b"deterministic tar payload"
    (left / "skyn3t-2.0.0.tar.gz").write_bytes(gzip.compress(content, mtime=left_time))
    (right / "skyn3t-2.0.0.tar.gz").write_bytes(gzip.compress(content, mtime=right_time))
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
