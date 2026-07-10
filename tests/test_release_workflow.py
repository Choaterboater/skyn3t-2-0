from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"
GUIDE_PATH = ROOT / "docs" / "RELEASING.md"


def _workflow() -> dict:
    parsed = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return parsed


def _script(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def test_release_workflow_is_tag_only_and_separates_privileged_jobs() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]

    assert set(workflow["on"]) == {"push"}
    assert "tags" in workflow["on"]["push"]
    assert workflow["permissions"] == {"contents": "read"}
    assert set(jobs) == {"build", "attest", "github-release", "pypi"}
    assert jobs["build"].get("permissions") is None
    assert jobs["attest"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
        "artifact-metadata": "write",
    }
    assert jobs["github-release"]["permissions"] == {"contents": "write"}
    assert jobs["pypi"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert jobs["pypi"]["environment"]["name"] == "pypi"


def test_release_build_proves_quality_and_byte_reproducibility() -> None:
    workflow = _workflow()
    build = workflow["jobs"]["build"]
    script = _script(build)

    assert "npm test" in script and "npm run build" in script
    assert "ruff check" in script and "mypy skyn3t" in script
    assert "python -m pytest -q" in script
    assert script.count("python -m build --no-isolation") == 2
    assert "prepare_release.py check-tag" in script
    assert "prepare_release.py verify" in script
    assert "check_release_wheel.py" in script
    assert "sha256sum --check SHA256SUMS" in script

    upload = next(step for step in build["steps"] if "upload-artifact@" in step.get("uses", ""))
    assert upload["uses"] == "actions/upload-artifact@v7.0.1"
    assert upload["with"]["archive"] == "true"
    assert upload["with"]["compression-level"] == "0"
    assert upload["with"]["if-no-files-found"] == "error"


def test_release_attests_before_github_and_optional_pypi_publish() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]

    attest = next(step for step in jobs["attest"]["steps"] if step.get("uses", "").startswith("actions/attest@"))
    assert attest["uses"] == "actions/attest@v4.1.1"
    assert attest["if"] == "vars.ARTIFACT_ATTESTATION_ENABLED == 'true'"
    assert "*.whl" in attest["with"]["subject-path"]
    assert "*.tar.gz" in attest["with"]["subject-path"]
    assert set(jobs["github-release"]["needs"]) == {"build", "attest"}
    assert set(jobs["pypi"]["needs"]) == {"build", "attest"}
    assert jobs["pypi"]["if"] == "vars.PYPI_PUBLISH_ENABLED == 'true'"
    pypi_action = next(
        step for step in jobs["pypi"]["steps"] if step.get("uses", "").startswith("pypa/")
    )
    assert pypi_action["uses"] == "pypa/gh-action-pypi-publish@v1.14.0"
    assert "secrets." not in WORKFLOW_PATH.read_text(encoding="utf-8")
    release_script = _script(jobs["github-release"])
    assert "gh release download" in release_script
    assert "cmp --silent" in release_script
    assert "--clobber" not in release_script


def test_release_guide_documents_verification_and_required_setup() -> None:
    guide = GUIDE_PATH.read_text(encoding="utf-8")

    assert "PYPI_PUBLISH_ENABLED" in guide
    assert "ARTIFACT_ATTESTATION_ENABLED" in guide
    assert "private repositories require GitHub Enterprise Cloud" in guide
    assert "Trusted Publisher" in guide
    assert "gh attestation verify" in guide
    assert "SHA256SUMS" in guide
    assert "project.version" in guide
    assert "never overwritten" in guide
