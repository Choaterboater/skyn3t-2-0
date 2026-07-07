from __future__ import annotations

from pathlib import Path

from skyn3t.studio.assessment_hygiene import check_assessment_artifacts


def test_assessment_hygiene_current_repo_passes():
    root = Path(__file__).resolve().parents[1]

    checks = check_assessment_artifacts(root)

    assert checks
    assert all(check["ok"] for check in checks), checks
