"""Validate release-wheel integrity, contents, and bundled dashboard parity."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
UI_DIST = ROOT / "skyn3t" / "web" / "ui" / "dist"
UI_PREFIX = "skyn3t/web/ui/dist/"
PROJECT_VERSION = str(
    tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
)


def _wheel_path(value: str) -> Path:
    matches = sorted(ROOT.glob(value)) if any(char in value for char in "*?[") else [Path(value)]
    if len(matches) != 1 or not matches[0].is_file():
        raise argparse.ArgumentTypeError(f"expected one wheel for {value!r}, found {len(matches)}")
    return matches[0].resolve()


def _verify_record(archive: zipfile.ZipFile, names: set[str], record_name: str) -> None:
    rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    assert {row[0] for row in rows} == names, "RECORD does not enumerate every wheel member"
    for path, digest_spec, size_text in rows:
        if path == record_name:
            assert not digest_spec and not size_text
            continue
        assert digest_spec and size_text, f"missing RECORD integrity fields for {path}"
        algorithm, encoded = digest_spec.split("=", 1)
        data = archive.read(path)
        digest = hashlib.new(algorithm, data).digest()
        actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert actual == encoded, f"RECORD hash mismatch for {path}"
        assert len(data) == int(size_text), f"RECORD size mismatch for {path}"


def check_wheel(wheel: Path) -> dict[str, int | str]:
    assert UI_DIST.is_dir(), f"dashboard dist is missing: {UI_DIST}"
    expected_ui = {
        UI_PREFIX + path.relative_to(UI_DIST).as_posix(): path
        for path in UI_DIST.rglob("*")
        if path.is_file()
    }

    with zipfile.ZipFile(wheel) as archive:
        assert archive.testzip() is None, "wheel ZIP integrity check failed"
        names = set(archive.namelist())
        wheel_ui = {name for name in names if name.startswith(UI_PREFIX) and not name.endswith("/")}
        assert wheel_ui == set(expected_ui), (
            f"dashboard wheel parity failed; missing={sorted(set(expected_ui) - wheel_ui)}, "
            f"extra={sorted(wheel_ui - set(expected_ui))}"
        )
        for name, source in expected_ui.items():
            assert archive.read(name) == source.read_bytes(), (
                f"dashboard wheel asset is stale or mutated relative to dist: {name}"
            )

        required_ui = {
            UI_PREFIX + "index.html",
            UI_PREFIX + "THIRD_PARTY_NOTICES.txt",
            UI_PREFIX + "fonts/NOTICE.txt",
            UI_PREFIX + "fonts/OFL-1.1.txt",
        }
        assert required_ui <= names, f"missing release notices: {sorted(required_ui - names)}"

        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        assert len(metadata_names) == 1, "expected exactly one dist-info METADATA"
        dist_info = metadata_names[0].removesuffix("METADATA")
        assert dist_info + "licenses/LICENSE" in names, "project LICENSE is missing"
        assert dist_info + "entry_points.txt" in names, "console entry point is missing"

        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        assert metadata["Name"] == "skyn3t"
        assert metadata["Version"] == PROJECT_VERSION
        assert metadata["License-Expression"] == "MIT"
        assert metadata["Requires-Python"] == ">=3.11"

        record_name = dist_info + "RECORD"
        assert record_name in names
        _verify_record(archive, names, record_name)

        forbidden_parts = {"node_modules", "__pycache__", ".git", "data", "logs"}
        forbidden_suffixes = (".pyc", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")
        for name in names:
            path = PurePosixPath(name)
            parts = set(path.parts)
            assert not parts.intersection(forbidden_parts), f"forbidden wheel member: {name}"
            assert not name.lower().endswith(forbidden_suffixes), f"forbidden wheel member: {name}"
            assert not path.name.startswith(".env"), f"forbidden wheel member: {name}"
            assert not name.startswith((UI_PREFIX + "src/", UI_PREFIX + "test/")), (
                f"frontend source/test leaked into wheel: {name}"
            )

    return {
        "wheel": wheel.name,
        "members": len(names),
        "ui_files": len(expected_ui),
        "bytes": wheel.stat().st_size,
    }


def compare_wheels(left: Path, right: Path) -> int:
    """Require normalized member names and bytes to match across two wheels."""
    with zipfile.ZipFile(left) as left_archive, zipfile.ZipFile(right) as right_archive:
        left_names = set(left_archive.namelist())
        right_names = set(right_archive.namelist())
        assert left_names == right_names, (
            f"wheel member parity failed; missing={sorted(left_names - right_names)}, "
            f"extra={sorted(right_names - left_names)}"
        )
        for name in sorted(left_names):
            assert left_archive.read(name) == right_archive.read(name), (
                f"wheel member differs between direct and sdist builds: {name}"
            )
    return len(left_names)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=_wheel_path)
    parser.add_argument(
        "--compare",
        type=_wheel_path,
        help="Wheel rebuilt from the sdist; every member and byte must match.",
    )
    args = parser.parse_args()
    result = check_wheel(args.wheel)
    print(
        f"PASS {result['wheel']}: {result['members']} members, "
        f"{result['ui_files']} UI files, {result['bytes']} bytes"
    )
    if args.compare is not None:
        check_wheel(args.compare)
        members = compare_wheels(args.wheel, args.compare)
        print(f"PASS direct/sdist wheel parity: {members} identical members")


if __name__ == "__main__":
    main()
