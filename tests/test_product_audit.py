from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from skyn3t.cli.main import app

runner = CliRunner()


def _fixture_repo(root: Path) -> Path:
    (root / "skyn3t" / "studio").mkdir(parents=True)
    (root / "skyn3t" / "agents").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "tests").mkdir()
    (root / "README.md").write_text("# SkyN3t\n\nApp factory overview.\n", encoding="utf-8")
    (root / "STATUS.md").write_text("_Last reviewed: 2026-01-01_\n\nKnown status.\n", encoding="utf-8")
    (root / "docs" / "ROADMAP.md").write_text(
        "| Item | Status |\n| --- | --- |\n| Proof-run | ✅ |\n| Browser extension | ⬜ |\n",
        encoding="utf-8",
    )
    (root / "docs" / "FUTURE_IDEAS.md").write_text(
        "Brief-fit verdicts\nBench = the factory's exam\nLive build legible\n",
        encoding="utf-8",
    )
    (root / "skyn3t" / "studio" / "runner.py").write_text(
        "def run():\n    # TODO: audit this path\n    return 'stage debug proof liveness'\n",
        encoding="utf-8",
    )
    (root / "skyn3t" / "agents" / "reviewer.py").write_text(
        "class ReviewerAgent: pass\n", encoding="utf-8"
    )
    (root / "tests" / "test_runner.py").write_text(
        "def test_runner():\n    assert True\n", encoding="utf-8"
    )
    return root


def test_product_audit_report_contains_factory_sections(tmp_path: Path) -> None:
    from skyn3t.audit import run_product_audit

    repo = _fixture_repo(tmp_path / "repo")
    report = run_product_audit(repo_root=repo, include_tests=True, max_findings=8, use_llm=False)

    titles = [section.title for section in report.sections]
    assert "Repo Review" in titles
    assert "Debug Audit" in titles
    assert "Product Rating" in titles
    assert "Gap Analysis" in titles
    assert "Explorer Map" in titles
    assert "Comparison Rubric" in titles
    assert report.overall_rating > 0
    assert report.prioritized_handoff


def test_product_audit_markdown_and_json_are_handoff_shaped(tmp_path: Path) -> None:
    from skyn3t.audit import render_markdown, run_product_audit

    repo = _fixture_repo(tmp_path / "repo")
    report = run_product_audit(repo_root=repo, include_tests=True, max_findings=8, use_llm=False)
    md = render_markdown(report)
    data = report.to_dict()

    assert "# Skyn3t Product Audit" in md
    assert "## Executive Summary" in md
    assert "## Product Rating Table" in md
    assert "## Top 10 Prioritized Next Steps" in md
    assert "## Detailed Handoff" in md
    assert data["repo_state"]["repo_root"] == repo.name
    assert str(repo) not in md
    assert str(repo) not in json.dumps(data)
    assert data["sections"]


def test_product_audit_excludes_local_agent_worktrees(tmp_path: Path) -> None:
    from skyn3t.audit import run_product_audit

    repo = _fixture_repo(tmp_path / "repo")
    noisy = repo / ".claude" / "worktrees" / "old" / "skyn3t"
    noisy.mkdir(parents=True)
    for i in range(12):
        (noisy / f"stale_{i}.py").write_text(
            "# TODO: stale worktree should not count\n", encoding="utf-8"
        )

    report = run_product_audit(repo_root=repo, include_tests=True, max_findings=20, use_llm=False)
    evidence = [
        item
        for section in report.sections
        for finding in section.findings
        for item in finding.evidence
    ]

    assert not any(item.startswith(".claude/") for item in evidence)


def test_product_audit_counts_tests_and_excludes_build_outputs(tmp_path: Path) -> None:
    from skyn3t.audit import run_product_audit
    from skyn3t.audit.agents import AuditContext

    repo = _fixture_repo(tmp_path / "repo")
    generated = repo / "build" / "lib" / "skyn3t"
    generated.mkdir(parents=True)
    (generated / "stale.py").write_text("# TODO: generated copy\n", encoding="utf-8")
    release_copy = repo / ".release_verify_wheel" / "unpack" / "skyn3t"
    release_copy.mkdir(parents=True)
    (release_copy / "stale.py").write_text(
        "# TODO: unpacked release copy\n", encoding="utf-8"
    )
    installed_copy = repo / "logs" / "installed-wheel" / "skyn3t"
    installed_copy.mkdir(parents=True)
    (installed_copy / "stale.py").write_text(
        "# TODO: installed validation copy\n", encoding="utf-8"
    )

    ctx = AuditContext(repo_root=repo)
    assert ctx.rel(repo / "tests" / "test_runner.py") == "tests/test_runner.py"

    report = run_product_audit(repo_root=repo, include_tests=True, max_findings=20, use_llm=False)
    repo_review = next(section for section in report.sections if section.title == "Repo Review")

    assert repo_review.metrics["code_files"] == 3
    assert repo_review.metrics["source_files"] == 2
    assert repo_review.metrics["test_files"] == 1
    assert not any(
        item.startswith(("build/", ".release_verify_", "logs/"))
        for finding in repo_review.findings
        for item in finding.evidence
    )


def test_product_audit_does_not_skip_checkout_under_build_ancestor(tmp_path: Path) -> None:
    from skyn3t.audit.agents import AuditContext

    repo = _fixture_repo(tmp_path / "build" / "repo")
    files = AuditContext(repo_root=repo).iter_files({".py"})

    assert {path.relative_to(repo).as_posix() for path in files} == {
        "skyn3t/agents/reviewer.py",
        "skyn3t/studio/runner.py",
        "tests/test_runner.py",
    }


def test_audit_product_cli_writes_explicit_report_paths(monkeypatch, tmp_path: Path) -> None:
    from skyn3t.config import settings as settings_mod
    from skyn3t.config.settings import REPO_ROOT

    settings_mod.get_settings.cache_clear()
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SKYN3T_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SKYN3T_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("SKYN3T_VECTOR_DB_PATH", str(tmp_path / "vdb"))
    monkeypatch.setenv("SKYN3T_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'data' / 'test.db'}")

    md_path = tmp_path / "audit.md"
    json_path = tmp_path / "audit.json"
    result = runner.invoke(
        app,
        [
            "audit",
            "product",
            "--out",
            str(md_path),
            "--json-out",
            str(json_path),
            "--max-findings",
            "6",
            "--no-llm",
            "--no-include-tests",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["repo_state"]["repo_root"] == REPO_ROOT.name
    assert md_path.is_file()
    assert json_path.is_file()
    assert "Product audit written" in result.output
    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed["sections"]
    assert parsed["prioritized_handoff"]

    settings_mod.get_settings.cache_clear()


def test_audit_product_cli_anchors_repo_root_to_checkout(monkeypatch, tmp_path: Path) -> None:
    from skyn3t.config import settings as settings_mod
    from skyn3t.config.settings import REPO_ROOT

    settings_mod.get_settings.cache_clear()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("SKYN3T_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SKYN3T_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SKYN3T_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("SKYN3T_VECTOR_DB_PATH", str(tmp_path / "vdb"))
    monkeypatch.setenv("SKYN3T_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'data' / 'test.db'}")

    md_path = tmp_path / "audit.md"
    json_path = tmp_path / "audit.json"
    result = runner.invoke(
        app,
        [
            "audit",
            "product",
            "--out",
            str(md_path),
            "--json-out",
            str(json_path),
            "--max-findings",
            "1",
            "--no-llm",
            "--no-include-tests",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["repo_state"]["repo_root"] == REPO_ROOT.name
    assert str(outside) not in json.dumps(data)
    assert md_path.is_file()
    assert json_path.is_file()

    settings_mod.get_settings.cache_clear()
