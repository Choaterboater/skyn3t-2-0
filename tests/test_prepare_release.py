from __future__ import annotations

import gzip
import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.prepare_release import (
    SIGNATURE_ASSET_SUFFIXES,
    normalize_gzip_mtime,
    normalize_sdist_archive,
    release_asset_names,
    validate_existing_release_assets,
    validate_release_ancestry,
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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def test_release_tag_must_match_project_version() -> None:
    assert validate_release_tag("v2.0.0", "2.0.0") == "2.0.0"
    with pytest.raises(ValueError, match="does not match"):
        validate_release_tag("v2.0.1", "2.0.0")
    with pytest.raises(ValueError, match="form"):
        validate_release_tag("release-2.0.0", "2.0.0")


def test_release_commit_must_be_contained_in_main(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    (repo / "proof.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "proof.txt")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "main")
    main_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "switch", "-c", "unmerged-release")
    (repo / "proof.txt").write_text("unmerged\n", encoding="utf-8")
    _git(repo, "add", "proof.txt")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "unmerged")
    unmerged_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")

    assert validate_release_ancestry(main_sha, "main", repo=repo) == (main_sha, main_sha)
    with pytest.raises(ValueError, match="not contained in main"):
        validate_release_ancestry(unmerged_sha, "main", repo=repo)


def test_existing_release_assets_allow_only_exact_files_and_companion_signatures(
    tmp_path: Path,
) -> None:
    release, _ = _release_pair(tmp_path)
    write_checksums(
        [
            release / "skyn3t-2.0.0-py3-none-any.whl",
            release / "skyn3t-2.0.0.tar.gz",
        ],
        release / "SHA256SUMS",
    )
    expected = release_asset_names(release)
    wheel = next(name for name in expected if name.endswith(".whl"))
    existing = expected | {f"{wheel}.sig", "SHA256SUMS.asc"}

    assert validate_existing_release_assets(existing, expected) == existing
    assert ".sig" in SIGNATURE_ASSET_SUFFIXES
    with pytest.raises(ValueError, match="unexpected assets"):
        validate_existing_release_assets(existing | {"release-notes.txt"}, expected)
    with pytest.raises(ValueError, match="unexpected assets"):
        validate_existing_release_assets(existing | {"unrelated-payload.sig"}, expected)


def test_release_asset_names_requires_checksum_manifest(tmp_path: Path) -> None:
    release, _ = _release_pair(tmp_path)

    with pytest.raises(ValueError, match="checksum manifest is missing"):
        release_asset_names(release)


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
