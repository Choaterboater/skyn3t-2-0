"""Symlink-escape confinement for the two write paths that lacked the guard.

``packaging_agent.package_dir`` and ``config_ui_agent.generate_config_ui`` write
hardcoded rel paths — but into a GENERATED project tree. A symlinked
subdirectory (``src/`` or ``app/`` pointing anywhere) would redirect the write
outside the project root. Same defect class as the workstream-1 path-traversal
fix; these sites now use the same confinement idiom as code_agent/_write_files,
proof_run._confine, llm._safe and code_improver._confined.
"""
from __future__ import annotations

from skyn3t.agents.config_ui_agent import generate_config_ui
from skyn3t.agents.packaging_agent import PackagingAgent
from skyn3t.studio.config_spec import ConfigKey, ConfigSpec


def test_confined_path_fails_closed_on_unresolvable_rel(tmp_path):
    """A rel that makes resolve() itself raise (e.g. an embedded NUL byte) must
    yield the guard's documented fail-closed None — never an uncaught
    ValueError. Same contract as proof_run._confine, the idiom this mirrors."""
    from skyn3t.agents._common import confined_path

    assert confined_path(tmp_path, "a\0b") is None


def test_package_dir_skips_write_through_symlinked_dir(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "public").mkdir()  # web marker -> has_web -> writes src/config.js
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj / "src").symlink_to(outside)

    res = PackagingAgent.package_dir(proj)

    assert not (outside / "config.js").exists()
    assert "src/config.js" not in res["files_written"]
    # in-root writes still happen
    assert (proj / "README.md").is_file()


def test_generate_config_ui_skips_write_through_symlinked_dir(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj / "app").symlink_to(outside)  # nextjs family writes app/config.js etc.

    spec = ConfigSpec(keys=[ConfigKey(name="API_URL", scope="client")])
    written = generate_config_ui(proj, "nextjs", spec)

    assert not any(outside.iterdir())
    assert written == []
