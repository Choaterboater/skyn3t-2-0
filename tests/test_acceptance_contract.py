from __future__ import annotations

from skyn3t.studio.acceptance_contract import (
    GENERATED_ACCEPTANCE_HEADER,
    GENERATED_ACCEPTANCE_PENDING_MARKER,
    is_system_acceptance_contract,
    restore_acceptance_contracts,
    snapshot_acceptance_contracts,
)


def _generated_contract() -> str:
    return (
        f'"""{GENERATED_ACCEPTANCE_HEADER}\n"""\n'
        "import pytest\n"
        f'@pytest.mark.skip(reason="{GENERATED_ACCEPTANCE_PENDING_MARKER}")\n'
        "def test_pending_contract():\n    assert True\n"
    )


def test_system_contract_requires_exact_path_header_and_pending_marker(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    system = tests / "test_acceptance_app.py"
    system.write_text(_generated_contract(), encoding="utf-8")

    assert is_system_acceptance_contract(system, root)
    assert is_system_acceptance_contract(
        "tests/test_acceptance_app.py", root
    )

    # A filename alone cannot grant immutability or a BuildVerifier skip exemption.
    user_named = tests / "test_acceptance_user.py"
    user_named.write_text("def test_real_user_case():\n    assert True\n", encoding="utf-8")
    assert not is_system_acceptance_contract(user_named, root)

    # Both generator markers are required, not a loose substring match.
    header_only = tests / "test_acceptance_header.py"
    header_only.write_text(
        f'"""{GENERATED_ACCEPTANCE_HEADER}\n"""\n', encoding="utf-8"
    )
    assert not is_system_acceptance_contract(header_only, root)

    # Only the direct system path is accepted; nested lookalikes are user files.
    nested = tests / "nested" / "test_acceptance_app.py"
    nested.parent.mkdir()
    nested.write_text(_generated_contract(), encoding="utf-8")
    assert not is_system_acceptance_contract(nested, root)


def test_snapshot_and_restore_preserves_only_preexisting_system_contracts(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    system = tests / "test_acceptance_app.py"
    original = _generated_contract().encode()
    system.write_bytes(original)
    user_test = tests / "test_acceptance_user.py"
    user_test.write_text("def test_user():\n    assert False\n", encoding="utf-8")

    snapshot = snapshot_acceptance_contracts(root)
    assert snapshot == {"tests/test_acceptance_app.py": original}

    system.write_text("def test_fake_green():\n    assert True\n", encoding="utf-8")
    user_test.write_text("def test_user():\n    assert True\n", encoding="utf-8")
    restored = restore_acceptance_contracts(root, snapshot)

    assert restored == ["tests/test_acceptance_app.py"]
    assert system.read_bytes() == original
    assert "assert True" in user_test.read_text(encoding="utf-8")
    assert restore_acceptance_contracts(root, snapshot) == []


def test_restore_rebuilds_tests_directory_replaced_by_a_file(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    system = tests / "test_acceptance_app.py"
    original = _generated_contract().encode()
    system.write_bytes(original)
    snapshot = snapshot_acceptance_contracts(root)

    system.unlink()
    tests.rmdir()
    tests.write_text("model replaced the test directory", encoding="utf-8")

    restored = restore_acceptance_contracts(root, snapshot)

    assert restored == ["tests/test_acceptance_app.py"]
    assert tests.is_dir()
    assert system.read_bytes() == original
