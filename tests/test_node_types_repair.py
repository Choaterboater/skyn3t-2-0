"""A TypeScript project importing `node:` builtins needs @types/node.

From a real delivered Astro site (52 files, every one of 36 checklist items
present, proof's own content score 100):

    tests/links.test.ts:2:65 - error ts(2307):
        Cannot find module 'node:fs' or its corresponding type declarations.

Twelve of those failed `astro check`, which failed the build step, which
failed proof — and proof blocks in every posture because a delivery that
cannot build genuinely does not run. Final verdict: no_go at 44. The app was
fine; it was missing a types package.

The gap is structural rather than bad luck. reconcile_npm_deps deliberately
skips `node:` specifiers because they need no runtime package — correct for
dependencies, and precisely why nothing ever supplied their TYPES.
"""

from __future__ import annotations

import json

from skyn3t.studio.proof_run import apply_deterministic_repairs

_TS_USING_NODE = (
    "import { existsSync } from 'node:fs';\n"
    "import { join } from 'node:path';\n"
    "export const root = join('.', 'x');\n"
)


def _pkg(root, *, dev=None, deps=None) -> None:
    payload = {"name": "demo-app", "version": "1.0.0"}
    if deps:
        payload["dependencies"] = deps
    if dev is not None:
        payload["devDependencies"] = dev
    (root / "package.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _dev(root) -> dict:
    return json.loads((root / "package.json").read_text(encoding="utf-8")).get(
        "devDependencies", {}
    )


def test_types_node_is_added_for_a_typescript_project(tmp_path):
    _pkg(tmp_path, dev={"typescript": "^5"})
    _write(tmp_path, "tests/links.test.ts", _TS_USING_NODE)

    repairs = apply_deterministic_repairs(tmp_path)

    assert "@types/node" in _dev(tmp_path)
    assert repairs["node_types_added"] == ["@types/node"]


def test_a_tsconfig_alone_is_enough_to_type_check(tmp_path):
    """Astro declares @astrojs/check rather than typescript directly."""
    _pkg(tmp_path, dev={"@astrojs/check": "^0.9"})
    (tmp_path / "tsconfig.json").write_text('{"extends": "astro/tsconfigs/strict"}', encoding="utf-8")
    _write(tmp_path, "tests/links.test.ts", _TS_USING_NODE)

    apply_deterministic_repairs(tmp_path)

    assert "@types/node" in _dev(tmp_path)


def test_astro_script_blocks_count_as_typed_sources(tmp_path):
    _pkg(tmp_path, dev={"typescript": "^5"})
    _write(tmp_path, "src/pages/index.astro", "---\nimport { readFileSync } from 'node:fs';\n---\n<h1>hi</h1>\n")

    apply_deterministic_repairs(tmp_path)

    assert "@types/node" in _dev(tmp_path)


def test_a_project_that_does_not_type_check_is_left_alone(tmp_path):
    """Plain JS with no tsconfig never type-checks, so @types/node is inert."""
    _pkg(tmp_path, dev={"vitest": "^2"})
    _write(tmp_path, "tests/links.test.js", "const { join } = require('node:path');\n")

    repairs = apply_deterministic_repairs(tmp_path)

    assert "@types/node" not in _dev(tmp_path)
    assert repairs["node_types_added"] == []


def test_a_project_not_importing_node_builtins_is_left_alone(tmp_path):
    _pkg(tmp_path, dev={"typescript": "^5"})
    _write(tmp_path, "src/app.ts", "export const x = 1;\n")

    apply_deterministic_repairs(tmp_path)

    assert "@types/node" not in _dev(tmp_path)


def test_an_existing_declaration_is_never_overwritten(tmp_path):
    _pkg(tmp_path, dev={"typescript": "^5", "@types/node": "^20.1.2"})
    _write(tmp_path, "tests/links.test.ts", _TS_USING_NODE)

    repairs = apply_deterministic_repairs(tmp_path)

    assert _dev(tmp_path)["@types/node"] == "^20.1.2"
    assert repairs["node_types_added"] == []


def test_a_runtime_dependency_declaration_also_counts(tmp_path):
    """Declared anywhere is declared — don't add a duplicate to devDependencies."""
    _pkg(tmp_path, dev={"typescript": "^5"}, deps={"@types/node": "^22"})
    _write(tmp_path, "tests/links.test.ts", _TS_USING_NODE)

    apply_deterministic_repairs(tmp_path)

    assert "@types/node" not in _dev(tmp_path)


def test_the_repair_is_idempotent(tmp_path):
    _pkg(tmp_path, dev={"typescript": "^5"})
    _write(tmp_path, "tests/links.test.ts", _TS_USING_NODE)

    apply_deterministic_repairs(tmp_path)
    second = apply_deterministic_repairs(tmp_path)

    assert second["node_types_added"] == []


def test_a_non_node_project_is_untouched(tmp_path):
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")

    repairs = apply_deterministic_repairs(tmp_path)

    assert repairs["node_types_added"] == []
