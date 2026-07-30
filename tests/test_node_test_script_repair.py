"""`node --test <dir>` does not discover tests, so the suite is a false failure.

Measured on Node v24.11.1 / Windows against a delivered build:

    $ node --test tests
    Error: Cannot find module 'D:\\Projects\\...\\tests'
        at Module._resolveFilename (node:internal/modules/cjs/loader:1421:15)
      code: 'MODULE_NOT_FOUND'
    i tests 1 / i fail 1

Node resolves the positional argument as a module entry point instead of
walking it, so the runner reports exactly one failing "test" whose message
names a CommonJS loader. That is doubly misleading: the app may be pure ESM,
and the real test content is never executed at all.

Isolated to the argument form, not the test content — a known-good ESM test
passes with a glob and fails with the bare directory:

    node --test tests                -> MODULE_NOT_FOUND, pass 0, fail 1
    node --test "tests/*.test.js"    -> pass 1, fail 0

So every delivered app whose test script is `node --test <dir>` reported a
failing suite no matter how good its tests were, and the captured error text
sent the repair loop after an imaginary missing module.
"""

from __future__ import annotations

import json

from skyn3t.studio.proof_run import apply_deterministic_repairs


def _pkg(root, test_script: str, *, type_module: bool = True) -> None:
    payload = {"name": "demo-app", "version": "1.0.0", "scripts": {"test": test_script}}
    if type_module:
        payload["type"] = "module"
    (root / "package.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _test_file(root, relpath: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert';\n"
        "test('works', () => { assert.ok(true); });\n",
        encoding="utf-8",
    )


def _script(root) -> str:
    return json.loads((root / "package.json").read_text(encoding="utf-8"))["scripts"]["test"]


def test_bare_directory_argument_is_rewritten_to_a_glob(tmp_path):
    _pkg(tmp_path, "node --test tests")
    _test_file(tmp_path, "tests/content.test.js")

    repairs = apply_deterministic_repairs(tmp_path)

    script = _script(tmp_path)
    assert "tests" in script and "*" in script, script
    assert script != "node --test tests"
    assert repairs["node_test_script"], "the repair must report what it changed"


def test_a_trailing_slash_is_also_a_directory(tmp_path):
    _pkg(tmp_path, "node --test tests/")
    _test_file(tmp_path, "tests/content.test.js")

    apply_deterministic_repairs(tmp_path)

    assert "*" in _script(tmp_path)


def test_nested_test_files_stay_reachable(tmp_path):
    """The rewrite must recurse: ** is why a glob can replace a directory walk."""
    _pkg(tmp_path, "node --test tests")
    _test_file(tmp_path, "tests/top.test.js")
    _test_file(tmp_path, "tests/unit/deep.test.js")

    apply_deterministic_repairs(tmp_path)

    assert "**" in _script(tmp_path)


def test_an_explicit_file_argument_is_left_alone(tmp_path):
    """Only a DIRECTORY is broken. A file path already works — do no harm."""
    _pkg(tmp_path, "node --test tests/rendered-html.test.mjs")
    _test_file(tmp_path, "tests/rendered-html.test.mjs")

    apply_deterministic_repairs(tmp_path)

    assert _script(tmp_path) == "node --test tests/rendered-html.test.mjs"


def test_auto_discovery_without_a_positional_argument_is_left_alone(tmp_path):
    """`node --test` with only flags discovers on its own; nothing to repair."""
    _pkg(tmp_path, "node --test --test-concurrency=1")
    _test_file(tmp_path, "tests/content.test.js")

    apply_deterministic_repairs(tmp_path)

    assert _script(tmp_path) == "node --test --test-concurrency=1"


def test_a_non_node_test_runner_is_left_alone(tmp_path):
    """vitest/jest own their own discovery — never touch them."""
    _pkg(tmp_path, "vitest run")
    _test_file(tmp_path, "tests/content.test.js")

    apply_deterministic_repairs(tmp_path)

    assert _script(tmp_path) == "vitest run"


def test_a_missing_directory_is_not_rewritten(tmp_path):
    """Rewriting to a glob that matches nothing would exit 0 and report a PASS
    with zero tests — turning a broken suite into false green evidence."""
    _pkg(tmp_path, "node --test tests")

    apply_deterministic_repairs(tmp_path)

    assert _script(tmp_path) == "node --test tests"


def test_the_emitted_glob_only_covers_extensions_that_exist(tmp_path):
    """Same reason: every emitted pattern must match at least one real file."""
    _pkg(tmp_path, "node --test tests")
    _test_file(tmp_path, "tests/content.test.js")

    apply_deterministic_repairs(tmp_path)

    script = _script(tmp_path)
    assert ".test.js" in script
    assert ".mjs" not in script and ".cjs" not in script


def test_the_repair_is_idempotent(tmp_path):
    """apply_deterministic_repairs runs every fix-loop iteration."""
    _pkg(tmp_path, "node --test tests")
    _test_file(tmp_path, "tests/content.test.js")

    apply_deterministic_repairs(tmp_path)
    once = _script(tmp_path)
    repairs = apply_deterministic_repairs(tmp_path)

    assert _script(tmp_path) == once
    assert not repairs["node_test_script"], "a repaired tree must be a no-op"


def test_a_project_without_package_json_is_untouched(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")

    repairs = apply_deterministic_repairs(tmp_path)

    assert not repairs["node_test_script"]
