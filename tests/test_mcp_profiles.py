from typer.testing import CliRunner

from skyn3t.agents._common import knowledge_block
from skyn3t.cli.main import app
from skyn3t.studio.mcp_profiles import MIST_PROFILE, mcp_profile_instructions, mcp_profile_product


def test_profile_binds_separate_inventory_and_promptless_requirements():
    product = mcp_profile_product(MIST_PROFILE, "mist-rich", "Read-only proactive Mist MCP")
    assert product.acceptance_registry_version == 1
    identifiers = {item.acceptance_ids[0] for item in product.requirements}
    assert len(identifiers) == 13
    assert {
        "mcp:capability:mist.inventory",
        "mcp:capability:mist.worker",
        "mcp:capability:mist.notifications",
        "mcp:capability:mist.scope",
    } <= identifiers


def test_mcp_profile_is_not_truncated_by_the_advisory_skill_budget():
    product = mcp_profile_product(MIST_PROFILE, "mist-rich", "Read-only proactive Mist MCP")
    prompt = knowledge_block({"extra": {
        "product_spec": product.to_dict(), "skills_advice": "advisory " * 2000,
    }})
    assert "MANDATORY MCP CAPABILITY PROFILE" in prompt
    assert "mist.worker" in prompt
    assert "mist.credentials" in prompt
    assert "check_mcp" in prompt
    assert "capability_profile=" in prompt
    assert "not an application dependency" in prompt


def test_cli_rejects_profile_for_a_different_stack_before_building():
    result = CliRunner().invoke(app, [
        "studio", "build", "Build a monitor", "--stack", "python",
        "--mcp-profile", MIST_PROFILE, "--yes",
    ])
    assert result.exit_code == 2
    assert "MCP capability profiles require --stack mcp" in result.output


def test_build_time_checker_uses_own_factory_not_ambient_pythonpath(tmp_path):
    import json
    import os
    import shlex
    import subprocess

    wrong_factory = tmp_path / "skyn3t"
    wrong_factory.mkdir()
    (wrong_factory / "__init__.py").write_text('raise RuntimeError("WRONG_FACTORY")\n')
    (tmp_path / "server.py").write_text("pass\n")
    command = next(
        line for line in mcp_profile_instructions(MIST_PROFILE).splitlines()
        if " -c " in line and "check_mcp" in line
    )
    result = subprocess.run(
        shlex.split(command), cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    evidence = json.loads(result.stdout)
    assert evidence["checked"]["capability_coverage"]["profile"] == MIST_PROFILE
    assert not evidence["ok"]
