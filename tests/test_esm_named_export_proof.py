"""Offline proof for local ESM named-import/export contracts."""

from __future__ import annotations

from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.core.events import EventBus
from skyn3t.studio.proof_run import proof_run


def _static_entry(root) -> None:
    (root / "index.html").write_text(
        '<main>ComfortFlow</main><script type="module" src="./main.js"></script>',
        encoding="utf-8",
    )


def test_static_proof_rejects_hvac_default_object_as_named_export(tmp_path):
    """Exact historical defect: object membership is not a named ESM export."""
    _static_entry(tmp_path)
    module = tmp_path / "js" / "main.js"
    module.parent.mkdir()
    (tmp_path / "main.js").write_text(
        "import { bootstrap } from './js/main.js';\nbootstrap();\n",
        encoding="utf-8",
    )
    module.write_text(
        "async function bootstrap() { return true; }\n"
        "export default { bootstrap };\n",
        encoding="utf-8",
    )

    result = proof_run(tmp_path, stack="static_html", execution_backend="inline")

    assert result.passed is False
    assert result.missing == ["<exports>"]
    assert result.detail["named_export_mismatches"] == [{
        "importer": "main.js",
        "specifier": "./js/main.js",
        "module": "js/main.js",
        "missing": ["bootstrap"],
    }]
    gap = next(g for g in result.error_gaps() if g.startswith("NAMED EXPORT MISMATCH"))
    assert "module js/main.js is missing named export(s) bootstrap" in gap
    assert "imported by main.js via ./js/main.js" in gap


def test_hvac_export_gap_targets_resolved_module_not_root_importer(tmp_path):
    _static_entry(tmp_path)
    module = tmp_path / "js" / "main.js"
    module.parent.mkdir()
    (tmp_path / "main.js").write_text(
        "import { bootstrap } from './js/main.js';\nbootstrap();\n",
        encoding="utf-8",
    )
    module.write_text(
        "function bootstrap() {}\nexport default { bootstrap };\n",
        encoding="utf-8",
    )
    proof = proof_run(tmp_path, stack="static_html", execution_backend="inline")
    agent = CodeImproverAgent(event_bus=EventBus())

    assert agent._targets_from_gaps(proof.error_gaps(), tmp_path) == ["js/main.js"]


def test_static_proof_accepts_explicit_named_export_and_alias(tmp_path):
    _static_entry(tmp_path)
    module = tmp_path / "js" / "main.js"
    module.parent.mkdir()
    (tmp_path / "main.js").write_text(
        "import { bootstrap as start, setup } from './js/main.js';\n"
        "start(); setup();\n",
        encoding="utf-8",
    )
    module.write_text(
        "async function bootstrap() {}\n"
        "export function setup() {}\n"
        "export { bootstrap };\n",
        encoding="utf-8",
    )

    result = proof_run(tmp_path, stack="static_html", execution_backend="inline")

    assert result.passed is True
    assert "named_export_mismatches" not in result.detail


def test_named_default_binding_is_not_misclassified_as_named_export(tmp_path):
    _static_entry(tmp_path)
    module = tmp_path / "js" / "main.js"
    module.parent.mkdir()
    (tmp_path / "main.js").write_text(
        "import { default as start } from './js/main.js';\nstart();\n",
        encoding="utf-8",
    )
    module.write_text("export default function bootstrap() {}\n", encoding="utf-8")

    result = proof_run(tmp_path, stack="static_html", execution_backend="inline")

    assert result.passed is True
    assert "named_export_mismatches" not in result.detail


def test_static_export_scan_degrades_open_for_star_reexport(tmp_path):
    """A star re-export can satisfy the name; the small scanner must not guess."""
    _static_entry(tmp_path)
    module = tmp_path / "js" / "main.js"
    module.parent.mkdir()
    (tmp_path / "main.js").write_text(
        "import { bootstrap } from './js/main.js';\nbootstrap();\n",
        encoding="utf-8",
    )
    module.write_text("export * from './runtime.js';\n", encoding="utf-8")
    (module.parent / "runtime.js").write_text(
        "export function bootstrap() {}\n", encoding="utf-8"
    )

    result = proof_run(tmp_path, stack="static_html", execution_backend="inline")

    assert result.passed is True
    assert "named_export_mismatches" not in result.detail


def test_comments_and_templates_do_not_create_phantom_import_contracts(tmp_path):
    _static_entry(tmp_path)
    module = tmp_path / "js" / "main.js"
    module.parent.mkdir()
    module.write_text("export default {};\n", encoding="utf-8")
    (tmp_path / "main.js").write_text(
        "/*\nimport { phantom } from './js/main.js';\n*/\n"
        "const example = `\nimport { alsoPhantom } from './js/main.js';\n`;\n"
        "console.log(example);\n",
        encoding="utf-8",
    )

    result = proof_run(tmp_path, stack="static_html", execution_backend="inline")

    assert result.passed is True
    assert "named_export_mismatches" not in result.detail
