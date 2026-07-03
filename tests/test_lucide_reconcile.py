"""reconcile_lucide_icons — replace hallucinated lucide-react icon imports (names the
package doesn't export, e.g. GeneratorIcon) with a real icon, in the import + usages,
without creating duplicate import specifiers."""
from __future__ import annotations

from skyn3t.studio.proof_run import reconcile_lucide_icons


def _fake_lucide(root, names):
    dist = root / "node_modules" / "lucide-react" / "dist"
    dist.mkdir(parents=True)
    (dist / "lucide-react.d.ts").write_text(
        "".join(f"declare const {n}: LucideIcon;\n" for n in names))


def test_replaces_hallucinated_icon(tmp_path):
    _fake_lucide(tmp_path, ["Zap", "Circle", "Wrench"])
    f = tmp_path / "Comp.jsx"
    f.write_text("import { Zap, GeneratorIcon } from 'lucide-react';\n"
                 "export default () => <div><Zap/><GeneratorIcon/></div>;\n")
    assert "Comp.jsx" in reconcile_lucide_icons(tmp_path)
    body = f.read_text()
    assert "GeneratorIcon" not in body          # hallucinated name gone (import + usage)
    assert "Zap" in body                         # real icon kept


def test_no_duplicate_import_when_fallback_already_present(tmp_path):
    # GeneratorIcon -> Circle, but Circle is already imported: must NOT yield {Circle, Circle}.
    _fake_lucide(tmp_path, ["Circle", "Square", "Star", "Box"])
    f = tmp_path / "C.jsx"
    f.write_text("import { Circle, GeneratorIcon, PipeIcon } from 'lucide-react';\n"
                 "export default () => <><Circle/><GeneratorIcon/><PipeIcon/></>;\n")
    reconcile_lucide_icons(tmp_path)
    body = f.read_text()
    import_line = next(ln for ln in body.splitlines() if "lucide-react" in ln)
    assert import_line.count("Circle") == 1      # deduped, not {Circle, Circle, Circle}
    assert "GeneratorIcon" not in body and "PipeIcon" not in body


def test_reconciles_common_hallucination_without_installed_lucide(tmp_path):
    # The build pipeline runs deterministic repairs before npm install, so common
    # hallucinated lucide names must still be fixable without node_modules.
    f = tmp_path / "X.jsx"
    f.write_text("import { GolfIcon } from 'lucide-react';\n"
                 "export default () => <GolfIcon />;\n")
    assert reconcile_lucide_icons(tmp_path) == ["X.jsx"]
    body = f.read_text()
    assert "GolfIcon" not in body
    assert "Flag" in body


def test_keeps_all_real_icons(tmp_path):
    _fake_lucide(tmp_path, ["Zap", "Wrench", "Thermometer"])
    f = tmp_path / "Ok.jsx"
    f.write_text("import { Zap, Wrench, Thermometer } from 'lucide-react';\n")
    assert reconcile_lucide_icons(tmp_path) == []   # nothing hallucinated -> no change


def test_keeps_single_letter_and_export_alias_icons(tmp_path):
    dist = tmp_path / "node_modules" / "lucide-react" / "dist"
    dist.mkdir(parents=True)
    (dist / "lucide-react.d.ts").write_text(
        "declare const X: LucideIcon;\n"
        "declare const CircleCheckBig: LucideIcon;\n"
        "declare const LoaderCircle: LucideIcon;\n"
        "export { CircleCheckBig as CheckCircle, LoaderCircle as Loader2, X };\n",
        encoding="utf-8",
    )
    f = tmp_path / "RealAliases.jsx"
    f.write_text("import { X, CheckCircle, Loader2 } from 'lucide-react';\n")
    assert reconcile_lucide_icons(tmp_path) == []
    assert f.read_text() == "import { X, CheckCircle, Loader2 } from 'lucide-react';\n"
