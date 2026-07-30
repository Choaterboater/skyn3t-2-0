"""CommonJS test files under `"type": "module"` never run.

Codegen routinely emits a package.json with `"type": "module"` and then writes
test files in CommonJS. Node refuses them:

    ReferenceError: require is not defined in ES module scope, you can use
    import instead. This file is being treated as an ES module because it has a
    '.js' file extension and '.../package.json' contains "type": "module".

Renaming to `.cjs` is the fix rather than rewriting to ESM, and the reason is
`__dirname`. Measured across a real delivered suite, all seven test files did
`path.resolve(__dirname, '..')` — a binding that does not exist in an ES
module. Converting would mean rewriting every require AND substituting
`import.meta.dirname`; the rename fixes both at once and touches no code.

Measured on that build, combined with the `node --test <dir>` glob repair:

    as delivered        1 phantom test,  0 pass,  1 fail (MODULE_NOT_FOUND)
    glob repair only    7 files,         0 pass,  7 fail (require not defined)
    glob + this repair  37 tests,       27 pass, 10 fail  <- real app defects

The 10 survivors are genuine findings ("input lacks an accessible label",
"index.html lacks aria-current"), which is the signal the fix loop is supposed
to receive.
"""

from __future__ import annotations

import json

from skyn3t.studio.proof_run import apply_deterministic_repairs

_CJS = (
    "const test = require('node:test');\n"
    "const assert = require('node:assert/strict');\n"
    "const path = require('node:path');\n"
    "const root = path.resolve(__dirname, '..');\n"
    "test('works', () => { assert.ok(root); });\n"
)
_ESM = (
    "import test from 'node:test';\n"
    "import assert from 'node:assert/strict';\n"
    "test('works', () => { assert.ok(true); });\n"
)


def _pkg(root, *, type_module: bool = True, test_script: str = "node --test tests") -> None:
    payload = {"name": "demo-app", "version": "1.0.0", "scripts": {"test": test_script}}
    if type_module:
        payload["type"] = "module"
    (root / "package.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write(root, relpath: str, body: str):
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_commonjs_test_file_is_renamed_to_cjs(tmp_path):
    _pkg(tmp_path)
    _write(tmp_path, "tests/content.test.js", _CJS)

    repairs = apply_deterministic_repairs(tmp_path)

    assert (tmp_path / "tests/content.test.cjs").is_file()
    assert not (tmp_path / "tests/content.test.js").exists()
    assert repairs["cjs_tests_renamed"]


def test_the_file_body_is_not_rewritten(tmp_path):
    """The whole point of renaming is that require and __dirname keep working."""
    _pkg(tmp_path)
    _write(tmp_path, "tests/content.test.js", _CJS)

    apply_deterministic_repairs(tmp_path)

    body = (tmp_path / "tests/content.test.cjs").read_text(encoding="utf-8")
    assert body == _CJS


def test_an_esm_test_file_is_left_alone(tmp_path):
    """It already runs under "type": "module" — renaming it would BREAK it."""
    _pkg(tmp_path)
    _write(tmp_path, "tests/content.test.js", _ESM)

    apply_deterministic_repairs(tmp_path)

    assert (tmp_path / "tests/content.test.js").is_file()
    assert not (tmp_path / "tests/content.test.cjs").exists()


def test_a_mixed_file_is_left_alone(tmp_path):
    """import + require together is ambiguous. Do no harm; let the fix loop see it."""
    _pkg(tmp_path)
    _write(tmp_path, "tests/mixed.test.js", "import x from 'y';\nconst z = require('w');\n")

    apply_deterministic_repairs(tmp_path)

    assert (tmp_path / "tests/mixed.test.js").is_file()


def test_without_type_module_commonjs_already_works(tmp_path):
    """A .js file in a non-module package IS CommonJS. Nothing to fix."""
    _pkg(tmp_path, type_module=False)
    _write(tmp_path, "tests/content.test.js", _CJS)

    apply_deterministic_repairs(tmp_path)

    assert (tmp_path / "tests/content.test.js").is_file()
    assert not (tmp_path / "tests/content.test.cjs").exists()


def test_mjs_is_never_renamed(tmp_path):
    """.mjs is explicitly ESM by extension — a rename would be a downgrade."""
    _pkg(tmp_path)
    _write(tmp_path, "tests/content.test.mjs", _ESM)

    apply_deterministic_repairs(tmp_path)

    assert (tmp_path / "tests/content.test.mjs").is_file()


def test_non_test_source_files_are_never_touched(tmp_path):
    """Only *.test.js. Renaming app source would break every import of it."""
    _pkg(tmp_path)
    _write(tmp_path, "assets/js/storage.js", "const fs = require('node:fs');\n")

    apply_deterministic_repairs(tmp_path)

    assert (tmp_path / "assets/js/storage.js").is_file()


def test_an_existing_cjs_target_is_not_clobbered(tmp_path):
    _pkg(tmp_path)
    _write(tmp_path, "tests/content.test.js", _CJS)
    _write(tmp_path, "tests/content.test.cjs", "// hand-written, keep me\n")

    apply_deterministic_repairs(tmp_path)

    assert (tmp_path / "tests/content.test.cjs").read_text(encoding="utf-8") == (
        "// hand-written, keep me\n"
    )
    assert (tmp_path / "tests/content.test.js").is_file()


def test_an_explicit_script_reference_is_updated(tmp_path):
    """Renaming a file the test script names by hand would break the script."""
    _pkg(tmp_path, test_script="node --test tests/content.test.js")
    _write(tmp_path, "tests/content.test.js", _CJS)

    apply_deterministic_repairs(tmp_path)

    script = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))["scripts"]["test"]
    assert script == "node --test tests/content.test.cjs"


def test_the_emitted_glob_follows_the_rename(tmp_path):
    """Ordering contract: the rename must run BEFORE the directory-glob repair,
    or the glob is generated from extensions that no longer exist."""
    _pkg(tmp_path)
    _write(tmp_path, "tests/content.test.js", _CJS)

    apply_deterministic_repairs(tmp_path)

    script = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))["scripts"]["test"]
    assert ".test.cjs" in script
    assert ".test.js\"" not in script


def test_the_repair_is_idempotent(tmp_path):
    _pkg(tmp_path)
    _write(tmp_path, "tests/content.test.js", _CJS)

    apply_deterministic_repairs(tmp_path)
    repairs = apply_deterministic_repairs(tmp_path)

    assert not repairs["cjs_tests_renamed"]
    assert (tmp_path / "tests/content.test.cjs").is_file()
